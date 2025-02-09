;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;

use64

VIRT_KERNEL_BASE = 0xFFFFFFFF80000000
DEFAULT_STACK_SIZE = 8192

org VIRT_KERNEL_BASE

dq 'KERNEL64' ; magic
dq k64_entry - VIRT_KERNEL_BASE ; entry point's offset from base where it loaded
dq DEFAULT_STACK_SIZE ; how many bytes of stack we need
kernel_phys_start dq 0 ; bootloader will put here phys addr where it loaded kernel
kernel_phys_end dq 0 ; and phys end addr (including stack ofc)

; 64 bit kernel entry point
k64_entry:
        mov     rsp, kernel_default_stack
        pop     qword [qword puthex_ptr - VIRT_KERNEL_BASE]
        pop     qword [qword putstr_ptr - VIRT_KERNEL_BASE]
        
        sub     rsp, 0x20
        mov     rcx, kmsg_hello - VIRT_KERNEL_BASE
        call    qword [qword putstr_ptr - VIRT_KERNEL_BASE]
        add     rsp, 0x20

        sub     rsp, 0x20
        mov     rcx, kmsg_ripis - VIRT_KERNEL_BASE
        call    qword [qword putstr_ptr - VIRT_KERNEL_BASE]
        add     rsp, 0x20

        sub     rsp, 0x20
        lea     rcx, [rip]
        call    qword [qword puthex_ptr - VIRT_KERNEL_BASE]
        add     rsp, 0x20

        jmp     $


kmsg_hello      db 'Hello from KERNEL',13,10,0
kmsg_ripis      db 'RIP = ',13,10,0

puthex_ptr      dq 0
putstr_ptr      dq 0

rb DEFAULT_STACK_SIZE
align 16
kernel_default_stack:

