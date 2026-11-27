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
; the main-loop redraw pass, DESKTOP-v2 frozen order:
;   1. restore the cursor OLD position (blit of the save buffer)
;   2. desktop_click_pump: the menu / taskbar click operations
;   3. desktop_echo_pump: consume the new key buffer entries
;   4. place the cursor at the NEW position (save + composite + draw)
; The clamp/evidence publication mirrors desktop_cursor_sync, which
; stays frozen for the app POLL path. Task 143 (DEFECT-1): EVERY pass
; restores the OLD sprite first, stationary included, so no readback
; save (menu_open / cursor_place) ever captures a live cursor; a menu
; redraw under a stationary cursor also forces the re-place
; (click_pump returns eax = 1).
; in
; nothing; runs with IF = 0 (self test wait or resident loop context)
; out
; eax = DESKTOP_OK
; ------------------------------------------------------------------

desktop_pump:
if defined K64_RUNTIME
        desktop_enter
        call    app_window_pump
        cmp     qword [app_window_front], 0
        jne     .done
        call    desktop_echo_pump
.done:
        xor     eax, eax
        desktop_leave
        ret
else
        desktop_enter
        cld
        ; clamp the accumulated input coordinates, publish the evidence
        mov     r12, [input_mouse_x]
        cmp     r12, DESKTOP_CURSOR_XMAX
        jbe     .x_ok
        mov     r12, DESKTOP_CURSOR_XMAX
.x_ok:
        mov     r13, [input_mouse_y]
        cmp     r13, DESKTOP_CURSOR_YMAX
        jbe     .y_ok
        mov     r13, DESKTOP_CURSOR_YMAX
.y_ok:
        mov     [desktop_test_cursor_x], r12
        mov     [desktop_test_cursor_y], r13
        xor     r14d, r14d              ; redraw-needed flag
        cmp     r12, [desktop_cursor_x]
        jne     .moved
        cmp     r13, [desktop_cursor_y]
        jne     .moved
        ; task 143 (DEFECT-1): the stationary path FALLS THROUGH to the
        ; OLD restore as well. A start press with a stationary cursor
        ; opens the menu: menu_open's readback and the place readback
        ; below must never capture the live sprite (the task-125 gate
        ; only covered the menu-CLOSE sister). Restoring first keeps
        ; EVERY readback save sprite-free; the forced re-place is
        ; pixel-neutral because the cursor is drawn last.
.moved:
        mov     r14d, 1
        ; 1. restore the background under the OLD position first
        lea     rcx, [desktop_cursor_save]
        mov     rdx, [desktop_cursor_x]
        mov     r8, [desktop_cursor_y]
        mov     r9d, DESKTOP_SPRITE_W
        mov     dword [rsp+32], DESKTOP_SPRITE_H
        call    video_blit_rect
.restored:
        ; 2. the menu / taskbar click operations
        call    desktop_click_pump
        or      r14d, eax
        ; 3. the keyboard echo
        call    desktop_echo_pump
        ; 4. save + composite + draw at the NEW position
        test    r14d, r14d
        jz      .out
        mov     rcx, r12
        mov     rdx, r13
        call    desktop_cursor_place
        mov     [desktop_cursor_x], r12
        mov     [desktop_cursor_y], r13
.out:
        xor     eax, eax
        desktop_leave
        ret
end if

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
if defined K64_RUNTIME
        call    app_runtime_init
        mov     rax, [input_mouse_edge_write]
        mov     [app_window_edge_read], rax
        call    video_gpu_init
        cmp     byte [video_gpu_online], 0
        je      .loop
        call    desktop_start_timer
end if
.loop:
        sti
        hlt                             ; sleep until a device IRQ wakes us
        cli
        call    desktop_pump
        ; task 125: consume a CHIP-8 launch request in the same IF = 0
        ; window (click mode: publish the APP_RUNNING/APP_EXIT stages)
        mov     ecx, 1
        call    desktop_launch_pump
if defined K64_RUNTIME
        call    app_tasks_pump
        call    app_window_flush
        call    video_gpu_pump
end if
        jmp     .loop

if defined K64_RUNTIME
; The 100 Hz timer paces user tasks and polls the graphics config event.
desktop_start_timer:
        mov     rax, [irq_calib_hz]
        xor     edx, edx
        mov     ecx, IRQ_TICK_HZ
        div     rcx
        mov     rdx, IRQ_LAPIC_VA
        mov     dword [rdx + IRQ_LAPIC_LVT_TIMER], IRQ_VEC_TIMER or IRQ_LVT_PERIODIC
        mov     [rdx + IRQ_LAPIC_INIT_COUNT], eax
        ret
end if
