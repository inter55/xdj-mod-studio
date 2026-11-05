/* fastcore.c -- SH-4 interpreter core for the XDJ-700 emulator, in C.
 *
 * A line-for-line port of emu/sh4.py + emu/sh4fpu.py (same semantics, same
 * memory map as emu/machine.py MachineMem._loc), so checkpoints move freely
 * between the two cores. Python keeps the peripherals: every access outside
 * RAM / flash / framebuffers / store queues / the P4 MMU registers calls
 * back into Python one byte at a time (as MachineMem does), and Python
 * delivers interrupts and ticks timers between slices (emu/fastcpu.py).
 *
 * Not handled here, by design (core_run returns FALLBACK and Python runs the
 * Python core for a while): MMU address translation (MMUCR.AT=1) and LDTLB.
 * The firmware runs with the MMU off.
 *
 * Build: cc -O2 -shared -fPIC -o _fastcore.so fastcore.c   (fastcpu.py does it)
 */
#include <math.h>
#include <setjmp.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define MD (1u << 30)
#define RB (1u << 29)
#define BL (1u << 28)
#define FD (1u << 15)
#define MB (1u << 9)
#define QB (1u << 8)
#define SB (1u << 1)
#define TB 1u

#define FB0 0x0E780000u
#define FB1 0x0E83B800u
#define FBSZ (800u * 480u * 2u)

enum { R_LIMIT = 0, R_IDLE = 1, R_SLEEP = 2, R_IO = 3, R_FALLBACK = 4 };

typedef uint32_t (*io_rd_t)(uint32_t addr);
typedef void (*io_wr_t)(uint32_t addr, uint32_t val);
/* sized variants: one Python crossing per access instead of one per byte.
 * Same bytes as n x byte accesses (the Python side loops identically);
 * only used for aligned accesses fully inside device space. */
typedef uint32_t (*io_rds_t)(uint32_t addr, uint32_t n);
typedef void (*io_wrs_t)(uint32_t addr, uint32_t val, uint32_t n);

typedef struct {
    uint32_t r[16];         /* current view: r[0..7] = active bank */
    uint32_t alt[8];        /* inactive R0-R7 bank */
    uint32_t pc, pr, gbr, vbr, mach, macl, sr, ssr, spc, sgr, dbr;
    uint32_t tra, expevt, intevt, tea, pteh, ptel, ptea, ttb, mmucr, ccr;
    uint32_t fpscr, fpul;
    uint32_t fr[32];        /* raw bits, fr[bank*16 + i] */
    uint64_t steps, cycles;
    uint32_t sleeping, low_is_ram;
    uint8_t *flash;
    uint32_t flash_len, pad0;
    uint8_t *ram0, *ram1, *fb0, *fb1, *misc, *sq;
    uint64_t fb_writes;
    io_rd_t io_rd;
    io_wr_t io_wr;
    /* everything through pad1 MUST match the pre-fastcore struct layout
     * exactly (older reference builds bind these fields by offset) */
    uint32_t idle_pc, idle_skip;
    uint32_t io_break, exc_n;       /* exc_n: entries in exc[] since drain */
    uint32_t exc[32][7];            /* spc, offset, code, tea, sr, r15, pr */
    uint32_t exc_lost, pad1;
    /* --- appended fields (fastcpu.py Core mirrors this layout; sizes
     * checked by core_sizeof). NEVER move these before pad1: the layout
     * above is shared with older reference builds. --- */
    io_rds_t io_rds;
    io_wrs_t io_wrs;
    /* CS6 DSP-bridge register file (mirror of SH7724.cs6; synced with the
     * Python device at run boundaries by fastcpu.py, which owns save/load).
     * Served here so the firmware's HPI polling never crosses to Python. */
    uint32_t cs6_addr, cs6_ctrl, cs6_run;
    uint64_t cs6_bulk;
    /* HPID upload log: (dsp_addr u32, value u32) pairs appended on every
     * completed 4-byte write to the CS6 data port 0x180C0000. Read back by
     * fastcpu.py. */
    uint32_t *hpid_buf;
    uint64_t hpid_n, hpid_cap;
    uint32_t hpid_acc, hpid_addr;
    uint8_t hpid_cnt;
    uint8_t hpid_pad[3];
    uint64_t hpid_bytes;
    /* cs6_py: route EVERY CS6 access to the Python device (a DSP model /
     * HLE / boot capture is attached there); C then holds no CS6 state. */
    uint32_t cs6_py, cs6_pad;
    /* --- appended fields (fastcpu.py Core mirrors this layout; sizes
     * checked by core_sizeof) --- */
    /* 2DG register-file mirror (emu/tdg.py TDG.reg keys are 16-bit offsets
     * into 0x04680000; only 0x000-0x0FF is mirrored, the rest crosses). */
    uint32_t tdg_reg[64];       /* words: (off & 0xFF) >> 2 */
    uint32_t tdg_ssr;           /* mirror of TDG.ssr */
    uint64_t tdg_dirty;         /* bit w set = tdg_reg[w] written since sync */
    uint64_t tdg_dirty_ssr;
    uint32_t tdg_py;            /* route ALL 2DG traffic to Python (rare) */
    uint32_t tdg_on;            /* mirror owns 0x00-0xFF (fastcpu.py syncs
                                   it around every Python 2DG touch) */
    /* --- appended fields (fastcpu.py Core mirrors this layout) ---
     * CS6/DSP co-simulation accelerator (XDJ_DSP_CS6C, 2026-10-24): the
     * firmware's HPI traffic during play is ~220k crossings per emulated
     * second (HPIA store + HPID load poll loops, the PCM pump) and each
     * one was a Python round trip + a DSP time-lock sync. With cs6_fast,
     * HPIA is served from this core's own mirror and HPID reads are
     * served straight out of the DSP's RAM regions (registered read-only
     * by fastcpu.py from dspemu.bus.ram -- the same bytearrays the C6x
     * core executes over, so the host sees the DSP's memory as it is,
     * at most one slice stale in DSP time). HPID/HPIC writes still cross
     * (fastcpu syncs dspdev.hpia from the mirror first; the mirror is
     * the single owner of HPIA while this core runs). */
    uint32_t cs6_fast;
    uint32_t dsp_region_n;
    struct { uint32_t base, len; uint8_t *ptr; } dsp_region[8];
    /* core_run hot-loop commit state (2026-10-24): cur_pc updated every
     * instruction (exact fault pc), run_commit every COMMIT iterations
     * (fault-path iteration reconstruction). */
    uint32_t cur_pc;
    uint32_t cur_pad;
    uint64_t run_commit;
    /* --- appended (fastcpu.py mirrors): DMAC register file shared with
     * Python (2026-10-26). sh7724.py's dev.dma channel dicts become views
     * onto these words while dmac_on, so C serves the firmware's CHCR
     * polls and plain stores; a CHCR store that would start a transfer
     * still crosses to Python (_dma_write runs it). Bases: 0 = 0xFE008000,
     * 1 = 0xFDC08000, 2 = 0xFE009000; regs sar, dar, tcr, chcr. */
    uint32_t dmac_on, dmac_pad;
    uint32_t dmac[3][12][4];
    uint32_t dmaor[3], dmaor_pad;
    /* --- appended: 2DG lists started from C (tdg_cfast, 2026-10-26).
     * LCOFS + the run counter mirror TDG.lcofs / TDG.runs (fastcpu syncs);
     * irq_cb raises TDG_INI (0) / TDG_TRI (1) in Python; tdg_cont hands a
     * list C cannot finish to the Python interpreter at (pc, ops). */
    uint32_t tdg_cfast, tdg_runs_c;
    int32_t tdg_lcx, tdg_lcy;
    uint32_t tdg_lc_dirty, tdg_pad3;
    void (*irq_cb)(uint32_t which);
    void (*tdg_cont)(uint32_t pc, uint32_t ops);
    /* --- appended: play-mode C event loop (2026-10-27, XDJ_EV_C).
     * While ev_on this core OWNS the device-side timers -- TMU (both
     * units), the LCDC frame counter and their interrupt latches -- plus
     * the INTC priority/mask mirror, the PORT (0x0405xxxx) register
     * mirror, the SCIF status reads and (hpid_wr) HPID word stores into
     * the DSP's RAM. Python keeps the authoritative device clock dev.cyc
     * and pushes it in after every mem.tick via core_ev_tick; slices are
     * bounded by core_ev_next so timer IRQs stay within one pass. NOT
     * cycle-exact by design (play mode only); exact mode never arms it. */
    uint32_t ev_on, ev_pad0;
    uint32_t tmu[2][3][4];       /* [unit][ch]{tcor, tcnt, tcr, acc} */
    uint32_t tstr[2];
    uint32_t cmt_str, cmt_csr, cmt_cnt, cmt_cor, cmt_acc; /* unused: the
                                   firmware never starts the CMT (cmstr
                                   writes land in the generic stub on both
                                   cores); kept for a future port */
    uint32_t lcdc_acc, lcdc_ldintr;   /* lo byte = LDINTR flags, hi = enables */
    uint32_t lcdc_frame_cyc, lcdc_frames;
    uint8_t  intc_ipr[0x28];     /* IPR bytes, 0x04080000+i */
    uint8_t  intc_imr[13];
    uint8_t  ev_lat;             /* latched IRQ bits (EV_*) awaiting delivery */
    uint8_t  ev_pad1[7];
    uint64_t ev_cyc;             /* device cycles ticked here */
    uint32_t port_on;            /* PORT 0x04050000-0x0405FFFF mirror owned */
    uint32_t scif_on;            /* SCFSR/SCFDR reads served from scif_rx[] */
    uint32_t scif_rx[3];         /* rx byte counts, 0..16 (Python keeps live) */
    uint32_t hpid_wr;            /* HPID word stores served into DSP RAM */
    uint32_t hint_bit;           /* HPIC.HINT (1 = asserted, pin reads 0) */
    uint8_t  *port;              /* 64 KB PORT mirror */
} Core;

/* ------------------------------------------------------------------ */
static jmp_buf g_jb;
static uint32_t x_off, x_code, x_tea, x_hastea;

static void raise_exc(uint32_t off, uint32_t code, int hastea, uint32_t tea)
{
    x_off = off; x_code = code; x_hastea = hastea; x_tea = tea;
    longjmp(g_jb, 1);
}
#define ILLEGAL() raise_exc(0x100, 0x180, 0, 0)

static void set_sr(Core *c, uint32_t v)
{
    if ((c->sr ^ v) & RB) {
        uint32_t t[8];
        memcpy(t, c->r, 32); memcpy(c->r, c->alt, 32); memcpy(c->alt, t, 32);
    }
    c->sr = v;
}
#define T() (c->sr & TB)
#define SETT(x) do { if (x) c->sr |= TB; else c->sr &= ~TB; } while (0)

static void log_exc(Core *c, uint32_t spc, uint32_t off, uint32_t code)
{
    if (c->exc_n < 32) {
        uint32_t *e = c->exc[c->exc_n++];
        e[0] = spc; e[1] = off; e[2] = code; e[3] = c->tea; e[4] = c->sr;
        e[5] = c->r[15]; e[6] = c->pr;
    } else {
        c->exc_lost++;
    }
}

static void enter_exc(Core *c, uint32_t off, uint32_t code, uint32_t spc,
                      int hastea, uint32_t tea)
{
    log_exc(c, spc, off, code);
    c->spc = spc;
    c->ssr = c->sr;
    c->sgr = c->r[15];
    c->expevt = code;
    if (hastea) c->tea = tea;
    set_sr(c, c->sr | MD | RB | BL);
    c->pc = c->vbr + off;
}

void core_enter_interrupt(Core *c, uint32_t level, uint32_t code)
{
    c->spc = c->pc;
    c->ssr = c->sr;
    c->sgr = c->r[15];
    c->intevt = code;
    set_sr(c, ((c->sr | MD | RB | BL) & ~0xF0u) | ((level & 0xF) << 4));
    c->pc = c->vbr + 0x600;
    c->sleeping = 0;
}

void core_set_sr(Core *c, uint32_t v) { set_sr(c, v); }

/* ---------------- memory map (mirror of MachineMem._loc) --------------- */
static inline uint8_t *loc(Core *c, uint32_t a, int *fb, int *ro)
{
    *fb = 0; *ro = 0;
    if (a < c->flash_len && !c->low_is_ram) { *ro = 1; return c->flash + a; }
    if (a - FB0 < FBSZ) { *fb = 1; return c->fb0 + (a - FB0); }
    if (a - FB1 < FBSZ) { *fb = 1; return c->fb1 + (a - FB1); }
    if (a - 0x0FFFD000u < 0x10000u) return c->misc + (a - 0x0FFFD000u);
    if (a - 0x08000000u < 0x10000000u) {
        uint32_t b = 0x08000000u | (a & 0x07FFFFFFu);
        if (b < 0x0C000000u) return c->ram0 + (b - 0x08000000u);
        return c->ram1 + (b - 0x0C000000u);
    }
    if (a - 0xE0000000u < 0x40u) return c->sq + (a - 0xE0000000u);
    if (a < 0x04000000u) return c->ram0 + a;
    return 0;
}

static uint32_t *mmu_field(Core *c, uint32_t base)
{
    switch (base) {
    case 0xFF000000u: return &c->pteh;   case 0xFF000004u: return &c->ptel;
    case 0xFF000008u: return &c->ttb;    case 0xFF00000Cu: return &c->tea;
    case 0xFF000010u: return &c->mmucr;  case 0xFF000014u: return &c->ptea;
    case 0xFF00001Cu: return &c->ccr;    case 0xFF000020u: return &c->tra;
    case 0xFF000024u: return &c->expevt; case 0xFF000028u: return &c->intevt;
    }
    return 0;
}

/* CS6 DSP bridge (physical 0x18000000, 1 MB window): control/status,
 * DSP-address latch, bulk FIFO (count only), data port. Byte-exact copy
 * of SH7724._cs6_read/_cs6_write. */
#define CS6BASE 0x18000000u
#define CS6SPAN 0x100000u

/* ------------------------- 2DG accelerator ---------------------------- */
/* Mirror of emu/tdg.py TDG: register file 0x000-0x0FF served in C from a
 * 64-word image, everything else (and the SCLR start handshake, which runs
 * the display list inside the Python device) crosses via io_wrs/io_rds.
 * Dirty words are pushed Python-ward by fastcpu.py at run boundaries.
 */
#define TDG_BASE 0x04680000u
#define TDG_SPAN 0x10000u

static uint32_t tdg_r8(Core *c, uint32_t a)
{
    uint32_t off = a - TDG_BASE;
    if (off < 0x100u) {
        uint32_t w = c->tdg_reg[off >> 2];
        return (w >> ((off & 3u) * 8u)) & 0xFFu;
    }
    return c->io_rd(a) & 0xFFu;             /* off-mirror: rare */
}

static void tdg_sclr(Core *c);
static void tdg_wr8(Core *c, uint32_t a, uint32_t v)
{
    uint32_t off = a - TDG_BASE;
    if (off >= 0x100u) {                    /* off-mirror: rare crossing */
        c->io_wr(a, v & 0xFFu);
        return;
    }
    uint32_t sh = (off & 3u) * 8u;
    uint32_t w = off >> 2;
    uint32_t m = 0xFFu << sh;
    uint32_t cur = c->tdg_reg[w];
    uint32_t new = (cur & ~m) | ((v << sh) & m);
    if (w == 2) {                           /* 0x08 SSCR: write-1-to-clear */
        c->tdg_ssr &= ~((v << sh) & m);
        c->tdg_reg[w] = new;
        c->tdg_dirty |= 1ull << w;
        c->tdg_dirty_ssr = 1;
        return;
    }
    c->tdg_reg[w] = new;
    c->tdg_dirty |= 1ull << w;
    if (w == 0 && (new & 1u)) {             /* SCLR start */
        if (c->tdg_cfast) {                 /* TDG.write: reg0 = 0, run */
            c->tdg_reg[0] = 0;
            tdg_sclr(c);
            return;
        }
        /* run the list in Python (side effects live there); fastcpu's
         * io_wr wrapper syncs the mirror both ways around tdg.write */
        c->io_wr(a, v & 0xFFu);
        c->io_break = 1;
        return;
    }
}

/* ------------------------------------------------------------------ */
static uint32_t cs6_rd8(Core *c, uint32_t a)
{
    if (c->cs6_py) {
        uint32_t off = a - CS6BASE;
        if (c->cs6_fast && off - 0x40000u < 4u)      /* HPIA: pure mirror */
            return (c->cs6_addr >> ((off - 0x40000u) * 8u)) & 0xFFu;
        return c->io_rd(a) & 0xFFu;
    }
    uint32_t off = a - CS6BASE;
    if (off < 4u)
        return (c->cs6_ctrl >> (off * 8u)) & 0xFFu;
    if (off - 0xC0000u < 4u) {
        uint32_t v = (c->cs6_addr == 0x1183FFF4u) ? 1u : 0u;
        return (v >> ((off - 0xC0000u) * 8u)) & 0xFFu;
    }
    return 0;
}

static void cs6_wr8(Core *c, uint32_t a, uint32_t v)
{
    uint32_t off = a - CS6BASE;
    if (c->cs6_py) {
        if (c->cs6_fast && off - 0x40000u < 4u) {    /* HPIA: pure mirror */
            uint32_t sh = (off - 0x40000u) * 8u;
            c->cs6_addr = (c->cs6_addr & ~(0xFFu << sh)) | ((v & 0xFFu) << sh);
            return;
        }
        c->io_wr(a, v & 0xFFu);
        return;
    }
    if (off < 4u) {
        uint32_t sh = off * 8u;
        c->cs6_ctrl = (c->cs6_ctrl & ~(0xFFu << sh)) | ((v & 0xFFu) << sh);
        return;
    }
    if (off - 0x40000u < 4u) {
        uint32_t sh = (off - 0x40000u) * 8u;
        c->cs6_addr = (c->cs6_addr & ~(0xFFu << sh)) | ((v & 0xFFu) << sh);
        return;
    }
    if (off - 0x80000u < 0x100u) { c->cs6_bulk++; return; }
    if (off - 0xC0000u < 4u) {
        uint32_t sh = (off - 0xC0000u) * 8u;
        if (c->cs6_addr == 0x1183FFF0u) c->cs6_run = v & 0xFFu;
        c->hpid_bytes++;
        /* accumulate byte-split HPID writes into 32-bit words */
        if (c->hpid_cnt == 0) { c->hpid_addr = c->cs6_addr; c->hpid_acc = 0; }
        c->hpid_acc |= (v & 0xFFu) << sh;
        c->hpid_cnt++;
        if (c->hpid_cnt == 4) {
            c->hpid_cnt = 0;
            if (c->hpid_n == c->hpid_cap) {
                uint64_t nc = c->hpid_cap ? c->hpid_cap * 2 : 65536;
                uint32_t *nb = realloc(c->hpid_buf, nc * 8);
                if (!nb) return;
                c->hpid_buf = nb; c->hpid_cap = nc;
            }
            if (c->hpid_buf) {
                c->hpid_buf[c->hpid_n * 2] = c->hpid_addr;
                c->hpid_buf[c->hpid_n * 2 + 1] = c->hpid_acc;
                c->hpid_n++;
            }
        }
        return;
    }
}


