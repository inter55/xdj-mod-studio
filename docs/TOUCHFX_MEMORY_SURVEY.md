# TOUCH FX: what the five inert effects need (survey, 2026-11-03)

Status: **static survey only.** No deck was available, so nothing here is a
measurement of the deck's DSP memory; the deck-side read-only pass is still
to do (last section). Facts below are from the repo's own build files and
from `cache/blk1.bin` (the user's own 1.15 firmware, unpacked).

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
| **Pitch** | none of its own — it *is* the key-shift stretcher already in the window | 0 | **yes, today** |
| **Flanger** | 5–25 ms delay line, both channels | 1,102 frames x 2 x 4 = **8.8 KB** | no (3x the window) |
| **Echo** | 1/4 beat at 120 BPM = 125 ms; 250 ms is the useful setting | **44 KB … 88 KB** | no (15–30x) |
| **Roll** | one loop of up to 1 beat (~0.25 s) | **44 KB** | no — or **0 B** by driving the deck's own loop engine instead |
| **Reverb** | 4 combs + 2 all-pass, 25–40 ms each | **50–65 KB** | no (20x) |

Reading that table is the whole survey: four of the five are memory jobs,
and Pitch is not a memory job at all — it is already implemented, just not
wired to a Touch FX button (`tools/keyshift_dsp/ks.c`, the engine TOUCH FX
replaces at install time).

## The room that might exist: ~103 KB above the DSP program

`cache/blk1.bin` 0x00004D70 holds the DSP download list. The visible records
are headed by a length word (0x3C, then 0x30) followed by addresses; every
load address seen is in **0xC0010000 .. 0xC0045900**. If that list is the
complete DSP program, the image ends near `0xC00458E0` and

```
0xC0045900 .. 0xC005F4C0 = 0x19BC0 = 103,360 B = ~101 KB
```

is unused space sitting immediately below our own window — enough for Echo
or Reverb, and 10x what a Flanger needs.

**This is a lead, not a fact.** Two things are unproven: (1) the record
lengths in that table have not been parsed, so the true end of the image is
unknown, and (2) something else may live in that range at runtime (the
key-shift cos table already sits above our window, so the region is used
for data by design).

## Next step (read-only, one deck session)

1. Parse the 0x4D70 records (length word + load address pairs) and print the
   occupied ranges; that turns the 103 KB gap into a measured figure or
   kills it.
2. On the deck, read `0xC0045900 .. 0xC005F4C0` through the DSP address
   window (address port 0x18040000 / data port 0x180C0000, P2 views
   0xB8040000 / 0xB80C0000 — STATUS.md) and confirm what is there: all
   `FF`/zero means free, anything else means occupied.
3. Only then design Echo/Flanger/Roll/Reverb into the proven range, one
   effect at a time, each A/B'd on the audio output in the real-DSP
   emulator before any deck test (the same gate Filter/Crush/Gate passed).

## What can be built with no new memory at all

**Pitch**, by routing the Touch FX "Pitch" button to the existing key-shift
stretcher (semitone + mix from pad X/Y), removing it from the "COMING SOON"
set and leaving four effects rather than five on the list. That is code
already proven on the deck (live key shift works), so it needs the UI wiring
and an emulator A/B, not a memory survey.
