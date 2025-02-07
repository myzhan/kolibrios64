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

; linux/arch/x86/include/uapi/asm/e820.h
E820_RAM       = 1
E820_RESERVED  = 2
E820_ACPI      = 3
E820_NVS       = 4
E820_UNUSABLE  = 5
E820_PMEM      = 7

include 'uefi_prints.inc'

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

        fstcall efi_puts, msg_e820_memmap_here

        ; Obtain and print uefi memory map
        fstcall efi_set_text_color, EFI_CYAN
        mov     rbx, [efi_table] ;;
        mov     r10, [rbx + EFI_SYSTEM_TABLE.BootServices]
        fstcall [r10 + EFI_BOOT_SERVICES.AllocatePages], EFI_ALLOCATE_ANY_PAGES, \
                EFI_RESERVED_MEMORY_TYPE, MEMORY_MAP_SIZE/0x1000, memory_map
        ;; call    halt_on_error ; TODO

        ;mov     rbx, [efi_table] ;;
        mov     r10, [rbx + EFI_SYSTEM_TABLE.BootServices]
        fstcall [r10 + EFI_BOOT_SERVICES.GetMemoryMap], memory_map_size, \
                [memory_map], memory_map_key, descriptor_size, descriptor_ver
        ;; call    halt_on_error ; TODO

        mov     rsi, [memory_map]
        mov     r15, rsi
        add     r15, [memory_map_size]
        xor     r14, r14 ; memmap entry idx
.next_descr:
        fstcall efi_print_hex_no_lz, r14
        fstcall efi_puts, msg_spacer

        mov     rax, [rsi + EFI_MEMORY_DESCRIPTOR.PhysicalStart]
        mov     r12, rax
        fstcall efi_print_hex_fixed, rax
        fstcall efi_puts, msg_spacer

        mov     rax, [rsi + EFI_MEMORY_DESCRIPTOR.NumberOfPages]
        shl     rax, 12
        add     r12, rax
        fstcall efi_print_hex_fixed, r12
        fstcall efi_puts, msg_spacer

        mov     ecx, [rsi+EFI_MEMORY_DESCRIPTOR.Type]
        cmp     ecx, EFI_LOADER_CODE
        jz      .mem_ram_if_wb
        cmp     ecx, EFI_LOADER_DATA
        jz      .mem_ram_if_wb
        cmp     ecx, EFI_BOOT_SERVICES_CODE
        jz      .mem_ram_if_wb
        cmp     ecx, EFI_BOOT_SERVICES_DATA
        jz      .mem_ram_if_wb
        cmp     ecx, EFI_CONVENTIONAL_MEMORY
        jz      .mem_ram_if_wb
        cmp     ecx, EFI_ACPI_RECLAIM_MEMORY
        mov     eax, E820_ACPI
        jz      .type_done
        cmp     ecx, EFI_ACPI_MEMORY_NVS
        mov     eax, E820_NVS
        jz      .type_done
        cmp     ecx, EFI_UNUSABLE_MEMORY
        mov     eax, E820_UNUSABLE
        jz      .type_done
        cmp     ecx, EFI_PERSISTENT_MEMORY
        mov     eax, E820_PMEM
        jz      .type_done
        jmp     .reserved
.mem_ram_if_wb:
        test    [rsi+EFI_MEMORY_DESCRIPTOR.Attribute], dword EFI_MEMORY_WB
        mov     eax, E820_RAM
        jnz     .type_done
.reserved:
        mov     eax, E820_RESERVED
.type_done:
        lea     rax, [e820_typenames + rax*8]
        fstcall efi_puts, qword [rax]
        fstcall efi_puts, msg_newline

.done:
        add     rsi, [descriptor_size]
        inc     r14
        cmp     rsi, r15
        jb      .next_descr


        ; fstcall efi_set_text_color, EFI_LIGHTGRAY
        ; fstcall efi_puts, msg_end_1

        ; fstcall efi_print_hex_no_lz, 0x000A000B00C
        ; fstcall efi_puts, msg_newline

        ; fstcall efi_print_hex_no_lz, 0xABCDEF133777
        ; fstcall efi_puts, msg_newline

        ; fstcall efi_print_hex_no_lz, 0xCAFE
        ; fstcall efi_puts, msg_newline

        ; fstcall efi_print_hex_fixed, 0x00764A0B
        ; fstcall efi_puts, msg_newline

        ; fstcall efi_print_hex_fixed, 0
        ; fstcall efi_puts, msg_newline

        ; fstcall efi_putc, <0+'a'>

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
msg_end_1 du 13,10,"---------- test printing functions:", 13,10,0
msg_spacer du "  ", 0
msg_dummy du "<....>", 0

msg_e820_memmap_here du "Memory map (phys_start, phys_end, e820 type)",13,10,0
msg_e820_available du "Available RAM", 0
mag_e820_reserved du "Reserved RAM", 0
mag_e820_acpi_reclaim du "ACPI reclaimable RAM", 0
mag_e820_acpi_nvs du "ACPI NVS RAM", 0
msg_e820_badmem du "Bad RAM", 0
msg_e820_persistent du "Persistent RAM", 0

e820_typenames:
        dq msg_dummy
        dq msg_e820_available
        dq mag_e820_reserved
        dq mag_e820_acpi_reclaim
        dq mag_e820_acpi_nvs
        dq msg_e820_badmem
        dq msg_dummy
        dq msg_e820_persistent

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
section '.bss' data readable writeable discardable
memory_map      dq ?


section '.reloc' fixups data discardable
