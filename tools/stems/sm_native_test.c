/* Mac test of sm.c's mixing with deck-sized reads: the file lives in
 * memory, fread/fseek are mocks with a position per handle (fp = 1).
 * usage: sm_native_test FILE MASK pos:n [pos:n ...] -> concatenated reads */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef unsigned char u8; typedef unsigned int u32;
u8 n_mask;
u8 n_vsel; u32 n_vfp, n_vtbl[12];   /* version table (unused here: zeros) */
static u8 *F; static long FL; static long P;
u32 n_fread(void *b, u32 sz, u32 cnt, u32 fp)
{ (void)fp; long n = (long)sz * cnt; if (P + n > FL) n = FL - P; if (n < 0) n = 0;
  memcpy(b, F + P, n); P += n; return sz ? (u32)(n / sz) : 0; }
int n_fseek(u32 fp, int off, int wh)
{ (void)fp; long p = wh == 0 ? off : wh == 1 ? P + off : FL + off;
  if (p < 0 || p > FL) return -1; P = p; return 0; }
u32 n_pos(u32 fp) { (void)fp; return (u32)P; }
u32 sm_fread(void *, u32, u32, u32);
/* the section split, as sm.c computes it (2026-11-11: gate the masked-section
 * size so it cannot drift unnoticed -- it is what the deck hears as a stutter) */
u32 dr_pieces(u32 *, u32 *, u32 *);
u32 dr_piece_expected(void);
u32 dr_block_samples(void);
void thr_note(u32, u32);
u32 thr_ok(u32, u32, u32, u32);
u32 thr_ahead(void);
/* the short read-ahead's decision (2026-11-16): steady state holds what is
 * past play + AHEAD; the first AHEAD blocks of a new run always pass */
static int thr_test(void)
{
    const u32 B = 2352, A = thr_ahead(), D = 44, R = 9408;
    int bad = 0;
    if (!A) { printf("thr: SM_AHEAD 0, nothing to test\n"); return 0; }
#define CHK(c) do { if (!(c)) { printf("thr FAIL line %d\n", __LINE__); bad = 1; } } while (0)
    u32 pos = D;
    thr_note(pos, R);                                   /* load: run starts */
    CHK(thr_ok(pos, R, ~0u, D));                        /* play block unknown: first window passes */
    for (; pos + R <= D + A * B; pos += R) { thr_note(pos, R); CHK(thr_ok(pos, R, ~0u, D)); }
    thr_note(pos, R);
    CHK(!thr_ok(pos, R, ~0u, D));                       /* past the window, nothing playing: hold */
    CHK(!thr_ok(pos, R, 0, D));                         /* playing block 0: still too far */
    CHK(thr_ok(pos, R, (pos + R - D) / B - A + 1, D));  /* playback caught up: pass */
    /* steady state far into the file */
    pos = D + 5000 * B; thr_note(pos, R);               /* (a jump: new run) */
    for (u32 k = 0; k < A / 4 + 8; k++) { pos += R; thr_note(pos, R); }
    CHK(!thr_ok(pos, R, 5000, D));                      /* run window used up, play at 5000 */
    CHK(thr_ok(pos, R, 5000 + A / 4 * 4 + 40 - A + 4, D) || thr_ok(pos, R, (pos + R - D) / B - A + 1, D));
    CHK(!thr_ok(pos, R, (pos + R - D) / B - A - 2, D)); /* 2 blocks short: hold */
    /* jump forward far past the buffer while the old place still plays */
    u32 pold = (pos - D) / B - A, j = D + 20000 * B;
    thr_note(j, R);
    CHK(thr_ok(j, R, pold, D));                         /* was: held 3 s per read */
    u32 q = j;
    for (; q + R <= j + A * B; q += R) { thr_note(q, R); CHK(thr_ok(q, R, pold, D)); }
    thr_note(q, R);
    CHK(!thr_ok(q, R, pold, D));                        /* window full, chip not there yet */
    CHK(thr_ok(q, R, 20000 + 8, D));                    /* chip plays the new place */
    /* jump back */
    thr_note(D + 100 * B, R);
    CHK(thr_ok(D + 100 * B, R, 20000, D));
    printf("thr: run rule + steady state %s (SM_AHEAD %u)\n", bad ? "FAIL" : "ok", A);
    return bad ? 3 : 0;
}

int main(int c, char **v)
{
    if (c > 1 && strcmp(v[1], "thr") == 0)
        return thr_test();
    if (c > 1 && strcmp(v[1], "pieces") == 0) {
        u32 sec = 0, tot = 0, mx = 0;
        if (!dr_pieces(&sec, &tot, &mx)) {
            printf("PIECE GAP: the sections do not tile the block\n");
            return 3;
        }
        if (tot != dr_block_samples() || mx != dr_piece_expected()) {
            printf("PIECE DRIFT: %u sections cover %u of %u samples, longest %u, "
                   "DR_PIECE %u\n", sec, tot, dr_block_samples(), mx,
                   dr_piece_expected());
            return 3;
        }
        printf("sections %u per block, cover %u samples, longest %u == DR_PIECE\n",
               sec, tot, mx);
        return 0;
    }
    FILE *f = fopen(v[1], "rb"); fseek(f, 0, 2); FL = ftell(f); fseek(f, 0, 0);
    F = malloc(FL); fread(F, 1, FL, f); fclose(f);
    n_mask = (u8)strtol(v[2], 0, 0);
    static u8 buf[1 << 21];
    for (int i = 3; i < c; i++) {
        long pos, n; sscanf(v[i], "%ld:%ld", &pos, &n);
        P = pos;
        u32 got = sm_fread(buf, 1, (u32)n, 1);
        if (P != pos + got) { fprintf(stderr, "POS WRONG after read %d\n", i); return 2; }
        fwrite(buf, 1, got, stdout);
    }
    return 0;
}
