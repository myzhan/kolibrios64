;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Symbol-bearing ELF64 wrapper around kernel/kernel64.asm, assembled by
;; tools/gen-kernel-symbols.py. The kernel source itself stays untouched.
;;
;; format elf64 (fasm2 include/format/elf64.inc) emits an ET_REL object with
;; a .symtab fed by the public macro. kernel64.asm keeps its absolute
;; `org VIRT_KERNEL_BASE`, so every published label is an absolute value and
;; lands in the symbol table as SHN_ABS with the full runtime address, which
;; is exactly what gdb add-symbol-file / lldb target create need against the
;; QEMU gdbstub. The emitted code bytes are identical to KERNEL64.BIN minus
;; its 40 byte header; gen-kernel-symbols.py verifies both facts.
;;
;; Assemble exactly like build_fasm2.sh assembles the kernel, with the fasm2
;; include directory on INCLUDE and fasm2.inc preloaded:
;;
;;   INCLUDE=<fasm2>/include <host> -e100 -i"Include 'fasm2.inc'" \
;;       tools/kernel64-elf.asm kernel64.elf

format elf64

include '../kernel/kernel64.asm'

public kernel_header
public k64_entry
public com1_init
public com1_putc
public com1_puts
public com1_puthex
public halt_forever