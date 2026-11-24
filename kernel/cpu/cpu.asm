;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; CPU descriptors, exceptions and panic: the module aggregator.
;;
;; Contents, in this order:
;;   api.inc         constants, packed structure assertions, vector helper
;;   gdt_tss.inc     cpu_gdt_init, cpu_set_rsp0
;;   idt_traps.inc   256 stubs, cpu_trap_expect, cpu_trap_finish, dispatch,
;;                   cpu_irq_set_handler
;;   panic.inc       bounded COM1 output, cpu_panic, halt
;;   selftest.inc    cpu_selftest
;;   this file       cpu_init
;;   data.inc        stacks, TSS, GDT, GDTR, IDT, IDTR, state, messages
;;
;; The includer has to set use64 and org before this file; the module itself
;; does not touch either, so it links at any base. The data region must stay
;; last: data.inc asserts that nothing follows it, and this file re-checks it
;; once more after the include.
;;
;; Include it exactly once per image, it defines code and data, not only
;; macros.
;;

; ------------------------------------------------------------------
; the code region, for the layout test of task 27
; ------------------------------------------------------------------

cpu_code_start:

include 'api.inc'
include 'gdt_tss.inc'
include 'idt_traps.inc'
include 'panic.inc'
include 'selftest.inc'

; ------------------------------------------------------------------
; bring the CPU into a state where this module owns every vector
; in
; rcx = physical address of a CPU_INIT_PARAMS, readable at its VA
; out
; eax = 0 on success
;       EPERM     not ring 0, or IF is set
;       ENOTSUP   not long mode, no PAE, paging off, or CET enabled
;       EINVAL    a field of CPU_INIT_PARAMS is not what the contract says
;       EALREADY  cpu_init already succeeded once
;       EIO       a descriptor could not be built (never observed)
; rdx = 0
;
; The window between LGDT and LIDT produces no console output at all: the
; console is not needed there, and calling into the printer with a half
; switched table is exactly how a bring-up kernel loses its evidence.
; ------------------------------------------------------------------

cpu_init:
        push    rbp
        mov     rbp, rsp
        sub     rsp, CPU_STACK_FRAME
        mov     [rbp-8], rcx
        cld
        ; ---------------- environment ----------------
        mov     ax, cs
        test    al, 3
        jnz     .eperm
        pushfq
        pop     rax
        test    eax, CPU_RFLAGS_IF
        jnz     .eperm
        mov     ecx, CPU_EFER_MSR
        rdmsr
        test    eax, CPU_EFER_LMA
        jz      .enotsup
        mov     rax, cr4
        test    eax, CPU_CR4_PAE
        jz      .enotsup
        test    eax, CPU_CR4_CET
        jnz     .enotsup
        mov     rax, cr0
        test    eax, CPU_CR0_PG
        jz      .enotsup
        ; ---------------- the handover parameters ----------------
        mov     rcx, [rbp-8]
        test    rcx, rcx
        jz      .einval
        test    rcx, 7
        jnz     .einval
        mov     rax, rcx
        shr     rax, CPU_KERNEL_SHIFT
        cmp     rax, CPU_KERNEL_TOP
        jne     .einval
        mov     eax, [rcx + CPU_INIT_OFF_VERSION]
        cmp     eax, CPU_INIT_VERSION
        jne     .einval
        mov     eax, [rcx + CPU_INIT_OFF_SIZE]
        cmp     eax, CPU_INIT_SIZE
        jne     .einval
        cmp     qword [rcx + CPU_INIT_OFF_FLAGS], CPU_INIT_FLAGS_VALUE
        jne     .einval
        cmp     qword [rcx + CPU_INIT_OFF_RESERVED0], CPU_INIT_RESERVED_VALUE
        jne     .einval
        cmp     qword [rcx + CPU_INIT_OFF_RESERVED1], CPU_INIT_RESERVED_VALUE
        jne     .einval
        ; BootInfo must be readable and 8 byte aligned
        mov     rax, [rcx + CPU_INIT_OFF_BOOTINFO_VA]
        test    rax, rax
        jz      .einval
        test    rax, 7
        jnz     .einval
        ; the physical addresses must stay inside this stage's window
        mov     rax, [rcx + CPU_INIT_OFF_BOOTINFO_PHYS]
        test    rax, rax
        jz      .einval
        test    rax, 7
        jnz     .einval
        mov     rdx, CPU_PHYS_LIMIT
        cmp     rax, rdx
        jae     .einval
        mov     rax, [rcx + CPU_INIT_OFF_CR3_PHYS]
        test    rax, rax
        jz      .einval
        test    rax, CPU_PAGE_SIZE - 1
        jnz     .einval
        mov     rdx, CPU_PHYS_LIMIT
        cmp     rax, rdx
        jae     .einval
        ; the kernel stack top this module must program into TSS.RSP0
        mov     rax, [rcx + CPU_INIT_OFF_STACK_TOP]
        test    rax, rax
        jz      .einval
        test    rax, CPU_STACK_ALIGN - 1
        jnz     .einval
        mov     rdx, rax
        shr     rdx, CPU_KERNEL_SHIFT
        cmp     rdx, CPU_KERNEL_TOP
        jne     .einval
        ; ---------------- once ----------------
        cmp     byte [cpu_state], 0
        jne     .ealready
        ; ---------------- the 8259 pair must stop delivering ----------------
        ; from here on the IDT owns every vector, the PIC is not part of it
        mov     al, 0xFF
        out     0x21, al
        out     0xA1, al
        ; ---------------- descriptors, TSS, IDT ----------------
        call    cpu_gdt_init
        test    eax, eax
        jnz     .fail
        mov     rcx, [rbp-8]
        mov     rcx, [rcx + CPU_INIT_OFF_STACK_TOP]
        call    cpu_set_rsp0
        test    eax, eax
        jnz     .fail
        call    cpu_idt_init
        test    eax, eax
        jnz     .fail
        ; keep the handover facts for diagnostics, they are read only after
        ; the IDT is live
        mov     rcx, [rbp-8]
        mov     rax, [rcx + CPU_INIT_OFF_BOOTINFO_VA]
        mov     [cpu_bootinfo_va], rax
        mov     rax, [rcx + CPU_INIT_OFF_CR3_PHYS]
        mov     [cpu_boot_cr3], rax
        mov     rax, [rcx + CPU_INIT_OFF_STACK_TOP]
        mov     [cpu_boot_stack], rax
        mov     byte [cpu_state], 1
        xor     eax, eax
        xor     edx, edx
        leave
        ret
