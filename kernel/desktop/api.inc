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
; DESKTOP-v2: the wallpaper taskbar, the start button and the single
; entry menu. The frozen v1 values above stay untouched; this block
; is a pure extension. The wallpaper asset constants live in
; wallpaper.inc (included inside the code region, after selftest.inc).
; ------------------------------------------------------------------

; the start button area inside the taskbar, frozen geometry
DESKTOP_START_X     = 0
DESKTOP_START_Y     = 0
DESKTOP_START_W     = 64
DESKTOP_START_H     = 32

; the single entry menu, frozen geometry: a 1 pixel accent frame and
; the inner panel one pixel inside on every side
DESKTOP_MENU_X      = 0
DESKTOP_MENU_Y      = 32
DESKTOP_MENU_W      = 200
DESKTOP_MENU_H      = 34
DESKTOP_MENU_COLOR  = 0x2C3E50          ; the inner panel, the bar color
DESKTOP_ACCENT_COLOR = 0x9594A2         ; the frame + taskbar accents,
                                        ; the wallpaper gray band color
DESKTOP_MENU_INNER_X = DESKTOP_MENU_X + 1
DESKTOP_MENU_INNER_Y = DESKTOP_MENU_Y + 1
DESKTOP_MENU_INNER_W = DESKTOP_MENU_W - 2
DESKTOP_MENU_INNER_H = DESKTOP_MENU_H - 2

; the single entry label 'CHIP-8', centered in the menu: the entry
; center DERIVES from the text origin and the font cell, never magic
DESKTOP_MENU_TEXT_LEN = 6
DESKTOP_MENU_TEXT_X = 76
DESKTOP_MENU_TEXT_Y = 41
DESKTOP_ENTRY_CX = DESKTOP_MENU_TEXT_X + DESKTOP_MENU_TEXT_LEN * DESKTOP_FONT_W / 2
DESKTOP_ENTRY_CY = DESKTOP_MENU_TEXT_Y + DESKTOP_FONT_H / 2

; the menu background save rectangle, one packed BGRX dword per pixel
DESKTOP_MENU_SAVE_BYTES = DESKTOP_MENU_W * DESKTOP_MENU_H * 4

; the v2 data labels, appended AFTER the frozen v1 block (0..1911):
; the messages end at 1872 + 36 + 3 = 1911, the first new qword sits
; at 1912. The menu save buffer owns data page 2 (offset 4096).
DESKTOP_DATA_OFF_PREV_BUTTONS    = 1912
DESKTOP_DATA_OFF_MENU_FLAG       = 1920
DESKTOP_DATA_OFF_APP_LAUNCH      = 1928
DESKTOP_DATA_OFF_TEST_CLICKS     = 1936
DESKTOP_DATA_OFF_TEST_MENU_OPENS = 1944
DESKTOP_DATA_OFF_MENU_SAVE       = DESKTOP_PAGE_SIZE
DESKTOP_DATA_PAGES               = 8

if DESKTOP_START_X <> 0 | DESKTOP_START_Y <> 0 | DESKTOP_START_W <> 64 | DESKTOP_START_H <> 32
        err 'the start button geometry is frozen at (0,0,64,32)'
end if
if DESKTOP_START_W > DESKTOP_BAR_W | DESKTOP_START_H <> DESKTOP_BAR_H
        err 'the start button must stay inside the taskbar'
end if
if DESKTOP_MENU_X <> 0 | DESKTOP_MENU_Y <> 32 | DESKTOP_MENU_W <> 200 | DESKTOP_MENU_H <> 34
        err 'the menu geometry is frozen at (0,32,200,34)'
end if
if DESKTOP_MENU_Y <> DESKTOP_BAR_H
        err 'the menu must hang directly below the taskbar'
end if
if DESKTOP_MENU_COLOR <> DESKTOP_BAR_COLOR | DESKTOP_ACCENT_COLOR <> 0x9594A2
        err 'the menu panel and accent colors are frozen'
end if
if DESKTOP_MENU_INNER_X <> 1 | DESKTOP_MENU_INNER_Y <> 33 | DESKTOP_MENU_INNER_W <> 198 | DESKTOP_MENU_INNER_H <> 32
        err 'the inner panel is the menu minus the 1 pixel accent frame'
end if
if DESKTOP_ENTRY_CX <> 100 | DESKTOP_ENTRY_CY <> 49
        err 'the CHIP-8 entry center derives to (100,49)'
end if
if DESKTOP_MENU_TEXT_X + DESKTOP_MENU_TEXT_LEN * DESKTOP_FONT_W > DESKTOP_MENU_X + DESKTOP_MENU_W
        err 'the entry label must fit inside the menu'
end if
if DESKTOP_MENU_TEXT_Y + DESKTOP_FONT_H > DESKTOP_MENU_Y + DESKTOP_MENU_H
        err 'the entry label must fit inside the menu vertically'
end if
if DESKTOP_MENU_SAVE_BYTES <> 27200
        err 'the menu save buffer is exactly 200*34 packed BGRX dwords'
end if
if DESKTOP_DATA_OFF_PREV_BUTTONS <> DESKTOP_DATA_OFF_MESSAGES + 36 + 3 + 1
        err 'the v2 labels start right after the frozen v1 messages'
end if
if DESKTOP_DATA_OFF_MENU_FLAG <> DESKTOP_DATA_OFF_PREV_BUTTONS + 8 | DESKTOP_DATA_OFF_APP_LAUNCH <> DESKTOP_DATA_OFF_MENU_FLAG + 8 | DESKTOP_DATA_OFF_TEST_CLICKS <> DESKTOP_DATA_OFF_APP_LAUNCH + 8 | DESKTOP_DATA_OFF_TEST_MENU_OPENS <> DESKTOP_DATA_OFF_TEST_CLICKS + 8
        err 'the v2 state qwords are consecutive'
end if
if DESKTOP_DATA_OFF_MENU_SAVE <> DESKTOP_PAGE_SIZE
        err 'the menu save buffer owns data page 2'
end if
if DESKTOP_DATA_PAGES <> (DESKTOP_DATA_OFF_MENU_SAVE + DESKTOP_MENU_SAVE_BYTES + DESKTOP_PAGE_SIZE - 1) / DESKTOP_PAGE_SIZE
        err 'the v2 data region is exactly 8 pages'
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

; ------------------------------------------------------------------
; task 125 DESKTOP-v2 T4/T5: the app launch wiring. Pure extension,
; every frozen v1/v2 value above stays untouched. The menu entry flag
; (offset 1928) is now consumed by desktop_launch_pump (taskbar.inc),
; the single consumer of app_launch; the foundation boot call site
; routes through desktop_app_boot into the same pump.
; ------------------------------------------------------------------

; the new host handshake stages, published ONLY on the click path:
; the boot self test never clicks the start button, so the frozen
; three-leg desktop_test_stage = 5 final stays intact
DESKTOP_STAGE_START_CLICK = 6           ; a start button press edge
DESKTOP_STAGE_ENTRY_CLICK = 7           ; the CHIP-8 entry press edge
DESKTOP_STAGE_APP_RUNNING = 8           ; app_launch entered (click mode)
DESKTOP_STAGE_APP_EXIT    = 9           ; app_launch returned (click mode)

; the taskbar running indicator: an accent block at the bar's right
; end, drawn while the hosted program runs. The successful teardown
; repaint erases it with the whole bar; the failure cleanup fills it
; back with the bar color. It never overlaps the title or accents.
DESKTOP_RUN_X     = 992
DESKTOP_RUN_Y     = 8
DESKTOP_RUN_W     = 24
DESKTOP_RUN_H     = 16
DESKTOP_RUN_COLOR = DESKTOP_ACCENT_COLOR

; the new evidence qwords, appended AFTER the frozen v2 block
; (1912..1951); page 1 tail padding shrinks, the page count stays 8
DESKTOP_DATA_OFF_TEST_START_CLICKS = 1952
DESKTOP_DATA_OFF_TEST_ENTRY_CLICKS = 1960
DESKTOP_DATA_OFF_TEST_APP_RUNS     = 1968
DESKTOP_DATA_OFF_TEST_APP_EXITS    = 1976

if DESKTOP_STAGE_START_CLICK <> DESKTOP_STAGE_DONE + 1 | DESKTOP_STAGE_ENTRY_CLICK <> DESKTOP_STAGE_START_CLICK + 1 | DESKTOP_STAGE_APP_RUNNING <> DESKTOP_STAGE_ENTRY_CLICK + 1 | DESKTOP_STAGE_APP_EXIT <> DESKTOP_STAGE_APP_RUNNING + 1
        err 'the task 125 click stages extend the frozen handshake 0..5'
end if
if DESKTOP_DATA_OFF_TEST_START_CLICKS <> DESKTOP_DATA_OFF_TEST_MENU_OPENS + 8 | DESKTOP_DATA_OFF_TEST_ENTRY_CLICKS <> DESKTOP_DATA_OFF_TEST_START_CLICKS + 8 | DESKTOP_DATA_OFF_TEST_APP_RUNS <> DESKTOP_DATA_OFF_TEST_ENTRY_CLICKS + 8 | DESKTOP_DATA_OFF_TEST_APP_EXITS <> DESKTOP_DATA_OFF_TEST_APP_RUNS + 8
        err 'the task 125 evidence qwords are consecutive after the v2 block'
end if
if DESKTOP_DATA_OFF_TEST_APP_EXITS + 8 > DESKTOP_PAGE_SIZE
        err 'the task 125 evidence must stay inside data page 1'
end if
if DESKTOP_RUN_X <> 992 | DESKTOP_RUN_Y <> 8 | DESKTOP_RUN_W <> 24 | DESKTOP_RUN_H <> 16
        err 'the running indicator geometry is frozen at (992,8,24,16)'
end if
if DESKTOP_RUN_X + DESKTOP_RUN_W > DESKTOP_BAR_W | DESKTOP_RUN_Y + DESKTOP_RUN_H > DESKTOP_BAR_H | DESKTOP_RUN_X <= DESKTOP_START_W
        err 'the running indicator must fit the bar right of the start button'
end if
if DESKTOP_RUN_COLOR <> DESKTOP_ACCENT_COLOR
        err 'the running indicator uses the frozen accent color'
end if
