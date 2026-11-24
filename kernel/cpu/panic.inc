;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Bounded COM1 diagnostics and the panic path.
;;
;; Every loop here has a hard bound: the transmit poll spends at most
;; CPU_COM1_TX_SPIN iterations, the loopback probe at most CPU_COM1_RX_SPIN,
;; puts writes at most the caller's cap, puthex at most 16 digits. A missing
;; UART therefore returns EIO instead of hanging the machine, which matters
;; most exactly where the console cannot be trusted anymore.
;;
;; cpu_panic never returns. It switches to its own 16 KiB stack before it
;; writes anything, so a corrupted kernel stack cannot turn a panic into a
;; triple fault, and a second panic (recursion, a fault inside the printer)
;; only halts without touching the UART again.
;;

; ------------------------------------------------------------------
; bring COM1 to 115200 8N1 and prove that it answers a local loopback
; in
; out
; eax = 0 on success, EIO when the UART does not answer in time
; ------------------------------------------------------------------

cpu_com1_init:
        push    rbp
        mov     rbp, rsp
        sub     rsp, CPU_STACK_FRAME
        ; no interrupts, DLAB on, divisor 1, 8N1
        mov     dx, CPU_COM1_PORT + 1
        xor     al, al
        out     dx, al
        mov     dx, CPU_COM1_PORT + 3
        mov     al, 0x80
        out     dx, al
        mov     dx, CPU_COM1_PORT + 0
        mov     al, 1
        out     dx, al
        mov     dx, CPU_COM1_PORT + 1
        xor     al, al
        out     dx, al
        mov     dx, CPU_COM1_PORT + 3
        mov     al, 0x03
        out     dx, al
        mov     dx, CPU_COM1_PORT + 2
        mov     al, 0xC7
        out     dx, al
        ; local loopback: MCR = OUT2 | LOOPBACK
        mov     dx, CPU_COM1_PORT + 4
        mov     al, 0x1E
        out     dx, al
        ; bounded wait for an empty transmit register
        mov     ecx, CPU_COM1_TX_SPIN
.wait_tx:
        mov     dx, CPU_COM1_PORT + 5
        in      al, dx
        test    al, 0x20
        jnz     .tx_ready
        dec     ecx
        jnz     .wait_tx
        jmp     .failed
.tx_ready:
        mov     dx, CPU_COM1_PORT
        mov     al, 0xAE
        out     dx, al
        ; bounded wait for the byte to come back
        mov     ecx, CPU_COM1_RX_SPIN
.wait_rx:
        mov     dx, CPU_COM1_PORT + 5
        in      al, dx
        test    al, 0x01
        jnz     .rx_ready
        dec     ecx
        jnz     .wait_rx
        jmp     .failed
.rx_ready:
        mov     dx, CPU_COM1_PORT
        in      al, dx
        cmp     al, 0xAE
        jne     .failed
        ; back to normal operation: DTR | RTS | OUT2
        mov     dx, CPU_COM1_PORT + 4
        mov     al, 0x0F
        out     dx, al
        mov     byte [cpu_com1_ready], 1
        xor     eax, eax
        leave
        ret
.failed:
        mov     dx, CPU_COM1_PORT + 4
        mov     al, 0x0F
        out     dx, al
        mov     eax, CPU_EIO
        leave
        ret

; ------------------------------------------------------------------
; write one byte, waiting for the transmit register within a bound
; in
; rcx = byte to write
; out
; eax = 0 on success, EIO when the transmitter stays busy
; rdx = 1 when the byte was written, 0 otherwise
; ------------------------------------------------------------------

cpu_com1_putc:
        push    rbp
        mov     rbp, rsp
        sub     rsp, CPU_STACK_FRAME
        mov     [rbp-8], rcx
        mov     ecx, CPU_COM1_TX_SPIN
.wait:
        mov     dx, CPU_COM1_PORT + 5
        in      al, dx
        test    al, 0x20
        jnz     .ready
        dec     ecx
        jnz     .wait
        mov     eax, CPU_EIO
        xor     edx, edx
        leave
        ret
.ready:
        mov     rax, [rbp-8]
        mov     dx, CPU_COM1_PORT
        out     dx, al
        xor     eax, eax
        mov     edx, 1
        leave
        ret

; ------------------------------------------------------------------
; write a zero terminated string, never more than the caller allows
; in
; rcx = string, rdx = maximum number of bytes to write
; out
; eax = 0 on success, EINVAL for a null string or a zero cap,
;       EOVERFLOW when the cap was reached before the terminator, EIO on a
;       dead UART
; rdx = bytes written
; ------------------------------------------------------------------

cpu_com1_puts:
        push    rbp
        mov     rbp, rsp
        sub     rsp, CPU_STACK_FRAME
        test    rcx, rcx
        jz      .invalid
        test    rdx, rdx
        jz      .invalid
        mov     [rbp-8], rcx
        mov     [rbp-16], rdx
        mov     qword [rbp-24], 0
.loop:
        mov     rax, [rbp-8]
        movzx   ecx, byte [rax]
        test    ecx, ecx
        jz      .done
        cmp     qword [rbp-16], 0
        je      .overflow
        call    cpu_com1_putc
        test    eax, eax
        jnz     .fail
        inc     qword [rbp-8]
        dec     qword [rbp-16]
        inc     qword [rbp-24]
        jmp     .loop
.done:
        xor     eax, eax
        mov     rdx, [rbp-24]
        leave
        ret
.overflow:
        mov     eax, CPU_EOVERFLOW
        mov     rdx, [rbp-24]
        leave
        ret
.fail:
        mov     rdx, [rbp-24]
        leave
        ret
.invalid:
        mov     eax, CPU_EINVAL
        xor     edx, edx
        leave
        ret

