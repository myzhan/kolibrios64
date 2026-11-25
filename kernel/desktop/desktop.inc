;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Desktop shell: bring-up gate, the bounded handshake wait, the
;; main-loop pump and the resident event loop.
;;
;; desktop_run is the SECOND legal STI owner of the whole kernel (the
;; first is the irq_sti_wait self test window). This file contains
;; exactly one STI instruction, in the resident loop below, and the
;; LAPIC LVT timer mask is always written BEFORE it: irq_test_ticks
;; stays frozen at 11, no timer tick may ever wake the loop. The IRQ
;; handlers still run with IF = 0 and only write buffers/counters;
;; every redraw happens here in the main loop through desktop_pump,
;; never in interrupt context. The loop never returns; the host closes
;; QEMU after the forensic evidence is collected.
;;

; ------------------------------------------------------------------
; bring-up gate
; in
; nothing; the video module must have initialised
; out
; eax = DESKTOP_OK, or DESKTOP_ENOTSUP when the framebuffer is not
; ready or the geometry is not the frozen 1024x768 reference (the
; foundation turns that into the non fatal K64-DESKTOP-SKIP)
; ------------------------------------------------------------------

desktop_init:
        desktop_enter
        cld
        cmp     dword [video_fb_state + VIDEO_FB_STATE.ready], 0
        je      .notsup
        cmp     dword [video_fb_state + VIDEO_FB_STATE.width], DESKTOP_SCREEN_W
        jne     .notsup
        cmp     dword [video_fb_state + VIDEO_FB_STATE.height], DESKTOP_SCREEN_H
        jne     .notsup
        mov     qword [desktop_state], 1
        xor     eax, eax
        desktop_leave
        ret
.notsup:
        mov     eax, DESKTOP_ENOTSUP
        desktop_leave
        ret

; ------------------------------------------------------------------
; wait until a public counter reaches a target, in bounded chunks
; in
; rcx = counter address, rdx = target
; out
; eax = DESKTOP_OK or DESKTOP_EIO; IF is 0 on return
; every round pumps the echo/cursor first, so the counter the host
; watches advances inside the wait; the interrupt window itself goes
; through the exported irq_sti_wait, never a private STI
; ------------------------------------------------------------------

desktop_wait_count:
        desktop_enter
        cld
        mov     r12, rcx
        mov     r13, rdx
        mov     r14d, DESKTOP_POLL_ROUNDS
.loop:
        call    desktop_pump
        cmp     [r12], r13
        jae     .ok
        xor     ecx, ecx
        mov     rdx, DESKTOP_POLL_CHUNK
        mov     r8d, 1
        call    irq_sti_wait
        dec     r14d
        jnz     .loop
        mov     eax, DESKTOP_EIO
        desktop_leave
        ret
.ok:
        xor     eax, eax
        desktop_leave
        ret

; ------------------------------------------------------------------
; the main-loop redraw pass: consume the new key buffer entries, then
; synchronise the cursor with the accumulated input coordinates
; in
; nothing; runs with IF = 0 (self test wait or resident loop context)
; out
; eax = DESKTOP_OK
; ------------------------------------------------------------------

desktop_pump:
        desktop_enter
        cld
        call    desktop_echo_pump
        call    desktop_cursor_sync
        xor     eax, eax
        desktop_leave
        ret

; ------------------------------------------------------------------
; the resident event loop, the normal end of a successful boot
; in
; nothing; desktop_selftest must have succeeded (RTEs masked, LVT
; timer masked, IF = 0, all counters frozen)
; out
; never returns
; ------------------------------------------------------------------

desktop_run:
        desktop_enter
        cld
        cli
        ; mask the LVT timer BEFORE the STI: the frozen tick count 11
        ; must never advance, the loop wakes on keyboard/mouse only
        mov     rax, DESKTOP_LAPIC_VA
        mov     dword [rax + DESKTOP_LAPIC_LVT_TIMER], DESKTOP_LVT_MASK
        ; confirm both device RTEs live (the self test froze them masked)
        mov     ecx, DESKTOP_IOAPIC_IDX_KBD_LO
        mov     edx, DESKTOP_RTE_KBD_LIVE
        call    input_ioapic_write
        mov     ecx, DESKTOP_IOAPIC_IDX_MOUSE_LO
        mov     edx, DESKTOP_RTE_MOUSE_LIVE
        call    input_ioapic_write
.loop:
        sti
        hlt                             ; sleep until a device IRQ wakes us
        cli
        call    desktop_pump
        jmp     .loop
