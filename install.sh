#!/bin/sh
# Install autobright as a systemd user service plus its settings window (no root needed).
#   ./install.sh            install (or update) and start
#   ./install.sh uninstall  stop and remove
set -eu
here=$(cd "$(dirname "$0")" && pwd)
dest="$HOME/.local/share/autobright"
unit="$HOME/.config/systemd/user/autobright.service"
desktop="$HOME/.local/share/applications/io.github.autobright.Settings.desktop"
conf="${XDG_CONFIG_HOME:-$HOME/.config}/autobright/config.toml"
uuid="autobright@autobright.local"
ext="$HOME/.local/share/gnome-shell/extensions/$uuid"

# add/remove our extension in GNOME's enabled-extensions list
set_extension() {
    command -v gsettings >/dev/null || return 0
    cur=$(gsettings get org.gnome.shell enabled-extensions 2>/dev/null) || return 0
    new=$(python3 -c 'import ast,sys
cur = sys.argv[1].replace("@as ", "")
lst = [x for x in ast.literal_eval(cur) if x != sys.argv[2]]
if sys.argv[3] == "on": lst.append(sys.argv[2])
print(lst)' "$cur" "$uuid" "$1")
    gsettings set org.gnome.shell enabled-extensions "$new"
}

# (a copy of this script is installed too: ~/.local/share/autobright/install.sh uninstall)
if [ "${1:-}" = uninstall ]; then
    systemctl --user disable --now autobright.service 2>/dev/null || true
    set_extension off
    rm -rf "$dest" "$unit" "$desktop" "$ext"
    systemctl --user daemon-reload
    echo "removed (config left in $conf)"
    exit 0
fi

mkdir -p "$dest" "$(dirname "$unit")" "$(dirname "$desktop")" "$(dirname "$conf")"
install -m 755 "$here/autobright.py" "$here/autobright-settings.py" "$here/install.sh" "$dest/"
install -m 644 "$here/v4l2cam.py" "$dest/"
install -m 644 "$here/autobright.service" "$unit"
mkdir -p "$ext"
install -m 644 "$here/gnome-extension/$uuid/metadata.json" "$here/gnome-extension/$uuid/extension.js" "$ext/"
set_extension on
[ -e "$conf" ] || install -m 644 "$here/config.example.toml" "$conf"

cat > "$desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Автояркость
Name[en]=Auto Brightness
Comment=Яркость экрана по освещению с веб-камеры
Comment[en]=Screen brightness from webcam ambient light
Exec=/usr/bin/python3 $dest/autobright-settings.py
Icon=display-brightness-symbolic
Categories=Settings;HardwareSettings;GTK;
Keywords=brightness;backlight;webcam;яркость;подсветка;камера;
StartupNotify=true
EOF
update-desktop-database -q "$(dirname "$desktop")" 2>/dev/null || true

systemctl --user daemon-reload
systemctl --user enable autobright.service
systemctl --user restart autobright.service
echo "installed; open \"Автояркость\" from the app grid, logs: journalctl --user -u autobright -f"
echo "the quick settings toggle appears after the next log out / log in (GNOME on Wayland can't load new extensions live)"
