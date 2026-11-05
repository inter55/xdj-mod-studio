"""The XDJ-700 emulator the app shows, running on QEMU (docs/EMU_REBUILD_PLAN.md).

QEMU = cdj-nxs2-qemu (GPL-2.0-or-later) plus our patch qemu/0001-*.patch,
run as a separate program. It runs Pioneer's firmware and DSP program at
real time; this module keeps everything the user touches from emu/ui.py
(deck artwork, mouse / keys / knob / jog / touch handling) and swaps the
inside:

  screen  QEMU's VNC server on a unix socket (RFB, raw encoding) ->
          screen_surf, only when it changed
  input   the deck's own panel-report bytes, sent to QEMU's panel key
          socket ("off:val:dur:op" datagrams): buttons hold/rel, touch and
          jog/tempo as 16-bit fields (op lvl16, ours), the browse knob as a
          RAM counter (op m16add, ours) -- byte map = emu/panl.py
  stick   the user's library FOLDER over NBD (tools/qemu_stick_nbd.py:
          our FolderDisk, FAT32, any size, nothing copied)
  sound   QEMU plays it itself (CoreAudio / Windows audio)

Where things are (env overrides; defaults = the dev spike layout):
  XDJ_QEMU_BIN      qemu-system-sh4 with the xdj700 machine
  XDJ_QEMU_KERNEL   main_unpacked.bin (the firmware image, from the user's UPD)
  XDJ_QEMU_DSP      the curated DSP module m.so (optional: slower without)
"""
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

import numpy as np
import pygame

from . import panl
from .ui import EmuApp, SCR_W, SCR_H, SCR_X, SCR_Y, INK, DIM

HOME = os.path.expanduser("~")
DSP_IDLE = "0xC004C8CC:0x11804AE0:0x11805C00"        # models/xdj700.conf
MODE = 0x0B6C4264
EXE = ".exe" if os.name == "nt" else ""


def _first(*paths):
    for p in paths:
        if p and os.path.isfile(p):
            return p
    return paths[-1]


def qemu_bin():
    """The app's own copy (tools/bundle_qemu.sh) first, then a dev build."""
    from .paths import repo_root
    return _first(os.environ.get("XDJ_QEMU_BIN"),
                  os.path.join(repo_root(), "qemu-bin", "qemu-system-sh4" + EXE),
                  os.path.join(HOME, "qemu-build-fast", "qemu-system-sh4" + EXE),
                  os.path.join(HOME, "qemu-build-lean", "qemu-system-sh4" + EXE),
                  os.path.join(HOME, "qemu-build-spike", "qemu-build", "qemu-system-sh4" + EXE))


def kernel_image():
    """The firmware's main program: blk1.bin from the user's own update
    (emu/fwhome.py) -- never shipped with the app."""
    from . import fwhome
    return _first(os.environ.get("XDJ_QEMU_KERNEL"), fwhome.path("blk1.bin"))


def dsp_module():
    """Native module for the audio chip's hot code. Built from the user's
    own firmware (firmware-derived, never shipped); without it QEMU runs
    the audio chip slower."""
    from . import fwhome
    return _first(os.environ.get("XDJ_QEMU_DSP"),
                  os.path.join(fwhome.home(), "dsp_m.so"),
                  os.path.join(HOME, "c14gen", "curated-xdj700", "m.so"))


def output_rate():
    """The Mac's default output device's sample rate (None elsewhere / on
    error). QEMU's CoreAudio output only ASKS the device for 44.1 kHz and
    writes to it directly; a device that stays at 48 kHz (AirPods and other
    Bluetooth outputs, many interfaces) then plays it 8.8 % too fast and
    keeps running dry (2026-11-02). Started at the device's own rate, QEMU
    converts the deck's 44.1 kHz itself."""
    if sys.platform != "darwin":
        return None
    try:
        import ctypes
        ca = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreAudio.framework/CoreAudio")

        class Addr(ctypes.Structure):
            _fields_ = [("sel", ctypes.c_uint32), ("scope", ctypes.c_uint32),
                        ("elem", ctypes.c_uint32)]

        def fourcc(t):
            return int.from_bytes(t.encode(), "big")
        dev = ctypes.c_uint32(0)
        size = ctypes.c_uint32(4)
        a = Addr(fourcc("dOut"), fourcc("glob"), 0)
        if ca.AudioObjectGetPropertyData(1, ctypes.byref(a), 0, None,
                                         ctypes.byref(size), ctypes.byref(dev)) or not dev.value:
            return None
        rate = ctypes.c_double(0)
        size = ctypes.c_uint32(8)
        a = Addr(fourcc("nsrt"), fourcc("glob"), 0)
        if ca.AudioObjectGetPropertyData(dev, ctypes.byref(a), 0, None,
                                         ctypes.byref(size), ctypes.byref(rate)):
            return None
        r = int(round(rate.value))
        return r if 8000 <= r <= 192000 else None
    except Exception:
        return None


def _free_port():
    t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    t.bind(("127.0.0.1", 0))
    p = t.getsockname()[1]
    t.close()
    return p


