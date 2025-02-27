;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;

use64

include 'struct.inc'
include 'proc64.inc'
include 'const.inc'

org VIRT_KERNEL_BASE

DEFAULT_STACK_SIZE = 4096*2

dq 'KERNEL64' ; magic
dq k64_entry - VIRT_KERNEL_BASE ; entry point's offset from base where it loaded
dq DEFAULT_STACK_SIZE ; how many bytes of stack we need
kernel_phys_start dq 0 ; bootloader will put here phys addr where it loaded kernel
kernel_phys_end dq 0 ; and phys end addr (including stack ofc)

PORT_COM1 = 0x3f8

macro outb port_, byte_  {
        mov     dx, port_
        mov     al, byte_
        out     dx, al
}

macro inb port_ {
        mov     dx, port_
        in      al, dx
}

; rcx - byte to write
; uses rdx
write_com1:
@@:
        inb     PORT_COM1 + 5
        and     al, 0x20
        test    al, al
        jz      @b
        outb    PORT_COM1, cl
        ret


; 64 bit kernel entry point
k64_entry:
        ; init com port:
        outb    PORT_COM1 + 1, 0x00
        outb    PORT_COM1 + 3, 0x80
        outb    PORT_COM1 + 0, 0x03
        outb    PORT_COM1 + 1, 0x00
        outb    PORT_COM1 + 3, 0x03
        outb    PORT_COM1 + 2, 0xC7
        outb    PORT_COM1 + 4, 0x0B
        outb    PORT_COM1 + 4, 0x1E
        outb    PORT_COM1 + 0, 0xAE
        ; check if serial is faulty (i.e: not same byte as sent)
        inb     PORT_COM1 + 0
        cmp     al, 0xAE
        jnz     .abyss
        ; set port in normal operation mode
        ; (not-loopback with IRQs enabled and OUT#1 and OUT#2 bits enabled)
        outb    PORT_COM1 + 4, 0x0F

        ;; try write some chars
        mov     rcx, 'H'
        call    write_com1
        mov     rcx, 'I'
        call    write_com1

        jmp     $
.abyss:


kmsg_hello      db 'Hello from KERNEL',13,10,0
kmsg_ripis      db 'RIP = ',13,10,0

puthex_ptr      dq 0
putstr_ptr      dq 0

