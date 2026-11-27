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
if defined K64_RUNTIME
video_fb_double:        db 0            ; one logical pixel covers 2x2 GOP pixels
video_gpu_dirty:        db 0
video_gpu_online:       db 0
video_gpu_inflight:     db 0
        align 8
gpu_pci:                dd 0
gpu_output_id:          dd 0
gpu_notify:             dq 0
gpu_config:             dq 0
gpu_dma_delta:          dq 0
gpu_next_frame:         dq 0
gpu_pages:              dd 0
gpu_width:              dd 0
gpu_height:             dd 0
        align 4096
gpu_descriptors:        db GPU_QUEUE_SIZE * 16 dup 0
gpu_available:          db 4 + GPU_QUEUE_SIZE * 2 dup 0
        align 4
gpu_used:               db 4 + GPU_QUEUE_SIZE * 8 dup 0
        align 16
gpu_response:           db GPU_RESPONSE_BYTES dup 0
        align 4096
gpu_request:            db GPU_REQUEST_BYTES dup 0
gpu_backing:            dq GPU_MAX_PAGES dup 0
gpu_columns:            dd GPU_MAX_WIDTH dup 0
        align 4096
gpu_canvas:             dd VIDEO_ST_WIDTH * VIDEO_ST_HEIGHT dup 0
end if

; tail padding, emitted as data so the file keeps every zero byte
        db (VIDEO_PAGE_SIZE - (($ - video_data_start) and (VIDEO_PAGE_SIZE-1))) and (VIDEO_PAGE_SIZE-1) dup 0
video_data_end:

if (video_data_end - video_data_start) mod VIDEO_PAGE_SIZE
        display "data block is not page aligned", 13, 10
        err
end if
