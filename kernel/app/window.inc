; Two backed windows share the desktop. Window helpers take the task
; record in RBP. The front visible window owns keyboard and pointer input.

APP_WINDOW_NORMAL = 0
APP_WINDOW_MAX = 1
APP_WINDOW_MIN = 2
APP_WINDOW_TITLE_H = 24
APP_WINDOW_BUTTON_W = 24
APP_WINDOW_TITLE_Y = APP_BLIT_DST_Y - APP_WINDOW_TITLE_H
APP_WINDOW_MAX_CLIENT_Y = DESKTOP_BAR_H + APP_WINDOW_TITLE_H
APP_WINDOW_MAX_H = APP_SCREEN_W * APP_FB_HEIGHT / APP_FB_WIDTH
APP_WINDOW_MAX_Y = (APP_SCREEN_H + APP_WINDOW_MAX_CLIENT_Y - APP_WINDOW_MAX_H) / 2
APP_WINDOW_TASK_X = 920
APP_WINDOW_TASK_W = 80
APP_WINDOW_TASK_STEP = 100
APP_WINDOW_TITLE_COLOR = 0x34465A
APP_WINDOW_BUTTON_COLOR = 0x40576C
APP_WINDOW_CLOSE_COLOR = 0x884647

; Copy the validated bounce row to the application's backing store.
; ecx = desktop x, edx = desktop y, r8d = width
app_window_copy_row:
        app_enter
        sub     ecx, APP_BLIT_DST_X
        sub     edx, APP_BLIT_DST_Y
        imul    edx, APP_FB_WIDTH
        add     ecx, edx
        mov     rbp, [app_task_running]
        lea     rdi, [rbp + task_surface]
        lea     rdi, [rdi + rcx*4]
        lea     rsi, [app_bounce]
        mov     ecx, r8d
        cld
        rep     movsd
        app_leave
        ret

; r10d = left, r11d = title y, r9d = width for a visible window.
app_window_geometry:
        mov     r10d, [rbp + window_left]
        mov     r11d, [rbp + window_top]
        mov     r9d, APP_FB_WIDTH
        cmp     byte [rbp + window_mode], APP_WINDOW_MAX
        jne     .done
        xor     r10d, r10d
        mov     r11d, DESKTOP_BAR_H
        mov     r9d, APP_SCREEN_W
.done:
        ret

; Return the other slot, including when it has no live task.
if DESKTOP_APP_COUNT <> 2
        err 'two task slots required by the window order'
end if
app_window_other:
        lea     rax, [app_task_slots]
        lea     rdx, [rax + APP_TASK_SIZE]
        cmp     rbp, rax
        cmove   rax, rdx
        ret

; Restore the background saved under the pointer before changing pixels.
app_window_erase_pointer:
        app_enter
        lea     rcx, [desktop_cursor_save]
        mov     rdx, [desktop_cursor_x]
        mov     r8, [desktop_cursor_y]
        mov     r9d, DESKTOP_SPRITE_W
        mov     dword [rsp+32], DESKTOP_SPRITE_H
        call    video_blit_rect
        app_leave
        ret

app_window_place_pointer:
        mov     rcx, [desktop_cursor_x]
        mov     rdx, [desktop_cursor_y]
        jmp     desktop_cursor_place

; Defer drawing until all applications have yielded this tick.
app_window_present:
        mov     rax, [app_task_running]
        mov     byte [rax + window_dirty], 1
        ret

; Repaint the base after a window changes geometry or order. Otherwise
; composite only changed clients and any window covering them.
app_window_flush:
        app_enter
        call    desktop_cursor_sync
        mov     r12, [app_window_front]
        cmp     byte [app_window_dirty], 0
        jne     .full
        test    r12, r12
        jz      .out
        mov     rbp, r12
        call    app_window_other
        mov     rbp, rax
        cmp     byte [rbp + task_status], APP_TASK_LIVE
        jne     .front
        cmp     byte [rbp + window_mode], APP_WINDOW_MIN
        je      .front
        cmp     byte [rbp + window_dirty], 0
        je      .front
        cmp     byte [r12 + window_mode], APP_WINDOW_MAX
        je      .front
        call    app_window_erase_pointer
        call    app_window_draw_client
        mov     rbp, r12
        call    app_window_draw
        jmp     .overlay
