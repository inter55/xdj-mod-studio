#!/bin/sh
# make_app_zip.sh APP OUT.zip [extra files...] -- zip a built .app so it still
# opens on someone else's Mac.
#
# What went wrong in 1.0.1: the zip was made with `zip -r` from an app that had
# been changed after it was signed. The archive carried 2,468 AppleDouble
# sidecars (._Info.plist, ._Contents, ...) and an app whose sealed resource
# list no longer matched its files (14 files had been removed). macOS sees
# either of those as a damaged app and refuses to open it -- what the Italian
# tester reported as "1.01 doesn't load / is corrupted".
#
# This script: strips the sidecars, re-seals the signature, zips with ditto
# (no HFS metadata), then proves the archive: extract it fresh, verify the code
# signature strictly, check no sidecar came back and check no file was dropped.
set -e
if [ "$1" = "--verify" ]; then
    # prove an EXISTING archive is publishable (and that this gate can fail:
    # run it on the 1.0.1 archive as originally shipped)
    OUT=$2
    SIDECARS=$(unzip -l "$OUT" | grep -c '\._' || true)
    [ "$SIDECARS" = 0 ] || { echo "FAIL: $SIDECARS AppleDouble entries in $OUT"; exit 1; }
    TMPV=$(mktemp -d)
    trap 'rm -rf "$TMPV"' EXIT
    unzip -q "$OUT" -d "$TMPV/x"
    P=$(find "$TMPV/x" -maxdepth 1 -name '*.app' | head -1)
    [ -n "$P" ] || { echo "FAIL: no .app in $OUT"; exit 1; }
    codesign --verify --deep --strict "$P" || { echo "FAIL: signature in $OUT"; exit 1; }
    python3 "$(dirname "$0")/app_bundle_check.py" "$P" || { echo "FAIL: bundle content in $OUT"; exit 1; }
    echo "ok $OUT"
    exit 0
fi
APP=$1
OUT=$2
shift 2
[ -n "$APP" ] && [ -n "$OUT" ] || { echo "usage: make_app_zip.sh APP OUT.zip [extras...]"; exit 2; }
[ -d "$APP" ] || { echo "REFUSED: no app at $APP"; exit 1; }

echo "== cleaning $APP"
find "$APP" -name '._*' -delete
find "$APP" -name '.DS_Store' -delete
rm -rf "$APP/__MACOSX"

echo "== re-sealing the signature"
codesign --force --deep -s - "$APP"
codesign --verify --deep --strict "$APP"

STAGE=$(mktemp -d)
TMP=$(mktemp -d)
trap 'rm -rf "$STAGE" "$TMP"' EXIT
ditto --norsrc "$APP" "$STAGE/$(basename "$APP")"
for x in "$@"; do cp -R "$x" "$STAGE/"; done

rm -f "$OUT"
ditto -c -k --norsrc "$STAGE" "$OUT"

echo "== proving the archive"
SIDECARS=$(unzip -l "$OUT" | grep -c '\._' || true)
[ "$SIDECARS" = 0 ] || { echo "REFUSED: $SIDECARS AppleDouble entries in the zip"; exit 1; }
unzip -q "$OUT" -d "$TMP/x"
PACKED=$(find "$TMP/x" -maxdepth 1 -name '*.app' | head -1)
[ -n "$PACKED" ] || { echo "REFUSED: no .app in the zip"; exit 1; }
codesign --verify --deep --strict "$PACKED" && echo "signature: verified in a fresh extraction"
python3 "$(dirname "$0")/app_bundle_check.py" "$PACKED" || { echo "REFUSED: the archive is missing runtime files"; exit 1; }

(cd "$APP" && find . -type f | sort) > "$TMP/before.txt"
(cd "$PACKED" && find . -type f | sort) > "$TMP/after.txt"
if ! diff -q "$TMP/before.txt" "$TMP/after.txt" >/dev/null; then
    echo "REFUSED: the archive does not hold the same files as the app:"
    diff "$TMP/before.txt" "$TMP/after.txt" | head -20
    exit 1
fi
echo "files: $(wc -l < "$TMP/after.txt" | tr -d ' ') all present"
echo "ok $OUT ($(du -h "$OUT" | cut -f1), md5 $(md5 -q "$OUT" 2>/dev/null || md5sum "$OUT" | cut -d' ' -f1))"
