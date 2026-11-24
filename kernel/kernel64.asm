;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; 64-bit kernel, first milestone: take over from the UEFI loader at
;; VIRT_KERNEL_BASE, switch to the kernel stack, validate K64BOOTINFO and
;; report a single matchable milestone over COM1. Boot services are already
;; gone here, so nothing but the CPU, our pages and the UART may be used.
;;
use64

; fasm2 native macro headers, see kernel/fasm2_compat.inc
include 'fasm2_compat.inc'
include 'const.inc'
include 'boot/bootinfo.inc'

org VIRT_KERNEL_BASE

DEFAULT_STACK_SIZE = 4096*4

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; file header, must match KERNEL64_HEADER from boot/bootinfo.inc
kernel_header:
        dq 'KERNEL64'                        ; magic
        dq k64_entry - VIRT_KERNEL_BASE      ; entry point offset
        dq DEFAULT_STACK_SIZE                ; stack size in bytes
        dq 0                                 ; phys_start, filled by the loader
        dq 0                                 ; phys_end, filled by the loader

if ($ - kernel_header) <> sizeof.KERNEL64_HEADER
        display "kernel header does not match KERNEL64_HEADER", 13, 10
        err
end if

PORT_COM1 = 0x3f8

macro outb port_, byte_
        mov     dx, port_
        mov     al, byte_
        out     dx, al
end macro

macro inb port_
        mov     dx, port_
        in      al, dx
end macro

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; stop the machine for good; every path that cannot continue ends here

halt_forever:
        cli
@@:
        hlt
        jmp     @b

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; initialise COM1 (115200 8N1, FIFO on, no interrupts) and test the UART
; out
; eax = 1 on success, 0 if the UART does not answer the loopback test
; destroys al, dx, ecx
com1_init:
        outb    PORT_COM1 + 1, 0x00 ; no interrupts
        outb    PORT_COM1 + 3, 0x80 ; DLAB on
        outb    PORT_COM1 + 0, 0x01 ; divisor = 1, 115200 baud
        outb    PORT_COM1 + 1, 0x00
        outb    PORT_COM1 + 3, 0x03 ; 8 bits, no parity, one stop bit
        outb    PORT_COM1 + 2, 0xC7 ; FIFO on, cleared
        outb    PORT_COM1 + 4, 0x03 ; DTR, RTS, OUT2
        ; loopback: the byte we send must come back unchanged
        outb    PORT_COM1 + 4, 0x1E
        outb    PORT_COM1 + 0, 0xAE
        mov     ecx, 0x10000
.wait_rx:
        inb     PORT_COM1 + 5
        test    al, 0x01
        jnz     .rx_ready
        dec     ecx
        jnz     .wait_rx
        jmp     .failed
.rx_ready:
        inb     PORT_COM1 + 0
        cmp     al, 0xAE
        jne     .failed
        ; back to normal operation
        outb    PORT_COM1 + 4, 0x0F
        mov     eax, 1
        ret
.failed:
        outb    PORT_COM1 + 4, 0x0F
        xor     eax, eax
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; write one byte to COM1, waiting for the transmit register
; in
; r8b = byte to write
; destroys al, dx
com1_putc:
.wait_tx:
        inb     PORT_COM1 + 5
        test    al, 0x20
        jz      .wait_tx
        outb    PORT_COM1, r8b
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; write a zero terminated string to COM1
; in
; rsi = string
; destroys al, dx, r8b, rsi
com1_puts:
.loop:
        mov     r8b, [rsi]
        test    r8b, r8b
        jz      .done
        call    com1_putc
        inc     rsi
        jmp     .loop
.done:
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; write a 64-bit value as 0x prefixed hex to COM1
; in
; rax = value
; destroys rax, rcx, rdx, r8b, r9
com1_puthex:
        mov     r9, rax
        mov     r8b, '0'
        call    com1_putc
        mov     r8b, 'x'
        call    com1_putc
        mov     ecx, 60
.loop:
        mov     rax, r9
        shr     rax, cl
        and     al, 0x0F
        add     al, '0'
        cmp     al, '9'
        jbe     .digit
        add     al, 'A' - ('9' + 1)
