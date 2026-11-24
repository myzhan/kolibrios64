;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Stage-2 physical and virtual memory plus the early bump heap.
;;
;; This file is the single aggregation point of the module. It emits
;; nothing but the code and the data of the sub files, in this order:
;;
;;   api.inc      private copy of the K64-FOUNDATION-v1 contract
;;   pmm.inc      physical page allocator
;;   bootmap.inc  BootInfo parser and the boot page maps
;;   vmm.inc      virtual memory on the loader CR3
;;   heap.inc     the early bump heap
;;   selftest.inc the aggregated self test
;;   data.inc     every mutable byte, 4 KiB aligned at both ends
;;
;; data.inc must stay last: it ends on a page boundary and no code may
;; follow it, so a loader can map the block read write without dragging
;; instructions into the mapping.
;;
;; The module does not call the console or the serial port and never
;; panics: every unexpected condition is reported through the EAX status
;; of the public entry points.
;;
;; Standalone assembly (this is what the layout tests do) sets MM_HOSTED
;; and supplies the origin; a kernel include leaves both alone.
;;

if ~ defined MM_HOSTED
        format binary
        org 0xFFFFFFFF80000000
end if

include 'api.inc'

include 'pmm.inc'
include 'bootmap.inc'
include 'vmm.inc'
include 'heap.inc'
include 'selftest.inc'
include 'data.inc'
