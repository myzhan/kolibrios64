;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Stage-2 memory management: BootInfo parsing and the page maps.
;;
;; Everything here runs once, from pmm_init, before any page is handed
;; out. The parser is strict on purpose: at this point the only trusted
;; description of the machine is the 208 byte BootInfo record plus the
;; UEFI memory map copy it points to, and both are still below 4 GiB so
;; they are reachable through the loader identity map.
;;
;; Errors:
;;  EINVAL    a field or a descriptor violates the handover contract;
;;  ENOTSUP   the CPU reports an unusable physical width, or a page table
;;            page sits outside the identity mapped 4 GiB, or more than
;;            MM_CR3_PIN_MAX table pages would have to be pinned.
;;

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; validate the 64 byte INIT_PARAMS block
; in
; rcx = INIT_PARAMS
; out
; eax = status
; destroys rax, rcx, rdx
mm_params_validate:
        test    rcx, rcx
        jz      .invalid
        cmp     dword [rcx + MM_INIT_PARAMS.version], 1
        jne     .invalid
        cmp     dword [rcx + MM_INIT_PARAMS.size], MM_INIT_PARAMS_size
        jne     .invalid
        cmp     qword [rcx + MM_INIT_PARAMS.flags], 0
        jne     .invalid
        cmp     qword [rcx + MM_INIT_PARAMS.reserved1], 0
        jne     .invalid
        cmp     qword [rcx + MM_INIT_PARAMS.reserved2], 0
        jne     .invalid

        ; CR3 must be a page aligned, identity mapped physical address
        mov     rax, qword [rcx + MM_INIT_PARAMS.cr3_phys]
        test    rax, MM_PAGE_SIZE-1
        jnz     .invalid
        test    rax, rax
        jz      .invalid
        mov     rdx, MM_PHYS_LIMIT
        cmp     rax, rdx
        jae     .invalid

        ; this stage reads the BootInfo through the identity map, so the
        ; virtual and the physical address must be the same low 4 GiB page
        mov     rax, qword [rcx + MM_INIT_PARAMS.bootinfo_va]
        mov     rdx, qword [rcx + MM_INIT_PARAMS.bootinfo_phys]
        cmp     rax, rdx
        jne     .invalid
        test    rax, rax
        jz      .invalid
        test    rax, 7
        jnz     .invalid
        mov     rdx, rax
        add     rdx, MM_BOOTINFO_SIZE
        jc      .invalid
        mov     rax, MM_PHYS_LIMIT
        cmp     rdx, rax
        ja      .invalid

        ; the stack top is a kernel virtual address, 16 byte aligned
        mov     rax, qword [rcx + MM_INIT_PARAMS.stack_top_va]
        test    rax, rax
        jz      .invalid
        test    rax, 15
        jnz     .invalid
        cmp     rax, MM_KERNEL_BASE
        jb      .invalid
        xor     eax, eax
        ret
.invalid:
        mov     eax, MM_EINVAL
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; read the CPU physical address width and derive the page address mask
; out
; eax = status
; destroys rax, rbx, rcx, rdx
mm_cpu_phys_bits:
        push    rbx
        mov     eax, 0x80000000
        cpuid
        cmp     eax, 0x80000008
        jb      .unsupported
        mov     eax, 0x80000008
        cpuid
        movzx   edx, al
        cmp     edx, 36
        jb      .unsupported
        cmp     edx, 52
        ja      .unsupported
        mov     dword [mm_vmm_state + MM_VMM_STATE.phys_bits], edx
        ; pa_mask = ((1 << bits) - 1) and not 0xFFF
        mov     ecx, edx
        mov     rax, 1
        shl     rax, cl
        dec     rax
        and     rax, -MM_PAGE_SIZE
        mov     qword [mm_vmm_state + MM_VMM_STATE.pa_mask], rax
        pop     rbx
        mov     eax, MM_OK
        ret
.unsupported:
        pop     rbx
        mov     eax, MM_ENOTSUP
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; whether a descriptor may feed the allocator
; in
; rcx = type, rdx = attributes
; out
; eax = 1 when conventional WB memory without a reserved attribute
; destroys rax, r8
mm_desc_is_eligible:
        xor     eax, eax
        cmp     ecx, MM_EFI_CONVENTIONAL_MEMORY
        jne     .done
        mov     r8, MM_EFI_MEMORY_WB
        test    rdx, r8
        jz      .done
        mov     r8, MM_EFI_MEMORY_RESERVED_ATTR
        test    rdx, r8
        jnz     .done
        mov     eax, 1
