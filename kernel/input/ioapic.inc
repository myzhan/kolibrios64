;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; IOAPIC bring-up: the indirect register window, the measured version
;; and the two frozen redirection entries.
;;
;; The RTE write order is frozen: high dword (destination) first, then
;; the low dword, and the low dword always carries mask = 1 during
;; bring-up. Only the self test legs ever unmask a pin. The 8259 pair
;; stays fully masked, the IRQ layer owns it.
;;

; ------------------------------------------------------------------
; indirect register access, leaves
; in  ecx = register index          out eax = value (read)
;     edx = value (write)
; clobbers rax, rdx (write keeps rdx), r8; no frame needed
; ------------------------------------------------------------------

input_ioapic_read:
        mov     r8, INPUT_IOAPIC_VA
        mov     dword [r8 + INPUT_IOAPIC_REGSEL], ecx
        mov     eax, dword [r8 + INPUT_IOAPIC_WIN]
        ret

input_ioapic_write:
        mov     r8, INPUT_IOAPIC_VA
        mov     dword [r8 + INPUT_IOAPIC_REGSEL], ecx
        mov     dword [r8 + INPUT_IOAPIC_WIN], edx
        ret

; ------------------------------------------------------------------
; measure VER, program both RTEs masked, verify by read back
; in
; nothing; the IOAPIC page must be mapped, IF must be 0
; out
; eax = INPUT_OK or INPUT_EIO, rdx = 0
; ------------------------------------------------------------------

input_ioapic_bringup:
        input_enter
        cld

        ; VER (index 1): low byte the version, bits 23..16 the maximum
        ; redirection entry index. Sanity floors only, the measured
        ; values are recorded as evidence, never assumed.
        mov     ecx, INPUT_IOAPIC_IDX_VER
        call    input_ioapic_read
        mov     [input_ioapic_ver], rax
        mov     edx, eax
        and     edx, 0xFF
        cmp     edx, INPUT_VER_MIN
        jb      .eio
        cmp     edx, 0xFF
        je      .eio
        shr     eax, 16
        and     eax, 0xFF
        inc     eax                     ; entry count = max index + 1
        mov     [input_ioapic_max], rax
        cmp     eax, INPUT_RTE_MIN
        jb      .eio

        ; pin 1 (keyboard): destination 0 first, then the masked RTE
        mov     ecx, INPUT_IOAPIC_IDX_KBD_HI
        xor     edx, edx
        call    input_ioapic_write
        mov     ecx, INPUT_IOAPIC_IDX_KBD_LO
        mov     edx, INPUT_RTE_KBD_MASKED
        call    input_ioapic_write

        ; pin 12 (mouse): same frozen order
        mov     ecx, INPUT_IOAPIC_IDX_MOUSE_HI
        xor     edx, edx
        call    input_ioapic_write
        mov     ecx, INPUT_IOAPIC_IDX_MOUSE_LO
        mov     edx, INPUT_RTE_MOUSE_MASKED
        call    input_ioapic_write

        ; read back all four dwords: vector, mask, polarity, trigger
        ; and destination must be exactly what was written
        mov     ecx, INPUT_IOAPIC_IDX_KBD_HI
        call    input_ioapic_read
        test    eax, eax
        jnz     .eio
        mov     ecx, INPUT_IOAPIC_IDX_KBD_LO
        call    input_ioapic_read
        cmp     eax, INPUT_RTE_KBD_MASKED
        jne     .eio
        mov     ecx, INPUT_IOAPIC_IDX_MOUSE_HI
        call    input_ioapic_read
        test    eax, eax
        jnz     .eio
        mov     ecx, INPUT_IOAPIC_IDX_MOUSE_LO
        call    input_ioapic_read
        cmp     eax, INPUT_RTE_MOUSE_MASKED
        jne     .eio

        xor     eax, eax
        xor     edx, edx
        input_leave
        ret
.eio:
        mov     eax, INPUT_EIO
        xor     edx, edx
        input_leave
        ret
