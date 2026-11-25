;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; FS-v1 M1 block layer: the private copy of the shared handover
;; contract (K64-FOUNDATION-v1) plus the frozen FS-v1 blk contract.
;;
;; The shared values are duplicated here with the BLK_ prefix exactly
;; like MM_ and INPUT_: the assembly time checks keep the local copy
;; self consistent, core/contracts.inc proves the three copies agree
;; and the stage2-blk gate compares everything against hand written
;; expectations and tests/stage2-blk/virtqueue_model.py.
;;
;; The backend is virtio-blk-pci-transitional driven through the legacy
;; PIO BAR0 window with a polled split virtqueue, read only (T_IN).
;; No interrupt is ever wired: the IRQ dispatch table stays frozen.
;;
;; Calling convention (identical to the one the foundation task fixes):
;;  - Microsoft x64 integer ABI, RCX/RDX/R8/R9 arguments;
;;  - 32 bytes of shadow space, RSP mod 16 = 0 before a call, no red zone;
;;  - RBX/RBP/RSI/RDI/R12-R15 are callee saved;
;;  - EAX is a signed status, RDX is the main result, 0 is success;
;;  - DF = 0 and IF unchanged on return.
;;

use64

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; kernel virtual base, the shared const.inc value; a plain constant,
; this fasm2 build never converges on "if ~ defined X / X = ..."

BLK_KERNEL_BASE = 0xFFFFFFFF80000000

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; page geometry, the shared contract values

BLK_PAGE_SHIFT = 12
BLK_PAGE_SIZE  = 0x1000
BLK_PHYS_LIMIT = 0x100000000     ; exclusive, low 4 GiB identity domain

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; status codes, EAX is signed, the shared K64 spelling

BLK_OK         = 0
BLK_EINVAL     = -1
BLK_ENOMEM     = -2
BLK_ENOENT     = -3
BLK_EEXIST     = -4
BLK_EPERM      = -5
BLK_ENOTSUP    = -6
BLK_ENOTREADY  = -7
BLK_EALREADY   = -8
BLK_EIO        = -9
BLK_EOVERFLOW  = -10
BLK_EFAULT     = -11
BLK_EBUSY      = -12

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; BootInfo v1, only the field this module reads: the physical load
; base of the kernel image, used to convert high half data labels into
; guest physical addresses inside the loader identity map

BLK_BOOTINFO_KERNEL_PHYS_START = 56
BLK_BOOTINFO_KERNEL_PHYS_END   = 64
BLK_BOOTINFO_INITRAMFS_ADDR    = 128    ; reserved for the ramdisk fallback
BLK_BOOTINFO_INITRAMFS_SIZE    = 136

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; PCI configuration space access, the frozen CF8/CFC mechanism

BLK_PCI_ADDR_PORT = 0xCF8
BLK_PCI_DATA_PORT = 0xCFC
BLK_PCI_ENABLE    = 0x80000000
BLK_PCI_BUS       = 0           ; QEMU puts the device on bus 0
BLK_PCI_MAX_DEV   = 32          ; devices 0..31, function 0 only
BLK_PCI_DEV_SHIFT = 11

BLK_PCI_REG_ID       = 0x00     ; vendor u16 | device u16
BLK_PCI_REG_COMMAND  = 0x04     ; u16, IO space + bus master
BLK_PCI_REG_REVISION = 0x08     ; u8 at the low byte
BLK_PCI_REG_BAR0     = 0x10     ; legacy PIO BAR, bit 0 = IO space
BLK_PCI_REG_SUBSYS   = 0x2C     ; subvendor u16 | subdevice u16

BLK_PCI_CMD_IO     = 0x01
BLK_PCI_CMD_MASTER = 0x04
BLK_PCI_BAR_IO     = 0x01
BLK_PCI_BAR_MASK   = 0xFFFC

; PCI identity of virtio-blk-pci-transitional (virtio 1.0 4.1.2)
BLK_PCI_VENDOR    = 0x1AF4
BLK_PCI_DEVICE    = 0x1001      ; transitional block device
BLK_PCI_SUBVENDOR = 0x1AF4
BLK_PCI_SUBDEVICE = 0x0002      ; legacy subsystem id of virtio-blk
BLK_PCI_REVISION  = 0x00        ; legacy revision

if BLK_PCI_VENDOR <> 0x1AF4 | BLK_PCI_DEVICE <> 0x1001
        display "virtio transitional PCI identity changed", 13, 10
        err
end if

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; legacy PIO register offsets inside BAR0 (virtio 0.9.5 layout,
; MSI-X disabled so the device config starts right after the ISR)

BLK_PIO_HOST_FEATURES  = 0x00   ; u32 R
BLK_PIO_GUEST_FEATURES = 0x04   ; u32 RW
BLK_PIO_QUEUE_PFN      = 0x08   ; u32 RW, queue base >> 12
BLK_PIO_QUEUE_NUM      = 0x0C   ; u16 R
BLK_PIO_QUEUE_SEL      = 0x0E   ; u16 W
BLK_PIO_QUEUE_NOTIFY   = 0x10   ; u16 W
BLK_PIO_STATUS         = 0x12   ; u8 RW
BLK_PIO_ISR            = 0x13   ; u8 R, read-clear
BLK_PIO_CONFIG_OFF     = 0x14   ; device config, MSI-X disabled

; the register map must stay offset ordered, exactly like the model
if BLK_PIO_HOST_FEATURES >= BLK_PIO_GUEST_FEATURES
        display "PIO register map out of order", 13, 10
        err
end if
if BLK_PIO_GUEST_FEATURES >= BLK_PIO_QUEUE_PFN | BLK_PIO_QUEUE_PFN >= BLK_PIO_QUEUE_NUM
        display "PIO register map out of order", 13, 10
        err
