;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; FS-v1 M1 block layer: all mutable state.
;;
;; The data block is emitted as real zero bytes in the flat image, 4 KiB
;; aligned at both ends, exactly like the other modules: "db N dup 0"
;; everywhere, never "rb", and an explicit zero tail up to the page
;; boundary so the last page stays in the file.
;;
;; The vring, the DMA bounce page and the request scratch page are page
;; aligned inside this block on purpose: the device addresses them by
;; guest physical address through the loader identity map, so they must
;; be real image bytes in low RAM, never a BSS promise. The evidence
;; buffers keep the LBA0 and LBA101 sectors for the host gdbstub audit.
;;

        align 4096
blk_data_start:

; driver state, all zero until blk_init succeeds
blk_ready:              dq 0    ; 1 only after DRIVER_OK read back
blk_backend:            dq 0    ; the blk_read seam, 0 = disarmed
blk_iobase:             dq 0    ; measured legacy PIO BAR0 base
blk_pci_addr:           dq 0    ; CF8 address of the found function
blk_qnum:               dq 0    ; measured queue size, a power of two
blk_capacity:           dq 0    ; config capacity in 512 B sectors
blk_vring_phys:         dq 0    ; guest physical base of the vring
blk_dma_phys:           dq 0    ; guest physical base of the bounce page
blk_req_phys:           dq 0    ; guest physical base of the request page
blk_last_used:          dq 0    ; driver side free running u16 consume idx

; test hooks and evidence, zero in a production boot:
;  - blk_test_mask: which self test legs reported a failure;
;  - blk_test_reads: completed virtio requests, the frozen final is 7;
;  - the two evidence buffers keep whole sectors for the host audit.
blk_test_mask:          dq 0
blk_test_reads:         dq 0
blk_test_lba0_buf:      db BLK_SECTOR_SIZE dup 0
blk_test_data_buf:      db BLK_SECTOR_SIZE dup 0

; self test scratch, 16 sectors so the 9 sector chunk legs fit
blk_test_scratch:       db 16 * BLK_SECTOR_SIZE dup 0

; the split virtqueue: desc table, avail ring, used ring padded to the
; legacy 4096 align; 3 pages cover the worst layout at qnum 256
        align 4096
blk_vring:              db BLK_VRING_PAGES * BLK_PAGE_SIZE dup 0

; the DMA bounce page, at most 8 sectors per request
blk_dma:                db BLK_PAGE_SIZE dup 0

; the request scratch page: 16 B header at +0, 1 B status at +16,
; both device addressed through the identity map
blk_req:                db BLK_PAGE_SIZE dup 0

; tail padding, emitted as data so the file keeps every zero byte
        db (BLK_PAGE_SIZE - (($ - blk_data_start) and (BLK_PAGE_SIZE-1))) and (BLK_PAGE_SIZE-1) dup 0
blk_data_end:

if (blk_data_end - blk_data_start) mod BLK_PAGE_SIZE
        display "data block is not page aligned", 13, 10
        err
end if
if (blk_vring - blk_data_start) mod BLK_PAGE_SIZE
        display "vring is not page aligned", 13, 10
        err
end if
if (blk_dma - blk_vring) <> BLK_VRING_PAGES * BLK_PAGE_SIZE
        display "DMA page must follow the vring reservation", 13, 10
        err
end if
if (blk_req - blk_dma) <> BLK_PAGE_SIZE
        display "request page must follow the DMA page", 13, 10
        err
end if
