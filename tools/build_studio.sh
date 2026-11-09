#!/bin/sh
# Build dist/XDJ Mod Studio.app (native Mac app around the studio server).
# Personal-use bundle: the frozen server reads/writes the repo it was
# built from (baked path below); firmware files stay in the repo, never
# in the bundle. Needs the repo .venv (numpy/PIL), Homebrew hidapi
# (bundled for the deck USB shell) and Xcode CLT (codesign).
set -e
cd "$(dirname "$0")/.."
# never bake the build folder into the app (it carries the builder's user
# name): emu/_buildinfo.py is only for the old emulator app (build_app.sh)
if [ -f emu/_buildinfo.py ]; then
  mv emu/_buildinfo.py build/_buildinfo.py.hold
  trap 'mv build/_buildinfo.py.hold emu/_buildinfo.py 2>/dev/null || true' EXIT
fi
printf 'import os\nos.environ.setdefault("XDJ_REPO_BAKED", "%s")\n' "$(pwd)" \
  > build/studio-app-hook.py
mkdir -p build/studio-app
# DSP C core, freshly built so the frozen app never needs a compiler
# (cfast skips its `cc` rebuild when the .so is newer than the source;
# build_lib adds the play-mode PGO profile, ~+20% at real speed)
if [ emu/c6x/c6xcore.c -nt emu/c6x/_c6xcore.so ] ||
   { [ -f emu/c6x/c6xcore.profdata ] && [ emu/c6x/c6xcore.profdata -nt emu/c6x/_c6xcore.so ]; }; then
  .venv/bin/python -c 'import emu.c6x.cfast as cf; cf.build_lib(cf.LIB)'
fi
.venv/bin/python -m PyInstaller --noconfirm XDJStudio.spec --distpath dist --workpath build/studio-app
# Bundled libSDL2 aborts inside pygame.base init (stack-chk fail); the
# Homebrew sdl2-compat library loads cleanly, so point the bundle at it.
# (Personal-use bundle: this Mac's Homebrew path is fine.)
for _sdl in "$PWD"/dist/XDJ\ Mod\ Studio/_internal/libSDL2-2.0.0.dylib \
            "$PWD"/dist/XDJ\ Mod\ Studio/_internal/libSDL2.dylib; do
  if [ -f "$_sdl" ] && [ ! -L "$_sdl" ]; then
    _sys=$(ls /opt/homebrew/opt/sdl2-compat/lib/libSDL2-2.0.0.dylib 2>/dev/null || true)
    # a real COPY (not a symlink: a link into /opt/homebrew dangles on
    # tester Macs and breaks the bundle signature); it finds the bundled
    # SDL3 at runtime (copied below)
    if [ -n "$_sys" ]; then rm -f "$_sdl"; cp "$_sys" "$_sdl"; chmod u+w "$_sdl"; codesign --force -s - "$_sdl"; fi
  fi
done
codesign --force --deep -s - "dist/XDJ Mod Studio"
# Native wrapper: Swift/AppKit window (Dock icon, menus) hosting the
# frozen server above as a headless child process. Replaces the
# PyInstaller .app in dist (its whole one-dir output travels inside
# Resources/server/, since the exe needs its _internal/ siblings).
SRVDIR="$(find dist -maxdepth 2 -name "_internal" -type d | head -1)"
SRVDIR="$(dirname "$SRVDIR")"
APPDIR="dist/XDJ-Mod-Studio-native"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/Contents/MacOS" "$APPDIR/Contents/Resources"
swiftc -O -target arm64-apple-macosx14.0 -o "$APPDIR/Contents/MacOS/XDJ Mod Studio" tools/studio-shell.swift \
  -framework AppKit -framework WebKit
