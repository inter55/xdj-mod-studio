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
measurement. That test has now run: see below.

## Deck measurement, 2026-11-07: 27.8 s with the fast trigger armed

Installed the bounded build in ONE shared session (`tools/deck_session.py`, 35
min, every step PASS), so the PERFORM pads and the stems engine came from the
same boot:

    RESULT: PASS - load a STEMS file; PERFORM > STEMS mutes/solos live (fast trigger on)

Stopwatch on the deck (fw 1.15, mute pad tapped mid-track): **27.8 s** to hear
the change -- no better than the 24.88 s of 2026-11-06.

Same-boot counters, read by absolute symbol address (`build/stems_direct.py`,
because FLAC was installed after stems and now chains above `sm_fread`, so the
fread pool no longer equals it and `tools/stems_probe.py` used to refuse):

    sm_calls 16445   sm_polls 26171   sm_hits 7367    sm_files 14
    sm_maxn 16384    sm_lastn 2560
    sm_drblk 143     sm_drscan 23     sm_drskip 359
    sm_drnoslot 2783 sm_drwfail 10    sm_drbusy 0

    POLL   0x08C1D56C = 0x09836010  (sm_poll_entry -- fast trigger IS armed)
    POOL   0x08D5FFA8 = 0x0998f2e4  ) FLAC on top, forwarding to sm_fread
    DIRECT 0x08DDFFD4 = 0x0998f2f0  )

**What it means.** The fast trigger fires (26k polls, 7.4k hits) but it rewrote
only 143 blocks, about 1.9 s of audio, and `drnoslot` hit 2783 times: the blocks
the pass wanted to fix were already decoded and had no slot left to rewrite. The
deck keeps roughly 28 s of decoded audio ahead of the playhead, so a change made
to the FILE cannot be heard until playback reaches audio decoded after the tap.
27.8 s is that store draining.

The bounded aim (`DR_AIM 1200`) is why there is no stutter this time; it is also
why the mute cannot beat the decoded store. The 2026-11-06 frontier fix did beat
it at track start by rewriting the whole 32 s store, and stuttered.

**Conclusion: this mute cannot be made instant at the file-read layer.** The
file-read path is a dead end; see the next section for the route that is not.

## How to actually get instant (2026-11-07, after the 27.8 s result)

The conclusion above is about the FILE path, and it is right: anything that
changes the bytes the decoder will read pays the whole decoded store. But the
module already has a second write path that does not go through the file at all.

`dr_write()` (sm.c) writes **the chip's decoded ring**: `RING 0xC0090000`, slots
of `SLOT 4804` bytes = 588 stereo frames as float32, with the block number in a
header word at slot+4704, and it writes them through the host port (HPIA/HPID).
That ring is the 28 s of PCM itself, i.e. the playout side of the buffer. A write
that lands there is heard within one block (588 frames, 13 ms), not 28 s later.
So "instant" needs no new layer -- it needs the existing writes to find the
right slots.

They do not, on hardware. `sm_drblk 143` against `sm_drnoslot 2783` says the
lookup, not the writing, is what fails: `dr_slot()` predicts
`s = PSLOT + (b - PLAYBLK)` and then confirms it by reading the slot's header
word; 2783 times the header did not name the block we asked for, so the pass
walked to its aim limit and stopped. `dr_write` only writes slots whose header
already names that block (that check is what keeps the deck from stuttering),
so a wrong prediction means no write at all. 143 written blocks is the residue
that happened to match.

That the ring writes DO work when the prediction holds is not a theory: the
2026-11-06 frontier run was "essentially instant" at track start, before its
far-ahead aim moved it into slots the decoder had not finished with.

**The fix, concretely.** Stop trusting `PSLOT` for the address and calibrate the
mapping instead. Block and slot are related by a constant offset plus a ring
period, so:

1. On arming a mask change (or when the fast path misses), find the slot whose
   header == `PLAYBLK` by scanning headers, bounded to a slice per poll (e.g.
   256 slots/poll, never a full 13846-slot sweep in one call). Record
   `delta = slot - PLAYBLK`.
2. Then `slot(b) = delta + b`, wrapped by the ring period; verify the header
   names `b` before writing, exactly as now, and re-anchor when it does not.
3. Keep everything else: `DR_AIM 1200`, `DR_CATCH 48`, the header check, the
   `dr_slot(b+1)` guard that avoids half-decoded slots.

Cost is the re-anchor scan, which only runs after a miss, a wrap or a seek. The
first poll still rewrites the blocks at the playhead, which is all "instant"
means.

**What the next deck session must prove.** One mute timed by stopwatch with the
calibrated lookup. If it lands under ~0.5 s, the 28 s problem is solved at the
chip's own ring and the two "remaining routes" (shrink the decode-ahead; per-stem
mixer gain) become unnecessary. If it is still seconds, the counters say why:
`drnoslot` large again = the mapping is still wrong; `drblk` large but the mute
late = the writes land in slots the chip is not playing, which points at the
decode/play relationship rather than the lookup.

**Built and installed, 2026-11-07 (evening).** The calibrated lookup is in
`tools/stems/sm.c` (`dr_slot`): a verified-anchor fast path, the old PSLOT
formula kept only as a hint, then a bounded header scan (`SCAN_PER 128` per
call) that re-learns the mapping from the PLAYING block and accepts a slot only
with its successor confirmed (`p` and `p+1`). New counters: `drcal`
(calibrations), `calp`/`cals` (block and slot confirmed), `drps` (PSLOT at that
moment = how wrong the old formula was), `drlab0` (label found in slot 0).

Gates run before install: `tools/stems_native_test.py` PASS (every mask, 0 wrong
reads); `tools/stems_fastmute_emu.py new mute` on the real DSP -- change heard
after **0.000 s**, 898 blocks placed, 0 skips. The emulator's PSLOT already
works, so that gate proves no regression, not the deck fix. Module 4320 B, top
symbol 0x09842950, inside the 0x09844000 cave.

Deck: `tools/deck_session.py --no-hotcues --no-themes --no-flac` finished in 5
min, exit 0, stems `RESULT: PASS ... (fast trigger on)`; the hook read back at
`POOL 0x08D5FFA8 = 0x09836908` (= the new `sm_fread`) and
`POLL 0x08C1D56C = 0x09836010`. All counters read 0 because no STEMS file has
been opened since the install. **Deck timing: measured, and it failed** -- that
build wrote not one block (see "Deck result, 2026-11-07 late" below). It must not
be installed again; `tools/stems/sm.c` is back to the bounded build and the
attempt is kept as `build/sm_calibration_attempt.patch`.

## Deck result, 2026-11-07 late: the lookup calibrated, the writes did not land

`tools/stems_mute_deck.py` (new) performs the mute itself -- it writes the same
PMASK byte the VOCALS pad writes -- then samples the counters. On the deck,
playing a STEMS file:

    pre      blocks 0    noslot 2586 wfail 0 skip 324 busy 0
    +0.05 s  blocks 0    noslot 2625 cal 43 (blk 4433 slot 4865 old-said 481)
    +0.20 s  blocks 15   noslot 2842
    +2.00 s  blocks 15   noslot 3875 cal 75 (blk 4535 slot 4815 old-said 583)

Three facts, in order of importance:

* **The ring labels are readable and the calibration works.** The scan found the
  PLAYING block in the ring -- block 4433 in slot 4865 -- and it passed the
  `p`/`p+1` successor check, so that pair is a real consecutive copy.
