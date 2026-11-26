;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Layout probe for the DESKTOP-v1/v2 shell module, tasks 96 and 124.
;;
;; This file is a test harness, not part of the kernel. It assembles
;; kernel/desktop/desktop.asm exactly as the kernel will include it:
;; behind cpu.asm (console, panic), mm.asm (vmm_map_mmio), irq.asm
;; (irq_sti_wait, the public tick evidence), input.asm (the IOAPIC
;; helper, the key buffer and the accumulated mouse coordinates) and
;; video.asm (the drawing primitives and the readback save path).
;; Nothing here is executed and no machine is started.
;;
;; DESKTOP_PROBE_ORG selects the link base; check.py assembles the
;; probe twice, once at 0 for byte offsets and once at the kernel base
;; to prove the module links high with the same internal layout.
;;
;; The manifest sits after desktop.asm: the module's own end assertion
;; runs at its include site and the data block is still the last module
;; byte. The manifest is a fixed list of qwords in the order check.py
;; expects; the magic and the entry count are checked before anything
;; else is read.
;;

use64

if defined DESKTOP_PROBE_ORG
        org DESKTOP_PROBE_ORG
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

; task 125: the standalone app_launch stub. The kernel resolves the
; real APP-v1 loader (kernel/app); this probe only needs the symbol
; so the desktop_launch_pump call target links. Nothing here runs.
app_launch:
        ret

DESKTOP_HOSTED = 1
include '../../kernel/desktop/desktop.asm'

; ------------------------------------------------------------------
; manifest, fixed order, read by check.py
; ------------------------------------------------------------------

