# Finding the SCIF1 pads — the exact procedure

Companion to `UART_DAY1.md` (the first-session runbook). This covers
everything up to that point: what the pins are, where to look, and how to
confirm them without guessing.

## THE ANSWER: the board has a dedicated debug port (confirmed from the schematic)

The official service manual's schematic page (manualslib 3800373, p.4 — the
text layer extracted cleanly) shows a 24-pin debug connector on MAIN ASSY:

```
FOR DEBUG (DSP&CPU)          "Only Proto or TP"

 pin  signal            pin  signal
  1   V+3R3_CPU        13   SCIF0_TXD   <-- console TX
  2   GNDD              14   SCIF0_RXD   <-- console RX
  3   GNDD              15   CPU_EMU_BRK
  4   NC                16   CPU_RST
  5   CPU_TRST          17   GNDD
  6   CPU_TDI           18   DSP_TMS
  7   CPU_TMS           19   DSP_TDI
  8   CPU_TCK           20   DSP_TDO
  9   CPU_TDO           21   DSP_TCK
 10   CPU_MPMD          22   DSP_EMU0
 11   NC                23   DSP_TRST
 12   NC                24   (NC)
```

Full JTAG for both CPUs **plus a UART**, on one labelled connector. "Only
Proto or TP" is the production note: on retail boards it is either an
unpopulated footprint or reduced to test points. Unpopulated pads or TP
circles are solderable with a normal iron — no microscope, no ball tracing.
This is exactly the access we hoped for.

## Channel correction — the schematic says SCIF0, and the board agrees

My earlier firmware-side conclusion was SCIF1. That is now withdrawn, and the
reasoning is worth keeping because it is the interesting kind of wrong:

- SCIF1 lives on port PTL2/PTL3, which multiplex **RMII_TXD1/TXD0 — the
  SH7724's Ethernet MAC**. The XDJ-700 has an Ethernet jack (Pro DJ Link).
  A board designer uses those pins for Ethernet, not a console.
- SCIF0 lives on PTF2/PTM0/PTM1, which multiplex LCDC signals (LCDRD,
  LCDVCPWC, LCDVEPWC) — LCD-timing outputs this panel design does not use
  (the panel takes video via the VOU/DV bus instead). Those pins are free,
  and the schematic spends them on the debug UART. Textbook.
- The two SCIF1 literals I proved in the firmware are real but are best read
  as vestigial platform code — nothing on this board can physically route
  SCIF1 out. The absence of proven SCIF0 literals is the decode-corruption
  blind spot this project keeps hitting: the console module's strings are
  half-corrupted in both decodes, so its code's literal pool did not survive
  either.

Net: console = **SCIF0**, wired to debug-connector pins **13 (TX) / 14 (RX)**,
ground on **2 or 3**. Still 3.3 V LVTTL, 115200 8N1. Adapter RX <- pin 13,
adapter TX -> pin 14.

## Effort estimate, honestly

- Open the case: screws, exploded view in the same manual. ~15 min careful
  work.
- Find the connector footprint on MAIN ASSY: 24-pin, part DKN1445-A (the
  schematic calls it CN501-class; "blue character is silk for services" per
  the PCB diagram — service silks exist on this board).
- If the footprint is populated: plug/clip 3 wires. If unpopulated: solder to
  pads 2, 13, 14 — normal iron, normal solder, no magnification beyond
  reading the pin-1 mark.
- The PCB connection diagram (p.17) marks "measuring points" with encircled
  numbers — check whether the UART nets have their own TP circles; if so it
  is even easier than the connector.

Before opening anything, download the full manual (free, e.g. elektrotanya
"pioneer_xdj-700_rrv4638" or the manualslib download) and look at: the
schematic sheet with the debug connector (which nets/series Rs), the PCB
connection diagram (TP circles near it?), and the service-mode page (the
48-page service manual, manualslib 3194596).

## Two things that may gate the console (know them before concluding failure)

1. **CPU_MPMD (pin 10)** is on this connector — a boot-mode strap. Pioneer
   debug consoles of this era are sometimes conditioned on boot mode, and
   "Only Proto" hints the console may be idle in normal retail boot. If pin
   13 is silent at 115200 in normal boot, do NOT conclude the route is dead
   until trying the service mode documented in the service manual.
2. The ctsh strings include login/password and "permission denied": expect an
   authentication prompt, not an open shell. The credential check runs in RAM
   and is recoverable once `dump` works — or the service mode may bypass it.

## 10-minute empirical test once wired (one wire, read-only)

Adapter GND -> pin 2. Adapter RX -> pin 13. Terminal at 115200 8N1. Power on.

- boot text or a `login:` prompt  -> console alive on SCIF0. Continue in
  UART_DAY1.md.
- silence in normal boot          -> try service mode per the manual, then
  report back. Do not start hacking straps on day one.

## Then

Go to `UART_DAY1.md`: dump calibration, BEU registers, DispCap, and send me
those three artifacts. First live RAM read is the whole goal — leave the
codec and persistent firmware alone, exactly as you said.
