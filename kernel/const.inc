;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;


VIRT_KERNEL_BASE = 0xFFFFFFFF80000000

struct DQ
        union
                lo    dd ?
                hi_be dd ?      ; big endian
        ends
        union
                hi    dd ?
                lo_be dd ?
        ends
ends