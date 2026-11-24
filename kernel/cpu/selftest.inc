;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; cpu_selftest: prove that the tables the CPU is using are the tables this
;; module built, without trusting the data it just wrote.
;;
;; The static part reads GDTR, IDTR and TR back from the CPU (SGDT, SIDT,
;; STR) and compares them with the module labels, then walks GDT, TSS and all
;; 256 IDT gates. The dynamic part arms real traps and checks that the frame
;; arrives, that it arrived where the CPU really saved the RIP, and that the
;; resumed frame is the one that was asked for. Only the six vectors this stage
;; can resume from are used:
;;
;;   #DE 0, #BP 3, #UD 6, #PF 14, and the software vectors 0xF0 and 0xF1.
;;   0xF1 is deliberately the one gate that carries IST4.
;;
;; Every arming names two addresses and they are not the same thing: the RIP
;; the hardware saves on entry (the faulting instruction for #DE, #UD and #PF,
;; the next instruction for int3 and int n), and the RIP the stub resumes at.
;; Passing the resume address where the saved address belongs is a real bug
;; that only shows up as an unexpected trap panic at run time, so the two are
;; kept apart here on purpose.
;;
;; The trap state machine is exercised as well: EFAULT for an armed trap that
;; never fired (and the arming is cleared), EBUSY for a second arming, and
;; ENOTREADY for a result that was already consumed.
;;
;; #PF has no address that is unmapped by construction in this stage, because
;; the loader keeps a low identity map alive. The caller may pass one in RCX;
;; with RCX = 0 the #PF leg is skipped and reported as skipped, never as
;; passed.
;;
;; Preconditions: cpu_init succeeded, IF is clear (traps are taken one by one
;; and an interrupt in between would be reported as unexpected), and the CPU
;; is running at the high half so that a resume RIP can be canonical.
;;
;; Every resume point is a plain global label: a local label would be captured
;; by the resume point that precedes it, and the vector is resumed into the
;; middle of this function on purpose. A faulting instruction gets a label of
;; its own, immediately in front of it, so the expected saved RIP is the
;; address the CPU really pushes.
;;
;; RDI is callee saved in the Microsoft x64 ABI and the #F1 leg poisons its
;; buffer with rep stosq, so RDI is saved in the frame on entry and restored
;; before every single return. Leaving one path out would corrupt a caller
;; that keeps a string destination across the call, which is exactly the kind
;; of bug that only shows up much later.
;;
;; The TSS descriptor has two states and this file must not confuse them. The
;; image holds the available type 0x89; LTR sets the busy bit and the running
;; system then shows 0x8B. The static check accepts either (the image is still
;; the built one), the TR check demands 0x8B, because by the time this runs
;; the CPU has loaded the TSS or the descriptor would not be in use at all.
;;

; ------------------------------------------------------------------
; run the self test
; in
; rcx = address that is known to be unmapped, or 0 to skip the #PF leg
; out
; eax = 0 when every mandatory leg passed, EPERM when IF is set,
;       ENOTREADY before cpu_init, EIO otherwise
; rdx = bitmask of the legs that passed
; r8  = bitmask of the legs that were skipped
;
; The trap state leg (CPU_SELFTEST_BIT_STATE) reports ENOTREADY, EBUSY and
; EFAULT; it is not in CPU_SELFTEST_MANDATORY, the six vector legs decide the
; result.
; ------------------------------------------------------------------

