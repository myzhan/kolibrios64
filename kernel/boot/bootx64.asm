;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; UEFI loader for the 64-bit kernel. It loads KERNEL64.BIN, validates it,
;; builds page tables that keep the loader alive across the CR3 switch,
;; describes the machine in K64BOOTINFO, leaves boot services and hands
;; control to the kernel mapped at VIRT_KERNEL_BASE.
;;
format pe64 efi
entry main

section '.text' code executable readable

; fasm2 native macro headers: struct.inc, proc64.inc, TCHAR, DN and the
; fstcall wrapper, see kernel/fasm2_compat.inc
include '../fasm2_compat.inc'
include '../const.inc'

; FASM 1.x had to purge the DQ structure type because it shadowed the "dq"
; data directive. fasm2 is case sensitive, so "DQ" and "dq" coexist and the
; UEFI declarations below keep using DQ as their 8 byte field type.
include 'uefi64.inc'
include 'uefi_prints.inc'
include 'bootinfo.inc'

; page table entry bits
PAGE_PRESENT = 0x1
PAGE_RW      = 0x2
PAGE_PS      = 0x80

PAGE_SIZE = 0x1000

; Temporary identity map covering the first 4 GiB with 2 MiB pages. Every
; address the loader still touches after reloading CR3 (its own code, the
; current stack, globals, the memory map copy, K64BOOTINFO) must be below it.
; The limit itself belongs to the boot contract, see boot/bootinfo.inc.
IDENTITY_MAP_LIMIT     = K64_IDENTITY_MAP_LIMIT
IDENTITY_MAP_2M_PAGES  = IDENTITY_MAP_LIMIT/PAGE_SIZE/512
IDENTITY_MAP_2M_TABLES = IDENTITY_MAP_2M_PAGES/512

; Kernel image and its stack are mapped with 4 KiB pages taken from a fixed
; pool, so the number of mappable pages is bounded and never wraps a P1 table.
P1_POOL_PAGES = 16
MAX_KERNEL_IMAGE_PAGES = P1_POOL_PAGES*512
MAX_KERNEL_FILE_SIZE   = MAX_KERNEL_IMAGE_PAGES*PAGE_SIZE

; kernel supplied stack_size limits
MIN_KERNEL_STACK_SIZE = PAGE_SIZE
MAX_KERNEL_STACK_SIZE = 0x1000000 ; 16 MiB

; memory map buffer sizing
MEMORY_MAP_INITIAL_PAGES = 8
MEMORY_MAP_MAX_PAGES     = 64

; how many times ExitBootServices may be retried with a fresh memory map
EXIT_BOOT_SERVICES_RETRIES = 4

; COM1 is the only console that survives ExitBootServices, so the loader
; drives the UART itself once the first exit attempt was made. Every wait is
; bounded: a missing UART must not hang a machine that has no firmware left.
COM1_PORT           = 0x3f8
COM1_SPIN_LIMIT     = 0x100000
POST_EXIT_MAX_CHARS = 256

; The boot services tables stop being trustworthy at the very first
; ExitBootServices call, even when that call reports a stale memory map key
; and could be retried. boot_services_state guards every failure path, see
; boot_fail and boot_fail_status.
BOOT_SERVICES_ALIVE          = 0
BOOT_SERVICES_EXIT_ATTEMPTED = 1

; indices of VIRT_KERNEL_BASE in the paging structures
P4_INDEX_KERNEL = (VIRT_KERNEL_BASE shr 39) and 0x1FF
P3_INDEX_KERNEL = (VIRT_KERNEL_BASE shr 30) and 0x1FF
P2_INDEX_KERNEL = (VIRT_KERNEL_BASE shr 21) and 0x1FF

if (P4_INDEX_KERNEL <> 511) | (P3_INDEX_KERNEL <> 510) | (P2_INDEX_KERNEL <> 0)
        display "VIRT_KERNEL_BASE must map to PML4[511]/PDPT[510]/PD[0]", 13, 10
        err
end if

; the header layout is part of the boot contract, keep it in sync
if sizeof.KERNEL64_HEADER <> 40
        display "KERNEL64_HEADER must be 40 bytes", 13, 10
        err
end if

if sizeof.EFI_MEMORY_DESCRIPTOR <> K64_MEMORY_DESCRIPTOR_SIZE
        display "EFI_MEMORY_DESCRIPTOR size differs from the boot contract", 13, 10
        err
end if

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; stop the machine for good

halt_forever:
        cli
@@:
        hlt
        jmp     @b

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; configure COM1 for 115200 8N1
; the loader writes the UART registers itself because no firmware call is
; guaranteed to work once ExitBootServices was attempted
; destroys nothing that matters
proc com1_init uses rax rdx
        mov     dx, COM1_PORT + 1
        xor     al, al
        out     dx, al          ; no interrupts
        mov     dx, COM1_PORT + 3
        mov     al, 0x80
        out     dx, al          ; DLAB on
        mov     dx, COM1_PORT
        mov     al, 0x01
        out     dx, al          ; divisor 1, 115200 baud
        mov     dx, COM1_PORT + 1
        xor     al, al
        out     dx, al
        mov     dx, COM1_PORT + 3
        mov     al, 0x03
        out     dx, al          ; 8 bits, no parity, one stop bit
        mov     dx, COM1_PORT + 2
        mov     al, 0xC7
        out     dx, al          ; FIFO on, cleared
        mov     dx, COM1_PORT + 4
        mov     al, 0x03
        out     dx, al          ; DTR, RTS, OUT2
        ret
endp

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; write one byte to COM1
; in
; cl = byte
; the transmit register wait is bounded, a dead UART cannot hang the loader
proc com1_putc uses rbx
        mov     bl, cl
        mov     ecx, COM1_SPIN_LIMIT
.wait:
        mov     dx, COM1_PORT + 5
        in      al, dx
        test    al, 0x20        ; transmit holding register empty
        jnz     .ready
        dec     ecx
        jnz     .wait
        ret                     ; give up instead of spinning forever
