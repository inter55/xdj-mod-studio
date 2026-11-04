"""dspdev.py -- the REAL XDJ-700 DSP inside the SH emulator (co-simulation).

Enable with XDJ_DSP_EMU=1 (SH7724 then routes ALL CS6 traffic here and
ticks the DSP with the SH clock). Pioneer's own DSP code runs on
emu/c6x/cpu.py; nothing about the protocol is faked.

Wiring (CLAUDE.md §68, read from both firmwares):
  SH CS6 0x18000000 HPIC  <-> DSP UHPI HPIC 0x01E10030 (same register)
  SH CS6 0x18040000 HPIA  (host address latch)
  SH CS6 0x18080000 HPID with auto-increment, 0x180C0000 HPID fixed
  SH port 0xA4050164 bit1 -> DSP GPIO GP4[5]   } boot command code
  SH port 0xA4050128 bit7 -> DSP GPIO GP4[2]   } (0..3)
  SH port 0xA405012E bit0 -> DSP reset release
  DSP boot (HPI boot ROM, modelled): after reset release the core waits
  for the host's first DSPINT, then jumps to the address the host
  stored at L2[0x11800000].
HPIC bits (UHPI): 0 HWOB, 1 DSPINT (host->DSP), 2 HINT (DSP->host),
  3 HRDY. Host writes: DSPINT=1 sets it, HINT=1 clears it. DSP writes:
  DSPINT=1 clears it, HINT=1 sets it.
"""
import os
import threading

from .bus import DspBus
from .cpu import Cpu, Halt
from .cfast import make_cpu
from .decode import decode_bytes
from .isa import Unimplemented

UHPI = 0x01E10000
GPIO = 0x01E26000
SPI1 = 0x01E12000
DSP_PER_SH = float(os.environ.get("XDJ_DSP_RATIO", "0.6"))   # 300 / 500 MHz
# The SH core runs in slices of up to millions of cycles between ticks, so a
# host handshake (set a command code on the GPIO pins, poll the DSP's busy
# pin, clear the code) can finish inside one slice before the DSP has run
# at all -- the DSP then never sees the command (boot deadlock, 2026-10-12).
# Catch-up: every SH read of a DSP-driven signal (busy pin, HPI registers)
# first runs the DSP for SYNC_CYCLES; the next tick() pays that time back.
SYNC_CYCLES = int(os.environ.get("XDJ_DSP_SYNC", "2000"))
SYNC_MAX_AHEAD = int(os.environ.get("XDJ_DSP_AHEAD", "20000"))
DSP_HZ = 300_000_000
FS = int(os.environ.get("XDJ_DSP_FS", "44100"))          # output sample rate (44.1k)
SLOT_CYCLES = DSP_HZ // (FS * 2)                          # 2 TDM slots (L, R)
BURST = int(os.environ.get("XDJ_DSP_BURST", "32"))       # slot events per pacing step
# Paused-deck mute. A paused DSP stops refilling its output ring but the
# McASP DMA keeps sending it, so the last 13 input blocks (0.17 s) repeat
# forever -- the "machine gun" on pause (cdj-nxs2-qemu notes the same and
# mutes on its host side). The real deck is silent. The DSP reports the
# pause itself: status 0x11837BF8 = 3 (2 while playing) and its play
# counter 0x11837CCC stops. Output is silenced while both hold; any counter
# movement (cue preview, jog scrub) releases it at once.
PAUSE_MUTE = os.environ.get("XDJ_DSP_PAUSE_MUTE", "1") != "0"
ST_STATE, ST_COUNT = 0x11837BF8, 0x11837CCC
HELD_BURSTS = 64                     # check every 64 bursts (~46 ms at 44.1k)


