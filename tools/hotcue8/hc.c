/* hc.c -- 8 hot cues on the XDJ-700 PERFORM screen (deck C, RAM only).
 *
 * The deck parses ALL of a track's hot cues (A-H) into the ANLZ ctx
 * (ctx [0x09826BE4] + 0xFC0: PCOB header, count u16 @+0x12, then
 * {u32 n, u32 ptr} groups @+0x20; entries 64 B: number u16 @+0x0E,
 * time ms u32 @+0x20, loop ms u32 @+0x24) but loads only A-C into the
 * player's three live slots (0x0B6C4118 + k*0x40: +0x08 u16 state (6 =
 * set, 0xFFFFFFFF word = empty), +0x0A number, +0x0C ms, +0x10 loop,
 * +0x14 time in 1/150 s). Re-tapping the HOT CUE tab pages the live
 * slots A-C -> D-F -> G-H (hc_next); the flush hook letters the pads of
 * pages 2/3 and resets to page 1 on a new track. All three pages always
 * exist: a track exported with no cues still gets empty D-H pads. HCPAGE (a byte in the
 * PERFORM pads state) tells the pads mod to block REC off page 1, so a
 * recording can never overwrite A-C on the stick.
 *
 * SETTING a cue on the deck (2026-11-01): pressing an EMPTY pad on page 2/3
 * stores D-H at the current playhead -- in RAM only, the stick is never
 * written (measured: no disk writes on an empty-pad press). The stock path
 * cannot do this: it edge-detects the panel key bit and jumps to the
 * hot-cue handler, which does nothing for an empty slot. So hc_hook watches
 * the same key bits (KEYB+86 bit 0x40 / 0x80, +87 bit 0x01) on every flush
 * and, on a rising edge over an empty page 2/3 slot, writes the cue itself:
 * state 6, number, playhead (POS150, 1/150 s -> ms), loop 0. Those values
 * live in hc_ram_ms, so paging away and back keeps the cue, and a new track
 * (preview hash change) clears them. Page 1 stays completely stock, so REC
 * still records A-C onto the stick. A press on a SET slot is untouched:
 * the stock recall runs as before.
 */
typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;

#define R32(a) (*(volatile u32 *)(a))
#define R16(a) (*(volatile u16 *)(a))
#define R8(a)  (*(volatile u8 *)(a))
#define UNCACHED(p) (((u32)(p) & 0x1FFFFFFFu) | 0xA0000000u)

#ifndef HCPAGE
#error HCPAGE
#endif
#ifndef PMODE
#error PMODE
#endif
#ifndef MSOFF
#error MSOFF
#endif
#define MODEW    0x0BA371D4u     /* 7 = PERFORM screen */
#define HCPAGES  3u             /* A-C, D-F, G-H always: a track whose
                                 * export carries fewer than 4 cues still
                                 * gets empty D-H pads, so cues can be set
                                 * on the deck (2026-11-01) */
#define CTX_TAB  0x09826BE4u
#define PCOB_OFF 0xFC0u
#define TAG_PCOB 0x424F4350u     /* "PCOB" as stored */
#define SLOTS    0x0B6C4118u
#define PREVIEW  0x0BA3BCCCu
#define KEYB     0x0B6C7A28u     /* panel key bitmap (zone handlers set it) */
#define POS150   0x0C0E6D18u     /* live position, 1/150 s (deck 2026-10-25) */

#include "hc_glyphs.h"

static const short PAD_X[3] = {116, 231, 345};
#define PAD_W 99
#define PAD_Y 232
#define PAD_H 63

/* state: every symbol comes BEFORE hc_count, because the installers zero
 * base..hc_count+4 and nothing past it */
u32 hc_sets;                     /* cues set on the deck this track (diag) */
u32 hc_draws;
static u32 hc_hash;
static u8 hc_ram_set[8];         /* cue set on the deck (no export entry) */
static u32 hc_ram_ms[8];
static u8 hc_keyprev;            /* pad key bits seen on the last flush */
u32 hc_count;                    /* cues in the loaded track (test/diag) */

static u32 hash_preview(void)
{
    u32 h = 2166136261u;
    const u32 *p = (const u32 *)PREVIEW;
    for (int i = 0; i < 100; i++) h = (h ^ p[i]) * 16777619u;
    return h;
}

