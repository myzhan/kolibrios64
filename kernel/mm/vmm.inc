;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Stage-2 virtual memory.
;;
;; The module keeps using the CR3 the loader installed: it never builds a
;; new address space of its own. Page table pages are therefore reached
;; through the identity map, which limits every table to the low 4 GiB;
;; leaf physical addresses, on the other hand, may use the full width the
;; CPU reports.
;;
;; Public mappings live in [MM_WINDOW_BASE, MM_WINDOW_LIMIT) only, are
;; always present and supervisor, take no cache attribute, and may be
;; requested only as read-only executable or writable non-executable.
;; Missing tables are allocated offline, linked inside the freshly zeroed
;; pages, and published with a single store into the first table that
;; already existed; a failure rolls every new table page back.
;;

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; walk the current page tables
; in
; rcx = virtual address
; rdx = 64 byte result record:
;       +0  pointer to the leaf entry, 0 when nothing is mapped
;       +8  page size 0x1000 / 0x200000 / 0x40000000, 0 when unmapped
;       +16 effective RW (AND over the path), 1 or 0
;       +24 effective US (AND over the path), 1 or 0
;       +32 effective NX (OR over the path), 1 or 0
;       +40 raw leaf entry
;       +48 leaf physical address
; out
; eax = MM_OK, MM_ENOENT when a level is missing,
;       MM_ENOTSUP when a table page is outside the identity map,
;       MM_EINVAL when the PML4 entry uses the reserved PS bit
mm_vmm_resolve:
        mm_enter
        cld
        test    rdx, rdx
        jz      .fault
        mov     r15, rdx
        mov     r14, rcx
        mov     qword [r15], 0
        mov     qword [r15+8], 0
        mov     qword [r15+16], 0
        mov     qword [r15+24], 0
        mov     qword [r15+32], 0
        mov     qword [r15+40], 0
        mov     qword [r15+48], 0

        ; only canonical addresses can be walked
        mov     rax, r14
        sar     rax, 47
        add     rax, 1
        cmp     rax, 1
        ja      .invalid

        mov     r13, qword [mm_vmm_state + MM_VMM_STATE.root_phys]
        mov     r8d, 1                  ; RW accumulator
        mov     r9d, 1                  ; US accumulator
        xor     r10d, r10d              ; NX accumulator

        ; PML4 entry
        mov     rax, r14
        shr     rax, 39
        and     eax, 0x1FF
        lea     rsi, [r13 + rax*8]
        mov     rdi, qword [rsi]
        test    dil, MM_PTE_P
        jz      .missing
        mov     rax, rdi
        shr     rax, 1
        and     eax, 1
        and     r8d, eax
        mov     rax, rdi
        shr     rax, 2
        and     eax, 1
        and     r9d, eax
        mov     rax, rdi
        shr     rax, 63
        or      r10d, eax
        test    dil, MM_PTE_PS
        jnz     .invalid
        mov     rax, qword [mm_vmm_state + MM_VMM_STATE.pa_mask]
        and     rdi, rax
        mov     r13, rdi
        mov     rax, MM_PHYS_LIMIT
        cmp     r13, rax
        jae     .outside

        ; PDPT entry, a 1 GiB leaf is allowed here
        mov     rax, r14
        shr     rax, 30
        and     eax, 0x1FF
        lea     rsi, [r13 + rax*8]
        mov     rdi, qword [rsi]
        test    dil, MM_PTE_P
        jz      .missing
        mov     rax, rdi
        shr     rax, 1
        and     eax, 1
        and     r8d, eax
        mov     rax, rdi
        shr     rax, 2
        and     eax, 1
        and     r9d, eax
        mov     rax, rdi
        shr     rax, 63
        or      r10d, eax
        test    dil, MM_PTE_PS
        jnz     .leaf_1g
        mov     rax, qword [mm_vmm_state + MM_VMM_STATE.pa_mask]
        and     rdi, rax
        mov     r13, rdi
        mov     rax, MM_PHYS_LIMIT
        cmp     r13, rax
        jae     .outside

        ; PD entry, a 2 MiB leaf is allowed here
        mov     rax, r14
        shr     rax, 21
        and     eax, 0x1FF
        lea     rsi, [r13 + rax*8]
        mov     rdi, qword [rsi]
        test    dil, MM_PTE_P
        jz      .missing
        mov     rax, rdi
        shr     rax, 1
        and     eax, 1
        and     r8d, eax
        mov     rax, rdi
        shr     rax, 2
        and     eax, 1
        and     r9d, eax
        mov     rax, rdi
        shr     rax, 63
        or      r10d, eax
        test    dil, MM_PTE_PS
        jnz     .leaf_2m
        mov     rax, qword [mm_vmm_state + MM_VMM_STATE.pa_mask]
        and     rdi, rax
        mov     r13, rdi
        mov     rax, MM_PHYS_LIMIT
        cmp     r13, rax
        jae     .outside

        ; PT entry, always a 4 KiB leaf
        mov     rax, r14
        shr     rax, 12
        and     eax, 0x1FF
        lea     rsi, [r13 + rax*8]
        mov     rdi, qword [rsi]
        test    dil, MM_PTE_P
        jz      .missing
        mov     rax, rdi
        shr     rax, 1
        and     eax, 1
        and     r8d, eax
        mov     rax, rdi
        shr     rax, 2
        and     eax, 1
        and     r9d, eax
        mov     rax, rdi
        shr     rax, 63
        or      r10d, eax
        mov     rax, rdi
        and     rax, qword [mm_vmm_state + MM_VMM_STATE.pa_mask]
        mov     rcx, r14
        and     ecx, MM_PAGE_SIZE-1
        add     rax, rcx
        mov     qword [r15], rsi
        mov     qword [r15+8], MM_PAGE_SIZE
        mov     qword [r15+40], rdi
        mov     qword [r15+48], rax
        jmp     .accumulated