cpu_selftest:
        push    rbp
        mov     rbp, rsp
        sub     rsp, CPU_STACK_FRAME
        mov     [rbp-32], rdi               ; callee saved, rep stosq clobbers it
        mov     [rbp-8], rcx                ; #PF probe address
        mov     qword [rbp-16], 0           ; passed
        mov     qword [rbp-24], 0           ; skipped
        ; traps are taken one by one, IF must be clear
        pushfq
        pop     rax
        test    eax, CPU_RFLAGS_IF
        jnz     cpu_selftest_precondition
        cmp     byte [cpu_state], 0
        je      cpu_selftest_not_ready
        ; the console is optional, a dead UART must not hide the result
        call    cpu_com1_init
        ; ---------------- GDTR, IDTR, GDT and the TSS descriptor ----------
        sgdt    [rbp-48]
        sidt    [rbp-64]
        cmp     word [rbp-48], CPU_GDTR_LIMIT
        jne     cpu_selftest_gdt_bad
        mov     rax, [rbp-46]
        cmp     rax, cpu_gdt
        jne     cpu_selftest_gdt_bad
        cmp     word [rbp-64], CPU_IDT_LIMIT
        jne     cpu_selftest_gdt_bad
        mov     rax, [rbp-62]
        cmp     rax, cpu_idt
        jne     cpu_selftest_gdt_bad
        mov     rax, [cpu_gdt + CPU_GDT_OFF_NULL]
        test    rax, rax
        jnz     cpu_selftest_gdt_bad
        ; loading a segment makes the CPU write the accessed bit (qword
        ; bit 40) into the descriptor in memory: retfq for CS, mov for
        ; DS/ES/SS. Mask it off before comparing, the image keeps the
        ; built value with the bit clear.
        mov     rax, [cpu_gdt + CPU_GDT_OFF_CODE]
        mov     rdx, CPU_GDT_DESC_RUNTIME_MASK
        and     rax, rdx                    ; CS was reloaded through retfq
        mov     rdx, CPU_GDT_CODE_VALUE
        cmp     rax, rdx
        jne     cpu_selftest_gdt_bad
        mov     rax, [cpu_gdt + CPU_GDT_OFF_DATA]
        mov     rdx, CPU_GDT_DESC_RUNTIME_MASK
        and     rax, rdx                    ; DS/ES/SS were loaded with mov
        mov     rdx, CPU_GDT_DATA_VALUE
        cmp     rax, rdx
        jne     cpu_selftest_gdt_bad
        movzx   eax, word [cpu_gdt_tss + 0]
        cmp     rax, CPU_TSS_LIMIT
        jne     cpu_selftest_gdt_bad
        ; the image is the image, whether or not LTR has run: accept the
        ; available type it was built with and the busy type the CPU writes
        movzx   eax, byte [cpu_gdt_tss + 5]
        cmp     rax, CPU_TSS_DESC_TYPE
        je      cpu_selftest_tss_type_ok
        cmp     rax, CPU_TSS_DESC_TYPE_BUSY
        jne     cpu_selftest_gdt_bad
cpu_selftest_tss_type_ok:
        movzx   eax, byte [cpu_gdt_tss + 6]
        test    eax, eax
        jnz     cpu_selftest_gdt_bad
        movzx   eax, word [cpu_gdt_tss + 2]
        movzx   edx, byte [cpu_gdt_tss + 4]
        shl     rdx, 16
        or      rax, rdx
        movzx   edx, byte [cpu_gdt_tss + 7]
        shl     rdx, 24
        or      rax, rdx
        mov     edx, dword [cpu_gdt_tss + 8]
        shl     rdx, 32
        or      rax, rdx
        cmp     rax, cpu_tss
        jne     cpu_selftest_gdt_bad
        or      qword [rbp-16], 1 shl CPU_SELFTEST_BIT_GDT
cpu_selftest_gdt_bad:
        ; ---------------- TSS fields and the IST stacks ------------------
        mov     rax, [cpu_tss_rsp0]
        test    rax, rax
        jz      cpu_selftest_tss_bad
        test    rax, CPU_STACK_ALIGN - 1
        jnz     cpu_selftest_tss_bad
        movzx   eax, word [cpu_tss_iomap]
        cmp     rax, CPU_TSS_IOMAP_NONE
        jne     cpu_selftest_tss_bad
        mov     rax, [cpu_tss_ist1]
        cmp     rax, cpu_ist1_top
        jne     cpu_selftest_tss_bad
        mov     rax, [cpu_tss_ist2]
        cmp     rax, cpu_ist2_top
        jne     cpu_selftest_tss_bad
        mov     rax, [cpu_tss_ist3]
        cmp     rax, cpu_ist3_top
        jne     cpu_selftest_tss_bad
        mov     rax, [cpu_tss_ist4]
        cmp     rax, cpu_ist4_top
        jne     cpu_selftest_tss_bad
        mov     rax, [cpu_tss_ist1]
        or      rax, [cpu_tss_ist2]
        or      rax, [cpu_tss_ist3]
        or      rax, [cpu_tss_ist4]
        test    rax, CPU_PAGE_SIZE - 1
        jnz     cpu_selftest_tss_bad
        mov     rax, [cpu_tss_ist5]
        or      rax, [cpu_tss_ist6]
        or      rax, [cpu_tss_ist7]
        jnz     cpu_selftest_tss_bad
        or      qword [rbp-16], 1 shl CPU_SELFTEST_BIT_TSS
