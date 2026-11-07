/* fxmemhook.S -- flush-hook trampoline for the one-shot DSP survey
 * (tools/fx_dsp/fxmem.c). Same shape as kshhook.S: save the argument
 * registers, call the C hook, restore them, then jump to whatever the pool
 * held before (sv_prev, written by the host before arming). */
        .section .entry,"ax"
        .global _sv_entry
        .align 2
_sv_entry:
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
.Lhook: .long   _sv_hook
.Lprev: .long   _sv_prev

        .section .data
        .global _sv_prev, _sv_flag, _sv_done, _sv_res, _sv_page
        .align 2
_sv_prev:    .long 0x08E7D278    /* stock 2DG flush; host rewrites */
_sv_flag:    .long 0             /* 'SVR1' arms one survey         */
_sv_done:    .long 0
_sv_page:    .long 0xC006C000    /* page to scan fully; host rewrites */
_sv_res:     .long 0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0
             .long 0,0,0,0,0,0,0
             .long 0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0
             .long 0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0
             .long 0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0
             .long 0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0