/* ------------------------- DMAC register file (shared) ----------------- */
static inline int dmac_base(uint32_t p, uint32_t *off)
{
    uint32_t b;
    if (p - 0xFE008000u < 0x200u) b = 0;
    else if (p - 0xFDC08000u < 0x200u) b = 1;
    else if (p - 0xFE009000u < 0x200u) b = 2;
    else return -1;
    *off = p & 0x1FFu;
    return (int)b;
}
/* channel word for off (sh7724._dma_ch), or NULL */
static inline uint32_t *dmac_word(Core *c, int b, uint32_t off)
{
    uint32_t ch;
    if (off - 0x20u < 0x40u) ch = (off - 0x20u) >> 4;
    else if (off - 0x70u < 0x80u) ch = ((off - 0x70u) >> 4) + 4u;
    else return 0;
    return &c->dmac[b][ch][(off & 0xCu) >> 2];
}
/* read n bytes at off (aligned within one word); 0 = not handled */
static int dmac_rd(Core *c, uint32_t p, int n, uint32_t *v)
{
    uint32_t off; int b = dmac_base(p, &off);
    if (b < 0 || !c->dmac_on) return 0;
    uint32_t m = 0xFFFFFFFFu >> (32u - 8u * (uint32_t)n);
    if (off == 0x60u) { *v = c->dmaor[b] & m; return 1; }
    uint32_t *w = dmac_word(c, b, off);
    if (!w) return 0;
    *v = (*w >> (8u * (off & 3u))) & m;
    return 1;
}
/* write n bytes; 0 = not handled (cross to Python unchanged) */
static int dmac_wr(Core *c, uint32_t p, int n, uint32_t v)
{
    uint32_t off; int b = dmac_base(p, &off);
    if (b < 0 || !c->dmac_on) return 0;
    uint32_t sh = 8u * (off & 3u), m = (0xFFFFFFFFu >> (32u - 8u * (uint32_t)n)) << sh;
    if (off - 0x60u < 4u) {                       /* DMAOR, 16-bit RMW */
        sh = 8u * (off - 0x60u); m = (0xFFFFFFFFu >> (32u - 8u * (uint32_t)n)) << sh;
        c->dmaor[b] = ((c->dmaor[b] & ~m) | ((v << sh) & m)) & 0xFFFFu;
        return 1;
    }
    uint32_t *w = dmac_word(c, b, off);
    if (!w) return 0;
    if ((off & 0xCu) == 0xCu) {
        /* CHCR: Python triggers a run after any byte store that leaves
         * DE=1, TE=0 in the low byte -- those stores cross unchanged */
        uint32_t lo = (off & 3u) == 0u ? v & 0xFFu : *w & 0xFFu;
        if ((lo & 3u) == 1u) return 0;
    }
    *w = (*w & ~m) | ((v << sh) & m);
    return 1;
}

/* ------------- play-mode event loop: TMU / LCDC / INTC / PORT ------------ */
/* Exact ports of sh7724.py's _tmu_tick, _lcdc_tick, cycles_to_next_event
 * (TMU + LCDC terms) and take_interrupt (these sources only), driven by
 * the Python-owned device clock. See the Core field comment. */
enum { EV_T00 = 0, EV_T01, EV_T02, EV_T10, EV_T11, EV_T12, EV_LCDC, EV_N };
static const struct { uint16_t code; uint8_t ipr, shift, imr, bit; }
    EV_SRC[EV_N] = {                    /* SOURCES in sh7724.py */
    {0x400, 0x00, 12, 4, 4}, {0x420, 0x00, 8, 4, 5}, {0x440, 0x00, 4, 4, 6},
    {0x920, 0x08, 12, 0, 4}, {0x940, 0x08, 8, 0, 5}, {0x960, 0x08, 4, 0, 6},
    {0xF40, 0x04, 8, 4, 0},
};
static const uint32_t tmu_div[8] = {4, 16, 64, 256, 1024, 4, 4, 4};

static void ev_tick(Core *c, uint64_t upto)
{
    if (upto <= c->ev_cyc) return;
    uint64_t d = upto - c->ev_cyc;
    c->ev_cyc = upto;
    uint64_t pcl = d / 10u;                 /* PCLK 50 MHz / CPU 500 MHz */
    for (int u = 0; u < 2; u++) {
        uint32_t tstr = c->tstr[u];
        for (int ch = 0; ch < 3; ch++) {
            if (!(tstr & (1u << ch))) continue;
            uint32_t *st = c->tmu[u][ch];   /* tcor, tcnt, tcr, acc */
            uint32_t div = tmu_div[st[2] & 7];
            uint64_t acc = st[3] + pcl;
            uint64_t n = acc / div;
            if (!n) { st[3] = (uint32_t)acc; continue; }
            st[3] = (uint32_t)(acc - n * div);
            uint64_t t = st[1];
            if (n <= t) { st[1] = (uint32_t)(t - n); continue; }
            uint64_t period = (uint64_t)st[0] + 1u;    /* 2^32 when all-ones */
            uint64_t rem = (n - t - 1) % period;
            st[1] = (uint32_t)(st[0] - rem);
            st[2] |= 0x100u;
            if (st[2] & 0x20u) c->ev_lat |= (uint8_t)(1u << (u * 3 + ch));
        }
    }
    c->lcdc_acc += (uint32_t)d;
    while (c->lcdc_acc >= c->lcdc_frame_cyc) {
        c->lcdc_acc -= c->lcdc_frame_cyc;
        c->lcdc_frames++;
        uint32_t en = (c->lcdc_ldintr >> 8) & 0x7Fu;
        if (en) {
            c->lcdc_ldintr |= en;
            c->ev_lat |= (uint8_t)(1u << EV_LCDC);
        }
    }
}

/* device cycles from ev_cyc until the next C-side event (timer underflow
 * or LCDC frame). ev_lat bits that could be delivered NOW force 0 so the
 * Python pass ends and delivery happens. */
uint64_t core_ev_next(Core *c)
{
    if (!c->ev_on) return 0xFFFFFFFF00000000ull;
    uint32_t imask = (c->sr >> 4) & 0xF;
    if (!(c->sr & BL)) {
        for (int i = 0; i < EV_N; i++) {
            if (!(c->ev_lat & (1u << i))) continue;
            uint32_t w = c->intc_ipr[EV_SRC[i].ipr] |
                         ((uint32_t)c->intc_ipr[EV_SRC[i].ipr + 1] << 8);
            if ((c->intc_imr[EV_SRC[i].imr] & (1u << EV_SRC[i].bit)) ||
                !((w >> EV_SRC[i].shift) & 0xF) ||
                (((w >> EV_SRC[i].shift) & 0xF) <= imask))
                continue;
            return 0;                       /* deliverable right now */
        }
    }
    uint64_t best = ~(uint64_t)0;
    for (int u = 0; u < 2; u++) {
        uint32_t tstr = c->tstr[u];
        for (int ch = 0; ch < 3; ch++) {
            if (!(tstr & (1u << ch))) continue;
            uint32_t *st = c->tmu[u][ch];
            if (!(st[2] & 0x20u)) continue;
            uint64_t pcl = ((uint64_t)st[1] + 1u) * tmu_div[st[2] & 7] - st[3];
            if (!pcl) pcl = 1;
            uint64_t cy = pcl * 10u;
            if (cy < best) best = cy;
        }
    }
    if ((c->lcdc_ldintr >> 8) & 0x7Fu) {
        uint64_t cy = c->lcdc_frame_cyc - c->lcdc_acc;
        if (cy < best) best = cy;
    }
    return best == ~(uint64_t)0 ? 100000u : best;
}

void core_ev_tick(Core *c, uint64_t upto_dev_cyc) { ev_tick(c, upto_dev_cyc); }

void core_ev_deliver(Core *c)
{
    if (!c->ev_on) return;
    while (c->ev_lat) {
        uint32_t imask = (c->sr >> 4) & 0xF;
        if (c->sr & BL) return;
        int took = 0;
        for (int i = 0; i < EV_N; i++) {
            if (!(c->ev_lat & (1u << i))) continue;
            if (c->intc_imr[EV_SRC[i].imr] & (1u << EV_SRC[i].bit)) continue;
            uint32_t w = c->intc_ipr[EV_SRC[i].ipr] |
                         ((uint32_t)c->intc_ipr[EV_SRC[i].ipr + 1] << 8);
            uint32_t lvl = (w >> EV_SRC[i].shift) & 0xF;
            if (!lvl || lvl <= imask) continue;
            c->ev_lat &= (uint8_t)~(1u << i);
            core_enter_interrupt(c, lvl, EV_SRC[i].code);
            took = 1;
            break;                          /* imask may have moved up */
        }
        if (!took) return;
    }
}

/* --- register serving (all addresses PHYSICAL, P2 already stripped) --- */
static int tmu_off(uint32_t p, int *unit, uint32_t *off)
{
    if (p - 0xFFD80000u < 0x30u) { *unit = 0; *off = p - 0xFFD80000u; return 1; }
    if (p - 0xFFD90000u < 0x30u) { *unit = 1; *off = p - 0xFFD90000u; return 1; }
    return 0;
}
/* n bytes at a TMU offset; 0 = not handled (cross). TOCR (+0) is not
 * modeled by the Python device either -- it crosses to the stub. */
static int ev_rd(Core *c, uint32_t p, int n, uint32_t *v)
{
    uint32_t m = 0xFFFFFFFFu >> (32u - 8u * (uint32_t)n);
    int u; uint32_t off;
    if (c->ev_on && tmu_off(p, &u, &off)) {
        if (off == 4u && n == 1) { *v = c->tstr[u] & 0xFFu; return 1; }
        if (off < 8u) return 0;
        uint32_t ch = (off - 8u) / 12u, kind = (off - 8u) % 12u;
        if (ch > 2u || kind + (uint32_t)n > 10u || (off & (uint32_t)(n - 1)))
            return 0;
        uint32_t *st = c->tmu[u][ch];
        uint32_t w = kind < 4u ? st[0] : kind < 8u ? st[1] : st[2];
        uint32_t sh = 8u * (kind & 3u);
        if (kind >= 8u && n == 1 && (off & 1u)) { w >>= 8; sh = 0; }
        *v = (w >> sh) & m;
        return 1;
    }
    if (c->ev_on && p - 0x04080000u < 0x28u && n == 1) {   /* IPR bytes */
        *v = c->intc_ipr[p - 0x04080000u]; return 1;
    }
    if (c->ev_on && p - 0x04080080u < 0x34u && n == 1) {   /* IMR bytes */
        *v = c->intc_imr[(p - 0x04080080u) >> 2]; return 1;
    }
    if (c->ev_on && p - 0xFE940468u < 2u) {               /* LDINTR */
        uint32_t sh = 8u * (p & 1u);
        *v = (c->lcdc_ldintr >> sh) & m; return 1;
    }
    if (c->port_on && p - 0x04050000u < 0x10000u && n == 1) {
        uint32_t b = c->port[p - 0x04050000u];
        if (p == 0x0405012Eu) b |= 0x12u;                  /* PIN_HIGH pull-up */
        if (p == 0x0405016Cu) b = (b & ~1u) | (c->hint_bit ? 0u : 1u);
        *v = b; return 1;
    }
    if (c->scif_on &&
        (p - 0xFFE00000u < 0x28u || p - 0xFFE10000u < 0x28u ||
         p - 0xFFE20000u < 0x28u) &&
        ((p & 0x1Fu) == 0x10u || (p & 0x1Fu) == 0x1Cu)) {
        int ch = (int)((p - 0xFFE00000u) >> 16);
        uint32_t r = (p & 0x1Eu) == 0x10u
            ? (c->scif_rx[ch] ? 0x63u : 0x60u)             /* SCFSR */
            : ((c->scif_rx[ch] < 16u ? c->scif_rx[ch] : 16u) << 8);
        /* SCFDR lo byte = 0; serve byte/word slices of the 16-bit reg */
        if (n == 1) *v = (p & 1u) ? ((r >> 8) & 0xFFu) : (r & 0xFFu);
        else *v = r & m;
        return 1;
    }
    return 0;
}

static int ev_wr(Core *c, uint32_t p, int n, uint32_t v)
{
    int u; uint32_t off;
    if (c->ev_on && tmu_off(p, &u, &off)) {
        if (off == 4u) { c->tstr[u] = v & 0xFFu;
            for (int ch = 0; ch < 3; ch++)                /* start = reload */
                if (v & (1u << ch)) c->tmu[u][ch][1] = c->tmu[u][ch][0];
            return 1; }
        if (off < 8u) return 0;
        uint32_t ch = (off - 8u) / 12u, kind = (off - 8u) % 12u;
        if (ch > 2u || kind + (uint32_t)n > 10u || (off & (uint32_t)(n - 1)))
            return 0;
        uint32_t *st = c->tmu[u][ch];
        uint32_t sh = 8u * (kind & 3u);
        uint32_t m = (0xFFFFFFFFu >> (32u - 8u * (uint32_t)n)) << sh;
        if (kind < 8u) {
            st[kind < 4u ? 0 : 1] = (st[kind < 4u ? 0 : 1] & ~m) | ((v << sh) & m);
            return 1;
        }
        if (n == 2) {                     /* UNF is R/(W0): 1 writes ignore */
            st[2] = ((v & ~0x0100u) | (st[2] & 0x0100u & v)) & 0xFFFFu;
        } else {                          /* byte: RMW the addressed half */
            uint32_t cur = st[2];
            if (off & 1u) cur = (cur & 0x00FFu) | ((v & 0xFFu) << 8);
            else          cur = (cur & 0xFF00u) | (v & 0xFFu);
            st[2] = cur & 0xFFFFu;
        }
        return 1;
    }
    if (c->ev_on && p - 0x04080000u < 0x28u && n == 1) {
        c->intc_ipr[p - 0x04080000u] = (uint8_t)v; return 1;
    }
    if (c->ev_on && p - 0x04080080u < 0x34u && n == 1) {
        c->intc_imr[(p - 0x04080080u) >> 2] = (uint8_t)v; return 1;
    }
    if (c->ev_on && p - 0x040800C0u < 0x34u && n == 1) {   /* IMCR: W1C */
        c->intc_imr[(p - 0x040800C0u) >> 2] &= (uint8_t)~v; return 1;
    }
    if (c->ev_on && p - 0xFE940468u < 2u && (p & 1u) + (uint32_t)n <= 2u) {
        for (int i = 0; i < n; i++) {
            uint32_t sh = 8u * ((p & 1u) + (uint32_t)i);
            c->lcdc_ldintr = (c->lcdc_ldintr & ~(0xFFu << sh)) |
                             (((v >> (8 * i)) & 0xFFu) << sh);
        }
        c->lcdc_ldintr &= 0xFFFFu;
        return 1;
    }
    if (c->port_on && p - 0x04050000u < 0x10000u &&
        p - 0x04050000u + (uint32_t)n <= 0x10000u) {
        if (p <= 0x04050128u && 0x04050128u < p + (uint32_t)n)
            return 0;                     /* DSP cmd pins: Python sync */
        for (int i = 0; i < n; i++)
            c->port[p - 0x04050000u + (uint32_t)i] = (uint8_t)(v >> (8 * i));
        return 1;
    }
    return 0;
}

/* HPID word store at the latched HPIA, straight into the DSP's RAM (the
 * same bytearrays the C6x core runs over). hpid_wr is armed by fastcpu.py
 * only while the DSP runs on its own thread and no code page has ever
 * been written through here (dspdev._store would have re-decoded). */
static int ev_hpid_wr(Core *c, uint32_t p, int n, uint32_t v)
{
    if (!c->hpid_wr || n != 4 || (p & 3u) || p - 0x180C0000u >= 4u)
        return 0;
    uint32_t a = c->cs6_addr & ~3u;
    for (uint32_t i = 0; i < c->dsp_region_n && i < 8u; i++) {
        if (a - c->dsp_region[i].base < c->dsp_region[i].len) {
            memcpy(c->dsp_region[i].ptr + (a - c->dsp_region[i].base), &v, 4);
            return 1;
        }
    }
    return 0;                              /* peripheral: Python decides */
}

static uint32_t rd8p(Core *c, uint32_t a)
{
    int fb, ro;
    if (a - 0xFF000000u < 0x2Cu) {
        uint32_t *f = mmu_field(c, a & ~3u);
        if (f) return (*f >> ((a & 3) * 8)) & 0xFF;
    }
    uint8_t *h = loc(c, a, &fb, &ro);
    if (h) return *h;
    if (a - CS6BASE < CS6SPAN) return cs6_rd8(c, a);
    if (a - TDG_BASE < 0x100u && c->tdg_on) return tdg_r8(c, a);
    { uint32_t dv; if (ev_rd(c, a, 1, &dv)) return dv; }
    { uint32_t dv; if (dmac_rd(c, a, 1, &dv)) return dv; }
    return c->io_rd(a) & 0xFF;
}

static void wr8p(Core *c, uint32_t a, uint32_t v)
{
    int fb, ro;
    if (a - 0xFF000000u < 0x2Cu) {
        uint32_t *f = mmu_field(c, a & ~3u);
        if (f) {
            uint32_t sh = (a & 3) * 8;
            *f = (*f & ~(0xFFu << sh)) | ((v & 0xFF) << sh);
            if (f == &c->mmucr) c->io_break = 1;
            return;
        }
    }
    uint8_t *h = loc(c, a, &fb, &ro);
    if (h) {
        if (ro) return;
        *h = (uint8_t)v;
        if (fb) c->fb_writes++;
        return;
    }
    if (a - CS6BASE < CS6SPAN) { cs6_wr8(c, a, v); return; }
    if (a - TDG_BASE < 0x100u && c->tdg_on) { tdg_wr8(c, a, v); return; }
    if (ev_wr(c, a, 1, v & 0xFFu)) return;
    if (dmac_wr(c, a, 1, v & 0xFFu)) return;
    c->io_wr(a, v & 0xFF);   /* the callback sets io_break if it matters */
}

/* n in {1,2,4}; p physical. Aligned accesses never straddle a region. */

