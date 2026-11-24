;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; 8042 PS/2 controller bring-up and the bounded port primitives.
;;
;; Frozen sequence: disable both ports, flush the output buffer, read
;; the command configuration byte, force KIE|MIE|translation on and the
;; clock disables off, write it back, enable both ports, then the mouse
;; reset (0xFF, ACK + 0xAA + ID, 0xFE resend capped at 5), defaults
;; (0xF6, ACK) and enable (0xF4, ACK). Every port wait is bounded; a
;; failure is INPUT_EIO, the foundation turns it into cpu_panic: the
;; IOAPIC and the 8042 are mandatory platform devices, there is no skip.
;;
;; All helpers here are leaves: caller saved registers only, IF stays 0.
;;

; ------------------------------------------------------------------
; wait until the input buffer is empty (IBF = 0)
; in  ecx = spin budget       out eax = INPUT_OK or INPUT_EIO
; a zero budget fails immediately, it never wraps into 2^32 spins
; ------------------------------------------------------------------

input_ps2_wait_ibf:
        test    ecx, ecx
        jz      .timeout                ; zero budget: no spin at all
.loop:
        in      al, INPUT_PS2_STATUS
        test    al, INPUT_PS2_STAT_IBF
        jz      .ok
        dec     ecx
        jnz     .loop
.timeout:
        mov     eax, INPUT_EIO
        ret
.ok:
        xor     eax, eax
        ret

; ------------------------------------------------------------------
; wait until the output buffer is full (OBF = 1)
; in  ecx = spin budget       out eax = INPUT_OK or INPUT_EIO
; a zero budget fails immediately, it never wraps into 2^32 spins
; ------------------------------------------------------------------

input_ps2_wait_obf:
        test    ecx, ecx
        jz      .timeout                ; zero budget: no spin at all
.loop:
        in      al, INPUT_PS2_STATUS
        test    al, INPUT_PS2_STAT_OBF
        jnz     .ok
        dec     ecx
        jnz     .loop
.timeout:
        mov     eax, INPUT_EIO
        ret
.ok:
        xor     eax, eax
        ret

; ------------------------------------------------------------------
; send a command byte to port 0x64
; in  r8b = command           out eax = INPUT_OK or INPUT_EIO
; ------------------------------------------------------------------

input_ps2_cmd:
        mov     ecx, INPUT_PORT_SPIN
        call    input_ps2_wait_ibf
        test    eax, eax
        jnz     .out
        mov     al, r8b
        out     INPUT_PS2_CMD, al
        xor     eax, eax
.out:
        ret

; ------------------------------------------------------------------
; send a data byte to port 0x60
; in  r8b = byte              out eax = INPUT_OK or INPUT_EIO
; ------------------------------------------------------------------

input_ps2_write_data:
        mov     ecx, INPUT_PORT_SPIN
        call    input_ps2_wait_ibf
        test    eax, eax
        jnz     .out
        mov     al, r8b
        out     INPUT_PS2_DATA, al
        xor     eax, eax
.out:
        ret

; ------------------------------------------------------------------
; read one data byte from port 0x60
; out eax = the byte (0..255) or INPUT_EIO on timeout
; ------------------------------------------------------------------

input_ps2_read_data:
        mov     ecx, INPUT_PORT_SPIN
        call    input_ps2_wait_obf
        test    eax, eax
        jnz     .out
        in      al, INPUT_PS2_DATA
        movzx   eax, al
.out:
        ret

; ------------------------------------------------------------------
; drain the output buffer, bounded by INPUT_FLUSH_MAX reads
; out eax = the number of bytes drained (0 when it was already empty);
;     the masked self test leg uses it as arrival evidence
; ------------------------------------------------------------------

input_ps2_flush:
        mov     ecx, INPUT_FLUSH_MAX
.loop:
        in      al, INPUT_PS2_STATUS
        test    al, INPUT_PS2_STAT_OBF
        jz      .ok
        in      al, INPUT_PS2_DATA
        dec     ecx
        jnz     .loop
.ok:
        mov     eax, INPUT_FLUSH_MAX
        sub     eax, ecx
        ret

; ------------------------------------------------------------------
; send one byte to the mouse through the 0xD4 prefix
; in  r9b = mouse command     out eax = INPUT_OK or INPUT_EIO
; ------------------------------------------------------------------

