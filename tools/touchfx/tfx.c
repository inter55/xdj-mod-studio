/* tfx.c -- TOUCH FX panel on the XDJ-700 PERFORM screen (SH-4 side).
 *
 * A fifth button under the PERFORM tab stack (HOT CUE / BEAT JUMP / KEY
 * SHIFT / STEMS) opens the TOUCH FX panel over the beat-loop + hot-cue
 * rows: an X/Y pad with a crosshair on the left, and on the right a 2x4
 * FX grid, a DEPTH bar, FX ON and HOLD. The finger on the pad drives the
 * effect; lifting it turns the effect off unless HOLD is on.
 *
 * Audio: the effect itself runs on the DSP (tools/fx_dsp/fx.c, installed
 * live by the FX engine installer in place of real-time key shift). This
 * module only writes ONE tagged word to SH RAM 0x09D24DD8, which the
 * firmware pushes to the DSP mailbox 0x11837BD8 every frame (the store
 * 0x08D36B3A that would overwrite it is NOPed by the engine installer):
 *   0x80000000 | ON<<29 | effect<<26 | X<<18 | Y<<10 | DEPTH<<2
 * Effect off = 0x00100000 (untagged: the DSP passes audio through).
 *
 * Hooks (both CHAINED, previous values in tfx_prev_flush / tfx_prev_touch):
 *   flush  pool 0x08C92E20 (2DG flush) -> draw into the UI layer
 *   touch  pool 0x08D5278C (screen hit-tester, the pads' hook) -> consume
 *          touches on our button / panel, pass everything else on.
 * Gates: PERFORM screen only (mode word 7), MODS on, FEATB bit FB_FX clear.
 * The DSP effect needs MASTER TEMPO on (the only path the engine wraps):
 * with MT off the panel shows MT OFF and never arms the word.
 *
 * Redraw policy (heavy redraws are what bit the deck before): the whole
 * panel only when its state changed or a stock repaint overwrote it
 * (sample points read back), per UI layer (the deck alternates two);
 * a moving finger only erases + redraws the two crosshair lines + dot.
 */
typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;

#define R32(a) (*(volatile u32 *)(a))
#define R16(a) (*(volatile u16 *)(a))
#define R8(a)  (*(volatile u8 *)(a))
#define UNCACHED(p) (((u32)(p) & 0x1FFFFFFFu) | 0xA0000000u)

#define MODEW    0x0BA371D4u   /* 7 = PERFORM screen */
#define MTW      0x0B6C4308u   /* MASTER TEMPO 1 = on */
#define KEYWORD  0x09D24DD8u   /* -> DSP mailbox BD8 every frame */
#define NOPSITE  0x08D36B3Au   /* 0x0009 when the engine is installed */
#ifndef PST
#error "build with -DPST=... -DMSOFF=... (build.sh)"
#endif
#define FEATB    (PST + 55u)
#define PMODE    (PST + 2u)    /* pads mode: 0 HOT CUE, 1 BJ, 2 KS, 3 STEMS */
#define FB_FX    4u            /* FEATB bit: TOUCH FX hidden */
#define WORD_OFF 0x00100000u

#define RGB(r, g, b) (u16)((((r) >> 3) << 11) | (((g) >> 2) << 5) | ((b) >> 3))
#define C_BG    RGB(8, 8, 12)
#define C_PAD   RGB(16, 20, 30)
#define C_EDGE  RGB(78, 92, 110)
#define C_GRID  RGB(40, 48, 62)
#define C_BTN   RGB(38, 44, 54)
#define C_BTNE  RGB(70, 80, 96)
#define C_SEL   RGB(40, 90, 200)
#define C_SELE  RGB(90, 140, 240)
#define C_CROSS RGB(70, 120, 230)
#define C_DOT   RGB(240, 244, 250)
#define C_WH    RGB(235, 238, 242)
#define C_GREY  RGB(150, 158, 170)
#define C_DIM   RGB(80, 86, 96)
#define C_OR    RGB(240, 140, 30)
#define C_BAR0  RGB(30, 34, 44)

/* ---- geometry (screen px): the whole PERFORM area inside the purple
 * frame, below the PERFORM button / phase meter line, above the player
 * strip (user, 2026-11-01: full screen, keep the player strip) ---- */
