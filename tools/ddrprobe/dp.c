/* dp.c -- READ-ONLY: how big is the XDJ-700 audio chip's main memory?
 * (2026-11-13, stems step 2: the firmware only ever uses DDR below
 * 0xC2000000 = 32 MB; the emulator models 64 MB. If the deck's chip really
 * has 64 MB, the top 32 MB is free room for the four stems.)
 *
 * One shot on the display-flush hook (like ksh.c's probe): IMASK=15 briefly,
 * HPIA saved, then for 16 offsets it reads the DDR word at 0xC0000000+off
 * and at 0xC2000000+off through the host port (the firmware's own 32-bit
 * HPIA/HPID accesses), HPIA restored, unhook. Writes NOTHING to the chip.
 *   32 MB part: the upper address wraps to the lower one -> every pair equal
 *              (the lower words are the DSP program: non-trivial values).
 *   64 MB part: the upper words are never written by the firmware -> they
 *              differ from the program words.
 * dp_status: 1 done, 2 skipped (host-port DMA busy for 600 frames).
 * dp_lo[16], dp_hi[16] = the words; dp_same = number of equal pairs. */
typedef unsigned int u32;
#define POOL_FLUSH 0x08C92E20u
#define HPIA_P2    0xB8040000u
#define HPID_P2    0xB80C0000u
#define R32(a) (*(volatile u32 *)(a))

extern u32 dp_prev, dp_flag, dp_status, dp_same, dp_tries, dp_hpia;
extern u32 dp_lo[16], dp_hi[16];
extern void dp_entry(void);

static u32 get_sr(void) { u32 v; __asm__ volatile("stc sr,%0" : "=r"(v)); return v; }
static void set_sr(u32 v) { __asm__ volatile("ldc %0,sr" : : "r"(v) : "memory"); }
static u32 hrd(u32 a) { R32(HPIA_P2) = a; return R32(HPID_P2); }

static int hpi_dma_busy(void)
{
    static const u32 base[2] = { 0xFE008000u, 0xFDC08000u };
    static const u32 off[6] = { 0x20, 0x30, 0x40, 0x50, 0x70, 0x80 };
    for (int b = 0; b < 2; b++)
        for (int c = 0; c < 6; c++) {
            u32 r = base[b] + off[c];
            if ((R32(r + 0xC) & 3u) != 1u) continue;
            u32 s = R32(r) & 0x1FFFFFFFu, d = R32(r + 4) & 0x1FFFFFFFu;
            if ((s >> 26) == 6u || (d >> 26) == 6u) return 1;
        }
    return 0;
}

void dp_hook(void)
{
    if (dp_flag != 0x44445031u) return;            /* 'DDP1' */
    if (R32(POOL_FLUSH) != (u32)dp_entry) { dp_flag = 0; return; }
    if (hpi_dma_busy()) {
        if (++dp_tries < 600u) return;
        dp_flag = 0; dp_status = 2; goto unhook;
    }
    dp_flag = 0;
    u32 sr = get_sr();
    set_sr(sr | 0xF0u);
    u32 hpia = R32(HPIA_P2);
    dp_hpia = hpia;
    u32 same = 0;
    for (u32 k = 0; k < 16; k++) {
        u32 off = k * 0x4000u + 0x100u;           /* inside the DSP program */
        dp_lo[k] = hrd(0xC0000000u + off);
        dp_hi[k] = hrd(0xC2000000u + off);
        same += dp_lo[k] == dp_hi[k];
    }
    dp_same = same;
    R32(HPIA_P2) = hpia;
    set_sr(sr);
    dp_status = 1;
unhook:
    R32(POOL_FLUSH) = dp_prev;
}
