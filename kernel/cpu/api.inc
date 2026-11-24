;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; CPU descriptors, traps and panic: private API, contract K64-FOUNDATION-v1.
;;
;; This module is deliberately standalone. It includes no other kernel header
;; and repeats every value the contract fixes under the CPU_ prefix, so the
;; boundary between this module and the rest of the kernel is explicit instead
;; of being an implicit shared file. Nothing here may be included twice.
;;
;; Calling convention (Microsoft x64, integer registers only):
;;  - integer arguments in RCX, RDX, R8, R9; no floating point, no vectors;
;;  - the caller provides 32 bytes of shadow space and leaves RSP 16 byte
;;    aligned before the call, so a function body starts with RSP mod 16 = 8;
;;  - no red zone is used, the 128 bytes below RSP belong to the callee;
;;  - RBX, RBP, RSI, RDI and R12..R15 are callee saved;
;;  - EAX returns a signed status, RDX returns the primary result;
;;  - DF is clear on return, IF is never changed by a returning call.
;;
;; Every exported function uses one uniform frame:
;;
;;        push    rbp
;;        mov     rbp, rsp
;;        sub     rsp, CPU_STACK_FRAME      ; 128 bytes, 32 shadow + locals
;;        ...
;;        leave
;;        ret
;;
;; 128 is a multiple of 16 and the entry RSP is 8 mod 16, so after the push
;; RSP mod 16 is 0 and stays 0 for the whole body: shadow space and alignment
;; are correct for every nested call without further bookkeeping. Locals live
;; at [rbp-8], [rbp-16] and so on inside the same 128 bytes. The body never
;; adjusts RSP; the only exception is the far return that reloads CS in
;; cpu_gdt_init, which pushes and pops 16 bytes in one straight line.
;;

CPU_API_VERSION = 1

; ------------------------------------------------------------------
; status codes, EAX, signed
; ------------------------------------------------------------------

CPU_OK              = 0
CPU_EINVAL          = -1
CPU_ENOMEM          = -2
CPU_ENOENT          = -3
CPU_EEXIST          = -4
CPU_EPERM           = -5
CPU_ENOTSUP         = -6
CPU_ENOTREADY       = -7
CPU_EALREADY        = -8
CPU_EIO             = -9
CPU_EOVERFLOW       = -10
CPU_EFAULT          = -11
CPU_EBUSY           = -12

; ------------------------------------------------------------------
; memory geometry fixed by the contract
; ------------------------------------------------------------------

CPU_PAGE_SIZE       = 0x1000
CPU_PAGE_MASK       = 0xFFFFFFFFFFFFF000
CPU_PHYS_LIMIT      = 0x100000000           ; exclusive, this stage
CPU_DATA_ALIGN      = 0x1000
CPU_STACK_ALIGN     = 16
CPU_IST_SIZE        = 0x4000                ; 16 KiB per IST stack
CPU_IST_COUNT       = 5                     ; ist1..ist4 plus panic stack
CPU_STACK_FRAME     = 128                   ; shadow space plus locals

; kernel addresses are the canonical high half, bits 63..48 all set
CPU_KERNEL_SHIFT    = 47
CPU_KERNEL_TOP      = 0x1FFFF

; ------------------------------------------------------------------
; INIT_PARAMS, 64 bytes, packed
; ------------------------------------------------------------------

CPU_INIT_VERSION        = 1
CPU_INIT_SIZE           = 64
CPU_INIT_OFF_VERSION    = 0
CPU_INIT_OFF_SIZE       = 4
CPU_INIT_OFF_BOOTINFO_VA   = 8
CPU_INIT_OFF_BOOTINFO_PHYS = 16
CPU_INIT_OFF_CR3_PHYS   = 24
CPU_INIT_OFF_STACK_TOP  = 32
CPU_INIT_OFF_FLAGS      = 40
CPU_INIT_OFF_RESERVED0  = 48
CPU_INIT_OFF_RESERVED1  = 56
CPU_INIT_FLAGS_VALUE    = 0
CPU_INIT_RESERVED_VALUE = 0

; ------------------------------------------------------------------
; selectors and the GDT, 40 bytes
; ------------------------------------------------------------------

CPU_SEL_NULL        = 0x00
CPU_SEL_CODE        = 0x08
CPU_SEL_DATA        = 0x10
CPU_SEL_TSS         = 0x18