/* Host HPID read served out of the DSP's own RAM (cs6_fast). b = byte
 * offset into the port, like dspdev.read: the word at HPIA&~3 (the
 * HPI never latches unaligned, but keep the same arithmetic), shifted
 * by the port offset; the auto-increment port bumps HPIA when the read
 * completes the word. Returns 1 on a region hit, 0 = cross to Python. */
static int cs6_hpid_rd(Core *c, uint32_t dp, int n, uint32_t *out)
{
    int inc;
    uint32_t b;
    if (dp == 0x80000u) inc = 1;            /* HPID auto-increment */
    else if (dp == 0xC0000u) inc = 0;       /* HPID fixed */
    else return 0;
    b = dp & 3u;
    if (b + (uint32_t)n < 4u || n < 1) return 0;    /* partial word: cross */
    for (uint32_t i = 0; i < c->dsp_region_n && i < 8u; i++) {
        uint32_t a = c->cs6_addr & ~3u, base = c->dsp_region[i].base;
        if (a - base < c->dsp_region[i].len) {
            uint32_t w;
            memcpy(&w, c->dsp_region[i].ptr + (a - base), 4);
            *out = (w >> (8u * b)) & (0xFFFFFFFFu >> (32u - 8u * (uint32_t)n));
            if (inc) c->cs6_addr += 4u;
            return 1;
        }
    }
    return 0;
}

static inline uint32_t prd(Core *c, uint32_t p, int n)
{
    int fb, ro;
    if (p - 0xFF000000u >= 0x2Cu) {
        uint8_t *h = loc(c, p, &fb, &ro);
        if (h) {
            if (n == 4) { uint32_t v; memcpy(&v, h, 4); return v; }
            if (n == 2) { uint16_t v; memcpy(&v, h, 2); return v; }
            return *h;
        }
        if (n > 1 && (p & (uint32_t)(n - 1)) == 0) {
            /* aligned device access fully outside RAM: one callback.
             * Both ends must miss RAM (a straddling access falls back
             * to bytes, like before). CS6 stays in C (cs6_rd8) on the
             * byte path, or io_rds once when the Python DSP model is
             * attached; 2DG reads are served from the mirror. */
            int fb2, ro2;
            uint32_t dp = p - CS6BASE, tp = p - TDG_BASE;
            if (dp < CS6SPAN && c->cs6_py) {
                if (p + (uint32_t)n > CS6BASE + CS6SPAN) {
                    /* straddles the CS6 window end: fall back to bytes */
                } else if (c->cs6_fast && dp - 0x40000u < 4u) {
                    return (c->cs6_addr >> (8u * (dp - 0x40000u))) &
                           (0xFFFFFFFFu >> (32u - 8u * (uint32_t)n));
                } else {
                    uint32_t v;
                    if (c->cs6_fast && cs6_hpid_rd(c, dp, n, &v))
                        return v;
                    return n == 4 ? c->io_rds(p, 4) & 0xFFFFFFFFu
                                  : c->io_rds(p, 2) & 0xFFFFu;
                }
            }
            { uint32_t dv; if (dmac_rd(c, p, n, &dv)) return dv; }
            { uint32_t dv; if (ev_rd(c, p, n, &dv)) return dv; }
            if (tp < 0x100u && c->tdg_on) {     /* 2DG register file */
                uint32_t w = (tp >> 2) == 1u ? c->tdg_ssr : c->tdg_reg[tp >> 2];
                return (w >> (8u * (tp & 3u))) &
                       (0xFFFFFFFFu >> (32u - 8u * (uint32_t)n));
            }
            if (!loc(c, p + (uint32_t)n - 1u, &fb2, &ro2) &&
                (dp >= CS6SPAN || p + (uint32_t)n <= CS6BASE + CS6SPAN))
                return n == 4 ? c->io_rds(p, 4) & 0xFFFFFFFFu
                              : c->io_rds(p, 2) & 0xFFFFu;
        }
    }
    uint32_t v = 0;
    for (int i = 0; i < n; i++) v |= rd8p(c, p + i) << (8 * i);
    return v;
}

static inline void pwr(Core *c, uint32_t p, int n, uint32_t v)
{
    int fb, ro;
    if (p - 0xFF000000u >= 0x2Cu) {
        uint8_t *h = loc(c, p, &fb, &ro);
        if (h) {
            if (ro) return;
            if (n == 4) memcpy(h, &v, 4);
            else if (n == 2) { uint16_t s = (uint16_t)v; memcpy(h, &s, 2); }
            else *h = (uint8_t)v;
            if (fb) c->fb_writes += n;
            return;
        }
        if (n > 1 && (p & (uint32_t)(n - 1)) == 0) {
            int fb2, ro2;
            uint32_t dp = p - CS6BASE, tp = p - TDG_BASE;
            if (dp - 0x40000u < 4u && c->cs6_py && c->cs6_fast &&
                dp + (uint32_t)n <= 0x40004u) {   /* HPIA: pure mirror */
                uint32_t sh = 8u * (dp - 0x40000u);
                uint32_t m = (0xFFFFFFFFu >> (32u - 8u * (uint32_t)n)) << sh;
                c->cs6_addr = (c->cs6_addr & ~m) | ((v << sh) & m);
                return;
            }
            if (dmac_wr(c, p, n, v)) return;
            if (ev_hpid_wr(c, p, n, v)) return;
            if (ev_wr(c, p, n, v)) return;
            if (tp < 0x100u && c->tdg_on && ((tp >> 2) != 0u || c->tdg_cfast)) {
                /* same as n byte stores in order; SCLR (word 0) crosses */
                for (int i = 0; i < n; i++)
                    tdg_wr8(c, p + (uint32_t)i, (v >> (8 * i)) & 0xFFu);
                return;
            }
            if (!loc(c, p + (uint32_t)n - 1u, &fb2, &ro2)) {
                if (n == 4) c->io_wrs(p, v, 4);
                else c->io_wrs(p, v & 0xFFFFu, 2);
                return;
            }
        }
    }
    for (int i = 0; i < n; i++) wr8p(c, p + i, (v >> (8 * i)) & 0xFF);
}

/* address translation, MMU off (sh4.py translate) */
static inline uint32_t xlat(Core *c, uint32_t a)
{
    if (a - 0x80000000u < 0x40000000u) {           /* P1 / P2 */
        if (!(c->sr & MD)) raise_exc(0x100, 0x0E0, 1, a);
        return a & 0x1FFFFFFFu;
    }
    if (a >= 0xE0000000u) {                         /* P4 */
        if (!(c->sr & MD)) raise_exc(0x100, 0x0E0, 1, a);
        return a;
    }
    if (c->sr & MD) {
        if (a < 0x20000000u) return a;
        if (a - 0xC0000000u < 0x20000000u) return a & 0x1FFFFFFFu;
        raise_exc(0x100, 0x0E0, 1, a);
    }
    if (a < 0x20000000u) return a;
    raise_exc(0x100, 0x0E0, 1, a);
    return 0;
}

/* 2026-10-26 data-page cache: host pointer of a recently used 4 KB page,
 * keyed on (virtual page, SR.MD, low-flash mapping) -- exactly what xlat
 * and loc depend on -- so a hit skips xlat/prd/loc and is the same access.
 * Only pages that loc() maps wholly into one host buffer are cached
 * (devices, CS6, 2DG, P4 never are). Stamped with a generation bumped at
 * every core_run entry, so Python-side changes between runs (buffers,
 * low_is_ram) can never be seen stale. XDJ_DTLB=0 (g_dtlb) turns it off. */
#define DTLB_N 256u
typedef struct { uint32_t key, gen; uint8_t *h; uint8_t fb, ro, code, pad[13]; } Dtlb;  /* 32 B: JIT indexes it; code = page holds blocks */
static Dtlb g_dtlb_t[DTLB_N];
static uint32_t g_dtlb_gen = 1;
int g_dtlb = 1;
void core_set_dtlb(int on) { g_dtlb = on; g_dtlb_gen++; }

static inline uint32_t dkey(Core *c, uint32_t a)
{
    return (a & ~0xFFFu) | ((c->sr & MD) ? 1u : 0u) | (c->low_is_ram ? 2u : 0u);
}
/* Code-change tracking for the block engine (2026-10-26). A block checks
 * its opcodes against memory only when g_vgen moved since its last check.
 * g_vgen moves on every core_run entry (Python may have written anything
 * between runs), after every slow-path access (devices: their callbacks
 * can write RAM), after the native fast paths, and on every store into a
 * host page that holds blocks (bit set in g_codepg, mirrored as Dtlb.code
 * so the cached store paths -- C and JIT -- see it). Pages outside ram0 /
 * ram1 are not tracked: their blocks always compare. */
/* g_vgen and g_blk_epoch side by side: chained native code reads both
 * through one base register (shjit.c) */
static uint32_t g_jg[2] = { 1, 1 };
#define g_vgen (g_jg[0])
#define g_blk_epoch (g_jg[1])
static uint8_t g_codepg[(2u * 0x04000000u) >> 15];      /* 1 bit / 4 KB page */
static inline int codepg_idx(Core *c, const uint8_t *h)
{
    if ((uintptr_t)(h - c->ram0) < 0x04000000u) return (int)((h - c->ram0) >> 12);
    if ((uintptr_t)(h - c->ram1) < 0x04000000u) return (int)(16384 + ((h - c->ram1) >> 12));
    return -1;
}
static inline int codepg_get(Core *c, const uint8_t *h)
{
    int i = codepg_idx(c, h);
    return i >= 0 && (g_codepg[i >> 3] >> (i & 7) & 1);
}

static inline Dtlb *dtlb_get(Core *c, uint32_t a)
{
    Dtlb *e = &g_dtlb_t[(a >> 12) & (DTLB_N - 1u)];
    uint32_t k = dkey(c, a);
    if (e->key == k && e->gen == g_dtlb_gen) return e;
    if (!g_dtlb) return 0;
    /* miss: translate exactly like the slow path would (a privilege
     * fault here is the same fault the slow path raises first) */
    uint32_t p = xlat(c, a) & ~0xFFFu;
    if (p - 0xFF000000u < 0x1000u) return 0;
    int fb, ro, fb2, ro2;
    uint8_t *h = loc(c, p, &fb, &ro);
    if (!h || loc(c, p + 0xFFFu, &fb2, &ro2) != h + 0xFFF || fb2 != fb || ro2 != ro)
        return 0;
    e->key = k; e->gen = g_dtlb_gen; e->h = h; e->fb = (uint8_t)fb; e->ro = (uint8_t)ro;
    e->code = (uint8_t)codepg_get(c, h);
    return e;
}

/* block engine write tracking (see blk_*): host address of the last
 * page-cached store, and "a slow-path (device / uncached) access happened" */
static const uint8_t *g_wlast;
static int g_wslow;

static inline uint32_t vrd(Core *c, uint32_t a, int n)
{
    if (!(a & (uint32_t)(n - 1))) {
        Dtlb *e = dtlb_get(c, a);
        if (e) {
            uint8_t *h = e->h + (a & 0xFFFu);
            if (n == 4) { uint32_t v; memcpy(&v, h, 4); return v; }
            if (n == 2) { uint16_t v; memcpy(&v, h, 2); return v; }
            return *h;
        }
    }
    g_wslow = 1;                                 /* block engine: end the block */
    uint32_t p = xlat(c, a);
    if (n == 2 && (a & 1)) raise_exc(0x100, 0x0E0, 1, a);
    if (n == 4 && (a & 3)) raise_exc(0x100, 0x0E0, 1, a);
    uint32_t v = prd(c, p, n);
    g_vgen++;                                    /* a device may have written RAM */
    return v;
}

static inline void vwr(Core *c, uint32_t a, int n, uint32_t v)
{
    if (!(a & (uint32_t)(n - 1))) {
        Dtlb *e = dtlb_get(c, a);
        if (e) {
            if (e->ro) return;                  /* flash: pwr ignores it too */
            uint8_t *h = e->h + (a & 0xFFFu);
            if (n == 4) memcpy(h, &v, 4);
            else if (n == 2) { uint16_t s = (uint16_t)v; memcpy(h, &s, 2); }
            else *h = (uint8_t)v;
            if (e->fb) c->fb_writes += (uint64_t)n;
            g_wlast = h;                         /* block engine: own-page check */
            if (e->code) g_vgen++;               /* a page holding blocks changed */
            return;
        }
    }
    g_wslow = 1;                                 /* block engine: end the block */
    uint32_t p = xlat(c, a);
    if (n == 2 && (a & 1)) raise_exc(0x100, 0x0E0, 1, a);
    if (n == 4 && (a & 3)) raise_exc(0x100, 0x0E0, 1, a);
    pwr(c, p, n, v);
    g_vgen++;
}

#define RD8(a)  vrd(c, (a), 1)
#define RD16(a) vrd(c, (a), 2)
#define RD32(a) vrd(c, (a), 4)
#define WR8(a, v)  vwr(c, (a), 1, (v))
#define WR16(a, v) vwr(c, (a), 2, (v))
#define WR32(a, v) vwr(c, (a), 4, (v))
#define S8(x)  ((uint32_t)(int32_t)(int8_t)(x))
#define S16(x) ((uint32_t)(int32_t)(int16_t)(x))

/* ------------------------------ FPU ---------------------------------- */
static inline float f_of(uint32_t u) { float f; memcpy(&f, &u, 4); return f; }
static inline uint32_t u_of(float f) { uint32_t u; memcpy(&u, &f, 4); return u; }
#define FBANK ((c->fpscr >> 21) & 1)
#define FSZ ((c->fpscr >> 20) & 1)
#define FPR ((c->fpscr >> 19) & 1)
#define FRRAW(b, i) (c->fr[((b) & 1) * 16 + ((i) & 15)])

static inline float getf(Core *c, int i) { return f_of(FRRAW(FBANK, i)); }
static inline void setf(Core *c, int i, double v) { FRRAW(FBANK, i) = u_of((float)v); }
static inline double getd(Core *c, int pair, int bank)
{
    uint64_t u = ((uint64_t)FRRAW(bank, pair * 2) << 32) | FRRAW(bank, pair * 2 + 1);
    double d; memcpy(&d, &u, 8); return d;
}
static inline void setd(Core *c, int pair, int bank, double d)
{
    uint64_t u; memcpy(&u, &d, 8);
    FRRAW(bank, pair * 2) = (uint32_t)(u >> 32);
    FRRAW(bank, pair * 2 + 1) = (uint32_t)u;
}

typedef struct { int kind; uint32_t tgt, cyc, rssr; } Res;
enum { K_NONE, K_TIMED, K_BR, K_DBR, K_RTE, K_EXC, K_ADV4 };

static void fpu_step(Core *c, uint32_t op, Res *res)
{
    if (c->sr & FD) raise_exc(0x100, 0x120, 0, 0);
    int n = (op >> 8) & 0xF, m = (op >> 4) & 0xF, lo4 = op & 0xF;
    int sz = FSZ, pr = FPR, bk = FBANK;
    uint32_t *R = c->r;

    if (lo4 <= 0x5) {
        if (pr) {
            if (op & 0x0110) ILLEGAL();
            int dp = (op >> 9) & 7, sp = (op >> 5) & 7;
            double a = getd(c, sp, bk), b = getd(c, dp, bk);
            if (lo4 == 4) { SETT(b == a); return; }
            if (lo4 == 5) { SETT(b > a); return; }
            static const uint32_t cy[4] = {4, 4, 5, 18};
            double v = lo4 == 0 ? b + a : lo4 == 1 ? b - a : lo4 == 2 ? b * a : b / a;
            setd(c, dp, bk, v);
            res->kind = K_TIMED; res->cyc = cy[lo4];
            return;
        }
        double a = getf(c, m), b = getf(c, n);
        if (lo4 == 4) { SETT(b == a); return; }
        if (lo4 == 5) { SETT(b > a); return; }
        static const uint32_t cy[4] = {3, 3, 3, 12};
        double v = lo4 == 0 ? b + a : lo4 == 1 ? b - a : lo4 == 2 ? b * a : b / a;
        setf(c, n, v);
        res->kind = K_TIMED; res->cyc = cy[lo4];
        return;
    }
    if (lo4 == 0xE) {                               /* FMAC */
        if (pr) ILLEGAL();
        setf(c, n, (double)getf(c, 0) * getf(c, m) + getf(c, n));
        res->kind = K_TIMED; res->cyc = 3;
        return;
    }
    if (lo4 == 0xC) {                               /* FMOV reg-reg */
        if (sz) {
            int dp = (op >> 9) & 7, db = (op >> 8) & 1, sp = (op >> 5) & 7, sb = (op >> 4) & 1;
            uint32_t hi = FRRAW(bk ^ sb, sp * 2), lo = FRRAW(bk ^ sb, sp * 2 + 1);
            FRRAW(bk ^ db, dp * 2) = hi; FRRAW(bk ^ db, dp * 2 + 1) = lo;
        } else {
            FRRAW(bk, n) = FRRAW(bk, m);
        }
        return;
    }
    if (lo4 >= 0x6 && lo4 <= 0xB) {                 /* FMOV memory */
        if (sz) {
            if (lo4 == 8 || lo4 == 9 || lo4 == 6) {
                int dp = (op >> 9) & 7, db = (op >> 8) & 1;
                uint32_t a = lo4 != 6 ? R[m] : R[0] + R[m];
                uint32_t lo = RD32(a), hi = RD32(a + 4);
                FRRAW(bk ^ db, dp * 2) = hi; FRRAW(bk ^ db, dp * 2 + 1) = lo;
                if (lo4 == 9) R[m] += 8;
            } else {
                int sp = (op >> 5) & 7, sb = (op >> 4) & 1;
                uint32_t hi = FRRAW(bk ^ sb, sp * 2), lo = FRRAW(bk ^ sb, sp * 2 + 1);
                if (lo4 == 0xB) R[n] -= 8;
                uint32_t a = lo4 != 7 ? R[n] : R[0] + R[n];
                WR32(a, lo); WR32(a + 4, hi);
            }
            return;
        }
        switch (lo4) {
        case 0x8: FRRAW(bk, n) = RD32(R[m]); break;
        case 0x9: FRRAW(bk, n) = RD32(R[m]); R[m] += 4; break;
        case 0xA: WR32(R[n], FRRAW(bk, m)); break;
        case 0xB: R[n] -= 4; WR32(R[n], FRRAW(bk, m)); break;
        case 0x6: FRRAW(bk, n) = RD32(R[0] + R[m]); break;
        default:  WR32(R[0] + R[n], FRRAW(bk, m)); break;
        }
        return;
    }
    if (op == 0xF3FD) { c->fpscr ^= 1u << 20; return; }
    if (op == 0xFBFD) { c->fpscr ^= 1u << 21; return; }
    if (op == 0xF7FD) { c->fpscr ^= 1u << 19; return; }
    if (lo4 == 0xD) {
        int sub = (op >> 4) & 0xF, dn = (n >> 1) & 7;
        switch (sub) {
        case 0x0: FRRAW(bk, n) = c->fpul; return;
        case 0x1: c->fpul = FRRAW(bk, n); return;
        case 0x2:
            if (pr) setd(c, dn, bk, (double)(int32_t)c->fpul);
            else setf(c, n, (double)(int32_t)c->fpul);
            res->kind = K_TIMED; res->cyc = 2; return;
        case 0x3: {
            double v = pr ? getd(c, dn, bk) : (double)getf(c, n);
            if (isnan(v)) c->fpul = 0x80000000u;
            else if (v >= 2147483647.0) c->fpul = 0x7FFFFFFFu;
            else if (v <= -2147483648.0) c->fpul = 0x80000000u;
            else c->fpul = (uint32_t)(int32_t)v;
            res->kind = K_TIMED; res->cyc = 2; return;
        }
        case 0x4: case 0x5: {
            int i = pr ? dn * 2 : n;
            uint32_t u = FRRAW(bk, i);
            FRRAW(bk, i) = sub == 4 ? (u ^ 0x80000000u) : (u & 0x7FFFFFFFu);
            return;
        }
        case 0x6:
            if (pr) { double v = getd(c, dn, bk); setd(c, dn, bk, v >= 0 ? sqrt(v) : NAN); }
            else { double v = getf(c, n); setf(c, n, v >= 0 ? sqrt(v) : NAN); }
            res->kind = K_TIMED; res->cyc = 8; return;
        case 0x7: {
            double v = getf(c, n);
            setf(c, n, v > 0 ? 1.0 / sqrt(v) : (v == 0 ? INFINITY : NAN));
            res->kind = K_TIMED; res->cyc = 6; return;
        }
        case 0x8: FRRAW(bk, n) = 0; return;
        case 0x9: FRRAW(bk, n) = 0x3F800000u; return;
        case 0xA: setd(c, dn, bk, (double)f_of(c->fpul)); res->kind = K_TIMED; res->cyc = 3; return;
        case 0xB: c->fpul = u_of((float)getd(c, dn, bk)); res->kind = K_TIMED; res->cyc = 3; return;
        case 0xE: {
            int vn = (op >> 10) & 3, vm = (op >> 8) & 3;
            double s = 0;
            for (int k = 0; k < 4; k++) s += (double)getf(c, vn * 4 + k) * getf(c, vm * 4 + k);
            setf(c, vn * 4 + 3, s);
            res->kind = K_TIMED; res->cyc = 6; return;
        }
        case 0xF:
            if ((op & 0x0300) == 0x0100) {          /* FTRV XMTRX,FVn */
                int vn = (op >> 10) & 3;
                double vec[4], out[4];
                for (int k = 0; k < 4; k++) vec[k] = getf(c, vn * 4 + k);
                for (int i = 0; i < 4; i++) {
                    double s = 0;
                    for (int k = 0; k < 4; k++) s += (double)f_of(FRRAW(bk ^ 1, k * 4 + i)) * vec[k];
                    out[i] = s;
                }
                for (int i = 0; i < 4; i++) setf(c, vn * 4 + i, out[i]);
                res->kind = K_TIMED; res->cyc = 8; return;
            }
            if (!(op & 0x0100)) {                   /* FSCA FPUL,DRn */
                double ang = (c->fpul & 0xFFFF) * (2 * M_PI / 65536.0);
                int dp = (op >> 9) & 7;
                setf(c, dp * 2, sin(ang));
                setf(c, dp * 2 + 1, cos(ang));
                res->kind = K_TIMED; res->cyc = 8; return;
            }
            ILLEGAL();
        }
        ILLEGAL();
    }
    ILLEGAL();
}

