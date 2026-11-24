#!/usr/bin/env python3
"""linkrgb_apply.py [--check|--status|--log-only|--revert] -- colour
waveforms on the deck that loads over Pro DJ Link (tools/linkrgb.py), over
the USB cable. RAM only: a power-cycle is stock again.

Run it on the deck WITHOUT the stick, after the colour waveform mod is
installed on that deck (cable install, or loader + stick pack): linkrgb
only feeds the RGB modules' slots, it does not draw anything itself.

  --check     read-only: hook words, overview cave, module area. Writes
              nothing.
  --status    read-only: counters, last analysis path, why a fetch failed,
              and the last 4 files the deck opened over Link. Use it after
              loading a track from the other deck.
  --log-only  install, but only RECORD what the deck opens over Link (no
              analysis file is read). The first deck session should start
              here: it proves the audio path / drive letter on hardware
              with zero extra network traffic.
  (none)      install with fetching on.
  --revert    hook words back to stock (open hook first); code stays in
              RAM, inert.

Refuses when a hook word is neither stock nor ours, when the overview
cave is missing (nothing would draw the colours), or when the module area
holds foreign data. Every write is read back; the hooks are armed last,
one 32-bit write each: snapshot word, then the media-server read, then the
NFS open; the enable word goes in after all three.
"""
import struct
import sys

sys.path[:0] = [".", "tools"]
import linkrgb as L
import rgb_overview as RO
import rgb_detail as RD


def image():
    """The bytes of [LBASE, DATA_END): code, then data with EN = 0."""
    code, ent = L.build()
    img = bytearray(L.DATA_END - L.LBASE)
    img[:len(code)] = code
    for a, b in L.data_init():
        img[a - L.LBASE:a - L.LBASE + len(b)] = b
    return bytes(img), code, ent


def decode_status(v, ring, extp, audio):
    def g(k):
        return struct.unpack_from("<I", v, L.V[k] - L.LV)[0]
    en = g("EN")
    print(f"enable {en} ({ {0: 'off', 1: 'log only', 3: 'log + fetch'}.get(en, '?') })")
    print(f"NFS opens seen {g('NOPEN')}, audio {g('NAUD')}, Link loads {g('GEN')}, "
          f"pending {g('PEND')}")
    print(f"  first opens (fetch waits for the deck's re-open) {g('NFIRST')}, "
          f"confirmed loads {g('NCONF')}, re-opens of the current track {g('NDUP')}, "
          f"neighbour pre-opens ignored {g('NPRE')}")
    print(f"fetches ok {g('NOK')}, failed {g('NFAIL')} (last reason: "
          f"{L.WHY.get(g('WHY'), g('WHY'))})")
    s = g("SECT")
    print(f"last fetch: gen {g('DONE')}, N {g('N'):#x}, sections "
          f"{'+'.join(t for b, t in ((1, 'PWV4'), (2, 'PWV5'), (4, 'PWV6')) if s & b) or '-'}, "
          f"{g('MS')} ms")
    if len(audio) > 3 and audio[1] == ":":
        print(f"last Link track: {audio}")
        print(f"  its analysis file: {L.ext_path(audio[0], audio[2:])} "
              f"(check it exists on the other deck's stick)")
    print(f"analysis path last opened: {extp}")
    print(f"new-track decisions: Link {g('NLINK')}, local {g('NLOCAL')} (bound gen {g('BOUND')})")
    sites = [struct.unpack_from("<I", v, L.V["SITE"] - L.LV + 4 * i)[0]
             for i in range(len(L.READ_HOOKS))]
    print("NFS reads per call site: " + ", ".join(
        f"{what} {n}" for (_, what, _), n in zip(L.READ_HOOKS, sites)))
    who = g("WHO")
    print("last fetch triggered by: " + (L.READ_HOOKS[who - 1][1]
          if 1 <= who <= len(L.READ_HOOKS) else "-"))
    tl = struct.unpack_from("<I", v, L.V["OKLEN"] - L.LV)[0]
    print(f"colours verified for a track of {tl / 150:.1f} s" if tl not in (0, 0xFFFFFFFF)
          else "colours not verified yet for the loaded track")
    print(f"  copied from the deck's buffer after a check {g('NFIX')}, "
          f"blanked (buffer held another track) {g('NBLANK')}")
    n = g("LOGI")
    print(f"last NFS opens ({min(n, L.LOG_N)} of {n}):")
    for k in range(min(n, L.LOG_N)):
        i = (n - 1 - k) % L.LOG_N
        hdr, path = ring[i]
        print(f"  {'audio ' if hdr >> 31 else 'other '} t={hdr & 0xFFFFFF:>8} ms  {path}")


