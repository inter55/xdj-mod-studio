# XDJ Mod Studio: what the user wants for v1 and after (2026-11-03)

For another agent picking this up. This is the user's own list, collected
from their messages, with where each item stands. Read CLAUDE.md (the
project log) and docs/ROADMAP.md for the detail behind each line.

## How the user works / what "done" means

- The user is the developer's only tester with an XDJ-700 (serial
  <deck serial>, firmware 1.15) and a Mac. Non-technical: write to them in
  plain English, no jargon.
- **v1 must be "PERFECT". Build everything, but do NOT release** until the
  user says so. Never publish, push or commit without being asked.
- Everything is proven in the emulator first, then on the real deck. Deck
  time is short and precious: arrive with a checklist (see
  docs/DECK_SESSION_*.md).
- The user will personally use the **USB stick file method** every time
  they play if it works: it is the priority delivery method.

## v1: must have

### Delivery (how mods get onto the deck)
1. **USB stick file method** (flash a small loader update once; any stick
   carrying `XDJMODS/PACK.BIN` brings the mods, other sticks play stock).
   Status: proven in the emulator end to end, including all failure
   cases. First real flash planned for the 2026-11-03 deck session
   (docs/DECK_SESSION_2026-11-03.md). Tools: tools/stick_pack.py,
   tools/usbloader/, tools/permanent_build.py --stick-loader,
   tools/usbloader_pack2_test.py.
2. **Live themes and FLAC must work with the update/USB method too.** They
   don't fit inside the update file itself (about 15 KB of room left;
   FLAC is about 40 KB, themes about 75 KB). They ride on the stick in
   PACK.BIN instead. Make the app say so simply.
3. Cable install stays (temporary, until power-off).

### The app (XDJ Mod Studio): simpler
4. **Far less clutter.** The Mods page, the "Good to know" box and the
   Setup list showed too much. Making mods is "way too complex".
   Started 2026-11-03: home shows only the hero and the cable box (the setup
   list appears only when something needs fixing); the install choice is
   two cards (USB stick, recommended / cable) with the update-file builder
   as an "Advanced" link; mod descriptions cut to one short line. Keep
   going: every screen should say only what the user needs right then.
5. **"File on USB stick" must not feel like making an update.** It should
   read as two simple steps: (1) put the mods file on my stick (every
   time), (2) one-time: give the deck the loader (only once, with
   recovery steps).
6. **Choose your own colour** in the Theme Builder (a colour picker, not
   only presets). Theme colour picker added 2026-11-03. Still to build:
   a custom WAVEFORM colour. The app had a hidden "custom" waveform
   option that no button reached, and the deck side was never built
   (recolour the stock overview colour and the centre palette at
   0x0E736150).
7. Light mode in the app.

### Deck features
8. **Stem mute under 1 second.** Built 2026-11-03 (fast trigger; emulator:
   heard 30-40 ms after the tap, bit-exact). Needs the deck check.
