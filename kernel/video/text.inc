;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; GUI-G1 video: 8x16 ASCII text rendering.
;;
;; video_puts walks a NUL terminated byte string and draws every glyph
;; from the embedded font subset. The glyph index is (c - 0x20) * 16.
;; Out of range policy, frozen in api.inc: a byte outside [0x20,0x7E]
;; draws nothing but still advances the cursor by 8 columns.
;;
;; Text goes through the same clipping as the primitives: every pixel
;; is tested against width and height before its store, so a string
;; that runs off any screen edge is cut, never wrapped.
;;

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; draw a NUL terminated ASCII string
; in
; ecx = x, edx = y, r8d = 0x00RRGGBB color, r9 = string kernel VA
; out
; eax = VIDEO_OK, VIDEO_EINVAL for a null string, VIDEO_ENOTREADY
video_puts:
        video_enter
        cld
        cmp     dword [video_fb_state + VIDEO_FB_STATE.ready], 0
        je      .notready
        test    r9, r9
        jz      .invalid
        mov     ecx, ecx
        mov     edx, edx
        mov     r12, rcx                ; cursor x
        mov     r13, rdx                ; cursor y
        mov     r15, r9                 ; string pointer
        mov     eax, r8d
        call    video_bgrx
        mov     r14d, eax               ; BGRX color for every store
.next:
        movzx   eax, byte [r15]
        test    al, al
        jz      .ok
        inc     r15
        ; frozen skip policy: out of range bytes only move the cursor
        cmp     eax, VIDEO_FONT_FIRST
        jb      .advance
        cmp     eax, VIDEO_FONT_LAST
        ja      .advance
        ; glyph = video_font + (c - 0x20) * VIDEO_FONT_STRIDE
        sub     eax, VIDEO_FONT_FIRST
        imul    eax, VIDEO_FONT_STRIDE
        lea     rsi, [video_font]
        add     rsi, rax
        xor     ebx, ebx                ; glyph row
.row:
        ; clip the row: y + row must stay below height
        mov     rdx, r13
        add     rdx, rbx
        mov     eax, dword [video_fb_state + VIDEO_FB_STATE.height]
        cmp     rdx, rax
        jae     .row_next
        ; row base = fb_va + (y + row) * stride
        mov     eax, dword [video_fb_state + VIDEO_FB_STATE.stride]
        imul    rax, rdx
        mov     rdi, qword [video_fb_state + VIDEO_FB_STATE.fb_va]
        add     rdi, rax
        movzx   r9d, byte [rsi + rbx]   ; bitmap row, MSB is leftmost
        xor     r10d, r10d              ; glyph column
.col:
        ; clip the column: cursor x + column must stay below width
        mov     rcx, r12
        add     rcx, r10
        mov     eax, dword [video_fb_state + VIDEO_FB_STATE.width]
        cmp     rcx, rax
        jae     .col_next
        test    r9b, 0x80
        jz      .col_next
        mov     dword [rdi + rcx*4], r14d
.col_next:
        shl     r9b, 1
        inc     r10d
        cmp     r10d, VIDEO_FONT_WIDTH
        jb      .col
.row_next:
        inc     rbx
        cmp     rbx, VIDEO_FONT_HEIGHT
        jb      .row
.advance:
        add     r12, VIDEO_FONT_WIDTH
        jmp     .next
.ok:
        xor     eax, eax
        jmp     .done
.invalid:
        mov     eax, VIDEO_EINVAL
        jmp     .done
.notready:
        mov     eax, VIDEO_ENOTREADY
.done:
        video_leave
        ret