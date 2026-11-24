;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; All mutable data of the input module, the last bytes it emits.
;;
;; The region starts and ends 4 KiB aligned, every byte is really emitted
;; (dq 0 / db ... dup 0, never rb) and starts as zero, so the flat binary
;; carries the whole block and the layout test can prove it. The labels
;; are public facts: the gdbstub smoke reads input_test_selftest_mask,
;; input_test_kbd_count, input_test_mouse_count and input_test_stage
;; straight out of guest memory.
;;
;; The key maps are constants and live in keyboard.inc (code side), the
;; same split the video module uses for its font: the data region stays
;; all zero at load time.
;;

align INPUT_DATA_ALIGN
input_data_start:

input_state:               dq 0         ; input_init succeeded once
input_test_selftest_mask:  dq 0         ; failed leg bits, 0 on success
input_test_kbd_count:      dq 0         ; keyboard data bytes, frozen at 2
input_test_mouse_count:    dq 0         ; complete mouse packets, frozen at 3
input_test_stage:          dq 0         ; host handshake, ends at 6
input_test_mouse_last:     dq 0         ; last raw packet b1|b2<<8|b3<<16
input_test_mouse_buttons:  dq 0         ; OR of every button bit seen
input_ioapic_ver:          dq 0         ; measured VER register, raw
input_ioapic_max:          dq 0         ; measured RTE entry count
input_mouse_id:            dq 0         ; measured mouse ID after reset
input_kbd_mods:            dq 0         ; modifier state bits
input_kbd_e0:              dq 0         ; 0xE0 prefix pending
input_kbd_used:            dq 0         ; key buffer fill level
input_kbd_drops:           dq 0         ; entries dropped on a full buffer
input_mouse_state:         dq 0         ; packet state machine 0/1/2
input_mouse_b1:            dq 0         ; packet byte 1 in flight
input_mouse_b2:            dq 0         ; packet byte 2 in flight
input_mouse_bad:           dq 0         ; packets dropped on sync/overflow
input_mouse_x:             dq 0         ; clamped screen X
input_mouse_y:             dq 0         ; clamped screen Y
input_kbd_e1:              dq 0         ; 0xE1 Pause payload bytes left

; the key buffer: INPUT_KBD_CAP entries of ascii qword + raw qword
input_kbd_buf:             dq INPUT_KBD_CAP * 2 dup 0

; diagnostics, ASCII only, printed on the failure path only
msg_input_selftest  db 'K64-INPUT-SELFTEST failed legs = ', 0
msg_input_nl        db 13, 10, 0

; tail padding, emitted as data so the file keeps every zero byte
        db (INPUT_PAGE_SIZE - (($ - input_data_start) and (INPUT_PAGE_SIZE - 1))) and (INPUT_PAGE_SIZE - 1) dup 0
input_data_end:

if (input_data_start and (INPUT_DATA_ALIGN - 1)) <> 0
        err 'the input data region must start page aligned'
end if
if (input_data_end - input_data_start) mod INPUT_PAGE_SIZE
        err 'the input data region must end page aligned'
end if
if input_state - input_data_start <> INPUT_DATA_OFF_STATE
        err 'INPUT data state label offset'
end if
if input_test_selftest_mask - input_data_start <> INPUT_DATA_OFF_TEST_MASK
        err 'INPUT data selftest mask label offset'
end if
if input_test_kbd_count - input_data_start <> INPUT_DATA_OFF_TEST_KBD
        err 'INPUT data keyboard count label offset'
end if
if input_test_mouse_count - input_data_start <> INPUT_DATA_OFF_TEST_MOUSE
        err 'INPUT data mouse count label offset'
end if
if input_test_stage - input_data_start <> INPUT_DATA_OFF_TEST_STAGE
        err 'INPUT data handshake stage label offset'
end if
if input_test_mouse_last - input_data_start <> INPUT_DATA_OFF_MOUSE_LAST
        err 'INPUT data last packet label offset'
end if
if input_test_mouse_buttons - input_data_start <> INPUT_DATA_OFF_MOUSE_BUTTONS
        err 'INPUT data buttons label offset'
end if
if input_ioapic_ver - input_data_start <> INPUT_DATA_OFF_IOAPIC_VER
        err 'INPUT data IOAPIC version label offset'
end if
if input_ioapic_max - input_data_start <> INPUT_DATA_OFF_IOAPIC_MAX
        err 'INPUT data IOAPIC entry count label offset'
end if
if input_mouse_id - input_data_start <> INPUT_DATA_OFF_MOUSE_ID
        err 'INPUT data mouse id label offset'
end if
if input_kbd_mods - input_data_start <> INPUT_DATA_OFF_KBD_MODS
        err 'INPUT data modifier state label offset'
end if
if input_kbd_e0 - input_data_start <> INPUT_DATA_OFF_KBD_E0
        err 'INPUT data E0 prefix label offset'
end if
if input_kbd_used - input_data_start <> INPUT_DATA_OFF_KBD_USED
        err 'INPUT data buffer fill label offset'
end if
if input_kbd_drops - input_data_start <> INPUT_DATA_OFF_KBD_DROPS
        err 'INPUT data buffer drop label offset'
end if
if input_mouse_state - input_data_start <> INPUT_DATA_OFF_MOUSE_STATE
        err 'INPUT data packet state label offset'
end if
if input_mouse_b1 - input_data_start <> INPUT_DATA_OFF_MOUSE_B1
        err 'INPUT data packet byte 1 label offset'
end if
if input_mouse_b2 - input_data_start <> INPUT_DATA_OFF_MOUSE_B2
        err 'INPUT data packet byte 2 label offset'
end if
if input_mouse_bad - input_data_start <> INPUT_DATA_OFF_MOUSE_BAD
        err 'INPUT data bad packet label offset'
end if
if input_mouse_x - input_data_start <> INPUT_DATA_OFF_MOUSE_X
        err 'INPUT data X coordinate label offset'
end if
if input_mouse_y - input_data_start <> INPUT_DATA_OFF_MOUSE_Y
        err 'INPUT data Y coordinate label offset'
end if
if input_kbd_e1 - input_data_start <> INPUT_DATA_OFF_KBD_E1
        err 'INPUT data E1 prefix label offset'
end if
if input_kbd_buf - input_data_start <> INPUT_DATA_OFF_KBD_BUF
        err 'INPUT data key buffer label offset'
end if
if msg_input_selftest - input_data_start <> INPUT_DATA_OFF_MESSAGES
        err 'the INPUT messages must follow the key buffer'
end if
if input_data_end <= input_data_start
        err 'the input data region must not be empty'
end if
