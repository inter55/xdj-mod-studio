"""periph.py -- the DSP-side peripherals Pioneer's audio engine uses
(OMAP-L137 / C6747 family; register layout from TI SPRUFE8B ch.10
(interrupt controller), the EDMA3 and McASP user guides, and what the
XDJ-700 DSP image actually programs (2026-10-12 boot log)):

  INTC   0x01800000  event flags / masks / INTMUX -> CPU INT4..15
  EDMA3  0x01C00000  PaRAM-driven transfers, region-1 completion IRQ
                     (system event 8 = EDMA3_CC0_INT1 -> INT8)
  McASP  0x01D04000 (McASP1, the DAC) and 0x01D08000 (McASP2): when the
         transmitter is out of reset each TDM slot raises its TX DMA event
         (EDMA channel 3 = McASP1 TX, 5 = McASP2 TX); the DMA writes the
         sample to the data port 0x01D06000 / 0x01D0A000, which we keep
         (that is the deck's audio output).

Event numbers (OMAP-L137 datasheet table 5-8): 4 T64P0_TINT12,
8 EDMA3_CC0_INT1, 34 UHPI_DSPINT, 61 MCASP_INT.
"""
import ctypes
import os

MASK = 0xFFFFFFFF
# Edma3.burst's word loop in C (c6xcore.c c6x_dma_words); 0 = Python
_NO_CDMA = os.environ.get("XDJ_DSP_CDMA", "1") == "0"
# Edma3.transfer's ABSYNC frame loop in C (c6xcore.c c6x_dma_frames); 0 = Python
_NO_CFRAME = os.environ.get("XDJ_DSP_CFRAME", "1") == "0"


