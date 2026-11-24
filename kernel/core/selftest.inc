;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Core integration: the cross module self test and the core data region.
;;
;; The CPU and MM modules have no call dependency on each other, so the
;; probes that need both (a real #PF driven through the CPU expected trap
;; interface against MM mappings) are orchestrated here:
;;
;;   leg 0  cpu_selftest with RCX = 0 (its own #PF leg is skipped, the
;;          real page fault legs below cover it with exact CR2 checks)
;;   leg 1  mm_selftest (pmm + vmm + heap aggregated mask)
;;   leg 2  PMM statistics baseline: total = free + allocated + reserved,
;;          plus the VMM table allocation counter
;;   leg 3  non present read at K64_VMM_TEST_BASE: vector 14, exact CR2,
;;          error low bits 0, exact saved RIP
;;   leg 4  CR0.WP write to a read only page: error low bits 0x03
;;   leg 5  EFER.NXE execution of an NX page: error low bits 0x11, the
;;          saved RIP is the fetch target itself
;;   leg 6  unmap of a touched page: the next access must fault, the old
;;          TLB entry must be gone, unmap returns the physical page
;;   leg 7  accounting: the first TEST_BASE mapping builds exactly three
;;          table pages (PDPT/PD/PT of its PML4 slot), they stay pinned
;;          and count as reserved, never as a leak; after cleanup the
;;          allocated/total counts are back at the baseline, reserved
;;          stays three above it and free stays three below it, because
;;          the pinned table pages are never reclaimed
;;
;; Every faulting instruction has its own global label immediately in
;; front of it, and every resume point is a global label too: a local
;; label would be captured by the resume point that precedes it. All
;; branch targets in core_selftest are global for the same reason.
;;
;; The test region is K64_VMM_TEST_BASE (PML4 slot 386); mm_selftest only
;; touches slot 384, so the region is clean when this test starts.
;;

; leg numbers of the failure mask returned in RDX
K64_CORE_BIT_CPU      = 0
K64_CORE_BIT_MM       = 1
K64_CORE_BIT_BASELINE = 2
K64_CORE_BIT_PF_NP    = 3
K64_CORE_BIT_PF_WP    = 4
K64_CORE_BIT_PF_NX    = 5
K64_CORE_BIT_TLB      = 6
K64_CORE_BIT_ACCOUNT  = 7

; the low five error code bits: P, W/R, U/S, RSVD, I/D
K64_PF_ERROR_MASK     = 0x1F

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; run the cross module self test
; in
; nothing; cpu_init, pmm_init, vmm_init and heap_init must have succeeded,
; IF must be clear
; out
; eax = K64_OK, or K64_EIO when a leg failed
; rdx = bitmask of the failed legs (also stored in core_selftest_mask)
; note
; on any failure the remaining legs are skipped, but every page the test
; allocated is unmapped and released before returning
core_selftest:
        mm_enter
        cld
        xor     r12d, r12d            ; failure mask, callee saved
        mov     qword [core_page_ro], 0
        mov     qword [core_page_nx], 0
        mov     qword [core_page_tlb], 0
        mov     qword [core_tlb_mapped], 0

        ; ---------------- leg 0: the CPU module self test ----------------
        xor     ecx, ecx              ; skip its #PF leg, core drives it
        call    cpu_selftest
        test    eax, eax
        jnz     core_st_fail_cpu

        ; ---------------- leg 1: the MM module self test ----------------
        call    mm_selftest
        test    eax, eax
        jnz     core_st_fail_mm

        ; ---------------- leg 2: the PMM counting baseline ----------------
        mov     rcx, core_stats_before
        call    pmm_get_stats
        test    eax, eax
        jnz     core_st_fail_baseline
        mov     rax, [core_stats_before + MM_PMM_STATS.free]
        add     rax, [core_stats_before + MM_PMM_STATS.allocated]
        add     rax, [core_stats_before + MM_PMM_STATS.reserved]
        cmp     rax, [core_stats_before + MM_PMM_STATS.total]
        jne     core_st_fail_baseline
        mov     rax, [mm_vmm_state + MM_VMM_STATE.table_allocs]
        mov     [core_tables_before], rax

        ; ---------------- leg 3: non present read ----------------
        mov     qword [core_expect + CPU_EXPECT_OFF_ERROR_MASK], K64_PF_ERROR_MASK
        mov     qword [core_expect + CPU_EXPECT_OFF_ERROR_VALUE], 0
        mov     qword [core_expect + CPU_EXPECT_OFF_CR2_MASK], -1
        mov     rax, K64_VMM_TEST_BASE
        mov     [core_expect + CPU_EXPECT_OFF_CR2_VALUE], rax
        mov     ecx, 14
        mov     rdx, core_pf_np_fault
        mov     r8, core_pf_np_resume
        mov     r9, core_expect
        call    cpu_trap_expect
        test    eax, eax
        jnz     core_st_fail_pf_np
        mov     rax, K64_VMM_TEST_BASE
