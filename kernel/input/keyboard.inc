;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; The keyboard IRQ handler and the frozen en_US set 1 key maps.
;;
;; input_keyboard_irq is a leaf called by irq_dispatch with RCX = frame
;; (unused) inside the interrupt frame: IF = 0, caller saved registers
;; only, no heap, no unbounded wait, no serial output, no EOI. One call
;; consumes at most one data byte.
;;
;; A masked RTE can leave a pending edge in the IOAPIC IRR that fires
;; right after the unmask; the handler therefore checks the 8042 OBF
;; first and ignores a delivery with an empty output buffer. That makes
;; the phantom harmless and keeps the counters honest.
;;
;; Source filter: status bit 5 (AUX) marks a mouse-sourced byte. The
;; keyboard leaf never consumes it: it returns without reading 0x60
;; and without touching any counter. The byte keeps OBF full, so the
;; 8042 keeps IRQ 12 asserted (a masked mouse RTE latches the edge in
;; the IOAPIC IRR and delivers it right after the unmask); the mouse
;; leaf consumes the byte on its own delivery. No edge is lost and no
;; byte is counted by the wrong stream, so the frozen counts derived
;; from the injection sequence stay exact under both streams live.
;;
;; Decoding: 0xE0 prefix state machine, the 0xE1 Pause prefix (the two
;; payload bytes are swallowed raw, never decoded as modifiers: E1 1D
;; would otherwise set CTRL), break bit 0x80, the modifier state
;; machine (LShift/RShift/Ctrl/Alt set-clear, Caps toggles on make),
;; then the en_US set 1 translation aligned with the 32 bit data32.inc
;; keymap/keymap_shift semantics. Every key event stores one
;; 16 byte buffer entry (ascii + raw scancode); extended keys store
;; raw | 0x100 with ascii 0. A full buffer drops the entry and counts
;; the drop, it never overwrites.
;;

input_keyboard_irq:
        in      al, INPUT_PS2_STATUS
        test    al, INPUT_PS2_STAT_OBF
        jz      .out                    ; phantom delivery, nothing to read
        test    al, INPUT_PS2_STAT_AUX
        jnz     .out                    ; mouse byte: leave it for the mouse IRQ
        in      al, INPUT_PS2_DATA
        inc     qword [input_test_kbd_count]
        cmp     al, INPUT_SCANCODE_E1
        jne     .not_e1
        mov     qword [input_kbd_e1], 2 ; Pause: two payload bytes follow
        ret
.not_e1:
        cmp     qword [input_kbd_e1], 0
        je      .not_pause
        dec     qword [input_kbd_e1]
        ret                             ; swallowed raw, no modifier decode
.not_pause:
        cmp     al, INPUT_SCANCODE_E0
        jne     .not_prefix
        mov     qword [input_kbd_e0], 1
        ret
.not_prefix:
        movzx   r9d, al                 ; the raw set 1 code
        mov     r10, r9                 ; buffer raw value
        xor     r8d, r8d                ; buffer ascii value
        cmp     qword [input_kbd_e0], 0
        je      .plain
        mov     qword [input_kbd_e0], 0
        or      r10, INPUT_RAW_E0_FLAG  ; extended key, ascii stays 0
        jmp     .store
.plain:
        mov     r11, [input_kbd_mods]
        cmp     r9d, INPUT_SC_CAPS
        je      .caps
        cmp     r9d, INPUT_SC_LSHIFT
        je      .set_lshift
        cmp     r9d, INPUT_SC_LSHIFT or INPUT_SCANCODE_BREAK
        je      .clr_lshift
        cmp     r9d, INPUT_SC_RSHIFT
        je      .set_rshift
        cmp     r9d, INPUT_SC_RSHIFT or INPUT_SCANCODE_BREAK
        je      .clr_rshift
        cmp     r9d, INPUT_SC_CTRL
        je      .set_ctrl
        cmp     r9d, INPUT_SC_CTRL or INPUT_SCANCODE_BREAK
        je      .clr_ctrl
        cmp     r9d, INPUT_SC_ALT
        je      .set_alt
        cmp     r9d, INPUT_SC_ALT or INPUT_SCANCODE_BREAK
        je      .clr_alt
        jmp     .key
.caps:
        xor     r11, INPUT_MOD_CAPS     ; toggle on make only
        jmp     .mod_done
.set_lshift:
        or      r11, INPUT_MOD_LSHIFT
        jmp     .mod_done
.clr_lshift:
        and     r11, not INPUT_MOD_LSHIFT
        jmp     .mod_done
.set_rshift:
        or      r11, INPUT_MOD_RSHIFT
        jmp     .mod_done
