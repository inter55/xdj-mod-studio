/* c6xcore.c -- the C674x DSP core of emu/c6x/cpu.py + isa.py, in C.
 *
 * Same machine model, cycle for cycle (TI SPRUFE8B; see cpu.py docstring):
 *   - an execute packet issues in one cycle; members read before writes;
 *     results land at the END of cycle issue+w;
 *   - loads read memory at the end of issue+3, register at issue+4;
 *     stores reach memory at the end of issue+2; within a cycle: stores,
 *     then loads, then register/control writes;
 *   - branches redirect pc at the end of issue+5 (several in flight);
 *     a landing branch ends a multi-cycle NOP;
 *   - maskable interrupts INT4..15 at EP boundaries;
 *   - SPLOOP/SPLOOPD/SPLOOPW loop buffer as isa.SPLoop.
 * Instructions are pre-decoded by emu/c6x/cfast.py into Ins records.
 * Anything not handled here is O_py: C calls back into Python, which runs
 * the isa.py handler against a proxy of this core (rare instructions).
 * cfast.py reads the OP(...) names below in order (one source of truth).
 */
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include <math.h>
#include <stddef.h>

/* the hot helpers inlined into step(): +15-20% (2026-10-21) */
#if defined(__GNUC__) || defined(__clang__)
#define ALWAYS __attribute__((always_inline))
#else
#define ALWAYS
#endif

typedef uint32_t u32;
typedef int32_t s32;
typedef uint64_t u64;
typedef int64_t s64;

#define OPS \
 OP(py) OP(nop) OP(mvk) OP(mvkh) OP(mv) OP(mvpair) OP(dmv) OP(zero) OP(zeropair) \
 OP(mvc_to) OP(mvc_from) OP(add) OP(sub) OP(addk) OP(adda) OP(suba) \
 OP(and) OP(or) OP(xor) OP(andn) OP(sadd) OP(ssub) OP(add2) OP(sub2) \
 OP(pack2) OP(packh2) OP(packhl2) OP(packlh2) OP(packl4) OP(packh4) \
 OP(neg) OP(not) OP(abs) OP(cmp) OP(shl) OP(shr) OP(shru) OP(sshl) \
 OP(ext) OP(extu) OP(set) OP(clr) OP(lmbd) OP(norm) OP(mpy16) OP(mpy32) \
 OP(mpy32p) OP(ld) OP(st) OP(b) OP(bnop) OP(callp) OP(addkpc) OP(bdec) \
 OP(sploop) OP(spctl) OP(dint) OP(rint) OP(idle) \
 OP(fadd) OP(fsub) OP(fmul) OP(intsp) OP(spint) OP(spdp) OP(dpsp) \
 OP(dadd) OP(dsub) OP(dmul) OP(fcmp) OP(dcmp) OP(abssp) OP(rcpsp) OP(swap2) \
 OP(mvd) OP(mpyxi)

#define OP(n) O_##n,
enum { OPS O_COUNT };
#undef OP

/* pre-decoded instruction; layout mirrored by cfast.Ins (ctypes) */
typedef struct {
    u32 addr;
    s32 k[4];
    u32 py;            /* Python fallback id */
    uint16_t op;
    uint8_t size, par, header, prot;
    int8_t creg;       /* predicate register or -1 */
    uint8_t cneg;
    uint8_t unit;      /* 0..7 L1 L2 S1 S2 M1 M2 D1 D2, 255 none */
    uint8_t side;      /* 1 A, 2 B, 0 none */
    uint8_t w;         /* write delay */
    uint8_t sub;       /* op variant */
    uint8_t ra, rb, rd, rah, rbh, rdh;
    uint8_t ca, cb;    /* srcA/srcB constant (k0/k1) */
    uint8_t msz, mmode, mbase, mofk, moff, mscale, msign, malign;
    uint8_t ctrl;
    uint8_t spmask;    /* SPMASK unit bits */
    uint8_t spk;       /* 1 spmask, 2 spkernel, 3 nested-reload forms */
    uint8_t pad;
} Ins;

enum { M_PLAIN, M_PADD, M_PSUB, M_PREINC, M_PREDEC, M_POSTINC, M_POSTDEC };

enum { C_AMR, C_CSR, C_IFR, C_ISR, C_ICR, C_IER, C_ISTP, C_IRP, C_NRP, C_ILC,
       C_RILC, C_SSR, C_TSR, C_ITSR, C_NTSR, C_ECR, C_EFR, C_IERR, C_REP,
       C_TSCL, C_TSCH, C_DNUM, C_GFPGFR, C_GPLYA, C_GPLYB, C_FADCR, C_FAUCR,
       C_FMCR, C_DIER, C_PCE1, C_NCTRL };

enum { E_ST, E_LD, E_REG, E_CTRL, E_PYLD };
typedef struct { uint8_t type, size, lo, hi, sx; u32 addr; u64 val; } Ev;
#define RING 16
#define EVMAX 128
/* per-cycle bucket: register writes (the bulk) in their own lean lane,
 * everything else (stores, loads, control writes) as Ev; `types` set when
 * a memory event is present. Register and control writes touch different
 * state, so applying the two lanes separately keeps the result exact. */
typedef struct { int n; int types; int rn; uint8_t ri[EVMAX]; u32 rv[EVMAX]; Ev e[EVMAX]; } Bucket;

typedef u32 (*mmio_rd_t)(u32 addr, int size);
typedef void (*mmio_wr_t)(u32 addr, int size, u32 val);
typedef int (*pyop_t)(u32 id, u64 cyc);
typedef void (*pyld_t)(u32 id, u32 lo, u32 hi);
typedef struct { u32 lo; u64 hi; uint8_t *buf; } Region;

#define SPBUF 1024
#define SPLC 1024
#define SPRES 32
typedef struct {
    int kind, ii, wcreg, wcneg, loading, fetch_prog, lc, lbc, dynlen, drain,
        post_delay, pc_after, since, initial_term, prog_stall, refill;
    int resume, int_drain; u32 addr;
    int nbuf;
    /* buffer entries chained by stage residue lc % ii (index + 1, 0 =
     * none), so a cycle visits only its own stage's entries, in buffer
     * order; reset with the head above (ii > SPRES: plain scan) */
    int rhead[SPRES], rtail[SPRES];
    int bl[SPBUF], bi[SPBUF], bu[SPBUF], rnext[SPBUF];
    uint8_t valid[SPLC];
    int pv[64], npv;
    uint8_t hist[4]; int nhist;
} SPLoop;

enum { K_SPLOOP, K_SPLOOPD, K_SPLOOPW };
#define SPLX (1u << 9)          /* TSR.SPLX */
enum { ST_OK, ST_NEED, ST_ERR, ST_STOP, ST_IDLE };

#define NPEND 16
/* execute packet starting at an address: members (Ins indexes) and the
 * address after it (headers skipped) -- fetch_ep's work done once */
struct EPC { u32 pc, nxt; int n, succ; int32_t idx[16]; };   /* succ: packet at nxt, -1 unknown */

typedef struct {
    u32 r[64];
    u32 c[C_NCTRL];
    u32 pc;
    u32 idle;
    u64 cycle;
    int stall;
    u64 ep_count, insn_count, interrupts;
    int npend;
    u64 pend_cyc[NPEND];
    u32 pend_tgt[NPEND];
    Bucket ring[RING];
    int evcount;
    int sp_on;
    SPLoop sp;
    Ins *ins;
    int nins, cap;
    int32_t *epi;      /* per Ins: execute-packet cache entry, -1 unknown */
    struct EPC *epc; int nepc, capepc;
    int lastreg;       /* ram(): region of the last hit */
    int hint, prevep, lastf;   /* step(): expected next packet, last run, last fetched */
    u32 *hkey; int32_t *hval; u32 hmask;
    Region reg[16];
    int nreg;
    mmio_rd_t mrd; mmio_wr_t mwr; pyop_t pyop; pyld_t pyld;
    int status;
    u32 need;
    int err; u32 err_pc;
    u64 py_calls;
    int splx_irq; u32 splx_after;
    u32 idle_head, idle_lo, idle_hi; int idle_armed, idle_dirty; u32 idle_r[64];
    /* busy-wait heads (step()): idle_head plus extra ones, all loops of the
     * idle task (same stack); the state seen at the last head visit */
    u32 idle_heads[4]; int nidle, idle_last;
    u32 idle_c[C_NCTRL]; uint8_t *idle_stk; u32 idle_stkn;
    u64 idle_cyc;               /* cycle of the last head visit */
    u64 run_end;                /* c6x_run: the cycle this call stops at (0 = none) */
    u64 idle_jump;              /* step() -> c6x_run: cycles just jumped */
    u64 idle_jumped, idle_jumps;   /* statistics */
    /* the reference pass's read set (every RAM load: address, size, value),
     * so a pass can be trusted across c6x_run calls (see step()) */
    #define RSMAX 512
    u32 rs_a[RSMAX]; uint8_t rs_n[RSMAX]; u64 rs_v[RSMAX];
    int rs_cnt, rs_on, rs_over;
    u32 sw_a[RSMAX]; uint8_t sw_n[RSMAX]; int sw_cnt;   /* the pass's own stack writes */
    int idle_fresh;
    /* the reference pass: entry state, read/write set, length */
    int ref_ok, ref_hk, ref_cnt; u64 ref_p, idle_refhits;
    u32 ref_r[64], ref_c[C_NCTRL]; uint8_t *ref_stk;
    u32 ref_a[RSMAX]; uint8_t ref_n[RSMAX]; u64 ref_v[RSMAX];
    u32 wlo, whi; int wn; u32 wpc[256], wad[256], wv[256]; u64 wcy[256];
    /* delay-loop fast-forward (see dff_* below) */
    int dff_on, dff_nt, dff_learn;
    u64 dff_hits, dff_cycles;
    struct { s32 n; u64 T, dep, dins; SPLoop sp; } dff_t[4];
    struct { s32 n; u32 r[64], c[C_NCTRL], b3, sp; u64 cyc, ep, ins, ints, py; } dff_pre;
    /* frozen call in progress (interrupts off): entry state for settle() */
    int dff_frozen, dff_fk, dff_replay;
    u64 dff_end;
#ifdef C6X_PROF
    /* measurement build only (-DC6X_PROF): instruction-start histogram,
     * exposed by c6x_hist(). Never present in the shipped core. */
    u32 *hist;
    u64 cyc_sp, cyc_norm;             /* cycles inside / outside a SPLOOP */
    u64 prof_stall, prof_run;
    int spn; u32 spa[8]; u64 spc[8];  /* SPLOOP cycles by loop address */
#endif
    struct { u32 r[64], word; u64 cyc, ep, ins; int hint, prevep, lastf, idle_dirty; SPLoop sp; } dff_in;
} Core;

#define DFF_PC 0xC004BEE0u     /* delay routine (see dff_enter) */

