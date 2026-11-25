;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Layout probe for the stage 2 fs module, task 132, contract FS-v1.
;;
;; This file is a test harness, not part of the kernel. It assembles
;; kernel/fs/fs.asm exactly as the kernel will include it: behind
;; cpu.asm (console) and blk.asm (the blk_read seam). The fs module
;; calls nothing else outside its own files, so no MM/IRQ stub is
;; needed. Nothing here is executed and no machine is started.
;;
;; FS_PROBE_ORG selects the link base; check.py assembles the probe
;; twice, once at 0 for byte offsets and once at the kernel base to
;; prove the module links high with the same internal layout.
;;
;; The manifest sits after fs.asm: the module's own end assertion
;; runs at its include site and the data block is still the last
;; module byte. The manifest is a fixed list of qwords in the order
;; check.py expects; the magic and the entry count are checked before
;; anything else is read.
;;

use64

if defined FS_PROBE_ORG
        org FS_PROBE_ORG
else
        org 0
end if

include '../../kernel/cpu/cpu.asm'

BLK_HOSTED = 1
include '../../kernel/blk/blk.asm'

FS_HOSTED = 1
include '../../kernel/fs/fs.asm'

; ------------------------------------------------------------------
; manifest, fixed order, read by check.py
; ------------------------------------------------------------------

