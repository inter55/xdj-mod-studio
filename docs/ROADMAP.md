# XDJ Mod Studio roadmap (user-agreed 2026-10-21, after v0.9.4)

Rules that hold for every version: XDJ-700 only; nothing flashed unless the
user explicitly builds a UPD; emulator before deck; never publish firmware.
Public repo = tools/make_public_repo.sh export only (agents get the PUBLIC
repo). Public builds: bake-free files swapped in, tools/scrub_app.py keeps
the builder's name out. No new Reddit post per release: edit the original
post / reply in its comments. Tester bug fixes take the next 0.9.x number
whenever they're needed.

## Shipped
- v0.9 / v0.9.1 (2026-10-19): first public release; install fix for the
  downloaded app; Updates section; centred UPD tab.
- v0.9.2 (2026-10-20): reinstall on plug-in, How it works guide, deck
  check (fw 1.15 + leftovers), sturdier USB reads (fastload flush/retry).
- v0.9.3 (2026-10-20): PERFORM pads install on Windows (font slots sized
  for Arial; PEND 0x081E03A0).
- v0.9.4 (2026-10-21): stems work when opened by double-click (Homebrew
  PATH), in-app Install stem separation, ffmpeg + ffprobe checks.
- v0.9.5 (2026-10-21): BEAT JUMP tab name on Windows (aafont squeezes the
  Arial stand-in to Neue Condensed widths, CONDENSE 0.84).
- v0.9.6 (2026-10-24): Windows parity -- the public repo/CI snapshot was
  STALE (permanent v2 + extras, phase meter module, font fix and deck
  fixes never reached the Windows zip; that one export gap explains the
  "UPD stops after 2 min", "centre colour module not built" and
  AssertionError-2802 reports). Key-shift MT freeze fix (tab inert while
  MT off, MT change resets the key), hot cue TAP pages again (swipe kept),
  phase meter reads POSL 0x0C0E6D1C (the time digits' live source; the
  old word 0x09D246E0 freezes on silicon), HLE advances POSL + adopts
  SH_POS seeks, FLAC & Opus untickable (10 of the 15-20 min), UPD
  presets (phase meter only / waveform colours only), prebuilt/deck
  fallback for source checkouts, Vinyl-brake row + linked-deck row fix.
  Gates: perform_pads emu, phasemeter_test (new, emu_track_loaded +
  synthetic grid), permanent full + pm-only boot tests, repro_freeze
  all-mods play, app smoke.

- v0.9.7 (2026-10-24): hotfix -- deck install crashed at job() (flac
  keyword).