CPU_GDT_ALIGN       = 16
CPU_GDT_SIZE        = 40
CPU_GDT_OFF_NULL    = 0
CPU_GDT_OFF_CODE    = 8
CPU_GDT_OFF_DATA    = 16
CPU_GDT_OFF_TSS     = 24
CPU_GDT_NULL_VALUE  = 0x0000000000000000
CPU_GDT_CODE_VALUE  = 0x00AF9A000000FFFF     ; base 0, limit 4 GiB, L=1, DPL0
CPU_GDT_DATA_VALUE  = 0x00CF92000000FFFF     ; base 0, limit 4 GiB, DPL0
; the CPU sets the accessed bit (access byte bit 0, qword bit 40) when the
; segment is loaded: retfq for CS, mov for DS/ES/SS. Runtime compares must
; mask it off; the image is built with it clear.
CPU_GDT_DESC_ACCESSED = 1 shl 40
CPU_GDT_DESC_RUNTIME_MASK = not CPU_GDT_DESC_ACCESSED
CPU_GDTR_SIZE       = 10                    ; packed limit(2) + base(8)
CPU_GDTR_LIMIT      = CPU_GDT_SIZE - 1

; ------------------------------------------------------------------
; TSS, 104 bytes, packed
; ------------------------------------------------------------------

CPU_TSS_ALIGN       = 16
CPU_TSS_SIZE        = 104
CPU_TSS_LIMIT       = CPU_TSS_SIZE - 1      ; 103
CPU_TSS_OFF_RSP0    = 4
CPU_TSS_OFF_RSP1    = 12
CPU_TSS_OFF_RSP2    = 20
CPU_TSS_OFF_IST1    = 36
CPU_TSS_OFF_IST2    = 44
CPU_TSS_OFF_IST3    = 52
CPU_TSS_OFF_IST4    = 60
CPU_TSS_OFF_IST5    = 68
CPU_TSS_OFF_IST6    = 76
CPU_TSS_OFF_IST7    = 84
CPU_TSS_OFF_IOMAP   = 102
CPU_TSS_IOMAP_NONE  = 104                   ; past the limit: no bitmap
CPU_TSS_DESC_TYPE   = 0x89                  ; present, 64-bit available TSS, before LTR
CPU_TSS_DESC_TYPE_BUSY = 0x8B               ; what LTR sets: the same type, busy bit on
CPU_TSS_DESC_G      = 0                     ; byte granularity, limit fits 16 bits
CPU_TSS_DESC_SIZE   = 16

; ------------------------------------------------------------------
; IDT, 256 gates of 16 bytes
; ------------------------------------------------------------------

CPU_IDT_VECTORS     = 256
CPU_IDT_GATE_SIZE   = 16
CPU_IDT_SIZE        = CPU_IDT_VECTORS * CPU_IDT_GATE_SIZE
CPU_IDT_LIMIT       = CPU_IDT_SIZE - 1
CPU_IDT_ALIGN       = 16
CPU_IDT_TYPE_ATTR   = 0x8E                  ; present, DPL0, 64-bit interrupt gate
CPU_IDT_DPL         = 0
CPU_IDT_IST_MAX     = 7
CPU_IDTR_SIZE       = 10
CPU_TRAP_ERROR_COUNT = 10                   ; vectors that push an error code

; ------------------------------------------------------------------
; vectors used by name, and the IST assignment of the whole table
; ------------------------------------------------------------------

CPU_VEC_DE          = 0
CPU_VEC_NMI         = 2
CPU_VEC_BP          = 3
CPU_VEC_UD          = 6
CPU_VEC_DF          = 8
CPU_VEC_MC          = 18
CPU_VEC_TEST        = 0xF0
CPU_VEC_IST_TEST    = 0xF1

; the asynchronous window the IRQ layer may own: vectors below this stay
; architectural, the dispatcher never hands them to a registered handler
CPU_IRQ_VECTOR_MIN  = 0x20

CPU_IDT_IST_DF      = 1
CPU_IDT_IST_NMI     = 2
CPU_IDT_IST_MC      = 3
CPU_IDT_IST_TEST    = 4

; ------------------------------------------------------------------
; the unified trap frame, 184 bytes, packed
;
; The stub pushes the dummy error code (when the vector has none), the
; vector and all 15 general purpose registers, then CR2. The hardware frame
; (RIP, CS, RFLAGS, and RSP/SS only on a privilege change) follows. The order
; of the pushes is what makes CR2 land at offset 0 and R15..RAX at 8..120.
; ------------------------------------------------------------------

