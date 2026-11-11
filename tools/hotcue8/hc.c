/* hc.c -- 8 hot cues on the XDJ-700 PERFORM screen (deck C, RAM only).
 *
 * The deck parses a track's hot cues into the ANLZ ctx -- the legacy list
 * (ctx [0x09826BE4] + 0xFC0) AND the extended .EXT list (+0x136C, see
 * PCO2_OFF: on a real export the only place D-H are). Legacy list:
 * (+0xFC0: PCOB header, count u16 @+0x12, then
 * {u32 n, u32 ptr} groups @+0x20; entries 64 B: number u16 @+0x0E,
 * time ms u32 @+0x20, loop ms u32 @+0x24) but loads only A-C into the
 * player's three live slots (0x0B6C4118 + k*0x40: +0x08 u16 state (6 =
 * set, 0xFFFFFFFF word = empty), +0x0A number, +0x0C ms, +0x10 loop,
 * +0x14 time in 1/150 s). The word is little-endian, so cue A reads
 * 0x0001_0006: 6 at +0x08 (state), 1 at +0x0A (number).
 *
 * 2026-11-08: a paged slot (state 6, number 4, D's ms) stays untouched
 * for 30M emu steps (build/hc_page_probe.py), yet the deck still played
 * A-C -- so playback does not take its cue from these slots. See
 * docs/HOTCUE8_GRID.md section 6. Re-tapping the HOT CUE tab pages the live
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
/* The EXTENDED (nxs2) cue list from the .EXT file, parsed by the deck too
 * (2026-11-08, GitHub #6). rekordbox writes only A-C into the legacy PCOB
 * list (players before nxs2 had three hot cues) and A-H into PCO2, so on a
 * real export D-H exist ONLY here. Measured in the emulator with that split
 * (.DAT PCOB A-C, .EXT PCO2 A-H): the deck's ctx +0xFC0 list held 3 cues,
 * +0x136C held all 8. Header: tag, u16 type @+0x0E (1 = hot cues), u32
 * count @+0x10, {u32 n, u32 ptr} groups from +0x1C. Entries 0x38 B: tag
 * "PCP2", number u16 @+0x0E, ms u32 @+0x14, loop ms u32 @+0x18 (-1 = none).
 * Legacy PCPT entries (0x40 B) keep ms @+0x20, loop @+0x24. */
#define PCO2_OFF 0x136Cu
#define TAG_PCO2 0x324F4350u     /* "PCO2" */
#define TAG_PCP2 0x32504350u     /* "PCP2" */
#define SLOTS    0x0B6C4118u
/* The deck keeps each pad's cue TWICE more, in two 8-record models of its
 * own (0x38 bytes per record: +0x00 u16 state (6 = set), +0x02 u16
 * number, +0x04 ms, +0x08 loop (-1 = none), +0x0C time in 1/150 s).
 * Rewriting the three live slots alone does NOT move them: measured
 * 2026-11-08 (build/hc_page_probe.py), after a page the slots held D/E/F
 * while both models still held A/B/C for 30M steps -- and the deck went
 * on playing A/B/C. These models are what the pads read. */
#define MODEL_A  0x0BA28800u
#define MODEL_B  0x0BA2A52Cu
#define MODEL_S  0x38u
#define PREVIEW  0x0BA3BCCCu
#define KEYB     0x0B6C7A28u     /* panel key bitmap (zone handlers set it) */
#define POS150   0x0C0E6D18u     /* live position, 1/150 s (deck 2026-10-25) */

#include "hc_glyphs.h"

static const short PAD_X[3] = {116, 231, 345};
#define PAD_W 99
#define PAD_Y 232
#define PAD_H 63

/* the 8-pad row: same y band, 58 px pads 63 px apart */
#define R8_Y   232
#define R8_H   63
#define R8_X0  116
#define R8_W   58
#define R8_P   63
#define R8_CLR 0
#define HG_DEAD   0xFF           /* swallowed press: eat it until release */
#define HC_HOLDOFF 8             /* flushes between release and restore */

