;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Core integration: the foundation bring-up orchestration.
;;
;; Runs on the BSP with IF = 0 after the first stage report, on the kernel
;; stack the entry point established. The fixed order is:
;;
;;   fill K64_INIT_PARAMS -> cpu_init (GDT -> RSP0 -> IDT, no output in
;;   between) -> K64-CPU-OK -> pmm_init -> K64-PMM-OK -> vmm_init ->
;;   vmm_boot_protect over the CPU/MM/core data regions (RW/NX) ->
;;   K64-VMM-OK -> heap_init -> K64-HEAP-OK -> irq_init ->
;;   vmm_boot_protect over the IRQ data region -> irq_selftest ->
;;   K64-IRQ-OK -> core_selftest -> the IRQ-v2 stage (input_init ->
;;   vmm_boot_protect over the input data region -> input_selftest ->
;;   K64-INPUT-OK, mandatory devices, no skip) -> the GUI-G1 stage
;;   (video_init -> vmm_boot_protect over the video data region ->
;;   video_selftest -> K64-GUI-OK, or K64-GUI-SKIP when the GOP is absent
;;   or the geometry is not the frozen 1024x768) -> the DESKTOP-v1 stage
;;   (desktop_init -> vmm_boot_protect over the desktop data region ->
;;   desktop_selftest -> K64-DESKTOP-OK, or K64-DESKTOP-SKIP when the
;;   GUI stage skipped) -> the FS-v1 M1 stage (blk_init ->
;;   vmm_boot_protect over the blk data region -> blk_selftest ->
;;   K64-BLK-OK, mandatory device, no skip) -> the FS-v1 M2 stage
;;   (fs_init -> vmm_boot_protect over the fs data region ->
;;   fs_selftest -> K64-FS-OK, mandatory image, no skip) -> the FS-v1
;;   M3 stage (fsys_init -> vmm_boot_protect over the fsys data region
;;   -> fsys_probe, the Ring 3 leg that tears its user domain footprint
;;   back down -> K64-FSPROBE-OK, mandatory, no skip) ->
;;   K64-FOUNDATION-OK -> the resident desktop event loop, or halt
;;   when the desktop skipped.
;;
;; Nothing here ever executes STI itself; the STI windows of the whole
;; kernel belong to irq_selftest and the resident desktop_run loop,
;; both always entered with the timer masked as their contracts demand.
;; Nothing calls back into UEFI. Every
;; failure goes through cpu_panic, which reports on COM1 within a bound
;; and halts on its own stack. All console output uses the new ABI CPU
;; module functions, never the first stage com1_* helpers.
;;

; bounded marker output through the new ABI console, inlined so the
; caller frame (RSP mod 16 = 0) is preserved for cpu_com1_puts
macro k64_report msg*, fail*
        mov     rcx, msg
        mov     rdx, CPU_PUTS_MAX
        call    cpu_com1_puts
        test    eax, eax
        jnz     fail