/* ================================================== code table ======== */
static inline u32 hslot(Core *c, u32 a)
{
    u32 h = (a * 2654435761u) & c->hmask;
    while (c->hkey[h] != 0xFFFFFFFFu && c->hkey[h] != a)
        h = (h + 1) & c->hmask;
    return h;
}

static inline ALWAYS Ins *find(Core *c, u32 a)
{
    u32 h = hslot(c, a);
    return c->hkey[h] == a ? &c->ins[c->hval[h]] : 0;
}

Core *c6x_new(int hbits)
{
    Core *c = calloc(1, sizeof(Core));
    c->hmask = (1u << hbits) - 1;
    c->hkey = malloc(sizeof(u32) << hbits);
    c->hval = malloc(sizeof(int32_t) << hbits);
    memset(c->hkey, 0xFF, sizeof(u32) << hbits);
    c->c[C_CSR] = 0x14000100;
    c->hint = c->prevep = c->lastf = -1;
#ifdef C6X_PROF
    c->hist = calloc(65536, sizeof(u32));
#endif
    return c;
}

void c6x_free(Core *c)
{
    free(c->idle_stk); free(c->ref_stk);
    free(c->ins); free(c->epi); free(c->epc); free(c->hkey); free(c->hval);
#ifdef C6X_PROF
    free(c->hist);
#endif
    free(c);
}
int c6x_sizeof_ins(void) { return sizeof(Ins); }
int c6x_sizeof_core(void) { return sizeof(Core); }
u32 *c6x_regs(Core *c) { return c->r; }
u32 *c6x_ctrl(Core *c) { return c->c; }

static void ep_flush(Core *c)
{
    if (c->epi) memset(c->epi, 0xFF, sizeof(int32_t) * c->cap);
    c->nepc = 0;
    c->hint = c->prevep = c->lastf = -1;
}

static void rehash(Core *c)
{
    memset(c->hkey, 0xFF, sizeof(u32) * (c->hmask + 1));
    for (int i = 0; i < c->nins; i++) {
        u32 h = hslot(c, c->ins[i].addr);
        c->hkey[h] = c->ins[i].addr;
        c->hval[h] = i;
    }
}

int c6x_add_code(Core *c, const Ins *v, int n)
{
    c->idle_armed = 0; c->ref_ok = 0;      /* code changed: re-learn idle passes */
    for (int i = 0; i < n; i++) {
        u32 h = hslot(c, v[i].addr);
        if (c->hkey[h] == v[i].addr) { c->ins[c->hval[h]] = v[i]; continue; }
        if (c->nins == c->cap) {
            c->cap = c->cap ? c->cap * 2 : 65536;
            c->ins = realloc(c->ins, sizeof(Ins) * c->cap);
            c->epi = realloc(c->epi, sizeof(int32_t) * c->cap);
        }
        if ((u32)(c->nins + 1) * 2 > c->hmask) return -1;
        c->ins[c->nins] = v[i];
        c->hkey[h] = v[i].addr;
        c->hval[h] = c->nins++;
    }
    ep_flush(c);           /* new code can extend or change packets */
    return c->nins;
}

void c6x_settle(Core *c);
void c6x_drop_code(Core *c, u32 lo, u32 hi)
{
    c->idle_armed = 0; c->ref_ok = 0;
    if (lo < DFF_PC + 0x58u && hi > DFF_PC) { c6x_settle(c); c->dff_nt = 0; c->dff_learn = 0; }
    int w = 0;
    for (int i = 0; i < c->nins; i++)
        if (!(c->ins[i].addr >= lo && c->ins[i].addr < hi))
            c->ins[w++] = c->ins[i];
    c->nins = w;
    rehash(c);
    ep_flush(c);
}

void c6x_clear_code(Core *c) { c->idle_armed = 0; c->ref_ok = 0; c->dff_nt = 0; c->dff_learn = 0; c->dff_frozen = 0; c->nins = 0; rehash(c); ep_flush(c); }

/* ================================================== memory ============ */
void c6x_set_cb(Core *c, mmio_rd_t r, mmio_wr_t w, pyop_t p, pyld_t l)
{ c->mrd = r; c->mwr = w; c->pyop = p; c->pyld = l; }

void c6x_add_region(Core *c, u32 lo, u32 size, uint8_t *buf)
{
    c->reg[c->nreg].lo = lo; c->reg[c->nreg].hi = (u64)lo + size;
    c->reg[c->nreg].buf = buf; c->nreg++;
}

static inline uint8_t *ram(Core *c, u32 a, int n)
{
    Region *g = &c->reg[c->lastreg];
    if (a >= g->lo && (u64)a + n <= g->hi)
        return g->buf + (a - g->lo);
    for (int i = 0; i < c->nreg; i++)
        if (a >= c->reg[i].lo && (u64)a + n <= c->reg[i].hi) {
            c->lastreg = i;
            return c->reg[i].buf + (a - c->reg[i].lo);
        }
    return 0;
}

/* L2 is also seen at 0x00800000 */
static inline u32 gaddr(u32 a) { return (a >= 0x00800000u && a < 0x00840000u) ? a + 0x11000000u : a; }
/* recording an idle pass: is [a, a+n) on the stack and written by the pass
 * itself before this read? (then its value is the pass's own, not input) */
static int rs_own(Core *c, u32 a, int n)
{
    u32 g = gaddr(a);
    if (g < c->idle_lo || g >= c->idle_hi) return 0;
    for (int i = 0; i < c->sw_cnt; i++) {
        u32 w = c->sw_a[i];
        if (g >= w && g + n <= w + c->sw_n[i]) return 1;
    }
    return 0;
}

static u64 mread(Core *c, u32 a, int n)
{
    uint8_t *p = ram(c, a, n);
    if (p) {     /* constant-size copies: no libc memmove call per access */
        u64 v;
        switch (n) {
        case 4: { u32 x; memcpy(&x, p, 4); v = x; break; }
        case 8: memcpy(&v, p, 8); break;
        case 2: { uint16_t x; memcpy(&x, p, 2); v = x; break; }
        case 1: v = *p; break;
        default: v = 0; memcpy(&v, p, n); break;
        }
        if (c->rs_on && !rs_own(c, a, n)) {   /* recording an idle pass's reads */
            if (c->rs_cnt < RSMAX) {
                c->rs_a[c->rs_cnt] = a; c->rs_n[c->rs_cnt] = (uint8_t)n; c->rs_v[c->rs_cnt] = v;
                c->rs_cnt++;
            } else c->rs_over = 1;
        }
        return v;
    }
    c->idle_dirty = 1;
    if (n == 8) return (u64)c->mrd(a, 4) | (u64)c->mrd(a + 4, 4) << 32;
    return c->mrd(a, n);
}

/* does every load of the recorded idle pass still find the value it read? */
static int rs_still(Core *c)
{
    for (int i = 0; i < c->ref_cnt; i++) {
        uint8_t *p = ram(c, c->ref_a[i], c->ref_n[i]);
        u64 v = 0;
        if (!p) return 0;
        memcpy(&v, p, c->ref_n[i]);
        if (v != c->ref_v[i]) return 0;
    }
    return 1;
}

static void mwrite(Core *c, u32 a, int n, u64 v)
{
    uint8_t *p = ram(c, a, n);
    if (p) {
        u32 g = gaddr(a);   /* L2 alias */
        if (c->rs_on) {     /* recording an idle pass: its stack and outside writes */
            if (g >= c->idle_lo && g < c->idle_hi) {
                if (c->sw_cnt < RSMAX) {
                    c->sw_a[c->sw_cnt] = g; c->sw_n[c->sw_cnt] = (uint8_t)n; c->sw_cnt++;
                } else c->rs_over = 1;
            } else if (c->rs_cnt < RSMAX) {
                c->rs_a[c->rs_cnt] = a; c->rs_n[c->rs_cnt] = (uint8_t)n; c->rs_v[c->rs_cnt] = v;
                c->rs_cnt++;
            } else c->rs_over = 1;
        }
        switch (n) {     /* constant sizes (little-endian host, as before) */
        case 4: { u32 o, x = (u32)v; memcpy(&o, p, 4);
            if (!c->idle_dirty && (g < c->idle_lo || g >= c->idle_hi) && o != x) c->idle_dirty = 1;
            memcpy(p, &x, 4); return; }
        case 8: { u64 o; memcpy(&o, p, 8);
            if (!c->idle_dirty && (g < c->idle_lo || g >= c->idle_hi) && o != v) c->idle_dirty = 1;
            memcpy(p, &v, 8); return; }
        case 2: { uint16_t o, x = (uint16_t)v; memcpy(&o, p, 2);
            if (!c->idle_dirty && (g < c->idle_lo || g >= c->idle_hi) && o != x) c->idle_dirty = 1;
            memcpy(p, &x, 2); return; }
        case 1: { uint8_t x = (uint8_t)v;
            if (!c->idle_dirty && (g < c->idle_lo || g >= c->idle_hi) && *p != x) c->idle_dirty = 1;
            *p = x; return; }
        }
        if (!c->idle_dirty && (g < c->idle_lo || g >= c->idle_hi) && memcmp(p, &v, n))
            c->idle_dirty = 1;               /* a real change outside the stack */
        memcpy(p, &v, n);
        return;
    }
    c->idle_dirty = 1;
    if (n == 8) { c->mwr(a, 4, (u32)v); c->mwr(a + 4, 4, (u32)(v >> 32)); return; }
    c->mwr(a, n, (u32)(n == 4 ? v : (v & ((1u << (8 * n)) - 1))));
}

/* ================================================== scheduling ======== */
static Ev *ev_new(Core *c, u64 w)
{
    Bucket *b = &c->ring[w % RING];
    if (b->n == EVMAX) { c->err = 3; c->status = ST_ERR; return &b->e[0]; }
    c->evcount++;
    return &b->e[b->n++];
}

/* memory events (stores/loads) make commit() scan for them */
static inline void ev_mem(Core *c, u64 w)
{
    c->ring[w % RING].types = 1;
}

static inline void wr(Core *c, u64 w, int idx, u32 v)
{
    Bucket *b = &c->ring[w % RING];
    if (b->rn == EVMAX) { c->err = 3; c->status = ST_ERR; return; }
    b->ri[b->rn] = (uint8_t)idx; b->rv[b->rn++] = v;
    c->evcount++;
}

static inline void wrc(Core *c, u64 w, int idx, u32 v)
{ Ev *e = ev_new(c, w); e->type = E_CTRL; e->lo = (uint8_t)idx; e->val = v; }

static inline void st(Core *c, u64 w, u32 a, int n, u64 v)
{ Ev *e = ev_new(c, w); e->type = E_ST; e->addr = a; e->size = (uint8_t)n; e->val = v; ev_mem(c, w); }

