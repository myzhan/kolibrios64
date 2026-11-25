;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Desktop shell: private API, contract DESKTOP-v1 on top of GUI-G1
;; and IRQ-v2.
;;
;; Like the other modules this file repeats every frozen value under
;; the DESKTOP_ prefix instead of including a shared header. The core
;; contract file compares this copy with the shared K64_* spelling at
;; assembly time. The module is included after video/video.asm: it
;; calls the video primitives, input_ioapic_write and irq_sti_wait,
;; reads the public input/irq/video evidence labels and the bounded
;; CPU console, and nothing else outside its own files.
;;
;; Calling convention (Microsoft x64, integer registers only):
;;  - integer arguments in RCX, RDX, R8, R9, the fifth on the stack at
;;    [rsp+40] on entry;
;;  - 32 bytes of shadow space, RSP mod 16 = 0 before a call, no red zone;
;;  - RBX, RBP, RSI, RDI and R12..R15 are callee saved;
;;  - EAX returns a signed status;
;;  - DF = 0 on return, IF unchanged by a returning call.
;;
;; STI ownership, frozen: the desktop event loop is the SECOND legal
;; STI owner of the whole kernel (the first is the irq_sti_wait self
;; test window). desktop.inc contains exactly one STI, in the resident
;; loop, always preceded by the LAPIC LVT timer mask. The IRQ handlers
;; still run with IF = 0 and only write buffers/counters; every redraw
;; happens in the main loop (desktop_pump), never in IRQ context.
;;
;; The only framebuffer READ path of the kernel is video_readback_rect,
;; used exclusively by the cursor background save. The video self test
;; stays draw-only; the desktop evidence is the external QEMU screendump.
;;

DESKTOP_API_VERSION = 1

; ------------------------------------------------------------------
; status codes, EAX, signed, the shared foundation set
; ------------------------------------------------------------------

DESKTOP_OK          = 0
DESKTOP_EINVAL      = -1
DESKTOP_ENOMEM      = -2
DESKTOP_ENOENT      = -3
DESKTOP_EEXIST      = -4
DESKTOP_EPERM       = -5
DESKTOP_ENOTSUP     = -6
DESKTOP_ENOTREADY   = -7
DESKTOP_EALREADY    = -8
DESKTOP_EIO         = -9
DESKTOP_EOVERFLOW   = -10
DESKTOP_EFAULT      = -11
DESKTOP_EBUSY       = -12

; ------------------------------------------------------------------
; geometry and the private copies of the shared facts
; ------------------------------------------------------------------

DESKTOP_PAGE_SIZE     = 0x1000
DESKTOP_DATA_ALIGN    = 0x1000

DESKTOP_PUTS_MAX      = 1024
DESKTOP_PUTHEX_DIGITS = 16

; the frozen reference geometry, private copy of the video self test
; reference; desktop_init refuses anything else with ENOTSUP
DESKTOP_SCREEN_W = 1024
DESKTOP_SCREEN_H = 768

; ------------------------------------------------------------------
; the frozen desktop frame: background, title bar, title text
; ------------------------------------------------------------------

DESKTOP_BG_COLOR    = 0x1A2B3C          ; deep blue, the whole screen
DESKTOP_BAR_COLOR   = 0x2C3E50          ; the top title bar
DESKTOP_BAR_X       = 0
DESKTOP_BAR_Y       = 0
DESKTOP_BAR_W       = 1024
DESKTOP_BAR_H       = 32
DESKTOP_TITLE_X     = 8
DESKTOP_TITLE_Y     = 8
DESKTOP_TITLE_COLOR = 0xFFFFFF
DESKTOP_TITLE_LEN   = 16                ; "K64 Desktop v0.1"

; ------------------------------------------------------------------
; the frozen cursor sprite: 12x18 arrow, white fill, black outline,
; one byte per pixel (0 transparent, 1 black, 2 white). The pattern
; data lives in cursor.inc and is pinned byte for byte plus sha256 by
; the stage2-desktop gate.
; ------------------------------------------------------------------

