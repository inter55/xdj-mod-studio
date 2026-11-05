#!/bin/sh
# Build the LIVE (no-reset) installer cave for the TOUCH FX DSP engine:
# build/fx/ksh.bin + ksh.syms. Same one-shot code as the hardware-proven
# key-shift installer (tools/keyshift_dsp/ksh.c + kshhook.S, CLAUDE.md
# sec. 119/122); it only carries fx.bin instead of ks.bin and re-points the
# stretcher's staging loads at FX.pbuf. Same cave address (0x09800000):
# key shift and TOUCH FX are installed one at a time (same DSP hook + home).
set -e
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SH=$ROOT/work/sh-tools/bin
OUT=$ROOT/build/fx
BASE=${KSH_BASE:-0x09800000}
sh "$ROOT/tools/fx_dsp/build.sh" >/dev/null
PBUF=$(awk '$3=="FX"{print "0x"$1}' "$OUT/fx.syms")
[ -n "$PBUF" ] || { echo "FX symbol missing"; exit 1; }
W=$OUT/inst
rm -rf "$W"; mkdir -p "$W"
cp "$OUT/fx.bin" "$W/ks.bin"          # kshhook.S: .incbin "ks.bin"
cd "$W"
"$SH/sh-elf-gcc" -DPBUF=$PBUF -m4-nofpu -mdiv=call-div1 -ml -O2 -Wall -ffreestanding -fno-builtin \
  -ffunction-sections -fdata-sections -nostdlib -Wl,--gc-sections -Wl,-e,_ksh_entry \
  -Wl,--section-start=.entry=$BASE -Wl,-Ttext=$(printf '0x%X' $((BASE + 0x40))) \
  -Wa,-I,"$W" -o "$OUT/ksh.elf" "$ROOT/tools/keyshift_dsp/kshhook.S" "$ROOT/tools/keyshift_dsp/ksh.c" -lgcc
"$SH/sh-elf-objcopy" -O binary -j .entry -j .text -j .rodata -j .data "$OUT/ksh.elf" "$OUT/ksh.bin"
"$SH/sh-elf-nm" "$OUT/ksh.elf" | grep -E " _(ksh|ks_)" > "$OUT/ksh.syms"
N=$("$SH/sh-elf-objdump" -d "$OUT/ksh.elf" | awk -F'\t' 'NF>=3{print $3}' | grep -cE '^(f[a-z.]+|frchg|fschg)\s|fpscr|fpul' || true)
echo "FPU instructions: $N (must be 0)"; [ "$N" = 0 ]
# the carried DSP code must be fx.bin byte for byte
"$ROOT/.venv/bin/python" - "$OUT" <<'P'
import sys
o = sys.argv[1]
s = {l.split()[2]: int(l.split()[0], 16) for l in open(o + "/ksh.syms")}
b = open(o + "/ksh.bin", "rb").read(); fx = open(o + "/fx.bin", "rb").read()
off = s["_ks_dsp"] - s["_ksh_entry"]
assert b[off:off + len(fx)] == fx, "carried DSP code != fx.bin"
assert s["_ks_dsp_end"] - s["_ks_dsp"] == len(fx)
print("carries fx.bin (%d B) at +%#x" % (len(fx), off))
P
ls -l "$OUT/ksh.bin"