#define AR_X0 12
#define AR_Y0 66
#define AR_X1 788             /* exclusive */
#define AR_Y1 311
#define TAB_X 16
#define TAB_W 92
/* Its own tab: in the KEY SHIFT tab's slot when that tab is hidden (key
 * shift and TOUCH FX share the DSP slot, so a deck has one or the other),
 * else a short tab under the stack (TAB_Y in tools/perform_pads.py). */
#define TAB_KS (R8(FEATB) & 1u)
#define TAB_Y (TAB_KS ? 264 : 297)
#define TAB_H (TAB_KS ? 15 : 12)
#define LX 18                 /* left column text */
#define PAD_X0 116
#define PAD_Y0 70
#define PAD_X1 521
#define PAD_Y1 309
#define RX 530                /* right column */
#define RW 256
#define GB_W 125
#define GB_H 24
#define GB_DX 131
#define GB_DY 28
#define GB_Y0 86
#define DEP_Y 214
#define DEP_H 11
#define BTN_Y 236
#define BTN_H 34
#define BTN_W 125

#include "tfx_glyphs.h"

/* ---- state (zeroed bss; the installer writes the cave fresh) ---- */
u32 tfx_mode;          /* 1 = panel open */
u32 tfx_eff;           /* 0 filter, 1 crush, 2 gate (3..7 not yet) */
u32 tfx_x = 128, tfx_y = 128, tfx_depth = 200;
u32 tfx_on = 1, tfx_hold;
u32 tfx_touching, tfx_drag;   /* drag: 1 pad 2 depth 3 tab 4 button */
u32 tfx_engine;        /* 1 = DSP FX engine installed (installer writes) */
u32 tfx_word;          /* last word written (0 = never) */
u32 tfx_active;        /* effect live now */
u32 tfx_ver = 1;       /* bumps on every visible state change */
u32 tfx_draws, tfx_full, tfx_touches;
u32 tfx_log[32], tfx_logn;   /* diagnostic: down<<31 | x<<16 | y per call */
static u32 lay_addr[2], lay_ver[2], lay_cx[2], lay_cy[2], lay_hasc[2];
static u16 lay_samp[2][16];

static const char *const fx_name[8] = {
    "FILTER", "CRUSH", "GATE", "ECHO", "PITCH", "FLANGER", "ROLL", "REVERB" };
static const char *const fx_x[3] = { "X: CUTOFF", "X: RATE", "X: SPEED" };
static const char *const fx_y[3] = { "Y: RESONANCE", "Y: BITS", "Y: DEPTH" };
#define N_LIVE 3

/* ---------------------------------------------------------- drawing ---- */
static u16 *L;            /* current layer (uncached) */
static int S;             /* stride in px */

static inline void put(int x, int y, u16 c) { L[y * S + x] = c; }
static void fill(int x, int y, int w, int h, u16 c)
{
    for (int j = 0; j < h; j++) {
        u16 *p = L + (y + j) * S + x;
        for (int i = 0; i < w; i++) p[i] = c;
    }
}
static void frame(int x, int y, int w, int h, u16 c)
{
    for (int i = 0; i < w; i++) { put(x + i, y, c); put(x + i, y + h - 1, c); }
    for (int j = 0; j < h; j++) { put(x, y + j, c); put(x + w - 1, y + j, c); }
}
static u16 mix(u16 fg, u16 bg, int a)          /* a 0..32 */
{
    u32 f = ((u32)fg | ((u32)fg << 16)) & 0x07E0F81Fu;
    u32 b = ((u32)bg | ((u32)bg << 16)) & 0x07E0F81Fu;
    u32 r = ((f * (u32)a + b * (u32)(32 - a)) >> 5) & 0x07E0F81Fu;
    return (u16)(r | (r >> 16));
}
/* three faces, all rendered through aafont.py (mkglyphs.py) so the
 * panel matches the deck's own lettering -- KEY SHIFT tabs/buttons,
 * SYNC/MASTER buttons, TRACK-style small labels */
