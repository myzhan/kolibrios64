;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;

use64

VIRT_KERNEL_BASE = 0xFFFFFFFF80000000

org VIRT_KERNEL_BASE

dq 'KERNEL64' ; magic
dq k64_entry - VIRT_KERNEL_BASE
; maybe more fields in header

; 64 bit kernel entry point
k64_entry:
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

rb 8192
kernel_tmp_stack:

