/*
 * lc.c -- on-deck USB-stick loader (docs/USBLOADER.md milestones 2 + 2b).
 * Freestanding SH4 C, no FPU, no libc.
 *
 * ONE pack parser, two byte sources:
 *
 *   lc_entry(r4 pack, r5 plen) -> r0 mask        [build: lc.bin]
 *       pack already in RAM (core proof, tools/usbloader_test.py core).
 *
 *   ld_open(path, mode, ctx)                     [build -DSTICK: ld.bin]
 *       sits on the SURVEYED open pool word 0x08DE39A4 of the deck's
 *       MYSETTING.DAT mount reader (FUN 0x08DE34F2, USB task, runs on
 *       every stick mount; pool users exactly 0x08DE38BA + 0x08DE3904,
 *       no literal/alias refs -- whole-image scan, re-run 2026-11-01).
 *       On a mount it opens <drive>:/XDJMODS/PACK.BIN with the CALLER'S
 *       OWN mode/ctx through the stock open 0x08DDF53C, streams it with
 *       stdio fread 0x08C61A6C (what the media read tail-jumps to for a
 *       USB handle), closes with 0x08DDF992, then chains to the stock
 *       open with the caller's args. The deck's own FAT driver does the
 *       FAT work: no second USB/FAT stack, no guessed boot code.
 *
 * Pack format XDJP001 rev 2 = a recorded cable install, see parse()
 * below (tools/stick_pack.py). The rev-1 description that follows is
 * the 2026-11-01 format, kept for the record:
 *   [0x00] magic "XDJP001\0" (8 B), u16 rev == 1, u16 count n,
 *          u32 CRC32 over the ENTRY TABLE (0x10 .. 0x10+28n)
 *   [0x10] n entries x 28 B: name[8] ram u32 declen u32 crc u32
 *          arm u16 pad u16 zlen u32
 *   bodies: LZSS (lzss_boot.py codec), each body's FILE offset aligned
 *   0x10, entry order, CRC over the body UNcompressed.
 *
 * Fail-closed, per payload: a home must lie wholly inside one of the
 * deck-surveyed free bands, must not touch the loader, and must read
 * ALL ZERO before we write (never decode over live code -- e.g. a mod
 * already installed by cable); the decode must give exactly declen
 * bytes; the CRC must match. Anything else = no mask bit. Header, rev,
 * count or table CRC wrong = nothing written at all. The loader never
 * writes flash. Mask bits are entry indices (count <= 32).
 */
typedef unsigned int u32;
typedef unsigned short u16;
typedef unsigned char u8;

#define N     4096u
#define R0    4078u              /* ring head at stream start */
#define MAXE  1200u              /* recorded runs per pack. 2026-11-03:
                                  * was 1024; a full install (RGB +
                                  * phase meter + TOUCH FX + stems)
                                  * recorded 1036, so the pack could
                                  * not be built. The table lives in
                                  * the loader's own state area (24 B
                                  * a run), which still ends well
                                  * inside the free 64 KB at
                                  * 0x09890000. */
#define CAP20 (1024u * 1024u)    /* 1 MB sanity cap per length */

#ifndef LC_SELF_LO
#error LC_SELF_LO/LC_SELF_HI come from build.sh
#endif

/* deck-surveyed free bands (CLAUDE.md 77, 81): zero idle/loaded/playing */
static const u32 BANDS[][2] = {
    { 0x0972B000u, 0x0980F000u },
    { 0x09836000u, 0x0996D000u },
    { 0x0996E000u, 0x09A00000u },
};

/* why the last payload was rejected (lc_why) */
enum { W_OK, W_SIZE, W_BAND, W_LIVE, W_SHORT, W_CRC };

static u8 ring[N];
static u8 tbl[MAXE * 24];

u32 lc_seen;                    /* packs that passed the header checks */
u32 lc_bad;                     /* payloads rejected (any reason) */
u32 lc_why;                     /* last rejection reason (W_*) */
u32 lc_mask;                    /* last mask */

static const u8 MAGIC[8] = { 'X', 'D', 'J', 'P', '0', '0', '1', 0 };

/* ---- byte source: a chunk (cb, clen) + refill; pos = file offset ---- */
static const u8 *cb;
static u32 cpos, clen, pos;
static int dead;
static int (*refill)(void);     /* sets cb/clen, returns clen (0 = end) */

static int getb(void)
{
    if (cpos >= clen) {
        if (dead || !refill || refill() <= 0) { dead = 1; return -1; }
        cpos = 0;
    }
    pos++;
    return cb[cpos++];
}

static int getn(u8 *d, u32 n)
{
    u32 i;
    for (i = 0; i < n; i++) {
        int c = getb();
        if (c < 0) return 0;
        d[i] = (u8)c;
    }
    return 1;
}

