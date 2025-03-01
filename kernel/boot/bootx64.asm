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

MEMORY_MAP_SIZE = 0x10000 ; NOTE: can be bigger?

; linux/arch/x86/include/uapi/asm/e820.h
E820_RAM       = 1
E820_RESERVED  = 2
E820_ACPI      = 3
E820_NVS       = 4
E820_UNUSABLE  = 5
E820_PMEM      = 7

include 'uefi_prints.inc'

struct KERNEL64_HEADER
        magic              dq ? ; magic, must be 'KERNEL64'
        entry_point_offset dq ? ; offset of 64bit kernel entry point in file
        stack_size         dq ? ; default kernel stack in bytes
        phys_start         dq ? ; bootloader will put here phys addr where it loaded kernel
        phys_end           dq ? ; and phys end addr (including stack ofc)
        ; to be continued :)
ends


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

; return in rax file size
proc get_file_size uses r12 r13, _root, _name, _file_size
        mov     [_root], rcx
        mov     [_name], rdx
        mov     [_file_size], r8
        xor     r12, r12 ; default return value is 0
        ; mov     rbx, [efi_table]

        mov     r10, [_root]
        mov     r11, [_name]
        mov     [file_handle], 0 ; to know its empty if open fails
        fstcall [r10 + EFI_FILE_PROTOCOL.Open], r10, file_handle, r11, EFI_FILE_MODE_READ, 0
        test    eax, eax
        jnz     .error

        ; mov     rax, rsp
        ; fstcall efi_print_hex_fixed, rax

        mov     rcx, [file_handle]
        mov     [buf_size], 0
        fstcall [rcx + EFI_FILE_PROTOCOL.GetInfo], rcx, fid_guid, buf_size, 0
        mov     rdx, EFI_BUFFER_TOO_SMALL
        cmp     rax, rdx ; TODO how to do with imm?
        jnz     .error  ; if error is not EFI_BUFFER_TOO_SMALL then error

        mov     r10, [rbx + EFI_SYSTEM_TABLE.BootServices]
        mov     [buf_ptr], 0 ; to know its none if AllocatePool fails
        fstcall [r10 + EFI_BOOT_SERVICES.AllocatePool], EFI_LOADER_DATA, buf_size, buf_ptr
        test    eax, eax
        jnz     .error

        mov     rcx, [file_handle]
        fstcall [rcx + EFI_FILE_PROTOCOL.GetInfo], rcx, fid_guid, buf_size, [buf_ptr]
        test    eax, eax
        jnz     .error

        mov     rcx, [buf_ptr]
        mov     r12, [rcx + EFI_FILE_INFO.FileSize] ; here is the file size
        jmp     .exit

.error:
        fstcall efi_puts, msg_get_file_size_error

.exit:
        cmp     [file_handle], 0
        jz      @f
        mov     rcx, [file_handle]
        fstcall [rcx + EFI_FILE_PROTOCOL.Close], rcx
@@:
        cmp     [buf_ptr], 0
        jz      @f
        mov     r10, [rbx + EFI_SYSTEM_TABLE.BootServices]
        fstcall [r10 + EFI_BOOT_SERVICES.FreePool], [buf_ptr]
@@:
        mov     rax, [_file_size]
        mov     [rax], r12
        ret
endp

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;

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


;;;;;;;;;;;;;;;;; loading kernel to memory
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

;;;;;;;;;;;;;;;;; read kernel file header
        fstcall efi_puts, msg_newline
        ; fstcall efi_print_hex_fixed, [volume_root]
        fstcall efi_puts, msg_reading_kernel_header
        fstcall load_file, [volume_root], kernel_file_path, kernel_header_buf, sizeof.KERNEL64_HEADER, 1

        ; fstcall efi_puts, msg_newline
        fstcall efi_puts, msg_thisis_kernel_header
        xor     r14, r14
@@:
        fstcall efi_putc, qword [r14 + kernel_header_buf]
        inc     r14
        cmp     r14, 8
        jl      @b
