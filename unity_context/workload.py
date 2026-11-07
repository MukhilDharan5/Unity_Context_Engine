"""Explainable workload predictions from window, playback, and compiler signals."""

from dataclasses import dataclass
import re

from .model import Window, app_key, game_evidence


@dataclass(frozen=True)
class Workload:
    name: str
    icon: str
    evidence: str
    basis: str


# Match complete app identifiers or their final desktop-ID component.
APP_GROUPS = (
    ("Coding", "applications-development-symbolic",
     "code code-insiders visual-studio-code vscodium codium cursor zed sublime-text "
     "gedit gnome-text-editor texteditor mousepad kate geany emacs neovide eclipse "
     "idea intellij-idea idea-community pycharm pycharm-community clion goland webstorm "
     "rubymine rustrover rider android-studio studio qtcreator unityhub unity godot unreal-editor"),
    ("Terminal work", "utilities-terminal-symbolic",
     "terminal gnome-terminal ptyxis console kgx konsole xterm alacritty kitty wezterm "
     "foot tilix ghostty terminator guake"),
    ("Browsing", "web-browser-symbolic",
     "firefox firefoxdeveloperedition librewolf zen zen-browser chromium chromium-browser "
     "google-chrome chrome brave-browser brave-browser-nightly vivaldi microsoft-edge "
     "opera epiphany floorp tor-browser"),
    ("Writing", "accessories-text-editor-symbolic",
     "libreoffice-writer writer abiword word onlyoffice-desktopeditors focuswriter typora"),
    ("Notes and planning", "accessories-text-editor-symbolic",
     "obsidian logseq joplin zim notes simplenote notion trilium triliumnext notion-app"),
    ("Spreadsheets", "x-office-spreadsheet-symbolic",
     "libreoffice-calc calc gnumeric excel"),
    ("Presentations", "x-office-presentation-symbolic",
     "libreoffice-impress impress powerpoint calligraplan"),
    ("Reading", "x-office-document-symbolic",
     "evince papers okular zathura calibre ebook-viewer foliate atril mupdf sioyek"),
    ("Messaging", "mail-unread-symbolic",
     "slack discord vesktop telegramdesktop telegram-desktop signal signal-desktop "
     "element element-desktop fractal neochat mattermost teams-for-linux hexchat"),
    ("Email", "mail-unread-symbolic",
     "thunderbird betterbird evolution geary mailspring kmail outlook"),
    ("Meetings", "camera-video-symbolic", "zoom zoom-us jitsi-meet webex teams"),
    ("Scheduling", "x-office-calendar-symbolic", "gnome-calendar calendar korganizer"),
    ("Music", "audio-x-generic-symbolic",
     "spotify rhythmbox lollypop amberol audacious strawberry clementine elisa "
     "tauon music gnome-music deadbeef cmus"),
    ("Watching video", "video-x-generic-symbolic",
     "mpv vlc totem showtime celluloid haruna smplayer kodi clapper videos"),
    ("Video editing", "video-x-generic-symbolic",
     "kdenlive shotcut openshot openshot-qt flowblade pitivi davinci-resolve resolve"),
    ("Audio production", "audio-x-generic-symbolic",
     "audacity ardour reaper bitwig-studio lmms qtractor ocenaudio"),
    ("Streaming and recording", "media-record-symbolic",
     "obs obs-studio simplescreenrecorder kooha"),
    ("Graphic design", "applications-graphics-symbolic",
     "inkscape krita gimp figma figma-linux pinta mypaint scribus"),
    ("Photo editing", "image-x-generic-symbolic",
     "darktable rawtherapee digikam shotwell gthumb"),
    ("3D modeling", "applications-graphics-symbolic", "blender maya houdini"),
    ("CAD and engineering", "applications-engineering-symbolic",
     "freecad librecad openscad kicad pcbnew eeschema solvespace"),
    ("Data analysis", "accessories-calculator-symbolic",
     "rstudio rstudio-bin jupyterlab jupyter-notebook julia matlab octave jamovi"),
    ("Research and references", "x-office-document-symbolic", "zotero jabref mendeleydesktop"),
    ("Technical documentation", "help-browser-symbolic", "devhelp zeal dash yelp"),
    ("AI assistance", "starred-symbolic", "chatgpt codex claude alpaca jan lm-studio lmstudio"),
    ("GIS and mapping", "mark-location-symbolic", "qgis qgis-bin grass grass-gis marble"),
    ("Finance and bookkeeping", "accessories-calculator-symbolic", "gnucash kmymoney money-manager-ex skrooge"),
    ("Mind mapping", "applications-graphics-symbolic", "freeplane freemind minder xmind"),
    ("Version control", "applications-development-symbolic", "gitg git-cola gitkraken sourcetree gitbutler"),
    ("Container operations", "computer-symbolic", "podman-desktop rancher-desktop lens openlens"),
    ("Profiling", "utilities-system-monitor-symbolic", "sysprof hotpot hotspot vtune-gui tracy-profiler"),
    ("Font design", "preferences-desktop-font-symbolic", "fontforge glyphs fontmatrix birdfont"),
    ("Publishing", "x-office-document-symbolic", "lyx texstudio texmaker texworks kile"),
    ("Password management", "dialog-password-symbolic", "keepassxc keepass2 bitwarden 1password"),
    ("File transfers and sync", "folder-remote-symbolic", "filezilla winscp transmission-gtk qbittorrent deluge"),
    ("Backups", "drive-harddisk-symbolic", "deja-dup deja-dup-preferences pika-backup vorta timeshift"),
    ("Simulation", "applications-engineering-symbolic", "paraview visit ngspice ltspice octave-qt"),
    ("Database work", "drive-harddisk-symbolic",
     "dbeaver pgadmin4 datagrip beekeeper-studio sqlitebrowser mysql-workbench"),
    ("Testing APIs", "network-workgroup-symbolic",
     "postman insomnia bruno hoppscotch httpie-desktop"),
    ("Debugging", "applications-development-symbolic",
     "nemiver ddd gdbgui edb debugger"),
    ("File management", "folder-symbolic",
     "nautilus files dolphin thunar nemo caja pcmanfm pcmanfm-qt ranger"),
    ("System maintenance", "preferences-system-symbolic",
     "gnome-control-center settings systemsettings systemmonitor gnome-system-monitor "
     "baobab disks gnome-disks gparted missioncenter software gnome-software discover"),
    ("Networking", "network-workgroup-symbolic", "wireshark zenmap ettercap"),
    ("Remote desktop", "computer-symbolic", "remmina connections krdc rustdesk anydesk"),
    ("Virtualization", "computer-symbolic",
     "virtualbox virtualboxvm virt-manager virt-viewer boxes vmware vmplayer qemu-system-x86_64"),
    ("Game library", "input-gaming-symbolic", "steam heroic lutris bottles prism-launcher"),
)

