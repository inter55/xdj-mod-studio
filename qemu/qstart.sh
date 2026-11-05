#!/bin/sh
rm -f /tmp/spike-mon.sock
export CDJ_ATA=1 CDJ_PANEL_KEYSOCK=40124 CDJ_DSP_AUDIO=1:3000:150:450 CDJ_C6747_IDLE=0xC004C8CC:0x11804AE0:0x11805C00 C66X_IDLE_ISR_FAST=1 C66X_JIT=$HOME/c14gen/curated-xdj700/m.so
R=$HOME/Downloads/ref/cdj-qemu-spike
exec $HOME/qemu-build-spike/qemu-build/qemu-system-sh4 -M xdj700 -name XDJ-700 -kernel $R/extract/xdj700/main_unpacked.bin -serial file:/tmp/spike-serial.log -drive if=none,id=usbstk,file=nbd:unix:/tmp/stick.sock,format=raw -device usb-storage,drive=usbstk,port=1 -display none -audio ${AUD:-none,timer-period=5000} -monitor unix:/tmp/spike-mon.sock,server,nowait "$@"
