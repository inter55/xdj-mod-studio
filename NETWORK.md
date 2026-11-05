# Network ports vs the `ctsh` console — what the firmware actually says

Written 2026-09-05. Question: the deck (192.168.1.231) listens on TCP 1051,
12523 and 50003. Is 1051 the `ctsh` debug console?

## Short answer

**No. Port 1051 is `dbserver` — the Pro DJ Link database query protocol, a
documented, interoperable service.** The `ctsh` console is in the firmware and
is exactly as powerful as hoped (`dump`, `edit`, framebuffer capture, key
emulation, task inspection), but its own strings say it is a **serial UART
console at 115200 8N1**, not a TCP service.

## Port 1051

Independent Pro DJ Link reverse-engineering identifies it directly: "Dbserver
(TCP 105, 'CDJ database query protocol') is an alternative to NFS for metadata
fetch — a custom request/response protocol... rekordbox laptops expose
dbserver" (CDJ_DJM_Emulator discovery notes, citing the Deep-Symmetry /
dysentery research). The deck is a Pro DJ Link player, and its firmware
contains the matching machinery:

```
OKU 0x74a8c   DbCli[en]tMBX        (DbClientMBX)
OKU 0x74ac3   odbcl...             (their ODBC-like db client)
OKU 0x74ad4   ...connect(): PlayerInfo=NULL
OKU 0x74bcd   rekordbox strings throughout the region
```

A `bind()` search was tried and deliberately rejected: exactly one
`\x02\x00\x04\x1b` (AF_INET + port 1051 BE) shaped hit exists in the OKU decode
at `0x101cad5`, but it sits mid-instruction-stream, has no counterpart in the
V1 decode, and the neighbourhood is 60% literal mixed with opcode noise —
coincidence, not a sockaddr. Given this project's history, one unconforted
pattern match is not evidence.

Port 50003 is in the Pro DJ Link range (TCNet / PRO DJ LINK Bridge territory).
12523 is the known rekordbox companion port. Neither has a static signature
either; both are expected services for a link player.

## The `ctsh` console — confirmed, and it is the real thing

All in block1, located in the OKU decode around `0x6f000-0x76000` (V1 places
the same strings ~0x34000 later; the codecs disagree on absolute offsets as
always — the raw-stream offsets are the ground truth):

```
raw 0x4ed09   D:\CDJ\Build_XDJ700\Middle\ce\shell\ctsh.c     <- source path
raw 0x4ef5a   115200 B8 PN S1 F...                           <- UART line setup
raw 0x4efc9   login: \0 password                             <- auth strings
raw 0x4d29c   c:\_cdj_DispCap565.raw / .raw / open           <- framebuffer capture
OKU 0x71874   dump  ... [-(b|w|l)] start [count]             <- memory dump cmd
OKU 0x71886   ... edit ...                                   <- memory edit cmd
OKU 0x6f2c8   KY Com...                                      <- key emulation
OKU 0x7179d   permission denied
OKU 0x70c4c   TNAME TPRI TSTS WAIT...                        <- task list (ps)
OKU 0x70c73   ADDRESS ... BASE ... CUR/SIZE                  <- memory map display
OKU 0x6f278   Level=0(OFF),1(ERR),2(CMD),3...                <- debug log levels
OKU 0x6f6ae   usage: ... [Po.. (default=5001)] .. [IP] [size] <- udp/ping TEST tools
```

Plus a complete embedded TCP/IP stack around it (ICMP, ARP, RARP, bootp, DHCP,
fragments, source route, MTU, routes, `TCP/IP sample prog...`).

## Why serial, not TCP

- The console carries its own **UART line setup string** ("115200 B8 PN S1") —
  you do not put a baud rate on a TCP socket.
- No telnet- or banner-like strings anywhere in either decode; `login:`/
  `password`/`permission denied` sit immediately next to the serial string.
- The only port numbers in the console region belong to its **network test
  commands** (`default=5001`), not to a listener.
- No static bind signature for any console port, in either decode.

Engineer consoles of this era are UART consoles. The likely reason none of the
three TCP ports speaks is that none of them is a shell.

## What this means for the project

The original hope stands, just over a wire instead of a socket: `dump` reads
runtime RAM — including the **decompressor's own output buffer**, i.e. the
correctly decompressed image that we have spent three codecs failing to
reproduce; `edit` writes it — live UI memory, no flash, no checksum, no version
ladder, and instantly reversible by reboot. The framebuffer capture command
(`_cdj_DispCap565.raw`, RGB565) and the memory-map display (`ADDRESS/BASE/
CUR/SIZE`) would also answer the palette question definitively: find the
framebuffer base, scan RAM for the colour table the UI actually reads.

Practical route: open the case, find the SH7724's SCIF UART pads (the console
string says 115200 8N1, no parity), attach a 3.3 V USB-serial adapter. Look
for the `login:` prompt. The auth strings are in the firmware image we already
hold, so whatever credential check exists is analysable offline.

## Confirming 1051 from your own LAN (optional, read-only)

- Connect rekordbox to the same network and let it browse the deck — that
  exercises dbserver exactly as designed; a packet capture shows the exchange.
- `nc 192.168.1.231 1051` for a few seconds: dbserver is request-driven and
  stays silent on connect; a login banner would mean a console and disprove
  this whole analysis.
- The dbserver wire format is openly documented (Deep-Symmetry's dysentery
  notes); a single well-formed query returning database metadata settles it.

None of this requires flashing, and none of it is recoverable-from — it is
identification, not modification.

## Provenance / how to regenerate

Sandbox restarted; `work/`, `.venv/` and /tmp were wiped. Regenerated from
`build/RECOVER_stock_v199.UPD` via `tools/updtool.py unpack`; decodes via
`cc -O2 -o /tmp/decode_prov tools/decode_prov.c` then

    /tmp/decode_prov work/blk01.lz /tmp/D_v1.bin /tmp/D_v1.prov v1
    /tmp/decode_prov work/blk01.lz /tmp/D_oku.bin /tmp/D_oku.prov oku

All offsets above are OKU-decode output offsets unless prefixed `raw` (raw LZSS
stream offsets — codec-independent). The git history had to be restored from
origin after the restart; `build/probe_green_v2.05.UPD` was re-checked out and
matches its recorded sha256 `465ec4f0...`.