.front:
        mov     rbp, r12
        cmp     byte [rbp + window_dirty], 0
        je      .out
        call    app_window_erase_pointer
        call    app_window_draw_client
        jmp     .overlay
.full:
        mov     byte [app_window_dirty], 0
        call    desktop_repaint_base
        call    desktop_repaint_echo
        lea     rbp, [app_task_slots]
        mov     ebx, DESKTOP_APP_COUNT
.task:
        cmp     byte [rbp + task_status], APP_TASK_LIVE
        jne     .next
        call    app_window_draw_task
.next:
        add     rbp, APP_TASK_SIZE
        dec     ebx
        jnz     .task
        test    r12, r12
        jz      .overlay
        mov     rbp, r12
        call    app_window_other
        mov     rbp, rax
        cmp     byte [r12 + window_mode], APP_WINDOW_MAX
        je      .draw_front
        call    app_window_draw
.draw_front:
        mov     rbp, r12
        call    app_window_draw
.overlay:
        cmp     qword [desktop_menu_flag], 0
        je      .pointer
        call    desktop_menu_open
.pointer:
        mov     byte [video_gpu_dirty], 1
        call    app_window_place_pointer
.out:
        mov     byte [app_task_slots + window_dirty], 0
        mov     byte [app_task_slots + APP_TASK_SIZE + window_dirty], 0
        app_leave
        ret

app_window_task_x:
        mov     ecx, [rbp + task_id]
        dec     ecx
        imul    ecx, -APP_WINDOW_TASK_STEP
        add     ecx, APP_WINDOW_TASK_X
        ret

app_window_draw_task:
        app_enter
        call    app_window_task_x
        mov     edx, 4
        mov     r8d, APP_WINDOW_TASK_W - 4
        mov     r9d, DESKTOP_BAR_H - 8
        mov     eax, APP_WINDOW_TITLE_COLOR
        cmp     rbp, [app_window_front]
        jne     .draw
        mov     eax, APP_WINDOW_BUTTON_COLOR
.draw:
        mov     [rsp+32], eax
        call    video_fill_rect
        call    app_window_task_x
        add     ecx, 4
        mov     edx, DESKTOP_TITLE_Y
        mov     r8d, DESKTOP_TITLE_COLOR
        mov     r9, [rbp + task_title]
        call    video_puts
        mov     ecx, DESKTOP_RUN_X
        mov     edx, DESKTOP_RUN_Y
        mov     r8d, DESKTOP_RUN_W
        mov     r9d, DESKTOP_RUN_H
        mov     dword [rsp+32], DESKTOP_RUN_COLOR
        call    video_fill_rect
        app_leave
        ret

app_window_draw:
        cmp     byte [rbp + task_status], APP_TASK_LIVE
        jne     .hidden
        cmp     byte [rbp + window_mode], APP_WINDOW_MIN
        je      .hidden
        app_enter
        cmp     byte [rbp + window_mode], APP_WINDOW_MAX
        jne     .title
        xor     ecx, ecx
        mov     edx, APP_WINDOW_MAX_CLIENT_Y
        mov     r8d, APP_SCREEN_W
        mov     r9d, APP_SCREEN_H - APP_WINDOW_MAX_CLIENT_Y
        mov     dword [rsp+32], 0
        call    video_fill_rect
.title:
        call    app_window_geometry
        mov     r12d, r10d
        mov     r13d, r11d
        lea     r14d, [r10d + r9d - 3 * APP_WINDOW_BUTTON_W]
        mov     ecx, r12d
        mov     edx, r13d
        mov     r8d, r9d
        mov     r9d, APP_WINDOW_TITLE_H
        mov     dword [rsp+32], APP_WINDOW_TITLE_COLOR
        call    video_fill_rect
        lea     ecx, [r12d + 8]
        lea     edx, [r13d + 4]
        mov     r8d, DESKTOP_TITLE_COLOR
        mov     r9, [rbp + task_title]
        call    video_puts
        lea     r15, [app_window_button_labels]
        mov     ebx, 3
.button:
        mov     ecx, r14d
        mov     edx, r13d
        mov     r8d, APP_WINDOW_BUTTON_W - 1
        mov     r9d, APP_WINDOW_TITLE_H
        mov     eax, APP_WINDOW_BUTTON_COLOR
        cmp     ebx, 1
        jne     .color
        mov     eax, APP_WINDOW_CLOSE_COLOR
