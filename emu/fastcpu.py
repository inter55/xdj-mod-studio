"""FastSH4: the C interpreter core (emu/fastcore.c) behind the SH4 API.

Drop-in for emu.sh4.SH4 as Machine.cpu. The C core runs the instruction
stream against the same bytearrays MachineMem owns; everything that is not
plain memory (peripherals) is a per-byte callback into MachineMem.dev,
exactly like MachineMem.r8phys/w8phys. Between slices this module does what
sh4.py did per instruction: interrupt delivery, device ticks (1 cycle per
loop iteration, as sh4.py), the idle-task fast-forward, and SLEEP.

Falls back to the Python core (same state, converted both ways):
  - whenever cpu.trace is set (per-instruction hooks are a debugging tool;
    Machine.run(stop_on_pc=...) and friends use them),
  - for LDTLB and with the MMU on (the firmware runs with it off).

Checkpoints are interchangeable with the Python core (same __getstate__).
Select with XDJ_CORE=py to force the Python core everywhere.
"""
import collections.abc
import ctypes
import os
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "fastcore.c")
LIB = os.path.join(HERE, "_fastcore.dll" if sys.platform == "win32"
                   else "_fastcore.so")

R_LIMIT, R_IDLE, R_SLEEP, R_IO, R_FALLBACK = range(5)
_EV_STATS = {}         # XDJ_EV_STATS=1: run-loop counters (debugging)
MD, RB, BL = 1 << 30, 1 << 29, 1 << 28
SLICE = 8192           # max SH steps per core_run (device tick granularity)


def _slice():
    """XDJ_SLICE overrides; play mode (XDJ_PLAY=1) ticks devices 16x less
    often (not cycle-exact vs the default)."""
    v = os.environ.get("XDJ_SLICE")
    if v:
        return int(v)
    return 131072 if os.environ.get("XDJ_PLAY") == "1" else SLICE

u32, u64, vp = ctypes.c_uint32, ctypes.c_uint64, ctypes.c_void_p
IO_RD = ctypes.CFUNCTYPE(u32, u32)
IO_WR = ctypes.CFUNCTYPE(None, u32, u32)
IO_RDS = ctypes.CFUNCTYPE(u32, u32, u32)
IRQ_CB = ctypes.CFUNCTYPE(None, u32)
TDG_CONT = ctypes.CFUNCTYPE(None, u32, u32)
IO_WRS = ctypes.CFUNCTYPE(None, u32, u32, u32)

_REGS = ("pc", "pr", "gbr", "vbr", "mach", "macl", "sr", "ssr", "spc", "sgr",
         "dbr", "tra", "expevt", "intevt", "tea", "pteh", "ptel", "ptea",
         "ttb", "mmucr", "ccr", "fpscr", "fpul")


class TdgIO(ctypes.Structure):
    """emu/fastcore.c TdgIO: one 2DG display-list run in C"""
    _fields_ = [("reg", u32 * 64), ("dirty", u64),
                ("lcx", ctypes.c_int32), ("lcy", ctypes.c_int32),
                ("ssr_or", u32), ("irq_ini", u32), ("irq_tri", u32),
                ("stop_pc", u32), ("ops", u32), ("fb_bytes", u64)]


class _DspRegion(ctypes.Structure):
    _fields_ = [("base", u32), ("len", u32), ("ptr", vp)]


class Core(ctypes.Structure):
    _fields_ = ([("r", u32 * 16), ("alt", u32 * 8)] +
                [(n, u32) for n in _REGS] +
                [("fr", u32 * 32), ("steps", u64), ("cycles", u64),
                 ("sleeping", u32), ("low_is_ram", u32),
                 ("flash", vp), ("flash_len", u32), ("pad0", u32),
                 ("ram0", vp), ("ram1", vp), ("fb0", vp), ("fb1", vp),
                 ("misc", vp), ("sq", vp), ("fb_writes", u64),
                 ("io_rd", IO_RD), ("io_wr", IO_WR),
                 ("idle_pc", u32), ("idle_skip", u32),
                 ("io_break", u32), ("exc_n", u32),
                 ("exc", (u32 * 7) * 32), ("exc_lost", u32), ("pad1", u32),
                  ("io_rds", IO_RDS), ("io_wrs", IO_WRS),
                  ("cs6_addr", u32), ("cs6_ctrl", u32), ("cs6_run", u32),
                  ("cs6_bulk", u64),
                 ("hpid_buf", vp), ("hpid_n", u64), ("hpid_cap", u64),
                 ("hpid_acc", u32), ("hpid_addr", u32), ("hpid_cnt", ctypes.c_uint8),
                 ("hpid_pad", ctypes.c_uint8 * 3), ("hpid_bytes", u64),
                 ("cs6_py", u32), ("cs6_pad", u32),
                 ("tdg_reg", u32 * 64), ("tdg_ssr", u32),
                 ("tdg_dirty", u64), ("tdg_dirty_ssr", u64),
                 ("tdg_py", u32), ("tdg_on", u32),
                 ("cs6_fast", u32), ("dsp_region_n", u32),
                 ("dsp_region", _DspRegion * 8),
                 ("cur_pc", u32), ("cur_pad", u32), ("run_commit", u64),
                 ("dmac_on", u32), ("dmac_pad", u32), ("dmac", u32 * 144),
                 ("dmaor", u32 * 3), ("dmaor_pad", u32),
                 ("tdg_cfast", u32), ("tdg_runs_c", u32),
                 ("tdg_lcx", ctypes.c_int32), ("tdg_lcy", ctypes.c_int32),
                 ("tdg_lc_dirty", u32), ("tdg_pad3", u32),
                 ("irq_cb", IRQ_CB), ("tdg_cont", TDG_CONT),
                 # play-mode C event loop (2026-10-27): mirrors the tail of
                 # fastcore.c's Core exactly; core_sizeof checks the layout
                 ("ev_on", u32), ("ev_pad0", u32),
                 ("tmu", (((u32 * 4) * 3) * 2)),
                 ("tstr", u32 * 2),
                 ("cmt_str", u32), ("cmt_csr", u32), ("cmt_cnt", u32),
                 ("cmt_cor", u32), ("cmt_acc", u32),
                 ("lcdc_acc", u32), ("lcdc_ldintr", u32),
                 ("lcdc_frame_cyc", u32), ("lcdc_frames", u32),
                 ("intc_ipr", ctypes.c_uint8 * 0x28),
                 ("intc_imr", ctypes.c_uint8 * 13),
                 ("ev_lat", ctypes.c_uint8), ("ev_pad1", ctypes.c_uint8 * 7),
                 ("ev_cyc", u64),
                 ("port_on", u32), ("scif_on", u32), ("scif_rx", u32 * 3),
                 ("hpid_wr", u32), ("hint_bit", u32),
                 ("port", vp)])

