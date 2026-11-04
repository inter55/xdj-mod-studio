#!/usr/bin/env python3
"""mysettings.py -- add rows to the XDJ-700's own BROWSE > MENU > MY SETTINGS
popup (like the XDJ-1000MK2's [WAVE FORM COLOR]), drawn by the firmware:

    MY SETTINGS
    [LOAD]
    [SAVE]
    ([BACKGROUND COLOR]  -- stock, only in some link states)
    [WAVE FORM COLOR]  -> BLUE / RGB / 3-BAND
    [KEY / STEMS]      -> MIX / the loaded file's versions

How the stock popup works (decompiled 2026-10-05):
  * builder FUN_08B8A9F0(ctx, p2, p3), called through pool 0x08B998D0:
    list L = ctx + 0x1A980: u32 count, u32 cursor, 0, title (12 B hdr +
    UTF-16), rows at L+0x118 + k*0x10C: {u32 style, u32 attr (0x63
    normal, 100 option, 0x5E option = current, 0x463 greyed), 0,
    UTF-16 text; [ ] stored as 0xFFFA / 0xFFFB}. The main list is shown
    by jsr [0x08B8AFE8] = 0x08DCB850 (r14 = L, count already stored).
  * push handler FUN_08B8CA40(ctx, p2, p3) via pool 0x08B91828; for this
    popup ([ctx] == 2, top level) the row = u16 [p3+0x78]. BACKGROUND
    COLOR uses submenu state ctx+0x69FC (0 main / 1 options), ctx+0x6A04
    (parent row), ctx+0x6A00 (u8) and re-renders via 0x08B93862.
Our three hooks (pool words, written last, popup closed):
  0x08B8AFE8 -> MAPPEND : append our rows to the main list, then show it
  0x08B91828 -> MPUSH   : push on our row -> options state (marker
                          0x100+k in ctx+0x6A04); push on an option ->
                          apply, back to the main list; else stock
  0x08B998D0 -> MBUILD  : in our options state build the option list
                          (current one highlighted) like the stock colour
                          list; else the stock builder

  mysettings.py plan | emu
"""
import os
import struct
import sys

sys.path[:0] = [".", "tools"]
from shasm import Asm
import rgb_overview as RO
import vredirect as VR
import theme_switch as TS

POOL_SHOW, SHOW = 0x08B8AFE8, 0x08DCB850
POOL_PUSH, PUSH = 0x08B91828, 0x08B8CA40
POOL_BUILD, BUILD = 0x08B998D0, 0x08B8A9F0
REFRESH = 0x08B93862
FINISH = 0x08B8BB6A                 # builder's common tail (ctx, p2, p3)
LIST = 0x1A980
ST69FC, ST6A04, ST6A00 = 0x69FC, 0x6A04, 0x6A00

MS = RO.CAVE + 0x2D00              # 0x081DB600 (the retired overlay menu's space)
MSDATA = MS + 0x400
MSST = MS + 0x7F0                  # u8 base row of ours in the main list
MSOFF = MS + 0x7F1                 # u8 1 = mods OFF
MSSAVE = MS + 0x7F2                # saved mode, version
MSBUF = MS + 0x7E0                 # 9-B ASCII name buffer
MSTAPP = MS + 0x7F5                # u8 1 = theme applied this boot
MSTNAME = MS + 0x800               # 16-B ASCII theme name (theme installer)
MSGATE = MS + 0x7F8                 # u8 hot-cue gate (0 off, 1 gate)
MSBRAKE = MS + 0x7F9                # u8 vinyl brake length index (0 = 1x)
# 2026-10-25: on a linked deck (no room for all five rows) [THEME] is the
# one left out, not [HOT CUE] (tester: "one player has the Hot Cue Gate
# row, the other doesn't"); otherwise all five rows, as before.
# UTILITY (tools/utility.py) always has every row. MSKB = first kind shown.
MSKB = MS + 0x7FA                   # u8 0 = [THEME] shown, 1 = hidden
# [VINYL BRAKE] (2026-10-18): brake length = K / ..., K = the one literal
# at 0x08D3714C (tools/brake_length_apply.py, CLAUDE.md §120)
BRAKE_LIT, BRAKE_STOCK = 0x08D3714C, 6_000_000
BRAKES = [("1X", 1.0), ("2X", 2.0), ("4X", 4.0), ("8X", 8.0), ("15X", 15.0), ("0.5X", 0.5)]
MSEND = MS + 0x820
# [THEME] push calls through this table word; 2026-10-12 theme switching has
# no proven-free home yet -> a word that is always zero (push = no-op)
THEME_TABLE = 0x081DD7FC if os.environ.get("THEMESW_DISABLE") else TS.TS_BASE

