# Real-time emulator audio — the rebuild plan (2026-10-26)

## Where we are (measured, M-series Mac, real-DSP snapshot playing)

`tools/dsp_rate_bench.py`: **14.5 % of real time** (SH 72.7 Mcyc/s of
500 MHz, DSP 43.6 Mcyc/s of 300 MHz, audio 12.8k of 44.1k words/s).
Audio plays in clean bursts with gaps (burst mode in `emu/dspsound.py`).

Wall-time split while playing (`sample` + `tools/io_profile.py`):

| part | share | notes |
|---|---|---|
| C6x DSP core (`sp_cycle`, `c6x_run`, `mread`/`mwrite`) | ~45 % | software-pipelined loops dominate |
| SH-4 interpreter (`step`, `core_run`, memory access) | ~40 % | after today's fetch shortcut |
| Python device crossings (2DG, DMAC, HPI, timers) | ~14 % | was ~35 % before the 10-24 work |

Compiler flags (`-O3`, `-mcpu=native`) gain nothing: the cost is in the
structure (decode every instruction every time, per-cycle event loops).

Real time needs **both** chips about **7x** faster.

## Phases

1. **SH-4: decode once, run blocks** (est. 2-3x on the SH side).
   A per-4 KB-page cache of pre-decoded instructions (handler index +
   operands), executed as straight-line blocks up to the next branch;
   pages are invalidated when RAM inside them is written (bitmap check in
   the store path; our mods write code at runtime). Same `step()`
   semantics, behind `XDJ_SH_BLOCKS=0/1`; gate = `tools/differ.py` A/B
   MATCH + `sh4test` + `core_lockstep`.
2. **C6x: cheaper cycle loop** (est. 1.5-2x on the DSP side). The core
   already runs pre-decoded `Ins` records; the cost is the per-cycle
   pipeline/event bookkeeping and the SPLOOP buffer stepping. Batch idle
   cycles, skip empty event slots, specialise the hot SPLOOP kernels. Gate
   = `tools/c6x_test.py` + the deterministic DSP-alone hash
   (`scratchpad/dspdet.py` pattern) + replay oracle.
3. **Devices in C** (est. +10-15 %): 2DG register file + DMAC status +
   INTC/timer reads served in C (the remaining Python crossings).
4. **JIT** (the step to real time): translate hot SH-4 blocks and hot C6x
   loops to native code (arm64 first, x86-64 for Windows). Weeks of work;
   phases 1-3 lay the ground (block boundaries, invalidation, exact
   accounting) that a JIT needs anyway.

Expected after 1-3: roughly 30-40 % of real time (smoother bursts, much
shorter gaps). Real time itself: phase 4.

## Ground rules (from the fast-path work)

- Every speed-up must be **exact**: same registers, RAM, device state and
  the same step/cycle **COMMIT** points (the DSP clock reads committed
  cycles). `XDJ_FASTPATH=0` turns the SH shortcuts off for A/B.
- `d_cyc` (DSP cycle counter) differs between two identical `--live`
  runs already: ignore it in A/B, compare everything else.
- Pool-hooked firmware routines (lt_rgb, stems, …) must never be bypassed
  by a native fast path: hook targets can change at runtime.

## Done 2026-10-26: 2DG display lists in C

`core_tdg_run` (emu/fastcore.c) runs every 2DG list op (fill, copy,
textured quad incl. scaling, polygon, line, WPR, LCOFS, NOP/INT, SYNC,
TRAP) as an exact port of `emu/tdg.py`; Python passes the register file
in and applies the results. It stops BEFORE any op it can't do (unknown
op, non-RAM memory, WPR >= 0x100) and Python continues the same list.
Off with `XDJ_TDG_C=0`, and automatically while `draw_log`/`fb_hook` are
in use. Gates: differ A/B MATCH (track_loaded, mainscreen, wavplay
--live), sh4test 44/44, light_emu_test PASS, 0 fallbacks seen.
Speed while playing: 13.9 % -> 15.4 % of real time.
(The old C register mirror `tdg_r8/tdg_wr8` is dead code: never called.)

## Done 2026-10-26: SH core phase 1 (part 1)

- **Data-page cache** (`dtlb_get` in emu/fastcore.c): loads/stores hit a
  256-entry cache of host page pointers keyed on (virtual page, SR.MD,
  low-flash map), generation-stamped per `core_run`; only pages `loc()`
  maps wholly into one buffer. `XDJ_DTLB=0` off.
