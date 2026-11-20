# Recording D–H so the deck calls them D–H (option B)

Status 2026-11-18: design only, nothing written into the module yet. The wait
is for one read-back from the deck (is a recorded "D" in the mod's table, or
did the deck record it as A?), because it decides which half of this matters.

## What the deck actually holds

Read from the emulator (`.venv/bin/python scratchpad/hc_ctx_cues.py`, snapshot
`$TMPDIR/hc_s0/hc_rb_s0_loaded.pkl`, a track with A–C at 5.0 / 13.0 / 21.0 s):

```
ctx = R32(CTX_TAB) = 0x0cb19ccc
ctx+0x0FC0  PCOB header   tag "PCOB", +0x04 0x18, +0x08 0x00c0 (size),
                          +0x12 count u16 = 3, groups at +0x20
            group        {cnt 3, ptr 0x0cd1bed0}
            entry 0      0x0cd1bed0  "PCPT"  cue 1 (A)   5000 ms
            entry 1      0x0cd1bf10  "PCPT"  cue 2 (B)  13000 ms
            entry 2      0x0cd1bf50  "PCPT"  cue 3 (C)  21000 ms
ctx+0x13B8  PCO2 header   here: not present (this track has no .EXT list)
```

Verified facts:

- Entries are a **separate heap allocation** (`ptr` in the group), not inside
  the ctx buffer.
- Stride is **0x40** (the entry's own length field says 0x38, the section size
  0xC0 = 0x18 + 3·0x38 — so the last entry ends 8 bytes before the next
  stride step).
- Cue number: u16 at entry+0x0E, value 1..8 (letters 'A'..'H' also occur).
- Time: `PCPT` (legacy PCOB) at entry+0x20, `PCP2` (.EXT PCO2) at
  entry+0x14; loop word 4 bytes later. This is exactly what `cue_e_ms()` /
  `cue_e_loop()` in `tools/hotcue8/hc.c` already read.
- There is **no slack** in the allocation: size = header + count·0x38.

## What that means for recording

Two different jobs, only one of them is safe:

**B1 — a cue that already exists in the track's list (D–H from rekordbox).**
Write the new time into the entry the deck itself uses (`PCPT` +0x20 /
`PCP2` +0x14). No allocation, no count change; the deck's own list, marker
and letter then agree with the mod, and page 1 is untouched because
`p1_restore()` puts the A–C slot bytes back anyway. This is the whole fix
for "I recorded a D and it shows as A" **when the D already existed**.

**B2 — a cue the track never had.** The list would have to grow: a free
entry inside the allocation (there is none), or a call into the deck's
allocator to re-create the group array, plus a count bump in two places
(group and header) and the section size at +0x08. That is the deck's own
parser data structure; guessing at it while the deck is playing that very
track is how you get a crash or a silent "no audio". Not doing this blind,
and not doing it without a deck to try it on.

So the rule for the module would be:

- cue exists in the export → write its time into the ctx entry *and* keep it
  in the mod's RAM table (both agree; survives paging; the deck's own view
  shows D).
- cue does not exist → do **not** write a slot that page 1 owns; either the
  cue simply cannot be recorded there (say so on the pad), or B2 becomes its
  own piece of work with its own risk note.

## The gate this needs

Extend `tools/hotcue8_emu_test.py` (or the hcpad test) so it: loads a track
with D–H in its list, arms REC, presses the D pad at a known position, then
reads the ctx entry for cue 4 and asserts it holds that position — and reads
slot 0 (A) and asserts it still holds A's own time. Two assertions, no deck:
they are exactly the two halves of "it still creates it as A".

## Open question for the deck (tomorrow)

`scratchpad/hc_rec_state.py` after recording a D, **before switching off**:
`hc_ram_set/hc_ram_ms` says cue 4 with the played position → the mod's store
is correct and this is B1+B2; the three pad slots holding the same position
→ the page-1 slot is picking it up and `p1_restore()` is not doing its job
on hardware.
