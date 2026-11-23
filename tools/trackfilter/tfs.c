/* tfs.c -- the XDJ-1000MK2's TRACK FILTER screen and button, on the 700.
 *
 * The 1000MK2 has a TRACK FILTER button: a tap switches the filter on/off,
 * a hold opens the condition screen. The 700 has no such button, so the
 * same control sits where the 1000MK2 shows the filter's funnel and [count]:
 * the right end of the browse list header (x 600..782, y 76..103).
 *   tap  = filter on/off (the list is redone at once, like the 1000MK2)
 *   hold = filter on + the TRACK FILTER screen (layout and behaviour of the 1000MK2's,
 *          measured on its firmware in the reference emulator):
 *          title "TRACK FILTER  USB@PLAYER1" + funnel + [live count],
 *          tabs BPM KEY / MY TAG, hold RESET, rows BPM (value <= +- %),
 *          KEY (RELATED KEY), RATING (stars), COLOR (8, any of), each with
 *          its tick box; BPM / % keypads and the key picker over the
 *          player strip; MASTER PLAYER.
 * Leave the screen with BACK, BROWSE, or a tap on the title's funnel.
 *
 * Drawing: into the UI layer at present time, like the TOUCH FX panel
 * (tools/touchfx/tfx.c); while the screen is open every stock draw command
 * that lies wholly inside it becomes a NOP, so the browse screen does not
 * paint over it. Conditions: tf.c's setters (the deck's own condition
 * store; saved on the stick as RBFLTR.DAT by the deck's own task).
 */
typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;
#define R32(a) (*(volatile u32 *)(a))
#define R16(a) (*(volatile u16 *)(a))
#define R8(a)  (*(volatile u8 *)(a))
#define UNCACHED(p) (((u32)(p) & 0x1FFFFFFFu) | 0xA0000000u)

#include "tfs_glyphs.h"

#define MODEW  0x0BA371D4u     /* 2 = browse */
#define KEYB   0x0B6C7A28u     /* key bitmap (parser) */
#define MSCLK  0x0B704A80u     /* 1 ms clock */
#define K_BACK 0x80u           /* KEYB + 102 */
#define K_PUSH 0x20u           /* KEYB + 102 */

extern struct { u8 on; u8 pad[7]; } tf_cfg;
extern u32 tf_na, tf_naver, tf_tracks, tf_last_kept, tf_left;
u32 tf_val(int k, int i);
int tf_tick(int k);
int tf_vn(int k);
u32 tf_bpm(void), tf_pct(void), tf_key(void);
int tf_setb(int tick, u32 bpm, u32 pct);
int tf_setk(int tick, u32 e);
int tf_setr(int tick, u32 stars);
int tf_setc(int tick, u32 mask);
int tf_count(void);
/* My Tag (tf.c) */
#define MT_MAX 32
#define MT_NAME 24
#define MT_SEL 8
struct mtag { u32 id; u8 col, pos, pad[2]; char name[MT_NAME]; };
extern struct mtag tf_mt[MT_MAX];
extern u32 tf_mtn;
struct mcol { u32 id, pos; char name[MT_NAME]; };
extern struct mcol tf_mc[4];
extern u32 tf_mcn;
struct mcond { u8 tick, orr, n, pad; u32 id[MT_SEL]; };
extern struct mcond tf_mcond[4];
#define ROTC 0x0B6C7B48u       /* browse knob counter (u16) */
#define TRKBPM 0x09D24692u     /* loaded track BPM x 100 (u16) */
#define PITCHW 0x0B6C3ED0u     /* tempo, s16 in 0.01 % */
#define TRKKEY 0x09D30430u     /* loaded track key as shown (UTF-16) */
#define MODEW_DECK 0x0B6C4264u /* deck mode: 0 = no track */

#define RGB(r, g, b) (u16)((((r) >> 3) << 11) | (((g) >> 2) << 5) | ((b) >> 3))
#define C_BLACK  0
#define C_WH     RGB(255, 255, 255)
#define C_TITLE  RGB(0, 28, 131)
#define C_BTN0   RGB(41, 60, 82)
#define C_BTN1   RGB(32, 48, 74)
#define C_BTNT   RGB(65, 80, 106)
#define C_BTNB   RGB(57, 76, 98)
#define C_TABS   RGB(32, 52, 82)
#define C_TABN   RGB(32, 48, 65)
#define C_STRIPE RGB(190, 200, 214)
#define C_PANEL  RGB(24, 28, 32)
#define C_PEDGE  RGB(41, 48, 57)
#define C_SEL    RGB(156, 170, 172)
#define C_DIM    RGB(70, 80, 92)
#define C_GREY   RGB(150, 158, 170)
#define C_HDR    RGB(16, 49, 90)
static const u16 COLS[8] = {
    RGB(255, 72, 172), RGB(255, 0, 0), RGB(255, 133, 0), RGB(255, 226, 41),
    RGB(0, 230, 0), RGB(0, 198, 255), RGB(0, 80, 255), RGB(156, 8, 255)};

/* screen geometry (the 1000MK2's) */
#define ROW_Y(i)  (67 + 64 * (i))
#define ROW_H     36
#define CHK_X     113
#define CHK_W     109
#define BOX_X     255
#define BOX_W     125
#define PCT_X     519
#define HDR_X0    600
#define HDR_Y0    76
#define HDR_Y1    103

u32 tfs_mode;            /* 1 = screen open */
u32 tfs_tab;             /* 0 BPM KEY, 1 MY TAG */
u32 tfs_edit;            /* 0 none, 1 BPM keypad, 2 % keypad, 3 key picker */
u32 tfs_minor;           /* key picker: 1 = minor row */
u32 tfs_col;             /* MY TAG: column tab shown */
u32 tfs_scroll;          /* MY TAG: first grid row (2 tags per row) */
u32 tfs_info;            /* MY TAG: the "i" overview */
static u32 lastrot;                   /* set when the screen opens (keys) */
u32 tfs_ver = 1;         /* bumps on every visible change */
u32 tfs_relist;          /* redo the list: 1 BACK, 2 wait, 3 push, 4 wait for it */
static u32 rl_n, rl_t;   /* frames / ms clock in this step */
u32 tfs_draws, tfs_full, tfs_touches, tfs_holds, tfs_taps, tfs_relists, tfs_masters;
static u32 ebuf, elen;   /* BPM keypad entry */
static u32 tdown, tx0, ty0, tfired, tzone;     /* a press in progress */
static u32 lastkeys, lastcount = 0xFFFFFFFFu, lastnaver;
static u32 lay_addr[2], lay_ver[2];
static int rotacc;                    /* knob steps taken from the browse */

