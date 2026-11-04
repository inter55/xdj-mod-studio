/* ul.c -- USB-stick mods loader (prototype, emulator only so far).
 *
 * Sits on the open pool word 0x08DE39A4 of the deck's MYSETTING.DAT mount
 * reader (FUN 0x08DE34F2, runs in the USB task each time a stick is
 * mounted; its only two callers are 0x08DE38BA and 0x08DE3904). Every
 * call first tries, once per mount burst, to load <drive>:/PIONEER/MODS/
 * MODS.BIN, then chains to the stock open with the caller's own args.
 *
 * MODS.BIN = 32-B header {u32 "XMOD", u32 version 1, u32 len, u32
 * entry_off, u32 sum (32-bit sum of payload bytes), u32 rsv[3]} + len
 * bytes of position-independent SH code. The payload is read into UL_PAY,
 * checked, and called through the uncached P2 alias (no I-cache state can
 * be stale) with r4 = &ul_state (which carries the caller's drive, open
 * mode and ctx, so a stage-2 payload can open more files). The operand cache is copy-back on the
 * deck (CCR 0x105), so the buffer is PURGED (ocbp = write back dirty lines
 * + invalidate) before the read and again after it: before, so no old line
 * can be written back over DMA data later; after, so data the stack copied
 * in with the CPU reaches RAM before the P2 (uncached) checksum and call.
 * (2026-11-04 fix: the after-read pass was ocbi = invalidate WITHOUT write
 * back, which threw away a CPU-copied payload.) Any problem = close +
 * error code, never a call.
 */
typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;

#ifndef UL_PAY
#error constants come from build.sh
#endif

typedef void *(*open_t)(const u16 *path, const void *mode, void *ctx);
typedef int (*close_t)(void *h, void *ctx);
typedef int (*fread_t)(void *buf, int size, int count, void *fp);
typedef void (*entry_t)(void *info);

#define STOCK_OPEN  ((open_t)0x08DDF53Cu)
#define STOCK_CLOSE ((close_t)0x08DDF992u)
#define STOCK_FREAD ((fread_t)0x08C61A6Cu)
#define DEVTAB 0x0C8D8028u          /* media device table (media read) */
#define PAY_MAX (UL_PAY_END - UL_PAY)

enum { ST_IDLE, ST_NOFILE, ST_BADDEV, ST_SHORT, ST_MAGIC, ST_SIZE,
       ST_SUM, ST_CALLED };

struct ul_state {
    u32 magic;        /* 'ULST' */
    u32 calls;        /* ul_open calls */
    u32 mark;         /* payloads may write here (test marker) */
    u32 status;       /* last attempt: ST_* */
    u32 tries, loads; /* attempts / successful payload calls */
    u32 len, sum;     /* last payload */
    u32 done;         /* 1 = a payload ran this boot (one-shot) */
    u32 drive;        /* the mounting caller's drive letter (UTF-16) */
    u32 mode, ctx;    /* its open mode + context: a payload opens more
                         files with STOCK_OPEN(path, mode, ctx) */
} ul_state = { 0x54534C55 };

static const u16 MODPATH[] = {'C', ':', '/', 'P', 'I', 'O', 'N', 'E', 'E', 'R',
    '/', 'M', 'O', 'D', 'S', '/', 'M', 'O', 'D', 'S', '.', 'B', 'I', 'N', 0};

/* ocbp every 32-B line of [a, a+n): dirty lines go to RAM, all leave the
 * cache. Never ocbi here: it drops dirty data. */
static void cache_purge(u32 a, u32 n)
{
    for (u32 p = a & ~31u; p < a + n; p += 32)
        __asm__ volatile("ocbp @%0" : : "r"(p) : "memory");
}

/* the handle must be a USB (device type 3) file: same lookup as the
 * firmware's media read 0x08DDFC96 before it tail-jumps to fread */
static int usb_handle(void *h)
{
    u32 a = (u32)h;
    if (!a || (a & 3)) return 0;
    u32 f = *(volatile u32 *)a;
    if (!f || (f & 3)) return 0;
    u32 k = (*(volatile u8 *)(f + 17) | 0x20) & 0xFF;
    return *(volatile u16 *)(DEVTAB + k * 12) == 3;
}

static void load(const u16 *path, const void *mode, void *ctx)
{
    u16 p[sizeof MODPATH / 2];
    for (u32 i = 0; i < sizeof MODPATH / 2; i++) p[i] = MODPATH[i];
    if (path && path[0]) p[0] = path[0];          /* the caller's drive */
    ul_state.tries++;
    ul_state.drive = p[0];
    ul_state.mode = (u32)mode;
    ul_state.ctx = (u32)ctx;
    void *h = STOCK_OPEN(p, mode, ctx);
    if (!h) { ul_state.status = ST_NOFILE; return; }
    if (!usb_handle(h)) { ul_state.status = ST_BADDEV; STOCK_CLOSE(h, ctx); return; }
    u32 hd[8];
    if (STOCK_FREAD(hd, 1, 32, h) != 32) { ul_state.status = ST_SHORT; goto out; }
    if (hd[0] != 0x444F4D58u || hd[1] != 1) { ul_state.status = ST_MAGIC; goto out; }
    u32 len = hd[2], eo = hd[3];
    if (!len || len > PAY_MAX || eo >= len || (eo & 1)) { ul_state.status = ST_SIZE; goto out; }
    cache_purge(UL_PAY, len);
    int got = STOCK_FREAD((void *)UL_PAY, 1, (int)len, h);
    cache_purge(UL_PAY, len);
    if (got != (int)len) { ul_state.status = ST_SHORT; goto out; }
    const volatile u8 *b = (const volatile u8 *)(UL_PAY | 0xA0000000u);
    u32 s = 0;
    for (u32 i = 0; i < len; i++) s += b[i];
    ul_state.len = len;
    ul_state.sum = s;
    if (s != hd[4]) { ul_state.status = ST_SUM; goto out; }
    STOCK_CLOSE(h, ctx);
    ul_state.done = 1;
    ul_state.status = ST_CALLED;
    ul_state.loads++;
    ((entry_t)((UL_PAY + eo) | 0xA0000000u))(&ul_state);
    return;
out:
    STOCK_CLOSE(h, ctx);
}

void *ul_open(const u16 *path, const void *mode, void *ctx)
{
    ul_state.calls++;
    if (!ul_state.done) load(path, mode, ctx);
    return STOCK_OPEN(path, mode, ctx);
}
