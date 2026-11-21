# Colour waveforms on a Pro DJ Link deck (linkrgb)

Two problems with the colour waveform mods, both fixed here:

1. **RGB over Link** (RELEASE_NOTES "Known issues", ROADMAP "RGB waveform
   over Pro DJ Link"): the colour waveforms only draw on the deck holding the
   USB stick. A deck that loads the track over Link stays stock blue.
2. **Wrong colours after SEARCH / BROWSE on the deck with the stick** (no
   Link needed; reproduced with the Link cable unplugged): SEARCH forward,
   then back, leaves track 2's colours drawn on track 1.

Files:

- `tools/linkrgb.py`: the module (SH-4 assembly, shasm).
- `tools/linkrgb_apply.py`: cable installer, with `--check`, `--status`,
  `--log-only` and `--revert`.
- `tools/linkrgb_test.py`: machine-code tests on `emu/sh4.py`.
- `tools/linkrgb_probe.py`, `tools/linkrgb_identity_probe.py` and
  `tools/linkrgb_overview_dump.py`: read-only deck inspectors used to find
  the facts below.

Everything lives in RAM and a power-cycle returns the deck to stock. Nothing
is flashed. It needs the colour waveform mod (rgb_overview + rgb_detail)
already installed, and the installer refuses without it. **Cable install
only.** It is not in PACK.BIN; see "Not done" below.

## Why the linked deck is blue (fw 1.15, static)

- `0x08BAE380` maps a device type to an ANLZ context slot. Type 3 (own USB)
  gets slot 0; **every other device gets -1**. A Link track therefore never
  gets an ANLZ context, so PWV4/PWV5 never reach RAM.
- The Link DB client (`dbcl_*` around `0x08074C0C`) can ask the other player
  for:
  - the stock waveform (`dbcl_GetWaveData`, `dbcl_GetParWaveData`);
  - the beat grid (`dbcl_GetQuantizeData`);
  - cues (`dbcl_GetUsbSdCueInfo_Ext`).

  It has **no request for the colour sections**.

## What the deck can do instead

- **NFS.** The other player's stick is mounted under a drive letter
  (`NFS mount PlNo=%d,Dev=%d,Lett=0x%x(%c)`).
  - Generic fopen `0x08DDF53C` (`wpath "L:/...", mode, opts`) dispatches on
    the letter's device type. Type 5 goes to NFS open `0x08D712E8` through
    the pool word **`0x08DDF7F4`**, which is loaded once.
  - Listing every pointer into the NFS module from outside it shows this is
    the only open.
  - NFS fread `0x08D72204` is reached through 5 pool words (`READ_HOOKS`).
    On the deck the Link track is read through **generic fread**
    (`0x08DDFFE4`), not the media read server.
- **The analysis path is computable from the audio path.** The ANLZ loader
  does not read it from the database.
  - `N = 0x08D08878(path)`, with `h = (h*0x5BC9 + c)*0x93B5 + c` over the
    UTF-16 PPTH path, then `% 200003`.
  - Folder `P%03X` packs bits 0,2,6,7,9,13,16 of N.
  - File: `PIONEER/USBANLZ/P%03X/%08X/ANLZ0000.EXT`.

  Checked on a real 1396-track export: **1396 of 1396** match.

## The module

The module sits at `0x0989E000`: code up to 0xC00, data up to `0x0989F200`.
This is the free tail of the loader's 64 KB, and the installer refuses
unless it reads zero. Build: 2676 B of code.

| entry | hook (pool word, stock -> ours) | does |
| --- | --- | --- |
| `lopen` | `0x08DDF7F4`: NFS open | Read-mode open of a file outside `/PIONEER` and `/.PIONEER` = an audio file. Logs it (ring of 4) and hashes N. **It confirms a load only on a re-open** (see below), then copies the path, bumps GEN and sets PEND. No I/O. Registers, stack and pr are untouched. |
| `lrs0..4` -> `lread` | the 5 NFS fread pool words | If PEND is set, a `tas.b` claim lets exactly one task run `lfetch`. Then the stock read, with r4-r7 as the caller left them. |
| `lfetch` | - | Builds `L:/PIONEER/USBANLZ/.../ANLZ0000.EXT` on the same drive letter and reads it with the firmware's own fopen/fseek/fread/fclose and request block. PWV4/PWV5/PWV6 go into SLOT4/5/6 in 32 KB pieces: tag cleared first, written last. GEN is checked per section and per piece, so a newer load stops it and publishes nothing. |
| `snap` | overview cave pool word `0x081D8CD0` (`COPY5`) | Same new-track detection as `rgb_detail.COPY_SRC`. `lnew` decides whether the track is Link or local, then calls `lfix` instead of the three blind copies. |
| `lfix` | - | **Colours only ever belong to the loaded track** (see below). |
| `lcol` | centre painter pool `0x08DC3D94` (DCAVE) | Fast path: the verified length equals the loaded one, so it jumps straight to DCAVE. Otherwise it reruns `lfix` at most every 100 ms, so the colours land as soon as the deck refills its buffer. Preserves r1, r2, r5, r8-r15, MAC and pr for DCAVE. |

Each hook is a single atomic 32-bit pool-word write. `--revert` restores
every word to stock.

ENABLE (`V_EN`): 0 off, 1 log only, 3 log + fetch.

### Which Link open is "the load" (learned on the deck)

- **The deck re-opens the file it really loaded**, 1.5-1.7 s after the first
  open. It is 5.8 s when both decks load the same file at once.
- **It pre-opens the neighbouring list track** 100-250 ms after a load, and
  never re-opens it.
- Skipping fast through a list opens every track once.

Rules:

- A file is fetched only when it is opened **twice within `CONFIRM_MS`**
  (15 s).
- A re-open less than `PRE_MS` (700 ms) after *another* file's open is a
  neighbour pre-open and never confirms. When skipping forward, that
  neighbour is the track just left.
- The first build fetched on the first open. It showed the neighbour
  track's colours, with the wrong waveform shape, because RGB mode takes
  heights from PWV5.

### Colours only ever belong to the loaded track (`lfix`)

**Problem.** The stock snapshot copies the shared ANLZ buffer once, on the
new-track paint. SEARCH, BROWSE preview and the Link server also use that
buffer, so it can still hold another track. On the deck, with track 1
loaded, `linkrgb_probe.py` read:

- screen: 413.7 s (track 2);
- buffer: 462.0 s (track 1);
- player length word `0x0C0E6D1C`: 69304 = 462.0 s at 150/s, which equals
  the PWV5 entry count.

**What does not work as an identity:**

- **Length alone.** In one 1396-track library, about 110 pairs of different
  songs share an exact PWV5 length, and 484 pairs are within ±53 ms.
- **The BPM word** `0x0C0E6D10`. It follows the tempo fader (1310 -> 1375
  at +5 %).

**The check.** Before copying from the buffer, all three must hold:

1. **Overview whiteness** of the buffer's PWAV (`ctx+0xD9C`: LE header,
   then 400 B of `h | w<<5`) equals the loaded track's overview the deck
   keeps (`PREVIEW 0x0BA3BCCC`, 400 × {height, whiteness}) on **all 400
   columns**.
