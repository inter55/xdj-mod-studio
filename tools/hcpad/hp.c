/* hp.c -- hot cue pads: SWIPE across A B C to page, like BEAT JUMP
 * (user 2026-11-13: "whatever is closest to my intent, like beatjump").
 *
 * The stock hot cue pad fires the moment a finger lands on it, so a swipe
 * starting on a pad would jump to that cue first. This module sits FIRST
 * in the touch chain (pool 0x08D5278C) and, on the PERFORM screen in HOT
 * CUE mode, holds a press on pad A/B/C back until it knows what it is:
 *   - slid >= SWIPE_PX sideways  -> page (left = next: A-C -> D-F -> G-H,
 *     right = previous), no cue;
 *   - held still >= HOLD_MS      -> passed on to the stock pad as a press at
 *     the spot it landed, for as long as the finger stays down (so the
 *     deck's own hot cue and HOT CUE GATE work as before, just HOLD_MS later);
 *   - lifted sooner (a tap)      -> passed on as a short press on release
 *     (REPLAY hit-tester calls), so the cue fires on lift like a BEAT JUMP pad.
 * A finger landing on REC is held back the same way: it is a REC tap only if
 * it lifts without sliding (hc_rec then arms the deck's own record mode, the
 * pad press stores); a slide from there pages instead.
 * Everything else goes down the chain untouched. Page changes call the 8
 * hot cue module's own hc_next / hc_prev_page (addresses at build time).
 *
 * hp_touch returns 0 = chain with the original args, 1 = consumed,
 * 2 = chain with the touch in hp_fwd[0..2] (down, x, y). */
typedef unsigned int u32;
typedef unsigned char u8;
typedef unsigned short u16;
#define R8(a)  (*(volatile u8 *)(a))
#define R32(a) (*(volatile u32 *)(a))

#ifndef PST
#error "build with -DPST=... -DMSOFF=... -DHC_NEXT=... -DHC_PREV=..."
#endif
#define MODEW    0x0BA371D4u
#define ZSTATE   0x0C0E75BCu
#define MSCLK    0xAB704A80u            /* 1 ms system clock, uncached */
#ifndef SWIPE_PX
#define SWIPE_PX 40
#endif
#ifndef HOLD_MS
#define HOLD_MS  150u
#endif
#ifndef REC_GAP_MS
#define REC_GAP_MS 400u
#endif
#ifndef RECARM
#error "build with -DRECARM=... (hotcue8 hc_recarm: 1 = REC record mode)"
#endif
#ifndef P1USED
#error "build with -DP1USED=... (hotcue8 hc_p1used: pad used off page 1)"
#endif
#ifndef REPLAY
#define REPLAY   2u
#endif
#define PAD_Y    232
#define PAD_H    63

typedef void (*fn_t)(void);
u32 hp_fwd[3];
u32 hp_taps, hp_holds, hp_pages, hp_seen, hp_recs;    /* diagnostics */
static u32 st, x0, y0, t0, replay, rec_t = 0x80000000u, pk = 0;
u32 hp_recbounce;   /* st: 0 idle 1 pending 2 held 3 swiped 4 tap 5 rec pending */

static int pad_at(int x, int y)
{
    /* the whole A..C stretch, gaps included (a swipe may start between
     * two pads; a tap in a gap goes to the nearer pad) */
    if (y < PAD_Y || y >= PAD_Y + PAD_H || x < 116 || x >= 444) return -1;
    return x < 223 ? 0 : x < 337 ? 1 : 2;
}

static u32 fwd(u32 down)
{
    hp_fwd[0] = down; hp_fwd[1] = x0; hp_fwd[2] = y0;
    return 2;
}

