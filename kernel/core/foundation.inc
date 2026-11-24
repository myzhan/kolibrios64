;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Core integration: the foundation bring-up orchestration.
;;
;; Runs on the BSP with IF = 0 after the first stage report, on the kernel
;; stack the entry point established. The fixed order is:
;;
;;   fill K64_INIT_PARAMS -> cpu_init (GDT -> RSP0 -> IDT, no output in
;;   between) -> K64-CPU-OK -> pmm_init -> K64-PMM-OK -> vmm_init ->
;;   vmm_boot_protect over the CPU/MM/core data regions (RW/NX) ->
;;   K64-VMM-OK -> heap_init -> K64-HEAP-OK -> irq_init ->
;;   vmm_boot_protect over the IRQ data region -> irq_selftest ->
;;   K64-IRQ-OK -> core_selftest -> the IRQ-v2 stage (input_init ->
;;   vmm_boot_protect over the input data region -> input_selftest ->
;;   K64-INPUT-OK, mandatory devices, no skip) -> the GUI-G1 stage
;;   (video_init -> vmm_boot_protect over the video data region ->
;;   video_selftest -> K64-GUI-OK, or K64-GUI-SKIP when the GOP is absent
;;   or the geometry is not the frozen 1024x768) -> K64-FOUNDATION-OK
;;   -> halt.
;;
;; Nothing here ever executes STI itself; the only STI windows of the
;; whole kernel belong to irq_selftest, which always returns with the
;; timer masked and IF = 0. Nothing calls back into UEFI. Every
;; failure goes through cpu_panic, which reports on COM1 within a bound
;; and halts on its own stack. All console output uses the new ABI CPU
;; module functions, never the first stage com1_* helpers.
;;

; bounded marker output through the new ABI console, inlined so the
; caller frame (RSP mod 16 = 0) is preserved for cpu_com1_puts
macro k64_report msg*, fail*
        mov     rcx, msg
        mov     rdx, CPU_PUTS_MAX
        call    cpu_com1_puts
        test    eax, eax
        jnz     fail
