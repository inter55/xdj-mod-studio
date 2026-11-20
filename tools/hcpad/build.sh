#!/bin/sh
# Hot cue pad swipe: build/hcpad/hp.bin at HP_BASE (default 0x09793000, the
# free gap between the light add-on and the UTILITY rows; deck-zero band).
set -e
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SH=$ROOT/work/sh-tools/bin
OUT=$ROOT/build/hcpad
BASE=${HP_BASE:-0x09793000}
mkdir -p "$OUT"; cd "$ROOT/tools/hcpad"
V=$(cd "$ROOT" && .venv/bin/python -c "
import sys; sys.path[:0]=['tools']
import perform_pads as PP, mysettings as MS
s={l.split()[2].lstrip('_'): l.split()[0] for l in open('build/hotcue8/hc.syms')}
t={l.split()[2].lstrip('_'): l.split()[0] for l in open('build/touchfx/tfx.syms')}
print(f'-DPST={PP.PST:#x}u -DMSOFF={MS.MSOFF:#x}u -DHC_NEXT=0x{s[\"hc_next\"]}u -DHC_PREV=0x{s[\"hc_prev_page\"]}u -DHC_REC=0x{s[\"hc_rec\"]}u -DRECARM=0x{s[\"hc_recarm\"]}u -DP1USED=0x{s[\"hc_p1used\"]}u -DHCPAGE={PP.HCPAGE:#x}u -DTFX_MODE=0x{t[\"tfx_mode\"]}u')")
"$SH/sh-elf-gcc" -m4-nofpu -mdiv=call-div1 -ml -O2 -Wall -ffreestanding -fno-builtin $V ${HP_CFLAGS:-} \
  -ffunction-sections -fdata-sections -nostdlib -Wl,--gc-sections -Wl,-e,_hp_touch_entry \
  -Wl,--section-start=.entry=$BASE -Wl,-Ttext=$(printf '0x%X' $((BASE + 0x80))) \
  -o "$OUT/hp.elf" hphook.S hp.c -lgcc
"$SH/sh-elf-objcopy" -O binary -j .entry -j .text -j .rodata -j .data "$OUT/hp.elf" "$OUT/hp.bin"
"$SH/sh-elf-nm" "$OUT/hp.elf" | grep -E " (_hp_|_end$)" > "$OUT/hp.syms"
N=$("$SH/sh-elf-objdump" -d "$OUT/hp.elf" | awk -F'\t' 'NF>=3{print $3}' | grep -cE '^(f[a-z.]+|frchg|fschg)\s|fpscr|fpul' || true)
echo "FPU instructions: $N (must be 0)"; [ "$N" = 0 ]
END=$(awk '$3=="_end"{print $1}' "$OUT/hp.syms")
[ $((0x$END)) -le $((0x09794000)) ] || { echo "hp ends at $END, past the UTILITY rows"; exit 1; }
echo "$V"; cat "$OUT/hp.syms"; ls -l "$OUT/hp.bin"
