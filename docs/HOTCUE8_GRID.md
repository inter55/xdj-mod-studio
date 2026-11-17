# 8 hot cues at once on the PERFORM screen (design + emulator prototype)

Status 2026-11-07: **integrated** into the real module (`tools/hotcue8/hc.c`
plus `hchook.S`): one cave, one flush hook, one touch hook, the view toggle in
entry-table slot +12, and every installer (deck flash, the app's emulator, the
baked firmware image) chains the touch pool. Gate: `tools/hotcue8_test.py`.
**Never run on the deck.**

Changes from the prototype below (decided during integration):
- **Tap** the HOT CUE tab toggles the 8-pad view; **swipe** still pages A-C /
  D-F / G-H, so the deck-side "set D-H on the deck" path (which needs pages
  2/3) keeps working. A swipe leaves the 8-pad view. Both views sit on page 1.
- `hc_armed` guards the view: if the touch hook is not installed (an older
  install, or a baked image built before this), a tap keeps its old meaning
  and pages instead of drawing pads whose presses would reach the stock zones.
- Empty pads are still **swallowed** in the 8-pad view rather than setting a
  cue: setting lives on pages 2/3, where REC is already blocked.
- The prototype's separate `0x09850000` cave and its own glyph copy are gone;
  the pads share `hc_glyphs.h` and the hot-cue state already in the module.

Files:
- `tools/hotcue8grid/hg.c`, `hghook.S`, `build.sh` — the C module (3.6 KB,
  0 FPU instructions, built with sh-elf-gcc, so the compiler places the
  delay slots).
- `tools/hotcue8grid_test.py [SNAPSHOT]` — emulator test (defaults to
  `$TMPDIR/hotcue8_loaded.pkl`).
- `screenshots/hotcue8grid/` — `row.png` (the full screen), `held_E.png`,
  `empty_GH.png`, `overview.png`.

