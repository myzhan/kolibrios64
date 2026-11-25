;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; FS-v1 M1 block layer: the virtio-blk-pci-transitional driver.
;;
;; PCI configuration space is scanned through the CF8/CFC mechanism
;; (bus 0, devices 0..31, function 0) for vendor 1AF4 / device 1001
;; with the legacy blk subsystem id. The legacy PIO BAR0 window drives
;; the frozen 0.9.5 bring-up: reset -> ACK -> DRIVER -> features ->
;; queue_pfn -> DRIVER_OK, every written register read back exactly
;; like the IOAPIC bring-up of the input module.
;;
;; One split virtqueue, one request in flight, polled used ring with a
;; bounded pause budget. No interrupt line is ever wired and the IRQ
;; dispatch table is not touched. Every descriptor address is a guest
;; physical address inside the loader identity map: the vring, the DMA
;; bounce page and the request scratch page all live in the module data
;; region, converted through BootInfo.kernel_phys_start at init.
;;
;; The module calls nothing outside its own files except the CPU
;; console (self test failure report). Every public entry returns a
;; signed EAX status; blk_read is the backend seam a future initramfs
;; ramdisk can replace through blk_backend.
;;

blk_code_start:

; ------------------------------------------------------------------
; legacy PIO access, leaves over the measured BAR0 base
; in  ecx = register offset        out eax = value (read, zero extended)
;     edx = value (write)
; clobbers rax, rdx only; rcx and every other register survive
; ------------------------------------------------------------------

blk_pio_in32:
        mov     dx, [blk_iobase]
        add     dx, cx
        in      eax, dx
        ret

blk_pio_in16:
        mov     dx, [blk_iobase]
        add     dx, cx
        xor     eax, eax
        in      ax, dx
        ret

blk_pio_in8:
        mov     dx, [blk_iobase]
        add     dx, cx
        xor     eax, eax
        in      al, dx
        ret

blk_pio_out32:
        mov     eax, edx
        mov     dx, [blk_iobase]
        add     dx, cx
        out     dx, eax
        ret

blk_pio_out16:
        mov     eax, edx
        mov     dx, [blk_iobase]
        add     dx, cx
        out     dx, ax
        ret

blk_pio_out8:
        mov     eax, edx
        mov     dx, [blk_iobase]
        add     dx, cx
        out     dx, al
        ret

; ------------------------------------------------------------------
; PCI configuration space, leaves over the CF8/CFC mechanism
; in  ecx = full config address (enable | bus | dev | func | reg)
;     edx = value (write)          out eax = dword (read)
; clobbers rax, rdx only
; ------------------------------------------------------------------

blk_pci_cfg_read:
        mov     eax, ecx
        mov     dx, BLK_PCI_ADDR_PORT
        out     dx, eax
        mov     dx, BLK_PCI_DATA_PORT
        in      eax, dx
        ret

blk_pci_cfg_write:
        ; the value must survive the port load: dx aliases the low half
        ; of edx, so 'mov dx, port' would destroy it; park it on the
        ; stack and pop it straight into eax (rdx ends up restored)
        push    rdx
        mov     eax, ecx
        mov     dx, BLK_PCI_ADDR_PORT
        out     dx, eax
        pop     rax
        mov     dx, BLK_PCI_DATA_PORT
        out     dx, eax
        ret

; ------------------------------------------------------------------
; find the transitional virtio-blk function and enable its PIO BAR
; out
; eax = BLK_OK, BLK_ENOENT (no device) or BLK_EIO (BAR/command fault)
; evidence: blk_pci_addr, blk_iobase
; ------------------------------------------------------------------

blk_pci_scan:
        blk_enter
        cld
        xor     r12d, r12d              ; device index