.ready:
        mov     dx, COM1_PORT
        mov     al, bl
        out     dx, al
        ret
endp

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; write a zero terminated ASCII string to COM1
; in
; rcx = string
; the character count is bounded, a missing terminator cannot hang the loader
proc com1_puts uses rbx r12
        mov     rbx, rcx
        mov     r12, POST_EXIT_MAX_CHARS
.loop:
        test    r12, r12
        jz      .done
        movzx   eax, byte [rbx]
        test    eax, eax
        jz      .done
        mov     rcx, rax
        fstcall com1_putc, rcx
        inc     rbx
        dec     r12
        jmp     .loop
.done:
        ret
endp

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; write a zero terminated CHAR16 string to COM1
; in
; rcx = string
; the loader messages are ASCII, so dropping the high byte is lossless and
; also bounds the output to POST_EXIT_MAX_CHARS characters
proc com1_puts_tchar uses rbx r12
        mov     rbx, rcx
        mov     r12, POST_EXIT_MAX_CHARS
.loop:
        test    r12, r12
        jz      .done
        movzx   eax, word [rbx]
        test    eax, eax
        jz      .done
        mov     rcx, rax
        fstcall com1_putc, rcx
        add     rbx, 2
        dec     r12
        jmp     .loop
.done:
        ret
endp

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; write a 64-bit value as 0x prefixed hex to COM1
; in
; rcx = value
proc com1_puthex uses rbx r12
        mov     rbx, rcx
        mov     rcx, '0'
        fstcall com1_putc, rcx
        mov     rcx, 'x'
        fstcall com1_putc, rcx
        mov     r12, 60
.loop:
        mov     ecx, r12d
        mov     rax, rbx
        shr     rax, cl
        and     eax, 0xF
        cmp     eax, 10
        jb      .digit
        add     eax, 'A' - 10
        jmp     .emit
.digit:
        add     eax, '0'
.emit:
        mov     rcx, rax
        fstcall com1_putc, rcx
        sub     r12, 4
        jns     .loop
        ret
endp

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; report a fatal loader error over COM1 and halt
; this is the post ExitBootServices attempt entry point: no boot service and
; no UEFI console may be used any more, only port I/O
; in
; rcx = zero terminated CHAR16 message
; never returns
proc post_exit_fail
        xor     edx, edx
        fstcall post_exit_fail_status, rcx, rdx
        jmp     halt_forever
endp

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; report a fatal loader error and an EFI status over COM1 and halt
; in
; rcx = zero terminated CHAR16 message
; rdx = EFI_STATUS, zero when the failure has no status of its own
; never returns
proc post_exit_fail_status uses rbx r12
        mov     rbx, rcx
        mov     r12, rdx
        fstcall com1_init
        lea     rcx, [msg_post_exit_prefix]
        fstcall com1_puts_tchar, rcx
        mov     rcx, rbx
        fstcall com1_puts_tchar, rcx
        lea     rcx, [msg_status]
        fstcall com1_puts_tchar, rcx
        mov     rcx, r12
        fstcall com1_puthex, rcx
        lea     rcx, [msg_newline]
        fstcall com1_puts_tchar, rcx
        jmp     halt_forever
endp

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; print a fatal loader error and halt
; in
; rcx = zero terminated message
; destroys nothing that matters, never returns
; once ExitBootServices was called the console is not used any more, the
; guard below turns this into the COM1 only path
proc boot_fail uses rbx
        mov     rax, [boot_services_state]
        test    rax, rax
        jnz     .boot_services_gone
        mov     rbx, rcx
        fstcall efi_puts, msg_error_prefix
        fstcall efi_puts, rbx
        fstcall efi_puts, msg_newline
        jmp     halt_forever
.boot_services_gone:
        fstcall post_exit_fail, rcx
        jmp     halt_forever
endp

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; print a fatal loader error with an EFI status and halt
; in
; rcx = zero terminated message
; rdx = EFI_STATUS
; never returns
; same guard as boot_fail: no UEFI console after the first exit attempt
proc boot_fail_status uses rbx r12
        mov     rax, [boot_services_state]
        test    rax, rax
        jnz     .boot_services_gone
        mov     rbx, rcx
        mov     r12, rdx
        fstcall efi_puts, msg_error_prefix
        fstcall efi_puts, rbx
        fstcall efi_puts, msg_status
        fstcall efi_print_hex_fixed, r12
        fstcall efi_puts, msg_newline
        jmp     halt_forever
.boot_services_gone:
        fstcall post_exit_fail_status, rcx, rdx
        jmp     halt_forever
endp

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; close the file opened by load_file or get_file_size, if any
; in
; file_handle
; out
; file_handle = 0
proc close_file uses r10
        cmp     [file_handle], 0
        jz      .done
        mov     r10, [file_handle]
        ; fasm2 needs the size of an indirect callee spelled out
        fstcall qword [r10 + EFI_FILE_PROTOCOL.Close], r10
        mov     [file_handle], 0
.done:
        ret
endp

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; free the EFI_FILE_INFO buffer allocated by get_file_size, if any
; in
; info_ptr
; out
; info_ptr = 0
proc free_info_buffer uses r10
        cmp     [info_ptr], 0
        jz      .done
        mov     r10, [efi_table]
        mov     r10, qword [r10 + EFI_SYSTEM_TABLE.BootServices]
        fstcall qword [r10 + EFI_BOOT_SERVICES.FreePool], [info_ptr]
        mov     [info_ptr], 0
.done:
        ret
endp

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; read a file into a buffer, closing it in any case
; in
; rcx = EFI_FILE_PROTOCOL of the root directory
; rdx = file path (CHAR16, zero terminated)
; r8  = destination buffer
; r9  = number of bytes that must be read
; out
; lf_read_size = number of bytes actually read
; a short read is a fatal error, the caller never sees one
proc load_file uses r12
        mov     [lf_root], rcx
        mov     [lf_name], rdx
        mov     [lf_buffer], r8
        mov     [lf_expected], r9

        mov     [file_handle], 0
        mov     r10, rcx
        mov     r11, rdx
        fstcall qword [r10 + EFI_FILE_PROTOCOL.Open], r10, file_handle, r11, EFI_FILE_MODE_READ, 0
        test    rax, rax
        jz      .opened
        fstcall boot_fail_status, msg_error_open_file, rax