core_pf_np_fault:
        mov     rax, [rax]
core_pf_np_resume:
        mov     rcx, core_frame
        call    cpu_trap_finish
        test    eax, eax
        jnz     core_st_fail_pf_np
        cmp     qword [core_frame + CPU_FRAME_VECTOR], 14
        jne     core_st_fail_pf_np
        mov     rax, K64_VMM_TEST_BASE
        cmp     [core_frame + CPU_FRAME_CR2], rax
        jne     core_st_fail_pf_np
        mov     rax, [core_frame + CPU_FRAME_ERROR]
        and     rax, K64_PF_ERROR_MASK
        jnz     core_st_fail_pf_np
        mov     rax, core_pf_np_fault
        cmp     [core_frame + CPU_FRAME_RIP], rax
        jne     core_st_fail_pf_np

        ; ---------------- leg 4: CR0.WP write to a RO page ----------------
        mov     rax, cr0
        test    eax, 0x10000          ; CR0.WP must be set by vmm_init
        jz      core_st_fail_pf_wp
        call    pmm_alloc_page
        test    eax, eax
        jnz     core_st_fail_pf_wp
        mov     [core_page_ro], rdx
        mov     rcx, K64_VMM_TEST_BASE + 0x1000
        mov     r8, MM_MAP_NX         ; present, read only, NX
        call    vmm_map_page
        test    eax, eax
        jnz     core_st_fail_pf_wp
        mov     qword [core_expect + CPU_EXPECT_OFF_ERROR_VALUE], 0x03
        mov     rax, K64_VMM_TEST_BASE + 0x1000
        mov     [core_expect + CPU_EXPECT_OFF_CR2_VALUE], rax
        mov     ecx, 14
        mov     rdx, core_pf_wp_fault
        mov     r8, core_pf_wp_resume
        mov     r9, core_expect
        call    cpu_trap_expect
        test    eax, eax
        jnz     core_st_fail_pf_wp
        mov     rax, K64_VMM_TEST_BASE + 0x1000
core_pf_wp_fault:
        mov     qword [rax], 0
core_pf_wp_resume:
        mov     rcx, core_frame
        call    cpu_trap_finish
        test    eax, eax
        jnz     core_st_fail_pf_wp
        cmp     qword [core_frame + CPU_FRAME_VECTOR], 14
        jne     core_st_fail_pf_wp
        mov     rax, K64_VMM_TEST_BASE + 0x1000
        cmp     [core_frame + CPU_FRAME_CR2], rax
        jne     core_st_fail_pf_wp
        mov     rax, [core_frame + CPU_FRAME_ERROR]
        and     rax, K64_PF_ERROR_MASK
        cmp     rax, 0x03
        jne     core_st_fail_pf_wp
        mov     rax, core_pf_wp_fault
        cmp     [core_frame + CPU_FRAME_RIP], rax
        jne     core_st_fail_pf_wp

        ; ---------------- leg 5: EFER.NXE execution of an NX page ----------------
        mov     ecx, CPU_EFER_MSR
        rdmsr
        test    eax, 0x800            ; EFER.NXE must be set by vmm_init
        jz      core_st_fail_pf_nx
        call    pmm_alloc_page
        test    eax, eax
        jnz     core_st_fail_pf_nx
        mov     [core_page_nx], rdx
        mov     rcx, K64_VMM_TEST_BASE + 0x2000
        mov     r8, MM_MAP_RW or MM_MAP_NX
        call    vmm_map_page
        test    eax, eax
        jnz     core_st_fail_pf_nx
        mov     qword [core_expect + CPU_EXPECT_OFF_ERROR_VALUE], 0x11
        mov     rax, K64_VMM_TEST_BASE + 0x2000
        mov     [core_expect + CPU_EXPECT_OFF_CR2_VALUE], rax
        mov     ecx, 14
        mov     rdx, K64_VMM_TEST_BASE + 0x2000   ; saved RIP = fetch target
        mov     r8, core_pf_nx_resume
        mov     r9, core_expect
        call    cpu_trap_expect
        test    eax, eax
        jnz     core_st_fail_pf_nx
        mov     rax, K64_VMM_TEST_BASE + 0x2000
        jmp     rax
