#!/usr/bin/env python3
"""Settings window for autobright (GTK 4 + libadwaita).

Shows what the daemon sees right now, lets you shape the brightness curve by
dragging points, and edits every option. Changes are saved to the config file
and the running daemon is told to reload (SIGHUP) — no restart, no terminal.
"""

import json
import math
import os
import signal
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gsk, Gtk  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import autobright as ab  # noqa: E402
import v4l2cam as v  # noqa: E402

APP_ID = "io.github.autobright.Settings"
UNIT = "autobright.service"
X_MIN, X_MAX = 0.0, 6.0   # log10(ambient) range shown on the graph


_unit_path = None


def service_state():
    """(installed, active, main_pid) — straight from the user systemd over D-Bus."""
    global _unit_path
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def call(path, iface, method, args):
        return bus.call_sync("org.freedesktop.systemd1", path, iface, method, args, None,
                             Gio.DBusCallFlags.NONE, 2000, None).unpack()

    def prop(iface, name):
        return call(_unit_path, "org.freedesktop.DBus.Properties", "Get",
                    GLib.Variant("(ss)", (iface, name)))[0]
    try:
        if _unit_path is None:
            _unit_path = call("/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager",
                              "LoadUnit", GLib.Variant("(s)", (UNIT,)))[0]
        return (prop("org.freedesktop.systemd1.Unit", "LoadState") == "loaded",
                prop("org.freedesktop.systemd1.Unit", "ActiveState") in ("active", "activating"),
                prop("org.freedesktop.systemd1.Service", "MainPID"))
    except GLib.Error:
        _unit_path = None
        return False, False, 0