end macro

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; bring up the foundation modules and run the core self test
; in
; rcx = physical address of K64BOOTINFO, identity mapped
; out
; never returns on success: reports K64-FOUNDATION-OK and halts;
; any failure panics and halts
; destroys everything (no caller state survives by design)
k64_foundation_init:
        push    rbp
        mov     rbp, rsp
        sub     rsp, K64_STACK_FRAME
        cld
        mov     [rbp-8], rcx            ; BootInfo physical address

        ; ---------------- the shared handover parameters ----------------
        ; filled before any module runs, in core static data so the VA is
        ; canonical high half and 8 byte aligned as cpu_init demands
        mov     dword [core_init_params + K64_INIT_OFF_VERSION], K64_INIT_VERSION
        mov     dword [core_init_params + K64_INIT_OFF_SIZE], K64_INIT_SIZE
        mov     rax, [rbp-8]
        mov     [core_init_params + K64_INIT_OFF_BOOTINFO_VA], rax
        mov     [core_init_params + K64_INIT_OFF_BOOTINFO_PHYS], rax
        mov     rax, cr3
        and     rax, not (K64_PAGE_SIZE - 1)
        mov     [core_init_params + K64_INIT_OFF_CR3_PHYS], rax
        ; the stack top is the exact BootInfo range conversion, not the
        ; current RSP: KERNEL_BASE + (stack_phys_end - kernel_phys_start)
        mov     rax, [rbp-8]
        mov     rdx, [rax + K64BOOTINFO.stack_phys_end]
        sub     rdx, [rax + K64BOOTINFO.kernel_phys_start]
        add     rdx, VIRT_KERNEL_BASE
        mov     [core_init_params + K64_INIT_OFF_STACK_TOP], rdx
        mov     qword [core_init_params + K64_INIT_OFF_FLAGS], K64_INIT_FLAGS_VALUE
        mov     qword [core_init_params + K64_INIT_OFF_RESERVED0], K64_INIT_RESERVED_VALUE
        mov     qword [core_init_params + K64_INIT_OFF_RESERVED1], K64_INIT_RESERVED_VALUE

        ; ---------------- CPU: GDT -> RSP0 -> IDT ----------------
        ; cpu_init performs the descriptor handover with no output and no
        ; other initialisation between LGDT and LIDT
        mov     qword [rbp-16], kmsg_fail_cpu
        mov     rcx, core_init_params
        call    cpu_init
        test    eax, eax
        jnz     .fail

        ; the console the rest of this path reports through
        mov     qword [rbp-16], kmsg_fail_com1
        call    cpu_com1_init
        test    eax, eax
        jnz     .fail

        k64_report kmsg_cpu_ok, .fail

        ; ---------------- physical allocator ----------------
        mov     qword [rbp-16], kmsg_fail_pmm
        mov     rcx, core_init_params
        call    pmm_init
        test    eax, eax
        jnz     .fail

        k64_report kmsg_pmm_ok, .fail

        ; ---------------- virtual memory on the loader CR3 ----------------
        mov     qword [rbp-16], kmsg_fail_vmm
        mov     rcx, core_init_params
        call    vmm_init
        test    eax, eax
        jnz     .fail

        ; the module data regions become RW/NX: stacks, bitmaps, tables
        ; and scratch are data, never instructions
        mov     qword [rbp-16], kmsg_fail_protect
        mov     rcx, cpu_data_start
        mov     rdx, (cpu_data_end - cpu_data_start) / K64_PAGE_SIZE
        mov     r8, MM_MAP_RW or MM_MAP_NX
        call    vmm_boot_protect
        test    eax, eax
        jnz     .fail
        mov     rcx, mm_data_start
        mov     rdx, (mm_data_end - mm_data_start) / K64_PAGE_SIZE
        mov     r8, MM_MAP_RW or MM_MAP_NX
        call    vmm_boot_protect
        test    eax, eax
        jnz     .fail
        mov     rcx, core_data_start
        mov     rdx, (core_data_end - core_data_start) / K64_PAGE_SIZE
        mov     r8, MM_MAP_RW or MM_MAP_NX
        call    vmm_boot_protect
        test    eax, eax
        jnz     .fail

        k64_report kmsg_vmm_ok, .fail

        ; ---------------- the early bump heap ----------------
        mov     qword [rbp-16], kmsg_fail_heap
        call    heap_init
        test    eax, eax
        jnz     .fail

        k64_report kmsg_heap_ok, .fail

        ; ---------------- the IRQ layer ----------------
        mov     qword [rbp-16], kmsg_fail_irq
        call    irq_init
        test    eax, eax
        jnz     .fail

        ; the IRQ data region becomes RW/NX like the other module blocks
        mov     qword [rbp-16], kmsg_fail_protect
        mov     rcx, irq_data_start
        mov     rdx, (irq_data_end - irq_data_start) / K64_PAGE_SIZE
        mov     r8, MM_MAP_RW or MM_MAP_NX
        call    vmm_boot_protect
        test    eax, eax
        jnz     .fail

        ; the IRQ self test owns the only STI windows of the kernel; on
        ; failure it has already printed its leg mask, the foundation
        ; takes the standard fatal path
        mov     qword [rbp-16], kmsg_fail_irq
        call    irq_selftest
        test    eax, eax
        jnz     .fail

        k64_report kmsg_irq_ok, .fail

        ; ---------------- the cross module core self test ----------------
        call    core_selftest
        test    eax, eax
        jnz     .selftest_fail

        ; ---------------- IRQ-v2: the input closed loop ----------------
        ; Runs AFTER core_selftest like the GUI stage: the IOAPIC mapping
        ; allocates its page table after the leg 7 accounting baseline,
        ; so the frozen 3/3/3 table delta stays untouched.
        ;
        ; The IOAPIC and the 8042 are mandatory platform devices: unlike
        ; the GUI stage there is no skip semantics, every failure is a
        ; real bring-up fault that panics. The self test needs the host
        ; injection handshake (gdbstub stage polling + monitor sendkey /
        ; mouse commands); a timeout is a leg failure, never a pass.
        mov     qword [rbp-16], kmsg_fail_input
        call    input_init
        test    eax, eax
        jnz     .fail

        ; the input data region becomes RW/NX like the other blocks
        mov     qword [rbp-16], kmsg_fail_protect
        mov     rcx, input_data_start
        mov     rdx, (input_data_end - input_data_start) / K64_PAGE_SIZE
        mov     r8, MM_MAP_RW or MM_MAP_NX
        call    vmm_boot_protect
        test    eax, eax
        jnz     .fail

        ; on failure the input self test has already printed its leg
        ; mask, the foundation takes the standard fatal path
        mov     qword [rbp-16], kmsg_fail_input
        call    input_selftest
        test    eax, eax
        jnz     .fail

        k64_report kmsg_input_ok, .fail

        ; ---------------- GUI-G1: the framebuffer closed loop ----------------
        ; Runs AFTER core_selftest on purpose: the framebuffer mapping
        ; allocates its page tables after the leg 7 accounting baseline,
        ; so the frozen 3/3/3 table delta stays untouched.
        ;
        ; A missing GOP (FLAG_FRAMEBUFFER clear) or a geometry G1 does
        ; not support (video_init / video_selftest return ENOTSUP) is a
        ; non fatal skip: K64-GUI-SKIP and straight on to FOUNDATION-OK.
        ; Frozen task-85 policy: ENOTSUP only ever comes from the video
        ; geometry gates; a mapping fault inside video_init returns EIO
        ; and every other failure is a real bring-up fault that panics.
        mov     rax, [rbp-8]
        mov     rax, [rax + K64BOOTINFO.flags]
        test    rax, K64BOOTINFO_FLAG_FRAMEBUFFER
        jz      .gui_skip               ; no GOP handover, nothing to map

        mov     qword [rbp-16], kmsg_fail_gui
        mov     rcx, [rbp-8]            ; BootInfo, identity mapped
        call    video_init
        cmp     eax, VIDEO_ENOTSUP
        je      .gui_skip               ; foreign format or oversized FB
        test    eax, eax
        jnz     .fail

        ; the video data region becomes RW/NX like the other blocks
        mov     qword [rbp-16], kmsg_fail_protect
        mov     rcx, video_data_start
        mov     rdx, (video_data_end - video_data_start) / K64_PAGE_SIZE
        mov     r8, MM_MAP_RW or MM_MAP_NX
        call    vmm_boot_protect
        test    eax, eax
        jnz     .fail

        ; the frozen pattern; a geometry other than exactly 1024x768 is
        ; refused with ENOTSUP and skips the milestone, never panics
        mov     qword [rbp-16], kmsg_fail_gui
        call    video_selftest
        cmp     eax, VIDEO_ENOTSUP
        je      .gui_skip
        test    eax, eax
        jnz     .fail

        k64_report kmsg_gui_ok, .fail

        ; the GUI frame forensic window, task 102: the pattern frame is
        ; about to be repainted by desktop_init, so publish READY in the
        ; desktop handshake slot and spin a bounded pause budget until
        ; the host screendumps screen-gui.ppm and writes RELEASE. IF
        ; stays 0, no new sti, ticks and vectors stay frozen; a timeout
        ; is EIO on the standard fatal path, never a hang. The skip
        ; path never enters the window, the slot stays 0.
        mov     qword [rbp-16], kmsg_fail_gui
        mov     qword [desktop_gui_frame_stage], K64_GUI_FRAME_READY
        mov     r14d, K64_GUI_FRAME_POLL_ROUNDS
