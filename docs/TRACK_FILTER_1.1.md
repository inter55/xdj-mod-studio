# Track filter on the XDJ-700 (1.1, started)

The 700 has no browse filter. The XDJ-1000 MK2 has one, and the two decks share
the browse module (`@Ui_Browse`, `DISPCMD SELECT_MENU`) and the BPM-range data
layer (`djdbBPMRange`, `INDEX:SHashBPMRange`), so this is a port, not a new
feature. No Pioneer code is copied.

## Established (2026-11-05)

- The real image lives in the official update file, `XDJ-1000/XDJ1KMK2.UPD`.
  Two LZSS blocks decode to 29512700 bytes (the second is exactly
  `MAIN1000.img`, which is that block decompressed, so the file on disk is not
  a container at all). The 700 decodes the same way, which is how the codec is
  known.
- The 1000 image carries 15 `DISPCMD FILTER` messages, starting at decoded
  offset `0x12EC4C`. They are not separate strings: they sit in one
  packed log-message blob beginning at `0x12EAC8`, each message written as
  `@Ui_Browse> DISPCMD FILTER ...\r\n` and padded to a 4-byte boundary.
- The stock 700 image has **0** such messages, so the feature is genuinely
  absent, not hidden behind a flag.

## The exact filter, read out of the 1000 image (2026-11-06)

`tools/filter_scan.py XDJ-1000/MAIN1000.img` lists the deck's filter log
commands, and together they enumerate the entire feature. Verbatim, packed in
the `@Ui_Browse` log blob at file offsets `0x0F4E74`..`0x0F51A8`. Verbatim, all
15, plus the reset command that sits just after them:

```
@Ui_Browse> DISPCMD FILTER CHANGE APPLY
@Ui_Browse> DISPCMD FILTER CATEGORY STATE CHANGE: Category %d, OnOff %d
@Ui_Browse> DISPCMD FILTER PARAMETER EDIT START: Type %d
@Ui_Browse> DISPCMD FILTER INPUT CHAR CHANGE cursor %d
@Ui_Browse> DISPCMD FILTER INPUT CHAR ENTER:
@Ui_Browse> DISPCMD FILTER RATING CHANGE: Value %d
@Ui_Browse> DISPCMD FILTER RATING ENTER:
@Ui_Browse> DISPCMD FILTER COLOR CHANGE: SelColor %d, OnOff %d
@Ui_Browse> DISPCMD FILTER MASTER FOLLOW CHANGE: OnOff %d
@Ui_Browse> DISPCMD FILTER SELECT MYTAG TAB SWITCH: kind %d
@Ui_Browse> DISPCMD FILTER MYTAG STATE CHANGE: OnOff %d
@Ui_Browse> DISPCMD FILTER MYTAG CRITERIA CHANGE: Criteria %d
@Ui_Browse> DISPCMD FILTER MYTAG SHOW INFO: OnOff %d
@Ui_Browse> DISPCMD FILTER MYTAG ITEM ON: pos %d OnOff %d
@Ui_Browse> DISPCMD FILTER MYTAG ITEM SCROLL: initial %d
@Ui_Browse> DISPCMD RESET FILTER SETTING
```

The table below reads those messages back into behaviour:

| message | what it proves |
| --- | --- |
| `DISPCMD FILTER CHANGE APPLY` | one Apply key; nothing filters until it is pressed |
| `DISPCMD FILTER CATEGORY STATE CHANGE: Category %d, OnOff %d` | the categories are independent on/off switches |
| `DISPCMD FILTER PARAMETER EDIT START: Type %d` | entering a category's parameter editor |
| `DISPCMD FILTER INPUT CHAR CHANGE cursor %d` / `INPUT CHAR ENTER:` | BPM and KEY are **typed as text**, not chosen from preset bands |
| `DISPCMD FILTER RATING CHANGE: Value %d` / `RATING ENTER:` | rating is one numeric value entered, not a star picker |
| `DISPCMD FILTER COLOR CHANGE: SelColor %d, OnOff %d` | each colour toggles on its own, so several can be on at once |
| `DISPCMD FILTER MASTER FOLLOW CHANGE: OnOff %d` | BPM/KEY can be taken from the sync master's loaded track |
| `DISPCMD FILTER SELECT MYTAG TAB SWITCH: kind %d` | MY TAG has tabs |
| `FILTER MYTAG STATE CHANGE` / `CRITERIA CHANGE` / `SHOW INFO` / `ITEM ON: pos %d OnOff %d` / `ITEM SCROLL: initial %d` | MY TAG: one criteria per tab, per-item on/off, and a scroll back to the first item |
| `DISPCMD RESET FILTER SETTING` | a single reset back to everything shown |

