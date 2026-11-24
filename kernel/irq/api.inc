;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; IRQ layer: private API, contract IRQ-v1 on top of K64-FOUNDATION-v1.
;;
;; Like the CPU and MM modules this file repeats every frozen value under
;; the IRQ_ prefix instead of including a shared header. The core contract
;; file compares this copy with the shared K64_* spelling at assembly time.
;; The module is included after cpu/cpu.asm and mm/mm.asm: it calls their
;; public entry points (vmm_map_mmio, cpu_irq_set_handler, cpu_panic, the
;; bounded console) but never touches their private state.
;;
;; Calling convention (Microsoft x64, integer registers only):
;;  - integer arguments in RCX, RDX, R8, R9;
;;  - 32 bytes of shadow space, RSP mod 16 = 0 before a call, no red zone;
;;  - RBX, RBP, RSI, RDI and R12..R15 are callee saved;
;;  - EAX returns a signed status, RDX the primary result;
;;  - DF = 0 on return, IF unchanged by a returning call.
;;
;; irq_init and irq_selftest enter with IF = 0. irq_selftest is the only
;; owner of STI in the whole kernel: it opens short bounded windows and
;; always leaves with the timer masked and IF = 0.
;;

IRQ_API_VERSION = 1

; ------------------------------------------------------------------
; status codes, EAX, signed, the shared foundation set
; ------------------------------------------------------------------

IRQ_OK              = 0
IRQ_EINVAL          = -1
IRQ_ENOMEM          = -2
IRQ_ENOENT          = -3
IRQ_EEXIST          = -4
IRQ_EPERM           = -5
IRQ_ENOTSUP         = -6
IRQ_ENOTREADY       = -7
IRQ_EALREADY        = -8
IRQ_EIO             = -9
IRQ_EOVERFLOW       = -10
IRQ_EFAULT          = -11
IRQ_EBUSY           = -12

; ------------------------------------------------------------------
; geometry and the private copies of the CPU frame facts this layer reads
; ------------------------------------------------------------------

IRQ_PAGE_SIZE       = 0x1000
IRQ_DATA_ALIGN      = 0x1000
IRQ_STACK_FRAME     = 128

IRQ_FRAME_VECTOR    = 128                 ; CPU_TRAP_FRAME.vector
IRQ_FRAME_RFLAGS    = 160                 ; CPU_TRAP_FRAME.rflags, the
                                          ; interrupted context: inside the
                                          ; STI window its IF bit is 1
IRQ_RFLAGS_IF       = 0x200

; bounded console caps, the private copies of the CPU printer contract
IRQ_PUTS_MAX        = 1024
IRQ_PUTHEX_DIGITS   = 16

; ------------------------------------------------------------------
; vectors and the LAPIC register window
;
; The LAPIC lives at its architectural physical address, mapped through
; vmm_map_mmio at the frozen MMIO VA (PCD|RW|NX, one 4 KiB page).
; ------------------------------------------------------------------

IRQ_VEC_TIMER       = 0x20
IRQ_VEC_SPURIOUS    = 0xFF

; IRQ-v2: the IOAPIC delivers the PS/2 keyboard on pin 1 and the PS/2
; mouse on pin 12 through these frozen vectors; the input module owns
; the handlers, irq_dispatch only routes and sends the EOI.
IRQ_VEC_KEYBOARD    = 0x21
IRQ_VEC_MOUSE       = 0x2C

IRQ_LAPIC_PA        = 0xFEE00000
IRQ_LAPIC_VA        = 0xFFFFC00002000000

