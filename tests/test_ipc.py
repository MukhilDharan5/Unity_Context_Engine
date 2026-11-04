"""Protocol integration against local fixtures, never the desktop compositor.

Set UNITY_CONTEXT_TEST_IPC=1 to enable Unix socket fixtures.
Set UNITY_CONTEXT_TEST_BUS=1 under dbus-run-session for a private MPRIS fixture.
"""

import asyncio
import contextlib
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from unity_context import focus, media
from unity_context.model import Store


async def eventually(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


@unittest.skipUnless(os.environ.get("UNITY_CONTEXT_TEST_IPC") == "1", "enable local socket fixtures")
class SwayIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_focus_fullscreen_events_and_read_only_queries(self):
        store = Store("test")
        current = {"id": 9, "focused": True, "app_id": "mpv", "name": "Video",
                   "pid": 123, "fullscreen_mode": 0}
        subscribers = []
        seen = []
        clients = set()

        async def serve(reader, writer):
            clients.add(writer)
            try:
                header = await reader.readexactly(14)
                self.assertEqual(header[:6], b"i3-ipc")
                size, kind = struct.unpack("=II", header[6:])
                raw = await reader.readexactly(size)
                payload = json.loads(raw) if raw else None
                seen.append(kind)
                if kind == 2:
                    self.assertEqual(payload, ["window", "workspace", "shutdown"])
                    await focus.i3_send(writer, 2, '{"success": true}')
                    subscribers.append(writer)
                    await reader.read()
                elif kind == 4:
                    await focus.i3_send(writer, 4, json.dumps({"nodes": [dict(current)]}))
                else:
                    self.fail(f"unexpected IPC request: {kind}")
            finally:
                await focus.close_writer(writer)
                clients.discard(writer)

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "ipc.sock")
            server = await asyncio.start_unix_server(serve, path)
            task = asyncio.create_task(focus.sway(store, path))
            try:
                await eventually(lambda: store.window is not None)
                self.assertFalse(store.window.fullscreen)
                current["fullscreen_mode"] = 1
                await focus.i3_send(subscribers[0], (1 << 31) | 3,
                                    json.dumps({"change": "fullscreen_mode", "container": dict(current)}))
                await eventually(lambda: store.window.fullscreen)
                await focus.i3_send(subscribers[0], (1 << 31) | 6, '{"change": "exit"}')
                with self.assertRaises(focus.Unavailable):
                    await asyncio.wait_for(task, 1)
                self.assertTrue(set(seen) <= {2, 4})  # Never type 0 (COMMAND).
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                server.close()
                await server.wait_closed()
                for writer in list(clients):
                    await focus.close_writer(writer)


@unittest.skipUnless(os.environ.get("UNITY_CONTEXT_TEST_IPC") == "1", "enable local socket fixtures")
class HyprlandIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_bursts_coalesce_into_one_read_only_query(self):
        store = Store("test")
        current = {"address": "0x12", "class": "mpv", "title": "Video", "fullscreen": 0}
        subscribers, requests, clients = [], [], set()

        async def query(reader, writer):
            try:
                requests.append(await reader.read(1024))
                writer.write(json.dumps(current).encode())
                await writer.drain()
            finally:
                await focus.close_writer(writer)

        async def events(reader, writer):
            clients.add(writer)
            subscribers.append(writer)
            try:
                await reader.read()
            finally:
                await focus.close_writer(writer)
                clients.discard(writer)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            query_server = await asyncio.start_unix_server(query, str(path / ".socket.sock"))
            event_server = await asyncio.start_unix_server(events, str(path / ".socket2.sock"))
            with patch("unity_context.focus.hypr_path", return_value=path):
                task = asyncio.create_task(focus.hyprland(store))
                try:
                    await eventually(lambda: store.window is not None)
                    self.assertFalse(store.window.fullscreen)
                    current["fullscreen"] = 2
                    subscribers[0].write(b"fullscreen>>1\nactivewindow>>mpv,Video\nactivewindowv2>>0x12\n")
                    await subscribers[0].drain()
                    await eventually(lambda: store.window.fullscreen)
                    await asyncio.sleep(0.1)
                    self.assertEqual(requests, [b"j/activewindow", b"j/activewindow"])
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    for server in (query_server, event_server):
                        server.close()
                        await server.wait_closed()
                    for writer in list(clients):
                        await focus.close_writer(writer)


@unittest.skipUnless(os.environ.get("UNITY_CONTEXT_TEST_BUS") == "1", "requires an explicitly isolated test bus")
class MprisIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_state_signals_invalidations_exit_and_replacement(self):
        from dbus_next import Message
        from dbus_next.constants import PropertyAccess
        from dbus_next.service import ServiceInterface, dbus_property

        class Root(ServiceInterface):
            def __init__(self):
                super().__init__(media.ROOT)
                self.fullscreen = False

            @dbus_property(access=PropertyAccess.READ)
            def Identity(self) -> "s":
                return "Fixture Player"

            @dbus_property(access=PropertyAccess.READ)
            def DesktopEntry(self) -> "s":
                return "fixture"

            @dbus_property(access=PropertyAccess.READ)
            def Fullscreen(self) -> "b":
                return self.fullscreen

        class Playback(ServiceInterface):
            def __init__(self):
                super().__init__(media.PLAYER)
                self.status = "Paused"
                self.reads = 0

            @dbus_property(access=PropertyAccess.READ)
            def PlaybackStatus(self) -> "s":
                self.reads += 1
                return self.status

        name = "org.mpris.MediaPlayer2.unity_context_fixture"
        publisher = await media.new_bus().connect()
        root, playback = Root(), Playback()
        publisher.export(media.PATH, root)
        publisher.export(media.PATH, playback)
        await publisher.request_name(name)
        store = Store("test")
        task = asyncio.create_task(media.watch(store))
        replacement = None
        try:
            await eventually(lambda: name in store.players)
            self.assertEqual(store.players[name].playback, "Paused")
            reads = playback.reads
            await asyncio.sleep(0.15)
            self.assertEqual(playback.reads, reads, "idle player must not be polled")

            playback.status = "Playing"
            playback.emit_properties_changed({"PlaybackStatus": "Playing"})
            root.fullscreen = True
            root.emit_properties_changed({"Fullscreen": True})
            await eventually(lambda: store.snapshot()["fullscreen_media"] is True)

            playback.status = "Paused"
            await publisher.send(Message.new_signal(
                media.PATH, media.PROPERTIES, "PropertiesChanged", "sa{sv}as",
                [media.PLAYER, {}, ["PlaybackStatus"]],
            ))
            await eventually(lambda: store.players[name].playback == "Paused")

            publisher.disconnect()
            await publisher.wait_for_disconnect()
            await eventually(lambda: not store.players)
            replacement = await media.new_bus().connect()
            replacement.export(media.PATH, Root())
            replacement.export(media.PATH, Playback())
            await replacement.request_name(name)
            await eventually(lambda: name in store.players)
            self.assertFalse(store.players[name].fullscreen)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            for bus in (publisher, replacement):
                if bus is not None and bus.connected:
                    bus.disconnect()
                    with contextlib.suppress(Exception):
                        await bus.wait_for_disconnect()


if __name__ == "__main__":
    unittest.main()