- **Decode-once table** (`g_cls[65536]` + `fast_step`): each opcode is
  classified once; 64 hot forms (~90 % of executed instructions on the
  playing snapshot) run directly, the rest go through `step()`.
  `XDJ_DTAB=0` off. Proof: `sh tools/dtab/run_selftest.sh` runs every
  classed opcode through both paths from random states over fake memory
  and logging fake devices (2.28M cases, 0 mismatches; a planted bug is
  caught), plus differ A/B MATCH x3, sh4test 44/44, core_lockstep 150k.
- Speed: SH-only snapshot ~95 -> ~130 M steps/s; playing with the real
  DSP 15.4 % -> 17.6 % of real time (the SH is now well under half of the
  wall time there; the C6x core and per-slice glue are the next targets).

## Done 2026-10-26: audio chip (C6x) delay-loop fast-forward

- Profile (playing, real DSP): the DSP is idle ~60 % of the time (already
  skipped); of its working cycles ~30 % are ONE busy-wait routine at
  0xC004BEE0, `for (volatile int i = 0; i < n; i++);` (SPLOOPW, 14 cycles
  per count; every call is delay(1000) = 14,044 cycles, called from an
  interrupt handler with interrupts off).
- c6xcore.c dff_*: the first call with a given n is LEARNED (run
  normally, accepted only if the code bytes match, no interrupt/Python
  op, only the known registers + stack word changed, to the closed-form
  values); later calls are replayed: whole call at once when it fits the
  run slice, else (interrupts off = nothing outside can change it) the
  call is FROZEN: c6x_run counts its cycles down and applies the recorded
  end state at the exact cycle. `cpu.settle()` (called by every status
  getter and dspdev.state) re-runs a frozen call from its entry state to
  the current cycle, so any observation equals plain execution. Reset /
  set_pc / set_cycle / code reload cancel it. XDJ_DSP_DFF=0 = off.
- Proof: tools/dsp_ab.py hashes all DSP RAM, registers, control regs,
  cycle, the DAC stream and the SH state at N checkpoints (each one a
  mid-run settle): identical with/without on wavplay (8 + 5 checkpoints)
  and playing7 (8); c6x_test 13/13; dsp_audio_diag balanced; a snapshot
  saved mid-delay reloads and plays.
- Speed while playing: ~17 % -> ~19 % of real time.

## 2026-10-26 (later): SH-4 block translator (native arm64), exact

