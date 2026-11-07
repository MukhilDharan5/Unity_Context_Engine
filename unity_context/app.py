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

from . import __version__, focus, media, presence, processes, sandbox
from .model import APP_ID, APP_NAME, ActiveProcess, Compiler, Player, Store, SystemLoad, Window


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

    line("CONTEXT ENGINE   /   READ ONLY", True)
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


def demo_store(*, inventory: bool = False) -> Store:
    if inventory:
        own_pid = sandbox.host_pid()
        store = Store("Demo desktop / Wayland", inventory=True, own_pid=own_pid)
        own = Window(APP_ID, APP_NAME, own_pid, False, "0")
        windows = (
            own,
            Window("code", "workload.py — Unity_Context_Engine", 1201, False, "1", "Visual Studio Code"),
            Window("firefox", "GTK documentation — Firefox", 1202, False, "2", "Firefox"),
            Window("spotify", "Spotify", 1203, False, "3", "Spotify"),
            Window("org.gnome.Nautilus", "Projects", 1204, False, "4", "Files"),
            Window("org.gnome.Ptyxis", "unity-context", 1205, False, "5", "Terminal"),
            Window("org.inkscape.Inkscape", "Artwork", 1206, False, "6", "Inkscape"),
        )
        store.set_desktop(own, windows, ranked=True)
        player = Player("org.mpris.MediaPlayer2.spotify", "Spotify", "spotify", 1203, "Playing", False)
        store.set_compilers((Compiler(1207, "rustc"),))
        store.set_idle(False)
        store.set_load(SystemLoad(42, 24 * 1024**3, 14 * 1024**3, 1.21, 0, 0.25), (
            ActiveProcess(1207, "rustc", 152, 640 * 1024**2, "Compiling"),
            ActiveProcess(1208, "ffmpeg", 87, 230 * 1024**2, "Media processing"),
            ActiveProcess(1202, "firefox", 14, 580 * 1024**2),
        ))
    else:
        store = Store("DEMO / sample data")
        store.set_window(Window("mpv", "A quiet evening", 1234, True, "demo"))
        player = Player("org.mpris.MediaPlayer2.mpv", "mpv", "mpv", 1234, "Playing", True)
        store.set_compilers((Compiler(5678, "rustc"),))
    store.set_players({player.name: player})
    if inventory:
        from .automation import clock_snapshot
        store.update_extra(clock=clock_snapshot(), applications=[
            {"id": "org.gnome.Settings.desktop", "name": "Settings", "focus_id": "org.gnome.Settings"}])
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


def session_store(args: argparse.Namespace, *, inventory: bool = False) -> Store:
    session_type = os.environ.get("XDG_SESSION_TYPE", "unknown session")
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "Linux")
    return Store(f"{desktop} / {session_type}", tuple(args.game_app),
                 inventory=inventory, own_pid=sandbox.host_pid() if inventory else None)


def start_collectors(args: argparse.Namespace, store: Store) -> list[asyncio.Task]:
    tasks = [asyncio.create_task(focus.watch(store, args.focus))]
    if store.inventory:
        if "gnome" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower().split(":"):
            tasks.append(asyncio.create_task(presence.watch(store)))
        else:
            store.source("presence", "unavailable", "idle events are supported on GNOME")
    if args.no_media:
        store.source("media", "disabled", "disabled by --no-media")
    else:
        tasks.append(asyncio.create_task(media.watch(store)))
    if args.no_processes:
        store.source("processes", "disabled", "disabled by --no-processes")
    else:
        tasks.append(asyncio.create_task(processes.watch(store, args.process_interval)))
    if store.inventory and api_requested(args):
        from . import automation, desktop_metadata, unity_devices
        tasks.extend((asyncio.create_task(automation.watch_clock(store)),
                      asyncio.create_task(desktop_metadata.watch_applications(store)),
                      asyncio.create_task(unity_devices.watch(store))))
    return tasks


def api_requested(args: argparse.Namespace) -> bool:
    return args.serve or not (args.no_api or args.demo or args.tui or args.once or args.json)


