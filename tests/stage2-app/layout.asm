;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2026. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Layout probe for the stage 2 app module, task 115, contract APP-v1.
;;
;; This file is a test harness, not part of the kernel. It assembles
;; kernel/app/app.asm exactly as the kernel will include it: behind
;; cpu.asm (frame layout, syscall/fault registration, RSP0), mm.asm
;; (resolve, user mapper, allocator), irq/input (the public evidence
;; labels, the IOAPIC helper), video/desktop (the draw primitives, the
;; cursor coordination), blk/fs (the frozen file calls) and fsys (the
;; resident file syscall adapters). Nothing here is executed and no
;; machine is started.
;;
;; APP_PROBE_ORG selects the link base; check.py assembles the probe
;; twice, once at 0 for byte offsets and once at the kernel base to
;; prove the module links high with the same internal layout.
;;
;; The manifest sits after app.asm: every module's own end assertion
;; runs at its include site and the app data block is still the last
;; module byte. The manifest is a fixed list of qwords in the order
;; check.py expects; the magic, the version and the entry count are
;; checked before anything else is read.
;;

use64

if defined APP_PROBE_ORG
        org APP_PROBE_ORG
else
        org 0
end if

include '../../kernel/cpu/cpu.asm'

MM_HOSTED = 1
include '../../kernel/mm/mm.asm'

include '../../kernel/irq/irq.asm'

INPUT_HOSTED = 1
include '../../kernel/input/input.asm'

VIDEO_HOSTED = 1
include '../../kernel/video/video.asm'

DESKTOP_HOSTED = 1
include '../../kernel/desktop/desktop.asm'

BLK_HOSTED = 1
include '../../kernel/blk/blk.asm'

FS_HOSTED = 1
include '../../kernel/fs/fs.asm'

FSYS_HOSTED = 1
include '../../kernel/fsys/fsys.asm'

APP_HOSTED = 1
include '../../kernel/app/app.asm'

; ------------------------------------------------------------------
; manifest, fixed order, read by check.py
; ------------------------------------------------------------------

