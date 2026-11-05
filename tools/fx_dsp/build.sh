#!/bin/sh
# Build the TOUCH FX DSP code: build/fx/fx.bin (DSP 0xC005F4C0, loader
# padding shared with key shift). 0 FPU-less? (C674x has float units; this
# just must assemble clean and export no undefined symbols).
set -e
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
C6=$ROOT/work/tic6x-tools/bin
OUT=$ROOT/build/fx
BASE=${FX_BASE:-0xC005F4C0}
mkdir -p "$OUT"
cd "$ROOT/tools/fx_dsp"
sed "s/0xC005F4C0/$BASE/" fx.ld > "$OUT/fx.ld"
"$C6/tic6x-elf-gcc" -Os -march=c674x -mlittle-endian -msdata=none -ffreestanding \
  -fno-builtin -nostdlib -ffunction-sections -c fx.c -o "$OUT/fx.o"
"$C6/tic6x-elf-ld" -T "$OUT/fx.ld" "$OUT/fx.o" -o "$OUT/fx.elf"
"$C6/tic6x-elf-objcopy" -O binary "$OUT/fx.elf" "$OUT/fx.bin"
cp "$OUT/fx.bin" fx.bin; cp "$OUT/fx.elf" fx.elf
"$C6/tic6x-elf-nm" "$OUT/fx.elf" > "$OUT/fx.syms"
U=$("$C6/tic6x-elf-nm" -u "$OUT/fx.elf" | wc -l | tr -d ' '); [ "$U" = 0 ] || { echo "UNDEFINED SYMBOLS:"; "$C6/tic6x-elf-nm" -u "$OUT/fx.elf"; exit 1; }
"$C6/tic6x-elf-size" "$OUT/fx.elf"
grep " FX$" "$OUT/fx.syms"
ls -l "$OUT/fx.bin"