def read_status():
    try:
        with open(ab.STATUS_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def backlight_fraction():
    try:
        base = "/sys/class/backlight/" + sorted(os.listdir("/sys/class/backlight"))[0]
        return int(open(base + "/brightness").read()) / int(open(base + "/max_brightness").read())
    except (OSError, IndexError, ValueError, ZeroDivisionError):
        return None


def list_cameras():
    out = []
    for name in sorted(os.listdir("/dev")):
        if not name.startswith("video"):
            continue
        path = "/dev/" + name
        try:
            with v.Camera(path) as cam:
                if cam.is_capture_device():
                    out.append((path, cam.capability().card.decode(errors="replace")))
        except (OSError, v.CameraBusy):
            pass
    return out


def ago(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} с"
    return f"{seconds // 60} мин {seconds % 60} с" if seconds < 3600 else f"{seconds // 3600} ч"


def describe_light(lg):
    for limit, word in ((1.5, "темно"), (2.2, "полумрак"), (3.2, "обычное освещение"),
                        (4.2, "светло"), (99, "очень светло")):
        if lg < limit:
            return word


# --- curve graph ---------------------------------------------------------------

def _rgba(c, alpha):
    out = Gdk.RGBA()
    out.red, out.green, out.blue, out.alpha = c.red, c.green, c.blue, alpha
    return out


def _pt(x, y):
    return Graphene.Point().init(x, y)


def _rect(x, y, w, h):
    return Graphene.Rect().init(x, y, w, h)


class CurveGraph(Gtk.Widget):
    """Draggable piecewise-linear curve with the current reading marked."""
    PAD_L, PAD_R, PAD_T, PAD_B = 44, 14, 14, 30
    HIT = 14  # px

    def __init__(self, on_changed):
        super().__init__()
        self.points = []
        self.current = None        # (log, target) or None
        self.offset = 0.0          # GNOME slider offset: drawn values = stored + offset
        self.on_changed = on_changed
        self._drag = None
        self.set_size_request(-1, 240)
        self.set_hexpand(True)
        self.add_css_class("caption")  # small font for axis labels

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.add_controller(drag)

        click = Gtk.GestureClick(button=0)
        click.connect("pressed", self._pressed)
        self.add_controller(click)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda _c, x, y: self.set_cursor_from_name(
            "grab" if self._hit(x, y) is not None else "default"))
        self.add_controller(motion)

    # coordinates
    def _box(self):
        w, h = self.get_width(), self.get_height()
        return self.PAD_L, self.PAD_T, w - self.PAD_L - self.PAD_R, h - self.PAD_T - self.PAD_B

    def _to_px(self, x, y):
        bx, by, bw, bh = self._box()
        return bx + (x - X_MIN) / (X_MAX - X_MIN) * bw, by + (1 - y) * bh

    def _to_data(self, px, py):
        bx, by, bw, bh = self._box()
        return X_MIN + (px - bx) / bw * (X_MAX - X_MIN), 1 - (py - by) / bh

    def _shown(self, y):
        return min(max(y + self.offset, 0.0), 1.0)

    def _stored(self, y):
        return round(min(max(y - self.offset, 0.0), 1.0), 3)

    def _hit(self, px, py):
        best, best_d = None, self.HIT
        for i, (x, y) in enumerate(self.points):
            qx, qy = self._to_px(x, self._shown(y))
            d = math.hypot(qx - px, qy - py)
            if d <= best_d:
                best, best_d = i, d
        return best

    # interaction
    def _drag_begin(self, gesture, x, y):
        i = self._hit(x, y)
        if i is None:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        self._drag = (i, x, y)
        self.set_cursor_from_name("grabbing")

    def _drag_update(self, _g, dx, dy):
        if not self._drag:
            return
        i, sx, sy = self._drag
        x, y = self._to_data(sx + dx, sy + dy)
        lo = self.points[i - 1][0] + 0.05 if i > 0 else X_MIN
        hi = self.points[i + 1][0] - 0.05 if i + 1 < len(self.points) else X_MAX
        self.points[i] = (round(min(max(x, lo), hi), 2), self._stored(y))
        self.queue_draw()

    def _drag_end(self, *_):
        if self._drag:
            self._drag = None
            self.set_cursor_from_name("grab")
            self.on_changed()

    def _pressed(self, gesture, n_press, x, y):
        button = gesture.get_current_button()
        i = self._hit(x, y)
        if button == Gdk.BUTTON_SECONDARY and i is not None and len(self.points) > 2:
            del self.points[i]
        elif button == Gdk.BUTTON_PRIMARY and n_press == 2 and i is None:
            dx, dy = self._to_data(x, y)
            if not X_MIN <= dx <= X_MAX:
                return
            self.points.append((round(dx, 2), self._stored(dy)))
            self.points.sort()
        else:
            return
        self.queue_draw()
        self.on_changed()

    # drawing (GtkSnapshot + Gsk: no cairo bindings needed)
    def _text(self, snap, text, x, y, color, align=0.0, valign=0.5):
        layout = self.create_pango_layout(text)
        w, h = layout.get_pixel_size()
        snap.save()
        snap.translate(_pt(round(x - w * align), round(y - h * valign)))
        snap.append_layout(layout, color)
        snap.restore()
        return w

    def do_snapshot(self, snap):
        fg = self.get_color()
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        white = Gdk.RGBA()
        white.parse("white")
        bx, by, bw, bh = self._box()
        if bw <= 0 or bh <= 0:
            return

        # grid + labels
        for pct in (0, 25, 50, 75, 100):
            _, py = self._to_px(0, pct / 100)
            snap.append_color(_rgba(fg, 0.10), _rect(bx, round(py), bw, 1))
            self._text(snap, f"{pct}%", bx - 6, py, _rgba(fg, 0.55), align=1.0)
        for xv in range(int(X_MIN), int(X_MAX) + 1):
            px, _ = self._to_px(xv, 0)
            snap.append_color(_rgba(fg, 0.06), _rect(round(px), by, 1, bh))
        for text, align in (("темно", 0.0), ("освещённость →", 0.5), ("светло", 1.0)):
            self._text(snap, text, bx + bw * align, by + bh + 16, _rgba(fg, 0.55), align=align)

        if not self.points:
            return

        # filled area + curve
        shown = [(x, self._shown(y)) for x, y in self.points]
        pts = [(X_MIN, shown[0][1]), *shown, (X_MAX, shown[-1][1])]
        b = Gsk.PathBuilder.new()
        b.move_to(*self._to_px(X_MIN, 0))
        for x, y in pts:
            b.line_to(*self._to_px(x, y))
        b.line_to(*self._to_px(X_MAX, 0))
        b.close()
        snap.append_fill(b.to_path(), Gsk.FillRule.WINDING, _rgba(accent, 0.15))

        b = Gsk.PathBuilder.new()
        b.move_to(*self._to_px(*pts[0]))
        for x, y in pts[1:]:
            b.line_to(*self._to_px(x, y))
        stroke = Gsk.Stroke.new(2.5)
        stroke.set_line_join(Gsk.LineJoin.ROUND)
        stroke.set_line_cap(Gsk.LineCap.ROUND)
        snap.append_stroke(b.to_path(), stroke, accent)

        def dot(px, py, r, color):
            b = Gsk.PathBuilder.new()
            b.add_circle(_pt(px, py), r)
            path = b.to_path()
            snap.append_fill(path, Gsk.FillRule.WINDING, color)
            snap.append_stroke(path, Gsk.Stroke.new(2), white)

        for x, y in shown:
            dot(*self._to_px(x, y), 6, accent)

        # current reading
        if self.current:
            lg, target = self.current
            lg = min(max(lg, X_MIN), X_MAX)
            px, py = self._to_px(lg, self._shown(target))
            b = Gsk.PathBuilder.new()
            b.move_to(round(px) + 0.5, by)
            b.line_to(round(px) + 0.5, by + bh)
            dash = Gsk.Stroke.new(1.2)
            dash.set_dash([4, 4])
            snap.append_stroke(b.to_path(), dash, _rgba(fg, 0.6))
            dot(px, py, 7.5, _rgba(fg, 0.9))
            label_y = py - 18 if py - 30 > by else py + 18
            w = self.create_pango_layout("сейчас").get_pixel_size()[0]
            lx = min(max(px, bx + w / 2), bx + bw - w / 2)
            self._text(snap, "сейчас", lx, label_y, _rgba(fg, 0.85), align=0.5)


