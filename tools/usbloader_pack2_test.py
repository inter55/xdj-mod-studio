"""usbloader_pack2_test.py -- the mods FILE on a USB stick, end to end in the
emulator (nothing here touches a deck).

From a booted deck with NO mods (cache/emu_dsp_mounted6.pkl: real DSP), put
the loader in RAM (build/usbloader/ld.bin, pool 0x08DE39A4 armed last),
then mount a stick carrying XDJMODS/PACK.BIN (tools/stick_pack.py: a
recorded cable install) and check:

  good     every byte the cable install wrote is there, bit for bit
           (final value per address from the write log); status DONE;
           afterwards the deck keeps running and the live key shift
           one-shot has patched the DSP (call word 0x00ACEC10)
  badbody  one body corrupted mid-pack -> replay stops AT it: everything
           before is applied, nothing after (incl. its hook arms)
  truncated  the file cut in half (a half-copied stick) -> nothing past
           the cut is applied (the loader refuses it or stops at the cut)
  oldbyte  a stock pool word the installer checked is changed before the
           mount -> NOTHING is written at all (two-phase check)
  every case: the stick mount completes (Browse opens it)

Usage: PACK=/tmp/PACK.BIN LOG=/tmp/stick_wlog.bin \\
       .venv/bin/python tools/usbloader_pack2_test.py [good|badbody|oldbyte|all]
"""
import os
import shutil
import struct
import sys
import tempfile

sys.path[:0] = [".", "tools"]
from emu.machine import Machine  # noqa: E402
import stick_pack as SP  # noqa: E402

OUT = "build/usbloader"
POOL, STOCK_OPEN = 0x08DE39A4, 0x08DDF53C
ST = ["IDLE", "NOFILE", "BADDEV", "HEADER", "DONE"]
PACK = os.environ.get("PACK", "/tmp/PACK.BIN")
LOG = os.environ.get("LOG", "/tmp/stick_wlog.bin")


def syms():
    s = {}
    for line in open(os.path.join(OUT, "ld.syms")):
        a, _, n = line.split()
        s[n.lstrip("_")] = int(a, 16)
    return s


def runtime_words():
    """Bytes the DECK itself writes after a replay (phase-meter frame
    counter, the key-shift one-shot's request flag and its status word).
    They differ between any two runs, so an exact-memory check has to skip
    them -- the 'good' case always did; the other cases did not."""
    out = set()
    for name, path in (("_pm_draws", "build/phasemeter/pm.syms"),
                       ("_ksh_flag", "build/keyshift/ksh.syms"),
                       ("_ksh_status", "build/keyshift/ksh.syms")):
        try:
            for line in open(path):
                x, _, nm = line.split()
                if nm == name:
                    out |= set(range(int(x, 16), int(x, 16) + 4))
        except OSError:
            pass
    return out


def expected():
    """final byte per address, and the run index that last wrote it"""
    fin = {}
    for r in SP.runs(SP.read_log(LOG)):
        if r[0] == "call":
            continue
        a, _o, n = r
        for k, v in enumerate(n):
            fin[a + k] = v
    return fin


def entries(pack):
    n = struct.unpack_from("<H", pack, 10)[0]
    return [struct.unpack_from("<IIIIHHI", pack, 0x10 + i * SP.ESZ) for i in range(n)]


