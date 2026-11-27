;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; GUI-G1 video: 32bpp BGRX drawing primitives.
;;
;; Every entry point clips first and writes second: a coordinate outside
;; 0..width-1 / 0..height-1 never reaches the framebuffer. Row offsets
;; are always computed from stride = pitch * 4, never from width * 4, so
;; a padded scan line stays intact.
;;
;; Colors arrive as 0x00RRGGBB; the GOP BGRX byte order (B in the low
;; byte, X byte zero) is exactly the little-endian store of that value,
;; so video_bgrx only drops the alpha byte before any store.
;;

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; drop the alpha byte of a 0xAARRGGBB color for the BGRX store
; in
; eax = 0xAARRGGBB, the high byte is dropped
; out
; eax = 0x00RRGGBB; stored little-endian the bytes are B,G,R,X=0, the
;       GOP PixelBlueGreenRedReserved8BitPerColor order QEMU renders
; note
; leaf helper, no frame, destroys only EAX. A bswap here would emit
; R,G,B,X and swap red/blue on screen: the task-78 screendump probes
; caught exactly that, the mask is the whole conversion
video_bgrx:
        and     eax, 0x00FFFFFF
        ret

if defined K64_RUNTIME
; Store one logical pixel in two physical scanlines. Clobbers r8.
macro video_store_2x row*, x*, color*
        mov     dword [row + x*8], color
        mov     dword [row + x*8 + 4], color
        mov     r8d, dword [video_fb_state + VIDEO_FB_STATE.stride]
        add     r8, row
        mov     dword [r8 + x*8], color
        mov     dword [r8 + x*8 + 4], color
end macro
end if

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; clip a rectangle against the framebuffer geometry
; in
; ecx = x, edx = y, r8d = w, r9d = h
; out
; eax = 1 when a nonempty rectangle remains, 0 when nothing is visible;
; ecx/edx/r8d/r9d hold the clipped rectangle when eax = 1
; note
; leaf helper, destroys only RAX/R10/R11; coordinates are unsigned, a
; negative argument arrives as a huge value and clips away
video_clip_rect:
        ; the ABI leaves the high halves of 32 bit arguments undefined
        mov     ecx, ecx
        mov     edx, edx
        mov     r8d, r8d
        mov     r9d, r9d
        test    r8d, r8d
        jz      .empty
        test    r9d, r9d
        jz      .empty
        mov     r10d, dword [video_fb_state + VIDEO_FB_STATE.width]
        mov     r11d, dword [video_fb_state + VIDEO_FB_STATE.height]
        cmp     rcx, r10
        jae     .empty
        cmp     rdx, r11
        jae     .empty
        ; right edge: x + w > width shrinks w, 64 bit so x + w cannot wrap
        mov     rax, rcx
        add     rax, r8
        cmp     rax, r10
        jbe     .vertical
        mov     r8, r10
        sub     r8, rcx
.vertical:
        mov     rax, rdx
        add     rax, r9
        cmp     rax, r11
        jbe     .nonempty
        mov     r9, r11
        sub     r9, rdx
.nonempty:
        mov     eax, 1
        ret
.empty:
        xor     eax, eax
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; draw one pixel
; in
; ecx = x, edx = y, r8d = 0x00RRGGBB color
; out
; eax = VIDEO_OK (a clipped pixel is a silent no-op), VIDEO_ENOTREADY
video_putpixel:
        video_enter
        cld
        cmp     dword [video_fb_state + VIDEO_FB_STATE.ready], 0
        je      .notready
        ; clip the point against width and height before any write
        mov     ecx, ecx
        mov     edx, edx
        mov     r10d, dword [video_fb_state + VIDEO_FB_STATE.width]
        mov     r11d, dword [video_fb_state + VIDEO_FB_STATE.height]
        cmp     rcx, r10
        jae     .clipped
        cmp     rdx, r11
        jae     .clipped
if defined K64_RUNTIME
        cmp     byte [video_fb_double], 0
        je      .native
        shl     rdx, 1
        mov     eax, dword [video_fb_state + VIDEO_FB_STATE.stride]
        imul    rax, rdx
        mov     rdi, qword [video_fb_state + VIDEO_FB_STATE.fb_va]
        add     rdi, rax
        mov     eax, r8d
        call    video_bgrx
        video_store_2x rdi, rcx, eax
        jmp     .clipped
.native:
end if
        ; address = fb_va + y * stride + x * 4
        mov     eax, dword [video_fb_state + VIDEO_FB_STATE.stride]
        imul    rax, rdx
        mov     rdi, qword [video_fb_state + VIDEO_FB_STATE.fb_va]
        add     rdi, rax
        lea     rdi, [rdi + rcx*4]
        mov     eax, r8d
        call    video_bgrx
        mov     dword [rdi], eax