.done:
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; pin every page a physical range touches
; in
; rcx = start, page aligned
; rdx = end, exclusive, byte granular
; out
; nothing
; destroys rax, rcx, rdx, r8
;
; The boot records are byte sized objects: the 208 byte BootInfo and the
; 160 byte memory map do not end on a page boundary, and their last page
; has to stay out of the allocator just like the first one.
mm_pin_range:
        mov     r8, MM_PHYS_LIMIT
        cmp     rdx, r8
        jbe     .clipped
        mov     rdx, r8
.clipped:
        cmp     rcx, rdx
        jae     .done
        shr     rcx, MM_PAGE_SHIFT
        ; end is exclusive, so round it up: a partial page still counts
        lea     rdx, [rdx + MM_PAGE_SIZE - 1]
        shr     rdx, MM_PAGE_SHIFT
        ; rounding must never step past the last frame of the bitmap, or a
        ; range that ends exactly on 4 GiB would set a bit behind it
        cmp     rdx, MM_PFN_COUNT
        jbe     .loop
        mov     rdx, MM_PFN_COUNT
.loop:
        cmp     rcx, rdx
        jae     .done
        bts     qword [mm_bitmap_pinned], rcx
        inc     rcx
        jmp     .loop
.done:
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; remember and pin one page table page
; in
; rcx = physical address
; out
; eax = MM_OK, MM_EALREADY when the page was already visited,
;       MM_ENOTSUP when it cannot be reached through the identity map
; destroys rax, rcx, rdx, r8, r9
mm_pin_table_page:
        mov     rax, MM_PHYS_LIMIT
        cmp     rcx, rax
        jae     .outside
        test    rcx, MM_PAGE_SIZE-1
        jnz     .outside
        xor     edx, edx
        mov     r8, qword [mm_table_seen_count]
.known:
        cmp     rdx, r8
        jae     .new
        mov     r9, qword [mm_table_seen + rdx*8]
        cmp     r9, rcx
        je      .already
        inc     rdx
        jmp     .known
.new:
        cmp     r8, MM_CR3_PIN_MAX
        jae     .too_many
        mov     qword [mm_table_seen + r8*8], rcx
        inc     r8
        mov     qword [mm_table_seen_count], r8
        shr     rcx, MM_PAGE_SHIFT
        bts     qword [mm_bitmap_pinned], rcx
        mov     eax, MM_OK
        ret
.already:
        mov     eax, MM_EALREADY
        ret
.outside:
        mov     eax, MM_ENOTSUP
        ret
.too_many:
        mov     eax, MM_ENOTSUP
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; pin the page table pages reachable from CR3, identifying large pages
; and cycles: large page entries stop the descent, a table seen twice is
; skipped instead of walked again.
; in
; rcx = CR3 physical address
; out
; eax = status
mm_pin_tables:
        mm_enter
        cld
        mov     r15, rcx                ; PML4 physical address
        call    mm_pin_table_page
        test    eax, eax
        jnz     .done                   ; the root itself must be new

        xor     r13d, r13d
.level4:
        cmp     r13, 512
        jae     .ok
        mov     rsi, r13
        shl     rsi, 3
        add     rsi, r15
        mov     rdi, qword [rsi]
        test    dil, MM_PTE_P
        jz      .next4
        test    dil, MM_PTE_PS
        jnz     .invalid
        mov     rax, qword [mm_vmm_state + MM_VMM_STATE.pa_mask]
        and     rdi, rax
        mov     r14, rdi                ; PDPT page
        mov     rcx, r14
        call    mm_pin_table_page
        cmp     eax, MM_EALREADY
        je      .next4
        test    eax, eax
        jnz     .done

        xor     ebx, ebx
.level3:
        cmp     rbx, 512
        jae     .next4
        mov     rsi, rbx
        shl     rsi, 3
        add     rsi, r14
        mov     rdi, qword [rsi]
        test    dil, MM_PTE_P
        jz      .next3
        test    dil, MM_PTE_PS
        jnz     .next3                  ; 1 GiB leaf, not a table page
        mov     rax, qword [mm_vmm_state + MM_VMM_STATE.pa_mask]
        and     rdi, rax
        mov     r12, rdi                ; PD page
        mov     rcx, r12
        call    mm_pin_table_page
        cmp     eax, MM_EALREADY
        je      .next3
        test    eax, eax
        jnz     .done

        xor     ebp, ebp
