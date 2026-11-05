"""cfast.py -- C version of the C674x core (c6xcore.c), same API as cpu.Cpu.

The Python decoder (decode.py) still turns objdump text into Insn records;
encode() turns each into a fixed C record. Instructions c6xcore.c does not
implement become op `py`: the C core calls back here and the isa.py
handler runs against this object (it has the same r / c / wr / st / ld /
branch interface as cpu.Cpu), so every instruction works from day one and
hot ones move to C as the profile says.

Use: CCpu(bus) instead of Cpu(bus). XDJ_DSP_CORE=py keeps the Python core
(tracing tools need it: no per-instruction trace hook in C).
"""
from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys

from . import isa
from .cpu import CTRL, Halt, REGIDX

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "c6xcore.c")
LIB = os.path.join(HERE, "_c6xcore.dll" if sys.platform == "win32"
                   else "_c6xcore.so")
MASK = 0xFFFFFFFF
u8, i8, u16, u32, s32, u64, s64 = (ctypes.c_uint8, ctypes.c_int8, ctypes.c_uint16,
                                   ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint64,
                                   ctypes.c_int64)

OPS = re.findall(r"OP\((\w+)\)", open(SRC).read().split("#define OP(n)")[0])
O = {n: k for k, n in enumerate(OPS)}
CTRLS = list(dict.fromkeys(CTRL))[:30]          # unique, C order
CIDX = {n: k for k, n in enumerate(CTRLS)}
UNITS = {"L1": 0, "L2": 1, "S1": 2, "S2": 3, "M1": 4, "M2": 5, "D1": 6, "D2": 7}
ST_OK, ST_NEED, ST_ERR, ST_STOP, ST_IDLE = range(5)


class Ins(ctypes.Structure):
    _fields_ = [("addr", u32), ("k", s32 * 4), ("py", u32), ("op", u16),
                ("size", u8), ("par", u8), ("header", u8), ("prot", u8),
                ("creg", i8), ("cneg", u8), ("unit", u8), ("side", u8),
                ("w", u8), ("sub", u8),
                ("ra", u8), ("rb", u8), ("rd", u8), ("rah", u8), ("rbh", u8), ("rdh", u8),
                ("ca", u8), ("cb", u8),
                ("msz", u8), ("mmode", u8), ("mbase", u8), ("mofk", u8), ("moff", u8),
                ("mscale", u8), ("msign", u8), ("malign", u8),
                ("ctrl", u8), ("spmask", u8), ("spk", u8), ("pad", u8)]


RD_T = ctypes.CFUNCTYPE(u32, u32, ctypes.c_int)
WR_T = ctypes.CFUNCTYPE(None, u32, ctypes.c_int, u32)
PY_T = ctypes.CFUNCTYPE(ctypes.c_int, u32, u64)
LD_T = ctypes.CFUNCTYPE(None, u32, u32, u32)

_lib = None


PROF = os.path.join(HERE, "c6xcore.profdata")


def build_lib(out, src=SRC, pgo=True):
    """compile the core to `out` (temp name + rename: a running test never
    loads a half-written file). With the profile from tools/c6x_pgo.py
    (play-mode run) clang lays the interpreter out for the hot paths:
    +20% DSP speed while playing (2026-11-01). Same C semantics, so the
    result is bit-identical; a compiler that can't read the profile gets
    the plain -O2 build."""
    tmp = out + ".tmp"
    base = ["cc", "-O2", "-shared", "-fPIC", "-o", tmp, src, "-lm"]
    if pgo and os.path.exists(PROF):
        try:
            subprocess.check_call(base[:2] + ["-fprofile-instr-use=" + PROF,
                                   "-Wno-profile-instr-out-of-date",
                                   "-Wno-profile-instr-unprofiled",
                                   "-Wno-profile-instr-missing"] + base[2:],
                                  stderr=subprocess.DEVNULL)
            os.replace(tmp, out)
            return
        except (subprocess.CalledProcessError, OSError):
            pass
    subprocess.check_call(base)
    os.replace(tmp, out)


