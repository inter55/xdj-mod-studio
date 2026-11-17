# XDJ Mod Studio 1.1 — the plan (started 2026-11-06)

1.0 was a feature release that mostly proved things in the emulator. 1.1 is
deliberately different in shape: it lands the pile of work that is **already
built and emulator-passed but has never played on a deck**, then adds the
community's top asks on top. That combination is what makes it a "massive"
release rather than a bug-fix round.

Rules that still hold: XDJ-700 only; nothing flashed unless the user builds an
update file themselves; emulator before deck; never publish firmware; the
public repo is `tools/make_public_repo.sh`'s export. No new Reddit post per
release — edit the original and reply in its comments.

Status words used below, and nowhere else:
- **deck-confirmed** — somebody ran it on a real XDJ-700.
- **emulator-passed** — it runs on the deck's real firmware in the emulator,
  with a named test. Not proof for hardware.
- **built** — code exists, gate not yet written or not yet passing.
- **unbuilt** — design or research only.

## Wave 1 — ship what is already built (no new reverse engineering)

| Feature | State | What 1.1 still needs |
| --- | --- | --- |
| Touch FX Echo | blocked on memory: the ring page is fully used on the deck with audio playing, and a four-pass deck survey found 512/512 pages across the first 8 MB (0xC0000000..0xC0800000) holding data -- no free page to move to | needs a proven-free delay line **and** the denormal marker test; `FX_LIVE` ships Filter/Crush/Gate/Pitch meanwhile |
| Touch FX Flanger | blocked on memory: the ring page is fully used on the deck with audio playing, and a four-pass deck survey found 512/512 pages across the first 8 MB (0xC0000000..0xC0800000) holding data -- no free page to move to | needs a proven-free delay line **and** the denormal marker test; `FX_LIVE` ships Filter/Crush/Gate/Pitch meanwhile |
| Stem mute rewrite (4 blocks in one pass) | emulator-passed, deck-measured 2026-11-07: **27.8 s** to hear a mute with the fast trigger armed (was 24.88 s) — the deck holds ~28 s of decoded audio ahead of the playhead, so a change made to the file cannot be heard until that drains (`docs/STEMS_MUTE_LATENCY.md`) | under 1 s needs the mute applied downstream of decode (the mixer's per-stem gain), not another read-layer rewrite |
| Opus playback | **deck-confirmed 2026-11-07** (user: an Opus file loaded and played) | nothing; README/site/release notes can drop "nobody has played an Opus file on a deck" |
| 8 hot cues settable on the deck | built + the Studio editor (Track Builder > pick a track > Hot cues A–H: set any of the eight from the playhead, or leave a row empty to delete it; it rewrites only the cue section of the track's own analysis file, gate `tools/anlz_cues_test.py`) | one deck load of a track whose A–H came from the app |
| Stems replace the original track | built (`tools/stems_replace.py`, `selftest` passes end to end) | play a replaced MP3 on a deck and check waveform + hot cues |
| Stems reachable without FOLDER (reported from Italy, 2026-11-07) | measured on the deck 2026-11-07: BROWSE › FOLDER **does** open and lists the folders at the root of the stick (the emulator said it did not — emulator wrong; `build/folder_*.py` is not evidence here). `Contents` is not listed, so a `Title (STEMS).wav` beside its track is unreachable by browsing, and SEARCH is the rekordbox database only. Published FOLDER text corrected to the hardware result (README, site, release notes, app log); a root-level `STEMS/` folder the app writes beside the tracks is the cheap fix to consider for 1.1 | give the sidecar a real library row (the PDB writer already exists) **or** always replace in place; decide which before 1.1 ships |
| Hot cue paging by swipe | emulator-passed 2026-11-06 (gate 40 → 24 px; 32-px drag pages, 8-px wobble is a tap) | the deck run with `tools/bj_monitor.py` |
| Track filter as playlists | built (`tools/filter_playlists.py`, 21 checks) | the Studio button ("Make the whole set" already exists) + one stick on a deck |
| 8-pad grid view | integrated 2026-11-07: tap the HOT CUE tab for all eight cues (swipe still pages), merged into the hotcue8 module with a touch hook; every installer chains it; gate `tools/hotcue8_test.py` | a deck look — tap, hold a pad, confirm it plays that cue and returns to it on release |
| Light deck theme | emulator-passed | a deck look, and a decision on the text colours |
| The 1.0.1 macOS zip could be "damaged" (reported from Italy) | fixed and verified: the archive was made with `zip -r`, which put 2,468 AppleDouble sidecars inside the app bundle and broke its code-signature seal (macOS calls that "damaged"); `tools/make_app_zip.sh` strips them, re-seals, zips with `ditto` and proves the archive, and its `--verify` rejects the shipped 1.0.1 and 1.0 zips and passes the re-cut one | 1.0.2, not a re-cut of 1.0.1: the app DID need a change — 1.0.1 shipped without `vendor/rekordbox_pdb`, so in-place stems replacement, filter playlists and OneLibrary conversion died on ImportError; and the copy in `dist/` is an older build than the shipped one, so a re-cut would be a downgrade. Build fresh, gate with `tools/app_bundle_check.py` |

This wave is the cheapest "massive" in the release: none of it needs new
firmware understanding, only deck time. Every row already has its emulator
gate, so each one is a single checklist item on a deck session.

**The session itself is written out in `docs/WAVE1_DECK_CHECKLIST.md`** — the
install command, the taps for each item, the expected numbers from the
emulator, what counts as a failure, and a results table to fill in. Print it
and work down it; the filled table is the release evidence for these rows.

## Wave 2 — finish Touch FX (the visible headline)

Four of the eight buttons are live (Filter, Crush, Gate, and Pitch since
2026-11-10); the panel draws the rest as "COMING SOON". Echo and Flanger
are compiled out, not hidden.

1. **Pitch** — **emulator-passed and in the shipping build (2,480 B of the
   2,496 B wall), 2026-11-10.** Pad X = semitones, ±11; it drives the deck's
   own stretcher, so tempo and waveform stay put. The Q14 table and removal
   of redundant latch/phase checks freed the last 128 B; gates: +6 = 0.981
   shifted vs 0.811 dry, −8 = 0.947 vs 0.621, Filter/Crush/Gate unchanged
   (`docs/TOUCHFX_MEMORY_SURVEY.md`, "Update 2026-11-10"). The shipping
   engine is ring-free; `build_studio.sh` and `build_fxh.sh` now pass
   `FX_PITCH=1` and rebuild the installer cave. **Still needs one deck
   listen** before it can be called deck-confirmed.
2. **Roll** — **emulator-passed 2026-11-12, no audio-chip memory.** The
   panel presses the deck's own BEAT LOOP row (size 1/2, 1, 2 or 4 beats from
   X, following the finger) and presses RELOOP/EXIT on lift; with SLIP on the
   deck ends the loop itself. Gate `tools/touchfx_roll_e2e.py`; details
   CLAUDE.md §173. **Needs one deck listen**, including with SLIP on (the
   emulator is not trusted there).
3. **Reverb** — the honest problem child. It needs 50–65 KB, and there is no
   through-playback-zero block to give it: the 52,224 B at
   `0xC0073400..0xC0080000` holds data on the deck, and the four-pass survey
   found all 512 pages of the first 8 MB in use. Either a smaller reverb
   topology, or it slips again. Do not promise it.

Memory rule learned the hard way: a delay line may only be built on a block the
**deck** has walked and proved zero (`tools/fxmem_probe.py`), never on an
emulator-only reading.

## Wave 3 — the USB-stick workflow (highest risk, highest payoff)

The stick loader changes 9 bytes of Pioneer's file and boots in the emulator
with and without a pack, but **nobody has flashed it on a real deck**. 1.1 is
the release where that either happens properly or is postponed again:

1. Write the recovery path down first (official 1.15 file on a second stick;
   the app's "Back to stock" build numbered higher than the deck reports).
2. Emulator proof: exhaustive pack present / absent / corrupt / half-written.
3. One careful first flash with a boot test, then a stick test.
4. Then the app's three-mode flow becomes real: cable, stick, update file.

Also here: the deck offers "MY SETTINGS DATA was found" on a stick carrying
`MYSETTING.DAT`, which fights the app's browse probe. Decide the policy
(decline by default) and neutralise it in `FolderDisk`.

## Wave 4 — the asks that need research

- **Track filter inside the deck's browse** (the community's #1). The XDJ-1000MK2
  has the feature and shares the browse module, so it is a port: 15 `DISPCMD
  FILTER` messages in its image, 0 in ours. Open item is naming the routines
  that emit them (`docs/TRACK_FILTER_1.1.md`). Wave 1 already gives users a
  filtered-playlist version of the same outcome, so this does not block the
  release.
- **RGB waveform over Pro DJ Link** (Reddit, 2026-11-06). A deck loading the
  track over Link never sees the ANLZ colour data, so it stays stock blue. The
  likely path is the media API's type-4 reader `0x08C95760`, the same dispatch
  the LINK re-entrancy work maps. Needs: prove which reader a linked track
  takes, then either serve the colour data to the peer or read the peer's own
  copy. **2026-11-08: the dispatch is decoded.** The generic read
  `0x08DDFC96` takes the media handle's byte at `+17`, does
  `r0 = (handle[17] | 0x20) * 12`, and reads a u16 type from a 12-byte
  descriptor table at `0x0C8D8028`; `cmp/eq #4` then tail-jumps to
  `0x08C95760`, type 3 goes to `0x08C61A6C` when `handle[2] == 2` (otherwise
  `0x08D617E4`, the same target as type 6), type 5 to the `0x08DDFE2E` path
  and type 7 to `0x08DDFE18`. So `0x08C95760` is the type-4 reader exactly as
  suspected; what is still missing is which device byte a *Link* handle
  carries. Reading `0x0C8D8028` in `cache/emu_flush_playing.pkl` returns
  `0x6666` filler, so that table is not filled by the point in boot that
  checkpoint captures (or for that device set); reading it in a session that
  has a Link peer, or finding the code that writes handle[+17], is the next
  concrete measurement.
- **Waveform colours that follow the theme — emulator-passed 2026-11-12.**
  The theme switcher now rewrites the blue overview, PERFORM strip and centre
  palettes and the 3-band overview colours on every theme change, and the
  centre's 3-band colours follow too (gate `tools/theme_wave_test.py`,
  CLAUDE.md §174). Needs a deck look. The older notes below are history.
   Physics first: the theme tables
  stop at `0x08F81800` and the waveform palettes sit at `0x0E736150` /
  `0x0E735FA8` (with copies at `0x08E6304C` / `0x08E631F4`), so today a theme
  cannot touch them. 2026-11-06 finding: patching all four changed nothing
  because the strip never repainted in those snapshots, so the overview
  palette address is still TBD (CLAUDE.md §49). Re-tested 2026-11-07 on a
  session that really draws (centre waveform on screen, pink palette written
  to all four addresses): **0 centre pixels and 0 overview pixels changed**,
  so the negative result was not a repaint artifact. **2026-11-08: neither
  regenerates nor bypasses -- the read was already redirected.** A PC watch on
  the painters' palette loads (`build/wave_palette_watch.py`, Python core)
  shows the PERFORM strip painter loading pool `0x08DC2A18` and the centre
  painter loading pool `0x08DC3D94`. In the stock image those hold
  `0x0E735FA8` and `0x0E736150`; in `cache/emu_flush_playing.pkl` they hold
  `0x081DB180` and `0x081DB100`, inside the RGB mod's cave. So the 2026-11-07
  test patched the stock palette while the painters read the mod's copy, which
  is exactly why nothing changed. The four addresses are still the right
  stock values; a theme has to recolour whatever the pool resolves to *at
  paint time* (or the test must use a stock, un-modded checkpoint). Doing it
  properly means adding
  the palette regions to the theme blob with a stock-value proof gate — data
  only, cosmetic failure mode — and accepting that the centre waveform can
  can be checked in the emulator after all — corrected 2026-11-07: it draws
the centre waveform whenever the saved session has a track loaded (the
gallery shots are emulator captures), and only looks blank on snapshots
parked before the load. So both the overview and the centre are testable
deck-free.
- **Faster emulator.** Measured 2026-10-26: 14.5 % of real time, both chips
  need about 7×. Plan and phases in `docs/EMU_REALTIME.md`. Nice to have, not
  a blocker.

## Order of work, and why

1. Wave 1 deck session (one evening, many checkboxes).
2. Wave 2 Pitch + Roll, then Reverb only if a memory home is proved.
3. Wave 3 flash decision, with the recovery path written before anything else.
4. Wave 4 in the order above; filter first because it is the loudest ask.

## Definition of done (per feature)

A feature ships in 1.1 only with all four:

1. an emulator gate (one of the 56 `tools/*_test.py`, or a new one);
2. a deck result written down, with the date, in the roadmap item;
3. the README, site and Reddit post saying the same true thing as the docs —
   no "deck-confirmed" wording without a deck;
4. a release-note line a DJ can act on.

## Not in 1.1

- Reverb, if the 52 KB block is the only home.
- Track filter inside the deck's own browse, if the routine hunt stalls — the
  playlist route ships instead.
- Any other deck model, streaming services, or DRM playback.
- Flash-free stick mods are *not* implied by Wave 3: the loader is one flash,
  once, and the recovery path is part of the deliverable.
