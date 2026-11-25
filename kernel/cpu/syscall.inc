;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; SYSCALL/SYSRET: the MSR setup, the handler registrations and the thin
;; ring 3 entry, contract APP-v1.
;;
;; The entry does not go through the IDT. SYSCALL lands here with IF
;; cleared by SFMASK, the return RIP in RCX and the return RFLAGS in R11.
;; The user stack is untrusted, so the very first act is a switch to
;; TSS.RSP0; the old user RSP parks in a private scratch slot because one
;; CPU runs with IF = 0 for the whole entry and a syscall can never nest.
;;
;; No swapgs anywhere: this kernel never programs the GS base and never
;; uses GS relative addressing, so there is no per CPU state to swap.
;;
;; The entry builds the same 184 byte frame the trap stubs build, with
;; VECTOR = CPU_SYS_VECTOR (0x100), a sentinel past every IDT vector, and
;; ERROR = 0. The handler table is indexed by the syscall number in RAX;
;; a handler takes RCX = frame under the MS x64 ABI and returns the result
;; in RAX, which the entry writes back into frame.RAX. An unknown or
;; unregistered number returns CPU_ENOTSUP without calling anything.
;;
;; The return path restores every register from the frame, loads RCX and
;; R11 from frame.RIP and frame.RFLAGS and executes SYSRETQ: the CPU
;; synthesises CS = STAR[63:48] + 16 | 3 = 0x33 and SS = STAR[63:48] + 8
;; | 3 = 0x2B (SDM volume 2, SYSRET), exactly the ring 3 selectors of the
;; GDT. SYSRET does not restore RSP, so the frame's user RSP is popped
;; into RSP just before it. Nothing in this file ever sets IF.
;;
;; Selector note: the ring 3 pair is 0x30 code / 0x28 data (loaded as
;; 0x33 / 0x2B), see the APP-v1 layout note in api.inc.
;;

; ------------------------------------------------------------------
; program the four MSRs of the SYSCALL/SYSRET pair
; in
; out
; eax = 0 on success, EPERM when not ring 0 or IF is set, EALREADY when
;       the MSRs are already programmed, EIO when a read back mismatches
; rdx = 0
;
; EFER is read modify written so NXE and every other bit survive; the
; read back compares the full 64 bit value, which proves both SCE and the
; preservation. STAR, LSTAR and SFMASK are written whole and read back.
; ------------------------------------------------------------------

cpu_syscall_init:
        push    rbp
        mov     rbp, rsp
        sub     rsp, CPU_STACK_FRAME
        ; this stage only runs in ring 0
        mov     ax, cs
        test    al, 3
        jnz     .eperm
        ; and only with interrupts masked, like every other setup call
        pushfq
        pop     rax
        test    eax, CPU_RFLAGS_IF
        jnz     .eperm
        cmp     byte [cpu_syscall_ready], 0
        jne     .ealready
        ; EFER: set SCE, preserve everything else, verify the whole value
        mov     ecx, CPU_EFER_MSR
        rdmsr
        shl     rdx, 32
        or      rax, rdx
        or      rax, CPU_EFER_SCE
        mov     [rbp-8], rax
        mov     rdx, rax
        shr     rdx, 32
        mov     ecx, CPU_EFER_MSR
        wrmsr
        mov     ecx, CPU_EFER_MSR
        rdmsr
        shl     rdx, 32
        or      rax, rdx
        cmp     rax, [rbp-8]
        jne     .eio
        ; STAR: the selector pair derived in api.inc
        mov     ecx, CPU_STAR_MSR
        mov     eax, CPU_STAR_VALUE and 0xFFFFFFFF
        mov     edx, (CPU_STAR_VALUE shr 32) and 0xFFFFFFFF
        wrmsr
        mov     ecx, CPU_STAR_MSR
        rdmsr
        shl     rdx, 32
        or      rax, rdx
        mov     rcx, CPU_STAR_VALUE
        cmp     rax, rcx
        jne     .eio
        ; LSTAR: the entry below, at its linked VA
        lea     rax, [cpu_syscall_entry]
        mov     [rbp-16], rax
        mov     rdx, rax
        shr     rdx, 32
        mov     ecx, CPU_LSTAR_MSR
        wrmsr
        mov     ecx, CPU_LSTAR_MSR
        rdmsr
        shl     rdx, 32
        or      rax, rdx
        cmp     rax, [rbp-16]
        jne     .eio
        ; SFMASK: SYSCALL clears exactly IF
        mov     ecx, CPU_SFMASK_MSR
        mov     eax, CPU_SFMASK_VALUE
        xor     edx, edx
        wrmsr
        mov     ecx, CPU_SFMASK_MSR
        rdmsr
        shl     rdx, 32
        or      rax, rdx
        cmp     rax, CPU_SFMASK_VALUE
        jne     .eio
        mov     byte [cpu_syscall_ready], 1
        xor     eax, eax
        xor     edx, edx
        leave
        ret
.eperm:
        mov     eax, CPU_EPERM
        jmp     .fail
.ealready:
        mov     eax, CPU_EALREADY
        jmp     .fail
.eio:
        mov     eax, CPU_EIO
.fail:
        xor     edx, edx
        leave
        ret

; ------------------------------------------------------------------
; register, or clear, the handler of one syscall number
; in
; rcx = syscall number, rdx = handler VA in the kernel half, or 0
; out
; eax = 0 on success, EPERM when not ring 0 or IF is set, EINVAL for a
;       number past the table or a non canonical handler, EALREADY when
;       the slot already holds a non zero handler
; rdx = 0
;
; The handler takes RCX = the 184 byte frame under the MS x64 ABI and
; returns the syscall result in RAX. The entry reads the slot once per
; syscall, so the swap needs no lock beyond IF = 0.
; ------------------------------------------------------------------