typedef struct { int h, n, W; const char *cs; const unsigned char *w, *a; } tfont;
static const tfont F_CAPS  = { TG_H, TG_N, TG_W, TG_chars, TG_w,
                               (const unsigned char *)TG_a };
static const tfont F_SMALL = { TS_H, TS_N, TS_W, TS_chars, TS_w,
                               (const unsigned char *)TS_a };
static const tfont F_TAB   = { TT_H, TT_N, TT_W, TT_chars, TT_w,
                               (const unsigned char *)TT_a };

static int glyph(const tfont *f, char ch)
{
    for (int i = 0; i < f->n; i++) if (f->cs[i] == ch) return i;
    return -1;
}
static int text_w(const tfont *f, const char *s)
{
    int w = 0;
    for (; *s; s++) { int g = glyph(f, *s); w += g < 0 ? 4 : f->w[g] + 1; }
    return w ? w - 1 : 0;
}
/* text over a known flat background colour (top of caps at y) */
static void text(const tfont *f, int x, int y, const char *s, u16 fg, u16 bg)
{
    for (; *s; s++) {
        int g = glyph(f, *s);
        if (g < 0) { x += 4; continue; }
        for (int j = 0; j < f->h; j++)
            for (int i = 0; i < f->w[g]; i++) {
                int a = f->a[g * (f->h * f->W) + j * f->W + i];
                if (a) put(x + i, y + j, a >= 32 ? fg : mix(fg, bg, a));
            }
        x += f->w[g] + 1;
    }
}
static void ctext(const tfont *f, int x, int y, int w, int h, const char *s, u16 fg, u16 bg)
{
    text(f, x + (w - text_w(f, s)) / 2, y + (h - f->h) / 2, s, fg, bg);
}
static void num(char *p, u32 v)                /* "0".."100" + '%' */
{
    char t[4]; int n = 0;
    do { t[n++] = (char)('0' + v % 10); v /= 10; } while (v && n < 3);
    while (n) *p++ = t[--n];
    *p++ = '%'; *p = 0;
}
static void button(int x, int y, int w, int h, const char *s, int sel, int live)
{
    u16 bg = sel ? C_SEL : C_BTN;
    fill(x, y, w, h, bg);
    frame(x, y, w, h, sel ? C_SELE : C_BTNE);
    ctext(&F_CAPS, x, y, w, h, s, sel ? C_WH : (live ? C_GREY : C_DIM), bg);
}

/* pad background pixel (grid every 81 px x / 48 px y) */
static u16 pad_bg(int x, int y)
{
    if (x == PAD_X0 || x == PAD_X1 - 1 || y == PAD_Y0 || y == PAD_Y1 - 1) return C_EDGE;
    if ((x - PAD_X0) % 81 == 0 || (y - PAD_Y0) % 48 == 0) return C_GRID;
    return C_PAD;
}
static int pad_cx(void) { return PAD_X0 + 1 + (int)(tfx_x * (PAD_X1 - PAD_X0 - 3) / 255u); }
static int pad_cy(void) { return PAD_Y1 - 2 - (int)(tfx_y * (PAD_Y1 - PAD_Y0 - 3) / 255u); }
static int in_pad_label(int x, int y)          /* axis labels stay drawn */
{
    return (y >= PAD_Y1 - 15 && x < PAD_X0 + 100) || (y < PAD_Y0 + 15 && x >= PAD_X1 - 120);
}
static void pad_labels(void)
{
    int e = tfx_eff < N_LIVE ? (int)tfx_eff : 0;
    text(&F_SMALL, PAD_X0 + 6, PAD_Y1 - 14, fx_x[e], C_DIM, C_PAD);
    text(&F_SMALL, PAD_X1 - 6 - text_w(&F_SMALL, fx_y[e]), PAD_Y0 + 5, fx_y[e], C_DIM, C_PAD);
}
static void cross_line(int cx, int cy, int erase)
{
    for (int x = PAD_X0 + 1; x < PAD_X1 - 1; x++)
        if (!in_pad_label(x, cy)) put(x, cy, erase ? pad_bg(x, cy) : C_CROSS);
    for (int y = PAD_Y0 + 1; y < PAD_Y1 - 1; y++)
        if (!in_pad_label(cx, y)) put(cx, y, erase ? pad_bg(cx, y) : C_CROSS);
    for (int j = -4; j <= 4; j++)
        for (int i = -4; i <= 4; i++) {
            int x = cx + i, y = cy + j;
            if (i * i + j * j > 12 || x <= PAD_X0 || x >= PAD_X1 - 1 ||
                y <= PAD_Y0 || y >= PAD_Y1 - 1 || in_pad_label(x, y)) continue;
            put(x, y, erase ? pad_bg(x, y) : (tfx_active ? C_DOT : C_GREY));
        }
}

