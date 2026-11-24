;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; All data of the CPU module: the five static stacks, the TSS, the GDT, both
;; descriptor table registers, the fully static IDT, the module state and the
;; diagnostic strings.
;;
;; This file must stay the last one included by cpu.asm: the data region starts
;; and ends 4 KiB aligned and no code may follow it. The stacks are emitted as
;; real zero bytes (db ... dup (0)), not reserved with rb, so a flat binary
;; really contains them and the layout test can see them.
;;
;; The IDT is built at assembly time from the stub labels of idt_traps.inc, so
;; a gate can never point at a stub that does not exist and the table needs no
;; runtime fill loop.
;;

align CPU_DATA_ALIGN
cpu_data_start:

; ------------------------------------------------------------------
; five 16 KiB stacks, each one page aligned by construction
; ------------------------------------------------------------------

cpu_ist1_df:
        db      CPU_IST_SIZE dup (0)
cpu_ist1_top:
cpu_ist2_nmi:
        db      CPU_IST_SIZE dup (0)
cpu_ist2_top:
cpu_ist3_mc:
        db      CPU_IST_SIZE dup (0)
cpu_ist3_top:
cpu_ist4_test:
        db      CPU_IST_SIZE dup (0)
cpu_ist4_top:
cpu_panic_stack:
        db      CPU_IST_SIZE dup (0)
cpu_panic_stack_top:

; ------------------------------------------------------------------
; TSS, 104 bytes. RSP0 is filled by cpu_set_rsp0, IST1..IST4 point at the top
; of their stacks, IST5..IST7 stay zero because no vector uses them, and the
; I/O map base is past the limit so no bitmap exists.
; ------------------------------------------------------------------

align CPU_TSS_ALIGN
cpu_tss:
cpu_tss_reserved0:
        dd      0
cpu_tss_rsp0:
        dq      0
cpu_tss_rsp1:
        dq      0
cpu_tss_rsp2:
        dq      0
cpu_tss_reserved1:
        dd      0
        dd      0
cpu_tss_ist1:
        dq      cpu_ist1_top
cpu_tss_ist2:
        dq      cpu_ist2_top
cpu_tss_ist3:
        dq      cpu_ist3_top
cpu_tss_ist4:
        dq      cpu_ist4_top
cpu_tss_ist5:
        dq      0
cpu_tss_ist6:
        dq      0
cpu_tss_ist7:
        dq      0
cpu_tss_reserved2:
        dd      0
        dd      0
        dw      0
cpu_tss_iomap:
        dw      CPU_TSS_IOMAP_NONE
cpu_tss_end:

if cpu_tss_end - cpu_tss <> CPU_TSS_SIZE
        err 'TSS must stay 104 bytes'
end if
if cpu_tss_rsp0 - cpu_tss <> CPU_TSS_OFF_RSP0
        err 'TSS rsp0 offset'
end if
if cpu_tss_rsp1 - cpu_tss <> CPU_TSS_OFF_RSP1
        err 'TSS rsp1 offset'
end if
if cpu_tss_rsp2 - cpu_tss <> CPU_TSS_OFF_RSP2
        err 'TSS rsp2 offset'
end if
if cpu_tss_ist1 - cpu_tss <> CPU_TSS_OFF_IST1
        err 'TSS ist1 offset'
end if
if cpu_tss_ist2 - cpu_tss <> CPU_TSS_OFF_IST2
        err 'TSS ist2 offset'
end if
if cpu_tss_ist3 - cpu_tss <> CPU_TSS_OFF_IST3
        err 'TSS ist3 offset'
end if
if cpu_tss_ist4 - cpu_tss <> CPU_TSS_OFF_IST4
        err 'TSS ist4 offset'
end if
if cpu_tss_ist5 - cpu_tss <> CPU_TSS_OFF_IST5
        err 'TSS ist5 offset'
end if
if cpu_tss_ist6 - cpu_tss <> CPU_TSS_OFF_IST6
        err 'TSS ist6 offset'
end if
if cpu_tss_ist7 - cpu_tss <> CPU_TSS_OFF_IST7
        err 'TSS ist7 offset'
end if
if cpu_tss_iomap - cpu_tss <> CPU_TSS_OFF_IOMAP
        err 'TSS iomap base offset'
end if
if (cpu_tss and (CPU_TSS_ALIGN - 1)) <> 0
        err 'TSS must be 16 byte aligned'
end if

