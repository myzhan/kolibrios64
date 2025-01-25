;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License.   ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
format pe64 efi
entry main

section '.text' code executable readable

include '../struct.inc'
; include '../macros.inc'
; include '../kglobals.inc'
fastcall fix fstcall
include '../proc64.inc'
include '../const.inc'

purge DQ ; because of some struct DQ in const.inc
include 'uefi64.inc'

; rcx - color = fore | back
efi_set_text_color:
        mov     rax, [efi_table]
        mov     rax, [rax+EFI_SYSTEM_TABLE.ConOut]
        mov     rdx, rcx ; arg2 - color
        mov     rcx, rax ; arg1 - this
        fstcall [rax+EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL.SetAttribute]
        ret

; rcx - null-terminated string
efi_puts:
        mov     rax, [efi_table]
        mov     rax, [rax+EFI_SYSTEM_TABLE.ConOut]
        mov     rdx, rcx ; arg2 - string
        mov     rcx, rax ; arg1 - this
        fstcall [rcx+EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL.OutputString]
        ret

; rcx - char
efi_putc:
        mov     rax, [efi_table]
        mov     rax, [rax+EFI_SYSTEM_TABLE.ConOut]
        push    qword 0
        mov     byte [rsp], cl
        mov     rdx, rsp
        mov     rcx, rax
        fstcall [rcx+EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL.OutputString]
        add     rsp, 8
        ret


;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;

proc main _efi_handle, _efi_table
        mov     [efi_handle], rcx
        mov     [efi_table], rdx
        mov     rbx, rdx

        mov     rcx, [rbx+EFI_SYSTEM_TABLE.ConOut]
        fstcall [rcx+EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL.Reset], rcx, 1
        test    eax, eax
        jnz     $ ; loop if fail to init text

        mov     rcx, EFI_BLUE or EFI_BACKGROUND_GREEN
        call    efi_set_text_color
        mov     rcx, msg_hello_k64_loader
        call    efi_puts

        mov     rcx, EFI_LIGHTRED
        call    efi_set_text_color
        mov     rcx, msg_2
        call    efi_puts

        mov     rcx, EFI_CYAN
        call    efi_set_text_color
        mov     rcx, 'a'
        call    efi_putc

        ;; TODO: print some dec, hex. => impl simple printf. fdo.inc

        jmp     $
endp


section '.data' data readable writeable
efi_handle  dq 0
efi_table   dq 0


section '.rodata' data readable
msg_hello_k64_loader du "Hello from Kolibri64 efi loader",13,10,0
msg_2 du "Lorem ipsum ! !",13,10,0


section '.reloc' fixups data discardable