.color:
        mov     [rsp+32], eax
        call    video_fill_rect
        lea     ecx, [r14d + 4]
        lea     edx, [r13d + 4]
        mov     r8d, DESKTOP_TITLE_COLOR
        mov     r9, r15
        call    video_puts
        add     r14d, APP_WINDOW_BUTTON_W
        add     r15, 3
        dec     ebx
        jnz     .button
        call    app_window_draw_client
        app_leave
.hidden:
        ret

app_window_draw_client:
        app_enter
        cmp     byte [rbp + window_mode], APP_WINDOW_MAX
        je      .maximized
        lea     rcx, [rbp + task_surface]
        mov     edx, [rbp + window_left]
        mov     r8d, [rbp + window_top]
        add     r8d, APP_WINDOW_TITLE_H
        mov     r9d, APP_FB_WIDTH
        mov     dword [rsp+32], APP_FB_HEIGHT
        call    video_blit_rect
        jmp     .done
.maximized:
        xor     r12d, r12d
.row:
        imul    eax, r12d, APP_FB_HEIGHT
        xor     edx, edx
        mov     ecx, APP_WINDOW_MAX_H
        div     ecx
        imul    eax, APP_FB_WIDTH * 4
        lea     rsi, [rbp + task_surface]
        add     rsi, rax
        lea     rdi, [app_window_row]
        mov     ecx, APP_SCREEN_W
        xor     r10d, r10d
        cld
.pixel:
        mov     eax, [rsi]
        stosd
        add     r10d, APP_FB_WIDTH
        cmp     r10d, APP_SCREEN_W
        jb      .next
        sub     r10d, APP_SCREEN_W
        add     rsi, 4
.next:
        dec     ecx
        jnz     .pixel
        lea     rcx, [app_window_row]
        xor     edx, edx
        lea     r8d, [r12d + APP_WINDOW_MAX_Y]
        mov     r9d, APP_SCREEN_W
        mov     dword [rsp+32], 1
        call    video_blit_rect
        inc     r12d
        cmp     r12d, APP_WINDOW_MAX_H
        jb      .row
.done:
        app_leave
        ret

app_window_pump:
        app_enter
        mov     r13, [input_mouse_edge_write]
        mov     r14, [app_window_edge_read]
        mov     rax, r13
        sub     rax, r14
        cmp     rax, INPUT_MOUSE_EDGE_CAP
        ja      .overflow
.edge:
        cmp     r14, r13
        je      .move
        mov     eax, r14d
        and     eax, INPUT_MOUSE_EDGE_CAP - 1
        shl     eax, 4
        lea     rdx, [input_mouse_edges]
        mov     r12, [rdx + rax]
        mov     r15, [rdx + rax + 8]
        inc     r14
        mov     [app_window_edge_read], r14
        movzx   ecx, r12w
        mov     rdx, r12
        shr     edx, 16
        bt      r12, 32
        jnc     .release
        call    app_window_route_click
        cmp     qword [desktop_app_launch_requested], 0
        jne     .done
        ; Consume at most one client press per dispatch.
        test    eax, eax
        jz      .edge
        jmp     .done
.release:
        mov     rbp, [app_window_drag_owner]
        test    rbp, rbp
        jz      .edge
        call    app_window_drag
        mov     qword [app_window_drag_owner], 0
        jmp     .edge
.move:
        mov     rbp, [app_window_drag_owner]
        test    rbp, rbp
        jz      .done
        mov     ecx, dword [input_mouse_x]
        mov     edx, dword [input_mouse_y]
        call    app_window_drag
        jmp     .done
.overflow:
        ; A stale release must never leave a captured title dragging.
        mov     [app_window_edge_read], r13
        mov     qword [app_window_drag_owner], 0
.done:
        app_leave
        ret

; Visit the taskbar and menu first, then hit-test visible windows in
; reverse paint order. ecx/edx = sampled position, r15 = sampled tick.
app_window_route_click:
        app_enter
        mov     r12d, ecx
        mov     r13d, edx
        call    desktop_click_pump
        test    eax, eax
        jnz     .ignored
        cmp     r13d, DESKTOP_BAR_H
        jb      .taskbar
        mov     rbp, [app_window_front]
        test    rbp, rbp
        jz      .ignored
        call    .hit
        jnc     .clicked
        call    app_window_other
        mov     rbp, rax
        call    .hit
        jc      .ignored
