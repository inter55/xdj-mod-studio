# Why a stem mute takes ~10 s (analysis 2026-11-02)

Status: **analysis only, no code changed.** Deck measurement tonight: about
30 s before, about 10 s now ("its faster, maybe 10s"). v1 target is well
under 0.5 s.

## How the mute works

`tools/stems/sm.c` hooks the media server's USB read (pool `0x08D5FFA8`).
A STEMS-4 file carries four planes; the deck's PERFORM STEMS pads set
`PMASK` bits 0..3 (vocals/drums/bass/other). While any bit is set, each
read inside the sample data is answered with the sum of the *un-muted*
planes, and the file position is left exactly where a plain read leaves it.

Audio the deck has **already buffered** does not pass through the hook
again, so a mask change also rewrites that buffer in place
(`remix()`), using the same `mix()` math. Un-muting (mask 0) re-reads the
original bytes, so it is bit-exact.

## Where the ten seconds go

- `REMIX_MAX = 8 MiB` bounds the rewrite. At 44.1 kHz, 16-bit stereo that is
  **47.6 s of audio**.
- Each re-mix reads **all four planes** for that range: up to **33.5 MiB**
  of reads from the USB stick, four seeks + four reads per 16 KiB chunk,
  while the deck's own media thread keeps using the file.
- On a stick that serves a few MiB/s, 33.5 MiB is the ~10 s observed. The
  earlier ~30 s figure matches an earlier, larger cap.

So the delay is **I/O volume**, not DSP math and not the panel.

## Why not just shrink the cap

Any byte of the buffered window left un-rewritten plays the OLD mix, so the
mute would land partially and then "lurch" into place -- which is exactly
the pitch/timing glitch reported on track "radio 84" (docs/ROADMAP.md).
The cap exists to bound work, not because the remaining bytes are safe to
skip.

## Two exact ways to cut it (both unbuilt)

1. **Subtract instead of re-sum, for a single mute.** The buffer holds the
   sum of the un-muted planes, so muting stem k can be `sample - plane_k`:
   one plane read (8.4 MiB) instead of four (~1.7 s at the same rate).
   *Caveat:* only exact when the stored sum never clipped at +-32767; a
   clipped sample minus a plane is not the same as re-summing. Needs a
   clipping guard (fall back to the four-plane re-sum when a sample is at
   the rail) and a bit-exact test against the current path.
2. **Start at the playhead, not at the window start.** `remix()` rewrites
   from `wpos0`, but everything before the DSP's current block
   (`hrd1(PLAYBLK)` in `dr_step`) has already been played. Starting at the
   playhead can shrink the region a lot; it is exact for the same reason.

Doing both takes the rewrite from ~33.5 MiB to roughly one plane over the
unplayed part, and is the only route to sub-second without changing where
the mixing happens.

## How to measure it

- Audible latency in the emulator (real DSP model, deterministic):
  `.venv/bin/python tools/stems_delay_emu.py measure stems` against the
  existing `cache/emu_stemsdelay_playing.pkl`, and `measure key` for the
  key-shift comparison.
- Wall-clock on the deck: stopwatch from the pad tap to the sound change,
  which is what the 30 s -> 10 s figures came from.
- Correctness: `tools/stems_native_test.py` (native build of `sm.c`) plus
  `tools/stems_emu_test.py`.

## Measurement attempt, 2026-11-02 (inconclusive -- read this before trusting it)

`tools/stems_delay_emu.py measure stems` (log `build/stems_delay_stems.log`):

```
ctl:   47.6 s audio, err None [426s]
stems: 47.6 s audio, err None [320s]
RESULT stems: no audible change within 47.6 s
```

47.6 s is the harness's own horizon (`measure(..., secs=SECS)`, default 45 s
of audio), NOT the rewrite cap, so the run says the emulator heard **no
change at all** in 45 s. The deck hears the change in ~10 s. Two candidate
explanations, neither proven:

1. **Stale snapshot.** `cache/emu_stemsdelay_playing.pkl` is from 2026-10-18
   while `build/stems/sm.bin` was rebuilt 2026-11-02 22:15. If `PC0` (the
   cave base the mod install picks) moved between those builds, the harness
   writes `PMASK` at an address the installed module no longer reads, so the
   mute never happens. Rebuild with `stems_delay_emu.py prep` and re-measure
   before drawing any conclusion.
