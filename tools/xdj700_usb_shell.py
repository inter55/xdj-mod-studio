#!/usr/bin/env python3
"""XDJ-700 USB debug-shell client (RAM reads + gated RAM writes).

Talks to the deck over USB HID like the research in
Thomas-Geraghty/pioneer-dj-usb-shell describes for the XDJ-1000MK2:
send a handshake, read the reply. Memory-write syntax (`BW`) verified
three ways: the upstream docs, the 700's own 232C table at file 0x6DC34
(BW argc=2 radix=16 handler 0x8B29920), and disassembly of that handler
(`mov.b arg1 -> [arg0]`: address first, value second).

Usage (at the studio, deck connected over rear USB-B, powered on):
    .venv/bin/python tools/xdj700_usb_shell.py --list
    .venv/bin/python tools/xdj700_usb_shell.py --handshake [--vid PID] [--pid PID]

Requires: the bundled vendor/hid plus Homebrew's libhidapi (brew install
hidapi). PYTHONPATH="$PWD/vendor" from a source checkout; the frozen app adds
it itself. pip install hidapi is NOT enough: that is cython-hidapi, which has
no hid.Device.
Pioneer DJ USB vendor ID: 0x2B73 (XDJ-700 shows pid 0x0008).

SAFETY: --bw is gated to the RAM window 0x08000000-0x0FFFFFFF (same as
--br) and only writes a single byte. YE (flash erase) and all other
state-changing commands are deliberately NOT in this script.
"""
import argparse
import sys
import os
import time

# The frozen app ships a working HID binding in vendor/ (added to sys.path by
# its pathex); a source checkout should use the same one. pip's "hidapi" is
# cython-hidapi, which has no hid.Device -- vendor/hid does, and only needs
# libhidapi from Homebrew (brew install hidapi).
VENDOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "vendor")
# XDJ_MOCK_SOCK = the pretend deck (tools/mock/mockdeck_server.py). Its
# `hid` stand-in has to come from the caller's path, so do NOT put the real
# vendor/hid in front of it: doing that made tools/stick_pack.py record (and
# mock_install.py) report "deck not found over USB" while a real-hid module
# was what they had actually imported.
if (not os.environ.get("XDJ_MOCK_SOCK")
        and os.path.isdir(os.path.join(VENDOR, "hid"))
        and VENDOR not in sys.path):
    sys.path.insert(0, VENDOR)

try:
    import hid
except ImportError as e:
    sys.exit(
        "need the 'hid' package (import failed: %r)\n"
        "  macOS, from a source checkout:\n"
        "    brew install hidapi\n"
        "    PYTHONPATH=\"$PWD/vendor\" .venv/bin/python "
        "tools/xdj700_usb_shell.py --list\n"
        "  pip install hidapi installs cython-hidapi, which has no hid.Device;\n"
        "  the bundled vendor/hid is the one the deck tools need." % (e,))

PIONEER_VID = 0x2B73  # Pioneer DJ USB vendor ID (XDJ-700 shows pid 0x0008)

# Read-only query commands. ?V asks firmware version, BP player status,
# ER error history, KL panel key-log dump (all documented read-only).
QUERY_CMDS = {
    "version": b"?V\r\x00",
    "status": b"BP\r\x00",
    "errors": b"ER\r\x00",
    "keylog": b"KL\r\x00",
}
# BR byte-read: "<addr>,<count-minus-one>BR", address as bare hex matching
# the shell's own convention (e.g. 80897a4,fBR reads 16 bytes). RAM reads
# only - never hardware registers (reads there can have side effects).
# Added as --br ADDR COUNT (both hex, COUNT = bytes wanted, 1..64).
# KY key injection (ctsh debug command, per BEATJUMP.md): emulates a key
# press by event code, auto-releases after ~20ms. Same effect as pressing
# a button on the deck - no flash, no firmware change. Beat jump codes:
# Rev1/2/4 = 57/58/59, Fwd1/2/4 = 5A/5B/5C (from the 0x6DD7C event table).
KY_CMDS = {
    "rev1": b"57KY\r\x00",
    "rev2": b"58KY\r\x00",
    "rev4": b"59KY\r\x00",
    "fwd1": b"5AKY\r\x00",
    "fwd2": b"5BKY\r\x00",
    "fwd4": b"5CKY\r\x00",
}


