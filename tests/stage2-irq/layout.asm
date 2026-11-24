;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Layout probe for the stage 2 IRQ module, task 70, contract IRQ-v1,
;; extended by task 89 for the IRQ-v2 dispatch branches.
;;
;; This file is a test harness, not part of the kernel. It assembles
;; kernel/irq/irq.asm exactly as the kernel will include it, behind the
;; two modules it consumes (cpu.asm for the trap frame, the handler hook
;; and the console, mm.asm for vmm_map_mmio), appends a manifest of label
;; addresses, and lets tests/stage2-irq/check.py verify the emitted bytes.
;; Nothing here is executed and no machine is started.
;;
;; IRQ-v2: irq_dispatch now calls input_keyboard_irq / input_mouse_irq.
;; This gate isolates the IRQ module, so the two input leaves are ret
;; stubs here; their real bodies are gated by tests/stage2-input. The
;; stub addresses join the manifest so check.py can prove the dispatch
;; branches call exactly them with RCX = frame.
;;
;; IRQ_PROBE_ORG selects the link base; check.py assembles the probe twice,
;; once at 0 for byte offsets and once at the kernel base to prove that the
;; module links high with the same internal layout.
;;
;; The manifest sits after irq.asm: the module's own end assertion runs at
;; its include site, and the data block is still the last module byte. The
;; manifest is a fixed list of qwords in the order check.py expects; the
;; magic and the entry count are checked before anything else is read.
;;

use64

if defined IRQ_PROBE_ORG
        org IRQ_PROBE_ORG
else
        org 0
end if

include '../../kernel/cpu/cpu.asm'

MM_HOSTED = 1
include '../../kernel/mm/mm.asm'

; IRQ-v2 input leaf stubs: the dispatch branches must call exactly these
input_keyboard_irq:
        ret
input_mouse_irq:
        ret

include '../../kernel/irq/irq.asm'

; ------------------------------------------------------------------
; manifest, fixed order, read by check.py
; ------------------------------------------------------------------

align 8
irq_probe:
        dq      'K64IRQP1'                  ; magic
        dq      1                           ; manifest version
        dq      irq_code_start
        dq      irq_code_end
        dq      irq_data_start
        dq      irq_data_end
        dq      irq_init
        dq      irq_lapic_bringup
        dq      irq_calibrate
        dq      irq_dispatch
        dq      irq_sti_wait
        dq      irq_selftest
        dq      irq_state
        dq      irq_tick_count
        dq      irq_spurious_count
        dq      irq_test_ticks
        dq      irq_test_selftest_mask
        dq      irq_test_isr_seen
        dq      irq_test_isr_after
        dq      irq_test_if_value
        dq      irq_test_skip_eoi
        dq      irq_calib_hz
        dq      irq_calib_s0
        dq      irq_calib_s1
        dq      irq_calib_s2
        dq      irq_test_frame_if
        dq      msg_irq_unknown
        dq      msg_irq_selftest
        dq      IRQ_VEC_TIMER
        dq      IRQ_VEC_SPURIOUS
        dq      IRQ_SVR_VALUE
        dq      IRQ_LAPIC_PA
        dq      IRQ_LAPIC_VA
        dq      IRQ_LVT_MASK
        dq      IRQ_LVT_PERIODIC
        dq      IRQ_DIVIDE_16
        dq      IRQ_TICK_HZ
        dq      IRQ_TIMER_COUNT_NOMINAL
        dq      IRQ_ST_PERIODIC_N
        dq      IRQ_ST_FINAL_N
        dq      IRQ_CALIB_ROUNDS
        dq      IRQ_CALIB_NOMINAL
        dq      IRQ_CALIB_TOL
        dq      IRQ_PIT_COUNT
        dq      IRQ_PIT_HZ
        dq      IRQ_WAIT_SPIN
        dq      IRQ_ST_WINDOW
        dq      IRQ_CALIB_SPIN
        dq      vmm_map_mmio
        dq      cpu_irq_set_handler
        dq      cpu_panic
        dq      cpu_halt_forever
        dq      cpu_com1_puts
        dq      cpu_com1_puthex
        dq      IRQ_DATA_OFF_TEST_TICKS
        dq      IRQ_DATA_OFF_TEST_MASK
        dq      IRQ_DATA_OFF_SKIP_EOI
        dq      IRQ_DATA_OFF_IF_VALUE
        dq      IRQ_DATA_OFF_FRAME_IF
        dq      IRQ_DATA_OFF_ISR_SEEN
        dq      IRQ_DATA_OFF_ISR_AFTER
        dq      IRQ_DATA_OFF_MESSAGES
        dq      input_keyboard_irq
        dq      input_mouse_irq
        dq      IRQ_VEC_KEYBOARD
        dq      IRQ_VEC_MOUSE
irq_probe_end:

if (irq_probe_end - irq_probe) <> 66 * 8
        err 'the manifest must keep its fixed entry count'
end if
