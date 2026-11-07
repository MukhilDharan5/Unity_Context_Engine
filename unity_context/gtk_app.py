"""Native GTK 4 and Libadwaita views for desktop context."""

import asyncio
import contextlib
from pathlib import Path
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from .app import demo_store, observe, safe_text, session_store, verdict
from .model import APP_ID, APP_NAME
from .presence import IDLE_MINUTES
from .workload import app_category, predict

if Adw.get_major_version() == 1 and Adw.get_minor_version() < 4:
    raise ImportError("Libadwaita 1.4 or newer is required")

CSS = """
.workload-card { padding: 20px; border-radius: 16px; }
.workload-icon {
    color: @accent_color;
    background: alpha(@accent_bg_color, 0.15);
    border-radius: 16px;
    min-width: 56px;
    min-height: 56px;
}
.context-status {
    background: alpha(currentColor, 0.08);
    border-radius: 999px;
    padding: 5px 10px;
}
"""


class Observer:
    """Run the shared asynchronous collectors off the GTK thread."""

    def __init__(self, args, on_snapshot):
        self.args = args
        self.on_snapshot = on_snapshot
        self.loop = None
        self.stop_event = None
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.pending = None
        self.delivery_scheduled = False
        self.thread = threading.Thread(target=self.run, name="desktop-context", daemon=True)
        self.thread.start()

    def run(self):
        asyncio.run(self.collect())

    def publish(self, snapshot):
        with self.lock:
            self.pending = snapshot
            if self.delivery_scheduled:
                return
            self.delivery_scheduled = True
        GLib.idle_add(self.deliver)

    def deliver(self):
        with self.lock:
            snapshot = self.pending
            self.pending = None
            self.delivery_scheduled = False
        if not self.closed.is_set():
            self.on_snapshot(snapshot)
        return GLib.SOURCE_REMOVE

    async def collect(self):
        self.stop_event = asyncio.Event()
        self.loop = asyncio.get_running_loop()
        if self.closed.is_set():
            return

        store = session_store(self.args, inventory=True)
        await observe(self.args, store, on_snapshot=self.publish, stop=self.stop_event)

    def close(self):
        self.closed.set()
        if self.loop is not None:
            with contextlib.suppress(RuntimeError):
                self.loop.call_soon_threadsafe(self.stop_event.set)
        self.thread.join(timeout=1.5)


def label(text="", css=None):
    widget = Gtk.Label(label=text, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
    if css:
        widget.add_css_class(css)
    return widget


def page(heading, subtitle):
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
    content.set_margin_top(28)
    content.set_margin_bottom(28)
    content.set_margin_start(18)
    content.set_margin_end(18)
    title = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
    title.append(label(heading, "title-1"))
    title.append(label(subtitle, "dim-label"))
    content.append(title)
    clamp = Adw.Clamp(maximum_size=748, tightening_threshold=520, child=content)
    scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=clamp)
    return scroll, content


class Rows:
    """Keep a preference group stable when its visible rows did not change."""

    def __init__(self, content, title):
        self.group = Adw.PreferencesGroup(title=title)
        content.append(self.group)
        self.values = ()
        self.rows = []

    def show(self, values):
        values = tuple(values)
        if values == self.values:
            return
        for row in self.rows:
            self.group.remove(row)
        self.rows.clear()
        for title, subtitle, icon in values:
            row = Adw.ActionRow(
                use_markup=False, title=safe_text(title), subtitle=safe_text(subtitle),
                title_lines=2, subtitle_lines=3,
            )
            if icon:
                row.add_prefix(Gtk.Image.new_from_icon_name(icon))
            self.group.add(row)
            self.rows.append(row)
        self.values = values


def window_row(window, *, detailed=False):
    app = window["app_name"] or window["app"] or "Unknown application"
    title = window["title"] or "Untitled window"
    category = app_category(window["app"], window["app_name"])
    icon = category[1] if category else "application-x-executable-symbolic"
    if detailed:
        fields = [app]
        if window["app"] and window["app"] != app:
            fields.append(window["app"])
        if window["pid"]:
            fields.append(f"PID {window['pid']}")
        if window["fullscreen"] is True:
            fields.append("Fullscreen")
        return title, " · ".join(fields), icon
    return app, title, icon


