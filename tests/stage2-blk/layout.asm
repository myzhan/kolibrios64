;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Layout probe for the stage 2 blk module, task 131, contract FS-v1.
;;
;; This file is a test harness, not part of the kernel. It assembles
;; kernel/blk/blk.asm exactly as the kernel will include it: behind
;; cpu.asm (console, panic). The blk module calls nothing else outside
;; its own files, so no MM/IRQ stub is needed. Nothing here is executed
;; and no machine is started.
;;
;; BLK_PROBE_ORG selects the link base; check.py assembles the probe
;; twice, once at 0 for byte offsets and once at the kernel base to
;; prove the module links high with the same internal layout.
;;
;; The manifest sits after blk.asm: the module's own end assertion
;; runs at its include site and the data block is still the last module
;; byte. The manifest is a fixed list of qwords in the order check.py
;; expects; the magic and the entry count are checked before anything
;; else is read.
;;

use64

if defined BLK_PROBE_ORG
        org BLK_PROBE_ORG
else
        org 0
end if

include '../../kernel/cpu/cpu.asm'

BLK_HOSTED = 1
include '../../kernel/blk/blk.asm'

; ------------------------------------------------------------------
; manifest, fixed order, read by check.py
; ------------------------------------------------------------------

align 8
blk_probe:
        dq      'K64BLKP1'                  ; magic
        dq      1                           ; manifest version
        dq      blk_code_start
        dq      blk_code_end
        dq      blk_data_start
        dq      blk_data_end
        dq      blk_pio_in32
        dq      blk_pio_in16
        dq      blk_pio_in8
        dq      blk_pio_out32
        dq      blk_pio_out16
        dq      blk_pio_out8
        dq      blk_pci_cfg_read
        dq      blk_pci_cfg_write
        dq      blk_pci_scan
        dq      blk_init
        dq      blk_read
        dq      blk_virtio_read
        dq      blk_selftest
        dq      blk_st_fail_cap
        dq      blk_st_fail_bpb
        dq      blk_st_fail_rom
        dq      blk_st_fail_range
        dq      blk_st_fail_last
        dq      blk_st_fail_chunk
        dq      blk_st_fail_count
        dq      msg_blk_selftest_mask
        dq      msg_blk_nl
        dq      blk_ready
        dq      blk_backend
        dq      blk_iobase
        dq      blk_pci_addr
        dq      blk_qnum
        dq      blk_capacity
        dq      blk_vring_phys
        dq      blk_dma_phys
        dq      blk_req_phys
        dq      blk_last_used
        dq      blk_test_mask
        dq      blk_test_reads
        dq      blk_test_lba0_buf
        dq      blk_test_data_buf
        dq      blk_test_scratch
        dq      blk_vring
        dq      blk_dma
        dq      blk_req
        dq      cpu_com1_puts
        dq      cpu_com1_puthex
        dq      BLK_PCI_ADDR_PORT
        dq      BLK_PCI_DATA_PORT
        dq      BLK_PCI_VENDOR
        dq      BLK_PCI_DEVICE
        dq      BLK_PCI_SUBVENDOR
        dq      BLK_PCI_SUBDEVICE
        dq      BLK_PCI_REVISION
        dq      BLK_PCI_REG_ID
        dq      BLK_PCI_REG_COMMAND
        dq      BLK_PCI_REG_REVISION
        dq      BLK_PCI_REG_BAR0
        dq      BLK_PCI_REG_SUBSYS
        dq      BLK_PCI_CMD_IO
        dq      BLK_PCI_CMD_MASTER
        dq      BLK_PCI_BAR_IO
        dq      BLK_PCI_BAR_MASK
        dq      BLK_PCI_MAX_DEV
        dq      BLK_PIO_HOST_FEATURES
        dq      BLK_PIO_GUEST_FEATURES
        dq      BLK_PIO_QUEUE_PFN
        dq      BLK_PIO_QUEUE_NUM
        dq      BLK_PIO_QUEUE_SEL
        dq      BLK_PIO_QUEUE_NOTIFY
        dq      BLK_PIO_STATUS
        dq      BLK_PIO_ISR
        dq      BLK_PIO_CONFIG_OFF
        dq      BLK_STATUS_ACK
        dq      BLK_STATUS_DRIVER
        dq      BLK_STATUS_DRIVER_OK
        dq      BLK_STATUS_FAILED
        dq      BLK_CFG_CAPACITY
        dq      BLK_CFG_BLK_SIZE
        dq      BLK_T_IN
        dq      BLK_T_OUT
        dq      BLK_T_FLUSH
        dq      BLK_S_OK
        dq      BLK_S_IOERR
        dq      BLK_S_UNSUPP
        dq      BLK_SECTOR_SIZE
        dq      BLK_HEADER_SIZE
        dq      BLK_STATUS_SIZE
        dq      BLK_DESC_F_NEXT
        dq      BLK_DESC_F_WRITE
        dq      BLK_AVAIL_F_NOINT
        dq      BLK_VRING_ALIGN
        dq      BLK_DESC_SIZE
        dq      BLK_U16_MASK
        dq      BLK_QNUM_MAX
        dq      BLK_QUEUE_SEL
        dq      BLK_DESC_HEAD
        dq      BLK_DESC_DATA
        dq      BLK_DESC_STATUS
        dq      BLK_VRING_PAGES
        dq      BLK_VRING_MAX_TOTAL
        dq      BLK_DMA_SECTORS
        dq      BLK_REQ_HDR_OFF
        dq      BLK_REQ_STATUS_OFF
        dq      BLK_POLL_MAX
        dq      BLK_BOOTINFO_KERNEL_PHYS_START
        dq      BLK_BOOTINFO_INITRAMFS_ADDR
        dq      BLK_BOOTINFO_INITRAMFS_SIZE
        dq      BLK_TEST_CAPACITY
        dq      BLK_TEST_DATA_LBA
        dq      BLK_TEST_BPB_SIG
        dq      BLK_TEST_OEM_ID
        dq      BLK_TEST_ROM_PREFIX
        dq      BLK_ST_CAP
        dq      BLK_ST_BPB
        dq      BLK_ST_ROM
        dq      BLK_ST_RANGE
        dq      BLK_ST_LAST
        dq      BLK_ST_CHUNK
        dq      BLK_ST_COUNT
        dq      BLK_TEST_READS_FINAL
        dq      BLK_OK
        dq      BLK_EINVAL
        dq      BLK_ENOENT
        dq      BLK_ENOTREADY
        dq      BLK_EIO
        dq      BLK_EOVERFLOW
        dq      BLK_EFAULT
        dq      BLK_ENOTSUP
        dq      BLK_PAGE_SIZE
        dq      BLK_PHYS_LIMIT
        dq      BLK_KERNEL_BASE
blk_probe_end:

if (blk_probe_end - blk_probe) <> 133 * 8
        err 'the manifest must keep its fixed entry count'
end if
