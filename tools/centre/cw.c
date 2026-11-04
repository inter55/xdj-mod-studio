/* cw.c -- centre (detail) waveform colours for the XDJ-700 (deck C).
 *
 * The stock painter FUN_08DC39B6 draws each screen column as `rows`
 * pixels up from the centre line (then mirrors). Our hook makes it read
 * one colour PER ROW from cw_tbl (the row-loop load at 0x08DC3B9E is
 * patched to post-increment), so a column can be stacked:
 *   mode 1 RGB    : XDJ-1000MK2 maths on PWV5 (tallest entry in the
 *                   column's range; height h*3/2; one colour)
 *   mode 2 3-BAND : rekordbox PWV7 (150/s, 3 B = mid, high, low) copied
 *                   into the .EXT by Mod Studio; cumulative like the
 *                   overview: cream core = lows, + orange mids, + blue
 *                   highs, scaled to the track's loudest moment
 *   mode 3 CUSTOM : the Studio's 1-3 colours (2026-11-04): 2-3 colours =
 *                   one colour per row from the centre line out
 *                   (cw_cust_ramp, the preview's grad565 ramp), 1 colour =
 *                   the stock 8-entry palette tinted to it (cw_cust_pal);
 *                   stock height either way. The installers fill the
 *                   tables (cw_cust_kind 1 = ramp, 0 = palette).
 *   else / no data: the stock palette colour, stock height.
 * Returns the row count to use, or 0 = keep the painter's own height.
 */
typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;

#define R32(a) (*(volatile u32 *)(a))
#define R16(a) (*(volatile u16 *)(a))
#define R8(a)  (*(volatile u8 *)(a))
#ifndef MODE_ADDR
#error constants come from build.sh
#endif
#define PALETTE 0x0E736150u
/* 2026-10-18: the PERFORM-screen strip is drawn by the twin painter
 * FUN_08DC2754 (50 rows, palette 0x0E735FA8): cw_col2 = same colours,
 * heights scaled 50/55, that palette as the stock fallback. */
#define PALETTE2 0x0E735FA8u
#define ROWS2 50
#define ROWS 55
#define C_CORE 0xEF18u           /* cream: lows (overview C_HIGH)   */
#define C_MIDC 0xE4A5u           /* orange: mids                    */
#define C_OUTER 0x1B1Au          /* blue: highs (overview C_LOW)    */

u16 cw_tbl[64];
u16 cw_cust_ramp[64];            /* mode 3, kind 1: colour per row         */
u16 cw_cust_pal[8];              /* mode 3, kind 0: tinted stock palette   */
u32 cw_cust_kind;
static u32 last_hash, k7;        /* k7: rows per unit, Q16 */
u32 cw_calls;

static u32 be32(const u8 *p) { return (u32)p[0] << 24 | p[1] << 16 | p[2] << 8 | p[3]; }

/* copy the loaded track's PWV7 section into SLOT7 (tag word last) */
/* 2026-10-25 LINK guard: the ANLZ context is shared with the Pro DJ Link
 * server (a linked deck loading a track from this deck's stick) -- every
 * pointer is checked to be 4-aligned RAM before it is read */
static int ok_ptr(u32 a, u32 len)
{
    return !(a & 3) && a >= 0x08000000u && a < 0x10000000u && len <= 0x10000000u - a;
}

static void snap7(void)
{
    R32(SLOT7) = 0;
    k7 = 0;
    u32 ctx = R32(CTX_TAB);
    if (!ok_ptr(ctx, IDX_OFF + 4 + 16 * 32)) return;
    u32 idx = ctx + IDX_OFF, n = R32(idx);
    if (n > 32) return;
    for (u32 i = 0; i < n; i++) {
        u32 e = idx + 4 + 16 * i;
        if (R32(e + 4) != TAG_PWV7) continue;
        const u8 *sec = (const u8 *)R32(e + 12);
        if (!ok_ptr((u32)sec, 24) || R32((u32)sec) != TAG_PWV7) return;
        u32 len = be32(sec + 8);
        if (len > SLOT7_MAX || len < 24 || !ok_ptr((u32)sec, len)) return;
        u32 *d = (u32 *)SLOT7;
        const u32 *s = (const u32 *)sec;
        for (u32 w = 1; w < (len + 3) / 4; w++) d[w] = s[w];
        /* per-track peak of the cumulative height -> scale to ROWS */
        u32 hl = be32(sec + 4), cnt = be32(sec + 16), peak = 1;
        const u8 *p = sec + hl;
        for (u32 j = 0; j < cnt && hl + 3 * j + 3 <= len; j++, p += 3) {
            u32 v = p[2] * 125u + p[0] * 82u + p[1] * 64u;
            if (v > peak) peak = v;
        }
        k7 = ((u32)ROWS << 16) / peak;
        d[0] = s[0];                                  /* publish */
        return;
    }
}