static void fmov32(Core *c, uint32_t w1, uint32_t w2, Res *res)
{
    int dbl = !(w1 & 0x0100);
    int n = (w1 >> 8) & (dbl ? 7 : 0xF), m = (w1 >> 4) & 0xF;
    int bk = FBANK;
    if ((w2 & 0xF000) == 0x7000) {
        uint32_t a = c->r[m] + (w2 & 0xFFF) * (dbl ? 8 : 4);
        if (dbl) {
            uint32_t lo = 0, hi = 0;
            for (int k = 0; k < 4; k++) lo |= prd(c, xlat(c, a + k), 1) << (8 * k);
            for (int k = 0; k < 4; k++) hi |= prd(c, xlat(c, a + 4 + k), 1) << (8 * k);
            FRRAW(0, n * 2) = hi; FRRAW(0, n * 2 + 1) = lo;
        } else {
            FRRAW(bk, n) = RD32(a);
        }
    } else if ((w2 & 0xF000) == 0x3000) {
        uint32_t a = c->r[n] + (w2 & 0xFFF) * (dbl ? 8 : 4);
        if (dbl) {
            uint32_t hi = FRRAW(0, n * 2), lo = FRRAW(0, n * 2 + 1);
            for (int k = 0; k < 4; k++) pwr(c, xlat(c, a + k), 1, (lo >> (8 * k)) & 0xFF);
            for (int k = 0; k < 4; k++) pwr(c, xlat(c, a + 4 + k), 1, (hi >> (8 * k)) & 0xFF);
            g_vgen++;
        } else {
            WR32(a, FRRAW(bk, m));
        }
    } else {
        ILLEGAL();
    }
    res->kind = K_ADV4; res->cyc = 2;
}

/* ------------------------- one instruction ---------------------------- */
static int g_fallback;      /* set by an op we leave to the Python core */

static void step(Core *c, uint32_t op, uint32_t nxt, Res *res)
{
    uint32_t *R = c->r;
    uint32_t n = (op >> 8) & 0xF, m = (op >> 4) & 0xF, i8 = op & 0xFF;
    res->kind = K_NONE;

    switch (op >> 12) {
    case 0x0: {
        uint32_t lo = op & 0xFF;
        if ((op & 0xF) == 0x7) { c->macl = R[n] * R[m]; res->kind = K_TIMED; res->cyc = 2; return; }
        if ((op & 0xF) == 0xF) {                    /* MAC.L */
            int32_t a = (int32_t)RD32(R[m]), b = (int32_t)RD32(R[n]);
            R[m] += 4; R[n] += 4;
            int64_t acc = (int64_t)(((uint64_t)c->mach << 32) | c->macl);
            acc = (int64_t)((uint64_t)acc + (uint64_t)((int64_t)a * b));
            c->mach = (uint32_t)((uint64_t)acc >> 32); c->macl = (uint32_t)acc;
            res->kind = K_TIMED; res->cyc = 3; return;
        }
        switch (lo) {
        case 0x02: R[n] = c->sr; return;
        case 0x03: c->pr = nxt + 2; res->kind = K_DBR; res->tgt = c->pc + 4 + R[n]; res->cyc = 2; return;
        case 0x08: c->sr &= ~TB; return;
        case 0x09: return;
        case 0x0B: res->kind = K_DBR; res->tgt = c->pr; res->cyc = 2; return;
        case 0x0A: R[n] = c->mach; return;
        case 0x18: c->sr |= TB; return;
        case 0x19: c->sr &= ~(MB | QB | TB); return;
        case 0x1A: R[n] = c->macl; return;
        case 0x1B: c->sleeping = 1; return;
        case 0x23: res->kind = K_DBR; res->tgt = c->pc + 4 + R[n]; res->cyc = 2; return;
        case 0x28: c->mach = c->macl = 0; return;
        case 0x29: R[n] = T() ? 1 : 0; return;
        case 0x2A: R[n] = c->pr; return;
        case 0x5A: R[n] = c->fpul; return;
        case 0x6A: R[n] = c->fpscr; return;
        case 0x2B:
            if (!(c->sr & MD)) ILLEGAL();
            res->kind = K_RTE; res->tgt = c->spc; res->rssr = c->ssr; res->cyc = 4; return;
        case 0x38:
            if (!(c->sr & MD)) ILLEGAL();
            g_fallback = 1; return;                 /* LDTLB: Python core */
        case 0x48: c->sr &= ~SB; return;
        case 0x58: c->sr |= SB; return;
        }
        switch (op & 0xF) {
        case 0xC: R[n] = S8(RD8(R[0] + R[m])); return;
        case 0xD: R[n] = S16(RD16(R[0] + R[m])); return;
        case 0xE: R[n] = RD32(R[0] + R[m]); return;
        case 0x4: WR8(R[0] + R[n], R[m] & 0xFF); return;
        case 0x5: WR16(R[0] + R[n], R[m]); return;
        case 0x6: WR32(R[0] + R[n], R[m]); return;
        }
        switch (lo) {
        case 0x12: R[n] = c->gbr; return;
        case 0x22: R[n] = c->vbr; return;
        case 0x32: R[n] = c->ssr; return;
        case 0x42: R[n] = c->spc; return;
        case 0x3A: R[n] = c->sgr; return;
        case 0xFA: R[n] = c->dbr; return;
        }
        if ((op & 0x008F) == 0x0082) { R[n] = c->alt[(op >> 4) & 7]; return; }
        if (lo == 0xC3) { WR32(R[n], R[0]); return; }
        if (lo == 0x83 || lo == 0x93 || lo == 0xA3 || lo == 0xB3 || lo == 0xD3 || lo == 0xE3) return;
        if (lo == 0xAB) return;
        ILLEGAL();
    }
    case 0x1: WR32(R[n] + (op & 0xF) * 4, R[m]); return;
    case 0x5: R[n] = RD32(R[m] + (op & 0xF) * 4); return;
    case 0x2:
        switch (op & 0xF) {
        case 0x0: WR8(R[n], R[m] & 0xFF); return;
        case 0x1: WR16(R[n], R[m]); return;
        case 0x2: WR32(R[n], R[m]); return;
        case 0x3: ILLEGAL();
        case 0x4: R[n] -= 1; WR8(R[n], R[m] & 0xFF); return;
        case 0x5: R[n] -= 2; WR16(R[n], R[m]); return;
        case 0x6: R[n] -= 4; WR32(R[n], R[m]); return;
        case 0x7: {
            uint32_t q = R[n] >> 31, mm = R[m] >> 31;
            if (q) c->sr |= QB; else c->sr &= ~QB;
            if (mm) c->sr |= MB; else c->sr &= ~MB;
            SETT(mm ^ q); return;
        }
        case 0x8: SETT((R[n] & R[m]) == 0); return;
        case 0x9: R[n] &= R[m]; return;
        case 0xA: R[n] ^= R[m]; return;
        case 0xB: R[n] |= R[m]; return;
        case 0xC: {
            uint32_t x = R[n] ^ R[m];
            SETT(!(x & 0xFF) || !(x & 0xFF00) || !(x & 0xFF0000) || !(x & 0xFF000000u));
            return;
        }
        case 0xD: R[n] = (R[n] >> 16) | (R[m] << 16); return;
        case 0xE: c->macl = (R[n] & 0xFFFF) * (R[m] & 0xFFFF); res->kind = K_TIMED; res->cyc = 3; return;
        case 0xF: c->macl = (uint32_t)((int32_t)(int16_t)R[n] * (int32_t)(int16_t)R[m]); res->kind = K_TIMED; res->cyc = 3; return;
        }
        return;
    case 0x3:
        switch (op & 0xF) {
        case 0x1: {
            uint32_t w2 = RD16(nxt);
            fmov32(c, op, w2, res); return;
        }
        case 0x0: SETT(R[n] == R[m]); return;
        case 0x2: SETT(R[n] >= R[m]); return;
        case 0x3: SETT((int32_t)R[n] >= (int32_t)R[m]); return;
        case 0x4: {                                 /* DIV1 */
            uint32_t old_q = (c->sr & QB) ? 1 : 0, mm = (c->sr & MB) ? 1 : 0;
            uint32_t q_new = R[n] >> 31;
            uint32_t rn = (R[n] << 1) | (T() ? 1 : 0), tmp, cy;
            if (old_q == mm) { tmp = rn - R[m]; cy = tmp > rn; }
            else { tmp = rn + R[m]; cy = tmp < rn; }
            uint32_t q = (mm == q_new) ? cy : !cy;
            if (q) c->sr |= QB; else c->sr &= ~QB;
            SETT(q == mm);
            R[n] = tmp; return;
        }
        case 0x5: {
            uint64_t p = (uint64_t)R[n] * R[m];
            c->macl = (uint32_t)p; c->mach = (uint32_t)(p >> 32);
            res->kind = K_TIMED; res->cyc = 3; return;
        }
        case 0x6: SETT(R[n] > R[m]); return;
        case 0x7: SETT((int32_t)R[n] > (int32_t)R[m]); return;
        case 0x8: R[n] -= R[m]; return;
        case 0xA: {
            uint64_t tmp = (uint64_t)R[n] - R[m] - (T() ? 1 : 0);
            SETT((int64_t)((int64_t)(uint64_t)R[n] - (int64_t)R[m] - (T() ? 1 : 0)) < 0);
            R[n] = (uint32_t)tmp; return;
        }
        case 0xB: {
            int64_t d = (int64_t)(int32_t)R[n] - (int32_t)R[m];
            SETT(d < -0x80000000LL || d > 0x7FFFFFFFLL);
            R[n] -= R[m]; return;
        }
        case 0xC: R[n] += R[m]; return;
        case 0xD: {
            int64_t p = (int64_t)(int32_t)R[n] * (int32_t)R[m];
            c->macl = (uint32_t)p; c->mach = (uint32_t)((uint64_t)p >> 32);
            res->kind = K_TIMED; res->cyc = 3; return;
        }
        case 0xE: {
            uint64_t tmp = (uint64_t)R[n] + R[m] + (T() ? 1 : 0);
            SETT(tmp > 0xFFFFFFFFull);
            R[n] = (uint32_t)tmp; return;
        }
        case 0xF: {
            int64_t d = (int64_t)(int32_t)R[n] + (int32_t)R[m];
            SETT(d < -0x80000000LL || d > 0x7FFFFFFFLL);
            R[n] += R[m]; return;
        }
        }
        ILLEGAL();
    case 0x4: {
        uint32_t lo = op & 0xFF, v;
        switch (lo) {
        case 0x00: case 0x20: SETT(R[n] >> 31); R[n] <<= 1; return;
        case 0x01: SETT(R[n] & 1); R[n] >>= 1; return;
        case 0x21: SETT(R[n] & 1); R[n] = (uint32_t)((int32_t)R[n] >> 1); return;
        case 0x04: v = R[n]; SETT(v >> 31); R[n] = (v << 1) | (v >> 31); return;
        case 0x05: v = R[n]; SETT(v & 1); R[n] = (v >> 1) | (v << 31); return;
        case 0x06: c->mach = RD32(R[n]); R[n] += 4; return;
        case 0x07:
            if (!(c->sr & MD)) ILLEGAL();
            v = RD32(R[n]); R[n] += 4; set_sr(c, v); SETT(v & 1); return;
        case 0x08: R[n] <<= 2; return;
        case 0x09: R[n] >>= 2; return;
        case 0x0A: c->mach = R[n]; return;
        case 0x0B: c->pr = nxt + 2; res->kind = K_DBR; res->tgt = R[n]; res->cyc = 2; return;
        case 0x0E:
            if (!(c->sr & MD)) ILLEGAL();
            v = R[n]; set_sr(c, v); SETT(v & 1); return;
        case 0x10: R[n] -= 1; SETT(R[n] == 0); return;
        case 0x11: SETT(!(R[n] >> 31)); return;
        case 0x15: SETT(R[n] != 0 && !(R[n] >> 31)); return;
        case 0x16: c->macl = RD32(R[n]); R[n] += 4; return;
        case 0x17: c->gbr = RD32(R[n]); R[n] += 4; return;
        case 0x18: R[n] <<= 8; return;
        case 0x19: R[n] >>= 8; return;
        case 0x1A: c->macl = R[n]; return;
        case 0x1B:
            v = RD8(R[n]); SETT(v == 0); WR8(R[n], v | 0x80);
            res->kind = K_TIMED; res->cyc = 4; return;
        case 0x1E: c->gbr = R[n]; return;
        case 0x24: { v = R[n]; uint32_t t = v >> 31; R[n] = (v << 1) | (T() ? 1 : 0); SETT(t); return; }
        case 0x25: { v = R[n]; uint32_t t = v & 1; R[n] = (v >> 1) | ((T() ? 1u : 0u) << 31); SETT(t); return; }
        case 0x26: c->pr = RD32(R[n]); R[n] += 4; return;
        case 0x27:
            if (!(c->sr & MD)) ILLEGAL();
            c->vbr = RD32(R[n]); R[n] += 4; return;
        case 0x28: R[n] <<= 16; return;
        case 0x29: R[n] >>= 16; return;
        case 0x2A: c->pr = R[n]; return;
        case 0x2B: res->kind = K_DBR; res->tgt = R[n]; res->cyc = 2; return;
        case 0x2E: if (!(c->sr & MD)) ILLEGAL(); c->vbr = R[n]; return;
        case 0x36: if (!(c->sr & MD)) ILLEGAL(); c->sgr = RD32(R[n]); R[n] += 4; return;
        case 0x37: if (!(c->sr & MD)) ILLEGAL(); c->ssr = RD32(R[n]); R[n] += 4; return;
        case 0x3A: if (!(c->sr & MD)) ILLEGAL(); c->sgr = R[n]; return;
        case 0x3E: if (!(c->sr & MD)) ILLEGAL(); c->ssr = R[n]; return;
        case 0x47: if (!(c->sr & MD)) ILLEGAL(); c->spc = RD32(R[n]); R[n] += 4; return;
        case 0x4E: if (!(c->sr & MD)) ILLEGAL(); c->spc = R[n]; return;
        case 0x56: c->fpul = RD32(R[n]); R[n] += 4; return;
        case 0x5A: c->fpul = R[n]; return;
        case 0x66: c->fpscr = RD32(R[n]); R[n] += 4; return;
        case 0x6A: c->fpscr = R[n]; return;
        case 0xE9: {                                /* MOVUA.L @Rm,R0 */
            uint32_t a = R[n], x = 0;
            for (int k = 0; k < 4; k++) x |= prd(c, xlat(c, a + k), 1) << (8 * k);
            R[0] = x; return;
        }
        }
        if ((op & 0xF00F) == 0x400C) {              /* SHAD */
            uint32_t s = R[m];
            if (!(s >> 31)) R[n] <<= (s & 0x1F);
            else if (!(s & 0x1F)) R[n] = ((int32_t)R[n] < 0) ? 0xFFFFFFFFu : 0;
            else R[n] = (uint32_t)((int32_t)R[n] >> (((~s) & 0x1F) + 1));
            return;
        }
        if ((op & 0xF00F) == 0x400D) {              /* SHLD */
            uint32_t s = R[m];
            if (!(s >> 31)) R[n] <<= (s & 0x1F);
            else if (!(s & 0x1F)) R[n] = 0;
            else R[n] >>= (((~s) & 0x1F) + 1);
            return;
        }
        if ((op & 0xF00F) == 0x400F) {              /* MAC.W */
            int32_t a = (int16_t)RD16(R[m]), b = (int16_t)RD16(R[n]);
            R[m] += 2; R[n] += 2;
            int64_t acc = (int64_t)(((uint64_t)c->mach << 32) | c->macl);
            acc += (int64_t)a * b;
            if (c->sr & SB) {
                if (acc > 0x7FFFFFFFFFFFLL) acc = 0x7FFFFFFFFFFFLL;
                else if (acc < -0x800000000000LL) acc = -0x800000000000LL;
            }
            c->mach = (uint32_t)((uint64_t)acc >> 32); c->macl = (uint32_t)acc;
            res->kind = K_TIMED; res->cyc = 3; return;
        }
        if (lo == 0x02 || lo == 0x12 || lo == 0x22) {
            R[n] -= 4; WR32(R[n], lo == 0x02 ? c->mach : lo == 0x12 ? c->macl : c->pr); return;
        }
        if (lo == 0x62 || lo == 0x52) { R[n] -= 4; WR32(R[n], lo == 0x62 ? c->fpscr : c->fpul); return; }
        if (lo == 0x13) { R[n] -= 4; WR32(R[n], c->gbr); return; }
        if (lo == 0x03 || lo == 0x23 || lo == 0x33 || lo == 0x43 || lo == 0x32) {
            uint32_t sv = lo == 0x03 ? c->sr : lo == 0x23 ? c->vbr : lo == 0x33 ? c->ssr
                        : lo == 0x43 ? c->spc : c->sgr;
            R[n] -= 4; WR32(R[n], sv); return;
        }
        if ((op & 0x008F) == 0x0083) {
            if (!(c->sr & MD)) ILLEGAL();
            v = c->alt[(op >> 4) & 7]; R[n] -= 4; WR32(R[n], v); return;
        }
        if ((op & 0x008F) == 0x008E) {
            if (!(c->sr & MD)) ILLEGAL();
            c->alt[(op >> 4) & 7] = R[n]; return;
        }
        if ((op & 0x008F) == 0x0087) {
            if (!(c->sr & MD)) ILLEGAL();
            c->alt[(op >> 4) & 7] = RD32(R[n]); R[n] += 4; return;
        }
        ILLEGAL();
    }
    case 0x6:
        switch (op & 0xF) {
        case 0x0: R[n] = S8(RD8(R[m])); return;
        case 0x1: R[n] = S16(RD16(R[m])); return;
        case 0x2: R[n] = RD32(R[m]); return;
        case 0x3: R[n] = R[m]; return;
        case 0x4: { uint32_t v = S8(RD8(R[m])); R[n] = v; if (n != m) R[m] += 1; else R[m] = v + 1; return; }
        case 0x5: { uint32_t v = S16(RD16(R[m])); R[n] = v; if (n != m) R[m] += 2; else R[m] = v + 2; return; }
        case 0x6: { uint32_t v = RD32(R[m]); R[n] = v; if (n != m) R[m] += 4; else R[m] = v + 4; return; }
        case 0x7: R[n] = ~R[m]; return;
        case 0x8: { uint32_t v = R[m]; R[n] = (v & 0xFFFF0000u) | ((v & 0xFF) << 8) | ((v >> 8) & 0xFF); return; }
        case 0x9: { uint32_t v = R[m]; R[n] = (v >> 16) | (v << 16); return; }
        case 0xA: {
            int64_t tmp = 0 - (int64_t)R[m] - (T() ? 1 : 0);
            SETT(tmp < 0); R[n] = (uint32_t)tmp; return;
        }
        case 0xB: R[n] = 0 - R[m]; return;
        case 0xC: R[n] = R[m] & 0xFF; return;
        case 0xD: R[n] = R[m] & 0xFFFF; return;
        case 0xE: R[n] = S8(R[m]); return;
        case 0xF: R[n] = S16(R[m]); return;
        }
        return;
    case 0x7: R[n] += S8(i8); return;
    case 0x8: {
        uint32_t sub = (op >> 8) & 0xF, disp4 = op & 0xF;
        uint32_t disp = S8(i8) * 2 + 4;
        switch (sub) {
        case 0x0: WR8(R[m] + disp4, R[0] & 0xFF); return;
        case 0x1: WR16(R[m] + disp4 * 2, R[0]); return;
        case 0x4: R[0] = S8(RD8(R[m] + disp4)); return;
        case 0x5: R[0] = S16(RD16(R[m] + disp4 * 2)); return;
        case 0x8: SETT(R[0] == S8(i8)); return;
        case 0x9: if (T()) { res->kind = K_BR; res->tgt = c->pc + disp; res->cyc = 1; } return;
        case 0xB: if (!T()) { res->kind = K_BR; res->tgt = c->pc + disp; res->cyc = 1; } return;
        case 0xD: if (T()) { res->kind = K_DBR; res->tgt = c->pc + disp; res->cyc = 1; } return;
        case 0xF: if (!T()) { res->kind = K_DBR; res->tgt = c->pc + disp; res->cyc = 1; } return;
        }
        ILLEGAL();
    }
    case 0x9: R[n] = S16(RD16(c->pc + 4 + i8 * 2)); return;
    case 0xA: { int32_t d = op & 0xFFF; if (d & 0x800) d -= 0x1000;
                res->kind = K_DBR; res->tgt = c->pc + 4 + d * 2; res->cyc = 2; return; }
    case 0xB: { int32_t d = op & 0xFFF; if (d & 0x800) d -= 0x1000;
                c->pr = nxt + 2; res->kind = K_DBR; res->tgt = c->pc + 4 + d * 2; res->cyc = 2; return; }
    case 0xC: {
        uint32_t a;
        switch (op & 0xFF00) {
        case 0xC700: R[0] = ((c->pc + 4) & ~3u) + i8 * 4; return;
        case 0xC300:
            c->tra = i8 << 2;
            enter_exc(c, 0x100, 0x160, c->pc + 2, 0, 0);
            res->kind = K_EXC; res->cyc = 8; return;
        case 0xC000: WR8(c->gbr + i8, R[0] & 0xFF); return;
        case 0xC100: WR16(c->gbr + i8 * 2, R[0]); return;
        case 0xC200: WR32(c->gbr + i8 * 4, R[0]); return;
        case 0xC400: R[0] = S8(RD8(c->gbr + i8)); return;
        case 0xC500: R[0] = S16(RD16(c->gbr + i8 * 2)); return;
        case 0xC600: R[0] = RD32(c->gbr + i8 * 4); return;
        case 0xC800: SETT((R[0] & i8) == 0); return;
        case 0xC900: R[0] &= i8; return;
        case 0xCA00: R[0] ^= i8; return;
        case 0xCB00: R[0] |= i8; return;
        case 0xCC00: SETT((RD8(R[0] + c->gbr) & i8) == 0); return;
        case 0xCD00: a = R[0] + c->gbr; WR8(a, RD8(a) & i8); return;
        case 0xCE00: a = R[0] + c->gbr; WR8(a, RD8(a) ^ i8); return;
        case 0xCF00: a = R[0] + c->gbr; WR8(a, RD8(a) | i8); return;
        }
        ILLEGAL();
    }
    case 0xD: R[n] = RD32(((c->pc + 4) & ~3u) + i8 * 4); return;
    case 0xE: R[n] = S8(i8); return;
    case 0xF: fpu_step(c, op, res); return;
    }
    ILLEGAL();
}