.level2:
        cmp     rbp, 512
        jae     .next3
        mov     rsi, rbp
        shl     rsi, 3
        add     rsi, r12
        mov     rdi, qword [rsi]
        test    dil, MM_PTE_P
        jz      .next2
        test    dil, MM_PTE_PS
        jnz     .next2                  ; 2 MiB leaf, not a table page
        mov     rax, qword [mm_vmm_state + MM_VMM_STATE.pa_mask]
        and     rdi, rax
        mov     rcx, rdi
        call    mm_pin_table_page
        cmp     eax, MM_EALREADY
        je      .next2
        test    eax, eax
        jnz     .done
.next2:
        inc     ebp
        jmp     .level2
.next3:
        inc     ebx
        jmp     .level3
.next4:
        inc     r13
        jmp     .level4
.ok:
        xor     eax, eax
        jmp     .done
.invalid:
        mov     eax, MM_EINVAL
.done:
        mm_leave
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; validate every descriptor and store the sorted (start, end) list
; in
; rbx = memory map physical address
; rbp = descriptor count
; r12d = descriptor stride
; out
; eax = status
mm_memmap_validate:
        mm_enter
        cld
        mov     r13, rbx                ; descriptor base
        mov     r14, rbp                ; descriptor count
        mov     r15, r12                ; stride
        mov     rdi, MM_PHYS_LIMIT
        mov     rax, 1
        mov     ecx, dword [mm_vmm_state + MM_VMM_STATE.phys_bits]
        shl     rax, cl
        mov     r12, rax                ; physical address limit

        xor     ebp, ebp
.desc_loop:
        cmp     rbp, r14
        jae     .descs_done
        mov     rax, rbp
        mul     r15
        mov     rbx, r13
        add     rbx, rax
        mov     rax, qword [rbx + MM_MEM_DESC.pages]
        test    rax, rax
        jz      .invalid
        mov     rcx, qword [rbx + MM_MEM_DESC.phys_start]
        test    rcx, MM_PAGE_SIZE-1
        jnz     .invalid
        mov     rdx, MM_PAGE_SIZE
        mul     rdx                     ; RDX:RAX = pages * 4096
        test    rdx, rdx
        jnz     .invalid                ; end point overflow
        add     rax, rcx
        jc      .invalid
        mov     rdx, rax                ; descriptor end, exclusive
        cmp     rdx, r12
        ja      .invalid                ; beyond the CPU physical width
        ; record the pair for the overlap check
        mov     rax, rbp
        shl     rax, 4
        mov     rbx, mm_desc_scratch
        add     rbx, rax
        mov     qword [rbx], rcx
        mov     qword [rbx + 8], rdx
        inc     ebp
        jmp     .desc_loop
.descs_done:

        ; insertion sort by start address, the map arrives unordered
        mov     rcx, r14
        mov     r8, 1
.outer:
        cmp     r8, rcx
        jae     .sorted
        mov     rax, r8
        shl     rax, 4
        mov     rbx, mm_desc_scratch
        add     rbx, rax
        mov     r9, qword [rbx]
        mov     r10, qword [rbx + 8]
        mov     rdx, r8
.inner:
        test    rdx, rdx
        jz      .place
        mov     rax, rdx
        dec     rax
        shl     rax, 4
        mov     rbx, mm_desc_scratch
        add     rbx, rax
        cmp     qword [rbx], r9
        jbe     .place
        mov     r11, qword [rbx]
        mov     rax, qword [rbx + 8]
        mov     qword [rbx + 16], r11
        mov     qword [rbx + 24], rax
        dec     rdx
        jmp     .inner
.place:
        mov     rax, rdx
        shl     rax, 4
        mov     rbx, mm_desc_scratch
        add     rbx, rax
        mov     qword [rbx], r9
        mov     qword [rbx + 8], r10
        inc     r8
        jmp     .outer
.sorted:

        ; no two descriptors may describe the same byte
        mov     r8, 1
.overlap:
        cmp     r8, r14
        jae     .ok
        mov     rax, r8
        dec     rax
        shl     rax, 4
        mov     rbx, mm_desc_scratch
        add     rbx, rax
        mov     rdx, qword [rbx + 8]
        mov     rcx, r8
        shl     rcx, 4
        mov     rbx, mm_desc_scratch
        add     rbx, rcx
        cmp     rdx, qword [rbx]
        ja      .invalid
        inc     r8
        jmp     .overlap
