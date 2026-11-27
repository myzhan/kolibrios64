;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; All mutable data of the desktop module, the last bytes it emits.
;;
;; The region starts and ends 4 KiB aligned, every byte is really emitted
;; (dq 0 / dq ... dup 0, never rb) and starts as zero, so the flat binary
;; carries the whole block and the layout test can prove it. The labels
;; are public facts: the gdbstub smoke reads desktop_test_selftest_mask,
;; desktop_test_stage, desktop_test_keys and desktop_test_cursor_x/y
;; straight out of guest memory.
;;
;; The sprite pattern and the title text are constants and live in
;; cursor.inc / echo.inc (code side), the same split the video module
;; uses for its font: the data region stays all zero at load time.
;; The save/draw buffers hold packed BGRX dwords, one dword per sprite
;; pixel, exactly the compact layout video_blit_rect consumes and
;; video_readback_rect produces (12 dword rows of 18).
;;

align DESKTOP_DATA_ALIGN
desktop_data_start:

desktop_state:              dq 0        ; desktop_init succeeded once
desktop_test_selftest_mask: dq 0        ; failed leg bits, 0 on success
desktop_test_stage:         dq 0        ; host handshake, ends at 5
desktop_test_keys:          dq 0        ; echoed printable chars, frozen at 2
desktop_test_cursor_x:      dq 0        ; clamped cursor X evidence, frozen 557
desktop_test_cursor_y:      dq 0        ; clamped cursor Y evidence, frozen 411
desktop_cursor_x:           dq 0        ; the drawn cursor position X
desktop_cursor_y:           dq 0        ; the drawn cursor position Y
desktop_echo_col:           dq 0        ; echo text column, 0..125
desktop_echo_row:           dq 0        ; echo text row, 0..44 (44 = full)
desktop_echo_drops:         dq 0        ; chars dropped on a full screen
desktop_kbd_consumed:       dq 0        ; private input buffer consumed index
desktop_echo_char:          dq 0        ; one char scratch cell for video_puts
; task 102: the foundation GUI frame evidence handshake lives in the
; first reserved slot: 1 = pattern frame ready, host captures it and
; writes 2 = release, the guest then publishes 3 = done. The desktop
; module itself never touches it; the layout and size stay frozen.
desktop_gui_frame_stage:    dq 0        ; foundation GUI frame handshake
desktop_reserved:           dq 4 dup 0  ; 112..143, keep the buffers aligned

; the cursor background save and the composited draw buffer, each one
; packed sprite rectangle: DESKTOP_SPRITE_BYTES dwords of BGRX
desktop_cursor_save:        dd DESKTOP_SPRITE_BYTES dup 0
desktop_cursor_draw:        dd DESKTOP_SPRITE_BYTES dup 0

; diagnostics, ASCII only, printed on the failure path only
msg_desktop_selftest  db 'K64-DESKTOP-SELFTEST failed legs = ', 0
msg_desktop_nl        db 13, 10, 0

; one alignment byte: the v1 messages end at 1911, the v2 qword block
; is pinned to the 8 byte boundary 1912
        db 0

; DESKTOP-v2 state, appended AFTER the frozen v1 block (0..1911):
; the private button edge memory, the menu flag, the launch request
; label task 125 wires to the loader, and the two click evidences
desktop_prev_buttons:         dq 0      ; 1912, input_mouse_b1 & 1 edge memory
desktop_menu_flag:            dq 0      ; 1920, 1 while the menu is drawn
desktop_app_launch_requested: dq 0      ; 1928, CHIP-8 entry clicked (task 125)
desktop_test_clicks:          dq 0      ; 1936, press edges, frozen 1 after ST
desktop_test_menu_opens:      dq 0      ; 1944, menu opens, frozen 0 after ST

; task 125 evidence, appended AFTER the frozen v2 qwords: the start
; button and CHIP-8 entry press edges, the app_launch invocations and
; the clean teardowns (a relaunch after ESC advances them again)
desktop_test_start_clicks:    dq 0      ; 1952, start button press edges
desktop_test_entry_clicks:    dq 0      ; 1960, CHIP-8 entry press edges
desktop_test_app_runs:        dq 0      ; 1968, app_launch invocations
desktop_test_app_exits:       dq 0      ; 1976, clean app teardowns

; page 1 tail padding, emitted as data so every zero byte stays real
        db (DESKTOP_PAGE_SIZE - (($ - desktop_data_start) and (DESKTOP_PAGE_SIZE - 1))) and (DESKTOP_PAGE_SIZE - 1) dup 0

; data page 2: the menu background save, one packed BGRX dword per
; pixel of the frozen (0,32,200,34) rectangle, exactly the compact
; layout video_readback_rect produces and video_blit_rect consumes
desktop_menu_save:            dd (DESKTOP_MENU_W * DESKTOP_MENU_ACTIVE_H) dup 0

; tail padding, emitted as data so the file keeps every zero byte
        db (DESKTOP_PAGE_SIZE - (($ - desktop_data_start) and (DESKTOP_PAGE_SIZE - 1))) and (DESKTOP_PAGE_SIZE - 1) dup 0
if defined K64_RUNTIME
desktop_echo_text:            db (DESKTOP_ECHO_ROWS * (DESKTOP_ECHO_COLS + 1)) dup 0
        db (DESKTOP_PAGE_SIZE - (($ - desktop_data_start) and (DESKTOP_PAGE_SIZE - 1))) and (DESKTOP_PAGE_SIZE - 1) dup 0
end if
desktop_data_end:

if (desktop_data_start and (DESKTOP_DATA_ALIGN - 1)) <> 0
        err 'the desktop data region must start page aligned'
end if
if (desktop_data_end - desktop_data_start) mod DESKTOP_PAGE_SIZE
        err 'the desktop data region must end page aligned'
end if
if desktop_state - desktop_data_start <> DESKTOP_DATA_OFF_STATE
        err 'DESKTOP data state label offset'
end if
if desktop_test_selftest_mask - desktop_data_start <> DESKTOP_DATA_OFF_TEST_MASK
        err 'DESKTOP data selftest mask label offset'
end if
if desktop_test_stage - desktop_data_start <> DESKTOP_DATA_OFF_TEST_STAGE
        err 'DESKTOP data handshake stage label offset'
end if
if desktop_test_keys - desktop_data_start <> DESKTOP_DATA_OFF_TEST_KEYS
        err 'DESKTOP data echoed keys label offset'
end if
if desktop_test_cursor_x - desktop_data_start <> DESKTOP_DATA_OFF_TEST_CURSOR_X
        err 'DESKTOP data cursor X evidence label offset'
end if
if desktop_test_cursor_y - desktop_data_start <> DESKTOP_DATA_OFF_TEST_CURSOR_Y
        err 'DESKTOP data cursor Y evidence label offset'
end if
if desktop_cursor_x - desktop_data_start <> DESKTOP_DATA_OFF_CURSOR_X
        err 'DESKTOP data cursor X label offset'
end if
if desktop_cursor_y - desktop_data_start <> DESKTOP_DATA_OFF_CURSOR_Y
        err 'DESKTOP data cursor Y label offset'
end if
if desktop_echo_col - desktop_data_start <> DESKTOP_DATA_OFF_ECHO_COL
        err 'DESKTOP data echo column label offset'
end if
if desktop_echo_row - desktop_data_start <> DESKTOP_DATA_OFF_ECHO_ROW
        err 'DESKTOP data echo row label offset'
end if
if desktop_echo_drops - desktop_data_start <> DESKTOP_DATA_OFF_ECHO_DROPS
        err 'DESKTOP data echo drop count label offset'
end if
if desktop_kbd_consumed - desktop_data_start <> DESKTOP_DATA_OFF_KBD_CONSUMED
        err 'DESKTOP data consumed index label offset'
end if
if desktop_echo_char - desktop_data_start <> DESKTOP_DATA_OFF_ECHO_CHAR
        err 'DESKTOP data echo scratch label offset'
end if
if desktop_gui_frame_stage - desktop_data_start <> DESKTOP_DATA_OFF_GUI_FRAME
        err 'DESKTOP data GUI frame handshake label offset'
end if
if desktop_cursor_save - desktop_data_start <> DESKTOP_DATA_OFF_CURSOR_SAVE
        err 'DESKTOP data cursor save buffer label offset'
end if
if desktop_cursor_draw - desktop_data_start <> DESKTOP_DATA_OFF_CURSOR_DRAW
        err 'DESKTOP data cursor draw buffer label offset'
end if
if msg_desktop_selftest - desktop_data_start <> DESKTOP_DATA_OFF_MESSAGES
        err 'the DESKTOP messages must follow the draw buffer'
end if
if desktop_prev_buttons - desktop_data_start <> DESKTOP_DATA_OFF_PREV_BUTTONS
        err 'DESKTOP v2 prev_buttons label offset'
end if
if desktop_menu_flag - desktop_data_start <> DESKTOP_DATA_OFF_MENU_FLAG
        err 'DESKTOP v2 menu flag label offset'
end if
if desktop_app_launch_requested - desktop_data_start <> DESKTOP_DATA_OFF_APP_LAUNCH
        err 'DESKTOP v2 launch request label offset'
end if
if desktop_test_clicks - desktop_data_start <> DESKTOP_DATA_OFF_TEST_CLICKS
        err 'DESKTOP v2 click evidence label offset'
end if
if desktop_test_menu_opens - desktop_data_start <> DESKTOP_DATA_OFF_TEST_MENU_OPENS
        err 'DESKTOP v2 menu open evidence label offset'
end if
if desktop_test_start_clicks - desktop_data_start <> DESKTOP_DATA_OFF_TEST_START_CLICKS
        err 'DESKTOP task 125 start click evidence label offset'
end if
if desktop_test_entry_clicks - desktop_data_start <> DESKTOP_DATA_OFF_TEST_ENTRY_CLICKS
        err 'DESKTOP task 125 entry click evidence label offset'
end if
if desktop_test_app_runs - desktop_data_start <> DESKTOP_DATA_OFF_TEST_APP_RUNS
        err 'DESKTOP task 125 app run evidence label offset'
end if
if desktop_test_app_exits - desktop_data_start <> DESKTOP_DATA_OFF_TEST_APP_EXITS
        err 'DESKTOP task 125 app exit evidence label offset'
end if
if desktop_menu_save - desktop_data_start <> DESKTOP_DATA_OFF_MENU_SAVE
        err 'the DESKTOP v2 menu save must own data page 2'
end if
if defined K64_RUNTIME
        if desktop_echo_text - desktop_menu_save < DESKTOP_MENU_W * DESKTOP_MENU_ACTIVE_H * 4
                err 'runtime echo storage overlaps the menu backing buffer'
        end if
else if desktop_data_end - desktop_data_start <> DESKTOP_DATA_PAGES * DESKTOP_PAGE_SIZE
        err 'the DESKTOP v2 data region is exactly 8 pages'
end if
if desktop_data_end <= desktop_data_start
        err 'the desktop data region must not be empty'
end if
