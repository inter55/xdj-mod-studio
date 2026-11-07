# TOUCH FX: what the five inert effects need (survey, 2026-11-03)

Status: static survey (2026-11-03) plus a **measured DSP memory map run in
the emulator on 2026-11-05** (`tools/dsp_map.py`, see "Measured"). No deck
has been available, so one read on hardware is still outstanding (last
section). Facts below are from the repo's own build files and from
`cache/blk1.bin` (the user's own 1.15 firmware, unpacked).

## Where our DSP code lives today

| what | value | source |
| --- | --- | --- |
| our DSP code + state window | `0xC005F4C0 .. 0xC0060000` | `tools/keyshift_dsp/ksr.c` ("the last chunk's loader padding") |
| size of that window | **0xB40 = 2,880 B** | subtraction |
| reserved for FX state (`struct fx`) | `0x180` = 384 B | `tools/fx_dsp/fx.ld` (. += 0x180) |
| built FX module today | **1,600 B** (`build/fx/fx.bin`) | ls |
| what starts right after the window | the key-shift runtime cos table at `0xC0060000` | `tools/keyshift_dsp/ksh.c` (LAND_END) |
| the SH-side one-shot installer | 3,376 B (`build/fx/ksh.bin`) — main CPU, not this window | ls |

So every DSP effect shares **2.8 KB**, and Live Key Shift and TOUCH FX
already take turns using it (they hook the same stretch path). Filter,
Crush and Gate fit because they are per-sample maths with one block of
state. Anything with a delay line does not.

## What each remaining effect needs

Rates: 44.1 kHz, stereo, float working buffers in the C674x (4 B a sample).
One second of stereo audio in float = **352,800 B**. The window is 2,880 B,
i.e. **8.2 ms** of stereo float audio.

| effect | buffer it needs | bytes | fits in 2.8 KB? |
| --- | --- | --- | --- |
| **Pitch** | none of its own — it *is* the key-shift stretcher already in the window | 0 | **no, not yet**: the code is **416 B** over the wall (measured 2026-11-07) |
| **Flanger** | 5–25 ms delay line, both channels | 1,102 frames x 2 x 4 = **8.8 KB** | no (3x the window) |
| **Echo** | 1/4 beat at 120 BPM = 125 ms; 250 ms is the useful setting | **44 KB … 88 KB** | no (15–30x) |
| **Roll** | one loop of up to 1 beat (~0.25 s) | **44 KB** | no — or **0 B** by driving the deck's own loop engine instead |
| **Reverb** | 4 combs + 2 all-pass, 25–40 ms each | **50–65 KB** | no (20x) |

Reading that table is the whole survey: four of the five are buffer jobs,
and Pitch needs no buffer of its own — it drives the same stretcher as
`tools/keyshift_dsp/ks.c`, the engine TOUCH FX replaces at install time. It
is not free, though: it was written on 2026-11-07 and the *code* does not
fit next to Filter, Crush and Gate (see "Pitch, measured" below). "Needs no
memory" was true of buffers and false of bytes.

## Measured 2026-11-05: the 103 KB guess is dead, and the first replacement was wrong too

The earlier guess in this document said `0xC0045900 .. 0xC005F4C0` (~101 KB)
was free. Two measured runs killed every guess in this section, and the
second one matters more than the first.

**Run 1 (`tools/dsp_map.py`)** loads a snapshot of the real DSP, lets it
play, reads DDR through the chip's memory port and reports non-zero pages and
zero runs. Three snapshots agreed byte for byte and the "gap" was full:

| snapshot | non-zero DDR | "gap" in use | zero run found |
| --- | --- | --- | --- |
| `emu_dsp_playing7.pkl`, 1 s play | 363,202 B | 97,308 B | 0xC0062000 (66,562 B) |
| `emu_dsp_playing7.pkl`, 2 s play | 363,202 B | 97,308 B | 0xC0062000 (66,562 B) |
| `emu_dsp_playing.pkl` | 363,202 B | 97,308 B | 0xC0062000 (66,562 B) |
| `emu_ks_diag_ok.pkl` (key shift) | 362,767 B | 97,308 B | 0xC0062000 (66,562 B) |

* **`0xC0045900 .. 0xC005F4C0` is NOT free.** 97,308 of its 105,408 bytes
  are non-zero, right up to `0xC005F4A6`. The words at `0xC0045900` are a
  table of `0xC0...` handler addresses — the 0x4D70 block is a dispatch /
  pointer table, not a load-address list, which is why the estimate was
  wrong.
* **Run 1's 66,562 B answer at `0xC0062000` was also wrong.** That run did
  not turn MASTER TEMPO on, so the audio path that fills the block was not
  running. With the FX engine installed, MT on, a track playing and the
  effect switched OFF — so our code writes nothing — the block fills with
  the audio path's own samples: 2,059 non-zero words in its first 16 KB,
  swapped every block, unchanged over 1.3 s. **It is in use.**

**Run 2 (the Echo build's own gate)** read three candidate blocks with the
effect off and MT on: `0xC0062000` used (2,059 words), `0xC0073400` and
`0xC0078000` both byte-zero through 1.3 s of playback.

So the usable home is `0xC0073400 .. 0xC0080000` = **52,224 B** (51.0 KB).
Against the needs table: **Flanger (8.8 KB) fits comfortably, Echo fits at
147 ms maximum (6,500 stereo frames, 52,000 B), Roll (44 KB) fits, Reverb
(50-65 KB) does not.** A 250 ms Echo would need 88,200 B and there is no
confirmed block that size.

What the emulator cannot settle: this is the chip's memory as the emulator
models it, on the MT path only. A deck must confirm that
`0xC0073400 .. 0xC0080000` reads back as zero **on hardware, with a track
playing and MASTER TEMPO on**. That is one read, not a session.

## Next step (read-only, one deck session)

1. ~~Parse the 0x4D70 records~~ done: a pointer table, not load addresses;
   `tools/dsp_map.py` replaced the estimate with a measurement.
2. On the deck, read `0xC0073400 .. 0xC0080000` through the DSP address
   window (address port 0x18040000 / data port 0x180C0000, P2 views
   0xB8040000 / 0xB80C0000 — STATUS.md) with a track playing, MT on, and
   check it is zero (and still zero ~30 s later).
3. Echo is already designed into that range and passes the real-DSP gate
   (`tools/fx_dsp_emu.py`, see below); the deck read only confirms the
   memory before anyone flashes it.

## Echo, built 2026-11-05 (emulator only)

`tools/fx_dsp/fx.c` effect 3 is Echo: a stereo float ring at `0xC0073400`,
X = delay 20..147 ms, Y = feedback 0..0.75, DEPTH = dry/wet. The delay index
is plain pointer arithmetic (no `%`: the C6x compiler turned the modulo into
a `rcpsp` reciprocal and the emulator produced garbage indices from it).
`EFF=echo X=40 Y=120 DEPTH=255 tools/fx_dsp_emu.py` on the real DSP program:

```
ECHO X=40 Y=120 D=255: delay 1763 frames (40 ms), best lag 1763 (40 ms)
corr 0.92, 2nd repeat 0.43 (feedback 0.35) -> RESULT: PASS
```

Play position was identical to the control run (317 -> 317 blocks, x1.000),
so Echo changes the sound and not the timing. Not yet on a deck.

## Flanger, built 2026-11-05 (emulator only, no new memory)

Effect 4 reuses Echo's ring: base 3 ms swept by a triangle LFO over 0..2 ms
(X = rate 0.05..4 Hz, Y = depth, DEPTH = dry/wet, feedback fixed 0.45, read
with linear interpolation so a moving tap does not zip). The two effects
share one delay routine because the engine has a hard size limit.

```
FLANGER X=60 Y=200 D=255: best lag per quarter [174, 193, 176, 152]
(window 132..220), sweep 41 frames, corr 0.86..0.89 -> RESULT: PASS
```

That is the whole flanger behaviour in one line: the delayed copy stays
inside the swept window and its lag moves between quarter-second windows
(the sweep), while play position stays at 317 -> 317 blocks.

## Fallback built 2026-11-06: a short Echo for the one empty page

If the marker test says the 52 KB block belongs to the chip, the only other
home seen is the single verified-empty page, `0xC006C000..0xC0070000`
= 4,096 stereo frames = **92.9 ms**. The effects themselves do not change;
only the ring's address and length do, and both are build-time values now
(`fx.c`: `ECHO_BASE`, `ECHO_FRAMES`), so the shipping build and the fallback
build come from the same source:

```
FX_OUT=build/fx_short FX_RING_BASE=0xC0074000u FX_RING_FRAMES=4096u \
    sh tools/fx_dsp/build.sh
```

`build.sh` only refreshes `tools/fx_dsp/fx.bin` (the shipping copy) when it
builds the default output, so a fallback build cannot silently become the
shipped one.

Emulator gates -- real DSP program, real audio path, ring at `0xC0074000`
(empty in the emulator's picture):

| build | setting | measured | result |
| --- | --- | --- | --- |
| shipping (6,500) | Echo X=40 | lag 1763 (40 ms), corr 0.92, repeat 0.43 | PASS |
| short (4,096) | Echo X=40 | lag 1386 (31 ms), corr 0.91, repeat 0.28 | PASS |
| short (4,096) | Echo X=255 | lag 4096 (93 ms), corr 0.89, repeat 0.38 | PASS |
| short (4,096) | Flanger X=60 Y=200 | lags 164/192/193/171, corr 0.85..0.88 | PASS |

Play position stayed 238 -> 238 blocks (x1.000) in every run, so the short
ring changes the sound and not the timing.

Two honest consequences:

* **The knob rescales.** With a 4,096-frame ring, X=40 gives 31 ms rather than
  40 ms. If the fallback ships, the X curve should be remapped so the useful
  settings land where they do today.
* **This cannot be gated at `0xC006C000` in the emulator.** That page carries
  live DSP data in the emulator's picture (2,865 of its 4,096 words non-zero),
  so a ring built there wrecked the audio path: output went to full scale
  (rms 32768/32768), correlation 0.00, and the ring read back `7fff0001`-style
  words. That is the concrete cost of a delay line on a page that is not
  actually free -- and the reason the deck's word-by-word proof must come
  before any ring goes at `0xC006C000`, never after.

## The deck check itself (built 2026-11-05, emulator-proven)

A deck session lasts 5-10 s, so the check is ONE shot: `tools/fxmem_probe.py`
writes a 488-byte cave, arms one word, and the DECK walks the block through
the chip's own host port and leaves a summary (non-zero count, first/last
non-zero, per-8 KB counts). The host reads 56 bytes at the end -- no 52 KB
transfer -- so the whole thing is about 2-3 s.

It writes nothing to the DSP (address port, then a read of the data port,
that is all), puts the flush pool back by itself before returning, and
`--revert` clears the cave. Fail-stop before any write: the hook must be
stock and the cave area zero or already ours.

Emulator proof (`tools/fxmem_emu.py`, real SH firmware + real DSP):

```
stock:    cave says nz=0,      direct scan nz=0      -> FREE
Echo on:  cave says nz=13000,  direct scan nz=13000  -> IN USE
          per-8K [2048,2048,2048,2048,2048,2048,712,0]
```

So the routine sees the delay line when it is there and reports an empty
block when it is not -- it is not just returning zeros.

## Deck result (2026-11-05): the block is NOT free on hardware

Ran `tools/fxmem_probe.py` on the XDJ-700 (fw 1.15), read-only, 7.8 s:

```
landmark program  000ED02A 0008C06A 00000362 004003E2   same code as the emulated DSP
landmark cos      3F800000 00000000 00000000 3F800000   exactly 1.0f
block             settled 13056/13056 words non-zero, hammered 13056
memory map        127 of 128 pages of 0xC0000000..+2 MB hold data;
                  only empty page 0xC006C000..0xC0070000
samples           D02DF41E F00FF00F D02DFC1E D12DF41E D12DF41E F00FF00F D02DFC1E C12DFC1E
```

The two landmarks prove the host port is reading real, correctly addressed
memory (program start matches our DSP image word for word; the cosine table
reads exactly 1.0f). So the block's content has to be believed: the planned
delay-line area is occupied on a real deck.

The emulator's zeros there were an artefact of its picture of DDR, not of the
hardware. The loader's image ends at `0xC005F4C0` (`IMG_LEN` 0x5F4C0), and in
the emulator everything above that is unpopulated; the deck's chip holds data
across almost the whole 2 MB window measured. Fast and settled reads agree on
hardware, so this is not a port-timing effect either.

Consequences:

* Echo/Flanger must NOT be installed at `0xC0073400` on this deck.
* The 2,880 B engine-code window at `0xC005F4C0` is unaffected: it is loader
  padding immediately after the image, and key shift has already written and
  read back code there on real hardware.
* A delay line needs a verified-free buffer. The only empty page seen is
  16 KB (4,096 frames, about 93 ms at 44.1 kHz), and that is one sample-based
  measurement (32 samples per 16 KB), not a proof of emptiness.
  `tools/fxmem_probe.py --page 0xC006C000` re-reads that page word by word
  (all 4,096) and reports PROVEN EMPTY or the data it found, so the next
  deck visit can settle it in the same one-shot run.

Deck-safety note: one run hit a USB timeout while the hook word was being
written as four separate bytes, which left the display-flush hook half updated
and the deck needed a power cycle. Nothing is flash-backed, so a reboot
restores stock. `tools/fxmem_probe.py` now writes every control word with one
atomic `LW` command, reads it back, and puts stock back if the arm does not
take.

## Marker test: live code, or leftovers? (built 2026-11-06, not yet run)

Full of data does not say whether the chip is *using* the block. `tools/fxmem_marker.py`
settles it with four words:

* writes a denormal float (`0x00000001`..`7`, ~1.4e-45) at four points spread
  through `0xC0073400 .. 0xC0080000`. That value is deliberate: if the block is
  a live audio buffer, one near-silent sample replaces one sample (a soft tick
  at worst). A loud-looking marker would be a full-scale click into the
  output -- never use one here;
* reads the originals first, checks the landmarks, then reads the markers back
  at 0.15 / 0.6 / 1.5 s. All four unchanged = the chip did not rewrite them;
  any overwritten or refused = the block belongs to the chip;
* puts the originals back and verifies that, even on Ctrl-C or an error --
  each write is one atomic `LW` with a read-back;
* with no `--write` it only looks: originals plus a neighbour-churn check that
  says whether the chip is writing there right now.

That leaves one thing a script cannot check: whether the deck still sounds
right. So a PASS is `RESULT: NOT BEING WRITTEN` plus a listening check for
clicks or drop-outs during the run. A stayed marker makes the block a
*candidate*, not proven free -- the chip could still use it only when a
particular effect is selected.

**The engine window is the real constraint, and it is 2,496 B.** Code plus
state must end by `0xC0060000`, where the key-shift cos table starts. A first
Flanger build was 2,624 B, so the state area crossed the table and the audio
path stopped after 44 blocks -- a silent, confusing failure that only showed
up because the gate compares play position with the control run.
`tools/fx_dsp/build.sh` now refuses anything over 2,496 B, and the FX
installer zeroes 0x180 bytes of state (the struct is 0x148) instead of the
stock 0x260; key shift keeps 0x260. Any further effect has to earn its bytes:
Pitch needs none (it is the existing stretcher), Roll needs the loop engine,
Reverb does not fit at all.

## What can be built with no new memory at all

**Pitch**, by routing the Touch FX "Pitch" button to the existing key-shift
stretcher (semitone + mix from pad X/Y), removing it from the "COMING SOON"
set and leaving four effects rather than five on the list. That is code
already proven on the deck (live key shift works), so it needs the UI wiring
and an emulator A/B, not a memory survey.

## 2026-11-06: the empty page is empty only while the deck is IDLE

The 2026-11-03 survey ran with no track loaded, and found one empty page,
`0xC006C000..0xC0070000`. That is an idle reading, and idle readings do not
predict playback: the audio path fills DSP memory once it starts.

Measured in the emulator with the real DSP core (tools/fx_dsp_emu.py, echo,
2,048-frame ring built at `0xC006C000`, MASTER TEMPO on, playing):

    SANITY: rms L/R ctl 8841/8934 fx 32768/32768 ... -> ok (tempo kept)
    ECHO X=255 ... best lag 6026, corr 0.00 -> RESULT: FAIL

Full-scale output, no correlation with the dry run: in the emulator that
page is written by the audio path itself from the first block, so a ring
there replaces live samples. The earlier note "0xC006C000 is live data
there (2,865/4,096 words)" is the same fact seen from the other side.

Consequences, now enforced in the tools:

* `tools/fxmem_probe.py --ring` REFUSES to certify the ring unless a track
  is loaded, because an idle-empty page is not evidence. Run it playing.
* `tools/keyshift_hot_apply.py --engine fx` refuses to install without that
  certificate (`build/fx/fxring_ok.txt`), so Echo/Flanger cannot write an
  unproven block. See tools/fxring_gate_test.py.
* The ring must fit the page that was actually proved empty: 4,096 words =
  2,048 stereo float frames = 46 ms at 44.1 kHz. A 4,096-frame (93 ms) ring
  spans two pages and the second one was occupied.

Nothing here proves `0xC0073400` is safe either: it read 13,056/13,056 words
non-zero on the deck, and the emulator only ever saw it zero while idle.
Echo/Flanger remain unshippable until a playback-time survey finds a block
that reads zero both while playing and after a write marker, which needs one
deliberate deck run (tools/fxmem_marker.py --write, denormal markers).

Next: `docs/DECK_SESSION_TOUCHFX.md` — the 2–3 minute session that settles it
(survey with a track playing, then either install and listen, or one denormal
marker write).

## Decision (2026-11-06, deck session): ship three effects, no ring

Echo and Flanger are switched off in the shipped engine (`FX_LIVE(i) ((i) < 3)`
in `tools/touchfx/tfx.c`), and the engine itself is built ring-free: `FX_NO_RING=1`
compiles out both the delay-line zero-fill (`fx.c`, the first-pull init) and the
Echo/Flanger branch. `tools/fx_dsp/build_fxh.sh` propagates the flag, so the
installer cave carries the same engine (`carries fx.bin (1664 B) at +0x6f0`).

Why the whole engine had to change rather than just hiding two buttons: the
zero-fill ran on the engine's **first audio pull, whatever effect was selected**,
so "only Echo touches the ring" was never true.

What the deck said tonight (`tools/fxmem_probe.py --ring`, track playing, MT on,
read-only):

* the ring page read **4,096 of 4,096 words non-zero**;
* the same run mapped `0xC0000000..+2 MB`: **128 of 128 pages hold data**, so
  there is no empty page in that window to fall back to;
* landmarks (program start, cos table = 1.0f) confirm the port read real memory.

Checks on the ring-free build:

| check | result |
| --- | --- |
| `0xC0073400` in the disassembly | **0** fragments (ring build: 2) |
| engine size vs the 2,496 B window | 1,664 B |
| `build/fx/ring.txt` | `0x0 0` -> `ring_gate()` allows, nothing to prove |
| `tools/fxring_gate_test.py` | PASS, including the new no-ring case |
| emulator, real DSP, MT on | Filter hi-band 0.0239 -> 0.0000 PASS; Crush corr 0.969 vs model PASS; Gate envelope 0.22..1.00 at 2.84 Hz PASS; play position 317 -> 317 blocks |

Echo and Flanger stay unshippable until a playing-time survey finds a block that
reads zero with audio running **and** survives the denormal marker test
(`tools/fxmem_marker.py --write`). Building them again is one flag:
a ring build, once that block exists (the live count is now `FX_LIVE(i)` in
`tools/touchfx/tfx.c`, not a `-D`).

## Pitch, measured (2026-11-07): written, and 416 B too big

Pitch was written in full. Pad X is semitones (±11). It stages the stretcher's
input and resamples it, so the deck's own time-stretcher runs at `speed / k`
while the play position it reports stays the original frames — that is the
reason to build it this way at all: tempo and waveform do not move, only the
pitch does.

It builds. It does not fit. Same toolchain, same linker script as the engine
that ships:

| build | bytes | wall |
| --- | --- | --- |
| shipping, `FX_NO_RING=1` (Filter/Crush/Gate) | 1,696 | 2,496 |
| + Pitch, `FX_PITCH=1 FX_NO_RING=1` | **2,912** | 2,496 — refused by `build.sh` |
| + Pitch with the delay line too | 3,744 | refused |

Three compaction passes took it 4,320 → 3,520 → 2,912 B: linear interpolation
instead of cubic, a polynomial 2^(semi/12) instead of a table, `premix` kept
out of line, the duplicated pass-through folded into one `goto stock`, and the
ring code moved under `#ifdef` so it is absent rather than tested (that last
one saved nothing — the compiler had already dropped it). **2,912 − 2,496 =
416 B** still to find, which is more than the whole Filter effect costs. No
honest shave that size was left without dropping a shipped effect.

So Pitch ships **off**. `FX_LIVE(i)` is `(i) < 3`, the pad still reads COMING
SOON, the installable engine is unchanged, and the code sits behind
`-DFX_PITCH` so the number above is reproducible:

```
FX_PITCH=1 FX_NO_RING=1 sh tools/fx_dsp/build.sh
# REFUSED: fx.bin 2912 B > 2496 B: code + 0x180 state would reach past 0xC0060000
```

What would unblock it, in order of honesty: (a) free ~416 B of code inside
the window — the candidates are Gate's envelope and Crush's level ladder, and
neither is a whole answer alone; (b) prove a second small code block free
with audio playing (the echo-ring job, just 2 KB instead of 52); (c) a DSP
home outside the window. "Pitch is free because it needs no buffer" was the
wrong lesson: the buffer was never the cost, the code room is.

## Wider than 2 MB: four passes of the same cave (Nov 6, late)

The 2 MB map answered "128/128 pages hold data" for 0xC0000000..+2 MB. Two
attempts to see further failed, and both are worth recording.

* A coarser map built into the cave (128 entries x 64 KB = 8 MB, page size
  behind `FXMEM_PAGE_SHIFT`) assembles to exactly 1,792 B -- the cave's cap --
  and the emulator never completed it: `done=0` after 400M steps with nothing
  restored, against 39M steps for the 16 KB build. Reverted rather than spend a
  boot on it. The shipping cave is byte-identical to the proven one, 1,784 B.
* A sweep from the laptop is impossible. The deck's shell reads its own RAM
  fine (a loaded track reads 0x0000EB52 at 0x0BA3C050), but every chip address
  comes back as stable junk -- 0xC0000000 = A0050009, 0xC0060000 = 91C7EAA3,
  0xC0001000 = FFFFFFFF -- with the deck idle and with a track playing. Only
  the chip can read chip memory, which is exactly why this cave exists. It also
  means the emulator is not a witness here: its memory object reports zero for
  that whole window even where the deck sees program words.

So the widening is the proven cave run four times, each with a different
build-time map base (`FXMEM_BASE`): 0xC0000000, 0xC0200000, 0xC0400000,
0xC0600000 -- 8 MB at 16 KB pages, zero extra cave bytes. `tools/fxmem_wide.py`
drives the four passes and stitches the maps.

Each pass needs the display-flush hook stock, so power-cycle before a wide run;
the cave puts the hook back at the end of every pass. Landmarks are still read
at their fixed addresses on the first pass, so "the chip is answering" is
checked before any of the four maps is believed. A single empty 16 KB page is
still only a candidate: `--page <addr>` proves it word by word.

### Result: the wide run (Nov 6, deck, track playing)

Four passes, stock boot, track playing, landmarks good on the first pass
(program `000ED02A 0008C06A 00000362 004003E2`, cos word `3F800000`), every pass
finished and put the hook back:

    0xC0000000..+2 MB   128/128 pages hold data   [6.7s]
    0xC0200000..+2 MB   128/128 pages hold data   [6.6s]
    0xC0400000..+2 MB   128/128 pages hold data   [6.6s]
    0xC0600000..+2 MB   128/128 pages hold data   [6.6s]
    whole span 0xC0000000..0xC0800000: 512/512 pages hold data

**There is no empty 16 KB page anywhere in the first 8 MB.** So the 2 MB answer
was not an artefact of where the first map stopped: the chip's memory is in use
across the whole span surveyed, not just the part the original map happened to
cover. This is now a proof rather than a question:

* Echo and Flanger cannot ship by *finding* a spare 52 KB delay line. There is
  none in the surveyed span, and there is no evidence of more span to look at:
  the window the engine itself lives in (`0xC0000000`) is the audio chip's own
  memory, and everything reachable from it that we mapped is occupied.
* The next honest route is making room, not finding room: a shorter delay
  line (a flanger needs far fewer frames than an echo), or sharing the window
  with the key-shift engine (`0xC005F4C0..0xC0060000`, 2,880 B) -- which today
  cannot coexist with the 1,664 B ring-free FX engine.
* Reverb needs a 52 KB-class delay line too, so it is blocked for the same
  reason and should be described the same way.

Nothing in this run wrote to the DSP: four cave passes, each with the display
hook put back, and the deck is stock afterwards.
