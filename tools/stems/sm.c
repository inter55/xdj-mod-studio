/* sm.c -- live STEM mute/solo for the XDJ-700 (deck C, RAM only).
 *
 * Hooks the media server's USB audio read (pool 0x08D5FFA8, the same read
 * the version switcher uses; we chain to whatever was there). For a
 * STEMS-4 file (tools/stems4.py: 128-B "XST4" trailer at the end) with
 * any stem muted on the PERFORM STEMS pads (PMASK bit i = stem i muted,
 * order vocals/drums/bass/other), a read inside the mix's sample data is
 * answered with the SUM of the un-muted stem planes at the same position
 * (16-bit, clipped, file endianness), the file position is left exactly
 * where a plain read would leave it. All muted = silence. Anything else
 * (other files, header reads, nothing muted) goes to the previous hook
 * untouched.
 */
typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;
typedef int s32;

typedef unsigned long uptr;       /* 32-bit on the deck, 64 on the Mac test */
#define R32(a) (*(volatile u32 *)(uptr)(a))
#define R16(a) (*(volatile u16 *)(uptr)(a))
#define R8(a)  (*(volatile u8 *)(uptr)(a))
#ifdef SM_NATIVE                  /* tools/stems/sm_native_test.c */
extern u8 n_mask;
u32 n_fread(void *, u32, u32, u32);
int n_fseek(u32, int, int);
u32 n_pos(u32);
#define PMASK ((uptr)&n_mask)
#define FREAD ((uptr)n_fread)
#define FSEEK3 ((uptr)n_fseek)
#define POS(fp) n_pos(fp)
#else
#ifndef PMASK
#error PMASK
#endif
#define FREAD  0x08C61A6Cu        /* fread(buf, size, count, fp) -> items */
#define FSEEK3 0x08C62E1Cu        /* fseek(fp, off, whence) -> 0 ok */
#define POS(fp) R32((fp) + 0x2C)  /* handle: current position */
#endif
#define MAXB 8192                 /* 8 KB chunks: room for the fast-trigger buffers below 0x09844000 (TOUCH FX) */

typedef u32 (*fread_t)(void *, u32, u32, u32);
typedef s32 (*fseek_t)(u32, s32, s32);

uptr sm_prev = FREAD;              /* chained hook; installer sets it */
/* diagnostics (read them over the shell): every call, the biggest read,
 * the last read, files recognised, reads mixed. sm_lastbuf/lo/hi = the
 * DESTINATION of the recent reads (where the deck buffers what it gets, i.e.
 * its read-ahead). sm_remix/remixb = re-mixes done / bytes re-mixed. */
u32 sm_hits, sm_calls, sm_maxn, sm_lastn, sm_lastpos, sm_valid, sm_files;
u32 sm_lastbuf, sm_buflo, sm_bufhi;
u32 sm_remix, sm_remixb;
/* Re-mix window: the deck reads ahead (tens of seconds), so a mask change
 * is not heard until the buffered old mix plays out. We track the buffered
 * window -- file [wpos0,wpos1) == ram [wbuf0, +len) -- and on a mask change
 * rewrite it with the new mix in place (same mix() math as mix-on-read).
 * mask == 0 (everything back on) re-reads the original bytes instead of
 * re-summing, so un-muting is bit-exact. The window only ever extends over
 * reads that were contiguous with it (else it restarts); a cap bounds the
 * work. Everything stays inside RAM the deck itself filled. */
static u32 wpos0, wpos1;
static uptr wbuf0;
static u32 last_mask;
#define REMIX_MAX (8u * 1024u * 1024u)
/* version-file window (tools/vredirect.py; addresses must match it) */
#ifndef V_VSEL
#define V_VSEL 0x081DB500u
#define V_VFP  0x081DB504u
#define V_VTBL 0x081DB508u
#endif
#ifdef SM_NATIVE
extern u8 n_vsel;
extern u32 n_vfp, n_vtbl[];
#define VVSEL ((uptr)&n_vsel)
#define VVFP  ((uptr)&n_vfp)
#define VVTBL ((uptr)n_vtbl)
#else
#define VVSEL ((uptr)V_VSEL)
#define VVFP  ((uptr)V_VFP)
#define VVTBL ((uptr)V_VTBL)
#endif
static u32 vwpos0, vwpos1;
static uptr vwbuf0;
static u32 vlast_fp, vlast_vs;
static u32 cur_fp, valid, d_off, d_len, big, rel[4];
static u8 trailer[128];
static u8 tmp[MAXB];
static s32 acc[MAXB / 2];

