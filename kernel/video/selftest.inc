;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; GUI-G1 video: the deterministic self test pattern.
;;
;; The pattern spec is frozen in api.inc (see the table at its top) and
;; is the forensic base of the QEMU screendump pixel assertions of the
;; integration task. Draw order, back to front:
;;
;;   1. eight 128 column vertical bars, colors video_st_colors in order
;;   2. a one pixel white border around the whole screen
;;   3. the centered 200x200 gray rectangle at (412,284)
;;   4. the white text "K64 GUI-G1" at (8,8)
;;
;; The reference geometry is exactly 1024x768; any other framebuffer is
;; refused with ENOTSUP so the pattern coordinates never need scaling.
;; No readback happens here: the framebuffer is write only evidence for
;; G1, the verification is the screendump of the integration run. This
;; entry point only guarantees the draw call sequence and its statuses.
;;

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; draw the frozen G1 pattern
; in
; nothing
; out
; eax = VIDEO_OK, VIDEO_ENOTREADY, VIDEO_ENOTSUP for a foreign geometry,
;       VIDEO_EIO when a draw call failed
; rdx = 0 on success, otherwise the failing layer (1 bars, 2 border,
;       3 rectangle, 4 text), also stored in video_selftest_mask
video_selftest:
        video_enter
        cld
        xor     r13d, r13d              ; failing layer, 0 = none
        cmp     dword [video_fb_state + VIDEO_FB_STATE.ready], 0
        je      .notready
        cmp     dword [video_fb_state + VIDEO_FB_STATE.width], VIDEO_ST_WIDTH
        jne     .notsup
        cmp     dword [video_fb_state + VIDEO_FB_STATE.height], VIDEO_ST_HEIGHT
        jne     .notsup

        ; layer 1: eight bars, bar i covers x in [i*128, (i+1)*128)
        mov     r13d, 1
        xor     ebx, ebx
        lea     r12, [video_st_colors]
.bar:
        mov     ecx, ebx
        imul    ecx, VIDEO_ST_BAR_W
        xor     edx, edx
        mov     r8d, VIDEO_ST_BAR_W
        mov     r9d, VIDEO_ST_HEIGHT
        mov     eax, dword [r12 + rbx*4]
        mov     dword [rsp+32], eax     ; color, the fifth argument
        call    video_fill_rect
        test    eax, eax
        jnz     .fail
        inc     ebx
        cmp     ebx, VIDEO_ST_BARS
        jb      .bar

        ; layer 2: the one pixel white border, four edge rectangles
        mov     r13d, 2
        xor     ecx, ecx
        xor     edx, edx
        mov     r8d, VIDEO_ST_WIDTH
        mov     r9d, 1
        mov     dword [rsp+32], VIDEO_ST_BORDER
        call    video_fill_rect         ; top row
        test    eax, eax
        jnz     .fail
        xor     ecx, ecx
        mov     edx, VIDEO_ST_HEIGHT-1
        mov     r8d, VIDEO_ST_WIDTH
        mov     r9d, 1
        mov     dword [rsp+32], VIDEO_ST_BORDER
        call    video_fill_rect         ; bottom row
        test    eax, eax
        jnz     .fail
        xor     ecx, ecx
        xor     edx, edx
        mov     r8d, 1
        mov     r9d, VIDEO_ST_HEIGHT
        mov     dword [rsp+32], VIDEO_ST_BORDER
        call    video_fill_rect         ; left column
        test    eax, eax
        jnz     .fail
        mov     ecx, VIDEO_ST_WIDTH-1
        xor     edx, edx
        mov     r8d, 1
        mov     r9d, VIDEO_ST_HEIGHT
        mov     dword [rsp+32], VIDEO_ST_BORDER
        call    video_fill_rect         ; right column
        test    eax, eax
        jnz     .fail

        ; layer 3: the centered gray rectangle
        mov     r13d, 3
        mov     ecx, VIDEO_ST_RECT_X
        mov     edx, VIDEO_ST_RECT_Y
        mov     r8d, VIDEO_ST_RECT_W
        mov     r9d, VIDEO_ST_RECT_H
        mov     dword [rsp+32], VIDEO_ST_RECT_COLOR
        call    video_fill_rect
        test    eax, eax
        jnz     .fail

        ; layer 4: the identification text
        mov     r13d, 4
        mov     ecx, VIDEO_ST_TEXT_X
        mov     edx, VIDEO_ST_TEXT_Y
        mov     r8d, VIDEO_ST_TEXT_COLOR
        lea     r9, [video_st_text]
        call    video_puts
        test    eax, eax
        jnz     .fail

        xor     r13d, r13d
        mov     qword [video_selftest_mask], r13
        xor     eax, eax
        xor     edx, edx
        jmp     .done
.fail:
        mov     qword [video_selftest_mask], r13
        mov     rdx, r13
        mov     eax, VIDEO_EIO
        jmp     .done
.notready:
        mov     eax, VIDEO_ENOTREADY
        xor     edx, edx
        jmp     .done
.notsup:
        mov     eax, VIDEO_ENOTSUP
        xor     edx, edx
.done:
        video_leave
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; constant pattern data, emitted next to the code on purpose: the
; mutable zeroed block of the module lives in data.inc alone

; the frozen bar color sequence, 0x00RRGGBB, index = bar number
video_st_colors:
        dd      VIDEO_ST_BAR0
        dd      VIDEO_ST_BAR1
        dd      VIDEO_ST_BAR2
        dd      VIDEO_ST_BAR3
        dd      VIDEO_ST_BAR4
        dd      VIDEO_ST_BAR5
        dd      VIDEO_ST_BAR6
        dd      VIDEO_ST_BAR7

video_st_text:
        db      'K64 GUI-G1', 0
video_st_text_end:

if (video_st_text_end - video_st_text) <> VIDEO_ST_TEXT_LEN + 1
        display "the self test text must stay K64 GUI-G1", 13, 10
        err
end if