So the exact model is: **five categories -- BPM, KEY, RATING, COLOR, MY TAG --
each switchable on/off, all AND'ed on Apply**, with MASTER FOLLOW able to source
BPM and KEY from the other deck, conditions saved per USB device (manual p. 30),
and Reset clearing them. Nothing in the list suggests a "preset band" concept:
the BPM and KEY conditions are text the DJ types.

### What that means for ours

| deck category | ours | verdict |
| --- | --- | --- |
| BPM, typed | `--bpm 124-128`, `--bpm 126`, `--bpm '>=124'` | same vocabulary; the **matching rule** for a typed value is not in the strings (see below) |
| KEY, typed, several | `--key Am,F#m` | matches |
| RATING, one value | `--rating 4` = 4 and up | open: `Value %d` does not say exact or at-least |
| COLOR, per colour | `--colour red --colour blue` (OR'ed) | matches |
| MY TAG, tabs + items | none | **missing**: the vendored parser already reads `exportExt.pdb` and its TAGS table, but table 8 (`TAG_TRACKS`, the tag-to-track link) is still an unparsed "unverified" row, so nothing can say which track carries which tag |
| MASTER FOLLOW | none | nothing to do offline: for a playlist the equivalent is explicit BPM/KEY values |

Three consequences worth writing down honestly:

1. The BPM rule is the one thing that decides whether a copied filter gives the
   same list. Typing `126` must be compared against a real deck (how many BPMs
   come back) or read out of the apply routine, whichever lands first. Do not
   guess a plus/minus band.
2. RATING has the same question in smaller form: `4` may mean exactly four
   stars. Our minimum-stars reading is the useful one but it is ours, not the
   deck's.
3. Until MY TAG exists we can say "BPM, key, rating and colour filters, written
   as playlists", and only after it can we say "the same categories as the
   XDJ-1000MK2's TRACK FILTER".

One naming caution: the `--presets` folder (Warm up / Build / Peak thirds, 4★+,
keys with 3+ tracks) is **ours**, worked out from the library. The deck has no
band presets at all -- its BPM and KEY conditions are typed -- so that folder
must never be described as "the XDJ-1000 filter". The deck's own filter is the
five-category model above, and the only exact way to reproduce it on the 700 is
that model, not the presets.

## Which code emits the messages -- solved 2026-11-08

The earlier negative ("no absolute pointer reaches these strings") was a tool
artefact, not a property of the image. The mapping and the emitters are now
known; the details and the corrected method are in "Solved 2026-11-08" at the
end of this document. In short: the image is flat at `0x08000000`, the messages
are referenced through the **uncached alias** `0xA8000000 + file offset`, and
all 48 browse messages are emitted from one code block around
`0x0950EFBE`. `tools/filter_scan.py` now resolves every message to a verified
`mov.l @(disp,PC)` pool load (48/48 on the 1000, 26/26 on the 700).

## The route that needs no reverse engineering

`export.pdb` playlists can be written today (`tools/onelibrary_to_pdb.py`
carries BPM, key, colour, rating and my-tag ids). Filtered playlists — a BPM
band, one key, one colour, 4 stars and up — give the deck BROWSE > PLAYLIST
lists that behave like a saved filter, with no firmware work at all.

## Built 2026-11-05: `tools/filter_playlists.py`

This is the 1.1 delivery for the most-requested item. It edits the stick's
existing `export.pdb` surgically (rows appended to the playlist tree and
entry tables only; no track, no field, no page of any other table is
rewritten) and reads the result back with the independent vendored parser
before it reports success.

    python tools/filter_playlists.py /path/to/stick --bpm 124-128 --rating 4
    python tools/filter_playlists.py /path/to/stick --key Am,F#m --colour red \
        --folder FILTERS --sort bpm
    python tools/filter_playlists.py /path/to/stick --list

Filters: `--bpm` (band, `>=`, `<=` or exact; repeatable), `--key`,
`--colour`, `--genre`, `--rating` (minimum stars), `--search` (title /
artist / file name) and `--limit`. All given filters must match. Entries
are ordered by `--sort` (`bpm` by default). `--folder` groups the filter
playlists under one folder; a name that already exists is refused unless
`--force`, which adds a numbered copy. `--dry-run` writes nothing.

Details that matter for correctness:

* Ratings are stored two ways by different exporters (0-5 stars, or
  rekordbox's 0/51/102/153/204/255). The scale is detected from the library
  itself (peak value > 5 means the scaled form), so `--rating 4` means the
  same thing on both.
* `tempo` in the track row is BPM x 100, so a 124-128 band is compared in
  hundredths and never rounds at the boundary.
* `export.pdb` is copied to `export.pdb.bak-filter-N` (first free N) before
  the first write of a run.
* A OneLibrary-only stick (no `export.pdb`) is refused with a pointer to
  `tools/onelibrary_to_pdb.py` rather than silently doing nothing.

Test: `tools/filter_playlists_test.py` (21 checks) builds a synthetic library
(10 tracks across BPM, key, colour and rating, one existing playlist),
writes each filter, and asserts the selected titles and order, the folder
placement, that every pre-existing track/playlist/entry is byte-unchanged,
plus dry-run, no-match, duplicate-name, `--force`, backup numbering and the
OneLibrary refusal.

In the app: **Library > Filter playlists** (BPM, key, colour, rating, words)
writes the same playlist through this tool as a job, on whatever stick is
chosen in Settings. The button is in the Library toolbar; the panel explains
that the deck has no filter of its own and that the result appears under
BROWSE > PLAYLIST.

### `--presets`: the whole filter folder in one go (2026-11-05)

One command writes the folder a DJ actually wants on the stick, worked out
from that library's own numbers instead of fixed BPM values:

* **Warm up / Build / Peak** -- the library's BPM range cut into thirds at
  its own values, as half-open bands, so every track lands in exactly one
  band (the test asserts 10/10 tracks, no duplicates and no gaps);
* **4 stars and up** and **5 stars** -- only when the library reaches them;
* **Key <name>** -- the up-to-four keys with at least three tracks behind
  them.

    python tools/filter_playlists.py /path/to/stick --presets
    python tools/filter_playlists.py /path/to/stick --presets --folder "MY SET"

Everything goes under one folder (`FILTERS` by default): one backup, one
write, then each playlist is read back and every pre-existing row checked
unchanged. Running it again leaves the playlists that are already there
alone, so nothing is duplicated; `--force` asks for numbered copies, and
`--dry-run` lists what would be written. In the app: **Library > Filter
playlists > Make the whole set**.

Still not covered: my-tag filters. The reader is not the gap -- the vendored
`rekordbox_pdb` package parses `exportExt.pdb` and its TAGS table (categories
and tags) today; table 8, `TAG_TRACKS`, is the tag-to-track link and is still
marked unverified, so it has never been decoded against a real file. Writing
`exportExt.pdb` is not needed for playlists at all: only reading that table
is, and no rekordbox export on this machine contains one to learn it from.
And no deck has loaded one of these sticks yet — the proof is the
vendored reader above, which is the same parser rekordbox-exports were
validated against. On the deck the check is BROWSE > PLAYLIST and the entry
count + first/last track.

## Solved 2026-11-08: the mapping and the emitters

### The file-to-address mapping is flat

`XDJ-1000/MAIN1000.img` is the decoded image `unpack_1000.py` writes, and it is
flat: file offset `F` is cached address `0x08000000 + F`, and the uncached alias
is `0xA8000000 + F`. Three independent checks agree:

* all 48 `@Ui_Browse> DISPCMD` message starts have a word equal to
  `0xA8000000 + F` (and none at `0x08000000 + F`);
* every one of those words is loaded by a real `mov.l @(disp,PC),Rn`
  instruction, so they are literal-pool entries, not stray data;
* the 232C command table at file `0x88AF0` holds `0x09xxxxxx` pointers whose
  targets are inside the image and disassemble as code, which only works with
  the same base.

The 2026-11-06 "0 bases" result and the interim "base `0xA8022594` explains
18" result were both artefacts of testing `W - offset` across messages that sit
**24 bytes apart**. With that spacing, one stray data word anywhere in the image
"explains" a whole cluster of neighbours at neighbouring bases. The corrected
test is the pool load: a pointer only counts if a `0xD`-form instruction loads
that exact word.

### The emitters

All 48 browse messages (including all 15 FILTER ones and `RESET FILTER
SETTING`) are emitted from one contiguous code block, file
`0x150EFBE..0x150F896` = address `0x0950EFBE..0x0950F896`. The strings
are at file `0xF49F0..0xF5294`; their pointer words live in the shared literal
pool at file `0x150F2B0..0x150F964`. The filter block is the middle of it:

| message | address of text (uncached) | pool word | string loaded at | DISPCMD id |
| --- | --- | --- | --- | --- |
| FILTER CHANGE APPLY | `A80F4E68` | `0950F630` | `0950F4FE` | `101B` |
| FILTER CATEGORY STATE CHANGE | `A80F4E94` | `0950F634` | `0950F530` | `101C` |
| FILTER PARAMETER EDIT START | `A80F4EE0` | `0950F638` | `0950F55E` | `101D` |
| FILTER INPUT CHAR CHANGE | `A80F4F1C` | `0950F63C` | `0950F58C` | `101E` |
| FILTER INPUT CHAR ENTER | `A80F4F58` | `0950F640` | `0950F5B2` | `101F` |
| FILTER RATING CHANGE | `A80F4F88` | `0950F644` | `0950F5E0` | `1020` |
| FILTER RATING ENTER | `A80F4FC0` | `0950F924` | `0950F658` | `1021` |
| FILTER COLOR CHANGE | `A80F4FEC` | `0950F934` | `0950F68A` | `1022` |
| FILTER MASTER FOLLOW CHANGE | `A80F5030` | `0950F938` | `0950F6B8` | `1023` |
| FILTER SELECT MYTAG TAB SWITCH | `A80F506C` | `0950F93C` | `0950F6E6` | `1024` |
| FILTER MYTAG STATE CHANGE | `A80F50AC` | `0950F940` | `0950F714` | `1025` |
| FILTER MYTAG CRITERIA CHANGE | `A80F50E8` | `0950F944` | `0950F742` | `1026` |
| FILTER MYTAG SHOW INFO | `A80F5128` | `0950F948` | `0950F770` | `1027` |
| FILTER MYTAG ITEM ON | `A80F5160` | `0950F94C` | `0950F7A2` | `1028` |
| FILTER MYTAG ITEM SCROLL | `A80F519C` | `0950F950` | `0950F7D0` | `1029` |
| RESET FILTER SETTING | `A80F51D8` | `0950F954` | `0950F7F6` | `102A` |

Each branch has the same shape: build the id in `r4` (`mov #16,r4; shll8
r4; add #n,r4`), call `0x09511A38` (the log entry that takes the id), push the
message pointer, call `0x09539494` (the formatter/printer), then branch to the
shared exit at `0x0950F8AA`. The ids are consecutive from APPLY to RESET, and
the 700 uses the same numbering for the messages it has (`MODE_CHANGE` =
`1002`, `SELECT_MENU` = `100B`), which is why the same tool resolves both
images.

What this gives the port: every filter action now has a stable name and an
address. The next step is the behaviour behind the ids, and it is *not* in this
block -- this is the command logger. Either follow the caller that passes id
`101B`, or take the filter state that the category/parameter branches mutate
and read the apply routine from there. The BPM match rule (exact value vs band)
is still a runtime question; nothing in the ids or the strings decides it.

Run it with:

    .venv/bin/python tools/filter_scan.py XDJ-1000/MAIN1000.img
    .venv/bin/python tools/filter_scan.py cache/blk1.bin      # 700, 26/26

`--md` writes the resolved table; `--base` overrides the automatic alias test
for an image whose mapping is not established yet.

## The second log layer: `@Ui_BrowseKey> KEYCMD` (2026-11-08)

The 1000 also carries 50 `@Ui_BrowseKey> KEYCMD` messages, 31 of them FILTER.
They use the same rule (uncached alias, verified pool load), so the whole key
layer is xref-able now. The filter keys, in file order:

```
0951CF0E  pool 0951D0EC   KEYCMD FILTER
0951CF8C  pool 0951D104   KEYCMD FILTER BPM KEY
0951D032  pool 0951D110   KEYCMD FILTER MY TAG
0951D0AE  pool 0951D114   KEYCMD FILTER MY TAG INFO
0951D176  pool 0951D3B4   KEYCMD FILTER RESET
0951D1FC  pool 0951D3C0   KEYCMD FILTER VALID BPM
0951D2F4  pool 0951D3C8   KEYCMD FILTER VALID KEY
0951D370  pool 0951D3CC   KEYCMD FILTER VALID RATING
0951D422  pool 0951D670   KEYCMD FILTER VALID COLOR
0951D49E  pool 0951D67C   KEYCMD FILTER EDIT BPM
0951D51A  pool 0951D680   KEYCMD FILTER EDIT BPM RANGE
0951D596  pool 0951D684   KEYCMD FILTER EDIT KEY
0951D62C  pool 0951D690   KEYCMD FILTER EDIT COLOR PINK
0951D700  pool 0951D8F8   KEYCMD FILTER EDIT COLOR RED
0951D790  pool 0951D900   KEYCMD FILTER EDIT COLOR ORANGE
0951D820  pool 0951D904   KEYCMD FILTER EDIT COLOR YELLOW
0951D8B0  pool 0951D908   KEYCMD FILTER EDIT COLOR GREEN
0951DA02  pool 0951DB78   KEYCMD FILTER EDIT COLOR BLUE
0951DA92  pool 0951DB7C   KEYCMD FILTER EDIT COLOR PURPLE
0951DB2A  pool 0951DB8C   KEYCMD FILTER AND
0951DBFE  pool 0951DDE0   KEYCMD FILTER OR
0951DCCA  pool 0951DDF4   KEYCMD FILTER CATEGORY 1
0951DD96  pool 0951DDF8   KEYCMD FILTER CATEGORY 2
0951DE9E  pool 0951E094   KEYCMD FILTER CATEGORY 3
0951DF6A  pool 0951E09C   KEYCMD FILTER CATEGORY 4
0951E03E  pool 0951E0B0   KEYCMD FILTER ITEM 1
0951E15E  pool 0951E360   KEYCMD FILTER ITEM 2
0951E234  pool 0951E368   KEYCMD FILTER ITEM 3
0951E30A  pool 0951E36C   KEYCMD FILTER ITEM 4
0951E41A  pool 0951E620   KEYCMD FILTER ITEM 5
0951E4F0  pool 0951E628   KEYCMD FILTER ITEM 6
```

What that vocabulary adds to the DISPCMD layer:

* **Four category keys and six item keys.** Together with the DISPCMD names
  this reads as a 4-category grid with six selectable items under a category.
  The keys are positional (`CATEGORY 1`..`4`, `ITEM 1`..`6`), so they do not
  name which category is which; the DISPCMD strings still supply the names
  BPM, KEY, RATING, COLOR and MY TAG.
* **AND / OR exist as separate actions.** The earlier "all AND'ed on Apply"
  reading was too narrow: the deck offers both combinators, so the Apply
  routine has to be read for which one it honours (per category or over the
  whole set).
* **Seven named colours** -- PINK, RED, ORANGE, YELLOW, GREEN, BLUE, PURPLE --
  each with its own EDIT key. "Off" is the absence of a colour, not an
  eighth name, which matches `COLOR CHANGE: SelColor %d, OnOff %d`.
* **VALID vs EDIT vs RESET are different verbs.** BPM, KEY, RATING and COLOR
  each have a VALID key and BPM/KEY/COLOR have EDIT keys, so the dialog checks
  a condition before/while editing rather than only at Apply. `EDIT BPM` and
  `EDIT BPM RANGE` being separate also means the typed BPM has a *range*
  editor next to it -- relevant to the exact-vs-band question, but it still
  does not answer it (the range editor may be for a band condition).
* **MY TAG is its own tab** with an INFO key, consistent with the DISPCMD
  MYTAG messages, and separate from the four positional categories.

These are key names, not behaviour: the routines behind them are still the
next step. But they make the UI shape concrete, and every address above is
reproducible with the same tool.

## 2026-11-08: the 1000MK2's filter code, found (static)

The "no pointer reaches the messages" verdict above was the uncached-alias
blind spot again (CLAUDE.md sec. 31): with base `0xA8000000` (file offset |
0xA8000000) ALL 48 `@Ui_Browse> DISPCMD` messages in `MAIN1000.img` are
reached by plain pool pointers, including all 16 filter messages.

- **Dispatcher** `FUN_0950EE76` (Ghidra, proj1000): the browse screen's
  DISPCMD switch. Every case posts a message to the browse engine through
  `0x09511A38` (post: allocates a 0x70-byte packet from a pool, type word
  `0x32C9` at +8, command at +0x24, args at +0x28/+0x2C/+0x30, sends it to
  the mailbox whose id lives in RAM `0x0C7A30B0`) and logs through
  `0x09539494`.
- Filter cases -> engine messages:

  | screen action | case | message |
  | --- | --- | --- |
  | CHANGE APPLY | 0x20 | 0x101B |
  | CATEGORY STATE (category, on/off) | 0x21 | 0x101C |
  | PARAMETER EDIT START (type) | 0x22 | 0x101D |
  | INPUT CHAR CHANGE (cursor) / ENTER | 0x23 / 0x24 | 0x101E / 0x101F |
  | RATING CHANGE (value) / ENTER | 0x25 / 0x26 | 0x1020 / 0x1021 |
  | COLOR CHANGE (colour, on/off) | 0x27 | 0x1022 |
  | MASTER FOLLOW (on/off) | 0x28 | 0x1023 |
  | MY TAG tab / state / criteria / info / item / scroll | 0x29..0x2E | 0x1024..0x1029 |
  | RESET FILTER SETTING | 0x2F | 0x102A |

- **Receiver (next):** the mailbox id variable `0x0C7A30B0` is loaded at
  `0x09517C94`, `0x09518144`, `0x095185AC/660/6D8`, `0x09518754`,
  `0x095188DC` -- the browse engine task. Ghidra has that stretch as
  unanalysed fragments: run auto-analysis on `0x09517000..0x09519000`
  (or create the function at its real entry), then follow the switch on
  packet+0x24 to the 0x101B (Apply) handler: that is the code that
  actually narrows the browse list, and the reference for ours.
- The 700 has the same DISPCMD dispatcher shape without these cases and no
  filter engine at all, so the port is our own engine (a C mod over the
  browse list) driven the same way: screen action -> engine message ->
  apply. The 1000MK2's Apply handler tells us which list it narrows and
  how it matches BPM/KEY text (the open question above).

### Receiver hunt, 2026-11-08 (later) -- what is ruled out

- The packet type `0x32C9` is never a constant: the poster builds it as
  `mov #51; shll8; add #-55`, so constant scans for it find nothing.
- Functions that load the mailbox-id variable `0x0C7A30B0` through a found
  instruction (`0x09517BFC`, `0x095180E4`, `0x0951854E`, `0x09518734`,
  `0x095188B6`, `0x0951899A`) are all SENDERS of a different message (type
  `0x709`, 0x870 bytes, to another mailbox) -- not the receiver.
- `0x095437F2` polls mailboxes `0x0C7A3040..4C` (not ours); its "handler"
  `0x09447274` is an error reporter (called with codes 0x52/0x54/0x56/0x62).
- No "subtract 0x1001 then `braf`" jump table anywhere in
  `0x09500000..0x09600000`.
- NEXT: a full Ghidra auto-analysis of the browse module (at least
  `0x09400000..0x09600000`) in `~/ghidra/proj1000`, then the data references
  to `0x0C7A30B0`: the one that feeds an RTOS receive call (the counterpart of
  the send `0x0944675E`) is the engine task, and its handling of command
  `0x101B` is Apply. Alternative: the receive function itself -- find the
  RTOS receive that pairs with send `0x0944675E` and list its callers.

## 2026-11-12: the receiver, the command switch and Apply -- found

The receiver hunt above is closed (static, `MAIN1000.img`, flat at
0x08000000; `IMG=XDJ-1000/MAIN1000.img tools/shdis.py`):

- RTOS pair: send `0x0944675E` -> `0x09589EDC`; receive `0x09589FFC`, with
  wrappers `0x09446764` (wait forever), `0x0944676A` (poll), `0x09446770`
  (timeout in r6).
- **Browse engine task** = the loop around `0x0954A0F8`: receive from the
  mailbox whose id is in `0x0C7A30B0`, 100 ms timeout. A packet whose
  sender (+16) equals `[0x0C7A3044]` (the browse screen) goes to
  `0x0954A55C`.
- **Command handler `0x0954A55C`**: checks type `0x32C9` at +8 (built as
  `mov #51 / shll8 / add #-55`, which is why constant scans missed it),
  then `cmd = [+0x24] - 0x1001`, 46 cases, `braf` table at `0x0954A648`
  (u16 offsets from `0x0954A5E4`). Filter cases:

  | cmd | action | handler |
  | --- | --- | --- |
  | 0x101B | APPLY | 0x0954B29A |
  | 0x101C | CATEGORY on/off | 0x0954B1EA |
  | 0x101D | PARAMETER EDIT START | 0x0954B16C |
  | 0x101E / 0x101F | INPUT CHAR change / enter | 0x0954B124 / 0x0954AEB2 |
  | 0x1020 / 0x1021 | RATING change / enter | 0x0954AE32 / 0x0954ADCE |
  | 0x1022 | COLOR | 0x0954AD18 |
  | 0x1023 | MASTER FOLLOW | 0x0954AC34 |
  | 0x1024..0x1029 | MY TAG tab/state/criteria/info/item/scroll | 0x0954ABCA .. 0x0954A90A |
  | 0x102A | RESET | 0x0954A88C |

- Each handler first checks the browse screen state (`0x0951959E()`: 2
  for Apply, 8 for the colour page).
- **Apply does not filter by itself.** It calls `0x095180E4(ctx, flag, 1)`,
  which sends message `0x71C` (`mov #7 / shll8 / add #28`) through
  `0x09518FC8` to the browse list owner (mailbox `[0x0C7A30B0]`): "rebuild the
  list". The conditions are NOT in the message. They are read from the
  shared browse context.
- **Filter state lives in the browse context `0x0DA74BE4`** (126 code
  references, the getter/setter module `0x095194E0..0x0951A8xx`). Colour
  bits: byte `0x0DA76F86` (= base + 0x23A2), bit k = colour k (0..7), set
  by `0x0951A75C(colour, on)` via `or.b/and.b @(r0,gbr)` with gbr = 0, read
  by `0x0951A47E(colour)`.

NEXT (static): the list builder that handles 0x71C, and in it the predicate
that calls the filter getters (colour `0x0951A47E`, and the BPM/KEY/RATING
getters next to it): callers of `0x0951A47E` outside the handler
`0x0954AD18` are the filter itself. That routine answers the BPM rule
(exact vs band, the open question above). Then the 700 port: the 700 has the
same browse module shape without the filter state, so ours is a C module
that holds its own state and drops non-matching rows when the deck builds the
list.

### 2026-11-12 (later): where the static trail stops

- Outside callers of the filter getters (`0x09516B54`, `0x09516DD4`,
  `0x09516E6E`, `0x09516EB8`, `0x0951D5xx..0x0951DAxx`) are all screen/key
  handlers (cursor states 3..6, colour buttons, debug logging), NOT the list
  predicate. Filter fields 0x2384..0x23AE of the context are touched only by
  the accessor module `0x0951A2C0..0x0951A8C0`.
- Apply -> `0x095180E4` -> `0x09518FC8`: allocates a **0x870-byte packet**
  (+8 = command 0x71C, +0xC ctx id, +0x10 sender, +0x14 arg, +0x1C = 8,
  +0x20 = 0x78, +0x6C.., u16 args from +0x70) and posts it; most of that
  function is debug printing per argument kind. `0x09516A58` is the request
  tracker (cancels a pending 0x709 / 0x70B, records the new 0x71C).
- No `mov.w` 0x071C compare, no `mov #7/shll8` + `braf` switch anywhere in
  the image: the 0x7xx command receiver dispatches some other way (likely a
  handler-pointer table). The three "0x71C" hits at 0x09454DC2 / 0x09455178 /
  0x094EB214 are struct offsets and an error code, not handlers.
- BPM rule, from the key layer: separate `KEYCMD FILTER EDIT BPM` and
  `EDIT BPM RANGE` keys = **a typed BPM plus/minus a range** (unit of the
  range still unknown: BPM or %). Good enough to design ours.

Decision: the 1000MK2's DB-side predicate is not needed for the 700 port --
the 700's database has no filter, so our module matches rows itself. The next
measurable step is on the 700, in the emulator with a real library (DB view
works since 2026-10-18): find where BROWSE > TRACK's list rows are built (DB
response / row array), then hook it to drop rows that fail the filter.

## 2026-11-12: the 700 side (emulator, user's library, BROWSE > TRACK)

- Snapshots: build/review_1110/tl_before.pkl (category list, cursor on
  TRACK) and tl_tracklist.pkl (TRACK list open, 263 tracks, A-Z). Built from
  cache/user_stick_mounted.pkl with DevUSB, rot +2, push.
- The screen holds only the 7 visible rows (text copies at 0x0BA6AE00..,
  0x0BA35xxx / 0x0BA37xxx / 0x0BA39xxx, 0x220 B per row, written by the
  browse screen module 0x08DB1xxx..0x08DB6xxx via memcpy). No track ids in
  them; no sorted id / pdb-row index of the 263 tracks anywhere in RAM.
- The list is a DATABASE TABLE: the 700 runs an in-RAM DB ("DJDB" tables:
  DJDBCONTENT, DJDBCOLOR, DJDBKEY, DJDBBPMRANGE, ...; indexes MPHash*/SHash*)
  and the browse list is table **DJDBLEFTBUF** (index IDXLEFTBUFCOND /
  SHashLeftBufCond). The screen reads it through the client stub
  **dbcl_GetLeftBuf** = FUN_08B742B4 (sends a 0x48-B request, msg 0x3000,
  offset/count, to the DB task and waits). Strings: DJDBLEFTBUF @0x08086C9C
  (used at 0x08C8ECF2 = table registration), djdbLeftBuf @0x08079524 (used
  0x08BC2D70).
- PLAN for the port: hook where the DB task FILLS DJDBLEFTBUF for a TRACK
  view and drop rows that fail the filter (count + offsets stay native).
  NEXT: the table registration at 0x08C8ECF2 -> the table's runtime row
  store; the writer of that store (py-core write watch during the push into
  TRACK) is the hook point; a row then names a DJDBCONTENT record (BPM, key,
  colour, rating all live there).
- Before/after RAM diff of the push (non-screen blocks) is a short list:
  0x096FB1C0, 0x0970D004/0x0970D804 (4 KB, 0x2E20.. records), 0x0981xxxx
  (DB task working memory, pointers 0x0E0B5C38 / 0x08E04BB8), 0x0B6DDF44
  (480 B), 0x0A4E17A4.. (row widgets). Inspect those first.
