;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2026. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Renderer: expand the 64x32 cell grid into the 640x320 BGRX frame
;; buffer, every cell becomes a solid 10x10 pixel block. Full repaint,
;; cell-by-cell fill; the 32-bit original's dword-write-with-3-byte-
;; stride quirk is gone. Ported from gui.inc of the 32-bit emulator.
;;

; repaint the whole frame buffer from gfx
; destroys: rax, rcx, rdx, flags (callee-saved preserved)
proc render_frame
        push    rbx
        push    rsi
        push    rdi
        push    r12
        push    r13
        lea     rsi, [chip8_gfx]
        xor     r12d, r12d            ; cell index 0..2047
.render_cell:
        movzx   eax, byte [rsi + r12]
        test    eax, eax
        mov     eax, COLOR_BACK
        mov     ebx, COLOR_CELL
        cmovne  eax, ebx
        ; destination = fb + row*10*FB_STRIDE + col*10*4
        mov     r13d, r12d
        shr     r13d, 6               ; cell row
        mov     ecx, r12d
        and     ecx, GFX_COLS - 1     ; cell column
        imul    edx, r13d, GFX_PIX_SIZE * FB_STRIDE
        imul    ecx, ecx, GFX_PIX_SIZE * 4
        mov     rdi, CHIP8_FB_VA
        add     rdi, rdx
        add     rdi, rcx
        mov     r13d, GFX_PIX_SIZE
.render_row:
        mov     ecx, GFX_PIX_SIZE
        rep stosd
        add     rdi, FB_STRIDE - GFX_PIX_SIZE * 4
        dec     r13d
        jnz     .render_row
        inc     r12d
        cmp     r12d, GFX_SIZE
        jb      .render_cell
        pop     r13
        pop     r12
        pop     rdi
        pop     rsi
        pop     rbx
        ret
endp