.opened:
        mov     rax, [lf_expected]
        mov     [lf_read_size], rax
        mov     r10, [file_handle]
        lea     rdx, [lf_read_size]
        mov     r8, [lf_buffer]
        fstcall qword [r10 + EFI_FILE_PROTOCOL.Read], r10, rdx, r8
        mov     r12, rax
        fstcall close_file
        test    r12, r12
        jz      .read_ok
        fstcall boot_fail_status, msg_error_read_file, r12
.read_ok:
        mov     rax, [lf_read_size]
        cmp     rax, [lf_expected]
        jz      .done
        fstcall boot_fail_status, msg_error_short_read, rax
.done:
        ret
endp

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; get the size of a file
; in
; rcx = EFI_FILE_PROTOCOL of the root directory
; rdx = file path
; out
; rax = file size in bytes, any error is fatal
proc get_file_size uses r12 r13
        mov     [file_handle], 0
        mov     [info_ptr], 0
        mov     [info_size], 0

        mov     r10, rcx
        mov     r11, rdx
        fstcall qword [r10 + EFI_FILE_PROTOCOL.Open], r10, file_handle, r11, EFI_FILE_MODE_READ, 0
        test    rax, rax
        jz      .opened
        fstcall boot_fail_status, msg_error_open_file, rax
.opened:
        ; a null buffer makes GetInfo report the required size
        mov     r10, [file_handle]
        fstcall qword [r10 + EFI_FILE_PROTOCOL.GetInfo], r10, fid_guid, info_size, 0
        mov     r12, rax
        mov     r13, EFI_BUFFER_TOO_SMALL
        cmp     rax, r13
        jz      .got_size
        fstcall close_file
        fstcall boot_fail_status, msg_error_get_file_size, r12
.got_size:
        ; a short EFI_FILE_INFO is fine, the fixed part plus a name is enough
        mov     rax, [info_size]
        mov     r13, EFI_FILE_INFO.FileName + 2
        cmp     rax, r13
        jb      .small_info
        ; EFI_FILE_INFO is variable sized, allocate exactly what GetInfo wants
        mov     r10, [efi_table]
        mov     r10, qword [r10 + EFI_SYSTEM_TABLE.BootServices]
        fstcall qword [r10 + EFI_BOOT_SERVICES.AllocatePool], EFI_LOADER_DATA, [info_size], info_ptr
        mov     r12, rax
        test    rax, rax
        jz      .allocated
        fstcall close_file
        fstcall boot_fail_status, msg_error_allocate_pool, r12
.allocated:
        mov     r10, [file_handle]
        fstcall qword [r10 + EFI_FILE_PROTOCOL.GetInfo], r10, fid_guid, info_size, [info_ptr]
        mov     r12, rax
        test    rax, rax
        jz      .info_ok
        fstcall free_info_buffer
        fstcall close_file
        fstcall boot_fail_status, msg_error_get_file_size, r12
.info_ok:
        mov     rax, [info_ptr]
        mov     r12, qword [rax + EFI_FILE_INFO.FileSize]
        fstcall free_info_buffer
        fstcall close_file
        mov     rax, r12
        ret
.small_info:
        fstcall close_file
        fstcall boot_fail, msg_error_file_info_size
endp

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; allocate pages for the memory map buffer
; in
; memory_map_pages
; out
; memory_map_addr, rax = EFI_STATUS
; the address is zeroed before the call, so a failed allocation can never
; leave a stale pointer behind
proc alloc_memory_map uses r10
        mov     [memory_map_addr], 0
        mov     r10, [efi_table]
        mov     r10, qword [r10 + EFI_SYSTEM_TABLE.BootServices]
        ; EfiLoaderData is the only legal type for pages owned by an image
        fstcall qword [r10 + EFI_BOOT_SERVICES.AllocatePages], EFI_ALLOCATE_ANY_PAGES, \
                EFI_LOADER_DATA, [memory_map_pages], memory_map_addr
        test    rax, rax
        jnz     .done
        ; a successful allocation never hands out page zero, but a zero here
        ; would be a silent dangling pointer in BootInfo
        cmp     [memory_map_addr], 0
        jnz     .done
        mov     rax, EFI_OUT_OF_RESOURCES
.done:
        ret
endp

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; release the memory map buffer, if any
; out
; rax = EFI_STATUS
; the address and the page count are only cleared when FreePages reported
; success, so a failed release neither loses the pointer nor frees twice
proc free_memory_map uses r10 r11
        cmp     [memory_map_addr], 0
        jz      .nothing
        mov     r10, [efi_table]
        mov     r10, qword [r10 + EFI_SYSTEM_TABLE.BootServices]
        mov     r11, [memory_map_addr]
        ; the page count belongs to the buffer that is still mapped
        fstcall qword [r10 + EFI_BOOT_SERVICES.FreePages], r11, [memory_map_pages]
        test    rax, rax
        jnz     .done
        mov     [memory_map_addr], 0
        mov     [memory_map_pages], 0
.done:
        ret
.nothing:
        xor     eax, eax
        ret
endp

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; double the memory map buffer after a BUFFER_TOO_SMALL answer
; out
; rax = 1 on success, 0 on failure with memory_map_error_msg and
; memory_map_error_status describing it
; the live buffer is released with the page count it was allocated with, and
; the bigger page count is published only after that release succeeded
proc grow_memory_map uses rbx r12
        mov     r12, [memory_map_pages] ; page count of the live buffer
        mov     rax, r12
        shl     rax, 1
        mov     rbx, MEMORY_MAP_MAX_PAGES
        cmp     rax, rbx
        jbe     .fits
        mov     rcx, msg_error_memory_map_too_big
        xor     edx, edx
        jmp     .fail