/* entry for hot cue n (1..8) or 0. Numbers are 1..8; letter codes
 * 'A'..'H' are accepted too (seen in newer exports). The group walk is
 * bounded and every pointer is checked to be aligned RAM -- the ctx list
 * is shared with the Pro DJ Link server (a linked deck loading a track
 * from this deck's stick). */
static u32 find_cue(u32 n)
{
    u32 ctx = R32(CTX_TAB);
    if ((ctx & 3) || ctx < 0x08000000u || ctx >= 0x0FFF0000u)
        return 0;
    if (R32(ctx + PCOB_OFF) != TAG_PCOB || R16(ctx + PCOB_OFF + 0x0E) != 1)
        return 0;
    u32 total = R16(ctx + PCOB_OFF + 0x12), seen = 0;
    if (total > 64) total = 64;
    for (u32 g = ctx + PCOB_OFF + 0x20; seen < total && g < ctx + PCOB_OFF + 0x60; g += 8) {
        u32 cnt = R32(g), e = R32(g + 4);
        if (!cnt || cnt > 64 || (e & 1) || e < 0x08000000u || e + cnt * 0x40 > 0x10000000u) break;
        for (u32 k = 0; k < cnt; k++, e += 0x40) {
            u32 num = R16(e + 0x0E);
            if (num >= 'A' && num <= 'H') num -= 'A' - 1;   /* letter code */
            if (num == n) return e;
        }
        seen += cnt;
    }
    return 0;
}

static u32 cue_count(void)
{
    u32 n = 0;
    for (u32 i = 1; i <= 8; i++) if (find_cue(i) || hc_ram_set[i - 1]) n = i;
    return n;
}

static int have_cue(u32 n)                 /* exported, or set on the deck */
{
    return n >= 1 && n <= 8 && (find_cue(n) || hc_ram_set[n - 1]);
}

static u32 cue_ms(u32 n)
{
    if (n < 1 || n > 8) return 0;              /* page 3 slot 3 has no cue */
    u32 e = find_cue(n);
    if (e) return R32(e + 0x20);
    return hc_ram_set[n - 1] ? hc_ram_ms[n - 1] : 0;
}

static void load_page(u32 page)
{
    for (u32 k = 0; k < 3; k++) {
        u32 s = SLOTS + k * 0x40, n = 3 * page + k + 1, e = find_cue(n);
        if (have_cue(n)) {
            u32 ms = cue_ms(n);
            R16(s + 0x08) = 6;
            R16(s + 0x0A) = (u16)n;
            R32(s + 0x0C) = ms;
            R32(s + 0x10) = e ? R32(e + 0x24) : 0;
            R32(s + 0x14) = ms * 3u / 20u;          /* ms -> 1/150 s */
        } else {
            R32(s + 0x08) = 0xFFFFFFFFu;
            R32(s + 0x0C) = 0;
            R32(s + 0x10) = 0;
            R32(s + 0x14) = 0;
        }
    }
    R8(HCPAGE) = (u8)page;
}

/* Rising edge on the three hot-cue pad key bits: on page 2/3 an EMPTY slot
 * gets the cue that was pressed, at the current playhead. RAM only. The
 * stock handler runs unchanged (and finds nothing to recall on an empty
 * slot, exactly as before); a press on a set slot is not touched here. */
static void scan_pads(void)
{
    u8 k86 = R8(KEYB + 86), k87 = R8(KEYB + 87);
    u8 now = (u8)(((k86 & 0x40) ? 1u : 0u) | ((k86 & 0x80) ? 2u : 0u) |
                  ((k87 & 0x01) ? 4u : 0u));
    u8 rise = (u8)(now & ~hc_keyprev);
    hc_keyprev = now;
    u32 page = R8(HCPAGE);
    if (!rise || !page || page >= HCPAGES) return;   /* page 1 is stock */
    if (R8(MSOFF) || R32(MODEW) != 7 || R8(PMODE) != 0) return;
    u32 pos = R32(POS150);
    if (pos == 0xFFFFFFFFu) return;                  /* no playhead yet */
    for (u32 k = 0; k < 3; k++) {
        if (!(rise & (1u << k))) continue;
        u32 s = SLOTS + k * 0x40, n = 3 * page + k + 1;
        if (n > 8) continue;
        if (R16(s + 0x08) == 6) continue;            /* set: stock recall */
        u32 ms = pos * 20u / 3u;                     /* 1/150 s -> ms */
        hc_ram_set[n - 1] = 1;
        hc_ram_ms[n - 1] = ms;
        R16(s + 0x08) = 6;
        R16(s + 0x0A) = (u16)n;
        R32(s + 0x0C) = ms;
        R32(s + 0x10) = 0;
        R32(s + 0x14) = pos;
        hc_sets++;
    }
}