end if
if BLK_PIO_QUEUE_NUM >= BLK_PIO_QUEUE_SEL | BLK_PIO_QUEUE_SEL >= BLK_PIO_QUEUE_NOTIFY
        display "PIO register map out of order", 13, 10
        err
end if
if BLK_PIO_QUEUE_NOTIFY >= BLK_PIO_STATUS | BLK_PIO_STATUS >= BLK_PIO_ISR
        display "PIO register map out of order", 13, 10
        err
end if
if BLK_PIO_ISR >= BLK_PIO_CONFIG_OFF
        display "PIO register map out of order", 13, 10
        err
end if

; device status bits (legacy: no FEATURES_OK step)
BLK_STATUS_ACK       = 0x01
BLK_STATUS_DRIVER    = 0x02
BLK_STATUS_DRIVER_OK = 0x04
BLK_STATUS_FAILED    = 0x80

; virtio-blk device config, offsets relative to BLK_PIO_CONFIG_OFF
BLK_CFG_CAPACITY = 0            ; u64, in 512 B sectors
BLK_CFG_BLK_SIZE = 20           ; u32

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; request types and status bytes, FS-v1 is read only

BLK_T_IN     = 0
BLK_T_OUT    = 1
BLK_T_FLUSH  = 4
BLK_S_OK     = 0
BLK_S_IOERR  = 1
BLK_S_UNSUPP = 2

BLK_SECTOR_SIZE = 512
BLK_HEADER_SIZE = 16            ; type u32, reserved u32, sector u64
BLK_STATUS_SIZE = 1

if BLK_HEADER_SIZE <> 16 | BLK_SECTOR_SIZE <> 512
        display "T_IN header and sector geometry changed", 13, 10
        err
end if

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; split virtqueue constants, legacy layout

BLK_DESC_F_NEXT   = 0x1
BLK_DESC_F_WRITE  = 0x2
BLK_AVAIL_F_NOINT = 0x1         ; polling driver, the device stays quiet
BLK_VRING_ALIGN   = 4096        ; legacy queue align, used ring pad
BLK_DESC_SIZE     = 16
BLK_U16_MASK      = 0xFFFF

; one queue, at most 256 entries (a power of two), single request in
; flight: desc 0 header -> desc 1 data -> desc 2 status
BLK_QNUM_MAX    = 256
BLK_QUEUE_SEL   = 0
BLK_DESC_HEAD   = 0
BLK_DESC_DATA   = 1
BLK_DESC_STATUS = 2

; the vring reservation must cover the worst legacy layout:
; used_off = align(16*q + 6 + 2*q, 4096), total = used_off + 6 + 8*q
BLK_VRING_PAGES     = 3
BLK_VRING_MAX_TOTAL = 8192 + 6 + 8 * BLK_QNUM_MAX
if BLK_VRING_MAX_TOTAL > BLK_VRING_PAGES * BLK_PAGE_SIZE
        display "vring reservation does not cover qnum 256", 13, 10
        err
end if

; the DMA bounce buffer is one page: at most 8 sectors per request,
; blk_read chunks longer transfers through it
BLK_DMA_SECTORS = 8
if BLK_DMA_SECTORS * BLK_SECTOR_SIZE > BLK_PAGE_SIZE
        display "DMA bounce buffer larger than one page", 13, 10
        err
end if

; request scratch page: header at +0, status byte at +16, both must
; live in the identity domain because the device reads/writes them
BLK_REQ_HDR_OFF    = 0
BLK_REQ_STATUS_OFF = 16

; bounded poll budget for the used ring, pause iterations; a timeout
; is EIO, never a hang and never a silent pass
BLK_POLL_MAX = 0x1000000

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; self test evidence, frozen against build/fs-v1/disk.img (8 MiB
; FAT16, sha256 e9e83dad3f6d5193ac15c7a35834cc5190ca34e978b537d80be9fa9beb7bc42b)

BLK_TEST_CAPACITY  = 16384      ; sectors of the frozen image
BLK_TEST_DATA_LBA  = 101        ; first sector of IBM.CH8, cluster 4
BLK_TEST_BPB_SIG   = 0xAA55     ; word at LBA0 + 510
BLK_TEST_OEM_ID    = 'KOLIBR64' ; 8 bytes at LBA0 + 3
; the first 8 ROM bytes of IBM.CH8: 00 E0 A2 2A 60 0C 61 08
BLK_TEST_ROM_PREFIX = 0x08610C602AA2E000

; leg failure bits of blk_test_mask
BLK_ST_CAP      = 0x01          ; leg 0: config capacity
BLK_ST_BPB      = 0x02          ; leg 1: LBA0 BPB signature/OEM
BLK_ST_ROM      = 0x04          ; leg 2: LBA101 ROM prefix
BLK_ST_RANGE    = 0x08          ; leg 3: argument and range refusal
BLK_ST_LAST     = 0x10          ; leg 4: last LBA reads OK
BLK_ST_CHUNK    = 0x20          ; leg 5: multi chunk copy, both chunks
BLK_ST_COUNT    = 0x40          ; leg 6: request counter evidence

; completed request counter after a clean self test run:
; leg1 1 + leg2 1 + leg4 1 + leg5 2+2 = 7 (the refused leg3 issues none)
BLK_TEST_READS_FINAL = 7

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; register helper macros, the full frame of the other modules

macro blk_enter
        push    rbp
        push    rbx
        push    rsi
        push    rdi
        push    r12
        push    r13
        push    r14
        push    r15
        sub     rsp, 40
end macro

macro blk_leave
        add     rsp, 40
        pop     r15
        pop     r14
        pop     r13
        pop     r12
        pop     rdi
        pop     rsi
        pop     rbx
        pop     rbp
end macro
