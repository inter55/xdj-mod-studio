/* ksh.c -- install real-time key shift into the XDJ-700's RUNNING audio chip
 * (TI C674x DSP) over the host port, WITHOUT resetting it.
 *
 * Why: the reload route (ksr.c) fails on the real deck in every context we
 * tried (0x101 or a hard wedge) -- the DSP's boot needs cold-boot state the
 * running deck no longer has. A reset is the dangerous step, so this route
 * has none. The SH already talks to the running DSP through the host port
 * (HPI) all the time (mailboxes); we use the same 32-bit HPIA/HPID accesses
 * the firmware uses (fw 1.15 FUN_08C37554: *0xB8040000 = addr,
 * *0xB80C0000 = data).
 *
 * What it does (one shot, from the display-flush hook, like ksr.c):
 *   1. refuse unless: request flag set, no track loaded, our SH-side words
 *      stock, no DMA channel streaming to/from the host port,
 *   2. interrupts masked (IMASK=15 only, short), HPIA saved,
 *   3. READ-ONLY CHECK: DSP DDR words at the call site and three other
 *      image offsets must equal the program copy in SH RAM (proves the
 *      host port reaches DDR and the program is stock); the landing area
 *      0xC005F4C0.. must be loader padding (all FF) or already our code,
 *   4. write ks.bin (+ zeroed state area) to 0xC005F4C0, read it back,
 *   5. only then flip ONE word: the call at DSP 0xC0008D64 (b 0xC001CBF4 ->
 *      b 0xC005F4C0), read it back,
 *   6. restore HPIA, unmask, unhook.
 * Revert ('KSH0') writes the stock call word back (same checks).
 * Probe ('KSHP') = steps 1-3 only, writes nothing to the DSP.
 *
 * Cache note: the DSP program never enables L2 cache or the MAR bits (only
 * L1PCFG/L1DCFG = 7 at 0xC004F100), so DDR is uncached for data and the
 * only stale copy of the call word can sit in the 32 KB direct-mapped L1P,
 * which the audio loop cycles through constantly. The landing area was
 * never fetched as code, so it can't be stale anywhere.
 *
 * Status (ksh_status): 1 installed; 5 reverted; 6 probe ok; 2 track loaded;
 * 3 SH-side not stock; 7 DSP memory check failed (nothing written);
 * 8 host-port DMA busy (flag kept, retried next frame, up to 600 frames);
 * 9 landing-area write did not read back (call word NOT touched);
 * 10 call word did not read back.
 * ksh_dbg: [0] HPIA saved, [1..4] DDR probe words, [5..8] expected,
 * [9] first landing word seen, [10] call word after, [11] HPIA after
 * restore, [12] SR before, [13] DMA busy retries, [14] ks length.
 */
typedef unsigned int u32;
typedef unsigned char u8;

#define POOL_FLUSH   0x08C92E20u
#define IMG          0x0800E770u     /* DSP program in SH RAM (== DDR 0xC0000000) */
#define DDR          0xC0000000u
#define IMG_LEN      0x0005F4C0u
#define CALL_OFF     0x00008D64u     /* DSP 0xC0008D64: b .S1 0xC001CBF4  */
#define CALL_OLD     0x0027D290u
#define LAND         (DDR + IMG_LEN) /* 0xC005F4C0, loader padding        */
#define LAND_END     0xC0060000u     /* runtime cos table starts here     */
/* Bytes of zeroed STATE written after the code. Key shift keeps the
 * generous 0x260; TOUCH FX builds with -DZSTATE=0x180 because its struct is
 * 0x148 and the extra room is what lets Echo+Flanger share the window. */
#ifndef ZSTATE
#define ZSTATE       0x260u
#endif
#define TRACK_LEN    0x0BA3C050u
#define HPIA_P2      0xB8040000u
#define HPID_P2      0xB80C0000u     /* fixed (no auto-increment) port    */
#define UNC          0xA0000000u

#define R32(a) (*(volatile u32 *)(a))