.loop:
        mov     ebx, BLK_PCI_ENABLE
        mov     eax, r12d
        shl     eax, BLK_PCI_DEV_SHIFT
        or      ebx, eax                ; bus 0, function 0, register 0
        mov     ecx, ebx
        call    blk_pci_cfg_read
        mov     edx, eax
        and     edx, 0xFFFF
        cmp     edx, BLK_PCI_VENDOR
        jne     .next
        shr     eax, 16
        cmp     eax, BLK_PCI_DEVICE
        jne     .next

        ; the subsystem id separates blk from the other legacy devices
        lea     ecx, [rbx + BLK_PCI_REG_SUBSYS]
        call    blk_pci_cfg_read
        mov     edx, eax
        and     edx, 0xFFFF
        cmp     edx, BLK_PCI_SUBVENDOR
        jne     .next
        shr     eax, 16
        cmp     eax, BLK_PCI_SUBDEVICE
        jne     .next

        ; legacy revision 0 only, modern-only devices are refused
        lea     ecx, [rbx + BLK_PCI_REG_REVISION]
        call    blk_pci_cfg_read
        and     eax, 0xFF
        cmp     eax, BLK_PCI_REVISION
        jne     .next

        ; BAR0 must be a non empty PIO BAR
        lea     ecx, [rbx + BLK_PCI_REG_BAR0]
        call    blk_pci_cfg_read
        test    eax, BLK_PCI_BAR_IO
        jz      .next
        and     eax, BLK_PCI_BAR_MASK
        jz      .next
        mov     [blk_iobase], rax
        mov     [blk_pci_addr], rbx

        ; enable IO space and bus master, then read the command back;
        ; the firmware may leave other command bits set (e.g. memory
        ; space), so the read back is masked to exactly the two bits
        ; this driver requires
        lea     ecx, [rbx + BLK_PCI_REG_COMMAND]
        call    blk_pci_cfg_read
        and     eax, 0xFFFF
        or      eax, BLK_PCI_CMD_IO or BLK_PCI_CMD_MASTER
        lea     ecx, [rbx + BLK_PCI_REG_COMMAND]
        mov     edx, eax
        call    blk_pci_cfg_write
        lea     ecx, [rbx + BLK_PCI_REG_COMMAND]
        call    blk_pci_cfg_read
        and     eax, BLK_PCI_CMD_IO or BLK_PCI_CMD_MASTER
        cmp     eax, BLK_PCI_CMD_IO or BLK_PCI_CMD_MASTER
        jne     .eio

        xor     eax, eax
        blk_leave
        ret
.next:
        inc     r12d
        cmp     r12d, BLK_PCI_MAX_DEV
        jb      .loop
        mov     eax, BLK_ENOENT
        blk_leave
        ret
.eio:
        mov     eax, BLK_EIO
        blk_leave
        ret

; ------------------------------------------------------------------
; bring up the device on the frozen legacy sequence
; in
; rcx = physical address of K64BOOTINFO, identity mapped
; out
; eax = BLK_OK or a negative status; on success blk_backend is armed
; every PIO write is verified by an exact read back
; ------------------------------------------------------------------

blk_init:
        blk_enter
        cld
        cli
        test    rcx, rcx
        jz      .einval
        mov     rax, BLK_PHYS_LIMIT
        cmp     rcx, rax
        jae     .einval

        ; guest physical addresses of the three DMA blocks: the data
        ; region sits in the kernel image, so phys = VA - kernel base
        ; + BootInfo.kernel_phys_start, all inside the identity domain
        mov     r13, [rcx + BLK_BOOTINFO_KERNEL_PHYS_START]
        mov     r14, BLK_PHYS_LIMIT
        mov     rax, blk_vring
        sub     rax, BLK_KERNEL_BASE
        add     rax, r13
        cmp     rax, r14
        jae     .fail_eio
        mov     [blk_vring_phys], rax
        mov     rax, blk_dma
        sub     rax, BLK_KERNEL_BASE
        add     rax, r13
        cmp     rax, r14
        jae     .fail_eio
        mov     [blk_dma_phys], rax
        mov     rax, blk_req
        sub     rax, BLK_KERNEL_BASE
        add     rax, r13
        cmp     rax, r14
        jae     .fail_eio
        mov     [blk_req_phys], rax

        call    blk_pci_scan
        test    eax, eax
        jnz     .fail

        ; reset: status 0, read back
        mov     ecx, BLK_PIO_STATUS
        xor     edx, edx
        call    blk_pio_out8
        mov     ecx, BLK_PIO_STATUS
        call    blk_pio_in8
        test    eax, eax
        jnz     .fail_eio

        ; ACKNOWLEDGE
        mov     ecx, BLK_PIO_STATUS
        mov     edx, BLK_STATUS_ACK
        call    blk_pio_out8
        mov     ecx, BLK_PIO_STATUS
        call    blk_pio_in8
        cmp     eax, BLK_STATUS_ACK
        jne     .fail_eio

        ; ACKNOWLEDGE | DRIVER
        mov     ecx, BLK_PIO_STATUS
        mov     edx, BLK_STATUS_ACK or BLK_STATUS_DRIVER
        call    blk_pio_out8
        mov     ecx, BLK_PIO_STATUS
        call    blk_pio_in8
        cmp     eax, BLK_STATUS_ACK or BLK_STATUS_DRIVER
        jne     .fail_eio

        ; features: accept none, the frozen read-only path needs no bit
        mov     ecx, BLK_PIO_GUEST_FEATURES
        xor     edx, edx
        call    blk_pio_out32
        mov     ecx, BLK_PIO_GUEST_FEATURES
        call    blk_pio_in32
        test    eax, eax
        jnz     .fail_eio

        ; select queue 0 and measure its size: a power of two <= 256
        mov     ecx, BLK_PIO_QUEUE_SEL
        mov     edx, BLK_QUEUE_SEL
        call    blk_pio_out16
        mov     ecx, BLK_PIO_QUEUE_NUM
        call    blk_pio_in16
        test    eax, eax
        jz      .fail_eio
        cmp     eax, BLK_QNUM_MAX
        ja      .fail_eio
        mov     edx, eax
        dec     edx
        test    eax, edx
        jnz     .fail_eio
        mov     [blk_qnum], rax

        ; queue_pfn is the physical page frame number, legacy semantics
        mov     rax, [blk_vring_phys]
        shr     rax, BLK_PAGE_SHIFT
        mov     edx, eax
        mov     ecx, BLK_PIO_QUEUE_PFN
        call    blk_pio_out32
        mov     ecx, BLK_PIO_QUEUE_PFN
        call    blk_pio_in32
        mov     rdx, [blk_vring_phys]
        shr     rdx, BLK_PAGE_SHIFT
        cmp     eax, edx
        jne     .fail_eio

        ; polling driver: ask the device to keep its interrupt quiet
        mov     rax, [blk_qnum]
        shl     rax, 4                  ; avail_off = 16 * qnum
        mov     rdx, blk_vring
        mov     word [rdx + rax], BLK_AVAIL_F_NOINT

        ; capacity from the device config, two PIO dwords
        mov     ecx, BLK_PIO_CONFIG_OFF + BLK_CFG_CAPACITY
        call    blk_pio_in32
        mov     r14d, eax
        mov     ecx, BLK_PIO_CONFIG_OFF + BLK_CFG_CAPACITY + 4
        call    blk_pio_in32
        shl     rax, 32
        or      rax, r14
        test    rax, rax
        jz      .fail_eio
        mov     [blk_capacity], rax

        ; ACKNOWLEDGE | DRIVER | DRIVER_OK
        mov     ecx, BLK_PIO_STATUS
        mov     edx, BLK_STATUS_ACK or BLK_STATUS_DRIVER or BLK_STATUS_DRIVER_OK
        call    blk_pio_out8
        mov     ecx, BLK_PIO_STATUS
        call    blk_pio_in8
        cmp     eax, BLK_STATUS_ACK or BLK_STATUS_DRIVER or BLK_STATUS_DRIVER_OK
        jne     .fail_eio

        mov     qword [blk_last_used], 0
        mov     qword [blk_test_reads], 0
        mov     rax, blk_virtio_read
        mov     [blk_backend], rax
        mov     qword [blk_ready], 1
        xor     eax, eax
        blk_leave
        ret

