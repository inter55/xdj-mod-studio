# Brief for the dev — 2026-09-05

Confirmed on hardware today. Two results, one of them the thing that has been
blocking this project since the start.

## 1. The bricks were a stale checksum. Not the palette region, not propagation.

Every compressed block in `MAIN.img` carries a trailing 16-bit checksum that
`repack.py` never knew about:

```
+0x00       u32   declared length (counts from +0x04, so it includes the magic)
+0x04       4 B   magic 01 00 ee ff
+0x08       ...   LZSS stream
+0x04+len   u16   unsigned byte sum of [base, base+4+len), little-endian
```

The verifier is in the boot stub at `MAIN.img 0x0AF6-0x0B7E` — uncompressed, so
it disassembles cleanly:

```
0b02  mov.l  @r6+,r12    ; len
0b0c  add    r12,r5      ; data end
0b0e  mov.b  @r5,r13     ; stored u16
0b46  mov.b  @r4+,r6     ; }
0b4e  add    r7,r6       ; }  sum loop
0b7c  cmp/eq r1,r5       ; computed vs stored
0b7e  bt/s   0xbb2       ; equal -> run
0b82  ...                ; else -> error path
```

Checked against stock, both exact first try:

```
block0  base 0x10000  computed 0xc948  stored 0xc948 @0x02f324
block1  base 0x40000  computed 0xd54a  stored 0xd54a @0x6ea79f
```

And against the build that bricked it most recently: computed `0xd5c5` vs stored
`0xd54a`. Delta **+123** — exactly the byte-sum delta of its three-byte colour
edit.

This predicts every result on record: all three MAIN CPU ERRORs, and both
successful flashes (which were preamble-only and never touched a summed region).
It is also the only theory that explains why **3 bytes bricked the deck exactly
as hard as 222**. A sum check is pass/fail, not proportional.

`repack.py` now recomputes it; `preflight.py` verifies it (20 checks, was 17).
The unmodified round trip is still byte-for-byte identical to stock.

### What this retires

- **`PROP_LIMIT`, `tools/propcost.py`, the knapsack in `mods/safe_theme.py`,
  `tools/safegate.py`.** All of it optimises propagation, which was never the
  failure mechanism.
- **The `0xCC2A70-0xCC54E0` quarantine.** Lifted. `probe_green_v2.05.UPD` edited
  `0xCC5414` inside that region and the deck booted normally.
- **HANDOVER §0's "version string at 0x568 — WORKED (twice)".** It never
  flashed. That build (`build/XDJ700.UPD`) had banner 1.15, the version the deck
  was already on, and HANDOVER records it being refused twenty lines earlier.
  Both successful flashes were `bump_version.py`, preamble only. So the real
  record was 3 payload flashes / 3 failures / 0 successes, and "the region is
  the problem" had no successful edit anywhere to contrast against.
- **HANDOVER §0's "that also rules out a blanket payload checksum".** It rested
  on the same false row.

## 2. Payload edits now run. The palette identification is wrong.

`build/probe_green_v2.05.UPD` — 6 bytes: 3 colour, 2 version string, 1 checksum.

```
block1 0xCC5414   RGBA 0x2C2C2CFF -> 0x00FF00FF   #2C2C2C -> green
block1 0x568      "1.15" -> "2.05"
```

Result: **deck boots normally, UTILITY reports 2.05, no visible colour change.**

- The version string proves the payload edit landed and ran. First time modified
  firmware payload has executed on this model.
- `#2C2C2C` measured 67,779 px / **8.07%** of the reference render, so a change
  could not have been missed. It is not duplicated either — the 32-byte context
  and the 4-byte entry are unique under both decoders, so we did not edit a
  spare copy.

So whatever `0xCC2A70-0xCC54E0` is, it does not paint the UI. The "94-entry
palette" identification was inferred from RGBA-shaped entries, smooth gradients
and tolerance-matched screenshot pixels, and it does not survive contact with
hardware. Colours are almost certainly not a static table in this image.

## 3. Two corrections you will care about

