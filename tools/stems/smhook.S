/* entry table at SM_BASE: +0 sm_fread (the read pool word points here),
 * +4 sm_poll_entry (the player task's buffer-level poll pool 0x08C1D56C).
 *
 * sm_poll runs on the module's own 8 KB stack (sm_stack in sm.c): its
 * rewrite reads the stick through the file system, which can need ~4 KB of
 * stack on a FAT walk -- more than the player task has (emulator: the task
 * hung after ~720 rewritten blocks on its own stack; same lesson as the FLAC
 * layer, tools/xflac/vfstack.S). Claimed with interrupts masked; if it is
 * already in use the call goes straight to the stock accessor (no rewrite
 * this time). Arguments r4..r7 and the result r0 pass straight through. */
        .section .entry,"ax"
        .global _sm_table
        .align 2
_sm_table:
        .long   _sm_fread
        .long   _sm_poll_entry

        .text
        .align 2
        .global _sm_poll_entry
_sm_poll_entry:
        stc     sr,r3
        mov     r3,r2
        mov.l   .Limask,r1
        or      r1,r2
        ldc     r2,sr
        mov.l   .Lbusy,r1
        mov.l   @r1,r2
        tst     r2,r2
        bf      .Linuse
        mov     #1,r2
        mov.l   r2,@r1
        ldc     r3,sr
        mov     r15,r2
        mov.l   .Ltop,r15
        mov.l   r2,@-r15
        sts.l   pr,@-r15
        mov.l   .Lbody,r0
        jsr     @r0
        nop
        lds.l   @r15+,pr
        mov.l   @r15,r2
        mov.l   .Lbusy,r1
        mov     #0,r3
        mov.l   r3,@r1
        rts
        mov     r2,r15
.Linuse:
        ldc     r3,sr
        mov.l   .Lacc,r0
        jmp     @r0
        nop

        .align 2
.Limask: .long  0x000000F0
.Lbusy: .long   _sm_stack_busy
.Ltop:  .long   _sm_stack + 8192
.Lbody: .long   _sm_poll
.Lacc:  .long   0x08BF2BCC
