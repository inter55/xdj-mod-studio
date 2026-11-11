#!/bin/sh
# Build the Type 1 phase meter cave: build/phasemeter/pm.bin + pm.syms
# PM_BASE = cave address (default 0x09798000: deck-sampled empty 2026-10-12; a deck
# address needs a read-only deck survey first).
set -e
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SH=$ROOT/work/sh-tools/bin
OUT=$ROOT/build/phasemeter
BASE=${PM_BASE:-0x09798000}
mkdir -p "$OUT"
cd "$ROOT/tools/phasemeter"
"$ROOT/.venv/bin/python" mklabel.py >/dev/null
TS_DATA=$("$ROOT/.venv/bin/python" -c "
import sys; sys.path[:0] = ['$ROOT/tools', '$ROOT', '$ROOT/mods']
import theme_switch as TS; print(hex(TS.TS_DATA))")
"$SH/sh-elf-gcc" -DTS_DATA=${TS_DATA}u -m4-nofpu -mdiv=call-div1 -ml -O2 -Wall -ffreestanding -fno-builtin \
  -ffunction-sections -fdata-sections -nostdlib -Wl,--gc-sections -Wl,-e,_pm_entry \
  -Wl,--section-start=.entry=$BASE -Wl,-Ttext=$(printf '0x%X' $((BASE + 0x40))) \
  -o "$OUT/pm.elf" pmhook.S pm.c -lgcc
"$SH/sh-elf-objcopy" -O binary -j .entry -j .text -j .rodata -j .data "$OUT/pm.elf" "$OUT/pm.bin"
"$SH/sh-elf-nm" "$OUT/pm.elf" | grep -E " (_pm_|_label)" > "$OUT/pm.syms"
"$SH/sh-elf-size" "$OUT/pm.elf"
N=$("$SH/sh-elf-objdump" -d "$OUT/pm.elf" | awk -F'\t' 'NF>=3{print $3}' | grep -cE '^(f[a-z.]+|frchg|fschg)\s|fpscr|fpul' || true)
echo "FPU instructions: $N (must be 0)"; [ "$N" = 0 ]
# POSL is written by the DSP side without invalidating the SH-4 D-cache.
# A cached P3 read redraws the same old position forever on a real deck;
# the emulator has no cache, so only this literal check catches a revert.
if ! "$SH/sh-elf-objdump" -d "$OUT/pm.elf" | grep -q 'ac0e6d18'; then
  echo "ERROR: POSL is not read through the uncached P2 alias (ac0e6d18)" >&2
  exit 1
fi
cat "$OUT/pm.syms"
