#!/bin/sh
# Build the track filter: build/trackfilter/tf.bin + tf.syms
# TF_BASE 0x09940000 (1.2): the top of the old PWV7 snapshot slot, which now
# ends there (rgb_detail SLOT7_MAX 0x90000). Was 0x09960000 (something on the
# deck wrote over 0x0996C730..0x0996D100 = the filter's state), then briefly
# 0x099F0000 (inside FLAC+Opus, which ends 0x099F7478). Free on both decks.
# Old note: between the PWV7 snapshot slot (ends 0x09960000,
# tools/rgb_detail.py) and the end of the deck's all-states-zero band
# 0x09836000..0x0996D000 (CLAUDE.md sec. 77). Limit 0x0996D000.
set -e
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SH=$ROOT/work/sh-tools/bin
OUT=${TF_OUT:-$ROOT/build/trackfilter}
BASE=${TF_BASE:-0x09940000}
LIMIT=${TF_LIMIT:-0x09960000}
mkdir -p "$OUT"
cd "$ROOT/tools/trackfilter"
"$ROOT/.venv/bin/python" mkglyphs.py >/dev/null
MSOFF=$("$ROOT/.venv/bin/python" -c "
import sys; sys.path[:0] = ['$ROOT/tools', '$ROOT']
import mysettings as M; print(hex(M.MSOFF))")
"$SH/sh-elf-gcc" $TF_CFLAGS -DMSOFF=${MSOFF}u -m4-nofpu -mdiv=call-div1 -ml -O2 -Wall -ffreestanding -fno-builtin \
  -ffunction-sections -fdata-sections -nostdlib -Wl,--gc-sections -Wl,-e,_tf_head \
  -Wl,--section-start=.entry=$BASE -Wl,-Ttext=$(printf '0x%X' $((BASE + 0xA0))) \
  -o "$OUT/tf.elf" tfentry.S tf.c tfs.c -lgcc
"$SH/sh-elf-objcopy" -O binary -j .entry -j .text -j .rodata -j .data "$OUT/tf.elf" "$OUT/tf.bin"
"$SH/sh-elf-nm" "$OUT/tf.elf" | grep -E " (_tfs?_|_end$)" > "$OUT/tf.syms"
"$SH/sh-elf-size" "$OUT/tf.elf"
N=$("$SH/sh-elf-objdump" -d "$OUT/tf.elf" | awk -F'\t' 'NF>=3{print $3}' | grep -cE '^(f[a-z.]+|frchg|fschg)\s|fpscr|fpul' || true)
echo "FPU instructions: $N (must be 0)"; [ "$N" = 0 ]
END=$("$SH/sh-elf-nm" "$OUT/tf.elf" | awk '$3=="_end"{print $1}')
[ $((0x$END)) -le $((LIMIT)) ] || { echo "ERROR: end 0x$END past $LIMIT"; exit 1; }
cat "$OUT/tf.syms"