.ok:
        xor     eax, eax
        jmp     .done
.invalid:
        mov     eax, MM_EINVAL
.done:
        mm_leave
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; mark the eligible page frames, clipping every range to the low 4 GiB
; in
; rbx = memory map physical address
; rbp = descriptor count
; r12 = descriptor stride
; destroys every volatile register
mm_memmap_mark:
        mm_enter
        cld
        mov     r13, rbx
        mov     r14, rbp
        mov     r15, r12
        mov     r12, MM_PHYS_LIMIT
        xor     ebp, ebp
.desc_loop:
        cmp     rbp, r14
        jae     .done
        mov     rax, rbp
        mul     r15
        mov     rbx, r13
        add     rbx, rax
        mov     ecx, dword [rbx + MM_MEM_DESC.type]
        mov     rdx, qword [rbx + MM_MEM_DESC.attributes]
        call    mm_desc_is_eligible
        test    eax, eax
        jz      .next
        mov     rcx, qword [rbx + MM_MEM_DESC.phys_start]
        cmp     rcx, r12
        jae     .next
        mov     rax, qword [rbx + MM_MEM_DESC.pages]
        shl     rax, MM_PAGE_SHIFT
        add     rax, rcx                ; end, overflow free by validation
        cmp     rax, r12
        jbe     .clipped
        mov     rax, r12
.clipped:
        shr     rcx, MM_PAGE_SHIFT
        shr     rax, MM_PAGE_SHIFT
.mark:
        cmp     rcx, rax
        jae     .next
        bts     qword [mm_bitmap_eligible], rcx
        btr     qword [mm_bitmap_pinned], rcx
        inc     rcx
        jmp     .mark
.next:
        inc     ebp
        jmp     .desc_loop
.done:
        xor     eax, eax
        mm_leave
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; validate BootInfo and build every map; called from pmm_init
; in
; rcx = INIT_PARAMS
; out
; eax = status
mm_bootmap_build:
        mm_enter
        cld
        mov     rbx, rcx

        ; cache the handover addresses first
        mov     rax, qword [rbx + MM_INIT_PARAMS.cr3_phys]
        mov     qword [mm_boot_state + MM_BOOT_STATE.cr3_phys], rax
        mov     rax, qword [rbx + MM_INIT_PARAMS.stack_top_va]
        mov     qword [mm_boot_state + MM_BOOT_STATE.stack_top_va], rax
        mov     rdi, qword [rbx + MM_INIT_PARAMS.bootinfo_phys]
        mov     qword [mm_boot_state + MM_BOOT_STATE.bootinfo_phys], rdi

        ; signature, version and size are fixed, flags follow a mask rule
        mov     rax, qword [rdi + MM_BOOTINFO.signature]
        mov     rdx, MM_BOOTINFO_SIGNATURE
        cmp     rax, rdx
        jne     .invalid
        cmp     dword [rdi + MM_BOOTINFO.version], MM_BOOTINFO_VERSION
        jne     .invalid
        cmp     dword [rdi + MM_BOOTINFO.struct_size], MM_BOOTINFO_SIZE
        jne     .invalid
        ; flags: unknown bits are refused, the memory map is mandatory,
        ; the framebuffer feature is optional
        mov     rax, qword [rdi + MM_BOOTINFO.flags]
        mov     rdx, MM_BOOTINFO_FLAG_KNOWN
        not     rdx
        test    rax, rdx
        jnz     .invalid
        test    rax, MM_BOOTINFO_FLAG_MAP
        jz      .invalid

        ; features without an implementation must stay explicit zeros
        mov     rdx, qword [rdi + MM_BOOTINFO.rsdp_addr]
        or      rdx, qword [rdi + MM_BOOTINFO.initramfs_addr]
        or      rdx, qword [rdi + MM_BOOTINFO.initramfs_size]
        jnz     .invalid

        ; the GOP block is conditional on the framebuffer flag
        test    rax, MM_BOOTINFO_FLAG_FRAMEBUFFER
        jnz     .gop_present
        mov     rdx, qword [rdi + MM_BOOTINFO.gop_framebuffer_addr]
        or      rdx, qword [rdi + MM_BOOTINFO.gop_framebuffer_size]
        or      rdx, qword [rdi + MM_BOOTINFO.gop_width]
        or      rdx, qword [rdi + MM_BOOTINFO.gop_height]
        or      rdx, qword [rdi + MM_BOOTINFO.gop_pixel_format]
        or      rdx, qword [rdi + MM_BOOTINFO.gop_pixels_per_line]
        jnz     .invalid
        jmp     .gop_done
