#!/usr/bin/env python3
"""stems_replace.py -- MP3 / M4A tracks: the stems file REPLACES the track.

1.0 could only replace WAV / AIFF, because a STEMS-4 file is a RIFF/AIFF
container (mix + four raw 16-bit planes + a 128-B "XST4" trailer) and the
deck picks its decoder from the extension. This tool gets MP3, M4A and FLAC
to the same place by writing the stems file as WAV and moving the library entry
with it, so the track keeps its cues, its waveform and its playlist slot.

Three things have to agree afterwards (all are byte-patched, never resized,
because ".mp3"/".m4a" and ".wav" are the same length and the ANLZ hash is
fixed-width):

  export.pdb track row   filename, file_path, analyze_path, file_type,
                         file_size, sample_rate, bitrate, sample_depth,
                         duration
  ANLZ folder            PIONEER/USBANLZ/P{XXX}/{YYYYYYYY}, where the name is
                         rekordbox's own hash of the audio path (algorithm
                         below; verified against real stick folders)
  ANLZ PPTH tag          the audio path as UTF-16BE + a NUL, which the deck
                         compares to the file it opened (no NUL -> it rejects
                         the analysis and re-analyses into ANLZ0001.DAT)

The original audio is kept next to the track as "<name>.pre-stems", the PDB
is backed up as export.pdb.bak-pre-stems, and everything is planned and
printed before a byte is written unless --yes is passed.

  stems_replace.py plan  STICK "/Contents/Artist/Album/Track.mp3"
  stems_replace.py apply STICK "/Contents/..." --stems DIR [--yes]
  stems_replace.py selftest

STICK is the root of the mounted USB stick. --stems DIR holds vocals.wav,
drums.wav, bass.wav and other.wav (what Demucs wrote); without it, apply
looks for "<track>.stems/" next to the track.

Not covered: PIONEER/rekordbox/exportLibrary.db (OneLibrary). The XDJ-700
reads export.pdb; a stick with both databases is refused unless --allow-onelibrary.
"""
import argparse
import os
import shutil
import struct
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
# Resolve our own modules from THIS file, not from the current directory: the
# app runs this from its bundled copy while the cwd is anywhere (a tester's
# Desktop, the app's own temp dir). CWD-relative paths shipped in 1.0.1 meant
# `import rekordbox_pdb` failed for MP3/M4A/FLAC replacement -- and the app
# bundle did not carry vendor/ at all (build_studio.sh now copies it).
sys.path[:0] = [_HERE, os.path.join(os.path.dirname(_HERE), "vendor")]

class Refuse(RuntimeError):
    """The stick or the request is not safe to touch; the message says why."""


# Original extension -> container the stems file is written as. The two must
# be the same LENGTH: the library stores names as length-prefixed DeviceSQL
# strings, so a different-length name would move the whole track row.
#   .mp3 / .m4a (4) -> .wav  (4)   PCM, plays everywhere
#   .flac       (5) -> .aiff (5)   PCM too and still lossless; AIFF is the only
#                                  5-character container the deck decodes
TARGET_EXT = {".mp3": ".wav", ".m4a": ".wav", ".flac": ".aiff"}
FILE_TYPE = {".wav": 0x0B, ".aiff": 0x0C, ".aif": 0x0C}
STEMS = ("vocals", "drums", "bass", "other")
PDB_BAK = ".bak-pre-stems"
AUDIO_BAK = ".pre-stems"
FILE_TYPE_OFFSET = 0x5A


# ---------------------------------------------------------------------------
# rekordbox's ANLZ path hash
# ---------------------------------------------------------------------------

def anlz_dir(file_path):
    """rekordbox's analysis folder for an audio path -> "P{XXX}/{YYYYYYYY}".

    Recovered from CreateAnlzFileFolderPath() in the rekordbox binary
    (credits: morizkraemer/fourfour, pioneer-usb-writer reference notes).
    XXX is seven scattered bits of the hash result, YYYYYYYY is the result
    modulo 200003. Verified here against the folders rekordbox itself wrote
    for three tracks on the dev stick.
    """
    h = 0
    for ch in file_path:
        c = ord(ch) & 0xFFFF
        h = (h * 0x5BC9 + c) & 0xFFFFFFFF
        h = (h * 0x93B5 + c) & 0xFFFFFFFF
    r = h % 200003
    p = 0
    p |= (r >> 0) & 0x01
    p |= (r >> 1) & 0x02
    p |= (r >> 4) & 0x04
    p |= (r >> 4) & 0x08
    p |= (r >> 5) & 0x10
    p |= (r >> 8) & 0x20
    p |= (r >> 10) & 0x40
    return "P%03X/%08X" % (p, r)


def anlz_rel(file_path):
    return "/PIONEER/USBANLZ/" + anlz_dir(file_path)


# ---------------------------------------------------------------------------
# byte surgery on export.pdb
# ---------------------------------------------------------------------------

def _pdb(stick):
    import rekordbox_pdb                              # noqa: F401
    from rekordbox_pdb.pdb import Database, TableType
    p = os.path.join(stick, "PIONEER", "rekordbox", "export.pdb")
    if not os.path.exists(p):
        raise Refuse("no export.pdb under %s/PIONEER/rekordbox" % stick)
    return p, Database(open(p, "rb").read()), TableType


def _find_track(db, want):
    want = "/" + want.strip("/")
    hits = [t for t in db.tracks if t.file_path == want]
    if not hits:
        raise Refuse("no track in export.pdb with path " + want)
    if len(hits) > 1:
        raise Refuse("%d tracks share the path %s" % (len(hits), want))
    return hits[0]


def _file_type(rel):
    ext = os.path.splitext(rel)[1].lower()
    if ext not in FILE_TYPE:
        raise Refuse("no file-type code for " + ext)
    return FILE_TYPE[ext]


def _patch_string(buf, old, new):
    """Replace one DeviceSQL string with another of the SAME byte length.

    Same length means the row never moves: no heap rewrite, no page
    bookkeeping, exactly what rekordbox's own in-place patches look like.
    """
    from rekordbox_pdb.edit import encode_string
    a, b = encode_string(old), encode_string(new)
    if len(a) != len(b):
        raise Refuse("string length would change (%r -> %r): refusing"
                         % (old, new))
    n = bytes(buf).count(a)
    if n != 1:
        raise Refuse("%d copies of %r in export.pdb (expected 1)" % (n, old))
    i = bytes(buf).find(a)
    buf[i:i + len(a)] = b
    return i


def _probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=sample_rate,channels,bits_per_raw_sample:format=duration,size",
         "-of", "default=nw=1", path],
        capture_output=True, text=True, check=True).stdout
    v = {}
    for line in out.splitlines():
        if "=" in line:
            k, x = line.split("=", 1)
            v[k] = x.strip()
    depth = v.get("bits_per_raw_sample") or ""
    rate = int(v.get("sample_rate") or 44100)
    ch = int(v.get("channels") or 2)
    dur = float(v.get("duration") or 0)
    size = int(v.get("size") or os.path.getsize(path))
    return {"sample_rate": rate, "channels": ch,
            "sample_depth": int(depth) if depth.isdigit() else 16,
            "duration": int(round(dur)), "file_size": size,
            "bitrate": round(rate * 16 * max(ch, 1) / 1000)}


# ---------------------------------------------------------------------------
# plan / apply
# ---------------------------------------------------------------------------

