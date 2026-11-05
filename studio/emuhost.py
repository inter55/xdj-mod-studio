"""The XDJ-700 emulator, hosted inside Mod Studio.

This is not a re-creation of the deck: it runs emu.ui.EmuApp -- the same
front-end the standalone emulator window uses (screen, buttons, jog wheel,
tempo fader, browse knob, touch, LEDs) -- headless, in this process, and
streams the finished deck image to the Emulator tab as MJPEG. Browser input
(pointer, keys, wheel) goes back through EmuApp's own input API, so every
control behaves exactly as it does in the emulator window.

Threads: the firmware runs on EmuApp's own emulation thread; one render
thread here turns the deck into JPEGs (only while the tab is being watched;
the firmware is paused a little after the tab is closed, and resumes when
it is opened again).
"""
import io
import os
import threading
import time
import zlib

FPS = 30
IDLE_PAUSE_S = 20          # nobody watching for this long -> pause firmware
HEARTBEAT_S = 1.5          # resend an unchanged frame this often
MIN_W, DEFAULT_W = 320, 800


class EmuHost:
    def __init__(self):
        self.app = None
        self.error = None
        self.lock = threading.RLock()       # app state: input vs. render
        self.cond = threading.Condition()   # new-frame signal
        self.frame = (0, b"")               # (sequence, jpeg)
        self.target_w = DEFAULT_W
        self._viewers = 0
        self._last_view = 0.0
        self._crc = None
        self._last_pub = 0.0
        self._started = False
        self._thread = None
        self._mods = None                   # last requested mods
        self._mods_gen = 0
        self.deck_size = None               # (w, h) of the native deck
        self.fw_id = None
        self.stick_wanted = None            # path the user asked to insert
        self._mute_req = False              # mute re-applied after re-attach

    # ------------------------------------------------------------ lifecycle
    def start(self):
        """Create the emulator and boot the default firmware (idempotent)."""
        with self.lock:
            if self._started:
                return
            self._started = True
            try:
                from emu import ui as emu_ui          # needs pygame
                from emu import deck
                self.deck_size = (deck.DECK_W, deck.DECK_H)
                # The QEMU engine (docs/EMU_REBUILD_PLAN.md) when it is
                # installed: real time, real DSP. XDJ_EMU_ENGINE=old keeps
                # our own emulator (tests, machines without the build).
                from emu import qemu_app
                if (os.environ.get("XDJ_EMU_ENGINE") != "old"
                        and qemu_app.available()):
                    self.app = qemu_app.QemuApp(headless=True)
                else:
                    self.app = emu_ui.EmuApp(headless=True)
            except Exception as e:                    # missing pygame etc.
                self.error = f"{type(e).__name__}: {e}"
                return
            self._thread = threading.Thread(target=self._render_loop,
                                            daemon=True, name="emu-render")
            self._thread.start()
        choices = self.firmware_list()
        if choices:
            self.boot(choices[0]["id"])
            self._auto_insert_stick()
        else:
            self.app.status = "no firmware found"

    def _auto_insert_stick(self):
        """Put the remembered library stick back at boot: the shipped
        snapshot starts WITHOUT a device, so the deck screen would show
        NO USB on every fresh launch until the user re-picked the folder.
        Silent no-op when none is remembered or it has moved; the button
        still allows a manual pick."""
        self._prefer_when_ready()

    def _prefer_remembered(self):
        """The user's own library always wins over whatever stick a
        snapshot carried in (otherwise the deck shows the snapshot's demo
        stick instead of your music). No remembered library: whatever is
        attached stays."""
        try:
            from .app import stick_path
            p = stick_path()
            if not (p and os.path.isdir(p)):
                return
            cur = self.app.info().get("usb") or ""
            try:
                same = os.path.realpath(cur) == os.path.realpath(p)
            except Exception:
                same = False
            if not same:
                self.insert_stick(p)
        except Exception:
            pass

    def _prefer_when_ready(self):
        def work():
            t0 = time.time()
            while time.time() - t0 < 180:
                try:
                    if self.app is not None and self.app.info().get(
                            "state") == "running":
                        time.sleep(0.5)    # adoption runs in _start (sync)
                        self._prefer_remembered()
                        return
                except Exception:
                    return
                time.sleep(1)
        threading.Thread(target=work, daemon=True,
                         name="emu-prefer-stick").start()

    def shutdown(self):
        self._started = False
        app = self.app
        if app is not None:
            snd = getattr(app, "_sound", None)
            if snd is not None:
                try:
                    snd.close()
                except Exception:
                    pass
                app._sound = None
            app._stop()

    # ------------------------------------------------------------ firmware
    def firmware_list(self):
        """Two choices for people (user, 2026-11-01: "why can't there just
        be two"): the deck WITH our mods, and the ORIGINAL firmware. Both
        restore the real-audio snapshot (or the plain one when it is
        missing); "mods" then installs the mod set in emulated RAM, exactly
        what the cable install does to a deck. XDJ_ALL_FIRMWARES=1 brings
        back the developer list (boot from reset, built .UPDs...)."""
        from emu.choices import firmware_choices
        if getattr(self.app, "engine", None) == "qemu":
            return [{"id": "mods", "label": "With mods", "kind": "qemu-mods",
                     "path": None, "boot_label": "Stock v1.15 + mods"},
                    {"id": "original", "label": "Original firmware",
                     "kind": "qemu", "path": None,
                     "boot_label": "Stock v1.15"}]
        out = []
        ch = firmware_choices()
        if os.environ.get("XDJ_ALL_FIRMWARES") != "1":
            snap = next((c for c in ch if c[1] == "ckpt-dsp"), None) or \
                next((c for c in ch if c[1] == "ckpt"), None)
            if snap is not None:
                return [{"id": "mods", "label": "With mods", "kind": snap[1],
                         "path": snap[2], "boot_label": snap[0]},
                        {"id": "original", "label": "Original firmware",
                         "kind": snap[1], "path": snap[2],
                         "boot_label": snap[0]}]
        for label, kind, path in ch:
            fid = ("snapshot" if kind == "ckpt" else
                   "snapshot-dsp" if kind == "ckpt-dsp" else
                   "reset" if path is None else "upd:" + os.path.basename(path))
            out.append({"id": fid, "label": label, "kind": kind, "path": path})
        return out

    def boot(self, fid):
        for c in self.firmware_list():
            if c["id"] == fid:
                break
        else:
            raise ValueError(f"unknown firmware {fid!r}")
        self.start()
        if self.app is None:
            raise RuntimeError(self.error or "emulator unavailable")
        with self.lock:
            self.fw_id = fid
            self.app.release_all()
            if fid == "mods" and getattr(self.app, "engine", None) == "qemu":
                self._seed_qemu(self._mods or {"wave": "rgb"})
            self.app._boot(c.get("boot_label", c["label"]), c["kind"], c["path"])
        # the new machine has nothing applied yet: re-apply what was asked
        # ("With mods" always gets the mod set; "Original" never does)
        if fid == "mods":
            self.apply_mods(self._mods or {"wave": "rgb"}, keep_fw=True)
        elif self._mods and fid != "original":
            self.apply_mods(self._mods)
        if self.stick_wanted:
            self._insert_when_ready(self.stick_wanted)
        else:
            # a fresh snapshot may carry its demo stick: prefer the
            # user's own library again once running
            self._prefer_when_ready()

    def restart(self):
        self.boot(self.fw_id or self.firmware_list()[0]["id"])

    # ---------------------------------------------------------------- mods
    def apply_mods(self, mods, keep_fw=False):
        """mods: {"theme": {"name", "spec"} | None, "wave": "blue|rgb|3band
        |custom" | None}. Applied to the stock firmware
        as soon as it is running; re-applied after every firmware switch.
        Custom is preview-only (no deck cave): the emulator keeps stock."""
        self.start()
        if self.fw_id == "original" and not keep_fw:
            self.fw_id = "mods"         # mods asked for: now it IS the modded deck
        self._mods = dict(mods)
        self._mods_gen += 1
        gen = self._mods_gen

        def work():
            if not self._wait_running(gen, 120):
                return
            app = self.app
            if not app.fw_name.startswith("Stock"):
                return                  # a built .UPD already carries its mods
            m = self._mods
            th = m.get("theme")
            if th is not None:
                app.apply_theme_spec(th.get("name") or "custom", th.get("spec"))
            elif m.get("theme_stock"):
                app.apply_theme_spec("stock", None)
            self._wait_status_clear(gen)
            if m.get("fx") and hasattr(app, "set_audio_fx"):
                app.set_audio_fx(m["fx"])
            if m.get("wave") in ("blue", "rgb", "3band"):
                app.apply_wave(m["wave"])
        threading.Thread(target=work, daemon=True, name="emu-mods").start()

    def _seed_qemu(self, m):
        """QEMU bakes the mods into the image it boots: set them BEFORE the
        boot, so the With-mods boot is already the right image (no second
        reboot when apply_mods runs after it)."""
        app = self.app
        if m.get("wave") in ("blue", "rgb", "3band"):
            app.wave_mode = m["wave"]
        th = m.get("theme")
        app.theme = (th.get("name") or "custom") if th else "stock"
        app.theme_spec = th.get("spec") if th else None
        if m.get("fx") in ("keyshift", "touchfx"):
            app.audio_fx = m["fx"]

    def _wait_running(self, gen, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout and gen == self._mods_gen:
            app = self.app
            if app is not None and app.info()["state"] == "running":
                time.sleep(0.4)         # let the first frames draw
                return gen == self._mods_gen
            time.sleep(0.25)
        return False

    def _wait_status_clear(self, gen, timeout=15):
        t0 = time.time()
        while time.time() - t0 < timeout and gen == self._mods_gen:
            if not self.app.status.startswith("theme "):
                return
            time.sleep(0.1)

    # ----------------------------------------------------------------- USB
    def insert_stick(self, path):
        self.start()
        if self.app is None:
            raise RuntimeError(self.error or "emulator unavailable")
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        self.stick_wanted = path
        self._insert_when_ready(path)

    def _insert_when_ready(self, path):
        def work():
            t0 = time.time()
            while time.time() - t0 < 120 and self.stick_wanted == path:
                # QEMU engine: no Python machine -- ready when running
                if (self.app.machine is not None
                        or (getattr(self.app, "engine", None) == "qemu"
                            and self.app.info().get("state") == "running")):
                    self.app.insert_usb(path)
                    return
                time.sleep(0.25)
        threading.Thread(target=work, daemon=True, name="emu-usb").start()

    def eject_stick(self):
        self.stick_wanted = None
        if self.app is not None:
            self.app.eject_usb()

    # --------------------------------------------------------------- input
    def input(self, events):
        """Browser events: pointer down/move/up (nx, ny in 0..1 of the deck
        image), key (pygame key name), rot (browse knob steps), release."""
        app = self.app
        if app is None or self.deck_size is None:
            return False
        W, H = self.deck_size
        with self.lock:
            app.paused = False
            # Recent input counts as being watched: the render loop only
            # ticks (and so only INJECTS input into the deck) while a
            # stream viewer is connected -- if the browser's MJPEG
            # connection dropped or the tab throttled it, every tap and
            # key was silently ignored and the app looked completely dead.
            self._last_view = self._last_input = time.time()
            for e in events:
                t = e.get("t")
                if t in ("down", "move"):
                    p = (float(e["x"]) * W, float(e["y"]) * H)
                    (app.pointer_down if t == "down" else app.pointer_move)(p)
                elif t == "up":
                    app.pointer_up()
                elif t == "key":
                    app.key_event(str(e["k"]), bool(e.get("d", 1)))
                elif t == "rot":
                    app.rotary_step(int(e["n"]))
                elif t == "release":
                    app.release_all()
        return True

    # --------------------------------------------------------------- state
    def state(self):
        out = {"started": self._started, "error": self.error,
               "firmwares": [], "firmware_id": self.fw_id,
               "stick_wanted": self.stick_wanted,
               "deck": list(self.deck_size) if self.deck_size else None}
        try:
            out["firmwares"] = self.firmware_list()
        except Exception as e:
            out["firmware_error"] = str(e)
        if self.app is not None:
            try:
                out.update(self.app.info())
            except Exception as e:      # never 500 the whole state poll
                out["info_error"] = f"{type(e).__name__}: {e}"
                out["error"] = out["error"] or str(e)
                out.setdefault("state", "error")
        else:
            out["state"] = "error" if self.error else "idle"
        s = getattr(self.app, "_sound", None) if self.app is not None else None
        if s is not None:
            out["sound"] = s.describe()
        elif getattr(self.app, "engine", None) == "qemu":
            # QEMU owns the audio: report it live so Mute is usable
            out["sound"] = {"on": True, "playing": True, "track": None,
                            "muted": bool(getattr(self.app, "_muted", False))}
        else:
            out["sound"] = {"on": False}
        for c in out["firmwares"]:
            c.pop("path", None)
        return out

    # ------------------------------------------------------------ deck sound
    def set_pitch(self, semis, hold=False):
        """Live pitch on the deck sound. hold=True drives the REAL chain
        (keeps rewriting the firmware's Q20 ratio word at tick rate, the
        keeper-mod prototype); hold=False shifts the audio cleanly."""
        s = self.app and getattr(self.app, "_sound", None)
        if s is not None and hasattr(s, "hold_semis"):
            v = None if semis is None else max(-8.0, min(8.0, float(semis)))
            if hold and v is not None:
                s.hold_semis = v
                s.pitch_override = None     # sound follows the mailbox echo
            else:
                s.hold_semis = None
                s.pitch_override = v
            return True
        return False

    def set_mute(self, muted):
        self._mute_req = bool(muted)
        if getattr(self.app, "engine", None) == "qemu":
            # QEMU plays the deck's sound itself: it has to restart with the
            # audio backend off, and that is the only mute it has.
            m = getattr(self.app, "set_mute", None)
            if callable(m):
                return bool(m(bool(muted)))
        if self.app is not None:
            self.app._mute_req = bool(muted)  # _sound_tick honors it
        s = self.app and getattr(self.app, "_sound", None)
        if s is not None:
            if hasattr(s, "set_mute"):
                s.set_mute(bool(muted))     # DspSound (real DSP bridge)
            else:
                s.muted = bool(muted)       # DeckSound (HLE path)
            return True
        return self.app is not None         # remembered, applied on attach

    # -------------------------------------------------------------- frames
    def viewer(self, width=None):
        """Context for a stream/frame consumer: keeps the emulator awake."""
        return _Viewer(self, width)

    def _render_loop(self):
        import pygame
        last = time.time()
        while self._started:
            now = time.time()
            # watched = a stream viewer OR input in the last 5 s (the
            # input path must keep ticking even without a viewer; see
            # input())
            watched = (self._viewers > 0
                       or now - getattr(self, "_last_input", 0) < 5.0)
            if not watched:
                if (self.app and not self.app.paused and
                        now - self._last_view > IDLE_PAUSE_S):
                    self.app.release_all()
                    self.app.paused = True
                    # silence: a paused deck must never audible-loop a
                    # stale ring at nobody (the 2026-10-15 machine-gun).
                    # _mute_req is untouched, so a user mute survives;
                    # resume re-attaches fresh audio on the next tick.
                    snd = getattr(self.app, "_sound", None)
                    if snd is not None:
                        try:
                            snd.close()
                        except Exception:
                            pass
                        self.app._sound = None
                time.sleep(0.1)
                last = time.time()
                continue
            dt, last = now - last, now
            try:
                with self.lock:
                    self.app.paused = False
                    self.app.tick(dt)
                    surf = self.app.render()
                    w = max(MIN_W, min(int(self.target_w), surf.get_width()))
                    if w < surf.get_width():
                        h = round(surf.get_height() * w / surf.get_width())
                        surf = pygame.transform.smoothscale(surf, (w, h))
                    size = surf.get_size()
                    raw = pygame.image.tobytes(surf, "RGB")
                crc = zlib.crc32(raw)
                if crc != self._crc or now - self._last_pub > HEARTBEAT_S:
                    self._publish(self._encode(raw, size))
                    self._crc, self._last_pub = crc, now
                if self.error and self.error.startswith("render:"):
                    self.error = None              # it recovered
            except Exception as e:                  # never kill the loop
                self.error = f"render: {type(e).__name__}: {e}"
            spent = time.time() - now
            time.sleep(max(0.0, 1.0 / FPS - spent))

    @staticmethod
    def _encode(raw, size):
        from PIL import Image
        im = Image.frombytes("RGB", size, raw)
        bio = io.BytesIO()
        im.save(bio, "JPEG", quality=88, subsampling=0)
        return bio.getvalue()

    def _publish(self, jpeg):
        with self.cond:
            self.frame = (self.frame[0] + 1, jpeg)
            self.cond.notify_all()

    def wait_frame(self, seq, timeout=2.0):
        with self.cond:
            if self.frame[0] == seq:
                self.cond.wait(timeout)
            return self.frame


class _Viewer:
    def __init__(self, host, width):
        self.host, self.width = host, width

    def __enter__(self):
        h = self.host
        with h.cond:
            h._viewers += 1
        h._last_view = time.time()
        if self.width:
            h.target_w = int(self.width)
        h._crc = None                        # force a fresh frame
        if h.app is not None:
            h.app.paused = False
        return self

    def __exit__(self, *a):
        h = self.host
        with h.cond:
            h._viewers = max(0, h._viewers - 1)
        h._last_view = time.time()
        if h._viewers == 0 and h.app is not None:
            h.app.release_all()
        return False
