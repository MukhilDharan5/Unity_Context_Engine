"""Small state store and conservative, explainable context inference."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import re


@dataclass(frozen=True)
class Window:
    app: str = ""
    title: str = ""
    pid: int | None = None
    fullscreen: bool | None = None
    id: str = ""


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
    if re.fullmatch(r"steam_app_\d+", window.app, re.IGNORECASE):
        return "focused window has a Steam game app ID"
    if app_key(window.app) in {app_key(app) for app in game_apps}:
        return "focused app matches --game-app"
    return None


class Store:
    def __init__(self, session: str, game_apps: tuple[str, ...] = ()):
        self.session = session
        self.game_apps = game_apps
        self.window: Window | None = None
        self.players: dict[str, Player] = {}
        self.compilers: tuple[Compiler, ...] = ()
        self.sources = {
            key: Source("starting", "initializing")
            for key in ("focus", "media", "processes")
        }
        self.changed = asyncio.Event()
        self.changed.set()

    def set_window(self, window: Window | None) -> None:
        if self.window != window:
            self.window = window
            self.changed.set()

    def set_players(self, players: dict[str, Player]) -> None:
        if self.players != players:
            self.players = dict(players)
            self.changed.set()

    def set_compilers(self, compilers: tuple[Compiler, ...]) -> None:
        if self.compilers != compilers:
            self.compilers = compilers
            self.changed.set()

    def source(self, key: str, status: str, detail: str) -> None:
        value = Source(status, detail)
        if self.sources[key] != value:
            self.sources[key] = value
            self.changed.set()

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
        return {
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
