# Emulator rebuild on QEMU (plan, 2026-11-01)

User decision (2026-11-01): rebuild the emulator the app shows on QEMU,
bundled inside the app. Licence is fine: our project is open source; the
QEMU-based part is GPL-2.0-or-later, so it ships with its source / a link to
it, and any changes we make to it are published under the GPL too.

## Why QEMU and not a rewrite of ours

Ours imitates both chips one instruction at a time. It sits at 65-100 % of
real time on WAV/AIFF and ~50 % on MP3 on this Mac (docs/EMU_REALTIME.md),
so audio has to pause to catch up. A rewrite in the same style hits the same
wall. Real time needs code translation (a JIT): QEMU has one for the SH-4
main CPU; the audio chip needs one too.

## Starting point: cdj-nxs2-qemu (github.com/Stijn-Jacobs/cdj-nxs2-qemu)

GPL-2.0-or-later, QEMU 9.1, Windows/macOS/Linux, portable packages via
GitHub releases. Already has: XDJ-700 v1.15 model (SH7724 board, same
SHA-256 check of XDJ700.UPD as ours), a C66x DSP emulator with a JIT running
Pioneer's own DSP program, sound, virtual USB stick, touch, a datagram API
for every key / jog / pot / touch, memory watch / poke, snapshots.

Its own status for the XDJ-700 (main branch, 2026-10-29): loads and plays
from USB in real time; touch and tempo fader work; PLAY/pause partly;
**MP3 breaks up about every 6 s; MASTER TEMPO runs below real time**; CUE,
jog bend, hot cues not yet tested. The user's local clone
(~/Downloads/ref/cdj-nxs2-qemu) is 262 commits behind (fetched, not merged).

## Phases

