# RGB / 3-band waveform over Pro DJ Link (1.1 research, started 2026-11-08)

Reports: GitHub #7, two Reddit testers. A deck playing a track from the OTHER
deck's stick shows the stock blue waveform even with our mods on both decks.

## Why it is blue

Our colour code (overview cave `rgb_overview`, centre module `tools/centre/cw.c`)
reads the colour sections (PWV4 / PWV5 / PWV6 / PWV7) from the ANLZ context the
deck builds when it loads a track from ITS OWN stick: ctx `[0x09826BE4]`, index
at ctx+0x13B8 (CLAUDE.md sec. 33). A linked track never fills that context with
the `.EXT` file's extra sections, so the snapshot finds nothing and the stock
blue painter runs.

## What the firmware has (static, 2026-11-08)

- The dbserver module (Pro DJ Link database protocol, port 1051) is in BOTH the
  700 and the XDJ-1000MK2 images: the message magic `0x872349AE` appears 9 times
  in each (700: file `0xBA3168`..`0xBC3904`, i.e. code around `0x08BA3168`..
  `0x08BC3904`; 1000MK2: `0x12D193C`..`0x12F2238`).
- Counting request-type constants does NOT settle which requests each deck
  makes or serves: the documented numbers (0x2004 preview, 0x2904 detail,
  0x2C04 analysis-tag, replies 0x4402 / 0x4A02 / 0x4F02) are not loaded as
  plain `mov.w` constants in either image (0x2C04: one load in the 700, none in
  the 1000MK2), so both build them another way. Reading this requires the
  dbserver code decompiled (Ghidra), not constant scans.

## Two possible routes

1. **Ask the other deck's dbserver for the colour sections** (what NXS2-era
   players do with the analysis-tag request). Only works if a STOCK 700 serves
   that request -- unknown, and our client side would have to build the
   request. Riskier: depends on the peer's firmware.
2. **Read the `.EXT` file over the link file path** (preferred). A linked deck
   already reads the AUDIO file from the other deck's stick: the media API
   dispatches by device type, and type 7 is the link reader (CLAUDE.md sec.
   42). If our snapshot routine can open
   `PIONEER/USBANLZ/Pxxx/xxxxxxxx/ANLZ0000.EXT` through the same type-7 path
   when the loaded track is remote, it gets the same bytes it reads from a
   local stick, and everything downstream (overview, centre, 3-band) works
   unchanged. Needs nothing from the peer except what it already serves.

## 2026-11-18: the LINK fallback was drawn in the THEME's colour

Report (fw 1.1): colours work on a track loaded from the deck's own stick,
but a track loaded over LINK showed the fallback waveform in the theme
colour (forest green) instead of the deck's blue.

Cause: 1.1's "waveform colours follow the theme" (`tools/themesw/ts.c`,
`wave_apply`) rewrites the three blue palettes (`tools/theme_switch.py`
`WAVE_PAL`: overview 0x0E7355E0 x12, PERFORM strip 0x0E735FA8 x8, centre
0x0E736150 x8) to the active theme's row. A LINK track has no PWV4/5/6/7, so
there is nothing to paint from and the stock painters draw from exactly
those palettes -- now theme-tinted. That is the one place the theme colour
was never wanted.

Fix (`tools/centre/cw.c`, `theme_sync`): the centre module already runs on
every overview/centre column, so it now also keeps the palettes in step with
WAVEFORM COLOR: the stock row (0) while RGB or 3-BAND is selected (modes 1
and 2), the theme's row only in BLUE/THEME (mode 0 -- the mode whose label
says the blue follows the theme, matching 1.1's note "if the deck doesn't
have the track's colour data, it shows blue instead"). A word holding
anything other than a known row is left alone, only addresses inside
PAL_LO..PAL_HI (defaults 0x0E7355E0..0x0E736170, from `build.sh`) are
touched, and the overview's 3-band colour literals (address bit 0) are
skipped. One frame of lag after a theme or mode change is accepted.

Gate: `tools/centre/cw_unit_test.py` section 4 -- mode 1/2 -> stock, mode 0
-> theme, stock -> theme in mode 0, foreign word untouched; PASS. Emulator
only (there is no LINK model): the deck check is a reinstall plus a LINK
load, and a mode switch on the same track.

## Checking on the deck: `tools/rgb_why.py`

When the colours do not appear at all, this read-only probe (one shell
session) prints MODS (MSOFF), the WAVEFORM COLOR mode byte, the overview
hook pool, the centre palette pool plus both hook words, and re-runs the
1.1 same-track gate's comparison in Python (the ANLZ cache's PWAV vs the
painter input at 0x0BA3BCCC, 360 of 400 columns), then names the cause. It
writes nothing.

If it reports that the cache does not hold this track's PWAV, that is a LINK
(or stale) load and 1.1 is behaving as designed. Before 1.1 nothing was
checked: the cave copied whatever analysis the deck's cache last held, so a
LINK track showed **another track's** colours and looked like RGB was working.
1.1 replaced that with blue. Real colours over LINK need the peer's `.EXT`
(route 2 below), not a change to the check.

Measured 2026-11-18 in `cache/emu_flush_playing.pkl`: the cached PWAV section
is a 20-byte header plus the 400-byte body -- tag `PWAV`, then a little-endian
header length 20, section length 420, 400 entries -- and the 400 columns agree
400/400 with the painter input at `0x0BA3BCCC`. So the check's fixed `+20`
skip is exact; the section header is LE in the cache, not BE (reading a
header-length byte at +7 would give 0).

## Next steps

1. Decompile the media API's type-7 reader and its open: how a remote file is
   named (device id + path?) and opened.
2. Find where the deck learns the loaded track's analysis path for a linked
   track (the dbserver track metadata carries it; the deck must keep it to
   fetch the blue waveform).
3. Emulator: two linked decks are not possible today (no network model in our
   emulator; QEMU's machine has none we use). So the first real test is two
   decks. Build the read-only part first: a probe that logs, on the linked
   deck, the device type + path of every file the deck opens while loading a
   remote track. That one deck session answers steps 1-2 on hardware.
