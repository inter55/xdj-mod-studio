# Deck session 2026-11-02 (user, real hardware)

Everything below was reported by the user at the deck on the night, or
measured in the emulator the same night. No item is inferred.

## Confirmed working on hardware tonight

- **TOUCH FX**: the tab opens on PERFORM and a finger on the pad changes
  the sound. Built effects are Filter, Crush and Gate (`FX_LIVE`); Echo,
  Pitch, Flanger, Roll and Reverb are drawn but inert ("COMING SOON") by
  design (docs/ROADMAP.md).
- **Phase meter**: runs on hardware.
- **Stem mutes much faster**: about **30 s down to about 10 s** ("its
  faster, maybe 10s"). This is a real gain but still far from the v1
  target of "well under 0.5 s" -- the remaining cost is the DSP cached
  block rewrite in ROADMAP.md.

## Bug found and fixed: the STEMS tab did nothing

Symptom (user): tapping STEMS on the deck did nothing at all -- the screen
stayed in BEAT JUMP.

Cause: the tab hit-test tested the KEY SHIFT hidden bit (FEATB bit 0)
*before* deciding which row was touched. KEY SHIFT is hidden whenever Live
Key Shift is not installed, which is always the case next to TOUCH FX
(same DSP slot), so every tap fell through before STEMS was considered.

Fix (`tools/perform_pads.py`, handler `pht2`, comment dated 2026-11-02):
decide the ROW first, then consult only that row's own bit -- KEY SHIFT
row -> bit 0, STEMS row -> bit 1. Same code size, so no other placement
moved; the one slot-scan hit is the known pool data.

Test case added to the PERFORM pads test: FEATB = KEY SHIFT hidden, then
tap the STEMS tab and expect `PST+2 == 3` (STEMS mode), while the KEY
SHIFT tab stays dead.

Live workaround used during the session (so the user could test at all):
install with every tab shown, and ignore KEY SHIFT -- pressing it is
inert/harmless because the FX engine lives in that slot. The proper fix
above means the workaround is no longer needed.

## One glitch seen, not yet explained

Loading a track **while drums were muted** produced "a little glitch" in
playback right after the load. Not reproduced deliberately; next session
should load with a stem muted on purpose and watch whether the mute
survives the load or the deck briefly plays the unmuted mix.

## Emulator: the deck really draws 5-7 frames a second

- User measured **7.2 fps** while playing; counting the display controller's
  buffer flips gave **5-7 swaps a second**, so the stream is not dropping
  frames.
- Studio host + mods image + one attached viewer: **105 frames in 40.8 s
  (2.6 fps)** at the idle screen.
- **QEMU 105 % of one CPU core**, Python server side 17 %: QEMU is the
  bottleneck, the capture path is not.
- `qemu-system-sh4 -accel help` -> `tcg` only (11.1.2); SH4 has no KVM/HVF
  and no multi-threaded TCG, so one core is the ceiling.
- Audio in the same runs: 0 underruns, 0 trims, `asrc +0.03 %`, fill
  ~17.6 k -- the earlier "fast + stuttery" audio was the CoreAudio buffer
  downsizing, fixed with `out.buffer-length=3840`.

So the emulator is slow, not stuttering: it executes the guest at roughly
1/7 real time and panel taps land on a deck that is behind. That is
emulator speed work (docs/EMU_REALTIME.md), not an app setting.

## Next deck session (in order)

1. STEMS tab with Key Shift hidden: one tap should switch tabs (regression
   check for tonight's fix).
2. Stem mute stopwatch, in seconds: still ~10 s or better.
3. Load a track with drums already muted, watch for the playback glitch.
4. Touch FX Gate only: confirm it audibly works on hardware (it is built
   and emulator-proven).

## Emulator proof for the STEMS-tab fix (2026-11-02, later)

`tools/perform_pads.py emu` -> **RESULT: PASS, TEST_EXIT:0**, zero failing
checks (`build/pads_test.log`). The new case is in the log:

- `[tab_ks_hidden]`  mode 0 -- KEY SHIFT tab dead while its bit is set.
- `[tab_st_only]`    mode 3 -- STEMS tab REACHED with KEY SHIFT hidden
  (this is the bug that made the deck tap do nothing).
- `[tab_ks_hidden2]` mode 3 -- a tap on the dead KEY SHIFT tab changed
  nothing.
- `[tab_ks_back]` / `[tab_st_back]` -- both tabs work again when the bits
  are cleared.

So the fix is emulator-proven; only the on-deck confirmation is left.
