/* QuRT loads the RX and RW regions on separate 4 KiB boundaries. */
PHDRS
{
    text PT_LOAD FILEHDR PHDRS FLAGS(5);
    data PT_LOAD FLAGS(6);
    dynamic PT_DYNAMIC FLAGS(6);
}
SECTIONS
{
    . = SIZEOF_HEADERS;
    .dynsym : { *(.dynsym) } :text
    .dynstr : { *(.dynstr) } :text
    .hash : { *(.hash) } :text
    .text : { *(.text .text.*) } :text
    .rodata : { *(.rodata .rodata.*) } :text
    . = ALIGN(4096);
    .dynamic : { *(.dynamic) } :data :dynamic
    .data : { *(.data .data.*) } :data
    .got : { *(.got .got.*) } :data
    .bss : { *(.bss .bss.* COMMON) } :data
    /DISCARD/ : { *(.comment .note.GNU-stack) }
}