cp -R "$SRVDIR/." "$APPDIR/Contents/Resources/server/"
# The bundled libSDL2 is Homebrew's sdl2-compat shim, which loads SDL3 at
# runtime and ABORTS (LoadSDL3 -> DynApiExitProcess) when it can't: tester
# Macs have no Homebrew, and even here the frozen app didn't find it. Ship
# SDL3 next to the shim (only system-framework deps; verified 2026-10-11).
_SDL3=$(ls /opt/homebrew/opt/sdl3/lib/libSDL3.0.dylib 2>/dev/null || true)
if [ -z "$_SDL3" ]; then echo "ERROR: Homebrew sdl3 missing (brew install sdl3)"; exit 1; fi
for _n in libSDL3.0.dylib libSDL3.dylib; do
  _d="$APPDIR/Contents/Resources/server/_internal/$_n"
  cp "$_SDL3" "$_d"; chmod u+w "$_d"
  install_name_tool -id "@rpath/$_n" "$_d" 2>/dev/null || true
  # install_name_tool breaks the dylib's own signature and macOS then
  # SIGKILLs the process on load (exit 137); --deep does not re-sign
  # dylibs that live under Resources, so sign each one explicitly
  codesign --force -s - "$_d"
done
# Portable repo copy: the shipped app must run on other Macs with no repo
# around (tester builds). Same filter as tools/make_tester_pack.sh.
REPO="$APPDIR/Contents/Resources/repo"
mkdir -p "$REPO/cache" "$REPO/work"
for d in tools mods emu studio vendor; do cp -R "$d" "$REPO/"; done
# vendor/ = the MIT rekordbox-pdb package: tools/stems_replace.py (MP3/M4A/FLAC
# replacing the track), tools/filter_playlists.py and tools/onelibrary_to_pdb.py
# all import it, and 1.0.1 shipped without it -- those features died on an
# ImportError in the released app. tools/app_bundle_check.py fails a build that
# drops it again.
# our own compiled deck code (no firmware bytes): Type 1 phase meter
# BUILT here, like xflac/utility/hotcue8 below. It used to be copied from
# build/phasemeter/pm.bin if one happened to exist, and the meter's own gate
# also never rebuilt -- so a source fix could miss a release and the gate
# would keep passing against the old binary.
sh tools/phasemeter/build.sh >/dev/null
mkdir -p "$REPO/build/phasemeter"
cp build/phasemeter/pm.bin build/phasemeter/pm.syms "$REPO/build/phasemeter/"
# FLAC-only decoder build (Studio installs it unless the stick has .opus)
VF_OPUS=0 VF_OUT="$PWD/build/xflac_flac" sh tools/xflac/build_vfile.sh >/dev/null
mkdir -p "$REPO/build/xflac_flac"
cp build/xflac_flac/vfile.bin build/xflac_flac/vfile.syms "$REPO/build/xflac_flac/"
# UTILITY mod rows (our C). utility/build.sh reads build/lighttext/lt.syms
# (the THEME row's LIGHT entry), so light-text is built FIRST: a clean
# checkout has no build/ and failed here (found building 1.0.5).
sh tools/lighttext/build.sh >/dev/null
sh tools/utility/build.sh >/dev/null
mkdir -p "$REPO/build/utility"
cp build/utility/ut.bin build/utility/ut.syms "$REPO/build/utility/"
# 8 hot cue pages (our C; build needs the SH compiler on this Mac)
HCPAGE=$(.venv/bin/python -c "import sys;sys.path[:0]=['tools'];import perform_pads as P;print(hex(P.HCPAGE))")
PMODE=$(.venv/bin/python -c "import sys;sys.path[:0]=['tools'];import perform_pads as P;print(hex(P.PST+2))")
MSOFF=$(.venv/bin/python -c "import sys;sys.path[:0]=['tools'];import mysettings as M;print(hex(M.MSOFF))")
HCPAGE=$HCPAGE PMODE=$PMODE MSOFF=$MSOFF sh tools/hotcue8/build.sh >/dev/null
mkdir -p "$REPO/build/hotcue8"
cp build/hotcue8/hc.bin build/hotcue8/hc.syms "$REPO/build/hotcue8/"
# on-deck theme switching: our C always ships; the colour tables carry
# artwork fingerprints, so a PUBLIC build leaves them to first-run setup
# (emu/fwhome.py builds them from the user's own firmware)
mkdir -p "$REPO/build/themesw"
if [ "$PUBLIC" = 1 ]; then
  MSTNAME=$(.venv/bin/python -c "import sys;sys.path[:0]=['tools'];import mysettings as M;print(hex(M.MSTNAME))") \
  MSTAPP=$(.venv/bin/python -c "import sys;sys.path[:0]=['tools'];import mysettings as M;print(hex(M.MSTAPP))") \
    sh tools/themesw/build.sh >/dev/null
  cp build/themesw/ts.bin build/themesw/ts.syms "$REPO/build/themesw/"
  printf '0x9770000 0x9779000\n' > "$REPO/build/themesw/layout.txt"
