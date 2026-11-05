/* ldentry.S -- fixed header at LD_BASE: +0 "LDR2", +4 ld_open (what the
 * hooked open pool word 0x08DE39A4 points at), +8 ld_state. */
        .section .entry,"ax"
        .global _ld_head
        .align 2
_ld_head:
        .long   0x3252444C
        .long   _ld_open
        .long   _ld_state
        .long   0
