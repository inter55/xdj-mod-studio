"""isa.py -- C674x instruction semantics for cpu.py (TI SPRUFE8B).

compile_insn(cpu, insn) -> f(cpu, cyc) -> nop_cycles|None. Handlers only
READ cpu.r at issue and SCHEDULE writes (cpu.wr(w, idx, val)) so that all
members of an execute packet read before any write, and results land at
the manual's write cycle (Table 3-8 / per-instruction "Delay Slots").

Operand text = GNU objdump tic6x syntax (TI order: src1/src2/dst; for the
"src2 first" instructions -- ADDA*/SUBA*/EXT*/SET/CLR/SHL/SHR*/SSHL/ROTL --
objdump also prints src2 first). Memory operands: (n) = byte offset,
[n] = scaled by the access size (always for register offsets).
"""
from __future__ import annotations

import math
import re
import struct

MASK = 0xFFFFFFFF


class Unimplemented(Exception):
    pass


# ----------------------------------------------------------------- helpers
def s32(v):
    v &= MASK
    return v - (1 << 32) if v & 0x80000000 else v


def s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def s8(v):
    v &= 0xFF
    return v - 0x100 if v & 0x80 else v


def sext(v, bits):
    v &= (1 << bits) - 1
    return v - (1 << bits) if v >> (bits - 1) & 1 else v


def sat32(v):
    return 0x7FFFFFFF if v > 0x7FFFFFFF else (-0x80000000 if v < -0x80000000 else v)


def sat16(v):
    return 0x7FFF if v > 0x7FFF else (-0x8000 if v < -0x8000 else v)


def satu8(v):
    return 255 if v > 255 else (0 if v < 0 else v)


def satu16(v):
    return 0xFFFF if v > 0xFFFF else (0 if v < 0 else v)


def lo16(v):
    return v & 0xFFFF


def hi16(v):
    return (v >> 16) & 0xFFFF


def bytes4(v):
    return [(v >> (8 * k)) & 0xFF for k in range(4)]


def from_bytes4(b):
    return (b[0] & 0xFF) | (b[1] & 0xFF) << 8 | (b[2] & 0xFF) << 16 | (b[3] & 0xFF) << 24


def from_h2(lo, hi):
    return (lo & 0xFFFF) | (hi & 0xFFFF) << 16


# --- IEEE helpers (C67x: denormal inputs read as 0, denormal results 0) ---
def f32(bits):
    bits &= MASK
    if (bits >> 23) & 0xFF == 0:              # zero / denormal -> signed 0
        return -0.0 if bits >> 31 else 0.0
    return struct.unpack("<f", struct.pack("<I", bits))[0]


def b32(x):
    """python float -> single bits, round-to-nearest, denormal -> 0"""
    try:
        b = struct.unpack("<I", struct.pack("<f", x))[0]
    except OverflowError:
        b = 0xFF800000 if x < 0 else 0x7F800000
    if (b >> 23) & 0xFF == 0:
        b &= 0x80000000
    return b


def f64(lo, hi):
    bits = (hi & MASK) << 32 | (lo & MASK)
    if (bits >> 52) & 0x7FF == 0:
        return -0.0 if bits >> 63 else 0.0
    return struct.unpack("<d", struct.pack("<Q", bits))[0]


def b64(x):
    try:
        b = struct.unpack("<Q", struct.pack("<d", x))[0]
    except OverflowError:
        b = 0xFFF0000000000000 if x < 0 else 0x7FF0000000000000
    if (b >> 52) & 0x7FF == 0:
        b &= 1 << 63
    return b & MASK, b >> 32


def round_mode(cpu, side):
    """FADCR rounding field for the unit side (0 nearest, 1 zero, 2 +inf,
    3 -inf). Bits 9-10 for .L1/.S1 (side A), 25-26 for side B."""
    v = cpu.c.get("fadcr", 0)
    return (v >> (25 if side == 2 else 9)) & 3


def to_int(x, mode):
    if math.isnan(x):
        return 0x80000000
    if mode == 0:
        r = round(x)                 # python rounds half to even, like IEEE
    elif mode == 1:
        r = math.trunc(x)
    elif mode == 2:
        r = math.ceil(x)
    else:
        r = math.floor(x)
    if r > 0x7FFFFFFF:
        return 0x7FFFFFFF
    if r < -0x80000000:
        return 0x80000000
    return r & MASK


# ----------------------------------------------------------------- operands
REGRE = re.compile(r"^([ab])(\d+)$")


def reg(s):
    m = REGRE.match(s)
    if not m:
        raise Unimplemented(f"not a register: {s!r}")
    return (0 if m.group(1) == "a" else 32) + int(m.group(2))


def is_reg(s):
    return bool(REGRE.match(s))


def pair(s):
    """'a5:a4' -> (lo_idx, hi_idx); also a single even register name."""
    if ":" in s:
        h, l = s.split(":")
        return reg(l), reg(h)
    r = reg(s)
    return r, r + 1


def const(s):
    return int(s, 0)


def src(s):
    """compiled reader for a register or constant operand"""
    if is_reg(s):
        i = reg(s)
        return lambda cpu: cpu.r[i]
    v = const(s) & MASK
    return lambda cpu: v


MEMRE = re.compile(r"^\*(\+\+|--|\+|-)?([ab]\d+)(\+\+|--)?(?:([\[(])([^\])]+)[\])])?$")


def memop(s, size):
    """-> fn(cpu) returning (address, base_idx, new_base or None)."""
    m = MEMRE.match(s)
    if not m:
        raise Unimplemented(f"memory operand {s!r}")
    pre, base, post, br, off = m.groups()
    b = reg(base)
    if off is None:
        offf = lambda cpu: 0
    elif is_reg(off):
        oi = reg(off)
        scale = size if br == "[" else 1
        offf = lambda cpu: cpu.r[oi] * scale
    else:
        n = int(off, 0) * (size if br == "[" else 1)
        offf = lambda cpu: n

    def addr(cpu):
        bv = cpu.r[b]
        o = offf(cpu)
        if post:
            nb = (bv + o) & MASK if post == "++" else (bv - o) & MASK
            return bv, b, nb
        if pre == "+":
            return (bv + o) & MASK, b, None
        if pre == "-":
            return (bv - o) & MASK, b, None
        if pre == "++":
            nb = (bv + o) & MASK
            return nb, b, nb
        if pre == "--":
            nb = (bv - o) & MASK
            return nb, b, nb
        return bv, b, None          # plain *R
    return addr


# ----------------------------------------------------------------- timing
# write cycle offsets per instruction (Table 3-8 + per-page "Delay Slots")
D4 = {  # four-cycle: result written at i+3
    "mpy32", "mpy32u", "mpy32us", "mpy32su", "mpysp", "addsp", "subsp",
    "intsp", "intspu", "spint", "sptrunc", "dpint", "dptrunc", "dpsp",
    "mpy2", "mpy2ir", "smpy2", "mpyu4", "mpysu4", "mpyus4", "dotp2", "dotpn2",
    "dotprsu2", "dotpnrsu2", "dotprus2", "dotpnrus2", "dotpsu4", "dotpus4",
    "dotpu4", "ddotp4", "ddotph2", "ddotph2r", "ddotpl2", "ddotpl2r", "gmpy4",
    "gmpy", "mpyhi", "mpyli", "mpyhir", "mpylir", "mpyih", "mpyil", "mpyihr",
    "mpyilr", "smpy32", "cmpy", "cmpyr", "cmpyr1", "xormpy", "mpyid",
    "mvd",
}
D1 = {  # two-cycle / 16x16 multiply: written at i+1
    "mpy", "mpyu", "mpyus", "mpysu", "mpyh", "mpyhu", "mpyhus", "mpyhsu",
    "mpyhl", "mpyhlu", "mpyhuls", "mpyhslu", "mpylh", "mpylhu", "mpyluhs",
    "mpylshu", "smpy", "smpyh", "smpyhl", "smpylh", "avg2", "avgu4", "rotl",
    "bitc4", "bitr", "deal", "shfl", "xpnd2", "xpnd4", "shfl3",
}


# ----------------------------------------------------------------- SPLOOP
SPLX = 1 << 9          # TSR.SPLX: loop buffer active / interrupted inside one
REFILL = int(__import__("os").environ.get("XDJ_SPLOOP_REFILL", "6"))


