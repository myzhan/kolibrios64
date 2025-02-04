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

MEMORY_MAP_SIZE = 0x10000


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

; print hex without leading zeros
; rcx - number
efi_print_hex_no_lz:
        push    rax rbx rcx rdx r8

        push    qword 0
        push    qword 0
        push    qword 0
        push    qword 0
        push    qword 0

        xor     r10, r10 ; leading zeros end flag
        mov     rdx, rcx
        mov     rcx, 64 ; how many nibbles in qword
        xor     r8, r8
        mov     byte [rsp + r8*2 ], '0'
        mov     byte [rsp + r8*2 + 1], 0 
        inc     r8
        mov     byte [rsp + r8*2 ], 'x'
        mov     byte [rsp + r8*2 + 1], 0
        inc     r8
.lp:
        sub     rcx, 4

        mov     rbx, rdx
        shr     rbx, cl
        and     rbx, 0xf

        test    r10, r10
        jnz     @f

        test    rbx, rbx
        jz      .lp_cont
        mov     r10, 1
@@:
        lea     rax, [hex_codes + rbx]
        movzx   rax, byte [rax]
        mov     byte [rsp + r8*2 ], al
        mov     byte [rsp + r8*2 + 1], 0 ; set high byte to 0 bc UEFI OutputString needs CHAR16
        inc     r8

.lp_cont:
        test    rcx, rcx
        jnz     .lp

        cmp     r8, 2
        ja      @f
        mov     byte [rsp + r8*2 ], '0'
        mov     byte [rsp + r8*2 + 1], 0 
@@:
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
        mov     rax, [rbx + EFI_SYSTEM_TABLE.ConOut]
        fstcall [rax + EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL.Reset], rax, 1
        test    eax, eax
        jnz     $ ; loop if fail to init text

        ; disable the default watchdog timer, otherwise it will reboot the pc after 5 mins of this app work
        mov     rax, [rbx + EFI_SYSTEM_TABLE.BootServices]
        fstcall [rax + EFI_BOOT_SERVICES.SetWatchdogTimer], 0, 0, 0, 0
        test    eax, eax
        jz      @f
        mov     rcx, msg_failed_disable_watchdog
        call    efi_puts
        jmp     $
@@:

        mov     rcx, EFI_LIGHTGREEN
        call    efi_set_text_color
        mov     rcx, msg_hello_k64_loader
        call    efi_puts
        mov     rcx, EFI_LIGHTGRAY
        call    efi_set_text_color

        mov     rcx, msg_firmware_vendor
        call    efi_puts
        mov     rcx, [rbx + EFI_SYSTEM_TABLE.FirmwareVendor]
        call    efi_puts
        mov     rcx, msg_newline
        call    efi_puts
        mov     rcx, msg_firmware_revision
        call    efi_puts
        mov     ecx, [rbx + EFI_SYSTEM_TABLE.FirmwareRevision]
        call    efi_print_hex_no_lz
        mov     rcx, msg_newline
        call    efi_puts

        ; Obtain and print uefi memory map
        mov     rbx, [efi_table] ;;
        mov     r10, [rbx + EFI_SYSTEM_TABLE.BootServices]
        fstcall [r10 + EFI_BOOT_SERVICES.AllocatePages], EFI_ALLOCATE_ANY_PAGES, \
                EFI_RESERVED_MEMORY_TYPE, MEMORY_MAP_SIZE/0x1000, memory_map
        ; mov     rcx, rax
        ; call    efi_print_hex_no_lz
        ; mov     rcx, msg_newline
        ; call    efi_puts
        ;; call    halt_on_error

        mov     rbx, [efi_table] ;;
        mov     r10, [rbx + EFI_SYSTEM_TABLE.BootServices]
        fstcall [r10 + EFI_BOOT_SERVICES.GetMemoryMap], memory_map_size, \
                [memory_map], memory_map_key, descriptor_size, descriptor_ver
        ;; call    halt_on_error

        mov     rcx, [memory_map]
        add     rcx, [descriptor_size]
        add     rcx, [descriptor_size]
        add     rcx, [descriptor_size]
        add     rcx, [descriptor_size]
        add     rcx, [descriptor_size]
        add     rcx, [descriptor_size]
        add     rcx, [descriptor_size]
        mov     rcx, [rcx]
        call    efi_print_hex_no_lz
        mov     rcx, msg_newline
        call    efi_puts


        mov     rcx, msg_end_1
        call    efi_puts

        mov     rcx, 0x000A000B00C
        call    efi_print_hex_no_lz
        mov     rcx, msg_newline
        call    efi_puts


        mov     rcx, 0xABCDEF133777
        call    efi_print_hex_no_lz
        mov     rcx, msg_newline
        call    efi_puts

        xor     rcx, rcx
        call    efi_print_hex_no_lz
        mov     rcx, msg_newline
        call    efi_puts


        jmp     $
endp

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
section '.data' data readable writeable
efi_handle  dq 0
efi_table   dq 0
hex_codes:
        db '0123456789ABCDEF', 0

memory_map_key  dq 0
descriptor_size dq 0
descriptor_ver  dq 0
memory_map_size dq MEMORY_MAP_SIZE

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
section '.rodata' data readable
msg_hello_k64_loader du "Kolibri64 EFI bootloader",13,10,0
msg_firmware_vendor du "UEFI vendor: ", 0
msg_firmware_revision du "UEFI revision: ", 0
msg_newline du 13,10,0
msg_failed_disable_watchdog du "Failed to disable watchdog timer!", 13,10,0
msg_end_1 du "----------", 13,10,0

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
section '.bss' data readable writeable discardable
memory_map      dq ?


section '.reloc' fixups data discardable
