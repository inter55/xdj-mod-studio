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
    || !defined(TS_BASE) || !defined(TS_DATA) || !defined(BRAKE_STOCK) \
    || !defined(PM_MODE) || !defined(PM_PREV) || !defined(TF_HEAD) \
    || !defined(TF_CFG) || !defined(TF_PREV) || !defined(MSHCCOL)
#error address defines missing (tools/utility/build.sh)
#endif

#define NSTOCK 16
#define NOURS  13
#define CB_NOP   0x08DD0C6Au
#define CB_DRAW  0x08DD0C6Eu       /* PLAY MODE draw: label + values[idx] */
#define CB_BEGIN 0x08DD198Cu
#define CB_SEL   0x08DD1AD0u
#define CB_Q0    0x08DD20A0u
#define CB_END   0x08DD20E2u

/* the TRACK FILTER rows come first so the mod rows keep their places
 * at the end of the list (tests count them from the end) */
enum { K_TF, K_TBPM, K_TRNG, K_TKEY, K_TCOL, K_TRAT,
       K_MODS, K_WAVE, K_PHASE, K_BRAKE, K_GATE, K_HCCOL, K_THEME };
#define K_TF_LAST K_TRAT

static const u16 L_MODS[] = u"MODS";
static const u16 L_WAVE[] = u"WAVEFORM COLOR";
static const u16 L_PHASE[] = u"PHASE METER";
static const u16 L_BRAKE[] = u"VINYL BRAKE LENGTH";
static const u16 L_GATE[] = u"HOT CUE GATE";
static const u16 L_HCCOL[] = u"HOT CUE COLORS";
static const u16 L_THEME[] = u"THEME";
static const u16 L_TF[] = u"TRACK FILTER";
static const u16 L_TBPM[] = u"FILTER BPM";
static const u16 L_TRNG[] = u"FILTER BPM RANGE";
static const u16 L_TKEY[] = u"FILTER RELATED KEY";
static const u16 L_TCOL[] = u"FILTER COLOR";
static const u16 L_TRAT[] = u"FILTER RATING";

static const u16 S_ON[] = u"ON", S_OFF[] = u"OFF", S_GATE[] = u"GATE";
static const u16 S_BLUE[] = u"BLUE/THEME",  /* 2026-11-12 user: blue follows the theme */ S_RGB[] = u"RGB", S_3B[] = u"3-BAND";
static const u16 S_B1[] = u"1X", S_B2[] = u"2X", S_B4[] = u"4X",
                 S_B8[] = u"8X", S_B15[] = u"15X", S_B05[] = u"0.5X";
static const u16 S_NONE[] = u"NOT INSTALLED";

static const u16 *const V_MODS[] = {S_ON, S_OFF};
static const u16 *const V_WAVE[] = {S_BLUE, S_RGB, S_3B};
static const u16 *const V_BRAKE[] = {S_B1, S_B2, S_B4, S_B8, S_B15, S_B05};
static const u16 *const V_GATE[] = {S_OFF, S_GATE};
/* 2026-11-22: colours of the overview hot cue markers (tools/hotcue8 hc.c
 * markers(), byte MSHCCOL): every letter its own / A-C in the deck's colour /
 * all eight in the deck's colour */
static const u16 S_HC8[] = u"8 COLORS", S_HCA[] = u"A-C STOCK", S_HCS[] = u"ALL STOCK";
static const u16 *const V_HCCOL[] = {S_HC8, S_HCA, S_HCS};
/* like the XDJ-1000MK2: TYPE 1 = scrolling beat ruler (tools/phasemeter),
 * TYPE 2 = the deck's own meter. pm_mode 1 / 0. */
static const u16 S_T1[] = u"TYPE 1", S_T2[] = u"TYPE 2";
static const u16 *const V_PHASE[] = {S_T1, S_T2};
#define PM_IN (R32(PM_PREV) != 0)              /* installer sets pm_prev */
static const u16 *const V_TNONE[] = {S_NONE};

