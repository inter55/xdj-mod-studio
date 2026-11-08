# PyInstaller spec for the Studio engine (Studio server + XDJ-700 emulator).
# Built by tools/build_studio.sh; the Swift shell wraps the result.
import glob
import os
import shutil
import subprocess
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

# --- bundled libhidapi (deck USB access) ------------------------------------
# The `hid` package dlopens the native hidapi library at runtime, which
# PyInstaller cannot see; without an explicit copy the frozen app never
# finds the deck. vendor/hid (found first via pathex) loads this copy.
def _bundle_hidapi():
    srcs = [p for pat in ("/opt/homebrew/opt/hidapi/lib/libhidapi.*.dylib",
                          "/usr/local/opt/hidapi/lib/libhidapi.*.dylib")
            for p in glob.glob(pat)]
    if not srcs:
        return None                      # runtime Homebrew fallback then
    real = os.path.realpath(srcs[0])
    out = os.path.join("build", "hidapi_bundle")
    os.makedirs(out, exist_ok=True)
    dst = os.path.join(out, "libhidapi.dylib")
    if not (os.path.exists(dst) and os.path.getmtime(dst) > os.path.getmtime(real)):
        shutil.copy(real, dst)
        for old in {os.path.join(os.path.dirname(real), "libhidapi.0.dylib"),
                    real}:
            subprocess.run(["install_name_tool", "-change", old,
                            "@loader_path/libhidapi.dylib", dst], check=False)
        subprocess.run(["install_name_tool", "-id",
                        "@loader_path/libhidapi.dylib", dst], check=False)
        subprocess.run(["codesign", "-f", "-s", "-", dst], check=False)
    return dst

WIN = sys.platform == "win32"
# Windows: tools/build_studio_win.py puts the native parts (built in CI, see
# .github/workflows/windows.yml) in build/win-natives/ before calling us
NATIVES = os.environ.get("XDJ_WIN_NATIVES", os.path.join("build", "win-natives"))
hidapi_dylib = None if WIN else _bundle_hidapi()

datas, binaries, hidden = [], [], []
# sqlcipher3: OneLibrary (exportLibrary.db) for Make stems / filter / convert
for pkg in ("pygame", "numpy", "PIL", "sqlcipher3"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hidden += h
if hidapi_dylib:
    binaries += [(hidapi_dylib, ".")]
if WIN:
    binaries += [(os.path.join(NATIVES, "hidapi.dll"), ".")]
# `hid` itself comes from vendor/ (patched loader); nothing to collect.

hidden += collect_submodules("emu") + collect_submodules("studio")
# stdlib modules the project tools (run from the project folder) import
hidden += ["wave", "colorsys", "msvcrt" if WIN else "fcntl", "zlib", "ctypes", "pickle", "array",
           "base64", "hashlib", "csv", "struct", "tempfile", "glob", "argparse",
           "runpy", "select", "socket"]

datas += [("studio/ui.html", "studio"),
          ("emu/icon_1024.png", "emu")]
if os.environ.get("PUBLIC") != "1":   # private builds only: firmware screens
    datas += [("studio/assets", "studio/assets"),   # + brand logos
              ("emu/assets", "emu/assets")]
if WIN:
    binaries += [(os.path.join(NATIVES, "_fastcore.dll"), "emu")]
else:
    binaries += [("emu/_fastcore.so", "emu")]      # compiled by build_studio.sh
# Real-DSP snapshot support in the frozen app: cfast.py reads the OP list
# from c6xcore.c at import, and dlopens the prebuilt lib (tester Macs have
# no compiler). Both must sit next to the frozen cfast module.
datas += [("emu/c6x/c6xcore.c", "emu/c6x")]
binaries += [(os.path.join(NATIVES, "_c6xcore.dll"), "emu/c6x") if WIN
             else ("emu/c6x/_c6xcore.so", "emu/c6x")]

a = Analysis(["studio/app.py"], pathex=["vendor", "."], binaries=binaries,
             datas=datas, hiddenimports=hidden, hookspath=[], runtime_hooks=[],
             excludes=["tkinter", "matplotlib", "scipy", "IPython"],
             cipher=block_cipher, noarchive=False)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="XDJ Mod Studio",
          debug=False, strip=False, upx=False,
          console=not WIN,          # Windows: no black console window
          icon=(os.path.join(NATIVES, "icon.ico") if WIN and
                os.path.exists(os.path.join(NATIVES, "icon.ico")) else None))
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, strip=False, upx=False,
               name="XDJ Mod Studio")
