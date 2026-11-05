"""DspSound: audible playback from the REAL modelled DSP (XDJ_DSP_EMU path).

The real DSP produces stereo s16 audio through McASP2's data port
(emu/c6x/periph.py McAsp.out: 32-bit slot words alternating L, R at
44.1 kHz, each an MSB-aligned sample -- the top 16 bits). This bridge drains that stream into
the platform output (macaudio.make_output: CoreAudio on macOS, pygame.mixer
on Windows), so "press play" in the app plays the emulated deck out loud.
Pitch/mute controls pass through (mute only; pitch comes from the DSP
itself via the firmware's own tempo path).

Sub-real-time hosts (the norm today: the SH interpreter sustains only a
fraction of 500 MHz): the consumer measures production and switches to
BURST MODE below ~85% of real time -- whole-buffer takes only, so audio
arrives as clean passages at exact pitch separated by quiet holds, never
the music-and-silence-in-one-buffer machine gun. ~95%+ production plays
gapless (smooth mode). See tools/dsp_audio_diag.py for the harness.

attach(machine) -> DspSound or None (no dspemu = None).
sync() every ~0.1 s from the UI loop: opens the queue on first play,
tracks the production rate and keeps the consumer fed by advancing a
drained-frames counter.
describe() merges into the sound status the host UIs already read.
Note: asp1.out is a bounded list (keep ~= 60 s); the ring-drain counter
     assumes words keep arriving during play; on seek backward the
     counter clamps (no double-play).
"""
import atexit
import ctypes
import os
import threading
import time

import numpy as np

from . import macaudio

SR = 44100
_LPCM_S16LE = (1 << 2) | (1 << 3)          # signed integer | packed
SAMPLE_SHIFT = 16                           # sample = top 16 bits of a slot word
MODE_PLAYING = 5
# Sub-real-time production: the cosim sustains only a fraction of the
# deck's cycle rate (2026-10-24: ~11% on an M5 -- the SH interpreter is
# the wall; see tools/dsp_rate_bench.py). Playing whatever trickles out
# at the AudioQueue's own 44.1 kHz cadence mixes music and silence
# inside single buffers -- the machine-gun stutter. Instead the consumer
# switches to BURST MODE when measured production stays under ~85% of
# real time: takes only WHOLE buffers, so the backlog comes out as clean
# contiguous passages separated by quiet holds, and the deck's pitch is
# exact. Above ~95% production it returns to the smooth take-what's-there
# mode (gapless at 100%).
BURST_ENTER = 0.93
BURST_EXIT = 0.98
BURST_WINDOW = 0.5                          # s between production samples
# How much audio to bank before releasing a burst (seconds). At fraction f
# of real time, a burst lasts BURST_S and the following hold ~BURST_S/f:
# 0.25 s passages every ~2.3 s at 11%, near-seamless as f -> 1.
BURST_S = float(os.environ.get("XDJ_DSP_BURST_S", "0.25"))


def bank_seconds(rate):
    """Audio to bank before a burst, from the measured production rate.
    A fixed 0.25 s made a deck running at 70-90 % of real time (WAV/AIFF on
    a busy Mac) play ~2 s, gap, ~2 s, gap... -- the stutter the user heard
    on a plain AIFF (2026-11-01). A passage lasts bank/(1-rate): banking
    0.2/(1-rate) s gives ~3 s passages at 70 %, ~10 s at 85 %, ~22 s at 90 %
    (capped at 3 s of banked audio). Slow decks (MP3) keep BURST_S."""
    if rate <= 0.5:
        return BURST_S
    return min(3.0, max(BURST_S, 0.2 / max(0.03, 1.0 - rate)))


