;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Desktop shell, DESKTOP-v2: the click pump and the start menu.
;;
;; The click path reads input_mouse_b1 & 1 and edge-detects against
;; the desktop PRIVATE desktop_prev_buttons qword: the input module
;; layout stays frozen, nothing outside the desktop data region is
;; written. A press edge inside the start button (0,0,64,32) toggles
;; the menu; a press inside the open menu rectangle (0,32,200,34) is
;; the single CHIP-8 entry: it sets desktop_app_launch_requested (the
;; private flag desktop_launch_pump consumes, task 125) and closes the
;; menu; a press anywhere else closes an open menu. Releases only
;; update the edge state. Task 125: the start button and entry press
;; edges advance desktop_test_start_clicks / desktop_test_entry_clicks
;; and publish the START_CLICK / ENTRY_CLICK handshake stages.
;;
;; The menu background is saved through video_readback_rect into the
;; desktop data region page 2 (desktop_menu_save, 200*34 packed BGRX
;; dwords) before the first menu pixel is drawn, and restored through
;; video_blit_rect on close. All drawing runs in the main loop with
;; IF = 0 through desktop_pump, never in interrupt context.
;;
;; The frozen redraw order of desktop_pump is: cursor OLD position
;; restore -> desktop_click_pump (menu/taskbar) -> desktop_echo_pump
;; -> cursor NEW position place. desktop_click_pump returns eax = 1
;; when it redrew the menu so the pump re-places the cursor even
;; without a move (the menu may have appeared/vanished under it).
;;

; ------------------------------------------------------------------
; consume the left button edge and drive the menu
; in
; nothing; reads input_mouse_b1 / input_mouse_x / input_mouse_y and
; the private desktop_prev_buttons / desktop_menu_flag
; out
; eax = 1 when the menu was opened or closed (a redraw happened),
; 0 otherwise; desktop_test_clicks counts every press edge
; ------------------------------------------------------------------

desktop_click_pump:
if defined K64_RUNTIME
        desktop_enter
        inc     qword [desktop_test_clicks]
        cmp     ecx, DESKTOP_START_W
        jae     .other
        cmp     edx, DESKTOP_START_H
        jae     .other
        inc     qword [desktop_test_start_clicks]
        cmp     qword [desktop_menu_flag], 0
        jne     .toggle
        inc     qword [desktop_test_menu_opens]
        jmp     .toggle
.other:
        cmp     qword [desktop_menu_flag], 0
        je      .unused
        cmp     ecx, DESKTOP_MENU_W
        jae     .toggle
        sub     edx, DESKTOP_MENU_Y
        shr     edx, 5
        cmp     edx, DESKTOP_APP_COUNT
        jae     .toggle
        inc     edx
        mov     [desktop_app_launch_requested], rdx
        inc     qword [desktop_test_entry_clicks]
.toggle:
        xor     qword [desktop_menu_flag], 1
        call    app_window_suspend_input
        mov     byte [app_window_dirty], 1
        mov     eax, 1
        jmp     .out
.unused:
        xor     eax, eax
.out:
        desktop_leave
        ret
else
        desktop_enter
        cld
        xor     eax, eax                ; no menu redraw by default
        mov     r12, [input_mouse_b1]
        and     r12, 1
        mov     r13, [desktop_prev_buttons]
        mov     [desktop_prev_buttons], r12
        cmp     r12, r13
        je      .out                    ; no edge
        test    r12, r12
        jz      .out                    ; a release never clicks
        inc     qword [desktop_test_clicks]
        ; the hit test uses the same hotspot clamp as the cursor
        mov     r14, [input_mouse_x]
        cmp     r14, DESKTOP_CURSOR_XMAX
        jbe     .x_ok
        mov     r14, DESKTOP_CURSOR_XMAX
.x_ok:
        mov     r15, [input_mouse_y]
        cmp     r15, DESKTOP_CURSOR_YMAX
        jbe     .y_ok
        mov     r15, DESKTOP_CURSOR_YMAX
.y_ok:
        ; the start button toggles the menu
        cmp     r14, DESKTOP_START_W
        jae     .not_start
        cmp     r15, DESKTOP_START_H
        jae     .not_start
        inc     qword [desktop_test_start_clicks]
        mov     qword [desktop_test_stage], DESKTOP_STAGE_START_CLICK
        cmp     qword [desktop_menu_flag], 0
        jne     .toggle_close
        call    desktop_menu_open
        mov     eax, 1
        jmp     .out
.toggle_close:
        call    desktop_menu_close
        mov     eax, 1
        jmp     .out
