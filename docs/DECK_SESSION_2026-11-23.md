# Deck session 2026-11-23 -- track filter fix + stems counters

RAM only; a power-cycle = stock. Order matters: stems FIRST (its counters
die with a crash), the track filter LAST (it crashed the deck last time).

## Install (fresh power-cycled deck, stick out until asked, ~8 min)

    STEMS_BUILD=instant XDJ_SKIP_PLAYTEST=1 .venv/bin/python -u tools/deck_session.py --mode rgb --no-themes --no-flac --trackfilter

## 1. Stems (5 min) -- read the counters while it plays
1. BROWSE > FOLDER (bottom of the list) > a STEMS file. Play 30 s.
2. PERFORM > STEMS > tap DRUMS once. Note: seconds of crackle, seconds to mute.
3. Straight away, while it plays (read-only):

       .venv/bin/python -u tools/stems_probe.py

4. Unmute DRUMS, note what you hear, run the probe again.
   Judge: drbump > 0 = playback overtook the rewrite; drtot/drbat avg ms
   (want <= ~6); "position fallback" > 0 = the level word is unusable.

## 2. Track filter -- in two steps (5 min)
1. BROWSE > TRACK. Hold the funnel (right end of the header) 1 s.
   Look: whole screen clean? (no song titles showing through, "i" button and
   CRITERIA present on MY TAG). Turn the knob: the list behind must NOT move.
2. Change NOTHING, press BACK. Crash or back to the list?
3. If fine: hold the funnel again, tick BPM, press BACK. List shrinks + [count]?
4. Tap the funnel: full list back?
If it crashes at any step: tell Claude WHICH step before power-cycling.
