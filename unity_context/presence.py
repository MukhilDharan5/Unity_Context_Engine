"""Observe Mutter's idle threshold without listening to input events."""

from __future__ import annotations

import asyncio

from .media import DBUS, new_bus
from .model import Store
from .sandbox import IDLE_PATH, IDLE_SERVICE


IDLE_MINUTES = 3
IDLE_MS = IDLE_MINUTES * 60 * 1000


async def watch(store: Store) -> None:
    bus = None
    tasks = []
    try:
        from dbus_next import Message, MessageFlag, MessageType

        bus = new_bus()
        changed = asyncio.Event()

        def signal(message):
            if (message.message_type == MessageType.SIGNAL and message.interface == IDLE_SERVICE
                    and message.path == IDLE_PATH and message.member == "WatchFired"):
                changed.set()

        async def call(member, signature="", body=None, *, daemon=False):
            destination = DBUS if daemon else IDLE_SERVICE
            path = "/org/freedesktop/DBus" if daemon else IDLE_PATH
            async with asyncio.timeout(2):
                reply = await bus.call(Message(
                    destination=destination, path=path, interface=destination, member=member,
                    signature=signature, body=body or [], flags=MessageFlag.NO_AUTOSTART,
                ))
            if reply.message_type == MessageType.ERROR:
                raise OSError(reply.error_name)
            return reply.body[0] if reply.body else None

        async def observe():
            await call("AddIdleWatch", "t", [IDLE_MS])
            active_watch = None
            changed.set()
            while True:
                await changed.wait()
                changed.clear()
                idle = await call("GetIdletime") >= IDLE_MS
                if idle and active_watch is None:
                    active_watch = await call("AddUserActiveWatch")
                    # Input can resume in between the idle check and registration.
                    idle = await call("GetIdletime") >= IDLE_MS
                if not idle and active_watch is not None:
                    await call("RemoveWatch", "u", [active_watch])
                    active_watch = None
                store.set_idle(idle)
                store.source("presence", "live", f"GNOME idle threshold · {IDLE_MINUTES} minutes")

        async with asyncio.timeout(3):
            await bus.connect()
        bus.add_message_handler(signal)
        rule = f"type='signal',sender='{IDLE_SERVICE}',interface='{IDLE_SERVICE}',member='WatchFired',path='{IDLE_PATH}'"
        await call("AddMatch", "s", [rule], daemon=True)
        tasks = [asyncio.create_task(observe()), asyncio.create_task(bus.wait_for_disconnect())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
        raise OSError("idle monitor disconnected")
    except (OSError, EOFError, ImportError, ValueError, TimeoutError):
        store.set_idle(None)
        store.source("presence", "unavailable", "GNOME idle monitor unavailable")
    finally:
        if bus is not None:
            # The service removes watches when this bus connection closes.
            bus.disconnect()
            bus._finalize()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
