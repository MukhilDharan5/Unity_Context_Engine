import argparse
import asyncio
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unity_context.app import demo_store, interval, safe_text, screen_lines
from unity_context.focus import choose_backend, focused_node, hypr_window, xprop_window
from unity_context.model import Player, Store, Window
from unity_context.processes import COMPILER, Process, active_compilers, parse_stat, scan


class InferenceTests(unittest.TestCase):
    def setUp(self):
        self.store = Store("test")
        self.store.source("media", "live", "test")
        self.store.source("focus", "live", "test")
        self.store.source("processes", "live", "test")

    def media(self, **changes):
        player = replace(Player("org.mpris.MediaPlayer2.mpv", "mpv", "mpv", 23, "Playing"), **changes)
        self.store.set_players({player.name: player})

    def test_unrelated_background_audio_does_not_imply_fullscreen_media(self):
        self.store.set_window(Window("terminal", "mpv is playing", 42, True))
        self.media()
        result = self.store.snapshot()
        self.assertIsNone(result["fullscreen_media"])
        self.assertEqual(result["activity"], ["Media playing"])

    def test_exact_desktop_entry_or_pid_matches_fullscreen_player(self):
        for window in (Window("mpv.desktop", pid=99, fullscreen=True),
                       Window("other-class", pid=23, fullscreen=True)):
            with self.subTest(window=window):
                self.store.set_window(window)
                self.media()
                self.assertTrue(self.store.snapshot()["fullscreen_media"])

    def test_paused_fullscreen_player_is_not_playing_media(self):
        self.media(playback="Paused", fullscreen=True)
        self.assertFalse(self.store.snapshot()["fullscreen_media"])

    def test_self_reported_fullscreen_works_without_focus(self):
        self.media(fullscreen=True)
        self.assertTrue(self.store.snapshot()["fullscreen_media"])

    def test_missing_fullscreen_property_stays_unknown(self):
        self.media()
        self.assertIsNone(self.store.snapshot()["fullscreen_media"])

    def test_player_that_reports_not_fullscreen_is_negative(self):
        self.media(fullscreen=False)
        self.assertFalse(self.store.snapshot()["fullscreen_media"])

    def test_unavailable_media_or_processes_are_not_negative(self):
        self.store.source("media", "unavailable", "bus disconnected")
        self.store.source("processes", "disabled", "disabled")
        self.assertIsNone(self.store.snapshot()["fullscreen_media"])
        self.assertIsNone(self.store.snapshot()["compiling"])

    def test_game_detection_does_not_treat_steam_launcher_or_fullscreen_as_game(self):
        for app in ("steam", "steamwebhelper", "wine", "firefox", "not_steam_app_123"):
            self.store.set_window(Window(app, fullscreen=True))
            self.assertFalse(self.store.snapshot()["game"]["detected"], app)
        self.store.set_window(Window("steam_app_123"))
        self.assertTrue(self.store.snapshot()["game"]["detected"])

    def test_user_game_ids_are_exact_case_insensitive_matches(self):
        store = Store("test", ("org.example.Game.desktop",))
        store.set_window(Window("org.example.game"))
        self.assertTrue(store.snapshot()["game"]["detected"])
        store.set_window(Window("org.example.GameLauncher"))
        self.assertFalse(store.snapshot()["game"]["detected"])

    def test_repeated_state_does_not_wake_idle_view(self):
        self.store.set_window(Window("mpv"))
        self.store.changed.clear()
        self.store.set_window(Window("mpv"))
        self.store.source("focus", "live", "test")
        self.assertFalse(self.store.changed.is_set())


def stat_line(pid=12, name="rustc", state="R", ticks=15, started=100):
    fields = [state] + ["0"] * 19
    fields[11], fields[12], fields[19] = str(ticks), "0", str(started)
    return f"{pid} ({name}) " + " ".join(fields)


