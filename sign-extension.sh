#!/bin/bash
# Builds and signs the Firefox extension on AMO, producing a self-distributable
# .xpi in releases/.
#
# Credentials come from ~/.web-ext-config.mjs (or the current directory's
# web-ext-config.mjs), which web-ext reads automatically:
#
#   export default {
#     sign: { apiKey: 'user:1234567:890', apiSecret: '<secret>' },
#   };
#
# Get them from https://addons.mozilla.org/developers/addon/api/key/
# The secret is displayed only once — store it in that file, never in the repo.
#
# Usage: bash sign-extension.sh [--channel unlisted|listed]
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

CHANNEL="unlisted"
while [ $# -gt 0 ]; do
    case "$1" in
        --channel) CHANNEL="${2:-}"; shift ;;
        --help|-h) sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 0 ;;
        *) err "Unknown option: $1"; exit 2 ;;
    esac
    shift
done

case "$CHANNEL" in
    unlisted|listed) ;;
    *) err "Channel must be 'unlisted' or 'listed', got '$CHANNEL'"; exit 2 ;;
esac

BUILD_DIR="$SCRIPT_DIR/build/sign"
RELEASES_DIR="$SCRIPT_DIR/releases"
MANIFEST="$SCRIPT_DIR/extension/firefox/manifest.json"

command -v web-ext >/dev/null 2>&1 || {
    err "web-ext is not installed."
    info "Install it with: npm install --global web-ext"
    exit 1
}

# ─── Read and validate the manifest ──────────────────────────
read_manifest() {
    MANIFEST="$MANIFEST" python3 - <<'PY'
import json, os, sys
with open(os.environ["MANIFEST"]) as fh:
    m = json.load(fh)
addon_id = m.get("browser_specific_settings", {}).get("gecko", {}).get("id")
if not addon_id:
    sys.exit("manifest has no browser_specific_settings.gecko.id — AMO cannot sign it")
print(m["version"])
print(addon_id)
PY
}

if ! MANIFEST_INFO=$(read_manifest); then
    err "$MANIFEST_INFO"
    exit 1
fi
VERSION=$(echo "$MANIFEST_INFO" | sed -n 1p)
ADDON_ID=$(echo "$MANIFEST_INFO" | sed -n 2p)

info "Add-on ID: $ADDON_ID"
info "Version:   $VERSION"
info "Channel:   $CHANNEL"

# AMO permanently rejects a version number that has already been uploaded, so
# catch the mistake here rather than after a failed round trip.
EXISTING="$RELEASES_DIR/odoo-runbot-local-$VERSION.xpi"
if [ -f "$EXISTING" ]; then
    err "releases/odoo-runbot-local-$VERSION.xpi already exists."
    info "AMO will not accept a version it has already signed."
    info "Bump \"version\" in extension/{chrome,firefox}/manifest.json first."
    exit 1
fi

# ─── Build a clean package ───────────────────────────────────
info "Building the Firefox package..."
rm -rf "$BUILD_DIR"
bash "$SCRIPT_DIR/build-extension.sh" "$BUILD_DIR" >/dev/null || {
    err "Extension build failed."
    exit 1
}
SRC="$BUILD_DIR/firefox"

# web-ext lints as part of signing, but running it first gives a readable report.
info "Linting..."
if ! web-ext lint --source-dir "$SRC" --warnings-as-errors=false; then
    err "Lint failed — fix the errors above before signing."
    exit 1
fi

# ─── Sign ────────────────────────────────────────────────────
info "Uploading to AMO for signing (this can take a minute)..."
mkdir -p "$RELEASES_DIR"
if ! web-ext sign \
        --source-dir "$SRC" \
        --artifacts-dir "$BUILD_DIR/artifacts" \
        --channel "$CHANNEL"; then
    err "Signing failed."
    info "Common causes:"
    info "  * No credentials — create ~/.web-ext-config.mjs (see the header of this script)"
    info "  * Wrong channel — an add-on registered as 'listed' cannot be signed 'unlisted'"
    info "  * Version $VERSION was already uploaded — bump it in both manifests"
    exit 1
fi

# ─── Collect the artifact ────────────────────────────────────
SIGNED=$(find "$BUILD_DIR/artifacts" -name '*.xpi' -print -quit)
if [ -z "$SIGNED" ]; then
    err "web-ext reported success but produced no .xpi in $BUILD_DIR/artifacts"
    exit 1
fi

# A signed package carries Mozilla's signature block; an unsigned one does not.
if ! unzip -l "$SIGNED" | grep -q 'META-INF/mozilla.rsa'; then
    err "$SIGNED is not signed (no META-INF/mozilla.rsa) — refusing to publish it."
    exit 1
fi

cp "$SIGNED" "$EXISTING"
ok "Signed extension written to releases/odoo-runbot-local-$VERSION.xpi"

echo ""
info "Install it in Firefox by opening the file, or from about:addons →"
info "gear icon → 'Install Add-on From File...'"
info ""
info "Older signed releases can now be deleted from releases/ if unneeded."
