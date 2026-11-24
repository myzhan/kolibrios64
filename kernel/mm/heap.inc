;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Stage-2 early bump heap.
;;
;; A fixed 16 MiB window at the base of the public mapping window, mapped
;; lazily in 4 KiB pages with RW/NX permissions. Allocation is a bump
;; pointer with a caller supplied power of two alignment between 16 and
;; 4096 bytes; alignment 0 means 16. There is no free. Growth maps one
;; page at a time and, when a mapping fails, unmaps every page added by
;; that call and returns the data pages to the physical allocator.
;;
;; The frozen state keeps absolute addresses: brk and mapped_end are
;; virtual addresses inside [base, limit). The allocation and growth
;; counters are private observability fields outside the state record.
;;

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; grow the mapped part of the heap
; in
; rcx = required size in bytes relative to base, page rounded up
; out
; eax = status
; note
; on failure nothing stays mapped and no data page stays allocated; the
; page tables that were created are kept pinned on purpose, because the
; rollback contract of the VMM does not reclaim them either
mm_heap_grow:
        mm_enter
        cld
        mov     r15, rcx
        add     r15, MM_PAGE_SIZE-1
        and     r15, -MM_PAGE_SIZE
        mov     r14, qword [mm_heap_state + MM_HEAP_STATE.mapped_end]
        sub     r14, qword [mm_heap_state + MM_HEAP_STATE.base]
        cmp     r14, r15
        jae     .ok
        mov     r13, r14                ; first offset added by this call
.grow_loop:
        cmp     r14, r15
        jae     .grown
        call    pmm_alloc_page
        test    eax, eax
        jnz     .rollback
        mov     r12, rdx
        mov     rcx, qword [mm_heap_state + MM_HEAP_STATE.base]
        add     rcx, r14
        mov     rdx, r12
        mov     r8, MM_MAP_RW or MM_MAP_NX
        call    vmm_map_page
        test    eax, eax
        jnz     .free_page
        add     r14, MM_PAGE_SIZE
        jmp     .grow_loop
.free_page:
        mov     rcx, r12
        call    pmm_free_page
.rollback:
        ; give back every page this call added, newest first; unmap
        ; reports the physical address, so no query is needed here
        mov     rbx, r14
.unmap_loop:
        cmp     rbx, r13
        jbe     .rolled_back
        sub     rbx, MM_PAGE_SIZE
        mov     rcx, qword [mm_heap_state + MM_HEAP_STATE.base]
        add     rcx, rbx
        call    vmm_unmap_page
        test    eax, eax
        jnz     .unmap_loop
        test    rdx, rdx
        jz      .unmap_loop
        mov     rcx, rdx
        call    pmm_free_page
        jmp     .unmap_loop
.rolled_back:
        mov     eax, MM_ENOMEM
        jmp     .done
.grown:
        mov     rax, qword [mm_heap_state + MM_HEAP_STATE.base]
        add     rax, r14
        mov     qword [mm_heap_state + MM_HEAP_STATE.mapped_end], rax
        inc     qword [mm_heap_grows]
        xor     eax, eax
        jmp     .done
.ok:
        xor     eax, eax
.done:
        mm_leave
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; create the heap
; in
; nothing, the VMM must be initialised
; out
; eax = status, rdx = heap base on success, 0 on failure
heap_init:
        mm_enter
        cld
        cmp     dword [mm_vmm_state + MM_VMM_STATE.ready], 0
        je      .notready
        cmp     qword [mm_heap_state + MM_HEAP_STATE.ready], 0
        jne     .already
        mov     rax, MM_HEAP_BASE
        mov     qword [mm_heap_state + MM_HEAP_STATE.base], rax
        mov     qword [mm_heap_state + MM_HEAP_STATE.brk], rax
        mov     qword [mm_heap_state + MM_HEAP_STATE.mapped_end], rax
        mov     rax, MM_HEAP_LIMIT
        mov     qword [mm_heap_state + MM_HEAP_STATE.limit], rax
        mov     qword [mm_heap_state + MM_HEAP_STATE.reserved], 0
        mov     qword [mm_heap_allocs], 0
        mov     qword [mm_heap_grows], 0
        mov     ecx, MM_HEAP_INITIAL
        call    mm_heap_grow
        test    eax, eax
        jnz     .fail
        mov     qword [mm_heap_state + MM_HEAP_STATE.ready], 1
        mov     rdx, MM_HEAP_BASE
        xor     eax, eax
        jmp     .done
