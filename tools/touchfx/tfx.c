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
u32 tfx_eff;           /* 0 filter 1 crush 2 gate 3 echo 4 flanger (5..7 not yet) */
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
/* 2026-11-10 (Reddit, 1.0.5 on a deck: "fast swipes or switching effects
 * fast: first the sound goes, then the screen freezes, sometimes it comes
 * back"): every visible change repainted the WHOLE panel (190,000 pixels
 * through the uncached layer, twice -- the deck flips between two layers),
 * and one tap is three changes (finger down, effect, finger up); a DEPTH
 * drag is one per frame. That kept the display task busy for long stretches
 * and everything below it waited. A layer is now repainted at most once per
 * REDRAW_MS; a change inside that window is drawn by the next flush after
 * it (the crosshair is separate and still follows every frame). */
#define MSCLK     0x0B704A80u   /* firmware 1 ms clock (pm.c, sm.c) */
#ifndef REDRAW_MS
#define REDRAW_MS 60u
#endif
static u32 lay_t[2];
u32 tfx_defer;                  /* repaints put off by the limit */

/* Index = the effect id the DSP engine uses (tools/fx_dsp/fx.c), NOT the
 * button order: 4 is the flanger (it shares echo's delay line) and 5 is
 * pitch, so the two grid labels must follow the ids. */
static const char *const fx_name[8] = {
    "FILTER", "CRUSH", "GATE", "ECHO", "FLANGER", "PITCH", "ROLL", "REVERB" };
static const char *const fx_x[8] = { "X: CUTOFF", "X: RATE", "X: SPEED", "X: TIME",
                                    "X: RATE", "X: SEMITONES", "X: LOOP 1/2-4",
                                    "X: DECAY" };
static const char *const fx_y[8] = { "Y: RESONANCE", "Y: BITS", "Y: DEPTH", "Y: FEEDBACK",
                                    "Y: DEPTH", "Y: -", "Y: -", "Y: DEPTH" };
/* What is actually live in this build: FILTER, CRUSH, GATE. ECHO (3) and
 * FLANGER (4) need a delay line the deck does not have -- the 52 KB block
 * came back in use with audio playing (docs/TOUCHFX_MEMORY_SURVEY.md).
 * PITCH (5) needs no buffer: it drives the deck's own stretcher, the way the
 * key-shift engine does -- and it is written, but it does not fit the FX
 * cave at first (2,912 B built vs the 2,496 B wall); compacted 2026-11-08
 * to 2,480 B, so the shipping build sets FX_PITCH=1 on BOTH sides (this
 * panel and tools/fx_dsp/build.sh) and offers it. */
#ifndef FX_PITCH
#define FX_PITCH 0
#endif
#ifndef FX_ROLL
#define FX_ROLL 0
#endif
#ifndef FX_REVERB
#define FX_REVERB 0
#endif
/* FILTER/CRUSH/GATE always; ROLL and REVERB only in a build whose DSP engine
 * carries them (tools/fx_dsp/build.sh FX_ROLL=1 FX_REVERB=1, which also needs
 * the state block moved out of the code window). The grid label ids must
 * match fx.c: 6 = Roll, 7 = Reverb. */
/* ROLL without a DSP build (FX_ROLL=0) is the deck's OWN loop, driven from
 * this panel (2026-11-12): no audio-chip memory at all. Finger down on the
 * pad = a touch forwarded to the stock BEAT LOOP row (zone 22, x 116..616,
 * y 154..207; the handler 0x08DD08F2 picks the size from x: 1/2 1 2 4 8 16)
 * at the size chosen by X (1/2, 1, 2, 4 beats), moving it as the finger
 * moves; finger up = release, then RELOOP/EXIT (key bit KEYB+86 0x10) ends
 * the loop and play carries on. With SLIP on (0x0B6C3E4C == 1) the deck's
 * beat loop is momentary and ends by itself, so no RELOOP/EXIT is sent (it
 * would re-arm the loop). Emulator, real audio chip: held 1/2-beat pad =
 * a 0.21 s loop in the DAC output, RELOOP/EXIT = plays on. */
