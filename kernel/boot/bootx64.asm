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
        push    rax rdx
        mov     rax, [efi_table]
        mov     rax, [rax+EFI_SYSTEM_TABLE.ConOut]
        mov     rdx, rcx ; arg2 - color
        mov     rcx, rax ; arg1 - this
        fstcall [rax+EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL.SetAttribute]
        pop     rdx rax
        ret

; rcx - null-terminated string
efi_puts:
        push    rax rdx
        mov     rax, [efi_table]
        mov     rax, [rax+EFI_SYSTEM_TABLE.ConOut]
        mov     rdx, rcx ; arg2 - string
        mov     rcx, rax ; arg1 - this
        fstcall [rcx+EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL.OutputString]
        pop     rdx rax
        ret

; rcx - char
efi_putc:
        push    rax rdx
        mov     rax, [efi_table]
        mov     rax, [rax+EFI_SYSTEM_TABLE.ConOut]
        push    qword 0
        mov     byte [rsp], cl
        mov     rdx, rsp
        mov     rcx, rax
        fstcall [rcx+EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL.OutputString]
        add     rsp, 8
        pop     rdx rax
        ret

; rcx - number
efi_print_hex:
        push    rax rbx rcx rdx r8

        push    qword 0
        push    qword 0
        push    qword 0
        push    qword 0
        push    qword 0

        ; TODO maybe add '0x' to buffer

        mov     rdx, rcx
        mov     rcx, 64 ; how many nibbles in qword
        xor     r8, r8
@@:
        sub     rcx, 4

        mov     rbx, rdx
        shr     rbx, cl
        and     rbx, 0xf

        lea     rax, [hex_codes + rbx]
        movzx   rax, byte [rax]
        mov     byte [rsp + r8*2 ], al
        mov     byte [rsp + r8*2 + 1], 0 ; set high byte to 0 bc UEFI OutputString needs CHAR16
        inc     r8

        test    rcx, rcx
        jnz     @b

        mov     rcx, rsp
        call    efi_puts

        pop     r8 rdx rcx rbx rax
        add     rsp, 8*5
        ret


;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;

proc main _efi_handle, _efi_table
        mov     [efi_handle], rcx
        mov     [efi_table], rdx
        mov     rbx, rdx

        ; reset the console
        mov     rcx, [rbx + EFI_SYSTEM_TABLE.ConOut]
        fstcall [rcx + EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL.Reset], rcx, 1
        test    eax, eax
        jnz     $ ; loop if fail to init text

        ; disable the default watchdog timer, otherwise it will reboot the pc after 5 mins of this app work
        mov     rcx, [rbx + EFI_SYSTEM_TABLE.BootServices]
        fstcall [rcx + EFI_BOOT_SERVICES.SetWatchdogTimer], rcx, 0, 0, 0, 0
        test    eax, eax
        jz      @f
        mov     rcx, msg_failed_disable_watchdog
        call    efi_puts
        jmp     $
@@:

        mov     rcx, msg_firmware_vendor
        call    efi_puts
        mov     rcx, [rbx + EFI_SYSTEM_TABLE.FirmwareVendor]
        call    efi_puts
        mov     rcx, msg_newline
        call    efi_puts
        mov     rcx, msg_firmware_revision
        call    efi_puts
        mov     ecx, [rbx + EFI_SYSTEM_TABLE.FirmwareRevision]
        call    efi_print_hex
        mov     rcx, msg_newline
        call    efi_puts

        mov     rcx, EFI_BLUE or EFI_BACKGROUND_CYAN
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
        mov     rcx, ' '
        call    efi_putc

        mov     rcx, EFI_LIGHTGREEN
        call    efi_set_text_color
        mov     rcx, 0xCAFEBABEFEEDCAFE
        call    efi_print_hex


        jmp     $
endp


section '.data' data readable writeable
efi_handle  dq 0
efi_table   dq 0
hex_codes:
        db '0123456789ABCDEF', 0


section '.rodata' data readable
msg_hello_k64_loader du "Hello from Kolibri64 efi loader",13,10,0
msg_2 du "Lorem ipsum ! !",13,10,0
msg_firmware_vendor du "UEFI vendor: ", 0
msg_firmware_revision du "UEFI revision: ", 0
msg_newline du 13,10,0
msg_failed_disable_watchdog du "Failed to disable watchdog timer!", 13,10,0


section '.reloc' fixups data discardable