CPU_FRAME_SIZE      = 184
CPU_FRAME_CR2       = 0
CPU_FRAME_R15       = 8
CPU_FRAME_R14       = 16
CPU_FRAME_R13       = 24
CPU_FRAME_R12       = 32
CPU_FRAME_R11       = 40
CPU_FRAME_R10       = 48
CPU_FRAME_R9        = 56
CPU_FRAME_R8        = 64
CPU_FRAME_RDI       = 72
CPU_FRAME_RSI       = 80
CPU_FRAME_RBP       = 88
CPU_FRAME_RBX       = 96
CPU_FRAME_RDX       = 104
CPU_FRAME_RCX       = 112
CPU_FRAME_RAX       = 120
CPU_FRAME_VECTOR    = 128
CPU_FRAME_ERROR     = 136
CPU_FRAME_RIP       = 144
CPU_FRAME_CS        = 152
CPU_FRAME_RFLAGS    = 160
CPU_FRAME_RSP       = 168
CPU_FRAME_SS        = 176
CPU_FRAME_GPR_COUNT = 15

; ------------------------------------------------------------------
; CPU_EXPECT, 32 bytes, packed: what an armed trap must look like
;
; cpu_trap_expect takes a pointer to one of these, or zero. A mask of zero
; does not constrain its field at all, so the zero pointer means "accept
; whatever arrives": both masks are zero and both comparisons trivially hold.
; The dispatcher applies the mask with AND and compares the result with the
; value, for the hardware error code and for CR2 independently.
; ------------------------------------------------------------------

CPU_EXPECT_SIZE         = 32
CPU_EXPECT_OFF_ERROR_MASK  = 0
CPU_EXPECT_OFF_ERROR_VALUE = 8
CPU_EXPECT_OFF_CR2_MASK    = 16
CPU_EXPECT_OFF_CR2_VALUE   = 24

; ------------------------------------------------------------------
; the trap API, contract K64-FOUNDATION-v1
;
;   cpu_trap_expect(rcx = vector, rdx = expected saved RIP,
;                   r8 = resume RIP, r9 = CPU_EXPECT * or 0)
;     only the six vectors 0, 3, 6, 14, 0xF0 and 0xF1 are accepted; both
;     addresses must be canonical in the kernel half; an already armed trap
;     or an unconsumed result makes the call fail with EBUSY.
;
;   the dispatcher compares the frame vector, the saved RIP, the masked
;   error code and the masked CR2 with the armed values. Only a full match
;   is accepted: the complete 184 byte frame is copied to internal storage,
;   the address of the handler frame is remembered, the frame RIP the stub
;   will return to is replaced by the resume RIP, the arming is consumed and
;   the result is marked ready. Anything else panics.
;
;   cpu_trap_finish(rcx = 184 byte buffer or 0)
;     with a ready result the frame is copied to the buffer (or dropped when
;     the buffer is zero), the result is consumed, EAX = 0 and RDX = the
;     handler frame address. With an armed trap that never fired the arming
;     is cleared and EAX = EFAULT. With neither, EAX = ENOTREADY. A result
;     can be consumed exactly once.
; ------------------------------------------------------------------

; ------------------------------------------------------------------
; the asynchronous IRQ hook, contract IRQ-v1
;
;   cpu_irq_set_handler(rcx = handler VA or 0)
;     ring 0 and IF = 0 only, otherwise EPERM. A non zero handler must be
;     canonical in the kernel half, with the same check the trap API applies
;     to its addresses; EINVAL otherwise. Registering while a non zero
;     handler is already registered is EALREADY. Zero unregisters, and a
;     later call may register again.
;
;   a trap that matches no armed expectation and carries a vector of
;   CPU_IRQ_VECTOR_MIN or above goes to the registered handler with
;   RCX = the stub frame, under the MS x64 ABI; the handler returns through
;   the stub's normal restore and iretq. The EOI belongs to the IRQ layer,
;   the dispatcher never sends one. Without a registered handler such a
;   trap still panics, exactly like any other unexpected vector.
; ------------------------------------------------------------------

; ------------------------------------------------------------------
; control registers, MSRs and flags this module reads or sets
; ------------------------------------------------------------------