- v0.9.8 (2026-10-25, salvofly's Windows list; RELEASED): mod rows in the
  stock UTILITY list (tools/utility, step 5u; scrolls, so nothing drops on
  linked decks; not baked into permanent UPDs yet); phase meter also on the
  PERFORM screen (pm.c pm_dy -103, phasemeter_test PERFORM leg); MY
  SETTINGS hides [THEME] when theme switching isn't installed OR the popup
  is short (linked deck) so [HOT CUE] gate + [VINYL BRAKE] stay (MSKB);
  BEAT JUMP stale key bit released from rtkeep after 8 flushes with the
  finger up (the hit-tester sometimes stopped before its countdown;
  KQTAB/KSDRAW moved +0x48/+0x20); emulator audio work from 10-24.

## salvofly after 0.9.8 (2026-10-25 night): "only the PERFORM meter fixed"
- 0.9.8 shipped BEFORE the deck-measured phase-meter position fix (it read
  track length 0x0C0E6D1C -> frozen); fixed for 1.0. Linked-deck overlap
  still open on our own deck (needs pm_probe in the linked state).
- LIKELY ROOT of the "only one player" reports: every tool opened the
  FIRST HID device with the XDJ-700 VID/PID, so with two decks on one PC an
  install could split across both. Fixed for 1.0: open_deck pins one deck
  by serial per process (XDJ_DECK_SERIAL selects), refuses when several
  are connected; Studio shows "Two decks are connected" and disables the
  install. Asked him: linked? both on USB while installing? file type for
  the key-shift freeze, D-H cues, what "N" is.

## Open tester reports -- after 0.9.8 (salvofly, 2026-10-25)
- Key shift + MT freeze + "corrupted file": NOT reproduced (emu real DSP:
  MP3 +-2, WAV +2/-6/+6 pitch exact, no DSP error; stock DSP with a tagged
  BD8 word = harmless; no SH reader of 0x09D24DD8 besides the BD8 pusher).
  Asked: file type, USB vs LINK source, steps, exact message. Suspects:
  LINK-loaded track (same re-entrancy family as the LINK report), a format
  we haven't run (AAC/M4A), tempo far from 0 + big shift.
- 3 hot cues only: asked whether tracks have D-H + install log. The
  8-pad grid (docs/HOTCUE8_GRID.md) is now integrated into the hotcue8 module
  (2026-11-07): tap the HOT CUE tab and all eight show at once.
- RGB on one player: asked for MY SETTINGS state + same-version installs.
- "N key toggles Quantize on the BEAT JUMP tab": QUANTIZE = zone 20
  (642,232) handler KEY+91 bit 0x80; our pads end x 615 and the emu shows
  no byte-91 change on any BJ tap. Asked what "N" is.
- Suggestions: vinyl brake picker on the jog (row exists in MY SETTINGS),
  FX (Echo/Reverb/Phaser), deck-side 3-band for unanalysed tracks.

## Open tester reports -- status after 0.9.6
- Phase meter on-deck: 0.9.6 needs one hardware confirm (POSL source +
  the "flickers between ours and stock squares" report; the flush redraws
  ours every frame and the sentinel covers themed borders -- if flicker
  persists, skip the stock box painter, CLAUDE.md sec. 65 lead).
- PERFORM "window switches to the stock square meter": that is the pads
  entering KEY SHIFT/STEMS mode on the PERFORM screen (stock strip there
  is the twin painter; DCAVE2 hooks it only when centre colours are on).
  Confirm on deck with 0.9.6.
- Key shift freeze + hot-cue paging: fixes shipped in 0.9.6 (both
  emulator-gated); confirm on deck.
- Themes "don't work" (one German tester): no log yet; 0.9.6 ships the
  row reorder + [THEME] table wiring -- retest, then ask for the log
  (theme_switch_apply --survey REFUSED is the likely stop).
- "Phase meter and RGB went away after theme settings": the MY SETTINGS
  [WAVEFORM]=BLUE and [MODS]=OFF rows switch them off (app text now says
  so). Not a code bug so far.
- Mac "too new version" (one report, no exact message): the app needs
  macOS 14+; get the exact wording before acting.
- LINK: with mods on one deck, the OTHER deck couldn't load songs from
  this deck's USB over Pro DJ Link (first report). SOLVED TO THE
  MECHANISM (static, 2026-10-24); cause not yet proven:
  * the deck's generic Media API -- read 0x08DDFC96(buf,size,count,h,req),
    seek 0x08DE00D6, tell 0x08DE0364, eof 0x08DE0510, size 0x08DE0E7A --
    dispatches BY DEVICE TYPE; USB = type 3, implemented by the six slots
    the FLAC layer replaces (close 0x08DDFD28, fread_direct 0x08DDFFD4,
    seek 0x08DE0298, tell 0x08DE04FC, eof 0x08DE0768, size 0x08DE0F48)
    plus the media read server's fread pool 0x08D5FFA8 (its only user --
    what versions/stems hook). The server also dispatches type 4 ->
    0x08C95760 (a cached-descriptor reader: lock helper 0x8C65860, cache
    descriptor @handle; IDENTIFY -- plausibly the link/stream reader or
    preview) and type 6 -> 0x08E1D2E0.
  * 24 call sites of the generic read spread over browse/db/waveform/
    loader modules, and the link stack is on the deck by name
    (RemoteDBServer / LocalDBServer / CONTENTLINK tasks; "dbserver" is
    Pioneer's link protocol) -> the link server almost certainly rides
    the same USB slots. Our FLAC vfile sits under EVERY USB file
    consumer incl. link serving; stems under the direct slot; versions
    under the pool.
  * foreign reads are handle-checked and mostly pass through BY DESIGN,
    so the ranked suspects: (1) NO re-entrancy serialisation in vfile/sm
    (one global decode ARENA; the link task entering while the player
    task is inside = state corruption -- built and tested single-
    consumer only), (2) a link-path access pattern one of the six
    wrappers mishandles (5-arg generic read vs 4-arg slots; seek/tell/
    size combos), (3) vredirect's new-handle trailer probe (3 extra
    ops) tripping a dbserver response timeout, (4) innocent: link reads
    take the type-4 reader and bypass all our hooks.
  * NEXT (deck, one session, instrumented -- NO deck-code changes
    before the evidence): rebuild vfile + sm with counters (entries per
    slot fn, foreign-handle rate, passthrough vs virtualised, arena
    re-entry hits), link two decks, peer loads 3 tracks, read the
    counters over USB. Then serialise (busy flag -> straight to the
    chained stock fn, no state touched) or gate by handle owner
    per what fired. Native reentrancy test can extend
    tools/xflac_vfile_test.py (blocking mock + second call).
