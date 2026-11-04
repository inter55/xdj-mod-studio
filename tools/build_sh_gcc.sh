#!/bin/sh
# Build a freestanding SH-4 C cross compiler (sh-elf) for deck code caves
# (Opus decoder, DSP-side helpers). Everything stays in work/ (gitignored).
# Sources:
#   binutils-2.43.1 (reused from tools/build_tic6x_objdump.sh, sha256 checked)
#   https://ftp.gnu.org/gnu/gcc/gcc-14.2.0/gcc-14.2.0.tar.xz
#     sha256 a7b39bc69cbf9e25826c5a60ab26477001f7c08d85cec04bc0e29cabed6f3cc9
# Needs Homebrew gmp mpfr libmpc isl. Output: work/sh-tools/bin/sh-elf-gcc
# Use: sh-elf-gcc -m4-single-only -ml -O2 -ffreestanding -nostdlib ...
#   (-ml = little endian like the deck; link with -lgcc for soft helpers)
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
W=$ROOT/work/sh-build
P=$ROOT/work/sh-tools
B=$ROOT/work/tic6x-build/binutils-2.43.1
mkdir -p "$W/binutils" "$W/gcc"
cd "$W"
if [ ! -x "$P/bin/sh-elf-as" ]; then
  [ -d "$B" ] || sh "$ROOT/tools/build_tic6x_objdump.sh"
  cd binutils
  "$B/configure" --target=sh-elf --prefix="$P" --disable-nls --disable-werror \
    --disable-gdb --disable-gprof --disable-gold --disable-sim --without-zstd \
    --with-system-zlib >cfg.log
  make -j8 MAKEINFO=true >make.log && make MAKEINFO=true install >inst.log
  cd ..
fi
[ -f gcc-14.2.0.tar.xz ] || curl -sSLO https://ftp.gnu.org/gnu/gcc/gcc-14.2.0/gcc-14.2.0.tar.xz
echo "a7b39bc69cbf9e25826c5a60ab26477001f7c08d85cec04bc0e29cabed6f3cc9  gcc-14.2.0.tar.xz" | shasum -a 256 -c -
[ -d gcc-14.2.0 ] || tar xf gcc-14.2.0.tar.xz
HB=/opt/homebrew/opt
cd gcc
PATH="$P/bin:$PATH" ../gcc-14.2.0/configure --target=sh-elf --prefix="$P" \
  --enable-languages=c --without-headers --with-newlib --disable-nls \
  --disable-shared --disable-threads --disable-libssp --disable-libquadmath \
  --disable-libgomp --disable-libatomic --disable-multilib \
  --with-endian=little --with-cpu=m4-single-only \
  --with-gmp=$HB/gmp --with-mpfr=$HB/mpfr --with-mpc=$HB/libmpc \
  --with-isl=$HB/isl --with-system-zlib >cfg.log
PATH="$P/bin:$PATH" make -j8 MAKEINFO=true all-gcc >make.log
# libgcc's SH extras (unwind-dw2-Os-4-200.o) can race libgcc_tm.h under -j:
# "fatal error: libgcc_tm.h: No such file or directory" -> retry serially
PATH="$P/bin:$PATH" make -j8 MAKEINFO=true all-target-libgcc >>make.log 2>&1 ||
  PATH="$P/bin:$PATH" make MAKEINFO=true all-target-libgcc >>make.log
PATH="$P/bin:$PATH" make MAKEINFO=true install-gcc install-target-libgcc >inst.log
echo "built $P/bin/sh-elf-gcc"
