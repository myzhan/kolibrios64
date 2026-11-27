;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Desktop shell: the mouse cursor.
;;
;; The move protocol is frozen: restore the saved background at the
;; OLD position (video_blit_rect of the save buffer), then save the
;; background under the NEW position (video_readback_rect, the only
;; sanctioned framebuffer read path of the kernel), composite the
;; sprite over the saved pixels (transparent bytes keep the background)
;; and draw the result (video_blit_rect of the draw buffer). All of it
;; runs in the main loop with IF = 0; the IRQ handlers only accumulate
;; coordinates, they never draw.
;;
;; The sprite is a frozen 12x18 arrow, white fill with a black outline,
;; one byte per pixel: 0 transparent, 1 black, 2 white. The pattern
;; below is pinned byte for byte plus sha256 by the stage2-desktop gate.
;;

; ------------------------------------------------------------------
; save + composite + draw the sprite at one position
; in
; rcx = x, rdx = y (already clamped so the whole sprite is on screen)
; out
; eax = DESKTOP_OK or the video status of the save/draw
; ------------------------------------------------------------------

desktop_cursor_place:
        desktop_enter
        cld
        mov     r12, rcx
        mov     r13, rdx
        ; save the background under the sprite, packed 12 dword rows
        lea     rcx, [desktop_cursor_save]
        mov     rdx, r12
        mov     r8, r13
        mov     r9d, DESKTOP_SPRITE_W
        mov     dword [rsp+32], DESKTOP_SPRITE_H
        call    video_readback_rect
        test    eax, eax
        jnz     .out
        call    desktop_cursor_composite
        ; draw the composited sprite over the saved background
        lea     rcx, [desktop_cursor_draw]
        mov     rdx, r12
        mov     r8, r13
        mov     r9d, DESKTOP_SPRITE_W
        mov     dword [rsp+32], DESKTOP_SPRITE_H
        call    video_blit_rect
.out:
        desktop_leave
        ret

; ------------------------------------------------------------------
; composite the sprite over the save buffer into the draw buffer:
; a transparent code keeps the saved pixel, black/white replace it
; in
; nothing; reads desktop_cursor_sprite and desktop_cursor_save
; out
; eax = DESKTOP_OK; desktop_cursor_draw holds the packed BGRX result
; ------------------------------------------------------------------

desktop_cursor_composite:
        desktop_enter
        cld
        lea     rsi, [desktop_cursor_sprite]
        lea     rdi, [desktop_cursor_save]
        lea     rdx, [desktop_cursor_draw]
        mov     ecx, DESKTOP_SPRITE_BYTES
.loop:
        movzx   eax, byte [rsi]
        test    eax, eax
        jz      .keep
        cmp     eax, DESKTOP_SPRITE_BLACK
        je      .black
        mov     dword [rdx], DESKTOP_CURSOR_WHITE
        jmp     .next
.black:
        mov     dword [rdx], DESKTOP_CURSOR_BLACK
        jmp     .next
.keep:
        mov     eax, [rdi]
        mov     [rdx], eax
.next:
        inc     rsi
        add     rdi, 4
        add     rdx, 4
        dec     ecx
        jnz     .loop
        xor     eax, eax
        desktop_leave
        ret

; ------------------------------------------------------------------
; synchronise the cursor with the accumulated input coordinates
; in
; nothing; reads input_mouse_x / input_mouse_y (already clamped to
; 1024x768 by the mouse handler)
; out
; eax = DESKTOP_OK
; the hotspot clamp keeps the sprite bottom right corner on screen;
; the public evidence counters mirror the clamped position every pass
; ------------------------------------------------------------------

desktop_cursor_sync:
        desktop_enter
        cld
        mov     r12, [input_mouse_x]
        mov     r13, [input_mouse_y]
        cmp     r12, DESKTOP_CURSOR_XMAX
        jbe     .x_ok
        mov     r12, DESKTOP_CURSOR_XMAX
.x_ok:
        cmp     r13, DESKTOP_CURSOR_YMAX
        jbe     .y_ok
        mov     r13, DESKTOP_CURSOR_YMAX
.y_ok:
        mov     [desktop_test_cursor_x], r12
        mov     [desktop_test_cursor_y], r13
        cmp     r12, [desktop_cursor_x]
        jne     .move
        cmp     r13, [desktop_cursor_y]
        je      .out
.move:
if defined K64_RUNTIME
        mov     byte [video_gpu_dirty], 1
end if
        ; restore the background under the OLD position first
        lea     rcx, [desktop_cursor_save]
        mov     rdx, [desktop_cursor_x]
        mov     r8, [desktop_cursor_y]
        mov     r9d, DESKTOP_SPRITE_W
        mov     dword [rsp+32], DESKTOP_SPRITE_H
        call    video_blit_rect
        ; then save + composite + draw at the new position
        mov     rcx, r12
        mov     rdx, r13
        call    desktop_cursor_place
        mov     [desktop_cursor_x], r12
        mov     [desktop_cursor_y], r13
.out:
        xor     eax, eax
        desktop_leave
        ret

; ------------------------------------------------------------------
; The arrowhead and stem share the same 45-degree axis from the hotspot.
; One byte per pixel, row major:
;   0 = transparent (keep the saved background pixel)
;   1 = black outline (0x000000)
;   2 = white fill (0xFFFFFF)
; sha256 of the 216 bytes:
;   f52a399c4986723b620472433c41bf483e4b44aa15c09080d28e9e172b7caabc
; ------------------------------------------------------------------

desktop_cursor_sprite:
        db 1,1,0,0,0,0,0,0,0,0,0,0      ; BB..........
        db 1,2,1,1,1,1,0,0,0,0,0,0      ; BWBBBB......
        db 0,1,2,2,2,2,1,1,1,1,1,0      ; .BWWWWBBBBB.
        db 0,1,2,2,2,2,2,2,2,1,0,0      ; .BWWWWWWWB..
        db 0,1,2,2,2,2,2,2,1,0,0,0      ; .BWWWWWWB...
        db 0,1,2,2,2,2,2,1,0,0,0,0      ; .BWWWWWB....
        db 0,0,1,2,2,2,2,2,1,0,0,0      ; ..BWWWWWB...
        db 0,0,1,2,2,1,2,2,2,1,0,0      ; ..BWWBWWWB..
        db 0,0,1,2,1,0,1,2,2,2,1,0      ; ..BWB.BWWWB.
        db 0,0,1,1,0,0,0,1,2,2,2,1      ; ..BB...BWWWB
        db 0,0,1,0,0,0,0,0,1,2,2,1      ; ..B.....BWWB
        db 0,0,0,0,0,0,0,0,0,1,1,0      ; .........BB.
        db 0,0,0,0,0,0,0,0,0,0,0,0      ; ............
        db 0,0,0,0,0,0,0,0,0,0,0,0      ; ............
        db 0,0,0,0,0,0,0,0,0,0,0,0      ; ............
        db 0,0,0,0,0,0,0,0,0,0,0,0      ; ............
        db 0,0,0,0,0,0,0,0,0,0,0,0      ; ............
        db 0,0,0,0,0,0,0,0,0,0,0,0      ; ............
desktop_cursor_sprite_end:

if desktop_cursor_sprite_end - desktop_cursor_sprite <> DESKTOP_SPRITE_BYTES
        err 'the cursor sprite must stay 216 bytes'
end if
