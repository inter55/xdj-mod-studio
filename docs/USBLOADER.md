# USB stick loader

## 2026-11-02: why the app's USB "load" probe times out (measured)

`tools/app_usb_probe.py` reports MOUNT and BROWSE fine and then 11 rotary
pushes that never load (`mode=0`). Re-run and instrumented with
`scratchpad/usb_input_try.py` (screen hash + mode after every press):

| step | result |
| --- | --- |
| insert (his stick) | 5.3 s, disk ops 1725, FAT walk plateaus |
| DevUSB press | ops 1725 -> 5903 (DB read), artists listed (Aaron Carl, Alan Parsons, Avicii...) |
| Back | screen changes (modal closed) |
| TrackFwd x2 | **screen unchanged** |
| rotary +5 x2 | **screen unchanged** |
| RotaryPush | **modal screen returns** |
| Browse | back to the deck screen |
| MENU | UTILITY opens |

So mounting, the FAT walk and the rekordbox DB read all work, and key
events do reach the deck (Back / Browse / MENU all act). The blocker is
two-fold and neither is the USB stack:

1. On insert the deck raises the modal **"MY SETTINGS DATA was found. Push
   the MENU button to LOAD."** (his stick carries MYSETTING.DAT). Back
   closes it, but the next rotary push brings it back, so the browser never
   gets a stable list to act on.
2. With the modal out of the way the list still ignores TrackFwd and knob
   movement, so browse-list navigation after that prompt is the open
   question -- not the load handshake the probe is timing.