9. **8 hot cues: D-H settable ON THE DECK** (REC / tap an empty pad on
   pages D-F and G-H stores the cue; delete too; saved to the stick).
   "You shouldn't have to do it in rekordbox." Setting D-H on the deck
   IS built (2026-11-01, tools/hotcue8/hc.c: an empty pad on page 2/3
   stores the cue at the playhead; tools/hotcue8_set_test.py PASS), but
   in RAM only: a new track or power-off forgets it. Still to build: save
   them to the stick (the deck's own ANLZ writer path, emulator-proven
   first), plus delete. Paging by swipe already exists.
   SAVE TO STICK, 2026-11-03 late: two findings.
   (a) REC does NOT save D-H. Tested in the emulator with a stick carrying
   A-C only, page D-F made live exactly as hc.c load_page(1) does, REC
   allowed (HCPAGE=0): the .DAT came back byte-identical. The stock cue
   writer does not serialise arbitrary live slots, so unblocking REC cannot
   save anything (scratchpad/rec_save2.py, build/rec_save2.log).
   (b) The write itself works: tools/anlz_cues.py rewrites the PCOB (type 1)
   hot-cue section (and PCO2 in .EXT) in the layout the deck parses, and the
   deck loaded ALL EIGHT cues straight out of the ctx afterwards --
   {1:5000, 2:13000, 3:21000, 4:61000, 5:62000, 6:63000, 7:64000, 8:65000} with
   live slots A-C left stock (scratchpad/anlz_deck_test.py,
   build/anlz_deck.log). CLI: show / set 4=61000,5=62000 / clear 4,5.
   (c) A track with NO hot cues is fine, which is most of this library.
   scratchpad/hc_pages_real.py loads a REAL export carrying 0 cues, installs
   the module, opens PERFORM: page 2 is reachable (HCPAGE=1, count=0) and a
   press on the empty pad A sets cue 4 at the playhead -- slot reads
   (num 4, ms 1646, state 0x40006) -- with zero disk writes. So seeing and
   setting D-H never depended on the track having cues.
   Library survey (263 analysed tracks): 200 have 0 hot cues, 62 have 1,
   1 has 2. Every one carries an empty PCOB type 1 (24 bytes), so the writer
   always has a section to fill; if an older export has none, anlz_cues now
   appends one. Proven: a 0-cue track written with A-H was parsed by the deck
   as {37,44,51,58,65,72,79,86} s (scratchpad/anlz_zero_test.py,
   build/anlz_zero.log).
   Still to build: the app button that calls it for the loaded track (needs
   the deck's track identity over the shell) so deck-set cues survive a track
   change, and delete on the deck.
10. **Touch FX: all 8 effects.** Filter, Crush and Gate work on the deck.
    Echo, Pitch, Flanger, Roll and Reverb need a bigger free area in the
    audio chip's memory (read-only deck survey first). Reverb may slip
    to 1.1. SURVEY DONE 2026-11-03 (static, no deck):
    docs/TOUCHFX_MEMORY_SURVEY.md. Our DSP window is 0xC005F4C0..
    0xC0060000 = 2,880 B, already shared with live key shift; Echo needs
    44-88 KB, Reverb 50-65 KB, Flanger 8.8 KB, Roll 44 KB (or 0 by using
    the deck's own loop engine). Pitch needs NO new memory -- it is the
    key-shift stretcher we already have, so it only needs the UI wired.
    Lead to check on the deck: the DSP download list at blk1 0x00004D70
    puts the program around 0xC0045900, leaving a ~101 KB gap up to our
    window that is unproven (parse the record lengths, then read the range
    through the DSP address window before designing anything into it).
11. Phase meter as a UTILITY setting, TYPE 1 / TYPE 2 like the
    XDJ-1000MK2 (built), plus the master row for a second linked deck
    (built, needs the deck).
12. Live key shift OR Touch FX, chosen in the app (they share the audio
    chip's hook).
13. Themes (live switching on the deck), waveform colours that follow the
    theme, light theme on the deck.
14. Stems files replace the originals on the stick (WAV/AIFF), so they
    show up normally (built).
14b. **Make it obvious the deck doesn't split stems by itself** (user,
    2026-11-03: people load a normal track and "nothing happens"). App
    wording done 2026-11-03 (Mods tick, Track Builder banner). Still to
    build: on the deck, the STEMS tab shows "NO STEMS IN THIS TRACK"
    when the loaded file isn't a STEMS file (the stems module knows:
    `valid`). PERFORM-pad assembly has crashed decks twice over delay
    slots: emulator + slotscan before any deck test.
    BUILT AND EMULATOR-VERIFIED 2026-11-03 (late): the row reads
    `SM_valid` through PMASK+3, draws one dim line when it is 0, and the
    module forces the repaint with PMASK+1 = 0xFF. `build/stemsok_test.py`
    measured it: 28,302 row pixels change from pads to the line and back to
    byte-identical pads, with the deck's own flush (a key press), no tap.
    The same case now sits in `tools/perform_pads.py emu`, and that suite
    passed with it: RESULT: PASS, TEST_EXIT:0 (build/pads_test.log), the
    row check itself measuring 28,292 changed pixels, zero failed checks.
    `tools/stems_fastmute_emu.py` also re-passed after the change (mute
    heard 0.000 s after the tap, EXIT:0).
    The change had also broken the build: the new string pushed the pads'
    data block 16 B past TRKKEY and `blobs()` refused to assemble, so no
    install could have been made from the working tree. TRKKEY moved
    0x19A4 -> 0x19C8 (the 36 spare bytes before PMASK); range still ends
    0x081E03E8, inside the zero run. slotscan: 1 hit, unchanged, in
    HOOK_SRC (see its output before trusting it).
    The tap wart is fixed too: the touch hook now returns early when
    STEMSOK is 0 (`pstmnone`), so a tap where the invisible pads sit does
    nothing. That needed 14 bytes the hook did not have, so PDRAW moved
    0x5F0 -> 0x600 (the draw unit had 24 spare before KSSTEP; it now ends
    8 B short). Full suite after the gate: RESULT: PASS, TEST_EXIT:0, 0
    failed checks, row check 28,292 px (build/pads_test.log). The suite's
    own mask-walk taps now set STEMSOK=1 first, which is what a STEMS file
    does -- without it they were correctly inert and the run failed.
15. Bugs to confirm fixed on the deck: FLAC load freeze, key-shift noise
    on AIFF, beat jump 32/64, lit-button theme stripes, the STEMS tab not
    responding when Key Shift is hidden.

### Emulator (in the app)
16. **Real time, with no exceptions.** The user's words: "we need real time
    or v1 isn't coming out". Specifically:
    - no noise frame on boot
    - play/pause must never jump in the track (FIXED 2026-11-03: we were
      driving the needle-search strip by mistake)
    - smooth frame rate. Today about 20 frames/s idle and 6-9 while
      playing; the deck's main chip is translated on one Mac core.
      docs/EMU_REBUILD_PLAN.md has the measurements.
    - the jog wheel must work properly. On the real deck the top platter
      (touch, scratch in vinyl mode) and the outer ring (pitch bend) are
      different; the app treats the whole wheel as a top touch and sends
      a fixed speed. Being measured 2026-11-03.
    - tempo fader works (FIXED 2026-11-03)
    - Master Tempo without stutter (audio was ~92 % of real time)
    - clicks must feel instant (now 0.3-0.6 s through the app)
17. Windows: works and is tested by a real person before "beta" comes off.
    AUDIT 2026-11-03: the CI workflow's inputs all exist (emu/fastcore.c,
    emu/c6x/c6xcore.c, both qemu patches, tools/build_studio_win.py,
    tools/win_selftest.py) and the app's own self-test -- the same one the
    Windows smoke step runs -- passes on the Mac: RESULT: PASS (C cores,
    tic6x objdump, hidapi, prebuilt deck code, installer plans). What is
    still unproven is the CI machine itself: the exe, the windowed-subsystem
    check and the port-8799 smoke run need a Windows runner or a tester.

