#!/bin/sh
# Build the live stem mixer: build/stems/sm.bin + sm.syms
set -e
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SH=$ROOT/work/sh-tools/bin
OUT=${SM_OUT:-$ROOT/build/stems}     # SM_OUT: build a variant beside the deck build
BASE=${SM_BASE:-0x09836000}
mkdir -p "$OUT"
cd "$ROOT/tools/stems"
"$SH/sh-elf-gcc" -m4-nofpu -mdiv=call-div1 -ml -O2 -Wall -ffreestanding -fno-builtin \
  -DPMASK=${PMASK:?}u ${SM_CFLAGS:-} \
  -ffunction-sections -fdata-sections -nostdlib -Wl,--gc-sections -Wl,-e,_sm_table \
  -Wl,--section-start=.entry=$BASE -Wl,-Ttext=$(printf '0x%X' $((BASE + 0x10))) \
  -o "$OUT/sm.elf" smhook.S sm.c -lgcc
"$SH/sh-elf-objcopy" -O binary -j .entry -j .text -j .rodata -j .data "$OUT/sm.elf" "$OUT/sm.bin"
"$SH/sh-elf-nm" "$OUT/sm.elf" | grep -E " (_sm_|_end$)" > "$OUT/sm.syms"
"$SH/sh-elf-size" "$OUT/sm.elf"
N=$("$SH/sh-elf-objdump" -d "$OUT/sm.elf" | awk -F'\t' 'NF>=3{print $3}' | grep -cE '^(f[a-z.]+|frchg|fschg)\s|fpscr|fpul' || true)
echo "FPU instructions: $N (must be 0)"; [ "$N" = 0 ]
# the cave ends where TOUCH FX begins
E=$(awk '$3=="_end"{print $1}' "$OUT/sm.syms")
[ $((0x$E)) -le $((${SM_LIMIT:-0x09844000})) ] || { echo "stems module too big: ends 0x$E"; rm -f "$OUT/sm.bin" "$OUT/sm.elf" "$OUT/sm.syms"; exit 1; }
echo "module ends 0x$E (limit ${SM_LIMIT:-0x09844000})"
cat "$OUT/sm.syms"