/* ------------------------------------------------------------ pixels ---- */
static u16 *L;
static int S;
static int clipx = 800;                    /* text is cut at this x */
static inline void put(int x, int y, u16 c)
{
    if ((unsigned)x < 800u && (unsigned)y < 480u) L[y * S + x] = c;
}
static void fill(int x, int y, int w, int h, u16 c)
{
    int i, j;
    for (j = 0; j < h; j++)
        for (i = 0; i < w; i++)
            put(x + i, y + j, c);
}
static void frame(int x, int y, int w, int h, u16 c)
{
    fill(x, y, w, 1, c); fill(x, y + h - 1, w, 1, c);
    fill(x, y, 1, h, c); fill(x + w - 1, y, 1, h, c);
}
static u16 mix(u16 fg, u16 bg, int a)          /* a 0..15 */
{
    u32 f = (fg | (u32)fg << 16) & 0x07E0F81Fu, b = (bg | (u32)bg << 16) & 0x07E0F81Fu;
    u32 m = (b + (((f - b) * (u32)a) >> 4)) & 0x07E0F81Fu;
    return (u16)(m | m >> 16);
}
/* 4-bit packed alpha bitmap, w x h, at x, y */
static void blit(const u8 *a, int w, int h, int x, int y, u16 fg, int keepbg)
{
    int i, j, pw = (w + 1) / 2;
    for (j = 0; j < h; j++)
        for (i = 0; i < w; i++) {
            int v = a[j * pw + i / 2];
            v = (i & 1) ? v >> 4 : v & 15;
            if (!v)
                continue;
            int px = x + i, py = y + j;
            if ((unsigned)px >= (unsigned)clipx || (unsigned)py >= 480u)
                continue;
            u16 *d = &L[py * S + px];
            *d = keepbg && v < 15 ? mix(fg, *d, v) : (v >= 8 || !keepbg ? fg : *d);
        }
}

/* h = bitmap rows, cap = cap height (h > cap when descenders are kept) */
typedef struct { int h, cap, n; const char *chars; const u8 *w; const u16 *o; const u8 *a; } font;
static const font FN = {GN_H, GN_CAP, GN_N, GN_chars, GN_w, GN_o, GN_a};
static const font FV = {GV_H, GV_H, GV_N, GV_chars, GV_w, GV_o, GV_a};
static const font FL = {GL_H, GL_H, GL_N, GL_chars, GL_w, GL_o, GL_a};
static const font FS = {GS_H, GS_H, GS_N, GS_chars, GS_w, GS_o, GS_a};
#define FT FN
#define FK FN

static int gidx(const font *f, char c)
{
    int i;
    for (i = 0; i < f->n; i++)
        if (f->chars[i] == c)
            return i;
    return -1;
}
static int cw(const font *f, char c)
{
    int g = gidx(f, c);
    if (c == ' ') return f->cap / 2 + 1;
    if (c == '[' || c == ']') return 7;
    return g < 0 ? 0 : f->w[g] + (f->cap > 12 ? 2 : 1);
}
static int text_w(const font *f, const char *s)
{
    int w = 0;
    while (*s) w += cw(f, *s++);
    return w;
}
/* top-left of the cap height at x, y */
static void text(const font *f, int x, int y, const char *s, u16 fg)
{
    for (; *s; s++) {
        if (*s == '[' || *s == ']') {        /* square brackets: drawn */
            int h = f->cap + f->cap / 3, t = y - f->cap / 6, bx = *s == '[' ? x : x + 2;
            fill(bx, t, 2, h, fg);
            fill(*s == '[' ? bx : bx - 3, t, 5, 2, fg);
            fill(*s == '[' ? bx : bx - 3, t + h - 2, 5, 2, fg);
            x += 7;
            continue;
        }
        int g = gidx(f, *s);
        if (g >= 0)
            blit(f->a + f->o[g], f->w[g], f->h, x, y, fg, 1);
        x += cw(f, *s);
    }
}
static void ctext(const font *f, int x, int y, int w, int h, const char *s, u16 fg)
{
    text(f, x + (w - text_w(f, s)) / 2, y + (h - f->cap) / 2, s, fg);
}
static void unum(char *p, u32 v)
{
    char t[8];
    int n = 0;
    do { t[n++] = (char)('0' + v % 10); v /= 10; } while (v && n < 7);
    while (n) *p++ = t[--n];
    *p = 0;
}

/* a 1000MK2-style button: dark blue gradient, light top line */
static void button(int x, int y, int w, int h, int pressed)
{
    int j;
    for (j = 0; j < h; j++)
        fill(x, y + j, w, 1, pressed ? C_SEL : mix(C_BTN1, C_BTN0, 15 - j * 15 / (h ? h : 1)));
    fill(x, y, w, 1, pressed ? C_WH : C_BTNT);
    fill(x, y + h - 1, w, 1, pressed ? C_SEL : C_BTNB);
}
static void circle(int cx, int cy, int r, u16 c)
{
    int x, y;
    for (y = -r; y <= r; y++)
        for (x = -r; x <= r; x++)
            if (x * x + y * y <= r * r + r / 2)
                put(cx + x, cy + y, c);
}

/* ---------------------------------------------------------- key names ---- */
static const char *const MAJ[12] = {"B", "F#", "Db", "Ab", "Eb", "Bb", "F", "C", "G", "D", "A", "E"};
static const char *const MIN[12] = {"Abm", "Ebm", "Bbm", "Fm", "Cm", "Gm", "Dm", "Am", "Em", "Bm", "F#m", "Dbm"};
static const char *kname(u32 e)                 /* 1000MK2 key number -> name */
{
    u32 n = (e + 1) / 2;
    if (n < 1 || n > 12) return "C";
    return (e & 1) ? MIN[n - 1] : MAJ[n - 1];
}

