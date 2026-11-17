/* hg.c -- PROTOTYPE: all 8 hot cues (A-H) as pads at once on the XDJ-700
 * PERFORM screen (deck C, RAM only, emulator-proven only).
 *
 * Design: docs/HOTCUE8_GRID.md. In short:
 *  - the player engine has 3 live hot-cue slots (0x0B6C4118 + k*0x40);
 *    the stock A/B/C pads are touch zones 23/24/25 whose handlers only set
 *    a panel KEY bit (KEY+86 bit 0x40 / 0x80, KEY+87 bit 0x01) -- the very
 *    same path the hardware-proven beat-jump pads use (key bit held while
 *    the finger is down);
 *  - 8 pads replace the A B C REC CALL row; a press on pad n (cue n set)
 *    uses slot k = (n-1) % 3: for n <= 3 that is the cue's own slot (no
 *    write at all); for n >= 4 the slot's 64 bytes are saved, cue n is
 *    written in (same field layout as tools/hotcue8 load_page) and the
 *    slot-k key bit is held while the finger is down -> the stock hot-cue
 *    consumer jumps to cue n. HG_HOLDOFF flushes after release the saved
 *    bytes go back (slot k = its own cue again);
 *  - empty pads and the gaps between pads are swallowed (an empty stock pad
 *    press could RECORD a cue into the borrowed slot);
 *  - REC / CALL are not on the 8-pad row; they stay on the stock row
 *    (8-pad view off). REC mode is therefore never active while we borrow.
 *
 * Entry table at HG_BASE: +0 flush hook, +4 touch hook, +8 hg_set_view(v).
 * Both hooks chain to whatever the pool held before (hg_bprev/hg_tprev).
 */
typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;

#define R32(a) (*(volatile u32 *)(a))
#define R16(a) (*(volatile u16 *)(a))
#define R8(a)  (*(volatile u8 *)(a))
#define UNCACHED(p) (((u32)(p) & 0x1FFFFFFFu) | 0xA0000000u)

#ifndef PST
#error PST
#endif
#ifndef HCPAGE
#error HCPAGE
#endif
#ifndef MSOFF
#error MSOFF
#endif
#define PMODE    (PST + 2u)
#define MODEW    0x0BA371D4u     /* 7 = PERFORM screen */
#define ZSTATE   0x0C0E75BCu     /* 0x1E = stock hit-tester idle */
#define CTX_TAB  0x09826BE4u
#define PCOB_OFF 0xFC0u
#define TAG_PCOB 0x424F4350u     /* "PCOB" as stored */
#define SLOTS    0x0B6C4118u
/* the deck keeps each pad's cue TWICE MORE, in two 8-record models of its
 * own (0x38 per record: +0x00 u16 state, +0x02 u16 number, +0x04 ms,
 * +0x08 loop, +0x0C 1/150 s). Measured 2026-11-08: paging the live slot
 * alone left both models on A-C and the deck went on playing A-C, so the
 * prototype borrow must write them as well (tools/hotcue8/hc.c does). */
#define MODEL_A  0x0BA28800u
#define MODEL_B  0x0BA2A52Cu
#define MODEL_S  0x38u
#define PREVIEW  0x0BA3BCCCu
#define KEYB     0x0B6C7A28u     /* panel key bitmap */

#include "../hotcue8/hc_glyphs.h"

/* 8 pads in the stock hot-cue row (y 232..295, x 116..615) */
#define ROW_Y  232
#define ROW_H  63
#define PAD_X0 116
#define PAD_W  58
#define PAD_P  63                 /* pitch: 58 px pad + 5 px gap */
#define CLR_X0 114
#define CLR_X1 622
#define HG_HOLDOFF 8
#define HG_DEAD    0xFF           /* gap / empty pad: eat until release */              /* flushes between release and restore */

/* key bit of live slot k (from the zone 23/24/25 handlers 0x8DD0BA4..) */
static const u8 KEY_OFF[3] = {86, 86, 87};
static const u8 KEY_BIT[3] = {0x40, 0x80, 0x01};

u32 hg_view;                     /* 1 = 8-pad view (tab swipe, later) */
u32 hg_held;                     /* 1 + pad index while a pad is held,
                                    HG_DEAD for a swallowed press */
u32 hg_slot;                     /* live slot used by the held pad */
u32 hg_borrow;                   /* 1 = slot hg_slot holds saved bytes */
u32 hg_restore;                  /* flush countdown to the restore */
u32 hg_presses, hg_swallowed, hg_restores, hg_draws;
u32 hg_saved[16];                /* the borrowed slot's 64 bytes */
static u32 hg_savedm[2][14];     /* ... and its two pad-model records */
static u32 hg_hash;

