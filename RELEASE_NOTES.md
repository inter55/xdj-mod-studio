# XDJ Mod Studio v1.0.6

Deck code: install the mods again after updating (cable, USB-stick file or
a flash) for this to reach your deck.

- **Hot cues no longer jump to another track's cue.** With a track loaded,
  a hot cue could play a DIFFERENT track's cue at the same letter (C on
  track Y jumps to track X's C), and some tracks seemed to "dominate". The
  deck keeps one buffer for a stick's track analysis, and BROWSE fills it
  with whatever track the cursor is on when it shows that track's details.
  The PERFORM hot-cue pages and the 8-pad view re-read that buffer every
  time you tapped HOT CUE or changed page, so after browsing they could copy
  the browsed track's A–C onto your pads. The "dominant" track was simply
  the one the cursor last stopped on. That is also why the pads could show
  A–C lit while the waveform preview only had B.
  Now the pads keep the deck's own A–C exactly as the deck loaded them, and
  take D–H from that buffer once per track, only after checking it belongs to
  the loaded track. Proven in the emulator by overwriting the buffer the way
  browsing does (`tools/hotcue8_browse_test.py`): 1.0.5 fails it exactly as
  reported, 1.0.6 passes. Not yet confirmed on a real deck.
- If the deck's own A–C never match that buffer for a track (for example
  with the stock setting that carries the previous track's hot cues over),
  that track's D–H pads stay empty rather than playing a wrong cue.
