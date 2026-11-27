;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2026. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; CHIP-8 interpreter core, ported from the 32-bit KolibriOS emulator
;; by rgimad (programs/emulator/chip8/emu.inc, GNU GPL v2).
;;
;; proc32 stdcall became self-contained MS x64 procedures (api.inc),
;; pushad/popad became explicit callee-saved register pairs. Scalar
;; data references assemble as RIP-relative disp32 (fasmg does this
;; automatically). Indexed references cannot be RIP-relative and the
;; >4 GiB data base does not fit a disp32, so r11 holds APP_DATA_BASE
;; for the duration of a procedure and indexed operands are written
;; [r11 + index + (label - APP_DATA_BASE)]. SYSCALL clobbers r11, so
;; it is reloaded after any kernel call preceding an indexed access
;; (only key_drain in input.inc).
;;
;; Frozen semantics kept from the original: font set at RAM 0x000,
;; PC starts at 0x200, FX55/FX65 increment I by x+1 (old VIP school),
;; 8xy6/8xyE shift V[x] in place (modern school), Bnnn uses V0,
;; Fx0A does not advance PC without a key, Cxkk uses the MSVC LCG.
;;
;; Mandatory fixes over the original:
;;   8xy5/8xy7  VF is the no-borrow flag computed from the OLD operands
;;   DXYN       pixels outside 64x32 are skipped, no wrap, no write
;;              through; VF counts collisions of drawn pixels only
;;   2nnn/00EE  stack depth is bounded to 16, faults exit with code 3
;;   unknown    opcodes exit with code 2
;;
;; Key semantics: the key table holds true press/release levels fed by
;; raw set 1 scancodes (input.inc), so Ex9E/ExA1/Fx0A read levels and
;; never consume keys, unlike the event-based 32-bit original.
;;
;; All RAM indices are masked to 12 bits so no access can leave the
;; 4096 byte machine RAM (Bnnn sums and FX1E can overflow 0xFFF).
;;

; zero a memory block
; in: rcx = destination VA, edx = byte count
; destroys: rax, rcx, rdi, flags
proc memzero, mz_dest, mz_count
        push    rdi
        mov     rdi, [mz_dest]
        mov     rcx, [mz_count]
        xor     eax, eax
        rep stosb
        pop     rdi
        ret
endp

; initialize the machine: clear RAM/gfx/state, install the font set,
; PC = 0x200, fixed LCG seed, draw flag raised
; destroys: rax, rcx, rdx, flags (callee-saved registers preserved)
proc chip8_init
        push    rsi
        push    rdi
        call64  memzero, CHIP8_RAM_VA, MEM_SIZE
        call64  memzero, CHIP8_GFX_VA, GFX_SIZE
        call64  memzero, CHIP8_STATE_VA, 4096
        lea     rsi, [chip8_fontset]
        mov     rdi, CHIP8_RAM_VA + FONTSET_ADDR
        mov     ecx, KEY_COUNT * FONTSET_CHAR
        rep movsb
        mov     word [st_pc], ROM_LOAD_ADDR
        mov     byte [st_draw], 1
        mov     dword [st_rand], RAND_SEED
        pop     rdi
        pop     rsi
        ret
endp

; task 115: chip8_loadrom is gone, the ROM lands in RAM+0x200 directly
; through the FS read syscall (load_rom_fs in chip8.asm), no code-image
; copy step exists any more.

; one LCG step, MSVC rand(): next = next*214013 + 2531011 (mod 2^32),
; result = (next >> 16) & 0x7FFF
; out: eax = pseudo random 15 bit value
; destroys: eax, flags
chip8_rand:
        mov     eax, dword [st_rand]
        imul    eax, eax, RAND_MUL
        add     eax, RAND_ADD
        mov     dword [st_rand], eax
        shr     eax, 16
        and     eax, 0x7FFF
        ret

; draw an n-byte sprite from RAM[I] at (V[x], V[y]) into gfx
; in: rcx = column, edx = row, r8d = sprite height
; out: VF = 1 if any drawn pixel collided
; destroys: rax, rcx, rdx, r8, r9, flags (callee-saved preserved)
proc chip8_draw_sprite, draw_col, draw_row, draw_n
        push    rbx
        push    rsi
        push    rdi
        push    r12
        push    r13
        push    r14
        push    r15
        mov     r11, APP_DATA_BASE    ; data base for indexed operands
        mov     byte [st_v + 15], 0
        movzx   esi, word [st_i]
        mov     r12d, dword [draw_col]
        mov     r13d, dword [draw_row]
        mov     r14d, dword [draw_n]
        xor     r15d, r15d