def load_lib():
    global _lib
    if _lib is not None:
        return _lib
    # Windows: the app ships a prebuilt DLL (no compiler on user PCs); the
    # .c still travels with it because the OP list above is read from it
    if sys.platform != "win32" and os.path.exists(SRC) and (not os.path.exists(LIB) or
                                os.path.getmtime(LIB) < os.path.getmtime(SRC) or
                                (os.path.exists(PROF) and
                                 os.path.getmtime(LIB) < os.path.getmtime(PROF))):
        build_lib(LIB)
    lib = ctypes.CDLL(LIB)
    P = ctypes.c_void_p
    sig = {
        "c6x_new": ([ctypes.c_int], P), "c6x_free": ([P], None),
        "c6x_sizeof_ins": ([], ctypes.c_int),
        "c6x_regs": ([P], ctypes.POINTER(u32)), "c6x_ctrl": ([P], ctypes.POINTER(u32)),
        "c6x_add_code": ([P, ctypes.POINTER(Ins), ctypes.c_int], ctypes.c_int),
        "c6x_drop_code": ([P, u32, u32], None), "c6x_clear_code": ([P], None),
        "c6x_set_cb": ([P, RD_T, WR_T, PY_T, LD_T], None),
        "c6x_add_region": ([P, u32, u32, P], None),
        "c6x_wr": ([P, u64, ctypes.c_int, u32], None),
        "c6x_wrc": ([P, u64, ctypes.c_int, u32], None),
        "c6x_st": ([P, u64, u32, ctypes.c_int, u64], None),
        "c6x_branch": ([P, u64, u32], None),
        "c6x_ldpy": ([P, u64, u32, ctypes.c_int, u32], None),
        "c6x_stop": ([P], None), "c6x_run": ([P, s64], s64),
        "c6x_status": ([P], ctypes.c_int), "c6x_need": ([P], u32),
        "c6x_err": ([P], ctypes.c_int), "c6x_err_pc": ([P], u32),
        "c6x_pc": ([P], u32), "c6x_set_pc": ([P, u32], None),
        "c6x_cycle": ([P], u64), "c6x_set_cycle": ([P, u64], None),
        "c6x_stall": ([P], ctypes.c_int), "c6x_evcount": ([P], ctypes.c_int),
        "c6x_npend": ([P], ctypes.c_int), "c6x_sp_on": ([P], ctypes.c_int),
        "c6x_idle": ([P], u32), "c6x_set_idle": ([P, u32], None),
        "c6x_counts": ([P, ctypes.c_int], u64),
        "c6x_splx_pending": ([P], ctypes.c_int),
        "c6x_set_idle_loop": ([P, u32, u32, u32], None),
        "c6x_add_idle_head": ([P, u32], ctypes.c_int),
        "c6x_skip": ([P, u64], None),
        "c6x_set_dff": ([P, ctypes.c_int], None),
        "c6x_dff_stat": ([P, ctypes.c_int], u64),
        "c6x_settle": ([P], None), "c6x_dff_frozen": ([P], ctypes.c_int),
        "c6x_sp_info": ([P, ctypes.POINTER(u32), ctypes.c_int], ctypes.c_int),
        "c6x_hist": ([P, ctypes.c_int], u32),
        "c6x_prof": ([P, ctypes.c_int, ctypes.POINTER(u32)], u64),
        "c6x_ifr_or": ([P, u32], None), "c6x_ifr_and": ([P, u32], None),
    }
    for name, (args, res) in sig.items():
        f = getattr(lib, name)
        f.argtypes, f.restype = args, res
    lib.c6x_set_refill.argtypes = [ctypes.c_int]
    lib.c6x_set_refill(isa.REFILL)
    if lib.c6x_sizeof_ins() != ctypes.sizeof(Ins):
        raise RuntimeError("c6xcore.c / cfast.py Ins layout mismatch")
    _lib = lib
    return lib


# ------------------------------------------------------------------ encoder
def _s32(v):
    v &= MASK
    return v - (1 << 32) if v & 0x80000000 else v


def _const(s):
    try:
        return int(s, 0)
    except ValueError:
        return None


class _Py(Exception):
    """this instruction stays in Python"""


def _reg(s):
    if not isa.is_reg(s):
        raise _Py
    return isa.reg(s)


def _pair(s):
    try:
        return isa.pair(s)
    except isa.Unimplemented:
        raise _Py


def _src(e, s, which):
    """register or constant operand (u32-masked, like isa.src)"""
    if ":" in s:
        raise _Py
    if isa.is_reg(s):
        setattr(e, "ra" if which == 0 else "rb", isa.reg(s))
        return
    v = _const(s)
    if v is None:
        raise _Py
    e.k[which] = _s32(v)
    setattr(e, "ca" if which == 0 else "cb", 1)


