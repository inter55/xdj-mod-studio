# XDJ-700 Mod Studio 1.0 — what's new


## What changed in 1.0

- **The emulator's sound no longer speeds up and drags.** The emulated audio
  chip was being driven at a clock faster than the Mac could deliver, so it
  fell behind and then dumped the backlog in one burst: a speed-up, a crawl,
  a stutter. It now runs at the rate the machine can actually hold, climbs
  back when there is headroom, and is paced by the deck's own clock, so pitch
  and tempo never move. Measured on an M5 with a track playing: no gaps.
- **Mods can come off your USB stick.** A small loader replays
  `XDJMODS/PACK.BIN` at every stick mount, so your normal rekordbox stick
  carries the mods and any other stick leaves the deck stock. It can be sent
  over the cable to try it, or built as an update file to keep it.
- **Centre RGB waveform fixed past 7 min 17 s.** The colour entry count was
  read as 16 bits, so longer tracks fell back to stock blue part-way through.
- **Hot cue pages D–H.** Paging reads letter-coded cue entries and larger
  cue lists from newer rekordbox exports.
- **Only the PERFORM tabs you installed.** Live Stems has its own tick box,
  and KEY SHIFT / STEMS are only drawn when those mods are actually there,
  so nobody gets dead tabs.
- **The stems list works**, and the release notes now say where stems files
  appear on the deck: BROWSE, bottom category (FOLDER), not in the rekordbox
  export lists.
- **Light mode** in the app and on the deck (UTILITY › THEME › LIGHT), the
  deck side still an early preview.

Everything else that was already working is still here: RGB and 3-band
waveforms, beat jump up to 64, 8 hot cues, live stems, key shift, Touch FX
(beta), the phase meter and FLAC/Opus playback.

## The three ways to get mods onto the deck

1. **USB stick (recommended).** Flash a small loader once, then your normal
   rekordbox stick carries the mods: the deck reads XDJMODS/PACK.BIN at every
   mount. Stick in, modded; any other stick, stock. No laptop at the gig. The
   update changes 9 bytes of Pioneer's file and boots correctly in the
   emulator with and without a pack, but nobody has put it through a real deck
   flash yet — keep the official 1.15 file to hand before you start; it can go
   back on the same stick afterwards, so one stick is enough.
   Nothing needs flashing to try it: the app can send the same loader over the
   cable in a few seconds, and the stick then works until the deck is powered
   off. The one-time flash is only what makes it survive a power cycle -- and
   after it the deck never needs a computer again.
2. **Cable (zero risk).** Plug the deck's USB-B port into your computer and
   the app installs the mods into the deck's memory. Nothing is written to
   flash, and switching the deck off puts it back to stock; the app offers
   to reinstall when you plug the deck back in.
3. **UPD file.** Builds a proper firmware update with the mods baked in, so
   they survive a power cycle. It is a real flash: it can brick the deck and
   the way back is the official 1.15 file.

## What the mods do

- **Colour waveforms** — RGB (each track in its own rekordbox colours) or
  3-band, on both the overview and the big centre waveform.
- **PERFORM pads** — BEAT JUMP up to 64 beats, and 8 hot cues.
  **Hot cues D–H** now work on every track, and you can set one from an
  empty pad: press it while the track plays and it stores that point.
  Nothing is written to your stick.
- **Live stems** — mute vocals, drums, bass or other while a track plays,
  from the PERFORM STEMS tab.
- **Key shift** — move a track a few semitones while it plays, tempo
  unchanged (MASTER TEMPO on). On by default in 1.0, untick it on the
  Mods page if you would rather leave it out.
- **Touch FX (beta)** — an X/Y pad with Filter, Crush and Gate.
- **Phase meter** — the scrolling beat meter from the XDJ-1000MK2.
- **Themes** — save your own colour set in the app, pick it on the deck in
  UTILITY. Light theme included, but new: the deck only offers LIGHT once
  its text and waveform add-on has fully installed, and nobody has looked
  at it on a real deck yet.
- **FLAC and Opus** playback.

## Stems, properly this time

Making stems for a WAV or AIFF track now **replaces the track** under its
own name, so it stays in ARTIST / TRACK with its cues, its waveform and its
place in your playlists. Your original is kept next to it as
`your track.wav.pre-stems` — rename it back to undo.

That fix matters: files where a chunk of data had an odd length used to sit
on "Loading" forever on the deck. Every chunk in a stems file now has an
even length, the way rekordbox's own files do.

MP3 and M4A tracks are not replaced in 1.0 — the deck picks its decoder from
the file extension, and the stem data rides in a chunk only WAV/AIFF carries —
so those get a stems file next to the original, under BROWSE › FOLDER.
Replacing them in place, so they keep their cues and playlist slot too, is
planned for 1.1.

## The emulator

The app can run the deck's own firmware on your computer, with your mods and
your stick. Good for trying waveforms, themes and the PERFORM pads without
touching the deck. It does not load a real Pioneer update and it cannot
flash anything.

## Known limits, honestly

- The USB-stick loader has not been through a real deck flash yet
  (emulator-tested, boot-tested, every write checked). The UPD path has been
  flashed successfully by a tester.
- **Track filter is not in 1.0.** This one is not a switch we left hidden:
  the XDJ-700's firmware has no filter code in it anywhere (no `FILTER`, no
  `FLTR`, no reader for `PIONEER/rekordbox/RBFLTR.DAT` in all 18.6 MB), so
  it has to be built into the browse menu. It is the first thing on the 1.1
  list, and filtered playlists written to the stick are the fast way to the
  same result.
- MP3 / M4A stems are separate files in 1.0, not replacements. Making them
  replace the track under its own name (so they keep cues, waveform and
  playlist slot like WAV/AIFF already do) is planned for 1.1.
- Touch FX is beta and needs MASTER TEMPO on; Echo, Pitch, Flanger, Roll and
  Reverb are drawn but not implemented yet.
- macOS 14 or newer.

## What we are hoping to have ready for 1.1

Not in this build, and not promised:

- **Faster stem mutes.** A mute that lands in under a second instead of the
  current ~30 s, by rewriting the audio the deck has already buffered. Works
  in the emulator; nobody has heard it on a deck yet.
- **MP3 / M4A stems that replace the track in place**, so they stop being a
  second file under BROWSE › FOLDER and keep the cues and playlist slot the
  way WAV/AIFF do now.
- **The track filter**, ported from the XDJ-1000MK2's browse menu.
- **Pro DJ Link**, so a deck loading over the link gets the same waveforms and
  hot-cue pages as the deck holding the stick.
- **The rest of Touch FX** (Echo, Pitch, Flanger, Roll, Reverb), which are
  drawn but not implemented.
