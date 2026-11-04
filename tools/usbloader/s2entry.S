/* s2entry.S -- first bytes of MODS.BIN (entry_off 0): jump to s2_main at
 * its P2 (uncached) link address, so the installer never runs from the
 * instruction cache. +8 points at s2_state. */
        .section .entry,"ax"
        .global _s2_head
        .align 2
_s2_head:
        mov.l   1f,r0
        jmp     @r0
        nop
        .align 2
1:      .long   _s2_main
        .long   _s2_state