SIMPLE = {"and": "and", "or": "or", "xor": "xor", "andn": "andn", "sadd": "sadd",
          "ssub": "ssub", "add2": "add2", "sub2": "sub2", "pack2": "pack2",
          "packh2": "packh2", "packhl2": "packhl2", "packlh2": "packlh2",
          "packl4": "packl4", "packh4": "packh4", "shl": "shl", "shr": "shr",
          "shru": "shru", "sshl": "sshl", "lmbd": "lmbd"}
TWO = {"neg": "neg", "not": "not", "swap2": "swap2", "abs": "abs", "norm": "norm",
       "mvd": "mvd"}
MPY16 = {"mpy": 0, "mpyu": 2 | 8, "mpyus": 2, "mpysu": 8, "mpyh": 1 | 4,
         "mpyhu": 1 | 2 | 4 | 8, "mpyhus": 1 | 2 | 4, "mpyhsu": 1 | 4 | 8,
         "mpyhl": 1, "mpyhlu": 1 | 2 | 8, "mpyhuls": 1 | 2, "mpyhslu": 1 | 8,
         "mpylh": 4, "mpylhu": 2 | 4 | 8, "mpyluhs": 2 | 4, "mpylshu": 4 | 8,
         "smpy": 16, "smpyh": 16 | 1 | 4, "smpyhl": 16 | 1, "smpylh": 16 | 4}
MPY32 = {"mpy32": 3, "mpy32u": 0, "mpy32us": 2, "mpy32su": 1}
CMP = {"cmpeq": 0, "cmpgt": 1, "cmplt": 2, "cmpgtu": 3, "cmpltu": 4}
ADDA = {"addab": ("adda", 0), "addah": ("adda", 1), "addaw": ("adda", 2),
        "addad": ("adda", 3), "subab": ("suba", 0), "subah": ("suba", 1),
        "subaw": ("suba", 2)}
DP = {"adddp": ("dadd", 0, 5, 6), "subdp": ("dsub", 0, 5, 6), "mpydp": ("dmul", 0, 8, 9),
      "mpyspdp": ("dmul", 1, 5, 6), "mpysp2dp": ("dmul", 3, 3, 4)}