class Uhpi:
    """DSP-side view of the HPI module registers"""
    def __init__(self, dev):
        self.dev = dev

    def read(self, off, n):
        if off == 0x30:
            return self.dev.hpic | 0x8         # HRDY: the host port is always ready
        if off == 0x00:
            return 0x44828A00          # UHPI peripheral ID (C6747 family)
        return self.dev.uhpi_regs.get(off, 0)

    def write(self, off, n, v):
        if off == 0x30:
            with self.dev.irq_lock:
                if v & 2:
                    self.dev.hpic &= ~2            # DSP acknowledges DSPINT
                if v & 4:
                    self.dev.hpic |= 4             # DSP raises HINT to the host
                self.dev.hpic = (self.dev.hpic & 0x6) | (v & ~0x6 & 0xFFFF)
            return
        self.dev.uhpi_regs[off] = v


class Gpio:
    """OMAP-L1x GPIO: per bank pair DIR +0, OUT_DATA +4, SET_DATA +8,
    CLR_DATA +0xC, IN_DATA +0x10; pair 45 at 0x60 (bank 4 = bits 0-15)."""
    def __init__(self, dev):
        self.dev = dev
        self.regs = {}
        self.out45 = 0

    def read(self, off, n):
        if off == 0x70:                        # IN_DATA45
            sh = self.dev.sh
            b5 = (sh.read(0x04050164, 1) >> 1) & 1
            b2 = (sh.read(0x04050128, 1) >> 7) & 1
            if (b5, b2) != getattr(self, "_last_code", None):
                self._last_code = (b5, b2)
                self.dev.ev("code-seen", b5 | b2 << 1)
            return (self.out45 & ~0x24) | b5 << 5 | b2 << 2
        if off in (0x64, 0x68, 0x6C):          # OUT/SET/CLR read back OUT
            return self.out45
        return self.regs.get(off, 0)

    def write(self, off, n, v):
        if off == 0x64:
            self.out45 = v
        elif off == 0x68:
            self.out45 |= v
        elif off == 0x6C:
            self.out45 &= ~v & 0xFFFFFFFF
        else:
            self.regs[off] = v


class Spi:
    """OMAP-L1x SPI (master) with an always-ready slave: a write to
    SPIDAT0/1 (+0x38/+0x3C) completes at once; SPIBUF (+0x40) returns the
    received word (RX_DATA in 15:0, 0 for now) with RXEMPTY bit31 = 0
    once, then 1 (reading consumes it); TXFULL bit29 always 0. SPIFLG
    (+0x10): TXINTFLG bit9 = 1, RXINTFLG bit8 = !RXEMPTY. Pioneer's DSP
    main image waits on these right after boot (2026-10-12). Every word
    sent is logged (dev.spi_tx) so the slave can be identified later."""
    def __init__(self, dev):
        self.dev = dev
        self.regs = {}
        self.rxempty = 1
        self.rx = 0

    def read(self, off, n):
        if off == 0x40:
            # RXEMPTY always set: the slave never answers. This is what a
            # working run of the real DSP program sees (cdj-nxs2-qemu
            # recording: 14,825 SPIBUF reads during play, all 0x80000000);
            # handing back a "received" 0 once per write sent the DSP's
            # control loop down another path.
            return 0x80000000
        if off == 0x10:
            return (1 << 9) | ((0 if self.rxempty else 1) << 8)
        return self.regs.get(off, 0)

    def write(self, off, n, v):
        self.regs[off] = v
        if off in (0x38, 0x3C):
            tx = self.dev.spi_tx
            if len(tx) < 100000:
                tx.append((self.dev.cycles, v & 0xFFFFFFFF))
            self.rx = 0
            self.rxempty = 0


class RegLog:
    """register file that remembers writes (read-back) and logs them --
    stand-in for a peripheral until it gets a real model"""
    def __init__(self, dev, name):
        self.dev, self.name, self.regs = dev, name, {}

    def read(self, off, n):
        return self.regs.get(off, 0)

    def write(self, off, n, v):
        self.regs[off] = v
        lg = self.dev.reglog
        if len(lg) < 200000:
            lg.append((self.dev.cycles, self.name, off, v & 0xFFFFFFFF))


