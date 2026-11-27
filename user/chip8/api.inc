;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2026. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Frozen APP-v1 ABI constants, data-domain layout and self-contained
;; MS x64 procedure macros for the CHIP-8 Ring 3 user program.
;;
;; This file includes nothing from kernel/. The instruction set macros
;; come from the fasm2 headers preloaded by the assembler command line
;; (INCLUDE=<fasm2>/include, -i"Include 'fasm2.inc'"), exactly like
;; tests/stage2-cpu/layout.asm. The procedure macros below are written
;; here so the user image stays self-contained.
;;

; ------------------------------------------------------------------
; frozen ABI: code, data and stack domains
; Scheme B relocation: user VAs shifted above 4 GiB, the loader
; identity map (2 MiB supervisor pages over [0, 4 GiB)) stays put.
; In-domain offsets, sizes and the syscall ABI are unchanged.
; ------------------------------------------------------------------

APP_CODE_BASE   = 0x100400000           ; flat binary org, entry point
APP_CODE_LIMIT  = 16 * 1024             ; RX+US code domain size
APP_DATA_BASE   = 0x100600000           ; RW+NX+US data domain base
APP_DATA_LIMIT  = 2 * 1024 * 1024       ; data domain size
APP_STACK_TOP   = 0x100800000           ; entry RSP, kernel-built stack
APP_STACK_SIZE  = 16 * 1024

; syscall numbers, Linux-style registers: rax = number, arguments in
; rdi/rsi/rdx/r10/r8/r9, result in rax; SYSCALL clobbers rcx and r11.
SYS_EXIT        = 0                     ; rdi = exit code, no return
SYS_BLIT        = 1                     ; rdi = src VA, esi/edx = dst x/y,
                                        ; r10d/r8d = w/h, rax = 0 or -errno
SYS_READ_KEY    = 2                     ; non-blocking, rax = ascii,
                                        ; rdx = raw set 1 scancode, 0 = empty
SYS_POLL        = 3                     ; cursor sync + bounded park: IBM leg
                                        ; pause spin IF=0, interactive leg a
                                        ; sti pause-spin window (task 141 W1).
                                        ; r8d = irq_test_ticks snapshot, frozen
                                        ; at 11 under the LVT-mask caliber; the
                                        ; interactive loop does NOT consume it
                                        ; (a tick-delta budget is always zero)

; exit codes
EXIT_OK         = 0
EXIT_BAD_OPCODE = 2                     ; unknown opcode encountered
EXIT_STACK_FAULT = 3                    ; call/ret stack depth out of range

; screen and blit geometry (screen is 1024x768 BGRX)
BLIT_DST_X      = 192
BLIT_DST_Y      = 224
FB_WIDTH        = 640
FB_HEIGHT       = 320
FB_STRIDE       = FB_WIDTH * 4

; palette, kept from the 32-bit original (BGRX dwords)
COLOR_BACK      = 0x00000000
COLOR_CELL      = 0x00ADE8F4

; ------------------------------------------------------------------
; CHIP-8 machine constants, semantics frozen to the 32-bit original
; ------------------------------------------------------------------

MEM_SIZE        = 4096
ROM_LOAD_ADDR   = 0x200                 ; PC initial value, ROM copy target
FONTSET_ADDR    = 0x000                 ; font set lives at RAM 0x000
FONTSET_CHAR    = 5                     ; FX29: I = V[x] * 5
STACK_DEPTH     = 16
KEY_COUNT       = 16
GFX_COLS        = 64
GFX_ROWS        = 32
GFX_SIZE        = GFX_COLS * GFX_ROWS
GFX_PIX_SIZE    = 10                    ; each cell is a 10x10 pixel block

RAND_MUL        = 214013                ; Cxkk LCG, MSVC constants
RAND_ADD        = 2531011
RAND_SEED       = 1                     ; fixed seed, IBM leg uses no random

IBM_INSTR_COUNT = 256                   ; deterministic boot leg budget
IPS_PER_TICK    = 5                     ; fixed interactive budget per POLL wake
TIMER_HZ        = 60                    ; delay/sound accumulator units per round
TIMER_ACC_BASE  = 100                   ; acc += 60 per round, drain by 100
DELTA_CLAMP     = 100                   ; legacy tick-delta guard, unused since
                                        ; task 141 W1 (kept: frozen layout)

SC_ESC          = 0x01                  ; set 1 make code, exits the program
SC_ENTER        = 0x1C                  ; set 1 make code, IBM -> game

; ------------------------------------------------------------------
; data-domain layout, frozen virtual addresses
; ------------------------------------------------------------------

CHIP8_RAM_VA    = APP_DATA_BASE + 0x0     ; 4096 B CHIP-8 RAM
CHIP8_GFX_VA    = APP_DATA_BASE + 0x1000  ; 64x32 cell bytes
CHIP8_FB_VA     = APP_DATA_BASE + 0x2000  ; 640*320*4 BGRX frame buffer
CHIP8_STATE_VA  = APP_DATA_BASE + 0xCA000 ; CPU state / key table page

; the frame buffer ends exactly where the state page begins
if CHIP8_FB_VA + FB_WIDTH * FB_HEIGHT * 4 <> CHIP8_STATE_VA
        err 'frame buffer must end at the state page'
end if
if CHIP8_RAM_VA + MEM_SIZE > CHIP8_GFX_VA
        err 'RAM overlaps gfx'
end if
if CHIP8_GFX_VA + GFX_SIZE > CHIP8_FB_VA
        err 'gfx overlaps frame buffer'
