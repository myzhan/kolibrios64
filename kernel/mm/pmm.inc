;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Stage-2 physical page allocator.
;;
;; Three bitmaps, one bit per 4 KiB page of the low 4 GiB:
;;  - eligible  : page may be handed out (conventional WB memory only);
;;  - allocated : page is currently owned by the public allocator;
;;  - pinned    : page must never be handed out or publicly released.
;;
;; Initialisation order is frozen: eligible=0, allocated=0, pinned=all
;; ones, then legal conventional pages set eligible and clear their pin,
;; then every reservation re-pins its range. A page is free when it is
;; eligible and neither allocated nor pinned.
;;
;; The state block keeps the counters incrementally; pmm_get_stats
;; recounts the bitmaps, checks the invariants and cross-checks the
;; counters. Counts cover eligible pages only and always obey
;; total = free + allocated + reserved.
;;
;; Allocation is next-fit over the whole 1M PFN space, so it always stays
;; below 4 GiB. A freshly allocated page is zeroed through the identity
;; map, which is the only way to touch physical memory at this stage.
;;

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; clear one bitmap
; in
; rcx = bitmap base
; destroys rax, rcx
mm_bitmap_clear:
        push    rdi
        cld
        mov     rdi, rcx
        xor     eax, eax
        mov     ecx, MM_BITMAP_BYTES/8
        rep     stosq
        pop     rdi
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; set every bit of one bitmap
; in
; rcx = bitmap base
; destroys rax, rcx
mm_bitmap_fill:
        push    rdi
        cld
        mov     rdi, rcx
        mov     rax, -1
        mov     ecx, MM_BITMAP_BYTES/8
        rep     stosq
        pop     rdi
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; count the set bits of one bitmap
; in
; rcx = bitmap base
; out
; rdx = number of set bits
; destroys rax, rcx
mm_bitmap_count:
        push    rsi
        mov     rsi, rcx
        mov     ecx, MM_BITMAP_BYTES/8
        xor     edx, edx
.loop:
        mov     rax, qword [rsi]
        popcnt  rax, rax
        add     rdx, rax
        add     rsi, 8
        dec     ecx
        jnz     .loop
        pop     rsi
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; zero one physical page through the identity map
; in
; rcx = physical address, page aligned
; destroys rax, rcx
mm_pfn_zero:
        push    rdi
        cld
        mov     rdi, rcx
        xor     eax, eax
        mov     ecx, MM_PAGE_SIZE/8
        rep     stosq
        pop     rdi
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; test hook for the rollback paths: let the N-th table allocation fail
; out
; eax = MM_OK when the allocation may go on, MM_ENOMEM to fail it
; destroys eax
; note
; the branch must test the decremented countdown in EAX, not the flags
; of the hits increment: the hook fires exactly when the countdown
; reaches zero
mm_pmm_hook:
        mov     eax, dword [mm_test_fail]
        test    eax, eax
        jz      .allow
        dec     eax
        mov     dword [mm_test_fail], eax
        inc     dword [mm_test_fail_hits]
        test    eax, eax
        jnz     .allow
        mov     eax, MM_ENOMEM
        ret
.allow:
        xor     eax, eax
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; find the next free page frame at or after a start PFN, wrapping once
; in
; rcx = start PFN
; out
; rdx = PFN, or MM_PFN_NONE when the whole space is used
; destroys rax, rcx, rdx, r8
mm_pfn_next_free:
        mov     r8, rcx
        mov     rax, rcx
.scan_tail:
        cmp     rax, MM_PFN_COUNT
        jae     .scan_head
        bt      qword [mm_bitmap_eligible], rax
        jnc     .skip_tail
        bt      qword [mm_bitmap_pinned], rax
        jc      .skip_tail
        bt      qword [mm_bitmap_allocated], rax
        jnc     .found
.skip_tail:
        inc     rax
        jmp     .scan_tail
.scan_head:
        xor     eax, eax
.scan_head_loop:
        cmp     rax, r8
        jae     .none
        bt      qword [mm_bitmap_eligible], rax
        jnc     .skip_head
        bt      qword [mm_bitmap_pinned], rax
        jc      .skip_head
        bt      qword [mm_bitmap_allocated], rax
        jnc     .found
.skip_head:
        inc     rax
        jmp     .scan_head_loop
.found:
        mov     rdx, rax
        ret
