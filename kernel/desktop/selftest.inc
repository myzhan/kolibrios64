;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; The desktop self test, six legs, every wait bounded.
;;
;; The module owns no private STI: every interrupt window of the
;; handshake goes through the exported irq_sti_wait of the IRQ layer
;; (inside desktop_wait_count), which always returns with IF = 0. The
;; host handshake: the guest publishes desktop_test_stage, the host
;; polls it through the gdbstub and injects through the monitor
;; (sendkey h / sendkey i / mouse_move 40 24 / mouse_button 1 then 0);
;; a timeout is a leg failure and panics, never a silent pass.
;;
;; Legs (failure bits of desktop_test_selftest_mask):
;;   0  background: the full screen wallpaper blit (DESKTOP-v2, the
;;      embedded 1024x768 BGRX asset, sha256 frozen by the gate)
;;   1  taskbar: the top bar rectangle, the start button accents and
;;      "K64 Desktop v0.1" at (8,8)
;;   2  cursor: save + composite + draw at the frozen initial position
;;      (517,387), derived from the input self test exit coordinates
;;   3  keyboard echo: the host injects sendkey h then sendkey i, the
;;      pump echoes "hi" into the text area, keys counts to 2
;;   4  mouse: the host injects mouse_move 40 24 then mouse_button 1/0,
;;      the cursor moves to (557,411) with the old position restored
;;   5  final: everything masked, IF = 0, the counters frozen at the
;;      deterministic values derived from the WHOLE injection chain
;;
;; On exit: both RTEs masked, LVT timer masked, IF = 0,
;; input_test_kbd_count == 6, input_test_mouse_count == 6,
;; irq_test_ticks == 11, desktop_test_keys == 2,
;; desktop_test_cursor_x/y == 557/411, desktop_test_stage == 5, mask 0.
;; The resident desktop_run unmask the RTEs again after FOUNDATION-OK.
;;

