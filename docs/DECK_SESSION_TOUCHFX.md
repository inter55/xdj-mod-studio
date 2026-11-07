# Deck session: prove (or kill) the Echo delay line — 1.1

Purpose: settle whether a 46 ms ring can live anywhere in the DSP's memory,
which is the only thing stopping Echo and Flanger from shipping. Two short
runs, both read-only until the very last step, then one deliberate marker
write. Budget: **2–3 minutes of deck time**, done in 2 sittings if that is
easier (each step is its own USB session).

The deck must be on firmware 1.15, one USB-B cable, no rekordbox / Serato /
DJ app running on the Mac. Every step prints `RESULT:` — read that line
before moving on.

---

## Step 0 — before you touch the deck (30 s, no deck)

    .venv/bin/python tools/fxring_gate_test.py          # expect RESULT: PASS
    cat build/fx/ring.txt                               # expect 0xC0073400 6500

If `ring.txt` is missing, rebuild the engine first: `sh tools/fx_dsp/build.sh`.

Current state: the shipped engine keeps its echo in the 52 KB block at
`0xC0073400`, and the deck survey found **all 13,056 words there non-zero**.
The install gate therefore refuses, by design. That is what this session
changes — either by finding a block that is genuinely free, or by proving
none is.

## Step 1 — survey WITH A TRACK PLAYING (about 1 min)

Why playing: the 2026-11-03 survey ran at idle and found one empty page. The
emulator then showed that page is written by the audio path from the first
block while playing, so an idle reading proves nothing. This run refuses to
certify an idle deck.

1. Put your usual rekordbox stick in, load any track, press play.
2. Leave it playing. Then, on the Mac:

       .venv/bin/python tools/fxmem_probe.py --ring

Expected, and what it means:

| Output | Meaning | Next |
| --- | --- | --- |
| `RESULT: PASS` + `wrote .../fxring_ok.txt` | every word of the built ring read zero **while playing** | Step 2 |
| `RESULT: IN USE` | the block belongs to the audio path | Step 3 |
| `REFUSED: load and play a track first` | nothing was loaded/playing | load a track and repeat |
| `STOP: the display-flush hook holds ...` | an earlier mod is still armed | power-cycle, repeat |

The run takes about 6 s of shell and writes only the finder cave in RAM plus
its own summary words; a power cycle always restores stock.

It also prints one line beginning `block content:` — how many words repeat
word 0, how many are floats below 1.0, and eight sample words. That answers
the question the old count left open:

| `block content:` line | Reading |
| --- | --- |
| `13056 of 13056 words repeat word 0` | one value everywhere: empty or unmapped space echoing a filler word, not audio |
| a tiny repeat count + all words floats | real samples: the block is carrying audio |
| anything in between | mixed; treat as in use |

For scale, the emulator with Echo running reads `1 of 13056 repeat, 13056 of
13056 floats below 1.0`, and the emulator's empty block reads `13056 of 13056
repeat`. It is a reading, not a proof — only step 3's marker test settles it.

## Step 2 — only if Step 1 passed: install and listen (about 1 min)

The certificate is good for **this boot only**. Do not power-cycle between
step 1 and here.

1. Eject the stick (the installer refuses with a track loaded).
2. `.venv/bin/python tools/keyshift_hot_apply.py --engine fx`
   Expect `ring: delay line ... proved empty` then `RESULT: PASS - TOUCH FX
   engine installed`.
3. `.venv/bin/python tools/touchfx_apply.py --survey` then
   `.venv/bin/python tools/touchfx_apply.py --engine-ok` for the screen.
4. Stick back in, load a track, **MASTER TEMPO ON**, PERFORM › TOUCH FX.
5. Pick Echo, drag X fully right, DEPTH fully up, then work X down.

Listen for: a repeat at a fixed interval, no clicks, no dropouts, tempo and
play position unchanged. Anything else is a fail — report what you heard and
power the deck off.

## Step 3 — if Step 1 said IN USE: the marker question (about 1 min)

This is the only step that deliberately writes into the block, and it writes
four denormal floats (`0x00000001`..`0x00000007`, ~1.4e-45) — one near-silent
sample each if the block turns out to be live audio. Never substitute a
normal-looking value here.

1. Deck freshly powered on (hook must be stock), a track playing.
2. `.venv/bin/python tools/fxmem_marker.py` — look only. It reports whether
   the chip is writing near the sampled words and puts nothing in.
3. Only if the user says go: `.venv/bin/python tools/fxmem_marker.py --write`.

| Output | Meaning |
| --- | --- |
| `RESULT: NOT BEING WRITTEN` | candidate block; still needs a listening check |
| `RESULT: IN USE` | markers were overwritten or refused — Echo cannot live here |
| `RESULT: UNCLEAR` | a word already held our marker; re-run with another set |

The tool restores every original word and verifies it, and refuses to write
unless the firmware landmarks match, the display hook is stock and its
`--self-test` passes.

---

## What each outcome buys us

- **Step 1 PASS** → a 46 ms Echo (and Flanger) can be shipped against a
  measured block, and `build/fx/ring.txt` gets that geometry.
- **Step 1 IN USE, Step 3 IN USE** → this deck's memory has no room for a
  delay line at 44.1 kHz. Then the honest 1.1 options are: Pitch only (no
  delay memory), or a shorter ring if a smaller block is ever measured free.
- **Step 1 IN USE, Step 3 NOT BEING WRITTEN** → the block is leftover, not
  live; the next session installs and listens.

## Do not

- Do not flash anything, build an update file or use `--write` on the marker
  without saying so first.
- Do not run step 2 after a power cycle: the certificate is for one boot.
- Do not trust an idle `0xC006C000` reading again; that mistake cost a week.