else
  .venv/bin/python tools/theme_switch.py export >/dev/null
  cp build/themesw/ts.bin build/themesw/ts.syms build/themesw/tables.bin build/themesw/layout.txt "$REPO/build/themesw/"
fi
# FLAC + Opus virtual-WAV layer (our C): the tester app plays FLAC/Opus on
# sticks like WAV (emu app install = deck step 5h bytes). Built, not copied:
# build/xflac/vfile.bin is what the deck loads, and a prebuilt copy here once
# let the source move on without the shipped module following it.
sh tools/xflac/build_vfile.sh >/dev/null
mkdir -p "$REPO/build/xflac"
cp build/xflac/vfile.bin build/xflac/vfile.syms "$REPO/build/xflac/"
# live stems mixer + live key shift (our C; the deck installers read these)
# -- 2026-10-18: were missing, so Studio's stems/key-shift deck steps failed.
# sm.bin needs PMASK (the PERFORM pad mask), same value the emu test passes.
PMASK=$(.venv/bin/python -c "import sys;sys.path[:0]=['tools'];import perform_pads as P;print(hex(P.PMASK))")
PMASK=$PMASK sh tools/stems/build.sh >/dev/null
# DSP-side modules need the C6x toolchain (here: work/tic6x-tools). A machine
# without it falls through to the freshness guard below, which refuses a stale
# copy instead of shipping it.
#   ks.bin  key-shift engine -- build_ksh.sh embeds its ks.syms, so it must be
#           built FIRST or the installer cave is linked against an old engine
#   fx.bin  TOUCH FX engine (this one was found stale on 2026-11-08)
if [ -d work/tic6x-tools/bin ]; then
  sh tools/keyshift_dsp/build_ksr.sh >/dev/null
  sh tools/fx_dsp/build.sh >/dev/null
fi
sh tools/keyshift_dsp/build_ksh.sh >/dev/null
mkdir -p "$REPO/build/stems" "$REPO/build/keyshift"
cp build/stems/sm.bin build/stems/sm.syms "$REPO/build/stems/"
cp build/keyshift/ksh.bin build/keyshift/ksh.syms build/keyshift/ks.bin build/keyshift/ks.syms "$REPO/build/keyshift/"
# The app's emulator: QEMU (GPL) + its libraries, Homebrew-free (QEMU_BIN
# overrides the build). Firmware image + DSP module are NOT here: they come
# from the user's own update file (emu/fwhome.py).
sh tools/bundle_qemu.sh "$REPO/qemu-bin"
# centre waveform colour module (our C): RGB / 3-band per-row colours
.venv/bin/python -c "import sys;sys.path[:0]=['.','tools'];import rgb_detail as R;R.cw_build()"
mkdir -p "$REPO/build/centre"
cp build/centre/cw.bin build/centre/cw.syms "$REPO/build/centre/"
# light-mode text add-on, TOUCH FX screen + its DSP engine cave (our C;
# 2026-11-01: never shipped, so deck steps 5l / 5x failed in the apps --
# the same packaging gap Reddit hit for build/centre in 0.9.x). Built here
# too; until now these were only checked for existence, so a source fix
# could sit unwrapped in a release forever.
sh tools/lighttext/build.sh >/dev/null
sh tools/touchfx/build.sh >/dev/null
sh tools/usbloader/build.sh >/dev/null
for f in lighttext/lt.bin lighttext/lt.syms touchfx/tfx.bin touchfx/tfx.syms \
         fx/ksh.bin fx/ksh.syms fx/fx.bin fx/fx.syms fx/ring.txt \
         usbloader/ld.bin usbloader/ld.syms; do
  [ -f "build/$f" ] || { echo "ERROR: build/$f missing"; exit 1; }
  mkdir -p "$REPO/build/$(dirname "$f")"
  cp "build/$f" "$REPO/build/$f"
