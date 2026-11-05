#!/usr/bin/env python3
"""pack_builder.py -- builds the USB-stick mods pack (XDJP001) for the
USB-stick loader (docs/USBLOADER.md).

Pack layout (little-endian):
  0x00  8 B  magic "XDJP001"
  0x08  u16  format revision (1)
  0x0a  u16  payload count
  0x0c  u32  pack CRC32 (over the entry TABLE only, 0x10..0x10+n*28:
        bodies stay CRC-per-payload so one bad body never blocks the
        rest)
  0x10  entries, 28 B each:
        name[8] (zero-padded), ram u32 (home in deck RAM),
        declen u32 (uncompressed length), crc u32 (CRC32 of the body
        UNcompressed), arm u16 (hook-arm bits), pad u16, zlen u32
        (compressed body length)
  bodies: LZSS (tools/lzss_boot.py encoder -- the same codec the boot
        copier already carries), each aligned up 0x10.

Arm bits: 1 centre, 2 keyshift, 4 stems, 8 hotcue8, 16 phasemeter,
          32 themes, 64 flac.

Manifest JSON (list of payloads): [{"name": "phasemeter", "file":
"build/phasemeter/pm.bin", "ram": "0x09798000", "arm": 16}, ...]. Ram
defaults for the known payloads come from the install scripts; anything
else must carry "ram" explicitly.

  pack_builder.py --manifest m.json --out $STICK/XDJMODS/PACK.BIN
  pack_builder.py --selftest
"""
import argparse
import binascii
import json
import os
import struct
import sys

sys.path[:0] = ["tools", "tools/phasemeter", "."]
import lzss_boot as L  # noqa: E402

MAGIC = b"XDJP001\x00"          # 8-byte slot; docs call it "XDJP001"
REV = 1
HEADER = 0x10
ENTRY = 28

# known RAM homes (see docs/USBLOADER.md / the install scripts)
HOMES = {"centre": 0x098A0000,
         "keyshift": 0x09800000,   # ksh.bin link address (KSH_BASE); 0x097A0000 was the RETIRED ksr DSP-copy area
         "stems": 0x09836000,
         "hotcue8": 0x09790000,
         "phasemeter": 0x09798000,
         "themes": 0x09779000}
ALIASES = {"pm": "phasemeter",      # payload-name -> HOMES key
           "sm": "stems", "hc": "hotcue8", "ts": "themes",
           "tables": "themes", "cw": "centre", "ks": "keyshift"}


def build(payloads):
    """payloads: list of dicts {name, ram, arm, data(bytes)} -> pack bytes."""
    ents, bodies = [], b""
    off = HEADER + len(payloads) * ENTRY          # first body's file offset
    for p in payloads:
        name = p["name"].encode()[:8]
        ram = int(p["ram"], 0) if isinstance(p["ram"], str) else int(p["ram"])
        zbody = bytes(L.encode(p["data"]))
        pad = (-off) % 0x10                       # align against the FILE
        ents.append(struct.pack("<8sIIIHHI", name, ram, len(p["data"]),
                                binascii.crc32(p["data"]) & 0xFFFFFFFF,
                                int(p.get("arm", 0)), 0, len(zbody)))
        bodies += b"\x00" * pad + zbody
        off += pad + len(zbody)
    nents = len(ents)
    head = MAGIC + struct.pack("<HHI", REV, nents, 0)
    head += b"".join(ents)
    pack = head + bodies
    tbl_end = 0x10 + nents * ENTRY
    crc = binascii.crc32(pack[0x10:tbl_end]) & 0xFFFFFFFF
    return pack[:0xC] + struct.pack("<I", crc) + pack[0x10:]


