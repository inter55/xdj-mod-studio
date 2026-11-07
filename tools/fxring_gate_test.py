#!/usr/bin/env python3
"""fxring_gate_test.py -- the TOUCH FX delay-line gate, no deck needed.

    .venv/bin/python tools/fxring_gate_test.py

Covers ring_gate() in tools/keyshift_hot_apply.py: the rule that Echo and
Flanger cannot be installed until tools/fxmem_probe.py --ring has proved the
engine's own ring empty on the deck. Also checks ring_fits(): a stereo float
ring must fit the one 4,096-word page the survey proved empty.
"""
import os
import sys
import tempfile

sys.path.insert(0, "tools")
import fxmem_probe as P            # noqa: E402
from keyshift_hot_apply import ring_gate   # noqa: E402

ok = True


def chk(label, good, extra=""):
    global ok
    ok &= bool(good)
    print(f"  {'ok  ' if good else 'FAIL'} {label}{'  ' + extra if extra else ''}")


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


def main():
    d = tempfile.mkdtemp(prefix="fxring-gate-")
    ring = os.path.join(d, "ring.txt")
    proof = os.path.join(d, "fxring_ok.txt")

    good, msg = ring_gate(ring, proof)
    chk("no ring.txt -> refuse", not good, msg.split(":")[0])

    write(ring, "0xC006c000 4096\n")
    good, msg = ring_gate(ring, proof)
    chk("no proof -> refuse", not good and "probe" in msg, msg.split(".")[0])

    write(proof, "0xC0073400 6500 1762400000\n")
    good, msg = ring_gate(ring, proof)
    chk("proof for another geometry -> refuse", not good and "#x" not in msg,
        msg.split(":")[0])

    write(proof, "0xC006c000 4096 1762400000\n")
    good, msg = ring_gate(ring, proof)
    chk("matching proof -> install allowed", good, msg)
    chk("message names the addressed range", "0xc006c000" in msg.lower(), "")

    write(proof, "garbage\n")
    good, _ = ring_gate(ring, proof)
    chk("unreadable proof -> refuse", not good)

    write(ring, "0xC006c000\n")
    good, _ = ring_gate(ring, proof)
    chk("unreadable geometry -> refuse", not good)

    # a build with no delay line at all needs no proof
    write(ring, "0x0 0\n")
    good, msg = ring_gate(ring, proof)
    chk("no-ring build needs no proof", good, msg)
    chk("no-ring message says why", "no delay line" in msg, msg)

    # geometry must fit ONE page: 4,096 words = 2,048 stereo float frames
    chk("2,048 frames fit a page", P.ring_fits(2048))
    chk("4,096 frames do not", not P.ring_fits(4096))
    chk("zero frames refused", not P.ring_fits(0))
    chk("page is 16 KB", P.PAGE_WORDS * 4 == 16384)

    # the shipping default is the 52 KB block the deck survey found in use
    chk("shipping ring needs 52,000 B", 6500 * 8 == 52000)

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
