;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; The input self test, seven legs, every wait bounded.
;;
;; The module has zero STI instructions: every interrupt window goes
;; through the exported irq_sti_wait of the IRQ layer, which always
;; returns with IF = 0. The host handshake: the guest publishes
;; input_test_stage, the host polls it through the gdbstub and injects
;; through the monitor (sendkey / mouse_move / mouse_button); a timeout
;; is a leg failure and panics, never a silent pass.
;;
;; Legs (failure bits of input_test_selftest_mask):
;;   0  IOAPIC RTE read back: vector, mask, polarity, trigger, dest
;;   1  8042 CCB read back (KIE/MIE/translation, clocks enabled) and
;;      the measured standard mouse ID
;;   2  keyboard masked: RTE pin 1 masked, the host injects sendkey b,
;;      a bounded IF = 1 window proves kbd_count stays 0 while BOTH the
;;      make and the break byte are drained from the 8042 (the drain
;;      count is the arrival evidence); only then may pin 1 go live
;;   3  keyboard unmask: the host injects sendkey a, make 0x1E and
;;      break 0x9E must land in the buffer with ascii 'a'
;;   4  mouse: the host injects mouse_move 5 3 then mouse_button 1/0,
;;      three packets with the sync bit, the button bit and the signed
;;      deltas; coordinates accumulate and clamp
;;   5  timer mask evidence (the IRQ-v1 gap): a one-shot armed under a
;;      masked LVT stays silent inside an IF = 1 window, unmasking and
;;      re-arming delivers exactly one tick (10 -> 11)
;;   6  final: everything masked, IF = 0, the counters frozen at the
;;      deterministic values derived from the injection sequence
;;
;; On exit: both RTEs masked, LVT timer masked, IF = 0,
;; input_test_kbd_count == 2, input_test_mouse_count == 3,
;; irq_test_ticks == 11, input_test_stage == 6, mask == 0.
;;

; ------------------------------------------------------------------
; wait until a public counter reaches a target, in bounded chunks
; in
; rcx = counter address, rdx = target
; out
; eax = INPUT_OK or INPUT_EIO; IF is 0 on return (irq_sti_wait owns it)
; leaf: r9/r10/r11 hold the state across the irq_sti_wait calls, which
; only touch rax/rcx/rdx/r8
; ------------------------------------------------------------------

input_wait_count:
        mov     r9, rcx
        mov     r10, rdx
        mov     r11d, INPUT_POLL_ROUNDS
.loop:
        cmp     [r9], r10
        jae     .ok
        xor     ecx, ecx
        mov     rdx, INPUT_POLL_CHUNK
        mov     r8d, 1
        call    irq_sti_wait
        dec     r11d
        jnz     .loop
        mov     eax, INPUT_EIO
        ret
.ok:
        xor     eax, eax
        ret

; ------------------------------------------------------------------
; run the seven legs
; in
; nothing; input_init must have succeeded, irq_selftest must have
; exited (timer masked, ticks = 10), IF must be 0
; out
; eax = INPUT_OK, or INPUT_EIO with rdx = the failed leg mask
; ------------------------------------------------------------------

