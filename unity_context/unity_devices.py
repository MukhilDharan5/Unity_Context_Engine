"""Read-only Unity device observation owned by Context Engine."""

import asyncio
import json

from dbus_next import MessageType

from .automation import DBUS, DBUS_PATH, call
from .media import new_bus
from .unity_choices import device_choices


BUS = "org.unityconnect"
MANAGER_PATH = "/org/unityconnect/Manager"
MANAGER = BUS + ".Manager1"
DEVICE = BUS + ".Device1"
PROPERTIES = "org.freedesktop.DBus.Properties"
PREFIX = "/org/unityconnect/devices/d_"
SENSORS = {
    "battery": ("level", "charging", "plugged", "status", "voltage_v", "current_ma", "power_w", "remaining_seconds", "estimate_kind", "estimate_source", "estimate_state"),
    "network": ("kind", "signal", "operator", "internet"),
    "transport": ("kind", "links", "control", "bulk"),
    "phone_connectivity": ("hotspot_enabled", "bluetooth_enabled", "bluetooth_status", "complete", "reason"),
    "ringer": ("mode",),
    "sound": ("available", "volume", "muted", "name", "error"),
    "desktop_sound": ("available", "volume", "muted", "name", "error"),
    "media": ("title", "artist", "playing", "app_name"),
    "audio": ("state", "source", "transport", "error"),
    "screen": ("state", "source", "error"),
    "call_notifications": ("state",),
    "call_control": ("muted", "route"),
}


def valid_id(value):
    return isinstance(value, str) and len(value) == 32 and all(char in "0123456789abcdef" for char in value)


def sensor_state(raw):
    return {name: {key: value for key, value in raw[name].items() if key in allowed}
            for name, allowed in SENSORS.items() if isinstance(raw.get(name), dict)}


class Observer:
    def __init__(self, bus, store):
        self.bus, self.store = bus, store
        self.owner = None
        self.requested_owner = ""
        self.dirty = asyncio.Event()
        self.epoch = 0

    def publish(self, devices, choices, status, detail, requested=False):
        self.store.update_extra(devices=devices, choices=choices,
            observer={"status": status, "detail": detail, "client": self.bus.unique_name if requested else ""})
        self.store.source("unity", "live" if status == "approved" else "permission_required" if requested else "unavailable", detail)

    def set_owner(self, owner):
        if owner != self.owner:
            self.owner, self.requested_owner = owner, ""
            self.epoch += 1
            self.publish({}, {}, "available" if owner else "offline", "Reading Unity connection status" if owner else "Start Unity Linux for device context")
            self.dirty.set()

    def changed(self, message):
        if message.message_type != MessageType.SIGNAL:
            return
        if (message.sender == DBUS and message.interface == DBUS and message.member == "NameOwnerChanged"
                and message.body[0] == BUS):
            self.set_owner(message.body[2])
        elif (message.sender == self.owner and message.interface == MANAGER and message.member == "Changed"
              and message.body and not message.body[0].startswith("/org/unityconnect/operations/")):
            self.epoch += 1
            self.dirty.set()

    async def manager(self, owner, method, signature="", body=()):
        return await call(self.bus, owner, MANAGER_PATH, MANAGER, method, signature, body)

    async def device(self, owner, path, can_read):
        identifier = path.removeprefix(PREFIX)
        if not path.startswith(PREFIX) or not valid_id(identifier):
            return None
        try:
            (values,) = await call(self.bus, owner, path, PROPERTIES, "GetAll", "s", [DEVICE])
            values = {key: value.value for key, value in values.items()}
            if values.get("Id") != identifier or type(values.get("Connected")) is not bool:
                return None
            capabilities = json.loads(values.get("CapabilitiesJson", "[]"))
            caps = {item["id"]: {"status": item.get("status", "unsupported"), "reason": item.get("reason", "")}
                    for item in capabilities if isinstance(item, dict) and isinstance(item.get("id"), str)}
            record = {"id": identifier, "name": str(values.get("Name", identifier))[:128],
                      "connected": values["Connected"], "capabilities": caps, "state": {}}
            choices = {}
            if can_read and record["connected"]:
                (raw,) = await call(self.bus, owner, path, DEVICE, "GetState")
                state = json.loads(raw).get("state", {})
                if isinstance(state, dict):
                    record["state"] = sensor_state(state)
                    choices = device_choices(state)
            return record, choices
        except (OSError, ValueError, TypeError):
            return None

    async def refresh(self, owner):
        (version,) = await call(self.bus, owner, MANAGER_PATH, PROPERTIES, "Get", "ss", [MANAGER, "ApiVersion"])
        if version.value != 1:
            raise ValueError("Unity D-Bus API v1 is required")
        if self.requested_owner != owner:
            await self.manager(owner, "RequestAccess", "sas", ["Unity Context Engine", ["read"]])
            self.requested_owner = owner
        epoch = self.epoch
        (raw,) = await self.manager(owner, "GetAccessStatus")
        status = json.loads(raw)
        approved = status.get("approved") is True and "read" in status.get("scopes", [])
        (paths,) = await self.manager(owner, "ListDevices")
        values = await asyncio.gather(*(self.device(owner, path, approved) for path in paths[:64]))
        (raw,) = await self.manager(owner, "GetAccessStatus")
        current = json.loads(raw)
        if owner != self.owner or epoch != self.epoch:
            self.dirty.set()
            return
        approved = current.get("approved") is True and "read" in current.get("scopes", [])
        devices, choices = {}, {}
        for value in values:
            if value is None:
                continue
            device, options = value
            if not approved:
                device["state"] = {}
                options = {}
            devices[device["id"]] = device
            choices[device["id"]] = options
        self.publish(devices, choices, "approved" if approved else "pending",
                     "Device context supplied by Context Engine" if approved else "Approve Context Engine with unityctl authorize " + self.bus.unique_name,
                     requested=True)

    async def watch(self):
        self.bus.add_message_handler(self.changed)
        try:
            for rule in (
                f"type='signal',sender='{DBUS}',interface='{DBUS}',member='NameOwnerChanged',arg0='{BUS}'",
                f"type='signal',sender='{BUS}',interface='{MANAGER}',member='Changed'",
            ):
                await call(self.bus, DBUS, DBUS_PATH, DBUS, "AddMatch", "s", [rule])
            try:
                (owner,) = await call(self.bus, DBUS, DBUS_PATH, DBUS, "GetNameOwner", "s", [BUS])
            except OSError:
                owner = ""
            if self.owner is None:
                self.set_owner(owner)
            while True:
                self.dirty.clear()
                owner = self.owner
                retry = None
                if owner:
                    try:
                        await self.refresh(owner)
                    except (OSError, ValueError, TypeError, TimeoutError):
                        if owner == self.owner:
                            self.publish({}, {}, "error", "Unity device context is unavailable")
                            retry = 2
                if retry is None:
                    await self.dirty.wait()
                else:
                    try:
                        await asyncio.wait_for(self.dirty.wait(), timeout=retry)
                    except TimeoutError:
                        pass
        finally:
            self.bus.remove_message_handler(self.changed)


async def watch(store):
    bus = new_bus()
    try:
        await bus.connect()
        await Observer(bus, store).watch()
    finally:
        bus.disconnect()
        bus._finalize()