.fits:
        mov     rbx, rax ; page count the buffer is going to have
        fstcall free_memory_map
        test    rax, rax
        jnz     .free_failed
        ; the old buffer is gone for good, the release above used the old
        ; count from memory_map_pages: only now the new size may take over
        mov     [memory_map_pages], rbx
        fstcall alloc_memory_map
        test    rax, rax
        jnz     .alloc_failed
        mov     eax, 1
        ret
.free_failed:
        ; memory_map_addr and memory_map_pages still describe the live buffer
        mov     rcx, msg_error_free_pages
        mov     rdx, rax
        jmp     .fail
.alloc_failed:
        mov     rcx, msg_error_allocate_pages
        mov     rdx, rax
.fail:
        mov     [memory_map_error_msg], rcx
        mov     [memory_map_error_status], rdx
        xor     eax, eax
        ret
endp

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; fetch the UEFI memory map into a buffer that is kept for the kernel
; out
; memory_map_* are filled, rax = 1 on success, 0 on failure with
; memory_map_error_msg and memory_map_error_status describing it
; nothing here prints: the caller decides whether the UEFI console or the
; COM1 only path is still allowed
proc get_memory_map uses rbx r12
        mov     [memory_map_error_msg], 0
        mov     [memory_map_error_status], 0
        ; a fresh map replaces the previous one, if any
        fstcall free_memory_map
        test    rax, rax
        jnz     .free_failed
        mov     [memory_map_pages], MEMORY_MAP_INITIAL_PAGES
        fstcall alloc_memory_map
        test    rax, rax
        jnz     .alloc_failed
.retry:
        mov     rax, [memory_map_pages]
        shl     rax, 12
        mov     [memory_map_size], rax
        mov     [memory_map_desc_size], 0
        mov     [memory_map_desc_version], 0
        mov     [memory_map_key], 0
        mov     r10, [efi_table]
        mov     r10, qword [r10 + EFI_SYSTEM_TABLE.BootServices]
        fstcall qword [r10 + EFI_BOOT_SERVICES.GetMemoryMap], memory_map_size, \
                [memory_map_addr], memory_map_key, memory_map_desc_size, memory_map_desc_version
        test    rax, rax
        jz      .got_map
        mov     rbx, EFI_BUFFER_TOO_SMALL
        cmp     rax, rbx
        jnz     .call_failed
        ; the buffer is too small: grow it, then start over
        fstcall grow_memory_map
        test    rax, rax
        jz      .failed
        jmp     .retry
.got_map:
        mov     rbx, [memory_map_desc_size]
        test    rbx, rbx
        jz      .bad_desc_size
        mov     rax, sizeof.EFI_MEMORY_DESCRIPTOR
        cmp     rbx, rax
        jb      .bad_desc_size
        mov     rax, [memory_map_size]
        xor     rdx, rdx
        div     rbx
        test    rax, rax
        jz      .bad_desc_size
        mov     [memory_map_desc_count], rax
        mov     eax, 1
        ret
.call_failed:
        mov     rcx, msg_error_get_memory_map
        mov     rdx, rax
        jmp     .fail
.free_failed:
        ; the live buffer was not released and is still described correctly
        mov     rcx, msg_error_free_pages
        mov     rdx, rax
        jmp     .fail
.alloc_failed:
        mov     rcx, msg_error_allocate_pages
        mov     rdx, rax
        jmp     .fail
.bad_desc_size:
        mov     rcx, msg_error_memory_map_desc_size
        xor     edx, edx
.fail:
        mov     [memory_map_error_msg], rcx
        mov     [memory_map_error_status], rdx
.failed:
        xor     eax, eax
        ret
endp

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; the memory map copy must stay inside the temporary identity map and inside
; the pages that were allocated for it, otherwise the kernel could not read it
; out
; rax = 1 when the range is sane, 0 otherwise
proc check_memory_map_range uses r10
        mov     rax, [memory_map_addr]
        test    rax, rax
        jz      .bad
        mov     r10, IDENTITY_MAP_LIMIT
        cmp     rax, r10
        jae     .bad
        add     rax, [memory_map_size]
        jc      .bad
        cmp     rax, r10
        ja      .bad
        ; and it must not reach beyond the pages that back it
        mov     rax, [memory_map_pages]
        shl     rax, 12
        cmp     [memory_map_size], rax
        ja      .bad
        mov     eax, 1
        ret
.bad:
        xor     eax, eax
        ret
endp

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; publish the current memory map to the kernel
; every field the kernel reads is refreshed together with the pointer, so the
; handover can never advertise a released or stale map
proc store_bootinfo_memmap
        mov     rax, [memory_map_addr]
        mov     qword [bootinfo + K64BOOTINFO.memmap_addr], rax
        mov     rax, [memory_map_size]
        mov     qword [bootinfo + K64BOOTINFO.memmap_size], rax
        mov     rax, [memory_map_desc_size]
        mov     dword [bootinfo + K64BOOTINFO.memmap_desc_size], eax
        mov     rax, [memory_map_desc_version]
        mov     dword [bootinfo + K64BOOTINFO.memmap_desc_version], eax
        mov     rax, [memory_map_desc_count]
        mov     qword [bootinfo + K64BOOTINFO.memmap_desc_count], rax
        ret
endp

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; build the page tables used from the CR3 switch on
; in
; rcx = physical base of the kernel image
; rdx = number of 4 KiB pages occupied by the image and its stack
; out
; table_p4 is ready for CR3, any inconsistency is fatal
proc build_page_tables uses rbx r12 r13 r14 r15
        mov     [pt_phys], rcx
        mov     [pt_pages], rdx

        ; 1. the tables live in one contiguous, page aligned block
        lea     rdi, [page_tables]
        mov     rcx, (page_tables_end - page_tables)/8
        xor     rax, rax
        cld
        rep     stosq

        ; 2. temporary identity map of the first 4 GiB, 2 MiB pages
        lea     rax, [table_p3_ident]
        or      rax, PAGE_PRESENT or PAGE_RW
        mov     [table_p4], rax

        lea     rbx, [table_p2_ident]
        xor     r12, r12