/* TRACK FILTER (tools/trackfilter/tf.c): settings struct at TF_CFG
 * read with tf_get(k) / written with tf_put(k, v) (header +16 / +20):
 * k 0 on, 1 bpm (0 off, else BPM), 2 +/- percent 0..6, 3 key (0 off,
 * 1..12 = 1A..12A, 13..24 = 1B..12B; matches related keys), 4 colour
 * bitmask (this row picks one), 5 exact stars. The conditions live in
 * the deck's own filter store and are saved on the stick (RBFLTR.DAT). */
#define TF_IN (R32(TF_HEAD) == 0x544C4654u && R32(TF_PREV) != 0)
#define BPM_LO 60
#define BPM_HI 200
#define NBPM (BPM_HI - BPM_LO + 2)
static const u16 *const V_TF[] = {S_OFF, S_ON};
static u16 bpmtxt[NBPM - 1][4];
static const u16 *bpmv[NBPM];
static const u16 R0[] = u"+-0%", R1[] = u"+-1%", R2[] = u"+-2%", R3[] = u"+-3%",
                 R4[] = u"+-4%", R5[] = u"+-5%", R6[] = u"+-6%";
static const u16 *const V_TRNG[] = {R0, R1, R2, R3, R4, R5, R6};
static const u16 K1A[] = u"Abm (1A)", K2A[] = u"Ebm (2A)", K3A[] = u"Bbm (3A)",
    K4A[] = u"Fm (4A)", K5A[] = u"Cm (5A)", K6A[] = u"Gm (6A)", K7A[] = u"Dm (7A)",
    K8A[] = u"Am (8A)", K9A[] = u"Em (9A)", K10A[] = u"Bm (10A)", K11A[] = u"F#m (11A)",
    K12A[] = u"Dbm (12A)", K1B[] = u"B (1B)", K2B[] = u"F# (2B)", K3B[] = u"Db (3B)",
    K4B[] = u"Ab (4B)", K5B[] = u"Eb (5B)", K6B[] = u"Bb (6B)", K7B[] = u"F (7B)",
    K8B[] = u"C (8B)", K9B[] = u"G (9B)", K10B[] = u"D (10B)", K11B[] = u"A (11B)",
    K12B[] = u"E (12B)";
static const u16 *const V_TKEY[] = {S_OFF, K1A, K2A, K3A, K4A, K5A, K6A, K7A, K8A,
    K9A, K10A, K11A, K12A, K1B, K2B, K3B, K4B, K5B, K6B, K7B, K8B, K9B, K10B,
    K11B, K12B};
static const u16 C1[] = u"PINK", C2[] = u"RED", C3[] = u"ORANGE", C4[] = u"YELLOW",
    C5[] = u"GREEN", C6[] = u"AQUA", C7[] = u"BLUE", C8[] = u"PURPLE";
static const u16 *const V_TCOL[] = {S_OFF, C1, C2, C3, C4, C5, C6, C7, C8};
static const u16 S1[] = u"1 STAR", S2[] = u"2 STARS", S3[] = u"3 STARS",
    S4[] = u"4 STARS", S5[] = u"5 STARS";
static const u16 *const V_TRAT[] = {S_OFF, S1, S2, S3, S4, S5};
static const u8 TF_N[] = {2, NBPM, 7, 25, 9, 6};      /* values per row */

