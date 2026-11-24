;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; IDT traps: one stub per vector, and the dispatcher behind them.
;;
;; The 256 stubs are generated from one template, so the frame they build is
;; identical for every vector. The two things that differ are the error code a
;; vector has already pushed (the ten vectors in cpu_classify_vector) and the
;; vector number itself. A vector with an error code pushes nothing, everything
;; else pushes a zero dummy, then every stub pushes its vector. The vector is
;; pushed as a qword immediate: push imm8 sign extends, so vectors 128..255
;; would become negative and the stub would record 0xFFFFFF80 for 0x80.
;;
;; Stub prologue, highest address first:
;;
;;   [error | dummy 0] [vector] [rax rcx rdx rbx rbp rsi rdi r8..r15] [cr2]
;;
;; which is exactly the 184 byte frame in api.inc: cr2 at 0, r15..rax at
;; 8..120, vector at 128, error at 136, then the hardware frame. The stub then
;; clears DF, aligns RSP down to 16 (the frame size is not a multiple of 16),
;; reserves the 32 byte shadow space and calls the dispatcher with the frame
;; pointer. The dispatcher may rewrite frame.RIP, and only frame.RIP; the stub
;; restores the original RSP and every register, drops vector and error, and
;; returns with iretq.
;;
;; Stubs have different lengths by design: the four IST vectors and the ten
;; error code vectors are not the same size, so nothing may index them by a
;; fixed stride. They are only reachable through the IDT or their label.
;;

cpu_trap_stubs_start:

repeat CPU_IDT_VECTORS, v:0
        cpu_classify_vector v
cpu_stub_#v:
        if cpu_vec_is_error = 0
                push    qword 0
        end if
        push    qword v
        push    rax
        push    rcx
        push    rdx
        push    rbx
        push    rbp
        push    rsi
        push    rdi
        push    r8
        push    r9
        push    r10
        push    r11
        push    r12
        push    r13
        push    r14
        push    r15
        mov     rax, cr2
        push    rax
        cld
        mov     rbp, rsp
        and     rsp, not 15
        sub     rsp, 32
        mov     rcx, rbp
        call    cpu_trap_dispatch
        mov     rsp, rbp
        pop     rax
        pop     r15
        pop     r14
        pop     r13
        pop     r12
        pop     r11
        pop     r10
        pop     r9
        pop     r8
        pop     rdi
        pop     rsi
        pop     rbp
        pop     rbx
        pop     rdx
        pop     rcx
        pop     rax
        add     rsp, 16
        iretq
end repeat

cpu_trap_stubs_end:

; ------------------------------------------------------------------
; arm a single expected trap
; in
; rcx = vector, one of 0, 3, 6, 14, 0xF0, 0xF1
; rdx = the RIP the hardware will save on entry, compared exactly
; r8  = resume RIP, the address the stub returns to instead
; r9  = CPU_EXPECT * describing the error code and CR2, or 0 for "any"
; out
; eax = 0 on success, EINVAL for an unsupported vector or a non canonical
;       address, EBUSY when a trap is already armed or a result is pending
;
; Both addresses must be canonical in the kernel half, which also rejects a
; zero. The expectation block is copied, not referenced: arming is complete
; when this call returns and the caller may reuse its own buffer. A mask of
; zero accepts any value, so the zero block pointer needs no special case in
; the dispatcher.
; ------------------------------------------------------------------

cpu_trap_expect:
        push    rbp
        mov     rbp, rsp
        sub     rsp, CPU_STACK_FRAME
        ; the six vectors this stage knows how to resume from
        cmp     rcx, CPU_VEC_DE
        je      .vector_ok
        cmp     rcx, CPU_VEC_BP
        je      .vector_ok
        cmp     rcx, CPU_VEC_UD
        je      .vector_ok
        cmp     rcx, 14
        je      .vector_ok
        cmp     rcx, CPU_VEC_TEST
        je      .vector_ok
        cmp     rcx, CPU_VEC_IST_TEST
        jne     .invalid
.vector_ok:
        mov     rax, rdx
        shr     rax, CPU_KERNEL_SHIFT
        cmp     rax, CPU_KERNEL_TOP
        jne     .invalid
        mov     rax, r8
        shr     rax, CPU_KERNEL_SHIFT
        cmp     rax, CPU_KERNEL_TOP
        jne     .invalid
        ; one armed trap and one unconsumed result at a time
        cmp     byte [cpu_trap_armed], 0
        jne     .busy
        cmp     byte [cpu_trap_result_ready], 0
        jne     .busy
        mov     [cpu_trap_vector], cl
        mov     [cpu_trap_expect_rip], rdx
        mov     [cpu_trap_resume_rip], r8
        ; no block: both masks zero, so any error code and any CR2 match
        mov     qword [cpu_trap_err_mask], 0
        mov     qword [cpu_trap_err_value], 0
        mov     qword [cpu_trap_cr2_mask], 0
        mov     qword [cpu_trap_cr2_value], 0
        test    r9, r9
        jz      .armed
        mov     rax, [r9 + CPU_EXPECT_OFF_ERROR_MASK]
        mov     [cpu_trap_err_mask], rax
        mov     rax, [r9 + CPU_EXPECT_OFF_ERROR_VALUE]
        mov     [cpu_trap_err_value], rax
        mov     rax, [r9 + CPU_EXPECT_OFF_CR2_MASK]
        mov     [cpu_trap_cr2_mask], rax
        mov     rax, [r9 + CPU_EXPECT_OFF_CR2_VALUE]
        mov     [cpu_trap_cr2_value], rax