/* binascii.crc32-compatible: start 0, reflected 0xEDB88320, final ~ */
static u32 crc32z(const volatile u8 *p, u32 n)
{
    u32 c = 0xFFFFFFFFu, i, k;
    for (i = 0; i < n; i++) {
        c ^= p[i];
        for (k = 0; k < 8; k++)
            c = (c >> 1) ^ (0xEDB88320u & (0u - (c & 1u)));
    }
    return ~c;
}

static u32 le32(const u8 *p)
{
    return p[0] | (u32)p[1] << 8 | (u32)p[2] << 16 | (u32)p[3] << 24;
}

static int home_zero(u32 home, u32 len)
{
    const volatile u8 *p = (const volatile u8 *)home;
    u32 i;
    for (i = 0; i < len; i++)
        if (p[i]) return 0;
    return 1;
}

/* stream-decode zlen compressed bytes into D (at most cap bytes; never
 * a byte past cap). Always consumes exactly zlen bytes of the source
 * when it can, so the next body's alignment holds. Returns decoded
 * length, or -1 if the source ended. Same semantics as lzss_boot.decode
 * on the zlen-byte slice. */
static int lzss(u32 zlen, volatile u8 *D, u32 cap)
{
    u32 used = 0, n = 0, i, rp = R0;
    int c;
    for (i = 0; i < R0; i++) ring[i] = 0x20;
    for (; i < N; i++) ring[i] = 0;
    while (used < zlen && n < cap) {
        int k, fl;
        if ((fl = getb()) < 0) return -1;
        used++;
        for (k = 0; k < 8; k++) {
            if (used >= zlen || n >= cap) break;
            if (fl & 1) {
                if ((c = getb()) < 0) return -1;
                used++;
                D[n++] = (u8)c; ring[rp] = (u8)c; rp = (rp + 1) & (N - 1);
            } else {
                int b1, b2;
                u32 idx, L, j;
                if (used + 1 >= zlen) break;
                if ((b1 = getb()) < 0 || (b2 = getb()) < 0) return -1;
                used += 2;
                idx = (u32)b1 | ((u32)(b2 & 0xF0) << 4);
                L = (u32)(b2 & 0x0F) + 3;
                for (j = 0; j < L && n < cap; j++) {
                    u8 v = ring[(idx + j) & (N - 1)];
                    D[n++] = v; ring[rp] = v; rp = (rp + 1) & (N - 1);
                }
            }
            fl >>= 1;
        }
    }
    for (; used < zlen; used++)          /* rest of the body (cap hit) */
        if (getb() < 0) return -1;
    return (int)n;
}

/* ---- pack rev 2: a RECORDED cable install, replayed ----
 * tools/stick_pack.py runs the real deck installers against the mock
 * deck (an emulator answering the USB shell) and records every RAM write
 * in order; coalesced runs become the entries below. Replaying them here
 * gives the same bytes and hook order as the cable install.
 *
 * entry, 24 B: addr u32, len u32, crc u32 (CRC32 of the new bytes),
 *              old u32 (CRC32 of the bytes there before), flags u16,
 *              pad u16, zlen u32 (LZSS body length)
 * flags: F_ZERO = the run must read all zero before (a cave in a deck-
 *        surveyed free band); F_OLD = the run's first-touch bytes must
 *        CRC to `old` (stock pool words / settings the installer checked).
 *
 * TWO PHASES, fail-closed: (1) the whole TABLE is checked against RAM --
 * address window, never the loader, every precondition -- and if one
 * entry fails, NOTHING is written (lc_why says why, lc_bad = its index+1).
 * (2) bodies are decoded in table order; small runs (<= 64 B: pool words,
 * flags) are decoded into a buffer, CRC-checked, then stored with aligned
 * word writes so a live pool never holds a torn pointer; big runs decode
 * in place (caves: zero, not yet referenced) and are CRC-checked. The
 * first body that fails STOPS the replay: later entries -- which include
 * the hook arms, recorded last per mod -- are never applied. Every written
 * range is written back from the operand cache and dropped from the
 * instruction cache (code the deck will now execute). */
#define F_ZERO 1u
#define F_OLD  2u
#define F_CALL 4u   /* addr = a void(void) one-shot to run NOW (len 0, no
                     * body): a step the cable install leaves to the next
                     * screen flush, e.g. live key shift's DSP patch. It is
                     * re-called while the u32 at `old` still equals `crc`
                     * (its "busy, try again" status), at most 600 times. */
#define ESZ    24u
#define SMALL  64u
enum { W2_WINDOW = 6, W2_ZERO, W2_OLD, W2_COUNT };
u32 lc_applied;                 /* entries applied in phase 2 */
static u8 small[SMALL] __attribute__((aligned(4)));