/* key bit of live slot k (zone handlers 0x8DD0BA4..: KEYB+86 bit 0x40 /
 * 0x80, KEYB+87 bit 0x01) */
static const u8 KEY_OFF[3] = {86, 86, 87};
static const u8 KEY_BIT[3] = {0x40, 0x80, 0x01};
#define PST      (PMODE - 2u)    /* PERFORM pads state (latch @PST) */
#define ZSTATE   0x0C0E75BCu     /* 0x1E = the stock hit-tester is idle */

/* MEMORY-DELETE, measured in the emulator 2026-11-07: zone 17 (0x11),
 * rect x 715 y 232 67x53, handler 0x08DD0B60, and it sets KEYB+84 bit 0x40
 * (tapping it changes only that bit; QUANTIZE next door still shows its
 * documented KEYB+91 0x80). Held with a D-H pad this would delete the cue
 * in the slot that pad borrowed -- which really holds A/B/C on the stick. */
#define DELKEY (KEYB + 84)
#define DELBIT 0x40

/* state: every symbol comes BEFORE hc_count, because the installers zero
 * base..hc_count+4 and nothing past it */
u32 hc_sets;                     /* cues set on the deck this track (diag) */
u32 hc_draws;
static u32 hc_hash;
static u8 hc_ram_set[8];         /* cue set on the deck (no export entry) */
static u32 hc_ram_ms[8];
static u8 hc_keyprev;            /* pad key bits seen on the last flush */

/* 8-pad view (2026-11-07, integrated from tools/hotcue8grid): tapping the
 * HOT CUE tab draws ALL eight cues as pads; swiping still pages. A press
 * borrows one of the three live slots exactly like a page does -- for A/B/C
 * the cue's own slot (no write at all), for D-H slot (n-1)%3 with the slot
 * saved and put back 8 flushes after the release. REC/CALL are not on this
 * row, so a recording can never overwrite A-C on the stick.
 * hc_armed is set by the installer: without the touch hook the pads must
 * never be drawn, because their presses would reach the stock zones. */
u32 hc_view;                     /* 1 = all 8 pads (tap on the tab) */
u32 hc_armed;                    /* 1 = the touch hook is installed */
u32 hc_presses, hc_swallowed, hc_restores, hc_draws8;
static u32 hc_held;              /* 1 + pad index, or HG_DEAD */
static u32 hc_bslot;             /* live slot the held pad borrows */
static u32 hc_borrow;            /* 1 = hc_bslot holds borrowed bytes */
static u32 hc_restore;           /* flushes left until the restore */
static u32 hc_saved[16];         /* the borrowed slot's 64 bytes */
static u32 hc_savedm[2][14];     /* ... and its two pad-model records */

/* THE LOADED TRACK'S CUES (2026-11-09, Reddit report: "C on track Y jumps to
 * track X's C", some tracks "dominate"). The ANLZ ctx is ONE buffer per
 * stick: BROWSE's track preview parses the track under the cursor into it
 * (the same cache reuse that made the centre RGB waveform show other
 * tracks' colours, CLAUDE.md 40). We used to re-read it on every page
 * change and on the 8-pad toggle, so after browsing, load_page(0) wrote the
 * PREVIEWED track's A-C into the live slots and pad models.
 *   A-C: the deck loads them itself and gets them right. While page 1 is
 *        showing we keep a copy of the deck's own slots + model records
 *        (hc_p1*), and going back to page 1 puts exactly those bytes back.
 *   D-H: taken from the ctx ONCE per track, and only after the ctx proves
 *        it is the loaded track: its A-C match the deck's own A-C (set /
 *        empty and ms). Until then D-H are unknown (empty pads, no paging
 *        of exported cues); after that the ctx is never read again. */
static u32 hc_cap;               /* 1 = hc_x* hold the loaded track's D-H */
static u8 hc_xhave[8];
static u32 hc_xms[8], hc_xloop[8];
static u32 hc_p1ok;              /* 1 = hc_p1* hold the deck's page-1 bytes */
static u32 hc_p1s[3][16];        /* live slots A-C */
static u32 hc_p1m[2][3][14];     /* their pad-model records */
u32 hc_caps;                     /* tracks captured (test/diag) */
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
/* one ctx cue list: hdr = list header, cnt_at/grp_at = where its count and
 * first {n, ptr} group sit, esz = entry stride */
