#!/bin/sh
# USB-stick loader builds (docs/USBLOADER.md):
#   ul.bin  -- 10-25 prototype (MODS.BIN, single payload), kept as-is
#   lc.bin  -- pack loader CORE, entry lc_entry(pack, plen) at LC_BASE
#              (emulator-only running position for tools/usbloader_test.py core)
#   ld.bin  -- pack loader on the SURVEYED open pool 0x08DE39A4: reads
#              <drive>:/XDJMODS/PACK.BIN at stick mount (same parser)
# UL_BASE/LD_BASE 0x09890000 = the free 64 KB between the theme maps
# (0x09850000 + 0x40000) and the centre colour module (0x098A0000), inside
# the deck's all-states-zero band 0x09836000..0x0996D000 (CLAUDE.md 77).
# ul and ld share that address: install one or the other, never both.
set -e
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SH=$ROOT/work/sh-tools/bin
OUT=$ROOT/build/usbloader
CC="$SH/sh-elf-gcc -m4-nofpu -mdiv=call-div1 -ml -O2 -Wall -ffreestanding -fno-builtin \
  -ffunction-sections -fdata-sections -nostdlib -Wl,--gc-sections"
mkdir -p "$OUT"
cd "$ROOT/tools/usbloader"

nofpu() {
  N=$("$SH/sh-elf-objdump" -d "$1" | awk -F'\t' 'NF>=3{print $3}' | grep -cE '^(f[a-z.]+|frchg|fschg)\s|fpscr|fpul' || true)
  echo "$(basename "$1"): FPU instructions: $N (must be 0)"; [ "$N" = 0 ]
}

# --- ul (prototype) ---
UB=${UL_BASE:-0x09890000}
PAY=$(printf '0x%X' $((UB + 0x1000)))
PEND=$(printf '0x%X' $((UB + 0x10000)))
$CC -DUL_PAY=${PAY}u -DUL_PAY_END=${PEND}u -Wl,-e,_ul_head \
  -Wl,--section-start=.entry=$UB -Wl,-Ttext=$(printf '0x%X' $((UB + 0x10))) \
  -o "$OUT/ul.elf" ulentry.S ul.c -lgcc
"$SH/sh-elf-objcopy" -O binary -j .entry -j .text -j .rodata -j .data "$OUT/ul.elf" "$OUT/ul.bin"
"$SH/sh-elf-nm" "$OUT/ul.elf" | grep -E " _(ul_|end$)" > "$OUT/ul.syms"
nofpu "$OUT/ul.elf"
END=$("$SH/sh-elf-nm" "$OUT/ul.elf" | awk '$3=="_end"{print $1}')
[ $((0x$END)) -le $((PAY)) ] || { echo "ERROR: ul end 0x$END reaches the payload buffer"; exit 1; }

# --- lc (core, memory source) ---
LB=${LC_BASE:-0x0BC00000}
LHI=$(printf '0x%X' $((LB + 0x10000)))
$CC -DLC_SELF_LO=${LB}u -DLC_SELF_HI=${LHI}u -Wl,-e,_lc_entry \
  -Wl,--section-start=.entry=$LB -Wl,-Ttext=$(printf '0x%X' $((LB + 0x800))) \
  -o "$OUT/lc.elf" lc.c -lgcc
"$SH/sh-elf-objcopy" -O binary -j .entry -j .text -j .rodata -j .data "$OUT/lc.elf" "$OUT/lc.bin"
"$SH/sh-elf-nm" "$OUT/lc.elf" | grep -E " _(lc_|end$)" > "$OUT/lc.syms"
nofpu "$OUT/lc.elf"
END=$("$SH/sh-elf-nm" "$OUT/lc.elf" | awk '$3=="_end"{print $1}')
[ $((0x$END)) -le $((LHI)) ] || { echo "ERROR: lc end 0x$END past its 64 KB"; exit 1; }

# --- ld (stick source on the surveyed pool) ---
DB=${LD_BASE:-0x09890000}
DHI=$(printf '0x%X' $((DB + 0x10000)))
$CC -DSTICK -DLC_SELF_LO=${DB}u -DLC_SELF_HI=${DHI}u -Wl,-e,_ld_head \
  -Wl,--section-start=.entry=$DB -Wl,-Ttext=$(printf '0x%X' $((DB + 0x10))) \
  -o "$OUT/ld.elf" ldentry.S lc.c -lgcc
"$SH/sh-elf-objcopy" -O binary -j .entry -j .text -j .rodata -j .data "$OUT/ld.elf" "$OUT/ld.bin"
"$SH/sh-elf-nm" "$OUT/ld.elf" | grep -E " _(ld_|lc_|end$)" > "$OUT/ld.syms"
nofpu "$OUT/ld.elf"
END=$("$SH/sh-elf-nm" "$OUT/ld.elf" | awk '$3=="_end"{print $1}')
[ $((0x$END)) -le $((DHI)) ] || { echo "ERROR: ld end 0x$END past its 64 KB"; exit 1; }

"$SH/sh-elf-size" "$OUT/ul.elf" "$OUT/lc.elf" "$OUT/ld.elf"
cat "$OUT/lc.syms" "$OUT/ld.syms"