static inline void ld(Core *c, u64 issue, u32 a, int n, int lo, int hi, int sx)
{
    Ev *e = ev_new(c, issue + 3);
    e->type = E_LD; e->addr = a; e->size = (uint8_t)n;
    e->lo = (uint8_t)lo; e->hi = (uint8_t)hi; e->sx = (uint8_t)sx;
    ev_mem(c, issue + 3);
}

static void branch(Core *c, u64 issue, u32 t)
{
    u64 w = issue + 5;
    for (int i = 0; i < c->npend; i++)
        if (c->pend_cyc[i] == w) { c->pend_tgt[i] = t; return; }
    if (c->npend < NPEND) { c->pend_cyc[c->npend] = w; c->pend_tgt[c->npend++] = t; }
    else { c->err = 4; c->status = ST_ERR; }
}

/* exported for the Python fallback proxy */
void c6x_wr(Core *c, u64 w, int idx, u32 v) { wr(c, w, idx, v); }
void c6x_wrc(Core *c, u64 w, int idx, u32 v) { wrc(c, w, idx, v); }
void c6x_st(Core *c, u64 w, u32 a, int n, u64 v) { st(c, w, a, n, v); }
void c6x_branch(Core *c, u64 issue, u32 t) { branch(c, issue, t); }
void c6x_ldpy(Core *c, u64 issue, u32 a, int n, u32 id)
{
    Ev *e = ev_new(c, issue + 3);
    e->type = E_PYLD; e->addr = a; e->size = (uint8_t)n; e->val = id;
    ev_mem(c, issue + 3);
}
void c6x_stop(Core *c) { c->status = ST_STOP; }
/* device models raise / clear interrupt flags atomically (play mode runs
 * them on another thread than the core) */
void c6x_ifr_or(Core *c, u32 m) { __atomic_fetch_or(&c->c[C_IFR], m, __ATOMIC_SEQ_CST); }
void c6x_ifr_and(Core *c, u32 m) { __atomic_fetch_and(&c->c[C_IFR], m, __ATOMIC_SEQ_CST); }

static void ctrl_set(Core *c, int i, u32 v)
{
    c->c[i] = v;
    if (i == C_CSR) c->c[C_TSR] = (c->c[C_TSR] & ~1u) | (v & 1);
    if (i == C_TSR) c->c[C_CSR] = (c->c[C_CSR] & ~1u) | (v & 1);
}

static inline ALWAYS void commit(Core *c, u64 cyc)
{
    Bucket *b = &c->ring[cyc % RING];
    int n = b->n;
    if (!n) goto regs;
    if (!b->types) goto ctrl;
    for (int i = 0; i < n; i++)
        if (b->e[i].type == E_ST) {
            Ev *e = &b->e[i];
            if (e->addr >= c->wlo && e->addr < c->whi) {   /* debug store watch */
                int k = c->wn++ & 255;
                c->wpc[k] = c->pc; c->wad[k] = e->addr; c->wv[k] = (u32)e->val; c->wcy[k] = cyc;
            }
            mwrite(c, e->addr, e->size, e->val);
        }
    for (int i = 0; i < n; i++) {
        Ev *e = &b->e[i];
        if (e->type == E_LD) {
            u64 v = mread(c, e->addr, e->size);
            if (e->size == 8) {
                wr(c, cyc + 1, e->lo, (u32)v);
                wr(c, cyc + 1, e->hi, (u32)(v >> 32));
            } else {
                u32 x = (u32)v;
                if (e->sx == 1) x = (u32)(s32)(int8_t)x;
                else if (e->sx == 2) x = (u32)(s32)(int16_t)x;
                wr(c, cyc + 1, e->lo, x);
            }
        } else if (e->type == E_PYLD) {
            u64 v = mread(c, e->addr, e->size);
            c->pyld((u32)e->val, (u32)v, (u32)(v >> 32));
        }
    }
    n = b->n;
ctrl:
    for (int i = 0; i < n; i++) {
        Ev *e = &b->e[i];
        if (e->type == E_CTRL) ctrl_set(c, e->lo, (u32)e->val);
    }
    c->evcount -= n;
    b->n = 0;
    b->types = 0;
regs:;
    int rn = b->rn;
    if (!rn) return;
    for (int i = 0; i < rn; i++) c->r[b->ri[i]] = b->rv[i];
    c->evcount -= rn;
    b->rn = 0;
}

/* ================================================== helpers =========== */
static inline s32 sat32(s64 v)
{ return v > 0x7FFFFFFFLL ? 0x7FFFFFFF : (v < -0x80000000LL ? (s32)0x80000000 : (s32)v); }
static inline u32 h2(u32 lo, u32 hi) { return (lo & 0xFFFF) | (hi << 16); }
static inline int bitlen(u32 v) { return v ? 32 - __builtin_clz(v) : 0; }

static inline int cond(Core *c, int r, int neg)
{
    if (r < 0) return 1;
    return neg ? c->r[r] == 0 : c->r[r] != 0;
}

static inline u32 A(Core *c, const Ins *i) { return i->ca ? (u32)i->k[0] : c->r[i->ra]; }
static inline u32 B(Core *c, const Ins *i) { return i->cb ? (u32)i->k[1] : c->r[i->rb]; }

static inline u32 f2b(float f) { u32 b; memcpy(&b, &f, 4); return b; }
static inline double fin(u32 b)
{
    float f;
    if (((b >> 23) & 0xFF) == 0) return (b >> 31) ? -0.0 : 0.0;
    memcpy(&f, &b, 4);
    return f;
}
static inline u32 fout(double x)
{
    u32 b = f2b((float)x);
    if (((b >> 23) & 0xFF) == 0) b &= 0x80000000u;
    return b;
}
static inline double din(u32 lo, u32 hi)
{
    u64 b = (u64)hi << 32 | lo; double d;
    if (((b >> 52) & 0x7FF) == 0) return (b >> 63) ? -0.0 : 0.0;
    memcpy(&d, &b, 8);
    return d;
}
static inline u64 dout(double x)
{
    u64 b; memcpy(&b, &x, 8);
    if (((b >> 52) & 0x7FF) == 0) b &= 1ULL << 63;
    return b;
}
static u32 to_int(double x, int mode)
{
    if (isnan(x)) return 0x80000000u;
    double r = mode == 0 ? nearbyint(x) : mode == 1 ? trunc(x) : mode == 2 ? ceil(x) : floor(x);
    if (r > 2147483647.0) return 0x7FFFFFFF;
    if (r < -2147483648.0) return 0x80000000u;
    return (u32)(s32)r;
}
static inline int round_mode(Core *c, int side)
{ return (c->c[C_FADCR] >> (side == 2 ? 25 : 9)) & 3; }

/* next instruction address (headers skipped), as cpu._next_addr */
static u32 next_addr(Core *c, u32 a)
{
    Ins *i = find(c, a);
    u32 n = a + (i ? i->size : 4);
    Ins *h = find(c, n);
    while (h && h->header) { n += 4; h = find(c, n); }
    return n;
}

/* ================================================== execute =========== */
static void sp_start(Core *c, const Ins *i);