#ifndef SM_NATIVE
static void clone_fp(u32 fp);
#endif
static u32 rfront;                 /* file offset the deck has asked for so far */

static u32 rd(void *b, u32 n, u32 fp)
{
    return ((fread_t)FREAD)(b, 1, n, fp);
}

static void probe(u32 fp)
{
    u32 pos = POS(fp);
    cur_fp = fp;
    valid = 0;
    wpos0 = wpos1 = wbuf0 = 0;          /* new file: windows restart */
    vwpos0 = vwpos1 = vwbuf0 = 0;
    vlast_fp = ~0u;
    if (((fseek_t)FSEEK3)(fp, -128, 2) == 0 && rd(trailer, 128, fp) == 128 &&
        trailer[0] == 'X' && trailer[1] == 'S' && trailer[2] == 'T' && trailer[3] == '4') {
        const u32 *t = (const u32 *)trailer;
        d_off = t[1];
        d_len = t[2];
        big = trailer[12];
        for (int k = 0; k < 4; k++) rel[k] = t[4 + k];
        valid = trailer[13] == 4 && d_len;
        sm_files += valid;
    }
    sm_valid = valid;
    ((fseek_t)FSEEK3)(fp, (s32)pos, 0);
#ifndef SM_NATIVE
    clone_fp(fp);
    /* tell the PERFORM STEMS row whether this track has stems (PMASK+3,
     * tools/perform_pads.py STEMSOK) and make it redraw (PMASK+1 is the
     * mask the row last drew: anything else forces a redraw) */
    R8(PMASK + 3) = (u8)valid;
    R8(PMASK + 1) = 0xFF;
#endif
}

/* mix [pos, pos+n) of the sample data (inside d_off..d_off+d_len, n <= MAXB,
 * 4-aligned) into out; 0 = a plane read failed */
static int mixb(u8 *out, u32 pos, u32 n, u32 mask, u32 fp, u8 *tmp, s32 *acc)
{
    u32 ns = n >> 1;
    for (u32 i = 0; i < ns; i++) acc[i] = 0;
    for (int k = 0; k < 4; k++) {
        if (mask & (1u << k)) continue;               /* muted */
        if (((fseek_t)FSEEK3)(fp, (s32)(pos + rel[k]), 0) != 0 || rd(tmp, n, fp) != n)
            return 0;
        const u8 *p = tmp;
        if (big)
            for (u32 i = 0; i < ns; i++, p += 2) acc[i] += (short)(p[0] << 8 | p[1]);
        else
            for (u32 i = 0; i < ns; i++, p += 2) acc[i] += (short)(p[1] << 8 | p[0]);
    }
    u8 *o = out;
    for (u32 i = 0; i < ns; i++, o += 2) {
        s32 v = acc[i];
        if (v > 32767) v = 32767;
        if (v < -32768) v = -32768;
        if (big) { o[0] = (u8)(v >> 8); o[1] = (u8)v; }
        else     { o[0] = (u8)v; o[1] = (u8)(v >> 8); }
    }
    return 1;
}

static int mix(u8 *out, u32 pos, u32 n, u32 mask, u32 fp)
{
    return mixb(out, pos, n, mask, fp, tmp, acc);
}

/* extend the re-mix window over a data read (file [pos,pos+n) -> ram buf);
 * anything non-contiguous restarts it */
static void win_extend(u32 pos, u32 n, uptr buf)
{
    if (n && pos == wpos1 && buf == wbuf0 + (wpos1 - wpos0)) {
        wpos1 = pos + n;
        return;
    }
    wpos0 = pos;
    wpos1 = pos + n;
    wbuf0 = buf;
}