.clr_rshift:
        and     r11, not INPUT_MOD_RSHIFT
        jmp     .mod_done
.set_ctrl:
        or      r11, INPUT_MOD_CTRL
        jmp     .mod_done
.clr_ctrl:
        and     r11, not INPUT_MOD_CTRL
        jmp     .mod_done
.set_alt:
        or      r11, INPUT_MOD_ALT
        jmp     .mod_done
.clr_alt:
        and     r11, not INPUT_MOD_ALT
.mod_done:
        mov     [input_kbd_mods], r11
        jmp     .store                  ; modifiers store ascii 0
.key:
        test    r9d, INPUT_SCANCODE_BREAK
        jnz     .store                  ; break: ascii 0, raw keeps 0x80
        ; make: translate through the active map
        lea     rax, [input_keymap]
        mov     r11, [input_kbd_mods]
        test    r11, INPUT_MOD_LSHIFT or INPUT_MOD_RSHIFT
        jz      .translate
        lea     rax, [input_keymap_shift]
.translate:
        movzx   r8d, byte [rax + r9]
        test    r11, INPUT_MOD_CAPS
        jz      .store
        ; caps lock flips the case of letters only
        mov     eax, r8d
        or      eax, 0x20
        sub     eax, 'a'
        cmp     eax, 25
        ja      .store
        xor     r8d, 0x20
.store:
        mov     rax, [input_kbd_used]
        cmp     rax, INPUT_KBD_CAP
        jae     .drop
        shl     rax, 4                  ; 16 bytes per entry
        lea     rdx, [input_kbd_buf]
        mov     [rdx + rax], r8
        mov     [rdx + rax + 8], r10
        inc     qword [input_kbd_used]
        ret
.drop:
        inc     qword [input_kbd_drops]
.out:
        ret

; ------------------------------------------------------------------
; the frozen en_US set 1 maps, 128 bytes each, indexed by the make
; code; 0 means "no ascii" (modifiers, function keys, unknown codes).
; Semantics follow the 32 bit data32.inc keymap/keymap_shift rows.
; ------------------------------------------------------------------

input_keymap:
        db 0, 27, '1234567890-=', 8, 9          ; 0x00..0x0F
        db 'qwertyuiop[]', 13, 0                ; 0x10..0x1D
        db 'asdfghjkl;', 39, 96, 0              ; 0x1E..0x2A
        db 92, 'zxcvbnm,./', 0                  ; 0x2B..0x36
        db '*', 0, ' ', 0                       ; 0x37..0x3A
        db 0, 0, 0, 0, 0, 0, 0, 0, 0, 0         ; 0x3B..0x44 F1..F10
        db 0, 0                                 ; 0x45..0x46
        db '789-456+1230.'                      ; 0x47..0x53 keypad
        db 0, 0, 0, 0, 0, 0                     ; 0x54..0x59
        db 0, 0, 0, 0, 0, 0, 0, 0, 0, 0         ; 0x5A..0x63
        db 0, 0, 0, 0, 0, 0, 0, 0, 0, 0         ; 0x64..0x6D
        db 0, 0, 0, 0, 0, 0, 0, 0, 0, 0         ; 0x6E..0x77
        db 0, 0, 0, 0, 0, 0, 0, 0               ; 0x78..0x7F
input_keymap_end:

input_keymap_shift:
        db 0, 27, '!@#$%^&*()_+', 8, 9          ; 0x00..0x0F
        db 'QWERTYUIOP{}', 13, 0                ; 0x10..0x1D
        db 'ASDFGHJKL:', 34, '~', 0             ; 0x1E..0x2A
        db '|ZXCVBNM<>?', 0                     ; 0x2B..0x36
        db '*', 0, ' ', 0                       ; 0x37..0x3A
        db 0, 0, 0, 0, 0, 0, 0, 0, 0, 0         ; 0x3B..0x44
        db 0, 0                                 ; 0x45..0x46
        db '789-456+1230.'                      ; 0x47..0x53 keypad
        db 0, 0, 0, 0, 0, 0                     ; 0x54..0x59
        db 0, 0, 0, 0, 0, 0, 0, 0, 0, 0         ; 0x5A..0x63
        db 0, 0, 0, 0, 0, 0, 0, 0, 0, 0         ; 0x64..0x6D
        db 0, 0, 0, 0, 0, 0, 0, 0, 0, 0         ; 0x6E..0x77
        db 0, 0, 0, 0, 0, 0, 0, 0               ; 0x78..0x7F
input_keymap_shift_end:

if input_keymap_end - input_keymap <> 128
        err 'the en_US key map must stay 128 bytes'
end if
if input_keymap_shift_end - input_keymap_shift <> 128
        err 'the en_US shift map must stay 128 bytes'
end if