end macro

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; bring up the foundation modules and run the core self test
; in
; rcx = physical address of K64BOOTINFO, identity mapped
; out
; never returns on success: reports K64-FOUNDATION-OK and halts;
; any failure panics and halts
; destroys everything (no caller state survives by design)
k64_foundation_init:
        push    rbp
        mov     rbp, rsp
        sub     rsp, K64_STACK_FRAME
        cld
        mov     [rbp-8], rcx            ; BootInfo physical address

        ; ---------------- the shared handover parameters ----------------
        ; filled before any module runs, in core static data so the VA is
        ; canonical high half and 8 byte aligned as cpu_init demands
        mov     dword [core_init_params + K64_INIT_OFF_VERSION], K64_INIT_VERSION
        mov     dword [core_init_params + K64_INIT_OFF_SIZE], K64_INIT_SIZE
        mov     rax, [rbp-8]
        mov     [core_init_params + K64_INIT_OFF_BOOTINFO_VA], rax
        mov     [core_init_params + K64_INIT_OFF_BOOTINFO_PHYS], rax
        mov     rax, cr3
        and     rax, not (K64_PAGE_SIZE - 1)
        mov     [core_init_params + K64_INIT_OFF_CR3_PHYS], rax
        ; the stack top is the exact BootInfo range conversion, not the
        ; current RSP: KERNEL_BASE + (stack_phys_end - kernel_phys_start)
        mov     rax, [rbp-8]
        mov     rdx, [rax + K64BOOTINFO.stack_phys_end]
        sub     rdx, [rax + K64BOOTINFO.kernel_phys_start]
        add     rdx, VIRT_KERNEL_BASE
        mov     [core_init_params + K64_INIT_OFF_STACK_TOP], rdx
        mov     qword [core_init_params + K64_INIT_OFF_FLAGS], K64_INIT_FLAGS_VALUE
        mov     qword [core_init_params + K64_INIT_OFF_RESERVED0], K64_INIT_RESERVED_VALUE
        mov     qword [core_init_params + K64_INIT_OFF_RESERVED1], K64_INIT_RESERVED_VALUE

        ; ---------------- CPU: GDT -> RSP0 -> IDT ----------------
        ; cpu_init performs the descriptor handover with no output and no
        ; other initialisation between LGDT and LIDT
        mov     qword [rbp-16], kmsg_fail_cpu
        mov     rcx, core_init_params
        call    cpu_init
        test    eax, eax
        jnz     .fail

        ; the console the rest of this path reports through
        mov     qword [rbp-16], kmsg_fail_com1
        call    cpu_com1_init
        test    eax, eax
        jnz     .fail

        k64_report kmsg_cpu_ok, .fail

        ; ---------------- physical allocator ----------------
        mov     qword [rbp-16], kmsg_fail_pmm
        mov     rcx, core_init_params
        call    pmm_init
        test    eax, eax
        jnz     .fail

        k64_report kmsg_pmm_ok, .fail

        ; ---------------- virtual memory on the loader CR3 ----------------
        mov     qword [rbp-16], kmsg_fail_vmm
        mov     rcx, core_init_params
        call    vmm_init
        test    eax, eax
        jnz     .fail

        ; the module data regions become RW/NX: stacks, bitmaps, tables
        ; and scratch are data, never instructions
        mov     qword [rbp-16], kmsg_fail_protect
        mov     rcx, cpu_data_start
        mov     rdx, (cpu_data_end - cpu_data_start) / K64_PAGE_SIZE
        mov     r8, MM_MAP_RW or MM_MAP_NX
        call    vmm_boot_protect
        test    eax, eax
        jnz     .fail
        mov     rcx, mm_data_start
        mov     rdx, (mm_data_end - mm_data_start) / K64_PAGE_SIZE
        mov     r8, MM_MAP_RW or MM_MAP_NX
        call    vmm_boot_protect
        test    eax, eax
        jnz     .fail
        mov     rcx, core_data_start
        mov     rdx, (core_data_end - core_data_start) / K64_PAGE_SIZE
        mov     r8, MM_MAP_RW or MM_MAP_NX
        call    vmm_boot_protect
        test    eax, eax
        jnz     .fail

        k64_report kmsg_vmm_ok, .fail

        ; ---------------- the early bump heap ----------------
        mov     qword [rbp-16], kmsg_fail_heap
        call    heap_init
        test    eax, eax
        jnz     .fail

        k64_report kmsg_heap_ok, .fail

        ; ---------------- the IRQ layer ----------------
        mov     qword [rbp-16], kmsg_fail_irq
        call    irq_init
        test    eax, eax
        jnz     .fail

        ; the IRQ data region becomes RW/NX like the other module blocks
        mov     qword [rbp-16], kmsg_fail_protect
        mov     rcx, irq_data_start
        mov     rdx, (irq_data_end - irq_data_start) / K64_PAGE_SIZE
        mov     r8, MM_MAP_RW or MM_MAP_NX
        call    vmm_boot_protect
        test    eax, eax
        jnz     .fail

        ; the IRQ self test owns the only STI windows of the kernel; on
        ; failure it has already printed its leg mask, the foundation
        ; takes the standard fatal path
        mov     qword [rbp-16], kmsg_fail_irq
        call    irq_selftest
        test    eax, eax
        jnz     .fail

        k64_report kmsg_irq_ok, .fail

        ; ---------------- the cross module core self test ----------------
        call    core_selftest
        test    eax, eax
        jnz     .selftest_fail

        ; ---------------- IRQ-v2: the input closed loop ----------------
        ; Runs AFTER core_selftest like the GUI stage: the IOAPIC mapping
        ; allocates its page table after the leg 7 accounting baseline,
        ; so the frozen 3/3/3 table delta stays untouched.
        ;
        ; The IOAPIC and the 8042 are mandatory platform devices: unlike
        ; the GUI stage there is no skip semantics, every failure is a
        ; real bring-up fault that panics. The self test needs the host
        ; injection handshake (gdbstub stage polling + monitor sendkey /
        ; mouse commands); a timeout is a leg failure, never a pass.
        mov     qword [rbp-16], kmsg_fail_input
        call    input_init
        test    eax, eax
        jnz     .fail

        ; the input data region becomes RW/NX like the other blocks
        mov     qword [rbp-16], kmsg_fail_protect
        mov     rcx, input_data_start
        mov     rdx, (input_data_end - input_data_start) / K64_PAGE_SIZE
        mov     r8, MM_MAP_RW or MM_MAP_NX
        call    vmm_boot_protect
        test    eax, eax
        jnz     .fail

        ; on failure the input self test has already printed its leg
        ; mask, the foundation takes the standard fatal path
        mov     qword [rbp-16], kmsg_fail_input
        call    input_selftest
        test    eax, eax
        jnz     .fail

        k64_report kmsg_input_ok, .fail

        ; ---------------- GUI-G1: the framebuffer closed loop ----------------
        ; Runs AFTER core_selftest on purpose: the framebuffer mapping
        ; allocates its page tables after the leg 7 accounting baseline,
        ; so the frozen 3/3/3 table delta stays untouched.
        ;
        ; A missing GOP (FLAG_FRAMEBUFFER clear) or a geometry G1 does
        ; not support (video_init / video_selftest return ENOTSUP) is a
        ; non fatal skip: K64-GUI-SKIP and straight on to FOUNDATION-OK.
        ; Frozen task-85 policy: ENOTSUP only ever comes from the video
        ; geometry gates; a mapping fault inside video_init returns EIO
        ; and every other failure is a real bring-up fault that panics.
        mov     rax, [rbp-8]
        mov     rax, [rax + K64BOOTINFO.flags]
        test    rax, K64BOOTINFO_FLAG_FRAMEBUFFER
        jz      .gui_skip               ; no GOP handover, nothing to map

        mov     qword [rbp-16], kmsg_fail_gui
        mov     rcx, [rbp-8]            ; BootInfo, identity mapped
        call    video_init
        cmp     eax, VIDEO_ENOTSUP
        je      .gui_skip               ; foreign format or oversized FB
        test    eax, eax
        jnz     .fail

        ; the video data region becomes RW/NX like the other blocks
        mov     qword [rbp-16], kmsg_fail_protect
        mov     rcx, video_data_start
        mov     rdx, (video_data_end - video_data_start) / K64_PAGE_SIZE
        mov     r8, MM_MAP_RW or MM_MAP_NX
        call    vmm_boot_protect
        test    eax, eax
        jnz     .fail

        ; the frozen pattern; a geometry other than exactly 1024x768 is
        ; refused with ENOTSUP and skips the milestone, never panics
        mov     qword [rbp-16], kmsg_fail_gui
        call    video_selftest
        cmp     eax, VIDEO_ENOTSUP
        je      .gui_skip
        test    eax, eax
        jnz     .fail

        k64_report kmsg_gui_ok, .fail
        jmp     .gui_done