.ident_p3:
        mov     rax, rbx
        or      rax, PAGE_PRESENT or PAGE_RW
        mov     [table_p3_ident + r12*8], rax
        add     rbx, PAGE_SIZE
        inc     r12
        cmp     r12, IDENTITY_MAP_2M_TABLES
        jb      .ident_p3

        lea     rdi, [table_p2_ident]
        xor     r12, r12
        mov     r13, IDENTITY_MAP_2M_PAGES
.ident_page:
        mov     rax, r12
        shl     rax, 21
        or      rax, PAGE_PRESENT or PAGE_RW or PAGE_PS
        mov     [rdi], rax
        add     rdi, 8
        inc     r12
        cmp     r12, r13
        jb      .ident_page

        ; 3. kernel image and stack at VIRT_KERNEL_BASE, 4 KiB pages
        lea     rax, [table_p3_kernel]
        or      rax, PAGE_PRESENT or PAGE_RW
        mov     [table_p4 + P4_INDEX_KERNEL*8], rax

        lea     rax, [table_p2_kernel]
        or      rax, PAGE_PRESENT or PAGE_RW
        mov     [table_p3_kernel + P3_INDEX_KERNEL*8], rax

        mov     r14, [pt_phys]
        mov     r15, [pt_pages]
        xor     r12, r12 ; index of the P1 table taken from the pool
.attach_p1:
        ; an image that fills the pool exactly needs no further table
        test    r15, r15
        jz      .done
        mov     rbx, P1_POOL_PAGES
        cmp     r12, rbx
        jae     .pool_exhausted
        mov     rax, r12
        shl     rax, 12
        lea     rax, [rax + table_p1_pool]
        mov     r10, rax ; current P1 table
        or      rax, PAGE_PRESENT or PAGE_RW
        mov     rcx, P2_INDEX_KERNEL
        add     rcx, r12
        mov     [table_p2_kernel + rcx*8], rax
        xor     r13, r13
.map_page:
        test    r15, r15
        jz      .done
        mov     rax, r14
        or      rax, PAGE_PRESENT or PAGE_RW
        mov     [r10 + r13*8], rax
        add     r14, PAGE_SIZE
        dec     r15
        inc     r13
        cmp     r13, 512
        jb      .map_page
        inc     r12
        jmp     .attach_p1
.done:
        ret
.pool_exhausted:
        fstcall boot_fail, msg_error_page_table_pool
endp

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; call ExitBootServices, refreshing the memory map if its key moved
; out
; returns with boot services gone, any earlier failure is fatal
; from the first call on nothing here may use the UEFI console: boot_fail and
; boot_fail_status route every failure to the COM1 only path. GetMemoryMap,
; FreePages and AllocatePages are only reached when ExitBootServices returned
; an error, which leaves boot services running by definition.
proc exit_boot_services uses rbx r12
        mov     [boot_services_state], BOOT_SERVICES_EXIT_ATTEMPTED
        mov     r12, EXIT_BOOT_SERVICES_RETRIES
.attempt:
        mov     r10, [efi_table]
        mov     r10, qword [r10 + EFI_SYSTEM_TABLE.BootServices]
        fstcall qword [r10 + EFI_BOOT_SERVICES.ExitBootServices], [efi_handle], [memory_map_key]
        test    rax, rax
        jz      .done
        mov     rbx, rax
        mov     rcx, EFI_INVALID_PARAMETER
        cmp     rax, rcx
        jnz     .failed
        ; the map key is stale but boot services answered: fetch a fresh map,
        ; republish it completely, then retry with the key it carries
        fstcall get_memory_map
        test    rax, rax
        jz      .map_failed
        fstcall check_memory_map_range
        test    rax, rax
        jz      .map_not_mapped
        fstcall store_bootinfo_memmap
        dec     r12
        jnz     .attempt
        mov     rbx, EFI_INVALID_PARAMETER
.failed:
        mov     rcx, msg_error_exit_boot_services
        mov     rdx, rbx
        fstcall post_exit_fail_status, rcx, rdx
        jmp     halt_forever
.map_failed:
        mov     rcx, [memory_map_error_msg]
        mov     rdx, [memory_map_error_status]
        fstcall post_exit_fail_status, rcx, rdx
        jmp     halt_forever
.map_not_mapped:
        mov     rcx, msg_error_memmap_not_mapped
        xor     edx, edx
        fstcall post_exit_fail_status, rcx, rdx
        jmp     halt_forever
.done:
        ret
endp

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; entry point

proc main _efi_handle, _efi_table
        mov     [efi_handle], rcx
        mov     [efi_table], rdx

        ; reset the console
        mov     rax, [efi_table]
        mov     rax, qword [rax + EFI_SYSTEM_TABLE.ConOut]
        fstcall qword [rax + EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL.Reset], rax, 1
        test    rax, rax
        jz      .console_ok
        jmp     halt_forever
.console_ok:
        ; disable the default watchdog timer, otherwise the pc reboots in 5 min
        mov     rax, [efi_table]
        mov     rax, qword [rax + EFI_SYSTEM_TABLE.BootServices]
        fstcall qword [rax + EFI_BOOT_SERVICES.SetWatchdogTimer], 0, 0, 0, 0
        test    rax, rax
        jz      .watchdog_ok
        fstcall boot_fail_status, msg_error_disable_watchdog, rax
.watchdog_ok:
        fstcall efi_set_text_color, EFI_LIGHTGREEN
        fstcall efi_puts, msg_hello_k64_loader
        fstcall efi_set_text_color, EFI_LIGHTGRAY

        fstcall efi_puts, msg_firmware_vendor
        mov     rax, [efi_table]
        fstcall efi_puts, qword [rax + EFI_SYSTEM_TABLE.FirmwareVendor]
        fstcall efi_puts, msg_newline

