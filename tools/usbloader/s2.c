/* s2.c -- stage-2 installer: the MODS.BIN payload that puts a whole mods
 * pack into deck RAM from the stick (emulator only so far).
 *
 * The stage-1 loader (ul.c) reads MODS.BIN (this code) into UL_PAY and
 * calls it through the uncached P2 alias with r4 = &ul_state, which holds
 * the mounting caller's drive letter, open mode and ctx. This code then
 * opens <drive>:/PIONEER/MODS/PACK.BIN and streams it:
 *
 *   header 32 B {u32 "XPK1", u32 version 1, u32 nrec, u32 bytes (all
 *                record headers + data after this header), u32 sum
 *                (32-bit sum of those bytes), u32 rsv[3]}
 *   nrec records, each {u32 dst, u32 len, u32 flags, u32 expect} + data
 *                (len rounded up to 4 bytes; none for ZERO records)
 *
 *   flags  ZERO    fill dst..+len with zeros (no data in the file)
 *          SURVEY  dst..+len must read all zero before anything is written
 *                  there (an empty RAM home, as the cable installers check)
 *          EXPSUM  the 32-bit byte sum of the live bytes must equal
 *                  `expect` before the write (a picture still exactly stock)
 *          ARM     len 4, data = the word that makes the deck use a mod
 *                  (pool / hook / entry word); the live word must equal
 *                  `expect` (stock). Arms are QUEUED, not written: only
 *                  after the whole file streamed with the right sum and no
 *                  error are they written, in file order, last.
 *
 * Memory: every record is read through the staging buffer with the same
 * cache dance as ul.c (ocbp before and after the fread, copy-back cache),
 * then copied to dst through the P2 alias (no dirty lines for dst), and
 * dst's lines are purged before and after (stale clean lines of a pool
 * word would otherwise serve the old value). Before the arms the whole
 * instruction cache is invalidated (CCR.ICI: patches to firmware code must
 * not run from stale lines). The pack builder puts arms last and never
 * lets a record touch RAM outside the surveyed homes / known stock sites.
 * Any problem = close + status, nothing armed: RAM-only, a power cycle is
 * stock again.
 */
typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;

typedef void *(*open_t)(const u16 *path, const void *mode, void *ctx);
typedef int (*close_t)(void *h, void *ctx);
typedef int (*fread_t)(void *buf, int size, int count, void *fp);

#define STOCK_OPEN  ((open_t)0x08DDF53Cu)
#define STOCK_CLOSE ((close_t)0x08DDF992u)
#define STOCK_FREAD ((fread_t)0x08C61A6Cu)
#define CCR (*(volatile u32 *)0xFF00001Cu)
#define CCR_ICI 0x800u

#define P0(a) ((u32)(a) & 0x1FFFFFFFu)          /* cached address, as the firmware uses */
#define P2(a) (P0(a) | 0xA0000000u)             /* uncached alias */

enum { F_ZERO = 1, F_SURVEY = 2, F_EXPSUM = 4, F_ARM = 8 };
enum { S2_IDLE, S2_NOFILE, S2_SHORT, S2_MAGIC, S2_COUNT, S2_SURVEY, S2_EXPSUM,
       S2_BADREC, S2_SUM, S2_ARMSTOCK, S2_DONE };

#define STAGE 8192
#define MAXARM 64

struct ul_state {                 /* ul.c, r4 at entry */
    u32 magic, calls, mark, status, tries, loads, len, sum, done;
    u32 drive, mode, ctx;
};

struct s2_state {
    u32 magic;        /* 'S2ST' */
    u32 status;       /* S2_* */
    u32 nrec;         /* records in the file header */
    u32 done;         /* records streamed */
    u32 bytes;        /* bytes placed in RAM (data + zero) */
    u32 armed;        /* arm words written */
    u32 err_rec;      /* record index of the failure */
    u32 err_addr;     /* its address */
    u32 sum, want;    /* stream sum seen / in the header */
} s2_state = { 0x54533253 };

static u32 arm_dst[MAXARM], arm_val[MAXARM], arm_exp[MAXARM];
static u32 stage[STAGE / 4] __attribute__((aligned(32)));

static const u16 PACKPATH[] = {'C', ':', '/', 'P', 'I', 'O', 'N', 'E', 'E', 'R',
    '/', 'M', 'O', 'D', 'S', '/', 'P', 'A', 'C', 'K', '.', 'B', 'I', 'N', 0};

static void purge(u32 a, u32 n)
{
    for (u32 p = P0(a) & ~31u; p < P0(a) + n; p += 32)
        __asm__ volatile("ocbp @%0" : : "r"(p) : "memory");
}

/* fread n bytes (n <= STAGE) into the staging buffer; -> bytes read.
 * The stream sum is kept over the bytes as they arrive. */
static int pull(void *h, u32 n)
{
    purge((u32)stage, n);
    int got = STOCK_FREAD((void *)P0(stage), 1, (int)n, h);
    purge((u32)stage, n);
    if (got > 0) {
        const volatile u8 *b = (const volatile u8 *)P2(stage);
        u32 s = s2_state.sum;
        for (int i = 0; i < got; i++) s += b[i];
        s2_state.sum = s;
    }
    return got;
}