def wstr(b):
    s = b.decode("utf-16-le", "replace")
    return s.split("\0", 1)[0]


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg not in ("", "--check", "--status", "--log-only", "--revert"):
        sys.exit(__doc__)
    want, code, ent = image()
    snapw = L.overview_pool_word()
    hooks = ((snapw, RO.COPY5, ent["snap"], "snapshot (overview cave)"),
             (L.CENTRE_POOL, RD.DCAVE, ent["lcol"], "centre painter (colours)"),
             ) + tuple(
        (pool, L.ORIG_READ, ent[f"lrs{i}"], f"NFS read: {what}")
        for i, (pool, what, _) in enumerate(L.READ_HOOKS)) + (
        (L.POOL_OPEN, L.ORIG_OPEN, ent["lopen"], "NFS open"),)
    print(f"linkrgb {len(code)} B at {L.LBASE:#x}, area to {L.DATA_END:#x}")

    # xdj700_usb_shell FIRST: it puts the bundled vendor/hid (hid.Device) on
    # sys.path; pip's "hidapi" is a different module with the same name.
    from xdj700_usb_shell import PIONEER_VID, open_deck, drain
    import hid
    import fastload
    from rgb_overview_apply import br, send

    def rd32(h, a):
        return struct.unpack("<I", bytes(br(h, a, 4)))[0]

    def lw(h, addr, val):
        q = f"{addr:x},{val:x}LW".encode() + b"\r\x00"
        h.write(q + b"\x00" * (26 - len(q)))
        drain(h, 0.5)

    def setw(h, addr, val, what):
        lw(h, addr, val)
        got = rd32(h, addr)
        if got != val:
            return f"{what}: wrote {val:#x}, reads {got:#x}"
        return None

    c = [d for d in hid.enumerate() if d["vendor_id"] == PIONEER_VID and d["usage"] == 1]
    if len(c) != 1:
        sys.exit(f"found {len(c)} shell interface(s) - plug in exactly one deck "
                 f"(the one WITHOUT the stick)")
    h = open_deck(PIONEER_VID, c[0]["product_id"], 1)
    try:
        drain(h, 1.0)
        send(h, "$CDJ", 1.5)
        def in_area(v):             # any linkrgb build (entry points move)
            return L.LBASE <= v < L.LBASE + L.CODE_MAX

        state = {}
        for a, stock, ours, what in hooks:
            cur = rd32(h, a)
            state[a] = cur
            tag = ("STOCK" if cur == stock else "OURS" if cur == ours else
                   "OURS (older build)" if in_area(cur) else "OTHER")
            print(f"  {what:26s} {a:#x} = {cur:#x} ({tag})")
        en = rd32(h, L.V["EN"])

        if arg == "--status":
            v = fastload.BR32(h, L.LV, 0x100)
            ring = []
            for i in range(L.LOG_N):
                e = fastload.BR32(h, L.V_LOG + i * L.LOG_SZ, L.LOG_SZ)
                ring.append((struct.unpack_from("<I", e)[0], wstr(e[4:])))
            extp = wstr(fastload.BR32(h, L.V_EXT, 0x60))
            audio = wstr(fastload.BR32(h, L.V_PATH, 2 * (L.PATH_MAX + 1)))
            decode_status(v, ring, extp, audio)
            return 0

        if arg == "--revert":
            errs = [setw(h, L.V["EN"], 0, "enable")] if en else []
            for a, stock, ours, what in reversed(hooks):     # open hook first
                if in_area(state[a]):
                    errs.append(setw(h, a, stock, what))
            errs = [e for e in errs if e]
            if errs:
                sys.exit("FAIL: " + "; ".join(errs) + " - POWER-CYCLE the deck")
            print("RESULT: reverted (stock hooks; module left inert in RAM)")
            return 0

        live = fastload.BR32(h, L.LBASE, len(want))
        code_ours = live[:len(code)] == code
        foreign = [i for i, b in enumerate(live[:len(code)]) if b and not code_ours]
        if arg == "--check":
            print(f"  module area: " + ("our code" if code_ours else
                  "all zero (usable)" if not any(live) else
                  f"FOREIGN data at {L.LBASE + foreign[0]:#x}" if foreign else
                  "code zero, data non-zero"))
            print(f"  enable {en}")
            return 0

        bad = [w for a, s_, o, w in hooks if state[a] != s_ and not in_area(state[a])]
        if (state[snapw] != RO.COPY5 and not in_area(state[snapw])) or (
                state[L.CENTRE_POOL] != RD.DCAVE and not in_area(state[L.CENTRE_POOL])):
            sys.exit(f"REFUSED: the colour waveform mod is not on this deck "
                     f"(snapshot word {state[snapw]:#x}, centre painter "
                     f"{state[L.CENTRE_POOL]:#x}) - install it first")
        if bad:
            sys.exit(f"REFUSED: {', '.join(bad)} hooked by something else - nothing written")
        if not code_ours:
            pion = "PIONEER".encode("utf-16-le")
            o = L.V["PION"] - L.LBASE
            older = live[o:o + len(pion)] == pion
            if any(live) and not older:
                sys.exit("REFUSED: the module area is not empty - nothing written "
                         "(power-cycle the deck, then install)")
            if older:               # an earlier linkrgb build: unhook, wipe, rewrite
                print("  an older linkrgb build is in RAM: unhooking and replacing it "
                      "(do not load a track for the next minute)")
                errs = [setw(h, L.V["EN"], 0, "enable")]
                for a, stock, ours, what in reversed(hooks):
                    if in_area(state[a]):
                        errs.append(setw(h, a, stock, what))
                        state[a] = stock
                errs = [e for e in errs if e]
                if errs:
                    sys.exit("FAIL: " + "; ".join(errs) + " - POWER-CYCLE the deck")
                fastload.fastblock(h, L.LBASE, bytes(len(want)))
                en = 0
            left = fastload.install_region(h, L.LBASE, want, fastload.SESSION_BUDGET)
            if left:
                print(f"RESULT: PARTIAL ({left} B left) - run again")
                return 0
            if fastload.BR32(h, L.LBASE, len(want)) != want:
                sys.exit("FAIL: module area verify - nothing armed")
        err = setw(h, L.V["EN"], 0, "enable") if en else None
        armed = []
        for a, stock, ours, what in hooks:
            if state[a] == ours:
                continue
            err = err or setw(h, a, ours, what)
            if err:
                break
            armed.append((a, stock, what))
        if err:
            for a, stock, what in reversed(armed):
                setw(h, a, stock, what)
            sys.exit(f"FAIL: {err} - hooks put back; power-cycle before playing")
        mode = 1 if arg == "--log-only" else 3
        err = setw(h, L.V["EN"], mode, "enable")
        if err:
            sys.exit(f"FAIL: {err}")
        print(f"RESULT: PASS - armed ({'log only' if mode == 1 else 'log + fetch'}). "
              f"Load a track from the OTHER deck, then run --status.")
        return 0
    finally:
        h.close()


if __name__ == "__main__":
    sys.exit(main())