/* --------------------------------------------------------------------- *
 * Native fast paths for hot firmware loops (2026-10-25). Each one runs the
 * loop's iterations directly in C with the exact register, memory, T,
 * steps, cycles and iteration-count results of the generic interpreter;
 * it bails out (returns 0, nothing changed) unless the loop's code bytes
 * match, the iteration budget covers the whole loop, and every access of
 * an iteration is plain RAM (checked before the iteration runs; a partial
 * run stops at the loop head with the state of that iteration boundary).
 * core_set_fastpath(0) turns them off (XDJ_FASTPATH=0).
 * --------------------------------------------------------------------- */
static int g_fastpath = 1;
int g_fp_bail = 0;             /* last fastpath bail reason (debug) */
uint64_t g_fp_bails[64];       /* per-reason counters (debug) */
#define BAIL(r) do { g_fp_bail = (r); g_fp_bails[(r) & 63]++; return 0; } while (0)
#define BAILB(r) do { g_fp_bail = (r); g_fp_bails[(r) & 63]++; break; } while (0)
static uint64_t g_fp_hits, g_fp_iters;
/* pc hit counters for tools (core_set_watch): off when g_watch_n == 0 */
static uint32_t g_watch_n, g_watch_pc[16];
static uint64_t g_watch_hit[16];
void core_set_watch(int n, const uint32_t *pcs)
{
    g_watch_n = n < 0 ? 0 : n > 16 ? 16 : (uint32_t)n;
    for (uint32_t i = 0; i < g_watch_n; i++) { g_watch_pc[i] = pcs[i]; g_watch_hit[i] = 0; }
}
uint64_t core_watch_hits(int i) { return (i >= 0 && i < 16) ? g_watch_hit[i] : 0; }
void core_set_fastpath(int on) { g_fastpath = on; }
uint64_t core_fastpath_hits(int which) { return which ? g_fp_iters : g_fp_hits; }
int core_fp_bail(void) { return g_fp_bail; }
void core_fp_bails(uint64_t *out) { for (int i = 0; i < 64; i++) out[i] = g_fp_bails[i]; }

/* plain-RAM host pointer for a P0 address, or 0 */
static inline uint8_t *ram_ptr(Core *c, uint32_t a, int n)
{
    if (a >= 0x20000000u || (a & (uint32_t)(n - 1))) return 0;
    int fb, ro;
    uint8_t *h = loc(c, a, &fb, &ro);
    if (!h || fb || ro) return 0;
    int fb2, ro2;
    if (loc(c, a + (uint32_t)n - 1u, &fb2, &ro2) != h + n - 1) return 0;
    return h;
}

/* same, but P2/P1 views allowed (physical RAM underneath) and framebuffer
 * regions allowed (plain host RAM; caller counts fb_writes) */
static inline uint8_t *ram_ptr_w(Core *c, uint32_t a, int n, int *fb_out)
{
    a &= 0x1FFFFFFFu;
    if (a < 0x08000000u || a >= 0x20000000u || (a & (uint32_t)(n - 1))) return 0;
    int fb, ro;
    uint8_t *h = loc(c, a, &fb, &ro);
    if (!h || ro) return 0;
    int fb2, ro2;
    if (loc(c, a + (uint32_t)n - 1u, &fb2, &ro2) != h + n - 1) return 0;
    *fb_out |= fb;
    return h;
}

/* centre-waveform layer copy, staging -> locked surface, two rows per pass
 * (FUN_08DC3xxx release path, 0x08DC40B8..0x08DC40EE, 28 instructions) */
static const uint16_t FP_WCOPY[28] = {
    0x66f2, 0xe004, 0x52f1, 0x4018, 0x0467, 0x061a, 0x7402, 0x1f9a,
    0x70f0, 0x4600, 0x362c, 0x6251, 0x367c, 0x2621, 0x62f2, 0x59f1,
    0x0127, 0x061a, 0x7102, 0x025d, 0x35dc, 0x4600, 0x34e2, 0x369c,
    0x59fa, 0x367c, 0x8fe4, 0x2621 };

/* per loop-iteration unit (one core_run iteration each): steps, cycles.
 * 26 single instructions (two mul.l = 2 cycles), then bf/s: taken = one
 * unit with its slot (2 steps, 1 cycle); not taken = bf/s + slot as two
 * one-cycle units. */
static const uint8_t WC_CYC[26] = {1,1,1,1,2,1,1,1,1,1,1,1,1,1,1,1,2,1,1,1,1,1,1,1,1,1};

static uint64_t fp_wcopy(Core *c, uint64_t budget, int *term)
{
    g_fp_bail = 1;
    uint8_t *code = ram_ptr(c, 0x08DC40B8u, 2);
    if (!code || memcmp(code, FP_WCOPY, sizeof FP_WCOPY)) BAIL(11);
    g_fp_bail = 2;
    uint32_t *R = c->r;
    /* iterations until r4 (+2 each) >= r14 */
    uint32_t r4 = R[4], kfull = 1;
    while ((uint32_t)(r4 + 2u * kfull) < R[14]) {
        if (++kfull > 0x100000u) return 0;
    }
    /* 2026-10-26: chunks shrink to the next device event, so a budget that
     * covers the WHOLE loop is rare (this was 100 % of the bails). Run as
     * many whole passes as fit; a partial run stops at the loop head with
     * the state of that pass boundary (identical accounting either way). */
    if (budget < 28ull) BAIL(3);
    uint64_t kmax = (budget - 28ull) / 27ull + 1;
    uint32_t k = kfull < kmax ? kfull : (uint32_t)kmax;
    uint64_t it = 0;
#define OVL(p, n, q, m) ((p) < (q) + (m) && (q) < (p) + (n))
    for (uint32_t i = 0; i < k; i++) {
        uint32_t sp = R[15] & 0x1FFFFFFFu, r5 = R[5] & 0x1FFFFFFFu;
        int fbw = 0;
        uint8_t *pw = ram_ptr_w(c, sp, 4, &fbw), *pd = ram_ptr_w(c, sp + 4, 4, &fbw);
        int fb40v = 0;
        uint8_t *p40 = ram_ptr_w(c, sp + 40, 4, &fb40v);
        if (!pw || !pd || !p40) BAILB(20 + (!pw) + 2 * (!pd) + 4 * (!p40));
        uint32_t W, D;
        memcpy(&W, pw, 4); memcpy(&D, pd, 4);
        uint32_t a1 = ((W * R[4]) << 1) + D + R[7];
        uint32_t a2 = ((W * R[1]) << 1) + D + R[7];
        /* fb_writes like the interpreter: bytes STORED into a framebuffer
         * (2026-10-26: the old "+8 if any access touched a framebuffer"
         * counted reads too and was off by 4 -> DSP time-lock drift) */
        int fb40 = 0, fb1 = 0, fb2 = 0, fbr = 0;
        uint8_t *ps1 = ram_ptr_w(c, r5, 2, &fbr), *pd1 = ram_ptr_w(c, a1, 2, &fb1),
                *ps2 = ram_ptr_w(c, r5 + 0x3F0u, 2, &fbr), *pd2 = ram_ptr_w(c, a2, 2, &fb2);
        if (!ps1 || !pd1 || !ps2 || !pd2) BAILB(30 + (!ps1) + 2 * (!pd1) + 4 * (!ps2) + 8 * (!pd2));
        /* stores of the first half must not feed a later read of this pass
         * (then the generic interpreter takes it) */
        if (OVL(p40, 4, pw, 4) || OVL(p40, 4, pd, 4) || OVL(p40, 4, ps2, 2) ||
            OVL(pd1, 2, pw, 4) || OVL(pd1, 2, pd, 4) || OVL(pd1, 2, ps2, 2) ||
            OVL(pd1, 2, p40, 4))
        BAILB(40);
        uint32_t r9 = R[9];
        int16_t v1, v2;
        memcpy(p40, &r9, 4);                  /* mov.l r9,@(40,r15) */
        memcpy(&v1, ps1, 2);
        memcpy(pd1, &v1, 2);                  /* mov.w r2,@r6 */
        memcpy(&v2, ps2, 2);
        memcpy(pd2, &v2, 2);                  /* mov.w r2,@r6 (slot) */
        (void)fb40; (void)fbr;
        c->fb_writes += (fb40v ? 4 : 0) + (fb1 ? 2 : 0) + (fb2 ? 2 : 0);
        c->macl = W * R[1];
        R[0] = 0x3F0u;
        R[1] += 2u;
        R[2] = (uint32_t)(int32_t)v2;
        R[4] += 2u;
        R[5] = r5 + R[13];
        R[6] = a2;
        /* r9 = @(40,r15) = the r9 just stored */
        if (R[4] >= R[14]) c->sr |= 1u; else c->sr &= ~1u;
        it++;
    }
#undef OVL
    if (it == 0) return 0;
    *term = (it == kfull);
    c->pc = (c->sr & 1u) ? 0x08DC40F0u : 0x08DC40B8u;
    c->cur_pc = 0x08DC40EEu;
    return it;                     /* loop passes */
}

/* firmware memset, 16-bytes-per-pass inner loop (0x08E1D994..0x08E1D9A0:
 * 4x mov.l r7,@-r2 + add #-1,r5, cmp/pl r5, bt) -- all 1-cycle ops */
static const uint16_t FP_MSET[7] = { 0x2276, 0x75ff, 0x2276, 0x2276, 0x2276, 0x4515, 0x89f8 };

static uint64_t fp_memset(Core *c, uint64_t budget)
{
    uint8_t *code = ram_ptr(c, 0x08E1D994u, 2);
    if (!code || memcmp(code, FP_MSET, sizeof FP_MSET)) return 0;
    uint32_t *R = c->r;
    uint32_t x = R[5];
    uint64_t k = (int32_t)x >= 1 ? x : 1;
    if (k > budget / 7) k = budget / 7;
    if (!k) return 0;
    /* the firmware clears through P2 (uncached) views as often as P1 --
     * same RAM here (2026-10-26: the P2 check made every such call bail) */
    uint32_t hi = R[2] & 0x1FFFFFFFu, lo = hi - (uint32_t)(16 * k);
    uint32_t top = R[2] >> 29;          /* 0 plain, 4 = P1, 5 = P2 view */
    if ((top && top != 4 && top != 5) || hi < 0x08000000u ||
        hi >= 0x20000000u || (hi & 3u) || lo > hi) return 0;
    if (lo < 0x08E1D9A2u && hi > 0x08E1D994u) return 0;    /* own code */
    int fb, ro, fb2, ro2;
    uint8_t *h = loc(c, lo, &fb, &ro), *h2 = loc(c, hi - 1u, &fb2, &ro2);
    if (!h || ro || ro2 || fb != fb2 || h2 != h + (hi - 1u - lo)) return 0;
    uint32_t v = R[7];
    for (uint64_t i = 0; i < 4 * k; i++) memcpy(h + 4 * i, &v, 4);
    if (fb) c->fb_writes += 16 * k;
    R[2] = lo | (R[2] & 0xE0000000u);
    R[5] = x - (uint32_t)k;
    int t = (int32_t)R[5] > 0;
    if (t) c->sr |= 1u; else c->sr &= ~1u;
    c->pc = t ? 0x08E1D994u : 0x08E1D9A2u;
    c->cur_pc = 0x08E1D9A0u;
    return k;                      /* loop passes (7 one-cycle units each) */
}