.sprite_row:
        cmp     r15d, r14d
        jae     .sprite_done
        mov     eax, r13d
        add     eax, r15d
        cmp     eax, GFX_ROWS
        jae     .sprite_row_next      ; clipped row: skip entirely
        mov     ecx, esi
        add     ecx, r15d
        and     ecx, 0x0FFF
        movzx   ebx, byte [r11 + rcx + (chip8_ram - APP_DATA_BASE)]
        imul    edi, eax, GFX_COLS
        xor     r8d, r8d
.sprite_bit:
        cmp     r8d, 8
        jae     .sprite_row_next
        mov     eax, r12d
        add     eax, r8d
        cmp     eax, GFX_COLS
        jae     .sprite_bit_next      ; clipped pixel: skip, no wrap
        mov     r9d, eax            ; keep the pixel column col+bit
        mov     ecx, 7
        sub     ecx, r8d
        mov     eax, ebx
        shr     eax, cl
        test    eax, 1
        jz      .sprite_bit_next
        ; pixel offset = row*64 + (col+bit): eax now holds the shifted
        ; sprite byte, so the column saved in r9d is the only source
        lea     ecx, [rdi + r9]
        movzx   edx, byte [r11 + rcx + (chip8_gfx - APP_DATA_BASE)]
        test    edx, edx
        jz      .sprite_no_coll
        mov     byte [st_v + 15], 1
.sprite_no_coll:
        xor     edx, 1
        mov     byte [r11 + rcx + (chip8_gfx - APP_DATA_BASE)], dl
.sprite_bit_next:
        inc     r8d
        jmp     .sprite_bit
.sprite_row_next:
        inc     r15d
        jmp     .sprite_row
.sprite_done:
        pop     r15
        pop     r14
        pop     r13
        pop     r12
        pop     rdi
        pop     rsi
        pop     rbx
        ret
endp

; fetch, decode and execute exactly one instruction
; destroys: volatile registers, flags; callee-saved preserved
proc chip8_step
        push    rbx
        push    rsi
        push    rdi
        push    r12
        push    r13
        push    r14
        push    r15
        mov     r11, APP_DATA_BASE    ; data base for indexed operands
        ; fetch the big-endian opcode at PC, both indices masked
        movzx   ecx, word [st_pc]
        and     ecx, 0x0FFF
        movzx   eax, byte [r11 + rcx + (chip8_ram - APP_DATA_BASE)]
        shl     eax, 8
        lea     edx, [rcx + 1]
        and     edx, 0x0FFF
        movzx   edx, byte [r11 + rdx + (chip8_ram - APP_DATA_BASE)]
        or      eax, edx
        mov     word [st_opcode], ax
        ; decode the fields once
        mov     r12d, eax
        shr     r12d, 8
        and     r12d, 0x0F            ; x
        mov     r13d, eax
        shr     r13d, 4
        and     r13d, 0x0F            ; y
        mov     r14d, eax
        and     r14d, 0x0F            ; n
        mov     r15d, eax
        and     r15d, 0xFF            ; kk
        mov     ebx, eax
        and     ebx, 0x0FFF           ; nnn
        ; dispatch on the top nibble
        shr     eax, 12
        cmp     eax, 0x0
        je      .op0
        cmp     eax, 0x1
        je      .op1
        cmp     eax, 0x2
        je      .op2
        cmp     eax, 0x3
        je      .op3
        cmp     eax, 0x4
        je      .op4
        cmp     eax, 0x5
        je      .op5
        cmp     eax, 0x6
        je      .op6
        cmp     eax, 0x7
        je      .op7
        cmp     eax, 0x8
        je      .op8
        cmp     eax, 0x9
        je      .op9
        cmp     eax, 0xA
        je      .opA
        cmp     eax, 0xB
        je      .opB
        cmp     eax, 0xC
        je      .opC
        cmp     eax, 0xD
        je      .opD
        cmp     eax, 0xE
        je      .opE
        cmp     eax, 0xF
        je      .opF
        jmp     .bad_opcode

.op0:
        cmp     r15d, 0xE0
        je      .cls
        cmp     r15d, 0xEE
        je      .ret_op
        jmp     .bad_opcode
