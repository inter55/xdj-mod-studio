/* tf.c -- on-deck TRACK FILTER for the XDJ-700 (fw 1.15), the XDJ-1000MK2's
 * TRACK FILTER made to work on the 700 with the 1000MK2's own code.
 *
 * The 700's firmware already carries the 1000MK2's whole filter back end
 * (docs/TRACK_FILTER_1.1.md, 2026-11-22 evening):
 *   - a condition store at TF_ST (0x09603674): the deck's own set at +4
 *     (BPM / KEY / RATING / COLOR entries of 0x1C bytes) and one copy per
 *     player at +0xFC + i*0xE4; a change counter at +4;
 *   - SETCOND 0x08B34D5A(0x300, type, 0, len, block, &changed): put one
 *     condition (the FCND block of RBFLTR.DAT, little-endian: flags byte
 *     (bit 0 = ticked), operator byte, u16 count, u32 values) into it;
 *   - PRED 0x08B357CC(3, player, record): the 1000MK2's "does this track
 *     pass" check on a DB content record;
 *   - FilterFileWrite_TASK: reads PIONEER/rekordbox/RBFLTR.DAT at mount
 *     and writes it back about a second after the store changes -- so the
 *     conditions are saved on each USB stick, and rekordbox's own filter
 *     settings for that stick are honoured.
 * Only the screen and the list step were missing on the 700. This module
 * adds the list step; the UTILITY rows / filter screen call tf_put.
 *
 * How the deck builds a browse list:
 *   the DB task (DBSMain) copies a query result into the browse list
 *   buffer (DJDBLEFTBUF) with FUN_08CDFC8C(dst, src); dst: +2 u16 row
 *   count, +4 u16 total, +0x4C rows of 20 bytes. A track row is
 *   {u32 content id, ..., +0x12 u8 kind}; kind 1..5 and 0x16..0x18 are
 *   DJDBCONTENT columns. tf_copy wraps that routine (its three literal-
 *   pool words point here): run the stock copy, then compact the rows
 *   that fail PRED. Records come from the deck's DB engine, called the
 *   way the deck's own marker FUN_08CE21FA does it (DB task only):
 *   h = Q(&err, "djdbContent", "idxContent", 0, 0, 0, "=", 1, &key);
 *   rec = F(h, &err); PRED(3, 0, rec); C(h)
 */
typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;

#define R8(a)  (*(volatile u8 *)(a))
#define R16(a) (*(volatile u16 *)(a))
#define R32(a) (*(volatile u32 *)(a))

#ifndef MSOFF
#error MSOFF missing (tools/trackfilter/build.sh)
#endif

#define STOCK_COPY 0x08CDFC8Cu
#define ENG_Q 0x08E120A4u
#define ENG_F 0x08E12C90u
#define ENG_G 0x08E12DB0u
#define ENG_C 0x08E129B2u
#define PRED 0x08B357CCu
#define SETCOND 0x08B34D5Au
#ifndef TF_ST
#define TF_ST 0x09603674u
#endif
#define PLAYER 0
#define ENT(k) (TF_ST + 8 + 0x1C * (k))                  /* condition copy the check reads */

typedef int (*copy_t)(u32, u32);
typedef int (*q_t)(int *, const char *, const char *, u32, u32, u32,
                   const char *, u32, u32 *);
typedef u32 (*f_t)(int, int *);
typedef u32 (*g_t)(u32, int, int *);
typedef void (*c_t)(int);
typedef int (*pred_t)(int, int, u32);
typedef int (*set_t)(u32, int, int, u32, u32, u32 *);

/* the filter's on/off switch (the 1000MK2's TRACK FILTER tap); the
 * conditions themselves live in the deck's condition store (tf_get/tf_put) */
struct cfg {
    u8 on;
    u8 pad[7];
};
struct cfg tf_cfg;

/* 2026-11-23 deck: something wrote 0x80-ish bytes over this module's state
 * (tfs_mode read 0x80808080 -> the screen opened by itself, went black,
 * the deck crashed on BACK). Every entry checks the state is sane first; if
 * not, the module switches itself off for good (tf_dead lives in .data,
 * part of the code image) and the deck behaves stock. */
