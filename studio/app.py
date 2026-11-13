#!/usr/bin/env python3
"""XDJ Mod Studio -- one local app for the XDJ-700 mods.

    python -m studio.app            (development; the Mac app runs the same
                                     server inside its window)

  Home      "Ready to mod?" checklist of the mods + setup state.
  Mods      the mod builder: pick mods, install them on the deck.
  Theme     theme + waveform + beat-jump editor with a live preview of
  Builder   the deck screen; test it in the Emulator with one click.
  Track     your USB library: 3-band waveforms, key and stem versions,
  Builder   with progress + estimated time per build.
  Emulator  the real XDJ-700 emulator front-end (emu/ui.py) hosted inside
            Studio: screen, buttons, jog, fader, knob, touch, LEDs.
  Settings  library folder, project folder, stem separation, captures.

Standard library + numpy/Pillow/pygame. Binds to 127.0.0.1 only.
"""
import glob
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import re
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FROZEN = getattr(sys, "frozen", False)
WINDOWS = sys.platform == "win32"
if sys.platform == "darwin":
    # Opened by double-click, macOS gives an app a bare PATH without Homebrew
    # (where ffmpeg, rubberband and python3.12 live): add it ourselves, like a
    # Terminal launch would (Reddit report 2026-10-21).
    _p = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    for _d in ("/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin"):
        if os.path.isdir(_d) and _d not in _p:
            _p.append(_d)
    os.environ["PATH"] = os.pathsep.join(_p or ["/usr/bin", "/bin"])
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from emu.paths import app_support_dir  # noqa: E402  (stdlib-only module)
if WINDOWS:
    # The Windows app is built windowed (no black console window): its
    # stdout/stderr are None unless a caller (CI, a parent tool) piped them.
    # Then everything goes to a log file in the app's own folder.
    if sys.stdout is None or sys.stderr is None:
        try:
            _logd = os.path.join(app_support_dir(), "logs")
            os.makedirs(_logd, exist_ok=True)
            _log = open(os.path.join(_logd, "tool.log" if "--run-tool" in sys.argv
                                     else "studio.log"), "a", encoding="utf-8",
                        errors="replace", buffering=1)
        except Exception:
            _log = open(os.devnull, "w")
        if sys.stdout is None:
            sys.stdout = _log
        if sys.stderr is None:
            sys.stderr = _log
    # child processes (tools, objdump, ffmpeg...) must not pop up console
    # windows either: default every Popen to CREATE_NO_WINDOW
    import subprocess as _sp
    _popen_init = _sp.Popen.__init__

    def _quiet_init(self, *a, **kw):
        if not kw.get("creationflags"):
            kw["creationflags"] = 0x08000000          # CREATE_NO_WINDOW
        _popen_init(self, *a, **kw)
    _sp.Popen.__init__ = _quiet_init
    # tools print non-ASCII (arrows, +-); the Windows console codepage can't
    # encode them -> UnicodeEncodeError. UTF-8 everywhere, never crash.
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    # emulator sound goes through macOS AudioToolbox: off on Windows (v1)
    os.environ.setdefault("XDJ_DECK_SOUND", "0")
# where the app may WRITE: never inside a packaged app (that breaks its code
# signature -> macOS can call it "damaged"); the user's support folder
WCACHE = os.path.join(app_support_dir(), "cache") if FROZEN else None
if FROZEN:                    # tools' screenshots (play test, UPD boot test)
    os.environ.setdefault("XDJ_SHOTS", os.path.join(WCACHE, "shots"))
    os.makedirs(os.environ["XDJ_SHOTS"], exist_ok=True)
HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE = getattr(sys, "_MEIPASS", os.path.dirname(HERE)) if FROZEN else os.path.dirname(HERE)
UIHTML = os.path.join(BUNDLE, "studio", "ui.html") if FROZEN else os.path.join(HERE, "ui.html")

APP_SUPPORT = app_support_dir()
# the app's own release number (GitHub release tag v<APP_VERSION>)
APP_VERSION = "1.0.9.2"
RELEASES_API = "https://api.github.com/repos/inter55/xdj-mod-studio/releases/latest"
RELEASES_PAGE = "https://github.com/inter55/xdj-mod-studio/releases/latest"
SETTINGS = os.path.join(APP_SUPPORT, "studio.json")


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def settings():
    return _read_json(SETTINGS, {})


def save_settings(s):
    os.makedirs(APP_SUPPORT, exist_ok=True)
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=1)
    os.replace(tmp, SETTINGS)


def _bundle_repo():
    """Inside a shipped app, the repo travels with the bundle. Mac:
    <App>/Contents/Resources/repo (server exe lives in .../server/);
    Windows: repo/ next to XDJ Mod Studio.exe."""
    if not getattr(sys, "frozen", False):
        return None
    try:
        srv = os.path.dirname(os.path.realpath(sys.executable))
        for cand in (os.path.join(srv, "..", "repo"), os.path.join(srv, "repo")):
            cand = os.path.abspath(cand)
            if os.path.isdir(os.path.join(cand, "tools")):
                return cand
    except Exception:
        pass
    return None


def find_project():
    """The project folder: firmware (.UPD), cache/, build/, mods/, tools/.
    In a shipped app the bundled repo is the fallback when no project
    folder is chosen in Settings; else XDJ_ROOT; else the folder the app
    was built from; else (development) source."""
    cands = [settings().get("project"), _bundle_repo(),
             os.environ.get("XDJ_ROOT")]
    try:
        from emu._buildinfo import REPO
        cands.append(REPO)
    except Exception:
        pass
    cands.append(os.path.dirname(HERE))
    for c in cands:
        if c and os.path.isdir(os.path.join(c, "tools")):
            return os.path.abspath(c)
    return os.path.abspath(next((c for c in cands if c and os.path.isdir(c)),
                                os.path.dirname(HERE)))


ROOT = find_project()
os.environ["XDJ_ROOT"] = ROOT          # emu.paths reads this (before emu loads)
# Every deck module is looked up as a path relative to the project root
# (build/themesw/ts.bin, build/stems/...), which is also where a checkout is
# run from. A packaged app is started with cwd "/" (Finder, Windows shortcut),
# so pin it: without this the emulator fails with "deck module not built and
# no compiler here: missing ['build/themesw/ts.bin', ...]".
try:
    os.chdir(ROOT)
except OSError:
    pass
# Faster emulator defaults (safe/fast by construction: identical byte results
# except I/O batching, verified in tools/usb_fast_bench.py + /tmp/usbfast_emu.py)
os.environ.setdefault("XDJ_USB_FAST", "1")
os.environ.setdefault("XDJ_USB_FAST_OUT", "0")
# Playable by default: the HLE DSP serves the deck's DSP mailbox so a
# loaded track PLAYS (no E-8302) and the TIME digits run. The plain
# snapshot has no DSP behind it; without this, play raises E-8302.
# Off with XDJ_HLE_DSP=0 (byte-identical stock behaviour for tests).
# Deck sound (Mac-side audible audio of the track) stays default-on via
# emu/decksound's own gate.
os.environ.setdefault("XDJ_HLE_DSP", "1")
# vendor/ carries the MIT rekordbox-pdb package (stems_replace, filter_playlists,
# onelibrary_to_pdb); it must be importable from the app, not just a checkout.
if FROZEN:      # bundled emu/studio win; project tools/mods are data on disk
    sys.path.extend([os.path.join(ROOT, "tools"), os.path.join(ROOT, "mods"),
                     os.path.join(ROOT, "vendor")])
else:
    sys.path[:0] = [ROOT, os.path.join(ROOT, "tools"), os.path.join(ROOT, "mods"),
                    os.path.join(ROOT, "vendor")]
# a checkout of the public repo has no build/ (gitignored): stage the
# committed prebuilt deck modules once, so deck installs and UPD builds
# find them wherever the app runs from (packaged apps already carry them)
try:
    import shbuild
    shbuild.stage_prebuilt(ROOT)
except Exception:
    pass

import numpy as np
from PIL import Image

PY = sys.executable
DEMUCS_PY = os.path.expanduser("~/.xdj700-studio/venv/Scripts/python.exe"
                               if WINDOWS else "~/.xdj700-studio/venv/bin/python")
AUDIO_EXT = (".wav", ".aif", ".aiff", ".mp3", ".flac", ".m4a")


def stick_path():
    return settings().get("stick") or ""


LOCK = threading.Lock()
RENDER = threading.Lock()           # numpy/Pillow renders one at a time
JOBS = {}
NEXT = [1]


# ----------------------------------------------------------------- jobs ---
def job(title, fn, *args):
    with LOCK:
        jid = NEXT[0]
        NEXT[0] += 1
        JOBS[jid] = {"id": jid, "title": title, "state": "running",
                     "log": [], "t0": time.time(), "frac": None, "note": "",
                     "hist": [], "eta": None, "wait": False}

    def log(msg):
        with LOCK:
            JOBS[jid]["log"].append(str(msg))

    def prog(frac=None, note=None, eta=None, wait=None):
        """Job progress for the UI: frac 0..1 (None = indeterminate) and a
        human-readable step note. Cheap enough to call per log line.
        eta = the job's OWN estimate of seconds left (overrides the rate
        estimate); wait = True while the job waits for the user (no ETA)."""
        with LOCK:
            j = JOBS[jid]
            if frac is not None:
                f = max(0.0, min(1.0, float(frac)))
                if j["frac"] is None or f != j["frac"]:
                    j["hist"].append((time.time(), f))
                    del j["hist"][:-400]
                j["frac"] = f
            if note:
                j["note"] = str(note)
            if eta is not None:
                j["eta"] = (time.time(), float(eta))
            if wait is not None:
                j["wait"] = bool(wait)

    def run():
        try:
            fn(log, prog, *args)
            st = "done"
        except Exception as e:
            log(f"ERROR: {e}")
            log(traceback.format_exc().splitlines()[-1])
            st = "failed"
            try:                       # keep failures after restarts: one
                os.makedirs(WCACHE or os.path.join(ROOT, "cache"), exist_ok=True)
                with open(os.path.join(WCACHE or os.path.join(ROOT, "cache"),
                                       "studio_jobs_errors.log"),
                          "a") as f:
                    f.write("\n" + "=" * 70 + f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                            f"{JOBS[jid]['title']}\n")
                    f.write("\n".join(JOBS[jid]["log"][-60:]) + "\n")
            except Exception:
                pass
        _SCAN.clear()
        with LOCK:
            JOBS[jid]["state"] = st
            JOBS[jid]["t1"] = time.time()      # GitHub #14: the timer stops here
    threading.Thread(target=run, daemon=True).start()
    return jid


def run_tool(log, args):
    """Run a project tool, streaming its output into the job log. In the
    packaged app the tool runs on the bundled interpreter (no .venv)."""
    cmd = [PY, "--run-tool"] + args if FROZEN else [PY, "-u"] + args
    # 2026-11-13 (Windows, Reddit): a job sat 31 min with an EMPTY log, so
    # nobody could tell whether the tool had even started. Say what runs,
    # and say it again every 2 minutes the tool stays silent.
    log(f"[app] starting {os.path.basename(args[0])}")
    p = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace")
    log(f"[app] {os.path.basename(args[0])} is running (process {p.pid})")
    out, last = [], [time.time()]

    def watchdog():
        while p.poll() is None:
            time.sleep(10)
            quiet = time.time() - last[0]
            if quiet >= 120 and p.poll() is None:
                log(f"[app] no output from {os.path.basename(args[0])} for "
                    f"{quiet / 60:.0f} min (process {p.pid} still running)")
                last[0] = time.time()
    threading.Thread(target=watchdog, daemon=True).start()
    for line in p.stdout:
        last[0] = time.time()
        line = line.rstrip()
        out.append(line)
        log(line)
    p.wait()
    log(f"[app] {os.path.basename(args[0])} finished (exit code {p.returncode})")
    return p.returncode, "\n".join(out)


def run_tool_main(argv):
    """`app --run-tool script.py args...`: execute a project script as
    __main__ (what `python script.py` would do), inside the bundled runtime."""
    import runpy
    script = os.path.join(ROOT, argv[0]) if not os.path.isabs(argv[0]) else argv[0]
    if script.endswith(".py") and not os.path.exists(script) \
            and os.path.exists(script + "c"):
        script += "c"           # tester bundle ships compiled tools only
    sys.argv = [script] + argv[1:]
    sys.path.insert(0, os.path.dirname(script))
    # a frozen runtime ignores PYTHONPATH; honour it (the mock deck's
    # stand-in `hid` module for the USB-stick pack recorder)
    for d in reversed([x for x in os.environ.get("PYTHONPATH", "").split(os.pathsep) if x]):
        sys.path.insert(0, d)
    sys.stdout.reconfigure(line_buffering=True)
    runpy.run_path(script, run_name="__main__")


# --------------------------------------------------------------- tracks ---
_SCAN = {}
_SCAN_TTL = 20
_SCAN_BUSY = set()
_SCAN_LOCK = threading.Lock()


def scan(stick):
    """Library scan, stale-while-revalidate: the first call for a stick
    blocks, later calls answer from the cache at once (the previews and the
    track list all ask for it) and refresh in the background once the entry
    is older than _SCAN_TTL -- a slow rescan of a big stick no longer
    stalls every preview and tab switch. Jobs invalidate it when they
    finish (_SCAN.clear())."""
    hit = _SCAN.get(stick)
    if hit and time.time() - hit[0] < _SCAN_TTL:
        return hit[1]
    if hit:
        with _SCAN_LOCK:
            if stick not in _SCAN_BUSY:
                _SCAN_BUSY.add(stick)

                def _refresh(st=stick):
                    try:
                        _SCAN[st] = (time.time(), _scan(st))
                    except Exception:
                        pass
                    finally:
                        with _SCAN_LOCK:
                            _SCAN_BUSY.discard(st)
                threading.Thread(target=_refresh, daemon=True).start()
        return hit[1]
    res = _scan(stick)
    _SCAN[stick] = (time.time(), res)
    return res