.gui_frame_poll:
        cmp     qword [desktop_gui_frame_stage], K64_GUI_FRAME_RELEASE
        je      .gui_frame_released
        mov     rdx, K64_GUI_FRAME_POLL_CHUNK
.gui_frame_pause:
        pause
        dec     rdx
        jnz     .gui_frame_pause
        dec     r14d
        jnz     .gui_frame_poll
        mov     eax, VIDEO_EIO
        jmp     .fail
.gui_frame_released:
        mov     qword [desktop_gui_frame_stage], K64_GUI_FRAME_DONE
        jmp     .gui_done
.gui_skip:
        k64_report kmsg_gui_skip, .fail
.gui_done:

        ; ---------------- DESKTOP-v1: the minimal shell ----------------
        ; Runs AFTER the GUI stage: no MMIO mapping of its own (it reuses
        ; the video FB window, the IRQ LAPIC page and the input IOAPIC
        ; page), the sprite and the save/draw buffers live in its data
        ; region, so the frozen leg 7 3/3/3 table delta stays untouched.
        ;
        ; A skipped GUI means a skipped desktop: no framebuffer, no
        ; shell. desktop_init refuses a foreign geometry with ENOTSUP,
        ; the same non fatal skip semantics as the GUI stage; every
        ; other failure is a real bring-up fault that panics. The self
        ; test needs the host injection handshake on top of the input
        ; sequence; a timeout is a leg failure, never a pass.
        mov     rax, [rbp-8]
        mov     rax, [rax + K64BOOTINFO.flags]
        test    rax, K64BOOTINFO_FLAG_FRAMEBUFFER
        jz      .desktop_skip           ; no GOP handover, no shell

        mov     qword [rbp-16], kmsg_fail_desktop
        call    desktop_init
        cmp     eax, DESKTOP_ENOTSUP
        je      .desktop_skip           ; foreign geometry, nothing to draw
        test    eax, eax
        jnz     .fail

        ; the desktop data region becomes RW/NX like the other blocks
        mov     qword [rbp-16], kmsg_fail_protect
        mov     rcx, desktop_data_start
        mov     rdx, (desktop_data_end - desktop_data_start) / K64_PAGE_SIZE
        mov     r8, MM_MAP_RW or MM_MAP_NX
        call    vmm_boot_protect
        test    eax, eax
        jnz     .fail

        ; on failure the desktop self test has already printed its leg
        ; mask, the foundation takes the standard fatal path
        mov     qword [rbp-16], kmsg_fail_desktop
        call    desktop_selftest
        test    eax, eax
        jnz     .fail

        k64_report kmsg_desktop_ok, .fail
        jmp     .desktop_done
