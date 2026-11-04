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
  8-pad grid prototype (docs/HOTCUE8_GRID.md) = "display all 8" if wanted.
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
  vinyl brake length, FLAC/Opus, new USB reads, reinstall prompt.
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

## 1.0 ("crazy update", user-agreed 2026-10-26)
- Light mode on the deck + in the app.
- Near-instant stem mute (well under 0.5 s): rewrite the DSP's cached
  decoded blocks at the tap, not at the next read burst (~5 s today).
- 8 hot cues settable on the deck (own storage on the stick).
- Phase meter master row (second linked deck).
- USB-stick install mode: usbloader flashed once via the UPD + a "mods
  pack" builder. One-time flash approved "if u can guarantee it works":
  exhaustive emulator proof, recovery path written down first, careful
  first deck flash with a boot test.
  2026-11-04: loader cache bug fixed (the after-read pass was ocbi, which
  drops a CPU-copied payload on the deck's copy-back cache; now ocbp, and
  build.sh fails on any ocbi). Built with tools/build_sh_gcc.sh (GCC
  14.2.0); tools/usbloader_test.py good/badsum/nofile PASS on the shipped
  main-screen snapshot. The emulator has no cache model, so the cache fix
  itself is checked by disassembly, not by the emulator run.
  Stage-2 (same day): MODS.BIN = tools/usbloader/s2.c, a 1.4 KB installer
  that streams PIONEER/MODS/PACK.BIN (tools/usbpack.py: the UPD baker's
  installers + theme switching run against a recording memory; records =
  surveyed zero homes, data runs, lit pictures as expected-sum writes, arm
  words LAST with the stock value each must still hold) straight into RAM
  through the P2 alias, purging lines before/after, I-cache invalidated
  before the arms; the arms are written only after the whole file summed
  right. tools/usbpack_test.py: full pack (overview, versions, pads,
  centre, phase meter, 8 hot cues, stems, UTILITY, 8 themes + 1 lit) on
  the emulated deck; PERFORM + theme push work live. Not yet: key shift
  (needs the hot DSP patch), FLAC/Opus (a second file, same mechanism), the
  pack builder in the Studio, the RAM-only rehearsal on the deck, the UPD
  with the loader as a permanent_build extra.
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
- Waveform colours that follow the theme (deck side).
- Windows out of beta (after a successful Windows install by a person).
- OneLibrary converter (exportLibrary.db -> export.pdb).
- MAYBE: track filter (only if a research pass says doable; lead: the deck
  reads PIONEER/rekordbox/RBFLTR.DAT), custom home screen (if possible).
- Refreshed site + README.

## 1.1 and later (ideas)
- Track filter (if it missed 1.0).
- Waveform preview next to track names in BROWSE (Reddit request; data is
  on the stick). Audio preview in the browser: unlikely (one playback path).
- Link over USB, more hot cue/loop pages. Reverb if it slips from 1.0.

## Not planned
- StreamingDirectPlay (partner logins/DRM). Other decks: XDJ-700 only.

## Colour mods (user ideas, 2026-11-04)
Order agreed: lit-button colour first, then button borders/frames.
- **Lit-button colour** (DONE 2026-11-04 in preview, emulator and the deck
  theme switcher; DECK TEST PENDING). mods/litbuttons.py holds the
  addresses and the one colour rule used by all three. Lit touch buttons
  are 72x53 RGB565 sprites, 4 per button (lit, lit-pressed, unlit,
  unlit-pressed): QUANTIZE 0x0826AAF0.. (red), MASTER 0x088647E0/0x088665B0
  (orange), SLIP 0x0886BF20/0x0886DCF0 (blue), VINYL BRAKE
  0x0887E940/0x08880710 (blue) + brake 0x088824E0/0x088842B0 (red); SYNC
  lit is neutral grey. ts.c maps colours with ONE global table and the lit
  pictures share colours with other art, so no ts.c change: the installer
  moves the lit pictures' shared colours to private ones 1 LSB away
  (theme_switch.lit_private, 502 colours, 28 k px) and the tables map them.
  The lit maths rolls off saturation/brightness instead of clipping (the
  one-to-one tables cannot merge colours; clipped white speckled pink and
  green): white and grey read silver.
- Button borders / frames (grey chrome of the touch buttons): pictures,
  same method.
- Cue / loop / hot-cue marker colours: probably colour tables; research.
- Text colours (track name, time, BPM) and background tint: drawn by code
  (2DG fills carry a colour word) -- research item, also needed for the
  light theme.
- **Own picture instead of the big rekordbox logo** (no-track splash on
  the player screen): it is a blitted picture, so a same-size RGB565
  replacement written to RAM by the installer (like the theme switcher
  writes pixels) looks possible; size, transparency key 0xF81F and the
  fingerprint safety check to be measured first.
- Physical button LEDs: single-colour LEDs, colour cannot change (only
  on / off / blink).