- FLAC not read on a deck: 0.9.6 install ends with the eject/reinsert
  note (types are classified at mount). Confirm it clears it.
- 3-band "doesn't work" on a user's players (probably no PWV6/PWV7 in .EXT:
  needs Track Builder > Add 3-band); RGB "occasionally bugs out with some
  tracks" (need track names).
- Emulator for an Italian user: every load -> "MEDIA or FILE COLLAPSED" +
  E-8302, colours only with "boot from reset". Needs their log/stick info.
- "Reverted when I played a song" (waiting on details).
- Requests: vinyl speed set with the jog wheel; Echo/Reverb/Phaser FX (DSP
  effect stage, same route as live key shift); waveform preview in BROWSE.

## Next 0.9.x: fixes from the user's deck session
- Install from the DOWNLOADED app on the real deck; check: phase meter,
  8 hot cues (Reddit: "didn't work"), faster stem mute, BEAT JUMP 32/64,
  Opus, new USB reads, reinstall prompt.
- **CONFIRMED 2026-11-06 by the user, on the deck (1.15):** vinyl brake
  length changes the stop as chosen, and a FLAC file lists, loads and plays
  in the deck's own decoder (the 2026-10-25 freeze does not come back). Both
  moved out of the "emulator only" list in README, the site and this file.
  Opus was NOT tested and stays unproven -- it shares the FLAC layer but that
  is an argument, not a result.
- **CONFIRMED 2026-11-06 as well (same Reddit feedback):** 8 hot cues work
  (pages A-C / D-F / G-H, setting one from an empty pad works), and the phase
  meter works, master row included (two decks linked). NEW BEHAVIOUR from the
  same message: "wischen geht nicht, man muss tippen" -- hot cue swiping does
  not page on the deck even though the code path exists; tapping the HOT CUE
  tab is the route that works. **User question 2026-11-06: can swipe be done
  for 1.1?** Yes -- it is not missing code: perform_pads already carries the
  tab swipe unit (PSW: down-x, pending code 52) and tools/hotcue8_test.py
  pages both ways in the emulator. What is missing is a deck diagnosis, so it
  is a 1.1 candidate rather than a promise.
- Fix what breaks; plus Windows tester reports (first full Windows install).
- Open Reddit report: "reverted when I played a song" (waiting on details).

## 0.9.6: emulator audio + OneLibrary converter
- Emulator audio (2026-10-24 session, CLAUDE.md 126): real time is out of
  reach for the cosim -- the SH interpreter sustains ~77 Mcyc/s vs the 500
  needed (JIT-scale project), and the SH is the PCM pump so the DSP can't
  be decoupled. What shipped instead: honest core speedups (+15% cosim,
  all differ-A/B-proven: CS6 word batching, C-side HPIA/HPID reads,
  sync throttling, interpreter commit rework), BURST playback in DspSound
  (sub-real-time hosts get clean quarter-second passages at exact pitch
  instead of the machine gun; automatic smooth mode near 100%), and sound
  on Windows via pygame.mixer (macaudio.make_output; mock-proven, needs
  one audible check on a real Windows wheel). Harnesses: tools/
  dsp_rate_bench.py, dsp_burst_test.py, pygame_output_test.py.