.gop_present:
        ; the framebuffer may sit outside every memory map descriptor,
        ; so no physical range or overlap check applies to it
        mov     rdx, qword [rdi + MM_BOOTINFO.gop_framebuffer_addr]
        test    rdx, rdx
        jz      .invalid
        mov     rdx, qword [rdi + MM_BOOTINFO.gop_framebuffer_size]
        test    rdx, rdx
        jz      .invalid
        mov     r8d, dword [rdi + MM_BOOTINFO.gop_width]
        test    r8d, r8d
        jz      .invalid
        mov     r9d, dword [rdi + MM_BOOTINFO.gop_height]
        test    r9d, r9d
        jz      .invalid
        mov     r10d, dword [rdi + MM_BOOTINFO.gop_pixels_per_line]
        cmp     r10d, r8d
        jb      .invalid                ; pitch must cover the width
        mov     edx, dword [rdi + MM_BOOTINFO.gop_pixel_format]
        cmp     rdx, MM_GOP_PIXEL_FORMAT_MAX
        ja      .invalid                ; BltOnly has no linear framebuffer
        ; size >= pixels_per_line * height * 4, the 32bpp budget
        mov     rax, r10
        mul     r9                      ; RDX:RAX = pitch * height
        test    rdx, rdx
        jnz     .invalid
        mov     rcx, 4
        mul     rcx
        test    rdx, rdx
        jnz     .invalid
        cmp     qword [rdi + MM_BOOTINFO.gop_framebuffer_size], rax
        jb      .invalid
.gop_done:

        ; and the reserved area must be zero in version 1
        xor     ecx, ecx
.reserved_loop:
        cmp     ecx, MM_BOOTINFO_RESERVED_QWORDS
        jae     .reserved_done
        mov     rax, qword [rdi + MM_BOOTINFO.reserved + rcx*8]
        test    rax, rax
        jnz     .invalid
        inc     ecx
        jmp     .reserved_loop