#define LOOPROLL(i) ((i) == 6 && !FX_ROLL)
#define FX_LIVE(i) ((i) < 3 || ((i) == 5 && FX_PITCH) || \
                    ((i) == 6) || ((i) == 7 && FX_REVERB))
#define KEYB     0x0B6C7A28u   /* key bitmap */
#define SLIPW    0x0B6C3E4Cu   /* SLIP 1 = on */
#define BL_X0    116           /* stock BEAT LOOP row */
#define BL_STEP  83
#define BL_Y     180
u32 tfx_fwd[3];                /* rewritten touch for the hit-tester: down, x, y */
u32 tfx_roll;                  /* 1 = a roll is held */
u32 tfx_rexit;                 /* 0 idle, else parser frames of RELOOP/EXIT pressed */
u32 tfx_rolls;                 /* diagnostic: rolls started */
u32 tfx_mtpulse;               /* parser frames left pressing MASTER TEMPO */
u32 tfx_mtauto;                /* diagnostic: times MT was switched on for FX */
static u32 tfx_tabx;           /* where a press on our tab landed (0x7FFF = moved) */
static u32 roll_size(void) { return tfx_x * 4u / 256u; }   /* 0..3 = 1/2 1 2 4 */

/* ---------------------------------------------------------- drawing ---- */
static u16 *L;            /* current layer (uncached) */
static int S;             /* stride in px */

/* 2026-11-13 (deck): the panel stayed dark under THEME > LIGHT. While the
 * light add-on says it is active (lt_active, tools/lighttext; zero when it is
 * not installed), neutral colours flip like the stock screen's fills
 * (lt.c flip565: v = 255 - 7/8 max); real colours (accent blue, orange)
 * stay. Computed once per fill and per text pixel. */
static int light;                               /* this flush */
static u16 lc(u16 c)
{
    u32 r = ((c >> 11) & 31) * 255 / 31, g = ((c >> 5) & 63) * 255 / 63, b = (c & 31) * 255 / 31;
    u32 mx = r > g ? (r > b ? r : b) : (g > b ? g : b);
    u32 mn = r < g ? (r < b ? r : b) : (g < b ? g : b);
    if (mx - mn > 48) return c;
    u32 v = 255 - (mx * 7 >> 3);
    return (u16)((v >> 3) << 11 | (v >> 2) << 5 | v >> 3);
}
static inline void put(int x, int y, u16 c) { L[y * S + x] = light ? lc(c) : c; }
static void fill(int x, int y, int w, int h, u16 c)
{
    if (light) c = lc(c);
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
    /* LIGHT: text on a COLOURED button (blue selection) keeps its colour --
     * only text on a flipped (neutral) background flips with it */
    u16 b2 = light ? lc(bg) : bg, f2 = (!light || b2 == bg) ? fg : lc(fg);
    int keep = light;
    light = 0;                                  /* colours already final */
    fg = f2; bg = b2;
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
    light = keep;
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
    int e = FX_LIVE(tfx_eff) ? (int)tfx_eff : 0;
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
    if (LOOPROLL(tfx_eff)) {
        static const char *const sz[4] = { "ROLL 1/2 BEAT", "ROLL 1 BEAT", "ROLL 2 BEATS", "ROLL 4 BEATS" };
        return tfx_on ? sz[roll_size()] : "FX OFF";
    }
    if (!tfx_engine || R16(NOPSITE) != 0x0009u) return "FX ENGINE NOT INSTALLED";
    if (R8(MTW) != 1) return "MT OFF: TURN ON MASTER TEMPO";
    if (!FX_LIVE(tfx_eff)) return "COMING SOON";
    const char *n = fx_name[tfx_eff];
    char *p = buf;
    while (*n) *p++ = *n++;
    *p++ = ' ';
    if (!tfx_active) { const char *o = tfx_on ? "TOUCH PAD" : "FX OFF"; while (*o) *p++ = *o++; *p = 0; }
    else num(p, (tfx_depth * 100u + 127u) / 255u);
    return buf;
}