APPS = {
    alias: (name, icon)
    for name, icon, aliases in APP_GROUPS
    for alias in aliases.split()
}

# Apply these only to known browsers. Titles can hint at an activity without
# revealing page contents or requiring an extension inside the browser.
BROWSER_HINTS = (
    ("Meetings", "camera-video-symbolic", r"google meet|zoom workplace|microsoft teams|jitsi meet"),
    ("Spreadsheets", "x-office-spreadsheet-symbolic", r"google sheets|excel online"),
    ("Presentations", "x-office-presentation-symbolic", r"google slides|powerpoint online"),
    ("Writing", "accessories-text-editor-symbolic", r"google docs|word online|overleaf"),
    ("Email", "mail-unread-symbolic", r"gmail|outlook mail|proton mail"),
    ("Scheduling", "x-office-calendar-symbolic", r"google calendar|outlook calendar"),
    ("Messaging", "mail-unread-symbolic", r"slack|discord|whatsapp"),
    ("Graphic design", "applications-graphics-symbolic", r"figma|canva|photopea"),
    ("Whiteboarding", "applications-graphics-symbolic", r"miro|excalidraw|figjam|tldraw"),
    ("Data analysis", "accessories-calculator-symbolic", r"jupyterlab|google colab|kaggle"),
    ("CI and releases", "system-run-symbolic", r"github actions|gitlab ci/cd|jenkins|circleci|buildkite|travis ci"),
    ("Version control", "applications-development-symbolic", r"github|gitlab|bitbucket|codeberg"),
    ("Technical documentation", "help-browser-symbolic", r"mdn web docs|read the docs|stackoverflow|stack overflow|man7\.org"),
    ("Coding", "applications-development-symbolic", r"leetcode|codesandbox|stackblitz|replit"),
    ("Notes and planning", "accessories-text-editor-symbolic", r"notion|obsidian publish"),
    ("Project management", "x-office-calendar-symbolic", r"trello|linear|jira|asana|clickup|basecamp"),
    ("AI assistance", "starred-symbolic", r"chatgpt|claude|google gemini|github copilot|perplexity"),
    ("Research and references", "x-office-document-symbolic", r"google scholar|arxiv|pubmed|semantic scholar|zotero"),
    ("Online learning", "x-office-document-symbolic", r"coursera|edx|moodle|khan academy|udemy"),
    ("Monitoring dashboards", "utilities-system-monitor-symbolic", r"grafana|kibana|datadog|new relic|prometheus"),
    ("Customer support", "mail-unread-symbolic", r"zendesk|freshdesk|servicenow|intercom"),
    ("GIS and mapping", "mark-location-symbolic", r"google maps|openstreetmap|arcgis online"),
    ("Finance and bookkeeping", "accessories-calculator-symbolic", r"quickbooks|xero|zoho books"),
    ("File transfers and sync", "folder-remote-symbolic", r"google drive|dropbox|onedrive|nextcloud"),
    ("Publishing", "x-office-document-symbolic", r"wordpress|ghost admin|substack"),
    ("Music", "audio-x-generic-symbolic", r"youtube music|spotify|soundcloud|bandcamp"),
    ("Watching video", "video-x-generic-symbolic", r"youtube|netflix|twitch|prime video|vimeo|disney\+"),
)
TERMINAL_HINTS = (
    ("Coding", "applications-development-symbolic", r"nvim|neovim|vim|emacs|helix"),
    ("Running tests", "system-run-symbolic", r"pytest|ctest|cargo test|go test|npm test|vitest"),
    ("Debugging", "applications-development-symbolic", r"gdb|lldb"),
    ("Remote shell", "network-workgroup-symbolic", r"ssh|mosh"),
    ("Container operations", "computer-symbolic", r"kubectl|k9s|podman|docker compose"),
    ("System maintenance", "preferences-system-symbolic", r"htop|btop|systemctl|journalctl"),
    ("Version control", "applications-development-symbolic", r"git (?:log|status|rebase|commit|diff|bisect)"),
    ("File transfers and sync", "folder-remote-symbolic", r"rsync|rclone|scp|sftp"),
    ("Backups", "drive-harddisk-symbolic", r"restic|borg|kopia"),
    ("Database work", "drive-harddisk-symbolic", r"psql|sqlite3|mysql|redis-cli|mongosh"),
    ("Profiling", "utilities-system-monitor-symbolic", r"perf (?:top|record|report)|valgrind"),
)
TITLE_RULES = {
    category: tuple(
        (name, icon, re.compile(r"(?<!\w)(?:" + pattern + r")(?!\w)" if category == "Terminal work" else pattern,
                                re.IGNORECASE))
        for name, icon, pattern in hints
    )
    for category, hints in (("Browsing", BROWSER_HINTS), ("Terminal work", TERMINAL_HINTS))
}
ICONS = {name: icon for name, icon, _ in (*APP_GROUPS, *BROWSER_HINTS, *TERMINAL_HINTS)}


