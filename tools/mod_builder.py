#!/usr/bin/env python3
"""mod_builder.py v0 -- theme colors -> mods/themes.py entry -> preview -> .UPD.

  .venv/bin/python tools/mod_builder.py --name NAME --hue DEG [--sat F]
      [--val F] [--spread F] [--version VER] [--export] [--force]

Steps: validate spec -> append THEMES entry (refuses overwrite unless
--force) -> preview PNG (screenshots/theme_NAME.png, via modapp's screen
renderer) -> optionally export build/XDJ700_NAME.UPD through the same
verified pipeline as the Mod Studio (decode, recolour, recompress,
checksums, safety gate). Nothing is flashed; the .UPD is a file.
"""
import argparse
import base64
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "mods"))

STOCK = os.path.join(ROOT, "FIRMWARE", "XDJ700_stock_v1.15.UPD")


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True,
                    help="theme name (lowercase-letters-and-dashes)")
    ap.add_argument("--hue", type=float, required=True,
                    help="target hue in degrees (0-360)")
    ap.add_argument("--sat", type=float, default=1.30)
    ap.add_argument("--val", type=float, default=1.00)
    ap.add_argument("--spread", type=float, default=0.50)
    ap.add_argument("--vivid", type=float, default=0.0,
                    help="0-1: push the slate UI toward full colour")
    ap.add_argument("--lift", type=float, default=0.0,
                    help="0-1: brighten dark fills (glow)")
    ap.add_argument("--lit", default="",
                    help="#rrggbb: lit touch buttons in this colour "
                         "(mods/litbuttons.py; on-deck theme switching)")
    ap.add_argument("--version", default="",
                    help="MAINVer stamp for the .UPD (default: unchanged)")
    ap.add_argument("--export", action="store_true",
                    help="also build build/XDJ700_<name>.UPD")
    ap.add_argument("--upd", default=None,
                    help="stock firmware .UPD (default: "
                         "FIRMWARE/XDJ700_stock_v1.15.UPD)")
    ap.add_argument("--force", action="store_true",
                    help="replace an existing THEMES entry")
    return ap.parse_args()


def main():
    a = parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", a.name):
        sys.exit("bad --name (use lowercase letters, digits, dashes)")
    if not (0.0 <= a.hue <= 360.0):
        sys.exit("bad --hue (0-360)")
    for k, v in (("sat", a.sat), ("val", a.val)):
        if not (0.05 <= v <= 3.0):
            sys.exit(f"bad --{k} (0.05-3.0)")
    if not (0.0 <= a.spread <= 1.0):
        sys.exit("bad --spread (0-1)")

    import themes as T
    spec = dict(hue=a.hue, sat=a.sat, val=a.val, spread=a.spread)
    for k in ("vivid", "lift"):
        v = getattr(a, k)
        if not (0.0 <= v <= 1.0):
            sys.exit(f"bad --{k} (0-1)")
        if v:
            spec[k] = v
    if a.lit:
        if not re.fullmatch(r"#?[0-9a-fA-F]{6}", a.lit):
            sys.exit("bad --lit (#rrggbb)")
        spec["lit"] = "#" + a.lit.lstrip("#").lower()
    if a.name in T.THEMES and not a.force:
        sys.exit(f"'{a.name}' already in mods/themes.py (use --force to replace)")
    print(f"spec: {spec}")

    # 1. themes.py entry (textual append inside the THEMES dict)
    tpath = os.path.join(ROOT, "mods", "themes.py")
    src = open(tpath).read()
    start = src.index("THEMES = {")
    end = src.index("\n}", start)
    body = src[start:end]
    extra = "".join(f", {k}={spec[k]!r}" for k in ("vivid", "lift", "lit") if k in spec)
    line = f"    '{a.name}': dict(hue={a.hue}, sat={a.sat}, val={a.val}, spread={a.spread}{extra}),"
    if re.search(rf"^    '{re.escape(a.name)}':", body, re.M):
        body = re.sub(rf"^    '{re.escape(a.name)}':.*$",
                      "    " + line.strip(), body, flags=re.M)
        print("replaced existing entry")
    else:
        body += "\n" + line
        print("appended new entry")
    open(tpath, "w").write(src[:start] + body + src[end:])
    import importlib
    importlib.reload(T)
    assert T.THEMES.get(a.name) == spec, "entry did not round-trip"
    print(f"mods/themes.py: '{a.name}' registered "
          f"(emu Theme menu picks it up on next launch)")

    # 2. preview PNG through the Mod Studio screen renderer
    try:
        import modapp
        b64 = modapp.screen_preview(spec)
        if b64:
            raw = base64.b64decode(b64.split(",", 1)[1])
            out = os.path.join(ROOT, "screenshots", f"theme_{a.name}.png")
            open(out, "wb").write(raw)
            print(f"preview: {out}")
        else:
            print("preview: skipped (no sim reference image)")
    except Exception as e:
        print(f"preview: FAILED ({e})")

    # 3. optional .UPD export (verified pipeline + safety gate)
    if a.export:
        import modapp
        stock = a.upd or STOCK
        if not os.path.isfile(stock):
            from emu import fw          # app/public checkout: the user's
            stock = fw.find_upd() or stock      # own firmware home
        if not stock or not os.path.isfile(stock):
            sys.exit(f"stock firmware not found: {STOCK} (pass --upd XDJ700.UPD)")
        modapp.STOCK = stock
        job = {"log": []}
        outpath = os.path.join(ROOT, "build", f"XDJ700_{a.name}.UPD")
        modapp.do_build(job, spec, a.name, a.version, outpath)
        print(f"export: {job.get('state')} -> {job.get('out', '(none)')}")
        if job.get("state") != "done":
            sys.exit(1)
    print("OK")


if __name__ == "__main__":
    main()
