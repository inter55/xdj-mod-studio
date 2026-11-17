/* fx.c -- TOUCH FX for the XDJ-700's audio chip (TI C674x DSP).
 *
 * Same proven, staging-safe mechanism as the real-time key shift
 * (tools/keyshift_dsp/ks.c, section 95/122): it REPLACES the one call at
 * DSP 0xC0008D64 (b 0xC001CBF4 = the Master-Tempo block puller) and the
 * installer re-points the stretcher's two staging-address loads at FX.pbuf,
 * so the staging buffer 0x1182AD70 is only ever READ (writing it corrupts
 * the right channel on real hardware -- section 122). Every block we fill
 * pbuf from staging with the effect applied; the puller then feeds the
 * stretcher from pbuf. The puller's return (frames consumed) is passed
 * through UNCHANGED, so tempo, play position and the waveform stay exactly
 * where they would be -- FX changes timbre only, never rate.
 *
 * TOUCH FX requires MASTER TEMPO ON (this is the only path that runs the
 * puller; MT off uses the resampler at 0xC000FE88). It shares the hook and
 * the loader-padding home 0xC005F4C0 with key shift, so the two are
 * mutually exclusive (one installed at a time; the SH tabs enforce it).
 *
 * Parameters ride mailbox word 0x11837BD8 (BD8), which the SH firmware
 * pushes to the DSP every frame from SH RAM 0x09D24DD8 (the installer NOPs
 * the store that would overwrite it, exactly like key shift). Stock firmware
 * puts a tempo copy there which the DSP audio path ignores. Layout:
 *   bits 31..30 = 0b10  -> FX mode (0x80000000); any other tag = stock
 *   bit  29     = ON
 *   bits 28..26 = effect 0..7  (0 Filter, 1 Crush, 2 Gate, 3 Echo,
 *                               4 Flanger, 5 Pitch; 6..7 reserved) -- Echo and
 *                               Flanger share the one delay line, so the
 *                               two effects cost no extra memory
 *   bits 25..18 = X      0..255  (pad horizontal)
 *   bits 17..10 = Y      0..255  (pad vertical)
 *   bits  9.. 2 = DEPTH  0..255  (dry/wet)
 * OFF or a non-FX tag -> plain pass-through (bit-exact stock audio).
 */
typedef unsigned int u32;

#define B14          0x11806900u
#define SPEED        (*(volatile u32 *)(B14 + 316))             /* unused here */
#define BLOCKNO      (*(volatile unsigned short *)(B14 + 510))  /* 0..31      */
#define MAILBOX_BD8  (*(volatile u32 *)0x11837BD8u)
#define STAGE        ((const float *)0x1182AD70u)   /* 32 frame slots L,R */
#define STAGE_VALID  (*(volatile u32 *)0x1182AD5Cu) /* valid frames (<=32) */
#define PULLER       ((int (*)(void))0xC001CBF4u)
#define NFR 32
#define FX_TAG 0x80000000u

/* Echo delay line: the DDR block 0xC0073400..0xC0080000 (52,224 B). The
 * 0xC0062000 block the first survey liked is NOT free: with the engine
 * installed and the effect OFF it fills with the audio path's own data
 * (2059 non-zero words in its first 16 KB, swapped every block). This one
 * stays byte-zero through playback, emulator-measured; one deck read still
 * pending (docs/TOUCHFX_MEMORY_SURVEY.md). 6,500 stereo frames = 52,000 B
 * = 147 ms at 44.1 kHz, inside the measured zero run. */
/* Overridable at build time (tools/fx_dsp/build.sh FX_RING_BASE /
 * FX_RING_FRAMES): the fallback ring is the ONE proven-empty 16 KB page,
 * 0xC006C000..0xC0070000, which holds 4,096 frames = 92.9 ms. Same code,
 * shorter line -- the effects themselves do not change. */
#ifndef ECHO_BASE
#define ECHO_BASE   0xC0073400u
#endif
#ifndef ECHO_FRAMES
#define ECHO_FRAMES 6500u          /* 147.4 ms at 44.1 kHz */
#endif
#define ECHO_BUF    ((float *)ECHO_BASE)
#define ECHO_MINF   882u           /* 20 ms */