/* v2: the stretcher's two staging-address loads (mvk lo / mvkh hi pairs)
 * re-pointed at KS.pbuf (PBUF from ks.syms at build time) */
#ifndef PBUF
#error "build with -DPBUF=<KS address> (build_ksh.sh)"
#endif
static const u32 PADDR[4] = { 0xC001CD40u, 0xC001CD98u, 0xC001CE34u, 0xC001CE48u };
static const u32 POLD[4]  = { 0x3356B828u, 0x0308C168u, 0x01D6B828u, 0x0188C168u };
static u32 pnew(int i)
{
    u32 c = (i & 1) ? ((u32)PBUF >> 16) : ((u32)PBUF & 0xFFFFu);
    return (POLD[i] & ~(0xFFFFu << 7)) | (c << 7);
}

extern u32 ksh_prev, ksh_flag, ksh_status, ksh_tries;
extern u32 ksh_dbg[16];
extern const u8 ks_dsp[], ks_dsp_end[];
extern void ksh_entry(void);

static u32 get_sr(void) { u32 v; __asm__ volatile("stc sr,%0" : "=r"(v)); return v; }
static void set_sr(u32 v) { __asm__ volatile("ldc %0,sr" : : "r"(v) : "memory"); }

static u32 hrd(u32 a) { R32(HPIA_P2) = a; return R32(HPID_P2); }
static void hwr(u32 a, u32 v) { R32(HPIA_P2) = a; R32(HPID_P2) = v; }

/* any DMAC0/DMAC1 channel 0-5 enabled, not finished, touching CS6? */
static int hpi_dma_busy(void)
{
    static const u32 base[2] = { 0xFE008000u, 0xFDC08000u };
    static const u32 off[6] = { 0x20, 0x30, 0x40, 0x50, 0x70, 0x80 };
    for (int b = 0; b < 2; b++)
        for (int c = 0; c < 6; c++) {
            u32 r = base[b] + off[c];
            u32 chcr = R32(r + 0xC);
            if ((chcr & 3u) != 1u) continue;          /* DE=1, TE=0 = live */
            u32 s = R32(r) & 0x1FFFFFFFu, d = R32(r + 4) & 0x1FFFFFFFu;
            if ((s >> 26) == 6u || (d >> 26) == 6u)  /* 0x18000000.. CS6  */
                return 1;
        }
    return 0;
}

static u32 ks_word(u32 i)
{
    return ks_dsp[i] | (u32)ks_dsp[i + 1] << 8 | (u32)ks_dsp[i + 2] << 16 |
           (u32)ks_dsp[i + 3] << 24;
}

