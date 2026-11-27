;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2026. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Input: drain SYS_READ_KEY and maintain true press/release levels
;; from raw set 1 scancodes. Make codes set the key, break codes
;; (scancode | 0x80) clear it. ESC make exits the program, Enter make
;; raises st_enter so the boot leg can switch to the game.
;;
;; Key map (scancode -> CHIP-8 key):
;;   '1'..'9' = 0x02..0x0A -> 1..9, '0' = 0x0B -> 0
;;   a=0x1E b=0x30 c=0x2E d=0x20 e=0x12 f=0x21 -> A..F
;;   E0 arrows: Up -> 4, Left -> 5, Right -> 6, Down -> 7
;; The 128 byte keymap table lives in chip8.asm, 0xFF = unmapped.
;;

; Drain until the raw scancode is zero. Break and E0 events have no ASCII.
; destroys: volatile registers, flags; updates st_key / st_enter
proc key_drain
        push    rsi
.drain_loop:
        call    sys_read_key
        mov     r11, APP_DATA_BASE    ; SYSCALL clobbered r11, reload base
        test    edx, edx
        jz      .drain_done
        cmp     edx, SC_ESC
        je      .drain_esc
        cmp     edx, SC_ENTER
        je      .drain_enter
        mov     ecx, edx
        and     ecx, not 0x80          ; preserve the E0 prefix flag
        mov     r9d, 1                ; numeric/hex binding
        lea     rsi, [keymap]
        cmp     ecx, 0x80
        jb      .lookup
        sub     ecx, 0x148             ; E0 48 (Up), through E0 50 (Down)
        cmp     ecx, arrow_keymap_end - arrow_keymap
        jae     .drain_loop
        lea     rsi, [arrow_keymap]
        mov     r9d, 2                ; independent arrow binding
.lookup:
        movzx   ecx, byte [rsi + rcx]
        cmp     ecx, 0xFF
        je      .drain_loop
        test    edx, 0x80
        jnz     .drain_release
        or      [r11 + rcx + (st_key - APP_DATA_BASE)], r9b
        jmp     .drain_loop
.drain_release:
        not     r9b
        and     [r11 + rcx + (st_key - APP_DATA_BASE)], r9b
        jmp     .drain_loop
.drain_enter:
        mov     byte [st_enter], 1
        jmp     .drain_loop
.drain_esc:
        mov     ecx, EXIT_OK
        call    sys_exit              ; never returns
.drain_done:
        pop     rsi
        ret
endp

arrow_keymap:
        db      4, 0xFF, 0xFF, 5, 0xFF, 6, 0xFF, 0xFF, 7
arrow_keymap_end:
