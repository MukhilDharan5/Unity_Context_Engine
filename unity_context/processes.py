"""Unprivileged, current-user-only /proc sampling; no command lines or env."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import re
from collections.abc import Iterator

from .model import Compiler, Store


# Build launchers (cargo, make, ninja), language servers and runtimes are
# deliberately absent: their presence alone doesn't demonstrate compilation.
COMPILER = re.compile(
    r"(?:cc1(?:plus)?|[a-z0-9_+.-]*-)?(?:gcc|g\+\+|clang(?:\+\+)?)(?:-\d+)?$"
    r"|(?:cc1|cc1plus|rustc|nvcc|cicc|ptxas|swift-frontend|f951|flang(?:-new)?|javac)$"
)


@dataclass(frozen=True)
class Process:
    pid: int
    name: str
    state: str
    ticks: int
    started: int


def parse_stat(text: str) -> Process:
    # comm may contain spaces and ')'; the final ')' ends the comm field.
    left, right = text.index("("), text.rindex(")")
    fields = text[right + 2:].split()
    return Process(
        pid=int(text[:left].strip()), name=text[left + 1:right],
        state=fields[0], ticks=int(fields[11]) + int(fields[12]),
        started=int(fields[19]),
    )


def iter_scan(proc: Path = Path("/proc"), uid: int | None = None) -> Iterator[Process | None]:
    uid = os.getuid() if uid is None else uid
    with os.scandir(proc) as entries:
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            try:
                process = None
                if entry.stat(follow_symlinks=False).st_uid == uid:
                    candidate = parse_stat((Path(entry.path) / "stat").read_text())
                    if COMPILER.fullmatch(candidate.name):
                        process = candidate
                yield process
            except (OSError, ValueError, IndexError):
                # Process exit races and permission restrictions are normal.
                yield None


def scan(proc: Path = Path("/proc"), uid: int | None = None) -> tuple[Process, ...]:
    return tuple(p for p in iter_scan(proc, uid) if p is not None)


async def scan_async() -> tuple[Process, ...]:
    found = []
    # procfs reads are small virtual-file reads. Yield between batches so large
    # process tables don't monopolize the UI. No executor threads to join at exit.
    for index, process in enumerate(iter_scan()):
        if process is not None:
            found.append(process)
        if index % 64 == 63:
            await asyncio.sleep(0)
    return tuple(found)


def active_compilers(current: tuple[Process, ...], previous: tuple[Process, ...]) -> tuple[Compiler, ...]:
    before = {(p.pid, p.started): p.ticks for p in previous}
    return tuple(sorted((
        Compiler(p.pid, p.name) for p in current
        if p.state not in {"T", "t", "Z", "X"} and (
            p.state == "R" or (
                (p.pid, p.started) in before and p.ticks > before[p.pid, p.started]
            )
        )
    ), key=lambda p: p.pid))


async def watch(store: Store, interval: float) -> None:
    previous: tuple[Process, ...] = ()
    while True:
        try:
            current = await scan_async()
            store.set_compilers(active_compilers(current, previous))
            previous = current
            store.source("processes", "live", f"/proc sampling every {interval:g}s; current user only")
        except OSError:
            previous = ()
            store.set_compilers(())
            store.source("processes", "unavailable", "cannot read /proc")
        await asyncio.sleep(interval)