CPU_RFLAGS_IF       = 0x200
CPU_CR0_PG          = 0x80000000
CPU_CR4_PAE         = 0x00000020
CPU_CR4_CET         = 0x00800000
CPU_EFER_MSR        = 0xC0000080
CPU_EFER_LMA        = 0x00000400

; ------------------------------------------------------------------
; bounded diagnostics over COM1
; ------------------------------------------------------------------

CPU_COM1_PORT       = 0x3F8
CPU_COM1_TX_SPIN    = 0x10000               ; THRE poll budget
CPU_COM1_RX_SPIN    = 0x10000               ; loopback probe budget
CPU_PUTS_MAX        = 1024                  ; hard cap of one puts call
CPU_PUTHEX_DIGITS   = 16
CPU_PUTHEX_MAX      = 16
CPU_CHAR_CR         = 13
CPU_CHAR_LF         = 10

; ------------------------------------------------------------------
; cpu_selftest leg numbers, the bitmask it returns in RDX and R8
; ------------------------------------------------------------------

CPU_SELFTEST_BIT_GDT        = 0
CPU_SELFTEST_BIT_TSS        = 1
CPU_SELFTEST_BIT_IDT        = 2
CPU_SELFTEST_BIT_TR         = 3
CPU_SELFTEST_BIT_VEC_F0     = 4
CPU_SELFTEST_BIT_VEC_F1     = 5
CPU_SELFTEST_BIT_VEC_BP     = 6
CPU_SELFTEST_BIT_VEC_DE     = 7
CPU_SELFTEST_BIT_VEC_UD     = 8
CPU_SELFTEST_BIT_VEC_PF     = 9
CPU_SELFTEST_BIT_STATE      = 10              ; ENOTREADY, EBUSY, EFAULT
CPU_SELFTEST_MANDATORY      = (1 shl (CPU_SELFTEST_BIT_VEC_UD + 1)) - 1
CPU_SELFTEST_ALL            = (1 shl (CPU_SELFTEST_BIT_STATE + 1)) - 1

; ------------------------------------------------------------------
; assembly time classification shared by the stub generator and the static
; IDT: an error code vector pushes no dummy, four vectors get an IST.
; ------------------------------------------------------------------

macro cpu_classify_vector v
        cpu_vec_is_error = 0
        if (v = 8) | (v = 10) | (v = 11) | (v = 12) | (v = 13) | (v = 14) | (v = 17) | (v = 21) | (v = 29) | (v = 30)
                cpu_vec_is_error = 1
        end if
        cpu_vec_ist = 0
        if v = CPU_VEC_DF
                cpu_vec_ist = CPU_IDT_IST_DF
        end if
        if v = CPU_VEC_NMI
                cpu_vec_ist = CPU_IDT_IST_NMI
        end if
        if v = CPU_VEC_MC
                cpu_vec_ist = CPU_IDT_IST_MC
        end if
        if v = CPU_VEC_IST_TEST
                cpu_vec_ist = CPU_IDT_IST_TEST
        end if
        if cpu_vec_ist > CPU_IDT_IST_MAX
                err 'IST index outside the TSS'
        end if
end macro

; ------------------------------------------------------------------
; packed structure assertions
;
; The constants above are what the code uses; the virtual blocks below are an
; independent description of the same layouts. Any drift between the two is a
; build failure, not a silent misalignment.
; ------------------------------------------------------------------

virtual at 0
cpu_init_params:
p_version:
        dd      ?
p_size:
        dd      ?
p_bootinfo_va:
        dq      ?
p_bootinfo_phys:
        dq      ?
p_cr3_phys:
        dq      ?
p_stack_top:
        dq      ?
p_flags:
        dq      ?
p_reserved0:
        dq      ?
p_reserved1:
        dq      ?
cpu_init_params_end:
end virtual

if cpu_init_params_end - cpu_init_params <> CPU_INIT_SIZE
        err 'INIT_PARAMS must stay 64 bytes'
end if
if p_version - cpu_init_params <> CPU_INIT_OFF_VERSION
        err 'INIT_PARAMS.version offset'
end if
if p_size - cpu_init_params <> CPU_INIT_OFF_SIZE
        err 'INIT_PARAMS.size offset'
end if
if p_bootinfo_va - cpu_init_params <> CPU_INIT_OFF_BOOTINFO_VA
        err 'INIT_PARAMS.bootinfo_va offset'