static u32 live_sum(u32 a, u32 n)
{
    purge(a, n);
    const volatile u8 *b = (const volatile u8 *)P2(a);
    u32 s = 0;
    for (u32 i = 0; i < n; i++) s += b[i];
    return s;
}

static int fail(u32 st, u32 rec, u32 addr)
{
    s2_state.status = st;
    s2_state.err_rec = rec;
    s2_state.err_addr = addr;
    return 0;
}

static int record(void *h, u32 i)
{
    u32 hd[4];
    if (pull(h, 16) != 16) return fail(S2_SHORT, i, 0);
    const volatile u32 *sp = (const volatile u32 *)P2(stage);
    hd[0] = sp[0]; hd[1] = sp[1]; hd[2] = sp[2]; hd[3] = sp[3];
    u32 dst = hd[0], len = hd[1], fl = hd[2], exp = hd[3];
    if ((dst & 3) || !len || (len & 3) || (fl & F_ARM && len != 4))
        return fail(S2_BADREC, i, dst);
    if (fl & F_ARM) {
        if (s2_state.armed >= MAXARM) return fail(S2_BADREC, i, dst);
        if (pull(h, 4) != 4) return fail(S2_SHORT, i, dst);
        arm_dst[s2_state.armed] = dst;
        arm_val[s2_state.armed] = sp[0];
        arm_exp[s2_state.armed] = exp;
        s2_state.armed++;             /* queued; written at the end */
        return 1;
    }
    if (fl & F_SURVEY && live_sum(dst, len)) return fail(S2_SURVEY, i, dst);
    if (fl & F_EXPSUM && live_sum(dst, len) != exp) return fail(S2_EXPSUM, i, dst);
    volatile u32 *d = (volatile u32 *)P2(dst);
    purge(dst, len);
    if (fl & F_ZERO) {
        for (u32 k = 0; k < len / 4; k++) d[k] = 0;
    } else {
        for (u32 off = 0; off < len; off += STAGE) {
            u32 n = len - off < STAGE ? len - off : STAGE;
            if (pull(h, n) != (int)n) return fail(S2_SHORT, i, dst + off);
            for (u32 k = 0; k < n / 4; k++) d[off / 4 + k] = sp[k];
        }
    }
    purge(dst, len);
    s2_state.bytes += len;
    return 1;
}

void s2_main(struct ul_state *ul)
{
    u16 p[sizeof PACKPATH / 2];
    for (u32 i = 0; i < sizeof PACKPATH / 2; i++) p[i] = PACKPATH[i];
    if (ul->drive) p[0] = (u16)ul->drive;
    ul->mark = (u32)&s2_state;            /* where to look */
    s2_state.status = S2_IDLE;
    s2_state.sum = s2_state.armed = s2_state.done = s2_state.bytes = 0;
    void *h = STOCK_OPEN(p, (const void *)ul->mode, (void *)ul->ctx);
    if (!h) { s2_state.status = S2_NOFILE; return; }
    if (pull(h, 32) != 32) { fail(S2_SHORT, 0, 0); goto out; }
    const volatile u32 *sp = (const volatile u32 *)P2(stage);
    u32 nrec = sp[2], want = sp[4];
    if (sp[0] != 0x314B5058u || sp[1] != 1) { fail(S2_MAGIC, 0, 0); goto out; }
    if (!nrec || nrec > 4096) { fail(S2_COUNT, 0, 0); goto out; }
    s2_state.nrec = nrec;
    s2_state.want = want;
    s2_state.sum = 0;                     /* the sum covers what follows the header */
    for (u32 i = 0; i < nrec; i++) {
        if (!record(h, i)) goto out;
        s2_state.done = i + 1;
    }
    if (s2_state.sum != want) { fail(S2_SUM, nrec, 0); goto out; }
    /* every arm word must still read what the pack expects before any of
     * them is written; a chain (hot cues -> phase meter -> pads all re-point
     * the flush pool) expects the earlier arm's value, not the live one */
    for (u32 k = 0; k < s2_state.armed; k++) {
        purge(arm_dst[k], 4);
        u32 v = *(volatile u32 *)P2(arm_dst[k]);
        for (u32 j = 0; j < k; j++)
            if (arm_dst[j] == arm_dst[k]) v = arm_val[j];
        if (v != arm_exp[k]) { fail(S2_ARMSTOCK, k, arm_dst[k]); goto out; }
    }
    CCR = CCR | CCR_ICI;                  /* patched firmware code: no stale I-lines */
    for (u32 k = 0; k < s2_state.armed; k++) {
        *(volatile u32 *)P2(arm_dst[k]) = arm_val[k];
        purge(arm_dst[k], 4);
    }
    s2_state.status = S2_DONE;
out:
    STOCK_CLOSE(h, (void *)ul->ctx);
}