/* rewrite the buffered window with the mix for mask (0 = original bytes,
 * bit-exact). Re-mixes the NEWEST up to REMIX_MAX bytes (the oldest is
 * already played). fp is left where it was. */
static void remix(u32 mask, u32 fp)
{
    u32 len = wpos1 - wpos0;
    if (len == 0)
        return;
    if (len > REMIX_MAX)
        len = REMIX_MAX;
    u32 a0 = wpos1 - len, b = wpos1;   /* newest bytes (oldest already played) */
    u32 save = POS(fp);
    u32 a = d_off + ((a0 - d_off + 3) & ~3u);   /* whole frames only */
    while (a + 4 <= b) {
        u32 m = b - a;
        if (m > MAXB)
            m = MAXB;
        m &= ~3u;
        if (!m)
            break;
        u8 *dst = (u8 *)(wbuf0 + (a0 - wpos0) + (a - a0));
        if (mask == 0) {
            if (((fseek_t)FSEEK3)(fp, (s32)a, 0) != 0 || rd(dst, m, fp) != m)
                break;
        } else if (!mix(dst, a, m, mask, fp))
            break;
        a += m;
    }
    ((fseek_t)FSEEK3)(fp, (s32)save, 0);
    sm_remix++;
    sm_remixb += len;
}

/* Version files (tools/vredirect.py) have the same read-ahead problem with
 * a different transform (re-read through the selected version instead of
 * re-sum). The asm hook already maintains a per-handle table; we reuse it
 * (sanity-checked before trusting). Needs no installer change: same cave,
 * same hook, same counters. Address defines are above with the windows. */

/* 1 = fp has a usable version table; outputs selected version, its rel
 * (0 for the plain mix) and the data range */
static int vtab(u32 fp, u32 *vs, u32 *rel, u32 *doff, u32 *dlen)
{
    if (R32(VVFP) != fp)
        return 0;
    u32 n = R16(VVTBL + 8);
    u32 v = R8(VVSEL);
    if (v > n || n > 8)
        return 0;
    u32 o = R32(VVTBL), l = R32(VVTBL + 4);
    if (!l)
        return 0;
    *vs = v;
    *rel = v ? R32(VVTBL + 12 + 4 * (v - 1)) : 0;
    *doff = o;
    *dlen = l;
    return 1;
}

/* re-read the buffered window through version vs (0 = plain mix) */
static void vre_remix(u32 vs, u32 rel, u32 fp)
{
    u32 len = vwpos1 - vwpos0;
    if (len == 0)
        return;
    if (len > REMIX_MAX)
        len = REMIX_MAX;
    u32 a = vwpos1 - len, b = vwpos1;
    u32 save = POS(fp);
    while (a < b) {
        u32 m = b - a;
        if (m > MAXB)
            m = MAXB;
        u8 *dst = (u8 *)(vwbuf0 + (vwpos1 - len - vwpos0) + (a - (vwpos1 - len)));
        if (vs == 0) {
            if (((fseek_t)FSEEK3)(fp, (s32)a, 0) != 0 || rd(dst, m, fp) != m)
                break;
        } else {
            if (((fseek_t)FSEEK3)(fp, (s32)(a + rel), 0) != 0 || rd(dst, m, fp) != m)
                break;
        }
        a += m;
    }
    ((fseek_t)FSEEK3)(fp, (s32)save, 0);
    sm_remix++;
    sm_remixb += len;
}

#ifndef SM_NATIVE
/* ---- DSP-side re-mix (2026-10-19) --------------------------------------
 * The ~25-30 s delay lives in the AUDIO CHIP, not in SH RAM: the DSP keeps
 * the track DECODED in a ring in its DDR -- slots of 4804 B at 0xC0090000:
 * 588 stereo frames as float32 (= s16 / 32768, L,R interleaved) followed by
 * a 100-B header whose first word is the block number n (block n = file
 * frames n*588..). Consecutive blocks use consecutive slots (ring wraps).
 * The playhead block = DSP word 0x11837CCC. On a mask change we rewrite the
 * blocks from just ahead of the playhead with the new mix through the host
 * port (same 32-bit HPIA/HPID accesses as the key-shift installer), a few
 * blocks per read call, only into slots whose header already names that
 * block, never while a DMA to the host port is live. Blocks the deck read
 * BEFORE the change but has not decoded yet are waited for. */