IRQ_LAPIC_ID        = 0x020
IRQ_LAPIC_VER       = 0x030
IRQ_LAPIC_TPR       = 0x080
IRQ_LAPIC_EOI       = 0x0B0
IRQ_LAPIC_SVR       = 0x0F0
IRQ_LAPIC_ISR       = 0x100               ; ISR0..ISR7, 0x100..0x170
IRQ_LAPIC_ISR_LAST  = 0x170
IRQ_LAPIC_ESR       = 0x280
IRQ_LAPIC_LVT_TIMER = 0x320
IRQ_LAPIC_LVT_LINT0 = 0x350
IRQ_LAPIC_LVT_LINT1 = 0x360
IRQ_LAPIC_LVT_ERROR = 0x370
IRQ_LAPIC_INIT_COUNT  = 0x380
IRQ_LAPIC_CUR_COUNT   = 0x390
IRQ_LAPIC_DIVIDE      = 0x3E0

IRQ_ISR_REGS        = 8                   ; eight 32 bit in-service registers
IRQ_ISR_BITS        = 32

; SVR: bit 8 enables the APIC, the low byte is the spurious vector
IRQ_SVR_ENABLE      = 0x100
IRQ_SVR_VALUE       = 0x1FF

; LVT timer: bit 16 masks, bits 17..18 select the mode, low byte the vector
IRQ_LVT_MASK        = 0x10000
IRQ_LVT_ONESHOT     = 0x00000
IRQ_LVT_PERIODIC    = 0x20000

; divide configuration register: 16 is encoded as 0x3 (bits 0,1 set, bit 3 clear)
IRQ_DIVIDE_16       = 0x3

; IA32_APIC_BASE MSR: bit 11 is the global enable, bits 12..35 the base
IRQ_APIC_BASE_MSR     = 0x1B
IRQ_APIC_BASE_ENABLE  = 0x800
IRQ_APIC_BASE_MASK    = 0xFFFFFF000
IRQ_APIC_BASE_PA      = 0xFEE00000

; ------------------------------------------------------------------
; the 8259 pair: full remap then total mask, verified by read back
; ------------------------------------------------------------------

IRQ_PIC1_CMD        = 0x20
IRQ_PIC1_DATA       = 0x21
IRQ_PIC2_CMD        = 0xA0
IRQ_PIC2_DATA       = 0xA1
IRQ_PIC_ICW1        = 0x11                ; edge, cascade, ICW4 needed
IRQ_PIC_ICW2_MASTER = 0x20
IRQ_PIC_ICW2_SLAVE  = 0x28
IRQ_PIC_ICW3_MASTER = 0x04                ; slave on IR2
IRQ_PIC_ICW3_SLAVE  = 0x02                ; cascade identity 2
IRQ_PIC_ICW4        = 0x01                ; 8086 mode
IRQ_PIC_OCW1_MASK   = 0xFF                ; every line masked
IRQ_PIC_IO_DELAY    = 0x80                ; posted-write delay port

; ------------------------------------------------------------------
; PIT channel 2, the calibration time base
;
; Mode 0 (interrupt on terminal count), gate from port 0x61 bit 0,
; speaker off (bit 1 clear), OUT2 on bit 5 marks the end of the window.
; ------------------------------------------------------------------

IRQ_PIT_CH2_DATA    = 0x42
IRQ_PIT_CMD         = 0x43
IRQ_PIT_CTRL        = 0x61
IRQ_PIT_MODE0_CH2   = 0xB0                ; ch2, lo/hi access, mode 0, binary
IRQ_PIT_GATE        = 0x01
IRQ_PIT_SPEAKER     = 0x02
IRQ_PIT_OUT2        = 0x20
IRQ_PIT_COUNT       = 0xFFFF              ; about 54.9 ms at 1.193182 MHz
IRQ_PIT_HZ          = 1193182

; ------------------------------------------------------------------
; calibration and self test bounds, every loop in this module is bounded
;
; Under TCG the LAPIC timer bus frequency is a fixed 1 GHz, so with
; divide = 16 the counter runs at 62.5 MHz. The PIT mode 0 quirk (the
; counter effectively runs initial_count + 1 cycles) shifts the measured
; window by one part in 65536, far below the 5 percent sanity band, so it
; is deliberately ignored.
; ------------------------------------------------------------------

