/* pm.c -- Type 1 phase meter for the XDJ-700 (XDJ-1000MK2 style), deck C.
 *
 * Replaces the stock 4-box meter rows above the centre waveform with two
 * scrolling beat rulers: orange = master (the LINKED deck, from the
 * link-state bar bytes tapped every frame -- 2026-10-31 deck probes:
 * 0x0A8D042E / 0x0A8BE662, "one own / one linked-deck"), blue = this deck.
 * Grey ticks = beats, red = downbeats, +-4 beats around a fixed white "now"
 * line at the waveform playhead (x 366).
 *
 * 2026-11-08 deck report: on the linked deck that is NOT the master only the
 * blue half drew -- the two mirrors are per-player slots, so on a slave deck
 * they do NOT agree and the old "mirrors must agree" test rejected every
 * frame. Each mirror is tapped separately now and the row follows whichever
 * one is actually beating.
 *
 * 2026-11-08 deck report 2 (BOTH decks on 1.0.4, linked): the orange MASTER
 * CLOCK row came in fine on the linked deck but ITS OWN blue row stayed a
 * flat line. The orange row needs only the link bars + SYS_MS; the blue row
 * needs this deck's beat grid, and snapshot_grid() called "no grid yet" a
 * FINAL answer, so the call site stored the track hash and never looked
 * again. On a track served over LINK the overview lands long before the
 * analysis does, so the one snapshot that ran found no grid and the row
 * stayed empty for the whole track. "Not there yet" is now a RETRY (the
 * hash is only stored once a grid is actually published), and the early
 * outs are three word reads, so an unanalysed track costs nothing extra.
 *
 * Called from pmhook.S on every 2DG flush (FUN_08E7D278(r4 dev, r5 req)).
 * Draws only when:
 *   - the request is the full-screen UI quad (same test as perform_pads),
 *   - the main screen (mode word [0x0BA371D4] == 1) or the PERFORM screen
 *     (== 7, same meter 103 px higher) is up,
 *   - MODS are on and PM_MODE == 1,
 *   - the meter is actually visible (stock box border pixel, or our own
 *     sentinel pixel) -> an open Browse list / popup is never painted over.
 * Data (see CLAUDE.md §65): beat grid = ANLZ ctx [0x09826BE4] + 0xD7C
 * ("PQTZ", count @+0x14, ptr @+0x18; LE {u16 beat-in-bar, u16 bpm*100,
 * u32 ms}), snapshotted when the loaded track changes (browsing reuses
 * the ctx); position = [0x0C0E6D1C] (POSL, 150 Hz -- the time digits'
 * own source, live on a real deck; the old 0x09D246E0 word freezes
 * on hardware: CLAUDE.md sec. 103/104).
 *
 * Layer writes go through the uncached P2 alias (the 2DG engine reads RAM).
 */
typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;

#define R32(a) (*(volatile u32 *)(a))
#define R16(a) (*(volatile u16 *)(a))
#define R8(a)  (*(volatile u8 *)(a))
#define UNCACHED(p) (((u32)(p) & 0x1FFFFFFFu) | 0xA0000000u)

#define MODEW      0x0BA371D4u   /* 1 main, 2 browse, 7 PERFORM */
#define CTX_TAB    0x09826BE4u
#define PQTZ_OFF   0xD7Cu
#define TAG_PQTZ   0x5A545150u   /* "PQTZ" as stored */
/* position: POSL (150/s) is what the time digits show -- derived from the
 * DSP stream copy and LIVE on a real deck. The word we used first,
 * [0x09D246E0] (75 Hz), freezes at the load position on real silicon
 * (CLAUDE.md sec. 103/104: healthy real-DSP play, clock still moving), so
 * the meter's beats never scrolled on hardware. POSL @0x0C0E6D1C = 2x that
 * word's units and keeps moving both in the HLE emulator (driven via it)
 * and with the real DSP program. */
#define POSL       0x0C0E6D18u   /* 150 Hz -- 2026-10-25 DECK READING
 * (tools/clock_hunt.py, stock deck playing at +10% tempo): THIS word counts
 * (165/s = track position in 1/150 s, follows the tempo); the 0x0C0E6D1C we
 * read before stayed 68344 (= 455 s, the track length) -> beats stood still.
 * The emulator's stand-in audio chip had been advancing 0x0C0E6D1C. */