#define HPIA_P2   0xB8040000u
#define HPID_P2   0xB80C0000u
#define RING      0xC0090000u
#define SLOT      4804u
#define NSLOT     13846u               /* up to the end of DDR             */
#define BLKF      588u                 /* frames per block                 */
#define PLAYBLK   0x11837CCCu
#define DR_BUDGET 24u                   /* blocks per read call (slow path, before the poll runs) */
#define DR_CATCH  48u                   /* blocks per poll while catching up: 128 at a far
                                         * aim starved the audio path (deck, 2026-11-06) */
#define DR_AIM    1200u                 /* aim this far past the playhead: ~16 s, inside the
                                         * chip's ~28 s store but clear of the slots it is
                                         * still decoding (writing those made the deck stutter) */
#define DR_KEEP   4u                    /* blocks per poll once caught up (28 polls x 4 > 75/s) */
#define MAXAHEAD  2400u                 /* never aim more than 32 s past the playhead           */
#define DR_GAP    8u                    /* missing blocks in a row = the decoded frontier        */
u32 sm_drblk, sm_drscan, sm_drskip;    /* counters (shell diagnostics)     */
/* 2026-11-03 deck: mute still ~30 s -> WHY the rewrite stops, per reason:
 * busy = host-port DMA live at entry, noslot = block not found in the
 * chip's cache, wfail = read/mix/DMA/header check failed mid-write,
 * lastp = chip's play block, lastb = block we wanted, lasth = header word
 * seen at the anchor slot (cache layout check) */
u32 sm_drbusy, sm_drnoslot, sm_drwfail, sm_drlastp, sm_drlastb, sm_drlasth;
static u32 dr_on, dr_mask, dr_next, dr_end, dr_ablk, dr_aslot = ~0u;
static u8 dblk[BLKF * 4];
static u8 dtmp[BLKF * 4];               /* the rewrite's own buffers: it may run */
static s32 dacc[BLKF * 2];              /* in another task than mix-on-read     */

/* ---- fast trigger (2026-11-03) -----------------------------------------
 * The rewrite above only ran inside the deck's own reads, and the deck reads
 * in bursts: the audio chip asks for more only when its buffer drops below
 * ~25.5 s, then tops it up to ~30 s -- one burst every ~5 s (emulator,
 * measured). A tap between bursts waited for the next one (deck: ~10 s).
 * The player task polls the chip's buffer level ~340 times a second
 * (fn 0x08C1D366 / 0x08C1D416, both calling the host-port read accessor
 * 0x08BF2BCC through their own pool 0x08C1D56C). sm_poll sits in that pool:
 * it calls the accessor, then rewrites a couple of blocks. The player task
 * must not use the deck's own file handle (the media task reads with it,
 * nested reads would move its position under it), so we read through a
 * COPY of the handle taken when the file is first seen: a stdio FILE here is
 * a self-contained 0x1250-byte record (volume pointer, cluster/position
 * state, open flag at +0x1246; no pointers into itself), the file system
 * serialises the two readers with its own lock. Once sm_poll has run, the
 * rewrite is driven from there only. */
#define CLONE_SZ  0x1250u
#define POLL_ACC  0x08BF2BCCu
#define POLL_BUDGET 1u               /* blocks per poll: ~4.5x play speed, keeps the player task light */
typedef u32 (*acc_t)(u32, u32, u32, u32);
u32 sm_polls, sm_parm;                 /* poll calls / rewrites started there */
static u32 dfp[CLONE_SZ / 4], dfp_of;
static u32 pmask_seen = ~0u;