- **Live Key Shift in the USB-stick mods file on Windows** (GitHub #11):
  not fixed yet. The recording stops at the key-shift step on one Windows
  PC and the same recording passes on a Mac. The installer now waits longer
  on a slow PC and reports how long the step took, so the next log can show
  why. Until then, untick Live Key Shift for the stick file on Windows.
- Not in this release: D–H markers on the waveform preview (the deck draws
  those itself and only knows three hot cues).

# XDJ Mod Studio v1.0.5

This one carries deck code, so the phase-meter fix only reaches a deck after
you install again (cable session, USB-stick pack, or a flash).

- **Phase meter, Type 1: the bottom row works on a linked deck.** On the
  second player of a Link pair it drew the orange MASTER CLOCK row and left
  its own blue beat row a flat line. The meter asked the deck for the beat
  grid once, when the track's overview appeared; a track served over Pro DJ
  Link gets its analysis later than that, so the single look found nothing
  and the row stayed empty for the whole track (the master row kept working
  because it only needs the Link bars and the clock, which is why the two
  rows disagreed). It now asks again until a grid exists. Proven in the
  emulator both ways: the old one-shot code reads 0 beats before and 0 after
  the grid lands; the fixed code reads 0 then 1000. Not yet confirmed on a
  real deck.
- **Hot cues D–H from a real rekordbox export** (GitHub #6). On 1.0.4 the
  8-pad view showed D–H empty, and on pages 2/3 the pads played A–C again.
  The cause is most likely the export layout: the nexus 2 players added an
  extended cue list (in the `.EXT` file) because the old list in the `.DAT`
  file was made for three hot cues, so D–H can sit only in the extended
  one. That matches the report exactly, but no real export with D–H was
  available to check. The deck loads both lists into memory; our pads only
  looked in the old one. They now fall back to the extended list. Our
  emulator test had put all eight cues into the `.DAT` file, which is why it
  passed. Proven in the emulator with a rekordbox-style track (A–C in `.DAT`,
  A–H in `.EXT`): the 1.0.4 code fails it exactly as reported (D plays cue
  A), the fixed code passes, and the old all-in-`.DAT` layout still passes.
  Lexicon and VirtualDJ exports should behave the same if they follow
  rekordbox's layout; not checked. Not yet confirmed on a real deck. Deck
  code: install the mods again.
- **Track Builder's hot-cue editor no longer deletes D–H.** It read cues
  from the old list only, then rewrote both lists from what it read, so
  setting or clearing any cue on a real export wiped D–H (and B, C) from the
  `.EXT` file. It now reads both, and it keeps rekordbox's own entries (cue
  colours, comments) for every cue you do not change. The **A–H** tag in the
  track list counts the extended list too, so it now shows on real exports.
- **The loader update takes the version number you type.** The one-time
  loader update for the USB-stick method was stamped 1.16 whatever the
  field said, so on a deck already reporting 1.16 the updater had nothing
  newer to install: the loader never landed, and the stick's
  `XDJMODS/PACK.BIN` was then never read. It now uses the Version number
  field like the full build. The deck only accepts a number higher than the
  one it shows; stock is 1.15 and every modded flash so far reports 1.16.
- **Windows: writing the mods file to a USB stick works** (GitHub issue #9).
  It stopped at about 2 % with `module 'socket' has no attribute
  'AF_UNIX'`: the app records the install on an emulated deck and talked to
  it over a Unix socket, which Python on Windows does not have. On Windows
  it now uses a local loopback connection instead (nothing outside your PC
  can reach it); macOS is unchanged. The Windows path was checked on a Mac
  by forcing it: the file it records leaves exactly the same bytes in the
  deck as the normal one (the only differences are 4-byte self-test writes
  of zero into memory that is already zero).
- **Windows: the emulator starts on more PCs** (GitHub issue #10, QEMU exit
  code 3221225501 with both firmware choices). That code is "illegal
  instruction": the Windows build compiled the emulated audio chip for the
  build server's own processor, so it used instructions many desktop CPUs
  do not have. It is now built for any x86-64 PC from roughly 2009 on.
  This fix is in the Windows build only and has not been run on the
  reporter's PC yet.
- **USB-stick loader: reads the mods file safely on the real deck.** After
  every 2 KB read from the stick the loader threw away the processor's
  cache lines for that buffer. If the deck's file system copies part of a
  read with the CPU instead of by DMA, that discarded the data it had just
  read, and the file looked damaged ("refused") or stopped part-way. The
  emulator has no such cache, so every test passed regardless. The loader
  now reads the stick exactly like live stems does, which is proven on a
  real deck. **The loader is in the flash, so this needs the loader update
  built again from 1.0.5** (or the loader sent over the cable again).
- **`ld_probe.py` reports a flashed loader correctly.** It looked up the
  loader's state at the address of the copy this computer last built, and
  checked the wrong word to decide whether the loader was armed, so on a deck
  carrying an older loader it could say "never ran" or "not armed" when both
  were fine. It now reads both from the loader on the deck, and says which
  of "damaged file", "not a mods file" or "deck not stock" stopped it.
- **Windows: UTILITY › PHASE METER TYPE writes to the right place again.**
  The UTILITY rows carry the phase meter's memory addresses, and the Windows
  copy had not been rebuilt since 1.0 while the meter moved in 1.0.4, so that
  row changed a byte inside the meter's working area instead of its mode.
  1.0.5 ships UTILITY rebuilt against its own meter, the same bytes on Mac
  and Windows.
- **Settings still reset when the deck is switched off** (unchanged):
  `[MODS]`, `[WAVEFORM]`, `[THEME]`, `[HOT CUE]` and `[VINYL BRAKE]` are live
  switches in deck memory. Bake the defaults you want into the flash, or set
  them again after power-on.
- Also here: packaging refuses a deck module that is older than its source
  (one had silently shipped stale), and the phase-meter installer refuses to
  run if any of its globals would sit outside the memory region it reserves.

# Confirmed on a real deck (2026-11-06)

No update needed for any of this — it is the build you already have, now
checked on hardware:

- **8 hot cues work:** pages A–C / D–F / G–H page correctly, and a cue can be
  set from an empty pad while the track plays. Page by **tapping** the HOT CUE
  tab; swiping across the pads does not page there (swiping is for BEAT JUMP).
- **Phase meter works:** it follows the playing track, and the master row
  shows on a second linked deck.
- **Vinyl brake length and FLAC playback** were confirmed the same day (the
  brake really does change the stop, and a FLAC file lists, loads and plays).

# XDJ Mod Studio v1.0.4

Nothing needs re-flashing. The phase meter and hot cue fixes are deck code:
install the mods again (cable or USB stick) to get them.

- **Make stems works again on MP3, M4A and FLAC tracks.** Since 1.0.2 it
  separated the track and then failed with "stick has exportLibrary.db".
  rekordbox 7.2.11 and later writes that second library database
  (OneLibrary) next to `export.pdb`, so almost every recent stick has one,
  and the app refused to change a stick it couldn't fully update. It now
  updates the track in both databases (path, file name, size, type, audio
  details and analysis folder), so the stems version shows in ARTIST /
  TRACK on the XDJ-700 and in anything that reads OneLibrary. The original
  file stays on the stick as `.pre-stems`, and both databases keep a
  `.bak-pre-stems` copy from before the first replacement. WAV and AIFF
  tracks were not affected.
- If something goes wrong partway, both databases, the analysis folder and
  the audio file are put back exactly as they were.
- **Phase meter on linked decks: the upper (master) row draws again.**
  On the deck that is not the master, only the lower row showed. The meter
  only drew the master row when both decks' beat counters agreed, and on
  that deck they never do. It now follows each deck's beat on its own.
  Reinstall the mods to get it (this one is deck code, not just the app).
- **Hot cues D–H play D–H.** On pages 2 and 3, pressing D played A (E
  played B, and so on). The pads play from two tables inside the deck that
  paging didn't update; it does now. The paged pads also no longer show
  half of the old letter and loop icon underneath. Reinstall the mods to
  get it.
- **Windows: the Choose buttons open a folder or file dialog.** Before,
  they only asked you to type the path.

Checked in the emulator; the two deck fixes have not been tried on a real
deck yet.

# XDJ Mod Studio v1.0.3 (app update)

App-only: nothing on the deck changes, nothing needs re-flashing.

- **Windows: the emulator starts and the cable install gets past "Perform
  Pads" again.** Both could stop with "index 0 is out of bounds for axis 0
  with size 0" (with or without mods ticked). First-run setup could save a
  PERFORM screen with no pad numbers on it, and building the PERFORM pad
  font from that screen crashed. The font now falls back to a system font
  in that case, so you don't need to run setup again, and setup now taps
  PERFORM again until the pads are really on screen.
- **Stems wording corrected:** BROWSE › FOLDER does open on a real deck, but
  it doesn't show the `Contents` folder where a stems copy sits, so the
  advice is unchanged: let **Make stems** replace the track.

# XDJ Mod Studio v1.0.2 (app update)

App-only: nothing on the deck changes, nothing needs re-flashing.

- **Stems replacement actually runs now.** 1.0.1 shipped the code that
  replaces an MP3, M4A or FLAC track with its stems version (keeping the
  library row, cues and waveform) but not the database package it imports,
  so that step stopped with an error. The package is in the app bundle now,
  and the build refuses to finish if it is missing again.
- **"Make the whole set" and the OneLibrary converter** used the same
  package, so they hit the same error in 1.0.1. Both are fixed by the same
  change.
- **BROWSE › FOLDER: corrected on hardware.** The knob does open FOLDER on a
  real deck (checked 2026-11-07) and lists the folders at the root of the
  stick; our emulator said it did not, and the emulator was wrong. It does
  not list `Contents`, where the tracks — and any `Title (STEMS).wav` beside
  them — live, and SEARCH searches the rekordbox database only, so a file
  that exists only beside a track still cannot be found there. Every place
  in the app tells you that now.
- **Honest limit:** replacing an MP3 in place keeps the cues and waveform in
  the database, but nobody has played a replaced MP3 on a deck yet. The
  original is kept as `…pre-stems`, so undo is a rename — check the track
  once before you delete that backup.

# For 1.1 (not released yet)

- **All eight hot cues on one screen.** In PERFORM, tap the HOT CUE tab and the
  stock A B C REC CALL row becomes eight pads A–H: set cues get a green strip,
  empty ones stay dark, and holding a pad plays that cue. Tap again for the
  stock row; swiping the tab still pages A-C / D-F / G-H (which is how D–H get
  set on the deck). Emulator-passed (`tools/hotcue8_test.py`, which presses E and
  the pad over the old REC spot and checks the borrowed slot is put back); it
  has not been run on a deck yet.

- **The 1.0.1 macOS download could refuse to open, or be called "damaged".**
  The cause was the archive, not the app: it was made with `zip -r`, which
  stored 2,468 macOS AppleDouble sidecars (`._Info.plist`, `._Contents`, …)
  inside the app bundle. The code signature covers the bundle's file list, so
  those extra files break the seal — `codesign --verify` says "a sealed
  resource is missing or invalid", and macOS shows that to the user as a
  damaged app. Proven both ways: deleting only the sidecars makes the
  untouched app verify, and re-adding them breaks it again.

  App zips are now built by `tools/make_app_zip.sh`, which strips the
  sidecars, re-seals the signature, zips with `ditto` (no HFS metadata) and
  then proves the result: it extracts the archive fresh, verifies the seal
  strictly, rejects any sidecar that came back and refuses if a single file
  is missing. Its `--verify` mode checks an existing archive: it rejects the
  1.0.1 and 1.0 zips as shipped, passes the re-cut one, and catches a
  deliberately damaged bundle (one file deleted) with the seal check.

  **Correction, 2026-11-07: the re-cut zip must not be published.** It was
  built from the copy in `dist/`, which turned out to be an OLDER 1.0.1 build
  than the one users downloaded (`studio/app.pyc` dated Nov 4 22:14, without
  `tools/stems_replace`), not a newer one. The signature/sidecar proof above
  still holds, but the artefact was not the shipped app, and it lacks the
  stems replacement. A 1.0.2 archive has to come from a fresh build, and both
  `tools/build_studio.sh` and `tools/make_app_zip.sh` now refuse a bundle that
  is missing files the app loads at runtime (`tools/app_bundle_check.py`).

# XDJ Mod Studio v1.0.1 (app update)

App-only: nothing on the deck changes and nothing needs re-flashing.

- **Back to stock.** Advanced now has a `Back to stock (no mods)` build. It
  makes a stock firmware file numbered higher than what the deck reports, so
  the deck accepts it as an update. Until now the advice was "flash the
  official 1.15 file", and a deck carrying a modded file can refuse that as
  an older version.
- **Choose the update-file version number.** The Advanced page takes the
  version the file will report, and refuses anything lower than 1.15.
- **Touch FX in the update file.** Pick Key Shift or Touch FX when baking
  (they share one slot in the audio chip). Touch FX is the biggest extra, so
  the build needs the phase meter or 8 hot cues left out; the app now says
  that instead of just failing.
- Corrected wording about going back to stock in the app, the READ ME that
  ships with the update file, and these notes.
- The Windows read-me now states that emulator sound works there too.

# XDJ Mod Studio v1.0 (XDJ-700, firmware 1.15)

Open-source mods for the Pioneer DJ XDJ-700, loaded into the deck's memory
over USB. **Recommended: flash the small stick loader once**, then carry the
mods on your normal rekordbox stick — the deck reads XDJMODS/PACK.BIN at
every mount, and a stick without it leaves the deck stock. That update changes
9 bytes of Pioneer's file and boots correctly in the emulator with and without
a pack, but it has **not** been through a real deck flash yet: keep the
official 1.15 file to hand before you start — it can go back on the same stick
afterwards, so one stick is enough. You do not have to flash
at all to try this: the app can send the same loader into the deck's memory
over the cable in a few seconds, and the stick then works until you power the
deck off. After that one update the deck never needs the computer again. If
you want zero risk,
use the cable instead — nothing is written, and **switch the deck off and on
and it is stock again.** You need your own official XDJ-700 v1.15 update file
(from Pioneer DJ's support site); the app checks it and builds everything from
it. Every route that writes firmware shows 1.16 in UTILITY. To go back to
stock use the app's **Back to stock** build, which gives the deck a stock
file numbered higher than it reports; Pioneer's own 1.15 file can be
refused as a downgrade, so keep it but do not rely on it.

Apple Silicon Mac, macOS 14 or newer (Windows build: see the README inside
the Windows zip). First launch: right-click the app → Open.

## What changed in 1.0
- **The emulator's sound no longer speeds up and drags.** The emulated audio
  chip was being told to run at a clock faster than the Mac could deliver, so
  it fell seconds behind and then dumped the backlog in one burst — heard as
  a sudden speed-up, then a crawl, then a stutter. It now drops that clock to
  what the machine can actually deliver and climbs back when there is spare
  capacity, and the audio output is paced by the deck's own clock, so pitch
  and tempo do not change. Measured with a track playing on an M5: no gaps,
  no dropped frames, pitch correction sitting at zero.
- **Mods can come off your USB stick.** Two ways: send the loader into the
  deck's memory over the cable (a few seconds, nothing written, gone at
  power-off), or build a loader-only update file that the deck keeps. Either
  way the loader replays `XDJMODS/PACK.BIN` at every stick mount. A stick
  with no pack leaves the deck stock, so one stick can carry the mods for
  several decks and a stock stick is still a stock deck.
- **Centre RGB waveform fixed on longer tracks.** The colour data's entry
  count was read as 16 bits, so on any track past 7 min 17 s the count
  wrapped and the centre waveform fell back to stock blue part-way through
  the track (jumping back to a cue made it RGB again — that was this bug).
  Waveform exports that use a colour rate other than 150 entries/s are now
  scaled to the deck's grid instead of drawing off the beat.
- **Hot cue pages D–F / G–H.** Paging now also reads letter-coded cue
  entries and larger cue lists (newer rekordbox exports), and Track
  Builder shows an **A–H** tag on every track whose exported analysis
  carries 4+ hot cues. If the pages don't appear on a track, its analysis
  on the stick predates the cues: set them in rekordbox and export the
  stick again — cues set after an export never reach the deck.
- **Pick only the PERFORM features you use.** Live Stems now has its own
  tick box in the app, and the deck's PERFORM screen only draws the KEY
  SHIFT and STEMS tabs when those mods are actually installed. Untick
  both and you keep just HOT CUE + BEAT JUMP, with no dead tabs. The same
  choice applies to the Permanent (UPD) tab's extras.
- **The stems list works.** The app's "stems files" section looked for an
  older file format only, so it stayed empty even after a successful
  build; it now lists your `Title (STEMS).wav` files with their size.
  And to be clear about the deck: stems files are **not** in the
  rekordbox export. BROWSE › FOLDER does open on a deck (hardware,
  2026-11-07 — the emulator said otherwise and was wrong), but it lists
  only folders at the root of the stick, not `Contents` where a sidecar
  sits, and SEARCH only covers the database. Replacement is the supported
  way: the track itself becomes the stems version and stays under
  ARTIST / TRACK with its cues.
  PERFORM › STEMS mutes vocals / drums / bass / other.
- **Light mode.** In the app (Settings › Appearance: System, Light, Dark)
  and on the deck (UTILITY › THEME › LIGHT). The deck light theme is an
  early preview: it has been tested in the emulator, not on a real deck
  yet; power-cycle to leave it.

## Known issues (not in 1.0)
- **Pro DJ Link:** waveform colours and hot-cue pages only work on the deck
  that holds the USB stick; a deck loading the track over Link from the other
  player stays stock (it never sees the analysis data our mods read).
  **The phase meter's master (orange) row is not affected** — it is proven
  working with a second deck linked (2026-11-06).
- **3-band waveforms need the data:** 3-BAND in UTILITY › WAVEFORM COLOR
  only draws on tracks whose analysis carries the 3-band section — Track
  Builder › Add 3-band writes it (the list shows Ready / In rekordbox /
  Missing per track). On tracks without it the deck falls back to blue —
  that is the "selecting 3-band shows blue" some of you saw, not a broken
  setting. RGB needs the colour data your rekordbox export already has.
- **Hot cues E–H set on the deck:** pages D–F / G–H play cues made in
  rekordbox; setting them on the deck comes later.
- **Track filter:** not in 1.0, and it is not a hidden setting — the
  XDJ-700's firmware contains no filter code at all (no FILTER, no FLTR, no
  RBFLTR anywhere in its 18.6 MB), so there is nothing to switch on. It has
  to be built in the browse menu; it is the first item for 1.1.
- **Mods on a stick are emulator-tested only so far.** Nobody has mounted a
  pack-bearing stick on a real deck yet.
- **Emulator on a heavily loaded Mac:** the audio chip drops to a lower
  clock to keep time, so a busy machine can give a brief gap now and then.
  It no longer speeds up or drags. Windows is still slower than real time.
- **Windows is out of beta.** The Windows app builds from the same source as
  the Mac one, runs the same checks on every build, and has had mods installed
  on a real deck from Windows (the PERFORM pads font bug that broke the first
  Windows installer, `AssertionError: 2802`, was fixed in 0.9.6).
- **Live key shift** stays beta and is **on by default** (untick it on the
  Mods page if you would rather leave it out). It needs MASTER TEMPO on — with MT off the tab says MT OFF and
  does nothing (that is by design: without MT the deck resamples, and a
  key change would also change tempo). One tester's "freeze + file
  corrupted" report is still being looked into; if you hit it, send the
  track's file type and whether it was loaded over Pro DJ Link.

## What changed in 0.99
- **Emulator about 3× faster while playing** (the audio chip now runs on
  its own processor core, and much more of the deck's hardware is
  emulated directly instead of through slower code). Mod testing still
  uses the exact, slower mode.
- **New app layout.** Home → **Mod** → pick how the mods get onto your
  deck: quick install over the USB cable (nothing flashed), a permanent
  firmware file (UPD), or — coming soon — a mods file on your USB stick.
  Theme Builder, Track Builder and the emulator stay in the top bar.
- **Simpler Mods page.** Just tick what to install. Waveform style, theme,
  vinyl brake length and hot cue gate are chosen on the deck (hold MENU ›
  UTILITY).
- **FLAC no longer freezes the deck.** (Deck-confirmed 2026-11-06: a FLAC
  file lists, loads and plays on the deck.) Two bugs: the FLAC decoder needed
  more working memory than the deck's track-loading task has (it now runs
  on its own), and hi-res FLAC (88.2–192 kHz) was handed raw to the deck.
  Hi-res FLAC now plays (converted to 44.1/48 kHz, which is what the
  XDJ-700 plays), and any FLAC that still can't play shows as unplayable
  instead of freezing. Verified in the emulator with the real audio chip.
