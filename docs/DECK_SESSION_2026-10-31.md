# Deck session 2026-10-31 (user visit, ~10 min warning)

Priority-ordered checklist for this session. Tomorrow's agent: read the
appendix at the bottom and fill it with the user's answers, then open
`docs/ROADMAP.md` items from it. NEVER flash anything outside the app; all
tests use the normal cable RAM install + the app's own Build UPD flow
only if the user asks.

## Bring
- Laptop with the app (built app or `studio/app.py`), USB-B data cable.
- A stick with: a converted 3-band track, a `(STEMS).wav` track, a FLAC
  track, a track with hot cues D–H exported from rekordbox.
- Second deck (if available) + Link cable for the link tests (P1–P3).

## P1 — Link freeze bisect (top priority, freeze history)
1. Mods on deck 2: **pads only** (no meter, no themes) → deck 1's stick
   plays a track over Link into deck 2. Play 3 tracks. Freeze?
2. Mods on deck 2: **pads + guarded phase meter** → same link test. Freeze?
   (0.9.8-era unguarded meter froze twice; guards went in, expect NO freeze.)
   ALSO note: the LINKED deck itself (deck 1, unmodded, playing) shows NO
   modded meter/phase — expected, that is the Pro DJ Link item (both-decks
   phase like the XDJ-700 PLUS needs the linked deck reading analysis data,
   ROADMAP §97, next big item). User priority 10-31: cover BOTH decks, not
   just the modded one.
3. Mods on deck 2: **full set** (pads + meter + themes + stems) → same.
4. Watch: does the STOCK phase meter overlap ours while the linked deck
   is master (the NOP-flush fix needs a deck look — either 0 or 154-both
   visible)? Type 1 meter OK visually? Master row / horizontal layout?
   Note the exact screen + where on it.

## P2 — converts: blue 3-band + FLAC
5. Track Builder track (RGB + PWV7 written): blue 3-BAND in use, not
   broken-blue reverts on >7:17 tracks (1.0 fix, emu-verified).
6. FLAC load, INCLUDING the 96 kHz / 24-bit file: browse → push → play →
   skip around. User worry "96k wouldn't play anyway" is WRONG —
   tools/xflac/vfile.c decimates 88.2/96k by 2 (dec=4 for 176.4/192k),
   24-bit carried, and unplayable rates get served as an empty file so
   the deck never sees raw FLAC (the 2026-10-25 freeze). re-test =
   confirm on hardware.

## P3 — stems + hot cues
7. `(STEMS).wav` STEMS tab: mute each part, instant mute timing, any
   crackle on stem switch (opus/aiff versions too if on the stick).
8. Deck-side D–H status: this is the "our own storage on the stick"
   feature — nothing ships yet; ask whether setting extra cues via
   rekordbox re-export still works (A–H tag in Track Builder).

## P4 — misc visuals + utility
9. UTILITY rows on deck (MODS/WAVEFORM COLOR/VINYL BRAKE LENGTH 0.5×–15×/
   HOT CUE GATE/THEME) — push/turn/push, note any row that misbehaves.
10. Light-mode deck theme: user liked screenshots/mockups/light_theme.png
    — if the light theme preset is on the stick, how do track name /
    time / BPM text colours look (research item: find firmware's own
    text colours)?
11. Key shift: hiss was FIXED before (deck round 2: key shift v3 PERFECT,
    no AIFF noise — user re-confirmed 10-31). 10-second sanity check only:
    shift ±6 on one AIFF, move on.

## Recurring agenda (user-set 2026-10-31)
- PHASE METER: user 10-31, hard requirement — "on BOTH decks even if one
  has the mods and NOT the other". When decks are linked, the modded deck
  must draw BOTH meters (own + the linked deck's) from the Link data.
  This is the ROADMAP §97 Pro DJ Link item; non-negotiable v1.0 scope.
- USB-stick mods file ("File on your USB stick") is NOW, not 1.1. The
  stock updater flow the user pasted 10-31 covers the boot hook:
  * stick FAT32, MBR partition; XDJ700.UPD at the ROOT, do NOT rename
  * power OFF, nothing connected; hold IN + RELOOP/EXIT while powering ON
  * deck shows [Connect USB storage device into top USB port]; insert
    stick in the TOP port → progress bar, ~3 min
  * [Firmware update is completed.] → power off/on; stuck >10 min or
    [MAIN firmware update failed] = trouble
  This is the same mechanism our permanent_build.py UPDs ride, so the
  loader design starts from: flash usbloader ONCE via this flow, then
  every boot the loader reads a mods file from the stick root. No
  read-only stick-boot probing needed this session.
- Emulator out of beta + realtime: after this session, all beta copy in
  the app (Emulator (beta) tag, emuBetaNote, phase meter "(beta)" tag)
  comes off once P1–P2 pass.

## Install run (all-on, cable/RAM) — 2026-10-31, session start ~now
- Command: tools/deck_session.py --mode rgb --phasemeter --keyshift --no-opus
  (full default bundle; user said "do whatever", deck plugged in).
- Emulator gate: PASS (all mods play together). Deck check: PASS — firmware
  1.15, no earlier mods in memory.
- Units complete: UTILITY rows PASS, centre waveform blob 28,624 B in 36 s,
  theme tables "tables complete" + arm session, FLAC "code complete" + arm,
  final RESULT: PASS.
- DONE in 23 min, EXIT=0: waveform=rgb; PERFORM tabs BEAT JUMP / HOT CUE /
  KEY SHIFT / STEMS; UTILITY mod rows at the end; phase meter installed;
  live key shift installed.
- Follow-up for the user: reinsert the stick once (if it was in during the
  install) so FLAC files re-scan; then sanity checks P2 (96 k FLAC, key
  shift hiss), then phase meter look (master row stays dim, one deck).

## Community asks about v1 (from user thread) — 2026-10-31
1. 8 hot cues "not working even though I have many set": confirm today on
   the deck with the user's own stick (cues set in rekordbox + re-exported
   so they are in the exported .ANLZ analysis; Track Builder shows an A-H
   tag when they are). If the user's stick has cues exported and D-H still
   don't hold, that is a real bug -> capture a failing stick export.
