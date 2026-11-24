;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; In-process execution probe for the stage 2 memory management module.
;;
;; check.py assembles this file at org 0, copies it into an anonymous
;; mapping (above 4 GiB under Rosetta, where the low 4 GiB is reserved)
;; and runs the real machine code of the refusal paths and the test hook.
;; It is a test harness, not part of the kernel.
;;
;; It includes the very same module sources as mm.asm, in the same order,
;; so the executed bytes are the bytes the kernel would run. At org 0
;; fasmg encodes every non-indexed global reference as RIP-relative, which
;; is position independent: once the image is copied to the arena those
;; references resolve to the arena copy of the data block automatically.
;; The only absolute (SIB-indexed) references are the mm_test_pages and
;; mm_table_seen arrays inside pmm_selftest and mm_pin_table_page; the
;; execution scenarios never call those two functions, so the low absolute
;; addresses they carry are never dereferenced.
;;
;; A compact manifest of qwords starts the image, in the order
;; EXEC_MANIFEST_NAMES in check.py expects. The manifest holds org-0
;; offsets; check.py adds the arena base to obtain the run time addresses.

format binary

if defined MM_EXEC_ORG
        org MM_EXEC_ORG
else
        org 0
end if

use64
MM_HOSTED = 1

align 8
mm_exec_manifest:
        dq      'K64MMEX1'                  ; magic
        dq      mm_data_start
        dq      mm_bitmap_eligible
        dq      mm_bitmap_allocated
        dq      mm_bitmap_pinned
        dq      mm_desc_scratch
        dq      mm_pmm_state
        dq      mm_vmm_state
        dq      mm_heap_state
        dq      mm_boot_state
        dq      mm_test_fail
        dq      mm_test_fail_hits
        dq      mm_pmm_stats_scratch
        dq      mm_heap_stats_scratch
        dq      mm_selftest_query
        dq      mm_pmm_hook
        dq      mm_bitmap_fill
        dq      mm_bitmap_clear
        dq      mm_bitmap_count
        dq      mm_pfn_next_free
        dq      mm_pfn_alloc
        dq      pmm_free_page
        dq      pmm_get_stats
        dq      mm_pmm_drop_table
        dq      heap_get_stats
        dq      heap_free
        dq      heap_alloc
        dq      vmm_query
        dq      vmm_map_page
        dq      vmm_unmap_page
        dq      vmm_boot_protect

include '../../kernel/mm/api.inc'
include '../../kernel/mm/pmm.inc'
include '../../kernel/mm/bootmap.inc'
include '../../kernel/mm/vmm.inc'
include '../../kernel/mm/heap.inc'
include '../../kernel/mm/selftest.inc'
include '../../kernel/mm/data.inc'