.reserved_done:

        ; the memory map copy must live below 4 GiB and be small enough
        mov     rsi, qword [rdi + MM_BOOTINFO.memmap_addr]
        mov     r14, qword [rdi + MM_BOOTINFO.memmap_size]
        test    rsi, rsi
        jz      .invalid
        test    r14, r14
        jz      .invalid
        mov     rax, MM_PHYS_LIMIT
        cmp     rsi, rax
        jae     .invalid
        cmp     r14, MM_MAP_MAX_SIZE
        ja      .invalid
        mov     rdx, rsi
        add     rdx, r14
        jc      .invalid
        cmp     rdx, rax
        ja      .invalid

        ; stride and count must describe exactly the buffer
        mov     r15d, dword [rdi + MM_BOOTINFO.memmap_desc_size]
        cmp     r15d, MM_EFI_DESC_MIN_SIZE
        jb      .invalid
        test    r15d, 7
        jnz     .invalid
        cmp     dword [rdi + MM_BOOTINFO.memmap_desc_version], 1
        jne     .invalid
        mov     rbp, qword [rdi + MM_BOOTINFO.memmap_desc_count]
        test    rbp, rbp
        jz      .invalid
        cmp     rbp, MM_MAP_MAX_DESCS
        ja      .invalid
        mov     rax, rbp
        mul     r15
        test    rdx, rdx
        jnz     .invalid
        cmp     rax, r14
        jne     .invalid

        ; keep the map description, the validators below reuse it
        mov     qword [mm_boot_state + MM_BOOT_STATE.memmap_phys], rsi
        mov     qword [mm_boot_state + MM_BOOT_STATE.memmap_size], r14
        mov     dword [mm_boot_state + MM_BOOT_STATE.desc_stride], r15d
        mov     dword [mm_boot_state + MM_BOOT_STATE.desc_count], ebp

        ; kernel image and stack ranges; the kernel may sit above 4 GiB,
        ; only the pages this allocator hands out are limited to the low
        ; 4 GiB, so the range is checked against the 32 MiB loader limit
        mov     rax, qword [rdi + MM_BOOTINFO.kernel_phys_start]
        mov     rdx, qword [rdi + MM_BOOTINFO.kernel_phys_end]
        test    rax, rax
        jz      .invalid
        test    rax, MM_PAGE_SIZE-1
        jnz     .invalid
        test    rdx, MM_PAGE_SIZE-1
        jnz     .invalid
        cmp     rdx, rax
        jbe     .invalid
        mov     rcx, rdx
        sub     rcx, rax
        cmp     rcx, MM_IMAGE_MAX
        ja      .invalid
        mov     qword [mm_boot_state + MM_BOOT_STATE.kernel_start], rax
        mov     qword [mm_boot_state + MM_BOOT_STATE.kernel_end], rdx

        mov     rax, qword [rdi + MM_BOOTINFO.stack_phys_start]
        mov     rdx, qword [rdi + MM_BOOTINFO.stack_phys_end]
        test    rax, MM_PAGE_SIZE-1
        jnz     .invalid
        test    rdx, MM_PAGE_SIZE-1
        jnz     .invalid
        cmp     rdx, rax
        jbe     .invalid
        mov     rcx, qword [mm_boot_state + MM_BOOT_STATE.kernel_start]
        cmp     rax, rcx
        jb      .invalid
        mov     rcx, qword [mm_boot_state + MM_BOOT_STATE.kernel_end]
        cmp     rdx, rcx
        ja      .invalid
        ; the stack ends exactly where the kernel image ends
        cmp     rdx, qword [mm_boot_state + MM_BOOT_STATE.kernel_end]
        jne     .invalid
        mov     qword [mm_boot_state + MM_BOOT_STATE.stack_start], rax
        mov     qword [mm_boot_state + MM_BOOT_STATE.stack_end], rdx

        ; the stack top is the exact conversion of the BootInfo range:
        ; KERNEL_BASE + (stack_phys_end - kernel_phys_start)
        mov     rax, qword [mm_boot_state + MM_BOOT_STATE.stack_end]
        sub     rax, qword [mm_boot_state + MM_BOOT_STATE.kernel_start]
        add     rax, MM_KERNEL_BASE
        cmp     rax, qword [mm_boot_state + MM_BOOT_STATE.stack_top_va]
        jne     .invalid

        ; descriptors: shape, end point, ordering, overlap
        mov     rbx, qword [mm_boot_state + MM_BOOT_STATE.memmap_phys]
        mov     rbp, qword [mm_boot_state + MM_BOOT_STATE.desc_count]
        mov     r12d, dword [mm_boot_state + MM_BOOT_STATE.desc_stride]
        call    mm_memmap_validate
        test    eax, eax
        jnz     .done

        ; conventional WB memory becomes the allocatable set
        mov     rbx, qword [mm_boot_state + MM_BOOT_STATE.memmap_phys]
        mov     rbp, qword [mm_boot_state + MM_BOOT_STATE.desc_count]
        mov     r12d, dword [mm_boot_state + MM_BOOT_STATE.desc_stride]
        call    mm_memmap_mark

        ; pin what this stage must never hand out
        xor     ecx, ecx                ; low 1 MiB belongs to firmware
        mov     rdx, 0x100000
        call    mm_pin_range

        mov     rcx, qword [mm_boot_state + MM_BOOT_STATE.kernel_start]
        mov     rdx, qword [mm_boot_state + MM_BOOT_STATE.kernel_end]
        call    mm_pin_range

        mov     rcx, qword [mm_boot_state + MM_BOOT_STATE.bootinfo_phys]
        mov     rdx, rcx
        and     rcx, -MM_PAGE_SIZE
        add     rdx, MM_BOOTINFO_SIZE
        call    mm_pin_range

        mov     rcx, qword [mm_boot_state + MM_BOOT_STATE.memmap_phys]
        mov     rdx, rcx
        add     rdx, qword [mm_boot_state + MM_BOOT_STATE.memmap_size]
        and     rcx, -MM_PAGE_SIZE
        call    mm_pin_range

        ; and the page tables the current CR3 can still reach
        mov     rcx, qword [mm_boot_state + MM_BOOT_STATE.cr3_phys]
        call    mm_pin_tables
        jmp     .done
.invalid:
        mov     eax, MM_EINVAL
.done:
        mm_leave
        ret