align 8
desktop_probe:
        dq      'K64DTP1'                   ; magic
        dq      1                           ; manifest version
        dq      desktop_code_start
        dq      desktop_code_end
        dq      desktop_data_start
        dq      desktop_data_end
        dq      desktop_init
        dq      desktop_wait_count
        dq      desktop_pump
        dq      desktop_run
        dq      desktop_cursor_place
        dq      desktop_cursor_composite
        dq      desktop_cursor_sync
        dq      desktop_echo_pump
        dq      desktop_echo_key
        dq      desktop_selftest
        dq      desktop_title
        dq      desktop_title_end
        dq      desktop_cursor_sprite
        dq      desktop_cursor_sprite_end
        dq      desktop_state
        dq      desktop_test_selftest_mask
        dq      desktop_test_stage
        dq      desktop_test_keys
        dq      desktop_test_cursor_x
        dq      desktop_test_cursor_y
        dq      desktop_cursor_x
        dq      desktop_cursor_y
        dq      desktop_echo_col
        dq      desktop_echo_row
        dq      desktop_echo_drops
        dq      desktop_kbd_consumed
        dq      desktop_echo_char
        dq      desktop_gui_frame_stage
        dq      desktop_cursor_save
        dq      desktop_cursor_draw
        dq      msg_desktop_selftest
        dq      msg_desktop_nl
        dq      video_fill_rect
        dq      video_blit_rect
        dq      video_readback_rect
        dq      video_puts
        dq      video_fb_state
        dq      input_ioapic_write
        dq      input_kbd_buf
        dq      input_kbd_used
        dq      input_kbd_drops
        dq      input_mouse_x
        dq      input_mouse_y
        dq      input_test_kbd_count
        dq      input_test_mouse_count
        dq      irq_sti_wait
        dq      irq_test_ticks
        dq      cpu_com1_puts
        dq      cpu_com1_puthex
        dq      DESKTOP_BG_COLOR
        dq      DESKTOP_BAR_COLOR
        dq      DESKTOP_BAR_X
        dq      DESKTOP_BAR_Y
        dq      DESKTOP_BAR_W
        dq      DESKTOP_BAR_H
        dq      DESKTOP_TITLE_X
        dq      DESKTOP_TITLE_Y
        dq      DESKTOP_TITLE_COLOR
        dq      DESKTOP_TITLE_LEN
        dq      DESKTOP_SPRITE_W
        dq      DESKTOP_SPRITE_H
        dq      DESKTOP_SPRITE_BYTES
        dq      DESKTOP_CURSOR_X0
        dq      DESKTOP_CURSOR_Y0
        dq      DESKTOP_CURSOR_XMAX
        dq      DESKTOP_CURSOR_YMAX
        dq      DESKTOP_ECHO_X
        dq      DESKTOP_ECHO_Y
        dq      DESKTOP_ECHO_COLS
        dq      DESKTOP_ECHO_ROWS
        dq      DESKTOP_FONT_W
        dq      DESKTOP_FONT_H
        dq      DESKTOP_ECHO_COLOR
        dq      DESKTOP_KEY_ENTER
        dq      DESKTOP_KEY_BACKSPACE
        dq      DESKTOP_ASCII_FIRST
        dq      DESKTOP_ASCII_LAST
        dq      DESKTOP_ST_MOVE_DX
        dq      DESKTOP_ST_MOVE_DY
        dq      DESKTOP_ST_CURSOR_X
        dq      DESKTOP_ST_CURSOR_Y
        dq      DESKTOP_ST_KBD_FINAL
        dq      DESKTOP_ST_MOUSE_FINAL
        dq      DESKTOP_ST_KEYS_FINAL
        dq      DESKTOP_ST_KBD_USED_FINAL
        dq      DESKTOP_IRQ_TICKS_FINAL
        dq      DESKTOP_STAGE_ECHO_H
        dq      DESKTOP_STAGE_ECHO_I
        dq      DESKTOP_STAGE_MOUSE_MOVE
        dq      DESKTOP_STAGE_MOUSE_BUTTON
        dq      DESKTOP_STAGE_DONE
        dq      DESKTOP_ST_ALL
        dq      DESKTOP_POLL_CHUNK
        dq      DESKTOP_POLL_ROUNDS
        dq      DESKTOP_LAPIC_VA
        dq      DESKTOP_LAPIC_LVT_TIMER
        dq      DESKTOP_LVT_MASK
        dq      DESKTOP_IOAPIC_IDX_KBD_LO
        dq      DESKTOP_IOAPIC_IDX_MOUSE_LO
        dq      DESKTOP_RTE_KBD_MASKED
        dq      DESKTOP_RTE_MOUSE_MASKED
        dq      DESKTOP_RTE_KBD_LIVE
        dq      DESKTOP_RTE_MOUSE_LIVE
        dq      DESKTOP_DATA_OFF_TEST_MASK
        dq      DESKTOP_DATA_OFF_TEST_STAGE
        dq      DESKTOP_DATA_OFF_TEST_KEYS
        dq      DESKTOP_DATA_OFF_TEST_CURSOR_X
        dq      DESKTOP_DATA_OFF_TEST_CURSOR_Y
        dq      DESKTOP_DATA_OFF_CURSOR_SAVE
        dq      DESKTOP_DATA_OFF_CURSOR_DRAW
        dq      DESKTOP_DATA_OFF_MESSAGES
        dq      DESKTOP_SCREEN_W
        dq      DESKTOP_SCREEN_H
        ; DESKTOP-v2 appendix, task 124: pure tail extension, the 119
        ; frozen v1 entries above keep their exact order
        dq      desktop_click_pump
        dq      desktop_menu_open
        dq      desktop_menu_close
        dq      desktop_menu_text
        dq      desktop_menu_text_end
        dq      desktop_wallpaper
        dq      desktop_wallpaper_end
        dq      desktop_prev_buttons
        dq      desktop_menu_flag
        dq      desktop_app_launch_requested
        dq      desktop_test_clicks
        dq      desktop_test_menu_opens
        dq      desktop_menu_save
        dq      input_mouse_b1
        dq      DESKTOP_START_X
        dq      DESKTOP_START_Y
        dq      DESKTOP_START_W
        dq      DESKTOP_START_H
        dq      DESKTOP_MENU_X
        dq      DESKTOP_MENU_Y
        dq      DESKTOP_MENU_W
        dq      DESKTOP_MENU_H
        dq      DESKTOP_ENTRY_CX
        dq      DESKTOP_ENTRY_CY
        dq      DESKTOP_MENU_COLOR
        dq      DESKTOP_ACCENT_COLOR
        dq      DESKTOP_MENU_TEXT_X
        dq      DESKTOP_MENU_TEXT_Y
        dq      DESKTOP_MENU_TEXT_LEN
        dq      DESKTOP_DATA_OFF_PREV_BUTTONS
        dq      DESKTOP_DATA_OFF_MENU_FLAG
        dq      DESKTOP_DATA_OFF_APP_LAUNCH
        dq      DESKTOP_DATA_OFF_TEST_CLICKS
        dq      DESKTOP_DATA_OFF_TEST_MENU_OPENS
        dq      DESKTOP_DATA_OFF_MENU_SAVE
        dq      DESKTOP_MENU_SAVE_BYTES
        dq      DESKTOP_WALLPAPER_W
        dq      DESKTOP_WALLPAPER_H
        dq      DESKTOP_WALLPAPER_BYTES
        dq      DESKTOP_WALLPAPER_SHA0
        dq      DESKTOP_WALLPAPER_SHA1
        dq      DESKTOP_WALLPAPER_SHA2
        dq      DESKTOP_WALLPAPER_SHA3
        ; task 125 appendix: the app launch wiring, pure tail extension,
        ; the 162 frozen v1/v2 entries above keep their exact order
        dq      desktop_repaint_base
        dq      desktop_launch_pump
        dq      desktop_app_boot
        dq      app_launch
        dq      desktop_test_start_clicks
        dq      desktop_test_entry_clicks
        dq      desktop_test_app_runs
        dq      desktop_test_app_exits
        dq      DESKTOP_STAGE_START_CLICK
        dq      DESKTOP_STAGE_ENTRY_CLICK
        dq      DESKTOP_STAGE_APP_RUNNING
        dq      DESKTOP_STAGE_APP_EXIT
        dq      DESKTOP_RUN_X
        dq      DESKTOP_RUN_Y
        dq      DESKTOP_RUN_W
        dq      DESKTOP_RUN_H
        dq      DESKTOP_RUN_COLOR
        dq      DESKTOP_DATA_OFF_TEST_START_CLICKS
        dq      DESKTOP_DATA_OFF_TEST_ENTRY_CLICKS
        dq      DESKTOP_DATA_OFF_TEST_APP_RUNS
        dq      DESKTOP_DATA_OFF_TEST_APP_EXITS
desktop_probe_end:

if (desktop_probe_end - desktop_probe) <> 183 * 8
        err 'the manifest must keep its fixed entry count'
end if