static u32 hash_preview(void)
{
    u32 h = 2166136261u;
    const u32 *p = (const u32 *)PREVIEW;
    for (int i = 0; i < 100; i++) h = (h ^ p[i]) * 16777619u;
    return h;
}

/* entry for hot cue n (1..8) or 0 -- same walk as tools/hotcue8/hc.c */
static u32 find_cue(u32 n)
{
    u32 ctx = R32(CTX_TAB);
    if (!ctx || R32(ctx + PCOB_OFF) != TAG_PCOB || R16(ctx + PCOB_OFF + 0x0E) != 1)
        return 0;
    u32 total = R16(ctx + PCOB_OFF + 0x12), seen = 0;
    if (total > 8) total = 8;
    for (u32 g = ctx + PCOB_OFF + 0x20; seen < total && g < ctx + PCOB_OFF + 0x40; g += 8) {
        u32 cnt = R32(g), e = R32(g + 4);
        if (!cnt || cnt > 8 || e < 0x08000000u || e >= 0x10000000u) break;
        for (u32 k = 0; k < cnt; k++, e += 0x40)
            if (R16(e + 0x0E) == n) return e;
        seen += cnt;
    }
    return 0;
}

static void key(u32 k, int on)
{
    u32 a = KEYB + KEY_OFF[k];
    if (on) R8(a) = (u8)(R8(a) | KEY_BIT[k]);
    else R8(a) = (u8)(R8(a) & ~KEY_BIT[k]);
}

/* one record of a pad model; only +0x00..+0x0D are used, the tail
 * +0x10..+0x37 is zero on every stock record (measured 2026-11-08) */
static void model_set(u32 k, u32 n, u32 ms, u32 loop)
{
    for (u32 m = 0; m < 2; m++) {
        u32 r = (m ? MODEL_B : MODEL_A) + k * MODEL_S;
        R16(r + 0x00) = 6;
        R16(r + 0x02) = (u16)n;
        R32(r + 0x04) = ms;
        R32(r + 0x08) = loop;
        R32(r + 0x0C) = ms * 3u / 20u;    /* ms -> 1/150 s */
    }
}

static void restore_slot(void)
{
    if (hg_borrow) {
        u32 s = SLOTS + hg_slot * 0x40;
        for (int i = 0; i < 16; i++) R32(s + 4 * i) = hg_saved[i];
        for (u32 j = 0; j < 14; j++) {
            R32(MODEL_A + hg_slot * MODEL_S + 4 * j) = hg_savedm[0][j];
            R32(MODEL_B + hg_slot * MODEL_S + 4 * j) = hg_savedm[1][j];
        }
        hg_borrow = 0;
        hg_restores++;
    }
    hg_restore = 0;
}

static int active(void)
{
    return hg_view && !R8(MSOFF) && R32(MODEW) == 7 && R8(PMODE) == 0 &&
           R8(HCPAGE) == 0;
}

/* track change: the firmware reloaded the slots itself -> forget saves */
static void track_check(void)
{
    u32 h = hash_preview();
    if (h != hg_hash) {
        hg_hash = h;
        hg_borrow = 0;
        hg_restore = 0;
    }
}

/* r4 down, r5 x, r6 y (stock hit-tester args). 0 = pass on, 2 = swallow */
u32 hg_touch(u32 down, u32 x, u32 y)
{
    if (hg_held == HG_DEAD) {              /* swallowed press: no slide-on */
        if (!down) hg_held = 0;
        return 2;
    }
    if (hg_held) {                         /* our press: finish it */
        u32 k = hg_slot;
        if (down) {
            key(k, 1);                     /* re-assert every frame */
        } else {
            key(k, 0);
            hg_held = 0;
            if (hg_borrow) hg_restore = HG_HOLDOFF;
        }
        return 2;
    }
    if (!down || !active() || R8(PST) != 0 || R32(ZSTATE) != 0x1E)
        return 0;
    if (y - ROW_Y >= ROW_H || x - PAD_X0 >= 8 * PAD_P - (PAD_P - PAD_W))
        return 0;                          /* not the hot-cue row */
    u32 i = (x - PAD_X0) / PAD_P;
    u32 n = i + 1, e = find_cue(n);
    if (x - PAD_X0 - i * PAD_P >= PAD_W || !e) {
        hg_swallowed++;                    /* gap, or empty pad (never a REC) */
        hg_held = HG_DEAD;
        return 2;
    }
    track_check();
    if (hg_restore) restore_slot();        /* quick re-press: put back first */
    u32 k = (n - 1) % 3, s = SLOTS + k * 0x40;
    if (n > 3) {
        for (int j = 0; j < 16; j++) hg_saved[j] = R32(s + 4 * j);
        for (u32 j = 0; j < 14; j++) {
            hg_savedm[0][j] = R32(MODEL_A + k * MODEL_S + 4 * j);
            hg_savedm[1][j] = R32(MODEL_B + k * MODEL_S + 4 * j);
        }
        hg_borrow = 1;
        u32 ms = R32(e + 0x20), loop = R32(e + 0x24);
        R16(s + 0x08) = 6;
        R16(s + 0x0A) = (u16)n;
        R32(s + 0x0C) = ms;
        R32(s + 0x10) = loop;
        R32(s + 0x14) = ms * 3u / 20u;     /* ms -> 1/150 s */
        model_set(k, n, ms, loop);         /* the pads read these */
    }
    hg_slot = k;
    hg_held = 1 + i;
    hg_presses++;
    key(k, 1);
    return 2;
}