done
# Nothing stale ships. Every deck module above is loaded at a fixed address
# and is expected to match the C it came from: a prebuilt pm.bin once shipped
# while pm.c had moved on, and the meter's own gate kept passing because it
# installs the binary from disk. Builds that need a toolchain this script does
# not drive (keyshift's DSP ks.bin -- tools/keyshift_dsp/build_ksr.sh, C6x)
# are still checked here, so they fail loudly instead of shipping.
stale() {   # stale <shipped file> <source file or dir> [source...]
  _b=$1; shift
  [ -f "$_b" ] || { echo "ERROR: $_b missing"; return 1; }
  for _d in "$@"; do
    if [ -f "$_d" ]; then
      _list=$_d                      # one named source: no globbing
    else
      _list=$(find "$_d" -maxdepth 1 \( -name '*.c' -o -name '*.S' \
              -o -name '*.h' \) 2>/dev/null)
    fi
    for _s in $_list; do
      if [ "$_s" -nt "$_b" ]; then
        echo "ERROR: $_b is older than $_s -- rebuild it before packaging"
        return 1
      fi
    done
  done
}
_stale=0
stale build/phasemeter/pm.bin          tools/phasemeter     || _stale=1
stale build/hotcue8/hc.bin             tools/hotcue8        || _stale=1
stale build/utility/ut.bin             tools/utility        || _stale=1
stale build/stems/sm.bin               tools/stems          || _stale=1
stale build/xflac/vfile.bin            tools/xflac          || _stale=1
stale build/centre/cw.bin              tools/centre         || _stale=1
stale build/keyshift/ksh.bin           tools/keyshift_dsp   || _stale=1
stale build/keyshift/ks.bin            tools/keyshift_dsp/ks.c || _stale=1
stale build/lighttext/lt.bin           tools/lighttext      || _stale=1
stale build/touchfx/tfx.bin            tools/touchfx        || _stale=1
# fx.bin is the DSP-side engine (tools/fx_dsp/build.sh, C6x toolchain),
# not the SH panel built from tools/touchfx.
stale build/fx/fx.bin                  tools/fx_dsp         || _stale=1
stale build/usbloader/ld.bin           tools/usbloader      || _stale=1
if [ "$_stale" != 0 ]; then
  echo "REFUSING to package: a deck module above does not match its sources."
  exit 1
fi
cp TESTER_README.md "$REPO/" 2>/dev/null || true
if [ "$PUBLIC" != 1 ]; then   # private tester build: firmware baked in
  cp work/MAIN.img "$REPO/work/"
  cp studio/assets/screens.npz "$REPO/studio/assets/" 2>/dev/null || true
fi
# Boot snapshot travels gzip'd (the emulator loads .pkl.gz directly):
# emu_mainscreen.pkl is 136 MB raw / ~11 MB gzipped. The other cache
# snapshots (emu_realusb*, emu_track_loaded) are dev-tool inputs and
# never ship.
if [ "$PUBLIC" = 1 ]; then :
elif [ -f "cache/emu_mainscreen.pkl.gz" ]; then cp "cache/emu_mainscreen.pkl.gz" "$REPO/cache/"
elif [ -f "cache/emu_mainscreen.pkl" ]; then gzip -9 -c "cache/emu_mainscreen.pkl" > "$REPO/cache/emu_mainscreen.pkl.gz"; fi
# Real-DSP snapshot (its own firmware choice; ships in PRIVATE builds only --
# firmware-derived, same rule as MAIN.img above).
if [ "$PUBLIC" != 1 ] && [ -f "cache/emu_dsp_sound.pkl.gz" ]; then
  cp "cache/emu_dsp_sound.pkl.gz" "$REPO/cache/"