- **FLAC is only installed when you need it** — ticked automatically when
  your stick has .flac files — and the Opus decoder only when it has .opus
  files: a FLAC-only install is about 3× smaller and much faster.
- **Phase meter fixed:** it read the track *length* instead of the
  position on real decks (beats stood still); it now reads the live
  position (measured on a real deck), shows on the PERFORM screen too,
  keeps the stock "Bars" meter from painting over it when a linked deck
  is master, and stays out of the MENU popup.
- **Linked decks: a freeze fixed.** With a second deck playing a track
  from this deck's stick over Pro DJ Link, the phase meter could read
  track data while the link was rewriting it and freeze the deck. All mods
  that read that data (phase meter, colour waveforms, 8 hot cues) now
  check it first. Tested on two linked decks: no freeze.
- **Live key shift v3:** fixes two bugs that put a buzz on the sound
  while shifted (it misread the samples just before each block and could
  read past the valid ones) — the likely cause of the right-channel noise
  on AIFF tracks. Still beta, now on by default.
- **Faster, honest install:** the time estimate only counts down.
- **Two decks on one computer:** the installer now talks to exactly one
  deck and refuses to start with two connected (before, an install could
  end up split across both players). Plug in one deck at a time.
- **"Waveform colours only" UPD preset** now includes the big centre
  waveform too.
