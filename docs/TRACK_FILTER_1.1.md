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

## Still open: which code emits the messages

Settled as far as a static sweep can take it (2026-11-06): **the messages are
not reached by an absolute pointer in this image.** For every 4-byte word `W` in
the image, `W - offset` was computed for all 15 tag offsets and the sets
intersected: **0 bases** explain all 15, at either the text address or the
`@Ui_Browse> ` prefix. The common bases seen cover at most 4 tags and the value
is not plausible (e.g. `0x0E230000`, where the last tag would land past the end
of the image), so they are collisions, not a window. The other candidate form --
offsets relative to a base held in a register -- does explain the data: the
sub-offsets `0x0C, 0x38, 0x84, 0xC0, ...` each appear as small integers all over
the image, which is exactly what a shared-pool index looks like and also what a
small literal looks like, so it proves nothing on its own.

The PC-relative forms were swept too, since those need no load base at all:
every `mova @(disp,PC),R0` (`0xC7xx`) in the image was resolved against its own
address, and **none of the 9 whose target falls in the blob region lands on a
filter message** -- they land in the earlier `@Ui_Browse>` strings, which is the
ordinary coincidence you get when code sits just before a string pool. Same
negative result for `mov.l @(disp,PC)` pools (0 words match any tag offset).

Consequence: the port cannot start by xref'ing the strings. Either the apply
routine is found by structure (the browse module's own message table) or the
match rule is established on a deck that has the filter. That is why the
playlist route stays the shipping path, and why `filter_scan.py` keeps the tag
list as the specification rather than pretending to find code.

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