#define PREVIEW    0x0BA3BCCCu   /* loaded track's overview (changes on load) */
#ifndef MSOFF
#define MSOFF      0x081DBDF1u   /* MY SETTINGS: 1 = MODS OFF */
#endif

/* geometry (measured on the emulated deck) */
#define X0 260
#define X1 471
#define CX 366
/* main-screen rows; on the PERFORM screen the stock meter sits exactly
 * 103 px higher (same x, same box sizes -- 2026-10-25, tester: "PERFORM
 * shows the 4-beat boxes instead of the phase meter") -> pm_dy */
#define PERFORM_DY (-103)
/* pm_dy is declared in the STATE block below, not here: declared here the
 * linker put it at the very end of the bss, 4 bytes PAST
 * max(pm_mode, pm_n, pm_layer) + 4 -- the region phasemeter_apply.py and
 * permanent_build.py reserve, zero-fill and survey -- so the last word of
 * the cave sat outside its own map (found 2026-11-08). Harmless only
 * because it is written before it is read. */
#define OY (124 + pm_dy)         /* orange (master) line */
#define BY (150 + pm_dy)         /* blue (this deck) line */
#define TOPY (116 + pm_dy)
#define BOTY (158 + pm_dy)

#define RGB(r, g, b) (u16)((((r) >> 3) << 11) | (((g) >> 2) << 5) | ((b) >> 3))
#define C_OR   RGB(232, 141, 33)
#define C_ORD  RGB(90, 55, 15)
#define C_BL   RGB(36, 139, 228)
#define C_BLD  RGB(15, 55, 90)
#define C_TICK RGB(150, 150, 150)
#define C_RED  RGB(226, 40, 40)
#define C_WH   RGB(255, 255, 255)
#define SENTINEL 0x0821          /* near-black marker we own */
#define STOCK_BORDER 0x89E0      /* stock orange box border at (260,120) */

#define MAXBEATS 4096            /* ~34 min at 120 BPM */

/* ---- state (in the cave's bss; the installer zero-fills it) ---- */
volatile u8 pm_mode;             /* 0 = stock meter, 1 = Type 1 */
u32 pm_light;                    /* LIGHT's number in the theme switcher
                                  * (0 = none): the installer sets it; the
                                  * white/grey bits below follow the theme */
static u32 pm_hash;
static u32 pm_n;
static u32 pm_ms[MAXBEATS];
static u8 pm_bar[MAXBEATS];      /* beat-in-bar 1..4 */
u32 pm_draws;                    /* frames drawn (test/diagnostic) */
u32 pm_layer;                    /* layer drawn last (test: buffers alternate) */
int pm_dy;                       /* 0 main screen, -103 on PERFORM: kept in
                                  * this block so the installer's cave end
                                  * covers every byte we own (see the
                                  * geometry block) */

/* ---- master (linked deck) tap ----
 * The link-state RAM carries a beat-in-bar byte per player: 0x0A8D042E and
 * 0x0A8BE662 (2026-10-31, docs/DECK_SESSION_2026-10-31.md: link_beat_search
 * with THIS deck paused found both cycling 1..4 -- "one own / one linked
 * deck"). They only agree while the two players sit on the same bar, so the
 * first build's `a != b -> return` left the orange row empty on the deck
 * that was not the master (user report 2026-11-08: "only the bottom half of
 * the phase meter, nothing on the upper half from deck 1"). Both mirrors are
 * tapped independently now: a change is accepted only when it steps the bar
 * 1..4 by exactly one (junk filter) and its period is plausible, and the row
 * follows the tap that beat most recently. Paused master (nothing fresh for
 * 4 periods): the ruler freezes at the last known beat instead of
 * extrapolating into the void. */
