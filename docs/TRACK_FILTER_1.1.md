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

## 2026-11-22: ON-DECK TRACK FILTER BUILT (emulator PASS on the user's library)

The 700 now has its own filter, a RAM mod like the others. Nothing is
copied from the 1000MK2; its behaviour (categories AND'ed, BPM = a typed
value plus or minus a range, KEY, COLOR, RATING, a reset) is the model.

### How the 700 builds a browse list (found this session, emulator)

* Entering a category, the DB task (`DBSMain_TASK`) copies a query result
  into the browse list buffer with **`FUN_08CDFC8C(dst, src)`**. In the
  emulator `dst` = `0x0B0E0FE8` (DJDBLEFTBUF), `src` = `0x0B0E2DD8`.
  Buffer layout: `+0` u16 capacity (50000), `+2` u16 row count, `+4` u16
  total, `+0xA` list type (`0x1004` for TRACK), `+0x34` u16 sort
  permutation (result side only; freed + cleared by the copy when it is
  the destination), `+0x4C` rows, 20 bytes each.
* A row is `{u32 id, u32 second id, u32, u32 (byte +0xC = 4), u8 sort
  letter +0x10, u8 kind +0x12 (column of the left text), u8 kind +0x13
  (right column)}`. Kinds 1..5 and `0x16..0x18` are DJDBCONTENT columns
  (3 = title, 1 = file name, 2 = path, 4 = ?, 5 = date), so a row with one
  of those kinds is a track and `id` is its content id (= export.pdb track
  id). Category rows have kind `0x1A` (menu items).
* The copy routine is reached through three literal-pool words:
  `0x08CE339C` (user `0x08CE3296`), `0x08CE42F8` (`0x08CE41DC`,
  `0x08CE42BE`), `0x08CE45AC` (`0x08CE4358`, `0x08CE442C`).
  Only this function and `FUN_08CE2FD6`/`FUN_08CE3EAC` load them.
* The screen then reads pages with `dbcl_GetLeftBuf` (`FUN_08B742B4`,
  request `0x3000`, offset/count), so a smaller row count is all the
  screen needs: the list, the scroll bar and the jump-to-letter search
  (`FUN_08CE08CC`) all follow `+2`.
* **BACK saves the shown list back into the result** with the same copy
  routine (dst = result, src = browse buffer). Re-entering the list copies
  it back. An in-place filter would therefore become permanent. When the
  result is still the exact one a filtered list came from (same rows
  pointer, count and permutation), our wrapper skips that save, so the
  result keeps the full sorted list and every visit filters it again with
  the settings of that moment (this is the 700's "Apply").
* With an empty result the deck copies the buffer onto itself once; that
  must not end the tracking (found by the empty-list test).

### Reading BPM / key / colour / rating on the deck

The 700's DB engine (a DeviceSQL-style library at `0x08E1xxxx`) keeps the
export.pdb pages in RAM. The deck's own record fetch `FUN_08C248FA(kind,
id, &out, 0)` (kind = table/column, 1..0x47; it returns a column as a
string and is used for the row texts) shows the engine calls, which we
make the same way from inside the DB task:

    h   = 0x08E120A4(&err, "djdbContent", "idxContent", 0, 0, 0, "=", 1, &key)
          key = {pointer to the u32 id, 4}
    rec = 0x08E12C90(h, &err)
    p   = 0x08E12DB0(rec, column, &len)
          0x08E129B2(h)                        /* close */

Column 0 of a content record points at the id field (`+0x48`) of the raw
export.pdb track row, so the row is `p - 0x48` and the fields are read
directly: `+0x20` key id, `+0x38` tempo (BPM x 100), `+0x58` colour id,
`+0x59` rating. Measured by calling `0x08E12DB0` for columns 0..47 on two
tracks: col 8 = `+0x38`, col 20 = `+0x20`, col 22 = `+0x58`, col 15 =
`+0x59`. Key names come from `djdbKey`/`idxKey` column 1 (UTF-16). This
library mixes musical names (`Abm`, `F#m`), and Open Key (`1m`, `9d`), so
every key is turned into a Camelot code before comparing (`8A` = `Am` =
`1m`).

### The mod