@@:
        fstcall efi_puts, msg_newline

        fstcall efi_print_hex_no_lz, [kernel_header_buf + KERNEL64_HEADER.entry_point_offset]
        fstcall efi_puts, msg_newline

        fstcall efi_print_hex_no_lz, [kernel_header_buf + KERNEL64_HEADER.stack_size]
        fstcall efi_puts, msg_newline

;;;;;;;;;;;;;;;;; get the kernel file size
        fstcall efi_puts, msg_newline
        fstcall get_file_size, [volume_root], kernel_file_path, kernel_file_size
        fstcall efi_puts, "kernel file size = "
        fstcall efi_print_hex_no_lz, [kernel_file_size]
        fstcall efi_puts, msg_newline

        mov     rax, [kernel_file_size]
        add     rax, 4095
        and     rax, -4096 ; round up to next 4k boundary
        shr     rax, BSF 4096
        mov     [kernel_file_size_pages], rax
        fstcall efi_puts, "4k pages need for kernel image = "
        fstcall efi_print_hex_no_lz, [kernel_file_size_pages]
        fstcall efi_puts, msg_newline

        mov     rax, [kernel_header_buf + KERNEL64_HEADER.stack_size]
        add     rax, 4095
        and     rax, -4096
        shr     rax, BSF 4096
        mov     [kernel_stack_size_pages], rax
        fstcall efi_puts, "4k pages need for kernel stack = "
        fstcall efi_print_hex_no_lz, [kernel_stack_size_pages]
        fstcall efi_puts, msg_newline

        mov     rax, [kernel_file_size_pages]
        add     rax, [kernel_stack_size_pages]
        mov     [kernel_image_total_pages], rax
        fstcall efi_puts, "4k pages need for kernel image = "
        fstcall efi_print_hex_no_lz, [kernel_image_total_pages]
        fstcall efi_puts, msg_newline

        mov     r10, [rbx + EFI_SYSTEM_TABLE.BootServices]
        fstcall [r10 + EFI_BOOT_SERVICES.AllocatePages], EFI_ALLOCATE_ANY_PAGES, EFI_RESERVED_MEMORY_TYPE, \
                [kernel_image_total_pages], kernel_image_phys_base

        fstcall load_file, [volume_root], kernel_file_path, [kernel_image_phys_base], [kernel_file_size], 1

        mov     rax, [kernel_image_phys_base]
        mov     [rax + KERNEL64_HEADER.phys_start], rax
        mov     rcx, [kernel_image_total_pages]
        shl     rcx, BSF 4096
        add     rcx, rax
        mov     [rax + KERNEL64_HEADER.phys_end], rcx
        mov     [kernel_image_phys_end], rcx

        fstcall efi_puts, "Kernel + kernel_stack loaded to phys region ["
        fstcall efi_print_hex_fixed, [rax + KERNEL64_HEADER.phys_start]
        fstcall efi_puts, ", "
        fstcall efi_print_hex_fixed, [rax + KERNEL64_HEADER.phys_end]
        fstcall efi_puts, <")",13,10,0>

        ; Zero out the paging tables p1-p4
        mov     rdi, table_p1
        mov     rcx, 512*4
        xor     rax, rax
        cld
        rep     stosq

        ; P4[P4_OFFSET(VIRT_KERNEL_BASE)] = P3 or flags
        mov     rax, table_p3
        or      rax, 0x3 ; present, r/w
        mov     rdi, table_p4
        add     rdi, ((VIRT_KERNEL_BASE shr 39) and 0x1FF)*8 ; offset of entry in P4
        stosq

        ; P3[P3_OFFSET(VIRT_KERNEL_BASE)] = P2 or flags
        mov     rax, table_p2
        or      rax, 0x3 ; present, r/w
        mov     rdi, table_p3
        add     rdi, ((VIRT_KERNEL_BASE shr 30) and 0x1FF)*8
        stosq

        ; P2[P2_OFFSET(VIRT_KERNEL_BASE)] = P1 or flags
        mov     rax, table_p1
        or      rax, 0x3 ; present, r/w
        mov     rdi, table_p2
        add     rdi, ((VIRT_KERNEL_BASE shr 21) and 0x1FF)*8
        stosq

        mov     rcx, [kernel_image_phys_base]
        mov     rdx, VIRT_KERNEL_BASE