- **All mod settings now live in UTILITY** (hold MENU, scroll to the end):
  MODS, WAVEFORM COLOR, VINYL BRAKE LENGTH, HOT CUE GATE, THEME. The
  BROWSE › MENU › MY SETTINGS popup is stock again — it only held 7 rows,
  so on a linked deck one mod row always went missing.

## What changed in 0.9.8
- **Phase meter on the PERFORM screen too.** It used to show only on the
  main screen; on PERFORM you got the stock 4-beat boxes. The same meter
  now draws there (same spot as the stock boxes), checked in the emulator
  against the beat grid tick by tick.
- **Mod settings in UTILITY.** Hold MENU and scroll to the end of the
  deck's own UTILITY list: MODS, WAVEFORM COLOR, VINYL BRAKE LENGTH, HOT
  CUE GATE and THEME, changed like any stock setting (push, turn, push).
  UTILITY scrolls, so every row is there on every deck — the MY SETTINGS
  popup only holds 7 rows, and with Pro DJ Link the deck adds its own
  [BACKGROUND COLOR] row there, which is why the Hot Cue Gate row went
  missing on a linked player. The MY SETTINGS rows stay exactly as before
  (only on a linked deck, where the popup can't fit them all, it now
  leaves out [THEME] instead of [HOT CUE]); both menus change the same
  settings. The Permanent (UPD) tab can bake the UTILITY rows in too
  (tick box "Mod settings in UTILITY", on by default and in the presets).
