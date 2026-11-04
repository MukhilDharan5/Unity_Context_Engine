"""Event-driven curses presentation and a small JSON interface."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import curses
import json
import math
import os
import signal
import sys
import textwrap

from . import __version__, focus, media, processes
from .model import Compiler, Player, Store, Window


def safe_text(value: object) -> str:
    # Window/app names are untrusted. Never render controls or bidi formatting.
    return "".join(c if c.isprintable() else " " for c in str(value))


def verdict(value: bool | None) -> str:
    return "unknown" if value is None else "yes" if value else "no"


def screen_lines(snapshot: dict, width: int) -> list[tuple[str, bool]]:
    lines: list[tuple[str, bool]] = []

    def line(text: str = "", heading: bool = False) -> None:
        for part in textwrap.wrap(safe_text(text), max(10, width),
                                  replace_whitespace=True, subsequent_indent="  ") or [""]:
            lines.append((part, heading))

    line("UNITY CONTEXT   /   READ ONLY", True)
    line(snapshot["session"])
    line()
    line(" + ".join(snapshot["activity"]), True)
    line()
    line("FOCUS", True)
    window = snapshot["focus"]
    if window is None:
        line("  No focused window" if snapshot["sources"]["focus"]["status"] == "live"
             else "  Unavailable")
    else:
        line(f"  App         {window['app'] or 'unknown'}")
        line(f"  Window      {window['title'] or '(untitled)'}")
        line(f"  Fullscreen  {verdict(window['fullscreen'])}")
    line()
    line("MEDIA", True)
    if not snapshot["media"]:
        line("  No MPRIS players" if snapshot["sources"]["media"]["status"] == "live"
             else "  Unavailable")
    for player in snapshot["media"]:
        line(f"  {player['identity'] or player['name']}  /  {player['playback']}")
    line(f"  Fullscreen media  {verdict(snapshot['fullscreen_media'])}")
    line()
    line("ACTIVITY", True)
    game = snapshot["game"]
    game_label = ("detected" if game["detected"] else "no matching app ID"
                  if game["detected"] is False else "unknown")
    line(f"  Game        {game_label}")
    if game["evidence"]:
        line(f"              {game['evidence']}")
    compiler_names = ", ".join(f"{c['name']} ({c['pid']})" for c in snapshot["compilers"])
    line(f"  Compiling   {compiler_names or verdict(snapshot['compiling'])}")
    line()
    line("SOURCES", True)
    for name, source in snapshot["sources"].items():
        line(f"  {name}: {source['status']} / {source['detail']}")
    return lines


async def tui(screen, store: Store) -> None:
    with contextlib.suppress(curses.error):
        curses.curs_set(0)
    screen.nodelay(True)
    screen.keypad(True)
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    offset = 0

    def keypress() -> None:
        nonlocal offset
        while (key := screen.getch()) != -1:
            if key in (ord("q"), ord("Q"), 27):
                stopped.set()
            elif key in (ord("j"), curses.KEY_DOWN):
                offset += 1
            elif key in (ord("k"), curses.KEY_UP):
                offset = max(0, offset - 1)
            elif key in (curses.KEY_NPAGE, ord(" ")):
                offset += max(1, screen.getmaxyx()[0] - 2)
            elif key == curses.KEY_PPAGE:
                offset = max(0, offset - screen.getmaxyx()[0] + 2)
            elif key in (ord("g"), curses.KEY_HOME):
                offset = 0
            store.changed.set()

    def resized() -> None:
        with contextlib.suppress(curses.error, OSError):
            size = os.get_terminal_size(sys.stdin.fileno())
            curses.resizeterm(size.lines, size.columns)
        store.changed.set()

    loop.add_reader(sys.stdin.fileno(), keypress)
    loop.add_signal_handler(signal.SIGWINCH, resized)
    stop_task = asyncio.create_task(stopped.wait())
    try:
        while not stopped.is_set():
            store.changed.clear()
            height, width = screen.getmaxyx()
            lines = screen_lines(store.snapshot(), max(10, width - 2))
            visible = max(0, height - 2)
            offset = min(offset, max(0, len(lines) - visible))
            screen.erase()
            for row, (text, heading) in enumerate(lines[offset:offset + visible]):
                with contextlib.suppress(curses.error):
                    screen.addnstr(row, 1, text, max(0, width - 2),
                                   curses.A_BOLD if heading else curses.A_NORMAL)
            footer = f"q quit   j/k scroll   {offset + 1}-{min(offset + visible, len(lines))}/{len(lines)}"
            with contextlib.suppress(curses.error):
                screen.addnstr(max(0, height - 1), 1, footer, max(0, width - 2), curses.A_DIM)
            screen.refresh()
            change_task = asyncio.create_task(store.changed.wait())
            try:
                await asyncio.wait({stop_task, change_task}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                change_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await change_task
    finally:
        stop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stop_task
        loop.remove_reader(sys.stdin.fileno())
        loop.remove_signal_handler(signal.SIGWINCH)


def demo_store() -> Store:
    store = Store("DEMO / sample data")
    store.set_window(Window("mpv", "A quiet evening", 1234, True, "demo"))
    player = Player("org.mpris.MediaPlayer2.mpv", "mpv", "mpv", 1234, "Playing", True)
    store.set_players({player.name: player})
    store.set_compilers((Compiler(5678, "rustc"),))
    for key in store.sources:
        store.source(key, "live", "demo data; no device collectors running")
    return store


async def ready(store: Store) -> None:
    try:
        async with asyncio.timeout(4):
            while any(s.status == "starting" for s in store.sources.values()):
                store.changed.clear()
                await store.changed.wait()
    except TimeoutError:
        for key, source in list(store.sources.items()):
            if source.status == "starting":
                store.source(key, "unavailable", "initial discovery timed out; collector still running")


async def run(args: argparse.Namespace, screen=None) -> None:
    session_type = os.environ.get("XDG_SESSION_TYPE", "unknown session")
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "Linux")
    store = demo_store() if args.demo else Store(f"{desktop} / {session_type}", tuple(args.game_app))
    tasks = []
    if not args.demo:
        tasks.append(asyncio.create_task(focus.watch(store, args.focus)))
        if args.no_media:
            store.source("media", "disabled", "disabled by --no-media")
        else:
            tasks.append(asyncio.create_task(media.watch(store)))
        if args.no_processes:
            store.source("processes", "disabled", "disabled by --no-processes")
        else:
            tasks.append(asyncio.create_task(processes.watch(store, args.process_interval)))
    try:
        if args.once or args.json:
            await ready(store)
            while True:
                store.changed.clear()
                print(json.dumps(store.snapshot(), ensure_ascii=True), flush=True)
                if args.once:
                    return
                await store.changed.wait()
        else:
            await tui(screen, store)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def interval(value: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("use a number of seconds (at least 1)") from error
    if not math.isfinite(number) or number < 1:
        raise argparse.ArgumentTypeError("process interval must be finite and at least 1 second")
    return number


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Minimal, read-only Linux device context. q quits; j/k scroll.")
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("--focus", choices=("auto", "sway", "hyprland", "x11", "none"), default="auto")
    result.add_argument("--process-interval", type=interval, default=3.0, metavar="SECONDS",
                        help="current-user /proc scan interval, at least 1s (default: 3)")
    result.add_argument("--no-processes", action="store_true", help="disable /proc scans; use only event sources")
    result.add_argument("--no-media", action="store_true", help="disable MPRIS D-Bus")
    result.add_argument("--game-app", action="append", default=[], metavar="APP_ID",
                        help="treat an exact focused app ID as a game (repeatable)")
    result.add_argument("--demo", action="store_true", help="show sample context without accessing the device")
    output = result.add_mutually_exclusive_group()
    output.add_argument("--once", action="store_true", help="print one JSON snapshot and exit")
    output.add_argument("--json", action="store_true", help="stream JSON snapshots on state changes")
    return result


def main() -> None:
    args = parser().parse_args()
    if sys.platform != "linux":
        parser().error("Linux is required")
    if not (args.once or args.json) and not (sys.stdin.isatty() and sys.stdout.isatty()):
        parser().error("the TUI needs a terminal; use --once or --json for non-interactive output")
    try:
        if args.once or args.json:
            asyncio.run(run(args))
        else:
            curses.wrapper(lambda screen: asyncio.run(run(args, screen)))
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        # Avoid a second BrokenPipeError when Python flushes stdout on exit.
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
    except curses.error as error:
        print(f"Cannot initialize terminal: {error}. Try --once.", file=sys.stderr)
        raise SystemExit(1) from error
