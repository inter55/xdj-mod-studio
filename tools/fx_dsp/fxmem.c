/* fxmem.c -- ONE-SHOT, READ-ONLY map of the audio chip's DDR on a real deck,
 * small enough to finish inside a few seconds of shell time.
 *
 * Why this exists: the emulator says 0xC0073400..0xC0080000 is free, but the
 * first settled run on hardware found every word non-zero and stable there.
 * The emulator models the full 64 MB, so its zero is a real zero in the
 * emulated chip -- which means the deck's DSP has something loaded that the
 * emulator does not. Guessing is not good enough before installing a delay
 * line, so this run does three things at once:
 *
 *   1. LANDMARKS. Reads the DSP program start (must look like the same code
 *      the emulator runs: 000ED02A 0008C06A ...) and the cosine table at
 *      0xC0060000 (must be exactly 1.0f = 0x3F800000). If those are right,
 *      the host port is reading real, correctly addressed chip memory and a
 *      surprising answer elsewhere has to be believed.
 *   2. THE OLD BLOCK, both fast and settled, so the two read modes can be
 *      compared on hardware (a hammered port would disagree).
 *   3. A PAGE MAP of 0xC0000000..0xC0200000: 128 pages of 16 KB, 32 samples
 *      each, so the host can see which pages hold data and which are empty.
 *      A page with 0 samples is a candidate for a new delay line; nothing is
 *      written here, ever -- this is a read-only survey.
 *
 * Hook: the 2DG flush pool 0x08C92E20, exactly like key shift / TOUCH FX
 * (tools/keyshift_dsp/kshhook.S). It runs once, restores the pool to the
 * value the host left in sv_prev, and writes nothing to the DSP: the only
 * access is HPIA <- address, then a read of HPID. The 32-bit HPIA/HPID
 * accesses are the ones the stock firmware uses (fw 1.15 FUN_08C37554) and
 * the ones key shift has already driven on hardware (ksh.c hrd/hwr).
 *
 * Result words (sv_res, 168 reserved):
 *   [0] old block, settled non-zero     [1] old block, fast non-zero
 *   [2] old block first non-zero        [3] old block last non-zero
 *   [4..7] DSP program 0xC0000000 +0..+12 (landmark)
 *   [8..11] cos table 0xC0060000 +0..+12 (landmark: 0x3F800000 = 1.0f)
 *   [12] 1 = HPIA read back as written  [13] pages (128)
 *   [14] page size (16384)              [15] samples per page (32)
 *   [16..143] non-zero sample count per 16 KB page, from 0xC0000000
 *   [144] old block bytes (52,224)
 *   [145] page under full scan (host sets sv_page before arming)
 *   [146] words in that page (4,096)   [147] non-zero words found
 *   [148] first non-zero in the page  [149] last non-zero in the page
 *   [150..157] eight words from that page, page/8 apart
 *   [158] words equal to word 0 of the old block (filler signature)
 *   [159] words that are a float < 1.0 (audio signature)
 *   [160..167] eight words from the old block, block/8 apart
 *
 * The last three lines exist because a bare non-zero COUNT cannot be
 * interpreted: 13,056 of 13,056 non-zero could be a live delay line or an
 * unmapped window echoing one filler word back. Real audio floats are almost
 * always below 1.0 in magnitude and almost never all the same value, so the
 * two counts separate the cases without a second pass over the block.
 *
 * The page scan is a FULL word-by-word read, not the 32-sample map, because
 * "empty" has to mean empty before anything is put there.
 */
typedef unsigned int u32;

#define R32(a)       (*(volatile u32 *)(a))
#define POOL         0x08C92E20u
#define HPIA_P2      0xB8040000u
#define HPID_P2      0xB80C0000u
#define BLK_LO       0xC0073400u
#define BLK_HI       0xC0080000u
#ifndef FXMEM_BASE
#define FXMEM_BASE   0xC0000000u   /* map base; build_fxmem.sh sets it */
#endif
#define DDR_LO       FXMEM_BASE
#define PROG         0xC0000000u
#define COS_TAB      0xC0060000u
#define NPAGES       128u
#define PAGE_SIZE    16384u
#define PAGE_SAMP    32u
#define PAGE_STEP    512u
#define MAGIC        0x53565231u
#define SETTLE       300u
#define SAMP_N       8u
#define SAMP_STEP    ((BLK_HI - BLK_LO) / SAMP_N)