#define MST_BAR    0x0A8D042Eu
#define MST_BAR2   0x0A8BE662u
#define MST_MAXP   3000u              /* ms: slower than ~20 BPM = noise */
/* The master is timed on the firmware's 1 ms SYSTEM clock, NOT on POSL:
 * POSL is THIS deck's track position and stands still while this deck is
 * paused -- exactly when you sync to a playing master (first build used
 * POSL: every interval measured 0 ms, the period never set, the orange
 * row stayed empty). 0x0B704A80 is incremented by the TMU underflow ISR
 * at 0x08D632C0 (clears TCR.UNF @0xFFD9001C, then [0x0B704A80] += 1);
 * emulator: 1.000 per ms vs the 3 ms heartbeat / 10 ms / 100 ms
 * counters (2026-11-01, independent of play state). */
#define SYS_MS     0x0B704A80u

static void vline(u16 *lay, int stride, int x, int y0, int y1, u16 c);

typedef struct {
    u32 prev;                    /* last raw bar (0 = not seen yet) */
    u32 bar;                     /* bar at the last accepted change (1..4) */
    u32 pos;                     /* monotonic beat count (per change) */
    u32 t;                       /* SYS_MS of the last accepted change */
    u32 p;                       /* smoothed period (ms); 0 = unknown */
} mtap;
static mtap mt[2];               /* one tap per link mirror */
static u32 m_sel;                /* the mirror the orange row follows */
u32 pm_mtap;                     /* accepted changes, both taps (diag) */
u32 pm_msel;                     /* the tap the row follows (diag) */

static void tap_one(mtap *m, u32 v, u32 ms)
{
    if (v < 1 || v > 4) return;                  /* mid-write / not live */
    if (!m->prev) {                              /* first sighting */
        m->prev = m->bar = v;
        m->t = ms;
        return;
    }
    if (v == m->prev) return;
    if (v != (m->prev % 4) + 1) return;          /* not a 1..4 beat step */
    u32 d = ms - m->t;
    m->prev = m->bar = v;                        /* exactly one beat */
    m->t = ms;
    m->pos++;
    pm_mtap++;
    if (d > 150 && d < MST_MAXP)                 /* one plausible period */
        m->p = m->p ? (m->p * 3 + d) / 4 : d;    /* EMA, ~4 beats settle */
}

/* a tap that changed within the last 4 periods is the one that is playing */
static int fresh(const mtap *m, u32 ms)
{
    return m->p && (int)(ms - m->t) < (int)(4 * m->p + 500);
}

static void tap_master(u32 ms)
{
    tap_one(&mt[0], R8(UNCACHED(MST_BAR)), ms);
    tap_one(&mt[1], R8(UNCACHED(MST_BAR2)), ms);
    int f0 = fresh(&mt[0], ms), f1 = fresh(&mt[1], ms);
    if (f0 && f1)
        m_sel = (int)(mt[0].t - mt[1].t) >= 0 ? 0 : 1;
    else if (f0) m_sel = 0;
    else if (f1) m_sel = 1;                      /* neither: hold, freeze */
    pm_msel = m_sel;
}

static void draw_master(u16 *lay, int stride, int ms, u16 tick)
{
    const mtap *m = &mt[m_sel];
    if (!m->p) return;
    /* frozen when the master stopped (byte quiet > 1 period): read "now"
     * as last-change + 1 period so the ruler holds its position */
    int now = ms;
    int lim = (int)(m->t + m->p + m->p / 2);
    if (now > lim) now = lim;
    for (int k = -4; k <= 4; k++) {
        int t = (int)(m->t + (u32)(k * (int)m->p));
        int x = CX + (t - now) * (int)(X1 - X0) / (8 * (int)m->p);
        int bi = (int)(m->bar - 1) + k;
        bi %= 4;
        if (bi < 0) bi += 4;
        if (bi == 0) {                           /* downbeat: red, crosses */
            vline(lay, stride, x - 1, OY - 7, OY + 7, C_RED);
            vline(lay, stride, x, OY - 7, OY + 7, C_RED);
        } else {
            vline(lay, stride, x, OY - 7, OY - 2, tick);
        }
    }
}

#include "pm_label.h"            /* LABEL_W, LABEL_H, label_alpha[] (0..32) */

/* ---- light theme (tools/lighttext follows the same rule) ---- */
#ifndef TS_DATA
#define TS_DATA 0x09779000u        /* the theme switcher's blob */
#endif
static int lit(void)              /* LIGHT is the switcher's current theme */
{
    return pm_light && R32(TS_DATA) == 0x57535458u
        && R8(TS_DATA + 32) == pm_light;
}

