"""Firmware loader: real .UPD -> flash image for the bus.

Uses tools/fwtool.py (container) + tools/lzss_boot.py (ground-truth codec
transcribed from the boot stub). Falls back to work/MAIN.img if no .UPD
given. Never modifies the source file.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
from .paths import repo_root  # noqa: E402  (frozen app: data lives in the repo)
ROOT = repo_root()
import fwtool
import lzss_boot as L


def load_upd(path):
    raw, secs = fwtool.parse(path)
    main = next(s for s in secs if s.name == "MAIN")
    panl = next((s for s in secs if s.name == "PANL"), None)
    return raw, main, panl


def flash_image(main_sec):
    return bytes(main_sec.img)


def decode_block1(main_sec):
    """Return (hdr, stream_len, decoded_bytes) for the big app block."""
    blocks = main_sec.blocks()
    # the app block is the largest
    hdr, ln = max(blocks, key=lambda t: t[1])
    stream = bytes(main_sec.img[hdr + 4:hdr + 4 + ln])
    return hdr, ln, L.decode(stream)


def find_upd(explicit=None):
    if explicit and os.path.exists(explicit):
        return explicit
    for cand in (
        os.path.join(ROOT, "build", "XDJ700.UPD"),
        os.path.join(ROOT, "XDJ700.UPD"),
        os.path.join(ROOT, "FIRMWARE", "XDJ700_stock_v1.15.UPD"),
        os.path.expanduser("~/Downloads/cdj3k-mods-0.1.0/XDJ700.UPD"),
    ):
        if os.path.exists(cand):
            return cand
    # the user's firmware home (made from their own update file)
    from . import fwhome
    h = fwhome.path("MAIN.img")
    if os.path.exists(h):
        return h
    # last resort: work/MAIN.img (already unpacked)
    w = os.path.join(ROOT, "work", "MAIN.img")
    if os.path.exists(w):
        return w
    return None


def load_flash(explicit=None):
    """Return (bytes flash, info dict). Works with .UPD or raw MAIN.img."""
    p = find_upd(explicit)
    if p is None:
        return None, {"error": "no firmware found (pass --upd XDJ700.UPD)"}
    if p.endswith(".img"):
        return open(p, "rb").read(), {"source": p}
    # Parsing a .UPD (tools/fwtool.py compiles a regex per record) took ~5 s
    # of every snapshot load / app start (2026-11-01). The parsed MAIN image
    # is cached next to the repo's other caches, keyed by the .UPD's path,
    # size and mtime; any mismatch or a broken cache file re-parses.
    import hashlib
    import pickle
    st = os.stat(p)
    key = hashlib.sha1(f"{os.path.abspath(p)}|{st.st_size}|{st.st_mtime_ns}"
                       .encode()).hexdigest()[:16]
    cpath = os.path.join(ROOT, "cache", f"flash_{key}.pkl")
    try:
        with open(cpath, "rb") as f:
            img, blocks, panl_img = pickle.load(f)
        return img, {"source": p, "blocks": blocks, "panl": panl_img}
    except Exception:
        pass
    raw, main, panl = load_upd(p)
    blocks = main.blocks()
    img, panl_img = bytes(main.img), (bytes(panl.img) if panl else None)
    try:
        os.makedirs(os.path.dirname(cpath), exist_ok=True)
        tmp = cpath + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump((img, blocks, panl_img), f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, cpath)
    except OSError:
        pass                              # read-only install: just slower
    return img, {"source": p, "blocks": blocks, "panl": panl_img}