def app_category(*identifiers: str) -> tuple[str, str] | None:
    for identifier in identifiers:
        key = app_key(identifier).strip()
        match = APPS.get(key) or APPS.get(key.rsplit(".", 1)[-1])
        if match:
            return match
    return None


def window_workload(window: Window, game_apps: tuple[str, ...], last_used: bool) -> Workload:
    evidence = game_evidence(window, game_apps)
    basis = "Last used window" if last_used else "Focused window"
    app = window.app_name or window.app or "Unknown application"
    if evidence:
        return Workload("Gaming", "input-gaming-symbolic", f"{app} · {evidence}", basis)
    match = app_category(window.app, window.app_name)
    if match is None:
        return Workload("Unclassified", "view-grid-symbolic", f"{app} has no workload match", basis)
    name, icon = match
    segments = []
    if name == "Browsing":
        # Browser titles use common words too. Match a product title segment.
        segments = [re.sub(r"^\(\d+\)\s*", "", part.strip())
                    for part in re.split(r"\s+[-–—|·]\s+", window.title)]
    for hint_name, hint_icon, pattern in TITLE_RULES.get(name, ()):
        if name == "Browsing":
            hint = next((match for part in segments if (match := pattern.fullmatch(part))), None)
        else:
            hint = pattern.search(window.title)
        if hint:
            return Workload(hint_name, hint_icon, f"{app} title mentions {hint[0]}", f"{basis} title hint")
    return Workload(name, icon, app, basis)