def list_devices():
    devs = hid.enumerate()
    print(f"{len(devs)} HID device(s) present")
    for d in devs:
        tag = "  <-- PIONEER" if d["vendor_id"] == PIONEER_VID else ""
        print(f"  vid={d['vendor_id']:#06x} pid={d['product_id']:#06x} "
              f"usage={d['usage_page']:#06x}/{d['usage']:#04x} "
              f"{d.get('product_string', '')!r} {d.get('serial_number', '')!r}{tag}")
    return [d for d in devs if d["vendor_id"] == PIONEER_VID]


PACE_S = float(os.environ.get("XDJ_SHELL_PACE", "30"))   # min s between sessions
_STAMP = os.path.expanduser(os.environ.get("XDJ_SHELL_STAMP", "~/.xdj700_shell_last"))


def _pace():
    """Rapid re-opens wedge the deck's USB (CLAUDE.md §3, and 2026-10-04:
    a re-open seconds after a 4-min session timed out). Every tool goes
    through open_deck, so enforce the gap here."""
    try:
        last = os.path.getmtime(_STAMP)
    except OSError:
        return
    wait = PACE_S - (time.time() - last)
    if wait > 0:
        print(f"pacing: waiting {wait:.0f}s since the last shell session")
        time.sleep(wait)


def _stamp():
    with open(_STAMP, "w") as f:
        f.write(str(time.time()))


_LOCK = None