# Kind order = popup order; with >2 stock rows (Pro DJ Link adds
# [BACKGROUND COLOR]) the popup's 7-row limit can't hold all five:
# 2026-10-25 (tester: the linked player had no Hot Cue Gate row) [THEME]
# is the one left out then -- unlink to change themes; the gate and the
# brake stay reachable while mixing. (Was: [HOT CUE] dropped, 2026-10-21.)
LABELS = ["[THEME]", "[MODS]", "[WAVEFORM]", "[VINYL BRAKE]", "[HOT CUE]"]
NROWS = len(LABELS)
TITLES = ["WAVE FORM COLOR", "KEY / STEMS"]
WAVES = ["BLUE", "RGB", "3-BAND"]
ONOFF = ["ON", "OFF"]


def data_layout():
    blob, A = bytearray(), {}

    def put(name, s):
        A[name] = MSDATA + len(blob)
        blob.extend(s.encode() + b"\0")
    for i, s in enumerate(LABELS):
        put(f"l{i}", s)
    for i, s in enumerate(ONOFF):
        put(f"o{i}", s)
    for i, s in enumerate(WAVES):
        put(f"w{i}", s)
    put("mix", "MIX")
    put("none", "-")
    put("gap", "  ")
    put("off", "OFF")
    put("gate", "GATE")
    for i, (t, _) in enumerate(BRAKES):
        put(f"b{i}", t)
    while len(blob) % 4:
        blob.append(0)
    for tab, keys in (("labels", [f"l{i}" for i in range(len(LABELS))]),
                      ("waves", ["w0", "w1", "w2"]),
                      ("brakes", [f"b{i}" for i in range(len(BRAKES))])):
        A[tab] = MSDATA + len(blob)
        for k in keys:
            blob += struct.pack("<I", A[k])
    A["bk"] = MSDATA + len(blob)                 # K value per brake index
    for _, x in BRAKES:
        blob += struct.pack("<I", int(BRAKE_STOCK * x))
    assert MSDATA + len(blob) <= MSBUF, len(blob)
    return bytes(blob), A


_, A = data_layout()