/* one instruction; returns nop cycles (0 = none). Predicate included. */
static inline ALWAYS int exec1(Core *c, const Ins *i, u64 cyc)
{
#ifdef C6X_PROF
    if (c->hist) c->hist[(i->addr >> 2) & 0xFFFF]++;
#endif
    if (i->op == O_py) {
        c->py_calls++;
        c->idle_dirty = 1;
        return c->pyop(i->py, cyc);
    }
    if (!cond(c, i->creg, i->cneg) && !(i->op == O_sploop && i->sub == K_SPLOOPW)) {
        switch (i->op) {
        case O_bnop: case O_addkpc: return i->k[1] + 1;
        case O_callp: return 6;
        default: return 0;
        }
    }
    u64 w = cyc + i->w;
    switch (i->op) {
    case O_nop: return i->k[0];
    case O_mvk: wr(c, cyc, i->rd, (u32)i->k[0]); return 0;
    case O_mvkh: wr(c, cyc, i->rd, (u32)i->k[0] | (c->r[i->rd] & 0xFFFF)); return 0;
    case O_mv: wr(c, cyc, i->rd, A(c, i)); return 0;
    case O_mvd: wr(c, cyc + 3, i->rd, A(c, i)); return 0;
    case O_mvpair: wr(c, cyc, i->rd, c->r[i->ra]); wr(c, cyc, i->rdh, c->r[i->rah]); return 0;
    case O_dmv: { u32 a = A(c, i), b = B(c, i); wr(c, cyc, i->rdh, a); wr(c, cyc, i->rd, b); return 0; }
    case O_zero: wr(c, cyc, i->rd, 0); return 0;
    case O_zeropair: wr(c, cyc, i->rd, 0); wr(c, cyc, i->rdh, 0); return 0;
    case O_mvc_to: {
        u32 v = c->r[i->ra];
        if (i->ctrl == C_ILC || i->ctrl == C_RILC) wrc(c, cyc + 3, i->ctrl, v);
        /* IFR is also set by the device models (another thread in play
         * mode): read-modify-writes of it are atomic everywhere */
        else if (i->ctrl == C_ICR) __atomic_fetch_and(&c->c[C_IFR], ~v, __ATOMIC_SEQ_CST);
        else if (i->ctrl == C_ISR) __atomic_fetch_or(&c->c[C_IFR], v, __ATOMIC_SEQ_CST);
        else wrc(c, cyc, i->ctrl, v);
        return 0;
    }
    case O_mvc_from: {
        u32 v;
        if (i->ctrl == C_TSCL) { c->idle_dirty = 1; v = (u32)c->cycle; c->c[C_TSCH] = (u32)(c->cycle >> 32); }
        else v = c->c[i->ctrl];
        wr(c, cyc, i->rd, v);
        return 0;
    }
    case O_add: wr(c, w, i->rd, A(c, i) + B(c, i)); return 0;
    case O_sub: wr(c, w, i->rd, A(c, i) - B(c, i)); return 0;
    case O_addk: wr(c, cyc, i->rd, c->r[i->rd] + (u32)i->k[0]); return 0;
    case O_adda: wr(c, cyc, i->rd, A(c, i) + (B(c, i) << i->sub)); return 0;
    case O_suba: wr(c, cyc, i->rd, A(c, i) - (B(c, i) << i->sub)); return 0;
    case O_and: wr(c, w, i->rd, A(c, i) & B(c, i)); return 0;
    case O_or: wr(c, w, i->rd, A(c, i) | B(c, i)); return 0;
    case O_xor: wr(c, w, i->rd, A(c, i) ^ B(c, i)); return 0;
    case O_andn: wr(c, w, i->rd, A(c, i) & ~B(c, i)); return 0;
    case O_sadd: wr(c, w, i->rd, (u32)sat32((s64)(s32)A(c, i) + (s32)B(c, i))); return 0;
    case O_ssub: wr(c, w, i->rd, (u32)sat32((s64)(s32)A(c, i) - (s32)B(c, i))); return 0;
    case O_add2: { u32 a = A(c, i), b = B(c, i); wr(c, w, i->rd, h2(a + b, (a >> 16) + (b >> 16))); return 0; }
    case O_sub2: { u32 a = A(c, i), b = B(c, i); wr(c, w, i->rd, h2(a - b, (a >> 16) - (b >> 16))); return 0; }
    case O_pack2: wr(c, w, i->rd, h2(B(c, i), A(c, i))); return 0;
    case O_packh2: wr(c, w, i->rd, h2(B(c, i) >> 16, A(c, i) >> 16)); return 0;
    case O_packhl2: wr(c, w, i->rd, h2(B(c, i), A(c, i) >> 16)); return 0;
    case O_packlh2: wr(c, w, i->rd, h2(B(c, i) >> 16, A(c, i))); return 0;
    case O_packl4: { u32 a = A(c, i), b = B(c, i);
        wr(c, w, i->rd, (b & 0xFF) | ((b >> 16) & 0xFF) << 8 | (a & 0xFF) << 16 | ((a >> 16) & 0xFF) << 24); return 0; }
    case O_packh4: { u32 a = A(c, i), b = B(c, i);
        wr(c, w, i->rd, ((b >> 8) & 0xFF) | ((b >> 24) & 0xFF) << 8 | ((a >> 8) & 0xFF) << 16 | ((a >> 24) & 0xFF) << 24); return 0; }
    case O_swap2: { u32 a = A(c, i); wr(c, w, i->rd, (a >> 16) | (a << 16)); return 0; }
    case O_neg: wr(c, w, i->rd, (u32)(-(s64)(s32)A(c, i))); return 0;
    case O_not: wr(c, w, i->rd, ~A(c, i)); return 0;
    case O_abs: { s64 v = (s32)A(c, i); wr(c, w, i->rd, (u32)sat32(v < 0 ? -v : v)); return 0; }
    case O_cmp: {   /* sub: 0 eq 1 gt 2 lt 3 gtu 4 ltu */
        u32 a = A(c, i), b = B(c, i); int r;
        switch (i->sub) {
        case 0: r = (s32)a == (s32)b; break;
        case 1: r = (s32)a > (s32)b; break;
        case 2: r = (s32)a < (s32)b; break;
        case 3: r = a > b; break;
        default: r = a < b;
        }
        wr(c, w, i->rd, r);
        return 0;
    }
    case O_shl: { u32 n = B(c, i) & 0x3F, x = A(c, i); wr(c, w, i->rd, n >= 32 ? 0 : x << n); return 0; }
    case O_shr: { u32 n = B(c, i) & 0x3F; s32 x = (s32)A(c, i); wr(c, w, i->rd, (u32)(n >= 32 ? (x >> 31) : (x >> n))); return 0; }
    case O_shru: { u32 n = B(c, i) & 0x3F, x = A(c, i); wr(c, w, i->rd, n >= 32 ? 0 : x >> n); return 0; }
    case O_sshl: {
        u32 n = B(c, i) & 0x3F; s32 v = (s32)A(c, i); u32 r;
        if (v == 0) r = 0;
        else if (n >= 32) r = v < 0 ? 0x80000000u : 0x7FFFFFFF;
        else r = (u32)sat32((s64)v << n);
        wr(c, w, i->rd, r);
        return 0;
    }
    case O_ext: case O_extu: case O_set: case O_clr: {
        u32 a = A(c, i), csta, cstb;
        if (i->cb) { csta = i->k[1] & 31; cstb = i->k[2] & 31; }
        else { u32 b = c->r[i->rb]; csta = (b >> 5) & 31; cstb = b & 31; }
        u32 r;
        if (i->op == O_ext) r = (u32)((s32)(a << csta) >> cstb);
        else if (i->op == O_extu) r = (a << csta) >> cstb;
        else {
            r = a;
            if (cstb >= csta) {
                u32 m = (u32)((((u64)1 << (cstb - csta + 1)) - 1) << csta);
                r = i->op == O_set ? (a | m) : (a & ~m);
            }
        }
        wr(c, w, i->rd, r);
        return 0;
    }
    case O_lmbd: { u32 v = B(c, i); if (!(A(c, i) & 1)) v = ~v; wr(c, w, i->rd, v ? __builtin_clz(v) : 32); return 0; }
    case O_norm: { s32 v = (s32)A(c, i); if (v < 0) v = ~v; wr(c, w, i->rd, v == 0 ? 31 : 31 - bitlen((u32)v)); return 0; }
    case O_mpy16: {   /* sub bits: 1 a-hi, 2 a-unsigned, 4 b-hi, 8 b-unsigned, 16 smpy */
        u32 a = A(c, i), b = B(c, i);
        u32 ah = i->sub & 1 ? a >> 16 : a & 0xFFFF, bh = i->sub & 4 ? b >> 16 : b & 0xFFFF;
        s64 x = i->sub & 2 ? (s64)ah : (s64)(int16_t)ah;
        s64 y = i->sub & 8 ? (s64)bh : (s64)(int16_t)bh;
        s64 p = x * y;
        if (i->sub & 16) { p <<= 1; if (p == 0x80000000LL) p = 0x7FFFFFFF; }
        wr(c, w, i->rd, (u32)p);
        return 0;
    }
    case O_mpy32: case O_mpy32p: {   /* sub: 1 a signed, 2 b signed */
        u32 a = A(c, i), b = B(c, i);
        s64 x = i->sub & 1 ? (s64)(s32)a : (s64)a, y = i->sub & 2 ? (s64)(s32)b : (s64)b;
        u64 p = (u64)x * (u64)y;
        wr(c, cyc + 3, i->rd, (u32)p);
        if (i->op == O_mpy32p) wr(c, cyc + 3, i->rdh, (u32)(p >> 32));
        return 0;
    }
    case O_ld: case O_st: {
        u32 bv = c->r[i->mbase], o = 0, addr, nb = 0; int upd = 0;
        if (i->mofk == 1) o = c->r[i->moff] * i->mscale;
        else if (i->mofk == 2) o = (u32)i->k[2];
        switch (i->mmode) {
        case M_PLAIN: addr = bv; break;
        case M_PADD: addr = bv + o; break;
        case M_PSUB: addr = bv - o; break;
        case M_PREINC: addr = nb = bv + o; upd = 1; break;
        case M_PREDEC: addr = nb = bv - o; upd = 1; break;
        case M_POSTINC: addr = bv; nb = bv + o; upd = 1; break;
        default: addr = bv; nb = bv - o; upd = 1; break;
        }
        if (upd) wr(c, cyc, i->mbase, nb);
        addr &= ~(u32)i->malign;
        if (i->op == O_ld) {
            ld(c, cyc, addr, i->msz, i->rd, i->rdh, i->msign);
            return i->prot ? 5 : 0;
        }
        u64 v = i->msz == 8 ? ((u64)c->r[i->rdh] << 32 | c->r[i->rd])
              : i->msz == 4 ? c->r[i->rd] : (c->r[i->rd] & ((1u << (8 * i->msz)) - 1));
        st(c, cyc + 2, addr, i->msz, v);
        return 0;
    }
    case O_b: case O_bnop: {   /* sub: 0 const k0, 1 reg ra, 2 irp, 3 nrp */
        u32 t = i->sub == 0 ? (u32)i->k[0] : i->sub == 1 ? c->r[i->ra]
              : i->sub == 2 ? c->c[C_IRP] : c->c[C_NRP];
        if (i->sub == 2) {
            u32 g = (c->c[C_CSR] >> 1) & 1;
            c->c[C_CSR] = (c->c[C_CSR] & ~1u) | g;
            c->c[C_TSR] = (c->c[C_ITSR] & ~1u) | g;
        }
        branch(c, cyc, t);
        return i->op == O_bnop && i->k[1] ? i->k[1] + 1 : 0;
    }
    case O_callp: {
        u32 a = i->addr, n;
        for (;;) {
            n = next_addr(c, a);
            Ins *j = find(c, n);
            if (!j || !j->par) break;
            a = n;
        }
        wr(c, cyc, i->rd, n);
        branch(c, cyc, (u32)i->k[0]);
        return 6;
    }
    case O_addkpc: wr(c, cyc, i->rd, (u32)i->k[0]); return i->k[1] ? i->k[1] + 1 : 0;
    case O_bdec: {
        s32 v = (s32)c->r[i->rd];
        if (v >= 0) { branch(c, cyc, (u32)i->k[0]); if (i->sub) wr(c, cyc, i->rd, (u32)(v - 1)); }
        return 0;
    }
    case O_sploop: sp_start(c, i); return 0;
    case O_spctl: return 0;
    case O_dint: { u32 csr = c->c[C_CSR];
        c->c[C_TSR] = (c->c[C_TSR] & ~1u) | (csr & 1); c->c[C_CSR] = csr & ~1u; return 0; }
    case O_rint: c->c[C_CSR] = (c->c[C_CSR] & ~1u) | (c->c[C_TSR] & 1); return 0;
    case O_idle: c->idle = 1; return 0;
    case O_fadd: wr(c, w, i->rd, fout(fin(A(c, i)) + fin(B(c, i)))); return 0;
    case O_fsub: wr(c, w, i->rd, fout(fin(A(c, i)) - fin(B(c, i)))); return 0;
    case O_fmul: wr(c, w, i->rd, fout(fin(A(c, i)) * fin(B(c, i)))); return 0;
    case O_intsp: { u32 a = A(c, i); wr(c, w, i->rd, fout(i->sub ? (double)a : (double)(s32)a)); return 0; }
    case O_spint: wr(c, w, i->rd, to_int(fin(A(c, i)), i->sub ? 1 : round_mode(c, i->side))); return 0;
    case O_spdp: { u64 b = dout(fin(A(c, i))); wr(c, cyc, i->rd, (u32)b); wr(c, cyc + 1, i->rdh, (u32)(b >> 32)); return 0; }
    case O_dpsp: wr(c, cyc + 3, i->rd, fout(din(c->r[i->ra], c->r[i->rah]))); return 0;
    case O_dadd: case O_dsub: case O_dmul: {
        /* sub bit0: src1 single, bit1: src2 single; k2 lo delay, k3 hi delay */
        double x = i->sub & 1 ? fin(A(c, i)) : din(c->r[i->ra], c->r[i->rah]);
        double y = i->sub & 2 ? fin(B(c, i)) : din(c->r[i->rb], c->r[i->rbh]);
        double r = i->op == O_dadd ? x + y : i->op == O_dsub ? x - y : x * y;
        u64 b = dout(r);
        wr(c, cyc + i->k[2], i->rd, (u32)b);
        wr(c, cyc + i->k[3], i->rdh, (u32)(b >> 32));
        return 0;
    }
    case O_fcmp: case O_dcmp: {
        double x, y; int r;
        if (i->op == O_fcmp) { x = fin(A(c, i)); y = fin(B(c, i)); }
        else { x = din(c->r[i->ra], c->r[i->rah]); y = din(c->r[i->rb], c->r[i->rbh]); }
        r = i->sub == 0 ? x == y : i->sub == 1 ? x > y : x < y;
        wr(c, w, i->rd, r);
        return 0;
    }
    case O_mpyxi: {   /* MPYHI/MPYLI(R): sub bit0 a-hi, bit1 round; a 16-bit, b 32-bit */
        u32 a = A(c, i), b = B(c, i);
        s64 p = (s64)(int16_t)(i->sub & 1 ? a >> 16 : a) * (s64)(s32)b;
        if (i->sub & 2) wr(c, cyc + 3, i->rd, (u32)((p + 0x4000) >> 15));
        else { wr(c, cyc + 3, i->rd, (u32)p); wr(c, cyc + 3, i->rdh, (u32)((u64)p >> 32)); }
        return 0;
    }
    case O_abssp: wr(c, cyc, i->rd, fout(fabs(fin(A(c, i))))); return 0;
    case O_rcpsp: {
        double x = fin(A(c, i)), v;
        int special = x == 0 || (i->sub && x < 0);
        if (special) v = x >= 0 ? INFINITY : NAN;
        else v = i->sub ? 1.0 / sqrt(x) : 1.0 / x;
        u32 b = f2b((float)v);
        if (!special) b &= ~0x7FFFu;
        if (((b >> 23) & 0xFF) == 0) b &= 0x80000000u;
        wr(c, cyc, i->rd, b);
        return 0;
    }
    }
    c->err = 5; c->err_pc = i->addr; c->status = ST_ERR;
    return 0;
}