class Intc:
    """C674x interrupt controller (event combiner + INTMUX)"""
    def __init__(self, dev):
        self.dev = dev
        self.flag = [0, 0, 0, 0]
        self.mask = [0, 0, 0, 0]
        # reset: INT4..15 <- events 4..15
        self.mux = {n: n for n in range(4, 16)}
        self.regs = {}

    def read(self, off, n):
        if off < 0x10:
            return self.flag[off // 4]
        if 0x80 <= off < 0x90:
            return self.mask[(off - 0x80) // 4]
        if 0xA0 <= off < 0xB0:
            g = (off - 0xA0) // 4
            return self.flag[g] & ~self.mask[g] & MASK
        if off in (0x104, 0x108, 0x10C):
            base = 4 + 4 * ((off - 0x104) // 4)
            return sum(self.mux[base + k] << (8 * k) for k in range(4))
        return self.regs.get(off, 0)

    def write(self, off, n, v):
        if 0x20 <= off < 0x30:                       # EVTSET
            g = (off - 0x20) // 4
            for b in range(32):
                if v >> b & 1:
                    self.raise_event(32 * g + b)
            return
        if 0x40 <= off < 0x50:                       # EVTCLR
            self.flag[(off - 0x40) // 4] &= ~v & MASK
            return
        if 0x80 <= off < 0x90:
            self.mask[(off - 0x80) // 4] = v & MASK
            return
        if off in (0x104, 0x108, 0x10C):
            base = 4 + 4 * ((off - 0x104) // 4)
            for k in range(4):
                self.mux[base + k] = (v >> (8 * k)) & 0x7F
            return
        self.regs[off] = v

    def raise_event(self, e):
        g, b = divmod(e, 32)
        if g > 3:
            return
        with self.dev.irq_lock:          # play mode: two threads raise events
            self.flag[g] |= 1 << b
            cpu = self.dev.cpu
            m = 0
            for intn, sel in self.mux.items():
                if sel == e:
                    m |= 1 << intn
            # combined event g (events 0..3) when an unmasked flag is set
            if e >= 4 and not (self.mask[g] >> b & 1):
                for intn, sel in self.mux.items():
                    if sel == g:
                        m |= 1 << intn
            if m:
                if hasattr(cpu, "ifr_or"):
                    cpu.ifr_or(m)            # atomic vs the C core's own updates
                else:
                    cpu.c["ifr"] |= m


class Edma3:
    """EDMA3 channel controller: event-triggered PaRAM transfers (A- and
    AB-synchronized), linking, completion codes -> IPR -> event 8."""
    SHADOW = (0x1000, 0x2000, 0x2200)               # global, region 0, 1

    def __init__(self, dev):
        self.dev = dev
        self.regs = {}
        self.param = bytearray(0x4000)               # 0x4000..0x7FFF
        self.eer = self.ier = self.ipr = self.er = 0
        self.qeer = 0                                # QDMA event enables
        self.stats = {"events": 0, "transfers": 0, "irq": 0, "null": 0, "qdma": 0}

    # ---- register file
    def read(self, off, n):
        if 0x4000 <= off < 0x8000:
            o = off - 0x4000
            return int.from_bytes(self.param[o:o + n], "little")
        for sh in self.SHADOW:
            r = off - sh
            if r in (0x00, 0x20, 0x50, 0x68, 0x84):
                return {0x00: self.er, 0x20: self.eer, 0x50: self.ier,
                        0x68: self.ipr, 0x84: self.qeer}[r]
        return self.regs.get(off, 0)

    def write(self, off, n, v):
        v &= MASK
        if 0x4000 <= off < 0x8000:
            o = off - 0x4000
            self.param[o:o + n] = (v & ((1 << 8 * n) - 1)).to_bytes(n, "little")
            self._qdma_check(o, n)
            return
        for sh in self.SHADOW:
            r = off - sh
            if r == 0x08:   self.er &= ~v; return          # ECR
            if r == 0x10:                                   # ESR: manual trigger
                for ch in range(32):
                    if v >> ch & 1:
                        self.transfer(ch)
                return
            if r == 0x28:   self.eer &= ~v; return         # EECR
            if r == 0x30:   self.eer |= v; return          # EESR
            if r == 0x58:   self.ier &= ~v; return         # IECR
            if r == 0x60:   self.ier |= v; self._irq(); return   # IESR
            if r == 0x70:                                   # ICR (level: re-raise
                self.ipr &= ~v; self._irq(); return        # while IPR&IER)
            if r == 0x88:   self.qeer &= ~v; return        # QEECR
            if r == 0x8C:   self.qeer |= v & 0xFF; return  # QEESR
            if r == 0x78:                                   # IEVAL
                if v & 1:
                    self._irq(force=True)
                return
        self.regs[off] = v

    # ---- QDMA: a write to the trigger word of the PaRAM set a QDMA channel
    # points at (QCHMAPn 0x200+4n: PAENTRY bits 13:5, TRWORD bits 4:2)
    # submits that set at once. Pioneer's DSP moves every 1/75-s audio
    # frame this way (set 40, trigger = CCNT) and polls IPR for TCC 8.
    def _qdma_check(self, o, n):
        if not self.qeer:
            return
        for q in range(8):
            if not (self.qeer >> q & 1):
                continue
            m = self.regs.get(0x200 + 4 * q, 0)
            s, tw = (m >> 5) & 0x1FF, (m >> 2) & 7
            t = 32 * s + 4 * tw
            if o <= t < o + n:
                self.stats["qdma"] += 1
                self.transfer(s, qdma=True)

    # ---- transfers
    def event(self, ch):
        """hardware DMA event on channel ch (e.g. McASP TX)"""
        self.stats["events"] += 1
        if self.eer >> ch & 1:
            self.transfer(ch)
        else:
            self.er |= 1 << ch

    def burst(self, ch, n):
        """n hardware DMA events on channel ch, back to back -- the McASP
        pacing step (dspdev._run) raises BURST of them at once. Same result
        as n x event(ch) (PaRAM, IPR/interrupt, link reloads, the words the
        data port receives); the common case (A-sync 4-byte array from RAM
        to a McASP data port at a fixed address) runs as one tight loop
        instead of n trips through transfer() and the bus (~40% of the
        audio-bound run time, 2026-10-21)."""
        if not (self.eer >> ch & 1):
            self.stats["events"] += n
            self.er |= 1 << ch
            return
        bus = self.dev.bus
        p = self.param
        frm = int.from_bytes
        while n > 0:
            b = 32 * ch
            opt = frm(p[b:b + 4], "little")
            src = frm(p[b + 4:b + 8], "little")
            abc = frm(p[b + 8:b + 12], "little")
            dst = frm(p[b + 12:b + 16], "little")
            bidx = frm(p[b + 16:b + 20], "little")
            lnk = frm(p[b + 20:b + 24], "little")
            cidx = frm(p[b + 24:b + 28], "little")
            ccnt = frm(p[b + 28:b + 32], "little") & 0xFFFF
            acnt, bcnt = abc & 0xFFFF, abc >> 16
            sink = None
            if (acnt == 4 and bcnt and ccnt and not opt & 0xC
                    and not bidx >> 16 and not cidx >> 16):
                dv, _ = bus._dev(dst)
                sink = getattr(dv, "asp", None)
            buf, lo = None, 0
            if sink is not None:
                for rlo, rhi, rb in bus.ram:
                    if rlo <= src < rhi:
                        buf, lo = rb, rlo
                        break
            if buf is None or bus.watch:
                self.stats["events"] += 1   # anything unusual: the general path
                self.transfer(ch)
                n -= 1
                continue
            sbidx = _s16(bidx & 0xFFFF)
            scidx = _s16(cidx & 0xFFFF)
            bcntrld = lnk >> 16
            out = sink.out
            muted = getattr(sink.dev, "muted", False)
            blen = len(buf) - 4
            done = 0
            fast = self._cdma(buf, p) if not _NO_CDMA else None
            if fast is not None:                      # same loop, in C
                cw, bufp, parp = fast
                if len(cw) < n:
                    cw = self._cbuf = (ctypes.c_uint32 * n)()
                    fast = (cw, bufp, parp)
                done = self._clib.c6x_dma_words(bufp, blen, lo, parp + b, n, cw,
                                                1 if muted else 0)
                out.extend(cw[:done])
                ccnt = 0 if not (p[b + 28] | p[b + 29]) else 1
                self.stats["events"] += done
                self.stats["transfers"] += done
                n -= done
                if ccnt == 0:
                    self._complete(ch, opt, lnk & 0xFFFF, False)
                elif done == 0:
                    self.stats["events"] += 1
                    self.transfer(ch)
                    n -= 1
                if len(out) >= sink.keep:
                    sink.trim()
                continue
            while done < n and ccnt:
                o = src - lo
                if o < 0 or o > blen:
                    break
                out.append(0 if muted else frm(buf[o:o + 4], "little"))
                done += 1
                if bcnt > 1:
                    src += sbidx
                    bcnt -= 1
                else:
                    src += scidx
                    bcnt, ccnt = bcntrld, ccnt - 1
            self.stats["events"] += done
            self.stats["transfers"] += done
            n -= done
            self._pw(ch, 1, src)
            self._pw(ch, 2, (bcnt << 16) | acnt)
            self._pw(ch, 7, ccnt)
            if ccnt == 0:                             # transfer complete
                self._complete(ch, opt, lnk & 0xFFFF, False)
            elif done == 0:                           # src left RAM
                self.stats["events"] += 1
                self.transfer(ch)
                n -= 1
            if len(out) >= sink.keep:
                sink.trim()

    def _cdma(self, buf, p):
        """(word buffer, &buf, &param) for c6x_dma_words, or None when the
        C core library is not in use / lacks it. The bus RAM bytearrays
        never resize (the C core shares them); restore() swaps `param`."""
        cache = self.__dict__.setdefault("_cd", {})
        key = (id(buf), id(p))
        e = cache.get(key)
        if e is not None and e[3] is buf and e[4] is p:
            return e[0], e[1], e[2]
        lib = getattr(self.dev.cpu, "lib", None)
        if lib is None or not hasattr(lib, "c6x_dma_words"):
            return None
        f = lib.c6x_dma_words
        P = ctypes.c_void_p
        f.argtypes = [P, ctypes.c_int64, ctypes.c_uint32, P, ctypes.c_int,
                      P, ctypes.c_int]
        f.restype = ctypes.c_int
        self._clib = lib
        bufp = ctypes.addressof((ctypes.c_char * len(buf)).from_buffer(buf))
        parp = ctypes.addressof((ctypes.c_char * len(p)).from_buffer(p))
        cw = getattr(self, "_cbuf", None) or (ctypes.c_uint32 * 64)()
        self._cbuf = cw
        if len(cache) > 16:
            cache.clear()
        cache[key] = (cw, bufp, parp, buf, p)
        return cw, bufp, parp

    def _frames(self, src, dst, acnt, bcnt, sbidx, dbidx):
        """c6x_dma_frames: up to bcnt frames of acnt bytes from src/dst,
        strides sbidx/dbidx, when both ranges sit wholly inside RAM buffers
        (nothing to watch, no device window). Returns frames copied; 0 means
        the Python loop below must do it (same order, same bytes)."""
        if _NO_CFRAME or sbidx < 0 or dbidx < 0:
            return 0
        bus = self.dev.bus
        if bus.watch:
            return 0
        sb = db = None
        slo = dlo = 0
        for rlo, rhi, rb in bus.ram:
            if sb is None and rlo <= src and src + sbidx * (bcnt - 1) + acnt <= rhi:
                sb, slo = rb, rlo
            if db is None and rlo <= dst and dst + dbidx * (bcnt - 1) + acnt <= rhi:
                db, dlo = rb, rlo
        if sb is None or db is None:
            return 0
        lib = getattr(self.dev.cpu, "lib", None)
        if lib is None or not hasattr(lib, "c6x_dma_frames"):
            return 0
        cache = self.__dict__.setdefault("_cf", {})
        key = (id(sb), id(db))
        e = cache.get(key)
        if e is None or e[2] is not sb or e[3] is not db:
            f = lib.c6x_dma_frames
            P = ctypes.c_void_p
            f.argtypes = [P, ctypes.c_int64, ctypes.c_uint32,
                          P, ctypes.c_int64, ctypes.c_uint32,
                          ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                          ctypes.c_uint32, ctypes.c_int, ctypes.c_int]
            f.restype = ctypes.c_int
            sbp = ctypes.addressof((ctypes.c_char * len(sb)).from_buffer(sb))
            dbp = ctypes.addressof((ctypes.c_char * len(db)).from_buffer(db))
            if len(cache) > 8:
                cache.clear()
            e = cache[key] = (sbp, dbp, sb, db, len(sb), len(db))
        return lib.c6x_dma_frames(e[0], e[4], slo, e[1], e[5], dlo, src, dst,
                                  acnt, bcnt, sbidx, dbidx)

    def _p(self, s, w):
        return int.from_bytes(self.param[32 * s + 4 * w:32 * s + 4 * w + 4], "little")

    def _pw(self, s, w, v):
        self.param[32 * s + 4 * w:32 * s + 4 * w + 4] = (v & MASK).to_bytes(4, "little")

    def transfer(self, s, qdma=False):
        bus = self.dev.bus
        opt, src, abc, dst = (self._p(s, k) for k in range(4))
        bidx, lnk, cidx, ccnt = (self._p(s, k) for k in range(4, 8))
        acnt, bcnt = abc & 0xFFFF, abc >> 16
        ccnt &= 0xFFFF
        if acnt == 0 or bcnt == 0 or ccnt == 0:
            self.stats["null"] += 1                  # null / exhausted set
            return
        sbidx = _s16(bidx & 0xFFFF)
        dbidx = _s16(bidx >> 16)
        scidx = _s16(cidx & 0xFFFF)
        dcidx = _s16(cidx >> 16)
        bcntrld = lnk >> 16
        link = lnk & 0xFFFF
        absync = opt >> 2 & 1
        self.stats["transfers"] += 1
        if not absync:                               # one array per event
            bus.write(dst, 4, bus.read(src, 4)) if acnt == 4 else \
                self._copy(src, dst, acnt)
            if bcnt > 1:
                src, dst, bcnt = src + sbidx, dst + dbidx, bcnt - 1
            else:
                src, dst = src + scidx, dst + dcidx
                bcnt, ccnt = bcntrld, ccnt - 1
        else:                                        # one frame per event
            s0, d0 = src, dst
            if sbidx == dbidx == acnt and acnt % 4 == 0:
                # contiguous frame (Pioneer's 588-word audio blocks): one
                # copy; _copy falls back to word order if the ranges overlap
                self._copy(src, dst, acnt * bcnt)
                bcnt = 0
            elif acnt == 4 and bcnt:
                # MP3 audio path: 588 words, source stride 4, destination
                # stride 8 (interleaved into the stereo buffer). The frame
                # loop in C -- in Python it was one _copy call per frame,
                # and it runs on the DSP thread (measured 2026-11-01).
                k = self._frames(src, dst, acnt, bcnt, sbidx, dbidx)
                if k:
                    src += sbidx * k
                    dst += dbidx * k
                    bcnt -= k
            for _ in range(bcnt):
                self._copy(src, dst, acnt)
                src, dst = src + sbidx, dst + dbidx
            src, dst, ccnt = s0 + scidx, d0 + dcidx, ccnt - 1
        static = opt >> 3 & 1
        if static:                                   # STATIC: set never updated
            if opt >> 20 & 1 and (ccnt == 0 or qdma):
                self.ipr |= 1 << (opt >> 12 & 0x3F)
                self._irq()
            return
        self._pw(s, 1, src)
        self._pw(s, 3, dst)
        self._pw(s, 2, (bcnt << 16) | acnt)
        self._pw(s, 7, ccnt)
        if ccnt == 0:                                # transfer complete
            self._complete(s, opt, link, qdma)

    def _complete(self, s, opt, link, qdma):
        if opt >> 20 & 1:                        # TCINTEN
            tcc = opt >> 12 & 0x3F
            self.ipr |= 1 << tcc
            self._irq()
        if link != 0xFFFF and 0x4000 <= link < 0x8000:
            o = link - 0x4000
            self.param[32 * s:32 * s + 32] = self.param[o:o + 32]
            if qdma:
                # the link update writes the QDMA trigger word, which
                # submits the new set at once (EDMA3 UG, QDMA linking):
                # Pioneer chains L then R (static, TCC 8) this way
                self._qdma_check(32 * s, 32)
        else:                                    # NULL set
            self.param[32 * s:32 * s + 32] = bytes(20) + \
                (0xFFFF).to_bytes(4, "little") + bytes(8)

    def _copy(self, src, dst, n):
        bus = self.dev.bus
        if not bus.watch:
            # RAM to RAM (the audio blocks Pioneer's QDMA moves, 588 words
            # each way per 1/75 s): one slice copy, same bytes as the word
            # loop below whenever the two ranges don't overlap
            sb = db = None
            for lo, hi, buf in bus.ram:
                if sb is None and lo <= src and src + n <= hi:
                    sb, so = buf, src - lo
                if db is None and lo <= dst and dst + n <= hi:
                    db, do = buf, dst - lo
            if sb is not None and db is not None and (
                    sb is not db or so + n <= do or do + n <= so):
                db[do:do + n] = sb[so:so + n]
                return
        for k in range(0, n, 4):
            w = min(4, n - k)
            bus.write(dst + k, w, bus.read(src + k, w))

    def _irq(self, force=False):
        if self.ipr & self.ier:
            self.stats["irq"] += 1
            self.dev.intc.raise_event(8)             # EDMA3_CC0_INT1


def _s16(v):
    return v - 0x10000 if v & 0x8000 else v


class McAsp:
    """McASP transmitter as a clocked source of DMA events; the data port
    (separate 4 KB window) collects the samples the DMA writes."""
    def __init__(self, dev, name, dma_ch):
        self.dev, self.name, self.dma_ch = dev, name, dma_ch
        self.regs = {}
        # 32-bit words written. A plain list (tools slice it), trimmed from
        # the front when it reaches `keep`; `base` counts the words trimmed
        # so base + len(out) is the absolute word count. (Until 2026-10-21
        # the list simply stopped growing after ~1 min: sound died there.)
        self.out = []
        self.base = 0
        self.keep = 44100 * 2 * 120                  # ~2 min of stereo

    def read(self, off, n):
        if off == 0xC0:                              # XSTAT: no errors
            return 0
        return self.regs.get(off, 0)

    def write(self, off, n, v):
        v &= MASK
        # GBLCTL (0x44) is one register seen through RGBLCTL (0x60, receive
        # bits 4:0) and XGBLCTL (0xA0, transmit bits 12:8): the DSP sets a
        # bit in XGBLCTL and polls GBLCTL until it shows (McASP UG; the
        # cdj-nxs2-qemu model does the same). Unlinked, the 700's DSP spun
        # on 0x01D04044 forever.
        if off in (0x44, 0x60, 0xA0):
            mask = {0x44: 0x1F1F, 0x60: 0x001F, 0xA0: 0x1F00}[off]
            g = (self.regs.get(0x44, 0) & ~mask) | (v & mask)
            for r in (0x44, 0x60, 0xA0):
                self.regs[r] = g
            return
        self.regs[off] = v

    def running(self):
        x = self.regs.get(0xA0, 0) | self.regs.get(0x44, 0)
        return (x & 0x1F00) == 0x1F00                # XFRST|XSMRST|... out of reset

    def slots(self):
        return max(1, bin(self.regs.get(0xB8, 3) & 0xFFFFFFFF).count("1"))

    @property
    def total(self):
        return self.base + len(self.out)

    def trim(self):
        k = len(self.out) - self.keep // 2           # keep the newest half
        if k > 0:
            del self.out[:k]
            self.base += k

    def data_write(self, v):
        if getattr(self.dev, "muted", False):
            v = 0                                    # paused deck (dspdev.held)
        self.out.append(v & MASK)
        if len(self.out) >= self.keep:
            self.trim()


class McAspData:
    def __init__(self, asp):
        self.asp = asp

    def read(self, off, n):
        return 0

    def write(self, off, n, v):
        self.asp.data_write(v)
