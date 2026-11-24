;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; IRQ layer: the module aggregator, contract IRQ-v1.
;;
;; Contents, in this order:
;;   api.inc       constants, layout assertions, register helper macros
;;   lapic.inc     irq_lapic_bringup (masked start + bounded ISR flush)
;;   calib.inc     irq_calibrate (PIT ch2 window, median of three)
;;   dispatch.inc  irq_dispatch (0x20 tick + EOI, 0xFF count, else panic)
;;   selftest.inc  irq_selftest, the only STI owner of the kernel
;;   this file     irq_init
;;   data.inc      every mutable byte, 4 KiB aligned at both ends
;;
;; The includer sets use64 and org and must include cpu/cpu.asm and
;; mm/mm.asm first: this module calls vmm_map_mmio, cpu_irq_set_handler,
;; cpu_panic and the bounded console, and nothing else outside itself.
;; The data region stays last, no code may follow irq_data_end.
;;
;; Include it exactly once per image.
;;

irq_code_start:

include 'api.inc'
include 'lapic.inc'
include 'calib.inc'
include 'dispatch.inc'
include 'selftest.inc'

; ------------------------------------------------------------------
; bring the IRQ layer up, in the frozen order
; in
; nothing; cpu_init, pmm_init, vmm_init and heap_init must have
; succeeded, IF must be 0
; out
; eax = IRQ_OK, or a signed status the foundation turns into cpu_panic:
;       EPERM     IF was set on entry
;       EALREADY  irq_init already succeeded once
;       anything vmm_map_mmio or cpu_irq_set_handler report
;       EIO       PIC read back, APIC base MSR or calibration failed
; rdx = 0
;
;   a  map the LAPIC page (PCD|RW|NX) at the frozen MMIO VA
;   b  full 8259 remap, then mask everything, verified by read back
;   c  IA32_APIC_BASE: enabled at 0xFEE00000, bit 11 written back set
;   d  LAPIC bring-up: TPR/SVR, masked LVTs, bounded ISR flush
;   e  PIT ch2 calibration, median of three, 5 percent sanity band
;   f  register irq_dispatch as the asynchronous handler
; ------------------------------------------------------------------

irq_init:
        push    rbp
        mov     rbp, rsp
        sub     rsp, IRQ_STACK_FRAME
        cld
        ; this stage only runs with interrupts masked
        pushfq
        pop     rax
        test    eax, IRQ_RFLAGS_IF
        jnz     .eperm
        cmp     qword [irq_state], 0
        jne     .ealready

        ; ---------------- a: the LAPIC MMIO page ----------------
        mov     rcx, IRQ_LAPIC_VA
        mov     rdx, IRQ_LAPIC_PA
        call    vmm_map_mmio
        test    eax, eax
        jnz     .fail

        ; ---------------- b: the 8259 pair ----------------
        ; ICW1: edge triggered, cascade, ICW4 needed
        mov     dx, IRQ_PIC1_CMD
        mov     al, IRQ_PIC_ICW1
        out     dx, al
        out     IRQ_PIC_IO_DELAY, al
        mov     dx, IRQ_PIC2_CMD
        mov     al, IRQ_PIC_ICW1
        out     dx, al
        out     IRQ_PIC_IO_DELAY, al
        ; ICW2: the vector bases
        mov     dx, IRQ_PIC1_DATA
        mov     al, IRQ_PIC_ICW2_MASTER
        out     dx, al
        out     IRQ_PIC_IO_DELAY, al
        mov     dx, IRQ_PIC2_DATA
        mov     al, IRQ_PIC_ICW2_SLAVE
        out     dx, al
        out     IRQ_PIC_IO_DELAY, al
        ; ICW3: the cascade wiring
        mov     dx, IRQ_PIC1_DATA
        mov     al, IRQ_PIC_ICW3_MASTER
        out     dx, al
        out     IRQ_PIC_IO_DELAY, al
        mov     dx, IRQ_PIC2_DATA
        mov     al, IRQ_PIC_ICW3_SLAVE
        out     dx, al
        out     IRQ_PIC_IO_DELAY, al
        ; ICW4: 8086 mode
        mov     dx, IRQ_PIC1_DATA
        mov     al, IRQ_PIC_ICW4
        out     dx, al
        out     IRQ_PIC_IO_DELAY, al
        mov     dx, IRQ_PIC2_DATA
        mov     al, IRQ_PIC_ICW4
        out     dx, al
        out     IRQ_PIC_IO_DELAY, al
        ; OCW1: mask every line, then prove it by read back
        mov     dx, IRQ_PIC1_DATA
        mov     al, IRQ_PIC_OCW1_MASK
        out     dx, al
        out     IRQ_PIC_IO_DELAY, al
        mov     dx, IRQ_PIC2_DATA
        mov     al, IRQ_PIC_OCW1_MASK
        out     dx, al
        out     IRQ_PIC_IO_DELAY, al
        mov     dx, IRQ_PIC1_DATA
        in      al, dx
        cmp     al, IRQ_PIC_OCW1_MASK
        jne     .eio
        mov     dx, IRQ_PIC2_DATA
        in      al, dx
        cmp     al, IRQ_PIC_OCW1_MASK
        jne     .eio

        ; ---------------- c: the APIC base MSR ----------------
        mov     ecx, IRQ_APIC_BASE_MSR
        rdmsr
        shl     rdx, 32
        or      rax, rdx
        mov     rdx, rax
        mov     r8, IRQ_APIC_BASE_MASK
        and     rdx, r8
        mov     r8, IRQ_APIC_BASE_PA
        cmp     rdx, r8
        jne     .eio
        test    eax, IRQ_APIC_BASE_ENABLE
        jz      .eio
        ; write the enable bit back explicitly
        or      rax, IRQ_APIC_BASE_ENABLE
        mov     rdx, rax
        shr     rdx, 32
        mov     ecx, IRQ_APIC_BASE_MSR
        wrmsr

        ; ---------------- d: LAPIC bring-up ----------------
        call    irq_lapic_bringup
        test    eax, eax
        jnz     .fail

        ; ---------------- e: counter calibration ----------------
        call    irq_calibrate
        test    eax, eax
        jnz     .fail

        ; ---------------- f: the asynchronous handler ----------------
        mov     rcx, irq_dispatch
        call    cpu_irq_set_handler
        test    eax, eax
        jnz     .fail

        mov     qword [irq_state], 1
        xor     eax, eax
        xor     edx, edx
        leave
        ret
.eperm:
        mov     eax, IRQ_EPERM
        jmp     .fail
.ealready:
        mov     eax, IRQ_EALREADY
        jmp     .fail
.eio:
        mov     eax, IRQ_EIO
.fail:
        xor     edx, edx
        leave
        ret

irq_code_end:

; ------------------------------------------------------------------
; data last: counters, evidence, calibration results, messages
; ------------------------------------------------------------------

include 'data.inc'

if $ <> irq_data_end
        err 'the IRQ module must not emit anything after irq_data_end'
end if
