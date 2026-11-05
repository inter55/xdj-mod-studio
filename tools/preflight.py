#!/usr/bin/env python3
"""preflight.py NEW.UPD [STOCK.UPD] -- check a built .UPD before flashing.

Every check is a thing that, if wrong, means the deck rejects the file or
fails mid-write. The boot-stub check is the brick-critical one: that code runs
before the payload and is what implements recovery mode (power on holding
IN + RELOOP/EXIT), so if it is byte-identical to stock a bad main image can
still be re-flashed from the top USB port. Note what this does NOT buy you:
the stub only compares the 16-bit block checksum (MAIN.img 0x0B7C). A correct
checksum means it runs the payload without looking at it, so a well-summed but
broken payload will crash rather than fail safe -- recovery mode still gets
there, because it is chosen at power-on before any payload code executes.
Recovery has been used on hardware: the deck that was bricked on 2026-09-05
was recovered with Pioneer's own 1.15 file through this path. See SAFETY.md.

Exit status 0 = all checks passed, 1 = at least one FAIL.
"""
import os, re, struct, sys, hashlib
from xdj700 import flatten, find_blocks, crc16_xmodem, MAGIC


def srec_parse(text):
    """Lenient parse: skip records that fail their checksum instead of
    raising, so preflight reports every problem rather than the first."""
    chunks, start = [], None
    for ln in text.split('\r\n'):
        for m in re.finditer(r'S([0-9])([0-9A-F]{2})([0-9A-F]+)', ln):
            t, cnt, rest = int(m.group(1)), int(m.group(2), 16), m.group(3)
            try:
                raw = bytes.fromhex(m.group(2) + rest[:cnt * 2])
            except ValueError:
                continue
            if len(raw) != cnt + 1 or (~sum(raw[:-1])) & 0xFF != raw[-1]:
                continue
            if t == 2:
                chunks.append((int(rest[:6], 16), bytes.fromhex(rest[6:-2])))
            elif t in (7, 8, 9):
                start = rest[:-2]
    return chunks, start

STOCK = os.environ.get('XDJ_STOCK',
        os.path.expanduser('~/Downloads/cdj3k-mods-0.1.0/XDJ700.UPD'))
BOOT_END = 0xC40           # boot stub: recovery mode lives here. Never touch.

ok = True


def chk(cond, label, detail=''):
    global ok
    ok &= bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ''))
    return cond


def sections(raw):
    e1 = raw.index(b'\r\n') + 2
    e2 = raw.index(b'\r\n', e1) + 2
    lens = [int(raw[:e1 - 2]), int(raw[e1:e2 - 2])]
    out, off = [], e2
    for L in lens:
        blob = raw[off:off + L]
        m = re.search(rb'XDJ-700\s+(\w+)Ver([\d.]+)', blob)
        out.append((m.group(1).decode() if m else '?',
                    m.group(2).decode() if m else '?', blob))
        off += L
    return e2, lens, out, off