cpu_selftest_tss_bad:
        ; ---------------- all 256 IDT gates -----------------------------
        xor     r8, r8
cpu_selftest_idt_loop:
        mov     rax, r8
        shl     rax, 4
        lea     rdx, [cpu_idt + rax]
        movzx   ecx, word [rdx + 2]
        cmp     ecx, CPU_SEL_CODE
        jne     cpu_selftest_idt_bad
        movzx   ecx, byte [rdx + 5]
        cmp     ecx, CPU_IDT_TYPE_ATTR
        jne     cpu_selftest_idt_bad
        movzx   ecx, byte [rdx + 4]
        and     ecx, 7
        cmp     ecx, CPU_IDT_IST_MAX
        ja      cpu_selftest_idt_bad
        movzx   ecx, word [rdx + 0]
        movzx   eax, word [rdx + 6]
        shl     rax, 16
        or      rcx, rax
        mov     eax, dword [rdx + 8]
        shl     rax, 32
        or      rcx, rax
        cmp     rcx, cpu_trap_stubs_start
        jb      cpu_selftest_idt_bad
        cmp     rcx, cpu_trap_stubs_end
        jae     cpu_selftest_idt_bad
        inc     r8
        cmp     r8, CPU_IDT_VECTORS
        jb      cpu_selftest_idt_loop
        or      qword [rbp-16], 1 shl CPU_SELFTEST_BIT_IDT
cpu_selftest_idt_bad:
        ; ---------------- TR -------------------------------------------
        xor     eax, eax
        str     ax
        cmp     ax, CPU_SEL_TSS
        jne     cpu_selftest_tr_bad
        ; with TR loaded the CPU must have marked the descriptor busy; a
        ; still available 0x89 here means LTR did not really happen
        movzx   eax, byte [cpu_gdt_tss + 5]
        cmp     rax, CPU_TSS_DESC_TYPE_BUSY
        jne     cpu_selftest_tr_bad
        or      qword [rbp-16], 1 shl CPU_SELFTEST_BIT_TR
cpu_selftest_tr_bad:
        ; ---------------- trap state machine ----------------------------
        ; nothing armed and nothing pending: finish has nothing to report
        xor     ecx, ecx
        call    cpu_trap_finish
        cmp     eax, CPU_ENOTREADY
        jne     cpu_selftest_state_bad
        ; arm #UD, then arm it again: one armed trap at a time
        mov     rcx, CPU_VEC_UD
        mov     rdx, cpu_selftest_ud_fault
        mov     r8, cpu_selftest_ud_resume
        xor     r9d, r9d
        call    cpu_trap_expect
        test    eax, eax
        jnz     cpu_selftest_state_bad
        mov     rcx, CPU_VEC_UD
        mov     rdx, cpu_selftest_ud_fault
        mov     r8, cpu_selftest_ud_resume
        xor     r9d, r9d
        call    cpu_trap_expect
        cmp     eax, CPU_EBUSY
        jne     cpu_selftest_state_bad
        ; a trap that never fired: EFAULT, and the arming is gone with it
        xor     ecx, ecx
        call    cpu_trap_finish
        cmp     eax, CPU_EFAULT
        jne     cpu_selftest_state_bad
        xor     ecx, ecx
        call    cpu_trap_finish
        cmp     eax, CPU_ENOTREADY
        jne     cpu_selftest_state_bad
        or      qword [rbp-16], 1 shl CPU_SELFTEST_BIT_STATE
cpu_selftest_state_bad:
        ; ---------------- vector 0xF0, plain software trap --------------
        mov     rcx, CPU_VEC_TEST
        mov     rdx, cpu_selftest_f0_resume      ; int n saves the next RIP
        mov     r8, cpu_selftest_f0_resume
        xor     r9d, r9d
        call    cpu_trap_expect
        test    eax, eax
        jnz     cpu_selftest_f0_bad
        int     CPU_VEC_TEST
cpu_selftest_f0_resume:
        xor     ecx, ecx                         ; drop the result
        call    cpu_trap_finish
        test    eax, eax
        jnz     cpu_selftest_f0_bad
        test    rdx, rdx                         ; the handler frame address
        jz      cpu_selftest_f0_bad
        xor     ecx, ecx                         ; a result is consumed once
        call    cpu_trap_finish
        cmp     eax, CPU_ENOTREADY
        jne     cpu_selftest_f0_bad
        or      qword [rbp-16], 1 shl CPU_SELFTEST_BIT_VEC_F0
