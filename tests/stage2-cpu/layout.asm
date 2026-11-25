;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Layout probe for the stage 2 CPU module, task 27, task 38, task 43,
;; task 69 and task 113.
;;
;; This file is a test harness, not part of the kernel. It assembles
;; kernel/cpu/cpu.asm exactly as the kernel will include it, appends a manifest
;; of label addresses, and lets tests/stage2-cpu/check.py verify the emitted
;; bytes. Nothing here is executed and no machine is started.
;;
;; CPU_PROBE_ORG selects the link base; check.py assembles the probe twice,
;; once at 0 for byte offsets and once at VIRT_KERNEL_BASE to prove that the
;; module links at the kernel base with the same internal layout.
;;
;; The manifest is the only way the checker learns where a label ended up. It
;; is a fixed list of qwords in the order check.py expects; the magic and the
;; version are checked before anything else is read.
;;

use64

if defined CPU_PROBE_ORG
        org CPU_PROBE_ORG
else
        org 0
end if

include '../../kernel/cpu/cpu.asm'

; ------------------------------------------------------------------
; manifest, fixed order, read by check.py
; ------------------------------------------------------------------

align 8
cpu_probe:
        dq      'K64CPUP1'                  ; magic
        dq      5                           ; manifest version
        dq      cpu_gdt
        dq      cpu_gdt_end - cpu_gdt
        dq      cpu_gdt_null
        dq      cpu_gdt_code
        dq      cpu_gdt_data
        dq      cpu_gdt_tss
        dq      cpu_gdtr
        dq      cpu_gdtr_end - cpu_gdtr
        dq      cpu_tss
        dq      cpu_tss_end - cpu_tss
        dq      cpu_tss_rsp0
        dq      cpu_tss_iomap
        dq      cpu_tss_ist1
        dq      cpu_idt
        dq      cpu_idt_end - cpu_idt
        dq      cpu_idtr
        dq      cpu_idtr_end - cpu_idtr
        dq      cpu_data_start
        dq      cpu_data_end
        dq      cpu_ist1_df
        dq      cpu_ist2_nmi
        dq      cpu_ist3_mc
        dq      cpu_ist4_test
        dq      cpu_panic_stack
        dq      cpu_trap_stubs_start
        dq      cpu_trap_stubs_end
        dq      cpu_trap_dispatch
        dq      cpu_code_start
        dq      cpu_code_end
        dq      CPU_FRAME_SIZE
        dq      CPU_FRAME_CR2
        dq      CPU_FRAME_VECTOR
        dq      CPU_FRAME_ERROR
        dq      CPU_FRAME_RIP
        dq      CPU_IDT_VECTORS
        dq      CPU_TSS_IOMAP_NONE
        dq      cpu_init
        dq      cpu_gdt_init
        dq      cpu_idt_init
        dq      cpu_set_rsp0
        dq      cpu_com1_init
        dq      cpu_com1_putc
        dq      cpu_com1_puts
        dq      cpu_com1_puthex
        dq      cpu_trap_expect
        dq      cpu_trap_finish
        dq      cpu_selftest
        dq      cpu_panic
        dq      cpu_halt_forever
        dq      CPU_EXPECT_SIZE
        dq      CPU_EXPECT_OFF_ERROR_MASK
        dq      CPU_EXPECT_OFF_ERROR_VALUE
        dq      CPU_EXPECT_OFF_CR2_MASK
        dq      CPU_EXPECT_OFF_CR2_VALUE
        dq      cpu_trap_armed
        dq      cpu_trap_result_ready
        dq      cpu_trap_vector
        dq      cpu_trap_expect_rip
        dq      cpu_trap_resume_rip
        dq      cpu_trap_err_mask
        dq      cpu_trap_err_value
        dq      cpu_trap_cr2_mask
        dq      cpu_trap_cr2_value
        dq      cpu_trap_handler_frame
        dq      cpu_trap_frame_result
        dq      cpu_selftest_expect
        dq      cpu_selftest_out
        dq      CPU_TSS_DESC_TYPE
        dq      CPU_TSS_DESC_TYPE_BUSY
        dq      cpu_irq_set_handler
        dq      cpu_irq_handler
        dq      CPU_IRQ_VECTOR_MIN
        ; APP-v1, task 113: the user segments, the syscall path and its state
        dq      cpu_gdt_user_data
        dq      cpu_gdt_user_code
        dq      cpu_syscall_init
        dq      cpu_syscall_set_handler
        dq      cpu_user_fault_set_handler
        dq      cpu_syscall_entry
        dq      cpu_syscall_ready
        dq      cpu_user_fault_handler
        dq      cpu_syscall_handlers
        dq      cpu_syscall_scratch_rsp
        dq      CPU_GDT_USER_DATA_VALUE
        dq      CPU_GDT_USER_CODE_VALUE
        dq      CPU_SEL_USER_DATA
        dq      CPU_SEL_USER_CODE
        dq      CPU_STAR_VALUE
        dq      CPU_SYSRET_CS
        dq      CPU_SYSRET_SS
        dq      CPU_SFMASK_VALUE
        dq      CPU_SYS_CALL_MAX
        dq      CPU_SYS_VECTOR
        dq      CPU_EFER_SCE
        dq      CPU_STAR_MSR
        dq      CPU_LSTAR_MSR
        dq      CPU_SFMASK_MSR
cpu_probe_end:

if (cpu_probe_end - cpu_probe) <> 98 * 8
        err 'the manifest must keep its fixed entry count'
end if
