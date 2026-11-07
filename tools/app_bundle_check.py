#!/usr/bin/env python3
"""app_bundle_check.py APP.app | OUT.zip -- refuse a build that would ship
without files the app loads at runtime.

Why this exists: 1.0.1 shipped with the in-place stems replacement wired up
(button sends replace, code present) but **no `vendor/`**, so
`stems_replace.apply` died on `ModuleNotFoundError: rekordbox_pdb` for
MP3/M4A/FLAC. The same package is needed by filter_playlists (Make the whole
set) and onelibrary_to_pdb. The zip gate checked sidecars and signatures, not
whether the app could actually do its job.

Checks run against Contents/Resources/repo (the app's own copy of the project)
and accept either source (.py) or the shipped sourceless form (.pyc).
"""
import os
import subprocess
import sys
import tempfile

# module path under repo/, extension supplied below
REQUIRED_MODULES = [
    "studio/app",
    "studio/emuhost",
    "tools/stems_replace",
    "tools/stems4",
    "tools/filter_playlists",
    "tools/onelibrary_to_pdb",
    "vendor/rekordbox_pdb/__init__",
    "vendor/rekordbox_pdb/pdb",
    "vendor/rekordbox_pdb/edit",
]
# our own compiled deck modules: .bin + .syms pairs the installers read
REQUIRED_DECK_MODULES = [
    "centre/cw", "hotcue8/hc", "stems/sm", "keyshift/ksh", "keyshift/ks",
    "utility/ut", "themesw/ts", "xflac/vfile", "xflac_flac/vfile",
    "lighttext/lt", "touchfx/tfx", "fx/fx", "fx/ksh", "usbloader/ld",
]


def find_app(path):
    if path.endswith(".zip"):
        tmp = tempfile.mkdtemp(prefix="bundlecheck")
        subprocess.run(["unzip", "-q", path, "-d", tmp], check=True)
        for root, dirs, files in os.walk(tmp):
            for d in dirs:
                if d.endswith(".app"):
                    return os.path.join(root, d)
        raise SystemExit("FAIL: no .app in %s" % path)
    if not os.path.isdir(path):
        raise SystemExit("FAIL: no app at %s" % path)
    return path


def has(base, stem):
    for ext in (".pyc", ".py"):
        if os.path.exists(os.path.join(base, stem + ext)):
            return True
    return False


def find_repo(app):
    """Mac: Contents/Resources/repo. Windows: repo/ next to the exe."""
    for rel in (os.path.join("Contents", "Resources", "repo"), "repo"):
        p = os.path.join(app, rel)
        if os.path.isdir(p):
            return p
    return None


def main(argv):
    if len(argv) != 2:
        print("usage: app_bundle_check.py APP.app | OUT.zip", file=sys.stderr)
        return 2
    app = find_app(argv[1])
    repo = find_repo(app)
    if repo is None:
        print("FAIL: no repo/ (Contents/Resources/repo or repo/) in", app)
        return 1
    miss = [m for m in REQUIRED_MODULES if not has(repo, m)]
    for m in REQUIRED_DECK_MODULES:
        for ext in (".bin", ".syms"):
            if not os.path.exists(os.path.join(repo, "build", m + ext)):
                miss.append("build/" + m + ext)
    if not os.path.exists(os.path.join(repo, "studio", "ui.html")):
        miss.append("studio/ui.html")
    if miss:
        print("FAIL: the app is missing files it loads at runtime:")
        for m in miss:
            print("   ", m)
        print("  (vendor/rekordbox_pdb is the 1.0.1 regression: stems replacement,"
              " filter playlists and OneLibrary conversion all import it)")
        return 1
    print("ok app bundle: %d modules, %d deck modules present"
          % (len(REQUIRED_MODULES), len(REQUIRED_DECK_MODULES)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