Built in emu/fastcore.c (block engine) + emu/shjit.c (codegen) +
emu/a64enc.h (encoder):
- blocks = runs of decode-table instructions, optionally ending in a
  delayed branch + slot (core_run's K_DBR semantics); 64K-slot cache.
- native code per block: inline page-cache memory, reference handler for
  misses/rare ops, own-page store exit, exact it/steps/cycles per exit.
- chaining: blocks jump straight to the next compiled block (pc/key/
  epoch/idle checked; special pcs excluded); every block checks its own
  opcodes against memory on entry (code changes from any writer).
- switches: XDJ_BLOCKS=0, XDJ_JIT=0. Windows/x86: interpreter only.
Proof: tools/jit/run_selftest.sh (99 encodings vs the assembler; random
blocks incl. delayed branches, faults, device io, both memory paths, 0
mismatches; 3000/3000 code-change refusals), differ MATCH (track_loaded,
mainscreen), dsp_ab IDENTICAL (wavplay), sh4test 44/44.

Speed: SH-only snapshot ~101 -> ~120 M steps/s; playing case unchanged
(~15-18 %), because the wall time is now spread evenly (profile, playing):
Python 28 %, SH C side 23 %, native code 23.5 %, C6x DSP 22.6 %.

Next steps toward 90 % (each needed, none enough alone):
1. Codegen quality: keep SH registers in host registers inside a block
   (write back before helpers/exits), direct-patched chains, fewer
   per-exit stores -> native code 2-3x.
2. Block cache churn: ~150k recompiles per 100M steps; hot-block
   retention (2-way sets / no flush on collision).
3. Python: the slice loop + device ticks + device callbacks into C.
4. C6x: a block translator like this one for the DSP (SPLOOP kernels).

## 2026-10-26 (night): play mode -- the two chips on two threads

The two real chips run in PARALLEL; the emulator ran them in lock-step.
Play mode (dspdev.py) puts the C6x on its own worker thread once the
DSP is booted (BOOT_SETTLE = 200M DSP cycles of lock-step first, and
after every snapshot restore): the SH side only queues the DSP time it
owes (Condition variable, 60k-cycle chunks); back-pressure beyond
XDJ_DSP_MAXLAG (default 3M cycles) makes the SH wait, so the DSP can
never trail by more than ~10 ms emulated. irq_lock guards the flags
both threads touch (HPIC, INTC raises; the C core's IFR updates are
C11 atomics -- c6x_ifr_or/and), run_lock keeps code rewrite / reset /
snapshot off a running core, and state() drains what is owed before
anyone looks. `drain()`/`stop_thread()` quiesce on demand.
NOT cycle-exact (interleavings differ run to run) -- exact mode stays
the default and every test/validation tool runs exact. XDJ_PLAY=1
enables play mode (own DSP thread + 131072-step device slices via
fastcpu._slice); the app sets it; XDJ_DSP_THREAD=1 threads the DSP
without the bigger slices. Playback + burst audio work (diag: gaps 2,
max lag 32 words); snapshot save/reload works mid-play; light_emu_test
FULLSET (boot, real UTILITY presses, theme to LIGHT and back) PASSES
in play mode. chunk 60k vs 250k/1M: no difference (Python/GIL
crossings dominate, not chunk size).
Exactness proof for the exact-mode default: c6x_test 13/13, sh4test
44/44, core_lockstep 200k, differ A/B MATCH x2 (track_loaded,
mainscreen), dsp_ab stable, jit/dtab selftests PASS.
Speed (wavplay, M-series): exact 24 % of real time; play mode 32.4 %
(SH 162 Mcyc/s, DSP 97 Mcyc/s). Note for A/B across checkouts: run
both sides in the SAME tree -- a git worktree run of the same commit
produces different dsp_ab hashes (missing untracked FIRMWARE/ or cache
state; harmless, dac counts identical).

## 2026-10-26 night: fewer Python crossings, wider fast table (play 32 -> ~35 %)

- BUG FIXED (since 10-24): fastcpu's CS6 crossings set `d.hpia` on the
  SH7724 device instead of `d.dspemu.hpia`, so the audio-chip model never
  adopted HPIA values the C mirror had taken (byte-path HPIA stores).
  Latent until more HPIA traffic stayed in C; now adopted correctly.
- HPIA word stores (aligned 16/32-bit to 0x18040000) stay in the C mirror
  (the byte path already did): ~39k Python crossings per 0.4 emulated s
  gone. dspdev's HPIA write has no side effects, so this is exact.
- 2DG register file 0x00-0xFF owned by C while running (tdg_on, the old
  unused tdg_pad slot): word and byte reads/writes served from the
  mirror; only SCLR (word 0, starts a list) crosses. fastcpu flushes C ->
  Python before every Python 2DG touch and adopts Python -> C after;
  TDG.state() pulls first, TDG.restore() bumps `gen` so the core
  re-adopts. XDJ_TDG_MIRROR=0 = old behaviour. 2DG crossings 56k -> 7k
  per 30M steps.
- Block table: BLK_BITS 18, slot = (pc>>1 ^ pc>>19) & mask (C + emitted
  chain code). Plain indexing made hot code 256 KB apart collide: ~470k
  rebuilds/s, 96k recompiles per 3 s -> 36k. (Wall effect small.)
- 12 more decode-once classes (fast_step + native emitters): mov.w
  @(R0,Rm); mov.b/w @(d,Rm),R0 and R0,@(d,Rm); mov.b/w Rm,@-Rn; sts/lds
  FPSCR; sts.l MACL,@-Rn / lds.l @Rm+,MACL; dmulu.l (3 cycles, cls_cyc()).
  step() traffic while playing 15.6M -> 5.0M per 300M steps; blocks run
  longer before falling back. dtab selftest 76 classes 0 mismatches, JIT
  selftest 20000 blocks 0 mismatches.
- Gates: differ A/B vs the previous commit's library MATCH (track_loaded,
  mainscreen, 60M steps), dsp_ab IDENTICAL to the previous commit at all
  6 checkpoints, sh4test 44/44, c6x 13/13, dsp_audio_diag balanced,
  light_emu_test PASS.
- Speed, play mode, alternating old/new runs on a busy Mac: 26.7-32.0 %
  -> 33.3-38.3 %.
- Where the time goes now (SH thread, DSP held): ~41 % of real time on
  one core; ~1/3 Python (run loop: ~33k passes per emulated second, ends
  forced by 2DG list-end IRQs, TMU, DMA, idle skips; each pass ~15-30 us),
  ~1/4 native code, the rest interpreter/C. Next levers: device ticks +
  next-event calc in C (lazy TMU counters), native handling of the COMMIT
  boundary (~8 % of instructions still interpreted there), ldc/stc SR in
  the fast table.
