;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2026. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; APP-v1 mutable data region. Every byte is a real zero byte in the
;; image (db dup, never rb). The region starts page aligned and ends
;; page aligned, 22 pages total (api.inc layout table):
;;   page 0      state, the resolve record, the stat buffer
;;   pages 1-2   the 516 entry leaf frame array
;;   page 3      the BLIT row bounce page
;;   pages 4-19  the echo first line save rectangle
;;   page 20     the fsprobe guest evidence snapshot (fsys_bounce copy)
;;   page 21     the private SYSCALL stack, its top is app_data_end
;; Nothing may follow it; the foundation maps the whole region RW/NX.
;;

align 4096
app_data_start:

; module, handshake and handler state, one qword each
app_ready:           dq 0         ; +0    1 after a successful app_init
app_stage:           dq 0         ; +8    host handshake, APP_STAGE_*
app_exit_code:       dq 0         ; +16   the program SYS_EXIT argument
app_fault_vector:    dq 0         ; +24   ring 3 fault vector + 1, 0 = none
app_blit_count:      dq 0         ; +32   successful BLIT calls
app_key_count:       dq 0         ; +40   real keys delivered by READ_KEY
app_poll_count:      dq 0         ; +48   POLL calls served
app_launch_rsp:      dq 0         ; +56   parked kernel stack pointer
app_launch_rip:      dq 0         ; +64   parked kernel resume address
app_saved_rsp0:      dq 0         ; +72   TSS.RSP0 before the launch
app_saved_fs_reads:  dq 0         ; +80   fs_test_reads snapshot
app_saved_blk_reads: dq 0         ; +88   blk_test_reads snapshot
app_table_base:      dq 0         ; +96   table_allocs before the maps
app_drops_base:      dq 0         ; +104  table_drops before the maps
app_table_delta:     dq 0         ; +112  published tables, must be 3
app_table_released:  dq 0         ; +120  tables reclaimed, budget on OK
app_pages_held:      dq 0         ; +128  frames held, 0 after teardown
app_image_size:      dq 0         ; +136  CHIP8.BIN bytes read from the FS
app_kbd_consumed:    dq 0         ; +144  READ_KEY cursor into input_kbd_buf
app_interactive:     dq 0         ; +152  1 after the Enter make was seen
app_fd_swept:        dq 0         ; +160  fds closed by the post-exit sweep
app_echo_saved:      dq 0         ; +168  1 when the echo line was saved

; the mm_vmm_resolve result record, 64 bytes
app_resolve_rec:     db 64 dup 0  ; +176

; the fs_stat output buffer for the program size gate
app_stat_buf:        db FS_STAT_SIZE dup 0   ; +240
if defined K64_RUNTIME
APP_TASK_STATE_SIZE = app_resolve_rec - app_stage
end if

; tail padding of the state page, emitted as real zero bytes
        db (4096 - (($ - app_data_start) and 4095)) and 4095 dup 0

; pages 1-2: the 516 leaf frames, physical addresses
app_pages:           dq APP_LAUNCH_PAGES dup 0
        db (8192 - (APP_LAUNCH_PAGES * 8)) dup 0

; page 3: the BLIT row bounce page
app_bounce:          db 4096 dup 0

; pages 4-19: the echo first line save, 64512 bytes + tail padding
app_echo_save:       db APP_ECHO_SAVE_BYTES dup 0
        db (65536 - APP_ECHO_SAVE_BYTES) dup 0

; page 20: the fsprobe guest evidence snapshot. app_launch copies the
; fsys_bounce probe snapshot here BEFORE the hosted program runs: the
; program's own file syscalls reuse fsys_bounce as their payload page
; and would otherwise clobber the guest evidence the QEMU judge reads.
app_fs_snapshot:     db 4096 dup 0

; page 21: the private SYSCALL stack, growing down from app_data_end
app_syscall_stack:   db APP_SYSCALL_STACK dup 0
app_syscall_stack_top:
if defined K64_RUNTIME
app_task_running:     dq 0
app_task_host_rsp:    dq 0
app_task_host_rsp0:   dq 0
app_task_aperture:    dq 0
app_tasks_ready:      db 0
app_window_dirty:    db 0
        align 8
app_window_edge_read: dq 0
app_window_front:     dq 0
app_window_drag_owner: dq 0
app_window_row:      dd APP_SCREEN_W dup 0
        align 16
app_task_host_fx:
        db APP_TASK_FX_SIZE dup 0
app_task_slots:      db APP_TASK_SIZE * DESKTOP_APP_COUNT dup 0
        db (4096 - (($ - app_data_start) and 4095)) and 4095 dup 0
end if
app_data_end:

if (app_data_end - app_data_start) mod 4096
        display "app data block is not page aligned", 13, 10
        err
end if
if app_syscall_stack_top - app_data_start <> APP_DATA_PAGES_TOTAL * 4096
        display "app data budget changed from the frozen 22 pages", 13, 10
        err
end if
if (app_pages - app_data_start) <> 4096
        display "the frame array must start at the second page", 13, 10
        err
end if
if (app_bounce - app_data_start) <> 3 * 4096
        display "the bounce page must be the fourth page", 13, 10
        err
end if
if (app_echo_save - app_data_start) <> 4 * 4096
        display "the echo save must start at the fifth page", 13, 10
        err
end if
if (app_fs_snapshot - app_data_start) <> 20 * 4096
        display "the fsprobe snapshot must be the twenty-first page", 13, 10
        err
end if
if (app_syscall_stack - app_data_start) <> 21 * 4096
        display "the syscall stack must be the last page", 13, 10
        err
end if
if ~ defined K64_RUNTIME
        if app_syscall_stack_top <> app_data_end
                display "the syscall stack top must close the region", 13, 10
                err
        end if
end if
if app_syscall_stack_top and 15
        display "the syscall stack top must be 16 byte aligned", 13, 10
        err
end if
if app_resolve_rec - app_data_start <> 176
        display "app_resolve_rec must sit at +176", 13, 10
        err
end if
if app_stat_buf - app_data_start <> 240
        display "app_stat_buf must sit at +240", 13, 10
        err
end if
