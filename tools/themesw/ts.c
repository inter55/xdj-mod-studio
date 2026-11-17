/* ts.c -- on-deck theme SWITCHING for the XDJ-700 (RAM only, deck C).
 *
 * Every theme is a bijection S -> D_t over the artwork colours (built on
 * the Mac by tools/theme_switch.py with the same colour maths as the
 * Studio preview), so switching CUR -> TARGET is exact in both directions:
 * a pixel holding FROM[i] becomes TO[i], where FROM/TO = S (stock) or the
 * theme's D list. Nothing else in RAM is touched: only pixels inside the
 * artwork regions, and only colours in FROM.
 *
 * Blob (TS_DATA, LE, written once by the installer):
 *   +0  "XTSW"  +4 nreg  +8 ns  +12 nt
 *   +16 reg_off +20 s_off +24 d_off +28 name_off   (offsets from TS_DATA)
 *   +32 u8 cur  +33 u8 target  +34 u8 busy  +35 pad  +36 u32 nswitch
 *   +40 hash_off  +44 chunk (pixels per fingerprint)  +48 nskipped
 *   +52 tbits_off (1 bit per chunk: chunk holds theme colours)
 *   +56 info_addr +60 info_n +64 light_idx (light-only stock INFO card)
 *   +68 wave_off: u32 n, m, addr[n]; u16 rows[nt + 1][m] waveform colours
 *       (theme_switch.py wave_section; addr bit 0 = 32-bit literal)
 *   regions {u32 addr, u32 pixels}, S u16[ns] ascending,
 *   D u16[nt][ns] (S order), names char[nt][16] (UPPER, NUL-padded),
 *   chunk bitmap, hashes u32[] (one per THEMABLE chunk, in order)
 * SAFETY: the firmware reuses some image memory for live data at runtime
 * (seen: 8 KB blocks at 0x087E0000.. holding play-HISTORY records). So a
 * chunk is recoloured ONLY if its pixels, mapped back to stock colours,
 * match the stock fingerprint computed on the Mac; anything the firmware
 * wrote makes the chunk skip, untouched (counted in nskipped).
 * Called from the MY SETTINGS [THEME] push (tools/mysettings.py): ts_push()
 * steps OFF -> theme 1 -> ... -> theme nt -> OFF, recolours at once, and
 * writes the row text into MSTNAME (+ MSTAPP = 1 so the row shows it).
 */
typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;

#ifndef TS_DATA
#error TS_DATA
#endif
#ifndef MSTNAME
#error MSTNAME
#endif
#ifndef MSTAPP
#error MSTAPP
#endif

struct hdr {
    u32 magic, nreg, ns, nt;
    u32 reg_off, s_off, d_off, name_off;
    u8 cur, target, busy, pad;
    u32 nswitch, hash_off, chunk, nskipped, tbits_off;
    u32 info_addr, info_n, light_idx;   /* light-only stock INFO card */
    u32 wave_off;                       /* 0 = no waveform table */
};

#define MAXS 4096   /* 2026-10-26: light makes S ~3400 colours */
static u16 frm[MAXS], to[MAXS], stk[MAXS];
static u32 bits[2048];                  /* 65536-bit "colour is in FROM" */
/* 2026-10-18 diag: start address of the first 32 chunks the stock-
 * fingerprint guard skipped (deck shows half-themed buttons the emulator
 * doesn't): read with tools/theme_skips.py after a [THEME] push */
u32 ts_skiplog[32];

static inline void wb(u32 a)            /* D-cache line write-back */
{
    __asm__ volatile("ocbwb @%0" : : "r"(a) : "memory");
}

static void build(struct hdr *h)
{
    u8 *base = (u8 *)h;
    const u16 *S = (const u16 *)(base + h->s_off);
    const u16 *D = (const u16 *)(base + h->d_off);
    const u16 *F = h->cur ? D + (h->cur - 1) * h->ns : S;
    const u16 *T = h->target ? D + (h->target - 1) * h->ns : S;
    u32 n = h->ns, i, j, gap;
    for (i = 0; i < n; i++) {
        frm[i] = F[i];
        to[i] = T[i];
        stk[i] = S[i];
    }
    for (gap = n / 2; gap; gap /= 2)    /* shell sort pairs by FROM */
        for (i = gap; i < n; i++) {
            u16 f = frm[i], t = to[i], k = stk[i];
            for (j = i; j >= gap && frm[j - gap] > f; j -= gap) {
                frm[j] = frm[j - gap];
                to[j] = to[j - gap];
                stk[j] = stk[j - gap];
            }
            frm[j] = f;
            to[j] = t;
            stk[j] = k;
        }
    for (i = 0; i < 2048; i++)
        bits[i] = 0;
    for (i = 0; i < n; i++)
        bits[frm[i] >> 5] |= 1u << (frm[i] & 31);
}

