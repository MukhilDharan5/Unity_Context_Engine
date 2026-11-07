# Unity_Context_Engine

**Context Engine** is a read-only Linux desktop app built with GTK 4 and Libadwaita. **Overview** shows
the predicted workload, the focused or last used window, and a preview of open
windows. **Details** shows the full window list, the evidence behind each
prediction, recent input status, CPU and memory measurements, resource pressure,
active processes, media players, and collector status. Context
Engine's own windows are excluded.

```bash
python3 -m unity_context
python3 -m unity_context --demo
```

The desktop app needs Python 3.11+, PyGObject, GTK 4, and Libadwaita 1.4 or newer.
The launcher also needs `bubblewrap`, user namespaces, and `xdg-dbus-proxy`.
Use your distribution's Python with the GTK and Libadwaita introspection packages.
On Fedora these are `python3-gobject`, `gtk4`, and `libadwaita`. For media and
GNOME Shell access, install `dbus-next`. You can install the project into a
virtual environment that can use the system GTK bindings.

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e .
.venv/bin/unity-context
```

## Window access

| Session | Source |
| --- | --- |
| GNOME Wayland | Optional Context Engine Windows extension |
| Sway / i3 | Native IPC events and window tree |
| Hyprland | Native IPC events and read-only window queries |
| X11 | EWMH via `xprop` (focus and window list) |
| Other Wayland desktops | Window information unavailable |

On X11, background window titles are sampled when windows first appear and
refreshed when they gain focus. Sway/i3 and Hyprland require their session socket
environment variables. Automatic detection uses native Wayland sources. An
explicit `--focus x11` on Wayland can only see XWayland windows.

### GNOME Wayland

GNOME Shell can supply native window information through the optional extension
in this repository. Build and install it from the project directory.

```bash
extension=context-engine-windows@mukhildharan5.github.io
gnome-extensions pack --force --out-dir /tmp "gnome-shell/$extension"
gnome-extensions install --force "/tmp/$extension.shell-extension.zip"
gnome-extensions enable "$extension"
```

If GNOME Shell does not recognize a newly installed extension, log out and back
in, then enable it. The app picks up the extension when it appears on the session
bus. You can disable it with `gnome-extensions disable "$extension"`. The
extension publishes a read-only window snapshot to local clients on your session
bus. Window titles can include document and website names. With the extension
disabled, media and compilation still work and window access is marked
unavailable.

## How predictions work

Context Engine recognizes around 70 workload categories from application IDs,
browser and terminal title hints, media playback, and measured process activity.
These include coding, code hosting, CI, project management, online learning,
research, design, meetings, bookkeeping, GIS, media editing, and gaming. Browser
hints match product name segments of the title, so ordinary titles containing
words such as “linear” do not suggest the project management app.

The focused window usually drives the primary prediction. When Context Engine
takes focus, the last observed external window can drive it while that window is
still open. Supported compositors can provide a recent window at startup.
Recognized compiler workers can make **Compiling** primary during coding or
terminal work. Media and compiler signals can also appear as concurrent
workloads. Active workers can also suggest tests, rendering, media processing,
backups, file transfers, databases, containers, virtual machines, local AI, and
other jobs. Details shows the worker names and measured CPU for these predictions.
Unknown apps and missing signals are labeled explicitly.

On GNOME, an idle threshold event marks a session without recent input after
three minutes. A window by itself stops driving the prediction while idle.
Playback and measured background jobs can still show up. Passive viewing can
continue while the desktop has no input.

Media uses MPRIS playback and player lifecycle events. Fullscreen media requires
a playing player that reports fullscreen or matches the focused fullscreen
window by process ID or desktop entry. Game detection uses Steam game IDs and
exact `--game-app` matches. Compiler detection samples the current user's
`/proc` process names and CPU ticks every three seconds. The same pass estimates
CPU used by the current user's active processes and reads whole-system CPU,
available memory, and 10-second resource pressure averages. A process's CPU is
shown relative to one core. Resident memory can include shared pages. Short
jobs can fall between samples.

Windows, media, and GNOME idle status update from events. The app stores no
history on disk. Use `--no-processes` to disable the periodic sampling.

## Read-only access

Context Engine runs as a regular user in a Bubblewrap sandbox. The app has zero
Linux capabilities, cannot gain privileges on exec, and sees read-only host
mounts, a private home and temporary directory, and an isolated network and
process namespace. It reads current-user process counters from the mounted
host procfs. Its launcher refuses to run the observer if the sandbox cannot
start.

The GNOME session bus is filtered to window snapshots, idle observation and
subscriptions, media property reads, appearance settings reads, and Context
Engine's own activation and local API, Unity device reads, and the Automation Engine context sink. Media queries read playback status, player identity, desktop
entry, and fullscreen state. Sway/i3, Hyprland, and X11 use their local sockets
and the app's read-only query code. The bundled GNOME extension runs within
the user's Shell and exports its window snapshot there.

## Local read-only API

Context Engine exports a D-Bus API while the GUI is open. Use
`unity-context --serve` to collect and serve data without a window, or
`unity-context --no-api` to open the GUI with sharing disabled. Use one API
provider at a time. A second provider leaves the first one in place. The API
ends when its provider exits. For integration testing, `--serve --demo`
publishes sample data without starting the collectors.

Clients on the same D-Bus session can read the data, including window titles.
The API uses the session bus only and exposes no control methods. In a sandboxed
client, allow access to the specific service name below.

| D-Bus endpoint | Value |
| --- | --- |
| Bus name and interface | `io.github.MukhilDharan5.UnityContextEngine.Context1` |
| Object path | `/io/github/MukhilDharan5/UnityContextEngine/Context1` |

| Member | Signature | Result |
| --- | --- | --- |
| `GetSnapshot` | `→ s` | JSON with the complete current snapshot |
| `GetFields` | `as → s` | JSON with only the requested data fields. Unknown fields return `InvalidArgs`. |
| `ListFields` | `→ as` | Available top-level field names |
| `SnapshotUpdated` signal | `s` | Complete JSON snapshot pushed on each change |
| `SnapshotChanged` signal | `t` | New revision number. Fetch the data with a method call. |

Each JSON reply contains `api_version` (currently 1), a `revision`, and a
`data` object. Revisions increase as data changes and restart with each
provider. Data includes focus, recent and open windows, media and fullscreen
state, game detection, compilers, active processes, CPU and memory pressure,
idle state, source status, and ranked `workloads` with their evidence. Missing
measurements use JSON `null`; consult `sources` for the collector status.
Requests read the latest snapshot without rerunning the collectors.
The `workload`, `workload_names`, and `media_playing` fields are convenient
condition values for Automation Engine. The richer evidence remains in
`workloads` and `media`.

### Automation Engine integration

Context Engine automatically connects to Automation Engine's provider-authenticated
`ContextSink1` on the same session bus. It negotiates the fields needed by saved
rules and publishes a complete selected snapshot immediately, then sends updates
when those values change. `RequirementsChanged` picks up added or edited rules.
Automation Engine never polls Context Engine or calls `GetSnapshot`. The
`SnapshotUpdated` signal remains available to other read-only clients.

Trigger conditions, mode enter/leave transitions, and `wait until` steps consume
pushed facts. Updates are ordered, preserving brief true/false changes. A bounded
queue resynchronizes to current state if an unresponsive receiver falls more
than 256 updates behind. Provider loss clears stale data immediately. Reconnects
negotiate fresh fields; cached lease renewals every 22.5 seconds keep unchanged
readings live, and missing renewals expire after 45 seconds.

Context Engine supplies local minute-boundary clock events, installed application
metadata (inotify), desktop workloads, windows, media, idle status, and Unity
device readings and action pick lists. Unity observation requests only `read` as
**Unity Context Engine**. Its `unity` source, and Automation Engine's Devices page,
show the exact `unityctl authorize :1.N` command when approval is required. The
grant belongs to that running connection. Device readings cover every current
Automation Engine trigger, including battery, transport/network, connectivity,
audio, media, screen sharing, and call state. Unavailable readings remain unknown.
Clipboard contents, SMS bodies, notification text, contact lists, and media
artwork are excluded; action pick lists are bounded.

Both engines must run in the same desktop session. Use `unity-context --serve`
for collection without a window. The sandbox permits only the required Unity
read methods and the two context sink calls; it has no device control methods.
Device state is refreshed on Unity's change signals. CPU and process counters
still use the configured sampling interval (default three seconds); their changes
are pushed immediately after each sample. Use `--no-processes` to disable that
sampling.

```bash
service=io.github.MukhilDharan5.UnityContextEngine.Context1
path=/io/github/MukhilDharan5/UnityContextEngine/Context1
gdbus call --session --dest "$service" --object-path "$path" \
  --method "$service.GetSnapshot"