.none:
        mov     rdx, MM_PFN_NONE
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; allocate one page frame, internal: no state checks, no zeroing
; out
; rdx = PFN, or MM_PFN_NONE
; destroys rax, rcx, rdx, r8
mm_pfn_alloc:
        sub     rsp, 40               ; shadow space, RSP 16 aligned
        mov     rcx, qword [mm_pmm_state + MM_PMM_STATE.next_pfn]
        call    mm_pfn_next_free
        add     rsp, 40
        cmp     rdx, MM_PFN_NONE
        je      .done
        bts     qword [mm_bitmap_allocated], rdx
        inc     qword [mm_pmm_state + MM_PMM_STATE.allocated_pages]
        dec     qword [mm_pmm_state + MM_PMM_STATE.free_pages]
        lea     rax, [rdx + 1]
        cmp     rax, MM_PFN_COUNT
        jb      .hint_ok
        xor     eax, eax
.hint_ok:
        mov     qword [mm_pmm_state + MM_PMM_STATE.next_pfn], rax
.done:
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; convert an allocated page frame into a pinned page table page
; out
; eax = status, rdx = physical address of the table page
; destroys rax, rcx, rdx, r8
mm_pmm_alloc_table:
        mm_enter
        call    mm_pmm_hook
        test    eax, eax
        jnz     .fail
        call    mm_pfn_alloc
        cmp     rdx, MM_PFN_NONE
        je      .nomem
        mov     rbx, rdx
        btr     qword [mm_bitmap_allocated], rbx
        bts     qword [mm_bitmap_pinned], rbx
        dec     qword [mm_pmm_state + MM_PMM_STATE.allocated_pages]
        inc     qword [mm_pmm_state + MM_PMM_STATE.reserved_pages]
        mov     rdx, rbx
        shl     rdx, MM_PAGE_SHIFT
        mov     rcx, rdx
        call    mm_pfn_zero
        xor     eax, eax
        jmp     .done
.nomem:
        mov     eax, MM_ENOMEM
        xor     edx, edx
        jmp     .done
.fail:
        mov     eax, MM_ENOMEM
        xor     edx, edx
.done:
        mm_leave
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; release a pinned page table page back to the free pool (rollback path)
; in
; rcx = physical address, page aligned, pinned and not allocated
; out
; eax = status
; destroys eax, rcx, rdx, r8
mm_pmm_drop_table:
        test    rcx, MM_PAGE_SIZE-1
        jnz     .invalid
        mov     rdx, rcx
        shr     rdx, MM_PAGE_SHIFT
        cmp     rdx, MM_PFN_COUNT
        jae     .invalid
        bt      qword [mm_bitmap_pinned], rdx
        jnc     .internal
        bt      qword [mm_bitmap_allocated], rdx
        jc      .internal
        btr     qword [mm_bitmap_pinned], rdx
        dec     qword [mm_pmm_state + MM_PMM_STATE.reserved_pages]
        inc     qword [mm_pmm_state + MM_PMM_STATE.free_pages]
        cmp     rdx, qword [mm_pmm_state + MM_PMM_STATE.next_pfn]
        jae     .hint_ok
        mov     qword [mm_pmm_state + MM_PMM_STATE.next_pfn], rdx
.hint_ok:
        mov     eax, MM_OK
        ret
.internal:
        mov     eax, MM_EIO
        ret
.invalid:
        mov     eax, MM_EINVAL
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; recount the bitmaps and check the allocator invariants
; out
; eax = MM_OK, MM_EIO when allocated leaves the eligible set or
;       overlaps the pinned set
; r8  = total (eligible pages)
; r10 = reserved (eligible AND pinned)
; r11 = allocated
; destroys rax, rcx, rdx, r9
mm_pmm_recount:
        mm_enter
        cld
        mov     rsi, mm_bitmap_eligible
        mov     rdi, mm_bitmap_allocated
        mov     r12, mm_bitmap_pinned

        xor     r8d, r8d                ; eligible
        xor     r10d, r10d              ; eligible AND pinned
        xor     r11d, r11d              ; allocated
        xor     r13d, r13d              ; word index
        mov     qword [rsp], 0          ; allocated outside eligible
        mov     qword [rsp+8], 0        ; allocated AND pinned