; ------------------------------------------------------------------
; write a value as 0x followed by a fixed number of hex digits
; in
; rcx = value, rdx = number of digits, 1..16, most significant first
; out
; eax = 0 on success, EINVAL for a bad digit count, EIO on a dead UART
; rdx = bytes written
; ------------------------------------------------------------------

cpu_com1_puthex:
        push    rbp
        mov     rbp, rsp
        sub     rsp, CPU_STACK_FRAME
        test    rdx, rdx
        jz      .invalid
        cmp     rdx, CPU_PUTHEX_MAX
        ja      .invalid
        mov     [rbp-8], rcx
        mov     [rbp-16], rdx
        mov     qword [rbp-24], 0
        mov     rax, rdx
        dec     rax
        shl     rax, 2
        mov     [rbp-32], rax
        mov     rcx, '0'
        call    cpu_com1_putc
        test    eax, eax
        jnz     .fail
        inc     qword [rbp-24]
        mov     rcx, 'x'
        call    cpu_com1_putc
        test    eax, eax
        jnz     .fail
        inc     qword [rbp-24]
.digit:
        mov     rcx, [rbp-32]
        mov     rax, [rbp-8]
        shr     rax, cl
        and     eax, 0x0F
        cmp     eax, 10
        jae     .alpha
        add     eax, '0'
        jmp     .have
.alpha:
        add     eax, 'A' - 10
.have:
        mov     ecx, eax
        call    cpu_com1_putc
        test    eax, eax
        jnz     .fail
        inc     qword [rbp-24]
        sub     qword [rbp-32], 4
        jns     .digit
        xor     eax, eax
        mov     rdx, [rbp-24]
        leave
        ret
.fail:
        mov     rdx, [rbp-24]
        leave
        ret
.invalid:
        mov     eax, CPU_EINVAL
        xor     edx, edx
        leave
        ret

; ------------------------------------------------------------------
; stop the machine, the only exit of a fatal path
; ------------------------------------------------------------------

cpu_halt_forever:
        cli
.halt:
        hlt
        jmp     .halt

; ------------------------------------------------------------------
; report a fatal condition on its own stack and halt
; in
; rcx = zero terminated message, may be null
; rdx = CPU_TRAP_FRAME to report, may be null
; r8  = reason, a status code
; out
; never returns
; ------------------------------------------------------------------

cpu_panic:
        cli
        cmp     byte [cpu_panic_active], 0
        jne     cpu_halt_forever
        mov     byte [cpu_panic_active], 1
        mov     [cpu_panic_message], rcx
        mov     [cpu_panic_frame], rdx
        mov     [cpu_panic_reason], r8
        ; a private stack: the current one may be the reason for the panic
        mov     rsp, cpu_panic_stack_top
        and     rsp, not 15
        sub     rsp, 32
        mov     rcx, msg_panic_head
        mov     rdx, CPU_PUTS_MAX
        call    cpu_com1_puts
        mov     rcx, msg_panic_reason
        mov     rdx, CPU_PUTS_MAX
        call    cpu_com1_puts
        mov     rcx, [cpu_panic_reason]
        mov     rdx, CPU_PUTHEX_DIGITS
        call    cpu_com1_puthex
        mov     rcx, msg_newline
        mov     rdx, CPU_PUTS_MAX
        call    cpu_com1_puts
        mov     rax, [cpu_panic_frame]
        test    rax, rax
        jz      .message
        mov     rcx, msg_panic_vector
        mov     rdx, CPU_PUTS_MAX
        call    cpu_com1_puts
        mov     rax, [cpu_panic_frame]
        mov     rcx, [rax + CPU_FRAME_VECTOR]
        mov     rdx, CPU_PUTHEX_DIGITS
        call    cpu_com1_puthex
        mov     rcx, msg_newline
        mov     rdx, CPU_PUTS_MAX
        call    cpu_com1_puts
        mov     rcx, msg_panic_error
        mov     rdx, CPU_PUTS_MAX
        call    cpu_com1_puts
        mov     rax, [cpu_panic_frame]
        mov     rcx, [rax + CPU_FRAME_ERROR]
        mov     rdx, CPU_PUTHEX_DIGITS
        call    cpu_com1_puthex
        mov     rcx, msg_newline
        mov     rdx, CPU_PUTS_MAX
        call    cpu_com1_puts
        mov     rcx, msg_panic_rip
        mov     rdx, CPU_PUTS_MAX
        call    cpu_com1_puts
        mov     rax, [cpu_panic_frame]
        mov     rcx, [rax + CPU_FRAME_RIP]
        mov     rdx, CPU_PUTHEX_DIGITS
        call    cpu_com1_puthex
        mov     rcx, msg_newline
        mov     rdx, CPU_PUTS_MAX
        call    cpu_com1_puts
        mov     rcx, msg_panic_cr2
        mov     rdx, CPU_PUTS_MAX
        call    cpu_com1_puts
        mov     rax, [cpu_panic_frame]
        mov     rcx, [rax + CPU_FRAME_CR2]
        mov     rdx, CPU_PUTHEX_DIGITS
        call    cpu_com1_puthex
        mov     rcx, msg_newline
        mov     rdx, CPU_PUTS_MAX
        call    cpu_com1_puts
.message:
        mov     rax, [cpu_panic_message]
        test    rax, rax
        jz      cpu_halt_forever
        mov     rcx, msg_panic_message
        mov     rdx, CPU_PUTS_MAX
        call    cpu_com1_puts
        mov     rcx, [cpu_panic_message]
        mov     rdx, CPU_PUTS_MAX
        call    cpu_com1_puts
        mov     rcx, msg_newline
        mov     rdx, CPU_PUTS_MAX
        call    cpu_com1_puts
        jmp     cpu_halt_forever