2. The mute path genuinely does not fire in that model, which would be a
   module bug that the deck contradicts (the user's deck DID change).

Do not quote the 47.6 s figure as latency. The deck's ~10 s and the I/O
analysis above are the current best evidence.

## Review, 2026-11-03 (corrects the analysis above)

- **"33.5 MiB per mute" is wrong.** `win_extend()` restarts the window on
  any non-contiguous read, and the deck reads into two alternating buffers
  (0x0E910B00 / 0x0E91FB40, CLAUDE.md sec. 121), so the SH-side `remix()`
  window is about ONE read (~0x24C0 B, measured `sm_remixb`), not 8 MiB.
  `REMIX_MAX` never binds in practice.
- The ~30 s read-ahead lives in the audio chip's decoded ring (sec. 124).
  The rewrite that matters is `dr_step()`: one block (588 frames) per
  `dr_write`, four plane reads per block, at most `DR_BUDGET` = 24 blocks
  per deck read call, and it only runs WHEN the deck calls fread -- the
  deck reads in bursts every ~5 s. The deck's ~10 s = waiting for read
  bursts + 24-block steps through the window.
- **Idea 2 already exists**: `dr_step` starts at the playhead
  (`dr_next = PLAYBLK + 3`).
- Real levers: run the rewrite without waiting for a deck read (e.g. from
  the flush hook, like other caves do every frame), and/or a larger budget
  per call; idea 1 (subtract one plane) still cuts USB reads by 4x.
- Stale-snapshot theory CONFIRMED in part: `PP.PMASK` is PC0+0x1A80 in
  the working tree, PC0+0x1A10 at HEAD, and the snapshot is from
  2026-10-18 (older still), so the harness writes the mask where the
  installed module does not read it. Run `stems_delay_emu.py prep` first.

## Fast trigger built, 2026-11-03 (emulator: ~30-40 ms, bit-exact; deck untested)

- Why the deck took seconds: the audio chip only asks for more data when
  its buffer drops below ~1913 blocks (~25.5 s) and then tops it up to
  ~2257 (~30 s). That gives one read burst every ~5 s (emulator, measured).
  The rewrite only ran inside those reads. The old 0.6 s emulator figure
  was a lucky tap right before a burst.
- Fix (`tools/stems/sm.c`, `smhook.S`): the player task polls the chip's
  buffer level ~340 times a second through pool `0x08C1D56C` (stock
  `0x08BF2BCC`, used only by fn `0x08C1D366`/`0x08C1D416`). `sm_poll_entry`
  sits there. It switches to the module's own 8 KB stack, calls the
  accessor, then rewrites one block per call.
- The rewrite reads through a copy of the deck's file handle (a
  self-contained 0x1250-B stdio record, taken when the file is first seen)
  so the media task's own handle never moves. Once the poll runs, the
  rewrite is driven only from there. The rewrite has its own mix buffers.
  `MAXB` dropped to 8 KB so the module still ends before TOUCH FX
  (`_end 0x098426AC` < `0x09844000`).
- Emulator (`tools/stems_fastmute_emu.py`, tap 1 s after a burst):
  - mute: output == drums+bass+other EXACTLY from 40 ms after the tap
    (0 wrong blocks over 3 s);
  - un-mute: == original mix from ~30 ms;
  - rewrite ~210 blocks/s (~3x play speed).
- Harness gotcha: a Machine.save while the poll task is mid-USB-read
  leaves that task blocked forever after load (in-flight reads are
  dropped). Never snapshot mid-rewrite.
- Gates: stems_native/remix/version_remix tests PASS; repro_freeze full
  set PASS; mock-deck `stems_apply` install/re-run/`--revert` PASS (both
  pools restored).
- Install: `stems_apply.py`, the permanent/emulator bake and `emu/ui.py`
  arm the poll pool only from stock. Otherwise they leave it off: mutes
  still work, at burst speed.

## CORRECTED 2026-11-03: never the buffer -- the rewrite aimed at stale slots

The measurement in the section below was right about *what* was happening and
wrong about *why*. The rewrite was not fighting a 30 s chip buffer: it was
writing the muted mix into OLD copies of those blocks, which the chip never
plays again, because the slot it searched for by label came from a search of
the whole store rather than from the playhead.

What the emulator shows (scratchpad/slot_calib.py, slot_frontier.py):
  * the chip's slot for block b holds exactly the file's mix plane for b,
    588 stereo frames as float32 = s16/32768, at block offset b*588 --
    correlation 1.0000, aligned to the sample, no encoder delay
  * the slot of the block now playing is the DSP word 0x1182E81C (PSLOT),
    and slot(s) = pslot + (b - playblock), labelled by the block number in
    the slot's 100-byte trailer
  * the chip keeps ~2100 blocks (28 s) decoded ahead of the playhead

So the fix (tools/stems/sm.c, 2026-11-03) is:
  * anchor every slot on PSLOT + playhead delta instead of searching
  * start each pass AT the playhead (p+3), not at the far end of the buffer:
    the bytes the chip plays next are the ones that must change for the tap
    to be heard, the rest is fixed up behind playback
  * clamp the aim to 32 s past the playhead, skip holes instead of stalling
  * never write a slot until the chip has labelled the NEXT block, i.e. the
    decoder has finished with this one (writing a half-filled slot was the
    stutter)
  * 128 blocks/poll while catching up, 4/poll afterwards (polls run ~340/s
    in the emulator, ~28/s on the deck: 4x28 = 112 blocks/s > 75 needed)

Emulator proof (scratchpad/mute_verify.py, cache/emu_flush_playing.pkl):
baseline 16/16 slots bit-exact against the plain mix; 0.05 s after the tap 8
blocks written and 16/16 exact against the muted mix, the block AT the
playhead included; 1.3 s later still 16/16; unmute restores all 16 exactly;
noslot 0, wfail 0, skip 0.

