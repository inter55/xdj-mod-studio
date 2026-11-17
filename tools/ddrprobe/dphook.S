/* ksrhook.S -- one-shot live DSP patch (no reset) on the display-flush hook
 * (pool 0x08C92E20). Calls ksr_hook() in C, then chains to whatever the
 * pool held before (ksr_prev, set by the installer) with the original
 * arguments. ks_dsp = the DSP-side key-shift code (tools/keyshift_dsp/ks.c),
 * appended to the DSP program copy by ksr_hook. */
        .section .entry,"ax"
        .global _dp_entry
        .align 2
_dp_entry:
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
.Lhook: .long   _dp_hook
.Lprev: .long   _dp_prev

        .section .data
        .global _dp_prev, _dp_flag, _dp_status, _dp_same, _dp_tries, _dp_hpia, _dp_lo, _dp_hi
        .align 2
_dp_prev:   .long 0x08E7D278
_dp_flag:   .long 0
_dp_status: .long 0
_dp_same:   .long 0
_dp_tries:  .long 0
_dp_hpia:   .long 0
_dp_lo:     .fill 16,4,0
_dp_hi:     .fill 16,4,0
