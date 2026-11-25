;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Layout probe for the DESKTOP-v1 shell module, task 96.
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
desktop_probe_end:

if (desktop_probe_end - desktop_probe) <> 119 * 8
        err 'the manifest must keep its fixed entry count'
end if
