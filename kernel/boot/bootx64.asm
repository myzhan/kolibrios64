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

; rbx - efi table
; rcx - color = fore | back
proc set_text_color; uses rax
        mov     rax, rcx
        mov     rcx, [rbx+EFI_SYSTEM_TABLE.ConOut]
        fstcall [rcx+EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL.SetAttribute], rcx, rax
        ret
endp

; rbx - efi table
; rcx - string
proc print_string; uses rax
        mov     rax, rcx
        mov     rcx, [rbx+EFI_SYSTEM_TABLE.ConOut]
        fstcall [rcx+EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL.OutputString], rcx, rax
        ret
endp

proc main _efi_handle, _efi_table
        mov     [efi_handle], rcx
        mov     [efi_table], rdx
        mov     rbx, rdx

        mov     rcx, [rbx+EFI_SYSTEM_TABLE.ConOut]
        fstcall [rcx+EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL.Reset], rcx, 1
        test    eax, eax
        jnz     $       ; what can I do here?

        fstcall set_text_color, EFI_BLUE or EFI_BACKGROUND_GREEN
        fstcall print_string, msg_hello_k64_loader

        fstcall set_text_color, EFI_LIGHTRED
        fstcall print_string, msg_2

        ;; TODO: print some dec, hex. => impl simple printf

        jmp     $
endp


section '.data' data readable writeable
efi_handle  dq 0
efi_table   dq 0


section '.rodata' data readable
msg_hello_k64_loader du "Hello from Kolibri64 efi loader",13,10,0
msg_2 du "Lorem ipsum ! !",13,10,0


section '.reloc' fixups data discardable
