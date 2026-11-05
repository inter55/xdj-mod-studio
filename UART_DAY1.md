# UART DAY-1 CARD — ctsh → live RAM, nothing else

One page. Everything else is in CTSH_ROADMAP.md. Goal today: prove `dump`
reads live RAM, find the framebuffer, do ONE pixel write. Not persistence.

## Wire-up

    Adapter          Deck pad
    GND  ---------  GND (test point / PSU ground / USB shell tab)
    RX   ---------  SCIF1 TXD
    TX   ---------  SCIF1 RXD
    3.3 V only. NEVER RS-232 levels. USB-TTL adapter jumper set to 3.3 V.

    Serial: 115200 baud, 8 data, no parity, 1 stop, NO flow control.
    (The console's own string says: 115200 B8 PN S1)

Boot the deck with nothing in the slots. Press Enter after boot. Expect a
banner or `login:`. If garbage: swap TX/RX first — it is always TX/RX.

>>> Pad identification is its own document: UART_PADS.md. It has the confirmed
>>> SoC balls (C25 = RXD1, D23 = TXD1, port PTL2/PTL3), the service-manual
>>> schematic route (RRV4637 is public), and the boot-burst method. If the
>>> console does not answer in normal boot, try the service mode documented
>>> in the same service manual before re-wiring anything.

## Step 1 — prove addressing (read-only)

    dump -b 0x08000000 0x40

If the shell prints usage on a bare `dump`, read it and adapt the syntax.

Expected (byte form, or 16-bit words: 0900 05a0 0900 0900 ...):

    090005a0 09000900 09000900 09000900
    07df0200 07d22920 0e4007d0 07d11120
    07d008d1 122008d0 6a4008d0 2b400900
    000000b0 0fffffdf 04005 2a4 07a50000

(strip spaces; last line exactly: `00 00 00 b0 0f ff ff df 04 00 52 a4 07 a5 00 00`)

    MATCH   -> identity-mapped, dump works, no MMU in the way. Continue.
    MISMATCH/empty -> addresses are remapped; run the console's memory-map
    command, note where 0x08000000 content lives, adapt. Do NOT write yet.

## Step 2 — framebuffer from the BEU (read-only)

The SH7724 display units sit at 0xFE930000, 0xFE934000 and ~0xFE935100
(confirmed constants from the display-driver work). One dump covers all:

    dump -l 0xFE930000 0x1800        # if count means "longs" this is 0x6000 bytes
    # check output length; if it looks short, count means BYTES: use 0x6000

Look through the output for 32-bit values in 0x08000000-0x10000000 — those are
RAM pointers, and per-layer source-base registers will hold framebuffer
addresses. Expect ~2-3 of them (one per display unit / layer).

Sanity check on the candidate: FB is a panel-sized RGB565 surface:
800 x 480 x 2 = 768,000 bytes = 0xBB800. Two candidates ~0xBB800 apart with a
second (live) one is normal — double buffer. Pick the one being written
(the DispCap in step 3 settles it definitively).

## Step 3 — capture the screen (read-only)

Run the capture command (string in firmware: c:\_cdj_DispCap565.raw), then
pull the file from the USB stick.

    size 768,000 bytes  -> 800x480 confirmed
    size / 2 = pixels

This file is also the BASELINE photograph for every later comparison. Take it
with a normal track loaded, and again with a different screen if easy.

Serial throughput warning: 115200 baud moves ~10 KB/s of text, so ~3.5 KB/s of
memory. The framebuffer over dump = ~4 min. The whole 18 MB firmware = ~1.5 h
— do NOT dump it today; DispCap over USB is instant. (The full-image dump that
would fix the codec forever can run overnight some other day.)

## Step 4 — first write: ONE pixel (auto-reverts)

Pick the framebuffer base from step 2. Write pixel 0:

    edit -w <FB_BASE> 0x001F        # RGB565 blue

(or 0xF800 red / 0x07E0 green)

    LOOK AT THE SCREEN. One blue pixel top-left = edit works, RGB565 confirmed,
    stride starts at +0. Photograph it — it may last one frame.

If nothing: try the other buffer candidate, then FB_BASE+8 (in case of a small
header). If the pixel appears in the WRONG place, the stride is padded — find
the line-width register in the step-2 dump instead of guessing.

## Step 5 — where I pick it back up

Send me: the step-1 dump text, the step-2 BEU dump text, and the DispCap file.
With those I can, statically:

    - compute where the live UI colour data must sit in RAM (the framebuffer
      pixels vs. the table that draws them)
    - give you exact addresses + values for a visible theme edit
    - and the DispCap file fixes RGB565 ground truth for every UI element.

THEN we theme live, reboot to clear, iterate. Persistence stays parked until
you say otherwise.