### Nice for v1 if possible
18. OneLibrary converter in the app (built; needs its database library
    bundled and one stick tried on the deck).
19. Track filter (community ask). Research 2026-11-04: the firmware has no
    filter code at all -- no FILTER/FLTR/RBFLTR in the image -- so it must be
    built, not enabled. See docs/ROADMAP.md "1.1 and later".
20. Refreshed website, README, release notes when it ships.

## After v1 (1.1 and later): ideas

The user's own ideas and community asks so far:

- Anything above that slips (Reverb, track filter).
- Mods baked into the update file AND the stick loader together (today:
  one or the other; the stick carries themes and FLAC).
- Custom waveform colour on the deck (if it misses v1).
- **A small web version (after v1, user agreed 2026-11-03).** For people
  who want mods without installing the app (a Reddit commenter's mods
  needed no app):
  - tick mods, download the stick's `XDJMODS/PACK.BIN` (our own code only:
    can be prebuilt per mod combination);
  - build the one-time loader update IN THE BROWSER from the user's own
    `XDJ700.UPD` (never uploaded, never hosted: it's Pioneer firmware);
  - maybe cable installs over WebHID (Chrome).
  Stays in the app: stems (Demucs), the emulator, theme previews. Check
  whether the live-theme tables count as firmware-derived before
  hosting them.
- Link over USB.
- More hot cue and loop pages.
- Custom home screen.
- Community asks logged in docs/ROADMAP.md ("Community asks").

## Rules that always apply

- RAM-only mods on the cable path; the update file is built from the
  user's own official 1.15 file and is never flashed by the app.
- Never put Pioneer firmware or anything derived from it in a public repo
  or public build (tools/make_public_repo.sh checks).
- Emulator first, then deck. Read-only surveys before using any new memory
  area on the deck.
- XDJ-700 only (no XDJ-1000MK2 mods).

### Addendum 2026-11-03 (corrected the same night)

An instant mute IS possible, and item 10 now means it: measured at 38 ms in
the emulator with the real DSP core (one poll plus one 588-frame block; ~50 ms
on the deck at its 28 polls/s). The first hardware
reading (~30 s) was a bug, not a limit: the rewrite anchored its slots by
searching the chip's store, so it wrote muted audio into old copies nothing
plays, and the aim came from the furthest read (rfront), so after a jump it
chased blocks the chip had not decoded. Anchored on the playhead, starting at
the playhead and refused for half-decoded slots, the emulator shows every
slot from the playhead forward sample-exact right after the tap. See
docs/STEMS_MUTE_LATENCY.md.
