"""Start the observer with a read-only filesystem view and filtered session bus."""

from __future__ import annotations

from contextlib import ExitStack
import importlib.util
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile

from .model import API_BUS_NAME, APP_ID, SANDBOX_ENV


IDLE_SERVICE = "org.gnome.Mutter.IdleMonitor"
IDLE_PATH = "/org/gnome/Mutter/IdleMonitor/Core"


def bus_rules(*, expose_api: bool = False) -> list[str]:
    windows = APP_ID + ".Windows"
    window_path = "/" + windows.replace(".", "/")
    media = "org.mpris.MediaPlayer2.*"
    media_path = "/org/mpris/MediaPlayer2"
    portal = "org.freedesktop.portal.Desktop"
    settings = "org.freedesktop.portal.Settings"
    portal_path = "/org/freedesktop/portal/desktop"
    return [
        f"--own={APP_ID}",
        *([f"--own={API_BUS_NAME}",
           "--call=org.unityconnect=org.unityconnect.Manager1.RequestAccess@/org/unityconnect/Manager",
           "--call=org.unityconnect=org.freedesktop.DBus.Properties.Get@/org/unityconnect/Manager",
           "--call=org.unityconnect=org.unityconnect.Manager1.GetAccessStatus@/org/unityconnect/Manager",
           "--call=org.unityconnect=org.unityconnect.Manager1.ListDevices@/org/unityconnect/Manager",
           "--call=org.unityconnect=org.freedesktop.DBus.Properties.GetAll@/org/unityconnect/devices/*",
           "--call=org.unityconnect=org.unityconnect.Device1.GetState@/org/unityconnect/devices/*",
           "--broadcast=org.unityconnect=org.unityconnect.Manager1.Changed@/org/unityconnect/Manager",
           "--call=io.github.MukhilDharan5.UnityAutomationEngine.Engine=io.github.MukhilDharan5.UnityAutomationEngine.ContextSink1.GetRequirements@/io/github/MukhilDharan5/UnityAutomationEngine/Engine/Context",
           "--call=io.github.MukhilDharan5.UnityAutomationEngine.Engine=io.github.MukhilDharan5.UnityAutomationEngine.ContextSink1.PublishSnapshot@/io/github/MukhilDharan5/UnityAutomationEngine/Engine/Context",
           "--broadcast=io.github.MukhilDharan5.UnityAutomationEngine.Engine=io.github.MukhilDharan5.UnityAutomationEngine.ContextSink1.RequirementsChanged@/io/github/MukhilDharan5/UnityAutomationEngine/Engine/Context",
          ] if expose_api else []),
        f"--call={windows}={windows}.GetSnapshot@{window_path}",
        f"--broadcast={windows}={windows}.Changed@{window_path}",
        f"--call={media}=org.freedesktop.DBus.Properties.Get@{media_path}",
        f"--broadcast={media}=org.freedesktop.DBus.Properties.PropertiesChanged@{media_path}",
        f"--call={portal}={settings}.Read@{portal_path}",
        f"--call={portal}={settings}.ReadAll@{portal_path}",
        f"--call={portal}=org.freedesktop.DBus.Properties.Get@{portal_path}",
        f"--broadcast={portal}={settings}.SettingChanged@{portal_path}",
        f"--broadcast={IDLE_SERVICE}={IDLE_SERVICE}.WatchFired@{IDLE_PATH}",
        *[f"--call={IDLE_SERVICE}={IDLE_SERVICE}.{method}@{IDLE_PATH}"
          for method in ("GetIdletime", "AddIdleWatch", "AddUserActiveWatch", "RemoveWatch")],
    ]


def start_bus_proxy(stack: ExitStack, address: str, proxy_path: Path, *, expose_api: bool = False) -> None:
    read_fd, write_fd = os.pipe()
    stack.callback(os.close, read_fd)
    try:
        proxy = subprocess.Popen([
            "xdg-dbus-proxy", f"--fd={write_fd}", address, str(proxy_path), "--filter",
            *bus_rules(expose_api=expose_api),
        ], pass_fds=(write_fd,))
    finally:
        os.close(write_fd)

    def stop() -> None:
        if proxy.poll() is None:
            proxy.terminate()
        proxy.wait()

    stack.callback(stop)
    if not select.select([read_fd], [], [], 3)[0] or not os.read(read_fd, 1):
        raise OSError("the filtered session bus could not start")