align 8
fs_probe:
        dq      'K64FSP1'                  ; magic
        dq      1                           ; manifest version
        dq      fs_code_start
        dq      fs_code_end
        dq      fs_data_start
        dq      fs_data_end
        dq      fs_sec_read
        dq      fs_fat_entry
        dq      fs_cluster_lba
        dq      fs_norm_comp
        dq      fs_dir_find
        dq      fs_resolve
        dq      fs_open
        dq      fs_read
        dq      fs_seek
        dq      fs_close
        dq      fs_stat
        dq      fs_readdir
        dq      fs_init
        dq      fs_selftest
        dq      fs_st_fail_bpb
        dq      fs_st_fail_ibm
        dq      fs_st_fail_tetris
        dq      fs_st_fail_seek
        dq      fs_st_fail_neg
        dq      fs_st_fail_big
        dq      fs_st_fail_stat
        dq      fs_st_fail_count
        dq      fs_st_bpb_done
        dq      fs_st_ibm_done
        dq      fs_st_tetris_done
        dq      fs_st_seek_done
        dq      fs_st_neg_done
        dq      fs_st_big_done
        dq      fs_st_stat_done
        dq      fs_st_count_done
        dq      msg_fs_selftest_mask
        dq      msg_fs_nl
        dq      p_fs_ibm_lower
        dq      p_fs_ibm
        dq      p_fs_tetris
        dq      p_fs_missing
        dq      p_fs_ibm_x
        dq      p_fs_nope
        dq      p_fs_roms
        dq      p_fs_root
        dq      p_fs_programs
        dq      p_fs_big
        dq      n_fs_roms
        dq      n_fs_programs
        dq      n_fs_ibm
        dq      n_fs_tetris
        dq      n_fs_big
        dq      fs_ready
        dq      fs_spc
        dq      fs_reserved
        dq      fs_nfats
        dq      fs_spf
        dq      fs_root_entries
        dq      fs_root_sectors
        dq      fs_total
        dq      fs_clusters
        dq      fs_fat_lba
        dq      fs_root_lba
        dq      fs_data_lba
        dq      fs_sec_tag
        dq      fs_sec_valid
        dq      fs_test_mask
        dq      fs_test_reads
        dq      fs_test_big_present
        dq      fs_fd_table
        dq      fs_ent_buf
        dq      fs_name_buf
        dq      fs_bpb_buf
        dq      fs_sec_cache
        dq      fs_test_ibm_buf
        dq      fs_test_tetris_buf
        dq      fs_test_big_buf
        dq      fs_test_scratch
        dq      fs_test_dirents
        dq      fs_test_stat
        dq      fs_fat_cache
        dq      blk_read
        dq      blk_ready
        dq      blk_capacity
        dq      blk_test_reads
        dq      cpu_com1_puts
        dq      cpu_com1_puthex
        dq      FS_KERNEL_BASE
        dq      FS_PAGE_SIZE
        dq      FS_OK
        dq      FS_EINVAL
        dq      FS_ENOMEM
        dq      FS_ENOENT
        dq      FS_EEXIST
        dq      FS_EPERM
        dq      FS_ENOTSUP
        dq      FS_ENOTREADY
        dq      FS_EALREADY
        dq      FS_EIO
        dq      FS_EOVERFLOW
        dq      FS_EFAULT
        dq      FS_EBUSY
        dq      FS_SECTOR_SIZE
        dq      FS_SECTOR_SHIFT
        dq      FS_ENTRY_SIZE
        dq      FS_ENTRY_SHIFT
        dq      FS_ENTRIES_PER_SECTOR
        dq      FS_BPB_BPS
        dq      FS_BPB_SPC
        dq      FS_BPB_RESERVED
        dq      FS_BPB_NFATS
        dq      FS_BPB_ROOT_ENTRIES
        dq      FS_BPB_TOTAL16
        dq      FS_BPB_SPF
        dq      FS_BPB_TOTAL32
        dq      FS_BPB_SIG
        dq      FS_BPB_SIG_VALUE
        dq      FS_FAT16_MIN_CLUSTERS
        dq      FS_FAT16_MAX_CLUSTERS
        dq      FS_FAT16_BAD
        dq      FS_FAT16_EOC_MIN
        dq      FS_ATTR_READ_ONLY
        dq      FS_ATTR_VOLUME_ID
        dq      FS_ATTR_LFN
        dq      FS_ATTR_DIRECTORY
        dq      FS_DIR_END
        dq      FS_DIR_DELETED
        dq      FS_DIR_KANJI
        dq      FS_MAX_FDS
        dq      FS_FD_SIZE
        dq      FS_PATH_MAX
        dq      FS_COMP_MAX
        dq      FS_FD_IN_USE
        dq      FS_FD_IS_DIR
        dq      FS_FD_CLUSTER
        dq      FS_FD_SIZE_OFF
        dq      FS_FD_POS
        dq      FS_FAT_CACHE_SECTORS
        dq      FS_O_RDONLY
        dq      FS_O_WRONLY
        dq      FS_O_RDWR
        dq      FS_O_ACCMODE
        dq      FS_SEEK_SET
        dq      FS_SEEK_CUR
        dq      FS_SEEK_END
        dq      FS_DIRENT_SIZE
        dq      FS_STAT_SIZE
        dq      FS_TEST_IBM_SIZE
        dq      FS_TEST_TETRIS_SIZE
        dq      FS_TEST_BIG_SIZE
        dq      FS_TEST_ROM_PREFIX
        dq      FS_TEST_BIG_PREFIX
        dq      FS_TEST_SPC
        dq      FS_TEST_RESERVED
        dq      FS_TEST_NFATS
        dq      FS_TEST_SPF
        dq      FS_TEST_ROOT_ENTRIES
        dq      FS_TEST_CLUSTERS
        dq      FS_TEST_TOTAL
        dq      FS_TEST_FAT_LBA
        dq      FS_TEST_ROOT_LBA
        dq      FS_TEST_DATA_LBA
        dq      FS_ST_BPB
        dq      FS_ST_IBM
        dq      FS_ST_TETRIS
        dq      FS_ST_SEEK
        dq      FS_ST_NEG
        dq      FS_ST_BIG
        dq      FS_ST_STAT
        dq      FS_ST_COUNT
        dq      FS_TEST_READS_FINAL
        dq      FS_TEST_READS_FINAL_BIG
fs_probe_end:

if (fs_probe_end - fs_probe) <> 173 * 8
        err 'the manifest must keep its fixed entry count'
end if