def parse(pack):
    """-> (list of {name, ram, declen, crc, arm, body}, errors)"""
    errs = []
    if pack[:8] != MAGIC:
        return [], ["bad magic"]
    rev, n, crc = struct.unpack_from("<HHI", pack, 8)
    if rev != REV:
        errs.append(f"revision {rev} != {REV}")
    tbl_end = 0x10 + n * ENTRY
    if binascii.crc32(pack[0x10:tbl_end]) & 0xFFFFFFFF != crc:
        errs.append("pack CRC mismatch")
    ents, off = [], HEADER
    for i in range(n):
        if off + ENTRY > len(pack):
            errs.append(f"truncated entry {i}")
            return ents, errs
        name, ram, declen, ecrc, arm, _, zlen = struct.unpack_from(
            "<8sIIIHHI", pack, off)
        ents.append({"name": name.rstrip(b"\x00").decode(errors="replace"),
                     "ram": ram, "declen": declen, "crc": ecrc, "arm": arm,
                     "zlen": zlen})
        off = off + ENTRY
    off = HEADER + n * ENTRY                       # bodies start here
    for e in ents:
        off = (off + 0xF) & ~0xF
        e["body"] = pack[off:off + e["zlen"]]
        if len(e["body"]) != e["zlen"]:
            errs.append(f"{e['name']}: body truncated")
        off = off + e["zlen"]
    return ents, errs


def verify(pack):
    """Round-trip decode: returns (ok, report)."""
    ents, errs = parse(pack)
    for e in ents:
        try:
            raw = L.decode(e["body"])
        except Exception as ex:                       # noqa: BLE001
            errs.append(f"{e['name']}: decode failed {ex}")
            continue
        e["dec"] = len(raw)
        if len(raw) != e["declen"]:
            errs.append(f"{e['name']}: length {len(raw)} != {e['declen']}")
        elif binascii.crc32(raw) & 0xFFFFFFFF != e["crc"]:
            errs.append(f"{e['name']}: CRC mismatch")
    return not errs, errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest")
    ap.add_argument("--out")
    ap.add_argument("--dump")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        ok = selftest()
        sys.exit(0 if ok else 1)
    if a.dump:
        raw = open(a.dump, "rb").read()
        ents, errs = parse(raw)
        for e in ents:
            print(f"{e['name']:<10} ram {e['ram']:#010x} arm {e['arm']:#06x} "
                  f"declen {e['declen']:>9,} crc {e['crc']:#010x}")
        for m in errs:
            print("  !", m)
        sys.exit(1 if errs else 0)
    if not a.manifest or not a.out:
        ap.error("--manifest and --out (or --selftest)")
    man = json.load(open(a.manifest))
    payloads = []
    for p in man:
        payloads.append({"name": p["name"],
                         "ram": p.get("ram", hex(HOMES[ALIASES.get(p["name"],
                                                                  p["name"])]
                                                              if (ALIASES.get(p["name"], p["name"]) in HOMES) else 0)),
                         "arm": p.get("arm", 0),
                         "data": open(p["file"], "rb").read()})
        if int(str(payloads[-1]["ram"]), 0) == 0:
            raise SystemExit(f"payload {p['name']}: no ram home")
    pack = build(payloads)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    open(a.out, "wb").write(pack)
    ok, errs = verify(pack)
    print(f"{a.out}: {len(pack):,} bytes, {len(payloads)} payloads, "
          f"{'ROUND-TRIP OK' if ok else 'VERIFY FAILED: ' + '; '.join(errs)}")
    sys.exit(0 if ok else 1)


def selftest():
    good = True
    # a payload whose bytes ARE already LZSS in the wild: the pm cave binary
    for f in ("build/phasemeter/pm.bin",):
        if not os.path.exists(f):
            print(f"selftest: {f} missing (run build.sh first)"); continue
        data = open(f, "rb").read()
        for arm in (16,):
            pack = build([{"name": "pm", "ram": HOMES["phasemeter"],
                       "arm": arm, "data": data}])
            ok, errs = verify(pack)
            print(f"  {f}: {len(data):,} -> {len(pack):,} packed "
                  f"(arm {arm}): {'OK' if ok else errs}")
            good &= ok
            ents, errs = parse(pack)
            assert (not errs) and ents[0]["name"] == "pm" and \
                ents[0]["ram"] == HOMES["phasemeter"]
            # corruption must be caught
            bad = bytearray(pack)
            bad[0x40] ^= 0xFF
            ok, errs = verify(bytes(bad))
            print(f"  corrupted copy: {'caught' if not ok else 'MISSED'} -> "
                  f"{errs[:1]}")
            good &= not ok
    print("SELFTEST", "PASS" if good else "FAIL")
    return good


if __name__ == "__main__":
    main()