2. Request: hide the KEY SHIFT / STEMS tabs even when installed, to give
   HOT CUE + BEAT JUMP more room (adding to ROADMAP as a v1.x candidate:
   UTILITY > MODS-style hide row or a FEATB flag the installer sets).
3. Request: track filter/browse system like XDJ-1000 MK2 and CDJ-2000 NXS2
   (user calls it the main missing thing). Big RE job on the browse engine;
   record as candidate for 1.1, not v1 (P1/P2 gates first).
4. TOUCH FX: already designed and APPROVED (ROADMAP TOUCH FX item) —
   community wants it too; keep in v1 scope unless the packer slips.

## Live results (user at deck, deck1 modded + deck2 stock, linked)
1. PHASE METER TOP ROW NOT SHOWING: single-deck row works (own beats);
   linked row stays empty even while deck2 plays. P1 data path suspected —
   the mod's link-beat tap not consuming (or not receiving) deck2 beatgrid
   packets. TODO: packet probe while deck2 plays (deck + link capture),
   check master-designation requirement, then fix in the tap code.
2. STEMS MUTE GLITCH (track "radio 84"): drums-off took a few seconds
   (known: ~5 s cached-DSP-block rewrite), BUT new symptom on top: after
   the mute landed, playback ran SLOWED briefly, then "glitched back into
   the normal" (pitch/time lurched twice). Suspect the mute rewrite
   interacting with the beat-grid/quantizer or a DSP buffer refill race.
   v1 item "near-instant stem mute" already targets the rewrite itself —
   this adds a pitch/timing recovery bug to it.
3. Everything else works with deck2 linked: no link freeze observed
   (P1 freeze did NOT fire in this pairing yet), per-deck mods unaffected.

## Phase-meter probe results (read-only, deck2 playing) — 2026-10-31
- pm_probe: hook draws fine (87k fills); sampled MASTER-row pixels all
  zero -> row never painted. pm.c documents why: "orange = master (needs
  link data: dim line for now), blue = this deck" — shipped as a dim line
  in v1, tap never written.
- link_beat_search (deck1 paused, 8 snapshots): TWO clean beat-in-bar
  counters cycle 1..4: 0x0a8d042e (region 0x0a8cf000) and 0x0a8be662
  (region 0x0a8be000) — one own / one linked-deck. LINK DATA FLOWS.
- Conclusion: P1 link path is ALIVE; phase master row is a tap-job:
  read the second counter in pmhook/pm.c and draw the master ruler.
  For a full master ruler deck2's BPM is also needed (bar phase alone
  gives the downbeat marker; check whether the link carries bpm*100
  packets next to these bytes — read neighbours of both counters).

## Phase meter MASTER ROW BUILT (tonight's big step) — 2026-10-31 evening
- pm.c tap implemented: reads both link bar mirrors (0x0A8D042E /
  0x0A8BE662) every frame, POSL-ms timestamps, EMA beat period, scrolls
  orange ticks + red downbeats on the top row; frozen (not extrapolating)
  when the master is paused; mirrors must agree (mid-write ignored).
- Build clean (0 FPU); tools/phasemeter_test.py RESULT: PASS (own row
  unchanged, pointers/Browse/Menu gates green, tap no-ops without link
  data). pm.bin rebuilt at build/phasemeter/.
- Deck reinstall of the updated meter REFUSED (mods already in RAM):
  needs deck1 power-cycled first. TOMORROW'S FIRST ACTION: user powers
  deck1 off/on, then: tools/deck_session.py --mode rgb --phasemeter
  (3 sessions, ~3 min), then user checks: two linked decks playing ->
  orange ticks scroll on the top row, red cross at master downbeats,
  row freezes when deck2 pauses.
- Stems timing glitch (radio 84) still open: folded into the
  near-instant-mute v1 item.