def _encode_body(i, e):
    m, ops = i.mnem, i.ops
    w = isa.w_at(i)
    e.w = w

    def op(name):
        e.op = O[name]

    if m == "nop":
        op("nop"); e.k[0] = int(ops[0], 0) if ops else 1
    elif m in ("mvk", "mvkl"):
        v = isa.const(ops[0])
        v = v & MASK if m == "mvk" else isa.sext(v, 16) & MASK
        op("mvk"); e.k[0] = _s32(v); e.rd = _reg(ops[1])
    elif m in ("mvkh", "mvklh"):
        v = isa.const(ops[0]) & MASK
        hi = (v & 0xFFFF) << 16 if m == "mvklh" else v & 0xFFFF0000
        op("mvkh"); e.k[0] = _s32(hi); e.rd = _reg(ops[1])
    elif m == "dmv":
        op("dmv"); _src(e, ops[0], 0); _src(e, ops[1], 1)
        e.rd, e.rdh = _pair(ops[2])
    elif m == "mv":
        if ":" in ops[0]:
            op("mvpair"); e.ra, e.rah = _pair(ops[0]); e.rd, e.rdh = _pair(ops[1])
        else:
            op("mv"); _src(e, ops[0], 0); e.rd = _reg(ops[1])
    elif m == "zero":
        if ":" in ops[0]:
            op("zeropair"); e.rd, e.rdh = _pair(ops[0])
        else:
            op("zero"); e.rd = _reg(ops[0])
    elif m == "mvc":
        s, d = ops
        if isa.is_reg(s):
            if d.lower() not in CIDX:
                raise _Py
            op("mvc_to"); e.ra = isa.reg(s); e.ctrl = CIDX[d.lower()]
        else:
            if s.lower() not in CIDX:
                raise _Py
            op("mvc_from"); e.ctrl = CIDX[s.lower()]; e.rd = _reg(d)
    elif m in ("add", "sub", "addu", "subu"):
        if any(":" in o for o in ops):
            raise _Py
        op("add" if m.startswith("add") else "sub")
        _src(e, ops[0], 0); _src(e, ops[1], 1); e.rd = _reg(ops[2])
    elif m == "addk":
        op("addk"); e.k[0] = isa.sext(isa.const(ops[0]), 16); e.rd = _reg(ops[1])
    elif m in ADDA:
        name, sh = ADDA[m]
        op(name); e.sub = sh; _src(e, ops[0], 0); _src(e, ops[1], 1); e.rd = _reg(ops[2])
    elif m in SIMPLE:
        op(SIMPLE[m]); _src(e, ops[0], 0); _src(e, ops[1], 1); e.rd = _reg(ops[2])
    elif m in TWO:
        if any(":" in o for o in ops):
            raise _Py
        op(TWO[m]); _src(e, ops[0], 0); e.rd = _reg(ops[1])
    elif m in CMP:
        if any(":" in o for o in ops):
            raise _Py
        op("cmp"); e.sub = CMP[m]
        for k, s in enumerate(ops[:2]):
            if isa.is_reg(s):
                setattr(e, "ra" if k == 0 else "rb", isa.reg(s))
                continue
            v = _const(s)
            if v is None:
                raise _Py
            if m in ("cmpgtu", "cmpltu"):
                v &= 0x1F
            elif not -0x80000000 <= v <= 0x7FFFFFFF:
                raise _Py
            e.k[k] = _s32(v)
            setattr(e, "ca" if k == 0 else "cb", 1)
        e.rd = _reg(ops[2])
    elif m in ("ext", "extu", "set", "clr"):
        op(m); _src(e, ops[0], 0)
        if len(ops) == 4:
            e.cb = 1; e.k[1] = isa.const(ops[1]) & 31; e.k[2] = isa.const(ops[2]) & 31
            e.rd = _reg(ops[3])
        else:
            if isa.is_reg(ops[1]):
                e.rb = isa.reg(ops[1])
            else:
                v = isa.const(ops[1]) & MASK
                e.cb = 1; e.k[1] = (v >> 5) & 31; e.k[2] = v & 31
            e.rd = _reg(ops[2])
    elif m in MPY16:
        op("mpy16"); e.sub = MPY16[m]
        _src(e, ops[0], 0); _src(e, ops[1], 1); e.rd = _reg(ops[2])
    elif m in ("mpyhi", "mpyli", "mpyhir", "mpylir", "mpyih", "mpyil", "mpyihr", "mpyilr"):
        op("mpyxi")
        a, b = (ops[1], ops[0]) if m[3] == "i" else (ops[0], ops[1])   # IH/IL: swapped
        e.sub = (1 if "h" in m[3:5] else 0) | (2 if m.endswith("r") else 0)
        _src(e, a, 0); _src(e, b, 1)
        if e.sub & 2:
            e.rd = _reg(ops[2])
        else:
            e.rd, e.rdh = _pair(ops[2])
    elif m in MPY32:
        e.sub = MPY32[m]; _src(e, ops[0], 0); _src(e, ops[1], 1)
        if ":" in ops[2]:
            op("mpy32p"); e.rd, e.rdh = _pair(ops[2])
        else:
            op("mpy32"); e.rd = _reg(ops[2])
    elif m in isa.LDSZ or m in isa.STSZ:
        ld = m in isa.LDSZ
        sz = isa.LDSZ[m] if ld else isa.STSZ[m]
        mo = isa.MEMRE.match(ops[0] if ld else ops[1])
        if not mo:
            raise _Py
        pre, base, post, br, off = mo.groups()
        e.mbase = isa.reg(base); e.msz = sz
        scale = sz if br == "[" else 1
        if off is None:
            e.mofk = 0
        elif isa.is_reg(off):
            e.mofk = 1; e.moff = isa.reg(off); e.mscale = scale
        else:
            e.mofk = 2; e.k[2] = _s32(int(off, 0) * scale)
        if post:
            e.mmode = 5 if post == "++" else 6
        else:
            e.mmode = {None: 0, "+": 1, "-": 2, "++": 3, "--": 4}[pre]
        if sz == 8:
            e.malign = 7 if m in ("lddw", "stdw") else 0
        else:
            e.malign = 0 if m in ("ldnw", "stnw") else sz - 1
        dst = ops[1] if ld else ops[0]
        if sz == 8:
            e.rd, e.rdh = _pair(dst)
        else:
            e.rd = _reg(dst)
        if ld:
            op("ld"); e.msign = {"ldb": 1, "ldh": 2}.get(m, 0)
            e.prot = 1 if getattr(i, "prot", False) else 0
        else:
            op("st")
    elif m in ("b", "bnop"):
        op(m); s = ops[0]
        if isa.is_reg(s):
            e.sub = 1; e.ra = isa.reg(s)
        elif s.lower() == "irp":
            e.sub = 2
        elif s.lower() == "nrp":
            e.sub = 3
        else:
            e.sub = 0; e.k[0] = _s32(int(s, 0))
        if m == "bnop":
            e.k[1] = int(ops[1], 0)
    elif m == "callp":
        op("callp"); e.k[0] = _s32(int(ops[0], 0)); e.rd = _reg(ops[1])
    elif m == "addkpc":
        op("addkpc"); e.k[0] = _s32(int(ops[0], 0)); e.rd = _reg(ops[1])
        e.k[1] = int(ops[2], 0)
    elif m in ("bdec", "bpos"):
        op("bdec"); e.k[0] = _s32(int(ops[0], 0)); e.rd = _reg(ops[1])
        e.sub = 1 if m == "bdec" else 0
    elif m in ("sploop", "sploopd", "sploopw"):
        if m != "sploopw" and i.cond is not None:
            raise _Py                      # nested reload: isa raises
        op("sploop"); e.k[0] = int(ops[0], 0)
        e.sub = {"sploop": 0, "sploopd": 1, "sploopw": 2}[m]
    elif m == "spmask":
        op("spctl"); e.spk = 1
        bits = 0
        for u in ops:
            for x in u.split(","):
                x = x.strip().upper()
                if x in UNITS:
                    bits |= 1 << UNITS[x]
        e.spmask = bits
    elif m == "spkernel":
        op("spctl"); e.spk = 2
        f = [int(x, 0) for x in ops] if ops else []
        f += [0, 0]
        e.k[0], e.k[1] = f[0], f[1]
    elif m in ("spkernelr", "spmaskr"):
        op("spctl"); e.spk = 3
    elif m in ("dint", "rint", "idle"):
        op(m)
    elif m in ("addsp", "subsp", "mpysp"):
        op({"addsp": "fadd", "subsp": "fsub", "mpysp": "fmul"}[m])
        _src(e, ops[0], 0); _src(e, ops[1], 1); e.rd = _reg(ops[2]); e.w = 3
    elif m in ("intsp", "intspu"):
        op("intsp"); e.sub = m == "intspu"; _src(e, ops[0], 0); e.rd = _reg(ops[1]); e.w = 3
    elif m in ("spint", "sptrunc"):
        op("spint"); e.sub = m == "sptrunc"; _src(e, ops[0], 0); e.rd = _reg(ops[1])
        e.w = 3; e.side = i.side or 0
    elif m == "spdp":
        op("spdp"); _src(e, ops[0], 0); e.rd, e.rdh = _pair(ops[1])
    elif m == "dpsp":
        op("dpsp"); e.ra, e.rah = _pair(ops[0]); e.rd = _reg(ops[1])
    elif m in DP:
        name, sub, wl, wh = DP[m]
        op(name); e.sub = sub; e.k[2], e.k[3] = wl, wh
        if sub & 1:
            _src(e, ops[0], 0)
        else:
            e.ra, e.rah = _pair(ops[0])
        if sub & 2:
            _src(e, ops[1], 1)
        else:
            e.rb, e.rbh = _pair(ops[1])
        e.rd, e.rdh = _pair(ops[2])
    elif m in ("cmpeqsp", "cmpgtsp", "cmpltsp"):
        op("fcmp"); e.sub = ("cmpeqsp", "cmpgtsp", "cmpltsp").index(m)
        _src(e, ops[0], 0); _src(e, ops[1], 1); e.rd = _reg(ops[2]); e.w = 0
    elif m in ("cmpeqdp", "cmpgtdp", "cmpltdp"):
        op("dcmp"); e.sub = ("cmpeqdp", "cmpgtdp", "cmpltdp").index(m)
        e.ra, e.rah = _pair(ops[0]); e.rb, e.rbh = _pair(ops[1]); e.rd = _reg(ops[2]); e.w = 1
    elif m == "abssp":
        op("abssp"); _src(e, ops[0], 0); e.rd = _reg(ops[1])
    elif m in ("rcpsp", "rsqrsp"):
        op("rcpsp"); e.sub = m == "rsqrsp"; _src(e, ops[0], 0); e.rd = _reg(ops[1])
    else:
        raise _Py


def encode(i, pyid):
    """Insn -> Ins (pyid used when it stays in Python)"""
    e = Ins()
    e.addr = i.addr
    e.size = i.size
    e.par = 1 if i.par else 0
    e.header = 1 if i.header else 0
    e.unit = UNITS.get(i.unit[:2], 255) if i.unit else 255
    e.side = i.side or 0
    e.creg, e.cneg = -1, 0
    if i.cond is not None:
        e.creg, e.cneg = REGIDX[i.cond[0]], 1 if i.cond[1] else 0
    if i.header or i.mnem in ("header", "undefined"):
        e.op = O["py"]; e.py = pyid
        return e, True
    try:
        _encode_body(i, e)
        return e, False
    except (_Py, ValueError, isa.Unimplemented, IndexError, KeyError, TypeError):
        e2 = Ins()
        ctypes.memmove(ctypes.byref(e2), ctypes.byref(e), ctypes.sizeof(Ins))
        for f in ("k",):
            for j in range(4):
                e2.k[j] = 0
        e2.op = O["py"]; e2.py = pyid
        if i.mnem == "spmask":
            e2.spk = 1
        return e2, True


# ------------------------------------------------------------------ core
class CtrlMap(dict):
    """cpu.c: control registers live in the C core; unknown names here"""
    def __init__(self, arr):
        super().__init__()
        self._a = arr

    def __getitem__(self, k):
        i = CIDX.get(k)
        if i is not None:
            return self._a[i]
        return dict.get(self, k, 0)

    def get(self, k, d=None):
        i = CIDX.get(k)
        return self._a[i] if i is not None else dict.get(self, k, d)

    def __setitem__(self, k, v):
        i = CIDX.get(k)
        if i is not None:
            self._a[i] = v & MASK
        else:
            dict.__setitem__(self, k, v)

    def __contains__(self, k):
        return k in CIDX or dict.__contains__(self, k)

    def keys(self):
        return list(CTRLS) + list(dict.keys(self))

    def items(self):
        return [(k, self[k]) for k in self.keys()]

    def __iter__(self):
        return iter(self.keys())

    def update(self, other=(), **kw):
        for k, v in (other.items() if hasattr(other, "items") else other):
            self[k] = v

    def copy(self):
        return dict(self.items())


