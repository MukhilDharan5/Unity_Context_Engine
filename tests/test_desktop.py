import os
from dataclasses import replace
import unittest

from unity_context.model import APP_ID, ActiveProcess, Compiler, Player, Store, Window
from unity_context.processes import Process, busy_processes
from unity_context.workload import predict


class DesktopOverviewTests(unittest.TestCase):
    def setUp(self):
        self.store = Store("test", inventory=True, own_pid=42)
        self.store.source("focus", "live", "test")
        self.store.source("media", "live", "test")
        self.store.source("processes", "live", "test")

    def test_engine_is_excluded_by_pid_and_application_id(self):
        editor = Window("org.gnome.TextEditor", "main.py", 10, id="1")
        own = Window("python3", "Context Engine", 42, id="2")
        other_instance = Window(APP_ID + ".desktop", "Context Engine", 99, id="3")
        self.store.set_desktop(own, (own, other_instance, editor), ranked=True)
        snapshot = self.store.snapshot()
        self.assertIsNone(snapshot["focus"])
        self.assertEqual(snapshot["last_window"]["id"], editor.id)
        self.assertEqual([w["id"] for w in snapshot["open_windows"]], [editor.id])
        self.assertEqual(predict(snapshot)[0].name, "Coding")

    def test_closed_last_window_stays_in_history_without_driving_prediction(self):
        editor = Window("code", "main.py", 10, id="1")
        own = Window(APP_ID, "Context Engine", 42, id="2")
        self.store.set_desktop(editor, (editor, own))
        self.store.set_desktop(own, (own,))
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["last_window"]["id"], editor.id)
        self.assertEqual(snapshot["open_windows"], [])
        self.assertEqual(predict(snapshot)[0].name, "No clear workload")

    def test_ranked_inventory_can_supply_recent_window_without_focus(self):
        window = Window("org.gnome.Papers", "Manual", id="1")
        self.store.set_desktop(None, (window,), ranked=True)
        snapshot = self.store.snapshot()
        self.assertIsNone(snapshot["focus"])
        self.assertEqual(snapshot["last_window"]["id"], window.id)
        self.assertEqual(predict(snapshot)[0].name, "Reading")

    def test_compilation_is_primary_for_editor_and_media_remains_concurrent(self):
        self.store.set_window(Window("code", "main.py"))
        self.store.set_compilers((Compiler(10, "rustc"),))
        player = Player("org.mpris.MediaPlayer2.spotify", "Spotify", "spotify", playback="Playing")
        self.store.set_players({player.name: player})
        self.assertEqual([item.name for item in predict(self.store.snapshot())], ["Compiling", "Coding", "Music"])

    def test_fullscreen_background_media_does_not_replace_a_meeting(self):
        self.store.set_window(Window("firefox", "Sprint sync – Google Meet — Mozilla Firefox"))
        player = Player("org.mpris.MediaPlayer2.mpv", "mpv", playback="Playing", fullscreen=True)
        self.store.set_players({player.name: player})
        predictions = predict(self.store.snapshot())
        self.assertEqual([item.name for item in predictions], ["Meetings", "Watching video"])
        self.assertIn("title hint", predictions[0].basis)

    def test_browser_title_rules_do_not_override_native_app_match(self):
        self.store.set_window(Window("org.inkscape.Inkscape", "YouTube banner.svg"))
        self.assertEqual(predict(self.store.snapshot())[0].name, "Graphic design")

    def test_window_source_loss_does_not_label_stale_focus_as_live(self):
        window = Window("org.gnome.Papers", "Manual", id="1")
        self.store.set_desktop(window, (window,))
        self.store.set_desktop(None, None)
        self.store.source("focus", "unavailable", "disconnected")
        self.assertIsNone(self.store.snapshot()["open_windows"])
        self.assertEqual(predict(self.store.snapshot())[0].name, "No clear workload")

    def test_idle_desktop_preserves_measured_jobs_and_playback(self):
        self.store.set_window(Window("code", "main.py"))
        self.store.set_idle(True)
        self.assertEqual(predict(self.store.snapshot())[0].name, "Desktop idle")
        self.store.set_load(None, (ActiveProcess(11, "ffmpeg", 120, 0, "Media processing"),))
        self.assertEqual([item.name for item in predict(self.store.snapshot())], ["Media processing"])

    def test_browser_topic_does_not_match_a_product_title(self):
        self.store.set_window(Window("firefox", "Linear algebra notes — Mozilla Firefox"))
        self.assertEqual(predict(self.store.snapshot())[0].name, "Browsing")

    def test_worker_cpu_uses_elapsed_ticks_and_ignores_recycled_pids(self):
        original = Process(10, "ffmpeg", "S", 0, 100)
        busy = replace(original, ticks=os.sysconf("SC_CLK_TCK"))
        self.assertEqual(busy_processes((busy,), (original,), 2), (
            ActiveProcess(10, "ffmpeg", 50, 0, "Media processing"),
        ))
        self.assertEqual(busy_processes((replace(busy, started=200),), (original,), 2), ())
