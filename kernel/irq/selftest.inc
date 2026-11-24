;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; The IRQ self test, the only STI owner in the whole kernel.
;;
;; The single sti instruction lives in irq_sti_wait below; every leg
;; opens its interrupt window through that helper and the helper always
;; executes cli before it returns, so no path can leak IF = 1.
;;
;; Legs (failure bits of irq_test_selftest_mask):
;;   0  PIC IMR read back 0xFF/0xFF
;;   1  SVR bit 8 set, spurious vector 0xFF registered (weak assertion),
;;      ID/VER fields plausible
;;   2  one-shot delivers exactly one tick; the handler evidence shows
;;      ISR bit 1 before the EOI and 0 after it
;;   3  periodic delivers exactly 20 ticks, then mask freezes the count
;;   4  a masked window adds nothing, unmasking resumes the count
;;   5  with irq_test_skip_eoi set the second tick is blocked by the
;;      in-service bit (bounded timeout proves it never arrives); the
;;      missing EOI releases the pending tick
;;   6  the handler runs with RFLAGS.IF = 0: the evidence pair is the
;;      CURRENT RFLAGS bit 9 read inside the handler (interrupt gate, 0)
;;      and frame.RFLAGS bit 9 (the interrupted STI window context, 0x200)
;;   7  the calibration result sits inside the 5 percent sanity band
;;   8  final leg: reset, periodic exactly 10 ticks, mask + cli freeze
;;
;; Every wait is bounded; a timeout or a wrong count sets the leg bit,
;; skips the remaining legs, prints the mask on COM1 and returns EIO so
;; the foundation takes the cpu_panic path. On exit the timer is masked,
;; IF = 0, irq_test_ticks == 10 and irq_test_selftest_mask == 0.
;;
;; Tick period: the frozen contract is 10 ms (100 Hz). Every arming site
;; below programs r14d = irq_calib_hz / IRQ_TICK_HZ, derived once from
;; the measured counter frequency, never a hardcoded count. The wait
;; budgets (IRQ_WAIT_SPIN, IRQ_ST_WINDOW) are sized for that period.
;;

; ------------------------------------------------------------------
; the one interrupt window helper
; in
; rcx = tick target, rdx = spin budget, r8d = mode:
;       0 wait until irq_test_ticks >= target (timeout = failure)
;       1 spin the whole budget, ignoring the count (window observation)
;       2 fail early if irq_test_ticks >= target (timeout = success)
; out
; eax = IRQ_OK or IRQ_EIO; IF is always 0 on return
; note
; leaf function: touches only caller saved registers, no frame needed
; ------------------------------------------------------------------

irq_sti_wait:
        sti
.loop:
        cmp     r8d, 1
        je      .spin
        cmp     qword [irq_test_ticks], rcx
        jae     .reached
.spin:
        dec     rdx
        jnz     .loop
        ; budget exhausted
        cli
        cmp     r8d, 0
        je      .timeout
        xor     eax, eax
        ret
.reached:
        cli
        cmp     r8d, 2
        je      .timeout
        xor     eax, eax
        ret
.timeout:
        mov     eax, IRQ_EIO
        ret

; ------------------------------------------------------------------
; run the nine legs
; in
; nothing; irq_init must have succeeded, IF must be 0
; out
; eax = IRQ_OK, or IRQ_EIO with rdx = the failed leg mask
; ------------------------------------------------------------------

