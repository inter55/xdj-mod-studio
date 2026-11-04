#!/bin/sh
# Stage-2 installer (the MODS.BIN payload): build/usbloader/s2.bin + s2.syms.
# Linked at the P2 (uncached) alias of UL_PAY, where ul.c calls it, so code
# and its own data never go through the caches; fread targets and ocbp use
# the P0 address (s2.c P0()).
set -e
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SH=$ROOT/work/sh-tools/bin
OUT=$ROOT/build/usbloader
BASE=${UL_BASE:-0x09890000}
PAY=$((BASE + 0x1000))
PEND=$((BASE + 0x10000))
LINK=$(printf '0x%X' $((PAY | 0xA0000000)))
TEXT=$(printf '0x%X' $(((PAY | 0xA0000000) + 0x10)))
mkdir -p "$OUT"
cd "$ROOT/tools/usbloader"
"$SH/sh-elf-gcc" -m4-nofpu -mdiv=call-div1 -ml -O2 -Wall -ffreestanding -fno-builtin \
  -ffunction-sections -fdata-sections -nostdlib -Wl,--gc-sections -Wl,-e,_s2_head \
  -Wl,--section-start=.entry=$LINK -Wl,-Ttext=$TEXT \
  -o "$OUT/s2.elf" s2entry.S s2.c -lgcc
"$SH/sh-elf-objcopy" -O binary -j .entry -j .text -j .rodata -j .data "$OUT/s2.elf" "$OUT/s2.bin"
"$SH/sh-elf-nm" "$OUT/s2.elf" | grep -E " _(s2_|end$|stage$)" > "$OUT/s2.syms"
"$SH/sh-elf-size" "$OUT/s2.elf"
D=$("$SH/sh-elf-objdump" -d "$OUT/s2.elf")
N=$(printf '%s\n' "$D" | awk -F'\t' 'NF>=3{print $3}' | grep -cE '^(f[a-z.]+|frchg|fschg)\s|fpscr|fpul' || true)
echo "FPU instructions: $N (must be 0)"; [ "$N" = 0 ]
NI=$(printf '%s\n' "$D" | grep -c 'ocbi' || true)
echo "ocbi: $NI (must be 0)"; [ "$NI" = 0 ]
END=$("$SH/sh-elf-nm" "$OUT/s2.elf" | awk '$3=="_end"{print $1}')
[ $((0x$END & 0x1FFFFFFF)) -le $((PEND)) ] || { echo "ERROR: end 0x$END past the payload buffer"; exit 1; }
echo "MODS.BIN payload $(stat -f %z "$OUT/s2.bin") B, bss to 0x$END"
cat "$OUT/s2.syms"