end if
if p_bootinfo_phys - cpu_init_params <> CPU_INIT_OFF_BOOTINFO_PHYS
        err 'INIT_PARAMS.bootinfo_phys offset'
end if
if p_cr3_phys - cpu_init_params <> CPU_INIT_OFF_CR3_PHYS
        err 'INIT_PARAMS.cr3_phys offset'
end if
if p_stack_top - cpu_init_params <> CPU_INIT_OFF_STACK_TOP
        err 'INIT_PARAMS.stack_top_va offset'
end if
if p_flags - cpu_init_params <> CPU_INIT_OFF_FLAGS
        err 'INIT_PARAMS.flags offset'
end if
if p_reserved0 - cpu_init_params <> CPU_INIT_OFF_RESERVED0
        err 'INIT_PARAMS.reserved0 offset'
end if
if p_reserved1 - cpu_init_params <> CPU_INIT_OFF_RESERVED1
        err 'INIT_PARAMS.reserved1 offset'
end if

virtual at 0
cpu_frame_layout:
f_cr2:
        dq      ?
f_r15:
        dq      ?
f_r14:
        dq      ?
f_r13:
        dq      ?
f_r12:
        dq      ?
f_r11:
        dq      ?
f_r10:
        dq      ?
f_r9:
        dq      ?
f_r8:
        dq      ?
f_rdi:
        dq      ?
f_rsi:
        dq      ?
f_rbp:
        dq      ?
f_rbx:
        dq      ?
f_rdx:
        dq      ?
f_rcx:
        dq      ?
f_rax:
        dq      ?
f_vector:
        dq      ?
f_error:
        dq      ?
f_rip:
        dq      ?
f_cs:
        dq      ?
f_rflags:
        dq      ?
f_rsp:
        dq      ?
f_ss:
        dq      ?
cpu_frame_layout_end:
end virtual

if cpu_frame_layout_end - cpu_frame_layout <> CPU_FRAME_SIZE
        err 'trap frame must stay 184 bytes'
end if
if f_cr2 - cpu_frame_layout <> CPU_FRAME_CR2
        err 'trap frame cr2 offset'
end if
if f_r15 - cpu_frame_layout <> CPU_FRAME_R15
        err 'trap frame r15 offset'
end if
if f_r14 - cpu_frame_layout <> CPU_FRAME_R14
        err 'trap frame r14 offset'
end if
if f_r13 - cpu_frame_layout <> CPU_FRAME_R13
        err 'trap frame r13 offset'
end if
if f_r12 - cpu_frame_layout <> CPU_FRAME_R12
        err 'trap frame r12 offset'
end if
if f_r11 - cpu_frame_layout <> CPU_FRAME_R11
        err 'trap frame r11 offset'
end if
if f_r10 - cpu_frame_layout <> CPU_FRAME_R10
        err 'trap frame r10 offset'
end if
if f_r9 - cpu_frame_layout <> CPU_FRAME_R9
        err 'trap frame r9 offset'
end if
if f_r8 - cpu_frame_layout <> CPU_FRAME_R8
        err 'trap frame r8 offset'
end if
if f_rdi - cpu_frame_layout <> CPU_FRAME_RDI
        err 'trap frame rdi offset'
end if
if f_rsi - cpu_frame_layout <> CPU_FRAME_RSI
        err 'trap frame rsi offset'
end if
if f_rbp - cpu_frame_layout <> CPU_FRAME_RBP
        err 'trap frame rbp offset'
end if
if f_rbx - cpu_frame_layout <> CPU_FRAME_RBX
        err 'trap frame rbx offset'
end if
if f_rdx - cpu_frame_layout <> CPU_FRAME_RDX
        err 'trap frame rdx offset'
end if
if f_rcx - cpu_frame_layout <> CPU_FRAME_RCX
        err 'trap frame rcx offset'
end if
if f_rax - cpu_frame_layout <> CPU_FRAME_RAX
        err 'trap frame rax offset'
end if
if f_vector - cpu_frame_layout <> CPU_FRAME_VECTOR
        err 'trap frame vector offset'
end if
if f_error - cpu_frame_layout <> CPU_FRAME_ERROR
        err 'trap frame error offset'
end if
if f_rip - cpu_frame_layout <> CPU_FRAME_RIP
        err 'trap frame rip offset'
end if
if f_cs - cpu_frame_layout <> CPU_FRAME_CS
        err 'trap frame cs offset'
