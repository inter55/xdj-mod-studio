"""decode.py -- C674x instruction decode for the XDJ-700 DSP emulator.

Decoding is delegated to GNU objdump's tic6x disassembler (work/tic6x/
objdump, tools/build_tic6x_objdump.sh): it implements TI's full encoding
table incl. compact 16-bit instructions in header-based fetch packets, so
this module only has to turn its text into structured records. Ranges are
decoded at their TRUE DSP address, 32-byte aligned (fetch packets are
32-byte aligned; a misaligned start decodes headers as garbage).

Insn fields:
  addr, size (2/4), par (executes in parallel with the PREVIOUS insn),
  cond (None | (reg, negate)), mnem, unit ('S2', 'L1X', 'D2T1', ...),
  side (1/2 or 0), cross (bool), ops (list of operand strings), text,
  header (True for a fetch-packet header word: never executed)
"""
import os
import re
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


_EXE = "objdump.exe" if os.name == "nt" else "objdump"


def _objdump():
    env = os.environ.get("XDJ_TIC6X_OBJDUMP")      # explicit override
    if env and os.path.exists(env):
        return env
    try:                                    # frozen app: bundled repo copy
        from emu.paths import repo_root
        p = os.path.join(repo_root(), "work", "tic6x", _EXE)
        if os.path.exists(p):
            return p
    except Exception:
        pass
    p = os.path.abspath(os.path.join(HERE, "..", "..", "work", "tic6x", _EXE))
    if os.path.exists(p):
        return p
    import shutil                           # a binutils tic6x-elf on PATH
    for n in ("tic6x-elf-objdump", "tic6x-objdump"):
        w = shutil.which(n)
        if w:
            return w
    return p


OBJDUMP = _objdump()
HOWTO = ("the TI C6x disassembler (GNU objdump for tic6x) is missing: run "
         "tools/build_tic6x_objdump.sh (builds work/tic6x/objdump from "
         "binutils, about 10 minutes) or set XDJ_TIC6X_OBJDUMP")


def available():
    """True when the decoder can run (objdump present and executable)."""
    return os.path.isfile(OBJDUMP) and os.access(OBJDUMP, os.X_OK)

_LINE = re.compile(r"^\s*([0-9a-f]+):\s+([0-9a-f]{4}(?:[0-9a-f]{4})?)\s+(.*)$")


class Insn:
    __slots__ = ("addr", "size", "par", "cond", "mnem", "unit", "side",
                 "cross", "ops", "text", "header", "raw", "x", "prot")

    def __repr__(self):
        return f"<{self.addr:#010x} {'|| ' if self.par else ''}{self.text}>"


def _split_ops(s):
    """operands separated by commas, but not inside (..) or [..]"""
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def parse_line(addr, raw, text):
    i = Insn()
    i.addr, i.raw, i.x = addr, raw, None
    i.size = len(raw) // 2
    i.header = text.startswith("<fetch packet header")
    i.par, i.cond, i.unit, i.side, i.cross, i.ops = False, None, "", 0, False, []
    i.text = text.strip()
    # compact fetch packet with PROT=1: objdump appends " || nop 5" to every
    # member; the real meaning is "4 NOP cycles after each LD" (SPRUFE8B
    # Table 3-14) -> flag it, isa.py adds the cycles to loads only
    i.prot = i.text.endswith(" || nop 5")
    if i.prot:
        i.text = i.text[:-len(" || nop 5")]
    t = i.text
    if i.header or t.startswith("<undefined") or t.startswith(".byte"):
        i.mnem = "header" if i.header else "undefined"
        return i
    if t.startswith("||"):
        i.par = True
        t = t[2:].strip()
    m = re.match(r"^\[(!?)([ab]\d+)\]\s*", t)
    if m:
        i.cond = (m.group(2), m.group(1) == "!")
        t = t[m.end():]
    parts = t.split(None, 1)
    i.mnem = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    m = re.match(r"^\.([LSMD])([12])(X?)(T[12])?\s*", rest)
    if m:
        i.unit = m.group(0).strip()[1:]
        i.side = int(m.group(2))
        i.cross = bool(m.group(3))
        rest = rest[m.end():]
    i.ops = _split_ops(rest) if rest else []
    return i