- OneLibrary converter in the apps: tools/onelibrary_to_pdb.py (tests pass
  incl. the user's real stick; needs sqlcipher3 bundled for Mac + Windows),
  Track Builder button "Make this stick readable on the XDJ-700"; check one
  converted stick on the deck (emulator library view works since the
  usbdisk short-name fix).

## 0.9.7: permanent file v2 (beta)
- Bake centre colours, key shift (DSP image patch), stems mixer, hot cues,
  phase meter via the boot copier (built + emulator-tested, private:
  tools/permanent_build.py, tools/permanent_extras_emu.py).
- PREREQ: one real flash of a basic permanent file going fine.

## Deck session 2026-10-25 (results)
- Install from the downloaded 0.9.8 app: works, 30 min (estimate wrong).
- UTILITY rows: WORK on hardware.
- Phase meter: read the wrong word -- 0x0C0E6D1C is the TRACK LENGTH
  (frozen); the live track position is 0x0C0E6D18 (1/150 s, follows the
  tempo; tools/clock_hunt.py at +10% tempo). Fixed + emulator-checked
  (tools/phasemeter_dsp_test.py on the real-audio-chip emulator, which
  agrees with the deck). Draw gate now = background right of the meter ==
  reference (200, blue row) -- the stock meter lights a marker at x 254
  when a LINKED deck is master; MENU popup no longer overdrawn.
- DECK 1 FROZE TWICE (pads + phase meter only) while deck 2 played a
  track loaded FROM DECK 1's STICK over LINK; deck 2 then went to
  emergency loop (its source died). Suspect: mods reading the ANLZ context
  [0x09826BE4] that the link server reuses for the served track. Guards
  added (pointer/length range checks, read-twice) to pm.c, the RGB
  snapshot (rgb_detail COPY5), cw.c snap7 and hc.c find_cue. Bisect the
  same day: pads alone + link = fine; pads + guarded meter + link = NO
  FREEZE (was frozen twice with the unguarded meter) -> the old meter's
  unguarded grid copy is the likely cause.
- Stock meter overlapping ours while a linked deck plays: each stock row
  repaint is its own small flush request (2DG list: one op-0x82 textured
  quad, 216x20 at (258,117|138)+dy). pm_hook now NOPs that one command
  while our meter is visible (call itself unchanged: the caller treats a
  skipped call as an error). Emulator: 154 stripped, 0 reach the screen.
  Needs one deck look. Next deck session: (1) pads only + link test, (2) pads +
  guarded meter + link test, (3) full set + link test.
- Key shift: right-channel noise again on AIFF while shifting (clean at
  0; MP3 was clean on 10-18). Emulator WAV: no L/R difference.
- FLAC: deck froze on LOAD (browse -> push).
- Hot cues: user wants D-H on every track (set them on the deck) ->
  needs our own storage on the stick (same file-open RE as the loader).
- Requests: app redesign (Ready to mod -> choose method: cable install /
  USB-stick file / firmware UPD), light + dark mode (app AND a deck light
  theme), FX tab (mockup screenshots/mockups/fx_tab.png) for 1.1.

## 0.9.9 (user requests during the 10-25 deck session)
- Mods page: plain install boxes only -- drop the single-theme picker +
  "Send to deck" and the waveform style buttons (both are chosen ON the
  deck: UTILITY / MY SETTINGS); one hint line pointing at hold-MENU UTILITY.
- FLAC unticked by default ("only if your library has .flac files"); Opus
  split from FLAC and only offered when the library has .opus files (our
  stems files are WAV/AIFF, they don't need it).
- Install estimate: real FLAC session count + measured s/session, never
  rising; progress bar by expected work, not step count (FLAC showed 37%
  at 2/3 done).
- Faster FLAC upload: compressed transfer (tiny unpacker first), skip the
  empty-memory check once remembered per deck.
- Emulator page: one "Apply mods" button instead of the repeated choices;
  hide Settings > Project folder in the downloadable app.
- "Waveform colours only" UPD preset must tick the centre waveform.

## Next: emulator speed (own branch, user-agreed 2026-10-25; PAUSED)
- Measured 2026-10-25 (wavplay snapshot, M5): 10.9% of real time, SH is
  the bottleneck (54.6 Mcyc/s). SH time by area: 0x08DC4000 27% (centre
  waveform painter + its staging->surface copy loop 0x08DC40B8..EE, one
  pixel per iteration), 0x08E1C000 17% (unknown, check next), 0x08DBC000
  12.5% (overview painter), 0x08DC0000 11% -> over half is software
  waveform drawing.
- Plan: (1) native C fast paths for those few hot loops, entered at the
  loop head, exact registers/memory/step counts (differ A/B must stay
  identical); (2) threaded/pre-decoded SH interpreter (2-3x); (3) JIT
  only if still needed. DSP core is already ~96% of real time.
- Light deck theme: user likes the "ideal" mockup
  (screenshots/mockups/light_theme.png) -> needs the firmware's own text
  colours (track name, time, BPM) found and themed; research item.

## v0.99 RELEASED 2026-10-27 (user: "release 0.99 for now, not everything
## is fixed, everything will be for 1"). Shipped the whole current tree
## (all 1.0 work so far) with a plain "still to come in 1.0" list. Gates:
## public_app_test + tester_pack_test PASS. Windows UPD-build fix: no
## separate commit ever found; Windows zip built fresh by the workflow.

## 0.99 (tiny, fixes only -- user-agreed 2026-10-26)
1. Windows UPD-build fix for the tester (he rebuilt the exe; the user says
   the cause is already fixed in our code). OPEN: no commit for it was
   found in git on 2026-10-26 -- find/commit it before building 0.99.
2. Install talks to one deck only (54f751d).
3. Phase meter reads the live position (0.9.8 shipped it frozen).
Nothing else. Gates (tester_pack_test + public_app_test), Mac + Windows
builds, publish as inter55 (only on the user's go).
News: a tester flashed our permanent UPD (v1.16) on a real player; RGB
overview, beat jump, key shift, stems, extended hot cues all worked.

## 1.0 ("crazy update", user-agreed 2026-10-26) — PREPARED 2026-10-27
Shipped in the 1.0 prep (see CLAUDE.md 144): the four 0.99 Reddit reports
fixed (centre RGB 16-bit count -> blue-revert on >7:17 tracks + rate
scaling for grid alignment; hot-cue paging hardened + A-H export tag in
Track Builder + re-export guidance; stems list sees XST4 files + "bottom
category FOLDER" guidance; key shift MT note in the release notes), light
mode un-held (app Appearance + deck LIGHT theme + 5l step), APP_VERSION
1.0, release notes + Reddit reply drafts (docs/REPLIES_2026-10-27.md).
Still open for 1.0 proper (need the dev's deck): master phase row,
stem mute inside the deck's buffer delay, E-H settable on deck, USB-stick mods
file, TOUCH FX, OneLibrary in-app, track filter, Windows out of beta.
- PERFORM feature pick: the deck now hides the KEY SHIFT / STEMS tabs
  when those mods aren't installed (same FEATB byte in the cable install
  and the permanent UPD); Live Stems has its own tick box in the app.
- Light mode on the deck + in the app.
- Stem mute, measured on hardware 2026-11-03: ~10 s on a partly filled
  buffer, ~30 s on a full one. Our rewrite of the read stream is already
  immediate (4 blocks in one pass), but the deck has streamed that audio into
  the chip, so the change waits for the buffer to drain. Instant mute needs a
  gain change inside the chip DSP (where the TOUCH FX engine runs); the read
  path cannot beat the buffer.
- 8 hot cues settable on the deck (own storage on the stick).
- Phase meter master row (second linked deck): **DONE, proven working on
  two linked decks (user, 2026-11-06).**
- USB-stick install mode: usbloader flashed once via the UPD + a "mods
  pack" builder. One-time flash approved "if u can guarantee it works":
  exhaustive emulator proof, recovery path written down first, careful
  first deck flash with a boot test. Design + space math now in
  docs/USBLOADER.md.
- The new 3-mode app (cable / USB stick / firmware UPD).
- Bug list proven on the deck: FLAC load freeze, key shift right-channel
  noise on AIFF, BJ 32/64, lit-button theme stripes (+ light mode, 8 hot
  cues confirmed on hardware).
- Faster emulator (nice-to-have, not a blocker; work in progress, see
  docs/EMU_REALTIME.md).
- TOUCH FX tab on PERFORM (design APPROVED; mockup
  screenshots/mockups/touch_fx_deck.png, 800x480): new tab in the left
  column under PERFORM; big X/Y pad with crosshair + soft glow under the
  finger (NO animated backgrounds -- heavy redraws froze the deck before);
  value + ring far left; right side 2x4 FX grid, DEPTH slider, FX ON, HOLD
  (lift finger = off unless HOLD); colours follow the deck theme.
  8 effects (pad X / Y): Filter (cutoff/resonance), Echo (time/feedback,
  needs a DSP buffer), Pitch (semitones/mix, reuses key shift), Crush
  (bits/rate), Gate (beat division/depth), Flanger (rate/depth), Roll
  (beat length/mix, maybe the deck's loop engine), Reverb (size/mix,
  hardest; may slip to 1.1 with the screen unchanged). The XDJ-700 has
  NO built-in FX: every effect is new C6x DSP code in the key-shift style
  (ks.c hook, hot install). Order easy -> hard; prove each in the
  real-DSP emulator (dsp_pitch_probe-style A/B) before any deck test.
  STATUS 2026-11-01: BUILT, EMULATOR-PROVEN, not yet on a deck. Filter,
  Crush, Gate run on the deck's real DSP program in the emulator (A/B on
  the audio output; tempo/position untouched), and a finger on the pad
  changes the sound end to end (tools/touchfx_e2e.py). Full-screen layout
  per the user (2026-11-01): the whole PERFORM area, player strip and the
  PERFORM button line stay live. Needs MASTER TEMPO ON. Shares the audio
  chip's hook with Live Key Shift: one or the other until a bigger DSP
  home is proven. Install: deck_session --touchfx / Studio "Touch FX
  (beta)". Echo / Pitch / Flanger / Roll / Reverb buttons are drawn but
  inert ("COMING SOON"). CLAUDE.md sec. 148.
- Waveform colours that follow the theme (deck side).
- ~~Windows out of beta~~ **DONE 2026-11-06**: same source as the Mac app, the
  same checks on every build, and real-deck installs from Windows; issue #1's
  PERFORM pads `AssertionError: 2802` (a system-font size difference) was
  fixed in 0.9.6 and its follow-up crash in 0.9.7.
- OneLibrary converter (exportLibrary.db -> export.pdb).
- MAYBE: track filter (lead DISPROVEN 2026-11-04, see the research note
  under "1.1 and later"), custom home screen (if possible).
- Refreshed site + README.

## 1.1 and later (ideas)

**The 1.1 plan lives in `docs/V1_1_PLAN.md` (started 2026-11-06).** It organises
1.1 into four waves: ship what is already emulator-passed (Echo, Flanger, faster
stem mute, Opus, saving hot cues, stems replace, hot cue swipe, filter
playlists, 8-pad grid, light theme), finish Touch FX (Pitch, Roll, Reverb if it
fits), one real USB-stick-loader flash with a written recovery path, then the
research items below (track filter, RGB over Link, theme-matched waveform,
faster emulator). Each feature needs an emulator gate, a dated deck result,
matching wording in README/site/Reddit, and a release-note line.
Priority (2026-11-04, from the user): **track filter first** -- the most-
requested item -- then the rest of Touch FX in the roadmap's own order:
Echo, Pitch, Flanger, Roll, Reverb.
- **Echo memory, measured twice 2026-11-05 (emulator, no deck):** the
  survey's "~101 KB free at 0xC0045900" is WRONG -- 97,308 of those 105,408
  bytes are in use (`tools/dsp_map.py`). The first replacement it found,
  `0xC0062000` (66,562 B), is ALSO in use: with MASTER TEMPO on it fills
  with the audio path's own samples (2,059 non-zero words in its first
  16 KB with our effect OFF). The block that stays byte-zero through
  playback is `0xC0073400..0xC0080000` = 52,224 B. Flanger (8.8 KB), Echo
  (147 ms max) and Roll (44 KB) fit there; Reverb (50-65 KB) does not.
  Left: one deck read confirming that range is zero on hardware -- DONE
  2026-11-05: it is NOT zero. The deck holds data through the whole block
  (13,056/13,056 words) and 127 of 128 pages of 0xC0000000..+2 MB; only
  0xC006C000..0xC0070000 is empty. Echo must not use that buffer. See
  docs/TOUCHFX_MEMORY_SURVEY.md ("Deck result 2026-11-05").
- **Echo BUILT 2026-11-05, emulator-passed:** effect 3 in
  `tools/fx_dsp/fx.c` (delay 20..147 ms, feedback, DEPTH), line at
  `0xC0073400`, panel button live in `tools/touchfx/tfx.c` (4 of 8).
  Gate: `EFF=echo X=40 Y=120 DEPTH=255 tools/fx_dsp_emu.py` -> delayed copy
  at 1763 frames exactly (corr 0.92), 2nd repeat 0.43 vs feedback 0.35,
  play position unchanged (317 -> 317 blocks). Not yet on a deck.
- **Flanger BUILT 2026-11-05, emulator-passed:** effect 4, same ring as
  Echo (base 3 ms swept 0..2 ms by a triangle LFO, X = rate 0.05..4 Hz,
  Y = depth, DEPTH = mix, linear interpolation so it does not zip), 5 of 8
  panel buttons live. Gate: `EFF=flanger X=60 Y=200 DEPTH=255` -> best lag
  per quarter 174/193/176/152 frames inside the 132..220 window (41-frame
  sweep), corr 0.86..0.89, play position unchanged. Not yet on a deck.
- **Engine window is 2,496 B, and it is tight.** Code + state must end by
  `0xC0060000` (the key-shift cos table). A first Flanger build made the
  binary 2,624 B, the state crossed the table and the audio path died after
  44 blocks -- `tools/fx_dsp/build.sh` now REFUSES over-budget builds. The FX
  installer zeroes `ZSTATE=0x180` (struct is 0x148); key shift keeps the
  stock 0x260. Echo and Flanger share one delay routine to stay inside it.
- **Track filter research, 2026-11-04: the RBFLTR.DAT lead is dead.** The
  decoded stock image (18,601,864 bytes) contains zero occurrences of
  `FILTER`, `FLTR` or `RBFLTR`, so the deck never had a filter reader to
  hook. What it does carry is the family-wide DeviceSQL table layer
  (`djdbBPMRange`, `djdbSortMenu`, `djdbTrackSort`) and its RAM hash caches
  (`INDEX:SHashSortMenu`, `INDEX:SHashBPMRange`) -- the schema every
  CDJ/XDJ shares, not a 700 filter UI. The two "RANGE" strings in the image
  are TEMPO RANGE, not a browse filter. So an on-deck filter has to be
  built into the browse menu.
  **Follow-up, same day, against the XDJ-1000MK2 image
  (`XDJ-1000/MAIN1000.img`, 29,275,684 B, base 0x08000000): the 1000 has the
  filter and the 700 does not, and the two decks are close enough to port
  between.** `FILTER` 68 hits vs 0, `BPM RANGE` 1 vs 0, `MY TAG` 2 vs 0, and
  the browse log tags `@Ui_Browse> DISPCMD FILTER CHANGE APPLY`,
  `FILTER CATEGORY STATE CHANGE`, `FILTER PARAMETER EDIT START`,
  `FILTER INPUT CHAR CHANGE` -- all 0 in the 700. The browse module itself
  is the same lineage (`@Ui_Browse` 59 vs 104, `DISPCMD SELECT_MENU` in
  both) and the shared plumbing is referenced identically in both images
  (`INDEX:SHashBPMRange` 1 P2 literal each, `djdbBPMRange` 2 each), so the
  700 already carries the BPM-range data layer and only lacks the filter
  commands and their screens. So this is a port, not a from-scratch build --
  with the pm.c precedent for HOW: read the 1000's path, write our own on
  the 700's plumbing; we do not transplant Pioneer bytes (copyright, and
  their code is bound to their own screen tables and absolute addresses).
  First step: xref the `DISPCMD FILTER` tags in MAIN1000.img to find the three
  routines -- category state, parameter edit, apply -- then mirror them.
  `tools/filter_scan.py` now indexes all 15 tags (0 in the 700 image, which
  confirms the gap); `docs/TRACK_FILTER_1.1.md` records where that stops: the
  1000 image has no absolute pointers to those strings and its block framing
  is not the 700's, so the load base has to be found before the routines can
  be named.
  Fastest route to the same user outcome (no deck work at all): write
  filtered playlists
  (BPM band, key, colour, rating, my-tag) into `export.pdb` -- the writer in
  `tools/onelibrary_to_pdb.py` already carries playlists, BPM, key, colour,
  rating and my-tag ids, and the deck shows them natively under PLAYLIST.
  **BUILT 2026-11-05: `tools/filter_playlists.py`** -- BPM band / key /
  colour / genre / minimum rating / text search, optional folder and sort,
  surgical append to the playlist tables, backup + read-back verification.
  Test: `tools/filter_playlists_test.py` (20 checks, synthetic library).
  `--presets` writes the whole folder in one pass -- BPM thirds worked out
  from the library itself, 4+/5 stars, the keys with 3+ tracks -- and is
  idempotent on a re-run; Studio has it as "Make the whole set".
  OneLibrary sticks go through `onelibrary_to_pdb.py` first (the tool says
  so). My-tag filters wait for an `exportExt.pdb` writer.
- **MP3 / M4A / FLAC stems replace the track in place (1.1): works.**
  The blocker was never the audio bytes -- it is that the deck finds the
  analysis (cues, grid, waveform) by hashing the *audio path* into
  `/PIONEER/USBANLZ/P{XXX}/{YYYYYYYY}`, so a renamed file silently loses it.
  That hash is solved and checked against the three folders rekordbox itself
  wrote for the tracks on the dev stick (3/3):
  `h = 0; for c in path: h = (h*0x5BC9 + c) & 0xFFFFFFFF; h = (h*0x93B5 + c)
  & 0xFFFFFFFF`, then `r = h % 200003`, `XXX` = seven scattered bits of `r`
  (`r>>0, >>1, >>4, >>4, >>5, >>8, >>10` against masks 1,2,4,8,16,32,64) and
  `YYYYYYYY = r`. Recovered from `CreateAnlzFileFolderPath()` (shared by
  morizkraemer/fourfour's pioneer-usb-writer notes).
  `tools/stems_replace.py` now does the whole swap on a stick: builds the
  STEMS-4 WAV, byte-patches the PDB track row (filename, file_path,
  analyze_path, file_type, size, rate, bitrate, depth, duration -- every
  string is the same length, so no row moves and no page bookkeeping), rewrites
  each ANLZ file's PPTH tag (UTF-16BE **plus a mandatory NUL**; without it the
  deck finds the file, rejects it and re-analyses into `ANLZ0001.DAT`), moves
  the folder to the new hash, keeps the original as `.pre-stems` and backs the
  PDB up to `export.pdb.bak-pre-stems`. `selftest` passes end to end on a copy
  of the dev stick (row says `.wav`, file_type 11, analysis folder moved, PPTH
  matches, cue container untouched, original kept).
  Wired in: Make stems replaces MP3 / M4A / FLAC in place -- MP3 and M4A
  become WAV, FLAC becomes AIFF (both keep the name's length, which the
  library row needs). The WAV / AIFF path is
  untouched, and a switch-off still writes the sidecar). Still open: play a
  replaced MP3 on a real deck and confirm waveform + hot cues (the only thing
  software cannot prove), and decide what to do about `exportLibrary.db`
  (OneLibrary sticks) -- the 700 reads `export.pdb`, so the tool refuses those
  unless told otherwise.
