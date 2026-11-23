/* tfentry.S -- fixed header at TF_BASE (tools/trackfilter/tf.c).
 *   +0  "TFLT" magic        +4  tf_copy (the hook: the firmware's three
 *   +8  &tf_cfg (settings)       list-copy pools point here)
 *   +12 &tf_prev (the copy routine we chain to; 0 = not installed)
 *   +16 tf_get(k) / +20 tf_put(k, v): the conditions, read from / written
 *       through the deck's own filter-condition store (UTILITY rows, screen) */
        .section .entry,"ax"
        .global _tf_head
        .align 2
_tf_head:
        .long   0x544C4654
        .long   _tf_copy
        .long   _tf_cfg
        .long   _tf_prev
        .long   _tf_get
        .long   _tf_put
        .long   _tfs_txt_entry          /* +24 text-blender hook */

/* screen hooks (tfs.c), chained in front like TOUCH FX (tfxhook.S):
 * tfs_flush_entry on the display-flush pool 0x08C92E20, tfs_touch_entry
 * on the hit-tester pool 0x08D5278C (1 = consumed). */
        .global _tfs_flush_entry, _tfs_touch_entry
        .align 2
_tfs_flush_entry:
        sts.l   pr,@-r15
        mov.l   r4,@-r15
        mov.l   r5,@-r15
        mov.l   r6,@-r15
        mov.l   r7,@-r15
        mov.l   .Lflush,r0
        jsr     @r0
        nop
        mov.l   @r15+,r7
        mov.l   @r15+,r6
        mov.l   @r15+,r5
        mov.l   @r15+,r4
        lds.l   @r15+,pr
        mov.l   .Lpflush,r0
        mov.l   @r0,r0
        jmp     @r0
        nop
        .align 2
_tfs_touch_entry:
        sts.l   pr,@-r15
        mov.l   r4,@-r15
        mov.l   r5,@-r15
        mov.l   r6,@-r15
        mov.l   r7,@-r15
        mov.l   .Ltouch,r0
        jsr     @r0
        nop
        mov.l   @r15+,r7
        mov.l   @r15+,r6
        mov.l   @r15+,r5
        mov.l   @r15+,r4
        tst     r0,r0
        bf      .Lconsumed
        lds.l   @r15+,pr
        mov.l   .Lptouch,r0
        mov.l   @r0,r0
        jmp     @r0
        nop
.Lconsumed:
        lds.l   @r15+,pr
        rts
        nop
        .align 2
.Lflush:  .long _tfs_flush
.Ltouch:  .long _tfs_touch
.Lpflush: .long _tfs_prev_flush
.Lptouch: .long _tfs_prev_touch

/* the firmware's per-pixel text blender FUN_08C2F5C2(en, dst pixel, ..)
 * (pools 0x08C2F8FC, 0x08C31FC4): while the TRACK FILTER screen is open,
 * a pixel inside it (tfs_tlo <= dst & 0x1FFFFFFF < tfs_thi) is dropped,
 * like the blender's own en == 0 exit. The browse keeps redrawing its
 * header and title text with it (deck 2026-11-22: drawn over the screen).
 * Everything else, and everything when the screen is shut (tfs_tlo 0),
 * goes on to the blender (or whoever owned the pools: tfs_prev_txt). */
        .text
        .global _tfs_txt_entry
        .align 2
_tfs_txt_entry:
        mov.l   .Ltlo,r0
        mov.l   @r0,r1
        tst     r1,r1
        bt      .Ltxtgo
        mov.l   .Lphys,r2
        mov     r5,r3
        and     r2,r3
        cmp/hs  r1,r3
        bf      .Ltxtgo
        mov.l   .Lthi,r0
        mov.l   @r0,r2
        cmp/hs  r2,r3
        bt      .Ltxtgo
        rts
        nop
.Ltxtgo:
        mov.l   .Lptxt,r0
        mov.l   @r0,r0
        jmp     @r0
        nop
        .align 2
.Ltlo:  .long _tfs_tlo
.Lthi:  .long _tfs_thi
.Lphys: .long 0x1FFFFFFF
.Lptxt: .long _tfs_prev_txt

        .section .data
        .global _tfs_prev_flush, _tfs_prev_touch, _tfs_prev_txt
        .align 2
_tfs_prev_flush: .long 0x08E7D278   /* stock flush; installer overwrites */
_tfs_prev_touch: .long 0x08DD0128   /* stock hit-tester; installer overwrites */
_tfs_prev_txt:   .long 0x08C2F5C2   /* stock text blender; installer overwrites */