/* Reverb geometry must be known before struct fx (it holds the positions). */
#ifdef FX_REVERB
#define RV_NC 2                    /* combs                          */
#define RV_NT 2                    /* combs + allpasses, total taps   */
#endif

#define L1DWIBAR (*(volatile u32 *)0x01844030u)
#define L1DWIWC  (*(volatile u32 *)0x01844034u)
static void stage_inv(void)              /* see what DMA put in staging */
{
    L1DWIBAR = (u32)((const char *)STAGE - 32);
    L1DWIWC = (NFR * 2 + 8);
    while (L1DWIWC) ;
}

/* per-channel effect state */
struct chan {
    float lp, bp;          /* SVF lowpass / bandpass accumulators   */
    float held;            /* crush: last emitted sample            */
    float gph;             /* gate: phase 0..1 (shared via s->gph)  */
    u32   cnt;             /* crush: samples since last resample    */
};

struct fx {
    float pbuf[NFR * 2];   /* stretcher input (8-byte aligned: lddw) */
    u32 magic;
    u32 calls, active;
    u32 w;                 /* last param word seen                   */
    struct chan ch[2];     /* L, R                                   */    float gph;             /* gate phase, shared across channels  */
    u32 dpos;              /* echo/flanger: next write frame         */
    float flph;            /* flanger LFO phase 0..1                 */
#ifdef FX_REVERB
    u32 rp[RV_NT];          /* reverb: comb/allpass positions          */
#endif
    /* Pitch: the stretcher resampler's state (same mechanism as the
     * key-shift engine, tools/keyshift_dsp/ks.c). pk == 0 means the
     * latch did not arm for this chunk; pshift was never read and is
     * gone with the 32 B it cost. */
    float pk, pphi, pkinv;  /* pkinv = 1/pk = 2^(-semi/12) */
};
extern struct fx FX;       /* placed by the linker script            */

/* ---- decoded parameters ---- */
struct par { int eff, on; float x, y, wet; u32 xi; };
static void decode(u32 w, struct par *p)
{
    p->xi  = (w >> 18) & 0xFF;
    p->on  = (w >> 29) & 1;
    p->eff = (w >> 26) & 7;
    p->x   = (float)((w >> 18) & 0xFF) * (1.0f / 255.0f);
    p->y   = (float)((w >> 10) & 0xFF) * (1.0f / 255.0f);
    p->wet = (float)((w >> 2)  & 0xFF) * (1.0f / 255.0f);
}

#define EFF_PITCH 5u
#define EFF_ROLL   6u
#define EFF_REVERB 7u

/* ---- Roll: the echo line played as a stutter -------------------------
 * No beat clock is available here, so the slice length comes from the pad
 * (X = 20..147 ms); feedback is 1.0, which makes the slice repeat forever
 * instead of decaying. Same memory and the same code path as Echo, so the
 * only cost is the branch that disables the decay. Gate one in
 * tools/fx_dsp/build.sh with FX_ROLL=1.
 *
 * ---- Reverb: four combs and two allpasses inside that same line -------
 * 2,229 of the line's 6,500 frames, carved by fixed offsets, so no second
 * buffer is needed and Roll/Reverb/Echo/Flanger can never overlap in
 * memory. Additive in the line, so the state area only grows by the six
 * positions (24 B). FX_REVERB=1 builds it.
 */
#ifdef FX_REVERB
/* Two combs and one allpass, not the four-and-two first tried: those cost
 * 736 B of code and no arrangement of the cave holds that. This is 3 taps
 * (a short room, not a hall) and fits beside Roll. */
static const unsigned short rv_len[RV_NT] = { 1427, 1613 };
static const unsigned short rv_off[RV_NT] = { 0, 1434 };

/* one stereo frame of a small Schroeder reverb; commit=0 on the preview
 * pass, which must READ the line but never write it (same rule as Echo) */