# --- window --------------------------------------------------------------------

HELP_TEXT = (
    "Время от времени веб-камера включается меньше чем на секунду и смотрит, насколько "
    "светло в комнате — по стене за вами, а не по лицу. Изображение никуда не сохраняется "
    "и не отправляется, считается только общая яркость.\n\n"
    "Пока свет не меняется, проверки становятся реже (до раза в 3 минуты), а когда вы "
    "отошли от компьютера — прекращаются совсем.\n\n"
    "Чем светлее вокруг, тем ярче экран. Насколько ярче, решаете вы.\n\n"
    "Как настроить:\n"
    "• В блоке «Подстроить под себя» сдвиньте ползунок, пока яркость экрана не "
    "станет удобной, и нажмите «Запомнить». Сделайте так днём и вечером.\n"
    "• Обычный ползунок яркости GNOME (справа вверху) действует только до следующей "
    "проверки, потом яркость снова станет автоматической. Чтобы изменить её насовсем, "
    "используйте «Запомнить». Чтобы управлять яркостью только вручную, выключите автояркость.\n\n"
    "Проверить, что всё работает: нажмите «Начать тест» и посветите в камеру "
    "фонариком или закройте её ладонью.\n\n"
    "Если вы несколько раз подправите яркость при одинаковом свете, программа предложит "
    "это запомнить.\n\n"
    "Пока экран заблокирован, камера не включается. Если камерой пользуется "
    "другая программа (Zoom, Telegram), проверка просто пропускается."
)

INTERVALS = [(10, "Каждые 10 секунд"), (30, "Каждые 30 секунд"), (60, "Раз в минуту"),
             (300, "Раз в 5 минут")]
# label, smoothing, transition
REACTIONS = [("Быстро", 1.0, 1.2), ("Обычно", 0.5, 2.5), ("Плавно", 0.3, 5.0)]
PREVIEW_TIMEOUT = 20  # s: an unsaved slider preview is undone after this


