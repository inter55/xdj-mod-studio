#!/bin/sh
# USB-stick mods loader (prototype): build/usbloader/ul.bin + ul.syms.
# UL_BASE 0x09890000 = the free 64 KB between the theme maps
# (0x09850000 + 0x40000) and the centre colour module (0x098A0000), inside
# the deck's all-states-zero band 0x09836000..0x0996D000 (CLAUDE.md 77).
# Code + state in the first 4 KB, payload buffer UL_PAY..UL_PAY_END.
set -e
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SH=$ROOT/work/sh-tools/bin
OUT=$ROOT/build/usbloader
BASE=${UL_BASE:-0x09890000}
PAY=$(printf '0x%X' $((BASE + 0x1000)))
PEND=$(printf '0x%X' $((BASE + 0x10000)))
mkdir -p "$OUT"
cd "$ROOT/tools/usbloader"
"$SH/sh-elf-gcc" -m4-nofpu -mdiv=call-div1 -ml -O2 -Wall -ffreestanding -fno-builtin \
  -DUL_PAY=${PAY}u -DUL_PAY_END=${PEND}u \
  -ffunction-sections -fdata-sections -nostdlib -Wl,--gc-sections -Wl,-e,_ul_head \
  -Wl,--section-start=.entry=$BASE -Wl,-Ttext=$(printf '0x%X' $((BASE + 0x10))) \
  -o "$OUT/ul.elf" ulentry.S ul.c -lgcc
"$SH/sh-elf-objcopy" -O binary -j .entry -j .text -j .rodata -j .data "$OUT/ul.elf" "$OUT/ul.bin"
"$SH/sh-elf-nm" "$OUT/ul.elf" | grep -E " _(ul_|end$)" > "$OUT/ul.syms"
"$SH/sh-elf-size" "$OUT/ul.elf"
N=$("$SH/sh-elf-objdump" -d "$OUT/ul.elf" | awk -F'\t' 'NF>=3{print $3}' | grep -cE '^(f[a-z.]+|frchg|fschg)\s|fpscr|fpul' || true)
echo "FPU instructions: $N (must be 0)"; [ "$N" = 0 ]
# copy-back operand cache: the payload buffer must only ever be PURGED
# (ocbp); an ocbi would drop the bytes fread just copied in
D=$("$SH/sh-elf-objdump" -d "$OUT/ul.elf")
NI=$(printf '%s\n' "$D" | grep -c 'ocbi' || true)
NP=$(printf '%s\n' "$D" | grep -c 'ocbp' || true)
echo "ocbi: $NI (must be 0), ocbp: $NP (must be >= 1)"; [ "$NI" = 0 ] && [ "$NP" -ge 1 ]
END=$("$SH/sh-elf-nm" "$OUT/ul.elf" | awk '$3=="_end"{print $1}')
[ $((0x$END)) -le $((PAY)) ] || { echo "ERROR: end 0x$END reaches the payload buffer"; exit 1; }
echo "UL_PAY $PAY..$PEND"
cat "$OUT/ul.syms"
