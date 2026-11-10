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
    /* Pitch: the stretcher resampler's state (same mechanism as the
     * key-shift engine, tools/keyshift_dsp/ks.c). */
    u32 platch, pshift;
    float pk, pphi;
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

/* Pitch is behind FX_PITCH because it does NOT fit the cave yet: built in,
 * fx.bin is 2,912 B against the 2,496 B wall (the key-shift cosine table
 * starts at 0xC0060000), i.e. 416 B short even with the ring removed and
 * the code compacted. Shipping builds must not define FX_PITCH. See
 * docs/TOUCHFX_MEMORY_SURVEY.md, "Pitch, measured". */
#ifdef FX_PITCH
static float recipf(float k)                /* 1/k, k in [0.5, 2] */
{
    float r = k > 1.5f ? 0.5f : k < 0.75f ? 2.0f : 1.0f;
    for (int i = 0; i < 3; i++) r = r * (2.0f - k * r);   /* 1e-5 is plenty */
    return r;
}

/* 2^(semi/12) for -11..+11: the series of 2^t up to t^4, t = semi/12.
 * Worst-case error at t = +-0.917 is ~1.5 cents -- inaudible, and it costs
 * a few multiply-adds instead of a loop (the cave has 832 B to spare). */
static float pk_of(int semi)
{
    float t = (float)semi * (1.0f / 12.0f);
    return 1.0f + t * (0.6931472f + t * (0.2402265f +
                    t * (0.0555041f + t * 0.0096181f)));
}

/* Resample the staging frames by k into the stretcher's buffer. Linear
 * interpolation, not the cubic ks.c uses: the cubic costs ~700 B of code
 * and this blob has a hard 2,496 B wall. The pitch is exact; the very top
 * of the spectrum loses a little on the backward shifts. noinline so the
 * loop exists once. Returns the last frame written. */
static __attribute__((noinline)) int premix(struct fx *s, const float *x,
                                            float *o, int valid)
{
    int vl = valid - 1, last = 0;
    float k = s->pk, phi = s->pphi;
    for (int j = 0; j < NFR; j++) {
        float pos = phi + (float)j * k;
        int i = (int)pos, i1;
        if (i > vl) break;                   /* past the valid frames */
        i1 = i + 1 > vl ? vl : i + 1;
        float t = pos - (float)i;
        o[2 * j]     = x[2 * i]     + (x[2 * i1]     - x[2 * i])     * t;
        o[2 * j + 1] = x[2 * i + 1] + (x[2 * i1 + 1] - x[2 * i + 1]) * t;
        last = j;
    }
    for (int j = last + 1; j < NFR; j++) {   /* never consumed */
        o[2 * j] = o[2 * last]; o[2 * j + 1] = o[2 * last + 1];
    }
    return last;
}
#endif  /* FX_PITCH */

/* one stereo frame through the current effect; mutates cs (2 chans) +
 * the shared gate phase. in/out interleaved L,R. */
static void frame(const struct par *p, struct chan *cs, float *gph, u32 *dpos,
                  float *flph, int commit, const float *in, float *out)
{
    float wet = p->wet, dry = 1.0f - wet;
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
    if (p->eff == 3 || p->eff == 4) {   /* ---- Echo / Flanger: one ring ---- */
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
        if (p->eff == 3) {
            /* ECHO_FRAMES - 1, not ECHO_FRAMES: at exactly one ring length
             * the read index lands on the frame being written, so the X=255
             * "longest echo" would be no delay at all. */
            df = (float)(ECHO_MINF +
                         p->xi * (ECHO_FRAMES - 1u - ECHO_MINF) / 255u);
            fb = p->y * 0.75f;
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
            /* 1.0.8: this filter form is only stable for q < 2/f - f/2.
             * With the cutoff wide open (X >= ~230) and low resonance
             * (Y <= ~90) it was not: the state ran to infinity and the
             * output was full-scale noise on both channels, fed straight
             * into the deck's stretcher. 3.1 - 1.75 f stays >= 0.06 under
             * that limit over the whole range of f; the second check drops
             * the state if it ever leaves any sane range (also NaN). */
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
        s->platch = s->pshift = 0;
        s->pk = 1.0f; s->pphi = 0.0f;      /* FX_PITCH state, harmless when off */
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
        u32 valid = STAGE_VALID;
        if (valid > NFR) valid = NFR;
        if (BLOCKNO == 0) {
            float k = pk_of((int)(p.xi * 22u / 255u) - 11);
            float sp = (float)SPEED * (1.0f / 1048576.0f);
            if (sp > 0.0f && valid >= 4 &&
                (16.0f * sp * recipf(k) + 2.0f) * k + 3.0f < (float)NFR) {
                s->platch = 1; s->pk = k;    /* staging can feed the chunk */
            } else {
                s->platch = 0;
            }
        }
        if (!s->platch || valid < 4) goto stock;   /* staging can't feed it */
        float k = s->pk;
        premix(s, x, o, (int)valid);
        u32 sp0 = SPEED, sp1 = (u32)((float)sp0 * recipf(k) + 0.5f);
        SPEED = sp1;
        int ny = PULLER();
        if (SPEED == sp1) SPEED = sp0;       /* host may have updated it */
        float end = s->pphi + (float)ny * k;
        int nx = (int)end;
        if (nx > (int)valid) nx = (int)valid;
        s->pphi = end - (float)nx;
        if (s->pphi > 8.0f) s->pphi = 8.0f;  /* never run away */
        s->pshift++;
        return nx;
    }
#endif  /* FX_PITCH */

    /* filter the whole block into pbuf from a COPY of the state, so the
     * real state can be advanced by exactly the frames the puller uses */
    struct chan tmp[2]; float gtmp = s->gph, ftmp = s->flph; u32 dtmp = s->dpos;
    tmp[0] = s->ch[0]; tmp[1] = s->ch[1];
    for (int j = 0; j < NFR; j++)
        frame(&p, tmp, &gtmp, &dtmp, &ftmp, 0, x + 2 * j, o + 2 * j);

    int used = PULLER();

    /* advance the real state by exactly `used` original frames */
    int n = used < 0 ? 0 : (used > NFR ? NFR : used);
    float scratch[2];
    for (int j = 0; j < n; j++)
        frame(&p, s->ch, &s->gph, &s->dpos, &s->flph, 1, x + 2 * j, scratch);
    return used;

stock:                                       /* bit-exact pass-through */
    for (int i = 0; i < NFR * 2; i++) o[i] = x[i];
    return PULLER();
}