def gnome_set_target(target):
    """Fire-and-forget SetAutoBrightnessTarget over the session bus (for live preview)."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call("org.gnome.Shell", "/org/gnome/Shell/Brightness", "org.gnome.Shell.Brightness",
                 "SetAutoBrightnessTarget", GLib.Variant("(d)", (target,)), None,
                 Gio.DBusCallFlags.NONE, 2000, None, None, None)
    except GLib.Error:
        pass


class SettingsWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Автояркость")
        self.set_default_size(560, 900)
        self.cfg = ab.load_config(ab.CONFIG_PATH)
        self._cfg_mtime = self._config_mtime()
        self._dirty = set()          # keys changed here, saved on top of the file on disk
        self._glow_seen = None       # last calibration result already shown
        self.status = {}
        self._loading = True
        self._save_id = 0
        self._service_busy = False
        self._preview_until = 0     # while > now, the slider belongs to the user
        self._slider_sync = False

        page = Adw.PreferencesPage()
        self.toasts = Adw.ToastOverlay(child=page)
        header = Adw.HeaderBar()
        help_btn = Gtk.Button(icon_name="help-about-symbolic", tooltip_text="Как это работает")
        help_btn.connect("clicked", self._on_help)
        header.pack_end(help_btn)
        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(header)
        self.set_content(view)

        page.add(self._build_status())
        page.add(self._build_tune())
        page.add(self._build_curve())
        page.add(self._build_settings())
        kbd = self._build_keyboard()
        if kbd:
            page.add(kbd)

        self._loading = False
        self._refresh()
        self._tick = 0
        GLib.timeout_add(500, self._on_tick)
        self.connect("close-request", self._on_close)

    # ---- sections
    def _build_status(self):
        g = Adw.PreferencesGroup()
        self.enable_row = Adw.SwitchRow(title="Автояркость")
        self.enable_row.add_prefix(Gtk.Image(icon_name="display-brightness-symbolic"))
        self.enable_row.connect("notify::active", self._on_enable_toggled)
        g.add(self.enable_row)

        self.light_row = self._value_row(g, "В комнате")
        self.screen_row = self._value_row(g, "Яркость экрана")
        self.checked_row = Adw.ActionRow(title="Последняя проверка")
        self.measure_btn = Gtk.Button(label="Проверить сейчас", valign=Gtk.Align.CENTER)
        self.measure_btn.connect("clicked", self._on_measure_now)
        self.checked_row.add_suffix(self.measure_btn)
        g.add(self.checked_row)

        self.test_row = Adw.ActionRow(title="Тест камеры")
        self.test_btn = Gtk.Button(label="Начать тест", valign=Gtk.Align.CENTER)
        self.test_btn.connect("clicked", self._on_test)
        self.test_row.add_suffix(self.test_btn)
        g.add(self.test_row)
        return g

    def _build_tune(self):
        g = Adw.PreferencesGroup(
            title="Подстроить под себя",
            description="Сдвиньте ползунок, и экран сразу поменяет яркость. Когда станет удобно, "
                        "нажмите «Запомнить». При таком освещении всегда будет такая яркость.")
        box = Gtk.Box(spacing=12, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        box.append(Gtk.Image(icon_name="display-brightness-symbolic", opacity=0.6))
        self.tune_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 100, 1)
        self.tune_scale.set_hexpand(True)
        self.tune_scale.set_draw_value(True)
        self.tune_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self.tune_scale.set_format_value_func(lambda _s, val: f"{val:.0f}%")
        self.tune_scale.connect("value-changed", self._on_tune_changed)
        box.append(self.tune_scale)
        self.remember_btn = Gtk.Button(label="Запомнить", valign=Gtk.Align.CENTER)
        self.remember_btn.add_css_class("suggested-action")
        self.remember_btn.connect("clicked", self._on_remember)
        box.append(self.remember_btn)
        card = Gtk.Frame(child=box)
        card.add_css_class("card")
        g.add(card)
        return g

    def _build_curve(self):
        g = Adw.PreferencesGroup(
            title="Как яркость зависит от света",
            description="Слева — темно, справа — светло, по высоте — яркость экрана. "
                        "Большая точка показывает текущее освещение. Цветные точки можно "
                        "перетаскивать мышью: двойной щелчок добавляет точку, правый удаляет.")
        reset = Gtk.Button(label="Сбросить", valign=Gtk.Align.CENTER,
                           tooltip_text="Вернуть настройки кривой по умолчанию")
        reset.add_css_class("flat")
        reset.connect("clicked", self._on_reset_curve)
        g.set_header_suffix(reset)

        self.graph = CurveGraph(self._on_curve_changed)
        self.graph.set_margin_top(8)
        self.graph.set_margin_bottom(6)
        self.graph.points = list(self.cfg["curve"])
        frame = Gtk.Frame(child=self.graph)
        frame.add_css_class("card")
        g.add(frame)
        return g

    def _build_settings(self):
        g = Adw.PreferencesGroup(title="Настройки")

        # how often
        labels = [label for _, label in INTERVALS]
        values = [val for val, _ in INTERVALS]
        if self.cfg["interval"] not in values:
            values.append(self.cfg["interval"])
            labels.append(f"Каждые {self.cfg['interval']:g} с")
        row = Adw.ComboRow(title="Как часто проверять свет",
                           subtitle="Если свет не меняется — реже, до 3 мин",
                           model=Gtk.StringList.new(labels))
        row.set_selected(values.index(self.cfg["interval"]))
        row.connect("notify::selected", lambda r, _p: self._set("interval", float(values[r.get_selected()])))
        g.add(row)

        # how fast
        row = Adw.ComboRow(title="Скорость реакции",
                           subtitle="«Плавно» не дёргает яркость от теней и проходящих людей",
                           model=Gtk.StringList.new([r[0] for r in REACTIONS]))
        nearest = min(range(len(REACTIONS)), key=lambda i: abs(REACTIONS[i][1] - self.cfg["smoothing"]))
        row.set_selected(nearest)

        def on_reaction(r, _p):
            _, smoothing, transition = REACTIONS[r.get_selected()]
            self._set("smoothing", smoothing)
            self._set("transition", transition)
        row.connect("notify::selected", on_reaction)
        g.add(row)

        row = Adw.SwitchRow(title="Не включать камеру на заблокированном экране",
                            active=self.cfg["pause_when_locked"])
        row.connect("notify::active", lambda r, _p: self._set("pause_when_locked", r.get_active()))
        g.add(row)

        row = Adw.SwitchRow(title="Не включать камеру, когда вас нет за компьютером",
                            subtitle="После 2 минут без мыши и клавиатуры. Видео и звонки не в счёт",
                            active=self.cfg["pause_when_away"])
        row.connect("notify::active", lambda r, _p: self._set("pause_when_away", r.get_active()))
        g.add(row)

        row = Adw.SwitchRow(title="Предлагать запомнить мои поправки",
                            subtitle="Если вы несколько раз подправите яркость при одинаковом свете",
                            active=self.cfg["suggest"])
        row.connect("notify::active", lambda r, _p: self._set("suggest", r.get_active()))
        g.add(row)

        self.glow_row = Adw.ActionRow(title="Учесть свет от самого экрана")
        self.glow_btn = Gtk.Button(label="Настроить", valign=Gtk.Align.CENTER)
        self.glow_btn.connect("clicked", self._on_calibrate_glow)
        self.glow_row.add_suffix(self.glow_btn)
        g.add(self.glow_row)
        self._update_glow_row()

        self.cameras = [("", "Выбирать автоматически")] + list_cameras()
        row = Adw.ComboRow(title="Камера", model=Gtk.StringList.new([n for _, n in self.cameras]))
        paths = [p for p, _ in self.cameras]
        row.set_selected(paths.index(self.cfg["device"]) if self.cfg["device"] in paths else 0)
        row.connect("notify::selected", lambda r, _p: self._set("device", self.cameras[r.get_selected()][0]))
        g.add(row)

        adv = Adw.ExpanderRow(title="Для опытных", subtitle="Обычно трогать не нужно")
        g.add(adv)
        self._spin(adv, "fast_interval", "Перепроверка после резкой смены света, с",
                   "Когда включили или выключили свет", 3, 120, 1, 0)
        self._spin(adv, "max_interval", "Реже всего проверять раз в, с",
                   "Когда свет долго не меняется", 30, 900, 10, 0)
        self._spin(adv, "away_after", "Считать, что вас нет, через, с",
                   "Без мыши и клавиатуры", 30, 1800, 30, 0)
        self._spin(adv, "hysteresis", "Не менять яркость меньше чем на, %",
                   "Чтобы яркость не дрожала", 0, 20, 1, 0, scale=100)
        self._spin(adv, "max_exposure_ms", "Максимальная выдержка камеры, мс",
                   "Больше — точнее в темноте, но камера дольше включена", 20, 500, 10, 0)
        self._spin(adv, "target_luma", "Целевая яркость кадра (0–255)", "", 30, 120, 1, 0)
        self._spin(adv, "black_level", "Уровень чёрного камеры (0–255)", "", 0, 40, 0.5, 1)
        return g

    def _build_keyboard(self):
        """Only on laptops whose keyboard light GNOME can control."""
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            bus.call_sync("org.gnome.SettingsDaemon.Power", "/org/gnome/SettingsDaemon/Power",
                          "org.freedesktop.DBus.Properties", "Get",
                          GLib.Variant("(ss)", ("org.gnome.SettingsDaemon.Power.Keyboard", "Brightness")),
                          None, Gio.DBusCallFlags.NONE, 2000, None)
        except GLib.Error:
            return None
        g = Adw.PreferencesGroup(title="Подсветка клавиатуры")
        row = Adw.SwitchRow(title="Включать в темноте",
                            subtitle="Днём выключается сама. Если переключите её клавишей — спорить не будет",
                            active=self.cfg["kbd_auto"])
        row.connect("notify::active", lambda r, _p: self._set("kbd_auto", r.get_active()))
        g.add(row)
        levels = [(33, "Слабая"), (66, "Средняя"), (100, "Яркая")]
        combo = Adw.ComboRow(title="Яркость подсветки", model=Gtk.StringList.new([n for _, n in levels]))
        combo.set_selected(min(range(3), key=lambda i: abs(levels[i][0] - self.cfg["kbd_level"])))
        combo.connect("notify::selected", lambda r, _p: self._set("kbd_level", levels[r.get_selected()][0]))
        g.add(combo)
        return g

    def _update_glow_row(self):
        if self.cfg["screen_glow"] > 0:
            self.glow_row.set_subtitle("Учитывается. Можно перенастроить вечером в тёмной комнате")
        else:
            self.glow_row.set_subtitle("Вечером в тёмной комнате нажмите «Настроить» — "
                                       "экран на 2 секунды мигнёт")

    def _on_calibrate_glow(self, _btn):
        try:
            os.makedirs(os.path.dirname(ab.COMMAND_PATH), exist_ok=True)
            with open(ab.COMMAND_PATH, "w") as f:
                f.write("calibrate-glow")
        except OSError as e:
            self.toasts.add_toast(Adw.Toast(title=f"Не получилось: {e}"))
            return
        if self._signal_daemon(signal.SIGUSR1):
            self.toasts.add_toast(Adw.Toast(title="Настраиваю: экран на пару секунд мигнёт…", timeout=3))
        else:
            self.toasts.add_toast(Adw.Toast(title="Сначала включите автояркость"))

    # ---- row helpers
    def _value_row(self, group, title):
        row = Adw.ActionRow(title=title)
        label = Gtk.Label(label="—", valign=Gtk.Align.CENTER)
        label.add_css_class("heading")
        row.add_suffix(label)
        row.value_label = label
        group.add(row)
        return row

    def _spin(self, expander, key, title, subtitle, lo, hi, step, digits, scale=1):
        row = Adw.SpinRow.new_with_range(lo, hi, step)
        row.set_title(title)
        if subtitle:
            row.set_subtitle(subtitle)
        row.set_digits(digits)
        row.set_value(self.cfg[key] * scale)
        row.connect("notify::value", lambda r, _p: self._set(key, r.get_value() / scale))
        expander.add_row(row)

    # ---- saving
    def _set(self, key, value):
        if self._loading:
            return
        self.cfg[key] = value
        self._dirty.add(key)
        self._schedule_save()

    def _schedule_save(self):
        if self._save_id:
            GLib.source_remove(self._save_id)
        self._save_id = GLib.timeout_add(500, self._save)

    @staticmethod
    def _config_mtime():
        try:
            return os.stat(ab.CONFIG_PATH).st_mtime_ns
        except OSError:
            return None

    def _save(self):
        """Save only what was changed here, on top of the file — the daemon writes it too
        (screen-glow calibration, remembered corrections, curve migration)."""
        self._save_id = 0
        try:
            disk = ab.load_config(ab.CONFIG_PATH)
            for key in self._dirty:
                disk[key] = self.cfg[key]
            ab.save_config(disk, ab.CONFIG_PATH)
            self.cfg = disk
            self._dirty.clear()
            self._cfg_mtime = self._config_mtime()
        except (OSError, ValueError) as e:
            self.toasts.add_toast(Adw.Toast(title=f"Не удалось сохранить: {e}"))
            return False
        self._signal_daemon(signal.SIGHUP)
        return False

    def _signal_daemon(self, sig):
        _, active, pid = service_state()
        if active and pid:
            try:
                os.kill(pid, sig)
                return True
            except OSError:
                pass
        return False

    # ---- brightness preview / remember
    def _offset(self):
        """GNOME adds its own slider as an offset on top of the target the daemon sends
        (screen = target + offset); the daemon tracks it and publishes it."""
        return self.status.get("slider_offset", 0.0) if self.status.get("backend") == "gnome" else 0.0

    def _write_preview(self, until):
        try:
            if until:
                os.makedirs(os.path.dirname(ab.PREVIEW_PATH), exist_ok=True)
                with open(ab.PREVIEW_PATH, "w") as f:
                    f.write(str(until))
            elif os.path.exists(ab.PREVIEW_PATH):
                os.remove(ab.PREVIEW_PATH)
        except OSError:
            pass

    def _on_tune_changed(self, scale):
        if self._slider_sync or self._loading:
            return
        if self.status.get("target") is None:
            return
        if not self._preview_until:
            self._preview_offset = self._offset()
        self._preview_until = time.time() + PREVIEW_TIMEOUT
        self._write_preview(self._preview_until + 2)
        target = min(max(scale.get_value() / 100 - self._preview_offset, 0.0), 1.0)
        if self.status.get("backend") == "gnome":
            gnome_set_target(target)

    def _end_preview(self, restore):
        if self._preview_until and restore and self.status.get("target") is not None:
            gnome_set_target(min(max(self.status["target"] - self._preview_offset, 0.0), 1.0))
        self._preview_until = 0
        self._write_preview(0)

    def _on_remember(self, _btn):
        lg = self.status.get("smoothed")
        if lg is None:
            self.toasts.add_toast(Adw.Toast(title="Сначала включите автояркость, чтобы измерить свет"))
            return
        y = round(self.tune_scale.get_value() / 100, 3)   # the curve is in screen brightness
        x = round(min(max(lg, X_MIN), X_MAX), 2)
        pts = [p for p in self.graph.points if abs(p[0] - x) > 0.25]
        pts.append((x, y))
        self.graph.points = sorted(pts)
        self.graph.queue_draw()
        self._preview_until = 0
        self._write_preview(0)
        self._on_curve_changed()
        self.toasts.add_toast(Adw.Toast(
            title=f"Запомнил: при таком свете яркость {self.tune_scale.get_value():.0f}%"))

    # ---- other actions
    def _on_help(self, _btn):
        dialog = Adw.AlertDialog(heading="Как это работает", body=HELP_TEXT)
        dialog.add_response("ok", "Понятно")
        dialog.present(self)

    def _on_close(self, *_):
        self._end_preview(restore=True)
        return False

    def _on_curve_changed(self):
        self.cfg["curve"] = list(self.graph.points)
        self._dirty.add("curve")
        self._schedule_save()

    def _pick_up_daemon_changes(self):
        """The daemon rewrote the config: show its curve and calibration."""
        mtime = self._config_mtime()
        if mtime == self._cfg_mtime or self._dirty or self._save_id or self.graph._drag:
            return
        self._cfg_mtime = mtime
        try:
            disk = ab.load_config(ab.CONFIG_PATH)
        except (OSError, ValueError):
            return
        self.cfg.update({k: disk[k] for k in ("curve", "curve_metric", "screen_glow")})
        self.graph.points = list(disk["curve"])
        self.graph.queue_draw()
        self._update_glow_row()

    def _on_reset_curve(self, _btn):
        self.graph.points = [tuple(p) for p in ab.DEFAULTS["curve"]]
        self.graph.queue_draw()
        self._on_curve_changed()
        self.toasts.add_toast(Adw.Toast(title="Кривая сброшена"))

    def _on_test(self, _btn):
        if not self._signal_daemon(signal.SIGUSR2):
            self.toasts.add_toast(Adw.Toast(title="Сначала включите автояркость"))

    def _on_measure_now(self, _btn):
        if self._signal_daemon(signal.SIGUSR1):
            self.toasts.add_toast(Adw.Toast(title="Проверяю освещение…", timeout=2))
        else:
            self.toasts.add_toast(Adw.Toast(title="Автояркость выключена"))

    def _on_enable_toggled(self, row, _p):
        if self._loading or self._service_busy:
            return
        on = row.get_active()
        self._service_busy = True
        row.set_sensitive(False)

        def done(proc, res):
            try:
                proc.communicate_utf8_finish(res)
                ok = proc.get_successful()
            except GLib.Error:
                ok = False
            if not ok:
                self.toasts.add_toast(Adw.Toast(title="Не получилось переключить автояркость"))
            self._service_busy = False
            row.set_sensitive(True)
            self._refresh()

        proc = Gio.Subprocess.new(["systemctl", "--user", "enable" if on else "disable", "--now", UNIT],
                                  Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE)
        proc.communicate_utf8_async(None, None, done)

    # ---- live status
    def _on_tick(self):
        # Every 0.5 s while a test runs (it's live), otherwise once a second.
        self._tick += 1
        live = self.status.get("live_left", 0) > 0
        if live or self._tick % 2 == 0:
            self._refresh()
        return True

    def _refresh(self):
        installed, active, _pid = service_state()
        self.status = st = read_status() if active else {}
        now = time.time()

        if not self._service_busy:
            self._loading = True
            self.enable_row.set_active(active)
            self._loading = False
        self.enable_row.set_sensitive(installed and not self._service_busy)

        state = st.get("state")
        if not installed:
            sub = "Не установлено: запустите install.sh"
        elif not active:
            sub = "Выключена: яркость меняете вы сами"
        elif state == "locked":
            sub = "Экран заблокирован, камера отдыхает"
        elif state == "away":
            sub = "Вы отошли — камера отдыхает, пока вас нет"
        elif state == "calibrating":
            sub = "Настраиваю учёт света от экрана…"
        elif state == "nocamera":
            sub = "Камера выключена: включите её клавишей с камерой (обычно F10), автояркость подождёт"
        elif state == "busy":
            sub = "Камерой пользуется другая программа, проверка пропущена"
        elif state == "error":
            sub = "Ошибка: " + st.get("message", "")
        elif state == "measuring":
            sub = "Смотрю, насколько светло…"
        elif st.get("next_at"):
            left = st["next_at"] - now
            sub = ("Включена · следующая проверка через " + ago(left)) if left >= 1 else "Включена · проверяю…"
        else:
            sub = "Запускается…"
        lg = st.get("smoothed")
        self.light_row.value_label.set_label(describe_light(lg) if lg is not None else "—")
        self.light_row.set_tooltip_text(f"Уровень света {lg:.2f} (логарифм)" if lg is not None else None)
        bl = backlight_fraction()
        self.screen_row.value_label.set_label(f"{bl:.0%}" if bl is not None else "—")
        self.checked_row.set_subtitle(f"{ago(now - st['measured_at'])} назад"
                                      if st.get("measured_at") else "ещё не было")
        live_left = st.get("live_left", 0) - (now - st.get("time", now))
        testing = active and live_left > 0
        self.measure_btn.set_sensitive(active and state != "measuring" and not testing)
        self.test_btn.set_sensitive(active and not testing)
        self.test_btn.set_label(f"Идёт тест… {ago(live_left)}" if testing else "Начать тест")
        self.test_row.set_subtitle(
            "Сейчас посветите в камеру фонариком или закройте её ладонью, яркость будет меняться"
            if testing else
            "20 секунд камера следит непрерывно: удобно, чтобы проверить, что всё работает")
        if testing:
            sub = "Тест: камера следит непрерывно"
        self.enable_row.set_subtitle(sub)
        self.remember_btn.set_sensitive(lg is not None)
        self.tune_scale.set_sensitive(lg is not None)

        # slider: follows the real screen brightness unless the user is previewing
        if self._preview_until and now > self._preview_until:
            self._end_preview(restore=True)
            self.toasts.add_toast(Adw.Toast(title="Яркость не запомнена — вернул как было"))
        if not self._preview_until and bl is not None:
            self._slider_sync = True
            self.tune_scale.set_value(round(bl * 100))
            self._slider_sync = False

        self._pick_up_daemon_changes()
        glow = st.get("glow_result")
        if glow and glow.get("at") != self._glow_seen:
            if self._glow_seen is not None or now - glow["at"] < 10:
                if "error" in glow:
                    msg = "Не получилось настроить: " + glow["error"]
                else:
                    msg = (f"Готово: свет экрана давал {glow['share']:.0%} показаний, теперь это учитывается"
                           if glow["share"] >= 0.02 else "Готово: свет экрана на замер почти не влияет")
                self.toasts.add_toast(Adw.Toast(title=msg, timeout=6))
            self._glow_seen = glow.get("at")
        self.glow_btn.set_sensitive(active and state not in ("calibrating", "measuring"))

        new_current = (lg, ab.curve_target(self.graph.points, lg)) if lg is not None else None
        if new_current != self.graph.current:
            self.graph.current = new_current
            self.graph.queue_draw()
        return True


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        win = self.get_active_window() or SettingsWindow(self)
        win.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
