/* ut.c -- the mod rows in the deck's own UTILITY screen (XDJ-700 fw 1.15).
 *
 * UTILITY (hold MENU) is a scrolling list driven by a table of 52-byte
 * records (fw 1.15: RAM copy at 0x0E736410, count word 0x0E736750 = 16):
 *   +0 label (UTF-16)  +4 value texts (ptr[])  +8 u16 number of values
 *   +12 current index  +16 index being edited (-1 = not editing)  +20 1
 *   +24..+48 callbacks: init, draw(rec, ui), begin edit, select(rec, i),
 *   commit(rec) -> 1 if changed, query (0), end edit.
 * The installer copies the 16 stock records into ut_tab[0..15] and points
 * the firmware's four table pools here (count -> 21). Our five rows use
 * the stock generic edit callbacks and the stock PLAY MODE draw routine
 * (label + values[index]; nothing row-specific once +16 != -1); only the
 * value lookup and the commit are ours. Same state bytes as the MY
 * SETTINGS rows (tools/mysettings.py), so both menus always agree; a
 * linked deck's 7-row MY SETTINGS popup can't hold them all, UTILITY can.
 */
typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;

#define R8(a)  (*(volatile u8 *)(a))
#define R32(a) (*(volatile u32 *)(a))

#if !defined(MSOFF) || !defined(MSSAVE) || !defined(MODEB) || !defined(VSELB) \
    || !defined(BRAKE_LIT) || !defined(MSBRAKE) || !defined(MSGATE) \
    || !defined(TS_BASE) || !defined(TS_DATA) || !defined(BRAKE_STOCK)
#error address defines missing (tools/utility/build.sh)
#endif

#define NSTOCK 16
#define NOURS  5
#define CB_NOP   0x08DD0C6Au
#define CB_DRAW  0x08DD0C6Eu       /* PLAY MODE draw: label + values[idx] */
#define CB_BEGIN 0x08DD198Cu
#define CB_SEL   0x08DD1AD0u
#define CB_Q0    0x08DD20A0u
#define CB_END   0x08DD20E2u

enum { K_MODS, K_WAVE, K_BRAKE, K_GATE, K_THEME };

static const u16 L_MODS[] = u"MODS";
static const u16 L_WAVE[] = u"WAVEFORM COLOR";
static const u16 L_BRAKE[] = u"VINYL BRAKE LENGTH";
static const u16 L_GATE[] = u"HOT CUE GATE";
static const u16 L_THEME[] = u"THEME";

static const u16 S_ON[] = u"ON", S_OFF[] = u"OFF", S_GATE[] = u"GATE";
static const u16 S_BLUE[] = u"BLUE", S_RGB[] = u"RGB", S_3B[] = u"3-BAND",
                 S_CUST[] = u"CUSTOM";
static const u16 S_B1[] = u"1X", S_B2[] = u"2X", S_B4[] = u"4X",
                 S_B8[] = u"8X", S_B15[] = u"15X", S_B05[] = u"0.5X";
static const u16 S_NONE[] = u"NOT INSTALLED";

static const u16 *const V_MODS[] = {S_ON, S_OFF};
static const u16 *const V_WAVE[] = {S_BLUE, S_RGB, S_3B, S_CUST};
static const u16 *const V_BRAKE[] = {S_B1, S_B2, S_B4, S_B8, S_B15, S_B05};
static const u16 *const V_GATE[] = {S_OFF, S_GATE};
static const u16 *const V_TNONE[] = {S_NONE};
/* the brake K literal per index (MY SETTINGS uses the same list) */
static const u32 BK[6] = {BRAKE_STOCK, BRAKE_STOCK * 2, BRAKE_STOCK * 4,
                          BRAKE_STOCK * 8, BRAKE_STOCK * 15, BRAKE_STOCK / 2};

/* theme names (UPPER ASCII in the theme blob) -> UTF-16 here */
#define MAXT 16
static u16 tnames[MAXT][17];
static const u16 *tvals[MAXT + 1];

int ut_draw(u32 *r, u32 ui);
int ut_commit(u32 *r);

#define REC(lbl, vals, n) {(u32)(lbl), (u32)(vals), (n), 0, 0xFFFFFFFFu, 1, \
    CB_NOP, (u32)ut_draw, CB_BEGIN, CB_SEL, (u32)ut_commit, CB_Q0, CB_END}

