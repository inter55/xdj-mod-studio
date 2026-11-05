# Deck session 2026-11-03 (tonight)

Three parts, in this order. Part A changes nothing permanent (a power-cycle
undoes it). Part B is the first time a modded update file goes on the deck.
Do A first: if anything in A looks wrong, stop there.

What to bring: the Mac with the deck cable, and THREE USB sticks:

| stick | what is on it | made how |
| --- | --- | --- |
| 1. Recovery | Pioneer's official `XDJ700.UPD` (v1.15) | copy `FIRMWARE/XDJ700_stock_v1.15.UPD`, rename to `XDJ700.UPD` |
| 2. Loader | the loader update `XDJ700.UPD` (1.15 + the stick loader) | app: Mod > File on your USB stick > step 2 |
| 3. Your rekordbox stick | your music + `XDJMODS/PACK.BIN` | app: Mod > File on your USB stick > step 1 |

Sticks 1 and 2: format them on the Mac with Disk Utility as **MS-DOS (FAT32)
with the Master Boot Record scheme** (not GUID: the deck can miss the file).
The file goes on the top level, named exactly `XDJ700.UPD`, and it must be
the only `.UPD` on the stick.

---

## Part A: cable install, check what is new (about 30 min)

Install everything with the cable as usual (Mods page > Install). Then:

1. **STEMS tab with Key Shift hidden**: one tap switches to STEMS. (Fixed
   last night; first time on the deck.)
2. **Stem mute**: play a STEMS track, tap VOCALS. It goes silent with the
   deck's own buffer delay, not instantly -- ~10 s on a partly filled buffer,
   ~30 s on a full one (measured on hardware 2026-11-03; the emulator's 0.04 s
   only times our rewrite, which the chip's buffer then still has to reach).
   Tapping again brings vocals back the same way. An instant mute has to move
   into the audio chip's DSP; the read path cannot do it. See the night-run
   note at the end of this file.
3. **Load with a stem already muted**: mute DRUMS, then load another STEMS
   track. Does it start clean (no glitch, drums stay muted)?
4. **Touch FX Gate**: works on the pad (Filter and Crush already confirmed).
5. **Light theme**: UTILITY > THEME > LIGHT, then back to a colour theme.
   Text readable? Anything stuck white or black?
6. **Lit buttons in a theme**: VINYL BRAKE / SYNC lit: no stripes.
7. **Beat jump 32 / 64**: each tap jumps.
8. **FLAC**: load a FLAC file: it plays, no freeze.
9. **Key shift on an AIFF** (only if you installed Key Shift instead of
   Touch FX): no noise in the right speaker.
10. **Set hot cues D-H on the deck**: play a track, tap the HOT CUE tab
   again (or swipe) to show D-F, tap an empty pad: it becomes a cue at that
   moment. Tap it again later: the track jumps there. (For now these
   live in the deck's memory only: loading another track or switching off
   forgets them; saving them to the stick comes next.)11. **Phase meter, second deck** (only if deck 2 is there): the orange
    row moves with deck 2's beats.
12. **PERFORM › STEMS on a NORMAL track**: the row must read
    "NO STEMS IN THIS TRACK" instead of four pads that do nothing. Load a
    STEMS track next: the four pads come back by themselves, without a tap.

Write down anything odd (a phone video helps). Power-cycle afterwards: that
takes the deck back to stock for Part B.

## Part B: the USB file method (the first flash)

This replaces the cable for good: flash the loader once, and from then on
any stick with `XDJMODS/PACK.BIN` brings the mods; any other stick plays
stock.

What is proven in the emulator with today's build:
- The loader update builds from your own official file. Checksums are
  OK; the boot code, block 0 and the panel section are byte-identical
  to stock.
- Cold boot from the loader update with stick 3: 422 of 422 mod
  records applied, the deck keeps running, and live key shift is active.
- The same records give exactly the cable install's bytes (only a frame
  counter differs, as between any two runs).
- A broken file stops cleanly at the broken record. A deck that isn't
  stock gets nothing written at all. No file on the stick = a normal deck.

Not proven: no modded update file has ever been flashed onto a real deck.
The way back is Pioneer's official file (stick 1).

Steps:
1. Deck on **mains power**. Power off. Take out any stick.
2. Hold **IN** and **RELOOP/EXIT** together and power on. Keep holding
   until the screen says to connect a USB device to the top port.
3. Insert **stick 2** (loader) in the top port. Start the update when
   asked. **Do not touch anything until it says it is finished**, even if
   it restarts itself. This takes several minutes.
4. Power off, take the stick out, power on normally.
5. Check: the deck boots. UTILITY shows version **1.16** (the loader
   update says 1.16 so the deck accepts it).
6. Insert a **plain stick** (no mods file): everything is stock. That's
   correct.
7. Power off, power on, insert **stick 3**: after it mounts, the mods
   should be there (colour waveforms, the PERFORM tabs, the UTILITY rows,
   and the rest of what you ticked).
8. Play a track and run through a few of the Part A checks.

If anything is wrong at step 5 or later: power off, then flash **stick 1**
(the official file) with the same steps 1-4. That puts the deck back to
exactly stock.

If the deck refuses the loader update or nothing happens, that is harmless:
power off, remove the stick, power on normally.

## Part C: notes for the emulator (no deck needed)

- If "noise on boot" means a crackle in the sound (not a flicker on the
  screen), tell the developer: today's fixes targeted a screen flicker.