/* ------------------------------------------------ cue marker labels ----
 * 2026-11-16 (deck): the markers above the bottom waveform are labelled by
 * the slot's PAD number (1..3, which the deck needs to play the cue -- see
 * hc.c PADNUM), so cues D-F showed as "A B C". The 700 only has three label
 * pictures (green 8x9, two colours: 0x0700 on the 0xF81F transparent key),
 * reached through its sprite table. While page D-F / G-H is up, their
 * pixels are redrawn as D E F / G H; on page A-C, a new track (page 0),
 * MODS OFF or any screen other than the main screen / PERFORM (so a theme
 * switch in UTILITY always sees the stock pictures) the stock A B C go back.
 * Nothing is written unless the table still points at the stock pictures and
 * each picture holds exactly what we expect there. */
#define LBL_A   0x083B9130u
#define LBL_B   0x083BA330u
#define LBL_C   0x083BA450u
#define LBL_KEY 0xF81Fu
static const u32 LBL_PIC[3] = { LBL_A, LBL_B, LBL_C };
static const u32 LBL_TAB[3] = { 0x081050A8u, 0x08105208u, 0x08105260u };
/* rows, bit 7 = left column: A B C (stock) then D E F G H */
static const u8 LBL_PAT[8][9] = {
    { 0x18, 0x18, 0x24, 0x24, 0x24, 0x7E, 0x42, 0x42, 0x42 },   /* A */
    { 0x7C, 0x42, 0x42, 0x42, 0x7C, 0x42, 0x42, 0x42, 0x7C },   /* B */
    { 0x38, 0x44, 0x82, 0x80, 0x80, 0x80, 0x82, 0x44, 0x38 },   /* C */
    { 0x78, 0x44, 0x42, 0x42, 0x42, 0x42, 0x42, 0x44, 0x78 },   /* D */
    { 0x7E, 0x40, 0x40, 0x40, 0x7C, 0x40, 0x40, 0x40, 0x7E },   /* E */
    { 0x7E, 0x40, 0x40, 0x40, 0x7C, 0x40, 0x40, 0x40, 0x40 },   /* F */
    { 0x38, 0x44, 0x82, 0x80, 0x80, 0x8E, 0x82, 0x46, 0x3A },   /* G */
    { 0x42, 0x42, 0x42, 0x42, 0x7E, 0x42, 0x42, 0x42, 0x42 },   /* H */
};
u32 hp_lbl, hp_lblw, hp_lblbad;            /* page shown, rewrites, refusals */
static u16 lbl_on;
#define PX(p, i) (*(volatile unsigned short *)(((p) | 0xA0000000u) + 2u * (i)))
static int lbl_is(u32 pic, int letter)
{
    for (int y = 0; y < 9; y++)
        for (int x = 0; x < 8; x++) {
            u16 v = PX(pic, y * 8 + x);
            int on = (LBL_PAT[letter][y] >> (7 - x)) & 1;
            if (on ? v == LBL_KEY : v != LBL_KEY) return 0;
            if (on) lbl_on = v;
        }
    return 1;
}
static void lbl_put(u32 pic, int letter)
{
    for (int y = 0; y < 9; y++)
        for (int x = 0; x < 8; x++)
            PX(pic, y * 8 + x) = ((LBL_PAT[letter][y] >> (7 - x)) & 1) ? lbl_on : LBL_KEY;
}
static int lbl_want(u32 page, int k)       /* letter on picture k for a page */
{
    int n = 3 * (int)page + k;
    return n < 8 ? n : k;                 /* past H: no cue, keep the stock C */
}
static void lbl_update(void)
{
    u32 mw = R32(MODEW), page = 0;
    if (!R8(MSOFF) && (mw == 7 || mw == 1)) page = R8(HCPAGE);
    if (page > 2) page = 0;
    if (page == hp_lbl) return;
    for (int k = 0; k < 3; k++)
        if (R32(LBL_TAB[k]) != LBL_PIC[k] || !lbl_is(LBL_PIC[k], lbl_want(hp_lbl, k))) {
            hp_lblbad++;                   /* not what we left there: hands off */
            hp_lbl = page;
            return;
        }
    for (int k = 0; k < 3; k++) {
        lbl_is(LBL_PIC[k], lbl_want(hp_lbl, k));          /* this picture's colour */
        lbl_put(LBL_PIC[k], lbl_want(page, k));
    }
    hp_lbl = page;
    hp_lblw++;
}