def _scan(stick):
    import make_3band as M3
    if not stick or not os.path.isdir(stick):
        return {"tracks": [], "versions": [], "name": "", "disk": None}
    rows = []
    for d in sorted(glob.glob(os.path.join(stick, "PIONEER", "USBANLZ", "*", "*"))):
        try:
            audio = M3.track_path(stick, d)
        except Exception:
            audio = None
        if not audio:
            continue
        ext = os.path.join(d, "ANLZ0000.EXT")
        try:
            eb = open(ext, "rb").read() if os.path.exists(ext) else b""
            # ready = overview (PWV6) AND centre (PWV7) data in the .EXT
            has3 = bool(eb) and M3.ext_has(eb, b"PWV6") and M3.ext_has(eb, b"PWV7")
        except Exception:
            has3 = False
        base = os.path.splitext(os.path.basename(audio))[0]
        artist, title = (base.split(" - ", 1) + [""])[:2] if " - " in base else ("", base)
        md = track_meta(audio) or {}
        rel = os.path.relpath(d, stick)
        datb = anlz(stick, rel, "ANLZ0000.DAT")
        rows.append({"dir": rel, "audio": audio,
                     "title": md.get("title") or title or base,
                     "artist": md.get("artist") or artist,
                     "duration": md.get("duration"),
                     "bpm": bpm_of(datb),
                     "cues": cues_of(datb, eb),
                     "exists": os.path.exists(audio), "three_band": has3,
                     "has_2ex": os.path.exists(os.path.join(d, "ANLZ0000.2EX")),
                     "has_x7k": os.path.exists(audio + ".x7k"),
                     "has_sidecar": os.path.exists(audio + ".x7s"),
                     "has_stems": os.path.exists(out_path(audio, "STEMS"))
                     or os.path.exists(audio + STEMS_BACKUP),
                     "can_replace": can_replace(audio)})
    # files written by the studio (not yet analysed by rekordbox):
    # STEMS-4 stems files ("XST4" trailer) and, if any remain from older
    # builds, key/stem VERSION files ("XVTR"). Both load on the deck from
    # beside the original track; the deck's BROWSE > FOLDER list does not open
    vers, stemsfiles = [], []
    for p in glob.glob(os.path.join(stick, "**", "*"), recursive=True):
        if not p.lower().endswith(AUDIO_EXT):
            continue
        try:
            sz = os.path.getsize(p)
        except OSError:
            sz = 0
        if is_stems_file(p):
            stemsfiles.append({"path": os.path.relpath(p, stick),
                               "mb": round(sz / 1e6, 1)})
        elif is_version_file(p):
            vers.append({"path": os.path.relpath(p, stick),
                         "names": version_names(p), "mb": round(sz / 1e6, 1)})
    fill_meta([r["audio"] for r in rows if r["exists"]])
    try:
        du = shutil.disk_usage(stick)
        disk = {"free": du.free, "total": du.total}
    except OSError:
        disk = None
    sidecar_mb = 0.0
    for r in rows:
        try:
            if os.path.exists(r["audio"] + ".x7s"):
                sidecar_mb += os.path.getsize(r["audio"] + ".x7s") / 1e6
        except OSError:
            pass
    stems_mb = round(sum(v.get("mb", 0) for v in vers + stemsfiles) + sidecar_mb, 1)
    return {"tracks": rows, "versions": vers, "stems_files": stemsfiles,
            "name": os.path.basename(stick.rstrip("/")),
            "disk": disk, "stems_mb": stems_mb}


def is_version_file(p):
    try:
        with open(p, "rb") as f:
            f.seek(-128, 2)
            return f.read(4) == b"XVTR"
    except OSError:
        return False


def is_stems_file(p):
    """STEMS-4 (tools/stems4.py): 128-B 'XST4' trailer at the end."""
    try:
        with open(p, "rb") as f:
            f.seek(-128, 2)
            return f.read(4) == b"XST4"
    except OSError:
        return False