.leaf_2m:
        ; a 2 MiB leaf stores PA in bits 51:21; bit 12 is PAT, not address
        mov     rax, qword [mm_vmm_state + MM_VMM_STATE.pa_mask]
        and     rax, -0x200000
        and     rax, rdi
        mov     rcx, r14
        and     ecx, 0x1FFFFF
        add     rax, rcx
        mov     qword [r15], rsi
        mov     qword [r15+8], 0x200000
        mov     qword [r15+40], rdi
        mov     qword [r15+48], rax
        jmp     .accumulated

.leaf_1g:
        ; a 1 GiB leaf stores PA in bits 51:30; bit 12 is PAT, not address
        mov     rax, qword [mm_vmm_state + MM_VMM_STATE.pa_mask]
        and     rax, -0x40000000
        and     rax, rdi
        mov     rcx, r14
        and     ecx, 0x3FFFFFFF
        add     rax, rcx
        mov     qword [r15], rsi
        mov     qword [r15+8], 0x40000000
        mov     qword [r15+40], rdi
        mov     qword [r15+48], rax
        jmp     .accumulated

.accumulated:
        mov     qword [r15+16], r8
        mov     qword [r15+24], r9
        mov     qword [r15+32], r10
        xor     eax, eax
        jmp     .done
.missing:
        mov     eax, MM_ENOENT
        jmp     .done
.outside:
        mov     eax, MM_ENOTSUP
        jmp     .done
.invalid:
        mov     eax, MM_EINVAL
        jmp     .done
.fault:
        mov     eax, MM_EFAULT
.done:
        mm_leave
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; report one mapping
; in
; rcx = virtual address
; rdx = 40 byte query record
; out
; eax = status, rdx = 0; every failure leaves the record zeroed
vmm_query:
        mm_enter
        cld
        sub     rsp, 96                 ; 32 shadow + 64 record
        test    rdx, rdx
        jz      .fault
        mov     rbx, rdx
        mov     r12, rcx
        mov     qword [rbx], 0
        mov     qword [rbx+8], 0
        mov     qword [rbx+16], 0
        mov     qword [rbx+24], 0
        mov     qword [rbx+32], 0
        cmp     dword [mm_vmm_state + MM_VMM_STATE.ready], 0
        je      .notready

        lea     rdx, [rsp+32]
        call    mm_vmm_resolve
        test    eax, eax
        jnz     .done
        mov     rax, qword [rsp+32+48]
        mov     qword [rbx + MM_VMM_QUERY.physical_address], rax
        mov     rax, qword [rsp+32]
        mov     qword [rbx + MM_VMM_QUERY.leaf_entry_va], rax
        mov     rax, qword [rsp+32+40]
        mov     qword [rbx + MM_VMM_QUERY.raw_leaf], rax
        mov     rax, qword [rsp+32+8]
        mov     qword [rbx + MM_VMM_QUERY.page_size], rax
        mov     rdx, MM_VQ_PRESENT
        cmp     qword [rsp+32+16], 0
        je      .no_rw
        or      rdx, MM_VQ_RW
.no_rw:
        cmp     qword [rsp+32+24], 0
        je      .no_us
        or      rdx, MM_VQ_US
.no_us:
        cmp     qword [rsp+32+32], 0
        je      .no_nx
        mov     rcx, MM_VQ_NX
        or      rdx, rcx
.no_nx:
        mov     qword [rbx + MM_VMM_QUERY.effective_flags], rdx
        xor     eax, eax
        jmp     .done
.notready:
        mov     eax, MM_ENOTREADY
        jmp     .done
.fault:
        mov     eax, MM_EFAULT
