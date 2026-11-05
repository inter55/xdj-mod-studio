#!/bin/sh
# make_public_repo.sh [DEST] -- clean copy of the project for the PUBLIC
# GitHub repo, with a FRESH git history.
#
# Why: this working repo's history contains Pioneer firmware (stock
# MAIN.img, .UPD files, XDJ-1000 zips, 2026-09). Pushing it publicly would
# publish them even though they're deleted now. The public repo must start
# from a new history built from the files below.
#
# Copies only what git would track today (respects .gitignore), drops
# firmware-derived paths, then REFUSES if anything firmware-like remains
# (.UPD/.img/.pkl/.npz/zip, big binaries, decoded-image magic). Nothing is
# pushed: review DEST, then add the remote and push yourself.
set -e
cd "$(dirname "$0")/.."
DEST=${1:-../xdj700-mods-public}
if [ -e "$DEST" ]; then echo "ERROR: $DEST exists (pick a new folder)"; exit 1; fi
mkdir -p "$DEST"
# tracked + untracked-but-not-ignored files, minus deleted ones
git ls-files -co --exclude-standard -z | while IFS= read -r -d '' f; do
  [ -f "$f" ] || continue
  # tracked files listed in .gitignore (private RE notes, CLAUDE.md, ...)
  # still come out of ls-files -c: drop anything .gitignore names
  git check-ignore -q --no-index -- "$f" && continue
  case "$f" in
    work/*|cache/*|dist/*|screenshots/*|XDJ-1000/*|FIRMWARE/*) continue ;;
    build/*|docs/ti/*|docs/old/*|vendor/*.dylib) continue ;;   # artefacts, music, TI manual, flash-era docs
    *.wav|*.aif|*.aiff|*.mp3|*.flac|*.m4a|*.opus|*.x7s|*.pdf|*.dylib|*.so) continue ;;
    *.UPD|*.upd|*.img|*.pkl|*.pkl.gz|*.npz|*.npy|*.zip|*.bin|*.gbf|*.prp) continue ;;
    *.pre-*|*.bak|*.BACKUP|*.removed) continue ;;
    scratchpad/*|HANDOFF_*.md|USER_ISSUE*.md|wav-playback.md|REDDIT_POST.md|*.v1|*.v1-*|*.cab) continue ;;   # session scratch + internal notes
    .cloud-attachments/*|.freebuff/*|1/*|tmp/*|export/*) continue ;;   # agent/chat scratch, never project content
    preview/*|dis_boot.txt|errp.txt) continue ;;   # emulator screen grabs (firmware pixels), firmware disassembly
  esac
  mkdir -p "$DEST/$(dirname "$f")"
  cp -p "$f" "$DEST/$f"
done
# our own compiled deck code (SH-4 / C674x .bin + .syms; no firmware bytes):
# shipped apps have no cross-compiler, and the Windows CI build has none
# either, so the public repo carries the exact builds the Mac app ships
for f in phasemeter/pm.bin phasemeter/pm.syms hotcue8/hc.bin hotcue8/hc.syms \
         themesw/ts.bin themesw/ts.syms xflac/vfile.bin xflac/vfile.syms \
         stems/sm.bin stems/sm.syms keyshift/ksh.bin keyshift/ksh.syms \
         keyshift/ks.bin keyshift/ks.syms centre/cw.bin centre/cw.syms \
         utility/ut.bin utility/ut.syms xflac_flac/vfile.bin xflac_flac/vfile.syms \
         lighttext/lt.bin lighttext/lt.syms touchfx/tfx.bin touchfx/tfx.syms \
         fx/ksh.bin fx/ksh.syms fx/fx.bin fx/fx.syms \
         usbloader/ld.bin usbloader/ld.syms; do
  [ -f "build/$f" ] || { echo "ERROR: build/$f missing (build the deck code first)"; exit 1; }
  mkdir -p "$DEST/prebuilt/deck/$(dirname "$f")"
  cp -p "build/$f" "$DEST/prebuilt/deck/$f"
done
cat > "$DEST/prebuilt/README.md" <<'EOT'
# prebuilt/deck

Compiled deck code for the mods, built from this repo's own sources
(tools/hotcue8, tools/stems, tools/xflac, tools/themesw, tools/keyshift_dsp,
tools/centre, tools/phasemeter, tools/utility, tools/lighttext,
tools/touchfx, tools/fx_dsp) with the SH-4 / C674x GCC toolchains
(tools/build_sh_gcc.sh, tools/build_c6x_gcc.sh). The apps install these
bytes into the deck's memory; they contain no Pioneer firmware. Kept here
because the app builds (including the Windows CI build) have no
cross-compiler. The deck installers rebuild them when a toolchain exists.
EOT
# the private .gitignore names private notes and firmware files: the public
# repo gets its own short, generic one instead
cat > "$DEST/.gitignore" <<'EOT'
# Python / OS / other tools
.venv/
.freebuff/
__pycache__/
*.pyc
.DS_Store

# build outputs and local caches
dist/
build/
work/
cache/
screenshots/
emu/_fastcore.so
emu/c6x/_c6xcore.so
emu/_buildinfo.py

# Pioneer DJ firmware and anything made from it: never commit these.
# The app builds them on your Mac from your own official update file.
*.UPD
*.img
*.pkl
*.pkl.gz
*.npz
*.npy
studio/assets/screens.npz
EOT
# personal device identifiers: scrub from text files
grep -rlI --exclude-dir=.git -e 'FAMP8645''80YY' -e 'EGMP8614''87YY' "$DEST" 2>/dev/null |
  while read -r f; do sed -i '' -e 's/FAMP8645''80YY/<deck serial>/g' -e 's/EGMP8614''87YY/<deck serial>/g' "$f"; done
# --- scan: refuse on anything firmware-like ---
BAD=0
find "$DEST" -type f -size +2M | while read -r f; do echo "BIG FILE: $f"; done > /tmp/.pubscan.$$
find "$DEST" -type f \( -iname '*.upd' -o -iname '*.img' -o -name '*.pkl*' -o -name '*.npz' -o -iname '*.zip' \) >> /tmp/.pubscan.$$
# firmware signature strings that only occur inside the images
# (binary files only: docs that MENTION a firmware string are fine)
grep -rl --exclude-dir=.git -e 'Cente USBH' -e 'PnlCom_SndTASK' "$DEST" 2>/dev/null |
  while read -r f; do
    file --mime-encoding "$f" | grep -q binary && echo "FIRMWARE STRINGS: $f"
  done >> /tmp/.pubscan.$$ || true
grep -rlI --exclude-dir=.git -e 'gill''banks' -e 'Jam''esG' "$DEST" 2>/dev/null |
  while read -r f; do echo "PERSONAL INFO: $f"; done >> /tmp/.pubscan.$$ || true
if [ -s /tmp/.pubscan.$$ ]; then
  echo "REFUSED: firmware-like files in the export:"; cat /tmp/.pubscan.$$; BAD=1
fi
rm -f /tmp/.pubscan.$$
[ "$BAD" = 0 ] || exit 1
cd "$DEST"
git init -q -b master
git add -A
echo "clean export ready: $DEST ($(git ls-files | wc -l | tr -d ' ') files, fresh history, not committed)"
echo "next: review it, commit, add the GitHub remote, push"