static int in_window(u32 a, u32 n)
{
    u32 e = a + n;
    if (n == 0 || e < a) return 0;
    if (a < 0x08000000u || e > 0x10000000u) return 0;
    if (a < LC_SELF_HI && e > LC_SELF_LO) return 0;          /* the loader */
    return 1;
}

static int in_band(u32 a, u32 n)
{
    u32 i;
    for (i = 0; i < sizeof BANDS / sizeof BANDS[0]; i++)
        if (a >= BANDS[i][0] && a + n <= BANDS[i][1]) return 1;
    return 0;
}

static void sync_range(u32 a, u32 n)
{
    u32 p;
    for (p = a & ~31u; p < a + n; p += 32) {
        __asm__ volatile("ocbwb @%0" : : "r"(p) : "memory");
        /* icbi @Rn (SH-4A), as a word: assemblers built for -m4 reject it */
        __asm__ volatile("mov %0,r1\n\t.word 0x01E3" : : "r"(p) : "r1", "memory");
    }
}

static void put_small(u32 a, const u8 *d, u32 n)
{
    u32 i = 0;
    volatile u8 *p = (volatile u8 *)a;
    while (i < n && ((a + i) & 3)) { p[i] = d[i]; i++; }
    for (; i + 4 <= n; i += 4)
        *(volatile u32 *)(a + i) = d[i] | (u32)d[i + 1] << 8 |
            (u32)d[i + 2] << 16 | (u32)d[i + 3] << 24;
    for (; i < n; i++) p[i] = d[i];
}

static u32 parse(int *hdr)
{
    u8 h[16];
    u32 i, n;
    *hdr = 0;
    lc_why = W_OK;
    lc_bad = 0;
    lc_applied = 0;
    if (!getn(h, 16)) return 0;
    for (i = 0; i < 8; i++)
        if (h[i] != MAGIC[i]) return 0;
    if ((h[8] | h[9] << 8) != 2) return 0;            /* rev 2 only */
    n = h[10] | h[11] << 8;
    if (n == 0 || n > MAXE) { lc_why = W2_COUNT; return 0; }
    if (!getn(tbl, n * ESZ)) return 0;
    if (le32(h + 12) != crc32z(tbl, n * ESZ)) return 0;
    lc_seen++;
    /* phase 1: every precondition, nothing written */
    for (i = 0; i < n; i++) {
        const u8 *e = tbl + i * ESZ;
        u32 a = le32(e), len = le32(e + 4), old = le32(e + 12);
        u32 fl = e[16] | e[17] << 8, zl = le32(e + 20);
        if (fl & F_CALL) {
            if (len || zl || !in_window(a, 4) || !in_window(old, 4)) {
                lc_why = W2_WINDOW; lc_bad = i + 1; return 0;
            }
            continue;
        }
        if (!in_window(a, len) || len > CAP20 || zl == 0 || zl > CAP20) {
            lc_why = W2_WINDOW; lc_bad = i + 1; return 0;
        }
        if ((fl & F_ZERO) && (!in_band(a, len) || !home_zero(a, len))) {
            lc_why = W2_ZERO; lc_bad = i + 1; return 0;
        }
        if ((fl & F_OLD) && crc32z((const volatile u8 *)a, len) != old) {
            lc_why = W2_OLD; lc_bad = i + 1; return 0;
        }
    }
    *hdr = 1;
    /* phase 2: apply in order, stop at the first bad body */
    for (i = 0; i < n; i++) {
        const u8 *e = tbl + i * ESZ;
        u32 a = le32(e), len = le32(e + 4), crc = le32(e + 8), zl = le32(e + 20);
        int got;
        if (le32(e + 16) & F_CALL) {
            u32 k, w;
            for (k = 0; k < 600u; k++) {
                ((void (*)(void))a)();
                if (*(volatile u32 *)le32(e + 12) != crc) break;
                for (w = 0; w < 20000u; w++)       /* ~0.1 ms, then again */
                    __asm__ volatile("nop");
            }
            lc_applied++;
            continue;
        }
        while (pos & 0xFu)                         /* FILE-aligned body */
            if (getb() < 0) { lc_why = W_SHORT; lc_bad = i + 1; return lc_applied; }
        if (len <= SMALL) {
            got = lzss(zl, small, len);
            if (got != (int)len || crc32z(small, len) != crc) {
                lc_why = got < 0 ? W_SHORT : W_CRC; lc_bad = i + 1; return lc_applied;
            }
            put_small(a, small, len);
        } else {
            got = lzss(zl, (volatile u8 *)a, len);
            if (got != (int)len || crc32z((const volatile u8 *)a, len) != crc) {
                sync_range(a, len);
                lc_why = got < 0 ? W_SHORT : W_CRC; lc_bad = i + 1; return lc_applied;
            }
        }
        sync_range(a, len);
        lc_applied++;
    }
    return lc_applied;
}