DESKTOP_SPRITE_W     = 12
DESKTOP_SPRITE_H     = 18
DESKTOP_SPRITE_BYTES = DESKTOP_SPRITE_W * DESKTOP_SPRITE_H
DESKTOP_SPRITE_TRANSPARENT = 0
DESKTOP_SPRITE_BLACK = 1
DESKTOP_SPRITE_WHITE = 2
DESKTOP_CURSOR_BLACK = 0x000000
DESKTOP_CURSOR_WHITE = 0xFFFFFF

; the initial cursor position DERIVES from the input self test frozen
; exit coordinates (512+5, 384+3): the first pump synchronises to the
; accumulated input position, so freezing the derived value keeps the
; initial frame deterministic without touching the input module
DESKTOP_MOUSE_XINIT = 512
DESKTOP_MOUSE_YINIT = 384
DESKTOP_ST_MOUSE_DX = 5
DESKTOP_ST_MOUSE_DY = -3                ; PS/2 Y is up positive
DESKTOP_CURSOR_X0 = DESKTOP_MOUSE_XINIT + DESKTOP_ST_MOUSE_DX
DESKTOP_CURSOR_Y0 = DESKTOP_MOUSE_YINIT - DESKTOP_ST_MOUSE_DY
; the hotspot clamp keeps the whole sprite on screen: the bottom right
; corner of the sprite may never leave the 1024x768 geometry
DESKTOP_CURSOR_XMAX = DESKTOP_SCREEN_W - DESKTOP_SPRITE_W
DESKTOP_CURSOR_YMAX = DESKTOP_SCREEN_H - DESKTOP_SPRITE_H

; ------------------------------------------------------------------
; the frozen echo text area: (16,64) origin, 126 columns of 8 pixels
; (16 + 126*8 = 1024 exactly), 44 rows of 16 pixels (64 + 44*16 = 768
; exactly). A full line wraps, a full screen STOPS and counts drops
; (first version policy, frozen). Only printable 0x20..0x7E glyphs
; count into desktop_test_keys; Enter and Backspace never count.
; ------------------------------------------------------------------

DESKTOP_ECHO_X     = 16
DESKTOP_ECHO_Y     = 64
DESKTOP_ECHO_COLS  = 126
DESKTOP_ECHO_ROWS  = 44
DESKTOP_FONT_W     = 8
DESKTOP_FONT_H     = 16
DESKTOP_ECHO_COLOR = 0xFFFFFF
DESKTOP_ASCII_FIRST   = 0x20
DESKTOP_ASCII_LAST    = 0x7E
DESKTOP_KEY_ENTER     = 13              ; the frozen en_US keymap value
DESKTOP_KEY_BACKSPACE = 8

; ------------------------------------------------------------------
; the frozen host injection of the desktop self test, on top of the
; input self test sequence (sendkey b masked, sendkey a, mouse_move 5 3,
; mouse_button 1/0): sendkey h, sendkey i (echo "hi"), mouse_move 40 24
; (screen space, down positive), mouse_button 1 then 0 with the release
; gated on the packet count so QEMU can never coalesce press+release.
; ------------------------------------------------------------------

DESKTOP_ST_KEY1 = 'h'
DESKTOP_ST_KEY2 = 'i'
DESKTOP_ST_MOVE_DX = 40
DESKTOP_ST_MOVE_DY = 24                 ; monitor dy, screen space
DESKTOP_ST_CURSOR_X = DESKTOP_CURSOR_X0 + DESKTOP_ST_MOVE_DX
DESKTOP_ST_CURSOR_Y = DESKTOP_CURSOR_Y0 + DESKTOP_ST_MOVE_DY