u32 tf_dead __attribute__((section(".data")));
extern u32 tfs_mode, tfs_edit, tfs_tab, tfs_relist;
int tf_sane(void)
{
    if (tf_dead)
        return 0;
    if (tf_cfg.on > 1 || tfs_mode > 1 || tfs_edit > 3 || tfs_tab > 1 || tfs_relist > 3) {
        tf_dead = 1;
        return 0;
    }
    return 1;
}
u32 tf_prev;                      /* the copy routine (installer sets) */
/* diagnostics: calls, lists filtered, rows in, rows kept, lookup fails */
/* ring of the last 8 copies: dst, src, src rows, src perm, n in, n kept */
u32 tf_log[8][6], tf_logi;
u32 tf_calls, tf_lists, tf_in, tf_kept, tf_fail, tf_last_n, tf_last_kept;

static const char T_CONT[] = "djdbContent", I_CONT[] = "idxContent";
static const char OP_EQ[] = "=";

/* ---- per-row cache for the screen's live [count] -------------------
 * tf_copy fills it for the newest list that holds tracks (its unfiltered
 * rows); the screen counts matches with the same rules as PRED (checked
 * against PRED in the emulator test): tempo | key number << 16 | colour
 * << 21 | rating << 25. Lists longer than TF_NA: no live count. */
#define TF_NA 1536
u32 tf_attr[TF_NA], tf_na, tf_naver, tf_tracks;   /* tracks: newest list has tracks */
u32 tf_tb[TF_NA];        /* per cached row: bit i = the track has tf_mt[i] */
u32 tf_left;            /* the list buffer the browse shows (a copy from a sorted DB result goes there) */

static const char T_KEY[] = "djdbKey", I_KEY[] = "idxKey";
/* the 1000MK2 key number (Camelot nA = 2n-1, nB = 2n) for a key name,
 * musical spelling only ("Am", "F#", "Bbm"); others 0 = no key */
static const u8 CAM_MAJ[12] = {8, 3, 10, 5, 12, 7, 2, 9, 4, 11, 6, 1};
static u32 key_num(const u16 *s)
{
    static const u8 PC[7] = {9, 11, 0, 2, 4, 5, 7};       /* A..G */
    if (!s)
        return 0;
    u16 c = s[0] | 0x20;
    if (c < 'a' || c > 'g')
        return 0;
    int pc = PC[c - 'a'], i = 1;
    if (s[1] == '#') { pc++; i = 2; }
    else if (s[1] == 'b') { pc--; i = 2; }
    pc = (pc + 12) % 12;
    if ((s[i] == 'm' || s[i] == 'M') && (s[i + 1] | 0x20) != 'a')
        return 2u * CAM_MAJ[(pc + 3) % 12] - 1;          /* minor: nA */
    return 2u * CAM_MAJ[pc];                              /* major: nB */
}
#define NKC 128
static u8 kcache[NKC];                    /* 0 = not looked up, else num + 1 */
static u32 key_of(u32 kid)
{
    if (!kid)
        return 0;
    if (kid < NKC && kcache[kid])
        return kcache[kid] - 1u;
    int err = 0, len = 0;
    u32 val = kid, key[2] = {(u32)&val, 4}, e = 0;
    int h = ((q_t)ENG_Q)(&err, T_KEY, I_KEY, 0, 0, 0, OP_EQ, 1, key);
    if (h) {
        u32 rec = ((f_t)ENG_F)(h, &err);
        if (rec && !err)
            e = key_num((const u16 *)((g_t)ENG_G)(rec, 1, &len));
        ((c_t)ENG_C)(h);
    }
    if (kid < NKC)
        kcache[kid] = (u8)(e + 1);
    return e;
}


