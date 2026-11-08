#!/usr/bin/env python3
"""mock_install.py [deck_session args] -- run the REAL deck installers
against the pretend deck (tools/mock/mockdeck_server.py on an emulator
snapshot). No deck, no pacing; reports the estimated real-deck time and
saves the resulting machine for checks.

  .venv/bin/python tools/mock/mock_install.py --mode rgb
"""
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
# packaged app: tools run through the app binary's --run-tool
PY = sys.executable
PYCMD = [PY, "--run-tool"] if getattr(sys, "frozen", False) else [PY]


def main():
    td = tempfile.mkdtemp(prefix="mockdeck")
    sock = os.path.join(td, "deck.sock")
    ckpt = os.environ.get("MOCK_CKPT", os.path.join(ROOT, "cache", "emu_mainscreen.pkl"))
    srv = subprocess.Popen(PYCMD + [os.path.join(ROOT, "tools/mock/mockdeck_server.py"), ckpt, sock],
                           stdout=subprocess.PIPE, text=True)
    srv.stdout.readline()
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "tools/mock"),
               XDJ_MOCK_SOCK=sock, XDJ_SHELL_PACE=os.environ.get("MOCK_PACE", "0"), XDJ_SKIP_PLAYTEST="1",
               XDJ_SHELL_STAMP=os.path.join(td, "stamp"),
               XDJ_SHELL_LOCK=os.path.join(td, "lock"),
               XDJ_SURVEY_CACHE=os.environ.get("MOCK_SURVEY_CACHE",
                                               os.path.join(td, "surveys.json")))
    t0 = time.time()
    rc = subprocess.call(PYCMD + ["tools/deck_session.py"] + sys.argv[1:], cwd=ROOT, env=env)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import mocksock
    s = mocksock.connect(sock)
    f = s.makefile("rwb")
    f.write(b"#STATS\n"); f.flush()
    stats = f.readline().decode().strip()
    out = os.path.join(td, "after_install.pkl")
    f.write(f"#SAVE {out}\n".encode()); f.flush(); f.readline()
    f.write(b"#QUIT\n"); f.flush(); f.readline()
    srv.wait(30)
    print(f"\nMOCK INSTALL rc={rc} wall {time.time() - t0:.0f}s | {stats}")
    print("machine after install:", out)
    return rc


if __name__ == "__main__":
    sys.exit(main())