/* ------------------------------------------------------------- screen ---- */
static void title(void)
{
    char b[12];
    fill(18, 12, 764, 30, C_TITLE);
    text(&FT, 30, 20, "TRACK FILTER", C_WH);
    text(&FT, 222, 20, "USB@PLAYER1", C_WH);
    if (tf_cfg.on) {
        int n = tf_count();
        blit(IC_funnel, IC_FUNNEL_W, IC_FUNNEL_H, 648, 17, C_WH, 1);
        text(&FT, 712, 20, "[", C_WH);
        if (n >= 0) {
            unum(b, (u32)n);
            text(&FT, 758 - text_w(&FT, b), 20, b, C_WH);
        }
        text(&FT, 761, 20, "]", C_WH);
    }
}

static void tabs(void)
{
    fill(18, 60, 68, 60, tfs_tab == 0 ? C_TABS : C_TABN);
    frame(18, 60, 68, 60, C_BTNT);
    if (tfs_tab == 0) fill(22, 64, 3, 52, C_STRIPE);
    ctext(&FS, 18, 70, 68, 16, "BPM", C_WH);
    ctext(&FS, 18, 88, 68, 16, "KEY", C_WH);
    fill(18, 123, 68, 62, tfs_tab == 1 ? C_TABS : C_TABN);
    frame(18, 123, 68, 62, C_BTNT);
    if (tfs_tab == 1) fill(22, 127, 3, 54, C_STRIPE);
    ctext(&FS, 18, 123, 68, 62, "MY TAG", C_WH);
    if (tfs_tab == 1) {                    /* the overview button */
        button(18, 193, 68, 50, tfs_info);
        circle(52, 218, 10, tfs_info ? C_BLACK : C_WH);
        fill(51, 216, 3, 9, tfs_info ? C_SEL : C_BTN1);
        fill(51, 211, 3, 3, tfs_info ? C_SEL : C_BTN1);
    }
    button(18, 251, 68, 48, 0);
    ctext(&FS, 18, 251, 68, 48, "-RESET", C_WH);
    fill(100, 60, 1, 240, C_PEDGE);
}

static void checkrow(int i, const char *lbl, int tick)
{
    int y = ROW_Y(i);
    button(CHK_X, y, CHK_W, ROW_H, 0);
    frame(CHK_X + 8, y + 9, 18, 18, C_WH);
    frame(CHK_X + 9, y + 10, 16, 16, C_WH);
    if (tick)
        blit(IC_tick, IC_TICK_W, IC_TICK_H, CHK_X + 8, y + 10, C_WH, 1);
    text(&FL, CHK_X + 39, y + (ROW_H - FL.h) / 2, lbl, C_WH);
    /* the notch from the label button into the value panel */
    int j;
    for (j = 0; j < 9; j++)
        fill(CHK_X + CHK_W + 9 - j, y + 18 - j, 1, 2 * j + 1, C_PEDGE);
}
static void panel(int i, int x1)
{
    int y = ROW_Y(i) - 5;
    fill(231, y, x1 - 231, ROW_H + 10, C_PANEL);
    frame(231, y, x1 - 231, ROW_H + 10, C_PEDGE);
}
static void valbox(int x, int y, int w, const char *s, int edit)
{
    button(x, y, w, ROW_H - 4, 0);
    if (edit)
        frame(x, y, w, ROW_H - 4, C_WH);
    ctext(&FV, x, y, w, ROW_H - 4, s, C_WH);
}

static void conditions(void)
{
    char b[12];
    int i;
    /* BPM */
    checkrow(0, "BPM", tf_tick(0));
    panel(0, 682);
    if (tfs_edit == 1) { unum(b, ebuf); if (!elen) b[0] = 0; } else unum(b, tf_bpm());
    valbox(BOX_X, ROW_Y(0) + 2, BOX_W, b, tfs_edit == 1);
    text(&FL, 388, ROW_Y(0) + 12, "BPM", C_WH);
    blit(IC_le, IC_LE_W, IC_LE_H, 452, ROW_Y(0) + 11, C_WH, 1);
    blit(IC_pm, IC_PM_W, IC_PM_H, 494, ROW_Y(0) + 11, C_WH, 1);
    unum(b, tf_pct());
    valbox(PCT_X, ROW_Y(0) + 2, 126, b, tfs_edit == 2);
    text(&FL, 652, ROW_Y(0) + 12, "%", C_WH);
    /* KEY */
    checkrow(1, "KEY", tf_tick(1));
    panel(1, 682);
    valbox(BOX_X, ROW_Y(1) + 2, BOX_W, kname(tf_key()), tfs_edit == 3);
    text(&FL, 388, ROW_Y(1) + 12, "RELATED KEY", C_WH);
    /* RATING */
    checkrow(2, "RATING", tf_tick(2));
    panel(2, 680);
    button(BOX_X, ROW_Y(2) + 2, 418, ROW_H - 4, 0);
    {
        u32 r = tf_val(2, 0);
        if (!tf_vn(2)) r = 3;
        for (i = 0; i < 5; i++)
            blit((u32)i < r ? IC_star1 : IC_star0, IC_STAR1_W, IC_STAR1_H,
                 321 + 66 * i, ROW_Y(2) + 7, C_WH, 1);
    }
    /* COLOR */
    checkrow(3, "COLOR", tf_tick(3));
    panel(3, 782);
    {
        u32 m = tf_vn(3) ? tf_val(3, 0) : 0;
        for (i = 0; i < 8; i++) {
            int x = 256 + i * 657 / 10;
            button(x, ROW_Y(3) + 2, 60, ROW_H - 4, (m >> i) & 1);
            circle(x + 30, ROW_Y(3) + 17, 9, COLS[i]);
        }
    }
    /* MASTER PLAYER */
    button(714, 67, 68, 99, 0);
    ctext(&FS, 714, 98, 68, 14, "MASTER", C_WH);
    ctext(&FS, 714, 116, 68, 14, "PLAYER", C_WH);
}