.count_loop:
        mov     rax, qword [rsi]        ; eligible
        mov     rcx, qword [rdi]        ; allocated
        mov     rdx, qword [r12]        ; pinned

        popcnt  rbx, rax
        add     r8, rbx
        mov     rbx, rax
        and     rbx, rdx
        popcnt  rbx, rbx
        add     r10, rbx
        popcnt  rbx, rcx
        add     r11, rbx

        mov     rbx, rax
        not     rbx
        and     rbx, rcx
        popcnt  rbx, rbx
        add     qword [rsp], rbx
        mov     rbx, rcx
        and     rbx, rdx
        popcnt  rbx, rbx
        add     qword [rsp+8], rbx

        add     rsi, 8
        add     rdi, 8
        add     r12, 8
        inc     r13
        cmp     r13, MM_BITMAP_BYTES/8
        jb      .count_loop

        ; invariants: allocated must be eligible and must not be pinned
        mov     rax, qword [rsp]
        or      rax, qword [rsp+8]
        jnz     .broken
        xor     eax, eax
        jmp     .done
.broken:
        mov     eax, MM_EIO
.done:
        mm_leave
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; initialise the physical allocator
; in
; rcx = INIT_PARAMS (64 bytes)
; out
; eax = status, rdx = 0
pmm_init:
        mm_enter
        cld
        mov     rbx, rcx
        cmp     qword [mm_pmm_state + MM_PMM_STATE.ready], 0
        jne     .already

        call    mm_params_validate
        test    eax, eax
        jnz     .done

        call    mm_cpu_phys_bits
        test    eax, eax
        jnz     .done
        mov     dword [mm_vmm_state + MM_VMM_STATE.phys_bits], edx

        ; frozen start state: eligible=0, allocated=0, pinned=all ones
        mov     rcx, mm_bitmap_eligible
        call    mm_bitmap_clear
        mov     rcx, mm_bitmap_allocated
        call    mm_bitmap_clear
        mov     rcx, mm_bitmap_pinned
        call    mm_bitmap_fill
        mov     qword [mm_table_seen_count], 0
        mov     rax, MM_PHYS_LIMIT
        mov     qword [mm_pmm_state + MM_PMM_STATE.managed_limit], rax
        mov     rax, qword [rbx + MM_INIT_PARAMS.cr3_phys]
        mov     qword [mm_pmm_state + MM_PMM_STATE.root_phys], rax

        mov     rcx, rbx                ; the bitmap helpers clobbered rcx
        call    mm_bootmap_build
        test    eax, eax
        jnz     .done

        ; first fit hint starts at the first free eligible page
        xor     ecx, ecx
        call    mm_pfn_next_free
        cmp     rdx, MM_PFN_NONE
        je      .nomem
        mov     qword [mm_pmm_state + MM_PMM_STATE.next_pfn], rdx

        ; seed the counters from the finished bitmaps
        call    mm_pmm_recount
        test    eax, eax
        jnz     .done
        mov     qword [mm_pmm_state + MM_PMM_STATE.total_pages], r8
        mov     qword [mm_pmm_state + MM_PMM_STATE.reserved_pages], r10
        mov     qword [mm_pmm_state + MM_PMM_STATE.allocated_pages], r11
        mov     rax, r8
        sub     rax, r10
        sub     rax, r11
        mov     qword [mm_pmm_state + MM_PMM_STATE.free_pages], rax

        mov     qword [mm_pmm_state + MM_PMM_STATE.ready], 1
        xor     eax, eax
        xor     edx, edx
        jmp     .done
.nomem:
        mov     eax, MM_ENOMEM
        jmp     .fail
.already:
        mov     eax, MM_EALREADY
        jmp     .fail
.done:
        test    eax, eax
        jz      .ret
.fail:
        xor     edx, edx
.ret:
        mm_leave
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; allocate one page
; out
; eax = status, rdx = physical address of the zeroed page (MM_OK) or 0
pmm_alloc_page:
        mm_enter
        cld
        cmp     qword [mm_pmm_state + MM_PMM_STATE.ready], 0
        je      .notready
        call    mm_pfn_alloc
        cmp     rdx, MM_PFN_NONE
        je      .nomem
        mov     rbx, rdx
        shl     rbx, MM_PAGE_SHIFT
        mov     rcx, rbx
        call    mm_pfn_zero
        mov     rdx, rbx
        xor     eax, eax
        jmp     .done
.nomem:
        mov     eax, MM_ENOMEM
        xor     edx, edx
        jmp     .done
.notready:
        mov     eax, MM_ENOTREADY
        xor     edx, edx