def main(new, stock):
    raw = open(new, 'rb').read()
    print(f"\n{new}  ({len(raw):,} bytes)\n")

    print("container")
    hdr, lens, secs, end = sections(raw)
    chk(end == len(raw), 'header lengths account for every byte',
        f'{hdr} + {" + ".join(str(l) for l in lens)} = {end}')
    chk(len(secs) == 2, 'two sections present',
        ', '.join(f'{n} v{v}' for n, v, _ in secs))

    print("\nper-section")
    imgs = {}
    for (name, ver, blob), L in zip(secs, lens):
        text = blob.decode('latin1')
        nrec = bad = 0
        for ln in text.split('\r\n'):
            if not ln.startswith('S'):
                continue
            m = re.match(r'S([0-9])([0-9A-F]{2})([0-9A-F]+)$', ln)
            if not m:
                continue
            nrec += 1
            b = bytes.fromhex(m.group(2) + m.group(3))
            if (~sum(b[:-1])) & 0xFF != b[-1]:
                bad += 1
        chk(bad == 0, f'{name}: all {nrec:,} S-record checksums valid',
            '' if not bad else f'{bad} bad')
        want = struct.unpack('<H', blob[-2:])[0]
        got = crc16_xmodem(blob[:-2])
        chk(want == got, f'{name}: section CRC-16/XMODEM',
            f'stored {want:#06x}, computed {got:#06x}')
        chunks, start = srec_parse(text)
        img, base = flatten(chunks)
        imgs[name] = (img, base)
        # records must not overlap
        s = sorted(chunks)
        overlap = any(s[i][0] + len(s[i][1]) > s[i + 1][0] for i in range(len(s) - 1))
        chk(not overlap, f'{name}: no overlapping S-records')

    print("\nMAIN image")
    img, base = imgs['MAIN']
    blocks = find_blocks(img)
    chk(len(blocks) == 2, 'two compressed blocks found',
        ', '.join(f'@{h:#x} len={l}' for h, l in blocks))
    for i, (h, l) in enumerate(blocks):
        chk(img[h + 4:h + 8] == MAGIC, f'block{i}: magic intact')
        # the boot stub's own check (MAIN.img 0x0AF6): 16-bit byte sum over
        # [h, h+4+l), stored LE at h+4+l. Stale sum -> MAIN CPU ERROR on boot.
        e = h + 4 + l
        if e + 2 <= len(img):
            got = sum(img[h:e]) & 0xFFFF
            want = struct.unpack_from('<H', img, e)[0]
            chk(got == want, f'block{i}: trailing 16-bit block checksum',
                f'computed {got:#06x}, stored {want:#06x} at {e:#x}'
                + ('' if got == want else '  <-- THIS IS WHAT BRICKS THE DECK'))
        limit = blocks[i + 1][0] if i + 1 < len(blocks) else len(img)
        chk(h + 8 + l <= limit, f'block{i}: fits before next block',
            f'ends {h+8+l:#x}, limit {limit:#x}')
        dec, used = decode(img[h + 8:h + 8 + l])
        chk(used == l, f'block{i}: stream consumes exactly its declared length',
            f'{used}/{l}, {len(dec):,} bytes out')

    print("\nbrick safety")
    st = open(stock, 'rb').read()
    _, _, ssecs, _ = sections(st)
    simg, _ = flatten(srec_parse(ssecs[0][2].decode('latin1'))[0])
    chk(img[:BOOT_END] == simg[:BOOT_END],
        f'boot stub 0x0-{BOOT_END:#x} identical to stock',
        'recovery mode intact' if img[:BOOT_END] == simg[:BOOT_END]
        else '*** RECOVERY MODE AT RISK ***')
    pimg, _ = flatten(srec_parse(ssecs[1][2].decode('latin1'))[0])
    nimg, _ = imgs['PANL']
    chk(nimg == pimg, 'PANL section identical to stock')

    print("\nchanges vs stock")
    if len(img) == len(simg):
        d = [i for i in range(len(img)) if img[i] != simg[i]]
        print(f"  MAIN image: {len(d):,} of {len(img):,} bytes differ")
        if d:
            print(f"    range {d[0]:#x} .. {d[-1]:#x}")
    else:
        print(f"  MAIN image size {len(simg):,} -> {len(img):,}")
    for i, (h, l) in enumerate(blocks):
        sb = find_blocks(simg)
        if i < len(sb):
            a, _ = decode(img[h + 8:h + 8 + l])
            b, _ = decode(simg[sb[i][0] + 8:sb[i][0] + 8 + sb[i][1]])
            if a == b:
                print(f"  block{i} payload: unchanged")
            elif len(a) == len(b):
                dd = [k for k in range(len(a)) if a[k] != b[k]]
                print(f"  block{i} payload: {len(dd)} of {len(a):,} bytes differ"
                      f"  {[hex(x) for x in dd[:12]]}")
            else:
                print(f"  block{i} payload: length {len(b):,} -> {len(a):,}")

    print(f"\n{'ALL CHECKS PASSED' if ok else 'FAILURES PRESENT -- DO NOT FLASH'}\n")
    return 0 if ok else 1


def decode(src):
    N, F, THR = 4096, 18, 2
    rb = bytearray([0x20] * (N - F) + [0] * F)
    r, out, p, n = N - F, bytearray(), 0, len(src)
    while p < n:
        fl = src[p]; p += 1
        for b in range(8):
            if p >= n:
                return bytes(out), p
            if fl >> b & 1:
                c = src[p]; p += 1
                out.append(c); rb[r] = c; r = (r + 1) & (N - 1)
            else:
                if p + 1 >= n:
                    return bytes(out), p
                b1, b2 = src[p], src[p + 1]; p += 2
                i = b2 | ((b1 & 0xF0) << 4); j = (b1 & 0x0F) + THR + 1
                for k in range(j):
                    c = rb[(i + k) & (N - 1)]
                    out.append(c); rb[r] = c; r = (r + 1) & (N - 1)
    return bytes(out), p


if __name__ == '__main__':
    sys.exit(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else STOCK))