/* the tags of column c, in position order -> idx[], returns how many */
static int col_tags(int c, int *idx)
{
    int n = 0, i, j;
    for (i = 0; i < (int)tf_mtn && i < MT_MAX; i++) {
        if (tf_mt[i].col != c) continue;
        for (j = n; j > 0 && tf_mt[idx[j - 1]].pos > tf_mt[i].pos; j--)
            idx[j] = idx[j - 1];
        idx[j] = i;
        n++;
    }
    return n;
}
static int chosen(int c, u32 id)
{
    int i;
    for (i = 0; i < tf_mcond[c].n; i++)
        if (tf_mcond[c].id[i] == id) return 1;
    return 0;
}
static const int COLX[4] = {100, 272, 445, 618};
#define COLW 164
static const int GX[2] = {272, 527};
static const int GY[3] = {124, 184, 244};

static void checkbox(int x, int y, int on, u16 c)
{
    frame(x, y, 18, 18, c);
    frame(x + 1, y + 1, 16, 16, c);
    if (on) blit(IC_tick, IC_TICK_W, IC_TICK_H, x, y + 1, c, 1);
}

static void mytag_info(void)
{
    int c, i, idx[MT_MAX];
    fill(100, 60, 682, 240, C_BLACK);
    frame(100, 60, 682, 240, C_PEDGE);
    for (c = 0; c < 4; c++) {
        int x = 100 + c * 1705 / 10;
        if (c) fill(x, 60, 1, 240, C_PEDGE);
        if ((u32)c >= tf_mcn) continue;
        checkbox(x + 6, 68, tf_mcond[c].tick, C_WH);
        clipx = x + 168;
        text(&FN, x + 32, 71, tf_mc[c].name, C_WH);
        int n = col_tags(c, idx);
        for (i = 0; i < n && i < 6; i++) {
            struct mtag *t = &tf_mt[idx[i]];
            text(&FN, x + 14, 110 + 32 * i, t->name, chosen(c, t->id) ? C_WH : C_DIM);
        }
        clipx = 800;
    }
    fill(100, 96, 682, 1, C_PEDGE);
}

static void mytag(void)
{
    int c, i, idx[MT_MAX];
    if (tfs_info) { mytag_info(); return; }
    if (tfs_col >= 4) tfs_col = 0;
    frame(100, 96, 682, 204, C_BTNT);
    for (c = 0; c < 4; c++) {               /* the four column tabs */
        int x = COLX[c], sel = (u32)c == tfs_col;
        if (sel) {
            fill(x, 60, COLW, 37, C_BLACK);
            fill(x, 60, COLW, 1, C_BTNT); fill(x, 60, 1, 37, C_BTNT);
            fill(x + COLW - 1, 60, 1, 37, C_BTNT);
        } else
            button(x, 61, COLW, 34, 0);
        if ((u32)c >= tf_mcn) continue;
        checkbox(x + 8, 69, tf_mcond[c].tick, sel ? C_WH : C_GREY);
        clipx = x + COLW - 2;              /* long names end at the tab edge */
        text(&FN, x + 33, 71, tf_mc[c].name, C_WH);
        clipx = 800;
    }
    c = (int)tfs_col;
    ctext(&FS, 110, 207, 130, 14, "CRITERIA", C_WH);
    button(112, 227, 64, 52, !tf_mcond[c].orr);
    ctext(&FS, 112, 227, 64, 52, "AND", !tf_mcond[c].orr ? C_BLACK : C_WH);
    button(176, 227, 64, 52, tf_mcond[c].orr);
    ctext(&FS, 176, 227, 64, 52, "OR", tf_mcond[c].orr ? C_BLACK : C_WH);
    fill(251, 125, 1, 156, C_PEDGE);
    int n = col_tags(c, idx), rows = (n + 1) / 2;
    if (rows > 3 && tfs_scroll > (u32)(rows - 3)) tfs_scroll = (u32)(rows - 3);
    if (rows <= 3) tfs_scroll = 0;
    if (rows > 3) {                        /* the scroll bar */
        int h = 156 * 3 / rows, y = 125 + (156 - h) * (int)tfs_scroll / (rows - 3);
        fill(262, y, 3, h, C_GREY);
    }
    for (i = 0; i < 6; i++) {
        int k = (int)tfs_scroll * 2 + i, x = GX[i & 1], y = GY[i >> 1];
        if (k >= n) { button(x, y, 243, 36, 0); continue; }
        struct mtag *t = &tf_mt[idx[k]];
        int on = chosen(c, t->id);
        button(x, y, 243, 36, on);
        clipx = x + 240;
        text(&FN, x + 10, y + 11, t->name, on ? C_BLACK : C_WH);
        clipx = 800;
    }
}

/* keypads and the key picker sit over the player strip (y 320..480) */
static void keypad(void)
{
    int i;
    fill(0, 320, 800, 160, C_BLACK);
    for (i = 0; i < 10; i++) {
        char b[2] = {(char)(i < 9 ? '1' + i : '0'), 0};
        int d = i < 9 ? i + 1 : 0, x = 40 + 66 * i;
        int off = tfs_edit == 2 && d > 6;
        button(x, 330, 60, 36, 0);
        if (off) fill(x, 330, 60, 36, C_PANEL);
        ctext(&FT, x, 330, 60, 36, b, off ? C_DIM : C_WH);
    }
    if (tfs_edit == 1) {
        button(717, 330, 64, 36, 0);
        blit(IC_bksp, IC_BKSP_W, IC_BKSP_H, 734, 339, C_WH, 1);
    }
    button(677, 418, 104, 44, 0);
    ctext(&FS, 677, 418, 104, 44, "CLOSE", C_WH);
}
static void picker(void)
{
    int i;
    fill(0, 320, 800, 160, C_BLACK);
    fill(48, 325, 704, 1, C_PEDGE);
    fill(48, 325, 1, 8, C_PEDGE);
    fill(751, 325, 1, 8, C_PEDGE);
    {
        const char *t = tfs_minor ? "MINOR" : "MAJOR";
        int w = text_w(&FS, t) + 12;
        fill(400 - w / 2, 320, w, 11, C_BLACK);
        ctext(&FS, 400 - w / 2, 320, w, 11, t, C_WH);
    }
    for (i = 0; i < 12; i++) {
        int x = 18 + 64 * i;
        u32 e = 2u * (u32)(i + 1) - (tfs_minor ? 1 : 0);
        button(x, 338, 60, 46, tf_key() == e);
        ctext(&FK, x, 338, 60, 46, tfs_minor ? MIN[i] : MAJ[i], tf_key() == e ? C_BLACK : C_WH);
        ctext(&FS, x, 392, 60, 14, tfs_minor ? MAJ[i] : MIN[i], C_GREY);
    }
    button(18, 418, 104, 44, 0);
    ctext(&FS, 18, 418, 104, 44, tfs_minor ? "MAJOR" : "MINOR", C_WH);
    button(677, 418, 104, 44, 0);
    ctext(&FS, 677, 418, 104, 44, "CLOSE", C_WH);
}

