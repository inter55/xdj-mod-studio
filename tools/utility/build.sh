#!/bin/sh
# Build the UTILITY mod rows: build/utility/ut.bin + ut.syms
# UT_BASE: in the gap between the hot cue module (0x09790000..0x09790D08)
# and the phase meter (0x09798000) -- deck-sampled zero in idle, loaded and
# playing states (CLAUDE.md sec. 77: 0x0972B000..0x0980F000).
set -e
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SH=$ROOT/work/sh-tools/bin
OUT=$ROOT/build/utility
BASE=${UT_BASE:-0x09794000}
mkdir -p "$OUT"
cd "$ROOT/tools/utility"
DEFS=$("$ROOT/.venv/bin/python" -c "
import sys; sys.path[:0] = ['$ROOT/tools', '$ROOT']
import mysettings as M, rgb_overview as RO, vredirect as VR, theme_switch as TS
d = dict(MSOFF=M.MSOFF, MSSAVE=M.MSSAVE, MODEB=RO.MODE_ADDR, VSELB=VR.VSEL,
         BRAKE_LIT=M.BRAKE_LIT, MSBRAKE=M.MSBRAKE, MSGATE=M.MSGATE,
         TS_BASE=TS.TS_BASE, TS_DATA=TS.TS_DATA, BRAKE_STOCK=M.BRAKE_STOCK)
pm = {l.split()[2].lstrip('_'): int(l.split()[0], 16)
      for l in open('$ROOT/build/phasemeter/pm.syms')}
d.update(PM_MODE=pm['pm_mode'], PM_PREV=pm['pm_prev'])
lt = {l.split()[2].lstrip('_'): int(l.split()[0], 16)
      for l in open('$ROOT/build/lighttext/lt.syms')}
d.update(LT_IDX=lt['lt_idx'])
print(' '.join(f'-D{k}={v:#x}u' for k, v in d.items()))")
"$SH/sh-elf-gcc" -m4-nofpu -mdiv=call-div1 -ml -O2 -Wall -ffreestanding -fno-builtin \
  $DEFS -ffunction-sections -fdata-sections -nostdlib -Wl,--gc-sections -Wl,-e,_ut_head \
  -Wl,--section-start=.entry=$BASE -Wl,-Ttext=$(printf '0x%X' $((BASE + 0x10))) \
  -o "$OUT/ut.elf" utentry.S ut.c -lgcc
"$SH/sh-elf-objcopy" -O binary -j .entry -j .text -j .rodata -j .data "$OUT/ut.elf" "$OUT/ut.bin"
"$SH/sh-elf-nm" "$OUT/ut.elf" | grep -E " (_ut_|_end$)" > "$OUT/ut.syms"
"$SH/sh-elf-size" "$OUT/ut.elf"
N=$("$SH/sh-elf-objdump" -d "$OUT/ut.elf" | awk -F'\t' 'NF>=3{print $3}' | grep -cE '^(f[a-z.]+|frchg|fschg)\s|fpscr|fpul' || true)
echo "FPU instructions: $N (must be 0)"; [ "$N" = 0 ]
END=$("$SH/sh-elf-nm" "$OUT/ut.elf" | awk '$3=="_end"{print $1}')
[ $((0x$END)) -le $((0x09798000)) ] || { echo "ERROR: end 0x$END reaches the phase meter"; exit 1; }
cat "$OUT/ut.syms"
