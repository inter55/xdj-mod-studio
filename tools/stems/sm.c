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
#define MAXB 4704                 /* 2 blocks: the deck reads 9408 B at a time = 2 chunks either way; the rest of
                                   * the cave (limit 0x09844000, TOUCH FX) goes to the rewrite batch buffers */

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
static u32 dr_stock;            /* no pass has run since this file was loaded */
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

/* Block geometry, shared by the deck build and the native gate (2026-11-11).
 * The masked-section split lives here, ABOVE the SM_NATIVE guard, because
 * tools/stems/sm_native_test.c must be able to assert that the sections tile
 * a block and that the longest is DR_PIECE -- the number sm_drpk reports on
 * the deck. Keeping it inside the guard made the native build link-fail. */
#define BLKF      588u                 /* frames per block                 */
#ifndef DR_PIECE
#define DR_PIECE  64u
#endif
#define DR_NPIECE ((BLKF * 2 + DR_PIECE - 1u) / DR_PIECE)

/* 2026-11-13 (deck, second visit): the ring rewrite still glitched on the
 * deck (each block ~10 ms there, playback overtook it). Default = the CLEAN
 * mute: short read-ahead only, heard ~3 s after the tap. -DSM_DR = the
 * instant rewrite on top (emulator 0.12 s / 0.84 s at deck speed), for deck
 * experiments while it is made cheaper. */
#ifndef SM_DR
#define SM_NODR
#endif

#ifndef SM_NATIVE
static void clone_fp(u32 fp);
#endif
static u32 rfront;                 /* file offset the deck has asked for so far */
static u32 thr_end = ~0u, thr_run0; /* short read-ahead: end of the last read / start of this run */
/* 2026-11-10 review. Everything the deck reads AFTER a mask change is mixed
 * with the new mask as it is read. The rewrite pass must stop where those
 * reads begin: it used to run on to "play position + 32 s", into blocks that
 * were already the new mix. The four-plane path only wasted work there; the
 * one-plane path (stored - stem) took the stem out a SECOND time, which puts
 * it back inverted at full level -- and that is heard ~25 s after the tap.
 * rd_mask/rd_chg = the mask of the newest read and the file offset where
 * reads with that mask began. */
static u32 rd_mask = ~0u, rd_chg;

/* -DSM_SIMLAT=N (EMULATOR ONLY, never on a deck): every stick trip sleeps N
 * clock-ms first. The emulated stick answers at once; the real one costs
 * ~4-5 ms per trip (deck 2026-11-13: 4.4 ms for one 2352-B plane read), and
 * that is what decides whether a short read-ahead can keep the chip fed. */
#if defined(SM_SIMLAT) && !defined(SM_NATIVE)
#define SIMLAT() ((void)((s32 (*)(u32))0x08E0335Cu)(SM_SIMLAT))
#else
#define SIMLAT() ((void)0)
#endif

static u32 rd(void *b, u32 n, u32 fp)
{
    SIMLAT();
    return ((fread_t)FREAD)(b, 1, n, fp);
}

/* is fp a STEMS-4 file? (reads its trailer, puts the position back,
 * changes no state) */
static int is_stems(u32 fp)
{
    u32 pos = POS(fp);
    int ok = ((fseek_t)FSEEK3)(fp, -128, 2) == 0 && rd(trailer, 128, fp) == 128 &&
             trailer[0] == 'X' && trailer[1] == 'S' && trailer[2] == 'T' &&
             trailer[3] == '4' && trailer[13] == 4;
    ((fseek_t)FSEEK3)(fp, (s32)pos, 0);
    return ok;
}