static u32 fill(u16 c, u32 rows)
{
    for (u32 k = 0; k < ROWS; k++) cw_tbl[k] = c;
    return rows;
}

static u32 col(u32 idx8, u32 step, u32 white, u32 rmax, u32 pal);

static u32 scl(u32 h, u32 rmax)           /* h * rmax / 55 (no divide) */
{
    return rmax == ROWS ? h : (h * rmax * 1192u) >> 16;
}

u32 cw_col(u32 idx8, u32 step, u32 white)
{
    return col(idx8, step, white, ROWS, PALETTE);
}

u32 cw_col2(u32 idx8, u32 step, u32 white)
{
    return col(idx8, step, white, ROWS2, PALETTE2);
}

static u32 col(u32 idx8, u32 step, u32 white, u32 rmax, u32 pal)
{
    cw_calls++;
    u32 h = R32(SNAP_HASH);
    if (h != last_hash) {                             /* new track loaded */
        last_hash = h;
        snap7();
    }
    u32 mode = R8(MODE_ADDR), first = idx8 >> 3;
    if (mode == 1 && R32(SLOT5) == TAG_PWV5) {
        const u8 *s = (const u8 *)SLOT5;
        u32 n = (u32)s[18] << 8 | s[19];
        if (first < n) {
            u32 end = first + step;
            if (end > n) end = n;
            const u8 *e = s + s[7];
            int best = -1;
            u32 bw = 0;
            for (u32 j = first; j < end; j++) {
                u32 w = (u32)e[2 * j] << 8 | e[2 * j + 1];
                int hh = (w >> 2) & 31;
                if (hh > best) { best = hh; bw = w; }
            }
            u32 rows = scl(((u32)best * 3) >> 1, rmax);
            if (!rows) rows = 1;
            u16 c = (u16)((bw & 0xE000) | ((bw & 0x1C00) >> 2) | ((bw & 0x380) >> 5));
            return fill(c, rows);
        }
    }
    if (mode == 2 && R32(SLOT7) == TAG_PWV7 && k7) {
        const u8 *s = (const u8 *)SLOT7;
        u32 hl = be32(s + 4), n = be32(s + 16);
        if (first < n) {
            u32 end = first + step;
            if (end > n) end = n;
            const u8 *p = s + hl + 3 * first;
            u32 mi = 0, hi = 0, lo = 0;
            for (u32 j = first; j < end; j++, p += 3) {
                if (p[0] > mi) mi = p[0];
                if (p[1] > hi) hi = p[1];
                if (p[2] > lo) lo = p[2];
            }
            u32 a = lo * 125u, b = a + mi * 82u, c = b + hi * 64u;
            u32 ha = scl((a * k7) >> 16, rmax), hb = scl((b * k7) >> 16, rmax),
                hc = scl((c * k7) >> 16, rmax);
            if (hc > rmax) hc = rmax;
            if (!hc) hc = 1;
            for (u32 k = 0; k < ROWS; k++)
                cw_tbl[k] = k < ha ? C_CORE : k < hb ? C_MIDC : C_OUTER;
            return hc;
        }
    }
    if (white > 7) white = 7;
    if (mode == 3) {
        if (cw_cust_kind) {
            for (u32 k = 0; k < ROWS; k++) cw_tbl[k] = cw_cust_ramp[k];
            return 0;
        }
        fill(cw_cust_pal[white], 0);
        return 0;
    }
    fill(R16(pal + 2 * white), 0);
    return 0;
}
