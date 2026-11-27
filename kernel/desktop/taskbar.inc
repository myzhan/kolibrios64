;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Desktop shell, task 125 DESKTOP-v2 T4: the taskbar repaint base,
;; the app launch pump and the foundation boot wrapper.
;;
;; desktop_repaint_base is the full v2 base frame (wallpaper, bar,
;; start button accents, title), the exact self test leg 0+1 sequence
;; without the failure legs. The APP-v1 teardown redraw delegates to
;; it, so an app exit restores the WHOLE desktop, running indicator
;; included, before the echo line and the cursor come back.
;;
;; desktop_launch_pump is the SINGLE consumer of app_launch: it waits
;; for desktop_app_launch_requested (set by the CHIP-8 menu entry or
;; by desktop_app_boot), clears the flag, suspends the cursor (blit of
;; the save buffer at the drawn position), draws the taskbar running
;; indicator, calls app_launch and accounts the run. A clean teardown
;; has already repainted the base and re-placed the cursor with a
;; fresh save; a failure (e.g. ENOENT on the FS-v1 images) runs the
;; idempotent cleanup: erase the indicator, blit the save FIRST and
;; place SECOND, so the save buffer can never capture a drawn sprite.
;;
;; Both callers run with IF = 0 (the resident loop after CLI, the
;; foundation before desktop_run), the app_launch environment gate.
;; The pump never STIs and never touches the frozen desktop_pump
;; four step order; the resident loop calls it right after the pump.
;;

; ------------------------------------------------------------------
; repaint the full v2 base frame: wallpaper, taskbar, start button
; underline and separator, title text (the self test leg 0+1 order)
; in
; nothing; IF = 0, the framebuffer must be ready
; out
; eax = DESKTOP_OK or the first video status that failed
; ------------------------------------------------------------------

desktop_repaint_base:
        desktop_enter
        cld
        ; the full screen wallpaper, the embedded BGRX asset
        lea     rcx, [desktop_wallpaper]
        xor     edx, edx
        xor     r8d, r8d
        mov     r9d, DESKTOP_WALLPAPER_W
        mov     dword [rsp+32], DESKTOP_WALLPAPER_H
        call    video_blit_rect
        test    eax, eax
        jnz     .out
        ; the taskbar
        mov     ecx, DESKTOP_BAR_X
        mov     edx, DESKTOP_BAR_Y
        mov     r8d, DESKTOP_BAR_W
        mov     r9d, DESKTOP_BAR_H
        mov     dword [rsp+32], DESKTOP_BAR_COLOR
        call    video_fill_rect
        test    eax, eax
        jnz     .out
        ; the start button underline
        xor     ecx, ecx
        mov     edx, DESKTOP_BAR_H - 1
        mov     r8d, DESKTOP_START_W
        mov     r9d, 1
        mov     dword [rsp+32], DESKTOP_ACCENT_COLOR
        call    video_fill_rect
        test    eax, eax
        jnz     .out
        ; the separator right of the start button
        mov     ecx, DESKTOP_START_W
        mov     edx, DESKTOP_TITLE_Y + DESKTOP_FONT_H
        mov     r8d, 1
        mov     r9d, DESKTOP_BAR_H - DESKTOP_TITLE_Y - DESKTOP_FONT_H
        mov     dword [rsp+32], DESKTOP_ACCENT_COLOR
        call    video_fill_rect
        test    eax, eax
        jnz     .out
        ; the title text
        mov     ecx, DESKTOP_TITLE_X
        mov     edx, DESKTOP_TITLE_Y
        mov     r8d, DESKTOP_TITLE_COLOR
        lea     r9, [desktop_title]
        call    video_puts
.out:
        desktop_leave
        ret

; ------------------------------------------------------------------
; consume the launch request and run the hosted program
; in
; ecx = 1 click mode (resident loop: publish the APP_RUNNING/APP_EXIT
; stages), 0 boot mode (foundation: no stage publication, the frozen
; three-leg desktop_test_stage = 5 final stays intact)
; out
; eax = the app_launch status, 0 when no launch was requested;
; desktop_test_app_runs counts every invocation, desktop_test_app_exits
; every clean teardown (a relaunch after ESC advances both again)
; ------------------------------------------------------------------

