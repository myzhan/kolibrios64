;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Layout and behaviour probe for the stage 2 memory management module,
;; task 28.
;;
;; This file is a test harness, not part of the kernel. It assembles
;; kernel/mm/mm.asm exactly as the kernel will include it, wrapped in a
;; manifest of label addresses, in page tables and in a set of BootInfo
;; fixtures, and lets tests/stage2-mm/check.py verify the emitted bytes.
;; Nothing here is executed and no machine is started.
;;
;; The image is a flat model of the low 4 GiB physical address space:
;; every fixture sits at a fixed physical address, so the host model can
;; read a BootInfo, walk its memory map and walk the page tables out of
;; the very bytes the assembler produced, the same way the kernel would
;; read them once they are loaded at those addresses.
;;
;; MM_PROBE_ORG selects the link base; check.py assembles the probe twice,
;; once at 0 and once at the kernel base:
;;
;;  - at 0 every fixture address is its physical address, which is what
;;    the host model needs;
;;  - at the kernel base the very same file proves that the module links
;;    at 0xFFFFFFFF80000000 and that the image still ends exactly on
;;    mm_data_end, that is that no code follows the data block.
;;
;; Everything the probe adds is emitted before mm.asm, because mm.asm must
;; stay last: its data block has to end the image.
;;
;; The manifest is a fixed list of qwords in the order check.py expects;
;; the magic and the version are checked before anything else is read.
;;

format binary

if defined MM_PROBE_ORG
        org MM_PROBE_ORG
        MM_PROBE_BASE = MM_PROBE_ORG
else
        org 0
        MM_PROBE_BASE = 0
end if

use64
MM_HOSTED = 1

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; fixed physical addresses of the probe, all below 4 GiB
;
; the low arena and the low fixtures stay under 1 MiB, so they are inside
; the firmware area the allocator pins; the high arena and the high
; fixture sit above it, so a pinned table page there can only come from
; the CR3 pinner, and a pinned BootInfo page can only come from the
; BootInfo pin.

MM_PB_LOW_ARENA  = 0x001000     ; four table pages, plain identity walk
MM_PB_HI_ARENA   = 0x140000     ; window mapping, 1 GiB and 2 MiB leaves
MM_PB_PS_ARENA   = 0x150000     ; PML4 entry with the reserved PS bit
MM_PB_4G_ARENA   = 0x160000     ; table page above 4 GiB
MM_PB_KERN_ARENA = 0x170000     ; KERNEL_BASE mapped onto 0x500000
MM_PB_HI_BOOT    = 0x180000     ; BootInfo of the high fixture
MM_PB_HI_MAP     = 0x181000     ; memory map of the high fixture
MM_PB_MANY_ARENA = 0x190000     ; more table pages than the pinner may pin

; pad the image with zero bytes up to an absolute physical address
macro pad_to addr
        align 8
        if ((addr) - ($ - mm_probe_manifest)) > 0
                db ((addr) - ($ - mm_probe_manifest)) dup 0
        end if
end macro

; ------------------------------------------------------------------
; manifest, fixed order, read by check.py
; ------------------------------------------------------------------

align 8
mm_probe_manifest:
        dq      'K64MMP01'                  ; magic
        dq      1                           ; manifest version
        dq      MM_PROBE_BASE               ; link base of this image

; mutable state, so the checker can locate it inside the data block
        dq      mm_data_start
        dq      mm_data_end
        dq      mm_bitmap_eligible
        dq      mm_bitmap_allocated
        dq      mm_bitmap_pinned
        dq      mm_desc_scratch
        dq      mm_table_seen
        dq      mm_table_seen_count
        dq      mm_pmm_state
        dq      mm_vmm_state
        dq      mm_heap_state
        dq      mm_heap_allocs
        dq      mm_heap_grows
        dq      mm_boot_state
        dq      mm_test_fail
        dq      mm_test_fail_hits
        dq      mm_test_selftest_mask
        dq      mm_pmm_stats_scratch
        dq      mm_heap_stats_scratch
        dq      mm_selftest_query
        dq      mm_test_pages

; public entry points
        dq      pmm_init
        dq      pmm_alloc_page
        dq      pmm_free_page
        dq      pmm_get_stats
        dq      pmm_selftest
        dq      vmm_init
        dq      vmm_query
        dq      vmm_map_page
        dq      vmm_unmap_page
        dq      vmm_boot_protect
        dq      vmm_selftest
        dq      heap_init
        dq      heap_alloc
        dq      heap_free
        dq      heap_get_stats
        dq      heap_selftest
        dq      mm_selftest

; internal helpers, checked for presence only
        dq      mm_params_validate
        dq      mm_cpu_phys_bits
        dq      mm_desc_is_eligible
        dq      mm_pin_range
        dq      mm_pin_table_page
        dq      mm_pin_tables
        dq      mm_memmap_validate
        dq      mm_memmap_mark
        dq      mm_bootmap_build
        dq      mm_bitmap_clear
        dq      mm_bitmap_fill
        dq      mm_bitmap_count
        dq      mm_pfn_zero
        dq      mm_pmm_hook
        dq      mm_pfn_next_free
        dq      mm_pfn_alloc
        dq      mm_pmm_alloc_table
        dq      mm_pmm_drop_table
        dq      mm_pmm_recount
        dq      mm_vmm_resolve
        dq      mm_vmm_protect_pass
        dq      mm_heap_grow

; contract constants, compared against hand written expectations
        dq      MM_PAGE_SHIFT
        dq      MM_PAGE_SIZE
        dq      MM_PHYS_LIMIT
        dq      MM_PFN_COUNT
        dq      MM_BITMAP_BYTES
        dq      MM_KERNEL_BASE
        dq      MM_WINDOW_BASE
        dq      MM_WINDOW_LIMIT
        dq      MM_WINDOW_SIZE
        dq      MM_HEAP_BASE
        dq      MM_HEAP_LIMIT
        dq      MM_HEAP_MAX
        dq      MM_HEAP_INITIAL
        dq      MM_PTE_NX
        dq      MM_PTE_ADDR_MASK
        dq      MM_MAP_ALLOWED
        dq      MM_BOOTINFO_SIZE
        dq      MM_INIT_PARAMS_size
        dq      MM_EFI_DESC_MIN_SIZE
        dq      MM_MAP_MAX_SIZE
        dq      MM_MAP_MAX_DESCS
        dq      MM_IMAGE_MAX
        dq      MM_CR3_PIN_MAX
        dq      MM_TABLE_WALK_MAX
        dq      MM_OK
        dq      MM_EINVAL
        dq      MM_ENOMEM
        dq      MM_ENOENT
        dq      MM_EEXIST
        dq      MM_EPERM
        dq      MM_ENOTSUP
        dq      MM_ENOTREADY
        dq      MM_EALREADY
        dq      MM_EIO
        dq      MM_EOVERFLOW
        dq      MM_EFAULT
        dq      MM_EBUSY