/* ================================================== SPLOOP ============ */
static void sp_start(Core *c, const Ins *i)
{
    SPLoop *s = &c->sp;
    /* reset (not a 13 KB memset per loop start): the scalar head, the
     * valid flags the last loop could have set (stages < its lc + 1), and
     * the small tails; bl/bi/bu are only read below nbuf */
    int oldlc = s->lc < SPLC ? s->lc + 1 : SPLC;
    memset(s, 0, offsetof(SPLoop, bl));
    memset(s->valid, 0, oldlc);
    memset(s->pv, 0, sizeof s->pv);
    s->npv = 0; memset(s->hist, 0, sizeof s->hist); s->nhist = 0;
    s->kind = i->sub; s->ii = i->k[0];
    s->wcreg = i->sub == K_SPLOOPW ? i->creg : -1; s->wcneg = i->cneg;
    s->loading = s->fetch_prog = 1;
    s->dynlen = s->drain = s->post_delay = s->pc_after = s->refill = -1;
    s->resume = (c->c[C_TSR] & SPLX) != 0;          /* §7.13.2 */
    c->c[C_TSR] |= SPLX;
    if (s->resume && s->kind == K_SPLOOPD) s->kind = K_SPLOOP;
    s->addr = i->addr;
    c->sp_on = 1;
    if (s->kind == K_SPLOOP) {
        if (c->c[C_ILC] == 0) s->initial_term = 1;
        else c->c[C_ILC]--;
    }
}

int c6x_refill = 6;      /* fetch refill after SPLOOP (isa.REFILL) */
void c6x_set_refill(int n) { c6x_refill = n; }

static void sp_exit_fetch(Core *c)
{
    SPLoop *s = &c->sp;
    if (c6x_refill) s->refill = c6x_refill;
    else { s->fetch_prog = 1; c->pc = s->pc_after; }
}

static void sp_leave(Core *c)
{
    SPLoop *s = &c->sp;
    c->sp_on = 0;
    c->c[C_TSR] &= ~SPLX;
    if (!s->fetch_prog) {
        c->pc = s->pc_after;
        int left = s->refill < 0 ? c6x_refill : s->refill;
        if (left > 0) c->stall = left;
    }
}

static int sp_want_int(Core *c)       /* §7.13.1 */
{
    SPLoop *s = &c->sp;
    u32 *r = c->c;
    if (s->loading || s->initial_term) return 0;
    if (!(r[C_CSR] & 1) || !(r[C_IER] & 2) || !(r[C_IFR] & r[C_IER] & 0xFFF0)) return 0;
    if (c->npend) return 0;
    if ((s->kind == K_SPLOOPD || s->kind == K_SPLOOPW) && s->since <= 3) return 0;
    if (s->kind == K_SPLOOPW)
        return !(s->nhist >= 4 && !s->hist[(s->nhist - 4) & 3]);
    int dl = s->dynlen < 0 ? 0 : s->dynlen;
    return (int)r[C_ILC] >= (dl + s->ii - 1) / s->ii;
}

static void sp_boundary(Core *c)
{
    SPLoop *s = &c->sp;
    s->since++;
    if (s->refill >= 0 && !s->fetch_prog) {
        if (--s->refill <= 0) { s->fetch_prog = 1; c->pc = s->pc_after; }
    }
    if (s->drain >= 0) {
        s->drain++;
        if (s->post_delay >= 0 && !s->fetch_prog && !s->int_drain && s->refill < 0 &&
            s->drain >= s->post_delay)
            sp_exit_fetch(c);
        if (s->dynlen >= 0 && s->drain >= s->dynlen - s->ii) {
            memset(s->valid, 0, s->lc < SPLC ? s->lc + 1 : SPLC);
            if (s->int_drain) {                     /* §7.13.4 */
                c->sp_on = 0;
                c->c[C_TSR] &= ~SPLX;
                c->pc = s->addr;
                c->splx_irq = 1;
                c->splx_after = (u32)s->pc_after;
                return;
            }
            sp_leave(c);
            return;
        }
    }
    if (s->wcreg >= 0) {
        s->hist[s->nhist & 3] = (uint8_t)cond(c, s->wcreg, s->wcneg);
        s->nhist++;
    }
    if (++s->lbc < s->ii) return;
    s->lbc = 0;
    if (s->initial_term) {
        if (!s->loading) sp_leave(c);
        return;
    }
    if (s->drain >= 0) return;
    if (sp_want_int(c)) { s->drain = 0; s->int_drain = 1; return; }
    if (s->kind == K_SPLOOPW) {
        int term = s->nhist >= 4 ? !s->hist[(s->nhist - 4) & 3] : 0;   /* exit when FALSE (7.10.2) */
        if (term) {
            memset(s->valid, 0, s->lc < SPLC ? s->lc + 1 : SPLC);
            if (!s->loading) sp_leave(c);
            else { c->sp_on = 0; c->c[C_TSR] &= ~SPLX; }
        }
        return;
    }
    if (s->kind == K_SPLOOPD && s->since <= 3) return;
    if (c->c[C_ILC] == 0) {
        s->drain = 0;
        if (s->post_delay == 0) sp_exit_fetch(c);
    } else c->c[C_ILC]--;
}

/* execute packet at pc; returns member count (0 = no code: ST_NEED) */
static inline ALWAYS int fetch_ep(Core *c, u32 pc, Ins **ep, u32 *nxt)
{
    u32 pc0 = pc;
    Ins *i = find(c, pc);
    if (i && i->header) { pc += 4; i = find(c, pc); }
    if (!i) { c->need = pc; c->status = ST_NEED; return 0; }
    int32_t *slot = &c->epi[i - c->ins];
    c->lastf = *slot;
    if (*slot >= 0) {                          /* cached packet */
        struct EPC *e = &c->epc[*slot];
        for (int k = 0; k < e->n; k++) ep[k] = &c->ins[e->idx[k]];
        *nxt = e->nxt;
        return e->n;
    }
    int n = 0; ep[n++] = i;
    u32 a = pc;
    for (;;) {
        u32 nx = next_addr(c, a);
        Ins *j = find(c, nx);
        if (!j || !j->par || j->header || n == 16) { *nxt = nx; break; }
        ep[n++] = j;
        a = nx;
    }
    /* cache it only when the packet's end is decided by known code: a
     * missing next word may be undecoded code that later joins it */
    if (find(c, *nxt)) {
        if (c->nepc == c->capepc) {
            c->capepc = c->capepc ? c->capepc * 2 : 16384;
            c->epc = realloc(c->epc, sizeof(struct EPC) * c->capepc);
        }
        struct EPC *e = &c->epc[c->nepc];
        e->n = n; e->nxt = *nxt; e->pc = pc0; e->succ = -1;
        for (int k = 0; k < n; k++) e->idx[k] = (int32_t)(ep[k] - c->ins);
        *slot = c->lastf = c->nepc++;
    }
    return n;
}

static int sp_cycle(Core *c, u64 cyc)
{
    SPLoop *s = &c->sp;
#ifdef C6X_PROF
    c->cyc_sp++;
    {
        int k = 0;
        while (k < c->spn && c->spa[k] != s->addr) k++;
        if (k == c->spn && c->spn < 8) { c->spa[k] = s->addr; c->spc[k] = 0; c->spn++; }
        if (k < 8) c->spc[k]++;
    }
#endif
    Ins *ep[16]; int nep = 0; u32 nxt = 0, mask = 0;
    if (s->fetch_prog && s->prog_stall == 0) {
        nep = fetch_ep(c, c->pc, ep, &nxt);
        if (!nep) return -1;
        for (int k = 0; k < nep; k++) {
            if (ep[k]->spk == 3) { c->err = 6; c->err_pc = ep[k]->addr; c->status = ST_ERR; return -1; }
            if (ep[k]->spk == 1) mask |= ep[k]->spmask;
        }
        c->pc = nxt;
    } else if (s->prog_stall) s->prog_stall--;
    if (s->drain >= 0 && s->drain < SPLC) s->valid[s->drain] = 0;
    static int bx[SPBUF];
    int nbx = 0;
    if (s->ii > 0 && s->ii <= SPRES) {
        for (int k = s->rhead[s->lbc] - 1; k >= 0; k = s->rnext[k] - 1) {
            if (s->valid[s->bl[k]] &&
                (s->resume || !(s->bu[k] < 8 && (mask >> s->bu[k] & 1))))
                bx[nbx++] = s->bi[k];
        }
    } else
    for (int k = 0; k < s->nbuf; k++) {
        int lc = s->bl[k];
        if (s->valid[lc] && lc % s->ii == s->lbc &&
            (s->resume || !(s->bu[k] < 8 && (mask >> s->bu[k] & 1))))
            bx[nbx++] = s->bi[k];
    }
    int nop = 1, kernel = -1;
    s->npv = 0;
    for (int k = 0; k < nep; k++) {
        Ins *i = ep[k];
        if (i->spk == 1) continue;
        if (i->spk == 2) { if (s->loading) kernel = k; continue; }
        int masked = (i->unit < 8 && (mask >> i->unit & 1)) ||
                     (i->op == O_bnop && i->sub == 0) || i->op == O_nop;
        if (s->resume && ((i->unit < 8 && (mask >> i->unit & 1)) || (i->op == O_bnop && i->sub == 0))) {
            int n = i->op == O_bnop ? i->k[1] + 1 : 0;      /* §7.13.2 */
            c->insn_count++;
            if (n > nop) nop = n;
            continue;
        }
        int n = (s->initial_term && !masked) ? 0 : exec1(c, i, cyc);
        if (i->op == O_nop) n = i->k[0];
        c->insn_count++;
        if (n > nop) nop = n;
        if (s->loading && !masked && !s->initial_term) {
            if (s->nbuf >= SPBUF || s->npv >= 64 || s->lc >= SPLC) {
                c->err = 7; c->status = ST_ERR; return -1;
            }
            s->bl[s->nbuf] = s->lc; s->bi[s->nbuf] = (int)(i - c->ins);
            s->bu[s->nbuf] = i->unit;
            if (s->ii > 0 && s->ii <= SPRES) {
                int r = s->lc % s->ii;
                s->rnext[s->nbuf] = 0;
                if (s->rtail[r]) s->rnext[s->rtail[r] - 1] = s->nbuf + 1;
                else s->rhead[r] = s->nbuf + 1;
                s->rtail[r] = s->nbuf + 1;
            }
            s->nbuf++;
            s->pv[s->npv++] = s->lc;
        }
    }
    for (int k = 0; k < nbx; k++) { exec1(c, &c->ins[bx[k]], cyc); c->insn_count++; }
    c->ep_count++;
    if (kernel >= 0) {
        Ins *ki = ep[kernel];
        s->loading = 0; s->fetch_prog = 0;
        s->dynlen = s->lc + 1;
        s->pc_after = nxt;
        s->post_delay = ki->k[0] * s->ii + ki->k[1];
    }
    if (nop > 1) { s->prog_stall = nop - 1; c->stall = 0; }
    for (int k = 0; k < s->npv; k++) s->valid[s->pv[k]] = 1;
    s->npv = 0;
    if (s->loading) s->lc++;
    sp_boundary(c);
    return 0;
}