2. **Overview heights** are within ±`H_TOL` (3) on every column. The deck
   rescales PREVIEW heights for some tracks. One 579 s track measured +2 on
   333 columns, +1 on 21 and 0 on 46, with whiteness identical.
3. **The buffer's PWV5 entry count** is within ±`LEN_TOL` (8 entries,
   ~50 ms) of the player's length word.

**Result:**

- All three hold: copy PWV4/PWV6/PWV5.
- Otherwise: blank the slots (stock blue), never another track's colours.
- A Link track's own fetch counts as verified when its slots match the
  length word.

## Tests (no deck)

Run `tools/linkrgb_test.py [STICK]`. It runs the machine code on
`emu/sh4.py` with blk1 mapped (the hash is the firmware's own). The file API,
NFS and DCAVE are stubbed in Python over a synthetic stick, or a real export
when `STICK` is given. **118 checks pass.** Borderline cases covered:

- **Hash:** 23 paths (accents, CJK, long ones); the firmware and the Python
  model agree.
- **`lopen` hook hygiene:** r4-r7 at the stock open, r8-r14, sp, pr and the
  result all unchanged.
- **`lopen` filtering:**
  - `/PIONEER` and `/.PIONEER` are skipped, in any case; `/PIONEERS/` is
    audio;
  - modes `r+`, `w` and `a` are ignored;
  - an over-long path is logged but not fetched;
  - EN 0 / 1 / 3.
- **Load detection:**
  - track + neighbour pre-open + re-open: the track wins;
  - 10 tracks skipped 400 ms apart: nothing fetched, then only the last one;
  - a re-open 5.8 s late (both decks on one file) is confirmed;
  - a second open more than 15 s later is not;
  - repeated re-opens of the current track: no refetch.
- **Abort:** a newer load stops the fetch between 32 KB pieces. Nothing is
  published and the file is closed.
- **All 5 read sites:**
  - each one fetches, chains to the stock fread, and preserves registers
    and macl/mach;
  - the `tas.b` claim keeps it to one fetch across tasks;
  - analysis with and without PWV6.
- **Failures:**
  - missing .EXT;
  - superseded;
  - not PMAI;
  - a section larger than its slot (the others are still published).
- **Snapshot:**
  - no Link activity: byte-identical to `COPY5`;
  - fetch before paint, and paint before fetch;
  - a local track after a Link track;
  - a paint outside the 8 s window.
- **Identity (`lfix` / `lcol`):**
  - SEARCH forward/back with a stale buffer: blank, then copied once it is
    refilled;
  - a **different song of exactly the same length** is refused;
  - a browsed track in the buffer is refused;
  - heights +2/+1/0 or +3 everywhere: accepted;
  - one height +4, or one whiteness off: refused;
  - DCAVE sees the painter's registers;
  - the 100 ms rate limit;
  - a Link fetch survives a disagreeing length word until the next track,
    then is blanked.
- **Real export** (`STICK` given): 12/12 random tracks fetched
  byte-exact.

The installer was exercised on the mock deck (`tools/mock/mock_run.py`):

- stock words and a zero module area are seen on a booted firmware;
- it refuses without the colour mod;
- log-only install, status, revert and re-install;
- the upgrade path from an older linkrgb build.

## Hardware status (2 × XDJ-700, fw 1.15, Link, one stick)

Tested by one user on their own decks between 2026-11-17 and 2026-11-18,
playing as players 1 and 4. The colour waveform mod came from XDJ Mod Studio
1.0.9.2. Install was by USB cable.

### Verified on the deck

| case | result |
| --- | --- |
| Deck without the stick loads over Link | RGB centre waveform and overview. Fetch 64-84 ms (PWV4+PWV5). Load time unchanged (~2 s). No freezes. |
| Neighbour pre-open | Fixed (confirm-on-reopen). The right track's colours. |
| Skipping ~10 tracks fast on the Link deck | The last track gets its own colours. Up to ~2 s before colours appear; the user found that acceptable. |
| Both decks load the same track at once | Colours on both after `CONFIRM_MS` was raised to 15 s. |
| Load on deck A first, then B, or B first, then A | Same colours on both. |
| Deck with the stick: SEARCH forward/back | Bug reproduced without Link (upstream colour mod). Fixed by `lfix`, with `--status` showing blank-then-copy. |
| Deck with the stick: tracks the deck rescales (579 s track) | Stayed blue with exact matching. Coloured with `H_TOL` 3 ("colours verified for a track of 579.3 s"). |
| Read sites | The deck uses generic fread (`0x08DDFFE4`); the media server saw 0 reads. |

### Seen once, not reproduced

- **A "waiting" icon on deck A**, after skipping 10 tracks on the Link deck
  and then browsing on deck A. That build fetched whole sections at once.
  Reads are now 32 KB pieces with abort. Not seen since, but not
  stress-tested either.
- **"Wrong overview near the end of the track (time-to-cue warning)" on the
  Link deck.** Most likely the deck-A SEARCH issue above; not reproduced
  afterwards. Unconfirmed.

### In progress at the time of this PR

- Deck A (stick) is running the final build (2676 B). The user is still
  confirming over a longer session that no track stays blue for good and
  that SEARCH/BROWSE never show another track.
- Deck B (Link) was verified on the build just before `lfix`/`lcol`. The
  final build has not yet run on deck B over Link.

### Not tested

- Firmware other than 1.15, and other models. Every address is
  fw-1.15-specific; the installer refuses if any hook word is not stock or
  ours.
- More than 2 players, or player numbers other than 1 and 4.
- Link from rekordbox on a laptop, or from a CDJ (not an XDJ-700) as the
  source.
  - Expected: no `/PIONEER/USBANLZ` over NFS gives "missing file"
    (`why 1`), so the deck stays stock blue.
- A deck with **its own stick that also loads over Link** (mixed). This is
  covered in the emulator only.
- Pulling the source stick while the other deck fetches. Failure paths are
  covered in the emulator only.
- Long sessions (hours), and libraries much larger than ~1400 tracks.
- Exports other than a classic rekordbox USB export (PPTH `/Contents/...`).
- The other mods (stems, hotcue8, touchfx, ...) in combinations other than
  the tester's.
- The 3-band centre (PWV7). It is copied by cw.c itself and not handled
  here.

## Known limits / not done

- **Overview painted before a late copy.** If the copy lands after the
  overview layer was painted, it stays blue until the next repaint (Browse
  open/close). The centre waveform reads SLOT5 every frame, so it colours
  immediately.
- **Blanks are transient by design.** `--status` "blanked" counts the
  moments the buffer still held another track. The copy follows within
  ~100 ms of the deck refilling it.
- **RAM only; reinstall by cable after every power-on.**
  - PACK.BIN integration is not done: `stick_pack` reserves
    `0x09890000-0x098A0000` as LOADER, which overlaps the module.
  - Integrating it means moving the module or carving that range.

## Deck session (cable, RAM only)

1. `python tools/linkrgb_apply.py --check`: hook words stock/ours, module
   area zero, colour mod present.
2. `--log-only`, then load a few tracks over Link. `--status` lists the
   opens (first / confirmed / re-opens / neighbour pre-opens) and the
   analysis path it would read. Check the file exists on the source stick.
3. `python tools/linkrgb_apply.py`: installs and enables the fetch.
   `--status` shows:
   - fetch ok;
   - sections;
   - ms;
   - which read site triggered it;
   - "colours verified for a track of X s";
   - copied / blanked counts.
4. Anything odd: power-cycle (stock) and keep the `--status` output.

On a Windows source checkout, copy the app's `_internal/hidapi.dll` into
`vendor/`. `xdj700_usb_shell.py` registers it with `os.add_dll_directory`.
pip's `hidapi` is a different module (no `hid.Device`) and must not be
installed.