;;;;;;;;;;;;;;;;; everything the loader still uses must stay inside the
;;;;;;;;;;;;;;;;; identity map, the current stack included
        mov     rax, IDENTITY_MAP_LIMIT
        cmp     rsp, rax
        jb      .stack_mapped
        fstcall boot_fail, msg_error_stack_not_mapped
.stack_mapped:

        mov     rax, [efi_table]
        mov     r10, qword [rax + EFI_SYSTEM_TABLE.BootServices]
        fstcall qword [r10 + EFI_BOOT_SERVICES.HandleProtocol], [efi_handle], lip_guid, lip_interface
        test    rax, rax
        jz      .lip_ok
        fstcall boot_fail_status, msg_error_efi_lip_handle, rax
.lip_ok:
        mov     r10, [lip_interface]
        mov     rax, qword [r10 + EFI_LOADED_IMAGE_PROTOCOL.ImageBase]
        mov     rdx, qword [r10 + EFI_LOADED_IMAGE_PROTOCOL.ImageSize]
        mov     rcx, IDENTITY_MAP_LIMIT
        cmp     rax, rcx
        jae     .image_not_mapped
        add     rax, rdx
        jc      .image_not_mapped
        cmp     rax, rcx
        ja      .image_not_mapped
        jmp     .image_ok
.image_not_mapped:
        fstcall boot_fail, msg_error_image_not_mapped
.image_ok:

;;;;;;;;;;;;;;;;; open the volume the loader was started from
        mov     r11, [lip_interface]
        mov     rax, [efi_table]
        mov     r10, qword [rax + EFI_SYSTEM_TABLE.BootServices]
        fstcall qword [r10 + EFI_BOOT_SERVICES.HandleProtocol], \
                qword [r11 + EFI_LOADED_IMAGE_PROTOCOL.DeviceHandle], sfsp_guid, sfsp_interface
        test    rax, rax
        jz      .sfsp_ok
        fstcall boot_fail_status, msg_error_lip_dev_sfsp, rax
.sfsp_ok:
        mov     r10, [sfsp_interface]
        fstcall qword [r10 + EFI_SIMPLE_FILE_SYSTEM_PROTOCOL.OpenVolume], r10, volume_root
        test    rax, rax
        jz      .volume_ok
        fstcall boot_fail_status, msg_error_sfsp_openvolume, rax
.volume_ok:

;;;;;;;;;;;;;;;;; validate the kernel file size
        fstcall efi_puts, msg_reading_kernel_size
        mov     rcx, [volume_root]
        mov     rdx, kernel_file_path
        fstcall get_file_size, rcx, rdx
        mov     [kernel_file_size], rax
        test    rax, rax
        jz      .empty_file
        mov     rcx, MAX_KERNEL_FILE_SIZE
        cmp     rax, rcx
        ja      .huge_file
        mov     rcx, sizeof.KERNEL64_HEADER
        cmp     rax, rcx
        jbe     .tiny_file

        fstcall efi_puts, msg_kernel_file_size
        fstcall efi_print_hex_no_lz, [kernel_file_size]
        fstcall efi_puts, msg_newline
        jmp     .size_ok
.empty_file:
        fstcall boot_fail, msg_error_kernel_file_empty
.huge_file:
        fstcall boot_fail, msg_error_kernel_file_too_big
.tiny_file:
        fstcall boot_fail, msg_error_kernel_file_too_small
.size_ok:

;;;;;;;;;;;;;;;;; read and validate the kernel header
        mov     rcx, [volume_root]
        mov     rdx, kernel_file_path
        lea     r8, [kernel_header_buf]
        mov     r9, sizeof.KERNEL64_HEADER
        fstcall load_file, rcx, rdx, r8, r9

        mov     rax, qword [kernel_header_buf + KERNEL64_HEADER.magic]
        mov     rcx, KERNEL64_MAGIC
        cmp     rax, rcx
        jz      .magic_ok
        fstcall boot_fail, msg_error_kernel_magic
.magic_ok:
        mov     rax, qword [kernel_header_buf + KERNEL64_HEADER.entry_point_offset]
        mov     rcx, sizeof.KERNEL64_HEADER
        cmp     rax, rcx
        jb      .bad_entry
        cmp     rax, [kernel_file_size]
        jae     .bad_entry
        mov     [kernel_entry_offset], rax

        mov     rax, qword [kernel_header_buf + KERNEL64_HEADER.stack_size]
        mov     rcx, MIN_KERNEL_STACK_SIZE
        cmp     rax, rcx
        jb      .bad_stack
        mov     rcx, MAX_KERNEL_STACK_SIZE
        cmp     rax, rcx
        ja      .bad_stack
        test    rax, PAGE_SIZE-1
        jnz     .bad_stack

        fstcall efi_puts, msg_kernel_entry
        fstcall efi_print_hex_no_lz, [kernel_entry_offset]
        fstcall efi_puts, msg_kernel_stack
        fstcall efi_print_hex_no_lz, qword [kernel_header_buf + KERNEL64_HEADER.stack_size]
        fstcall efi_puts, msg_newline
        jmp     .header_ok
.bad_entry:
        fstcall boot_fail, msg_error_kernel_entry
.bad_stack:
        fstcall boot_fail, msg_error_kernel_stack
.header_ok:

;;;;;;;;;;;;;;;;; count the pages needed by the image and its stack
        mov     rax, [kernel_file_size]
        add     rax, PAGE_SIZE-1
        jc      .too_many_pages
        and     rax, -PAGE_SIZE
        shr     rax, 12
        mov     [kernel_file_pages], rax

        mov     rax, qword [kernel_header_buf + KERNEL64_HEADER.stack_size]
        shr     rax, 12
        mov     [kernel_stack_pages], rax

        mov     rax, [kernel_file_pages]
        add     rax, [kernel_stack_pages]
        jc      .too_many_pages
        mov     rcx, MAX_KERNEL_IMAGE_PAGES
        cmp     rax, rcx
        ja      .too_many_pages
        mov     [kernel_total_pages], rax

        fstcall efi_puts, msg_kernel_pages
        fstcall efi_print_hex_no_lz, [kernel_file_pages]
        fstcall efi_puts, msg_kernel_stack_pages
        fstcall efi_print_hex_no_lz, [kernel_stack_pages]
        fstcall efi_puts, msg_newline
        jmp     .pages_ok