core_pf_nx_resume:
        mov     rcx, core_frame
        call    cpu_trap_finish
        test    eax, eax
        jnz     core_st_fail_pf_nx
        cmp     qword [core_frame + CPU_FRAME_VECTOR], 14
        jne     core_st_fail_pf_nx
        mov     rax, K64_VMM_TEST_BASE + 0x2000
        cmp     [core_frame + CPU_FRAME_CR2], rax
        jne     core_st_fail_pf_nx
        mov     rax, [core_frame + CPU_FRAME_ERROR]
        and     rax, K64_PF_ERROR_MASK
        cmp     rax, 0x11
        jne     core_st_fail_pf_nx
        mov     rax, K64_VMM_TEST_BASE + 0x2000
        cmp     [core_frame + CPU_FRAME_RIP], rax
        jne     core_st_fail_pf_nx

        ; ---------------- leg 6: unmap must invalidate the TLB ----------------
        call    pmm_alloc_page
        test    eax, eax
        jnz     core_st_fail_tlb
        mov     [core_page_tlb], rdx
        mov     rcx, K64_VMM_TEST_BASE + 0x3000
        mov     r8, MM_MAP_RW or MM_MAP_NX
        call    vmm_map_page
        test    eax, eax
        jnz     core_st_fail_tlb
        mov     qword [core_tlb_mapped], 1
        ; write and read back so the translation really sits in the TLB
        mov     rax, K64_VMM_TEST_BASE + 0x3000
        mov     qword [rax], 0x1234
        cmp     qword [rax], 0x1234
        jne     core_st_fail_tlb
        mov     rcx, K64_VMM_TEST_BASE + 0x3000
        call    vmm_unmap_page
        test    eax, eax
        jnz     core_st_fail_tlb
        cmp     rdx, [core_page_tlb]
        jne     core_st_fail_tlb
        mov     qword [core_tlb_mapped], 0
        ; the very next access must fault as non present, not hit the TLB
        mov     qword [core_expect + CPU_EXPECT_OFF_ERROR_VALUE], 0
        mov     rax, K64_VMM_TEST_BASE + 0x3000
        mov     [core_expect + CPU_EXPECT_OFF_CR2_VALUE], rax
        mov     ecx, 14
        mov     rdx, core_pf_tlb_fault
        mov     r8, core_pf_tlb_resume
        mov     r9, core_expect
        call    cpu_trap_expect
        test    eax, eax
        jnz     core_st_fail_tlb
        mov     rax, K64_VMM_TEST_BASE + 0x3000
core_pf_tlb_fault:
        mov     rax, [rax]