.cls:
        mov     rdi, CHIP8_GFX_VA
        xor     eax, eax
        mov     ecx, GFX_SIZE
        rep stosb
        mov     byte [st_draw], 1
        jmp     .pc2
.ret_op:
        movzx   ecx, word [st_sp]
        test    ecx, ecx
        jz      .stack_fault          ; return with empty stack
        dec     ecx
        mov     word [st_sp], cx
        movzx   eax, word [r11 + rcx*2 + (st_stack - APP_DATA_BASE)]
        mov     word [st_pc], ax
        jmp     .done

.op1:
        mov     word [st_pc], bx
        jmp     .done

.op2:
        movzx   ecx, word [st_sp]
        cmp     ecx, STACK_DEPTH
        jae     .stack_fault          ; call with full stack
        movzx   eax, word [st_pc]
        add     eax, 2
        mov     word [r11 + rcx*2 + (st_stack - APP_DATA_BASE)], ax
        inc     ecx
        mov     word [st_sp], cx
        mov     word [st_pc], bx
        jmp     .done

.op3:
        mov     ecx, r12d
        mov     al, byte [r11 + rcx + (st_v - APP_DATA_BASE)]
        cmp     al, r15b
        je      .pc4
        jmp     .pc2

.op4:
        mov     ecx, r12d
        mov     al, byte [r11 + rcx + (st_v - APP_DATA_BASE)]
        cmp     al, r15b
        jne     .pc4
        jmp     .pc2

.op5:
        test    r14d, r14d
        jnz     .bad_opcode           ; 5xy0 only
        mov     ecx, r12d
        mov     al, byte [r11 + rcx + (st_v - APP_DATA_BASE)]
        mov     edx, r13d
        cmp     al, byte [r11 + rdx + (st_v - APP_DATA_BASE)]
        je      .pc4
        jmp     .pc2

.op6:
        mov     ecx, r12d
        mov     byte [r11 + rcx + (st_v - APP_DATA_BASE)], r15b
        jmp     .pc2

.op7:
        mov     ecx, r12d
        add     byte [r11 + rcx + (st_v - APP_DATA_BASE)], r15b
        jmp     .pc2

.op8:
        cmp     r14d, 0x0
        je      .alu0
        cmp     r14d, 0x1
        je      .alu1
        cmp     r14d, 0x2
        je      .alu2
        cmp     r14d, 0x3
        je      .alu3
        cmp     r14d, 0x4
        je      .alu4
        cmp     r14d, 0x5
        je      .alu5
        cmp     r14d, 0x6
        je      .alu6
        cmp     r14d, 0x7
        je      .alu7
        cmp     r14d, 0xE
        je      .aluE
        jmp     .bad_opcode
.alu0:
        mov     ecx, r12d
        mov     edx, r13d
        mov     al, byte [r11 + rdx + (st_v - APP_DATA_BASE)]
        mov     byte [r11 + rcx + (st_v - APP_DATA_BASE)], al
        jmp     .pc2
.alu1:
        mov     ecx, r12d
        mov     edx, r13d
        mov     al, byte [r11 + rcx + (st_v - APP_DATA_BASE)]
        or      al, byte [r11 + rdx + (st_v - APP_DATA_BASE)]
        mov     byte [r11 + rcx + (st_v - APP_DATA_BASE)], al
        jmp     .pc2
.alu2:
        mov     ecx, r12d
        mov     edx, r13d
        mov     al, byte [r11 + rcx + (st_v - APP_DATA_BASE)]
        and     al, byte [r11 + rdx + (st_v - APP_DATA_BASE)]
        mov     byte [r11 + rcx + (st_v - APP_DATA_BASE)], al
        jmp     .pc2
.alu3:
        mov     ecx, r12d
        mov     edx, r13d
        mov     al, byte [r11 + rcx + (st_v - APP_DATA_BASE)]
        xor     al, byte [r11 + rdx + (st_v - APP_DATA_BASE)]
        mov     byte [r11 + rcx + (st_v - APP_DATA_BASE)], al
        jmp     .pc2
.alu4:
        mov     ecx, r12d
        mov     edx, r13d
        movzx   eax, byte [r11 + rcx + (st_v - APP_DATA_BASE)]
        movzx   r8d, byte [r11 + rdx + (st_v - APP_DATA_BASE)]
        add     eax, r8d
        mov     byte [r11 + rcx + (st_v - APP_DATA_BASE)], al
        shr     eax, 8                ; carry out of bit 7
        mov     byte [st_v + 15], al
        jmp     .pc2
