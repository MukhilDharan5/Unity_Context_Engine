# Unity Context

A minimal, read-only Linux context TUI: focused application/window, fullscreen
media, focused games, and compilation activity. Python 3.11+, curses, and one
small Python dependency (`dbus-next`). No service, root privileges, telemetry,
history files, screenshots, input hooks, or desktop settings changes.

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/unity-context
```

You can also run `python3 -m unity_context` directly from this directory.
Media discovery requires `dbus-next`; everything else works without it. Some
distributions package curses separately as `python3-curses`.

Press **q** to quit, **j/k** or arrow keys to scroll, **Page Up/Down** to page,
and **g** to return to the top. The view redraws on context, input, and resize
events; there is no refresh timer or animated dashboard.

```text
UNITY CONTEXT   /   READ ONLY
Sway / wayland

Fullscreen media + Compiling

FOCUS
  App         mpv
  Window      A quiet evening
  Fullscreen  yes

MEDIA
  mpv  /  Playing
  Fullscreen media  yes

ACTIVITY
  Game        no matching app ID
  Compiling   rustc (5678)

SOURCES
  focus: live / i3/Sway IPC events
  media: live / MPRIS D-Bus property and player lifecycle signals
  processes: live / /proc sampling every 3s; current user only
```

## Detection and overhead

| Signal | How it is collected | Limitations |
| --- | --- | --- |
| Focus and fullscreen on Sway/i3 | Native IPC subscription, initial tree query and queries after relevant events | Requires `SWAYSOCK` or `I3SOCK` in the launch environment |
| Focus and fullscreen on Hyprland | Native event socket and read-only `activewindow` queries | Requires the current session's socket environment |
| Focus and fullscreen on X11 | EWMH property subscriptions through `xprop -spy` | Needs `xprop`; at most two idle helper processes |
| Media | MPRIS discovery, `PropertiesChanged`, and player start/exit signals | Apps must expose MPRIS; no playback-position or metadata polling |
| Compiling | Current user's `/proc` process names, state, start time and CPU ticks every 3 seconds | Heuristic; short builds between samples can be missed, names are limited by Linux `comm`, other users are excluded |
| Game focused | `steam_app_<number>` window app IDs or exact `--game-app` IDs | Heuristic; no universal game flag exists; launchers, Wine, fullscreen, or high CPU alone do not imply a game |

**GNOME/KDE on Wayland:** the built-in focus backends do not support these
compositors. Focus, window fullscreen state, and focused game detection show
unavailable/unknown. Media and compilation detection still work. The program
does not install extensions, enable accessibility services, use Shell Eval,
or change the session to bypass this limitation. Automatic detection never
uses X11 as a substitute for native Wayland focus. Explicit `--focus x11` on
Wayland exposes only XWayland windows, clearly labeled as partial.

Fullscreen media requires a playing MPRIS player that reports fullscreen, or
a fullscreen focused window matching the player's process ID or exact desktop
entry. A browser playing background audio does not make an unrelated fullscreen
app count as media. `null`/`unknown` means the evidence is unavailable. MPRIS
cannot reliably distinguish audio from video; fullscreen media is an inference,
and a player's self-reported fullscreen state does not prove that it is visible
or on the active workspace.

Compilation requires a recognized compiler that is runnable or consumed CPU
since the previous sample. Sleeping build launchers and language servers do not
count. Signals are independent, so media and compilation can appear together.
Inaccessible and exited processes are skipped. `--once` can miss a sleeping
compiler because it has no previous CPU sample. A negative game result means no
matching app ID, not proof that the app is not a game.

While connected, focus and media have no polling loop. Disconnects clear stale
state and retry with a capped backoff. Process scans yield between batches of
64 processes to keep the UI responsive without worker threads.
`--no-processes` removes the only periodic device scan. No CPU or memory usage
target is claimed; overhead depends on process count and event frequency.

## Useful options

```bash
python3 -m unity_context --demo                 # sample TUI; no collectors
python3 -m unity_context --once                 # one JSON snapshot
python3 -m unity_context --json                 # JSON lines on changes
python3 -m unity_context --no-processes         # event sources only
python3 -m unity_context --process-interval 10  # slower compilation sampling
python3 -m unity_context --game-app my-game --game-app org.example.Game
python3 -m unity_context --focus none --no-media # compilation only
```

JSON output contains window titles, so treat it as personal data if you redirect
or share it. The program stores nothing itself. It does not read process command
lines, environment blocks, files being edited, media titles, or browser URLs.
Window titles can still reveal document or website names. Terminal control
characters are sanitized in the TUI and escaped in JSON.

One-shot JSON waits up to four seconds for initial collector discovery. Each
source reports `starting`, `live`, `disabled`, or `unavailable`, with an explanation.
Run inside the desktop session as your normal user; SSH, containers, and
sandboxes may not have permission to access the display or session bus.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m unity_context --demo --once
# Optional local compositor fixtures + private D-Bus service (needs dbus-daemon):
UNITY_CONTEXT_TEST_IPC=1 UNITY_CONTEXT_TEST_BUS=1 dbus-run-session -- \
  python3 -m unittest discover -s tests -v
```

Protocol references: [MPRIS](https://specifications.freedesktop.org/mpris/latest/),
[i3 IPC](https://i3wm.org/docs/ipc.html),
[Hyprland IPC](https://wiki.hypr.land/IPC/),
[EWMH](https://specifications.freedesktop.org/wm/latest-single/), and
[/proc](https://www.kernel.org/doc/html/latest/filesystems/proc.html).
