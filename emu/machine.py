"""XDJ-700 machine: physical memory, SH-4 + FPU + SH7724 wiring, boot driver.

Physical map (from repo RE notes):
  flash  MAIN.img              @ 0x00000000 (P1/P2 alias 0x80000000/0xA0000000)
  RAM    64 MB DDR2            @ 0x08000000
  framebuffers 2 x 768000 B    @ 0x0E780000 / 0x0E83B800
  staging/resident window      @ 0x0FFFD000 (64 KB)
  store queues 2 x 32 B        @ 0xE0000000
  everything else >= 0xE0000000 or in peripheral windows -> SH7724 device
"""
import collections
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
sys.path.insert(0, ROOT)

from emu.sh4 import SH4
from emu.sh4fpu import FPU
from emu.sh7724 import SH7724

FB0 = 0x0E780000
FB1 = 0x0E83B800
FB_SIZE = 800 * 480 * 2
IDLE_STACKCHECK = 0x08D6A7C0   # RTOS idle task's stack checker (see _native_idle)


class MachineMem:
    # P4 MMU-area registers that must stay live with CPU state (byte storage
    # mirrors the cpu fields; firmware reaches them via mov.l, split to bytes)
    MMU_IO = {0xFF000000: "pteh", 0xFF000004: "ptel", 0xFF000008: "ttb",
              0xFF00000C: "tea", 0xFF000010: "mmucr", 0xFF000014: "ptea",
              0xFF00001C: "ccr", 0xFF000020: "tra", 0xFF000024: "expevt",
              0xFF000028: "intevt"}

    def __init__(self, flash, dev_log=None):
        self.flash = flash
        self.cpu = None         # bound by Machine after SH4 creation
        self.mmu_bytes = {}     # addr -> byte (P4 MMU-area mirror)
        self.watch = None       # (olo, ohi, ((plo,phi),...)): latch first
                                # write to ram OFFSET in [olo,ohi) while pc
                                # is in a range (catches ALL aliases: P1/P2/
                                # direct/mirror write the same storage)
        self.watch_hit = None
        self.watch_dma = False  # latch transfers >= 1 MB into dma_hit
        self.dma_hit = None
        # Two 64 MB DRAM banks (NOT mirrors of each other: firmware clears
        # bank1 while running from bank0, so merging them is suicide).
        self.ram0 = bytearray(64 * 1024 * 1024)   # phys 0x08000000 bank
        self.ram1 = bytearray(64 * 1024 * 1024)   # phys 0x0C000000 bank
        self.ram = self.ram0    # compat alias (bank0)
        self.fb = [bytearray(FB_SIZE), bytearray(FB_SIZE)]
        self.misc = bytearray(0x10000)      # @0x0FFFD000
        # NOTE: no separate staging array: 0x0F000000+ is DRAM mirror (see
        # _loc). Kept as an empty attr only for old-checkpoint compat.
        self.staging = bytearray()
        self.sq = bytearray(0x40)           # store queues @0xE0000000
        # BSC Area0 remap model: out of reset Area0 is flash (stub reads the
        # UPD via P0/P2-stripped); once unpacked code is entered the boot has
        # remapped Area0 to SDRAM (its P1 exception vectors at unpack+0x100
        # are live code, while the UPD bytes there are header garbage).
        # Proven: post-entry code makes zero flash-range accesses.
        self.low_is_ram = False
        self.dev = SH7724(self, log=dev_log)
        self.fb_writes = 0
        self.fb_hook = None      # optional fn(lo, hi) on framebuffer writes
                                 # (DMA slice-stores bypass w8phys; hook here)

    # -- physical access (sh4.translate already applied) ------------------
    def _mmu_field(self, a):
        base = a & ~3
        return self.MMU_IO.get(base)

    def _loc(self, a):
        if a < len(self.flash) and not self.low_is_ram:
            return self.flash, a, False
        if FB0 <= a < FB0 + FB_SIZE:
            return self.fb[0], a - FB0, True
        if FB1 <= a < FB1 + FB_SIZE:
            return self.fb[1], a - FB1, True
        if 0x0FFFD000 <= a < 0x1000D000:
            # NOTE: the 64 KB window crosses 0x10000000 (0x0FFFD000+0x10000
            # = 0x1000D000, not 0x0FFE0000); on-chip mem, before DRAM below.
            return self.misc, a - 0x0FFFD000, False
        if 0x08000000 <= a < 0x18000000:
            # Two 64 MB banks + mirrors: 0x08-0x0B bank0, 0x0C-0x0F bank1,
            # 0x10-0x13 mirror bank0, 0x14-0x17 mirror bank1. CS6 at 0x18+
            # stays device. (Bank1 is SEPARATE: firmware clears it while
            # running from bank0; merging the banks suicides the boot.)
            b = 0x08000000 | (a & 0x07FFFFFF)
            if b < 0x0C000000:
                return self.ram0, b - 0x08000000, False
            return self.ram1, b - 0x0C000000, False
        if 0xE0000000 <= a < 0xE0000040:
            return self.sq, a - 0xE0000000, False
        if a < 0x04000000:
            # P1-cached (0x80-0x83) strips to low physical area 0, which
            # aliases SDRAM bank0 once remapped (low_is_ram). Pre-remap the
            # flash branch above keeps precedence for [0, flashlen).
            # (Area 1 stays device: PFC/INTC/CMT at 0x04050100+/0x04080000+.)
            return self.ram0, a, False
        return None, a, False

    def r8phys(self, a):
        if self.cpu is not None and 0xFF000000 <= a < 0xFF00002C:
            f = self._mmu_field(a)
            if f is not None:
                return (getattr(self.cpu, f) >> ((a & 3) * 8)) & 0xFF
        b, o, _ = self._loc(a)
        if b is not None:
            return b[o]
        return self.dev.read(a, 1)

    def w8phys(self, a, v):
        w = self.watch
        # watch: (olo, ohi, pcs, zero_only): latch first matching write.
        # Zero-only watches ignore legit unpack output (nonzero) but catch
        # wipes; pc ranges exclude the stub decompressor.
        if w is not None and self.watch_hit is None and (v & 0xFF) == w[3]:
            cpu = self.cpu
            if cpu is not None and any(lo <= cpu.pc < hi for lo, hi in w[2]):
                b, o, _ = self._loc(a)
                if (b is self.ram0 or b is self.ram1) and w[0] <= o < w[1]:
                    self.watch_hit = (a, o, cpu.pc, v & 0xFF,
                                      cpu.gr(4), cpu.gr(5), cpu.gr(6))
        if self.cpu is not None and 0xFF000000 <= a < 0xFF00002C:
            f = self._mmu_field(a)
            if f is not None:
                cur = getattr(self.cpu, f)
                sh = (a & 3) * 8
                setattr(self.cpu, f, (cur & ~(0xFF << sh)) | ((v & 0xFF) << sh))
                return
        b, o, fb = self._loc(a)
        if b is not None:
            if b is self.flash:
                return
            b[o] = v & 0xFF
            if fb:
                self.fb_writes += 1
                if self.fb_hook is not None:
                    self.fb_hook(a, a + 1)
            return
        self.dev.write(a, 1, v)

    def r8(self, a):
        return self.r8phys(a)

    def w8(self, a, v):
        self.w8phys(a, v)

    def r_block(self, a, n):
        """Up to n bytes from RAM-like storage (None if unmapped/device).
        Same bytes as n x r8, but segment-sliced instead of per-byte."""
        out = bytearray()
        while len(out) < n:
            b, o, _ = self._loc(a)
            if b is None:
                return None
            seg = min(len(b) - o, n - len(out))
            if seg <= 0:
                return None
            out += b[o:o + seg]
            a += seg
        return bytes(out)

    def w_block(self, a, data):
        """Write bytes to RAM-like storage (False if any part unmapped).
        Same effect as per-byte w8 (incl. framebuffer accounting). Falls
        back to exact per-byte semantics while a write-watch is armed."""
        w = self.watch
        if w is not None and self.watch_hit is None:
            for i, v in enumerate(data):
                self.w8phys(a + i, v)
            return True
        i, n = 0, len(data)
        while i < n:
            b, o, fb = self._loc(a)
            if b is None or b is self.flash:
                if b is None:
                    return False
                i += min(len(b) - o, n - i)
                a += min(len(b) - o, n - i)
                continue
            seg = min(len(b) - o, n - i)
            if seg <= 0:
                return False
            b[o:o + seg] = data[i:i + seg]
            if fb:
                self.fb_writes += seg
                if self.fb_hook is not None:
                    self.fb_hook(a, a + seg)
            a += seg
            i += seg
        return True

    def r16(self, a):
        return self.r8phys(a) | (self.r8phys(a + 1) << 8)
    def w16(self, a, v):
        self.w8phys(a, v & 0xFF)
        self.w8phys(a + 1, (v >> 8) & 0xFF)

    def r32(self, a):
        return (self.r8phys(a) | (self.r8phys(a + 1) << 8) |
                (self.r8phys(a + 2) << 16) | (self.r8phys(a + 3) << 24))

    def w32(self, a, v):
        for i in range(4):
            self.w8phys(a + i, (v >> (8 * i)) & 0xFF)

    def r64(self, a):
        return self.r32(a) | (self.r32(a + 4) << 32)

    def w64(self, a, v):
        self.w32(a, v & 0xFFFFFFFF)
        self.w32(a + 4, (v >> 32) & 0xFFFFFFFF)

    # -- cpu interface ------------------------------------------------------
    def take_interrupt(self, imask=-1):
        return self.dev.take_interrupt(imask)

    def tick(self, cyc, ev=False):
        """ev=True (play mode, C event loop): the TMU/LCDC counters live in
        the C core -- fastcpu ticks them via core_ev_tick right after this."""
        self.dev.tick(cyc, ev=ev)