1. **Spike (1-2 days).** Fresh clone of upstream next to ours (never into the
   user's clone), build on this Mac, boot the XDJ-700 from the user's own
   XDJ700.UPD. Measure, through their own front end: real-time % on WAV,
   AIFF (Mylo - Drop The Pressure), MP3; folder vs image stick; PLAY/pause,
   CUE, jog, browse knob, touch. Decide go / no-go with numbers.
2. **Mods bridge.** Our installers talk to a deck over the USB shell; the
   mock deck (tools/mock/mockdeck_server.py) already answers that shell from
   emulator memory. Point it at QEMU instead (gdbstub memory read/write or
   their memory-poke API), so every real installer (tools/deck_session.py)
   runs unchanged and "With mods" = the exact bytes a deck gets.
3. **App integration.** The Studio Emulator tab starts the bundled QEMU
   program, shows its screen (their RFB/VNC or screen stream), forwards
   clicks/keys/knob/jog/touch through their datagram API, inserts the
   user's library folder, plays sound. Two choices: Original / With mods.
4. **XDJ-700 gaps.** Fix what the spike shows is broken on the 700
   (PLAY/pause, MP3 break-ups, MASTER TEMPO speed, jog), using what we
   already know about this firmware (CLAUDE.md: jog bytes 8-11 + key bit,
   DSP state words, McASP pacing, E-8302 causes). Offer the fixes upstream.
5. **Package** Mac + Windows apps; gates (tester_pack_test /
   public_app_test) adapted to the new emulator.

## What stays on our old emulator

All mod-validation tests and gates (cycle-exact mode, py core trace hooks,
snapshots, tools/*_test.py) keep using emu/ -- they need its exactness and
introspection. Only the emulator the user sees moves to QEMU.

## Open questions for the spike

- Does their virtual USB stick take a FOLDER (users' libraries) or only an
  image (their `tools/baken`)? If image only: build the image from the
  folder on insert, or port our FolderDisk idea.
- Screen path into the Studio window with the least latency.
- Memory writes while running: gdbstub vs their CDJ_MPOKE / PCALL hooks.
- Bundle size and Windows build in our CI.

## Spike results (2026-11-01/02 night) -- verdict: GO

Setup: fresh clone of upstream main (63a5186, 2026-10-29) at
~/Downloads/ref/cdj-qemu-spike, built into ~/qemu-build-spike (the user's
own clone and ~/qemu-build untouched). Prerequisites were already
installed; the build took ~1 min (cache). The user's XDJ700.UPD unpacked
and verified; their main_unpacked.bin == our cache/blk1.bin byte for byte
(theirs has 133 extra bytes at the end). Driver: ~/Downloads/ref/
spike_measure.py (monitor socket, load steps, then the deck's live play
position 0x0C0E6D18 read with `xp` every 3 s against the wall clock).

Measured:
- Boots on the user's firmware in ~45 s; one host core busy.
- **MP3 ("House Music", 128k): 100 % of real time, steady over 30 s**
  (ours: ~50 %). Recorded audio: no gaps after the first ~4 s of the play
  start over ~65 s (the source file has 1 gap of its own). Their README's
  "MP3 breaks up about every 6 s" did not show here.
- Screen, waveform, BPM, MASTER all drawn by the firmware (spike_play.png).

Gaps found (all work we have the knowledge for):
1. On the XDJ-700 only three controls are mapped: USB, knob push, PLAY. No
   knob turn, no jog, no other buttons. Ours has the full 700 panel map
   (emu/panl.py BUTTONS), the knob (RAM counter 0x0B6C7B48) and the jog
   (bytes 8-11 + payload 15 bit 0x80, CLAUDE.md 153). Port these into
   their xdj700 board / panel datagram.
2. USB stick = a FAT16 image of at most 2 GB, built by `baken` from a
   folder. The user's library is 14.9 GB and already a rekordbox export.
   Plan: serve our FolderDisk (emu/usbdisk.py, FAT32, any size, no copy)
   to QEMU as a network block device (`-drive file=nbd+unix://...`).
   baken's one-track test sticks listed no artists (ARTIST = EMPTY), so the
   AIFF and WAV could not be reached without a knob -- not measured yet.
3. No live memory writes besides gdbstub (which halts MAIN). Not needed:
   "With mods" = their kernel image patched like tools/permanent_build.py
   does, plus QEMU `-device loader,file=...,addr=...` blobs for every cave
   outside the image (stems, hot cues, themes, centre module, ...) -- the
   end-state bytes our installers produce (tools/mock/verify_install.py
   knows them all).
4. MASTER TEMPO is slower than real time on the 700 (their note; not
   measured here).

Next (phase 2/3 work, in order): panel controls for the 700 -> folder
stick over NBD -> measure AIFF/WAV/Mylo -> "With mods" boot -> Studio
Emulator tab on top of it -> Windows build.

## Phase 2 results (2026-11-02 morning)

- **Whole library as the stick, no copying:** tools/qemu_stick_nbd.py serves
  our FolderDisk over NBD; QEMU mounts the 14.9 GB "Rekordbox stuff" export
  as a 16.7 GB FAT32 stick. BROWSE shows the real rekordbox artist list.
- **Knob:** new panel op `m16add` (qemu/0001-...patch): 3 turns = 3 rows,
  exact. Browsed artist + track lists with it.
- **Mylo - Drop The Pressure (AIFF) from the library: 100 % of real time,
  steady over 32 s, 0 audio underruns while playing** (ours: 65-100 %, the
  stutter the user heard).
- **PLAY/pause works** via 0x10:0x01 (paused = mode 5, resumed plays).
- **Jog:** new op `lvl16` + bytes 8-11 + bit 15/0x80: paused, 30 jog steps
  forward = +4.44 s, 30 back = -4.44 s, 60 = +8.89 s (linear, exact).
- Quirk seen: artist "Mylo" previews EMPTY in [ARTIST] (push does nothing)
  -- the track is reachable via [TRACK]. Same DB on our emulator? check.

Remaining for "in the app": Studio Emulator tab -> QEMU (screen stream,
input mapping from our deck GUI to these panel ops, folder stick server,
sound), "With mods" boot (patched kernel + loader blobs), Windows build.

## Phase 3 (in the app), 2026-11-02

- emu/qemu_app.py: QemuApp = EmuApp's deck art + input, QEMU inside
  (VNC-over-unix-socket screen reader, panel datagrams, NBD folder stick,
  QEMU's own sound). Watchdog wrapper: QEMU dies with the server even on
  SIGKILL (verified: nothing left after killing the server).
- studio/emuhost.py picks QemuApp when the QEMU build + firmware image are
  present (XDJ_EMU_ENGINE=old = our engine); firmware list = With mods /
  Original firmware. Insert waits for "running" on the QEMU engine.
- Patch now also fixes USB hot-plug in their r8a66597 model (a stick added
  after boot was never announced to the firmware).
- END TO END through the Studio server's own web API (scratch e2e.py):
  boot, library auto-insert, touch USB, knob to Mylo, push = loaded and
  playing (mode 3), screenshot of the streamed deck.
- Deck art redone against the user's product photos (emu/deck.py): bowl
  ring + glossy platter jog (cached per degree, render 3.7 ms), ribbed
  tempo rail with black cap and dotted scale.

NOT DONE: "With mods" on QEMU (patched kernel + loader blobs), shipping
QEMU + DSP module + firmware image inside the app (today it uses the
dev build in ~/qemu-build-spike), Windows, LEDs (VINYL/MT lamps) from QEMU.

## Phase 4 (2026-11-02): mods, bundling, lamps, Windows path

- "With mods": the app bakes the mods into the firmware image QEMU starts
  from (tools/permanent_build.mods_image, cached per waveform style in the
  app-support qemu/ folder). Verified: RGB waveforms, phase meter, PERFORM.
- Bundled: tools/bundle_qemu.sh copies the lean QEMU build, the audio-chip
  library libc66x.so (QEMU's built-in default was the builder's
  ~/build/c6x path) and their 7 Homebrew libraries into the app
  (repo/qemu-bin, 19 MB), rewritten to @loader_path, signed; plus keymaps
  (-L share), licence, our patch, source note (GPL). build_studio.sh runs it.
  Firmware image = blk1.bin from the user's own update (fwhome); not shipped.
- Measured with the bundled copy + fwhome blk1.bin + mods: Get Lucky plays
  at 100 % of real time; also 100 % WITHOUT the curated DSP module (m.so,
  firmware-derived, never shipped) -- audio gaps without it not measured.
- Lamps: MASTER TEMPO / VINYL light from QEMU's panel state frames
  (verified after a render).
- Windows path: XDJ_QEMU_TCP=1 (automatic on Windows) = VNC, monitor and
  NBD stick on local TCP; tested on the Mac end to end (boot, stick,
  browse). CI: .github/workflows/windows.yml job `qemu` builds the patched
  QEMU under MSYS2 with its own release script; app job copies it into
  repo/qemu-bin. NOT RUN YET (needs a push). No watchdog on Windows yet:
  QEMU can outlive a force-killed server there.

## Phase 5 (2026-11-03): tempo fader, click delay, faster QEMU

**Tempo fader.** The XDJ-700 panel reports its fader on two channels, and
our input sent neither correctly:
- Bytes 4/5: a 16-bit fader word. Bytes 6/7: its centre reference
  (0x8000). The pitch code at 0x08D49E90 computes tempo % from position
  against centre. With a centre of 0 the readout stays 0.00 % wherever the
  fader is (the reference emulator notes the same for the CDJ-900NXS).
- Bytes 12/13: 0..499 with bit 15 set = "tempo fader active". The parser
  (0x08D51EA6) turns that bit into key byte 84 bit 0x04 (snapshot
  0x0B6C40D8). Without it the tempo latch 0x0B6C3E10 is never set (the
  handler 0x08D3E814, in mode 5 = playing), so the rate is never computed.
  `panl.BUTTONS["NeedlePositionFwd"] = (12, 0x80)` is really this flag.

Fix (`emu/qemu_app.py inject_panl`): send all three, and re-send the
analogue values every second. A value sent while QEMU was still booting was
lost, and the "send on change" cache never repeated it. Verified on the
QEMU deck: the screen reads +6.15 %, +10.00 %, -6.10 % and 0.00 % for
faders 400 / 499 / 100 / centre (±10 range). In QEMU, mode 3 = loaded and
waiting at the cue point, mode 5 = playing. Our own emulator
(emu/panl.build_frame) still sends the old layout (test bench only).

**Click delay.**
- In QEMU itself a press or tap shows in ~0.15 s.
- Through the app it took 0.5-0.85 s: EmuApp holds every press >= 0.25 s
  and 3 frames (needed by our own emulator, which samples input once per
  frame), and a tap only counts on release.
- QemuApp now uses MIN_HOLD 0.08 s / MIN_TICKS 1: 0.3-0.6 s end to end,
  measured with two separate HTTP posts per tap.
- (A first measurement of 0.8-1.8 s was the test client reading the MJPEG
  stream in fixed 64 KB chunks; use read1.)

**Faster QEMU.**
- `~/qemu-build-fast` = the lean build's configure flags + `--enable-lto
  --disable-qom-cast-debug --disable-debug-info -Dbuildtype=release`
  (QEMU forbids NDEBUG, so keep b_ndebug=false).
- Deck screen updates: idle 15 -> 20/s, playing ~4.3 -> 6-9/s.
- `tools/bundle_qemu.sh` now ships it.
- `qemu/0002-c66x-arm64-rounding.patch`: on Apple Silicon the audio
  chip's float ops called fesetround twice per op (41 % of the DSP thread).
  The patch writes FPCR.RMode directly, only on a change (FPCR access is
  itself slow on Apple cores). The DSP thread is now mostly fop_run itself.
- Main-chip thread while playing: 55 % guest code, 14 % TB lookup
  (indirect jumps), 9 % waiting on the lock the DSP thread holds,
  6 % get_tb_cpu_state. No further cheap wins found.

**USB re-insert bug.** A stopped stick server leaves its unix socket file;
the next one's bind() failed (EEXIST), so every re-insert, including the
host's auto-insert after boot, gave the deck no stick. `_StickProc` now
unlinks the socket path first.

**Not verified:** audio by ear on the fast build. The silent "none" audio
driver doesn't consume at 44.1 kHz, so its underrun counters mean nothing.
Check with real output.

**Jog (open, 2026-11-03).** QEMU, track playing, wheel turned in the app
at 60 counts per frame for 2 s (sound OFF, so absolute speeds are ~60 %:
compare against the "no jog" row):

| case | moved |
| --- | --- |
| playing, no jog | +1.43 s |
| ring forward | +1.47..1.51 s (small bend, plausible) |
| ring backward | **+4.8..6.6 s (jumps AHEAD: wrong)** |
| top touch, no turn | +1.65..3.0 s (doesn't stop: vinyl mode off?) |
| paused, forward | +2.5 s (search works) |

Tried with no effect: byte 15 bit 0x40 ("JogRoundFwd") as the direction
bit; a counter that only counts up. Both reverted. Next: decode the KY
handler 0x08B2B0F4 path for bytes 8-11 / 15 properly (it is what
the old emulator measured as +16 count, +20 period, 15.0x80 moving),
test paused backward, and split the app's wheel into top (touch, inner
~70 %) and ring (bend) like the real deck.
Also: with XDJ_DECK_SOUND=0 QEMU's "none" audio driver consumes ~24k
frames/s, so a deck in a sound-off test plays at ~60 % speed. Timing tests
need real audio output.
- Jog follow-up (same day): both KY jog handlers (0x08B2B0F4 / 0x08B2B132)
  set key byte 87 bits 0x08 and 0x20 alike and differ only in the counter
  step (-200 / +200 at struct +16), so direction = counter sign. Paused:
  counter up = forward, down = back, as expected. While playing, whichever
  turn comes SECOND in a run jumps ahead (+4..6 s) regardless of sign: a
  lag / catch-up effect, measured with the sound-off driver's ~60 % clock.
  Re-test with real audio output and one case per run.
- CORRECTION (later 2026-11-03): sound-off QEMU runs are NOT ~60 % slow.
  Over 10 s they run at 1.05x. The slow readings were the first seconds
  after a track loads (the deck plays slow, then catches up), and that
  catch-up was the jog's "jump ahead". Measure after a 20 s settle, over
  5-10 s windows.
- JOG FIXED: bytes 10/11 (time between pulses) set the bend: 300 ~ +-6 %,
  100 ~ +-25 % (measured). inject_panl now derives it from the real spin
  rate (EMA of counts/s; period = 180000 / rate, clamped 40..2000):
  20 counts/frame = gentle, 60 = 1.33x / 0.82x, 150 = 1.52x / 0.59x.
  Paused scrubbing was already right both ways. emu/ui.py: inner 72 % of
  the wheel = top platter (JogTouch), outer ring = bend only.
