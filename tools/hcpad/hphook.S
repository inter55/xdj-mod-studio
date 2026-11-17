/* hphook.S -- hot cue pad swipe (hp.c): touch-pool entry. Calls
 * hp_touch(down, x, y): 0 = chain with the original args, 1 = consumed
 * (return like the pads do), 2 = chain with hp_fwd (down, x, y). */
        .section .entry,"ax"
        .global _hp_touch_entry
        .align 2
_hp_touch_entry:
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
        cmp/eq  #2,r0
        bf      .Lnofwd
        mov.l   .Lfwd,r1
        mov.l   @r1,r4
        mov.l   @(4,r1),r5
        bra     .Lchain
        mov.l   @(8,r1),r6
.Lnofwd:
        tst     r0,r0
        bf      .Lconsumed
.Lchain:
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
.Ltouch:  .long _hp_touch
.Lptouch: .long _hp_prev_touch
.Lfwd:    .long _hp_fwd
        .section .data
        .global _hp_prev_touch
        .align 2
_hp_prev_touch: .long 0x08DD0128   /* installer overwrites with the live pool */