class CCpu:
    """drop-in for cpu.Cpu running c6xcore.c"""

    def __init__(self, mem):
        self.lib = lib = load_lib()
        self.mem = mem
        self.h = lib.c6x_new(21)
        # delay-loop fast-forward (c6xcore.c dff_*); XDJ_DSP_DFF=0 = off
        lib.c6x_set_dff(self.h, 0 if os.environ.get("XDJ_DSP_DFF") == "0" else 1)
        self.r = (u32 * 64).from_address(ctypes.addressof(lib.c6x_regs(self.h).contents))
        self.c = CtrlMap((u32 * 30).from_address(ctypes.addressof(lib.c6x_ctrl(self.h).contents)))
        self.prog = {}
        self.ops = {}
        self.pyins = []                # pyid -> Insn
        self.pyfn = []                 # pyid -> compiled isa handler
        self._ld = {}
        self._ldid = 0
        self._exc = None
        self.unknown = {}
        self.trace = None
        self.code_hook = None
        self.idle_skipped = 0
        self._keep = []
        for lo, hi, buf in getattr(mem, "ram", []):
            arr = (ctypes.c_char * len(buf)).from_buffer(buf)
            self._keep.append(arr)
            lib.c6x_add_region(self.h, lo, hi - lo, ctypes.addressof(arr))
        self._cbs = (RD_T(self._mrd), WR_T(self._mwr), PY_T(self._pyop), LD_T(self._pyld))
        lib.c6x_set_cb(self.h, *self._cbs)

    def __del__(self):
        try:
            self.lib.c6x_free(self.h)
        except Exception:
            pass

    # ---- callbacks (exceptions must not cross into C)
    def _mrd(self, a, n):
        try:
            return self.mem.read(a, n) & MASK
        except BaseException as e:      # noqa: BLE001
            self._fail(e)
            return 0

    def _mwr(self, a, n, v):
        try:
            self.mem.write(a, n, v)
        except BaseException as e:      # noqa: BLE001
            self._fail(e)

    def _pyop(self, pid, cyc):
        try:
            f = self.pyfn[pid]
            if f is None:
                f = self.pyfn[pid] = isa.compile_insn(self, self.pyins[pid])
            n = f(self, cyc)
            return int(n) if n else 0
        except BaseException as e:      # noqa: BLE001
            self._fail(e)
            return 0

    def _pyld(self, lid, lo, hi):
        try:
            fn, size = self._ld.pop(lid)
            fn(self, lo | (hi << 32) if size == 8 else lo)
        except BaseException as e:      # noqa: BLE001
            self._fail(e)

    def _fail(self, e):
        if self._exc is None:
            self._exc = e
        self.lib.c6x_stop(self.h)

    # ---- the cpu.Cpu interface used by isa.py handlers
    def wr(self, w, idx, val):
        self.lib.c6x_wr(self.h, w, idx, val & MASK)

    def wr_ctrl(self, w, name, val):
        i = CIDX.get(name)
        if i is None:
            dict.__setitem__(self.c, name, val & MASK)
        else:
            self.lib.c6x_wrc(self.h, w, i, val & MASK)

    def st(self, w, addr, size, val):
        self.lib.c6x_st(self.h, w, addr & MASK, size, val & ((1 << 64) - 1))

    def ld(self, issue, addr, size, fn):
        self._ldid = (self._ldid + 1) & 0xFFFFFFFF
        self._ld[self._ldid] = (fn, size)
        self.lib.c6x_ldpy(self.h, issue, addr & MASK, size, self._ldid)

    def branch(self, issue, target):
        self.lib.c6x_branch(self.h, issue, target & MASK)

    def cond(self, c):
        if c is None:
            return True
        v = self.r[REGIDX[c[0]]]
        return (v == 0) if c[1] else (v != 0)

    def _next_addr(self, a):
        i = self.prog.get(a)
        n = a + (i.size if i else 4)
        h = self.prog.get(n)
        while h is not None and h.header:
            n += 4
            h = self.prog.get(n)
        return n

    # ---- program
    def add_code(self, insns):
        self.prog.update(insns)
        arr = (Ins * len(insns))()
        for k, (a, i) in enumerate(insns.items()):
            pid = len(self.pyins)
            e, used = encode(i, pid)
            if used:
                self.pyins.append(i)
                self.pyfn.append(None)
            arr[k] = e
        if self.lib.c6x_add_code(self.h, arr, len(insns)) < 0:
            raise Halt("C core code table full")

    def drop_code(self, lo, hi):
        for a in [a for a in self.prog if lo <= a < hi]:
            self.prog.pop(a, None)
        self.lib.c6x_drop_code(self.h, lo, hi)

    def clear_code(self):
        self.prog.clear()
        self.pyins.clear()
        self.pyfn.clear()
        self.lib.c6x_clear_code(self.h)

    def ifr_or(self, m):
        self.lib.c6x_ifr_or(self.h, m & MASK)

    def ifr_and(self, m):
        self.lib.c6x_ifr_and(self.h, m & MASK)

    # ---- state
    def settle(self):
        """make r/c/memory exactly what plain execution would show now (a
        delay call fast-forwarded with interrupts off is re-run up to the
        current cycle; no-op otherwise). The status getters below call it;
        code that reads the raw `r`/`c` views mid-run must call it first."""
        self.lib.c6x_settle(self.h)

    def _g(f):
        def get(s):
            s.lib.c6x_settle(s.h)
            return f(s)
        return get

    pc = property(_g(lambda s: s.lib.c6x_pc(s.h)), lambda s, v: s.lib.c6x_set_pc(s.h, v & MASK))
    cycle = property(lambda s: s.lib.c6x_cycle(s.h), lambda s, v: s.lib.c6x_set_cycle(s.h, v))
    idle = property(lambda s: bool(s.lib.c6x_idle(s.h)), lambda s, v: s.lib.c6x_set_idle(s.h, int(bool(v))))
    stall = property(_g(lambda s: s.lib.c6x_stall(s.h)))
    ev = property(_g(lambda s: s.lib.c6x_evcount(s.h)))
    pend = property(_g(lambda s: s.lib.c6x_npend(s.h)))
    sp = property(_g(lambda s: True if s.lib.c6x_sp_on(s.h) else None))
    splx_irq = property(_g(lambda s: s.lib.c6x_splx_pending(s.h) or None))
    branch_at = property(_g(lambda s: s.lib.c6x_npend(s.h) or None))
    ep_count = property(_g(lambda s: s.lib.c6x_counts(s.h, 0)))
    insn_count = property(_g(lambda s: s.lib.c6x_counts(s.h, 1)))
    interrupts = property(lambda s: s.lib.c6x_counts(s.h, 2))
    py_calls = property(lambda s: s.lib.c6x_counts(s.h, 3))
    idle_jumped = property(lambda s: s.lib.c6x_counts(s.h, 4))   # busy-wait cycles jumped (exact)
    idle_jumps = property(lambda s: s.lib.c6x_counts(s.h, 5))
    idle_refhits = property(lambda s: s.lib.c6x_counts(s.h, 6))

    # ---- run
    def run_cycles(self, n):
        """run n cycles (decoding code on demand via code_hook)"""
        lib, h = self.lib, self.h
        while n > 0:
            k = lib.c6x_run(h, n)
            n -= k
            st = lib.c6x_status(h)
            if st == ST_OK:
                continue
            if st == ST_IDLE:
                # the idle loop is a fixed point: nothing changes until an
                # interrupt, and none can arrive inside this call
                lib.c6x_skip(h, n)
                self.idle_skipped += n
                return
            if st == ST_NEED:
                pc = lib.c6x_need(h)
                if self.code_hook is None:
                    raise Halt(f"no code at {pc:#010x}")
                before = len(self.prog)
                self.code_hook(pc)
                if pc not in self.prog or len(self.prog) == before and pc not in self.prog:
                    raise Halt(f"no code at {pc:#010x}")
                continue
            if st == ST_STOP:
                e, self._exc = self._exc, None
                if e is not None:
                    raise e
                continue
            raise Halt(f"C core error {lib.c6x_err(h)} at {lib.c6x_err_pc(h):#010x} "
                       f"(pc {self.pc:#010x})")

    def step(self):
        self.run_cycles(1)

    def run(self, cycles, stop=None):
        if stop is None:
            self.run_cycles(cycles)
            return False
        for _ in range(cycles):
            self.run_cycles(1)
            if stop(self):
                return True
        return False