.done:
        xor     edx, edx
        add     rsp, 96
        mm_leave
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; map one 4 KiB page inside the public window
; in
; rcx = virtual address, rdx = physical address, r8 = MM_MAP_RW/MM_MAP_NX
; out
; eax = status, rdx = 0
; note
; the physical page must have been handed out by pmm_alloc_page and
; must not be pinned; anything else is refused with EPERM
vmm_map_page:
        mm_enter
        cld
        sub     rsp, 96                 ; 32 shadow + publish/build state
        mov     r15, rcx
        mov     r14, rdx
        mov     r13, r8
        mov     qword [rsp+32], 0       ; publish point
        mov     qword [rsp+40], 0       ; number of new tables

        cmp     dword [mm_vmm_state + MM_VMM_STATE.ready], 0
        je      .notready

        ; the public window only
        mov     rax, qword [mm_vmm_state + MM_VMM_STATE.window_base]
        cmp     r15, rax
        jb      .permitted
        mov     rax, qword [mm_vmm_state + MM_VMM_STATE.window_limit]
        cmp     r15, rax
        jae     .permitted
        test    r15, MM_PAGE_SIZE-1
        jnz     .invalid

        ; a page aligned physical address
        test    r14, MM_PAGE_SIZE-1
        jnz     .invalid

        ; only RW and NX, and never writable plus executable
        mov     rax, r13
        mov     rdx, MM_MAP_ALLOWED
        not     rdx
        test    rax, rdx
        jnz     .invalid
        test    rax, MM_MAP_RW
        jz      .flags_ok
        mov     rcx, MM_MAP_NX
        test    rax, rcx
        jz      .invalid
.flags_ok:

        ; ownership: allocated by the public allocator and not pinned
        mov     rbx, r14
        shr     rbx, MM_PAGE_SHIFT
        cmp     rbx, MM_PFN_COUNT
        jae     .permitted
        bt      qword [mm_bitmap_allocated], rbx
        jnc     .permitted
        bt      qword [mm_bitmap_pinned], rbx
        jc      .permitted

        ; the address must also fit the CPU physical width
        mov     rax, qword [mm_vmm_state + MM_VMM_STATE.pa_mask]
        not     rax
        test    r14, rax
        jnz     .overflow

        mov     rbx, qword [mm_vmm_state + MM_VMM_STATE.root_phys]
        mov     r12, 39
.level_loop:
        mov     rax, r15
        mov     ecx, r12d
        shr     rax, cl
        and     eax, 0x1FF
        lea     rsi, [rbx + rax*8]
        mov     rdi, qword [rsi]
        test    dil, MM_PTE_P
        jnz     .descend

        ; offline build: allocate, record, link inside the new pages only
        call    mm_pmm_alloc_table
        test    eax, eax
        jnz     .rollback_nomem
        cmp     qword [rsp+40], 0
        jne     .link_now
        mov     qword [rsp+32], rsi
        jmp     .record
.link_now:
        mov     rax, rdx
        or      rax, MM_PTE_P or MM_PTE_RW
        mov     qword [rsi], rax
.record:
        mov     rax, qword [rsp+40]
        mov     qword [rsp+48 + rax*8], rdx
        inc     rax
        mov     qword [rsp+40], rax
        mov     rbx, rdx
        jmp     .next_level
.descend:
        test    dil, MM_PTE_PS
        jnz     .rollback_notsup
        mov     rax, qword [mm_vmm_state + MM_VMM_STATE.pa_mask]
        and     rdi, rax
        mov     rbx, rdi
        mov     rax, MM_PHYS_LIMIT
        cmp     rbx, rax
        jae     .rollback_notsup
.next_level:
        cmp     r12, 21
        je      .leaf
        sub     r12, 9
        jmp     .level_loop

.leaf:
        mov     rax, r15
        shr     rax, 12
        and     eax, 0x1FF
        lea     rsi, [rbx + rax*8]
        mov     rdi, qword [rsi]
        test    dil, MM_PTE_P
        jnz     .rollback_exists
        mov     rax, r14
        or      rax, MM_PTE_P
        or      rax, r13
        mov     qword [rsi], rax

        ; atomic publish of the whole new chain
        mov     rcx, qword [rsp+40]
        test    rcx, rcx
        jz      .published
        mov     rsi, qword [rsp+32]
        mov     rax, qword [rsp+48]
        or      rax, MM_PTE_P or MM_PTE_RW
        mov     qword [rsi], rax
.published:
        mov     rax, qword [rsp+40]
        add     qword [mm_vmm_state + MM_VMM_STATE.table_allocs], rax
        invlpg  [r15]
        xor     eax, eax
        jmp     .done

.rollback_exists:
        mov     r12d, MM_EEXIST
        jmp     .rollback
.rollback_notsup:
        mov     r12d, MM_ENOTSUP
        jmp     .rollback
.rollback_nomem:
        mov     r12d, MM_ENOMEM
.rollback:
        mov     rax, qword [rsp+40]
        add     qword [mm_vmm_state + MM_VMM_STATE.table_drops], rax
        mov     rcx, qword [rsp+40]
.drop_loop:
        test    rcx, rcx
        jz      .dropped
        dec     rcx
        mov     rbx, rcx
        mov     rcx, qword [rsp+48 + rbx*8]
        call    mm_pmm_drop_table
        mov     rcx, rbx
        jmp     .drop_loop
.dropped:
        mov     eax, r12d
        jmp     .done
.permitted:
        mov     eax, MM_EPERM
        jmp     .done
.invalid:
        mov     eax, MM_EINVAL
        jmp     .done
.overflow:
        mov     eax, MM_EOVERFLOW
        jmp     .done
.notready:
        mov     eax, MM_ENOTREADY