/* 2026-11-17 (deck photo): with KEY SHIFT hidden the tab stack ends at 295
 * and the firmware's USB source button (18,238 72x81, with its green access
 * light) shows in the gap under STEMS and across the panel's bottom border.
 * The emulator never draws it (no stick activity there). Repaint the corner
 * on every present while the panel is open: panel background inside, and
 * below our edge a copy of the same rows 128 px to the right (stock frame:
 * border line + black), so it matches whatever the firmware drew there. */
static void corner_patch(void)
{
    int y0 = TAB_KS ? 296 : 309;
    if (y0 < AR_Y1 - 1)                       /* tab column background */
        fill(AR_X0, y0, 116 - AR_X0, AR_Y1 - 1 - y0, RGB(0, 0, 0));
    for (int y = AR_Y1 - 1; y < 320; y++) {   /* border row 310 and below */
        u16 *r = L + y * S;
        for (int x = AR_X0; x < 116; x++) r[x] = r[x + 128];
    }
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
    text(&F_SMALL, LX, 96, FX_LIVE(tfx_eff) ? fx_name[tfx_eff] : "-", tfx_active ? C_OR : C_GREY, C_BG);
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
        button(x, y, GB_W, GB_H, fx_name[i], (u32)i == tfx_eff, FX_LIVE(i));
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
u32 tfx_why;                    /* last repaint reason: 0x100 = state changed, n = sample point n-1 damaged */
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
        if (L[SPY[i] * S + SPX[i]] != lay_samp[k][i]) { tfx_why = (u32)i + 1u; return 0; }
    }
    return 1;
}

