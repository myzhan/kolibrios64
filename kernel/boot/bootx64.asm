;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
format pe64 efi
entry main

section '.text' code executable readable

include '../struct.inc'
; include '../macros.inc'
; include '../kglobals.inc'
fastcall fix fstcall
TCHAR fix du ; bc uefi uses CHAR16
sizeof.TCHAR = 2
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


proc load_file _root, _name, _buffer, _size, _fatal
        mov     [_root], rcx
        mov     [_name], rdx
        mov     [_buffer], r8
        mov     [_size], r9
        mov     r10, [_root]
        mov     r11, [_name]
        fstcall [r10 + EFI_FILE_PROTOCOL.Open], r10, file_handle, r11, EFI_FILE_MODE_READ, 0
        test    eax, eax
        jz      @f
        mov     [_size], 0
        cmp     [_fatal], 1
        jnz     .done
        fstcall efi_puts, msg_error_open_file
        fstcall efi_puts, [_name]
        jmp     $
@@:
        lea     rdx, [_size]
        mov     r8, [_buffer]
        mov     r10, [file_handle]
        fstcall [r10 + EFI_FILE_PROTOCOL.Read], [file_handle], rdx, r8
        mov     r10, [file_handle]
        fstcall [r10 + EFI_FILE_PROTOCOL.Close], [file_handle]
.done:
        fstcall efi_puts, msg_bytes_read
        fstcall efi_print_hex_no_lz, [_size]
        fstcall efi_puts, msg_newline
        mov     rax, [_size]
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
        fstcall efi_puts, msg_error_disable_watchdog
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

;;;;;;;;;;;;; Obtain and print uefi memory map
;         fstcall efi_puts, msg_e820_memmap_here
;         fstcall efi_set_text_color, EFI_CYAN
;         mov     rbx, [efi_table] ;;
;         mov     r10, [rbx + EFI_SYSTEM_TABLE.BootServices]
;         fstcall [r10 + EFI_BOOT_SERVICES.AllocatePages], EFI_ALLOCATE_ANY_PAGES, \
;                 EFI_RESERVED_MEMORY_TYPE, MEMORY_MAP_SIZE/0x1000, memory_map
;         ;; call    halt_on_error ; TODO

;         ;mov     rbx, [efi_table] ;;
;         mov     r10, [rbx + EFI_SYSTEM_TABLE.BootServices]
;         fstcall [r10 + EFI_BOOT_SERVICES.GetMemoryMap], memory_map_size, \
;                 [memory_map], memory_map_key, descriptor_size, descriptor_ver
;         ;; call    halt_on_error ; TODO

;         mov     rsi, [memory_map]
;         mov     r15, rsi
;         add     r15, [memory_map_size]
;         xor     r14, r14 ; memmap entry idx
; .next_descr:
;         fstcall efi_print_hex_no_lz, r14
;         fstcall efi_puts, msg_spacer

;         mov     rax, [rsi + EFI_MEMORY_DESCRIPTOR.PhysicalStart]
;         mov     r12, rax
;         fstcall efi_print_hex_fixed, rax
;         fstcall efi_puts, msg_spacer

;         mov     rax, [rsi + EFI_MEMORY_DESCRIPTOR.NumberOfPages]
;         shl     rax, 12
;         add     r12, rax
;         fstcall efi_print_hex_fixed, r12
;         fstcall efi_puts, msg_spacer

;         mov     ecx, [rsi+EFI_MEMORY_DESCRIPTOR.Type]
;         cmp     ecx, EFI_LOADER_CODE
;         jz      .mem_ram_if_wb
;         cmp     ecx, EFI_LOADER_DATA
;         jz      .mem_ram_if_wb
;         cmp     ecx, EFI_BOOT_SERVICES_CODE
;         jz      .mem_ram_if_wb
;         cmp     ecx, EFI_BOOT_SERVICES_DATA
;         jz      .mem_ram_if_wb
;         cmp     ecx, EFI_CONVENTIONAL_MEMORY
;         jz      .mem_ram_if_wb
;         cmp     ecx, EFI_ACPI_RECLAIM_MEMORY
;         mov     eax, E820_ACPI
;         jz      .type_done
;         cmp     ecx, EFI_ACPI_MEMORY_NVS
;         mov     eax, E820_NVS
;         jz      .type_done
;         cmp     ecx, EFI_UNUSABLE_MEMORY
;         mov     eax, E820_UNUSABLE
;         jz      .type_done
;         cmp     ecx, EFI_PERSISTENT_MEMORY
;         mov     eax, E820_PMEM
;         jz      .type_done
;         jmp     .reserved
; .mem_ram_if_wb:
;         test    [rsi+EFI_MEMORY_DESCRIPTOR.Attribute], dword EFI_MEMORY_WB
;         mov     eax, E820_RAM
;         jnz     .type_done
; .reserved:
;         mov     eax, E820_RESERVED
; .type_done:
;         lea     rax, [e820_typenames + rax*8]
;         fstcall efi_puts, qword [rax]
;         fstcall efi_puts, msg_newline

