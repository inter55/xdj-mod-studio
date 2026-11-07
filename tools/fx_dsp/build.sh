#!/bin/sh
# Build the TOUCH FX DSP code: build/fx/fx.bin (DSP 0xC005F4C0, loader
# padding shared with key shift). 0 FPU-less? (C674x has float units; this
# just must assemble clean and export no undefined symbols).
set -e
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
C6=$ROOT/work/tic6x-tools/bin
OUT=${FX_OUT:-$ROOT/build/fx}
case "$OUT" in /*) ;; *) OUT="$ROOT/$OUT" ;; esac
BASE=${FX_BASE:-0xC005F4C0}
mkdir -p "$OUT"
cd "$ROOT/tools/fx_dsp"
sed "s/0xC005F4C0/$BASE/" fx.ld > "$OUT/fx.ld"
# optional short ring (fallback if the big block turns out to be in use)
RING=""
if [ -n "$FX_RING_BASE" ]; then RING="$RING -DECHO_BASE=$FX_RING_BASE"; fi
if [ -n "$FX_RING_FRAMES" ]; then RING="$RING -DECHO_FRAMES=$FX_RING_FRAMES"; fi
# FX_NO_RING=1: no delay line at all (Filter/Crush/Gate only). The engine
# then never writes the ring, so there is nothing to prove before install.
if [ -n "$FX_NO_RING" ]; then RING="$RING -DFX_NO_RING"; fi
# FX_PITCH=1: build the Touch FX Pitch experiment. It does NOT fit the 2,496 B
# wall (2,912 B), so the gate below refuses it -- keep this off for shipping;
# it is here so the measurement is reproducible instead of folklore.
if [ -n "$FX_PITCH" ]; then RING="$RING -DFX_PITCH"; fi
"$C6/tic6x-elf-gcc" -Os -march=c674x -mlittle-endian -msdata=none -ffreestanding \
  -fno-builtin -nostdlib -ffunction-sections $RING -c fx.c -o "$OUT/fx.o"
"$C6/tic6x-elf-ld" -T "$OUT/fx.ld" "$OUT/fx.o" -o "$OUT/fx.elf"
"$C6/tic6x-elf-objcopy" -O binary "$OUT/fx.elf" "$OUT/fx.bin"
# Hard gate: the FX installer (build_fxh.sh) zero-fills ZSTATE=0x180 bytes of
# state after the code and the key-shift cosine table starts at 0xC0060000,
# giving 2,880 - 384 = 2,496 B. Overrun it and the audio path dies (it did,
# at 2,624 B with a 0x260 state area).
BLEN=$(wc -c < "$OUT/fx.bin" | tr -d ' ')
if [ "$BLEN" -gt 2496 ]; then
    echo "REFUSED: fx.bin $BLEN B > 2496 B: code + 0x180 state would reach past 0xC0060000"
    exit 1
fi
echo "fx.bin $BLEN B of 2496 B budget (state 0x180, ends before 0xC0060000)"
# tools/fx_dsp/fx.bin is the shipping copy: only the default build updates it
if [ "$OUT" = "$ROOT/build/fx" ]; then
    cp "$OUT/fx.bin" fx.bin; cp "$OUT/fx.elf" fx.elf
fi
# delay-line geometry, so the install can refuse a ring no survey has proved
# empty (tools/fxmem_probe.py --ring writes the matching fxring_ok.txt)
if [ -n "$FX_NO_RING" ]; then
    printf '0x0 0\n' > "$OUT/ring.txt"
else
    printf '%s %s\n' "${FX_RING_BASE:-0xC0073400}" "${FX_RING_FRAMES:-6500}" > "$OUT/ring.txt"
fi
echo "ring $(cat "$OUT/ring.txt") ($OUT/ring.txt)"
"$C6/tic6x-elf-nm" "$OUT/fx.elf" > "$OUT/fx.syms"
U=$("$C6/tic6x-elf-nm" -u "$OUT/fx.elf" | wc -l | tr -d ' '); [ "$U" = 0 ] || { echo "UNDEFINED SYMBOLS:"; "$C6/tic6x-elf-nm" -u "$OUT/fx.elf"; exit 1; }
"$C6/tic6x-elf-size" "$OUT/fx.elf"
grep " FX$" "$OUT/fx.syms"
ls -l "$OUT/fx.bin"