input_selftest:
        input_enter
        cld
        cli
        xor     r12d, r12d              ; failure mask
        mov     rsi, INPUT_IOAPIC_VA
        mov     r13, INPUT_LAPIC_VA

        ; the 10 ms count derives from the public IRQ calibration
        ; evidence, exactly like the IRQ self test arms it
        mov     rax, [irq_calib_hz]
        xor     edx, edx
        mov     ecx, INPUT_TICK_HZ
        div     rcx
        mov     r14, rax

        ; ---------------- leg 0: the RTE read back ----------------
        cmp     qword [input_ioapic_ver], 0
        je      input_st_fail_ioapic
        cmp     qword [input_ioapic_max], INPUT_RTE_MIN
        jb      input_st_fail_ioapic
        mov     ecx, INPUT_IOAPIC_IDX_KBD_HI
        call    input_ioapic_read
        test    eax, eax
        jnz     input_st_fail_ioapic
        mov     ecx, INPUT_IOAPIC_IDX_KBD_LO
        call    input_ioapic_read
        cmp     eax, INPUT_RTE_KBD_MASKED
        jne     input_st_fail_ioapic
        mov     ecx, INPUT_IOAPIC_IDX_MOUSE_HI
        call    input_ioapic_read
        test    eax, eax
        jnz     input_st_fail_ioapic
        mov     ecx, INPUT_IOAPIC_IDX_MOUSE_LO
        call    input_ioapic_read
        cmp     eax, INPUT_RTE_MOUSE_MASKED
        jne     input_st_fail_ioapic

        ; ---------------- leg 1: CCB read back + mouse ID ----------------
        mov     r8b, INPUT_PS2_CMD_READ_CCB
        call    input_ps2_cmd
        test    eax, eax
        jnz     input_st_fail_ps2
        call    input_ps2_read_data
        test    eax, eax
        js      input_st_fail_ps2
        and     eax, INPUT_CCB_CHECK_MASK
        cmp     eax, INPUT_CCB_CHECK_VALUE
        jne     input_st_fail_ps2
        cmp     qword [input_mouse_id], INPUT_MOUSE_ID_STANDARD
        jne     input_st_fail_ps2

        ; ---------------- leg 2: keyboard masked ----------------
        ; pin 1 stays masked; publish the stage and give the host a
        ; bounded budget to inject sendkey b. Every chunk runs with
        ; IF = 1: the count must not move while BOTH the make and the
        ; break byte are drained (sendkey holds the key ~100 ms, so
        ; the break may land after the make; unmasking before it
        ; arrives would count it into leg 3). That separates the mask
        ; from a missing key.
        mov     qword [input_test_stage], INPUT_STAGE_KBD_MASKED
        mov     r15d, INPUT_POLL_ROUNDS
        xor     ebx, ebx                ; bytes drained while masked
.kbd_masked_poll:
        xor     ecx, ecx
        mov     rdx, INPUT_POLL_CHUNK
        mov     r8d, 1
        call    irq_sti_wait
        call    input_ps2_flush
        add     ebx, eax
        cmp     qword [input_test_kbd_count], 0
        jne     input_st_fail_kbd_masked
        cmp     ebx, 2                  ; make + break of sendkey b
        jae     .kbd_masked_done
        dec     r15d
        jnz     .kbd_masked_poll
        jmp     input_st_fail_kbd_masked