/* ------------------------------------------------------------ word ---- */
static void set_word(void)
{
    int live = tfx_mode && tfx_engine && R16(NOPSITE) == 0x0009u && R8(MTW) == 1 &&
               FX_LIVE(tfx_eff) && !LOOPROLL(tfx_eff) && tfx_on && (tfx_touching || tfx_hold) &&
               R32(MODEW) == 7u && !R8(MSOFF);
    u32 w = 0x80000000u | 1u << 29 | (tfx_eff & 7u) << 26 | (tfx_x & 0xFFu) << 18 |
            (tfx_y & 0xFFu) << 10 | (tfx_depth & 0xFFu) << 2;
    int shown = live || tfx_roll;
    if ((u32)shown != tfx_active) { tfx_active = (u32)shown; tfx_ver++; }
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
u32 tfx_stripped, tfx_cropped;
static int inside(int x0, int y0, int x1, int y1)
{
    /* y1 may reach the pane's bottom edge (319): the firmware's USB source
     * button (18,238 72x81) hangs 8 px below the panel. On PERFORM it is
     * normally erased by the pane-wide fill we now cut short. */
    return x0 >= AR_X0 && x1 <= AR_X1 && y0 >= AR_Y0 && y0 < AR_Y1 && y1 <= 320;
}
static void strip_stock(u32 req)
{
    u32 l = R32(req), end = R32(req - 4);
    if ((l >> 29) != 5 || end <= l || end - l > 0x1000 || ((end - l) & 3))
        return;
    volatile u32 *w = (volatile u32 *)l;
    u32 n = (end - l) >> 2, i = 0;
    /* pass 0 finds the pane-wide fill (if any), pass 1 edits the list */
    u32 fi = ~0u;
    int fx0 = 0, fy0 = 0, fx1 = 0, fy1 = 0;
  for (int pass = 0; pass < 2; pass++) {
    i = 0;
    while (i < n) {
        u32 op = w[i] >> 24, len;
        switch (op) {
        case 0x18: len = 3; break;
        case 0x40: case 0x48: len = 2; break;
        case 0x08: case 0x12: len = 1; break;
        case 0xA0: case 0xA2: case 0x80: len = 6; break;
        case 0x82: len = 8; break;
        case 0xB0: len = 3 + (i + 1 < n ? (w[i + 1] & 0xFFFF) : 0); break;
        default: i = n; continue;                  /* 0x00 end / unknown: this pass is over */
        }
        if (i + len > n) break;
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
        if (pass == 0) {
            if (fi == ~0u && op == 0xA0 && x0 <= AR_X0 && x1 >= AR_X1 &&
                y0 <= AR_Y0 && y1 >= AR_Y1 && !inside(x0, y0, x1, y1)) {
                fi = i; fx0 = x0; fy0 = y0; fx1 = x1; fy1 = y1;
            }
            i += len;
            continue;
        }
        if (any && i < fi && fi != ~0u && y1 > AR_Y0 &&
            x0 >= fx0 && x1 <= fx1 && y0 >= fy0 && y1 <= fy1) {
            /* drawn BEFORE the pane-wide fill and under it: the firmware
             * meant it to be erased (its USB source button sits there on
             * PERFORM). The fill no longer reaches it, so drop it. */
            for (u32 k = 0; k < len; k++) w[i + k] = 0x08000000u;
            tfx_stripped++;
        } else
        if (any && inside(x0, y0, x1, y1)) {
            for (u32 k = 0; k < len; k++) w[i + k] = 0x08000000u;
            tfx_stripped++;
        } else if (op == 0xA0 && x0 <= AR_X0 && x1 >= AR_X1 &&
                   y0 <= AR_Y0 && y1 >= AR_Y1) {
            /* 2026-11-10: with a track PLAYING the firmware rebuilds the
             * whole upper pane every frame, starting with one fill that
             * covers it all (emulator: 4,4 796x315). It is bigger than the
             * panel, so it was left alone, wiped the panel, and the panel
             * was repainted in full on EVERY frame for as long as it was
             * open. The fill is cut down to the part above the panel; the
             * slivers left and right of it hold the frame art the firmware
             * blits again right after. */
            if (y0 >= AR_Y0) {
                for (u32 k = 0; k < len; k++) w[i + k] = 0x08000000u;
            } else {
                w[i + 4] = (u32)(AR_Y0 - y0);                    /* up 0, down to our top edge */
                w[i + 5] = (w[i + 5] & 0xFFFF0000u) | ((u32)y0 & 0xFFFFu);
            }
            tfx_cropped++;
        }
        i += len;
    }
  }
}

/* ----------------------------------------------------------- hooks ---- */
void tfx_flush(u32 dev, u32 req)
{
    (void)dev;
    set_word();
#ifdef LT_ACTIVE
    {
        int l = R32(LT_ACTIVE) != 0;
        if (l != light) { light = l; tfx_ver++; }   /* theme switched: redraw */
    }
#endif
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
    int dmg = !intact(k);                     /* stock painted over us: never wait */
    if (lay_ver[k] != tfx_ver) tfx_why = 0x100u;
    if (lay_ver[k] != tfx_ver || dmg) {
        u32 now = R32(MSCLK);
        if (dmg || lay_ver[k] == 0 || now - lay_t[k] >= REDRAW_MS) {
            draw_panel();
            sample(k);
            lay_ver[k] = tfx_ver;
            lay_hasc[k] = 0;
            lay_t[k] = now;
            tfx_full++;
        } else
            tfx_defer++;
    }
    corner_patch();
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
    /* RELOOP/EXIT after a roll. This hook runs inside the panel parser
     * (0x8D5263A), after it has rebuilt the key bytes from the buttons and
     * before the snapshot copy (0x8D4A7A2) that the RELOOP/EXIT handler
     * 0x08D4632C edge-detects (current 0x0B6C40DA vs previous 0x0B6C4106,
     * bit 0x10; loop active -> exit 0x08D3D98A). Setting the bit here for a
     * few parser frames is one clean press; from the flush hook it was
     * cleared by the parser before the snapshot ever saw it. */
    if (tfx_rexit) {
        R8(KEYB + 86) = (u8)(R8(KEYB + 86) | 0x10u);
        if (++tfx_rexit > 4) tfx_rexit = 0;
    }
    /* 2026-11-12 (deck): the effects only work with MASTER TEMPO on, and the
     * user tested with it off -- the panel never armed (tfx_word 0 all
     * session). Opening the panel now presses MASTER TEMPO (key byte 85, bit
     * 0x04, a toggle; emulator-measured) when it is off, the same way ROLL
     * presses RELOOP/EXIT. */
    if (tfx_mtpulse) {
        R8(KEYB + 85) = (u8)(R8(KEYB + 85) | 0x04u);
        if (++tfx_mtpulse > 4) tfx_mtpulse = 0;
    }
    tfx_log[tfx_logn++ & 31] = (u32)(down != 0) << 31 | (u32)(x & 0x7FFF) << 16 | (u32)(y & 0xFFFF);
    int r = touch(down, x, y);
    set_word();                               /* the word follows the finger */
    return r;
}
static int touch(int down, int x, int y)
{
    if (tfx_roll && (!down || R32(MODEW) != 7u || R8(MSOFF))) {
        tfx_roll = 0;                         /* roll ends: release the loop pad */
        if (R32(SLIPW) != 1u) tfx_rexit = 1;  /* then RELOOP/EXIT (not in SLIP) */
        tfx_ver++;
    }
    if (R32(MODEW) != 7u || R8(MSOFF) || (R8(FEATB) & FB_FX)) {
        tfx_drag = 0; tfx_touching = 0;
        return 0;
    }
    if (!down) {                              /* release: let the pads see it too */
        if (tfx_drag == 3 && tfx_tabx != 0x7FFF) {      /* a clean tap on our tab */
            tfx_mode = !tfx_mode;
            if (tfx_mode) R8(PMODE) = 0;      /* pads: HOT CUE row = stock */
            if (tfx_mode && R8(MTW) != 1 && !tfx_mtpulse) { tfx_mtpulse = 1; tfx_mtauto++; }
            tfx_ver++;
        }
        if (tfx_touching) tfx_ver++;
        tfx_drag = 0; tfx_touching = 0;
        return 0;
    }
    tfx_touches++;
    int fresh = tfx_drag == 0;
    if (tfx_drag == 4) return 1;                         /* held on a button */
    /* 2026-11-13 (deck): a swipe across the hot cue pads that slid ONTO our
     * tab opened TOUCH FX -- the finger was "fresh" to us the first time we
     * looked at it. A touch that starts anywhere else (panel closed) is
     * marked not-ours (5) until the finger lifts. */
    if (tfx_drag == 5) return 0;
    /* 2026-11-12 (deck): a swipe along the HOT CUE tab opened TOUCH FX when
     * the finger strayed onto our tab. The tab now acts on a clean TAP only:
     * pressed on the tab, lifted within 12 px of where it landed. */
    if (fresh && inr(x, y, TAB_X, TAB_Y - 1, TAB_W, TAB_H + 3)) {
        tfx_tabx = (u32)x;
        tfx_drag = 3;
        return 1;
    }
    if (tfx_drag == 3) {
        int dx = x - (int)tfx_tabx;
        if (dx > 12 || dx < -12) tfx_tabx = 0x7FFF;   /* moved: not a tap */
        return 1;
    }
    if (fresh && !tfx_mode) { tfx_drag = 5; return 0; }   /* started elsewhere */
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
        if (LOOPROLL(tfx_eff) && tfx_on && !tfx_rexit) {   /* the deck's own loop */
            u32 k = roll_size();
            if (!tfx_roll) { tfx_roll = 1; tfx_rolls++; tfx_ver++; }
            tfx_fwd[0] = 1;
            tfx_fwd[1] = BL_X0 + k * BL_STEP + BL_STEP / 2;
            tfx_fwd[2] = BL_Y;
            return 2;                         /* forward the rewritten touch */
        }
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
            if (FX_LIVE(i) && (u32)i != tfx_eff) { tfx_eff = (u32)i; tfx_ver++; }
            tfx_drag = 4;
            return 1;
        }
    }
    if (inr(x, y, RX, BTN_Y, BTN_W, BTN_H)) { tfx_on = !tfx_on; tfx_drag = 4; tfx_ver++; return 1; }
    if (inr(x, y, RX + RW - BTN_W, BTN_Y, BTN_W, BTN_H)) { tfx_hold = !tfx_hold; tfx_drag = 4; tfx_ver++; return 1; }
    if (inr(x, y, AR_X0, AR_Y0, AR_X1 - AR_X0, AR_Y1 - AR_Y0)) { tfx_drag = 4; return 1; }
    return 0;
}