align 8
app_probe_manifest:
        dq      'K64APP1'                 ; magic
        dq      1                         ; manifest version
        dq      (app_probe_manifest_end - app_probe_manifest) / 8
        dq      app_check_range
        dq      app_copy_from_user
        dq      app_h_exit
        dq      app_h_fault
        dq      app_h_blit
        dq      app_h_read_key
        dq      app_h_poll
        dq      app_init
        dq      app_redraw_desktop
        dq      app_launch
        dq      app_program_path
        dq      app_program_path_end
        dq      app_selftest
        dq      app_data_start
        dq      app_data_end
        dq      app_ready
        dq      app_stage
        dq      app_exit_code
        dq      app_fault_vector
        dq      app_blit_count
        dq      app_key_count
        dq      app_poll_count
        dq      app_launch_rsp
        dq      app_launch_rip
        dq      app_saved_rsp0
        dq      app_saved_fs_reads
        dq      app_saved_blk_reads
        dq      app_table_base
        dq      app_drops_base
        dq      app_table_delta
        dq      app_table_released
        dq      app_pages_held
        dq      app_image_size
        dq      app_kbd_consumed
        dq      app_interactive
        dq      app_fd_swept
        dq      app_echo_saved
        dq      app_resolve_rec
        dq      app_stat_buf
        dq      app_pages
        dq      app_bounce
        dq      app_echo_save
        dq      app_fs_snapshot
        dq      app_syscall_stack
        dq      app_syscall_stack_top
        dq      fs_open
        dq      fs_read
        dq      fs_close
        dq      fs_stat
        dq      fs_fd_table
        dq      fs_test_reads
        dq      blk_test_reads
        dq      mm_vmm_resolve
        dq      vmm_map_user_page
        dq      vmm_unmap_user_page
        dq      pmm_alloc_page
        dq      pmm_free_page
        dq      mm_vmm_state
        dq      cpu_syscall_set_handler
        dq      cpu_user_fault_set_handler
        dq      cpu_set_rsp0
        dq      cpu_tss_rsp0
        dq      cpu_syscall_handlers
        dq      video_fill_rect
        dq      video_blit_rect
        dq      video_readback_rect
        dq      video_puts
        dq      video_fb_state
        dq      desktop_cursor_sync
        dq      desktop_cursor_place
        dq      desktop_repaint_base
        dq      desktop_title
        dq      desktop_state
        dq      desktop_echo_row
        dq      desktop_cursor_x
        dq      desktop_cursor_y
        dq      input_kbd_buf
        dq      input_kbd_used
        dq      input_mouse_x
        dq      input_mouse_y
        dq      input_mouse_b1
        dq      input_ioapic_write
        dq      irq_test_ticks
        dq      fsys_ready
        dq      fs_ready
        dq      APP_SYS_EXIT
        dq      APP_SYS_BLIT
        dq      APP_SYS_READ_KEY
        dq      APP_SYS_POLL
        dq      APP_OK
        dq      APP_EINVAL
        dq      APP_ENOMEM
        dq      APP_ENOENT
        dq      APP_EPERM
        dq      APP_ENOTSUP
        dq      APP_ENOTREADY
        dq      APP_EALREADY
        dq      APP_EIO
        dq      APP_EFAULT
        dq      APP_USER_VA_BASE
        dq      APP_USER_VA_LIMIT
        dq      APP_USER_CODE_BASE
        dq      APP_USER_CODE_SIZE
        dq      APP_USER_DATA_BASE
        dq      APP_USER_DATA_SIZE
        dq      APP_USER_STACK_TOP
        dq      APP_USER_STACK_SIZE
        dq      APP_CODE_PAGES
        dq      APP_DATA_PAGES
        dq      APP_STACK_PAGES
        dq      APP_LAUNCH_PAGES
        dq      APP_TABLE_BUDGET
        dq      APP_USER_CS
        dq      APP_USER_SS
        dq      APP_USER_RFLAGS
        dq      APP_BLIT_DST_X
        dq      APP_BLIT_DST_Y
        dq      APP_FB_WIDTH
        dq      APP_FB_HEIGHT
        dq      APP_SCREEN_W
        dq      APP_SCREEN_H
        dq      APP_STAGE_IDLE
        dq      APP_STAGE_IBM_READY
        dq      APP_STAGE_EXIT_REQ
        dq      APP_STAGE_DONE
        dq      APP_SC_ESC
        dq      APP_SC_ENTER
        dq      APP_ASCII_ESC
        dq      APP_POLL_CHUNK
        dq      APP_IPOLL_CHUNK
        dq      APP_LAPIC_VA
        dq      APP_LAPIC_LVT_TIMER
        dq      APP_LVT_MASK
        dq      APP_IOAPIC_IDX_KBD_LO
        dq      APP_IOAPIC_IDX_MOUSE_LO
        dq      APP_RTE_KBD_MASKED
        dq      APP_RTE_MOUSE_MASKED
        dq      APP_RTE_KBD_LIVE
        dq      APP_RTE_MOUSE_LIVE
        dq      APP_ECHO_SAVE_X
        dq      APP_ECHO_SAVE_Y
        dq      APP_ECHO_SAVE_W
        dq      APP_ECHO_SAVE_H
        dq      APP_ECHO_SAVE_BYTES
        dq      APP_BOUNCE_ROW
        dq      APP_PROGRAM_PATH_MAX
        dq      APP_DATA_PAGES_TOTAL
        dq      APP_SYSCALL_STACK
        dq      CPU_SYS_CALL_MAX
        dq      CPU_FRAME_RDI
        dq      CPU_FRAME_RSI
        dq      CPU_FRAME_RDX
        dq      CPU_FRAME_R10
        dq      CPU_FRAME_R8
        dq      CPU_FRAME_RAX
        dq      CPU_FRAME_VECTOR
        dq      CPU_FRAME_SIZE
        dq      MM_MAP_US
        dq      MM_MAP_RW
        dq      MM_MAP_NX
        dq      MM_USER_TABLE_BUDGET
        dq      MM_VMM_STATE.table_allocs
        dq      MM_VMM_STATE.table_drops
        dq      MM_ENOENT
        dq      FS_MAX_FDS
        dq      FS_STAT_SIZE
        dq      FS_FD_SIZE
        dq      FS_FD_IN_USE
        dq      DESKTOP_BG_COLOR
        dq      DESKTOP_BAR_COLOR
        dq      DESKTOP_BAR_W
        dq      DESKTOP_BAR_H
        dq      DESKTOP_TITLE_X
        dq      DESKTOP_TITLE_Y
        dq      DESKTOP_TITLE_COLOR
        dq      VIDEO_FB_STATE.ready
        dq      FS_KERNEL_BASE
        dq      input_kbd_head
        dq      desktop_kbd_consumed
app_probe_manifest_end:

if (app_probe_manifest_end - app_probe_manifest) <> 182 * 8
        err 'the manifest must keep its fixed entry count'
end if