.done:
        xor     edx, edx
        add     rsp, 96
        mm_leave
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; remove one 4 KiB mapping from the public window
; in
; rcx = virtual address
; out
; eax = status, rdx = the unmapped physical address, 0 on failure;
;       the physical page is not released
vmm_unmap_page:
        mm_enter
        cld
        sub     rsp, 96                 ; 32 shadow + 64 record
        mov     r12, rcx
        cmp     dword [mm_vmm_state + MM_VMM_STATE.ready], 0
        je      .notready
        mov     rax, qword [mm_vmm_state + MM_VMM_STATE.window_base]
        cmp     r12, rax
        jb      .permitted
        mov     rax, qword [mm_vmm_state + MM_VMM_STATE.window_limit]
        cmp     r12, rax
        jae     .permitted
        test    r12, MM_PAGE_SIZE-1
        jnz     .invalid

        mov     rcx, r12
        lea     rdx, [rsp+32]
        call    mm_vmm_resolve
        test    eax, eax
        jnz     .fail
        cmp     qword [rsp+32+8], MM_PAGE_SIZE
        jne     .notsup
        mov     rsi, qword [rsp+32]
        mov     qword [rsi], 0
        invlpg  [r12]
        mov     rdx, qword [rsp+32+48]  ; the released physical address
        xor     eax, eax
        jmp     .done
.notsup:
        mov     eax, MM_ENOTSUP
        jmp     .fail
.permitted:
        mov     eax, MM_EPERM
        jmp     .fail
.invalid:
        mov     eax, MM_EINVAL
        jmp     .fail
.notready:
        mov     eax, MM_ENOTREADY
.fail:
        xor     edx, edx
.done:
        add     rsp, 96
        mm_leave
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; map one 4 KiB MMIO page inside the public window
; in
; rcx = virtual address, rdx = physical address
; out
; eax = status, rdx = 0
; note
; the leaf flags are fixed to PCD|RW|NX, there is no flags argument.
; The physical page must NOT be eligible: conventional memory belongs
; to pmm and has to go through vmm_map_page, MMIO and reserved pages
; are exempt from the ownership check in the opposite direction. The
; heap range and the core test PML4 slot are refused with EPERM, an
; existing leaf reports EALREADY. Table pages still come from
; mm_pmm_alloc_table so the pin/accounting invariants stay untouched.
vmm_map_mmio:
        mm_enter
        cld
        sub     rsp, 96                 ; 32 shadow + publish/build state
        mov     r15, rcx
        mov     r14, rdx
        mov     qword [rsp+32], 0       ; publish point
        mov     qword [rsp+40], 0       ; number of new tables

        cmp     dword [mm_vmm_state + MM_VMM_STATE.ready], 0
        je      .notready

        ; the public window only
        mov     rax, qword [mm_vmm_state + MM_VMM_STATE.window_base]
        cmp     r15, rax
        jb      .permitted
        mov     rax, qword [mm_vmm_state + MM_VMM_STATE.window_limit]
        cmp     r15, rax
        jae     .permitted

        ; never inside the early heap range
        mov     rax, MM_HEAP_LIMIT
        cmp     r15, rax
        jb      .permitted

        ; never inside the core test PML4 slot
        mov     rax, r15
        shr     rax, 39
        and     eax, 0x1FF
        cmp     eax, MM_CORE_TEST_SLOT
        je      .permitted

        ; both addresses page aligned
        test    r15, MM_PAGE_SIZE-1
        jnz     .invalid
        test    r14, MM_PAGE_SIZE-1
        jnz     .invalid

        ; reverse ownership: an eligible page is conventional memory and
        ; must go through vmm_map_page, only non eligible pages pass here
        mov     rbx, r14
        shr     rbx, MM_PAGE_SHIFT
        cmp     rbx, MM_PFN_COUNT
        jae     .mmio_ok
        bt      qword [mm_bitmap_eligible], rbx
        jc      .permitted
.mmio_ok:

        ; the address must also fit the CPU physical width
        mov     rax, qword [mm_vmm_state + MM_VMM_STATE.pa_mask]
        not     rax
        test    r14, rax
        jnz     .overflow

        mov     rbx, qword [mm_vmm_state + MM_VMM_STATE.root_phys]
        mov     r12, 39
.level_loop:
        mov     rax, r15
        mov     ecx, r12d
        shr     rax, cl
        and     eax, 0x1FF
        lea     rsi, [rbx + rax*8]
        mov     rdi, qword [rsi]
        test    dil, MM_PTE_P
        jnz     .descend

        ; offline build: allocate, record, link inside the new pages only
        call    mm_pmm_alloc_table
        test    eax, eax
        jnz     .rollback_nomem
        cmp     qword [rsp+40], 0
        jne     .link_now
        mov     qword [rsp+32], rsi
        jmp     .record
.link_now:
        mov     rax, rdx
        or      rax, MM_PTE_P or MM_PTE_RW
        mov     qword [rsi], rax
.record:
        mov     rax, qword [rsp+40]
        mov     qword [rsp+48 + rax*8], rdx
        inc     rax
        mov     qword [rsp+40], rax
        mov     rbx, rdx
        jmp     .next_level
.descend:
        test    dil, MM_PTE_PS
        jnz     .rollback_notsup
        mov     rax, qword [mm_vmm_state + MM_VMM_STATE.pa_mask]
        and     rdi, rax
        mov     rbx, rdi
        mov     rax, MM_PHYS_LIMIT
        cmp     rbx, rax
        jae     .rollback_notsup
