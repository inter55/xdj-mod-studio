#!/usr/bin/env python3
"""make_3band.py -- 3-band (CDJ-3000 style) overview waveform data for any
track on an XDJ-700 stick, so the waveform mod's 3-BAND mode works even for
tracks rekordbox never wrote 3-band data for.

Format = rekordbox's own PWV6 section (verified on a real export,
"Rekordbox stuff" P018/000206A8/ANLZ0000.2EX):
    'PWV6'  u32 header len = 20  u32 section len  u32 entry size = 3
    u32 count = 1200            then 1200 x (mid, high, low) bytes
The XDJ-700 caches every non-standard .EXT section in RAM on track load
(CLAUDE.md §33), so the section goes into ANLZ0000.EXT; the stock firmware
ignores it and the waveform mod draws from it.

Per track: if ANLZ0000.2EX already has rekordbox's PWV6 it is copied (best
quality); otherwise the audio (PPTH path) is analysed: 1200 equal columns,
each = RMS of the low (<200 Hz) / mid (200 Hz..2.5 kHz) / high (>2.5 kHz)
band, mapped through per-band curves CALIBRATED against real rekordbox
PWV6 data (`make_3band.py calibrate STICK` refits them).

  make_3band.py STICK_ROOT [--compute-all] [ANLZ_DIR ...]
  make_3band.py file AUDIO OUT.bin       (raw 3600-byte entries, for tests)
  make_3band.py calibrate STICK_ROOT [N] (fit band curves from real tracks)

Also the CENTRE waveform's 3-band data, PWV7 (rekordbox puts it in .2EX,
which the XDJ-700 never opens): 'PWV7' u32 header len = 24, u32 section
len, u32 entry size = 3, u32 count, u16 150 (entries per second), u16 0,
then count x (mid, high, low). Copied from .2EX when there, else computed
at 150 columns/s (STFT, calibrated against real PWV7:
`make_3band.py calibrate7 STICK_ROOT [N]`).

Idempotent (skips sections the .EXT already has). The original .EXT is kept
once as ANLZ0000.EXT.orig (restore = rename back).
"""
import glob
import json
import os
import shutil
import struct
import sys

import numpy as np

sys.path[:0] = [".", "tools"]
from stick_add_3band import sections

N_COLS = 1200
SR = 22050
EDGES = (200.0, 2500.0)
SMOOTH = 5                                  # columns averaged (power)                     # low | mid | high split (Hz)
CAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "pwv6_calibration.json")
CAL7_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "data", "pwv7_calibration.json")
RATE7 = 150                                 # PWV7 entries per second
NFFT7 = 1024                                # STFT window for the detail bands
# default curve per band: byte = clip(gain * rms ** power); refit by calibrate
DEFAULT_CAL = {"low": [255.0, 0.6], "mid": [255.0, 0.6], "high": [255.0, 0.6]}


def load_cal():
    try:
        return json.load(open(CAL_FILE))
    except Exception:
        return DEFAULT_CAL


class NoFfmpeg(RuntimeError):
    """the track has no rekordbox 3-band data and ffmpeg is not installed,
    so the colours cannot be worked out from the audio either"""


def decode_mono(path, sr=SR):
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        raise NoFfmpeg("ffmpeg is not installed")
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar",
                          str(sr), "-f", "s16le", "-"], capture_output=True,
                         check=True).stdout
    return np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0


def band_rms(x, sr=SR, cols=N_COLS):
    """(cols, 3) RMS of low / mid / high per column."""
    n = len(x)
    out = np.zeros((cols, 3), np.float64)
    if n < cols * 64:
        return out
    edges = np.linspace(0, n, cols + 1).astype(int)
    for c in range(cols):
        seg = x[edges[c]:edges[c + 1]]
        if len(seg) < 64:
            continue
        spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) ** 2
        f = np.fft.rfftfreq(len(seg), 1.0 / sr)
        tot = len(seg) / 2.0
        lo = spec[f < EDGES[0]].sum()
        mi = spec[(f >= EDGES[0]) & (f < EDGES[1])].sum()
        hi = spec[f >= EDGES[1]].sum()
        out[c] = np.sqrt(np.array([lo, mi, hi]) / tot) / np.sqrt(len(seg))
    # rekordbox's overview is smooth across neighbouring columns: average
    # the power over a 5-column window (centred)
    k = np.ones(SMOOTH) / SMOOTH
    pw = np.stack([np.convolve(out[:, i] ** 2, k, mode="same") for i in range(3)], 1)
    return np.sqrt(pw)