/* --------------------------------------------------------------------- *
 * Run up to max_iter loop iterations (a branch + its delay slot is one, as
 * in sh4.py). *done receives the count. Stops early for: idle-task stack
 * checker (R_IDLE, pc left there), SLEEP (R_SLEEP), a device write or MMU
 * register write (R_IO, so Python can deliver interrupts promptly),
 * LDTLB / MMU on (R_FALLBACK, pc left at the instruction).
 * --------------------------------------------------------------------- */

/* ------------------ decode-once table (2026-10-26) --------------------- *
 * g_cls[op] = fast-path class of each 16-bit opcode, built once. The ~45
 * hottest forms (about 90 % of executed instructions, measured on the
 * real-DSP playing snapshot) run here with bodies copied from step();
 * class 0 = everything else -> step(). Only forms with step() result
 * K_NONE / K_TIMED / K_BR are here (no delay slots, no exceptions other
 * than the memory faults step() raises the same way).
 * Proven by core_fast_selftest (every classed opcode, random state, vs
 * step()) and the differ A/B runs. XDJ_DTAB=0 -> core_set_dtab(0). */
enum {
    F_NONE, F_MOV, F_ADD, F_ADDI, F_MOVI, F_MOVLPC, F_MOVWPC, F_MOVL_LD,
    F_MOVL_ST, F_MOVL_LDD, F_MOVL_STD, F_MOVL_POP, F_MOVL_PUSH, F_EXTUB,
    F_EXTUW, F_TST, F_TSTI, F_BT, F_BF, F_MOVB_LD, F_MOVB_POP, F_MOVB_ST,
    F_MOVW_LD, F_MOVW_ST, F_CMPEQ, F_CMPHS, F_CMPGE, F_CMPHI, F_CMPGT,
    F_CMPEQI, F_SHAD, F_SHLD, F_AND, F_OR, F_XOR, F_ANDI, F_SHLL, F_SHLL2,
    F_SHLL8, F_SHLL16, F_SHLR, F_SHLR2, F_SHLR8, F_SHLR16, F_SHAR, F_DT,
    F_STSMACH, F_STSMACL, F_MULL, F_SUB, F_MOVL_LD0, F_MOVB_LD0, F_MOVL_ST0,
    F_STSPR_PUSH, F_LDSPR_POP, F_EXTSB, F_EXTSW, F_CMPPL, F_CMPPZ, F_NEG,
    F_NOT, F_MOVW_POP, F_MOVA, F_MOVLGBR, F_ORI,
    /* 2026-10-26: the forms that still went through step() while playing */
    F_MOVW_LD0, F_MOVB_LDD, F_MOVW_LDD, F_MOVB_STD8, F_MOVW_STD8,
    F_MOVB_PUSH, F_MOVW_PUSH, F_STSFPSCR, F_LDSFPSCR, F_STSMACL_PUSH,
    F_LDSMACL_POP, F_DMULU
};
/* cycles of a classed instruction (fast_step's return value) */
static inline uint32_t cls_cyc(uint32_t k) { return k == F_MULL ? 2u : k == F_DMULU ? 3u : 1u; }
static uint8_t g_cls[65536];
static int g_dtab = 1, g_dtab_built = 0;
void core_set_dtab(int on) { g_dtab = on; }

static void dtab_build(void)
{
    for (uint32_t op = 0; op < 65536; op++) {
        uint32_t h = op >> 12, lo4 = op & 0xF, lo8 = op & 0xFF, k = F_NONE;
        switch (h) {
        case 0x0:
            if (lo4 == 0x7) k = F_MULL;
            else if (lo8 == 0x0A || (op & 0xF0FF) == 0x000A) k = ((op & 0xF0FF) == 0x000A) ? F_STSMACH : F_NONE;
            if ((op & 0xF0FF) == 0x001A) k = F_STSMACL;
            if (lo4 == 0xE) k = F_MOVL_LD0;
            if (lo4 == 0xC) k = F_MOVB_LD0;
            if (lo4 == 0x6) k = F_MOVL_ST0;
            if (lo4 == 0xD) k = F_MOVW_LD0;
            if ((op & 0xF0FF) == 0x006A) k = F_STSFPSCR;
            break;
        case 0x1: k = F_MOVL_STD; break;
        case 0x2:
            switch (lo4) {
            case 0x0: k = F_MOVB_ST; break; case 0x1: k = F_MOVW_ST; break;
            case 0x2: k = F_MOVL_ST; break; case 0x6: k = F_MOVL_PUSH; break;
            case 0x4: k = F_MOVB_PUSH; break; case 0x5: k = F_MOVW_PUSH; break;
            case 0x8: k = F_TST; break; case 0x9: k = F_AND; break;
            case 0xA: k = F_XOR; break; case 0xB: k = F_OR; break;
            }
            break;
        case 0x3:
            switch (lo4) {
            case 0x0: k = F_CMPEQ; break; case 0x2: k = F_CMPHS; break;
            case 0x3: k = F_CMPGE; break; case 0x6: k = F_CMPHI; break;
            case 0x7: k = F_CMPGT; break; case 0x8: k = F_SUB; break;
            case 0xC: k = F_ADD; break;
            case 0x5: k = F_DMULU; break;
            }
            break;
        case 0x4:
            switch (lo8) {
            case 0x00: k = F_SHLL; break; case 0x08: k = F_SHLL2; break;
            case 0x18: k = F_SHLL8; break; case 0x28: k = F_SHLL16; break;
            case 0x01: k = F_SHLR; break; case 0x09: k = F_SHLR2; break;
            case 0x19: k = F_SHLR8; break; case 0x29: k = F_SHLR16; break;
            case 0x21: k = F_SHAR; break; case 0x10: k = F_DT; break;
            case 0x11: k = F_CMPPZ; break; case 0x15: k = F_CMPPL; break;
            case 0x22: k = F_STSPR_PUSH; break; case 0x26: k = F_LDSPR_POP; break;
            case 0x12: k = F_STSMACL_PUSH; break; case 0x16: k = F_LDSMACL_POP; break;
            case 0x6A: k = F_LDSFPSCR; break;
            }
            if (lo4 == 0xC) k = F_SHAD;
            if (lo4 == 0xD) k = F_SHLD;
            break;
        case 0x5: k = F_MOVL_LDD; break;
        case 0x6:
            switch (lo4) {
            case 0x0: k = F_MOVB_LD; break; case 0x1: k = F_MOVW_LD; break;
            case 0x2: k = F_MOVL_LD; break; case 0x3: k = F_MOV; break;
            case 0x4: k = F_MOVB_POP; break; case 0x5: k = F_MOVW_POP; break;
            case 0x6: k = F_MOVL_POP; break; case 0x7: k = F_NOT; break;
            case 0xB: k = F_NEG; break; case 0xC: k = F_EXTUB; break;
            case 0xD: k = F_EXTUW; break; case 0xE: k = F_EXTSB; break;
            case 0xF: k = F_EXTSW; break;
            }
            break;
        case 0x7: k = F_ADDI; break;
        case 0x8:
            switch ((op >> 8) & 0xF) {
            case 0x8: k = F_CMPEQI; break; case 0x9: k = F_BT; break;
            case 0x0: k = F_MOVB_STD8; break; case 0x1: k = F_MOVW_STD8; break;
            case 0x4: k = F_MOVB_LDD; break; case 0x5: k = F_MOVW_LDD; break;
            case 0xB: k = F_BF; break;
            }
            break;
        case 0x9: k = F_MOVWPC; break;
        case 0xC:
            switch (op & 0xFF00) {
            case 0xC700: k = F_MOVA; break; case 0xC800: k = F_TSTI; break;
            case 0xC900: k = F_ANDI; break; case 0xCB00: k = F_ORI; break;
            case 0xC600: k = F_MOVLGBR; break;
            }
            break;
        case 0xD: k = F_MOVLPC; break;
        case 0xE: k = F_MOVI; break;
        }
        g_cls[op] = (uint8_t)k;
    }
    g_dtab_built = 1;
}

/* returns the instruction's cycle count; *br != 0 -> taken branch to *tgt */
static inline uint32_t fast_step(Core *c, uint32_t op, uint32_t k, int *br, uint32_t *tgt)
{
    uint32_t *R = c->r;
    uint32_t n = (op >> 8) & 0xF, m = (op >> 4) & 0xF, i8 = op & 0xFF, v;
    switch (k) {
    case F_MOV: R[n] = R[m]; return 1;
    case F_ADD: R[n] += R[m]; return 1;
    case F_SUB: R[n] -= R[m]; return 1;
    case F_ADDI: R[n] += S8(i8); return 1;
    case F_MOVI: R[n] = S8(i8); return 1;
    case F_MOVLPC: R[n] = RD32(((c->pc + 4) & ~3u) + i8 * 4); return 1;
    case F_MOVWPC: R[n] = S16(RD16(c->pc + 4 + i8 * 2)); return 1;
    case F_MOVA: R[0] = ((c->pc + 4) & ~3u) + i8 * 4; return 1;
    case F_MOVL_LD: R[n] = RD32(R[m]); return 1;
    case F_MOVW_LD: R[n] = S16(RD16(R[m])); return 1;
    case F_MOVB_LD: R[n] = S8(RD8(R[m])); return 1;
    case F_MOVL_ST: WR32(R[n], R[m]); return 1;
    case F_MOVW_ST: WR16(R[n], R[m]); return 1;
    case F_MOVB_ST: WR8(R[n], R[m] & 0xFF); return 1;
    case F_MOVL_LDD: R[n] = RD32(R[m] + (op & 0xF) * 4); return 1;
    case F_MOVL_STD: WR32(R[n] + (op & 0xF) * 4, R[m]); return 1;
    case F_MOVL_POP: v = RD32(R[m]); R[n] = v; if (n != m) R[m] += 4; else R[m] = v + 4; return 1;
    case F_MOVW_POP: v = S16(RD16(R[m])); R[n] = v; if (n != m) R[m] += 2; else R[m] = v + 2; return 1;
    case F_MOVB_POP: v = S8(RD8(R[m])); R[n] = v; if (n != m) R[m] += 1; else R[m] = v + 1; return 1;
    case F_MOVL_PUSH: R[n] -= 4; WR32(R[n], R[m]); return 1;
    case F_MOVL_LD0: R[n] = RD32(R[0] + R[m]); return 1;
    case F_MOVB_LD0: R[n] = S8(RD8(R[0] + R[m])); return 1;
    case F_MOVL_ST0: WR32(R[0] + R[n], R[m]); return 1;
    case F_MOVLGBR: R[0] = RD32(c->gbr + i8 * 4); return 1;
    case F_EXTUB: R[n] = R[m] & 0xFF; return 1;
    case F_EXTUW: R[n] = R[m] & 0xFFFF; return 1;
    case F_EXTSB: R[n] = S8(R[m]); return 1;
    case F_EXTSW: R[n] = S16(R[m]); return 1;
    case F_NOT: R[n] = ~R[m]; return 1;
    case F_NEG: R[n] = 0 - R[m]; return 1;
    case F_TST: SETT((R[n] & R[m]) == 0); return 1;
    case F_TSTI: SETT((R[0] & i8) == 0); return 1;
    case F_AND: R[n] &= R[m]; return 1;
    case F_OR: R[n] |= R[m]; return 1;
    case F_XOR: R[n] ^= R[m]; return 1;
    case F_ANDI: R[0] &= i8; return 1;
    case F_ORI: R[0] |= i8; return 1;
    case F_CMPEQ: SETT(R[n] == R[m]); return 1;
    case F_CMPHS: SETT(R[n] >= R[m]); return 1;
    case F_CMPGE: SETT((int32_t)R[n] >= (int32_t)R[m]); return 1;
    case F_CMPHI: SETT(R[n] > R[m]); return 1;
    case F_CMPGT: SETT((int32_t)R[n] > (int32_t)R[m]); return 1;
    case F_CMPEQI: SETT(R[0] == S8(i8)); return 1;
    case F_CMPPZ: SETT(!(R[n] >> 31)); return 1;
    case F_CMPPL: SETT(R[n] != 0 && !(R[n] >> 31)); return 1;
    case F_BT: if (T()) { *br = 1; *tgt = c->pc + S8(i8) * 2 + 4; } return 1;
    case F_BF: if (!T()) { *br = 1; *tgt = c->pc + S8(i8) * 2 + 4; } return 1;
    case F_SHAD: {
        uint32_t s = R[m];
        if (!(s >> 31)) R[n] <<= (s & 0x1F);
        else if (!(s & 0x1F)) R[n] = ((int32_t)R[n] < 0) ? 0xFFFFFFFFu : 0;
        else R[n] = (uint32_t)((int32_t)R[n] >> (((~s) & 0x1F) + 1));
        return 1;
    }
    case F_SHLD: {
        uint32_t s = R[m];
        if (!(s >> 31)) R[n] <<= (s & 0x1F);
        else if (!(s & 0x1F)) R[n] = 0;
        else R[n] >>= (((~s) & 0x1F) + 1);
        return 1;
    }
    case F_SHLL: SETT(R[n] >> 31); R[n] <<= 1; return 1;
    case F_SHLR: SETT(R[n] & 1); R[n] >>= 1; return 1;
    case F_SHAR: SETT(R[n] & 1); R[n] = (uint32_t)((int32_t)R[n] >> 1); return 1;
    case F_SHLL2: R[n] <<= 2; return 1;
    case F_SHLL8: R[n] <<= 8; return 1;
    case F_SHLL16: R[n] <<= 16; return 1;
    case F_SHLR2: R[n] >>= 2; return 1;
    case F_SHLR8: R[n] >>= 8; return 1;
    case F_SHLR16: R[n] >>= 16; return 1;
    case F_DT: R[n] -= 1; SETT(R[n] == 0); return 1;
    case F_STSMACH: R[n] = c->mach; return 1;
    case F_STSMACL: R[n] = c->macl; return 1;
    case F_MULL: c->macl = R[n] * R[m]; return 2;
    case F_STSPR_PUSH: R[n] -= 4; WR32(R[n], c->pr); return 1;
    case F_LDSPR_POP: c->pr = RD32(R[n]); R[n] += 4; return 1;
    case F_MOVW_LD0: R[n] = S16(RD16(R[0] + R[m])); return 1;
    case F_MOVB_LDD: R[0] = S8(RD8(R[m] + (op & 0xF))); return 1;
    case F_MOVW_LDD: R[0] = S16(RD16(R[m] + (op & 0xF) * 2)); return 1;
    case F_MOVB_STD8: WR8(R[m] + (op & 0xF), R[0] & 0xFF); return 1;
    case F_MOVW_STD8: WR16(R[m] + (op & 0xF) * 2, R[0]); return 1;
    case F_MOVB_PUSH: R[n] -= 1; WR8(R[n], R[m] & 0xFF); return 1;
    case F_MOVW_PUSH: R[n] -= 2; WR16(R[n], R[m]); return 1;
    case F_STSFPSCR: R[n] = c->fpscr; return 1;
    case F_LDSFPSCR: c->fpscr = R[n]; return 1;
    case F_STSMACL_PUSH: R[n] -= 4; WR32(R[n], c->macl); return 1;
    case F_LDSMACL_POP: c->macl = RD32(R[n]); R[n] += 4; return 1;
    case F_DMULU: { uint64_t p = (uint64_t)R[n] * R[m];
        c->macl = (uint32_t)p; c->mach = (uint32_t)(p >> 32); return 3; }
    }
    return 0;
}


/* -------------------- block engine, stage 1 (2026-10-26) ---------------- *
 * A block = a straight run of up to BMAX instructions that the decode-once
 * table handles (g_cls != 0), ending at the first other instruction, after
 * a bt/bf, at a 4 KB page end, or BEFORE any pc the run loop treats
 * specially (idle loop, native fast paths, the low-flash switch). Blocks
 * run inside core_run with the same locals, so the step/cycle accounting,
 * the COMMIT points, io_break and fault semantics are the interpreter's
 * own. Code can change under a block at any time (our mods, the firmware
 * copying code, DMA): every entry compares the block's opcodes with the
 * live memory (rebuild on any difference), and inside a block a store
 * into the block's own code page, or any slow-path (device / uncached)
 * access, ends the block right after that instruction. Key = (pc, MD,
 * low-flash map) + buffer epoch + idle pc. XDJ_BLOCKS=0 -> off. */
#define BLK_BITS 18
#define BLK_N (1u << BLK_BITS)
/* slot of a block: halfword index folded with the bits above the table
 * span, so hot code exactly 512 KB apart no longer shares a slot (the
 * plain index thrashed ~470k rebuilds per second while playing) */
#define BLK_IDX(pc) ((((pc) >> 1) ^ ((pc) >> (1 + BLK_BITS))) & (BLK_N - 1u))
#define BMAX 32
typedef struct Blk_ {
    uint32_t pc, key, epoch, idle;
    uint16_t n, first;                   /* first = opcode at pc (n == 0 blocks) */
    uint32_t hot;                        /* stage-1 runs (JIT threshold) */
    const uint8_t *host;                 /* host base of the code page */
    uint32_t (*fn)(Core *);              /* compiled code (shjit.c) or NULL */
    uint8_t tkind;                       /* T_* delayed branch at ops[n], slot ops[n+1] */
    uint8_t vchk;                        /* page tracked by g_codepg */
    uint32_t vgen;                       /* g_vgen of the last opcode check */
    uint16_t ops[BMAX + 2];
    uint8_t k[BMAX + 2];                 /* class; 0 = the branch of a terminator */
} Blk;
/* delayed-branch terminators (core_run's K_DBR path) */
enum { T_NONE, T_BRA, T_BSR, T_RTS, T_JSR, T_JMP, T_BSRF, T_BRAF, T_BTS, T_BFS };
static inline int blk_term(uint32_t op)
{
    if ((op & 0xF000u) == 0xA000u) return T_BRA;
    if ((op & 0xF000u) == 0xB000u) return T_BSR;
    if ((op & 0xF0FFu) == 0x000Bu) return T_RTS;
    if ((op & 0xF0FFu) == 0x400Bu) return T_JSR;
    if ((op & 0xF0FFu) == 0x402Bu) return T_JMP;
    if ((op & 0xF0FFu) == 0x0003u) return T_BSRF;
    if ((op & 0xF0FFu) == 0x0023u) return T_BRAF;
    if ((op & 0xFF00u) == 0x8D00u) return T_BTS;
    if ((op & 0xFF00u) == 0x8F00u) return T_BFS;
    return T_NONE;
}
static Blk g_blk[BLK_N];
static int g_blk_on = 1;
static uint8_t *g_blk_bufs[7];
static uint64_t g_blk_runs, g_blk_insns, g_blk_builds;
void core_set_blocks(int on) { g_blk_on = on; g_blk_epoch++; }
uint64_t core_blk_stat(int k) { return k == 0 ? g_blk_runs : k == 1 ? g_blk_insns : g_blk_builds; }