.too_many_pages:
        fstcall boot_fail, msg_error_kernel_too_many_pages
.pages_ok:

;;;;;;;;;;;;;;;;; reserve and load the kernel image
        mov     [kernel_phys_base], 0
        mov     rax, [efi_table]
        mov     r10, qword [rax + EFI_SYSTEM_TABLE.BootServices]
        fstcall qword [r10 + EFI_BOOT_SERVICES.AllocatePages], EFI_ALLOCATE_ANY_PAGES, \
                EFI_LOADER_DATA, [kernel_total_pages], kernel_phys_base
        test    rax, rax
        jz      .allocated
        fstcall boot_fail_status, msg_error_allocate_pages, rax
.allocated:
        mov     rax, [kernel_phys_base]
        test    rax, rax
        jz      .bad_allocation
        mov     rcx, [kernel_total_pages]
        shl     rcx, 12
        add     rcx, rax
        jc      .bad_allocation
        mov     [kernel_phys_end], rcx
        jmp     .range_ok
.bad_allocation:
        fstcall boot_fail, msg_error_allocate_pages
.range_ok:
        ; the stack starts right after the page aligned image
        mov     rax, [kernel_file_pages]
        shl     rax, 12
        add     rax, [kernel_phys_base]
        mov     [kernel_stack_phys_start], rax

        mov     rcx, [volume_root]
        mov     rdx, kernel_file_path
        mov     r8, [kernel_phys_base]
        mov     r9, [kernel_file_size]
        fstcall load_file, rcx, rdx, r8, r9

        ; tell the kernel where it lives
        mov     rax, [kernel_phys_base]
        mov     rcx, [kernel_phys_end]
        mov     qword [rax + KERNEL64_HEADER.phys_start], rax
        mov     qword [rax + KERNEL64_HEADER.phys_end], rcx

        fstcall efi_puts, msg_kernel_phys
        fstcall efi_print_hex_fixed, [kernel_phys_base]
        fstcall efi_puts, msg_comma
        fstcall efi_print_hex_fixed, [kernel_phys_end]
        fstcall efi_puts, msg_kernel_stack_phys
        fstcall efi_print_hex_fixed, [kernel_stack_phys_start]
        fstcall efi_puts, msg_comma
        fstcall efi_print_hex_fixed, [kernel_phys_end]
        fstcall efi_puts, msg_newline

;;;;;;;;;;;;;;;;; build the page tables that survive the CR3 switch
        mov     rcx, [kernel_phys_base]
        mov     rdx, [kernel_total_pages]
        fstcall build_page_tables, rcx, rdx

;;;;;;;;;;;;;;;;; keep a copy of the memory map for the kernel
        fstcall get_memory_map
        test    rax, rax
        jnz     .memmap_fetched
        mov     rcx, [memory_map_error_msg]
        mov     rdx, [memory_map_error_status]
        fstcall boot_fail_status
.memmap_fetched:
        ; the copy is read by the kernel through the identity map, so it must
        ; fit below it; the ExitBootServices retry repeats the same check
        fstcall check_memory_map_range
        test    rax, rax
        jnz     .memmap_mapped
        fstcall boot_fail, msg_error_memmap_not_mapped
.memmap_mapped:

        fstcall efi_puts, msg_memory_map
        fstcall efi_print_hex_fixed, [memory_map_addr]
        fstcall efi_puts, msg_comma
        fstcall efi_print_hex_no_lz, [memory_map_size]
        fstcall efi_puts, msg_memory_map_desc
        fstcall efi_print_hex_no_lz, [memory_map_desc_size]
        fstcall efi_puts, msg_comma
        fstcall efi_print_hex_no_lz, [memory_map_desc_count]
        fstcall efi_puts, msg_newline

;;;;;;;;;;;;;;;;; describe the machine for the kernel
        lea     rdi, [bootinfo]
        mov     rcx, sizeof.K64BOOTINFO/8
        xor     rax, rax
        cld
        rep     stosq

        mov     rax, K64BOOTINFO_SIGNATURE
        mov     qword [bootinfo + K64BOOTINFO.signature], rax
        mov     dword [bootinfo + K64BOOTINFO.version], K64BOOTINFO_VERSION
        mov     dword [bootinfo + K64BOOTINFO.struct_size], sizeof.K64BOOTINFO
        mov     qword [bootinfo + K64BOOTINFO.flags], K64BOOTINFO_FLAG_MEMORY_MAP

        fstcall store_bootinfo_memmap

        mov     rax, [kernel_phys_base]
        mov     qword [bootinfo + K64BOOTINFO.kernel_phys_start], rax
        mov     rax, [kernel_phys_end]
        mov     qword [bootinfo + K64BOOTINFO.kernel_phys_end], rax
        mov     rax, [kernel_stack_phys_start]
        mov     qword [bootinfo + K64BOOTINFO.stack_phys_start], rax
        mov     rax, [kernel_phys_end]
        mov     qword [bootinfo + K64BOOTINFO.stack_phys_end], rax

        ; GOP, RSDP and initramfs stay zero: the matching flags are clear and
        ; the kernel must not expect any of them in version 1
        fstcall efi_puts, msg_bootinfo
        lea     rax, [bootinfo]
        fstcall efi_print_hex_fixed, rax
        fstcall efi_puts, msg_comma
        fstcall efi_print_hex_no_lz, sizeof.K64BOOTINFO
        fstcall efi_puts, msg_newline

;;;;;;;;;;;;;;;;; no way back after this call
        ; interrupts must not fire into firmware code once it is gone
        cli
        fstcall exit_boot_services

        ; boot services are gone: only registers, our page tables and COM1
        lea     rax, [table_p4]
        mov     cr3, rax

        ; the temporary identity map keeps this code and the stack alive,
        ; while the kernel itself runs from the high half
        lea     rcx, [bootinfo] ; physical address, still reachable
        mov     rax, [kernel_entry_offset]
        add     rax, VIRT_KERNEL_BASE
        jmp     rax

        jmp     halt_forever