desktop_selftest:
        desktop_enter
        cld
        cli
        xor     r12d, r12d              ; failure mask
        mov     r13, DESKTOP_LAPIC_VA

        ; the echo starts AFTER the input self test entries: 'a' and
        ; its break already sit in the buffer, the desktop must not
        ; re-echo them (the frozen keys count derives from the desktop
        ; injection only). Skip those entries by publishing the current
        ; write sequence as both the desktop cursor and the queue head.
        mov     rax, [input_kbd_used]
        mov     [desktop_kbd_consumed], rax
        mov     [input_kbd_head], rax

        ; ---------------- leg 0: the wallpaper background ----------------
        lea     rcx, [desktop_wallpaper]
        xor     edx, edx
        xor     r8d, r8d
        mov     r9d, DESKTOP_WALLPAPER_W
        mov     dword [rsp+32], DESKTOP_WALLPAPER_H
        call    video_blit_rect
        test    eax, eax
        jnz     desktop_st_fail_bg

        ; ---------------- leg 1: the taskbar and text ----------------
        mov     ecx, DESKTOP_BAR_X
        mov     edx, DESKTOP_BAR_Y
        mov     r8d, DESKTOP_BAR_W
        mov     r9d, DESKTOP_BAR_H
        mov     dword [rsp+32], DESKTOP_BAR_COLOR
        call    video_fill_rect
        test    eax, eax
        jnz     desktop_st_fail_title
        ; the start button underline, the accent gray of the wallpaper
        xor     ecx, ecx
        mov     edx, DESKTOP_BAR_H - 1
        mov     r8d, DESKTOP_START_W
        mov     r9d, 1
        mov     dword [rsp+32], DESKTOP_ACCENT_COLOR
        call    video_fill_rect
        test    eax, eax
        jnz     desktop_st_fail_title
        ; the separator right of the start button, below the text rows
        mov     ecx, DESKTOP_START_W
        mov     edx, DESKTOP_TITLE_Y + DESKTOP_FONT_H
        mov     r8d, 1
        mov     r9d, DESKTOP_BAR_H - DESKTOP_TITLE_Y - DESKTOP_FONT_H
        mov     dword [rsp+32], DESKTOP_ACCENT_COLOR
        call    video_fill_rect
        test    eax, eax
        jnz     desktop_st_fail_title
        mov     ecx, DESKTOP_TITLE_X
        mov     edx, DESKTOP_TITLE_Y
        mov     r8d, DESKTOP_TITLE_COLOR
        lea     r9, [desktop_title]
        call    video_puts
        test    eax, eax
        jnz     desktop_st_fail_title

        ; ---------------- leg 2: the initial cursor ----------------
        mov     qword [desktop_cursor_x], DESKTOP_CURSOR_X0
        mov     qword [desktop_cursor_y], DESKTOP_CURSOR_Y0
        mov     rcx, DESKTOP_CURSOR_X0
        mov     rdx, DESKTOP_CURSOR_Y0
        call    desktop_cursor_place
        test    eax, eax
        jnz     desktop_st_fail_cursor

        ; ---------------- leg 3: the keyboard echo ----------------
        ; unmask pin 1; a pending IRR edge from the input self test may
        ; deliver a phantom, the handler ignores it (empty OBF)
        mov     ecx, DESKTOP_IOAPIC_IDX_KBD_LO
        mov     edx, DESKTOP_RTE_KBD_LIVE
        call    input_ioapic_write
        mov     qword [desktop_test_stage], DESKTOP_STAGE_ECHO_H
        mov     rcx, desktop_test_keys
        mov     rdx, 1
        call    desktop_wait_count
        test    eax, eax
        jnz     desktop_st_fail_echo
        mov     qword [desktop_test_stage], DESKTOP_STAGE_ECHO_I
        mov     rcx, desktop_test_keys
        mov     rdx, DESKTOP_ST_KEYS_FINAL
        call    desktop_wait_count
        test    eax, eax
        jnz     desktop_st_fail_echo
        cmp     qword [desktop_echo_col], DESKTOP_ST_KEYS_FINAL
        jne     desktop_st_fail_echo
        cmp     qword [desktop_echo_row], 0
        jne     desktop_st_fail_echo
        cmp     qword [desktop_echo_drops], 0
        jne     desktop_st_fail_echo

        ; ---------------- leg 4: the mouse move and buttons ----------------
        mov     ecx, DESKTOP_IOAPIC_IDX_MOUSE_LO
        mov     edx, DESKTOP_RTE_MOUSE_LIVE
        call    input_ioapic_write
        mov     qword [desktop_test_stage], DESKTOP_STAGE_MOUSE_MOVE
        mov     rcx, desktop_test_cursor_x
        mov     rdx, DESKTOP_ST_CURSOR_X
        call    desktop_wait_count
        test    eax, eax
        jnz     desktop_st_fail_mouse
        cmp     qword [desktop_test_cursor_y], DESKTOP_ST_CURSOR_Y
        jne     desktop_st_fail_mouse
        cmp     qword [desktop_cursor_x], DESKTOP_ST_CURSOR_X
        jne     desktop_st_fail_mouse
        cmp     qword [desktop_cursor_y], DESKTOP_ST_CURSOR_Y
        jne     desktop_st_fail_mouse
        mov     qword [desktop_test_stage], DESKTOP_STAGE_MOUSE_BUTTON
        mov     rcx, input_test_mouse_count
        mov     rdx, DESKTOP_ST_MOUSE_FINAL
        call    desktop_wait_count
        test    eax, eax
        jnz     desktop_st_fail_mouse

        ; ---------------- leg 5: the final freeze ----------------
        cli
        mov     dword [r13 + DESKTOP_LAPIC_LVT_TIMER], DESKTOP_LVT_MASK
        mov     ecx, DESKTOP_IOAPIC_IDX_KBD_LO
        mov     edx, DESKTOP_RTE_KBD_MASKED
        call    input_ioapic_write
        mov     ecx, DESKTOP_IOAPIC_IDX_MOUSE_LO
        mov     edx, DESKTOP_RTE_MOUSE_MASKED
        call    input_ioapic_write
        cmp     qword [input_test_kbd_count], DESKTOP_ST_KBD_FINAL
        jne     desktop_st_fail_final
        cmp     qword [input_test_mouse_count], DESKTOP_ST_MOUSE_FINAL
        jne     desktop_st_fail_final
        cmp     qword [irq_test_ticks], DESKTOP_IRQ_TICKS_FINAL
        jne     desktop_st_fail_final
        cmp     qword [input_kbd_used], DESKTOP_ST_KBD_USED_FINAL
        jne     desktop_st_fail_final
        cmp     qword [input_kbd_drops], 0
        jne     desktop_st_fail_final
        cmp     qword [desktop_test_keys], DESKTOP_ST_KEYS_FINAL
        jne     desktop_st_fail_final
        cmp     qword [desktop_test_cursor_x], DESKTOP_ST_CURSOR_X
        jne     desktop_st_fail_final
        cmp     qword [desktop_test_cursor_y], DESKTOP_ST_CURSOR_Y
        jne     desktop_st_fail_final
        cmp     qword [desktop_echo_col], DESKTOP_ST_KEYS_FINAL
        jne     desktop_st_fail_final
        cmp     qword [desktop_echo_row], 0
        jne     desktop_st_fail_final
        cmp     qword [desktop_echo_drops], 0
        jne     desktop_st_fail_final
        ; DESKTOP-v2: the leg 4 press/release at (557,411) is exactly
        ; one click outside the menu, nothing else may have happened
        cmp     qword [desktop_test_clicks], 1
        jne     desktop_st_fail_final
        cmp     qword [desktop_test_menu_opens], 0
        jne     desktop_st_fail_final
        cmp     qword [desktop_menu_flag], 0
        jne     desktop_st_fail_final
        cmp     qword [desktop_app_launch_requested], 0
        jne     desktop_st_fail_final
        cmp     qword [desktop_prev_buttons], 0
        jne     desktop_st_fail_final
        mov     qword [desktop_test_stage], DESKTOP_STAGE_DONE
        mov     [desktop_test_selftest_mask], r12
        xor     eax, eax
        xor     edx, edx
        desktop_leave
        ret

