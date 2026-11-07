# XDJ Mod Studio: XDJ-700 Mods

Open-source mods for the **XDJ-700** (firmware 1.15): RGB and 3-band
waveforms, live key shift, stems, 8 hot cues, beat jump pads, Touch FX, live
colour themes and more. Fully open source (MIT). There's also a Mac and
Windows app, **Mod Studio**, which prepares your tracks, designs themes,
runs your deck's firmware in a built-in emulator and installs the mods.

There are three ways to get the mods onto the deck:

- **Cable (the default).** Nothing is flashed: the mods are loaded into the
deck's working memory over USB through the deck's own service connection.
Switch the deck off and on and it is completely stock again.
- **USB stick.** Flash a small loader once, after which your normal rekordbox
stick carries the mods and no laptop is needed at the gig. See
[docs/USBLOADER.md](docs/USBLOADER.md).
- **Firmware update file.** A real Pioneer update with the mods baked in, so
they survive a power cycle. This one does write to the deck's flash and it
can brick the deck. Once a modded file is installed the deck reports a
higher version, so the<br>official 1.15 file can be refused as a downgrade:
the app's **Back to stock** builds a stock file numbered higher, which the
deck will accept. Keep both to hand.

> Independent community project, not affiliated with, endorsed by or
> supported by Pioneer DJ / AlphaTheta. "Pioneer DJ", "XDJ", "CDJ" and
> "rekordbox" are trademarks of their owners and are used only to describe
> compatibility. Use at your own risk.

## What works

**Tested on a real deck**

| Mod | Notes |
| --- | --- |
| RGB and 3-band waveforms | overview strip and centre display |
| Live key shift | PERFORM › KEY SHIFT, MASTER TEMPO on, tempo stays put |
| Live themes | pick on the deck: UTILITY › THEME; OFF restores the stock colours exactly |
| Live stems | PERFORM › STEMS mutes vocals / drums / bass / other, heard after ~30 s |
| Beat jump pads | PERFORM › BEAT JUMP, tap and swipe (1–64 beats) |
| Mod settings in UTILITY | hold MENU, end of the list: mods on/off, waveform style, vinyl brake, hot cue gate, theme (hardware-tested) |
| Vinyl brake length | UTILITY › VINYL BRAKE LENGTH, 0.5×–15×; the stop changes to the length you pick |
| FLAC playback | the deck's own decoder; a normal FLAC file lists, loads and plays |
| Phase meter | this deck's beats, following the playing track; with two decks linked the master row shows too |
| 8 hot cues | pages A–C / D–F / G–H, and you can set one from an empty pad while the track plays; page by **tapping** the HOT CUE tab (swiping across the pads does not page); the cues live on your stick, not the deck |

**Working in the emulator, not yet tested on a deck.** These run against the
deck's real firmware in the emulator, so they should work on a real deck,
but nobody has tried them on one yet:

| Mod | Notes |
| --- | --- |
| Faster stem mute (beta) | rewrites the audio the deck already buffered; < 1 s in the emulator |
| Ogg Opus playback | own decoder on the deck (FLAC is the confirmed one; Opus uses the same layer but nobody has played an Opus file on a deck) |
| Touch FX (beta) | PERFORM › TOUCH FX, an X/Y pad with Filter, Crush and Gate; needs MASTER TEMPO on and shares the DSP hook with live key shift, so one or the other. Echo, Pitch, Flanger, Roll and Reverb are drawn but not implemented |

## For DJs: Mod Studio

1. Download Mod Studio from Releases.
   - **Mac** (Apple Silicon, macOS 14+): `XDJ-Mod-Studio.zip`. The first
     time, right-click the app and choose Open.
   - **Windows** (64-bit Windows 10/11): `XDJ-Mod-Studio-Windows.zip`.
     Unzip it and run `XDJ Mod Studio.exe` (SmartScreen: More info → Run
     anyway). Every build is checked by CI on a clean Windows
     machine (the app starts, its pages load, the emulator engine loads).
     Testers have installed the mods on real decks from Windows over USB;
     our own hardware checks run on a Mac. Emulator sound on Windows
     shipped 2026-10-24 (pygame mixer; the Mac uses CoreAudio) -- if it is
     silent for you, set `XDJ_DECK_SOUND=1` and please report how it goes.
2. **Add your firmware.** Get the official XDJ-700 firmware 1.15 update file
   from Pioneer DJ's support site and give it to Mod Studio when it asks.
   It checks the exact file, then builds everything it needs in about
   3 minutes. This project never distributes Pioneer's firmware.
3. Try the mods in the emulator, then connect the deck over USB and click
   **Install**. Power-cycle the deck any time to go back to stock.
4. Optional: once you trust it, build the **USB-stick loader** or a
   **firmware update file** in the app so the mods survive a power cycle.

### Stems

Making stems **replaces the track under its own name**, so it keeps its cues,
its waveform and its place in your playlists. The original stays next to it as
`your track.wav.pre-stems`; rename it back to undo. WAV and AIFF are swapped as
they are; MP3, M4A and FLAC are written as WAV and their database row, their
hash-named analysis folder and the analysis path move with the file
(`tools/stems_replace.py`, which prints the plan and backs up the database
before it writes), so the deck still lists the track under ARTIST / TRACK.

Do not go looking for a stems copy under BROWSE › FOLDER: on 1.15 the row is
drawn but the knob does not open it (emulator, 2026-11-07; also reported on a
deck), and SEARCH searches the rekordbox database only, so a file with no
database entry can never appear there. If an earlier build left
`Title (STEMS).wav` beside a track, either run **Make stems** again with
replacement on, or point `tools/stems_replace.py apply` at that track.