static void draw_screen(void)
{
    fill(0, 0, 800, 320, C_BLACK);
    title();
    tabs();
    if (tfs_tab == 0) conditions(); else mytag();
    if (tfs_edit == 1 || tfs_edit == 2) keypad();
    else if (tfs_edit == 3) picker();
}

/* is the browse showing a list of tracks right now? (its list buffer) */
static int is_trk(u32 r)
{
    u8 k = R8(r + 0x12);
    return (k >= 1 && k <= 5) || (k >= 0x16 && k <= 0x18);
}
static int left_tracks(void)
{
    u32 l = tf_left, n, rows;
    if (l < 0x08000000u || l >= 0x10000000u)
        return 0;
    n = R16(l + 2); rows = R32(l + 0x4C);
    if (!n)                                   /* an empty filtered list */
        return tf_tracks && tf_cfg.on;
    if (rows < 0x08000000u || rows >= 0x10000000u || n > 50000)
        return 0;
    return is_trk(rows);
}

/* the funnel + [count] in the browse header while the screen is closed;
 * a dim funnel marks the control when the filter is off */
static void header(void)
{
    char b[12];
    if (!left_tracks()) return;
    {   /* the bar's own gradient, measured on the 700 */
        int j;
        for (j = HDR_Y0; j < HDR_Y1; j++)
            fill(640, j, 145, 1, mix(RGB(25, 57, 100), RGB(16, 42, 78), (j - HDR_Y0) * 15 / (HDR_Y1 - HDR_Y0)));
    }
    blit(IC_funnel, IC_FUNNEL_W, IC_FUNNEL_H, 648, HDR_Y0 + 5, tf_cfg.on ? C_WH : C_DIM, 1);
    if (!tf_cfg.on) return;
    text(&FT, 700, HDR_Y0 + 6, "[", C_WH);
    unum(b, R16(tf_left + 2));
    text(&FT, 758 - text_w(&FT, b), HDR_Y0 + 6, b, C_WH);
    text(&FT, 761, HDR_Y0 + 6, "]", C_WH);
}

/* our pixels' checksum: every 3rd pixel of 14 lines through the screen.
 * The deck redraws the browse list's titles straight into the layer (CPU
 * text, not 2DG: the strip cannot catch it), e.g. for long-title scrolling;
 * 8 sample points missed it (deck 2026-11-22). Lines every 17 px catch any
 * line of text (glyphs are taller than that). ~3,200 reads per present. */
#define NLINE 19
static u32 lay_line[2][NLINE];
u32 tfs_dmg;                 /* lines found changed at the last check (bit per line) */
static u32 line_sum(int y)
{
    const volatile u16 *p = L + y * S;
    u32 h = 0x811C9DC5u;
    int x;
    for (x = 4; x < 796; x += 3)
        h = (h ^ p[x]) * 0x01000193u;
    return h;
}
static void sample(int k)
{
    int i;
    for (i = 0; i < NLINE; i++) lay_line[k][i] = line_sum(8 + 17 * i);
}
static int intact(int k)
{
    u32 m = 0;
    int i;
    for (i = 0; i < NLINE; i++)
        if (lay_line[k][i] != line_sum(8 + 17 * i)) m |= 1u << i;
    if (m) tfs_dmg = m;
    return !m;
}

/* ------------------------------------------------- stock command strip ---- */
u32 tfs_stripped, tfs_cropped, tfs_lay;
u32 tfs_tlo, tfs_thi;      /* physical layer range the text blender skips (tfentry.S) */
static void strip_stock(u32 req, int y1)
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
        default: return;
        }
        if (i + len > n) return;
        int x0 = 0x7FFF, ya = 0x7FFF, x1 = -1, yb = -1, any = 0;
        if (op == 0x82 || op == 0x80) {
            u32 k0 = op == 0x82 ? 4 : 2, k;
            for (k = k0; k < k0 + 4; k++) {
                int x = (short)(w[i + k] >> 16), y = (short)(w[i + k] & 0xFFFF);
                if (x < x0) x0 = x;
                if (x > x1) x1 = x;
                if (y < ya) ya = y;
                if (y > yb) yb = y;
            }
            any = 1;
        } else if (op == 0xA0 || op == 0xA2) {
            int ax = (short)(w[i + 5] >> 16), ay = (short)(w[i + 5] & 0xFFFF);
            x0 = ax - (short)(w[i + 3] >> 16); x1 = ax + (short)(w[i + 3] & 0xFFFF);
            ya = ay - (short)(w[i + 4] >> 16); yb = ay + (short)(w[i + 4] & 0xFFFF);
            any = 1;
        } else if (op == 0xB0) {
            u32 np = w[i + 1] & 0xFFFF, k;
            for (k = 0; k < np; k++) {
                int x = (short)(w[i + 3 + k] >> 16), y = (short)(w[i + 3 + k] & 0xFFFF);
                if (x < x0) x0 = x;
                if (x > x1) x1 = x;
                if (y < ya) ya = y;
                if (y > yb) yb = y;
            }
            any = np != 0;
        }
        if (op == 0x82 && x0 <= 0 && x1 >= 799 && ya <= 0 && yb >= 479) {
            i += len;                                    /* the UI layer itself */
            continue;
        }
        if (any && x0 >= 0 && x1 <= 800 && ya >= 0 && yb <= y1) {
            u32 k;
            for (k = 0; k < len; k++) w[i + k] = 0x08000000u;
            tfs_stripped++;
        } else if (op == 0xA0 && ya < y1 && yb >= y1 && x0 < 800 && x1 >= 0) {
            /* a clear reaching below us (the browse clears 0,0 800x480 and
             * 4,4 792x472 every frame): keep only its part below the screen.
             * The old test wanted x1 >= 800, but x1 is the last pixel (799),
             * so those clears wiped the screen on every frame. */
            w[i + 4] = (u32)(yb - y1);                     /* up 0, down to its end */
            w[i + 5] = (w[i + 5] & 0xFFFF0000u) | (u32)y1;
            tfs_cropped++;
        }
        i += len;
    }
}