irq_selftest:
        irq_enter
        cld
        cli
        xor     r12d, r12d              ; failure mask
        mov     rsi, IRQ_LAPIC_VA
        mov     dword [rsi + IRQ_LAPIC_LVT_TIMER], IRQ_LVT_MASK

        ; derive the 10 ms tick count from the measured frequency:
        ; r14 = irq_calib_hz / IRQ_TICK_HZ (nominal 62.5 MHz -> 625000),
        ; every initial-count write below uses r14d
        mov     rax, [irq_calib_hz]
        xor     edx, edx
        mov     ecx, IRQ_TICK_HZ
        div     rcx
        mov     r14, rax

        ; ---------------- leg 0: the PIC stays fully masked ----------------
        mov     dx, IRQ_PIC1_DATA
        in      al, dx
        cmp     al, IRQ_PIC_OCW1_MASK
        jne     irq_st_fail_pic
        mov     dx, IRQ_PIC2_DATA
        in      al, dx
        cmp     al, IRQ_PIC_OCW1_MASK
        jne     irq_st_fail_pic

        ; ---------------- leg 1: SVR, spurious vector, ID/VER ----------------
        mov     eax, dword [rsi + IRQ_LAPIC_SVR]
        test    eax, IRQ_SVR_ENABLE
        jz      irq_st_fail_svr
        and     eax, 0xFF
        cmp     eax, IRQ_VEC_SPURIOUS
        jne     irq_st_fail_svr
        mov     eax, dword [rsi + IRQ_LAPIC_VER]
        mov     edx, eax
        and     edx, 0xFF
        cmp     edx, 0x10               ; a real LAPIC version, not 0/FF
        jb      irq_st_fail_svr
        shr     eax, 16
        and     eax, 0xFF
        cmp     eax, 4                  ; max LVT covers timer..error
        jb      irq_st_fail_svr
        mov     eax, dword [rsi + IRQ_LAPIC_ID]
        shr     eax, 24
        cmp     eax, 0xFE               ; the BSP id is not the broadcast id
        ja      irq_st_fail_svr

        ; ---------------- leg 2: one-shot, exactly one tick ----------------
        mov     qword [irq_test_ticks], 0
        mov     qword [irq_test_isr_seen], 0
        mov     qword [irq_test_isr_after], 0
        mov     dword [rsi + IRQ_LAPIC_DIVIDE], IRQ_DIVIDE_16
        mov     dword [rsi + IRQ_LAPIC_LVT_TIMER], IRQ_VEC_TIMER or IRQ_LVT_ONESHOT
        mov     dword [rsi + IRQ_LAPIC_INIT_COUNT], r14d
        mov     rcx, 1
        mov     rdx, IRQ_WAIT_SPIN
        xor     r8d, r8d
        call    irq_sti_wait
        test    eax, eax
        jnz     irq_st_fail_oneshot
        mov     dword [rsi + IRQ_LAPIC_LVT_TIMER], IRQ_LVT_MASK
        cmp     qword [irq_test_ticks], 1
        jne     irq_st_fail_oneshot
        cmp     qword [irq_test_isr_seen], 1
        jne     irq_st_fail_oneshot
        cmp     qword [irq_test_isr_after], 0
        jne     irq_st_fail_oneshot

        ; ---------------- leg 3: periodic, exactly 20 ticks ----------------
        mov     qword [irq_test_ticks], 0
        mov     dword [rsi + IRQ_LAPIC_LVT_TIMER], IRQ_VEC_TIMER or IRQ_LVT_PERIODIC
        mov     dword [rsi + IRQ_LAPIC_INIT_COUNT], r14d
        mov     rcx, IRQ_ST_PERIODIC_N
        mov     rdx, IRQ_WAIT_SPIN
        xor     r8d, r8d
        call    irq_sti_wait
        test    eax, eax
        jnz     irq_st_fail_periodic
        mov     dword [rsi + IRQ_LAPIC_LVT_TIMER], IRQ_LVT_PERIODIC or IRQ_LVT_MASK
        cmp     qword [irq_test_ticks], IRQ_ST_PERIODIC_N
        jne     irq_st_fail_periodic

        ; ---------------- leg 4: masked window, then resume ----------------
        mov     r13, [irq_test_ticks]
        xor     ecx, ecx
        mov     rdx, IRQ_ST_WINDOW
        mov     r8d, 1
        call    irq_sti_wait
        cmp     qword [irq_test_ticks], r13
        jne     irq_st_fail_mask
        mov     dword [rsi + IRQ_LAPIC_LVT_TIMER], IRQ_VEC_TIMER or IRQ_LVT_PERIODIC
        mov     rcx, r13
        inc     rcx
        mov     rdx, IRQ_WAIT_SPIN
        xor     r8d, r8d
        call    irq_sti_wait
        test    eax, eax
        jnz     irq_st_fail_mask
        mov     dword [rsi + IRQ_LAPIC_LVT_TIMER], IRQ_LVT_PERIODIC or IRQ_LVT_MASK

        ; ---------------- leg 5: no EOI blocks the second tick ----------------
        mov     qword [irq_test_ticks], 0
        mov     qword [irq_test_skip_eoi], 1
        mov     dword [rsi + IRQ_LAPIC_LVT_TIMER], IRQ_VEC_TIMER or IRQ_LVT_ONESHOT
        mov     dword [rsi + IRQ_LAPIC_INIT_COUNT], r14d
        mov     rcx, 1
        mov     rdx, IRQ_WAIT_SPIN
        xor     r8d, r8d
        call    irq_sti_wait
        test    eax, eax
        jnz     irq_st_fail_noeoi
        ; re-arm while the first tick is still in service: the second
        ; expiry may latch in IRR but must never be delivered
        mov     dword [rsi + IRQ_LAPIC_INIT_COUNT], r14d
        mov     rcx, 2
        mov     rdx, IRQ_ST_WINDOW
        mov     r8d, 2
        call    irq_sti_wait
        test    eax, eax
        jnz     irq_st_fail_noeoi
        cmp     qword [irq_test_ticks], 1
        jne     irq_st_fail_noeoi
        ; recovery: clear the hook and send the missing EOI, the pending
        ; tick must now arrive
        mov     qword [irq_test_skip_eoi], 0
        mov     dword [rsi + IRQ_LAPIC_EOI], 0
        mov     rcx, 2
        mov     rdx, IRQ_WAIT_SPIN
        xor     r8d, r8d
        call    irq_sti_wait
        test    eax, eax
        jnz     irq_st_fail_noeoi
        mov     dword [rsi + IRQ_LAPIC_LVT_TIMER], IRQ_LVT_MASK
        cmp     qword [irq_test_ticks], 2
        jne     irq_st_fail_noeoi

        ; ---------------- leg 6: the handler runs with IF = 0 ------------
        ; spec note: frame.RFLAGS is the interrupted context (IF = 1 in
        ; the STI window); the interrupt-gate proof is the handler's own
        ; RFLAGS bit 9 == 0, paired with frame bit 9 == 1 as complementary
        ; evidence that the frame semantics are intact
        mov     qword [irq_test_ticks], 0
        mov     qword [irq_test_if_value], -1
        mov     qword [irq_test_frame_if], -1
        mov     dword [rsi + IRQ_LAPIC_LVT_TIMER], IRQ_VEC_TIMER or IRQ_LVT_ONESHOT
        mov     dword [rsi + IRQ_LAPIC_INIT_COUNT], r14d
        mov     rcx, 1
        mov     rdx, IRQ_WAIT_SPIN
        xor     r8d, r8d
        call    irq_sti_wait
        test    eax, eax
        jnz     irq_st_fail_if
        mov     dword [rsi + IRQ_LAPIC_LVT_TIMER], IRQ_LVT_MASK
        cmp     qword [irq_test_if_value], 0
        jne     irq_st_fail_if
        cmp     qword [irq_test_frame_if], IRQ_RFLAGS_IF
        jne     irq_st_fail_if

        ; ---------------- leg 7: calibration sanity ----------------
        mov     rax, [irq_calib_hz]
        test    rax, rax
        jz      irq_st_fail_calib
        sub     rax, IRQ_CALIB_NOMINAL
        jns     irq_st_calib_band
        neg     rax