- KNOWN, NOT FROM THIS CHANGE: tools/app_usb_probe.py never sees a load
  ("mode=0", red=560) on the user's library -- same on the previous build.

## 2026-10-28: the C event loop (play mode 35-44% -> 82-86% of real time)

The designed "device ticks + next-event calc in C" step, XDJ_EV_C=1
(default ON in play mode, exact mode never arms it):

- fastcore.c owns TMU (both units), the LCDC frame counter, their
  interrupt latches, the INTC IPR/IMR mirror, a 64 KB PORT (0x0405xxxx)
  register mirror, the SCIF status reads (SCFSR/SCFDR from rx byte
  counts Python keeps live) and -- hpid_wr, armed only while the DSP
  thread runs and no code page ever came through HPID -- the HPID word
  stores straight into the DSP's RAM regions.
- Timer IRQs are delivered IN C (core_ev_deliver -> core_enter_interrupt)
  with the same level/IMR/SR-imask checks as take_interrupt; masked
  latches persist like dev.pending entries do.
- Python keeps the authoritative device clock (dev.cyc) and pushes it in
  after every mem.tick via core_ev_tick; slices are bounded by
  core_ev_next (next TMU underflow / LCDC frame / a deliverable latch),
  so a pass ends only for Python-owned events (2DG lists, DMAC starts,
  USB, HPIC). Passes collapsed from ~33k/s to ~70 per 3M steps.
- dev.tmu/dev.tstr/dev.imr become ctypes views over the C arrays (the
  DMAC pattern; __reduce__ pickles them back to plain dicts), PORT /
  IPR / LDINTR bytes copy in at attach and back at __getstate__.
- THE BIG ONE: idle/sleep fast-forwards must also advance c.cycles --
  the DSP is time-locked to the SH cycle counter, and jumping dev.cyc
  alone starved it (first working build sat at 12 Mcyc/s with the DSP
  fed 285-cycle crumbs; audio 2.1k words/s). Jumps are bounded by
  core_ev_next so timer wakes stay on time.
- XDJ_EV_STATS=1 prints run-loop counters; XDJ_HPID_WR=0 disables the
  C HPID store path.

Gates: sh4test 44/44, c6x 13/13, core_lockstep 200k identical, differ
A/B MATCH (track_loaded, 3M steps), dsp_audio_diag gaps=0 L/R balanced,
rgb_detail rgb+3band PASS. Measured (dsp_rate_bench, wavplay, busy Mac):
35-44% -> 82-86% sustained.

Remaining to 100%: the SH interpreter/JIT itself (the known shjit
register-allocation lever) and the DSP worker's ~300 Mcyc/s ceiling --
at SH=500 the DSP needs 300 (96% of its measured max).

## 2026-11-01: play mode past real time (82-93 % -> 124-129 %)

Measured first (scratchpad split bench: wraps dspdev._queue/_run): in play
mode the SH thread WAITED 91 % of the time on back-pressure while the DSP
worker was 99 % busy at ~260 Mcyc/s -- the C674x core was the bottleneck,
not the SH. DSP time while playing: ~85 % in c6x_run, ~15 % Python (McASP
burst pacing). 70 % of DSP cycles are idle-skipped; of the rest, normal
execute packets dominate, SPLOOP kernels ~11 %, stall cycles ~25 %.