_DMAC_BASES = (0xFE008000, 0xFDC08000, 0xFE009000)
_DMA_KEYS = ("sar", "dar", "tcr", "chcr")
_DMA_IDX = {k: i for i, k in enumerate(_DMA_KEYS)}


class _DmaCh(collections.abc.MutableMapping):
    """sh7724 DMAC channel dict backed by the C core's shared register
    words (Core.dmac): Python and C see one copy, no syncing."""
    __slots__ = ("_a", "_i")

    def __init__(self, arr, i):
        self._a, self._i = arr, i

    def __getitem__(self, k):
        return self._a[self._i + _DMA_IDX[k]]

    def __setitem__(self, k, v):
        self._a[self._i + _DMA_IDX[k]] = v & 0xFFFFFFFF

    def __delitem__(self, k):
        raise TypeError("DMAC channel registers cannot be deleted")

    def __iter__(self):
        return iter(_DMA_KEYS)

    def __len__(self):
        return 4

    def __reduce__(self):                 # pickles / copies as a plain dict
        return (dict, (dict(self),))


class _DmaBase(collections.abc.MutableMapping):
    """dev.dma[base]: {"dmaor": <shared word>, "ch": {n: _DmaCh}}"""
    __slots__ = ("_or", "_b", "_ch")

    def __init__(self, dmaor, b, ch):
        self._or, self._b, self._ch = dmaor, b, ch

    def __getitem__(self, k):
        if k == "dmaor":
            return self._or[self._b]
        if k == "ch":
            return self._ch
        raise KeyError(k)

    def __setitem__(self, k, v):
        if k == "dmaor":
            self._or[self._b] = v & 0xFFFFFFFF
        else:
            raise KeyError(k)

    def __delitem__(self, k):
        raise TypeError("cannot delete")

    def __iter__(self):
        return iter(("dmaor", "ch"))

    def __len__(self):
        return 2

    def __reduce__(self):
        return (dict, ({"dmaor": self["dmaor"],
                        "ch": {n: dict(v) for n, v in self._ch.items()}},))


_TMU_KEYS = ("tcor", "tcnt", "tcr", "acc")


class _TmuCh(collections.abc.MutableMapping):
    """dev.tmu[base][ch] backed by the C core's shared words (ev_on)."""
    __slots__ = ("_a", "_u", "_ch")

    def __init__(self, unit_arr, ch):
        self._a, self._u, self._ch = unit_arr, None, ch

    def __getitem__(self, k):
        return self._a[self._ch][ _TMU_KEYS.index(k)]

    def __setitem__(self, k, v):
        self._a[self._ch][_TMU_KEYS.index(k)] = v & 0xFFFFFFFF

    def __delitem__(self, k):
        raise TypeError("cannot delete")

    def __iter__(self):
        return iter(_TMU_KEYS)

    def __len__(self):
        return 4

    def __reduce__(self):
        return (dict, (dict(self),))


class _TstrView(collections.abc.MutableMapping):
    """dev.tstr {base+4: byte} over Core.tstr while the C core owns it."""
    __slots__ = ("_c",)
    _BASES = (0xFFD80004, 0xFFD90004)

    def __init__(self, core):
        self._c = core

    def __getitem__(self, k):
        return self._c.tstr[self._BASES.index(k)] & 0xFF

    def __setitem__(self, k, v):
        self._c.tstr[self._BASES.index(k)] = v & 0xFF

    def __delitem__(self, k):
        raise TypeError("cannot delete")

    def __iter__(self):
        return iter(self._BASES)

    def __len__(self):
        return 2

    def __reduce__(self):
        return (dict, (dict(self),))


class _ImrView(collections.abc.MutableMapping):
    """dev.imr {idx: byte} over Core.intc_imr while the C core owns it."""
    __slots__ = ("_c",)

    def __init__(self, core):
        self._c = core

    def __getitem__(self, k):
        return self._c.intc_imr[k]

    def __setitem__(self, k, v):
        self._c.intc_imr[k] = v & 0xFF

    def __delitem__(self, k):
        raise TypeError("cannot delete")

    def __iter__(self):
        return iter(range(13))

    def __len__(self):
        return 13

    def __reduce__(self):
        return (dict, ({k: self[k] for k in self},))


_lib = None