SRC = f"""
mappend:                    ; = show fn of the main MY SETTINGS list
  sts.l pr,@-r15            ; r14 = list, [r14] = row count, r4 = arg
  mov.l r4,@-r15
  mov.l r8,@-r15
  mov.l r9,@-r15
  mov.l @r14,r8             ; n
  mov #3,r0
  cmp/hi r0,r8              ; already > 3 rows: leave it alone
  bt apdone
  mov #7,r0                 ; room = 7 - n (popup holds 7)
  sub r8,r0
  mov #0,r2                 ; r2 = first kind shown: all five rows ...
  mov #{NROWS},r1
  cmp/hs r1,r0              ; ... unless there's no room (linked deck: stock
  bt apk                    ; [BACKGROUND COLOR]) -> skip [THEME], so
  mov #1,r2                 ; [HOT CUE] + [VINYL BRAKE] stay reachable
apk:
  mov.l @({MSKB:#x},pc),r1
  mov.b r2,@r1
  ; our rows = min(NROWS - kb, room)
  mov #{NROWS},r1
  sub r2,r1
  cmp/hi r1,r0
  bf apc
  mov r1,r0
apc:
  mov.l r0,@-r15
  mov.l @({MSST:#x},pc),r1
  mov r8,r0
  mov.b r0,@r1              ; our first row index
  mov #0,r9                 ; k
apl:
  mov r8,r0
  add r9,r0
  bsr rowaddr               ; r0 = row
  nop
  mov #0,r1
  mov.l r1,@r0
  mov #99,r1
  mov.l r1,@(4,r0)          ; attr 0x63 = normal row
  mov #0,r1
  mov.l r1,@(8,r0)
  mov r0,r4
  mov.l @({MSKB:#x},pc),r1
  mov.b @r1,r1
  add r1,r9                 ; r9 = kind of this row
  bsr rowtext               ; r4 = row, r9 = kind
  nop
  mov.l @({MSKB:#x},pc),r1
  mov.b @r1,r1
  sub r1,r9                 ; back to the row counter
  add #1,r9
  mov.l @r15,r0
  cmp/hs r0,r9
  bf apl
  mov.l @r15+,r0
  add r0,r8
  mov.l r8,@r14
apdone:
  mov.l @r15+,r9
  mov.l @r15+,r8
  mov.l @r15+,r4
  lds.l @r15+,pr
  mov.l @({SHOW:#x},pc),r0
  jmp @r0
  nop
rowaddr:                    ; r0 = row index, r14 = list -> r0 = row ptr
  mov r0,r1
  shll8 r1                  ; *256
  mov r0,r2
  shll2 r2
  shll2 r2                  ; *16
  add r2,r1
  shll2 r0                  ; *4  -> 268 = 256 + 16 - 4
  sub r0,r1
  mov.l @({0x118:#x},pc),r0
  add r14,r0
  rts
  add r1,r0
rowtext:                    ; r4 = row, r9 = kind: "[LABEL]  VALUE"
  sts.l pr,@-r15
  add #12,r4
  mov.l @({A["labels"]:#x},pc),r1
  mov r9,r0
  shll2 r0
  mov.l @(r0,r1),r5
  bsr wcopy
  nop
  add #-2,r4                ; overwrite the terminator
  mov.l @({A["gap"]:#x},pc),r5
  bsr wcopy
  nop
  add #-2,r4
  mov r4,r1                 ; value text -> r5
  mov.l r1,@-r15
  mov r9,r0
  bsr valstr
  nop
  mov.l @r15+,r4
  bsr wcopy
  nop
  lds.l @r15+,pr
  rts
  nop
valstr:                     ; r0 = kind -> r5 = ASCII value string
  cmp/eq #0,r0              ; 0 = THEME (first: never pushed off by stock rows)
  bf vmods
  mov.l @({MSTNAME:#x},pc),r5
  mov.b @r5,r0
  tst r0,r0
  bf vst1
  mov.l @({A["none"]:#x},pc),r5
  rts
  nop
vst1:
  mov.l @({MSTAPP:#x},pc),r1
  mov.b @r1,r0
  tst r0,r0
  bf vst2
  mov.l @({A["off"]:#x},pc),r5
vst2:
  rts
  nop
vmods:
  add #-1,r0
  cmp/eq #0,r0              ; 1 = MODS
  bf vwave
  mov.l @({MSOFF:#x},pc),r1
  mov.b @r1,r0
  mov.l @({A["o0"]:#x},pc),r5
  tst r0,r0
  bt vmx
  mov.l @({A["o1"]:#x},pc),r5
vmx:
  rts
  nop
vwave:
  add #-1,r0
  cmp/eq #0,r0              ; 2 = WAVEFORM
  bf vsver
  mov.l @({RO.MODE_ADDR:#x},pc),r1
  mov.b @r1,r0
  extu.b r0,r0
  mov #3,r1
  cmp/hs r1,r0
  bf vs0
  mov #0,r0
vs0:
  shll2 r0
  mov.l @({A["waves"]:#x},pc),r1
  rts
  mov.l @(r0,r1),r5
vsver:                      ; 3 = VINYL BRAKE, 4 = HOT CUE gate (OFF/GATE)
  add #-1,r0
  cmp/eq #0,r0
  bf vsgate
  mov.l @({MSBRAKE:#x},pc),r1
  mov.b @r1,r0
  extu.b r0,r0
  mov #{len(BRAKES)},r1
  cmp/hs r1,r0
  bf vb0
  mov #0,r0
vb0:
  shll2 r0
  mov.l @({A["brakes"]:#x},pc),r1
  rts
  mov.l @(r0,r1),r5
vsgate:
  mov.l @({MSGATE:#x},pc),r1
  mov.b @r1,r0
  mov.l @({A["off"]:#x},pc),r5
  tst r0,r0
  bt vsx
  mov.l @({A["gate"]:#x},pc),r5
  bra vsx
  nop
vsx:
  rts
  nop
wcopy:                      ; r4 = UTF-16 dst, r5 = ASCII src ('['/']' ->
  mov.b @r5+,r0             ; the firmware's bracket codes 0xFFFA/0xFFFB)
  extu.b r0,r0
  cmp/eq #91,r0
  bf wc1
  mov.l @({0xFFFA:#x},pc),r0
wc1:
  cmp/eq #93,r0
  bf wc2
  mov.l @({0xFFFB:#x},pc),r0
wc2:
  mov.w r0,@r4
  tst r0,r0
  bf/s wcopy
  add #2,r4
  rts
  nop
mpush:                      ; = push handler (r4 ctx, r5, r6 p3)
  mov.l @r4,r0
  cmp/eq #2,r0              ; MY SETTINGS-type popup?
  bf porig
  mov.l @(4,r4),r0          ; top level only
  shll2 r0
  add r4,r0
  mov.l @({0x6998:#x},pc),r1
  add r1,r0
  mov.l @r0,r0
  mov #2,r1
  cmp/hs r1,r0
  bt porig
  mov.l @({ST69FC:#x},pc),r0
  mov.l @(r0,r4),r1
  tst r1,r1                 ; main list (not a stock sub-list)
  bf porig
  mov #120,r0
  mov.w @(r0,r6),r1
  extu.w r1,r1              ; cursor row
  mov.l @({MSST:#x},pc),r2
  mov.b @r2,r2
  extu.b r2,r2
  sub r2,r1                 ; k = row - first
  mov.l @({MSKB:#x},pc),r3
  mov.b @r3,r3
  mov #{NROWS},r0
  sub r3,r0                 ; rows of ours
  cmp/hs r0,r1
  bf ours
porig:
  mov.l @({PUSH:#x},pc),r0
  jmp @r0
  nop
ours:
  mov.l @({MSST + 6:#x},pc),r3
  mov.b r1,@r3              ; debug: k of this push
  mov.l @({MSST + 7:#x},pc),r3
  add r2,r1
  mov.b r1,@r3              ; debug: absolute cursor row
  sub r2,r1                 ; r1 = k again for dispatch below
  mov.l @({MSKB:#x},pc),r3
  mov.b @r3,r3
  add r3,r1                 ; r1 = kind (skips a hidden [THEME])
  sts.l pr,@-r15
  mov.l r8,@-r15
  mov.l r9,@-r15
  mov.l r10,@-r15
  mov.l r11,@-r15
  mov.l r14,@-r15
  mov r4,r8
  mov r5,r10
  mov r6,r11
  mov r1,r9                 ; kind
  mov r1,r0
  cmp/eq #0,r0              ; 0 = THEME: next theme, recoloured now
  bf pm1                    ; (tools/theme_switch.py, deck C ts_push)
  mov.l @({MSOFF:#x},pc),r1
  mov.b @r1,r1
  tst r1,r1
  bf papplied
  mov.l @({THEME_TABLE:#x},pc),r1
  mov.l @r1,r1              ; entry table +0 = ts_push (0 = not installed)
  tst r1,r1
  bt papplied
  jsr @r1                   ; C: keeps r8-r14 (ABI)
  nop
  bra papplied
  nop
pm1:
  add #-1,r0
  cmp/eq #0,r0              ; 1 = MODS
  bf pm2
  mov.l @({MSOFF:#x},pc),r1
  mov.b @r1,r0
  tst r0,r0
  bf pon
  mov #1,r0                 ; -> OFF: save settings, stock behaviour
  mov.b r0,@r1
  mov.l @({MSSAVE:#x},pc),r2
  mov.l @({RO.MODE_ADDR:#x},pc),r1
  mov.b @r1,r0
  mov.b r0,@(0,r2)
  mov #0,r0
  mov.b r0,@r1              ; blue
  mov.l @({VR.VSEL:#x},pc),r1
  mov.b @r1,r0
  mov.b r0,@(1,r2)
  mov #0,r0
  mov.b r0,@r1              ; mix
  mov.l @({BRAKE_LIT:#x},pc),r1
  mov.l @({BRAKE_STOCK:#x},pc),r0
  bra papplied
  mov.l r0,@r1              ; stock vinyl brake
pon:
  mov #0,r0                 ; -> ON: restore
  mov.b r0,@r1
  mov.l @({MSSAVE:#x},pc),r2
  mov.b @(0,r2),r0
  mov.l @({RO.MODE_ADDR:#x},pc),r1
  mov.b r0,@r1
  mov.b @(1,r2),r0
  mov.l @({VR.VSEL:#x},pc),r1
  mov.b r0,@r1
  bsr bkset                 ; chosen vinyl brake back
  nop
  bra papplied
  nop
bkset:                      ; K literal <- bk[MSBRAKE] (index clamped)
  mov.l @({MSBRAKE:#x},pc),r1
  mov.b @r1,r0
  extu.b r0,r0
  mov #{len(BRAKES)},r2
  cmp/hs r2,r0
  bf bks0
  mov #0,r0
bks0:
  shll2 r0
  mov.l @({A["bk"]:#x},pc),r1
  mov.l @(r0,r1),r2
  mov.l @({BRAKE_LIT:#x},pc),r1
  rts
  mov.l r2,@r1
pm2:
  mov.l @({MSOFF:#x},pc),r1 ; other rows do nothing while MODS = OFF
  mov.b @r1,r1
  tst r1,r1
  bf papplied
  add #-1,r0
  cmp/eq #0,r0              ; 2 = WAVEFORM
  bf pm3
  mov.l @({RO.MODE_ADDR:#x},pc),r1
  mov.b @r1,r0
  add #1,r0
  extu.b r0,r0
  mov #3,r2
  cmp/hs r2,r0
  bf pk0
  mov #0,r0
pk0:
  bra papplied
  mov.b r0,@r1
pm3:                        ; 3 = VINYL BRAKE: next length, applied now; 4 = HOT CUE
  add #-1,r0
  cmp/eq #0,r0
  bf pm3g
  mov.l @({MSBRAKE:#x},pc),r1  ; 4 = VINYL BRAKE: next length, applied now
  mov.b @r1,r0
  add #1,r0
  extu.b r0,r0
  mov #{len(BRAKES)},r2
  cmp/hs r2,r0
  bf pb0
  mov #0,r0
pb0:
  mov.b r0,@r1
  bsr bkset
  nop
  bra papplied
  nop
pm3g:
  mov.l @({MSGATE:#x},pc),r1
  mov.b @r1,r0
  mov #1,r2
  xor r2,r0
  extu.b r0,r0
  bra papplied
  mov.b r0,@r1
papplied:                   ; value stored: close like stock LOAD/SAVE
  mov r8,r4                 ; (an auto-reopen was attempted: MENU key bits
  mov r10,r5                ; live in the panel-RX path, unreachable from a
  mov r11,r6                ; RAM cave, so a firmware-side re-press can never
  mov.l @({REFRESH:#x},pc),r0 ; fire it; the close itself is healthy stock
  jsr @r0                   ; behaviour)
  nop
  mov.l @r15+,r14
  mov.l @r15+,r11
  mov.l @r15+,r10
  mov.l @r15+,r9
  mov.l @r15+,r8
  lds.l @r15+,pr
  rts
  mov #0,r0
"""


