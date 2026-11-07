"""Installed application metadata, refreshed by inotify rather than a timer."""

import asyncio
import ctypes
import os
from pathlib import Path


def application_dirs():
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    return tuple(dict.fromkeys(Path(root) / "applications" for root in (data_home, *data_dirs.split(":")) if root))


def applications(directories):
    from gi.repository import GLib
    values = {}
    desktops = set(os.environ.get("XDG_CURRENT_DESKTOP", "").split(":"))
    for directory in directories:
        for path in sorted(directory.rglob("*.desktop")):
            identifier = str(path.relative_to(directory)).replace("/", "-")
            if identifier in values:
                continue
            values[identifier] = None
            info = GLib.KeyFile()
            try:
                info.load_from_file(str(path), GLib.KeyFileFlags.NONE)
                def string(key):
                    try:
                        return info.get_string("Desktop Entry", key)
                    except GLib.Error:
                        return ""
                only, exclude = string("OnlyShowIn"), string("NotShowIn")
                if (string("Type") != "Application" or string("Hidden").lower() == "true"
                        or string("NoDisplay").lower() == "true"
                        or only and not desktops.intersection(only.split(";"))
                        or exclude and desktops.intersection(exclude.split(";"))):
                    continue
                name = info.get_locale_string("Desktop Entry", "Name", None)
            except GLib.Error:
                continue
            # Read metadata without validating or running Exec/TryExec. Targets
            # may intentionally be inaccessible inside the observer sandbox.
            values[identifier] = {"id": identifier, "name": name[:160],
                                  "focus_id": identifier.removesuffix(".desktop")}
    return sorted((app for app in values.values() if app), key=lambda app: (app["name"].casefold(), app["id"]))[:1024]


class DirectoryEvents:
    def __init__(self, directories, dirty):
        self.directories, self.dirty = directories, dirty
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.fd = self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify is unavailable")
        self.watches = set()
        asyncio.get_running_loop().add_reader(self.fd, self.read)

    def refresh(self):
        for directory in self.directories:
            parent = directory
            while not parent.is_dir() and parent != parent.parent:
                parent = parent.parent
            for path in (parent, *directory.rglob("*")):
                if path.is_dir() and path not in self.watches:
                    # modify, attrib, close_write, moves, create, delete, self removal
                    if self.libc.inotify_add_watch(self.fd, os.fsencode(path), 0x00000FCE) >= 0:
                        self.watches.add(path)

    def read(self):
        try:
            while os.read(self.fd, 65536):
                self.dirty.set()
        except BlockingIOError:
            pass
        # Replaced directories need new watches, including after queue overflow.
        self.watches.clear()

    def close(self):
        asyncio.get_running_loop().remove_reader(self.fd)
        os.close(self.fd)


async def watch_applications(store, directories=None):
    directories = directories or application_dirs()
    dirty = asyncio.Event()
    watcher = None
    try:
        watcher = DirectoryEvents(directories, dirty)
        while True:
            dirty.clear()
            watcher.refresh()
            store.update_extra(applications=await asyncio.to_thread(applications, directories))
            store.source("applications", "live", "Installed desktop metadata; inotify change events")
            await dirty.wait()
    except (OSError, ImportError) as error:
        store.source("applications", "unavailable", str(error))
    finally:
        if watcher is not None:
            watcher.close()