class ProcessTests(unittest.TestCase):
    def test_stat_parser_handles_spaces_and_parentheses(self):
        self.assertEqual(parse_stat(stat_line(name="compiler ) name")),
                         Process(12, "compiler ) name", "R", 15, 100))

    def test_compile_detection_requires_runnable_or_cpu_progress(self):
        old = Process(12, "rustc", "S", 10, 100)
        self.assertFalse(active_compilers((old,), (old,)))
        self.assertTrue(active_compilers((replace(old, ticks=11),), (old,)))
        self.assertTrue(active_compilers((replace(old, state="R"),), ()))
        self.assertFalse(active_compilers((replace(old, ticks=20, state="T"),), (old,)))

    def test_pid_reuse_does_not_create_cpu_progress(self):
        old = Process(12, "rustc", "S", 10, 100)
        new = replace(old, ticks=50, started=200)
        self.assertFalse(active_compilers((new,), (old,)))

    def test_compiler_names_exclude_daemons_and_launchers(self):
        for name in ("rustc", "cc1plus", "clang++-20", "x86_64-linux-gnu-gcc", "javac"):
            self.assertIsNotNone(COMPILER.fullmatch(name), name)
        for name in ("cargo", "ninja", "make", "cmake", "clangd", "rust-analyzer", "java", "dotnet"):
            self.assertIsNone(COMPILER.fullmatch(name), name)

    def test_scan_skips_foreign_uid_bad_stat_and_exit_races(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for pid in (12, 13, 14):
                (root / str(pid)).mkdir()
            (root / "12" / "stat").write_text(stat_line())
            (root / "13" / "stat").write_text("broken")
            self.assertEqual(len(scan(root)), 1)
            self.assertEqual(scan(root, os.getuid() + 1), ())


class FocusTests(unittest.TestCase):
    def test_auto_does_not_treat_xwayland_as_global_focus(self):
        with patch.dict(os.environ, {"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}, clear=True):
            self.assertEqual(choose_backend("auto"), "unsupported-wayland")

    def test_native_compositor_takes_priority(self):
        with patch.dict(os.environ, {"XDG_SESSION_TYPE": "wayland", "SWAYSOCK": "/test"}, clear=True):
            self.assertEqual(choose_backend("auto"), "sway")

    def test_sway_floating_window_and_x11_properties(self):
        tree = {"nodes": [], "floating_nodes": [{"nodes": [
            {"id": 9, "focused": True, "window": 5, "name": "game", "pid": 12,
             "window_properties": {"class": "steam_app_123"}, "fullscreen_mode": 1}
        ]}]}
        self.assertEqual(focused_node(tree), Window("steam_app_123", "game", 12, True, "9"))

    def test_hyprland_maximized_is_not_fullscreen(self):
        self.assertFalse(hypr_window({"address": "0x12", "fullscreen": 1}).fullscreen)
        self.assertTrue(hypr_window({"address": "0x12", "fullscreen": 2}).fullscreen)
        self.assertTrue(hypr_window({"address": "0x12", "fullscreen": 3}).fullscreen)
        self.assertIsNone(hypr_window({}))

    def test_xprop_parses_class_title_and_state_without_executing_title(self):
        props = {"WM_CLASS": '"instance", "Mpv"', "_NET_WM_NAME": '"title \\"quoted\\""',
                 "_NET_WM_STATE": "_NET_WM_STATE_FULLSCREEN, _NET_WM_STATE_FOCUSED",
                 "_NET_WM_PID": "12"}
        self.assertEqual(xprop_window(props, "0x12"), Window("Mpv", 'title "quoted"', 12, True, "0x12"))


class PresentationTests(unittest.TestCase):
    def test_untrusted_titles_have_no_terminal_controls(self):
        clean = safe_text("title\x1b[31m\n\u202eexample")
        self.assertNotIn("\x1b", clean)
        self.assertNotIn("\n", clean)
        self.assertNotIn("\u202e", clean)

    def test_narrow_and_wide_layouts_include_all_sections(self):
        for width in (10, 38, 100):
            text = "\n".join(t for t, _ in screen_lines(demo_store().snapshot(), width))
            for heading in ("FOCUS", "MEDIA", "ACTIVITY", "SOURCES"):
                self.assertIn(heading, text)

    def test_interval_rejects_nonfinite_and_busy_polling(self):
        for value in ("nan", "inf", "-1", "0.1", "wrong"):
            with self.assertRaises(argparse.ArgumentTypeError):
                interval(value)


if __name__ == "__main__":
    unittest.main()
