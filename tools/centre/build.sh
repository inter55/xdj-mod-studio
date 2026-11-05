#!/bin/sh
# Build the centre-waveform colour module: build/centre/cw.bin + cw.syms
set -e
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SH=$ROOT/work/sh-tools/bin
OUT=${CW_OUT:-$ROOT/build/centre}
BASE=${CW_BASE:-0x098A0000}
mkdir -p "$OUT"
cd "$ROOT/tools/centre"
"$SH/sh-elf-gcc" -m4-nofpu -mdiv=call-div1 -ml -O2 -Wall -ffreestanding -fno-builtin \
  -DMODE_ADDR=${MODE_ADDR:?}u -DSLOT5=${SLOT5:?}u -DSNAP_HASH=${SNAP_HASH:?}u \
  -DCTX_TAB=${CTX_TAB:?}u -DIDX_OFF=${IDX_OFF:?}u -DTAG_PWV5=${TAG_PWV5:?}u \
  -DTAG_PWV7=${TAG_PWV7:?}u -DSLOT7=${SLOT7:?}u -DSLOT7_MAX=${SLOT7_MAX:?}u \
  -DTS_DATA=${TS_DATA:-0x09779000}u -DTS_END=${TS_END:-0x09790000}u \
  -ffunction-sections -fdata-sections -nostdlib -Wl,--gc-sections -Wl,-e,_cw_col -Wl,-u,_cw_col2 \
  -Wl,-Ttext=$BASE \
  -o "$OUT/cw.elf" cw.c -lgcc
"$SH/sh-elf-objcopy" -O binary -j .text -j .rodata -j .data "$OUT/cw.elf" "$OUT/cw.bin"
"$SH/sh-elf-nm" "$OUT/cw.elf" | grep -E " (_cw_|_end$)" > "$OUT/cw.syms"
"$SH/sh-elf-size" "$OUT/cw.elf"
N=$("$SH/sh-elf-objdump" -d "$OUT/cw.elf" | awk -F'\t' 'NF>=3{print $3}' | grep -cE '^(f[a-z.]+|frchg|fschg)\s|fpscr|fpul' || true)
echo "FPU instructions: $N (must be 0)"; [ "$N" = 0 ]
cat "$OUT/cw.syms"