cpu_selftest_f0_bad:
        ; ---------------- vector 0xF1, the IST4 gate --------------------
        ; IST always pushes SS and RSP, so this is the one frame whose last
        ; qword is real: poisoning the buffer first makes the copy visible
        lea     rdi, [cpu_selftest_out]
        mov     eax, 0xAAAAAAAA
        mov     ecx, CPU_FRAME_SIZE / 8
        cld
        rep stosq
        mov     rcx, CPU_VEC_IST_TEST
        mov     rdx, cpu_selftest_f1_resume
        mov     r8, cpu_selftest_f1_resume
        xor     r9d, r9d
        call    cpu_trap_expect
        test    eax, eax
        jnz     cpu_selftest_f1_bad
        int     CPU_VEC_IST_TEST
cpu_selftest_f1_resume:
        lea     rcx, [cpu_selftest_out]
        call    cpu_trap_finish
        test    eax, eax
        jnz     cpu_selftest_f1_bad
        ; the reported address is the frame on the IST stack, not our copy
        test    rdx, rdx
        jz      cpu_selftest_f1_bad
        mov     rax, cpu_ist4_test
        cmp     rdx, rax
        jb      cpu_selftest_f1_bad
        mov     rax, cpu_ist4_top
        cmp     rdx, rax
        jae     cpu_selftest_f1_bad
        ; and the copy holds the frame the stub really built
        mov     rax, [cpu_selftest_out + CPU_FRAME_VECTOR]
        cmp     rax, CPU_VEC_IST_TEST
        jne     cpu_selftest_f1_bad
        mov     rax, [cpu_selftest_out + CPU_FRAME_ERROR]
        test    rax, rax
        jnz     cpu_selftest_f1_bad
        mov     rax, [cpu_selftest_out + CPU_FRAME_RIP]
        cmp     rax, cpu_selftest_f1_resume
        jne     cpu_selftest_f1_bad
        mov     rax, [cpu_selftest_out + CPU_FRAME_CS]
        cmp     rax, CPU_SEL_CODE
        jne     cpu_selftest_f1_bad
        mov     rax, [cpu_selftest_out + CPU_FRAME_RSP]
        test    rax, rax
        jz      cpu_selftest_f1_bad
        mov     rax, [cpu_selftest_out + CPU_FRAME_SS]
        cmp     rax, CPU_SEL_DATA
        jne     cpu_selftest_f1_bad
        or      qword [rbp-16], 1 shl CPU_SELFTEST_BIT_VEC_F1
cpu_selftest_f1_bad:
        ; ---------------- vector 3, breakpoint --------------------------
        mov     rcx, CPU_VEC_BP
        mov     rdx, cpu_selftest_bp_resume      ; int3 saves the next RIP
        mov     r8, cpu_selftest_bp_resume
        xor     r9d, r9d
        call    cpu_trap_expect
        test    eax, eax
        jnz     cpu_selftest_bp_bad
        int3
cpu_selftest_bp_resume:
        xor     ecx, ecx
        call    cpu_trap_finish
        test    eax, eax
        jnz     cpu_selftest_bp_bad
        or      qword [rbp-16], 1 shl CPU_SELFTEST_BIT_VEC_BP
cpu_selftest_bp_bad:
        ; ---------------- vector 0, divide error, masked expect ----------
        mov     rcx, CPU_VEC_DE
        mov     rdx, cpu_selftest_de_fault       ; #DE saves the div itself
        mov     r8, cpu_selftest_de_resume
        lea     r9, [cpu_selftest_expect]
        call    cpu_trap_expect
        test    eax, eax
        jnz     cpu_selftest_de_bad
        xor     eax, eax
        mov     edx, 1
        xor     ecx, ecx
cpu_selftest_de_fault:
        div     ecx
cpu_selftest_de_resume:
        lea     rcx, [cpu_selftest_out]
        call    cpu_trap_finish
        test    eax, eax
        jnz     cpu_selftest_de_bad
        test    rdx, rdx
        jz      cpu_selftest_de_bad
        mov     rax, [cpu_selftest_out + CPU_FRAME_VECTOR]
        test    rax, rax
        jnz     cpu_selftest_de_bad
        mov     rax, [cpu_selftest_out + CPU_FRAME_ERROR]
        test    rax, rax
        jnz     cpu_selftest_de_bad
        mov     rax, [cpu_selftest_out + CPU_FRAME_RIP]
        cmp     rax, cpu_selftest_de_fault
        jne     cpu_selftest_de_bad
        mov     rax, [cpu_selftest_out + CPU_FRAME_CS]
        cmp     rax, CPU_SEL_CODE
        jne     cpu_selftest_de_bad
        or      qword [rbp-16], 1 shl CPU_SELFTEST_BIT_VEC_DE
