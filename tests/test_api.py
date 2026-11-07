import asyncio
import json
import os
import unittest


@unittest.skipUnless(os.environ.get("UNITY_CONTEXT_TEST_BUS") == "1", "requires a private D-Bus session")
class ContextAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_headless_mode_registers_and_releases_the_api(self):
        from dbus_next import Message

        from unity_context.app import demo_store, observe, parser
        from unity_context.context_api import API_BUS_NAME, API_PATH
        from unity_context.media import new_bus

        args = parser().parse_args(["--serve", "--demo"])
        observer = asyncio.create_task(observe(args, demo_store(inventory=True)))
        bus = new_bus()
        await bus.connect()

        async def owned():
            reply = await bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                                           interface="org.freedesktop.DBus", member="NameHasOwner",
                                           signature="s", body=[API_BUS_NAME]))
            return reply.body[0]

        try:
            async with asyncio.timeout(2):
                while not await owned():
                    if observer.done():
                        observer.result()
                    await asyncio.sleep(0.01)
            reply = await bus.call(Message(destination=API_BUS_NAME, path=API_PATH,
                                           interface=API_BUS_NAME, member="GetSnapshot"))
            self.assertEqual(json.loads(reply.body[0])["data"]["sources"]["api"]["status"], "live")
        finally:
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)
            try:
                async with asyncio.timeout(2):
                    while await owned():
                        await asyncio.sleep(0.01)
            finally:
                bus.disconnect()
                bus._finalize()

    async def test_queries_reject_unknown_fields_and_report_live_changes(self):
        from dbus_next import DBusError

        from unity_context.app import demo_store
        from unity_context.context_api import API_BUS_NAME, API_PATH, export
        from unity_context.media import new_bus

        store = demo_store(inventory=True)
        async with export(store.snapshot()) as (_, service):
            bus = new_bus()
            await bus.connect()
            try:
                info = await bus.introspect(API_BUS_NAME, API_PATH)
                interface = bus.get_proxy_object(API_BUS_NAME, API_PATH, info).get_interface(API_BUS_NAME)
                self.assertEqual({method.name for method in interface.introspection.methods},
                                 {"GetSnapshot", "GetFields", "ListFields"})

                snapshot = json.loads(await interface.call_get_snapshot())
                self.assertEqual(snapshot["api_version"], 1)
                self.assertEqual(set(await interface.call_list_fields()), set(snapshot["data"]))
                self.assertEqual(snapshot["data"]["workloads"][0]["name"], "Compiling")
                self.assertEqual(snapshot["data"]["workload"], "Compiling")
                self.assertIn("Coding", snapshot["data"]["workload_names"])
                self.assertTrue(snapshot["data"]["media_playing"])
                self.assertTrue(snapshot["data"]["open_windows"])
                selected = json.loads(await interface.call_get_fields(["system_load", "idle"]))
                self.assertEqual(selected["revision"], snapshot["revision"])
                self.assertEqual(set(selected["data"]), {"system_load", "idle"})
                with self.assertRaises(DBusError) as rejected:
                    await interface.call_get_fields(["__dict__"])
                self.assertEqual(rejected.exception.type, "org.freedesktop.DBus.Error.InvalidArgs")

                changed = asyncio.Future()
                pushed = asyncio.Future()

                def on_change(revision):
                    if not changed.done():
                        changed.set_result(revision)

                interface.on_snapshot_changed(on_change)
                def on_snapshot(raw):
                    if not pushed.done():
                        pushed.set_result(json.loads(raw))

                interface.on_snapshot_updated(on_snapshot)
                service.publish(store.snapshot())
                unchanged = json.loads(await interface.call_get_snapshot())
                self.assertEqual(unchanged, snapshot)

                store.set_idle(True)
                service.publish(store.snapshot())
                revision = await asyncio.wait_for(changed, 2)
                current = json.loads(await interface.call_get_snapshot())
                self.assertEqual(await asyncio.wait_for(pushed, 2), current)
                self.assertGreater(revision, snapshot["revision"])
                self.assertEqual(current["revision"], revision)
                self.assertTrue(current["data"]["idle"])
            finally:
                bus.disconnect()
                bus._finalize()

    async def test_duplicate_provider_keeps_original_owner(self):
        from unity_context.app import demo_store
        from unity_context.context_api import export

        snapshot = demo_store(inventory=True).snapshot()
        async with export(snapshot):
            with self.assertRaisesRegex(OSError, "already running"):
                async with export(snapshot):
                    self.fail("duplicate provider acquired the context API name")