.next_level:
        cmp     r12, 21
        je      .leaf
        sub     r12, 9
        jmp     .level_loop

.leaf:
        mov     rax, r15
        shr     rax, 12
        and     eax, 0x1FF
        lea     rsi, [rbx + rax*8]
        bt      qword [rsi], 0
        jc      .rollback_already
        mov     rax, r14
        or      rax, MM_PTE_P or MM_PTE_RW or MM_PTE_PCD
        mov     rcx, MM_PTE_NX
        or      rax, rcx
        mov     qword [rsi], rax

        ; atomic publish of the whole new chain
        mov     rcx, qword [rsp+40]
        test    rcx, rcx
        jz      .published
        mov     rsi, qword [rsp+32]
        mov     rax, qword [rsp+48]
        or      rax, MM_PTE_P or MM_PTE_RW
        mov     qword [rsi], rax
.published:
        mov     rax, qword [rsp+40]
        add     qword [mm_vmm_state + MM_VMM_STATE.table_allocs], rax
        invlpg  [r15]
        xor     eax, eax
        jmp     .done

.rollback_already:
        mov     r12d, MM_EALREADY
        jmp     .rollback
.rollback_notsup:
        mov     r12d, MM_ENOTSUP
        jmp     .rollback
.rollback_nomem:
        mov     r12d, MM_ENOMEM
.rollback:
        mov     rax, qword [rsp+40]
        add     qword [mm_vmm_state + MM_VMM_STATE.table_drops], rax
        mov     rcx, qword [rsp+40]
.drop_loop:
        test    rcx, rcx
        jz      .dropped
        dec     rcx
        mov     rbx, rcx
        mov     rcx, qword [rsp+48 + rbx*8]
        call    mm_pmm_drop_table
        mov     rcx, rbx
        jmp     .drop_loop
.dropped:
        mov     eax, r12d
        jmp     .done
.permitted:
        mov     eax, MM_EPERM
        jmp     .done
.invalid:
        mov     eax, MM_EINVAL
        jmp     .done
.overflow:
        mov     eax, MM_EOVERFLOW
        jmp     .done
.notready:
        mov     eax, MM_ENOTREADY
.done:
        xor     edx, edx
        add     rsp, 96
        mm_leave
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; initialise virtual memory on the loader CR3
; in
; rcx = INIT_PARAMS
; out
; eax = status, rdx = the current root physical address, 0 on failure
; note
; the root is read from the actual CR3 first and must match both the
; init parameters and the cached boot state; the CPU must run four
; level paging without PCID; every PML4 slot of the managed window must
; be empty; without NX support nothing is changed at all
vmm_init:
        mm_enter
        cld
        sub     rsp, 96                 ; 32 shadow + 64 record
        mov     rbx, rcx
        cmp     qword [mm_pmm_state + MM_PMM_STATE.ready], 0
        je      .notready
        cmp     dword [mm_vmm_state + MM_VMM_STATE.ready], 0
        jne     .already

        call    mm_params_validate
        test    eax, eax
        jnz     .fail

        ; the root is the CR3 the CPU is actually using right now
        mov     rax, cr3
        and     rax, -MM_PAGE_SIZE
        cmp     rax, qword [rbx + MM_INIT_PARAMS.cr3_phys]
        jne     .invalid
        cmp     rax, qword [mm_boot_state + MM_BOOT_STATE.cr3_phys]
        jne     .invalid
        mov     r13, rax                ; root physical address

        ; four level paging, no PCID
        mov     rax, cr4
        test    rax, 1 shl 12           ; CR4.LA57
        jnz     .notsup
        test    rax, 1 shl 17           ; CR4.PCIDE
        jnz     .notsup
        cmp     qword [mm_vmm_state + MM_VMM_STATE.pa_mask], 0
        je      .io

        ; every PML4 slot of the managed window must be empty
        mov     rsi, (MM_WINDOW_BASE shr 39) and 0x1FF
        mov     rdi, (MM_WINDOW_LIMIT shr 39) and 0x1FF
.slot_loop:
        cmp     rsi, rdi
        jae     .slots_ok
        cmp     qword [r13 + rsi*8], 0
        jne     .exists
        inc     rsi
        jmp     .slot_loop
.slots_ok:

        ; NX must be advertised by the CPU; nothing has been changed yet
        mov     eax, 0x80000000
        cpuid
        cmp     eax, 0x80000001
        jb      .notsup
        mov     eax, 0x80000001
        cpuid
        bt      edx, 20
        jnc     .notsup

        ; from here on the state may change; the walker needs the root
        mov     qword [mm_vmm_state + MM_VMM_STATE.root_phys], r13

        ; verify the whole stack range first: every page must already be
        ; a 4 KiB leaf carrying the physical address BootInfo describes
        mov     r14, qword [mm_boot_state + MM_BOOT_STATE.stack_start]
        sub     r14, qword [mm_boot_state + MM_BOOT_STATE.kernel_start]
        add     r14, MM_KERNEL_BASE     ; first stack virtual address
        mov     r15, qword [mm_boot_state + MM_BOOT_STATE.stack_end]
        sub     r15, qword [mm_boot_state + MM_BOOT_STATE.kernel_start]
        add     r15, MM_KERNEL_BASE     ; end, exclusive
