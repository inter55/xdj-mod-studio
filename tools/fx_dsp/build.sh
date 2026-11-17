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
# FX_STATE_BASE=0xC006C000: put the 0x180 state block OUTSIDE the code
# window. The window 0xC005F4C0..0xC0060000 is only 2,880 B, and 384 of those
# were state -- which is why the engine was capped at 2,496 B of code. The
# proven-empty 16 KB page 0xC006C000..0xC0070000 has room for it, and the
# installer reads the FX symbol from the map, so nothing else changes.
if [ -n "$FX_STATE_BASE" ]; then
  "$ROOT/.venv/bin/python" - "$FX_STATE_BASE" "$BASE" fx.ld > "$OUT/fx.ld" <<'PY'
import sys
state, base = sys.argv[1], sys.argv[2]
src = open(sys.argv[3]).read().replace("0xC005F4C0", base)
old = "  . = ALIGN(8);\n  FX = .;\n  . += 0x180;\n"
if old not in src:
    raise SystemExit("fx.ld layout changed - fix FX_STATE_BASE support")
sys.stdout.write(src.replace(old, "  . = ALIGN(8);\n  FX = %s;\n" % state))
PY
else
  sed "s/0xC005F4C0/$BASE/" fx.ld > "$OUT/fx.ld"
fi
# optional short ring (fallback if the big block turns out to be in use)
RING=""
RING_BUILD=""
if [ -n "$FX_RING_BASE" ]; then RING="$RING -DECHO_BASE=$FX_RING_BASE"; RING_BUILD=1; fi
if [ -n "$FX_RING_FRAMES" ]; then RING="$RING -DECHO_FRAMES=$FX_RING_FRAMES"; RING_BUILD=1; fi
# DEFAULT: no delay line. The ring pages were measured in use with audio
# playing, and the first-pull zero-fill crashes the audio path even when no
# echo button is selected. A ring build is only for an explicit experiment:
# FX_RING=1 (or FX_ROLL/FX_REVERB, which need the line).
if [ -n "$FX_RING" ]; then RING_BUILD=1; fi
if [ -n "$FX_ROLL" ] || [ -n "$FX_REVERB" ]; then RING_BUILD=1; fi
if [ -z "$RING_BUILD" ]; then RING="$RING -DFX_NO_RING"; fi
if [ -n "$FX_NO_RING" ] && [ -n "$RING_BUILD" ]; then
    echo "REFUSED: FX_NO_RING=1 cannot be combined with a ring build" >&2
    exit 1
fi
# FX_PITCH=1: Touch FX Pitch. It fits the 2,496 B wall with FX_NO_RING=1
# (2,480 B on 2026-11-10) and passes the emulator gates; pair it with
# FX_PITCH=1 for tools/touchfx/build.sh so the panel offers the button.
if [ -n "$FX_PITCH" ]; then RING="$RING -DFX_PITCH"; fi
# FX_ROLL=1 / FX_REVERB=1: the two effects built on the existing delay line
# (Roll is Echo with no decay; Reverb carves 4 combs + 2 allpasses out of the
# same 6,500 frames). Both need the line, so do NOT combine them with
# FX_NO_RING.
if [ -n "$FX_ROLL" ]; then RING="$RING -DFX_ROLL"; fi
if [ -n "$FX_REVERB" ]; then RING="$RING -DFX_REVERB"; fi
"$C6/tic6x-elf-gcc" -Os -march=c674x -mlittle-endian -msdata=none -ffreestanding \
  -fno-builtin -nostdlib -ffunction-sections $RING -c fx.c -o "$OUT/fx.o"
"$C6/tic6x-elf-ld" -T "$OUT/fx.ld" "$OUT/fx.o" -o "$OUT/fx.elf"
"$C6/tic6x-elf-objcopy" -O binary "$OUT/fx.elf" "$OUT/fx.bin"
# Hard gate: the FX installer (build_fxh.sh) zero-fills ZSTATE=0x180 bytes of
# state after the code and the key-shift cosine table starts at 0xC0060000,
# giving 2,880 - 384 = 2,496 B. Overrun it and the audio path dies (it did,
# at 2,624 B with a 0x260 state area).
BLEN=$(wc -c < "$OUT/fx.bin" | tr -d ' ')
if [ -n "$FX_STATE_BASE" ]; then BUDGET=2880; else BUDGET=2496; fi
if [ "$BLEN" -gt "$BUDGET" ]; then
    echo "REFUSED: fx.bin $BLEN B > $BUDGET B: code would reach past 0xC0060000"
    exit 1
fi
echo "fx.bin $BLEN B of $BUDGET B budget (state 0x180" \
     "$([ -n "$FX_STATE_BASE" ] && echo at $FX_STATE_BASE || echo in-window), ends before 0xC0060000)"
# tools/fx_dsp/fx.bin is the shipping copy: only the default build updates it
if [ "$OUT" = "$ROOT/build/fx" ]; then
    cp "$OUT/fx.bin" fx.bin; cp "$OUT/fx.elf" fx.elf
fi
# delay-line geometry, so the install can refuse a ring no survey has proved
# empty (tools/fxmem_probe.py --ring writes the matching fxring_ok.txt)
if [ -z "$RING_BUILD" ]; then
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
