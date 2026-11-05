"""SH7724 on-chip peripheral model. Register addresses from
docs/sh7724_registers.json (1195 entries); bit layouts from R01UH0174EJ
Sections 13 (INTC), 20 (TMU), 22 (CMT), 26 (SCIF).

Strategy: model what the boot/RTOS needs functionally (timers tick and
interrupt, serial RX injects / TX logs, DMAC copies instantly, LCDC
captures the framebuffer base); everything else is stub storage that
logs its first touch. Unknown reads return 0 -- the standard bring-up
assumption, and every such read is logged so a wrong assumption shows
up in the trace instead of silently hanging.
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    _REGNAMES = {int(v): k for k, v in
                 json.load(open(os.path.join(ROOT, "docs", "sh7724_registers.json"))).items()
                 if isinstance(v, int)}
except OSError:
    _REGNAMES = {}

PCLK = 50_000_000   # P-clock assumption (documented guess; timers scale linearly)
RCLK = 32768        # RCLK assumption for CMT
# Board input pins that idle HIGH on the real deck (pull-ups). The generic
# stub reads back 0 for never-written port data registers, which the app
# takes as an asserted active-low signal.
#   PHDR (0xA405012E) bit1 = USB-A power-switch overcurrent flag, active
#   low: polled by 0x8D56038; when 0 it posts msg 0x92 "USB Error. Remove
#   the device." and the browser shows NO USB (found 2026-10-04).
#   PHDR bit4 = USB port state: sampled with a 3-count debounce; while it
#   reads low the USB stack deletes the drive right after adding it, so
#   the stick never lists (the "second insert" quirk of CLAUDE.md §32;
#   documented by cdj-nxs2-qemu boards/xdj1000/pfc.c).
PIN_HIGH = {0x0405012E: 0x12}

CPUFREQ = 500_000_000  # I-clock assumption: device cycles = cpu_cyc * PCLK/CPUFREQ

# interrupt sources: code, IPR(addr, shift), IMR(reg, bit)
SOURCES = {
    "TUNI0_0": (0x400, 0xA4080000, 12, 4, 4),
    "TUNI0_1": (0x420, 0xA4080000, 8, 4, 5),
    "TUNI0_2": (0x440, 0xA4080000, 4, 4, 6),
    "TUNI1_0": (0x920, 0xA4080008, 12, 0, 4),
    "TUNI1_1": (0x940, 0xA4080008, 8, 0, 5),
    "TUNI1_2": (0x960, 0xA4080008, 4, 0, 6),
    "CMTI":    (0xF00, 0xA4080014, 0, 9, 4),
    "SCIF0":   (0xC00, 0xA4080018, 12, 5, 0),
    "SCIF1":   (0xC20, 0xA4080018, 8, 5, 1),
    "USI0":    (0xA20, 0xA4080014, 4, 9, 1),
    "USI1":    (0xA40, 0xA4080014, 4, 9, 2),
    "LCDCI":   (0xF40, 0xA4080004, 8, 4, 0),   # IPRB[11:8] (manual table 13.x)
    "DEI0A":   (0x800, 0xA4080010, 12, 1, 0),   # DMAC0 ch0 (legacy name)
    "DEI1":    (0x820, 0xA4080010, 12, 1, 1),
    "DEI2":    (0x840, 0xA4080010, 12, 1, 2),
    "DEI3":    (0x860, 0xA4080010, 12, 1, 3),
    "DEI4":    (0xB80, 0xA4080014, 8, 5, 4),
    "DEI5":    (0xBA0, 0xA4080014, 8, 5, 5),
    # 2DG: all four sources share IPRI[3:0]; IMR11 bits 4..7 = TRI INI CEI BRK
    "TDG_TRI": (0x780, 0xA4080020, 0, 11, 4),
    "TDG_INI": (0x7A0, 0xA4080020, 0, 11, 5),
    # IIC0: priority IPRH[3:0] (firmware writes 4 there); IMR7 bits 7..4 =
    # DTEI WAITI TACKI ALI (Linux setup-sh7724.c mask table)
    "IIC0_ALI":   (0xE00, 0xA408001C, 0, 7, 4),
    "IIC0_TACKI": (0xE20, 0xA408001C, 0, 7, 5),
    "IIC0_WAITI": (0xE40, 0xA408001C, 0, 7, 6),
    "IIC0_DTEI":  (0xE60, 0xA408001C, 0, 7, 7),
}


class SH7724:
    def __init__(self, mem, log=None):
        self.mem = mem          # back-pointer for DMAC copies
        self.log = log          # callable(str) or None
        self.reg = {}           # addr -> int (stub storage)
        self.touched = set()
        self.tx_log = [[], [], []]  # SCIF0/1/2 transmitted bytes
        self.rx = [[], [], []]      # SCIF0/1/2 receive queues (bytes)
        self.pending = set()    # interrupt source names with flag set
        self.cyc = 0
        self._evc = None        # the C core while it owns TMU/LCDC (play)
        # TMU: unit -> ch -> {tcor, tcnt, tcr, acc}
        self.tmu = {}
        for base in (0xFFD80000, 0xFFD90000):
            self.tmu[base] = {0: self._new_tmu_ch(), 1: self._new_tmu_ch(),
                              2: self._new_tmu_ch()}
        self.tstr = {0xFFD80004: 0, 0xFFD90004: 0}
        # CMT
        self.cmstr = 0
        self.cmcsr = 0
        self.cmcnt = 0
        self.cmcor = 0xFFFFFFFF
        self.cm_acc = 0
        # DMAC channels: (base) -> ch -> dict (DMAC0 x2 views + DMAC1)
        self.dma = {}
        for base in (0xFE008000, 0xFDC08000, 0xFE009000):
            self.dma[base] = {"dmaor": 0,
                              "ch": {i: {"sar": 0, "dar": 0, "tcr": 0, "chcr": 0}
                                     for i in range(12)}}
        self.fb_base = None     # LCDC-captured framebuffer base
        self.imr = {i: 0xFF for i in range(13)}  # IMR0-12 reset masked (1=masked)
        # Behavioral DSP model (emu/hledsp.py): only when XDJ_HLE_DSP=1.
        # Default off: None, and every hook below is a single None-check,
        # so stock behavior is byte-identical.
        self.hle = None
        # REAL DSP co-simulation (emu/c6x/dspdev.py): Pioneer's DSP code on
        # the C674x core, wired through CS6/HPI + GPIO. Opt-in.
        self.dspemu = None
        if os.environ.get("XDJ_DSP_EMU") == "1":
            from .c6x.dspdev import DspDevice
            self.dspemu = DspDevice(self)
            self.cs6_py = True
        if os.environ.get("XDJ_HLE_DSP") == "1" and self.dspemu is None:
            from .hledsp import HleDsp
            self.hle = HleDsp(self)
        self._imr_addr = {i: 0xA4080080 + i * 4 for i in range(13)}
        import collections as _c
        self.dma_log = _c.deque(maxlen=32)  # recent transfers (not saved)
        self.dma_over = []  # truncated/unmapped transfers (not saved)
        # CS6 DSP-bridge (phys 0x18000000, P2 0xB8xxxxxx): control/status,
        # DSP-address port, bulk FIFO (DMA target), single-word data port.
        self.cs6 = {"addr": 0, "ctrl": 0, "bulk": 0, "run": 0}

    @staticmethod
    def _new_tmu_ch():
        return {"tcor": 0xFFFFFFFF, "tcnt": 0xFFFFFFFF, "tcr": 0, "acc": 0}

    # -- helpers ----------------------------------------------------------
    def _scif_ct(self, ch=None):
        """rx byte counts into the C core's SCIF status mirror (no-op
        unless the play-mode event loop owns it)."""
        evc = getattr(self, "_evc", None)
        if evc is None:
            return
        for k in range(3):
            n = len(self.rx[k])
            evc.scif_rx[k] = n if n < 16 else 16

    def _touch(self, op, addr, val=None):
        if addr in self.touched:
            return
        self.touched.add(addr)
        if self.log is not None:
            name = _REGNAMES.get(addr & 0xFFFFFFFF, "")
            extra = f" ({name})" if name else ""
            if val is None:
                self.log(f"dev {op} {addr:#010x}{extra}")
            else:
                self.log(f"dev {op} {addr:#010x}={val:#x}{extra}")

    def _imr_masked(self, reg, bit):
        return bool(self.imr.get(reg, 0xFF) & (1 << bit))

    def _level(self, name):
        code, ipr, shift, imr, bit = SOURCES[name]
        # reg[] is keyed by post-translate address (P2 stripped), while the
        # table holds virtual P2 addresses: normalize before lookup. IPRs
        # are 16-bit (byte-split stores), so assemble both bytes first.
        if 0xA0000000 <= ipr < 0xC0000000:
            ipr &= 0x1FFFFFFF
        evc = self.__dict__.get("_evc")
        if evc is not None and 0x04080000 <= ipr < 0x04080027:
            # play-mode C event loop owns the IPR bytes: reg[] is stale
            # until _ev_out. Reading reg[] here gave USI0 level 0, so the
            # USB interrupt was never delivered: the mount stalled and
            # every pass shrank to 64 cycles (1-2 fps) -- 2026-11-01.
            o = ipr - 0x04080000
            v = evc.intc_ipr[o] | (evc.intc_ipr[o + 1] << 8)
            return (v >> shift) & 0xF
        v = self.reg.get(ipr, 0) | (self.reg.get(ipr + 1, 0) << 8)
        return (v >> shift) & 0xF

    def raise_irq(self, name):
        self.pending.add(name)

    def take_interrupt(self, imask=-1):
        best = None
        for name in self.pending:
            code, ipr, shift, imr, bit = SOURCES[name]
            if self._imr_masked(imr, bit):
                continue
            lvl = self._level(name)
            if lvl == 0 or lvl <= imask:
                continue
            if best is None or lvl > best[0]:
                best = (lvl, code, name)
        if best is None:
            return None
        self.pending.discard(best[2])
        return best[0], best[1]

    # -- 2DG ------------------------------------------------------------------
    def _tdg(self):
        t = getattr(self, "tdg", None)
        if t is None:
            from .tdg import TDG
            t = self.tdg = TDG(self.mem, self.raise_irq, self.log)
            # adopt whatever the stub storage already captured
            for off in range(0, 0x100, 4):
                v = 0
                for i in range(4):
                    v |= self.reg.pop(0x04680000 + off + i, 0) << (8 * i)
                if v and off != 0:
                    t.reg[off] = v
        return t

    # -- USB0 host controller -------------------------------------------------
    def _usb(self):
        u = getattr(self, "usb", None)
        if u is None:
            from .usbhost import USBHost, BASE
            disk = getattr(self, "usb_disk", None)
            if disk is None and os.environ.get("XDJ_USB"):
                from .usbdisk import open_disk
                disk = open_disk(os.environ["XDJ_USB"])
            u = self.usb = USBHost(self, disk)
            # adopt whatever the stub storage already captured
            for off in range(0, 0x200, 2):
                v = self.reg.pop(BASE + off, 0) | (self.reg.pop(BASE + off + 1, 0) << 8)
                if v:
                    u.r[off] = v
        return u

    def _iic(self):
        i = getattr(self, "iic", None)
        if i is None:
            from .iic import IIC
            i = self.iic = IIC(self)
        return i

    # -- register access ----------------------------------------------------
    def read(self, addr, size):
        a = addr & 0xFFFFFFFF
        if 0x04680000 <= (a & 0x1FFFFFFF) < 0x04690000:
            return self._tdg().read(a & 0xFFFF, size)
        if 0x04D80000 <= (a & 0x1FFFFFFF) < 0x04D80200 and \
                not os.environ.get("XDJ_NOUSB"):
            return self._usb().read(a, size)
        if 0x04470000 <= (a & 0x1FFFFFFF) < 0x04470020:
            return self._iic().read(a, size)
        # SCIF status/data
        for ch, base in ((0, 0xFFE00000), (1, 0xFFE10000), (2, 0xFFE20000)):
            if base <= a < base + 0x28:
                return self._scif_read(ch, a - base, size)
        # TMU
        for base in (0xFFD80000, 0xFFD90000):
            if base <= a < base + 0x30:
                return self._tmu_read(base, a - base, size)
        # CMT
        if 0xA44A0000 <= a < 0xA44A0070:
            return self._cmt_read(a, size)
        # INTC IMR/IMCR (8-bit). Firmware reaches these via P2 (0xA40800xx,
        # stripped by translate to 0x040800xx), so accept both views.
        if 0xA4080080 <= a < 0xA40800B4 and size == 1:
            return self.imr[(a - 0xA4080080) // 4]
        if 0x04080080 <= a < 0x040800B4 and size == 1:
            return self.imr[(a - 0x04080080) // 4]
        for base in (0xFE008000, 0xFDC08000, 0xFE009000):
            if base <= a < base + 0x200:
                return self._dma_read(base, a - base, size)
        if 0x18000000 <= a < 0x18100000:  # CS6 DSP bridge
            return self._cs6_read(a, size)
        if 0xFE940460 <= a < 0xFE940464:
            # MLDPMR (LCD power mgmt): firmware polls bits[1:0]==3 after
            # enabling power. Stored power bits are kept; ready bits read 1
            # (the power sequence "completes instantly" in emulation).
            v = 0
            for i in range(size):
                rb = self.reg.get(a + i, 0) | ((0x3 >> ((a + i - 0xFE940460) * 8)) & 0xFF)
                v |= (rb & 0xFF) << (8 * i)
            return v
        # generic stub: byte storage, assembled LE (stores arrive byte-split)
        v = 0
        for i in range(size):
            b = self.reg.get(a + i, 0)
            pin = PIN_HIGH.get((a + i) & 0x1FFFFFFF)
            if pin:
                b |= pin
            de = getattr(self, "dspemu", None)
            if de is not None and (a + i) & 0x1FFFFFFF == 0x0405016C:
                b = (b & ~1) | de.busy_pin()       # DSP GP4[14] -> SH PTx bit0
            v |= b << (8 * i)
            if a + i not in self.touched:
                self.touched.add(a + i)
                if self.log is not None:
                    name = _REGNAMES.get((a + i) & 0xFFFFFFFF, "")
                    self.log(f"dev rd {(a+i):#010x}" + (f" ({name})" if name else ""))
        return v

    def write(self, addr, size, val):
        a = addr & 0xFFFFFFFF
        val &= (1 << (size * 8)) - 1
        if 0x04680000 <= (a & 0x1FFFFFFF) < 0x04690000:
            self._tdg().write(a & 0xFFFF, size, val)
            return
        if 0x04D80000 <= (a & 0x1FFFFFFF) < 0x04D80200 and \
                not os.environ.get("XDJ_NOUSB"):
            self._usb().write(a, size, val)
            return
        if 0x04470000 <= (a & 0x1FFFFFFF) < 0x04470020:
            self._iic().write(a, size, val)
            return
        for ch, base in ((0, 0xFFE00000), (1, 0xFFE10000), (2, 0xFFE20000)):
            if base <= a < base + 0x28:
                self._scif_write(ch, a - base, size, val)
                return
        for base in (0xFFD80000, 0xFFD90000):
            if base <= a < base + 0x30:
                self._tmu_write(base, a - base, size, val)
                return
        if (a & 0x1FFFFFFF) == 0x04600000:
            # EDMR (Ethernet E-DMAC mode): bit0 SWR = software reset, which
            # the hardware clears itself once the reset completes. The
            # firmware writes 1 and polls for 0 (task 64 at 0x8C52EAA).
            val &= ~1
        if 0xA44A0000 <= a < 0xA44A0070:
            self._cmt_write(a, size, val)
            return
        if 0xA4080080 <= a < 0xA40800B4 and size == 1:
            self.imr[(a - 0xA4080080) // 4] = val
            return
        if 0x04080080 <= a < 0x040800B4 and size == 1:
            self.imr[(a - 0x04080080) // 4] = val
            return
        if 0xA40800C0 <= a < 0xA40800F4 and size == 1:
            # IMCRn: write 1 clears mask bit
            self.imr[(a - 0xA40800C0) // 4] &= ~val
            return
        if 0x040800C0 <= a < 0x040800F4 and size == 1:
            self.imr[(a - 0x040800C0) // 4] &= ~val
            return
        # DMAC: channels 0-3 @+0x20, DMAOR @+0x60, channels 4+ @+0x70
        for base in (0xFE008000, 0xFDC08000, 0xFE009000):
            if base <= a < base + 0x200:
                self._dma_write(base, a - base, size, val)
                return
        if 0x18000000 <= a < 0x18100000:  # CS6 DSP bridge
            self._cs6_write(a, size, val)
            return
        # LCDC capture: any 32-bit write of a framebuffer-ish base
        if 0xFE940000 <= a < 0xFE960000 and size == 4:
            if val in (0x0E780000, 0x0E83B800):
                self.fb_base = val
                if self.log is not None:
                    self.log(f"LCDC framebuffer base {val:#010x} @ {a:#010x}")
            self._touch("wr", a, val)
            self.reg[a] = val
            return
        de = getattr(self, "dspemu", None)
        if de is not None and (a & 0x1FFFFFFF) == 0x04050128:
            # DSP command code = pins 0x128.7 + 0x164.1, always written in
            # that order (helper 0x8C37472): let the DSP see the complete
            # old code once, but never the half-written pair in between
            from .c6x.dspdev import SYNC_CYCLES
            de.sync(SYNC_CYCLES)
        for i in range(size):
            self.reg[a + i] = (val >> (8 * i)) & 0xFF
            if a + i not in self.touched:
                self.touched.add(a + i)
                if self.log is not None:
                    name = _REGNAMES.get((a + i) & 0xFFFFFFFF, "")
                    self.log(f"dev wr {(a+i):#010x}" + (f" ({name})" if name else ""))

    # -- TMU ------------------------------------------------------------------
    def _tmu_ch(self, base, off):
        ch = (off - 0x08) // 12
        kind = (off - 0x08) % 12
        return ch, kind  # kind 0..3=tcor, 4..7=tcnt, 8..9=tcr

    def _tmu_read(self, base, off, size):
        if off == 0x04:
            return self.tstr[base + 0x04] if base + 0x04 in self.tstr else 0
        ch, kind = self._tmu_ch(base, off)
        st = self.tmu[base][ch]
        if kind <= 3:
            return st["tcor"] & ((1 << (size * 8)) - 1)
        if kind <= 7:
            return st["tcnt"] & ((1 << (size * 8)) - 1)
        v = st["tcr"]
        if size == 1 and (off & 1):     # little-endian: odd byte = high half
            v >>= 8
        return v & ((1 << (size * 8)) - 1)

    def _tmu_write(self, base, off, size, val):
        if off == 0x04:
            self.tstr[base + 0x04] = val & 0xFF
            # starting a channel loads TCOR->TCNT (hardware behavior)
            for ch in range(3):
                if val & (1 << ch):
                    self.tmu[base][ch]["tcnt"] = self.tmu[base][ch]["tcor"]
            return
        ch, kind = self._tmu_ch(base, off)
        st = self.tmu[base][ch]
        if kind <= 3:       # TCOR byte `kind`
            sh = kind * 8
            m = ((1 << (size * 8)) - 1) << sh
            st["tcor"] = (st["tcor"] & ~m) | ((val << sh) & m)
        elif kind <= 7:     # TCNT byte `kind-4`
            sh = (kind - 4) * 8
            m = ((1 << (size * 8)) - 1) << sh
            st["tcnt"] = (st["tcnt"] & ~m) | ((val << sh) & m)
        else:
            if size == 2:
                # UNF is R/(W): writing 0 clears, writing 1 ignored
                old = st["tcr"]
                st["tcr"] = (val & ~0x0100) | (old & 0x0100 & val)
                st["tcr"] &= 0xFFFF
            else:
                # byte access: read-modify-write the addressed half.
                # Little-endian bus: the EVEN address is the LOW byte.
                # (Was big-endian here, which moved UNIE (bit 5) to bit 13
                # so timer interrupts were never raised.)
                cur = st["tcr"]
                if off & 1:
                    cur = (cur & 0x00FF) | ((val & 0xFF) << 8)
                else:
                    cur = (cur & 0xFF00) | (val & 0xFF)
                st["tcr"] = cur & 0xFFFF

    def _tmu_tick(self, base, cyc):
        tstr = self.tstr.get(base + 0x04, 0)
        unit = 0 if base == 0xFFD80000 else 1
        for ch in range(3):
            if not (tstr & (1 << ch)):
                continue
            st = self.tmu[base][ch]
            div = {0: 4, 1: 16, 2: 64, 3: 256, 4: 1024}.get(st["tcr"] & 7, 4)
            st["acc"] += cyc * PCLK // CPUFREQ
            n = st["acc"] // div
            if n == 0:
                continue
            st["acc"] -= n * div
            t = st["tcnt"]
            if n <= t:
                st["tcnt"] = t - n
            else:
                # underflow: t->0 takes t ticks, one more wraps and reloads
                period = (st["tcor"] + 1) or 0x100000000
                rem = (n - t - 1) % period
                st["tcnt"] = (st["tcor"] - rem) & 0xFFFFFFFF
                st["tcr"] |= 0x0100
                if st["tcr"] & 0x20:
                    self.raise_irq(f"TUNI{unit}_{ch}")

    # -- CMT --------------------------------------------------------------------
    def _cmt_read(self, a, size):
        off = a - 0xA44A0000
        if off == 0x00:
            return self.cmstr & 0xFFFF
        if off == 0x60:
            return self.cmcsr & 0xFFFF
        if off == 0x64:
            return self.cmcnt & 0xFFFFFFFF
        if off == 0x68:
            return self.cmcor & 0xFFFFFFFF
        return 0

    def _cmt_write(self, a, size, val):
        off = a - 0xA44A0000
        sh = (a & 3) * 8 if size < 4 else 0
        if size == 4:
            sh = 0

        def rmw(cur, bits):
            m = ((1 << (size * 8)) - 1) << sh
            return (cur & ~m) | ((val << sh) & m) & ((1 << bits) - 1)

        if off in (0x00, 0x01):
            self.cmstr = rmw(self.cmstr, 16)
        elif off in (0x60, 0x61):
            # CMF/OVF (bits 15:14): write 0 clears, write 1 ignored.
            # Other bits: plain RMW.
            for i in range(size):
                o = off + i
                b = (val >> (8 * i)) & 0xFF
                if o == 0x60:
                    self.cmcsr = (self.cmcsr & ~0xFF) | (b & 0x3F)
                elif o == 0x61:
                    sticky = (self.cmcsr >> 8) & 0xC0
                    written = b & 0xC0
                    nb = (sticky & written) | (b & 0x3F)
                    self.cmcsr = (self.cmcsr & ~0xFF00) | (nb << 8)
            self.cmcsr &= 0xFFFF
        elif off in (0x64, 0x65, 0x66, 0x67):
            self.cmcnt = rmw(self.cmcnt, 32)
        elif off in (0x68, 0x69, 0x6A, 0x6B):
            self.cmcor = rmw(self.cmcor, 32)

    def _cmt_tick(self, cyc):
        if not (self.cmstr & 1):
            return
        div = {0: 8, 1: 32, 2: 128, 3: 512}.get(self.cmcsr & 7, 8)
        self.cm_acc += cyc * PCLK // CPUFREQ
        n = self.cm_acc // div
        if n == 0:
            return
        self.cm_acc -= n * div
        wide = not (self.cmcsr & 0x0200)  # CMS=0 -> 32-bit
        mod = 0x100000000 if wide else 0x10000
        t = self.cmcnt + n
        if t >= self.cmcor:
            self.cmcsr |= 0x8000
            if (self.cmcsr >> 4) & 3:  # CMR != 0 -> interrupt
                self.raise_irq("CMTI")
            if self.cmcsr & 0x0100:  # CMM free-run
                self.cmcnt = t % mod
            else:
                self.cmcnt = 0
        else:
            self.cmcnt = t % mod

    # -- SCIF ---------------------------------------------------------------------
    def _scif_read(self, ch, off, size):
        if off == 0x10:  # SCFSR
            v = 0x60  # TEND+TDFE always (TX drains instantly)
            if self.rx[ch]:
                v |= 0x03  # RDF+DR
            return v
        if off == 0x14:  # SCFRDR
            if self.rx[ch]:
                b = self.rx[ch].pop(0)
                self._scif_ct()
                return b
            return 0
        if off == 0x1C:  # SCFDR: R count hi, T count lo
            return (min(len(self.rx[ch]), 16) << 8) & 0xFF00
        v = 0
        for i in range(size):
            v |= self.reg.get((0xFFE00000, 0xFFE10000, 0xFFE20000)[ch] + off + i, 0) << (8 * i)
        return v

    def _scif_write(self, ch, off, size, val):
        base = (0xFFE00000, 0xFFE10000, 0xFFE20000)[ch]
        if off == 0x0C:  # SCFTDR: transmit
            self.tx_log[ch].append(val & 0xFF)
            return
        if off == 0x18 and (val & 0x06):  # SCFCR FIFO resets
            pass
        self.reg[base + off] = val

    # -- DMAC -----------------------------------------------------------------------
    # channels 0-3 @ +0x20, DMAOR @ +0x60, channels 4-11 @ +0x70.
    # NOTE: the CPU may program these with byte accesses (our memory layer
    # splits every store into bytes), so every access is a shifted RMW.
    def _dma_ch(self, off):
        if 0x20 <= off < 0x60:
            return (off - 0x20) // 16, off & 0xC, off & 0x3
        if 0x70 <= off < 0xF0:
            return (off - 0x70) // 16 + 4, off & 0xC, off & 0x3
        return None, None, None

    def _dma_read(self, base, off, size):
        if off == 0x60:  # DMAOR
            return self.dma[base]["dmaor"] & ((1 << (size * 8)) - 1)
        ch, reg, bo = self._dma_ch(off)
        if ch is None:
            return self.reg.get(base + off, 0) & ((1 << (size * 8)) - 1)
        st = self.dma[base]["ch"][ch]
        v = {"sar": st["sar"], "dar": st["dar"], "tcr": st["tcr"],
             "chcr": st["chcr"]}[["sar", "dar", "tcr", "chcr"][reg // 4]]
        return (v >> (bo * 8)) & ((1 << (size * 8)) - 1)

    def _dma_write(self, base, off, size, val):
        if 0x60 <= off <= 0x63:  # DMAOR, 16-bit (byte-split stores RMW)
            sh = (off - 0x60) * 8
            m = ((1 << (size * 8)) - 1) << sh
            dma = self.dma[base]
            dma["dmaor"] = ((dma["dmaor"] & ~m) | ((val << sh) & m)) & 0xFFFF
            return
        ch, reg, bo = self._dma_ch(off)
        if ch is None:
            self.reg[base + off] = val
            return
        st = self.dma[base]["ch"][ch]
        key = ["sar", "dar", "tcr", "chcr"][reg // 4]
        mask = ((1 << (size * 8)) - 1) << (bo * 8)
        st[key] = (st[key] & ~mask) | ((val << (bo * 8)) & mask)
        # trigger on final CHCR state (writes arrive byte-split from mem layer)
        if reg == 0xC:
            chcr = st["chcr"]
            if chcr & 0x01 and not (chcr & 0x02):  # DE set, TE clear -> run
                if self.dma[base]["dmaor"] & 0x01:
                    self._dma_run(base, ch)
                elif self.log is not None:
                    self.log(f"DMAC ch{ch} DE without DME; TE forced")
                    st["chcr"] |= 0x02

    def _dma_run(self, base, ch):
        st = self.dma[base]["ch"][ch]
        chcr = st["chcr"]
        # SH7724 manual §16 (CHCR): TS[3:0] = bits 21,20 + bits 4,3.
        # 0000:1B 0001:2B 0010:4B 0011:16B 0100:32B 0111:8B
        # 1011:16B(8x2 div) 1100:32B(16x2 div). SM=bits13:12, DM=bits15:14
        # (00/11=fixed, 01=increment, 10=decrement). Firmware-proven:
        # 0x5438->16B copy, 0x4430->4B fixed-source fill (BSS clear).
        ts4 = (((chcr >> 20) & 3) << 2) | ((chcr >> 3) & 3)
        unit = {0: 1, 1: 2, 2: 4, 3: 16, 4: 32, 7: 8,
                0xB: 16, 0xC: 32}.get(ts4)
        if unit is None:
            raise ValueError(f"DMAC ch{ch} prohibited TS={ts4:#06b} "
                             f"(CHCR={chcr:#010x})")
        sm, dm = (chcr >> 12) & 3, (chcr >> 14) & 3
        n = (st["tcr"] & 0xFFFFFF) * unit
        src, dst = st["sar"] & 0xFFFFFFFF, st["dar"] & 0xFFFFFFFF
        # SAR/DAR hold P1/P2 views; the bus is physical. Normalize first so
        # CS6 views (e.g. P2 0xB8080000 = phys 0x18080000, used by the audio
        # feeder's DMAC ch3/ch5) hit the bridge check below instead of
        # falling through to truncate (which stalled playback forever).
        if 0x80000000 <= dst < 0xC0000000:
            dst &= 0x1FFFFFFF
        if 0x18080000 <= dst < 0x18080100:
            # CS6 DSP-bridge bulk FIFO: the audio DSP consumes whatever the
            # firmware streams (MP3 bytes, PCM, command packets). No DSP is
            # modelled, so accept and count the bytes, complete the transfer
            # fully (TCR=0, TE) exactly like hardware would once the FIFO
            # drains. Without this the transfer truncates with TCR left
            # over and the feeder waits forever (track loads, PLAY engages,
            # but time stays --:-- and no further DMA ever starts).
            if 0x80000000 <= src < 0xC0000000:
                src &= 0x1FFFFFFF
            mem = self.mem            # (was NameError: mem undefined here)
            sb, so, _ = mem._loc(src)
            if sb is not None:
                seg = min(len(sb) - so, n)
                self.cs6["bulk"] = self.cs6.get("bulk", 0) + seg
                # HPI auto-increment semantics: the bytes land in DSP
                # memory at the live address latch, which advances.
                self._dsp_bulk(bytes(sb[so:so + seg]))
            st["tcr"] = 0
            st["chcr"] |= 0x02  # TE
            cpu = getattr(self.mem, "cpu", None)
            self.dma_log.append((base, ch, src, dst, n,
                                 cpu.pc if cpu else 0))
            if st["chcr"] & 0x04:  # IE
                self.raise_irq(self._dei(ch))
            return
        if src == self.SCIF2_RDR:
            self._scif2_service()      # completes now if bytes are queued,
            return                     # else stays armed until a TX clocks them in
        if dst == self.SCIF2_TDR:
            s0 = src & 0x1FFFFFFF if 0x80000000 <= src < 0xC0000000 else src
            self.tx_log[2].extend(self.mem.r8(s0 + i) for i in range(n))
            del self.tx_log[2][:-4096]
            st["chcr"] |= 0x02
            if st["chcr"] & 0x04:
                self.raise_irq(self._dei(ch))
            self._panel_reply(n)
            self._scif2_service()
            return
        # SAR/DAR hold P1/P2 views; the bus is physical
        if 0x80000000 <= src < 0xC0000000:
            src &= 0x1FFFFFFF
        if 0x80000000 <= dst < 0xC0000000:
            dst &= 0x1FFFFFFF
        from .usbhost import USBHost
        if (USBHost.owns(src) or USBHost.owns(dst)) and \
                not os.environ.get("XDJ_NOUSB"):
            # DREQ-paced: stays armed and moves data as packets arrive
            # (bulk IN) or as the FIFO frees up (bulk OUT)
            self._usb().dma_armed.add((base, ch))
            self.usb_dma_pump()
            return
        if n >= 1048576 and self.mem is not None and \
                getattr(self.mem, "watch_dma", False):
            cpu = getattr(self.mem, "cpu", None)
            self.mem.dma_hit = (base, ch, src, dst, n,
                                cpu.pc if cpu else 0,
                                cpu.pr if cpu else 0,
                                cpu.gr(14) if cpu else 0,
                                cpu.gr(7) if cpu else 0,
                                cpu.gr(15) if cpu else 0)
        if self.log is not None:
            self.log(f"DMAC ch{ch} copy {n} B ({st['tcr'] & 0xFFFFFF}x{unit}) "
                     f"{src:#010x} -> {dst:#010x}")
        # region-aware transfer (byte loop would take minutes for 100MB+).
        # fixed source (SM=0/3, e.g. CHCR 0x4430) = fill: repeat source unit.
        mem = self.mem
        left = n
        s, d = src, dst
        fill = None
        if sm in (0, 3):
            sb, so, _ = mem._loc(s)
            fill = bytes(sb[so:so + unit]) if sb is not None else bytes(unit)
        s_step = 0 if sm in (0, 3) else (unit if sm == 1 else -unit)
        d_step = 0 if dm in (0, 3) else (unit if dm == 1 else -unit)
        trunc = None
        while left > 0:
            if fill is not None:
                db, do, dfb = mem._loc(d)
                if db is None:
                    trunc = d
                    break  # unmapped; stop (matches old fault-and-stop)
                seg = min(len(db) - do if d_step >= 0 else do + 1, left)
                if seg <= 0:
                    trunc = d
                    break
                if db is not mem.flash:
                    # tile the source unit across the segment
                    reps, rem = divmod(seg, unit)
                    db[do:do + seg] = fill * reps + fill[:rem]
                if dfb:
                    mem.fb_writes += seg
                    if getattr(mem, "fb_hook", None) is not None:
                        mem.fb_hook(d, d + seg)
                d += seg if d_step >= 0 else -seg
                left -= seg
                continue
            sb, so, _ = mem._loc(s if s_step >= 0 else s - unit + 1)
            db, do, dfb = mem._loc(d if d_step >= 0 else d - unit + 1)
            if sb is None or db is None:
                trunc = s if sb is None else d
                break  # unmapped; stop (matches old fault-and-stop)
            if s_step >= 0:
                seg = min(len(sb) - so, len(db) - do, left)
            else:
                seg = min(so + 1, len(db) - do, left)
                if d_step < 0:
                    seg = min(seg, do + 1)
            if seg <= 0:
                trunc = s if s_step < 0 else d
                break
            if db is not mem.flash:
                if s_step >= 0 and d_step >= 0:
                    db[do:do + seg] = sb[so:so + seg]
                else:
                    # rare direction mode: correct-but-slow unit loop
                    for k in range(0, seg, unit):
                        u = bytes(sb[so + k:so + k + unit])
                        db[do + k:do + k + unit] = u
                    s, d = s + s_step * (seg // unit), d + d_step * (seg // unit)
                    left -= seg
                    continue
            if dfb:
                mem.fb_writes += seg
                if getattr(mem, "fb_hook", None) is not None:
                    mem.fb_hook(d, d + seg)
            s += seg
            d += seg
            left -= seg
        if trunc is not None:
            self.dma_over.append((base, ch, src, dst, n, trunc, left))
        st["chcr"] |= 0x02  # TE
        cpu = getattr(self.mem, "cpu", None)
        self.dma_log.append((base, ch, src, dst, n,
                             cpu.pc if cpu else 0))
        if st["chcr"] & 0x04:  # IE
            self.raise_irq(self._dei(ch))

    def usb_dma_pump(self):
        """Advance every DMAC channel armed on a USB FIFO port."""
        usb = self._usb()
        # Fast path (XDJ_USB_FAST=1): bulk data phases as whole runs.
        # Handles eligible channels, leaves the rest armed for slow below.
        try:
            from emu.usb_fast import is_fast, fast_pump
            if is_fast():
                try:
                    fast_pump(self, usb)
                except Exception:
                    pass
        except ImportError:
            pass
        for base, ch in list(usb.dma_armed):
            st = self.dma[base]["ch"][ch]
            chcr = st["chcr"]
            if not chcr & 1 or chcr & 2:
                usb.dma_armed.discard((base, ch))
                continue
            ts4 = (((chcr >> 20) & 3) << 2) | ((chcr >> 3) & 3)
            unit = {0: 1, 1: 2, 2: 4, 3: 16, 4: 32, 7: 8, 0xB: 16, 0xC: 32}[ts4]
            sm, dm = (chcr >> 12) & 3, (chcr >> 14) & 3
            src, dst = st["sar"] & 0x1FFFFFFF, st["dar"] & 0x1FFFFFFF
            n = (st["tcr"] & 0xFFFFFF) * unit
            if usb.owns(src):
                data = usb.dma_take(src, n)
                if dm == 1:
                    if not self.mem.w_block(dst, data):
                        for i, b in enumerate(data):
                            self.mem.w8(dst + i, b)
                else:
                    for b in data:
                        self.mem.w8(dst, b)
                moved = len(data)
                if dm == 1:
                    st["dar"] = (st["dar"] + moved) & 0xFFFFFFFF
            else:
                room = usb.dma_room(dst, n)
                if sm == 1:
                    blk = self.mem.r_block(src, room)
                    data = blk if blk is not None else bytes(
                        self.mem.r8(src + i) for i in range(room))
                else:
                    one = self.mem.r_block(src, 1) if room else b"\x00"
                    data = (bytes(one) * room if one is not None else bytes(
                        self.mem.r8(src) for _ in range(room)))
                usb.dma_give(dst, data)
                moved = len(data)
                if sm == 1:
                    st["sar"] = (st["sar"] + moved) & 0xFFFFFFFF
            if moved:
                left = n - moved
                st["tcr"] = -(-left // unit) if left > 0 else 0
            if st["tcr"] & 0xFFFFFF == 0:
                usb.dma_armed.discard((base, ch))
                st["chcr"] |= 0x02
                if st["chcr"] & 0x04:
                    self.raise_irq(self._dei(ch))

    @staticmethod
    def _dei(ch):
        return ("DEI0A", "DEI1", "DEI2", "DEI3", "DEI4", "DEI5")[ch % 6]

    # -- panel MCU link: SCIF2, clocked, both directions DMA-driven ---------
    # PnlCom arms one DMAC channel SCFRDR2 -> RAM (32 B) and another
    # RAM -> SCFTDR2 (32 B). In clocked mode every byte the main CPU
    # shifts out shifts one in, so the panel's frame arrives with ours.
    # The stand-in panel answers every transmitted frame with
    # self.panel_frame (set by the UI; defaults to "nothing pressed").
    SCIF2_RDR = 0xFFE20014
    # real panel cadence: ~1 frame per 1.5 ms of emulated time (500 MHz)
    PANEL_FRAME_CYCLES = 750_000
    SCIF2_TDR = 0xFFE2000C

    def _scif2_rx_pending(self):
        for base, dma in self.dma.items():
            for ch, st in dma["ch"].items():
                if (st["sar"] & 0xFFFFFFFF) == self.SCIF2_RDR and \
                        st["chcr"] & 1 and not st["chcr"] & 2:
                    yield base, ch, st

    def _scif2_service(self):
        q = self.rx[2]
        for base, ch, st in list(self._scif2_rx_pending()):
            n = st["tcr"] & 0xFFFFFF
            if len(q) < n:
                continue
            dst = st["dar"] & 0xFFFFFFFF
            if 0x80000000 <= dst < 0xC0000000:
                dst &= 0x1FFFFFFF
            for i in range(n):
                self.mem.w8(dst + i, q.pop(0))
            st["chcr"] |= 0x02
            self.panel_rx_frames = getattr(self, "panel_rx_frames", 0) + 1
            if st["chcr"] & 0x04:
                self.raise_irq(self._dei(ch))

    def _panel_reply(self, nbytes):
        fr = getattr(self, "panel_frame", None)
        if fr is None:
            from .panl import build_frame
            fr = build_frame()
        fr = bytes(fr)
        # one byte in per byte out; frames are 32 B on both sides.
        # NOTE (2026-10-03): do NOT pace this echo -- the link has tight
        # timing expectations; a 1.5 ms gap stalls the handshake and the
        # boot hangs at panel identification. Measured load at instant
        # echo is ~15-20% CPU (no starvation).
        while nbytes > 0:
            chunk = fr[:nbytes]
            self.rx[2].extend(chunk)
            self._scif_ct()
            nbytes -= len(chunk)

    # -- DSP memory as seen through the HPI (sparse 64 KB pages) ----------
    # Everything the SH writes to the DSP (boot image via bulk DMA, word
    # writes via the data port) lands here, so a DSP emulator can start
    # from the exact memory image the real C674x gets. Read-only to the
    # stock path (nothing reads it back unless a DSP model is attached).
    def _dsp_page(self, a):
        pages = self.__dict__.setdefault("dspmem", {})
        pg = pages.get(a >> 16)
        if pg is None:
            pg = pages[a >> 16] = bytearray(0x10000)
        return pg

    def dsp_write(self, a, data):
        a &= 0xFFFFFFFF
        for i, b in enumerate(data):
            self._dsp_page(a + i)[(a + i) & 0xFFFF] = b

    def _cs6_hpia(self, new=None):
        """Live HPI address latch (the C core holds it while running,
        unless cs6_py routes all CS6 traffic here)."""
        core = getattr(getattr(self.mem, "cpu", None), "core", None)
        if core is not None and core.cs6_py:
            core = None
        if new is None:
            return (core.cs6_addr if core is not None else self.cs6["addr"]) & 0xFFFFFFFF
        new &= 0xFFFFFFFF
        self.cs6["addr"] = new
        if core is not None:
            core.cs6_addr = new

    def _dsp_bulk(self, data):
        if getattr(self, "dspemu", None) is not None:
            self.dspemu.bulk(data)
            return
        a = self._cs6_hpia()
        self.dsp_write(a, data)
        if getattr(self, "cs6_py", False):     # ordered stream for capture
            log = self.__dict__.setdefault("hpi_seq", [])
            if log and log[-1][0] == "B" and log[-1][1] + len(log[-1][2]) == a:
                log[-1] = ("B", log[-1][1], log[-1][2] + bytes(data))
            else:
                log.append(("B", a, bytes(data)))
        self._cs6_hpia(a + len(data))

    # -- CS6 DSP bridge -------------------------------------------------------
    # 0x18000000 ctrl/status (RMW), 0x18040000 DSP-addr latch,
    # 0x18080000 bulk FIFO (write-only, DMA streams the DSP image here),
    # 0x180C0000 data port (single-word access at the latched address).
    # DSP->host ready flag 0x1183FFF4 reads 1; PFC 0xA405016C bit0 reads 0
    # (generic stub default) = ready. Run flag 0x1183FFF0 is accepted.
    def _cs6_read(self, a, size):
        if getattr(self, "dspemu", None) is not None:
            return self.dspemu.read(a, size)
        if self.hle is not None:
            return self.hle.read(a, size)
        off = a - 0x18000000
        if off < 4:
            sh = off * 8
            return (self.cs6["ctrl"] >> sh) & ((1 << (size * 8)) - 1)
        if 0xC0000 <= off < 0xC0004:
            v = 1 if self.cs6["addr"] == 0x1183FFF4 else 0
            return (v >> ((off - 0xC0000) * 8)) & ((1 << (size * 8)) - 1)
        return 0

    def _cs6_write(self, a, size, val):
        if getattr(self, "dspemu", None) is not None:
            self.dspemu.write(a, size, val)
            return
        if self.hle is not None:
            self.hle.write(a, size, val)
            return
        off = a - 0x18000000
        if off < 4:  # control (byte-split stores RMW)
            sh = off * 8
            m = ((1 << (size * 8)) - 1) << sh
            self.cs6["ctrl"] = ((self.cs6["ctrl"] & ~m) |
                                ((val << sh) & m)) & 0xFFFFFFFF
            return
        if 0x40000 <= off < 0x40004:  # address latch (byte-split RMW)
            sh = (off - 0x40000) * 8
            m = ((1 << (size * 8)) - 1) << sh
            self.cs6["addr"] = ((self.cs6["addr"] & ~m) |
                                ((val << sh) & m)) & 0xFFFFFFFF
            return
        if 0x80000 <= off < 0x80100:  # bulk FIFO: accept and count
            self.cs6["bulk"] += size
            self._dsp_bulk(val.to_bytes(size, "little"))
            return
        if 0xC0000 <= off < 0xC0004:  # data port
            a = self.cs6["addr"] + (off - 0xC0000)
            self.dsp_write(a, val.to_bytes(size, "little"))
            if getattr(self, "cs6_py", False):
                self.__dict__.setdefault("hpi_words", []).append((a, size, val))
                self.__dict__.setdefault("hpi_seq", []).append(("W", a, size, val))
            if self.cs6["addr"] == 0x1183FFF0:
                self.cs6["run"] = val & ((1 << (size * 8)) - 1)
            return

    # -- idle fast-forward ------------------------------------------------------------
    def cycles_to_next_event(self):
        """CPU cycles until the next timer underflow / compare match that
        would raise an enabled interrupt (0 if one is already pending).
        TMU/LCDC are excluded while the C core owns them (play mode): the
        C side bounds its own slices via core_ev_next."""
        if self.pending:
            return 0
        evc = getattr(self, "_evc", None)
        best = None
        ratio = CPUFREQ // PCLK
        if evc is None:
            for base, chs in self.tmu.items():
                tstr = self.tstr.get(base + 0x04, 0)
                for ch, st in chs.items():
                    if not (tstr & (1 << ch)) or not (st["tcr"] & 0x20):
                        continue
                    div = {0: 4, 1: 16, 2: 64, 3: 256, 4: 1024}.get(st["tcr"] & 7, 4)
                    pcl = (st["tcnt"] + 1) * div - st["acc"]
                    c = max(1, pcl) * ratio
                    best = c if best is None else min(best, c)
            if self.reg.get(self.LDINTR + 1, 0) & 0x7F:
                c = max(1, self._lcdc_left())
                best = c if best is None else min(best, c)
        if self.cmstr & 1 and (self.cmcsr >> 4) & 3:
            div = {0: 8, 1: 32, 2: 128, 3: 512}.get(self.cmcsr & 7, 8)
            left = (self.cmcor - self.cmcnt) & 0xFFFFFFFF
            c = max(1, left * div - self.cm_acc) * ratio
            best = c if best is None else min(best, c)
        for dv in (getattr(self, "usb", None), getattr(self, "iic", None)):
            c = dv.cycles_to_next() if dv is not None else None
            if c is not None:
                best = c if best is None else min(best, c)
        return best if best is not None else 100_000

    # -- LCDC frame timing ---------------------------------------------------------------
    # LDINTR (0xFE940468): enables in bits 14..8 (SE UE ME MHE FE VSE VEE),
    # matching status flags in bits 6..0; firmware clears a flag by writing
    # 0 to it. Once per 60 Hz frame every enabled event is flagged and
    # LCDCI raised -- the GUI waits on this to know a flip has taken.
    # XDJ_LCD_HZ: LCD frame rate (default the deck's 60; lower rates gave no
    # speed-up in tests -- painting is not paced by frames).
    FRAME_CYC = CPUFREQ // int(os.environ.get("XDJ_LCD_HZ", "60"))
    LDINTR = 0xFE940468

    # The 60 Hz frame count, which the app's wall-clock pacing reads
    # (emu/ui.py _emu_loop). While the play-mode C event loop owns the LCDC
    # (fastcpu._ev_attach) the live count is in the core; the plain value
    # stays in __dict__ so snapshots pickle as before. Until 2026-11-01 the
    # app read a frozen count there and never slept -- harmless below real
    # time, unbounded audio lag above it.
    @property
    def frames(self):
        evc = self.__dict__.get("_evc")
        if evc is not None:
            return evc.lcdc_frames
        return self.__dict__.get("frames", 0)

    @frames.setter
    def frames(self, v):
        self.__dict__["frames"] = v

    def _lcdc_tick(self, cyc):
        acc = getattr(self, "lcdc_acc", 0) + cyc
        if acc < self.FRAME_CYC:
            self.lcdc_acc = acc
            return
        self.lcdc_acc = acc % self.FRAME_CYC
        self.frames = getattr(self, "frames", 0) + 1
        en = self.reg.get(self.LDINTR + 1, 0) & 0x7F
        if en:
            self.reg[self.LDINTR] = (self.reg.get(self.LDINTR, 0) | en) & 0x7F
            self.raise_irq("LCDCI")

    def _lcdc_left(self):
        return self.FRAME_CYC - getattr(self, "lcdc_acc", 0)

    # -- tick --------------------------------------------------------------------------
    def tick(self, cyc, ev=False):
        self.cyc += cyc
        if getattr(self, "dspemu", None) is not None:
            self.dspemu.tick(cyc)
        if self.hle is not None:
            self.hle.tick(cyc)
        if not ev:                # play-mode C event loop owns these counters
            self._lcdc_tick(cyc)
            self._tmu_tick(0xFFD80000, cyc)
            self._tmu_tick(0xFFD90000, cyc)
        # CMT is NOT owned by the C event loop (fastcore's cmt_* slots are
        # unused). Skipping it in play mode froze the CMT the USB stack
        # starts on a stick insert: mount stuck at 0 reads, 1 fps (11-01).
        self._cmt_tick(cyc)
        u = getattr(self, "usb", None)
        if u is not None:
            u.tick()
        i = getattr(self, "iic", None)
        if i is not None:
            i.tick()
