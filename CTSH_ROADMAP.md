# ctsh console → live theme modification: the concrete roadmap

Written 2026-09-05, in response to: find the console init, the UART instance and
pads, the auth logic, the dump/edit semantics, whether RAM at 0x0FFFD000 is
reachable, what the framebuffer capture gives us, and the safest first
experiment. No TCP 1051. No compression work unless the RAM route fails.

Method note: block1 is compressed and our two decoders are both wrong (they
diverge at output offset 0x74). Everything below marked **confirmed** is
therefore established by evidence that does not depend on the codec: raw-stream
literals whose provenance roots agree across both decodes, or strings present in
*both* decodes. Anything the codec cannot resolve is marked **open**, with the
step that resolves it on hardware.

---

## 1. Where the console lives — **confirmed**

The shell is `D:\CDJ\Build_XDJ700\Middle\c??e\shell\ctsh.c` (source path,
consensus in both decodes). Its data cluster sits late in block1, near the
peripheral-driver region (about a megabyte past the BEU display driver under
the OKU decode). The init *function* cannot be pinpointed statically at current
codec fidelity — but it does not need to be, because of §2.

## 2. UART instance — **confirmed: SCIF1.** Pins — partially open

The SH7724 has six serial channels: SCIF0/1/2 at `0xFFE00000/0xFFE10000/
0xFFE20000` and SCIFA3/4/5 at `0xA4A3/4/50000`. A console init must load its
channel base as a literal pool constant. Scanning every literal-verified word
in both decodes of both blocks (method validated: the same scan reproduces the
known BEU pool at `0xE7EDD0-0xE7EE20` exactly):

```
literal occurrences of a UART base, block1, raw-stream roots (codec-independent):

    0xFFE10000 (SCIF1)   exactly 2   at stream 0x511853 and 0x6776cf
    every other channel   exactly 0
```

Both decodes agree on the roots byte-for-byte, so the true image contains
**exactly two references to SCIF1 and zero to all other channels**. (Apparent
SCIF0/SCFTDR hits dissolved under the literal check — match-copy noise.)
The console runs on **SCIF1, base `0xFFE10000`**, two independent code
references (likely init + the read path).

The PCB pads are board-level, not firmware: which balls carry SCIF1's TXD1/RXD1
is in the SH7724 hardware manual's pin tables (docs/, too large to parse here —
look up "SCIF1" there for the ball numbers). Practical identification:

- 3.3 V LVTTL, 115200 8N1, no flow control (the console's own string says
  `115200 B8 PN S1`).
- Look for an unpopulated 3–4 pad header near the SoC — Pioneer boards of this
  era usually expose the factory console.
- Otherwise scope candidate pads at boot: console TX idles high at 3.3 V and
  bursts on power-up; RX sits next to it, often behind a series resistor.
- **Never** connect an RS-232 level converter — 12 V will kill the SoC.

## 3. Auth — mechanism **confirmed**, credential **open (by design of the plan)**

Consensus strings in both decodes:

```
"in: \0password"      login + password prompts (prefixes corrupted)
"permission denied"   right next to the source path, inside ctsh
"than 2  :s hav..."   "more than 2 args have..." — argument checking
"CTASSERT"            debug-build artifact — the shell is fully compiled in
```

So: login prompt → password prompt → per-attempt failure ("permission denied"),
with argument validation. No plaintext credential string is consensus-visible
adjacent to the prompts, and the compare code is inside the unreadable region.
A base64-style alphabet (`1234567890ABC…xyz`) sits nearby in the same module —
possible hint of an encoded secret, unproven.

This is fine, because the credential is not on the critical path: the compare
code lives in RAM at runtime, and `dump` reads RAM. Once the console talks
(even at the login prompt) the check becomes visible in the §7 plan. Expect it
to be a plain string compare or a small hash in ctsh.c — a 20-minute job once
we hold the RAM bytes around the prompt string. Do **not** brute-force on
hardware; there is no reason to.

## 4. dump/edit semantics — syntax **confirmed**, address space **high confidence**

The usage strings survive in both decodes:

```
"dump ... [-(b|w|l)] start [count]"    byte/word/long, start address, count
"edit ..."                             same option style (fragment "edit)] st"
task list header  "TNAME TPRI TSTS"    ps-style process display
memory map        "ADDRESS ... BASE ... CUR/SIZE"
log levels        "Level=0(OFF),1(ERR),2(CMD),3... PROC),4(...)"
```

So: `dump -b|-w|-l <start> [count]`, edit likewise with a value. In practice
the console prints its own usage on a bare `dump` — syntax resolves in one
command on the day.

Address space: the boot stub enables the cache (CCR `0xFF00001C`) and never
touches MMUCR, and everything we have traced (load address `0x08000000`,
updater workspace `0x0FFFD000`) behaves as identity-mapped physical. Expected
addressing:

| range | what |
| --- | --- |
| `0x08000000…` | the decompressed firmware, running (code + data) |
| `0x0FFFD000…` | RAM below the 256 MB line (updater's old workspace) |
| `0xFE930000…` | BEU display controller registers (P4, direct) |
| `0xA0000000+` | uncached alias of physical RAM — use for device-ish access |

Unknown: whether the OS later enables the MMU with a remap. §7 step 2 detects
that in one command and the console's own memory-map display gives the real map
if so.

## 5. Can dump reach the decompressor and 0x0FFFD000? — yes, with one correction

The premise needs a small fix: at runtime there is no decompressor output
buffer to find. Decompression happens once, at boot; the output **is** the
firmware — loaded at `0x08000000` and running from RAM. `dump` can read all of
it. That is better than the original hope: we don't get a stale buffer, we get
the live image.

- `0x0FFFD000` is inside the same RAM bank (the updater stack sat at physical
  `0x10000000`), so it is readable — but by runtime it holds whatever the OS
  put there, not updater state. Interesting for archaeology, not needed.
- The decompression problem resolves itself as a *byproduct*: dump the running
  image, diff against our two wrong decodes, and the true codec falls out.
  That is phase 3, after themes — exactly the ordering you asked for.

## 6. Framebuffer capture — **confirmed, and better than hoped**

Consensus: `c:\_cdj_DispCap565.raw` + `.raw` + `open` — it writes the screen as
RGB565 to a file on the USB device. That gives, in one command:

- **dimensions** — file size ÷ 2 = pixels; with the aspect ratio the panel
  geometry falls out (768,000 B would mean 800×480);
- **the framebuffer content** — exact RGB565 values for every UI element on a
  real screen;
- and combined with `dump`, the **framebuffer base**: either from the BEU layer
  source-address registers (`dump -l 0xFE930000 0x1000` — the base is confirmed
  `0xFE930000` from the earlier display-driver work), or by scanning RAM for
  bytes matching the captured file.

That answers the palette question with ground truth: recolour the capture,
find the pixel value you want, locate it in RAM, and the colour pipeline is in
your hands.

## 7. The safest first experiment — UART → theme, in 7 steps

**Phase 0 — cable (no deck risk).** 3.3 V USB-serial, GND + TX + RX, 115200
8N1. Find pads per §2. Photograph everything before probing.

**Phase 1 — reach the console (read-only).** Boot with the adapter attached.
Expect a boot banner, or press Enter for a `login:` prompt. No media in the
deck. If nothing: re-check TX/RX swap first, it is always TX/RX swap.

**Phase 2 — validate addressing (read-only, one command).** After login:

    dump -b 0x08000000 0x40

Expected bytes (we know them exactly — the boot stub):

    09 00 05 a0 09 00 09 00 09 00 09 00 09 00 09 00
    07 df 02 00 07 d2 29 20 0e 40 07 d0 07 d1 11 20
    07 d0 08 d1 12 20 08 d0 6a 40 08 d0 2b 40 09 00
    00 00 00 b0 0f ff ff df 04 00 52 a4 07 a5 00 00

Match → addressing confirmed, MMU is not in the way, and `dump` works. Mismatch
→ addresses are remapped; use the console's memory-map command and adapt. Either
way we learn everything important without writing a byte.

**Phase 3 — read-only mapping.**

    dump -l 0xFE930000 0x1000      # BEU registers -> framebuffer base
    <DispCap command>              # USB: _cdj_DispCap565.raw -> dimensions+pixels
    help / ?                       # full command list (usage strings confirmed)
    <ps> / <memory-map commands>   # task list, region map

Dump the framebuffer, then hunt the theme data: recolour something in the
capture, take the wanted RGB565 value, and `dump`-scan the candidate regions
(start with the flash-mirror territory where our decodes place the grey ramps;
the consensus bytes from this document are the landmarks). Also save a full
`0x08000000`+ image dump to USB over time — it fixes the codec for free.

**Phase 4 — first write (trivially reversible).** Edit **one 16-bit word in the
framebuffer** — a single pixel of the live display. It survives at most until
the next frame: the safest possible write, and it proves `edit` semantics.
Then edit a small block (one box outline) so it's visible on camera.

**Phase 5 — the theme experiment (reversible by reboot).** One colour-table
entry in RAM — the equivalent of the flash probe that cost three bricks, minus
the brick: RAM edits vanish on power cycle. Compare against the capture.
Reboot → stock. Repeat freely.

**Phase 6 — persistence (only after 5 works).** Map the live table to its flash
location using the RAM bytes as the Rosetta stone for the codec, patch the
flash image with our toolchain — which now computes the real block checksum —
and flash. The compression problem gets solved here, if and only if this phase
needs it.

**Abort criteria:** any behaviour you can't explain at the login prompt (wrong
baud ghosts, garbled echo) → stop, re-check wiring. Never `edit` before Phase 2
has validated addressing. Never edit outside the framebuffer before Phase 5.

## 8. Compression: deliberately untouched

Nothing above requires solving LZSS. The only compression work in the plan is
*enabled by* success (Phase 3's dump fixes the codec as a side effect), which
is the right way round: ground truth first, theory second.

---

### Regenerating the evidence

    cc -O2 -o /tmp/decode_prov tools/decode_prov.c
    /tmp/decode_prov work/blk01.lz /tmp/D_v1.bin /tmp/D_v1.prov v1
    /tmp/decode_prov work/blk01.lz /tmp/D_oku.bin /tmp/D_oku.prov oku

SCIF base scan: literal-verified (provenance kind==0 on all 4 bytes) u32
constants in the six channel ranges; raw-stream roots compared across decodes.
String evidence: fragments present in both decodes at independent offsets.