.notready:
        mov     eax, MM_ENOTREADY
        jmp     .fail
.already:
        mov     eax, MM_EALREADY
.fail:
        xor     edx, edx
.done:
        mm_leave
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; allocate a block from the bump heap
; in
; rcx = size in bytes, rdx = alignment, a power of two in [16, 4096],
;       0 selects the default alignment 16
; out
; eax = status, rdx = virtual address of the block, 0 on failure
heap_alloc:
        mm_enter
        cld
        mov     r15, rcx
        mov     r14, rdx
        cmp     qword [mm_heap_state + MM_HEAP_STATE.ready], 0
        je      .notready
        test    r15, r15
        jz      .invalid
        test    r14, r14
        jnz     .align_set
        mov     r14d, 16                ; alignment 0 means 16
.align_set:
        cmp     r14, 16
        jb      .invalid
        cmp     r14, MM_PAGE_SIZE
        ja      .invalid
        mov     rax, r14
        dec     rax
        test    rax, r14
        jnz     .invalid

        ; start = (brk - base) rounded up to the alignment
        mov     r13, qword [mm_heap_state + MM_HEAP_STATE.brk]
        sub     r13, qword [mm_heap_state + MM_HEAP_STATE.base]
        mov     rax, r13
        mov     rcx, r14
        dec     rcx
        add     rax, rcx
        jc      .overflow
        mov     rcx, r14
        neg     rcx
        and     rax, rcx
        mov     r13, rax
        ; end = start + size, inside the window
        add     rax, r15
        jc      .overflow
        cmp     rax, MM_HEAP_MAX
        ja      .nomem
        mov     r12, rax

        mov     rax, qword [mm_heap_state + MM_HEAP_STATE.mapped_end]
        sub     rax, qword [mm_heap_state + MM_HEAP_STATE.base]
        cmp     r12, rax
        jbe     .space_ready
        mov     rcx, r12
        call    mm_heap_grow
        test    eax, eax
        jnz     .fail
.space_ready:

        ; the block must come back zeroed even after a grown region
        mov     rdi, qword [mm_heap_state + MM_HEAP_STATE.base]
        add     rdi, r13
        mov     rcx, r15
        xor     eax, eax
        rep     stosb

        mov     rax, qword [mm_heap_state + MM_HEAP_STATE.base]
        add     rax, r12
        mov     qword [mm_heap_state + MM_HEAP_STATE.brk], rax
        inc     qword [mm_heap_allocs]
        mov     rdx, qword [mm_heap_state + MM_HEAP_STATE.base]
        add     rdx, r13
        xor     eax, eax
        jmp     .done
.notready:
        mov     eax, MM_ENOTREADY
        jmp     .fail
.invalid:
        mov     eax, MM_EINVAL
        jmp     .fail
.overflow:
        mov     eax, MM_EOVERFLOW
        jmp     .fail
.nomem:
        mov     eax, MM_ENOMEM
        jmp     .fail
.fail:
        xor     edx, edx