* **PSLOT is not the play slot.** It said 481 where the block actually sat at
  4865 (and 5962 against 4326 in the sample before). The 1182-slot error in the
  first sample is almost exactly `DR_AIM` (1200), which is precisely why the old
  formula missed every time: it was aiming at slots a full aim-window away.
* **No blocks are written.** 15 in two seconds, `drnoslot` climbing, `wfail 0`:
  `dr_write` is never reached because `dr_slot` cannot confirm the target block.

The mapping also MOVES: successive calibrations place the same playing block at
slot-minus-block offsets of +432 and +283, i.e. **one block exists in more than
one slot** (a second/re-decoded copy), and slot 0's label advanced from 0 to
6692 while playing. A fixed offset from the playhead therefore does not identify
the copy the chip will play next -- and writing a stale copy is silent, which is
what the 2026-11-06 "later mutes did nothing" report was.

**Conclusion: rewriting the chip's decoded ring is not a one-line fix.** The
ring's copy/bank structure and the word that really names the play slot have to
be mapped before this route can be instant. Until that is done, the bounded
build (mute works, ~28 s) is the only working state, and this calibration build
must not be installed: on it, mute taps do nothing at all.

**A seek does not drop the decode either** -- measured at last from those
2026-10-19 artefacts (`tools/stems_flush_emu.py report`, `build/flush_*.npy`):

    mute vs control:       differs after 25.54 s
    mute+jump vs jump:     differs after 25.08 s
    jump vs control:       differs after  0.37 s

The jump is heard at once because playback MOVED, but the muted audio is still
not heard for 25 s: the deck re-reads, yet what it plays next was already
decoded. So "mute plus a nudge" is dead too, and that route is now disproved
rather than unaudited.

## The chip's store, mapped in the emulator (2026-11-07 night)

`tools/dsp_ringmap.py` reads the store out of a playing emulator snapshot
(`cache/emu_flush_playing.pkl`, real DSP program) and prints the map:

    PLAYBLK 2043   PSLOT(0x1182E81C) 2403   status 2
    slots 13846; headers naming a block 4324; label range 1..391976795
    slot 0..5 labels: 14720 14721 14722 14723 14724 14725
    blocks present in more than one slot: 0
    slot-minus-label offsets: +360 for 3959 slots, -14720 for 360 slots
    slots naming the PLAYING block 2043: [2403]
    play-slot header: 000007fb 00000001 00000000 00000100 0 0 0 0 2 3f7b8400 3f7d1000
    control words equal to the playing slot: 0x1182e81c only

### Install attempts that evening (record of the failures, not of the mute)

The diagnostic build below never got installed: the USB shell kept dropping mid
session. In order:

    attempt 1  0b PASS (deck clean) -> overview step aborted on a SHORT READ at
               0x8dbc5c8 (0/4 bytes)               -> nothing written
    attempt 2  0b itself failed: "couldn't read the deck over USB"
    attempt 3  0b PASS, then PERFORM pads step aborted on a short read at
               0x8d5278c                           -> nothing written
    attempt 4  stopped at 0b (read failure); then the deck stopped enumerating

Two lessons, both now tools rather than notes:

* **A shell that enumerates but answers nothing is wedged.** The deck showed
  both HID interfaces and `<deck serial>`, and returned empty to `$CDJ` and to a
  plain read of `0x08000000`. Recovery: switch off/on AND re-seat the USB cable;
  a fresh open on the same wedged device does not help.
* **Do not judge "stock" by eye.** `tools/deck_ram_state.py` compares every hook
  word with the firmware image (the same authority `tools/deck_check.py` uses).
  The installer's own low-RAM trampolines live at `0x081d....`, which look like
  ordinary firmware addresses; reading `0x08D5FFA8 = 0x081db300` and calling it
  stock was wrong, and cost an install attempt. Stock for that word is
  `0x08c61a6c`.
* Installs are now run as persistent background jobs with the log tailed, never
  inside a shell call that a 5-minute cap can kill: the first killed run left
  five hook words re-pointed with no module behind them, and the installer's
  guard correctly refused to continue until the deck was cycled.

* **A silent reply is a different fault from a short one**, and the read path
  now says so. Both tries used to be spent on a deck that had answered
  nothing, and the message called it a short read. `br()` re-issues the `$CDJ`
  handshake, doubles the wait, and after four tries reports "the deck stopped
  answering over USB - no reply at all", which is the case where the cable or
  the power has to be dealt with rather than the deck being busy.
  `fastload` does the same per read round. Proven with no deck attached:
  `build/test_link_retry.py` runs a mock deck that ignores its first commands
  (`MOCK_DROP_FIRST`) -- (A) the read recovers and returns the firmware's own
  bytes, (B) endless silence is named as silence, (C) a 256-byte block read is
  unchanged. `RESULT: PASS` for all three.

Build that is ready and gated (not installed): `sm_poll_entry 0x09836010`,
`sm_fread 0x0983684c`, emulator mute gate 0.000 s / 905 blocks / 0 skips.

So in the emulator: **PSLOT is exactly the slot being played** (slot 2403 holds
block 2043 = PLAYBLK; 2044 is at 2404, 2045 at 2405); the mapping is a
**constant offset** (slot = block + 360 for 3959 slots); **no block exists
twice**; the header's first word is the block number; and nothing else in the
chip's 8 KB control area holds the playing slot. The `-14720` family is an older
played-from position still in the store -- stale copies of OTHER blocks.

So the old formula `s = PSLOT + (b - PLAYBLK)` is CORRECT under this model,
which is why it always worked in the emulator and why that gate could never
reproduce the deck. The deck contradicts the model twice: PSLOT did not match
the slot where the playing block was found (1182 slots off in one sample), and
one block turned up at two different offsets, which the emulator never does.

**The next measurement has to come from the deck.** The next build keeps the
working mute and, on every mask change, records the 25 header words at the slot
PSLOT names plus the index of the word that holds PLAYBLK (`sm_hdr[]`,
`sm_hdrmatch`, `sm_hdrps`, `sm_hdrp`). That distinguishes the three possible
causes directly: wrong header offset on hardware, a live copy that is labelled
only once it has played, or a PSLOT that means something else on hardware.

## Deck session 2026-11-09: the fast mute WORKS early in a track, fails later

Build: the diagnostic bounded build (sm_hdr capture), installed with
`deck_session.py --mode rgb --no-hotcues --no-themes --no-flac` (4 min, all
PASS, fast trigger armed). STEMS file playing, mutes fired by
`tools/stems_mute_deck.py` (2 s mute, then restore) and by hand.

| when | PLAYBLK | PSLOT | label at PSLOT | first 50 ms | heard |
|---|---|---|---|---|---|
| ~10 s in (first track after install) | 775 | 775 | 775 (match, word 0) | 2 blocks; 150 by 2 s, noslot 13 | (vocals: intro had none) |
| ~25 s in | 1852 | 5393 | 1852 (match, word 0) | 5 blocks; 172 by 2 s | **drums gone + back inside the 2 s window** |
| ~47 s in | 3555 | 731 | 4523 (= PLAYBLK + 968) | 0 blocks, noslot climbing | (no rewrite) |
| user tap, early | | | | | **~2 s, small glitch** |
| user tap, near the end | | | | | **20-30 s (the old slow path)** |

What this settles:
* The label IS header word 0 on hardware (all three captures), and when the
  label at PSLOT equals PLAYBLK the rewrite lands and the mute is heard in
  about 2 s. The ring-write route works on the real chip.