class DspSound:
    def __init__(self, machine):
        self.m = machine
        self.playing = False
        self.muted = False
        self.status = ""
        self.served = 0
        self.lock = threading.Lock()
        self.aq = None
        self.out = None             # the live output (AudioQueue / pygame)
        self.at = None
        self.bufs = []
        self._cb = None
        self._drained = 0            # asp1 output words already consumed
        self._open_failed = False
        self._nfr = 4096
        # -- burst mode (sub-real-time production; see above):
        self.burst = False           # gate engaged (whole-buffer takes only)
        self._releasing = False      # draining a banked burst
        self.prod_rate = 1.0         # measured words/s as a fraction of 2*SR
        self._prod_t = None          # last production sample (wall time)
        self._prod_w = 0             # len(asp1.out) at that sample
        self.holds = 0               # silent callbacks while the gate held
        # -- diagnostics (the 2026-10-16 machine-gun audit; all counters
        #    count plane-level gaps of the L/R interleaved stream):
        self.gaps = 0                # callbacks that found NO fresh audio
        self.half = 0                # callbacks with only a partial frame take
        self.stalls = 0              # consecutive gap callbacks (machine-gun)
        self.max_stall = 0
        self.primed = False          # DSP got a sync run before the first take
        self.lag = 0                 # words behind the producer at the last take
        self.dropped = 0             # words trimmed by the producer (drop-ack)
        self.trimmed_total = 0       # acknowledgement of asp1.out trims
        self.l_edge = 0              # L plane gaps seen since attach
        self.r_edge = 0              # R plane gaps seen since attach

    # -- state -------------------------------------------------------------
    def _asp(self):
        de = getattr(self.m.mem.dev, "dspemu", None)
        return getattr(de, "asp1", None) if de is not None else None

    def _mode(self):
        try:
            return self.m.mem.r32(0x0B6C4264)
        except Exception:
            return 0

    def _dsp_playing(self):
        de = getattr(self.m.mem.dev, "dspemu", None)
        try:
            return de is not None and de.bus.read(0x11837BF8, 4) == 2
        except Exception:
            return False

    # -- consumer ----------------------------------------------------------
    def _filler(self, nfr):
        """nfr stereo frames of s16le bytes for one output period."""
        out = np.zeros((nfr, 2), np.int32)
        asp = self._asp()
        took = 0
        held = False
        if asp is not None and self.playing and not self.muted:
            with self.lock:
                avail = max(0, len(asp.out) - self._drained)
                take_words = min(avail, nfr * 2)
                # BURST GATE (sub-real-time production): hold (emit silence)
                # until BURST_S of audio is backlogued, then release and
                # take WHOLE buffers until less than one remains. Partial
                # takes are what made sub-real-time playback a machine
                # gun: every buffer held some music and some silence,
                # L/R planes drifted. (The not-primed first-take
                # relaxation is smooth-mode only: in burst mode the hold
                # itself guarantees the first audible take is full.)
                burst_min = max(nfr * 2, int(bank_seconds(self.prod_rate) * 2 * SR))
                if self.burst:
                    if self._releasing:
                        if avail < nfr * 2:
                            self._releasing = False
                    elif avail >= burst_min:
                        self._releasing = True
                    if not self._releasing:
                        take_words = 0
                        held = True
                # stall cleanly on a partial take: enqueue the whole buffer
                # only when a FULL frame's worth arrived (fix for the
                # machine-gun pattern: half-takes left one channel stale
                # while the other advanced -- audible as a one-ear tick).
                if take_words >= nfr * 2 or (take_words > 0 and not self.primed
                                              and not self.burst):
                    raw = asp.out[self._drained:self._drained + take_words]
                    self._drained += take_words
                    a = np.array(raw, dtype=np.uint32).view(np.int32)
                    n = take_words // 2                      # whole frames
                    a = a[:n * 2]
                    lr = np.empty((n, 2), np.int32)
                    # McASP TDM slots alternate L, R; each slot word is a
                    # 32-bit MSB-aligned sample (the low 16 bits are 0 for
                    # 16-bit music). 2026-10-18: L used to be read from
                    # the LOW half = silence on the left ear.
                    lr[:, 0] = a[0::2] >> SAMPLE_SHIFT               # left
                    lr[:, 1] = a[1::2] >> SAMPLE_SHIFT               # right
                    out[:n] = lr
                    took = n
                    if n >= nfr:
                        # first WHOLE buffer: the producer is flowing --
                        # partial takes (the prime relaxation) end here
                        self.primed = True
                self.lag = max(0, len(asp.out) - self._drained)
        # -- gap bookkeeping (the plane-gap counters from the pain report)
        if self.playing and not self.muted:
            if took == 0:
                if held:
                    self.holds += 1         # expected in burst mode: quiet
                else:
                    self.gaps += 1
                    self.stalls += 1
                    self.max_stall = max(self.max_stall, self.stalls)
            else:
                if took < nfr:
                    self.half += 1
                if took < nfr and self.stalls:
                    # a partial take right after a stall: the R (top half)
                    # is what audibly leads; count a plane-edge hit on the
                    # channel that lagged (L = low half arrives first in the
                    # interleave, so a short take starves R specifically)
                    self.r_edge += 1
                self.stalls = 0
        self.served += nfr
        return (np.clip(out, -32768, 32767)).astype("<i2").tobytes()

    def _callback(self, user, aq, bufref):
        data = self._filler(self._nfr)
        b = ctypes.cast(bufref, ctypes.POINTER(macaudio._AQB)).contents
        ctypes.memmove(b.mAudioData, data, self._nfr * 4)
        b.mAudioDataByteSize = self._nfr * 4
        if self.at is not None:
            self.at.AudioQueueEnqueueBuffer(aq, bufref, 0, None)

    def _open_queue(self):
        if os.environ.get("XDJ_DECK_SOUND") == "0":
            self.status = "sound: disabled (XDJ_DECK_SOUND=0)"
            self._open_failed = True   # tests drive _filler directly
            return False
        # Prime the DSP before the first take: the pain report's root #2
        # (nothing written yet -> the prime reads zeros; the R plane
        # suffers first because it is the TOP half of each 32-bit word).
        de = getattr(self.m.mem.dev, "dspemu", None)
        if de is not None:
            try:
                de.sync()
            except Exception:
                pass
            self.primed = bool(de.asp1.out)
        else:
            self.primed = False
        # Portable output (CoreAudio AudioQueue, pygame.mixer on Windows).
        # The consumer cadence is identical on both: nfr frames per period.
        self.out = macaudio.make_output(self._nfr, self._filler)
        if self.out is None:
            self.status = f"sound: {macaudio.OUTPUT_ERROR}"
            return False
        self.aq = self.out                    # truthiness for describe()
        self.status = "sound: ready (real DSP)"
        return True

    # -- UI loop entry -----------------------------------------------------
    def sync(self):
        # Ask the DSP, not the SH mode word: its status 0x11837BF8 reads 2
        # while it plays (1 loading, 3 paused). 2026-10-18: the mode word's
        # bit 2 is set while PAUSED in the real-DSP line (load auto-plays at
        # mode 3; PLAY/PAUSE then pauses at mode 5, the DSP's counter stops
        # and its output ring loops) -- so the `mode & 4` test kept the
        # speakers shut during real playback and opened them on pause: the
        # "machine gun" users heard. Verified by matching the DAC output to
        # the source file (ncorr 1.0000 while BF8 == 2).
        playing = self._dsp_playing()
        if playing and self.aq is None and not self._open_failed:
            self._open_failed = not self._open_queue()
        if playing != self.playing:
            was = self.playing
            self.playing = playing
            asp = self._asp()
            if playing and asp is not None and not was:
                # RESUME/SKIP-INTO-PLAY: chase the producer instead of
                # hard-resetting the counter (hard resets were the audible
                # glitch root #1). Track lag; drain speeds catch up by
                # itself because take = min(avail, nfr).
                backlog = len(asp.out) - self._drained
                if backlog > 4096 * 2:
                    # only for a TRUE monster backlog (minutes muted):
                    # re-arm just behind the head, and acknowledge the skip
                    # in the gap counters
                    self._drained = max(0, len(asp.out) - 4096)
                    self.trimmed_total += backlog
            if self.aq is not None:
                self.status = ("sound: playing (real DSP)" if playing
                               else "sound: paused")
        # bounded capture list: if the producer wrapped (nothing new for a
        # long time), the drain counter would stall playback; reset then.
        asp = self._asp()
        if asp is not None and self._drained > len(asp.out) + self._nfr * 8:
            self._drained = max(0, len(asp.out) - self._nfr)
        # -- burst-mode hysteresis: sample the production rate every
        #    BURST_WINDOW seconds; hold-and-release under BURST_ENTER,
        #    smooth takes again once production is effectively real time.
        if asp is not None and playing:
            now = time.time()
            if self._prod_t is None:
                self._prod_t, self._prod_w = now, len(asp.out)
            elif now - self._prod_t >= BURST_WINDOW:
                dt = now - self._prod_t
                self.prod_rate = (len(asp.out) - self._prod_w) / dt / (2 * SR)
                self._prod_t, self._prod_w = now, len(asp.out)
                if self.burst and self.prod_rate > BURST_EXIT:
                    self.burst = False
                    self._releasing = False
                elif not self.burst and self.prod_rate < BURST_ENTER:
                    self.burst = True
                    self._releasing = False
                    self.holds = 0
        elif not playing:
            self._prod_t = None

    def set_mute(self, muted):
        self.muted = bool(muted)

    def describe(self):
        back = 0
        asp = self._asp()
        if asp is not None:
            back = max(0, len(asp.out) - self._drained)
        return {"on": self.aq is not None, "playing": self.playing,
                "muted": self.muted, "pitch": 0.0,
                "follows_firmware": True, "hold": None, "hold_duty": None,
                "status": self.status, "track": None,
                "burst": self.burst,
                "prod_rate": round(self.prod_rate, 3),
                "plane_gaps": {"gap_callbacks": self.gaps,
                               "partial_takes": self.half,
                               "stall_run": self.stalls,
                               "max_stall_run": self.max_stall,
                               "lag_words": back,
                               "primed": self.primed,
                               "skipped_words": self.trimmed_total,
                               "l_edge": self.l_edge,
                               "r_edge": self.r_edge,
                               "burst_holds": self.holds}}

    def close(self):
        out = getattr(self, "out", None)
        if out is not None:
            try:
                out.close()
            except Exception:
                pass
            self.out = None
            self.aq = None


atexit.register(lambda: SOUND[0] is not None and SOUND[0].close())


SOUND = [None]


def attach(machine):
    """The one DspSound for this machine (None when no real DSP)."""
    de = getattr(machine.mem.dev, "dspemu", None)
    if de is None:
        return None
    if os.environ.get("XDJ_DECK_SOUND") == "0":      # same kill switch as
        return None                                   # decksound (tests)
    if SOUND[0] is None or SOUND[0].m is not machine:
        if SOUND[0] is not None:
            SOUND[0].close()
        SOUND[0] = DspSound(machine)
    return SOUND[0]