desktop_st_fail_bg:
        or      r12, 1 shl DESKTOP_ST_BIT_BG
        jmp     desktop_st_failed
desktop_st_fail_title:
        or      r12, 1 shl DESKTOP_ST_BIT_TITLE
        jmp     desktop_st_failed
desktop_st_fail_cursor:
        or      r12, 1 shl DESKTOP_ST_BIT_CURSOR
        jmp     desktop_st_failed
desktop_st_fail_echo:
        or      r12, 1 shl DESKTOP_ST_BIT_ECHO
        jmp     desktop_st_failed
desktop_st_fail_mouse:
        or      r12, 1 shl DESKTOP_ST_BIT_MOUSE
        jmp     desktop_st_failed
desktop_st_fail_final:
        or      r12, 1 shl DESKTOP_ST_BIT_FINAL
desktop_st_failed:
        ; freeze everything, then report the mask before returning EIO
        cli
        mov     dword [r13 + DESKTOP_LAPIC_LVT_TIMER], DESKTOP_LVT_MASK
        mov     ecx, DESKTOP_IOAPIC_IDX_KBD_LO
        mov     edx, DESKTOP_RTE_KBD_MASKED
        call    input_ioapic_write
        mov     ecx, DESKTOP_IOAPIC_IDX_MOUSE_LO
        mov     edx, DESKTOP_RTE_MOUSE_MASKED
        call    input_ioapic_write
        mov     [desktop_test_selftest_mask], r12
        mov     rcx, msg_desktop_selftest
        mov     rdx, DESKTOP_PUTS_MAX
        call    cpu_com1_puts
        mov     rcx, [desktop_test_selftest_mask]
        mov     rdx, DESKTOP_PUTHEX_DIGITS
        call    cpu_com1_puthex
        mov     rcx, msg_desktop_nl
        mov     rdx, DESKTOP_PUTS_MAX
        call    cpu_com1_puts
        mov     rdx, r12
        mov     eax, DESKTOP_EIO
        desktop_leave
        ret