.kbd_masked_done:

        ; ---------------- leg 3: keyboard unmask ----------------
        ; unmask pin 1; a pending IRR edge from the masked leg may
        ; deliver a phantom, the handler ignores it (empty OBF)
        mov     ecx, INPUT_IOAPIC_IDX_KBD_LO
        mov     edx, INPUT_RTE_KBD_LIVE
        call    input_ioapic_write
        mov     qword [input_test_stage], INPUT_STAGE_KBD_LIVE
        mov     rcx, input_test_kbd_count
        mov     rdx, INPUT_ST_KBD_FINAL
        call    input_wait_count
        test    eax, eax
        jnz     input_st_fail_kbd_live
        mov     ecx, INPUT_IOAPIC_IDX_KBD_LO
        mov     edx, INPUT_RTE_KBD_MASKED
        call    input_ioapic_write
        cmp     qword [input_test_kbd_count], INPUT_ST_KBD_FINAL
        jne     input_st_fail_kbd_live
        ; the buffer: 'a' make with ascii, break with ascii 0
        cmp     qword [input_kbd_used], INPUT_ST_KBD_FINAL
        jne     input_st_fail_kbd_live
        cmp     qword [input_kbd_drops], 0
        jne     input_st_fail_kbd_live
        cmp     qword [input_kbd_buf], 'a'
        jne     input_st_fail_kbd_live
        cmp     qword [input_kbd_buf + 8], INPUT_ST_KBD_SCANCODE
        jne     input_st_fail_kbd_live
        cmp     qword [input_kbd_buf + 16], 0
        jne     input_st_fail_kbd_live
        cmp     qword [input_kbd_buf + 24], INPUT_ST_KBD_SCANCODE or INPUT_SCANCODE_BREAK
        jne     input_st_fail_kbd_live

        ; ---------------- leg 4: the mouse packets ----------------
        ; start the coordinates at the frozen desktop center so both
        ; delta signs are observable, then unmask pin 12
        mov     qword [input_mouse_x], INPUT_MOUSE_XINIT
        mov     qword [input_mouse_y], INPUT_MOUSE_YINIT
        mov     ecx, INPUT_IOAPIC_IDX_MOUSE_LO
        mov     edx, INPUT_RTE_MOUSE_LIVE
        call    input_ioapic_write
        mov     qword [input_test_stage], INPUT_STAGE_MOUSE_MOVE
        mov     rcx, input_test_mouse_count
        mov     rdx, 1
        call    input_wait_count
        test    eax, eax
        jnz     input_st_fail_mouse
        ; the move packet: sync bit, the sign bit of the negative Y
        ; (derived byte 1, measured QEMU behavior), no buttons, the
        ; frozen deltas
        mov     rax, INPUT_ST_MOUSE_B1 or ((INPUT_ST_MOUSE_DX and 0xFF) shl 8) or ((INPUT_ST_MOUSE_DY and 0xFF) shl 16)
        cmp     [input_test_mouse_last], rax
        jne     input_st_fail_mouse
        mov     qword [input_test_stage], INPUT_STAGE_MOUSE_BUTTON
        mov     rcx, input_test_mouse_count
        mov     rdx, INPUT_ST_MOUSE_FINAL
        call    input_wait_count
        test    eax, eax
        jnz     input_st_fail_mouse
        mov     ecx, INPUT_IOAPIC_IDX_MOUSE_LO
        mov     edx, INPUT_RTE_MOUSE_MASKED
        call    input_ioapic_write
        cmp     qword [input_test_mouse_count], INPUT_ST_MOUSE_FINAL
        jne     input_st_fail_mouse
        cmp     qword [input_mouse_bad], 0
        jne     input_st_fail_mouse
        ; the left button was seen; the release packet is the last one:
        ; zero deltas, no buttons, sync set. The sign bits are masked
        ; out: QEMU may set them even on a zero delta.
        cmp     qword [input_test_mouse_buttons], 1
        jne     input_st_fail_mouse
        mov     rax, [input_test_mouse_last]
        and     rax, INPUT_ST_MOUSE_LAST_MASK
        cmp     rax, INPUT_MOUSE_SYNC
        jne     input_st_fail_mouse
        ; the coordinates moved by exactly the injected deltas
        cmp     qword [input_mouse_x], INPUT_MOUSE_XINIT + INPUT_ST_MOUSE_DX
        jne     input_st_fail_mouse
        cmp     qword [input_mouse_y], INPUT_MOUSE_YINIT - INPUT_ST_MOUSE_DY
        jne     input_st_fail_mouse

        ; ---------------- leg 5: the timer mask evidence ----------------
        ; arm a one-shot under a masked LVT: the expiry must stay
        ; silent inside the IF = 1 window (mode 2 fails if a tick
        ; arrives), then unmask + re-arm delivers exactly one tick.
        ; The final 11 is deterministic under both QEMU masked-expiry
        ; behaviors: (a) expiry discarded -> the re-arm delivers one
        ; tick; (b) expiry latched in IRR -> the unmask delivers the
        ; phantom as tick 11, the mode-0 wait exits within a few
        ; instructions and masks the LVT long before the re-armed
        ; 10 ms countdown could deliver a 12th. A one-shot fires at
        ; most once per arm, so no timing window is relied on.
        mov     qword [input_test_stage], INPUT_STAGE_TIMER
        mov     dword [r13 + INPUT_LAPIC_DIVIDE], INPUT_DIVIDE_16
        mov     dword [r13 + INPUT_LAPIC_LVT_TIMER], INPUT_LVT_MASK
        mov     dword [r13 + INPUT_LAPIC_INIT_COUNT], r14d
        mov     rcx, INPUT_IRQ_TICKS_FINAL
        mov     rdx, INPUT_TIMER_WINDOW
        mov     r8d, 2
        call    irq_sti_wait
        test    eax, eax
        jnz     input_st_fail_timer
        mov     dword [r13 + INPUT_LAPIC_LVT_TIMER], INPUT_VEC_TIMER or INPUT_LVT_ONESHOT
        mov     dword [r13 + INPUT_LAPIC_INIT_COUNT], r14d
        mov     rcx, INPUT_IRQ_TICKS_FINAL
        mov     rdx, INPUT_WAIT_SPIN
        xor     r8d, r8d
        call    irq_sti_wait
        test    eax, eax
        jnz     input_st_fail_timer
        mov     dword [r13 + INPUT_LAPIC_LVT_TIMER], INPUT_LVT_MASK
        cmp     qword [irq_test_ticks], INPUT_IRQ_TICKS_FINAL
        jne     input_st_fail_timer

        ; ---------------- leg 6: the final freeze ----------------
        cli
        mov     dword [r13 + INPUT_LAPIC_LVT_TIMER], INPUT_LVT_MASK
        mov     ecx, INPUT_IOAPIC_IDX_KBD_LO
        mov     edx, INPUT_RTE_KBD_MASKED
        call    input_ioapic_write
        mov     ecx, INPUT_IOAPIC_IDX_MOUSE_LO
        mov     edx, INPUT_RTE_MOUSE_MASKED
        call    input_ioapic_write
        cmp     qword [input_test_kbd_count], INPUT_ST_KBD_FINAL
        jne     input_st_fail_final
        cmp     qword [input_test_mouse_count], INPUT_ST_MOUSE_FINAL
        jne     input_st_fail_final
        cmp     qword [irq_test_ticks], INPUT_IRQ_TICKS_FINAL
        jne     input_st_fail_final
        mov     qword [input_test_stage], INPUT_STAGE_DONE
        mov     [input_test_selftest_mask], r12
        xor     eax, eax
        xor     edx, edx
        input_leave
        ret

