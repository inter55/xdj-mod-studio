#!/usr/bin/env python3
"""shasm_test.py -- prove tools/shasm.py: assemble a memcpy, run it on the
emulated SH-4 (checkpoint), verify the copy. Exercises labels, branches,
delay slots, dt, mov.b forms."""
import sys
sys.path.insert(0, ".")
sys.path.insert(0, "tools")
from emu.machine import Machine
from emu.sh4 import PcHalt
from shasm import assemble

SRC = """
memcpy:
  tst r6, r6
  bt done
loop:
  mov.b @r5+, r0
  mov.b r0, @r4
  add #1, r4
  dt r6
  bf loop
done:
  rts
  nop
"""

CODE_ADDR = 0x0B100000
SRC_ADDR = 0x0B110000
DST_ADDR = 0x0B120000
MAGIC = 0x08000118


def main():
    code = assemble(SRC)
    print(f"assembled {len(code)} bytes:", code.hex(" "))
    import os
    if len(sys.argv) > 1:
        ckpt = sys.argv[1]
    else:
        # the user's own firmware home (emu.fwhome), falling back to the
        # old research checkpoint for machines that still have it
        from emu import fwhome
        ckpt = fwhome.path("mainscreen.pkl.gz")
        if not os.path.exists(ckpt):
            ckpt = os.path.expanduser("~/emu-ckpt/emu_clean3.pkl")
    if not os.path.exists(ckpt):
        sys.exit(f"no emulator snapshot at {ckpt}: run "
                 "`python -m emu.fwhome setup XDJ700.UPD` first, or pass a .pkl")
    m = Machine.load(ckpt, core="py")
    c, mem = m.cpu, m.mem
    for i, b in enumerate(code):
        mem.w8(CODE_ADDR + i, b)
    data = bytes((i * 7 + 3) & 0xFF for i in range(64))
    for i, b in enumerate(data):
        mem.w8(SRC_ADDR + i, b)
    for i in range(64):
        mem.w8(DST_ADDR + i, 0xA5)
    c.sr |= 0x10000000
    c.b0[4], c.b0[5], c.b0[6] = DST_ADDR, SRC_ADDR, 64
    c.b1[4], c.b1[5], c.b1[6] = DST_ADDR, SRC_ADDR, 64  # RB-state-proof
    c.pr = MAGIC
    c.pc = CODE_ADDR

    def tr(pc, op):
        if pc == MAGIC:
            raise PcHalt(pc)
    c.trace = tr
    res = c.run(limit=100000)
    c.trace = None
    print("run:", res)
    got = bytes(mem.r8(DST_ADDR + i) for i in range(64))
    ok = got == data and "8000118" in str(res)
    print("RESULT:", "PASS" if ok else f"FAIL got={got.hex()}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