* It breaks when the slot PSLOT names already holds a LATER block (4523 vs
  3555: 968 blocks, ~13 s ahead) -- the chip refills slots ahead of the play
  position, so "PSLOT holds PLAYBLK" stops being true as the track goes on.
  dr_slot only accepts "label == b", finds nothing near PSLOT, and the pass
  stalls (noslot 15225 -> 19725). Fix direction: locate block b by searching
  labels (bounded scan around the last hit / a learned slot-minus-label
  table), never by PSLOT + (b - PLAYBLK) alone; skip blocks whose label is not
  found instead of stalling the whole pass.
* The deck had already READ the whole file (sm_lastpos 62.99 MB) a minute
  into the track, so the file-read path can never carry a mid-track mute.
* SEPARATE BUG (seen live): `probe()` re-runs on every fread from a DIFFERENT
  handle (analysis files, a second STEMS file being browsed/loaded) and sets
  valid=0 + resets the windows -> the STEMS row flips to "NO STEMS IN THIS
  TRACK" and a pending mute is dropped (sm_files 15 -> 25 during the session).
  Fix: switch the current file only on a real track load (preview hash, like
  hc.c/pm.c), ignore reads of other handles instead of re-probing them.
* The mute "glitched a bit" when it landed: the rewrite replaces audio the
  chip is about to play; a short crossfade at the first rewritten block is
  worth adding.

Logs: build/stems_measure_2026-11-09.log; readers build/stems_state.py,
build/stems_hdr.py (read-only, by sm.syms address).

## Fix built 2026-11-09 night (emulator-proven, deck test next)

`tools/stems/sm.c`:
* `dr_slot()`: (1) anchor shortcut -- the last confirmed slot + distance,
  label-checked; (2) the PSLOT guess; (3) NEW: if the label found there is a
  different block, step back/forward by the label difference ONCE and
  confirm (stays in PSLOT's run = the newest copy); (4) the old slot-0 wrap
  path. Every slot is still label-confirmed before a write. Counters
  `sm_drfix` (corrections) and `sm_dranc` (anchor hits).
* File switching only after a track load (preview-hash change, as hc.c/pm.c):
  the first STEMS file read after it becomes current, or a non-stems handle
  read 8 times in a row; other handles pass through untouched. Counters
  `sm_switch`, `sm_foreign`. Native build (SM_NATIVE) keeps the old rule.
* `tools/stems_fastmute_emu.py` PSLOT_SKEW=N skews only the SH's host-port
  read of PSLOT (XDJ_DSP_CS6C=0 path), as the deck reported.

| run (emulator, real DSP) | blocks | noslot | heard |
|---|---|---|---|
| new build | 905 | 0 | 0.046 s |
| new build, PSLOT_SKEW=968 | 904 (fix 1) | 0 | 0.046 s |
| tonight's deck build, PSLOT_SKEW=968 | 0 | 2175 | none in 3 s |

Native: stems_native_test, stems_remix_test, version_remix_test PASS.
Not covered offline: the foreign-handle rule (needs browse-while-playing on
the deck: watch sm_switch / sm_foreign); the glitch at the first rewritten
block (crossfade not built -- judge it on the deck first).

## Deck result of that fix, 2026-11-09 late: writes land on a copy the chip does not play

Same install (4 min, all PASS). User, mid-song DRUMS tap: "glitching /
stuttery noise straight after clicking", mute heard only at ~28 s (unmute
the same), the deck's TIME display lagged. Counters right after:

    sm_drblk 3649  sm_drfix 0  sm_dranc 7363  sm_drscan 1  sm_drnoslot 46
    sm_drwfail 23  sm_parm 3   hdr capture: PLAYBLK 1024 PSLOT 5224 match none
    sm_valid 1 sm_files 1 sm_switch 2 sm_foreign 5428   (flicker fix WORKS)

Reading: the first block was found by the slot-0 wrap path (drscan 1), not
via PSLOT (the label at PSLOT matched nothing and the one-step correction
did not confirm), and the anchor then walked that run: 3649 correctly
labelled blocks rewritten, none audible. => the deck holds MORE THAN ONE
labelled copy of a block and the run we found is not the one played.
"Label == block" is not enough to pick the playing copy on hardware.
The stutter + lagging clock = the rewrite volume (DR_CATCH 128 blocks per
poll through the host port) starving the player task.

Next (if pursued): a one-shot RING MAP at a mute -- every slot's label
copied into SH RAM in slices per poll (13846 words; the cave has ~5.6 KB
left, so store runs {slot, first label, length} rather than raw words) --
read over the cable, to see where the copies are and which run PSLOT/
PLAYBLK actually play from. Also cap the rewrite rate (it must never starve
the player). Until then the shipped bounded build (~28 s) stays.

## Ring map build (2026-11-09, late)

`SM_CFLAGS=-DSM_MAP` builds a diagnostic stems module that REWRITES
NOTHING: on a mask change it reads all 13846 slot labels (256 per poll,
~2 s) and stores runs {first slot, first label, length} (max 256; runs of
one slot counted as junk), plus PLAYBLK/PSLOT at the start and end of the
scan. Emulator check tools/stems_ringmap_emu.py PASS: module's map ==
labels read straight from the chip (2 runs: slots 0..359 = blocks
14720..15079, slots 360..4759 = blocks 0..4399; 9086 junk; PSLOT =
PLAYBLK + 360). Deck reader: tools/stems_ringmap_deck.py. Module ends
0x09842658 (< 0x09844000).

## Ring map ON THE DECK (2026-11-09 ~21:40, <deck serial>) -- root cause found

Raw: build/ringmap_deck2.log, build/ringmap_deck3.log (first run read the
map after the mask restore had restarted it -- reader fixed to read first).
  run A: slots 0..3107 = blocks 6692..9799, 3108..6691 = 3108..6691;
         PLAYBLK 4724, PSLOT 1140 -> block 7832 there. 7154 slots unused.
  run B (~40 s later): slots 0..375 = 13384..13759, 376..6691 = 7068..13383;
         PLAYBLK 4764 (NOT IN THE RING), PSLOT 5140 -> block 11832.
- Deck ring = 6692 slots used (of 13846), one copy per block, block b at
  slot b mod 6692 (emulator differs: no fixed rule assumed).
- Block at PSLOT is ~1950 blocks (~26 s) behind the fill front in BOTH
  runs = the observed ~28 s mute delay (mix-on-read lands at the front).
  => PSLOT is the real play slot on the deck; PLAYBLK (0x11837CCC) is NOT
  the playing block mid-track on hardware (in the emu they agree). Tonight's
  earlier fix started at PLAYBLK -> rewrote blocks already played (3649
  unheard) and its volume caused the stutter/timer lag.
- FIX DESIGN: P = label at PSLOT; walk forward from PSLOT+3: next slot =
  s+1 if its label == b+1, else slot 0 if label(0) == b+1 (wrap), stop on
  mismatch; label-confirm before each write (existing); nearest-first,
  rate-capped (a few blocks per poll, must beat 75 blocks/s); stop at the
  fill front. Never use PLAYBLK for position.

## 2026-11-09 ~22:00: why the PSLOT build was still slow on the deck, and the fix

Deck counters with the 21:48 build (rewrite from label(PSLOT)), after a user
mute during play (build/mute_deck_2201.log has the follow-up):

    drblk 101  drnoslot 1790  drwfail 261  sm_hpibusy 1790  drbusy 0

`hpibusy == noslot`: every "block not found" was really a label read REFUSED
because the deck's own DMA was using the host port. The pass treated a
refusal as "missing", skipped the block and never came back: 2051 of ~2150
blocks were left with the old mix, so the mute was heard at ~28 s again.
Controlled mute from the cable with the deck PAUSED (port idle): 6 blocks in
50 ms, 249 in 2 s, 0 failures, label at PSLOT == block (word 0) -- the
lookup and the writes are right; rate 125 blocks/s (1.7x play speed).