## Night run 2026-11-03 (second session, deck on USB)

Install command: `bash tools/deck_session.py --mode rgb --phasemeter --touchfx`.
Steps that ran and passed: 0b survey, 2b survey, 5b stem versions, 5u UTILITY
rows, 5d PERFORM pads, 5f 8 hot cue pages, 5g live stems with the fast trigger
armed, 5e themes (every session PARTIAL, last one PASS for all 9 themes).

**The run stopped early:** 5h FLAC refused -- "a track is loaded - eject the
USB stick first" -- after two good sessions (115,572 B then 83,926 B left).
deck_session exits on that failure, and the TOUCH FX steps (5x) come *after*
5h in the code, so the engine and the screen were never installed. That is the
whole reason the deck showed no TOUCH FX tab; there was nothing wrong with the
FX code. Both were installed by hand afterwards:
`tools/keyshift_hot_apply.py --engine fx` PASS, then
`tools/touchfx_apply.py --survey` PASS (area all zero), then
`tools/touchfx_apply.py --engine-ok` twice (code, then arm)
-> PASS, "PERFORM > TOUCH FX (in the tab stack)". Effects need MASTER TEMPO ON.
Still to finish: FLAC, with the stick ejected --
`.venv/bin/python tools/flac_apply.py` until PASS.

### What the user checked, on hardware

- STEMS tab with Key Shift hidden: **works** (one tap).
- "NO STEMS IN THIS TRACK" row on a normal track: **works**.
- Touch FX: **was missing** (cause above); now installed.
- Live stem mute: **about 30 s**, sometimes not at all.

### Stem mute: the 30 s was a bug (fixed and proven in the emulator)

Read live on the deck while it played: poll pool 0x08C1D56C = `sm_poll_entry`
(armed), media pool 0x08D5FFA8 = `sm_fread` -- the trigger was installed
correctly, so the slowness was in the rewrite. Measured on hardware: the tap
was heard ~30 s late and sometimes seemed to do nothing.

Cause (found by reading the chip's own ring in the emulator): the rewrite
picked slots by SEARCHING the decoded store for a block label, so after the
track had played a while it wrote muted audio into old copies that never
play again; and it aimed using `rfront` (the furthest byte ever read), so
after a jump back it chased blocks the chip had not decoded yet and stalled
(`drnoslot 420`, wanting block 5000 while the chip played 2902). The chip's
ring turned out to be exactly the file's mix plane, 588 stereo frames per
slot as float32 = s16/32768, aligned to the sample.

Fix in tools/stems/sm.c: anchor on the chip's play slot (0x1182E81C), start
each pass AT the playhead, clamp the aim to 32 s ahead, skip holes, never
write a slot whose successor has no label yet (that guard removes the
stutter), 128 blocks per poll while catching up and 4 afterwards.

Emulator proof: 16/16 slots from the playhead bit-exact for the muted mix
within one poll, unmute bit-exact, no skips/failures. Audible A/B from the
same snapshot: first differing sample 1,668 = **38 ms** after the tap, 94.7%
of samples different afterwards. Not yet re-tested on the deck.

### Not yet done

- FLAC install (83,926 B left) and the FLAC playback check.
- Load-with-drums-muted; light theme; beat jump 32/64; hot cues D-H on the deck.
- Stick-file test on hardware (needs a power-cycle first).

### Light theme on the deck: why it looked broken (review, same night)

User photos: titles, track lists, "NO USB", "CUE/LOOP CALL", "ZOOM GRID",
"Bars" almost invisible (white-ish on light blue); waveform window black.
CAUSE: the light ADD-ON (step 5l, tools/lighttext: text ink, waveform
window, screen-clear fills) was NEVER INSTALLED -- the run ended at 5h
(FLAC refused, track loaded) and 5l comes after it. The deck had only the
LIGHT theme's picture tables (5e), so pictures flipped light while every
CPU-drawn text stayed white. Same reason the phase meter (5m) was missing.
FIXES (not yet on the deck):
- deck_session: FLAC is now the LAST step, waits (up to 10 min) for the
  stick to come out instead of stopping, and never stops the run.
- UTILITY > THEME only offers LIGHT once the add-on has armed itself
  (lt_idx == LIGHT's number) -> half-light can't be picked any more.
To check on the deck next time: full light with the add-on; if the "E" in
PLAYER or other small labels still look speckled, run tools/theme_skips.py
(read-only) -- that would be a picture block the safety guard skipped.

### Next visit: why stem mute is still ~30 s (2 minutes, read-only)

User: Touch FX GATE works; stem mute still ~30 s; stick method not tried.
The ~30 s is the audio chip's own buffer. The fast path rewrites that buffer
over the host port (emulator: heard at once), but tonight's probe printed
only the slow path's counters. sm.c now counts each way the fast path gives
up, and stems_probe.py prints them. On the deck (cable install of the new
build): play a STEMS track, tap VOCALS, wait 5 s, then run
`.venv/bin/python tools/stems_probe.py` once. Read the "fast rewrite" line:
- polls 0            -> the player-task hook never runs on hardware
- parm 0             -> the tap is not seen from the hook
- drbusy large       -> the deck's host-port DMA is always busy (our guard)
- drnoslot large     -> the chip's cache is laid out differently on hardware
                        ("header" = the word we read there)
- drwfail large      -> reading/mixing the stems file fails in that task
- drblk in hundreds  -> the rewrite ran: then the delay is elsewhere