fi
# the C6x C core needs its SOURCE (cfast.py re-reads the OP list) + the
# compiled lib; ship both (our own code, no firmware bytes)
mkdir -p "$REPO/emu/c6x"
cp emu/c6x/c6xcore.c "$REPO/emu/c6x/" 2>/dev/null || true
# the DSP path decodes through tic6x objdump (tool binary, not firmware)
if [ -f work/tic6x/objdump ]; then mkdir -p "$REPO/work/tic6x"; cp work/tic6x/objdump "$REPO/work/tic6x/"; fi
find "$REPO" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$REPO" -name "*.pyc" -delete 2>/dev/null || true
find "$REPO" -name ".DS_Store" -delete 2>/dev/null || true
find "$REPO" \( -name "*.pre-*" -o -name "*.bak" -o -name "*.orig" \) -delete 2>/dev/null || true
# debug-symbol bundles (cc -g leftovers) carry the builder's paths
find "$REPO" -name "*.dSYM" -type d -prune -exec rm -rf {} + 2>/dev/null || true
rm -f "$REPO"/studio/*.BACKUP "$REPO"/tools/*.bak
# No source in the shipped app: compile every module to a sourceless .pyc
# beside it (same Python as the frozen server, so imports + --run-tool
# work), then drop the .py and all other source/build files. Exception:
# mods/themes.py is the user's theme list (Theme Builder appends to it).
.venv/bin/python -m compileall -b -q "$REPO" >/dev/null
find "$REPO" -name "*.py" ! -path "$REPO/mods/themes.py" -delete
rm -f "$REPO"/mods/themes.pyc
find "$REPO" \( -name "*.c" ! -name "c6xcore.c" -o -name "*.h" -o -name "*.swift" -o -name "*.sh" \
  -o -name "*.removed" -o -name "*.spec" -o -name "*.md" ! -name TESTER_README.md \) -delete
rm -rf "$REPO"/emu/icon.iconset
if [ "$PUBLIC" = 1 ]; then          # no firmware pixels in a public build
  rm -f "$REPO"/tools/data/*.npy "$REPO"/tools/data/*.npz "$REPO"/studio/assets/screens.npz
  rm -rf "$REPO"/cache/*.pkl* "$REPO"/work/*
  rm -f "$REPO"/emu/assets/pioneer_dj_white.png "$REPO"/emu/assets/rekordbox_white.png
fi
# the DSP decoder binary is our own tool (not firmware): it must survive
# the wipe above -- the real-audio snapshot setup needs it on first run
if [ -f work/tic6x/objdump ]; then mkdir -p "$REPO/work/tic6x"; cp work/tic6x/objdump "$REPO/work/tic6x/"; fi
[ "$PUBLIC" = 1 ] || cat > "$REPO/DO_NOT_SHARE.txt" <<'EOF'
Private tester app content. Contains files derived from Pioneer firmware
for ONE tester only. Do not publish, re-upload, or share.
EOF
cp tools/Studio-Info.plist "$APPDIR/Contents/Info.plist"
# Finder > Get Info showed "Version 1" because the template carries no
# CFBundleShortVersionString: stamp the app's own number in
_VER=$(sed -n 's/^APP_VERSION = "\([^"]*\)"$/\1/p' studio/app.py)
[ -n "$_VER" ] || { echo "ERROR: no APP_VERSION in studio/app.py"; exit 1; }
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $_VER" \
  "$APPDIR/Contents/Info.plist" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $_VER" \
     "$APPDIR/Contents/Info.plist"
if [ -f emu/icon.icns ]; then cp emu/icon.icns "$APPDIR/Contents/Resources/AppIcon.icns"; fi
# pip install records hold the build Mac's folder path: not needed at runtime
find "$APPDIR" -name direct_url.json -path "*dist-info*" -delete 2>/dev/null || true
# build paths (the builder's home folder) out of every file, then refuse if
# the user name is still anywhere (compressed modules included)
find "$APPDIR" -name "_buildinfo.py*" -delete 2>/dev/null || true
.venv/bin/python tools/scrub_app.py "$APPDIR"
codesign --force --deep -s - "$APPDIR"
# refuse to call this build finished if it cannot do its job: 1.0.1 shipped
# the stems replacement without vendor/rekordbox_pdb and died on ImportError
.venv/bin/python tools/app_bundle_check.py "$APPDIR"
FINAL="dist/XDJ Mod Studio.app"
[ "$PUBLIC" = 1 ] && { mkdir -p dist/public; FINAL="dist/public/XDJ Mod Studio.app"; }
rm -rf "$FINAL"
mv "$APPDIR" "$FINAL"
codesign --force --deep -s - "$FINAL"
rm -rf "dist/XDJ Mod Studio"        # the onedir intermediate (80 MB); the
                                    # .app carries its own copy in server/
echo "built $FINAL (native shell + bundled server)"
