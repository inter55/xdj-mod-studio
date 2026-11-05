#!/bin/sh
# Build the live stem mixer: build/stems/sm.bin + sm.syms
set -e
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SH=$ROOT/work/sh-tools/bin
OUT=$ROOT/build/stems
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
cat "$OUT/sm.syms"
