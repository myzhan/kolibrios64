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

; hmm proc64.inc uses TCHAR. to define string literals (?)
struc TCHAR [val] {
        common match any, val \{ . du val \}
        match , val \{ . du ? \}
}
sizeof.TCHAR = 2

; rcx - color = fore | back
proc efi_set_text_color
        mov     rax, [efi_table]
        mov     rax, [rax+EFI_SYSTEM_TABLE.ConOut]
        mov     rdx, rcx ; arg2 - color
        mov     rcx, rax ; arg1 - this
        fstcall [rax+EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL.SetAttribute]
        ret
endp

; rcx - null-terminated string
proc efi_puts
        mov     rax, [efi_table]
        mov     rax, [rax+EFI_SYSTEM_TABLE.ConOut]
        mov     rdx, rcx ; arg2 - string
        mov     rcx, rax ; arg1 - this
        fstcall [rcx+EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL.OutputString]
        ret
endp

; rcx - char
proc efi_putc
locals
        cstr dq  0
endl
        mov     rax, [efi_table]
        mov     rax, [rax+EFI_SYSTEM_TABLE.ConOut]
        mov     byte [cstr], cl
        lea     rdx, [cstr]
        mov     rcx, rax
        fstcall [rcx+EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL.OutputString]
        ret
endp


;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;

proc main _efi_handle, _efi_table
        mov     [efi_handle], rcx
        mov     [efi_table], rdx
        mov     rbx, rdx

        mov     rcx, [rbx+EFI_SYSTEM_TABLE.ConOut]
        fstcall [rcx+EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL.Reset], rcx, 1
        test    eax, eax
        jnz     $ ; loop if fail to init text

        fstcall efi_set_text_color, EFI_BLUE or EFI_BACKGROUND_GREEN
        fstcall efi_puts, msg_hello_k64_loader
        fstcall efi_puts, <'hi there', 13, 10> ;; didnt work unless declared TCHAR

        fstcall efi_set_text_color, EFI_LIGHTRED
        fstcall efi_puts, msg_2

        fstcall efi_set_text_color, EFI_CYAN
        fstcall efi_putc, 0+'a' ;; 0+ is to cast it to number to put in register it itself, not the address of string literal 'a' but function expects char in rcx

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
