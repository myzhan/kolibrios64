;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; FS-v1 M1 block layer: virtio-blk-pci-transitional, legacy PIO,
;; polled split virtqueue, read only.
;;
;; This file is the single aggregation point of the module. It emits
;; nothing but the code and the data of the sub files, in this order:
;;
;;   api.inc      private copy of the shared contract plus FS-v1 blk
;;   blk.inc      PCI scan, legacy bring-up and the blk_read backend
;;   selftest.inc the seven leg self test and its messages
;;   data.inc     every mutable byte, 4 KiB aligned at both ends
;;
;; data.inc must stay last: it ends on a page boundary and no code may
;; follow it, so the foundation can map the block RW/NX without
;; dragging instructions into the mapping. The vring, the DMA bounce
;; page and the request page inside it are device addressed through
;; the loader identity map.
;;
;; The module calls nothing outside its own files except the CPU
;; console for the self test failure report; it never panics and never
;; executes STI. Every unexpected condition is a signed EAX status.
;;
;; Standalone assembly (this is what the layout tests do) sets
;; BLK_HOSTED and supplies the origin; a kernel include leaves both
;; alone.
;;

if ~ defined BLK_HOSTED
        format binary
        org 0xFFFFFFFF80000000
end if

include 'api.inc'

include 'blk.inc'
include 'selftest.inc'
include 'data.inc'

if $ <> blk_data_end
        err 'nothing may follow the blk data region'
end if
