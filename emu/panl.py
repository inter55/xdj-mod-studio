"""PANL <-> MAIN 32-byte frame builder (PANL_PROTOCOL.md).

  [0..29]  payload
  [30]     u8 checksum: 8-bit sum of bytes 0..29 with END-AROUND CARRY
  [31]     u8 0x8F terminator

Button map: measured, not assumed. tools/panl_oracle.py loads a booted
emulator checkpoint, sets one payload bit at a time, CALLS THE FIRMWARE'S
OWN PARSER (0x8D51A64) and records which key-bitmap bit it sets
(PnlCom state 0x0B6C7A28 + 84..107, the same bits the ctsh `KY` command
uses; names from the KY table at blk1 0x6DD7C). Run on the XDJ-700
v1.15 image, parser variant selected at boot.

Analogue fields (same oracle): payload[2], [3] are u8; payload[4:6],
[6:8], [8:10], [10:12] are big-endian u16 -> panel struct 0x0B6C7A5C
+0, +4, +8, +12, +16, +20. payload[14] is an 8-bit value -> key byte 96.
Which physical control each carries (jog count, tempo fader, rotary) is
set in ANALOG below from the firmware's consumers and is provisional
where marked.

Touchscreen buttons DO travel in this frame once the firmware has
switched to its post-identification parser (see BUTTONS).
"""

# name -> (payload byte, bit mask)
# Measured on a checkpoint where the UI is up (tools/panl_oracle.py on the
# main-screen state). The firmware switches PANL parser variant once the
# panel has identified itself, so a map measured earlier in boot is wrong:
# an earlier table here (payload[12] = CALL/DELETE/MEMORY, payload[20] =
# VINYL/TEMPO RANGE/MT) came from that pre-identification variant.
# Names come from the firmware's own KY table (tools/panl_keymap.py).
# The touch-panel buttons (BROWSE, TAG LIST, SYNC, hot cue MEMORY/CALL...)
# are reported by the panel MCU as ordinary key bits in this frame.
BUTTONS = {
    "NeedlePositionFwd": (12, 0x80),
    "JogTouch":     (15, 0x20),
    "JogRoundFwd":  (15, 0x40),
    "JogPositionFwd": (15, 0x80),
    "PlayPause":    (16, 0x01),
    "Cue":          (16, 0x02),
    "ReloopExit":   (16, 0x04),
    "LoopOut":      (16, 0x08),
    "LoopIn":       (16, 0x10),
    "RotaryPush":   (17, 0x01),
    "SlipMode":     (17, 0x08),
    "RecMode":      (18, 0x02),
    "TrackRev":     (18, 0x04),
    "TrackFwd":     (18, 0x08),
    "ScanRev":      (18, 0x10),
    "ScanFwd":      (18, 0x20),
    "Call_Rev":     (18, 0x40),
    "Call_Fwd":     (18, 0x80),
    "DevLink":      (19, 0x01),
    "DevRekordbox": (19, 0x02),
    "DevUSB":       (19, 0x04),
    "TimeAcue":     (19, 0x20),
    "Delete":       (19, 0x40),
    "Memory":       (19, 0x80),
    "Browse":       (20, 0x01),
    "Taglist":      (20, 0x02),
    "Information":  (20, 0x04),
    "MenuUtility":  (20, 0x08),
    "Back":         (20, 0x10),
    "Tagtrack":     (20, 0x20),
    "JogModeVinyl": (21, 0x01),
    "Sync":         (21, 0x02),
    "Master":       (21, 0x04),
    "TempoRange":   (21, 0x08),
    "MasterTempo":  (21, 0x10),
}
# Bits the parser maps to key bits with no KY name: payload 15.1, 16.5-7,
# 17.1 (key 86 0x08, IN/4BEAT hold?), 18.0, 19.3, 19.4, 20.6, 21.5.

# old key names used by ui.py before the oracle map existed
ALIASES = {"TagList": None, "Return": "Back", "AddTag": "Tagtrack",
           "LoopMode": None, "Slip": "SlipMode", "JogMode": "JogModeVinyl",
           "Quantize": None}

PANL_RAM = 0x0B6C7A30  # parser destination in MAIN RAM
# Browse-knob position counter (u16), +1/-1 per detent via KY 0x42/0x43
# handlers 0x8B2B1EC/0x8B2B204 (= panel struct 0x0B6C7A5C + 0xEC).
ROTARY_COUNTER = 0x0B6C7B48


