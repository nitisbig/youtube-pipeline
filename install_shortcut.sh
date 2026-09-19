#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_SRC="$SCRIPT_DIR/youtube-pipeline.desktop"
LAUNCHER="$SCRIPT_DIR/launch.sh"
ICON_PNG="$SCRIPT_DIR/assets/icon.png"

# Ensure local files are executable
chmod +x "$LAUNCHER"
chmod +x "$DESKTOP_SRC"

# Target paths
USER_DESKTOP_DIR="${XDG_DESKTOP_DIR:-$HOME/Desktop}"
USER_APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

echo "Installing YouTube Video Pipeline shortcut..."

# 1. Install to Desktop
if [ -d "$USER_DESKTOP_DIR" ]; then
    DEST_DESKTOP="$USER_DESKTOP_DIR/youtube-pipeline.desktop"
    cp "$DESKTOP_SRC" "$DEST_DESKTOP"
    chmod +x "$DEST_DESKTOP"
    # Linux Mint / Cinnamon (Nemo) trusted flag
    if command -v gio >/dev/null 2>&1; then
        gio set "$DEST_DESKTOP" metadata::trusted true 2>/dev/null || true
        gio set "$DEST_DESKTOP" metadata::trusted yes 2>/dev/null || true
    fi
    echo "✓ Created Desktop shortcut: $DEST_DESKTOP"
else
    echo "Notice: Desktop directory not found at $USER_DESKTOP_DIR"
fi

# 2. Install to Applications Menu (Linux Mint Start Menu)
mkdir -p "$USER_APPS_DIR"
DEST_APP="$USER_APPS_DIR/youtube-pipeline.desktop"
cp "$DESKTOP_SRC" "$DEST_APP"
chmod +x "$DEST_APP"
echo "✓ Created Application Menu shortcut: $DEST_APP"

# 3. Update desktop database if available
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$USER_APPS_DIR" 2>/dev/null || true
fi

echo ""
echo "Shortcut installation complete!"
echo "- Desktop icon is ready on your Desktop."
echo "- App is also accessible from the Linux Mint Application Menu under 'Sound & Video'."
