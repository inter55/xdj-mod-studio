"""SH7724 USB0 host controller + a virtual USB mass-storage stick.

The SH7724's USB block is Renesas' R8A66597-family controller (the same
register set as Linux drivers/usb/host/r8a66597-hcd.c). The firmware
configures USB0 as host (SYSCFG 0x04E1: USBE, DRPD, DCFM, HSE, SCKE) and
drives it by interrupt (INTENB1 BCHGE|SIGNE|SACKE, INTENB0 BRDYE|NRDYE|
BEMPE), exactly as the manual describes: on BCHG it samples SYSSTS.LNST,
pulses DVSTCTR.USBRST, sets UACT and polls DVSTCTR.RHST for the speed.

Modelled:
  - attach (BCHG/ATTCH + J-state), bus reset, full-speed handshake
  - DCP control transfers: SUREQ -> SACK, IN data/status via BRDY,
    OUT data/status via BEMP
  - pipes 1-9 bulk IN/OUT through CFIFO/D0FIFO/D1FIFO (PIO or DMAC)
  - W0C status registers, per-pipe BRDY/NRDY/BEMP, level IRQ USI0

The stick is a Bulk-Only-Transport SCSI disk (INQUIRY, READ CAPACITY,
READ(10), WRITE(10) into an overlay, ...), backed by emu/usbdisk.py.

Register offsets are relative to 0x04D80000 (USB0, P2 0xA4D80000).
"""
import os
import struct

BASE = 0x04D80000
SPAN = 0x200

SYSCFG, SYSSTS, DVSTCTR = 0x00, 0x04, 0x08
CFIFOSEL, CFIFOCTR = 0x20, 0x22
D0FIFOSEL, D0FIFOCTR, D1FIFOSEL, D1FIFOCTR = 0x28, 0x2A, 0x2C, 0x2E
INTENB0, INTENB1 = 0x30, 0x32
BRDYENB, NRDYENB, BEMPENB = 0x36, 0x38, 0x3A
INTSTS0, INTSTS1 = 0x40, 0x42
BRDYSTS, NRDYSTS, BEMPSTS = 0x46, 0x48, 0x4A
FRMNUM = 0x4C
USBREQ, USBVAL, USBINDX, USBLENG = 0x54, 0x56, 0x58, 0x5A
DCPCFG, DCPMAXP, DCPCTR = 0x5C, 0x5E, 0x60
PIPESEL, PIPECFG, PIPEBUF, PIPEMAXP, PIPEPERI = 0x64, 0x68, 0x6A, 0x6C, 0x6E

W0C = (INTSTS0, INTSTS1, BRDYSTS, NRDYSTS, BEMPSTS)
# FIFO data ports: offset range -> the select register that governs it
PORTS = ((0x14, 0x18, CFIFOSEL), (0x18, 0x1C, D0FIFOSEL), (0x1C, 0x20, D1FIFOSEL),
         (0x100, 0x120, D0FIFOSEL), (0x120, 0x140, D1FIFOSEL))
SEL_CTR = {CFIFOSEL: CFIFOCTR, D0FIFOSEL: D0FIFOCTR, D1FIFOSEL: D1FIFOCTR}
CTR_SEL = {v: k for k, v in SEL_CTR.items()}

PID_NAK, PID_BUF, PID_STALL = 0, 1, 2
DELAY = 3000            # CPU cycles from "go" to the packet completing (~6 us)
NAK_RETRY = 25_000      # IN token re-poll after a NAK (~50 us)
# XDJ_USB_HS=1: the stick enumerates HIGH-SPEED (512-byte bulk packets,
# device qualifier). Tried 2026-11-01 against E-8302: no change, and the
# mount took ~1.7x longer, so full speed (the old model) stays default.
HIGH_SPEED = os.environ.get("XDJ_USB_HS", "0") == "1"   # off: measured slower
BULK_MPS = 512 if HIGH_SPEED else 64


def _pctr(n):
    return 0x70 + 2 * (n - 1)