extern u32 sv_prev, sv_flag, sv_done, sv_res[168], sv_page;

static u32 get_sr(void) { u32 v; __asm__ volatile("stc sr,%0" : "=r"(v)); return v; }
static void set_sr(u32 v) { __asm__ volatile("ldc %0,sr" : : "r"(v) : "memory"); }

static u32 rd_fast(u32 a) { R32(HPIA_P2) = a; return R32(HPID_P2); }

static u32 rd_slow(u32 a)
{
    R32(HPIA_P2) = a;
    for (volatile u32 i = 0; i < SETTLE; i++)
        ;
    return R32(HPID_P2);
}

/* One pass over the block. eq/sm/samp are optional (0 = do not collect);
 * they ride along on the same reads, so the deck time does not change. */
static void scan(u32 (*rd)(u32), u32 *nz, u32 *first, u32 *last,
                 u32 *eq, u32 *sm, u32 *samp)
{
    u32 n = 0, f = 0, l = 0, e = 0, s = 0, w0 = 0, k = 0;
    u32 next = BLK_LO;
    for (u32 a = BLK_LO; a < BLK_HI; a += 4) {
        u32 v = rd(a);
        if (a == BLK_LO) w0 = v;
        if (v) {
            if (!n) f = a;
            l = a;
            n++;
        }
        if (eq && v == w0) e++;
        if (sm && (v & 0x7FFFFFFFu) < 0x3F800000u) s++;
        if (samp && a >= next && k < SAMP_N) {
            samp[k++] = v;
            next = a + SAMP_STEP;
        }
    }
    *nz = n; *first = f; *last = l;
    if (eq) *eq = e;
    if (sm) *sm = s;
}

void sv_hook(void)
{
    if (sv_flag != MAGIC)                 /* not armed: pass straight through */
        return;
    sv_flag = 0;

    u32 sr = get_sr();
    set_sr(sr | 0xF0u);
    u32 hpia = R32(HPIA_P2);

    /* 1. landmarks: same program, and the cosine table's 1.0f */
    for (u32 i = 0; i < 4; i++) sv_res[4 + i] = rd_slow(PROG + 4 * i);
    for (u32 i = 0; i < 4; i++) sv_res[8 + i] = rd_slow(COS_TAB + 4 * i);

    /* 2. the old block, settled then hammered */
    u32 nz, first, last;
    scan(rd_slow, &nz, &first, &last, &sv_res[158], &sv_res[159], &sv_res[160]);
    sv_res[0] = nz; sv_res[2] = first; sv_res[3] = last;
    scan(rd_fast, &nz, &first, &last, 0, 0, 0);
    sv_res[1] = nz;

    /* 3. page map: 32 settled samples per 16 KB page */
    for (u32 p = 0; p < NPAGES; p++) {
        u32 base = DDR_LO + p * PAGE_SIZE;
        u32 cnt = 0;
        for (u32 k = 0; k < PAGE_SAMP; k++)
            if (rd_slow(base + k * PAGE_STEP)) cnt++;
        sv_res[16 + p] = cnt;
    }

    sv_res[13] = NPAGES;
    sv_res[14] = PAGE_SIZE;
    sv_res[15] = PAGE_SAMP;
    sv_res[144] = BLK_HI - BLK_LO;

    /* 4. the candidate page, every word, so "empty" is proven not sampled */
    u32 pb = sv_page;
    u32 pnz = 0, pf = 0, pl = 0;
    for (u32 a = pb; a < pb + PAGE_SIZE; a += 4) {
        if (rd_slow(a)) {
            if (!pnz) pf = a;
            pl = a;
            pnz++;
        }
    }
    sv_res[145] = pb;
    sv_res[146] = PAGE_SIZE / 4;
    sv_res[147] = pnz;
    sv_res[148] = pf;
    sv_res[149] = pl;
    for (u32 i = 0; i < 8; i++)
        sv_res[150 + i] = rd_slow(pb + (PAGE_SIZE / 8) * i);

    R32(HPIA_P2) = hpia;
    sv_res[12] = (R32(HPIA_P2) == hpia);
    sv_done = 1;

    set_sr(sr);
    R32(POOL) = sv_prev;
}
