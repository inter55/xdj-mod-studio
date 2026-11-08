#!/usr/bin/env python3
"""mock_run.py 'tool args' ['tool args' ...] -- run deck tools one after the
other against the pretend deck (mockdeck_server.py on MOCK_CKPT), like
mock_install.py does for deck_session. Saves the machine afterwards
(MOCK_SAVE, default <tmp>/after_run.pkl).

  MOCK_CKPT=cache/emu_dsp_mounted6.pkl .venv/bin/python tools/mock/mock_run.py \
      'tools/keyshift_dsp_apply.py' 'tools/keyshift_dsp_apply.py --semis 2'
"""
import os
import shlex
import subprocess
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PY = sys.executable


def main():
    td = tempfile.mkdtemp(prefix="mockdeck")
    sock = os.path.join(td, "deck.sock")
    ckpt = os.environ.get("MOCK_CKPT", os.path.join(ROOT, "cache", "emu_mainscreen.pkl"))
    srv = subprocess.Popen([PY, os.path.join(ROOT, "tools/mock/mockdeck_server.py"), ckpt, sock],
                           stdout=subprocess.PIPE, text=True)
    srv.stdout.readline()
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "tools/mock"),
               XDJ_MOCK_SOCK=sock, XDJ_SHELL_PACE=os.environ.get("MOCK_PACE", "0"),
               XDJ_SHELL_STAMP=os.path.join(td, "stamp"),
               XDJ_SHELL_LOCK=os.path.join(td, "lock"),
               XDJ_SURVEY_CACHE=os.environ.get("MOCK_SURVEY_CACHE",
                                               os.path.join(td, "surveys.json")))
    rc = 0
    for c in sys.argv[1:]:
        t = time.time()
        print(f"$ {c}", flush=True)
        rc = subprocess.call([PY] + shlex.split(c), cwd=ROOT, env=env)
        print(f"  (exit {rc}, {time.time() - t:.0f}s)", flush=True)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import mocksock
    s = mocksock.connect(sock)
    f = s.makefile("rwb")
    out = os.environ.get("MOCK_SAVE", os.path.join(td, "after_run.pkl"))
    f.write(f"#SAVE {out}\n".encode()); f.flush(); f.readline()
    f.write(b"#QUIT\n"); f.flush(); f.readline()
    srv.wait(30)
    print("machine after run:", out)
    return rc


if __name__ == "__main__":
    sys.exit(main())
