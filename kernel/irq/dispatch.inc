;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; The asynchronous handler the CPU dispatcher calls with RCX = frame.
;;
;; Contract IRQ-v1:
;;   vector 0x20  increment irq_tick_count and irq_test_ticks, record the
;;                in-service evidence and the IF evidence pair (current
;;                RFLAGS bit 9 = 0, interrupted frame bit 9 = 0x200),
;;                then write LAPIC EOI = 0. The
;;                leg 5 switch irq_test_skip_eoi suppresses exactly the
;;                EOI of the ticks that arrive while it is set; it defaults
;;                to zero and only the self test ever touches it.
;;   vector 0xFF  increment the spurious count, never an EOI.
;;   anything else  cpu_panic inside the handler: the CPU dispatcher
;;                iretqs as soon as this function returns, so an unknown
;;                vector must never reach that return.
;;
;; No heap allocation, no unbounded wait, no serial output on any normal
;; path. The uniform 128 byte frame keeps RSP 16 byte aligned; RSI is the
;; only callee saved register this body uses and the frame restores it.
;;

irq_dispatch:
        push    rbp
        mov     rbp, rsp
        sub     rsp, IRQ_STACK_FRAME
        mov     [rbp-8], rcx
        mov     rsi, IRQ_LAPIC_VA
        mov     rax, [rcx + IRQ_FRAME_VECTOR]
        cmp     rax, IRQ_VEC_TIMER
        je      .timer
        cmp     rax, IRQ_VEC_SPURIOUS
        je      .spurious
        ; unknown vector: panic here, returning would iretq into the wild
        mov     rcx, msg_irq_unknown
        mov     rdx, [rbp-8]
        mov     r8, IRQ_EIO
        call    cpu_panic
        jmp     cpu_halt_forever
.timer:
        inc     qword [irq_tick_count]
        inc     qword [irq_test_ticks]
        ; evidence pair for leg 6: the handler itself must run with
        ; IF = 0 (the IDT gate is an interrupt gate), so the CURRENT
        ; RFLAGS bit 9 read here is 0; frame.RFLAGS holds the interrupted
        ; context, which legitimately ran inside the STI window with
        ; IF = 1, and that complementary bit is recorded as well
        pushfq
        pop     rax
        and     eax, IRQ_RFLAGS_IF
        mov     [irq_test_if_value], rax
        mov     rax, [rbp-8]
        mov     rax, [rax + IRQ_FRAME_RFLAGS]
        and     eax, IRQ_RFLAGS_IF
        mov     [irq_test_frame_if], rax
        ; evidence: the timer vector must be in service before the EOI
        mov     edx, dword [rsi + IRQ_LAPIC_ISR + ((IRQ_VEC_TIMER shr 5) * 0x10)]
        and     edx, 1 shl (IRQ_VEC_TIMER and 31)
        mov     [irq_test_isr_seen], rdx
        ; leg 5 hook: skip exactly the EOI while the switch is set
        cmp     qword [irq_test_skip_eoi], 0
        jne     .done
        mov     dword [rsi + IRQ_LAPIC_EOI], 0
        ; evidence: after the EOI the in-service bit must be gone
        mov     edx, dword [rsi + IRQ_LAPIC_ISR + ((IRQ_VEC_TIMER shr 5) * 0x10)]
        and     edx, 1 shl (IRQ_VEC_TIMER and 31)
        mov     [irq_test_isr_after], rdx
        jmp     .done
.spurious:
        inc     qword [irq_spurious_count]
        ; the spurious vector gets no EOI, that is the LAPIC contract
.done:
        leave
        ret
