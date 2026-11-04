# XDJ-700 emulator (macOS)

Runs Pioneer's real XDJ-700 firmware (v1.15) on an emulated Renesas SH7724:
from the reset vector, through the RTOS boot and the handshake with the
front-panel MCU, to the deck's own screens, drawn by the firmware itself.

```
.venv/bin/python -m emu.main
```

Opens instantly from `cache/emu_mainscreen.pkl` (a snapshot of the booted
player screen). Without it, or with `--ckpt none`, it boots from reset,
which takes about 8 seconds.

## Speed

The CPU core is C (`emu/fastcore.c`, compiled automatically on first run
with the system `cc`), about 20-25 M SH-4 instructions/s: the UI runs at
~50 of the real deck's 60 frames/s. `XDJ_CORE=py` selects the original
Python core (0.5 M/s), which is kept for debugging: any per-instruction
trace hook (`cpu.trace`, `Machine.run(stop_on_pc=...)`) runs on it
automatically. `tools/core_lockstep.py CHECKPOINT` runs both cores side by
side and compares every register after every instruction.

The audio chip (C674x, `emu/c6x/c6xcore.c`) spends most of its time in
two busy-wait loops of its idle task: 0xC004C8CC while the deck is stopped,
0xC0048C2E while a track plays (ten channel polls per 2,237-cycle pass,
about 75 % of its cycles). When a pass ends in exactly the state it began
in, the core jumps whole passes instead of running them, so the result is
cycle-exact. Measured on Apple Silicon (2026-11-04): playing 83 → 232 M
DSP cycles/s, stopped 90 → 505 M. `tools/dsp_idle_equiv.py SNAPSHOT`
proves it on any snapshot with the real DSP (same final state with and
without the skip). `XDJ_DSP_IDLE=0` turns all skipping off,
`XDJ_DSP_IDLE_PLAY=0` only the playing deck's loop.

The app ties the audio chip's clock to the SH's cycle counter
(`XDJ_DSP_RATIO` DSP cycles per SH cycle, default 3; was 0.6 before 0.99.6).
The SH model counts about 28.6 M cycles per second of deck time (0.48 M per
60 Hz frame), so the chip gets about 86 M cycles per deck second instead of
the real 300 M and makes about 30 % of real-time audio (0.6 gave 6 %). That
ratio, not the chip's speed, is why emulator sound stutters. Measured on
Apple Silicon (2026-11-04, `XDJ_PLAY=1`): ratio 3 keeps a stopped deck at
60 frames/s and a playing deck at 30-35 frames/s, the same as 0.6 (25-31).
Inside the app's fast-forward bursts (stick mount,
track load, the window after a button press) it stays at the old 0.6
(`XDJ_DSP_LOAD_RATIO`): there ratio 3 doubled a track load's wall time.
`XDJ_DSP_RATIO=10.5` gives the chip its real share: real-time audio on a
stopped deck, but a playing deck then holds the SH back to about 15-17
frames/s.

## The window

- **Firmware menu** (toolbar): the stock snapshot, stock from reset, or any
  `build/*.UPD`. A mod boots from reset; one that faults before drawing
  anything shows **CRASHED** -- what a deck would do with a black screen.
  `build/PURPLE_v2.20.UPD` (the build that bricked a deck) reproduces this.
- **Zoom**: toolbar - / +, Cmd -/+ / Cmd 0, or resize the window.
- **USB stick** (toolbar): insert a folder -- a rekordbox USB export, i.e.
  the folder that holds `PIONEER/` and `Contents/` -- or a raw disk image;
  click again to eject. The deck mounts it exactly as a real stick (the
  folder is presented as FAT32, built on the fly). Anything the deck writes
  stays in memory; your files are never modified. `XDJ_USB=<path>` inserts
  one at start-up.
- **Save state**: writes the stock session back to its snapshot (closing the
  window does not, unless `--save-on-exit`).
- **Touch screen**: click the on-screen buttons (BROWSE, TAG LIST, INFO,
  MENU, rekordbox/LINK/USB, TIME, SYNC, MASTER, SLIP, CALL, MEMORY). The
  panel MCU reports these as key bits, so that is what the emulator sends.
- **Controls**: click or drag the deck; keys below.

| key | control | key | control |
| --- | --- | --- | --- |
| Space | PLAY/PAUSE | C | CUE |
| I / O / R | loop IN / OUT / RELOOP | [ ] | track search |
| , . | search | ← → | jog |
| ↑ ↓ / wheel | rotary | + / - | tempo fader |
| T / G / V | MASTER TEMPO / TEMPO RANGE / VINYL | B / Y | BACK / TAG TRACK |

## What is modelled

| part | file | notes |
| --- | --- | --- |
| SH-4 CPU + FPU | `fastcore.c`, `sh4.py`, `sh4fpu.py` | PR selects precision, SZ the move size (manual 6.3) |
| memory map | `machine.py` | 2 x 64 MB DDR, flash, framebuffers, P4 MMU regs |
| timers, INTC, DMAC, SCIF | `sh7724.py` | per-channel DMA interrupts; masked IRQs stay pending |
| 2D graphics engine | `tdg.py` | display lists: fills, blits, colour key, textured quads |
| LCD controller | `sh7724.py` | 60 Hz frame-end interrupt, scan-out address |
| front panel MCU | `sh7724.py`, `panl.py` | SCIF2 + DMA ch1/ch2, 32-byte frames both ways |
| USB host + stick | `usbhost.py`, `usbdisk.py` | R8A66597-family USB0, DREQ-paced DMA; mass-storage stick |

Not yet: the audio DSP and the network. The "USB Error. Remove the device."
popup (message 123) is posted during start-up, before the USB port is even
initialised, so it is not the USB model; its source is still being traced.

## Tools

- `tools/theme_test.py THEME.UPD ...` boots stock and each theme to the same
  point and counts un-themed pixels; side-by-side images in `build/theme_test/`.
- `tools/sprite_map.py CHECKPOINT` records which firmware bitmaps the
  graphics engine draws (`mods/sprite_map.json`, used by the theme engine and
  the safety gate).
- `tools/panl_oracle.py` measures the panel button map from the firmware's
  own parser.

## Mac app

The emulator ships inside **XDJ Mod Studio** (Emulator tab): the same
front-end (`emu/ui.py`), run headless by `studio/emuhost.py` and streamed
to the tab. Build it with `sh tools/build_studio.sh`. `python -m emu.main`
still opens the standalone window for emulator development.