STUBS = ((0x01800000, 0x1000, "INTC"), (0x01C00000, 0x8000, "EDMA3CC"),
         (0x01C08000, 0x800, "EDMA3TC"), (0x01C20000, 0x1000, "TIMER0"),
         (0x01C21000, 0x1000, "TIMER1"), (0x01D00000, 0x1000, "MCASP0"),
         (0x01D04000, 0x1000, "MCASP1"), (0x01D08000, 0x1000, "MCASP2"))


class DspDevice:
    def __init__(self, sh):
        self.sh = sh
        self.bus = DspBus()
        self.bus.devices[UHPI] = (0x1000, Uhpi(self))
        self.bus.devices[GPIO] = (0x1000, Gpio(self))
        self.bus.devices[SPI1] = (0x1000, Spi(self))
        self.spi_tx = []
        self.reglog = []
        from . import periph
        self.intc = periph.Intc(self)
        self.edma = periph.Edma3(self)
        self.asp1 = periph.McAsp(self, "MCASP1", 3)      # EDMA ch 3 = McASP1 TX
        self.asp2 = periph.McAsp(self, "MCASP2", 5)      # EDMA ch 5 = McASP2 TX
        self.bus.devices[0x01800000] = (0x1000, self.intc)
        self.bus.devices[0x01C00000] = (0x8000, self.edma)
        self.bus.devices[0x01D04000] = (0x1000, self.asp1)
        self.bus.devices[0x01D06000] = (0x1000, periph.McAspData(self.asp1))
        self.bus.devices[0x01D08000] = (0x1000, self.asp2)
        self.bus.devices[0x01D0A000] = (0x1000, periph.McAspData(self.asp2))
        self.next_slot = 0                         # DSP cycle of the next TDM slot
        self.muted = False                         # paused-deck mute (see PAUSE_MUTE)
        self._held_n = 0
        self._held_cnt = None
        for base, size, name in STUBS:            # replaced by real models
            self.bus.devices.setdefault(base, (size, RegLog(self, name)))
        self.cpu = make_cpu(self.bus)
        self.cpu.code_hook = self._ensure_code
        self._arm_idle()
        self.hpic = 0
        self.hpia = 0
        self.uhpi_regs = {}
        self.running = False          # core executing
        self.in_reset = True
        self.decoded = set()          # 64 KB pages already decoded
        self.debt = 0.0
        self.ratio = DSP_PER_SH       # DSP cycles per SH cycle (lower = faster
                                      # emulation while nothing plays)
        # Sub-threshold syncs are no-ops: the firmware polls HPI state in
        # tight loops, and advancing the DSP for every ~dozen-cycle poll
        # cost a Python+ctypes round trip each (2026-10-24: 410k advances
        # per 10 s of play). Deferring to the next slice boundary is
        # indistinguishable -- the data is at most MINSYNC*ratio DSP cycles
        # stale and the poll loop just iterates again, exactly as on a host
        # whose HPI reads are slower than the DSP. tick() keeps time
        # flowing at every slice, so no handshake can deadlock on this.
        self.minsync = int(os.environ.get("XDJ_DSP_MINSYNC", "128"))
        self.ahead = 0                # cycles run early by sync() (no clock)
        self.clock = None             # () -> SH cycle count (set by the C core)
        self.sh_seen = None           # SH cycle already accounted for
        self.credit = 0               # DSP cycles run ahead (bonus bursts)
        self.error = None
        self.hold = False            # ui: pause the model during USB bursts
                                     # (mount/track load: the deck is silent,
                                     # so stopping the DSP for those seconds is
                                     # inaudible and it is 4x the SH's speed)
        self.cycles = 0
        self._acc = {}                # byte-wise host accesses being assembled
        self.log = []
        # play mode (XDJ_PLAY=1 or XDJ_DSP_THREAD=1): the DSP runs on its own
        # thread, in parallel with the SH (as two real chips do). The SH
        # side only queues the DSP time it owes; the worker works it off in
        # chunks. Not cycle-exact (the DSP may trail the SH by up to
        # max_lag cycles; the SH waits beyond that). Exact mode = default.
        # irq_lock guards state both threads change (interrupt flags, HPIC);
        # run_lock is held by the worker while it executes a chunk and by
        # the SH side for the rare things that must not overlap a running
        # core (code rewrite, reset, snapshot).
        self.irq_lock = threading.RLock()
        self.run_lock = threading.RLock()
        self.threaded = os.environ.get(
            "XDJ_DSP_THREAD", "1" if os.environ.get("XDJ_PLAY") == "1" else "0") == "1"
        self.max_lag = int(os.environ.get("XDJ_DSP_MAXLAG", "3000000"))
        self._cv = threading.Condition()
        self._owed = 0
        self._thr = None
        self._stop = False
        self._run_since = 0           # DSP cycles since boot (threading waits for boot)

    # ---------------------------------------------------------- host side
    def _word(self, a):
        return self.bus.read(a & ~3, 4)

    # HPIA: while the SH fast core runs with cs6_fast it OWNS the address
    # latch (word stores land only in its mirror, Core.cs6_addr); every
    # Python reader -- HPID crossings, the DMAC bulk path that downloads
    # the boot image -- must see that value, so the property reads through.
    @property
    def hpia(self):
        m = self.__dict__.get("_cs6_mirror")
        return m.cs6_addr if m is not None else self.__dict__.get("_hpia", 0)

    @hpia.setter
    def hpia(self, v):
        self.__dict__["_hpia"] = v & 0xFFFFFFFF
        m = self.__dict__.get("_cs6_mirror")
        if m is not None:
            m.cs6_addr = v & 0xFFFFFFFF

    def _hpia_set(self, v):
        """HPIA changed on the Python side (crossings, DMAC/bulk paths);
        push into the fast core's mirror, which owns HPIA while it runs
        (cs6_fast -- see emu/fastcpu.py)."""
        self.hpia = v & 0xFFFFFFFF
        m = getattr(self, "_cs6_mirror", None)
        if m is not None:
            m.cs6_addr = self.hpia

    def read(self, a, size):
        """SH read of CS6 physical address a (1/2/4 bytes)."""
        self.sync()
        off = a - 0x18000000
        if off < 4:
            return (self.hpic >> (8 * off)) & ((1 << (8 * size)) - 1)
        if 0x40000 <= off < 0x40004:
            return (self.hpia >> (8 * (off - 0x40000))) & ((1 << (8 * size)) - 1)
        for port, inc in ((0x80000, True), (0xC0000, False)):
            if port <= off < port + 4:
                b = off - port
                v = self._word(self.hpia)
                if inc and b + size >= 4:
                    self._hpia_set(self.hpia + 4)
                return (v >> (8 * b)) & ((1 << (8 * size)) - 1)
        return 0

    def write(self, a, size, val):
        off = a - 0x18000000
        if off < 4:
            self.sync(SYNC_CYCLES)              # DSP sees HPIC as it was
            sh = 8 * off
            v = (val & ((1 << (8 * size)) - 1)) << sh
            low = v & 0xFFFF
            self.ev("hpic-w", hex(low))
            with self.irq_lock:
                if low & 2 and not (self.hpic & 2):
                    self.hpic |= 2                  # host -> DSP interrupt
                    self._dspint()
                if low & 4:
                    self.hpic &= ~4                 # host clears HINT
                keep = 0x6
                self.hpic = (self.hpic & keep) | (low & ~keep & 0xFFFF)
            return
        if 0x40000 <= off < 0x40004:
            sh = 8 * (off - 0x40000)
            m = ((1 << (8 * size)) - 1) << sh
            self._hpia_set(((self.hpia & ~m) | ((val << sh) & m)))
            return
        for port, inc in ((0x80000, True), (0xC0000, False)):
            if port <= off < port + 0x100 if inc else port <= off < port + 4:
                b = (off - port) & 3
                cur, n = self._acc.get(port, (0, 0))
                cur |= (val & ((1 << (8 * size)) - 1)) << (8 * b)
                n += size
                if n >= 4:
                    self._store(self.hpia, cur & 0xFFFFFFFF)
                    if inc:
                        self._hpia_set(self.hpia + 4)
                    self._acc.pop(port, None)
                else:
                    self._acc[port] = (cur, n)
                return

    def bulk(self, data):
        """DMA burst into the auto-increment port (whole words)."""
        for k in range(0, len(data) - 3, 4):
            self._store(self.hpia, int.from_bytes(data[k:k + 4], "little"))
            self._hpia_set(self.hpia + 4)

    def _store(self, a, v):
        self.bus.write(a, 4, v)
        pg = a >> 16
        if pg in self.decoded:        # code in this page changed: re-decode
            # never under a running core (play mode)
            with self.run_lock:
                self.decoded.discard(pg)
                if hasattr(self.cpu, "drop_code"):
                    self.cpu.drop_code(pg << 16, (pg + 1) << 16)
                for x in [x for x in self.cpu.prog if x >> 16 == pg]:
                    self.cpu.prog.pop(x, None)
                    self.cpu.ops.pop(x, None)

    def busy_pin(self):
        """SH 0xA405016C bit0 = the DSP's HPI interrupt line HINT (active
        low): 0 while HPIC.HINT is set. The SH waits for 0 after each
        command (stage-1 raises HINT when a chunk is placed) and clears it
        by writing HPIC bit 2. (2026-10-12: was GP4[14] -- wrong; stage-1
        raises HINT, never drops GP4[14], so the SH timed out and retried.)"""
        self.sync()
        v = 0 if self.hpic & 4 else 1
        if v != getattr(self, "_last_busy", None):
            self._last_busy = v
            self.ev("busy", v)
        return v

    def _held(self):
        cnt = self.bus.read(ST_COUNT, 4)
        moved = cnt != self._held_cnt
        self._held_cnt = cnt
        self.muted = not moved and self.bus.read(ST_STATE, 4) == 3

    def ev(self, *what):
        self.log.append((self.sh.cyc,) + what)

    # ---------------------------------------------------------- boot
    def _dspint(self):
        if self.running and not self.in_reset:
            self.intc.raise_event(34)              # UHPI_DSPINT -> INT15 (INTMUX3)
            return
        if self.in_reset:
            return
        entry = self.bus.read(0x11800000, 4)
        self.ev("boot", hex(entry))
        self.cpu.pc = entry
        self.running = True

    def reset(self):
        """DSP held in reset again (SH retry): core stops, GPIO outputs and
        HPI state return to reset values; memory keeps its contents."""
        with self.run_lock:
            with self._cv:
                self._owed = 0
                self._cv.notify_all()
            self._reset_locked()

    def _reset_locked(self):
        self._run_since = 0
        self.in_reset = True
        self.running = False
        self.cpu = make_cpu(self.bus)
        self.cpu.code_hook = self._ensure_code
        self._arm_idle()
        self.bus.devices[GPIO][1].out45 = 0
        self.hpic = 0
        if getattr(self, "full_reset", True):  # a chip reset clears peripherals too
            from . import periph
            self.intc = periph.Intc(self)
            self.edma = periph.Edma3(self)
            self.asp1 = periph.McAsp(self, "MCASP1", 3)
            self.asp2 = periph.McAsp(self, "MCASP2", 5)
            self.bus.devices[0x01800000] = (0x1000, self.intc)
            self.bus.devices[0x01C00000] = (0x8000, self.edma)
            self.bus.devices[0x01D04000] = (0x1000, self.asp1)
            self.bus.devices[0x01D06000] = (0x1000, periph.McAspData(self.asp1))
            self.bus.devices[0x01D08000] = (0x1000, self.asp2)
            self.bus.devices[0x01D0A000] = (0x1000, periph.McAspData(self.asp2))
            self.next_slot = self.cycles
            self.decoded = set()

    def _arm_idle(self):
        if os.environ.get("XDJ_DSP_IDLE", "1") != "0":
            from .cfast import set_idle_loop
            # 0xC004C8CC: the stopped deck's idle loop; 0xC0048C2E: the
            # playing deck's (ten channel polls per 2237-cycle pass, ~75 %
            # of the DSP's cycles while a track plays, 2026-11-04)
            more = () if os.environ.get("XDJ_DSP_IDLE_PLAY", "1") == "0" else (0xC0048C2E,)
            set_idle_loop(self.cpu, 0xC004C8CC, 0x11804AE0, 0x11805C00, more)

    def _ensure_code(self, pc):
        pg = pc >> 16
        if pg not in self.decoded:
            base = pg << 16
            self.cpu.add_code(decode_bytes(self.bus.dump(base, 0x10000), base))
            self.decoded.add(pg)

    # ---------------------------------------------------------- clock
    def tick(self, sh_cycles):
        # reset line: SH port 0xA405012E bit0 = 1 releases the DSP
        rl = self.sh.read(0x0405012E, 1) & 1
        if self.in_reset and rl:
            self.in_reset = False
            self.hpic = 4             # HPI comes out of reset clean; boot ROM: "ready" (HINT)
            self.ev("reset-release")
        elif not self.in_reset and not rl:
            self.ev("reset-assert")
            self.reset()
        if self.clock is not None:
            self._advance()
            return
        if self.hold or not self.running or self.error:
            return
        self.debt += sh_cycles * self.ratio
        n = int(self.debt)
        self.debt -= n
        paid = min(n, self.ahead)                # already run by sync()
        self.ahead -= paid
        self._run(n - paid)

    def _advance(self, bonus=0):
        """time-locked mode (C core): run the DSP for exactly the SH time
        passed since the last call (300/500 MHz); `bonus` = run at least
        this many now, repaid from later time (back-to-back host writes
        that real hardware separates by a data transfer)."""
        now = self.clock()
        if self.sh_seen is None:
            self.sh_seen = now
            return
        if self.hold:
            # held (USB burst fast-forward): account the time, run nothing
            self.sh_seen = now
            return
        if not self.running or self.error or self.in_reset:
            self.sh_seen = now
            return
        if bonus == 0 and self.minsync and now - self.sh_seen < self.minsync:
            return                          # deferred (see MINSYNC above)
        self.debt += (now - self.sh_seen) * self.ratio
        self.sh_seen = now
        n = int(self.debt)
        self.debt -= n
        pay = min(n, self.credit)
        self.credit -= pay
        n -= pay
        if bonus > n:
            self.credit += bonus - n
            n = bonus
        if n > 0:
            if self._thread_ok():
                self._queue(n)
            else:
                self._run(n)

    # ---------------------------------------------------------- play mode
    BOOT_SETTLE = 200_000_000          # DSP cycles of lock-step after boot

    def _thread_ok(self):
        return (self.threaded and self.running and not self.in_reset and not self.error
                and self._run_since >= self.BOOT_SETTLE)

    def _queue(self, n):
        if self._thr is None or not self._thr.is_alive():
            self._stop = False
            self._thr = threading.Thread(target=self._worker, name="dsp", daemon=True)
            self._thr.start()
        with self._cv:
            self._owed += n
            self._cv.notify_all()
            while self._owed > self.max_lag and self._thr.is_alive() and not self.error:
                self._cv.wait(0.05)        # back-pressure: the SH waits for the DSP

    CHUNK = 60_000

    def _worker(self):
        while True:
            with self._cv:
                while self._owed <= 0 and not self._stop:
                    self._cv.wait(0.1)
                if self._stop:
                    return
                k = min(self._owed, self.CHUNK)
            with self.run_lock:
                if self._thread_ok():
                    self._run(k)
                    done = k
                else:
                    done = 0
            with self._cv:
                self._owed -= done if done else self._owed
                self._cv.notify_all()

    def drain(self):
        """play mode: run whatever the DSP still owes, now, on this thread
        (callers hold run_lock to keep the worker out). No-op otherwise."""
        with self.run_lock:
            with self._cv:
                k, self._owed = self._owed, 0
                self._cv.notify_all()
            if k > 0 and self.running and not self.in_reset and not self.error:
                self._run(k)

    def stop_thread(self):
        if self._thr is not None:
            self.drain()
            with self._cv:
                self._stop = True
                self._cv.notify_all()
            self._thr.join(timeout=5)
            self._thr = None

    def sync(self, n=None):
        """run the DSP now (before the SH sees a DSP-driven signal). Never
        more than SYNC_MAX_AHEAD cycles ahead of the SH-paced clock: the
        SH polls the HPI constantly once the DSP runs, and unbounded
        catch-up ran the DSP ~18x faster than real (2026-10-12)."""
        if self.clock is not None:
            self._advance(0 if n is None else n)
            return
        if self.running and not self.error and not self.in_reset:
            n = SYNC_CYCLES if n is None else n
            n = min(n, SYNC_MAX_AHEAD - self.ahead)
            if n <= 0:
                return
            self._run(n)
            self.ahead += n

    def _run(self, n):
        cpu = self.cpu
        self._run_since += n
        try:
            while n > 0:
                k = n
                # TX DMA events for every McASP channel enabled in EDMA EER
                # (ch 3 McASP1, ch 5 McASP2): gating on McASP2's registers
                # starved ch 5, which the DSP's audio loop also waits on.
                active = (self.edma.eer >> 3 | self.edma.eer >> 5) & 1
                if active:
                    if self.next_slot <= self.cycles:
                        self.next_slot = self.cycles + 1
                    k = min(n, self.next_slot - self.cycles)
                if hasattr(cpu, "run_cycles"):
                    cpu.run_cycles(k)
                else:
                    for _ in range(k):
                        self._ensure_code(cpu.pc)
                        cpu.step()
                self.cycles += k
                n -= k
                if active and self.cycles >= self.next_slot:
                    # One half-buffer (BURST slot events) per SLOT_CYCLES
                    # * BURST cycles = the chip's own cadence of 1 word per
                    # SLOT_CYCLES DSP cycles, batched the way the program's
                    # half-buffer servicing expects. 2026-10-16 briefly
                    # paced one BURST per SLOT_CYCLES to fatten the
                    # DspSound bridge; that is 32x the designed rate: the
                    # DSP never gets the cycles to refill its output
                    # between services, the McASP ships zeros, the SH4's
                    # load handshake (buffer level 0x11837CCC) never
                    # completes and every track load dies as E-8302
                    # (reproduced on stick7 AND the user's rekordbox
                    # stick; XDJ_DSP_BURST=1 A/B-proved it). Audio on a
                    # host slower than real time gaps instead (DspSound
                    # stalls cleanly); the core getting faster is the only
                    # honest way to close that gap.
                    for asp in (self.asp1, self.asp2):
                        if self.edma.eer >> asp.dma_ch & 1:
                            self.edma.burst(asp.dma_ch, BURST)
                    self.next_slot += SLOT_CYCLES * BURST
                    self._held_n += 1
                    if PAUSE_MUTE and self._held_n >= HELD_BURSTS:
                        self._held_n = 0
                        self._held()
        except (Halt, Unimplemented, KeyError, AssertionError) as e:
            self.error = f"{type(e).__name__}: {e} at pc {cpu.pc:#010x} cycle {cpu.cycle}"
            self.log.append(("error", self.error))


