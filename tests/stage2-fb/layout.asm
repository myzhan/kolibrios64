;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Layout probe for the GUI-G1 video module, task 77.
;;
;; This file is a test harness, not part of the kernel. It assembles
;; kernel/video/video.asm exactly as the kernel will include it, with
;; kernel/mm/mm.asm in front of it because video_init calls the frozen
;; vmm_map_mmio entry, and wraps both in a manifest of label addresses
;; that tests/stage2-fb/check.py verifies. Nothing here is executed and
;; no machine is started.
;;
;; FB_PROBE_ORG selects the link base; check.py assembles the probe
;; twice, once at 0 and once at the kernel base, to prove the module
;; links at 0xFFFFFFFF80000000 with the same internal layout.
;;
;; The video data block must end the image: video.asm stays last, the
;; MM data block sits in the middle of the probe image and is located
;; through the manifest so the checker can keep it apart from the video
;; state. The manifest is a fixed list of qwords in the order check.py
;; expects; the magic, the version and the entry count are pinned.
;;

format binary

if defined FB_PROBE_ORG
        org FB_PROBE_ORG
        FB_PROBE_BASE = FB_PROBE_ORG
else
        org 0
        FB_PROBE_BASE = 0
end if

use64
MM_HOSTED = 1
VIDEO_HOSTED = 1

; ------------------------------------------------------------------
; manifest, fixed order, read by check.py
; ------------------------------------------------------------------

align 8
fb_probe_manifest:
        dq      'K64FBP1'                  ; magic
        dq      1                           ; manifest version
        dq      FB_PROBE_BASE               ; link base of this image

; data blocks, video first so the checker can find the image end
        dq      video_data_start
        dq      video_data_end
        dq      video_fb_state
        dq      video_selftest_mask
        dq      mm_data_start
        dq      mm_data_end

; public entry points and internal helpers
        dq      video_init
        dq      video_putpixel
        dq      video_fill_rect
        dq      video_blit_rect
        dq      video_puts
        dq      video_selftest
        dq      video_bgrx
        dq      video_clip_rect
        dq      vmm_map_mmio

; constant data emitted next to the code
        dq      video_font
        dq      video_font_end
        dq      video_st_colors
        dq      video_st_text

; contract constants, compared against hand written expectations
        dq      VIDEO_FB_VA
        dq      VIDEO_FB_VA_LIMIT
        dq      VIDEO_FB_MAX_SIZE
        dq      VIDEO_FB_MAX_PAGES
        dq      VIDEO_MAX_WIDTH
        dq      VIDEO_MAX_HEIGHT
        dq      VIDEO_BPP
        dq      VIDEO_FMT_BGRX
        dq      VIDEO_FLAG_FRAMEBUFFER
        dq      VIDEO_BOOTINFO_SIZE
        dq      VIDEO_BOOTINFO.flags
        dq      VIDEO_BOOTINFO.gop_framebuffer_addr
        dq      VIDEO_BOOTINFO.gop_framebuffer_size
        dq      VIDEO_BOOTINFO.gop_width
        dq      VIDEO_BOOTINFO.gop_height
        dq      VIDEO_BOOTINFO.gop_pixel_format
        dq      VIDEO_BOOTINFO.gop_pixels_per_line
        dq      VIDEO_FB_STATE.ready
        dq      VIDEO_FB_STATE.mapped_pages
        dq      VIDEO_FB_STATE.width
        dq      VIDEO_FB_STATE.height
        dq      VIDEO_FB_STATE.pitch
        dq      VIDEO_FB_STATE.stride
        dq      VIDEO_FB_STATE.format
        dq      VIDEO_FB_STATE.fb_pa
        dq      VIDEO_FB_STATE.fb_va
        dq      VIDEO_FB_STATE.fb_size
        dq      VIDEO_FB_STATE_size
        dq      VIDEO_FONT_WIDTH
        dq      VIDEO_FONT_HEIGHT
        dq      VIDEO_FONT_STRIDE
        dq      VIDEO_FONT_FIRST
        dq      VIDEO_FONT_LAST
        dq      VIDEO_FONT_GLYPHS
        dq      VIDEO_FONT_BYTES
        dq      VIDEO_ST_WIDTH
        dq      VIDEO_ST_HEIGHT
        dq      VIDEO_ST_BARS
        dq      VIDEO_ST_BAR_W
        dq      VIDEO_ST_RECT_X
        dq      VIDEO_ST_RECT_Y
        dq      VIDEO_ST_RECT_W
        dq      VIDEO_ST_RECT_H
        dq      VIDEO_ST_RECT_COLOR
        dq      VIDEO_ST_BORDER
        dq      VIDEO_ST_TEXT_X
        dq      VIDEO_ST_TEXT_Y
        dq      VIDEO_ST_TEXT_COLOR
        dq      VIDEO_ST_TEXT_LEN
        dq      VIDEO_OK
        dq      VIDEO_EINVAL
        dq      VIDEO_ENOTSUP
        dq      VIDEO_ENOTREADY
        dq      VIDEO_EALREADY
        dq      VIDEO_EIO
        dq      MM_MMIO_LAPIC_VA
        dq      MM_HEAP_LIMIT
        dq      VIDEO_PAGE_SIZE
fb_probe_manifest_end:

if (fb_probe_manifest_end - fb_probe_manifest) <> 80 * 8
        display "the manifest must keep its fixed entry count", 13, 10
        err
end if

; the MM dependency first: it provides vmm_map_mmio, the only external
; symbol the video module calls
include '../../kernel/mm/mm.asm'

; the module under test, its data block ends the image
include '../../kernel/video/video.asm'