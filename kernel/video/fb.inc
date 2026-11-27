;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; GUI-G1 video: framebuffer handover and MMIO mapping.
;;
;; video_init consumes the GOP block of BootInfo and maps the linear
;; framebuffer into the FB window through vmm_map_mmio, one 4 KiB page
;; at a time. The MM side guarantees the per page rollback semantics;
;; this module records how many pages were mapped before a failure so
;; the integration stage can diagnose a partial handover.
;;
;; Nothing here calls the console or panics: every refusal is an EAX
;; status. The BootInfo signature and version are validated by MM at
;; boot, this entry point only reads the framebuffer feature.
;;

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; validate the GOP handover and map the framebuffer
; in
; rcx = BootInfo virtual address
; out
; eax = VIDEO_OK, VIDEO_EINVAL for a broken geometry, VIDEO_ENOTSUP for
;       a format or size G1 does not support, VIDEO_EALREADY on a second
;       init, VIDEO_EIO when a vmm_map_mmio call fails. ENOTSUP is
;       reserved for the geometry gates: a mapping fault must never
;       reach foundation as a skip-able status
; note
; mapped_pages holds the number of successfully mapped pages on every
; exit path, including a failure in the middle of the loop
video_init:
        video_enter
        cld
        mov     r12, rcx
        test    r12, r12
        jz      .invalid
        cmp     dword [video_fb_state + VIDEO_FB_STATE.ready], 0
        jne     .already

        ; the framebuffer feature must be present
        mov     rax, qword [r12 + VIDEO_BOOTINFO.flags]
        test    rax, VIDEO_FLAG_FRAMEBUFFER
        jz      .invalid

        ; geometry: width, height and pitch are nonzero, pitch >= width
        mov     r8d, dword [r12 + VIDEO_BOOTINFO.gop_width]
        mov     r9d, dword [r12 + VIDEO_BOOTINFO.gop_height]
        mov     r10d, dword [r12 + VIDEO_BOOTINFO.gop_pixels_per_line]
        mov     r11d, dword [r12 + VIDEO_BOOTINFO.gop_pixel_format]
        test    r8d, r8d
        jz      .invalid
        test    r9d, r9d
        jz      .invalid
        test    r10d, r10d
        jz      .invalid
        cmp     r11d, VIDEO_FMT_BGRX
        jne     .notsup
        cmp     r10d, r8d
        jb      .invalid                ; pitch must cover the width
        cmp     r8d, VIDEO_MAX_WIDTH
        ja      .notsup
        cmp     r9d, VIDEO_MAX_HEIGHT
        ja      .notsup

        ; the physical range: nonzero and page aligned, vmm_map_mmio
        ; refuses an unaligned PA and the GOP hands over whole pages
        mov     r13, qword [r12 + VIDEO_BOOTINFO.gop_framebuffer_addr]
        mov     r14, qword [r12 + VIDEO_BOOTINFO.gop_framebuffer_size]
        test    r13, r13
        jz      .invalid
        test    r14, r14
        jz      .invalid
        test    r13, VIDEO_PAGE_SIZE-1
        jnz     .invalid

        ; size >= pitch * height * 4, the 32bpp budget; width and height
        ; are bounded above, so the product can never overflow 64 bits
        mov     eax, r10d
        imul    rax, r9
        shl     rax, 2
        cmp     r14, rax
        jb      .invalid

        ; the size must fit the 16 MiB FB window BEFORE the round up:
        ; add 4095 wraps for a size near 2^64 (0 pages, ready published
        ; over unmapped VAs), and the bound also keeps pitch * 4 below
        ; the 32 bit stride wrap. The task-85 review pinned this guard
        cmp     r14, VIDEO_FB_MAX_SIZE
        ja      .notsup

        ; pages = ceil(size / 4096), the mapping must fit the FB window
        mov     r15, r14
        add     r15, VIDEO_PAGE_SIZE-1
        shr     r15, VIDEO_PAGE_SHIFT
        cmp     r15, VIDEO_FB_MAX_PAGES
        ja      .notsup

        ; map every page, the count of finished pages is published before
        ; each call so a failure leaves the exact progress behind
        xor     ebx, ebx
.map_loop:
        cmp     rbx, r15
        jae     .mapped
        mov     rcx, VIDEO_FB_VA
        mov     rax, rbx
        shl     rax, VIDEO_PAGE_SHIFT
        add     rcx, rax
        mov     rdx, r13
        add     rdx, rax
        mov     dword [video_fb_state + VIDEO_FB_STATE.mapped_pages], ebx
        call    vmm_map_mmio
        test    eax, eax
        jnz     .map_fail               ; a mapping fault is EIO, never the
                                        ; raw MM status: foundation would
                                        ; read MM_ENOTSUP as a geometry skip
        inc     rbx
        jmp     .map_loop

.mapped:
        mov     dword [video_fb_state + VIDEO_FB_STATE.mapped_pages], ebx

        ; r8..r11 are caller-saved: the vmm_map_mmio calls clobbered them,
        ; so the validated geometry is reloaded from the BootInfo that
        ; r12 still holds before anything is published
        mov     r8d, dword [r12 + VIDEO_BOOTINFO.gop_width]
        mov     r9d, dword [r12 + VIDEO_BOOTINFO.gop_height]
        mov     r10d, dword [r12 + VIDEO_BOOTINFO.gop_pixels_per_line]
        mov     r11d, dword [r12 + VIDEO_BOOTINFO.gop_pixel_format]

if defined K64_RUNTIME
        cmp     r8d, VIDEO_ST_WIDTH * 2
        jne     .geometry_ready
        cmp     r9d, VIDEO_ST_HEIGHT * 2
        jne     .geometry_ready
        mov     byte [video_fb_double], 1
        shr     r8d, 1
        shr     r9d, 1
.geometry_ready:
end if
        ; publish the logical geometry and physical pitch/stride.
        ; Every draw row uses the physical stride.
        mov     dword [video_fb_state + VIDEO_FB_STATE.width], r8d
        mov     dword [video_fb_state + VIDEO_FB_STATE.height], r9d
        mov     dword [video_fb_state + VIDEO_FB_STATE.pitch], r10d
        mov     dword [video_fb_state + VIDEO_FB_STATE.format], r11d
        mov     eax, r10d
        shl     eax, 2
        mov     dword [video_fb_state + VIDEO_FB_STATE.stride], eax
        mov     qword [video_fb_state + VIDEO_FB_STATE.fb_pa], r13
        mov     qword [video_fb_state + VIDEO_FB_STATE.fb_size], r14
        mov     rax, VIDEO_FB_VA
        mov     qword [video_fb_state + VIDEO_FB_STATE.fb_va], rax
        mov     dword [video_fb_state + VIDEO_FB_STATE.ready], 1
        xor     eax, eax
        jmp     .done
.invalid:
        mov     eax, VIDEO_EINVAL
        jmp     .done
.notsup:
        mov     eax, VIDEO_ENOTSUP
        jmp     .done
.map_fail:
        ; mapped_pages keeps the exact progress; the structural fault
        ; takes the fatal foundation path, not the non-fatal GUI skip
        mov     eax, VIDEO_EIO
        jmp     .done
.already:
        mov     eax, VIDEO_EALREADY
.done:
        video_leave
        ret