; record offsets, checked against the handover contract
        dq      MM_BOOTINFO.signature
        dq      MM_BOOTINFO.version
        dq      MM_BOOTINFO.struct_size
        dq      MM_BOOTINFO.flags
        dq      MM_BOOTINFO.memmap_addr
        dq      MM_BOOTINFO.memmap_size
        dq      MM_BOOTINFO.memmap_desc_size
        dq      MM_BOOTINFO.memmap_desc_version
        dq      MM_BOOTINFO.memmap_desc_count
        dq      MM_BOOTINFO.kernel_phys_start
        dq      MM_BOOTINFO.kernel_phys_end
        dq      MM_BOOTINFO.stack_phys_start
        dq      MM_BOOTINFO.stack_phys_end
        dq      MM_BOOTINFO.gop_framebuffer_addr
        dq      MM_BOOTINFO.rsdp_addr
        dq      MM_BOOTINFO.initramfs_addr
        dq      MM_BOOTINFO.initramfs_size
        dq      MM_BOOTINFO.reserved
        dq      MM_MEM_DESC.type
        dq      MM_MEM_DESC.phys_start
        dq      MM_MEM_DESC.virt_start
        dq      MM_MEM_DESC.pages
        dq      MM_MEM_DESC.attributes
        dq      MM_INIT_PARAMS.version
        dq      MM_INIT_PARAMS.size
        dq      MM_INIT_PARAMS.bootinfo_va
        dq      MM_INIT_PARAMS.bootinfo_phys
        dq      MM_INIT_PARAMS.cr3_phys
        dq      MM_INIT_PARAMS.stack_top_va
        dq      MM_INIT_PARAMS.flags
        dq      MM_INIT_PARAMS.reserved1
        dq      MM_INIT_PARAMS.reserved2
        dq      MM_PMM_STATE.ready
        dq      MM_PMM_STATE.managed_limit
        dq      MM_PMM_STATE.total_pages
        dq      MM_PMM_STATE.free_pages
        dq      MM_PMM_STATE.allocated_pages
        dq      MM_PMM_STATE.reserved_pages
        dq      MM_PMM_STATE.next_pfn
        dq      MM_PMM_STATE.root_phys
        dq      MM_VMM_STATE.ready
        dq      MM_VMM_STATE.phys_bits
        dq      MM_VMM_STATE.root_phys
        dq      MM_VMM_STATE.pa_mask
        dq      MM_VMM_STATE.window_base
        dq      MM_VMM_STATE.window_limit
        dq      MM_VMM_STATE.table_allocs
        dq      MM_VMM_STATE.table_drops
        dq      MM_HEAP_STATE.ready
        dq      MM_HEAP_STATE.base
        dq      MM_HEAP_STATE.brk
        dq      MM_HEAP_STATE.mapped_end
        dq      MM_HEAP_STATE.limit
        dq      MM_HEAP_STATE.reserved
        dq      MM_BOOT_STATE.bootinfo_phys
        dq      MM_BOOT_STATE.cr3_phys
        dq      MM_BOOT_STATE.memmap_phys
        dq      MM_BOOT_STATE.memmap_size
        dq      MM_BOOT_STATE.kernel_start
        dq      MM_BOOT_STATE.kernel_end
        dq      MM_BOOT_STATE.stack_start
        dq      MM_BOOT_STATE.stack_end
        dq      MM_BOOT_STATE.stack_top_va
        dq      MM_BOOT_STATE.desc_stride
        dq      MM_BOOT_STATE.desc_count
        dq      MM_PMM_STATS.managed_limit
        dq      MM_PMM_STATS.total
        dq      MM_PMM_STATS.free
        dq      MM_PMM_STATS.allocated
        dq      MM_PMM_STATS.reserved
        dq      MM_PMM_STATS.next_pfn
        dq      MM_VMM_QUERY.physical_address
        dq      MM_VMM_QUERY.leaf_entry_va
        dq      MM_VMM_QUERY.raw_leaf
        dq      MM_VMM_QUERY.page_size
        dq      MM_VMM_QUERY.effective_flags
        dq      MM_HEAP_STATS.base
        dq      MM_HEAP_STATS.brk
        dq      MM_HEAP_STATS.mapped_end
        dq      MM_HEAP_STATS.limit
        dq      MM_VQ_PRESENT
        dq      MM_VQ_RW
        dq      MM_VQ_US
        dq      MM_VQ_NX

; address spaces, fixture tables and their lengths
        dq      mm_fx_pml4
        dq      mm_fx_pdpt
        dq      mm_fx_pd
        dq      mm_fx_pt
        dq      mm_fx2_base
        dq      mm_fx_ps_pml4
        dq      mm_fx_4g_pml4
        dq      mm_fx_many_base
        dq      mm_fx_high_boot
        dq      mm_fx_high_map
        dq      mm_fx_count
        dq      mm_fx_table
        dq      mm_qry_count
        dq      mm_qry_table
        dq      mm_map_count
        dq      mm_map_table
        dq      mm_unmap_count
        dq      mm_unmap_table
        dq      mm_ip_stride40
        dq      mm_ip_high
        dq      mm_ip_badver
        dq      mm_kern_pml4
        dq      mm_kern_pt

; MMIO mapping: the new public entry, its contract constants and matrix
        dq      vmm_map_mmio
        dq      MM_MMIO_LAPIC_VA
        dq      MM_MMIO_FLAGS
        dq      MM_CORE_TEST_SLOT
        dq      mm_mmio_count
        dq      mm_mmio_table

; BootInfo feature flags and the GOP format ceiling
        dq      MM_BOOTINFO_FLAG_MAP
        dq      MM_BOOTINFO_FLAG_FRAMEBUFFER
        dq      MM_BOOTINFO_FLAG_RSDP
        dq      MM_BOOTINFO_FLAG_INITRAMFS
        dq      MM_BOOTINFO_FLAG_KNOWN
        dq      MM_GOP_PIXEL_FORMAT_MAX

; ------------------------------------------------------------------
; low address space, MM_PB_LOW_ARENA
;
; one PML4 entry covers the low 512 GiB and gives the pinner four table
; pages to find. The PDPT holds a 1 GiB leaf and the PD holds a 2 MiB
; leaf, so the pinner has to skip both instead of descending into them.
; ------------------------------------------------------------------

pad_to MM_PB_LOW_ARENA

mm_fx_pml4:
        dq      mm_fx_pdpt + 3              ; present, writable
        dq      511 dup 0

mm_fx_pdpt:
        dq      mm_fx_pd + 3
        dq      0x40000000 + 0x83           ; 1 GiB leaf, PS, writable
        dq      510 dup 0

mm_fx_pd:
        dq      mm_fx_pt + 3
        dq      0x200000 + 0x8000000000000083 ; 2 MiB leaf, PS, NX
        dq      510 dup 0

mm_fx_pt:
        dq      0x00400000 + 0x8000000000000007 ; RW, US, NX
        dq      0x000FFFFFFFFFF003              ; read only, executable, 52 bit
        dq      0                                   ; absent
        dq      0x00500000 + 0x003                  ; RW, executable
        dq      508 dup 0

; ------------------------------------------------------------------
; fixtures
;
; one EFI descriptor with the mandatory 40 byte prefix, and a padded
; variant that fills the remaining stride bytes with a recognisable
; pattern so a parser that ignores memmap_desc_size is caught.
; ------------------------------------------------------------------

macro fx_desc type, phys, pages, attrs
        dd      type
        dd      0
        dq      phys
        dq      phys
        dq      pages
        dq      attrs
end macro

macro fx_desc_pad type, phys, pages, attrs, stride
        fx_desc type, phys, pages, attrs
        dq      ((stride - 40) / 8) dup 0xF00D000000000000
end macro

macro fx_bootinfo map, size, stride, count
        dq      'K64BOOTI'
        dd      1
        dd      208
        dq      1
        dq      map
        dq      size
        dd      stride
        dd      1
        dq      count
        dq      0x00500000                  ; kernel start
        dq      0x00501000                  ; kernel end
        dq      0x00500000                  ; stack start
        dq      0x00501000                  ; stack end
        dq      4 dup 0                     ; no framebuffer
        dq      0                           ; no RSDP
        dq      0, 0                        ; no initramfs
        dq      8 dup 0                     ; reserved
end macro

