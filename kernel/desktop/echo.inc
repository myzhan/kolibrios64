;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Desktop shell: the keyboard echo.
;;
;; The pump advances the keyboard ring head and its desktop cursor.
;; Every printable ASCII entry (0x20..0x7E) is drawn into the text area and
;; counted, Enter starts a new line, Backspace erases the last glyph
;; cell by restoring the wallpaper source row by row (DESKTOP-v2: the
;; background is the embedded image, not a flat color). A full line
;; wraps to the next row;
;; a full screen STOPS and counts drops (first version policy, frozen).
;; Enter and Backspace never count into desktop_test_keys. All drawing
;; runs in the main loop with IF = 0, never in interrupt context.
;;

; ------------------------------------------------------------------
; consume every new key buffer entry
; in
; nothing; reads input_kbd_used / input_kbd_buf, advances the head and
; desktop_kbd_consumed. The diagnostic desktop snapshots the write
; sequence first, skipping the input self test events.
; out
; eax = DESKTOP_OK
; ------------------------------------------------------------------

desktop_echo_pump:
        desktop_enter
        cld
if defined K64_RUNTIME
        ; Drain queued keys after the menu's saved background is restored.
        cmp     qword [desktop_menu_flag], 0
        jne     .done
        mov     rax, [input_kbd_head]
        mov     [desktop_kbd_consumed], rax
end if
.loop:
        mov     r12, [desktop_kbd_consumed]
        cmp     r12, [input_kbd_used]
        jae     .done
        mov     rax, r12
        input_kbd_slot
        lea     rdx, [input_kbd_buf]
        mov     r13, [rdx + rax]        ; the ascii qword
        inc     qword [desktop_kbd_consumed]
        inc     qword [input_kbd_head]
        mov     rcx, r13
        call    desktop_echo_key
        jmp     .loop
.done:
        xor     eax, eax
        desktop_leave
        ret

; ------------------------------------------------------------------
; echo one ascii code
; in
; rcx = ascii (0 = a break/modifier entry, ignored)
; out
; eax = DESKTOP_OK
; ------------------------------------------------------------------

desktop_echo_key:
        desktop_enter
        cld
        mov     r12, rcx
        cmp     r12, DESKTOP_KEY_ENTER
        je      .enter
        cmp     r12, DESKTOP_KEY_BACKSPACE
        je      .backspace
        cmp     r12, DESKTOP_ASCII_FIRST
        jb      .out
        cmp     r12, DESKTOP_ASCII_LAST
        ja      .out
        ; printable: a full screen stops and counts, never scrolls
        cmp     qword [desktop_echo_row], DESKTOP_ECHO_ROWS
        jae     .drop
if defined K64_RUNTIME
        imul    rax, [desktop_echo_row], DESKTOP_ECHO_COLS + 1
        add     rax, [desktop_echo_col]
        lea     rdx, [desktop_echo_text]
        mov     [rdx + rax], r12b
        mov     byte [app_window_dirty], 1
else
        ; draw the glyph through video_puts, one char scratch cell
        mov     [desktop_echo_char], r12b
        mov     rax, [desktop_echo_col]
        shl     rax, 3                  ; 8 pixels per column
        add     rax, DESKTOP_ECHO_X
        mov     rcx, rax
        mov     rax, [desktop_echo_row]
        shl     rax, 4                  ; 16 pixels per row
        add     rax, DESKTOP_ECHO_Y
        mov     rdx, rax
        mov     r8d, DESKTOP_ECHO_COLOR
        lea     r9, [desktop_echo_char]
        call    video_puts
end if
        inc     qword [desktop_test_keys]
        inc     qword [desktop_echo_col]
        cmp     qword [desktop_echo_col], DESKTOP_ECHO_COLS
        jb      .out
        ; a full line wraps to the next row
        mov     qword [desktop_echo_col], 0
        inc     qword [desktop_echo_row]
        jmp     .out
.drop:
        inc     qword [desktop_echo_drops]
        jmp     .out
.enter:
        ; invariant: row >= DESKTOP_ECHO_ROWS implies col == 0. row is
        ; only ever incremented by the line wrap and by Enter, and both
        ; paths set col = 0; a full screen drops printables without
        ; advancing col, and Backspace is guarded by col > 0. Any future
        ; scrolling support must preserve this invariant.
        mov     qword [desktop_echo_col], 0
        inc     qword [desktop_echo_row]
        jmp     .out
.backspace:
        cmp     qword [desktop_echo_col], 0
        je      .out                    ; line start: nothing to erase
        dec     qword [desktop_echo_col]
if defined K64_RUNTIME
        imul    rax, [desktop_echo_row], DESKTOP_ECHO_COLS + 1
        add     rax, [desktop_echo_col]
        lea     rdx, [desktop_echo_text]
        mov     byte [rdx + rax], 0
        mov     byte [app_window_dirty], 1
else
        ; DESKTOP-v2: restore the glyph cell from the wallpaper source,
        ; one packed 8 pixel row per blit, 16 rows top to bottom
        mov     rax, [desktop_echo_col]
        shl     rax, 3
        add     rax, DESKTOP_ECHO_X
        mov     r14, rax                ; the cell x
        mov     rax, [desktop_echo_row]
        shl     rax, 4
        add     rax, DESKTOP_ECHO_Y
        mov     r15, rax                ; the running row y
        mov     r13d, DESKTOP_FONT_H
.erase_row:
        ; source = desktop_wallpaper + (y * 1024 + x) * 4
        mov     rax, r15
        shl     rax, 10                 ; the wallpaper row stride 1024
        add     rax, r14
        shl     rax, 2                  ; one BGRX dword per pixel
        lea     rcx, [desktop_wallpaper]
        add     rcx, rax
        mov     rdx, r14
        mov     r8, r15
        mov     r9d, DESKTOP_FONT_W
        mov     dword [rsp+32], 1
        call    video_blit_rect
        inc     r15
        dec     r13d
        jnz     .erase_row
end if
.out:
        xor     eax, eax
        desktop_leave
        ret

; ------------------------------------------------------------------
; the frozen title text, a read only constant next to the code (the
; same split the video module uses for its self test text)
; ------------------------------------------------------------------

desktop_title:
        db 'K64 Desktop v0.1', 0
desktop_title_end:

if defined K64_RUNTIME
; Repaint the retained echo after desktop_repaint_base. Each row is an
; independent NUL-terminated string so Enter alone never draws pixels.
desktop_repaint_echo:
        desktop_enter
        lea     r12, [desktop_echo_text]
        mov     r13d, DESKTOP_ECHO_Y
.row:
        cmp     byte [r12], 0
        je      .next
        mov     ecx, DESKTOP_ECHO_X
        mov     edx, r13d
        mov     r8d, DESKTOP_ECHO_COLOR
        mov     r9, r12
        call    video_puts
.next:
        add     r12, DESKTOP_ECHO_COLS + 1
        add     r13d, DESKTOP_FONT_H
        cmp     r13d, DESKTOP_SCREEN_H
        jb      .row
        desktop_leave
        ret
end if

if desktop_title_end - desktop_title <> DESKTOP_TITLE_LEN + 1
        err 'the title text must stay K64 Desktop v0.1'
end if