IRQ_CALIB_ROUNDS    = 3                   ; three samples, median wins
IRQ_CALIB_SPIN      = 0x10000000          ; OUT2 poll budget per round
IRQ_CALIB_NOMINAL   = 62500000            ; 1 GHz / 16
IRQ_CALIB_TOL       = 3125000             ; 5 percent of the nominal

; The frozen tick contract is 10 ms (100 Hz). The initial count is NOT a
; hardcoded constant: irq_selftest derives it from the measured counter
; frequency, counts_per_10ms = irq_calib_hz / IRQ_TICK_HZ. The nominal
; value below is the sanity anchor of that derivation (divide = 16 gives
; 62.5 MHz, hence 625000 counts per tick); the runtime always uses the
; calibration result, so a frequency inside the 5 percent band still
; yields a 10 ms tick.
IRQ_TICK_HZ         = 100                 ; frozen: one tick every 10 ms
IRQ_TIMER_COUNT_NOMINAL = IRQ_CALIB_NOMINAL / IRQ_TICK_HZ

; Wait budgets, sized for the 10 ms tick: the longest bounded wait is
; the periodic leg (20 ticks = 200 ms), the observation windows must
; span at least two full tick periods (20 ms) to prove a masked or
; blocked timer stays silent. Both stay finite constants.
IRQ_WAIT_SPIN       = 0x70000000          ; per-leg wait budget
IRQ_ST_WINDOW       = 0x1000000           ; masked/blocked window spins
IRQ_ST_PERIODIC_N   = 20
IRQ_ST_FINAL_N      = 10

; self test leg numbers, the failure mask is a qword
IRQ_ST_BIT_PIC      = 0
IRQ_ST_BIT_SVR      = 1
IRQ_ST_BIT_ONESHOT  = 2
IRQ_ST_BIT_PERIODIC = 3
IRQ_ST_BIT_MASK     = 4
IRQ_ST_BIT_NOEOI    = 5
IRQ_ST_BIT_IF       = 6
IRQ_ST_BIT_CALIB    = 7
IRQ_ST_BIT_FINAL    = 8
IRQ_ST_ALL          = (1 shl (IRQ_ST_BIT_FINAL + 1)) - 1

; ------------------------------------------------------------------
; the static data region layout, offsets from irq_data_start
;
; irq_tick_count and irq_test_ticks are both incremented by the timer
; path of irq_dispatch: the first is the lifetime count, the second is
; the per-leg counter the self test resets. The evidence qwords are
; written by the handler itself, the skip-EOI switch is the leg 5 test
; hook and must default to zero: the normal path always sends the EOI.
; ------------------------------------------------------------------

IRQ_DATA_OFF_STATE       = 0
IRQ_DATA_OFF_TICK_COUNT  = 8
IRQ_DATA_OFF_SPURIOUS    = 16
IRQ_DATA_OFF_TEST_TICKS  = 24
IRQ_DATA_OFF_TEST_MASK   = 32
IRQ_DATA_OFF_ISR_SEEN    = 40
IRQ_DATA_OFF_ISR_AFTER   = 48
IRQ_DATA_OFF_IF_VALUE    = 56
IRQ_DATA_OFF_SKIP_EOI    = 64
IRQ_DATA_OFF_CALIB_HZ    = 72
IRQ_DATA_OFF_CALIB_S0    = 80
IRQ_DATA_OFF_CALIB_S1    = 88
IRQ_DATA_OFF_CALIB_S2    = 96
IRQ_DATA_OFF_FRAME_IF    = 104
IRQ_DATA_OFF_MESSAGES    = 112

virtual at 0
irq_data_layout:
d_state:
        dq      ?
d_tick_count:
        dq      ?
d_spurious:
        dq      ?
d_test_ticks:
        dq      ?
d_test_mask:
        dq      ?
d_isr_seen:
        dq      ?
d_isr_after:
        dq      ?
