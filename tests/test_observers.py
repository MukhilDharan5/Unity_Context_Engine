import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unity_context.model import Store
from unity_context.unity_choices import device_choices
from unity_context.unity_devices import Observer, sensor_state


class MetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_application_changes_use_directory_events(self):
        from unity_context.desktop_metadata import watch_applications
        store = Store("test", inventory=True)
        async def eventually(predicate):
            async with asyncio.timeout(3):
                while not predicate():
                    await asyncio.sleep(.01)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            task = asyncio.create_task(watch_applications(store, (path,)))
            try:
                await eventually(lambda: store.sources.get("applications") is not None)
                self.assertEqual(store.extra_context["applications"], [])
                file = path / 'example.desktop'
                file.write_text('[Desktop Entry]\nType=Application\nName=Example\nExec=true\n')
                await eventually(lambda: bool(store.extra_context["applications"]))
                self.assertEqual(store.extra_context["applications"][0]["focus_id"], "example")
                file.unlink()
                await eventually(lambda: not store.extra_context["applications"])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def test_sensor_projection_and_pick_lists(self):
        raw = {"battery": {"level": 80}, "media": {"title": "Song", "players": [{"session": "one", "app_name": "Music"}]},
               "clipboard": {"content": "private"}, "sms": {"body": "private"}}
        self.assertEqual(sensor_state(raw), {"battery": {"level": 80}, "media": {"title": "Song"}})
        self.assertEqual(device_choices(raw)["media-session"], [("one", "Music")])


@unittest.skipUnless(os.environ.get("UNITY_CONTEXT_TEST_BUS") == "1", "requires a private D-Bus session")
class UnityObserverTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_authorization_signals_revocation_and_no_polling(self):
        from dbus_next.constants import PropertyAccess
        from dbus_next.service import ServiceInterface, method, signal, dbus_property
        from unity_context.media import new_bus
        from unity_context.unity_devices import BUS, MANAGER, MANAGER_PATH, DEVICE, PREFIX

        identifier = "a" * 32
        class Manager(ServiceInterface):
            def __init__(self):
                super().__init__(MANAGER)
                self.reads, self.approved, self.requests = 0, False, []

            @dbus_property(access=PropertyAccess.READ)
            def ApiVersion(self) -> 'u':
                return 1

            @method()
            def RequestAccess(self, label: 's', scopes: 'as') -> 's':
                self.requests.append((label, scopes))
                return "test-client"

            @method()
            def GetAccessStatus(self) -> 's':
                self.reads += 1
                return json.dumps({"approved": self.approved, "scopes": ["read"], "requested": True})

            @method()
            def ListDevices(self) -> 'ao':
                return [PREFIX + identifier]

            @signal()
            def Changed(self, path) -> 's':
                return path

        class Device(ServiceInterface):
            def __init__(self):
                super().__init__(DEVICE)
                self.level, self.reads = 50, 0

            @dbus_property(access=PropertyAccess.READ)
            def Id(self) -> 's':
                return identifier

            @dbus_property(access=PropertyAccess.READ)
            def Name(self) -> 's':
                return "Test phone"

            @dbus_property(access=PropertyAccess.READ)
            def Connected(self) -> 'b':
                return True

            @dbus_property(access=PropertyAccess.READ)
            def CapabilitiesJson(self) -> 's':
                return '[{"id":"battery","status":"available"}]'

            @method()
            def GetState(self) -> 's':
                self.reads += 1
                return json.dumps({"state": {"battery": {"level": self.level}, "clipboard": {"text": "private"}}})

        async def eventually(predicate):
            async with asyncio.timeout(3):
                while not predicate():
                    await asyncio.sleep(.01)

        server, client = new_bus(), new_bus()
        await server.connect()
        await client.connect()
        manager, device = Manager(), Device()
        server.export(MANAGER_PATH, manager)
        server.export(PREFIX + identifier, device)
        await server.request_name(BUS)
        store = Store("test", inventory=True)
        task = asyncio.create_task(Observer(client, store).watch())
        try:
            await eventually(lambda: store.extra_context["observer"].get("status") == "pending")
            self.assertEqual(manager.requests, [("Unity Context Engine", ["read"])])
            self.assertEqual(device.reads, 0)
            manager.approved = True
            manager.Changed(MANAGER_PATH)
            await eventually(lambda: store.extra_context["observer"].get("status") == "approved")
            self.assertEqual(store.extra_context["devices"][identifier]["state"], {"battery": {"level": 50}})
            reads = manager.reads
            await asyncio.sleep(.2)
            self.assertEqual(manager.reads, reads, "healthy connection waits for signals")
            device.level = 20
            manager.Changed(PREFIX + identifier)
            await eventually(lambda: store.extra_context["devices"][identifier]["state"]["battery"]["level"] == 20)
            manager.approved = False
            manager.Changed(MANAGER_PATH)
            await eventually(lambda: store.extra_context["observer"].get("status") == "pending")
            self.assertEqual(store.extra_context["devices"][identifier]["state"], {})
            await server.release_name(BUS)
            await eventually(lambda: not store.extra_context["devices"])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            for bus in (server, client):
                bus.disconnect()
                bus._finalize()
