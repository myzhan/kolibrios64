;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; GUI-G1 video: all mutable state.
;;
;; The data block is emitted as real zero bytes in the flat image: the
;; framebuffer state must exist before video_init fills it, and the
;; module must not depend on a BSS loader. The block is 4 KiB aligned at
;; both ends and nothing may follow it, so video.asm includes this file
;; last. Every field is written with "db N dup 0" instead of "rb": this
;; fasm2 build drops a trailing uninitialised area from the output file.
;; The block closes with an explicit tail of zero bytes up to the page
;; boundary, which keeps the last page in the file.
;;

        align 4096
video_data_start:

; the frozen 64 byte framebuffer state: geometry, stride = pitch * 4,
; the mapping progress counter and the ready flag
video_fb_state:         db VIDEO_FB_STATE_size dup 0

; the failing layer of the last video_selftest run, 0 = success
video_selftest_mask:    dq 0

; tail padding, emitted as data so the file keeps every zero byte
        db (VIDEO_PAGE_SIZE - (($ - video_data_start) and (VIDEO_PAGE_SIZE-1))) and (VIDEO_PAGE_SIZE-1) dup 0
video_data_end:

if (video_data_end - video_data_start) mod VIDEO_PAGE_SIZE
        display "data block is not page aligned", 13, 10
        err
end if