**The version check is inequality, not "newer".** A lower banner was accepted
twice (recovered from 1.16 and from 1.18-1.20 using Pioneer's original 1.15).
Only an *equal* banner was ever refused. Consequences: Pioneer's stock 1.15 is a
universal recovery from any version except 1.15 itself, and 9.99 is not a
terminal state.

**Two codec facts, read off the boot stub rather than guessed.** At `0x0ABC` it
clears a `0x1011`-byte buffer — 4113 = `N+F-1` for `N=4096, F=18`, confirming
the LZSS parameters — and it clears it to **zero**. Our `lzss.c` and
`decode_prov.c` inherited Okumura's `0x20` space-fill. Re-decoding zero-filled
moves 5,329 output bytes under V1 and 6,363 under Okumura, all in the first
0xA47C. Noted in `lzss.c`, not yet applied so offsets stay valid.

Separately, the declared length counts from `+0x04`, so our decoders read 4
bytes too many at the end of each stream and produce ~5 bytes of junk. Harmless
so far; `find_blocks` and both decoders should be corrected.

## 4. Where this leaves it

Solid ground now:

- The container, S-records, section CRC, literal patching, **and the block
  checksum** are all correct. Arbitrary payload edits install and run.
- Recovery is proven and cheap, and Pioneer's stock file works from anywhere.
- The boot stub (3,136 uncompressed bytes, 79.1% decodes as valid SH-4) is the
  only trustworthy code in the image. It should have been read on day one — it
  answered in an afternoon what three sessions of arguing about 30%-literal
  regions did not.

Still open:

- **Where colours actually come from.** Not that table. Worth checking whether
  the UI palette is built at runtime, or lives in the PANL section, or in a
  flash region the .UPD does not carry.
- **The codec is still wrong.** V1 and Okumura diverge at output offset `0x74`
  and end 231 KB apart; the oracle scores 93 mis-decoded constants. Fixing the
  zero-fill is a start. Until the decode is right, absolute output offsets
  beyond the early region should not be trusted — and note that literal patching
  works regardless, because it operates on stream positions.
- `BEU_MAP.md`'s dispatcher / `jsr @r1` chain remains withdrawn (see
  `BEU_TRACE.md`); it was match-derived in 0-4.5%-literal regions.

Everything is on branch `arena/01a07172-xdj700-mods`. Start with `BOOTSTUB.md`,
then `PAYLOAD_EDITS.md`, then `build/FLASH_ORDER.txt`.

---

# Addendum — same day, later: network, UART, and the decision

## What happened after the flash result

**The LAN check (no flashing).** Deck on 192.168.1.231, TCP 1051/12523/50003
open, silent on connect. Identified statically from the firmware:

- **1051 = dbserver** (Pro DJ Link database protocol — documented by the
  Deep-Symmetry/dysentery work). The deck runs the matching client machinery
  (`DbClientMBX`, `dbcli...connect(): PlayerInfo=NULL`). Not a shell.
- **The ctsh console is real and confirmed** — source path
  `D:\CDJ\Build_XDJ700\Middle\c??e\shell\ctsh.c`, commands `dump [-(b|w|l)]
  start [count]`, `edit`, task list (TNAME/TPRI/TSTS), memory map
  (ADDRESS/BASE/CUR/SIZE), framebuffer capture to `c:\_cdj_DispCap565.raw`,
  key emulation (KY), `login:`/`password`/`permission denied`. But it is a
  **serial** console: it carries its own `115200 B8 PN S1` UART setup string.

**The UART question, closed.** The service manual schematic (RRV4637, text
layer extractable) shows a 24-pin debug connector (DKN1445-A) — full CPU+DSP
JTAG **plus the console UART on pins 13/14, labelled SCIF0_TXD/RXD**,
annotated "Only Proto or TP". Two corrections fall out of it:

- **Console is SCIF0, not SCIF1** (my earlier SCIF1 call is withdrawn). The
  board logic is conclusive: SCIF1's pins multiplex RMII_TXD1/TXD0 — the
  Ethernet MAC, and the deck has Ethernet. SCIF0's pins multiplex unused LCDC
  signals. The two literal SCIF1 references I proved in the firmware are
  vestigial platform code; the absent SCIF0 literals are the usual
  decode-corruption blind spot (the ctsh module's strings are half-corrupted
  in both decodes).
- It confirms a factory debug port exists — behind the case. **Decision: the
  hardware route is dropped.** No opening the deck. The console's `dump`/
  `edit` remain the roadmap *if that decision is ever reversed* (see
  `CTSH_ROADMAP.md`, `UART_PADS.md`, `UART_DAY1.md` — all preserved).

## Consequences for the plan

Software-only live RAM requires code execution; via the update path that means
**patching code bytes**, which the current decoders cannot do reliably (V1 and
OKU disagree from output offset 0x74 and end 231 KB apart). Two codec facts
from the boot stub are now ground truth (ring 0x1011 bytes, N=4096/F=18,
zero-filled; declared length counts from +0x04 so decoders overrun by 4 B) but
the zero-fill change tested **inconclusive** — no landmark distinguishes the
fills. PANL.img ruled out (6.7% SH-4 validity, mostly padding).

**The live lead is consensus patching:** edit only bytes where both decodes
agree AND provenance says literal — modifying code regions without ever fully
solving the codec. Each validation costs a flash cycle (recovery is proven;
screen + boot/no-boot are the instruments). Alternative named and rejected:
parser exploitation of dbserver/MTP/NFS on live gear — wrong trade for a deck
that can't be replaced.

## Standing asks

- If you have a record of the original 0x568 "version string" flash (logs,
  photos of the confirm screen), it settles whether that row in the old
  HANDOVER was ever true — everything else says it never flashed.
- Any older `.UPD` builds or notes from before 2026-09-05 are wanted for the
  flash-result table in `PAYLOAD_EDITS.md`.
- Decision wanted: continue consensus-patch experiments (flash-cycle cost per
  step), or stop here. The colour work has one clean negative; nothing further
  proceeds without that call.