; the same record with explicit kernel/stack ranges and descriptor version
macro fx_bootinfo_ks map, size, stride, count, kstart, kend, sstart, send, descver
        dq      'K64BOOTI'
        dd      1
        dd      208
        dq      1
        dq      map
        dq      size
        dd      stride
        dd      descver
        dq      count
        dq      kstart
        dq      kend
        dq      sstart
        dq      send
        dq      4 dup 0
        dq      0
        dq      0, 0
        dq      8 dup 0
end macro

; fixture 0: the baseline, stride 40, ordered, three eligible pages
align 8
fx_stride40_map:
        fx_desc 7, 0x00100000, 1, 8         ; conventional, cacheable
        fx_desc 7, 0x00200000, 2, 8
        fx_desc 3, 0x00300000, 1, 8         ; boot services, not ours
        fx_desc 7, 0x00400000, 1, 0x8000000000000008 ; runtime, not ours
fx_stride40_end:
fx_stride40_boot:
        fx_bootinfo fx_stride40_map, fx_stride40_end - fx_stride40_map, 40, 4

; fixture 1: the same ranges described with a 48 byte stride
align 8
fx_stride48_map:
        fx_desc_pad 7, 0x00100000, 1, 8, 48
        fx_desc_pad 7, 0x00200000, 2, 8, 48
        fx_desc_pad 3, 0x00300000, 1, 8, 48
        fx_desc_pad 7, 0x00400000, 1, 0x8000000000000008, 48
fx_stride48_end:
fx_stride48_boot:
        fx_bootinfo fx_stride48_map, fx_stride48_end - fx_stride48_map, 48, 4

; fixture 2: the same ranges described with a 64 byte stride
align 8
fx_stride64_map:
        fx_desc_pad 7, 0x00100000, 1, 8, 64
        fx_desc_pad 7, 0x00200000, 2, 8, 64
        fx_desc_pad 3, 0x00300000, 1, 8, 64
        fx_desc_pad 7, 0x00400000, 1, 0x8000000000000008, 64
fx_stride64_end:
fx_stride64_boot:
        fx_bootinfo fx_stride64_map, fx_stride64_end - fx_stride64_map, 64, 4

; fixture 3: the same four descriptors in reverse order
align 8
fx_unorder_map:
        fx_desc 7, 0x00400000, 1, 0x8000000000000008
        fx_desc 3, 0x00300000, 1, 8
        fx_desc 7, 0x00200000, 2, 8
        fx_desc 7, 0x00100000, 1, 8
fx_unorder_end:
fx_unorder_boot:
        fx_bootinfo fx_unorder_map, fx_unorder_end - fx_unorder_map, 40, 4

; fixture 4: two descriptors describe the same page
align 8
fx_overlap_map:
        fx_desc 7, 0x00100000, 2, 8
        fx_desc 7, 0x00101000, 1, 8
fx_overlap_end:
fx_overlap_boot:
        fx_bootinfo fx_overlap_map, fx_overlap_end - fx_overlap_map, 40, 2

; fixture 5: the page count overflows the end point
align 8
fx_overflow_map:
        fx_desc 7, 0x00100000, 0xFFFFFFFFFFFFFFFF, 8
fx_overflow_end:
fx_overflow_boot:
        fx_bootinfo fx_overflow_map, fx_overflow_end - fx_overflow_map, 40, 1

; fixture 6: a range that runs past 4 GiB must be clipped, not rejected
align 8
fx_clip_map:
        fx_desc 7, 0xFFFFF000, 16, 8
fx_clip_end:
fx_clip_boot:
        fx_bootinfo fx_clip_map, fx_clip_end - fx_clip_map, 40, 1

; fixture 7: every EFI type, only conventional memory may be claimed
align 8
fx_types_map:
        fx_desc 0, 0x00100000, 1, 8
        fx_desc 1, 0x00101000, 1, 8
        fx_desc 2, 0x00102000, 1, 8
        fx_desc 3, 0x00103000, 1, 8
        fx_desc 4, 0x00104000, 1, 8
        fx_desc 5, 0x00105000, 1, 8
        fx_desc 6, 0x00106000, 1, 8
        fx_desc 7, 0x00107000, 1, 8
        fx_desc 8, 0x00108000, 1, 8
        fx_desc 9, 0x00109000, 1, 8
        fx_desc 10, 0x0010A000, 1, 8
        fx_desc 11, 0x0010B000, 1, 8
        fx_desc 12, 0x0010C000, 1, 8
        fx_desc 13, 0x0010D000, 1, 8
        fx_desc 14, 0x0010E000, 1, 8
        fx_desc 15, 0x0010F000, 1, 8
        fx_desc 7, 0x00110000, 1, 0x0000000000001008   ; write protected
        fx_desc 7, 0x00111000, 1, 0x0000000000002008   ; read protected
        fx_desc 7, 0x00112000, 1, 0x0000000000020008   ; read only
        fx_desc 7, 0x00113000, 1, 0x0000000000008008   ; non volatile
        fx_desc 7, 0x00114000, 1, 0x0000000000000000   ; not cacheable
fx_types_end:
fx_types_boot:
        fx_bootinfo fx_types_map, fx_types_end - fx_types_map, 40, 21

; fixture 8: wrong signature
align 8
fx_badsign_map:
        fx_desc 7, 0x00100000, 1, 8
fx_badsign_end:
fx_badsign_boot:
        dq      'K64BOOTX'
        dd      1
        dd      208
        dq      1
        dq      fx_badsign_map
        dq      fx_badsign_end - fx_badsign_map
        dd      40
        dd      1
        dq      1
        dq      0x00500000
        dq      0x00501000
        dq      0x00500000
        dq      0x00501000
        dq      4 dup 0
        dq      0
        dq      0, 0
        dq      8 dup 0

; fixture 9: a stride below the 40 byte minimum
align 8
fx_badstride_map:
        fx_desc 7, 0x00100000, 1, 8
fx_badstride_end:
fx_badstride_boot:
        fx_bootinfo fx_badstride_map, fx_badstride_end - fx_badstride_map, 36, 1

; fixture 10: count times stride does not match the buffer size
align 8
fx_count_map:
        fx_desc 7, 0x00100000, 1, 8
        fx_desc 7, 0x00101000, 1, 8
fx_count_end:
fx_count_boot:
        fx_bootinfo fx_count_map, fx_count_end - fx_count_map, 40, 3

; fixture 11: a map larger than the 256 KiB contract limit
align 8
fx_bigmap_map:
        fx_desc 7, 0x00100000, 1, 8
fx_bigmap_end:
fx_bigmap_boot:
        fx_bootinfo fx_bigmap_map, 0x40001, 40, 1

; fixture 12: a descriptor that is not page aligned
align 8
fx_unalign_map:
        fx_desc 7, 0x00100800, 1, 8
fx_unalign_end:
fx_unalign_boot:
        fx_bootinfo fx_unalign_map, fx_unalign_end - fx_unalign_map, 40, 1

; fixture 13: a descriptor that covers no page at all
align 8
fx_zeropage_map:
        fx_desc 7, 0x00100000, 0, 8
fx_zeropage_end:
fx_zeropage_boot:
        fx_bootinfo fx_zeropage_map, fx_zeropage_end - fx_zeropage_map, 40, 1

; fixture 14: the stack is not inside the kernel mapping
align 8
fx_badstack_map:
        fx_desc 7, 0x00100000, 1, 8
fx_badstack_end:
fx_badstack_boot:
        dq      'K64BOOTI'
        dd      1
        dd      208
        dq      1
        dq      fx_badstack_map
        dq      fx_badstack_end - fx_badstack_map
        dd      40
        dd      1
        dq      1
        dq      0x00500000
        dq      0x00501000
        dq      0x00600000                  ; stack outside the kernel range
        dq      0x00601000
        dq      4 dup 0
        dq      0
        dq      0, 0
        dq      8 dup 0