def resource_rows(load):
    if load is None:
        return [("Resource measurements unavailable", "", "utilities-system-monitor-symbolic")]
    cpu = load["cpu_percent"]
    available = load["memory_available"]
    total = load["memory_total"]
    memory = "Unavailable"
    if available is not None and total is not None:
        gib = 1024 ** 3
        memory = f"{available / gib:.1f} GiB available of {total / gib:.1f} GiB"
    pressure = [f"{name} {load[key]:.2f}%" for name, key in (
        ("CPU", "cpu_pressure"), ("Memory", "memory_pressure"), ("I/O", "io_pressure"),
    ) if load[key] is not None]
    return [
        ("CPU utilization", "Measuring…" if cpu is None else f"{cpu}% across all cores", "utilities-system-monitor-symbolic"),
        ("System memory", memory, "drive-harddisk-symbolic"),
        ("Resource pressure · 10-second average", " · ".join(pressure) or "Unavailable", "dialog-information-symbolic"),
    ]


class ContextWindow(Adw.ApplicationWindow):
    def __init__(self, application):
        super().__init__(application=application, title=APP_NAME,
                         default_width=800, default_height=760)
        self.stack = Adw.ViewStack()
        toolbar = Adw.ToolbarView(content=self.stack)
        header = Adw.HeaderBar()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        toolbar.add_top_bar(header)
        self.set_content(toolbar)

        overview, body = page("Desktop overview", "Your recent windows and likely activity")
        self.stack.add_titled_with_icon(overview, "overview", "Overview", "view-grid-symbolic")

        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        card.add_css_class("card")
        card.add_css_class("workload-card")
        self.workload_icon = Gtk.Image(pixel_size=27, valign=Gtk.Align.CENTER)
        self.workload_icon.add_css_class("workload-icon")
        card.append(self.workload_icon)
        workload_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        workload_text.append(label("Predicted workload", "caption-heading"))
        self.workload_name = label(css="title-2")
        self.workload_hint = label(css="dim-label")
        workload_text.append(self.workload_name)
        workload_text.append(self.workload_hint)
        card.append(workload_text)
        body.append(card)

        self.recent = Rows(body, "Last used window")
        self.open_windows = Rows(body, "Open windows")
        self.all_windows = Gtk.Button(label="See all")
        self.all_windows.add_css_class("flat")
        self.all_windows.connect("clicked", lambda _: self.stack.set_visible_child_name("details"))
        self.open_windows.group.set_header_suffix(self.all_windows)
        self.status = label(css="caption")
        self.status.set_halign(Gtk.Align.START)
        self.status.add_css_class("context-status")
        body.append(self.status)

        details, body = page("Context details", "Signals behind the workload prediction")
        self.stack.add_titled_with_icon(details, "details", "Details", "view-list-symbolic")
        self.evidence = Rows(body, "Workload evidence")
        self.active = Rows(body, "Focus")
        self.inventory = Rows(body, "Window inventory")
        self.presence = Rows(body, "Desktop activity")
        self.resources = Rows(body, "System resources")
        self.resources.group.set_description("Whole-system counters · pressure is time spent waiting for a resource")
        self.busy = Rows(body, "Active processes")
        self.busy.group.set_description("Your most active processes · 100% CPU equals one core")
        self.players = Rows(body, "Media")
        self.compilers = Rows(body, "Compiler processes")
        self.sources = Rows(body, "Data sources")

    def update(self, snapshot, *, demo=False):
        predictions = predict(snapshot)
        primary = predictions[0]
        self.workload_icon.set_from_icon_name(primary.icon)
        self.workload_name.set_text(primary.name)
        if len(predictions) > 1:
            hint = "Also detected · " + ", ".join(item.name for item in predictions[1:4])
            if len(predictions) > 4:
                hint += f" + {len(predictions) - 4} more"
        else:
            hint = primary.basis
        self.workload_hint.set_text(safe_text(hint))

        focus = snapshot["focus"]
        recent = focus or snapshot["last_window"]
        self.recent.group.set_title("Focused window" if focus else "Last used window")
        focus_source = snapshot["sources"]["focus"]
        if recent:
            self.recent.show([window_row(recent)])
        else:
            detail = focus_source["detail"] if focus_source["status"] != "live" else ""
            self.recent.show([("No recent window available", detail, "window-new-symbolic")])

        windows = snapshot["open_windows"]
        self.all_windows.set_visible(windows is not None and len(windows) > 4)
        if windows is None:
            self.open_windows.group.set_description("")
            self.open_windows.show([("Window list unavailable", "", "view-list-symbolic")])
            self.inventory.show([("Window list unavailable", focus_source["detail"], "view-list-symbolic")])
        elif not windows:
            self.open_windows.group.set_description("")
            self.open_windows.show([("No open windows", "", "view-list-symbolic")])
            self.inventory.show([("No open windows", "", "view-list-symbolic")])
        else:
            count = len(windows)
            description = f"{count} window" if count == 1 else f"{count} windows"
            self.open_windows.group.set_description(description)
            self.open_windows.show(window_row(window) for window in windows[:4])
            self.inventory.show(window_row(window, detailed=True) for window in windows)

        statuses = {source["status"] for key, source in snapshot["sources"].items() if key not in {"presence", "api"}}
        if demo:
            status = "Demo data"
        elif "starting" in statuses:
            status = "Connecting"
        elif windows is None or "unavailable" in statuses:
            status = "Limited visibility"
        else:
            status = "Live"
        self.status.set_text(f"Read only · {status}" if snapshot["read_only"] else status)

        self.evidence.show(
            (item.name, f"{item.basis} · {item.evidence}", item.icon) for item in predictions
        )
        idle = snapshot["idle"]
        presence_title = "Input activity unavailable"
        presence_detail = snapshot["sources"]["presence"]["detail"]
        if idle is not None:
            presence_title = "Desktop idle" if idle else "Recent desktop input"
            presence_detail = (f"No input for at least {IDLE_MINUTES} minutes" if idle
                               else f"Input within the last {IDLE_MINUTES} minutes")
        self.presence.show([(presence_title, presence_detail, "alarm-symbolic")])
        self.resources.show(resource_rows(snapshot["system_load"]))
        active = []
        for process in snapshot["active_processes"][:8]:
            detail = [f"PID {process['pid']}", f"{process['cpu_percent']}% CPU"]
            if process["memory_bytes"]:
                detail.append(f"{process['memory_bytes'] / 1024**2:.0f} MiB resident")
            if process["workload"]:
                detail.append(process["workload"])
            active.append((process["name"], " · ".join(detail), "system-run-symbolic"))
        self.busy.show(active or [("No sampled processes above 3% CPU", "", "system-run-symbolic")])
        if focus:
            self.active.show([window_row(focus, detailed=True)])
        else:
            title = "No external window focused" if focus_source["status"] == "live" else "Focus unavailable"
            self.active.show([(title, "", "window-new-symbolic")])
        self.players.group.set_description(f"Fullscreen media · {verdict(snapshot['fullscreen_media'])}")
        self.players.show([
            (player["identity"] or player["name"],
             f"{player['playback']} · {player['name']}", "multimedia-player-symbolic")
            for player in snapshot["media"]
        ] or [("No media players reported", "", "multimedia-player-symbolic")])
        self.compilers.show([
            (worker["name"], f"PID {worker['pid']}", "system-run-symbolic")
            for worker in snapshot["compilers"]
        ] or [("No active compiler reported", "", "system-run-symbolic")])
        self.sources.group.set_description(safe_text(snapshot["session"]))
        sources = [
            (name, f"{source['status'].title()} · {source['detail']}", "preferences-system-symbolic")
            for name, source in (
                ("Windows", focus_source), ("Media", snapshot["sources"]["media"]),
                ("Processes", snapshot["sources"]["processes"]),
            )
        ]
        if snapshot["read_only"]:
            sources.insert(0, ("Read-only sandbox", "Host files read-only · network isolated · filtered D-Bus", "security-high-symbolic"))
        if api := snapshot["sources"].get("api"):
            sources.append(("Local API", f"{api['status'].title()} · {api['detail']}", "network-workgroup-symbolic"))
        self.sources.show(sources)