class SPLoop:
    """Loop buffer per SPRUFE8B ch.7 (non-nested forms; reload -> Unimplemented)."""

    def __init__(self, kind, ii, wcond, cpu, cyc, addr=None):
        # §7.13.2: returning from an interrupt into the loop (TSR.SPLX=1)
        self.resume = bool(cpu.c["tsr"] & SPLX)
        cpu.c["tsr"] |= SPLX
        if self.resume and kind == "sploopd":
            kind = "sploop"                 # SPLOOPD executes as SPLOOP
        self.addr = addr
        self.int_drain = False
        self.kind, self.ii, self.wcond = kind, ii, wcond
        self.active = True
        self.loading = True         # program memory feeds + loads the buffer
        self.fetch_prog = True      # program memory fetch enabled
        self.lc = 0                 # loading counter (cycles since SPLOOP)
        self.lbc = 0
        self.buf = []               # [ (lc, insn, unit, compiled) ]
        self.valid = set()          # lc values currently valid
        self.dynlen = None
        self.drain = None           # draining counter when draining
        self.post_delay = None      # cycles after drain start to refetch
        self.pc_after = None
        self.started = cyc
        self.since = 0              # cycles since SPLOOP (for SPLOOPD guard)
        self.term_hist = []         # SPLOOPW predicate history
        self.initial_term = False
        self.prog_stall = 0
        self._pending_valid = []
        self.refill = None
        if kind == "sploop":
            ilc = cpu.c["ilc"]
            if ilc == 0:
                # §7.9.1: zero-trip loop. Non-SPMASKed body instructions
                # execute as NOPs and are stored disabled; the buffer goes
                # idle at the last loading stage boundary.
                self.initial_term = True
            else:
                cpu.c["ilc"] = ilc - 1

    def unit_of(self, i):
        return (i.unit[:1] + i.unit[1:2]) if i.unit else ""

    def cycle(self, cpu, cyc):
        """one cycle while the loop buffer is active"""
        mask = set()
        prog_ep = []
        nxt = None
        if self.fetch_prog and self.prog_stall == 0:
            prog_ep, nxt = cpu.execute_packet(cpu.pc)
            for i in prog_ep:
                if i.mnem == "spmaskr":
                    raise Unimplemented("SPMASKR (nested reload)")
                if i.mnem == "spmask":
                    for u in ",".join(i.ops).split(","):
                        mask.add(u.strip().upper())
            cpu.pc = nxt
        elif self.prog_stall:
            self.prog_stall -= 1
        # draining: invalidate (in loading order) before fetching this LBC
        if self.drain is not None:
            for lc in [lc for lc in self.valid if lc == self.drain]:
                self.valid.discard(lc)
        bufexec = [(lc, i, u, f) for lc, i, u, f in self.buf
                   if lc in self.valid and lc % self.ii == self.lbc
                   and (self.resume or u not in mask)]
        nop = 1
        loading_now = []
        kernel_insn = None
        for i in prog_ep:
            if i.mnem in ("spmask", "spkernel", "spkernelr"):
                if i.mnem == "spkernelr":
                    raise Unimplemented("SPKERNELR (nested reload)")
                if i.mnem == "spkernel" and self.loading:
                    kernel_insn = i          # its EP is still loaded
                continue
            f = cpu.ops.get(i.addr)
            if f is None:
                f = cpu.ops[i.addr] = compile_insn(cpu, i)
            u = self.unit_of(i)
            masked = u in mask or (i.mnem == "bnop" and is_disp(i)) or i.mnem == "nop"
            if self.resume and (u in mask or (i.mnem == "bnop" and is_disp(i))):
                # §7.13.2: SPMASKed program instructions act as NOPs, BNOP
                # label,n as NOP n+1
                n = int(i.ops[1], 0) + 1 if i.mnem == "bnop" else None
                cpu.insn_count += 1
                if n and n > nop:
                    nop = n
                continue
            if self.initial_term and not masked:
                n = int(i.ops[0], 0) if (i.mnem == "nop" and i.ops) else None
            else:
                n = f(cpu, cyc)
            if i.mnem == "nop":
                n = int(i.ops[0], 0) if i.ops else 1
            cpu.insn_count += 1
            if n and n > nop:
                nop = n
            if self.loading and not masked and not self.initial_term:
                loading_now.append((self.lc, i, u, f))
        for lc, i, u, f in bufexec:
            f(cpu, cyc)
            cpu.insn_count += 1
        cpu.ep_count += 1
        self.buf.extend(loading_now)
        self._pending_valid = [e[0] for e in loading_now]
        if kernel_insn is not None:      # last EP of the body: now stop loading
            self.on_kernel(cpu, kernel_insn, nxt)
        if nop > 1:
            # NOP n from program memory: n cycles with no program fetch,
            # while the buffer keeps issuing every cycle
            self.prog_stall = nop - 1
            cpu.stall = 0
        self.end_cycle(cpu, cyc)

    def on_kernel(self, cpu, i, nxt):
        self.loading = False
        self.fetch_prog = False
        self.dynlen = self.lc + 1
        self.pc_after = nxt
        f = [int(x, 0) for x in i.ops] if i.ops else [0, 0]
        while len(f) < 2:
            f.append(0)
        self.post_delay = f[0] * self.ii + f[1]

    def end_cycle(self, cpu, cyc):
        for lc in getattr(self, "_pending_valid", []):
            self.valid.add(lc)
        self._pending_valid = []
        if self.loading:
            self.lc += 1
        self._boundary_step(cpu, cyc)

    def _exit_fetch(self, cpu):
        """program fetch restarts after the loop buffer had it off: the
        post-loop packet reaches E1 REFILL cycles later (fetch pipeline
        PG..DC refill; cdj-nxs2-qemu measured 6 over all 273 SPLOOPs)"""
        if REFILL:
            self.refill = REFILL
        else:
            self.fetch_prog = True
            cpu.pc = self.pc_after

    def _leave(self, cpu):
        """buffer done: post-loop code after the remaining refill"""
        self.active = False
        cpu.sp = None
        cpu.c["tsr"] &= ~SPLX
        if not self.fetch_prog:
            cpu.pc = self.pc_after
            left = REFILL if self.refill is None else self.refill
            if left > 0:
                cpu.stall = left

    def _boundary_step(self, cpu, cyc):
        self.since += 1
        if self.refill is not None and not self.fetch_prog:
            self.refill -= 1
            if self.refill <= 0:
                self.fetch_prog = True
                cpu.pc = self.pc_after
        if self.drain is not None:
            self.drain += 1
            if (self.post_delay is not None and not self.fetch_prog and not self.int_drain
                    and self.refill is None and self.drain >= self.post_delay):
                self._exit_fetch(cpu)
            if self.dynlen is not None and self.drain >= self.dynlen - self.ii:
                self.valid.clear()
                if self.int_drain:
                    # §7.13.4: drained for an interrupt -> it is taken with
                    # IRP = the SPLOOP packet once pending writes complete
                    self.active = False
                    cpu.sp = None
                    cpu.c["tsr"] &= ~SPLX
                    cpu.pc = self.addr
                    cpu.splx_irq = self.pc_after
                    return
                self._leave(cpu)
                return
        if self.wcond is not None:
            self.term_hist.append(cpu.cond(self.wcond))
        self.lbc += 1
        if self.lbc < self.ii:
            return
        self.lbc = 0
        # stage boundary: termination test
        if self.initial_term:
            if not self.loading:             # last loading stage boundary
                self._leave(cpu)
            return
        if self.drain is not None:
            return
        if self._want_int(cpu):
            self.drain = 0
            self.int_drain = True
            return
        if self.kind == "sploopw":
            # termination = the predicate evaluates FALSE 3 cycles before the
            # stage boundary (SPRUFE8B 7.10.2; a "while (cond)" loop). This
            # was inverted until 2026-10-13: Pioneer's delay loop
            # 0xC004BEE0 ([b0] sploopw 14, b0 = i < n) quit after one pass;
            # found by replaying a cdj-nxs2-qemu recording.
            hist = self.term_hist
            term = (not hist[-4]) if len(hist) >= 4 else False
            if term:
                self.valid.clear()                 # abrupt: no epilog
                if not self.loading:
                    self._leave(cpu)
                else:
                    self.active = False
                    cpu.sp = None
                    cpu.c["tsr"] &= ~SPLX
                # still loading: the first pass simply continues from
                # program memory (rest of body, SPKERNEL = no-op), unbuffered
            return
        if self.kind == "sploopd" and self.since <= 3:
            return
        ilc = cpu.c["ilc"]
        if ilc == 0:
            # (early exit §7.8.2 when still loading: draining starts while
            # program memory keeps feeding/loading up to SPKERNEL)
            self.drain = 0
            if self.post_delay == 0:
                self._exit_fetch(cpu)
        else:
            cpu.c["ilc"] = ilc - 1

    def _want_int(self, cpu):
        """§7.13.1: begin interrupt draining at this stage boundary?"""
        c = cpu.c
        if self.loading or self.addr is None or self.initial_term:
            return False
        if not (c["csr"] & 1) or not (c["ier"] & 2) or not (c["ifr"] & c["ier"] & 0xFFF0):
            return False
        if cpu.pend:
            return False
        if self.kind in ("sploopd", "sploopw") and self.since <= 3:
            return False
        if self.kind == "sploopw":
            hist = self.term_hist
            if len(hist) >= 4 and not hist[-4]:
                return False                    # terminating anyway
            return True
        dl = self.dynlen or 0
        return c["ilc"] >= -(-dl // self.ii)    # ILC >= ceil(dynlen/ii)

    def on_branch(self, cpu):
        # §7.7.3.2: the buffer goes idle after the last delay slot of a
        # taken branch (we do not support reload, so always)
        self.active = False
        cpu.sp = None
        cpu.c["tsr"] &= ~SPLX


def is_disp(i):
    return bool(i.ops) and i.ops[0].startswith("0x")


# ----------------------------------------------------------------- compile
def compile_insn(cpu, i):
    m = i.mnem
    if m in ("undefined", "header"):
        raise Unimplemented(f"executing {m} at {i.addr:#010x}")
    h = HANDLERS.get(m)
    if h is None:
        raise Unimplemented(f"{m} ({i.text}) at {i.addr:#010x}")
    body = h(i)
    if getattr(i, "prot", False) and m in LDSZ:
        inner = body

        def body(cpu, cyc, inner=inner):     # protected load: +4 NOP cycles
            n = inner(cpu, cyc)
            return max(n or 1, 5)
    c = i.cond
    if c is None or m == "sploopw":      # SPLOOPW's predicate = exit test
        return body
    if m in ("bnop", "callp", "addkpc"):
        # NOP part executes regardless of the predicate
        def pred_nop(cpu, cyc, body=body, c=c, i=i):
            if cpu.cond(c):
                return body(cpu, cyc)
            return int(i.ops[-1], 0) + 1 if m != "callp" else 6
        return pred_nop

    def pred(cpu, cyc, body=body, c=c):
        if cpu.cond(c):
            return body(cpu, cyc)
        return None
    return pred


HANDLERS = {}


def H(*names):
    def deco(fn):
        for n in names:
            HANDLERS[n] = fn
        return fn
    return deco


def w_at(i):
    m = i.mnem
    if m in D4:
        return 3
    if m in D1:
        return 1
    return 0


def simple(fn):
    """3-operand integer op: fn(a, b) with ops (src1, src2, dst)"""
    def make(i):
        a, b = src(i.ops[0]), src(i.ops[1])
        d = reg(i.ops[2])
        w = w_at(i)

        def run(cpu, cyc):
            cpu.wr(cyc + w, d, fn(a(cpu), b(cpu)))
        return run
    return make


def two(fn):
    """2-operand op: (src, dst)"""
    def make(i):
        a = src(i.ops[0])
        d = reg(i.ops[1])
        w = w_at(i)

        def run(cpu, cyc):
            cpu.wr(cyc + w, d, fn(a(cpu)))
        return run
    return make


def long_dst(fn):
    """op with a 64-bit (pair) destination: fn(a, b) -> (lo, hi)"""
    def make(i):
        a, b = src(i.ops[0]), src(i.ops[1])
        lo, hi = pair(i.ops[2])
        w = w_at(i)

        def run(cpu, cyc):
            l, h_ = fn(a(cpu), b(cpu))
            cpu.wr(cyc + w, lo, l)
            cpu.wr(cyc + w, hi, h_)
        return run
    return make


# ---- moves / constants ----
@H("nop")
def _nop(i):
    n = int(i.ops[0], 0) if i.ops else 1
    return lambda cpu, cyc: n


@H("mvk", "mvkl")
def _mvk(i):
    v = const(i.ops[0])
    d = reg(i.ops[1])
    if i.mnem == "mvk":
        v = v & MASK          # objdump prints the sign-extended value
    else:
        v = sext(v, 16) & MASK
    return lambda cpu, cyc: cpu.wr(cyc, d, v)


@H("mvkh", "mvklh")
def _mvkh(i):
    v = const(i.ops[0]) & MASK
    d = reg(i.ops[1])
    if i.mnem == "mvklh":
        hi = (v & 0xFFFF) << 16
    else:
        hi = v & 0xFFFF0000     # objdump prints the full 32-bit constant
    return lambda cpu, cyc: cpu.wr(cyc, d, hi | (cpu.r[d] & 0xFFFF))


@H("mv", "dmv")
def _mv(i):
    if i.mnem == "dmv":         # DMV src1, src2, dst_o:dst_e
        a, b = src(i.ops[0]), src(i.ops[1])
        lo, hi = pair(i.ops[2])

        def run(cpu, cyc):
            cpu.wr(cyc, hi, a(cpu))
            cpu.wr(cyc, lo, b(cpu))
        return run
    if ":" in i.ops[0]:          # 64-bit move (MV of a pair? -> DMV form)
        sl, sh = pair(i.ops[0])
        dl, dh = pair(i.ops[1])

        def run(cpu, cyc):
            cpu.wr(cyc, dl, cpu.r[sl])
            cpu.wr(cyc, dh, cpu.r[sh])
        return run
    a = src(i.ops[0])
    d = reg(i.ops[1])
    return lambda cpu, cyc: cpu.wr(cyc, d, a(cpu))


@H("zero")
def _zero(i):
    if ":" in i.ops[0]:
        lo, hi = pair(i.ops[0])

        def run(cpu, cyc):
            cpu.wr(cyc, lo, 0)
            cpu.wr(cyc, hi, 0)
        return run
    d = reg(i.ops[0])
    return lambda cpu, cyc: cpu.wr(cyc, d, 0)


@H("mvc")
def _mvc(i):
    s, d = i.ops[0], i.ops[1]
    if is_reg(s):                # GPR -> control register
        a = reg(s)
        name = d.lower()

        def run(cpu, cyc):
            v = cpu.r[a]
            if name in ("ilc", "rilc"):
                cpu.wr_ctrl(cyc + 3, name, v)        # 4-cycle latency
            elif name == "icr":                      # clear IFR bits
                (cpu.ifr_and(~v & MASK) if hasattr(cpu, "ifr_and") else
                 cpu.c.__setitem__("ifr", cpu.c["ifr"] & ~v & MASK))
            elif name == "isr":                      # set IFR bits
                (cpu.ifr_or(v) if hasattr(cpu, "ifr_or") else
                 cpu.c.__setitem__("ifr", cpu.c["ifr"] | v))
            else:
                cpu.wr_ctrl(cyc, name, v)
        return run
    name = s.lower()
    d_ = reg(d)

    def run(cpu, cyc):
        if name == "tscl":
            v = cpu.cycle & MASK
            cpu.c["tsch"] = (cpu.cycle >> 32) & MASK
        else:
            v = cpu.c.get(name, 0)
        cpu.wr(cyc, d_, v)
    return run


# ---- integer arithmetic ----
def _arith40(op):
    """ADD/SUB/ADDU/SUBU with optional 40-bit (pair) operands/dst."""
    def make(i):
        o1, o2, od = i.ops
        signed = i.mnem in ("add", "sub")

        def rd(s):
            if ":" in s:
                lo, hi = pair(s)
                return lambda cpu: sext(((cpu.r[hi] & 0xFF) << 32) | cpu.r[lo], 40)
            if is_reg(s):
                r_ = reg(s)
                return (lambda cpu: s32(cpu.r[r_])) if signed else (lambda cpu: cpu.r[r_])
            v = const(s)
            return lambda cpu: v
        a, b = rd(o1), rd(o2)
        if ":" in od:
            lo, hi = pair(od)

            def run(cpu, cyc):
                r_ = op(a(cpu), b(cpu)) & 0xFFFFFFFFFF
                cpu.wr(cyc, lo, r_ & MASK)
                cpu.wr(cyc, hi, (r_ >> 32) & 0xFF)
            return run
        d = reg(od)
        return lambda cpu, cyc: cpu.wr(cyc, d, op(a(cpu), b(cpu)))
    return make


H("add", "addu")(_arith40(lambda a, b: a + b))
H("sub", "subu")(_arith40(lambda a, b: a - b))


@H("addk")
def _addk(i):
    v = sext(const(i.ops[0]), 16)
    d = reg(i.ops[1])
    return lambda cpu, cyc: cpu.wr(cyc, d, cpu.r[d] + v)


def _adda(shift, sub=False):
    def make(i):
        base = src(i.ops[0])        # src2 (base register) first
        off = src(i.ops[1])
        d = reg(i.ops[2])

        def run(cpu, cyc):
            o = off(cpu) << shift
            cpu.wr(cyc, d, base(cpu) - o if sub else base(cpu) + o)
        return run
    return make


H("addab")(_adda(0))
H("addah")(_adda(1))
H("addaw")(_adda(2))
H("addad")(_adda(3))
H("subab")(_adda(0, True))
H("subah")(_adda(1, True))
H("subaw")(_adda(2, True))

H("and")(simple(lambda a, b: a & b))
H("or")(simple(lambda a, b: a | b))
H("xor")(simple(lambda a, b: a ^ b))
H("andn")(simple(lambda a, b: a & ~b))
H("sadd")(simple(lambda a, b: sat32(s32(a) + s32(b))))
H("ssub")(simple(lambda a, b: sat32(s32(a) - s32(b))))
H("add2")(simple(lambda a, b: from_h2(a + b, hi16(a) + hi16(b))))
H("sub2")(simple(lambda a, b: from_h2(a - b, hi16(a) - hi16(b))))
H("add4")(simple(lambda a, b: from_bytes4([x + y for x, y in zip(bytes4(a), bytes4(b))])))
H("sub4")(simple(lambda a, b: from_bytes4([x - y for x, y in zip(bytes4(a), bytes4(b))])))
H("sadd2")(simple(lambda a, b: from_h2(sat16(s16(a) + s16(b)), sat16(s16(a >> 16) + s16(b >> 16)))))
H("ssub2")(simple(lambda a, b: from_h2(sat16(s16(a) - s16(b)), sat16(s16(a >> 16) - s16(b >> 16)))))
H("saddus2")(simple(lambda a, b: from_h2(satu16(lo16(a) + s16(b)), satu16(hi16(a) + s16(b >> 16)))))
H("saddsu2")(simple(lambda a, b: from_h2(satu16(s16(a) + lo16(b)), satu16(s16(a >> 16) + hi16(b)))))
H("saddu4")(simple(lambda a, b: from_bytes4([satu8(x + y) for x, y in zip(bytes4(a), bytes4(b))])))
H("subabs4")(simple(lambda a, b: from_bytes4([abs(x - y) for x, y in zip(bytes4(a), bytes4(b))])))
H("max2")(simple(lambda a, b: from_h2(max(s16(a), s16(b)), max(s16(a >> 16), s16(b >> 16)))))
H("min2")(simple(lambda a, b: from_h2(min(s16(a), s16(b)), min(s16(a >> 16), s16(b >> 16)))))
H("maxu4")(simple(lambda a, b: from_bytes4([max(x, y) for x, y in zip(bytes4(a), bytes4(b))])))
H("minu4")(simple(lambda a, b: from_bytes4([min(x, y) for x, y in zip(bytes4(a), bytes4(b))])))
H("avg2")(simple(lambda a, b: from_h2((s16(a) + s16(b) + 1) >> 1, (s16(a >> 16) + s16(b >> 16) + 1) >> 1)))
H("avgu4")(simple(lambda a, b: from_bytes4([(x + y + 1) >> 1 for x, y in zip(bytes4(a), bytes4(b))])))
H("pack2")(simple(lambda a, b: from_h2(b, a)))
H("packh2")(simple(lambda a, b: from_h2(hi16(b), hi16(a))))
H("packhl2")(simple(lambda a, b: from_h2(b, hi16(a))))
H("packlh2")(simple(lambda a, b: from_h2(hi16(b), a)))
H("packh4")(simple(lambda a, b: from_bytes4([bytes4(b)[1], bytes4(b)[3], bytes4(a)[1], bytes4(a)[3]])))
H("packl4")(simple(lambda a, b: from_bytes4([bytes4(b)[0], bytes4(b)[2], bytes4(a)[0], bytes4(a)[2]])))
H("spack2")(simple(lambda a, b: from_h2(sat16(s32(b)), sat16(s32(a)))))
H("spacku4")(simple(lambda a, b: from_bytes4([satu8(s16(b)), satu8(s16(b >> 16)), satu8(s16(a)), satu8(s16(a >> 16))])))
H("rpack2")(simple(lambda a, b: from_h2(sat32(s32(b) * 2) >> 16 if False else ((sat32(s32(b) << 1) + 0x8000) >> 16),
                                        ((sat32(s32(a) << 1) + 0x8000) >> 16))))


@H("addsub", "saddsub")
def _addsub(i):
    a, b = src(i.ops[0]), src(i.ops[1])
    lo, hi = pair(i.ops[2])
    sat = i.mnem == "saddsub"

    def run(cpu, cyc):
        x, y = s32(a(cpu)), s32(b(cpu))
        s_, d_ = x + y, x - y
        if sat:
            s_, d_ = sat32(s_), sat32(d_)
        cpu.wr(cyc, hi, s_)
        cpu.wr(cyc, lo, d_)
    return run


@H("addsub2", "saddsub2")
def _addsub2(i):
    a, b = src(i.ops[0]), src(i.ops[1])
    lo, hi = pair(i.ops[2])
    sat = i.mnem == "saddsub2"
    f = sat16 if sat else (lambda v: v)

    def run(cpu, cyc):
        x, y = a(cpu), b(cpu)
        cpu.wr(cyc, hi, from_h2(f(s16(x) + s16(y)), f(s16(x >> 16) + s16(y >> 16))))
        cpu.wr(cyc, lo, from_h2(f(s16(x) - s16(y)), f(s16(x >> 16) - s16(y >> 16))))
    return run


@H("abs")
def _abs(i):
    if ":" in i.ops[0]:
        sl, sh = pair(i.ops[0])
        dl, dh = pair(i.ops[1])

        def run(cpu, cyc):
            v = sext(((cpu.r[sh] & 0xFF) << 32) | cpu.r[sl], 40)
            v = min(abs(v), (1 << 39) - 1)
            cpu.wr(cyc, dl, v & MASK)
            cpu.wr(cyc, dh, (v >> 32) & 0xFF)
        return run
    return two(lambda v: sat32(abs(s32(v))))(i)


H("abs2")(two(lambda v: from_h2(sat16(abs(s16(v))), sat16(abs(s16(v >> 16))))))
H("neg")(two(lambda v: -s32(v)))
H("not")(two(lambda v: ~v))
H("swap2")(two(lambda v: from_h2(hi16(v), v)))
H("swap4")(two(lambda v: from_bytes4([bytes4(v)[1], bytes4(v)[0], bytes4(v)[3], bytes4(v)[2]])))
H("unpkhu4")(two(lambda v: from_h2(bytes4(v)[2], bytes4(v)[3])))
H("unpklu4")(two(lambda v: from_h2(bytes4(v)[0], bytes4(v)[1])))
H("xpnd2")(two(lambda v: (0xFFFF if v & 1 else 0) | (0xFFFF0000 if v & 2 else 0)))
H("xpnd4")(two(lambda v: from_bytes4([0xFF if v >> k & 1 else 0 for k in range(4)])))
H("bitc4")(two(lambda v: from_bytes4([bin(x).count("1") for x in bytes4(v)])))
H("bitr")(two(lambda v: int(f"{v & MASK:032b}"[::-1], 2)))


@H("sat")
def _sat(i):
    sl, sh = pair(i.ops[0])
    d = reg(i.ops[1])
    return lambda cpu, cyc: cpu.wr(cyc, d, sat32(sext(((cpu.r[sh] & 0xFF) << 32) | cpu.r[sl], 40)))


def _norm_val(v, bits):
    v = sext(v, bits)
    if v < 0:
        v = ~v
    if v == 0:
        return bits - 1
    return bits - 1 - v.bit_length()


@H("norm")
def _norm(i):
    if ":" in i.ops[0]:
        sl, sh = pair(i.ops[0])
        d = reg(i.ops[1])
        return lambda cpu, cyc: cpu.wr(cyc, d, _norm_val(((cpu.r[sh] & 0xFF) << 32) | cpu.r[sl], 40))
    return two(lambda v: _norm_val(v, 32))(i)


@H("lmbd")
def _lmbd(i):
    a, b = src(i.ops[0]), src(i.ops[1])
    d = reg(i.ops[2])

    def run(cpu, cyc):
        bit = a(cpu) & 1
        v = b(cpu) & MASK
        if not bit:
            v = ~v & MASK
        cpu.wr(cyc, d, 32 if v == 0 else 32 - v.bit_length())
    return run


@H("subc")
def _subc(i):
    a, b = src(i.ops[0]), src(i.ops[1])
    d = reg(i.ops[2])

    def run(cpu, cyc):
        x, y = a(cpu), b(cpu)
        t = (x - y) & MASK
        cpu.wr(cyc, d, ((t << 1) + 1) if x >= y else (x << 1))
    return run


# ---- compares (40-bit forms when src2 is a pair) ----
def _cmp(fn, unsigned=False):
    def make(i):
        def rd(s):
            if ":" in s:
                lo, hi = pair(s)
                if unsigned:
                    return lambda cpu: ((cpu.r[hi] & 0xFF) << 32) | cpu.r[lo]
                return lambda cpu: sext(((cpu.r[hi] & 0xFF) << 32) | cpu.r[lo], 40)
            if is_reg(s):
                r_ = reg(s)
                return (lambda cpu: cpu.r[r_]) if unsigned else (lambda cpu: s32(cpu.r[r_]))
            v = const(s)
            if unsigned:
                v &= 0x1F if i.mnem in ("cmpgtu", "cmpltu") else MASK
            return lambda cpu: v
        a, b = rd(i.ops[0]), rd(i.ops[1])
        d = reg(i.ops[2])
        return lambda cpu, cyc: cpu.wr(cyc, d, 1 if fn(a(cpu), b(cpu)) else 0)
    return make


H("cmpeq")(_cmp(lambda a, b: a == b))
H("cmpgt")(_cmp(lambda a, b: a > b))
H("cmplt")(_cmp(lambda a, b: a < b))
H("cmpgtu")(_cmp(lambda a, b: a > b, True))
H("cmpltu")(_cmp(lambda a, b: a < b, True))
H("cmpeq2")(simple(lambda a, b: (lo16(a) == lo16(b)) | (hi16(a) == hi16(b)) << 1))
H("cmpgt2")(simple(lambda a, b: (s16(a) > s16(b)) | (s16(a >> 16) > s16(b >> 16)) << 1))
H("cmplt2")(simple(lambda a, b: (s16(a) < s16(b)) | (s16(a >> 16) < s16(b >> 16)) << 1))
H("cmpeq4")(simple(lambda a, b: sum((x == y) << k for k, (x, y) in enumerate(zip(bytes4(a), bytes4(b))))))
H("cmpgtu4")(simple(lambda a, b: sum((x > y) << k for k, (x, y) in enumerate(zip(bytes4(a), bytes4(b))))))
H("cmpltu4")(simple(lambda a, b: sum((x < y) << k for k, (x, y) in enumerate(zip(bytes4(a), bytes4(b))))))


# ---- shifts / bit fields ----
def _shiftop(kind):
    def make(i):
        long_src = ":" in i.ops[0]   # 40-bit source pair (b5:b4): never src()
        a = None if long_src else src(i.ops[0])   # src2 first
        b = src(i.ops[1])            # shift amount (src1)
        dst = i.ops[2]
        if long_src:
            sl, sh = pair(i.ops[0])
            a = lambda cpu: ((cpu.r[sh] & 0xFF) << 32) | cpu.r[sl]

        def val(cpu):
            x, n = a(cpu), b(cpu) & 0x3F
            if kind == "shl":
                return x << n
            if kind == "shr":
                return sext(x, 40 if long_src else 32) >> n
            if kind == "shru":
                return (x & ((1 << (40 if long_src else 32)) - 1)) >> n
            raise AssertionError
        if ":" in dst:
            lo, hi = pair(dst)

            def run(cpu, cyc):
                v = val(cpu) & 0xFFFFFFFFFF
                cpu.wr(cyc, lo, v & MASK)
                cpu.wr(cyc, hi, (v >> 32) & 0xFF)
            return run
        d = reg(dst)
        return lambda cpu, cyc: cpu.wr(cyc, d, val(cpu))
    return make


H("shl")(_shiftop("shl"))
H("shr")(_shiftop("shr"))
H("shru")(_shiftop("shru"))


@H("sshl")
def _sshl(i):
    a, b = src(i.ops[0]), src(i.ops[1])
    d = reg(i.ops[2])

    def run(cpu, cyc):
        v, n = s32(a(cpu)), b(cpu) & 0x3F
        r_ = v << n
        cpu.wr(cyc, d, sat32(r_))
    return run


@H("sshvl", "sshvr")
def _sshv(i):
    a, b = src(i.ops[0]), src(i.ops[1])    # src2, src1(shift, signed)
    d = reg(i.ops[2])
    left = i.mnem == "sshvl"

    def run(cpu, cyc):
        v, n = s32(a(cpu)), s32(b(cpu))
        n = max(-31, min(31, n))
        if not left:
            n = -n
        r_ = sat32(v << n) if n >= 0 else v >> (-n)
        cpu.wr(cyc, d, r_)
    return run


H("shr2")(simple(lambda a, b: from_h2(s16(a) >> (b & 0x1F), s16(a >> 16) >> (b & 0x1F))))
H("shru2")(simple(lambda a, b: from_h2(lo16(a) >> (b & 0x1F), hi16(a) >> (b & 0x1F))))
H("shlmb")(simple(lambda a, b: ((b << 8) | (a >> 24)) & MASK))
H("shrmb")(simple(lambda a, b: ((b >> 8) | ((a & 0xFF) << 24)) & MASK))
H("rotl")(simple(lambda a, b: ((a << (b & 31)) | (a >> (32 - (b & 31)))) & MASK if b & 31 else a))


def _field(i):
    """EXT/EXTU/SET/CLR: (src2, csta, cstb, dst) or (src2, src1reg, dst)"""
    if len(i.ops) == 4:
        a = src(i.ops[0])
        csta, cstb = const(i.ops[1]) & 31, const(i.ops[2]) & 31
        return a, (lambda cpu: (csta, cstb)), reg(i.ops[3])
    a, b = src(i.ops[0]), src(i.ops[1])
    return a, (lambda cpu: ((b(cpu) >> 5) & 31, b(cpu) & 31)), reg(i.ops[2])


@H("ext", "extu")
def _ext(i):
    a, cs, d = _field(i)
    signed = i.mnem == "ext"

    def run(cpu, cyc):
        csta, cstb = cs(cpu)
        v = (a(cpu) << csta) & MASK
        v = (s32(v) >> cstb) if signed else (v >> cstb)
        cpu.wr(cyc, d, v)
    return run


@H("set", "clr")
def _setclr(i):
    a, cs, d = _field(i)
    sett = i.mnem == "set"

    def run(cpu, cyc):
        csta, cstb = cs(cpu)
        v = a(cpu)
        if cstb >= csta:
            mask = ((1 << (cstb - csta + 1)) - 1) << csta
            v = (v | mask) if sett else (v & ~mask)
        cpu.wr(cyc, d, v)
    return run


@H("deal")
def _deal(i):
    a = src(i.ops[0])
    d = reg(i.ops[1])

    def run(cpu, cyc):
        v = a(cpu)
        ev = od = 0
        for k in range(16):
            ev |= ((v >> (2 * k)) & 1) << k
            od |= ((v >> (2 * k + 1)) & 1) << k
        cpu.wr(cyc + 1, d, ev | od << 16)
    return run


@H("shfl")
def _shfl(i):
    a = src(i.ops[0])
    d = reg(i.ops[1])

    def run(cpu, cyc):
        v = a(cpu)
        r_ = 0
        for k in range(16):
            r_ |= ((v >> k) & 1) << (2 * k)
            r_ |= ((v >> (16 + k)) & 1) << (2 * k + 1)
        cpu.wr(cyc + 1, d, r_)
    return run


# ---- multiplies (16x16 family: write at i+1) ----
def _m16(fa, fb):
    return simple(lambda a, b: fa(a) * fb(b))


SL = lambda v: s16(v)            # signed low
UL = lambda v: v & 0xFFFF
SH = lambda v: s16(v >> 16)
UH = lambda v: (v >> 16) & 0xFFFF
H("mpy")(_m16(SL, SL))
H("mpyu")(_m16(UL, UL))
H("mpyus")(_m16(UL, SL))
H("mpysu")(_m16(SL, UL))
H("mpyh")(_m16(SH, SH))
H("mpyhu")(_m16(UH, UH))
H("mpyhus")(_m16(UH, SH))
H("mpyhsu")(_m16(SH, UH))
H("mpyhl")(_m16(SH, SL))
H("mpyhlu")(_m16(UH, UL))
H("mpyhuls")(_m16(UH, SL))
H("mpyhslu")(_m16(SH, UL))
H("mpylh")(_m16(SL, SH))
H("mpylhu")(_m16(UL, UH))
H("mpyluhs")(_m16(UL, SH))
H("mpylshu")(_m16(SL, UH))
H("smpy")(simple(lambda a, b: 0x7FFFFFFF if (s16(a) * s16(b)) << 1 == 0x80000000 else (s16(a) * s16(b)) << 1))
H("smpyh")(simple(lambda a, b: 0x7FFFFFFF if (SH(a) * SH(b)) << 1 == 0x80000000 else (SH(a) * SH(b)) << 1))
H("smpyhl")(simple(lambda a, b: 0x7FFFFFFF if (SH(a) * SL(b)) << 1 == 0x80000000 else (SH(a) * SL(b)) << 1))
H("smpylh")(simple(lambda a, b: 0x7FFFFFFF if (SL(a) * SH(b)) << 1 == 0x80000000 else (SL(a) * SH(b)) << 1))


@H("mpy32", "mpy32u", "mpy32us", "mpy32su")
def _mpy32(i):
    a, b = src(i.ops[0]), src(i.ops[1])
    m = i.mnem
    fa = s32 if m in ("mpy32", "mpy32su") else (lambda v: v)
    fb = s32 if m in ("mpy32", "mpy32us") else (lambda v: v)
    if ":" in i.ops[2]:
        lo, hi = pair(i.ops[2])

        def run(cpu, cyc):
            p = (fa(a(cpu)) * fb(b(cpu))) & ((1 << 64) - 1)
            cpu.wr(cyc + 3, lo, p & MASK)
            cpu.wr(cyc + 3, hi, p >> 32)
        return run
    d = reg(i.ops[2])
    return lambda cpu, cyc: cpu.wr(cyc + 3, d, fa(a(cpu)) * fb(b(cpu)))


@H("smpy32")
def _smpy32(i):
    a, b = src(i.ops[0]), src(i.ops[1])
    d = reg(i.ops[2])

    def run(cpu, cyc):
        x, y = s32(a(cpu)), s32(b(cpu))
        if x == y == -0x80000000:
            cpu.wr(cyc + 3, d, 0x7FFFFFFF)
        else:
            cpu.wr(cyc + 3, d, ((x * y) << 1) >> 32)
    return run


@H("mpyi")
def _mpyi(i):
    a, b = src(i.ops[0]), src(i.ops[1])
    d = reg(i.ops[2])
    return lambda cpu, cyc: cpu.wr(cyc + 8, d, s32(a(cpu)) * s32(b(cpu)))


@H("mpyid")
def _mpyid(i):
    a, b = src(i.ops[0]), src(i.ops[1])
    lo, hi = pair(i.ops[2])

    def run(cpu, cyc):
        p = (s32(a(cpu)) * s32(b(cpu))) & ((1 << 64) - 1)
        cpu.wr(cyc + 8, lo, p & MASK)
        cpu.wr(cyc + 9, hi, p >> 32)
    return run


@H("mpyhi", "mpyli", "mpyhir", "mpylir", "mpyih", "mpyil", "mpyihr", "mpyilr")
def _mpyxi(i):
    m = i.mnem
    a, b = src(i.ops[0]), src(i.ops[1])
    # MPYHI/MPYLI src1 = 16-bit (hi/lo), src2 = 32-bit; MPYIH/IL swapped
    if m in ("mpyih", "mpyil", "mpyihr", "mpyilr"):
        a, b = b, a
        m = {"mpyih": "mpyhi", "mpyil": "mpyli", "mpyihr": "mpyhir", "mpyilr": "mpylir"}[m]
    half = SH if m in ("mpyhi", "mpyhir") else SL
    rnd = m.endswith("r")
    if rnd:
        d = reg(i.ops[2])
        return lambda cpu, cyc: cpu.wr(cyc + 3, d, (half(a(cpu)) * s32(b(cpu)) + 0x4000) >> 15)
    lo, hi = pair(i.ops[2])

    def run(cpu, cyc):
        p = (half(a(cpu)) * s32(b(cpu))) & ((1 << 64) - 1)
        cpu.wr(cyc + 3, lo, p & MASK)
        cpu.wr(cyc + 3, hi, p >> 32)
    return run


@H("mpy2", "mpy2ir")
def _mpy2(i):
    a, b = src(i.ops[0]), src(i.ops[1])
    if i.mnem == "mpy2ir":        # MPY2IR src1(16x2), src2(32) -> 2 x 32 rounded
        lo, hi = pair(i.ops[2])

        def run(cpu, cyc):
            x, y = a(cpu), s32(b(cpu))
            cpu.wr(cyc + 3, lo, sat32((SL(x) * y + 0x4000) >> 15))
            cpu.wr(cyc + 3, hi, sat32((SH(x) * y + 0x4000) >> 15))
        return run
    lo, hi = pair(i.ops[2])

    def run(cpu, cyc):
        x, y = a(cpu), b(cpu)
        cpu.wr(cyc + 3, lo, SL(x) * SL(y))
        cpu.wr(cyc + 3, hi, SH(x) * SH(y))
    return run


@H("smpy2")
def _smpy2(i):
    a, b = src(i.ops[0]), src(i.ops[1])
    lo, hi = pair(i.ops[2])

    def sm(x, y):
        p = (x * y) << 1
        return 0x7FFFFFFF if p == 0x80000000 else p

    def run(cpu, cyc):
        x, y = a(cpu), b(cpu)
        cpu.wr(cyc + 3, lo, sm(SL(x), SL(y)))
        cpu.wr(cyc + 3, hi, sm(SH(x), SH(y)))
    return run


H("mpyu4")(long_dst(lambda a, b: (from_h2(bytes4(a)[0] * bytes4(b)[0], bytes4(a)[1] * bytes4(b)[1]),
                                  from_h2(bytes4(a)[2] * bytes4(b)[2], bytes4(a)[3] * bytes4(b)[3]))))
H("mpysu4")(long_dst(lambda a, b: (from_h2(s8(bytes4(a)[0]) * bytes4(b)[0], s8(bytes4(a)[1]) * bytes4(b)[1]),
                                   from_h2(s8(bytes4(a)[2]) * bytes4(b)[2], s8(bytes4(a)[3]) * bytes4(b)[3]))))
H("mpyus4")(long_dst(lambda a, b: (from_h2(bytes4(a)[0] * s8(bytes4(b)[0]), bytes4(a)[1] * s8(bytes4(b)[1])),
                                   from_h2(bytes4(a)[2] * s8(bytes4(b)[2]), bytes4(a)[3] * s8(bytes4(b)[3])))))
H("dotp2")(simple(lambda a, b: SL(a) * SL(b) + SH(a) * SH(b)))
H("dotpn2")(simple(lambda a, b: SH(a) * SH(b) - SL(a) * SL(b)))
H("dotprsu2")(simple(lambda a, b: (SH(a) * UH(b) + SL(a) * UL(b) + 0x8000) >> 16))
H("dotpnrsu2")(simple(lambda a, b: (SH(a) * UH(b) - SL(a) * UL(b) + 0x8000) >> 16))
H("dotprus2")(simple(lambda a, b: (UH(a) * SH(b) + UL(a) * SL(b) + 0x8000) >> 16))
H("dotpnrus2")(simple(lambda a, b: (UH(a) * SH(b) - UL(a) * SL(b) + 0x8000) >> 16))
H("dotpu4")(simple(lambda a, b: sum(x * y for x, y in zip(bytes4(a), bytes4(b)))))
H("dotpsu4")(simple(lambda a, b: sum(s8(x) * y for x, y in zip(bytes4(a), bytes4(b)))))
H("dotpus4")(simple(lambda a, b: sum(x * s8(y) for x, y in zip(bytes4(a), bytes4(b)))))


def _gf_mul(a, b, poly):
    r = 0
    for _ in range(8):
        if b & 1:
            r ^= a
        b >>= 1
        a <<= 1
        if a & 0x100:
            a ^= 0x100 | poly
    return r & 0xFF


@H("gmpy4")
def _gmpy4(i):
    a, b = src(i.ops[0]), src(i.ops[1])
    d = reg(i.ops[2])

    def run(cpu, cyc):
        poly = cpu.c.get("gfpgfr", 0x0700001D) & 0xFF or 0x1D
        x, y = bytes4(a(cpu)), bytes4(b(cpu))
        cpu.wr(cyc + 3, d, from_bytes4([_gf_mul(p, q, poly) for p, q in zip(x, y)]))
    return run


@H("cmpy", "cmpyr", "cmpyr1")
def _cmpy(i):
    a, b = src(i.ops[0]), src(i.ops[1])
    m = i.mnem

    def parts(cpu):
        x, y = a(cpu), b(cpu)
        re_ = SH(x) * SH(y) - SL(x) * SL(y)
        im_ = SH(x) * SL(y) + SL(x) * SH(y)
        return re_, im_
    if m == "cmpy":
        lo, hi = pair(i.ops[2])

        def run(cpu, cyc):
            re_, im_ = parts(cpu)
            cpu.wr(cyc + 3, hi, sat32(re_ << 1) if False else re_)
            cpu.wr(cyc + 3, lo, im_)
        return run
    d = reg(i.ops[2])

    def run(cpu, cyc):
        re_, im_ = parts(cpu)
        if m == "cmpyr":
            r_h = sat16((re_ + 0x8000) >> 16)
            r_l = sat16((im_ + 0x8000) >> 16)
        else:
            r_h = sat16((sat32(re_ << 1) + 0x8000) >> 16)
            r_l = sat16((sat32(im_ << 1) + 0x8000) >> 16)
        cpu.wr(cyc + 3, d, from_h2(r_l, r_h))
    return run


@H("ddotp4")
def _ddotp4(i):
    a, b = src(i.ops[0]), src(i.ops[1])
    lo, hi = pair(i.ops[2])

    def run(cpu, cyc):
        x = a(cpu)
        y = b(cpu)
        bx = [s8(v) for v in bytes4(x)]
        cpu.wr(cyc + 3, lo, bx[0] * lo16(y) + bx[1] * hi16(y) if False else bx[0] * UL(y) + bx[1] * UH(y))
        cpu.wr(cyc + 3, hi, bx[2] * UL(y) + bx[3] * UH(y))
    return run


@H("ddotpl2r", "ddotph2r", "ddotpl2", "ddotph2")
def _ddotp2(i):
    sl, sh = pair(i.ops[0])           # src1 pair (4 halves)
    b = src(i.ops[1])
    m = i.mnem
    rnd = m.endswith("r")
    if rnd:
        d = reg(i.ops[2])
    else:
        lo, hi = pair(i.ops[2])

    def run(cpu, cyc):
        x0, x1 = cpu.r[sl], cpu.r[sh]
        y = b(cpu)
        h = [SL(x0), SH(x0), SL(x1), SH(x1)]
        if m.startswith("ddotpl2"):
            r1 = h[1] * SH(y) + h[0] * SL(y)
            r2 = h[2] * SH(y) + h[1] * SL(y)
        else:
            r1 = h[2] * SH(y) + h[1] * SL(y)
            r2 = h[3] * SH(y) + h[2] * SL(y)
        if rnd:
            cpu.wr(cyc + 3, d, from_h2(sat16((r1 + 0x8000) >> 16), sat16((r2 + 0x8000) >> 16)))
        else:
            cpu.wr(cyc + 3, lo, r1)
            cpu.wr(cyc + 3, hi, r2)
    return run


# ---- loads / stores ----
LDSZ = {"ldb": 1, "ldbu": 1, "ldh": 2, "ldhu": 2, "ldw": 4, "ldnw": 4,
        "lddw": 8, "ldndw": 8}
STSZ = {"stb": 1, "sth": 2, "stw": 4, "stnw": 4, "stdw": 8, "stndw": 8}


@H(*LDSZ)
def _ld(i):
    m = i.mnem
    sz = LDSZ[m]
    a = memop(i.ops[0], sz)
    dst = i.ops[1]
    if sz == 8:
        lo, hi = pair(dst)

        def run(cpu, cyc):
            addr, b, nb = a(cpu)
            if nb is not None:
                cpu.wr(cyc, b, nb)
            if m == "lddw":
                addr &= ~7

            def done(cpu, v):
                cpu.wr(cyc + 4, lo, v & MASK)
                cpu.wr(cyc + 4, hi, v >> 32)
            cpu.ld(cyc, addr, 8, done)
        return run
    d = reg(dst)
    signed = m in ("ldb", "ldh")
    align = 0 if m == "ldnw" else sz - 1

    def run(cpu, cyc):
        addr, b, nb = a(cpu)
        if nb is not None:
            cpu.wr(cyc, b, nb)

        def done(cpu, v):
            cpu.wr(cyc + 4, d, sext(v, sz * 8) & MASK if signed else v)
        cpu.ld(cyc, addr & ~align, sz, done)
    return run


@H(*STSZ)
def _st(i):
    m = i.mnem
    sz = STSZ[m]
    s = i.ops[0]
    a = memop(i.ops[1], sz)
    if sz == 8:
        lo, hi = pair(s)

        def run(cpu, cyc):
            addr, b, nb = a(cpu)
            if nb is not None:
                cpu.wr(cyc, b, nb)
            if m == "stdw":
                addr &= ~7
            cpu.st(cyc + 2, addr, 8, cpu.r[lo] | cpu.r[hi] << 32)
        return run
    r_ = reg(s)
    align = 0 if m == "stnw" else sz - 1

    def run(cpu, cyc):
        addr, b, nb = a(cpu)
        if nb is not None:
            cpu.wr(cyc, b, nb)
        cpu.st(cyc + 2, addr & ~align, sz, cpu.r[r_] & ((1 << (8 * sz)) - 1))
    return run


# ---- branches ----
def _target(cpu, s, i):
    if is_reg(s):
        return cpu.r[reg(s)]
    if s.lower() == "irp":
        return cpu.c["irp"]
    if s.lower() == "nrp":
        return cpu.c["nrp"]
    return int(s, 0)


@H("b")
def _b(i):
    s = i.ops[0]

    def run(cpu, cyc):
        t = _target(cpu, s, i)
        if s.lower() == "irp":
            _irp_return(cpu)
        cpu.branch(cyc, t)
    return run


def _irp_return(cpu):
    """B IRP: GIE <- PGIE, TSR <- ITSR (interrupt return)"""
    c = cpu.c
    g = (c["csr"] >> 1) & 1
    c["csr"] = (c["csr"] & ~1) | g
    c["tsr"] = (c.get("itsr", 0) & ~1) | g


@H("bnop")
def _bnop(i):
    s, n = i.ops[0], int(i.ops[1], 0)

    def run(cpu, cyc):
        if s.lower() == "irp":
            _irp_return(cpu)
        cpu.branch(cyc, _target(cpu, s, i))
        return n + 1 if n else None
    return run


@H("callp")
def _callp(i):
    t = int(i.ops[0], 0)
    ret = reg(i.ops[1])

    def run(cpu, cyc):
        ep_end = cpu._next_addr(i.addr)
        # return address = the execute packet after the CALLP's EP
        a = i.addr
        while True:
            n = cpu._next_addr(a)
            j = cpu.prog.get(n)
            if j is None or not j.par:
                break
            a = n
        cpu.wr(cyc, ret, n)
        cpu.branch(cyc, t)
        return 6
    return run


@H("addkpc")
def _addkpc(i):
    t = int(i.ops[0], 0)          # objdump prints the absolute address
    d = reg(i.ops[1])
    n = int(i.ops[2], 0)

    def run(cpu, cyc):
        cpu.wr(cyc, d, t)
        return n + 1 if n else None
    return run


@H("bdec", "bpos")
def _bdec(i):
    t = int(i.ops[0], 0)
    r_ = reg(i.ops[1])
    dec = i.mnem == "bdec"

    def run(cpu, cyc):
        v = s32(cpu.r[r_])
        if v >= 0:
            cpu.branch(cyc, t)
            if dec:
                cpu.wr(cyc, r_, v - 1)
    return run


# ---- SPLOOP control ----
@H("sploop", "sploopd", "sploopw")
def _sploop(i):
    ii = int(i.ops[0], 0)
    kind = i.mnem

    def run(cpu, cyc):
        wcond = i.cond if kind == "sploopw" else None
        if kind != "sploopw" and i.cond is not None:
            raise Unimplemented("predicated SPLOOP(D) = nested reload")
        cpu.sp = SPLoop(kind, ii, wcond, cpu, cyc, i.addr)
    return run


@H("spkernel", "spmask")
def _spctl(i):
    return lambda cpu, cyc: None


# ---- misc ----
@H("idle")
def _idle(i):
    def run(cpu, cyc):
        cpu.idle = True
    return run


@H("swe", "swenr")
def _swe(i):
    def run(cpu, cyc):
        raise Unimplemented("software exception")
    return run


@H("dint")
def _dint(i):
    def run(cpu, cyc):
        csr = cpu.c["csr"]
        cpu.c["tsr"] = (cpu.c.get("tsr", 0) & ~1) | (csr & 1)   # SGIE <- GIE
        cpu.c["csr"] = csr & ~1
    return run


@H("rint")
def _rint(i):
    def run(cpu, cyc):
        cpu.c["csr"] = (cpu.c["csr"] & ~1) | (cpu.c.get("tsr", 0) & 1)
    return run


@H("mvd")
def _mvd(i):
    a = src(i.ops[0])
    d = reg(i.ops[1])
    return lambda cpu, cyc: cpu.wr(cyc + 3, d, a(cpu))


@H("ll", "sl", "cmtl")
def _atomic(i):
    m = i.mnem
    if m == "sl":
        r_ = reg(i.ops[0])
        a = memop(i.ops[1], 4)

        def run(cpu, cyc):
            addr, b, nb = a(cpu)
            cpu._sl = (addr, cpu.r[r_])
        return run
    a = memop(i.ops[0], 4)
    d = reg(i.ops[1])
    if m == "ll":
        def run(cpu, cyc):
            addr, b, nb = a(cpu)
            cpu.ld(cyc, addr & ~3, 4, lambda cpu, v: cpu.wr(cyc + 4, d, v))
        return run

    def run(cpu, cyc):           # CMTL: commit the pending SL (single core)
        sl = getattr(cpu, "_sl", None)
        if sl:
            cpu.st(cyc + 2, sl[0] & ~3, 4, sl[1])
            cpu._sl = None
        cpu.wr(cyc + 4, d, 1)
    return run


# ---- floating point ----
def _fp2(fn, w):
    """single-precision op (src1, src2, dst) -> bits at i+w"""
    def make(i):
        a, b = src(i.ops[0]), src(i.ops[1])
        d = reg(i.ops[2])
        return lambda cpu, cyc: cpu.wr(cyc + w, d, b32(fn(f32(a(cpu)), f32(b(cpu)))))
    return make


H("addsp")(_fp2(lambda a, b: a + b, 3))
H("subsp")(_fp2(lambda a, b: a - b, 3))
H("mpysp")(_fp2(lambda a, b: a * b, 3))


def _dpsrc(s):
    lo, hi = pair(s)
    return lambda cpu: f64(cpu.r[lo], cpu.r[hi])


def _dp2(fn, wlo, whi, srcs="dd"):
    def make(i):
        rd = [(_dpsrc(s) if t == "d" else (lambda cpu, f=src(s): f32(f(cpu))))
              for s, t in zip(i.ops[:2], srcs)]
        lo, hi = pair(i.ops[2])

        def run(cpu, cyc):
            l, h_ = b64(fn(rd[0](cpu), rd[1](cpu)))
            cpu.wr(cyc + wlo, lo, l)
            cpu.wr(cyc + whi, hi, h_)
        return run
    return make


H("adddp")(_dp2(lambda a, b: a + b, 5, 6))
H("subdp")(_dp2(lambda a, b: a - b, 5, 6))
H("mpydp")(_dp2(lambda a, b: a * b, 8, 9))
H("mpyspdp")(_dp2(lambda a, b: a * b, 5, 6, "sd"))
H("mpysp2dp")(_dp2(lambda a, b: a * b, 3, 4, "ss"))


def _fcmp(fn, dp):
    def make(i):
        if dp:
            a, b = _dpsrc(i.ops[0]), _dpsrc(i.ops[1])
            w = 1
        else:
            fa, fb = src(i.ops[0]), src(i.ops[1])
            a, b = (lambda cpu: f32(fa(cpu))), (lambda cpu: f32(fb(cpu)))
            w = 0
        d = reg(i.ops[2])
        return lambda cpu, cyc: cpu.wr(cyc + w, d, 1 if fn(a(cpu), b(cpu)) else 0)
    return make


H("cmpeqsp")(_fcmp(lambda a, b: a == b, False))
H("cmpgtsp")(_fcmp(lambda a, b: a > b, False))
H("cmpltsp")(_fcmp(lambda a, b: a < b, False))
H("cmpeqdp")(_fcmp(lambda a, b: a == b, True))
H("cmpgtdp")(_fcmp(lambda a, b: a > b, True))
H("cmpltdp")(_fcmp(lambda a, b: a < b, True))


@H("abssp")
def _abssp(i):
    a = src(i.ops[0])
    d = reg(i.ops[1])
    return lambda cpu, cyc: cpu.wr(cyc, d, b32(abs(f32(a(cpu)))))


@H("absdp")
def _absdp(i):
    sl, sh = pair(i.ops[0])
    dl, dh = pair(i.ops[1])

    def run(cpu, cyc):
        l, h_ = b64(abs(f64(cpu.r[sl], cpu.r[sh])))
        cpu.wr(cyc, dl, l)
        cpu.wr(cyc + 1, dh, h_)
    return run


@H("intsp", "intspu")
def _intsp(i):
    a = src(i.ops[0])
    d = reg(i.ops[1])
    u = i.mnem == "intspu"
    return lambda cpu, cyc: cpu.wr(cyc + 3, d, b32(float(a(cpu) if u else s32(a(cpu)))))


@H("intdp", "intdpu")
def _intdp(i):
    a = src(i.ops[0])
    lo, hi = pair(i.ops[1])
    u = i.mnem == "intdpu"

    def run(cpu, cyc):
        l, h_ = b64(float(a(cpu) if u else s32(a(cpu))))
        cpu.wr(cyc + 3, lo, l)
        cpu.wr(cyc + 4, hi, h_)
    return run


@H("spint", "sptrunc")
def _spint(i):
    a = src(i.ops[0])
    d = reg(i.ops[1])
    trunc = i.mnem == "sptrunc"
    side = i.side

    def run(cpu, cyc):
        mode = 1 if trunc else round_mode(cpu, side)
        cpu.wr(cyc + 3, d, to_int(f32(a(cpu)), mode))
    return run


@H("dpint", "dptrunc")
def _dpint(i):
    s = _dpsrc(i.ops[0])
    d = reg(i.ops[1])
    trunc = i.mnem == "dptrunc"
    side = i.side

    def run(cpu, cyc):
        mode = 1 if trunc else round_mode(cpu, side)
        cpu.wr(cyc + 3, d, to_int(s(cpu), mode))
    return run


@H("spdp")
def _spdp(i):
    a = src(i.ops[0])
    lo, hi = pair(i.ops[1])

    def run(cpu, cyc):
        l, h_ = b64(f32(a(cpu)))
        cpu.wr(cyc, lo, l)
        cpu.wr(cyc + 1, hi, h_)
    return run


@H("dpsp")
def _dpsp(i):
    s = _dpsrc(i.ops[0])
    d = reg(i.ops[1])
    return lambda cpu, cyc: cpu.wr(cyc + 3, d, b32(s(cpu)))


def _recip_seed(x, sqrt):
    """RCPSP/RSQRSP: 'exact to 8 bits of mantissa' -> truncate the true
    result's mantissa to 8 bits (FIDELITY NOTE: the real seed table is not
    published; Newton steps after it usually absorb the difference)."""
    if x == 0 or (sqrt and x < 0):
        return float("inf") if x >= 0 else float("nan")
    v = 1.0 / math.sqrt(x) if sqrt else 1.0 / x
    b = struct.unpack("<I", struct.pack("<f", v))[0] & ~((1 << 15) - 1)
    return struct.unpack("<f", struct.pack("<I", b))[0]


@H("rcpsp", "rsqrsp")
def _rcpsp(i):
    a = src(i.ops[0])
    d = reg(i.ops[1])
    sq = i.mnem == "rsqrsp"
    return lambda cpu, cyc: cpu.wr(cyc, d, b32(_recip_seed(f32(a(cpu)), sq)))


@H("rcpdp", "rsqrdp")
def _rcpdp(i):
    s = _dpsrc(i.ops[0])
    lo, hi = pair(i.ops[1])
    sq = i.mnem == "rsqrdp"

    def run(cpu, cyc):
        x = s(cpu)
        if x == 0 or (sq and x < 0):
            v = float("inf")
        else:
            v = 1.0 / math.sqrt(x) if sq else 1.0 / x
        bits = struct.unpack("<Q", struct.pack("<d", v))[0] & ~((1 << 44) - 1)
        cpu.wr(cyc, lo, bits & MASK)
        cpu.wr(cyc + 1, hi, bits >> 32)
    return run


@H("xormpy", "gmpy")
def _gfx(i):
    a, b = src(i.ops[0]), src(i.ops[1])
    d = reg(i.ops[2])

    def run(cpu, cyc):
        x, y, r_ = a(cpu), b(cpu) & 0x1FFFFFF, 0
        for k in range(25):
            if y >> k & 1:
                r_ ^= x << k
        cpu.wr(cyc + 3, d, r_ & MASK)
    return run
