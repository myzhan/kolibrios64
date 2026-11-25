;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2026. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; FS-v1 M3 file syscall adapter over the frozen M2 fs API, task 133.
;;
;; This file is the single aggregation point of the module. It emits
;; nothing but the code and the data of the sub files, in this order:
;;
;;   api.inc      the frozen M3 contract and the cross checks
;;   fsys.inc     the six handlers, the safety helpers, init, probe
;;   data.inc     every mutable byte, 4 KiB aligned at both ends
;;
;; data.inc must stay last: it ends on a page boundary and no code may
;; follow it, so the foundation can map the region RW/NX without
;; dragging instructions into the mapping.
;;
;; The module needs cpu (frame layout, syscall registration, console),
;; mm (resolve, user mapper, allocator) and fs (the six file calls)
;; included first; the kernel and the stage2-fsys probe both do. The
;; embedded fsprobe image is prebuilt by build_fasm2.sh into
;; build/user-fsprobe/fsprobe.bin.
;;
;; Standalone assembly sets FSYS_HOSTED and supplies the origin; a
;; kernel include leaves both alone.
;;

if ~ defined FSYS_HOSTED
        format binary
        org 0xFFFFFFFF80000000
end if

include 'api.inc'

include 'fsys.inc'
include 'data.inc'

if $ <> fsys_data_end
        err 'nothing may follow the fsys data region'
end if