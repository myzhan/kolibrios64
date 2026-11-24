;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Stage-2 memory management: aggregated self test.
;;
;; The three sub tests run in the order the module is initialised:
;; the physical allocator first, then the virtual memory on top of it,
;; then the heap. Each returns its own failure mask in RDX; this entry
;; point shifts them apart so one mask identifies the failing layer:
;;
;;   bits 0-15   pmm_selftest
;;   bits 16-31  vmm_selftest
;;   bits 32-47  heap_selftest
;;
;; A layer that is not initialised returns ENOTREADY with an empty mask;
;; that is folded into bit 0 of its own group so the aggregated result is
;; never a silent success.
;;

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; run every layer self test
; in
; nothing
; out
; eax = MM_OK, MM_EIO when a step failed, MM_NOTREADY when the module is
;       not initialised at all
; rdx = aggregated failure mask
mm_selftest:
        mm_enter
        cld
        xor     r12d, r12d

        call    pmm_selftest
        test    eax, eax
        jz      .pmm_done
        test    rdx, rdx
        jnz     .pmm_done
        mov     edx, 1
.pmm_done:
        mov     r12, rdx

        call    vmm_selftest
        test    eax, eax
        jz      .vmm_done
        test    rdx, rdx
        jnz     .vmm_done
        mov     edx, 1
.vmm_done:
        shl     rdx, 16
        or      r12, rdx

        call    heap_selftest
        test    eax, eax
        jz      .heap_done
        test    rdx, rdx
        jnz     .heap_done
        mov     edx, 1
.heap_done:
        shl     rdx, 32
        or      r12, rdx

        mov     qword [mm_test_selftest_mask], r12
        mov     rdx, r12
        test    r12, r12
        jnz     .broken
        xor     eax, eax
        jmp     .done
.broken:
        mov     eax, MM_EIO
.done:
        mm_leave
        ret
