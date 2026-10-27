#!/bin/sh
# Build GNU objdump for the TI C6000 (tic6x) target, used by tools/dspdis.py.
# Source: https://ftp.gnu.org/gnu/binutils/binutils-2.43.1.tar.xz
#   sha256 13f74202a3c4c51118b797a39ea4200d3f6cfbe224da6d1d95bb938480132dfd
# Output: work/tic6x/objdump   (work/ is gitignored)
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
W=$ROOT/work/tic6x-build
# GNU configure/make can't build in a path with spaces (the sub-configures
# split it): build elsewhere then, the result is still copied into work/
case $W in *" "*) W=${TMPDIR:-/tmp}/xdj-tic6x-build ;; esac
mkdir -p "$W" "$ROOT/work/tic6x"
cd "$W"
[ -f binutils-2.43.1.tar.xz ] || curl -sSLO https://ftp.gnu.org/gnu/binutils/binutils-2.43.1.tar.xz
echo "13f74202a3c4c51118b797a39ea4200d3f6cfbe224da6d1d95bb938480132dfd  binutils-2.43.1.tar.xz" | shasum -a 256 -c -
[ -d binutils-2.43.1 ] || tar xf binutils-2.43.1.tar.xz
mkdir -p build && cd build
# --with-system-zlib: the bundled zlib does not compile against current macOS SDKs
../binutils-2.43.1/configure --target=tic6x-elf --disable-nls --disable-werror \
  --disable-gdb --disable-gprof --disable-ld --disable-gas --disable-gold \
  --disable-sim --without-zstd --with-system-zlib >cfg.log
make MAKEINFO=true all-binutils >make.log
cp binutils/objdump "$ROOT/work/tic6x/objdump"
echo "built $ROOT/work/tic6x/objdump"
