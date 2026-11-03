# XDJ Mod Studio v0.99.1 (XDJ-700, firmware 1.15)

Maintenance release on top of 0.99: the Studio app only, no deck-side
changes. Same requirements as 0.99 (your own XDJ-700 v1.15 update file;
Apple Silicon Mac on macOS 14+, or the Windows zip).

## Mac: "XDJ Mod Studio is damaged and can't be opened"

The app is signed but not notarized with Apple (no paid developer
account), so a downloaded copy is blocked by Gatekeeper. On macOS 15 and
later the message is "damaged" and right-click › Open no longer offers a
way in.
The app is fine; remove the download quarantine mark once:

```bash
xattr -dr com.apple.quarantine "/Applications/XDJ Mod Studio.app"
```

(adjust the path to wherever you put the app), then open it normally.
Alternative: try to open it once, then System Settings › Privacy &
Security › scroll down › **Open Anyway**.

## Fixed in 0.99.1

- **Theme Builder showed a black box** until the firmware was set up, with
  no hint why. It now says so and offers the firmware setup right there;
  the preview appears by itself once setup finishes, and a failed render
  shows its reason instead of a broken image.
- **Long freezes with a big USB library.** Rescanning 1000+ tracks took
  half a minute and happened far too often: on nearly every theme preview,
  on every setup check and on the app's first screen. The library is now
  scanned once and refreshed in the background; previews, tab switches
  and the Home page answer at once.
- **Audio preview in Track Builder** now supports seeking (byte-range
  requests), which the Mac app's web view needs to play files reliably.
- The **Custom colour** swatch no longer shows under every waveform style,
  and the unfinished Appearance setting is hidden again until 1.0.
- **First-run firmware setup** from a source checkout failed late with a
  traceback when the audio-chip disassembler was missing; it now checks
  up front and tells you what to build. Developer README updated.
- The public Mac build no longer needs the SH-4 cross compiler on the
  build Mac: without it, the committed prebuilt deck modules ship.

---

# XDJ Mod Studio v0.99 (XDJ-700, firmware 1.15)

Open-source mods for the Pioneer DJ XDJ-700, loaded into the deck's memory
over USB. Nothing is flashed: **switch the deck off and on and it is stock
again.** You need your own official XDJ-700 v1.15 update file (from Pioneer
DJ's support site); the app checks it and builds everything from it.

Apple Silicon Mac, macOS 14 or newer (Windows build: see the README inside
the Windows zip). First launch: right-click the app → Open.

## About 0.99 — please read
**0.99 is a stepping stone, not the finished 1.0.** It brings every fix
that is ready today, but not everything on the list is fixed yet. The
rest comes in **1.0**, and 1.0 is where everything will be fixed. If
something below says "still to come", it is not in this version.

**Still to come in 1.0 (not in 0.99):**
- **Phase meter, master row:** on linked decks the top (orange) row
  stays empty. Your own deck's row works.
- **Faster stem mute:** muting a stem can still take a few seconds to
  be heard.
- **Hot cues E–H set on the deck:** pages D–F / G–H play cues made in
  rekordbox; setting them on the deck comes later.
- **Light mode** — for the app and as a deck theme — is saved for 1.0.
- **Mods file on your USB stick** (the "coming soon" choice in the app).
- **Emulator:** much faster than 0.9.8 (about 3× while a track plays) but
  still not real time, so sound comes in short bursts.
- **Windows** stays a beta.
- Live key shift stays beta and off by default.

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
- **FLAC no longer freezes the deck.** Two bugs: the FLAC decoder needed
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
  on AIFF tracks. Still beta and off by default.
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
  naturally tapped the HOT CUE tab. Tap and swipe both page A–C / D–F / G–H.
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
These run on the deck's own firmware in the emulator, so they should work
on a real deck too, but nobody has tried them on one yet:
- **Faster stem mute (beta)** — rewrites the audio the deck has already
  buffered, so a mute is heard in under a second in the emulator.
- **Phase meter with the live position source** — this deck's beats and
  downbeats above the waveform, main screen and PERFORM (the 0.9.5 build's beats stood still on a
  deck; this one reads the clock's own word).
- **Vinyl brake length** — UTILITY › VINYL BRAKE LENGTH, 0.5× to 15×.
- **8 hot cues** — A–H, in pages of three (swipe or tap the tab).
- **FLAC and Opus** playback.

## Known issues
- **Live key shift (beta, off by default):** v3 fixes the buzz found in
  the code, but it hasn't been heard on a deck yet, and one tester's
  "freeze + file corrupted" report is still being looked into. Leave it
  unticked if you play live.
- **Not yet confirmed on a real deck:** the FLAC fix, the phase meter's
  stock-meter fix with a linked master, key shift v3.
- The emulator's audio stutters (the emulated audio chip runs slower than
  real time). Browsing, loading and the screens are fine.
- Stems need PWV6/PWV7 waveform data in the track's .EXT (rekordbox's
  Track Builder can add it); without it the STEMS tab shows the mix only.

## Safety
Everything lives in the deck's memory; a power-cycle always returns it to
stock. The optional **Permanent (UPD)** tab builds a firmware file you
would flash yourself. That one *can* brick a deck, and the app says so
before it builds anything.

## Open source
All of it — the Mac app, the emulator, every mod and the tools that build
them — is on GitHub under the MIT licence. Pioneer DJ's firmware is not
included; you add your own copy.

Not affiliated with Pioneer DJ / AlphaTheta.
