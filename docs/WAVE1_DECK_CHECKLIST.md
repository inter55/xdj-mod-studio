# Wave 1 deck session — checklist (print this)

Eleven things that are already emulator-passed and have never played on a deck.
Nothing here writes flash: a power-cycle returns the deck to stock. Budget
about 90 minutes, and stop at the first thing that looks wrong.

## Bring

- The Mac with the deck cable, one XDJ-700 on USB (unplug any second deck: the
  installer refuses two).
- Your rekordbox stick, plus a stick with a **STEMS track** and a **FLAC** and
  an **Opus** file if you want those rows checked.
- A stick with hot cues **A–H** exported from rekordbox (for the paging rows).
- Paper: the results table at the bottom is the deliverable.

## 0. Install (one command, ~15 min)

```
.venv/bin/python tools/deck_session.py --mode rgb --phasemeter --touchfx --brake 6
```

- `--touchfx` and `--keyshift` share one slot in the audio chip — pick Touch FX
  today (Echo and Flanger are the new rows). Do not pass both.
- `--phasemeter` and `--brake 6` are only needed if you want those rows in the
  same run; drop them for a shorter session.
- Keep the **USB stick OUT until the script says otherwise** (step 5h is last).
- It stops at the first FAIL and prints what to do. Each step is its own
  process; the 30 s gap between steps is deliberate, don't hammer it.

Expected steps: 1 survey, 2/4/5 RGB waveform, 5b, 5u, 5d, 5f, 5g, 5e, 5v,
5m, 5x, 5l, 5h.

## 1. Touch FX Echo — new

PERFORM › TOUCH FX, MASTER TEMPO **ON**, a track playing. Press **ECHO**, then
move the pad: X = delay time, Y = feedback, DEPTH slider = wet amount.

- Good: a clear repeat, spaced as you move X, fading as you move Y.
- Emulator reference: X=40 gives a repeat at 1763 frames (~40 ms), correlation
  0.92, second repeat 0.43 against feedback 0.35. Pitch and play position must
  not move.
- Bad: silence, a buzz, tempo drift, or the deck stalls. Write down which.

**Not in tonight's build.** Echo and Flanger are compiled out (`FX_LIVE`, engine built with `FX_NO_RING=1`, 1,696 B): the delay-line page is in use on the deck with audio playing. The pad shows them as COMING SOON. Test FILTER / CRUSH / GATE instead.

## 2. Touch FX Flanger — new

Same page, press **FLANGER**.

- Good: a soft jet sweep, X = rate, Y = depth, DEPTH = mix. No zipper noise.
- Emulator reference: lag sweeps 152–193 frames per quarter inside a 132–220
  window, correlation 0.86–0.89.
- Bad: comb noise with no movement, or the sweep runs backwards.

**Not in tonight's build.** Echo and Flanger are compiled out (`FX_LIVE`, engine built with `FX_NO_RING=1`, 1,696 B): the delay-line page is in use on the deck with audio playing. The pad shows them as COMING SOON. Test FILTER / CRUSH / GATE instead.

## 3. Stem mute speed — expect ~10 s, not instant

A STEMS track playing, PERFORM › STEMS, tap **VOCALS**.

- Good: vocals gone after the deck's own buffer runs out — about **10 s** on a
  partly filled buffer, **30 s** if it is full. Tapping again brings them back
  the same way.
- Important: under 1 s is *not* promised here. The rewrite is immediate but the
  audio chip has already buffered the sound; an instant mute needs a gain
  change inside the DSP (Wave 2). If you measure something much worse than
  10 s, that is the finding.

## 4. Load with a stem already muted

Mute **DRUMS**, then load another STEMS track.

- Good: it starts clean, drums stay muted, no click or dropout.

## 5. Opus playback — new

Load an **.opus** file from BROWSE.

- Good: it lists, loads and plays, exactly like the confirmed FLAC path.
- Bad: "unsupported", a freeze, or silence. Note the file's sample rate.

## 6. Hot cue pages A–C / D–F / G–H

A track with cues A–H, PERFORM › HOT CUE.

- Good: tapping the HOT CUE tab pages **D–F**, again **G–H**, again back to
  **A–C**; REC is faded/blocked on pages 2–3.
- Also: press an **empty** pad on page D–F while playing → it becomes a cue at
  that point (RAM only; a new track forgets it).

## 7. Hot cue swipe — the experiment

With the deck at a known page, run on the Mac:

```
.venv/bin/python tools/bj_monitor.py 60
```

It is read-only. Swipe **along the HOT CUE tab strip** (the small tab at the
left, not the pads) left and right, several times, slowly.

- What good looks like: `swiped` flips to 1 and the page changes. Since
  2026-11-06 the gate is 24 px (was 40) in a 92-px strip.
- What to write down: does `downx` change while your finger moves, does
  `swiped` ever become 1, and does the page change? That answers "threshold"
  versus "the deck stops sending move events once the finger leaves the tab".
- If swipe still fails, tapping the tab is the working route — say so in the
  results rather than calling the whole step a failure.

## 8. Filter playlists on the stick — new

On the Mac, with the rekordbox stick unmounted from the deck:

```
.venv/bin/python tools/filter_playlists.py /Volumes/YOURSTICK --presets
```

- Good: it backs up `export.pdb.bak-filter-N`, writes a playlist per filter
  (BPM thirds, 4+ stars, keys with 3+ tracks), and reads the result back.
- Then on the deck: BROWSE › PLAYLIST › your filter. Tracks load and play.
- Bad: a playlist is missing, or a track in it fails to load (note which).

## 9. Stems replace the original track — new