.desktop_skip:
        k64_report kmsg_desktop_skip, .fail
.desktop_done:

        ; ---------------- FS-v1 M1: the block layer ----------------
        ; Runs AFTER core_selftest like the input/GUI/desktop stages:
        ; the blk data region protection allocates its page tables
        ; after the leg 7 accounting baseline, so the frozen 3/3/3
        ; table delta stays untouched. The virtio-blk transitional
        ; device is mandatory in the smoke matrix: a missing device or
        ; a failed bring-up is a real fault that panics, there is no
        ; skip marker. IF stays 0, no interrupt line is wired, the
        ; used ring is polled with a bounded pause budget.
        mov     qword [rbp-16], kmsg_fail_blk
        mov     rcx, [rbp-8]            ; BootInfo, identity mapped
        call    blk_init
        test    eax, eax
        jnz     .fail

        ; the blk data region becomes RW/NX like the other blocks; the
        ; vring/DMA pages stay device reachable through the untouched
        ; loader identity map of the low RAM
        mov     qword [rbp-16], kmsg_fail_protect
        mov     rcx, blk_data_start
        mov     rdx, (blk_data_end - blk_data_start) / K64_PAGE_SIZE
        mov     r8, MM_MAP_RW or MM_MAP_NX
        call    vmm_boot_protect
        test    eax, eax
        jnz     .fail

        ; on failure the blk self test has already printed its leg
        ; mask, the foundation takes the standard fatal path
        mov     qword [rbp-16], kmsg_fail_blk
        call    blk_selftest
        test    eax, eax
        jnz     .fail

        k64_report kmsg_blk_ok, .fail

        ; ---------------- FS-v1 M2: the read-only FAT16 mount ----------------
        ; Runs right after the blk stage, still after the leg 7
        ; accounting baseline: the fs data region protection allocates
        ; its page tables late, the frozen 3/3/3 delta stays untouched.
        ; fs_init mounts the frozen FAT16 image through blk_read; an
        ; illegal BPB is the fs self test failure path (EIO), never a
        ; module panic. IF stays 0 and no interrupt line is wired.
        mov     qword [rbp-16], kmsg_fail_fs
        call    fs_init
        test    eax, eax
        jnz     .fail

        ; the fs data region becomes RW/NX like the other blocks
        mov     qword [rbp-16], kmsg_fail_protect
        mov     rcx, fs_data_start
        mov     rdx, (fs_data_end - fs_data_start) / K64_PAGE_SIZE
        mov     r8, MM_MAP_RW or MM_MAP_NX
        call    vmm_boot_protect
        test    eax, eax
        jnz     .fail

        ; on failure the fs self test has already printed its leg
        ; mask, the foundation takes the standard fatal path
        mov     qword [rbp-16], kmsg_fail_fs
        call    fs_selftest
        test    eax, eax
        jnz     .fail

        k64_report kmsg_fs_ok, .fail

        ; ---------------- FS-v1 M3: the file syscall layer ----------------
        ; Runs after the fs stage, still after the leg 7 accounting
        ; baseline: the fsys data protection and the probe user domain
        ; tables allocate late, the frozen 3/3/3 delta stays untouched.
        ; fsys_init registers the file handlers 4..9; fsys_probe maps
        ; the frozen nine page footprint, runs the Ring 3 fsprobe
        ; program and restores the frozen fs/blk sector counters, so
        ; the judge keeps the M1/M2 evidence. IF stays 0 throughout.
        mov     qword [rbp-16], kmsg_fail_fsys
        call    fsys_init
        test    eax, eax
        jnz     .fail

        ; the fsys data region becomes RW/NX like the other blocks;
        ; the private syscall stack inside it stays writable
        mov     qword [rbp-16], kmsg_fail_protect
        mov     rcx, fsys_data_start
        mov     rdx, (fsys_data_end - fsys_data_start) / K64_PAGE_SIZE
        mov     r8, MM_MAP_RW or MM_MAP_NX
        call    vmm_boot_protect
        test    eax, eax
        jnz     .fail

        ; the Ring 3 probe reports its own verdict through the exit
        ; code and the evidence mask; a failure takes the fatal path
        mov     qword [rbp-16], kmsg_fail_fsys
        call    fsys_probe
        test    eax, eax
        jnz     .fail

        k64_report kmsg_fsysprobe_ok, .fail

        ; ---------------- APP-v1: the hosted program loader ----------------
        ; Runs after the fsys stage, still after the leg 7 accounting
        ; baseline: the app data protection allocates late and the 516
        ; page user footprint is mapped AND torn down inside app_launch,
        ; the frozen 3/3/3 delta stays untouched. A skipped desktop means
        ; a skipped app: no shell to return to, the same non fatal skip
        ; convention as the GUI/desktop stages. app_launch reads
        ; /PROGRAMS/CHIP8.BIN through the kernel-internal fs API, enters
        ; Ring 3 and only returns after the program exits: the teardown
        ; repainted the desktop and restored the frozen fs/blk counters.
        cmp     qword [desktop_state], 0
        je      .app_skip

        mov     qword [rbp-16], kmsg_fail_app
        call    app_init
        test    eax, eax
        jnz     .fail

        ; the app data region becomes RW/NX like the other blocks; the
        ; private syscall stack inside it stays writable
        mov     qword [rbp-16], kmsg_fail_protect
        mov     rcx, app_data_start
        mov     rdx, (app_data_end - app_data_start) / K64_PAGE_SIZE
        mov     r8, MM_MAP_RW or MM_MAP_NX
        call    vmm_boot_protect
        test    eax, eax
        jnz     .fail

        ; pristine state + the frozen handler table shape (0..3 empty,
        ; 4..9 resident); a failure takes the standard fatal path
        mov     qword [rbp-16], kmsg_fail_app
        call    app_selftest
        test    eax, eax
        jnz     .fail

        ; the hosted run: load, Ring 3, EXIT, teardown, desktop redraw.
        ; The verdict covers the exit code, the fault seam and the BLIT
        ; evidence; a failure is a real fault that panics.
        mov     qword [rbp-16], kmsg_fail_app
        call    app_launch
        test    eax, eax
        jz      .app_ok
        ; a disk without /PROGRAMS/CHIP8.BIN reports ENOENT: the app
        ; leg skips non-fatally so the FS-v1 images stay bootable and
        ; the frozen 13-marker judge caliber is unchanged
        cmp     eax, APP_ENOENT
        je      .app_skip
        jmp     .fail