; .done:
;         add     rsi, [descriptor_size]
;         inc     r14
;         cmp     rsi, r15
;         jb      .next_descr

        fstcall efi_set_text_color, EFI_LIGHTGRAY


;;;;;;;;;;;;;; loading kernel to memory
        mov     r10, [rbx + EFI_SYSTEM_TABLE.BootServices]
        fstcall [r10 + EFI_BOOT_SERVICES.HandleProtocol], [efi_handle], lip_guid, lip_interface
        test    eax, eax
        jz      @f
        fstcall efi_set_text_color, EFI_LIGHTRED
        fstcall efi_puts, msg_error_efi_lip_handle
        jmp     $
@@:
        mov     r11, [lip_interface]
        mov     r10, [rbx + EFI_SYSTEM_TABLE.BootServices]
        fstcall [r10 + EFI_BOOT_SERVICES.HandleProtocol], [r11 + EFI_LOADED_IMAGE_PROTOCOL.DeviceHandle], sfsp_guid, sfsp_interface
        test    eax, eax
        jz      @f
        fstcall efi_set_text_color, EFI_LIGHTRED
        fstcall efi_puts, msg_error_lip_dev_sfsp
        jmp     $
@@:
        mov     r10, [sfsp_interface]
        fstcall [r10 + EFI_SIMPLE_FILE_SYSTEM_PROTOCOL.OpenVolume], [sfsp_interface], volume_root
        test    eax, eax
        jz      @f
        fstcall efi_set_text_color, EFI_LIGHTRED
        fstcall efi_puts, msg_error_sfsp_openvolume
        jmp     $
@@:
        fstcall efi_puts, msg_newline
        ; fstcall efi_print_hex_fixed, [volume_root]
        fstcall efi_puts, msg_reading_kernel_header
        fstcall load_file, [volume_root], kernel_file_path, kernel_header_buf, 8*5, 0 ; TODO maybe kernel header make as struct => dont hardcode size use  sizeof

        fstcall efi_puts, msg_newline
        fstcall efi_puts, msg_thisis_kernel_header
        xor     r14, r14
@@:
        fstcall efi_putc, [r14 + kernel_header_buf]
        inc     r14
        cmp     r14, 8
        jl      @b
@@:
        fstcall efi_puts, msg_newline

        fstcall efi_print_hex_no_lz, [kernel_header_buf + 8]
        fstcall efi_puts, msg_newline

        fstcall efi_print_hex_no_lz, [kernel_header_buf + 16]
        fstcall efi_puts, msg_newline

        ;; TODO
        ;; read allocpages for kernel code  and stack  a contignuous range of pages 
        ;; put phys start / end to kernel header
        ;; set kernel rsp, jmp to k64_entry (offset is in kernel header)

        fstcall efi_set_text_color, EFI_LIGHTGRAY
        fstcall efi_puts, <"-----------------",0>

        ; fstcall efi_set_text_color, EFI_LIGHTGRAY
        ; fstcall efi_puts, <"dfdsfds",13,10,"fdfdf0983827",0>

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

lip_interface   dq 0
sfsp_interface  dq 0

hex_codes:
        db '0123456789ABCDEF', 0

memory_map_key  dq 0
descriptor_size dq 0
descriptor_ver  dq 0
memory_map_size dq MEMORY_MAP_SIZE

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
section '.rodata' data readable

gop_guid        db EFI_GRAPHICS_OUTPUT_PROTOCOL_GUID
lip_guid        db EFI_LOADED_IMAGE_PROTOCOL_GUID
sfsp_guid       db EFI_SIMPLE_FILE_SYSTEM_PROTOCOL_GUID
pcirbiop_guid   db EFI_PCI_ROOT_BRIDGE_IO_PROTOCOL_GUID

kernel_file_path du '\EFI\KERNEL64.BIN', 0

msg_hello_k64_loader du "Kolibri64 EFI bootloader",13,10,0
msg_firmware_vendor du "UEFI vendor: ", 0
msg_firmware_revision du "UEFI revision: ", 0
msg_reading_kernel_header du "Reading kernel header..",13,10,0
msg_thisis_kernel_header du "Kernel header:",13,10,0
msg_bytes_read du "Bytes read: ", 0

msg_error_disable_watchdog du "Failed to disable watchdog timer!", 13,10,0
msg_error_open_file       du "Error: can't open file ",0
msg_error_efi_lip_handle  du "efi_handle can't handle LIP",13,10,0
msg_error_lip_dev_sfsp    du "LIP device handle can't handle SFSP",13,10,0
msg_error_sfsp_openvolume du "SFSP OpenVolume failed",13,10,0

; msg_end_1 du 13,10,"---------- test printing functions:", 13,10,0
msg_newline du 13,10,0
msg_spacer du "    ", 0
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
volume_root     dq ?
file_handle     dq ? 
kernel_header_buf   dq 4 dup(?)


section '.reloc' fixups data discardable