def plan(stick, rel, stems_dir=None):
    """Everything that would change, without writing anything."""
    rel = "/" + rel.strip("/")
    old_ext = os.path.splitext(rel)[1].lower()
    new_ext = TARGET_EXT.get(old_ext)
    if not new_ext:
        raise Refuse("%s is %s: Make stems replaces that directly, or takes "
                     "this route for MP3 / M4A / FLAC" % (rel, old_ext))
    if len(new_ext) != len(old_ext):
        raise Refuse("cannot swap %s for %s: the name would change length and "
                     "move the library row" % (old_ext, new_ext))
    new_rel = rel[:-len(old_ext)] + new_ext
    audio = os.path.join(stick, rel.lstrip("/"))
    if not os.path.exists(audio):
        raise Refuse("audio file is not on the stick: " + audio)
    pdb_path, db, TableType = _pdb(stick)
    track = _find_track(db, rel)
    old_dir = anlz_rel(rel)
    new_dir = anlz_rel(new_rel)
    stems = [os.path.join(stems_dir or audio + ".stems", s + ".wav")
             for s in STEMS]
    return {"rel": rel, "new_rel": new_rel, "audio": audio,
            "target": os.path.join(stick, new_rel.lstrip("/")),
            "pdb": pdb_path, "track_id": track.id,
            "old_dir": old_dir, "new_dir": new_dir, "stems": stems,
            "has_analysis": os.path.isdir(os.path.join(stick, old_dir.lstrip("/"))),
            "one_library": os.path.exists(os.path.join(
                stick, "PIONEER", "rekordbox", "exportLibrary.db"))}


def _show_plan(p):
    print("replace in place: %s" % p["rel"])
    print("  becomes         : %s  (stems-4)" % p["new_rel"])
    print("  library row     : track %d, file_type -> %s, size/rate/bitrate "
          "refreshed" % (p["track_id"],
                         "AIFF" if p["new_rel"].endswith((".aiff", ".aif"))
                         else "WAV"))
    print("  analysis folder : %s -> %s%s" % (
        p["old_dir"], p["new_dir"], "" if p["has_analysis"] else "  (none on stick)"))
    print("  original kept   : %s%s" % (os.path.basename(p["audio"]), AUDIO_BAK))
    for s in p["stems"]:
        mark = "ok  " if os.path.exists(s) else "MISS"
        print("  stem %s %s" % (mark, s))
    if p["one_library"]:
        print("  note: exportLibrary.db is present (OneLibrary); it is not "
              "updated by this tool")