static const char T_SMT[] = "djdbexSongMyTag", I_SMTC[] = "idxExtSongMyTagContentID";
static const char T_MT[] = "djdbexMyTag", I_MT[] = "idxExtMyTagMyTagID";
#ifdef TF_PROBE
/* diagnostic build only: what the My Tag tables give back in the DB task */
u32 tf_pb[256], tf_pbn;
static void pbw(u32 v) { if (tf_pbn < 256) tf_pb[tf_pbn++] = v; }
static void probe(const char *t, const char *ix, u32 key)
{
    int err = 0, len = 0, k, col, i;
    u32 val = key, kk[2] = {(u32)&val, 4};
    int h = ((q_t)ENG_Q)(&err, t, ix, 0, 0, 0, OP_EQ, 1, kk);
    pbw(0xAAAA0000u | key); pbw((u32)h); pbw((u32)err);
    if (!h) return;
    for (k = 0; k < 4; k++) {
        err = 0;
        u32 rec = ((f_t)ENG_F)(h, &err);
        pbw(0xBBBB0000u | k); pbw(rec); pbw((u32)err);
        if (!rec || err) break;
        for (col = 0; col < 4; col++) {
            len = 0;
            u32 p = ((g_t)ENG_G)(rec, col, &len);
            pbw(p); pbw((u32)len);
            for (i = 0; i < 6; i++)
                pbw(p >= 0x08000000u && p < 0x10000000u ? R32((p & ~3u) + 4 * i - 8) : 0);
        }
    }
    ((c_t)ENG_C)(h);
}
#endif


/* ---- MY TAG (exportExt.pdb, read through the deck's own DB engine) -------
 * djdbexSongMyTag by content id: one record per tag on the track, column 0
 * -> the tag id. djdbexMyTag by tag id: column 0 -> the raw row's id field
 * (row + 0x14): +0x0C column id, +0x10 position, +0x1B 1 = a column,
 * +0x1D offset of the name (DeviceSQL string). Like the XDJ-1000MK2 the
 * screen lists only the tags tracks use, by column, in position order, and
 * the choices are not saved on the stick (RBFLTR.DAT has no My Tag block;
 * the 1000MK2 and the 700's own writer write the four BPM/KEY/RATING/COLOR
 * blocks only). Rules (the 1000MK2's, measured + its checks 0x08B3623E AND /
 * 0x08B362BC OR): per ticked column with tags chosen, AND = all of them on
 * the track, OR = any; columns AND'ed; a ticked column with none chosen
 * does not filter. */
#define MT_MAX 32
#define MT_NAME 24
#define MT_SEL 8
struct mtag { u32 id; u8 col, pos, pad[2]; char name[MT_NAME]; };
struct mtag tf_mt[MT_MAX];
u32 tf_mtn, tf_mtover;                     /* tags seen in the newest list */
struct mcol { u32 id, pos; char name[MT_NAME]; };
struct mcol tf_mc[4];
u32 tf_mcn;                                /* My Tag columns (position order) */
struct mcond { u8 tick, orr, n, pad; u32 id[MT_SEL]; };
struct mcond tf_mcond[4];                  /* per column, RAM only */

static void ds_name(u32 s, char *out)      /* DeviceSQL string -> ASCII */
{
    u32 b = R8(s), n, i, st = 1, k = 0;
    if (b & 1) n = (b >> 1) - 1;                       /* short ASCII */
    else if (b == 0x40 || b == 0x90) {                 /* long ASCII / UTF-16 */
        n = (R8(s + 1) | R8(s + 2) << 8) - 4; st = 4;
        if (b == 0x90) { n /= 2; k = 1; }
    } else n = 0;
    if (n > MT_NAME - 1) n = MT_NAME - 1;
    for (i = 0; i < n; i++) {
        u32 c = k ? R8(s + st + 2 * i) | R8(s + st + 2 * i + 1) << 8 : R8(s + st + i);
        out[i] = c >= 32 && c < 127 ? (char)c : '?';
    }
    out[n] = 0;
}

/* one djdbexMyTag row by id: column id, position, name; 0 = not found */
static int mt_row(u32 id, u32 *col, u32 *pos, char *name, int *iscol)
{
    int err = 0, len = 0, ok = 0;
    u32 val = id, key[2] = {(u32)&val, 4};
    int h = ((q_t)ENG_Q)(&err, T_MT, I_MT, 0, 0, 0, OP_EQ, 1, key);
    if (!h)
        return 0;
    u32 rec = ((f_t)ENG_F)(h, &err);
    if (rec && !err) {
        u32 p = ((g_t)ENG_G)(rec, 0, &len);
        if (p >= 0x08000000u && p < 0x10000000u && R32(p) == id) {
            u32 row = p - 0x14;
            *col = R32(row + 0x0C); *pos = R32(row + 0x10);
            *iscol = R8(row + 0x1B) != 0;
            ds_name(row + R8(row + 0x1D), name);
            ok = 1;
        }
    }
    ((c_t)ENG_C)(h);
    return ok;
}