def decode_bytes(blob, vma):
    """{addr: Insn} for blob placed at DSP address vma (vma % 32 == 0)."""
    assert vma % 32 == 0, "decode ranges must start on a fetch packet"
    if not available():
        raise FileNotFoundError(f"{HOWTO} (looked at {OBJDUMP})")
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        f.write(blob)
        name = f.name
    try:
        out = subprocess.run([OBJDUMP, "-D", "-z", "-EL", "-b", "binary", "-m", "tic6x",
                              f"--adjust-vma={vma:#x}", name],
                             capture_output=True, text=True, check=True).stdout
    finally:
        os.unlink(name)
    res = {}
    for line in out.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        a = int(m.group(1), 16)
        res[a] = parse_line(a, m.group(2), m.group(3))
    _fix_rs_mv(res)
    _fix_rs_pdst(res)
    return res


def _fix_rs_mv(res):
    """binutils bug (found by cdj-nxs2-qemu): in a compact fetch packet with
    header RS=1 (bit 19) only 3-bit register fields move to A16-A23/B16-B23,
    but objdump also adds 16 to the 5-bit register of the compact moves:
    LSDmvto (w & 0x66 == 0x06, 5-bit src) and LSDmvfr (w & 0x66 == 0x46,
    5-bit dst). E.g. 'mv b22,a19' must be 'mv b6,a19'."""
    hdr = {}
    for a, i in res.items():
        if i.size != 2 or i.mnem != "mv" or len(i.ops) != 2:
            continue
        fp = a & ~31
        if fp not in hdr:
            h = res.get(fp + 28)
            hw = int(h.raw, 16) if h is not None and h.header else 0
            hdr[fp] = hw
        if not hdr[fp] >> 19 & 1:
            continue
        w = int(i.raw, 16)
        k = 0 if (w & 0x66) == 0x06 else 1 if (w & 0x66) == 0x46 else None
        if k is None:
            continue
        m = re.match(r"^([ab])(\d+)$", i.ops[k])
        if not m or int(m.group(2)) < 16:
            continue
        i.ops[k] = f"{m.group(1)}{int(m.group(2)) - 16}"
        i.text = i.text.replace(f"{m.group(0)}", i.ops[k], 1) if k == 0 else \
            i.text[::-1].replace(m.group(0)[::-1], i.ops[k][::-1], 1)[::-1]


_PDST_OPS = {"cmpeq", "cmplt", "cmpgt", "cmpltu", "cmpgtu", "and", "or", "xor"}


def _fix_rs_pdst(res):
    """binutils bug #2 (2026-10-18): the compact .L compares / logic ops in
    formats L2c (dst = bit 4), Lx3c and Lx1c (dst = bit 11) can only write
    A0/A1/B0/B1 ("dst = A0, A1 or B0, B1 as selected by dst and s",
    SPRUFE8B figs D-7, D-9, D-10) -- the header's RS bit does not apply to
    that field, but objdump adds 16 anyway. E.g. 0x2927 in an RS=1 packet is
    'cmpeq .L2 1,b18,b1', not '...,b17'. Pioneer's PCM unpacker
    (0xC0016B08 SPLOOP) computes its load predicates this way; with the
    wrong dst it clobbered a pointer and read address 0x2: WAV/AIFF froze."""
    hdr = {}
    for a, i in res.items():
        if i.size != 2 or i.mnem not in _PDST_OPS or len(i.ops) != 3:
            continue
        w = int(i.raw, 16)
        if (w & 0x47E) == 0x026:            # Lx3c / Lx1c
            bit = w >> 11 & 1
        elif (w & 0x40E) == 0x408:          # L2c
            bit = w >> 4 & 1
        else:
            continue
        fp = a & ~31
        if fp not in hdr:
            h = res.get(fp + 28)
            hdr[fp] = int(h.raw, 16) if h is not None and h.header else 0
        if not hdr[fp] >> 19 & 1:
            continue
        m = re.match(r"^([ab])(\d+)$", i.ops[2])
        if not m or int(m.group(2)) < 16:
            continue
        new = f"{m.group(1)}{bit}"
        if int(m.group(2)) != 16 + bit:
            continue                           # not the +16 pattern: leave it
        i.ops[2] = new
        i.text = i.text[::-1].replace(m.group(0)[::-1], new[::-1], 1)[::-1]