/* ------------------------------------------------------------- flush ---- */
static int pending;                       /* condition writes queued by touch */
static u32 q_kind, q_a, q_b;
static void apply_pending(void);
static u32 master_key(void);
extern u32 tfs_mkey;

int tf_sane(void);
void tfs_flush(u32 dev, u32 req)
{
    if (!tf_sane()) { tfs_tlo = 0; return; }
    (void)dev;
    apply_pending();
    {
        u32 mw = R32(MODEW);
        if (mw == 1u || mw == 7u)
            tfs_mkey = R32(MODEW_DECK) ? master_key() : 0;
    }
    if (R32(MODEW) != 2u || R8(MSOFF)) {
        tfs_tlo = 0;
        if (tfs_mode) { tfs_mode = 0; tfs_edit = 0; tfs_ver++; }
        return;
    }
    const u32 *w = (const u32 *)R32(req);
    int present = w && w[13] == 0x4C && w[16] == 0x58 &&
                  (w[27] >> 24) == 0x82 && w[29] == 0x032001E0u;
    int y1 = tfs_edit ? 480 : 320;
    if (tfs_mode && (present || R32(req + 32) == 1600u))
        strip_stock(req, y1);

    if (!present) return;
    int stride = (int)w[17];
    if (stride < 800 || stride > 1024) return;
    tfs_lay = w[14];
    L = (u16 *)UNCACHED(w[14]);
    S = stride;
    tfs_draws++;
    if (!tfs_mode) { tfs_tlo = 0; header(); return; }
    {   /* the browse's own text stays out of the screen (tfentry.S) */
        u32 lo = w[14] & 0x1FFFFFFFu;
        tfs_thi = lo + (u32)stride * 2u * (u32)y1;
        tfs_tlo = lo;
    }
    {   /* the knob scrolls the My Tag grid (a row = 2 tags) */
        int d = rotacc;
        rotacc = 0;
        if (d && tfs_tab == 1 && !tfs_info) {
            int t = (int)tfs_scroll + (d > 0 ? 1 : -1);
            if (t >= 0) { tfs_scroll = (u32)t; tfs_ver++; }
        }
    }
    {   /* the live count follows new lists */
        u32 c = (u32)tf_count();
        if (c != lastcount || tf_naver != lastnaver) { lastcount = c; lastnaver = tf_naver; tfs_ver++; }
    }
    int k = lay_addr[0] == w[14] ? 0 : lay_addr[1] == w[14] ? 1 : -1;
    if (k < 0) {
        k = lay_addr[0] == 0 ? 0 : lay_addr[1] == 0 ? 1 : (int)(tfs_full & 1);
        lay_addr[k] = w[14]; lay_ver[k] = 0;
    }
    if (lay_ver[k] != tfs_ver || !intact(k)) {
        draw_screen();
        sample(k);
        lay_ver[k] = tfs_ver;
        tfs_full++;
    }
}

/* MASTER PLAYER: BPM and KEY of this deck's loaded track (the sync master
 * when it is alone; a LINK master's data is not read yet) -> 1000MK2 key
 * number (nA = 2n-1, nB = 2n) from "Abm" / "F#" / "8A"; 0 = none */
static u32 master_key(void)
{
    static const u8 CAM[12] = {8, 3, 10, 5, 12, 7, 2, 9, 4, 11, 6, 1};
    static const u8 PC[7] = {9, 11, 0, 2, 4, 5, 7};
    u16 c0 = R16(TRKKEY), c1 = R16(TRKKEY + 2), c2 = R16(TRKKEY + 4);
    if (c0 >= '1' && c0 <= '9') {                       /* Camelot */
        u32 n = c0 - '0';
        u16 l = c1;
        if (c1 >= '0' && c1 <= '2') { n = n * 10 + (c1 - '0'); l = c2; }
        if (n < 1 || n > 12) return 0;
        if ((l | 0x20) == 'a') return 2 * n - 1;
        if ((l | 0x20) == 'b') return 2 * n;
        return 0;
    }
    if ((c0 | 0x20) < 'a' || (c0 | 0x20) > 'g') return 0;
    int pc = PC[(c0 | 0x20) - 'a'];
    u16 m = c1;
    if (c1 == '#') { pc++; m = c2; }
    else if (c1 == 'b') { pc--; m = c2; }
    pc = (pc + 12) % 12;
    if (m == 'm') return 2u * CAM[(pc + 3) % 12] - 1;
    return 2u * CAM[pc];
}
/* the key text is only the loaded track's key while the main / PERFORM
 * screen shows it (the browse reuses the buffer): remembered from there */
u32 tfs_mkey;
static u32 master_bpm(void)
{
    u32 u = R16(TRKBPM);
    int p = (short)R16(PITCHW);
    if (!R32(MODEW_DECK) || u < 2000 || u > 99900) return 0;
    return (u * (u32)(10000 + p) / 10000 + 50) / 100;
}