static const char *status(char *buf)
{
    if (!tfx_engine || R16(NOPSITE) != 0x0009u) return "FX ENGINE NOT INSTALLED";
    if (R8(MTW) != 1) return "MT OFF: TURN ON MASTER TEMPO";
    if (tfx_eff >= N_LIVE) return "COMING SOON";
    const char *n = fx_name[tfx_eff];
    char *p = buf;
    while (*n) *p++ = *n++;
    *p++ = ' ';
    if (!tfx_active) { const char *o = tfx_on ? "TOUCH PAD" : "FX OFF"; while (*o) *p++ = *o++; *p = 0; }
    else num(p, (tfx_depth * 100u + 127u) / 255u);
    return buf;
}

static void draw_tab(void)
{
    int sel = tfx_mode != 0;
    fill(TAB_X, TAB_Y, TAB_W, TAB_H, sel ? RGB(190, 190, 190) : RGB(41, 41, 41));
    frame(TAB_X, TAB_Y, TAB_W, TAB_H, RGB(74, 77, 74));
    ctext(&F_TAB, TAB_X, TAB_Y + 1, TAB_W, TAB_H - 1, "TOUCH FX", sel ? RGB(24, 28, 24) : RGB(140, 142, 140),
          sel ? RGB(190, 190, 190) : RGB(41, 41, 41));
}

static void pct(char *p, const char *lbl, u32 v)  /* "X 62%" */
{
    while (*lbl) *p++ = *lbl++;
    *p++ = ' ';
    num(p, (v * 100u + 127u) / 255u);
}

static void draw_panel(void)
{
    char buf[40];
    /* the whole area except the pads' tab column (x 14..111, y 230..):
     * the panel draws after the pads, and their tabs stay usable */
    fill(AR_X0, AR_Y0, AR_X1 - AR_X0, 230 - AR_Y0, C_BG);
    fill(AR_X0, 230, 14 - AR_X0, AR_Y1 - 230, C_BG);
    fill(112, 230, AR_X1 - 112, AR_Y1 - 230, C_BG);
    /* left column (above the tab stack) */
    text(&F_SMALL, LX, 74, "TOUCH FX", C_WH, C_BG);
    text(&F_SMALL, LX, 96, tfx_eff < N_LIVE ? fx_name[tfx_eff] : "-", tfx_active ? C_OR : C_GREY, C_BG);
    pct(buf, "X", tfx_x);     text(&F_SMALL, LX, 122, buf, C_GREY, C_BG);
    pct(buf, "Y", tfx_y);     text(&F_SMALL, LX, 138, buf, C_GREY, C_BG);
    pct(buf, "D", tfx_depth); text(&F_SMALL, LX, 154, buf, C_GREY, C_BG);
    text(&F_SMALL, LX, 182, tfx_active ? "LIVE" : "OFF", tfx_active ? C_OR : C_DIM, C_BG);
    /* pad */
    for (int y = PAD_Y0; y < PAD_Y1; y++)
        for (int x = PAD_X0; x < PAD_X1; x++) put(x, y, pad_bg(x, y));
    pad_labels();
    /* FX grid: 2 columns x 4 rows (full names) */
    text(&F_SMALL, RX, GB_Y0 - 14, "FX SELECT", C_DIM, C_BG);
    for (int i = 0; i < 8; i++) {
        int x = RX + (i & 1) * GB_DX, y = GB_Y0 + (i >> 1) * GB_DY;
        button(x, y, GB_W, GB_H, fx_name[i], (u32)i == tfx_eff, i < N_LIVE);
    }
    /* depth */
    num(buf, (tfx_depth * 100u + 127u) / 255u);
    text(&F_SMALL, RX, DEP_Y - 14, "DEPTH", C_DIM, C_BG);
    text(&F_SMALL, RX + RW - text_w(&F_SMALL, buf), DEP_Y - 14, buf, C_GREY, C_BG);
    fill(RX, DEP_Y, RW, DEP_H, C_BAR0);
    fill(RX, DEP_Y, (int)(tfx_depth * RW / 255u), DEP_H, C_SEL);
    /* FX ON / HOLD */
    button(RX, BTN_Y, BTN_W, BTN_H, "FX ON", tfx_on != 0, 1);
    button(RX + RW - BTN_W, BTN_Y, BTN_W, BTN_H, "HOLD", tfx_hold != 0, 1);
    /* status line */
    const char *st = status(buf);
    text(&F_SMALL, RX, BTN_Y + BTN_H + 8, st, tfx_active ? C_OR : C_GREY, C_BG);
    text(&F_SMALL, RX, BTN_Y + BTN_H + 24, "LIFT: OFF   HOLD: KEEP", C_DIM, C_BG);
}