def process_workloads(processes: list[dict]) -> list[Workload]:
    groups = {}
    for process in processes:
        name = process["workload"]
        if name and name != "Compiling":
            groups.setdefault(name, []).append(process)

    predictions = []
    for name, workers in groups.items():
        names = ", ".join(dict.fromkeys(worker["name"] for worker in workers[:3]))
        cores = sum(worker["cpu_percent"] for worker in workers) / 100
        evidence = f"{names} · {len(workers)} active · {cores:.2f} CPU cores"
        predictions.append(Workload(name, ICONS.get(name, "system-run-symbolic"), evidence, "Active process CPU"))
    return predictions


def predict(snapshot: dict) -> tuple[Workload, ...]:
    predictions = []
    focus = snapshot.get("focus")
    recent = snapshot.get("last_window")
    windows = snapshot.get("open_windows")
    if not focus and recent and snapshot["sources"]["focus"]["status"] == "live":
        if windows is None or any(w["id"] == recent["id"] for w in windows):
            focus = recent
    if focus and snapshot.get("idle") is not True:
        predictions.append(window_workload(
            Window(**focus), snapshot.get("game_apps", ()), not snapshot.get("focus"),
        ))

    if snapshot["compiling"]:
        names = ", ".join(sorted({worker["name"] for worker in snapshot["compilers"]}))
        measured = [worker for worker in snapshot.get("active_processes", []) if worker["workload"] == "Compiling"]
        if measured:
            cores = sum(worker["cpu_percent"] for worker in measured) / 100
            names = f"{names} · {len(measured)} with CPU activity · {cores:.2f} CPU cores"
        build = Workload("Compiling", "system-run-symbolic", names, "Active compiler processes")
        if predictions and predictions[0].name in {"Coding", "Terminal work", "Unclassified"}:
            predictions.insert(0, build)
        else:
            predictions.append(build)

    for player in snapshot["media"]:
        if player["playback"] != "Playing":
            continue
        match = app_category(player["desktop_entry"], player["identity"])
        if match and match[0] in {"Music", "Watching video"}:
            name, icon = match
        elif player["name"] in snapshot["fullscreen_media_players"]:
            name, icon = "Fullscreen media", "video-x-generic-symbolic"
        else:
            name, icon = "Media playback", "multimedia-player-symbolic"
        identity = player["identity"] or player["name"]
        predictions.append(Workload(name, icon, identity, "Playing media"))

    predictions.extend(process_workloads(snapshot.get("active_processes", [])))
    if len(predictions) > 1 and predictions[0].name == "Unclassified":
        predictions.pop(0)
    if not predictions:
        if snapshot.get("idle") is True:
            return (Workload("Desktop idle", "alarm-symbolic", "GNOME reports no recent input", "Idle threshold"),)
        return (Workload("No clear workload", "view-grid-symbolic",
                         "Waiting for a window, media player, or compiler signal", "No matching signal"),)
    unique = {}
    for prediction in predictions:
        prior = unique.get(prediction.name)
        if prior is None:
            unique[prediction.name] = prediction
        else:
            basis = prior.basis if prior.basis == prediction.basis else f"{prior.basis} + {prediction.basis}"
            evidence = f"{prior.evidence} · {prediction.evidence}"
            unique[prediction.name] = Workload(prior.name, prior.icon, evidence, basis)
    return tuple(unique.values())
