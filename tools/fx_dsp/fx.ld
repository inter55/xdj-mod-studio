ENTRY(fx_pull)
SECTIONS {
  . = 0xC005F4C0;
  .text : { *(.text.fx_pull) *(.text*) }
  .rodata : { *(.rodata*) *(.const*) }
  . = ALIGN(8);
  FX = .;
  . += 0x180;
  /DISCARD/ : { *(.data*) *(.bss*) *(.neardata*) *(.fardata*) *(.comment) *(.c6xabi.*) }
}
