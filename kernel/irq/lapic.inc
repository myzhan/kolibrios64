;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; LAPIC bring-up behind vmm_map_mmio.
;;
;; Every LAPIC access in this module uses RSI = IRQ_LAPIC_VA and a dword
;; mov with a plain displacement, so the layout test can decode the write
;; sequence straight out of the emitted bytes.
;;
;; The bring-up order is frozen: TPR = 0, SVR = 0x1FF (enable + spurious
;; vector 0xFF), then timer/LINT0/LINT1/error masked, then a bounded flush
;; of whatever OVMF left in service: every set bit of ISR7..ISR0 gets one
;; EOI. At most 8 * 32 = 256 EOIs, no unbounded wait anywhere.
;;
;; The register walk MUST be descending. An EOI clears the highest
;; in-service vector, not the bit the software is looking at: scanning
;; ISR0 first while a higher register still holds a set bit would spend
;; the EOI on that higher vector and leave the low bit in service
;; forever. Walking ISR7 down to ISR0 guarantees the highest set bit
;; always lives in the register being scanned, so one EOI per snapshot
;; bit drains the ISR exactly.
;;

; ------------------------------------------------------------------
; bring the local APIC into the masked, drained state irq_init wants
; in
; nothing; the LAPIC page must already be mapped at IRQ_LAPIC_VA
; out
; eax = IRQ_OK
; note
; IF stays 0, the spurious vector is armed but every real source is
; masked until the self test opens its bounded windows
; ------------------------------------------------------------------

irq_lapic_bringup:
        irq_enter
        cld
        mov     rsi, IRQ_LAPIC_VA
        ; accept every priority, then enable with the spurious vector
        mov     dword [rsi + IRQ_LAPIC_TPR], 0
        mov     dword [rsi + IRQ_LAPIC_SVR], IRQ_SVR_VALUE
        ; every real source starts masked
        mov     dword [rsi + IRQ_LAPIC_LVT_TIMER], IRQ_LVT_MASK
        mov     dword [rsi + IRQ_LAPIC_LVT_LINT0], IRQ_LVT_MASK
        mov     dword [rsi + IRQ_LAPIC_LVT_LINT1], IRQ_LVT_MASK
        mov     dword [rsi + IRQ_LAPIC_LVT_ERROR], IRQ_LVT_MASK
        ; flush the firmware leftovers: one EOI per in-service bit,
        ; descending ISR7..ISR0 so every EOI clears a bit of the register
        ; being scanned (EOI always clears the highest in-service vector);
        ; bounded by the register count times the bit count
        mov     r8d, IRQ_ISR_REGS - 1
.reg_loop:
        mov     eax, r8d
        shl     eax, 4
        mov     edx, dword [rsi + rax + IRQ_LAPIC_ISR]
        mov     ecx, IRQ_ISR_BITS
.bit_loop:
        test    edx, 1
        jz      .next_bit
        mov     dword [rsi + IRQ_LAPIC_EOI], 0
.next_bit:
        shr     edx, 1
        dec     ecx
        jnz     .bit_loop
        dec     r8d
        jns     .reg_loop
        xor     eax, eax
        irq_leave
        ret