def apply(stick, rel, stems_dir=None, yes=False, allow_onelibrary=False):
    p = plan(stick, rel, stems_dir)
    _show_plan(p)
    missing = [s for s in p["stems"] if not os.path.exists(s)]
    if missing:
        raise Refuse("missing stem file(s): " + ", ".join(missing))
    if os.path.exists(p["target"]):
        raise Refuse("target already exists: " + p["target"])
    if p["one_library"] and not allow_onelibrary:
        raise Refuse("stick has exportLibrary.db; pass --allow-onelibrary "
                         "to replace anyway (the deck reads export.pdb)")
    if not yes:
        raise Refuse("nothing written: add --yes to do it")

    import stems4
    from rekordbox_pdb.edit import PdbEditor

    tmpdir = tempfile.mkdtemp(prefix="stems_replace_")
    built = os.path.join(tmpdir, os.path.basename(p["new_rel"]))
    undo = []

    def rollback():
        for step in reversed(undo):
            try:
                step()
            except Exception as e:                    # best effort, logged
                print("  rollback step failed: %s" % e)

    try:
        print("building the stems file (mix + 4 planes) ...")
        stems4.build(p["audio"], built,
                     {s: os.path.join(os.path.dirname(p["stems"][0]), s + ".wav")
                      for s in STEMS},
                     aiff=p["new_rel"].lower().endswith((".aiff", ".aif")))
        info = _probe(built)
        print("  %s: %.0f MB, %d Hz, %d s" % (
            os.path.basename(built), info["file_size"] / 1e6,
            info["sample_rate"], info["duration"]))

        # 1. export.pdb: numeric fields in place, then the three strings
        pdb_bytes = open(p["pdb"], "rb").read()
        bak = p["pdb"] + PDB_BAK
        if not os.path.exists(bak):
            open(bak, "wb").write(pdb_bytes)
            undo.append(lambda: open(p["pdb"], "wb").write(pdb_bytes))
        ed = PdbEditor(pdb_bytes)
        for field in ("file_size", "sample_rate", "bitrate", "sample_depth",
                      "duration"):
            ed.set_track_field(p["track_id"], field, info[field])
        buf = bytearray(ed.to_bytes())

        old_anlz = p["old_dir"] + "/ANLZ0000.DAT"
        new_anlz = p["new_dir"] + "/ANLZ0000.DAT"
        _patch_string(buf, p["rel"], p["new_rel"])
        _patch_string(buf, old_anlz, new_anlz)
        from rekordbox_pdb.pdb import Database, TableType as TT
        db_now = Database(bytes(buf))
        locs = dict(zip([t.id for t in db_now.tracks],
                        db_now.row_locations(TT.TRACKS)))
        off = locs[p["track_id"]] + FILE_TYPE_OFFSET
        want_type = _file_type(p["new_rel"])
        if buf[off] == want_type:
            print("  track row already says the right file type")
        else:
            buf[off] = want_type
        with open(p["pdb"] + ".tmp", "wb") as f:
            f.write(buf)
        os.replace(p["pdb"] + ".tmp", p["pdb"])
        print("export.pdb patched (path, analysis path, file type, audio facts)")

        # 2. analysis: PPTH must name the new file, then the folder moves
        old_dir_abs = os.path.join(stick, p["old_dir"].lstrip("/"))
        if p["has_analysis"]:
            for name in sorted(os.listdir(old_dir_abs)):
                f = os.path.join(old_dir_abs, name)
                if name.upper().startswith("ANLZ") and os.path.isfile(f):
                    _patch_ppth(f, p["new_rel"])
            new_dir_abs = os.path.join(stick, p["new_dir"].lstrip("/"))
            os.makedirs(os.path.dirname(new_dir_abs), exist_ok=True)
            os.rename(old_dir_abs, new_dir_abs)
            undo.append(lambda: os.rename(new_dir_abs, old_dir_abs))
            print("analysis moved to %s (cues, grid and waveform follow)" % p["new_dir"])
        else:
            print("no analysis folder for this track: nothing to move")

        # 3. audio: original aside, stems file takes the name
        audio_bak = p["audio"] + AUDIO_BAK
        if os.path.exists(audio_bak):
            raise Refuse("already has %s; refusing" % audio_bak)
        os.replace(p["audio"], audio_bak)
        undo.append(lambda: os.replace(audio_bak, p["audio"]))
        shutil.move(built, p["target"])
        undo.append(lambda: os.replace(p["target"], built))
        print("done: %s is now the stems version (%s kept)" % (
            p["new_rel"], os.path.basename(audio_bak)))
    except BaseException:
        rollback()
        raise
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return p


def _patch_ppth(path, new_path):
    """Point an ANLZ file's PPTH tag at the new audio path (same length)."""
    b = bytearray(open(path, "rb").read())
    i = bytes(b).find(b"PPTH")
    if i < 0:
        return False
    plen = struct.unpack_from(">I", b, i + 0x0C)[0]
    payload = new_path.encode("utf-16-be") + b"\x00\x00"
    if len(payload) != plen:
        raise Refuse("%s: PPTH is %d bytes, new path needs %d -- refusing"
                         % (os.path.basename(path), plen, len(payload)))
    b[i + 0x10:i + 0x10 + plen] = payload
    open(path, "wb").write(b)
    return True


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------

