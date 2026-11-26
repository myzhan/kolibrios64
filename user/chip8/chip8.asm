;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2026. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; CHIP-8 emulator, Ring 3 user program for the 64-bit KolibriOS.
;; Ported from the 32-bit KolibriOS emulator by rgimad (2021),
;; programs/emulator/chip8/, GNU GPL v2.
;;
;; Flat binary, org 0x100400000, entry at 0x100400000 (scheme B
;; relocation above the 4 GiB identity map). Code domain
;; [0x100400000, +16KiB) RX+US; data domain [0x100600000, +2MiB)
;; RW+NX+US with fixed layout frozen in api.inc; entry RSP =
;; 0x100800000.
;;
;; Boot: deterministic IBM leg. Initialize, load /ROMS/IBM.CH8 through
;; the frozen FS syscalls 4..6 straight into RAM+0x200 (task 115: the
;; program never embeds ROM bytes, the digest constants in api.inc stay
;; as gate expectations), execute exactly 256 instructions (pure
;; instruction count, no tick dependency), render 640x320, SYS_BLIT
;; once, then drain keys + POLL until Enter make. Enter loads
;; /ROMS/TETRIS1.CH8 the same way and enters interactive mode: a FIXED
;; budget of IPS_PER_TICK instructions per POLL wake (task 141 W1: the
;; frozen LVT-mask caliber keeps irq_test_ticks at 11, so a tick-delta
;; budget would always be zero), nominal ~417 ips at the ~12 ms bounded
;; park round, 60Hz timer divider accumulated per round, render + BLIT
;; when the draw flag is raised. ESC exits with code 0.
;;
;; Assemble exactly like tests/stage2-cpu/layout.asm:
;;   INCLUDE=<fasm2>/include fasmg -e100 -i"Include 'fasm2.inc'" \
;;       user/chip8/chip8.asm build/user-chip8/chip8.bin
;;

use64
org     APP_CODE_BASE

include 'api.inc'

; ------------------------------------------------------------------
; entry point, must stay at APP_CODE_BASE (api.inc emits no bytes)
; ------------------------------------------------------------------

start:
        ; deterministic IBM leg, the ROM lands via the FS read syscall
        call64  chip8_init
        call64  load_rom_fs, path_ibm, ROM_IBM_SIZE
        mov     dword [st_budget], IBM_INSTR_COUNT
.ibm_step:
        call64  chip8_step
        dec     dword [st_budget]
        jnz     .ibm_step
        call64  render_frame
        call64  sys_blit_frame
        ; wait for Enter make, draining keys and parking on POLL
.wait_enter:
        call64  key_drain
        cmp     byte [st_enter], 1
        je      .game
        call64  sys_poll
        jmp     .wait_enter
.game:
        call64  chip8_init
        call64  load_rom_fs, path_tetris, ROM_TETRIS_SIZE
        mov     byte [st_mode], 1
        ; interactive loop, task 141 W1: a FIXED instruction budget per
        ; POLL wake. The POLL r8d snapshot (irq_test_ticks) is frozen at
        ; 11 under the LVT-mask caliber and is NOT consumed here: the
        ; old tick-delta budget was always zero (the W1 dead end). Each
        ; round = one bounded POLL park (kernel sti pause-spin window,
        ; ~12 ms nominal) + IPS_PER_TICK instructions (~417 ips) +
        ; TIMER_HZ accumulator units, drained by TIMER_ACC_BASE below
        ; for the 60 Hz delay/sound timers. No timer tick dependency.
.main_loop:
        call64  key_drain
        call64  sys_poll
        mov     ebx, IPS_PER_TICK     ; fixed budget per wake round
        add     dword [st_timer_acc], TIMER_HZ
.run_instr:
        call64  chip8_step
        dec     ebx
        jnz     .run_instr
        ; 60Hz divider: while acc >= 100 decrement both timers
.timer_drain:
        cmp     dword [st_timer_acc], TIMER_ACC_BASE
        jb      .present
        sub     dword [st_timer_acc], TIMER_ACC_BASE
        cmp     byte [st_delay], 0
        je      .tick_sound
        dec     byte [st_delay]
.tick_sound:
        cmp     byte [st_sound], 0
        je      .timer_drain
        dec     byte [st_sound]       ; sound output is a stub
        jmp     .timer_drain
.present:
        cmp     byte [st_draw], 0
        je      .main_loop
        mov     byte [st_draw], 0
        call64  render_frame
        call64  sys_blit_frame
        jmp     .main_loop

include 'emu.inc'
include 'gui.inc'
include 'input.inc'

; ------------------------------------------------------------------
; syscall wrappers, frozen APP-v1 ABI
; SYSCALL clobbers rcx (return RIP) and r11 (RFLAGS); callers keep
; nothing live in those two across a call.
; ------------------------------------------------------------------

; exit the program
; in: ecx = exit code, never returns
sys_exit:
        mov     eax, SYS_EXIT
        mov     edi, ecx
        syscall
        jmp     sys_exit

; non-blocking key read
; out: rax = ascii (0 = queue empty), rdx = raw set 1 scancode
sys_read_key:
        mov     eax, SYS_READ_KEY
        syscall
        ret

; park until any interrupt, kernel syncs the cursor first
; out: rax = mouse_x | mouse_y<<16, rdx = buttons, r8d = tick snapshot
sys_poll:
        mov     eax, SYS_POLL
        syscall
        ret