/* ================================================== the machine ======= */
static int take_interrupt(Core *c)
{
    u32 *r = c->c;
    if (!(r[C_CSR] & 1) || !(r[C_IER] & 2) || c->npend || c->stall || c->sp_on || c->evcount)
        return 0;
    u32 p = r[C_IFR] & r[C_IER] & 0xFFF0;
    if (!p) return 0;
    int n = __builtin_ctz(p);
    __atomic_fetch_and(&r[C_IFR], ~(1u << n), __ATOMIC_SEQ_CST);
    r[C_IRP] = c->pc;
    r[C_CSR] = (r[C_CSR] & ~3u) | ((r[C_CSR] & 1) << 1);
    r[C_ITSR] = r[C_TSR];
    r[C_TSR] &= ~1u;
    c->pc = (r[C_ISTP] & ~0x3FFu) + 32 * n;
    c->interrupts++;
    c->idle_dirty = 1;
    return 1;
}

static int step(Core *c)
{
    u64 cyc = c->cycle;
#ifdef C6X_PROF
    if (!c->sp_on) c->cyc_norm++;
    if (c->stall) c->prof_stall++; else c->prof_run++;
#endif
    if (c->splx_irq) {             /* §7.13.4: writes complete, then the interrupt */
        if (!c->evcount && !c->npend) {
            c->splx_irq = 0;
            if (take_interrupt(c)) c->c[C_ITSR] |= SPLX;
            else c->pc = c->splx_after;
        } else {
            commit(c, cyc);
            c->cycle = cyc + 1;
            return c->status ? -1 : 0;
        }
    }
    if (!c->stall && !c->sp_on && (c->c[C_IFR] & c->c[C_IER] & 0xFFF0))
        take_interrupt(c);
    if (c->stall) c->stall--;
    else if (c->sp_on) { if (sp_cycle(c, cyc) < 0) return -1; }
    else {
        int hk = -1;
        for (int k = 0; k < c->nidle; k++)
            if (c->pc == c->idle_heads[k]) { hk = k; break; }
        if (hk >= 0) {
            /* busy-wait skip, cycle-exact: a whole pass of an idle loop,
             * head to the same head, that touched no hardware, took no
             * interrupt, made no Python op, changed no memory outside the
             * idle stack, and ends with the registers, the control
             * registers and the idle stack byte-identical to the pass
             * before, is a fixed point: the DSP's whole state repeats
             * every P cycles (P = this pass) until something outside the
             * core changes it -- an interrupt or a host write, both only
             * between c6x_run calls. So whole passes are jumped (the clock
             * moves k*P, the state is already what it would be) and the
             * rest of the slice runs normally: the state at the end of the
             * call, and the point where the next interrupt lands, are
             * exactly those of running every cycle.
             * Across c6x_run calls the host may have written memory in
             * between: the first head visit of a call trusts the last
             * pass only if every RAM load that pass made (its read set)
             * still finds the value it read then -- the pass's path and
             * result depend on nothing else, so it repeats exactly.
             * The playing deck's loop (0xC0048C2E) passes every 2237 cycles
             * through ten channel polls that toggle CSR.GIE and store the
             * values already there (~75 % of the DSP's cycles while a
             * track plays); the stopped deck's is 0xC004C8CC. */
            int quiet = !c->evcount && !c->npend && !c->splx_irq &&
                        !(c->c[C_IFR] & c->c[C_IER] & 0xFFF0);
            uint8_t *stk = c->idle_stk ? ram(c, c->idle_lo, (int)c->idle_stkn) : 0;
            /* the live stack: from SP up (below it only the pass's own
             * frames, whose reads-before-write are in the read set) */
            u32 sp = gaddr(c->r[47]);              /* B15 */
            u32 live = (sp >= c->idle_lo && sp < c->idle_hi) ? sp - c->idle_lo : 0;
            /* (a) a whole pass inside this call, back at the state it
             * started from, with nothing changed outside the stack */
            int eq_last = !c->idle_fresh && c->idle_armed && c->idle_last == hk &&
                          !c->idle_dirty && quiet && stk &&
                          !memcmp(c->idle_r, c->r, sizeof c->r) &&
                          !memcmp(c->idle_c, c->c, sizeof c->c) &&
                          !memcmp(c->idle_stk, stk, c->idle_stkn);
            u64 p = 0;
            int same = 0;
            if (eq_last) {
                same = 1;
                p = cyc - c->idle_cyc;
                if (c->rs_on && !c->rs_over && c->ref_stk) {   /* keep it as the reference */
                    memcpy(c->ref_r, c->r, sizeof c->r);
                    memcpy(c->ref_c, c->c, sizeof c->c);
                    memcpy(c->ref_stk, stk, c->idle_stkn);
                    memcpy(c->ref_a, c->rs_a, sizeof(u32) * c->rs_cnt);
                    memcpy(c->ref_n, c->rs_n, c->rs_cnt);
                    memcpy(c->ref_v, c->rs_v, sizeof(u64) * c->rs_cnt);
                    c->ref_cnt = c->rs_cnt; c->ref_p = p; c->ref_hk = hk; c->ref_ok = 1;
                }
            } else if (c->ref_ok && c->ref_hk == hk && quiet && stk &&
                       !memcmp(c->ref_r, c->r, sizeof c->r) &&
                       !memcmp(c->ref_c, c->c, sizeof c->c) &&
                       !memcmp(c->ref_stk + live, stk + live, c->idle_stkn - live) &&
                       rs_still(c)) {
                /* (b) the reference pass's entry state again (e.g. after an
                 * interrupt's work: registers, control registers and the
                 * live stack equal), and every address it read (stack
                 * bytes below SP only where it read before writing them)
                 * or wrote outside the stack still holds what it held
                 * then: its path and result depend on nothing else, so it
                 * repeats -- and its writes are silent again */
                same = 1;
                p = c->ref_p;
                c->idle_refhits++;
            }
            c->idle_fresh = 0;
            c->rs_on = 0;
            if (same) {
                /* leave this cycle (executed below) inside the call */
                if (p && c->run_end > cyc + 1 && c->run_end - cyc - 1 >= p) {
                    u64 j = (c->run_end - cyc - 1) / p * p;
                    c->cycle = cyc = cyc + j;
                    c->idle_jump += j;
                    c->idle_jumped += j;
                    c->idle_jumps++;
                }
                if (!eq_last) {                  /* the next visit compares with this one */
                    memcpy(c->idle_r, c->r, sizeof c->r);
                    memcpy(c->idle_c, c->c, sizeof c->c);
                    memcpy(c->idle_stk, stk, c->idle_stkn);
                    c->idle_armed = 1;
                    c->idle_last = hk;
                }
            } else {
                memcpy(c->idle_r, c->r, sizeof c->r);
                memcpy(c->idle_c, c->c, sizeof c->c);
                if (stk) memcpy(c->idle_stk, stk, c->idle_stkn);
                c->idle_armed = quiet && stk;
                c->idle_last = hk;
                c->rs_cnt = 0; c->sw_cnt = 0; c->rs_over = 0; c->rs_on = c->idle_armed;   /* record this pass */
            }
            c->idle_cyc = cyc;
            c->idle_dirty = 0;
        }
        Ins *ep[16]; u32 nxt; int n;
        int hi = c->hint;
        if (hi >= 0 && c->epc[hi].pc == c->pc) {      /* the expected packet */
            struct EPC *e = &c->epc[hi];
            n = e->n; nxt = e->nxt;
            for (int k = 0; k < n; k++) ep[k] = &c->ins[e->idx[k]];
            c->prevep = hi;
            c->hint = e->succ;
        } else {
            n = fetch_ep(c, c->pc, ep, &nxt);
            if (!n) return -1;
            int f = c->lastf, pv = c->prevep;
            if (f >= 0 && pv >= 0 && c->epc[pv].nxt == c->pc)
                c->epc[pv].succ = f;               /* learn the fall-through */
            c->prevep = f;
            c->hint = f >= 0 ? c->epc[f].succ : -1;
        }
        c->ep_count++;
        int nop = 1;
        if (ep[0]->op == O_sploop && (c->c[C_TSR] & SPLX)) n = 1;   /* §7.13.2 */
        for (int k = 0; k < n; k++) {
            int r = exec1(c, ep[k], cyc);
            c->insn_count++;
            if (r > nop) nop = r;
        }
        if (nop > 1) c->stall = nop - 1;
        c->pc = nxt;
    }
    if (c->status == ST_ERR) return -1;
    commit(c, cyc);
    for (int k = 0; k < c->npend; k++)
        if (c->pend_cyc[k] == cyc) {
            c->pc = c->pend_tgt[k];
            c->npend--;
            c->pend_cyc[k] = c->pend_cyc[c->npend];
            c->pend_tgt[k] = c->pend_tgt[c->npend];
            c->stall = 0;
            if (c->sp_on) c->c[C_TSR] &= ~SPLX;
            c->sp_on = 0;
            break;
        }
    c->cycle = cyc + 1;
    return c->status ? -1 : 0;
}


