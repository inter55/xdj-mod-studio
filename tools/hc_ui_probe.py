#!/usr/bin/env python3
"""hc_ui_probe.py -- DECK, read-only: the stock hot cue pad tables, to learn
what makes a pad look 'set' vs 'empty / ready to record' (2026-11-17).

    .venv/bin/python tools/hc_ui_probe.py LABEL

Reads live slots, pad state, both pad models (records 0-2) and the UI tables
0x0C0CCB20.. and 0x0C0E6B00.. ; appends everything to
docs/deck_logs/hc_ui_<date>.log under LABEL so two states can be diffed.
"""
import os, sys, struct, time
sys.path[:0] = [".", "tools"]
from xdj700_usb_shell import PIONEER_VID, open_deck, drain
from rgb_overview_apply import read_block, send
import hid

AREAS = (("slots", 0x0B6C4118, 0xC0), ("padst", 0x0B6C3C68, 0x20),
         ("modelA", 0x0BA28800, 0xA8), ("modelB", 0x0BA2A52C, 0xA8),
         ("ui_ccb", 0x0C0CCB20, 0x100), ("ui_e6b", 0x0C0E6B00, 0x300))


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "state"
    c = [d for d in hid.enumerate(PIONEER_VID) if d["product_id"] == 8]
    if not c:
        print("no deck found on USB"); return 1
    h = open_deck(PIONEER_VID, c[0]["product_id"], 1)
    out = []
    try:
        drain(h, 1.0); send(h, "$CDJ", 1.5)
        for name, a, n in AREAS:
            b = bytes(read_block(h, a, n))
            out.append((name, a, b))
    finally:
        h.close()
    os.makedirs("docs/deck_logs", exist_ok=True)
    with open(time.strftime("docs/deck_logs/hc_ui_%Y-%m-%d.log"), "a") as f:
        f.write(f"==== {time.strftime('%H:%M:%S')} {label}\n")
        for name, a, b in out:
            for o in range(0, len(b), 16):
                ln = f"{name:7s} {a + o:08x}  " + " ".join(
                    "%08x" % w for w in struct.unpack_from("<%dI" % (min(16, len(b) - o) // 4), b, o))
                f.write(ln + "\n")
    print(f"saved '{label}' ({sum(len(b) for _, _, b in out)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