/* sample points: panel pixels away from the crosshair + dynamic text */
static const u16 SPX[16] = { 14, 110, 60, 300, 518, 200, 400, 520,
                             600, 700, 784, 784, 600, 540, 700, 113 };
static const u16 SPY[16] = { 68, 68, 200, 72, 150, 306, 306, 300,
                             68, 98, 150, 230, 290, 226, 250, 300 };
static void sample(int k)
{
    for (int i = 0; i < 16; i++) lay_samp[k][i] = L[SPY[i] * S + SPX[i]];
}
static int intact(int k)
{
    int cx = lay_hasc[k] ? (int)lay_cx[k] : -9, cy = lay_hasc[k] ? (int)lay_cy[k] : -9;
    for (int i = 0; i < 16; i++) {
        int dx = SPX[i] - cx, dy = SPY[i] - cy;
        if (SPX[i] == cx || SPY[i] == cy || (dx * dx + dy * dy) <= 10) continue;
        if (L[SPY[i] * S + SPX[i]] != lay_samp[k][i]) return 0;
    }
    return 1;
}

/* ------------------------------------------------------------ word ---- */
static void set_word(void)
{
    int live = tfx_mode && tfx_engine && R16(NOPSITE) == 0x0009u && R8(MTW) == 1 &&
               tfx_eff < N_LIVE && tfx_on && (tfx_touching || tfx_hold) &&
               R32(MODEW) == 7u && !R8(MSOFF);
    u32 w = 0x80000000u | 1u << 29 | (tfx_eff & 7u) << 26 | (tfx_x & 0xFFu) << 18 |
            (tfx_y & 0xFFu) << 10 | (tfx_depth & 0xFFu) << 2;
    if ((u32)live != tfx_active) { tfx_active = (u32)live; tfx_ver++; }
    if (live) {
        R32(KEYWORD) = w;                  /* every flush: loads clear it */
        tfx_word = w;
    } else if (tfx_word != 0 && tfx_word != WORD_OFF) {
        R32(KEYWORD) = WORD_OFF;           /* once, on the way out */
        tfx_word = WORD_OFF;
    }
}

/* While the panel is open, the firmware's own draw commands that lie
 * wholly inside our area (stock pads, REV, QUANTIZE, the PERFORM waveform
 * strip quad in the present list, ...) become NOPs (0x08000000), so the
 * stock never paints over the panel and we almost never redraw. Same
 * technique and list walk as the phase meter (pm.c strip_stock). Anything
 * reaching outside the area (the screen-sized UI quad, the frame) stays. */
