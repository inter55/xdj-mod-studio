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
 *   bits 28..26 = effect 0..7  (0 Filter, 1 Crush, 2 Gate; 3..7 reserved)
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
    struct chan ch[2];     /* L, R                                   */
    float gph;             /* gate phase, shared across channels     */
    u32 pad;
};
extern struct fx FX;       /* placed by the linker script            */

/* ---- decoded parameters ---- */
struct par { int eff, on; float x, y, wet; };
static void decode(u32 w, struct par *p)
{
    p->on  = (w >> 29) & 1;
    p->eff = (w >> 26) & 7;
    p->x   = (float)((w >> 18) & 0xFF) * (1.0f / 255.0f);
    p->y   = (float)((w >> 10) & 0xFF) * (1.0f / 255.0f);
    p->wet = (float)((w >> 2)  & 0xFF) * (1.0f / 255.0f);
}

/* one stereo frame through the current effect; mutates cs (2 chans) +
 * the shared gate phase. in/out interleaved L,R. */
static void frame(const struct par *p, struct chan *cs, float *gph,
                  const float *in, float *out)
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
            float hp = x - cs[c].lp - q * cs[c].bp;
            cs[c].bp += f * hp;
            cs[c].lp += f * cs[c].bp;
            y = cs[c].lp;
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
    }
    s->calls++;
    stage_inv();
    u32 w = MAILBOX_BD8;
    s->w = w;
    struct par p;
    decode(w, &p);
    if ((w & 0xC0000000u) != FX_TAG || !p.on) {   /* stock pass-through */
        for (int i = 0; i < NFR * 2; i++) o[i] = x[i];
        return PULLER();
    }
    s->active++;

    /* filter the whole block into pbuf from a COPY of the state, so the
     * real state can be advanced by exactly the frames the puller uses */
    struct chan tmp[2]; float gtmp = s->gph;
    tmp[0] = s->ch[0]; tmp[1] = s->ch[1];
    for (int j = 0; j < NFR; j++)
        frame(&p, tmp, &gtmp, x + 2 * j, o + 2 * j);

    int used = PULLER();

    /* advance the real state by exactly `used` original frames */
    int n = used < 0 ? 0 : (used > NFR ? NFR : used);
    float scratch[2];
    for (int j = 0; j < n; j++)
        frame(&p, s->ch, &s->gph, x + 2 * j, scratch);
    return used;
}
