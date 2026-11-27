; VirtIO 1.x GPU, PCI function on bus 0, one 2D scanout.
; The desktop composes at 1024x768. The scanout follows the host's pixel
; dimensions while preserving the aspect ratio of that logical canvas.

GPU_PCI_ID = 0x10501AF4
GPU_COMMON_VA = 0xFFFFC00020000000
GPU_NOTIFY_VA = GPU_COMMON_VA + 0x1000
GPU_CONFIG_VA = GPU_COMMON_VA + 0x2000
GPU_PIXELS_VA = GPU_COMMON_VA + 0x200000
GPU_QUEUE_SIZE = 8
GPU_MAX_WIDTH = 4096
GPU_MAX_HEIGHT = 3072
GPU_MAX_PAGES = GPU_MAX_WIDTH * GPU_MAX_HEIGHT * 4 / MM_PAGE_SIZE
GPU_RESPONSE_BYTES = 24 + 16 * 24
GPU_REQUEST_BYTES = 32 + GPU_MAX_PAGES * 16
GPU_DISPLAY_EVENT = 1

GPU_GET_DISPLAY_INFO = 0x0100
GPU_CREATE_2D = 0x0101
GPU_UNREF = 0x0102
GPU_SET_SCANOUT = 0x0103
GPU_FLUSH = 0x0104
GPU_TRANSFER = 0x0105
GPU_ATTACH_BACKING = 0x0106
GPU_OK = 0x1100
GPU_DISPLAY_INFO = 0x1101
GPU_FORMAT_BGRX = 2

GPU_DEVICE_FEATURE_SELECT = 0
GPU_DEVICE_FEATURE = 4
GPU_DRIVER_FEATURE_SELECT = 8
GPU_DRIVER_FEATURE = 12
GPU_DEVICE_STATUS = 20
GPU_QUEUE_SELECT = 22
GPU_QUEUE_ENTRIES = 24
GPU_QUEUE_ENABLE = 28
GPU_QUEUE_NOTIFY_OFF = 30
GPU_QUEUE_DESC = 32
GPU_QUEUE_AVAIL = 40
GPU_QUEUE_USED = 48

; Find a VirtIO PCI capability of type ECX. Returns the capability's
; physical BAR region in RDX, length in R8D and PCI cap address in R9D.
gpu_capability:
        video_enter
        mov     r12d, ecx
        mov     ebx, [gpu_pci]
        lea     ecx, [rbx + 0x34]
        call    blk_pci_cfg_read
        movzx   r13d, al
        mov     r14d, 48
.entry:
        and     r13d, 0xfc
        cmp     r13d, 0x40
        jb      .bad
        lea     ecx, [rbx + r13]
        call    blk_pci_cfg_read
        mov     r15d, eax
        cmp     al, 9                  ; vendor-specific capability
        jne     .next
        shr     eax, 24
        cmp     eax, r12d
        jne     .next
        shr     r15d, 16
        cmp     r15b, 16
        jb      .bad
        cmp     r13d, 0xf0
        ja      .bad
        cmp     r12d, 2
        jne     .read_bar
        cmp     r15b, 20
        jb      .bad
        cmp     r13d, 0xec
        ja      .bad
.read_bar:
        lea     ecx, [rbx + r13 + 4]
        call    blk_pci_cfg_read
        movzx   eax, al
        cmp     eax, 5
        ja      .bad
        lea     edi, [rbx + rax*4 + 0x10]
        mov     ecx, edi
        call    blk_pci_cfg_read
        test    al, 1
        jnz     .bad
        mov     esi, eax
        and     esi, -16
        test    al, 4
        jz      .offset
        lea     ecx, [rdi + 4]
        call    blk_pci_cfg_read
        shl     rax, 32
        or      rsi, rax
.offset:
        test    rsi, rsi
        jz      .bad
        lea     ecx, [rbx + r13 + 8]
        call    blk_pci_cfg_read
        add     rsi, rax
        lea     ecx, [rbx + r13 + 12]
        call    blk_pci_cfg_read
        mov     r8d, eax
        mov     rdx, rsi
        lea     r9d, [rbx + r13]
        xor     eax, eax
        jmp     .out