def run(case):
    from emu.usbdisk import open_disk
    from emu.usbhost import MassStorage
    from emu import panl
    good = open(PACK, "rb").read()
    ents = entries(good)
    data = bytearray(good)
    bad_at = None
    if case == "badbody":
        bad_at = len(ents) // 2
        while ents[bad_at][1] <= 64:                  # a big (in-place) run
            bad_at += 1
        pos = 0x10 + len(ents) * SP.ESZ
        for i in range(bad_at):
            pos += (-pos) % 0x10
            pos += ents[i][6]
        pos += (-pos) % 0x10
        data[pos + ents[bad_at][6] // 2] ^= 0x5A
    if case == "truncated":                          # a half-copied stick
        cut = len(data) // 2
        del data[cut:]
        pos, whole = 0x10 + len(ents) * SP.ESZ, 0
        for e in ents:
            pos += (-pos) % 0x10
            pos += e[6]
            if pos > cut:
                break
            whole += 1
        bad_at = whole                                # first record cut off
    s = syms()
    m = Machine.load(os.environ.get("CKPT", "cache/emu_dsp_mounted6.pkl"))
    mem, dev = m.mem, m.mem.dev
    base, st = s["ld_head"], s["ld_state"]
    assert mem.r32(POOL) == STOCK_OPEN, f"pool not stock: {mem.r32(POOL):#x}"
    assert not any(mem.r8(base + i) for i in range(0, 0x10000, 4)), "loader cave not empty"
    for i, b in enumerate(open(os.path.join(OUT, "ld.bin"), "rb").read()):
        mem.w8(base + i, b)
    if case == "oldbyte":                              # a checked stock word
        tgt = next(e for e in ents if e[4] == SP.F_OLD and e[1] == 4)
        mem.w32(tgt[0], mem.r32(tgt[0]) ^ 0x10)
    pre = {}
    if case == "truncated":                            # memory before the mount
        for e in ents:
            if not e[4] & SP.F_CALL:
                for k in range(e[1]):
                    pre[e[0] + k] = mem.r8(e[0] + k)
    mem.w32(POOL, s["ld_open"])                       # arm LAST
    stick = tempfile.mkdtemp(prefix="pack2_")
    os.makedirs(os.path.join(stick, "PIONEER"))
    open(os.path.join(stick, "PIONEER", "MYSETTING.DAT"), "wb").write(b"\0" * 160)
    os.makedirs(os.path.join(stick, "XDJMODS"))
    open(os.path.join(stick, "XDJMODS", "PACK.BIN"), "wb").write(bytes(data))
    u = dev._usb()
    u.unplug()
    m.cpu.run(limit=20_000_000)
    disk = open_disk(stick)
    dev.usb_disk = disk
    u.stick = MassStorage(disk)
    for _ in range(8):
        m.cpu.run(limit=250_000_000)
    names = ["magic", "calls", "status", "tries", "mask", "bad", "why", "bytes", "done"]
    v = dict(zip(names, [mem.r32(st + 4 * i) for i in range(len(names))]))
    print(f"[{case}] status={ST[v['status']] if v['status'] < 5 else v['status']} "
          f"applied={v['mask']}/{len(ents)} bad={v['bad']} why={v['why']} "
          f"tries={v['tries']} bytes={v['bytes']}")
    fin = expected()
    ok = True
    if case == "good":
        wrong = [a for a, b in fin.items() if mem.r8(a) != b]
        # words the deck itself changes after the replay: the phase meter's
        # frame counter, and the audio-chip one-shot's request flag (used up)
        # + its status word (1 = installed)
        def rt(name, path, n=4):
            for line in open(path):
                x, _, nm = line.split()
                if nm == name:
                    return set(range(int(x, 16), int(x, 16) + n))
            return set()
        runtime = (rt("_pm_draws", "build/phasemeter/pm.syms")
                   | rt("_ksh_flag", "build/keyshift/ksh.syms")
                   | rt("_ksh_status", "build/keyshift/ksh.syms"))
        if any(a in runtime for a in wrong):
            print(f"[good] runtime words changed by the deck (fine): "
                  f"{sorted(hex(a) for a in wrong if a in runtime)}")
        wrong = [a for a in wrong if a not in runtime]
        if os.environ.get("AFTER"):     # the cable install's own end state
            ref = Machine.load(os.environ["AFTER"]).mem
            wrong = [a for a in fin if mem.r8(a) != ref.r8(a)]
            names = {}
            for f in ("build/phasemeter/pm.syms", "build/keyshift/ksh.syms",
                      "build/hotcue8/hc.syms", "build/stems/sm.syms"):
                for line in open(f):
                    x, _, n = line.split()
                    names[int(x, 16)] = n
            near = lambda a: max((k for k in names if k <= a), default=None)
            print("[good] vs cable install, differing:",
                  sorted({names.get(near(a), "?") for a in wrong})[:12])
            # runtime counters, different in any two runs
            wrong = [a for a in wrong if names.get(near(a)) not in ("_pm_draws",)]
        print(f"[good] bytes checked {len(fin)}, wrong {len(wrong)}"
              + (f" first {wrong[0]:#x}" if wrong else ""))
        ok &= v["status"] == 4 and v["mask"] == len(ents) and not wrong
    elif case == "badbody":
        # every byte the entries BEFORE the corruption write must be there;
        # compare with those bodies decoded in order (not the log's final
        # value, which a later record may overwrite).
        import lzss_boot as LZ
        body, bpos = {}, 0x10 + len(ents) * SP.ESZ
        for i, e in enumerate(ents):
            if e[4] & SP.F_CALL:
                continue
            bpos += (-bpos) % 0x10
            body[i] = bytes(LZ.decode(good[bpos:bpos + e[6]]))[:e[1]]
            bpos += e[6]
        exp_pre = {}
        for i in range(bad_at):
            if i in body:
                for k, b in enumerate(body[i]):
                    exp_pre[ents[i][0] + k] = b
        rt = runtime_words()
        bad = [a for a, b in exp_pre.items() if mem.r8(a) != b and a not in rt]
        before = not bad
        if bad:
            print(f"[badbody] {len(bad)} bytes before the corruption differ, "
                  f"first {bad[0]:#x}")
        after = all(not any(mem.r8(e[0] + k) for k in range(min(e[1], 64)))
                    for e in ents[bad_at + 1:] if e[4] == SP.F_ZERO)
        print(f"[badbody] corrupted entry {bad_at}: before applied {before}, "
              f"later caves untouched {after}")
        ok &= v["mask"] == bad_at and v["bad"] == bad_at + 1 and before and after
    elif case == "truncated":
        # nothing past the cut is ever applied; either the loader refuses the
        # whole short file (mask 0) or it stops at the first cut record
        # each record's content, decoded from its true place in the full file
        import lzss_boot as LZ
        n_ok = v["mask"]
        body, pos = {}, 0x10 + len(ents) * SP.ESZ
        for i, e in enumerate(ents):
            if e[4] & SP.F_CALL:
                continue
            pos += (-pos) % 0x10
            body[i] = bytes(LZ.decode(good[pos:pos + e[6]]))[:e[1]]
            pos += e[6]
        exp = dict(pre)
        for i in range(n_ok):                       # the applied ones, in order
            if i in body:
                for k, b in enumerate(body[i]):
                    exp[ents[i][0] + k] = b
        # the first cut-off record is read straight into place before its CRC
        # fails: allowed ONLY if it replaces all-zero memory (an unused cave
        # that nothing points at -- the loader stops before any later record
        # could arm it; a power-cycle clears it)
        cut_rec = ents[n_ok] if n_ok < len(ents) else None
        cut_ok = True
        if cut_rec is not None and not cut_rec[4] & SP.F_CALL:
            rng = range(cut_rec[0], cut_rec[0] + cut_rec[1])
            cut_ok = not any(pre.get(a, 0) for a in rng)
            for a in rng:
                exp.pop(a, None)
        print(f"[truncated] cut-off record {n_ok} replaced only empty memory: {cut_ok}")
        rt = runtime_words()
        diff = [a for a in exp if mem.r8(a) != exp[a] and a not in rt]
        ok &= cut_ok
        recs = {}
        for d in diff:
            i = next(i for i, e in enumerate(ents) if e[0] <= d < e[0] + e[1])
            recs.setdefault(i, []).append(d)
        for i in sorted(recs)[:6]:
            e = ents[i]
            print(f"   rec {i} @{e[0]:#x} len {e[1]} flags {e[4]}: {len(recs[i])} "
                  f"bytes differ (mem {mem.r8(recs[i][0]):#x} want {exp[recs[i][0]]:#x})")
        print(f"[truncated] {bad_at} of {len(ents)} records fit; applied {n_ok}; "
              f"memory == before + exactly those records: {not diff}")
        ok &= n_ok in (0, bad_at) and not diff
    else:
        zero = all(not any(mem.r8(e[0] + k) for k in range(min(e[1], 64)))
                   for e in ents if e[4] == SP.F_ZERO)
        print(f"[oldbyte] every cave still empty: {zero}")
        ok &= v["mask"] == 0 and v["status"] == 3 and zero
    # the deck goes on: mount finished, Browse opens
    fb0 = bytes(mem.fb[0][::97]) + bytes(mem.fb[1][::97])
    dev.panel_frame = panl.build_frame(keys=["Browse"])
    m.cpu.run(limit=15_000_000)
    dev.panel_frame = panl.build_frame(keys=[])
    m.cpu.run(limit=200_000_000)
    fb1 = bytes(mem.fb[0][::97]) + bytes(mem.fb[1][::97])
    de = getattr(dev, "dspemu", None)
    extra = ""
    if case == "good" and de is not None:
        cw = de.bus.read(0xC0008D64, 4)
        # only a pack that CARRIES the one-shot (recorded with live key
        # shift / TOUCH FX: an F_CALL entry) patches the DSP; one recorded
        # without it must leave the call word stock
        pk = open(PACK, "rb").read()
        npk = struct.unpack_from("<H", pk, 10)[0]
        calls = any(struct.unpack_from("<H", pk, 0x10 + i * SP.ESZ + 16)[0] & SP.F_CALL
                    for i in range(npk))
        want = 0x00ACEC10 if calls else 0x0027D290
        extra = (f" DSP call word {cw:#010x} (pack has the key-shift step: {calls}, "
                 f"want {want:#010x})")
        ok &= cw == want
    print(f"[{case}] screen changed after Browse: {fb0 != fb1}{extra}  "
          f"dsp err {getattr(de, 'error', None)}")
    ok &= fb0 != fb1
    shutil.rmtree(stick, ignore_errors=True)
    print(f"[{case}] {'PASS' if ok else 'FAIL'}")
    return ok


if __name__ == "__main__":
    cases = sys.argv[1:] or ["all"]
    if cases == ["all"]:
        cases = ["good", "badbody", "oldbyte", "truncated"]
    res = [run(c) for c in cases]
    print("RESULT:", "PASS" if all(res) else "FAIL")
    sys.exit(0 if all(res) else 1)