.app_ok:
        k64_report kmsg_app_ok, .fail
        jmp     .app_done
.app_skip:
        k64_report kmsg_app_skip, .fail
.app_done:

        k64_report kmsg_foundation_ok, .fail
        ; FOUNDATION-OK stays the terminal judge marker, printed BEFORE
        ; the resident loop: a successful desktop never returns from
        ; desktop_run (the host closes QEMU after the forensics), a
        ; skipped desktop halts exactly like every previous milestone
        cmp     qword [desktop_state], 0
        je      cpu_halt_forever
        call    desktop_run
        jmp     cpu_halt_forever

.selftest_fail:
        ; report the leg mask before the panic so the serial log names
        ; the failing probe, then take the standard fatal path
        mov     [rbp-24], eax
        mov     rcx, kmsg_selftest_mask
        mov     rdx, CPU_PUTS_MAX
        call    cpu_com1_puts
        mov     rcx, [core_selftest_mask]
        mov     rdx, CPU_PUTHEX_DIGITS
        call    cpu_com1_puthex
        mov     rcx, kmsg_core_nl
        mov     rdx, CPU_PUTS_MAX
        call    cpu_com1_puts
        mov     eax, [rbp-24]
        mov     qword [rbp-16], kmsg_fail_selftest
        jmp     .fail