static inline int blk_is_mem(uint32_t k)
{
    switch (k) {
    case F_MOVLPC: case F_MOVWPC: case F_MOVL_LD: case F_MOVW_LD: case F_MOVB_LD:
    case F_MOVL_ST: case F_MOVW_ST: case F_MOVB_ST: case F_MOVL_LDD: case F_MOVL_STD:
    case F_MOVL_POP: case F_MOVW_POP: case F_MOVB_POP: case F_MOVL_PUSH:
    case F_MOVL_LD0: case F_MOVB_LD0: case F_MOVL_ST0: case F_MOVLGBR:
    case F_STSPR_PUSH: case F_LDSPR_POP:
    case F_MOVW_LD0: case F_MOVB_LDD: case F_MOVW_LDD: case F_MOVB_STD8: case F_MOVW_STD8:
    case F_MOVB_PUSH: case F_MOVW_PUSH: case F_STSMACL_PUSH: case F_LDSMACL_POP:
        return 1;
    }
    return 0;
}

static inline int blk_special(Core *c, uint32_t pc)
{
    return (pc & 0x1FFFFFFFu) == c->idle_pc || pc == 0x08DC40B8u || pc == 0x08E1D994u ||
           pc == 0xA8000800u;
}

static void blk_build(Core *c, Blk *b, uint32_t pc, uint32_t key, const uint16_t *page)
{
    g_blk_builds++;
    b->pc = pc; b->key = key; b->epoch = g_blk_epoch; b->idle = c->idle_pc;
    b->host = (const uint8_t *)page;
    b->fn = 0; b->hot = 0;
    /* mark the page as holding blocks: stores into it from now on move
     * g_vgen (the cached store paths read Dtlb.code) */
    {
        int pi = codepg_idx(c, (const uint8_t *)page);
        b->vchk = pi >= 0;
        if (pi >= 0 && !(g_codepg[pi >> 3] >> (pi & 7) & 1)) {
            g_codepg[pi >> 3] |= (uint8_t)(1u << (pi & 7));
            for (uint32_t q = 0; q < DTLB_N; q++)
                if (g_dtlb_t[q].h == (uint8_t *)page) g_dtlb_t[q].code = 1;
        }
        b->vgen = g_vgen;
    }
    b->first = page[(pc & 0xFFFu) >> 1];
    int n = 0;
    for (uint32_t a = pc; n < BMAX; a += 2) {
        if (n && ((a & 0xFFFu) == 0 || blk_special(c, a))) break;
        uint32_t op = page[(a & 0xFFFu) >> 1];
        uint32_t k = g_cls[op];
        if (!k) break;
        b->ops[n] = (uint16_t)op; b->k[n] = (uint8_t)k;
        n++;
        if (k == F_BT || k == F_BF) break;
    }
    b->n = (uint16_t)n;
    b->tkind = T_NONE;
    /* a delayed branch + an ordinary slot instruction close the block when
     * both sit on this page and neither pc is one the run loop watches */
    uint32_t a = pc + 2u * (uint32_t)n;
    if (n < BMAX && !(n && (b->k[n - 1] == F_BT || b->k[n - 1] == F_BF)) &&
        (a & 0xFFFu) <= 0xFFCu && (!n || !blk_special(c, a)) && !blk_special(c, a + 2)) {
        uint32_t bop = page[(a & 0xFFFu) >> 1], sop = page[((a + 2) & 0xFFFu) >> 1];
        int t = blk_term(bop);
        uint32_t sk = g_cls[sop];
        if (t && sk && sk != F_BT && sk != F_BF && !blk_term(sop)) {
            b->tkind = (uint8_t)t;
            b->ops[n] = (uint16_t)bop; b->k[n] = 0;
            b->ops[n + 1] = (uint16_t)sop; b->k[n + 1] = (uint8_t)sk;
        }
    }
}
static inline uint32_t blk_len(const Blk *b) { return b->n + (b->tkind ? 2u : 0u); }

/* buffers moved (Python re-bound them): forget every block */
static inline void blk_check_bufs(Core *c)
{
    uint8_t *now[7] = { c->ram0, c->ram1, c->fb0, c->fb1, c->misc, c->sq, c->flash };
    if (memcmp(now, g_blk_bufs, sizeof now)) {
        memcpy(g_blk_bufs, now, sizeof now);
        g_blk_epoch++;
    }
}

#include "shjit.c"

int core_run(Core *c, uint64_t max_iter, uint64_t *done)
{
    /* Hot-loop bookkeeping lives in locals, committed to the Core every
     * COMMIT iterations and at every exit: the old `volatile` counters
     * cost a load+add+store per instruction (2026-10-24). cur_pc is the
     * one thing committed EVERY iteration -- a fault (longjmp below) must
     * land on the exact faulting pc. After longjmp the locals are
     * indeterminate (registers rewound), so the fault path reconstructs
     * from the last commit: *done trails the true count by < COMMIT,
     * deterministic for a given instruction stream (same boundaries on
     * both sides of any A/B) and bounded by COMMIT cycles of tick time.
     * The intervening steps/cycles are under-counted, never over. */
    enum { COMMIT = 64 };
    uint32_t f_key = 0xFFFFFFFFu;            /* fetch shortcut (see below) */
    const uint16_t *f_host = 0;
    uint64_t it = 0, steps = 0, cycles = 0;
    int reason = R_LIMIT;
    c->io_break = 0;
    g_fallback = 0;
    c->run_commit = 0;
    g_dtlb_gen++;
    g_vgen++;
    if (!g_dtab_built) dtab_build();
    const int fk_on = g_dtab;
    blk_check_bufs(c);
    const int blk_on = g_blk_on && fk_on;
    if (c->mmucr & 1) { *done = 0; return R_FALLBACK; }

    if (setjmp(g_jb)) {
        /* a precise fault from the instruction at cur_pc (or its slot).
         * Return after ONE reconstructed iteration: the locals are
         * indeterminate after longjmp, so the loop cannot resume here
         * without losing its place; Python re-enters core_run for the
         * rest of the slice (faults are rare -- a storm just costs one
         * Python round trip per instruction). */
        enter_exc(c, x_off, x_code, c->cur_pc, x_hastea, x_tea);
        c->steps += 1;
        c->cycles += 1;
        *done = 1;
        return R_IO;
    }
    while (it < max_iter) {
        uint32_t pc = c->pc;
        if (!c->low_is_ram && pc == 0xA8000800u) c->low_is_ram = 1;
        if (c->idle_skip && (pc & 0x1FFFFFFFu) == c->idle_pc) { reason = R_IDLE; break; }
        if (g_fastpath && (pc == 0x08DC40B8u || pc == 0x08E1D994u)) {
            /* replay the interpreter's per-iteration accounting exactly
             * (incl. the COMMIT points the DSP clock reads) */
            if (pc == 0x08DC40B8u) {
                int term = 0;
                uint64_t n = fp_wcopy(c, max_iter - it, &term);
                if (n) {
                    g_fp_hits++; g_fp_iters += n; g_vgen++;
                    /* the per-unit replay, done in runs between COMMIT points:
                     * same totals and the same values committed at each
                     * boundary (2026-10-26; nothing can look at the counters
                     * inside a native fast path) */
                    static uint16_t ps[2][29], pcy[2][29];
                    static int pinit;
                    if (!pinit) {
                        for (int t = 0; t < 2; t++) {
                            ps[t][0] = pcy[t][0] = 0;
                            for (int u = 0; u < 26; u++) {
                                ps[t][u + 1] = (uint16_t)(ps[t][u] + 1);
                                pcy[t][u + 1] = (uint16_t)(pcy[t][u] + WC_CYC[u]);
                            }
                            if (t == 0) { ps[0][27] = (uint16_t)(ps[0][26] + 2); pcy[0][27] = (uint16_t)(pcy[0][26] + 1); }
                            else {
                                ps[1][27] = (uint16_t)(ps[1][26] + 1); pcy[1][27] = (uint16_t)(pcy[1][26] + 1);
                                ps[1][28] = (uint16_t)(ps[1][27] + 1); pcy[1][28] = (uint16_t)(pcy[1][27] + 1);
                            }
                        }
                        pinit = 1;
                    }
                    for (uint64_t i = 0; i < n; i++) {
                        int t = term && i + 1 == n;
                        uint32_t units = t ? 28u : 27u, pos = 0;
                        while (pos < units) {
                            uint32_t room = COMMIT - (uint32_t)(it & (COMMIT - 1u));
                            uint32_t take = units - pos < room ? units - pos : room;
                            steps += ps[t][pos + take] - ps[t][pos];
                            cycles += pcy[t][pos + take] - pcy[t][pos];
                            it += take; pos += take;
                            if ((it & (COMMIT - 1u)) == 0u) { c->run_commit = it;
                                c->steps += steps; c->cycles += cycles; steps = cycles = 0; }
                        }
                    }
                    continue;
                }
            } else {
                uint64_t n = fp_memset(c, max_iter - it);
                if (n) {
                    g_fp_hits++; g_fp_iters += n; g_vgen++;
                    uint64_t left = 7 * n;                 /* units of (1, 1), in runs */
                    while (left) {
                        uint64_t room = COMMIT - (it & (COMMIT - 1u));
                        uint64_t take = left < room ? left : room;
                        steps += take; cycles += take; it += take; left -= take;
                        if ((it & (COMMIT - 1u)) == 0u) { c->run_commit = it;
                            c->steps += steps; c->cycles += cycles; steps = cycles = 0; }
                    }
                    continue;
                }
            }
        }
        if (g_watch_n) {
            for (uint32_t w = 0; w < g_watch_n; w++)
                if (g_watch_pc[w] == pc) g_watch_hit[w]++;
        }
        c->cur_pc = pc;
        /* 2026-10-26 fetch shortcut: the host pointer of the current 4 KB
         * code page, valid while privilege (MD) and the low-flash mapping
         * are unchanged -- one load instead of xlat + loc per instruction.
         * Odd pcs and non-RAM pages take the full path (same faults). */
        uint32_t op;
        uint32_t fkey = (pc & ~0xFFFu) | ((c->sr & MD) ? 1u : 0u) | (c->low_is_ram ? 2u : 0u);
        if (fkey == f_key && !(pc & 1u)) {
            op = (uint32_t)f_host[(pc & 0xFFFu) >> 1];
        } else {
            op = RD16(pc);
            f_key = 0xFFFFFFFFu;
            if (!(pc & 1u) && g_fastpath) {
                uint32_t p = xlat(c, pc);
                int fb, ro;
                uint8_t *h = loc(c, p & ~0xFFFu, &fb, &ro);
                int fb2, ro2;
                if (h && !fb && loc(c, (p & ~0xFFFu) + 0xFFEu, &fb2, &ro2) == h + 0xFFE) {
                    f_host = (const uint16_t *)h;
                    f_key = fkey;
                }
            }
        }
        if (blk_on && f_key == fkey && !(pc & 1u) && !g_watch_n) {
            Blk *b = &g_blk[BLK_IDX(pc)];
            const uint16_t *hp = f_host + ((pc & 0xFFFu) >> 1);
            if (b->pc != pc || b->key != fkey || b->epoch != g_blk_epoch ||
                b->idle != c->idle_pc || b->host != (const uint8_t *)f_host)
                blk_build(c, b, pc, fkey, f_host);
#ifdef SHJIT
            else if (b->fn && g_jit_on && !g_watch_n && it + blk_len(b) <= max_iter &&
                     (it & (COMMIT - 1u)) + blk_len(b) < COMMIT) {
                /* compiled: the code checks its own opcodes on entry */
                JCtx x = { it, steps, cycles, max_iter, 0, 0 };
                jit_enter(c, (void *)b->fn, &x);
                g_jit_calls++;
                g_jit_its += x.it - it;
                it = x.it; steps = x.steps; cycles = x.cycles;
                if (x.flags & 1u) { reason = R_IO; goto out; }
                /* flags 2: a block's code changed; it unhooked itself */
                continue;
            }
#endif
            else if (!b->vchk || b->vgen != g_vgen) {
                if (blk_len(b) ? memcmp(hp, b->ops, (size_t)blk_len(b) * 2u) != 0 : hp[0] != b->first)
                    blk_build(c, b, pc, fkey, f_host);
                else b->vgen = g_vgen;
            }
            uint32_t bn = b->n;
#ifdef SHJIT
            /* native code, when the longest path fits the slice and no
             * COMMIT point falls inside it */
            uint32_t units = blk_len(b);
            if (units >= 2 && g_jit_on && !g_watch_n) {
                if (!b->fn && ++b->hot >= JIT_HOT) jit_compile(c, b);
                if (b->fn && it + units <= max_iter && (it & (COMMIT - 1u)) + units < COMMIT) {
                    JCtx x = { it, steps, cycles, max_iter, 0, 0 };
                    jit_enter(c, (void *)b->fn, &x);
                    g_jit_calls++;
                    g_jit_its += x.it - it;
                    it = x.it; steps = x.steps; cycles = x.cycles;
                    if (x.flags) { reason = R_IO; goto out; }
                    continue;
                }
            }
#endif
            if (bn >= 2 && it + bn <= max_iter) {
                uint32_t bp = pc;
                g_blk_runs++;
                for (uint32_t j = 0; j < bn; j++) {
                    c->pc = bp; c->cur_pc = bp;
                    int br = 0; uint32_t tgt = 0;
                    int mem = blk_is_mem(b->k[j]);
                    if (mem) { g_wslow = 0; g_wlast = 0; }
                    uint32_t cy = fast_step(c, b->ops[j], b->k[j], &br, &tgt);
                    steps++; cycles += cy; it++;
                    g_blk_insns++;
                    if ((it & (COMMIT - 1u)) == 0u) {
                        c->run_commit = it;
                        c->steps += steps;
                        c->cycles += cycles;
                        steps = cycles = 0;
                    }
                    if (br) { bp = tgt; break; }
                    bp += 2;
                    if (c->io_break) { c->pc = bp; reason = R_IO; goto out; }
                    if (mem && (g_wslow ||
                                (uintptr_t)(g_wlast - b->host) < 0x1000u)) break;
                }
                c->pc = bp;
                continue;
            }
        }
        uint32_t nxt = pc + 2;
        if (fk_on) {
            uint32_t k = g_cls[op];
            if (k) {
                int br = 0; uint32_t tgt = 0;
                uint32_t cy = fast_step(c, op, k, &br, &tgt);
                c->pc = br ? tgt : nxt;
                steps++; cycles += cy;
                goto tail;
            }
        }
        Res r;
        step(c, op, nxt, &r);
        if (g_fallback) { c->pc = pc; reason = R_FALLBACK; break; }
        steps++;
        switch (r.kind) {
        case K_NONE: c->pc = nxt; cycles += 1; break;
        case K_TIMED: c->pc = nxt; cycles += r.cyc; break;
        case K_BR: c->pc = r.tgt; cycles += r.cyc; break;
        case K_EXC: cycles += r.cyc; break;
        case K_ADV4: c->pc = pc + 4; cycles += r.cyc; break;
        case K_DBR: case K_RTE: {
            cycles += r.cyc;
            uint32_t dop = RD16(nxt);
            c->pc = nxt;
            Res d;
            step(c, dop, nxt + 2, &d);
            if (g_fallback) { c->pc = pc; reason = R_FALLBACK; goto out; }
            if (d.kind == K_BR || d.kind == K_DBR || d.kind == K_RTE || d.kind == K_EXC) {
                enter_exc(c, 0x100, 0x1A0, nxt, 0, 0);
            } else {
                steps++;
                if (r.kind == K_RTE) { set_sr(c, r.rssr); c->pc = r.tgt; }
                else c->pc = r.tgt;
            }
            break;
        }
        }
    tail:
        it++;
        if ((it & (COMMIT - 1u)) == 0u) {
            c->run_commit = it;
            c->steps += steps;
            c->cycles += cycles;
            steps = cycles = 0;
        }
        if (c->sleeping) { reason = R_SLEEP; break; }
        if (c->io_break) { reason = R_IO; break; }
    }
out:
    c->steps += steps;
    c->cycles += cycles;
    *done = it;
    return reason;
}

/* bulk helpers for Python-side tools */
uint32_t core_read_virt(Core *c, uint32_t a, int n, int *ok)
{
    if (setjmp(g_jb)) { *ok = 0; return 0; }
    uint32_t v = vrd(c, a, n);
    *ok = 1;
    return v;
}

/* HPID log accessor: returns a pointer to 2*n uint32s (addr, value pairs)
 * written through the CS6 data port. Caller must not free. */
uint32_t *core_hpid(Core *c, uint64_t *n)
{
    if (n) *n = c->hpid_n;
    return c->hpid_buf;
}

/* DSP RAM regions for the cs6_fast HPID read serving (read-only views of
 * the bytearrays the C6x core runs over; fastcpu.py keeps them alive). */
void core_clear_dsp_regions(Core *c) { c->dsp_region_n = 0; }

int core_add_dsp_region(Core *c, uint32_t base, uint32_t len, void *ptr)
{
    if (c->dsp_region_n >= 8) return -1;
    c->dsp_region[c->dsp_region_n].base = base;
    c->dsp_region[c->dsp_region_n].len = len;
    c->dsp_region[c->dsp_region_n].ptr = (uint8_t *)ptr;
    c->dsp_region_n++;
    return 0;
}

int core_sizeof(void) { return (int)sizeof(Core); }

/* --------------------------------------------------------------------- *
 * 2DG display-list executor in C (2026-10-26). Exact port of
 * emu/tdg.py TDG.run's ops (fill / blit / textured quad / polygon / line,
 * WPR, LCOFS, MOVE, NOP/INTERRUPT, SYNC, TRAP) for lists whose memory is
 * plain host RAM. Python stays authoritative: it passes its registers in,
 * gets back the words C changed, the SSR bits, interrupts to raise and the
 * framebuffer byte count -- and on anything this code does not handle
 * (unknown op, WPR outside 0x000-0x0FC, memory that is not plain RAM) it
 * stops BEFORE that op and Python continues the same list from stop_pc.
 * Every op validates all of its memory before writing a single pixel, so a
 * stop never leaves an op half done.
 * --------------------------------------------------------------------- */
typedef struct {
    uint32_t reg[64];           /* in/out: registers 0x000..0x0FC */
    uint64_t dirty;             /* out: words written by WPR */
    int32_t lcx, lcy;           /* in/out: LCOFS */
    uint32_t ssr_or;            /* out: SSR bits to set */
    uint32_t irq_ini, irq_tri;  /* out: interrupts to raise (count / flag) */
    uint32_t stop_pc;           /* out: where Python continues (fallback) */
    uint32_t ops;               /* out: ops executed */
    uint64_t fb_bytes;          /* out: framebuffer bytes written */
} TdgIO;

