#!/bin/sh
set -e

APP_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
chmod +x "$APP_DIR/cnews-direct" "$APP_DIR/cnews_direct.py"

if ! command -v mpv >/dev/null 2>&1 || ! python3 -c "import tkinter" >/dev/null 2>&1; then
    echo "Installation des dépendances requises (mpv, python3-tk)..."
    sudo apt-get update && sudo apt-get install -y mpv python3 python3-tk
fi

APPS_DIR="$HOME/.local/share/applications"
mkdir -p "$APPS_DIR"

DESKTOP_FILE="$APPS_DIR/cnews-direct.desktop"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=CNEWS Direct
GenericName=Direct TV & Radio
Comment=Regarder CNEWS TV et écouter CNEWS Audio / Europe 1
Exec="$APP_DIR/cnews-direct"
Icon=$APP_DIR/icon.png
Terminal=false
Categories=AudioVideo;Video;TV;
Keywords=CNEWS;Europe1;direct;tv;radio;info;actualités;
StartupNotify=true
EOF

chmod +x "$DESKTOP_FILE"

DESKTOP_DIR=""
if command -v xdg-user-dir >/dev/null 2>&1; then
    DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
fi

for d in "$DESKTOP_DIR" "$HOME/Bureau" "$HOME/Desktop"; do
    if [ -n "$d" ] && [ -d "$d" ] && [ "$d" != "$HOME" ]; then
        TARGET="$d/cnews-direct.desktop"
        cp "$DESKTOP_FILE" "$TARGET"
        chmod +x "$TARGET"
        if command -v gio >/dev/null 2>&1; then
            gio set "$TARGET" metadata::trusted true >/dev/null 2>&1 || true
        fi
        break
    fi
done

echo "Installation terminée ! L'icône CNEWS Direct a été ajoutée sur le Bureau et dans le menu des applications."