.not_start:
        cmp     qword [desktop_menu_flag], 0
        je      .out                    ; closed menu: nothing to do
        ; inside the menu rectangle = the single CHIP-8 entry
        cmp     r14, DESKTOP_MENU_W
        jae     .blank
        cmp     r15, DESKTOP_MENU_Y
        jb      .blank
        cmp     r15, DESKTOP_MENU_Y + DESKTOP_MENU_H
        jae     .blank
        mov     qword [desktop_app_launch_requested], 1
        inc     qword [desktop_test_entry_clicks]
        mov     qword [desktop_test_stage], DESKTOP_STAGE_ENTRY_CLICK
.blank:
        call    desktop_menu_close
        mov     eax, 1
.out:
        desktop_leave
        ret
end if

; ------------------------------------------------------------------
; open the menu: save the background, draw the frame, panel and label
; in
; nothing; the menu must be closed (desktop_menu_flag = 0)
; out
; eax = DESKTOP_OK or the video status of the save; on success
; desktop_menu_flag = 1 and desktop_test_menu_opens advanced
; ------------------------------------------------------------------

desktop_menu_open:
        desktop_enter
        cld
if ~ defined K64_RUNTIME
        ; save the background under the menu rectangle, data page 2
        lea     rcx, [desktop_menu_save]
        mov     edx, DESKTOP_MENU_X
        mov     r8d, DESKTOP_MENU_Y
        mov     r9d, DESKTOP_MENU_W
        mov     dword [rsp+32], DESKTOP_MENU_ACTIVE_H
        call    video_readback_rect
        test    eax, eax
        jnz     .out
end if
        ; the 1 pixel accent frame
        mov     ecx, DESKTOP_MENU_X
        mov     edx, DESKTOP_MENU_Y
        mov     r8d, DESKTOP_MENU_W
        mov     r9d, DESKTOP_MENU_ACTIVE_H
        mov     dword [rsp+32], DESKTOP_ACCENT_COLOR
        call    video_fill_rect
        ; the inner panel
        mov     ecx, DESKTOP_MENU_INNER_X
        mov     edx, DESKTOP_MENU_INNER_Y
        mov     r8d, DESKTOP_MENU_INNER_W
        mov     r9d, DESKTOP_MENU_ACTIVE_H - 2
        mov     dword [rsp+32], DESKTOP_MENU_COLOR
        call    video_fill_rect
if defined K64_RUNTIME
        ; Draw the same catalog that the launcher uses for hit testing.
        lea     r12, [app_catalog + 8]
        mov     r13d, DESKTOP_MENU_TEXT_Y
        mov     ebx, DESKTOP_APP_COUNT
.label:
        mov     ecx, DESKTOP_MENU_TEXT_X
        mov     edx, r13d
        mov     r8d, DESKTOP_TITLE_COLOR
        mov     r9, [r12]
        call    video_puts
        add     r12, 16
        add     r13d, 32
        dec     ebx
        jnz     .label
else
        mov     ecx, DESKTOP_MENU_TEXT_X
        mov     edx, DESKTOP_MENU_TEXT_Y
        mov     r8d, DESKTOP_TITLE_COLOR
        lea     r9, [desktop_menu_text]
        call    video_puts
end if
if ~ defined K64_RUNTIME
        mov     qword [desktop_menu_flag], 1
        inc     qword [desktop_test_menu_opens]
end if
        xor     eax, eax
.out:
        desktop_leave
        ret

; ------------------------------------------------------------------
; close the menu: restore the saved background pixel for pixel
; in
; nothing; the menu must be open (desktop_menu_flag = 1)
; out
; eax = the video status of the restore; desktop_menu_flag = 0
; ------------------------------------------------------------------

desktop_menu_close:
        desktop_enter
        cld
        lea     rcx, [desktop_menu_save]
        mov     edx, DESKTOP_MENU_X
        mov     r8d, DESKTOP_MENU_Y
        mov     r9d, DESKTOP_MENU_W
        mov     dword [rsp+32], DESKTOP_MENU_ACTIVE_H
        call    video_blit_rect
        mov     qword [desktop_menu_flag], 0
        desktop_leave
        ret

; ------------------------------------------------------------------
; the frozen single entry label, a read only constant next to the
; code (the same split as the title text of echo.inc)
; ------------------------------------------------------------------

desktop_menu_text:
        db 'CHIP-8', 0
desktop_menu_text_end:

if defined K64_RUNTIME
desktop_files_text:
        db      'Files', 0
end if

if desktop_menu_text_end - desktop_menu_text <> DESKTOP_MENU_TEXT_LEN + 1
        err 'the menu entry label must stay CHIP-8'
end if