/* ============================================ delay-loop fast-forward ==
 * Pioneer's DSP program spends ~30 % of its working cycles in one busy-
 * wait routine at 0xC004BEE0 (measured on the playing snapshot; every call
 * is delay(1000)): `for (volatile int i = 0; i < n; i++);` as an SPLOOPW,
 * 14 cycles per count. It touches nothing but a1/a3/a0, b0/b4/b5/b6 and
 * its stack word at b15-4, so a whole call can be replaced by its exact
 * end state when nothing else can happen meanwhile:
 *   - entered at a clean packet boundary (no stall, nothing in flight, no
 *     SPLOOP, no pending branch);
 *   - no interrupt deliverable now -- and none can become deliverable
 *     before the run slice ends: IFR only changes between c6x_run calls
 *     (Python raises events there) or by the DSP's own MVC/MMIO writes,
 *     which this routine does not make;
 *   - the whole call fits in the slice's remaining cycles.
 * The first call with a given n is LEARNED by running it normally; it is
 * accepted only if the code bytes are the expected routine, no interrupt /
 * Python op happened, every register outside the written set is unchanged,
 * the written set and the stack word equal the closed form, and control
 * registers are unchanged. The cycle count, statistics and the finished
 * SPLOOP state are recorded and replayed verbatim. A mismatch disables
 * the shortcut for good. c6x_set_dff(c, 0) turns it off (XDJ_DSP_DFF=0). */
static const u32 DFF_SIG[22] = {
    0x07BF005Au, 0xBC450627u, 0x6C6EBC4Du, 0x28679E28u, 0xD028A120u, 0x2683E000u,
    0x00010000u, 0xE1C00080u, 0x00000000u, 0x22BC22E6u, 0x2CE62C6Eu, 0x0C6ED247u,
    0xAC3536B0u, 0x6C6EBC4Du, 0x02188AFAu, 0xE7800020u, 0x2C6E0247u, 0x00000000u,
    0x00034000u, 0x00000000u, 0x07BD005Au, 0x008CA362u };
enum { RA0 = 0, RA3 = 3, RB0 = 32, RB3 = 35, RB4 = 36, RB5 = 37, RB6 = 38, RB15 = 47 };

void c6x_set_dff(Core *c, int on) { c->dff_on = on; c->dff_nt = 0; c->dff_learn = 0; }
/* 0 calls replayed, 1 cycles replayed, 2 on | learned << 8 | learning << 16 */
u64 c6x_dff_stat(Core *c, int k)
{
    switch (k) {
    case 0: return c->dff_hits;
    case 1: return c->dff_cycles;
    default: return (u64)c->dff_on | (u64)c->dff_nt << 8 | (u64)c->dff_learn << 16;
    }
}

static int dff_sig_ok(Core *c)
{
    uint8_t *p = ram(c, DFF_PC, sizeof DFF_SIG);
    return p && !memcmp(p, DFF_SIG, sizeof DFF_SIG);
}

static inline int dff_clean(Core *c)
{
    return !c->stall && !c->sp_on && !c->splx_irq && !c->evcount && !c->npend &&
           !(c->wlo < c->whi) && !c->c[C_AMR] && !(c->c[C_TSR] & SPLX);
}

static inline int dff_int_possible(Core *c)
{
    u32 *r = c->c;
    return (r[C_CSR] & 1) && (r[C_IER] & 2) && (r[C_IFR] & r[C_IER] & 0xFFF0);
}

/* the recorded end state of call k (registers, stack word, SPLOOP state,
 * statistics, pc); cycle already at the end */
static void dff_finish(Core *c, int k, u32 sp)
{
    s32 n = c->dff_t[k].n;
    u32 a = sp - 4;
    uint8_t *p = ram(c, a, 4);
    /* the routine's stores (0, 1, .., n at b15-4) through mwrite's idle
     * rule: they change memory, so outside the idle stack they dirty */
    u32 g = (a >= 0x00800000u && a < 0x00840000u) ? a + 0x11000000u : a;
    if (!c->idle_dirty && (g < c->idle_lo || g >= c->idle_hi)) c->idle_dirty = 1;
    u32 v = (u32)n; memcpy(p, &v, 4);
    c->r[RA0] = 1; c->r[RA3] = (u32)n; c->r[RB0] = 0; c->r[RB4] = 0;
    c->r[RB5] = (u32)(n - 1); c->r[RB6] = (u32)n;
    c->sp = c->dff_t[k].sp;
    c->ep_count += c->dff_t[k].dep; c->insn_count += c->dff_t[k].dins;
    c->pc = c->r[RB3];
    c->hint = -1; c->prevep = -1;
    c->dff_hits++; c->dff_cycles += c->dff_t[k].T;
}

/* at a packet boundary with pc == DFF_PC: replay a learned call or start
 * learning one. Returns cycles consumed now (0 = run normally, or the call
 * is frozen and c6x_run counts its cycles down). */
static u64 dff_enter(Core *c, s64 budget)
{
    s32 n = (s32)c->r[4];
    if (c->dff_replay || n < 2 || n > 0x01000000 || !dff_clean(c) || dff_int_possible(c))
        return 0;
    for (int k = 0; k < c->dff_nt; k++) {
        if (c->dff_t[k].n != n) continue;
        u64 T = c->dff_t[k].T;
        u32 sp = c->r[RB15];
        uint8_t *p = ram(c, sp - 4, 4);
        if (!p) return 0;
        if ((s64)T <= budget) {                 /* whole call now */
            c->cycle += T;
            dff_finish(c, k, sp);
            return T;
        }
        if (c->c[C_CSR] & 1) return 0;          /* interruptible: run it */
        /* interrupts are off (GIE = 0): nothing outside can change the
         * call's course, so it is FROZEN: the cycles are counted down by
         * c6x_run and the end state applied at its exact cycle. Anything
         * that looks inside meanwhile goes through c6x_settle(), which
         * re-runs the call from this entry state to the current cycle. */
        memcpy(c->dff_in.r, c->r, sizeof c->r);
        memcpy(&c->dff_in.word, p, 4);
        c->dff_in.cyc = c->cycle; c->dff_in.ep = c->ep_count; c->dff_in.ins = c->insn_count;
        c->dff_in.hint = c->hint; c->dff_in.prevep = c->prevep; c->dff_in.lastf = c->lastf;
        c->dff_in.idle_dirty = c->idle_dirty;
        c->dff_in.sp = c->sp;
        c->dff_frozen = 1; c->dff_fk = k; c->dff_end = c->cycle + T;
        return 0;
    }
    if (c->dff_nt >= 4 || c->dff_learn || !dff_sig_ok(c)) return 0;
    c->dff_learn = 1;
    c->dff_pre.n = n;
    memcpy(c->dff_pre.r, c->r, sizeof c->r);
    memcpy(c->dff_pre.c, c->c, sizeof c->c);
    c->dff_pre.b3 = c->r[RB3]; c->dff_pre.sp = c->r[RB15];
    c->dff_pre.cyc = c->cycle; c->dff_pre.ep = c->ep_count; c->dff_pre.ins = c->insn_count;
    c->dff_pre.ints = c->interrupts; c->dff_pre.py = c->py_calls;
    return 0;
}

s64 c6x_run(Core *c, s64 n);

/* frozen call: make the visible state exactly what running it would have
 * produced by now (restore the entry state, run the elapsed cycles with
 * the shortcut held off). Cheap no-op when nothing is frozen. */
void c6x_settle(Core *c)
{
    if (!c->dff_frozen) return;
    u64 now = c->cycle;
    c->dff_frozen = 0;
    memcpy(c->r, c->dff_in.r, sizeof c->r);
    uint8_t *p = ram(c, c->dff_in.r[RB15] - 4, 4);
    memcpy(p, &c->dff_in.word, 4);
    c->cycle = c->dff_in.cyc; c->ep_count = c->dff_in.ep; c->insn_count = c->dff_in.ins;
    c->hint = c->dff_in.hint; c->prevep = c->dff_in.prevep; c->lastf = c->dff_in.lastf;
    c->idle_dirty = c->dff_in.idle_dirty;
    c->sp = c->dff_in.sp;
    c->pc = DFF_PC;
    int st = c->status;
    c->dff_replay = 1;
    while (c->cycle < now) {
        s64 k = c6x_run(c, (s64)(now - c->cycle));
        if (k <= 0 || c->status != ST_OK) break;      /* cannot happen: learned code */
    }
    c->dff_replay = 0;
    c->status = st;
}

int c6x_dff_frozen(Core *c) { return c->dff_frozen; }

/* learning: called at every packet boundary while dff_learn is set */
static void dff_check_learn(Core *c)
{
    if (c->pc != c->dff_pre.b3 || c->sp_on || c->stall) {
        if (c->cycle - c->dff_pre.cyc > 50000000ull) c->dff_learn = 0;   /* lost it */
        return;
    }
    c->dff_learn = 0;
    if (!dff_clean(c) || c->interrupts != c->dff_pre.ints || c->py_calls != c->dff_pre.py)
        return;                                    /* disturbed: try again later */
    s32 n = c->dff_pre.n;
    int ok = 1;
    for (int i = 0; i < 64; i++) {
        u32 want;
        switch (i) {
        case RA0: want = 1; break;
        case RA3: want = (u32)n; break;
        case RB0: want = 0; break;
        case RB4: want = 0; break;
        case RB5: want = (u32)(n - 1); break;
        case RB6: want = (u32)n; break;
        default: want = c->dff_pre.r[i];
        }
        if (c->r[i] != want) ok = 0;
    }
    if (memcmp(c->c, c->dff_pre.c, sizeof c->c)) ok = 0;
    uint8_t *p = ram(c, c->dff_pre.sp - 4, 4);
    u32 v = 0; if (p) memcpy(&v, p, 4);
    if (!p || v != (u32)n) ok = 0;
    if (!ok) { c->dff_on = 0; return; }           /* not the routine we know */
    int k = c->dff_nt++;
    c->dff_t[k].n = n;
    c->dff_t[k].T = c->cycle - c->dff_pre.cyc;
    c->dff_t[k].dep = c->ep_count - c->dff_pre.ep;
    c->dff_t[k].dins = c->insn_count - c->dff_pre.ins;
    c->dff_t[k].sp = c->sp;
}