static inline u32 find(u32 n, u16 v)     /* index of v in frm (present) */
{
    u32 lo = 0, hi = n;
    while (lo < hi) {
        u32 mid = (lo + hi) >> 1;
        if (frm[mid] < v)
            lo = mid + 1;
        else
            hi = mid;
    }
    return lo;
}

#define IN(v) (bits[(v) >> 5] & (1u << ((v) & 31)))

/* 2026-10-25 (deck: "themes way too long to load"): per pixel the old code
 * did two binary searches (~11 steps each) over ~1.5 M artwork pixels. Two
 * direct 64K lookup tables (colour -> target colour, colour -> stock
 * colour, 256 KB) make it one load per pixel. They live in deck RAM that is
 * zero in every state (0x09836000..0x0996D000, measured on the deck:
 * CLAUDE.md sec. 77; stems end 0x098423F0, centre colours start 0x098A0000).
 * Claimed only if the whole area reads zero the first time; otherwise the
 * binary search is used as before. */
#define TS_MAP 0x09850000u
#define TOMAP ((volatile u16 *)TS_MAP)
#define STMAP ((volatile u16 *)(TS_MAP + 0x20000u))
static u32 map_state;                   /* 0 unchecked, 1 ours, 2 in use */
u32 ts_maps_used;                       /* diagnostic: switches via maps */

static int maps(u32 n)
{
    u32 i;
    if (map_state == 0) {
        const volatile u32 *w = (const volatile u32 *)TS_MAP;
        map_state = 1;
        for (i = 0; i < 0x40000u / 4; i++)
            if (w[i]) { map_state = 2; break; }
    }
    if (map_state != 1)
        return 0;
    for (i = 0; i < 65536; i++) {
        TOMAP[i] = (u16)i;
        STMAP[i] = (u16)i;
    }
    for (i = 0; i < n; i++) {
        TOMAP[frm[i]] = to[i];
        STMAP[frm[i]] = stk[i];
    }
    ts_maps_used++;
    return 1;
}

static void recolour(struct hdr *h)
{
    const u32 *reg = (const u32 *)((u8 *)h + h->reg_off);
    const u32 *hash = (const u32 *)((u8 *)h + h->hash_off);
    const u8 *tb = (const u8 *)h + h->tbits_off;
    u32 n = h->ns, r, c = 0;
    int fast = maps(n);
    for (r = 0; r < h->nreg; r++) {
        u16 *p0 = (u16 *)reg[2 * r], *end = p0 + reg[2 * r + 1];
        while (p0 < end) {
            u16 *p, *e = p0 + h->chunk;
            u32 hv = 0x811C9DC5u, a;
            if (e > end)
                e = end;
            if (!(tb[c >> 3] & (1u << (c & 7)))) {   /* nothing to theme */
                c++;
                p0 = e;
                continue;
            }
            c++;
            if (fast) {
                for (p = p0; p < e; p++)    /* fingerprint in stock colours */
                    hv = (hv ^ STMAP[*p]) * 0x01000193u;
            } else {
                for (p = p0; p < e; p++) {
                    u16 v = *p;
                    if (IN(v))
                        v = stk[find(n, v)];
                    hv = (hv ^ v) * 0x01000193u;
                }
            }
            if (hv == *hash++) {
                if (fast) {
                    for (p = p0; p < e; p++)
                        *p = TOMAP[*p];
                } else {
                    for (p = p0; p < e; p++) {
                        u16 v = *p;
                        if (IN(v))
                            *p = to[find(n, v)];
                    }
                }
                for (a = (u32)p0 & ~31u; a < (u32)e; a += 32)
                    wb(a);                   /* display/2DG read RAM */
            } else {
                if (h->nskipped < 32)
                    ts_skiplog[h->nskipped] = (u32)p0;
                h->nskipped++;
            }
            p0 = e;
        }
    }
}

/* The INFO rows' icons are separate 24x24 sprite blits, not part of the
 * card picture: keep their stock (black) pixels too while LIGHT is on. The
 * selected row's note (0x08127190) stays themed so it contrasts with the
 * light selection bar, like the row text. */
static const u32 info_ico[][2] = {
    {0x08113910u, 24 * 24}, {0x08112B90u, 24 * 24},
    {0x08138890u, 24 * 24}, {0x08115410u, 24 * 24}, {0x08121310u, 24 * 24},
    {0x0813C090u, 24 * 24}, {0x0813A110u, 24 * 24},
};

static void map_range(struct hdr *h, u16 *p, u32 cnt, u32 fast)
{
    u16 *end = p + cnt;
    u32 a;
    if (fast) {
        while (p < end) {
            *p = TOMAP[*p];
            p++;
        }
    } else {
        while (p < end) {
            u16 v = *p;
            if (IN(v))
                *p = to[find(h->ns, v)];
            p++;
        }
    }
    for (a = (u32)(end - cnt) & ~31u; a < (u32)end; a += 32)
        wb(a);
}