void hg_set_view(u32 v)
{
    hg_view = v ? 1 : 0;
}

/* ------------------------------------------------------------ drawing -- */
static u16 mix(u16 bg, u16 fg, int a)          /* a 0..32 */
{
    int r = ((bg >> 11) * (32 - a) + (fg >> 11) * a) >> 5;
    int g = (((bg >> 5) & 63) * (32 - a) + ((fg >> 5) & 63) * a) >> 5;
    int b = ((bg & 31) * (32 - a) + (fg & 31) * a) >> 5;
    return (u16)(r << 11 | g << 5 | b);
}

static void fill(u16 *lay, int stride, int x0, int y0, int w, int h, u16 c)
{
    for (int y = y0; y < y0 + h; y++) {
        u16 *row = lay + y * stride;
        for (int x = x0; x < x0 + w; x++) row[x] = c;
    }
}

#define C_BG     0x0000
#define C_BODY   0x29C9           /* stock pad body (41,57,74) */
#define C_EDGE   0x3A6C           /* stock pad edge (58,77,99) */
#define C_TOP    0x1947           /* stock top strip */
#define C_SET    0x2E4A           /* cue-set strip (green) */
#define C_EMPTY  0x10E4
#define C_HOLD   0xFC00           /* pressed rim (orange, as our other pads) */

static void pad(u16 *lay, int stride, int i, int have, int held)
{
    int x = PAD_X0 + i * PAD_P, y = ROW_Y + 1, w = PAD_W, h = ROW_H - 2;
    u16 edge = held ? C_HOLD : C_EDGE;
    fill(lay, stride, x, y, w, h, edge);
    fill(lay, stride, x + 1, y + 1, w - 2, h - 2, have ? C_BODY : C_EMPTY);
    if (held) fill(lay, stride, x + 1, y + 1, w - 2, 1, C_HOLD);
    fill(lay, stride, x + 4, y + 4, w - 8, 4, have ? C_SET : C_TOP);
    u16 fg = have ? 0xFFFF : 0x6B4D;
    int gw = gl_w[i], gx = x + w / 2 - gw / 2, gy = y + h / 2 - GL_H / 2 + 2;
    for (int yy = 0; yy < GL_H; yy++) {
        u16 *row = lay + (gy + yy) * stride;
        for (int xx = 0; xx < gw; xx++) {
            int a = gl_a[i][yy * GL_W + xx];
            if (a) row[gx + xx] = mix(row[gx + xx], fg, a);
        }
    }
}

/* r4 = dev, r5 = req (FUN_08E7D278 args), every display flush */
void hg_hook(u32 dev, u32 req)
{
    (void)dev;
    track_check();
    if (hg_restore && !hg_held && --hg_restore == 0) restore_slot();
    if (!active()) return;
    const u32 *w = (const u32 *)R32(req);
    if (!w || w[13] != 0x4C || w[16] != 0x58) return;
    if ((w[27] >> 24) != 0x82 || w[29] != 0x032001E0u) return;
    u16 *lay = (u16 *)UNCACHED(w[14]);
    int stride = (int)w[17];
    if (stride < 800 || stride > 1024) return;
    fill(lay, stride, CLR_X0, ROW_Y, CLR_X1 - CLR_X0, ROW_H, C_BG);
    for (int i = 0; i < 8; i++)
        pad(lay, stride, i, find_cue(i + 1) != 0, hg_held == (u32)(i + 1));
    hg_draws++;
}