def find_list(mem):
    """The open popup's list L (heap address moves with open/close
    history): locate "[WAVEFORM]" UTF-16, back up to the header, accept
    the candidate whose title reads MY SETTINGS. None if closed."""
    needle = "WAVEFORM".encode("utf-16-le")  # (brackets in RAM
    # are the firmware codes 0xFFFA/0xFFFB, not ASCII). Both RAM banks:
    # the popup heap sits in bank 1 (0x0C000000+).
    for ram, base in ((mem.ram1, 0x0C000000), (mem.ram0, 0x08000000)):
        L = _scan_bank(mem, ram, base, needle)
        if L is not None:
            return L
    return None


def _scan_bank(mem, ram, base, needle):
    start = 0
    while True:
        i = ram.find(needle, start)
        if i < 0:
            return None
        for idx in range(7):
            L = base + i - 2 - 12 - 0x118 - idx * 0x10C
            if not (base <= L < base + len(ram)):
                continue
            if mem.r32(L) > 7:
                continue
            t = "".join(chr(mem.r16(L + 24 + 2 * j)) for j in range(11))
            if t == "MY SETTINGS":
                return L
        start = i + 2


def _src():
    return SRC


def assemble_all():
    a = Asm()
    code = a.assemble(_src())
    assert MS + len(code) <= MSDATA, len(code)
    return code, {k: MS + a.labels[k] for k in ("mappend", "mpush")}