; the host handshake stages of desktop_test_stage
DESKTOP_STAGE_IDLE         = 0
DESKTOP_STAGE_ECHO_H       = 1          ; host: sendkey h
DESKTOP_STAGE_ECHO_I       = 2          ; host: sendkey i
DESKTOP_STAGE_MOUSE_MOVE   = 3          ; host: mouse_move 40 24
DESKTOP_STAGE_MOUSE_BUTTON = 4          ; host: mouse_button 1, then 0
DESKTOP_STAGE_DONE         = 5

; frozen final counts, DETERMINISTICALLY derived from the WHOLE
; injection chain, never from a runtime observation:
;  - input kbd = 2 (input exit) + 4 (make+break of h and i) = 6;
;  - input mouse = 3 (input exit) + 3 (move + press + release) = 6;
;  - desktop keys = 2 (the echoed 'h' and 'i'; breaks store ascii 0,
;    the input self test entries are skipped by the consumed snapshot);
;  - ticks stays 11: the desktop loop masks the LVT timer BEFORE its
;    STI, no further tick may ever deliver.
DESKTOP_INPUT_KBD_BASE   = 2            ; input self test exit contract
DESKTOP_INPUT_MOUSE_BASE = 3
DESKTOP_ST_KBD_FINAL   = DESKTOP_INPUT_KBD_BASE + 4
DESKTOP_ST_MOUSE_FINAL = DESKTOP_INPUT_MOUSE_BASE + 3
DESKTOP_ST_KEYS_FINAL  = 2
DESKTOP_ST_KBD_USED_FINAL = DESKTOP_INPUT_KBD_BASE + 4
DESKTOP_IRQ_TICKS_FINAL = 11

; self test leg numbers, the failure mask is a qword
DESKTOP_ST_BIT_BG     = 0
DESKTOP_ST_BIT_TITLE  = 1
DESKTOP_ST_BIT_CURSOR = 2
DESKTOP_ST_BIT_ECHO   = 3
DESKTOP_ST_BIT_MOUSE  = 4
DESKTOP_ST_BIT_FINAL  = 5
DESKTOP_ST_ALL        = (1 shl (DESKTOP_ST_BIT_FINAL + 1)) - 1

; ------------------------------------------------------------------
; private copies of the IRQ/INPUT facts the shell consumes
; ------------------------------------------------------------------

DESKTOP_LAPIC_VA        = 0xFFFFC00002000000
DESKTOP_LAPIC_LVT_TIMER = 0x320
DESKTOP_LVT_MASK        = 0x10000

DESKTOP_IOAPIC_IDX_KBD_LO   = 0x10 + 2 * 1      ; pin 1, low dword
DESKTOP_IOAPIC_IDX_MOUSE_LO = 0x10 + 2 * 12     ; pin 12, low dword
DESKTOP_VEC_KEYBOARD = 0x21
DESKTOP_VEC_MOUSE    = 0x2C
DESKTOP_RTE_KBD_MASKED   = DESKTOP_LVT_MASK or DESKTOP_VEC_KEYBOARD
DESKTOP_RTE_MOUSE_MASKED = DESKTOP_LVT_MASK or DESKTOP_VEC_MOUSE
DESKTOP_RTE_KBD_LIVE     = DESKTOP_VEC_KEYBOARD
DESKTOP_RTE_MOUSE_LIVE   = DESKTOP_VEC_MOUSE

; the injection wait budget, the same sizing as the input self test:
; POLL_CHUNK * POLL_ROUNDS covers the 0.5 s host polling cadence plus
; the 100 ms sendkey hold under TCG jitter
DESKTOP_POLL_CHUNK  = 0x2000000
DESKTOP_POLL_ROUNDS = 64

DESKTOP_KBD_ENTRY = 16                  ; ascii qword + raw qword

; ------------------------------------------------------------------
; the static data region layout, offsets from desktop_data_start
; ------------------------------------------------------------------

DESKTOP_DATA_OFF_STATE        = 0
DESKTOP_DATA_OFF_TEST_MASK    = 8
DESKTOP_DATA_OFF_TEST_STAGE   = 16
DESKTOP_DATA_OFF_TEST_KEYS    = 24
DESKTOP_DATA_OFF_TEST_CURSOR_X = 32
DESKTOP_DATA_OFF_TEST_CURSOR_Y = 40
DESKTOP_DATA_OFF_CURSOR_X     = 48
DESKTOP_DATA_OFF_CURSOR_Y     = 56
DESKTOP_DATA_OFF_ECHO_COL     = 64
DESKTOP_DATA_OFF_ECHO_ROW     = 72
DESKTOP_DATA_OFF_ECHO_DROPS   = 80
DESKTOP_DATA_OFF_KBD_CONSUMED = 88
DESKTOP_DATA_OFF_ECHO_CHAR    = 96
DESKTOP_DATA_OFF_GUI_FRAME    = 104
DESKTOP_DATA_OFF_CURSOR_SAVE  = 144
DESKTOP_DATA_OFF_CURSOR_DRAW  = DESKTOP_DATA_OFF_CURSOR_SAVE + DESKTOP_SPRITE_BYTES * 4
DESKTOP_DATA_OFF_MESSAGES     = DESKTOP_DATA_OFF_CURSOR_DRAW + DESKTOP_SPRITE_BYTES * 4

virtual at 0
desktop_data_layout:
dt_state:
        dq      ?
dt_test_mask:
        dq      ?
dt_test_stage:
        dq      ?
dt_test_keys:
        dq      ?
dt_test_cursor_x:
        dq      ?
dt_test_cursor_y:
        dq      ?
dt_cursor_x:
        dq      ?
dt_cursor_y:
        dq      ?
dt_echo_col:
        dq      ?
dt_echo_row:
        dq      ?
dt_echo_drops:
        dq      ?
dt_kbd_consumed:
        dq      ?
dt_echo_char:
        dq      ?
dt_gui_frame:
        dq      ?
dt_reserved:
        dq      4 dup ?
dt_cursor_save:
        dd      DESKTOP_SPRITE_BYTES dup ?
dt_cursor_draw:
        dd      DESKTOP_SPRITE_BYTES dup ?
desktop_data_layout_end:
end virtual

if dt_state - desktop_data_layout <> DESKTOP_DATA_OFF_STATE
        err 'DESKTOP data state offset'
end if
if dt_test_mask - desktop_data_layout <> DESKTOP_DATA_OFF_TEST_MASK
        err 'DESKTOP data selftest mask offset'
end if
if dt_test_stage - desktop_data_layout <> DESKTOP_DATA_OFF_TEST_STAGE
        err 'DESKTOP data handshake stage offset'
end if
if dt_test_keys - desktop_data_layout <> DESKTOP_DATA_OFF_TEST_KEYS
        err 'DESKTOP data echoed keys offset'
end if
if dt_test_cursor_x - desktop_data_layout <> DESKTOP_DATA_OFF_TEST_CURSOR_X
        err 'DESKTOP data cursor X evidence offset'
end if
if dt_test_cursor_y - desktop_data_layout <> DESKTOP_DATA_OFF_TEST_CURSOR_Y
        err 'DESKTOP data cursor Y evidence offset'
end if
if dt_cursor_x - desktop_data_layout <> DESKTOP_DATA_OFF_CURSOR_X
        err 'DESKTOP data cursor X offset'
end if
if dt_cursor_y - desktop_data_layout <> DESKTOP_DATA_OFF_CURSOR_Y
        err 'DESKTOP data cursor Y offset'
end if
if dt_echo_col - desktop_data_layout <> DESKTOP_DATA_OFF_ECHO_COL
        err 'DESKTOP data echo column offset'
end if
if dt_echo_row - desktop_data_layout <> DESKTOP_DATA_OFF_ECHO_ROW
        err 'DESKTOP data echo row offset'
end if
if dt_echo_drops - desktop_data_layout <> DESKTOP_DATA_OFF_ECHO_DROPS
        err 'DESKTOP data echo drop count offset'
end if
if dt_kbd_consumed - desktop_data_layout <> DESKTOP_DATA_OFF_KBD_CONSUMED
        err 'DESKTOP data consumed index offset'
end if
if dt_echo_char - desktop_data_layout <> DESKTOP_DATA_OFF_ECHO_CHAR
        err 'DESKTOP data echo scratch offset'
end if
if dt_gui_frame - desktop_data_layout <> DESKTOP_DATA_OFF_GUI_FRAME
        err 'DESKTOP data GUI frame handshake offset'
end if
if dt_cursor_save - desktop_data_layout <> DESKTOP_DATA_OFF_CURSOR_SAVE
        err 'DESKTOP data cursor save buffer offset'
end if
if dt_cursor_draw - desktop_data_layout <> DESKTOP_DATA_OFF_CURSOR_DRAW
        err 'DESKTOP data cursor draw buffer offset'
end if
if desktop_data_layout_end - desktop_data_layout <> DESKTOP_DATA_OFF_MESSAGES
        err 'the DESKTOP data block must end where the messages start'
end if

; ------------------------------------------------------------------
; self consistency of the frozen facts
; ------------------------------------------------------------------

if DESKTOP_SCREEN_W <> 1024 | DESKTOP_SCREEN_H <> 768
        err 'the desktop geometry is the frozen video reference'
end if
if DESKTOP_BG_COLOR <> 0x1A2B3C | DESKTOP_BAR_COLOR <> 0x2C3E50
        err 'the desktop colors are frozen'
end if
if DESKTOP_BAR_W <> DESKTOP_SCREEN_W | DESKTOP_BAR_H <> 32
        err 'the title bar spans the screen width, 32 pixels tall'
end if
if DESKTOP_TITLE_X <> 8 | DESKTOP_TITLE_Y <> 8 | DESKTOP_TITLE_LEN <> 16
        err 'the title text origin and length are frozen'
end if
if DESKTOP_TITLE_Y + DESKTOP_FONT_H > DESKTOP_BAR_H
        err 'the title text must fit inside the title bar'
end if
if DESKTOP_SPRITE_W <> 12 | DESKTOP_SPRITE_H <> 18 | DESKTOP_SPRITE_BYTES <> 216
        err 'the cursor sprite is the frozen 12x18 arrow'
end if
if DESKTOP_CURSOR_X0 <> 517 | DESKTOP_CURSOR_Y0 <> 387
        err 'the initial cursor position derives from the input exit coordinates'
end if
if DESKTOP_CURSOR_X0 <> DESKTOP_MOUSE_XINIT + DESKTOP_ST_MOUSE_DX | DESKTOP_CURSOR_Y0 <> DESKTOP_MOUSE_YINIT - DESKTOP_ST_MOUSE_DY
        err 'the initial cursor position must stay derived, never magic'
end if
if DESKTOP_CURSOR_XMAX <> 1012 | DESKTOP_CURSOR_YMAX <> 750
        err 'the hotspot clamp keeps the whole sprite on screen'
end if
if DESKTOP_CURSOR_X0 + DESKTOP_SPRITE_W > DESKTOP_SCREEN_W | DESKTOP_CURSOR_Y0 + DESKTOP_SPRITE_H > DESKTOP_SCREEN_H
        err 'the initial sprite must stay fully on screen'
end if
if DESKTOP_ECHO_X + DESKTOP_ECHO_COLS * DESKTOP_FONT_W <> DESKTOP_SCREEN_W
        err 'the echo columns must tile the remaining width exactly'
end if
if DESKTOP_ECHO_Y + DESKTOP_ECHO_ROWS * DESKTOP_FONT_H <> DESKTOP_SCREEN_H
        err 'the echo rows must tile the remaining height exactly'
end if
if DESKTOP_ECHO_COLS <> 126 | DESKTOP_ECHO_ROWS <> 44
        err 'the echo area is frozen at 126 columns of 44 rows'