end if
if f_rflags - cpu_frame_layout <> CPU_FRAME_RFLAGS
        err 'trap frame rflags offset'
end if
if f_rsp - cpu_frame_layout <> CPU_FRAME_RSP
        err 'trap frame rsp offset'
end if
if f_ss - cpu_frame_layout <> CPU_FRAME_SS
        err 'trap frame ss offset'
end if
if (CPU_FRAME_VECTOR - CPU_FRAME_CR2) <> (CPU_FRAME_GPR_COUNT + 1) * 8
        err 'trap frame register block does not end at the vector slot'
end if

virtual at 0
cpu_expect_layout:
e_error_mask:
        dq      ?
e_error_value:
        dq      ?
e_cr2_mask:
        dq      ?
e_cr2_value:
        dq      ?
cpu_expect_layout_end:
end virtual

if cpu_expect_layout_end - cpu_expect_layout <> CPU_EXPECT_SIZE
        err 'CPU_EXPECT must stay 32 bytes'
end if
if e_error_mask - cpu_expect_layout <> CPU_EXPECT_OFF_ERROR_MASK
        err 'CPU_EXPECT.error_mask offset'
end if
if e_error_value - cpu_expect_layout <> CPU_EXPECT_OFF_ERROR_VALUE
        err 'CPU_EXPECT.error_value offset'
end if
if e_cr2_mask - cpu_expect_layout <> CPU_EXPECT_OFF_CR2_MASK
        err 'CPU_EXPECT.cr2_mask offset'
end if
if e_cr2_value - cpu_expect_layout <> CPU_EXPECT_OFF_CR2_VALUE
        err 'CPU_EXPECT.cr2_value offset'
end if
if CPU_EXPECT_SIZE <> 4 * 8
        err 'CPU_EXPECT is four qwords, not a frame'
end if
if CPU_EXPECT_OFF_ERROR_VALUE - CPU_EXPECT_OFF_ERROR_MASK <> 8
        err 'CPU_EXPECT fields must be qwords'
end if
if CPU_EXPECT_OFF_CR2_MASK - CPU_EXPECT_OFF_ERROR_VALUE <> 8
        err 'CPU_EXPECT error and cr2 pairs must be adjacent qwords'
end if
if CPU_EXPECT_OFF_CR2_VALUE - CPU_EXPECT_OFF_CR2_MASK <> 8
        err 'CPU_EXPECT cr2 pair must be two qwords'
end if

if CPU_GDT_OFF_TSS <> CPU_SEL_TSS
        err 'TSS selector must be the GDT slot offset'
end if
if CPU_GDT_OFF_CODE <> CPU_SEL_CODE
        err 'code selector must be the GDT slot offset'
end if
if CPU_GDT_OFF_DATA <> CPU_SEL_DATA
        err 'data selector must be the GDT slot offset'
end if
if CPU_GDT_OFF_TSS + CPU_TSS_DESC_SIZE <> CPU_GDT_SIZE
        err 'GDT must end right after the TSS descriptor'
end if
if (CPU_GDT_CODE_VALUE and CPU_GDT_DESC_ACCESSED) <> 0
        err 'the code descriptor is built with the accessed bit clear'
end if
if (CPU_GDT_DATA_VALUE and CPU_GDT_DESC_ACCESSED) <> 0
        err 'the data descriptor is built with the accessed bit clear'
end if
if CPU_GDTR_SIZE <> 10
        err 'GDTR must be the packed 10 byte form'
end if
if CPU_IDTR_SIZE <> 10
        err 'IDTR must be the packed 10 byte form'
end if
if CPU_TSS_IOMAP_NONE <= CPU_TSS_LIMIT
        err 'iomap base must be past the TSS limit'
end if
if CPU_TSS_DESC_TYPE_BUSY <> (CPU_TSS_DESC_TYPE or 2)
        err 'LTR only sets the busy bit of the TSS descriptor type'
end if
if (CPU_TSS_DESC_TYPE and 0x0F) <> 9
        err 'the TSS descriptor type must be 64-bit TSS'
end if
if CPU_IST_COUNT <> 5
        err 'stage 2 expects five 16 KiB stacks'
end if
if CPU_IRQ_VECTOR_MIN <> 0x20
        err 'the IRQ window must start at vector 0x20'
end if
if CPU_IRQ_VECTOR_MIN >= CPU_IDT_VECTORS
        err 'the IRQ window must fit inside the IDT'
end if
