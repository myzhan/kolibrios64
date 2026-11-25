;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2026. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Layout probe for the stage 2 fsys module, task 133, contract
;; FS-v1 M3.
;;
;; This file is a test harness, not part of the kernel. It assembles
;; kernel/fsys/fsys.asm exactly as the kernel will include it: behind
;; cpu.asm (frame layout, syscall registration, RSP0), mm.asm (resolve,
;; user mapper, allocator), blk.asm (the blk_read seam) and fs.asm
;; (the six frozen file calls). Nothing here is executed and no
;; machine is started.
;;
;; FSYS_PROBE_ORG selects the link base; check.py assembles the probe
;; twice, once at 0 for byte offsets and once at the kernel base to
;; prove the module links high with the same internal layout.
;;
;; The manifest sits after fsys.asm: every module's own end assertion
;; runs at its include site and the fsys data block is still the last
;; module byte. The manifest is a fixed list of qwords in the order
;; check.py expects; the magic and the entry count are checked before
;; anything else is read.
;;

use64

if defined FSYS_PROBE_ORG
        org FSYS_PROBE_ORG
else
        org 0
end if

include '../../kernel/cpu/cpu.asm'

MM_HOSTED = 1
include '../../kernel/mm/mm.asm'

BLK_HOSTED = 1
include '../../kernel/blk/blk.asm'

FS_HOSTED = 1
include '../../kernel/fs/fs.asm'

FSYS_HOSTED = 1
include '../../kernel/fsys/fsys.asm'

; ------------------------------------------------------------------
; manifest, fixed order, read by check.py
; ------------------------------------------------------------------

align 8
fsys_probe_manifest:
        dq      'K64FSY1'                 ; magic
        dq      1                         ; manifest version
        dq      fsys_map_error
        dq      fsys_check_range
        dq      fsys_copy_path
        dq      fsys_copy_to_user
        dq      fsys_h_open
        dq      fsys_h_read
        dq      fsys_h_close
        dq      fsys_h_seek
        dq      fsys_h_stat
        dq      fsys_h_readdir
        dq      fsys_h_exit
        dq      fsys_init
        dq      fsys_probe
        dq      fsys_errmap
        dq      fsys_errmap_end
        dq      fsys_probe_image
        dq      fsys_probe_image_end
        dq      fsys_data_start
        dq      fsys_data_end
        dq      fsys_ready
        dq      fsys_probe_rsp
        dq      fsys_probe_rip
        dq      fsys_exit_code
        dq      fsys_saved_rsp0
        dq      fsys_saved_fs_reads
        dq      fsys_saved_blk_reads
        dq      fsys_table_base
        dq      fsys_table_delta
        dq      fsys_probe_mask
        dq      fsys_probe_fail
        dq      fsys_resolve_rec
        dq      fsys_path_buf
        dq      fsys_stat_buf
        dq      fsys_pages
        dq      fsys_probe_evidence
        dq      fsys_pages_held
        dq      fsys_drops_base
        dq      fsys_table_released
        dq      fsys_bounce
        dq      fsys_syscall_stack
        dq      fsys_syscall_stack_top
        dq      fs_open
        dq      fs_read
        dq      fs_seek
        dq      fs_close
        dq      fs_stat
        dq      fs_readdir
        dq      fs_test_reads
        dq      blk_test_reads
        dq      mm_vmm_resolve
        dq      vmm_map_user_page
        dq      pmm_alloc_page
        dq      vmm_unmap_user_page
        dq      pmm_free_page
        dq      mm_vmm_state
        dq      cpu_syscall_set_handler
        dq      cpu_set_rsp0
        dq      cpu_tss_rsp0
        dq      cpu_syscall_handlers
        dq      FSYS_SYS_EXIT
        dq      FSYS_SYS_OPEN
        dq      FSYS_SYS_READ
        dq      FSYS_SYS_CLOSE
        dq      FSYS_SYS_SEEK
        dq      FSYS_SYS_STAT
        dq      FSYS_SYS_READDIR
        dq      FSYS_OK
        dq      FSYS_EINVAL
        dq      FSYS_ENOMEM
        dq      FSYS_ENOENT
        dq      FSYS_EPERM
        dq      FSYS_ENOTSUP
        dq      FSYS_ENOTREADY
        dq      FSYS_EALREADY
        dq      FSYS_EIO
        dq      FSYS_EFAULT
        dq      FSYS_USER_VA_BASE
        dq      FSYS_USER_VA_LIMIT
        dq      FSYS_USER_CODE_BASE
        dq      FSYS_USER_CODE_SIZE
        dq      FSYS_USER_DATA_BASE
        dq      FSYS_USER_STACK_TOP
        dq      FSYS_USER_STACK_SIZE
        dq      FSYS_CODE_PAGES
        dq      FSYS_DATA_PAGES
        dq      FSYS_STACK_PAGES
        dq      FSYS_PROBE_PAGES
        dq      FSYS_TABLE_BUDGET
        dq      FSYS_FP_MASK_ALL
        dq      FSYS_FP_EVIDENCE
        dq      FSYS_USER_CS
        dq      FSYS_USER_SS
        dq      FSYS_USER_RFLAGS
        dq      FSYS_DATA_PAGES_TOTAL
        dq      FSYS_BOUNCE_SIZE
        dq      FSYS_SYSCALL_STACK
        dq      CPU_SYS_CALL_MAX
        dq      CPU_FRAME_RDI
        dq      CPU_FRAME_RSI
        dq      CPU_FRAME_RDX
        dq      CPU_FRAME_RAX
        dq      CPU_FRAME_SIZE
        dq      MM_MAP_US
        dq      MM_MAP_RW
        dq      MM_MAP_NX
        dq      MM_USER_TABLE_BUDGET
        dq      MM_VMM_STATE.table_allocs
        dq      MM_VMM_STATE.table_drops
        dq      FS_MAX_FDS
        dq      FS_PATH_MAX
        dq      FS_STAT_SIZE
        dq      FS_DIRENT_SIZE
        dq      FS_SEEK_END
        dq      FS_KERNEL_BASE
fsys_probe_manifest_end:

if (fsys_probe_manifest_end - fsys_probe_manifest) <> 116 * 8
        err 'the manifest must keep its fixed entry count'
end if