.done:
        mm_leave
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; the bump heap has no free
; in
; rcx = block address, only checked for shape
; out
; eax = MM_ENOTSUP always, rdx = 0
heap_free:
        cld
        mov     eax, MM_ENOTSUP
        xor     edx, edx
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; fill the 32 byte heap statistics record
; in
; rcx = output record
; out
; eax = status, rdx = 0
heap_get_stats:
        cld
        test    rcx, rcx
        jz      .fault
        cmp     qword [mm_heap_state + MM_HEAP_STATE.ready], 0
        je      .notready
        mov     rax, qword [mm_heap_state + MM_HEAP_STATE.base]
        mov     qword [rcx + MM_HEAP_STATS.base], rax
        mov     rax, qword [mm_heap_state + MM_HEAP_STATE.brk]
        mov     qword [rcx + MM_HEAP_STATS.brk], rax
        mov     rax, qword [mm_heap_state + MM_HEAP_STATE.mapped_end]
        mov     qword [rcx + MM_HEAP_STATS.mapped_end], rax
        mov     rax, qword [mm_heap_state + MM_HEAP_STATE.limit]
        mov     qword [rcx + MM_HEAP_STATS.limit], rax
        xor     eax, eax
        xor     edx, edx
        ret
.notready:
        mov     eax, MM_ENOTREADY
        xor     edx, edx
        ret
.fault:
        mov     eax, MM_EFAULT
        xor     edx, edx
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; exercise the heap on the live machine
; out
; eax = MM_OK or MM_EIO, rdx = bitmask of the failed steps
heap_selftest:
        mm_enter
        cld
        xor     r12d, r12d
        cmp     qword [mm_heap_state + MM_HEAP_STATE.ready], 0
        je      .notready

        ; step 0: every allowed alignment is honoured and the block is zero
        mov     r13, 16
.align_loop:
        mov     rcx, 32
        mov     rdx, r13
        call    heap_alloc
        test    eax, eax
        jnz     .fail0
        mov     rax, rdx
        mov     rcx, r13
        dec     rcx
        test    rax, rcx
        jnz     .fail0
        cmp     qword [rdx], 0
        jne     .fail0
        cmp     qword [rdx + 24], 0
        jne     .fail0
        shl     r13, 1
        cmp     r13, MM_PAGE_SIZE
        jbe     .align_loop
        ; alignment 0 selects the default 16
        mov     rcx, 16
        xor     edx, edx
        call    heap_alloc
        test    eax, eax
        jnz     .fail0
        test    rdx, 15
        jnz     .fail0
        jmp     .step1
.fail0:
        or      r12d, 1

.step1:
        ; step 1: rejected arguments
        xor     ecx, ecx
        mov     rdx, 16
        call    heap_alloc
        cmp     eax, MM_EINVAL
        jne     .fail1
        mov     rcx, 16
        mov     rdx, 8
        call    heap_alloc
        cmp     eax, MM_EINVAL
        jne     .fail1
        mov     rcx, 16
        mov     rdx, 8192
        call    heap_alloc
        cmp     eax, MM_EINVAL
        jne     .fail1
        mov     rcx, 16
        mov     rdx, 24
        call    heap_alloc
        cmp     eax, MM_EINVAL
        jne     .fail1
        mov     rcx, 16
        mov     rdx, 16
        call    heap_free
        cmp     eax, MM_ENOTSUP
        jne     .fail1
        jmp     .step2
.fail1:
        or      r12d, 2

.step2:
        ; step 2: a request above the window is refused without any change
        lea     rcx, [mm_heap_stats_scratch]
        call    heap_get_stats
        test    eax, eax
        jnz     .fail2
        mov     r13, qword [mm_heap_stats_scratch + MM_HEAP_STATS.brk]
        mov     r14, qword [mm_heap_stats_scratch + MM_HEAP_STATS.mapped_end]
        mov     rcx, MM_HEAP_MAX
        mov     rdx, 16
        call    heap_alloc
        cmp     eax, MM_ENOMEM
        jne     .fail2
        lea     rcx, [mm_heap_stats_scratch]
        call    heap_get_stats
        test    eax, eax
        jnz     .fail2
        cmp     qword [mm_heap_stats_scratch + MM_HEAP_STATS.brk], r13
        jne     .fail2
        cmp     qword [mm_heap_stats_scratch + MM_HEAP_STATS.mapped_end], r14
        jne     .fail2
        jmp     .step3