.next:
        shr     r15d, 8
        movzx   r13d, r15b
        dec     r14d
        jnz     .entry
.bad:
        mov     eax, VIDEO_ENOTSUP
.out:
        video_leave
        ret

; RCX = reserved virtual page, RDX = physical register address,
; R8D = bytes accessed in that page. RDX returns the mapped address.
gpu_map_registers:
        video_enter
        mov     r12, rcx
        mov     r13d, edx
        and     r13d, MM_PAGE_SIZE - 1
        lea     eax, [r13d + r8d]
        cmp     eax, MM_PAGE_SIZE
        ja      .bad
        and     rdx, -MM_PAGE_SIZE
        call    vmm_map_mmio
        test    eax, eax
        jnz     .out
        lea     rdx, [r12 + r13]
        jmp     .out
.bad:
        mov     eax, VIDEO_ENOTSUP
.out:
        video_leave
        ret

gpu_transport_init:
        video_enter
        mov     ebx, 0x80000000
.scan:
        mov     ecx, ebx
        call    blk_pci_cfg_read
        cmp     eax, GPU_PCI_ID
        je      .found
        add     ebx, 0x800
        cmp     ebx, 0x80010000
        jb      .scan
        jmp     .bad
.found:
        mov     [gpu_pci], ebx
        lea     ecx, [rbx + 4]
        call    blk_pci_cfg_read
        movzx   edx, ax
        or      edx, 0x406             ; memory space, bus master, INTx disable
        lea     ecx, [rbx + 4]
        call    blk_pci_cfg_write

        mov     ecx, 1                 ; common configuration
        call    gpu_capability
        test    eax, eax
        jnz     .bad
        cmp     r8d, 56
        jb      .bad
        mov     rcx, GPU_COMMON_VA
        mov     r8d, 56
        call    gpu_map_registers
        test    eax, eax
        jnz     .bad
        mov     r12, rdx
        mov     byte [r12 + GPU_DEVICE_STATUS], 0
        xor     r13d, r13d
.reset:
        cmp     byte [r12 + GPU_DEVICE_STATUS], 0
        je      .features
        pause
        inc     r13d
        cmp     r13d, 0x100000
        jb      .reset
        jmp     .bad
.features:
        mov     byte [r12 + GPU_DEVICE_STATUS], 3
        mov     dword [r12 + GPU_DEVICE_FEATURE_SELECT], 1
        test    dword [r12 + GPU_DEVICE_FEATURE], 1   ; VERSION_1
        jz      .bad
        mov     dword [r12 + GPU_DRIVER_FEATURE_SELECT], 0
        mov     dword [r12 + GPU_DRIVER_FEATURE], 0
        mov     dword [r12 + GPU_DRIVER_FEATURE_SELECT], 1
        mov     dword [r12 + GPU_DRIVER_FEATURE], 1
        mov     byte [r12 + GPU_DEVICE_STATUS], 11
        test    byte [r12 + GPU_DEVICE_STATUS], 8
        jz      .bad
        mov     word [r12 + GPU_QUEUE_SELECT], 0
        cmp     word [r12 + GPU_QUEUE_ENTRIES], GPU_QUEUE_SIZE
        jb      .bad
        mov     word [r12 + GPU_QUEUE_ENTRIES], GPU_QUEUE_SIZE

        mov     ecx, 2                 ; notification region
        call    gpu_capability
        test    eax, eax
        jnz     .bad
        mov     r13, rdx
        mov     r14d, r8d
        lea     ecx, [r9 + 16]
        call    blk_pci_cfg_read
        movzx   ecx, word [r12 + GPU_QUEUE_NOTIFY_OFF]
        imul    rax, rcx
        lea     rdx, [rax + 2]
        cmp     rdx, r14
        ja      .bad
        lea     rdx, [r13 + rax]
        mov     rcx, GPU_NOTIFY_VA
        mov     r8d, 2
        call    gpu_map_registers
        test    eax, eax
        jnz     .bad
        mov     [gpu_notify], rdx

        mov     ecx, 4                 ; GPU configuration, events and scanouts
        call    gpu_capability
        test    eax, eax
        jnz     .bad
        cmp     r8d, 12
        jb      .bad
        mov     rcx, GPU_CONFIG_VA
        mov     r8d, 12
        call    gpu_map_registers
        test    eax, eax
        jnz     .bad
        cmp     dword [rdx + 8], 0
        je      .bad
        mov     [gpu_config], rdx

        ; The kernel image is physically contiguous. Resolve its offset
        ; once for the ring and the DMA command buffers in video data.
        sub     rsp, 64
        lea     rcx, [gpu_descriptors]
        lea     rdx, [rsp + 32]
        call    mm_vmm_resolve
        mov     rdx, [rsp + 32 + 48]
        add     rsp, 64
        test    eax, eax
        jnz     .bad
        sub     rdx, gpu_descriptors
        mov     [gpu_dma_delta], rdx
        mov     rax, gpu_descriptors
        add     rax, rdx
        mov     [r12 + GPU_QUEUE_DESC], rax
        mov     rax, gpu_available
        add     rax, rdx
        mov     [r12 + GPU_QUEUE_AVAIL], rax
        mov     rax, gpu_used
        add     rax, rdx
        mov     [r12 + GPU_QUEUE_USED], rax
        mov     word [gpu_available], 1   ; poll completions without interrupts
        mov     word [r12 + GPU_QUEUE_ENABLE], 1
        mov     byte [r12 + GPU_DEVICE_STATUS], 15
        xor     eax, eax
        jmp     .out
