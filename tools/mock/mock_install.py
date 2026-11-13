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
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    # 2026-11-13 (Windows, Reddit): the USB-stick job sat at 2 % for 14+ min
    # with an EMPTY log -- it was waiting here for the emulated deck's
    # "ready" line with no time limit and nothing printed. Now it says what
    # it is doing, passes on anything the server prints, and gives up after
    # MOCK_READY_S with a clear reason.
    import threading
    print("starting the emulated deck (loading its snapshot)...", flush=True)
    t_start, ready, lines = time.time(), threading.Event(), []

    def pump():
        for ln in srv.stdout:
            ln = ln.rstrip()
            lines.append(ln)
            if ln.startswith("mock deck ready"):
                ready.set()
                return
            print("  emulated deck: " + ln, flush=True)
        ready.set()                          # it exited: let the check below report
    threading.Thread(target=pump, daemon=True).start()
    limit = float(os.environ.get("MOCK_READY_S", "600"))
    while not ready.wait(30):
        print(f"  still loading the emulated deck ({time.time() - t_start:.0f} s)...",
              flush=True)
        if time.time() - t_start > limit:
            srv.kill()
            print(f"FAIL: the emulated deck did not start within {limit:.0f} s", flush=True)
            return 3
    if srv.poll() is not None or not (lines and lines[-1].startswith("mock deck ready")):
        print("FAIL: the emulated deck stopped while loading:", *lines[-12:], sep="\n  ",
              flush=True)
        return 3
    print(f"emulated deck ready after {time.time() - t_start:.0f} s", flush=True)
    # keep draining its output so it never blocks on a full pipe
    threading.Thread(target=lambda: [print("  emulated deck: " + ln.rstrip(), flush=True)
                                     for ln in srv.stdout], daemon=True).start()
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