.digit:
        mov     r8b, al
        call    com1_putc
        sub     ecx, 4
        jns     .loop
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; 64 bit kernel entry point
; in
; rcx = physical address of K64BOOTINFO, readable through the loader's
;       temporary identity map
; out
; never returns, the machine is halted after reporting the milestone
k64_entry:
        cli
        cld
        mov     r15, rcx

        call    com1_init
        test    eax, eax
        jz      halt_forever ; no console, nothing to report

        ; the loader must have passed a complete version 1 BootInfo
        test    r15, r15
        jz      fail_no_bootinfo
        ; it must stay below the identity map the loader keeps alive
        mov     rax, K64_IDENTITY_MAP_LIMIT
        cmp     r15, rax
        jae     fail_no_bootinfo
        mov     rax, qword [r15 + K64BOOTINFO.signature]
        mov     rbx, K64BOOTINFO_SIGNATURE
        cmp     rax, rbx
        jne     fail_no_bootinfo
        mov     eax, dword [r15 + K64BOOTINFO.version]
        cmp     eax, K64BOOTINFO_VERSION
        jne     fail_bad_version
        mov     eax, dword [r15 + K64BOOTINFO.struct_size]
        cmp     eax, sizeof.K64BOOTINFO
        jb      fail_bad_version

        ; only features that are really provided may be flagged
        mov     rax, qword [r15 + K64BOOTINFO.flags]
        mov     rbx, K64BOOTINFO_FLAG_KNOWN
        not     rbx
        test    rax, rbx
        jnz     fail_bad_flags
        mov     rbx, K64BOOTINFO_FLAG_MEMORY_MAP
        test    rax, rbx
        jz      fail_bad_flags

        ; the memory map copy must be a non empty run of descriptors
        mov     rax, qword [r15 + K64BOOTINFO.memmap_addr]
        test    rax, rax
        jz      fail_bad_memmap
        mov     rax, qword [r15 + K64BOOTINFO.memmap_size]
        test    rax, rax
        jz      fail_bad_memmap
        mov     eax, dword [r15 + K64BOOTINFO.memmap_desc_size]
        cmp     rax, K64_MEMORY_DESCRIPTOR_SIZE
        jb      fail_bad_memmap
        mov     rax, qword [r15 + K64BOOTINFO.memmap_desc_count]
        test    rax, rax
        jz      fail_bad_memmap

        ; the stack must be a part of the mapped kernel image
        mov     rax, qword [r15 + K64BOOTINFO.kernel_phys_start]
        mov     rbx, qword [r15 + K64BOOTINFO.kernel_phys_end]
        cmp     rbx, rax
        jbe     fail_bad_ranges
        mov     rcx, qword [r15 + K64BOOTINFO.stack_phys_start]
        mov     rdx, qword [r15 + K64BOOTINFO.stack_phys_end]
        cmp     rdx, rcx
        jbe     fail_bad_ranges
        cmp     rcx, rax
        jb      fail_bad_ranges
        cmp     rdx, rbx
        ja      fail_bad_ranges

        ; move to the kernel stack, the loader stack is not ours anymore
        mov     rax, rdx
        sub     rax, qword [r15 + K64BOOTINFO.kernel_phys_start]
        add     rax, VIRT_KERNEL_BASE
        and     rax, not 15
        mov     rsp, rax
        xor     rbp, rbp

        ; unique milestone, then the details needed to check the handover
        mov     rsi, kmsg_boot_ok
        call    com1_puts

        mov     rsi, kmsg_bootinfo
        call    com1_puts
        mov     rax, r15
        call    com1_puthex
        mov     rsi, kmsg_newline
        call    com1_puts

        mov     rsi, kmsg_memmap
        call    com1_puts
        mov     rax, qword [r15 + K64BOOTINFO.memmap_addr]
        call    com1_puthex
        mov     rsi, kmsg_memmap_size
        call    com1_puts
        mov     rax, qword [r15 + K64BOOTINFO.memmap_size]
        call    com1_puthex
        mov     rsi, kmsg_memmap_count
        call    com1_puts
        mov     rax, qword [r15 + K64BOOTINFO.memmap_desc_count]
        call    com1_puthex
        mov     rsi, kmsg_newline
        call    com1_puts

        mov     rsi, kmsg_kernel
        call    com1_puts
        mov     rax, qword [r15 + K64BOOTINFO.kernel_phys_start]
        call    com1_puthex
        mov     rsi, kmsg_comma
        call    com1_puts
        mov     rax, qword [r15 + K64BOOTINFO.kernel_phys_end]
        call    com1_puthex
        mov     rsi, kmsg_kernel_virt
        call    com1_puts
        mov     rax, VIRT_KERNEL_BASE
        call    com1_puthex
        mov     rsi, kmsg_newline
        call    com1_puts

        mov     rsi, kmsg_stack
        call    com1_puts
        mov     rax, qword [r15 + K64BOOTINFO.stack_phys_start]
        call    com1_puthex
        mov     rsi, kmsg_comma
        call    com1_puts
        mov     rax, qword [r15 + K64BOOTINFO.stack_phys_end]
        call    com1_puthex
        mov     rsi, kmsg_stack_top
        call    com1_puts
        mov     rax, rsp
        call    com1_puthex
        mov     rsi, kmsg_newline
        call    com1_puts

        ; the first stage report is complete: hand over to the foundation
        ; (CPU descriptors, PMM, VMM, heap and the core self test). It
        ; reports its own milestones and halts, on success it never returns.
        mov     rcx, r15
        call    k64_foundation_init
        jmp     halt_forever

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; handover failures, reported before halting