; ------------------------------------------------------------------
; GDT, 40 bytes: null, 64-bit code, 64-bit data, 16 byte TSS descriptor
; ------------------------------------------------------------------

align CPU_GDT_ALIGN
cpu_gdt:
cpu_gdt_null:
        dq      CPU_GDT_NULL_VALUE
cpu_gdt_code:
        dq      CPU_GDT_CODE_VALUE
cpu_gdt_data:
        dq      CPU_GDT_DATA_VALUE
cpu_gdt_tss:
        dw      CPU_TSS_LIMIT
        dw      cpu_tss and 0xFFFF
        db      (cpu_tss shr 16) and 0xFF
        db      CPU_TSS_DESC_TYPE
        db      0
        db      (cpu_tss shr 24) and 0xFF
        dq      (cpu_tss shr 32)
cpu_gdt_end:

if cpu_gdt_end - cpu_gdt <> CPU_GDT_SIZE
        err 'GDT must stay 40 bytes'
end if
if cpu_gdt_code - cpu_gdt <> CPU_GDT_OFF_CODE
        err 'code descriptor offset'
end if
if cpu_gdt_data - cpu_gdt <> CPU_GDT_OFF_DATA
        err 'data descriptor offset'
end if
if cpu_gdt_tss - cpu_gdt <> CPU_GDT_OFF_TSS
        err 'TSS descriptor offset'
end if
if (CPU_TSS_LIMIT shr 16) <> 0
        err 'the TSS limit must fit the low limit field'
end if
if (cpu_gdt and (CPU_GDT_ALIGN - 1)) <> 0
        err 'GDT must be 16 byte aligned'
end if

; packed GDTR: 16 bit limit followed by the 64 bit base
cpu_gdtr:
        dw      CPU_GDTR_LIMIT
        dq      cpu_gdt
cpu_gdtr_end:

if cpu_gdtr_end - cpu_gdtr <> CPU_GDTR_SIZE
        err 'GDTR must stay 10 bytes'
end if

; ------------------------------------------------------------------
; IDT, one gate per vector, built from the stub labels
; gate: offset 0..15, selector, IST | 0, type, offset 16..31, offset 32..63
; ------------------------------------------------------------------

align CPU_IDT_ALIGN
cpu_idt:

repeat CPU_IDT_VECTORS, v:0
        cpu_classify_vector v
        dq      (cpu_stub_#v and 0xFFFF) or (CPU_SEL_CODE shl 16) or (cpu_vec_ist shl 32) or (CPU_IDT_TYPE_ATTR shl 40) or (((cpu_stub_#v shr 16) and 0xFFFF) shl 48)
        dq      (cpu_stub_#v shr 32)
end repeat

cpu_idt_end:

if cpu_idt_end - cpu_idt <> CPU_IDT_SIZE
        err 'IDT must stay 256 gates of 16 bytes'
end if
if (cpu_idt and (CPU_IDT_ALIGN - 1)) <> 0
        err 'IDT must be 16 byte aligned'
end if

cpu_idtr:
        dw      CPU_IDT_LIMIT
        dq      cpu_idt
cpu_idtr_end:

if cpu_idtr_end - cpu_idtr <> CPU_IDTR_SIZE
        err 'IDTR must stay 10 bytes'
end if

; ------------------------------------------------------------------
; module state, all zero until the owning function sets it
;
; The trap state is exactly what the three trap API calls agree on: one armed
; expectation (vector, the RIP the hardware must have saved, the RIP to resume
; at, and the two masked hardware facts) and one result waiting to be
; collected.
; ------------------------------------------------------------------

align 8
cpu_state:
        db      0
cpu_gdt_ready:
        db      0
cpu_idt_ready:
        db      0
cpu_com1_ready:
        db      0
cpu_trap_armed:
        db      0
cpu_trap_result_ready:
        db      0
cpu_trap_vector:
        db      0
        db      0                       ; pad to the first qword
cpu_trap_expect_rip:
        dq      0
cpu_trap_resume_rip:
        dq      0
cpu_trap_err_mask:
        dq      0
cpu_trap_err_value:
        dq      0
cpu_trap_cr2_mask:
        dq      0
cpu_trap_cr2_value:
        dq      0
cpu_trap_handler_frame:
        dq      0
cpu_panic_active:
        db      0
        db      0
        dw      0
        dd      0
cpu_panic_message:
        dq      0
cpu_panic_frame:
        dq      0
cpu_panic_reason:
        dq      0
cpu_bootinfo_va:
        dq      0
cpu_boot_cr3:
        dq      0
cpu_boot_stack:
        dq      0

; ------------------------------------------------------------------
; the dispatcher's private copy of the accepted frame, the self test's output
; buffer, and the expectation block the self test passes for #DE: any error
; code (all bits set) that is zero, and any CR2 (mask zero)
; ------------------------------------------------------------------

align 8
cpu_trap_frame_result:
        db      CPU_FRAME_SIZE dup (0)
cpu_selftest_out:
        db      CPU_FRAME_SIZE dup (0)
cpu_selftest_expect:
cpu_selftest_expect_error_mask:
        dq      0xFFFFFFFFFFFFFFFF
cpu_selftest_expect_error_value:
        dq      0
cpu_selftest_expect_cr2_mask:
        dq      0
cpu_selftest_expect_cr2_value:
        dq      0

; ------------------------------------------------------------------
; diagnostics, ASCII only
; ------------------------------------------------------------------

msg_panic_head        db 'K64-CPU-PANIC', 13, 10, 0
msg_panic_reason      db ' reason  = ', 0
msg_panic_vector      db ' vector  = ', 0
msg_panic_error       db ' error   = ', 0
msg_panic_rip         db ' rip     = ', 0
msg_panic_cr2         db ' cr2     = ', 0
msg_panic_message     db ' message = ', 0
msg_newline           db 13, 10, 0
msg_trap_unexpected   db 'trap outside cpu_trap_expect', 0
msg_trap_not_kernel   db 'trap from a non ring 0 context', 0
msg_selftest_head     db 'K64-CPU-SELFTEST passed = ', 0
msg_selftest_skipped  db ' skipped = ', 0

; ------------------------------------------------------------------
; end of the data region: 4 KiB aligned, nothing may follow
; ------------------------------------------------------------------

align CPU_DATA_ALIGN
cpu_data_end:

if (cpu_data_start and (CPU_DATA_ALIGN - 1)) <> 0
        err 'the data region must start page aligned'
end if
if (cpu_data_end and (CPU_DATA_ALIGN - 1)) <> 0
        err 'the data region must end page aligned'
end if
if cpu_ist1_top - cpu_ist1_df <> CPU_IST_SIZE
        err 'IST1 must be 16 KiB'
end if
if cpu_ist2_nmi - cpu_ist1_df <> CPU_IST_SIZE
        err 'the IST stacks must follow each other'
end if
if (cpu_ist1_df and (CPU_PAGE_SIZE - 1)) <> 0
        err 'IST1 must be page aligned'
end if
if (cpu_panic_stack and (CPU_PAGE_SIZE - 1)) <> 0
        err 'the panic stack must be page aligned'
end if
if cpu_panic_stack < cpu_data_start
        err 'the panic stack must live inside the data region'
end if
if cpu_data_end <= cpu_panic_stack_top
        err 'every stack must live inside the data region'
end if
if (cpu_trap_frame_result and 7) <> 0
        err 'the trap result copy must be qword aligned'
end if
if (cpu_selftest_out and 7) <> 0
        err 'the self test output buffer must be qword aligned'
end if
if (cpu_selftest_expect and 7) <> 0
        err 'the self test expectation block must be qword aligned'
end if
if cpu_selftest_out - cpu_trap_frame_result <> CPU_FRAME_SIZE
        err 'the result and the self test buffer are one frame apart'
end if
if cpu_selftest_expect - cpu_selftest_out <> CPU_FRAME_SIZE
        err 'the expectation block must follow the self test buffer'
end if
if cpu_selftest_expect_error_mask - cpu_selftest_expect <> CPU_EXPECT_OFF_ERROR_MASK
        err 'self test CPU_EXPECT.error_mask offset'
end if
if cpu_selftest_expect_error_value - cpu_selftest_expect <> CPU_EXPECT_OFF_ERROR_VALUE
        err 'self test CPU_EXPECT.error_value offset'
end if
if cpu_selftest_expect_cr2_mask - cpu_selftest_expect <> CPU_EXPECT_OFF_CR2_MASK
        err 'self test CPU_EXPECT.cr2_mask offset'
end if
if cpu_selftest_expect_cr2_value - cpu_selftest_expect <> CPU_EXPECT_OFF_CR2_VALUE
        err 'self test CPU_EXPECT.cr2_value offset'
end if
if (cpu_trap_expect_rip and 7) <> 0
        err 'the trap state qwords must stay qword aligned'
end if