.gui_skip:
        k64_report kmsg_gui_skip, .fail
.gui_done:

        k64_report kmsg_foundation_ok, .fail
        jmp     cpu_halt_forever

.selftest_fail:
        ; report the leg mask before the panic so the serial log names
        ; the failing probe, then take the standard fatal path
        mov     [rbp-24], eax
        mov     rcx, kmsg_selftest_mask
        mov     rdx, CPU_PUTS_MAX
        call    cpu_com1_puts
        mov     rcx, [core_selftest_mask]
        mov     rdx, CPU_PUTHEX_DIGITS
        call    cpu_com1_puthex
        mov     rcx, kmsg_core_nl
        mov     rdx, CPU_PUTS_MAX
        call    cpu_com1_puts
        mov     eax, [rbp-24]
        mov     qword [rbp-16], kmsg_fail_selftest
        jmp     .fail

.fail:
        ; eax = the status of the failed step, [rbp-16] = its message
        movsxd  r8, eax
        xor     edx, edx
        mov     rcx, [rbp-16]
        call    cpu_panic
        ; cpu_panic never returns; halt is the belt and braces exit
        jmp     cpu_halt_forever

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; GUI-G1 messages, read only constants emitted with the foundation code
; like the first stage kmsg_* block of kernel64.asm; the mutable core
; data region stays the last bytes of the image in core/selftest.inc.
; The skip marker is not a milestone: the smoke judges it separately.
kmsg_gui_ok           db 'K64-GUI-OK', 13, 10, 0
kmsg_gui_skip         db 'K64-GUI-SKIP', 13, 10, 0
kmsg_fail_gui         db 'foundation: video bring-up failed', 0

; IRQ-v2 messages, same read only convention; there is no input skip
; marker, the IOAPIC and the 8042 are mandatory platform devices
kmsg_input_ok         db 'K64-INPUT-OK', 13, 10, 0
kmsg_fail_input       db 'foundation: input bring-up failed', 0
