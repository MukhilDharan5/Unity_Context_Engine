"""MPRIS discovery and property signals. Never calls player control methods."""

from __future__ import annotations

import asyncio
import contextlib

from .model import Player, Store

PATH = "/org/mpris/MediaPlayer2"
ROOT = "org.mpris.MediaPlayer2"
PLAYER = "org.mpris.MediaPlayer2.Player"
PROPERTIES = "org.freedesktop.DBus.Properties"
DBUS = "org.freedesktop.DBus"
RELEVANT = {"PlaybackStatus", "Fullscreen", "DesktopEntry", "Identity"}


def new_bus():
    from dbus_next.aio import MessageBus

    class OwnedBus(MessageBus):
        # dbus-next 0.2.x shuts down sockets without closing its socket/file
        # objects. Own their lifetime explicitly, including failed connects,
        # so repeated reconnects cannot leak descriptors. Dependency is <0.3.
        def _close_handles(self):
            for name in ("_stream", "_sock"):
                handle = getattr(self, name, None)
                if handle is not None:
                    with contextlib.suppress(OSError):
                        handle.close()

        def _setup_socket(self):
            try:
                super()._setup_socket()
            except BaseException:
                self._close_handles()
                raise

        def _finalize(self, err=None):
            try:
                super()._finalize(err)
            finally:
                self._close_handles()

    return OwnedBus()


async def session(store: Store) -> None:
    from dbus_next import Message, MessageFlag, MessageType

    bus = new_bus()
    dirty = asyncio.Event()
    names_changed = True
    pending: set[str] = set()
    owners: dict[str, str] = {}
    pids: dict[str, int] = {}

    async def call(destination: str, path: str, interface: str, member: str,
                   signature: str = "", body: list | None = None) -> list:
        async with asyncio.timeout(2):
            reply = await bus.call(Message(
                destination=destination, path=path, interface=interface,
                member=member, signature=signature, body=body or [], flags=MessageFlag.NO_AUTOSTART,
            ))
        if reply is None or reply.message_type == MessageType.ERROR:
            raise RuntimeError(reply.error_name if reply else "missing D-Bus reply")
        return reply.body

    async def daemon(member: str, signature: str = "", body: list | None = None) -> list:
        return await call(DBUS, "/org/freedesktop/DBus", DBUS, member, signature, body)

    def signal(message):
        nonlocal names_changed
        if message.message_type != MessageType.SIGNAL:
            return
        if message.sender == "org.freedesktop.DBus" and message.interface == DBUS and message.member == "NameOwnerChanged":
            if message.body and str(message.body[0]).startswith(ROOT + "."):
                names_changed = True
                dirty.set()
        elif message.path == PATH and message.interface == PROPERTIES and message.member == "PropertiesChanged":
            if len(message.body) != 3 or message.body[0] not in {ROOT, PLAYER}:
                return
            changed, invalidated = message.body[1:]
            if RELEVANT.intersection(changed) or RELEVANT.intersection(invalidated):
                pending.add(message.sender)
                dirty.set()

    async def get_property(owner: str, interface: str, key: str):
        try:
            body = await call(owner, PATH, PROPERTIES, "Get", "ss", [interface, key])
            return body[0].value
        except (RuntimeError, TimeoutError):
            return None

    async def read_player(name: str) -> Player | None:
        owner = owners[name]
        playback, identity, desktop, fullscreen = await asyncio.gather(
            get_property(owner, PLAYER, "PlaybackStatus"),
            get_property(owner, ROOT, "Identity"),
            get_property(owner, ROOT, "DesktopEntry"),
            get_property(owner, ROOT, "Fullscreen"),
        )
        if not isinstance(playback, str):
            return None
        return Player(
            name=name, identity=identity or name.removeprefix(ROOT + "."),
            desktop_entry=desktop or "", pid=pids.get(owner), playback=playback,
            fullscreen=fullscreen if isinstance(fullscreen, bool) else None,
        )

    disconnect: asyncio.Task | None = None
    try:
        async with asyncio.timeout(3):
            await bus.connect()
        bus.add_message_handler(signal)
        for rule in (
            "type='signal',sender='org.freedesktop.DBus',interface='org.freedesktop.DBus',member='NameOwnerChanged',arg0namespace='org.mpris.MediaPlayer2'",
            "type='signal',interface='org.freedesktop.DBus.Properties',member='PropertiesChanged',path='/org/mpris/MediaPlayer2'",
        ):
            await daemon("AddMatch", "s", [rule])
        disconnect = asyncio.create_task(bus.wait_for_disconnect())
        dirty.set()
        while True:
            waiter = asyncio.create_task(dirty.wait())
            try:
                done, _ = await asyncio.wait({waiter, disconnect}, return_when=asyncio.FIRST_COMPLETED)
                if disconnect in done:
                    await disconnect
                    raise RuntimeError("session bus disconnected")
                await asyncio.sleep(0.03)  # Coalesce bursts; no timer when idle.
            finally:
                waiter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await waiter
            dirty.clear()
            rediscover = names_changed
            names_changed = False
            senders = set(pending)
            pending.clear()
            if rediscover:
                names = (await daemon("ListNames"))[0]
                owners.clear()
                pids.clear()
                for name in names:
                    if not name.startswith(ROOT + "."):
                        continue
                    try:
                        owner = (await daemon("GetNameOwner", "s", [name]))[0]
                        owners[name] = owner
                        pids[owner] = (await daemon("GetConnectionUnixProcessID", "s", [owner]))[0]
                    except (RuntimeError, TimeoutError):
                        continue  # A player can exit during discovery.
            players = {name: p for name, p in store.players.items() if name in owners}
            for name, owner in list(owners.items()):
                if rediscover or owner in senders:
                    player = await read_player(name)
                    if player is None:
                        players.pop(name, None)
                    else:
                        players[name] = player
            store.set_players(players)
            store.source("media", "live", "MPRIS D-Bus property and player lifecycle signals")
    finally:
        bus.disconnect()
        # Finalize even when cancellation interrupted authentication before the
        # library installed its reader. All operations on this connection ended.
        bus._finalize()
        if disconnect is not None:
            disconnect.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await disconnect


async def watch(store: Store) -> None:
    try:
        import dbus_next  # noqa: F401
    except ImportError:
        store.source("media", "unavailable", "install dbus-next or run with --no-media")
        return
    delay = 1
    while True:
        try:
            await session(store)
        except Exception as error:
            store.set_players({})
            store.source("media", "unavailable", f"{type(error).__name__}: {error}; reconnecting in {delay}s")
        await asyncio.sleep(delay)
        delay = min(30, delay * 2)