def hooks():
    _, e = assemble_all()
    return [(POOL_SHOW, SHOW, e["mappend"]), (POOL_PUSH, PUSH, e["mpush"])]


def image():
    code, _ = assemble_all()
    data, _ = data_layout()
    img = bytearray(MSST - MS)
    img[:len(code)] = code
    img[MSDATA - MS:MSDATA - MS + len(data)] = data
    return bytes(img)


def install(mem):
    for i, b in enumerate(image()):
        mem.w8(MS + i, b)
    for pool, old, new in hooks():
        assert mem.r32(pool) == old, hex(pool)
        mem.w32(pool, new)


def emu():
    from emu.machine import Machine
    from emu import panl
    from ghosttap_emu import fb_png
    out = "screenshots/mysettings"
    os.makedirs(out, exist_ok=True)
    m = Machine.load(os.environ.get("CKPT", "/tmp/browse_top.pkl"))
    mem, dev, c = m.mem, m.mem.dev, m.cpu
    for a, b in RO.plan(RO.MODES["rgb"])[1]:        # mode byte lives here
        mem.w8(a, b)
    for a, b in VR.plan():
        mem.w8(a, b)
    install(mem)
    if os.environ.get("THEMEROW"):              # theme switching "installed"
        for i, b in enumerate(b"OFF\0"):
            mem.w8(MSTNAME + i, b)

    def key(k, hold=12_000_000, after=25_000_000):
        dev.panel_frame = panl.build_frame(keys=[k])
        c.run(limit=hold)
        dev.panel_frame = panl.build_frame()
        c.run(limit=after)

    def turn(n):
        for _ in range(abs(n)):
            mem.w16(panl.ROTARY_COUNTER,
                    (mem.r16(panl.ROTARY_COUNTER) + (1 if n > 0 else -1)) & 0xFFFF)
            c.run(limit=15_000_000)

    ok = True

    def find_list():
        return globals()["find_list"](mem)

    def ctx_now():
        return 0x0CAF8F24

    def row_text(k):
        L = find_list()
        if L is None:
            return ""
        k -= mem.r8(MSKB)                 # kind -> row ([THEME] hidden?)
        if k < 0:
            return ""
        r = L + 0x118 + (mem.r8(MSST) + k) * 0x10C + 12
        s = ""
        for i in range(40):
            ch = mem.r16(r + 2 * i)
            if not ch:
                break
            s += {0xFFFA: "[", 0xFFFB: "]"}.get(ch, chr(ch))
        return s

    def open_menu_at(k):
        key("MenuUtility")
        turn(mem.r8(MSST) + k)

    rows = lambda: [row_text(k) for k in range(NROWS)]
    key("MenuUtility")                    # open once; stays open now
    c.run(limit=25_000_000)
    fb_png(m, f"{out}/a_rows.png")
    print("rows:", rows())
    cur = None
    base = None
    # pushes close the popup (stock LOAD/SAVE behaviour): reopen and move
    # absolute every time.

    def goto(k):
        nonlocal cur, base
        if base is None:
            base = mem.r8(MSST)
        if any(row_text(i) for i in range(5)):
            key("Back")                # ensure closed (MENU toggles)
        key("MenuUtility")             # open fresh, cursor on top
        turn(base + k - mem.r8(MSKB))
        cur = k

    goto(2)
    m0 = mem.r8(RO.MODE_ADDR)
    key("RotaryPush")                       # WAVEFORM -> next
    print(f"WAVEFORM push: {m0} -> {mem.r8(RO.MODE_ADDR)}")
    ok &= mem.r8(RO.MODE_ADDR) == (m0 + 1) % 3
    goto(2)                                 # reopen: list rebuilt
    print("rows:", rows())
    ok &= WAVES[mem.r8(RO.MODE_ADDR)] in row_text(2)
    mode1 = mem.r8(RO.MODE_ADDR)
    goto(1)
    key("RotaryPush")                       # MODS -> OFF
    print(f"MODS OFF: mode {mem.r8(RO.MODE_ADDR)} (want 0)")
    ok &= mem.r8(RO.MODE_ADDR) == 0
    print("rows:", rows())
    fb_png(m, f"{out}/b_mods_off.png")
    goto(1)                                 # popup closed after the push
    key("RotaryPush")                       # MODS -> ON
    print(f"MODS ON : mode {mem.r8(RO.MODE_ADDR)} (want {mode1})")
    ok &= mem.r8(RO.MODE_ADDR) == mode1
    print("rows:", rows())
    fb_png(m, f"{out}/c_final.png")
    goto(0)                                 # unlinked: all five rows
    print("rows:", rows())
    ok &= mem.r8(MSKB) == 0 and row_text(0).startswith("[THEME]")
    goto(4)
    key("RotaryPush")                       # HOT CUE -> GATE
    print(f"HOT CUE push: gate={mem.r8(MSGATE)}")
    ok &= mem.r8(MSGATE) == 1
    goto(4)
    print("rows:", rows())
    ok &= row_text(4).endswith("GATE")
    key("RotaryPush")                       # HOT CUE -> OFF
    ok &= mem.r8(MSGATE) == 0
    goto(4)
    print("rows:", rows())
    ok &= row_text(4).endswith("OFF")
    # [VINYL BRAKE]: each push = next length, K literal written at once
    ok &= mem.r32(BRAKE_LIT) == BRAKE_STOCK and row_text(3).endswith("1X")
    seq = []
    for _ in range(len(BRAKES)):
        goto(3)
        key("RotaryPush")
        seq.append((mem.r8(MSBRAKE), mem.r32(BRAKE_LIT)))
    print("VINYL BRAKE pushes:", seq)
    ok &= [k for k, _ in seq] == [1, 2, 3, 4, 5, 0]
    ok &= all(v == int(BRAKE_STOCK * BRAKES[k][1]) for k, v in seq)
    goto(3)
    key("RotaryPush")                       # -> 2X
    goto(3)
    print("rows:", rows())
    ok &= row_text(3).endswith("2X")
    goto(1)
    key("RotaryPush")                       # MODS OFF -> stock brake
    ok &= mem.r32(BRAKE_LIT) == BRAKE_STOCK
    goto(1)
    key("RotaryPush")                       # MODS ON -> 2X again
    ok &= mem.r32(BRAKE_LIT) == 2 * BRAKE_STOCK
    print(f"brake after MODS off/on: {mem.r32(BRAKE_LIT)}")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    if sys.argv[1:2] == ["plan"]:
        code, e = assemble_all()
        print(f"code {len(code)} B @ {MS:#x}; entries "
              + ", ".join(f"{k} {v:#x}" for k, v in e.items()))
        for pool, old, new in hooks():
            print(f"hook {pool:#x}: {old:#x} -> {new:#x}")
        return 0
    if sys.argv[1:2] == ["theme"]:
        return emu_theme(*(sys.argv[2:3] or ["hot-pink"]))
    return emu()