static u32 find_in(u32 hdr, u32 tag, u32 total, u32 grp, u32 esz, u32 n)
{
    if (R32(hdr) != tag || R16(hdr + 0x0E) != 1)
        return 0;
    u32 seen = 0;
    if (total > 64) total = 64;
    for (u32 g = grp; seen < total && g < grp + 0x40; g += 8) {
        u32 cnt = R32(g), e = R32(g + 4);
        if (!cnt || cnt > 64 || (e & 3) || e < 0x08000000u || e + cnt * esz > 0x10000000u) break;
        for (u32 k = 0; k < cnt; k++, e += esz) {
            u32 num = R16(e + 0x0E);
            if (num >= 'A' && num <= 'H') num -= 'A' - 1;   /* letter code */
            if (num == n) return e;
        }
        seen += cnt;
    }
    return 0;
}

/* entry for hot cue n (1..8) or 0: the legacy list first (it is what the
 * deck itself loads A-C from), then the extended one. Every pointer is
 * checked to be aligned RAM -- the ctx lists are shared with the Pro DJ
 * Link server (a linked deck loading a track from this deck's stick).
 * Read an entry's fields only through cue_e_ms()/cue_e_loop(). */
static u32 find_cue(u32 n)
{
    u32 ctx = R32(CTX_TAB);
    if ((ctx & 3) || ctx < 0x08000000u || ctx >= 0x0FFF0000u)
        return 0;
    u32 a = ctx + PCOB_OFF, b = ctx + PCO2_OFF;
    u32 e = find_in(a, TAG_PCOB, R16(a + 0x12), a + 0x20, 0x40, n);
    if (!e)
        e = find_in(b, TAG_PCO2, R32(b + 0x10), b + 0x1C, 0x38, n);
    return e;
}

static u32 cue_e_ms(u32 e)
{
    return R32(e) == TAG_PCP2 ? R32(e + 0x14) : R32(e + 0x20);
}

static u32 cue_e_loop(u32 e)
{
    return R32(e) == TAG_PCP2 ? R32(e + 0x18) : R32(e + 0x24);
}

/* 2026-11-10 (report on 1.0.8: "A-C show and trigger, but every pad of the
 * A-H view is empty, A-C included"). A set cue was recognised by state == 6.
 * That is what the deck stores for the TEST cues this module was developed
 * against (written with status 4). Every hot cue in a real rekordbox export
 * has status 0, and the deck then stores state 4 (emulator, real firmware,
 * tools/hotcue8_emu.py HC_STATUS=0). So on real sticks no cue ever counted
 * as set: the A-H view drew eight empty pads and D-H were never taken from
 * the track. "Set" is now any state that is not empty with the right cue
 * number, and cues this module writes use the state the deck itself used
 * for this track's A-C (hc_st; 4 until one is seen). */
#define ST_SET(st) ((st) != 0 && (st) != 0xFFFFu)
static u16 hc_st = 4;

static int p1_have(u32 n)                   /* n = 1..3: the deck's own */
{
    u32 k = n - 1;
    if (hc_p1ok)
        return ST_SET(hc_p1s[k][2] & 0xFFFFu) && (hc_p1s[k][2] >> 16) == n;
    u32 s = SLOTS + k * 0x40;
    return ST_SET(R16(s + 0x08)) && R16(s + 0x0A) == n;
}

static int have_cue(u32 n)                 /* exported, or set on the deck */
{
    if (n < 1 || n > 8) return 0;
    if (n <= 3) return p1_have(n);
    return (hc_cap && hc_xhave[n - 1]) || hc_ram_set[n - 1];
}

static u32 cue_count(void)
{
    u32 n = 0;
    for (u32 i = 1; i <= 8; i++) if (have_cue(i)) n = i;
    return n;
}