def _connect(addr, timeout=None):
    """addr = unix socket path, or ("127.0.0.1", port) (Windows: QEMU there
    has no unix-socket VNC/monitor/NBD, so the app uses local TCP)."""
    fam = socket.AF_INET if isinstance(addr, tuple) else socket.AF_UNIX
    s = socket.socket(fam, socket.SOCK_STREAM)
    if timeout is not None:
        s.settimeout(timeout)
    s.connect(addr)
    return s


def available():
    """True when a QEMU build and a firmware image are here."""
    return os.path.isfile(qemu_bin()) and os.path.isfile(kernel_image())


class _StickProc:
    """tools/qemu_stick_nbd.py in a child process; close() ends it (it also
    ends by itself when this process dies)."""

    def __init__(self, root, src, sock):
        script = os.path.join(root, "tools", "qemu_stick_nbd.py")
        if not os.path.exists(script):
            script += "c"                        # the app ships .pyc only
        cmd = ([sys.executable, "--run-tool"] if getattr(sys, "frozen", False)
               else [sys.executable]) + [script, src,
                                         f"tcp:{sock[1]}" if isinstance(sock, tuple) else sock]
        env = dict(os.environ, PYTHONPATH=root)
        # a stopped server leaves its socket FILE behind; the next one's
        # bind() then fails (EEXIST) and the deck gets no stick -- every
        # re-insert (the host's auto-insert after boot included) hit this
        if isinstance(sock, str):
            try:
                os.unlink(sock)
            except OSError:
                pass
        # readiness = the socket answers (in the app, a tool's prints go to
        # its log file, so never wait for a "READY" line on a pipe)
        self.log = open(os.path.join(os.path.dirname(sock) if isinstance(sock, str)
                                     else tempfile.gettempdir(), "stick.log"), "w")
        self.p = subprocess.Popen(cmd, stdout=self.log, stderr=subprocess.STDOUT, env=env)
        t0 = time.time()
        while True:                              # a big library: FAT built first
            if self.p.poll() is not None:
                raise RuntimeError("stick server stopped (see stick.log)")
            try:
                if isinstance(sock, tuple) or os.path.exists(sock):
                    _connect(sock, 1.0).close()
                    break
            except OSError:
                pass
            if time.time() - t0 > 180:
                self.close()
                raise RuntimeError("stick server did not start")
            time.sleep(0.2)

    def close(self):
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(3)
            except subprocess.TimeoutExpired:
                self.p.kill()


# ---------------------------------------------------------------- screen ---
class Rfb:
    """Minimal RFB 3.8 client: no auth, raw encoding, 32-bit pixels."""

    def __init__(self, path):
        self.s = _connect(path)
        self.lock = threading.Lock()
        self.seq = 0                      # bumps on every applied update
        self._handshake()
        self.alive = True
        threading.Thread(target=self._loop, daemon=True, name="rfb").start()

    def _rx(self, n):
        b = bytearray()
        while len(b) < n:
            p = self.s.recv(n - len(b))
            if not p:
                raise ConnectionError("vnc closed")
            b += p
        return bytes(b)

    def _handshake(self):
        self._rx(12)
        self.s.sendall(b"RFB 003.008\n")
        n = self._rx(1)[0]
        types = self._rx(n)
        if 1 not in types:
            raise ConnectionError("vnc wants a password")
        self.s.sendall(b"\x01")
        if struct.unpack(">I", self._rx(4))[0] != 0:
            raise ConnectionError("vnc refused")
        self.s.sendall(b"\x01")                               # shared
        w, h = struct.unpack(">HH", self._rx(4))
        self._rx(16)
        self._rx(struct.unpack(">I", self._rx(4))[0])         # name
        self.w, self.h = w, h
        self.fb = np.zeros((h, w, 3), np.uint8)
        # 32 bpp, depth 24, little endian, true colour, R<<16 G<<8 B
        self.s.sendall(struct.pack(">BxxxBBBBHHHBBBxxx", 0, 32, 24, 0, 1,
                                   255, 255, 255, 16, 8, 0))
        self.s.sendall(struct.pack(">BxHi", 2, 1, 0))         # raw only
        self._ask(False)

    def _ask(self, inc=True):
        self.s.sendall(struct.pack(">BBHHHH", 3, int(inc), 0, 0, self.w, self.h))

    def _loop(self):
        try:
            while True:
                t = self._rx(1)[0]
                if t == 0:                                    # update
                    self._rx(1)
                    n = struct.unpack(">H", self._rx(2))[0]
                    for _ in range(n):
                        x, y, w, h, enc = struct.unpack(">HHHHi", self._rx(12))
                        if enc == -223:                       # DesktopSize
                            self.w, self.h = w, h
                            self.fb = np.zeros((h, w, 3), np.uint8)
                            continue
                        px = np.frombuffer(self._rx(w * h * 4), np.uint8)
                        px = px.reshape(h, w, 4)
                        with self.lock:
                            self.fb[y:y + h, x:x + w] = px[:, :, 2::-1]
                    self.seq += 1
                    self._ask(True)
                elif t == 1:                                  # colour map
                    self._rx(3)
                    self._rx(6 * struct.unpack(">H", self._rx(2))[0])
                elif t == 2:                                  # bell
                    pass
                elif t == 3:                                  # cut text
                    self._rx(3)
                    self._rx(struct.unpack(">I", self._rx(4))[0])
                else:
                    raise ConnectionError(f"vnc message {t}")
        except (OSError, ConnectionError, struct.error):
            self.alive = False

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