end if
if CHIP8_STATE_VA + 4096 > APP_DATA_BASE + APP_DATA_LIMIT
        err 'state page exceeds the data domain'
end if
if APP_STACK_TOP - APP_STACK_SIZE < CHIP8_STATE_VA + 4096
        err 'stack overlaps the data domain'
end if

; CHIP-8 RAM, font set at 0x000, ROM copied to 0x200
virtual at CHIP8_RAM_VA
chip8_ram       rb MEM_SIZE
end virtual

; 64x32 display cells, one byte each, 0 = off, 1 = on
virtual at CHIP8_GFX_VA
chip8_gfx       rb GFX_SIZE
end virtual

; compact BGRX frame buffer, row stride FB_STRIDE
virtual at CHIP8_FB_VA
chip8_fb        rb FB_WIDTH * FB_HEIGHT * 4
end virtual

; CPU state page, self-arranged, all fields inside one 4 KiB page
virtual at CHIP8_STATE_VA
st_opcode       rw 1                    ; +0x00 current opcode
st_v            rb 16                   ; +0x02 V0..VF registers
st_i            rw 1                    ; +0x12 index register
st_pc           rw 1                    ; +0x14 program counter
st_sp           rw 1                    ; +0x16 stack pointer
st_delay        rb 1                    ; +0x18 delay timer
st_sound        rb 1                    ; +0x19 sound timer (silent)
st_draw         rb 1                    ; +0x1A draw flag
st_mode         rb 1                    ; +0x1B 0 = IBM leg, 1 = interactive
st_stack        rw STACK_DEPTH          ; +0x1C call stack
st_key          rb KEY_COUNT            ; +0x3C per-key numeric and arrow masks
st_rand         rd 1                    ; +0x4C LCG state
st_last_tick    rd 1                    ; +0x50 legacy tick snapshot, unused
                                        ; since task 141 W1 (kept: frozen layout)
st_timer_acc    rd 1                    ; +0x54 60 Hz timer accumulator
st_enter        rb 1                    ; +0x58 Enter make seen
                rb 3                    ; +0x59 pad to dword alignment
st_budget       rd 1                    ; +0x5C IBM leg instruction budget
st_page_end     rb 0                    ; +0x60
end virtual

if st_page_end > CHIP8_STATE_VA + 4096
        err 'state fields exceed the state page'
end if

; ------------------------------------------------------------------
; ROM digests, big-endian qwords of the sha256 byte string.
; Assembly cannot hash, so these constants are the recorded digests;
; tests/stage2-app/chip8_model.py parses them from this file and
; asserts them against the actual roms/*.ch8 bytes. ROM sizes are
; asserted at the file inclusion sites in chip8.asm.
; ------------------------------------------------------------------

; sha256(ibm.ch8) =
; 8bf3b46d8a64c2074e7538200f684a2eaced258404d3c7d3bd7a917c3d0143e5
ROM_IBM_SHA0    = 0x8BF3B46D8A64C207
ROM_IBM_SHA1    = 0x4E7538200F684A2E
ROM_IBM_SHA2    = 0xACED258404D3C7D3
ROM_IBM_SHA3    = 0xBD7A917C3D0143E5

; sha256(tetris1.ch8) =
; 667cb026dee03f59f3a2fd81a2ffeab47da87731883f9601d37ba019976f94dd
ROM_TETRIS_SHA0 = 0x667CB026DEE03F59
ROM_TETRIS_SHA1 = 0xF3A2FD81A2FFEAB4
ROM_TETRIS_SHA2 = 0x7DA87731883F9601
ROM_TETRIS_SHA3 = 0xD37BA019976F94DD

; sha256(chip8.bin), 3315 bytes with raw Set 1 arrow support:
; 79f55550699fa61a82b8b258193c87f59c4d4ff4da223a8a9878fbdffafe6b81

; ------------------------------------------------------------------
; self-contained MS x64 procedure macros
;
; proc name[, param...]   opens a procedure. The first four integer
;                         arguments arrive in rcx/rdx/r8/r9 and are
;                         homed into the caller-provided shadow space,
;                         so each param is a qword memory operand.
;                         Callee-saved registers (rbx, rsi, rdi,
;                         r12..r15) used in the body must be pushed
;                         and popped explicitly, pushad/popad style.
; ret                     leave + retn, restoring the frame.
; endp                    closes the procedure.
; call64 target[, arg...] reserves the 32 byte shadow space, loads up
;                         to four arguments (64-bit registers or
;                         immediates only) and calls the target.
; ------------------------------------------------------------------

macro proc name*, params&
        name:
        push    rbp
        mov     rbp, rsp
        mov     [rbp+16], rcx
        mov     [rbp+24], rdx
        mov     [rbp+32], r8
        mov     [rbp+40], r9
        virtual at rbp+16
                iterate p, params
                        p dq ?
                end iterate
        end virtual
        macro ret? operand
                match any, operand
                        retn operand
                else
                        leave
                        retn
                end match
        end macro
        macro endp?!
                purge ret?
                purge endp?
        end macro
end macro

macro call64 target*, args&
        sub     rsp, 32
        local n
        n = 0
        iterate a, args
                n = n + 1
                if n = 1
                        mov     rcx, a
                else if n = 2
                        mov     rdx, a
                else if n = 3
                        mov     r8, a
                else if n = 4
                        mov     r9, a
                else
                        err 'call64 supports at most four arguments'
                end if
        end iterate
        call    target
        add     rsp, 32
end macro