; fixture 15: a descriptor version other than 1 must be refused
align 8
fx_baddescv_boot:
        fx_bootinfo_ks fx_stride40_map, fx_stride40_end - fx_stride40_map, 40, 4, 0x00500000, 0x00501000, 0x00500000, 0x00501000, 0

; fixture 16: the stack end does not coincide with the kernel end
align 8
fx_stkend_boot:
        fx_bootinfo_ks fx_stride40_map, fx_stride40_end - fx_stride40_map, 40, 4, 0x00500000, 0x00502000, 0x00500000, 0x00501000, 1

; fixture 17: a two page image, the exact stack top is base + 2 pages
align 8
fx_stktop2_boot:
        fx_bootinfo_ks fx_stride40_map, fx_stride40_end - fx_stride40_map, 40, 4, 0x00500000, 0x00502000, 0x00501000, 0x00502000, 1

; fixture 18: kernel plus stack larger than the 32 MiB contract limit
align 8
fx_bigimg_boot:
        fx_bootinfo_ks fx_stride40_map, fx_stride40_end - fx_stride40_map, 40, 4, 0x00500000, 0x02501000, 0x00500000, 0x02501000, 1

; fixture 19: a legal kernel image above 4 GiB, outside the managed pool
align 8
fx_kern4g_boot:
        fx_bootinfo_ks fx_stride40_map, fx_stride40_end - fx_stride40_map, 40, 4, 0x100000000, 0x100001000, 0x100000000, 0x100001000, 1

; ------------------------------------------------------------------
; fixtures 20..29: BootInfo feature flags and the GOP handover block
;
; The flags follow a mask rule: unknown bits are refused, MEMORY_MAP is
; mandatory, FRAMEBUFFER is optional. With the flag clear the six GOP
; fields must stay zero; with it set the geometry must be consistent:
; pitch >= width, size >= pitch * height * 4 (the 32bpp budget), and
; the pixel format stays below BltOnly. The framebuffer address may be
; absent from the memory map entirely, no range check applies to it.
; ------------------------------------------------------------------

macro fx_bootinfo_f map, size, stride, count, flags, fbaddr, fbsize, width, height, format, pitch
        dq      'K64BOOTI'
        dd      1
        dd      208
        dq      flags
        dq      map
        dq      size
        dd      stride
        dd      1
        dq      count
        dq      0x00500000                  ; kernel start
        dq      0x00501000                  ; kernel end
        dq      0x00500000                  ; stack start
        dq      0x00501000                  ; stack end
        dq      fbaddr
        dq      fbsize
        dd      width
        dd      height
        dd      format
        dd      pitch
        dq      0                           ; no RSDP
        dq      0, 0                        ; no initramfs
        dq      8 dup 0                     ; reserved
end macro

; fixture 20: a valid GOP handover, the framebuffer is absent from the map
align 8
fx_fbok_boot:
        fx_bootinfo_f fx_stride40_map, fx_stride40_end - fx_stride40_map, 40, 4, 3, 0x80000000, 0x300000, 1024, 768, 0, 1024

; fixture 21: a padded pitch above the width, BGRX format
align 8
fx_fbpitch_boot:
        fx_bootinfo_f fx_stride40_map, fx_stride40_end - fx_stride40_map, 40, 4, 3, 0x80000000, 0x360000, 1024, 768, 1, 1152

; fixture 22: the BitMask format is accepted as well
align 8
fx_fbfmt2_boot:
        fx_bootinfo_f fx_stride40_map, fx_stride40_end - fx_stride40_map, 40, 4, 3, 0x80000000, 0x300000, 1024, 768, 2, 1024

; fixture 23: an unknown flag bit must be refused
align 8
fx_fbunk_boot:
        fx_bootinfo_f fx_stride40_map, fx_stride40_end - fx_stride40_map, 40, 4, 0x11, 0, 0, 0, 0, 0, 0

; fixture 24: GOP fields without the framebuffer flag must be refused
align 8
fx_fbnofl_boot:
        fx_bootinfo_f fx_stride40_map, fx_stride40_end - fx_stride40_map, 40, 4, 1, 0x80000000, 0x300000, 1024, 768, 0, 1024

; fixture 25: the framebuffer flag with all zero GOP fields is refused
align 8
fx_fbzero_boot:
        fx_bootinfo_f fx_stride40_map, fx_stride40_end - fx_stride40_map, 40, 4, 3, 0, 0, 0, 0, 0, 0

; fixture 26: a pitch below the width is refused
align 8
fx_fbpitlo_boot:
        fx_bootinfo_f fx_stride40_map, fx_stride40_end - fx_stride40_map, 40, 4, 3, 0x80000000, 0x300000, 1024, 768, 0, 1023

; fixture 27: a size below the 32bpp budget is refused
align 8
fx_fbsize_boot:
        fx_bootinfo_f fx_stride40_map, fx_stride40_end - fx_stride40_map, 40, 4, 3, 0x80000000, 0x2FFFFF, 1024, 768, 0, 1024

; fixture 28: BltOnly has no linear framebuffer and is refused
align 8
fx_fbfmt3_boot:
        fx_bootinfo_f fx_stride40_map, fx_stride40_end - fx_stride40_map, 40, 4, 3, 0x80000000, 0x300000, 1024, 768, 3, 1024

; fixture 29: the framebuffer flag without the memory map flag is refused
align 8
fx_fbmap0_boot:
        fx_bootinfo_f fx_stride40_map, fx_stride40_end - fx_stride40_map, 40, 4, 2, 0x80000000, 0x300000, 1024, 768, 0, 1024

; ------------------------------------------------------------------
; INIT_PARAMS blocks, one per fixture
;
; The stack top is a kernel virtual address one page above the kernel
; base, which lands on the single stack page the fixtures describe.
; ------------------------------------------------------------------

MM_PB_STACK = 0xFFFFFFFF80001000

macro fx_params_ext bootva, bootphys, cr3, stack, version, size, flags
        align 8
        dd      version
        dd      size
        dq      bootva
        dq      bootphys
        dq      cr3
        dq      stack
        dq      flags
        dq      0
        dq      0
end macro

macro fx_params boot, cr3, stack
        fx_params_ext boot, boot, cr3, stack, 1, 64, 0
end macro

mm_ip_stride40:
        fx_params fx_stride40_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_stride48:
        fx_params fx_stride48_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_stride64:
        fx_params fx_stride64_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_unorder:
        fx_params fx_unorder_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_overlap:
        fx_params fx_overlap_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_overflow:
        fx_params fx_overflow_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_clip:
        fx_params fx_clip_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_types:
        fx_params fx_types_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_badsign:
        fx_params fx_badsign_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_badstride:
        fx_params fx_badstride_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_count:
        fx_params fx_count_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_bigmap:
        fx_params fx_bigmap_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_unalign:
        fx_params fx_unalign_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_zeropage:
        fx_params fx_zeropage_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_badstack:
        fx_params fx_badstack_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_high:
        fx_params fx_stride40_boot, mm_fx2_base, MM_PB_STACK
; the same low memory map described by a BootInfo above 1 MiB, so the
; BootInfo and the map page pins are visible on their own
mm_ip_highboot:
        fx_params mm_fx_high_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_tblps:
        fx_params fx_stride40_boot, mm_fx_ps_pml4, MM_PB_STACK
mm_ip_tbl4g:
        fx_params fx_stride40_boot, mm_fx_4g_pml4, MM_PB_STACK
mm_ip_tbl64:
        fx_params fx_stride40_boot, mm_fx_many_base, MM_PB_STACK
mm_ip_top:
        fx_params fx_stride40_boot, mm_fx_pml4, 0xFFFFFFFF80002000
