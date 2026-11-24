;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Stage-2 memory management: all mutable state.
;;
;; The data block is emitted as real zero bytes in the flat image: the
;; three page bitmaps must exist at run time even before pmm_init fills
;; them, and the module must not depend on a BSS loader. The block is 4 KiB
;; aligned at both ends and nothing may follow it, so mm.asm includes this
;; file last.
;;
;; Every field is written with "db/qword N dup 0" instead of "rb": this
;; fasm2 build drops a trailing uninitialised area from the output file, so
;; only genuinely emitted bytes end up in the image. The block closes with
;; an explicit tail of zero bytes up to the page boundary, which keeps the
;; last page in the file and leaves mm_data_end exactly page aligned.
;;

        align 4096
mm_data_start:

; three page frame bitmaps covering the low 4 GiB, 128 KiB each
mm_bitmap_eligible:     db MM_BITMAP_BYTES dup 0
mm_bitmap_allocated:    db MM_BITMAP_BYTES dup 0
mm_bitmap_pinned:       db MM_BITMAP_BYTES dup 0

; overlap check scratch: one (start, end) pair per descriptor
mm_desc_scratch:        db MM_MAP_MAX_DESCS * MM_DESC_RECORD_SIZE dup 0

; table pages already visited by the CR3 pinner, cycle detection
mm_table_seen:          db MM_CR3_PIN_MAX * 8 dup 0
mm_table_seen_count:    dq 0

; module states in their frozen sizes
mm_pmm_state:           db MM_PMM_STATE_size dup 0
mm_vmm_state:           db MM_VMM_STATE_size dup 0
mm_heap_state:          db MM_HEAP_STATE_size dup 0

; private heap observability counters, outside the frozen state record
mm_heap_allocs:         dq 0
mm_heap_grows:          dq 0

; boot information cached from INIT_PARAMS and BootInfo
mm_boot_state:          db MM_BOOT_STATE_size dup 0

; test hooks, zero in a production boot:
;  - mm_test_fail: when nonzero, the N-th page table allocation fails;
;  - mm_test_fail_hits: allocations already consumed by the hook;
;  - mm_test_selftest_mask: which selftests reported a failure.
mm_test_fail:           dd 0
mm_test_fail_hits:      dd 0
mm_test_selftest_mask:  dq 0

; scratch blocks used by get_stats and by the selftests
mm_pmm_stats_scratch:   db MM_PMM_STATS_size dup 0
mm_heap_stats_scratch:  db MM_HEAP_STATS_size dup 0
mm_selftest_query:      db MM_VMM_QUERY_size dup 0
mm_test_pages:          dq 4 dup 0

; tail padding, emitted as data so the file keeps every zero byte
        db (MM_PAGE_SIZE - (($ - mm_data_start) and (MM_PAGE_SIZE-1))) and (MM_PAGE_SIZE-1) dup 0
mm_data_end:

if (mm_data_end - mm_data_start) mod MM_PAGE_SIZE
        display "data block is not page aligned", 13, 10
        err
end if