def version_names(p):
    with open(p, "rb") as f:
        f.seek(-128, 2)
        tr = f.read(128)
    n = int.from_bytes(tr[12:14], "little")
    names = [tr[48 + 8 * k:56 + 8 * k].rstrip(b"\0").decode(errors="replace")
             for k in range(n)]
    kc = tr[14]
    key = None
    if kc:
        import keydetect
        key = keydetect.name((kc - 1) // 2, (kc - 1) % 2)
    return {"versions": names, "key": key}


def eta_left(j, now=None):
    """Seconds left for a running job, or None (= say "estimating").
    Explicit job estimate first; else the RECENT progress rate (last
    ~2 min) blended with the overall rate, both measured up to NOW -- so a
    stalled step makes the estimate grow instead of freezing at "almost
    done". Startup time before the first progress is not counted."""
    now = now or time.time()
    if j["state"] != "running" or j.get("wait"):
        return None
    if j.get("eta"):
        t, e = j["eta"]
        return max(5.0, e - (now - t))
    h, f = j.get("hist") or [], j.get("frac")
    if f is None or len(h) < 2 or f >= 1.0:
        return None
    t_first, f_first = h[0]
    if now - t_first < 8 or f - f_first < 0.01:
        return None
    overall = (f - f_first) / (now - t_first)
    old = [p for p in h if p[0] <= now - 120]
    t_w, f_w = old[-1] if old else h[0]
    recent = (f - f_w) / max(1e-6, now - t_w)
    rate = 0.6 * recent + 0.4 * overall
    if rate <= 0:
        return None
    return (1.0 - f) / rate


def job_view(j):
    v = {k: j[k] for k in ("id", "title", "state", "log", "t0", "frac",
                           "note", "wait")}
    v["t1"] = j.get("t1")
    v["left"] = eta_left(j)
    return v


def out_path(audio, tag):
    base, ext = os.path.splitext(audio)
    ext = ".aiff" if ext.lower() in (".aif", ".aiff") else ".wav"
    return f"{base} ({tag}){ext}"


def j_3band(log, prog, stick, dirs, compute_all):
    import make_3band as M3
    dirs = dirs or [r["dir"] for r in scan(stick)["tracks"]]
    n = len(dirs) or 1
    for i, d in enumerate(dirs):
        prog(i / n, f"computing 3-band waveform {i + 1} of {n}")
        log(f"[{i + 1}/{len(dirs)}] {d}: "
            f"{M3.patch_dir(stick, os.path.join(stick, d), compute_all)}")
    prog(1.0, "done")


# (pre-rendered key versions removed 2026-10-14: the deck gets real-time
# key shift instead, so the Mac-side KEYS builder is gone)


_DEMUCS = [0.0, False]


def _find_torch_python():
    """A Python PyTorch supports (3.11 / 3.12) for the Demucs environment."""
    for name in ("python3.12", "python3.11"):
        p = shutil.which(name)
        if p:
            return [p]
    if WINDOWS and shutil.which("py"):
        for v in ("-3.12", "-3.11"):
            r = subprocess.run(["py", v, "-c", "print(1)"], capture_output=True)
            if r.returncode == 0:
                return ["py", v]
    for p in ("/opt/homebrew/bin/python3.12", "/usr/local/bin/python3.12",
              "/opt/homebrew/bin/python3.11", "/usr/local/bin/python3.11"):
        if os.path.exists(p):
            return [p]
    return None


def j_demucs_install(log, prog):
    """Stem separation (Demucs + PyTorch, ~2-3 GB) into its own environment
    ~/.xdj700-studio/venv, from inside the app (no Terminal, no source)."""
    py = _find_torch_python()
    if not py:
        raise RuntimeError(
            "Stem separation needs Python 3.11 or 3.12. Mac: install Homebrew "
            "(brew.sh), then in Terminal: brew install python@3.12 ffmpeg -- "
            "Windows: install Python 3.12 from python.org. Then press Install again.")
    env = os.path.dirname(os.path.dirname(DEMUCS_PY))
    os.makedirs(os.path.dirname(env), exist_ok=True)
    log(f"creating the environment with {' '.join(py)}")
    prog(0.02, "Creating the environment")
    subprocess.run(py + ["-m", "venv", env], check=True)
    steps = [(["-m", "pip", "install", "--upgrade", "pip"], 0.05, "Updating pip"),
             (["-m", "pip", "install", "torch", "torchaudio", "demucs", "soundfile"],
              0.9, "Downloading PyTorch + Demucs (2-3 GB, takes a while)")]
    for args, frac, note in steps:
        prog(frac - 0.04, note)
        p = subprocess.Popen([DEMUCS_PY] + args, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace")
        for line in p.stdout:
            line = line.rstrip()
            if line:
                log(line[-200:])
        if p.wait():
            raise RuntimeError("pip failed -- see the log (check the internet connection)")
    r = subprocess.run([DEMUCS_PY, "-c", "import demucs; print('Demucs ready')"],
                       capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("Demucs did not import: " + (r.stderr or "")[-300:])
    _DEMUCS[:] = [0, False]                     # re-check on the next poll
    if not shutil.which("ffmpeg"):
        log("NOTE: ffmpeg is still missing; Make stems needs it "
            "(Mac: brew install ffmpeg -- Windows: winget install ffmpeg).")
    prog(1.0, "Stem separation ready")


def demucs_ok():
    if time.time() - _DEMUCS[0] < 60:           # the page polls; don't spawn each time
        return _DEMUCS[1]
    ok = False
    if os.path.exists(DEMUCS_PY):
        r = subprocess.run([DEMUCS_PY, "-c", "import demucs"], capture_output=True)
        ok = r.returncode == 0
    _DEMUCS[:] = [time.time(), ok]
    return ok


STEMS_BACKUP = ".pre-stems"            # original kept as "Track.aiff.pre-stems"


def can_replace(audio):
    """WAV / AIFF originals can be swapped for their stems file under the
    SAME name: rekordbox's database (and its cues, grid, waveforms) keeps
    pointing at it, so the deck lists it in ARTIST / TRACK. MP3 / M4A get
    there a different way: the stems file is written as WAV, and the library
    row plus its hash-named ANLZ folder move with it (tools/stems_replace.py),
    so cues and waveform survive the extension change."""
    return os.path.splitext(audio)[1].lower() in (
        ".wav", ".aif", ".aiff", ".mp3", ".m4a", ".flac")


def drop_id3_chunk(path):
    """Remove the ID3 tag chunk ffmpeg writes after the audio of a STEMS-4
    file, fixing the stem plane offsets in its trailer. With that chunk a
    track opened from the rekordbox LIBRARY hangs at loading (even on stock
    firmware; emulator 2026-11-02) -- the library supplies title/artist
    anyway, so a file that replaces a library track goes without it."""
    import struct
    big = open(path, "rb").read(4) == b"FORM"
    fmt = ">I" if big else "<I"
    with open(path, "rb") as f:
        b = bytearray(f.read())
    i = 12
    while i + 8 <= len(b):
        cid, n = bytes(b[i:i + 4]), struct.unpack_from(fmt, b, i + 4)[0]
        if cid in (b"ID3 ", b"id3 "):
            cut = 8 + n + (n & 1)
            del b[i:i + cut]
            struct.pack_into(fmt, b, 4, len(b) - 8)
            t = len(b) - 128                     # trailer: rel[4] at +16
            assert bytes(b[t:t + 4]) == b"XST4", "not a STEMS-4 file"
            rel = struct.unpack_from("<4I", b, t + 16)
            struct.pack_into("<4I", b, t + 16, *[r - cut for r in rel])
            with open(path + ".tmp", "wb") as f:
                f.write(b)
            os.replace(path + ".tmp", path)
            return True
        i += 8 + n + (n & 1)
    return False


def j_stems(log, prog, audio, keys=(), quality="opus", replace=True):
    if not demucs_ok():
        raise RuntimeError("Demucs is not installed (Settings > Stem separation)")
    miss = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if miss:                      # say so now, not after minutes of separating
        raise RuntimeError(f"{' and '.join(miss)} not found -- Make stems needs them. "
                           "Mac: brew install ffmpeg (it includes ffprobe); "
                           "Windows: winget install ffmpeg. Then try again.")
    import re
    with tempfile.TemporaryDirectory() as td:
        log("separating with Demucs (htdemucs) -- a few minutes per track")
        # Demucs gets a plain-named copy: accented / long track names broke
        # it on Windows (cp1252 console, MAX_PATH) after the separation had
        # already run -- Reddit "80-100% then failed" (2026-11-01). UTF-8 I/O
        # for the same reason; the tail of its output goes into the error.
        src = os.path.join(td, "input" + os.path.splitext(audio)[1].lower())
        shutil.copyfile(audio, src)
        denv = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        p = subprocess.Popen([DEMUCS_PY, "-m", "demucs", "-n", "htdemucs", "-o", td,
                              src], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, env=denv,
                             encoding="utf-8", errors="replace")
        tail = []
        for line in p.stdout:
            if line.strip():
                tail = (tail + [line.rstrip()[-200:]])[-4:]
                log(line.rstrip()[-160:])
                m = re.search(r"(\d{1,3})%", line)
                if m:
                    prog(0.05 + 0.75 * int(m.group(1)) / 100.0,
                         "separating vocals / drums / bass / other")
        if p.wait():
            raise RuntimeError("demucs failed: " + (" | ".join(tail) or f"exit {p.returncode}"))
        prog(0.80, "building the version file")
        stem_dir = os.path.join(td, "htdemucs", "input")
        if not os.path.isdir(stem_dir):
            raise RuntimeError("demucs wrote no stems (output folder missing)")
        stems = [os.path.join(stem_dir, f"{s}.wav") for s in ("bass", "drums", "vocals", "other")]
        stem_map = {"bass": stems[0], "drums": stems[1],
                    "vocals": stems[2], "other": stems[3]}
        keep = audio                    # the file the deck plays afterwards
        if replace and os.path.splitext(audio)[1].lower() in (
                ".mp3", ".m4a", ".flac"):
            # 1.1: MP3 / M4A / FLAC are replaced in place too. The stems file
            # is written as WAV (AIFF for FLAC, so the name keeps its length)
            # and the library row plus the hash-named ANLZ
            # folder move with it, so the track keeps its cues and waveform
            # (tools/stems_replace.py has the hash and the byte layout).
            prog(0.80, "replacing the track with its stems file")
            import stems_replace as SR
            plan = SR.apply(stick_path(),
                            "/" + os.path.relpath(audio, stick_path()),
                            stems_dir=stem_dir, yes=True)
            keep = plan["target"]
            log(f"done: {os.path.basename(keep)} is now the stems version "
                f"({os.path.getsize(keep) / 1e6:.0f} MB; the original is kept "
                f"as {os.path.basename(audio)}{SR.AUDIO_BAK}). It stays in "
                "ARTIST / TRACK with its cues and waveform; PERFORM > STEMS "
                "mutes vocals / drums / bass / other")
        else:
            out = out_path(audio, "STEMS")
            # STEMS-4 file: the normal mix + 4 stem planes; on the deck the
            # PERFORM > STEMS pads mute / solo them live (tools/stems/sm.c)
            import stems4 as S4
            S4.build(audio, out, stem_map, aiff=out.endswith(".aiff"),
                     progress=lambda f, n: prog(0.80 + 0.12 * f, n))
            size = os.path.getsize(out) / 1e6
            if replace and can_replace(audio):
                # the original moves aside (same folder, a name the deck never
                # lists) and the stems file takes its name
                drop_id3_chunk(out)
                bak = audio + STEMS_BACKUP
                if not os.path.exists(bak):
                    os.replace(audio, bak)
                os.replace(out, audio)
                log(f"done: {os.path.basename(audio)} is now the stems version "
                    f"({size:.0f} MB; the original is kept as "
                    f"{os.path.basename(bak)}). On the deck it is in the usual "
                    "lists; PERFORM > STEMS mutes vocals / drums / bass / other")
            else:
                log(f"done: {os.path.basename(out)}  ({size:.0f} MB) -- it sits "
                    "next to the original. BROWSE > FOLDER opens on the deck "
                    "(checked on hardware 2026-11-07) but lists only folders at "
                    "the root of the stick, not the Contents folder the track "
                    "is in, and SEARCH only covers the rekordbox database, so "
                    "it cannot be reached there. "
                    "Run this again with Replacement on to keep the track in "
                    "ARTIST / TRACK. "
                    + ("Replacement is switched off. " if not replace else "")
                    + "PERFORM > STEMS then mutes vocals / drums / bass / other")
        # X7S sidecar next to the audio (quality choice from the dialog)
        try:
            import x7s as X7
            prog(0.92, "encoding the stems sidecar")
            preset = {"opus": ("opus", 96, {"bass": 64}),
                      "mp3": ("mp3", 128, None),
                      "flac": ("flac", 0, None)}[quality]
            sp = {s: stems[i] for i, s in
                  enumerate(("bass", "drums", "vocals", "other"))}
            data = X7.build(sp, codec=preset[0], bitrate=preset[1],
                            bitrates=preset[2])
            xp = keep + ".x7s"
            open(xp, "wb").write(data)
            log(f"x7k/x7s sidecar {quality}: {os.path.basename(xp)} "
                f"({len(data) / 1e6:.1f} MB)")
        except Exception as e:
            log(f"x7s sidecar skipped: {e}")
        prog(1.0, "done")


def j_x7s(log, prog, audio):
    """Opus v0: decode the .x7s sidecar into the deck-playable stems file."""
    import x7s_to_versions as X2V
    prog(0.05, "decoding the opus sidecar (delay-aligned to the mix)")
    log(f"decoding sidecar -> deck file for {os.path.basename(audio)}")
    out = X2V.build_versions(audio)
    prog(0.95, "deck file written")
    log(f"done: {os.path.basename(out)} ({os.path.getsize(out) / 1e6:.0f} MB)")
    log("load it on the deck from folder view; STEMS panel mutes/solos")
    prog(1.0, "done")


# ------------------------------------------------------- track metadata ---
META_FILE = os.path.join(WCACHE or os.path.join(ROOT, "cache"), "studio_meta.json")
_META = None
_META_BUSY = [False]


def meta_cache():
    global _META
    if _META is None:
        try:
            _META = json.load(open(META_FILE))
        except Exception:
            _META = {}
    return _META


def _probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration:format_tags=artist,title,ARTIST,TITLE",
                        "-of", "json", path], capture_output=True, text=True)
    f = json.loads(r.stdout or "{}").get("format", {})
    tags = {k.lower(): v for k, v in (f.get("tags") or {}).items()}
    return {"duration": float(f.get("duration") or 0),
            "artist": tags.get("artist", ""), "title": tags.get("title", "")}


def fill_meta(paths):
    """Background ffprobe of every track (cached on disk by path+mtime)."""
    if _META_BUSY[0]:
        return
    _META_BUSY[0] = True

    def run():
        try:
            m = meta_cache()
            n = 0
            for p in paths:
                try:
                    key = f"{p}|{os.path.getmtime(p)}"
                except OSError:
                    continue
                if key in m:
                    continue
                m[key] = _probe(p)
                n += 1
                if n % 25 == 0:
                    _save_meta()
            _save_meta()
        finally:
            _META_BUSY[0] = False
    threading.Thread(target=run, daemon=True).start()


def _save_meta():
    os.makedirs(os.path.dirname(META_FILE), exist_ok=True)
    with LOCK:
        tmp = META_FILE + ".tmp"
        json.dump(meta_cache(), open(tmp, "w"))
        os.replace(tmp, META_FILE)


def track_meta(p):
    try:
        return meta_cache().get(f"{p}|{os.path.getmtime(p)}")
    except OSError:
        return None


def anlz(stick, d, fn):
    try:
        return open(os.path.join(stick, d, fn), "rb").read()
    except OSError:
        return b""


def bpm_of(dat):
    import make_3band as M3
    e = M3.section_entries(dat, b"PQTZ")
    if e and len(e) >= 8:
        return int.from_bytes(e[2:4], "big") / 100.0
    return None


def cues_of(*anlz_bytes):
    """How many hot cues (A..H) the track's EXPORTED analysis carries.
    The deck's A-C / D-F / G-H pages read these -- cues set in rekordbox
    after the stick was exported never reach it, which is the first thing
    to check when the pages do not appear."""
    nums = set()
    for b in anlz_bytes:
        if len(b) < 32:
            continue
        p = int.from_bytes(b[4:8], "big")       # first section offset
        while p + 12 <= len(b):
            tag, ln = b[p:p + 4], int.from_bytes(b[p + 8:p + 12], "big")
            if ln == 0 or p + ln > len(b):
                break
            # PCOB (legacy list, A-C on a real rekordbox export) and PCO2
            # (the .EXT's extended list, the only place D-H are written;
            # the deck pads read both, GitHub #6). Both entries carry the
            # cue number as u32 at +0x0C.
            if tag in (b"PCOB", b"PCO2") and p + 16 <= len(b) \
                    and int.from_bytes(b[p + 12:p + 16], "big") == 1:
                # entries start after the section's own header (0x18 PCOB,
                # 0x14 PCO2)
                q, end = p + int.from_bytes(b[p + 4:p + 8], "big"), p + ln
                while q + 16 <= end:
                    if b[q:q + 4] in (b"PCPT", b"PCP2"):
                        step = int.from_bytes(b[q + 8:q + 12], "big") or 0x38
                        num = int.from_bytes(b[q + 14:q + 16], "big")
                        if 1 <= num <= 8 or 0x41 <= num <= 0x48:
                            nums.add(num if num <= 8 else num - 0x40)
                        q += step
                    else:
                        q += 8
            p += ln
    return len(nums)


def _cues_paths(stick, audio):
    """(folder, files) for a library track's analysis; refuses anything that
    is not a track inside the library folder."""
    stick = os.path.realpath(os.path.expanduser(stick or ""))
    p = os.path.realpath(os.path.expanduser(audio or ""))
    if not stick or not p.startswith(stick + os.sep):
        raise ValueError("that track is outside your library folder")
    d = os.path.dirname(p)
    files = [f for f in ("ANLZ0000.DAT", "ANLZ0000.EXT")
             if os.path.isfile(os.path.join(d, f))]
    if not files:
        raise ValueError("this track has no analysis file on the stick yet "
                         "(export it from rekordbox first)")
    return d, files


def cues_read(stick, audio):
    """The hot cues (A..H = 1..8, ms) the deck will read for this track."""
    import anlz_cues as AC
    d, files = _cues_paths(stick, audio)
    cues = {}
    for f in files:
        with open(os.path.join(d, f), "rb") as fh:
            cues.update(AC.read_cues(fh.read()))
    return {"cues": {str(n): cues[n] for n in sorted(cues)}, "files": files}


def cues_edit(stick, audio, set_cues=None, clear=()):
    """Write and/or delete hot cues in the track's own analysis file(s).
    set_cues = {1..8: ms}, clear = [1..8]. Returns the state after the edit.

    Only the hot-cue section is rewritten; the waveforms, beat grid and
    everything else in the file are copied byte for byte (anlz_cues.py),
    so this cannot damage a track the deck already plays."""
    import anlz_cues as AC
    try:
        set_cues = {int(k): int(v) for k, v in (set_cues or {}).items()}
        clear = [int(n) for n in (clear or ())]
    except (TypeError, ValueError):
        raise ValueError("cues are numbered 1..8 (A..H), times in milliseconds")
    for n, ms in set_cues.items():
        if not 1 <= n <= 8:
            raise ValueError("cues are numbered 1..8 (A..H)")
        if not 0 <= ms <= 86_400_000:
            raise ValueError("that cue time is not a real position")
    if any(not 1 <= n <= 8 for n in clear):
        raise ValueError("cues are numbered 1..8 (A..H)")
    d, files = _cues_paths(stick, audio)
    for f in files:
        p = os.path.join(d, f)
        if clear:
            AC.clear_cues(p, clear)
        if set_cues:
            AC.write_cues(p, set_cues)
    _SCAN.clear()           # the track list shows the A-H tag from this
    return cues_read(stick, audio)


# ------------------------------------------------------------ waveforms ---
STOCK_BLUE = 0x6D1E                 # the stock overview colour (emu-sampled)
_3B_CACHE = os.path.join(WCACHE or os.path.join(ROOT, "cache"), "studio_3band")


def three_band_entries(stick, d):
    import make_3band as M3
    for fn in ("ANLZ0000.EXT", "ANLZ0000.2EX"):
        e = M3.section_entries(anlz(stick, d, fn))
        if e and len(e) == 3600:
            return e, "rekordbox" if fn.endswith("2EX") else "on stick"
    os.makedirs(_3B_CACHE, exist_ok=True)
    cp = os.path.join(_3B_CACHE, d.replace("/", "_") + ".bin")
    if os.path.exists(cp):
        return open(cp, "rb").read(), "computed"
    try:
        audio = M3.track_path(stick, os.path.join(stick, d))
        if not audio or not os.path.exists(audio):
            return None, None
        e = M3.to_entries(M3.band_rms(M3.decode_mono(audio)))
        open(cp, "wb").write(e)
        return e, "computed"
    except Exception:
        return None, None


def hex565(h):
    h = h.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def wave_cols(stick, d, style, w):
    """[col][k] RGB565 (k = 0 bottom) exactly like the deck's overview.

    Four modes (not four colours):
      blue   = stock mono waveform (STOCK_BLUE, PWAV heights)
      rgb    = existing RGB rendering (PWV5 mean / PWV4), unchanged
      3band  = existing 3-band rendering, unchanged
      custom = separate mono waveform in the user's colour (PWAV heights)
    """
    import rgb_overview as RO
    import theme_fixed as TF
    if style == "rgb":
        ext = anlz(stick, d, "ANLZ0000.EXT")
        import make_3band as M3
        p5 = M3.section_entries(ext, b"PWV5")
        if p5:
            return RO.model_p5_mean(p5)
        p4 = M3.section_entries(ext, b"PWV4")
        return RO.model_pixels(p4, 1) if p4 else None
    if style == "3band":
        e, _ = three_band_entries(stick, d)
        if not e:
            return None
        cols = RO.model_pixels(e, 2)
        # NB rgb_overview names are inverted: C_HIGH = cream CORE (lows),
        # C_MID = orange (mids), C_LOW = blue OUTER layer (highs)
        remap = {RO.C_HIGH: hex565(w["low"]) if w.get("low") else RO.C_HIGH,
                 RO.C_MID: hex565(w["mid"]) if w.get("mid") else RO.C_MID,
                 RO.C_LOW: hex565(w["high"]) if w.get("high") else RO.C_LOW}
        return [[remap.get(c, c) for c in col] for col in cols]
    import make_3band as M3
    pw = M3.section_entries(anlz(stick, d, "ANLZ0000.DAT"), b"PWAV")
    if not pw:
        return None
    col = STOCK_BLUE
    if style == "custom":
        try:
            col = hex565(w.get("custom") or "#6ba3f0")
        except Exception:
            col = STOCK_BLUE
    elif style == "blue":
        # Legacy compat: very old saved themes stored a custom tint under
        # style=blue + tint=#hex. Honour it so old previews still render;
        # new saves use style=custom instead. Stock blue otherwise.
        t = w.get("tint")
        if isinstance(t, str) and t.startswith("#"):
            try:
                col = hex565(t)
            except Exception:
                col = STOCK_BLUE
        elif t == "theme" and w.get("spec"):
            import themes as T
            col = int(T.map_colours(np.array([STOCK_BLUE]), w["spec"])[0])
    out = []
    for c in range(RO.COLS):
        b = pw[c * len(pw) // RO.COLS]
        h = (b & 31) * RO.ROWS // 31
        out.append([col if k < h else 0 for k in range(RO.ROWS)])
    return out


def cols_to_rgb(cols, rows):
    a = np.zeros((rows, len(cols)), np.uint32)
    for x, col in enumerate(cols):
        for k, c in enumerate(col[:rows]):
            a[rows - 1 - k, x] = c
    return np.dstack([((a >> 11) & 31) * 255 // 31, ((a >> 5) & 63) * 255 // 63,
                      (a & 31) * 255 // 31]).astype(np.uint8)


def wave_png(stick, d, style, w):
    cols = wave_cols(stick, d, style, w)
    if cols is None:
        img = np.zeros((34, 500, 3), np.uint8)
    else:
        img = cols_to_rgb(cols, 34)
    bio = io.BytesIO()
    Image.fromarray(img).save(bio, "PNG")
    return bio.getvalue()


def demo_track(stick):
    """A track with complete waveform data for previews: overview (PWAV in
    DAT) + RGB overview/centre (PWV5 in EXT) + stock centre (PWV3 in EXT) +
    3-band (PWV6 in EXT or .2EX). Most complete wins; ties go to the
    longest audio file. Deterministic. Falls back to any usable track."""
    import make_3band as M3
    tracks = scan(stick)["tracks"]
    # Reading every track's ANLZ .DAT + .EXT (a few MB each) over USB took
    # minutes on a 1100-track stick, on EVERY preview. Remember the pick per
    # library (track count is the cheap "has it changed" key: in memory and
    # on disk) and stop looking once enough complete tracks have been seen.
    ck = f"{stick}|{len(tracks)}"
    hit = _DEMO_CACHE.get(ck)
    if hit is not None:
        return hit or None
    disk = _read_json(DEMO_CACHE_FILE, {})
    if disk.get("key") == ck and (not disk.get("dir") or os.path.isdir(
            os.path.join(stick, disk["dir"]))):
        _DEMO_CACHE[ck] = disk.get("dir") or ""
        return disk.get("dir") or None
    best = None
    complete = 0
    for r in tracks:
        if not r["exists"]:
            continue
        dd = os.path.join(stick, r["dir"])
        try:
            dat = open(os.path.join(dd, "ANLZ0000.DAT"), "rb").read()
            ext = open(os.path.join(dd, "ANLZ0000.EXT"), "rb").read()
        except OSError:
            continue
        has = lambda b, t: M3.section_entries(b, t) is not None
        score = (has(dat, b"PWAV") + has(ext, b"PWV5") + has(ext, b"PWV3")
                 + (has(ext, b"PWV6")
                    or os.path.exists(os.path.join(dd, "ANLZ0000.2EX"))))
        if score < 3:
            continue
        try:
            size = os.path.getsize(r["audio"])
        except OSError:
            size = 0
        key = (score, size)
        if best is None or key > best[0]:
            best = (key, r["dir"])
        if score == 4:
            complete += 1
            if complete >= DEMO_CANDIDATES:
                break
    out = best[1] if best else next((r["dir"] for r in tracks if r["exists"]), None)
    _DEMO_CACHE[ck] = out or ""
    try:
        os.makedirs(os.path.dirname(DEMO_CACHE_FILE), exist_ok=True)
        with open(DEMO_CACHE_FILE, "w") as f:
            json.dump({"key": ck, "dir": out or ""}, f)
    except OSError:
        pass
    return out


_DEMO_CACHE = {}
DEMO_CANDIDATES = 24        # complete tracks to compare before picking
DEMO_CACHE_FILE = os.path.join(APP_SUPPORT, "cache", "demo_track.json")


STOCK_DETAIL = (0x09F1, 0x0AF7, 0x037F, 0x0D5E, 0x55BE, 0x757F, 0x757F, 0xCF3F)
CENTRE = {"main": dict(x0=116, cx=366, cy=240, rmax=55),      # playhead x 366
          "perform": dict(x0=116, cx=366, cy=100, rmax=34)}


def _tint_spec(c565):
    """A theme spec that moves the stock blue onto colour c565."""
    import colorsys
    r, g, b = ((c565 >> 11) & 31) / 31, ((c565 >> 5) & 63) / 63, (c565 & 31) / 31
    h, sat, v = colorsys.rgb_to_hsv(r, g, b)
    return {"hue": h * 360, "sat": 1.0 + sat, "val": 0.6 + v * 0.5,
            "spread": 0.3, "vivid": sat * 0.6}


def centre_cols(stick, d, style, w, pos_frac=0.4, step=2):
    """[(colour, rows)] x 500 for the CENTRE waveform with the deck's maths:
    RGB = 1000 MK2 colour detail from PWV5 (rgb_detail.expected_column);
    3band = stacked per-row colours from rekordbox PWV7 (.EXT copy, else
    .2EX; rgb_detail.expected_band_column = what tools/centre/cw.c draws);
    blue = the stock PWV3 painter (palette[whiteness], height).
    custom = separate mono mode: same stock PWV3 heights, palette mapped
    onto the user's colour."""
    import make_3band as M3
    import rgb_detail as RD
    ext = anlz(stick, d, "ANLZ0000.EXT")
    out = []
    if style == "rgb":
        p5 = M3.section_entries(ext, b"PWV5")
        if not p5:
            return None
        n = len(p5) // 2
        pos = int(n * pos_frac)
        for x in range(500):
            first = pos + (x - 250) * step
            if first < 0 or first >= n:
                out.append((0, 0))
                continue
            out.append(RD.expected_column(p5, first, step))
        return out
    if style == "3band":
        sec = None
        for fn in ("ANLZ0000.EXT", "ANLZ0000.2EX"):
            try:
                sec = RD.section_raw(anlz(stick, d, fn), b"PWV7")
                break
            except (SystemExit, OSError, TypeError):
                pass
        if sec:
            n = RD.expected_band_count(sec)
            pos = int(n * pos_frac)
            for x in range(500):
                first = pos + (x - 250) * step
                if first < 0 or first >= n:
                    out.append((0, 0))
                    continue
                rows = RD.expected_band_column(sec, first, step)
                out.append((rows, len(rows)))
            return out
    p3 = M3.section_entries(ext, b"PWV3")
    if not p3:
        return None
    import themes as T
    pal = np.array(STOCK_DETAIL, np.uint16)
    if style == "custom":
        try:
            pal = T.map_colours(pal, _tint_spec(hex565(w.get("custom") or "#6ba3f0")))
        except Exception:
            pass
    elif style == "blue" and isinstance(w.get("tint"), str) and w.get("tint", "").startswith("#"):
        # Legacy compat with old blue+custom-tint saves.
        try:
            pal = T.map_colours(pal, _tint_spec(hex565(w["tint"])))
        except Exception:
            pass
    pal = [int(c) for c in pal]
    n = len(p3)
    pos = int(n * pos_frac)
    for x in range(500):
        i0 = pos + (x - 250) * step
        if i0 < 0 or i0 >= n:
            out.append((0, 0))
            continue
        best = max(p3[i0:i0 + step], key=lambda b: b & 31)
        out.append((pal[min(7, best >> 5)], max(1, best & 31)))
    return out


def draw_centre(fb, cols, screen):
    g = CENTRE[screen]
    for x, (c, rows) in enumerate(cols):
        sx = g["x0"] + x
        if not rows or sx == g["cx"]:
            continue
        r = min(g["rmax"], rows if screen == "main" else rows * g["rmax"] // 55)
        if isinstance(c, list):          # 3-band: one colour per row
            for k in range(r + 1):
                ck = c[min(len(c) - 1, k * rows // max(1, r))]
                fb[g["cy"] - k, sx] = ck
                fb[g["cy"] + k, sx] = ck
            continue
        fb[g["cy"] - r:g["cy"] + r + 1, sx] = c


def track_label(stick, d):
    for r in scan(stick)["tracks"]:
        if r["dir"] == d:
            return r
    return None


def _font(size):
    from PIL import ImageFont
    for path, idx in (("/System/Library/Fonts/HelveticaNeue.ttc", 0),
                      ("/System/Library/Fonts/Helvetica.ttc", 0),
                      (os.path.join(os.environ.get("WINDIR", "C:\\Windows"),
                                    "Fonts", "arialbd.ttf"), 0)):
        try:
            return ImageFont.truetype(path, size, index=idx)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


BPM_BOX = (699, 403, 782, 456)      # the deck's BPM cell on the main screen


def draw_track_text(rgb, screen, t, bar_rgb):
    """Replace the emulator screenshot's track title + BPM with track t."""
    from PIL import ImageDraw, ImageFont
    im = Image.fromarray(rgb)
    dr = ImageDraw.Draw(im)
    if screen == "main":
        dr.rectangle([100, 78, 778, 102], fill=bar_rgb)   # whole bar: no
                                                           # half-erased old title
        f = _font(23)
        title = t["title"]
        while f.getlength(title) > 660 and len(title) > 4:
            title = title[:-2]
        if title != t["title"]:
            title = title.rstrip() + "\u2026"
        dr.text((108, 90), title, font=f, fill=(255, 255, 255), anchor="lm")
        # deck BPM cell (699..781 x 403..455); blanks the burned-in 126.0
        x0, y0, x1, y1 = BPM_BOX
    else:
        x0, y0, x1, y1 = BPM_BOX         # PERFORM: same cell, "BPM" label
        y0 -= 2                          # under it, so no black-out
    if t.get("bpm") and x0 is not None:
        if screen == "main":
            dr.rectangle([x0, y0, x1 - 1, y1 - 1], fill=(0, 0, 0))
        whole, frac = f"{t['bpm']:.1f}".split(".")
        fb_ = _font(34)
        fs = _font(24)
        wf, ws = fb_.getlength(whole), fs.getlength("." + frac)
        dr.text((x1 - 3 - wf - ws, y0 + 28), whole, font=fb_, fill=(255, 255, 255), anchor="ls")
        dr.text((x1 - 3 - ws, y0 + 28), "." + frac, font=fs, fill=(255, 255, 255), anchor="ls")
    return np.array(im)



# --------------------------------------------------------------- themes ---
_SCREENS = None
MASKS = {   # procedurally drawn waveforms: the deck theme never touches them
    "main": [(116, 170, 504, 143), (108, 399, 528, 60)],
    "perform": [(116, 64, 504, 72), (108, 399, 528, 60)],
}


def screens():
    global _SCREENS
    if _SCREENS is None:
        if getattr(sys, "frozen", False):
            # In PyInstaller bundle, _MEIPASS points to _internal/, assets at _MEIPASS/studio/assets/
            meipass = getattr(sys, "_MEIPASS", HERE)
            base = os.path.join(meipass, "studio")
        else:
            base = HERE
        from emu import fwhome
        p = fwhome.path("screens.npz")             # user's firmware home
        if not os.path.exists(p):
            # Without this the page got a raw FileNotFoundError in an <img>
            # tag: a black panel with a broken-image icon and no hint.
            raise RuntimeError(
                "Firmware not set up yet: add the official XDJ-700 v1.15 "
                "update file on Home (Setup > Firmware > Add) to see the "
                "theme preview.")
        z = np.load(p)
        _SCREENS = {k: z[k] for k in z.files}
    return _SCREENS


# Lit touch buttons: addresses, positions and the colour maths live in
# mods/litbuttons.py, shared with the deck's theme tables and the emulator.
LIT_KEY = 0xF81F
_LIT = None


def lit_sprites():
    """{name: (x, y, uint16[53, 72])} read from the user's firmware block1
    (firmware home blk1.bin, loaded at 0x08000000); {} if it is missing."""
    global _LIT
    if _LIT is None:
        import litbuttons as LB
        from emu import fwhome
        out = {}
        try:
            with open(fwhome.path("blk1.bin"), "rb") as f:
                for k in LB.SHOWN:
                    a, _ap, x, y = LB.BUTTONS[k]
                    f.seek(a - LB.IMG_BASE)
                    px = np.frombuffer(f.read(LB.W * LB.H * 2), "<u2")
                    if len(px) == LB.W * LB.H:
                        out[k] = (x, y, px.reshape(LB.H, LB.W).copy())
        except OSError:
            pass
        _LIT = out
    return _LIT


_LIT_REF = None


def lit_ref():
    """one shared (saturation, brightness) reference for every button"""
    global _LIT_REF
    if _LIT_REF is None:
        import litbuttons as LB
        _LIT_REF = LB.reference([px for _x, _y, px in lit_sprites().values()])
    return _LIT_REF


def lit_recolour(px, colour):
    """one lit button (stock RGB565) in the user's lit colour -- the same
    maths as the deck's theme tables (mods/litbuttons.py)"""
    import litbuttons as LB
    return LB.recolour(px, colour, lit_ref())


def preview_png(spec, screen, wave=False, wopts=None, track=None, lit=None):
    """wave=True: the waveforms follow the theme too (the deck recolours the
    stock waveform palettes with the same maths). wopts: overview style +
    colours, drawn with the deck's own overview maths on a stick track.
    screen "lit" = the player screen with every lit-able touch button lit.
    lit = "#rrggbb": the lit buttons in that colour (the deck does the
    same with a theme saved with that lit colour)."""
    import themes as T
    show_lit = screen == "lit"
    screen = "main" if show_lit else screen
    fb = screens()[screen].copy()
    lits = {}
    if screen == "main" and (show_lit or lit):
        for k, (x, y, px) in lit_sprites().items():
            if show_lit or k == "quantize":       # QUANTIZE is lit in the shot
                lits[k] = (x, y, px)
                if show_lit:
                    sub = fb[y:y + 53, x:x + 72]
                    fb[y:y + 53, x:x + 72] = np.where(px == LIT_KEY, sub, px)
    if spec:
        keep = np.zeros(fb.shape, bool)
        for x, y, w, h in ([] if wave else MASKS.get(screen, [])):
            keep[y:y + h, x:x + w] = True
        cols, inv = np.unique(fb, return_inverse=True)
        new = T.map_colours(cols, spec)        # == the deck's theme tables
        out = new[inv].reshape(fb.shape)
        fb = np.where(keep, fb, out)
    if lit:
        try:
            for x, y, px in lits.values():
                sub = fb[y:y + 53, x:x + 72]
                fb[y:y + 53, x:x + 72] = np.where(px == LIT_KEY, sub, lit_recolour(px, lit))
        except (ValueError, IndexError):
            pass                                  # bad colour: theme only
    stick = stick_path()
    d = track or demo_track(stick)
    # `track` may be a dir (lookup) or a ready label dict from the caller
    t = track if isinstance(track, dict) else (track_label(stick, d) if d else None)
    if wopts and wopts.get("style") and d:
        w = dict(wopts, spec=spec)
        cols = wave_cols(stick, d, wopts["style"], w)
        if cols:
            a = np.zeros((34, 500), np.uint16)
            for x, col in enumerate(cols):
                for k, c in enumerate(col[:34]):
                    a[33 - k, x] = c
            fb[407:441, 116:616] = a
        cc = centre_cols(stick, d, wopts["style"], w)
        if cc:
            if screen == "main":
                # the screenshot was taken with no track: clear the
                # rekordbox splash behind the centre waveform first
                fb[171:312, 116:620] = 0
            draw_centre(fb, cc, screen)
    a = fb.astype(np.uint32)
    rgb = np.dstack([((a >> 11) & 31) * 255 // 31, ((a >> 5) & 63) * 255 // 63,
                     (a & 31) * 255 // 31]).astype(np.uint8)
    if t:
        rgb = draw_track_text(rgb, screen, t, tuple(int(v) for v in rgb[90, 700]))
    bio = io.BytesIO()
    Image.fromarray(rgb).save(bio, "PNG")
    return bio.getvalue()


SWATCH_SRC = (0x29A9, 0x29E9)       # stock title bar + button (emu-sampled)


_THEMES = [None, {}]
_THEME_ERR = [""]


def theme_list():
    """{name: {spec, swatch}} from mods/themes.py; {} when the project has
    no theme engine (the Mods page then says so)."""
    import importlib
    tp = os.path.join(ROOT, "mods", "themes.py")
    try:
        mt = os.path.getmtime(tp)
    except OSError:
        _THEME_ERR[0] = f"{tp} does not exist"
        return {}
    if _THEMES[0] == mt:
        return _THEMES[1]
    try:
        import themes as T
        importlib.reload(T)
    except Exception as e:
        _THEME_ERR[0] = f"mods/themes.py failed to load: {type(e).__name__}: {e}"
        return {}
    _THEME_ERR[0] = ""
    out = {}
    for k, v in T.THEMES.items():
        try:
            cols = T.map_colours(np.array(SWATCH_SRC, np.uint16), v) if v else np.array(SWATCH_SRC)
        except Exception as e:
            _THEME_ERR[0] = f"theme '{k}': {type(e).__name__}: {e}"
            continue
        hexes = ["#%02x%02x%02x" % (((c >> 11) & 31) * 255 // 31, ((c >> 5) & 63) * 255 // 63,
                                    (c & 31) * 255 // 31) for c in (int(x) for x in cols)]
        out[k] = {"spec": v, "swatch": hexes}
    _THEMES[:] = [mt, out]
    return out


def _fw_ready():
    try:
        from emu import fwhome
        return fwhome.ready()
    except Exception:
        return False


# ------------------------------------------------------------- updates ---
_UPD_CHECK = {}


def _vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4]) or (0,)


def update_check(force=False):
    """Latest public release on GitHub (cached 30 min). Read-only: nothing
    is downloaded until the user asks for it."""
    import urllib.request
    hit = _UPD_CHECK.get("r")
    if hit and not force and time.time() - hit[0] < 1800:
        return hit[1]
    res = {"current": APP_VERSION, "page": RELEASES_PAGE}
    try:
        req = urllib.request.Request(RELEASES_API, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"XDJ-Mod-Studio/{APP_VERSION}"})
        with urllib.request.urlopen(req, timeout=8) as r:
            rel = json.load(r)
        tag = rel.get("tag_name", "")
        want = "Windows" if sys.platform == "win32" else "XDJ-Mod-Studio.zip"
        asset = next((a for a in rel.get("assets", [])
                      if want.lower() in a.get("name", "").lower()), None)
        res.update(latest=tag.lstrip("v"), name=rel.get("name") or tag,
                   notes=(rel.get("body") or "")[:4000],
                   newer=_vtuple(tag) > _vtuple(APP_VERSION),
                   asset=asset and {"name": asset["name"], "size": asset.get("size", 0),
                                    "url": asset["browser_download_url"]})
    except Exception as e:
        res["error"] = f"couldn't reach GitHub ({type(e).__name__})"
    _UPD_CHECK["r"] = (time.time(), res)
    return res


def reveal(path):
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    elif sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])


def j_app_update(log, prog):
    """Download the newer release to Downloads and show it. The running app
    is never replaced in place (that would break its code signature)."""
    import urllib.request
    info = update_check(force=True)
    if not info.get("newer") or not info.get("asset"):
        raise RuntimeError(info.get("error") or "no newer version to download")
    a = info["asset"]
    if not a["url"].startswith("https://github.com/inter55/xdj-mod-studio/"):
        raise RuntimeError("unexpected download address")
    out = os.path.join(os.path.expanduser("~/Downloads"),
                       a["name"].replace(".zip", f"-{info['latest']}.zip"))
    log(f"downloading {a['name']} ({a['size'] / 1e6:.0f} MB) -> {out}")
    req = urllib.request.Request(a["url"], headers={"User-Agent": f"XDJ-Mod-Studio/{APP_VERSION}"})
    tmp = out + ".part"
    with urllib.request.urlopen(req, timeout=30) as r, open(tmp, "wb") as f:
        total, got = a["size"] or int(r.headers.get("Content-Length") or 0), 0
        while True:
            blk = r.read(1 << 18)
            if not blk:
                break
            f.write(blk)
            got += len(blk)
            if total:
                prog(got / total, f"{got / 1e6:.0f} of {total / 1e6:.0f} MB")
    os.replace(tmp, out)
    log(f"done: {out}")
    log("Quit XDJ Mod Studio, open the zip, and drag the new app over the old one.")
    reveal(out)


def j_firmware(log, prog, upd):
    """First run: build the firmware home from the user's OWN official
    XDJ-700 v1.15 update file (nothing from Pioneer ships with the app)."""
    from emu import fwhome
    ver, h = fwhome.check_upd(upd)
    if not ver:
        raise RuntimeError("That isn't the XDJ-700 firmware 1.15 update file. "
                           "Download it from Pioneer DJ's XDJ-700 support page.")
    log(f"XDJ-700 firmware {ver} recognised ({h[:12]}...)")
    prog(0.0, "unpacking the update file", eta=200)

    def p(f, note):
        prog(f, note)
    d = fwhome.setup(upd, p)
    log(f"firmware home ready: {d}")
    prog(1.0, "firmware ready")


def j_theme_save(log, prog, name, spec):
    args = ["tools/mod_builder.py", "--name", name,
            "--hue", str(spec["hue"]), "--sat", str(spec["sat"]),
            "--val", str(spec["val"]), "--spread", str(spec["spread"]), "--force"]
    for k in ("vivid", "lift"):
        if spec.get(k):
            args += [f"--{k}", str(spec[k])]
    if spec.get("lit") and re.fullmatch(r"#?[0-9a-fA-F]{6}", str(spec["lit"])):
        args += ["--lit", str(spec["lit"])]     # lit buttons (on-deck switching)
    prog(0.2, "building the theme")
    rc, _ = run_tool(log, args)
    if rc:
        raise RuntimeError("mod_builder failed")
    prog(1.0, "saved")


# ----------------------------------------------------------------- deck ---
_DECK = [False]
_DECK_ERR = [""]
_DECK_N = [0]               # distinct decks (serials) on USB


def _deck_watch():
    """The ONLY place that calls hid.enumerate: macOS hidapi crashes
    (SIGTRAP in IOHIDManager) when enumerated from several threads at once."""
    try:
        import hid
        from xdj700_usb_shell import PIONEER_VID
    except Exception as e:
        _DECK_ERR[0] = f"USB library did not load: {e}"
        return
    _DECK_ERR[0] = ""
    while True:
        try:
            # XDJ-700: VID 0x2B73, PID 0x0008, HID usages 1 (shell) / 2
            ds = [d for d in hid.enumerate() if d.get("vendor_id") == PIONEER_VID and
                  (d.get("product_id") == 0x0008 or d.get("usage") == 0x01)]
            _DECK[0] = bool(ds)
            _DECK_N[0] = len({d.get("serial_number") or "" for d in ds})
            # two players on one computer: an install must go to ONE deck
            _DECK_ERR[0] = (MULTI_DECK if _DECK_N[0] > 1 else "")
        except Exception:
            _DECK[0] = False
            _DECK_N[0] = 0
        time.sleep(3)


MULTI_DECK = ("Two decks are connected. Plug in one deck at a time to install "
              "(each deck gets its own install).")


def deck_ready():
    """None when an install may start, else the reason it may not"""
    if not deck_present():
        return "deck not connected (USB)"
    if _DECK_N[0] > 1:
        return MULTI_DECK
    return None


def deck_present():
    return _DECK[0]


def deck_err():
    return _DECK_ERR[0]




# The deck installer's own step lines -> (prefix, note, expected deck
# sessions for that step). Sessions are the real cost (~30 s pacing each +
# the tool's own time), so progress = sessions done / sessions expected and
# the time left = sessions left x the measured time per session.
INSTALL_STEPS = [          # sessions per step (mock-deck measured 2026-10-12)
    ("1/5", "Safety check of the deck's memory", 5),   # 1 once remembered
    ("2/5", "Overview waveform", 3),
    ("2b", "Safety check of the mod area", 1),
    ("4/5", "Eject the USB stick from the deck now", 1),
    ("5/5", "Centre waveform", 1),
    ("5b", "Key & stem switching", 1),
    ("5u", "UTILITY rows", 1),
    ("5d", "PERFORM pads", 1),
    ("5f", "8 hot cue pages", 1),
    ("5g", "Live stems", 1),
    ("5h", "FLAC playback", 5),         # FLAC + Opus: 5 measured 10-25 (FLAC only ~2)
    ("5e", "Theme switching", 4),
    ("5v", "Vinyl brake length", 1),
    ("5m", "Phase meter", 3),                                # survey + 2
    ("5k", "Live key shift (no audio-chip restart)", 2),     # probe + install
    ("5x", "TOUCH FX (no audio-chip restart)", 4),   # engine probe + install, screen survey + install
]
SESSION_S = 55.0            # per session incl. 30 s cool-down (mock-deck model)


def j_filter_playlists(log, prog, opts):
    """A 1.1 track filter the stock deck can actually show: the 700 has no
    browse filter in its firmware, but it shows the playlists in the stick's
    export.pdb (BROWSE > PLAYLIST), so a filter becomes a saved playlist.
    tools/filter_playlists.py appends the rows, backs the file up and reads
    the result back with the independent parser before it reports."""
    stick = settings().get("stick")
    if not stick or not os.path.isdir(stick):
        raise RuntimeError("choose your USB stick in Settings first")
    args = ["tools/filter_playlists.py", stick]
    if opts.get("presets"):
        args.append("--presets")
        if not opts.get("folder"):
            args += ["--folder", "FILTERS"]
    for key, flag in (("bpm", "--bpm"), ("key", "--key"),
                      ("colour", "--colour"), ("genre", "--genre"),
                      ("rating", "--rating"), ("search", "--search"),
                      ("limit", "--limit"), ("name", "--name"),
                      ("folder", "--folder"), ("sort", "--sort")):
        if opts.get(key):
            args += [flag, str(opts[key])]
    if opts.get("dry_run"):
        args.append("--dry-run")
    prog(0.15, "Reading the stick's library")
    rc, txt = run_tool(log, args)
    if rc:
        raise RuntimeError("no playlist was written -- see the log above")
    if opts.get("dry_run"):
        prog(1.0, "Dry run: nothing was changed")
        return
    prog(1.0, "Playlist ready -- on the deck: BROWSE > PLAYLIST")
    log("The deck needs the stick re-inserted (or a power cycle) to see a "
        "new playlist. export.pdb was backed up next to it before the write.")


def install_args(mode, pads=True, themes=True, liveks=False, stems=True,
                 phase=False, brake=0.0, flac=True, opus=False, touchfx=False):
    """tools/deck_session.py options for a Mods-page selection (the cable
    install and the USB-stick pack record the very same install)."""
    args = ["--mode", mode]
    if not pads:
        args.append("--no-pads")
    if not themes:
        args.append("--no-themes")
    if touchfx and pads:                     # TOUCH FX and live key shift share
        liveks = False                       # the audio chip's hook: one of the two
        args.append("--touchfx")
    if liveks:
        args.append("--keyshift")
    if not stems:
        args.append("--no-stems")
    if phase and pads:
        args.append("--phasemeter")
    if not flac:
        args.append("--no-flac")
    elif not opus:
        args.append("--no-opus")             # FLAC only: smaller, faster
    if brake and brake != 1:
        args += ["--brake", f"{brake:g}"]
    return args


def j_stick_pack(log, prog, sel):
    """USB-STICK mods file: record the cable install for this selection on
    the mock deck (the emulator answering the deck's USB shell) and write it
    as XDJMODS/PACK.BIN on the library stick. A deck flashed once with the
    loader update replays it at every stick mount (tools/stick_pack.py,
    tools/usbloader/lc.c)."""
    stick = settings().get("stick")
    if not stick or not os.path.isdir(stick):
        raise RuntimeError("choose your USB stick in Settings first")
    from emu import fwhome
    ckpt = fwhome.path("dspsound.pkl.gz")
    if not ckpt or not os.path.exists(ckpt):
        raise RuntimeError("firmware setup first (Home > Firmware: Add)")
    os.environ["MOCK_CKPT"] = ckpt
    out = os.path.join(stick, "XDJMODS", "PACK.BIN")
    # 2026-11-13 (Windows, Reddit): the bar sat at 2 % with "a few seconds
    # left" for 14+ minutes -- the recording runs the whole install on the
    # emulated deck and only the end reported. Now the bar follows the
    # install's own step lines (same names as the cable install), weighted by
    # each step's measured size, with no time estimate: a slow PC simply
    # takes longer, and the step name shows it is still moving.
    args = install_args(**sel)
    steps = [(pre, note, n) for pre, note, n in INSTALL_STEPS if pre != "4/5"]
    weight = {pre: max(1, n) for pre, _, n in steps}
    total = sum(weight.values())
    st = {"done": 0, "cur": None}

    def tlog(line):
        log(line)
        t = line.strip()
        for pre, note, _ in steps:
            if t.startswith(pre + " ") or t == pre:
                if st["cur"]:
                    st["done"] += weight[st["cur"]]
                st["cur"] = pre
                prog(0.02 + 0.93 * st["done"] / total,
                     "Recording on the emulated deck: " + note
                     + (" (skipped)" if "skipped" in t else ""))
                return

    prog(0.02, "Starting the emulated deck (a slow PC can take 20+ minutes in all)")
    rc, txt = run_tool(tlog, ["tools/stick_pack.py", "record"] + args
                       + ["--out", out])
    if rc or "wrote " not in txt:
        raise RuntimeError("could not make the mods file -- see the log")
    log(f"ready: {out} -- eject the stick safely; the deck applies it when "
        "the stick goes in. The deck needs the loader in memory first: send "
        "it over the cable (5 seconds, no flash) or build the loader update "
        "once if you never want the cable again.")
    prog(1.0, "Mods file written to your stick")


def j_stick_loader(log, prog, upd, banner="1.16"):
    """The one-time update for the USB-stick method: STOCK firmware + the
    stick loader only (permanent_build.py --stick-loader), boot-tested in
    the emulator -- with the stick's pack when there is one."""
    out_dir = os.path.expanduser("~/Downloads/XDJ700 modded update")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "XDJ700.UPD")
    prog(0.0, "Building the loader update", eta=90)
    rc, txt = run_tool(log, ["tools/permanent_build.py", upd, out, "--stick-loader",
                             "--banner", banner, "--i-know-flashing-can-brick"])
    if rc or "RESULT: built" not in txt:
        if os.path.exists(out):
            os.remove(out)
        raise RuntimeError("build refused -- see log (nothing was written)")
    stick = settings().get("stick") or ""
    pack = os.path.join(stick, "XDJMODS", "PACK.BIN")
    if os.path.isfile(pack):
        os.environ["PACK"] = pack
    prog(0.3, "Boot-testing it in the emulator (about 5 minutes)", eta=300)
    os.environ.setdefault("XDJ_SHOTS", os.path.join(out_dir, "boot-test"))
    rc, txt = run_tool(log, ["tools/permanent_build.py", "--emu", out])
    os.environ.pop("PACK", None)
    if rc or "RESULT: PASS" not in txt:
        bad = out + ".FAILED-BOOT-TEST"
        os.replace(out, bad)
        fails = [l for l in txt.splitlines() if l.strip().startswith("FAIL")]
        if fails and all("stick pack applied" in l for l in fails):
            # the loader itself checked out; only the mods file already on
            # the stick (written by this or an older app) did not apply
            raise RuntimeError(
                "the loader update itself boot-tested fine, but the mods file "
                f"on your stick ({pack}) did not apply in the test -- do NOT "
                "flash yet. Write the mods file again with this version of the "
                "app (Mod > USB stick, step 2), then build the loader update "
                f"again. (kept as {bad})")
        raise RuntimeError("the emulator boot test FAILED -- do NOT flash it "
                           f"(kept as {bad} for inspection)")
    with open(os.path.join(out_dir, "READ ME FIRST.txt"), "w") as f:
        f.write(STICK_README.replace("{banner}", banner))
    log(f"ready: {out} -- reports firmware {banner}; the deck accepts it as "
        "an update. Read the warnings before flashing.")
    prog(1.0, "Built + boot-tested. Read the warnings before flashing.")


def j_stick_arm(log, prog):
    """The USB-stick method WITHOUT flashing anything: write the 2.7 KB
    loader into the deck's RAM over the cable and arm the mount reader
    (tools/usbloader_hot.py). From then on every stick with
    XDJMODS/PACK.BIN brings the mods and other sticks play stock. A
    power-cycle is stock again, so this is once per power-on."""
    if not deck_present():
        raise RuntimeError("connect the deck over USB first -- this is the "
                          "no-flash step, and it needs the cable once per "
                          "power-on (5 seconds, nothing is written to flash)")
    prog(0.05, "Putting the loader in the deck's memory", eta=30)
    rc, txt = run_tool(log, ["tools/usbloader_hot.py"])
    if rc or "RESULT: PASS" not in txt:
        if "REFUSED" in txt:
            raise RuntimeError("the deck refused (another hook or live data in "
                              "that area) -- see the log; power-cycle the deck "
                              "and try again")
        raise RuntimeError("the loader was not armed -- see the log; nothing "
                          "was written to flash")
    log("loader is in the deck's memory. Unplug the cable, then insert your "
        "stick: any stick with XDJMODS/PACK.BIN brings the mods, any other "
        "stick plays stock. Power-cycle = stock again.")
    prog(1.0, "Done -- unplug the cable and insert your stick")


STICK_README = """XDJ-700 -- USB-stick mods loader (version shows {banner} in UTILITY)

This update is the OFFICIAL v1.15 firmware plus one small addition: when a
USB stick goes in, the deck looks for XDJMODS/PACK.BIN on it (made by XDJ
Mod Studio > Mod > File on your USB stick) and loads those mods. A stick
without that file = a completely stock deck.

FLASHING CAN BRICK A DECK. Mains power, do not touch the deck or the stick
until the update says it is complete. The way back to stock is the app's
Back to stock build (a stock file numbered higher than this one): the
official v1.15 file may be refused as an older version. Keep both to hand.

If a stick ever makes the deck misbehave, power it off, take the stick out
and start it again: it comes up stock.

How to update: copy XDJ700.UPD to the root of an empty FAT32 stick, then
follow Pioneer DJ's own firmware update steps for the XDJ-700.

Nothing on the deck changes until a stick with a mods file goes in; each
time, the mods are checked before anything is written (a deck that does
not look stock refuses the whole file and stays as it is).
"""


def j_deck_install(log, prog, mode, pads=True,
                   themes=True, liveks=False, stems=True, phase=False,
                   brake=0.0, flac=True, opus=False, touchfx=False):
    if deck_ready():
        raise RuntimeError(deck_ready())
    try:
        s = settings()
        s["mods"] = {"mode": mode, "pads": bool(pads), "themes": bool(themes),
                     "liveks": bool(liveks), "stems": bool(stems),
                     "phase": bool(phase),
                     "brake": float(brake or 0), "flac": bool(flac),
                     "opus": bool(opus), "touchfx": bool(touchfx and pads)}
        save_settings(s)
    except Exception:
        pass
    args = ["tools/deck_session.py"] + install_args(mode, pads, themes, liveks, stems,
                                                    phase, brake, flac, opus, touchfx)
    if touchfx and pads:
        liveks = False
    plan = {pre: n for pre, _, n in INSTALL_STEPS}
    if os.path.exists(os.path.expanduser("~/.xdj700_surveys.json")):
        plan["1/5"] = 1                   # this Mac has surveyed a deck before
    if not pads:
        plan["5d"] = 0
        plan["5f"] = 0
        plan["5g"] = 0
    elif not stems:
        plan["5g"] = 0
    if not themes:
        plan["5e"] = 0
    if not liveks:
        plan["5k"] = 0
    if not (touchfx and pads):
        plan["5x"] = 0
    if not (phase and pads):
        plan["5m"] = 0
    if not flac:
        plan["5h"] = 0
    elif not opus:
        plan["5h"] = 2
    if not brake or brake == 1:
        plan["5v"] = 0
    order = [pre for pre, _, _ in INSTALL_STEPS]
    st = {"step": None, "in_step": 0, "done": 0, "work_s": 0.0,
          "t_last": time.time(), "waiting": False}

    def update():
        i = order.index(st["step"]) if st["step"] in order else -1
        later = sum(plan[p] for p in order[i + 1:])
        cur_left = max(1 if st["step"] else 0,
                       plan.get(st["step"], 0) - st["in_step"]) if i >= 0 else 0
        if i < 0:
            later = sum(plan.values())
        left = cur_left + later
        total = st["done"] + left
        per = (st["work_s"] / st["done"]) if st["done"] >= 3 else SESSION_S
        eta = left * per
        # never let the estimate go UP (2026-10-25: 28 -> 29 min mid-FLAC
        # looked broken): cap it by the last estimate minus the time since
        now = time.time()
        if st.get("eta_t"):
            eta = min(eta, max(0.0, st["eta"] - (now - st["eta_t"])) + 30)
        st["eta"], st["eta_t"] = eta, now
        prog(st["done"] / max(1, total), None,
             eta=None if st["waiting"] else eta,
             wait=st["waiting"])

    def tlog(line):
        log(line)
        t = line.strip()
        if t == "@session":
            now = time.time()
            if st["waiting"]:
                st["t_last"] = now           # user-wait polls don't count
            else:
                st["done"] += 1
                st["in_step"] += 1
                st["work_s"] += now - st["t_last"]
                st["t_last"] = now
            update()
            return
        for pre, note, _ in INSTALL_STEPS:
            if t.startswith(pre):
                st["step"], st["in_step"] = pre, 0
                st["waiting"] = pre == "4/5"
                if "skipped" in t:
                    plan[pre] = 0
                prog(None, note + (" (skipped)" if "skipped" in t else ""))
                if st["waiting"]:
                    prog(None, "Eject the USB stick from the deck now -- "
                         "waiting for you", wait=True)
                update()
                return
        if st["waiting"] and ("5/5" in t or "5b" in t):
            st["waiting"] = False

    prog(0.0, "Starting", eta=sum(plan.values()) * SESSION_S)
    rc, out = run_tool(tlog, args)
    if rc and "still in the deck's memory" in out:
        # the deck kept this power-up's mods (cable re-plugged, no restart)
        log("Your mods are already on the deck from earlier (it hasn't been "
            "switched off since). Nothing to do. If they misbehave, switch the "
            "deck off and on and install again.")
        prog(1.0, "Already installed")
        return
    if rc or "DONE" not in out:
        raise RuntimeError("deck session stopped -- see log")
    prog(1.0, "Finished")


UPD_ACKS = ("brick", "recovery", "power")
BANNER_ERR = ("the version number must look like 1.16 (one digit, a dot, two "
              "digits) and differ from 1.15 -- it has to be higher than the "
              "version already on the deck")


def upd_banner(raw):
    """The version the built file will report, or None if unusable.

    The deck's updater refuses a file that is not NEWER than the one it runs,
    and every modded flash reports 1.16 -- so a second flash, or going back to
    stock afterwards, needs 1.17 or higher. This used to be hardcoded to 1.16
    in the stick-loader build, which made that file un-installable on a deck
    already at 1.16."""
    b = str(raw or "1.16").strip()
    if not re.fullmatch(r"\d\.\d\d", b) or b == "1.15":
        return None
    return b


def j_upd_build(log, prog, upd, mode, pads, versions, banner, extras=()):
    """PERMANENT mode: build a modded update file from the user's OWN
    official XDJ-700 v1.15 .UPD (tools/permanent_build.py), then boot it in
    the emulator from reset. Writes a file only -- the app never flashes."""
    out_dir = os.path.expanduser("~/Downloads/XDJ700 modded update")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "XDJ700.UPD")
    args = ["tools/permanent_build.py", upd, out, "--mode", mode,
            "--banner", banner, "--i-know-flashing-can-brick"]
    if not pads:
        args.append("--no-pads")
    if not versions:
        args.append("--no-versions")
    if extras:
        args += ["--extras", ",".join(extras)]
    prog(0.0, "Building the update file", eta=90)
    rc, txt = run_tool(log, args)
    if rc or "RESULT: built" not in txt:
        if os.path.exists(out):
            os.remove(out)
        if "does not fit the flash slot" in txt:
            raise RuntimeError(
                "this set of mods does not fit in the update file's free space. "
                "Leave out the phase meter or 8 hot cues (Touch FX is the "
                "biggest extra), or use the USB-stick route instead, where "
                "everything fits. Nothing was written.")
        raise RuntimeError("build refused -- see log (nothing was written)")
    prog(0.35, "Boot-testing it in the emulator (about 4 minutes)", eta=260)
    os.environ.setdefault("XDJ_SHOTS", os.path.join(out_dir, "boot-test"))
    rc, txt = run_tool(log, ["tools/permanent_build.py", "--emu", out])
    if rc or "RESULT: PASS" not in txt:
        bad = out + ".FAILED-BOOT-TEST"
        os.replace(out, bad)
        raise RuntimeError("the emulator boot test FAILED -- do NOT flash it "
                           f"(kept as {bad} for inspection)")
    with open(os.path.join(out_dir, "READ ME FIRST.txt"), "w") as f:
        names = [UPD_EXTRA_NAMES[e] for e in UPD_EXTRAS if e in extras]
        f.write(UPD_README.format(
            banner=banner, mode=mode,
            extras="".join(f"- {n} (copied into place at every power-on)\n"
                           for n in names) or "- (no extras ticked)\n",
            usb=", ".join([n for e, n in UPD_EXTRA_NAMES.items() if e not in extras]
                          + ["theme switching", "FLAC & Opus"])))
    log(f"ready: {out} -- firmware {banner}; read READ ME FIRST.txt before "
        "flashing")
    prog(1.0, "Built + boot-tested. Read the warnings before flashing.")


_FMT_CACHE = {}
_FMT_BUSY = set()


def lib_formats_async(root):
    """lib_formats() with the walk off the request thread: the polled
    /api/state answers at once (the cached value, or zeros on the first
    ask) and a background thread refreshes it. Walking a big stick inside
    /api/state stalled the 6 s poll and the UI with it."""
    if not root or not os.path.isdir(root):
        return {"flac": None, "opus": None}
    hit = _FMT_CACHE.get(root)
    if (hit is None or time.time() - hit[0] >= 60) and root not in _FMT_BUSY:
        _FMT_BUSY.add(root)

        def _walk(r=root):
            try:
                lib_formats(r)
            finally:
                _FMT_BUSY.discard(r)
        threading.Thread(target=_walk, daemon=True).start()
    return hit[1] if hit else {"flac": 0, "opus": 0}


def lib_formats(root):
    """{'flac': n, 'opus': n} audio files on the library stick (cached per
    folder for 60 s) -> the Mods page ticks FLAC only when it's needed and
    installs Opus only for .opus files. {'flac': None} = no stick."""
    if not root or not os.path.isdir(root):
        return {"flac": None, "opus": None}
    hit = _FMT_CACHE.get(root)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    n = {"flac": 0, "opus": 0}
    seen = 0
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if not x.startswith(".") and x != "PIONEER"]
        for f in files:
            ext = f.rsplit(".", 1)[-1].lower() if "." in f else ""
            if ext in ("flac", "opus"):
                n[ext] += 1
            seen += 1
        if seen > 50000:
            break
    _FMT_CACHE[root] = (time.time(), n)
    return n


UPD_EXTRAS = ("centre", "phasemeter", "hotcue8", "stems", "keyshift", "touchfx",
              "utility")  # utility: always baked (1.0)
UPD_EXTRA_NAMES = {"centre": "coloured centre waveform",
                   "phasemeter": "phase meter",
                   "hotcue8": "8 hot cues (HOT CUE tab paging)",
                   "stems": "live stems (STEMS tab)",
                   "keyshift": "live key shift (KEY SHIFT tab, MASTER TEMPO on)",
                   "touchfx": "Touch FX (X/Y pad: Filter, Crush, Gate)",
                   "utility": "mod settings in UTILITY (hold MENU)"}

UPD_README = """XDJ-700 MODDED UPDATE FILE (firmware {banner}) -- READ BEFORE FLASHING

This file was built by XDJ Mod Studio from YOUR official v1.15 update and
boot-tested in the emulator. It has NEVER been confirmed on a real deck.

RISKS
- Flashing firmware is the one thing in this project that can leave a deck
  needing a Pioneer DJ service centre (a "brick"). Everything else we do is
  in RAM and a power-cycle always undoes it; this does NOT undo by
  power-cycling.
- Losing power or pulling the USB stick while the deck updates can brick it.
- Warranty: modified firmware is not supported by Pioneer DJ / AlphaTheta.

BEFORE YOU FLASH
1. Keep the OFFICIAL v1.15 file (XDJ700.UPD from Pioneer DJ's support
   page) somewhere safe. Do not count on it as a downgrade -- see GOING
   BACK TO STOCK at the end of this file.
2. Use mains power, no extension leads you might kick; no pets/kids near.
3. Copy THIS XDJ700.UPD to the root of an empty FAT32 stick.
4. Follow Pioneer DJ's own XDJ-700 update instructions. Do not touch the
   deck until it says the update is complete.

WHAT IS BAKED IN ({mode} waveform)
- overview waveform colours, key/stem version switching, the mod settings
  in UTILITY (hold MENU), PERFORM pads (BEAT JUMP / KEY SHIFT / STEMS tabs).
{extras}NOT baked (still installed over USB from the app after each power-on):
- {usb}.
  Themes and FLAC/Opus are too big for the space the firmware file has.

GOING BACK TO STOCK
This file makes the deck report {banner}. The official v1.15 file is OLDER
than that, and the deck's updater can refuse an older file. Use XDJ Mod
Studio > Advanced > Back to stock instead: it builds a stock file numbered
higher than the deck's current version (nothing baked in) and boot-tests it
before you flash it. If you want to try the official file first, go ahead
-- just do not rely on it.
"""


def j_theme_deck(log, prog, name):
    """Install on-deck theme SWITCHING (all saved themes; the deck's
    UTILITY > THEME row then picks them live). Each session
    writes up to 32 KB and reports what is left; the write step reads the
    whole area first (foreign-data check), so no separate survey."""
    if deck_ready():
        raise RuntimeError(deck_ready())
    prog(0.0, "sending the themes to the deck", eta=5 * SESSION_S)
    total, t0, runs = None, time.time(), 0
    for i in range(20):
        rc, out = run_tool(log, ["tools/theme_switch_apply.py", "--precheck"])
        runs += 1
        if "RESULT: PASS" in out:
            log("themes installed: hold MENU > UTILITY > THEME "
                f"({name} is in the list)")
            prog(1.0, "installed")
            return
        if rc or "FAIL" in out or "REFUSED" in out:
            raise RuntimeError("theme install stopped -- see log")
        per = (time.time() - t0) / runs
        m = re.findall(r"(\d+) left", out)
        if m:
            left = int(m[-1])
            total = total or left + 32768
            prog(0.9 * (total - left) / total, f"theme tables: {left // 1024} KB left",
                 eta=(left // 32768 + 2) * per)
        else:
            prog(0.95, "switching the themes on", eta=per)
    raise RuntimeError("theme install did not finish")


def _firmware_labels():
    try:
        from emu.choices import firmware_choices
        import emu.fw as F          # noqa: F401  (resolves .UPD location)
        has = F.find_upd() is not None
        return [l for l, k, p in firmware_choices() if k == "ckpt" or p or has]
    except Exception:
        return []


# ------------------------------------------------------------- emulator ---
_EMU = [None]
_EMU_LOCK = threading.Lock()


def emu():
    """The one emulator host (created on first use; pygame loads then)."""
    with _EMU_LOCK:
        if _EMU[0] is None:
            from studio.emuhost import EmuHost
            _EMU[0] = EmuHost()
        return _EMU[0]


def emu_cmd(b):
    h = emu()
    c = b.get("cmd")
    if c == "start":
        h.start()
    elif c == "boot":
        h.boot(b["id"])
    elif c == "restart":
        h.restart()
    elif c == "mods":
        m = {"wave": b.get("wave") or None,
             "fx": b.get("fx") if b.get("fx") in ("keyshift", "touchfx") else None}
        th = b.get("theme")
        if th and th.get("spec"):
            m["theme"] = {"name": th.get("name") or "custom", "spec": th["spec"]}
        else:
            m["theme_stock"] = True
        h.apply_mods(m)
    elif c == "usb_insert":
        path = b.get("path") or stick_path()
        if not path or not os.path.exists(path):
            raise ValueError("Choose your USB stick or rekordbox export first "
                             "(Settings > Library).")
        h.insert_stick(path)
    elif c == "usb_eject":
        h.eject_stick()
    elif c == "pitch":
        return {"ok": bool(h.set_pitch(b.get("semis"), bool(b.get("hold"))))}
    elif c == "mute":
        return {"ok": bool(h.set_mute(bool(b.get("muted"))))}
    else:
        raise ValueError(f"unknown emulator command {c!r}")
    return {"ok": True}


# ------------------------------------------------------ native choosers ---
# The Mac app's window has its own chooser (tools/studio-shell.swift). The
# Windows app (and a source run in a browser) has no such window: the page
# is in Edge/Chrome, so the Choose buttons only said "type the path"
# (GitHub #8). The server runs on the same computer, so it opens the
# system's own dialog instead. The prompt rides an environment variable so
# quotes in it cannot break the command.
_PS_FOLDER = r"""
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$o = New-Object System.Windows.Forms.Form -Property @{TopMost = $true}
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = $env:XDJ_PICK_PROMPT
$d.ShowNewFolderButton = $false
if ($d.ShowDialog($o) -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Out.Write($d.SelectedPath) }
"""
_PS_FILE = r"""
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$o = New-Object System.Windows.Forms.Form -Property @{TopMost = $true}
$d = New-Object System.Windows.Forms.OpenFileDialog
$d.Title = $env:XDJ_PICK_PROMPT
$d.Filter = "Update file (*.UPD)|*.UPD;*.upd|All files (*.*)|*.*"
if ($d.ShowDialog($o) -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Out.Write($d.FileName) }
"""


def native_pick(kind, prompt):
    """{"path": str} chosen, {"path": None} cancelled, {"unsupported": True}
    when this computer has no dialog we can open (the page then falls back
    to typing the path)."""
    folder = kind != "file"
    env = dict(os.environ, XDJ_PICK_PROMPT=prompt[:200])
    try:
        if WINDOWS:
            import base64              # -EncodedCommand: no quoting of a multi-line script
            enc = base64.b64encode((_PS_FOLDER if folder else _PS_FILE)
                                   .encode("utf-16-le")).decode("ascii")
            cmd = ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
                   "-EncodedCommand", enc]
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            r = subprocess.run(cmd, capture_output=True, env=env, timeout=1800,
                               creationflags=flags)
            out = r.stdout.decode("utf-8", "replace").strip().lstrip("﻿")
        elif sys.platform == "darwin":
            what = "folder" if folder else "file"
            script = (f'set p to POSIX path of (choose {what} with prompt '
                      f'(system attribute "XDJ_PICK_PROMPT"))\nreturn p')
            r = subprocess.run(["osascript", "-e", script], capture_output=True,
                               env=env, timeout=1800)
            out = r.stdout.decode("utf-8", "replace").strip()
        elif shutil.which("zenity"):
            cmd = ["zenity", "--file-selection", "--title", prompt[:200]]
            if folder:
                cmd.append("--directory")
            r = subprocess.run(cmd, capture_output=True, timeout=1800)
            out = r.stdout.decode("utf-8", "replace").strip()
        else:
            return {"unsupported": True}
    except (OSError, subprocess.SubprocessError) as e:
        return {"unsupported": True, "detail": str(e)}   # not "error": the page would toast it
    if folder and out.endswith(("/", "\\")) and len(out) > 3:
        out = out.rstrip("/\\")
    return {"path": out or None}


# ----------------------------------------------------------------- http ---
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"          # keep-alive: input events are small

    def log_message(self, format, *args):
        pass                               # no per-request console noise

    def send(self, body, ctype="application/json", code=200, headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy",
                         "default-src 'self' 'unsafe-inline' data: blob:; "
                         "img-src 'self' data: blob:; media-src 'self' blob:")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        try:
            if u.path == "/":
                with open(UIHTML, "rb") as f:
                    return self.send(f.read(), "text/html; charset=utf-8")
            if u.path == "/api/update":
                return self.send(update_check(force=q.get("force") == "1"))
            if u.path == "/api/state":
                s = settings()
                fmts = lib_formats_async(stick_path())
                themes = theme_list()
                return self.send({"stick": stick_path(),
                                  "stick_ok": os.path.isdir(stick_path()),
                                  "app": FROZEN,
                                  "project": ROOT,
                                  "project_ok": os.path.isdir(os.path.join(ROOT, "tools")),
                                  "deck": deck_present(), "deck_err": deck_err(), "deck_multi": _DECK_N[0] > 1,
                                  "demucs": demucs_ok(),
                                  "ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                  "mods": s.get("mods", {}),
                                  "themes": themes,
                                  "themes_ok": bool(themes), "themes_err": _THEME_ERR[0],
                                  "firmware": _firmware_labels(),
                                  "fw_ready": _fw_ready(),
                                  "surveyed": os.path.exists(os.path.expanduser(
                                      "~/.xdj700_surveys.json")),
                                  "lib_formats": fmts,
                                  "upd": s.get("upd", "")})
            if u.path == "/api/tracks":
                return self.send(scan(stick_path()))
            if u.path == "/api/cues":
                try:
                    return self.send(cues_read(stick_path(), q.get("path", "")))
                except ValueError as e:
                    return self.send({"error": str(e)}, code=400)
            if u.path == "/api/jobs":
                with LOCK:
                    return self.send([job_view(j) for j in
                                      sorted(JOBS.values(), key=lambda j: -j["id"])])
            if u.path in ("/api/preview", "/api/wave"):
                with RENDER:
                    return self._render(u.path, q)
            if u.path == "/api/audio":
                return self._audio(q)
            if u.path == "/api/x7k":
                return self._x7k(q)
            if u.path == "/api/x7k_live":
                return self._x7k_live(q)
            if u.path == "/api/emu/state":
                return self.send(emu().state())
            if u.path == "/api/emu/stream":
                return self._emu_stream(q)
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as e:
            return self.send({"error": str(e)}, code=500)
        self.send({"error": "not found"}, code=404)

    def _audio(self, q):
        """The track file, with byte ranges: WebKit (the Mac app window)
        needs 206 / Accept-Ranges to seek, and often to play at all."""
        import re
        stick = os.path.realpath(stick_path())
        p = os.path.realpath(q["path"])
        if not p.startswith(stick + os.sep):
            return self.send({"error": "outside the library"}, code=403)
        ext = os.path.splitext(p)[1].lower()
        ct = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
              ".aif": "audio/aiff", ".aiff": "audio/aiff", ".flac": "audio/flac"}
        ctype = ct.get(ext, "application/octet-stream")
        try:
            size = os.path.getsize(p)
        except OSError:
            return self.send({"error": "cannot open the track"}, code=404)
        m = re.match(r"bytes=(\d*)-(\d*)$", (self.headers.get("Range") or "").strip())
        if not m:
            with open(p, "rb") as f:
                return self.send(f.read(), ctype, headers={"Accept-Ranges": "bytes"})
        a, b = m.group(1), m.group(2)
        if a == "" and b == "":
            return self.send(b"", ctype, code=416,
                             headers={"Content-Range": f"bytes */{size}"})
        if a == "":                       # suffix range: the last N bytes
            start, end = max(0, size - int(b)), size - 1
        else:
            start = int(a)
            end = int(b) if b else size - 1
        end = min(end, size - 1)
        if start > end or start >= size:
            return self.send(b"", ctype, code=416,
                             headers={"Content-Range": f"bytes */{size}"})
        with open(p, "rb") as f:
            f.seek(start)
            body = f.read(end - start + 1)
        return self.send(body, ctype, code=206, headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes"})

    def _x7k(self, q):
        """.x7k key version as WAV (decoded on the Mac): proof-of-concept
        playback for Key Shift without the deck DSP. ?dir=track dir
        &semis=K. 404 when the track has no sidecar (normal playback)."""
        import x7k as X7
        stick = stick_path()
        d = q.get("dir") or ""
        for r in scan(stick)["tracks"]:
            if r["dir"] == d:
                break
        else:
            return self.send({"error": "unknown track"}, code=404)
        xp = r["audio"] + ".x7k"
        if not os.path.exists(xp):
            return self.send({"error": "no x7k sidecar"}, code=404)
        try:
            semis = int(q.get("semis", 0))
        except ValueError:
            return self.send({"error": "bad semis"}, code=400)
        idx = X7.read_index(xp)
        if not any(v["semis"] == semis for v in idx["versions"]):
            return self.send({"error": "no such key"}, code=404)
        cache = os.path.join(WCACHE or os.path.join(ROOT, "cache"), "studio_x7k")
        os.makedirs(cache, exist_ok=True)
        cp = os.path.join(cache, hashlib.sha256(
            f"{xp}|{semis}".encode()).hexdigest() + ".wav")
        if not os.path.exists(cp):
            pcm, rate = X7.version_pcm(idx, semis, tempfile.mkdtemp())
            with RENDER:
                subprocess.run(
                    ["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar",
                     str(rate), "-ac", str(idx["ch"]), "-i", "-",
                     "-c:a", "pcm_s16le", cp], check=True,
                    input=pcm.astype(np.float32).tobytes())
        with open(cp, "rb") as f:
            return self.send(f.read(), "audio/wav")

    def _x7k_live(self, q):
        """Live phase-vocoder Key Shift (candidate D): NO sidecar needed.
        ?dir=track dir &semis=float (-8..+8). Renders on the Mac at 44.1k
        stereo via pv_shift_chunked (streaming, tempo-constant), cached.
        First hit costs ~duration/15; later hits are instant."""
        import x7k_arch_d as XD
        stick = stick_path()
        d = q.get("dir") or ""
        for r in scan(stick)["tracks"]:
            if r["dir"] == d:
                break
        else:
            return self.send({"error": "unknown track"}, code=404)
        try:
            semis = float(q.get("semis", 0))
        except ValueError:
            return self.send({"error": "bad semis"}, code=400)
        if not (-8.0 <= semis <= 8.0):
            return self.send({"error": "range is -8..+8"}, code=400)
        if not r["exists"]:
            return self.send({"error": "audio missing"}, code=404)
        cache = os.path.join(WCACHE or os.path.join(ROOT, "cache"), "studio_x7k")
        os.makedirs(cache, exist_ok=True)
        key = f"{r['audio']}|{r['audio'] and os.path.getmtime(r['audio'])}|{semis:.2f}"
        cp = os.path.join(cache, "live_" + hashlib.sha256(
            key.encode()).hexdigest() + ".wav")
        if not os.path.exists(cp):
            with RENDER:
                x = XD.load_clip_stereo(r["audio"])
                y = XD.np.stack(
                    [XD.pv.pv_shift(x[:, c], semis)
                     for c in range(x.shape[1])], axis=1)
                XD.write_wav(cp + ".tmp", y)
                os.replace(cp + ".tmp", cp)
        with open(cp, "rb") as f:
            return self.send(f.read(), "audio/wav")

    def _emu_stream(self, q):
        """The deck as multipart MJPEG, for an <img> in the Emulator tab."""
        host = emu()
        host.start()
        if host.app is None:
            return self.send({"error": host.error or "emulator unavailable"}, code=503)
        try:
            width = int(q.get("w") or 0) or None
        except ValueError:
            width = None
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=deck")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        seq = 0
        with host.viewer(width):
            while not self._client_gone():
                seq, jpg = host.wait_frame(seq, timeout=1.0)
                if not jpg:
                    continue
                self.wfile.write(b"--deck\r\nContent-Type: image/jpeg\r\n"
                                 b"Content-Length: " + str(len(jpg)).encode() +
                                 b"\r\n\r\n" + jpg + b"\r\n")
                self.wfile.flush()

    def _client_gone(self):
        """True once the browser has closed a streaming connection."""
        import select
        import socket
        try:
            r, _, _ = select.select([self.connection], [], [], 0)
            return bool(r) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def _render(self, path, q):
        if path == "/api/wave":
            stick = stick_path()
            w = {k: q.get(k) for k in ("tint", "low", "mid", "high", "custom")}
            return self.send(wave_png(stick, q["dir"], q.get("style", "blue"), w), "image/png")
        spec = None
        if q.get("hue") not in (None, "", "stock"):
            spec = {k: float(q[k]) for k in ("hue", "sat", "val", "spread")}
            for k in ("vivid", "lift"):
                if q.get(k) not in (None, "", "0"):
                    spec[k] = float(q[k])
        wopts = {k: q.get(k) for k in ("style", "tint", "low", "mid", "high", "custom")}
        return self.send(preview_png(spec, q.get("screen", "main"),
                                     q.get("wave") == "1", wopts,
                                     q.get("track") or None,
                                     lit=q.get("lit") or None), "image/png")

    def do_POST(self):
        if self.path == "/api/pick":               # blocks while the dialog is open
            n = int(self.headers.get("Content-Length") or 0)
            try:
                b = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self.send({"error": "bad request"}, code=400)
            return self.send(native_pick(b.get("kind"), b.get("prompt") or ""))
        n = int(self.headers.get("Content-Length") or 0)
        try:
            b = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self.send({"error": "bad request"}, code=400)
        p = self.path
        stick = stick_path()
        try:
            if p == "/api/emu/input":
                return self.send({"ok": emu().input(b.get("events", []))})
            if p == "/api/emu/cmd":
                return self.send(emu_cmd(b))
            if p == "/api/update/download":
                return self.send({"job": job("Download app update", j_app_update)})
            if p == "/api/update/page":
                webbrowser.open(RELEASES_PAGE)
                return self.send({"ok": True})
            if p == "/api/demucs/install":
                return self.send({"job": job("Install stem separation", j_demucs_install)})
            if p == "/api/firmware/setup":
                return self.send({"job": job("Set up your firmware", j_firmware,
                                             os.path.expanduser(b["path"].strip()))})
            if p == "/api/stick":
                path = os.path.expanduser(b["path"].strip())
                s = settings()
                s["stick"] = path
                save_settings(s)
                _SCAN.clear()
                return self.send({"ok": os.path.isdir(path)})
            if p == "/api/project":
                path = os.path.expanduser(b["path"].strip())
                ok = os.path.isdir(os.path.join(path, "tools"))
                if ok:
                    s = settings()
                    s["project"] = path
                    save_settings(s)
                return self.send({"ok": ok, "restart": ok and path != ROOT})
            if p == "/api/3band":
                return self.send({"job": job("3-band waveforms", j_3band, stick,
                                             b.get("dirs"), b.get("compute_all", False))})
            if p == "/api/stems":
                return self.send({"job": job(f"Stems: {os.path.basename(b['audio'])}",
                                             j_stems, b["audio"], [],
                                             b.get("quality", "opus"),
                                             b.get("replace", True) is not False)})
            if p == "/api/filter/playlists":
                opts = {k: str(b.get(k) or "").strip() for k in
                        ("bpm", "key", "colour", "genre", "search", "name",
                         "folder", "sort")}
                presets = bool(b.get("presets"))
                if not presets and not any(opts[k] for k in
                                           ("bpm", "key", "colour",
                                            "genre", "search")):
                    return self.send({"error": "give at least one filter "
                                               "(BPM, key, colour, genre or "
                                               "words), or make the whole "
                                               "set"}, code=400)
                if presets:
                    opts["presets"] = True
                if b.get("rating"):
                    opts["rating"] = int(b["rating"])
                if b.get("limit"):
                    opts["limit"] = int(b["limit"])
                opts["dry_run"] = bool(b.get("dry_run"))
                return self.send({"job": job("Filter set" if presets else
                                             "Filtered playlist",
                                             j_filter_playlists, opts)})
            if p == "/api/cues":
                try:
                    return self.send(cues_edit(stick, b.get("path", ""),
                                               b.get("cues") or {},
                                               b.get("clear") or ()))
                except (ValueError, OSError) as e:
                    return self.send({"error": str(e)}, code=400)
            if p == "/api/x7s":
                return self.send({"job": job(f"Deck file from sidecar: {os.path.basename(b['audio'])}",
                                             j_x7s, b["audio"])})
            if p == "/api/theme/save":
                return self.send({"job": job(f"Save theme {b['name']}", j_theme_save,
                                             b["name"], b["spec"])})
            if p == "/api/theme/deck":
                return self.send({"job": job(f"Install theme {b['name']} on deck",
                                             j_theme_deck, b["name"])})
            if p == "/api/upd/build":
                if not all(b.get("ack", {}).get(k) is True for k in UPD_ACKS):
                    return self.send({"error": "tick all three warnings first"}, code=400)
                upd = os.path.expanduser((b.get("path") or "").strip())
                if not os.path.isfile(upd):
                    return self.send({"error": "pick your official XDJ700.UPD (v1.15) file"},
                                     code=400)
                s_ = settings()
                s_["upd"] = upd
                save_settings(s_)
                banner = upd_banner(b.get("banner"))
                if not banner:
                    return self.send({"error": BANNER_ERR}, code=400)
                pads = bool(b.get("pads", True))
                extras = tuple(e for e in UPD_EXTRAS if e in (b.get("extras") or ())
                               and (pads or e not in ("hotcue8", "stems", "touchfx")))
                if "touchfx" in extras:          # one DSP slot: Touch FX wins
                    extras = tuple(e for e in extras if e != "keyshift")
                return self.send({"job": job("Build permanent update (UPD)", j_upd_build,
                                             upd, b.get("mode", "rgb"), pads,
                                             bool(b.get("versions", True)), banner,
                                             extras)})
            if p == "/api/stick/pack":
                sel = {k: b[k] for k in ("mode", "pads", "themes", "liveks", "stems",
                                         "phase", "brake", "flac", "opus", "touchfx")
                       if k in b}
                return self.send({"job": job("Write mods file to the USB stick",
                                             j_stick_pack, sel)})
            if p == "/api/stick/loader":
                if not all(b.get("ack", {}).get(k) is True for k in UPD_ACKS):
                    return self.send({"error": "tick all three warnings first"}, code=400)
                upd = os.path.expanduser((b.get("path") or "").strip())
                if not os.path.isfile(upd):
                    return self.send({"error": "pick your official XDJ700.UPD (v1.15) file"},
                                     code=400)
                banner = upd_banner(b.get("banner"))
                if not banner:
                    return self.send({"error": BANNER_ERR}, code=400)
                return self.send({"job": job("Build the USB-stick loader update",
                                             j_stick_loader, upd, banner)})
            if p == "/api/stick/arm":
                return self.send({"job": job("Send the stick loader to the deck "
                                             "(no flash)", j_stick_arm)})
            if p == "/api/deck/install":
                return self.send({"job": job("Install mods on deck", j_deck_install,
                                             b.get("mode", "rgb"),
                                             bool(b.get("pads", True)),
                                             bool(b.get("themes", True)),
                                             bool(b.get("liveks", False)),
                                             bool(b.get("stems", True)),
                                             bool(b.get("phase", False)),
                                             float(b.get("brake") or 0),
                                             bool(b.get("flac", False)),
                                             bool(b.get("opus", False)),
                                             bool(b.get("touchfx", False)))})
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as e:
            return self.send({"error": str(e)}, code=500)
        self.send({"error": "not found"}, code=404)


_LAST_REQ = [time.time()]


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def process_request(self, request, client_address):
        _LAST_REQ[0] = time.time()
        super().process_request(request, client_address)

    def handle_error(self, request, client_address):
        # the browser cancelling an image/stream request is normal: stay quiet
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def _arg(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def _quit_when_unused(idle_s=600):
    """Windows app: there is no window of its own to close (the UI is an
    Edge/Chrome app window that polls /api/state every few seconds). Once
    nothing has asked for anything for idle_s and no job is running, quit
    instead of lingering in the background."""
    def run():
        while True:
            time.sleep(30)
            with LOCK:
                busy = any(j.get("state") == "running" for j in JOBS.values())
            if not busy and time.time() - _LAST_REQ[0] > idle_s:
                if _EMU[0] is not None:
                    try:
                        _EMU[0].shutdown()
                    except Exception:
                        pass
                os._exit(0)
    threading.Thread(target=run, daemon=True).start()


def _watch_parent(pid):
    """The Mac app starts this server as a child; if the app goes away
    without telling us, exit rather than keep the emulator running."""
    def run():
        while True:
            time.sleep(2)
            try:
                os.kill(pid, 0)
            except OSError:
                os._exit(0)
    threading.Thread(target=run, daemon=True).start()


def main():
    try:                                    # crash reports name the import
        import faulthandler
        faulthandler.enable(all_threads=True)
    except Exception:
        pass
    if len(sys.argv) > 2 and sys.argv[1] == "--run-tool":
        return run_tool_main(sys.argv[2:])
    port = int(_arg("--port", 8720))
    try:
        srv = Server(("127.0.0.1", port), H)
    except OSError:
        if WINDOWS and "--no-browser" not in sys.argv and _studio_answers(port):
            # double-clicked while already running: show the running one
            open_window(f"http://127.0.0.1:{port}")
            return
        raise
    port = srv.server_address[1]
    url = f"http://127.0.0.1:{port}"
    parent = _arg("--parent")
    if parent:
        _watch_parent(int(parent))
    # the Mac app reads this line to learn where to point its window
    print(f"READY {port}", flush=True)
    threading.Thread(target=_deck_watch, daemon=True).start()
    if WINDOWS and FROZEN and "--no-browser" not in sys.argv:
        _quit_when_unused()
    if "--no-browser" not in sys.argv:
        open_window(url)
    try:
        srv.serve_forever()
    finally:
        if _EMU[0] is not None:
            _EMU[0].shutdown()


def _studio_answers(port):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state",
                                    timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def open_window(url):
    """Mac development: the system browser. Windows: an app-style window
    (Edge or Chrome --app, no tabs/address bar), else the default browser."""
    if WINDOWS:
        roots = [os.environ.get(k) for k in
                 ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        rels = (r"Microsoft\Edge\Application\msedge.exe",
                r"Google\Chrome\Application\chrome.exe")
        for rel in rels:
            for r in roots:
                exe = os.path.join(r, rel) if r else None
                if exe and os.path.exists(exe):
                    try:
                        subprocess.Popen([exe, f"--app={url}",
                                          "--window-size=1400,900"])
                        return
                    except OSError:
                        pass
    webbrowser.open(url)


if __name__ == "__main__":
    main()
