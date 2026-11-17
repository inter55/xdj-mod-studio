import os, sys, struct
os.environ["XDJ_HLE_DSP"] = "1"
sys.path[:0] = [".", "tools"]
from emu.machine import Machine
from emu import panl

if not os.path.exists("/tmp/ram0.bin"):
    print("SKIP: /tmp/ram0.bin missing -- a RAM dump taken during a deck "
          "session (see CODEC.md); this check needs one")
    raise SystemExit(0)
ram = open("/tmp/ram0.bin","rb").read()
BASE = 0x08000000
SITES = [0x08D3664E, 0x08D365B0, 0x08D366B2, 0x08D366C0,
         0x08D36788, 0x08D36960, 0x08D3737C]
Q20 = lambda x: int(round(x * (1 << 20)))

m = Machine.load("cache/emu_track_loaded.pkl")
hle = m.mem.dev.hle
print("original bytes:", {hex(s): ram[s-BASE:s-BASE+2].hex() for s in SITES})
for s in SITES:                              # install the hook: nop stores
    assert m.mem.r8(s) == ram[s-BASE], f"{s:#x} drifted"
    m.mem.w8(s, 0x09); m.mem.w8(s+1, 0x00)

def run(t): m.cpu.run(limit=int(t * 500_000_000))
def frame(keys=(), fader=250):
    m.mem.dev.panel_frame = panl.build_frame(keys=keys, fader_raw=fader)
def wr32(a, v):
    for i, b in enumerate(v.to_bytes(4, "little")): m.mem.w8(a+i, b)
def w(a): return int.from_bytes(bytes(m.mem.r8(a+i) for i in range(4)), "little")

frame(); run(0.5)
frame(("PlayPause",)); run(0.15); frame(); run(0.8)
frame(("MasterTempo",)); run(0.15); frame(); run(0.8)

V = Q20(2.0 ** (2.0/12.0))
wr32(0x0B6C2C50, V)  # no dirty: with writers NOPed it only triggers the recompute
print(f"hook installed, wrote {V:#x} once (+2 semis); watching 10 s...")
pos0 = hle.pos_samples
ok_word = ok_echo = 0
for i in range(50):
    run(0.2)
    if w(0x0B6C2C50) == V: ok_word += 1
    if hle.echo.get(0x11837BC0) == V: ok_echo += 1
print(f"word==ours: {ok_word}/50   mailbox echo==ours: {ok_echo}/50")
print(f"poller copied to 3E9C: {w(0x0B6C3E9C):#x}   mode: {w(0x0B6C4264)}   "
      f"MT state: {w(0x0B6C4308)}   pos advanced: {hle.pos_samples - pos0}")
# change value live (what the PERFORM KEY pads would do)
V2 = Q20(2.0 ** (-1.0/12.0))
wr32(0x0B6C2C50, V2); run(2.0)
print(f"live change to -1 semi: word {w(0x0B6C2C50):#x} echo {hle.echo.get(0x11837BC0,0):#x} mode {w(0x0B6C4264)}")
good = ok_word == 50 and ok_echo >= 45 and w(0x0B6C4264) == 5
print("HOOK TEST", "PASS" if good else f"PARTIAL (word {ok_word}/50, echo {ok_echo}/50)")
