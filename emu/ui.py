"""XDJ-700 front-end: the deck's top panel with the live firmware screen.

Everything is drawn on a fixed canvas (toolbar + deck, emu/deck.py) and scaled
to the window, so the window can be zoomed (toolbar -/+, Cmd -/+ or Cmd 0)
and resized freely; mouse positions are mapped back to canvas coordinates.

The emulator runs in a background thread. Input becomes PANL frames that
the stand-in panel MCU (sh7724._panel_reply) answers the firmware with:
physical controls and the touch screen both travel as key bits in that
frame (emu/panl.py), exactly as the real panel MCU reports them.

Toolbar: firmware menu (stock snapshot or any build/*.UPD, booted from
reset -- a mod test), zoom, save state.

Two ways to run it:
  * standalone window (`python -m emu.main`): SDL window + toolbar.
  * hosted (`EmuApp(headless=True)`, used by Mod Studio): no window and no
    toolbar. The host calls render() for the deck image and feeds input
    through pointer_down/move/up, key_event and wheel -- the very same code
    paths the window uses, so the deck behaves identically.
"""
import math
import os
import sys
import threading
import time

import numpy as np
import pygame

from . import panl

from .paths import repo_root

ROOT = repo_root()

from . import deck
from .deck import DECK_W, DECK_H, SCR_X, SCR_Y, SCR_W, SCR_H
from . import simplay

TB_H = 48                     # toolbar height (canvas px)
CAN_W, CAN_H = DECK_W, DECK_H + TB_H
FB_W, FB_H = 800, 480
ZOOMS = (0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 1.0)

# Touch-screen button regions (screen px) -> PANL key name. Taken from the
# firmware's own layout; the panel MCU reports touches on these as key bits.
TOUCH = [
    ((100, 10, 130, 56), "Browse"),
    ((234, 10, 130, 56), "Taglist"),
    ((368, 10, 130, 56), "Information"),
    ((502, 10, 130, 56), "MenuUtility"),
    ((18, 76, 68, 80), "DevRekordbox"),
    ((18, 158, 68, 78), "DevLink"),
    ((18, 238, 68, 80), "DevUSB"),
    ((18, 402, 68, 54), "SlipMode"),
    ((190, 340, 70, 54), "TimeAcue"),
    ((642, 340, 67, 55), "Sync"),
    ((715, 340, 67, 55), "Master"),
    # player screen (no track loaded / playing): hot-cue CALL and MEMORY
    ((642, 154, 69, 52), "Call_Rev"),
    ((714, 154, 68, 52), "Call_Fwd"),
    ((714, 232, 68, 54), "Memory"),
]

INK = (232, 232, 236)
DIM = (150, 152, 160)