u32 tfx_stripped;
static int inside(int x0, int y0, int x1, int y1)
{
    return x0 >= AR_X0 && x1 <= AR_X1 && y0 >= AR_Y0 && y1 <= AR_Y1;
}
static void strip_stock(u32 req)
{
    u32 l = R32(req), end = R32(req - 4);
    if ((l >> 29) != 5 || end <= l || end - l > 0x1000 || ((end - l) & 3))
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
        int x0 = 0x7FFF, y0 = 0x7FFF, x1 = -1, y1 = -1, any = 0;
        if (op == 0x82 || op == 0x80) {            /* quad / flat polygon */
            u32 k0 = op == 0x82 ? 4 : 2;
            for (u32 k = k0; k < k0 + 4; k++) {
                int x = (short)(w[i + k] >> 16), y = (short)(w[i + k] & 0xFFFF);
                if (x < x0) x0 = x;
                if (x > x1) x1 = x;
                if (y < y0) y0 = y;
                if (y > y1) y1 = y;
            }
            any = 1;
        } else if (op == 0xA0 || op == 0xA2) {      /* fill / copy */
            int ax = (short)(w[i + 5] >> 16), ay = (short)(w[i + 5] & 0xFFFF);
            x0 = ax - (short)(w[i + 3] >> 16); x1 = ax + (short)(w[i + 3] & 0xFFFF);
            y0 = ay - (short)(w[i + 4] >> 16); y1 = ay + (short)(w[i + 4] & 0xFFFF);
            any = 1;
        } else if (op == 0xB0) {                   /* polyline */
            u32 np = w[i + 1] & 0xFFFF;
            for (u32 k = 0; k < np; k++) {
                int x = (short)(w[i + 3 + k] >> 16), y = (short)(w[i + 3 + k] & 0xFFFF);
                if (x < x0) x0 = x;
                if (x > x1) x1 = x;
                if (y < y0) y0 = y;
                if (y > y1) y1 = y;
            }
            any = np != 0;
        }
        if (any && inside(x0, y0, x1, y1)) {
            for (u32 k = 0; k < len; k++) w[i + k] = 0x08000000u;
            tfx_stripped++;
        }
        i += len;
    }
}

/* ----------------------------------------------------------- hooks ---- */
void tfx_flush(u32 dev, u32 req)
{
    (void)dev;
    set_word();
    if (R32(MODEW) != 7u || R8(MSOFF) || (R8(FEATB) & FB_FX)) return;
    const u32 *w = (const u32 *)R32(req);
    int present = w && w[13] == 0x4C && w[16] == 0x58 &&
                  (w[27] >> 24) == 0x82 && w[29] == 0x032001E0u;
    /* only lists drawing into the 800-px UI layer, and the present list
     * (screen coordinates); other layers have their own coordinates */
    if (tfx_mode && (present || R32(req + 32) == 1600u))
        strip_stock(req);
    /* the PERFORM waveform strip is presented by its own request with the
     * same list layout (quad 504x72, top-left 116,64 -- perform_pads.py):
     * while the panel is open its quad becomes NOPs */
    if (tfx_mode && w && !present && w[13] == 0x4C && w[16] == 0x58 &&
        (w[27] >> 24) == 0x82 && w[29] == 0x01F80048u && w[31] == 0x00740040u &&
        (((u32)w) >> 29) == 5) {
        volatile u32 *q = (volatile u32 *)&w[27];
        for (int k = 0; k < 8; k++) q[k] = 0x08000000u;
        tfx_stripped++;
    }
    if (!present) return;
    int stride = (int)w[17];
    if (stride < 800 || stride > 1024) return;
    L = (u16 *)UNCACHED(w[14]);
    S = stride;
    tfx_draws++;
    if (!tfx_mode) { draw_tab(); return; }
    int k = lay_addr[0] == w[14] ? 0 : lay_addr[1] == w[14] ? 1 : -1;
    if (k < 0) {                              /* a layer we haven't drawn */
        k = lay_addr[0] == 0 ? 0 : lay_addr[1] == 0 ? 1 : (int)(tfx_full & 1);
        lay_addr[k] = w[14]; lay_ver[k] = 0; lay_hasc[k] = 0;
    }
    int cx = pad_cx(), cy = pad_cy();
    if (lay_ver[k] != tfx_ver || !intact(k)) {
        draw_panel();
        sample(k);
        lay_ver[k] = tfx_ver;
        lay_hasc[k] = 0;
        tfx_full++;
    }
    draw_tab();                               /* on top of the panel fill */
    int want = tfx_touching || tfx_hold;
    if (lay_hasc[k] && (!want || (int)lay_cx[k] != cx || (int)lay_cy[k] != cy)) {
        cross_line((int)lay_cx[k], (int)lay_cy[k], 1);
        lay_hasc[k] = 0;
    }
    if (want && !lay_hasc[k]) {
        cross_line(cx, cy, 0);
        lay_cx[k] = (u32)cx; lay_cy[k] = (u32)cy; lay_hasc[k] = 1;
    }
}

