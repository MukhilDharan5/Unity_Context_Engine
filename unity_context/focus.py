"""Read-only focus backends. Only subscriptions and information queries."""

from __future__ import annotations

import ast
import asyncio
import contextlib
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import shutil
import struct

from .model import Store, Window


class Unavailable(Exception):
    pass


async def close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()


def focused_node(tree: dict) -> Window | None:
    if tree.get("focused") and (tree.get("app_id") or tree.get("window")):
        props = tree.get("window_properties") or {}
        return Window(
            app=tree.get("app_id") or props.get("class") or "",
            title=tree.get("name") or "", pid=tree.get("pid"),
            fullscreen=bool(tree.get("fullscreen_mode", 0)), id=str(tree["id"]),
        )
    for child in tree.get("nodes", []) + tree.get("floating_nodes", []):
        if (window := focused_node(child)) is not None:
            return window
    return None


async def i3_send(writer: asyncio.StreamWriter, kind: int, payload: str = "") -> None:
    data = payload.encode()
    writer.write(b"i3-ipc" + struct.pack("=II", len(data), kind) + data)
    await writer.drain()


async def i3_receive(reader: asyncio.StreamReader) -> tuple[int, dict | list]:
    header = await reader.readexactly(14)
    if header[:6] != b"i3-ipc":
        raise Unavailable("invalid i3/Sway IPC header")
    size, kind = struct.unpack("=II", header[6:])
    if size > 32 * 1024 * 1024:
        raise Unavailable("i3/Sway IPC response too large")
    return kind, json.loads(await reader.readexactly(size))


async def i3_tree(path: str) -> dict:
    reader, writer = await asyncio.open_unix_connection(path)
    try:
        await i3_send(writer, 4)  # GET_TREE; never COMMAND
        _, tree = await i3_receive(reader)
        return tree
    finally:
        await close_writer(writer)


async def sway(store: Store, path: str) -> None:
    reader, writer = await asyncio.open_unix_connection(path)
    try:
        await i3_send(writer, 2, '["window", "workspace", "shutdown"]')
        async with asyncio.timeout(2):
            _, reply = await i3_receive(reader)
        if not isinstance(reply, dict) or not reply.get("success"):
            raise Unavailable("i3/Sway rejected event subscription")
        # Subscribe before snapshotting. Use a separate request socket so events
        # cannot be mistaken for replies; queued events trigger a fresh snapshot.
        async def refresh() -> None:
            async with asyncio.timeout(2):
                store.set_window(focused_node(await i3_tree(path)))

        await refresh()
        store.source("focus", "live", "i3/Sway IPC events")
        while True:
            kind, event = await i3_receive(reader)
            if kind == (1 << 31) | 6:
                raise Unavailable("compositor IPC closed")
            if kind == (1 << 31) | 3:
                change = event.get("change")
                container = event.get("container") or {}
                if change == "title":
                    if store.window and str(container.get("id")) == store.window.id:
                        store.set_window(replace(store.window, title=container.get("name") or ""))
                    continue  # Background title churn needs no tree query.
                if change not in {"focus", "fullscreen_mode", "close"}:
                    continue
                if change == "close" and store.window and str(container.get("id")) != store.window.id:
                    continue
            await refresh()
    finally:
        await close_writer(writer)


def hypr_window(data: dict) -> Window | None:
    if not data.get("address"):
        return None
    # Hyprland fullscreen: 0 = none, 1 = maximized, 2/3 = fullscreen.
    # fullscreenClient describes the client's request, not compositor state.
    return Window(
        app=data.get("class", ""), title=data.get("title", ""),
        pid=data.get("pid"), fullscreen=data.get("fullscreen", 0) in (2, 3),
        id=data["address"],
    )


def hypr_path() -> Path:
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not signature or not runtime:
        raise Unavailable("Hyprland session socket environment is missing")
    return Path(runtime) / "hypr" / signature


async def hypr_query(path: Path) -> dict:
    # This is the single allowed Hyprland request. Close each request promptly;
    # Hyprland handles this socket synchronously.
    reader, writer = await asyncio.open_unix_connection(str(path / ".socket.sock"))
    try:
        writer.write(b"j/activewindow")
        await writer.drain()
        chunks, size = [], 0
        while chunk := await reader.read(65536):
            size += len(chunk)
            if size > 1024 * 1024:
                raise Unavailable("Hyprland response too large")
            chunks.append(chunk)
        return json.loads(b"".join(chunks))
    finally:
        await close_writer(writer)


async def hyprland(store: Store) -> None:
    path = hypr_path()
    reader, writer = await asyncio.open_unix_connection(str(path / ".socket2.sock"))
    events: asyncio.Task | None = None
    try:
        async def refresh() -> None:
            async with asyncio.timeout(1):
                store.set_window(hypr_window(await hypr_query(path)))

        relevant = {b"activewindow", b"activewindowv2", b"windowtitle", b"windowtitlev2",
                    b"fullscreen", b"closewindow", b"workspace", b"workspacev2",
                    b"focusedmon", b"focusedmonv2"}
        dirty = asyncio.Event()

        async def read_events() -> None:
            while line := await reader.readline():
                event, _, payload = line.partition(b">>")
                if event == b"windowtitlev2" and store.window:
                    address = payload.partition(b",")[0].decode(errors="replace")
                    if address != store.window.id:
                        continue
                if event in relevant:
                    dirty.set()
            raise Unavailable("Hyprland event socket closed")

        events = asyncio.create_task(read_events())
        await refresh()
        store.source("focus", "live", "Hyprland IPC events")
        while True:
            change = asyncio.create_task(dirty.wait())
            try:
                done, _ = await asyncio.wait({events, change}, return_when=asyncio.FIRST_COMPLETED)
                if events in done:
                    await events
                await asyncio.sleep(0.03)  # Collapse paired v1/v2 events.
                dirty.clear()
                await refresh()
            finally:
                change.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await change
    finally:
        if events is not None:
            events.cancel()
            await asyncio.gather(events, return_exceptions=True)
        await close_writer(writer)