def checksum(payload30):
    acc = 0
    for x in payload30:
        acc += x
        if acc > 0xFF:  # end-around carry: clear high, increment low
            acc = ((acc & 0xFF) + 1) & 0xFF
    return acc & 0xFF


# Touch screen (decoded live on the deck 2026-10-05, emu-verified 2026-10-06):
# payload +22/23 = raw X (BE, HIGH = LEFT), +24/25 = raw Y (BE, LOW = top).
# The panel never sends +21: the parser (0x8D52634) ORs bit0 into PAY+21 once
# the coords are valid (>=32) for 3 frames; release = both coords back to ~1.
# Firmware's default conversion (fit over a 4x4 grid in the emu, exact to 1px):
#   x_px = 818.04 - 0.82166 * X      y_px = 0.50935 * Y - 15.96
# Hit-tester FUN_08DD0128 (zones 0x080D5D38 {x,y,w,h,handler}) then runs the
# real on-screen buttons (PERFORM, BROWSE, pads ...).
def touch_raw(x_px, y_px):
    """Screen pixel (0..799, 0..479) -> raw panel (X, Y)."""
    X = int(round((818.04 - x_px) / 0.82166))
    Y = int(round((y_px + 15.96) / 0.50935))
    return max(32, min(0x3FF, X)), max(32, min(0x3FF, Y))


def build_frame(keys=(), fader_raw=250, jog=0, rotary=0, needle=0,
                companion=0, flag=False, touch=None, tempo=None,
                jog_period=0xFFFF, jog_moving=False):
    """keys: iterable of button names held down.
    fader_raw: tempo-fader raw ADC mirror 0..511 -> bytes 4/5 (provisional).
    tempo: tempo fader 0..499 -> bytes 12/13 BE (the rate calc's input
      @0x0B6C40D0; None = zeros, i.e. pre-fix behaviour). The GUI fader
      must pass tempo= (bytes 4/5 alone never reach deck tempo).
    jog: running 16-bit jog position counter (bytes 8/9 -> panel struct
      +16 = 0x0B6C7A6C). The firmware's own jog key commands (KY 0x3D/
      0x3E) move it by -/+200 per jog pulse, set the pulse PERIOD (+20,
      bytes 10/11: 300 while turning, 0xFFFF idle) and the "jog moving"
      key bit (payload 15 bit 0x80 -> key byte 87 bit 0x08). All three
      are needed: emulator 2026-11-01, paused deck, +200 x 80 frames with
      the bit set -> playhead +11.8 s; without the bit -> nothing.
    rotary: ignored (bytes 10/11 are the jog period, not the browse knob;
      the knob is the RAM counter ROTARY_COUNTER).
    touch: None or (x_px, y_px) finger on the screen."""
    p = bytearray(30)
    for k in keys:
        k = ALIASES.get(k, k)
        if k is None or k not in BUTTONS:
            continue
        b, m = BUTTONS[k]
        p[b] |= m
    fader = max(0, min(511, int(fader_raw)))
    if jog_moving:
        p[15] |= 0x80
    for off, v in ((4, fader), (8, jog), (10, jog_period)):
        v &= 0xFFFF
        p[off] = v >> 8
        p[off + 1] = v & 0xFF
    p[14] = needle & 0xFF
    if tempo is not None:
        t = max(0, min(499, int(tempo)))
        p[12], p[13] = (t >> 8) & 0xFF, t & 0xFF
    tx, ty = touch_raw(*touch) if touch else (1, 1)
    p[22], p[23], p[24], p[25] = tx >> 8, tx & 0xFF, ty >> 8, ty & 0xFF
    frame = bytearray(p)
    frame.append(checksum(p))
    frame.append(0x8F)
    return bytes(frame)


def validate(frame):
    if len(frame) != 32 or frame[31] != 0x8F:
        return False
    return checksum(frame[:30]) == frame[30]


def inject(bus, frame):
    """Copy payload to emulated RAM like firmware 0xD507A0 does."""
    assert validate(frame), "bad PANL frame"
    base = PANL_RAM - 0x08000000
    for i in range(30):
        bus.ram[base + i] = frame[i]
    return True
