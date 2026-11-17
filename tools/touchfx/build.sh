#!/bin/sh
# Build the TOUCH FX panel cave: build/touchfx/tfx.bin + tfx.syms
# TFX_BASE default 0x09844000 (after the stems module, inside the
# all-states-zero band 0x09836000..0x0996D000, CLAUDE.md sec. 77; still
# needs a read-only deck survey before a deck install). Must end
# before 0x09850000 (theme maps).
set -e
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SH=$ROOT/work/sh-tools/bin
OUT=$ROOT/build/touchfx
BASE=${TFX_BASE:-0x09844000}
mkdir -p "$OUT"
cd "$ROOT/tools/touchfx"
"$ROOT/.venv/bin/python" mkglyphs.py >/dev/null
eval $(cd "$ROOT" && "$ROOT/.venv/bin/python" -c "
import sys; sys.path[:0] = ['tools', '.']
import perform_pads as PP
print('PST=%#x MSOFF=%#x' % (PP.PST, PP.MSOFF))")
# FX_ROLL=1 / FX_REVERB=1: the panel must match the DSP engine it is installed
# with, or the grid offers an effect the blob does not implement.
EXTRA=""
[ -n "$FX_ROLL" ] && EXTRA="$EXTRA -DFX_ROLL=1"
[ -n "$FX_REVERB" ] && EXTRA="$EXTRA -DFX_REVERB=1"
[ -n "$FX_PITCH" ] && EXTRA="$EXTRA -DFX_PITCH=1"
[ -n "$REDRAW_MS" ] && EXTRA="$EXTRA -DREDRAW_MS=${REDRAW_MS}u"   # tests: 0 = no repaint limit
# LIGHT theme: flip the panel's neutral colours while the light add-on is on
LTA=$(awk '$3=="_lt_active"{print "0x"$1}' "$ROOT/build/lighttext/lt.syms" 2>/dev/null)
[ -n "$LTA" ] && EXTRA="$EXTRA -DLT_ACTIVE=${LTA}u"
#   # engine: FX_PITCH=1 FX_NO_RING=1
"$SH/sh-elf-gcc" -DPST=${PST}u -DMSOFF=${MSOFF}u $EXTRA -m4-nofpu -mdiv=call-div1 -ml -O2 -Wall \
  -ffreestanding -fno-builtin -ffunction-sections -fdata-sections -nostdlib -Wl,--gc-sections \
  -Wl,-e,_tfx_flush_entry -Wl,--section-start=.entry=$BASE \
  -Wl,-Ttext=$(printf '0x%X' $((BASE + 0x80))) \
  -o "$OUT/tfx.elf" tfxhook.S tfx.c -lgcc
"$SH/sh-elf-objcopy" -O binary -j .entry -j .text -j .rodata -j .data "$OUT/tfx.elf" "$OUT/tfx.bin"
"$SH/sh-elf-nm" "$OUT/tfx.elf" | grep -E " _tfx_|_end$" > "$OUT/tfx.syms"
"$SH/sh-elf-size" "$OUT/tfx.elf"
END=$(awk '$3=="_end"{print "0x"$1}' "$OUT/tfx.syms")
[ $((END)) -le $((0x09850000)) ] || { echo "cave too big: ends $END"; exit 1; }
N=$("$SH/sh-elf-objdump" -d "$OUT/tfx.elf" | awk -F'\t' 'NF>=3{print $3}' | grep -cE '^(f[a-z.]+|frchg|fschg)\s|fpscr|fpul' || true)
echo "FPU instructions: $N (must be 0)"; [ "$N" = 0 ]
echo "end $END"; ls -l "$OUT/tfx.bin"
