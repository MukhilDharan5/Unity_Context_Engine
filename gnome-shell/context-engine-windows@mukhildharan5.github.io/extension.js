import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const NAME = 'io.github.MukhilDharan5.UnityContextEngine.Windows';
const PATH = '/io/github/MukhilDharan5/UnityContextEngine/Windows';
const INTERFACE = '<node><interface name="' + NAME + '">' +
    '<method name="GetSnapshot"><arg type="s" direction="out"/></method>' +
    '<signal name="Changed"/>' +
    '</interface></node>';

export default class ContextEngineWindows extends Extension {
    enable() {
        this._windows = new Map();
        this._idleId = 0;
        this._tracker = Shell.WindowTracker.get_default();
        this._object = Gio.DBusExportedObject.wrapJSObject(INTERFACE, this);
        this._object.export(Gio.DBus.session, PATH);
        this._ownerId = Gio.bus_own_name_on_connection(
            Gio.DBus.session, NAME, Gio.BusNameOwnerFlags.NONE, null, null);
        this._displayIds = [
            global.display.connect('notify::focus-window', () => this._queueChanged()),
            global.display.connect('window-created', (_display, window) => {
                this._watchWindow(window);
                this._queueChanged();
            }),
        ];
        for (const window of global.display.get_tab_list(Meta.TabList.NORMAL_ALL, null))
            this._watchWindow(window);
    }

    disable() {
        for (const id of this._displayIds)
            global.display.disconnect(id);
        for (const window of this._windows.keys())
            this._unwatchWindow(window);
        if (this._idleId)
            GLib.Source.remove(this._idleId);
        Gio.bus_unown_name(this._ownerId);
        this._object.unexport();
        this._object = null;
        this._tracker = null;
        this._windows = null;
        this._idleId = 0;
    }

    _watchWindow(window) {
        if (this._windows.has(window) || window.is_override_redirect())
            return;
        const ids = ['title', 'fullscreen', 'wm-class', 'skip-taskbar'].map(prop =>
            window.connect('notify::' + prop, () => this._queueChanged()));
        ids.push(window.connect('unmanaged', () => {
            this._unwatchWindow(window);
            this._queueChanged();
        }));
        this._windows.set(window, ids);
    }

    _unwatchWindow(window) {
        for (const id of this._windows.get(window))
            window.disconnect(id);
        this._windows.delete(window);
    }

    _queueChanged() {
        if (this._idleId)
            return;
        this._idleId = GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
            this._idleId = 0;
            // Only send a notification. Titles are fetched on demand by the client.
            this._object.emit_signal('Changed', null);
            return GLib.SOURCE_REMOVE;
        });
    }

    GetSnapshot() {
        const display = global.display;
        const focus = display.get_focus_window();
        const windows = display.get_tab_list(Meta.TabList.NORMAL_ALL, null)
            .filter(window => !window.is_skip_taskbar() && !window.is_override_redirect())
            .map(window => {
                const app = this._tracker.get_window_app(window);
                const id = app?.get_id() || '';
                const appId = id.startsWith('window:') ? '' : id;
                return {
                    id: String(window.get_stable_sequence()),
                    app: appId || window.get_gtk_application_id() || window.get_wm_class() || '',
                    app_name: app?.get_name() || '',
                    title: window.get_title() || '',
                    pid: window.get_pid() || null,
                    fullscreen: window.is_fullscreen(),
                };
            });
        return JSON.stringify({
            focused: focus ? String(focus.get_stable_sequence()) : null,
            windows,
        });
    }
}