static void clone_fp(u32 fp)
{
    for (u32 i = 0; i < CLONE_SZ / 4; i++)
        dfp[i] = R32(fp + 4 * i);
    dfp_of = fp;
    pmask_seen = R8(PMASK) & 15;        /* the reads from here on use it */
}

static u32 get_sr(void) { u32 v; __asm__ volatile("stc sr,%0" : "=r"(v)); return v; }
static void set_sr(u32 v) { __asm__ volatile("ldc %0,sr" : : "r"(v) : "memory"); }

static int hpi_dma_busy(void)
{
    static const u32 base[2] = { 0xFE008000u, 0xFDC08000u };
    static const u32 off[6] = { 0x20, 0x30, 0x40, 0x50, 0x70, 0x80 };
    for (int b = 0; b < 2; b++)
        for (int c = 0; c < 6; c++) {
            u32 r = base[b] + off[c];
            if ((R32(r + 0xC) & 3u) != 1u) continue;
            u32 s = R32(r) & 0x1FFFFFFFu, d = R32(r + 4) & 0x1FFFFFFFu;
            if ((s >> 26) == 6u || (d >> 26) == 6u)
                return 1;
        }
    return 0;
}

/* one masked host-port read (HPIA saved/restored) */
static u32 hrd1(u32 a)
{
    u32 sr = get_sr();
    set_sr(sr | 0xF0u);
    u32 h = R32(HPIA_P2);
    R32(HPIA_P2) = a;
    u32 v = R32(HPID_P2);
    R32(HPIA_P2) = h;
    set_sr(sr);
    return v;
}

static u32 f32bits(int v)               /* s16 / 32768 as IEEE float bits */
{
    if (v == 0) return 0;
    u32 s = v < 0 ? 0x80000000u : 0, a = v < 0 ? (u32)-v : (u32)v, e = 15;
    while (!(a & 0x8000u)) { a <<= 1; e--; }
    return s | ((e + 112u) << 23) | ((a & 0x7FFFu) << 8);
}

/* slot holding block b (its header already written), or ~0.
 *
 * 2026-11-03 deck: mute heard ~30 s late. The old version SEARCHED the
 * chip's decoded store for any slot labelled b -- but the store keeps
 * minutes of audio, so after the track ran a while, jumped, or was loaded
 * again, an OLD copy of block b (one the chip will never play again) can
 * carry the same label. The rewrite then went there and nothing changed
 * until the slow path. Now the chip itself says where it plays: PSLOT is
 * the slot of the block now playing (DSP word, follows PLAYBLK exactly in
 * the emulator); block b is pslot + (b - p) -- the copy the chip will play
 * next. Past the store's end it continues at slot 0 (checked by label). */
#define PSLOT     0x1182E81Cu
static u32 dr_slot(u32 b)
{
    u32 p = hrd1(PLAYBLK), ps = hrd1(PSLOT);
    if (ps >= NSLOT || b < p)
        return ~0u;
    u32 s = ps + (b - p);
    if (s < NSLOT && hrd1(RING + s * SLOT + 4704u) == b) {
        dr_ablk = b; dr_aslot = s;
        return s;
    }
    /* wrapped: the store restarted at slot 0 after ps */
    u32 w0 = hrd1(RING + 4704u);                 /* block in slot 0 */
    if (w0 <= b && b - w0 < ps) {
        u32 t = b - w0;
        if (hrd1(RING + t * SLOT + 4704u) == b) {
            dr_ablk = b; dr_aslot = t; sm_drscan++;
            return t;
        }
    }
    sm_drlasth = s < NSLOT ? hrd1(RING + s * SLOT + 4704u) : 0xFFFFFFFFu;
    return ~0u;
}