# ------------------------------------------------------------------ app ---
class QemuApp(EmuApp):
    """EmuApp's deck, driven by a QEMU process instead of our own machine."""
    engine = "qemu"
    # QEMU's panel holds what we send until we change it and the deck sees
    # it within a frame or two: an 0.08 s tap registers (measured), so no
    # need for our own emulator's 0.25 s / 3-frame minimum press, which made
    # every click feel slow (a tap only counts on release)
    MIN_HOLD = 0.08
    MIN_TICKS = 1

    def __init__(self, headless=True):
        super().__init__(headless=headless)
        self.proc = None
        self.rfb = None
        self.tmp = tempfile.mkdtemp(prefix="xdj-qemu-")
        self.tcp = os.name == "nt" or os.environ.get("XDJ_QEMU_TCP") == "1"
        if self.tcp:
            self.mon = ("127.0.0.1", _free_port())
            self.vnc = ("127.0.0.1", _free_port())
            self.stick_sock = ("127.0.0.1", _free_port())
        else:
            self.mon = os.path.join(self.tmp, "mon.sock")
            self.vnc = os.path.join(self.tmp, "vnc.sock")
            self.stick_sock = os.path.join(self.tmp, "stick.sock")
        self.keyport = 40000 + os.getpid() % 5000
        self._ksock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # MAIN's frame to the panel MCU ("pnl <hex>", cdj_pnl_stateout):
        # the button lamps (MASTER TEMPO byte 2 bit 0x08, VINYL byte 3 bit
        # 0x04 -- EmuApp.FW_LEDS, measured on our emulator)
        self._lamp = None
        self._ssock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._ssock.bind(("127.0.0.1", 0))
        self.stateport = self._ssock.getsockname()[1]
        threading.Thread(target=self._lamps, daemon=True, name="qemu-lamps").start()
        self._stick_srv = None
        self._sn = 0                        # stick / drive id counter
        self._sent_keys = set()
        self._sent = {}                    # report offset -> last 16-bit value
        self._sent_t = {}                  # report offset -> when last sent
        self._rot_qsent = 0
        self._seen_seq = -1
        self._booted_at = None
        self._mode = None
        self._mode_t = 0.0
        self.mods_on = False
        # the audio-chip slot: live key shift or TOUCH FX (one or the other)
        self.audio_fx = "keyshift"
        self.theme_spec = None
        self._booted_key = None
        import atexit
        atexit.register(self.close)

    # The host pauses an unwatched emulator (EmuHost: tab closed): pause
    # QEMU itself, or the deck plays on unseen with its sound.
    @property
    def paused(self):
        return getattr(self, "_paused", False)

    @paused.setter
    def paused(self, v):
        v = bool(v)
        if v != getattr(self, "_paused", False):
            self._paused = v
            if getattr(self, "proc", None) is not None and self.proc.poll() is None:
                self.monitor("stop" if v else "cont", 1.0)

    def _lamps(self):
        while True:
            try:
                msg = self._ssock.recv(512).decode("ascii", "replace")
            except OSError:
                return
            if msg.startswith("pnl "):
                try:
                    self._lamp = bytes.fromhex(msg[4:].strip())
                except ValueError:
                    pass

    # ------------------------------------------------------- process ---
    def _send(self, msg):
        try:
            self._ksock.sendto(msg.encode(), ("127.0.0.1", self.keyport))
        except OSError:
            pass

    def monitor(self, line, timeout=3.0):
        """One HMP command; returns its text ('' when QEMU is not up)."""
        try:
            s = _connect(self.mon, timeout)
            out = b""
            try:
                out += s.recv(65536)              # greeting
            except socket.timeout:
                pass
            s.sendall(line.encode() + b"\n")
            end = time.time() + timeout
            while not out.rstrip().endswith(b"(qemu)") or line.encode() not in out:
                if time.time() > end:
                    break
                try:
                    p = s.recv(65536)
                except socket.timeout:
                    break
                if not p:
                    break
                out += p
            s.close()
            return out.decode("utf-8", "replace")
        except OSError:
            return ""

    def read32(self, addr):
        import re
        m = re.search(r":\s*0x([0-9a-f]+)", self.monitor(f"xp /1wx {addr:#x}").split(
            f"xp /1wx {addr:#x}")[-1])
        return int(m.group(1), 16) if m else None

    def _stick(self, path):
        """(Re)serve `path` on the stick socket; None = no stick."""
        if self._stick_srv is not None:
            self._stick_srv.close()
            self._stick_srv = None
        if not path:
            return False
        from .paths import repo_root              # NOT __file__: in the app that
        root = repo_root()                        # is its internal module folder
        # its own process: in this one the stick shared Python with the deck
        # picture / stream, and a busy moment there delayed the deck's file
        # reads -> audio underruns a few seconds into a track (2026-11-02)
        if os.environ.get("XDJ_NBD_INPROC") != "1":
            self._stick_srv = _StickProc(root, path, self.stick_sock)
            return True
        sys.path.insert(0, os.path.join(root, "tools"))
        import qemu_stick_nbd as N
        from .usbdisk import FolderDisk, ImageDisk
        disk = FolderDisk(path) if os.path.isdir(path) else ImageDisk(path)
        self._stick_srv = N.serve_background(disk, self.stick_sock)
        return True

    def _stop(self):
        if self.rfb is not None:
            self.rfb.close()
            self.rfb = None
        if self.proc is not None and self.proc.poll() is None:
            self.monitor("quit", 1.0)
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                try:
                    if os.name != "nt":
                        import signal
                        os.killpg(self.proc.pid, signal.SIGKILL)   # wrapper + QEMU
                    else:
                        self.proc.kill()
                except OSError:
                    pass
        self.proc = None
        self._booted_at = None

    def _boot(self, label, kind, path):
        """kind 'qemu' (Original) or 'qemu-mods' (With mods)."""
        self._stop()
        self.fw_name = label
        self.mods_on = kind == "qemu-mods"
        if self.mods_on and self.wave_mode not in ("rgb", "3band", "blue"):
            self.wave_mode = "rgb"
        self._booted_key = self._mods_key() if self.mods_on else None
        self.status = f"starting {label} ..."

        def work():
            try:
                self._launch()
            except Exception as e:
                self.status = f"cannot start {label}: {e}"
        threading.Thread(target=work, daemon=True).start()

    def set_mute(self, muted):
        """QEMU plays the deck's sound itself, so muting means restarting the
        machine with its audio backend off (about 2 s). The firmware, the mods
        and the stick are kept; the loaded track is not (QEMU has no runtime
        volume control)."""
        muted = bool(muted)
        if muted == getattr(self, "_muted", False):
            return True
        self._muted = muted
        if self.proc is not None:
            kind = "qemu-mods" if self.mods_on else "qemu"
            self._boot(self.fw_name, kind, None)
        return True

    def _theme_names(self):
        """(theme list for UTILITY > THEME, the one to start in). An
        unsaved Theme Builder spec joins the list under its name."""
        try:
            from .paths import repo_root
            root = repo_root()
            for d in ("tools", "mods"):
                if os.path.join(root, d) not in sys.path:
                    sys.path.insert(0, os.path.join(root, d))
            import themes as T
            import theme_switch as TS
            names = TS.default_names()
            start = self.theme if self.theme not in (None, "", "stock") else None
            if start and self.theme_spec and start not in names:
                T.THEMES[start] = self.theme_spec
                names = names + [start]
            elif start and self.theme_spec and T.THEMES.get(start) != self.theme_spec:
                T.THEMES[start] = self.theme_spec        # edited, not saved yet
            return names, (start if start in names else None)
        except Exception:
            return [], None

    def _mods_kernel(self, base):
        """The firmware image with the mods baked in (tools/permanent_build
        .mods_image: what a permanent update carries, boot copier included),
        cached next to the app's other files. Rebuilt when the base image or
        the mod builds change; takes about a second."""
        import hashlib
        from .paths import repo_root
        root = repo_root()
        tools = os.path.join(root, "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        h = hashlib.sha1()
        h.update(open(base, "rb").read())
        h.update((self.wave_mode or "rgb").encode())
        h.update(repr(self._mods_key()).encode())
        h.update(os.environ.get("XDJ_QEMU_EXTRAS", "-").encode())
        for d in ("build", "prebuilt"):
            for dp, _, fs in os.walk(os.path.join(root, d)):
                for f in sorted(fs):
                    if f.endswith((".bin", ".syms")):
                        h.update(f"{f}{os.path.getmtime(os.path.join(dp, f))}".encode())
        for f in ("permanent_build.py", "perform_pads.py", "rgb_detail.py",
                  "rgb_overview.py", "utility.py"):
            for ext in ("", "c"):
                q = os.path.join(tools, f + ext)
                if os.path.exists(q):
                    h.update(f"{f}{os.path.getmtime(q)}".encode())
        try:
            from .paths import app_support_dir
            cache = os.path.join(app_support_dir(), "qemu")
        except Exception:
            cache = self.tmp
        os.makedirs(cache, exist_ok=True)
        out = os.path.join(cache, f"kernel_mods_{h.hexdigest()[:16]}.bin")
        if not os.path.exists(out):
            import permanent_build as PB
            mode = self.wave_mode if self.wave_mode in ("rgb", "3band", "blue") else "rgb"
            other = "keyshift" if self.audio_fx == "touchfx" else "touchfx"
            ext = tuple(e for e in PB.EXTRAS if e != other)
            if os.environ.get("XDJ_QEMU_EXTRAS") is not None:      # tests
                ext = tuple(e for e in os.environ["XDJ_QEMU_EXTRAS"].split(",") if e)
            names, start = self._theme_names()
            img = PB.mods_image(open(base, "rb").read(), mode, extras=ext,
                                log=lambda m: None, themes=bool(names),
                                theme_start=start, theme_names=names)
            open(out + ".tmp", "wb").write(img)
            os.replace(out + ".tmp", out)
        return out

    # QEMU addresses for our endpoints (unix sockets, or local TCP)
    def _qchar(self, a):
        return f"tcp:{a[0]}:{a[1]}" if self.tcp else f"unix:{a}"

    def _qvnc(self):
        return f"{self.vnc[0]}:{self.vnc[1] - 5900}" if self.tcp else "unix:" + self.vnc

    def _qnbd(self):
        a = self.stick_sock
        return f"nbd:{a[0]}:{a[1]}" if self.tcp else f"nbd:unix:{a}"

    def _launch(self):
        qbin = qemu_bin()
        kernel = kernel_image()
        if self.mods_on:
            self.status = "baking the mods into the firmware image ..."
            kernel = self._mods_kernel(kernel)
        for p in (self.mon, self.vnc):
            try:
                if not self.tcp:
                    os.unlink(p)
            except OSError:
                pass
        env = dict(os.environ, CDJ_ATA="1", CDJ_PANEL_KEYSOCK=str(self.keyport),
                   CDJ_PANEL_STATESOCK=str(self.stateport),
                   # ring_ms:prefill_ms:maxlat_ms. 550 ms was too tight: while a track
                   # plays, the chip delivers audio in bursts of up to ~350 ms,
                   # so the ring either hit the cap (a 68 ms dump = a cut) or ran
                   # dry (silence). 900 ms holds a burst. TRIM_MS caps any single
                   # dump at 8 ms, so a correction can never be a jump.
                   CDJ_DSP_AUDIO=os.environ.get("CDJ_DSP_AUDIO", "1:3000:250:900"),
                   CDJ_DSP_AUDIO_TRIM_MS=os.environ.get("CDJ_DSP_AUDIO_TRIM_MS", "8"),
                   CDJ_C6747_IDLE=DSP_IDLE,
                   # the XDJ-700's audio chip runs at 150 MHz (models/
                   # xdj700.conf MODEL_DSP_MHZ); the default is 300, which
                   # doubled the emulated chip's work and ate the headroom
                   # The XDJ-700's audio chip is 150 MHz, but the emulated
                   # core executes ~170 Mcyc/s while it is decoding a track
                   # (flat out, no wall-clock throttle). The bridge turns
                   # executed cycles into audio at MHz, so at 150 it emitted
                   # 49,990 frames/s where 44,100 is real time: the host-side
                   # rate converter clamps at -4 %, then dumps the remaining
                   # 13 % in ~68 ms trims -- heard as crackle and cuts while
                   # a track plays (measured 2026-11-04 from a live run:
                   # 11,445,296 in vs 9,930,773 out, 435 trims). Pacing the
                   # model to the rate it actually runs makes that conversion
                   # 1:1; the deck's own clock stops running 13 % fast too.
                   # Idle is unaffected (measured: identical in/out, 0 drops).
                   # At the chip's own 150 MHz it produces 13 % more than
                   # real time, and the sink now takes only what the Mac reads
                   # and stops the chip when its ring is full (dsp_host.c,
                   # backpressure): the surplus costs nothing and leaves the
                   # 13 % of host time that playback needs as headroom. At
                   # 170 the chip was at 100 % and 21 % idle -- saturated --
                   # and fell 4 s behind in 5 s the moment a track played.
                   CDJ_C6747_MHZ=os.environ.get("CDJ_C6747_MHZ", "150"),
                   C66X_IDLE_ISR_FAST="1",
                   # one statistic line per 5 s of the chip's clock: how far
                   # it fell behind and how much host time it needed, so a
                   # report from a real session says why audio broke.
                   CDJ_REPORT=os.environ.get("CDJ_REPORT", "1"))
        lib = os.path.join(os.path.dirname(qbin), "libc66x.so")
        if os.path.isfile(lib):                   # the app's own copy
            env["C66X_JIT_LIB"] = lib
        dsp = dsp_module()
        if os.path.isfile(dsp):
            env["C66X_JIT"] = dsp
        sound = (os.environ.get("XDJ_DECK_SOUND", "1") != "0"
                 and not getattr(self, "_muted", False))
        # CoreAudio grants what IT wants and logs "Downsizing buffer frames"
        # when the request is larger -- and a queue that was set up for one
        # size then runs at another is what the deck's host-audio bridge
        # cannot follow: its rate converter clamps at -4.00 %, then dumps
        # thousands of frames in a "trim" (audio jumps ahead = plays fast,
        # with clicks). Ask for the 960 frames the 44.1 kHz voice actually
        # gets, so the queue is created at the size it runs with.
        # XDJ_EMU_AUDIO_DEV pins the output device: the emulator must not
        # run on a 48 kHz virtual device (Hue Sync Audio, BlackHole) --
        # switch the Mac's output or name the device here.
        cdev = ""
        qdev = os.environ.get("XDJ_EMU_AUDIO_DEV")
        if qdev:
            cdev = "out.dev=" + qdev.replace(",", " ") + ","
        share = os.path.join(os.path.dirname(qbin), "share")
        argv = [qbin] + (["-L", share] if os.path.isdir(share) else []) + ["-M", "xdj700", "-name", "XDJ-700", "-kernel", kernel,
                "-serial", "file:" + os.path.join(self.tmp, "serial.log"),
                "-display", "none", "-vnc", self._qvnc(),
                "-monitor", self._qchar(self.mon) + ",server,nowait",
                "-audio", os.environ.get("XDJ_QEMU_AUDIO") or ("coreaudio," + cdev +
                           (f"out.frequency={output_rate()}," if output_rate() and not qdev else "") +
                           "out.buffer-length=3840,out.buffer-count=8,"
                           "timer-period=5000" if sound and sys.platform == "darwin"
                           else "dsound" if sound and os.name == "nt"
                           else "none,timer-period=" + os.environ.get("XDJ_QEMU_NONE_PERIOD", "5000"))]
        if self.usb_path and self._stick(self.usb_path):
            self._sn += 1
            argv += ["-drive", f"if=none,id=stk{self._sn},file={self._qnbd()},format=raw",
                     "-device", f"usb-storage,drive=stk{self._sn},id=stick{self._sn},port=1"]
        # macOS decides a process's cores from its scheduling class, and one
        # started from a server thread gets "utility": the system parks it on
        # the efficiency cores and takes slices away, which the deck has to
        # catch up on afterwards -- audio heard as a stutter, then a slow-down.
        # The class is per process and survives exec, so a small launcher sets
        # it and hands over to QEMU unchanged.
        launch = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "qemu_launch.py")
        if not os.path.exists(launch):
            launch += "c"                      # the app ships .pyc only
        if sys.platform == "darwin" and os.path.exists(launch):
            argv = (([sys.executable, "--run-tool"]
                     if getattr(sys, "frozen", False) else [sys.executable])
                    + [launch] + argv)
        log = open(os.path.join(self.tmp, "qemu.log"), "w")
        if os.name != "nt":
            # Watchdog: QEMU dies with us even on a SIGKILL (the Mac shell
            # force-quits the server after 1.5 s) -- an orphaned deck would
            # keep playing out of the speakers with no window.
            argv = ["/bin/sh", "-c",
                    'P=$PPID; "$@" & Q=$!; '
                    'while kill -0 $Q 2>/dev/null && '
                    '[ "$(ps -o ppid= -p $$ | tr -d " ")" = "$P" ]; do sleep 1; done; '
                    'kill $Q 2>/dev/null; wait $Q', "qemu-watch"] + argv
        self.proc = subprocess.Popen(argv, env=env, stdout=log, stderr=subprocess.STDOUT,
                                     start_new_session=os.name != "nt")
        t0 = time.time()
        while True:
            # unix: wait for the socket file; TCP: no file -- keep trying the
            # VNC handshake itself (a bare probe connection gets QEMU to reset
            # the next client while it is still starting)
            if not self.tcp and os.path.exists(self.vnc):
                self.rfb = Rfb(self.vnc)
                break
            if self.tcp:
                try:
                    self.rfb = Rfb(self.vnc)
                    break
                except (OSError, ConnectionError):
                    pass
            if self.proc.poll() is not None or time.time() - t0 > 20:
                raise RuntimeError("QEMU did not start (see qemu.log in " + self.tmp + ")")
            time.sleep(0.2)
        self._booted_at = time.time()
        self._sent_keys, self._sent = set(), {}
        self._rot_qsent = self.st.rotary
        self.status = ""

    # -------------------------------------------------------- screen ---
    def _update_screen(self):
        r = self.rfb
        if r is None or r.seq == self._seen_seq:
            return
        with r.lock:
            img = r.fb[:SCR_H, :SCR_W].copy()
        self._seen_seq = r.seq
        pygame.surfarray.blit_array(self.screen_surf, img.swapaxes(0, 1))

    def _boot_progress(self):
        if self.proc is None or self._booted_at is None:
            return None
        # QEMU is sending screens = booted (whether or not anyone draws them)
        if self.rfb is not None and self.rfb.seq > 2:
            return None
        return min(0.99, (time.time() - self._booted_at) / 45.0)

    def _crash(self):
        if self.proc is not None and self.proc.poll() is not None:
            return f"QEMU stopped (exit {self.proc.returncode})"
        return None

    def _draw_deck(self):
        # EmuApp._draw_deck keys its "no firmware" screen on self.machine;
        # here QEMU is the machine. Same artwork otherwise.
        import emu.deck as deck
        d = self.deck
        d.fill((24, 24, 26))
        d.blit(self._background(), (0, 0))
        crash = self._crash()
        if self.proc is None or crash:
            self.screen_surf.fill((0, 0, 0))
            msg = crash or self.status or "starting ..."
            t = self.label.render(msg, True, INK if crash else DIM)
            self.screen_surf.blit(t, (SCR_W // 2 - t.get_width() // 2, SCR_H // 2 - 10))
        else:
            self._update_screen()
        d.blit(self.screen_surf, (SCR_X, SCR_Y))
        self._leds = self._panel_leds()
        f = self._fonts()
        deck.draw_jog(d, self.st.jog_count, self.st.jog_touch, f)
        for name in self.CONTROLS:
            deck.draw_control(d, name, self._lit(name), f)
        deck.draw_rotary(d, self.st.rotary, "RotaryPush" in self.st.held)
        deck.draw_fader(d, self.st.fader_raw)
        t = self.small.render(f"{self.st.rate() * 100:+.2f}%", True, DIM)
        d.blit(t, (self.FADER.centerx - t.get_width() // 2, self.FADER.bottom + deck.S(36)))

    def _panel_leds(self):
        return self._lamp if self.proc is not None else None

    # --------------------------------------------------------- input ---
    def _lvl16(self, off, v):
        # sent on change, and again every second: a value sent while QEMU
        # was still booting is lost, and the cache would never resend it
        # (the tempo fader's "active" bit then never reached the deck)
        v &= 0xFFFF
        now = time.time()
        if self._sent.get(off) != v or now - self._sent_t.get(off, 0) > 1.0:
            self._sent[off] = v
            self._sent_t[off] = now
            self._send(f"{off:#04x}:{v}:0:lvl16")

    def inject_panl(self):
        if self.proc is None or self.proc.poll() is not None:
            return
        keys = set()
        for k in self.st.frame_keys():
            k = panl.ALIASES.get(k, k)
            if k and k in panl.BUTTONS:
                keys.add(panl.BUTTONS[k])
        for b, m in keys - self._sent_keys:
            self._send(f"{b:#04x}:{m:#04x}:0:hold")
        for b, m in self._sent_keys - keys:
            self._send(f"{b:#04x}:{m:#04x}:0:rel")
        self._sent_keys = keys
        # Tempo fader: bytes 4/5 = the fader as a 16-bit word, 6/7 = its
        # centre reference 0x8000 (pitch code 0x08D49E90: % = position
        # against the centre; centre 0 = always 0.00 %), as on the
        # CDJ-900NXS. fader_raw 0..499, 0 = top.
        # Bytes 12/13 are NOT the tempo: they are the NEEDLE SEARCH strip
        # (0..499, bit 15 = touched) -- with bit 15 set, PLAY jumps to that
        # fraction of the track (2026-11-03: the fader centre put every
        # play at mid-track). Keep it untouched.
        f = max(0.0, min(499.0, self.st.fader_raw))
        self._lvl16(4, int(round(f / 499.0 * 0xFFFF)))
        self._lvl16(6, 0x8000)
        self._lvl16(12, 0)
        tx, ty = panl.touch_raw(*self._touch_px) if self._touch_px else (1, 1)
        self._lvl16(22, tx)
        self._lvl16(24, ty)
        # jog: counter + pulse period + "moving" bit (CLAUDE.md 153)
        now = time.time()
        # bytes 10/11 = time between jog pulses: the deck bends the tempo by
        # spin speed (measured 2026-11-03: 300 ~ +-6 %, 100 ~ +-25 %). A
        # fixed 300 made every turn a weak nudge; follow the real spin rate.
        prev = getattr(self, "_jog_sent", None)
        if self.st.jog_count != prev:
            dt = max(1e-3, now - getattr(self, "_jog_t", now - 1 / 30))
            if prev is not None:
                rate = abs(self.st.jog_count - prev) / dt          # counts/s
                self._jog_rate = 0.6 * getattr(self, "_jog_rate", rate) + 0.4 * rate
            self._jog_sent, self._jog_t = self.st.jog_count, now
        moving = now - getattr(self, "_jog_t", 0) < 0.08
        period = int(max(40, min(2000, 180000 / max(getattr(self, "_jog_rate", 600), 1))))
        self._lvl16(8, 0x8000 + self.st.jog_count)
        self._lvl16(10, period if moving else 0xFFFF)
        if moving:
            self._send("0x0f:0x80:60")
        # browse knob: the u16 RAM counter, one detent per frame
        if self.st.rotary != self._rot_qsent:
            d = 1 if self.st.rotary > self._rot_qsent else -1
            self._send(f"{panl.ROTARY_COUNTER}:{d}:0:m16add")
            self._rot_qsent += d

    def _input_burst(self):
        pass                                # QEMU runs at real time

    def _press_start(self):
        pass

    def tick(self, dt):
        self.t += dt
        self.inject_panl()
        self._held_tick()

    def _sound_tick(self):
        pass

    def watch_usb(self):
        pass

    # ----------------------------------------------------------- USB ---
    def _stick_ids(self):
        """Every USB stick QEMU currently has, whatever our counter believes
        (the counter drifts across failed attempts, and deleting the wrong
        id left the old stick holding port 1 -> every re-insert failed)."""
        return [int(m) for m in re.findall(r"ID: stick(\d+)",
                                           self.monitor("info usb", timeout=2.0))]

    def _mon_until(self, token, cmd, secs=6.0, gone=False):
        """Poll one HMP command: True = token there, False = QEMU answered
        without it, None = the monitor never answered at all (a second
        connection can be refused while one is still open -- that is NOT
        evidence the stick is missing)."""
        end = time.time() + secs
        answered = False
        while time.time() < end:
            out = self.monitor(cmd, timeout=2.0)
            if out.strip():
                answered = True
                if (token not in out) if gone else (token in out):
                    return True
            time.sleep(0.25)
        return False if answered else None

    def insert_usb(self, path):
        """Hot-plug the library folder (or image) as the stick."""
        self.usb_path = path
        if self.proc is None:
            return
        self.eject_usb(keep_path=True)
        if not self._stick(path):
            return
        # a NEW id every time, and WAIT for each step: device_add on a USB
        # port that device_del has not released yet fails, and a drive_add
        # right after the NBD process starts can race its listener -- both
        # showed up as "could not attach" on a perfectly good folder
        # (2026-11-04)
        for attempt in range(2):
            live = self._stick_ids()
            self._sn = (max(live) if live else self._sn) + 1
            n = self._sn
            self.monitor(f"drive_add 0 if=none,id=stk{n},file={self._qnbd()},format=raw")
            seen = self._mon_until(f"stk{n} (", "info block", 6.0)
            if seen is False:             # QEMU answered: the drive is not there
                continue
            self.monitor(f"device_add usb-storage,drive=stk{n},id=stick{n},port=1")
            got = self._mon_until(f"ID: stick{n}", "info usb", 6.0)
            if got is not False:          # attached, or the monitor went quiet
                self.status = f"USB stick: {os.path.basename(path.rstrip('/'))}"
                return
            self.monitor(f"device_del stick{n}")
            self._mon_until(f"ID: stick{n}", "info usb", 5.0, gone=True)
        self.status = "USB stick: could not attach (try Eject + Insert)"

    def eject_usb(self, keep_path=False):
        if self.proc is not None:
            ids = self._stick_ids() or [self._sn]
            for n in ids:
                self.monitor(f"device_del stick{n}")   # its drive goes with it
            # wait for the port to be free: re-adding while QEMU is still
            # tearing the old stick down is what made attach fail sometimes
            end = time.time() + 5.0
            while time.time() < end and self._stick_ids():
                time.sleep(0.2)
        if self._stick_srv is not None:
            self._stick_srv.close()
            self._stick_srv = None
        if not keep_path:
            self.usb_path = None

    # ----------------------------------------------------------- mods ---
    def apply_wave(self, name):
        """Mods page / Theme Builder 'test in emulator': the QEMU deck runs
        the mods baked into its firmware image, so a new waveform style =
        boot the With-mods image built for it (cached). BLUE keeps the
        other mods (it is just the deck's WAVEFORM COLOR setting)."""
        if name not in ("rgb", "3band", "blue"):
            return
        self.wave_mode = name
        self._rebuild_soon()

    def apply_theme_spec(self, name, spec):
        """A theme from the app: pre-applied in the With-mods image (and
        every saved theme is on UTILITY > THEME). 'stock' = none."""
        self.theme = name or "stock"
        self.theme_spec = spec
        self._rebuild_soon()

    def set_audio_fx(self, which):
        """'keyshift' or 'touchfx': they share the audio chip's slot."""
        if which in ("keyshift", "touchfx"):
            self.audio_fx = which
            self._rebuild_soon()

    def _mods_key(self):
        return (self.wave_mode, self.theme, repr(self.theme_spec), self.audio_fx)

    def _rebuild_soon(self):
        """Several changes in a row (theme + waveform + effect) = ONE
        reboot, and none when the running image already matches."""
        self._want_gen = getattr(self, "_want_gen", 0) + 1
        gen = self._want_gen

        def later():
            time.sleep(0.6)
            if gen != self._want_gen:
                return
            if self.mods_on and self._booted_key == self._mods_key():
                return
            self._boot("Stock v1.15 + mods", "qemu-mods", None)
        threading.Thread(target=later, daemon=True).start()

    # ---------------------------------------------------------- state ---
    def _deck_mode(self):
        if self.proc is None or self._boot_progress() is not None:
            return None
        if time.time() - self._mode_t > 1.0:
            v = self.read32(MODE)
            self._mode = None if v is None else v & 0xFF
            self._mode_t = time.time()
        return self._mode

    def info(self):
        crash = self._crash()
        prog = self._boot_progress()
        state = ("error" if self.status.startswith("cannot") else
                 "loading" if self.proc is None else
                 "crashed" if crash else
                 "booting" if prog is not None else "running")
        return {"state": state, "fw_name": self.fw_name, "status": self.status,
                "crash": crash, "progress": prog, "theme": self.theme,
                "wave": self.wave_mode, "usb": self.usb_path,
                "deck_mode": self._deck_mode(), "stock": not self.mods_on,
                "real_dsp": True, "engine": "qemu", "fx": self.audio_fx,
                "muted": bool(getattr(self, "_muted", False))}

    def close(self):
        self._stop()
        if self._stick_srv is not None:
            self._stick_srv.close()
