;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; DESKTOP-v1 minimal shell: frozen background and title bar, a saved
;; background mouse cursor, the keyboard echo and the resident event
;; loop, all evidenced through host injection.
;;
;; This file is the single aggregation point of the module. It emits
;; nothing but the code and the data of the sub files, in this order:
;;
;;   api.inc      private copy of the DESKTOP-v1 contract
;;   desktop.inc  init, the bounded handshake wait, pump, desktop_run
;;   cursor.inc   save / restore / composite / draw and the sprite
;;   echo.inc     the key buffer pump, the echo and the title text
;;   selftest.inc the six leg host handshake self test
;;   data.inc     every mutable byte, 4 KiB aligned at both ends
;;
;; data.inc must stay last: it ends on a page boundary and no code may
;; follow it, so a loader can map the block read write without dragging
;; instructions into the mapping.
;;
;; The module calls the video primitives (including video_readback_rect,
;; the only sanctioned framebuffer read path), input_ioapic_write and
;; irq_sti_wait, reads the public input/irq/video evidence labels and
;; the bounded CPU console, and nothing else outside its own files:
;; whoever includes desktop.asm must include mm.asm, irq.asm, input.asm
;; and video.asm first.
;;
;; desktop_run is the SECOND legal STI owner of the whole kernel (the
;; first is the irq_sti_wait self test window); desktop.inc carries
;; exactly one STI, always after the LAPIC LVT timer mask. The module
;; never panics on its own: every unexpected condition is an EAX status
;; the foundation turns into the standard fatal path, or the non fatal
;; K64-DESKTOP-SKIP when the framebuffer is absent.
;;
;; Standalone assembly (this is what the layout tests do) leaves
;; DESKTOP_HOSTED undefined and gets the format/org pair; a kernel
;; include sets DESKTOP_HOSTED and supplies both. The .inc files carry
;; no org and no use64 of their own.
;;

if ~ defined DESKTOP_HOSTED
        format binary
        use64
        org 0xFFFFFFFF80000000
end if

desktop_code_start:

include 'api.inc'

include 'desktop.inc'
include 'cursor.inc'
include 'echo.inc'
include 'selftest.inc'

desktop_code_end:

include 'data.inc'