/* lt.c's neutral flip on an RGB565 word: neutrals -> grey(v), colours keep */
static u16 flip(u16 c)
{
    u32 r = (c >> 11) & 31, g = (c >> 5) & 63, b = c & 31;
    u32 mx = r > b ? r : b, mn = r < b ? r : b;
    if (g > mx) mx = g;
    if (g < mn) mn = g;                 /* 6-bit domain */
    if (mx - mn > 12) return c;         /* coloured */
    u32 v = 255 - ((mx << 2) * 7 >> 3);
    v = v * v >> 8;
    return (u16)(((v >> 3) << 11) | ((v >> 2) << 5) | (v >> 3));
}

static u32 hash_preview(void)
{
    u32 h = 2166136261u;
    const u32 *p = (const u32 *)PREVIEW;
    for (int i = 0; i < 100; i++) h = (h ^ p[i]) * 16777619u;
    return h;
}

/* 2026-10-25 DECK: deck 1 froze twice while deck 2 played a track loaded
 * from deck 1's stick over Pro DJ Link. Deck 1's analysis code then serves a
 * SECOND track through the same ANLZ context we read here, so the grid can
 * be mid-rewrite (or point anywhere) when we copy it. Every pointer and
 * length is range-checked before a single read, the header is read again
 * after the copy (changed = discard, retry next frame), and the beat times
 * must rise. Returns 1 only when a grid was published, 0 to retry on a
 * later frame -- including "the analysis has not arrived yet", which is the
 * normal state for the first frames of a LINK-loaded track (2026-11-08). */
#define RAM_LO 0x08000000u
#define RAM_HI 0x10000000u
static int in_ram(u32 a, u32 len)
{
    return a >= RAM_LO && a < RAM_HI && len <= RAM_HI - a;
}

u32 pm_rejects;                      /* diagnostic: discarded snapshots */

static int snapshot_grid(void)
{
    u32 ctx = R32(CTX_TAB);
    pm_n = 0;
    /* NOT "final": a track that just started loading has no context yet, and
     * a LINK-served one gets its analysis well after its overview. Returning
     * 1 here is what kept the blue row empty for a whole track. */
    if (!in_ram(ctx, PQTZ_OFF + 0x1C) || (ctx & 3))
        return 0;                    /* no track context yet */
    u32 q = ctx + PQTZ_OFF;
    if (R32(q) != TAG_PQTZ || R32(q + 0x10) != 8)
        return 0;                    /* no PQTZ header yet */
    u32 n = R32(q + 0x14), ep = R32(q + 0x18);
    if (n < 2)
        return 0;                    /* header there, beats not yet */
    if (n > MAXBEATS) n = MAXBEATS;
    if (!in_ram(ep, 8 * n)) {
        pm_rejects++;
        return 0;
    }
    const u8 *e = (const u8 *)ep;
    u32 prev = 0;
    for (u32 i = 0; i < n; i++, e += 8) {
        u32 ms = e[4] | e[5] << 8 | e[6] << 16 | (u32)e[7] << 24;
        if (e[0] < 1 || e[0] > 4 || (i && ms <= prev)) {
            pm_rejects++;
            return 0;                /* half-written or not a grid */
        }
        pm_bar[i] = e[0];
        pm_ms[i] = prev = ms;
    }
    if (R32(ctx + PQTZ_OFF) != TAG_PQTZ || R32(q + 0x14) < n
        || R32(q + 0x18) != ep || R32(CTX_TAB) != ctx) {
        pm_rejects++;
        return 0;                    /* changed under us */
    }
    pm_n = n;
    return 1;
}

static inline void put(u16 *lay, int stride, int x, int y, u16 c)
{
    lay[y * stride + x] = c;
}

static void vline(u16 *lay, int stride, int x, int y0, int y1, u16 c)
{
    if (x < X0 || x > X1) return;
    for (int y = y0; y <= y1; y++) put(lay, stride, x, y, c);
}

static u16 blend(u16 c, int a)   /* c * a/32 over black */
{
    int r = (c >> 11) * a >> 5, g = ((c >> 5) & 63) * a >> 5, b = (c & 31) * a >> 5;
    return (u16)(r << 11 | g << 5 | b);
}