cpu_selftest_de_bad:
        ; ---------------- vector 6, invalid opcode ----------------------
        mov     rcx, CPU_VEC_UD
        mov     rdx, cpu_selftest_ud_fault       ; #UD saves the ud2 itself
        mov     r8, cpu_selftest_ud_resume
        xor     r9d, r9d
        call    cpu_trap_expect
        test    eax, eax
        jnz     cpu_selftest_ud_bad
cpu_selftest_ud_fault:
        ud2
cpu_selftest_ud_resume:
        xor     ecx, ecx
        call    cpu_trap_finish
        test    eax, eax
        jnz     cpu_selftest_ud_bad
        or      qword [rbp-16], 1 shl CPU_SELFTEST_BIT_VEC_UD
cpu_selftest_ud_bad:
        ; ---------------- vector 14, optional --------------------------
        mov     rax, [rbp-8]
        test    rax, rax
        jz      cpu_selftest_pf_skip
        mov     rcx, 14
        mov     rdx, cpu_selftest_pf_fault       ; #PF saves the load itself
        mov     r8, cpu_selftest_pf_resume
        xor     r9d, r9d
        call    cpu_trap_expect
        test    eax, eax
        jnz     cpu_selftest_pf_bad
        mov     rax, [rbp-8]
cpu_selftest_pf_fault:
        mov     rax, [rax]
cpu_selftest_pf_resume:
        lea     rcx, [cpu_selftest_out]
        call    cpu_trap_finish
        test    eax, eax
        jnz     cpu_selftest_pf_bad
        mov     rax, [cpu_selftest_out + CPU_FRAME_VECTOR]
        cmp     rax, 14
        jne     cpu_selftest_pf_bad
        ; CR2 in the copied frame is the address that faulted
        mov     rax, [cpu_selftest_out + CPU_FRAME_CR2]
        cmp     rax, [rbp-8]
        jne     cpu_selftest_pf_bad
        or      qword [rbp-16], 1 shl CPU_SELFTEST_BIT_VEC_PF
        jmp     cpu_selftest_report
cpu_selftest_pf_skip:
        or      qword [rbp-24], 1 shl CPU_SELFTEST_BIT_VEC_PF
cpu_selftest_pf_bad:
        ; ---------------- report ---------------------------------------
cpu_selftest_report:
        mov     rcx, msg_selftest_head
        mov     rdx, CPU_PUTS_MAX
        call    cpu_com1_puts
        mov     rcx, [rbp-16]
        mov     rdx, CPU_PUTHEX_DIGITS
        call    cpu_com1_puthex
        mov     rcx, msg_selftest_skipped
        mov     rdx, CPU_PUTS_MAX
        call    cpu_com1_puts
        mov     rcx, [rbp-24]
        mov     rdx, CPU_PUTHEX_DIGITS
        call    cpu_com1_puthex
        mov     rcx, msg_newline
        mov     rdx, CPU_PUTS_MAX
        call    cpu_com1_puts
        mov     rax, [rbp-16]
        mov     rdx, CPU_SELFTEST_MANDATORY
        and     rax, rdx
        cmp     rax, rdx
        jne     cpu_selftest_failed
        xor     eax, eax
        mov     rdx, [rbp-16]
        mov     r8, [rbp-24]
        mov     rdi, [rbp-32]
        leave
        ret
cpu_selftest_failed:
        mov     eax, CPU_EIO
        mov     rdx, [rbp-16]
        mov     r8, [rbp-24]
        mov     rdi, [rbp-32]
        leave
        ret
cpu_selftest_not_ready:
        mov     eax, CPU_ENOTREADY
        xor     edx, edx
        xor     r8d, r8d
        mov     rdi, [rbp-32]
        leave
        ret
cpu_selftest_precondition:
        mov     eax, CPU_EPERM
        xor     edx, edx
        xor     r8d, r8d
        mov     rdi, [rbp-32]
        leave
        ret