d_if_value:
        dq      ?
d_skip_eoi:
        dq      ?
d_calib_hz:
        dq      ?
d_calib_s0:
        dq      ?
d_calib_s1:
        dq      ?
d_calib_s2:
        dq      ?
d_frame_if:
        dq      ?
irq_data_layout_end:
end virtual

if d_state - irq_data_layout <> IRQ_DATA_OFF_STATE
        err 'IRQ data state offset'
end if
if d_tick_count - irq_data_layout <> IRQ_DATA_OFF_TICK_COUNT
        err 'IRQ data tick count offset'
end if
if d_spurious - irq_data_layout <> IRQ_DATA_OFF_SPURIOUS
        err 'IRQ data spurious count offset'
end if
if d_test_ticks - irq_data_layout <> IRQ_DATA_OFF_TEST_TICKS
        err 'IRQ data test tick offset'
end if
if d_test_mask - irq_data_layout <> IRQ_DATA_OFF_TEST_MASK
        err 'IRQ data selftest mask offset'
end if
if d_isr_seen - irq_data_layout <> IRQ_DATA_OFF_ISR_SEEN
        err 'IRQ data ISR-before-EOI evidence offset'
end if
if d_isr_after - irq_data_layout <> IRQ_DATA_OFF_ISR_AFTER
        err 'IRQ data ISR-after-EOI evidence offset'
end if
if d_if_value - irq_data_layout <> IRQ_DATA_OFF_IF_VALUE
        err 'IRQ data handler IF evidence offset'
end if
if d_skip_eoi - irq_data_layout <> IRQ_DATA_OFF_SKIP_EOI
        err 'IRQ data leg5 skip-EOI switch offset'
end if
if d_calib_hz - irq_data_layout <> IRQ_DATA_OFF_CALIB_HZ
        err 'IRQ data calibration result offset'
end if
if d_calib_s0 - irq_data_layout <> IRQ_DATA_OFF_CALIB_S0
        err 'IRQ data calibration sample 0 offset'
end if
if d_calib_s1 - irq_data_layout <> IRQ_DATA_OFF_CALIB_S1
        err 'IRQ data calibration sample 1 offset'
end if
if d_calib_s2 - irq_data_layout <> IRQ_DATA_OFF_CALIB_S2
        err 'IRQ data calibration sample 2 offset'
end if
if d_frame_if - irq_data_layout <> IRQ_DATA_OFF_FRAME_IF
        err 'IRQ data interrupted-frame IF evidence offset'
end if
if irq_data_layout_end - irq_data_layout <> IRQ_DATA_OFF_MESSAGES
        err 'the IRQ data qword block must stay 112 bytes'
end if

; ------------------------------------------------------------------
; self consistency of the frozen register facts
; ------------------------------------------------------------------

if IRQ_VEC_TIMER <> 0x20 | IRQ_VEC_SPURIOUS <> 0xFF
        err 'the IRQ vectors are frozen at 0x20 and 0xFF'
end if
if IRQ_VEC_KEYBOARD <> 0x21 | IRQ_VEC_MOUSE <> 0x2C
        err 'the input vectors are frozen at 0x21 and 0x2C'
end if
if IRQ_VEC_KEYBOARD <= IRQ_VEC_TIMER | IRQ_VEC_MOUSE <= IRQ_VEC_KEYBOARD | IRQ_VEC_MOUSE >= IRQ_VEC_SPURIOUS
        err 'the input vectors must stay ordered inside the device range'
end if
if IRQ_SVR_VALUE <> (IRQ_SVR_ENABLE or IRQ_VEC_SPURIOUS)
        err 'SVR must enable the APIC with the spurious vector 0xFF'
end if
if IRQ_LAPIC_ISR_LAST - IRQ_LAPIC_ISR <> (IRQ_ISR_REGS - 1) * 0x10
        err 'the ISR registers must be eight 16 byte spaced dwords'