def xprop_window(properties: dict[str, str], window_id: str) -> Window:
    def strings(value: str) -> list[str]:
        parts = re.findall(r'"(?:[^"\\]|\\.)*"', value)
        result = []
        for part in parts:
            try:
                result.append(ast.literal_eval(part))
            except (ValueError, SyntaxError):
                pass
        return result

    classes = strings(properties.get("WM_CLASS", ""))
    title = strings(properties.get("_NET_WM_NAME", "")) or strings(properties.get("WM_NAME", ""))
    pid_text = properties.get("_NET_WM_PID", "").strip()
    state = properties.get("_NET_WM_STATE")
    return Window(
        app=classes[-1] if classes else "", title=title[0] if title else "",
        pid=int(pid_text) if pid_text.isdecimal() else None,
        fullscreen=("_NET_WM_STATE_FULLSCREEN" in state) if state is not None else None,
        id=window_id,
    )


async def xprop_lines(*args: str):
    process = await asyncio.create_subprocess_exec(
        "xprop", "-notype", *args, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env={**os.environ, "LC_ALL": "C.UTF-8"}, limit=1024 * 1024,
    )
    try:
        assert process.stdout is not None
        while line := await process.stdout.readline():
            yield line.decode("utf-8", errors="replace").rstrip("\n")
        raise Unavailable("xprop subscription ended; check DISPLAY and X11 permissions")
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()  # Only this application's helper process.
        try:
            await asyncio.wait_for(process.wait(), 1)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()


async def x11(store: Store) -> None:
    if not shutil.which("xprop"):
        raise Unavailable("X11 requires xprop (usually the x11-utils / xorg-xprop package)")

    async def window_watch(window_id: str) -> None:
        properties: dict[str, str] = {}
        stream = xprop_lines("-spy", "-id", window_id, "WM_CLASS", "_NET_WM_NAME",
                             "WM_NAME", "_NET_WM_STATE", "_NET_WM_PID")
        try:
            async for line in stream:
                name, separator, value = line.partition(" = ")
                if separator:
                    properties[name] = value
                elif ":" in line:
                    # A removed EWMH state property means no fullscreen state.
                    name = line.partition(":")[0]
                    properties[name] = ""
                store.set_window(xprop_window(properties, window_id))
        except (Unavailable, OSError, ValueError) as error:
            store.set_window(None)
            store.source("focus", "unavailable", f"{error}; waiting for next focus change")
        finally:
            await stream.aclose()

    task: asyncio.Task | None = None
    stream = xprop_lines("-spy", "-root", "_NET_ACTIVE_WINDOW")
    try:
        async for line in stream:
            if task is not None:
                task.cancel()
                # A destroyed window is normal, and may end its helper first.
                with contextlib.suppress(asyncio.CancelledError, Unavailable):
                    await task
                task = None
            match = re.search(r"0x[0-9a-fA-F]+", line)
            store.set_window(None)
            if not match:
                raise Unavailable("window manager does not expose _NET_ACTIVE_WINDOW")
            detail = "X11 EWMH property events (xprop)"
            if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
                detail += "; XWayland windows only (partial)"
            store.source("focus", "live", detail)
            if int(match[0], 16):
                task = asyncio.create_task(window_watch(match[0]))
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Unavailable):
                await task
        await stream.aclose()


def choose_backend(requested: str) -> str:
    if requested != "auto":
        return requested
    if os.environ.get("SWAYSOCK") or os.environ.get("I3SOCK"):
        return "sway"
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return "hyprland"
    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
        return "unsupported-wayland"
    return "x11" if os.environ.get("DISPLAY") else "headless"


async def watch(store: Store, requested: str) -> None:
    backend = choose_backend(requested)
    if backend in {"none", "unsupported-wayland", "headless"}:
        detail = {
            "none": "disabled by --focus none",
            "unsupported-wayland": "this Wayland compositor has no supported read-only focus API; use Sway, Hyprland or X11",
            "headless": "no supported graphical session found",
        }[backend]
        store.source("focus", "disabled" if backend == "none" else "unavailable", detail)
        return
    delay = 1
    while True:
        try:
            if backend == "sway":
                path = os.environ.get("SWAYSOCK") or os.environ.get("I3SOCK")
                if not path:
                    raise Unavailable("SWAYSOCK / I3SOCK is missing")
                await sway(store, path)
            elif backend == "hyprland":
                await hyprland(store)
            else:
                await x11(store)
        except (OSError, ValueError, KeyError, TypeError, TimeoutError,
                asyncio.IncompleteReadError, Unavailable) as error:
            store.set_window(None)
            store.source("focus", "unavailable", f"{error}; reconnecting in {delay}s")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30)
