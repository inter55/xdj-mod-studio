#!/bin/sh
# bundle_qemu.sh DEST -- copy the lean QEMU build (the app's emulator, see
# docs/EMU_REBUILD_PLAN.md) plus exactly the non-system libraries it needs
# into DEST/, point every copy at its neighbours (@loader_path) and code-sign
# each one. DEST then runs on a Mac without Homebrew.
#
#   QEMU_BIN   default ~/qemu-build-fast/qemu-system-sh4 (release build:
#              the lean build's configure flags + --enable-lto
#              --disable-qom-cast-debug --disable-debug-info
#              -Dbuildtype=release; ~30 % more deck frames than the -O2 debug
#              build), else ~/qemu-build-lean
#   SIGN       codesign identity (default "-" = ad hoc)
#
# QEMU and this patch set are GPL-2.0-or-later: DEST also gets the licence,
# our patch and where the source lives (QEMU-SOURCE.txt).
set -eu
DEST=${1:?usage: bundle_qemu.sh DEST}
QEMU_BIN=${QEMU_BIN:-$HOME/qemu-build-fast/qemu-system-sh4}
[ -x "$QEMU_BIN" ] || QEMU_BIN=$HOME/qemu-build-lean/qemu-system-sh4
SIGN=${SIGN:--}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
[ -x "$QEMU_BIN" ] || { echo "no QEMU build at $QEMU_BIN" >&2; exit 1; }
rm -rf "$DEST"
mkdir -p "$DEST"
cp "$QEMU_BIN" "$DEST/qemu-system-sh4"
chmod u+w "$DEST/qemu-system-sh4"
# The audio-chip (C66x/C674x) core library QEMU loads at run time (its
# built-in default is the builder's ~/build/c6x path; the app passes
# C66X_JIT_LIB pointing here).
C66X=${C66X_LIB:-$HOME/build/c6x/libc66x.so}
[ -f "$C66X" ] || { echo "no $C66X (cdj-nxs2-qemu ./build.sh dsp)" >&2; exit 1; }
cp "$C66X" "$DEST/libc66x.so"
chmod u+w "$DEST/libc66x.so"


# Non-system dependencies of one Mach-O file (absolute Homebrew paths and
# @rpath entries resolved against /opt/homebrew/lib).
deps() {
    otool -L "$1" | tail -n +2 | awk '{print $1}' | while read -r d; do
        case "$d" in
            /usr/lib/*|/System/*) ;;
            @rpath/*) echo "/opt/homebrew/lib/${d#@rpath/}" ;;
            @loader_path/*|@executable_path/*) ;;
            *) echo "$d" ;;
        esac
    done
}

# Copy the dependency closure, breadth first.
todo="$DEST/qemu-system-sh4 $DEST/libc66x.so"
seen=""
while [ -n "$todo" ]; do
    next=""
    for f in $todo; do
        for d in $(deps "$f"); do
            real=$(python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$d")
            base=$(basename "$d")
            case " $seen " in *" $base "*) continue ;; esac
            seen="$seen $base"
            [ -f "$real" ] || { echo "missing library $d" >&2; exit 1; }
            cp "$real" "$DEST/$base"
            chmod u+w "$DEST/$base"
            next="$next $DEST/$base"
        done
    done
    todo=$next
done

# Rewrite every reference to point at the copies beside it.
for f in "$DEST"/qemu-system-sh4 "$DEST"/libc66x.so "$DEST"/*.dylib; do
    [ -f "$f" ] || continue
    case "$f" in *.dylib|*.so) install_name_tool -id "@loader_path/$(basename "$f")" "$f" ;; esac
    otool -L "$f" | tail -n +2 | awk '{print $1}' | while read -r d; do
        case "$d" in /usr/lib/*|/System/*|@loader_path/*) continue ;; esac
        install_name_tool -change "$d" "@loader_path/$(basename "$d")" "$f"
    done
done
# Leftover absolute rpaths would make the loader look in Homebrew first.
for f in "$DEST"/qemu-system-sh4 "$DEST"/libc66x.so "$DEST"/*.dylib; do
    otool -l "$f" | awk '/LC_RPATH/{getline;getline;print $2}' | while read -r r; do
        install_name_tool -delete_rpath "$r" "$f" 2>/dev/null || true
    done
done
for f in "$DEST"/*.dylib "$DEST"/libc66x.so "$DEST"/qemu-system-sh4; do
    [ -f "$f" ] || continue
    codesign --force --sign "$SIGN" "$f" >/dev/null 2>&1
done

# QEMU's VNC server needs its keymap (looked up as <-L dir>/keymaps/en-us)
KM=$(dirname "$QEMU_BIN")/qemu-bundle/usr/local/share/qemu/keymaps
mkdir -p "$DEST/share/keymaps"
cp "$KM/en-us" "$KM/common" "$KM/modifiers" "$DEST/share/keymaps/" 2>/dev/null ||
    cp "$KM/en-us" "$DEST/share/keymaps/"

# GPL obligations: licence, our changes, where the source is.
SRC=$HOME/Downloads/ref/cdj-qemu-spike
[ -f "$SRC/LICENSE" ] && cp "$SRC/LICENSE" "$DEST/LICENSE-QEMU.txt"
cp "$ROOT"/qemu/*.patch "$DEST/" 2>/dev/null || true
cat > "$DEST/QEMU-SOURCE.txt" <<EOF
This emulator is QEMU (GPL-2.0-or-later) with the CDJ/XDJ machine models from
https://github.com/Stijn-Jacobs/cdj-nxs2-qemu (GPL-2.0-or-later), commit
$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo unknown), plus our
changes in the .patch file(s) beside this note. The full source of the version
shipped here is that repository at that commit with the patch applied.
EOF

# Proof it no longer needs Homebrew.
if otool -L "$DEST"/qemu-system-sh4 "$DEST"/libc66x.so "$DEST"/*.dylib | grep -q "/opt/homebrew\|$HOME"; then
    echo "still points at Homebrew:" >&2
    otool -L "$DEST"/qemu-system-sh4 "$DEST"/*.dylib | grep /opt/homebrew >&2
    exit 1
fi
echo "bundled QEMU in $DEST: $(ls "$DEST" | wc -l | tr -d ' ') files, $(du -sh "$DEST" | cut -f1)"