input_st_fail_ioapic:
        or      r12, 1 shl INPUT_ST_BIT_IOAPIC
        jmp     input_st_failed
input_st_fail_ps2:
        or      r12, 1 shl INPUT_ST_BIT_PS2
        jmp     input_st_failed
input_st_fail_kbd_masked:
        or      r12, 1 shl INPUT_ST_BIT_KBD_MASKED
        jmp     input_st_failed
input_st_fail_kbd_live:
        or      r12, 1 shl INPUT_ST_BIT_KBD_LIVE
        jmp     input_st_failed
input_st_fail_mouse:
        or      r12, 1 shl INPUT_ST_BIT_MOUSE
        jmp     input_st_failed
input_st_fail_timer:
        or      r12, 1 shl INPUT_ST_BIT_TIMER
        jmp     input_st_failed
input_st_fail_final:
        or      r12, 1 shl INPUT_ST_BIT_FINAL
input_st_failed:
        ; freeze everything, then report the mask before returning EIO
        cli
        mov     dword [r13 + INPUT_LAPIC_LVT_TIMER], INPUT_LVT_MASK
        mov     ecx, INPUT_IOAPIC_IDX_KBD_LO
        mov     edx, INPUT_RTE_KBD_MASKED
        call    input_ioapic_write
        mov     ecx, INPUT_IOAPIC_IDX_MOUSE_LO
        mov     edx, INPUT_RTE_MOUSE_MASKED
        call    input_ioapic_write
        mov     [input_test_selftest_mask], r12
        mov     rcx, msg_input_selftest
        mov     rdx, INPUT_PUTS_MAX
        call    cpu_com1_puts
        mov     rcx, [input_test_selftest_mask]
        mov     rdx, INPUT_PUTHEX_DIGITS
        call    cpu_com1_puthex
        mov     rcx, msg_input_nl
        mov     rdx, INPUT_PUTS_MAX
        call    cpu_com1_puts
        mov     rdx, r12
        mov     eax, INPUT_EIO
        input_leave
        ret
