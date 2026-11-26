;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2026. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; APP-v1 bring-up self test: the module state must be pristine before
;; the first launch and the syscall handler table must have the frozen
;; shape: numbers 0..3 empty (the app handlers live only while a
;; program runs), numbers 4..9 owned by the resident fsys adapters.
;; Runs with IF = 0 after app_init and the data protection, before
;; app_launch; a failure takes the standard fatal foundation path.
;;

app_selftest:
        app_enter
        cmp     qword [app_ready], 1
        jne     .eio
        cmp     qword [app_stage], APP_STAGE_IDLE
        jne     .eio
        cmp     qword [app_pages_held], 0
        jne     .eio
        cmp     qword [app_table_delta], 0
        jne     .eio
        cmp     qword [app_blit_count], 0
        jne     .eio
        cmp     qword [app_exit_code], 0
        jne     .eio
        cmp     qword [app_fault_vector], 0
        jne     .eio
        ; handler table shape: 0..3 empty, 4..9 resident
        xor     r12d, r12d
.empty_loop:
        cmp     qword [cpu_syscall_handlers + r12*8], 0
        jne     .eio
        inc     r12d
        cmp     r12d, 4
        jb      .empty_loop
.resident_loop:
        cmp     qword [cpu_syscall_handlers + r12*8], 0
        je      .eio
        inc     r12d
        cmp     r12d, 10
        jb      .resident_loop
        xor     eax, eax
        jmp     .out
.eio:
        mov     eax, APP_EIO
.out:
        movsxd  rax, eax
        app_leave
        ret