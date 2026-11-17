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
