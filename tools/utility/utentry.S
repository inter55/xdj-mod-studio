/* utentry.S -- fixed header at UT_BASE: +0 "UTIL" magic, +4 ut_tab
 * (the extended UTILITY row table), +8 number of our rows. */
        .section .entry,"ax"
        .global _ut_head
        .align 2
_ut_head:
        .long   0x4C495455
        .long   _ut_tab
        .long   13
