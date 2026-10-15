// Quick Settings toggle for autobright (webcam auto brightness).
//
// State comes from the user systemd over D-Bus (signals, no polling) and from the
// daemon's status file (watched with inotify). Turning it on/off enables or disables
// the systemd user service, exactly like the settings window does.

import GObject from 'gi://GObject';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import {QuickMenuToggle, SystemIndicator} from 'resource:///org/gnome/shell/ui/quickSettings.js';

const UNIT = 'autobright.service';
const SYSTEMD = 'org.freedesktop.systemd1';
const SETTINGS_APP = 'io.github.autobright.Settings.desktop';
const ICON = 'display-brightness-symbolic';

function describeLight(lg) {
    if (lg < 1.5) return 'темно';
    if (lg < 2.2) return 'полумрак';
    if (lg < 3.2) return 'обычный свет';
    if (lg < 4.2) return 'светло';
    return 'очень светло';
}

function run(argv) {
    try {
        Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE);
    } catch (e) {
        logError(e, 'autobright');
    }
}

const AutoBrightnessToggle = GObject.registerClass(
class AutoBrightnessToggle extends QuickMenuToggle {
    _init() {
        super._init({title: 'Автояркость', iconName: ICON, toggleMode: false});
        this._active = false;
        this._status = {};
        this._unitPath = null;
        this._signalId = 0;
        this._cancellable = new Gio.Cancellable();

        this.menu.setHeader(ICON, 'Автояркость', 'Яркость экрана по свету с веб-камеры');
        this.menu.addAction('Проверить сейчас', () => this._signal('SIGUSR1'));
        this.menu.addAction('Тест камеры (20 секунд)', () => this._signal('SIGUSR2'));
        this.menu.addAction('Настройки…', () => {
            Main.panel.closeQuickSettings();
            Gio.DesktopAppInfo.new(SETTINGS_APP)?.launch([], null);
        });

        this.connect('clicked', () => {
            run(['systemctl', '--user', this._active ? 'disable' : 'enable', '--now', UNIT]);
        });

        this._watchService();
        this._watchStatus();
        this._sync();
    }

    _signal(sig) {
        run(['systemctl', '--user', 'kill', '-s', sig, '--kill-whom=main', UNIT]);
    }

    _watchService() {
        const bus = Gio.DBus.session;
        bus.call(SYSTEMD, '/org/freedesktop/systemd1', `${SYSTEMD}.Manager`, 'Subscribe',
            null, null, Gio.DBusCallFlags.NONE, -1, this._cancellable, (b, res) => {
                try { b.call_finish(res); } catch (e) { /* already subscribed or no systemd */ }
            });
        bus.call(SYSTEMD, '/org/freedesktop/systemd1', `${SYSTEMD}.Manager`, 'LoadUnit',
            new GLib.Variant('(s)', [UNIT]), new GLib.VariantType('(o)'),
            Gio.DBusCallFlags.NONE, -1, this._cancellable, (b, res) => {
                try {
                    [this._unitPath] = b.call_finish(res).deepUnpack();
                } catch (e) {
                    return;  // service not installed
                }
                this._signalId = bus.signal_subscribe(SYSTEMD, 'org.freedesktop.DBus.Properties',
                    'PropertiesChanged', this._unitPath, null, Gio.DBusSignalFlags.NONE,
                    () => this._readActiveState());
                this._readActiveState();
            });
    }

    _readActiveState() {
        if (!this._unitPath)
            return;
        Gio.DBus.session.call(SYSTEMD, this._unitPath, 'org.freedesktop.DBus.Properties', 'Get',
            new GLib.Variant('(ss)', [`${SYSTEMD}.Unit`, 'ActiveState']), null,
            Gio.DBusCallFlags.NONE, -1, this._cancellable, (b, res) => {
                try {
                    const state = b.call_finish(res).deepUnpack()[0].deepUnpack();
                    this._active = state === 'active' || state === 'activating';
                    this._sync();
                } catch (e) { /* cancelled on disable */ }
            });
    }

    _watchStatus() {
        const dir = Gio.File.new_for_path(GLib.build_filenamev([GLib.get_user_runtime_dir(), 'autobright']));
        this._statusFile = dir.get_child('status.json');
        try {
            this._monitor = dir.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, null);
            this._monitor.connect('changed', (_m, file, other) => {
                const names = [file?.get_basename(), other?.get_basename()];
                if (names.includes('status.json'))
                    this._readStatus();
            });
        } catch (e) {
            logError(e, 'autobright: cannot watch status');
        }
        this._readStatus();
    }

    _readStatus() {
        this._statusFile.load_contents_async(this._cancellable, (f, res) => {
            try {
                const [, bytes] = f.load_contents_finish(res);
                this._status = JSON.parse(new TextDecoder().decode(bytes));
            } catch (e) {
                this._status = {};
            }
            this._sync();
        });
    }

    _sync() {
        this.checked = this._active;
        const st = this._status;
        let sub;
        if (!this._active)
            sub = 'Выключено';
        else if (st.state === 'away')
            sub = 'Вы отошли';
        else if (st.state === 'locked')
            sub = 'Пауза';
        else if (st.state === 'nocamera')
            sub = 'Камера выключена';
        else if (typeof st.smoothed === 'number')
            sub = describeLight(st.smoothed);
        else
            sub = 'Включено';
        this.subtitle = sub;
    }

    destroy() {
        this._cancellable.cancel();
        if (this._signalId)
            Gio.DBus.session.signal_unsubscribe(this._signalId);
        this._monitor?.cancel();
        super.destroy();
    }
});

const AutoBrightnessIndicator = GObject.registerClass(
class AutoBrightnessIndicator extends SystemIndicator {
    _init() {
        super._init();
        this.quickSettingsItems.push(new AutoBrightnessToggle());
    }

    destroy() {
        this.quickSettingsItems.forEach(item => item.destroy());
        super.destroy();
    }
});

export default class AutoBrightnessExtension extends Extension {
    enable() {
        this._indicator = new AutoBrightnessIndicator();
        Main.panel.statusArea.quickSettings.addExternalIndicator(this._indicator);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
