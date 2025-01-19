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

proc main _efi_handle, _efi_table
        mov     [efi_handle], rcx
        mov     [efi_table], rdx
        mov     rbx, rdx

        mov     rcx, [rbx+EFI_SYSTEM_TABLE.ConOut]
        fstcall [rcx+EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL.Reset], rcx, 1
        test    eax, eax
        jnz     $       ; what can I do here?
        mov     rcx, [rbx+EFI_SYSTEM_TABLE.ConOut]
        fstcall [rcx+EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL.OutputString], rcx, msg_hello_k64_loader

        jmp     $
endp


section '.data' data readable writeable
efi_handle  dq 0
efi_table   dq 0


section '.rodata' data readable
msg_hello_k64_loader du "Hello from Kolibri64 efi loader",13,10,0


section '.reloc' fixups data discardable