end if
if DESKTOP_KEY_ENTER <> 13 | DESKTOP_KEY_BACKSPACE <> 8
        err 'Enter/Backspace are the frozen en_US keymap ascii values'
end if
if DESKTOP_ST_CURSOR_X <> 557 | DESKTOP_ST_CURSOR_Y <> 411
        err 'the final cursor position derives from the injected move'
end if
if DESKTOP_ST_CURSOR_X <> DESKTOP_CURSOR_X0 + DESKTOP_ST_MOVE_DX | DESKTOP_ST_CURSOR_Y <> DESKTOP_CURSOR_Y0 + DESKTOP_ST_MOVE_DY
        err 'the final cursor position must stay derived, never magic'
end if
if DESKTOP_ST_CURSOR_X + DESKTOP_SPRITE_W > DESKTOP_SCREEN_W | DESKTOP_ST_CURSOR_Y + DESKTOP_SPRITE_H > DESKTOP_SCREEN_H
        err 'the final sprite must stay fully on screen'
end if
if DESKTOP_ST_KBD_FINAL <> 6 | DESKTOP_ST_MOUSE_FINAL <> 6 | DESKTOP_ST_KEYS_FINAL <> 2
        err 'the frozen final counts derive from the whole injection chain'
end if
if DESKTOP_ST_KBD_FINAL <> DESKTOP_INPUT_KBD_BASE + 4 | DESKTOP_ST_MOUSE_FINAL <> DESKTOP_INPUT_MOUSE_BASE + 3
        err 'the final counts must stay derived from the input exit values'
end if
if DESKTOP_IRQ_TICKS_FINAL <> 11
        err 'the desktop loop masks the timer, ticks stay frozen at 11'
end if
if DESKTOP_STAGE_DONE <> 5
        err 'the desktop handshake protocol ends at stage 5'
end if
if DESKTOP_ST_ALL <> 0x3F
        err 'the desktop self test mask covers six legs'
end if
if DESKTOP_LAPIC_VA <> 0xFFFFC00002000000 | DESKTOP_LAPIC_LVT_TIMER <> 0x320
        err 'the private IRQ LAPIC copies drifted'
end if
if DESKTOP_LVT_MASK <> 0x10000
        err 'the private IRQ mask copy drifted'
end if
if DESKTOP_IOAPIC_IDX_KBD_LO <> 0x12 | DESKTOP_IOAPIC_IDX_MOUSE_LO <> 0x28
        err 'the RTE indices derive from the pins: 0x10+2*1 and 0x10+2*12'
end if
if DESKTOP_RTE_KBD_MASKED <> 0x10021 | DESKTOP_RTE_MOUSE_MASKED <> 0x1002C
        err 'the masked RTE values are the shared mask bit plus the vector'
end if
if DESKTOP_RTE_KBD_LIVE <> 0x21 | DESKTOP_RTE_MOUSE_LIVE <> 0x2C
        err 'the live RTE values are the frozen vectors'
end if
if DESKTOP_POLL_CHUNK <> 0x2000000 | DESKTOP_POLL_ROUNDS <> 64
        err 'the injection budget matches the input self test sizing'
end if
if DESKTOP_KBD_ENTRY <> 16
        err 'the key buffer entry size is the input contract'
end if
if DESKTOP_PUTS_MAX <> 1024 | DESKTOP_PUTHEX_DIGITS <> 16
        err 'the private console caps drifted'
end if
if DESKTOP_DATA_OFF_CURSOR_SAVE + DESKTOP_SPRITE_BYTES * 4 <> DESKTOP_DATA_OFF_CURSOR_DRAW
        err 'the save buffer is exactly one packed sprite rectangle'
end if

; ------------------------------------------------------------------
; register helper macros, the same full frame the other modules use:
; eight pushes keep RSP mod 16 = 8, sub 40 realigns to 0 and provides
; the 32 byte shadow space plus one 8 byte slot for a fifth argument
; ------------------------------------------------------------------

macro desktop_enter
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

macro desktop_leave
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
