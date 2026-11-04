#!/usr/bin/env python3
"""deck_session.py [--mode rgb|3band|blue] [--skip-survey]

ONE command to put the proven mods back on a freshly booted deck (all
RAM-only; a power-cycle always returns the deck to stock):

  1. read-only survey of the snapshot area (SNAP .. +0x44000)
  2. RGB overview + snapshot code   (tools/rgb_overview_apply.py MODE,
                                     re-run until PASS: chunked, armed
                                     in its own fresh session)
  4. waits for the USB stick to be EJECTED (no track loaded), then
  5. RGB centre waveform            (tools/rgb_detail_apply.py, re-run
                                     until PASS; refuses if a track is
                                     loaded)
  5b stems version switching (tools/vredirect_apply.py, same gate)
  (5c MY SETTINGS rows: retired in 1.0, UTILITY has them all)
  .. (old)                          (tools/mysettings_apply.py, same gate:
                                     BROWSE > MENU: [MODS] [WAVEFORM]
                                     [BEAT JUMP] [KEY/STEMS])
  5u UTILITY rows                  (tools/utility_apply.py: the same mod
                                     rows at the end of hold-MENU UTILITY,
                                     which scrolls -- all rows on linked decks)
  5f 8 hot cue pages               (tools/hotcue8_apply.py, after the pads)
  5e theme switching               (tools/theme_switch_apply.py: survey,
                                   tables, then the entry word)
  5k real-time key shift           (tools/keyshift_hot_apply.py: read-only
                                     probe, then a live patch of the audio
                                     chip -- no restart, stick out; PERFORM
                                     > KEY SHIFT < > then shifts live, MASTER
                                     TEMPO ON)
  5d PERFORM pads                  (tools/perform_pads_apply.py: BEAT JUMP
                                     row + hot-cue swipe, the touchscreen
                                     pads; hooks armed with
                                     atomic LW after a self-test; needs the
                                     PERFORM screen CLOSED)

Each step is a separate tool process; the shell library enforces one
session at a time + a 30 s gap after each (long sessions wedge the
deck's USB). Stops at the first FAIL/timeout and says what to do.
"""
import argparse
import os
import subprocess
import sys
import time

PY = sys.executable
sys.path[:0] = [".", "tools"]
# one process, shared deck connection (see xdj700_usb_shell.SHARE_MAX_S);
# XDJ_SHARE_SESSION=0 = the old one-process-per-step behaviour
os.environ.setdefault("XDJ_SHARE_SESSION", "1")


def _script(path):
    """the packaged apps ship compiled tools only (tools/x.pyc, no .py)"""
    if path.endswith(".py") and not os.path.exists(path) and os.path.exists(path + "c"):
        return path + "c"
    return path


def _cmd(args):
    """command line that runs a tool script (packaged app: its own runtime)"""
    a = [_script(args[0])] + list(args[1:])
    return [PY, "--run-tool"] + a if getattr(sys, "frozen", False) else [PY] + a


def _inproc(args):
    """Run an installer script inside THIS process (shared deck session:
    xdj700_usb_shell XDJ_SHARE_SESSION). Returns (output, new opens)."""
    import contextlib
    import io
    import runpy
    import traceback
    import xdj700_usb_shell as S
    import fastload
    fastload.LAST_WROTE = False
    if os.environ.get("XDJ_HANG_DUMP"):              # debugging aid
        import faulthandler
        faulthandler.dump_traceback_later(int(os.environ["XDJ_HANG_DUMP"]),
                                          repeat=True, file=sys.__stderr__)
    o0 = S.OPENS
    buf = io.StringIO()
    argv = sys.argv
    args = [_script(args[0])] + list(args[1:])
    sys.argv = [args[0]] + list(args[1:])
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                runpy.run_path(args[0], run_name="__main__")
            except SystemExit as e:
                if e.code not in (None, 0):
                    print(e.code if isinstance(e.code, str) else f"exit {e.code}")
            except BaseException:
                traceback.print_exc()
    finally:
        sys.argv = argv
    return buf.getvalue(), S.OPENS - o0


def run(args, show=("RESULT", "FAIL", "REFUSED", "pacing", "Timeout",
                    "ZERO", "nonzero", "complete", "left", "track length",
                    "32-bit", "reusing", "fastload:", "short", "Error",
                    "rror:", "aborting")):
    if os.environ.get("XDJ_SHARE_SESSION") == "1":
        out, opens = _inproc(args)
    else:
        p = subprocess.run(_cmd(args), capture_output=True, text=True)
        out, opens = p.stdout + p.stderr, 1
    # one line per real deck connection: the Studio counts these for its
    # progress bar / time estimate (connections are the real cost: ~30 s pace)
    for _ in range(opens):
        print("  @session", flush=True)
    for line in out.splitlines():
        if any(s in line for s in show):
            print("   ", line.strip(), flush=True)
    return out