mm_ip_baddescv:
        fx_params fx_baddescv_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_stkend:
        fx_params fx_stkend_boot, mm_fx_pml4, MM_PB_STACK
; aligned, but one page below the exact top of the two page image
mm_ip_stktop2:
        fx_params fx_stktop2_boot, mm_fx_pml4, 0xFFFFFFFF80001000
mm_ip_bigimg:
        fx_params fx_bigimg_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_kern4g:
        fx_params fx_kern4g_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_fbok:
        fx_params fx_fbok_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_fbpitch:
        fx_params fx_fbpitch_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_fbfmt2:
        fx_params fx_fbfmt2_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_fbunk:
        fx_params fx_fbunk_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_fbnofl:
        fx_params fx_fbnofl_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_fbzero:
        fx_params fx_fbzero_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_fbpitlo:
        fx_params fx_fbpitlo_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_fbsize:
        fx_params fx_fbsize_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_fbfmt3:
        fx_params fx_fbfmt3_boot, mm_fx_pml4, MM_PB_STACK
mm_ip_fbmap0:
        fx_params fx_fbmap0_boot, mm_fx_pml4, MM_PB_STACK

; malformed INIT_PARAMS blocks, every one of them must be refused
mm_ip_badver:
        fx_params_ext fx_stride40_boot, fx_stride40_boot, mm_fx_pml4, MM_PB_STACK, 2, 64, 0
mm_ip_badsize:
        fx_params_ext fx_stride40_boot, fx_stride40_boot, mm_fx_pml4, MM_PB_STACK, 1, 63, 0
mm_ip_badflags:
        fx_params_ext fx_stride40_boot, fx_stride40_boot, mm_fx_pml4, MM_PB_STACK, 1, 64, 1
mm_ip_cr3align:
        fx_params_ext fx_stride40_boot, fx_stride40_boot, mm_fx_pml4 + 4, MM_PB_STACK, 1, 64, 0
mm_ip_cr3high:
        fx_params_ext fx_stride40_boot, fx_stride40_boot, 0x100000000, MM_PB_STACK, 1, 64, 0
mm_ip_vamism:
        fx_params_ext fx_stride40_boot + 8, fx_stride40_boot, mm_fx_pml4, MM_PB_STACK, 1, 64, 0
mm_ip_bootalign:
        fx_params_ext fx_stride40_boot + 4, fx_stride40_boot + 4, mm_fx_pml4, MM_PB_STACK, 1, 64, 0
mm_ip_bootnull:
        fx_params_ext 0, 0, mm_fx_pml4, MM_PB_STACK, 1, 64, 0
mm_ip_stacklow:
        fx_params_ext fx_stride40_boot, fx_stride40_boot, mm_fx_pml4, 0x1000, 1, 64, 0
mm_ip_stackmis:
        fx_params_ext fx_stride40_boot, fx_stride40_boot, mm_fx_pml4, 0xFFFFFFFF80001008, 1, 64, 0

; ------------------------------------------------------------------
; high address space, MM_PB_HI_ARENA
;
; The same walk as the low one plus the public window at PML4 index
; 0x180: a 4 KiB table with permission and physical width combinations,
; a 2 MiB leaf in the PD, a 1 GiB leaf in the PDPT and a PDPT entry that
; points back at its own PML4, so the pinner has to notice the cycle.
;
; The window chain (PML4, PDPT, PD) carries U/S, the leaves do not all
; carry it, so the query results prove that U/S is ANDed along the path
; instead of being taken from the leaf alone.
; Every table page here is above 1 MiB, so it can only be pinned by the
; CR3 pinner and never by the firmware pin.
; ------------------------------------------------------------------

pad_to MM_PB_HI_ARENA

mm_fx2_base:
mm_fx2_pml4:
        dq      mm_fx2_pdpt_low + 3         ; 0: identity chain
        dq      383 dup 0
        dq      mm_fx2_pdpt_win + 7         ; 0x180: the public window, user
        dq      127 dup 0

mm_fx2_pdpt_low:
        dq      mm_fx2_pd_low + 3
        dq      511 dup 0

mm_fx2_pd_low:
        dq      mm_fx2_pt_low + 3
        dq      511 dup 0

mm_fx2_pt_low:
        dq      512 dup 0                   ; tables present, every leaf absent

mm_fx2_pdpt_win:
        dq      mm_fx2_pd_win + 7           ; 0: window PDPT index 0, user
        dq      0x40000000 + 0x83           ; 1: 1 GiB leaf, PS, writable
        dq      mm_fx2_base + 3             ; 2: cycle back to the PML4
        dq      0x80000000 + 0x1000 + 0x83  ; 3: 1 GiB leaf with the PAT bit
        dq      508 dup 0

mm_fx2_pd_win:
        dq      mm_fx2_pt_win + 7           ; 0, user
        dq      0x200000 + 0x8000000000000083 ; 1: 2 MiB leaf, PS, NX
        dq      0x400000 + 0x1000 + 0x83    ; 2: 2 MiB leaf with the PAT bit
        dq      509 dup 0

mm_fx2_pt_win:
        dq      0x00500000 + 0x000007       ; 0: present, RW, US
        dq      0x00600000 + 0x8000000000000003 ; 1: present, RW, NX
        dq      0x000FFFFFFFFFF003           ; 2: 52 bit physical address
        dq      0                           ; 3: absent
        dq      0x00700000 + 0x8000000000000001 ; 4: read only, NX
        dq      507 dup 0

; one PML4 whose index 0 entry uses the reserved PS bit
pad_to MM_PB_PS_ARENA
mm_fx_ps_pml4:
        dq      MM_PB_PS_ARENA + 0x1000 + 0x83
        dq      511 dup 0

; one PML4 whose index 0 entry points at a table page above 4 GiB
pad_to MM_PB_4G_ARENA
mm_fx_4g_pml4:
        dq      0x100000000 + 3
        dq      511 dup 0

; ------------------------------------------------------------------
; kernel and stack mapping, MM_PB_KERN_ARENA
;
; KERNEL_BASE + n * 4 KiB maps 0x500000 + n * 4 KiB, which is the layout
; vmm_boot_protect assumes: the kernel image starts at the kernel base and
; the stack sits inside it. The three leaves start deliberately wrong, so
; one pass has to fix all of them:
;
;   page 0  read only, executable              -> writable, executable
;   page 1  writable, non executable           -> writable, executable
;   page 2  read only, executable, the stack   -> writable, non executable
; ------------------------------------------------------------------

pad_to MM_PB_KERN_ARENA
mm_kern_pml4:
        dq      511 dup 0
        dq      mm_kern_pdpt + 3            ; 511: KERNEL_BASE
mm_kern_pdpt:
        dq      510 dup 0
        dq      mm_kern_pd + 3              ; 510
        dq      0
mm_kern_pd:
        dq      mm_kern_pt + 3              ; 0
        dq      511 dup 0
mm_kern_pt:
        dq      0x00500000 + 0x001          ; read only, executable
        dq      0x00501000 + 0x8000000000000003 ; writable, non executable
        dq      0x00502000 + 0x001          ; read only, executable, stack
        dq      509 dup 0

; ------------------------------------------------------------------
; high fixture: BootInfo and memory map above 1 MiB, so the pinning of
; the BootInfo page and of the map pages is visible on its own
; ------------------------------------------------------------------

pad_to MM_PB_HI_BOOT
mm_fx_high_boot:
        fx_bootinfo mm_fx_high_map, mm_fx_high_end - mm_fx_high_map, 40, 4

pad_to MM_PB_HI_MAP
mm_fx_high_map:
        fx_desc 7, 0x10000000, 1, 8
        fx_desc 7, 0x10001000, 2, 8
        fx_desc 3, 0x10003000, 1, 8
        fx_desc 7, 0x10004000, 1, 0x8000000000000008
