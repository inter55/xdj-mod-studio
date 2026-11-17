# Next deck visit -- STEMS mute: find the glitch (written 2026-11-16)

All RAM only; a power-cycle puts the deck back to stock. About 30 minutes.
The goal is ONE answer: does the glitch come from the short read-ahead or
from the rewrite of the audio chip's buffer? Test 1 has no rewrite at all.

Use a STEMS track that is at least 3 minutes long. Keep MASTER TEMPO as it
normally is. Do not touch TOUCH FX during these tests.

## Test 1 -- the CLEAN build (the one we want to ship)

Mute is heard about 4.5 s after the tap. The audio chip is never written to.

1. Power-cycle the deck, USB stick OUT, cable in. Then:

   ```bash
   .venv/bin/python tools/deck_session.py --mode rgb --touchfx --phasemeter --no-flac
   ```

   (The log must say `stems build: clean`.)

2. Stick in, load the STEMS track, press PLAY straight away.
3. **Listen for 30 s WITHOUT touching the STEMS pads.** Is playback clean?
   (This is the short read-ahead on its own.)
4. Tap DRUMS once -- **do not jump or loop first.** A tap after a jump BACK
   plays audio that is already decoded, so its delay is the distance jumped
   plus the read-ahead (this is what made the 11-17 run look like a failure).
   Count the seconds until the drums are gone. Listen for 30 s: any stutter,
   slow-down, or drums coming back?
5. Within 10 s of step 4 ending, BEFORE touching anything else:

   ```bash
   .venv/bin/python tools/stems_probe.py
   ```

6. Tap DRUMS again (drums back on), then VOCALS off, VOCALS on, a few
   seconds apart. Same listening.
7. Press a hot cue or beat jump that lands far ahead in the track (more than
   10 s). Does the music carry on at once, or is there a gap?
8. Run the probe once more.

What to write down: step 3 clean yes/no; step 4 seconds + clean yes/no;
step 7 gap yes/no.

**If Test 1 is clean everywhere: stop here. That is the build that ships.**

## Test 2 -- only if Test 1 glitched: the STOCK read path

No short read-ahead, no rewrite: the 1.0 behaviour (mute after ~30 s). This
has been clean on the deck before; it is the fallback.

1. Power-cycle, stick OUT, wait 30 s. Then:

   ```bash
   STEMS_BUILD=stock .venv/bin/python tools/deck_session.py --mode rgb --touchfx --phasemeter --no-flac
   ```

2. Same track: play, tap DRUMS, wait up to 40 s for the mute, listen.
3. `.venv/bin/python tools/stems_probe.py`

If Test 1 glitched and Test 2 is clean, the short read-ahead is the cause and
the probe lines from Test 1 say why (see below).

## Test 3 -- optional, only if Test 1 was clean and there is time: INSTANT

The rewrite on top of the clean build, now starting 2.1 s ahead of playback
instead of 0.85 s so playback cannot catch it. Expect the mute after ~2 s.
This kind of build has glitched on every visit so far.

1. Power-cycle, stick OUT, wait 30 s. Then:

   ```bash
   STEMS_BUILD=instant .venv/bin/python tools/deck_session.py --mode rgb --touchfx --phasemeter --no-flac
   ```

2. Play for 30 s first, then tap DRUMS. Listen, then run the probe at once.

## Test 3b -- optional, after Test 3: FAST (closest to instant)

Same as Test 3, but the rewrite starts 0.5 s ahead and pauses only 2 ms
between blocks. Emulator: mute heard 0.52 s after the tap. Risk: at the
deck's speed it only just keeps up with playback, so it may glitch again.

1. Power-cycle, stick OUT, wait 30 s. Then:

   ```bash
   STEMS_BUILD=fast .venv/bin/python tools/deck_session.py --mode rgb --touchfx --phasemeter --no-flac
   ```

2. Play for 30 s, tap DRUMS, listen for 30 s, run the probe at once.
   Compare with Test 3: if this one is clean too, it becomes the instant
   build; if it stutters or the drums come back, Test 3 is the one.

## Reading the probe (for whoever reads the log; it is saved in docs/deck_logs/)

- `holds: chip buffer N, position fallback M` -- the mute gate now reads the
  audio chip's own buffer level (0x11837CD0). `chip buffer` should dominate and
  `position fallback` should stay 0; `fallback` above 0 means that word is not
  usable on this deck and the old position rule ran (say so in the notes).

- `chip fed? lowest read-ahead when a read arrived`: normal is just under
  340. Under ~260 after a tap = the stick could not keep up with a muted
  read (7 stick trips instead of 1).
- `chip buffer level ... lowest`: the deck slows playback when this gets
  near 240. A "lowest since the last tap" near or under 250 = that is the
  glitch, and the limit needs to go up (SM_AHEAD in tools/stems/sm.c).
  (If the numbers look like nonsense -- thousands, or 0 while music plays --
  that word means something else on the real deck; ignore the line.)
- `muted reads: average / longest ms`: 4 blocks play in 53 ms; an average
  over ~45 ms means muted reads cannot keep the buffer level.
- `holds that ran out`: should stay small while playing.
- Instant build only: `drbump` must be 0 (playback caught the rewrite),
  `drlate` 0, and the average batch under ~6 ms.