.alu5:
        ; VF = no-borrow, computed from the old operands (fix)
        mov     ecx, r12d
        mov     edx, r13d
        movzx   eax, byte [r11 + rcx + (st_v - APP_DATA_BASE)]
        movzx   r8d, byte [r11 + rdx + (st_v - APP_DATA_BASE)]
        mov     r9d, eax
        sub     r9d, r8d
        cmp     eax, r8d
        setae   r10b
        mov     byte [r11 + rcx + (st_v - APP_DATA_BASE)], r9b
        mov     byte [st_v + 15], r10b
        jmp     .pc2
.alu6:
        ; in-place shift of V[x], VF = old bit 0 (modern school)
        mov     ecx, r12d
        mov     al, byte [r11 + rcx + (st_v - APP_DATA_BASE)]
        mov     dl, al
        and     dl, 1
        mov     byte [st_v + 15], dl
        shr     al, 1
        mov     byte [r11 + rcx + (st_v - APP_DATA_BASE)], al
        jmp     .pc2
.alu7:
        ; V[x] = V[y] - V[x], VF = no-borrow from old operands (fix)
        mov     ecx, r12d
        mov     edx, r13d
        movzx   eax, byte [r11 + rdx + (st_v - APP_DATA_BASE)]
        movzx   r8d, byte [r11 + rcx + (st_v - APP_DATA_BASE)]
        mov     r9d, eax
        sub     r9d, r8d
        cmp     eax, r8d
        setae   r10b
        mov     byte [r11 + rcx + (st_v - APP_DATA_BASE)], r9b
        mov     byte [st_v + 15], r10b
        jmp     .pc2
.aluE:
        ; in-place shift of V[x], VF = old bit 7 (modern school)
        mov     ecx, r12d
        mov     al, byte [r11 + rcx + (st_v - APP_DATA_BASE)]
        mov     dl, al
        shr     dl, 7
        mov     byte [st_v + 15], dl
        shl     al, 1
        mov     byte [r11 + rcx + (st_v - APP_DATA_BASE)], al
        jmp     .pc2

.op9:
        test    r14d, r14d
        jnz     .bad_opcode           ; 9xy0 only
        mov     ecx, r12d
        mov     al, byte [r11 + rcx + (st_v - APP_DATA_BASE)]
        mov     edx, r13d
        cmp     al, byte [r11 + rdx + (st_v - APP_DATA_BASE)]
        jne     .pc4
        jmp     .pc2

.opA:
        mov     word [st_i], bx
        jmp     .pc2

.opB:
        movzx   eax, byte [st_v]      ; Bnnn uses V0
        add     eax, ebx
        and     eax, 0x0FFF
        mov     word [st_pc], ax
        jmp     .done

.opC:
        call    chip8_rand
        and     al, r15b
        mov     ecx, r12d
        mov     byte [r11 + rcx + (st_v - APP_DATA_BASE)], al
        jmp     .pc2

.opD:
        mov     ecx, r12d
        movzx   ecx, byte [r11 + rcx + (st_v - APP_DATA_BASE)]
        mov     edx, r13d
        movzx   edx, byte [r11 + rdx + (st_v - APP_DATA_BASE)]
        mov     r8d, r14d
        call64  chip8_draw_sprite, rcx, rdx, r8
        mov     byte [st_draw], 1
        jmp     .pc2

.opE:
        cmp     r15d, 0x9E
        je      .key_pressed
        cmp     r15d, 0xA1
        je      .key_not_pressed
        jmp     .bad_opcode
.key_pressed:
        mov     ecx, r12d
        movzx   ecx, byte [r11 + rcx + (st_v - APP_DATA_BASE)]
        and     ecx, 0x0F
        cmp     byte [r11 + rcx + (st_key - APP_DATA_BASE)], 0
        jne     .pc4
        jmp     .pc2
.key_not_pressed:
        mov     ecx, r12d
        movzx   ecx, byte [r11 + rcx + (st_v - APP_DATA_BASE)]
        and     ecx, 0x0F
        cmp     byte [r11 + rcx + (st_key - APP_DATA_BASE)], 0
        je      .pc4
        jmp     .pc2

.opF:
        cmp     r15d, 0x07
        je      .fx07
        cmp     r15d, 0x0A
        je      .fx0A
        cmp     r15d, 0x15
        je      .fx15
        cmp     r15d, 0x18
        je      .fx18
        cmp     r15d, 0x1E
        je      .fx1E
        cmp     r15d, 0x29
        je      .fx29
        cmp     r15d, 0x33
        je      .fx33
        cmp     r15d, 0x55
        je      .fx55
        cmp     r15d, 0x65
        je      .fx65
        jmp     .bad_opcode