```
.venv/bin/python tools/stems_replace.py plan  /Volumes/YOURSTICK "/Contents/Artist/Album/Track.mp3"
.venv/bin/python tools/stems_replace.py apply /Volumes/YOURSTICK "/Contents/Artist/Album/Track.mp3" --stems ~/Music/STEMS --yes
```

- Run `plan` first: it prints every change and writes nothing. `apply` only
  writes with `--yes`.
- Good: the row now says `.wav`, the original is kept as `.pre-stems`, the PDB
  is backed up, and the deck shows the same title in the same playlist slot.
- Deck check: play it — the **waveform** and the **hot cues** must still be
  there (that is the only thing software cannot prove).
- Bad: track listed but silent, or re-analysed into a new ANLZ folder.

## 10. Light theme

UTILITY › THEME › LIGHT, then back to a colour theme.

- Good: text stays readable, nothing stuck white on white; lit buttons
  (VINYL BRAKE, SYNC) show no stripes.
- Also check a theme switch leaves the waveform and the phase meter alone.

## 11. 8 hot cues at once — new (2026-11-07)

PERFORM › HOT CUE, track loaded with cues A–H, then **tap the HOT CUE tab**
(not swipe). Eight pads A–H replace the A B C REC CALL row.

- Good: all eight letters show, set cues have a green strip and empty ones are
  dark; a **swipe** along the tab goes back to paging (tap returns to eight).
- Hold E (a cue past C): the deck jumps to E and, on release, returns to E.
  That is the one real risk here — the module borrows one of the three live
  slots and puts it back ~130 ms after the release (`docs/HOTCUE8_GRID.md` §4).
- Press the pad where REC used to sit (H): it must play H, never start a
  recording.
- Leave the 8-pad view by tapping the tab, then confirm the stock row works as
  before (A/B/C + REC/CALL).

## Not in this session (do not report these as failures)

- **Touch FX Pitch / Roll / Reverb** — still drawn as "COMING SOON" (five of
  eight buttons are live).

- **8-pad view and DELETE** — the double press does not exist: DELETE and the
  pads are on the same single-touch screen, and the panel sends one point. The
  remaining unknown is a *sticky* delete mode, so do this instead: tap
  MEMORY–DELETE once, release, then press pad D. Nothing should be deleted. If
  the deck does delete a cue, stop using the view and say so.
- **Theme-coloured waveforms** — the palette address is still open
  (CLAUDE.md §49); nothing to test on a deck.
- **RGB waveform on a Linked deck** — unbuilt; a linked deck still shows stock
  blue on purpose.

## Results table (filled 2026-11-06 — this is the release evidence)

| # | Item | Result | Measured / seen | Date | Deck fw |
| --- | --- | --- | --- | --- | --- |
| 1 | Echo | not in this build | compiled out (`FX_NO_RING`); Touch FX ships Filter/Crush/Gate, and the separate Touch FX run on this deck passed | 2026-11-06 | 1.15 |
| 2 | Flanger | not in this build | no delay line to build on: a four-pass survey found 512/512 pages of the first 8 MB in use (`docs/TOUCHFX_MEMORY_SURVEY.md`) | 2026-11-06 | 1.15 |
| 3 | Stem mute speed | **24.88 s** (before the fix) | stopwatch, mute pad to audible change; counters showed only 173 of ~2100 decoded blocks were rewritten. First fix (aim 32 s ahead) was instant at track start but then stuttered and later mutes died, so it is not shippable; bounded version (16 s aim) is emulator-passed and **needs a deck re-time** | 2026-11-06 | 1.15 |
| 4 | Muted load | not run | no muted load in this session | - | 1.15 |
| 5 | Opus | not run | FLAC passed (stage 5h); no Opus file in this session | - | 1.15 |
| 6 | Hot cue pages / set | pages PASS, set not re-tested | 8 pages by swipe, A-C / D-F / G-H (stage 5f) | 2026-11-06 | 1.15 |
| 7 | Hot cue swipe (`bj_monitor`) | swipe PASS, numbers not captured | pages changed A-C -> D-F -> G-H by swipe (stage 5f); `bj_monitor` downx / swiped / page were not recorded | 2026-11-06 | 1.15 |
| 8 | Filter playlists | not run | tool built and gated (21 checks); needs the Studio button and one stick. MY TAG still blocked, so no invented tag rules | - | 1.15 |
| 9 | Stems replace | not run | version switching (5b) and live stems (5g) proved instead; a replaced MP3 was not played | - | 1.15 |
| 10 | Light theme | PASS | light add-on installed (stage 5l); theme step went through all 9 (stage 5e) | 2026-11-06 | 1.15 |
| 11 | 8 hot cues at once | not run | integrated 2026-11-07 (tap the HOT CUE tab; `tools/hotcue8_test.py` passes) — needs this deck look, especially hold-and-release on E | - | 1.15 |

Also proved in this session, outside the table above: the emulator play test with
every mod installed, the firmware/mod check (fw 1.15, no earlier mods in memory),
the snapshot and code-area surveys (all zero), the RGB overview, the centre
waveform, stems version switching, the UTILITY rows, the PERFORM tabs, vinyl
brake length x6, the Type 1 phase meter, and FLAC playback. The session ran
`DONE in 36 min` with every stage finished.

Deliberately left blank: anything this session did not exercise. A blank spot is
the honest state; a tick it did not earn is a false claim in a release document.
Touch FX was a separate opt-in run the same evening (installed, PASS), which is
why Echo and Flanger read "not in this build" rather than "not tested".

Anything that fails goes into the roadmap item for that feature with the deck
date, not into a general "known issues" list — the whole point of Wave 1 is
replacing "emulator-passed" with a dated deck result.