u32 ut_tab[NSTOCK + NOURS][13] = {
    [NSTOCK + K_MODS]  = REC(L_MODS, V_MODS, 2),
    [NSTOCK + K_WAVE]  = REC(L_WAVE, V_WAVE, 4),
    [NSTOCK + K_BRAKE] = REC(L_BRAKE, V_BRAKE, 6),
    [NSTOCK + K_GATE]  = REC(L_GATE, V_GATE, 2),
    [NSTOCK + K_THEME] = REC(L_THEME, V_TNONE, 1),
};
u32 ut_draws, ut_commits;                 /* diagnostics */

struct ts_hdr {                            /* tools/themesw/ts.c blob head */
    u32 magic, nreg, ns, nt;
    u32 reg_off, s_off, d_off, name_off;
    u8 cur, target, busy, pad;
};

static struct ts_hdr *themes(void)
{
    struct ts_hdr *h = (struct ts_hdr *)TS_DATA;
    if (!R32(TS_BASE) || h->magic != 0x57535458u || h->nt == 0)
        return 0;
    return h;
}

static int kind_of(u32 *r)
{
    return (int)((r - ut_tab[NSTOCK]) / 13);
}

static u32 current(int k, u32 *r)
{
    switch (k) {
    case K_MODS:  return R8(MSOFF) ? 1 : 0;
    case K_WAVE:  { u32 v = R8(MODEB); return v < 4 ? v : 0; }
    case K_BRAKE: { u32 v = R8(MSBRAKE); return v < 6 ? v : 0; }
    case K_GATE:  return R8(MSGATE) ? 1 : 0;
    default: {
        struct ts_hdr *h = themes();
        if (!h) {
            r[1] = (u32)V_TNONE; r[2] = 1;
            return 0;
        }
        u32 nt = h->nt > MAXT ? MAXT : h->nt, i, j;
        tvals[0] = S_OFF;
        for (i = 0; i < nt; i++) {
            const u8 *s = (const u8 *)h + h->name_off + 16 * i;
            for (j = 0; j < 16 && s[j]; j++)
                tnames[i][j] = s[j] == '-' ? ' ' : s[j];
            tnames[i][j] = 0;
            tvals[i + 1] = tnames[i];
        }
        r[1] = (u32)tvals; r[2] = nt + 1;
        return h->cur <= nt ? h->cur : 0;
    }
    }
}

typedef int (*draw_t)(u32 *, u32);

int ut_draw(u32 *r, u32 ui)
{
    int k = kind_of(r), ret;
    ut_draws++;
    if (r[4] != 0xFFFFFFFFu)                 /* editing: show the choice */
        return ((draw_t)CB_DRAW)(r, ui);
    r[3] = current(k, r);
    r[4] = r[3];                             /* stock draw skips its own */
    ret = ((draw_t)CB_DRAW)(r, ui);          /* (PLAY MODE) value fetch */
    r[4] = 0xFFFFFFFFu;
    return ret;
}

static void mods(u32 off)
{
    volatile u8 *save = (volatile u8 *)MSSAVE;
    if (off == R8(MSOFF))
        return;
    if (off) {                               /* OFF: save, stock behaviour */
        R8(MSOFF) = 1;
        save[0] = R8(MODEB); R8(MODEB) = 0;
        save[1] = R8(VSELB); R8(VSELB) = 0;
        R32(BRAKE_LIT) = BRAKE_STOCK;
    } else {                                 /* ON: restore */
        R8(MSOFF) = 0;
        R8(MODEB) = save[0];
        R8(VSELB) = save[1];
        u32 b = R8(MSBRAKE);
        R32(BRAKE_LIT) = BK[b < 6 ? b : 0];
    }
}

typedef void (*fn_t)(void);

int ut_commit(u32 *r)
{
    int k = kind_of(r);
    u32 v = r[4];
    if (v >= (r[2] & 0xFFFF) || v == r[3])
        return 0;
    if (k != K_MODS && R8(MSOFF))            /* like MY SETTINGS: the other */
        return 0;                            /* rows rest while MODS = OFF */
    ut_commits++;
    switch (k) {
    case K_MODS:  mods(v); break;
    case K_WAVE:  R8(MODEB) = (u8)v; break;
    case K_BRAKE: R8(MSBRAKE) = (u8)v; R32(BRAKE_LIT) = BK[v]; break;
    case K_GATE:  R8(MSGATE) = (u8)v; break;
    default: {
        struct ts_hdr *h = themes();
        if (!h || h->busy)
            return 0;
        h->target = (u8)v;
        ((fn_t)R32(TS_BASE + 4))();          /* ts_apply: recolour now */
        break;
    }
    }
    r[3] = v;
    return 1;
}