static void mt_cols(void)                  /* columns 1..4 in position order */
{
    u32 id, col, pos, i, j;
    int isc;
    tf_mcn = 0;
    for (id = 1; id <= 8 && tf_mcn < 4; id++) {
        struct mcol c;
        if (!mt_row(id, &col, &pos, c.name, &isc) || !isc)
            continue;
        c.id = id; c.pos = pos;
        for (i = tf_mcn; i > 0 && tf_mc[i - 1].pos > pos; i--)
            tf_mc[i] = tf_mc[i - 1];
        tf_mc[i] = c;
        tf_mcn++;
    }
    (void)j;
}

/* the track's tags (up to n ids), -1 if the table is not there */
static int song_tags(u32 cid, u32 *out, int n)
{
    int err = 0, len = 0, k = 0;
    u32 val = cid, key[2] = {(u32)&val, 4};
    int h = ((q_t)ENG_Q)(&err, T_SMT, I_SMTC, 0, 0, 0, OP_EQ, 1, key);
    if (!h)
        return -1;
    while (k < n) {
        err = 0;
        u32 rec = ((f_t)ENG_F)(h, &err);
        if (!rec || err)
            break;
        u32 p = ((g_t)ENG_G)(rec, 0, &len);
        if (p >= 0x08000000u && p < 0x10000000u)
            out[k++] = R32(p);
    }
    ((c_t)ENG_C)(h);
    return k;
}

static int mt_index(u32 id)                /* tf_mt index, adding a new tag */
{
    u32 i, col, pos;
    int isc;
    for (i = 0; i < tf_mtn; i++)
        if (tf_mt[i].id == id)
            return (int)i;
    if (tf_mtn >= MT_MAX) { tf_mtover = 1; return -1; }
    struct mtag *t = &tf_mt[tf_mtn];
    if (!mt_row(id, &col, &pos, t->name, &isc) || isc)
        return -1;
    t->id = id; t->pos = (u8)(pos > 255 ? 255 : pos); t->col = 0xFF;
    for (i = 0; i < tf_mcn; i++)
        if (tf_mc[i].id == col)
            t->col = (u8)i;
    return (int)tf_mtn++;
}

int tf_mt_active(void)
{
    int c;
    for (c = 0; c < 4; c++)
        if (tf_mcond[c].tick && tf_mcond[c].n)
            return 1;
    return 0;
}

/* the My Tag rule on a track's tag list */
static int mt_pass(const u32 *tags, int nt)
{
    int c, i, j;
    for (c = 0; c < 4; c++) {
        struct mcond *m = &tf_mcond[c];
        if (!m->tick || !m->n)
            continue;
        int hit = 0;
        for (i = 0; i < m->n; i++) {
            for (j = 0; j < nt; j++)
                if (tags[j] == m->id[i])
                    break;
            if (j < nt) hit++;
            else if (!m->orr) return 0;
        }
        if (m->orr && !hit)
            return 0;
    }
    return 1;
}

/* the same rule on a cached row's tag bits */
int tf_mt_match(u32 bits)
{
    int c, i, j;
    for (c = 0; c < 4; c++) {
        struct mcond *m = &tf_mcond[c];
        if (!m->tick || !m->n)
            continue;
        int hit = 0;
        for (i = 0; i < m->n; i++) {
            for (j = 0; j < (int)tf_mtn; j++)
                if (tf_mt[j].id == m->id[i])
                    break;
            int has = j < (int)tf_mtn && ((bits >> j) & 1);
            if (has) hit++;
            else if (!m->orr) return 0;
        }
        if (m->orr && !hit)
            return 0;
    }
    return 1;
}

