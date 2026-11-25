;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2026. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; FS-v1 M3 mutable data region. Every byte is a real zero byte in the
;; image (db dup, never rb). The region starts page aligned and ends
;; page aligned, three pages total:
;;   page 0  state, the resolve record, the bounce path/stat buffers,
;;           the probe page array and the read-back evidence
;;   page 1  the 4 KiB payload bounce page
;;   page 2  the private SYSCALL stack, its top is fsys_data_end
;; Nothing may follow it; the foundation maps the whole region RW/NX.
;;

align 4096
fsys_data_start:

; module and probe state, one qword each
fsys_ready:           dq 0          ; +0    1 after a successful fsys_init
fsys_probe_rsp:       dq 0          ; +8    parked kernel stack pointer
fsys_probe_rip:       dq 0          ; +16   parked kernel resume address
fsys_exit_code:       dq 0          ; +24   the fsprobe SYS_EXIT argument
fsys_saved_rsp0:      dq 0          ; +32   TSS.RSP0 before the probe
fsys_saved_fs_reads:  dq 0          ; +40   fs_test_reads snapshot
fsys_saved_blk_reads: dq 0          ; +48   blk_test_reads snapshot
fsys_table_base:      dq 0          ; +56   table_allocs before the maps
fsys_table_delta:     dq 0          ; +64   published tables, must be 3
fsys_probe_mask:      dq 0          ; +72   read-back leg mask, 0x7FF
fsys_probe_fail:      dq 0          ; +80   read-back failing leg, -1

; the mm_vmm_resolve result record, 64 bytes
fsys_resolve_rec:     db 64 dup 0   ; +88

; bounce buffers for the user path and the stat record
fsys_path_buf:        db FS_PATH_MAX dup 0     ; +152
fsys_stat_buf:        db FS_STAT_SIZE dup 0    ; +280

; the nine probe leaf frames, physical addresses
fsys_pages:           dq FSYS_PROBE_PAGES dup 0 ; +344

; the read-back evidence qwords: open fd, read len, eof len, seek end,
; stat size, stat attr, readdir bytes, ENOENT verdict
fsys_probe_evidence:  dq FSYS_FP_EVIDENCE dup 0 ; +416

; task 138 W3 teardown bookkeeping, appended after the frozen fields
fsys_pages_held:      dq 0          ; +480  frames held, 0 after teardown
fsys_drops_base:      dq 0          ; +488  table_drops before the maps
fsys_table_released:  dq 0          ; +496  tables reclaimed, budget on OK

; tail padding of the state page, emitted as real zero bytes
        db (4096 - (($ - fsys_data_start) and 4095)) and 4095 dup 0

; page 1: the payload bounce page
fsys_bounce:          db FSYS_BOUNCE_SIZE dup 0

; page 2: the private SYSCALL stack, growing down from fsys_data_end
fsys_syscall_stack:   db FSYS_SYSCALL_STACK dup 0
fsys_syscall_stack_top:
fsys_data_end:

if (fsys_data_end - fsys_data_start) mod 4096
        display "fsys data block is not page aligned", 13, 10
        err
end if
if fsys_data_end - fsys_data_start <> FSYS_DATA_PAGES_TOTAL * 4096
        display "fsys data budget changed from the frozen three pages", 13, 10
        err
end if
if (fsys_bounce - fsys_data_start) <> 4096
        display "the bounce page must be the second page", 13, 10
        err
end if
if (fsys_syscall_stack - fsys_data_start) <> 2 * 4096
        display "the syscall stack must be the third page", 13, 10
        err
end if
if fsys_syscall_stack_top <> fsys_data_end
        display "the syscall stack top must close the region", 13, 10
        err
end if
if fsys_syscall_stack_top and 15
        display "the syscall stack top must be 16 byte aligned", 13, 10
        err
end if
if fsys_pages_held - fsys_data_start <> 480
        display "fsys_pages_held must sit at +480", 13, 10
        err
end if
if fsys_drops_base - fsys_data_start <> 488
        display "fsys_drops_base must sit at +488", 13, 10
        err
end if
if fsys_table_released - fsys_data_start <> 496
        display "fsys_table_released must sit at +496", 13, 10
        err
end if