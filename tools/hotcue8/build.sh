#!/bin/sh
# Build the 8-hot-cue pages: build/hotcue8/hc.bin + hc.syms
# HC_BASE needs a read-only deck survey before any deck install.
set -e
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SH=$ROOT/work/sh-tools/bin
OUT=$ROOT/build/hotcue8
BASE=${HC_BASE:-0x09790000}
mkdir -p "$OUT"
cd "$ROOT/tools/hotcue8"
"$ROOT/.venv/bin/python" mkglyphs.py >/dev/null
"$SH/sh-elf-gcc" -m4-nofpu -mdiv=call-div1 -ml -O2 -Wall -ffreestanding -fno-builtin \
  -DHCPAGE=${HCPAGE:?}u -DPMODE=${PMODE:?}u -DMSOFF=${MSOFF:?}u \
  -ffunction-sections -fdata-sections -nostdlib -Wl,--gc-sections -Wl,-e,_hc_entry \
  -Wl,--section-start=.entry=$BASE -Wl,-Ttext=$(printf '0x%X' $((BASE + 0x80))) \
  -o "$OUT/hc.elf" hchook.S hc.c -lgcc
"$SH/sh-elf-objcopy" -O binary -j .entry -j .text -j .rodata -j .data "$OUT/hc.elf" "$OUT/hc.bin"
"$SH/sh-elf-nm" "$OUT/hc.elf" | grep -E " (_hc_|_end$)" > "$OUT/hc.syms"
"$SH/sh-elf-size" "$OUT/hc.elf"
N=$("$SH/sh-elf-objdump" -d "$OUT/hc.elf" | awk -F'\t' 'NF>=3{print $3}' | grep -cE '^(f[a-z.]+|frchg|fschg)\s|fpscr|fpul' || true)
echo "FPU instructions: $N (must be 0)"; [ "$N" = 0 ]
# POS150 is DSP-written; a cached P3 literal stores deck-set cues at a
# stale playhead. Only the literal check can catch that (no cache in emu).
if ! "$SH/sh-elf-objdump" -d "$OUT/hc.elf" | grep -q 'ac0e6d18'; then
  echo "ERROR: POS150 is not read through the uncached P2 alias (ac0e6d18)" >&2
  exit 1
fi
cat "$OUT/hc.syms"