- **BEAT JUMP pads let go properly.** After a tap the deck's touch handler
  sometimes stopped before our pad released its button signal, so the
  jump "button" stayed held until the next touch (found in the emulator).
  It's now released a few frames after your finger leaves the screen, which
  matters with SLIP on.
- **Emulator:** faster audio-chip emulation, cleaner sound when the
  emulator can't keep up (short clean bursts instead of the machine-gun
  stutter), and sound on Windows.

## What changed in 0.9.6
- **Windows gets everything the Mac had.** The 0.9.5 Windows app was built
  from a stale snapshot of the source: the permanent-file builder with its
  extras, the phase meter module, the PERFORM-pad font fix and several
  deck-side fixes never reached it. The Windows reports of "UPD creation
  stops after 2 minutes", "centre colour module not built" and "PERFORM
  pads install failed (AssertionError 2802)" all trace back to that —
  0.9.6 ships one source for both platforms.
- **Key shift freeze fixed** (tester: MT off → key shift → MT on froze the
  deck): the KEY SHIFT tab is inert while MASTER TEMPO is off, and any
  MASTER TEMPO change resets the key to 0.
- **8 hot cues: tap works too.** Paging was swipe-only; three testers
  naturally tapped the HOT CUE tab. Tapping the HOT CUE tab pages A–C / D–F /
  G–H. (Deck check 2026-11-06: swiping across the pads does not page there
  yet, so tap is the route; making swipe work is a 1.1 candidate.)