* `tools/trackfilter/tf.c` + `tfentry.S` + `build.sh` ->
  `build/trackfilter/tf.bin` at **`TF_BASE 0x09960000`** (1.4 KB code,
  ~2.3 KB with state, limit `0x0996D000`): between the PWV7 snapshot slot
  (ends `0x09960000`) and the end of the all-states-zero band
  `0x09836000..0x0996D000` (CLAUDE.md sec. 77). NOT deck-surveyed yet (the
  installer refuses without its own read-only survey on that deck).
  Header: `+0` "TFLT", `+4` tf_copy, `+8` &tf_cfg, `+12` &tf_prev.
* `tf_copy` = the three pools' new target: runs the stock copy, then (when
  TRACK FILTER is ON, MODS is ON and a condition is set) drops the track
  rows that fail, compacts the rest in order and lowers `+2` (and `+4`
  when it was the same count). Non-track rows are never touched; a track
  without a record is kept. Diagnostics: tf_calls, tf_lists, tf_in,
  tf_kept, tf_fail, tf_skips, and a ring of the last 8 copies (tf_log).
* Settings = six rows in hold-MENU **UTILITY** (tools/utility/ut.c, placed
  before the MODS row so the mod rows keep their places at the end):
  TRACK FILTER (OFF/ON), FILTER BPM (OFF, 60..200), FILTER BPM RANGE
  (+-0 = same whole BPM, +-1 .. +-20 BPM), FILTER KEY (OFF, 24 keys shown
  as `Am (8A)`), FILTER COLOR (OFF + the 8 rekordbox colours), FILTER
  RATING (OFF, 1..4 OR MORE, 5). They say NOT INSTALLED without the
  module. All conditions are AND'ed, like the 1000MK2. The filter applies
  to every list that shows tracks (TRACK, an artist's or album's tracks,
  playlists, KEY lists...): only rows that are tracks are judged.
* Range unit: BPM (the 1000MK2's `EDIT BPM RANGE` unit is still unknown;
  BPM is what DJs mean by "plus or minus 2").
* MY TAG: not done. The 700's DB does have `djdbexMyTag` (idxExtMyTag
  MyTagID, FUN_08C248FA kind 0x19), so it is reachable the same way once
  the track-to-tag table is found; left for later.
* No 1000-style FILTER screen yet: the UTILITY rows are the first UI (the
  task allowed it); a BROWSE > FILTER screen is the next step.

### Tests

* `tools/trackfilter.py emu` (user's library, `tl_before.pkl`): drives the
  real UTILITY rows, then opens BROWSE > TRACK and compares the deck's
  rows with export.pdb (the deck's own unfiltered order, filtered in
  Python) after each of: ON / 128 / +-2 / 8A (13 tracks), 124 / +-0 / key
  OFF (14, changed while inside the list), COLOR PINK (0, empty list),
  TRACK FILTER OFF (all 263 back, same order). No CPU exceptions.
  **RESULT: PASS.** Screens: `screenshots/trackfilter/`.
* Other lists (scratch check, same snapshot, BPM 128 +-2): BROWSE >
  ARTIST shows all 111 artists (kind 7 rows untouched); Alessandro
  Calabrese > "Coral City | Vol. 3" (album rows untouched) > its track list
  is filtered to 0 rows -- correct, its only track is 123 BPM.
* Deck installer `tools/trackfilter_apply.py` (`--survey` | install |
  `--revert`; the survey marker is per deck serial, so a mock-deck survey
  never counts for a real deck), `deck_session.py --trackfilter` = step
  5t (opt-in until proven on hardware). **Mock deck**
  (`tools/mock/mock_install.py --mode rgb --trackfilter`, real installers
  against the emulated deck): every step PASS, 5t survey + write + arm in
  the shared session; `tools/mock/verify_install.py` (now checks the
  module bytes, tf_prev and the three pools) **RESULT: PASS**.
* `tools/utility.py emu` with the new rows (13 mod rows now, together
  with another session's HOT CUE COLORS row; the mod rows keep their
  places from the end): **RESULT: PASS**.
* Build order: `tools/utility/build.sh` reads `build/trackfilter/tf.syms`,
  so `tools/trackfilter/build.sh` runs first (tools/build_studio.sh does;
  `make_public_repo.sh` ships `prebuilt/deck/trackfilter/`).

### Open / next

1. Deck: survey `0x09960000..0x0996D000` (read-only, `--survey`) and a first
   hardware run. On the deck the list copy runs in the DB task like in the
   emulator; the engine calls are the deck's own, but each lookup is one
   index query, so time a big TRACK list (thousands of tracks) on the deck.
2. A 1000-style BROWSE > FILTER screen (the UTILITY rows are the first UI).
3. MY TAG (djdbexMyTag is in the 700's DB; the track-to-tag table is next).
4. MASTER FOLLOW (BPM/KEY from the other deck over LINK).
5. Studio: a "Track Filter (beta)" tick box -> `--trackfilter`.
6. Permanent UPD: not baked (the rows say NOT INSTALLED in a flashed
   build until it is).

### 2026-11-22 (later): XDJ-1000MK2 reference in QEMU (started, not finished)

Goal (user): make the 700 filter IDENTICAL to the 1000MK2's, using the
cdj-nxs2-qemu emulator as the reference deck (no real 1000 here).
- Firmware: `~/Downloads/ref/cdj-nxs2-qemu/scripts/firmware/prepare_firmware.sh
  --model xdj1000mk2 XDJ-1000/XDJ1KMK2.UPD ~/Downloads/ref/x1k-fw` -> PASS
  (main_unpacked.bin sha 16928d8c..).
- `tools/x1k_qemu.py start|shot|key|touch|mon|stop`: machine `xdj1000mk2`
  (our ~/qemu-build-fast build has it), user's library over NBD. BOOTS to
  the main screen. Touch reaches the panel (QEMU log "touch pressed").
- BLOCKER: the stick does not mount -- BROWSE shows "NO USB" (the NBD
  server never got a read). Try next: hot-plug the stick after boot
  (`device_add usb-storage` via `mon`, the 700's boot-attach quirk), or the
  reference's own `launcher/deck.py` path for this model.
- Then: BROWSE > FILTER screens, the BPM range unit, rating rule (exact or
  at least), several colours/keys, AND/OR, Apply -- screenshots into
  screenshots/x1k_filter/, then build the 700 screen to match.

## 2026-11-22 later: the XDJ-1000MK2's rules, measured, and the 700 made to match

Measured on the real 1000MK2 firmware running in the reference emulator
(cdj-nxs2-qemu, machine xdj1000mk2; driver `tools/x1k_qemu.py`) with the
user's library, plus a copy (`cache/x1k_ratestick`) whose export.pdb has
ratings 1..5 and colours 1..8 set on a few tracks. Counts are the deck's own
[n] in the list header.

| condition | 1000MK2 result | rule |
|---|---|---|
| BPM 130 ±6 % | 234 | value × (100 ± p) %, inclusive |
| KEY C (RELATED KEY) | 32 | same key, relative minor/major, a fifth up/down (Camelot same number other letter, or ±1 same letter). Tracks whose key is named in Camelot/Open Key ("1m", "8A") never match |
| BPM + KEY | 28 | conditions are AND'ed |
| RATING 1 star | 2 (10 tracks rated 1..5) | exactly that many stars |
| COLOR pink + red | 4 | any picked colour (OR); COLOR ticked with none picked = all |

Screen facts: ± is typed on a keypad, 0..6 % only (7, 8, 9 greyed); BPM is
typed (integer); KEY picker = 12 majors in circle-of-fifths order + MINOR;
hold RESET = everything unticked, defaults 130 / ±6 % / C / 3 stars / no
colour; MY TAG tab = the export's My Tag columns with AND/OR criteria
(this library has none); tap TRACK FILTER = filter on/off, hold = this
screen; the list header shows a funnel and [count].

700 module changed to the same rules (`tools/trackfilter/tf.c`,
`tools/utility/ut.c`): FILTER BPM RANGE is now ±0..±6 %, FILTER RELATED KEY
matches related keys and only musical key names, FILTER RATING is exact
(1 STAR .. 5 STARS), the colour setting is a bitmask (the UTILITY row picks
one colour; the engine ORs any it is given). `tools/trackfilter.py`'s Python
model reproduces all five 1000MK2 counts above.

Still NOT identical: no TRACK FILTER button or condition screen on the 700
(settings are UTILITY rows; one colour at a time from the row); conditions
are not saved to the stick (RBFLTR.DAT) -- they live in RAM until power-off;
no MY TAG and no MASTER PLAYER (take BPM/KEY from the master deck).

## 2026-11-22 evening: the 700 already has the 1000MK2's filter back end

Found in the 700's own firmware (fw 1.15), only the screen and the list step
were missing:
- RBFLTR.DAT reader/writer module 0x08B3646C..0x08B38xxx + FilterFileWrite_TASK
  (saves the file about a second after a change). Your stick's RBFLTR.DAT was
  written by the 700 ("XDJ-700 1.15", defaults 130 / ±6 % / C / 3 stars).
- Condition store TF_ST 0x09603674: own set at +4 (entries 0x1C: +4 ticked,
  +0xE count, +0x10 values, +0x14 operator), five per-player copies at
  +0xFC + i*0xE4, change counter at +4.
- SETCOND 0x08B34D5A(0x300, type, 0, len, block, &changed): the FCND block,
  LITTLE-endian {flags (bit0 ticked), operator, u16 count, u32 values}.
- PRED 0x08B357CC(3, player, record): the "does this track pass" check, with
  0x08B35F6C BPM between (lo == hi means the BPM it rounds to: lo-50..lo+49),
  0x08B3602A key in list, 0x08B36138 rating equals, 0x08B361F8 colour bit
  (mask 0 = all).
- 0x08CE21FA = the 1000's list marker (row +0x0F bit 2, count at +4); nothing
  on the 700 calls it, so the 700 never filters by itself.

RBFLTR.DAT format (decoded from what the 1000 writes; big-endian on disk):
FMAI header (0x74 B, device name + version, block count) + four FCND blocks
{"FCND", hdr 0x14, len, type} + {flags, operator, u16 count, values}:
BPM type 6 op 6 [lo, hi] x100; KEY 0x0C op 0x11 [key, relative, fifth down,
fifth up] numbered Camelot nA = 2n-1, nB = 2n (Am 15, C 16); RATING 7 op 0
[stars, 0]; COLOR 0x0F op 0x11 [bitmask].

The mod now (tools/trackfilter/tf.c + tfs.c, 27.7 KB @0x09960000):
- list step: tf_copy calls PRED on each track row (so the matching IS the
  1000's code), conditions read from and written through the store, which the
  700 saves on the stick itself (per stick; rekordbox-set filters honoured).
  Never filters a copy FROM the shown list (BACK saves it back).
- screen + control: in a track list, the right end of the list header shows a
  funnel + [count] like the 1000 (dim funnel when off: the 700 has no TRACK
  FILTER button). Tap = filter on/off, hold 1 s = filter on + the TRACK
  FILTER screen: title + live [count], BPM KEY / MY TAG tabs, hold RESET,
  BPM / KEY / RATING / COLOR rows with tick boxes, BPM and ± keypads and the
  key picker over the player strip (CLOSE), MASTER PLAYER button. Leave with
  BACK, a knob push, or a tap on the title funnel; the list is redone at once
  (BACK + push injected, waiting for the screen to change between them).
- installer: checks the 700's own filter code is the fw 1.15 bytes first;
  chains the screen in front of the flush and hit-tester hooks; deck_session
  runs 5t LAST of the screen mods (others only accept heads they know).

Tests: trackfilter.py emu PASS (UTILITY rows, PRED lists == model, stick
file rewritten with the last conditions); trackfilter_screen_emu.py PASS
(hold opens, keypads + picker by touch, live count 48 = the 1000's, close ->
list 48, tap -> 263, hold RESET); mock install (rgb + phase meter + TOUCH FX
+ light + filter) + verify_install PASS. Not on a real deck.

Still not done: MY TAG contents (tab is there, empty like the 1000 with no
tags; needs the My Tag tables read in the DB task + a library with tags to
test), MASTER PLAYER (button drawn, does nothing yet: needs the master
deck's BPM/key over LINK).

## 2026-11-22 night: MY TAG and MASTER PLAYER

Measured on the XDJ-1000MK2 firmware (reference emulator) with a test copy of
the library stick (cache/x1k_ratestick: tag links written into exportExt.pdb
table 4 as {0, track id, tag id, 3}; the 1000MK2 reads them; your own stick has
the tags but no track uses them):
- MY TAG tab: four column tabs (tick box + name, long names cut at the tab
  edge), each listing only the tags tracks use, in position order, 2 x 3 grid,
  the knob scrolls a row (2 tags); CRITERIA AND/OR per column; "i" shows all
  four columns' used tags (chosen white, others grey).
- Rules: ticked column with no tag chosen = no filter; AND = all chosen tags
  on the track, OR = any; columns AND'ed with each other and with BPM/KEY/
  RATING/COLOR. Counts: Acid House 3, + Deep House AND 1 / OR 4, Genre OR +
  Components Vocal 1.
- Hold RESET clears only the tab you are on.
- My Tag choices are NOT saved in RBFLTR.DAT (the 1000MK2 and the 700's own
  writer write only the BPM/KEY/RATING/COLOR blocks): RAM only.
- MASTER PLAYER (manual): BPM and KEY of the sync-master deck into the BPM and
  KEY fields.

700 build (tf.c + tfs.c, 37 KB, ends 0x0996C84C):
- My Tag data from the deck's own tables in the DB task: djdbexSongMyTag by
  content id (one record per tag; column 0 -> tag id), djdbexMyTag by id
  (column 0 -> raw row + 0x14: +0x0C column, +0x10 position, +0x1B is a
  column, +0x1D name offset). Columns = ids 1..8 with the column flag, in
  position order. Used tags gathered from each track list (up to 32 for the
  screen; the filter itself has no limit).
- Live count cache now 1536 tracks (BPM/key/colour/rating word + tag bits);
  longer lists filter fine but show no live [count].
- MASTER PLAYER = this deck's loaded track: BPM = 0x09D24692 (track BPM x100)
  with the tempo 0x0B6C3ED0 applied, key = the text 0x09D30430 the main
  screen shows (remembered while the main/PERFORM screen is up -- the browse
  reuses that buffer); sets both values and ticks BPM and KEY. A LINK master
  is not read yet.
- The shown-list detection gained a second rule: the first copy after a BACK
  press (the deck saving the shown list) copies FROM the shown list (needed
  when a list comes from a stored result without a sort table).

Tests: tools/trackfilter_mytag_emu.py PASS (snapshot from
tools/trackfilter_tagstick.py): columns + used tags read, the three 1000MK2
counts reproduced on the 700, knob scroll, "i", RESET, MASTER PLAYER on
"Adventure" (124.76 BPM, Abm -> 125 / Abm). trackfilter.py emu PASS,
trackfilter_screen_emu.py PASS, mock install + verify_install PASS.
1000MK2 reference screenshots: screenshots/trackfilter/x1k_ref/.
Not on a real deck. Not done: MASTER PLAYER from a LINK master.

## 2026-11-22 night: first deck run (<deck serial>) and the fixes

Deck result (user photos): the TRACK FILTER screen drew, but parts of it were
missing (the "i" button, CRITERIA) and the browse list's titles and
"[TRACK]" header showed through on top. Leaving with BACK crashed the deck;
the user power-cycled, so no counters survived.

Causes, found in the emulator (it shows the same thing once you look at
the layer between presents, not at the composed screen):
- The browse clears the screen EVERY frame with two 2DG fills (0,0 800x480
  and 4,4 792x472). strip_stock's crop rule wanted `x1 >= 800`, but x1 is
  the last pixel (799), so neither was cropped: each frame wiped the screen,
  and the old 8-point damage check (most points on black) mostly missed it.
  Fix: any A0 fill that starts above the screen and reaches below it is
  cut to its part below (tfs_cropped). After: idle repaints 0 (were 1 per
  present).
- The browse also redraws its header and the selected row's title with the
  CPU per-pixel text blender FUN_08C2F5C2 (r5 = pixel address), which no
  2DG strip can catch. New hook tfs_txt_entry (tfentry.S, header +24) on
  pools 0x08C2F8FC / 0x08C31FC4: while the screen is open, a pixel inside
  [tfs_tlo, tfs_thi) (the layer's rows 0..319, or 0..479 with a keypad) is
  dropped like the blender's own en == 0 exit; chained to tfs_prev_txt
  (stock or the light-mode wrapper). Installed by trackfilter.py install /
  trackfilter_apply.py (also --revert); verify_install checks it.
- The knob scrolled the browse list under the screen. keys() now takes the
  knob's steps for the My Tag grid and puts the counter back (rotacc).
- Damage check: 19 line checksums (every 3rd pixel, lines 17 px apart)
  instead of 8 points; tfs_dmg = the lines found changed last time.

The BACK crash did NOT reproduce in the emulator (BACK, with and without a
track loaded: relist completes, no exception). Best guess: the per-frame
full repaint (the screen wiped and redrawn on every present, ~256k uncached
writes) plus the injected BACK/push relist starved the deck. That load is
gone now; the crash itself is unproven either way. Next deck run: open the
screen, change nothing, BACK. Then try ticking something and BACK.

## 2026-11-23 DECK: the filter's working memory is in use on this deck

On <deck serial>, after loading an UNANALYSED file (STEMS via FOLDER), the
firmware wrote 8-bit data centred on 0x80 (values 118..156) over
0x0996C730..0x0996D0FF -- probably the deck's own analysis buffer. The
filter's bss (tfs_*/tf_* state, 0x0996C7AC..0x0996C91C) sits inside it:
tfs_mode read 0x80808080 -> screen opened by itself, went black, crashed on
BACK. The SURVEY saw zero (idle). Code (0x09960000..0x09969xxx) intact.
0x0996C000..0x0996C700 and 0x0996DF00.. read zero. The 11-12 "all states
zero" map never included an unanalysed load. FIX NEEDED: move the state
(and ideally the module) out of 0x0996C700..0x0996D100 -- find which
buffer it is and its full extent first (unanalysed load in the emulator +
write watch). tools/tf_probe.py = read-only state probe.

## 2026-11-23 later: moved + self-check -> HARDWARE PROVEN (<deck serial>)

- Stock deck, unanalysed STEMS file playing: band map 0x09700000..0x09A00000
  (docs/deck_logs/band_map_2026-11-23.log) shows NOTHING in 0x0996xxxx; the
  emulator (list + screen + ticks) writes nothing outside the module either.
  So the writer of 0x0996C730..0x0996D100 is unknown (only seen with our mods
  in). Not chased further.
- tf moved: TF_BASE 0x099F0000, end 0x099FCA28, limit 0x09A00000 (after the
  FLAC+Opus image end 0x099EAA80). tf_sane() at tf_copy/tfs_flush/tfs_touch:
  tf_cfg.on > 1, tfs_mode > 1, tfs_edit > 3, tfs_tab > 1 or tfs_relist > 3
  -> tf_dead = 1 (in .data) for good, copy goes to STOCK_COPY directly (tf_prev
  is state too), screen/touch pass through. Emu test with the deck's
  0x80808080 garbage: dead 1, no screen, BACK to categories, no exceptions.
- DECK (install_2026-11-23c.log, all PASS 10 min): tap = on/off ([278]),
  hold = clean screen that stays clean, BACK with nothing changed = fine,
  tick BPM + BACK = shorter list + count, tap = full list: ALL PASS (user).
  tools/tf_probe.py after: tf_dead 0, 3145 draws, 15 full repaints, 29961
  stripped / 1308 cropped stock commands, 0 damaged lines, 8 relists.

## 2026-11-23 release prep: home moved again, 0x099F0000 -> 0x09940000

The mock install with FLAC+Opus showed 0x099F0000 is inside FLAC+Opus
(vfile _end 0x099F7478; the "image end 0x099EAA80" note was the code only).
The deck test above ran without FLAC, so it never met. New home 0x09940000
(end 0x0994CA28, limit 0x09960000) = the top of the PWV7 snapshot slot,
which now ends there (rgb_detail SLOT7_MAX 0xB0000 -> 0x90000, 21 min of
PWV7; longer tracks: 3-band centre falls back to stock). Inside the band
surveyed zero on both decks and inside the stick loader's band. Same code
that passed on the deck; only the address differs. The deck install's
survey checks the area before writing.