/* ----------------------------------------------------- condition writes ---- */
static void queue(u32 kind, u32 a, u32 b) { q_kind = kind; q_a = a; q_b = b; pending = 1; }
static void apply_pending(void)
{
    if (!pending) return;
    pending = 0;
    switch (q_kind) {
    case 1: tf_setb((int)q_a, tf_bpm(), tf_pct()); break;         /* tick BPM */
    case 2: tf_setk((int)q_a, tf_key()); break;
    case 3: { u32 r = tf_vn(2) ? tf_val(2, 0) : 3; tf_setr((int)q_a, r ? r : 3); break; }
    case 4: tf_setc((int)q_a, tf_vn(3) ? tf_val(3, 0) : 0); break;
    case 5: tf_setb(tf_tick(0), q_a, tf_pct()); break;              /* BPM value */
    case 6: tf_setb(tf_tick(0), tf_bpm(), q_a); break;              /* percent */
    case 7: tf_setk(tf_tick(1), q_a); break;                        /* key */
    case 8: tf_setr(tf_tick(2), q_a); break;                        /* stars */
    case 9: tf_setc(tf_tick(3), q_a); break;                        /* colours */
    case 12:                                                        /* MASTER PLAYER */
        if (q_a) tf_setb(1, q_a, tf_pct());
        if (q_b) tf_setk(1, q_b);
        break;
    case 10:                                                        /* RESET */
        if (tfs_tab == 1) {                 /* MY TAG: only My Tag (1000MK2) */
            int c;
            for (c = 0; c < 4; c++) {
                tf_mcond[c].tick = 0; tf_mcond[c].orr = 0; tf_mcond[c].n = 0;
            }
            break;
        }
        tf_setb(0, 130, 6); tf_setk(0, 16); tf_setr(0, 3); tf_setc(0, 0);
        break;
    }
    tfs_ver++;
}

/* ------------------------------------------------------------- touch ---- */
static int inr(int x, int y, int x0, int y0, int w, int h)
{
    return x >= x0 && x < x0 + w && y >= y0 && y < y0 + h;
}
static void close_screen(void)
{
    if (ebuf && tfs_edit == 1 && ebuf != tf_bpm()) queue(5, ebuf, 0);
    tfs_mode = 0; tfs_edit = 0; tfs_ver++;
    if (left_tracks()) { tfs_relist = 1; rl_n = 0; }
}

/* every parser frame (also with no finger down): key presses we inject,
 * hold timing, the keys that leave the screen */
u32 tfs_backt;           /* 1 ms clock at the last BACK press (tf.c) */
static void keys(void)
{
    u32 kb = R8(KEYB + 102);
    if (kb & ~lastkeys & K_BACK) {           /* also our own injected BACK */
        tfs_backt = R32(MSCLK);
        if (!tfs_backt) tfs_backt = 1;
    }
    if (tfs_relist) {                        /* BACK, wait, push: redo the list */
        rl_n++;
        switch (tfs_relist) {
        case 1:                              /* BACK until the tracks are gone */
            if (rl_n <= 4) {
                R8(KEYB + 102) = (u8)(kb | K_BACK);
                tfs_backt = R32(MSCLK) | 1;
            } else if (!left_tracks() || rl_n > 400) { tfs_relist = 2; rl_n = 0; rl_t = R32(MSCLK); }
            break;
        case 2:                              /* let the category list settle */
            if (R32(MSCLK) - rl_t >= 400u && rl_n > 8) { tfs_relist = 3; rl_n = 0; }
            break;
        case 3:                              /* push until the tracks are back */
            if (rl_n <= 6) R8(KEYB + 102) = (u8)(kb | K_PUSH);
            else if (left_tracks() || rl_n > 600) { tfs_relist = 0; tfs_relists++; }
            break;
        default:
            tfs_relist = 0;
        }
        lastkeys = R8(KEYB + 102);
        return;
    }
    if (tfs_mode) {
        /* the knob belongs to the screen: its steps go to the My Tag grid and
         * the counter is put back, so the browse list never scrolls under us
         * (on the deck it did, and its titles were drawn over the screen) */
        u32 r = R16(ROTC);
        if (lastrot <= 0xFFFFu && r != lastrot) {
            rotacc += (short)(r - lastrot);
            R16(ROTC) = (u16)lastrot;
        } else
            lastrot = r;
        u32 edge = kb & ~lastkeys;
        if (edge & (K_BACK | K_PUSH)) {      /* BACK (or a knob push) leaves */
            if (tfs_edit) { tfs_edit = 0; tfs_ver++; }
            else close_screen();
        }
        R8(KEYB + 102) = (u8)(kb & ~(K_BACK | K_PUSH));   /* the browse never sees them */
    }
    if (!tfs_mode) lastrot = R16(ROTC);
    lastkeys = kb;
}

