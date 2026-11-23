#!/usr/bin/env python3
"""x1k_qemu.py -- run the XDJ-1000MK2 (fw 1.45) in QEMU as a REFERENCE deck,
to see exactly how its features look and behave (the 700 mods copy the
behaviour, never the code). Uses the cdj-nxs2-qemu machine "xdj1000mk2"
(bring-up) from our QEMU build and the user's library over NBD.

  x1k_qemu.py start [FOLDER]      boot (state in $X1K_DIR, default /tmp/x1k)
  x1k_qemu.py shot OUT.png        grab the screen (VNC)
  x1k_qemu.py key B:M[:MS]        panel key, e.g. 0x13:0x04:300 (USB source)
  x1k_qemu.py touch X Y [MS]      touch the screen at pixel X,Y (800x480)
  x1k_qemu.py drag X0 Y0 X1 Y1     finger drag
  x1k_qemu.py rot N               turn the browse knob N detents (+ = down)
  x1k_qemu.py plug [N]           hot-plug the stick (N = new id, 1, 2..)
  x1k_qemu.py mon "CMD"           QEMU monitor command
  x1k_qemu.py stop

Firmware: the reference repo's prepare_firmware.sh --model xdj1000mk2 on
XDJ-1000/XDJ1KMK2.UPD (sha 6fd2a500..02dbcd) -> main_unpacked.bin.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tools")]
D = os.environ.get("X1K_DIR", "/tmp/x1k")
KERNEL = os.environ.get("X1K_KERNEL", os.path.expanduser(
    "~/Downloads/ref/x1k-fw/extract/main_unpacked.bin"))
QEMU = os.environ.get("X1K_QEMU", os.path.expanduser("~/qemu-build-fast/qemu-system-sh4"))
KEYPORT = int(os.environ.get("X1K_KEYPORT", "40177"))


def _p(n):
    return os.path.join(D, n)


def nbd_start(folder):
    """tools/qemu_stick_nbd.py serves ONE connection: start one per plug"""
    try:
        os.unlink(_p("stick.sock"))
    except OSError:
        pass
    nbd = subprocess.Popen([sys.executable, os.path.join(ROOT, "tools", "qemu_stick_nbd.py"),
                            folder, _p("stick.sock")], env=dict(os.environ, PYTHONPATH=ROOT),
                           stdout=open(_p("stick.log"), "a"), stderr=subprocess.STDOUT,
                           start_new_session=True)
    t0 = time.time()
    while not os.path.exists(_p("stick.sock")):
        if nbd.poll() is not None or time.time() - t0 > 180:
            sys.exit("stick server failed (see stick.log)")
        time.sleep(0.3)
    return nbd


def start(folder):
    os.makedirs(D, exist_ok=True)
    for n in ("mon.sock", "vnc.sock", "stick.sock", "qmp.sock"):
        try:
            os.unlink(_p(n))
        except OSError:
            pass
    open(_p("folder.txt"), "w").write(folder)
    nbd = nbd_start(folder)
    env = dict(os.environ, CDJ_ATA="1", CDJ_PANEL_KEYSOCK=str(KEYPORT),
               CDJ_C6747_IDLE="0xC004EDCC:0x11804AE0:0x11805C00", C66X_IDLE_ISR_FAST="1")
    argv = [QEMU, "-M", "xdj1000mk2", "-name", "XDJ-1000MK2", "-kernel", KERNEL,
            "-serial", "file:" + _p("serial.log"), "-display", "none",
            "-vnc", "unix:" + _p("vnc.sock"),
            "-monitor", "unix:" + _p("mon.sock") + ",server,nowait",
            "-qmp", "unix:" + _p("qmp.sock") + ",server,nowait",
            "-audio", "none,timer-period=5000"]
    if os.environ.get("X1K_BOOTSTICK"):        # stick in at power-on
        argv += ["-drive", "if=none,id=stk0,file=nbd:unix:" + _p("stick.sock") + ",format=raw",
                 "-device", "usb-storage,drive=stk0,id=stick0,port=1"]
    q = subprocess.Popen(argv, env=env, stdout=open(_p("qemu.log"), "w"),
                         stderr=subprocess.STDOUT, start_new_session=True)
    json.dump({"qemu": q.pid, "nbd": nbd.pid}, open(_p("pids.json"), "w"))
    print("started qemu", q.pid, "nbd", nbd.pid)


def stop():
    try:
        pids = json.load(open(_p("pids.json")))
    except OSError:
        return
    for p in pids.values():
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:
            pass


def mon(cmd, wait=0.5):
    s = socket.socket(socket.AF_UNIX)
    s.connect(_p("mon.sock"))
    s.settimeout(wait)
    s.sendall(cmd.encode() + b"\n")
    out = b""
    t0 = time.time()
    while time.time() - t0 < wait + 1:
        try:
            b = s.recv(65536)
            if not b:
                break
            out += b
        except socket.timeout:
            break
    s.close()
    txt = out.decode("latin1")
    return "\n".join(l for l in txt.splitlines() if "\x1b" not in l and l.strip() not in ("(qemu)", ""))


def shot(path):
    from emu.qemu_app import Rfb
    from PIL import Image
    r = Rfb(_p("vnc.sock"))
    t0 = time.time()
    while r.seq < 1 and time.time() - t0 < 10:
        time.sleep(0.05)
    with r.lock:
        img = r.fb.copy()
    try:                                  # one client at a time: QEMU drops
        r.s.close()                       # new ones while old ones linger
    except OSError:
        pass
    Image.fromarray(img).save(path)
    print(path, img.shape)


def plug(n):
    """hot-plug the stick (like the app does after boot): new ids each time"""
    mon(f"device_del stick{n-1}", 1.0)
    time.sleep(2)
    src = open(_p("folder.txt")).read()
    if os.path.isfile(src):              # a raw disk image: QEMU writes it
        f = src                          # (lets you read back what the deck saved)
    else:
        nbd = nbd_start(src)
        pids = json.load(open(_p("pids.json")))
        pids["nbd%d" % n] = nbd.pid
        json.dump(pids, open(_p("pids.json"), "w"))
        f = "nbd:unix:" + _p("stick.sock")
    print(mon(f"drive_add 0 if=none,id=stk{n},file={f},format=raw", 2.0)[-200:])
    time.sleep(1)
    print(mon(f"device_add usb-storage,drive=stk{n},id=stick{n},port=1", 2.0)[-200:])
    time.sleep(1)
    print(mon("info usb", 1.0)[-300:])


def key(spec):
    b, m, *ms = spec.split(":")
    msg = f"{b}:{m}:{ms[0] if ms else 300}"
    socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(msg.encode(),
                                                           ("127.0.0.1", KEYPORT))


def qmp(cmds):
    """QMP: negotiate, then run each command dict"""
    s = socket.socket(socket.AF_UNIX)
    s.connect(_p("qmp.sock"))
    f = s.makefile("rw")
    f.readline()
    for c in [{"execute": "qmp_capabilities"}] + cmds:
        f.write(json.dumps(c) + "\n")
        f.flush()
        while True:                         # skip async events
            r = json.loads(f.readline())
            if "return" in r or "error" in r:
                break
        if "error" in r:
            print("QMP error:", r["error"])
    s.close()


def _ev(x, y, down):
    """one touch batch: the 800x480 screen maps onto QEMU's 0..32767 axes
    (cdj_pnl_touch.c pointer_event)"""
    ax = lambda v, n: int(round(v * 32767 / (n - 1)))
    return {"execute": "input-send-event", "arguments": {"events": [
        {"type": "abs", "data": {"axis": "x", "value": ax(x, 800)}},
        {"type": "abs", "data": {"axis": "y", "value": ax(y, 480)}},
        {"type": "btn", "data": {"down": down, "button": "left"}}]}}


# browse knob: KY 0x42/0x43 call 0x095423B0(+-1) = add to the u16 at
# 0x13BCB5E0 (fw 1.45; the 700's is 0x0B6C7B48). Our QEMU patch's
# m16add panel op adds to that word, one detent per message.
KNOB = 0x13BCB5E0


def rot(n):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for _ in range(abs(n)):
        s.sendto(f"{KNOB}:{1 if n > 0 else -1}:0:m16add".encode(), ("127.0.0.1", KEYPORT))
        time.sleep(0.25)


def touch(x, y, ms=250):
    qmp([_ev(x, y, True)])
    time.sleep(ms / 1000)
    qmp([_ev(x, y, False)])


def drag(x0, y0, x1, y1, steps=10, ms=400):
    qmp([_ev(x0, y0, True)])
    for i in range(1, steps + 1):
        time.sleep(ms / 1000 / steps)
        qmp([_ev(x0 + (x1 - x0) * i // steps, y0 + (y1 - y0) * i // steps, True)])
    qmp([_ev(x1, y1, False)])


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        sys.exit(__doc__)
    c = a[0]
    if c == "start":
        start(a[1] if len(a) > 1 else os.path.expanduser("~/Downloads/Rekordbox stuff"))
    elif c == "stop":
        stop()
    elif c == "shot":
        shot(a[1])
    elif c == "key":
        key(a[1])
    elif c == "rot":
        rot(int(a[1]))
    elif c == "drag":
        drag(*[int(v) for v in a[1:5]])
    elif c == "touch":
        touch(int(a[1]), int(a[2]), int(a[3]) if len(a) > 3 else 250)
    elif c == "plug":
        plug(int(a[1]) if len(a) > 1 else 1)
    elif c == "mon":
        print(mon(a[1], 1.0))
    else:
        sys.exit(__doc__)