.fx07:
        mov     ecx, r12d
        mov     al, byte [st_delay]
        mov     byte [r11 + rcx + (st_v - APP_DATA_BASE)], al
        jmp     .pc2
.fx0A:
        ; no key pressed: PC does not advance (frozen original school)
        xor     ecx, ecx
.fx0A_scan:
        cmp     ecx, KEY_COUNT
        jae     .done
        cmp     byte [r11 + rcx + (st_key - APP_DATA_BASE)], 0
        jne     .fx0A_found
        inc     ecx
        jmp     .fx0A_scan
.fx0A_found:
        mov     edx, r12d
        mov     byte [r11 + rdx + (st_v - APP_DATA_BASE)], cl
        jmp     .pc2
.fx15:
        mov     ecx, r12d
        mov     al, byte [r11 + rcx + (st_v - APP_DATA_BASE)]
        mov     byte [st_delay], al
        jmp     .pc2
.fx18:
        mov     ecx, r12d
        mov     al, byte [r11 + rcx + (st_v - APP_DATA_BASE)]
        mov     byte [st_sound], al
        jmp     .pc2
.fx1E:
        mov     ecx, r12d
        movzx   eax, byte [r11 + rcx + (st_v - APP_DATA_BASE)]
        add     word [st_i], ax
        jmp     .pc2
.fx29:
        mov     ecx, r12d
        movzx   eax, byte [r11 + rcx + (st_v - APP_DATA_BASE)]
        imul    eax, eax, FONTSET_CHAR
        add     eax, FONTSET_ADDR
        mov     word [st_i], ax
        jmp     .pc2
.fx33:
        mov     ecx, r12d
        movzx   eax, byte [r11 + rcx + (st_v - APP_DATA_BASE)]
        movzx   esi, word [st_i]
        xor     edx, edx
        mov     ecx, 100
        div     ecx
        mov     ecx, esi
        and     ecx, 0x0FFF
        mov     byte [r11 + rcx + (chip8_ram - APP_DATA_BASE)], al
        mov     eax, edx
        xor     edx, edx
        mov     ecx, 10
        div     ecx
        lea     ecx, [rsi + 1]
        and     ecx, 0x0FFF
        mov     byte [r11 + rcx + (chip8_ram - APP_DATA_BASE)], al
        lea     ecx, [rsi + 2]
        and     ecx, 0x0FFF
        mov     byte [r11 + rcx + (chip8_ram - APP_DATA_BASE)], dl
        jmp     .pc2
.fx55:
        movzx   esi, word [st_i]
        xor     ecx, ecx
.fx55_loop:
        cmp     ecx, r12d
        ja      .fx55_end
        mov     al, byte [r11 + rcx + (st_v - APP_DATA_BASE)]
        lea     edx, [rsi + rcx]
        and     edx, 0x0FFF
        mov     byte [r11 + rdx + (chip8_ram - APP_DATA_BASE)], al
        inc     ecx
        jmp     .fx55_loop
.fx55_end:
        lea     eax, [r12 + 1]
        add     word [st_i], ax       ; old VIP school: I += x + 1
        jmp     .pc2
.fx65:
        movzx   esi, word [st_i]
        xor     ecx, ecx
.fx65_loop:
        cmp     ecx, r12d
        ja      .fx65_end
        lea     edx, [rsi + rcx]
        and     edx, 0x0FFF
        mov     al, byte [r11 + rdx + (chip8_ram - APP_DATA_BASE)]
        mov     byte [r11 + rcx + (st_v - APP_DATA_BASE)], al
        inc     ecx
        jmp     .fx65_loop
.fx65_end:
        lea     eax, [r12 + 1]
        add     word [st_i], ax       ; old VIP school: I += x + 1
        jmp     .pc2

.bad_opcode:
        mov     ecx, EXIT_BAD_OPCODE
        call    sys_exit
.stack_fault:
        mov     ecx, EXIT_STACK_FAULT
        call    sys_exit

.pc2:
        add     word [st_pc], 2
        jmp     .done
.pc4:
        add     word [st_pc], 4
.done:
        pop     r15
        pop     r14
        pop     r13
        pop     r12
        pop     rdi
        pop     rsi
        pop     rbx
        ret
endp