static u32 cue_ms(u32 n)                   /* D-H only */
{
    if (n < 4 || n > 8) return 0;              /* page 3 slot 3 has no cue */
    if (hc_cap && hc_xhave[n - 1]) return hc_xms[n - 1];
    return hc_ram_set[n - 1] ? hc_ram_ms[n - 1] : 0;
}

static u32 cue_loop(u32 n)                 /* D-H only */
{
    return (n >= 4 && n <= 8 && hc_cap && hc_xhave[n - 1]) ? hc_xloop[n - 1] : 0;
}

static void p1_save(void)
{
    for (u32 k = 0; k < 3; k++) {
        for (u32 j = 0; j < 16; j++) hc_p1s[k][j] = R32(SLOTS + k * 0x40 + 4 * j);
        for (u32 j = 0; j < 14; j++) {
            hc_p1m[0][k][j] = R32(MODEL_A + k * MODEL_S + 4 * j);
            hc_p1m[1][k][j] = R32(MODEL_B + k * MODEL_S + 4 * j);
        }
    }
    hc_p1ok = 1;
}

static void p1_restore(void)
{
    if (!hc_p1ok) return;                  /* never left page 1 with data */
    for (u32 k = 0; k < 3; k++) {
        for (u32 j = 0; j < 16; j++) R32(SLOTS + k * 0x40 + 4 * j) = hc_p1s[k][j];
        for (u32 j = 0; j < 14; j++) {
            R32(MODEL_A + k * MODEL_S + 4 * j) = hc_p1m[0][k][j];
            R32(MODEL_B + k * MODEL_S + 4 * j) = hc_p1m[1][k][j];
        }
    }
}

/* page 1 showing, nothing borrowed: the live slots are the deck's own A-C.
 * Take D-H from the ctx when its A-C agree with them. */
static void capture(void)
{
    if (hc_cap) return;
    for (u32 k = 0; k < 3; k++) {
        u32 s = SLOTS + k * 0x40, n = k + 1, e = find_cue(n);
        int set = ST_SET(R16(s + 0x08)) && R16(s + 0x0A) == n;
        if (set) hc_st = R16(s + 0x08);              /* the deck's own value */
        if (set != (e != 0)) return;                 /* another track's list */
        if (set && R32(s + 0x0C) != cue_e_ms(e)) return;
    }
    for (u32 n = 1; n <= 8; n++) {
        u32 e = find_cue(n);
        hc_xhave[n - 1] = e != 0;
        hc_xms[n - 1] = e ? cue_e_ms(e) : 0;
        hc_xloop[n - 1] = e ? cue_e_loop(e) : 0;
    }
    hc_cap = 1;
    hc_caps++;
    hc_count = cue_count();
}

/* write one record of a pad model. Measured 2026-11-08
 * (build/hc_model_probe.py, dump of all 8 records of both models): a record
 * uses ONLY +0x00..+0x0D, the tail +0x10..+0x37 is zero on every stock
 * record, and an unused pad is state 0 / number 0 / ms 0 / pos 0 -- with
 * loop 0xFFFFFFFF in model A but loop 0 in model B, so the empty loop is a
 * per-model constant and not one value for both. */
#define EMPTY_LOOP_A 0xFFFFFFFFu
#define EMPTY_LOOP_B 0u
static void model_set(u32 base, u32 k, u32 n, u32 have, u32 ms, u32 loop,
                      u32 eloop)
{
    u32 r = base + k * MODEL_S;
    R16(r + 0x00) = have ? hc_st : 0;
    R16(r + 0x02) = have ? (u16)n : 0;
    R32(r + 0x04) = have ? ms : 0;
    R32(r + 0x08) = have ? loop : eloop;
    R32(r + 0x0C) = have ? ms * 3u / 20u : 0;
}