static void rv_frame(float xl, float xr,
                                               float *ol, float *or_uint,
                                               unsigned int *rp, float fb,
                                               int commit)
{
    float a0 = 0.0f, a1 = 0.0f;
    for (int i = 0; i < RV_NC; i++) {
        unsigned int p = rp[i], ln = rv_len[i];
        unsigned int q = (p + 1u >= ln) ? 0u : p + 1u, a = (rv_off[i] + p) * 2u;
        float vl = ECHO_BUF[a], vr = ECHO_BUF[a + 1];
        rp[i] = q;
        if (commit) {
            ECHO_BUF[a]     = xl + vl * fb;
            ECHO_BUF[a + 1] = xr + vr * fb;
        }
        a0 += vl; a1 += vr;
    }
    for (int i = RV_NC; i < RV_NT; i++) {
        unsigned int p = rp[i], ln = rv_len[i];
        unsigned int q = (p + 1u >= ln) ? 0u : p + 1u, a = (rv_off[i] + p) * 2u;
        float vl = ECHO_BUF[a], vr = ECHO_BUF[a + 1];
        float yl = vl - 0.5f * a0, yr = vr - 0.5f * a1;
        rp[i] = q;
        if (commit) {
            ECHO_BUF[a]     = a0 + 0.5f * yl;
            ECHO_BUF[a + 1] = a1 + 0.5f * yr;
        }
        a0 = yl; a1 = yr;
    }
    *ol = a0; *or_uint = a1;
}
#endif  /* FX_REVERB */

/* Pitch is behind FX_PITCH: with FX_NO_RING it builds to 2,480 B of the
 * 2,496 B wall (2026-11-10: Q14 table replaces the polynomial, and two
 * redundant latch/phase checks are gone). Re-run the build gate after any
 * change here. See docs/TOUCHFX_MEMORY_SURVEY.md. */
#ifdef FX_PITCH
/* 2^(semi/12) for -11..+11 as Q14: 46 B of table, one indexed load for
 * each direction. The polynomial version was 128 B and two calls. Q14
 * error is under 1/16384 (~0.004 cent), far below the stretcher's own. */
static const short pktab[23] = { 8679, 9195, 9742, 10321, 10935,
    11585, 12274, 13004, 13777, 14596, 15464, 16384, 17358, 18390,
    19484, 20643, 21870, 23170, 24548, 26008, 27554, 29193, 30929 };
#define PK(x) ((float)pktab[x] * (1.0f / 16384.0f))

/* Resample the staging frames by k into the stretcher's buffer. Linear
 * interpolation, not the cubic ks.c uses: the cubic costs ~700 B of code
 * and this blob has a hard 2,496 B wall. The pitch is exact; the very top
 * of the spectrum loses a little on the backward shifts. noinline so the
 * loop exists once. Returns the last frame written. */
static inline __attribute__((always_inline)) void premix(struct fx *s, const float *x,
                                             float *o, int valid)
{
    /* Frames past `valid` are never consumed (the latch check guarantees
     * the stretcher's budget fits), so reads are simply clamped to the last
     * valid frame -- one loop instead of a loop plus a fill-in loop. */
    int vl = valid - 1;
    float k = s->pk, phi = s->pphi;
    for (int j = 0; j < NFR; j++) {
        float pos = phi + (float)j * k;
        int i = (int)pos;
        float t = pos - (float)i;
        if (i >= vl) { i = vl; t = 0.0f; }
        const float *a = x + 2 * i, *b = a + (i < vl ? 2 : 0);
        o[2 * j]     = a[0] + (b[0] - a[0]) * t;
        o[2 * j + 1] = a[1] + (b[1] - a[1]) * t;
    }
}
#endif  /* FX_PITCH */

/* one stereo frame through the current effect; mutates cs (2 chans) +
 * the shared gate phase. in/out interleaved L,R. */
