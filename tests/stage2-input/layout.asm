;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Layout probe for the stage 2 input module, task 89, contract IRQ-v2.
;;
;; This file is a test harness, not part of the kernel. It assembles
;; kernel/input/input.asm exactly as the kernel will include it: behind
;; cpu.asm (trap frame, console, panic), mm.asm (vmm_map_mmio) and
;; irq.asm (irq_sti_wait, the dispatch branches, the public evidence
;; labels). Unlike the stage2-irq gate, the input leaves here are the
;; REAL handlers: irq_dispatch must call them, and their bodies are
;; decoded by tests/stage2-input/check.py. Nothing here is executed and
;; no machine is started.
;;
;; INPUT_PROBE_ORG selects the link base; check.py assembles the probe
;; twice, once at 0 for byte offsets and once at the kernel base to
;; prove the module links high with the same internal layout.
;;
;; The manifest sits after input.asm: the module's own end assertion
;; runs at its include site and the data block is still the last module
;; byte. The manifest is a fixed list of qwords in the order check.py
;; expects; the magic and the entry count are checked before anything
;; else is read.
;;

use64

if defined INPUT_PROBE_ORG
        org INPUT_PROBE_ORG
else
        org 0
end if

include '../../kernel/cpu/cpu.asm'

MM_HOSTED = 1
include '../../kernel/mm/mm.asm'

include '../../kernel/irq/irq.asm'

INPUT_HOSTED = 1
include '../../kernel/input/input.asm'

; ------------------------------------------------------------------
; manifest, fixed order, read by check.py
; ------------------------------------------------------------------

align 8
input_probe:
        dq      'K64INPP1'                  ; magic
        dq      1                           ; manifest version
        dq      input_code_start
        dq      input_code_end
        dq      input_data_start
        dq      input_data_end
        dq      input_init
        dq      input_ioapic_read
        dq      input_ioapic_write
        dq      input_ioapic_bringup
        dq      input_ps2_wait_ibf
        dq      input_ps2_wait_obf
        dq      input_ps2_cmd
        dq      input_ps2_write_data
        dq      input_ps2_read_data
        dq      input_ps2_flush
        dq      input_ps2_mouse_write
        dq      input_ps2_mouse_ack
        dq      input_ps2_bringup
        dq      input_keyboard_irq
        dq      input_mouse_irq
        dq      input_wait_count
        dq      input_selftest
        dq      input_keymap
        dq      input_keymap_shift
        dq      input_state
        dq      input_test_selftest_mask
        dq      input_test_kbd_count
        dq      input_test_mouse_count
        dq      input_test_stage
        dq      input_test_mouse_last
        dq      input_test_mouse_buttons
        dq      input_ioapic_ver
        dq      input_ioapic_max
        dq      input_mouse_id
        dq      input_kbd_mods
        dq      input_kbd_e0
        dq      input_kbd_used
        dq      input_kbd_drops
        dq      input_mouse_state
        dq      input_mouse_b1
        dq      input_mouse_b2
        dq      input_mouse_bad
        dq      input_mouse_x
        dq      input_mouse_y
        dq      input_kbd_e1
        dq      input_kbd_buf
        dq      msg_input_selftest
        dq      msg_input_nl
        dq      vmm_map_mmio
        dq      irq_sti_wait
        dq      cpu_panic
        dq      cpu_com1_puts
        dq      cpu_com1_puthex
        dq      irq_dispatch
        dq      irq_test_ticks
        dq      irq_calib_hz
        dq      INPUT_IOAPIC_PA
        dq      INPUT_IOAPIC_VA
        dq      INPUT_VEC_KEYBOARD
        dq      INPUT_VEC_MOUSE
        dq      INPUT_IOAPIC_IDX_KBD_LO
        dq      INPUT_IOAPIC_IDX_KBD_HI
        dq      INPUT_IOAPIC_IDX_MOUSE_LO
        dq      INPUT_IOAPIC_IDX_MOUSE_HI
        dq      INPUT_RTE_KBD_MASKED
        dq      INPUT_RTE_MOUSE_MASKED
        dq      INPUT_RTE_KBD_LIVE
        dq      INPUT_RTE_MOUSE_LIVE
        dq      INPUT_CCB_SET
        dq      INPUT_CCB_CLEAR
        dq      INPUT_CCB_CHECK_MASK
        dq      INPUT_CCB_CHECK_VALUE
        dq      INPUT_PS2_STAT_AUX
        dq      INPUT_MOUSE_SYNC
        dq      INPUT_MOUSE_OVERFLOW
        dq      INPUT_ST_MOUSE_B1
        dq      INPUT_MOUSE_B1_MASK
        dq      INPUT_ST_MOUSE_LAST_MASK
        dq      INPUT_ST_KBD_FINAL
        dq      INPUT_ST_MOUSE_FINAL
        dq      INPUT_IRQ_TICKS_BASE
        dq      INPUT_IRQ_TICKS_FINAL
        dq      INPUT_KBD_CAP
        dq      INPUT_KBD_ENTRY
        dq      INPUT_SCANCODE_E1
        dq      INPUT_PORT_SPIN
        dq      INPUT_FLUSH_MAX
        dq      INPUT_ACK_RETRY
        dq      INPUT_WAIT_SPIN
        dq      INPUT_POLL_CHUNK
        dq      INPUT_POLL_ROUNDS
        dq      INPUT_TIMER_WINDOW
        dq      INPUT_MOUSE_XMAX
        dq      INPUT_MOUSE_YMAX
        dq      INPUT_MOUSE_XINIT
        dq      INPUT_MOUSE_YINIT
        dq      INPUT_ST_MOUSE_DX
        dq      INPUT_ST_MOUSE_DY
        dq      INPUT_ST_KBD_SCANCODE
        dq      INPUT_STAGE_DONE
        dq      INPUT_DATA_OFF_TEST_MASK
        dq      INPUT_DATA_OFF_TEST_KBD
        dq      INPUT_DATA_OFF_TEST_MOUSE
        dq      INPUT_DATA_OFF_TEST_STAGE
        dq      INPUT_DATA_OFF_KBD_E1
        dq      INPUT_DATA_OFF_KBD_BUF
        dq      INPUT_DATA_OFF_MESSAGES
        dq      IRQ_VEC_KEYBOARD
        dq      IRQ_VEC_MOUSE
        dq      IRQ_ST_FINAL_N
        dq      INPUT_LAPIC_VA
input_probe_end:

if (input_probe_end - input_probe) <> 112 * 8
        err 'the manifest must keep its fixed entry count'
end if