.bad:
        mov     eax, VIDEO_ENOTSUP
.out:
        video_leave
        ret

; ECX = request bytes, EDX = expected reply type, R8D = minimum reply
; length. Only one descriptor chain may be owned by the device.
gpu_submit:
        video_enter
        mov     eax, VIDEO_EIO
        cmp     byte [video_gpu_inflight], 0
        jne     .out
        mov     r12d, edx
        mov     r13d, r8d
        mov     rax, [gpu_dma_delta]
        lea     rdx, [gpu_request]
        add     rdx, rax
        mov     [gpu_descriptors], rdx
        mov     [gpu_descriptors + 8], ecx
        mov     dword [gpu_descriptors + 12], 0x00010001
        lea     rdx, [gpu_response]
        add     rdx, rax
        mov     [gpu_descriptors + 16], rdx
        mov     dword [gpu_descriptors + 24], GPU_RESPONSE_BYTES
        mov     dword [gpu_descriptors + 28], 2
        movzx   ebx, word [gpu_available + 2]
        mov     eax, ebx
        and     eax, GPU_QUEUE_SIZE - 1
        mov     word [gpu_available + 4 + rax*2], 0
        mov     byte [video_gpu_inflight], 1
        lea     r14d, [rbx + 1]
        sfence
        mov     [gpu_available + 2], r14w
        mfence
        mov     rax, [gpu_notify]
        mov     word [rax], 0
        mov     ecx, 0x400000
.wait:
        cmp     [gpu_used + 2], r14w
        je      .complete
        pause
        dec     ecx
        jnz     .wait
        mov     eax, VIDEO_EIO
        jmp     .out
.complete:
        lfence
        mov     byte [video_gpu_inflight], 0
        and     ebx, GPU_QUEUE_SIZE - 1
        cmp     dword [gpu_used + 4 + rbx*8], 0
        jne     .error
        cmp     [gpu_used + 8 + rbx*8], r13d
        jb      .error
        cmp     [gpu_response], r12d
        jne     .error
        xor     eax, eax
        jmp     .out
.error:
        mov     eax, VIDEO_EIO
.out:
        video_leave
        ret

; EAX = command type. All current commands use an unfenced header.
gpu_header:
        mov     [gpu_request], eax
        mov     dword [gpu_request + 4], 0
        mov     qword [gpu_request + 8], 0
        mov     qword [gpu_request + 16], 0
        ret

gpu_submit_ok:
        mov     edx, GPU_OK
        mov     r8d, 24
        jmp     gpu_submit

