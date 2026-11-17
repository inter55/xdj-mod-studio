# Deck session 2026-11-09: map the chip's decoded buffer (stem mute)

Goal: one measurement that tells us why stem mutes take ~28 s on the deck
(STEMS_MUTE_LATENCY.md, end), so the instant version can be built. This build
rewrites nothing (no stutter); it only RECORDS where the chip keeps its
decoded audio.

Deck: power-cycled, stock, home screen. USB-B cable to the Mac, no hub. Quit
rekordbox and any DJ/MIDI apps. USB stick OUT during the install.

1. Install (about 5 min; pads + stems, nothing else):

       SM_CFLAGS=-DSM_MAP .venv/bin/python tools/deck_session.py --mode rgb --no-hotcues --no-themes --no-flac

   Expect stems: `RESULT: PASS ... (fast trigger on)`.

2. Stick in. BROWSE > FOLDER > the STEMS file (e.g. "Get Lucky ... (STEMS)"
   or "Don't Come Back (STEMS demo)"). Load it, press PLAY, let it play.

3. Map (the tool waits for the STEMS file, lets it play 40 s, mutes DRUMS
   by writing the pad's byte, waits ~2 s for the map, puts the mask back):

       WAIT=600 SETTLE=40 .venv/bin/python tools/stems_ringmap_deck.py

   Paste the whole output. Then run it once more (same command) so we see
   whether the layout moves. Emulator reference (2026-11-09): 2 runs,
   slots 0..359 = blocks 14720.., slots 360.. = blocks 0..4399, PSLOT =
   PLAYBLK + 360, no duplicates.

4. Optional, for a human number: tap VOCALS on PERFORM > STEMS mid-track and
   stopwatch until the vocals go (expected ~28 s on this build).

5. Power-cycle when done: the deck is stock again.

Rules: one shell session at a time (the tools pace themselves); if the deck
stops answering over USB, switch off/on AND re-seat the cable.