Audible A/B in the same emulator (scratchpad/audible_ab.py: two runs from the
same snapshot, one untouched, one muted, real C674x core): the first sample
that differs is number 1,668 -- **38 ms** after the tap -- and 94.7% of the
following samples differ, i.e. the whole mute, not a fragment. 38 ms is one
poll interval plus a block, which is the floor for this mechanism: the tap
has to wait for the next poll (3 ms in the emulator, up to ~36 ms on the deck
at 28 polls/s) plus one 588-frame block (13.3 ms).

Note for future runs: tools/stems_rec_analyse.py cannot judge a mute whose
tap lands where the vocals are quiet -- it correlates the recording against
the mix and the muted mix, which are 0.92 alike. Pick a vocal-heavy tap point,
or use the A/B above, which needs no reference at all.

## Deck measurement, 2026-11-06: 24.88 s, and why

First stopwatch reading on hardware (XDJ-700, fw 1.15, track playing, mute pad
tapped): **24.88 s** to hear the change. That is the chip's decoded store
(~2100 blocks, ~28 s) running out, which is the signature of a per-pass window
that never covered the buffer.

The module's own counters said the same thing without guessing
(`tools/stems_probe.py`, same boot): `polls 13195`, `parm 1` (one mute armed),
`drblk 173`, `drskip 22`, `drnoslot 26`, `drwfail 21`. 173 blocks is about
2.3 s of audio rewritten near the playhead -- the rest of the decoded store was
never touched.

**Cause:** `dr_end` was computed from `rfront`, the deck's last *read* position.
On the deck that frontier sits only a couple of seconds past the playhead (the
media server reads into two alternating buffers), while the chip has already
decoded ~28 s. The rewrite therefore stopped almost immediately and the mute
was heard only when the old audio drained.

**Fix** (`tools/stems/sm.c`, `dr_run`): aim at the chip's decoded limit
(`p + MAXAHEAD`, clamped as before) and let the slot labels say where the real
frontier is -- `dr_slot()` returns ~0 for a block the chip has not written yet
and `DR_GAP` stops the pass there. Same writes, same labels, no new memory.

Emulator evidence (real DSP core, tap between read bursts,
`tools/stems_fastmute_emu.py new mute`):

    [new] at measure point, buffer 2178 blocks [20s]
      ctl: polls 1710 armed 0 blocks 0 skips 0 reads 110
      chg: polls 702 armed 1 blocks 905 skips 0 reads 110
    RESULT new mute: change heard after 0.000 s

905 blocks rewritten instead of 173, and the change is audible at the measure
point rather than after the buffer drains. Native correctness still PASS (masks
0b0101 / 0b1110 / 0b1111 / 0b0000, 27 reads each, 0 wrong). **Deck re-measure
pending** -- this section is the emulator result plus the deck diagnosis, and
the number that goes in the release notes is whatever the stopwatch says next.

## Deck try of the frontier fix, 2026-11-06: instant, then stutter (not shippable)

The fix above (aim at `p + MAXAHEAD`, 32 s) was installed and tapped on the deck.
What happened, in order:

* at the **start of the track the mute was essentially instant** -- the mechanism
  works;
* then the **audio stuttered and glitched, and the bottom waveform glitched
  too**;
* after that, **mid-song mutes did nothing at all**.

Counters from that boot (`tools/stems_probe.py`): `parm 11`, `drblk 2974`,
`drnoslot 17981`, `drwfail 84`, `drskip 2398`, `drlastb 4644` against
`drlastp 2245`. Aiming 2400 blocks ahead put the pass **past the slots the
decoder had finished with** -- exactly the half-filled-slot write that the
2026-11-03 notes call out as the stutter cause -- and the far-ahead label
matches came from old copies of those blocks, so later passes wrote places the
chip never plays and the mute stopped working. The waveform glitching points at
the shared file offset being moved far more often than before, not at the mixes.

**Current code is bounded instead:** `DR_AIM 1200` (~16 s past the playhead,
inside the ~28 s store but clear of the decode frontier) and `DR_CATCH 48`
blocks per poll instead of 128. The blocks at the playhead are still written
first, in the first poll, which is all "instant" needs; the rest of the store is
fixed up behind playback.

Emulator gates on that build (real DSP core, tap between read bursts):

    native: mask 0b0000: 27 reads (up to 1 MB), 0 wrong
RESULT: PASS
    mute:   chg: polls 713 armed 1 blocks 898 skips 0 reads 110 err None
RESULT new mute: change heard after 0.000 s [90s]
    unmute: chg: polls 1588 armed 2 blocks 1704 skips 1 reads 210 err None
RESULT new unmute: change heard after 0.139 s [145s]

**Deck status: unproven.** No deck time was available after that try, and the
deck stopped enumerating on USB afterwards (switched off or unplugged), so it has
not been re-tested. The next session must (1) power-cycle the deck, which is
stock regardless of what is in RAM, (2) install with `tools/stems_apply.py` and
(3) time one mute at the start and one mid-track. Until then this section claims
an emulator result only -- the 24.88 s deck figure above is still the last real
measurement.