/* write block b (frames b*588..) of the current mix into slot s */
static int dr_write(u32 b, u32 s, u32 fp)
{
    u32 a = d_off + b * BLKF * 4;
    if (a + BLKF * 4 > d_off + d_len)
        return 0;
    if (dr_mask == 0) {                 /* everything on: the original mix */
        if (((fseek_t)FSEEK3)(fp, (s32)a, 0) != 0 || rd(dblk, BLKF * 4, fp) != BLKF * 4)
            return 0;
    } else if (!mixb(dblk, a, BLKF * 4, dr_mask, fp, dtmp, dacc))
        return 0;
    if (hpi_dma_busy())
        return 0;
    u32 base = RING + s * SLOT;
    if (hrd1(base + 4704u) != b)        /* slot no longer holds block b */
        return 0;
    /* write in 8 short masked pieces (~147 words each), HPIA restored after
     * every piece, so no interrupt waits long */
    const u8 *p = dblk;
    for (u32 c = 0; c < 8; c++) {
        if (hpi_dma_busy())
            return 0;
        u32 sr = get_sr();
        set_sr(sr | 0xF0u);
        u32 h = R32(HPIA_P2);
        for (u32 i = c * (BLKF * 2 / 8); i < (c + 1) * (BLKF * 2 / 8); i++, p += 2) {
            int v = big ? (short)(p[0] << 8 | p[1]) : (short)(p[1] << 8 | p[0]);
            R32(HPIA_P2) = base + 4 * i;
            R32(HPID_P2) = f32bits(v);
        }
        R32(HPIA_P2) = h;
        set_sr(sr);
    }
    return 1;
}

static void dr_run(u32 fp, u32 budget)
{
    if (!dr_on)
        return;
    if (hpi_dma_busy()) { sm_drbusy++; return; }
    u32 save = POS(fp);
    u32 p = hrd1(PLAYBLK);
    sm_drlastp = p;
    /* start at the playhead, not where the last pass stopped: the bytes the
     * chip plays NEXT are the ones that must change for the tap to be heard,
     * and the rest of the buffer is fixed up behind playback */
    if (dr_next < p + 3) dr_next = p + 3;
    /* a jump back leaves rfront far in the future: never chase blocks the
     * chip has not decoded (the old code stalled there and mutes went to
     * stale copies -- the deck's ~30 s) */
    /* A bounded aim, not the store's limit: aiming at p + MAXAHEAD (32 s) made the
 * deck stutter, glitch its waveform and then stop muting at all -- drlastb
 * 4644 while drlastp was 2245 says it was writing slots the decoder had not
 * finished with. DR_AIM keeps the pass inside decoded audio and still covers
 * what the chip plays for the next ~16 s; the blocks at the playhead are
 * written first, in the first poll, which is what "instant" needs. */
    dr_end = p + DR_AIM;
u32 done = 0, miss = 0;
    while (done < budget && miss < DR_GAP) {
        if (dr_next >= dr_end) { dr_on = 0; break; }
        sm_drlastb = dr_next;
        u32 s = dr_slot(dr_next);
        if (s == ~0u) { sm_drnoslot++; dr_next++; miss++; continue; }
        /* only blocks the decoder has finished: its NEXT block must already
         * carry a label, else this slot may still be filling (writing into a
         * half-decoded slot is what makes the deck stutter) */
        if (dr_slot(dr_next + 1) == ~0u) { sm_drnoslot++; break; }
        if (!dr_write(dr_next, s, fp)) { sm_drwfail++; dr_next++; miss++; continue; }
        dr_next++; done++; miss = 0;
        sm_drblk++;
    }
    if (done < budget)
        sm_drskip++;
    ((fseek_t)FSEEK3)(fp, (s32)save, 0);
}

static void dr_step(u32 fp)
{
    dr_run(fp, DR_BUDGET);
}

static void poll_step(void)
{
    if (!valid || dfp_of != cur_fp)
        return;
    u32 mask = R8(PMASK) & 15;
    if (mask != pmask_seen) {
        pmask_seen = mask;
        dr_on = 1;
        dr_mask = mask;
        dr_next = 0;
        dr_end = (rfront > d_off ? (rfront - d_off) / (BLKF * 4) : 0) + 1;
        sm_parm++;
    }
    if (dr_on)
        dr_run((u32)(uptr)dfp, dr_next + 8u < dr_end ? DR_CATCH : DR_KEEP);
}

u32 sm_stack[8192 / 4] __attribute__((aligned(8)));   /* smhook.S */
u32 sm_stack_busy;