.einval:
        mov     eax, BLK_EINVAL
        blk_leave
        ret
.fail_eio:
        mov     eax, BLK_EIO
.fail:
        ; fail stop: mark FAILED best effort, disarm the backend seam
        mov     r15d, eax
        cmp     qword [blk_iobase], 0
        je      .fail_disarm
        mov     ecx, BLK_PIO_STATUS
        mov     edx, BLK_STATUS_FAILED
        call    blk_pio_out8
.fail_disarm:
        mov     qword [blk_ready], 0
        mov     qword [blk_backend], 0
        mov     eax, r15d
        blk_leave
        ret

; ------------------------------------------------------------------
; the public read seam, a leaf dispatcher
; in
; rcx = start LBA, rdx = caller buffer (kernel VA), r8 = sector count
; out
; eax = BLK_OK or a negative status
; a future initramfs ramdisk replaces blk_backend with its own routine
; implementing exactly this ABI; 0 means no backend is armed
; ------------------------------------------------------------------

blk_read:
        mov     rax, [blk_backend]
        test    rax, rax
        jz      .notready
        jmp     rax
.notready:
        mov     eax, BLK_ENOTREADY
        ret

; ------------------------------------------------------------------
; the virtio read backend: T_IN only, chunked through the bounce page
; in
; rcx = start LBA, rdx = caller buffer, r8 = sector count
; out
; eax = BLK_OK, BLK_EINVAL (n = 0), BLK_EFAULT (null buffer),
;       BLK_EOVERFLOW (range beyond capacity, no device traffic),
;       BLK_EIO (device fault or timeout, the backend disarms)
; one request in flight: desc 0 header -> desc 1 data -> desc 2 status
; ------------------------------------------------------------------