static int inr(int x, int y, int x0, int y0, int w, int h)
{
    return x >= x0 && x < x0 + w && y >= y0 && y < y0 + h;
}
static u32 clamp255(int v) { return v < 0 ? 0u : v > 255 ? 255u : (u32)v; }

/* 1 = consumed (the hit-tester and the pads never see it) */
static int touch(int down, int x, int y);
int tfx_touch(int down, int x, int y)
{
    tfx_log[tfx_logn++ & 31] = (u32)(down != 0) << 31 | (u32)(x & 0x7FFF) << 16 | (u32)(y & 0xFFFF);
    int r = touch(down, x, y);
    set_word();                               /* the word follows the finger */
    return r;
}
static int touch(int down, int x, int y)
{
    if (R32(MODEW) != 7u || R8(MSOFF) || (R8(FEATB) & FB_FX)) {
        tfx_drag = 0; tfx_touching = 0;
        return 0;
    }
    if (!down) {                              /* release: let the pads see it too */
        if (tfx_touching) tfx_ver++;
        tfx_drag = 0; tfx_touching = 0;
        return 0;
    }
    tfx_touches++;
    int fresh = tfx_drag == 0;
    if (tfx_drag == 3 || tfx_drag == 4) return 1;        /* held on a button */
    if (fresh && inr(x, y, TAB_X, TAB_Y - 1, TAB_W, TAB_H + 3)) {
        tfx_mode = !tfx_mode;
        if (tfx_mode) R8(PMODE) = 0;          /* pads: HOT CUE row = stock */
        tfx_drag = 3; tfx_ver++;
        return 1;
    }
    if (!tfx_mode) return 0;
    if (fresh && x < PAD_X0 - 4 && y >= 230 && y < 297) {    /* a pads tab: leave */
        tfx_mode = 0; tfx_touching = 0; tfx_ver++;
        return 0;
    }
    if (tfx_drag == 1 || (fresh && inr(x, y, PAD_X0, PAD_Y0, PAD_X1 - PAD_X0, PAD_Y1 - PAD_Y0))) {
        tfx_x = clamp255((x - PAD_X0 - 1) * 255 / (PAD_X1 - PAD_X0 - 3));
        tfx_y = clamp255((PAD_Y1 - 2 - y) * 255 / (PAD_Y1 - PAD_Y0 - 3));
        if (!tfx_touching) tfx_ver++;
        tfx_touching = 1; tfx_drag = 1;
        return 1;
    }
    if (tfx_drag == 2 || (fresh && inr(x, y, RX, DEP_Y - 6, RW, DEP_H + 12))) {
        u32 d = clamp255((x - RX) * 255 / RW);
        if (d != tfx_depth) { tfx_depth = d; tfx_ver++; }
        tfx_drag = 2;
        return 1;
    }
    if (!fresh) return 1;
    for (int i = 0; i < 8; i++) {
        int bx = RX + (i & 1) * GB_DX, by = GB_Y0 + (i >> 1) * GB_DY;
        if (inr(x, y, bx, by, GB_W, GB_H)) {
            if (i < N_LIVE && (u32)i != tfx_eff) { tfx_eff = (u32)i; tfx_ver++; }
            tfx_drag = 4;
            return 1;
        }
    }
    if (inr(x, y, RX, BTN_Y, BTN_W, BTN_H)) { tfx_on = !tfx_on; tfx_drag = 4; tfx_ver++; return 1; }
    if (inr(x, y, RX + RW - BTN_W, BTN_Y, BTN_W, BTN_H)) { tfx_hold = !tfx_hold; tfx_drag = 4; tfx_ver++; return 1; }
    if (inr(x, y, AR_X0, AR_Y0, AR_X1 - AR_X0, AR_Y1 - AR_Y0)) { tfx_drag = 4; return 1; }
    return 0;
}
