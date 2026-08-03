#!/bin/bash
# Assembles browser-specific extension packages from extension/shared/ plus each
# browser's manifest. Keeping one copy of the logic is what stops the Chrome and
# Firefox builds from silently drifting apart.
#
# Usage: bash build-extension.sh [dest-dir]   (default: ./build/extension)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/extension"
SHARED="$SRC/shared"
DEST="${1:-$SCRIPT_DIR/build/extension}"

SHARED_FILES=(content.js background.js popup.html popup.js)

for file in "${SHARED_FILES[@]}"; do
    if [ ! -f "$SHARED/$file" ]; then
        echo "error: missing $SHARED/$file" >&2
        exit 1
    fi
done

for browser in chrome firefox; do
    manifest="$SRC/$browser/manifest.json"
    if [ ! -f "$manifest" ]; then
        echo "error: missing $manifest" >&2
        exit 1
    fi

    target="$DEST/$browser"
    rm -rf "$target"
    mkdir -p "$target"
    cp "$manifest" "$target/manifest.json"
    for file in "${SHARED_FILES[@]}"; do
        cp "$SHARED/$file" "$target/$file"
    done
    echo "built $target"
done
