#!/usr/bin/env python3
"""littest.py -- does literal fraction predict decode correctness?

The oracle (tools/oracle.py) gives 177 peripheral-register constants in the
OKU decode of block1, of which 84 are "coupled" (targeted by a
`mov.l @(disp,PC),Rn`, which a correct decode requires) and 93 are not.

If our codec's failure is what breaks coupling, then coupled constants should
sit in LITERAL-rich neighbourhoods and uncoupled ones in MATCH-rich ones.
That would make literal fraction a usable per-region trust metric -- which is
what we need in order to know whether a given disassembly is worth tracing.

    littest.py
"""
import os, struct, sys, json, statistics
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from provmap import Prov, BASE

HERE = os.path.dirname(os.path.abspath(__file__))
REGS = json.load(open(os.path.join(HERE, '..', 'docs', 'sh7724_registers.json')))
REAL = set(REGS.values())
WIN = 512


def movl_targets(buf):
    t = set()
    for p in range(0, len(buf) - 1, 2):
        w = buf[p] | (buf[p + 1] << 8)
        if (w & 0xF000) == 0xD000:
            a = ((p + 4) & ~3) + (w & 0xFF) * 4
            if 0 <= a < len(buf) - 3:
                t.add(a)
    return t


def main():
    if not os.path.exists(os.path.join(HERE, "..", "work", "OKU.blk01.bin")):
        print("SKIP: work/OKU.blk01.bin missing -- it is a block of the user's "
              "own firmware image (never shipped with this repo); put it in "
              "work/ before running this")
        return 0
    P = Prov()
    buf = P.img
    print('scanning for mov.l pool targets ...', file=sys.stderr)
    ptd = movl_targets(buf)
    consts = [o for o in range(0, len(buf) - 3, 4)
              if struct.unpack_from('<I', buf, o)[0] in REAL]
    coupled = [o for o in consts if o in ptd]
    uncoupled = [o for o in consts if o not in ptd]
    print(f'constants={len(consts)}  coupled={len(coupled)}  uncoupled={len(uncoupled)}\n')

    def stats(lst, label):
        fr = [P.litfrac(max(0, o - WIN), o + WIN) * 100 for o in lst]
        fr.sort()
        med = statistics.median(fr)
        hi = sum(1 for f in fr if f >= 35) / len(fr) * 100
        print(f'{label:<22} n={len(fr):>4}  median lit%={med:5.1f}  '
              f'mean={statistics.mean(fr):5.1f}  >=35% lit: {hi:4.1f}% of them')
        return fr

    a = stats(coupled, 'COUPLED (decodes ok)')
    b = stats(uncoupled, 'UNCOUPLED (broken)')

    # rank-sum sanity
    allv = sorted(a + b)
    ra = sum(allv.index(x) for x in a) / len(a)
    rb = sum(allv.index(x) for x in b) / len(b)
    print(f'\nmean rank: coupled {ra:.0f} vs uncoupled {rb:.0f} (of {len(allv)})')

    # decision threshold
    print('\nliteral-fraction threshold sweep (window +-512B):')
    print(f"{'thresh':>7} {'coupled kept':>13} {'uncoupled kept':>15}")
    for t in (10, 20, 25, 30, 35, 40, 50, 60):
        ka = sum(1 for f in a if f >= t) / len(a) * 100
        kb = sum(1 for f in b if f >= t) / len(b) * 100
        print(f'{t:>6}% {ka:12.1f}% {kb:14.1f}%')


if __name__ == '__main__':
    main()