class USBHost:
    def __init__(self, dev, disk=None, irq="USI0"):
        self.dev = dev
        self.irq = irq
        self.r = {}                         # 16-bit registers, keyed by offset
        self.pipecfg = {n: {PIPECFG: 0, PIPEBUF: 0, PIPEMAXP: 0, PIPEPERI: 0}
                        for n in range(1, 10)}
        self.buf = {n: bytearray() for n in range(10)}   # rx data / tx staging
        self.off = {n: 0 for n in range(10)}           # consumed prefix (no pop(0):
                                                       # that memmoves the whole
                                                       # packet per byte = O(n^2))
        self.valid = {n: False for n in range(10)}       # buffer holds a packet
        self.tre = {n: [0, 0] for n in range(1, 6)}      # TRE, TRN
        self.trcnt = {n: 0 for n in range(1, 6)}
        self.due = None                     # cycle at which _service runs
        self.stick = MassStorage(disk) if disk is not None else None
        self.attached = False
        self.reset_done = False
        self.ctl = None                     # control transfer in progress
        self.dma_armed = set()              # (DMAC base, channel) on a FIFO port
        self.log = []

    # -- plumbing -------------------------------------------------------------
    @staticmethod
    def owns(addr):
        return BASE <= (addr & 0x1FFFFFFF) < BASE + SPAN

    def _kick(self, delay=DELAY):
        t = self.dev.cyc + delay
        if self.due is None or t < self.due:
            self.due = t

    def tick(self):
        if self.stick is not None and not self.attached and \
                self.r.get(INTENB1, 0) & 0x4000 and self.r.get(SYSCFG, 0) & 1:
            self.plug()
        # USBRST auto-clears on hardware (~1 ms); without this the
        # firmware's port reset never completes and it posts
        # "USB Error. Remove the device." with nothing attached.
        if self.r.get(DVSTCTR, 0) & 0x40 and not getattr(
                self, "_rst_pending", False):
            self._rst_pending = True
            self._rst_due = self.dev.cyc + 500_000
        if getattr(self, "_rst_pending", False) and \
                self.dev.cyc >= self._rst_due:
            self._rst_pending = False
            self.r[DVSTCTR] = self.r.get(DVSTCTR, 0) & ~0x40
        if self.due is not None and self.dev.cyc >= self.due:
            self.due = None
            self._service()
        self._irq()

    def cycles_to_next(self):
        return None if self.due is None else max(1, self.due - self.dev.cyc)

    def _irq(self):
        s0 = self._intsts0()
        on = (s0 & self.r.get(INTENB0, 0) & 0xFF00) or \
             (self.r.get(INTSTS1, 0) & self.r.get(INTENB1, 0))
        if on:
            self.dev.raise_irq(self.irq)
        else:
            self.dev.pending.discard(self.irq)

    def _set(self, reg, bits):
        self.r[reg] = self.r.get(reg, 0) | bits

    # -- attach -----------------------------------------------------------------
    def plug(self):
        """Insert the stick: the port sees a full-speed J state."""
        self.attached = True
        self.reset_done = False
        self._set(INTSTS1, 0x4000 | 0x0800)     # BCHG | ATTCH
        self.log.append("attach")
        self._irq()

    def unplug(self):
        self.attached = False
        self.reset_done = False
        self._set(INTSTS1, 0x4000 | 0x1000)     # BCHG | DTCH
        self.log.append("detach")
        self._irq()

    # -- register file ------------------------------------------------------------
    def _intsts0(self):
        v = self.r.get(INTSTS0, 0) & 0xF800
        if self.r.get(BRDYSTS, 0) & self.r.get(BRDYENB, 0):
            v |= 0x0100
        if self.r.get(NRDYSTS, 0) & self.r.get(NRDYENB, 0):
            v |= 0x0200
        if self.r.get(BEMPSTS, 0) & self.r.get(BEMPENB, 0):
            v |= 0x0400
        return v | 0x0080                        # VBSTS: VBUS present

    def _rd16(self, o):
        if o == SYSSTS:
            return (self.r.get(o, 0) & ~3) | (1 if self.attached else 0)
        if o == DVSTCTR:
            v = self.r.get(o, 0) & ~7
            if v & 0x40:
                return v | 4                     # reset in progress
            if self.attached and v & 0x10:
                self.reset_done = True
                return v | (3 if HIGH_SPEED else 2)   # RHST: HS / FS
            return v
        if o == INTSTS0:
            return self._intsts0()
        if o == FRMNUM:
            return (self.dev.cyc // 500_000) & 0x7FF   # 1 ms frames @500 MHz
        if o in SEL_CTR.values():
            sel = self.r.get(CTR_SEL[o], 0)
            p = sel & 0xF
            dtln = (len(self.buf[p]) - self.off[p]) & 0xFFF
            return 0x2000 | dtln                 # FRDY
        if o == DCPCTR:
            return self._ctr_read(0, self.r.get(o, 0))
        if 0x70 <= o < 0x82:
            n = (o - 0x70) // 2 + 1
            return self._ctr_read(n, self.r.get(o, 0))
        if PIPECFG <= o <= PIPEPERI:
            p = self.r.get(PIPESEL, 0) & 0xF
            if 1 <= p <= 9:
                return self.pipecfg[p][o]
            return 0
        if 0x90 <= o < 0xA4:
            n, k = (o - 0x90) // 4 + 1, ((o - 0x90) // 2) & 1
            return self.trcnt[n] if k else self.tre[n][0]
        return self.r.get(o, 0)

    def _ctr_read(self, n, v):
        v &= ~(0x8000 | 0x4000 | 0x0020 | 0x0040 | 0x4000)
        if self._dir_out(n):
            if not self.valid[n]:
                v |= 0x8000                      # BSTS: writable
            else:
                v |= 0x4000 if n else 0          # INBUFM
        elif self.valid[n]:
            v |= 0x8000                          # BSTS: data to read
        if getattr(self, "_sq", {}).get(n):
            v |= 0x0040
        return v

    def _dir_out(self, n):
        if n == 0:
            return bool(self.r.get(DCPCFG, 0) & 0x10)
        return bool(self.pipecfg[n][PIPECFG] & 0x10)

    def _wr16(self, o, val, mask):
        old = self.r.get(o, 0)
        if o in W0C:
            self.r[o] = old & (val | ~mask) & 0xFFFF
            return
        new = (old & ~mask) | (val & mask)
        if o in SEL_CTR.values():
            p = self.r.get(CTR_SEL[o], 0) & 0xF
            if val & mask & 0x4000:              # BCLR
                self.buf[p] = bytearray()
                self.off[p] = 0
                self.valid[p] = False
                self._kick()
            if val & mask & 0x8000:              # BVAL: short/zero packet
                self.valid[p] = True
                self._kick()
            return
        if PIPECFG <= o <= PIPEPERI:
            p = self.r.get(PIPESEL, 0) & 0xF
            if 1 <= p <= 9:
                c = self.pipecfg[p]
                c[o] = (c[o] & ~mask) | (val & mask)
            return
        if 0x90 <= o < 0xA4:
            n, k = (o - 0x90) // 4 + 1, ((o - 0x90) // 2) & 1
            if k:
                self.tre[n][1] = (self.tre[n][1] & ~mask) | (val & mask)
            else:
                if val & mask & 0x0100:          # TRCLR
                    self.trcnt[n] = 0
                self.tre[n][0] = new & ~0x0100
            return
        if o == DCPCTR or 0x70 <= o < 0x82:
            n = 0 if o == DCPCTR else (o - 0x70) // 2 + 1
            sq = self.__dict__.setdefault("_sq", {})
            if val & mask & 0x0100:              # SQCLR
                sq[n] = 0
            if val & mask & 0x0080:              # SQSET
                sq[n] = 1
            if n and val & mask & 0x0200:        # ACLRM
                self.buf[n] = bytearray()
                self.off[n] = 0
                self.valid[n] = False
            if n == 0 and val & mask & 0x4000:   # SUREQ
                self._setup_pending = True
                self._kick()
            if n == 0 and val & mask & 0x0800:   # SUREQCLR
                self._setup_pending = False
            self.r[o] = new & ~(0x4000 | 0x0800 | 0x0100 | 0x0080 | 0x0200)
            if (new & 3) == PID_BUF:
                if n and self.valid[n] and not self._dir_out(n):
                    # Armed for IN while a packet already sits unread (we
                    # fetch eagerly; on hardware this packet -- the BOT CSW
                    # -- arrives AFTER the arm): report it as ready again.
                    # The firmware had cleared BRDY for it and then waited
                    # its 8.4 s BOT timeout (2026-11-01).
                    self._set(BRDYSTS, 1 << n)
                self._kick()
            return
        if o == CFIFOSEL or o == D0FIFOSEL or o == D1FIFOSEL:
            self.r[o] = new
            if self.dma_armed and new & 0x1000:
                self.dev.usb_dma_pump()
            return
        self.r[o] = new

    # -- CPU-visible access (any size, any alignment) --------------------------------
    def _port(self, o):
        for lo, hi, sel in PORTS:
            if lo <= o < hi:
                return sel
        return None

    def read(self, addr, size):
        o = (addr & 0x1FFFFFFF) - BASE
        v = 0
        for i in range(size):
            sel = self._port(o + i)
            if sel is not None:
                b = self._fifo_pop(sel)
            else:
                w = self._rd16((o + i) & ~1)
                b = (w >> (8 * ((o + i) & 1))) & 0xFF
            v |= b << (8 * i)
        self._irq()
        return v

    def write(self, addr, size, val):
        o = (addr & 0x1FFFFFFF) - BASE
        if self._port(o) is not None:
            for i in range(size):
                self._fifo_push(self._port(o + i) or self._port(o), (val >> (8 * i)) & 0xFF)
            self._irq()
            return
        i = 0
        while i < size:
            a = o + i
            if a & 1 or size - i == 1:
                sh = 8 * (a & 1)
                self._wr16(a & ~1, ((val >> (8 * i)) & 0xFF) << sh, 0xFF << sh)
                i += 1
            else:
                self._wr16(a, (val >> (8 * i)) & 0xFFFF, 0xFFFF)
                i += 2
        self._irq()

    # -- FIFOs -----------------------------------------------------------------------
    def _mxps(self, n):
        if n == 0:
            return (self.r.get(DCPMAXP, 0) & 0x7F) or 64
        return (self.pipecfg[n][PIPEMAXP] & 0x7FF) or 64

    def _fifo_pop(self, sel):
        p = self.r.get(sel, 0) & 0xF
        b = self.buf[p]
        o = self.off[p]
        if o >= len(b):
            return 0
        v = b[o]
        o += 1
        if o >= len(b):
            self.buf[p] = bytearray()
            self.off[p] = 0
            self.valid[p] = False
            self._kick()                         # fetch the next packet
        else:
            self.off[p] = o
        return v

    def _fifo_push(self, sel, byte):
        p = self.r.get(sel, 0) & 0xF
        if self.valid[p]:
            return                               # buffer busy: drop (HW would too)
        o = self.off[p]
        if o:
            del self.buf[p][:o]
            self.off[p] = 0
        self.buf[p].append(byte)
        if len(self.buf[p]) >= self._mxps(p):
            self.valid[p] = True                 # full packet: auto-send
            self._kick()

    # DMAC side (DREQ-paced; sh7724.usb_dma_pump drives these)
    def _dma_pipe(self, addr):
        """(select register, pipe) -- pipe None while DREQE is off: the
        DMAC only moves data once the FIFO port requests it."""
        sel = self._port((addr & 0x1FFFFFFF) - BASE) or D0FIFOSEL
        v = self.r.get(sel, 0)
        # pipe 0 (DCP) cannot be served by the D0/D1 DMA FIFOs
        return sel, (v & 0xF) if v & 0x1000 and v & 0xF else None

    def dma_take(self, src, n):
        """Up to n bytes of the received packet waiting in the FIFO."""
        _, p = self._dma_pipe(src)
        if p is None or not self.valid[p] or self._dir_out(p):
            return b""
        o = self.off[p]
        take = bytes(self.buf[p][o:o + n])
        o += len(take)
        if o >= len(self.buf[p]):
            self.buf[p] = bytearray()
            self.off[p] = 0
            self.valid[p] = False
            self._kick()                         # fetch the next packet
        else:
            self.off[p] = o
        return take

    def dma_room(self, dst, n):
        _, p = self._dma_pipe(dst)
        if p is None or self.valid[p]:
            return 0
        return min(n, self._mxps(p) - (len(self.buf[p]) - self.off[p]))

    def dma_give(self, dst, data):
        sel, _ = self._dma_pipe(dst)
        for b in data:
            self._fifo_push(sel, b)

    # -- the wire ------------------------------------------------------------------------
    def _service(self):
        """Run every transaction the controller would start right now."""
        if getattr(self, "_setup_pending", False):
            self._setup_pending = False
            self._do_setup()
        for n in range(10):
            ctr = self.r.get(DCPCTR if n == 0 else _pctr(n), 0)
            if (ctr & 3) != PID_BUF:
                continue
            if self._dir_out(n):
                if self.valid[n]:
                    self._send(n)
            elif not self.valid[n]:
                self._receive(n)
        if self.dma_armed:
            self.dev.usb_dma_pump()
        self._irq()

    def _do_setup(self):
        req = self.r.get(USBREQ, 0)
        setup = struct.pack("<BBHHH", req & 0xFF, req >> 8, self.r.get(USBVAL, 0),
                            self.r.get(USBINDX, 0), self.r.get(USBLENG, 0))
        if not self.attached or not self.reset_done or self.stick is None:
            self._set(INTSTS1, 0x0020)           # SIGN: no answer
            return
        data = self.stick.control(setup)
        self.log.append(("setup", setup.hex(), None if data is None else len(data)))
        if data is None:                         # STALL
            self.r[DCPCTR] = (self.r.get(DCPCTR, 0) & ~3) | PID_STALL
            self._set(INTSTS1, 0x0010)           # SACK (the stall shows in data stage)
            self.ctl = {"in": b"", "stall": True, "zlp": False}
            return
        self.ctl = {"in": bytes(data), "stall": False, "zlp": False,
                    "out": bytearray(), "setup": setup}
        self._set(INTSTS1, 0x0010)               # SACK

    def _receive(self, n):
        if n == 0:
            c = self.ctl
            if c is None:
                return
            if c.get("stall"):
                self.r[DCPCTR] = (self.r.get(DCPCTR, 0) & ~3) | PID_STALL
                self._set(NRDYSTS, 1)
                return
            mx = self._mxps(0)
            if c["in"]:
                pkt, c["in"] = c["in"][:mx], c["in"][mx:]
            elif not c["zlp"]:
                pkt, c["zlp"] = b"", True        # status stage (or short end)
            else:
                return
            self.buf[0] = bytearray(pkt)
            self.valid[0] = True
            self._set(BRDYSTS, 1)
            return
        if not self.attached or self.stick is None:
            return
        t = self.tre.get(n)
        if t and t[0] & 0x0200 and self.trcnt[n] >= t[1]:
            return                               # transaction counter done
        ep = self.pipecfg[n][PIPECFG] & 0xF
        pkt = self.stick.bulk_in(ep, self._mxps(n))
        if pkt is None:
            # NAK. A real controller keeps re-sending IN tokens while the
            # pipe is BUF; without a scheduled retry nothing kicked this
            # pipe again and the firmware sat out its 8.4 s BOT timeout
            # (4 per mount of the user's library = 33 of 34 s; 2026-11-01)
            self._kick(NAK_RETRY)
            return
        if pkt == "stall":
            self.r[_pctr(n)] = (self.r.get(_pctr(n), 0) & ~3) | PID_STALL
            self._set(NRDYSTS, 1 << n)
            return
        if t:
            self.trcnt[n] += 1
        self.buf[n] = bytearray(pkt)
        self.valid[n] = True
        self._set(BRDYSTS, 1 << n)

    def _send(self, n):
        o = self.off[n]
        data = bytes(self.buf[n][o:])
        self.buf[n] = bytearray()
        self.off[n] = 0
        self.valid[n] = False
        if n == 0:
            c = self.ctl
            if c is not None and data:
                c["out"] += data
                if len(c["out"]) >= struct.unpack("<H", c["setup"][6:8])[0]:
                    self.stick.control_out(c["setup"], bytes(c["out"]))
                    c["in"], c["zlp"] = b"", False
        elif self.attached and self.stick is not None:
            ep = self.pipecfg[n][PIPECFG] & 0xF
            self.stick.bulk_out(ep, data)
            self._kick()                         # an IN pipe may now have data
        self._set(BEMPSTS, 1 << n)
        self._set(BRDYSTS, 1 << n)


# -- the stick ---------------------------------------------------------------------
def _sdesc(s):
    u = s.encode("utf-16-le")
    return bytes([2 + len(u), 3]) + u


class MassStorage:
    """USB flash drive: Bulk-Only Transport, SCSI transparent command set."""
    EP_IN, EP_OUT = 1, 2

    DEVICE = struct.pack("<BBHBBBBHHHBBBB", 18, 1, 0x0200, 0, 0, 0, 64,
                         0x0781, 0x5567, 0x0100, 1, 2, 3, 1)
    CONFIG = (struct.pack("<BBHBBBBB", 9, 2, 32, 1, 1, 0, 0x80, 50) +
              struct.pack("<BBBBBBBBB", 9, 4, 0, 0, 2, 8, 6, 0x50, 0) +
              struct.pack("<BBBBHB", 7, 5, 0x80 | EP_IN, 2, BULK_MPS, 0) +
              struct.pack("<BBBBHB", 7, 5, EP_OUT, 2, BULK_MPS, 0))
    QUALIFIER = struct.pack("<BBHBBBBBB", 10, 6, 0x0200, 0, 0, 0, 64, 1, 0)
    STRINGS = {0: b"\x04\x03\x09\x04", 1: _sdesc("XDJ Emu"),
               2: _sdesc("Virtual USB Stick"), 3: _sdesc("0123456789AB")}

    def __init__(self, disk):
        self.disk = disk
        self.addr = 0
        self.config = 0
        self.cbw = bytearray()
        self.tx = bytearray()          # data-in bytes pending
        self.csw = None
        self.rx_need = 0               # WRITE(10) bytes still expected
        self.rx = bytearray()
        self.rx_cmd = None
        self.sense = (0, 0, 0)
        self.halted = set()
        self.log = []

    # control endpoint --------------------------------------------------------------
    def control(self, setup):
        rt, rq, val, idx, ln = struct.unpack("<BBHHH", setup)
        self.log.append(("ctl", rt, rq, val, idx, ln))
        if rt == 0x80 and rq == 6:                         # GET_DESCRIPTOR
            kind, i = val >> 8, val & 0xFF
            d = {1: self.DEVICE, 2: self.CONFIG}.get(kind)
            if kind == 3:
                d = self.STRINGS.get(i)
            if kind == 6:                                  # device qualifier
                return self.QUALIFIER[:ln] if HIGH_SPEED else None
            return None if d is None else d[:ln]
        if rt == 0x00 and rq == 5:
            self.addr = val & 0x7F
            return b""
        if rt == 0x00 and rq == 9:
            self.config = val & 0xFF
            return b""
        if rt == 0x80 and rq == 8:
            return bytes([self.config])[:ln]
        if rq == 0 and rt & 0x80:                          # GET_STATUS
            return b"\0\0"[:ln]
        if rt == 0x02 and rq == 1:                         # CLEAR_FEATURE(HALT)
            self.halted.discard(idx & 0x0F)
            return b""
        if rt == 0x01 and rq == 11:                        # SET_INTERFACE
            return b""
        if rt == 0xA1 and rq == 0xFE:                      # GET_MAX_LUN
            return b"\0"[:ln]
        if rt == 0x21 and rq == 0xFF:                      # BOT reset
            self.cbw, self.tx, self.csw, self.rx_need = bytearray(), bytearray(), None, 0
            return b""
        return None

    def control_out(self, setup, data):
        self.log.append(("ctl-out", setup.hex(), len(data)))

    # bulk --------------------------------------------------------------------------
    def bulk_in(self, ep, mx):
        if ep != self.EP_IN:
            return None
        if ep in self.halted:
            return "stall"
        if self.tx:
            pkt = bytes(self.tx[:mx])
            del self.tx[:mx]
            return pkt
        if self.csw is not None:
            c, self.csw = self.csw, None
            return c
        return None

    def bulk_out(self, ep, data):
        if ep != self.EP_OUT:
            return
        if self.rx_need:
            self.rx += data
            self.rx_need -= len(data)
            if self.rx_need <= 0:
                lba = self.rx_cmd
                try:
                    self.disk.write(lba, bytes(self.rx))
                except Exception as e:
                    self.log.append(("disk-exc", "write", lba,
                                     len(self.rx), repr(e)[:160]))
                    self.sense = (4, 0x44, 0)
                    self._status(1, self.dlen)
                    self.rx = bytearray()
                    return
                self.rx = bytearray()
                self._status(0)
            return
        self.cbw += data
        if len(self.cbw) < 31:
            return
        cbw, self.cbw = bytes(self.cbw[:31]), bytearray()
        sig, tag, dlen, flags, lun, cblen = struct.unpack("<IIIBBB", cbw[:15])
        if sig != 0x43425355:
            return
        self.tag, self.dlen = tag, dlen
        self._scsi(cbw[15:15 + cblen], dlen, flags)

    def _status(self, st, residue=0):
        self.csw = struct.pack("<IIIB", 0x53425355, self.tag, residue, st)
        if st:
            self.log.append(("status-FAIL", st, residue, self.sense))

    def _data(self, b, dlen):
        b = bytes(b)[:dlen]
        self.tx += b
        if dlen - len(b):
            self.log.append(("short", len(b), dlen))
        self._status(0, dlen - len(b))

    def _scsi(self, cb, dlen, flags):
        op = cb[0]
        d = self.disk
        self.log.append(("scsi", hex(op), dlen))
        if op == 0x00:                                     # TEST UNIT READY
            self._status(0)
        elif op == 0x03:                                   # REQUEST SENSE
            k, asc, ascq = self.sense
            self.sense = (0, 0, 0)
            self._data(bytes([0x70, 0, k, 0, 0, 0, 0, 10, 0, 0, 0, 0, asc, ascq, 0, 0, 0, 0]), dlen)
        elif op == 0x12:                                   # INQUIRY
            if cb[1] & 1:
                self._data(b"\0\0\0\0", dlen)
            else:
                self._data(bytes([0, 0x80, 4, 2, 31, 0, 0, 0]) + b"XDJ Emu " +
                           b"Virtual Stick   " + b"1.00", dlen)
        elif op == 0x25:                                   # READ CAPACITY(10)
            self._data(struct.pack(">II", d.sectors - 1, 512), dlen)
        elif op == 0x23:                                   # READ FORMAT CAPACITIES
            self._data(bytes([0, 0, 0, 8]) + struct.pack(">I", d.sectors) +
                       bytes([2, 0, 2, 0]), dlen)
        elif op == 0x1A:                                   # MODE SENSE(6)
            self._data(bytes([3, 0, 0, 0]), dlen)
        elif op == 0x5A:                                   # MODE SENSE(10)
            self._data(bytes([0, 6, 0, 0, 0, 0, 0, 0]), dlen)
        elif op in (0x1E, 0x35, 0x1B, 0x2F):               # PREVENT/SYNC/START/VERIFY
            self._status(0)
        elif op == 0x28:                                   # READ(10)
            lba, cnt = struct.unpack(">I", cb[2:6])[0], struct.unpack(">H", cb[7:9])[0]
            if lba + cnt > d.sectors:
                self.sense = (5, 0x21, 0)
                self._status(1, dlen)
            else:
                try:
                    self._data(d.read(lba, cnt), dlen)
                except Exception as e:
                    self.log.append(("disk-exc", "read", lba, cnt, repr(e)[:160]))
                    self.sense = (4, 0x44, 0)
                    self._status(1, dlen)
        elif op == 0x2A:                                   # WRITE(10)
            lba, cnt = struct.unpack(">I", cb[2:6])[0], struct.unpack(">H", cb[7:9])[0]
            self.rx_cmd, self.rx_need, self.rx = lba, cnt * 512, bytearray()
            if not self.rx_need:
                self._status(0)
        else:
            self.sense = (5, 0x20, 0)                      # invalid opcode
            if dlen and flags & 0x80:
                self.tx += bytes(0)
            self._status(1, dlen)