/* LIGHT-only INFO repair. Stock INFO is a white card in a dark UI, so a
 * plain light-map inversion turns it into a dark panel. Keep that one
 * region stock while LIGHT is active; the text wrapper also leaves its
 * dark ink alone. Called AFTER the global recolour, with `cur` still the
 * old theme, so a colour target reached from LIGHT is mapped directly
 * from stock instead of from LIGHT's dark colours. */
static void info_apply(struct hdr *h, u32 src_idx, u32 dst_idx)
{
    u8 oldc = h->cur, oldt = h->target;
    u32 fast, i;
    if (!h->info_addr || !h->info_n)
        return;
    h->cur = (u8)src_idx;
    h->target = (u8)dst_idx;
    build(h);
    fast = maps(h->ns);
    map_range(h, (u16 *)h->info_addr, h->info_n, fast);
    for (i = 0; i < sizeof(info_ico) / sizeof(info_ico[0]); i++)
        map_range(h, (u16 *)info_ico[i][0], info_ico[i][1], fast);
    h->cur = oldc;
    h->target = oldt;
}

/* 2026-11-10 review: the card used to be repaired only AFTER the recolour.
 * Leaving LIGHT, every 1024-px chunk that held card/icon pixels (stock) next
 * to ordinary artwork (light) failed the stock-fingerprint guard and was
 * skipped: ~2750 neighbouring pixels (other 24x24 icons) stayed in LIGHT
 * colours until power-off, more after each visit. Now the card is put back
 * into LIGHT colours BEFORE leaving, so the guard sees one consistent theme
 * and the normal recolour takes everything to the target. */
static void info_leave(struct hdr *h)        /* before build()/recolour() */
{
    if (h->light_idx && h->cur == h->light_idx && h->target != h->light_idx)
        info_apply(h, 0, h->light_idx);
}

static void info_enter(struct hdr *h)        /* after recolour() */
{
    if (h->light_idx && h->target == h->light_idx)
        info_apply(h, h->light_idx, 0);
}

static void label(struct hdr *h)
{
    volatile u8 *dst = (volatile u8 *)MSTNAME;
    const u8 *src = h->target
        ? (const u8 *)h + h->name_off + 16 * (h->target - 1)
        : (const u8 *)"OFF";
    u32 i;
    for (i = 0; i < 15 && src[i]; i++)
        dst[i] = src[i];
    for (; i < 16; i++)
        dst[i] = 0;
    *(volatile u8 *)MSTAPP = 1;
}

/* 2026-11-12: the waveform colours follow the theme: the three firmware
 * palettes (blue overview 12, PERFORM strip 8, centre 8 -- .data in RAM,
 * read on every repaint) and the 3-band overview module's three colour
 * literals. An entry is rewritten only while it still holds the colour of
 * the theme being left, so anything the firmware or another mod changed
 * there is left alone (and a module that is not installed is skipped).
 * Exact in both directions (absolute values per theme row). */
u32 ts_wave_writes, ts_wave_skips;
static void wave_apply(struct hdr *h)
{
    if (!h->wave_off || h->wave_off & 3)
        return;
    const u32 *w = (const u32 *)((u8 *)h + h->wave_off);
    u32 n = w[0], m = w[1];
    if (!n || n > 64 || m < n + 3 || m > 72)
        return;
    const u16 *rows = (const u16 *)(w + 2 + n);
    const u16 *from = rows + m * h->cur, *to = rows + m * h->target;
    for (u32 i = 0; i < n; i++) {
        u32 a = w[2 + i];
        if (a & 1) {                       /* 32-bit literal, high half 0 */
            volatile u32 *p = (volatile u32 *)(a & ~1u);
            if (*p == from[i]) { *p = to[i]; ts_wave_writes++; }
            else if (*p != to[i]) ts_wave_skips++;
        } else {
            volatile u16 *p = (volatile u16 *)a;
            if (*p == from[i]) { *p = to[i]; ts_wave_writes++; }
            else if (*p != to[i]) ts_wave_skips++;
        }
    }
}

/* apply h->target now (no-op when already there) */
void ts_apply(void)
{
    struct hdr *h = (struct hdr *)TS_DATA;
    if (h->magic != 0x57535458u || h->busy || h->ns > MAXS)
        return;
    if (h->target > h->nt)
        h->target = 0;
    if (h->target != h->cur) {
        h->busy = 1;
        info_leave(h);
        build(h);
        recolour(h);
        info_enter(h);
        wave_apply(h);
        h->cur = h->target;
        h->nswitch++;
        h->busy = 0;
    }
    label(h);
}

/* [THEME] push: next theme (wraps through OFF) */
void ts_push(void)
{
    struct hdr *h = (struct hdr *)TS_DATA;
    if (h->magic != 0x57535458u || h->busy)
        return;
    h->target = (u8)((h->cur + 1) % (h->nt + 1));
    ts_apply();
}