core_pf_tlb_resume:
        mov     rcx, core_frame
        call    cpu_trap_finish
        test    eax, eax
        jnz     core_st_fail_tlb
        cmp     qword [core_frame + CPU_FRAME_VECTOR], 14
        jne     core_st_fail_tlb
        mov     rax, K64_VMM_TEST_BASE + 0x3000
        cmp     [core_frame + CPU_FRAME_CR2], rax
        jne     core_st_fail_tlb
        mov     rax, [core_frame + CPU_FRAME_ERROR]
        and     rax, K64_PF_ERROR_MASK
        jnz     core_st_fail_tlb

        ; ---------------- leg 7: reserved accounting ----------------
        ; the first TEST_BASE mapping built the PDPT, PD and PT of PML4
        ; slot 386: three table pages, allocated then pinned, so they
        ; count as reserved; all four test pages share the one PT.
        ; The LAPIC PT that irq_init built under the window slot is
        ; already part of the leg 2 baseline (irq_init runs before this
        ; test), so the frozen delta stays K64_CORE_ACCOUNT_TABLES.
        mov     rcx, core_stats_after
        call    pmm_get_stats
        test    eax, eax
        jnz     core_st_fail_account
        mov     rax, [core_stats_after + MM_PMM_STATS.reserved]
        sub     rax, [core_stats_before + MM_PMM_STATS.reserved]
        cmp     rax, K64_CORE_ACCOUNT_TABLES
        jne     core_st_fail_account
        mov     rax, [mm_vmm_state + MM_VMM_STATE.table_allocs]
        sub     rax, [core_tables_before]
        cmp     rax, K64_CORE_ACCOUNT_TABLES
        jne     core_st_fail_account
        ; the three data pages are still held at this point
        mov     rax, [core_stats_after + MM_PMM_STATS.allocated]
        sub     rax, [core_stats_before + MM_PMM_STATS.allocated]
        cmp     rax, K64_CORE_ACCOUNT_TABLES
        jne     core_st_fail_account

        ; give every data page back, the pinned table pages stay
        call    core_cleanup
        mov     rcx, core_stats_final
        call    pmm_get_stats
        test    eax, eax
        jnz     core_st_fail_account
        ; the three data pages are back, the three pinned table pages
        ; are not: free stays three below the baseline, reserved three
        ; above it, allocated and total are unchanged
        mov     rax, [core_stats_final + MM_PMM_STATS.free]
        add     rax, K64_CORE_ACCOUNT_TABLES
        cmp     rax, [core_stats_before + MM_PMM_STATS.free]
        jne     core_st_fail_account
        mov     rax, [core_stats_final + MM_PMM_STATS.allocated]
        cmp     rax, [core_stats_before + MM_PMM_STATS.allocated]
        jne     core_st_fail_account
        mov     rax, [core_stats_final + MM_PMM_STATS.total]
        cmp     rax, [core_stats_before + MM_PMM_STATS.total]
        jne     core_st_fail_account
        mov     rax, [core_stats_final + MM_PMM_STATS.reserved]
        sub     rax, [core_stats_before + MM_PMM_STATS.reserved]
        cmp     rax, K64_CORE_ACCOUNT_TABLES
        jne     core_st_fail_account

        mov     [core_selftest_mask], r12
        xor     eax, eax
        xor     edx, edx
        mm_leave
        ret

core_st_fail_cpu:
        or      r12, 1 shl K64_CORE_BIT_CPU
        jmp     core_st_failed
core_st_fail_mm:
        or      r12, 1 shl K64_CORE_BIT_MM
        jmp     core_st_failed
core_st_fail_baseline:
        or      r12, 1 shl K64_CORE_BIT_BASELINE
        jmp     core_st_failed
core_st_fail_pf_np:
        or      r12, 1 shl K64_CORE_BIT_PF_NP
        jmp     core_st_failed
core_st_fail_pf_wp:
        or      r12, 1 shl K64_CORE_BIT_PF_WP
        jmp     core_st_failed
core_st_fail_pf_nx:
        or      r12, 1 shl K64_CORE_BIT_PF_NX
        jmp     core_st_failed
core_st_fail_tlb:
        or      r12, 1 shl K64_CORE_BIT_TLB
        jmp     core_st_failed
core_st_fail_account:
        or      r12, 1 shl K64_CORE_BIT_ACCOUNT
core_st_failed:
        ; release whatever the test holds, then report the mask
        call    core_cleanup
        mov     [core_selftest_mask], r12
        mov     rdx, r12
        mov     eax, K64_EIO
        mm_leave
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; unmap and release every page the self test allocated
; in
; nothing; reads the tracking slots in the core data region
; out
; nothing; idempotent, safe on every failure path
; destroys rax, rcx, rdx, r8-r11 (the module calls preserve the rest)
core_cleanup:
        push    rbp
        mov     rbp, rsp
        sub     rsp, K64_STACK_FRAME
        cld
        ; the TLB page is unmapped by leg 6 itself when it got that far
        cmp     qword [core_tlb_mapped], 0
        je      .ro
        mov     rcx, K64_VMM_TEST_BASE + 0x3000
        call    vmm_unmap_page
        mov     qword [core_tlb_mapped], 0