Fix (tools/stems/sm.c):
* `dr_slot` returns DR_BUSY when a label read was refused; `dr_run` then
  WAITS on the same block. A block is left only once written (or once the
  play position passed it). Failed writes are retried the same way.
* Batches: DRN = 3 blocks per stick seek+read per plane (`dr_mix`), written
  from the sums directly (`dr_put`). One batch per poll, at most one per
  10 ms, one per 20 ms once the pass is 2 s ahead of playback.
* MAXB 8192 -> 4704 (the deck reads 9408 B = 2 chunks either way) pays for
  the batch buffers. Module ends 0x0984334C (< 0x09844000).
* The one-step label correction is gone (PSLOT is exact on the deck).

Emulator (real DSP): mute 0.000 s, unmute 0.000 s, mute with PLAYBLK skewed
-1500 (deck-like) 0.046 s; ~390 blocks/s. Ring content after masks 01, 06,
00 compared with the stem planes of the playing file: 619 / 682 / 787
blocks, 0 wrong samples. Native stems tests PASS.
NOT on the deck yet (needs a power-cycle + install). Deck check: mute
mid-song while playing; then tools/stems_probe.py -- drblk should climb to
~2000 within ~10 s, drnoslot stays small, hpibusy may be large (waits).

## 2026-11-09 ~22:40: deck result of the busy-wait + batch build, and what changed after

Installed (build/busyfix_install.log, all PASS). User, mute mid-song while
playing: "stutters and glitches but I think it might've worked slightly".
NO COUNTERS: the deck was off USB before they could be read (unknown whether
switched off or dropped off by itself -- ask next time). So the cause is NOT
measured. Changes made on reasoning alone (emulator-tested, never on a deck):
* An overtaken pass (playback caught up with the rewrite) used to restart 3
  blocks ahead every time -> muted / un-muted blocks alternating. It now
  restarts DR_LEAD = 30 blocks (0.4 s) ahead: one gap, not a flutter. A fresh
  tap still starts 3 blocks ahead. Counter sm_drbump.
* The first block after every start is a 512-frame fade old mix -> new mix
  (old mix computed with the previous mask; skipped mid-pass). sm_drfade.
* sm_drms = longest batch in ms (how long the player task was held).
Emulator: mute 0.000 s, unmute 0.000 s, ring vs stem planes 626/692/790
blocks 0 wrong, native tests PASS. Module ends 0x09843424. The overtaken
path does not occur in the emulator (rewrite runs ~390 blocks/s there).

NEXT DECK VISIT (in this order, deck left ON afterwards):
1. power-cycle, `deck_session.py --mode rgb --no-hotcues --no-themes --no-flac`
2. play a STEMS track 1 min, tap DRUMS once, wait 15 s, then
   `tools/stems_probe.py`. Read: drbump (0 = never overtaken; large = the
   stick/port cannot keep 75 blocks/s -> need a bigger lead or fewer reads),
   drms (> ~30 = batches hold the player too long -> DRN 2 or 20 ms pacing),
   hpibusy vs drblk (how much the port is taken during play), drwfail.
3. If the deck drops off USB by itself after a mute: that is a crash, not a
   stutter -- say so, different problem.

## 2026-11-09 ~23:10 review: writes could land on the block being played

The play position was sampled before a batch's stick reads; on a busy stick
those take 10-100 ms, so the samples could be written into the very block
the chip was playing (a glitch), batch after batch when rewrite speed is
close to play speed. Likely the "stutters and glitches" of the 22:40 deck
test (unproven: no counters). Now: position re-read before every block
write; a block closer than DR_SAFE (2) is not written (sm_drlate) and the
pass restarts DR_LEAD ahead; a fresh tap starts DR_FIRST = 8 blocks ahead.
Emulator: mute / unmute heard after 0.093 s; ring vs stem planes 631 / 697 /
795 blocks 0 wrong; native tests PASS; repro_freeze (all mods) PASS. Module
ends 0x098434E0. The -DSM_MAP diagnostic build compiles again (it did not
after the fade change). NOT on the deck. stems_probe prints drlate/drbump/
drms/drfade/hpibusy.

## 2026-11-10: one-plane single-stem path + the pass can finish

The 2026-11-09 deck runs still stuttered or stayed slow. Two things in
`tools/stems/sm.c` were wrong independently of the ring mapping:

1. **The finish line moved.** `dr_end` was recomputed as `p + MAXAHEAD` on
every poll, so the pass chased playback and never completed. `dr_store_ok`
could never become true, and every tap had to assume an unknown store (full
four-plane re-sum). The line is now fixed once at the tap (`p + MAXAHEAD`,
32 s) and the pass ends when it reaches it. Decoded blocks beyond the line
are mixed by `sm_fread` with the current mask, so they need no rewrite.
2. **The store never became "known".** The pass now also ends at the real
decoded frontier (eight consecutive blocks with no slot) instead of polling
the same missing block forever. That sets `dr_store_ok`; a later tap can
then use the fast path safely.

**One-plane path.** When exactly one stem changes and the last pass
completed, the slot already holds the previous mix: muting is `stored -
plane_k`, un-muting is `stored + plane_k`. That is one USB read per batch
instead of up to four, which is the player-task stall (the stick, not the
DSP or host port). Before the first use in a pass it checks one block's
stored samples against the old mask (`dr_get_cmp`); if they differ, or any
stored sample is at the 16-bit rail, the whole batch falls back to the
existing four-plane `dr_mix()`. This makes a stale/partial ring base
harmless. The first tap after install therefore still uses the four-plane
path; subsequent taps use one plane. New counters: `sm_drsub` (batches via
the one-plane path), `sm_drsubf` (attempts that fell back), `sm_drchk`
(1 = base verified, 2 = full re-sum), `sm_drbase`/`sm_drmask`/`sm_drold`.

Emulator, real DSP core, final build (module ends `0x098439F4`, under the
`0x09844000` cave):

* first mute, tap between read bursts: change heard after **0.093 s**;
* two-tap test (mute one stem, let the pass finish, un-mute it): pass 1
  wrote 2392 blocks, pass 2 used the one-plane path for 160 batches, base
  check passed, and 20 blocks of the playing file matched the stem planes
  with **0 wrong samples**;
* `stems_native_test` PASS, `stems_remix_test` PASS, `stems_overtake_emu`
  PASS (199 blocks, 4 fades, 0 failed writes; slow pacing means it does not
  exercise the one-plane path).

**Deck status: still unproven.** No hardware run since these changes. The
next deck session should mute/unmute a playing STEMS track mid-song and read
`drsub`, `drchk`, `drms`, `drbump`, `drwfail`, `hpibusy`:

* `drchk 2` every time = the stored mix does not match the mask the deck
  reports (fallback is working, but the fast path is off);
* `drms` still > 30 ms = reads still hold the player too long; reduce DRN or
  lengthen the pacing before changing anything else;
* `drsub` large with the mute still late = the one-plane writes are landing
  on the wrong copy (the 2026-11-09 mapping problem, not the I/O path).

Until a deck run, the 24.88 s / 27.8 s stopwatch figures remain the last
real latency measurements; the 0.093 s here is the emulator result.

## Deck session 2026-11-10 ~22:00-22:12 (user's ears; NO counters kept)

Run by another agent; nothing was written down and its readings are gone
(no log, no transcript). What the user heard, muting DRUMS mid-track:

1. the mute was heard after **about 1 s** (was 25-28 s on every earlier
   deck run) -- the ring lookup and the writes are right on hardware now;
2. then a short stutter;
3. then the drums **came back briefly**, then went muted again and stayed.

Reading (not measured): (3) is the pass being overtaken by playback once --
old-mix blocks play until the pass restarts ahead (`sm_drbump`). After that
session `dr_lead` was capped at 60 blocks (0.8 s) in sm.c (22:16) and the
module rebuilt; that edit is emulator-tested only.

Why it can be overtaken: a batch is 3 blocks, then a 10 ms pause. If a batch
takes ~25 ms on a playing deck (stick reads behind the deck's own reads, port
busy waits), that is ~86 blocks/s against 75/s of playback and ~70 % of the
player task's time: almost no margin (overtaken on any hiccup) AND a starved
player task (stutter). Both symptoms from one number nobody has read yet:
the batch time.

Changes after this session (review, emulator only):

* `dr_get`/`dr_get_cmp` (one-plane path, new 11-10) read a whole block with
  interrupts off -- 1176 port round trips in one go, where `dr_put` was cut
  into 8 pieces precisely because long masked sections stutter. Now 8 pieces
  too (`dr_get8`).
* new counters `sm_drbat` (batches) and `sm_drtot` (ms spent in them):
  average batch cost = drtot / drbat, next to the longest (`sm_drms`).
* `tools/stems_probe.py` now appends every run to
  `docs/deck_logs/stems_probe_<date>.log`. Readings were lost on 11-09 and
  again on 11-10; this is the fix for that.

**Next deck visit (5 minutes):** install, play a STEMS track, tap DRUMS once
mid-song, wait 30 s, run `tools/stems_probe.py` BEFORE touching anything
else. Decide from: `drbump` (times overtaken), average + longest batch ms,
`hpibusy`, `drsub`/`drsubf`, `drlate`. If the average batch is > ~15 ms the
fix is fewer stick reads per second near the playhead (one-plane path from
the first batch, bigger batches further ahead), not more speed.

## 2026-11-11: the "drums came back" bug found -- the stem was taken out twice

`tools/stems_ringcheck_emu.py` mutes one stem, plays 14 s and compares every
block ahead of the playhead with the file. On the build of 2026-11-10 22:16
(what the deck most likely ran):

    1058 blocks = original mix minus drums   (the pass, one-plane path)
     224 blocks = that minus drums AGAIN     <- 3 s of drums, inverted
     696 blocks = sum of the other three     (read by the deck after the tap)

The pass's finish line was "play position + 32 s". Everything the deck reads
after the tap is already mixed with the new mask, and the pass ran on into
those blocks and subtracted the stem a second time. Heard on the deck as:
muted quickly, then the drums back for a few seconds some time later, then
muted again. The fix: the finish line is the file offset where reads with
the new mask begin (known exactly at the tap), the one-plane path never
crosses it, and a block short of the line that has not reached the chip yet
is waited for. Same test on the fixed build: 1058 + 880 good, 0 double.

**Re-run 2026-11-11 22:14 on the shipped `build/stems/sm.bin` (built 08:47,
newer than `sm.c` 08:21, so it carries the fix):**

    ringcheck: play block 3299; drblk 2168, drsub 723, drsubf 0, drchk 1,
      drbump 0, drlate 0, drfade 1, drwfail 0, drms 20, drend 4360
      1938 blocks ahead of the playhead: sub 1058, new 880, orig 0,
      double 0, other 0   -> RESULT: PASS
    overtake: 2184 -> 2503; drblk 311, drbump 1, drlate 0, drfade 2,
      drwfail 0, drms 33; orig 408 / new 309 / other 2, switches 2
      -> RESULT: PASS
    native: every mask 27 reads, 0 wrong -> PASS

So the double-subtraction fix holds on this exact binary, and an overtaken
pass still ends with one gap and no failed writes. Still no deck run: what a
deck has to answer is the stutter (`drms`, `drbat`) and whether the mute is
heard with the stem NOT coming back.

## 2026-11-11 late: the stutter itself -- masked sections cut to 64 samples

Every masked section of a block holds interrupts off, and the 2026-11-09 deck
note names exactly that as the stutter (long masked sections = the player task
stalled). A block was cut into **8 sections of 147 samples**; `DR_PIECE` caps a
section at **64 samples** (19 sections per block) at the same total work, so
the longest wait for an interrupt drops by 56 % and the audio is identical.
`sm_drpk` (new) records the longest section actually done, and
`tools/stems_probe.py` prints it next to `drms`/`drbat`.

Emulator, shipped `build/stems/sm.bin` (`tools/stems_ringcheck_emu.py`):

    play block 3299; drblk 2168, drsub 723, drchk 1, drbump 0, drlate 0,
      drfade 1, drwfail 0, drms 22, drend 4360
      1938 blocks ahead: sub 1058, new 880, orig 0, double 0, other 0
      -> RESULT: PASS
    native: every mask 27 reads, 0 wrong -> PASS

A/B on the same snapshot (`tools/stems_ringcheck_emu.py`, 724 batches, 1938
blocks checked each run; the only difference is `-DDR_PIECE=147`):

| sections per block | `drpk` (samples) | `drms` (ms) | `drbat` | `drtot` (ms) | mean (ms) |
| --- | --- | --- | --- | --- | --- |
| 8 (the old split) | 147 | 20 | 724 | 6833 | 9.44 |
| 19 (`DR_PIECE` 64) | **64** | 22 | 724 | 6848 | 9.46 |

Kept: the longest masked section is 56 % shorter (147 -> 64, and `drpk` proves
it) while the mean batch cost is unchanged (9.44 -> 9.46 ms, +0.2 %). The
longest batch moved 20 -> 22 ms, inside the 20-33 ms this same code shows run
to run. Rewritten blocks and ring content are identical in both runs.

The value is **by construction, not measured**: `DR_PIECE` 64 makes `drpk` 64
(19 sections per block, the last 24 samples), and the emulator reproduces
exactly that. Nothing here is a hardware measurement — a deck run should read
`drpk` 64 next to `drms`/`drbat`, and if a stutter is still heard with `drpk`
64 and a small batch time, the cause is elsewhere (stick reads, not the masked
sections): the next lever is fewer reads near the playhead.

Still open (deck only): the short stutter. Read `stems_probe.py` 30 s after
a mute -- it now prints the average and longest batch time and saves the
output under `docs/deck_logs/`.

## 2026-11-12 night: NEW DESIGN -- short read-ahead (no ring rewrite), emulator PASS

After the deck said "stutter, very bad" for the ring rewrite, the design was
restarted. Step 1 of 2:

- `tools/stems/sm.c` `throttle()`: at the top of `sm_fread`, a read of the
  current STEMS file that lands more than `SM_AHEAD` blocks (1/75 s) past the
  playing block (`play_blk()`: label at PSLOT) waits in 5 ms steps with the
  firmware's own sleep `0x08E0335C` (dly_tsk-like: 448 callers with 1..50,
  checks ISR / dispatch-disabled flags 0x0B0E5D36/37, then sleeps) until
  playback catches up. The mask is read AFTER the wait, so mix-on-read uses
  the newest mask. The audio chip then never holds more than ~SM_AHEAD of the
  old mix: the stable 1.0 mix-on-read path mutes within that.
- Safety: play block unknown or standing still for STILL_MS (paused, cued,
  loading) -> read at once; one call never waits more than MAXW_MS (3 s deck
  default). Timing uses the 1 ms system clock 0xAB704A80 (uncached).
  EMULATOR GOTCHA: that clock runs ~6x fast against emulated audio, so the
  emulator build uses -DMAXW_MS=30000u -DSTILL_MS=1000u.
