"""Subscribe to the optional, read-only GNOME Shell window bridge."""

import asyncio
import contextlib
import json

from .media import DBUS, new_bus
from .model import APP_ID, Store, Window

SERVICE = APP_ID + ".Windows"
PATH = "/" + SERVICE.replace(".", "/")


async def watch(store: Store) -> None:
    from dbus_next import Message, MessageFlag, MessageType

    bus = new_bus()
    changed = asyncio.Event()
    tasks = []

    def receive(message):
        if message.message_type != MessageType.SIGNAL:
            return
        if (message.interface == SERVICE and message.path == PATH and message.member == "Changed"
                or message.sender == DBUS and message.interface == DBUS
                and message.member == "NameOwnerChanged" and message.body and message.body[0] == SERVICE):
            changed.set()

    async def call(destination, path, interface, member, signature="", body=None):
        async with asyncio.timeout(2):
            return await bus.call(Message(
                destination=destination, path=path, interface=interface, member=member,
                signature=signature, body=body or [], flags=MessageFlag.NO_AUTOSTART,
            ))

    async def observe():
        changed.set()
        while True:
            await changed.wait()
            await asyncio.sleep(0.04)  # Coalesce focus and title changes from the same action.
            changed.clear()
            reply = await call(SERVICE, PATH, SERVICE, "GetSnapshot")
            if reply.message_type == MessageType.ERROR:
                store.set_desktop(None, None)
                if reply.error_name in {f"{DBUS}.Error.ServiceUnknown", f"{DBUS}.Error.NameHasNoOwner"}:
                    detail = "Enable the Context Engine Windows extension for GNOME Wayland window access"
                else:
                    detail = f"GNOME Shell window bridge: {reply.error_name}"
                store.source("focus", "unavailable", detail)
                continue

            snapshot = json.loads(reply.body[0])
            windows = tuple(Window(**item) for item in snapshot["windows"])
            focused = next((w for w in windows if w.id == snapshot["focused"]), None)
            store.set_desktop(focused, windows if store.inventory else None, ranked=True)
            store.source("focus", "live", "GNOME Shell window and focus events")

    try:
        async with asyncio.timeout(3):
            await bus.connect()
        bus.add_message_handler(receive)
        for rule in (
            f"type='signal',sender='{SERVICE}',interface='{SERVICE}',member='Changed',path='{PATH}'",
            f"type='signal',sender='{DBUS}',interface='{DBUS}',member='NameOwnerChanged',arg0='{SERVICE}'",
        ):
            reply = await call(DBUS, "/org/freedesktop/DBus", DBUS, "AddMatch", "s", [rule])
            if reply.message_type == MessageType.ERROR:
                raise OSError(reply.error_name)
        tasks = [asyncio.create_task(observe()), asyncio.create_task(bus.wait_for_disconnect())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
        raise OSError("GNOME window bridge connection closed")
    finally:
        bus.disconnect()
        bus._finalize()
        for task in tasks:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks, return_exceptions=True)