If PERFORM › STEMS shows the mix only, or the KEY SHIFT buttons do nothing:

- **Key shift is an install option, not stock.** Tick **Live key shift** when
  you install, and put MASTER TEMPO on; with the mod left out the buttons have
  nothing driving them.
- **STEMS only mutes what has been separated.** The deck cannot split audio.
  Select the tracks in **Library** and press **Make stems** (needs ffmpeg and
  Demucs), then build the deck's stems file from **Stems sidecar › Build deck
  stems file**. On any other track the STEMS pads do nothing.

## For DJs: OneLibrary USB sticks made readable for the XDJ-700

The XDJ-700 only reads the old Device Library (`PIONEER/rekordbox/export.pdb`).
Sticks written by newer software — djay, Traktor (via the public OneLibrary
SDK) or recent rekordbox versions — carry the encrypted SQLite file
`PIONEER/rekordbox/exportLibrary.db` instead, which the deck ignores.

The bundled converter turns that OneLibrary database into the file the deck
reads (tracks, artists, albums, genres, labels, keys, colours, playlists with
folder tree + entries, browse columns and artwork paths — analysis data and
hot cues are not part of it; the deck will analyse on first play):

```bash
# Python 3.10+ with wheels available for macOS (incl. Apple Silicon) and Windows
python3 -m pip install sqlcipher3-wheels
python3 tools/onelibrary_to_pdb.py /Volumes/YOUR_STICK      # or any stick folder
```

It writes `PIONEER/rekordbox/export.pdb` next to `exportLibrary.db` and backs
up any export.pdb already there. It never modifies the original files. Pass
`--dry-run` to see what it would write, and run the test suite (add your own
stick with `ONELIBRARY_TEST_STICK=...`):

```bash
python3 tools/onelibrary_to_pdb_test.py
```

Fields the converter cannot fill (documented in detail in
[tools/onelibrary_to_pdb.py](tools/onelibrary_to_pdb.py), `_FIELDS_DOC`):
play count (the OneLibrary database resets `djPlayCount` to 0 while
rekordbox's own export keeps old counts), hot-cue/loop data, and a few
per-track id rows that rekordbox renumbers into historically freed slots on
its own re-exports (irrelevant to the deck — the link targets are the same).
`export.pdb` is the first open-source writer of this format ever, built from
crate-digger's documentation and verified table-by-table against real
rekordbox exports.

## For developers

```bash
python3 -m venv .venv && .venv/bin/pip install numpy pillow capstone pygame
brew install hidapi                    # libhidapi.dylib for the deck tools
sh tools/build_tic6x_objdump.sh        # C674x objdump; needs Xcode CLT
.venv/bin/python -m emu.fwhome setup /path/to/XDJ700_v1.15.UPD --dev
```

`--dev` also places the generated files at the paths the research tools
expect (`work/`, `cache/`, `studio/assets/`, `tools/data/`, all git-ignored).
Run Mod Studio from source with `.venv/bin/python -m studio.app`.

The deck tools need a `hid` module with `hid.Device`. `pip install hidapi`
installs cython-hidapi, which does not have it, so use the copy in `vendor/`:

```bash
PYTHONPATH="$PWD/vendor" .venv/bin/python tools/xdj700_usb_shell.py --list
```

`tools/build_tic6x_objdump.sh` builds the GNU objdump used for the audio DSP
(`work/tic6x/objdump`, ~5 minutes). `fwhome setup` needs it for the
"booting with the real audio chip" step and stops early with a clear message
if it is missing; the Xcode command-line tools (`xcode-select --install`)
supply `make` and a compiler.

On first launch macOS asks for **Local Network** permission. Denying it
changes nothing: Mod Studio serves its interface on `127.0.0.1` and reaches
the deck over USB, so neither path uses the local network.

Deck code is written in SH-4 assembly (`tools/shasm.py`) or C built with a
local `sh-elf-gcc` (`tools/build_sh_gcc.sh`). Every mod has an emulator test
that must pass before it touches hardware, for example:

```bash
.venv/bin/python tools/perform_pads.py emu
.venv/bin/python tools/hotcue8_test.py
.venv/bin/python tools/theme_switch.py check
.venv/bin/python tools/sh4test.py
```

### Layout

| Path | What |
| --- | --- |
| `emu/` | SH7724 emulator (C core + Python devices), deck UI, firmware home |
| `emu/c6x/` | TI C674x DSP emulator (work in progress) |
| `tools/` | mods, deck installers (`*_apply.py`), emulator tests, RE tools |
| `studio/` | Mod Studio (local web UI + server) |
| `mods/themes.py` | theme presets |
| `site/` | project website (GitHub Pages) |
| `docs/`, `*.md` | reverse-engineering notes (formats, protocols, addresses) |

### Safety rules (enforced by every deck tool)

- Writes only inside the deck's RAM window; the firmware-erase command never
  appears in any tool.
- Every byte is checked before it changes and read back after; anything
  unexpected stops the install.
- A read-only survey proves a mod's memory is empty on *your* deck first.
- Emulator before hardware, always.

## Firmware and copyright

No firmware, decoded firmware, emulator snapshots, screenshots of the
deck's screen or artwork lifted from it are in this repository. They are
all rebuilt on your own Mac from your own official update file
(`emu/fwhome.py`); see [SOURCES.md](SOURCES.md).

## Licence

MIT for the code in this repository (see [LICENSE](LICENSE)). It grants
nothing for Pioneer DJ / AlphaTheta firmware.
