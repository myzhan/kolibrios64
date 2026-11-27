;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; The mouse IRQ handler: the 3 byte packet state machine.
;;
;; input_mouse_irq is a leaf called by irq_dispatch with RCX = frame
;; (unused) inside the interrupt frame: IF = 0, caller saved registers
;; only, no heap, no unbounded wait, no serial output, no EOI. One call
;; consumes at most one data byte; the packet counter advances only on
;; a complete valid packet.
;;
;; Byte 1 must carry the sync bit 3; a byte without it is a bad packet:
;; dropped, counted, state machine reset. Bits 0..2 are the buttons,
;; bits 4/5 the sign of the X/Y deltas (a set sign bit means the delta
;; byte is negative, delta = byte - 256), bits 6/7 the X/Y overflow
;; flags: either one set means the movement counters saturated and the
;; deltas are unreliable, so the whole packet is dropped and counted
;; like a sync failure. The empty-OBF check makes a phantom delivery
;; from a masked-RTE pending edge harmless, exactly like in the
;; keyboard handler.
;;
;; Source filter: status bit 5 (AUX) marks a mouse-sourced byte; the
;; mouse leaf never consumes a keyboard byte (bit 5 clear): it returns
;; without reading 0x60 and without touching any counter. The byte
;; keeps OBF full, so the 8042 keeps IRQ 1 asserted (a masked keyboard
;; RTE latches the edge in the IOAPIC IRR); the keyboard leaf consumes
;; it on its own delivery. Symmetric to the keyboard source filter.
;;
;; Coordinates accumulate in screen space (X right, Y down; the PS/2
;; Y delta is up-positive, hence the subtraction) and clamp to the
;; frozen 1024x768 desktop geometry. Nothing is drawn here.
;;

input_mouse_irq:
        in      al, INPUT_PS2_STATUS
        test    al, INPUT_PS2_STAT_OBF
        jz      .out                    ; phantom delivery, nothing to read
        test    al, INPUT_PS2_STAT_AUX
        jz      .out                    ; keyboard byte: leave it for the kbd IRQ
        in      al, INPUT_PS2_DATA
        movzx   r9d, al
        mov     rax, [input_mouse_state]
        cmp     rax, 1
        je      .byte2
        cmp     rax, 2
        je      .byte3
        ; byte 1: the sync bit is mandatory, overflow bits 6/7 poison
        ; the whole packet
        test    r9d, INPUT_MOUSE_SYNC
        jz      .bad
        test    r9d, INPUT_MOUSE_OVERFLOW
        jnz     .bad
        mov     [input_mouse_b1], r9
        mov     qword [input_mouse_state], 1
        ret
.byte2:
        mov     [input_mouse_b2], r9
        mov     qword [input_mouse_state], 2
        ret
.byte3:
        ; complete packet: b1 in r10, b2 in r11, b3 in r9
        mov     r10, [input_mouse_b1]
        mov     r11, [input_mouse_b2]
        ; evidence: the raw packet, b1 | b2 << 8 | b3 << 16
        mov     rax, r9
        shl     rax, 16
        shl     r11, 8
        or      rax, r11
        or      rax, r10
        mov     [input_test_mouse_last], rax
        ; evidence: every button ever seen, OR accumulated
        mov     rax, r10
        and     eax, 7
        or      [input_test_mouse_buttons], rax
        ; X delta, sign extended through b1 bit 4
        mov     rdx, r11
        shr     rdx, 8                  ; r11 was shifted, recover b2
        test    r10, INPUT_MOUSE_SIGN_X
        jz      .dx_ready
        sub     rdx, 256
.dx_ready:
        ; Y delta, sign extended through b1 bit 5
        mov     rcx, r9
        test    r10, INPUT_MOUSE_SIGN_Y
        jz      .dy_ready
        sub     rcx, 256
.dy_ready:
        ; x += dx, clamp [0, 1023]
        mov     rax, [input_mouse_x]
        add     rax, rdx
        test    rax, rax
        jns     .x_high
        xor     eax, eax
        jmp     .x_store
.x_high:
        cmp     rax, INPUT_MOUSE_XMAX
        jle     .x_store
        mov     rax, INPUT_MOUSE_XMAX
.x_store:
        mov     [input_mouse_x], rax
        ; y -= dy (PS/2 Y is up positive, screen Y is down), clamp [0, 767]
        mov     rax, [input_mouse_y]
        sub     rax, rcx
        test    rax, rax
        jns     .y_high
        xor     eax, eax
        jmp     .y_store
.y_high:
        cmp     rax, INPUT_MOUSE_YMAX
        jle     .y_store
        mov     rax, INPUT_MOUSE_YMAX
.y_store:
        mov     [input_mouse_y], rax
if defined K64_RUNTIME
        ; Record left edges only after a complete PS/2 packet, even when
        ; the app is drawing and a full click occurs before its next POLL.
        mov     edx, r10d
        and     edx, 7
        mov     rcx, [input_mouse_buttons]
        mov     [input_mouse_buttons], rdx
        xor     ecx, edx
        test    cl, 1
        jz      .packet_done
        shl     rax, 16
        or      rax, [input_mouse_x]
        and     edx, 1
        shl     rdx, 32
        or      rax, rdx
        mov     rcx, [input_mouse_edge_write]
        and     ecx, INPUT_MOUSE_EDGE_CAP - 1
        shl     ecx, 4
        lea     rdx, [input_mouse_edges]
        mov     [rdx + rcx], rax
        mov     rax, [irq_tick_count]
        mov     [rdx + rcx + 8], rax
        inc     qword [input_mouse_edge_write]
.packet_done:
end if
        mov     qword [input_mouse_state], 0
        inc     qword [input_test_mouse_count]
        ret
.bad:
        inc     qword [input_mouse_bad]
        mov     qword [input_mouse_state], 0
.out:
        ret