/* current beat index i and fraction (0..255) of the way to beat i+1 */
static int beat_at(u32 ms, int *frac)
{
    if (pm_n < 2 || ms < pm_ms[0]) return -1;
    int lo = 0, hi = (int)pm_n - 1;
    if (ms >= pm_ms[hi]) { *frac = 0; return hi; }
    while (hi - lo > 1) {             /* pm_ms[lo] <= ms < pm_ms[hi] */
        int mid = (lo + hi) >> 1;
        if (pm_ms[mid] <= ms) lo = mid; else hi = mid;
    }
    u32 span = pm_ms[lo + 1] - pm_ms[lo];
    *frac = span ? (int)(((ms - pm_ms[lo]) << 8) / span) : 0;
    return lo;
}

void pm_draw(u16 *lay, int stride)
{
    /* the row lines (full width), dim when the row has no data */
    int i = -1, frac = 0;
    u32 ms = R32(POSL) * 20u / 3u;              /* 1/150 s -> ms */
    int L = lit();                              /* light: whites/greys flip */
    u16 bg = L ? flip(0) : (u16)0;              /* box + label clear */
    u16 tick = L ? flip(C_TICK) : C_TICK;
    u16 now = L ? flip(C_WH) : C_WH;
    if (pm_n) i = beat_at(ms, &frac);
    u32 sms = R32(SYS_MS);                      /* free-running 1 ms */
    tap_master(sms);
    for (int y = TOPY; y <= BOTY; y++)
        for (int x = X0 - 2; x <= X1 + 2; x++) put(lay, stride, x, y, bg);
    for (int x = X0; x <= X1; x++) {
        put(lay, stride, x, OY, C_ORD); put(lay, stride, x, OY + 1, C_ORD);
        u16 b = i >= 0 ? C_BL : C_BLD;
        put(lay, stride, x, BY, b); put(lay, stride, x, BY + 1, b);
    }
    if (i >= 0) {
        for (int k = -5; k <= 5; k++) {
            int j = i + k;
            if (j < 0 || j >= (int)pm_n) continue;
            /* 211 px for 8 beats */
            int x = CX + (((k << 8) - frac) * (X1 - X0)) / (8 << 8);
            if (pm_bar[j] == 1) {             /* downbeat: red, crosses line */
                vline(lay, stride, x - 1, BY - 8, BY + 9, C_RED);
                vline(lay, stride, x, BY - 8, BY + 9, C_RED);
            } else {
                vline(lay, stride, x, BY - 8, BY - 2, tick);
            }
        }
    }
    draw_master(lay, stride, (int)sms, tick);
    for (int y = TOPY - 1; y <= BOTY + 1; y++)       /* now */
        for (int x = CX - 1; x <= CX + 1; x++) put(lay, stride, x, y, now);
    /* clear the label area first: the stock "MASTER PLAYER" label + icon
     * (linked deck master) may have been drawn there before we stripped it */
    for (int y = 0; y < LABEL_H; y++)
        for (int x = -8; x < LABEL_W; x++)
            if (LABEL_X + x < X0 - 2) put(lay, stride, LABEL_X + x, LABEL_Y + pm_dy + y, bg);
    for (int y = 0; y < LABEL_H; y++)                /* MASTER CLOCK */
        for (int x = 0; x < LABEL_W; x++) {
            int a = label_alpha[y * LABEL_W + x];
            if (a) put(lay, stride, LABEL_X + x, LABEL_Y + pm_dy + y, blend(C_OR, a));
        }
    put(lay, stride, X0 - 2, TOPY, SENTINEL);
    put(lay, stride, X1 + 2, TOPY, SENTINEL);      /* right end, see pm_hook */
    pm_draws++;
}

/* reference x 200 at the blue row's height: below our MASTER CLOCK label
 * (y116..145), clear of the stock master marker that appears at x 254
 * when a linked deck is master (deck pm_probe 2026-10-25: (254,120/124)
 * = 0xFB80 made an x-254 reference step aside). Visible = the background
 * right of the meter (x 476, both rows) equals that reference; the MENU
 * popup covers x 430+, BROWSE / UTILITY are other screen modes. */