.verify_loop:
        cmp     r14, r15
        jae     .verified
        mov     rcx, r14
        lea     rdx, [rsp+32]
        call    mm_vmm_resolve
        cmp     eax, MM_ENOENT
        je      .invalid_unwind
        test    eax, eax
        jnz     .unwind
        cmp     qword [rsp+32+8], MM_PAGE_SIZE
        jne     .notsup_unwind
        mov     rax, r14
        sub     rax, MM_KERNEL_BASE
        add     rax, qword [mm_boot_state + MM_BOOT_STATE.kernel_start]
        cmp     qword [rsp+32+48], rax
        jne     .io_unwind
        add     r14, MM_PAGE_SIZE
        jmp     .verify_loop
.verified:

        ; EFER.NXE
        mov     ecx, 0xC0000080
        rdmsr
        test    eax, 0x800
        jnz     .efer_ok
        or      eax, 0x800
        wrmsr
        rdmsr
        test    eax, 0x800
        jz      .io_unwind
.efer_ok:

        ; CR0.WP, so read-only leaves stay read-only in ring 0
        mov     rax, cr0
        test    rax, 0x10000
        jnz     .wp_ok
        or      rax, 0x10000
        mov     cr0, rax
        mov     rax, cr0
        test    rax, 0x10000
        jz      .io_unwind
.wp_ok:

        ; apply RW/NX to every stack leaf, one invlpg per page
        mov     r14, qword [mm_boot_state + MM_BOOT_STATE.stack_start]
        sub     r14, qword [mm_boot_state + MM_BOOT_STATE.kernel_start]
        add     r14, MM_KERNEL_BASE
.apply_loop:
        cmp     r14, r15
        jae     .applied
        mov     rcx, r14
        lea     rdx, [rsp+32]
        call    mm_vmm_resolve
        test    eax, eax
        jnz     .unwind
        mov     rsi, qword [rsp+32]
        mov     rax, MM_PTE_NX
        or      qword [rsi], rax
        or      qword [rsi], MM_PTE_RW
        invlpg  [r14]
        add     r14, MM_PAGE_SIZE
        jmp     .apply_loop
.applied:

        mov     rax, MM_WINDOW_BASE
        mov     qword [mm_vmm_state + MM_VMM_STATE.window_base], rax
        mov     rax, MM_WINDOW_LIMIT
        mov     qword [mm_vmm_state + MM_VMM_STATE.window_limit], rax
        mov     qword [mm_vmm_state + MM_VMM_STATE.table_allocs], 0
        mov     qword [mm_vmm_state + MM_VMM_STATE.table_drops], 0
        mov     dword [mm_vmm_state + MM_VMM_STATE.ready], 1
        mov     rdx, r13
        xor     eax, eax
        jmp     .done
.invalid:
        mov     eax, MM_EINVAL
        jmp     .fail
.invalid_unwind:
        mov     eax, MM_EINVAL
        jmp     .unwind
.notsup:
        mov     eax, MM_ENOTSUP
        jmp     .fail
.notsup_unwind:
        mov     eax, MM_ENOTSUP
        jmp     .unwind
.io:
        mov     eax, MM_EIO
        jmp     .fail
.io_unwind:
        mov     eax, MM_EIO
        jmp     .unwind
.exists:
        mov     eax, MM_EEXIST
        jmp     .fail
.notready:
        mov     eax, MM_ENOTREADY
        jmp     .fail
.already:
        mov     eax, MM_EALREADY
        jmp     .fail
.unwind:
        mov     qword [mm_vmm_state + MM_VMM_STATE.root_phys], 0
.fail:
        xor     edx, edx
.done:
        add     rsp, 96
        mm_leave
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; verify or apply leaf permissions over a kernel range
; in
; rcx = first virtual address, page aligned, inside the kernel mapping
; rdx = number of pages
; r8  = new flags, only MM_MAP_RW / MM_MAP_NX
; r9  = 0 to verify only, 1 to apply
; out
; eax = status, nothing is changed when the verification pass fails
mm_vmm_protect_pass:
        mm_enter
        cld
        sub     rsp, 96                 ; 32 shadow + 64 record
        mov     r15, r9                 ; apply flag
        mov     r13, r8                 ; new flags
        mov     r14, rcx                ; cursor virtual address
        mov     r12, rdx                ; pages left
.loop:
        test    r12, r12
        jz      .ok
        mov     rcx, r14
        lea     rdx, [rsp+32]
        call    mm_vmm_resolve
        cmp     eax, MM_ENOENT
        je      .invalid
        test    eax, eax
        jnz     .done
        cmp     qword [rsp+32+8], MM_PAGE_SIZE
        jne     .notsup
        mov     rax, r14
        sub     rax, MM_KERNEL_BASE
        add     rax, qword [mm_boot_state + MM_BOOT_STATE.kernel_start]
        cmp     qword [rsp+32+48], rax
        jne     .io
        test    r15, r15
        jz      .next
        mov     rsi, qword [rsp+32]
        mov     rax, qword [rsi]
        mov     rcx, MM_MAP_ALLOWED
        not     rcx
        and     rax, rcx                ; clear RW and NX, keep PA and P
        or      rax, r13
        mov     qword [rsi], rax
        invlpg  [r14]