- **Phase meter: reads the time digits' own position source.** The word it
  used first freezes on a real deck (the beats never scrolled); it now
  reads the live one, verified in the emulator against a tick-by-tick model.
- **FLAC & Opus can be left out of an install** — it was 10 of the 15–20
  minutes. Untick it on the Mods page and add it later.
- **Permanent (UPD) file presets** — "Phase meter only" and "Waveform
  colours only" one-click presets next to the tick boxes.
- (0.9.6 note, MY SETTINGS rows since replaced by UTILITY) If colours or the phase meter look gone after a trip through MY SETTINGS:
  check [WAVEFORM] is not BLUE and [MODS] is ON in that menu — those rows
  switch them off.

## Tested on a real deck
- **RGB and 3-band waveforms** — the overview strip and the big centre
  waveform, from the colour data rekordbox already exports.
- **Live key shift** — PERFORM › KEY SHIFT moves the key up or down while
  the track plays (MASTER TEMPO on); tempo stays put and the panel shows
  the track's real key.
- **Live themes** — pick colour themes on the deck (UTILITY › THEME); OFF
  brings back the exact stock look.
- **Live stems** — mute vocals / drums / bass / other from PERFORM ›
  STEMS. A mute is heard after about 30 seconds.
- **Beat jump pads** — PERFORM › BEAT JUMP: tap to jump, swipe the row for
  1 up to 64 beats.
