"""Push negotiated context to Automation Engine over the local session bus."""

import asyncio
from datetime import datetime
import json
import logging

from dbus_next import Message, MessageType


ENGINE_BUS = "io.github.MukhilDharan5.UnityAutomationEngine.Engine"
SINK = "io.github.MukhilDharan5.UnityAutomationEngine.ContextSink1"
SINK_PATH = "/io/github/MukhilDharan5/UnityAutomationEngine/Engine/Context"
DBUS = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MAX_PACKET = 512 * 1024
LOG = logging.getLogger(__name__)


def clock_snapshot():
    now = datetime.now().astimezone()
    return {"time": now.strftime("%H:%M"), "day": DAYS[now.weekday()], "date": now.date().isoformat()}


async def watch_clock(store):
    while True:
        store.update_extra(clock=clock_snapshot())
        now = datetime.now()
        await asyncio.sleep(max(.1, 60 - now.second - now.microsecond / 1_000_000))


async def call(bus, destination, path, interface, member, signature="", body=()):
    reply = await asyncio.wait_for(bus.call(Message(destination=destination, path=path,
        interface=interface, member=member, signature=signature, body=list(body))), timeout=4)
    if reply.message_type == MessageType.ERROR:
        raise OSError(reply.error_name)
    return reply.body


class Publisher:
    """One authenticated connection, ordered changes, and cached lease renewals.

    A snapshot is queued at publication time so a brief true/false edge cannot
    disappear while a preceding D-Bus method call is in flight. Requirements
    are fetched only on connection or RequirementsChanged, never per update.
    """

    def __init__(self, bus, service):
        self.bus, self.service = bus, service
        self.owner = None
        self.events = asyncio.Queue(maxsize=256)
        self.fields = None
        self.renew_after = 15
        self.revision = 0
        self.last_data = None
        self.packet = None
        self.active_owner = None
        self.processed_revision = 0
        self.next_renew = 0

    def published(self, data):
        if self.owner:
            self.enqueue("snapshot", (self.service._revision, data))

    def enqueue(self, kind, value=None):
        if self.events.full():
            # Bound memory if the receiving daemon stalls during a signal storm.
            # Resynchronize to the latest full state after an explicit overrun.
            while not self.events.empty():
                self.events.get_nowait()
            LOG.warning("Automation context queue overrun; resynchronizing current state")
            kind, value = "requirements", None
        self.events.put_nowait((kind, value))

    def changed(self, message):
        if message.message_type != MessageType.SIGNAL:
            return
        if (message.sender == DBUS and message.interface == DBUS and message.member == "NameOwnerChanged"
                and message.body[0] == ENGINE_BUS):
            self.owner = message.body[2]
            self.enqueue("requirements")
        elif (message.sender == self.owner and message.interface == SINK and message.path == SINK_PATH
              and message.member == "RequirementsChanged"):
            self.enqueue("requirements")

    async def negotiate(self, owner):
        (raw,) = await call(self.bus, owner, SINK_PATH, SINK, "GetRequirements")
        requirements = json.loads(raw)
        fields, lease = requirements.get("fields"), requirements.get("lease_seconds")
        if (requirements.get("api_version") != 1 or not isinstance(fields, list)
                or any(not isinstance(name, str) for name in fields)
                or type(lease) not in (int, float) or not 2 <= lease <= 3600):
            raise ValueError("Unsupported Automation Engine context requirements")
        self.fields = tuple(fields)
        self.renew_after = lease / 2

    async def send(self, owner, values):
        data = {name: values[name] for name in self.fields if name in values}
        if data == self.last_data:
            return
        self.revision += 1
        packet = json.dumps({"api_version": 1, "revision": self.revision, "data": data},
                            ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        if len(packet.encode("utf-8")) > MAX_PACKET:
            raise ValueError("Requested context exceeds Automation Engine's 512 KiB packet limit")
        self.last_data, self.packet = data, packet
        await self.renew(owner)

    async def renew(self, owner):
        await call(self.bus, owner, SINK_PATH, SINK, "PublishSnapshot", "s", [self.packet])
        self.next_renew = asyncio.get_running_loop().time() + self.renew_after

    async def watch(self):
        self.bus.add_message_handler(self.changed)
        self.service.listeners.add(self.published)
        try:
            for rule in (
                f"type='signal',sender='{DBUS}',interface='{DBUS}',member='NameOwnerChanged',arg0='{ENGINE_BUS}'",
                f"type='signal',sender='{ENGINE_BUS}',interface='{SINK}',member='RequirementsChanged',path='{SINK_PATH}'",
            ):
                await call(self.bus, DBUS, DBUS_PATH, DBUS, "AddMatch", "s", [rule])
            try:
                (initial,) = await call(self.bus, DBUS, DBUS_PATH, DBUS, "GetNameOwner", "s", [ENGINE_BUS])
            except OSError:
                initial = ""
            if self.owner is None:
                self.owner = initial
            kind, values = "requirements", None
            retry = 1
            while True:
                receiver = self.owner
                if receiver != self.active_owner:
                    self.active_owner = receiver
                    self.fields, self.last_data, self.packet = None, None, None
                    self.revision = 0
                timeout = None
                if receiver:
                    try:
                        if kind == "requirements" or self.fields is None:
                            await self.negotiate(receiver)
                            values = (self.service._revision, self.service._values())
                            self.processed_revision = 0
                            self.last_data = None
                            kind = "snapshot"
                        if receiver == self.owner:
                            if kind == "renew" and self.packet is not None:
                                # Liveness only: reuse bytes and revision, no new readings.
                                await self.renew(receiver)
                            elif values and values[0] > self.processed_revision:
                                await self.send(receiver, values[1])
                                self.processed_revision = values[0]
                        timeout, retry = max(.001, self.next_renew - asyncio.get_running_loop().time()), 1
                    except (OSError, ValueError, TypeError, TimeoutError) as error:
                        LOG.warning("Automation context delivery: %s", error)
                        self.fields = None
                        timeout, retry = retry, min(30, retry * 2)
                try:
                    if timeout is None:
                        kind, values = await self.events.get()
                    else:
                        kind, values = await asyncio.wait_for(self.events.get(), timeout)
                except TimeoutError:
                    kind, values = "renew", None
        finally:
            self.service.listeners.discard(self.published)
            self.bus.remove_message_handler(self.changed)


async def publish_automation(bus, service):
    await Publisher(bus, service).watch()