.next:
        add     r14, MM_PAGE_SIZE
        dec     r12
        jmp     .loop
.ok:
        xor     eax, eax
        jmp     .done
.invalid:
        mov     eax, MM_EINVAL
        jmp     .done
.notsup:
        mov     eax, MM_ENOTSUP
        jmp     .done
.io:
        mov     eax, MM_EIO
.done:
        add     rsp, 96
        mm_leave
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; integration only: change the permissions of existing kernel leaves
; in
; rcx = first virtual address, rdx = page count, r8 = flags
; out
; eax = status, rdx = 0
; note
; only the current kernel+stack high half range is accepted, only
; existing 4 KiB leaves are touched, and every page is verified before
; the first store happens
vmm_boot_protect:
        mm_enter
        cld
        mov     r12, rcx
        mov     r13, rdx
        mov     r14, r8
        cmp     dword [mm_vmm_state + MM_VMM_STATE.ready], 0
        je      .notready
        mov     rax, qword [mm_boot_state + MM_BOOT_STATE.kernel_end]
        test    rax, rax
        jz      .notready
        test    r13, r13
        jz      .invalid
        test    r12, MM_PAGE_SIZE-1
        jnz     .invalid
        mov     rax, r14
        mov     rdx, MM_MAP_ALLOWED
        not     rdx
        test    rax, rdx
        jnz     .invalid

        ; the range must stay inside the kernel+stack high half mapping
        mov     r15, qword [mm_boot_state + MM_BOOT_STATE.kernel_end]
        sub     r15, qword [mm_boot_state + MM_BOOT_STATE.kernel_start]
        add     r15, MM_KERNEL_BASE     ; exclusive high half end
        cmp     r12, MM_KERNEL_BASE
        jb      .invalid
        cmp     r12, r15
        jae     .invalid
        mov     rax, r13
        shr     rax, 52
        jnz     .invalid                ; count * 4096 would overflow
        mov     rbx, r13
        shl     rbx, MM_PAGE_SHIFT
        mov     rax, r12
        add     rax, rbx
        jc      .invalid
        cmp     rax, r15
        ja      .invalid

        ; verify everything, then apply
        mov     rcx, r12
        mov     rdx, r13
        mov     r8, r14
        xor     r9d, r9d
        call    mm_vmm_protect_pass
        test    eax, eax
        jnz     .done
        mov     rcx, r12
        mov     rdx, r13
        mov     r8, r14
        mov     r9d, 1
        call    mm_vmm_protect_pass
        jmp     .done
.notready:
        mov     eax, MM_ENOTREADY
        jmp     .done
.invalid:
        mov     eax, MM_EINVAL
.done:
        xor     edx, edx
        mm_leave
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; exercise the module on the live machine
; out
; eax = MM_OK or MM_EIO, rdx = bitmask of the failed steps
; note
; the successful mappings leave their page tables pinned, and the test
; hook is always restored to zero
vmm_selftest:
        mm_enter
        cld
        xor     r12d, r12d              ; failure mask
        xor     r14d, r14d              ; data page of the round trip
        cmp     dword [mm_vmm_state + MM_VMM_STATE.ready], 0
        je      .notready

        ; step 0: the stack top page is a writable, non-executable 4 KiB leaf
        mov     rcx, qword [mm_boot_state + MM_BOOT_STATE.stack_top_va]
        dec     rcx
        and     rcx, -MM_PAGE_SIZE
        lea     rdx, [mm_selftest_query]
        call    vmm_query
        test    eax, eax
        jnz     .fail0
        mov     rax, qword [mm_selftest_query + MM_VMM_QUERY.effective_flags]
        mov     rcx, MM_VQ_PRESENT or MM_VQ_RW or MM_VQ_NX
        and     rax, rcx
        cmp     rax, rcx
        jne     .fail0
        cmp     qword [mm_selftest_query + MM_VMM_QUERY.page_size], MM_PAGE_SIZE
        jne     .fail0
        jmp     .step1
.fail0:
        or      r12d, 1

.step1:
        ; step 1: the kernel entry page is executable
        mov     rcx, MM_KERNEL_BASE
        lea     rdx, [mm_selftest_query]
        call    vmm_query
        test    eax, eax
        jnz     .fail1
        mov     rax, qword [mm_selftest_query + MM_VMM_QUERY.effective_flags]
        mov     rcx, MM_VQ_NX
        test    rax, rcx
        jnz     .fail1
        test    rax, MM_VQ_PRESENT
        jz      .fail1
        jmp     .step2
.fail1:
        or      r12d, 2

.step2:
        ; step 2: an untouched window address is not mapped
        mov     r15, MM_WINDOW_BASE
        mov     rax, 0x100000000
        add     r15, rax
        mov     rcx, r15
        lea     rdx, [mm_selftest_query]
        call    vmm_query
        cmp     eax, MM_ENOENT
        je      .step3
        or      r12d, 4