static const u16 *const *tf_vals(int k)
{
    switch (k) {
    case K_TF:   return V_TF;
    case K_TBPM:
        if (!bpmv[0]) {
            u32 i, b;
            for (i = 0; i < NBPM - 1; i++) {
                b = BPM_LO + i;
                bpmtxt[i][0] = b >= 100 ? '0' + b / 100 : '0' + (b / 10) % 10;
                bpmtxt[i][1] = b >= 100 ? '0' + (b / 10) % 10 : '0' + b % 10;
                bpmtxt[i][2] = b >= 100 ? '0' + b % 10 : 0;
                bpmtxt[i][3] = 0;
                bpmv[i + 1] = bpmtxt[i];
            }
            bpmv[0] = S_OFF;
        }
        return bpmv;
    case K_TRNG: return V_TRNG;
    case K_TKEY: return V_TKEY;
    case K_TCOL: return V_TCOL;
    default:     return V_TRAT;
    }
}
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
    [NSTOCK + K_TF]    = REC(L_TF, V_TNONE, 1),
    [NSTOCK + K_TBPM]  = REC(L_TBPM, V_TNONE, 1),
    [NSTOCK + K_TRNG]  = REC(L_TRNG, V_TNONE, 1),
    [NSTOCK + K_TKEY]  = REC(L_TKEY, V_TNONE, 1),
    [NSTOCK + K_TCOL]  = REC(L_TCOL, V_TNONE, 1),
    [NSTOCK + K_TRAT]  = REC(L_TRAT, V_TNONE, 1),
    [NSTOCK + K_MODS]  = REC(L_MODS, V_MODS, 2),
    [NSTOCK + K_WAVE]  = REC(L_WAVE, V_WAVE, 3),
    [NSTOCK + K_PHASE] = REC(L_PHASE, V_PHASE, 2),
    [NSTOCK + K_BRAKE] = REC(L_BRAKE, V_BRAKE, 6),
    [NSTOCK + K_GATE]  = REC(L_GATE, V_GATE, 2),
    [NSTOCK + K_HCCOL] = REC(L_HCCOL, V_HCCOL, 3),
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
    if (k <= K_TF_LAST) {
        if (!TF_IN) { r[1] = (u32)V_TNONE; r[2] = 1; return 0; }
        u32 v = (u32)((int (*)(int))R32(TF_HEAD + 16))(k), n = TF_N[k];
        r[1] = (u32)tf_vals(k); r[2] = n;
        if (k == K_TBPM)
            v = v >= BPM_LO && v <= BPM_HI ? v - BPM_LO + 1 : 0;
        if (k == K_TCOL) {                   /* bitmask -> lowest colour */
            u32 c = 0;
            while (c < 8 && !(v & (1u << c)))
                c++;
            v = c < 8 ? c + 1 : 0;
        }
        return v < n ? v : 0;
    }
    switch (k) {
    case K_MODS:  return R8(MSOFF) ? 1 : 0;
    case K_WAVE:  { u32 v = R8(MODEB); return v < 3 ? v : 0; }
    case K_BRAKE: { u32 v = R8(MSBRAKE); return v < 6 ? v : 0; }
    case K_GATE:  return R8(MSGATE) ? 1 : 0;
    case K_HCCOL: { u32 v = R8(MSHCCOL); return v < 3 ? v : 0; }
    case K_PHASE:
        if (!PM_IN) { r[1] = (u32)V_TNONE; r[2] = 1; return 0; }
        r[1] = (u32)V_PHASE; r[2] = 2;
        return R32(PM_MODE) == 1 ? 0 : 1;
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
        /* 2026-11-03 deck: LIGHT without the light add-on (tools/lighttext)
         * = flipped pictures + WHITE text on a light screen (unreadable).
         * Offer LIGHT only once the add-on has armed itself for it
         * (lt_idx == LIGHT's number, written last by its installer). */
        if (nt && R32(LT_IDX) != nt) {
            const u8 *s = (const u8 *)h + h->name_off + 16 * (nt - 1);
            if (s[0] == 'L' && s[1] == 'I' && s[2] == 'G' && s[3] == 'H' && s[4] == 'T')
                nt--;
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
    if (k <= K_TF_LAST) {
        if (!TF_IN)
            return 0;
        ((int (*)(int, int))R32(TF_HEAD + 20))(k, (int)(k == K_TBPM && v ? v - 1 + BPM_LO
                              : k == K_TCOL && v ? 1u << (v - 1) : v));
        r[3] = v;
        return 1;
    }
    switch (k) {
    case K_MODS:  mods(v); break;
    case K_WAVE:  R8(MODEB) = (u8)v; break;
    case K_BRAKE: R8(MSBRAKE) = (u8)v; R32(BRAKE_LIT) = BK[v]; break;
    case K_GATE:  R8(MSGATE) = (u8)v; break;
    case K_HCCOL: R8(MSHCCOL) = (u8)v; break;
    case K_PHASE:
        if (!PM_IN)
            return 0;
        R32(PM_MODE) = v == 0 ? 1u : 0u;
        break;
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