Changes (all exact; DSP-side A/B below):
- PGO build of the DSP core (the big one, ~+20 %): emu/c6x/cfast.build_lib
  compiles with -fprofile-instr-use=emu/c6x/c6xcore.profdata when present
  (plain -O2 otherwise / if clang can't read it); the .so rebuilds when the
  profile is newer. tools/c6x_pgo.py [SECS] regenerates the profile from a
  play-mode run of cache/emu_dsp_wavplay.pkl.gz. Same C semantics.
- c6xcore.c: constant-size memcpy in mread/mwrite (variable n was a libc
  memmove call per DSP load/store); SPLOOP buffer entries chained per
  stage residue (lc % ii) so a cycle walks only its stage's entries in
  buffer order (was a modulo scan of the whole buffer every cycle); stall
  cycles jump up to the first non-empty event bucket even with loads in
  flight (empty buckets commit nothing).
- McASP burst word loop in C (c6x_dma_words; periph.Edma3.burst calls it,
  XDJ_DSP_CDMA=0 = old Python loop).
- Play-mode DSP worker chunk 60k -> 500k cycles (XDJ_DSP_CHUNK), ~+5 %.
Measured (CPU time, alternating old/new runs, busy Mac): DSP standalone
258-272 -> 316-327 Mcyc/s before PGO; play mode new+PGO 108-114 % vs
84-93 % in the same session; tools/dsp_rate_bench.py XDJ_PLAY=1 SECS=30:
124.3 % and 129.1 % (SH 621/645 Mcyc/s, audio 109.6k/113.8k words/s).

App pacing fixes that only matter now that play mode is faster than real:
- SH7724.frames is a property: while the C event loop owns the LCDC the
  live count comes from the core (it used to read a FROZEN count, so the
  app never slept -> unbounded audio lag above 100 %).
- FastSH4.run(max_cycles=) / Machine.run(max_cycles=): stop a pass after
  that much device time. Play-mode sleep jumps let 1M steps cover ~0.6 s
  of emulated time; the app now caps a pass at 4M cycles (8 ms).
- EmuApp.PACE_LEAD (XDJ_PACE_LEAD, 0.1 s): play mode runs up to 100 ms
  ahead of the wall clock so the AudioQueue consumer (whole 93 ms buffers)
  always finds a full buffer; no cushion stalled it on every jitter.
- fastcpu._ev_out gave dev.imr back as a LIST (sh7724 keeps a dict):
  Machine.save mid-play crashed since 10-28. Now a dict (+ view __reduce__).
tools/dsp_audio_diag.py PACE=1 = the app's pacing rule: 2 x 40 s play
mode, consumer 100 %, gaps 0, max lag ~11k words (0.13 s).

Gates: c6x_test 13/13; dsp_ab DSP hashes identical to the pre-session core
at 8 checkpoints on wavplay and playing7 (2e9 steps; the SH hash wobbles at
checks 5/6 between two runs of the OLD build too -- pre-existing);
jit + dtab selftests PASS; sh4test 44/44; differ MATCH (track_loaded 3M);
save + reload mid-play: reloaded deck keeps playing (DSP status 2).
Not covered: Windows (MinGW, no clang profile -> no PGO there; the other
changes apply); tools/build_studio.sh still compiles c6xcore with plain
-O2 when the .c is newer than the .so -- it should call cfast.build_lib.

## 2026-11-01 afternoon: why the app still felt awful (user: "USB takes FAR too long")

Measured through the app's own code path (EmuApp headless + 30 Hz tick
thread + the user's library; scratchpad loadprobe/load2):
1. PACING FROZE THE DECK AFTER EVERY BURST. Input/USB fast-forward bursts
   did not re-anchor the pacing clock, so after a burst the loop slept the
   deck until the wall clock caught up with every frame it had skipped:
   5-12 fps after insert and while browsing. Fixed (re-anchor while
   bursting): 53-61 fps, mount 1.1 s, browse 0.3 s.
2. EVERY CLICK WAS A LONG PRESS. Bursts started on key/pointer DOWN; the
   0.25 s minimum hold then lasted seconds of emulated time. The browse
   knob showed the scroll hint instead of entering, MENU risked UTILITY.
   Bursts now start at the release.
3. THE FAST LOOP LIVELOCKED ON A STICK INSERT (why someone set
   XDJ_EV_C=0 in the app this afternoon). fastcpu's R_IDLE branch did
   `c.pc = c.pr` AFTER the pass-end _ev_pass had already entered a timer
   interrupt: the vector was overwritten and the idle task ran on with
   SR.BL set forever (USB interrupt never delivered, 0 reads, 1-2 fps).
   Fixed (no idle shortcut when an interrupt was just entered). Also:
   SH7724._level reads IPR from the C mirror while it owns it; CMT ticks
   in Python in play mode (C never owned it). XDJ_EV_C=0 removed from
   the app: audio WAV 22 % -> 85 %, MP3 25 % -> 30 % (old loop only looked
   faster by starving the DSP).
4. The morning's 124-129 % was partly fake: USB was wedged in the fast
   loop, so no file streaming happened during the bench. With USB really
   working: wavplay ~90 % (dsp_rate_bench), SH thread now busy with USB
   register crossings (205k Python reads / 12 s) -- next lever.
5. MP3 is DSP-bound: 63 % of DSP cycles in one memcpy SPLOOP (L2
   0x11804838, ldndw/stndw, ii 2). A closed-form skip of steady-state
   iterations (K a multiple of 8 so the event ring stays aligned; patch
   in-flight load/store events) would cut MP3 DSP cost ~2.5x. Not built.
6. RESOLVED (2026-11-01 late): the "E-8302 ~0.5 s in" folder runs were a
   PAUSED deck, not a failing load. In this firmware a LOAD AUTO-PLAYS
   (SH mode 3, DSP 0x11837BF8 == 2) and PlayPause then PAUSES it (SH mode
   5, DSP 3). Every harness pressed PLAY after the load, so the DSP block
   counter froze at ~39-44, the read-ahead (~785 KB) stopped and the DSP
   looped its output ring ("machine gun"). With that press removed -- same
   folder, same MP3, same snapshot -- DSP state stays 2, block counter goes
   44 -> 2270, file position 0x80000 -> 0x147105, disk reads resume in
   32 KB steps, 2.6M DAC words with 9 zeros, no DSP error: 53 s emulated
   (14e9 SH cycles, build/longplay.log). E-8302 red text stays 0.
   NOTE: 0x09D246E0 (tools' "SH_POS") is a static load field; the live
   position is in the DSP hand-off (0x11837CCC = 1024-frame blocks,
   0x118381B0 = file offset) and SH 0x09D24718/0x09D24728. Tools now only
   press PLAY when 0x11837BF8 != 2 (dsp_play_test.py, app_usb_probe.py).

Gates after: sh4test 44/44, differ MATCH, core_lockstep 100k, c6x 13/13,
dsp_ab DSP identical, light_emu_test FULLSET (play) PASS.

## 2026-11-01 evening: USB load time (launch -> library ready ~20 s -> ~5 s)
- MOUNT = four 8.4 s BOT timeouts (33 of 34 s emulated). emu/usbhost.py:
  (1) an IN NAK schedules a retry (NAK_RETRY 25k cycles) -- nothing used
  to kick the pipe again; (2) arming an IN pipe that already holds an
  unread packet (the eagerly fetched CSW) re-raises BRDY -- the firmware
  had cleared BRDY for it and waited out its timeout. Mount span 34 s ->
  0.45 s emulated (scratchpad mountgap). usbloader_stick_test PASS.
- Snapshot load 5.1 s -> 0.17 s: emu/fw.load_flash caches the parsed
  .UPD MAIN image (cache/flash_<sha>.pkl keyed by path/size/mtime); the
  frozen tools/fwtool.py compiled a regex per record on every load.
- studio/emuhost: the remembered library is inserted 0.5 s after the
  machine runs (was 3 s).
- Fingertip circle overlay removed from emu/ui.py (user found it weird).
- Tried and dropped (no measured benefit): high-speed stick (XDJ_USB_HS=1,
  mount 1.7x slower), PIPECFG SHTNAK auto-NAK, DBLB OUT queueing.
- E-8302 (folder library) was a PAUSED-DECK artifact, not a read failure:
  see the item above. The folder flow now loads, plays and keeps streaming
  for 53 s emulated; "die at block 39" was the frozen counter of a deck
  that the harness had just paused. The image-vs-folder difference also
  disappears once nothing presses PLAY.

## 2026-11-01 late: the APP-path E-8302 (a held DSP during a track load)
- After the paused-deck artifact was cleared (item 6 above), the app front
  end still ended a folder load in E-8302: load reached SH mode 3 / DSP
  state 2 / block counter 37, then 1 s later mode 14, DSP state 1, red
  text, no USB reads, and the SH spun on the HPI handshake.
- Cause: `EmuApp._burst_sound` holds the C6x model (`dspemu.hold = True`)
  during USB fast-forward bursts whenever the deck is not PLAYING -- and a
  track LOAD is not playing. The firmware's load handshake needs exactly
  that model: it waits for the DSP block counter 0x11837CCC, which cannot
  move while the DSP is held, so the handshake times out.
- A/B through the same app flow (scratchpad/app_variant.py):
  - baseline: fails ~1 s into play;
  - only the DSP hold disabled (everything else identical, bursts on):
    plays, ctr 133 -> 1013 in 14.5 s, file offset advancing, red 0;
  - bursts and hold both off: plays (slower load) -> the hold, not the
    burst, is what breaks it;
  - DSP worker thread off, hold on: still fails -> not the thread.
- Fix (emu/ui.py): hold the DSP only while NO track is on the deck
  (`_deck_has_track`: SH mode 0/1). Loading (2), playing (3) and paused
  (5) leave the model running; playing still ends the burst. Mount/browse
  bursts (mode 0) are unchanged, so mount stays ~5 s wall.
- Verified through EmuApp (scratchpad/app_force3.py, build/appfix.log):
  load at the 4th rotary push, 45 s wall -> mode 3, DSP state 2 the whole
  time, block counter 47 -> 2574, file offset 0x3CFAF -> 0x1697A2,
  2.99M DAC words with 9 zeros, no DSP error, E-8302 red text 0.

## 2026-11-01 night: MP3 speed -- the EDMA frame loop was Python

Measured (same harness, play mode, flat out): WAV snapshot 83 % of real
time, MP3 snapshot 30 %. cProfile of the SH thread showed the DSP's DMA
running in Python: 262,055 `Edma3._copy` calls/s, and the DSP worker (and
so the SH) starved behind it. PaRAM capture of the hot set: slot 40,
opt 0x8204/0x10820C (ABSYNC), acnt 4, bcnt 588 (0x24C), sbidx 4, dbidx 8,
dst RAM -- Pioneer's mono-to-interleaved audio move, copied one Python
call per frame. WAV playback never takes that geometry, which is why only
MP3 was slow.

- `c6x_dma_frames` (emu/c6x/c6xcore.c): the ABSYNC frame loop (read acnt
  bytes, then write them) in C, ranges bounds-checked against their RAM
  buffers, stops at the first frame that leaves one.
- `Edma3._frames` (emu/c6x/periph.py) wires it into `transfer` when both
  ranges sit wholly in RAM and nothing is watched; the Python loop still
  runs the remainder, in the same forward order. `XDJ_DSP_CFRAME=0` off.
- Exactness: scratchpad/cframe_ab.py runs 1e8 steps exact-mode both ways
  -> identical DSP status/block counter, RAM sha256, 200k-word DAC stream
  and EDMA transfer count. c6x_test 13/13, sh4test 44/44, differ MATCH
  (e8302_playing, 2M steps), usbloader_stick PASS.
- Speed (SNAP=cache/e8302_playing.pkl, XDJ_PLAY=1): 26 % -> 47 % of real
  time; SH 131 -> 237 Mcyc/s, DSP 78 -> 142 Mcyc/s.

CORRECTION to the 2026-11-01 afternoon note: MP3 is NOT 63 % in one
memcpy SPLOOP on this snapshot. A cycle counter over SPLOOPs
(`c6x_prof`, -DC6X_PROF) gives the loop at 0x11804838 16.1M of 153M DSP
cycles = 10.5 %; `step` (normal packets) is the bulk. So the closed-form
SPLOOP skip would gain ~10 %, not 2.5x -- the Python DMA frame copy above
was the real loss.

Still open (measured, not fixed): MP3 remains under real time. The rest
of the DSP-thread Python cost is per-execution opcode decode/encode for
instructions the C core calls back for (~123k `decode.parse_line` /
`cfast.encode` calls per 6 s) -- caching those encodings is the next
lever, then the interpreter itself (DSP core alone: 336 Mcyc/s, needed:
~300 Mcyc/s while also sharing the machine with the SH).

Debug accessors added (harmless in release builds): `c6x_sp_info` (live
SPLOOP state) and, only with `-DC6X_PROF`, `c6x_hist` / `c6x_prof`.

## 2026-11-02: how far from real time is MP3, and why (measured)

After the EDMA frame fix, the same harness gives MP3 47-51 % of real time
(quiet machine) against WAV's 83-119 %. What the numbers say:

- DSP demand while playing MP3 is ~300 Mcyc per emulated second (at 50 %
  real the chip runs 150 Mcyc/s). The C6x core inside cosim delivers
  ~35-41M `step()` calls/s and, on MP3, ~1.5 cycles per step -- i.e. it
  executes essentially every cycle, with 0 % pipeline-stall cycles and
  only ~10 % of cycles inside SPLOOPs. WAV looks fast only because most of
  its cycles are idle/delay cycles the core skips.
- "DSP core alone: 336 Mcyc/s" (dsp_split_bench part B) is therefore
  misleading for MP3: from a restored snapshot with no SH feeding it the
  chip runs dry and idles. Real decode throughput is ~50-60 Mcyc/s, so
  real time needs roughly a 5x faster C6x core.
- The hot instructions are ordinary DSP math spread over thousands of
  addresses (`mpysp`, `addsp`, `subsp`, `ldw/stw`; top 18 PCs = 11 % of
  all executions). There is no polling loop or delay loop to skip: the
  EDMA/delay/SPLOOP shortcuts are already in place and the remaining work
  is genuine computation.

Consequence: MP3 at real time is the phase-4 item (a C6x block translator
/ JIT like emu/shjit.c) or high-level emulation of the decode itself --
not another micro-optimisation. Recorded here so the next session does not
re-derive it from a misleading flat-out benchmark.

## 2026-11-02: why the emulator's audio is fast + stuttery (measured)

The QEMU host-audio bridge logs its own health: `dsp audio: N frames in, N out,
underruns, dropped in trims, fill (prefill, cap), asrc ±%`. Read from the
user's own run (`.../T/xdj-qemu-*/qemu.log`) and from headless runs of the
same binary:

| path | asrc | trims | underruns |
|---|---|---|---|
| Mac output = 48 kHz virtual device (Hue Sync Audio / BlackHole) | **-4.00 % (clamped)** | 3,989 frames dropped | 1-6, growing |
| Mac output by default (44.1 kHz path) | ±0.03 % | 0 | 0 |

`coreaudio: Downsizing buffer frames to 960` appears only on the first: the
queue is created for one buffer size and runs at another, the bridge cannot
follow, its rate converter clamps at its ±4 % limit, and the remaining drift
is discarded as audio -- which is heard as "plays too fast" plus stutter. The
guest is also CPU-starved, so panel clicks feel late.

Fixes applied in `emu/qemu_app.py`: request `out.buffer-length=3840` (the 960
frames the voice is actually given) so the queue is set up at its running
size, and `XDJ_EMU_AUDIO_DEV=<device>` to pin the emulator's output device.
Do not run the emulator on a 48 kHz virtual device.

Measurements (44.1 kHz path, same binary): 44.1k frames/s produced, fill
steady 17.5k frames, 0 underruns, 0 trims, asrc ±0.03 % over 45 s. The DSP's
own throughput ceiling is unchanged (docs/EMU_REALTIME.md phases 1-4).

## 2026-11-02 (night): the emulator's frame rate is the guest's own rate

- Studio host + mods image + one viewer: **105 frames in 40.8 s (2.6 fps)**
  at the idle screen; user measured 7.2 fps while playing.
- QEMU holds **105 % of one core**; the Python server side 17 %. QEMU is the
  bottleneck, not the MJPEG path (Pillow shrink + blit are ~10 % of the
  server's own samples).
- `qemu-system-sh4 -accel help` -> `tcg` only (11.1.2). SH4 has no KVM/HVF
  and no MTTCG, so a faster guest means either the interpreter work in this
  document or a native path for the guest's draw calls.
- Same runs: audio 0 underruns, 0 trims, asrc +0.03 %, fill ~17.6 k -- the
  audio side of the emulator is clean and independent of this ceiling.

## The clock follows the host, and the sink stops the chip

Measured on an M5 with a track playing: the DSP ran at `CDJ_C6747_MHZ=150` and
fell 7.4 s behind in a 5 s window while using 9.3 s of host time in it. Nothing
was dropped — the ring never overflowed — but the debt was paid back in one
burst of audio, which is heard as a sudden speed-up followed by a crawl.

Two changes, in the end-before-start order that makes them work:

- **Backpressure.** The DSP thread stops while the sink ring is at 7/8 of the
  latency cap (`cdj_dsp_audio_full()`, the `full` hook in `dsp_host.h`), the
  way the real chip stalls on its audio FIFO. The output then runs at the
  host's rate and the sink never has to trim.
- **Governor.** `host_govern()` in `dsp_host.c`, on a 200 ms wall window: busy
  for the whole window and still behind means the clock is more than the host
  delivers, so the clock drops to what it did deliver (`dc * 1000 / busy`); it
  climbs a few percent per window once the thread has slack. Only the clock
  changes: EDMA3 and McASP are paced from `dsp_ns`, which is virtual time, so
  audio still leaves at 44.1 kHz. `CDJ_<PREFIX>_GOVERN=0` pins the clock,
  `CDJ_<PREFIX>_MHZ_FLOOR` sets the least it may drop to (default 2/3).

After the governor settles, the log reads `0 underruns, 0 dropped in 0 trims`,
fill steady around 24 000, `asrc` within ±0.1 %, and `at most 15-80 ms behind`.
The M5 delivers about 100 MHz of emulated C6747, which is why every fixed clock
above that (150, 170) lagged no matter what the buffer did.