fail_no_bootinfo:
        mov     rsi, kmsg_fail_bootinfo
        jmp     fail_report
fail_bad_version:
        mov     rsi, kmsg_fail_version
        jmp     fail_report
fail_bad_flags:
        mov     rsi, kmsg_fail_flags
        jmp     fail_report
fail_bad_memmap:
        mov     rsi, kmsg_fail_memmap
        jmp     fail_report
fail_bad_ranges:
        mov     rsi, kmsg_fail_ranges

fail_report:
        call    com1_puts
        jmp     halt_forever

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; messages, ASCII only, COM1 has no notion of CHAR16

kmsg_boot_ok          db 'K64-BOOT-OK', 13, 10, 0
kmsg_bootinfo         db 'bootinfo = ', 0
kmsg_memmap           db 'memmap   = ', 0
kmsg_memmap_size      db ' size = ', 0
kmsg_memmap_count     db ' descriptors = ', 0
kmsg_kernel           db 'kernel   = ', 0
kmsg_kernel_virt      db ' -> virt ', 0
kmsg_stack            db 'stack    = ', 0
kmsg_stack_top        db ' rsp = ', 0
kmsg_comma            db ', ', 0
kmsg_newline          db 13, 10, 0

kmsg_fail_bootinfo    db 'K64-BOOT-FAIL bootinfo'
                      db 13, 10, 0
kmsg_fail_version     db 'K64-BOOT-FAIL bootinfo version'
                      db 13, 10, 0
kmsg_fail_flags       db 'K64-BOOT-FAIL unsupported flags'
                      db 13, 10, 0
kmsg_fail_memmap      db 'K64-BOOT-FAIL memory map'
                      db 13, 10, 0
kmsg_fail_ranges      db 'K64-BOOT-FAIL kernel and stack ranges'
                      db 13, 10, 0

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; stage 2 modules and the core integration
;
; Each module emits all of its code first and its data region last, 4 KiB
; aligned at both ends and really present in the file. MM_HOSTED keeps
; mm.asm from emitting its standalone format/org pair. The core files
; close the image: nothing may follow core_data_end.

include 'cpu/cpu.asm'

MM_HOSTED = 1
include 'mm/mm.asm'

include 'irq/irq.asm'

; GUI-G1: the framebuffer module calls vmm_map_mmio of MM and nothing
; else outside its own files. VIDEO_HOSTED keeps video.asm from emitting
; its standalone format/org pair, exactly like MM_HOSTED above. Its data
; region sits mid image and is protected RW/NX by the foundation GUI
; stage; the core files still close the image.
VIDEO_HOSTED = 1
include 'video/video.asm'

include 'core/contracts.inc'
include 'core/foundation.inc'
include 'core/selftest.inc'

if $ <> core_data_end
        err 'nothing may follow the core data region'
end if
