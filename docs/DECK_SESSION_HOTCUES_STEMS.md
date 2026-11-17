# Deck session: D–H hot-cue recall + stem mute (one sitting)

Two fixes are waiting for a deck run: the 8-hot-cue recall (pad numbers +
pad reload in `tools/hotcue8/hc.c`, §10 of `HOTCUE8_GRID.md`) and the stem mute's
double-subtraction fix (`tools/stems/sm.c`, `STEMS_MUTE_LATENCY.md`). Both
pass every emulator gate; neither has been heard on hardware.

Everything below is **read-only on the deck and the stick**: the two probes
only read RAM. Nothing here writes to your USB stick.

## 0. Before you start (2 min)

1. Install the mods from **this working tree** over the cable, then
   power-cycle the deck (the mods are RAM only, so a cycle gives a clean
   start). **1.0.9 cannot test either fix:** its `hc.bin` writes cue numbers
   4–8 into the pad slots, and its `sm.bin` still subtracts a stem
   twice. Only an install from this folder tests anything here.
2. Prove the deck really runs that build before you test anything:

   ```bash
   .venv/bin/python tools/hotcue_probe.py --check
   ```

   It reads the hot-cue module back off the deck and compares it byte for
   byte with `build/hotcue8/hc.bin`. **MATCH** = this build. **DIFFER at
   +0x…** = another build (reinstall: with 1.0.9 both the pad test and the
   counters are meaningless). With no deck to hand, `--selftest` proves the
   comparison itself (equal bytes pass, one wrong byte is located) without
   touching USB.
3. Pick the track you will test with — one of these, not a mix:
   - **the test stick** (`tools/hotcue8_emu.py`): cues A–H sit at 5, 13, 21,
     29, 37, 45, 53, 61 s, so §1's table applies exactly;
   - **any rekordbox track that has A–H**: ignore those seconds, and write
     down each pad's own time from the deck; pad n must land on cue n's own
     time.

## 1. D–H recall (3 min)

In PERFORM, **tap** the HOT CUE tab: the stock `A B C REC CALL` row becomes
eight pads `A–H`.

Press each pad once, from A to H, letting it play ~1 s and releasing:

| Pad | must jump to |
| --- | --- |
| A | 0:05 |
| B | 0:13 |
| C | 0:21 |
| D | 0:29 |
| E | 0:37 |
| F | 0:45 |
| G | 0:53 |
| H | 1:01 |

On your own track, read the same table as "pad n must jump to cue n's own
time": the point is that each pad lands on its own cue, not these seconds.

Then, specifically:

1. Press **D** twice in a row (the second press exercises the per-flush
   re-arm, not just the first-time path).
2. With the deck stopped, hold **E**, release, and confirm it returns to
   **E's own time**, not B's.
3. Tap the tab back to the stock row, then swipe to page 2 (D–F) and back to
   page 1: A–C must still be at the times they had before.

Finally, keep the counters:

```bash
.venv/bin/python tools/hotcue_probe.py
```

It prints the slot and pad-model dump either way, and the module counters
**only** when the deck's module matches `build/hotcue8/hc.bin` — otherwise it
says `DIFFER at +0x…` and refuses, because the offsets would belong to another
build. It appends to `docs/deck_logs/hotcue_probe_<date>.log`; send that file.

**Pass:** every pad jumps to its own time, and E returns to E.
**Fail:** note exactly which pad plays which time (e.g. "D→A, E→B, F→C,
G→A, H→B") and send the log — that mapping says whether the record is being
overwritten or never read.

## 2. Stem mute (3 min)

Load a track that has a stems version, play it for about a minute (so the
decoded buffer is full — the hard case), then tap **DRUMS** once.

Listen for exactly three things:

1. Is the mute heard quickly (about a second, not ~28 s)?
2. Is there a stutter as it happens?
3. **Does the drums come back for a few seconds?** (This was the bug fixed on
   2026-11-11; a brief return means the old build is running or the fix did
   not hold.)

Wait 15 s with nothing else touched, then read the counters:

```bash
.venv/bin/python tools/stems_probe.py
```

Its first lines name the stems build it found on the deck (`stems build on the
deck: …`) and it refuses a symbol map that does not match that build's
`sm_fread`; check that it is **this** build (`build/stems/sm.syms`), not
1.0.9's. It appends to `docs/deck_logs/stems_probe_<date>.log`; send that file
too.

How to read it:

| Counter | Good | What it means if not |
| --- | --- | --- |
| `drblk` | climbs to ~2000 within ~10 s | the pass is not running (no stems file, or the mask was already set) |
| `drsub` | large, `drsubf` small | `drchk 2` every time = the stored mix never matches the mask; the fast path is off, the fallback is working |
| `drbump` | 0–1 | larger = playback overtook the rewrite (stick/port too slow) |
| `drlate` | 0 | blocks skipped because they were already at the playhead |
| `drwfail` | 0 | failed writes — the pass cannot keep up |
| `drms` | well under 15 ms | > 30 ms = a batch holds the player task long enough to be heard |
| `drbat` / `drtot` | average `drtot/drbat` small | the real stutter number: average time held per batch |
| `hpibusy` | may be large | large with low `drblk` = the deck's own DMA is refusing the port |

If the deck **drops off USB by itself** after the mute, stop: that is a crash,
not a stutter, and a different problem.

## 3. Send back

- the two logs (`docs/deck_logs/hotcue_probe_<date>.log`,
  `docs/deck_logs/stems_probe_<date>.log`);
- the pad→time table you heard (even if it is all correct);
- one sentence on the stem mute: fast? stutter? came back?

With those, the two fixes either ship as they are or the logs say which of
the five suspects (record ordering, jump source, read pacing, mask base,
port contention) is the real one — no guessing needed.
