;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; GUI-G1 framebuffer video: linear framebuffer handover, 32bpp BGRX
;; primitives, 8x16 ASCII text and the frozen self test pattern.
;;
;; This file is the single aggregation point of the module. It emits
;; nothing but the code and the data of the sub files, in this order:
;;
;;   api.inc      private copy of the contract and the pattern spec
;;   fb.inc       video_init, the GOP handover and the MMIO mapping
;;   draw.inc     putpixel / fill_rect / blit_rect and their helpers
;;   text.inc     video_puts, the clipped 8x16 text renderer
;;   font.inc     the constant ASCII font subset, 1520 bytes
;;   selftest.inc the frozen deterministic pattern
;;   data.inc     every mutable byte, 4 KiB aligned at both ends
;;
;; data.inc must stay last: it ends on a page boundary and no code may
;; follow it, so a loader can map the block read write without dragging
;; instructions into the mapping.
;;
;; The module calls vmm_map_mmio of kernel/mm and nothing else outside
;; its own files: whoever includes video.asm must include mm.asm first.
;; It does not call the console or the serial port and never panics:
;; every unexpected condition is an EAX status of the public entries.
;;
;; Standalone assembly (this is what the layout tests do) sets
;; VIDEO_HOSTED and supplies the origin; a kernel include leaves both
;; alone. The .inc files carry no org and no use64 of their own.
;;

if ~ defined VIDEO_HOSTED
        format binary
        use64
        org 0xFFFFFFFF80000000
end if

include 'api.inc'

include 'fb.inc'
include 'draw.inc'
include 'text.inc'
include 'font.inc'
include 'selftest.inc'
include 'data.inc'