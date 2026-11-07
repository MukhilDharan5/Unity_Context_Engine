"""Unprivileged, current-user-only /proc sampling; no command lines or env."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import re
from collections.abc import Iterator
import time

from .model import ActiveProcess, Compiler, Store, SystemLoad


# Build launchers (cargo, make, ninja), language servers and runtimes are
# deliberately absent: their presence alone doesn't demonstrate compilation.
COMPILER = re.compile(
    r"(?:cc1(?:plus)?|[a-z0-9_+.-]*-)?(?:gcc|g\+\+|clang(?:\+\+)?)(?:-\d+)?$"
    r"|(?:cc1|cc1plus|rustc|zig|nvcc|cicc|ptxas|swift-frontend|f951|flang(?:-new)?|javac|ghc|kotlinc|wasm-opt)$"
)

PROCESS_WORKLOADS = {
    worker: category
    for category, names in (
        ("Build tooling", "ninja make gmake cmake meson cargo bazel bazelisk"),
        ("Running tests", "pytest ctest vitest jest playwright regrtest"),
        ("Debugging", "gdb lldb rr valgrind"),
        ("Profiling", "perf sysprof-cli tracy-capture"),
        ("Media processing", "ffmpeg ffprobe handbrakecli gst-launch-1.0 opusenc"),
        ("Image processing", "magick convert cwebp optipng pngquant darktable-cli"),
        ("Rendering", "povray mitsuba luxcoreconsole blender-render"),
        ("Compression and archives", "gzip pigz bzip2 pbzip2 xz zstd tar 7z 7zz unrar"),
        ("File transfers and sync", "rsync rclone syncthing scp sftp aria2c"),
        ("Backups", "restic borg kopia duplicity bup"),
        ("Database activity", "postgres mysqld mariadbd mongod redis-server clickhouse duckdb"),
        ("Virtual machines", "virtualboxvm vboxheadless vmware-vmx cloud-hyperviso"),
        ("Container operations", "podman buildah conmon docker containerd-shim buildkitd kubectl"),
        ("Local AI processing", "ollama llama-cli llama-server ollama_llama_se koboldcpp whisper-cli"),
        ("Indexing", "updatedb tracker-miner-f localsearch-3 baloo_file rust-analyzer clangd"),
        ("Version control", "git git-remote-http git-lfs"),
        ("Software packages", "dnf dnf5 apt dpkg rpm pacman flatpak"),
        ("Network diagnostics", "tcpdump tshark iperf3 nmap"),
        ("Simulation", "ngspice ltspice gmx mdrun lmp openfoam"),
    )
    for worker in names.split()
}


@dataclass(frozen=True)
class Process:
    pid: int
    name: str
    state: str
    ticks: int
    started: int
    parent: int = 0
    rss_pages: int = 0


def parse_stat(text: str) -> Process:
    # comm may contain spaces and ')'; the final ')' ends the comm field.
    left, right = text.index("("), text.rindex(")")
    fields = text[right + 2:].split()
    return Process(
        pid=int(text[:left].strip()), name=text[left + 1:right],
        state=fields[0], ticks=int(fields[11]) + int(fields[12]),
        started=int(fields[19]), parent=int(fields[1]),
        rss_pages=max(0, int(fields[21])) if len(fields) > 21 else 0,
    )


def iter_scan(proc: Path = Path("/proc"), uid: int | None = None, *,
              all_processes: bool = False) -> Iterator[Process | None]:
    uid = os.getuid() if uid is None else uid
    with os.scandir(proc) as entries:
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            try:
                process = None
                # Resolve stat inside the original PID directory. A recycled PID
                # cannot swap in a process from another user after the UID check.
                fd = os.open(entry.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
                try:
                    if os.fstat(fd).st_uid == uid:
                        stat_fd = os.open("stat", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                        with os.fdopen(stat_fd, errors="replace") as stream:
                            candidate = parse_stat(stream.read())
                        if all_processes or COMPILER.fullmatch(candidate.name):
                            process = candidate
                finally:
                    os.close(fd)
                yield process
            except (OSError, ValueError, IndexError):
                # Process exit races and permission restrictions are normal.
                yield None


def scan(proc: Path = Path("/proc"), uid: int | None = None) -> tuple[Process, ...]:
    return tuple(p for p in iter_scan(proc, uid) if p is not None)


async def scan_async(*, all_processes: bool = False) -> tuple[Process, ...]:
    found = []
    # procfs reads are small virtual-file reads. Yield between batches so large
    # process tables don't monopolize the UI. No executor threads to join at exit.
    for index, process in enumerate(iter_scan(all_processes=all_processes)):
        if process is not None:
            found.append(process)
        if index % 64 == 63:
            await asyncio.sleep(0)
    return tuple(found)


def active_compilers(current: tuple[Process, ...], previous: tuple[Process, ...]) -> tuple[Compiler, ...]:
    before = {(p.pid, p.started): p.ticks for p in previous}
    names = {p.pid: p.name for p in current}
    return tuple(sorted((
        Compiler(p.pid, p.name) for p in current
        if is_compiler(p, names) and p.state not in {"T", "t", "Z", "X"} and (
            p.state == "R" or (
                (p.pid, p.started) in before and p.ticks > before[p.pid, p.started]
            )
        )
    ), key=lambda p: p.pid))


def is_compiler(process: Process, names: dict[int, str]) -> bool:
    # Go's compiler workers have generic names. Require a Go parent to match.
    return bool(COMPILER.fullmatch(process.name)) or (
        process.name == "compile" and names.get(process.parent) == "go"
    )


def busy_processes(current: tuple[Process, ...], previous: tuple[Process, ...],
                   elapsed: float, own_pid: int | None = None) -> tuple[ActiveProcess, ...]:
    if elapsed <= 0 or not previous:
        return ()
    ticks_per_second = os.sysconf("SC_CLK_TCK")
    page_size = os.sysconf("SC_PAGE_SIZE")
    before = {(p.pid, p.started): p.ticks for p in previous}
    names = {p.pid: p.name for p in current}
    busy = []
    for process in current:
        old_ticks = before.get((process.pid, process.started))
        if old_ticks is None or process.pid == own_pid or process.state in {"T", "t", "Z", "X"}:
            continue
        cpu = round(100 * (process.ticks - old_ticks) / ticks_per_second / elapsed)
        if cpu < 3:
            continue
        name = process.name.casefold()
        workload = PROCESS_WORKLOADS.get(name)
        if name.startswith("qemu-system-"):
            workload = "Virtual machines"
        if is_compiler(process, names):
            workload = "Compiling"
        busy.append(ActiveProcess(process.pid, process.name, cpu, process.rss_pages * page_size, workload))
    return tuple(sorted(busy, key=lambda p: (-p.cpu_percent, p.pid)))


def cpu_ticks(path: Path) -> tuple[int, int] | None:
    try:
        with path.open() as stream:
            fields = [int(value) for value in stream.readline().split()[1:9]]
        return sum(fields), fields[3] + fields[4]
    except (OSError, ValueError, IndexError):
        return None


def read_system_load(previous_cpu: tuple[int, int] | None, proc: Path = Path("/proc")) -> tuple[SystemLoad, tuple[int, int] | None]:
    current_cpu = cpu_ticks(proc / "stat")
    cpu = None
    if previous_cpu and current_cpu:
        elapsed = current_cpu[0] - previous_cpu[0]
        idle = current_cpu[1] - previous_cpu[1]
        if elapsed > 0:
            cpu = max(0, min(100, round(100 * (elapsed - idle) / elapsed)))

    memory = {}
    try:
        for line in (proc / "meminfo").read_text().splitlines():
            field, _, value = line.partition(":")
            if field in {"MemTotal", "MemAvailable"}:
                memory[field] = int(value.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass

    def pressure(resource: str) -> float | None:
        try:
            with (proc / "pressure" / resource).open() as stream:
                values = stream.readline().split()
            if values[0] == "some" and values[1].startswith("avg10="):
                return float(values[1].removeprefix("avg10="))
        except (OSError, ValueError, IndexError):
            pass
        return None

    return SystemLoad(cpu, memory.get("MemTotal"), memory.get("MemAvailable"),
                      pressure("cpu"), pressure("memory"), pressure("io")), current_cpu


async def watch(store: Store, interval: float) -> None:
    previous: tuple[Process, ...] = ()
    previous_cpu = None
    sampled_at = time.monotonic()
    while True:
        try:
            current = await scan_async(all_processes=store.inventory)
            now = time.monotonic()
            store.set_compilers(active_compilers(current, previous))
            if store.inventory:
                load, previous_cpu = read_system_load(previous_cpu)
                store.set_load(load, busy_processes(current, previous, now - sampled_at, store.own_pid))
            sampled_at = now
            previous = current
            store.source("processes", "live", f"/proc sampling every {interval:g}s; current user only")
        except OSError:
            previous = ()
            previous_cpu = None
            store.set_compilers(())
            store.set_load(None, ())
            store.source("processes", "unavailable", "cannot read /proc")
        await asyncio.sleep(interval)