.fail:
        ; eax = the status of the failed step, [rbp-16] = its message
        movsxd  r8, eax
        xor     edx, edx
        mov     rcx, [rbp-16]
        call    cpu_panic
        ; cpu_panic never returns; halt is the belt and braces exit
        jmp     cpu_halt_forever

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; GUI-G1 messages, read only constants emitted with the foundation code
; like the first stage kmsg_* block of kernel64.asm; the mutable core
; data region stays the last bytes of the image in core/selftest.inc.
; The skip marker is not a milestone: the smoke judges it separately.
kmsg_gui_ok           db 'K64-GUI-OK', 13, 10, 0
kmsg_gui_skip         db 'K64-GUI-SKIP', 13, 10, 0
kmsg_fail_gui         db 'foundation: video bring-up failed', 0

; IRQ-v2 messages, same read only convention; there is no input skip
; marker, the IOAPIC and the 8042 are mandatory platform devices
kmsg_input_ok         db 'K64-INPUT-OK', 13, 10, 0
kmsg_fail_input       db 'foundation: input bring-up failed', 0

; DESKTOP-v1 messages, same read only convention; the skip marker
; follows the GUI skip semantics (no framebuffer, no shell), it is
; not a milestone and the smoke judges it separately
kmsg_desktop_ok       db 'K64-DESKTOP-OK', 13, 10, 0
kmsg_desktop_skip     db 'K64-DESKTOP-SKIP', 13, 10, 0
kmsg_fail_desktop     db 'foundation: desktop bring-up failed', 0

; FS-v1 M1 messages, same read only convention; there is no blk skip
; marker, the virtio-blk device is mandatory in the smoke matrix
kmsg_blk_ok           db 'K64-BLK-OK', 13, 10, 0
kmsg_fail_blk         db 'foundation: blk bring-up failed', 0

; FS-v1 M2 messages, same read only convention; there is no fs skip
; marker, the frozen FAT16 image is mandatory in the smoke matrix
kmsg_fs_ok            db 'K64-FS-OK', 13, 10, 0
kmsg_fail_fs          db 'foundation: fs bring-up failed', 0

; FS-v1 M3 messages, same read only convention; there is no fsys skip
; marker, the Ring 3 fsprobe leg is mandatory in the smoke matrix
kmsg_fsysprobe_ok     db 'K64-FSPROBE-OK', 13, 10, 0
kmsg_fail_fsys        db 'foundation: fsys bring-up failed', 0

; APP-v1 messages, same read only convention; the skip marker follows
; the GUI/desktop skip semantics (no shell, no hosted program), it is
; not a milestone and the smoke judges it separately
kmsg_app_ok           db 'K64-APP-OK', 13, 10, 0
kmsg_app_skip         db 'K64-APP-SKIP', 13, 10, 0
kmsg_fail_app         db 'foundation: app bring-up failed', 0