.fill_p1:
        mov     rax, rdx
        shr     rax, 12
        and     rax, 0x1FF
        shl     rax, BSF 8 ; *8
        add     rax, table_p1
        mov     rdi, rcx
        or      rdi, 0x3
        mov     [rax], rdi

        add     rcx, 4096
        add     rdx, 4096
        cmp     rcx, [kernel_image_phys_end]
        jb      .fill_p1 

        ; causes fault and reboots. maybe i need exitbootservices first? maybe paging structures incorrect
        ; mov     rax, table_p4
        ; mov     cr3, rax

        ; TODO ExitBootServices here

        mov     rax, [kernel_image_phys_base]
        mov     rax, [rax + KERNEL64_HEADER.entry_point_offset]
        add     rax, [kernel_image_phys_base] ; jump to phys kernel for now
        ; add     rax, VIRT_KERNEL_BASE
        jmp     rax

        ; TODO
        ;; map [kernel_phys_start; kernel_phys_end) to 0xFFFFFFFF80000000
        ;; (~~fill paging tables~~, ExitBootServices and switch to the new PML4), set kernel rsp, jmp to k64_entry
        ;; NOTE: dont allocate after getting memmap bc uefi allocations change the memmap. => perform GetMemoryMap last of all..
        ;; QUESTION: How to pass memory map to the kernel?
        ;; Push in contents in stack? (wtf). Write memmap to some buffer,
        ;; map it into kernel address space and push pointer to it into the kernel stack
        ;; getting memmap should be a separate function   


;;;;;;;;;;;;;;;;; test output
        fstcall efi_set_text_color, EFI_LIGHTGRAY
        fstcall efi_puts, <13,10,"-----------------",13,10,0>

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
fid_guid        db EFI_FILE_INFO_ID

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
msg_get_file_size_error   du "Error getting file size",13,10,0

; msg_end_1 du 13,10,"---------- test printing functions:", 13,10,0
msg_newline du 13,10,0
msg_spacer du "    ", 0
msg_dummy du "<....>", 0

msg_e820_memmap_here du "Memory map (phys_start, phys_end, e820 type)",13,10,0
msg_e820_available du "Available RAM", 0
msg_e820_reserved du "Reserved RAM", 0
msg_e820_acpi_reclaim du "ACPI reclaimable RAM", 0
msg_e820_acpi_nvs du "ACPI NVS RAM", 0
msg_e820_badmem du "Bad RAM", 0
msg_e820_persistent du "Persistent RAM", 0

e820_typenames:
        dq msg_dummy
        dq msg_e820_available
        dq msg_e820_reserved
        dq msg_e820_acpi_reclaim
        dq msg_e820_acpi_nvs
        dq msg_e820_badmem
        dq msg_dummy
        dq msg_e820_persistent


;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
section '.bss' data readable writeable discardable
memory_map               dq ?
volume_root              dq ?
file_handle              dq ? ; for load_file, get_file_size
buf_size                 dq ? ; for get_file_size
buf_ptr                  dq ? ; for get_file_size

kernel_header_buf        KERNEL64_HEADER
kernel_file_size         dq ?
kernel_file_size_pages   dq ?

kernel_stack_size_pages  dq ?

kernel_image_total_pages dq ?
kernel_image_phys_base   dq ?
kernel_image_phys_end    dq ?

align 4096
table_p1                 rq 512
table_p2                 rq 512
table_p3                 rq 512
table_p4                 rq 512


section '.reloc' fixups data discardable
