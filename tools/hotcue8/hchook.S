/* hchook.S -- entry table at HC_BASE: +0 flush-hook entry (calls
 * hc_hook(dev, req) then chains to hc_prev), +4 hc_next (HOT CUE tab swipe
 * left, called from perform_pads), +8 hc_prev_page (swipe right), +12
 * hc_toggle (tap: 8-pad view <-> the stock row).
 *
 * The touch entry (+0 of _hc_tentry) sits in front of the stock hit-tester
 * only while the 8-pad view exists; hc_touch answers 2 = swallow, else it
 * chains hc_tprev with the arguments untouched. */
        .section .entry,"ax"
        .global _hc_entry
        .global _hc_tentry
        .align 2
_hc_table:
        .long   _hc_entry
        .long   _hc_next
        .long   _hc_prev_page   /* +8 (2026-10-13: swipe right) */
        .long   _hc_toggle      /* +12 (2026-11-07: tap = 8-pad view) */
_hc_entry:
        sts.l   pr,@-r15
        mov.l   r4,@-r15
        mov.l   r5,@-r15
        mov.l   r6,@-r15
        mov.l   r7,@-r15
        mov.l   .Lhook,r0
        jsr     @r0
        nop
        mov.l   @r15+,r7
        mov.l   @r15+,r6
        mov.l   @r15+,r5
        mov.l   @r15+,r4
        lds.l   @r15+,pr
        mov.l   .Lprev,r0
        mov.l   @r0,r0
        jmp     @r0
        nop
        .align 2
.Lhook: .long   _hc_hook
.Lprev: .long   _hc_prev
_hc_tentry:
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
        lds.l   @r15+,pr
        cmp/eq  #2,r0
        bt      .Lswallow
        mov.l   .Ltprev,r0
        mov.l   @r0,r0
        jmp     @r0
        nop
.Lswallow:
        rts
        nop
        .align 2
.Ltouch: .long  _hc_touch
.Ltprev: .long  _hc_tprev

        .section .data
        .global _hc_prev
        .global _hc_tprev
        .align 2
_hc_prev: .long 0x08E7D278      /* stock flush; installer overwrites */
_hc_tprev: .long 0x08DD0128     /* stock hit-tester; installer overwrites */