.clipped:
        xor     eax, eax
        jmp     .done
.notready:
        mov     eax, VIDEO_ENOTREADY
.done:
        video_leave
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; fill an axis aligned rectangle with one color
; in
; ecx = x, edx = y, r8d = w, r9d = h, [rsp+40] = 0x00RRGGBB color
; out
; eax = VIDEO_OK (a fully clipped rectangle is a silent no-op),
;       VIDEO_ENOTREADY
video_fill_rect:
        mov     eax, dword [rsp+40]     ; fifth argument, MS x64 stack slot
        video_enter
        cld
        mov     r12d, eax
        cmp     dword [video_fb_state + VIDEO_FB_STATE.ready], 0
        je      .notready
        call    video_clip_rect
        test    eax, eax
        jz      .clipped
if defined K64_RUNTIME
        cmp     byte [video_fb_double], 0
        je      .fill_native
        shl     rcx, 1
        shl     rdx, 1
        shl     r8, 1
        shl     r9, 1
.fill_native:
end if
        mov     r13, rcx                ; x
        mov     r14, rdx                ; y
        mov     r15, r8                 ; w
        mov     rbx, r9                 ; h
        ; row base = fb_va + y * stride + x * 4
        mov     eax, dword [video_fb_state + VIDEO_FB_STATE.stride]
        imul    rax, r14
        mov     rdi, qword [video_fb_state + VIDEO_FB_STATE.fb_va]
        add     rdi, rax
        lea     rdi, [rdi + r13*4]
        ; after rep stosd rdi advanced by w * 4, the rest of the scan
        ; line is stride - w * 4 bytes of padding that must be skipped
        mov     r13d, dword [video_fb_state + VIDEO_FB_STATE.stride]
        mov     rax, r15
        shl     rax, 2
        sub     r13, rax
        mov     eax, r12d
        call    video_bgrx
.row:
        mov     rcx, r15
        rep stosd
        add     rdi, r13
        dec     rbx
        jnz     .row
.clipped:
        xor     eax, eax
        jmp     .done
.notready:
        mov     eax, VIDEO_ENOTREADY
.done:
        video_leave
        ret

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; copy a rectangle from a kernel source buffer into the framebuffer
; in
; rcx = source kernel VA, tightly packed w * 4 byte rows,
; edx = dst x, r8d = dst y, r9d = w, [rsp+40] = h
; out
; eax = VIDEO_OK (a fully clipped rectangle is a silent no-op),
;       VIDEO_EINVAL for a null source, VIDEO_ENOTREADY
; note
; the source pixels are already BGRX dwords, no color conversion happens;
; the source rows stay packed at the ORIGINAL w even when the right edge
; clips w down, so every row advance compensates (orig_w - clipped_w) * 4
video_blit_rect:
        mov     eax, dword [rsp+40]     ; fifth argument, MS x64 stack slot
        video_enter
        cld
        mov     r12, rcx                ; source
        mov     ecx, edx                ; x
        mov     edx, r8d                ; y
        mov     r14d, r9d               ; original w, saved before the clip
        mov     r8d, r9d                ; w
        mov     r9, rax                 ; h, already zero extended
        test    r12, r12
        jz      .invalid
        cmp     dword [video_fb_state + VIDEO_FB_STATE.ready], 0
        je      .notready
        call    video_clip_rect
        test    eax, eax
        jz      .clipped
        mov     r15, r8                 ; w
        mov     rbx, r9                 ; h
        mov     rsi, r12
if defined K64_RUNTIME
        cmp     byte [video_fb_double], 0
        jne     .scaled
end if
        ; row base = fb_va + y * stride + x * 4
        mov     eax, dword [video_fb_state + VIDEO_FB_STATE.stride]
        imul    rax, rdx
        mov     rdi, qword [video_fb_state + VIDEO_FB_STATE.fb_va]
        add     rdi, rax
        lea     rdi, [rdi + rcx*4]
        ; destination rows are stride bytes apart, source rows w * 4
        mov     r13d, dword [video_fb_state + VIDEO_FB_STATE.stride]
        mov     rax, r15
        shl     rax, 2
        sub     r13, rax
        ; the source stays packed at the original w: after a right edge
        ; clip each row must skip (orig_w - clipped_w) * 4 source bytes
        sub     r14, r8
        shl     r14, 2
.row:
        mov     rcx, r15
        rep movsd
        add     rsi, r14                ; source row pitch compensation
        add     rdi, r13
        dec     rbx
        jnz     .row
if defined K64_RUNTIME
        jmp     .clipped
