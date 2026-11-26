;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; DESKTOP-v2 shell: the wallpaper background and the taskbar with a
;; start button, a saved background mouse cursor, the click pump with
;; the single entry start menu, the keyboard echo and the resident
;; event loop, all evidenced through host injection.
;;
;; This file is the single aggregation point of the module. It emits
;; nothing but the code and the data of the sub files, in this order:
;;
;;   api.inc      private copy of the DESKTOP-v1/v2 contract
;;   desktop.inc  init, the bounded handshake wait, pump, desktop_run
;;   cursor.inc   save / restore / composite / draw and the sprite
;;   echo.inc     the key buffer pump, the echo and the title text
;;   menu.inc     the click edge pump and the start menu open/close
;;   taskbar.inc  the repaint base, the app launch pump, the boot wrap
;;   selftest.inc the six leg host handshake self test
;;   wallpaper.inc the read only 1024x768 BGRX wallpaper (incbin)
;;   data.inc     every mutable byte, 4 KiB aligned at both ends
;;
;; data.inc must stay last: it ends on a page boundary and no code may
;; follow it, so a loader can map the block read write without dragging
;; instructions into the mapping.
;;
;; The module calls the video primitives (including video_readback_rect,
;; the only sanctioned framebuffer read path), input_ioapic_write and
;; irq_sti_wait, reads the public input/irq/video evidence labels and
;; the bounded CPU console; task 125 adds exactly one external call,
;; app_launch through desktop_launch_pump (the hosted program leg),
;; and nothing else outside its own files: whoever includes desktop.asm
;; must include mm.asm, irq.asm, input.asm and video.asm first and
;; resolve app_launch (the kernel includes app.asm, the standalone
;; layout test supplies a stub).
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
include 'menu.inc'
include 'taskbar.inc'
include 'selftest.inc'
include 'wallpaper.inc'

desktop_code_end:

include 'data.inc'
