#!/usr/bin/env python3
"""xflac_vfile_test.py -- the virtual-WAV layer (tools/xflac/vfile.c) must
serve exactly the WAV the deck would get if the FLAC were a real WAV.

Native build (build/xflac/vfile_native, same C as the deck) with stdio
mocks of the deck's file functions. For 16-bit/44.1k, 24-bit/48k and
mono/44.1k FLACs made by ffmpeg from a real track:
  * whole file read in deck-sized 0x24C0 chunks: header fields correct,
    data == ffmpeg's raw PCM decode (s16le / s24le), byte for byte
  * 200 random seek+read pairs (unaligned offsets, across the header)
    == the same slices of the reference
  * hi-res FLAC (96 kHz) becomes a 48 kHz WAV (2:1 low-pass decimation;
    2026-10-25: passing it through raw froze the deck -- its WAV parser
    got FLAC bytes); 32 kHz (unsupported) becomes an EMPTY file
The cuts start 60 s in, so SRC (env SRC=, any ffmpeg-readable track) must be
at least 67 s long; a shorter one makes empty FLACs, which would "match".
"""
import os
import random
import struct
import subprocess
import sys
import tempfile

SRC = os.environ.get("SRC") or os.path.expanduser(
    "~/Downloads/Rekordbox stuff/Contents/Lifelike & Roman Salzger/"
    "UnknownAlbum/Lifelike & Roman Salzger - Radio 84.aiff")
BIN = "build/xflac/vfile_native"


def ff(args):
    return subprocess.run(["ffmpeg", "-v", "error", "-y"] + args,
                          capture_output=True, check=True).stdout


def run(path, pairs=()):
    args = [BIN, path] + [str(x) for p in pairs for x in p]
    return subprocess.run(args, capture_output=True, check=True).stdout


def main():
    tmp = tempfile.mkdtemp()
    ok = True
    for name, rate, ch, fmt, raw in (("s16_44k", 44100, 2, "s16", "s16le"),
                                     ("s24_48k", 48000, 2, "s32", "s24le"),
                                     ("mono_44k", 44100, 1, "s16", "s16le")):
        f = os.path.join(tmp, name + ".flac")
        extra = ["-bits_per_raw_sample", "24"] if fmt == "s32" else []
        ff(["-ss", "60", "-t", "7", "-i", SRC, "-ar", str(rate), "-ac", str(ch),
            "-sample_fmt", fmt] + extra + ["-metadata", "title=Radio 84 (\u00e9dit)", f])
        ref = ff(["-i", f, "-f", raw, "-"])
        v = run(f)
        riff, size, wave, fmtid, flen, tag, nch, sr, br, ba, bits = \
            struct.unpack("<4sI4s4sIHHIIHH", v[:36])
        o, dataid, dlen, tags = 36, None, None, {}
        while o + 8 <= len(v):                       # walk the chunks
            cid, cl = v[o:o + 4], struct.unpack("<I", v[o + 4:o + 8])[0]
            if cid == b"data":
                dataid, dlen = cid, cl
                break
            if cid == b"LIST":
                q = o + 12
                while q < o + 8 + cl:
                    sid, sl = v[q:q + 4], struct.unpack("<I", v[q + 4:q + 8])[0]
                    tags[sid.decode()] = v[q + 8:q + 8 + sl].rstrip(b"\0").decode()
                    q += 8 + sl + (sl & 1)
            o += 8 + cl + (cl & 1)
        hdr, data = v[:o + 8], v[o + 8:]
        bps = 3 if fmt == "s32" else 2
        hdr_ok = (riff, wave, fmtid, dataid) == (b"RIFF", b"WAVE", b"fmt ", b"data") and \
            tag == 1 and nch == ch and sr == rate and bits == 8 * bps and \
            ba == ch * bps and br == rate * ch * bps and dlen == len(ref) and \
            size == len(hdr) - 8 + dlen and "ISFT" in tags
        filled = len(ref) >= 7 * rate * ch * bps * 99 // 100  # not an empty cut
        whole = filled and data == ref
        full = hdr + ref
        rnd = random.Random(7)
        pairs = [(rnd.randrange(0, len(full) - 1), rnd.randrange(1, 30000)) for _ in range(200)]
        got = run(f, pairs)
        want = b"".join(full[o:o + n] for o, n in pairs)
        seeks = got == want
        print(f"   tags {tags}")
        print(f"{name}: header {'ok' if hdr_ok else 'BAD'}, whole-file data "
              f"{'==' if data == ref else '!='} ffmpeg ({len(data)} B"
              f"{'' if filled else ', EMPTY CUT: SRC too short'}), 200 random seeks "
              f"{'match' if seeks else 'DIFFER'}")
        ok &= hdr_ok and whole and seeks
    f96 = os.path.join(tmp, "hi.flac")
    ff(["-ss", "60", "-t", "2", "-i", SRC, "-ar", "96000", "-sample_fmt", "s16", f96])
    import numpy as np
    v = run(f96)
    rate = struct.unpack_from("<I", v, 24)[0] if v[:4] == b"RIFF" else 0
    ref = subprocess.run(["ffmpeg", "-v", "error", "-i", f96, "-ar", "48000",
                          "-f", "s16le", "-"], capture_output=True).stdout
    i = v.find(b"data") + 8
    x = np.frombuffer(v[i:i + len(ref) // 2 * 2], "<i2").astype(float)
    y = np.frombuffer(ref, "<i2")[:len(x)].astype(float)
    err = np.sqrt(np.mean((x - y) ** 2)) / max(1.0, np.sqrt(np.mean(y ** 2)))
    good = rate == 48000 and len(ref) > 0 and abs(len(v) - i - len(ref)) <= 8 \
        and err < 0.01
    print(f"96 kHz -> {rate} Hz WAV, rel. error vs ffmpeg {err:.4f}: {'ok' if good else 'BAD'}")
    ok &= good
    f32 = os.path.join(tmp, "lo.flac")
    ff(["-ss", "60", "-t", "1", "-i", SRC, "-ar", "32000", "-sample_fmt", "s16", f32])
    empty = run(f32) == b""
    print(f"32 kHz (unsupported): served as an empty file: {empty}")
    ok &= empty
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