Next: (a) decide the app's policy for that prompt -- declining it keeps the
deck's/mods' settings intact and is the safe default; (b) teach the browser
path to omit or neutralise MYSETTING.DAT in `FolderDisk` so the emulator
never offers to load settings off the user's stick; (c) re-run the probe
turning the knob before pressing (a rotary press on a category row is not a
load on a real deck, so the probe's model of the interaction is also wrong).

## Design (unchanged)

1. Boot: the firmware's own MYSETTING.DAT reader at
   0x09890000 and the image's pool 0x08DE39A4 points at ld_open.
2. Stick mount (the deck's own USB/FAT stack): the MYSETTING.DAT reader
   calls open -> ld_open reads /XDJMODS/PACK.BIN, verifies, decodes each
   payload into its (empty, in-band) RAM home, then the stock open runs.
3. Arm (milestone 3): each good payload's hooks are chained exactly like
   the cable installers do.

In-image mods (waveform colours, UTILITY rows, PERFORM pads hooks) are
baked in the firmware as today and do not depend on the stick.

## Gates before any deck flash (the "guarantee")

1. Emulator boot A/B: loader boot with pack == permanent build's boot
   byte-for-byte (core_lockstep); with no pack == permanent build minus
   stick-borne mods; plain stock image still stock.
2. Emu: wrong stick, missing pack, truncated pack, bad CRC, top port vs.
   front port — all degrade to the no-pack boot, never a stop.
3. differ A/B screens + repro_freeze + c6x_test gates stay green.
4. Documented recovery first (flash official 1.15 file — same as the UPD
   method today; second stick holds it).
5. First deck flash: mains power, load pack BEFORE flashing so the very
   first boot proves the pack path, official recovery stick in pocket.

## App surfaces

- Mod tab, "File on your USB stick" card: flips from COMING SOON to
  shipping when the packer + loader are proven.
- UPD tab: themes + FLAC/Opus move from "Can't be baked" to "ships with
  the stick loader" once builders exist.
- The one-time flash warning stack stays exactly the UPD tab's today.

## 2026-11-02: milestone 3 DONE in the emulator -- the mods file works end to end

- **Pack rev 2 = a recorded cable install.** tools/stick_pack.py runs the
  real installers (deck_session.py, the Mods page's options via
  studio install_args) on the mock deck with MOCK_WRITE_LOG, merges the
  logged RAM writes into runs and writes XDJMODS/PACK.BIN. First-touch runs
  carry a precondition (F_ZERO: a cave in a surveyed band reads zero;
  F_OLD: CRC of the stock bytes the installer found). One-shot flush caves
  (live key shift / TOUCH FX engine, the ksh cave) get an F_CALL record
  where the mock deck ran them, so the instant replay still runs them.
- **Loader (tools/usbloader/lc.c -DSTICK, ld.bin @0x09890000)**: phase 1
  checks the WHOLE table (window, never itself, every precondition) and
  writes nothing if one fails (retries at the next mount); phase 2 applies
  in recorded order, small runs (<= 64 B) as aligned word stores after a
  CRC check (no torn pool words), big runs in place + CRC; stops at the
  first bad body (later hook arms never applied); ocbwb + icbi per line.
- **Tests (all PASS):** tools/usbloader_pack2_test.py from a no-mods
  real-DSP deck: good (482/482 records, every byte == the cable install's
  end state except the phase meter's frame counter, key shift DSP patch
  live), badbody (stops exactly at the corrupted record), oldbyte (nothing
  written). permanent_build.py --stick-loader builds stock + loader only
  (9 bytes + boot copier); `--emu` boots it from reset and, with PACK=,
  mounts a stick with the pack: 482/482 applied.
- **App:** Mod > "File on your USB stick" page: (1) Write mods file to my
  stick (j_stick_pack, ~2.5-5 min), (2) Build loader update (j_stick_loader,
  same three acknowledgements as the UPD page, boot-tested with the stick's
  pack). Never flashes. Not yet tested on a real deck.

## 2026-11-03: re-proven with today's builds (before the first deck flash)

- Pack recorded from today's installers (`stick_pack.py record --mode rgb
  --phasemeter --keyshift`): 422 records, 285,828 bytes.
- tools/usbloader_pack2_test.py, all PASS:
  - good (with `AFTER=` the cable install's end state): every byte equal
    except `_pm_draws`; live key shift active.
  - badbody: stops at the corrupted record.
  - oldbyte: nothing written.
  - NEW truncated (a half-copied file): the 236 complete records are
    applied exactly; the cut-off record left partial bytes in an empty
    cave that nothing points at; nothing after it.
- Every big record that replaces non-zero-in-band memory replaces only
  zeros (42/42), so a cut-off big record can only leave junk in an unused
  cave.
- permanent_build.py --stick-loader from the official 1.15 file: built (35
  KB of flash slot left); `--emu` with the pack: 422/422 applied; without a
  pack: boots stock (loader in RAM, nothing else changed).
- Re-verified 2026-11-04 on the shipping tree, from the official 1.15 file:
  **9 bytes** of the firmware image change; the loader block lands in space
  the file records as zeroed (0x09890000..0x098993a8) and the compressed
  block comes out 35,266 B *smaller*; boot stub, block 0 and PANL identical
  to stock. Boot from reset, no pack: PASS (loader in RAM, open pool armed,
  overview hook stock). Boot from reset with a 185 KB pack: PASS, **1029 of
  1029 records** applied at mount, status 4. Neither run has been on a real
  deck; the only irreducible risk is losing power mid-flash, which is the
  same risk Pioneer's own updater carries.
- tools/usbloader_stick_test.py is OBSOLETE: it writes the first pack
  format, which today's loader rightly refuses at the header (4 of its 6
  cases "fail" for that reason only).

## 2026-11-06: pack re-recorded from tonight's stems build (emulator only)

The Nov 4 pack was recorded before the bounded stem-mute rewrite, so
`build/pack_verify.py` found 3,118 bytes of drift (the stems module and its
pools). It has been re-recorded from the current tree:

- `tools/stick_pack.py record --mode rgb --phasemeter --touchfx` (the same
  flags `j_stick_pack` passes) against the mock deck on
  `cache/emu_dsp_mounted6.pkl`.
- `build/stick/XDJMODS/PACK.BIN`: **324,581 B, 1,099 entries, 1,097 runs +
  2 one-shot calls, 308,821 B of writes**. Still under the loader's 1,200
  run cap.
- `build/pack_verify.py --pack PACK --after <after-install.pkl>`: table CRC
  OK, every byte equal except 7 bytes that are the deck's own live counters
  (phase-meter frame counter, key-shift flag/status), which it now reports
  separately rather than counting as drift.
- `tools/usbloader_pack2_test.py all`: PASS. good applies all 1,099
  records; badbody stops at the corrupted entry and leaves the later caves
  untouched; oldbyte refuses at the header (nothing written, every cave
  still empty); truncated applies the 298 complete records and nothing
  after.

### The recorder itself was broken, and is now fixed

The first attempt to re-record failed before touching the deck: `deck not
found over USB`, from `tools/stick_pack.py record` and from
`tools/mock/mock_install.py` alike, while running `deck_check` by hand
found the pretend deck fine. Cause: `tools/xdj700_usb_shell.py` put
`vendor/` (the app's real `hid` binding) at the front of `sys.path`
whenever it existed, so the child process imported the real binding no
matter what `PYTHONPATH` said, and with no deck plugged in that enumerates
nothing. It now skips that insert when `XDJ_MOCK_SOCK` is set (i.e. when
the caller is deliberately talking to the pretend deck). Real-deck runs
never set that variable, so their behaviour is unchanged.

The pack above was then recorded by the same path the app's "write the
mods file to your stick" button uses, end to end, with no workaround.

This is emulator proof only. The pack has not been replayed on a real deck
since the stems module changed.

## The pack built for the first time, 2026-11-03 (and two caps that were too low)

`tools/stick_pack.py record --mode rgb --phasemeter --touchfx` now produces
`build/stick/XDJMODS/PACK.BIN`: **321,988 B, 1,034 runs, 308,642 B of writes
(296,050 of them into caves)**. Two limits had to move first, and both were
real bugs rather than tuning:

- `tools/usbloader/lc.c` `MAXE` was **1024** runs per pack; a full install
  records **1,036**, so the recorder refused its own output. Now **1200**.
  The table lives in the loader's own state area (24 B a run); the loader
  still ends at `0x098993A8`, inside its free 64 KB at `0x09890000`.
- `tools/stick_pack.py` carried the same 1024 in `MAXE`; raised to match.

## Testing the file WITHOUT flashing (new)

The file on the stick does nothing on its own: stock firmware never looks
for it. It is read at every stick mount by the loader (`ld.bin`), which
normally arrives inside the flashed update. There is now a RAM path, so the
file can be tried on a real deck with nothing permanent:

    .venv/bin/python tools/usbloader_hot.py --check    # read-only
    .venv/bin/python tools/usbloader_hot.py            # write + arm the mount pool
    .venv/bin/python tools/usbloader_hot.py --revert   # pool back, loader left inert

It refuses unless the mount-open pool (`0x08DE39A4`) is stock and the cave
is empty, verifies every write, and arms the pool last. All six cases pass
against the mock deck: free -> install PASS -> already armed -> revert ->
"cave holds OUR loader" -> install re-arms. **Power-cycle = stock.**

Sequencing matters: the pack's first-touch runs require their caves to be
EMPTY, so the file only applies to a freshly booted deck. Testing it on top
of a cable install would (correctly) write nothing.

### Content check, 2026-11-03

`build/pack_verify.py` replays the pack in order (later runs overwrite
earlier ones, as the loader does) and compares every written address with
the mock deck's memory right after the install it was recorded from:

    1036 entries, table CRC OK, 1034 runs + 2 one-shot calls
    308,514 bytes replayed, 308,507 identical
    7 bytes differ, all written by the mods themselves once running:
      0x0979A154            phasemeter  pm_draws   (frame counter)
      0x098006A4/0x098006A8 keyshift    ksh_flag / ksh_status (the one-shot's
                            own flag + status, which the pack's call entries
                            set at the next flush anyway)

So the file carries the install's bytes; the only differences are runtime
state, not install content.

### FULL replay proven, 2026-11-03 (this started as an open question)

`tools/usbloader_stick_test.py` was written against pack rev 1
(`pack_builder.py`) and began failing at the header once the loader accepted
rev 2 only. It now builds rev 2 (via `stick_pack.build`) and gets further:

    [good] status DONE, calls=2, mask=0x3, bytes=0x2e87
    [good] exact={'pm': True, 'stems': True, 'hotcue8': False}

A Python model of the same replay (`stick_pack.apply_model`) applies all
three entries with 0 mismatches, so the pack is right and the loader is
applying only two of three legacy payloads — or the third is being applied
and then overwritten by the deck after the mount (0x09790000 is the theme
module's home now, so that test's map is stale either way).

**RESOLVED the same night.** The loader's state word at `ld_state+16` is
`lc_applied`, and after mounting the real pack it reads **0x40C = 1,036**
with status DONE and `bad`/`why` both 0 -- i.e. every entry, including the
two one-shot calls, was applied. `build/pack_replay_full.py` now judges on
that number instead of on a byte comparison, because the deck's own
modules write 32 bytes afterwards (counters such as `pm_draws`,
`ut_draws`, `hc_draws`, `tfx_draws`, the key-shift engine's flag/status
words, and chain pools the deck re-points itself). Reducing the wait after
the mount from 6 x 250M to 1 x 250M steps changes nothing, so those bytes
are post-mount activity, not a skipped write.

So the stick file is proven twice over: content (`build/pack_verify.py`:
308,507 of 308,514 bytes) and application (all 1,036 entries). It is still
untested on real hardware; the cable install remains the trusted path until
the RAM-loader path has been tried once on the deck.