static void frame(const struct par *p, struct chan *cs, float *gph, u32 *dpos,
                  float *flph, u32 *rp, int commit, const float *in, float *out)
{
    float wet = p->wet, dry = 1.0f - wet;
#ifdef FX_REVERB
    if (p->eff == EFF_REVERB) {              /* ---- Reverb ---- */
        float l, r;
        rv_frame(in[0], in[1], &l, &r, rp, 0.62f + p->x * 0.28f, commit);
        out[0] = dry * in[0] + wet * l;
        out[1] = dry * in[1] + wet * r;
        return;
    }
#endif
    if (p->eff == 2) {                       /* ---- Gate (chopper) ---- */
        /* X = rate (2..~40 Hz), Y = depth (closed level 1-y) */
        float rate = 2.0f + p->x * p->x * 38.0f;
        float ph = *gph + rate * (1.0f / 44100.0f);
        if (ph >= 1.0f) ph -= 1.0f;
        *gph = ph;
        float env = ph < 0.5f ? 1.0f : (1.0f - p->y);   /* 50% duty */
        out[0] = in[0] * (dry + wet * env);
        out[1] = in[1] * (dry + wet * env);
        return;
    }
    if (p->eff == 3 || p->eff == 4
#ifdef FX_ROLL
        || p->eff == EFF_ROLL
#endif
       ) {                              /* ---- Echo / Flanger / Roll: one ring ---- */
#ifdef FX_NO_RING
        /* No delay line in this build: the ring code is kept out with #else
         * rather than behind a runtime early-out. Measured 2026-11-07: the
         * compiler was already dropping it (the build is 1,696 B either
         * way), so this is housekeeping, not a saving -- it only keeps the
         * code honest if that early-out ever moves. */
        out[0] = in[0]; out[1] = in[1];
        return;
#else
        /* Shared so the engine stays inside its 2.8 KB window: at 2,624 B
         * of code the state area crossed 0xC0060000 (the key-shift cos
         * table) and the audio path died after 44 blocks.
         * Echo   X = time 20..147 ms, Y = feedback 0..0.75, DEPTH = mix.
         * Flanger X = rate 0.05..4 Hz, Y = depth 0..2 ms sweep, DEPTH = mix.
         * `commit` = 0 on the preview pass: every read is >= 44 frames
         * behind the write pointer, so a block never reads what it writes
         * and the preview can run without touching the line. No `%`: the
         * C6x compiler turns a 32-bit modulo into an `rcpsp` reciprocal
         * and the emulator produced garbage indices from it. */
        float df, fb;
        if (p->eff == 3
#ifdef FX_ROLL
            || p->eff == EFF_ROLL
#endif
           ) {
            /* ECHO_FRAMES - 1, not ECHO_FRAMES: at exactly one ring length
             * the read index lands on the frame being written, so the X=255
             * "longest echo" would be no delay at all. */
            df = (float)(ECHO_MINF +
                         p->xi * (ECHO_FRAMES - 1u - ECHO_MINF) / 255u);
#ifdef FX_ROLL
            /* Roll: the line repeats with almost no decay. Exactly 1.0 was
             * tried first and is wrong twice -- this topology SUMS, so the
             * slice grows without bound, and the DSP fell behind (play
             * position ran at 0.67x, emulator 2026-11-08). 0.95 repeats for
             * seconds, stays bounded, and is one constant, which is what
             * keeps Roll inside the wall. */
            fb = (p->eff == EFF_ROLL) ? 0.95f : p->y * 0.75f;
#else
            fb = p->y * 0.75f;
#endif
        } else {
            float rate = 0.05f + p->x * p->x * 3.95f;
            float ph = *flph + rate * (1.0f / 44100.0f);
            if (ph >= 1.0f) ph -= 1.0f;
            *flph = ph;
            float tri = ph < 0.5f ? ph * 4.0f - 1.0f : 3.0f - ph * 4.0f;
            df = 132.0f + (tri + 1.0f) * 0.5f * (p->y * 88.0f);
            fb = 0.45f;
        }
        u32 di = (u32)df, w = *dpos;
        float fr = df - (float)di;
        u32 r0 = w >= di ? w - di : w + ECHO_FRAMES - di;
        u32 r1 = r0 ? r0 - 1u : ECHO_FRAMES - 1u;
        float a0 = ECHO_BUF[r0 * 2], a1 = ECHO_BUF[r0 * 2 + 1];
        float d0 = a0 + (ECHO_BUF[r1 * 2] - a0) * fr;
        float d1 = a1 + (ECHO_BUF[r1 * 2 + 1] - a1) * fr;
        if (commit) {
            ECHO_BUF[w * 2]     = in[0] + fb * d0;
            ECHO_BUF[w * 2 + 1] = in[1] + fb * d1;
        }
        *dpos = (w + 1u >= ECHO_FRAMES) ? 0u : w + 1u;
        out[0] = dry * in[0] + wet * d0;
        out[1] = dry * in[1] + wet * d1;
        return;
#endif
    }
    for (int c = 0; c < 2; c++) {
        float x = in[c], y;
        if (p->eff == 1) {                   /* ---- Crush ---- */
            /* Y = bit depth 1..16, X = sample rate (hold 1..32) */
            float nb = 1.0f + p->y * 15.0f;
            float levels = 1.0f;
            {   int k = (int)nb; while (k-- > 0) levels *= 2.0f; }
            float step = 2.0f / levels;
            u32 hold = 1u + (u32)((1.0f - p->x) * 31.0f);
            if (cs[c].cnt == 0) {
                float q = x * (1.0f / step);
                int qi = (int)(q >= 0.0f ? q + 0.5f : q - 0.5f);
                cs[c].held = (float)qi * step;
                cs[c].cnt = hold;
            }
            cs[c].cnt--;
            y = cs[c].held;
        } else {                             /* ---- Filter (SVF LP) ---- */
            /* X = cutoff, Y = resonance */
            float f = 0.012f + p->x * p->x * 1.28f;      /* 0.012..1.29 */
            float q = 1.45f - p->y * 1.40f;              /* 1.45..0.05  */
            if (q < 0.05f) q = 0.05f;
            /* 2026-11-09: this filter form is only stable for
             * q < 2/f - f/2. With the cutoff wide open (X >= ~230) and low
             * resonance (Y <= ~90) it was NOT: the state ran to infinity,
             * the output was full-scale noise on both channels (emulator:
             * rms 32768/32768) and the garbage went into the deck's
             * stretcher -- the "freeze + high-pitched scream" reports.
             * 3.1 - 1.75 f stays >= 0.06 under that limit over the whole
             * range of f; the second check drops the state if it ever
             * leaves any sane range (also catches NaN). */
            float qm = 3.1f - 1.75f * f;
            if (q > qm) q = qm;
            float hp = x - cs[c].lp - q * cs[c].bp;
            cs[c].bp += f * hp;
            cs[c].lp += f * cs[c].bp;
            y = cs[c].lp;
            if (!(y * y < 4096.0f)) {      /* |y| >= 64, or NaN */
                cs[c].lp = cs[c].bp = 0.0f;
                y = x;
            }
        }
        out[c] = dry * x + wet * y;
    }
}

