#!/bin/sh
# Build the read-only DSP survey cave (fxmem.c + fxmemhook.S) used by
# tools/fxmem_probe.py. Same toolchain and cave base as the key-shift
# installer; no DSP code is carried, this cave only reads.
set -e
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SH=$ROOT/work/sh-tools/bin
OUT=${FXMEM_OUT:-$ROOT/build/fxmem}
BASE=${SV_BASE:-0x09800000}
mkdir -p "$OUT"
cd "$ROOT/tools/fx_dsp"
MB=""; [ -n "$FXMEM_BASE" ] && MB="-DFXMEM_BASE=$FXMEM_BASE"
"$SH/sh-elf-gcc" $MB -m4-nofpu -mdiv=call-div1 -ml -O2 -Wall -ffreestanding \
  -fno-builtin -ffunction-sections -fdata-sections -nostdlib \
  -Wl,--gc-sections -Wl,-e,_sv_entry \
  -Wl,--section-start=.entry=$BASE \
  -Wl,-Ttext=$(printf '0x%X' $((BASE + 0x40))) \
  -o "$OUT/fxmem.elf" fxmemhook.S fxmem.c -lgcc
"$SH/sh-elf-objcopy" -O binary -j .entry -j .text -j .rodata -j .data \
  "$OUT/fxmem.elf" "$OUT/fxmem.bin"
"$SH/sh-elf-nm" "$OUT/fxmem.elf" | grep -E " _(sv_[a-z]+)$" > "$OUT/fxmem.syms"
N=$("$SH/sh-elf-objdump" -d "$OUT/fxmem.elf" | awk -F'\t' 'NF>=3{print $3}' \
    | grep -cE '^(f[a-z.]+|frchg|fschg)\s|fpscr|fpul' || true)
echo "FPU instructions: $N (must be 0)"; [ "$N" = 0 ]
SIZE=$(wc -c < "$OUT/fxmem.bin" | tr -d ' ')
# 1,792 B is about 2.5 s of deck time at the measured write rate (~650 B/s);
# the whole point of this cave is to stay inside a short deck session.
[ "$SIZE" -le 1792 ] || { echo "REFUSED: cave $SIZE B is too big for a fast write"; exit 1; }
echo "fxmem.bin $SIZE B at $BASE"
cat "$OUT/fxmem.syms"
