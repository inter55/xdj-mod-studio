#!/usr/bin/env python3
"""stems4.py -- STEMS-4 files for live stem mute/solo on the XDJ-700.

One ordinary 16-bit stereo WAV/AIFF: the normal MIX (plays and analyses as
a plain track everywhere) + one extra chunk holding 4 sample-aligned
stereo stem planes in the same sample format as the mix, + a fixed 128-B
trailer at the very end of the file:

  +0  "XST4"   +4 data_off (mix sample data, file offset)
  +8  data_len (bytes)   +12 u8 big_endian (AIFF=1)  +13 u8 nplanes (4)
  +16 rel[4]   u32: plane k sample data = file offset data_off + rel[k]
  +32 order    "VDBO" (vocals, drums, bass, other = PERFORM STEMS pads)
  ...zero to 128
WAV chunk `xst4`, AIFF `APPL` + "XST4". On the deck, tools/stems/sm.c
hooks the audio file reads: with any stem muted it reads the 4 planes at
the play position and sums the ones that are ON (clipped), so muting is
sample-accurate and needs no audio-chip work.

  stems4.py build MIX OUT --vocals V --drums D --bass B --other O
  stems4.py selftest
"""
import argparse
import os
import struct
import subprocess
import sys
import tempfile

import numpy as np

sys.path[:0] = [".", "tools"]
from stemfile import decode, probe_rate, fit           # noqa: E402
from versionfile import mix_data_span, write_mix        # noqa: E402

MAGIC = b"XST4"
TRAILER = 128
ORDER = ("vocals", "drums", "bass", "other")          # pad / bit order


def even_chunks(path, aiff):
    """Rewrite so every top-level chunk has an even length.

    RIFF/AIFF put a pad byte AFTER an odd chunk. That is legal, but the
    deck's parser loses its place on it when it opens the file from the
    rekordbox library: ffmpeg's 115-byte ID3 chunk made a replaced track
    hang at loading even on stock firmware (emulator, 2026-11-02).
    rekordbox's own files only have even chunks.

    The zero byte goes INSIDE the chunk (and, for ID3, its own synchsafe
    size is bumped too) so the chunk size, the tag size and the next chunk
    header all agree -- there is no stray byte for a parser to trip on."""
    b = open(path, "rb").read()
    fmt = ">I" if aiff else "<I"
    out, i = bytearray(b[:12]), 12
    while i + 8 <= len(b):
        cid = b[i:i + 4]
        n = struct.unpack_from(fmt, b, i + 4)[0]
        data = bytearray(b[i + 8:i + 8 + n])
        if len(data) < n:                      # truncated: keep what is there
            n = len(data)
        if n & 1:
            data += b"\0"
            n += 1
            if bytes(data[:3]) == b"ID3" and len(data) >= 10:
                sz = ((data[6] & 0x7f) << 21) | ((data[7] & 0x7f) << 14) \
                     | ((data[8] & 0x7f) << 7) | (data[9] & 0x7f)
                sz += 1
                for k in range(4):
                    data[6 + k] = (sz >> (7 * (3 - k))) & 0x7f
        out += cid + struct.pack(fmt, n) + bytes(data)
        i += 8 + n
    if i < len(b):                             # stray pad byte / junk tail
        out += b[i:]
    struct.pack_into(fmt, out, 4, len(out) - 8)
    open(path, "wb").write(bytes(out))


def chunks(path):
    """Every top-level chunk as (id, size, offset); asserts they are all
    even, none overruns and nothing is left dangling at the end."""
    b = open(path, "rb").read()
    big = b[:4] == b"FORM"
    fmt = ">I" if big else "<I"
    out, i = [], 12
    while i + 8 <= len(b):
        cid = b[i:i + 4]
        n = struct.unpack_from(fmt, b, i + 4)[0]
        assert n % 2 == 0, f"odd chunk {cid!r} of {n} bytes at {i} in {path}"
        assert i + 8 + n <= len(b), f"chunk {cid!r} overruns {path}"
        out.append((cid, n, i))
        i += 8 + n
    assert i == len(b), f"{len(b) - i} stray bytes after the last chunk in {path}"
    return out


def build(mix_src, out, stems, aiff=None, progress=None):
    """stems = {"vocals": path, "drums": path, "bass": path, "other": path}"""
    aiff = out.lower().endswith((".aif", ".aiff")) if aiff is None else aiff
    rate = probe_rate(mix_src)
    if rate not in (44100, 48000):
        conv = os.path.join(tempfile.mkdtemp(), "src48.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", mix_src, "-ar", "48000",
                        "-ac", "2", "-c:a", "pcm_s16le", conv], check=True)
        mix_src, rate = conv, 48000
    if progress:
        progress(0.05, "writing the mix")
    write_mix(mix_src, out, rate, aiff)
    even_chunks(out, aiff)
    d_off, d_len, big = mix_data_span(out)
    frames = d_len // 4
    planes = []
    for i, k in enumerate(ORDER):
        if progress:
            progress(0.1 + 0.6 * i / 4, f"adding the {k} stem")
        a = np.clip(fit(decode(stems[k], rate, 2).astype(np.int32), frames), -32768, 32767)
        planes.append(np.asarray(a, dtype=">i2" if big else "<i2").tobytes())
        assert len(planes[-1]) == d_len
    b = bytearray(open(out, "rb").read())
    head = len(b) + (12 if aiff else 8)                 # chunk header (+OSType)
    pad = (-head) % 4
    pad += pad & 1                                     # body must END even too
    first = head + pad
    rel = [first + k * d_len - d_off for k in range(4)]
    body = bytes(pad) + b"".join(planes)
    if aiff:
        b += b"APPL" + struct.pack(">I", 4 + len(body)) + b"XST4" + body
    else:
        b += b"xst4" + struct.pack("<I", len(body)) + body
    if len(body) & 1:
        b += b"\0"
    tb = struct.pack("<4sIIBB2x4I4s", MAGIC, d_off, d_len, int(big), 4, *rel, b"VDBO")
    tb = tb.ljust(TRAILER, b"\0")
    if aiff:
        b += b"APPL" + struct.pack(">I", 4 + TRAILER) + b"XS4Z" + tb
        struct.pack_into(">I", b, 4, len(b) - 8)
    else:
        b += b"xs4t" + struct.pack("<I", TRAILER) + tb
        struct.pack_into("<I", b, 4, len(b) - 8)
    assert bytes(b[-TRAILER:-TRAILER + 4]) == MAGIC
    open(out, "wb").write(b)
    if progress:
        progress(1.0, "stems file written")
    return out