end if
if IRQ_LAPIC_ID <> 0x20 | IRQ_LAPIC_VER <> 0x30 | IRQ_LAPIC_TPR <> 0x80
        err 'LAPIC ID/VER/TPR offsets'
end if
if IRQ_LAPIC_EOI <> 0xB0 | IRQ_LAPIC_SVR <> 0xF0 | IRQ_LAPIC_ESR <> 0x280
        err 'LAPIC EOI/SVR/ESR offsets'
end if
if IRQ_LAPIC_LVT_TIMER <> 0x320 | IRQ_LAPIC_LVT_LINT0 <> 0x350
        err 'LAPIC timer/LINT0 LVT offsets'
end if
if IRQ_LAPIC_LVT_LINT1 <> 0x360 | IRQ_LAPIC_LVT_ERROR <> 0x370
        err 'LAPIC LINT1/error LVT offsets'
end if
if IRQ_LAPIC_INIT_COUNT <> 0x380 | IRQ_LAPIC_CUR_COUNT <> 0x390 | IRQ_LAPIC_DIVIDE <> 0x3E0
        err 'LAPIC timer count/divide offsets'
end if
if IRQ_DIVIDE_16 <> 0x3
        err 'divide by 16 must be encoded as 0x3'
end if
if IRQ_LVT_MASK <> 0x10000 | IRQ_LVT_PERIODIC <> 0x20000 | IRQ_LVT_ONESHOT <> 0
        err 'LVT mask bit 16 and mode bits 17..18'
end if
if IRQ_APIC_BASE_PA <> IRQ_LAPIC_PA
        err 'the MSR base and the mapped page must be the same address'
end if
if IRQ_APIC_BASE_MASK and IRQ_APIC_BASE_PA <> IRQ_APIC_BASE_PA
        err 'the APIC base must fit the MSR base field'
end if
if IRQ_CALIB_NOMINAL * 16 <> 1000000000
        err 'the nominal counter frequency is 1 GHz / 16'
end if
if IRQ_CALIB_TOL * 20 <> IRQ_CALIB_NOMINAL
        err 'the sanity band is exactly 5 percent'
end if
if IRQ_CALIB_ROUNDS <> 3
        err 'calibration takes the median of three samples'
end if
if IRQ_TICK_HZ <> 100
        err 'the frozen tick contract is 10 ms, 100 Hz'
end if
if IRQ_TIMER_COUNT_NOMINAL <> 625000
        err 'the nominal 10 ms count is 62.5 MHz / 100'
end if
if IRQ_TIMER_COUNT_NOMINAL * IRQ_TICK_HZ <> IRQ_CALIB_NOMINAL
        err 'the tick count must derive from the counter frequency'
end if
if IRQ_PIT_COUNT <= 0
        err 'the calibration divisor must never be zero'
end if
if IRQ_ST_PERIODIC_N <> 20 | IRQ_ST_FINAL_N <> 10
        err 'the periodic legs count 20 and 10 ticks'
end if
if IRQ_FRAME_VECTOR <> 128 | IRQ_FRAME_RFLAGS <> 160 | IRQ_RFLAGS_IF <> 0x200
        err 'the private CPU frame copies drifted'
end if
if IRQ_PUTS_MAX <> 1024 | IRQ_PUTHEX_DIGITS <> 16
        err 'the private console caps drifted'
end if
if IRQ_ST_ALL <> 0x1FF
        err 'the self test mask covers nine legs'
end if

; ------------------------------------------------------------------
; register helper macros, the same full frame the MM module uses
; ------------------------------------------------------------------

macro irq_enter
        push    rbp
        push    rbx
        push    rsi
        push    rdi
        push    r12
        push    r13
        push    r14
        push    r15
        sub     rsp, 40
end macro

macro irq_leave
        add     rsp, 40
        pop     r15
        pop     r14
        pop     r13
        pop     r12
        pop     rdi
        pop     rsi
        pop     rbx
        pop     rbp
end macro