/* the PERFORM button is drawn (main and PERFORM screens both have it at
 * x 652..783, y 20..63); the boot logo shows with MODEW already 1 and a
 * plain black meter area, so visible() alone drew the meter over it */
static int screen_up(const u16 *lay, int stride)
{
    u16 a = lay[40 * stride + 700], b = lay[40 * stride + 760];
    return a && b && a != 0xF81Fu && b != 0xF81Fu;
}

static int visible(const u16 *lay, int stride)
{
    u16 bl = lay[BY * stride + 200];
    return lay[OY * stride + 476] == bl && lay[BY * stride + 476] == bl;
}

/* 2026-10-25 deck: with a LINKED deck playing, the stock meter repaints its
 * two 216x20 rows more often and lands on top of ours. Each repaint is its
 * own small flush request whose 2DG list holds one textured quad (op 0x82,
 * 32 B: src size 216x20, top-left (258, 117|138)+dy). While our meter is
 * showing we turn that one command into NOPs (0x08000000) and let the call
 * go through unchanged (skipping the call would take the caller's error
 * path). The list is uncached (P2) and ends at the cursor at req-4. */
u32 pm_stripped;                                   /* diagnostic */
u32 pm_last[8];             /* last stripped command (diagnostic) */
u32 pm_miss[8], pm_nmiss;   /* last quad touching the box, NOT stripped */
u32 pm_otherop, pm_nother;  /* fills touching (not inside) the box */
u32 pm_nfill;               /* fills / flat polygons stripped */
u32 pm_nline;               /* polylines stripped */
u32 pm_seen[8][8], pm_nseen; /* distinct small quads/fills touching the box */
static void seen(volatile u32 *c, u32 n)
{
    for (u32 j = 0; j < pm_nseen; j++) {
        u32 k = 0;
        while (k < n && pm_seen[j][k] == c[k]) k++;
        if (k == n) return;
    }
    if (pm_nseen >= 8) return;
    for (u32 k = 0; k < 8; k++) pm_seen[pm_nseen][k] = k < n ? c[k] : 0;
    pm_nseen++;
}
u32 pm_visfail, pm_vispx;   /* gate failures + last (476,OY)<<16|(200,BY) */
#define BOX_X0 252
#define BOX_X1 480
static void strip_stock(u32 req)
{
    u32 l = R32(req), end = R32(req - 4);
    if ((l >> 29) != 5 || end <= l || end - l > 0x400 || ((end - l) & 3))
        return;
    volatile u32 *w = (volatile u32 *)l;
    u32 n = (end - l) >> 2, i = 0;
    while (i < n) {
        u32 op = w[i] >> 24, len;
        switch (op) {
        case 0x18: len = 3; break;
        case 0x40: case 0x48: len = 2; break;
        case 0x08: case 0x12: len = 1; break;
        case 0xA0: case 0xA2: case 0x80: len = 6; break;
        case 0x82: len = 8; break;
        case 0xB0: len = 3 + (i + 1 < n ? (w[i + 1] & 0xFFFF) : 0); break;
        default: return;                           /* 0x00 end / unknown */
        }
        if (i + len > n) return;
        if (op == 0x82) {
            /* corners +4..+7 = (x << 16) | y: TL TR BR BL. Strip a quad that
             * lies wholly inside the meter box (the stock rows are 216x20 at
             * (258, 117|138); a linked deck's master state may paint other
             * pieces there too). Anything reaching outside is left alone. */
            u32 x0 = 0xFFFF, y0 = 0xFFFF, x1 = 0, y1 = 0;
            for (u32 k = 4; k < 8; k++) {
                u32 x = w[i + k] >> 16, y = w[i + k] & 0xFFFF;
                if (x < x0) x0 = x;
                if (x > x1) x1 = x;
                if (y < y0) y0 = y;
                if (y > y1) y1 = y;
            }
            int touch = (int)x1 >= BOX_X0 && (int)x0 <= BOX_X1 &&
                        (int)y1 >= TOPY - 6 && (int)y0 <= BOTY + 6;
            int inlabel = (int)x0 >= LABEL_X - 8 && (int)x1 <= BOX_X1 &&
                          (int)y0 >= LABEL_Y + pm_dy - 2 && (int)y1 <= LABEL_Y + pm_dy + LABEL_H + 2;
            if (((int)x0 >= BOX_X0 && (int)x1 <= BOX_X1 &&
                 (int)y0 >= TOPY - 6 && (int)y1 <= BOTY + 6) || inlabel) {
                for (u32 k = 0; k < 8; k++) { pm_last[k] = w[i + k]; w[i + k] = 0x08000000u; }
                pm_stripped++;
            } else if (touch) {
                for (u32 k = 0; k < 8; k++) pm_miss[k] = w[i + k];
                pm_nmiss++;
                if (x1 - x0 < 400) seen(&w[i], 8);
            }
        } else if (op == 0xA0 || op == 0xA2) {
            /* fill / copy: +3 XY(L,R), +4 XY(U,D) around the anchor +5
             * (rect = [x-L, x+R] x [y-U, y+D]). Deck 2026-10-26: with a
             * linked deck the stock meter also paints its boxes this way. */
            int ax = (short)(w[i + 5] >> 16), ay = (short)(w[i + 5] & 0xFFFF);
            int l = (short)(w[i + 3] >> 16), r = (short)(w[i + 3] & 0xFFFF);
            int u = (short)(w[i + 4] >> 16), d = (short)(w[i + 4] & 0xFFFF);
            int x0 = ax - l, x1 = ax + r, y0 = ay - u, y1 = ay + d;
            if (x0 >= BOX_X0 && x1 <= BOX_X1 && y0 >= TOPY - 6 && y1 <= BOTY + 6) {
                for (u32 k = 0; k < 6; k++) w[i + k] = 0x08000000u;
                pm_nfill++;
            } else if (x1 >= BOX_X0 && x0 <= BOX_X1 && y1 >= TOPY - 6 && y0 <= BOTY + 6) {
                pm_otherop = w[i];
                pm_nother++;
                if (x1 - x0 < 400) seen(&w[i], 6);
            }
        } else if (op == 0xB0) {                   /* polyline (box outlines) */
            u32 np = w[i + 1] & 0xFFFF;
            int x0 = 0xFFFF, y0 = 0xFFFF, x1 = -1, y1 = -1;
            for (u32 k = 0; k < np; k++) {
                int x = (short)(w[i + 3 + k] >> 16), y = (short)(w[i + 3 + k] & 0xFFFF);
                if (x < x0) x0 = x;
                if (x > x1) x1 = x;
                if (y < y0) y0 = y;
                if (y > y1) y1 = y;
            }
            if (np && x0 >= BOX_X0 && x1 <= BOX_X1 && y0 >= TOPY - 6 && y1 <= BOTY + 6) {
                for (u32 k = 0; k < len; k++) w[i + k] = 0x08000000u;
                pm_nline++;
            } else if (np && x1 >= BOX_X0 && x0 <= BOX_X1 && y1 >= TOPY - 6 && y0 <= BOTY + 6
                       && x1 - x0 < 400) {
                seen(&w[i], len < 8 ? len : 8);
            }
        } else if (op == 0x80) {                   /* flat 4-corner polygon */
            int x0 = 0xFFFF, y0 = 0xFFFF, x1 = -1, y1 = -1;
            for (u32 k = 2; k < 6; k++) {
                int x = (short)(w[i + k] >> 16), y = (short)(w[i + k] & 0xFFFF);
                if (x < x0) x0 = x;
                if (x > x1) x1 = x;
                if (y < y0) y0 = y;
                if (y > y1) y1 = y;
            }
            if (x0 >= BOX_X0 && x1 <= BOX_X1 && y0 >= TOPY - 6 && y1 <= BOTY + 6) {
                for (u32 k = 0; k < 6; k++) w[i + k] = 0x08000000u;
                pm_nfill++;
            }
        }
        i += len;
    }
}


/* diagnostic only: lists aimed at OTHER layers -- any small quad / fill
 * near the meter's screen position, with the request's layer + stride */
u32 pm_other[16][8], pm_nothl;
static void scan_other(u32 req)
{
    u32 l = R32(req), end = R32(req - 4);
    if ((l & 0x1FFFFFFFu) - 0x08000000u >= 0x08000000u || end <= l ||
        end - l > 0x400 || ((end - l) & 3))
        return;
    volatile u32 *w = (volatile u32 *)UNCACHED(l);
    u32 n = (end - l) >> 2, i = 0;
    while (i < n) {
        u32 op = w[i] >> 24, len;
        switch (op) {
        case 0x18: len = 3; break;
        case 0x40: case 0x48: len = 2; break;
        case 0x08: case 0x12: len = 1; break;
        case 0xA0: case 0xA2: case 0x80: len = 6; break;
        case 0x82: len = 8; break;
        default: return;
        }
        if (i + len > n) return;
        if ((op == 0x82 || op == 0xA0 || op == 0xA2) && pm_nothl < 16) {
            u32 tl = op == 0x82 ? w[i + 4] : w[i + 5];
            int x = (short)(tl >> 16), y = (short)(tl & 0xFFFF);
            u32 shape = op == 0x82 ? w[i + 2] : w[i + 3] ^ (w[i + 4] << 1);
            int dup = 0;
            for (u32 j = 0; j < pm_nothl; j++)   /* one entry per layer+shape */
                if (pm_other[j][0] == R32(req + 12) && pm_other[j][2] == w[i] &&
                    pm_other[j][7] == shape) dup = 1;
            if (!dup && x >= 200 && x <= 500 && y >= 0 && y <= 200) {
                u32 j = pm_nothl++;
                pm_other[j][0] = R32(req + 12);
                pm_other[j][1] = R32(req + 32);
                for (u32 k = 0; k < 5; k++) pm_other[j][2 + k] = w[i + (k < len ? k : 0)];
                pm_other[j][7] = shape;
            }
        }
        i += len;
    }
}