cpu_syscall_set_handler:
        push    rbp
        mov     rbp, rsp
        sub     rsp, CPU_STACK_FRAME
        ; this stage only runs in ring 0
        mov     ax, cs
        test    al, 3
        jnz     .eperm
        ; and only with interrupts masked, like every other setup call
        pushfq
        pop     rax
        test    eax, CPU_RFLAGS_IF
        jnz     .eperm
        cmp     rcx, CPU_SYS_CALL_MAX
        jae     .einval
        mov     [rbp-8], rcx
        test    rdx, rdx
        jz      .clear
        mov     rax, rdx
        shr     rax, CPU_KERNEL_SHIFT
        cmp     rax, CPU_KERNEL_TOP
        jne     .einval
        ; one handler per number, clear first to replace it
        mov     rcx, [rbp-8]
        cmp     qword [cpu_syscall_handlers + rcx*8], 0
        jne     .ealready
        mov     [cpu_syscall_handlers + rcx*8], rdx
        jmp     .ok
.clear:
        mov     rcx, [rbp-8]
        mov     qword [cpu_syscall_handlers + rcx*8], 0
.ok:
        xor     eax, eax
        xor     edx, edx
        leave
        ret
.eperm:
        mov     eax, CPU_EPERM
        jmp     .fail
.einval:
        mov     eax, CPU_EINVAL
        jmp     .fail
.ealready:
        mov     eax, CPU_EALREADY
.fail:
        xor     edx, edx
        leave
        ret

; ------------------------------------------------------------------
; register, or unregister, the user fault handler of the dispatcher
; in
; rcx = handler VA in the kernel half, or 0 to unregister
; out
; eax = 0 on success, EPERM when not ring 0 or IF is set, EINVAL for a non
;       canonical handler, EALREADY when a non zero handler is registered
; rdx = 0
;
; Same single slot rules as cpu_irq_set_handler. A trap whose frame CS
; has RPL 3 goes to this handler with RCX = frame; the stub restores and
; iretqs afterwards. Without a registered handler such a trap still
; panics, exactly like before APP-v1.
; ------------------------------------------------------------------

cpu_user_fault_set_handler:
        push    rbp
        mov     rbp, rsp
        sub     rsp, CPU_STACK_FRAME
        ; this stage only runs in ring 0
        mov     ax, cs
        test    al, 3
        jnz     .eperm
        ; and only with interrupts masked, like every other setup call
        pushfq
        pop     rax
        test    eax, CPU_RFLAGS_IF
        jnz     .eperm
        test    rcx, rcx
        jz      .clear
        mov     rax, rcx
        shr     rax, CPU_KERNEL_SHIFT
        cmp     rax, CPU_KERNEL_TOP
        jne     .einval
        ; one handler at a time, unregister first to replace it
        cmp     qword [cpu_user_fault_handler], 0
        jne     .ealready
        mov     [cpu_user_fault_handler], rcx
        jmp     .ok
.clear:
        mov     qword [cpu_user_fault_handler], 0
.ok:
        xor     eax, eax
        xor     edx, edx
        leave
        ret
.eperm:
        mov     eax, CPU_EPERM
        jmp     .fail
.einval:
        mov     eax, CPU_EINVAL
        jmp     .fail
.ealready:
        mov     eax, CPU_EALREADY
.fail:
        xor     edx, edx
        leave
        ret

; ------------------------------------------------------------------
; the LSTAR target: SYSCALL from ring 3, never called
; in
; rax = syscall number, rdi/rsi/rdx/r10/r8/r9 = arguments,
; rcx = return RIP, r11 = return RFLAGS, rsp = the untrusted user stack
; out
; returns with SYSRETQ, rax = the handler result or CPU_ENOTSUP
;
; The frame mirrors the stub layout of idt_traps.inc exactly, so a
; handler cannot tell a syscall frame from a trap frame except by the
; VECTOR sentinel. The call protocol is the stub's: RBP = frame base,
; RSP aligned down to 16 with 32 bytes of shadow space below the frame.
; ------------------------------------------------------------------

cpu_syscall_entry:
        ; the user stack is untrusted: park it and switch to TSS.RSP0
        mov     [cpu_syscall_scratch_rsp], rsp
        mov     rsp, [cpu_tss_rsp0]
        ; the hardware part of the frame, highest address first
        push    CPU_SYSRET_SS
        push    qword [cpu_syscall_scratch_rsp]
        push    r11
        push    CPU_SYSRET_CS
        push    rcx
        push    qword 0
        push    qword CPU_SYS_VECTOR
        ; the 15 general purpose registers, then CR2, exactly like a stub
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
        ; dispatch by number: unknown or unregistered returns ENOTSUP
        mov     rax, [rbp + CPU_FRAME_RAX]
        cmp     rax, CPU_SYS_CALL_MAX
        jae     .unknown
        mov     rax, [cpu_syscall_handlers + rax*8]
        test    rax, rax
        jz      .unknown
        mov     rcx, rbp
        call    rax
        mov     [rbp + CPU_FRAME_RAX], rax
        jmp     .return_user
.unknown:
        mov     qword [rbp + CPU_FRAME_RAX], CPU_ENOTSUP
.return_user:
        ; restore the frame; RCX and R11 come from RIP and RFLAGS, the
        ; two facts SYSRET needs, and RSP is popped back to the user stack
        mov     rsp, rbp
        add     rsp, 8
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
        pop     rcx
        add     rsp, 8
        pop     r11
        pop     rsp
        sysretq