/* HOT CUE tab tapped again while in HOT CUE mode */
void hc_next(void)
{
    if (R8(MSOFF)) return;
    hc_count = cue_count();
    u32 p = R8(HCPAGE) + 1;
    if (p >= HCPAGES) p = 0;
    if (p == R8(HCPAGE)) return;              /* cannot happen: 3 pages */
    load_page(p);
}

/* swipe right on the HOT CUE tab: previous page (wraps) */
void hc_prev_page(void)
{
    if (R8(MSOFF)) return;
    hc_count = cue_count();
    u32 p = R8(HCPAGE);
    load_page(p ? p - 1 : HCPAGES - 1);
}

static u16 mix(u16 bg, u16 fg, int a)          /* a 0..32 */
{
    int r = ((bg >> 11) * (32 - a) + (fg >> 11) * a) >> 5;
    int g = (((bg >> 5) & 63) * (32 - a) + ((fg >> 5) & 63) * a) >> 5;
    int b = ((bg & 31) * (32 - a) + (fg & 31) * a) >> 5;
    return (u16)(r << 11 | g << 5 | b);
}

static void letter(u16 *lay, int stride, int k, int idx, int have)
{
    int px = PAD_X[k], cx = px + PAD_W / 2, cy = PAD_Y + PAD_H / 2;
    int x0 = cx - 13, y0 = cy - 11;
    for (int y = y0; y < y0 + 22; y++) {           /* cover the stock letter */
        u16 bg = lay[y * stride + px + 8];
        for (int x = x0; x < x0 + 26; x++) lay[y * stride + x] = bg;
    }
    if (idx < 0 || idx > 7) return;
    u16 fg = have ? 0xFFFF : 0x6B4D;              /* white / dim grey */
    int w = gl_w[idx], gx = cx - w / 2, gy = cy - GL_H / 2;
    for (int y = 0; y < GL_H; y++) {
        u16 *row = lay + (gy + y) * stride;
        for (int x = 0; x < w; x++) {
            int a = gl_a[idx][y * GL_W + x];
            if (a) row[gx + x] = mix(row[gx + x], fg, a);
        }
    }
}

/* REC is blocked off page 1 (pads mod): fade its pad to show that */
static void dim_rec(u16 *lay, int stride)
{
    for (int y = PAD_Y + 6; y < PAD_Y + PAD_H - 4; y++) {
        u16 *row = lay + y * stride;
        u16 bg = row[460 + 4];
        for (int x = 460 + 4; x < 460 + 78 - 4; x++) row[x] = mix(row[x], bg, 22);
    }
}

/* r4 = dev, r5 = req (FUN_08E7D278 args) */
void hc_hook(u32 dev, u32 req)
{
    (void)dev;
    u32 h = hash_preview();
    if (h != hc_hash) {                 /* new track: firmware loaded A-C */
        hc_hash = h;
        R8(HCPAGE) = 0;
        for (u32 i = 0; i < 8; i++) hc_ram_set[i] = 0;   /* drop deck cues */
        hc_count = cue_count();
    }
    scan_pads();
    u32 page = R8(HCPAGE);
    if (!page || R8(MSOFF) || R32(MODEW) != 7 || R8(PMODE) != 0) return;
    const u32 *w = (const u32 *)R32(req);
    if (!w || w[13] != 0x4C || w[16] != 0x58) return;
    if ((w[27] >> 24) != 0x82 || w[29] != 0x032001E0u) return;
    u16 *lay = (u16 *)UNCACHED(w[14]);
    int stride = (int)w[17];
    if (stride < 800 || stride > 1024) return;
    for (int k = 0; k < 3; k++) {
        int n = 3 * (int)page + k;                 /* 0-based letter */
        letter(lay, stride, k, n < 8 ? n : -1, have_cue(n + 1));
    }
    dim_rec(lay, stride);
    hc_draws++;
}