def band_rms_detail(x, sr=SR, rate=RATE7):
    """(frames, 3) low/mid/high RMS at `rate` frames/s (PWV7 timing):
    Hann STFT, window NFFT7 centred on each frame, chunked for memory."""
    hop = sr / rate
    n = int(len(x) / hop)
    out = np.zeros((n, 3), np.float64)
    if n == 0:
        return out
    pad = np.concatenate([np.zeros(NFFT7 // 2, np.float32), x,
                          np.zeros(NFFT7, np.float32)])
    win = np.hanning(NFFT7).astype(np.float32)
    f = np.fft.rfftfreq(NFFT7, 1.0 / sr)
    bands = (f < EDGES[0], (f >= EDGES[0]) & (f < EDGES[1]), f >= EDGES[1])
    starts = (np.arange(n) * hop).astype(np.int64)
    for c0 in range(0, n, 4096):
        st = starts[c0:c0 + 4096]
        fr = pad[st[:, None] + np.arange(NFFT7)[None, :]] * win
        spec = np.abs(np.fft.rfft(fr, axis=1)) ** 2
        for i, m in enumerate(bands):
            out[c0:c0 + len(st), i] = np.sqrt(spec[:, m].sum(1) / (NFFT7 / 2)) \
                / np.sqrt(NFFT7)
    return out


def _curve(v, c):
    """[gain, power] (old) or {"x": log-rms quantiles, "y": byte quantiles}."""
    if isinstance(c, dict):
        out = np.interp(np.log(np.maximum(v, 1e-9)), c["x"], c["y"])
        return np.where(v > 1e-7, out, 0.0)
    return np.clip(c[0] * np.power(v, c[1]), 0, 255)


def to_entries(rms, cal=None):
    """(cols, 3) low/mid/high RMS -> bytes in PWV6 order (mid, high, low)."""
    cal = cal or load_cal()
    lo, mi, hi = (np.clip(_curve(rms[:, i], cal[k]), 0, 255)
                  for i, k in enumerate(("low", "mid", "high")))
    e = np.stack([mi, hi, lo], 1).round().astype(np.uint8)
    return e.tobytes()


def pwv6_section(entries):
    assert len(entries) == 3 * N_COLS
    return b"PWV6" + struct.pack(">IIII", 20, 20 + len(entries), 3, N_COLS) + entries


def load_cal7():
    try:
        return json.load(open(CAL7_FILE))
    except OSError:
        return load_cal()


def pwv7_section(entries):
    assert len(entries) % 3 == 0
    n = len(entries) // 3
    return b"PWV7" + struct.pack(">IIIIHH", 24, 24 + len(entries), 3, n,
                                 RATE7, 0) + entries


def section_raw(b, tag):
    for t, off, ln in sections(b):
        if t == tag:
            return b[off:off + ln]
    return None


def ext_has(e, tag):
    return any(t == tag for t, _, _ in sections(e))


def append_section(ext, sec):
    e = open(ext, "rb").read()
    if not os.path.exists(ext + ".orig"):
        shutil.copy2(ext, ext + ".orig")
    end = sum(t for _, _, t in sections(e)) + struct.unpack(">I", e[4:8])[0]
    new = bytearray(e[:end]) + sec
    struct.pack_into(">I", new, 8, len(new))      # PMAI total file length
    assert ext_has(bytes(new), sec[:4])
    tmp = ext + ".tmp"
    open(tmp, "wb").write(new)
    os.replace(tmp, ext)


def track_path(stick, anlz_dir):
    """Audio file of an ANLZ dir via its PPTH (UTF-16BE, stick-relative)."""
    for fn in ("ANLZ0000.DAT", "ANLZ0000.EXT"):
        p = os.path.join(anlz_dir, fn)
        if not os.path.exists(p):
            continue
        b = open(p, "rb").read()
        for tag, off, ln in sections(b):
            if tag == b"PPTH":
                h = struct.unpack(">I", b[off + 4:off + 8])[0]
                rel = b[off + h:off + ln].decode("utf-16-be").rstrip("\0")
                return os.path.join(stick, rel.lstrip("/"))
    return None


def section_entries(b, tag=b"PWV6"):
    for t, off, ln in sections(b):
        if t == tag:
            h = struct.unpack(">I", b[off + 4:off + 8])[0]
            return b[off + h:off + ln]
    return None


def patch_dir(stick, d, compute_all=False):
    """overview (PWV6) + centre (PWV7) 3-band data into the .EXT"""
    ext, x2 = os.path.join(d, "ANLZ0000.EXT"), os.path.join(d, "ANLZ0000.2EX")
    if not os.path.exists(ext):
        return "skip (no .EXT)"
    e = open(ext, "rb").read()
    have6, have7 = ext_has(e, b"PWV6"), ext_has(e, b"PWV7")
    if have6 and have7:
        return "already has 3-band"
    x2b = open(x2, "rb").read() if not compute_all and os.path.exists(x2) else b""
    done, mono = [], None

    def audio_mono():
        nonlocal mono
        if mono is None:
            audio = track_path(stick, d)
            if not audio or not os.path.exists(audio):
                raise FileNotFoundError(audio)
            mono = decode_mono(audio)
        return mono
    try:
        if not have6:
            ent = section_entries(x2b) if x2b else None
            if ent and len(ent) == 3 * N_COLS:
                append_section(ext, pwv6_section(ent))
                done.append("overview (rekordbox's own)")
            else:
                append_section(ext, pwv6_section(to_entries(band_rms(audio_mono()))))
                done.append("overview (computed)")
        if not have7:
            sec = section_raw(x2b, b"PWV7") if x2b else None
            if sec and len(sec) >= 24:
                append_section(ext, sec)
                done.append("centre (rekordbox's own)")
            else:
                ent = to_entries(band_rms_detail(audio_mono()), load_cal7())
                append_section(ext, pwv7_section(ent))
                done.append("centre (computed)")
    except FileNotFoundError as ex:
        done.append(f"skipped the rest (audio not found: {ex})")
    except NoFfmpeg:
        # (subprocess raises FileNotFoundError for a missing program too: it
        # used to be reported as "audio not found" and nothing was added)
        done.append("NOT added: rekordbox made no 3-band data for this track "
                    "(no ANLZ0000.2EX) and ffmpeg is missing to work it out "
                    "from the audio")
    if not any(x.startswith(("overview", "centre")) for x in done):
        return "no 3-band: " + ", ".join(done)
    return "added 3-band: " + ", ".join(done)


def calibrate7(stick, n=8):
    """Quantile maps for the detail bands vs rekordbox's own PWV7."""
    dirs = sorted(glob.glob(os.path.join(stick, "PIONEER", "USBANLZ", "*", "*")))
    pairs = {k: ([], []) for k in ("low", "mid", "high")}
    used = 0
    for d in dirs:
        x2 = os.path.join(d, "ANLZ0000.2EX")
        if not os.path.exists(x2):
            continue
        ent = section_entries(open(x2, "rb").read(), b"PWV7")
        audio = track_path(stick, d)
        if not ent or not audio or not os.path.exists(audio):
            continue
        ref = np.frombuffer(ent, np.uint8).reshape(-1, 3).astype(float)
        rms = band_rms_detail(decode_mono(audio))
        k_ = min(len(ref), len(rms))
        ref, rms = ref[:k_], rms[:k_]
        for i, k in enumerate(("low", "mid", "high")):
            col = {"mid": 0, "high": 1, "low": 2}[k]
            m = (ref[:, col] > 2) & (rms[:, i] > 1e-5)
            pairs[k][0].append(np.log(rms[m, i]))
            pairs[k][1].append(ref[m, col])
        used += 1
        print("  calibrating with", os.path.basename(audio))
        if used >= n:
            break
    cal, q = {}, np.linspace(0, 100, 65)
    for k, (xs, ys) in pairs.items():
        x, y = np.concatenate(xs), np.concatenate(ys)
        cx, cy = np.percentile(x, q), np.percentile(y, q)
        cx = np.maximum.accumulate(cx + np.arange(len(cx)) * 1e-9)
        cal[k] = {"x": cx.tolist(), "y": cy.tolist()}
        err = np.mean(np.abs(np.interp(x, cx, cy) - y))
        print(f"  {k:4s}: mean abs err {err:.1f} (of 255), {len(x)} frames")
    json.dump(cal, open(CAL7_FILE, "w"), indent=1)
    print("saved", CAL7_FILE, "from", used, "tracks")
    return cal


def calibrate(stick, n=12):
    """Fit gain/power per band: rekordbox byte ~= gain * rms ** power."""
    dirs = sorted(glob.glob(os.path.join(stick, "PIONEER", "USBANLZ", "*", "*")))
    pairs = {k: ([], []) for k in ("low", "mid", "high")}
    used = 0
    for d in dirs:
        x2 = os.path.join(d, "ANLZ0000.2EX")
        if not os.path.exists(x2):
            continue
        ent = section_entries(open(x2, "rb").read())
        audio = track_path(stick, d)
        if not ent or not audio or not os.path.exists(audio):
            continue
        ref = np.frombuffer(ent, np.uint8).reshape(-1, 3).astype(float)
        rms = band_rms(decode_mono(audio))
        for i, k in enumerate(("low", "mid", "high")):
            col = {"mid": 0, "high": 1, "low": 2}[k]
            m = (ref[:, col] > 2) & (rms[:, i] > 1e-5)
            pairs[k][0].append(np.log(rms[m, i]))
            pairs[k][1].append(np.log(ref[m, col]))
        used += 1
        print("  calibrating with", os.path.basename(audio))
        if used >= n:
            break
    cal = {}
    q = np.linspace(0, 100, 65)
    for k, (xs, ys) in pairs.items():
        # quantile mapping: our log-RMS distribution -> rekordbox's byte
        # distribution (monotone, keeps the dynamics a power fit squashes)
        x, y = np.concatenate(xs), np.exp(np.concatenate(ys))
        cx, cy = np.percentile(x, q), np.percentile(y, q)
        cx = np.maximum.accumulate(cx + np.arange(len(cx)) * 1e-9)
        cal[k] = {"x": cx.tolist(), "y": cy.tolist()}
        err = np.mean(np.abs(np.interp(x, cx, cy) - y))
        print(f"  {k:4s}: quantile map, mean abs err {err:.1f} (of 255), "
              f"{len(x)} columns")
    os.makedirs(os.path.dirname(CAL_FILE), exist_ok=True)
    json.dump(cal, open(CAL_FILE, "w"), indent=1)
    print("saved", CAL_FILE, "from", used, "tracks")
    return cal


def main():
    a = sys.argv[1:]
    if not a:
        sys.exit(__doc__)
    if a[0] == "calibrate7":
        calibrate7(a[1], int(a[2]) if len(a) > 2 else 8)
        return
    if a[0] == "calibrate":
        calibrate(a[1], int(a[2]) if len(a) > 2 else 12)
        return
    if a[0] == "file":
        ent = to_entries(band_rms(decode_mono(a[1])))
        open(a[2], "wb").write(ent)
        print(f"wrote {len(ent)} B")
        return
    stick, rest = a[0], [x for x in a[1:] if not x.startswith("--")]
    compute_all = "--compute-all" in a
    dirs = rest or sorted(glob.glob(os.path.join(stick, "PIONEER", "USBANLZ", "*", "*")))
    for d in dirs:
        if not os.path.isabs(d):
            d = os.path.join(stick, "PIONEER", "USBANLZ", d)
        print(os.path.relpath(d, stick), "->", patch_dir(stick, d, compute_all))


if __name__ == "__main__":
    main()