blk_virtio_read:
        blk_enter
        cld
        cmp     qword [blk_ready], 0
        je      .notready
        test    r8, r8
        jz      .einval
        test    rdx, rdx
        jz      .efault
        mov     rax, rcx
        add     rax, r8
        jc      .erange
        cmp     rax, [blk_capacity]
        ja      .erange

        mov     r13, rcx                ; current LBA
        mov     r14, r8                 ; sectors left
        mov     r15, rdx                ; caller cursor
        mov     r12, [blk_qnum]
        dec     r12                     ; slot mask, qnum is a power of 2

        ; ring geometry, fixed for the whole transfer: r10 points at
        ; the avail ring, r11 at the used ring (legacy 4096 align)
        mov     rsi, blk_vring
        mov     rax, [blk_qnum]
        mov     rdx, rax
        shl     rax, 4                  ; avail_off = 16*q
        shl     rdx, 1
        lea     r11, [rax + rdx + 6]    ; 16*q + 6 + 2*q, pre align
        add     r11, BLK_VRING_ALIGN - 1
        and     r11, not (BLK_VRING_ALIGN - 1)
        add     r11, rsi                ; used ring VA
        lea     r10, [rsi + rax]        ; avail ring VA

.chunk:
        mov     rbx, r14
        cmp     rbx, BLK_DMA_SECTORS
        jbe     .have_n
        mov     rbx, BLK_DMA_SECTORS
.have_n:
        ; the request header and the status sentinel
        mov     rdx, blk_req
        mov     dword [rdx + BLK_REQ_HDR_OFF], BLK_T_IN
        mov     dword [rdx + BLK_REQ_HDR_OFF + 4], 0
        mov     [rdx + BLK_REQ_HDR_OFF + 8], r13
        mov     byte [rdx + BLK_REQ_STATUS_OFF], 0xFF

        ; desc 0: header, device readable, chains to the data
        mov     rax, [blk_req_phys]
        mov     [rsi + 0], rax
        mov     dword [rsi + 8], BLK_HEADER_SIZE
        mov     word [rsi + 12], BLK_DESC_F_NEXT
        mov     word [rsi + 14], BLK_DESC_DATA
        ; desc 1: the bounce page, device writable, chains to status
        mov     rax, [blk_dma_phys]
        mov     [rsi + 16], rax
        mov     eax, ebx
        shl     eax, 9
        mov     [rsi + 24], eax
        mov     word [rsi + 28], BLK_DESC_F_WRITE or BLK_DESC_F_NEXT
        mov     word [rsi + 30], BLK_DESC_STATUS
        ; desc 2: the status byte, device writable, end of chain
        mov     rax, [blk_req_phys]
        add     rax, BLK_REQ_STATUS_OFF
        mov     [rsi + 32], rax
        mov     dword [rsi + 40], BLK_STATUS_SIZE
        mov     word [rsi + 44], BLK_DESC_F_WRITE
        mov     word [rsi + 46], 0

        ; publish the head in the avail ring, free running u16 idx
        movzx   edx, word [r10 + 2]
        mov     r9d, edx
        and     r9d, r12d
        mov     word [r10 + 4 + r9*2], BLK_DESC_HEAD
        inc     edx
        mov     word [r10 + 2], dx

        ; notify queue 0
        mov     ecx, BLK_PIO_QUEUE_NOTIFY
        mov     edx, BLK_QUEUE_SEL
        call    blk_pio_out16

        ; poll the used idx with a bounded pause budget, IF stays 0
        mov     r9d, BLK_POLL_MAX
        movzx   edx, word [blk_last_used]
        inc     edx
        and     edx, BLK_U16_MASK
.poll:
        cmp     word [r11 + 2], dx
        je      .done
        pause
        dec     r9d
        jnz     .poll
        jmp     .eio
.done:
        ; the used entry must name our head and count data + status
        movzx   r9d, word [blk_last_used]
        and     r9d, r12d
        cmp     dword [r11 + 4 + r9*8], BLK_DESC_HEAD
        jne     .eio
        mov     eax, ebx
        shl     eax, 9
        inc     eax
        cmp     dword [r11 + 8 + r9*8], eax
        jne     .eio
        cmp     byte [blk_req + BLK_REQ_STATUS_OFF], BLK_S_OK
        jne     .eio
        mov     [blk_last_used], rdx

        ; copy the bounce page into the caller buffer, DF = 0
        mov     rax, rsi
        mov     rsi, blk_dma
        mov     rdi, r15
        mov     rcx, rbx
        shl     rcx, 9
        rep movsb
        mov     rsi, rax

        inc     qword [blk_test_reads]
        add     r13, rbx
        mov     rax, rbx
        shl     rax, 9
        add     r15, rax
        sub     r14, rbx
        jnz     .chunk

        xor     eax, eax
        blk_leave
        ret

.notready:
        mov     eax, BLK_ENOTREADY
        blk_leave
        ret
.einval:
        mov     eax, BLK_EINVAL
        blk_leave
        ret
.efault:
        mov     eax, BLK_EFAULT
        blk_leave
        ret
.erange:
        mov     eax, BLK_EOVERFLOW
        blk_leave
        ret
.eio:
        ; fail stop: a desynchronised ring must never serve again
        mov     qword [blk_ready], 0
        mov     qword [blk_backend], 0
        mov     eax, BLK_EIO
        blk_leave
        ret