; Read scanout 0's preferred dimensions when the frontend changes.
; A compositor may notify a zero size when the window is minimized.
gpu_query_mode:
        video_enter
        cmp     byte [video_gpu_inflight], 0
        jne     .out
        mov     rax, [gpu_config]
        mov     dword [rax + 4], GPU_DISPLAY_EVENT
        mov     eax, GPU_GET_DISPLAY_INFO
        call    gpu_header
        mov     ecx, 24
        mov     edx, GPU_DISPLAY_INFO
        mov     r8d, GPU_RESPONSE_BYTES
        call    gpu_submit
        test    eax, eax
        jnz     .out
        mov     ecx, [gpu_response + 32]
        mov     edx, [gpu_response + 36]
        call    gpu_set_mode
.out:
        video_leave
        ret

; ECX = required pages. Grow one retained, kernel-only backing mapping.
; Each successfully mapped page can serve future resources of any size.
gpu_grow_backing:
        video_enter
        mov     r12d, ecx
        mov     ebx, [gpu_pages]
        mov     r15d, ebx
.next:
        cmp     ebx, r12d
        jae     .ok
        call    pmm_alloc_page
        test    eax, eax
        jnz     .rollback
        mov     r13, rdx
        mov     rcx, GPU_PIXELS_VA
        mov     eax, ebx
        shl     rax, MM_PAGE_SHIFT
        add     rcx, rax
        mov     r8, MM_MAP_RW or MM_MAP_NX
        call    vmm_map_page
        test    eax, eax
        jnz     .free
        mov     [gpu_backing + rbx*8], r13
        inc     ebx
        mov     [gpu_pages], ebx
        jmp     .next
.free:
        mov     r14d, eax
        mov     rcx, r13
        call    pmm_free_page
        mov     eax, r14d
.rollback:
        mov     r14d, eax
.unmap:
        cmp     ebx, r15d
        je      .rolled_back
        dec     ebx
        mov     rcx, GPU_PIXELS_VA
        mov     eax, ebx
        shl     rax, MM_PAGE_SHIFT
        add     rcx, rax
        call    vmm_unmap_page
        test    eax, eax
        jnz     .out
        mov     [gpu_pages], ebx
        mov     rcx, rdx
        call    pmm_free_page
        test    eax, eax
        jnz     .out
        jmp     .unmap
.rolled_back:
        mov     eax, r14d
        jmp     .out
.ok:
        xor     eax, eax
.out:
        video_leave
        ret

; R12D = resource, R13D = scanout width, R14D = scanout height.
; Prepare a rectangle at the origin for the three rect commands.
gpu_rectangle:
        sub     rsp, 40
        call    gpu_header
        add     rsp, 40
        mov     qword [gpu_request + 24], 0
        mov     [gpu_request + 32], r13d
        mov     [gpu_request + 36], r14d
        ret

gpu_transfer:
        sub     rsp, 40
        mov     eax, GPU_TRANSFER
        call    gpu_rectangle
        add     rsp, 40
        mov     qword [gpu_request + 40], 0
        mov     [gpu_request + 48], r12d
        mov     dword [gpu_request + 52], 0
        mov     ecx, 56
        jmp     gpu_submit_ok

gpu_flush:
        sub     rsp, 40
        mov     eax, GPU_FLUSH
        call    gpu_rectangle
        add     rsp, 40
        mov     [gpu_request + 40], r12d
        mov     dword [gpu_request + 44], 0
        mov     ecx, 48
        jmp     gpu_submit_ok

; ECX = resource to release, after changing the scanout.
gpu_drop:
        cmp     byte [video_gpu_inflight], 0
        jne     .out
        sub     rsp, 40
        mov     eax, GPU_UNREF
        call    gpu_header
        add     rsp, 40
        mov     [gpu_request + 24], ecx
        mov     dword [gpu_request + 28], 0
        mov     ecx, 32
        jmp     gpu_submit_ok
.out:
        ret

; ECX/EDX = requested dimensions. Keep old scanout until a rendered
; replacement is ready. Consecutive pages share one DMA entry.
gpu_set_mode:
        video_enter
        mov     eax, VIDEO_ENOTSUP
        cmp     ecx, 4
        jb      .out
        cmp     edx, 4
        jb      .out
        cmp     byte [video_gpu_inflight], 0
        jne     .out
        mov     r13d, GPU_MAX_WIDTH
        cmp     ecx, r13d
        cmovb   r13d, ecx
        mov     r14d, GPU_MAX_HEIGHT
        cmp     edx, r14d
        cmovb   r14d, edx
        cmp     r13d, [gpu_width]
        jne     .allocate
        cmp     r14d, [gpu_height]
        je      .unchanged