u32 hp_touch(u32 down, u32 x, u32 y)
{
    lbl_update();
    if (down) {
        if (st == 0) {
            if (R32(MODEW) != 7 || R8(MSOFF) || R8(PST + 2) != 0 || R32(ZSTATE) != 30)
                return 0;
#ifdef TFX_MODE
            if (R32(TFX_MODE)) return 0;         /* TOUCH FX panel open: its pad */
#endif
            /* REC on pages D-F / G-H: hc_rec toggles the deck's record mode
             * (the pads mod blocks stock REC there, it would save as A-C).
             * 2026-11-18 (deck: "click REC and swipe creates a D cue"): the
             * tap is only decided on LIFT, like a pad -- a finger that landed
             * on REC and then slid is a page swipe, not a REC press, so it
             * can neither arm record mode nor store a cue. */
            if (R8(HCPAGE) && (int)y >= PAD_Y && (int)y < PAD_Y + PAD_H &&
                (int)x >= 460 && (int)x < 538) {
                st = 5; x0 = x; y0 = y; t0 = R32(MSCLK);
                return 1;
            }
            int k = pad_at((int)x, (int)y);
            if (k < 0) return 0;
            /* this pad was used away from page 1: hc.c must reload it when
             * page 1 comes back (its engine cue is this page's now) */
            if (R8(HCPAGE)) R8(P1USED + k) = 1;
            st = 1; x0 = x; y0 = y; t0 = R32(MSCLK); pk = (u32)k;
            hp_seen++;
            return 1;
        }
        if (st == 1) {
            int dx = (int)x - (int)x0;
            if (dx <= -SWIPE_PX || dx >= SWIPE_PX) {
                ((fn_t)(dx < 0 ? HC_NEXT : HC_PREV))();
                st = 3; hp_pages++;
                return 1;
            }
            /* REC record mode: stock does not recall while recording, so
             * hold this press back (no forward at all) and store on lift */
            if (R32(RECARM)) return 1;
            if (R32(MSCLK) - t0 < HOLD_MS) return 1;
            st = 2; hp_holds++;
        }
        if (st == 5) {                        /* REC: still a tap? */
            int dx = (int)x - (int)x0;
            if (dx <= -SWIPE_PX || dx >= SWIPE_PX) {
                ((fn_t)(dx < 0 ? HC_NEXT : HC_PREV))();
                st = 3; hp_pages++;
            }
            return 1;
        }
        if (st == 2) return fwd(1);
        if (st == 4) { st = 0; return 0; }      /* new touch during a replay */
        return 1;                                /* swiped: until lifted */
    }
    if (st == 5) {                        /* REC tap: one mode change */
        /* 2026-11-16 (deck): one REC press made three cues -- the panel
         * reports a long press as quick lift/press bounces and each "press"
         * set the next empty pad. One change per REC_GAP_MS. */
        u32 now = R32(MSCLK);
        st = 0;
        if (now - rec_t >= REC_GAP_MS) {
            rec_t = now;
            hp_recs += ((u32 (*)(u32))HC_REC)(~0u);
        } else
            hp_recbounce++;
        return 1;
    }
    if (st == 1) {                        /* lift without a slide = a tap */
        if (R32(RECARM)) {                /* record mode: this pad stores */
            st = 0;
            hp_recs += ((u32 (*)(u32))HC_REC)(pk);
            return 1;
        }
        st = 4; replay = REPLAY; hp_taps++;
    }
    if (st == 4) {
        if (replay) { replay--; return fwd(1); }
        st = 0;
        return fwd(0);
    }
    if (st == 2) { st = 0; return fwd(0); }
    st = 0;
    return 0;
}