; load one ROM through the frozen FS-v1 syscalls: open read-only, one
; POSIX read straight into RAM+0x200, exact byte count check, close.
; Any failure exits with EXIT_FS_LOAD, the kernel tears the domain down.
; in: lrf_path = NUL terminated image path (code domain),
;     lrf_len  = frozen ROM byte count
proc load_rom_fs, lrf_path, lrf_len
        push    rbx
        push    rsi
        push    rdi
        mov     rdi, [lrf_path]
        xor     esi, esi            ; FS_O_RDONLY
        call    sys_open            ; rax = fd 0..7 or negative K64 errno
        test    rax, rax
        js      .fail
        mov     ebx, eax
        mov     edi, ebx
        mov     rsi, CHIP8_RAM_VA + ROM_LOAD_ADDR
        mov     rdx, [lrf_len]
        call    sys_read            ; rax = bytes read or negative errno
        cmp     rax, [lrf_len]
        jne     .close_fail         ; short read or error: frozen size gate
        mov     edi, ebx
        call    sys_close
        test    rax, rax
        jnz     .fail
        pop     rdi
        pop     rsi
        pop     rbx
        ret
.close_fail:
        mov     edi, ebx
        call    sys_close
.fail:
        mov     ecx, EXIT_FS_LOAD
        call    sys_exit
endp

; blit the frame buffer to the fixed screen position (192, 224)
; out: rax = 0 on success, negative errno otherwise
sys_blit_frame:
        mov     eax, SYS_BLIT
        mov     rdi, CHIP8_FB_VA
        mov     esi, BLIT_DST_X
        mov     edx, BLIT_DST_Y
        mov     r10d, FB_WIDTH
        mov     r8d, FB_HEIGHT
        syscall
        ret

; FS-v1 file wrappers, numbers frozen by kernel/fs/api.inc (4..9); the
; APP-v1 api.inc stays untouched, the three numbers used here are
; mirrored locally and cross checked by the stage2-app gate.
; open: rdi = path, esi = flags -> rax = fd or negative errno
sys_open:
        mov     eax, SYS_FS_OPEN
        syscall
        ret
; read: edi = fd, rsi = buffer, edx = length -> rax = bytes or errno
sys_read:
        mov     eax, SYS_FS_READ
        syscall
        ret
; close: edi = fd -> rax = 0 or negative errno
sys_close:
        mov     eax, SYS_FS_CLOSE
        syscall
        ret

; ------------------------------------------------------------------
; inline data, read-only, inside the code image
; ------------------------------------------------------------------

; font set, 16 characters * 5 bytes, copied to RAM 0x000 at init
chip8_fontset:
        db 0xF0, 0x90, 0x90, 0x90, 0xF0             ; 0
        db 0x20, 0x60, 0x20, 0x20, 0x70             ; 1
        db 0xF0, 0x10, 0xF0, 0x80, 0xF0             ; 2
        db 0xF0, 0x10, 0xF0, 0x10, 0xF0             ; 3
        db 0x90, 0x90, 0xF0, 0x10, 0x10             ; 4
        db 0xF0, 0x80, 0xF0, 0x10, 0xF0             ; 5
        db 0xF0, 0x80, 0xF0, 0x90, 0xF0             ; 6
        db 0xF0, 0x10, 0x20, 0x40, 0x40             ; 7
        db 0xF0, 0x90, 0xF0, 0x90, 0xF0             ; 8
        db 0xF0, 0x90, 0xF0, 0x10, 0xF0             ; 9
        db 0xF0, 0x90, 0xF0, 0x90, 0x90             ; A
        db 0xE0, 0x90, 0xE0, 0x90, 0xE0             ; B
        db 0xF0, 0x80, 0x80, 0x80, 0xF0             ; C
        db 0xE0, 0x90, 0x90, 0x90, 0xE0             ; D
        db 0xF0, 0x80, 0xF0, 0x80, 0xF0             ; E
        db 0xF0, 0x80, 0xF0, 0x80, 0x80             ; F

; task 115: no ROM bytes live in the image any more. The frozen sizes
; stay as the exact-count read gate; the sha256 digest qwords in
; api.inc remain the gate expectations against the FS-read content.
ROM_IBM_SIZE    = 132
ROM_TETRIS_SIZE = 494
if ROM_IBM_SIZE > MEM_SIZE - ROM_LOAD_ADDR
        err 'ibm.ch8 does not fit into CHIP-8 RAM'
end if
if ROM_TETRIS_SIZE > MEM_SIZE - ROM_LOAD_ADDR
        err 'tetris1.ch8 does not fit into CHIP-8 RAM'
end if

; FS syscall numbers, mirror of the frozen kernel/fs/api.inc set
SYS_FS_OPEN  = 4
SYS_FS_READ  = 5
SYS_FS_CLOSE = 6
; load failure exit code, distinct from the frozen 2/3 fault codes
EXIT_FS_LOAD = 4

; image paths, NUL terminated, read-only inside the code domain
path_ibm:
        db '/ROMS/IBM.CH8', 0
path_tetris:
        db '/ROMS/TETRIS1.CH8', 0

; set 1 make scancode -> CHIP-8 key, 0xFF = unmapped, 128 entries
keymap:
        db 0xFF, 0xFF, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06 ; 0x00
        db 0x07, 0x08, 0x09, 0x00, 0xFF, 0xFF, 0xFF, 0xFF ; 0x08
        db 0xFF, 0xFF, 0x0E, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF ; 0x10
        db 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x0A, 0xFF ; 0x18
        db 0x0D, 0x0F, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF ; 0x20
        db 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x0C, 0xFF ; 0x28
        db 0x0B, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF ; 0x30
        db 72 dup 0xFF                                  ; 0x38..0x7F

; ------------------------------------------------------------------
; image size gate, the code domain is 16 KiB
; ------------------------------------------------------------------

app_image_end:
if app_image_end - APP_CODE_BASE > APP_CODE_LIMIT
        err 'user image exceeds the 16 KiB code domain'
end if
if start <> APP_CODE_BASE
        err 'entry must sit at the code domain base'
end if