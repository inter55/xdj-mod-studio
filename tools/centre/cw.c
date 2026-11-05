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
static u32 last_hash, k7;        /* k7: rows per unit, Q16 */
u32 cw_calls;

/* 2026-10-28: the 3-band palette follows the deck theme. The theme
 * switcher (tools/themesw/ts.c) keeps the stock artwork colours S and
 * per-theme colours D_t at TS_DATA, sorted ascending, with the active
 * theme in hdr.cur (0 = stock/OFF). We map our three band colours
 * through the active theme's table once per theme change; colours not
 * in S (or no theme blob) keep the stock triad. RGB mode is untouched:
 * its colours are the track's own analysis data. */
#ifndef TS_DATA
#define TS_DATA 0x09779000u
#endif
#ifndef TS_END
#define TS_END 0x09790000u
#endif
static u32 last_cur_t = 0xFFFFFFFFu;
static u16 t_core = C_CORE, t_midc = C_MIDC, t_outer = C_OUTER;

static u16 tmap(u16 v, const u16 *S, const u16 *D, u32 ns)
{
    u32 lo = 0, hi = ns;
    while (lo < hi) {
        u32 mid = (lo + hi) >> 1;
        if (S[mid] < v) lo = mid + 1; else hi = mid;
    }
    return (lo < ns && S[lo] == v) ? D[lo] : v;
}

static void theme_sync(void)
{
    const u32 *h = (const u32 *)TS_DATA;
    if (R32(TS_DATA) != 0x57535458u) {       /* no theme blob installed */
        if (last_cur_t != 0xFFFFFFFFu) {
            last_cur_t = 0xFFFFFFFFu;
            t_core = C_CORE; t_midc = C_MIDC; t_outer = C_OUTER;
        }
        return;
    }
    u32 ns = h[2], nt = h[3], cur = ((const u8 *)h)[32];
    u32 s_off = h[4], d_off = h[6];
    if (!ns || ns > 4096 || !nt || cur > nt) return;
    if (s_off + 2 * ns > TS_END - TS_DATA) return;
    if (d_off + 2 * nt * ns > TS_END - TS_DATA) return;
    if (cur == last_cur_t) return;
    last_cur_t = cur;
    if (!cur) {
        t_core = C_CORE; t_midc = C_MIDC; t_outer = C_OUTER;
        return;
    }
    const u16 *S = (const u16 *)(TS_DATA + s_off);
    const u16 *D = (const u16 *)(TS_DATA + d_off + (cur - 1) * 2 * ns);
    t_core = tmap(C_CORE, S, D, ns);
    t_midc = tmap(C_MIDC, S, D, ns);
    t_outer = tmap(C_OUTER, S, D, ns);
}

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
    theme_sync();
    if (mode == 1 && R32(SLOT5) == TAG_PWV5) {
        const u8 *s = (const u8 *)SLOT5;
        /* entry count is the BE u32 @+16. Until 2026-10-27 only the low 16
         * bits were read, so any track past 65535 entries (65535/150 =
         * 7 min 17 s) wrapped the count and the centre fell back to stock
         * blue a while into the track (back to RGB after a CUE jump to the
         * covered range). The file's own rate byte (@+21, 150 on every
         * export seen) scales our 150/s painter index: a denser export
         * would otherwise draw the colours at the wrong position (the
         * off-axis-grid report). */
        u32 n = ((u32)s[16] << 24) | ((u32)s[17] << 16) | ((u32)s[18] << 8) | s[19];
        u32 rate = s[21] ? s[21] : 150;
        if (rate != 150) {
            first = first * rate / 150;
            step = step * rate / 150;
            if (!step) step = 1;
        }
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
                cw_tbl[k] = k < ha ? t_core : k < hb ? t_midc : t_outer;
            return hc;
        }
    }
    if (white > 7) white = 7;
    fill(R16(pal + 2 * white), 0);
    return 0;
}