gdbus call --session --dest "$service" --object-path "$path" \
  --method "$service.GetFields" "['workloads', 'system_load', 'focus']"
gdbus monitor --session --dest "$service" --object-path "$path"
```

## Terminal and JSON

```bash
python3 -m unity_context --tui                  # q quits; j/k scroll
python3 -m unity_context --once                 # one JSON snapshot
python3 -m unity_context --json                 # JSON on changes
python3 -m unity_context --process-interval 10  # slower compiler sampling
python3 -m unity_context --game-app org.example.Game
python3 -m unity_context --focus none --no-media
```

The terminal and JSON modes can run without GTK. JSON can contain a window
title. One-shot JSON allows up to four seconds for initial discovery. Run inside
the desktop session as your normal user to access the display and session bus.

## Development

```bash
python3 -m unittest discover -s tests -v
UNITY_CONTEXT_TEST_IPC=1 UNITY_CONTEXT_TEST_BUS=1 dbus-run-session -- \
  python3 -m unittest discover -s tests -v
# Include the cross-engine contract and push-trigger tests:
UNITY_CONTEXT_TEST_BUS=1 UNITY_AUTOMATION_SOURCE=../Unity_Automation_Engine \
  dbus-run-session -- python3 -m unittest discover -s tests -v
```

[Libadwaita](https://gnome.pages.gitlab.gnome.org/libadwaita/doc/main/) ·
[GNOME app icons](https://developer.gnome.org/hig/guidelines/app-icons.html) ·
[GNOME Shell extensions](https://gjs.guide/extensions/) ·
[Bubblewrap](https://github.com/containers/bubblewrap) ·
[D-Bus proxy](https://github.com/flatpak/xdg-dbus-proxy) ·
[Linux pressure counters](https://docs.kernel.org/accounting/psi.html) ·
[MPRIS](https://specifications.freedesktop.org/mpris/latest/) ·
[i3 IPC](https://i3wm.org/docs/ipc.html) ·
[Hyprland IPC](https://wiki.hypr.land/IPC/) ·
[EWMH](https://specifications.freedesktop.org/wm/latest-single/)