def command(arguments: list[str], proxy_path: Path | None, *, terminal: bool = False,
            x11: bool = False) -> list[str]:
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    args = [
        "bwrap", "--unshare-user", "--unshare-pid", "--unshare-net", "--unshare-ipc",
        "--disable-userns", "--cap-drop", "ALL", "--die-with-parent",
        "--clearenv", "--ro-bind", "/usr", "/usr", "--ro-bind", "/proc", "/proc",
        "--dev", "/dev", "--tmpfs", "/tmp", "--dir", str(runtime),
        "--dir", "/home/context-engine", "--chdir", "/app",
        "--ro-bind", str(Path(__file__).resolve().parent), "/app/unity_context",
        "--setenv", "HOME", "/home/context-engine",
        "--setenv", "XDG_RUNTIME_DIR", str(runtime),
        "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "PYTHONPATH", "/app",
        "--setenv", "GSK_RENDERER", "cairo", "--setenv", SANDBOX_ENV, "1",
    ]
    if not terminal:
        args.append("--new-session")
    for path in ("/bin", "/sbin", "/lib", "/lib64"):
        if os.path.islink(path):
            args.extend(("--symlink", os.readlink(path), path))
        elif os.path.isdir(path):
            args.extend(("--ro-bind", path, path))
    for path in ("/etc/fonts", "/etc/ld.so.cache", "/etc/localtime", "/etc/machine-id",
                 "/etc/passwd", "/etc/group", "/etc/xdg", "/etc/gtk-4.0"):
        args.extend(("--ro-bind-try", path, path))
    for name in ("LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TERM", "COLORTERM", "XDG_CURRENT_DESKTOP",
                 "XDG_SESSION_TYPE", "XDG_ACTIVATION_TOKEN", "DESKTOP_STARTUP_ID", "GTK_A11Y"):
        if name in os.environ:
            args.extend(("--setenv", name, os.environ[name]))

    applications = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "applications"
    args.extend(("--ro-bind-try", str(applications), "/home/context-engine/.local/share/applications"))
    for source, target in (
        ("/usr/local/share/applications", "/usr/local/share/applications"),
        ("/var/lib/flatpak/exports/share/applications", "/run/context-engine/flatpak/system/exports/share/applications"),
        ("/var/lib/flatpak/app", "/run/context-engine/flatpak/system/app"),
        (str(Path.home() / ".local/share/flatpak/exports/share/applications"), "/run/context-engine/flatpak/user/exports/share/applications"),
        (str(Path.home() / ".local/share/flatpak/app"), "/run/context-engine/flatpak/user/app"),
    ):
        args.extend(("--ro-bind-try", source, target))
    args.extend(("--setenv", "XDG_DATA_DIRS", "/usr/local/share:/usr/share:/run/context-engine/flatpak/user/exports/share:/run/context-engine/flatpak/system/exports/share"))
    if wayland := os.environ.get("WAYLAND_DISPLAY"):
        path = str(runtime / wayland)
        args.extend(("--ro-bind-try", path, path, "--setenv", "WAYLAND_DISPLAY", wayland))
    # Local X11/XWayland remains available to the existing xprop backend.
    if (not wayland or x11) and (display := os.environ.get("DISPLAY", "")).startswith(":"):
        number = display[1:].split(".", 1)[0]
        if number.isdecimal():
            path = f"/tmp/.X11-unix/X{number}"
            args.extend(("--ro-bind-try", path, path, "--setenv", "DISPLAY", display))
            auth = os.environ.get("XAUTHORITY") or str(Path.home() / ".Xauthority")
            args.extend(("--ro-bind-try", auth, "/run/context-engine.Xauthority",
                         "--setenv", "XAUTHORITY", "/run/context-engine.Xauthority"))
    for name in ("SWAYSOCK", "I3SOCK"):
        if path := os.environ.get(name):
            args.extend(("--ro-bind-try", path, path, "--setenv", name, path))
    if signature := os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        if "/" not in signature and signature not in {".", ".."}:
            args.extend(("--setenv", "HYPRLAND_INSTANCE_SIGNATURE", signature))
            for socket_name in (".socket.sock", ".socket2.sock"):
                path = str(runtime / "hypr" / signature / socket_name)
                args.extend(("--ro-bind-try", path, path))

    if proxy_path is not None:
        path = str(runtime / "context-engine.bus")
        args.extend(("--ro-bind", str(proxy_path), path,
                     "--setenv", "DBUS_SESSION_BUS_ADDRESS", f"unix:path={path}"))
    # Retain a pip-installed dbus-next when launched from a virtual environment.
    spec = importlib.util.find_spec("dbus_next")
    if spec and spec.origin:
        args.extend(("--ro-bind", str(Path(spec.origin).resolve().parent), "/app/dbus_next"))
    args.extend(("--remount-ro", "/", "--", sys._base_executable, "-B", "-P", "-m", "unity_context", *arguments))
    return args


def launch(arguments: list[str], *, terminal: bool = False, x11: bool = False,
           expose_api: bool = False) -> int:
    if os.getuid() == 0 or os.geteuid() == 0:
        raise OSError("Context Engine must run as a regular desktop user")
    for binary in ("bwrap", "xdg-dbus-proxy"):
        if not shutil.which(binary):
            raise OSError(f"{binary} is required for read-only operation")

    with ExitStack() as stack:
        proxy_path = None
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime / 'bus'}")
        unix_address = next((entry for entry in address.split(";") if entry.startswith("unix:")), None)
        if unix_address and ("DBUS_SESSION_BUS_ADDRESS" in os.environ or (runtime / "bus").exists()):
            directory = stack.enter_context(tempfile.TemporaryDirectory(prefix="context-engine-"))
            proxy_path = Path(directory) / "bus"
            start_bus_proxy(stack, unix_address, proxy_path, expose_api=expose_api)
        return subprocess.run(command(arguments, proxy_path, terminal=terminal, x11=x11), check=False).returncode


def host_pid() -> int:
    # The read-only host procfs uses host PIDs, as do the window and media sources.
    try:
        return int(os.readlink("/proc/self"))
    except (OSError, ValueError):
        return os.getpid()
