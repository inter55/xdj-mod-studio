#!/bin/sh
# make_noob_pack.sh -- the noob deliverable: portable .app + one page.
# Expects dist/XDJ Mod Studio.app built (tools/build_studio.sh).
# NEVER publish: firmware-derived content for one tester only.
set -e
cd "$(dirname "$0")/.."
APP="dist/XDJ Mod Studio.app"
[ -d "$APP" ] || { echo "build the app first"; exit 1; }
REPO="$APP/Contents/Resources/repo"
# slim: emulator boots from mainscreen alone; drop the other snapshots
for p in emu_track_loaded.pkl emu_realusb.pkl emu_realusb_m.pkl; do
  rm -f "$REPO/cache/$p"
done
codesign --force --deep -s - "$APP" 2>&1 | tail -n 1
cat > /tmp/TESTER_ONEPAGE.txt <<'EOF'
XDJ-700 TEST DRIVE -- one page, that's it.

WHAT THIS IS: try our XDJ-700 mods. Everything is temporary -- turn the
deck off and on and it's stock again. Nothing here can brick your deck.

NEEDS: an Apple Silicon Mac (M1 or newer) on macOS 14 Sonoma or later.
(Intel Macs: not supported yet -- tell us if that's you.)

START: unzip, then RIGHT-CLICK "XDJ Mod Studio" -> Open -> Open (first
time only: macOS blocks apps from small developers on a plain double-
click). After that it opens normally. No setup, no terminal.

TRY THESE THREE:
1. Emulator -- open the Emulator tab. A virtual XDJ-700 appears and
   boots by itself (a few seconds). Click its buttons / drag the
   screen like the real thing. Tell us if anything looks broken.
2. Your own music on it -- press "Insert library" and pick your rekordbox
   USB stick or export folder (the one with the PIONEER folder in it).
   Give the virtual deck about a minute to read it, then press BROWSE,
   pick a track and load it. (The USB button ejects when a stick is in;
   NO USB just means nothing is inserted yet.) FLAC and Ogg Opus files on
   the stick play too. Try Mods: RGB waveform, PERFORM screen
   (beat jump / key shift / stems tabs), MY SETTINGS rows. Everything
   in the emulator is safe to click.
3. Real deck (optional, only if you have an XDJ-700 on fw 1.15): plug it
   in by USB with music apps closed. Deck installs are RAM-only (power
   off = stock again) -- but message us first and we'll walk you through.
   Known issues: beat-jump sizes 32/64 occasionally need a second swipe
   and themes leave a few stock buttons half-coloured. If anything looks
   odd: stop and power-cycle (that always returns the deck to stock).

NOTE: emulator audio is our approximation, not the deck's own sound chip
(that part is still being built).

REPORT: deck model + firmware (Utility screen), Mac model, what worked,
what didn't, one photo where it helps. "Nothing happened" is data.

RULES: if anything looks odd, stop and power-cycle the deck. Delete this
when done, don't share it.
EOF
cp /tmp/TESTER_ONEPAGE.txt "$APP/../TESTER_ONEPAGE.txt"
OUT="$PWD/dist/XDJ700-noob.zip"
# the 1.0.1 lesson: `zip -r` adds AppleDouble sidecars and a stale seal makes
# macOS call the app damaged. This strips them, re-seals and proves the archive.
sh tools/make_app_zip.sh "$APP" "$OUT" "dist/TESTER_ONEPAGE.txt"
echo "noob pack: $OUT ($(du -h "$OUT" | cut -f1))"