.allocate:
        mov     r15d, r13d
        imul    r15d, r14d
        lea     r15d, [r15d * 4 + MM_PAGE_SIZE - 1]
        shr     r15d, MM_PAGE_SHIFT
        mov     ecx, r15d
        call    gpu_grow_backing
        test    eax, eax
        jnz     .out
        mov     r12d, 1
        cmp     dword [gpu_output_id], 1
        jne     .create
        mov     r12d, 2
.create:
        mov     eax, GPU_CREATE_2D
        call    gpu_header
        mov     [gpu_request + 24], r12d
        mov     dword [gpu_request + 28], GPU_FORMAT_BGRX
        mov     [gpu_request + 32], r13d
        mov     [gpu_request + 36], r14d
        mov     ecx, 40
        call    gpu_submit_ok
        test    eax, eax
        jnz     .out

        mov     eax, GPU_ATTACH_BACKING
        call    gpu_header
        mov     [gpu_request + 24], r12d
        lea     rdi, [gpu_request + 32]
        xor     ebx, ebx
        xor     ecx, ecx
.entry:
        mov     rax, [gpu_backing + rbx*8]
        mov     [rdi], rax
        xor     edx, edx
.extend:
        inc     ebx
        add     edx, MM_PAGE_SIZE
        cmp     ebx, r15d
        jae     .emit
        lea     r8, [rax + rdx]
        cmp     [gpu_backing + rbx*8], r8
        je      .extend
.emit:
        mov     [rdi + 8], rdx
        add     rdi, 16
        inc     ecx
        cmp     ebx, r15d
        jb      .entry
        mov     [gpu_request + 28], ecx
        shl     ecx, 4
        add     ecx, 32
        call    gpu_submit_ok
        test    eax, eax
        jnz     .discard

        mov     ecx, r13d
        mov     edx, r14d
        call    gpu_render
        call    gpu_transfer
        test    eax, eax
        jnz     .discard
        mov     eax, GPU_SET_SCANOUT
        call    gpu_rectangle
        mov     dword [gpu_request + 40], 0
        mov     [gpu_request + 44], r12d
        mov     ecx, 48
        call    gpu_submit_ok
        test    eax, eax
        jnz     .discard

        mov     ebx, [gpu_output_id]
        mov     [gpu_output_id], r12d
        mov     [gpu_width], r13d
        mov     [gpu_height], r14d
        call    gpu_flush
        test    eax, eax
        jnz     .cleanup
        mov     byte [video_gpu_dirty], 0
.cleanup:
        test    ebx, ebx
        jz      .unchanged
        mov     ecx, ebx
        call    gpu_drop
.unchanged:
        xor     eax, eax
        jmp     .out
.discard:
        mov     ecx, r12d
        call    gpu_drop
        mov     eax, VIDEO_EIO
.out:
        video_leave
        ret

; ECX/EDX = physical output size. Draw an aspect-preserving viewport and
; clear the uncovered margins. Map nearest neighbours without blending.
gpu_render:
        video_enter
        mov     r12d, ecx                ; output width
        mov     r13d, edx                ; output height
        mov     r14d, ecx                ; viewport width
        mov     r15d, edx                ; viewport height
        imul    eax, ecx, VIDEO_ST_HEIGHT
        imul    ebx, edx, VIDEO_ST_WIDTH
        cmp     eax, ebx
        ja      .fit_width
        xor     edx, edx
        mov     ecx, VIDEO_ST_WIDTH
        div     ecx
        mov     r15d, eax
        jmp     .columns
.fit_width:
        mov     eax, ebx
        xor     edx, edx
        mov     ecx, VIDEO_ST_HEIGHT
        div     ecx
        mov     r14d, eax
.columns:
        xor     ebx, ebx
.map_x:
        imul    eax, ebx, VIDEO_ST_WIDTH
        xor     edx, edx
        div     r14d
        mov     [gpu_columns + rbx*4], eax
        inc     ebx
        cmp     ebx, r14d
        jb      .map_x

        mov     rdi, GPU_PIXELS_VA
        cmp     r12d, r14d
        jne     .bars
        cmp     r13d, r15d
        je      .pixels
