; A cooperative scheduler for the regular desktop. Applications keep the
; APP-v1 virtual addresses. Only the user PDPT entry is swapped, and it
; is absent whenever the desktop owns the CPU.

APP_TASK_EMPTY = 0
APP_TASK_LIVE = 1
APP_TASK_FAILED = 2
APP_TASK_KEY_WORDS = 6
APP_TASK_FX_SIZE = 512
APP_TASK_FX_MXCSR = 24

virtual at 0
        task_id          dd ?
        task_status      db ?
        task_focus_lost  db ?
        window_mode      db ?
        window_restore   db ?
        window_close     db ?
        window_pressed   db ?
        window_dirty     db ?
        align 4
        window_left      dd ?
        window_top       dd ?
        window_grab_x    dd ?
        window_grab_y    dd ?
        window_click_xy  dd ?
        align 8
        window_click_tick dq ?
        task_title       dq ?
        task_path        dq ?
        task_pdpt_entry  dq ?
        task_poll_rsp    dq ?
        task_last_tick   dq ?
        task_keys        rq APP_TASK_KEY_WORDS
        task_loader      rb APP_TASK_STATE_SIZE
        task_fds         rb FS_MAX_FDS * FS_FD_SIZE
        task_pages       rq APP_LAUNCH_PAGES
        align 16
        task_fx:
                         rb APP_TASK_FX_SIZE
        task_surface     rd APP_FB_WIDTH * APP_FB_HEIGHT
        align 16
        task_loader_stack rb 16384
        task_loader_top:
        task_syscall_stack rb APP_SYSCALL_STACK
        task_syscall_top:
        APP_TASK_SIZE = $
end virtual

; Hooks are installed once before either task enters ring 3.
app_runtime_init:
        app_enter
        mov     rax, [cpu_tss_rsp0]
        mov     [app_task_host_rsp0], rax
        mov     rax, [mm_vmm_state + MM_VMM_STATE.root_phys]
        mov     rax, [rax]
        and     rax, [mm_vmm_state + MM_VMM_STATE.pa_mask]
        lea     rax, [rax + MM_USER_PDPT_INDEX * 8]
        cmp     qword [rax], 0
        jne     .out
        mov     [app_task_aperture], rax
        lea     r12, [app_runtime_handlers]
        xor     ebx, ebx
.install:
        mov     ecx, ebx
        mov     rdx, [r12 + rbx*8]
        call    cpu_syscall_set_handler
        test    eax, eax
        jnz     .rollback
        inc     ebx
        cmp     ebx, APP_SYS_POLL + 1
        jb      .install
        lea     rcx, [app_h_fault]
        call    cpu_user_fault_set_handler
        test    eax, eax
        jnz     .rollback
        ; x86-64 guarantees FXSR. Use the legacy x87/SSE context for
        ; the current QEMU CPU and start it even if firmware used TS.
        mov     rax, cr0
        and     rax, not ((1 shl 2) or (1 shl 3)) ; EM, TS
        mov     cr0, rax
        mov     rax, cr4
        or      rax, 1 shl 9                    ; OSFXSR
        mov     cr4, rax
        mov     byte [app_tasks_ready], 1
        jmp     .out
.rollback:
        test    ebx, ebx
        jz      .out
        dec     ebx
        mov     ecx, ebx
        xor     edx, edx
        call    cpu_syscall_set_handler
        jmp     .rollback
.out:
        app_leave
        ret

; ecx = one-based catalog ID. A second launch raises the existing window.
app_task_start:
        app_enter
        lea     eax, [ecx - 1]
        cmp     eax, DESKTOP_APP_COUNT
        jae     .out
        cmp     byte [app_tasks_ready], 1
        jne     .out
        imul    eax, APP_TASK_SIZE
        lea     rbp, [app_task_slots]
        add     rbp, rax
        cmp     byte [rbp + task_status], APP_TASK_LIVE
        je      .raise
        cmp     byte [rbp + task_status], APP_TASK_EMPTY
        jne     .out
        mov     ebx, ecx
        mov     rdi, rbp
        mov     ecx, task_loader_stack / 8
        xor     eax, eax
        cld
        rep     stosq
        mov     [rbp + task_id], ebx
        lea     eax, [rbx - 1]
        shl     eax, 4
        lea     rdx, [app_catalog]
        mov     rcx, [rdx + rax]
        mov     [rbp + task_path], rcx
        mov     rax, [rdx + rax + 8]
        mov     [rbp + task_title], rax
        mov     word [rbp + task_fx], 0x037f
        mov     dword [rbp + task_fx + APP_TASK_FX_MXCSR], 0x1f80
        mov     dword [rbp + window_left], APP_BLIT_DST_X
        mov     dword [rbp + window_top], APP_WINDOW_TITLE_Y
        call    app_window_other
        cmp     byte [rax + task_status], APP_TASK_LIVE
        jne     .timer
        mov     dword [rbp + window_left], 320
        mov     dword [rbp + window_top], 320
        jmp     .publish