#ifndef STICK
/* ---- memory source (core proof) ---- */
static const u8 *mpack;
static u32 mlen;
static int mem_refill(void)
{
    if (!mpack) return 0;
    cb = mpack; clen = mlen; mpack = 0;
    return (int)clen;
}

u32 lc_entry(u32 pack, u32 plen)
    __attribute__((section(".entry"), used));
u32 lc_entry(u32 pack, u32 plen)
{
    int hdr;
    mpack = (const u8 *)pack; mlen = plen;
    cb = 0; cpos = clen = pos = 0; dead = 0;
    refill = mem_refill;
    lc_mask = parse(&hdr);
    return lc_mask;
}

#else
/* ---- stick source: the deck's own FAT through its own file API ---- */
typedef void *(*open_t)(const u16 *path, const void *mode, void *ctx);
typedef int (*close_t)(void *h, void *ctx);
typedef int (*fread_t)(void *buf, int size, int count, void *fp);

#define STOCK_OPEN  ((open_t)0x08DDF53Cu)
#define STOCK_CLOSE ((close_t)0x08DDF992u)
#define STOCK_FREAD ((fread_t)0x08C61A6Cu)
#define DEVTAB 0x0C8D8028u          /* media device table (media read) */
#define CHUNK 2048u

enum { ST_IDLE, ST_NOFILE, ST_BADDEV, ST_HEADER, ST_DONE };

struct ld_state {
    u32 magic;        /* 'LDST' */
    u32 calls;        /* ld_open calls */
    u32 status;       /* last attempt: ST_* */
    u32 tries;        /* pack opens attempted */
    u32 mask;         /* entries applied (rev 2) */
    u32 bad;          /* payloads rejected */
    u32 why;          /* last rejection reason (W_*) */
    u32 bytes;        /* pack bytes consumed */
    u32 done;         /* 1 = a valid pack header was seen this boot */
} ld_state = { 0x5453444C };

static u8 chunk[CHUNK] __attribute__((aligned(32)));
static void *fh;

static const u16 PACKPATH[] = {'C', ':', '/', 'X', 'D', 'J', 'M', 'O', 'D',
    'S', '/', 'P', 'A', 'C', 'K', '.', 'B', 'I', 'N', 0};

static void cache_range(u32 a, u32 n, int purge)
{
    u32 p;
    for (p = a & ~31u; p < a + n; p += 32) {
        if (purge) __asm__ volatile("ocbp @%0" : : "r"(p) : "memory");
        else __asm__ volatile("ocbi @%0" : : "r"(p) : "memory");
    }
}

static int stick_refill(void)
{
    int got;
    cache_range((u32)chunk, CHUNK, 1);
    got = STOCK_FREAD(chunk, 1, (int)CHUNK, fh);
    cache_range((u32)chunk, CHUNK, 0);
    if (got <= 0 || got > (int)CHUNK) return 0;
    cb = chunk; clen = (u32)got;
    return got;
}

/* the handle must be a USB (device type 3) file: same lookup as the
 * firmware's media read 0x08DDFC96 before it tail-jumps to fread */
static int usb_handle(void *h)
{
    u32 a = (u32)h, f, k;
    if (!a || (a & 3)) return 0;
    f = *(volatile u32 *)a;
    if (!f || (f & 3)) return 0;
    k = (*(volatile u8 *)(f + 17) | 0x20) & 0xFF;
    return *(volatile u16 *)(DEVTAB + k * 12) == 3;
}

static void load(const u16 *path, const void *mode, void *ctx)
{
    u16 p[sizeof PACKPATH / 2];
    u32 i;
    int hdr;
    for (i = 0; i < sizeof PACKPATH / 2; i++) p[i] = PACKPATH[i];
    if (path && path[0]) p[0] = path[0];          /* the caller's drive */
    ld_state.tries++;
    fh = STOCK_OPEN(p, mode, ctx);
    if (!fh) { ld_state.status = ST_NOFILE; return; }
    if (!usb_handle(fh)) {
        ld_state.status = ST_BADDEV; STOCK_CLOSE(fh, ctx); return;
    }
    cb = 0; cpos = clen = pos = 0; dead = 0;
    refill = stick_refill;
    ld_state.mask = parse(&hdr);
    ld_state.bytes = pos;
    ld_state.bad = lc_bad;
    ld_state.why = lc_why;
    STOCK_CLOSE(fh, ctx);
    fh = 0;
    if (hdr) {
        ld_state.done = 1;            /* homes may be written: one-shot */
        ld_state.status = ST_DONE;
    } else {
        ld_state.status = ST_HEADER;  /* nothing written: retry next mount */
    }
}

void *ld_open(const u16 *path, const void *mode, void *ctx)
{
    ld_state.calls++;
    if (!ld_state.done) load(path, mode, ctx);
    return STOCK_OPEN(path, mode, ctx);
}
#endif