Snapshot used: `tools/hotcue8_emu.py` mini stick (track with hot cues A-H at
5, 13 … 61 s, the user's library left untouched) + one PERFORM tap. Every
press scenario reloads it, because a stock hot-cue press stalls the emulated
panel link (CLAUDE.md §48).

## 1. What the stock firmware does (found today, emulator + disassembly)

The A/B/C pads are touch zones 23/24/25 in the zone table 0x080D5D38
(x 116/231/345, y 232, 99×63). Their handlers do one thing: set a panel KEY
bit.

| pad | zone handler | key bit |
|---|---|---|
| A | 0x08DD0BA4 | KEY+86 (0x0B6C7A7E) bit 0x40 |
| B | 0x08DD0BB4 | KEY+86 bit 0x80 |
| C | 0x08DD0BC4 | KEY+87 (0x0B6C7A7F) bit 0x01 |
| REC | 0x08DD0BD4 | KEY+86 bit 0x20 (also re-checks the finger is still inside) |
| CALL | 0x08DD0C18 | — |

(r6 = 0x0B6C7A28 at handler entry was confirmed by trace.) The handler runs
on every panel frame while the finger is down. The snapshotter 0x8D4A7A2
copies the key bitmap into 0x0B6C40B8. The hot-cue **consumer** at
0x8D44200.. compares snapshot bytes 34/35 with the previous frame (rising
edge) and tail-jumps into the **hot-cue handler 0x8D391E6** with
`r4 = slot index 1..3`. That handler reads the slot's press state from
0x8D38F8E (tables 0x0B6C3C64 / 0x0B6C3C74 + 4·n, plus 0x0B6C42EC). Those
tables hold the hot-cue press/play state machine (0 = idle, 1/2 = active,
5/6 = transitions; written by 0x8D3858C..0x8D38680), not "is the cue set".

This is the same mechanism the beat-jump pads use: hold a panel key bit
while the finger is down. That mechanism is already proven on the real deck.

## 2. The design

**Layout.** In HOT CUE mode with the 8-pad view on, the stock row
`A B C REC CALL` (y 232..295) is replaced by eight pads A-H. Each pad is
58 px wide with a 5 px gap (63 px apart), from x 116 to x 615. They use the
stock pad colours, a green top strip when the cue is set, and white
Helvetica Neue letters (the existing `hc_glyphs.h`). Empty pads are drawn
dark with grey letters. The held pad gets an orange rim.

**Which slot is borrowed.** Pad n uses live slot `k = (n-1) % 3`.
- A, B, C use their own slots. Nothing is written; only the key bit is held.
- D-H borrow a slot: D→0, E→1, F→2, G→0, H→1. This is the same slot that
  the existing pager (`tools/hotcue8/hc.c load_page`) would use for that cue.
  When the finger goes down, the slot's 64 bytes are saved and cue n is
  written in using hc.c's field layout (state 6, number, ms, loop, 1/150 s).

**How the press reaches the stock handler.** The module chains in front of
the touch hook (pool 0x08D5278C, in front of `perform_pads`' ptouch). On a
new press (stock hit-tester idle, ZSTATE 0x1E; pads latch free) on a set
pad, it takes the press:
- It sets the slot-k key bit on every down frame.
- It never calls the stock hit-tester for that press, so no zone handler
  runs and REC can't be hit by accident.
- On release it clears the bit. Then, 8 display flushes later, it writes the
  saved slot bytes back, so slot k holds its own cue (A/B/C) again.

The stock consumer and handler 0x8D391E6 then run unchanged. A new track
(preview hash change) drops any pending restore, because the firmware has
reloaded the slots itself.

**REC / CALL / delete.**
- REC and CALL are not on the 8-pad row. They stay reachable by switching
  the 8-pad view off (stock row). So REC mode is never entered from this
  view. Pressing a set pad in REC mode would overwrite that slot, which
  would save cue D-H as A-C on the stick.
- Empty pads and the gaps between pads are swallowed. They latch until
  release, so sliding the finger onto a set pad does not fire it. An empty
  stock pad press could record a cue into the borrowed slot.
- **Delete: cannot combine with a pad -- settled 2026-11-07 (emulator).**
  MEMORY-DELETE is **zone 17 (0x11)**, rect x 715 y 232 67x53, handler
  `0x08DD0B60`; tapping it sets **KEYB+84 bit 0x40** and nothing else that a
  pad reads (QUANTIZE next door still shows its documented KEYB+91 0x80).
  That bit is set by DELETE's own *zone handler*, not by a panel key -- holding
  the `"Delete"` frame bit leaves it at 0. And a panel frame carries **one
  touch point** (payload +22..+25: one X, one Y), so DELETE and a hot-cue pad
  cannot be pressed at the same time, on the emulator or on the deck. The
  "delete a borrowed slot" combination does not exist.
  `hc_touch` still reads the bit and swallows D-H presses while it is set, as
  defence in depth; that guard deliberately cannot be driven through the
  single-touch panel, so it is not claimed as emulator-tested. A-C presses are
  never blocked: DELETE + A deletes cue A, which is what the screen shows.

**Toggling the view.** In the prototype this is just a byte (`hg_view`,
entry +8 `hg_set_view`). For integration, the proposal is that the HOT CUE
tab swipe, which pages A-C/D-F/G-H today, switches between the "8 pads"
view and the "stock row with REC/CALL" view. The 8-pad view replaces
paging, so `HCPAGE` stays 0 and the hc.c letter overlay goes away. The
module already only acts when HCPAGE == 0, PMODE == 0 (HOT CUE), MODS ON
and MODEW == 7.

## 3. What the emulator proved (tools/hotcue8grid_test.py)

- 8 pads drawn on the PERFORM screen. Gaps and empty pads are swallowed,
  and a finger sliding from an empty pad onto a set one does not fire it.
- Pad E held: slot 1 = cue E (5, 37000 ms), KEY+86/0x80 up, stock
  hit-tester untouched, slots 0/2 untouched. On release the bit drops and
  slot 1 is restored byte-exact (cue B).
- Pad A: no slot write. Pad H, sitting where stock REC was: slot 1 = H, and
  the REC zone is never latched.
- View off: the stock A zone is reached as before.
- **Handler entry (py core trace):** 0x8D391E6 is entered with r4 = 2 and the
  slot holding cue 5 (pad E), r4 = 2 / cue 8 (H), r4 = 3 / cue 6 (F),
  r4 = 1 / cue 1 (A). That is the same entry a stock B press makes
  (r4 = 2 / cue 2), and no zone handler runs for our pads.
- The delay-slot scan of the build (no branch or PC-relative load in any
  slot, §119) is clean. A literal-pool word that decodes as `bra` is
  filtered out by its out-of-module target.

**Not proven (emulator limits):** the actual jump or audio. In this lineage
the handler's state check 0x8D38F8E returns 0 for the stock press too
(tables 0x0B6C3C64/74 are all zero; stand-in DSP), so neither stock nor ours
gets past the gate. The deck is the judge.

## 4. Deck questions / risks before any install

1. **Restore timing.** On a stopped deck, holding a hot cue plays from it
   and release returns to the cue. If the engine reads the slot **at
   release** or later, the 8-flush holdoff (~130 ms) must cover it. Check:
   hold E while stopped, release, and confirm it returns to E, not B.
2. **Delete/SHIFT key bits** (see above). Settle this before the first deck
   run.
3. **Track unload / save while borrowed.** If the deck writes hot cues back
   to the stick on unload (it does for recorded cues), a borrowed slot
   could be saved. The borrow window is only the press + holdoff. The track
   hash drops a stale restore. Recommendation: also refuse a D-H press while
   the deck is in a load/eject transition (mode word check).
4. **Cave address.** HG_BASE defaults to 0x09850000 (emulator only: the old
   VF base, believed free). The deck rule applies: survey it read-only in
   idle/loaded/playing before installing (§75/§77). Better: merge into the
   hotcue8 module at 0x09790000 during integration.
5. **Pad colours.** No per-cue colours yet (rekordbox colours live in
   PCO2/PCP2; the stock 700 row doesn't show them either).

## 5. Integration (done 2026-11-07)

- Touch logic moved into the hotcue8 module (one cave, one flush hook, one
  touch hook). `load_page`, `find_cue` and the page letters all stayed: paging
  is what lets D-H be set on the deck, so it was not retired.
- In perform_pads the HOT CUE tab **tap** now calls `hc_toggle` (entry +12);
  the swipe keeps calling `hc_next` / `hc_prev_page`. `phhc`'s REC block is
  unchanged and still guards pages 2/3.
- Installer order: code + zeroed bss, both chain words (`hc_prev`,
  `hc_tprev`) written from the live pools, then LW: armed flag, touch pool,
  flush pool last (a failing step unwinds what it already did). Revert unwinds
  touch before flush. The touch pool chains in FRONT of perform_pads' ptouch.
- Baked firmware: `permanent_build.bake_hotcue8` writes the same three words,
  so a permanent build has the view too.
- Gate: `tools/hotcue8_test.py` (8-pad view legs + the paging legs) and
  `tools/perform_pads.py emu`. `tools/hotcue8grid_test.py` is kept as the
  prototype's own record; it still builds the standalone unit.

## 6. 2026-11-08: the paged pads played A-C (wrong cue store)

Deck report: pages 2/3 showed D-H, but D played A, E played B, and so on;
the D+ pads showed a half-drawn loop icon and the waveform still had A-C.

Measurements (`build/hc_page_probe.py`, stock snapshot with cues A-H):

- A page rewrite of the three live player slots (0x0B6C4118) is **kept**:
  after a swipe the slots read state 6 / number 4-6 / D-F's ms, and they
  are still there 30M steps later. The deck does not revert them.
- The deck keeps every pad's cue **twice more**, in two 8-record models of
  its own, 0x38 bytes per record at `0x0BA28800` and `0x0BA2A52C`:
  `+0x00 u16 state (6)`, `+0x02 u16 number`, `+0x04 ms`, `+0x08 loop`,
  `+0x0C 1/150 s`. Both models held A/B/C through the whole run and never
  followed the slots.
- Cue times 29 s / 37 s / 45 s / 53 s / 61 s (D-H) appear **nowhere** in
  RAM after a stock load: only A-C are ever loaded, into the ctx entries,
  the slots and the two models.

So the pager was writing the one copy playback does not read. `load_page`
(and the 8-pad borrow) now write the two model records as well, and the
borrow path saves/restores them byte-exact. The gate asserts both models
follow page 2 and go back to A-C on page 1.

Also fixed in the same change: `letter()` repaints the whole pad face
instead of a 26x22 box, which had left the stock letter and loop badge
half visible on the paged pads.

**Still deck-only:** whether the recall reads the models or the ANLZ
entries. If D-H still play A-C after this, the next step is to hook the
press path (0x8D39940) and feed it the paged cue's ms.

## 7. 2026-11-08 second pass: what else a model record holds

`build/hc_model_probe.py` dumps all 8 records of both models byte by byte
on a stock load and again after a page. Measured:

- A record uses **only +0x00..+0x0D**; the tail +0x10..+0x37 is zero on
  every stock record, set or empty, so `model_set` need not touch it.
- A set record: state 6, number, ms, loop (-1 = no loop), pos =
  ms*3/20 truncated (5000 -> 0x2EE, 29000 -> 0x10FE), exactly what hc.c
  writes.
- An **empty** record is state 0 / number 0 / ms 0 / pos 0, but the loop
  differs per model: `0xFFFFFFFF` in `0x0BA28800`, `0` in `0x0BA2A52C`.
  `model_set` now takes the empty loop as a per-model argument instead of
  writing -1 into both.

Two logic defects found in the same pass and fixed:

1. `scan_pads` (setting a cue from an empty pad on page 2/3) wrote only the
   live slot, so a deck-set cue would draw but not recall until the page
   was reloaded. It writes both model records now; `hotcue8_set_test.py`
   asserts the models hold the new cue and still hold it after paging.
2. `hc_toggle` / `hc_next` / `hc_prev_page` restored a borrow only when
   `hc_restore` was pending, but a press that is still held has
   `hc_borrow` set with the countdown at 0 -- the borrowed bytes would then
   be left in the slot. All three check `hc_borrow` now (defensive: the
   single touch point makes the sequence hard to drive).

The standalone prototype `tools/hotcue8grid/hg.c` had the same
slot-only borrow and now writes/restores the model records too;
`tools/hotcue8grid_test.py` is runnable again (it defaults to the hotcue8
snapshot and opens PERFORM itself).

## 8. 2026-11-11: hardware (1.0.9, cable) -- D-H show but recall A-C

Deck report (Reddit, over the USB cable): stems work after ~30 s, the 8-pad
view shows all eight cues and D-H can be set, but pressing D/E/F/G/H plays
A/B/C. Unaffected by the HOT CUE GATE setting, which is expected: that
gate only arms the pause latch in the touch hook (`pgate`), never recall.
The mapping is per slot index (`k = (n-1) % 3`: D->A, E->B, F->C, G->A,
H->B), which is the pager's own borrow rule.

Firmware evidence (the user's own 1.15 image, `blk1.bin`, little-endian):

- `0x0BA28800` (MODEL_A) is referenced by **no instruction** in the whole
  image (0 literal refs); `0x0BA2A52C` (MODEL_B) has one. Writing the
  models can only affect what is drawn.
- The press path `0x8D39940..0x8D39C10` reads neither the live slots
  `0x0B6C4118`, nor the models, nor the ANLZ ctx pointer `0x09826BE4`.
  It works from `0x0B6C41E4`, the press-state tables `0x0B6C3C64/68/84`,
  and calls `0x8D379AC`, `0x8CFB37E`, `0x8D3BA02`, `0x8D4B0BC`,
  `0x8D2F402`, `0x8D2F9DE`.
- The slots are loaded by the gate function `0x8D38F8E` (its literal pool
  at `0x8D38EC0` holds `0x0B6C4118`), i.e. they answer "is this pad set?",
  not "where is this pad".

So the borrow writes the one copy recall does not consult: the position is
resolved by pad index from a store still unidentified. This closes the
question left open in section 6 ("whether the recall reads the models or
the ANLZ entries"): it reads neither.

Attempts and what they rule out:

- `tools/hotcue8_recall_probe.py` (new): boots the **with-mods** image in
  QEMU, loads the 8-cue mini stick, opens the 8-pad view and presses every
  pad, reading `0x0B6C0F10 + 0x14*k`. Measured twice:
  - playing, the words simply follow the track (764, 764, 766 -> 1214 ->
    1666 -> 2116, about 2 s per two pads), and pads B/C stay on A's value
    instead of moving to 1950/3150: they are playhead mirrors;
  - parked, the deck did not stay parked ("parked at 736 (playing: False)"
    then 736 -> 1186 -> 1636 -> 2090) and **no press produced a cue value
    at all** -- pad A read 736 (4.91 s), i.e. where the playhead was, not
    cue A's 750.
  So in the emulator's 8-pad view the press does not reach a seek; this is
  the same wall as the gate `0x8D38F8E` returning 0 for stock presses.
  **Recall cannot be verified in the emulator** through these words; the
  "CALL writes 0x2EE" seen by `hotcue_call_diff.py` was the same field read
  once with the deck at rest.
- The earlier `hcq_*.log` runs compared POS150 while the track played, so
  every pad looked WRONG regardless of what it did.

Next step (unchanged in direction, now with the right place to look): hook
the press path at `0x8D39940` and substitute the cue time the deck is about
to use, instead of hoping a borrowed slot is read. Because the emulator
does not perform the seek at all, the only honest verification is a build
tested on the deck over the cable -- one track, pads D-H, watch which cue
plays.

## 9. 2026-11-11: the press path itself, and the fix

Disassembly of `0x8D39940..0x8D39A40` (little-endian, `--adjust-vma=0x08000000`)
quotes the mechanism exactly:

    8d39974  r6 = @(28,r2)      r2 = 0x0B6C41E4 -> the pad number (+0x1C)
    8d39978  cmp/eq #23,r0      bf -> not our zone
    8d3997c  r2 = r14<<2 ; r6 = 0x0B6C3C84 + r2      per-pad table entry
    8d399ee  r2 = @r6 ; tst ; bt 0x8D39C02            empty -> no jump
    8d399f8  r5 = 0x0BA14AE8 + r14*16                 per-pad 16-byte record
    8d399fe  jsr 0x8CFB37E                            (0x0B6C41E4, record-12)
    8d39a1c  r2 = @(r1-4)  cmp/eq @(44,0x0B6C41E4)    compare 1
    8d39a2e  r2 = @(r1-16) cmp/eq @(12,0x0B6C41E4)    compare 2
    8d39a3c  jsr 0x8D3BA02 ... (then 0x8D4B0BC, 0x8D2F402, 0x8D2F9DE, 0x8D38F34)

The jump point is taken from a **per-pad record the deck keeps for that pad**
(the 16-byte record at `0x0BA14AD8 + 16*i`, plus the audio chip's copy), not
from the live slot's `+0x0C` ms. That is why the borrow could never work: a
borrowed slot changes what the pad *shows*, while the press jumps to the point
remembered for that pad, which for a never-used pad is A-C's.

So the fix, in `hc.c`, stops *clearing* that record for a D-H press and **arms
it with the pad's own cue** (`pad_arm`: `{0, 0x40, ms*3/20, loop or -1}`),
both in the 8-pad borrow (`hc_touch`) and in setting a cue from an empty pad
(`scan_pads`). The stock press path then jumps to the number the pad carries,
whatever it believes the slot holds.

Ordering: the handler reads that record at the key edge, inside the same
flush as the touch hook, so `hc_hook` re-arms it on **every** flush while a
D-H pad is held (`hc_hold_ms` / `hc_hold_loop`). That covers the window where
the borrow is still up but the slot has been put back.

Gate: `tools/hotcue8_recall_test.py` (new) checks the record instead of a jump,
so recall is covered without the seek the emulator does not perform. Measured
on the shipped `hc.bin`: every D-H press arms `{0, 0x40, n*3/20, -1}` -- D
4350, E 5550, F 6750, G 7950, H 9150 (29..61 s) -- it is still armed several
flushes later, release returns the empty pattern `{0, 0, -1, -1}`, and a press
on A/B/C leaves the record untouched. PASS.

Status: builds, and the emulator gates (`tools/hotcue8_test.py`,
`tools/hotcue8_set_test.py`) must still pass, but **recall itself is
unverified** -- the emulator performs no seek at all (§8), so only a deck run
can confirm it. Nothing was installed on the deck for this.

## 10. 2026-11-12: the real cause -- the slot's cue number is an index

Report on 1.0.9 (Italian tester, cable install): "D-H are shown, but D, E,
F, G, H always play A, B, C, with Gate Cue on or off".

§9's `pad_arm` did NOT fix it: in QEMU (real audio chip, `CALL=1 HOLD=2
QOFF=1 tools/hotcue8_qemu_test.py`) page-2 pads still played A/B. Its build
also ended at 0x09792038, inside the light-mode module (0x09792000) -- never
install that build. It is removed, and `hotcue8_recall_test.py` is retired.

What the emulator actually shows (our emulator, real audio-chip program,
CALL first -- stock pads do nothing before CALL, which is why earlier runs
"saw no seek"):

1. **The cue number is an index.** The slot's +0x0A number goes into the
   deck's 3-pad tables. With 4..8 there a press jumps to the cue and then
   playback STOPS: no more stick reads, the audio counter freezes, the pad
   sits in state 5 ("loading") forever. Same slot with number 1: jumps to
   D and plays on. Tested on the untouched 1.0.9 module too, so this is the
   shipped bug; on the deck it came out as "D-H play A-C".
2. **Pad state word.** 0x0B6C3C78 + 4*k: 3 = "cue set, load it", 5 =
   loading, 1 = loaded (pre-loaded for an instant start); 0x0B6C3C68 + 4*k
   = triggered flag. A pad used once stays 1 and keeps playing its old
   point; the press routine 0x8D3974A (state 4 -> 6, else -> 5 and request a
   load) and the poll loop 0x8D38E80 reload a pad at 3. In the "pad A used,
   then page 2" case only this word decided the jump (5.31 s vs 29.00 s);
   the 16-byte records, the descriptor and the "current cue" copy at
   0x0B6C3C20 did not.

Fix (hc.c): every slot and pad model the module writes carries the PAD
number (k+1), the letter is the module's own business (PADNUM); whenever it
changes what a pad holds it sets the state word to 3 (0 if empty) and the
triggered flag to 0 (`pad_reload`). Module ends 0x09791FE8 (24 B left).

Proof:
- `tools/hotcue8_jump_emu.py` (new): page 1 A/B, A used then page 2 pad 1 =
  D, E, D again, back to page 1 A/B, 8-pad D/H/A: all 10 land within 0.3 s.
- `tools/hotcue8_qemu_test.py` CALL=1 HOLD=2 QOFF=1: PASS, playback goes on
  after every jump.
- hotcue8_test x3 fixtures, browse x2, set: PASS (expectations changed from
  "slot number = cue number" to "pad number + the cue's time").
Not tested: Gate Cue on (held-press only in tests), a real deck.