void ksh_hook(void)
{
    u32 req = ksh_flag;
    if (req != 0x4B534831u && req != 0x4B534830u && req != 0x4B534850u)
        return;                                   /* KSH1 / KSH0 / KSHP */
    u32 blen = (u32)(ks_dsp_end - ks_dsp);
    u32 newcall = (CALL_OLD & ~(0x1FFFFFu << 7)) |
                  ((((IMG_LEN - (CALL_OFF & ~31u)) >> 2) & 0x1FFFFFu) << 7);
    ksh_dbg[14] = blen;
    if (R32(POOL_FLUSH) != (u32)ksh_entry) {      /* reached via another mod's
                                                     chain: the pool is not ours
                                                     to restore */
        ksh_flag = 0; ksh_status = 3; return;
    }
    if (R32(TRACK_LEN) != 0) { ksh_flag = 0; ksh_status = 2; goto unhook; }
    if (R32(IMG + CALL_OFF) != CALL_OLD || LAND + blen + ZSTATE > LAND_END)
        { ksh_flag = 0; ksh_status = 3; goto unhook; }
    if (hpi_dma_busy()) {                         /* try again next frame */
        ksh_dbg[13] = ++ksh_tries;
        if (ksh_tries < 600u) { ksh_status = 8; return; }
        ksh_flag = 0; goto unhook;
    }
    ksh_flag = 0;

    u32 sr = get_sr();
    ksh_dbg[12] = sr;
    set_sr(sr | 0xF0u);                           /* IMASK=15, short */
    u32 hpia = R32(HPIA_P2);
    ksh_dbg[0] = hpia;

    /* 3. read-only check: host port sees the stock program in DDR */
    static const u32 probe[4] = { CALL_OFF, 0x00000000u, 0x00010000u, IMG_LEN - 4u };
    int ok = 1;
    for (int k = 0; k < 4; k++) {
        u32 want = R32(IMG + probe[k]);
        u32 got = hrd(DDR + probe[k]);
        if (k == 0 && got == newcall) want = newcall;   /* already installed */
        ksh_dbg[1 + k] = got;
        ksh_dbg[5 + k] = want;
        if (got != want) ok = 0;
    }
    for (int i = 0; i < 4 && ok; i++) {
        u32 w = hrd(PADDR[i]);
        if (w != POLD[i] && w != pnew(i)) { ok = 0; ksh_dbg[15] = PADDR[i]; }
    }
    u32 first = hrd(LAND);
    ksh_dbg[9] = first;
    if (ok && req == 0x4B534831u) {              /* install: landing must be
                                                     padding or ours */
        int pad = 1, ours = 1;
        for (u32 i = 0; i < blen; i += 4) {
            u32 w = hrd(LAND + i);
            if (w != 0xFFFFFFFFu) pad = 0;
            if (w != ks_word(i)) ours = 0;
        }
        /* 2026-10-18: an OLDER build of our code may sit here; with the
           jump stock (word 0 matched CALL_OLD) nothing executes it, and
           the area is loader padding (FF at boot, never used): replace */
        if (!pad && !ours && ksh_dbg[1] != CALL_OLD) ok = 0;
    }
    if (!ok) { ksh_status = 7; goto done; }
    if (req == 0x4B534850u) { ksh_status = 6; goto done; }        /* probe */

    if (req == 0x4B534830u) {                     /* revert */
        hwr(DDR + CALL_OFF, CALL_OLD);
        ksh_dbg[10] = hrd(DDR + CALL_OFF);
        ksh_status = ksh_dbg[10] == CALL_OLD ? 5u : 10u;
        for (int i = 3; i >= 0; i--) hwr(PADDR[i], POLD[i]);
        for (int i = 0; i < 4; i++)
            if (hrd(PADDR[i]) != POLD[i]) ksh_status = 10u;
        goto done;
    }

    /* 4. code + zeroed state, verified, BEFORE the call word */
    for (u32 i = 0; i < blen; i += 4) hwr(LAND + i, ks_word(i));
    for (u32 i = (blen + 7u) & ~7u; i < ((blen + 7u) & ~7u) + ZSTATE; i += 4)
        hwr(LAND + i, 0);
    for (u32 i = 0; i < blen; i += 4)
        if (hrd(LAND + i) != ks_word(i)) { ksh_status = 9; goto done; }

    /* 4b. stretcher input -> KS.pbuf (stretcher idle: no track loaded) */
    for (int i = 0; i < 4; i++) hwr(PADDR[i], pnew(i));
    for (int i = 0; i < 4; i++)
        if (hrd(PADDR[i]) != pnew(i)) {
            for (int j = 3; j >= 0; j--) hwr(PADDR[j], POLD[j]);
            ksh_status = 9; goto done;
        }

    /* 5. the one word that switches it on */
    hwr(DDR + CALL_OFF, newcall);
    ksh_dbg[10] = hrd(DDR + CALL_OFF);
    if (ksh_dbg[10] != newcall) {
        hwr(DDR + CALL_OFF, CALL_OLD);            /* put stock back */
        ksh_status = 10; goto done;
    }
    ksh_status = 1;

done:
    R32(HPIA_P2) = hpia;                          /* 6. leave HPI as found */
    ksh_dbg[11] = R32(HPIA_P2);
    set_sr(sr);
unhook:
    R32(POOL_FLUSH) = ksh_prev;
}
