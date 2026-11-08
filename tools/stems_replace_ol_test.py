#!/usr/bin/env python3
"""stems_replace_ol_test.py -- Make stems on a stick that ALSO has OneLibrary.

1.0.2 refused every MP3/M4A/FLAC on such a stick ("stick has
exportLibrary.db"), and rekordbox 7.2.11+ writes OneLibrary by default, so
Make stems failed after the separation for almost everyone. stems_replace
now updates the track's OneLibrary row too. This test runs that on a COPY of
a real rekordbox stick's databases (never the stick itself):

  A  replace: export.pdb and exportLibrary.db both point at the .wav, with
     the new size/type/audio facts and the moved analysis path; every other
     OneLibrary row is unchanged
  B  rollback: a failure after the database step (target file blocked)
     restores export.pdb AND exportLibrary.db byte for byte, and the audio

    .venv/bin/python tools/stems_replace_ol_test.py [STICK]
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "vendor")]
import stems_replace as SR  # noqa: E402

STICK = (sys.argv[1] if len(sys.argv) > 1
         else os.path.expanduser("~/Downloads/Rekordbox stuff"))
REL = "/Contents/Blaze/The Greatest House Acappellas/blaze_-_do_you_remember_house-sq.mp3"


def tone(path, f, secs=4):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency={f}:duration={secs}", "-ac", "2",
                    "-ar", "44100", path], check=True)


def make(tmp):
    for sub in ("PIONEER/rekordbox", "PIONEER/USBANLZ"):
        shutil.copytree(os.path.join(STICK, sub), os.path.join(tmp, sub),
                        ignore=shutil.ignore_patterns("*.bak-*", "._*"))
    audio = os.path.join(tmp, REL.lstrip("/"))
    os.makedirs(os.path.dirname(audio), exist_ok=True)
    tone(audio, 440)
    sd = os.path.join(tmp, "stems")
    os.makedirs(sd)
    for s in SR.STEMS:
        tone(os.path.join(sd, s + ".wav"), 220)
    return audio, sd


def ol_rows(path):
    con = SR._ol_connect(path)
    cols = [r[1] for r in con.execute("pragma table_info(content)")]
    rows = {r[0]: dict(zip(cols, r)) for r in con.execute("select * from content")}
    con.close()
    return rows


def main():
    ok = True

    def check(name, good):
        nonlocal ok
        print("%s %s" % ("ok  " if good else "FAIL", name))
        ok &= bool(good)

    # A: replace
    tmp = tempfile.mkdtemp(prefix="stems_ol_test_")
    try:
        audio, sd = make(tmp)
        olp = os.path.join(tmp, "PIONEER", "rekordbox", SR.OL_NAME)
        before = ol_rows(olp)
        cid = [k for k, v in before.items() if v["path"] == REL]
        check("track has one OneLibrary row", len(cid) == 1)
        out = SR.apply(tmp, REL, sd, yes=True)
        after = ol_rows(olp)
        r = after[cid[0]]
        info = SR._probe(out["target"])
        check("OneLibrary path is the .wav", r["path"] == out["new_rel"])
        check("OneLibrary fileName", r["fileName"] == os.path.basename(out["new_rel"]))
        check("OneLibrary fileType WAV (11)", r["fileType"] == 11)
        check("OneLibrary fileSize = stems file", r["fileSize"] == os.path.getsize(out["target"]))
        check("OneLibrary bitrate/rate/depth", (r["bitrate"], r["samplingRate"], r["bitDepth"])
              == (info["bitrate"], info["sample_rate"], info["sample_depth"]))
        check("OneLibrary analysis path moved",
              r["analysisDataFilePath"] == out["new_dir"] + "/ANLZ0000.DAT")
        check("OneLibrary analysis folder exists",
              os.path.isdir(os.path.join(tmp, out["new_dir"].lstrip("/"))))
        others = [k for k in before if k != cid[0] and before[k] != after[k]]
        check("every other OneLibrary row unchanged (%d rows)" % (len(before) - 1),
              not others and len(after) == len(before))
        for k in ("title", "artist_id_artist", "bpmx100", "key_id", "masterContentId"):
            check("OneLibrary %s kept" % k, r[k] == before[cid[0]][k])
        check("no temp/WAL files left", not any(
            os.path.exists(olp + e) for e in ("-wal", "-shm", ".tmp-stems")))
        from rekordbox_pdb.pdb import Database
        db = Database(open(out["pdb"], "rb").read())
        check("export.pdb names the .wav", SR._find_track(db, out["new_rel"]) is not None)
        check("original kept", os.path.exists(audio + SR.AUDIO_BAK))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # B: rollback after the database step
    tmp = tempfile.mkdtemp(prefix="stems_ol_test_")
    try:
        audio, sd = make(tmp)
        olp = os.path.join(tmp, "PIONEER", "rekordbox", SR.OL_NAME)
        pdb = os.path.join(tmp, "PIONEER", "rekordbox", "export.pdb")
        ol0, pdb0, a0 = (open(olp, "rb").read(), open(pdb, "rb").read(),
                         open(audio, "rb").read())
        open(audio + SR.AUDIO_BAK, "wb").write(b"x")   # makes step 3 refuse
        try:
            SR.apply(tmp, REL, sd, yes=True)
            check("forced failure raised", False)
        except SR.Refuse as e:
            check("forced failure raised (%s)" % str(e)[:50], True)
        check("exportLibrary.db restored byte for byte", open(olp, "rb").read() == ol0)
        check("export.pdb restored byte for byte", open(pdb, "rb").read() == pdb0)
        check("audio untouched", open(audio, "rb").read() == a0)
        new_dir = os.path.join(tmp, SR.anlz_rel(REL[:-4] + ".wav").lstrip("/"))
        check("analysis folder back in place", os.path.isdir(
            os.path.join(tmp, SR.anlz_rel(REL).lstrip("/"))) and not os.path.isdir(new_dir))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("RESULT: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