.fail2:
        or      r12d, 4

.step3:
        ; step 3: a failed growth rolls the mapping and the data pages back.
        ; The failure hook lives in the table allocator only, so the
        ; injection must force a fresh page table: one PT spans 2 MiB, so
        ; first grow the heap up to the next 2 MiB boundary (no hook), and
        ; only then arm the hook -- the next page needs a new PT and the
        ; injection is guaranteed to fire. A growth inside an existing PT
        ; never calls the table allocator and could not be failed this way.
        lea     rcx, [mm_heap_stats_scratch]
        call    heap_get_stats
        test    eax, eax
        jnz     .fail3
        mov     rcx, qword [mm_heap_stats_scratch + MM_HEAP_STATS.mapped_end]
        sub     rcx, qword [mm_heap_stats_scratch + MM_HEAP_STATS.base]
        add     rcx, 0x1FFFFF
        and     rcx, -0x200000            ; next 2 MiB boundary >= mapped
        sub     rcx, qword [mm_heap_stats_scratch + MM_HEAP_STATS.brk]
        add     rcx, qword [mm_heap_stats_scratch + MM_HEAP_STATS.base]
        jz      .at_boundary              ; already on a boundary
        mov     rdx, 16
        call    heap_alloc
        test    eax, eax
        jnz     .fail3
.at_boundary:
        lea     rcx, [mm_heap_stats_scratch]
        call    heap_get_stats
        test    eax, eax
        jnz     .fail3
        mov     r13, qword [mm_heap_stats_scratch + MM_HEAP_STATS.brk]
        mov     r14, qword [mm_heap_stats_scratch + MM_HEAP_STATS.mapped_end]
        mov     rcx, MM_PAGE_SIZE
        mov     rdx, 16
        mov     dword [mm_test_fail], 1
        call    heap_alloc
        mov     dword [mm_test_fail], 0
        cmp     eax, MM_ENOMEM
        jne     .fail3
        lea     rcx, [mm_heap_stats_scratch]
        call    heap_get_stats
        test    eax, eax
        jnz     .fail3
        cmp     qword [mm_heap_stats_scratch + MM_HEAP_STATS.brk], r13
        jne     .fail3
        cmp     qword [mm_heap_stats_scratch + MM_HEAP_STATS.mapped_end], r14
        jne     .fail3
        jmp     .step4
.fail3:
        or      r12d, 8
        mov     dword [mm_test_fail], 0

.step4:
        ; step 4: a real growth works and the statistics stay consistent
        lea     rcx, [mm_heap_stats_scratch]
        call    heap_get_stats
        test    eax, eax
        jnz     .fail4
        mov     rcx, qword [mm_heap_stats_scratch + MM_HEAP_STATS.mapped_end]
        sub     rcx, qword [mm_heap_stats_scratch + MM_HEAP_STATS.base]
        add     rcx, MM_HEAP_INITIAL
        mov     rdx, 16
        call    heap_alloc
        test    eax, eax
        jnz     .fail4
        lea     rcx, [mm_heap_stats_scratch]
        call    heap_get_stats
        test    eax, eax
        jnz     .fail4
        mov     rax, qword [mm_heap_stats_scratch + MM_HEAP_STATS.brk]
        cmp     rax, qword [mm_heap_stats_scratch + MM_HEAP_STATS.mapped_end]
        ja      .fail4
        mov     rax, qword [mm_heap_stats_scratch + MM_HEAP_STATS.mapped_end]
        cmp     rax, qword [mm_heap_stats_scratch + MM_HEAP_STATS.limit]
        ja      .fail4
        mov     rax, qword [mm_heap_stats_scratch + MM_HEAP_STATS.base]
        mov     rcx, MM_HEAP_BASE
        cmp     rax, rcx
        jne     .fail4
        jmp     .finish
.fail4:
        or      r12d, 16

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