static void load_page(u32 page)
{
    if (page == 0) {                       /* the deck's own A-C, as it had them */
        p1_restore();
        R8(HCPAGE) = 0;
        return;
    }
    for (u32 k = 0; k < 3; k++) {
        u32 s = SLOTS + k * 0x40, n = 3 * page + k + 1;
        u32 have = (u32)have_cue(n), ms = cue_ms(n), loop = cue_loop(n);
        if (have) {
            R16(s + 0x08) = hc_st;
            R16(s + 0x0A) = (u16)n;
            R32(s + 0x0C) = ms;
            R32(s + 0x10) = loop;
            R32(s + 0x14) = ms * 3u / 20u;          /* ms -> 1/150 s */
        } else {
            R32(s + 0x08) = 0xFFFFFFFFu;
            R32(s + 0x0C) = 0;
            R32(s + 0x10) = 0;
            R32(s + 0x14) = 0;
        }
        model_set(MODEL_A, k, n, have, ms, loop, EMPTY_LOOP_A);
        model_set(MODEL_B, k, n, have, ms, loop, EMPTY_LOOP_B);
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
    /* P2: POS150 is written by the DSP side without invalidating the cache;
     * a cached read would store this cue at a stale playhead on the deck
     * (same hardware-only class as the phase meter's POSL read). */
    u32 pos = R32(UNCACHED(POS150));
    if (pos == 0xFFFFFFFFu) return;                  /* no playhead yet */
    for (u32 k = 0; k < 3; k++) {
        if (!(rise & (1u << k))) continue;
        u32 s = SLOTS + k * 0x40, n = 3 * page + k + 1;
        if (n > 8) continue;
        if (ST_SET(R16(s + 0x08))) continue;         /* set: stock recall */
        u32 ms = pos * 20u / 3u;                     /* 1/150 s -> ms */
        hc_ram_set[n - 1] = 1;
        hc_ram_ms[n - 1] = ms;
        R16(s + 0x08) = hc_st;
        R16(s + 0x0A) = (u16)n;
        R32(s + 0x0C) = ms;
        R32(s + 0x10) = 0;
        R32(s + 0x14) = pos;
        /* the pads read the two models, not this slot: without these the
         * freshly set cue would draw (hc_ram_set) but not recall until the
         * page was reloaded */
        model_set(MODEL_A, k, n, 1, ms, 0, EMPTY_LOOP_A);
        model_set(MODEL_B, k, n, 1, ms, 0, EMPTY_LOOP_B);
        hc_sets++;
    }
}

/* ------------------------------------------------------- 8-pad view ---- */
static void key(u32 k, int on)
{
    u32 a = KEYB + KEY_OFF[k];
    if (on) R8(a) = (u8)(R8(a) | KEY_BIT[k]);
    else R8(a) = (u8)(R8(a) & ~KEY_BIT[k]);
}

static void restore_slot(void)
{
    if (hc_borrow) {
        u32 s = SLOTS + hc_bslot * 0x40;
        for (int i = 0; i < 16; i++) R32(s + 4 * i) = hc_saved[i];
        for (u32 j = 0; j < 14; j++) {
            R32(MODEL_A + hc_bslot * MODEL_S + 4 * j) = hc_savedm[0][j];
            R32(MODEL_B + hc_bslot * MODEL_S + 4 * j) = hc_savedm[1][j];
        }
        hc_borrow = 0;
        hc_restores++;
    }
    hc_restore = 0;
}

static void drop_press(void)
{
    if (hc_held && hc_held != HG_DEAD) key(hc_bslot, 0);
    hc_held = 0;
}

static int active8(void)
{
    return hc_view && !R8(MSOFF) && R32(MODEW) == 7 && R8(PMODE) == 0 &&
           R8(HCPAGE) == 0;
}

/* r4 down, r5 x, r6 y (stock hit-tester args). 0 = pass on, 2 = swallow.
 * Only runs in the 8-pad view: any other view leaves the stock row whole. */
u32 hc_touch(u32 down, u32 x, u32 y)
{
    if (!hc_view) return 0;
    if (hc_held == HG_DEAD) {              /* swallowed press: no slide-on */
        if (!down) hc_held = 0;
        return 2;
    }
    if (hc_held) {                         /* our press: finish it */
        u32 k = hc_bslot;
        if (down) {
            key(k, 1);                     /* re-assert every frame */
        } else {
            key(k, 0);
            hc_held = 0;
            if (hc_borrow) hc_restore = HC_HOLDOFF;
        }
        return 2;
    }
    if (!down || !active8() || R8(PST) != 0 || R32(ZSTATE) != 0x1E)
        return 0;
    if (y - R8_Y >= R8_H || x < R8_X0 || x - R8_X0 >= 8 * R8_P)
        return 0;
    u32 i = (x - R8_X0) / R8_P;
    u32 n = i + 1;
    if (x - R8_X0 - i * R8_P >= R8_W || !have_cue(n)) {
        hc_swallowed++;                    /* gap, or empty pad (never REC) */
        hc_held = HG_DEAD;
        return 2;
    }
    if (n > 3 && (R8(DELKEY) & DELBIT)) {
        hc_swallowed++;                    /* DELETE held: a D-H pad would
                                            * delete the borrowed cue (A-C
                                            * are stock-correct, so they
                                            * still go through) */
        hc_held = HG_DEAD;
        return 2;
    }
    if (hc_restore) restore_slot();        /* quick re-press: put back first */
    u32 k = (n - 1) % 3, s = SLOTS + k * 0x40;
    if (n > 3) {
        for (int j = 0; j < 16; j++) hc_saved[j] = R32(s + 4 * j);
        for (u32 j = 0; j < 14; j++) {
            hc_savedm[0][j] = R32(MODEL_A + k * MODEL_S + 4 * j);
            hc_savedm[1][j] = R32(MODEL_B + k * MODEL_S + 4 * j);
        }
        hc_borrow = 1;
        u32 ms = cue_ms(n), loop = cue_loop(n);
        R16(s + 0x08) = hc_st;
        R16(s + 0x0A) = (u16)n;
        R32(s + 0x0C) = ms;
        R32(s + 0x10) = loop;
        R32(s + 0x14) = ms * 3u / 20u;     /* ms -> 1/150 s */
        model_set(MODEL_A, k, n, 1, ms, loop, EMPTY_LOOP_A);
        model_set(MODEL_B, k, n, 1, ms, loop, EMPTY_LOOP_B);
    }
    hc_bslot = k;
    hc_held = 1 + i;
    hc_presses++;
    key(k, 1);
    return 2;
}

void hc_next(void);              /* defined below; the fallback uses it */

/* HOT CUE tab tapped while in HOT CUE mode: 8 pads <-> the stock 3-pad row.
 * Both views sit on page 1 (A-C), so the switch is just a repaint. */
void hc_toggle(void)
{
    if (R8(MSOFF) || R32(MODEW) != 7 || R8(PMODE) != 0) return;
    if (!hc_armed) {              /* no touch hook (older install): the pads
                                   * would hand presses to the stock zones, so
                                   * a tap keeps its old meaning -- page on */
        hc_next();
        return;
    }
    if (hc_borrow) restore_slot();     /* a held press has no hc_restore yet:
                                        * checking hc_restore alone would
                                        * leave the borrowed bytes behind */
    drop_press();
    hc_view = hc_view ? 0 : 1;
    load_page(0);
    hc_count = cue_count();
}

/* HOT CUE tab tapped again while in HOT CUE mode */
void hc_next(void)
{
    if (R8(MSOFF)) return;
    if (hc_borrow) restore_slot();
    drop_press();
    hc_view = 0;                             /* paging uses the stock row */
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
    if (hc_borrow) restore_slot();
    drop_press();
    hc_view = 0;
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
    /* Repaint the whole pad face. A 26x22 box over the middle left the
     * stock letter and its loop badge half visible on the paged pads
     * ("loop icon only partially rendering", reported 2026-11-08). The
     * sample column is just inside the stock edge, where no art sits. */
    for (int y = PAD_Y + 2; y < PAD_Y + PAD_H - 2; y++) {
        u16 bg = lay[y * stride + px + 3];
        for (int x = px + 3; x < px + PAD_W - 3; x++) lay[y * stride + x] = bg;
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

static void fill(u16 *lay, int stride, int x0, int y0, int w, int h, u16 c)
{
    for (int y = y0; y < y0 + h; y++) {
        u16 *row = lay + y * stride;
        for (int x = x0; x < x0 + w; x++) row[x] = c;
    }
}

#define C_BODY   0x29C9           /* stock pad body (41,57,74) */
#define C_EDGE   0x3A6C           /* stock pad edge (58,77,99) */
#define C_TOP    0x1947           /* stock top strip */
#define C_SET    0x2E4A           /* cue-set strip (green) */
#define C_EMPTY  0x10E4
#define C_HOLD   0xFC00           /* pressed rim (orange) */

static void pad8(u16 *lay, int stride, int i, int have, int held)
{
    int x = R8_X0 + i * R8_P, y = R8_Y + 1, w = R8_W, h = R8_H - 2;
    u16 edge = held ? C_HOLD : C_EDGE;
    fill(lay, stride, x, y, w, h, edge);
    fill(lay, stride, x + 1, y + 1, w - 2, h - 2, have ? C_BODY : C_EMPTY);
    if (held) fill(lay, stride, x + 1, y + 1, w - 2, 1, C_HOLD);
    fill(lay, stride, x + 4, y + 4, w - 8, 4, have ? C_SET : C_TOP);
    if (i < 0 || i > 7) return;
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

/* r4 = dev, r5 = req (FUN_08E7D278 args) */
void hc_hook(u32 dev, u32 req)
{
    (void)dev;
    u32 h = hash_preview();
    if (h != hc_hash) {                 /* new track: firmware loaded A-C */
        hc_hash = h;
        R8(HCPAGE) = 0;
        hc_view = 0;                    /* a new track = the stock row */
        hc_borrow = 0;
        hc_restore = 0;
        hc_held = 0;
        for (u32 i = 0; i < 8; i++) hc_ram_set[i] = 0;   /* drop deck cues */
        hc_cap = 0; hc_st = 4;                     /* D-H: prove the ctx again */
        hc_p1ok = 0;
        hc_count = cue_count();
    }
    /* page 1 showing and nothing borrowed: the live slots are the deck's own
     * A-C -- keep their bytes, and take D-H once the ctx agrees with them */
    if (R8(HCPAGE) == 0 && !hc_borrow && !hc_held && !hc_restore) {
        p1_save();
        capture();
    }
    scan_pads();
    if (R8(HCPAGE) >= HCPAGES) {        /* uninitialised page byte: never
                                         * letter the pads off a bad page */
        R8(HCPAGE) = 0;
        return;
    }
    /* the borrowed slot goes back HC_HOLDOFF flushes after the release (the
     * engine may still be reading the slot on the release frame) */
    if (hc_restore && !hc_held && --hc_restore == 0) restore_slot();
    u32 page = R8(HCPAGE);
    if (R8(MSOFF) || R32(MODEW) != 7 || R8(PMODE) != 0) return;
    if (!page && !hc_view) return;
    const u32 *w = (const u32 *)R32(req);
    if (!w || w[13] != 0x4C || w[16] != 0x58) return;
    if ((w[27] >> 24) != 0x82 || w[29] != 0x032001E0u) return;
    u16 *lay = (u16 *)UNCACHED(w[14]);
    int stride = (int)w[17];
    if (stride < 800 || stride > 1024) return;
    if (!page) {                               /* 8-pad view (page 1 only) */
        fill(lay, stride, R8_X0 - 2, R8_Y, 8 * R8_P, R8_H, R8_CLR);
        for (int i = 0; i < 8; i++)
            pad8(lay, stride, i, have_cue(i + 1), hc_held == (u32)(i + 1));
        hc_draws8++;
        return;
    }
    for (int k = 0; k < 3; k++) {
        int n = 3 * (int)page + k;                 /* 0-based letter */
        letter(lay, stride, k, n < 8 ? n : -1, have_cue(n + 1));
    }
    dim_rec(lay, stride);
    hc_draws++;
}
