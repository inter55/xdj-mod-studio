/* tfxhook.S -- TOUCH FX entries.
 * tfx_flush_entry: display-flush pool 0x08C92E20; calls tfx_flush(dev, req)
 *   in C, then chains to tfx_prev_flush with the original arguments.
 * tfx_touch_entry: hit-tester pool 0x08D5278C (down, x, y, r7); calls
 *   tfx_touch(down, x, y); nonzero = consumed (return to the caller like
 *   the pads do), zero = chain to tfx_prev_touch with the original args. */
        .section .entry,"ax"
        .global _tfx_flush_entry, _tfx_touch_entry
        .align 2
_tfx_flush_entry:
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
_tfx_touch_entry:
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
.Lflush:  .long _tfx_flush
.Ltouch:  .long _tfx_touch
.Lpflush: .long _tfx_prev_flush
.Lptouch: .long _tfx_prev_touch

        .section .data
        .global _tfx_prev_flush, _tfx_prev_touch
        .align 2
_tfx_prev_flush: .long 0x08E7D278   /* stock flush; installer overwrites */
_tfx_prev_touch: .long 0x08DD0128   /* stock hit-tester; installer overwrites */