endp

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
section '.data' data readable writeable
efi_handle  dq 0
efi_table   dq 0

lip_interface   dq 0
sfsp_interface  dq 0

hex_codes:
        db '0123456789ABCDEF', 0

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
section '.rodata' data readable

gop_guid        db EFI_GRAPHICS_OUTPUT_PROTOCOL_GUID
lip_guid        db EFI_LOADED_IMAGE_PROTOCOL_GUID
sfsp_guid       db EFI_SIMPLE_FILE_SYSTEM_PROTOCOL_GUID
pcirbiop_guid   db EFI_PCI_ROOT_BRIDGE_IO_PROTOCOL_GUID
fid_guid        db EFI_FILE_INFO_ID

kernel_file_path du '\EFI\KERNEL64.BIN', 0

msg_hello_k64_loader du "Kolibri64 EFI bootloader", 13, 10, 0
msg_firmware_vendor du "UEFI vendor: ", 0
msg_reading_kernel_size du "Reading kernel file size..", 13, 10, 0
msg_kernel_file_size du "Kernel file size: ", 0
msg_kernel_entry du "Kernel entry offset: ", 0
msg_kernel_stack du ", stack size: ", 0
msg_kernel_pages du "Kernel image pages: ", 0
msg_kernel_stack_pages du ", stack pages: ", 0
msg_kernel_phys du "Kernel image: [", 0
msg_kernel_stack_phys du "), stack: [", 0
msg_memory_map du "Memory map: ", 0
msg_memory_map_desc du ", descriptor size ", 0
msg_bootinfo du "BootInfo: ", 0
msg_comma du ", ", 0

msg_error_prefix du "ERROR: ", 0
msg_status du " status=", 0
msg_newline du 13, 10, 0

; the post ExitBootServices path talks to COM1 only, so its banner must be
; distinct from the UEFI console one in a serial log
msg_post_exit_prefix du "K64-LOADER-FAIL: ", 0

msg_error_disable_watchdog du "failed to disable the watchdog timer", 0
msg_error_open_file        du "can't open ", 0
msg_error_read_file        du "can't read ", 0
msg_error_short_read       du "short read, bytes read: ", 0
msg_error_efi_lip_handle   du "handle has no loaded image protocol", 0
msg_error_lip_dev_sfsp     du "device handle has no simple file system protocol", 0
msg_error_sfsp_openvolume  du "OpenVolume failed", 0
msg_error_get_file_size    du "GetInfo failed", 0
msg_error_file_info_size   du "EFI_FILE_INFO is smaller than expected", 0
msg_error_allocate_pool    du "AllocatePool failed", 0
msg_error_allocate_pages   du "AllocatePages failed", 0
msg_error_free_pages       du "FreePages failed", 0
msg_error_get_memory_map   du "GetMemoryMap failed", 0
msg_error_memory_map_too_big    du "memory map does not fit the buffer limit", 0
msg_error_memory_map_desc_size  du "GetMemoryMap returned a bad descriptor size", 0
msg_error_kernel_file_empty     du "kernel file is empty", 0
msg_error_kernel_file_too_big   du "kernel file is too big for the page table pool", 0
msg_error_kernel_file_too_small du "kernel file is smaller than its header", 0
msg_error_kernel_magic          du "kernel file has no KERNEL64 magic", 0
msg_error_kernel_entry          du "kernel entry offset is out of range", 0
msg_error_kernel_stack          du "kernel stack size is out of range", 0
msg_error_kernel_too_many_pages du "kernel image and stack need too many pages", 0
msg_error_page_table_pool       du "page table pool overrun", 0
msg_error_image_not_mapped      du "loader image is above the identity map", 0
msg_error_stack_not_mapped      du "loader stack is above the identity map", 0
msg_error_memmap_not_mapped     du "memory map copy is above the identity map", 0
msg_error_exit_boot_services    du "ExitBootServices failed", 0

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; not discardable on purpose: the page tables live here and the CR3 switch
; depends on them, so their pages must stay backed for good
section '.bss' data readable writeable
memory_map_addr         dq ?
memory_map_size         dq ?
memory_map_desc_size    dq ?
memory_map_desc_version dq ?
memory_map_desc_count   dq ?
memory_map_key          dq ?
memory_map_pages        dq ?

; get_memory_map reports failures through these instead of printing, so the
; caller can pick the UEFI console or the COM1 only path
memory_map_error_msg    dq ?
memory_map_error_status dq ?

; BOOT_SERVICES_*, set right before the first ExitBootServices call
boot_services_state     dq ?

volume_root             dq ?
file_handle             dq ? ; for load_file, get_file_size
info_ptr                dq ? ; for get_file_size
info_size               dq ? ; for get_file_size

lf_root                 dq ?
lf_name                 dq ?
lf_buffer               dq ?
lf_expected             dq ?
lf_read_size            dq ?

kernel_header_buf        KERNEL64_HEADER
kernel_file_size         dq ?
kernel_file_pages        dq ?
kernel_stack_pages       dq ?
kernel_total_pages       dq ?
kernel_phys_base         dq ?
kernel_phys_end          dq ?
kernel_stack_phys_start  dq ?
kernel_entry_offset      dq ?

pt_phys                  dq ? ; build_page_tables: physical base of the image
pt_pages                 dq ? ; build_page_tables: image + stack pages

bootinfo                 K64BOOTINFO

align PAGE_SIZE
page_tables:
table_p4          rq 512
table_p3_ident    rq 512
table_p2_ident    rq 512*IDENTITY_MAP_2M_TABLES
table_p3_kernel   rq 512
table_p2_kernel   rq 512
table_p1_pool     rq 512*P1_POOL_PAGES
page_tables_end:

section '.reloc' fixups data discardable