def read(path):
    """-> (d_off, d_len, big, {name: int16 stereo array}, mix array)"""
    b = open(path, "rb").read()
    t = b[-TRAILER:]
    assert t[:4] == MAGIC, "not a STEMS-4 file"
    d_off, d_len, big, n = struct.unpack_from("<IIBB", t, 4)
    rel = struct.unpack_from("<4I", t, 16)
    dt = ">i2" if big else "<i2"
    mix = np.frombuffer(b[d_off:d_off + d_len], dt)
    st = {k: np.frombuffer(b[d_off + rel[i]:d_off + rel[i] + d_len], dt)
          for i, k in enumerate(ORDER)}
    return d_off, d_len, big, st, mix


def expected(st, mask, dt="<i2"):
    """what the deck should play with mute mask (bit i = ORDER[i] muted)"""
    acc = np.zeros(len(next(iter(st.values()))), np.int64)
    for i, k in enumerate(ORDER):
        if not mask & (1 << i):
            acc += st[k].astype(np.int64)
    return np.clip(acc, -32768, 32767).astype(dt)


def odd_chunk_case():
    """even_chunks must turn an odd chunk into an even one AND still find the
    next chunk (ffmpeg's 115-B AIFF ID3 chunk is the real-world case)."""
    td = tempfile.mkdtemp()
    p = os.path.join(td, "odd.wav")
    fmt = struct.pack("<HHIIHH", 1, 2, 44100, 44100 * 4, 4, 16)
    odd = b"a" * 7                     # odd body + the pad byte RIFF adds
    data = b"\0\0" * 100
    payload = b"WAVE"
    for cid, c in ((b"fmt ", fmt), (b"junk", odd), (b"data", data)):
        payload += cid + struct.pack("<I", len(c)) + c + (b"\0" if len(c) & 1 else b"")
    open(p, "wb").write(b"RIFF" + struct.pack("<I", len(payload)) + payload)
    even_chunks(p, False)
    cs = {c[0]: c[1] for c in chunks(p)}
    good = cs.get(b"junk") == 8 and set(cs) == {b"fmt ", b"junk", b"data"}
    print(f"odd chunk: source 7 B -> {cs.get(b'junk')} B, "
          f"chunks {sorted(k.decode(errors='replace') for k in cs)} "
          f"-> {'ok' if good else 'FAIL'}")
    return good


def selftest():
    td = tempfile.mkdtemp()
    sr = 44100
    t = np.arange(sr * 3) / sr
    paths = {}
    for i, k in enumerate(ORDER):
        x = (0.2 * np.sin(2 * np.pi * (220 * (i + 1)) * t) * 32767).astype(np.int16)
        p = os.path.join(td, k + ".wav")
        import wave
        with wave.open(p, "wb") as w:
            w.setnchannels(2); w.setsampwidth(2); w.setframerate(sr)
            w.writeframes(np.repeat(x, 2).tobytes())
        paths[k] = p
    mixp = os.path.join(td, "mix.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-y"] + sum((["-i", paths[k]] for k in ORDER), [])
                   + ["-filter_complex", "amix=inputs=4:normalize=0", "-c:a", "pcm_s16le", mixp],
                   check=True)
    ok = True
    for ext in ("wav", "aiff"):
        out = os.path.join(td, f"S.{ext}")
        build(mixp, out, paths)
        d_off, d_len, big, st, mix = read(out)
        dt = ">i2" if big else "<i2"
        for k in ORDER:
            ref = fit(decode(paths[k], sr, 2).astype(np.int32), d_len // 4)
            ok &= np.array_equal(st[k].astype(np.int32), np.clip(ref, -32768, 32767).ravel())
        # the mix still decodes as a normal track of the right length
        dec = decode(out, sr, 2)
        ok &= abs(dec.size - d_len // 2) <= 4
        cs = chunks(out)            # every chunk even, nothing stray at the end
        print(f"{ext}: planes exact, plays as a normal {dec.size / 2 / sr:.2f} s "
              f"track; chunks {[(c[0].decode(errors='replace'), c[1]) for c in cs]}")
    ok &= odd_chunk_case()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "selftest"])
    ap.add_argument("mix", nargs="?")
    ap.add_argument("out", nargs="?")
    for k in ORDER:
        ap.add_argument(f"--{k}")
    a = ap.parse_args()
    if a.cmd == "selftest":
        return selftest()
    build(a.mix, a.out, {k: getattr(a, k) for k in ORDER})
    print("written", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