.clicked:
        call    app_window_raise
        mov     ecx, r12d
        mov     edx, r13d
        mov     r8, r15
        call    app_window_press
        movzx   eax, byte [rbp + window_pressed]
        jmp     .out
.taskbar:
        lea     rbp, [app_task_slots]
        mov     ebx, DESKTOP_APP_COUNT
.button:
        cmp     byte [rbp + task_status], APP_TASK_LIVE
        jne     .next
        call    app_window_task_x
        mov     eax, r12d
        sub     eax, ecx
        cmp     eax, APP_WINDOW_TASK_W
        jb      .task_clicked
.next:
        add     rbp, APP_TASK_SIZE
        dec     ebx
        jnz     .button
        jmp     .ignored
.task_clicked:
        call    app_window_raise
.ignored:
        xor     eax, eax
.out:
        app_leave
        ret
.hit:
        cmp     byte [rbp + task_status], APP_TASK_LIVE
        jne     .miss
        cmp     byte [rbp + window_close], 0
        jne     .miss
        cmp     byte [rbp + window_mode], APP_WINDOW_MIN
        je      .miss
        cmp     byte [rbp + window_mode], APP_WINDOW_MAX
        je      .inside
        mov     eax, r12d
        sub     eax, [rbp + window_left]
        cmp     eax, APP_FB_WIDTH
        jae     .miss
        mov     eax, r13d
        sub     eax, [rbp + window_top]
        cmp     eax, APP_WINDOW_TITLE_H + APP_FB_HEIGHT
        jae     .miss
.inside:
        clc
        ret
.miss:
        stc
        ret

; Give a visible window focus and put it above the other window. Keys
; already queued for the old focus cannot be delivered to its successor.
app_window_raise:
        cmp     byte [rbp + window_close], 0
        jne     .done
        cmp     byte [rbp + window_mode], APP_WINDOW_MIN
        jne     .focus
        mov     al, [rbp + window_restore]
        mov     [rbp + window_mode], al
.focus:
        mov     rcx, rbp
        jmp     app_window_focus
.done:
        ret

app_window_focus:
        cmp     rcx, [app_window_front]
        je      .done
        sub     rsp, 40
        call    app_window_suspend_input
        add     rsp, 40
        mov     [app_window_front], rcx
        mov     byte [app_window_dirty], 1
.done:
        ret

; Called on focus change and when the Start menu intercepts input.
; Preserves RCX (the next focus, if any).
app_window_suspend_input:
        mov     rax, [input_kbd_used]
        mov     [input_kbd_head], rax
        mov     rax, [app_window_front]
        test    rax, rax
        jz      .done
        mov     byte [rax + task_focus_lost], 1
        mov     byte [rax + window_pressed], 0
.done:
        ret

; When the front window is hidden or closes, expose the other visible
; window. Clear any capture held by the disappearing window.
app_window_unfocus:
        app_enter
        cmp     rbp, [app_window_drag_owner]
        jne     .focus
        mov     qword [app_window_drag_owner], 0
.focus:
        cmp     rbp, [app_window_front]
        jne     .done
        call    app_window_other
        xor     ecx, ecx
        cmp     byte [rax + task_status], APP_TASK_LIVE
        jne     .publish
        cmp     byte [rax + window_mode], APP_WINDOW_MIN
        je      .publish
        cmp     byte [rax + window_close], 0
        jne     .publish
        mov     rcx, rax
.publish:
        call    app_window_focus
.done:
        mov     byte [app_window_dirty], 1
        app_leave
        ret

app_window_remove:
        app_enter
        call    app_window_unfocus
        call    app_window_other
        cmp     byte [rax + task_status], APP_TASK_LIVE
        je      .done
        cmp     byte [video_gpu_online], 0
        jne     .done
        mov     rdx, IRQ_LAPIC_VA
        mov     dword [rdx + IRQ_LAPIC_LVT_TIMER], IRQ_LVT_MASK
        mov     dword [rdx + IRQ_LAPIC_INIT_COUNT], 0
.done:
        app_leave
        ret

; ecx/edx = sampled screen position, r8 = sampled tick.
app_window_press:
        app_enter
        call    app_window_geometry
        mov     eax, edx
        sub     eax, r11d
        cmp     eax, APP_WINDOW_TITLE_H
        jae     .client
        sub     ecx, r10d
        cmp     ecx, r9d
        jae     .done
        sub     r9d, 3 * APP_WINDOW_BUTTON_W
        cmp     ecx, r9d
        jb      .pickup
        sub     ecx, r9d
        cmp     ecx, 2 * APP_WINDOW_BUTTON_W
        jae     .close
        cmp     ecx, APP_WINDOW_BUTTON_W
        jae     .maximize
        mov     al, [rbp + window_mode]
        mov     [rbp + window_restore], al
        mov     byte [rbp + window_mode], APP_WINDOW_MIN
        call    app_window_unfocus
        jmp     .done