def emu_theme(name="hot-pink"):
    """[THEME] row: OFF -> push -> name shown, artwork recoloured on the
    next track load (theme code/blob as theme_deck_apply.py installs)."""
    import numpy as np
    from emu.machine import Machine
    from emu import panl
    from ghosttap_emu import fb_png
    import theme_deck as TD
    m = Machine.load(os.environ.get("CKPT", "/tmp/browse_top.pkl"))
    mem, dev, c = m.mem, m.mem.dev, m.cpu
    for a, b in RO.plan(RO.MODES["rgb"])[1]:
        mem.w8(a, b)
    for base, b in ((TD.THEME_CODE, TD.theme_code()), (TD.THEME_DATA, TD.blob(name))):
        for i, x in enumerate(b):
            mem.w8(base + i, x)
    for i, x in enumerate(name.upper().encode()[:15] + b"\0"):
        mem.w8(MSTNAME + i, x)
    for a, b in VR.plan():
        mem.w8(a, b)
    install(mem)

    def key(k, hold=12_000_000, after=25_000_000):
        dev.panel_frame = panl.build_frame(keys=[k])
        c.run(limit=hold)
        dev.panel_frame = panl.build_frame()
        c.run(limit=after)

    def turn(n):
        for _ in range(n):
            mem.w16(panl.ROTARY_COUNTER, (mem.r16(panl.ROTARY_COUNTER) + 1) & 0xFFFF)
            c.run(limit=15_000_000)

    def row(k):
        L = find_list(mem)
        if L is None:
            return ""
        r = L + 0x118 + (mem.r8(MSST) + k) * 0x10C + 12
        s = ""
        for i in range(40):
            ch = mem.r16(r + 2 * i)
            if not ch:
                break
            s += {0xFFFA: "[", 0xFFFB: "]"}.get(ch, chr(ch))
        return s
    ok = True
    key("MenuUtility")
    print("row before:", row(0))
    ok &= row(0).endswith("OFF")
    turn(mem.r8(MSST) + 0)
    key("RotaryPush")
    print("flag", mem.r8(RO.MODE_ADDR + 1), "applied", mem.r8(MSTAPP))
    ok &= mem.r8(RO.MODE_ADDR + 1) == 1 and mem.r8(MSTAPP) == 1
    key("MenuUtility")
    print("row after:", row(0))
    ok &= row(0).endswith(name.upper())
    fb_png(m, "screenshots/mysettings/t_theme_row.png")
    key("Back")
    w = TD.stock_words()
    before = np.frombuffer(bytes(mem.ram0[:len(w) * 2]), "<u2").copy()
    for _ in range(5):                       # dive to the first track, load
        key("RotaryPush", after=40_000_000)
    c.run(limit=int(os.environ.get("STEPS", "1500000000")))
    after = np.frombuffer(bytes(mem.ram0[:len(w) * 2]), "<u2")
    _, src, dst, new = TD.theme_tables(name, w)
    exp = (new != w)                          # pixels the theme recolours
    n = int((after[exp] == new[exp]).sum())   # ... that now hold them
    print(f"themed pixels in RAM: {n:,} of {int(exp.sum()):,}  flag now "
          f"{mem.r8(RO.MODE_ADDR + 1)} (0 = theme pass ran)")
    ok &= n >= 0.99 * int(exp.sum()) and mem.r8(RO.MODE_ADDR + 1) == 0
    fb_png(m, "screenshots/mysettings/t_theme_applied.png")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def emu_theme_switch(names):
    """[THEME] cycles OFF -> each theme -> OFF via REAL menu pushes (deck C
    ts_push). After every push the artwork in RAM must equal the Python
    model (tools/theme_switch.py) exactly; after the full cycle it must be
    byte-identical to before the first push (= stock)."""
    import numpy as np
    from emu.machine import Machine
    from emu import panl
    from ghosttap_emu import fb_png
    out = "screenshots/themesw"
    os.makedirs(out, exist_ok=True)
    m = Machine.load(os.environ.get("CKPT", "cache/emu_mainscreen.pkl"))
    mem, dev, c = m.mem, m.mem.dev, m.cpu
    for a, b in RO.plan(RO.MODES["rgb"])[1]:
        mem.w8(a, b)
    install(mem)
    for a in range(TS.TS_BASE, TS.TS_DATA, 4):        # bss: deck survey = 0
        mem.w32(a, 0)
    for base, b in TS.plan(names):
        for i, x in enumerate(b):
            mem.w8(base + i, x)
    for i, x in enumerate(b"OFF".ljust(16, b"\0")):
        mem.w8(MSTNAME + i, x)
    # lit button pictures -> private colours (theme_switch_apply.py order:
    # before arming, only while they read stock)
    nlit = 0
    for a, st, pv in TS.lit_writes(names):
        if bytes(mem.ram0[a - 0x08000000:a - 0x08000000 + len(st)]) == st:
            for i, x in enumerate(pv):
                mem.w8(a + i, x)
            nlit += 1
    if nlit:
        print(f"lit button pictures moved to private colours: {nlit}")
    wmod, regs, S, Ds, _, _g = TS.tables_full(names)
    lo = min(a for a, _ in regs)
    hi = max(a + 2 * n for a, n in regs)

    mask = np.zeros((hi - lo) // 2, bool)   # artwork pixels only (our own
    for a, n in regs:                         # state bytes sit in between)
        mask[(a - lo) // 2:(a - lo) // 2 + n] = True

    def ram_words():
        w = np.frombuffer(bytes(mem.ram0[lo - 0x08000000:hi - 0x08000000]),
                          "<u2").copy()
        w[~mask] = 0
        return w

    def model(img, cur, tgt):
        frm = S if cur == 0 else Ds[cur - 1]
        to = S if tgt == 0 else Ds[tgt - 1]
        lut = np.arange(65536, dtype=np.uint16)
        lut[frm] = to
        o = img.copy()
        for a, n in regs:
            s = (a - lo) // 2
            o[s:s + n] = lut[img[s:s + n]]
        return o

    def key(k, hold=12_000_000, after=25_000_000):
        dev.panel_frame = panl.build_frame(keys=[k])
        c.run(limit=hold)
        dev.panel_frame = panl.build_frame()
        c.run(limit=after)

    def turn(n):
        for _ in range(abs(n)):
            mem.w16(panl.ROTARY_COUNTER,
                    (mem.r16(panl.ROTARY_COUNTER) + (1 if n > 0 else -1)) & 0xFFFF)
            c.run(limit=15_000_000)

    def row0():
        L = find_list(mem)
        if L is None:
            return None
        r = L + 0x118 + mem.r8(MSST) * 0x10C + 12
        t = ""
        for i in range(40):
            ch = mem.r16(r + 2 * i)
            if not ch:
                break
            t += {0xFFFA: "[", 0xFFFB: "]"}.get(ch, chr(ch))
        return t

    def open_menu():
        key("MenuUtility")
        if row0() is None:                 # not at browse top: go there
            key("Back")
            key("Browse")
            key("MenuUtility")
        return row0()

    ok = True
    first = open_menu()
    print("row before:", first)
    ok &= bool(first) and first.endswith("OFF")
    # SAFETY GUARD test: fake "firmware data" in one artwork chunk -> that
    # chunk must be skipped (left exactly as is) on every switch
    S_set = set(S.tolist())                # a block that holds theme colours
    ga = gw = None
    for a_, n_ in sorted(regs, key=lambda r: -r[1]):
        k0 = (a_ - lo) // 2
        for k in range(0, n_ - TS.CHUNK, TS.CHUNK):
            if any(int(x) in S_set for x in ram_words()[k0 + k:k0 + k + TS.CHUNK]):
                ga, gw = a_ + 2 * k, k0 + k
                break
        if ga:
            break
    mem.w16(ga + 2 * 5, 0xBEEF)
    guard = slice(gw, gw + TS.CHUNK)
    guard_before = ram_words()[guard].copy()
    stock = ram_words()
    # blocks whose RAM differs from the stock image (firmware data + our
    # planted guard): the deck code must leave exactly these alone
    import theme_deck as TD
    sw = wmod[(lo - 0x08000000) // 2:(hi - 0x08000000) // 2].copy()   # stock (+ lit privates)
    sw[~mask] = 0
    frozen = np.zeros(len(stock), bool)
    nfrozen = 0
    for a_, n_ in regs:
        k0 = (a_ - lo) // 2
        for k in range(0, n_, TS.CHUNK):
            sl = slice(k0 + k, k0 + min(n_, k + TS.CHUNK))
            if (stock[sl] != sw[sl]).any():
                frozen[sl] = True
                nfrozen += 1
    print(f"blocks differing from stock (must be skipped): {nfrozen}")
    img, cur = stock, 0
    hdr = TS.TS_DATA
    for step in range(len(names) + 1):
        if step:
            r = open_menu()
            want = names[cur - 1].upper()[:15] if cur else "OFF"
            print(f"  row shows: {r}")
            ok &= bool(r) and r.endswith(want)
        turn(mem.r8(MSST))
        tgt = (cur + 1) % (len(names) + 1)
        key("RotaryPush", after=1_000_000)
        n = 0                              # recolour runs inside the push
        while (mem.r8(hdr + 34) or mem.r8(hdr + 32) != tgt) and n < 100:
            c.run(limit=20_000_000)
            n += 1
        print(f"  recolour done within ~{(n + 1) * 20}M emulated steps")
        c.run(limit=25_000_000)
        exp = model(img, cur, tgt)
        exp[frozen] = img[frozen]          # touched blocks: untouched
        got = ram_words()
        bad = int((got != exp).sum())
        print(f"push {step + 1}: {cur} -> {tgt} ({names[tgt - 1] if tgt else 'OFF'}): "
              f"cur={mem.r8(hdr + 32)} switches={mem.r32(hdr + 36)} "
              f"busy={mem.r8(hdr + 34)} px changed={int((got != img).sum()):,} "
              f"mismatch vs model={bad}")
        ok &= bad == 0 and mem.r8(hdr + 32) == tgt and mem.r8(hdr + 34) == 0
        if bad:
            idx = np.nonzero(got != exp)[0][:8]
            print("   mismatch at", [(hex(lo + 2 * int(k)), hex(int(img[k])),
                                      hex(int(exp[k])), hex(int(got[k])))
                                     for k in idx])
        img, cur = got, tgt
        fb_png(m, f"{out}/step{step + 1}_{names[tgt - 1] if tgt else 'OFF'}.png")
    end = ram_words()
    g_ok = bool(np.array_equal(end[guard], guard_before))
    skipped = mem.r32(hdr + 48)
    print(f"guard chunk untouched: {g_ok}; chunks skipped: {skipped} "
          f"(at most {nfrozen} per switch, >= 1 per switch)")
    ok &= g_ok and len(names) + 1 <= skipped <= nfrozen * (len(names) + 1)
    back = bool(np.array_equal(end, stock))
    print("back to stock byte-exact:", back)
    if not back:
        idx = np.nonzero(end != stock)[0]
        print(f"   {len(idx)} px differ, e.g.",
              [(hex(lo + 2 * int(k)), hex(int(stock[k])), hex(int(end[k])))
               for k in idx[:8]])
    ok &= back
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