- `-DSM_NODR` = no ring rewrite at all (poll hook left stock, dr_* never run).
- Counters: sm_thr (reads held), sm_thrms (ms held), sm_thrpause (gave up:
  play position not moving).
- tools/stems_shortahead_emu.py (cache/emu_flush_playing.pkl): plays until
  the buffer (0x11837CD0) shrinks, then control vs one-stem-muted runs.
  4 s limit: buffer drains from 1753 at play speed, settles at 278..284
  blocks; mute heard 3.95 s after the tap; play counter +638 in 8 s (real
  speed 600), longest flat stretch 0 ms, no DSP error: PASS
  (build/stems2/shortahead_4s.log).
- Side effect to check on the deck: after a load or a long pause the buffer
  starts full (~30 s) and takes ~26 s of play to drain to the limit; mutes in
  that time are still slow. Fix idea: also hold reads while cued (the load
  itself needs only 180 blocks = 2.4 s, PLAYBLK poll 0x08C1D366).
- Step 2 (instant): mix in the audio chip at output (same hook family as
  Touch FX / key shift), with the 4 stems kept in the chip's memory -- only
  possible because the read-ahead is now short.
- 1 s limit (SM_AHEAD=75): FAIL. Below ~160 blocks (~2.1 s) the deck slows
  playback to ~8 blocks/s (play counter 3835 -> 3933 in 12 s) -- its own
  "buffer low" rule (the load gate wants ~180 blocks = 2.4 s, player poll
  0x08C1D366..). So this method alone bottoms out at ~3-4 s. Going lower
  would mean moving that firmware threshold too (it guards real stick
  underruns). Default stays SM_AHEAD 300 (4 s). Instant = step 2 (mix in
  the audio chip), which only needs the stems for this short window in the
  chip's memory (~3-4 s x 4 stems ~ 2.8 MB as 16-bit).

## 2026-11-13: step 2 needs one deck answer -- does the audio chip have 64 MB?

- Emulator: the firmware never touches DDR at/above 0xC2000000 (last nonzero
  byte 0xC1F3F41F while playing; ring 6692 x 4804 B ends just under 32 MB).
  The emulator models 64 MB; the real part size is unknown (stage-1 / main
  image never program EMIFB SDCR from a constant we could find).
- tools/ddr_size_probe.py (+ tools/ddrprobe/dp.c, build/ddrprobe/dp.bin @
  0x09800000, the key-shift one-shot area): READ-ONLY on the chip. One-shot
  on the flush hook reads 16 DDR words at 0xC0000000+off and 0xC2000000+off
  over HPI (IMASK=15 briefly, HPIA restored), unhooks. 32 MB = every pair
  equal (wrap). Mock deck (emu_flush_playing): runs, "64 MB" (as modelled).
- 64 MB -> stems live in chip DDR above 32 MB, mixed at output: instant.
  32 MB -> SH-side rewrite of the short (~4 s) window right after a tap
  (old fast mute, ~7x less work now).

## 2026-11-13: the audio chip has 32 MB (offline answer, emulator boot log)

Booting the chip from flash in the emulator and logging every write to the
EMIFB SDRAM controller (0xB0000000..): stage-1 (pc 0x11802A00..0x11802A34)
writes SDCR 0x0080C621 -> 0x00814621 -> 0x00014621, SDRCR (0x0C) 0x3F6,
SDTIMR 0x10912A08, SDTIMR2 0x600A0007. SDCR PAGESIZE=1 (9 column bits),
IBANK=2 (4 banks), NM=1 (16-bit bus): with the usual 13 row bits that is
4 x 8192 x 512 x 2 B = 32 MB (a 256 Mbit x16 part). Matches the firmware
never touching DDR >= 0xC2000000 and the ring ending just under 32 MB.
=> no free chip memory for stems. tools/ddr_size_probe.py still worth one
deck run to confirm, but the design no longer waits on it.

Instant-stems design for 1.1 therefore = short read-ahead (step 1) + the
ring rewrite (old fast mute), which now only has the ~4 s window to fix
(~300 blocks instead of ~2000), nearest block first.

## 2026-11-13: INSTANT STEMS in the emulator -- 0.12 s, all four stems

- Combined build (now the DEFAULT): short read-ahead (throttle, SM_AHEAD 300
  blocks) + the ring rewrite (old fast mute), DR_FIRST 10 (0.13 s ahead).
  tools/stems_shortahead_emu.py from a settled state (BASE=...): every stem
  (vocals/drums/bass/other) differs at the output 0.12 s after the tap; 288
  blocks rewritten, play counter at exact real speed, no flat stretch, no
  chip error.
- The earlier "2.55 s / 3.95 s" readings were the TEST: first_change() only
  counts a difference above 25 % of the music's level, so a quieter stem
  muted on time looked late. The test now uses "mean |diff| > 8 LSB over 512
  samples" (both runs are deterministic) and prints the level drop.
- tools/stems_ringcheck_short.py SNAP STEM SECS: classifies the chip's
  buffered blocks after a tap. 0.3 s after the tap: blocks +0..+81 = new
  (file mix minus the stem, the one-plane path), +82..+273 still old (the
  pass was still going), the newest = new (mixed on read). Note the file's
  mix is NOT exactly the sum of its stems (separation), so "sum of the
  other three" never matches a rewritten block -- compare against
  "mix -/+ stem".
- Mock install (stock dspsound): 5g PASS; verify_install flags the PERFORM
  touch pool = hc_tentry (hot cues chained before the pads) -- verifier gap,
  not a stems fault.
- DECK: still the open question is the rewrite's cost on the real player
  task (the stutter). Now ~300 blocks per tap instead of ~2000. Right after
  a load or a long pause the buffer is still big (reads only crawl while
  paused: 150 ms per read), so a tap then rewrites more.

## 2026-11-13 night: deck says the instant rewrite still glitches; clean 4 s mute is the default

- Deck (DR_FIRST 64, all mods): 1 change, 137 blocks rewritten, drbump 2,
  last pass 1 block ahead of playback -> old/new alternating = glitches,
  then the read-ahead's muted audio ~4 s later. ~10 ms per block (max 30).
- Default build is now SM_NODR (short read-ahead only, SM_AHEAD 300 = 4 s,
  mute heard ~4 s after the tap, emulator clean). -DSM_DR = instant rewrite.
- 240 blocks (3.2 s) FAILS in the emulator: the deck slows playback to a
  crawl (play counter +17 per 2 s); the earlier 1 s failure was the same
  rule. 300 keeps the buffer at ~278 and plays at real speed.