irq_st_calib_band:
        cmp     rax, IRQ_CALIB_TOL
        ja      irq_st_fail_calib

        ; ---------------- leg 8: final freeze at exactly 10 ticks ----------
        mov     qword [irq_test_ticks], 0
        mov     dword [rsi + IRQ_LAPIC_LVT_TIMER], IRQ_VEC_TIMER or IRQ_LVT_PERIODIC
        mov     dword [rsi + IRQ_LAPIC_INIT_COUNT], r14d
        mov     rcx, IRQ_ST_FINAL_N
        mov     rdx, IRQ_WAIT_SPIN
        xor     r8d, r8d
        call    irq_sti_wait
        test    eax, eax
        jnz     irq_st_fail_final
        mov     dword [rsi + IRQ_LAPIC_LVT_TIMER], IRQ_LVT_PERIODIC or IRQ_LVT_MASK
        cmp     qword [irq_test_ticks], IRQ_ST_FINAL_N
        jne     irq_st_fail_final

        ; success: timer masked, IF = 0, ticks frozen at 10, mask zero
        mov     [irq_test_selftest_mask], r12
        xor     eax, eax
        xor     edx, edx
        irq_leave
        ret

irq_st_fail_pic:
        or      r12, 1 shl IRQ_ST_BIT_PIC
        jmp     irq_st_failed
irq_st_fail_svr:
        or      r12, 1 shl IRQ_ST_BIT_SVR
        jmp     irq_st_failed
irq_st_fail_oneshot:
        or      r12, 1 shl IRQ_ST_BIT_ONESHOT
        jmp     irq_st_failed
irq_st_fail_periodic:
        or      r12, 1 shl IRQ_ST_BIT_PERIODIC
        jmp     irq_st_failed
irq_st_fail_mask:
        or      r12, 1 shl IRQ_ST_BIT_MASK
        jmp     irq_st_failed
irq_st_fail_noeoi:
        or      r12, 1 shl IRQ_ST_BIT_NOEOI
        jmp     irq_st_failed
irq_st_fail_if:
        or      r12, 1 shl IRQ_ST_BIT_IF
        jmp     irq_st_failed
irq_st_fail_calib:
        or      r12, 1 shl IRQ_ST_BIT_CALIB
        jmp     irq_st_failed
irq_st_fail_final:
        or      r12, 1 shl IRQ_ST_BIT_FINAL
irq_st_failed:
        ; freeze everything, then report the mask before returning EIO
        cli
        mov     dword [rsi + IRQ_LAPIC_LVT_TIMER], IRQ_LVT_MASK
        mov     qword [irq_test_skip_eoi], 0
        mov     [irq_test_selftest_mask], r12
        mov     rcx, msg_irq_selftest
        mov     rdx, IRQ_PUTS_MAX
        call    cpu_com1_puts
        mov     rcx, [irq_test_selftest_mask]
        mov     rdx, IRQ_PUTHEX_DIGITS
        call    cpu_com1_puthex
        mov     rcx, msg_irq_nl
        mov     rdx, IRQ_PUTS_MAX
        call    cpu_com1_puts
        mov     rdx, r12
        mov     eax, IRQ_EIO
        irq_leave
        ret