.scaled:
        mov     r13d, dword [video_fb_state + VIDEO_FB_STATE.stride]
        shl     rdx, 1
        imul    rdx, r13
        mov     rdi, qword [video_fb_state + VIDEO_FB_STATE.fb_va]
        add     rdi, rdx
        lea     rdi, [rdi + rcx*8]
        sub     r14, r15
        shl     r14, 2                  ; packed source row remainder
        lea     r12, [r13*2]
        mov     rax, r15
        shl     rax, 3
        sub     r12, rax                ; destination row-pair remainder
.scaled_row:
        lea     rdx, [rdi + r13]
        mov     rcx, r15
.scaled_pixel:
        lodsd
        mov     [rdi], eax
        mov     [rdi + 4], eax
        mov     [rdx], eax
        mov     [rdx + 4], eax
        add     rdi, 8
        add     rdx, 8
        dec     rcx
        jnz     .scaled_pixel
        add     rsi, r14
        add     rdi, r12
        dec     rbx
        jnz     .scaled_row
end if
.clipped:
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

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; read a rectangle from the framebuffer into a kernel buffer
; in
; rcx = destination kernel VA, tightly packed w * 4 byte rows,
; edx = src x, r8d = src y, r9d = w, [rsp+40] = h
; out
; eax = VIDEO_OK (a fully clipped rectangle is a silent no-op),
;       VIDEO_EINVAL for a null destination, VIDEO_ENOTREADY
; note
; task 96, the ONLY sanctioned framebuffer read path: the desktop
; cursor saves the background under the sprite before drawing it.
; video_selftest stays draw-only, its evidence is the external QEMU
; screendump, never this API. Exact mirror of video_blit_rect with
; the direction reversed: source rows are stride bytes apart (r13
; compensation), destination rows stay packed at the ORIGINAL w, so
; after a right edge clip every row advance skips (orig_w - clipped_w)
; * 4 destination bytes (r14). No color conversion happens, the copy
; moves the raw BGRX dwords.
video_readback_rect:
        mov     eax, dword [rsp+40]     ; fifth argument, MS x64 stack slot
        video_enter
        cld
        mov     r12, rcx                ; destination
        mov     ecx, edx                ; x
        mov     edx, r8d                ; y
        mov     r14d, r9d               ; original w, saved before the clip
        mov     r8d, r9d                ; w
        mov     r9, rax                 ; h, already zero extended
        test    r12, r12
        jz      .invalid
        cmp     dword [video_fb_state + VIDEO_FB_STATE.ready], 0
        je      .notready
        call    video_clip_rect
        test    eax, eax
        jz      .clipped
        mov     r15, r8                 ; w
        mov     rbx, r9                 ; h
        mov     rdi, r12
if defined K64_RUNTIME
        cmp     byte [video_fb_double], 0
        jne     .scaled
end if
        ; row base = fb_va + y * stride + x * 4
        mov     eax, dword [video_fb_state + VIDEO_FB_STATE.stride]
        imul    rax, rdx
        mov     rsi, qword [video_fb_state + VIDEO_FB_STATE.fb_va]
        add     rsi, rax
        lea     rsi, [rsi + rcx*4]
        ; source rows are stride bytes apart, destination rows w * 4
        mov     r13d, dword [video_fb_state + VIDEO_FB_STATE.stride]
        mov     rax, r15
        shl     rax, 2
        sub     r13, rax
        ; the destination stays packed at the original w: after a right
        ; edge clip each row must skip (orig_w - clipped_w) * 4 dest bytes
        sub     r14, r8
        shl     r14, 2
.row:
        mov     rcx, r15
        rep movsd
        add     rsi, r13                ; source row pitch compensation
        add     rdi, r14
        dec     rbx
        jnz     .row
if defined K64_RUNTIME
        jmp     .clipped
.scaled:
        mov     r13d, dword [video_fb_state + VIDEO_FB_STATE.stride]
        shl     r13, 1
        imul    rdx, r13
        mov     rsi, qword [video_fb_state + VIDEO_FB_STATE.fb_va]
        add     rsi, rdx
        lea     rsi, [rsi + rcx*8]
        sub     r14, r15
        shl     r14, 2                  ; packed destination row remainder
        mov     rax, r15
        shl     rax, 3
        sub     r13, rax                ; source row-pair remainder
.scaled_row:
        mov     rcx, r15
.scaled_pixel:
        mov     eax, [rsi]              ; upper-left pixel of the 2x2 block
        stosd
        add     rsi, 8
        dec     rcx
        jnz     .scaled_pixel
        add     rsi, r13
        add     rdi, r14
        dec     rbx
        jnz     .scaled_row
end if
.clipped:
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