/* look one track up: its attributes into *attr; PRED's answer if asked */
static int keep(u32 id, int filt, u32 *attr, u32 *tbits)
{
    int err = 0, len = 0, r = 1;
    {   /* My Tag: the track's tags -> bits of tf_mt, and the rule */
        u32 tags[16];
        int nt = song_tags(id, tags, 16), i, x;
        *tbits = 0;
        if (nt < 0) nt = 0;
        for (i = 0; i < nt; i++)
            if ((x = mt_index(tags[i])) >= 0)
                *tbits |= 1u << x;
        if (filt && !mt_pass(tags, nt))
            r = 0;
    }
    u32 val = id;
    u32 key[2] = {(u32)&val, 4};
    *attr = 0xFFFFFFFFu;
    int h = ((q_t)ENG_Q)(&err, T_CONT, I_CONT, 0, 0, 0, OP_EQ, 1, key);
    if (!h) {
        tf_fail++;                       /* no record: never hide it */
        return 1;
    }
    u32 rec = ((f_t)ENG_F)(h, &err);
    if (rec && !err) {
        u32 p = ((g_t)ENG_G)(rec, 0, &len);
        if (p >= 0x08000000u && p < 0x10000000u && R32(p) == id) {
            u32 row = p - 0x48, rt = R8(row + 0x59);
            if (rt > 5)
                rt /= 51;
            *attr = (R32(row + 0x38) & 0xFFFF) | key_of(R32(row + 0x20)) << 16 |
                    (R8(row + 0x58) & 15u) << 21 | (rt & 7u) << 25;
        }
        if (filt && r)
            r = ((pred_t)PRED)(3, PLAYER, rec) == 1;
    } else
        tf_fail++;
    ((c_t)ENG_C)(h);
    return r;
}

/* ---- the conditions, in the deck's store (own set at TF_ST + 4) ----
 * entry k (0 BPM, 1 KEY, 2 RATING, 3 COLOR) at TF_ST + 8 + 0x1C*k:
 *   +4 ticked, +0xE u16 count, +0x10 values, +0x14 operator          */
static const u8 TYPE[4] = {6, 0x0C, 7, 0x0F};
static const u8 OPER[4] = {6, 0x11, 0, 0x11};

u32 tf_val(int k, int i)
{
    u32 n = R16(ENT(k) + 0xE), p = R32(ENT(k) + 0x10);
    if ((u32)i >= n || p < 0x08000000u || p >= 0x10000000u)
        return 0;
    return R32(p + 4 * i);
}
int tf_tick(int k) { return R32(ENT(k) + 4) == 1; }
int tf_vn(int k) { return R16(ENT(k) + 0xE); }

static int put(int k, int tick, const u32 *v, int n)
{
    u32 blk[1 + 8], changed = 0;
    int i;
    blk[0] = (tick ? 1 : 0) | OPER[k] << 8 | n << 16;
    for (i = 0; i < n; i++)
        blk[1 + i] = v[i];
    return ((set_t)SETCOND)(0x300, TYPE[k], 0, 4 + 4 * n, (u32)blk, &changed);
}

/* the store's BPM / percent (defaults 130 / 6 when it holds nothing) */
u32 tf_bpm(void)
{
    u32 lo = tf_val(0, 0), hi = tf_val(0, 1);
    return lo && hi >= lo ? (lo + hi) / 200 : 130;
}
u32 tf_pct(void)
{
    u32 lo = tf_val(0, 0), hi = tf_val(0, 1);
    return lo && hi >= lo ? (hi - lo) * 100 / (lo + hi) : 6;
}
u32 tf_key(void)                          /* the key number, C if none */
{
    u32 e = tf_val(1, 0);
    return e >= 1 && e <= 24 ? e : 16;
}

static u32 knum(u32 n, int major)       /* n wraps 0..13 -> 1..12 */
{
    n = (n + 11) % 12 + 1;
    return 2 * n - (major ? 0 : 1);
}

/* the setters the screen and the UTILITY rows use (as the 1000MK2 stores
 * them: BPM = low/high x 100, KEY = the key + its relative + a fifth down
 * + a fifth up, RATING = {stars, 0}, COLOR = bitmask) */
int tf_setb(int tick, u32 bpm, u32 pct)
{
    u32 w[2];
    if (pct > 6) pct = 6;
    w[0] = bpm * (100 - pct); w[1] = bpm * (100 + pct);
    return put(0, tick, w, 2);
}
int tf_setk(int tick, u32 e)
{
    u32 w[4], n = (e + 1) / 2;
    int major = !(e & 1);
    w[0] = e; w[1] = knum(n, !major); w[2] = knum(n - 1, major); w[3] = knum(n + 1, major);
    return put(1, tick, w, 4);
}
int tf_setr(int tick, u32 stars)
{
    u32 w[2] = {stars, 0};
    return put(2, tick, w, 2);
}
int tf_setc(int tick, u32 mask)
{
    u32 w = mask & 0xFF;
    return put(3, tick, &w, 1);
}