POWER = ' Switch the XDJ-700 OFF and ON, wait for its home screen, then install again (once).'


def bad(out):
    return any(s in out for s in ("FAIL", "Timeout", "Traceback",
                                  "found 0 shell"))


def until_pass(args, label, max_runs=40):
    for i in range(1, max_runs + 1):
        print(f"  {label}: session {i}", flush=True)
        out = run(args)
        if "RESULT: PASS" in out:
            return True
        if "REFUSED" in out:
            return "refused"
        if bad(out):
            return False
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="rgb", choices=["rgb", "3band", "blue", "custom"])
    ap.add_argument("--colours", default="",
                    help="--mode custom: 1-3 colours '#rrggbb,#rrggbb,#rrggbb' "
                         "(overview gradient bottom -> top, centre line -> peak)")
    ap.add_argument("--skip-survey", action="store_true")
    ap.add_argument("--no-versions", action="store_true",
                    help="(kept for compatibility, ignored)")
    ap.add_argument("--no-pads", action="store_true",
                    help="skip 5d (PERFORM pads)")
    ap.add_argument("--no-hotcues", action="store_true",
                    help="skip 5f (8 hot cue pages)")
    ap.add_argument("--no-stems", action="store_true",
                    help="skip 5g (live stems)")
    ap.add_argument("--no-opus", action="store_true",
                    help="FLAC only in 5h (no Ogg Opus decoder): much shorter")
    ap.add_argument("--no-flac", action="store_true",
                    help="skip 5h (FLAC playback)")
    ap.add_argument("--no-themes", action="store_true",
                    help="skip 5e (on-deck theme switching)")
    ap.add_argument("--brake", type=float, default=0,
                    help="also 5v: VINYL BRAKE length x this (0.5..20; "
                         "stock = 1; opt-in until proven on hardware)")
    ap.add_argument("--phasemeter", action="store_true",
                    help="also 5m: Type 1 phase meter (needs the PERFORM pads; "
                         "opt-in until proven on hardware)")
    ap.add_argument("--keyshift", action="store_true",
                    help="also 5k: real-time key shift (live patch of the audio "
                         "chip, no restart; opt-in until proven on hardware)")
    a = ap.parse_args()
    if a.mode == "custom" and not a.colours:
        sys.exit("--mode custom needs --colours #rrggbb[,#rrggbb[,#rrggbb]]")
    colour_args = ["--colours", a.colours] if a.mode == "custom" else []
    # public checkouts have no build/ output (gitignored): stage the
    # committed prebuilt deck modules so the *apply tools find them
    try:
        import shbuild
        shbuild.stage_prebuilt()
    except Exception:
        pass
    # 2026-10-12 lesson (session 5 freeze): every mod together must PLAY in
    # the emulator before anything goes on a deck. ~3 min.
    ckpt = os.environ.get("CKPT", "cache/emu_track_loaded.pkl")
    if not os.environ.get("XDJ_SKIP_PLAYTEST") and not os.path.exists(ckpt):
        # the play test needs a track-loaded emulator snapshot made from the
        # developer's own library; public installs don't have one (it ran,
        # crashed on the missing file and reported a "freeze").
        print("0/5 emulator play test skipped (no track-loaded snapshot on this Mac)",
              flush=True)
    elif not os.environ.get("XDJ_SKIP_PLAYTEST"):
        print("0/5 emulator play test with every mod installed (about 3 min)", flush=True)
        r = subprocess.run(_cmd(["tools/repro_freeze.py"]), capture_output=True, text=True)
        if "RESULT: PASS" not in r.stdout and "RESULT:" not in r.stdout:
            sys.exit("STOP: the emulator play test could not run - nothing installed.\n"
                     + (r.stdout + r.stderr)[-600:])
        if "RESULT: PASS" not in r.stdout:
            sys.exit("STOP: the mods freeze in the emulator - nothing installed.\n"
                     + r.stdout[-600:])
        print("    RESULT: PASS - the mods play together in the emulator", flush=True)
    sys.path.insert(0, "tools")
    import rgb_overview as RO
    t0 = time.time()

    print("0b  checking the deck: firmware 1.15, no earlier mods in memory", flush=True)
    out = run(["tools/deck_check.py"], show=("RESULT", "STOP", "differs", "re-pointed"))
    if "RESULT: PASS" not in out:
        stop = [l.strip() for l in out.splitlines() if l.strip().startswith("STOP")]
        sys.exit(stop[-1] if stop else "STOP: couldn't read the deck over USB." + POWER)

    if not a.skip_survey:
        print("1/5 read-only survey of the snapshot area")
        end = RO.SLOT5 + RO.SLOT5_MAX
        ranges = [f"{a0:x}:{min(0x10000, end - a0):x}"
                  for a0 in range(RO.SNAP, end, 0x10000)]   # 64 KB per range
        import json
        try:
            known = json.load(open(os.path.expanduser(os.environ.get(
                "XDJ_SURVEY_CACHE", "~/.xdj700_surveys.json"))))
        except Exception:
            known = {}
        seen = any(all(f"{int(r.split(':')[0], 16):#x}:{int(r.split(':')[1], 16):#x}" in v
                       for r in ranges) for v in known.values())
        # remembered on this deck: ONE spot-check session; else 64 KB/session
        for grp in ([ranges] if seen else [[r] for r in ranges]):
            out = run(["tools/deck_zero_survey.py"] + grp)
            if out.count("ALL ZERO") < len(grp) and "nonzero" not in out:
                # no answer (not data): the USB link hiccuped -- try once more
                print("    no clean answer from the deck - retrying that check once", flush=True)
                out = run(["tools/deck_zero_survey.py"] + grp)
            if out.count("ALL ZERO") < len(grp):
                tail = [l for l in out.strip().splitlines() if l.strip()][-3:]
                for l in tail:
                    print("    (deck said) " + l.strip()[:160], flush=True)
                sys.exit("STOP: safety check failed (memory not empty, or the USB "
                         "link dropped) - nothing installed." + POWER)
    print(f"2/5 waveform overview ({a.mode}) + snapshot routine")
    r = until_pass(["tools/rgb_overview_apply.py", a.mode] + colour_args, "overview")
    if r is not True:
        sys.exit("STOP: overview install failed - switch the deck off and on, then install again (once).")
    print("2b  read-only survey of the version-switch + settings code area")
    import vredirect as VR
    import mysettings as MSET
    import perform_pads as PP
    # ONE read session for both areas (was ~11 sessions x 30 s pacing)
    out = run(["tools/deck_zero_survey.py",
               f"{VR.VCAVE:x}:{MSET.MSEND - VR.VCAVE:x}",
               f"{PP.PC0:x}:{PP.PEND - PP.PC0:x}"])
    if ("ALL ZERO" not in out or out.count("ALL ZERO") < 2) and "nonzero" not in out.lower():
        print("    no clean answer from the deck - retrying that check once", flush=True)
        out = run(["tools/deck_zero_survey.py",
                   f"{VR.VCAVE:x}:{MSET.MSEND - VR.VCAVE:x}",
                   f"{PP.PC0:x}:{PP.PEND - PP.PC0:x}"])
    if "ALL ZERO" not in out or "nonzero" in out.lower():
        for l in [l for l in out.strip().splitlines() if l.strip()][-3:]:
            print("    (deck said) " + l.strip()[:160], flush=True)
        sys.exit("STOP: safety check of the mod area failed (memory not "
                 "empty, or the USB link dropped) - nothing more installed." + POWER)
    print("4/5 EJECT THE USB STICK from the deck now (no track loaded).")
    print("    Waiting for the deck to report no track ...", flush=True)
    while True:
        out = run(["tools/rgb_detail_apply.py"] + colour_args)
        if "REFUSED" not in out:
            break
        time.sleep(15)
    if "RESULT: PASS" not in out:
        print("5/5 centre waveform")
        r = until_pass(["tools/rgb_detail_apply.py"] + colour_args, "centre")
        if r is not True:
            sys.exit("STOP: centre install failed - switch the deck off and on, then install again (once).")
    print("5b  stems version switching (per-stem deck file) -- always on")
    r = until_pass(["tools/vredirect_apply.py"], "versions")
    if r is not True:
        sys.exit("STOP: version-switch install failed - switch the deck off and on, then install again (once).")
    # 1.0: no MY SETTINGS rows any more -- every mod setting lives in the
    # stock UTILITY list (5u); its state bytes are the same RAM (zero on a
    # fresh deck = mods on, stock brake, gate off)
    print("5u  UTILITY rows (hold MENU: MODS, WAVEFORM, BRAKE, GATE, THEME)")
    r = until_pass(["tools/utility_apply.py"], "utility")
    if r is not True:
        sys.exit("STOP: UTILITY rows install failed - switch the deck off and on, then install again (once).")
    if a.no_pads:
        print("5d  PERFORM pads skipped (not selected)")
    else:
        print("5d  PERFORM pads (keep the PERFORM screen closed)")
        r = until_pass(["tools/perform_pads_apply.py"], "perform")
        if r is not True:
            sys.exit("STOP: PERFORM pads install failed - switch the deck off and on, then install again (once).")
        if a.no_hotcues:
            print("5f  8 hot cue pages skipped (not selected)")
        else:
            print("5f  8 hot cue pages (PERFORM > swipe along the HOT CUE tab)")
            r = until_pass(["tools/hotcue8_apply.py", "--precheck"], "hotcues")
            if r is not True:
                sys.exit("STOP: hot cue pages install failed - power-cycle "
                         "the deck.")
        if a.no_stems:
            print("5g  live stems skipped (not selected)")
        else:
            print("5g  live stems (PERFORM > STEMS mutes / solos STEMS files)")
            r = until_pass(["tools/stems_apply.py"], "stems")
            if r is not True:
                sys.exit("STOP: stems install failed - switch the deck off and on, then install again (once).")
    if a.no_themes:
        print("5e  theme switching skipped (not selected)")
    else:
        print("5e  theme switching (UTILITY > THEME)")
        r = until_pass(["tools/theme_switch_apply.py", "--precheck"], "themes")
        if r is not True:
            sys.exit("STOP: theme install failed - switch the deck off and on, then install again (once).")
    if a.no_flac:
        print("5h  FLAC skipped (not selected)")
    else:
        print("5h  FLAC playback (keep the USB stick OUT until the end)")
        r = until_pass(["tools/flac_apply.py"] + (["--no-opus"] if a.no_opus else []), "flac")
        if r is not True:
            sys.exit("STOP: FLAC install failed - switch the deck off and on, then install again (once).")
    if not a.brake or a.brake == 1:
        print("5v  vinyl brake length skipped (opt-in: --brake X)")
    else:
        print(f"5v  vinyl brake length x{a.brake:g}")
        r = until_pass(["tools/brake_length_apply.py", "--scale", f"{a.brake:g}"],
                       "brake")
        if r is not True:
            print("    vinyl brake length not changed (everything else is "
                  "installed); tell Claude what it printed.")
    if not a.phasemeter or a.no_pads:
        print("5m  phase meter skipped (opt-in: --phasemeter, needs the pads)")
    else:
        print("5m  Type 1 phase meter (read-only area check, then install)")
        out = run(["tools/phasemeter_apply.py", "--survey"])
        if "SURVEY OK" not in out:
            print("    phase meter area not empty on this deck - skipped "
                  "(everything else is installed)")
        else:
            r = until_pass(["tools/phasemeter_apply.py"], "phasemeter")
            if r is not True:
                sys.exit("STOP: phase meter install failed - switch the deck off and on, then install again (once).")
    if not a.keyshift:
        print("5k  real-time key shift skipped (opt-in: --keyshift)")
    else:
        # 2026-10-18: NO-RESET route (tools/keyshift_hot_apply.py, ksh.c):
        # the reload route (keyshift_dsp_apply.py) failed on hardware in
        # every context; this one patches the running chip over the host
        # port after a read-only check, so the sound never stops.
        print("5k  real-time key shift (stick still OUT; no chip restart)")
        r = until_pass(["tools/keyshift_hot_apply.py", "--probe"], "keyshift probe")
        if r is not True:
            sys.exit("STOP: key-shift probe failed (read-only: nothing was "
                     "changed on the audio chip) - everything else is "
                     "installed; tell Claude what it printed.")
        r = until_pass(["tools/keyshift_hot_apply.py"], "keyshift")
        if r is not True:
            sys.exit("STOP: key shift install failed - power-cycle the deck "
                     "(everything installed before it is fine after a re-run).")
    if os.environ.get("XDJ_LIGHT") != "1":
        pass                     # light theme held back for 1.0 (XDJ_LIGHT=1)
    elif a.no_themes:
        print("5l  light add-on skipped (needs theme switching)")
    else:
        # the LIGHT theme's tables rode the normal 5e install; this adds the
        # rest of the screen (text ink, waveform window, clear fills) and
        # arms the pads' + phase meter's light palettes when they are in
        print("5l  light add-on (UTILITY > THEME > LIGHT)")
        out = run(["tools/lighttext_apply.py", "--survey"])
        if "SURVEY OK" not in out:
            print("    light area not empty on this deck - skipped "
                  "(everything else is installed)")
        else:
            r = until_pass(["tools/lighttext_apply.py"], "light")
            if r is not True:
                sys.exit("STOP: light add-on install failed - switch the "
                         "deck off and on, then install again (once).")
    print(f"DONE in {(time.time() - t0) / 60:.0f} min: insert the stick and "
          f"load a track. Waveform = {a.mode}; PERFORM has BEAT JUMP / "
          f"HOT CUE / KEY SHIFT / STEMS; hold MENU > UTILITY (scroll to "
          f"the end) has the mod settings.")
    print("If the stick was in the deck at any point during the install: "
          "eject it and put it back once, so the deck re-reads the file "
          "types (otherwise FLAC/Opus files still show 'unsupported').")


if __name__ == "__main__":
    main()