.ro:
        cmp     qword [core_page_ro], 0
        je      .nx
        mov     rcx, K64_VMM_TEST_BASE + 0x1000
        call    vmm_unmap_page
        mov     rcx, [core_page_ro]
        call    pmm_free_page
        mov     qword [core_page_ro], 0
.nx:
        cmp     qword [core_page_nx], 0
        je      .tlb
        mov     rcx, K64_VMM_TEST_BASE + 0x2000
        call    vmm_unmap_page
        mov     rcx, [core_page_nx]
        call    pmm_free_page
        mov     qword [core_page_nx], 0
.tlb:
        cmp     qword [core_page_tlb], 0
        je      .done
        mov     rcx, [core_page_tlb]
        call    pmm_free_page
        mov     qword [core_page_tlb], 0
.done:
        leave
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; the core data region, the last bytes of the kernel image
;
; 4 KiB aligned at both ends, every byte really emitted (db ... dup 0,
; never rb), no code follows it. The foundation maps it RW/NX through
; vmm_boot_protect before the self test runs.
;

        align K64_DATA_ALIGN
core_data_start:

; the shared handover parameters, filled by k64_foundation_init
core_init_params:       db K64_INIT_SIZE dup 0

; scratch of the cross module probes
core_expect:            db CPU_EXPECT_SIZE dup 0
core_frame:             db CPU_FRAME_SIZE dup 0
core_stats_before:      db MM_PMM_STATS_size dup 0
core_stats_after:       db MM_PMM_STATS_size dup 0
core_stats_final:       db MM_PMM_STATS_size dup 0
core_tables_before:     dq 0

; page tracking for the cleanup path
core_page_ro:           dq 0
core_page_nx:           dq 0
core_page_tlb:          dq 0
core_tlb_mapped:        dq 0
core_selftest_mask:     dq 0

; milestone markers, ASCII only, each one appears exactly once
kmsg_cpu_ok           db 'K64-CPU-OK', 13, 10, 0
kmsg_pmm_ok           db 'K64-PMM-OK', 13, 10, 0
kmsg_vmm_ok           db 'K64-VMM-OK', 13, 10, 0
kmsg_heap_ok          db 'K64-HEAP-OK', 13, 10, 0
kmsg_irq_ok           db 'K64-IRQ-OK', 13, 10, 0
kmsg_foundation_ok    db 'K64-FOUNDATION-OK', 13, 10, 0

; failure messages, none of them contains a milestone marker
kmsg_fail_cpu         db 'foundation: cpu_init failed', 0
kmsg_fail_com1        db 'foundation: cpu_com1_init failed', 0
kmsg_fail_pmm         db 'foundation: pmm_init failed', 0
kmsg_fail_vmm         db 'foundation: vmm_init failed', 0
kmsg_fail_protect     db 'foundation: vmm_boot_protect failed', 0
kmsg_fail_heap        db 'foundation: heap_init failed', 0
kmsg_fail_irq         db 'foundation: irq bring-up failed', 0
kmsg_fail_selftest    db 'foundation: core self test failed', 0
kmsg_selftest_mask    db 'core self test failed legs = ', 0
kmsg_core_nl          db 13, 10, 0

; tail padding, emitted as data so the file keeps every zero byte
        db (K64_PAGE_SIZE - (($ - core_data_start) and (K64_PAGE_SIZE - 1))) and (K64_PAGE_SIZE - 1) dup 0
core_data_end:

if (core_data_start and (K64_DATA_ALIGN - 1)) <> 0
        err 'the core data region must start page aligned'
end if
if (core_data_end - core_data_start) mod K64_PAGE_SIZE
        err 'the core data region must end page aligned'
end if
if (core_init_params and 7) <> 0
        err 'K64_INIT_PARAMS must be 8 byte aligned'
end if
if core_expect - core_init_params <> K64_INIT_SIZE
        err 'the expect block must follow the init parameters'
end if
if (core_expect and 7) <> 0
        err 'the expect block must be qword aligned'
end if
if (core_frame and 7) <> 0
        err 'the trap frame buffer must be qword aligned'
end if
if core_data_end <= core_data_start
        err 'the core data region must not be empty'
end if