async def observe(args: argparse.Namespace, store: Store, *, on_snapshot=None,
                  stop: asyncio.Event | None = None) -> None:
    """Share the same collectors between the window and the local API."""
    tasks = [] if args.demo else start_collectors(args, store)
    async with contextlib.AsyncExitStack() as stack:
        session = None
        try:
            if api_requested(args):
                try:
                    from .context_api import export
                    store.source("api", "live", "Read-only D-Bus session API")
                    session = await stack.enter_async_context(export(store.snapshot()))
                    store.listeners.add(session[1].publish)
                    from .automation import publish_automation
                    tasks.append(asyncio.create_task(publish_automation(*session)))
                except (ImportError, OSError) as error:
                    if args.serve:
                        raise OSError(str(error)) from error
                    store.source("api", "unavailable", str(error))
            elif args.no_api:
                store.source("api", "disabled", "Disabled by --no-api")

            async def updates():
                while True:
                    await store.changed.wait()
                    store.changed.clear()
                    snapshot = store.snapshot()
                    if session is not None:
                        session[1].publish(snapshot)
                    if on_snapshot is not None:
                        on_snapshot(snapshot)

            async def watch_api():
                nonlocal session
                try:
                    await session[0].wait_for_disconnect()
                except Exception:
                    pass  # Either kind of disconnect ends the API session.
                store.listeners.discard(session[1].publish)
                session = None
                detail = "Context API disconnected from the session bus"
                if args.serve:
                    raise OSError(detail)
                store.source("api", "unavailable", detail)

            tasks.append(asyncio.create_task(updates()))
            if args.serve:
                await watch_api()
            else:
                if session is not None:
                    tasks.append(asyncio.create_task(watch_api()))
                await stop.wait()
        finally:
            if session is not None:
                store.listeners.discard(session[1].publish)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def run(args: argparse.Namespace, screen=None) -> None:
    store = demo_store() if args.demo else session_store(args)
    tasks = [] if args.demo else start_collectors(args, store)
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
    result = argparse.ArgumentParser(description="Read-only Linux desktop context and workload overview.")
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("--focus", choices=("auto", "gnome", "sway", "hyprland", "x11", "none"), default="auto")
    result.add_argument("--process-interval", type=interval, default=3.0, metavar="SECONDS",
                        help="current-user /proc scan interval, at least 1s (default: 3)")
    result.add_argument("--no-processes", action="store_true", help="disable /proc scans; use only event sources")
    result.add_argument("--no-media", action="store_true", help="disable MPRIS D-Bus")
    result.add_argument("--no-api", action="store_true", help="open the GUI without the local D-Bus API")
    result.add_argument("--game-app", action="append", default=[], metavar="APP_ID",
                        help="treat an exact focused app ID as a game (repeatable)")
    result.add_argument("--demo", action="store_true", help="show sample context without accessing the device")
    output = result.add_mutually_exclusive_group()
    output.add_argument("--tui", action="store_true", help="open the terminal interface")
    output.add_argument("--once", action="store_true", help="print one JSON snapshot and exit")
    output.add_argument("--json", action="store_true", help="stream JSON snapshots on state changes")
    output.add_argument("--serve", action="store_true", help="serve the local D-Bus API without a window")
    return result


def main() -> None:
    args = parser().parse_args()
    if sys.platform != "linux":
        parser().error("Linux is required")
    if args.serve and args.no_api:
        parser().error("--serve requires the API")
    if os.environ.get(sandbox.SANDBOX_ENV) != "1":
        try:
            status = sandbox.launch(sys.argv[1:], terminal=args.tui, x11=args.focus == "x11",
                                    expose_api=api_requested(args))
        except OSError as error:
            parser().error(str(error))
        except KeyboardInterrupt:
            return
        raise SystemExit(status)
    if args.serve:
        store = demo_store(inventory=True) if args.demo else session_store(args, inventory=True)
        try:
            asyncio.run(observe(args, store))
        except KeyboardInterrupt:
            pass
        except OSError as error:
            parser().exit(1, f"Context Engine: {error}\n")
        return
    if not (args.tui or args.once or args.json):
        try:
            from .gtk_app import run_gui
        except (ImportError, ValueError) as error:
            parser().error(f"GTK 4, Libadwaita 1.4+, and PyGObject are required: {error}. Use --tui for terminal mode.")
        raise SystemExit(run_gui(args))
    if args.tui and not (sys.stdin.isatty() and sys.stdout.isatty()):
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
