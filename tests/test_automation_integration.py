"""Cross-check the real Automation Engine contract on a private session bus."""

import asyncio
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


AUTOMATION_SOURCE = os.environ.get("UNITY_AUTOMATION_SOURCE")


@unittest.skipUnless(AUTOMATION_SOURCE and os.environ.get("UNITY_CONTEXT_TEST_BUS") == "1",
                     "requires Automation Engine sources and a private D-Bus session")
class AutomationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_negotiated_push_drives_triggers_routes_waits_and_lease_without_reads(self):
        sys.path.insert(0, str(Path(AUTOMATION_SOURCE).resolve()))
        from dbus_next.service import method
        from unity_automation import engine_name
        from unity_automation.catalog import TRIGGERS, WORKLOADS
        from unity_automation.context import ContextIngress, snapshot
        from unity_automation.engine import Engine, Store
        from unity_context.app import demo_store
        from unity_context.automation import Publisher, call
        from unity_context.context_api import ContextAPI, export
        from unity_context.media import new_bus
        from unity_context.model import Compiler, Player, Window
        from unity_context.processes import PROCESS_WORKLOADS
        from unity_context.unity_devices import SENSORS
        from unity_context.workload import ICONS

        class CountingAPI(ContextAPI):
            reads = 0

            @method()
            def GetSnapshot(self) -> 's':
                self.reads += 1
                return self._snapshot_json()

        observed, calls = [], []

        async def execute(action):
            observed.append(action.payload["title"])

        async def counted_call(bus, owner, path, interface, member, *args):
            calls.append((member, args))
            return await call(bus, owner, path, interface, member, *args)

        async def eventually(predicate):
            async with asyncio.timeout(4):
                while not predicate():
                    await asyncio.sleep(.01)

        engine = Engine(Store(None), execute)
        engine.save({"id": "", "name": "Compilation", "kind": "routine", "enabled": True,
                     "script": 'when context.compiling == true\ndo notify {"title":"compile"}'})
        route = engine.save({"id": "", "name": "Audio route", "kind": "mode", "enabled": True,
                             "script": 'when context.media_playing == true\nenter\n'
                                       '  do notify {"title":"route-on"}\nleave\n'
                                       '  do notify {"title":"route-off"}'})
        manual = engine.save({"id": "", "name": "Wait", "kind": "routine", "enabled": False,
                              "script": 'when manual\nwait until context.compiling == true within 3s\n'
                                        'do notify {"title":"wait-satisfied"}'})
        context = demo_store(inventory=True)
        context.source("api", "live", "test")
        context.set_compilers(())
        context.set_load(context.system_load, ())
        context.set_players({})
        context.set_window(Window("code", "source.py", id="1"))
        client = new_bus()
        await client.connect()
        await client.request_name(engine_name())
        ingress = ContextIngress(client, engine)
        engine.changed = ingress.requirements_changed
        await ingress.start()
        worker = asyncio.create_task(engine.watch())
        lease = asyncio.create_task(ingress.watch())
        publisher = None
        try:
            with patch("unity_context.context_api.ContextAPI", CountingAPI), \
                    patch("unity_context.automation.call", counted_call), \
                    patch("unity_automation.context.LEASE_SECONDS", 2):
                async with export(context.snapshot()) as (bus, service):
                    _, contract = snapshot(service._snapshot_json())
                    # Every desktop trigger path is present. Device trigger paths
                    # are supplied by the read-only Unity sensor projection.
                    for trigger in TRIGGERS:
                        if trigger.path.startswith("context."):
                            value = contract
                            for key in trigger.path.split(".")[1:]:
                                self.assertIsInstance(value, dict, trigger.path)
                                self.assertIn(key, value, trigger.path)
                                value = value[key]
                        elif trigger.path.startswith("devices.state."):
                            _, _, sensor, field = trigger.path.split(".")
                            self.assertIn(field, SENSORS[sensor], trigger.path)
                    self.assertFalse(set(ICONS).difference(WORKLOADS))
                    self.assertFalse(set(PROCESS_WORKLOADS.values()).difference(WORKLOADS))
                    context.listeners.add(service.publish)
                    publisher = asyncio.create_task(Publisher(bus, service).watch())
                    await eventually(lambda: bool(engine.context))
                    self.assertNotIn("focus", engine.context, "send only requested fields")
                    engine.run(manual)
                    await eventually(lambda: bool(engine.waiters))
                    # No scheduler turn between true/false: queue both updates.
                    context.set_compilers((Compiler(12, "rustc"),))
                    context.set_compilers(())
                    await eventually(lambda: {"compile", "wait-satisfied"} <= set(observed))
                    self.assertFalse(engine.context["compiling"])
                    player = Player("org.mpris.MediaPlayer2.test", "Test", playback="Playing")
                    context.set_players({player.name: player})
                    await eventually(lambda: "route-on" in observed)
                    self.assertTrue(engine.modes[route])
                    context.set_players({})
                    await eventually(lambda: "route-off" in observed)
                    self.assertFalse(engine.modes[route])
                    engine.save({"id": "", "name": "Focus", "kind": "routine", "enabled": True,
                                 "script": 'when context.focus.app == "code"\ndo notify {"title":"focused"}'})
                    await eventually(lambda: "focused" in observed)
                    self.assertEqual(engine.context["focus"]["app"], "code")
                    requirements_count = sum(member == "GetRequirements" for member, _ in calls)
                    revision = ingress.revision
                    published_count = sum(member == "PublishSnapshot" for member, _ in calls)
                    await asyncio.sleep(1.15)
                    self.assertGreater(sum(member == "PublishSnapshot" for member, _ in calls), published_count)
                    self.assertEqual(ingress.revision, revision, "lease renewal reuses the cached packet")
                    self.assertEqual(sum(member == "GetRequirements" for member, _ in calls), requirements_count)
                    self.assertEqual(service.reads, 0, "Automation must never read Context snapshots")
                    publisher.cancel()
                    await asyncio.gather(publisher, return_exceptions=True)
                    context.listeners.discard(service.publish)
                await eventually(lambda: not engine.context)
                # A restarted provider is authenticated and may start revisions at 1.
                async with export(context.snapshot()) as (bus, service):
                    publisher = asyncio.create_task(Publisher(bus, service).watch())
                    await eventually(lambda: bool(engine.context))
                    self.assertEqual(service.reads, 0)
                    self.assertEqual(ingress.revision, 1)
        finally:
            for task in (publisher, worker, lease):
                if task is not None:
                    task.cancel()
            await asyncio.gather(*(task for task in (publisher, worker, lease) if task), return_exceptions=True)
            await engine.close()
            client.disconnect()
            client._finalize()