def _lock():
    """One shell session at a time (two tools racing for the HID port
    happened 2026-10-04). The lock lives until the process exits."""
    global _LOCK
    if _LOCK is None:
        _LOCK = open(os.path.expanduser(os.environ.get(
            "XDJ_SHELL_LOCK", "~/.xdj700_shell_lock")), "w")
        if os.name == "nt":                     # Windows: msvcrt byte lock
            import msvcrt
            try:
                msvcrt.locking(_LOCK.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                print("another deck session is running - waiting for it")
                while True:
                    try:
                        msvcrt.locking(_LOCK.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(1)
            return
        import fcntl
        try:
            fcntl.flock(_LOCK, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("another deck session is running - waiting for it")
            fcntl.flock(_LOCK, fcntl.LOCK_EX)


OPEN_TIME = None          # when this process opened its shell session


# One-process installs (tools/deck_session.py sets XDJ_SHARE_SESSION=1):
# consecutive tools reuse the open connection while it is younger than
# SHARE_MAX_S instead of paying a 30 s cool-down each; an older one is
# closed, paced and reopened as usual. close() is a no-op while shared;
# end_shared() (also at exit) really closes it.
SHARE_MAX_S = float(os.environ.get("XDJ_SHARE_MAX_S", "30"))
OPENS = 0
_SHARED_H = None


def end_shared():
    global _SHARED_H
    if _SHARED_H is not None:
        h, _SHARED_H = _SHARED_H, None
        h.real_close()


_PINNED = [""]            # serial of the deck this process talks to


def open_deck(vid, pid, usage=None):
    global OPEN_TIME, OPENS, _SHARED_H
    shared = os.environ.get("XDJ_SHARE_SESSION") == "1"
    if shared and _SHARED_H is not None:
        if time.time() - OPEN_TIME < SHARE_MAX_S:
            print(f"reusing the open deck session ({time.time() - OPEN_TIME:.0f}s old)")
            return _SHARED_H
        end_shared()
    _lock()
    _pace()
    _stamp()
    OPEN_TIME = time.time()
    OPENS += 1
    # 2026-10-25 (tester with two XDJ-700s on one PC): every tool used to
    # open the FIRST matching device, so one install could land half on
    # each deck. Now: exactly one deck, pinned by serial for the whole
    # process (XDJ_DECK_SERIAL picks one when several are connected).
    devs = [d for d in hid.enumerate(vid, pid)
            if usage is None or d.get("usage") == usage]
    want = os.environ.get("XDJ_DECK_SERIAL") or _PINNED[0]
    if want:
        devs = [d for d in devs if (d.get("serial_number") or "") == want]
        if not devs:
            raise IOError(f"deck {want} is not connected")
    else:
        sers = sorted({d.get("serial_number") or "" for d in devs})
        if len(sers) > 1:
            raise IOError("more than one deck is connected (" + ", ".join(sers) +
                          "): plug in one deck at a time")
    if not devs:
        raise IOError(f"no interface with usage {usage:#x}" if usage is not None
                      else "no deck connected")
    _PINNED[0] = devs[0].get("serial_number") or ""
    h = hid.Device(path=devs[0]["path"])
    h.nonblocking = True
    _close = h.close

    def close_and_stamp():
        _stamp()                      # pace from the END of the session
        _close()
    h.close = close_and_stamp
    if shared:
        h.real_close = close_and_stamp
        h.close = lambda: None
        _SHARED_H = h
        import atexit
        atexit.register(end_shared)
    print(f"opened vid={vid:#06x} pid={pid:#06x}")
    try:
        print("manufacturer:", h.manufacturer)
        print("product     :", h.product)
        print("serial      :", h.serial)
        print("report descriptor:")
        show("descriptor", h.get_report_descriptor())
    except Exception as e:
        print("strings/descriptor unavailable:", e)
    return h


def drain_values(h, n, secs=9.0):
    """Collect a BR reply until n 'XX' values (+ line end) arrived or secs
    elapse -- usually well under a second instead of a fixed wait."""
    import re
    out = bytearray()
    end = time.time() + secs
    while time.time() < end:
        chunk = h.read(64, timeout=100)
        if chunk:
            out.extend(chunk)
            txt = out.decode("ascii", "replace")
            if len(re.findall(r"\b[0-9A-Fa-f]{2}\b(?:\s|$)", txt)) >= n \
                    and ("\n" in txt or "\r" in txt):
                # (reports are zero-padded after CRLF, so the line end is
                # not necessarily in the last bytes)
                break
        else:
            time.sleep(0.02)
    return bytes(out)


def drain(h, secs=1.5):
    """Collect everything the deck sends within `secs`. Returns raw bytes."""
    out = bytearray()
    end = time.time() + secs
    while time.time() < end:
        chunk = h.read(64, timeout=100)
        if chunk:
            out.extend(chunk)
        else:
            time.sleep(0.05)
    return bytes(out)


def show(tag, data):
    print(f"--- {tag} ({len(data)} bytes) ---")
    for i in range(0, len(data), 16):
        blk = data[i:i + 16]
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in blk)
        print(f"  {i:04x}  {blk.hex(' '):<48}  {asc}")
    if not data:
        print("  (nothing)")


def validate_args(a):
    """Refuse bad --br/--bw/--lw arguments with sys.exit, no HID traffic."""

    def hx(s, what):
        try:
            return int(s, 16)
        except ValueError:
            sys.exit(f"bad hex for {what}: {s!r}")

    if a.br:
        addr = hx(a.br[0], "ADDR")
        count = hx(a.br[1], "COUNT")
        if not (1 <= count <= 64):
            sys.exit("COUNT must be 1..64 (hex)")
        if not (0x08000000 <= addr < 0x10000000 or
                0xFE008000 <= addr < 0xFE00A000 or
                0xFDC08000 <= addr < 0xFDC08200):
            sys.exit("ADDR must be in RAM window 0x08000000-0x0FFFFFFF "
                     "or DMAC window 0xFE008000-0xFE00A000 "
                     "or DMA1 block 0xFDC08000-0xFDC08200 (refusing "
                     "other hardware registers)")
    if a.bw:
        addr = hx(a.bw[0], "ADDR")
        val = hx(a.bw[1], "VALUE")
        if not (0 <= val <= 0xFF):
            sys.exit("VALUE must fit in one byte (hex 0..ff)")
        if not (0x08000000 <= addr < 0x10000000):
            sys.exit("ADDR must be in RAM window 0x08000000-0x0FFFFFFF "
                     "(refusing hardware registers)")
    if a.lw:
        addr = hx(a.lw[0], "ADDR")
        val = hx(a.lw[1], "VALUE")
        if not (0 <= val <= 0xFFFFFFFF):
            sys.exit("VALUE must fit in 32 bits (hex 0..ffffffff)")
        if not (0x08000000 <= addr < 0x10000000):
            sys.exit("ADDR must be in RAM window 0x08000000-0x0FFFFFFF "
                     "(refusing hardware registers)")
        if addr & 3:
            sys.exit(f"ADDR {addr:#x} not 4-aligned (LW masks with -4; "
                     "refusing rather than writing next door)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--handshake", action="store_true")
    ap.add_argument("--vid", type=lambda x: int(x, 0), default=PIONEER_VID)
    ap.add_argument("--pid", type=lambda x: int(x, 0), default=None)
    ap.add_argument("--usage", type=lambda x: int(x, 0), default=0x01,
                    help="HID usage id (deck exposes 0x01 shell, 0x02 other)")
    ap.add_argument("--cmd", choices=sorted(QUERY_CMDS), default=None,
                    help="read-only query to send after handshake")
    ap.add_argument("--ky", choices=sorted(KY_CMDS), default=None,
                    help="emulate a key press by event code (auto-releases; "
                         "same effect as pressing a deck button)")
    ap.add_argument("--br", nargs=2, metavar=("ADDR", "COUNT"), default=None,
                     help="RAM byte-read: hex ADDR and hex COUNT (bytes, 1..64). "
                          "RAM only, never hardware registers.")
    ap.add_argument("--bw", nargs=2, metavar=("ADDR", "VALUE"), default=None,
                     help="RAM byte-write: hex ADDR and hex VALUE (one byte). "
                          "RAM window only. Always read-verify after write.")
    ap.add_argument("--lw", nargs=2, metavar=("ADDR", "VALUE"), default=None,
                     help="RAM long-write: hex ADDR (4-aligned) and hex VALUE "
                          "(up to 8 digits, 32-bit). Same family as BW "
                          "(table + mov.l handler); ~4x fewer rounds for bulk "
                          "installs. Prove with tools/lw_noop_test.py first.")
    a = ap.parse_args()

    if a.list or not (a.handshake or a.list):
        plist = list_devices()
        if not plist:
            print("no Pioneer deck found - check USB-B cable, power, then retry")
        return

    if a.pid is None:
        cands = [d for d in hid.enumerate()
                 if d["vendor_id"] == a.vid and d["usage"] == a.usage]
        if len(cands) != 1:
            print(f"found {len(cands)} matching device(s); pass --pid explicitly")
            list_devices()
            return
        a.pid = cands[0]["product_id"]

    # Validate everything BEFORE touching the deck: a bad argument must
    # never cost a paced, locked shell session (each open wedges the USB
    # budget). All checks exit(2)-style with a message, zero HID traffic.
    validate_args(a)

    h = open_deck(a.vid, a.pid, a.usage)
    try:
        print("draining pre-existing traffic...")
        show("pre-traffic", drain(h, 1.0))
        hs = b"$CDJ\r\x00"
        print(f"handshake {hs!r} ...")
        h.write(hs + b"\x00" * (26 - len(hs)))
        show("handshake reply", drain(h, 1.5))
        if a.cmd:
            q = QUERY_CMDS[a.cmd]
            print(f"query {a.cmd} {q!r} ...")
            h.write(q + b"\x00" * (26 - len(q)))
            show(f"reply to {a.cmd}", drain(h, 1.5))
        if a.ky:
            q = KY_CMDS[a.ky]
            print(f"key inject {a.ky} {q!r} (auto-releases, like a button press) ...")
            h.write(q + b"\x00" * (26 - len(q)))
            show(f"reply to KY {a.ky}", drain(h, 1.5))
        if a.br:
            addr = int(a.br[0], 16)
            count = int(a.br[1], 16)
            if 0x08000000 <= addr < 0x10000000:
                kind = "RAM"
            elif 0xFE008000 <= addr < 0xFE00A000:
                # DMAC SAR/DAR/TCR/CHCR reads are side-effect-free per the
                # SH manual (address/count regs never clear on read).
                # Nothing else outside RAM is allowed.
                kind = "DMAC register"
                print(f"warning: {addr:#x} is a {kind} (read-only use)")
            else:
                # DMA1 block (user-approved 2026-09-30, reads only).
                kind = "DMA1 register"
                print(f"warning: {addr:#x} is a {kind} (read-only use)")
            q = f"{addr:x},{count - 1:x}BR\r\x00".encode()
            print(f"{kind} read {q!r} ...")
            h.write(q + b"\x00" * (26 - len(q)))
            show(f"RAM at {addr:#x}", drain(h, 2.0))
        if a.bw:
            addr = int(a.bw[0], 16)
            val = int(a.bw[1], 16)
            q = f"{addr:x},{val:x}BW\r\x00".encode()
            print(f"RAM write {q!r} ...")
            h.write(q + b"\x00" * (26 - len(q)))
            show(f"reply to BW {addr:#x}", drain(h, 2.0))
        if a.lw:
            addr = int(a.lw[0], 16)
            val = int(a.lw[1], 16)
            q = f"{addr:x},{val:x}LW\r\x00".encode()
            print(f"RAM long-write {q!r} ...")
            h.write(q + b"\x00" * (26 - len(q)))
            show(f"reply to LW {addr:#x}", drain(h, 2.0))
    finally:
        h.close()
    print("done - RAM window only; no flash command exists in this script")


if __name__ == "__main__":
    main()
