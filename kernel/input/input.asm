;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; IRQ-v2 input layer: the module aggregator.
;;
;; Contents, in this order:
;;   api.inc       constants, layout assertions, register helper macros
;;   ioapic.inc    the indirect window, VER and the masked RTE bring-up
;;   ps2.inc       the bounded 8042 primitives and the frozen bring-up
;;   keyboard.inc  the keyboard IRQ leaf and the en_US set 1 key maps
;;   mouse.inc     the mouse IRQ leaf, the 3 byte packet state machine
;;   selftest.inc  the seven legs and the host injection handshake
;;   this file     input_init
;;   data.inc      every mutable byte, 4 KiB aligned at both ends
;;
;; The includer sets use64 and org and must include cpu/cpu.asm,
;; mm/mm.asm and irq/irq.asm first: this module calls vmm_map_mmio,
;; irq_sti_wait, cpu_panic and the bounded console, reads the public
;; IRQ evidence labels, and nothing else outside itself. The handlers
;; are called by irq_dispatch (forward references resolved at link of
;; the flat image); input_init registers nothing with the CPU layer.
;; The data region stays last, no code may follow input_data_end.
;;
;; The module contains zero STI instructions and never writes the
;; LAPIC EOI: the IRQ layer owns both.
;;
;; Standalone assembly (this is what the layout tests do) sets
;; INPUT_HOSTED and supplies the origin; a kernel include leaves both
;; alone. The .inc files carry no org and no use64 of their own.
;;
;; Include it exactly once per image.
;;

if ~ defined INPUT_HOSTED
        format binary
        use64
        org 0xFFFFFFFF80000000
end if

input_code_start:

include 'api.inc'
include 'ioapic.inc'
include 'ps2.inc'
include 'keyboard.inc'
include 'mouse.inc'
include 'selftest.inc'

; ------------------------------------------------------------------
; bring the input layer up, in the frozen order
; in
; nothing; cpu_init, pmm_init, vmm_init, heap_init and irq_init must
; have succeeded, IF must be 0
; out
; eax = INPUT_OK, or a signed status the foundation turns into
; cpu_panic: the IOAPIC and the 8042 are mandatory platform devices,
; there is no skip semantics here
;       EPERM     IF was set on entry
;       EALREADY  input_init already succeeded once
;       anything vmm_map_mmio reports
;       EIO       IOAPIC VER/RTE or the 8042/mouse bring-up failed
; rdx = 0
;
;   a  map the IOAPIC page (PCD|RW|NX) at the frozen MMIO VA
;   b  IOAPIC bring-up: measured VER, both RTEs masked, read back
;   c  8042 bring-up: the frozen CCB/mouse sequence, both ports on
; ------------------------------------------------------------------

input_init:
        input_enter
        cld
        ; this stage only runs with interrupts masked
        pushfq
        pop     rax
        test    eax, 0x200
        jnz     .eperm
        cmp     qword [input_state], 0
        jne     .ealready

        ; ---------------- a: the IOAPIC MMIO page ----------------
        mov     rcx, INPUT_IOAPIC_VA
        mov     rdx, INPUT_IOAPIC_PA
        call    vmm_map_mmio
        test    eax, eax
        jnz     .fail

        ; ---------------- b: the IOAPIC itself ----------------
        call    input_ioapic_bringup
        test    eax, eax
        jnz     .fail

        ; ---------------- c: the 8042 and the mouse ----------------
        call    input_ps2_bringup
        test    eax, eax
        jnz     .fail

        mov     qword [input_state], 1
        xor     eax, eax
        xor     edx, edx
        input_leave
        ret
.eperm:
        mov     eax, INPUT_EPERM
        jmp     .fail
.ealready:
        mov     eax, INPUT_EALREADY
.fail:
        xor     edx, edx
        input_leave
        ret

input_code_end:

; ------------------------------------------------------------------
; data last: counters, evidence, the key buffer, messages
; ------------------------------------------------------------------

include 'data.inc'

if $ <> input_data_end
        err 'the input module must not emit anything after input_data_end'
end if