/* run up to n cycles; returns cycles completed; c6x_status() says why */
s64 c6x_run(Core *c, s64 n)
{
    c->status = ST_OK;
    s64 k = 0;
    /* the host may have written DSP memory or raised events since the last
     * call: the first idle-head visit re-checks the pass's read set */
    c->idle_fresh = 1;
    c->run_end = c->cycle + (u64)n;
    while (k < n) {
        /* multi-cycle NOP / load-delay stall with nothing in flight that
         * lands meanwhile: those cycles only count down (no interrupt is
         * taken while stalled, no event commits, no branch lands) -- jump */
        if (c->stall > 1 && !c->sp_on && !c->splx_irq) {
            s64 j = c->stall - 1;               /* leave the last one to step() */
            if (j > n - k) j = n - k;
            for (int q = 0; q < c->npend; q++) {
                s64 d = (s64)(c->pend_cyc[q] - c->cycle);
                if (d < j) j = d;               /* a branch lands at cycle+d */
            }
            /* events in flight (a load landing during the NOP): the
             * cycles before the first non-empty bucket commit nothing,
             * so they jump too (every event sits < RING cycles ahead) */
            if (c->evcount)
                for (s64 d = 0; d < j && d < RING; d++) {
                    Bucket *b = &c->ring[(c->cycle + (u64)d) % RING];
                    if (b->n || b->rn) { j = d; break; }
                }
            if (j > 0) {
                c->stall -= (int)j;
                c->cycle += (u64)j;
                k += j;
                continue;
            }
        }
        if (c->stall && !c->sp_on && !c->splx_irq) {
            /* a stall cycle: exactly step()'s path for it (no interrupt
             * while stalled, no fetch) without the rest of its checks */
            u64 cyc = c->cycle;
            c->stall--;
            commit(c, cyc);
            for (int q = 0; q < c->npend; q++)
                if (c->pend_cyc[q] == cyc) {
                    c->pc = c->pend_tgt[q];
                    c->npend--;
                    c->pend_cyc[q] = c->pend_cyc[c->npend];
                    c->pend_tgt[q] = c->pend_tgt[c->npend];
                    c->stall = 0;
                    break;
                }
            c->cycle = cyc + 1;
            if (c->status) {
                if (c->status == ST_STOP) k++;
                break;
            }
            k++;
            continue;
        }
        if (c->dff_frozen) {                    /* counting a frozen call down */
            u64 left = c->dff_end - c->cycle;
            s64 adv = (s64)left < n - k ? (s64)left : n - k;
            c->cycle += (u64)adv;
            k += adv;
            if (c->cycle == c->dff_end) {
                c->dff_frozen = 0;
                dff_finish(c, c->dff_fk, c->dff_in.r[RB15]);
            }
            continue;
        }
        if (c->dff_on && !c->dff_replay) {
            if (c->dff_learn) dff_check_learn(c);
            else if (c->pc == DFF_PC) {
                u64 t = dff_enter(c, n - k);
                if (t) { k += (s64)t; continue; }
                if (c->dff_frozen) continue;
            }
        }
        int st = step(c);
        k += (s64)c->idle_jump;                 /* whole idle passes jumped */
        c->idle_jump = 0;
        if (st < 0) {
            if (c->status == ST_STOP) k++;      /* that cycle completed */
            break;
        }
        k++;
    }
    c->run_end = 0;
    return k;
}

int c6x_status(Core *c) { return c->status; }
u32 c6x_need(Core *c) { return c->need; }
int c6x_err(Core *c) { return c->err; }
u32 c6x_err_pc(Core *c) { return c->err_pc; }
u32 c6x_pc(Core *c) { return c->pc; }
void c6x_set_pc(Core *c, u32 pc) { c->dff_frozen = 0; c->dff_learn = 0; c->pc = pc; }
u64 c6x_cycle(Core *c) { return c->cycle; }
void c6x_set_cycle(Core *c, u64 v) { c->dff_frozen = 0; c->dff_learn = 0; c->cycle = v; }
int c6x_stall(Core *c) { return c->stall; }
int c6x_evcount(Core *c) { return c->evcount; }
int c6x_npend(Core *c) { return c->npend; }
int c6x_sp_on(Core *c) { return c->sp_on; }
u32 c6x_idle(Core *c) { return c->idle; }
void c6x_set_idle(Core *c, u32 v) { c->idle = v; }
/* 0 packets, 1 instructions, 2 interrupts, 3 Python ops, 4 idle cycles
 * jumped, 5 idle jumps, 6 jumps from the reference pass */
u64 c6x_counts(Core *c, int k)
{ return k == 0 ? c->ep_count : k == 1 ? c->insn_count : k == 2 ? c->interrupts :
         k == 3 ? c->py_calls : k == 4 ? c->idle_jumped : k == 5 ? c->idle_jumps : c->idle_refhits; }

u64 c6x_prof(Core *c, int k, u32 *addr)
{
#ifdef C6X_PROF
    if (k == 100) return c->prof_stall;
    if (k == 101) return c->prof_run;
    if (k < 0) { if (addr) addr[0] = (u32)c->cyc_sp; return c->cyc_norm; }
    if (k >= c->spn) { if (addr) addr[0] = 0; return 0; }
    if (addr) addr[0] = c->spa[k];
    return c->spc[k];
#else
    (void)c; (void)k; if (addr) addr[0] = 0; return 0;
#endif
}

u32 c6x_hist(Core *c, int i)
{
#ifdef C6X_PROF
    return c->hist ? c->hist[i & 0xFFFF] : 0;
#else
    (void)c; (void)i; return 0;
#endif
}

void c6x_watch(Core *c, u32 lo, u32 hi) { c->wlo = lo; c->whi = hi; c->wn = 0; }
int c6x_watch_get(Core *c, int k, u32 *out)
{
    int i = k & 255;
    out[0] = c->wpc[i]; out[1] = c->wad[i]; out[2] = c->wv[i]; out[3] = (u32)c->wcy[i];
    return c->wn;
}

/* idle skip (replay of cdj-nxs2-qemu recordings): writes in flight land
 * as if the cycles ran, then the clock jumps */
void c6x_skip(Core *c, u64 n)
{
    for (int k = 0; k < RING && n; k++, n--) { commit(c, c->cycle); c->cycle++; }
    c->cycle += n;
}
int c6x_splx_pending(Core *c) { return c->splx_irq; }

/* debug only: live SPLOOP state + buffer entries (addr, lc/lbc/unit word).
 * out[0..7] = kind, ii, lc, lbc, addr, nbuf, nhist, ILC; then 2 per entry.
 * Returns the number of words written (0 when no SPLOOP is active). */
int c6x_sp_info(Core *c, u32 *out, int max)
{
    SPLoop *s = &c->sp;
    if (!c->sp_on || max < 9) return 0;
    int n = 0;
    out[n++] = (u32)s->kind; out[n++] = (u32)s->ii; out[n++] = (u32)s->lc;
    out[n++] = (u32)s->lbc; out[n++] = s->addr; out[n++] = (u32)s->nbuf;
    out[n++] = (u32)s->nhist; out[n++] = (u32)c->c[C_ILC];
    for (int k = 0; k < s->nbuf && n + 2 <= max; k++) {
        out[n++] = c->ins[s->bi[k]].addr;
        out[n++] = (u32)s->valid[s->bl[k]] | (u32)s->bl[k] << 8 | (u32)s->bu[k] << 16;
    }
    return n;
}

void c6x_set_idle_loop(Core *c, u32 head, u32 lo, u32 hi)
{
    c->idle_head = head; c->idle_lo = lo; c->idle_hi = hi; c->idle_armed = 0;
    c->nidle = head ? 1 : 0; c->idle_heads[0] = head; c->idle_last = -1;
    free(c->idle_stk); c->idle_stk = 0; c->idle_stkn = 0;
    free(c->ref_stk); c->ref_stk = 0; c->ref_ok = 0;
    if (head && hi > lo && hi - lo <= 0x10000u) {
        c->idle_stkn = hi - lo;
        c->idle_stk = malloc(c->idle_stkn);
        c->ref_stk = malloc(c->idle_stkn);
        if (!c->idle_stk || !c->ref_stk) {
            free(c->idle_stk); free(c->ref_stk);
            c->idle_stk = c->ref_stk = 0; c->idle_stkn = 0;
        }
    }
}
/* one more loop of the idle task (same stack) for the busy-wait skip */
int c6x_add_idle_head(Core *c, u32 head)
{
    if (!head || c->nidle >= 4) return 0;
    for (int k = 0; k < c->nidle; k++) if (c->idle_heads[k] == head) return 1;
    c->idle_heads[c->nidle++] = head; c->idle_armed = 0; c->idle_last = -1; c->ref_ok = 0;
    return 1;
}

/* ================================================ McASP DMA burst ======
 * The inner loop of periph.Edma3.burst (the common A-sync case: 4-byte
 * arrays from DSP RAM to a McASP data port), word for word the Python
 * loop: up to n words from buf (region base `lo`, valid offsets
 * 0..blen) into out, PaRAM set `p` (32 bytes) updated as the Python
 * _pw writes (src, abc, ccnt). Returns the words moved. */
/* ABSYNC frame copy of periph.Edma3.transfer (Pioneer's MP3 audio path:
 * 588 frames of 4 bytes per event, source stride 4, destination stride 8).
 * One frame = read acnt bytes, then write them (never a partial overlap
 * within the frame -- exactly what Python's _copy does word by word).
 * Both ranges are checked against their RAM buffers; copying stops at the
 * first frame that leaves one, and the count returns. */
int c6x_dma_frames(const uint8_t *sb, s64 slen, u32 slo,
                   uint8_t *db, s64 dlen, u32 dlo,
                   u32 src, u32 dst, u32 acnt, u32 bcnt, int sbidx, int dbidx)
{
    s64 so = (s64)src - (s64)slo, dof = (s64)dst - (s64)dlo;
    u32 k = 0;
    for (; k < bcnt; k++, so += sbidx, dof += dbidx) {
        if (so < 0 || so + acnt > slen) break;
        if (dof < 0 || dof + acnt > dlen) break;
        if (acnt == 4) {
            u32 v; memcpy(&v, sb + so, 4); memcpy(db + dof, &v, 4);
        } else {
            uint8_t t[16];
            if (acnt <= 16) {
                memcpy(t, sb + so, acnt); memcpy(db + dof, t, acnt);
            } else {
                memmove(db + dof, sb + so, acnt);
            }
        }
    }
    return (int)k;
}

int c6x_dma_words(const uint8_t *buf, s64 blen, u32 lo, uint8_t *p, int n,
                  u32 *out, int muted)
{
    u32 w[8];
    memcpy(w, p, 32);
    s64 src = w[1];
    u32 acnt = w[2] & 0xFFFF, bcnt = w[2] >> 16, ccnt = w[7] & 0xFFFF;
    s64 sbidx = (int16_t)(w[4] & 0xFFFF), scidx = (int16_t)(w[6] & 0xFFFF);
    u32 bcntrld = w[5] >> 16;
    int done = 0;
    while (done < n && ccnt) {
        s64 o = src - (s64)lo;
        if (o < 0 || o > blen) break;
        u32 v; memcpy(&v, buf + o, 4);
        out[done++] = muted ? 0 : v;
        if (bcnt > 1) { src += sbidx; bcnt--; }
        else { src += scidx; bcnt = bcntrld; ccnt--; }
    }
    w[1] = (u32)src;
    w[2] = (bcnt << 16) | acnt;
    w[7] = ccnt;
    memcpy(p + 4, &w[1], 8);
    memcpy(p + 28, &w[7], 4);
    return done;
}
