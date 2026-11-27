;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2026. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; APP-v1 hosted program loader over the frozen FS-v1 fs API, task 115.
;;
;; This file is the single aggregation point of the module. It emits
;; nothing but the code and the data of the sub files, in this order:
;;
;;   api.inc      the frozen APP-v1 integration contract + cross checks
;;   app.inc      the four handlers, the loader, teardown and redraw
;;   selftest.inc the bring-up state and handler table shape gate
;;   data.inc     every mutable byte, 4 KiB aligned at both ends
;;
;; data.inc must stay last: it ends on a page boundary and no code may
;; follow it, so the foundation can map the region RW/NX without
;; dragging instructions into the mapping.
;;
;; The module needs cpu (frame layout, syscall/fault registration,
;; RSP0), mm (resolve, user mapper, allocator), fs (the kernel-internal
;; file calls), video (the draw primitives), desktop (cursor sync/place,
;; the frozen frame constants) and input/irq (the public evidence
;; labels, the IOAPIC helper) included first; the kernel and the
;; stage2-app probe both do. The program image is NOT embedded: it is
;; prebuilt by build_fasm2.sh into build/user-chip8/chip8.bin and put
;; onto the app disk image as /PROGRAMS/CHIP8.BIN by
;; tools/build-fat16-image.py --manifest.
;;
;; Standalone assembly sets APP_HOSTED and supplies the origin; a
;; kernel include leaves both alone.
;;

if ~ defined APP_HOSTED
        format binary
        org 0xFFFFFFFF80000000
end if

include 'api.inc'

include 'app.inc'
if defined K64_RUNTIME
include 'tasks.inc'
include 'window.inc'
end if
include 'selftest.inc'
include 'data.inc'

if $ <> app_data_end
        err 'nothing may follow the app data region'
end if
