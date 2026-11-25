;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; FS-v1 M2 read-only FAT16 over the blk_read seam, task 132.
;;
;; This file is the single aggregation point of the module. It emits
;; nothing but the code and the data of the sub files, in this order:
;;
;;   api.inc      private copy of the shared contract plus FS-v1 fs
;;   fs.inc       mount, cache, path resolution and the six file calls
;;   selftest.inc the eight leg self test and its messages
;;   data.inc     every mutable byte, 4 KiB aligned at both ends
;;
;; data.inc must stay last: it ends on a page boundary and no code may
;; follow it, so the foundation can map the region RW/NX without
;; dragging instructions into the mapping.
;;
;; The module calls nothing outside its own files except blk_read and
;; the CPU console for the self test failure report; it never panics
;; and never executes STI. Every unexpected condition is a signed EAX
;; status. There is no write path anywhere in the module.
;;
;; Standalone assembly (this is what the layout tests do) sets
;; FS_HOSTED and supplies the origin; a kernel include leaves both
;; alone.
;;

if ~ defined FS_HOSTED
        format binary
        org 0xFFFFFFFF80000000
end if

include 'api.inc'

include 'fs.inc'
include 'selftest.inc'
include 'data.inc'

if $ <> fs_data_end
        err 'nothing may follow the fs data region'
end if