int fx_pull(void)
{
    struct fx *s = &FX;
    const float *x = STAGE;
    float *o = s->pbuf;
    if (s->magic != 0x46583031u) {           /* 'FX01' */
        s->magic = 0x46583031u;
        s->calls = s->active = s->w = 0;
        s->gph = 0.0f;
        for (int c = 0; c < 2; c++) {
            s->ch[c].lp = s->ch[c].bp = s->ch[c].held = 0.0f;
            s->ch[c].cnt = 0;
        }
#ifndef FX_NO_RING
        for (u32 i = 0; i < ECHO_FRAMES * 2u; i++) ECHO_BUF[i] = 0.0f;
#endif
        s->dpos = 0;
        s->flph = 0.0f;
#ifdef FX_REVERB
        for (int i = 0; i < RV_NT; i++) s->rp[i] = 0;
#endif
#ifdef FX_PITCH
        /* pk/pkinv are set by the latch; clearing pk keeps a stale ratio
         * from surviving a reinstall (state is zero-filled anyway). */
        s->pk = 0.0f; s->pphi = 0.0f;
#endif
    }
    s->calls++;
    stage_inv();
    u32 w = MAILBOX_BD8;
    s->w = w;
    struct par p;
    decode(w, &p);
    if ((w & 0xC0000000u) != FX_TAG || !p.on) goto stock;  /* pass-through */
    s->active++;

#ifdef FX_PITCH
    if (p.eff == EFF_PITCH) {                /* ---- Pitch (stretcher) ---- */
        /* Not a per-frame effect: pitch comes from the deck's own stretcher
         * driven the way the key-shift engine drives it (ks.c) -- resample
         * the staging frames by k, run the stretcher at speed/k, report the
         * ORIGINAL frames used. Pitch x k; tempo, play position and the
         * waveform stay where they were. k is latched at chunk starts only,
         * because that is where the puller fixes the chunk's frame budget. */
        /* STAGE_VALID is documented as <= NFR (32). The clamp used to
         * cost a 32-B fetch packet; the use site's valid<4 check and that
         * contract carry it. */
        u32 valid = STAGE_VALID;
        if (BLOCKNO == 0) {
            int si = (int)((p.xi * 23u) >> 8);   /* 0..22, no divide */
            float k = PK(si), ki = PK(22 - si);  /* 1/k: table symmetry */
            /* (16*sp/k + 2)*k + 3 < 32  <=>  16*sp + 2k < 29, sp = SPEED/2^20,
             * so 16*sp = SPEED >> 16 (rounded down: allow one frame less). */
            u32 sp16 = SPEED >> 16;
            if ((float)(int)sp16 + 2.0f * k < 28.0f) {
                s->pk = k; s->pkinv = ki;  /* staging can feed it */
            } else {
                s->pk = 0.0f;
            }
        }
        if (s->pk == 0.0f || valid < 4) goto stock;  /* staging can't feed it */
        float k = s->pk;
        premix(s, x, o, (int)valid);
        /* signed conversions: one instruction each on the C674x, while the
         * unsigned ones expand to ~100 B. SPEED (Q20, ~1.0) is far below 2^31.
         * Truncating instead of rounding is off by one Q20 unit (1e-6). */
        u32 sp0 = SPEED, sp1 = (u32)(int)((float)(int)sp0 * s->pkinv);
        SPEED = sp1;
        int ny = PULLER();
        if (SPEED == sp1) SPEED = sp0;       /* host may have updated it */
        float end = s->pphi + (float)ny * k;
        int nx = (int)end;
        if (nx > (int)valid) nx = (int)valid;
        s->pphi = end - (float)nx;
        /* no shift counter: 32 B the cave does not have */
        return nx;
    }
#endif  /* FX_PITCH */

    /* filter the whole block into pbuf from a COPY of the state, so the
     * real state can be advanced by exactly the frames the puller uses */
    struct chan tmp[2]; float gtmp = s->gph, ftmp = s->flph; u32 dtmp = s->dpos;
#ifdef FX_REVERB
    u32 rtmp[RV_NT];
    for (int i = 0; i < RV_NT; i++) rtmp[i] = s->rp[i];
#else
    u32 *rtmp = 0;                 /* no reverb state in this build */
#endif
    tmp[0] = s->ch[0]; tmp[1] = s->ch[1];
    for (int j = 0; j < NFR; j++)
        frame(&p, tmp, &gtmp, &dtmp, &ftmp, rtmp, 0, x + 2 * j, o + 2 * j);

    int used = PULLER();

    /* advance the real state by exactly `used` original frames */
    int n = used < 0 ? 0 : (used > NFR ? NFR : used);
    float scratch[2];
    for (int j = 0; j < n; j++)
        frame(&p, s->ch, &s->gph, &s->dpos, &s->flph,
#ifdef FX_REVERB
              s->rp,
#else
              rtmp,
#endif
              1, x + 2 * j, scratch);
    return used;

stock:                                       /* bit-exact pass-through */
    for (int i = 0; i < NFR * 2; i++) o[i] = x[i];
    return PULLER();
}