static inline int32_t s16(uint32_t v) { return (int32_t)(int16_t)(v & 0xFFFFu); }
static inline uint32_t tphys(uint32_t a)
{
    return (a - 0x80000000u < 0x40000000u) ? (a & 0x1FFFFFFFu) : a;
}
/* host pointer for [a, a+n) as one plain buffer; writable: not flash */
static uint8_t *thost(Core *c, int64_t a, int64_t n, int writable, int *fb)
{
    if (a < 0 || a > 0xFFFFFFFFll || n <= 0) return 0;
    uint32_t p = tphys((uint32_t)a);
    int f1, r1, f2, r2;
    uint8_t *h = loc(c, p, &f1, &r1);
    if (!h || (writable && r1)) return 0;
    uint8_t *h2 = loc(c, p + (uint32_t)(n - 1), &f2, &r2);
    if (h2 != h + (n - 1) || f1 != f2) return 0;
    if (fb) *fb = f1;
    return h;
}
static int trd(Core *c, uint32_t a, uint32_t *v)
{
    uint8_t *h = thost(c, a, 4, 0, 0);
    if (!h || (tphys(a) & 3u)) return 0;     /* unaligned: Python's job */
    memcpy(v, h, 4);
    return 1;
}
#define REG(o) (io->reg[(o) >> 2])

static void tclip(TdgIO *io, uint32_t flags, int32_t *x1, int32_t *y1, int32_t *x2, int32_t *y2)
{
    uint32_t smax = REG(0xD0);
    *x1 = 0; *y1 = 0;
    if (smax) { *x2 = s16(smax >> 16); *y2 = s16(smax); }
    else { *x2 = 0x7FFF; *y2 = 0x7FFF; }
    if (flags & 0x2000u) {
        int32_t cx1 = s16(REG(0xD4) >> 16), cy1 = s16(REG(0xD4));
        int32_t cx2 = s16(REG(0xD8) >> 16), cy2 = s16(REG(0xD8));
        if (cx1 > *x1) *x1 = cx1;
        if (cy1 > *y1) *y1 = cy1;
        if (cx2 < *x2) *x2 = cx2;
        if (cy2 < *y2) *y2 = cy2;
    }
}

/* 0 = done, -1 = cannot (nothing written) */
static int tfill(Core *c, TdgIO *io, uint32_t flags, uint32_t colour,
                 int32_t x, int32_t y, int32_t w, int32_t h)
{
    uint32_t dst = REG(0x50);
    int64_t stride = REG(0x5C) ? (int64_t)REG(0x5C) : 800;
    x += io->lcx; y += io->lcy;
    int32_t cx1, cy1, cx2, cy2;
    tclip(io, flags, &cx1, &cy1, &cx2, &cy2);
    int32_t xa = x > cx1 ? x : cx1, xb = (x + w - 1) < cx2 ? (x + w - 1) : cx2;
    int32_t ya = y > cy1 ? y : cy1, yb = (y + h - 1) < cy2 ? (y + h - 1) : cy2;
    if (xa > xb || ya > yb) return 0;
    int64_t a0 = (int64_t)dst + ((int64_t)ya * stride + xa) * 2;
    int64_t n = (((int64_t)(yb - ya)) * stride + (xb - xa + 1)) * 2;
    int fb = 0;
    uint8_t *h0 = thost(c, a0, n, 1, &fb);
    if (!h0 || (a0 & 1)) return -1;
    uint16_t c16 = (uint16_t)colour;
    for (int32_t yy = ya; yy <= yb; yy++) {
        uint8_t *row = h0 + ((int64_t)(yy - ya) * stride) * 2;
        for (int32_t xx = 0; xx <= xb - xa; xx++) memcpy(row + 2 * xx, &c16, 2);
    }
    if (fb) io->fb_bytes += (uint64_t)(yb - ya + 1) * (uint64_t)(xb - xa + 1) * 2;
    return 0;
}

static int tblit(Core *c, TdgIO *io, uint32_t flags, int32_t sx, int32_t sy,
                 int32_t x, int32_t y, int32_t w, int32_t h)
{
    uint32_t src = REG(0x4C), dst = REG(0x50);
    int64_t sstride = REG(0x58) ? (int64_t)REG(0x58) : 800;
    int64_t dstride = REG(0x5C) ? (int64_t)REG(0x5C) : 800;
    x += io->lcx; y += io->lcy;
    int32_t cx1, cy1, cx2, cy2;
    tclip(io, flags, &cx1, &cy1, &cx2, &cy2);
    int keyed = (flags & 0x0800u) != 0;
    uint16_t key = (uint16_t)REG(0x80);
    int32_t ya = y > cy1 ? y : cy1, yb = (y + h - 1) < cy2 ? (y + h - 1) : cy2;
    int32_t xa = x > cx1 ? x : cx1, xb = (x + w - 1) < cx2 ? (x + w - 1) : cx2;
    if (ya > yb || xa > xb) return 0;
    int32_t rows = yb - ya + 1, cols = xb - xa + 1;
    int64_t s0 = (int64_t)src + (((int64_t)sy + (ya - y)) * sstride + sx + (xa - x)) * 2;
    int64_t d0 = (int64_t)dst + ((int64_t)ya * dstride + xa) * 2;
    int fbs = 0, fbd = 0;
    uint8_t *hs = thost(c, s0, (((int64_t)rows - 1) * sstride + cols) * 2, 0, &fbs);
    uint8_t *hd = thost(c, d0, (((int64_t)rows - 1) * dstride + cols) * 2, 1, &fbd);
    if (!hs || !hd || (s0 & 1) || (d0 & 1)) return -1;
    static uint16_t tmp[4096];
    if (cols > 4096) return -1;
    for (int32_t j = 0; j < rows; j++) {            /* ascending rows, each row
        read before written = the Python row path (and its vectorised path
        when the rectangles do not overlap) */
        uint8_t *sr = hs + (int64_t)j * sstride * 2, *dr = hd + (int64_t)j * dstride * 2;
        memcpy(tmp, sr, (size_t)cols * 2);
        if (!keyed) memcpy(dr, tmp, (size_t)cols * 2);
        else
            for (int32_t i = 0; i < cols; i++)
                if (tmp[i] != key) memcpy(dr + 2 * i, &tmp[i], 2);
    }
    if (fbd) io->fb_bytes += (uint64_t)rows * (uint64_t)cols * 2;
    return 0;
}

static int tquad(Core *c, TdgIO *io, uint32_t flags, int32_t sx, int32_t sy,
                 int32_t sw, int32_t sh, const int32_t *px, const int32_t *py)
{
    int32_t x0 = px[0], y0 = py[0], x1 = px[0], y1 = py[0];
    for (int k = 1; k < 4; k++) {
        if (px[k] < x0) x0 = px[k];
        if (px[k] > x1) x1 = px[k];
        if (py[k] < y0) y0 = py[k];
        if (py[k] > y1) y1 = py[k];
    }
    int32_t w = x1 - x0 + 1, h = y1 - y0 + 1;
    if (sw <= 0 || sh <= 0) return 0;
    if (w == sw && h == sh) return tblit(c, io, flags, sx, sy, x0, y0, w, h);
    uint32_t src = REG(0x4C), dst = REG(0x50);
    int64_t sstride = REG(0x58) ? (int64_t)REG(0x58) : 800;
    int64_t dstride = REG(0x5C) ? (int64_t)REG(0x5C) : 800;
    int32_t ox = io->lcx, oy = io->lcy;
    int32_t cx1, cy1, cx2, cy2;
    tclip(io, flags, &cx1, &cy1, &cx2, &cy2);
    int keyed = (flags & 0x0800u) != 0;
    uint16_t key = (uint16_t)REG(0x80);
    if (w > 4096 || sw > 4096) return -1;
    /* validate every row first (nothing written on a stop) */
    int fb = 0, any = 0;
    for (int32_t j = 0; j < h; j++) {
        int32_t yy = y0 + j + oy;
        if (yy < cy1 || yy > cy2) continue;
        int64_t sa = (int64_t)src + (((int64_t)sy + (int64_t)j * sh / h) * sstride + sx) * 2;
        int64_t da = (int64_t)dst + ((int64_t)yy * dstride + x0 + ox) * 2;
        int f2 = 0;
        if (!thost(c, sa, (int64_t)sw * 2, 0, 0) || !thost(c, da, (int64_t)w * 2, 1, &f2)
            || (sa & 1) || (da & 1))
            return -1;
        fb |= f2; any = 1;
    }
    (void)any;
    static uint16_t sv[4096], dv[4096];
    for (int32_t j = 0; j < h; j++) {
        int32_t yy = y0 + j + oy;
        if (yy < cy1 || yy > cy2) continue;
        int64_t sa = (int64_t)src + (((int64_t)sy + (int64_t)j * sh / h) * sstride + sx) * 2;
        int64_t da = (int64_t)dst + ((int64_t)yy * dstride + x0 + ox) * 2;
        int f2 = 0;
        uint8_t *hs = thost(c, sa, (int64_t)sw * 2, 0, 0);
        uint8_t *hd = thost(c, da, (int64_t)w * 2, 1, &f2);
        memcpy(sv, hs, (size_t)sw * 2);
        memcpy(dv, hd, (size_t)w * 2);
        for (int32_t i = 0; i < w; i++) {
            int32_t xx = i + x0 + ox;
            if (xx < cx1 || xx > cx2) continue;
            uint16_t s = sv[(int64_t)i * sw / w];
            if (keyed && s == key) continue;
            dv[i] = s;
        }
        memcpy(hd, dv, (size_t)w * 2);
        if (f2) io->fb_bytes += (uint64_t)w * 2;
    }
    return 0;
}

static int tline(Core *c, TdgIO *io, uint32_t flags, uint32_t colour,
                 const int32_t *px, const int32_t *py, uint32_t n)
{
    /* validate: each point fill must be possible -- done lazily: a stop on
     * a later point would leave earlier points drawn, so check all first */
    TdgIO save = *io;
    for (uint32_t k = 0; k + 1 < n; k++) {
        int32_t x0 = px[k], y0 = py[k], x1 = px[k + 1], y1 = py[k + 1];
        int32_t dx = x1 > x0 ? x1 - x0 : x0 - x1, dy = -(y1 > y0 ? y1 - y0 : y0 - y1);
        int32_t sxs = x0 < x1 ? 1 : -1, sys_ = y0 < y1 ? 1 : -1, err = dx + dy;
        for (;;) {
            /* dry check of the 1x1 fill */
            uint32_t dst = REG(0x50);
            int64_t stride = REG(0x5C) ? (int64_t)REG(0x5C) : 800;
            int32_t cx1, cy1, cx2, cy2;
            tclip(io, flags, &cx1, &cy1, &cx2, &cy2);
            int32_t xx = x0 + io->lcx, yy = y0 + io->lcy;
            if (xx >= cx1 && xx <= cx2 && yy >= cy1 && yy <= cy2) {
                int64_t a = (int64_t)dst + ((int64_t)yy * stride + xx) * 2;
                if (!thost(c, a, 2, 1, 0) || (a & 1)) return -1;
            }
            if (x0 == x1 && y0 == y1) break;
            int32_t e2 = 2 * err;
            if (e2 >= dy) { err += dy; x0 += sxs; }
            if (e2 <= dx) { err += dx; y0 += sys_; }
        }
    }
    *io = save;
    for (uint32_t k = 0; k + 1 < n; k++) {
        int32_t x0 = px[k], y0 = py[k], x1 = px[k + 1], y1 = py[k + 1];
        int32_t dx = x1 > x0 ? x1 - x0 : x0 - x1, dy = -(y1 > y0 ? y1 - y0 : y0 - y1);
        int32_t sxs = x0 < x1 ? 1 : -1, sys_ = y0 < y1 ? 1 : -1, err = dx + dy;
        for (;;) {
            tfill(c, io, flags, colour, x0, y0, 1, 1);
            if (x0 == x1 && y0 == y1) break;
            int32_t e2 = 2 * err;
            if (e2 >= dy) { err += dy; x0 += sxs; }
            if (e2 <= dx) { err += dx; y0 += sys_; }
        }
    }
    return 0;
}

/* returns 0 = list finished (TRAP or op limit), 1 = stopped: Python
 * continues at io->stop_pc */
int core_tdg_run(Core *c, uint32_t addr, TdgIO *io, uint32_t limit)
{
    uint32_t pc = addr, n = 0;
    io->dirty = 0; io->ssr_or = 0; io->irq_ini = 0; io->irq_tri = 0;
    io->ops = 0; io->fb_bytes = 0; io->stop_pc = 0;
    /* no setjmp here: this runs NESTED inside core_run (SCLR write ->
     * Python -> here) and must not clobber its jmp_buf; nothing below
     * can longjmp (loc() never raises) */
#define STOP() do { io->stop_pc = pc; return 1; } while (0)
#define RD(a, v) do { if (!trd(c, (a), (v))) STOP(); } while (0)
    while (n < limit) {
        n++;
        uint32_t w;
        RD(pc, &w);
        uint32_t op = w >> 24, flags = w & 0x00FFFFFFu;
        if (op == 0x00) {
            io->ssr_or |= 1u;
            if (REG(0x0C) & 1u) io->irq_tri = 1;
            io->ops++;
            return 0;
        } else if (op == 0x18) {
            uint32_t r, v;
            RD(pc + 4, &r); RD(pc + 8, &v);
            r &= 0xFFFFu;
            if (r >= 0x100u || (r & 3u)) STOP();
            io->reg[r >> 2] = v;
            io->dirty |= 1ull << (r >> 2);
            pc += 12;
        } else if (op == 0x40) {
            uint32_t v; RD(pc + 4, &v);
            io->lcx = s16(v >> 16); io->lcy = s16(v);
            pc += 8;
        } else if (op == 0x48) {
            pc += 8;
        } else if (op == 0x08) {
            if (w & 0x8000u) io->irq_ini++;
            pc += 4;
        } else if (op == 0x12) {
            pc += 4;
        } else if (op == 0xA0) {
            uint32_t col, lr, ud, xy;
            RD(pc + 8, &col); RD(pc + 12, &lr); RD(pc + 16, &ud); RD(pc + 20, &xy);
            int32_t l = s16(lr >> 16), r = s16(lr), u = s16(ud >> 16), d = s16(ud);
            int32_t x = s16(xy >> 16), y = s16(xy);
            if (tfill(c, io, flags, col, x - l, y - u, l + r + 1, u + d + 1)) STOP();
            pc += 24;
        } else if (op == 0xA2) {
            uint32_t sxy, lr, ud, xy;
            RD(pc + 8, &sxy); RD(pc + 12, &lr); RD(pc + 16, &ud); RD(pc + 20, &xy);
            int32_t l = s16(lr >> 16), r = s16(lr), u = s16(ud >> 16), d = s16(ud);
            int32_t sx = s16(sxy >> 16), sy = s16(sxy), x = s16(xy >> 16), y = s16(xy);
            if (tblit(c, io, flags, sx - l, sy - u, x - l, y - u, l + r + 1, u + d + 1)) STOP();
            pc += 24;
        } else if (op == 0x82) {
            uint32_t sxy, swh, p[4];
            RD(pc + 4, &sxy); RD(pc + 8, &swh);
            for (int k = 0; k < 4; k++) RD(pc + 16 + 4 * k, &p[k]);
            int32_t px[4], py[4];
            for (int k = 0; k < 4; k++) { px[k] = s16(p[k] >> 16); py[k] = s16(p[k]); }
            if (tquad(c, io, flags, s16(sxy >> 16), s16(sxy), s16(swh >> 16), s16(swh), px, py))
                STOP();
            pc += 32;
        } else if (op == 0x80) {
            uint32_t col, p[4];
            RD(pc + 4, &col);
            for (int k = 0; k < 4; k++) RD(pc + 8 + 4 * k, &p[k]);
            int32_t x0 = s16(p[0] >> 16), y0 = s16(p[0]), x1 = x0, y1 = y0;
            for (int k = 1; k < 4; k++) {
                int32_t xx = s16(p[k] >> 16), yy = s16(p[k]);
                if (xx < x0) x0 = xx;
                if (xx > x1) x1 = xx;
                if (yy < y0) y0 = yy;
                if (yy > y1) y1 = yy;
            }
            if (tfill(c, io, flags, col, x0, y0, x1 - x0 + 1, y1 - y0 + 1)) STOP();
            pc += 24;
        } else if (op == 0xB0) {
            uint32_t cw; RD(pc + 4, &cw);
            uint32_t cnt = cw & 0xFFFFu;
            if (cnt > 256) STOP();
            int32_t px[256], py[256];
            for (uint32_t k = 0; k < cnt; k++) {
                uint32_t v; RD(pc + 12 + 4 * k, &v);
                px[k] = s16(v >> 16); py[k] = s16(v);
            }
            if (tline(c, io, flags, cw >> 16, px, py, cnt)) STOP();
            pc += 12 + 4 * cnt;
        } else {
            STOP();                            /* unknown: Python reports it */
        }
        io->ops++;
    }
#undef STOP
#undef RD
    return 0;                                  /* op limit (as Python) */
}

/* SCLR start from C (tdg_cfast): same effects, in the same order, as the
 * Python path TDG.write -> TDG.run -> fastcpu._tdg_cexec */
static void tdg_sclr(Core *c)
{
    TdgIO io;
    memcpy(io.reg, c->tdg_reg, sizeof io.reg);
    io.lcx = c->tdg_lcx; io.lcy = c->tdg_lcy;
    c->tdg_runs_c++;
    int r = core_tdg_run(c, c->tdg_reg[0x48u >> 2], &io, 200000u);
    for (uint32_t w = 0; w < 64; w++)
        if (io.dirty >> w & 1u) c->tdg_reg[w] = io.reg[w];
    c->tdg_dirty |= io.dirty;
    if (io.lcx != c->tdg_lcx || io.lcy != c->tdg_lcy) {
        c->tdg_lcx = io.lcx; c->tdg_lcy = io.lcy; c->tdg_lc_dirty = 1;
    }
    c->fb_writes += io.fb_bytes;
    if (io.ssr_or) { c->tdg_ssr |= io.ssr_or; c->tdg_dirty_ssr = 1; }
    for (uint32_t i = 0; i < io.irq_ini; i++) c->irq_cb(0);
    if (io.irq_tri) c->irq_cb(1);
    if (io.irq_ini || io.irq_tri) c->io_break = 1;
    if (r) c->tdg_cont(io.stop_pc, io.ops);   /* sets io_break itself */
}
