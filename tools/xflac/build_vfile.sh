#!/bin/sh
# Virtual-WAV layer (vfile.c + dr_flac + libopus fixed-point decoder) for
# the deck: build/xflac/vfile.bin + vfile.syms, linked at $VF_BASE (default
# 0x09970000: inside 0x0996E000..0x09A00000, zero on the deck idle, loaded
# and playing -- tools/mem_states.py). A new deck address needs a read-only
# deck survey first. Also builds build/xflac/vfile_native (Mac test, same C).
# VF_OPUS=0 builds FLAC only.
set -e
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
OUT=${VF_OUT:-$ROOT/build/xflac}
SH=$ROOT/work/sh-tools/bin
BASE=${VF_BASE:-0x09970000}
LIMIT=0x09A00000
mkdir -p "$OUT"
# $SRC/$INC/$DEFS are word-split lists: keep them relative to $ROOT so a
# checkout path with spaces still builds
cd "$ROOT"
O=work/opus/opus-1.5.2
SRC="tools/xflac/vfile.c"
DEFS="-DXF_FREESTANDING"
INC="-Itools/xopus/libc -Iwork/drflac"
if [ "${VF_OPUS:-1}" = 1 ]; then
  # same libopus build as tools/xopus/build.sh (patched celt/arch.h there)
  grep -q "ifdef OPUS_FAST_INT64" "$O/celt/arch.h" || sh "$ROOT/tools/xopus/build.sh" >/dev/null
  cd "$O"
  for f in src/opus.c src/opus_decoder.c src/extensions.c \
           $(sed -n '/^CELT_SOURCES = /,/^$/p' celt_sources.mk | grep '\.c' | tr -d '\\') \
           $(sed -n '/^SILK_SOURCES = /,/^$/p' silk_sources.mk | grep '\.c' | tr -d '\\') \
           $(sed -n '/^SILK_SOURCES_FIXED = /,/^$/p' silk_sources.mk | grep '\.c' | tr -d '\\'); do
    SRC="$SRC $O/$f"
  done
  cd "$ROOT"
  DEFS="$DEFS -DVF_OPUS -DOPUS_BUILD -DFIXED_POINT -DDISABLE_FLOAT_API \
    -DNONTHREADSAFE_PSEUDOSTACK -DGLOBAL_STACK_SIZE=65536 -DOPUS_VERSION=\"1.5.2-xdj\""
  INC="$INC -I$O/include -I$O/celt -I$O/silk -I$O/silk/fixed -I$O/src -I$O"
fi
"$SH/sh-elf-gcc" -m4-nofpu -mdiv=call-div1 -ml -O2 -w -ffreestanding -fno-builtin \
  -ffunction-sections -fdata-sections $DEFS $INC \
  -nostdlib -Wl,--gc-sections -Wl,-e,_vf_entry \
  -Wl,--section-start=.entry=$BASE -Wl,-Ttext=$(printf '0x%X' $((BASE + 0x40))) \
  -o "$OUT/vfile.elf" $SRC "$ROOT/tools/xflac/vfstack.S" "$ROOT/tools/xflac/libc_min.c" -lgcc
"$SH/sh-elf-objcopy" -O binary -j .entry -j .text -j .rodata -j .data "$OUT/vfile.elf" "$OUT/vfile.bin"
"$SH/sh-elf-nm" "$OUT/vfile.elf" | grep -E " _(vf_|o_|S$|neg|end$)" > "$OUT/vfile.syms"
"$SH/sh-elf-size" "$OUT/vfile.elf"
END=$("$SH/sh-elf-nm" "$OUT/vfile.elf" | awk '$3=="_end"{print $1}')
echo "image end 0x$END (limit $LIMIT)"
[ $((0x$END)) -le $((LIMIT)) ] || { echo "TOO BIG for the free area"; exit 1; }
N=$("$SH/sh-elf-objdump" -d "$OUT/vfile.elf" | awk -F'\t' 'NF>=3{print $3}' | grep -cE '^(f[a-z.]+|frchg|fschg)\s|fpscr|fpul' || true)
echo "FPU instructions: $N (must be 0)"; [ "$N" = 0 ]
# Mac test binary: libopus' 32-bit multiply path like the deck
cc -O2 -w -DVF_NATIVE -DOPUS_FAST_INT64=0 $(echo $DEFS | sed 's/-DXF_FREESTANDING//') \
  $(echo $INC | sed "s|-Itools/xopus/libc||") \
  -o "$OUT/vfile_native" "$ROOT/tools/xflac/vfile_native_test.c" $SRC