def make_cpu(bus):
    if os.environ.get("XDJ_DSP_CORE", "c") == "py":
        from .cpu import Cpu
        return Cpu(bus)
    try:
        return CCpu(bus)
    except (OSError, subprocess.CalledProcessError):
        from .cpu import Cpu
        return Cpu(bus)


def watch(cpu, lo, hi):
    """debug: record DSP stores into [lo, hi) (last 256; pc is approximate:
    the pc when the store commits, a few packets after it issued)"""
    lib = cpu.lib
    lib.c6x_watch.argtypes = [ctypes.c_void_p, u32, u32]
    lib.c6x_watch(cpu.h, lo, hi)


def watch_log(cpu):
    lib = cpu.lib
    lib.c6x_watch_get.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(u32)]
    lib.c6x_watch_get.restype = ctypes.c_int
    out = (u32 * 4)()
    n = lib.c6x_watch_get(cpu.h, 0, out)
    res = []
    for k in range(max(0, n - 256), n):
        lib.c6x_watch_get(cpu.h, k, out)
        res.append(tuple(out))
    return n, res


def set_idle_loop(cpu, head, lo, hi, more=()):
    """arm the busy-wait skip (XDJ-700 v1.15: 0xC004C8CC, stack
    0x11804AE0..0x11805C00, from cdj-nxs2-qemu models/xdj700.conf).
    more: further loop heads of the same idle task (same stack), e.g. the
    playing deck's channel-poll loop 0xC0048C2E."""
    if hasattr(cpu, "lib"):
        cpu.lib.c6x_set_idle_loop(cpu.h, head, lo, hi)
        # a prebuilt core from before the multi-head skip (the Windows DLL)
        # has no c6x_add_idle_head: keep the single loop rather than fail
        add = getattr(cpu.lib, "c6x_add_idle_head", None)
        if add is not None:
            for h in more:
                add(cpu.h, h)