/* the 1000MK2's rules (PRED 0x08B357CC and its four checks 0x08B35F6C
 * between, 0x08B3602A in list, 0x08B36138 equals, 0x08B361F8 colour bit)
 * on one cached row */
int tf_match(u32 a)
{
    if (a == 0xFFFFFFFFu)
        return 1;
    if (tf_tick(0)) {
        u32 t = a & 0xFFFF, lo = tf_val(0, 0), hi = tf_val(0, 1);
        if (tf_vn(0) < 2)
            return 0;
        if (lo == hi) { if (t + 50 < lo + 0 || t > lo + 49) return 0; }
        else if (t < lo || t > hi) return 0;
    }
    if (tf_tick(1)) {
        u32 e = a >> 16 & 31, n = (u32)tf_vn(1), i;
        for (i = 0; i < n && i < 8; i++)
            if (e == tf_val(1, (int)i))
                break;
        if (i >= n || i >= 8)
            return 0;
    }
    if (tf_tick(2) && (!tf_vn(2) || (a >> 25 & 7) != tf_val(2, 0)))
        return 0;
    if (tf_tick(3) && tf_vn(3)) {
        u32 c = a >> 21 & 15, m = tf_val(3, 0);
        if (m && !(c && (m & (1u << (c - 1)))))
            return 0;
    }
    return 1;
}

int tf_count(void)                         /* -1 = list too long to count */
{
    u32 i, n = 0;
    int mt = tf_mt_active();
    if (tf_na > TF_NA || (mt && tf_mtover))
        return -1;
    for (i = 0; i < tf_na; i++)
        n += (u32)(tf_match(tf_attr[i]) && (!mt || tf_mt_match(tf_tb[i])));
    return (int)n;
}

/* k: 0 on, 1 bpm (0 off, else BPM), 2 +/- percent, 3 key (0 off, our
 * Camelot code 1..12 = 1A..12A, 13..24 = 1B..12B), 4 colour bitmask (0 off),
 * 5 stars (0 off) -- the UTILITY rows */
int tf_get(int k)
{
    switch (k) {
    case 0: return tf_cfg.on;
    case 1: return tf_tick(0) ? (int)tf_bpm() : 0;
    case 2: return (int)tf_pct();
    case 3: {
        u32 e = tf_key();
        return tf_tick(1) ? (int)((e & 1) ? (e + 1) / 2 : e / 2 + 12) : 0;
    }
    case 4: return tf_tick(3) ? (int)(tf_val(3, 0) & 0xFF) : 0;
    case 5: return tf_tick(2) ? (int)tf_val(2, 0) : 0;
    }
    return 0;
}

int tf_put(int k, int v)
{
    switch (k) {
    case 0: tf_cfg.on = v ? 1 : 0; return 0;
    case 1: return tf_setb(v != 0, v ? (u32)v : tf_bpm(), tf_pct());
    case 2: return tf_setb(tf_tick(0), tf_bpm(), (u32)v);
    case 3:
        if (v < 1 || v > 24)
            return tf_setk(0, tf_key());
        return tf_setk(1, v > 12 ? 2u * (u32)(v - 12) : 2u * (u32)v - 1);
    case 4: return tf_setc(v != 0, v ? (u32)v : tf_val(3, 0));
    case 5: {
        u32 r = tf_val(2, 0);
        return tf_setr(v >= 1 && v <= 5, v >= 1 && v <= 5 ? (u32)v : (r ? r : 3));
    }
    }
    return -1;
}

static int is_track(u32 row)
{
    u8 k = R8(row + 0x12);
    return (k >= 1 && k <= 5) || (k >= 0x16 && k <= 0x18);
}

/* Lists we filtered: the browse buffer (dst) and the DB result it came
 * from (src, with its rows pointer, count and sort permutation). On BACK
 * the deck saves the shown list into that same result with this same
 * copy routine (which also frees the result's sort permutation): with a
 * filtered list that would make the drop permanent, so the next visit
 * could never show the tracks again after the filter changes. When the
 * result is still exactly the one we filtered from, the save is skipped:
 * the result keeps the full sorted list and the next visit filters it
 * again with the settings of that moment (= Apply on the XDJ-1000MK2). */
#define NTRK 4
struct trk { u32 dst, src, rows, n, perm; };
static struct trk trk[NTRK];
u32 tf_skips;