- **The permanent (UPD) file** — one tester flashed it on a real player:
  version 1.16 ran with the RGB overview, beat jump, key shift, stems and
  hot cue paging after power-off.

## Working in the emulator, not yet tested on a deck
None of this has been seen on a real deck. The faster stem mute and the
live-position phase meter are the two we are aiming to finish properly in
1.1; the others are in 1.0 as beta features waiting for a deck report:
- **Faster stem mute (beta)** — rewrites the audio the deck has already
  buffered, so a mute is heard in under a second in the emulator.
- **Phase meter with the live position source** — this deck's beats and
  downbeats above the waveform, main screen and PERFORM (the 0.9.5 build's beats stood still on a
  deck; this one reads the clock's own word).
- **Vinyl brake length** — UTILITY › VINYL BRAKE LENGTH, 0.5× to 15×.
  Deck-confirmed 2026-11-06: the stop really does change to the length you
  pick.
- **8 hot cues** — A–H, in pages of three (tap the HOT CUE tab; swiping the
  pads does not page — deck check 2026-11-06).
- **FLAC and Opus** playback.

## Known issues
- **Live key shift (beta, on by default):** v3 fixes the buzz found in
  the code, but it hasn't been heard on a deck yet, and one tester's
  "freeze + file corrupted" report is still being looked into. Untick it
  on the Mods page if you play live.
- **Not yet confirmed on a real deck:** the FLAC fix, the phase meter's
  stock-meter fix with a linked master, key shift v3.
- The emulator plays at real time with clean audio on Apple Silicon (an M5
  holds the audio chip at ~100-104 MHz, where the governor settled it, with
  zero gaps). Under heavy load expect an occasional brief gap rather than
  any change of pitch or tempo. Windows is still slower than real time.
- Stems need PWV6/PWV7 waveform data in the track's .EXT (rekordbox's
  Track Builder can add it); without it the STEMS tab shows the mix only.

## Safety
The cable install lives in the deck's memory: a power-cycle returns it to
stock. The stick loader and the **Permanent (UPD)** tab write firmware, and
the app says so — it refuses to build either until you tick the brick
acknowledgement. The loader-only update changes 9 bytes of Pioneer's own
file, and Pioneer's 1.15 update puts a deck back the same way you flashed it —
keep that file to hand; the same stick will do. The loader only runs when a
stick goes in: it is not part of the boot, so a deck with no stick in it always
starts up stock.

## Open source
All of it — the Mac app, the emulator, every mod and the tools that build
them — is on GitHub under the MIT licence. Pioneer DJ's firmware is not
included; you add your own copy.

Not affiliated with Pioneer DJ / AlphaTheta.