.bars:
        mov     ecx, r12d
        imul    ecx, r13d
        xor     eax, eax
        cld
        rep     stosd
        mov     eax, r13d
        sub     eax, r15d
        shr     eax, 1
        imul    eax, r12d
        mov     ecx, r12d
        sub     ecx, r14d
        shr     ecx, 1
        add     eax, ecx
        mov     rdi, GPU_PIXELS_VA
        lea     rdi, [rdi + rax*4]
.pixels:
        lea     rbp, [gpu_columns]
        xor     ebx, ebx
.row:
        imul    eax, ebx, VIDEO_ST_HEIGHT
        xor     edx, edx
        div     r15d
        imul    eax, eax, VIDEO_ST_WIDTH * 4
        lea     rsi, [gpu_canvas]
        add     rsi, rax
        xor     ecx, ecx
.pixel:
        mov     eax, [rbp + rcx*4]
        mov     eax, [rsi + rax*4]
        mov     [rdi + rcx*4], eax
        inc     ecx
        cmp     ecx, r14d
        jb      .pixel
        lea     rdi, [rdi + r12*4]
        inc     ebx
        cmp     ebx, r15d
        jb      .row
        video_leave
        ret

video_gpu_init:
        video_enter
        call    gpu_transport_init
        test    eax, eax
        jnz     .out
        ; Seed the new output from the already-composed GOP desktop.
        lea     rcx, [gpu_canvas]
        xor     edx, edx
        xor     r8d, r8d
        mov     r9d, VIDEO_ST_WIDTH
        mov     dword [rsp + 32], VIDEO_ST_HEIGHT
        call    video_readback_rect
        call    gpu_query_mode
        test    eax, eax
        jz      .enable
        mov     ecx, VIDEO_ST_WIDTH * 2
        mov     edx, VIDEO_ST_HEIGHT * 2
        call    gpu_set_mode
        test    eax, eax
        jnz     .out
.enable:
        lea     rax, [gpu_canvas]
        mov     [video_fb_state + VIDEO_FB_STATE.fb_va], rax
        add     rax, [gpu_dma_delta]
        mov     [video_fb_state + VIDEO_FB_STATE.fb_pa], rax
        mov     qword [video_fb_state + VIDEO_FB_STATE.fb_size], VIDEO_ST_WIDTH * VIDEO_ST_HEIGHT * 4
        mov     dword [video_fb_state + VIDEO_FB_STATE.mapped_pages], VIDEO_ST_WIDTH * VIDEO_ST_HEIGHT * 4 / MM_PAGE_SIZE
        mov     dword [video_fb_state + VIDEO_FB_STATE.stride], VIDEO_ST_WIDTH * 4
        mov     dword [video_fb_state + VIDEO_FB_STATE.pitch], VIDEO_ST_WIDTH
        mov     byte [video_fb_double], 0
        mov     byte [video_gpu_online], 1
.out:
        video_leave
        ret

video_gpu_pump:
        cmp     byte [video_gpu_online], 0
        je      .inactive
        cmp     byte [video_gpu_inflight], 0
        jne     .inactive
        video_enter
        mov     rax, [gpu_config]
        test    dword [rax], GPU_DISPLAY_EVENT
        jz      .present
        call    gpu_query_mode
.present:
        cmp     byte [video_gpu_dirty], 0
        je      .out
        cmp     byte [video_gpu_inflight], 0
        jne     .out
        mov     rax, [irq_tick_count]
        cmp     rax, [gpu_next_frame]
        jb      .out
        add     rax, 3                 ; at most ~33 fps for ordinary damage
        mov     [gpu_next_frame], rax
        mov     r12d, [gpu_output_id]
        mov     r13d, [gpu_width]
        mov     r14d, [gpu_height]
        mov     ecx, r13d
        mov     edx, r14d
        call    gpu_render
        call    gpu_transfer
        test    eax, eax
        jnz     .out
        call    gpu_flush
        test    eax, eax
        jnz     .out
        mov     byte [video_gpu_dirty], 0
.out:
        video_leave
.inactive:
        ret