def selftest(stick=None):
    stick = stick or "cache/stemsdelay_stick"
    ok = True

    for pth, want in (("/Contents/Leo Portela/Bon Vibrant - Leo Portela.flac",
                       "P00E/000281CE"),
                      ("/Contents/Daniela Cast/Jazzy - Daniela Cast.flac",
                       "P00A/0000CC9C")):
        got = anlz_dir(pth)
        print("%s hash vector %s -> %s" % ("ok  " if got == want else "FAIL",
                                           pth.rsplit("/", 1)[-1], got))
        ok &= got == want

    _, db, _ = _pdb(stick)
    for t in db.tracks:
        want = (t.analyze_path or "").rsplit("/ANLZ", 1)[0]
        want = want.replace("/PIONEER/USBANLZ/", "")
        got = anlz_dir(t.file_path)
        good = want == got and os.path.isdir(
            os.path.join(stick, "PIONEER", "USBANLZ", got))
        print("%s stick track %-24s %s (rekordbox wrote %s)"
              % ("ok  " if good else "FAIL", t.filename, got, want))
        ok &= good

    # end to end on a throwaway copy: mp3 -> wav, analysis moved, PPTH patched
    tmp = tempfile.mkdtemp(prefix="stems_replace_test_")
    try:
        for sub in ("PIONEER/rekordbox", "PIONEER/USBANLZ"):
            shutil.copytree(os.path.join(stick, sub), os.path.join(tmp, sub))
        rel = "/Contents/Daft Punk/Get Lucky/Get Lucky (ESQUIRE).mp3"
        audio = os.path.join(tmp, rel.lstrip("/"))
        os.makedirs(os.path.dirname(audio), exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=4", "-ac", "2",
                        "-ar", "44100", audio], check=True)
        sd = os.path.join(tmp, "stems")
        os.makedirs(sd)
        for s in STEMS:
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                            "sine=frequency=220:duration=4", "-ac", "2",
                            "-ar", "44100", os.path.join(sd, s + ".wav")],
                           check=True)
        out = apply(tmp, rel, sd, yes=True)
        from rekordbox_pdb.pdb import Database, TableType
        db2 = Database(open(out["pdb"], "rb").read())
        t2 = _find_track(db2, out["new_rel"])
        checks = {
            "row path is .wav": t2.file_path.endswith(".wav"),
            "analyze_path moved": t2.analyze_path == out["new_dir"] + "/ANLZ0000.DAT",
            "file_type matches": open(out["pdb"], "rb").read()[
                dict(zip([x.id for x in db2.tracks],
                         db2.row_locations(TableType.TRACKS)))[t2.id]
                + FILE_TYPE_OFFSET] == _file_type(out["new_rel"]),
            "analysis dir moved": os.path.isdir(
                os.path.join(tmp, out["new_dir"].lstrip("/"))),
            "old analysis dir gone": not os.path.isdir(
                os.path.join(tmp, out["old_dir"].lstrip("/"))),
            "PPTH names the wav": _ppth(os.path.join(
                tmp, out["new_dir"].lstrip("/"), "ANLZ0000.DAT")) == out["new_rel"],
            "original kept": os.path.exists(audio + AUDIO_BAK),
            "stems file in place": open(out["target"], "rb").read()[-128:-124] == b"XST4",
            "flac -> aiff, same length": (TARGET_EXT[".flac"] == ".aiff"
                                          and len(".flac") == len(".aiff")),
            "mp3/m4a -> wav, same length": all(
                len(e) == len(TARGET_EXT[e]) for e in (".mp3", ".m4a")),
            "vdj track count unchanged": len(db2.tracks) == len(db.tracks),
        }
        for name, good in checks.items():
            print("%s %s" % ("ok  " if good else "FAIL", name))
            ok &= bool(good)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("SELFTEST %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def _ppth(path):
    b = open(path, "rb").read()
    i = b.find(b"PPTH")
    if i < 0:
        return None
    plen = struct.unpack_from(">I", b, i + 0x0C)[0]
    return b[i + 0x10:i + 0x10 + plen - 2].decode("utf-16-be")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("plan", help="show what would change")
    p1.add_argument("stick"), p1.add_argument("track")
    p1.add_argument("--stems")
    p2 = sub.add_parser("apply", help="do it (needs --yes)")
    p2.add_argument("stick"), p2.add_argument("track")
    p2.add_argument("--stems"), p2.add_argument("--yes", action="store_true")
    p2.add_argument("--allow-onelibrary", action="store_true")
    p3 = sub.add_parser("selftest")
    p3.add_argument("stick", nargs="?")
    a = ap.parse_args()
    if a.cmd == "plan":
        _show_plan(plan(a.stick, a.track, a.stems))
    elif a.cmd == "apply":
        apply(a.stick, a.track, a.stems, a.yes, a.allow_onelibrary)
    else:
        try:
            sys.exit(selftest(a.stick))
        except Refuse as e:
            print(e)
            sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Refuse as e:
        print("refused: %s" % e)
        sys.exit(2)