def demo_snapshot():
    return demo_store(inventory=True).snapshot()


class ContextApplication(Adw.Application):
    def __init__(self, args):
        flags = Gio.ApplicationFlags.NON_UNIQUE if args.demo else Gio.ApplicationFlags.DEFAULT_FLAGS
        super().__init__(application_id=APP_ID, flags=flags)
        self.args = args
        self.observer = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        display = Gdk.Display.get_default()
        Gtk.IconTheme.get_for_display(display).add_search_path(str(Path(__file__).with_name("icons")))
        Gtk.Window.set_default_icon_name(APP_ID)
        provider = Gtk.CssProvider()
        if hasattr(provider, "load_from_string"):
            provider.load_from_string(CSS)
        else:
            provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])

    def do_activate(self):
        window = self.get_active_window()
        if window is None:
            window = ContextWindow(self)
            if self.args.demo:
                window.update(demo_snapshot(), demo=True)
            else:
                window.update(session_store(self.args, inventory=True).snapshot())
                self.observer = Observer(self.args, window.update)
        window.present()

    def do_shutdown(self):
        if self.observer is not None:
            self.observer.close()
        Adw.Application.do_shutdown(self)


def run_gui(args):
    GLib.set_prgname(APP_ID)
    GLib.set_application_name(APP_NAME)
    return ContextApplication(args).run([])