.step3:
        ; step 3: map, reject a duplicate, query, unmap
        call    pmm_alloc_page
        test    eax, eax
        jnz     .fail3
        mov     r14, rdx
        mov     rcx, r15
        mov     rdx, r14
        mov     r8, MM_MAP_RW or MM_MAP_NX
        call    vmm_map_page
        test    eax, eax
        jnz     .fail3
        mov     rcx, r15
        mov     rdx, r14
        mov     r8, MM_MAP_RW or MM_MAP_NX
        call    vmm_map_page
        cmp     eax, MM_EEXIST
        jne     .fail3
        mov     rcx, r15
        lea     rdx, [mm_selftest_query]
        call    vmm_query
        test    eax, eax
        jnz     .fail3
        cmp     qword [mm_selftest_query + MM_VMM_QUERY.physical_address], r14
        jne     .fail3
        cmp     qword [mm_selftest_query + MM_VMM_QUERY.page_size], MM_PAGE_SIZE
        jne     .fail3
        mov     rax, qword [mm_selftest_query + MM_VMM_QUERY.effective_flags]
        mov     rcx, MM_VQ_PRESENT or MM_VQ_RW or MM_VQ_NX
        and     rax, rcx
        cmp     rax, rcx
        jne     .fail3
        jmp     .step4
.fail3:
        or      r12d, 8
        test    r14, r14
        jz      .step4
        mov     rcx, r15
        call    vmm_unmap_page          ; best effort, the map may be absent
        mov     rcx, r14
        call    pmm_free_page
        xor     r14d, r14d

.step4:
        ; step 4: the flag rules and the window border
        mov     rcx, r15
        mov     rdx, r14
        mov     r8, MM_MAP_RW
        call    vmm_map_page
        cmp     eax, MM_EINVAL
        jne     .fail4
        mov     rcx, r15
        mov     rdx, r14
        mov     r8, MM_PTE_P
        call    vmm_map_page
        cmp     eax, MM_EINVAL
        jne     .fail4
        mov     rcx, MM_WINDOW_BASE
        mov     rax, MM_PAGE_SIZE
        sub     rcx, rax
        mov     rdx, r14
        mov     r8, MM_MAP_NX
        call    vmm_map_page
        cmp     eax, MM_EPERM
        jne     .fail4
        mov     rcx, MM_WINDOW_LIMIT
        mov     rdx, r14
        mov     r8, MM_MAP_NX
        call    vmm_map_page
        cmp     eax, MM_EPERM
        jne     .fail4
        ; drop the round trip mapping before the address is reused
        mov     rcx, r15
        call    vmm_unmap_page
        test    eax, eax
        jnz     .fail4
        cmp     rdx, r14                ; unmap reports the released PA
        jne     .fail4
        jmp     .step5
.fail4:
        or      r12d, 16

.step5:
        ; step 5: a failed offline build must leave the allocator untouched
        test    r14, r14
        jz      .abort5
        mov     r15, MM_WINDOW_BASE
        mov     rax, 0x200000000
        add     r15, rax
        lea     rcx, [mm_pmm_stats_scratch]
        call    pmm_get_stats
        test    eax, eax
        jnz     .fail5
        mov     r13, qword [mm_pmm_stats_scratch + MM_PMM_STATS.reserved]
        mov     dword [mm_test_fail], 2
        mov     rcx, r15
        mov     rdx, r14
        mov     r8, MM_MAP_NX
        call    vmm_map_page
        mov     dword [mm_test_fail], 0
        cmp     eax, MM_ENOMEM
        jne     .fail5
        lea     rcx, [mm_pmm_stats_scratch]
        call    pmm_get_stats
        test    eax, eax
        jnz     .fail5
        cmp     qword [mm_pmm_stats_scratch + MM_PMM_STATS.reserved], r13
        jne     .fail5
        ; and the same address must work once the hook is gone
        mov     rcx, r15
        mov     rdx, r14
        mov     r8, MM_MAP_NX
        call    vmm_map_page
        test    eax, eax
        jnz     .fail5
        mov     rcx, r15
        call    vmm_unmap_page
        test    eax, eax
        jnz     .fail5
        jmp     .step6
.fail5:
        or      r12d, 32
        mov     dword [mm_test_fail], 0
        jmp     .step6
.abort5:
        or      r12d, 32 or 64
        jmp     .finish

.step6:
        ; step 6: release the data page of the round trip
        mov     rcx, r15
        call    vmm_unmap_page
        cmp     eax, MM_ENOENT
        jne     .fail6
        mov     rcx, r14
        call    pmm_free_page
        test    eax, eax
        jnz     .fail6
        jmp     .finish
.fail6:
        or      r12d, 64
        test    r14, r14
        jz      .finish
        mov     rcx, r14
        call    pmm_free_page

.finish:
        mov     qword [mm_test_selftest_mask], r12
        mov     rdx, r12
        test    r12, r12
        jnz     .broken
        xor     eax, eax
        jmp     .done
.broken:
        mov     eax, MM_EIO
        jmp     .done
.notready:
        mov     eax, MM_ENOTREADY
        xor     edx, edx
.done:
        mm_leave
        ret