.eperm:
        mov     eax, CPU_EPERM
        jmp     .fail
.enotsup:
        mov     eax, CPU_ENOTSUP
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
; load the static IDT after checking every gate against this module
; in
; out
; eax = 0 on success, EINVAL when a gate is not the gate this module built,
;       EALREADY when the IDT is already loaded
; ------------------------------------------------------------------

cpu_idt_init:
        push    rbp
        mov     rbp, rsp
        sub     rsp, CPU_STACK_FRAME
        cmp     byte [cpu_idt_ready], 0
        jne     .already
        xor     r8, r8
.gate:
        mov     rax, r8
        shl     rax, 4
        lea     rdx, [cpu_idt + rax]
        movzx   ecx, word [rdx + 2]
        cmp     ecx, CPU_SEL_CODE
        jne     .invalid
        movzx   ecx, byte [rdx + 5]
        cmp     ecx, CPU_IDT_TYPE_ATTR
        jne     .invalid
        movzx   ecx, byte [rdx + 4]
        and     ecx, 7
        cmp     ecx, CPU_IDT_IST_MAX
        ja      .invalid
        movzx   ecx, word [rdx + 0]
        movzx   eax, word [rdx + 6]
        shl     rax, 16
        or      rcx, rax
        mov     eax, dword [rdx + 8]
        shl     rax, 32
        or      rcx, rax
        cmp     rcx, cpu_trap_stubs_start
        jb      .invalid
        cmp     rcx, cpu_trap_stubs_end
        jae     .invalid
        inc     r8
        cmp     r8, CPU_IDT_VECTORS
        jb      .gate
        lidt    [cpu_idtr]
        mov     byte [cpu_idt_ready], 1
        xor     eax, eax
        xor     edx, edx
        leave
        ret
.already:
        mov     eax, CPU_EALREADY
        jmp     .fail
.invalid:
        mov     eax, CPU_EINVAL
.fail:
        xor     edx, edx
        leave
        ret

cpu_code_end:

; ------------------------------------------------------------------
; data last: stacks, TSS, GDT, IDT, state, messages
; ------------------------------------------------------------------

include 'data.inc'

if $ <> cpu_data_end
        err 'the CPU module must not emit anything after cpu_data_end'
end if