mm_fx_high_end:

; ------------------------------------------------------------------
; an address space with more table pages than the pinner may pin:
; 1 PML4 + 33 PDPTs + 33 PDs, the 65th page must be refused
; ------------------------------------------------------------------

pad_to MM_PB_MANY_ARENA
mm_fx_many_base:
        repeat 33, i
                dq      mm_fx_many_base + (i + 1) * MM_PAGE_SIZE + 3
        end repeat
        dq      479 dup 0
        repeat 33, i
                dq      mm_fx_many_base + (34 + i) * MM_PAGE_SIZE + 3
                dq      511 dup 0
        end repeat
        repeat 33
                dq      512 dup 0
        end repeat

; ------------------------------------------------------------------
; fixture table, read by check.py
;
; 16 qwords per entry:
;   +0   tag, eight characters
;   +8   INIT_PARAMS address
;   +16  expected status of pmm_init
;   +24  expected eligible page count
;   +32  expected pinned page count
;   +40  expected first free PFN, or -1 when there is none
;   +48  expected number of distinct page table pages the pinner visited
;   +56  probe A PFN, +64 expected eligible bit, +72 expected pinned bit
;   +80  probe B PFN, +88 expected eligible bit, +96 expected pinned bit
;   +104 probe C PFN, +112 expected eligible bit, +120 expected pinned bit
; ------------------------------------------------------------------

mm_fx_count:
        dq      46
mm_fx_table:
        ; baseline and stride variants
        dq      'STRIDE40', mm_ip_stride40, 0, 3, 0xFFFFD, 0x100, 4
        dq      0x100, 1, 0, 0x200, 1, 0, 0x10, 0, 1
        dq      'STRIDE48', mm_ip_stride48, 0, 3, 0xFFFFD, 0x100, 4
        dq      0x201, 1, 0, 0x500, 0, 1, 0x300, 0, 1
        dq      'STRIDE64', mm_ip_stride64, 0, 3, 0xFFFFD, 0x100, 4
        dq      0x201, 1, 0, 0x500, 0, 1, 0x300, 0, 1
        dq      'UNORDER', mm_ip_unorder, 0, 3, 0xFFFFD, 0x100, 4
        dq      0x200, 1, 0, 0x100, 1, 0, 0x10, 0, 1
        ; ranges the allocator must keep out of
        dq      'OVERLAP', mm_ip_overlap, -1, 0, 0x100000, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'OVRFLOW', mm_ip_overflow, -1, 0, 0x100000, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'CLIP4G', mm_ip_clip, 0, 1, 0xFFFFF, 0xFFFFF, 4
        dq      0x100, 0, 1, 0xFFFF0, 0, 1, 0xFFFFF, 1, 0
        dq      'TYPES', mm_ip_types, 0, 1, 0xFFFFF, 0x107, 4
        dq      0x107, 1, 0, 0x106, 0, 1, 0x110, 0, 1
        ; malformed BootInfo records
        dq      'BADSIGN', mm_ip_badsign, -1, 0, 0x100000, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'BADSTRD', mm_ip_badstride, -1, 0, 0x100000, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'COUNTMM', mm_ip_count, -1, 0, 0x100000, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'BIGMAP', mm_ip_bigmap, -1, 0, 0x100000, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'UNALIGN', mm_ip_unalign, -1, 0, 0x100000, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'ZERPAGE', mm_ip_zeropage, -1, 0, 0x100000, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'BADSTACK', mm_ip_badstack, -1, 0, 0x100000, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        ; a fully valid fixture placed above 1 MiB
        dq      'HIGH', mm_ip_high, 0, 3, 0xFFFFD, 0x100, 7
        dq      0x100, 1, 0, 0x180, 0, 1, 0x140, 0, 1
        ; the same map with a BootInfo and a map page above 1 MiB
        dq      'HIGHBI', mm_ip_highboot, 0, 3, 0xFFFFD, 0x10000, 4
        dq      0x10000, 1, 0, 0x180, 0, 1, 0x181, 0, 1
        ; page table shapes the pinner must refuse
        dq      'TBLPS', mm_ip_tblps, -1, 3, 0xFFFFD, 0x100, 1
        dq      0x150, 0, 1, 0, 0, 0, 0, 0, 0
        dq      'TBL4G', mm_ip_tbl4g, -6, 3, 0xFFFFD, 0x100, 1
        dq      0x160, 0, 1, 0, 0, 0, 0, 0, 0
        dq      'TBL64', mm_ip_tbl64, -6, 3, 0xFFFFD, 0x100, 64
        dq      0x190, 0, 1, 0, 0, 0, 0, 0, 0
        ; malformed INIT_PARAMS blocks
        dq      'BADVER', mm_ip_badver, -1, 0, 0, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'BADSIZE', mm_ip_badsize, -1, 0, 0, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'BADFLAGS', mm_ip_badflags, -1, 0, 0, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'CR3ALIGN', mm_ip_cr3align, -1, 0, 0, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'CR3HIGH', mm_ip_cr3high, -1, 0, 0, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'VAMISM', mm_ip_vamism, -1, 0, 0, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'BOOTALN', mm_ip_bootalign, -1, 0, 0, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'BOOTNUL', mm_ip_bootnull, -1, 0, 0, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'STKLOW', mm_ip_stacklow, -1, 0, 0, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'STKMIS', mm_ip_stackmis, -1, 0, 0, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'STKTOP', mm_ip_top, -1, 0, 0x100000, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        ; descriptor version, stack rules and the image size limit
        dq      'BADDESCV', mm_ip_baddescv, -1, 0, 0x100000, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'STKEND', mm_ip_stkend, -1, 0, 0x100000, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'STKTOP2', mm_ip_stktop2, -1, 0, 0x100000, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'BIGIMG', mm_ip_bigimg, -1, 0, 0x100000, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'KERN4G', mm_ip_kern4g, 0, 3, 0xFFFFD, 0x100, 4
        dq      0x100, 1, 0, 0x500, 0, 1, 0x10, 0, 1
        ; BootInfo feature flags and the GOP handover block
        dq      'FBOK', mm_ip_fbok, 0, 3, 0xFFFFD, 0x100, 4
        dq      0x100, 1, 0, 0x200, 1, 0, 0x10, 0, 1
        dq      'FBPITCH', mm_ip_fbpitch, 0, 3, 0xFFFFD, 0x100, 4
        dq      0x100, 1, 0, 0x200, 1, 0, 0x10, 0, 1
        dq      'FBFMT2', mm_ip_fbfmt2, 0, 3, 0xFFFFD, 0x100, 4
        dq      0x100, 1, 0, 0x200, 1, 0, 0x10, 0, 1
        dq      'FBUNK', mm_ip_fbunk, -1, 0, 0x100000, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'FBNOFL', mm_ip_fbnofl, -1, 0, 0x100000, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'FBZERO', mm_ip_fbzero, -1, 0, 0x100000, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'FBPITLO', mm_ip_fbpitlo, -1, 0, 0x100000, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'FBSIZE', mm_ip_fbsize, -1, 0, 0x100000, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'FBFMT3', mm_ip_fbfmt3, -1, 0, 0x100000, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0
        dq      'FBMAP0', mm_ip_fbmap0, -1, 0, 0x100000, -1, 0
        dq      0, 0, 0, 0, 0, 0, 0, 0, 0

; ------------------------------------------------------------------
; query table, read by check.py
;
; 9 qwords per entry:
;   +0  tag
;   +8  CR3 physical address
;   +16 virtual address
;   +24 expected status
;   +32 expected physical address
;   +40 expected leaf entry address
;   +48 expected page size
;   +56 expected effective flags, PTE bit values
;   +64 expected raw leaf entry
;
; MM_WINDOW_BASE is PML4 index 0x180, so adding 1 shl 39 selects index
; 0x181 and subtracting one page selects index 0x17F; the limit itself is
; index 0x184.
; ------------------------------------------------------------------