- Timers added for the instant rewrite (sm_tget / sm_tusb / sm_tput / sm_tn:
  ms per part; stems_probe prints "per block: chip read / stick read / chip
  write"). Next deck test with an SM_DR build tells which part to make
  cheaper (bigger stick reads in the media task? fewer chip transfers?).

## 2026-11-16: "extremely glitchy" explained -- the rewrite lost the race; clean build hardened

Deck (<deck serial>), build = short read-ahead + rewrite (SM_DR, DR_FIRST 64,
PCN 4), one DRUMS tap ~5 s into a track. Probe: drblk 178, drsub 174,
drbump 1, drfade 3, 184 batches in 946 ms (5.1 ms each), drlastp 663 /
drlastb 699.

**What the numbers say.** The window was the expected ~300 blocks (finish
line = block 699 = tap + 300). 184 batches took ~3.5 s of playback, so the
pass really ran at **~52-67 blocks/s** (5.1 ms work + 6 ms pace + ~8 ms until
the player task polls again), under playback's 75/s. It started 64 blocks
ahead, was caught once (drbump), restarted 60 ahead, and finished 36 ahead.
Heard: drums on 0.85 s -> off -> **back on ~0.8 s** -> off, three fades.
That is the glitch; it is in the design, not a bad write (drwfail 0,
drlate 0).

**Reproduced in the emulator** by running the same build at the deck's
rewrite speed (`-DDR_PACE_MS=130u`, the emulator's 1 ms clock is ~8.6x fast:
1 block per ~15 ms): 176 blocks rewritten, **drbump 1** -- the deck's 178 / 1
(`build/stems_1116/short_slow64.log`). Same speed with the start 120 or 160
blocks ahead: bump 0 (`short_slow120.log`, `short_slow160.log`; at 120 the
pass ends only ~26 blocks ahead, at 160 ~80).

**The short read-ahead on its own was NOT shown to glitch.** The "35.8 s
held" is normal: every read waits ~53 ms for playback to move 4 blocks, and
184 of the 430 holds are the 150 ms "not playing" exits (the track sat cued /
paused). With a slow stick simulated (`-DSM_SIMLAT=40u`: ~4.7 ms per stick
trip, 7 trips per muted read) the chip's buffer stayed at 302..324 blocks
(limit 340) and 262..284 (limit 300); playback at full speed, no flat
stretch (`short_lat.log`, `short_lat300.log`). Not proven on hardware: no
deck has ever run the read-ahead WITHOUT the rewrite. That is Test 1 of
docs/DECK_SESSION_NEXT.md.

**Changes (tools/stems/sm.c):**

- `SM_AHEAD` 300 -> **340** (4.5 s). The deck slows playback when the chip's
  buffer nears ~240 blocks; 300 left ~40 blocks over that, 340 leaves ~80.
- **Run rule** (`thr_note` / `thr_ok`, native-tested): the first SM_AHEAD
  blocks of every run of reads pass at once. Before, after a jump forward
  past the buffer (hot cue, beat jump, needle) every read at the new place
  was held -- 3 s each while the old audio played, 150 ms each once it
  stood still. Found by reading the code; never observed, never tested with
  a real jump (the emulator cannot jump). Only ever passes a read sooner.
- New counters (stems_probe prints them): `sm_thrlo`/`thrlopre` (lowest
  read-ahead at which a read arrived un-held), `sm_lvl`/`lvlmin`/`lvlpre`
  (chip buffer level 0x11837CD0 -- an emulator word, unproven on the deck),
  `sm_rdms`/`rdtot` (ms per muted read), `sm_thrmaxw`, `sm_runs`.
- `-DSM_SIMLAT=N` (emulator only): N clock-ms sleep before every stick trip.
- Three builds, picked with `STEMS_BUILD=` in tools/stems_apply.py (so also
  through deck_session): **clean** (default: read-ahead only, 4.49 s in the
  emulator, the poll hook is no longer armed), **stock** (`-DSM_AHEAD=0u`,
  the 1.0 read path, ~30 s), **instant** (`-DSM_DR -DDR_FIRST=160u`: 2.12 s
  in the emulator, also at deck speed). build.sh: `SM_OUT=` builds a variant
  elsewhere, and it now refuses a module that ends past 0x09844000 (instant
  ends 0x09843F1C: 228 B left).
- stems_apply saves the installed build's symbols as
  `build/stems/sm_deck_<date>_<time>.syms`, so the probe still reads the
  deck after build/stems is rebuilt with other flags.

**Gates (final sources):** native PASS (incl. the new hold/pass test);
shortahead clean 4.49 s PASS, instant 2.12 s PASS; ringcheck_short on the
instant build: 0.3 s after the tap +0..+135 old, 1 fade block, then new;
ringcheck_emu (instant, full buffer): 1016 blocks ahead, all new/sub, 0
orig/double/other PASS -- its old ">1500 blocks" rule was stale (with the
read-ahead short the deck reads nothing more for the 14 s of the test);
repro_freeze PASS.

**Known limits, not fixed:** (1) while a track sits cued or paused the deck
still reads one 4-block chunk per 150 ms, so after ~80 s cued the buffer is
back at ~30 s and the first mutes after PLAY are slow until ~26 s have
played. (2) The instant build still spends ~27-45 % of the player task for
~4 s per tap; whether THAT stutters is a deck question.

**Next design step for instant, if wanted:** rewrite the window BACKWARDS
from its far end. Playback then meets the pass at one point (~2.4-2.7 s at
the deck's speed), everything after it is new, and there is nothing to
overtake at any rewrite speed; a slower rewrite just moves the meeting point
toward 4.5 s.

## 2026-11-17: "fast" build (-DSM_DR -DDR_PACE_MS=2u -DDR_FIRST=40u)

User: 2.1 s "still not instant", passable only if it does not glitch.
- Normal emulator speed: mute heard 0.52 s, 298 blocks rewritten, 0
  overtakes, playback at full speed (build/stems_1116/short_fast.log);
  ringcheck_emu: 1017 blocks ahead all new/sub, 0 orig/double PASS.
- Deck-speed simulation (pace scaled to 93 ms in emulator time, a rough
  estimate of 5.1 ms work + 2 ms pause + the poll gap on the deck): the pass
  rewrote exactly as fast as playback (20 blocks per 0.25 s each), lead
  stayed at 39 blocks, no overtake -- but NO margin
  (short_fastslow.log). On the deck a slow moment (the deck's own read
  bursts) would let playback catch it = the glitch again.
- First try (2 blocks per batch) did not fit before 0x09844000 (ends
  0x09844034). The build script refused it but left the files behind, so
  the first test round ran on a bad build; it now deletes them on refusal.
- STEMS_BUILD=fast in stems_apply; Test 3b in docs/DECK_SESSION_NEXT.md.
- To get real margin the per-block cost (chip read 1.1 ms, stick 1.7 ms,
  chip write 0.8 ms on the deck) has to come down, or the rewrite has to
  move off the player task. Neither is built.

## 2026-11-17 deck (<deck serial>): CLEAN build -- no mute within 30 s

deck_session --mode rgb --touchfx --phasemeter --no-flac, `stems build: clean`
(all steps PASS, 20 min). User: Get Lucky (STEMS) played 30 s clean, jumped
back to the start, tapped DRUMS: nothing heard within 30 s.
stems_probe right after (mask 02):
  calls 6365, hits 781 (muted reads), files 1, valid 1, lastpos 9.06 MB
  short read-ahead: 1416 reads held, 88479 ms held, 249 let through
  lowest read-ahead at a read: 271 blocks (since tap and before)
  chip buffer level: now 1568, lowest 1473 since the tap, 1200 before
  muted reads: avg 25.5 ms, longest 38 ms; holds ran out 1; read runs 3
READING: the throttle THINKS the reads are 271 blocks (3.6 s) ahead, but the
chip holds ~1500 decoded blocks (~20 s). Same as the 11-09 ring map: the
playing-block word used for "ahead" lags the real playhead on hardware
(PLAYBLK is not the playing block). So the clean build's 4.5 s premise does
not hold on the deck: real delay ~20 s+. Fix direction: measure "ahead"
against the chip buffer level (or the PSLOT label), not PLAYBLK.
Also: stems_probe.py crashed at the end ('int' has no write: h reused after
close in the pool/direct line) -- the counters above printed first.

### Caveat on that run: the tap followed a jump BACK

The sequence was play 30 s -> jump back to the start -> tap DRUMS. A jump
backwards plays audio that is already decoded, so nothing can change it
without a ring write: the delay is the distance jumped PLUS the read-ahead.
The deck had read 9.06 MB (~51 s) by then, so a 30 s listen could not have
heard the change even if the throttle were perfect. The run is therefore
inconclusive about the gate -- and it is why Test 1 of DECK_SESSION_NEXT.md
says to tap with no jump.

## 2026-11-17 night: the gate is the CHIP'S OWN BUFFER LEVEL (emulator gate still open)

`throttle()` now holds a read while the audio chip's buffer level is at or
over `SM_AHEAD`, instead of estimating the playhead from a DSP word:

* `thr_lvl()` reads `LEVEL` 0x11837CD0 -- the word the emulator gates have
always used as "blocks buffered" (1/75 s each). Valid range 1..8191; a port
refusal or a value outside that (a deck where the word means something else)
falls back to the old position rule.
* The run rule is unchanged: the first `SM_AHEAD` of a run (load, jump, loop)
passes at once, which is what keeps a hot cue from stalling.
* New counters `sm_lvlh` (holds decided by the level) and `sm_posh` (by the
fallback). `tools/stems_probe.py` prints them (`holds: chip buffer N,
position fallback M`): `posh > 0` on a deck means the word is not usable
there and the old rule ran.

Why it should fix the deck: the buffer level is exactly the quantity that
decides how much old-mix audio is still queued, so the mute delay is bounded
by `SM_AHEAD` by construction -- no playhead calibration, no wrap or
duplicate-slot assumptions, and a forward jump cannot confuse it. It also parks
the buffer at ~340 blocks instead of the 271 the position gate produced, i.e.
further from the deck's ~240-block low-buffer slowdown.

Build: clean (default flags), module ends 0x0983C72C, FPU 0 (the level read
costs ~216 B; the clean build still has ~30 KB free, the instant build has 12).

Gates:
* `tools/stems_native_test.py` PASS (run rule + every mask, 0 wrong reads).
* `tools/stems_shortahead_emu.py`: NOT completed. On this Mac the emulator
  runs at roughly 60-85 s of wall time per emulated second of audio, so the
  harness's two 8-second legs need ~25 minutes, and every attempt was cut off
  (the runner restarted once; background/PTY runs stopped early). What was
  observed from `cache/emu_flush_playing.pkl`'s settled base
  (`build/shortahead_base_clean.pkl`, level 278-300 blocks) over 2.5 s: the
  level drained at the play rate (80 blocks/s) and **no `sm_fread` call
  arrived at all** in that window -- the deck's reads are bursts -- so neither
  the gate nor its counters were exercised. That is a limitation of this
  snapshot's short window, not a result; the emulator claim for this change is
  open.
  What does still hold offline: the deck build's gate is inactive whenever the
  buffer is below 340, which is exactly the state the 11-13 gate measured
  (4.49 s), so the mute path itself is unchanged there.
* `instant` and `fast` builds still fit, but barely: after the level read the
  instant build ends 0x09843FF4 (12 B left of 0x09844000) and fast 0x09843FEC
  (20 B). Any further change must free bytes first.

Also fixed: `stems_probe.py` used `h` for the hit count, shadowing the deck
handle, so `h.close()` crashed at the end of every run (the 11-17 counters
nearly got lost that way). It is `nh` now.

**Next deck visit:** the same Test 1 (no jump), then read `holds:` -- `chip
buffer` should dominate and `position fallback` stay 0.

## 2026-11-17 late: the delay was DR_FIRST, not the rewrite speed (0.09 s in the emulator)

Every "instant" measurement we had was just the start distance. The rewrite
begins at `PLAYBLK + DR_FIRST` and walks forward, so the blocks between the
playing block and that start are **never rewritten** and the drums keep playing
for them. `DR_FIRST / 75` is the delay, exactly:

| build | DR_FIRST | predicted | measured (emulator) |
|---|---|---|---|
| instant (old) | 160 | 2.13 s | 2.12 s |
| fast (old) | 40 | 0.53 s | 0.52 s |
| **instant (new)** | **8** | **0.11 s** | **0.09 s** |

Rewriting faster never helped the first second because those blocks were not
being rewritten at all. Starting AT the playhead is what makes it instant; the
rewrite only has to stay ahead of playback from there.

**Changes (tools/stems/sm.c unchanged; flags + build table):**

* `instant` = `-DSM_AHEAD=340u -DSM_DR -DDR_FIRST=8u -DDR_PACE_MS=2u`
  (8 blocks = 0.11 s of lead, 2 ms pause between blocks).
* `fast` kept as the old 40-block start for comparison.
* **Build-table bug found and fixed:** neither DR build passed `-DSM_AHEAD=340u`,
  so a `STEMS_BUILD=instant/fast` deck run would have had NO short read-ahead
  and NO level gate -- the rewrite would have faced the deck's whole ~20 s
  buffer (the 11-07 situation) instead of the 4.5 s window it can cover.
  Both entries now carry it.

**Emulator gate** (`stems_shortahead_emu.py`, `DR=1`, real DSP, settled at the
level gate: buffer 2073 -> 340 blocks, 89 reads held):

    mute heard after: 0.09 s
    chg: rewrite blocks 352 sub 351 bump 0 wfail 0 batches 352
      first second after the change: control rms 7249, muted rms 5558, difference rms 2317
      ctl/chg: play counter +638 in 8 s (real speed = 600), buffer 340..362 blocks,
               longest flat 0 ms, err None
    RESULT: PASS [358 s wall]

So: the whole 352-block window is rewritten, 351 of them by the one-plane
path, playback never overtaken, never slowed, no flat stretch, mute confirmed
as the new mix. `tools/stems_native_test.py` PASS (run rule + every mask, 0
wrong). Module ends 0x09843FEC, 20 B left of the 0x09844000 (TOUCH FX) limit.

**What is NOT proven: the deck.** The emulator writes blocks ~3x faster than
playback, so it cannot show whether the pass keeps up on hardware. The budget
says it should: playback consumes one block per 13.3 ms and the deck's rewrite
costs ~7 ms/block (5.1 ms work + 2 ms pace, 11-16 counters), i.e. ~1.9x
headroom. If a stick or CPU stall eats more than ~6 ms per block on average,
playback overtakes and the drums return until the pass restarts -- visible as
`bump` > 0 in `tools/stems_mute_deck.py` and `stems_probe.py`. That counter is
the deck's verdict.

## 2026-11-23 DECK (<deck serial>): clean build = 5 s, no crackle (HARDWARE PROVEN)

- instant build (SM_AHEAD 340 + DR, DR_FIRST 8, pace 2): mute after a few
  seconds of CRACKLE, unmute crackles, vocals "randomly coming back".
  Counters: 202 blocks rewritten, drbump 2 (playback overtook the pass twice),
  avg batch 5.2 ms / longest 21 ms, 1.0 ms chip read + 1.7 ms stick read +
  1.0 ms chip write per block. The rewrite race is the crackle.
- clean build (STEMS_BUILD=clean = -DSM_AHEAD=340u, level gate, NO rewrite):
  user "drums muted after about 5 seconds, no crackle". Counters: 63 reads
  held (14.5 s), lowest read-ahead 344 at a read, chip buffer lowest 323
  since the tap (deck slows near 240), 225 muted reads avg 21.9 ms / max 34,
  holds that ran out 0, holds by chip level 62 / position fallback 2. The
  level word 0x11837CD0 works on the deck.
- => recommended default for 1.2: STEMS_BUILD=clean (stems_apply default is
  still "stock" = 1.0 path, ~30 s). Unmute not reported yet.