def load_lib():
    """Compile fastcore.c if needed (clang/cc) and load it. None if no
    compiler is available -- callers fall back to the Python core."""
    global _lib
    if _lib is not None:
        return _lib
    try:
        # XDJ_FASTCORE_LIB: load a specific build (tools/differ.py uses it to
        # compare the current fastcore.c against a compiled reference copy).
        alt = os.environ.get("XDJ_FASTCORE_LIB")
        if alt and os.path.exists(alt):
            lib = ctypes.CDLL(alt)
        else:
            # a bundled app ships the compiled library without the source
            # (Windows: the app ships a prebuilt DLL and there is no compiler)
            if sys.platform != "win32" and os.path.exists(SRC) and (not os.path.exists(LIB) or
                    os.path.getmtime(LIB) < os.path.getmtime(SRC)):
                tmp = LIB + ".tmp"
                subprocess.check_call(["cc", "-O2", "-shared", "-fPIC", "-o", tmp,
                                       SRC, "-lm"])
                os.replace(tmp, LIB)
            lib = ctypes.CDLL(LIB)
    except (OSError, subprocess.CalledProcessError):
        return None
    try:                              # 2DG display lists in C (2026-10-26)
        lib.core_tdg_run.argtypes = [ctypes.POINTER(Core), u32,
                                     ctypes.POINTER(TdgIO), u32]
        lib.core_tdg_run.restype = ctypes.c_int
    except AttributeError:
        pass                          # reference build without it
    lib.core_run.argtypes = [ctypes.POINTER(Core), u64, ctypes.POINTER(u64)]
    lib.core_run.restype = ctypes.c_int
    lib.core_enter_interrupt.argtypes = [ctypes.POINTER(Core), u32, u32]
    lib.core_set_sr.argtypes = [ctypes.POINTER(Core), u32]
    try:                          # play-mode C event loop (2026-10-27)
        lib.core_ev_next.argtypes = [ctypes.POINTER(Core)]
        lib.core_ev_next.restype = u64
        lib.core_ev_tick.argtypes = [ctypes.POINTER(Core), u64]
        lib.core_ev_deliver.argtypes = [ctypes.POINTER(Core)]
    except AttributeError:
        pass                      # reference build: play mode just stays off
    lib.core_read_virt.argtypes = [ctypes.POINTER(Core), u32, ctypes.c_int,
                                   ctypes.POINTER(ctypes.c_int)]
    lib.core_read_virt.restype = u32
    _alt = os.environ.get("XDJ_FASTCORE_LIB")
    if lib.core_sizeof() != ctypes.sizeof(Core):
        # a reference build (XDJ_FASTCORE_LIB) predating the newest fields is
        # fine: the C side reads its own (shorter) struct prefix only, and the
        # extra ctypes tail just stays untouched.  Anything else is a bug.
        if not (_alt and 0 < ctypes.sizeof(Core) - lib.core_sizeof() <= 1024):
            raise RuntimeError("fastcore.c / fastcpu.py struct layout mismatch")
    try:
        lib.core_hpid  # noqa: B018 (pre-capture reference builds lack it)
    except AttributeError:
        # a reference build predating the capture: inject a stub by hand
        # (CDLL.__getattr__ dlsyms on first touch, so go around it)
        def _stub(_c, n):
            n.contents.value = 0
            return 0          # NULL: no words captured
        _fn = ctypes.CFUNCTYPE(ctypes.c_void_p,
                               ctypes.POINTER(Core), ctypes.POINTER(u64))(_stub)
        object.__setattr__(lib, "core_hpid", _fn)
        _fn.argtypes = [ctypes.POINTER(Core), ctypes.POINTER(u64)]
        _fn.restype = ctypes.c_void_p
    # native loop fast paths (2026-10-25); absent in older reference builds
    try:
        lib.core_set_fastpath.argtypes = [ctypes.c_int]
        lib.core_set_fastpath(0 if os.environ.get("XDJ_FASTPATH") == "0" else 1)
    except AttributeError:
        pass
    try:                          # data-page cache (2026-10-26)
        lib.core_set_dtlb.argtypes = [ctypes.c_int]
        lib.core_set_dtlb(0 if os.environ.get("XDJ_DTLB") == "0" else 1)
    except AttributeError:
        pass
    try:                          # decode-once table (2026-10-26)
        lib.core_set_dtab.argtypes = [ctypes.c_int]
        lib.core_set_dtab(0 if os.environ.get("XDJ_DTAB") == "0" else 1)
    except AttributeError:
        pass
    try:                          # block engine (2026-10-26)
        lib.core_set_blocks.argtypes = [ctypes.c_int]
        lib.core_set_blocks(0 if os.environ.get("XDJ_BLOCKS") == "0" else 1)
        lib.core_blk_stat.argtypes = [ctypes.c_int]
        lib.core_blk_stat.restype = ctypes.c_uint64
    except AttributeError:
        pass
    try:                          # native code for blocks (shjit.c, arm64 Mac)
        lib.core_set_jit.argtypes = [ctypes.c_int]
        lib.core_set_jit(0 if os.environ.get("XDJ_JIT") == "0" else 1)
        lib.core_jit_stat.argtypes = [ctypes.c_int]
        lib.core_jit_stat.restype = ctypes.c_uint64
    except AttributeError:
        pass
    try:                          # debug: last bail reason (fastcore g_fp_bail)
        lib.core_fp_bail.restype = ctypes.c_int
        lib.core_fp_bails.argtypes = [ctypes.POINTER(u64)]
    except AttributeError:
        pass
    # cs6_fast region serving (2026-10-24): absent in pre-capture reference
    # builds -- stub to no-ops so cs6_fast simply never arms there.
    for _nm, _sig in (("core_clear_dsp_regions",
                       ([ctypes.POINTER(Core)], None)),
                      ("core_add_dsp_region",
                       ([ctypes.POINTER(Core), u32, u32, vp], ctypes.c_int))):
        try:
            getattr(lib, _nm).argtypes = _sig[0]
            getattr(lib, _nm).restype = _sig[1]
        except AttributeError:
            if _nm == "core_clear_dsp_regions":
                def _clr(_c, _b=[]):
                    return None
                object.__setattr__(lib, _nm, _clr)
            else:
                def _add(*_a):
                    return -1
                object.__setattr__(lib, _nm, _add)
    _lib = lib
    return lib


def available():
    return os.environ.get("XDJ_CORE", "fast") != "py" and load_lib() is not None


def _f2u(x):
    try:
        return struct.unpack("<I", struct.pack("<f", float(x)))[0]
    except OverflowError:
        return 0x7F800000 if x > 0 else 0xFF800000


def _u2f(u):
    return struct.unpack("<f", struct.pack("<I", u & 0xFFFFFFFF))[0]