desktop_launch_pump:
if defined K64_RUNTIME
        xor     ecx, ecx
        xchg    rcx, [desktop_app_launch_requested]
        test    ecx, ecx
        jnz     app_task_start
        xor     eax, eax
        ret
else
        desktop_enter
        cld
        mov     r13d, ecx               ; the publication mode
        xor     r12d, r12d              ; the returned status
        cmp     qword [desktop_app_launch_requested], 0
        je      .out
        mov     qword [desktop_app_launch_requested], 0
        ; suspend the cursor: restore the background at the drawn
        ; position, the save buffer stays valid for the cleanup
        lea     rcx, [desktop_cursor_save]
        mov     rdx, [desktop_cursor_x]
        mov     r8, [desktop_cursor_y]
        mov     r9d, DESKTOP_SPRITE_W
        mov     dword [rsp+32], DESKTOP_SPRITE_H
        call    video_blit_rect
        ; the taskbar running indicator
        mov     ecx, DESKTOP_RUN_X
        mov     edx, DESKTOP_RUN_Y
        mov     r8d, DESKTOP_RUN_W
        mov     r9d, DESKTOP_RUN_H
        mov     dword [rsp+32], DESKTOP_RUN_COLOR
        call    video_fill_rect
        inc     qword [desktop_test_app_runs]
        test    r13d, r13d
        jz      .launch
        mov     qword [desktop_test_stage], DESKTOP_STAGE_APP_RUNNING
.launch:
        call    app_launch
        mov     r12d, eax
        test    r13d, r13d
        jz      .verdict
        mov     qword [desktop_test_stage], DESKTOP_STAGE_APP_EXIT
.verdict:
        test    r12d, r12d
        jnz     .cleanup
        inc     qword [desktop_test_app_exits]
        ; the teardown redraw already repainted the base (indicator
        ; gone) and re-placed the cursor with a fresh save
        jmp     .out
.cleanup:
        ; the failure path never reached the teardown redraw: erase
        ; the indicator, then restore the cursor IDEMPOTENTLY, blit
        ; the save FIRST and place SECOND so a second cleanup (or a
        ; later pump move) can never restore a captured sprite
        mov     ecx, DESKTOP_RUN_X
        mov     edx, DESKTOP_RUN_Y
        mov     r8d, DESKTOP_RUN_W
        mov     r9d, DESKTOP_RUN_H
        mov     dword [rsp+32], DESKTOP_BAR_COLOR
        call    video_fill_rect
        lea     rcx, [desktop_cursor_save]
        mov     rdx, [desktop_cursor_x]
        mov     r8, [desktop_cursor_y]
        mov     r9d, DESKTOP_SPRITE_W
        mov     dword [rsp+32], DESKTOP_SPRITE_H
        call    video_blit_rect
        mov     rcx, [desktop_cursor_x]
        mov     rdx, [desktop_cursor_y]
        call    desktop_cursor_place
.out:
        mov     eax, r12d
        desktop_leave
        ret
end if

; ------------------------------------------------------------------
; the foundation boot call site: route the boot-time hosted run
; through the SAME launch pump the menu entry uses (boot mode). The
; request flag is set here exactly like the CHIP-8 entry sets it, so
; both paths share one consumer and one accounting.
; in
; nothing; IF = 0, the app module must be initialised
; out
; eax = the app_launch status (the foundation verdict is unchanged)
; ------------------------------------------------------------------

desktop_app_boot:
        desktop_enter
        cld
        mov     qword [desktop_app_launch_requested], 1
        xor     ecx, ecx
        call    desktop_launch_pump
        desktop_leave
        ret

if defined K64_RUNTIME
; Paint the initial desktop without the diagnostic input handshake.
desktop_show:
        desktop_enter
        call    desktop_repaint_base
        test    eax, eax
        jnz     .out
        mov     qword [input_mouse_x], DESKTOP_CURSOR_X0
        mov     qword [input_mouse_y], DESKTOP_CURSOR_Y0
        mov     qword [desktop_cursor_x], DESKTOP_CURSOR_X0
        mov     qword [desktop_cursor_y], DESKTOP_CURSOR_Y0
        mov     ecx, DESKTOP_CURSOR_X0
        mov     edx, DESKTOP_CURSOR_Y0
        call    desktop_cursor_place
.out:
        desktop_leave
        ret
end if

