"""Small state store and conservative, explainable context inference."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import os
import re


APP_ID = "io.github.MukhilDharan5.UnityContextEngine"
APP_NAME = "Context Engine"
API_BUS_NAME = APP_ID + ".Context1"
SANDBOX_ENV = "CONTEXT_ENGINE_READ_ONLY"


@dataclass(frozen=True)
class Window:
    app: str = ""
    title: str = ""
    pid: int | None = None
    fullscreen: bool | None = None
    id: str = ""
    app_name: str = ""


@dataclass(frozen=True)
class Player:
    name: str
    identity: str = ""
    desktop_entry: str = ""
    pid: int | None = None
    playback: str = "Unknown"
    fullscreen: bool | None = None


@dataclass(frozen=True)
class Compiler:
    pid: int
    name: str


@dataclass(frozen=True)
class ActiveProcess:
    pid: int
    name: str
    cpu_percent: int
    memory_bytes: int
    workload: str | None = None


@dataclass(frozen=True)
class SystemLoad:
    cpu_percent: int | None = None
    memory_total: int | None = None
    memory_available: int | None = None
    cpu_pressure: float | None = None
    memory_pressure: float | None = None
    io_pressure: float | None = None


@dataclass(frozen=True)
class Source:
    status: str
    detail: str


def app_key(app: str) -> str:
    return app.casefold().removesuffix(".desktop")


def same_app(window: Window, player: Player) -> bool:
    # No title or substring matching: unrelated background audio must not turn
    # an arbitrary fullscreen window into "fullscreen media".
    return bool(
        (window.pid is not None and window.pid == player.pid)
        or (window.app and player.desktop_entry
            and app_key(window.app) == app_key(player.desktop_entry))
    )


def game_evidence(window: Window | None, game_apps: tuple[str, ...]) -> str | None:
    if window is None:
        return None
    if re.fullmatch(r"steam_app_\d+", app_key(window.app)):
        return "window has a Steam game app ID"
    if app_key(window.app) in {app_key(app) for app in game_apps}:
        return "app matches --game-app"
    return None


class Store:
    def __init__(self, session: str, game_apps: tuple[str, ...] = (), *,
                 inventory: bool = False, own_pid: int | None = None):
        self.session = session
        self.game_apps = game_apps
        self.inventory = inventory
        self.own_pid = own_pid
        self.open_windows: tuple[Window, ...] | None = None
        self.last_window: Window | None = None
        self.window: Window | None = None
        self.players: dict[str, Player] = {}
        self.compilers: tuple[Compiler, ...] = ()
        self.active_processes: tuple[ActiveProcess, ...] = ()
        self.system_load: SystemLoad | None = None
        self.idle: bool | None = None
        self.sources = {
            key: Source("starting", "initializing")
            for key in ("focus", "media", "processes")
        }
        if inventory:
            self.sources["presence"] = Source("starting", "initializing")
        self.extra_context = {"devices": {}, "choices": {}, "applications": [], "clock": {}, "observer": {}}
        self.listeners = set()
        self.changed = asyncio.Event()
        self.notify()

    def notify(self) -> None:
        self.changed.set()
        if self.listeners:
            snapshot = self.snapshot()
            for listener in tuple(self.listeners):
                listener(snapshot)

    def update_extra(self, **values) -> None:
        if any(self.extra_context.get(key) != value for key, value in values.items()):
            self.extra_context.update(values)
            self.notify()

    def is_own_window(self, window: Window) -> bool:
        return (
            (self.own_pid is not None and window.pid == self.own_pid)
            or app_key(window.app) == app_key(APP_ID)
        )

    def set_window(self, window: Window | None) -> None:
        if window is not None and self.is_own_window(window):
            window = None
        if self.window != window or window is not None and self.last_window != window:
            self.window = window
            if window is not None:
                self.last_window = window
            self.notify()

    def set_desktop(self, focused: Window | None, windows: tuple[Window, ...] | None,
                    *, ranked: bool = False) -> None:
        self.set_window(focused)
        if windows is not None:
            windows = tuple(window for window in windows if not self.is_own_window(window))
            if ranked and self.last_window is None and windows and self.window is None:
                # Seed the recent window from compositor history before we observe any external focus.
                self.last_window = windows[0]
                self.notify()
            if self.last_window and self.window is None:
                recent = next((w for w in windows if w.id == self.last_window.id), None)
                if recent is not None and recent != self.last_window:
                    self.last_window = recent
                    self.notify()
        if self.open_windows != windows:
            self.open_windows = windows
            self.notify()

    def set_players(self, players: dict[str, Player]) -> None:
        if self.players != players:
            self.players = dict(players)
            self.notify()

    def set_compilers(self, compilers: tuple[Compiler, ...]) -> None:
        if self.compilers != compilers:
            self.compilers = compilers
            self.notify()

    def set_load(self, load: SystemLoad | None, active: tuple[ActiveProcess, ...]) -> None:
        if self.system_load != load or self.active_processes != active:
            self.system_load = load
            self.active_processes = active
            self.notify()

    def set_idle(self, idle: bool | None) -> None:
        if self.idle != idle:
            self.idle = idle
            self.notify()

    def source(self, key: str, status: str, detail: str) -> None:
        value = Source(status, detail)
        if self.sources.get(key) != value:
            self.sources[key] = value
            self.notify()

    def snapshot(self) -> dict:
        playing = [p for p in self.players.values() if p.playback == "Playing"]
        evidence = game_evidence(self.window, self.game_apps)
        fullscreen_players = [
            p for p in playing if p.fullscreen is True
            or (self.window is not None and self.window.fullscreen is True
                and same_app(self.window, p))
        ]
        media_live = self.sources["media"].status == "live"
        fullscreen_media: bool | None = None
        if fullscreen_players:
            fullscreen_media = True
        elif media_live and (not playing or all(
            p.fullscreen is False or (
                self.window is not None and same_app(self.window, p)
                and self.window.fullscreen is False
            ) for p in playing
        )):
            fullscreen_media = False

        compiling = (bool(self.compilers)
                     if self.sources["processes"].status == "live" else None)
        labels = []
        if evidence:
            labels.append("Game focused")
        if fullscreen_media:
            labels.append("Fullscreen media")
        elif playing:
            labels.append("Media playing")
        if compiling:
            labels.append("Compiling")
        result = {
            "session": self.session,
            "activity": labels or ["Observing"],
            "focus": asdict(self.window) if self.window is not None else None,
            "media": [asdict(p) for p in sorted(self.players.values(), key=lambda p: p.name)],
            "fullscreen_media": fullscreen_media,
            "fullscreen_media_players": [p.name for p in fullscreen_players],
            "game": {
                "detected": bool(evidence) if self.window is not None else None,
                "evidence": evidence,
            },
            "compiling": compiling,
            "compilers": [asdict(c) for c in self.compilers],
            "sources": {k: asdict(v) for k, v in self.sources.items()},
        }
        if self.inventory:
            result.update(self.extra_context)
            result.update(
                last_window=asdict(self.last_window) if self.last_window else None,
                open_windows=None if self.open_windows is None else [asdict(w) for w in self.open_windows],
                game_apps=self.game_apps,
                active_processes=[asdict(p) for p in self.active_processes],
                system_load=asdict(self.system_load) if self.system_load is not None else None,
                idle=self.idle,
                read_only=os.environ.get(SANDBOX_ENV) == "1",
            )
        elif result["focus"] is not None:
            result["focus"].pop("app_name")
        return result