def _make_font(name, size, bold=False):
    """pygame.font is broken on Python 3.14 (pygame 2.6.1 C ext predates it),
    so fall back to Pillow's font rendered to a Surface."""
    try:
        import importlib
        font_mod = importlib.import_module("pygame.font")
        return font_mod.SysFont(name, size, bold=bold)
    except Exception:
        pass
    from PIL import Image, ImageDraw, ImageFont
    pil = None
    for cand in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold
                 else "/System/Library/Fonts/Supplemental/Arial.ttf",
                 "/System/Library/Fonts/Helvetica.ttc"):
        try:
            pil = ImageFont.truetype(cand, size)
            break
        except Exception:
            pass
    if pil is None:
        try:
            pil = ImageFont.load_default(size=size)
        except TypeError:
            pil = ImageFont.load_default()

    class PilFont:
        def __init__(self):
            self._cache = {}

        def render(self, text, aa, color):
            key = (text, tuple(color))
            s = self._cache.get(key)
            if s is not None:
                return s
            tmp = Image.new("RGB", (8, 8))
            d = ImageDraw.Draw(tmp)
            l, t, r, b = d.textbbox((0, 0), text, font=pil)
            w, h = max(1, r - l), max(1, b - t)
            img = Image.new("RGBA", (w + 4, h + 4), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.text((2 - l, 2 - t), text, font=pil,
                   fill=tuple(color) + (255,))
            s = pygame.image.fromstring(img.tobytes(), img.size, "RGBA")
            if len(self._cache) < 1024:
                self._cache[key] = s
            return s

    return PilFont()


def rgb565_to_rgb888(buf):
    a = np.frombuffer(bytes(buf), dtype="<u2").astype(np.uint32)
    r = ((a >> 11) & 0x1F)
    g = ((a >> 5) & 0x3F)
    b = (a & 0x1F)
    r = ((r * 255 + 15) // 31).astype(np.uint8)
    g = ((g * 255 + 31) // 63).astype(np.uint8)
    b = ((b * 255 + 15) // 31).astype(np.uint8)
    return np.dstack([r.reshape(FB_H, FB_W), g.reshape(FB_H, FB_W),
                      b.reshape(FB_H, FB_W)])


from .choices import firmware_choices, theme_names  # noqa: E402,F401



def _deck_playing(m):
    """Is the deck playing audio? With the real DSP model ask the DSP: its
    status 0x11837BF8 is 1 loading, 2 playing, 3 paused (CLAUDE.md, WAV
    session 2026-10-18: the SH mode word's bit 2 means PAUSED in that line).
    The HLE stand-in keeps the old mode-word test."""
    de = getattr(m.mem.dev, "dspemu", None)
    if de is not None:
        return de.bus.read(0x11837BF8, 4) == 2
    return bool(m.mem.r32(0x0B6C4264) & 4)


def _deck_has_track(m):
    """Is anything on the deck that may need the audio chip? True unless the
    SH mode word 0x0B6C4264 is 0 (nothing loaded, nothing loading) AND the
    DSP is not playing/paused. Seen values: 0 idle, 2 loading, 3 playing,
    5 paused, 14 error/browse-over-track (CLAUDE.md §150/§151). Written as
    "everything but idle" rather than a list of known modes: a mode we have
    not seen (a second load from the browse screen over a paused track,
    say) must never freeze the DSP mid-handshake -- that is E-8302. The
    DSP's own status cannot tell idle from loading (1 for both)."""
    if m.mem.r8(0x0B6C4264) != 0:
        return True
    de = getattr(m.mem.dev, "dspemu", None)
    return de is not None and de.bus.read(0x11837BF8, 4) in (2, 3)

class DeckState:
    """Physical control state. Buttons are MOMENTARY (held while pressed);
    the firmware does any toggling, exactly as on the deck."""

    def __init__(self):
        self.held = set()          # PANL key names currently pressed
        self.fader_raw = 249.5     # 0..499 (centre = 0%)
        self.jog_count = 0
        self.jog_touch = False
        self.rotary = 0
        self.needle = 0

    def rate(self):
        v = max(0, min(499, self.fader_raw))
        return (249.5 - v) / 249.5 * 0.10

    def frame_keys(self):
        ks = set(self.held)
        if self.jog_touch:
            ks.add("JogTouch")
        return ks


# ---------------------------------------------------------------------------
# drawing helpers
# ---------------------------------------------------------------------------
def _vgrad(w, h, top, bot):
    s = pygame.Surface((w, h))
    for y in range(h):
        t = y / max(1, h - 1)
        c = [int(top[i] + (bot[i] - top[i]) * t) for i in range(3)]
        pygame.draw.line(s, c, (0, y), (w, y))
    return s


def _radial(r, inner, outer, hi=None):
    """Radial-gradient disc (with an optional soft top-left highlight)."""
    s = pygame.Surface((2 * r + 2, 2 * r + 2), pygame.SRCALPHA)
    for i in range(r, 0, -1):
        t = i / r
        c = [int(inner[k] + (outer[k] - inner[k]) * t) for k in range(3)]
        pygame.draw.circle(s, c, (r + 1, r + 1), i)
    if hi:
        g = pygame.Surface(s.get_size(), pygame.SRCALPHA)
        pygame.draw.circle(g, hi, (int(r * 0.7), int(r * 0.7)), int(r * 0.55))
        s.blit(g, (0, 0))
    return s


class EmuApp:
    PACE_CYCLES = 4_000_000      # play mode: emulated SH cycles per paced pass (8 ms)
    # play mode: the deck may run this far ahead of the wall clock, so the
    # audio consumer (whole 93 ms AudioQueue buffers) always finds a full
    # buffer waiting; pacing with no cushion stalled it on every jitter
    PACE_LEAD = float(os.environ.get("XDJ_PACE_LEAD", "0.1"))
    # geometry lives in emu/deck.py (measured from Pioneer's product photo)
    CONTROLS = deck.CONTROLS
    KEYS = {  # keyboard -> control
        "space": "PLAY", "c": "CUE", "i": "IN", "o": "OUT", "r": "RELOOP",
        "t": "MT", "g": "RANGE", "v": "VINYL", "b": "BACK", "y": "TAGTRACK",
        "[": "TRK-", "]": "TRK+", ",": "SCH-", ".": "SCH+",
    }
    JOG_C, JOG_R = deck.JOG_C, deck.JOG_R
    ROT_C, ROT_R = deck.ROT_C, deck.ROT_R
    FADER = deck.FADER

    def __init__(self, machine=None, fw_info=None, ckpt=None, headless=False):
        self.headless = headless
        if headless:
            # hosted inside Mod Studio: nothing may open a window or touch
            # the audio device
            os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
            os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
            # SDL installs its own SIGTERM/SIGINT handlers (it turns them
            # into SDL_QUIT events nobody drains here), so a served app
            # IGNORED terminate() and kept running after the Mac app quit
            # -- the ghost servers with looping audio. No handlers: the
            # default signal disposition kills the process cleanly.
            os.environ.setdefault("SDL_NO_SIGNAL_HANDLERS", "1")
        # Deck sound defaults ON (needs the HLE DSP play clock + CoreAudio;
        # XDJ_HLE_DSP=0 / XDJ_DECK_SOUND=0 disable either). SDL's dummy
        # audio driver above does not matter: decksound goes through
        # AudioToolbox directly.
        os.environ.setdefault("XDJ_HLE_DSP", "1")
        # The app's emulator runs in play mode: the audio chip on its own
        # thread, devices ticked in larger slices -- much faster, not
        # cycle-exact. Test / mod-validation tools never set it (exact).
        os.environ.setdefault("XDJ_PLAY", "1")
        # The play-mode C event loop stays ON (2026-11-01). It was switched
        # off here earlier the same day after app_speed_bench showed 17 fps
        # and a dead MENU press; the causes were (a) the idle shortcut
        # overwriting an interrupt entry (fastcpu R_IDLE, the USB-insert
        # livelock), (b) clicks held through an input burst = long presses
        # (key_event/pointer bursts now start at release), and (c) that
        # bench plays an MP3 (DSP-bound). The Python loop only looked
        # faster by starving the audio chip: 22 % of real-time audio on a
        # WAV vs 85 % with the C loop (scratchpad appaud, XDJ_PLAY).
        pygame.init()
        self.view = "deck"
        self.paused = False
        if headless:
            self.zoom = 1.0
            self.window = None
        else:
            info = pygame.display.Info()
            sw, sh = (info.current_w or 1440), (info.current_h or 900)
            fit = min((sh - 90) / CAN_H, (sw - 40) / CAN_W)
            self.zoom = max([z for z in ZOOMS if z <= fit] or [ZOOMS[0]])
            self._init_window()
        self.clock = pygame.time.Clock()
        self.small = _make_font("helvetica", 15)
        self.label = _make_font("helvetica", 16, bold=True)
        self.big = _make_font("helvetica", 26, bold=True)
        self.logo = _make_font("helvetica", 50, bold=True)
        self.canvas_deck = pygame.Surface((CAN_W, CAN_H))
        self.canvas_scr = pygame.Surface((SCR_W, SCR_H + TB_H))
        self.deck = pygame.Surface((DECK_W, DECK_H))
        self.screen_surf = pygame.Surface((SCR_W, SCR_H))
        self._bg = None
        self._fb_seen = -1
        self._fb_t = 0.0
        self.st = DeckState()
        self.t = 0.0
        self.menu_open = False
        self.realtime = True
        # Burst fast-forward: a reset boot needs only ~375M instructions to
        # draw its idle screen (verified pixel-identical to the shipped
        # snapshot from 375M on, and RAM-identical to within 6 time-keyed
        # pages of 32,768). The minutes after that are pure firmware
        # housekeeping. We therefore run the boot stretch uncapped, then
        # hand control back to real time - the USB plug (if any) is eaten
        # by the burst as well (see the end-of-burst test in _emu_loop).
        self.BURST_MARK = 400_000_000
        self._burst_usb = False      # True while a USB (not boot) burst runs
        # Mount fast-forward: renewed deadline while a stick is mid-mount.
        # Deadline-capped (not "renew only when the queue is busy"): the USB
        # service queue drains to empty at every BOT round-trip, so a busy
        # check saw "idle" ~3 times a second and the burst died seconds in --
        # then the FAT walk ran at real-deck speed (~1 min wall clock).
        # The deadline limits any single renewal; the walk paces it above.
        self.USB_BURST = 400_000_000
        # Window fast-forwarded behind every user input (see _input_burst):
        # enough to let the deck dismiss a dialog and redraw at full emu
        # speed (~0.5 s of a 500 MHz SH4), short enough that the deck
        # clock never visibly jumps.
        self.INPUT_BURST = 300_000_000
        self.burst_until = None      # steps >= this AND usb idle -> realtime
        self._disk_ops = 0           # usb ops seen at the last deadline
        self._reburst_ops = 0        # usb ops seen at the last quiet slice
        self._burst_t0 = 0.0         # wall start of the current burst window
        self.theme = "stock"
        self._leds = None
        self._stock_words = None
        self._theme_words = None
        self.status = ""
        self.fw_name = "Stock v1.15"
        self.ckpt = ckpt
        self.machine = None
        self._thread = None
        self._emu_alive = False
        self._pressed = None
        self._touch_key = None
        self._touch_px = None   # native touch: finger at screen px
        # 2026-10-18 "weird controls": a quick click (down+up between two
        # frames) never reached the deck, and one frame of touch is under
        # the panel's 3-frame debounce. Every press is now held at least
        # MIN_HOLD seconds AND MIN_TICKS injected frames before release.
        self._hold_t0 = 0.0          # pointer press start (wall)
        self._hold_ticks = 0         # frames injected since
        self._up_pending = False     # pointer release waiting for that
        self._key_t0 = {}            # key name -> (wall t0, frames)
        self._key_up_pending = set()
        self._jog_ang = None
        self.fader_drag = False
        self._ov_down = None         # screen-overlay button held
        self._mousepos = (0, 0)      # last known mouse pos (our own poll)
        self.sim = simplay.SimPlay()  # Mac-side playback display sim (SIM-badged)
        self.wave_mode = "blue"      # overview waveform style (rgb_overview cave)
        self._init_rev = 0
        self.usb_path = os.environ.get("XDJ_USB") or None
        self.usb_watch = time.time() if self.usb_path else None
        self._mute_req = False      # re-applied whenever sound re-attaches
        if machine is not None:
            self._start(machine, "Stock v1.15" if ckpt else "firmware")
            if ckpt is None and machine.cpu.steps < self.BURST_MARK:
            # Fresh reset boot handed in directly (e.g. `python -m
            # emu.main` with no snapshot yet): same fast-forward as
            # the firmware menu's boot-from-reset path.
                self.burst_until = self.BURST_MARK
                self._burst_usb = False
                self.status = ("Booting the deck (fast-forwarding the "
                               "firmware's startup)")

    # -- window / zoom ------------------------------------------------------
    # The window comes from emu/hidpi.py: a real Retina window (pygame's own
    # drops the high-DPI flag, so it drew at half resolution and macOS
    # doubled it up -- the pixelated look). The canvas is drawn at the
    # display's true pixel count with linear filtering, keeps its aspect
    # ratio (resizes snap to it), and any slack is letterboxed.
    @property
    def canvas(self):
        return self.canvas_deck if self.view == "deck" else self.canvas_scr

    def _view_size(self):
        return (CAN_W, CAN_H) if self.view == "deck" else (SCR_W, SCR_H + TB_H)

    def _init_window(self):
        from .hidpi import HiDPIWindow
        w, h = self._view_size()
        icon = None
        try:
            icon = pygame.transform.smoothscale(
                deck.load_image(os.path.join(os.path.dirname(__file__),
                                             "icon_1024.png")), (256, 256))
        except Exception:
            pass
        self.window = HiDPIWindow("XDJ-700 emulator",
                                  (int(w * self.zoom), int(h * self.zoom)), icon)

    def _apply_zoom(self):
        w, h = self._view_size()
        self.window.size = (int(w * self.zoom), int(h * self.zoom))

    def _zoom_step(self, d):
        i = min(range(len(ZOOMS)), key=lambda k: abs(ZOOMS[k] - self.zoom))
        self.zoom = ZOOMS[max(0, min(len(ZOOMS) - 1, i + d))]
        self._apply_zoom()

    def _snap_to_aspect(self):
        """Keep the deck's shape: resize the window to the canvas aspect."""
        cw, ch = self._view_size()
        ww, wh = self.window.size
        z = min(ww / cw, wh / ch)
        target = (int(round(cw * z)), int(round(ch * z)))
        self.zoom = z
        if abs(target[0] - ww) > 2 or abs(target[1] - wh) > 2:
            self.window.size = target

    def set_view(self, view):
        if view == self.view:
            return
        self.view = view
        self.menu_open = False
        self._apply_zoom()

    def present(self):
        self.window.present(self.canvas)

    def _to_canvas(self, pos):
        return self.window.to_canvas(pos)

    # -- emulation thread ----------------------------------------------------
    def _start(self, machine, name):
        self._stop()
        self.theme = "stock"
        self._theme_words = None
        self.machine = machine
        self.fw_name = name
        # A restored snapshot can already carry an ATTACHED stick; adopt
        # it into the GUI state so the toolbar offers "eject" instead of a
        # desynced "insert" (clicking insert with a stick in replaced the
        # host's stick mid-enumeration -> nothing mounted, "cancelled").
        if not os.environ.get("XDJ_USB"):
            self.usb_path = None        # a fresh machine has no stick yet
            self.usb_watch = None
        try:
            usb = getattr(machine.mem.dev, "usb", None)
            stick = getattr(usb, "stick", None) if usb else None
            if stick is not None and usb.attached and not self.usb_path:
                disk = getattr(stick, "disk", None)
                root = (getattr(disk, "root", None)
                        or getattr(disk, "path", None))
                # Adopt only a stick that actually exists: a snapshot
                # restored where its folder no longer lives must NOT
                # claim the USB state, or the auto-insert never brings
                # the user's own library (stuck showing a dead name).
                if root and os.path.exists(root):
                    self.usb_path = root
                    self.usb_watch = None
                elif stick is not None:
                    # ... and DETACH the dead one at the machine level
                    # before the deck runs: the firmware mounted that
                    # stick when the snapshot was saved, and booting it
                    # against vanished files made the deck throw
                    # "rekordbox Database not found!" / "media collapsed"
                    # at the next swap. Unplugged here, the deck sees a
                    # clean removal at resume; the auto-insert then
                    # brings the real library with no ghost to yank.
                    usb.unplug()
                    usb.stick = None
                    machine.mem.dev.usb_disk = None
        except Exception:
            pass
        self._fb_seen = -1
        self._emu_alive = True
        self._thread = threading.Thread(target=self._emu_loop, daemon=True)
        self._thread.start()

    def _stop(self):
        self._emu_alive = False
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def _emu_loop(self):
        """Run the firmware in real time: the LCDC frame counter (60 Hz of
        emulated time) is held to the wall clock, so the deck is never
        faster than a real one. Behind (a slow machine), it just runs flat
        out."""
        m = self.machine
        dev = m.mem.dev
        t0, f0 = time.time(), getattr(dev, "frames", 0)
        play = os.environ.get("XDJ_PLAY") == "1"
        try:
            while self._emu_alive and m is self.machine:
                if self.paused and not self.bursting():
                    time.sleep(0.05)     # Studio: tab closed (burst still runs)
                    t0, f0 = time.time(), getattr(dev, "frames", 0)
                    continue
                # 1M slices both ways: a 300k slice spends a big share of
                # its wall time in this loop's Python overhead, and the
                # un-burst path only reached ~5-15% of deck speed -- every
                # firmware-side wait (dialog timers, handshakes) then took
                # tens of seconds of wall clock. Pacing is unchanged: the
                # loop still sleeps to the 60 Hz frame clock.
                # Play mode runs faster than real time (2026-11-01) and its
                # sleep jumps make 1M steps up to 0.6 s of emulated time:
                # cap a pass at ~8 ms so the pacing below holds the deck
                # (and its audio) to the wall clock.
                cap = (self.PACE_CYCLES if play and self.realtime
                       and not self.bursting() else None)
                m.run(limit=1_000_000, max_cycles=cap)
                self._burst_sound(m, dev)
                if self.burst_until is not None and m.cpu.steps >= self.burst_until:
                    try:
                        u = dev._usb()
                    except Exception:
                        u = getattr(dev, "usb", None)
                    # Track-load reads too: the deck reads 10k+ sectors when
                    # the user loads a track -- if the burst deadline lands
                    # exactly on a BOT round-trip gap (due == None, stick
                    # attached), the old check dropped to deck speed and the
                    # load took a minute of wall clock. So also renew while
                    # the stick is actively READING (a real block of reads in
                    # the last burst window; a wall ceiling stops runaway).
                    now = time.time()
                    disk_active = False
                    # set before the disk check: with NO stick inserted the
                    # first input burst to time out crashed the whole
                    # emulator loop here (UnboundLocalError) -- the deck
                    # then ignored every later click (2026-11-01)
                    playing = False
                    disk = getattr(dev, "usb_disk", None)
                    if disk is not None:
                        # ops is a MONOTONIC IO counter: the access ring
                        # caps at ~20k entries, so len() went flat during
                        # long reads and every renewal check saw zero
                        # growth mid-load (a 13 GB load streams tens of
                        # thousands of sectors). Older disks only have
                        # the ring; fall back to its length there.
                        ops = getattr(disk, "ops", None)
                        if ops is None:
                            ops = len(disk.access) if hasattr(disk,
                                                              "access") else 0
                        grown = ops - self._disk_ops
                        self._disk_ops = ops
                        self._reburst_ops = ops
                        # never fast-forward a PLAYING track: the Mac audio
                        # consumer is the real-time clock then, so running
                        # the machine uncapped makes the deck's own TIME
                        # race ahead of what the user hears
                        playing = False
                        try:
                            playing = _deck_playing(m)
                        except Exception:
                            pass
                        disk_active = (grown >= 1024 and not playing
                                       and now - self._burst_t0 < 240)
                        if playing:
                            disk = None        # only the attach check below
                    if self.burst_until is None:
                        self._burst_t0 = now
                    if u is not None and not playing and (
                            u.due is not None or disk_active or (
                                u.stick is not None and not u.attached)):
                        # A stick is plugged or mid-enumeration/mount:
                        # keep fast-forwarding (deadline-capped renewals) so
                        # the FAT walk takes seconds, not a minute of wall
                        # clock. Renew on the settle wait too, not only when
                        # the queue is busy: the queue drains to empty at
                        # every BOT round-trip, so a busy-only check killed
                        # the burst ~1 s in and the walk fell to deck speed.
                        self.burst_until = m.cpu.steps + self.USB_BURST
                    else:
                        self.burst_until = None
                        if self.status.startswith("Booting"):
                            self.status = ""
                elif self.burst_until is None:
                    # RE-ENTRY: the deadline branch above only EXTENDS an
                    # existing burst, so a track load starting from a quiet
                    # idle (the deck settled minutes ago, the user scrolls
                    # and pushes = thousands of READ10 sectors) ran at deck
                    # speed with nothing able to reopen the burst. Watch the
                    # disk every slice; a real read surge re-opens one.
                    self._reburst(m, dev)
                if not self.realtime or self.bursting():
                    # Re-anchor the pacing clock while fast-forwarding:
                    # otherwise the frames a burst raced ahead count as
                    # "ahead of the wall clock" once it ends and the loop
                    # SLEPT the deck until real time caught up -- after
                    # every click and every USB read the deck froze for
                    # as long as it had just skipped (5-9 fps measured
                    # browsing, 2026-11-01; "USB takes far too long").
                    t0, f0 = time.time(), getattr(dev, "frames", 0)
                    continue
                ahead = (getattr(dev, "frames", 0) - f0) / 60.0 - (time.time() - t0)
                if play:
                    ahead -= self.PACE_LEAD   # audio cushion (see PACE_LEAD)
                if ahead > 0.005:
                    time.sleep(min(ahead, 0.05))
                elif ahead < -0.5:           # fell behind: don't try to catch up
                    t0, f0 = time.time(), getattr(dev, "frames", 0)
        except Exception as e:
            self.status = f"emulator stopped: {e}"

    def _boot(self, label, kind, path):
        """Switch firmware. Snapshots resume instantly; a .UPD boots from
        reset (the mod test)."""
        self._stop()
        self.machine = None
        self.status = f"loading {label} ..."

        def work():
            from .machine import Machine
            from . import fw as F
            try:
                if kind in ("ckpt", "ckpt-dsp"):
                    try:
                        m = Machine.load(path)
                        self.ckpt = path
                    except Exception as e:
                        if kind != "ckpt-dsp":
                            raise
                        # Real-DSP snapshot unreadable (partial download,
                        # interrupted copy): fall back to the instant
                        # snapshot instead of stranding the user. Tracks
                        # won't play there (E-8302), but everything else
                        # works; re-running firmware setup repairs it.
                        # (Separate names: `label` is _boot's parameter and
                        # must never be rebound in here.)
                        from .choices import firmware_choices
                        plain = [c for c in firmware_choices()
                                 if c[1] == "ckpt"]
                        if not plain:
                            raise
                        m = Machine.load(plain[0][2])
                        self.ckpt = plain[0][2]
                        self._start(m, plain[0][0])
                        self.status = (f"DSP snapshot unreadable ({e}); "
                                       f"started {plain[0][0]} instead")
                        self.burst_until = None
                        return
                else:
                    flash, info = F.load_flash(path)
                    if flash is None:
                        self.status = f"cannot load {label}: {info.get('error')}"
                        return
                    m = Machine(flash)
                    self.ckpt = None     # never autosave a mod over the snapshot
                if kind in ("ckpt", "ckpt-dsp"):
                    self.status = ""
                    self.burst_until = None
                else:
                    # Reset boot: fast-forward the firmware's own startup
                    # real-time stretch (BURST_MARK in _emu_loop); the
                    # user sees the idle screen in seconds, not minutes.
                    self.burst_until = self.BURST_MARK
                    self._burst_usb = False
                    self.status = ("Booting the deck (fast-forwarding the "
                                   "firmware's startup)")
                self._start(m, label)
            except Exception as e:
                self.status = f"cannot load {label}: {e}"
        threading.Thread(target=work, daemon=True).start()

    def save_state(self):
        m = self.machine
        if m is None or not self.ckpt:
            self.status = "save: only the stock snapshot session is saved"
            return
        self._stop()
        m.save(self.ckpt)
        self.status = f"saved {os.path.basename(self.ckpt)}"
        self._start(m, self.fw_name)

    # -- waveform styles ----------------------------------------------------
    def apply_wave(self, name):
        """Overview waveform style: blue (stock) / rgb / 3band. Installs the
        tools/rgb_overview.py cave + hook (same bytes as the deck tool) and
        sets its mode byte. Takes effect on the next track load (the stock
        painter only redraws the overview when a track loads)."""
        m = self.machine
        if m is None or not self.fw_name.startswith("Stock"):
            self.status = "waveform styles need the stock firmware running"
            return
        import sys as _sys
        _sys.path.insert(0, os.path.join(ROOT, "tools"))
        import rgb_overview as RO
        code, writes = RO.plan(RO.MODES[name])
        hook = m.mem.r32(RO.HOOK_POOL)
        if hook not in (RO.ORIG_RELEASE, RO.CAVE):
            self.status = f"waveform hook site unexpected ({hook:#x}) - not applied"
            return
        for a, b in writes:
            m.mem.w8(a, b)
        # centre (detail) waveform too: same mode byte; snapshot routine is
        # part of `writes` (RO.extra_writes)
        import rgb_detail as RD
        if m.mem.r32(RD.POOL) in (RD.PALETTE, RD.DCAVE) and \
                m.mem.r16(RD.INSN) in (RD.INSN_OLD, RD.INSN_NEW):
            RD.install(m.mem)            # C colour module + shim + row patch
        # the rest of the deck set: version switching + the mod settings in
        # UTILITY (same bytes as tools/deck_session.py installs; 1.0: no
        # MY SETTINGS rows any more)
        import vredirect as VR
        if m.mem.r32(VR.HOOK) == VR.FREAD:
            for a, b in VR.plan():
                m.mem.w8(a, b)
        try:                          # mod settings in UTILITY (hold MENU)
            import utility as UT      # (tools/utility_apply.py, step 5u)
            if m.mem.r32(UT.POOLS[0]) == UT.STOCK_TAB:
                UT.install(m.mem)
        except Exception:
            pass
        # PERFORM screen pads (HOT CUE / BEAT JUMP / KEY SHIFT / STEMS tabs),
        # same bytes as tools/perform_pads_apply.py (deck_session step 5d)
        import perform_pads as PP
        if all(m.mem.r32(p) == old for p, old, new in PP.hooks()):
            PP.install(m.mem)
        self._install_light(m)         # UNDER pm/hc: lt_fill rewrites their
        self._install_phasemeter(m)   # fill colours too
        self._install_hotcue8(m)
        self._install_themesw(m)
        self._install_stems(m)        # deck order: 5g stems, 5h FLAC, 5k key
        self._install_flac(m)
        self._install_keyshift(m)
        self._arm_light(m)            # pads' PDKW + pm_light need their caves
        self.wave_mode = name
        self.status = (f"waveform: {name} - load a track to see it; "
                       f"hold MENU > UTILITY = mod settings"
                       + (" (3-band needs PWV6: run tools/stick_add_3band.py)"
                          if name == "3band" else ""))

    def _install_light(self, m):
        """Light-mode add-on (tools/lighttext, 2026-10-26): lt.c/ltentry.S
        wrappers for the title blender, waveform windows + colours, and the
        flush-chain fill rewrite -- chained FIRST (under the phase meter /
        hot cues) so lt_fill also rewrites their list units. Arms the pads'
        ink-flip word (PDKW) and the phase meter's pm_light with LIGHT's
        number in the theme switcher; both follow the switcher's current
        theme byte on their own. Skipped quietly when not built."""
        d = os.path.join(ROOT, "build", "lighttext")
        try:
            syms = {}
            for line in open(os.path.join(d, "lt.syms")):
                a, _, n = line.split()
                syms[n.lstrip("_")] = int(a, 16)
            img = open(os.path.join(d, "lt.bin"), "rb").read()
        except OSError:
            return
        import theme_switch as TS
        try:
            idx = TS.default_names().index("light") + 1
        except ValueError:
            return
        TEXT_POOLS = (0x08C2F8FC, 0x08C31FC4)
        MSET_POOLS = (0x08DC3B44, 0x08DC2808, 0x08DBC3FC)   # +overview strip
        RGB_POOLS = (0x08DC2A24, 0x08DC2C74, 0x08DC2EA4, 0x08DC31A0, 0x08DC32B0,
                     0x08DC3DA8, 0x08DC3FF4, 0x08DC417C, 0x08DC4470, 0x08DC4758,
                     0x08DC4A24)
        live = m.mem.r32(syms["lt_head"])
        if live != 0x5448474C:                     # not ours yet
            if any(m.mem.r8(a) for a in range(syms["lt_head"],
                                              syms["lt_head"] + len(img), 64)):
                return                             # cave in use
            for i, b in enumerate(img):
                m.mem.w8(syms["lt_head"] + i, b)
            for p in TEXT_POOLS:
                if m.mem.r32(p) == 0x08C2F5C2:
                    m.mem.w32(p, syms["lt_entry"])
            for p in MSET_POOLS:
                if m.mem.r32(p) == 0x08E1D954:
                    m.mem.w32(p, syms["lt_mset"])
            for p in RGB_POOLS:
                if m.mem.r32(p) == 0x08DB6CD4:
                    m.mem.w32(p, syms["lt_rgb"])
            m.mem.w32(syms["lt_prev"], m.mem.r32(0x08C92E20))
            m.mem.w32(0x08C92E20, syms["lt_flush"])
        m.mem.w32(syms["lt_idx"], idx)
        self._lt_syms = syms

    def _arm_light(self, m):
        """Light add-on's second half: arm the pads' ink-flip word (PDKW)
        and the phase meter's pm_light with LIGHT's number -- AFTER their
        modules are installed (their installers reset those words)."""
        syms = getattr(self, "_lt_syms", None)
        if syms is None:
            return
        try:
            import theme_switch as TS
            idx = TS.default_names().index("light") + 1
        except ValueError:
            return
        import perform_pads as PP
        if m.mem.r32(PP.PDKW) in (0, PP.PDKSTUB, syms["lt_pdk"]):
            m.mem.w32(PP.PDKW, syms["lt_pdk"])
        pd = os.path.join(ROOT, "build", "phasemeter")
        try:
            for line in open(os.path.join(pd, "pm.syms")):
                a, _, n = line.split()
                if n.lstrip("_") == "pm_light":
                    if m.mem.r32(int(a, 16)) in (0, idx):
                        m.mem.w32(int(a, 16), idx)
                    break
        except OSError:
            pass

    def _install_phasemeter(self, m):
        """Type 1 phase meter (tools/phasemeter, C): same bytes as the deck
        installer, chained after the PERFORM pads on the 2DG flush hook.
        Skipped quietly when not built or the cave area is in use."""
        d = os.path.join(ROOT, "build", "phasemeter")
        try:
            syms = {}
            for line in open(os.path.join(d, "pm.syms")):
                a, _, n = line.split()
                syms[n.lstrip("_")] = int(a, 16)
            img = open(os.path.join(d, "pm.bin"), "rb").read()
        except OSError:
            return
        hook, base = 0x08C92E20, syms["pm_entry"]
        live = m.mem.r32(hook)
        if live == base:
            return
        end = max(syms["pm_mode"], syms["pm_n"], syms.get("pm_layer", 0)) + 4
        if any(m.mem.r8(a) for a in range(base, end, 16)):
            return
        for i, b in enumerate(img):
            m.mem.w8(base + i, b)
        m.mem.w32(syms["pm_prev"], live)
        m.mem.w8(syms["pm_mode"], 1)
        m.mem.w32(hook, base)

    def _install_hotcue8(self, m):
        """8 hot cue pages (tools/hotcue8, C): same bytes as
        hotcue8_apply.py, chained last on the 2DG flush hook (needs the
        PERFORM pads, which call hc_next from their HOT CUE tab)."""
        d = os.path.join(ROOT, "build", "hotcue8")
        try:
            syms = {}
            for line in open(os.path.join(d, "hc.syms")):
                a, _, n = line.split()
                syms[n.lstrip("_")] = int(a, 16)
            img = open(os.path.join(d, "hc.bin"), "rb").read()
        except OSError:
            return
        hook, base = 0x08C92E20, syms["hc_table"]
        live = m.mem.r32(hook)
        if live == syms["hc_entry"]:
            return
        if any(m.mem.r8(a) for a in range(base, syms.get("end", syms["hc_count"] + 4), 16)):
            return
        for a in range(base, syms.get("end", syms["hc_count"] + 4), 4):
            m.mem.w32(a, 0)
        for i, b in enumerate(img):
            m.mem.w8(base + i, b)
        m.mem.w32(syms["hc_prev"], live)
        m.mem.w32(hook, syms["hc_entry"])
        # 8-pad view: the touch hook sits in front of the stock hit-tester
        # (the PERFORM pads' own ptouch now runs behind it)
        tpool = 0x08D5278C
        m.mem.w32(syms["hc_tprev"], m.mem.r32(tpool))
        m.mem.w32(syms["hc_armed"], 1)
        m.mem.w32(tpool, syms["hc_tentry"])

    def _install_themesw(self, m):
        """On-deck theme switching (tools/theme_switch.py, deck C): the
        prebuilt build/themesw files, same bytes as theme_switch_apply.py.
        UTILITY > THEME then picks the themes live."""
        d = os.path.join(ROOT, "build", "themesw")
        try:
            code = open(os.path.join(d, "ts.bin"), "rb").read()
            # 2026-10-25: build the tables fresh (<1 s) -- the copy fwhome
            # made at setup goes stale whenever ts.c / the maths change
            try:
                sys.path.insert(0, os.path.join(ROOT, "tools"))
                sys.path.insert(0, os.path.join(ROOT, "mods"))
                import theme_switch as TS
                data, lit = TS.build(TS.default_names())
            except Exception:
                lit = []
                from . import fwhome
                tp = os.path.join(fwhome.home(), "themesw_tables.bin")
                data = open(tp if os.path.exists(tp) else os.path.join(d, "tables.bin"), "rb").read()
            cb, db = (int(x, 16) for x in
                      open(os.path.join(d, "layout.txt")).read().split()[:2])
        except (OSError, ValueError):
            return
        if m.mem.r32(cb) == int.from_bytes(code[:4], "little"):
            return
        if any(m.mem.r8(a) for a in range(cb, db + len(data), 64)):
            return                                # area in use: leave it
        for a in range(cb, db, 4):
            m.mem.w32(a, 0)                       # work area (bss)
        for i, b in enumerate(code[4:]):
            m.mem.w8(cb + 4 + i, b)
        for i, b in enumerate(data):
            m.mem.w8(db + i, b)
        # lit buttons (a theme with a lit colour): their pictures -> private
        # colours before the switch is armed, only while they read stock
        for a, st, pv in lit:
            if bytes(m.mem.r8(a + i) for i in range(len(st))) == st:
                for i, b in enumerate(pv):
                    m.mem.w8(a + i, b)
        import mysettings as MSET
        if not m.mem.r8(MSET.MSTNAME):
            for i, b in enumerate(b"OFF\0"):
                m.mem.w8(MSET.MSTNAME + i, b)
        m.mem.w32(cb, int.from_bytes(code[:4], "little"))   # on-switch last

    def _install_stems(self, m):
        """Live stem mute/solo (tools/stems/sm.c, deck step 5g bytes): the
        prebuilt build/stems image, chained in front of the live audio read
        (version switcher or stock) + the direct read slot. The PERFORM
        STEMS pads own the mask; a mask change re-mixes the read-ahead."""
        d = os.path.join(ROOT, "build", "stems")
        try:
            syms = {}
            for line in open(os.path.join(d, "sm.syms")):
                a, _, n = line.split()
                syms[n.lstrip("_")] = int(a, 16)
            img = open(os.path.join(d, "sm.bin"), "rb").read()
        except OSError:
            return
        hook, direct, fread = 0x08D5FFA8, 0x08DDFFD4, 0x08C61A6C
        base, end, entry = syms["sm_table"], syms["end"], syms["sm_fread"]
        live = m.mem.r32(hook)
        if live == entry:
            return
        import perform_pads as PP
        if not m.mem.r32(PP.PC0):
            return                                 # pads hold the mask
        if any(m.mem.r8(a) for a in range(base, end, 64)):
            return                                 # area in use: leave it
        for a in range(base, end, 4):
            m.mem.w32(a, 0)
        for i, b in enumerate(img):
            m.mem.w8(base + i, b)
        m.mem.w32(syms["sm_prev"], live)
        m.mem.w32(hook, entry)
        if m.mem.r32(direct) == fread:
            m.mem.w32(direct, entry)
        if "sm_poll_entry" in syms and m.mem.r32(0x08C1D56C) == 0x08BF2BCC:
            m.mem.w32(0x08C1D56C, syms["sm_poll_entry"])   # fast mute trigger

    def _install_keyshift(self, m):
        """Real-time key shift (tools/keyshift_dsp/ksh.c, deck step 5k): the
        one-shot cave patches the RUNNING emulated audio chip over its host
        port on the next frame (no reset), then the BD8 store is NOPed so
        PERFORM > KEY SHIFT < > drives the key. Only with the real DSP
        model (the HLE stand-in has no DSP program to patch) and no track
        loaded (the cave refuses otherwise)."""
        if getattr(m.mem.dev, "dspemu", None) is None:
            return
        d = os.path.join(ROOT, "build", "keyshift")
        try:
            syms = {}
            for line in open(os.path.join(d, "ksh.syms")):
                a, _, n = line.split()
                syms[n.lstrip("_")] = int(a, 16)
            img = open(os.path.join(d, "ksh.bin"), "rb").read()
        except OSError:
            return
        hook, base = 0x08C92E20, syms["ksh_entry"]
        if m.mem.r32(0x0BA3C050):                  # track loaded
            return
        if m.mem.r32(syms["ksh_status"]) == 1 or m.mem.r32(hook) == base:
            return
        if any(m.mem.r8(base + i) and m.mem.r8(base + i) != b
               for i, b in enumerate(img)):
            return                                 # area in use
        live = m.mem.r32(hook)
        for i, b in enumerate(img):
            m.mem.w8(base + i, b)
        m.mem.w32(syms["ksh_prev"], live)
        m.mem.w32(syms["ksh_status"], 0)
        m.mem.w32(syms["ksh_flag"], 0x4B534831)    # 'KSH1'
        m.mem.w32(hook, base)                      # runs on the next frame
        if m.mem.r16(0x08D36B3A) == 0x1C15:        # BD8 store -> ours
            m.mem.w32(0x09D24DD8, 0x00100000)
            m.mem.w16(0x08D36B3A, 0x0009)

    def _install_flac(self, m):
        """FLAC/Opus playback (deck step 5h bytes, tools/xflac): patches the
        extension classifier ("3g2"->flac, FLAC/FLA/OPUS = WAV class) and
        installs the built virtual-WAV layer at its link address, chained to
        the live USB read slots (versions/stems stay wrapped). FLAC + Ogg
        Opus on the stick then list, analyse and play as 44.1/48 kHz WAV."""
        os.environ["VFILE_DIR"] = os.path.join(ROOT, "build", "xflac")
        import sys as _sys
        if os.path.join(ROOT, "tools") not in _sys.path:
            _sys.path.insert(0, os.path.join(ROOT, "tools"))
        try:
            import vfile_emu_test as VE
            VE.install(m.mem, VE.syms())
            self.flac = True
        except (OSError, AssertionError, KeyError) as e:
            self.flac = False
            self.status = (f"FLAC layer skipped ({e}) - a loaded track or "
                           f"another mod owns a USB slot; retry after loading "
                           f"a stock snapshot, then re-insert the stick")
            return
        # FLAC/Opus on an ALREADY-inserted stick were classified before the
        # patch: re-read the same folder so they show up as playable
        path = getattr(self, "usb_path", None)
        if path and path != "(snapshot stick)":
            self.status = "FLAC + Opus ready - re-reading your stick ..."
            self.insert_usb(path)

    def inject_panl(self):
        m = self.machine
        if m is None:
            return
        keys = self.st.frame_keys()
        # the jog counts as "turning" for a moment after each change (a
        # real jog streams pulses; one GUI drag event = one burst of them)
        now = time.time()
        if self.st.jog_count != getattr(self, "_jog_sent", None):
            self._jog_sent, self._jog_t = self.st.jog_count, now
        moving = now - getattr(self, "_jog_t", 0) < 0.08
        m.mem.dev.panel_frame = panl.build_frame(
            keys, fader_raw=self.st.fader_raw, jog=self.st.jog_count,
            jog_period=300 if moving else 0xFFFF, jog_moving=moving,
            needle=self.st.needle,
            tempo=self.st.fader_raw,
            touch=self._touch_px)
        # The browse knob is NOT a frame field: KY 0x42/0x43 (Rotary-
        # PositionFwd/Rev) bump the u16 counter at 0x0B6C7B48, which the
        # browser polls. Apply the GUI's rotary delta there directly.
        last = getattr(self, "_rot_sent", None)
        if last is None:
            self._rot_sent = self.st.rotary
        elif self.st.rotary != last:
            # ONE detent per injected frame: the browser polls the counter
            # and a jump of several steps between polls was taken as a single
            # (or accelerated) move, so quick knob turns lost detents (user,
            # 2026-11-01). A real knob sends one KY 0x42/0x43 per detent.
            a = panl.ROTARY_COUNTER
            d = 1 if self.st.rotary > last else -1
            m.mem.w16(a, (m.mem.r16(a) + d) & 0xFFFF)
            self._rot_sent = last + d

    # -- firmware screen -------------------------------------------------------
    def _update_screen(self):
        """Convert the scanned-out framebuffer when it changed (at most
        ~30/s: conversion time is time stolen from the emulator)."""
        m = self.machine
        if m is None or self.t - self._fb_t < 1 / 30:
            return
        try:
            seen = m.mem.fb_writes
            if seen == self._fb_seen:
                return
            reg = m.mem.dev.reg
            base = sum(reg.get(0xFE940430 + i, 0) << (8 * i) for i in range(4))
            idx = 1 if (base & 0x1FFFFFFF) == 0x0E83B800 else 0
            buf = bytes(m.mem.fb[idx])
            if not any(buf[::97]):
                buf = bytes(m.mem.fb[1 - idx])
            rgb = rgb565_to_rgb888(buf)
            pygame.surfarray.blit_array(self.screen_surf, rgb.swapaxes(0, 1))
            self._fb_seen = seen
            self._fb_t = self.t
        except Exception:
            pass

    def _draw_simplay(self):
        """Mac-side playback display sim (SIM-badged, never firmware pixels).

        While the firmware reports mode 5 with a loaded track, paint a
        scrolling detail strip (loaded track's own PWV5) + running time over
        the firmware screen. The DSP position word is still unfound; this
        stands in for it on a wall-clock playhead so display-side work can
        continue. Off = pure firmware pixels.
        """
        try:
            m = self.machine
            if m is None or self.sim is None:
                return
            # The real audio chip makes the firmware draw its own waveform
            # and time. SimPlay keyed on mode 5, which with the real chip
            # means PAUSED -- it painted a made-up waveform + clock over a
            # paused deck, even over menus (user, 2026-11-01). Only the old
            # no-playback stand-in ever needs it.
            if getattr(m.mem.dev, "dspemu", None) is not None:
                return
            track, pos = self.sim.update(m, self.usb_path, time.time())
            if track is None:
                return
            s = self.screen_surf
            # detail strip (firmware detail-wave area; keeps beat-grid bars)
            x0, x1, y0, y1 = 96, 640, 195, 334
            mid = (y0 + y1) // 2
            amp = (y1 - y0) // 2 - 6
            pygame.draw.rect(s, (0, 0, 0), (x0, y0, x1 - x0, y1 - y0))
            ncols = max(track.ncols, 1)
            base = pos / max(track.duration, 0.001) * ncols
            head = x0 + int((x1 - x0) * 0.30)
            for x in range(x0, x1):
                h = track.height01(int(base + (x - head))) * amp
                if h >= 1:
                    pygame.draw.line(s, (80, 168, 248),
                                     (x, mid - h), (x, mid + h))
            pygame.draw.line(s, (200, 200, 200), (x0, mid), (x1, mid))
            pygame.draw.line(s, (255, 255, 255), (head, y0), (head, y1))
            tag = self.small.render("SIM", True, (150, 152, 160))
            s.blit(tag, (x1 - tag.get_width() - 4, y0 + 2))
            # running time over the firmware's dashes
            t = self.label.render(simplay.SimPlay.fmt(pos), True,
                                  (232, 232, 236))
            pygame.draw.rect(s, (0, 0, 0), (280, 395, 135, 60))
            s.blit(t, (410 - t.get_width(), 410))
        except Exception:
            pass

    def bursting(self):
        """Inside the reset-boot fast-forward window (also while a stick
        mount rides it)."""
        m = self.machine
        return (self.burst_until is not None and m is not None and
                m.cpu.steps < self.burst_until)

    def _boot_progress(self):
        """Reset-boot progress for the GUI: the deck draws its idle screen
        ~375M instructions in and we fast-forward that stretch, so the bar
        tracks the burst window (BURST_MARK), not wall-clock guesses."""
        m = self.machine
        if m is None:
            return None
        try:
            if m.mem.fb_writes > 0:
                return None
            return min(0.99, m.cpu.steps / self.BURST_MARK)
        except Exception:
            return None

    def _crash(self):
        """A build that faults before drawing anything: what a bricked deck
        does (black screen). Returns a description or None."""
        m = self.machine
        try:
            if m is None or m.mem.fb_writes > 0 or m.exc_total < 20:
                return None
            spc, off, code = m.excs_first[0][:3]
            kind = {0x0E0: "address error (read)", 0x100: "address error (write)",
                    0x180: "illegal instruction", 0x1A0: "illegal slot instruction"
                    }.get(code, f"exception {code:#x}")
            return f"{kind} at {spc:#010x}"
        except Exception:
            return None

    # -- deck drawing (emu/deck.py) ------------------------------------------------
    def _fonts(self):
        return (self.small, self.label, self.big, self.logo)

    def _background(self):
        if self._bg is None:
            self._bg = deck.background(self._fonts())
        return self._bg

    # LEDs the firmware drives, from the main->panel frame (the one the real
    # panel MCU uses to light the buttons). Measured by toggling each mode:
    # MASTER TEMPO = byte 2 bit 0x08; VINYL = byte 3 bit 0x04 (0x02 = CDJ).
    FW_LEDS = {"MT": (2, 0x08), "VINYL": (3, 0x04)}

    def _panel_leds(self):
        m = self.machine
        try:
            t = m.mem.dev.tx_log[2]
            return bytes(t[-32:]) if len(t) >= 32 else None
        except Exception:
            return None

    def _lit(self, name):
        key = self.CONTROLS[name][3]
        if (key or name) in self.st.held:
            return True
        if name in self.FW_LEDS:
            f = self._leds
            b, bit = self.FW_LEDS[name]
            return bool(f and f[b] & bit)
        return False

    def _draw_deck(self):
        d = self.deck
        d.fill((24, 24, 26))
        d.blit(self._background(), (0, 0))
        # screen
        prog = self._boot_progress()
        crash = self._crash()
        if crash:
            self.screen_surf.fill((0, 0, 0))
            for i, (txt, col) in enumerate((
                    ("CRASHED", (255, 70, 60)),
                    (f"{self.fw_name}: {crash}", INK),
                    ("The firmware faulted before drawing anything.", DIM),
                    ("On a real deck this build would stay on a black screen.", DIM))):
                t = (self.big if i == 0 else self.label).render(txt, True, col)
                self.screen_surf.blit(t, (SCR_W // 2 - t.get_width() // 2, 150 + i * 44))
        elif self.machine is None or prog is not None:
            self.screen_surf.fill((0, 0, 0))
            msg = self.status or (f"booting {self.fw_name}  {int((prog or 0) * 100)}%"
                                  if self.machine else "no firmware")
            t = self.label.render(msg, True, DIM)
            self.screen_surf.blit(t, (SCR_W // 2 - t.get_width() // 2, SCR_H // 2 - 10))
            if prog is not None:
                pygame.draw.rect(self.screen_surf, (40, 40, 44), (200, 270, 400, 8))
                pygame.draw.rect(self.screen_surf, (60, 140, 230),
                                 (200, 270, int(400 * prog), 8))
        else:
            self._update_screen()
            self._draw_simplay()
        d.blit(self.screen_surf, (SCR_X, SCR_Y))
        # (no fingertip marker: the user found the touch circles weird, and
        # the deck's own screen already reacts to a touch; 2026-11-01)
        self._leds = self._panel_leds()
        f = self._fonts()
        deck.draw_jog(d, self.st.jog_count, self.st.jog_touch, f)
        for name in self.CONTROLS:
            deck.draw_control(d, name, self._lit(name), f)
        deck.draw_rotary(d, self.st.rotary, "RotaryPush" in self.st.held)
        deck.draw_fader(d, self.st.fader_raw)
        t = self.small.render(f"{self.st.rate() * 100:+.2f}%", True, DIM)
        d.blit(t, (self.FADER.centerx - t.get_width() // 2, self.FADER.bottom + deck.S(36)))

    # -- toolbar ---------------------------------------------------------------------
    def _tb_items(self):
        fw = f"Firmware: {self.fw_name}   v"
        th = f"Theme: {self.theme}   v"
        wv = f"Wave: {self.wave_mode}   v"
        # action label, not state: the stick name lives in the status
        # line; a state name here once read as "insert" and ejected a
        # mounted stick on a curious click (the deck screen then shows
        # NO USB until re-inserted)
        usb = "USB: eject stick" if self.usb_path else "USB: insert library"
        if self.view == "screen":               # compact: 800 px wide
            return [("fw", pygame.Rect(8, 8, 170, 32), fw),
                    ("theme", pygame.Rect(182, 8, 110, 32), th),
                    ("wave", pygame.Rect(296, 8, 100, 32), wv),
                    ("usb", pygame.Rect(400, 8, 110, 32), usb),
                    ("zout", pygame.Rect(514, 8, 26, 32), "–"),
                    ("zin", pygame.Rect(544, 8, 26, 32), "+"),
                    ("view", pygame.Rect(574, 8, 114, 32), "Deck (Tab)")]
        return [("fw", pygame.Rect(10, 8, 230, 32), fw),
                ("theme", pygame.Rect(246, 8, 150, 32), th),
                ("wave", pygame.Rect(402, 8, 150, 32), wv),
                ("zout", pygame.Rect(558, 8, 36, 32), "–"),
                ("zin", pygame.Rect(598, 8, 36, 32), "+"),
                ("save", pygame.Rect(640, 8, 100, 32), "Save state"),
                ("usb", pygame.Rect(746, 8, 150, 32), usb),
                ("sim", pygame.Rect(902, 8, 84, 32),
                 "Sim: " + ("ON" if self.sim.enabled else "OFF")),
                ("view", pygame.Rect(992, 8, 170, 32), "Screen only (Tab)")]

    def _menu_rows(self):
        rows = []
        y = TB_H
        items = {k: r for k, r, _ in self._tb_items()}
        if self.menu_open == "wave":
            x, w = items["wave"].x, max(items["wave"].w, 200)
            for name in ("blue", "rgb", "3band"):
                rows.append((pygame.Rect(x, y, w, 26), name, "wave", None))
                y += 26
            return rows
        if self.menu_open == "theme":
            x, w = items["theme"].x, max(items["theme"].w, 200)
            for name in theme_names():
                rows.append((pygame.Rect(x, y, w, 26), name, "theme", None))
                y += 26
            return rows
        x, w = items["fw"].x, max(items["fw"].w, 300)
        ch = firmware_choices()
        rh = max(14, min(26, (self.canvas.get_height() - TB_H - 8) // max(1, len(ch))))
        for label, kind, path in ch:
            rows.append((pygame.Rect(x, y, w, rh), label, kind, path))
            y += rh
        return rows

    # -- live theme preview ------------------------------------------------------
    def apply_theme(self, name):
        """Preview a saved theme (mods/themes.py) on the running stock
        firmware."""
        def spec():
            import sys as _sys
            _sys.path.insert(0, os.path.join(ROOT, "mods"))
            import themes as T
            return None if name == "stock" else T.THEMES[name]
        self.apply_theme_spec(name, spec)

    def apply_theme_spec(self, name, spec):
        """Recolour the running stock firmware's artwork in emulated RAM,
        exactly as a themed build would have it (mods/themes.py), without
        rebuilding or rebooting. block1 is unpacked at 0x08000000; only
        pixels still holding the previous theme's value are rewritten, so
        live data the firmware changed since boot is never touched.

        spec: a theme dict (hue/sat/val/spread/...), None for stock, or a
        callable returning one (resolved on the worker thread)."""
        m = self.machine
        if m is None or not self.fw_name.startswith("Stock"):
            self.status = "theme preview needs the stock firmware running"
            return
        self.status = f"theme {name} ..."

        def work():
            try:
                import sys as _sys
                _sys.path.insert(0, os.path.join(ROOT, "mods"))
                import themes as T
                sp = spec() if callable(spec) else spec
                if self._stock_words is None:
                    from . import fwhome
                    raw = open(fwhome.path("blk1.bin"), "rb").read()
                    self._stock_words = np.frombuffer(raw[:len(raw) // 2 * 2], "<u2")
                stock = self._stock_words
                new = stock if sp is None else T.recolour(stock.copy(), sp)[0]
                if sp and sp.get("lit"):        # lit buttons (mods/litbuttons.py)
                    import litbuttons as LB
                    LB.apply_words(new, stock, sp["lit"])
                prev = self._theme_words if self._theme_words is not None else stock
                ram = np.frombuffer(m.mem.ram0, dtype="<u2", count=len(stock))
                safe = (ram == prev) & (new != prev)
                ram[safe] = new[safe]
                self._theme_words = new
                self.theme = name
                self.status = ""
            except Exception as e:
                self.status = f"theme preview failed: {e}"
        threading.Thread(target=work, daemon=True).start()

    # -- virtual USB stick --------------------------------------------------------
    def _burst_sound(self, m, dev):
        """Hold the C6x DSP model while a USB burst fast-forwards with
        nothing audible. Running the DSP's Python is ~4x the SH's whole
        cost (11.7M vs 48.7M steps/s measured), and it advances on WALL
        time -- so the mount FAT walk was mostly waiting on the DSP.
        Correctness: bursts never run while playing (checked here too); if
        the user presses PLAY inside a window the burst ends immediately
        and the model is running again. A track LOAD is not audible but it
        uses the DSP: the firmware's load handshake waits for the DSP's
        block counter (0x11837CCC), so holding the DSP through it made
        every app-path load die as E-8302 (2026-11-01; same flow with only
        the hold disabled: ctr 133 -> 1013 over 14.5 s, red 0)."""
        de = getattr(dev, "dspemu", None)
        if de is None:
            return
        if self.bursting() and self._burst_usb:
            playing = False
            try:
                playing = _deck_playing(m)
            except Exception:
                pass
            if playing:
                self.burst_until = None        # keep audio at real time
                de.hold = False
            elif _deck_has_track(m):
                de.hold = False                # load/seek handshake needs it
            else:
                de.hold = True
        else:
            de.hold = False

    @staticmethod
    def _usb_ops(disk):
        """Monotonic IO count for a disk (mirror of usbdisk's `ops`)."""
        ops = getattr(disk, "ops", None)
        if ops is None:
            return len(disk.access) if hasattr(disk, "access") else 0
        return ops

    def _reburst(self, m, dev):
        """Open a new burst window when the stick suddenly gets read a lot
        (the user just loaded a track). Housekeeping reads (the deck's
        periodic settings pokes: a few sectors per slice) stay at real
        time. Never bursts while a track is playing: the Mac audio
        consumer is the real-time clock then, so an uncapped machine
        would race the deck's TIME ahead of the sound."""
        if os.environ.get("XDJ_NO_REBURST") == "1":
            return
        disk = getattr(dev, "usb_disk", None)
        ops = self._usb_ops(disk) if disk is not None else 0
        grown = ops - self._reburst_ops
        self._reburst_ops = ops
        if disk is None or grown < 16:
            return
        try:
            if _deck_playing(m):
                return
        except Exception:
            pass
        self.burst_until = m.cpu.steps + self.USB_BURST
        self._burst_usb = True
        self._burst_t0 = time.time()

    def insert_usb(self, path):
        """Insert a folder (e.g. a rekordbox export: the folder holding
        PIONEER/ and Contents/) or a raw disk image as a USB stick. The
        deck's writes stay in memory; the source is never modified."""
        m = self.machine
        if m is None:
            self.status = "USB: the emulator is still starting"
            return
        dev = m.mem.dev
        usb = dev._usb()

        def work():
            from .usbdisk import open_disk
            from .usbhost import MassStorage
            try:
                disk = open_disk(path)
            except Exception as e:
                self.status = f"cannot open {path}: {e}"
                return
            if self.machine is not m:           # firmware was switched meanwhile
                return
            if usb.attached:                    # swap mid-session: eject first
                usb.unplug()                    # so the host re-enumerates
            dev.usb_disk = disk
            usb.stick = MassStorage(disk)       # plugged in on the next tick
            self.usb_path = path
            self.usb_watch = time.time()
            self.status = f"USB stick: {os.path.basename(path.rstrip('/'))}"
            # Mount fast-forward: the FAT walk is emulated-CPU time; without
            # this it drags a minute of wall clock. The emu loop only EXTENDS
            # an existing burst, so start one here. The _emu_loop renews it
            # (deadline-capped) while the stick is still being read and ends
            # it when the FAT walk goes quiet, so the cap never bites.
            self.burst_until = m.cpu.steps + 200_000_000
            self._burst_usb = True
            self._reburst_ops = self._usb_ops(disk)
        threading.Thread(target=work, daemon=True).start()

    def eject_usb(self):
        m = self.machine
        if m is None:
            return
        dev = m.mem.dev
        usb = dev._usb()
        if usb.attached:
            usb.unplug()
        usb.stick = None
        dev.usb_disk = None
        self.usb_path = None
        self.usb_watch = None
        self.status = ("USB stick ejected (the deck screen may show NO USB "
                       "until you insert one again; click USB: insert "
                       "library to put it back)")

    def toggle_usb(self, path=None):
        """Standalone toolbar button: eject if a stick is in, else insert
        `path` (or ask with the macOS folder picker)."""
        if self.usb_path and path is None:
            return self.eject_usb()
        if path:
            return self.insert_usb(path)

        def ask():
            self.status = "pick a folder in the file dialog..."
            p = self._choose_usb()
            if not p:
                self.status = "USB insert cancelled"
            elif p.startswith("ERR:"):
                self.status = f"USB insert failed: {p[4:]}"
            else:
                self.insert_usb(p)
        threading.Thread(target=ask, daemon=True).start()

    def watch_usb(self):
        """One-shot loud failure for the silent no-mount: if a stick is
        inserted but the firmware never switches its USB host on (SYSCFG
        stays 0 -- the panel-link/input path never wakes the USB task),
        say so instead of leaving the deck on USB Error.

        NOT during early boot: a boot-from-reset does nothing about USB for
        ~10-20 s of real time (the deck's own startup), and this check used
        to fire at 10 s with "never switched USB on" -- a false alarm right
        before the stick attached. Now: wait until the screen has drawn
        AND give the firmware 90 s (the deck's own timeout window) before
        calling it a failure."""
        if self.usb_watch is None or self.machine is None:
            return
        try:
            m = self.machine
            usb = m.mem.dev._usb()
        except Exception:
            return
        if usb is None:
            return
        if getattr(usb, "attached", False):
            self.usb_watch = None
            return
        # still booting (nothing drawn yet): boot is slow, be silent
        if getattr(m.mem, "fb_writes", 0) <= 0:
            return
        if time.time() - self.usb_watch < 90:
            return
        self.usb_watch = None
        on = bool(usb.r.get(0x00, 0) & 1)
        self.status = ("USB ready" if on else
                       "USB not mounting: 90 s after the deck started it "
                       "never switched the stick on. Power-cycle the "
                       "emulator (Restart), then insert the stick again.")

    @staticmethod
    def _choose_usb():
        """macOS folder picker. Brings our window forward first so the
        dialog doesn't hide behind the SDL window; reports blocks
        (Automation permission) instead of failing silently."""
        import subprocess
        try:
            r = subprocess.run(
                ["osascript",
                 "-e", 'tell application "System Events" to set frontmost '
                       'of (first process whose name contains "XDJ") to true',
                 "-e", 'POSIX path of (choose folder with prompt '
                       '"Choose a rekordbox USB export (the folder holding PIONEER)")'],
                capture_output=True, text=True, timeout=600)
            err = (r.stderr or "").strip()
            if r.returncode != 0 and "not allowed" in err + r.stdout:
                return "ERR:macOS blocked the picker (Privacy & Security > Automation)"
            out = r.stdout.strip().splitlines()
            out = out[-1] if out else ""
            return out or None
        except Exception as e:
            return f"ERR:{e}"

    def _draw_toolbar(self):
        c = self.canvas
        cw = c.get_width()
        pygame.draw.rect(c, (18, 18, 20), (0, 0, cw, TB_H))
        pygame.draw.line(c, (60, 60, 66), (0, TB_H - 1), (cw, TB_H - 1))
        for key, r, text in self._tb_items():
            pygame.draw.rect(c, (44, 45, 50), r, border_radius=6)
            t = self.small.render(text, True, INK)
            c.blit(t, (r.x + 10 if key in ("fw", "theme", "wave") else r.centerx - t.get_width() // 2,
                       r.centery - t.get_height() // 2))
        m = self.machine
        info = self.status
        if m is not None and not info:
            try:
                info = (f"{int(self.zoom * 100)}%   steps {m.cpu.steps / 1e6:,.0f}M   "
                        f"exceptions {m.exc_total}")
                if m.exc_total:
                    info += "  (firmware crashed?)" if m.exc_total > 20 else ""
            except Exception:
                pass
        if self.view == "deck":
            c.blit(self.small.render(info, True, DIM), (1224, 16))

    def _draw_menu(self):
        if not self.menu_open:
            return
        c = self.canvas
        rows = self._menu_rows()
        box = rows[0][0].unionall([r[0] for r in rows]).inflate(8, 8)
        pygame.draw.rect(c, (28, 28, 32), box, border_radius=6)
        pygame.draw.rect(c, (80, 80, 88), box, 1, border_radius=6)
        mp = self._to_canvas(getattr(self, "_mousepos", (0, 0)))
        for r, label, kind, path in rows:
            if r.collidepoint(mp):
                pygame.draw.rect(c, (60, 110, 190), r, border_radius=4)
            t = self.small.render(label, True, INK)
            c.blit(t, (r.x + 10, r.centery - t.get_height() // 2))

    def draw(self):
        self._draw_deck()
        if self.view == "deck":
            self.canvas.blit(self.deck, (0, TB_H))
        else:
            self.canvas.blit(self.deck, (0, TB_H), (SCR_X, SCR_Y, SCR_W, SCR_H))
        self._draw_toolbar()
        self._draw_menu()
        self.present()

    # -- events ------------------------------------------------------------------------
    def _hit(self, pos):
        for name, ((cx, cy), r, label, key, style) in self.CONTROLS.items():
            if (pos[0] - cx) ** 2 + (pos[1] - cy) ** 2 <= (r + 6) ** 2:
                return name
        return None

    def _set(self, name, down):
        key = self.CONTROLS[name][3] or name
        (self.st.held.add if down else self.st.held.discard)(key)

    def _click_toolbar(self, p):
        if self.menu_open:
            for r, label, kind, path in self._menu_rows():
                if r.collidepoint(p):
                    self.menu_open = False
                    if kind == "theme":
                        self.apply_theme(label)
                    elif kind == "wave":
                        self.apply_wave(label)
                    else:
                        self._boot(label, kind, path)
                    return True
            self.menu_open = False
            return True
        if p[1] >= TB_H:
            return False
        for key, r, _ in self._tb_items():
            if r.collidepoint(p):
                if key in ("fw", "theme", "wave"):
                    self.menu_open = key
                elif key == "zin":
                    self._zoom_step(+1)
                elif key == "zout":
                    self._zoom_step(-1)
                elif key == "save":
                    self.save_state()
                elif key == "usb":
                    self.toggle_usb()
                elif key == "sim":
                    self.sim.enabled = not self.sim.enabled
                    self.status = ("SimPlay overlay " +
                                   ("ON" if self.sim.enabled else "OFF"))
                elif key == "view":
                    self.set_view("screen" if self.view == "deck" else "deck")
        return True

    def handle(self, ev):
        """Standalone window: an SDL event goes to the toolbar or to the
        shared input API below."""
        pg = pygame
        if ev.type == pg.QUIT:
            return False
        if ev.type in (pg.VIDEORESIZE, getattr(pg, "WINDOWRESIZED", -1)):
            self._snap_to_aspect()
        if ev.type in (pg.KEYDOWN, pg.KEYUP):
            down = ev.type == pg.KEYDOWN
            name = pg.key.name(ev.key)
            cmd = ev.mod & (pg.KMOD_META | pg.KMOD_CTRL)
            if down and cmd and name in ("=", "+"):
                self._zoom_step(+1)
            elif down and cmd and name == "-":
                self._zoom_step(-1)
            elif down and cmd and name == "0":
                self.zoom = 1.0
                self._apply_zoom()
            elif down and name == "escape":
                self.menu_open = False
            elif down and name == "s":
                self.sim.enabled = not self.sim.enabled
                self.status = ("SimPlay overlay " +
                               ("ON" if self.sim.enabled else "OFF"))
            elif down and name == "tab":
                self.set_view("screen" if self.view == "deck" else "deck")
            else:
                self.key_event(name, down)
        if ev.type == pg.MOUSEWHEEL:
            self.wheel(ev.y)
        if ev.type == pg.MOUSEBUTTONDOWN and ev.button == 1:
            p = self._to_canvas(ev.pos)
            if self._click_toolbar(p):
                return True
            self.pointer_down((p[0], p[1] - TB_H))
        if ev.type == pg.MOUSEBUTTONUP and ev.button == 1:
            self.pointer_up()
        if ev.type == pg.MOUSEMOTION:
            self._mousepos = ev.pos
            p = self._to_canvas(ev.pos)
            self.pointer_move((p[0], p[1] - TB_H))
        return True

    # -- input API (shared by the window and by Mod Studio) -------------------
    # Pointer positions are in deck coordinates: the deck surface in "deck"
    # view, the 800x480 screen in "screen" view.
    def _input_burst(self):
        """User just pressed something: the deck's reaction (popup dismiss,
        browse navigation, load handshake) is emulated-CPU work that a
        wall-clock-paced loop executes at a fraction of deck speed --
        every screen change then took tens of seconds (user: "very very
        slow"). Fast-forward a fixed window behind every input, the same
        trick the mount uses; skipped while playing so audio stays at
        real time."""
        m = self.machine
        if m is None:
            return
        try:
            if _deck_playing(m):
                return
        except Exception:
            return
        want = m.cpu.steps + self.INPUT_BURST
        if self.burst_until is None or want > self.burst_until:
            self.burst_until = want
            self._burst_usb = True
            self._burst_t0 = time.time()

    def _press_start(self):
        """A button / touch / knob push went DOWN: stop any fast-forward
        first. The hold is timed on the wall clock (MIN_HOLD), so pressing
        during a burst (the one the previous click opened) stretched a
        0.25 s click into seconds of deck time -- the deck took it as a
        LONG press: BROWSE opened SEARCH instead of closing, the knob push
        showed the scroll hint (user: "clicks don't register", measured
        2026-11-01: 6 of 7 BROWSE taps lost while a burst ran). The release
        reopens the burst, so the reaction itself is still fast."""
        if self.burst_until is not None:
            self.burst_until = None
            de = getattr(getattr(self.machine, "mem", None), "dev", None)
            de = getattr(de, "dspemu", None)
            if de is not None:
                de.hold = False

    MIN_HOLD = 0.25
    MIN_TICKS = 3

    def key_event(self, name, down):
        """A deck key by its pygame name ("space", "c", "left", "[" ...).
        The fast-forward burst starts at the RELEASE (see _input_burst):
        a burst under a held key turned every click into a long press."""
        if name in self.KEYS:
            if down:
                self._press_start()
                self._key_up_pending.discard(name)
                self._key_t0[name] = [time.time(), 0]
            else:
                t0 = self._key_t0.get(name)
                if t0 and (time.time() - t0[0] < self.MIN_HOLD
                           or t0[1] < self.MIN_TICKS):
                    self._key_up_pending.add(name)   # released in tick()
                    return
            self._set(self.KEYS[name], down)
            if not down:
                self._input_burst()
            return
        if down:
            self._input_burst()
        if name in ("left", "right"):
            self.st.jog_touch = down
            if down:
                self.st.jog_count += -200 if name == "left" else 200
        elif name in ("up", "down") and down:
            self.st.rotary += -1 if name == "up" else 1
        elif name in ("=", "+") and down:
            self.st.fader_raw = max(0, self.st.fader_raw - 5)
        elif name == "-" and down:
            self.st.fader_raw = min(499, self.st.fader_raw + 5)

    def wheel(self, dy):
        """Mouse wheel: dy > 0 = scrolled up = browse knob counter-clockwise."""
        self.st.rotary += -dy
        self._input_burst()

    def rotary_step(self, n):
        self.st.rotary += n
        self._input_burst()

    def pointer_down(self, p):
        if self._up_pending:                     # previous tap not out yet
            self._up_pending = False
            self._pointer_release()
        self._hold_t0, self._hold_ticks = time.time(), 0
        self._press_start()
        if self.view == "screen":               # the canvas is the screen
            if 0 <= p[0] < FB_W and 0 <= p[1] < FB_H:
                self._touch_px = (p[0], p[1])
            return
        name = self._hit(p)
        if name:
            self._pressed = name
            self._set(name, True)
        elif (p[0] - self.ROT_C[0]) ** 2 + (p[1] - self.ROT_C[1]) ** 2 \
                <= self.ROT_R ** 2:
            self._pressed = "ROTARY"
            self.st.held.add("RotaryPush")
        elif self.FADER.inflate(30, 0).collidepoint(p):
            self.fader_drag = True
            self.fader_to(p[1])
        elif (p[0] - self.JOG_C[0]) ** 2 + (p[1] - self.JOG_C[1]) ** 2 \
                <= self.JOG_R ** 2:
            # like the real jog: the top platter (inner part) is touch-
            # sensitive (VINYL mode: stop + scratch), the outer ring only
            # turns (pitch bend)
            r2 = (p[0] - self.JOG_C[0]) ** 2 + (p[1] - self.JOG_C[1]) ** 2
            self.st.jog_touch = r2 <= (0.72 * self.JOG_R) ** 2
            self._jog_drag = True
            self._jog_ang = math.atan2(p[1] - self.JOG_C[1],
                                       p[0] - self.JOG_C[0])
        elif pygame.Rect(SCR_X, SCR_Y, SCR_W, SCR_H).collidepoint(p):
            self._touch_px = (p[0] - SCR_X, p[1] - SCR_Y)  # native touch

    def pointer_move(self, p):
        if self.fader_drag:
            self.fader_to(p[1])
        if self._touch_px is not None:          # finger drag on the screen
            q = p if self.view == "screen" else (p[0] - SCR_X, p[1] - SCR_Y)
            self._touch_px = (min(max(q[0], 0), FB_W - 1),
                              min(max(q[1], 0), FB_H - 1))
        if getattr(self, "_jog_drag", False) and self._jog_ang is not None:
            a = math.atan2(p[1] - self.JOG_C[1], p[0] - self.JOG_C[0])
            dd = (a - self._jog_ang + math.pi) % (2 * math.pi) - math.pi
            self.st.jog_count += int(dd * 300)
            self._jog_ang = a

    def pointer_up(self):
        if time.time() - self._hold_t0 < self.MIN_HOLD \
                or self._hold_ticks < self.MIN_TICKS:
            self._up_pending = True              # released in tick()
            return
        self._pointer_release()

    def _held_tick(self):
        """once per injected frame: count it, release deferred taps/keys"""
        self._hold_ticks += 1
        now = time.time()
        for k, t0 in self._key_t0.items():
            t0[1] += 1
        if self._up_pending and now - self._hold_t0 >= self.MIN_HOLD \
                and self._hold_ticks >= self.MIN_TICKS:
            self._up_pending = False
            self._pointer_release()
        for k in list(self._key_up_pending):
            t0 = self._key_t0.get(k)
            if not t0 or (now - t0[0] >= self.MIN_HOLD and t0[1] >= self.MIN_TICKS):
                self._key_up_pending.discard(k)
                self._set(self.KEYS[k], False)
                self._input_burst()

    def _pointer_release(self):
        n = self._pressed
        if n == "ROTARY":
            self.st.held.discard("RotaryPush")
        elif n:
            self._set(n, False)
        self._pressed = None
        self._touch_key = None
        self._touch_px = None
        self.fader_drag = False
        self.st.jog_touch = False
        self._jog_drag = False
        self._input_burst()      # the deck's reaction runs fast, the press did not

    def release_all(self):
        """Let go of everything (focus lost, tab hidden): nothing may stay
        pressed with nobody holding it."""
        self._up_pending = False
        self._key_up_pending.clear()
        self._pointer_release()
        self.st.held.clear()

    # -- hosted rendering ---------------------------------------------------
    def tick(self, dt):
        """Hosted mode, once per frame: input -> panel frame, USB watchdog."""
        self.t += dt
        self.inject_panl()
        self._held_tick()
        self.watch_usb()
        self._sound_tick()

    def _sound_tick(self):
        """Deck sound (emu/decksound.py): the real track plays while the
        firmware plays; the MT ratio word pitches it live. Needs the HLE
        DSP (XDJ_HLE_DSP=1); XDJ_DECK_SOUND=0 kills it. Never raises."""
        try:
            if self.machine is None:
                if getattr(self, "_sound", None) is not None:
                    self._sound.close()
                    self._sound = None
                return
            s = getattr(self, "_sound", None)
            if s is None or s.m is not self.machine:
                # The real-DSP path (snapshot with the modelled C6x DSP) has
                # its own audible bridge: the DSP's McASP stream out loud.
                if getattr(self.machine.mem.dev, "dspemu", None) is not None:
                    from emu import dspsound
                    s = self._sound = dspsound.attach(self.machine)
                else:
                    from emu import decksound
                    s = self._sound = decksound.attach(self.machine)
                if s is not None and getattr(self, "_mute_req", False):
                    if hasattr(s, "set_mute"):
                        s.set_mute(True)
                    else:
                        s.muted = True
            if s is not None and self.t - getattr(self, "_sound_t", 0) > 0.1:
                self._sound_t = self.t
                s.sync()
        except Exception:
            pass

    def render(self):
        """Hosted mode: draw the whole deck (screen, jog, buttons, LEDs,
        fader, rotary) and return it as a pygame Surface."""
        self._draw_deck()
        return self.deck

    def info(self):
        """Plain-data status for the host UI."""
        m = self.machine
        prog = self._boot_progress()
        crash = self._crash()
        state = ("loading" if m is None and not self.status.startswith("cannot")
                 and not self.status.startswith("no firmware") else
                 "error" if m is None else
                 "crashed" if crash else
                 "booting" if prog is not None else "running")
        return {"state": state, "fw_name": self.fw_name,
                "status": self.status, "crash": crash,
                "progress": prog, "theme": self.theme,
                "wave": self.wave_mode,
                "usb": self.usb_path,
                "deck_mode": self._deck_mode(),
                "stock": self.fw_name.startswith("Stock"),
                "real_dsp": getattr(self.machine and self.machine.mem.dev,
                                    "dspemu", None) is not None}

    def _deck_mode(self):
        """The firmware's screen mode word (0 idle, 3 loaded, 5 playing);
        None without a machine. Exposed so hosted UIs/tests can follow a
        load without scraping frames."""
        if self.machine is None:
            return None
        try:
            return self.machine.mem.r8(0x0B6C4264)
        except Exception:
            return None

    def fader_to(self, my):
        f = self.FADER
        frac = (my - f.y - 15) / float(f.h - 30)
        self.st.fader_raw = max(0.0, min(1.0, frac)) * 499.0

    def run(self):
        running = True
        while running:
            # NOTE: input comes from the window's own SDL poll, not
            # pygame.event -- the Retina window is created behind
            # pygame's back, so pygame never sees its input.
            for ev in self.window.poll():
                if not self.handle(ev):
                    running = False
            dt = self.clock.tick(30) / 1000.0
            self.t += dt
            self.inject_panl()
            self._held_tick()
            self.watch_usb()
            self._sound_tick()
            self.draw()
        self._stop()
        pygame.quit()