/* r4 = dev, r5 = req (FUN_08E7D278 args); returns nothing, never blocks */
void pm_hook(u32 dev, u32 req)
{
    (void)dev;
    if (pm_mode != 1 || R8(MSOFF)) return;
    u32 mw = R32(MODEW);
    if (mw == 1) pm_dy = 0;                            /* main screen */
    else if (mw == 7) pm_dy = PERFORM_DY;              /* PERFORM screen */
    else return;
    {   /* a stock meter repaint? Judge visibility on the UI layer our
         * meter draws into (the repaint's own target may be another alias
         * or stride when a linked deck is master). */
        u32 t = pm_layer ? pm_layer : R32(req + 12);
        /* only lists aimed at an 800-px-wide (UI-sized) layer: other
         * layers use their own coordinates (waveform strips etc.) */
        if (R32(req + 32) == 1600u && (t & 0x1FFFFFFFu) - 0x08000000u < 0x08000000u &&
            screen_up((u16 *)UNCACHED(t), 800) && visible((u16 *)UNCACHED(t), 800))
            strip_stock(req);
        else if (pm_nothl < 16)
            scan_other(req);
    }
    const u32 *w = (const u32 *)R32(req);              /* display list */
    if (!w || w[13] != 0x4C || w[16] != 0x58) return;
    if ((w[27] >> 24) != 0x82 || w[29] != 0x032001E0u) return;
    u16 *lay = (u16 *)UNCACHED(w[14]);
    int stride = (int)w[17];
    if (stride < 800 || stride > 1024) return;
    /* visible = the plain background either side of the meter is the same
     * colour (right side vs a reference left of the label). 2026-10-25 deck: when a LINKED deck plays (becomes master) the
     * stock meter lights up in other colours, the old "stock border or our
     * sentinel" test failed and the stock "Bars" meter came back; and the
     * MENU popup covers only the right side (x 430+), so a left-only test
     * drew over it. BROWSE (mode 2) / UTILITY (6) are excluded by MODEW. */
    if (!screen_up(lay, stride) || !visible(lay, stride)) {
        pm_visfail++;
        pm_vispx = (u32)lay[OY * stride + 476] << 16 | lay[BY * stride + 200];
        return;
    }
    u32 h = hash_preview();
    /* the hash marks "this track is now settled" only once a grid exists, so
     * a track whose grid arrives late (LINK) keeps being asked about */
    if (h != pm_hash && snapshot_grid()) pm_hash = h;   /* 0 = retry */
    pm_draw(lay, stride);
    pm_layer = w[14];
}