.done:
        mm_leave
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; release one page
; in
; rcx = physical address, page aligned
; out
; eax = status, rdx = 0
; error order: misaligned/out of range EINVAL, not eligible or pinned
; EPERM, not allocated (double release) EALREADY
pmm_free_page:
        cld
        cmp     qword [mm_pmm_state + MM_PMM_STATE.ready], 0
        je      .notready
        test    rcx, MM_PAGE_SIZE-1
        jnz     .invalid
        mov     rdx, rcx
        shr     rdx, MM_PAGE_SHIFT
        cmp     rdx, MM_PFN_COUNT
        jae     .invalid
        bt      qword [mm_bitmap_eligible], rdx
        jnc     .permitted
        bt      qword [mm_bitmap_pinned], rdx
        jc      .permitted
        bt      qword [mm_bitmap_allocated], rdx
        jnc     .already
        btr     qword [mm_bitmap_allocated], rdx
        dec     qword [mm_pmm_state + MM_PMM_STATE.allocated_pages]
        inc     qword [mm_pmm_state + MM_PMM_STATE.free_pages]
        cmp     rdx, qword [mm_pmm_state + MM_PMM_STATE.next_pfn]
        jae     .ok
        mov     qword [mm_pmm_state + MM_PMM_STATE.next_pfn], rdx
.ok:
        xor     eax, eax
        xor     edx, edx
        ret
.invalid:
        mov     eax, MM_EINVAL
        xor     edx, edx
        ret
.permitted:
        mov     eax, MM_EPERM
        xor     edx, edx
        ret
.already:
        mov     eax, MM_EALREADY
        xor     edx, edx
        ret
.notready:
        mov     eax, MM_ENOTREADY
        xor     edx, edx
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; fill the 48 byte statistics record
; in
; rcx = output record
; out
; eax = status, rdx = 0; MM_EIO when the bitmap invariants or the
;       incremental counters are broken
pmm_get_stats:
        mm_enter
        cld
        test    rcx, rcx
        jz      .fault
        mov     rbp, rcx
        cmp     qword [mm_pmm_state + MM_PMM_STATE.ready], 0
        je      .notready

        call    mm_pmm_recount
        test    eax, eax
        jnz     .done

        ; the incremental counters must agree with the bitmaps
        cmp     r8, qword [mm_pmm_state + MM_PMM_STATE.total_pages]
        jne     .broken
        cmp     r10, qword [mm_pmm_state + MM_PMM_STATE.reserved_pages]
        jne     .broken
        cmp     r11, qword [mm_pmm_state + MM_PMM_STATE.allocated_pages]
        jne     .broken
        mov     rax, r8
        sub     rax, r10
        sub     rax, r11
        cmp     rax, qword [mm_pmm_state + MM_PMM_STATE.free_pages]
        jne     .broken

        mov     rax, r8
        sub     rax, r10
        sub     rax, r11
        mov     qword [rbp + MM_PMM_STATS.free], rax
        mov     rax, MM_PHYS_LIMIT
        mov     qword [rbp + MM_PMM_STATS.managed_limit], rax
        mov     qword [rbp + MM_PMM_STATS.total], r8
        mov     qword [rbp + MM_PMM_STATS.allocated], r11
        mov     qword [rbp + MM_PMM_STATS.reserved], r10
        mov     rax, qword [mm_pmm_state + MM_PMM_STATE.next_pfn]
        mov     qword [rbp + MM_PMM_STATS.next_pfn], rax
        xor     eax, eax
        jmp     .done
.broken:
        mov     eax, MM_EIO
        jmp     .done
.notready:
        mov     eax, MM_ENOTREADY
        jmp     .done
.fault:
        mov     eax, MM_EFAULT
.done:
        xor     edx, edx
        mm_leave
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; exercise the allocator on the live machine
; out
; eax = MM_OK or MM_EIO, rdx = bitmask of the failed steps
; note
; the test borrows four pages, checks them and gives all of them back;
; when a step fails it still releases what it holds, so a failure never
; changes the free page count permanently
pmm_selftest:
        mm_enter
        cld
        xor     r12d, r12d              ; failure mask
        xor     r13d, r13d              ; pages held by the test
        cmp     qword [mm_pmm_state + MM_PMM_STATE.ready], 0
        je      .notready

        ; step 0: four aligned, distinct, zeroed pages below 4 GiB