static struct trk *tracked(u32 dst)
{
    int i;
    for (i = 0; i < NTRK; i++)
        if (trk[i].dst == dst)
            return &trk[i];
    return 0;
}

extern u32 tfs_backt;                    /* 1 ms clock at the last BACK press */
int tf_copy(u32 dst, u32 src)
{
    if (!tf_sane())                      /* stock routine: tf_prev is state too */
        return ((copy_t)STOCK_COPY)(dst, src);
    struct trk *t = tracked(src);
    if (t && t->src == dst && R32(dst + 0x4C) == t->rows && R16(dst + 2) == t->n
        && R32(dst + 0x34) == t->perm) {
        tf_skips++;                          /* the BACK save of a filtered list */
        tf_left = src;                       /* (so src is the shown list) */
        tfs_backt = 0;
        t->dst = 0;
        return 0;                            /* (the stock routine returns 0) */
    }
    t = tracked(dst);
    if (t && src != dst)                     /* dst gets new content: forget */
        t->dst = 0;                          /* (an empty list is copied onto
                                                itself once: keep tracking) */
    u32 *lg = tf_log[tf_logi++ & 7];
    lg[0] = dst; lg[1] = src; lg[2] = R32(src + 0x4C); lg[3] = R32(src + 0x34);
    int ret = ((copy_t)tf_prev)(dst, src);
    tf_calls++;
    if (src != dst && src != tf_left && R32(src + 0x34) != 0)
        tf_left = dst;                       /* a fresh sorted DB result */
    if (src != dst && tfs_backt && R32(0x0B704A80u) - tfs_backt < 1500u)
        tf_left = src;                       /* BACK saves the shown list back */
    if (src != dst)
        tfs_backt = 0;                       /* only the first copy after it */
    lg[4] = R16(dst + 2); lg[5] = 0xFFFF;
    if (R8(MSOFF) || src == tf_left)         /* a save of the shown list: */
        return ret;                          /* never filter that */
    int filt = tf_cfg.on && (tf_tick(0) || tf_tick(1) || tf_tick(2) || tf_tick(3) ||
                             tf_mt_active());
    u32 n = R16(dst + 2), rows = R32(dst + 0x4C), i, j = 0, na = 0;
    if (!n || n > 50000 || rows < 0x08000000u || rows >= 0x10000000u)
        return ret;
    if (!is_track(rows) && (n < 2 || !is_track(rows + 20 * (n - 1)))) {
        if (src != dst)                      /* a list without tracks */
            tf_tracks = 0;
        return ret;
    }
    for (i = 0; i < NKC; i++)
        kcache[i] = 0;
    mt_cols();                               /* My Tag: columns, and the tags */
    tf_mtn = 0; tf_mtover = 0;               /* this list's tracks use */
#ifdef TF_PROBE
    if (!tf_pbn) { probe(T_SMT, I_SMTC, 32); probe(T_SMT, I_SMTC, 33); probe(T_MT, I_MT, 1); probe(T_MT, I_MT, 2360286408u); }
#endif
    int any = 0;
    for (i = 0; i < n; i++) {
        u32 r = rows + 20 * i, a, tb;
        int k = 1;
        if (is_track(r)) {
            any = 1;
            k = keep(R32(r), filt, &a, &tb);
            if (na < TF_NA) {
                tf_attr[na] = a;
                tf_tb[na] = tb;
            }
            na++;
        }
        if (!k)
            continue;
        if (j != i) {
            u32 d = rows + 20 * j, w;
            for (w = 0; w < 20; w += 4)
                R32(d + w) = R32(r + w);
        }
        j++;
    }
    if (!any)
        return ret;
    tf_na = na;
    tf_naver++;
    tf_tracks = 1;
    if (!filt)
        return ret;
    tf_lists++;
    tf_in += n;
    tf_kept += j;
    tf_last_n = n;
    tf_last_kept = j;
    lg[5] = j;
    if (j != n && src != dst) {
        t = tracked(0);
        if (!t)
            t = &trk[tf_lists & (NTRK - 1)];
        t->dst = dst; t->src = src; t->rows = R32(src + 0x4C);
        t->n = R16(src + 2); t->perm = R32(src + 0x34);
    }
    R16(dst + 2) = (u16)j;
    if (R16(dst + 4) == n)
        R16(dst + 4) = (u16)j;
    return ret;
}