class Machine:
    def __init__(self, flash, trace_len=8192, dev_log=None, core=None):
        self.mem = MachineMem(flash, dev_log=dev_log)
        self.trace = collections.deque(maxlen=trace_len)
        self.idle_skip = True
        self.idle_cycles = 0
        self.excs = collections.deque(maxlen=8192)  # bounded: storms must not OOM
        self.exc_total = 0
        self.excs_first = []  # first 16 faults ever (storm-proof diagnosis)
        self.fault_cb = None     # called as cb(spc, offset, code, cpu) on each fault
        self._make_cpu(core)
        self.cpu.reset()

    def _make_cpu(self, core=None, state=None):
        """C core (emu/fastcore.c) unless XDJ_CORE=py / core="py" or no C
        compiler; the Python core otherwise. Same state either way."""
        from emu import fastcpu
        want = core or os.environ.get("XDJ_CORE", "fast")
        if want != "py" and fastcpu.available():
            self.cpu = fastcpu.FastSH4(self.mem, machine=self)
            self.mem.cpu = self.cpu
            if state is not None:
                self.cpu.__setstate__(state)
            return
        self.cpu = SH4(self.mem)
        self.mem.cpu = self.cpu
        if state is not None:
            self.cpu.__setstate__(state)
        self.cpu.fpu = FPU(self.cpu)
        self.cpu.trace = self._trace
        self.cpu.native_hook = self._native
        self._hook_exc()

    def _hook_exc(self):
        cpu = self.cpu
        machine = self

        def wrapped(offset, code, spc, use_expevt=True, tea=None):
            machine.excs.append((spc, offset, code))
            machine.exc_total += 1
            if len(machine.excs_first) < 16:
                machine.excs_first.append(
                    (spc, offset, code, cpu.tea, cpu.sr, cpu.gr(15), cpu.pr))
            if machine.fault_cb is not None:
                machine.fault_cb(spc, offset, code, cpu)
            return SH4.enter_exc(cpu, offset, code, spc, use_expevt, tea)

        self.cpu.enter_exc = wrapped

    def _trace(self, pc, op):
        # BSC Area0-remap point: entering unpacked code means low physical
        # is SDRAM from here on (see MachineMem.low_is_ram). Exact: fires on
        # the entry instruction's own fetch, in every run path (fast/sliced).
        if pc == 0xA8000800 and not self.mem.low_is_ram:
            self.mem.low_is_ram = True
        self.trace.append((pc, op))

    def _native(self, cpu, pc):
        # Native fast-paths for hot firmware helpers (each verified against
        # the slow path). Returns True if emulated (caller counts a step).
        if (pc & 0x1FFFFFFF) == 0x08E1D2B4:
            return self._native_memcmp(cpu)
        if (pc & 0x1FFFFFFF) == 0x08E1DDB4:
            return self._native_strlen(cpu)
        if (pc & 0x1FFFFFFF) == IDLE_STACKCHECK and self.idle_skip:
            return self._native_idle(cpu)
        return False

    def _native_idle(self, cpu):
        # The RTOS idle task loops calling its stack checker 0x08D6A7C0
        # (walks every task's "StackCheckArea"; no side effects when the
        # stacks are intact). While idle, nothing happens until the next
        # interrupt, so return at once and fast-forward the device clock to
        # the next timer event. Emulated time stays consistent (timers,
        # counters and interrupts all advance); only host time is saved.
        cyc = self.mem.dev.cycles_to_next_event()
        if cyc > 0:
            self.mem.tick(min(cyc + 16, 4_000_000))
            self.idle_cycles += cyc
        cpu.pc = cpu.pr
        return True

    def _read_vbytes(self, cpu, a, n):
        # Bulk virtual read for native helpers (None if unmapped -> caller
        # falls back to slow path, which faults exactly like hardware).
        # Only with MMU off (direct map); with MMU on, translations could
        # vary mid-range, so decline.
        if cpu.mmu_on():
            return None
        from emu.sh4 import SHException
        out = bytearray()
        left, p = n, a & 0xFFFFFFFF
        while left > 0:
            try:
                pp = cpu.translate(p)
            except SHException:
                return None
            sb, so, _ = self.mem._loc(pp)
            if sb is None:
                return None
            seg = min(len(sb) - so, left)
            if seg <= 0:
                return None
            out += sb[so:so + seg]
            p = (p + seg) & 0xFFFFFFFF
            left -= seg
        return bytes(out)

    def _native_memcmp(self, cpu):
        # memcmp(a=r4, b=r5, n=r6): returns extu(a[i])-extu(b[i]) on the last
        # pair (0 if all n match); r4/r1 advance past compared bytes;
        # r5/r6 = last table byte, r7 = last needle byte; T = (r0 == 0).
        r, w = cpu.gr, cpu.set_gr
        a, b, n = r(4), r(5), r(6)
        if n == 0:
            w(1, b)
            w(7, 0)
            cpu.set_t(1)
            cpu.pc, cpu.steps, cpu.cycles = cpu.pr, cpu.steps + 1, cpu.cycles + 2
            return True
        A, B = self._read_vbytes(cpu, a, n), self._read_vbytes(cpu, b, n)
        if A is None or B is None:
            return False
        k = n
        for i in range(n):
            if A[i] != B[i]:
                k = i
                break
        if k == n:
            tb, nb, diff = A[n - 1], B[n - 1], 0
        else:
            tb, nb, diff = A[k], B[k], A[k] - B[k]
        adv = (k + 1) if k < n else n
        w(4, (a + adv) & 0xFFFFFFFF)
        w(1, (b + adv) & 0xFFFFFFFF)
        w(6, tb)
        w(7, nb)
        w(5, tb)
        w(0, diff & 0xFFFFFFFF)
        cpu.set_t(1 if k == n else 0)
        cpu.pc, cpu.steps, cpu.cycles = cpu.pr, cpu.steps + 1, cpu.cycles + n * 10
        return True

    def _native_strlen(self, cpu):
        # strlen @0x08E1DDB4 (r4 = ptr): scans to NUL; returns r0 = r5 =
        # length + 1 (NUL included), r4 = start + len + 1 (past NUL),
        # r6 = NUL address, r2 = 0, T = 1. Unmapped bytes read 0 (generic
        # device stub), same as the slow path; translate faults decline.
        from emu.sh4 import SHException
        r, w = cpu.gr, cpu.set_gr
        start = r(4) & 0xFFFFFFFF
        length, p, total = 0, start, 0
        while True:
            if total > 16 * 1024 * 1024:
                return False  # pathological: let slow path grind/fault
            chunk = self._read_vbytes(cpu, p, 4096)
            if chunk is None:
                try:
                    for i in range(4096):
                        if cpu.r8((p + i) & 0xFFFFFFFF) == 0:
                            length += i + 1
                            p = (p + i) & 0xFFFFFFFF
                            raise StopIteration
                    length += 4096
                    p = (p + 4096) & 0xFFFFFFFF
                    total += 4096
                    continue
                except StopIteration:
                    pass
                except SHException:
                    return False
                break
            i = chunk.find(b"\x00")
            if i == -1:
                length += 4096
                p = (p + 4096) & 0xFFFFFFFF
                total += 4096
                continue
            length += i + 1
            p = (p + i) & 0xFFFFFFFF
            break
        w(5, length & 0xFFFFFFFF)
        w(0, length & 0xFFFFFFFF)
        w(4, (p + 1) & 0xFFFFFFFF)
        w(6, p)
        w(2, 0)
        cpu.set_t(1)
        cpu.pc, cpu.steps, cpu.cycles = cpu.pr, cpu.steps + 1, cpu.cycles + length * 8
        return True

    def watch(self, addrs, cb):
        """Call cb(cpu) the first time pc hits any addr in addrs."""
        want = set(addrs)
        cpu = self.cpu
        orig_trace = cpu.trace

        def tracing(pc, op):
            if pc in want:
                want.discard(pc)
                cb(cpu)
            if orig_trace is not None:
                return orig_trace(pc, op)

        cpu.trace = tracing

    def inject_panl(self, frame):
        """Deliver a 32-byte PANL frame to SCIF1 RX (as the MCU would)."""
        for b in frame:
            self.mem.dev.rx[1].append(b)

    def _usb_state(self):
        """Picklable USB host + stick state (None if no stick was in).
        The overlay (deck's writes) is kept so the FAT still agrees with
        itself after a load; in-flight transfers reset (BOT retries)."""
        dev = self.mem.dev
        usb = getattr(dev, "usb", None)
        if usb is None:
            return None
        if getattr(usb, "stick", None) is None:
            # no stick: still keep the controller registers -- without them
            # a reload shows the USB host switched OFF (SYSCFG 0) and a stick
            # inserted later never mounts (2026-10-12: hit every snapshot
            # booted with no stick, incl. the public app's)
            return {"r": dict(usb.r), "stick": None}
        disk = usb.stick.disk
        if hasattr(disk, "root"):
            spec = {"kind": "folder", "path": disk.root,
                    "label": disk.label.decode("ascii", "replace")}
        else:
            spec = {"kind": "image", "path": getattr(disk, "path", None)}
            if spec["path"] is None:
                return None
        spec["overlay"] = {k: bytes(v) for k, v in disk.written.items()}
        return {"r": dict(usb.r),
                "pipecfg": {n: dict(c) for n, c in usb.pipecfg.items()},
                "buf": {n: bytes(b)[usb.off.get(n, 0):]
                        for n, b in usb.buf.items()},
                "valid": dict(usb.valid),
                "attached": usb.attached, "reset_done": usb.reset_done,
                "tre": {n: list(t) for n, t in usb.tre.items()},
                "trcnt": dict(usb.trcnt),
                "dma_armed": [tuple(x) for x in usb.dma_armed],
                "stick": spec}

    def _dsp_state(self):
        de = getattr(self.mem.dev, "dspemu", None)
        if de is None:
            return None
        from .c6x import dspdev
        return dspdev.state(de)

    def save(self, path, flash_path=None):
        """Checkpoint (flash bytes NOT stored; reloaded by path on load)."""
        import pickle
        st = {"flash_path": flash_path, "cpu": self.cpu.__getstate__(),
              "mem": {"ram0": bytes(self.mem.ram0), "ram1": bytes(self.mem.ram1),
                      "fb": [bytes(x) for x in self.mem.fb],
                      "misc": bytes(self.mem.misc),
                      "sq": bytes(self.mem.sq), "fb_writes": self.mem.fb_writes,
                      "low_is_ram": self.mem.low_is_ram},
              "dev": {"reg": dict(self.mem.dev.reg),
                      "touched": set(self.mem.dev.touched),
                      "tx": [list(x) for x in self.mem.dev.tx_log],
                      "rx": [list(x) for x in self.mem.dev.rx],
                      "pending": set(self.mem.dev.pending),
                      "cyc": self.mem.dev.cyc,
                      "tmu": {b: {c: dict(s) for c, s in u.items()}
                              for b, u in self.mem.dev.tmu.items()},
                      "tstr": dict(self.mem.dev.tstr),
                      "cm": (self.mem.dev.cmstr, self.mem.dev.cmcsr,
                             self.mem.dev.cmcnt, self.mem.dev.cmcor,
                             self.mem.dev.cm_acc),
                      "dma": {b: {"dmaor": v["dmaor"],
                                  "ch": {c: dict(s) for c, s in v["ch"].items()}}
                              for b, v in self.mem.dev.dma.items()},
                      "fb_base": self.mem.dev.fb_base,
                      "imr": dict(self.mem.dev.imr),
                      "cs6": dict(self.mem.dev.cs6),
                      "dspmem": dict(getattr(self.mem.dev, "dspmem", {})),
                      "dsp": self._dsp_state(),
                      "tdg": self.mem.dev.tdg.state()
                      if getattr(self.mem.dev, "tdg", None) else None,
                      "usb": self._usb_state()},
              "excs": list(self.excs), "exc_total": self.exc_total,
              "excs": list(self.excs), "exc_total": self.exc_total,
              "excs_first": list(self.excs_first),
              "dma_log": list(self.mem.dev.dma_log)}
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(st, f, protocol=4)
        os.replace(tmp, path)  # atomic: a kill never corrupts the checkpoint

    @classmethod
    def _usb_restore(cls, dv, u):
        """Rebuild the USB host + stick from _usb_state() (None-safe)."""
        if u and not u.get("stick") and u.get("r"):
            from emu.usbhost import USBHost
            usb = USBHost(dv, disk=None)              # empty port, host state kept
            usb.r = dict(u["r"])
            dv.usb = usb
            return
        if not u or not (u.get("stick") or {}).get("path"):
            return
        try:
            from emu.usbhost import USBHost, MassStorage
            from emu.usbdisk import FolderDisk, ImageDisk
            spec = u["stick"]
            disk = (FolderDisk(spec["path"], spec.get("label") or "REKORDBOX")
                    if spec["kind"] == "folder" else ImageDisk(spec["path"]))
            disk.written.update({int(k): bytes(v)
                                 for k, v in spec.get("overlay", {}).items()})
            usb = USBHost(dv, disk=None)
            usb.r = dict(u.get("r", {}))
            usb.pipecfg = {int(n): dict(c)
                           for n, c in u.get("pipecfg", {}).items()}
            usb.buf = {int(n): bytearray(b)
                       for n, b in u.get("buf", {}).items()}
            for n in usb.buf:
                usb.off[n] = 0
            usb.valid = {int(n): bool(v)
                         for n, v in u.get("valid", {}).items()} \
                if "valid" in u else {n: bool(b) for n, b in usb.buf.items()}
            usb.attached = u.get("attached", False)
            usb.reset_done = u.get("reset_done", False)
            usb.tre = {int(n): list(t)
                       for n, t in u.get("tre", {}).items()}
            usb.trcnt = {int(n): v for n, v in u.get("trcnt", {}).items()}
            usb.dma_armed = set(tuple(x) for x in u.get("dma_armed", []))
            usb.stick = MassStorage(disk)
            dv.usb = usb
            dv.usb_disk = disk
        except Exception:
            pass  # corrupt/old USB state: boot without the stick (as before)

    @classmethod
    def load(cls, path, flash_path=None, core=None):
        import pickle
        import collections
        from emu.sh4fpu import FPU
        if str(path).endswith(".gz"):
            # shipped snapshot: same pickle, gzip'd (136 MB -> ~11 MB)
            import gzip
            with gzip.open(path, "rb") as f:
                st = f.read()
        else:
            with open(path, "rb") as f:
                st = f.read()
        import io as _io
        # cap untrusted loads to this file's own checkpoints (same-repo use)
        d = pickle.loads(st)
        from emu import fw as F
        flash, info = F.load_flash(flash_path or d.get("flash_path"))
        m = cls.__new__(cls)
        from emu.machine import MachineMem
        m.mem = MachineMem(flash, dev_log=None)
        m.mem.ram0 = bytearray(d["mem"].get("ram0") or d["mem"].get("ram") or b"")
        if len(m.mem.ram0) < 64 * 1024 * 1024:
            m.mem.ram0 += bytearray(64 * 1024 * 1024 - len(m.mem.ram0))
        m.mem.ram1 = bytearray(d["mem"].get("ram1") or b"")
        if len(m.mem.ram1) < 64 * 1024 * 1024:
            m.mem.ram1 += bytearray(64 * 1024 * 1024 - len(m.mem.ram1))
        m.mem.ram = m.mem.ram0
        m.mem.fb = [bytearray(x) for x in d["mem"]["fb"]]
        m.mem.misc = bytearray(d["mem"]["misc"])
        # NOTE: pre-mirror checkpoints kept a separate staging array whose
        # bytes cannot be merged soundly (mirror aliasing); reboot those.
        m.mem.sq = bytearray(d["mem"]["sq"])
        m.mem.fb_writes = d["mem"]["fb_writes"]
        m.mem.fb_hook = None
        m.mem.low_is_ram = d["mem"].get("low_is_ram", False)
        m.mem.watch = None
        m.mem.watch_hit = None
        dv, dd = m.mem.dev, d["dev"]
        dv.reg = dd["reg"]
        dv.touched = dd["touched"]
        dv.tx_log = dd["tx"]
        dv.rx = dd["rx"]
        while len(dv.tx_log) < 3:  # old checkpoints predate SCIF2
            dv.tx_log.append([])
        while len(dv.rx) < 3:
            dv.rx.append([])
        dv.pending = dd["pending"]
        dv.cyc = dd["cyc"]
        dv.tmu = dd["tmu"]
        dv.tstr = dd["tstr"]
        dv.cmstr, dv.cmcsr, dv.cmcnt, dv.cmcor, dv.cm_acc = dd["cm"]
        dv.dma = dd["dma"]
        for _base in (0xFE008000, 0xFDC08000, 0xFE009000):  # old ckpts lack DMAC1
            _st = dv.dma.setdefault(_base, {"dmaor": 0, "ch": {}})
            for _ch in range(12):
                _st["ch"].setdefault(_ch, {"sar": 0, "dar": 0, "tcr": 0,
                                           "chcr": 0})
        dv.fb_base = dd["fb_base"]
        dv.imr = dd["imr"]
        dv.cs6 = dd.get("cs6", {"addr": 0, "ctrl": 0, "bulk": 0, "run": 0})
        dv.dspmem = dd.get("dspmem", {})
        if dd.get("dsp"):                  # snapshot taken with the real DSP
            from .c6x import dspdev
            if getattr(dv, "dspemu", None) is None:
                dv.dspemu = dspdev.DspDevice(dv)
                dv.cs6_py = True
                dv.hle = None
            dspdev.restore(dv.dspemu, dd["dsp"])
        if dd.get("tdg"):
            dv._tdg().restore(dd["tdg"])
        cls._usb_restore(dv, dd.get("usb"))
        m.excs = collections.deque(d.get("excs", []), maxlen=8192)
        m.exc_total = d.get("exc_total", len(m.excs))
        m.excs_first = d.get("excs_first", [])
        m.mem.dev.dma_log.extend(d.get("dma_log", []))
        m.trace = collections.deque(maxlen=8192)
        m.idle_skip = True
        m.idle_cycles = 0
        m.fault_cb = None
        m._make_cpu(core, state=d["cpu"])
        return m

    def run(self, limit=2000000, report_every=0, stop_on_fault=False,
            stop_on_watch=False, stop_on_pc=(), max_cycles=None):
        t = time.time()
        if not stop_on_fault and not stop_on_watch and not stop_on_pc:
            if max_cycles is not None and type(self.cpu).__name__ == "FastSH4":
                res = self.cpu.run(limit=limit, max_cycles=max_cycles)
            else:
                res = self.cpu.run(limit=limit)
        else:
            start = self.exc_total
            # run in slices so a halt condition stops promptly, state intact
            done = 0
            res = f"limit {limit} reached at {self.cpu.pc:#010x}"
            stop_pcs = set(stop_on_pc)
            cpu = self.cpu
            orig_trace = cpu.trace
            from emu.sh4 import PcHalt

            def tracing(pc, op):
                if pc in stop_pcs:
                    raise PcHalt(pc)
                if orig_trace is not None:
                    return orig_trace(pc, op)

            cpu.trace = tracing
            try:
                while done < limit:
                    n = min(1000000, limit - done)
                    res = self.cpu.run(limit=n)
                    done += n
                    if res.startswith("pc halt"):
                        break
                    if stop_on_fault and self.exc_total > start:
                        res = (f"FAULT HALT at {self.cpu.pc:#010x} "
                               f"after {done:,} steps")
                        break
                    if stop_on_watch and self.mem.watch_hit is not None:
                        a, o, pc, v, r4, r5, r6 = self.mem.watch_hit
                        res = (f"WATCH HALT: write {v:#04x} to {a:#010x} "
                               f"(ram+{o:#x}) from pc={pc:#010x} "
                               f"r4={r4:#010x} r5={r5:#010x} r6={r6:#010x} "
                               f"after {done:,} steps")
                        break
                    if stop_on_watch and self.mem.dma_hit is not None:
                        base, ch, src, dst, n, pc, pr, r14, r7, sp = \
                            self.mem.dma_hit
                        res = (f"DMA HALT: ch{ch} {n} B {src:#010x} -> {dst:#010x} "
                               f"from pc={pc:#010x} pr={pr:#010x} "
                               f"r14={r14:#010x} r7={r7:#010x} sp={sp:#010x} "
                               f"after {done:,} steps")
                        break
            finally:
                cpu.trace = orig_trace
        dt = time.time() - t
        return res, dt

    def pc_histogram(self, top=12):
        c = collections.Counter(pc for pc, _ in self.trace)
        return c.most_common(top)

    def report(self):
        lines = [f"pc={self.cpu.pc:#010x} steps={self.cpu.steps:,} "
                 f"excs={self.exc_total} fb_writes={self.mem.fb_writes}"]
        lines.append("top PCs (poll loops show up here):")
        for pc, n in self.pc_histogram():
            lines.append(f"  {pc:#010x} x{n}")
        if self.excs:
            lines.append("last exceptions (spc, vector, code):")
            for spc, off, code in list(self.excs)[-8:]:
                lines.append(f"  spc={spc:#010x} vec+{off:#x} code={code:#x}")
        if self.mem.dev.fb_base is not None:
            lines.append(f"LCDC framebuffer base: {self.mem.dev.fb_base:#010x}")
        tx = self.mem.dev.tx_log
        if tx[0] or tx[1]:
            lines.append(f"SCIF TX0: {len(tx[0])} B  TX1: {len(tx[1])} B")
        return "\n".join(lines)


def boot(flash_path=None, limit=2000000, trace_len=8192, dev_log=None):
    from emu import fw as F
    flash, info = F.load_flash(flash_path)
    if flash is None:
        raise SystemExit(f"no firmware: {info.get('error')}")
    print(f"loaded {info.get('source')} ({len(flash):,} B)")
    m = Machine(flash, trace_len=trace_len, dev_log=dev_log)
    res, dt = m.run(limit=limit)
    print(f"{res}  ({m.cpu.steps / max(dt, 1e-9):,.0f} steps/s)")
    print(m.report())
    return m


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--upd", default=None)
    ap.add_argument("--limit", type=int, default=2000000)
    a = ap.parse_args()
    boot(a.upd, limit=a.limit, dev_log=print)