u32 sm_poll(u32 a, u32 b, u32 c, u32 d)
{
    u32 r = ((acc_t)POLL_ACC)(a, b, c, d);
    sm_polls++;
    poll_step();
    return r;
}
#endif

u32 sm_fread(void *buf, u32 size, u32 count, u32 fp)
{
    sm_calls++;
    if (fp != cur_fp)
        probe(fp);
    u32 mask = R8(PMASK) & 15;
    u32 pos = POS(fp), n = size * count;
    sm_lastn = n;
    sm_lastpos = pos;
    sm_lastbuf = (u32)buf;
    if (n > sm_maxn) sm_maxn = n;
    if (!sm_buflo || sm_lastbuf < sm_buflo) sm_buflo = sm_lastbuf;
    { u32 e = sm_lastbuf + n; if (e > sm_bufhi) sm_bufhi = e; }
    u32 lo = d_off, hi = d_off + d_len;
    if (valid && n != 0 && size != 0 && pos + n > d_off)
        rfront = pos + n;               /* before the read: in flight counts */
    if (valid && n != 0 && size != 0) {
        /* track the buffered window even when passing through (mask == 0):
         * a later mute needs it */
        u32 a = pos > lo ? pos : lo, b = pos + n < hi ? pos + n : hi;
        if (b > a)
            win_extend(a, b - a, (uptr)buf + (a - pos));
        if (mask != last_mask) {
            remix(mask, fp);        /* restores fp to pos */
            last_mask = mask;
#ifndef SM_NATIVE
            /* the decoded copy in the audio chip: from the playhead up to
             * what was read before this change (later reads get mixed).
             * Driven from sm_poll once that runs (fast trigger above). */
            if (!sm_polls) {
                dr_on = 1; dr_mask = mask; dr_next = 0;
                dr_end = (pos > d_off ? (pos - d_off) / (BLKF * 4) : 0) + 1;
            }
#endif
        }
#ifndef SM_NATIVE
        if (!sm_polls)
            dr_step(fp);            /* restores fp to pos */
#endif
    }
    if (!valid || !mask || n == 0 || pos + n <= lo || pos >= hi || size == 0) {
        /* not a stems read: maybe a version read (same delay, other file) */
        if (!valid && n != 0 && size != 0) {
            u32 vs, rel, vdo, vdl;
            if (vtab(fp, &vs, &rel, &vdo, &vdl)) {
                u32 a = pos > vdo ? pos : vdo, b = pos + n < vdo + vdl ? pos + n : vdo + vdl;
                if (b > a) {
                    if (a == vwpos1 && (uptr)buf + (a - pos) == vwbuf0 + (vwpos1 - vwpos0))
                        vwpos1 = b;
                    else {
                        vwpos0 = a;
                        vwpos1 = b;
                        vwbuf0 = (uptr)buf + (a - pos);
                    }
                }
                if (fp != vlast_fp || vs != vlast_vs) {
                    vre_remix(vs, rel, fp);     /* restores fp to pos */
                    vlast_fp = fp;
                    vlast_vs = vs;
                }
            }
        }
        return ((fread_t)sm_prev)(buf, size, count, fp);
    }
    /* plain read first (any size, the deck's real reads are big); then
     * overwrite the part inside the sample data with the mix, 16 KB at a
     * time, 4-byte frames aligned to the data start */
    u32 got = ((fread_t)sm_prev)(buf, size, count, fp);
    u32 end = pos + got * size;                       /* bytes actually read */
    u32 a = pos > lo ? pos : lo, b = end < hi ? end : hi;
    a = lo + ((a - lo + 3) & ~3u);                    /* whole frames only */
    while (a + 4 <= b) {
        u32 m = b - a;
        if (m > MAXB) m = MAXB;
        m &= ~3u;
        if (!m || !mix((u8 *)buf + (a - pos), a, m, mask, fp))
            break;
        a += m;
    }
    ((fseek_t)FSEEK3)(fp, (s32)end, 0);               /* where a plain read leaves it */
    sm_hits++;
    return got;
}