# ------------------------------------------------------------- snapshots
def _quiet(c):
    return (not c.ev and not c.pend and c.sp is None and not c.stall
            and getattr(c, "splx_irq", None) is None)


def _quiesce(d, limit=50_000_000):
    """run until nothing is in flight (pipeline events, branches, SPLOOP,
    an interrupt drained out of a SPLOOP), so the state is plain data that
    pickles. Pioneer's delay loops are SPLOOPs of ~14k cycles that get
    interrupted and restarted, so this can take a while."""
    c = d.cpu
    for _ in range(limit // 64):
        if _quiet(c):
            return True
        if hasattr(c, "run_cycles"):           # C core: probe cycle by cycle
            for _ in range(64):
                c.run_cycles(1)
                d.cycles += 1
                if _quiet(c):
                    return True
        else:
            for _ in range(64):
                d._ensure_code(c.pc)
                c.step()
                d.cycles += 1
                if _quiet(c):
                    return True
    return False


def state(d):
    with d.run_lock:                       # play mode: worker paused, owed run
        d.drain()
        return _state(d)


def _state(d):
    if hasattr(d.cpu, "settle"):
        d.cpu.settle()                     # a frozen delay call -> real state
    if d.running and not d.error:
        if not _quiesce(d):
            raise RuntimeError("DSP never quiet enough to snapshot")
    m = getattr(d, "_cs6_mirror", None)     # fast core owns HPIA: adopt it
    if m is not None:
        d.hpia = m.cs6_addr
    seen, regions = set(), []
    for lo, hi, buf in d.bus.ram:
        if id(buf) not in seen:
            seen.add(id(buf))
            regions.append((lo, bytes(buf)))
    c = d.cpu
    g = d.bus.devices[GPIO][1]
    sp = d.bus.devices[SPI1][1]
    return {"regions": regions, "r": list(c.r), "c": dict(c.c), "pc": c.pc,
            "cycle": c.cycle, "idle": c.idle,
            "hpic": d.hpic, "hpia": d.hpia, "uhpi": dict(d.uhpi_regs),
            "gpio": (g.out45, dict(g.regs)), "spi": (dict(sp.regs), sp.rxempty, sp.rx),
            "running": d.running, "in_reset": d.in_reset, "debt": d.debt,
            "credit": d.credit, "cycles": d.cycles, "error": d.error,
            "spi_tx": d.spi_tx[-2000:],
            "intc": (list(d.intc.flag), list(d.intc.mask), dict(d.intc.mux), dict(d.intc.regs)),
            "edma": (bytes(d.edma.param), dict(d.edma.regs), d.edma.eer, d.edma.ier,
                     d.edma.ipr, d.edma.er),
            "qeer": d.edma.qeer,
            "asp": (dict(d.asp1.regs), dict(d.asp2.regs)), "next_slot": d.next_slot}


def restore(d, st):
    for lo, data in st["regions"]:
        d.bus.load(lo, data)
    c = d.cpu
    c.r[:] = st["r"]
    c.c.update(st["c"])
    c.pc, c.cycle, c.idle = st["pc"], st["cycle"], st["idle"]
    d.decoded = set()
    if hasattr(c, "clear_code"):
        c.clear_code()
    c.prog.clear()
    c.ops.clear()
    d.hpic, d.hpia = st["hpic"], st["hpia"]
    d.uhpi_regs = dict(st["uhpi"])
    g = d.bus.devices[GPIO][1]
    g.out45, g.regs = st["gpio"][0], dict(st["gpio"][1])
    sp = d.bus.devices[SPI1][1]
    sp.regs, sp.rxempty, sp.rx = dict(st["spi"][0]), st["spi"][1], st["spi"][2]
    d.running, d.in_reset = st["running"], st["in_reset"]
    d.debt, d.credit, d.cycles, d.error = st["debt"], st["credit"], st["cycles"], st["error"]
    d._hpia_set(st["hpia"])                # pushes into the fast-core mirror
    d.spi_tx = list(st["spi_tx"])
    d.sh_seen = None                  # re-anchored to the SH clock on first use
    if d.running and not d.in_reset:  # a booted DSP: play mode may thread at once
        d._run_since = d.BOOT_SETTLE
    if "intc" in st:
        f, mk, mux, rg = st["intc"]
        d.intc.flag, d.intc.mask, d.intc.mux, d.intc.regs = list(f), list(mk), dict(mux), dict(rg)
        pa, rg, d.edma.eer, d.edma.ier, d.edma.ipr, d.edma.er = st["edma"]
        d.edma.param, d.edma.regs = bytearray(pa), dict(rg)
        d.edma.qeer = st.get("qeer", d.edma.regs.get(0x108C, 0) | d.edma.regs.get(0x208C, 0)) & 0xFF
        d.asp1.regs, d.asp2.regs = dict(st["asp"][0]), dict(st["asp"][1])
        d.next_slot = st["next_slot"]