static void probe(u32 fp)
{
    u32 pos = POS(fp);
    cur_fp = fp;
    valid = 0;
    dr_stock = 1;                       /* the ring is the deck's original mix */
    wpos0 = wpos1 = wbuf0 = 0;          /* new file: windows restart */
    vwpos0 = vwpos1 = vwbuf0 = 0;
    vlast_fp = ~0u;
    thr_end = ~0u;                      /* new file: its first read starts a run */
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
/* BLKF is defined above the SM_NATIVE guard, where the native gate can see it */
#define PLAYBLK   0x11837CCCu
#define DR_BUDGET 24u                   /* blocks per read call (slow path, before the poll runs) */
#define DR_CATCH  8u                    /* blocks per poll while a backlog is cleared (2026-11-09: 128 starved the player) */
#define DR_RATE   3u                    /* and never more than this many per 10 ms (300/s, 4x play speed) */
#define MSCLK     0x0B704A80u            /* firmware 1 ms system clock */
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
/* 2026-11-07 night: what is really at the slot PSLOT names. The emulator says
 * PSLOT is exact (tools/dsp_ringmap.py: block 2043 in slot 2403, which is what
 * 0x1182E81C reads), yet on the deck the label check there failed. On every
 * mask change we therefore record the 100-byte header AT that slot and the
 * index of the word, if any, that holds PLAYBLK. Three causes separate
 * cleanly: header at a different offset on hardware (match at i>0), a live
 * copy labelled only once played (no match, slice 0), or PSLOT meaning
 * something else (match nowhere near, sm_hdrps not a plausible slot). */
u32 sm_hdr[25], sm_hdrmatch, sm_hdrps, sm_hdrp, sm_caps;
static u32 dr_on, dr_mask, dr_next, dr_end, dr_ablk, dr_aslot = ~0u;
/* 2026-11-09 (deck, paused, port idle): one block per stick round trip gave
 * 125 blocks/s -- only 1.7x play speed. DRN blocks are now read per plane in
 * ONE seek+read and written from the sums directly. */
/* 2026-11-12 deck (2nd deck, mask 02): batches of 3 held the player task 15 ms
 * on average, 35 ms at worst (237 batches) -> audible stutter. One block per
 * batch, evenly paced, keeps every hold near one block's cost (~5 ms) while
 * still rewriting faster than playback (75 blocks/s): see DR_PACE_MS. */
#ifndef DRN
#define DRN       1u
#endif
#define DTMPN     (DRN > 3u ? DRN : 3u)   /* the fade's "old" copy sits at block 2 of dtmp */
/* 2026-11-11 (the stutter): every masked section holds interrupts off, and on
 * the deck that is exactly what the player task hears (2026-11-09 note). A
 * block was split into 8 sections of 147 samples; at most DR_PIECE now, so the
 * longest wait drops 147 -> 64 with the same total work and identical audio
 * (ringcheck compares every block). sm_drpk records the longest one done. */
/* DR_PIECE / DR_NPIECE are defined above the SM_NATIVE guard (single source
 * for the deck build and the native gate) */
#define DR_BUSY   0xFFFFFFFEu           /* dr_slot: host port in use, ask again */
/* 2026-11-09 late, deck: "stutters and glitches, might have worked slightly"
 * (no counters: the deck was off before they could be read). Two things a
 * listener would call that: (1) the pass being overtaken by playback again
 * and again -- it restarted 3 blocks ahead each time, so muted and un-muted
 * blocks alternated; (2) the hard switch at the first rewritten block. Now an
 * overtaken pass restarts DR_LEAD blocks ahead (one clean gap instead of a
 * flutter), and the first block after every start fades old mix -> new mix.
 * Counters for the next deck visit: sm_drbump (times overtaken), sm_drms
 * (longest batch, ms), sm_drfade. */
#ifndef DR_LEAD
#define DR_LEAD   60u                   /* 0.8 s (was 30: overtaken 7x on the deck) */
#endif
/* Review 2026-11-09: the play position was sampled BEFORE the stick reads of
 * a batch (10-100 ms on a busy stick), so by the time the samples were
 * written the chip could already be playing that very block -- a block
 * changed while it is being played is a glitch, and at a rewrite speed close
 * to play speed that happened batch after batch. The position is now read
 * again right before every block is written; a block closer than DR_SAFE to
 * it is NOT written (sm_drlate) and the pass restarts DR_LEAD ahead. A fresh
 * tap starts DR_FIRST blocks ahead (~0.1 s) to leave room for its reads. */
#ifndef DR_PACE_MS                      /* tools/stems_overtake_emu.py builds a slow one */
#define DR_PACE_MS 6u                   /* gap AFTER each 1-block batch: ~5 ms batch + 6 = ~90 blocks/s */
#endif
#define DR_SAFE   2u
#ifndef DR_FIRST
#define DR_FIRST  64u                   /* fresh tap: 0.85 s ahead. 2026-11-13 deck: one block
                                         * costs ~10 ms there (+6 ms pace = ~62 blocks/s < 75/s
                                         * playback): from 10 ahead playback overtook the pass
                                         * (drbump 4, heard as glitches). From 64 the pass covers
                                         * the ~300-block window before playback gets there
                                         * (emulator at deck speed: 0 overtakes, 0.84 s) */
#endif
u32 sm_drbump, sm_drms, sm_drfade, sm_drlate, sm_drsub, sm_drsubf, sm_drchk;
u32 sm_drbat, sm_drtot;                 /* batches run / ms spent in them (deck: average cost) */
u32 sm_drpk;                            /* longest masked section, samples (2026-11-11) */
u32 sm_drbase, sm_drmask, sm_drold;     /* the subtract-path decision */
static u32 dr_old = ~0u, dr_fade, dr_lead = DR_LEAD;
static u32 dr_base_ok;          /* last pass finished: the store is one known mix */
static u32 dr_store_ok;         /* a pass reached the decoded frontier since install */
static u32 dr_checked;          /* this pass: 1 = base verified, 2 = full re-sum */
static u32 dr_miss;             /* consecutive blocks with no slot (frontier) */
static u32 dr_sube;             /* one-plane path only below this block (0 = never) */
static u32 dr_misst;            /* ms clock at the first miss of a run */
u32 sm_drend, sm_drwait;        /* diag: the pass's finish block / frontier time-outs */
static u8 dtmp[DTMPN * BLKF * 4] __attribute__((aligned(4)));   /* the rewrite's own buffers: it may run */
static s32 dacc[DRN * BLKF * 2];        /* in another task than mix-on-read     */

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

u32 sm_hpibusy;                         /* port accesses skipped: DMA running */
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

/* one masked host-port read (HPIA saved/restored), or ~0 when a DMA is
 * using the port. 2026-11-09 deck: the deck's own DMA streams audio through
 * the SAME HPIA; moving it mid-transfer returned garbage AND sent part of
 * the deck's stream to the wrong address (stutter, waveform glitch). The
 * busy check runs AFTER masking: only CPU code arms a DMA, so nothing can
 * start one while we hold the port. (The emulator's DMA is instant -- it
 * cannot show this.) */
static u32 hrd1(u32 a)
{
    u32 sr = get_sr();
    set_sr(sr | 0xF0u);
    if (hpi_dma_busy()) { set_sr(sr); sm_hpibusy++; return ~0u; }
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
/* 2026-11-09 deck: the slot PSLOT names can already hold a LATER block of
 * the same run -- label 4523 there while block 3555 played (968 blocks,
 * ~13 s ahead), and the mute then fell back to ~25 s. Early in a track the
 * label at PSLOT equalled PLAYBLK and the rewrite was heard in ~2 s, so the
 * ring and the label offset (+4704, word 0) are right; only the guess is
 * off. The label found tells how far: step back (or forward) by it ONCE
 * and confirm. That lands in the run PSLOT is in = the newest copy, the one
 * the chip plays next (an older copy elsewhere carrying the same number,
 * 2026-11-07, is never chosen by this step). Every slot is still confirmed
 * by its label before it is used; a wrong guess writes nothing. */
u32 sm_drfix, sm_dranc;                 /* corrected guesses / anchor hits */
static u32 dr_lab(u32 s) { return hrd1(RING + s * SLOT + 4704u); }

/* 2026-11-09 deck ring map (STEMS_MUTE_LATENCY.md): the block the chip
 * PLAYS is the label of the slot PSLOT names. PLAYBLK (0x11837CCC) only
 * agrees early in a track (and in the emulator); mid-track on hardware it
 * lags by thousands of blocks and even falls out of the store -- rewrites
 * aimed from it went to audio already played. ps_out gets PSLOT. */
static u32 play_blk(u32 *ps_out)
{
    u32 ps = hrd1(PSLOT);
    if (ps_out) *ps_out = ps;
    if (ps == ~0u)
        return ~0u;
    if (ps < NSLOT) {
        u32 l = dr_lab(ps);
        if (l <= d_len / (BLKF * 4) + 1u)
            return l;
        return ~0u;                      /* busy or not a block: try later */
    }
    return hrd1(PLAYBLK);
}

/* slot holding block b, ~0 = not in the store (yet), DR_BUSY = the host
 * port was in use (a label read came back refused): ask again, do NOT treat
 * the block as missing. 2026-11-09 deck: refused reads were counted as
 * "missing" and the pass skipped on -- 2051 of ~2150 blocks were never
 * rewritten (hpibusy 1790 == noslot 1790) and the mute waited ~28 s. */
static u32 dr_slot(u32 b, u32 ps, u32 p)
{
    u32 l;
    if (b < p)
        return ~0u;
    /* last confirmed slot: consecutive blocks sit in consecutive slots */
    if (dr_aslot < NSLOT) {
        u32 s = ~0u;
        if (b >= dr_ablk && b - dr_ablk < NSLOT)
            s = (dr_aslot + (b - dr_ablk)) % NSLOT;
        else if (b < dr_ablk && dr_ablk - b <= dr_aslot)
            s = dr_aslot - (dr_ablk - b);
        if (s != ~0u) {
            l = dr_lab(s);
            if (l == ~0u) return DR_BUSY;
            if (l == b) {
                dr_ablk = b; dr_aslot = s; sm_dranc++;
                return s;
            }
        }
    }
    if (ps < NSLOT) {
        u32 s = (ps + (b - p)) % NSLOT;
        l = dr_lab(s);
        if (l == ~0u) return DR_BUSY;
        if (l == b) {
            dr_ablk = b; dr_aslot = s;
            return s;
        }
        sm_drlasth = l;
    }
    /* wrapped: the store restarted at slot 0 after ps */
    u32 w0 = dr_lab(0);                         /* block in slot 0 */
    if (w0 == ~0u) return DR_BUSY;
    if (w0 <= b && b - w0 < NSLOT) {
        u32 t = b - w0;
        l = dr_lab(t);
        if (l == ~0u) return DR_BUSY;
        if (l == b) {
            dr_ablk = b; dr_aslot = t; sm_drscan++;
            return t;
        }
    }
    return ~0u;
}

/* the current mix of blocks b..b+n-1 (n <= DRN) as clipped samples in dacc;
 * one seek + read per plane for the whole batch */
static int dr_mix(u32 b, u32 n, u32 fp)
{
    u32 a = d_off + b * BLKF * 4, len = n * BLKF * 4, ns = len >> 1;
    const u8 *q;
    if (dr_mask == 0) {                 /* everything on: the original mix */
        if (((fseek_t)FSEEK3)(fp, (s32)a, 0) != 0 || rd(dtmp, len, fp) != len)
            return 0;
        q = dtmp;
        if (big) for (u32 i = 0; i < ns; i++, q += 2) dacc[i] = (short)(q[0] << 8 | q[1]);
        else     for (u32 i = 0; i < ns; i++, q += 2) dacc[i] = (short)(q[1] << 8 | q[0]);
        return 1;
    }
    for (u32 i = 0; i < ns; i++) dacc[i] = 0;
    for (int k = 0; k < 4; k++) {
        if (dr_mask & (1u << k)) continue;
        if (((fseek_t)FSEEK3)(fp, (s32)(a + rel[k]), 0) != 0 || rd(dtmp, len, fp) != len)
            return 0;
        q = dtmp;
        if (big) for (u32 i = 0; i < ns; i++, q += 2) dacc[i] += (short)(q[0] << 8 | q[1]);
        else     for (u32 i = 0; i < ns; i++, q += 2) dacc[i] += (short)(q[1] << 8 | q[0]);
    }
    for (u32 i = 0; i < ns; i++) {
        s32 v = dacc[i];
        if (v > 32767) dacc[i] = 32767;
        else if (v < -32768) dacc[i] = -32768;
    }
    return 1;
}

/* 2026-11-10: single-stem changes read ONE plane, not all un-muted planes.
 * The slot already holds the previous mix, so muting one more stem is
 * (stored - that stem's plane) and un-muting it is (stored + plane). On the
 * deck the stick is the bottleneck (STEMS_MUTE_LATENCY.md), so this cuts a
 * batch's USB reads from up to four to one and keeps the player task's
 * blocking time down. It is exact only while the stored mix did not clip;
 * a rail sample sends the batch back to dr_mix() (four planes). */
static int s16of(u32 w)
{
    if (w == 0)
        return 0;
    int e = (int)((w >> 23) & 0xFFu) - 127;
    u32 m = (w & 0x7FFFFFu) | 0x800000u;
    int v = e >= 0 ? 32768 : (e < -15 ? 0 : (int)(m >> (8 - e)));
    if (w & 0x80000000u)
        v = -v;
    if (v > 32767) v = 32767;
    if (v < -32768) v = -32768;
    return v;
}

/* dr_piece (the masked-section split) is defined below, outside the SM_NATIVE
 * guard, so tools/stems/sm_native_test.c can gate it too. */
u32 dr_piece(u32 c, u32 *i0);

/* read block b's 1176 samples from its ring slot into v; 0 = the slot moved
 * or the stored mix has a clipped sample (either way: use dr_mix).
 * cmp != 0: compare with v instead of storing (0 = differs).
 * 2026-11-10 review: this held interrupts off for a whole block (1176 port
 * round trips) where dr_put was cut into 8 pieces for exactly that reason
 * (2026-11-09 deck: long masked sections = stutter). Same 8 pieces here. */
static int dr_get8(u32 b, u32 s, s32 *v, int cmp)
{
    u32 base = RING + s * SLOT;
    if (hrd1(base + 4704u) != b)
        return 0;
    for (u32 c = 0; c < DR_NPIECE; c++) {
        u32 i0, i1 = dr_piece(c, &i0) + i0;
        if (i1 - i0 > sm_drpk) sm_drpk = i1 - i0;
        int bad = 0;
        u32 sr = get_sr();
        set_sr(sr | 0xF0u);
        if (hpi_dma_busy()) { set_sr(sr); sm_hpibusy++; return 0; }
        u32 h = R32(HPIA_P2);
        for (u32 i = i0; i < i1; i++) {
            R32(HPIA_P2) = base + 4u * i;
            u32 w = R32(HPID_P2);
            if (cmp) {
                if (s16of(w) != v[i]) { bad = 1; break; }
            } else if (w == 0x3F7FFE00u || w == 0xBF800000u) {   /* 32767 / -32768 */
                bad = 1; break;
            } else
                v[i] = s16of(w);
        }
        R32(HPIA_P2) = h;
        set_sr(sr);
        if (bad)
            return 0;
    }
    return hrd1(base + 4704u) == b;     /* still block b after the last piece */
}

static int dr_get(u32 b, u32 s, s32 *v) { return dr_get8(b, s, v, 0); }
static int dr_get_cmp(u32 b, u32 s, const s32 *v) { return dr_get8(b, s, (s32 *)v, 1); }

/* the current mix of b..b+n-1 from the ring, plus/minus one plane, into dacc */
/* 2026-11-13 deck timing: one stem block (2352 B) per stick read cost
 * 4.4 ms of the ~9.7 ms a rewritten block takes -- almost all of it the
 * read's fixed cost. The one-stem path now reads PCN blocks per trip and
 * serves the following blocks from this cache (key = stem + first block;
 * dropped whenever a new pass starts). */
#ifndef PCN
#define PCN 4u
#endif
static u8 pcache[PCN * BLKF * 4] __attribute__((aligned(4)));
static u32 pc_b = ~0u, pc_k = ~0u, pc_n;
u32 sm_pcread, sm_pchit;                 /* stick trips / blocks served from the cache */
static void pc_drop(void) { pc_b = ~0u; }
static const u8 *plane_blocks(u32 b, u32 n, int k, u32 fp)
{
    if (pc_k == (u32)k && pc_b != ~0u && b >= pc_b && b + n <= pc_b + pc_n) {
        sm_pchit += n;
        return pcache + (b - pc_b) * BLKF * 4;
    }
    u32 last = d_len / (BLKF * 4u);          /* whole blocks in the file */
    u32 m = PCN;
    if (n > m) return 0;
    if (b + m > last) m = last > b ? last - b : 0;
    if (m < n) return 0;
    u32 a = d_off + b * BLKF * 4, len = m * BLKF * 4;
    pc_b = ~0u;
    if (((fseek_t)FSEEK3)(fp, (s32)(a + rel[k]), 0) != 0 || rd(pcache, len, fp) != len)
        return 0;
    pc_b = b; pc_k = (u32)k; pc_n = m;
    sm_pcread++;
    return pcache;
}

/* 2026-11-13 deck: ~10 ms per rewritten block -- where does it go?
 * ms spent reading the chip's block (HPI), reading the stem off the stick,
 * writing the block back (HPI), and blocks timed. stems_probe prints them. */
u32 sm_tget, sm_tusb, sm_tput, sm_tn;
static int dr_mix_sub(u32 b, u32 n, u32 fp, const u32 *sl, int k, int mute)
{
    u32 t = R32(MSCLK);
    for (u32 j = 0; j < n; j++)
        if (!dr_get(b + j, sl[j], dacc + j * BLKF * 2))
            return 0;
    u32 t2 = R32(MSCLK);
    sm_tget += t2 - t;
    u32 len = n * BLKF * 4, ns = len >> 1;
    const u8 *q = plane_blocks(b, n, k, fp);
    if (!q)
        return 0;
    sm_tusb += R32(MSCLK) - t2;
    sm_tn++;
    const int lo = big ? 1 : 0;
    for (u32 i = 0; i < ns; i++, q += 2) {
        s32 p = (short)((q[!lo] << 8) | q[lo]);
        s32 v = dacc[i] + (mute ? -p : p);
        if (v > 32767) v = 32767;
        else if (v < -32768) v = -32768;
        dacc[i] = v;
    }
    return 1;
}

/* write one block's samples v[0..1175] into slot s (must still hold block b).
 * 0 = port busy or the slot was recycled: nothing lost, the caller retries
 * (a half-written block is simply written again) */
static int dr_put(u32 b, u32 s, const s32 *v)
{
    u32 base = RING + s * SLOT;
    if (hrd1(base + 4704u) != b)        /* busy, or no longer block b */
        return 0;
    /* masked pieces of at most DR_PIECE samples (147 -> 64 since 2026-11-11),
     * HPIA restored after every piece, so no interrupt waits long */
    for (u32 c = 0; c < DR_NPIECE; c++) {
        u32 i0, i1 = dr_piece(c, &i0) + i0;
        if (i1 - i0 > sm_drpk) sm_drpk = i1 - i0;
        u32 sr = get_sr();
        set_sr(sr | 0xF0u);
        if (hpi_dma_busy()) { set_sr(sr); sm_hpibusy++; return 0; }
        u32 h = R32(HPIA_P2);
        for (u32 i = i0; i < i1; i++) {
            R32(HPIA_P2) = base + 4 * i;
            R32(HPID_P2) = f32bits((int)v[i]);
        }
        R32(HPIA_P2) = h;
        set_sr(sr);
    }
    return 1;
}

/* One pass. Rules (2026-11-09 deck ring map + counters):
 *  - always from just ahead of the playing block (the label at PSLOT), in
 *    order, so what plays next changes first;
 *  - a block is left ONLY once written: port busy, a failed read or a block
 *    not decoded yet all mean "same block again next poll" (the play position
 *    passing it is the only other way on);
 *  - only blocks whose successor is already labelled (a slot still being
 *    decoded is never touched);
 *  - at most one batch of DRN blocks per call and per 10 ms, 20 ms once the
 *    pass is 2 s ahead of playback, so the player task keeps running. */
static void dr_run(u32 fp, u32 budget)
{
    if (!dr_on)
        return;
    if (hpi_dma_busy()) { sm_drbusy++; return; }
    static u32 rl_t;
    u32 ps, p = play_blk(&ps);
    if (p == ~0u) { sm_drbusy++; return; }
    sm_drlastp = p;
    sm_drbase = dr_base_ok; sm_drmask = dr_mask; sm_drold = dr_old;
    if (dr_next < p + 3) {
        if (dr_next) {                  /* overtaken: restart further ahead each time,
                                         * so a stick that cannot keep up gives a few
                                         * long clean stretches, not a flutter */
            dr_next = p + dr_lead;
            if (dr_lead < 120u) dr_lead *= 2u;  /* cap the old-mix gap at 1.6 s */
            sm_drbump++;
        } else {
            dr_next = p + DR_FIRST;     /* fresh tap: ~0.5 s */
            dr_lead = DR_LEAD;
        }
        dr_fade = 1;
        dr_miss = 0;
    }
    /* The finish line is fixed at the tap: p + 32 s covers the chip's whole
     * decoded store, and a moving line made the pass chase playback forever
     * (it never became known, so a second tap could not use the fast path).
     * Slot labels still end it earlier at the real frontier (dr_miss). */
    if (!dr_end) dr_end = p + MAXAHEAD;
    if (dr_next >= dr_end) { dr_on = 0; dr_store_ok = 1; dr_miss = 0; return; }
    u32 now = R32(MSCLK);
    /* 2026-11-10 deck: "muted in ~1 s, stuttered, drums came back a little,
     * muted again" = overtaken once. The pass used to slow down as soon as it
     * was 2 s ahead, and then gained only ~10 blocks/s on playback -- so it sat
     * 2-3 s ahead when the deck's next read burst (which is mixed on read, 4x
     * the stick traffic) held it up. It now keeps its speed until it is 8 s
     * ahead. */
    if (now - rl_t < (dr_next - p > 600u ? 2u * DR_PACE_MS : DR_PACE_MS))
        return;
    sm_drlastb = dr_next;
    u32 want = dr_end - dr_next, sl[DRN + 1], n = 0, st = 0;
    if (want > DRN) want = DRN;
    if (want > budget) want = budget;
    while (n <= want) {                 /* slots of b .. b+want (successor too) */
        st = dr_slot(dr_next + n, ps, p);
        if (st >= NSLOT) break;
        sl[n++] = st;
    }
    if (n < 2) {                        /* nothing writable here */
        if (st == DR_BUSY) {            /* port busy: ask again, never skip */
            sm_drbusy++;
        } else if (n == 0 && dr_sube) {
            /* the finish line is exact (the last block read before the tap):
             * a block short of it that is not in the store yet was read but
             * is still on its way to the chip -- WAIT for it (ending here
             * would let it play with the old mix). 3 s without it = it is
             * never coming (a jump dropped it): give up, store unknown. */
            if (!dr_miss++) dr_misst = now;
            else if (now - dr_misst > 3000u) {
                dr_on = 0; dr_store_ok = 0; dr_miss = 0; sm_drwait++;
            }
            sm_drnoslot++;
        } else if (n == 0 && ++dr_miss >= DR_GAP) {
            /* finish line unknown (no read seen since the module started):
             * the labels stop here, treat it as the decoder's frontier. */
            dr_on = 0;
            dr_store_ok = 1;
            dr_miss = 0;
        } else {
            sm_drnoslot++;
        }
        sm_drskip++;
        return;
    }
    n--;                                /* the last one found only proves n-1 is complete */
    u32 save = POS(fp);
    short *old = (short *)(dtmp + 2 * BLKF * 4);    /* free while n == 1 */
    int fade = 0;
    if (dr_fade && dr_old < 16u && dr_old != dr_mask) {
        u32 m = dr_mask;                /* the mix that is in the slot now */
        n = 1;
        dr_mask = dr_old;
        fade = dr_mix(dr_next, 1, fp);
        dr_mask = m;
        if (fade)
            for (u32 i = 0; i < BLKF * 2; i++) old[i] = (short)dacc[i];
    }
    int sub = 0, subk = 0;
    if (dr_base_ok && !dr_fade && dr_mask && dr_old < 16u &&
        dr_next + n <= dr_sube) {           /* never where a new-mask read may reach */
        u32 one = dr_mask ^ dr_old;         /* exactly one stem changed? */
        if (one && !(one & (one - 1u))) {
            sub = 1;
            while (!(one & (1u << subk))) subk++;
        }
    }
    if (sub && !dr_checked) {           /* verify the stored mix once per pass */
        u32 save = POS(fp), m = dr_mask;
        dr_mask = dr_old;
        int same = dr_mix(dr_next, 1, fp) && dr_get_cmp(dr_next, sl[0], dacc);
        dr_mask = m;
        ((fseek_t)FSEEK3)(fp, (s32)save, 0);
        dr_checked = same ? 1u : 2u;
        sm_drchk = dr_checked;
        if (!same) sub = 0;             /* stale base: use the four-plane mix */
    }
    int ok = 0;
    if (sub) {
        ok = dr_mix_sub(dr_next, n, fp, sl, subk,
                        (dr_mask & (1u << subk)) != 0);
        if (ok) sm_drsub++;
        else sm_drsubf++;
    }
    if (!ok && !dr_mix(dr_next, n, fp)) {
        sm_drwfail++;
        /* past the end of the data: nothing more to do for this change */
        if (d_off + (dr_next + n) * BLKF * 4 > d_off + d_len) {
            dr_on = 0;
            dr_store_ok = 0;            /* past EOF: do not claim a known store */
        }
    } else {
        if (fade) {                     /* 512-frame ramp old -> new, rest new */
            for (u32 f = 0; f < 512u; f++)
                for (u32 c = 0; c < 2u; c++) {
                    u32 i = 2 * f + c;
                    dacc[i] = (old[i] * (s32)(512u - f) + dacc[i] * (s32)f) >> 9;
                }
            sm_drfade++;
        }
        for (u32 i = 0; i < n; i++) {
            u32 p2 = play_blk(0);       /* where the chip plays NOW */
            if (p2 == ~0u) { sm_drbusy++; break; }
            if (dr_next < p2 + DR_SAFE) { sm_drlate++; break; }   /* too late: leave it */
            { u32 tp = R32(MSCLK);
              int okp = dr_put(dr_next, sl[i], dacc + i * BLKF * 2);
              sm_tput += R32(MSCLK) - tp;
              if (!okp) { sm_drwfail++; break; } }
            dr_next++;
            sm_drblk++;
            dr_fade = 0;
        }
    }
    dr_miss = 0;
    ((fseek_t)FSEEK3)(fp, (s32)save, 0);
    { u32 dt = R32(MSCLK) - now; if (dt > sm_drms) sm_drms = dt; sm_drbat++; sm_drtot += dt; }
    rl_t = R32(MSCLK);                  /* pace from the END of the batch */
}

static void dr_step(u32 fp)
{
    dr_run(fp, DR_BUDGET);
}

/* record the header the chip has at the slot it says it is playing */
static void cap_pslot(void)
{
    u32 ps, p = play_blk(&ps);
    sm_hdrp = p;
    sm_hdrps = ps;
    sm_hdrmatch = 0xFFu;
    if (ps >= NSLOT)
        return;
    u32 base = RING + ps * SLOT + 4704u;
    for (u32 i = 0; i < 25u; i++) {
        u32 v = hrd1(base + 4u * i);
        sm_hdr[i] = v;
        if (v == p && sm_hdrmatch == 0xFFu)
            sm_hdrmatch = i;
    }
    sm_caps++;
}

#ifdef SM_MAP
/* ---- ring map (2026-11-09 diagnostic build, -DSM_MAP) -------------------
 * The deck keeps more than one labelled copy of a block, and rewriting a
 * correctly labelled one was not heard (3649 blocks, 2026-11-09 late). This
 * build REWRITES NOTHING: on a mask change it reads the label of every slot
 * (256 per poll, ~2 s for the whole store) and keeps them as runs of
 * consecutive labels {first slot, first label, length} (runs of 1 are
 * counted as junk), plus the playing block and the play slot when the map
 * started and when it finished. Read over the cable: build/stems_ringmap.py.
 * The normal mute still works through the read path (~28 s). */
#define MAP_MAX   256u
#define MAP_SLICE 256u
u32 sm_map[MAP_MAX * 3];
u32 sm_mapn, sm_mapdone, sm_mapjunk, sm_mapp0, sm_mapps0, sm_mapp1, sm_mapps1, sm_maplost;
static u32 map_next = NSLOT, run_s, run_l, run_n;

static void map_flush(void)
{
    if (run_n >= 2) {
        if (sm_mapn < MAP_MAX) {
            sm_map[sm_mapn * 3] = run_s;
            sm_map[sm_mapn * 3 + 1] = run_l;
            sm_map[sm_mapn * 3 + 2] = run_n;
            sm_mapn++;
        } else
            sm_maplost++;
    } else
        sm_mapjunk += run_n;
    run_n = 0;
}

static void map_start(void)
{
    sm_mapn = sm_mapjunk = sm_maplost = 0;
    sm_mapp0 = hrd1(PLAYBLK);
    sm_mapps0 = hrd1(PSLOT);
    run_n = 0;
    map_next = 0;
}

static void map_step(void)
{
    if (map_next >= NSLOT)
        return;
    u32 end = map_next + MAP_SLICE;
    if (end > NSLOT) end = NSLOT;
    for (u32 sl = map_next; sl < end; sl++) {
        u32 l = dr_lab(sl);
        if (run_n && l == run_l + run_n) {
            run_n++;
            continue;
        }
        map_flush();
        run_s = sl; run_l = l; run_n = 1;
    }
    map_next = end;
    if (map_next >= NSLOT) {
        map_flush();
        sm_mapp1 = hrd1(PLAYBLK);
        sm_mapps1 = hrd1(PSLOT);
        sm_mapdone++;
    }
}
#endif

static void poll_step(void)
{
    if (!valid || dfp_of != cur_fp)
        return;
    u32 mask = R8(PMASK) & 15;
    u32 dr_mask_now = pmask_seen;       /* the mix in the store before this change */
    (void)dr_mask_now;
    if (mask != pmask_seen) {
        pmask_seen = mask;
#ifdef SM_MAP
        cap_pslot();
        map_start();
        return;
    }
    map_step();
    return;
    {
#endif
        /* A fresh file's ring is the deck's original mix, so the first tap
         * can subtract one plane too. After any pass, only a completed pass
         * proves the store is one known mix. */
        dr_base_ok = !dr_on && (dr_store_ok || (dr_stock && dr_mask_now == 0));
        dr_stock = 0;
        dr_store_ok = 0;
        dr_checked = 0;
        dr_miss = 0;
        dr_old = dr_on ? ~0u : dr_mask_now;   /* mid-pass: store is mixed, no fade */
        dr_on = 1;
        pc_drop();
        dr_mask = mask;
        dr_next = 0;
        /* finish line = where reads with the NEW mask begin (or will) */
        dr_end = 0;                     /* unknown: dr_run uses p + MAXAHEAD */
        dr_sube = 0;
        if (rfront > d_off) {
            u32 e = (rd_mask == mask && rd_chg >= d_off) ? rd_chg : rfront;
            dr_sube = (e - d_off) / (BLKF * 4u);            /* blocks wholly old */
            dr_end = (e - d_off + BLKF * 4u - 1u) / (BLKF * 4u);
            if (!dr_end) dr_end = 1;
        }
        sm_drend = dr_end;
        sm_parm++;
        cap_pslot();            /* diagnostic: what the chip has at PSLOT */
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
#ifndef SM_NODR
    poll_step();
#endif
    return r;
}
#endif

#ifndef SM_NATIVE
/* 2026-11-09 deck: the old rule re-probed EVERY other handle the deck read
 * (analysis files, a second STEMS file being browsed or loaded): the STEMS
 * row flipped to "NO STEMS IN THIS TRACK" and the pending mute was dropped
 * (sm_files 15 -> 25 in one session). Now the current file only changes
 * after a TRACK LOAD (the loaded track's overview data changes, as hc.c and
 * pm.c detect it): the first STEMS file read after it becomes current, or a
 * non-stems file read 8 times in a row (that is the audio stream). Reads of
 * any other handle pass straight through and touch nothing. */
#define PREVIEW 0x0BA3BCCCu
u32 sm_switch, sm_foreign;              /* file switches / foreign reads */
static u32 trk_hash, armed = 1, cand_fp, cand_n, cand_st;
static u32 preview_hash(void)
{
    u32 h = 2166136261u;
    for (u32 i = 0; i < 100; i++) h = (h ^ R32(PREVIEW + 4 * i)) * 16777619u;
    return h;
}

/* 1 = fp is the deck's current track file (probed now if it just became
 * current), 0 = some other file: pass it through untouched */
static int current_file(u32 fp)
{
    u32 h = preview_hash();
    if (h != trk_hash) {                /* a new track was loaded */
        trk_hash = h;
        armed = 1;
        cand_fp = 0;
    }
    if (fp == cur_fp)
        return 1;
    if (!armed) {
        sm_foreign++;
        return 0;
    }
    if (fp != cand_fp) {
        cand_fp = fp;
        cand_n = 0;
        cand_st = (u32)is_stems(fp);
    }
    if (cand_st || ++cand_n >= 8) {     /* stems file, or the audio stream */
        probe(fp);
        dr_aslot = ~0u;                 /* the last slot belonged to the old track */
        dr_on = 0;
        dr_base_ok = 0;
        dr_store_ok = 0;
        dr_checked = 0;                 /* new file: do not subtract from old mixes */
        dr_miss = 0;
        dr_end = 0; dr_sube = 0;
        armed = 0;
        sm_switch++;
        return 1;
    }
    sm_foreign++;
    return 0;
}
#endif

/* The c-th masked section of a block: [*i0, *i0+len). dr_get8 and dr_put both
 * split a block with this, and the native gate asserts the sections tile the
 * block with the longest equal to DR_PIECE -- so neither the split nor the
 * drpk figure it reports can drift without a failing test (2026-11-11). */
u32 dr_piece(u32 c, u32 *i0)
{
    u32 a = c * DR_PIECE, b = a + DR_PIECE;
    if (b > BLKF * 2) b = BLKF * 2;
    *i0 = a;
    return b > a ? b - a : 0;
}

/* the native gate's view of the same numbers */
u32 dr_piece_expected(void) { return DR_PIECE; }
u32 dr_block_samples(void) { return BLKF * 2; }
/* 1 = the sections tile the block with no gap or overlap */
u32 dr_pieces(u32 *sections, u32 *total, u32 *longest)
{
    u32 tot = 0, mx = 0;
    for (u32 c = 0; c < DR_NPIECE; c++) {
        u32 i0, n = dr_piece(c, &i0);
        if (i0 != tot)
            return 0;
        tot += n;
        if (n > mx) mx = n;
    }
    if (sections) *sections = DR_NPIECE;
    if (total) *total = tot;
    if (longest) *longest = mx;
    return 1;
}

/* 2026-11-12 SHORT READ-AHEAD. The deck decodes ~30 s ahead, and mix-on-read
 * only reaches the speakers after that buffer has played out (the 30 s
 * mute). Here a read of the STEMS file that would land more than SM_AHEAD
 * blocks (1/75 s each) past the block now playing waits in DLY_MS steps
 * (system sleep 0x08E0335C, the firmware's own "wait N ms", 448 callers)
 * until playback catches up. The audio chip then never holds more than
 * ~SM_AHEAD of the old mix, so a mute is heard within that.
 * Safety: the play block is unknown or stands still for STILL_MS (paused,
 * cued, loading) -> read now; one call never waits longer than MAXW_MS.
 *
 * 2026-11-16 (deck: "extremely glitchy" with the rewrite on top):
 *  - SM_AHEAD 300 -> 340. The deck slows playback to a crawl when the chip's
 *    buffer falls to ~240 blocks (emulator, 2026-11-13). 300 left ~40 blocks
 *    over that with a stick that answers at once; the real stick needs
 *    ~5 ms per trip and a muted read makes 7 trips. 340 = 4.5 s.
 *  - RUN RULE. The old test only knew "this far past the playing block".
 *    After a jump FORWARD past the buffer (hot cue, beat jump, needle) the
 *    playing block is still the old place, so every read at the new place
 *    was held: 3 s each while the old audio kept playing, 150 ms each once
 *    it stood still -- seconds of silence per jump. Now the first SM_AHEAD
 *    blocks of every run of reads (a run starts wherever a read does not
 *    continue the one before: load, jump, loop) pass at once. This only
 *    ever lets a read through SOONER than before.
 * SM_AHEAD 0 = no hold at all (the 1.0 read path: ~30 s mute, known clean). */
#ifndef SM_AHEAD            /* 1.2 (2026-11-23): 340 = 4.5 s read-ahead, held   */
#define SM_AHEAD 340u       /* on the chip's buffer level; deck: mute + unmute */
#endif                      /* ~5 s, no crackle. -DSM_AHEAD=0u = the 1.0 path  */
u32 sm_runs;                            /* runs of reads started (loads, jumps) */

/* a read [pos, pos+n) of the current file is about to be made */
void thr_note(u32 pos, u32 n)
{
    if (pos != thr_end) { thr_run0 = pos; sm_runs++; }
    thr_end = pos + n;
}

/* 1 = let it through now. p = block playing (~0 unknown), doff = data start */
u32 thr_ok(u32 pos, u32 n, u32 p, u32 doff)
{
    u32 e = pos + n, w = SM_AHEAD * BLKF * 4u;
    if (e - thr_run0 <= w)                       /* the first SM_AHEAD of a run */
        return 1;
    return p != ~0u && e <= doff + (p + SM_AHEAD) * BLKF * 4u;
}
u32 thr_ahead(void) { return SM_AHEAD; }

#ifndef SM_NATIVE
#define DLY       0x08E0335Cu
#define DLY_MS    5u
#ifndef STILL_MS
#define STILL_MS  150u
#endif
#ifndef MAXW_MS
#define MAXW_MS   3000u
#endif
#define LEVEL     0x11837CD0u            /* chip: blocks buffered ahead (emulator; deck unproven) */
typedef s32 (*dly_t)(u32);
u32 sm_thr, sm_thrms, sm_thrpause;      /* reads held / ms held / gave up (paused) */
/* 2026-11-16: is the chip kept fed? All "+1, 0 = none yet"; the *pre copies
 * are the values before the last mask change, so one probe after a mute shows
 * both the plain and the muted (7 stick trips per read) state.
 *  thrlo  = lowest read-ahead (blocks) at which a read ARRIVED un-held past
 *           the start of its run: far under SM_AHEAD = reads are late;
 *  lvlmin = lowest chip buffer level seen at a read;
 *  thrmaxw = holds that ran into MAXW_MS;  rdms/rdtot = longest / total ms of
 *           the muted reads (sm_hits of them). */
u32 sm_thrlo, sm_thrlopre, sm_lvl, sm_lvlmin, sm_lvlpre, sm_thrmaxw, sm_rdms, sm_rdtot;
/* 2026-11-17: which rule held a read -- the chip's buffer level or the old
 * position fallback. A session where sm_posh stays 0 proves the level is the
 * gate; anything else says the word was unreadable there. */
u32 sm_lvlh, sm_posh;
#define MSCLK_U   0xAB704A80u            /* 1 ms system clock, uncached (TMU ISR) */
/* 2026-11-17: the gate is the CHIP'S OWN BUFFER LEVEL (0x11837CD0, blocks of
 * 1/75 s = 588 stereo frames), not an estimate of the playhead.
 * 2026-11-17 deck: the position gate believed reads were 271 blocks ahead
 * while the chip held ~1568 -- and the 11-09 ring map had already shown both
 * PLAYBLK and the label at PSLOT disagreeing with the real playhead (mid-track
 * PLAYBLK even fell out of the store). The buffer level is the quantity that
 * actually decides how much OLD mix (decoded before the tap) is still queued,
 * so holding reads while it is at or over SM_AHEAD bounds the mute delay by
 * construction: no calibration, immune to wraps, duplicate slots and jumps.
 * It also sits the buffer at ~SM_AHEAD (340) instead of the 271 the position
 * gate produced, i.e. further from the deck's own ~240-block low-buffer rule.
 * Fallback: if the word is unreadable (port busy) or not a plausible level
 * (0 or >= 8192 -- some other meaning on a deck we have not seen), the old
 * position rule decides. sm_lvlh / sm_posh count which one held. */
#define LVL_MAX   8192u                  /* sanity bound for "blocks buffered"  */
static u32 thr_lvl(void)
{
    u32 lv = hrd1(LEVEL);
    return (lv >= 1u && lv < LVL_MAX) ? lv : ~0u;
}

static void throttle(u32 pos, u32 n)
{
    u32 last = ~0u, lastlv = ~0u, t0 = R32(MSCLK_U), tl = t0, w = SM_AHEAD * BLKF * 4u;
    int held = 0;
    thr_note(pos, n);
    for (;;) {
        u32 p = play_blk(0), now = R32(MSCLK_U), lv = thr_lvl();
        int ok, by = 1;                  /* by: 1 = level gate, 2 = position */
        if (pos + n - thr_run0 <= w)     /* first SM_AHEAD of a run: load / jump */
            ok = 1;
        else if (lv != ~0u)
            ok = lv < SM_AHEAD;
        else {
            ok = p != ~0u && pos + n <= d_off + (p + SM_AHEAD) * BLKF * 4u;
            by = 2;
        }
        if (ok) {
            if (!held && p != ~0u && pos + n - thr_run0 > SM_AHEAD * BLKF * 4u) {
                u32 fb = (pos + n - d_off) / (BLKF * 4u);
                u32 a = fb > p ? fb - p : 0;
                if (!sm_thrlo || a + 1u < sm_thrlo) sm_thrlo = a + 1u;
            }
            break;
        }
        /* "still playing" = the play block moved OR the chip's buffer level
         * changed. 2026-11-23 deck: the play block only updates now and
         * then while playing, so 150 ms without a change let 613 reads
         * through as "paused" and the read-ahead crept to 1281 blocks (17 s:
         * the very slow unmute). The level drains ~1 block per 13 ms while
         * playing and stands still when paused. */
        if ((p != ~0u && p != last) || lv != lastlv) { last = p; lastlv = lv; tl = now; }
        if (now - tl >= STILL_MS) { sm_thrpause++; break; }
        if (now - t0 >= MAXW_MS) { sm_thrmaxw++; break; }
        if (!held) {
            held = 1; sm_thr++;
            if (by == 1) sm_lvlh++; else sm_posh++;
        }
        ((dly_t)DLY)(DLY_MS);
    }
    if (held) sm_thrms += R32(MSCLK_U) - t0;
    u32 lv = hrd1(LEVEL);
    if (lv < 8192u) {
        sm_lvl = lv;
        if (!sm_lvlmin || lv + 1u < sm_lvlmin) sm_lvlmin = lv + 1u;
    }
}
#endif

u32 sm_fread(void *buf, u32 size, u32 count, u32 fp)
{
    sm_calls++;
#ifndef SM_NATIVE
    if (!current_file(fp))
        return ((fread_t)sm_prev)(buf, size, count, fp);
#else
    if (fp != cur_fp)
        probe(fp);
#endif
#ifndef SM_NATIVE
    if (valid && SM_AHEAD && size && count)
        throttle(POS(fp), size * count);     /* mask read AFTER the wait */
#endif
    u32 mask = R8(PMASK) & 15;
    u32 pos = POS(fp), n = size * count;
    sm_lastn = n;
    sm_lastpos = pos;
    sm_lastbuf = (u32)buf;
    if (n > sm_maxn) sm_maxn = n;
    if (!sm_buflo || sm_lastbuf < sm_buflo) sm_buflo = sm_lastbuf;
    { u32 e = sm_lastbuf + n; if (e > sm_bufhi) sm_bufhi = e; }
    u32 lo = d_off, hi = d_off + d_len;
    if (valid && n != 0 && size != 0 && pos + n > d_off) {
        if (mask != rd_mask) {          /* first read with this mask */
            rd_mask = mask;
            rd_chg = pos > d_off ? pos : d_off;
        }
        rfront = pos + n;               /* before the read: in flight counts */
    }
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
            sm_thrlopre = sm_thrlo; sm_thrlo = 0;
            sm_lvlpre = sm_lvlmin; sm_lvlmin = 0;
            /* the decoded copy in the audio chip: from the playhead up to
             * what was read before this change (later reads get mixed).
             * Driven from sm_poll once that runs (fast trigger above). */
#ifdef SM_NODR
            if (0) {
#else
            if (!sm_polls) {
#endif
                dr_old = ~0u;
                dr_stock = 0;
                dr_on = 1; dr_mask = mask; dr_next = 0;
                dr_end = 0;             /* fixed by dr_step at p + MAXAHEAD */
            }
#endif
        }
#ifndef SM_NATIVE
#ifndef SM_NODR
        if (!sm_polls)
            dr_step(fp);            /* restores fp to pos */
#endif
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
        if (valid) SIMLAT();
        return ((fread_t)sm_prev)(buf, size, count, fp);
    }
    /* plain read first (any size, the deck's real reads are big); then
     * overwrite the part inside the sample data with the mix, 16 KB at a
     * time, 4-byte frames aligned to the data start */
#ifndef SM_NATIVE
    u32 tr0 = R32(MSCLK_U);
#endif
    SIMLAT();
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
#ifndef SM_NATIVE
    { u32 dt = R32(MSCLK_U) - tr0; sm_rdtot += dt; if (dt > sm_rdms) sm_rdms = dt; }
#endif
    sm_hits++;
    return got;
}