input_ps2_mouse_write:
        mov     r8b, INPUT_PS2_CMD_WRITE_AUX
        call    input_ps2_cmd
        test    eax, eax
        jnz     .out
        mov     r8b, r9b
        call    input_ps2_write_data
.out:
        ret

; ------------------------------------------------------------------
; send a mouse command and wait for the ACK, 0xFE triggers a resend
; in  r9b = mouse command     out eax = INPUT_OK or INPUT_EIO
; the resend budget is INPUT_ACK_RETRY, every read is bounded
; ------------------------------------------------------------------

input_ps2_mouse_ack:
        mov     r10d, INPUT_ACK_RETRY
.retry:
        call    input_ps2_mouse_write
        test    eax, eax
        jnz     .out
        call    input_ps2_read_data
        test    eax, eax
        js      .out
        cmp     eax, INPUT_MOUSE_RESEND
        jne     .check
        dec     r10d
        jnz     .retry
        mov     eax, INPUT_EIO
        jmp     .out
.check:
        cmp     eax, INPUT_MOUSE_ACK
        je      .ok
        mov     eax, INPUT_EIO
        jmp     .out
.ok:
        xor     eax, eax
.out:
        ret

; ------------------------------------------------------------------
; the frozen bring-up sequence
; in
; nothing; IF must be 0, both RTEs stay masked throughout
; out
; eax = INPUT_OK or INPUT_EIO, rdx = 0
; ------------------------------------------------------------------

input_ps2_bringup:
        input_enter
        cld

        ; disable both ports so nothing races the configuration
        mov     r8b, INPUT_PS2_CMD_DISABLE_KBD
        call    input_ps2_cmd
        test    eax, eax
        jnz     .eio
        mov     r8b, INPUT_PS2_CMD_DISABLE_AUX
        call    input_ps2_cmd
        test    eax, eax
        jnz     .eio

        ; flush whatever the firmware left behind
        call    input_ps2_flush

        ; read the CCB, force the frozen bits, write it back
        mov     r8b, INPUT_PS2_CMD_READ_CCB
        call    input_ps2_cmd
        test    eax, eax
        jnz     .eio
        call    input_ps2_read_data
        test    eax, eax
        js      .eio
        or      eax, INPUT_CCB_SET
        and     eax, not INPUT_CCB_CLEAR
        mov     r12d, eax               ; keep for the write back
        mov     r8b, INPUT_PS2_CMD_WRITE_CCB
        call    input_ps2_cmd
        test    eax, eax
        jnz     .eio
        mov     r8b, r12b
        call    input_ps2_write_data
        test    eax, eax
        jnz     .eio

        ; enable both ports
        mov     r8b, INPUT_PS2_CMD_ENABLE_KBD
        call    input_ps2_cmd
        test    eax, eax
        jnz     .eio
        mov     r8b, INPUT_PS2_CMD_ENABLE_AUX
        call    input_ps2_cmd
        test    eax, eax
        jnz     .eio

        ; mouse reset: ACK, then the 0xAA self test pass, then the ID
        mov     r9b, INPUT_MOUSE_CMD_RESET
        call    input_ps2_mouse_ack
        test    eax, eax
        jnz     .eio
        call    input_ps2_read_data
        cmp     eax, INPUT_MOUSE_RESET_OK
        jne     .eio
        call    input_ps2_read_data
        test    eax, eax
        js      .eio
        mov     [input_mouse_id], rax
        cmp     eax, INPUT_MOUSE_ID_STANDARD
        jne     .eio

        ; defaults, then enable reporting, each ACK'd
        mov     r9b, INPUT_MOUSE_CMD_DEFAULT
        call    input_ps2_mouse_ack
        test    eax, eax
        jnz     .eio
        mov     r9b, INPUT_MOUSE_CMD_ENABLE
        call    input_ps2_mouse_ack
        test    eax, eax
        jnz     .eio

        ; drain any residual byte so the legs start from a clean buffer
        call    input_ps2_flush

        xor     eax, eax
        xor     edx, edx
        input_leave
        ret
.eio:
        mov     eax, INPUT_EIO
        xor     edx, edx
        input_leave
        ret
