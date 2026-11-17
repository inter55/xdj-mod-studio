# Deck session 2026-11-12 -- today's fixes, one sitting (~45 min)

Everything here is RAM only: a power-cycle puts the deck back to stock.
Read-only probes never write to the deck or the stick.

## 0. Install (about 25 min, deck does nothing while it runs)

1. Power-cycle the deck. **Take the USB stick OUT.** Cable in.
2. Run:

   ```bash
   .venv/bin/python tools/deck_session.py --mode rgb --touchfx --phasemeter --no-flac
   ```

   It must end with every step PASS. If anything says FAIL: stop, note the
   step, power-cycle.
3. Stick back in once it has finished.
4. Prove the hot-cue module on the deck is THIS build:

   ```bash
   .venv/bin/python tools/hotcue_probe.py --check
   ```

   Wait ~30 s before any other cable command (the deck's shell needs the gap).

## 1. Hot cues D-H (the 1.0.9 report: "D-H play A-C")

Use a track that has more than three hot cues (in the app's track list it
shows an **A-H** tag). Write down each cue's time from the deck first.

1. PERFORM. Press pad **A** (it should jump to A).
2. Swipe the HOT CUE tab left -> page 2 (pads show D E F).
3. Press pad 1 -> must jump to **D**. Pad 2 -> **E**. Pad 1 again -> **D**.
4. Swipe back to page 1, pad 1 -> **A** again.
5. Tap the HOT CUE tab -> the 8-pad view: press **D**, **H**, **A**.
6. UTILITY > HOT CUE GATE = **ON**, repeat steps 2-3 (hold the pad: it plays
   from the cue while held, goes back when released).
7. `.venv/bin/python tools/hotcue_probe.py` (wait 30 s after the check).

Fail = write "pad -> what it played" for each press.

## 2. Touch FX ROLL (new)

1. PERFORM > **TOUCH FX** > **ROLL** (bottom-left button of the grid).
2. Track playing. Hold a finger on the LEFT of the pad: a 1/2-beat loop.
3. Slide right while holding: the loop gets longer (1, 2, 4 beats).
4. Lift: the track just plays on (no stuck loop, LOOP display off).
5. Turn **SLIP** on, repeat: on lift it should jump ahead to where the track
   would have been. (The emulator could not judge SLIP -- this is the test.)

Fail = a loop that stays after lifting, or a freeze.

## 3. Waveform colours follow the theme + LIGHT buttons (new)

1. Load a track (RGB mode is installed; also try UTILITY > WAVEFORM COLOR
   **BLUE** and **3-BAND**).
2. UTILITY > THEME > **ICE-CYAN**: in BLUE mode the overview at the bottom,
   the centre waveform and the PERFORM strip turn cyan. In 3-BAND the blue
   parts follow the theme too. RGB stays the track's own colours (on purpose).
3. THEME > **HOT-PINK**: pink. THEME > **LIGHT**: waveform stays blue, and the
   **SLIP / VINYL BRAKE / SYNC buttons have smooth edges** (no dotted rim).
4. THEME > **OFF**: everything back to stock.

A photo of the deck in ICE-CYAN (BLUE mode) and in LIGHT is the best evidence.

## 4. Stem mute (unchanged since the last visit, still unmeasured)

Load a STEMS track, play ~1 min, tap **DRUMS** once. Note: how fast is it
heard, any stutter, does the drum come back. Wait 15 s, then:

```bash
.venv/bin/python tools/stems_probe.py
```

## Send back

- the pad -> time table from section 1 (even if all correct)
- one line each for ROLL (with and without SLIP), themes, LIGHT buttons, stems
- the logs in `docs/deck_logs/` (hotcue_probe, stems_probe)

## RESULTS 2026-11-12 (deck <deck serial>, install all PASS in 21 min)

User report:
- Hot cues: swiping the HOT CUE tab opened TOUCH FX instead. The tap/
  double-tap 8-pad grid layout is wrong: user wants pages that look EXACTLY
  like the stock A B C row, swipe -> D E F, swipe -> G H.
- Touch FX: "no sound at all", and the panel does not cover some text.
  PROBE (docs/deck_logs/tfx_probe_2026-11-12.log): tfx_word 0, tfx_active 0
  all session, MT byte 0, engine 1, NOP live, ksh_status 1 -> MASTER TEMPO
  was OFF, so the panel never armed the effect. Design trap, not a crash.
- Waveform colours follow the theme: WORKS. Rename BLUE -> "BLUE / THEME".
- HOT CUE GATE: user does not know what it does (needs a clearer name or
  removal).
- LIGHT: fine except MASTER CLOCK text, centre waveform background still
  black, some buttons glitch while HELD (pressed sprites), UTILITY dark text
  on dark background.
- Stems: "stuttering and glitching, very bad". PROBE (stems_probe log):
  mask 02, drblk 596, drsub 199, drbump 7, drfade 18, drnoslot 1691,
  drskip 1690, hpibusy 28, 237 batches 3638 ms (avg 15.4, max 35 ms),
  drpk 64. => player held 15-35 ms per batch, overtaken 7 times.
