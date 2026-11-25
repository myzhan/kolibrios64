;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; FS-v1 M2 mutable data region. Every byte is a real zero byte in the
;; image (db dup, never rb): the loader copies the file, there is no
;; .bss clear pass. The region starts page aligned (the code before it
;; is padded by the foundation layout) and ends page aligned through
;; the tail padding below, so the foundation can map it RW/NX as whole
;; pages without dragging instructions in. Nothing may follow it.
;;
;; Budget: 32 KiB total, all static, zero heap bytes. The 16 KiB FAT
;; cache holds the whole first FAT (spf <= 32 enforced by fs_init);
;; the single sector cache is direct mapped with one tag; the 9 KiB
;; BIG.BIN test buffer dominates the test evidence area.
;;

align 4096
fs_data_start:

; mount state and the published geometry, one qword each
fs_ready:            dq 0           ; +0    1 after a successful fs_init
fs_spc:              dq 0           ; +8    sectors per cluster
fs_reserved:         dq 0           ; +16   reserved sectors
fs_nfats:            dq 0           ; +24   FAT copies
fs_spf:              dq 0           ; +32   sectors per FAT
fs_root_entries:     dq 0           ; +40   fixed root entry count
fs_root_sectors:     dq 0           ; +48   root area sector count
fs_total:            dq 0           ; +56   total sectors
fs_clusters:         dq 0           ; +64   data cluster count
fs_fat_lba:          dq 0           ; +72   first FAT LBA
fs_root_lba:         dq 0           ; +80   root area LBA
fs_data_lba:         dq 0           ; +88   cluster 2 LBA

; the single sector direct mapped cache bookkeeping
fs_sec_tag:          dq 0           ; +96   cached LBA
fs_sec_valid:        dq 0           ; +104  cache armed

; self test evidence counters
fs_test_mask:        dq 0           ; +112  leg failure bits
fs_test_reads:       dq 0           ; +120  completed sector fetches
fs_test_big_present: dq 0           ; +128  BIG.BIN probe verdict

; the static eight slot fd table, FS_FD_SIZE bytes per slot
fs_fd_table:         db FS_MAX_FDS * FS_FD_SIZE dup 0        ; +136

; path resolution scratch: the matched raw entry and the 8.3 name
fs_ent_buf:          db FS_ENTRY_SIZE dup 0                  ; +648
fs_name_buf:         db 16 dup 0                             ; +680

; the boot sector staging buffer of fs_init
fs_bpb_buf:          db FS_SECTOR_SIZE dup 0                 ; +696

; the single sector cache payload
fs_sec_cache:        db FS_SECTOR_SIZE dup 0                 ; +1208

; self test evidence buffers, the QEMU judge reads these byte for byte
fs_test_ibm_buf:     db 512 dup 0                            ; +1720
fs_test_tetris_buf:  db 512 dup 0                            ; +2232
fs_test_big_buf:     db 9216 dup 0                           ; +2744
fs_test_scratch:     db 512 dup 0                            ; +11960
fs_test_dirents:     db 96 dup 0                             ; +12472
fs_test_stat:        db FS_STAT_SIZE dup 0                   ; +12568

; the whole first FAT, page aligned, FS_FAT_CACHE_SECTORS sectors
align 4096
fs_fat_cache:        db FS_FAT_CACHE_SECTORS * FS_SECTOR_SIZE dup 0

; tail padding, emitted as data so the file keeps every zero byte
        db (FS_PAGE_SIZE - (($ - fs_data_start) and (FS_PAGE_SIZE-1))) and (FS_PAGE_SIZE-1) dup 0
fs_data_end:

if (fs_data_end - fs_data_start) mod FS_PAGE_SIZE
        display "fs data block is not page aligned", 13, 10
        err
end if
if (fs_fat_cache - fs_data_start) mod FS_PAGE_SIZE
        display "FAT cache is not page aligned", 13, 10
        err
end if
if fs_fat_cache - fs_data_start <> 4 * FS_PAGE_SIZE
        display "FAT cache must start at the second 16 KiB boundary", 13, 10
        err
end if
if fs_data_end - fs_data_start <> 8 * FS_PAGE_SIZE
        display "fs data budget changed from the frozen 32 KiB", 13, 10
        err
end if