class FastSH4:
    def __init__(self, mem, machine=None):
        object.__setattr__(self, "core", Core())
        object.__setattr__(self, "lib", load_lib())
        self.mem = mem
        self.machine = machine
        self.utlb = []
        self.trace = None
        self.native_hook = None
        self.fpu = None
        self._pins = None
        self._pin_ids = None
        self._dsp_keep = []             # ctypes views of DSP RAM regions
        self._tdg_obj = None            # 2DG device whose regs C mirrors
        self._tdg_gen = -1
        self._py = None
        # cached word-read helper (emu.usb_fast), imported once, not per crossing
        try:
            from emu.usb_fast import fast_io_read
            self._fast_io = fast_io_read
        except ImportError:
            self._fast_io = None
        dev_rd, dev_wr = self._io_rd, self._io_wr
        self._cb_rd = IO_RD(dev_rd)
        self._cb_wr = IO_WR(dev_wr)
        self._cb_rds = IO_RDS(self._io_rds)
        self._cb_wrs = IO_WRS(self._io_wrs)
        self.core.io_rd = self._cb_rd
        self.core.io_wr = self._cb_wr
        self.core.io_rds = self._cb_rds
        self._cb_irq = IRQ_CB(self._irq_from_c)
        self._cb_cont = TDG_CONT(self._tdg_cont_from_c)
        self.core.irq_cb = self._cb_irq
        self.core.tdg_cont = self._cb_cont
        self.core.io_wrs = self._cb_wrs
        self.core.idle_pc = 0x08D6A7C0
        self.reset()
        # 2DG display lists run in C (emu/tdg.py TDG.run asks mem for this;
        # XDJ_TDG_C=0 keeps the pure-Python interpreter)
        if (os.environ.get("XDJ_TDG_C", "1") != "0"
                and hasattr(self.lib, "core_tdg_run")):
            object.__setattr__(self, "_tdg_io", TdgIO())
            mem.tdg_cexec = self._tdg_cexec

    def _tdg_cexec(self, t, addr, limit):
        """Run 2DG list at addr in C. Returns (finished, pc, ops_used):
        finished -> TDG.run returns; else Python continues at pc (an op the
        C side does not handle; nothing of it was done)."""
        io = self._tdg_io
        g = t.reg.get
        io.reg[:] = [g(i * 4, 0) & 0xFFFFFFFF for i in range(64)]
        io.lcx, io.lcy = t.lcofs
        r = self.lib.core_tdg_run(ctypes.byref(self.core), addr & 0xFFFFFFFF,
                                  ctypes.byref(io), limit)
        d = io.dirty
        if d:
            reg = t.reg
            for w in range(64):
                if d >> w & 1:
                    reg[w * 4] = io.reg[w]
            if d >> (0x50 >> 2) & 1 and hasattr(t, "dsts"):
                t.dsts.add(io.reg[0x50 >> 2])
        t.lcofs = (io.lcx, io.lcy)
        if io.fb_bytes:
            self.mem.fb_writes += io.fb_bytes
        t.ssr |= io.ssr_or
        for _ in range(io.irq_ini):
            t.raise_irq("TDG_INI")
        if io.irq_tri:
            t.raise_irq("TDG_TRI")
        return (r == 0, io.stop_pc, io.ops)

    # -- register access (SH4-compatible) ----------------------------------
    def __getattr__(self, name):
        if name in _REGS or name in ("steps", "cycles"):
            return getattr(self.core, name)
        if name == "sleeping":
            return bool(self.core.sleeping)
        raise AttributeError(name)

    def __setattr__(self, name, v):
        if name == "sr":
            self.lib.core_set_sr(ctypes.byref(self.core), v & 0xFFFFFFFF)
        elif name in _REGS or name in ("steps", "cycles"):
            setattr(self.core, name, v & (0xFFFFFFFF if name in _REGS
                                          else 0xFFFFFFFFFFFFFFFF))
        elif name == "sleeping":
            self.core.sleeping = 1 if v else 0
        else:
            object.__setattr__(self, name, v)

    def gr(self, i):
        return self.core.r[i]

    def set_gr(self, i, v):
        self.core.r[i] = v & 0xFFFFFFFF

    def t(self):
        return self.core.sr & 1

    def set_t(self, v):
        self.core.sr = (self.core.sr | 1) if v else (self.core.sr & ~1)

    @property
    def hpid(self):
        """The DSP-image upload log: list of (dsp_addr, value) 32-bit words
        written through the CS6 data port 0x180C0000. Grows in C; read here."""
        c = self.core
        n = ctypes.c_uint64(0)
        p = self.lib.core_hpid(ctypes.byref(c), ctypes.byref(n))
        if not p or not n.value:
            return []
        buf = ctypes.cast(p, ctypes.POINTER(ctypes.c_uint32 * (n.value * 2)))
        return [(buf.contents[i], buf.contents[i + 1])
                for i in range(0, n.value * 2, 2)]

    def imask(self):
        return (self.core.sr >> 4) & 0xF

    def mmu_on(self):
        return bool(self.core.mmucr & 1)

    @property
    def b0(self):
        c = self.core
        return list(c.alt) if c.sr & RB else list(c.r[:8])

    @property
    def b1(self):
        c = self.core
        return list(c.r[:8]) if c.sr & RB else list(c.alt)

    @property
    def rhi(self):
        return list(self.core.r[8:16])

    @property
    def fr(self):
        f = self.core.fr
        return [[_u2f(f[i]) for i in range(16)],
                [_u2f(f[16 + i]) for i in range(16)]]

    def translate(self, addr, write=False, fetch=False):
        from .sh4 import SH4
        return SH4.translate(self, addr, write, fetch)

    def _rd(self, a, n, write=False, fetch=False):
        from .sh4 import SH4
        return SH4._rd(self, a, n, write, fetch)

    def r8(self, a):
        return self._rd(a, 1)

    def r32(self, a):
        return self._rd(a, 4)

    # -- state --------------------------------------------------------------
    def reset(self):
        c = self.core
        for i in range(16):
            c.r[i] = 0
        for i in range(8):
            c.alt[i] = 0
        c.pc = 0xA0000000
        c.pr = 0
        c.sr = MD | RB | BL | 0xF0 | (1 << 15)
        c.vbr = 0
        c.sleeping = 0
        if c.hpid_buf:
            ctypes.free(ctypes.cast(c.hpid_buf, ctypes.c_void_p))
        c.hpid_buf = 0
        c.hpid_n = 0
        c.hpid_cap = 0
        c.hpid_acc = 0
        c.hpid_addr = 0
        c.hpid_cnt = 0

    def _cs6_out(self):
        """Flush the C DSP-bridge registers to the Python device (which
        owns save/load and the Python-core path)."""
        if self.core.cs6_py:            # Python owns CS6 outright
            return
        d = self.mem.dev.cs6
        d["addr"], d["ctrl"], d["run"] = (self.core.cs6_addr & 0xFFFFFFFF,
                                          self.core.cs6_ctrl & 0xFFFFFFFF,
                                          self.core.cs6_run & 0xFFFFFFFF)
        d["bulk"] = self.core.cs6_bulk

    def _cs6_in(self):
        """Adopt the Python device's DSP-bridge registers into C."""
        self.core.cs6_py = 1 if getattr(self.mem.dev, "cs6_py", False) else 0
        d = self.mem.dev.cs6
        self.core.cs6_addr = d.get("addr", 0) & 0xFFFFFFFF
        self.core.cs6_ctrl = d.get("ctrl", 0) & 0xFFFFFFFF
        self.core.cs6_run = d.get("run", 0) & 0xFFFFFFFF
        self.core.cs6_bulk = d.get("bulk", 0) & 0xFFFFFFFFFFFFFFFF

    # -- 2DG register-file mirror (emu/tdg.py TDG) ---------------------------
    def _tdg_out(self):
        """Flush the C 2DG register mirror into the Python device (C owns
        the register file 0x00-0xFF while tdg_on)."""
        c = self.core
        if not c.tdg_on or not (c.tdg_dirty or c.tdg_dirty_ssr or
                                c.tdg_lc_dirty or c.tdg_runs_c):
            return
        t = getattr(self, "_tdg_obj", None)
        if t is None:
            return
        if c.tdg_lc_dirty:
            t.lcofs = (c.tdg_lcx, c.tdg_lcy)
            c.tdg_lc_dirty = 0
        if c.tdg_runs_c:
            t.runs = getattr(t, "runs", 0) + c.tdg_runs_c
            c.tdg_runs_c = 0
        reg = t.reg
        d = c.tdg_dirty
        for w in range(64):
            if d >> w & 1:
                reg[w * 4] = c.tdg_reg[w]
        c.tdg_dirty = 0
        if c.tdg_dirty_ssr:
            t.ssr = c.tdg_ssr
            c.tdg_dirty_ssr = 0

    def _tdg_in(self):
        """Adopt the Python device's 2DG registers into the C mirror (after
        Python touched them: a list ran, a restore, an off-mirror write)."""
        c = self.core
        if not c.tdg_on:
            return
        t = getattr(self, "_tdg_obj", None)
        if t is None:
            return
        g = t.reg.get
        c.tdg_reg[:] = [g(i * 4, 0) & 0xFFFFFFFF for i in range(64)]
        c.tdg_ssr = t.ssr & 0xFFFFFFFF
        c.tdg_lcx, c.tdg_lcy = t.lcofs
        c.tdg_dirty = 0
        c.tdg_dirty_ssr = 0
        c.tdg_lc_dirty = 0
        object.__setattr__(self, "_tdg_gen", t.gen)

    def _dmac_attach(self):
        """Back dev.dma with the C core's register words (XDJ_DMAC_C=0
        keeps the plain Python dicts and every access crossing)."""
        c = self.core
        dma = getattr(self.mem.dev, "dma", None)
        if dma is None or os.environ.get("XDJ_DMAC_C", "1") == "0":
            if c.dmac_on:
                self._dmac_detach()
            return
        if isinstance(dma.get(_DMAC_BASES[0]), _DmaBase):
            return                                  # already ours
        arr, dor = c.dmac, c.dmaor
        new = {}
        for b, base in enumerate(_DMAC_BASES):
            src = dma.get(base, {"dmaor": 0, "ch": {}})
            dor[b] = src.get("dmaor", 0) & 0xFFFFFFFF
            chs = {}
            for ch in range(12):
                i = (b * 12 + ch) * 4
                st = src["ch"].get(ch, {})
                for k, key in enumerate(_DMA_KEYS):
                    arr[i + k] = st.get(key, 0) & 0xFFFFFFFF
                chs[ch] = _DmaCh(arr, i)
            new[base] = _DmaBase(dor, b, chs)
        for base, v in dma.items():                 # anything unexpected
            if base not in new:
                new[base] = v
        self.mem.dev.dma = new
        c.dmac_on = 1

    def _dmac_detach(self):
        dev = self.mem.dev
        dev.dma = {b: {"dmaor": v["dmaor"],
                       "ch": {ch: dict(st) for ch, st in v["ch"].items()}}
                   for b, v in dev.dma.items()}
        self.core.dmac_on = 0

    # -- play-mode C event loop ------------------------------------------------
    def _ev_attach(self):
        """Hand the device timers to the C core (play mode, XDJ_EV_C=0
        off). TMU/LCDC/INTC register state becomes shared (views over the
        C arrays, like the DMAC registers) so Python-side reads stay live;
        the PORT mirror and IPR/LDINTR bytes are copied in and written
        back at snapshot. NOT cycle-exact: timer IRQs arrive within one
        pass and the C side delivers them itself."""
        c = self.core
        dev = self.mem.dev
        if c.ev_on or os.environ.get("XDJ_EV_C", "1") == "0" or \
                os.environ.get("XDJ_PLAY") != "1":
            return
        for u, base in enumerate((0xFFD80000, 0xFFD90000)):
            c.tstr[u] = dev.tstr.get(base + 4, 0) & 0xFF
            for ch in range(3):
                st = dev.tmu[base][ch]
                for k, key in enumerate(("tcor", "tcnt", "tcr", "acc")):
                    c.tmu[u][ch][k] = st[key] & 0xFFFFFFFF
        c.lcdc_frame_cyc = dev.FRAME_CYC
        c.lcdc_acc = getattr(dev, "lcdc_acc", 0)
        c.lcdc_frames = getattr(dev, "frames", 0)
        v = dev.reg.get(0xFE940468, 0) | (dev.reg.get(0xFE940469, 0) << 8)
        c.lcdc_ldintr = v & 0xFFFF
        for i in range(13):
            c.intc_imr[i] = dev.imr[i] & 0xFF
        for i in range(0x28):
            c.intc_ipr[i] = dev.reg.get(0x04080000 + i, 0) & 0xFF
        port = bytearray(0x10000)
        for a, by in dev.reg.items():
            if 0x04050000 <= a < 0x04060000:
                port[a - 0x04050000] = by
        self._port_seen = {a - 0x04050000 for a in dev.reg
                           if 0x04050000 <= a < 0x04060000}
        self._port_view = (ctypes.c_char * 0x10000).from_buffer(port)
        self._port_bytes = port
        c.port = ctypes.addressof(self._port_view)
        c.port_on = 1
        c.scif_on = 1
        self._scif_counts()
        de = getattr(dev, "dspemu", None)
        c.hint_bit = (1 if (de.hpic & 4) else 0) if de is not None else 0
        c.hpid_wr = 0
        c.ev_lat = 0
        c.ev_cyc = dev.cyc
        # swap the Python dicts for views over the C arrays (pickling a
        # view yields a plain dict, so snapshots are unaffected)
        tmu = {}
        for u, base in enumerate((0xFFD80000, 0xFFD90000)):
            tmu[base] = {ch: _TmuCh(c.tmu[u], ch) for ch in range(3)}
        dev.tmu = tmu
        dev.tstr = _TstrView(c)
        dev.imr = _ImrView(c)
        dev._evc = c
        c.ev_on = 1

    def _ev_out(self):
        """C mirror -> Python (snapshots, trace fallback, detach)."""
        c = self.core
        if not c.ev_on:
            return
        dev = self.mem.dev
        dev.tmu = {base: {ch: dict(st) for ch, st in chs.items()}
                   for base, chs in dev.tmu.items()}
        dev.tstr = {base + 4: c.tstr[u]
                    for u, base in enumerate((0xFFD80000, 0xFFD90000))}
        dev.imr = {i: c.intc_imr[i] for i in range(13)}   # a dict, as sh7724 keeps it
        dev._evc = None
        c.ev_on = 0
        c.port_on = 0
        c.scif_on = 0
        c.hpid_wr = 0
        dev.lcdc_acc = c.lcdc_acc
        dev.frames = c.lcdc_frames
        dev.reg[0xFE940468] = c.lcdc_ldintr & 0xFF
        dev.reg[0xFE940469] = (c.lcdc_ldintr >> 8) & 0xFF
        for i in range(0x28):
            if c.intc_ipr[i]:
                dev.reg[0x04080000 + i] = c.intc_ipr[i]
        port, base = self._port_bytes, 0x04050000
        seen = getattr(self, "_port_seen", set())
        for off in range(0x10000):
            b = port[off]
            if b or off in seen:
                dev.reg[base + off] = b
        self._port_view = None                 # release the from_buffer view
        c.port = 0

    def _scif_counts(self):
        """rx byte counts -> C (SCFSR/SCFDR polls stay inside the core)."""
        c = self.core
        dev = self.mem.dev
        if not c.scif_on:
            return
        for ch in range(3):
            n = len(dev.rx[ch])
            c.scif_rx[ch] = n if n < 16 else 16

    def _ev_pass(self, cyc):
        """One Python pass done at device cycle `cyc`: advance the C
        timers, deliver their IRQs, refresh the pin mirrors."""
        c = self.core
        lib = self.lib
        lib.core_ev_tick(c, cyc)
        lib.core_ev_deliver(c)
        if c.ev_on:
            # rx queues can change mid-slice (panel echo via the DMAC
            # callbacks): SCFSR/SCFDR polls in C must see live counts,
            # or the firmware's panel handshake stalls (2026-11-01).
            self._scif_counts()
        de = getattr(self.mem.dev, "dspemu", None)
        if de is not None:
            c.hint_bit = 1 if (de.hpic & 4) else 0
            if c.hpid_wr:
                if getattr(de, "_hpid_code_hit", False):
                    c.hpid_wr = 0      # a code page went through HPID
            elif (c.cs6_fast and de.threaded and
                  getattr(de, "_thr", None) is not None and
                  not getattr(de, "_hpid_code_hit", False) and
                  not getattr(de, "_acc", None) and
                  os.environ.get("XDJ_HPID_WR", "1") != "0"):
                c.hpid_wr = 1          # DSP on its thread: stores are plain RAM

    def _irq_from_c(self, which):
        try:
            self.mem.dev.tdg.raise_irq("TDG_TRI" if which else "TDG_INI")
        except Exception as e:
            self._io_err = repr(e)

    def _tdg_cont_from_c(self, pc, ops):
        """A list the C core stopped in (unknown op / non-RAM memory):
        Python continues it, exactly like TDG.run after _tdg_cexec."""
        dev = self.mem.dev
        try:
            before = len(dev.pending)
            self._tdg_out()
            dev.tdg.run_from(pc, ops)
            self._tdg_in()
            if len(dev.pending) != before:
                self.core.io_break = 1
        except Exception as e:
            self._io_err = repr(e)
            self.core.io_break = 1

    def _tdg_attach(self):
        """Hand the 2DG register file to C once the device exists
        (XDJ_TDG_MIRROR=0 keeps every access in Python)."""
        c = self.core
        t = getattr(self.mem.dev, "tdg", None)
        if t is None or os.environ.get("XDJ_TDG_MIRROR", "1") == "0":
            if c.tdg_on:
                self._tdg_out()
                c.tdg_on = 0
            return
        if t is not getattr(self, "_tdg_obj", None):
            if c.tdg_on:
                self._tdg_out()
            object.__setattr__(self, "_tdg_obj", t)
            t.pull = self._tdg_out          # TDG.state() flushes first
            c.tdg_on = 1
            self._tdg_in()
        elif t.gen != getattr(self, "_tdg_gen", -1):          # restored behind our back
            self._tdg_in()
        # SCLR runs the list straight from C (XDJ_TDG_CFAST=0: via Python)
        mem = self.mem
        c.tdg_cfast = 1 if (c.tdg_on and getattr(mem, "tdg_cexec", None)
                            is not None
                            and getattr(t, "draw_log", None) is None
                            and getattr(mem, "fb_hook", None) is None
                            and os.environ.get("XDJ_TDG_CFAST", "1") != "0"
                            ) else 0

    def __getstate__(self):
        self._cs6_out()
        self._tdg_out()
        self._ev_out()
        c = self.core
        d = {n: getattr(c, n) for n in _REGS}
        d.update(b0=self.b0, b1=self.b1, rhi=self.rhi, fr=self.fr,
                 fr_raw=list(c.fr), utlb=list(self.utlb),
                 sleeping=bool(c.sleeping), steps=c.steps, cycles=c.cycles)
        return d

    def __setstate__(self, d):
        c = self.core
        for n in _REGS:
            setattr(c, n, d.get(n, 0) & 0xFFFFFFFF)
        cur, alt = (d["b1"], d["b0"]) if c.sr & RB else (d["b0"], d["b1"])
        for i in range(8):
            c.r[i] = cur[i] & 0xFFFFFFFF
            c.alt[i] = alt[i] & 0xFFFFFFFF
            c.r[8 + i] = d["rhi"][i] & 0xFFFFFFFF
        if d.get("fr_raw"):
            for i in range(32):
                c.fr[i] = d["fr_raw"][i]
        else:
            for b in range(2):
                for i in range(16):
                    c.fr[b * 16 + i] = _f2u(d["fr"][b][i])
        self.utlb = list(d.get("utlb", []))
        c.sleeping = 1 if d.get("sleeping") else 0
        c.steps = d.get("steps", 0)
        c.cycles = d.get("cycles", 0)
        if c.hpid_buf:
            ctypes.free(ctypes.cast(c.hpid_buf, ctypes.c_void_p))
        c.hpid_buf = 0
        c.hpid_n = 0
        c.hpid_cap = 0
        c.hpid_acc = 0
        c.hpid_addr = 0
        c.hpid_cnt = 0
        c.ev_on = 0                    # re-armed from the dicts on next run
        c.port_on = 0
        c.scif_on = 0
        c.hpid_wr = 0
        c.port = 0
        self._cs6_in()
        self._tdg_in()

    # -- memory binding -------------------------------------------------------
    def _bind(self):
        m = self.mem
        bufs = (m.ram0, m.ram1, m.fb[0], m.fb[1], m.misc, m.sq)
        ids = tuple(id(b) for b in bufs) + (id(m.flash),)
        if ids == self._pin_ids:
            return
        pins = [(ctypes.c_char * len(b)).from_buffer(b) for b in bufs]
        fl = ctypes.c_char_p(bytes(m.flash)) if not isinstance(m.flash, bytes) \
            else ctypes.c_char_p(m.flash)
        c = self.core
        c.ram0, c.ram1, c.fb0, c.fb1, c.misc, c.sq = \
            [ctypes.addressof(p) for p in pins]
        c.flash = ctypes.cast(fl, vp).value
        c.flash_len = len(m.flash)
        self._pins = (pins, fl, m.flash)
        self._pin_ids = ids

    def _cached_dspemu(self):
        """The live-DSP device (cs6_py route): cached after first hit."""
        d = getattr(self, "_cs6_dev", None)
        if d is None:
            d = self.mem.dev
            if getattr(d, "dspemu", None) is not None:
                object.__setattr__(self, "_cs6_dev", d)
        return d

    def _io_rd(self, a):
        a &= 0xFFFFFFFF
        # CS6 (live DSP): straight to the word-semantic reader; the generic
        # read where the SDK bridge sits LAST among a dozen range checks and
        # the HPI poll crosses hundreds of thousands of times per second.
        if 0x18000000 <= a < 0x18100000:
            try:
                d = self._cached_dspemu()
                if self.core.cs6_fast:
                    d.dspemu.hpia = self.core.cs6_addr  # C mirror owns HPIA
                return d._cs6_read(a, 1) & 0xFF
            except Exception as e:
                self._io_err = repr(e)
                object.__setattr__(self, "_cs6_dev", None)
                return 0
        is2dg = 0x04680000 <= a < 0x04690000
        try:
            if is2dg:
                self._tdg_out()
            v = self.mem.dev.read(a, 1) & 0xFF
            if is2dg:
                self._tdg_in()
            return v
        except Exception as e:           # a device bug must not kill the core
            self._io_err = repr(e)
            return 0

    def _io_wr(self, a, v):
        # End the slice only when the write can change what interrupt the
        # CPU should take next: a new pending source, or an INTC register
        # (IPR/IMR/IMCR). Most device writes (GPIO strobes, DMA/2DG setup)
        # do neither, and breaking on each cost ~half the run time.
        dev = self.mem.dev
        a &= 0xFFFFFFFF
        if 0x18000000 <= a < 0x18100000:
            try:
                d = self._cached_dspemu()
                if self.core.cs6_fast:
                    d.dspemu.hpia = self.core.cs6_addr  # C mirror owns HPIA
                d._cs6_write(a, 1, v & 0xFF)
                return                      # HPIC/HPIA never raise interrupts
            except Exception as e:
                self._io_err = repr(e)
                object.__setattr__(self, "_cs6_dev", None)
                self.core.io_break = 1
                return
        is2dg = 0x04680000 <= a < 0x04690000
        try:
            before = len(dev.pending)
            if is2dg:
                self._tdg_out()              # C mirror -> python regs first
            dev.write(a, 1, v)
            if is2dg:
                self._tdg_in()               # ...then python -> C mirror
            if len(dev.pending) != before or (a & 0x1FFF0000) == 0x04080000:
                self.core.io_break = 1
        except Exception as e:
            self._io_err = repr(e)
            self.core.io_break = 1

    def _io_rds(self, a, n):
        # One crossing instead of n: the device still sees n identical
        # byte accesses in order (same triggers, same FIFO pops), so this
        # is exactly _io_rd x n with less ctypes overhead.
        a &= 0xFFFFFFFF
        is2dg = 0x04680000 <= a < 0x04690000
        cs6 = is2dg or (0x18000000 <= a < 0x18100000)
        try:
            if cs6:
                d = self._cached_dspemu()
                if not is2dg:
                    if self.core.cs6_fast:
                        d.dspemu.hpia = self.core.cs6_addr  # C mirror owns HPIA
                    # The C64x co-simulation is time-locked PER WORD, not per
                    # byte: a 32-bit HPI access reads the same latched word
                    # four bytes would (auto-increment fires at the same
                    # boundary), so one dspemu.read(a, n) replaces the byte
                    # loop. This is where the firmware's HPI polling spent
                    # 3 of its 4 Python crossings (2026-10-24 profile:
                    # ~490k CS6 byte crossings per emulated second).
                    if n in (2, 4) and not (a & (n - 1)):
                        return d._cs6_read(a, n) & ((1 << (8 * n)) - 1)
                    v = 0
                    for i in range(n):
                        v |= d._cs6_read(a + i, 1) << (8 * i)
                    return v & ((1 << (8 * n)) - 1)
            # Fast path (XDJ_USB_FAST=1): single word-sized dev.read where
            # trivially safe (USB + generic stub); else the loop below.
            fi = self._fast_io
            if fi is not None:
                fv = fi(self.mem.dev, a, n)
                if fv is not None:
                    return fv
            if is2dg:
                self._tdg_out()
            v = 0
            dev_read = self.mem.dev.read
            for i in range(n):
                v |= dev_read(a + i, 1) << (8 * i)
            if is2dg:
                self._tdg_in()
            return v
        except Exception as e:
            self._io_err = repr(e)
            return 0

    def _io_wrs(self, a, v, n):
        dev = self.mem.dev
        a &= 0xFFFFFFFF
        if 0x18000000 <= a < 0x18100000:
            try:
                d = self._cached_dspemu()
                if self.core.cs6_fast:
                    d.dspemu.hpia = self.core.cs6_addr  # C mirror owns HPIA
                # word-at-a-time for the aligned 16/32-bit HPI stores (the
                # PCM pump): dspdev.write's size-n path assembles exactly
                # what n byte writes would (RMW on HPIA/HPIC, word assembly
                # on HPID), with one Python crossing instead of four.
                if n in (2, 4) and not (a & (n - 1)):
                    d._cs6_write(a, n, v & ((1 << (8 * n)) - 1))
                    return
                cs6_write = d._cs6_write
                for i in range(n):             # byte-by-byte (see _io_rds)
                    cs6_write(a + i, 1, (v >> (8 * i)) & 0xFF)
                return
            except Exception as e:
                self._io_err = repr(e)
                object.__setattr__(self, "_cs6_dev", None)
                self.core.io_break = 1
                return
        is2dg = 0x04680000 <= a < 0x04690000
        if is2dg:
            self._tdg_out()
        try:
            before = len(dev.pending)
            for i in range(n):
                dev.write(a + i, 1, (v >> (8 * i)) & 0xFF)
            if is2dg:
                self._tdg_in()
            if len(dev.pending) != before or (a & 0x1FFF0000) == 0x04080000:
                self.core.io_break = 1
        except Exception as e:
            self._io_err = repr(e)
            self.core.io_break = 1

    def _drain_exc(self):
        c = self.core
        mc = self.machine
        for i in range(c.exc_n):
            e = tuple(c.exc[i])
            if mc is not None:
                mc.excs.append(e[:3])
                mc.exc_total += 1
                if len(mc.excs_first) < 16:
                    mc.excs_first.append(e)
        if mc is not None:
            mc.exc_total += c.exc_lost
        c.exc_n = 0
        c.exc_lost = 0

    # -- Python-core fallback ---------------------------------------------------
    def _py_run(self, limit):
        from .sh4 import SH4
        from .sh4fpu import FPU
        py = SH4(self.mem)
        py.__setstate__(self.__getstate__())
        py.fpu = FPU(py)
        py.mem = self.mem
        mc = self.machine
        user = self.trace
        if mc is not None:
            def tr(pc, op):
                mc._trace(pc, op)
                if user is not None:
                    return user(pc, op)
            py.trace = tr
            py.native_hook = mc._native
            saved = mc.cpu
            mc.cpu = py
            mc._hook_exc()
            self.mem.cpu = py
        else:
            py.trace = user
        try:
            res = py.run(limit=limit)
        finally:
            if mc is not None:
                mc.cpu = saved
                self.mem.cpu = self
            st = py.__getstate__()
            st.pop("fr_raw", None)
            self.__setstate__(st)
            self.core.low_is_ram = 1 if self.mem.low_is_ram else 0
        return res

    # -- run --------------------------------------------------------------------
    def run(self, limit=1000000, max_cycles=None):
        """run up to `limit` steps; `max_cycles` (optional) also stops once
        that much device time has passed -- play mode's sleep/idle jumps
        let one step cover up to 4M cycles, so a step limit alone can be
        0.6 s of emulated time (the app's wall-clock pacing needs ~ms)."""
        if self.trace is not None or self.mmu_on():
            return self._py_run(limit)
        lib, c, mem = self.lib, self.core, self.mem
        dev = mem.dev
        self._bind()
        mc = self.machine
        c.idle_skip = 1 if (mc is None or getattr(mc, "idle_skip", True)) else 0
        c.cs6_py = 1 if getattr(dev, "cs6_py", False) else 0
        de = getattr(dev, "dspemu", None)
        if de is not None and de.clock is None:     # time-lock the real DSP
            de.clock = lambda c=c: c.cycles
        # cs6_fast (XDJ_DSP_CS6C=0 disables): the C core serves HPIA and
        # host HPID reads out of the DSP's own RAM, so the firmware's HPI
        # poll loops stop crossing to Python entirely. This core's mirror
        # becomes the owner of HPIA; every remaining CS6 crossing adopts
        # it into dspdev.hpia first, and dspdev pushes its own mutations
        # (DMAC bulk path) back into the mirror.
        want_fast = (de is not None
                     and os.environ.get("XDJ_DSP_CS6C", "1") != "0")
        if want_fast and not c.cs6_fast:
            lib.core_clear_dsp_regions(c)
            for lo, hi, buf in de.bus.ram:
                arr = (ctypes.c_char * len(buf)).from_buffer(buf)
                self._dsp_keep.append(arr)
                if lib.core_add_dsp_region(c, lo, hi - lo,
                                           ctypes.addressof(arr)) != 0:
                    break
            c.dsp_region_n = min(8, len(de.bus.ram))
            c.cs6_addr = de.hpia
            de._cs6_mirror = c
            c.cs6_fast = 1
        elif not want_fast and c.cs6_fast:
            c.cs6_fast = 0
            if de is not None:
                h = c.cs6_addr               # keep the latch the core owned
                de._cs6_mirror = None
                de.hpia = h
        self._tdg_attach()
        self._dmac_attach()
        self._ev_attach()
        c.low_is_ram = 1 if mem.low_is_ram else 0
        c.fb_writes = 0
        done = u64()
        n = 0
        slice_ = _slice()
        cref = ctypes.byref(c)
        ev = bool(c.ev_on)

        def _tick(cyc, _ev=ev, _mt=mem.tick):
            # ev kwarg not understood by frozen-tick test stubs
            if _ev:
                try:
                    _mt(cyc, ev=True)
                except TypeError:
                    _mt(cyc)
            else:
                _mt(cyc)
        try:
            stats = {} if os.environ.get("XDJ_EV_STATS") else None
            cmax = None if max_cycles is None else dev.cyc + max_cycles
            while n < limit and (cmax is None or dev.cyc < cmax):
                if dev.pending and not (c.sr & BL):
                    got = dev.take_interrupt((c.sr >> 4) & 0xF)
                    if got is not None:
                        lib.core_enter_interrupt(cref, got[0], got[1])
                if c.sleeping:
                    cyc = dev.cycles_to_next_event()
                    jump = min(max(cyc, 100), 4_000_000)
                    if ev:
                        # bound by the next C-side timer event so its IRQ
                        # wakes the machine on time
                        kc = lib.core_ev_next(cref)
                        if kc + 16 < jump:
                            jump = max(100, kc + 16)
                    _tick(jump)
                    n += 1
                    if ev:
                        # the SH clock keeps running while it sleeps: the
                        # DSP is time-locked to c.cycles and must see this
                        # time, or play mode starves it (2026-10-27)
                        c.cycles += jump
                        self._ev_pass(dev.cyc)
                    if stats is not None:
                        stats["sleep"] = stats.get("sleep", 0) + 1
                    continue
                cte = dev.cycles_to_next_event()
                k = min(limit - n, slice_, cte if cte > 0 else 64)
                if ev:
                    kc = lib.core_ev_next(cref)
                    if kc < k:
                        k = max(1, kc)
                k = max(k, 1)
                reason = lib.core_run(cref, k, ctypes.byref(done))
                d = done.value
                if d:
                    _tick(d)
                    n += d
                    if ev:
                        self._ev_pass(dev.cyc)
                    if stats is not None:
                        r = str(reason)
                        stats[r] = stats.get(r, 0) + 1
                        stats["d" + r] = stats.get("d" + r, 0) + d
                        stats["k" + r] = stats.get("k" + r, 0) + k
                        _EV_STATS.update(stats)
                if c.exc_n or c.exc_lost:
                    self._drain_exc()
                if reason == R_IDLE and ev and \
                        (c.pc & 0x1FFFFFFF) != (c.idle_pc & 0x1FFFFFFF):
                    # the pass-end _ev_pass above already ENTERED an
                    # interrupt (pc = vector, SR.BL set). The idle shortcut
                    # must not run now: `c.pc = c.pr` overwrote the vector,
                    # the idle task then ran with interrupts blocked forever
                    # (after a USB insert: mount stuck, 1-2 fps; 11-01).
                    n += 1
                elif reason == R_IDLE:
                    cyc = dev.cycles_to_next_event()
                    if cyc > 0:
                        jump = min(cyc + 16, 4_000_000)
                        if ev:
                            kc = lib.core_ev_next(cref)
                            if kc + 16 < jump:
                                jump = max(16, kc + 16)
                        _tick(jump)
                        if mc is not None:
                            mc.idle_cycles += cyc
                        if ev:
                            c.cycles += jump    # SH clock runs while idle
                    c.pc = c.pr
                    n += 1
                    if ev:
                        self._ev_pass(dev.cyc)
                    if stats is not None:
                        stats["idle"] = stats.get("idle", 0) + 1
                elif reason == R_FALLBACK:
                    mem.fb_writes += c.fb_writes
                    c.fb_writes = 0
                    mem.low_is_ram = bool(c.low_is_ram)
                    self._py_run(256)
                    self._bind()
                    n += 256
        finally:
            mem.fb_writes += c.fb_writes
            c.fb_writes = 0
            mem.low_is_ram = bool(c.low_is_ram)
        return f"limit {limit} reached at {c.pc:#010x}"