.timer:
        cmp     byte [video_gpu_online], 0
        jne     .publish
        call    desktop_start_timer
.publish:
        mov     byte [rbp + task_status], APP_TASK_LIVE
        inc     qword [desktop_test_app_runs]
.raise:
        call    app_window_raise
.out:
        xor     eax, eax
        app_leave
        ret

; Dispatch at most one slice per live task per timer tick. Window close
; and first entry can dispatch immediately.
app_tasks_pump:
        app_enter
        lea     rbp, [app_task_slots]
        mov     ebx, DESKTOP_APP_COUNT
.next:
        cmp     byte [rbp + task_status], APP_TASK_LIVE
        jne     .advance
        cmp     qword [rbp + task_poll_rsp], 0
        je      .dispatch
        cmp     byte [rbp + window_close], 0
        jne     .dispatch
        cmp     byte [rbp + window_pressed], 0
        jne     .dispatch
        mov     rax, [irq_tick_count]
        cmp     [rbp + task_last_tick], rax
        je      .advance
.dispatch:
        call    app_task_run
.advance:
        add     rbp, APP_TASK_SIZE
        dec     ebx
        jnz     .next
        app_leave
        ret

; rbp = task. The loader's working state and FS slots are projected into
; their existing kernel tables only while this task runs with IF clear.
app_task_run:
        app_enter
        mov     [app_task_host_rsp], rsp
        mov     [app_task_running], rbp
        mov     rax, [irq_tick_count]
        mov     [rbp + task_last_tick], rax
        cld
        lea     rsi, [rbp + task_loader]
        lea     rdi, [app_stage]
        mov     ecx, APP_TASK_STATE_SIZE / 8
        rep     movsq
        lea     rsi, [rbp + task_fds]
        lea     rdi, [fs_fd_table]
        mov     ecx, FS_MAX_FDS * FS_FD_SIZE / 8
        rep     movsq
        mov     rcx, [rbp + task_pdpt_entry]
        call    app_task_install_domain
        lea     rcx, [rbp + task_syscall_top]
        call    cpu_set_rsp0
        fxsave64 [app_task_host_fx]
        fxrstor64 [rbp + task_fx]
        mov     rax, [rbp + task_poll_rsp]
        test    rax, rax
        jnz     .resume_poll
        lea     rsp, [rbp + task_loader_top - 32]
        call    app_launch
        mov     rbp, [app_task_running]
        test    eax, eax
        jnz     .finished
        inc     qword [desktop_test_app_exits]
.finished:
        mov     byte [rbp + task_status], APP_TASK_EMPTY
        cmp     qword [app_pages_held], 0
        je      .closed
        ; An allocator invariant failed. Keep ownership of the damaged
        ; task's state so a later launch cannot reuse its slot.
        mov     byte [rbp + task_status], APP_TASK_FAILED
.closed:
        mov     rsp, [app_task_host_rsp]
        call    app_window_remove
        jmp     .park
.resume_poll:
        mov     rsp, rax
        jmp     app_task_yield.resume
.park:
        mov     rbp, [app_task_running]
        fxsave64 [rbp + task_fx]
        fxrstor64 [app_task_host_fx]
        mov     rax, [app_task_aperture]
        mov     rcx, [rax]
        mov     [rbp + task_pdpt_entry], rcx
        xor     ecx, ecx
        call    app_task_install_domain
        mov     rcx, [app_task_host_rsp0]
        call    cpu_set_rsp0
        cld
        lea     rsi, [app_stage]
        lea     rdi, [rbp + task_loader]
        mov     ecx, APP_TASK_STATE_SIZE / 8
        rep     movsq
        lea     rsi, [fs_fd_table]
        lea     rdi, [rbp + task_fds]
        mov     ecx, FS_MAX_FDS * FS_FD_SIZE / 8
        rep     movsq
        lea     rdi, [fs_fd_table]
        mov     ecx, FS_MAX_FDS * FS_FD_SIZE / 8
        xor     eax, eax
        rep     stosq
        mov     qword [app_task_running], 0
        app_leave
        ret

; SYSCALL has already saved user state on the task's own kernel stack.
; Park the handler here and finish the same POLL when selected again.
app_task_yield:
        app_enter
        mov     rax, [app_task_running]
        mov     [rax + task_poll_rsp], rsp
        mov     rsp, [app_task_host_rsp]
        jmp     app_task_run.park
.resume:
        app_leave
        ret

app_task_install_domain:
        mov     rax, [app_task_aperture]
        mov     [rax], rcx
        mov     rax, cr3
        mov     cr3, rax
        ret

align 8
app_runtime_handlers:
        dq app_h_exit, app_h_blit, app_h_read_key, app_h_poll
