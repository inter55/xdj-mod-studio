#!/bin/sh
# READ-ONLY chip memory-size probe cave: build/ddrprobe/dp.bin at DP_BASE
# (default 0x09800000 = the key-shift one-shot area, deck-surveyed).
set -e
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SH=$ROOT/work/sh-tools/bin
OUT=$ROOT/build/ddrprobe
BASE=${DP_BASE:-0x09800000}
mkdir -p "$OUT"; cd "$ROOT/tools/ddrprobe"
"$SH/sh-elf-gcc" -m4-nofpu -mdiv=call-div1 -ml -O2 -Wall -ffreestanding -fno-builtin \
  -ffunction-sections -fdata-sections -nostdlib -Wl,--gc-sections -Wl,-e,_dp_entry \
  -Wl,--section-start=.entry=$BASE -Wl,-Ttext=$(printf '0x%X' $((BASE + 0x40))) \
  -o "$OUT/dp.elf" dphook.S dp.c -lgcc
"$SH/sh-elf-objcopy" -O binary -j .entry -j .text -j .rodata -j .data "$OUT/dp.elf" "$OUT/dp.bin"
"$SH/sh-elf-nm" "$OUT/dp.elf" | grep -E " _dp_" > "$OUT/dp.syms"
N=$("$SH/sh-elf-objdump" -d "$OUT/dp.elf" | awk -F'\t' 'NF>=3{print $3}' | grep -cE '^(f[a-z.]+|frchg|fschg)\s|fpscr|fpul' || true)
echo "FPU instructions: $N (must be 0)"; [ "$N" = 0 ]
cat "$OUT/dp.syms"; ls -l "$OUT/dp.bin"
