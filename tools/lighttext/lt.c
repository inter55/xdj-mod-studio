/* lt.c -- light-mode text colours (see ltentry.S). lt_on = 1: near-neutral
 * text (white / greys) becomes dark, coloured text keeps its colour. */
typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;
#define R32(a) (*(volatile u32 *)(a))
#define R8(a) (*(volatile u8 *)(a))
#ifndef TS_DATA
#error constants come from build.sh
#endif
u32 lt_on;          /* 1 = force light (tests) */
u32 lt_idx;         /* the light theme's number in the theme switcher (0 = none) */
u32 lt_fills;       /* diagnostic: fill colours rewritten */
u32 lt_active;      /* on(), refreshed every display flush (lt_fill) --
                     * the per-pixel wrappers test only this word */

/* light while the theme switcher's CURRENT theme is the light one (its
 * header byte +32, tools/themesw/ts.c) -- the text / waveform / clear
 * changes follow the theme with no extra state */
static int on(void)
{
    if (lt_on) return 1;
    return lt_idx && R32(TS_DATA) == 0x57535458u && R8(TS_DATA + 32) == lt_idx;
}
u32 lt_calls;

void lt_map(u8 *c)
{
    static u32 last_in = 0xFFFFFFFFu, last_out;
    lt_calls++;
    if (!lt_active) return;
    u32 r = c[0], g = c[1], b = c[2];
    u32 key = r << 16 | g << 8 | b;
    if (key == last_in) {                   /* a string = one colour */
        c[0] = (u8)(last_out >> 16); c[1] = (u8)(last_out >> 8); c[2] = (u8)last_out;
        return;
    }
    last_in = key;
    u32 mx = r > g ? (r > b ? r : b) : (g > b ? g : b);
    u32 mn = r < g ? (r < b ? r : b) : (g < b ? g : b);
    if (mx - mn > 48) {                     /* coloured text: keep */
        last_out = key;
        return;
    }
    if (mx < 0x80) {                         /* dark INFO ink: keep it dark
                                              * on the stock-white INFO card */
        last_out = key;
        return;
    }
    /* neutral: lightness flip with extra contrast (white -> 0x20) */
    u32 v = 255 - (mx * 7 >> 3);
    v = v * v >> 8;                          /* darker mid greys */
    c[0] = c[1] = c[2] = (u8)v;
    last_out = v << 16 | v << 8 | v;
}

/* RGB -> RGB565 for the waveform painters (their pool words point here;
 * stock converter 0x08DB6CD4): with lt_on, neutral greys flip like text
 * (black window -> white, white ticks -> dark), colours stay. */
typedef u32 (*rgb_t)(u32 r, u32 g, u32 b);
u32 lt_rgb(u32 r, u32 g, u32 b)
{
    if (lt_active) {
        /* own rule, NOT lt_map: lt_map keeps dark greys dark (INFO card
         * ink), which left the centre waveform's black background black
         * (2026-11-12 deck photos). Here every neutral flips. */
        u32 mx = r > g ? (r > b ? r : b) : (g > b ? g : b);
        u32 mn = r < g ? (r < b ? r : b) : (g < b ? g : b);
        if (mx - mn <= 48) {
            u32 v = 255 - (mx * 7 >> 3);
            r = g = b = v;
        }
    }
    return ((rgb_t)0x08DB6CD4u)(r, g, b);
}

/* the flush hook (pool 0x08C92E20 chain, see ltentry.S): in lists aimed
 * at the 800-px UI layer, neutral fill colours flip like text -- the
 * screen clears (colour 0) turn white. RGB565 in the fill's +2 word. */
/* fills are BACKGROUNDS (row stripes, clears, panels): a plain lightness
 * flip -- the text curve (v^2) made dark-grey row stripes mid grey
 * (2026-10-26 BROWSE/UTILITY). Coloured fills stay. */
static u16 flip565(u16 c)
{
    u32 r = ((c >> 11) & 31) * 255 / 31, g = ((c >> 5) & 63) * 255 / 63, b = (c & 31) * 255 / 31;
    u32 mx = r > g ? (r > b ? r : b) : (g > b ? g : b);
    u32 mn = r < g ? (r < b ? r : b) : (g < b ? g : b);
    if (mx - mn > 48) return c;
    u32 v = 255 - (mx * 7 >> 3);
    return (u16)((v >> 3) << 11 | (v >> 2) << 5 | v >> 3);
}

void lt_fill(u32 req)
{
    lt_active = on();
    /* every list, not only the UI layer's: the overview's playhead line and
     * bar markers are white fills on their own small layers (2026-10-26) */
    if (!lt_active) return;
    u32 l = R32(req), end = R32(req - 4);
    if (((l & 0x1FFFFFFFu) - 0x08000000u) >= 0x08000000u || end <= l ||
        end - l > 0x400 || ((end - l) & 3))
        return;
    volatile u32 *w = (volatile u32 *)((l & 0x1FFFFFFFu) | 0xA0000000u);
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
        if (op == 0xB0) {                       /* polyline: (colour << 16) | n */
            u32 c = w[i + 1];
            u16 nc = flip565((u16)(c >> 16));
            if (nc != (u16)(c >> 16)) {
                w[i + 1] = ((u32)nc << 16) | (c & 0xFFFFu);
                lt_fills++;
            }
        }
        if (op == 0xA0 || op == 0x80) {        /* fill / flat polygon colour */
            u32 c = w[i + (op == 0xA0 ? 2 : 1)];
            u16 nc = flip565((u16)c);
            if (nc != (u16)c) {
                w[i + (op == 0xA0 ? 2 : 1)] = (c & 0xFFFF0000u) | nc;
                lt_fills++;
            }
        }
        i += len;
    }
}

/* the waveform painters' staging clears (pools 0x08DC3B44 centre,
 * 0x08DC2808 PERFORM strip -> lt_mset): white instead of black in light.
 * 2026-10-26: 0x09E44B00 = the OVERVIEW strip's staging (400x43 at half
 * resolution, 0x85E0 B, live-sampled; the bottom of the main screen). */
unsigned char lt_bg = 0xFF;
u32 lt_msval(u32 ptr, u32 val)
{
    if ((ptr == 0x0C0A6FBCu || ptr == 0x0C09408Cu || ptr == 0x09E44B00u)
        && lt_active)
        return lt_bg;
    return val;
}