mm_qry_count:
        dq      20
mm_qry_table:
        dq      'QR4K0', mm_fx2_base, MM_WINDOW_BASE, 0
        dq      0x00500000, mm_fx2_pt_win, 0x1000, 1 or 2 or 4, 0x00500000 + 7
        dq      'QR4K1', mm_fx2_base, MM_WINDOW_BASE + 0x1000, 0
        dq      0x00600000, mm_fx2_pt_win + 8, 0x1000, 1 or 2 or 0x8000000000000000, 0x00600000 + 0x8000000000000003
        dq      'QR4K2', mm_fx2_base, MM_WINDOW_BASE + 0x2000, 0
        dq      0x000FFFFFFFFFF000, mm_fx2_pt_win + 16, 0x1000, 1 or 2, 0x000FFFFFFFFFF003
        dq      'QR4K2O', mm_fx2_base, MM_WINDOW_BASE + 0x2ABC, 0
        dq      0x000FFFFFFFFFFABC, mm_fx2_pt_win + 16, 0x1000, 1 or 2, 0x000FFFFFFFFFF003
        dq      'QRABS', mm_fx2_base, MM_WINDOW_BASE + 0x3000, -3
        dq      0, 0, 0, 0, 0
        dq      'QRRO', mm_fx2_base, MM_WINDOW_BASE + 0x4000, 0
        dq      0x00700000, mm_fx2_pt_win + 32, 0x1000, 1 or 0x8000000000000000, 0x00700000 + 0x8000000000000001
        dq      'QR2M', mm_fx2_base, MM_WINDOW_BASE + 0x200000, 0
        dq      0x200000, mm_fx2_pd_win + 8, 0x200000, 1 or 2 or 0x8000000000000000, 0x200000 + 0x8000000000000083
        dq      'QR2MO', mm_fx2_base, MM_WINDOW_BASE + 0x201234, 0
        dq      0x201234, mm_fx2_pd_win + 8, 0x200000, 1 or 2 or 0x8000000000000000, 0x200000 + 0x8000000000000083
        dq      'QR1G', mm_fx2_base, MM_WINDOW_BASE + (1 shl 30), 0
        dq      0x40000000, mm_fx2_pdpt_win + 8, 0x40000000, 1 or 2, 0x40000000 + 0x83
        dq      'QR1GO', mm_fx2_base, MM_WINDOW_BASE + (1 shl 30) + 0x5678, 0
        dq      0x40005678, mm_fx2_pdpt_win + 8, 0x40000000, 1 or 2, 0x40000000 + 0x83
        ; large leaves with the PAT bit set: bit 12 must not leak into the PA
        dq      'QRPAT2M', mm_fx2_base, MM_WINDOW_BASE + 0x400000 + 0x1234, 0
        dq      0x401234, mm_fx2_pd_win + 16, 0x200000, 1 or 2, 0x400000 + 0x1000 + 0x83
        dq      'QRPAT1G', mm_fx2_base, MM_WINDOW_BASE + (3 shl 30) + 0x5678, 0
        dq      0x80005678, mm_fx2_pdpt_win + 24, 0x40000000, 1 or 2, 0x80000000 + 0x1000 + 0x83
        dq      'QRNX1', mm_fx2_base, MM_WINDOW_BASE + (1 shl 39), -3
        dq      0, 0, 0, 0, 0
        dq      'QRLIM', mm_fx2_base, MM_WINDOW_LIMIT, -3
        dq      0, 0, 0, 0, 0
        dq      'QRBAS', mm_fx2_base, MM_WINDOW_BASE - 0x1000, -3
        dq      0, 0, 0, 0, 0
        dq      'QRIDL', mm_fx2_base, 0x00100000, -3
        dq      0, 0, 0, 0, 0
        dq      'QRLOWAS', mm_fx_pml4, MM_WINDOW_BASE, -3
        dq      0, 0, 0, 0, 0
        dq      'QR4GPT', mm_fx_4g_pml4, 0, -6
        dq      0, 0, 0, 0, 0
        dq      'QRPS', mm_fx_ps_pml4, 0, -1
        dq      0, 0, 0, 0, 0
        dq      'QRCANON', mm_fx2_base, 0x0000800000000000, -1
        dq      0, 0, 0, 0, 0

; ------------------------------------------------------------------
; map table, read by check.py
;
; 9 qwords per entry:
;   +0  tag
;   +8  virtual address
;   +16 physical address
;   +24 mapping flags
;   +32 expected status
;   +40 test hook: fail the N-th page table allocation, 0 to disable
;   +48 expected number of newly built page tables
;   +56 expected raw leaf entry
;   +64 expected leaf present, 1 or 0
;
; Every case starts from a fresh copy of the image, so a mapping made by
; one case never leaks into the next one. An empty flag set is a read only
; executable page and must be accepted; RW together with execute is the one
; combination the contract refuses.
; ------------------------------------------------------------------

MM_MAP_RWX = 0x2
MM_MAP_NXX = 0x8000000000000000

mm_map_count:
        dq      26
mm_map_table:
        dq      'MPEXIST', MM_WINDOW_BASE + 0x1000, 0x00900000, MM_MAP_RWX or MM_MAP_NXX, -4, 0, 0, 0, 0
        dq      'MP0TAB', MM_WINDOW_BASE + 0x3000, 0x00900000, MM_MAP_RWX or MM_MAP_NXX, 0, 0, 0
        dq      0x00900000 + 0x8000000000000003, 1
        dq      'MP1TAB', MM_WINDOW_BASE + 0x600000, 0x00900000, MM_MAP_RWX or MM_MAP_NXX, 0, 0, 1
        dq      0x00900000 + 0x8000000000000003, 1
        dq      'MP2TAB', MM_WINDOW_BASE + (5 shl 30), 0x00900000, MM_MAP_NXX, 0, 0, 2
        dq      0x00900000 + 0x8000000000000001, 1
        dq      'MP3TAB', MM_WINDOW_BASE + (2 shl 39), 0x00900000, MM_MAP_RWX or MM_MAP_NXX, 0, 0, 3
        dq      0x00900000 + 0x8000000000000003, 1
        dq      'MPROLL1', MM_WINDOW_BASE + (2 shl 39), 0x00900000, MM_MAP_RWX or MM_MAP_NXX, -2, 1, 0, 0, 0
        dq      'MPROLL2', MM_WINDOW_BASE + (2 shl 39), 0x00900000, MM_MAP_RWX or MM_MAP_NXX, -2, 2, 0, 0, 0
        dq      'MPROLL3', MM_WINDOW_BASE + (2 shl 39), 0x00900000, MM_MAP_RWX or MM_MAP_NXX, -2, 3, 0, 0, 0
        dq      'MPBIG2M', MM_WINDOW_BASE + 0x200000, 0x00900000, MM_MAP_NXX, -6, 0, 0, 0, 0
        dq      'MPBIG1G', MM_WINDOW_BASE + (1 shl 30), 0x00900000, MM_MAP_NXX, -6, 0, 0, 0, 0
        dq      'MPWNONE', MM_WINDOW_BASE - 0x1000, 0x00900000, MM_MAP_NXX, -5, 0, 0, 0, 0
        dq      'MPWLIM', MM_WINDOW_LIMIT, 0x00900000, MM_MAP_NXX, -5, 0, 0, 0, 0
        dq      'MPWLAST', MM_WINDOW_LIMIT - 0x1000, 0x00900000, MM_MAP_NXX, 0, 0, 3
        dq      0x00900000 + 0x8000000000000001, 1
        dq      'MPROEX', MM_WINDOW_BASE + 0x5000, 0x00900000, 0, 0, 0, 0
        dq      0x00900000 + 1, 1
        dq      'MPFLRW', MM_WINDOW_BASE + 0x5000, 0x00900000, MM_MAP_RWX, -1, 0, 0, 0, 0
        dq      'MPFLUS', MM_WINDOW_BASE + 0x5000, 0x00900000, MM_MAP_NXX or 4, -1, 0, 0, 0, 0
        dq      'MPFLG', MM_WINDOW_BASE + 0x5000, 0x00900000, MM_MAP_NXX or 0x100, -1, 0, 0, 0, 0
        dq      'MPFLPAT', MM_WINDOW_BASE + 0x5000, 0x00900000, MM_MAP_NXX or 0x1000, -1, 0, 0, 0, 0
        dq      'MPFLPWT', MM_WINDOW_BASE + 0x5000, 0x00900000, MM_MAP_NXX or 0x8, -1, 0, 0, 0, 0
        dq      'MPFLPCD', MM_WINDOW_BASE + 0x5000, 0x00900000, MM_MAP_NXX or 0x10, -1, 0, 0, 0, 0
        dq      'MPFLPS', MM_WINDOW_BASE + 0x5000, 0x00900000, MM_MAP_NXX or 0x80, -1, 0, 0, 0, 0
        dq      'MPPAAL', MM_WINDOW_BASE + 0x5000, 0x00900123, MM_MAP_NXX, -1, 0, 0, 0, 0
        dq      'MPPAOV', MM_WINDOW_BASE + 0x5000, 0x10000000000000, MM_MAP_NXX, -5, 0, 0, 0, 0
        dq      'MPVAAL', MM_WINDOW_BASE + 0x678, 0x00900000, MM_MAP_NXX, -1, 0, 0, 0, 0
        dq      'MPRWEX', MM_WINDOW_BASE + 0x5000, 0x00900000, MM_MAP_RWX or 1, -1, 0, 0, 0, 0
        ; a page inside the pool that was never handed out by pmm_alloc_page
        dq      'MPUNOWN', MM_WINDOW_BASE + 0x5000, 0x00902000, MM_MAP_NXX, -5, 0, 0, 0, 0