.alloc_loop:
        call    pmm_alloc_page
        test    eax, eax
        jnz     .fail0
        mov     qword [mm_test_pages + r13*8], rdx
        inc     r13                     ; the page is held from here on
        test    rdx, MM_PAGE_SIZE-1
        jnz     .fail0
        mov     rax, MM_PHYS_LIMIT
        cmp     rdx, rax
        jae     .fail0
        cmp     qword [rdx], 0
        jne     .fail0
        mov     rax, qword [rdx + MM_PAGE_SIZE - 8]
        test    rax, rax
        jnz     .fail0
        xor     ecx, ecx
.distinct:
        lea     rax, [r13 - 1]
        cmp     rcx, rax
        jae     .distinct_ok
        cmp     qword [mm_test_pages + rcx*8], rdx
        je      .fail0
        inc     rcx
        jmp     .distinct
.distinct_ok:
        cmp     r13, 4
        jb      .alloc_loop
        jmp     .step1
.fail0:
        or      r12d, 1
        test    r13, r13
        jz      .step2
        ; whatever was taken is released together with the earlier pages
.step1:
        ; step 1: release everything, a second release is refused
        xor     r14d, r14d
.free_loop:
        cmp     r14, r13
        jae     .freed
        mov     rcx, qword [mm_test_pages + r14*8]
        call    pmm_free_page
        test    eax, eax
        jnz     .free_bad
        inc     r14
        jmp     .free_loop
.free_bad:
        or      r12d, 2
        inc     r14
        jmp     .free_loop
.freed:
        mov     rcx, qword [mm_test_pages]
        call    pmm_free_page
        cmp     eax, MM_EALREADY         ; already released
        jne     .free_shape
        mov     rcx, qword [mm_test_pages]
        inc     rcx
        call    pmm_free_page
        cmp     eax, MM_EINVAL           ; not page aligned
        jne     .free_shape
        mov     rcx, MM_PHYS_LIMIT
        call    pmm_free_page
        cmp     eax, MM_EINVAL           ; outside the managed window
        jne     .free_shape
        xor     ecx, ecx
        call    pmm_free_page
        cmp     eax, MM_EPERM            ; low memory is not eligible
        jne     .free_shape
        jmp     .step2
.free_shape:
        or      r12d, 2

.step2:
        ; step 2: a pinned page cannot be released until the pin is gone
        call    pmm_alloc_page
        test    eax, eax
        jnz     .pin_fail
        mov     r15, rdx
        mov     rax, rdx
        shr     rax, MM_PAGE_SHIFT
        bts     qword [mm_bitmap_pinned], rax
        mov     rcx, r15
        call    pmm_free_page
        cmp     eax, MM_EPERM
        jne     .pin_bad
        mov     rax, r15
        shr     rax, MM_PAGE_SHIFT
        btr     qword [mm_bitmap_pinned], rax
        mov     rcx, r15
        call    pmm_free_page
        test    eax, eax
        jnz     .pin_bad
        jmp     .step3
.pin_bad:
        mov     rax, r15
        shr     rax, MM_PAGE_SHIFT
        btr     qword [mm_bitmap_pinned], rax
        mov     rcx, r15
        call    pmm_free_page
.pin_fail:
        or      r12d, 4

.step3:
        ; step 3: the statistics must add up and point at a free page
        lea     rcx, [mm_pmm_stats_scratch]
        call    pmm_get_stats
        test    eax, eax
        jnz     .stats_bad
        mov     rax, MM_PHYS_LIMIT
        cmp     qword [mm_pmm_stats_scratch + MM_PMM_STATS.managed_limit], rax
        jne     .stats_bad
        mov     rax, qword [mm_pmm_stats_scratch + MM_PMM_STATS.free]
        add     rax, qword [mm_pmm_stats_scratch + MM_PMM_STATS.allocated]
        add     rax, qword [mm_pmm_stats_scratch + MM_PMM_STATS.reserved]
        cmp     rax, qword [mm_pmm_stats_scratch + MM_PMM_STATS.total]
        jne     .stats_bad
        xor     ecx, ecx
        call    mm_pfn_next_free
        cmp     qword [mm_pmm_stats_scratch + MM_PMM_STATS.free], 0
        je      .stats_none
        cmp     rdx, MM_PFN_NONE
        je      .stats_bad
        bt      qword [mm_bitmap_eligible], rdx
        jnc     .stats_bad
        bt      qword [mm_bitmap_allocated], rdx
        jc      .stats_bad
        bt      qword [mm_bitmap_pinned], rdx
        jc      .stats_bad
        jmp     .finish
.stats_none:
        cmp     rdx, MM_PFN_NONE
        jne     .stats_bad
        jmp     .finish
.stats_bad:
        or      r12d, 8

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