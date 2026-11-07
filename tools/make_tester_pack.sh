#!/bin/sh
# make_tester_pack.sh -- MINIMAL private tester pack. NEVER publish: it
# contains firmware-derived files for one tester only. Do not redistribute.
set -e
cd "$(dirname "$0")/.."
STAGE=/tmp/tester_pack_stage
rm -rf "$STAGE"
mkdir -p "$STAGE/cache" "$STAGE/work"
# code only (no docs: several .md files contain personal serials/notes)
for d in tools mods emu studio; do
  cp -R "$d" "$STAGE/"
done
find "$STAGE" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -name "*.pyc" -delete 2>/dev/null || true
find "$STAGE" -name ".DS_Store" -delete 2>/dev/null || true
rm -f "$STAGE"/studio/*.BACKUP "$STAGE"/tools/*.bak
cp TESTER_README.md XDJ-Test-Drive.command "$STAGE/"
chmod +x "$STAGE/XDJ-Test-Drive.command"
# firmware-derived essentials ONLY (snapshots gzip'd: .pkl.gz loads fine)
cp work/MAIN.img "$STAGE/work/"
cp studio/assets/screens.npz "$STAGE/studio/assets/" 2>/dev/null || true
for p in emu_mainscreen.pkl emu_track_loaded.pkl emu_realusb.pkl emu_realusb_m.pkl; do
  if [ -f "cache/$p.gz" ]; then cp "cache/$p.gz" "$STAGE/cache/"
  elif [ -f "cache/$p" ]; then gzip -9 -c "cache/$p" > "$STAGE/cache/$p.gz"; fi
done
# the real-DSP snapshot ("Stock v1.15 (real audio DSP)" firmware choice):
[ -f cache/emu_dsp_sound.pkl.gz ] && cp cache/emu_dsp_sound.pkl.gz "$STAGE/cache/" || true
# + its decode tool (emu/c6x/c6xcore.c already travels with the emu/ copy)
[ -f work/tic6x/objdump ] && mkdir -p "$STAGE/work/tic6x" && cp work/tic6x/objdump "$STAGE/work/tic6x/" || true
# the pack goes to someone else: no real names or machine paths in it
if [ -d "$STAGE" ]; then
  # text files: plain replacement. Binaries (objdump, .so): same-length
  # bytes only, so nothing inside them shifts or breaks.
  grep -rla -e '/Users/' -e "Mac""Book" -e "Jam""esG" "$STAGE" 2>/dev/null |
    while IFS= read -r f; do
      if grep -qI . "$f" 2>/dev/null; then
        sed -i '' -e 's|/Users/[A-Za-z0-9._-]*|~|g' \
                  -e "s|Mac""Book[-A-Za-z0-9._]*\.local|localhost|g" \
                  -e "s|Jam""esG|tester|g" "$f"
      else
        perl -pi -e "s{/Users/Jam""esG}{/build/ccache}g; s{Mac""Book}{Machine}g; s{Jam""esG}{tester}g" "$f"
      fi
    done
fi
cat > "$STAGE/DO_NOT_SHARE.txt" <<'EOF'
Private tester pack. Contains files derived from Pioneer firmware for
ONE tester only (emulator snapshots, firmware image, screen captures).
Do not publish, re-upload, or share. Delete when done.
No flashing tools are included; everything here is RAM-only or preview-only.
EOF
OUT="$PWD/dist/XDJ700-tester-pack.zip"
rm -f "$OUT"
(cd "$STAGE" && zip -qr "$OUT" .)
echo "pack: $OUT ($(du -h "$OUT" | cut -f1))"