; ------------------------------------------------------------------
; unmap table, read by check.py
;
; 5 qwords per entry:
;   +0  tag
;   +8  virtual address
;   +16 expected status
;   +24 expected leaf state after the call, 1 when the entry is cleared
;   +32 expected physical address returned in RDX, 0 on failure
; ------------------------------------------------------------------

mm_unmap_count:
        dq      6
mm_unmap_table:
        dq      'UMNORM', MM_WINDOW_BASE + 0x1000, 0, 1, 0x00600000
        dq      'UMABS', MM_WINDOW_BASE + 0x3000, -3, 0, 0
        dq      'UM2M', MM_WINDOW_BASE + 0x200000, -6, 0, 0
        dq      'UM1G', MM_WINDOW_BASE + (1 shl 30), -6, 0, 0
        dq      'UMOUT', MM_WINDOW_BASE - 0x1000, -5, 0, 0
        dq      'UMAL', MM_WINDOW_BASE + 0x800, -1, 0, 0

; ------------------------------------------------------------------
; MMIO map table, read by check.py
;
; 8 qwords per entry:
;   +0  tag
;   +8  virtual address
;   +16 physical address
;   +24 expected status
;   +32 test hook: fail the N-th page table allocation, 0 to disable
;   +40 expected number of newly built page tables
;   +48 expected raw leaf entry
;   +56 expected leaf present, 1 or 0
;
; vmm_map_mmio takes no flags argument: every accepted leaf carries
; exactly PCD|RW|NX. The physical page must be non eligible (an eligible
; page belongs to pmm and has to go through vmm_map_page), the VA must
; clear the heap range and the core test PML4 slot, and an existing leaf
; reports EALREADY. Every case starts from a fresh copy of the image.
; ------------------------------------------------------------------

mm_mmio_count:
        dq      19
mm_mmio_table:
        ; accepted: the LAPIC window needs one fresh PT under the window
        dq      'MMIOOK', MM_MMIO_LAPIC_VA, 0xFEE00000, 0, 0, 1
        dq      0xFEE00000 + 0x8000000000000013, 1
        ; the heap limit itself is already above the heap range
        dq      'MMIOHBAS', MM_HEAP_LIMIT, 0xFEE00000, 0, 0, 1
        dq      0xFEE00000 + 0x8000000000000013, 1
        ; PML4 slot 385 is inside the window and builds three tables
        dq      'MMIO3TB', MM_WINDOW_BASE + (1 shl 39), 0xFEE00000, 0, 0, 3
        dq      0xFEE00000 + 0x8000000000000013, 1
        ; a framebuffer like address no memory map descriptor covers: the
        ; eligible bit is zero, so the reverse ownership check passes it
        dq      'MMIOFB', MM_HEAP_LIMIT + 0x1000, 0x80000000, 0, 0, 1
        dq      0x80000000 + 0x8000000000000013, 1
        ; an eligible page is conventional memory and must be refused
        dq      'MMIOELIG', MM_MMIO_LAPIC_VA, 0x00260000, -5, 0, 0, 0, 0
        dq      'MMIOWLOW', MM_WINDOW_BASE - 0x1000, 0xFEE00000, -5, 0, 0, 0, 0
        dq      'MMIOWLIM', MM_WINDOW_LIMIT, 0xFEE00000, -5, 0, 0, 0, 0
        dq      'MMIOHEAP', MM_WINDOW_BASE, 0xFEE00000, -5, 0, 0, 0, 0
        dq      'MMIOHEND', MM_HEAP_LIMIT - 0x1000, 0xFEE00000, -5, 0, 0, 0, 0
        ; the core test slot 386 is refused even though map_page takes it
        dq      'MMIOS386', MM_WINDOW_BASE + (2 shl 39), 0xFEE00000, -5, 0, 0, 0, 0
        dq      'MMIOVAAL', MM_MMIO_LAPIC_VA + 0x800, 0xFEE00000, -1, 0, 0, 0, 0
        dq      'MMIOPAAL', MM_MMIO_LAPIC_VA, 0xFEE00123, -1, 0, 0, 0, 0
        ; mapping the LAPIC page twice reports EALREADY (check.py maps it
        ; once first: the heap refusal runs before the leaf check)
        dq      'MMIODUP', MM_MMIO_LAPIC_VA, 0xFEE00000, -8, 0, 0, 0, 0
        ; a 2 MiB leaf in the PD cannot be split (check.py plants the PS
        ; entry above the heap limit before this case runs)
        dq      'MMIOBIG', MM_HEAP_LIMIT + 0x200000, 0xFEE00000, -6, 0, 0, 0, 0
        ; hooked builds roll every fresh table back
        dq      'MMIORLL1', MM_WINDOW_BASE + (1 shl 39), 0xFEE00000, -2, 1, 0, 0, 0
        dq      'MMIORLL2', MM_WINDOW_BASE + (1 shl 39), 0xFEE00000, -2, 2, 0, 0, 0
        dq      'MMIORLL3', MM_WINDOW_BASE + (1 shl 39), 0xFEE00000, -2, 3, 0, 0, 0
        dq      'MMIORL1T', MM_MMIO_LAPIC_VA, 0xFEE00000, -2, 1, 0, 0, 0
        ; a physical address beyond the CPU width, non eligible by range
        dq      'MMIOOVFL', MM_MMIO_LAPIC_VA, 0x10000000000000, -10, 0, 0, 0, 0

mm_probe_end:

include '../../kernel/mm/mm.asm'