.maximize:
        xor     byte [rbp + window_mode], APP_WINDOW_MAX
        mov     byte [app_window_dirty], 1
        jmp     .done
.close:
        mov     byte [rbp + window_close], 1
        call    app_window_unfocus
        jmp     .done
.pickup:
        cmp     byte [rbp + window_mode], APP_WINDOW_NORMAL
        jne     .done
        mov     [rbp + window_grab_x], ecx
        mov     [rbp + window_grab_y], eax
        mov     [app_window_drag_owner], rbp
        jmp     .done
.client:
        call    app_window_client_point
        cmp     eax, -1
        je      .done
        mov     [rbp + window_click_xy], eax
        mov     [rbp + window_click_tick], r8
        mov     byte [rbp + window_pressed], 1
.done:
        app_leave
        ret

; ecx/edx = screen position. Clamp the whole frame to the desktop.
app_window_drag:
        sub     ecx, [rbp + window_grab_x]
        sub     edx, [rbp + window_grab_y]
        xor     eax, eax
        cmp     ecx, eax
        cmovl   ecx, eax
        mov     eax, APP_SCREEN_W - APP_FB_WIDTH
        cmp     ecx, eax
        cmovg   ecx, eax
        mov     eax, DESKTOP_BAR_H
        cmp     edx, eax
        cmovl   edx, eax
        mov     eax, APP_SCREEN_H - APP_WINDOW_TITLE_H - APP_FB_HEIGHT
        cmp     edx, eax
        cmovg   edx, eax
        cmp     ecx, [rbp + window_left]
        jne     .changed
        cmp     edx, [rbp + window_top]
        je      .done
.changed:
        mov     [rbp + window_left], ecx
        mov     [rbp + window_top], edx
        mov     byte [app_window_dirty], 1
.done:
        ret

; Convert screen position to the same coordinates as the backed pixels.
; eax = x | y<<16, or -1 outside. Preserves r8 (event time).
app_window_client_point:
        cmp     byte [rbp + window_mode], APP_WINDOW_MIN
        je      .outside
        cmp     byte [rbp + window_mode], APP_WINDOW_MAX
        je      .maximized
        sub     ecx, [rbp + window_left]
        sub     edx, [rbp + window_top]
        sub     edx, APP_WINDOW_TITLE_H
        cmp     ecx, APP_FB_WIDTH
        jae     .outside
        cmp     edx, APP_FB_HEIGHT
        jae     .outside
        jmp     .pack
.maximized:
        sub     edx, APP_WINDOW_MAX_Y
        cmp     ecx, APP_SCREEN_W
        jae     .outside
        cmp     edx, APP_WINDOW_MAX_H
        jae     .outside
        mov     r10d, edx
        imul    eax, ecx, APP_FB_WIDTH
        xor     edx, edx
        mov     ecx, APP_SCREEN_W
        div     ecx
        mov     r11d, eax
        imul    eax, r10d, APP_FB_HEIGHT
        xor     edx, edx
        mov     ecx, APP_WINDOW_MAX_H
        div     ecx
        mov     edx, eax
        mov     ecx, r11d
.pack:
        shl     edx, 16
        lea     eax, [edx + ecx]
        ret
.outside:
        mov     rax, -1
        ret

app_window_poll_client:
        cmp     byte [rbp + window_pressed], 0
        je      .position
        mov     byte [rbp + window_pressed], 0
        mov     eax, [rbp + window_click_xy]
        mov     edx, APP_POLL_PRESS or 1
        mov     r8, [rbp + window_click_tick]
        ret
.position:
        mov     ecx, [input_mouse_x]
        mov     edx, [input_mouse_y]
        sub     rsp, 40
        call    app_window_client_point
        add     rsp, 40
        xor     edx, edx
        cmp     rax, -1
        je      .done
        mov     edx, [input_mouse_buttons]
.done:
        ret

app_window_button_labels:
        db      '-', 0, 0, '[', ']', 0, 'X', 0, 0
