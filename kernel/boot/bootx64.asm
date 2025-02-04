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
proc efi_set_text_color uses rax rdx
        mov     rax, [efi_table]
        mov     rax, [rax+EFI_SYSTEM_TABLE.ConOut]
        mov     rdx, rcx ; arg2 - color
        mov     rcx, rax ; arg1 - this
        fstcall [rax+EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL.SetAttribute]
        ret
endp

; rcx - null-terminated string
proc efi_puts uses rax rdx
        mov     rax, [efi_table]
        mov     rax, [rax+EFI_SYSTEM_TABLE.ConOut]
        mov     rdx, rcx ; arg2 - string
        mov     rcx, rax ; arg1 - this
        fstcall [rcx+EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL.OutputString]
        ret
endp

; rcx - char
proc efi_putc uses rax rdx
locals
        chr     dq 0
endl
        mov     rax, [efi_table]
        mov     rax, [rax+EFI_SYSTEM_TABLE.ConOut]
        mov     byte [chr], cl
        lea     rdx, [chr] ; arg2 - string
        mov     rcx, rax   ; arg1 - this
        fstcall [rcx+EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL.OutputString]
        ret
endp

; print hex without leading zeros
; rcx - number
proc efi_print_hex_no_lz uses rax rbx rcx rdx r8 r10 ; TODO also need function that displays with leading zeros
locals
        buf     dq 5 dup(0)
endl
        xor     r10, r10 ; leading zeros end flag
        mov     rdx, rcx
        mov     rcx, 64 ; how many nibbles in qword
        xor     r8, r8
        mov     byte [buf + r8*2 ], '0'
        mov     byte [buf + r8*2 + 1], 0 
        inc     r8
        mov     byte [buf + r8*2 ], 'x'
        mov     byte [buf + r8*2 + 1], 0
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
        mov     byte [buf + r8*2 ], al
        mov     byte [buf + r8*2 + 1], 0 ; set high byte to 0 bc UEFI OutputString needs CHAR16
        inc     r8

.lp_cont:
        test    rcx, rcx
        jnz     .lp

        cmp     r8, 2
        ja      @f
        mov     byte [buf + r8*2 ], '0'
        mov     byte [buf + r8*2 + 1], 0 
@@:
        lea     rcx, [buf]
        fstcall efi_puts, rcx

        ret
endp

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
        fstcall efi_puts, msg_failed_disable_watchdog
        jmp     $
@@:

        fstcall efi_set_text_color, EFI_LIGHTGREEN
        fstcall efi_puts, msg_hello_k64_loader
        fstcall efi_set_text_color, EFI_LIGHTGRAY

        fstcall efi_puts, msg_firmware_vendor
        fstcall efi_puts,  [rbx + EFI_SYSTEM_TABLE.FirmwareVendor]
        fstcall efi_puts, msg_newline

        fstcall efi_puts, msg_firmware_revision
        fstcall efi_print_hex_no_lz, [rbx + EFI_SYSTEM_TABLE.FirmwareRevision]
        fstcall efi_puts, msg_newline

        ; Obtain and print uefi memory map
        mov     rbx, [efi_table] ;;
        mov     r10, [rbx + EFI_SYSTEM_TABLE.BootServices]
        fstcall [r10 + EFI_BOOT_SERVICES.AllocatePages], EFI_ALLOCATE_ANY_PAGES, \
                EFI_RESERVED_MEMORY_TYPE, MEMORY_MAP_SIZE/0x1000, memory_map
        ;; call    halt_on_error ; TODO

        mov     rbx, [efi_table] ;;
        mov     r10, [rbx + EFI_SYSTEM_TABLE.BootServices]
        fstcall [r10 + EFI_BOOT_SERVICES.GetMemoryMap], memory_map_size, \
                [memory_map], memory_map_key, descriptor_size, descriptor_ver
        ;; call    halt_on_error ; TODO

        ; TODO iretae through memory map and print it in e820 form
        mov     rcx, [memory_map]
        add     rcx, [descriptor_size]
        add     rcx, [descriptor_size]
        add     rcx, [descriptor_size]
        add     rcx, [descriptor_size]
        add     rcx, [descriptor_size]
        add     rcx, [descriptor_size]
        add     rcx, [descriptor_size]
        fstcall efi_print_hex_no_lz, qword [rcx]
        fstcall efi_puts, msg_newline

        fstcall efi_puts, msg_end_1

        fstcall    efi_print_hex_no_lz, 0x000A000B00C
        fstcall    efi_puts, msg_newline

        fstcall    efi_print_hex_no_lz, 0xABCDEF133777
        fstcall    efi_puts, msg_newline

        fstcall    efi_print_hex_no_lz, 0xCAFE
        fstcall    efi_puts, msg_newline

        fstcall    efi_putc, <0+'a'>

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
