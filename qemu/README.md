# QEMU emulator work (docs/EMU_REBUILD_PLAN.md)

- `0001-xdj700-knob-and-jog16.patch` -- against cdj-nxs2-qemu main
  (63a5186). GPL-2.0-or-later like the file it changes
  (hw/cdj/common/cdj_pnl_link.c): panel-socket ops `m16add` (bump a u16 in
  MAIN RAM: the 700's browse knob at 0x0B6C7B48) and `lvl16` (set a
  big-endian 16-bit panel field atomically: jog counter report[8..9],
  pulse period [10..11]).
- `0003-audio-clock-and-backpressure.patch` -- the audio work: the sink stops
  the chip while its ring is nearly full (a real chip stalls on its audio
  FIFO) instead of the sink trimming; the chip's clock follows what the host
  delivers (CDJ_<PREFIX>_GOVERN / _MHZ_FLOOR) because a fixed clock above it
  falls behind without bound and the debt is paid back as a burst; and the
  chip's thread asks for the fast-core scheduling class itself.
- `qstart.sh` -- start the XDJ-700 on the spike build with the stick served
  by tools/qemu_stick_nbd.py on /tmp/stick.sock and a monitor socket.
- `qx.py` -- drive it: `key 0x13:0x04`, `knob N`, `push`, `shot NAME`, `pos`.

Jog on the 700 = `0x08:<count>:0:lvl16` (+-200 per pulse), period
`0x0a:300:0:lvl16` while turning (65535 idle), and `0x0f:0x80:<ms>` (the
"jog moving" bit). PLAY/pause = `0x10:0x01:300`, USB = `0x13:0x04:300`,
knob push = `0x11:0x01:300`, BACK = `0x14:0x10:300`. Every other button:
emu/panl.py BUTTONS gives (payload byte, mask) = the same report offsets.
