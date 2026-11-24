;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; PIT channel 2 calibration of the LAPIC timer counter frequency.
;;
;; One round: PIT ch2 in mode 0 with the gate on and the speaker off,
;; loaded with 0xFFFF (about 54.9 ms at 1.193182 MHz); the LAPIC counter
;; runs masked and one-shot with divide = 16 from 0xFFFFFFFF; a bounded
;; poll of port 0x61 bit 5 (OUT2) marks the end of the window; the current
;; count difference times the PIT frequency over the window length gives
;; the counter frequency. Three rounds, the median wins.
;;
;; Sanity: under TCG the LAPIC bus clock is a fixed 1 GHz, so the result
;; must sit within 5 percent of 1 GHz / 16 = 62.5 MHz. The PIT mode 0
;; quirk (the counter effectively runs initial_count + 1 cycles) shifts
;; the window by one part in 65536, orders of magnitude below the band,
;; so it is deliberately ignored. No IRQ is needed or enabled here.
;;

; ------------------------------------------------------------------
; measure the LAPIC counter frequency, median of three bounded rounds
; in
; nothing; the LAPIC page must be mapped, IF must be 0
; out
; eax = IRQ_OK with irq_calib_hz and the three samples filled,
;       IRQ_EIO on a poll timeout, a zero divisor or an out-of-band result
; ------------------------------------------------------------------

irq_calibrate:
        irq_enter
        cld
        mov     rsi, IRQ_LAPIC_VA
        mov     r12d, IRQ_CALIB_ROUNDS
.round:
        ; PIT channel 2, lo/hi access, mode 0, binary
        mov     dx, IRQ_PIT_CMD
        mov     al, IRQ_PIT_MODE0_CH2
        out     dx, al
        ; gate on, speaker off
        mov     dx, IRQ_PIT_CTRL
        in      al, dx
        and     al, not (IRQ_PIT_GATE or IRQ_PIT_SPEAKER)
        or      al, IRQ_PIT_GATE
        out     dx, al
        ; load the known window length, low byte then high byte
        mov     dx, IRQ_PIT_CH2_DATA
        mov     al, IRQ_PIT_COUNT and 0xFF
        out     dx, al
        mov     al, (IRQ_PIT_COUNT shr 8) and 0xFF
        out     dx, al
        ; LAPIC counter: divide 16, masked one-shot, maximum initial count
        mov     dword [rsi + IRQ_LAPIC_DIVIDE], IRQ_DIVIDE_16
        mov     dword [rsi + IRQ_LAPIC_LVT_TIMER], IRQ_LVT_MASK
        mov     dword [rsi + IRQ_LAPIC_INIT_COUNT], 0xFFFFFFFF
        ; bounded wait for OUT2: the window is over when bit 5 rises
        mov     ecx, IRQ_CALIB_SPIN
.poll:
        mov     dx, IRQ_PIT_CTRL
        in      al, dx
        test    al, IRQ_PIT_OUT2
        jnz     .window_done
        dec     ecx
        jnz     .poll
        jmp     .fail
.window_done:
        ; read the elapsed LAPIC ticks immediately, then close the gate
        mov     edx, dword [rsi + IRQ_LAPIC_CUR_COUNT]
        mov     dx, IRQ_PIT_CTRL
        in      al, dx
        and     al, not IRQ_PIT_GATE
        out     dx, al
        ; hz = (0xFFFFFFFF - current) * PIT_HZ / PIT_COUNT
        mov     eax, 0xFFFFFFFF
        sub     eax, edx
        mov     rcx, IRQ_PIT_HZ
        mul     rcx
        mov     rcx, IRQ_PIT_COUNT
        test    rcx, rcx                ; explicit zero-divisor guard
        jz      .fail
        div     rcx
        ; store the sample in its round slot
        mov     edx, IRQ_CALIB_ROUNDS
        sub     edx, r12d
        lea     rcx, [irq_calib_s0]
        mov     [rcx + rdx*8], rax
        dec     r12d
        jnz     .round
        ; median of three: two compare-swap passes leave r9 in the middle
        mov     r8, [irq_calib_s0]
        mov     r9, [irq_calib_s1]
        mov     r10, [irq_calib_s2]
        cmp     r8, r9
        jbe     .median1
        xchg    r8, r9
.median1:
        cmp     r9, r10
        jbe     .median2
        xchg    r9, r10
.median2:
        cmp     r8, r9
        jbe     .median3
        xchg    r8, r9
.median3:
        mov     [irq_calib_hz], r9
        ; sanity band: |hz - 62.5 MHz| <= 5 percent
        mov     rax, r9
        sub     rax, IRQ_CALIB_NOMINAL
        jns     .band
        neg     rax
.band:
        cmp     rax, IRQ_CALIB_TOL
        ja      .fail
        xor     eax, eax
        irq_leave
        ret
.fail:
        ; leave the gate closed on every failure path
        mov     dx, IRQ_PIT_CTRL
        in      al, dx
        and     al, not IRQ_PIT_GATE
        out     dx, al
        mov     eax, IRQ_EIO
        irq_leave
        ret
