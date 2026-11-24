;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; All mutable data of the IRQ module, the last bytes it emits.
;;
;; The region starts and ends 4 KiB aligned, every byte is really emitted
;; (dq 0 / db ... dup 0, never rb) and starts as zero, so the flat binary
;; carries the whole block and the layout test can prove it. The labels
;; are public facts: the gdbstub smoke reads irq_test_selftest_mask and
;; irq_test_ticks straight out of guest memory.
;;
;; The leg 5 switch irq_test_skip_eoi defaults to zero: the normal
;; dispatch path always sends the EOI, only the self test ever sets it.
;;

align IRQ_DATA_ALIGN
irq_data_start:

irq_state:              dq 0            ; irq_init succeeded once
irq_tick_count:         dq 0            ; lifetime timer ticks
irq_spurious_count:     dq 0            ; vector 0xFF arrivals, never EOI'd
irq_test_ticks:         dq 0            ; per-leg counter, frozen at 10
irq_test_selftest_mask: dq 0            ; failed leg bits, 0 on success
irq_test_isr_seen:      dq 0            ; ISR bit before the EOI (want 1)
irq_test_isr_after:     dq 0            ; ISR bit after the EOI (want 0)
irq_test_if_value:      dq 0            ; handler frame RFLAGS bit 9
irq_test_skip_eoi:      dq 0            ; leg 5 hook, default 0
irq_calib_hz:           dq 0            ; median counter frequency
irq_calib_s0:           dq 0            ; the three raw samples
irq_calib_s1:           dq 0
irq_calib_s2:           dq 0
irq_test_frame_if:      dq 0            ; interrupted frame RFLAGS bit 9 (want 0x200)

; diagnostics, ASCII only, printed on the failure path only
msg_irq_unknown   db 'irq: unexpected vector', 0
msg_irq_selftest  db 'K64-IRQ-SELFTEST failed legs = ', 0
msg_irq_nl        db 13, 10, 0

; tail padding, emitted as data so the file keeps every zero byte
        db (IRQ_PAGE_SIZE - (($ - irq_data_start) and (IRQ_PAGE_SIZE - 1))) and (IRQ_PAGE_SIZE - 1) dup 0
irq_data_end:

if (irq_data_start and (IRQ_DATA_ALIGN - 1)) <> 0
        err 'the IRQ data region must start page aligned'
end if
if (irq_data_end - irq_data_start) mod IRQ_PAGE_SIZE
        err 'the IRQ data region must end page aligned'
end if
if irq_state - irq_data_start <> IRQ_DATA_OFF_STATE
        err 'IRQ data state label offset'
end if
if irq_tick_count - irq_data_start <> IRQ_DATA_OFF_TICK_COUNT
        err 'IRQ data tick count label offset'
end if
if irq_spurious_count - irq_data_start <> IRQ_DATA_OFF_SPURIOUS
        err 'IRQ data spurious count label offset'
end if
if irq_test_ticks - irq_data_start <> IRQ_DATA_OFF_TEST_TICKS
        err 'IRQ data test tick label offset'
end if
if irq_test_selftest_mask - irq_data_start <> IRQ_DATA_OFF_TEST_MASK
        err 'IRQ data selftest mask label offset'
end if
if irq_test_isr_seen - irq_data_start <> IRQ_DATA_OFF_ISR_SEEN
        err 'IRQ data ISR-before-EOI label offset'
end if
if irq_test_isr_after - irq_data_start <> IRQ_DATA_OFF_ISR_AFTER
        err 'IRQ data ISR-after-EOI label offset'
end if
if irq_test_if_value - irq_data_start <> IRQ_DATA_OFF_IF_VALUE
        err 'IRQ data IF evidence label offset'
end if
if irq_test_skip_eoi - irq_data_start <> IRQ_DATA_OFF_SKIP_EOI
        err 'IRQ data leg5 switch label offset'
end if
if irq_calib_hz - irq_data_start <> IRQ_DATA_OFF_CALIB_HZ
        err 'IRQ data calibration result label offset'
end if
if irq_calib_s0 - irq_data_start <> IRQ_DATA_OFF_CALIB_S0
        err 'IRQ data calibration sample 0 label offset'
end if
if irq_calib_s1 - irq_data_start <> IRQ_DATA_OFF_CALIB_S1
        err 'IRQ data calibration sample 1 label offset'
end if
if irq_calib_s2 - irq_data_start <> IRQ_DATA_OFF_CALIB_S2
        err 'IRQ data calibration sample 2 label offset'
end if
if irq_test_frame_if - irq_data_start <> IRQ_DATA_OFF_FRAME_IF
        err 'IRQ data interrupted-frame IF label offset'
end if
if msg_irq_unknown - irq_data_start <> IRQ_DATA_OFF_MESSAGES
        err 'the IRQ messages must follow the qword block'
end if
if irq_data_end <= irq_data_start
        err 'the IRQ data region must not be empty'
end if