.armed:
        mov     byte [cpu_trap_armed], 1
        xor     eax, eax
        leave
        ret
.busy:
        mov     eax, CPU_EBUSY
        leave
        ret
.invalid:
        mov     eax, CPU_EINVAL
        leave
        ret

; ------------------------------------------------------------------
; collect, or drop, the result of the trap that was armed
; in
; rcx = 184 byte buffer that receives a copy of the frame, or 0 to discard
; out
; eax = 0 with a result ready, EFAULT when the armed trap never fired
;       (the arming is cleared), ENOTREADY with neither armed nor ready
; rdx = address of the handler frame that produced the result, else 0
;
; The copy is the whole 184 byte frame the stub built, taken before the stub
; returned, so the caller sees the hardware state at the moment of the trap
; and not whatever the resuming context left on the stack. The result is
; consumed: a second call without a new arming is ENOTREADY.
; ------------------------------------------------------------------

cpu_trap_finish:
        push    rbp
        mov     rbp, rsp
        sub     rsp, CPU_STACK_FRAME
        mov     [rbp-16], rsi
        mov     [rbp-24], rdi
        cmp     byte [cpu_trap_result_ready], 0
        je      .no_result
        test    rcx, rcx
        jz      .consumed
        mov     [rbp-8], rcx
        lea     rsi, [cpu_trap_frame_result]
        mov     rdi, [rbp-8]
        mov     ecx, CPU_FRAME_SIZE / 8
        cld
        rep movsq
.consumed:
        mov     byte [cpu_trap_result_ready], 0
        mov     rdx, [cpu_trap_handler_frame]
        xor     eax, eax
        jmp     .done
.no_result:
        cmp     byte [cpu_trap_armed], 0
        je      .not_ready
        ; the trap never fired: report it and disarm, so the next arming works
        mov     byte [cpu_trap_armed], 0
        mov     eax, CPU_EFAULT
        xor     edx, edx
        jmp     .done
.not_ready:
        mov     eax, CPU_ENOTREADY
        xor     edx, edx
.done:
        mov     rsi, [rbp-16]
        mov     rdi, [rbp-24]
        leave
        ret

; ------------------------------------------------------------------
; common trap entry, called by every stub with rcx = frame
; in
; rcx = CPU_TRAP_FRAME of the stub that fired
; out
; never returns for an unexpected trap; for an expected one the frame is
; copied, rewritten and the stub returns through iretq
;
; An expected trap has to match on four independent facts: the vector, the
; saved RIP and the two masked hardware values. Anything else is a bug in the
; kernel or in the expectation, and either way it is not something this stage
; can recover from, so it panics with the frame address.
; ------------------------------------------------------------------

cpu_trap_dispatch:
        push    rbp
        mov     rbp, rsp
        sub     rsp, CPU_STACK_FRAME
        mov     [rbp-8], rcx
        ; this stage only runs in ring 0, any other CS is a broken frame
        mov     rax, [rcx + CPU_FRAME_CS]
        test    al, 3
        jnz     .not_kernel
        cmp     byte [cpu_trap_armed], 0
        je      .unexpected
        ; the vector must be the one that was armed
        mov     rdx, [rcx + CPU_FRAME_VECTOR]
        cmp     rdx, CPU_IDT_VECTORS
        jae     .unexpected
        movzx   eax, byte [cpu_trap_vector]
        cmp     rax, rdx
        jne     .unexpected
        ; and it must have been taken at the expected instruction
        mov     rax, [rcx + CPU_FRAME_RIP]
        cmp     rax, [cpu_trap_expect_rip]
        jne     .unexpected
        ; (error & mask) == value
        mov     rax, [rcx + CPU_FRAME_ERROR]
        mov     rdx, [cpu_trap_err_mask]
        and     rax, rdx
        cmp     rax, [cpu_trap_err_value]
        jne     .unexpected
        ; (cr2 & mask) == value
        mov     rax, [rcx]
        mov     rdx, [cpu_trap_cr2_mask]
        and     rax, rdx
        cmp     rax, [cpu_trap_cr2_value]
        jne     .unexpected
        ; accepted: keep the handler frame address and a private copy of the
        ; whole frame before the stub restores anything
        mov     [cpu_trap_handler_frame], rcx
        mov     [rbp-16], rsi
        mov     [rbp-24], rdi
        mov     rsi, rcx
        lea     rdi, [cpu_trap_frame_result]
        mov     ecx, CPU_FRAME_SIZE / 8
        cld
        rep movsq
        mov     rsi, [rbp-16]
        mov     rdi, [rbp-24]
        ; the only field the returning frame may change is RIP
        mov     rax, [cpu_trap_resume_rip]
        mov     rcx, [rbp-8]
        mov     [rcx + CPU_FRAME_RIP], rax
        mov     byte [cpu_trap_armed], 0
        mov     byte [cpu_trap_result_ready], 1
        xor     eax, eax
        leave
        ret
.unexpected:
        mov     rcx, msg_trap_unexpected
        mov     rdx, [rbp-8]
        mov     r8, CPU_EIO
        call    cpu_panic
        jmp     cpu_halt_forever
.not_kernel:
        mov     rcx, msg_trap_not_kernel
        mov     rdx, [rbp-8]
        mov     r8, CPU_EPERM
        call    cpu_panic
        jmp     cpu_halt_forever