/* 1 = consumed */
int tfs_touch(int down, int x, int y)
{
    if (!tf_sane()) return 0;
    keys();
    if (R32(MODEW) != 2u || R8(MSOFF)) { tdown = 0; return 0; }
    u32 now = R32(MSCLK);
    if (!down) {
        int was = tdown;
        tdown = 0;
        if (!was) return 0;
        if (tzone == 1 && !tfired) {                     /* header tap: on / off */
            tf_cfg.on = !tf_cfg.on;
            tfs_taps++;
            if (left_tracks()) { tfs_relist = 1; rl_n = 0; }
        }
        return tzone != 0;
    }
    tfs_touches++;
    if (!tdown) {                                        /* a new press */
        tdown = 1; tx0 = (u32)x; ty0 = (u32)y; tfired = 0; tzone = 0;
        tdown = now ? now : 1;
        if (!tfs_mode) {
            if (left_tracks() && inr(x, y, HDR_X0, HDR_Y0, 782 - HDR_X0, HDR_Y1 - HDR_Y0)) {
                tzone = 1;
                return 1;
            }
            return 0;
        }
        tzone = 2;
        /* the screen: one action per press */
        if (tfs_edit == 1 || tfs_edit == 2) {
            int i;
            for (i = 0; i < 10; i++)
                if (inr(x, y, 40 + 66 * i, 330, 60, 36)) {
                    u32 d = i < 9 ? (u32)i + 1 : 0;
                    if (tfs_edit == 2) {
                        if (d <= 6) queue(6, d, 0);
                    } else if (elen < 3) {
                        ebuf = ebuf * 10 + d; elen++;
                        if (ebuf) queue(5, ebuf, 0);
                        tfs_ver++;
                    }
                    return 1;
                }
            if (tfs_edit == 1 && inr(x, y, 717, 330, 64, 36)) {
                ebuf /= 10; if (elen) elen--;
                if (ebuf) queue(5, ebuf, 0);
                tfs_ver++;
                return 1;
            }
            if (inr(x, y, 677, 418, 104, 44)) { tfs_edit = 0; tfs_ver++; return 1; }
            if (y >= 320) return 1;
        }
        if (tfs_edit == 3) {
            int i;
            for (i = 0; i < 12; i++)
                if (inr(x, y, 18 + 64 * i, 338, 60, 46)) {
                    queue(7, 2u * (u32)(i + 1) - (tfs_minor ? 1 : 0), 0);
                    return 1;
                }
            if (inr(x, y, 18, 418, 104, 44)) { tfs_minor = !tfs_minor; tfs_ver++; return 1; }
            if (inr(x, y, 677, 418, 104, 44)) { tfs_edit = 0; tfs_ver++; return 1; }
            if (y >= 320) return 1;
        }
        if (y >= 320) { tzone = 0; return 0; }           /* the player strip */
        if (inr(x, y, 600, 12, 182, 30)) { close_screen(); return 1; }   /* title funnel */
        if (inr(x, y, 18, 60, 68, 60)) { if (tfs_tab) { tfs_tab = 0; tfs_edit = 0; tfs_ver++; } return 1; }
        if (inr(x, y, 18, 123, 68, 62)) { if (!tfs_tab) { tfs_tab = 1; tfs_edit = 0; tfs_ver++; } return 1; }
        if (inr(x, y, 18, 251, 68, 48)) { tzone = 3; return 1; }        /* RESET: on hold */
        if (tfs_tab == 1) {
            int c, i, n, idx[MT_MAX];
            if (inr(x, y, 18, 193, 68, 50)) { tfs_info = !tfs_info; tfs_ver++; return 1; }
            for (c = 0; c < 4; c++) {
                int cx = tfs_info ? 100 + c * 1705 / 10 : COLX[c];
                if ((u32)c < tf_mcn && inr(x, y, cx, 60, tfs_info ? 170 : COLW, 36)) {
                    if (x < cx + 30) tf_mcond[c].tick = !tf_mcond[c].tick;   /* tick box */
                    else if (!tfs_info) { tfs_col = (u32)c; tfs_scroll = 0; }
                    tfs_ver++;
                    return 1;
                }
            }
            if (tfs_info) return 1;
            c = (int)tfs_col;
            if (inr(x, y, 112, 227, 64, 52)) { tf_mcond[c].orr = 0; tfs_ver++; return 1; }
            if (inr(x, y, 176, 227, 64, 52)) { tf_mcond[c].orr = 1; tfs_ver++; return 1; }
            n = col_tags(c, idx);
            for (i = 0; i < 6; i++) {
                int k = (int)tfs_scroll * 2 + i;
                if (k < n && inr(x, y, GX[i & 1], GY[i >> 1], 243, 36)) {
                    struct mcond *m = &tf_mcond[c];
                    u32 id = tf_mt[idx[k]].id;
                    int j;
                    for (j = 0; j < m->n; j++)
                        if (m->id[j] == id) break;
                    if (j < m->n) {                       /* chosen: drop it */
                        for (; j + 1 < m->n; j++) m->id[j] = m->id[j + 1];
                        m->n--;
                    } else if (m->n < MT_SEL)
                        m->id[m->n++] = id;
                    tfs_ver++;
                    return 1;
                }
            }
            return 1;
        }
        {
            int i;
            for (i = 0; i < 4; i++)
                if (inr(x, y, CHK_X, ROW_Y(i), CHK_W, ROW_H)) {
                    queue((u32)i + 1, !tf_tick(i == 0 ? 0 : i == 1 ? 1 : i == 2 ? 2 : 3), 0);
                    return 1;
                }
        }
        if (inr(x, y, 714, 67, 68, 99)) {                 /* MASTER PLAYER */
            u32 b = master_bpm(), k = tfs_mkey;
            if (b || k) { queue(12, b, k); tfs_masters++; }
            return 1;
        }
        if (inr(x, y, BOX_X, ROW_Y(0), BOX_W, ROW_H)) {
            tfs_edit = 1; ebuf = tf_bpm(); elen = ebuf >= 100 ? 3 : ebuf >= 10 ? 2 : 1;
            tfs_ver++; return 1;
        }
        if (inr(x, y, PCT_X, ROW_Y(0), 126, ROW_H)) { tfs_edit = 2; tfs_ver++; return 1; }
        if (inr(x, y, BOX_X, ROW_Y(1), BOX_W, ROW_H)) {
            tfs_edit = 3; tfs_minor = tf_key() & 1; tfs_ver++; return 1;
        }
        if (inr(x, y, BOX_X, ROW_Y(2), 418, ROW_H)) {
            int i = (x - 288) / 66;
            if (i < 0) i = 0;
            if (i > 4) i = 4;
            queue(8, (u32)i + 1, 0);
            return 1;
        }
        if (inr(x, y, 256, ROW_Y(3), 526, ROW_H)) {
            int i = (x - 256) * 10 / 657;
            if (i >= 0 && i < 8) {
                u32 m = tf_vn(3) ? tf_val(3, 0) : 0;
                queue(9, m ^ (1u << i), 0);
            }
            return 1;
        }
        return 1;
    }
    /* still held */
    if (!tfired && now - tdown >= 1000u) {
        if (tzone == 1) {                                 /* header hold: the screen */
            tfired = 1; tfs_holds++;
            tf_cfg.on = 1;                                /* like the 1000MK2 */
            tfs_mode = 1; tfs_tab = 0; tfs_edit = 0; tfs_ver++;
            lastrot = R16(ROTC); rotacc = 0;
        } else if (tzone == 3) {                          /* hold RESET */
            tfired = 1;
            queue(10, 0, 0);
        }
    }
    return tzone != 0;
}