- **RGB waveform over Pro DJ Link (Reddit request 2026-11-06: "RGB waveform
  uber pro link ware eine gute Verbesserung").** Today the colours only draw
  on the deck holding the USB stick; a deck that loads the track over Link
  never sees the ANLZ data the RGB modules read, so it stays stock blue. This
  is the same media-API dispatch the LINK re-entrancy work above maps (type 4
  reader 0x08C95760 is the likely linked-track path), so it is a follow-on to
  that investigation, not a separate project. Colour data would have to be
  served to the peer over the link, or the peer's own copy used.
- **Hot cue paging by swipe: 1.1 candidate** (user, 2026-11-06). CHANGED in
  the emulator the same day: the tab's swipe gate in perform_pads HCSW was
  hard-coded at 40 px while the strip is only TAB_W=92 px wide, so a drag
  rarely fit inside it -- now 24 px (cmp/gt, so 24 exactly does not fire).
  tools/hotcue8_test.py gained two checks at the end of its run: a 32-px drag
  pages in both directions and an 8-px wobble still counts as a TAP -- PASS,
  and every older paging/tap/wrap leg still passes. Still unproven: the deck,
  where the other suspect is that the touch leaves the tab zone mid-drag and
  the move events stop (tools/bj_monitor.py prints latch / pending code /
  swiped / downx live for that). UI only -- no DSP, no new memory, nothing
  flashed; rollback copy of the pre-swipe file is perform_pads.py.pre-hcswipe.
- Track filter (if it missed 1.0).
- Waveform preview next to track names in BROWSE (Reddit request; data is
  on the stick). Audio preview in the browser: unlikely (one playback path).
- Link over USB, more hot cue/loop pages. Reverb if it slips from 1.0.

## Not planned
- StreamingDirectPlay (partner logins/DRM). Other decks: XDJ-700 only.

## Community asks (2026-10-31, from the public thread)
- Track filter/browse system (XDJ-1000 MK2 / CDJ-2000 NXS2 style): 1.1
  candidate, big browse-engine RE; the community's most-wanted item after
  the mod set itself.
- Hide KEY SHIFT / STEMS tabs even when installed (more room for HOT CUE +
  BEAT JUMP): cheap candidate; a hide flag in the same FEATB/utility table
  the installer writes.
- TOUCH FX: already v1 scope (see APPROVED TOUCH FX section).
- 8 hot cue report ("many set, don't work"): verify on deck 2026-10-31;
  failure with an exported analysis = bug, capture the stick.
