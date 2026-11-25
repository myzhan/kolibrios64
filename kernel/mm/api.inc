;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Stage-2 memory management: the private copy of the shared handover
;; contract (K64-FOUNDATION-v1).
;;
;; The contract is not kept in a shared file, so every fixed value is
;; duplicated here with the MM_ prefix. The assembly time checks at the
;; bottom of this file keep the local copy self consistent; the layout
;; tests compare the public offsets against hand written expectations.
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
; kernel virtual base, the shared const.inc value
;
; It is a plain constant and not a guard around the shared symbol: this
; build of fasm2 never converges on "if ~ defined X / X = ...", it keeps
; re-running passes because the condition flips between them.

MM_KERNEL_BASE = 0xFFFFFFFF80000000

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; page geometry and the physical window this stage manages

MM_PAGE_SHIFT   = 12
MM_PAGE_SIZE    = 0x1000
MM_PHYS_LIMIT   = 0x100000000     ; exclusive, low 4 GiB only
MM_PFN_COUNT    = MM_PHYS_LIMIT / MM_PAGE_SIZE
MM_BITMAP_BYTES = MM_PFN_COUNT / 8
MM_PFN_NONE     = -1

; one bit per 4 KiB page, so a bitmap covering the low 4 GiB is 128 KiB
if MM_PFN_COUNT <> 0x100000
        display "unexpected PFN count", 13, 10
        err
end if
if MM_BITMAP_BYTES <> 0x20000
        display "unexpected bitmap size", 13, 10
        err
end if

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; public mapping window and the early bump heap inside it

MM_WINDOW_BASE  = 0xFFFFC00000000000
MM_WINDOW_LIMIT = 0xFFFFC20000000000
MM_WINDOW_SIZE  = MM_WINDOW_LIMIT - MM_WINDOW_BASE

MM_HEAP_BASE    = MM_WINDOW_BASE
MM_HEAP_LIMIT   = 0xFFFFC00001000000
MM_HEAP_MAX     = MM_HEAP_LIMIT - MM_HEAP_BASE
MM_HEAP_INITIAL = 0x10000         ; 64 KiB mapped by heap_init

if MM_HEAP_BASE <> MM_WINDOW_BASE
        display "heap must start at the mapping window base", 13, 10
        err
end if
if MM_HEAP_INITIAL mod MM_PAGE_SIZE
        display "initial heap size must be page aligned", 13, 10
        err
end if

; the whole window must live in canonical high half addresses with bit 47
; set, otherwise the NX bit and the PTE address bits would be ambiguous
if (MM_WINDOW_BASE shr 47) <> 0x1FFFF
        display "mapping window must be canonical", 13, 10
        err
end if
if (MM_WINDOW_LIMIT shr 47) <> 0x1FFFF
        display "mapping window must be canonical", 13, 10
        err
end if

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; frozen user VA domain, FS-v1 M3 adjudication of the task 120 plan B
; layout (user/chip8/api.inc): code 16 KiB at 0x100400000 (US, RO+X),
; data 2 MiB at 0x100600000 (US|RW|NX) with the 16 KiB user stack at the
; very top of the data region, stack top 0x100800000 exclusive.
;
; The whole domain lives above the low 4 GiB identity map: PML4 slot 0,
; PDPT index 4. The loader identity already owns the slot 0 PDPT (its
; entries 0..3 point at the four 2 MiB leaf PDs), so index 4 is an empty
; slot of that existing PDPT and no identity 2 MiB leaf ever needs
; splitting. The self consistent page table budget is exactly +3 pages
; for the full domain wiring: 1 PD at PDPT[4] + 1 PT for the code 2 MiB
; + 1 PT shared by data+stack. The original +4 draft double counted the
; identity PDPT; adjudicated here and frozen by the stage2-mm gate.
; The shared PML4 slot 0 entry gains US once on the first successful
; user map (anchor 0 of vmm_map_user_page): US is ANDed along the path
; and the identity PDPT/PD entries stay supervisor, so the low 4 GiB
; identity keeps its supervisor-only effective access.
;
; vmm_unmap_user_page (FS-v1 task 138 W3) is the teardown counterpart
; and the foundation of the APP-v1 app exit path: rcx = VA inside the
; frozen domain, page aligned. It clears a 4 KiB leaf whose effective
; US is set, invlpg's it, and returns the released physical address in
; rdx; an emptied PT (and then an emptied PD) is unlinked and handed
; back through mm_pmm_drop_table, incrementing table_drops here, so the
; net allocs-drops invariant of the +3 budget returns to the pre-map
; baseline. The identity PDPT and PML4 are never reclaimed and the
; anchor 0 US bit stays set (one way). Refusals: ENOTREADY offline,
; EPERM outside the domain or effective US clear, EINVAL unaligned,
; ENOENT absent leaf (tolerated by idempotent teardown loops), ENOTSUP
; large leaf, EIO on a failing table drop; every failure zeroes rdx.

MM_USER_VA_BASE      = 0x100400000      ; inclusive
MM_USER_VA_LIMIT     = 0x100800000      ; exclusive, also the user stack top
MM_USER_VA_SIZE      = MM_USER_VA_LIMIT - MM_USER_VA_BASE
MM_USER_CODE_BASE    = MM_USER_VA_BASE
MM_USER_CODE_SIZE    = 0x4000           ; 16 KiB, US read only executable
MM_USER_DATA_BASE    = 0x100600000
MM_USER_DATA_SIZE    = 0x200000         ; 2 MiB, US|RW|NX, stack at the top
MM_USER_STACK_SIZE   = 0x4000           ; 16 KiB at [LIMIT-16K, LIMIT)
MM_USER_PML4_SLOT    = 0                ; shared with the identity map
MM_USER_PDPT_INDEX   = 4                ; empty slot of the identity PDPT
MM_USER_TABLE_BUDGET = 3                ; 1 PD + 2 PT, frozen

if MM_USER_VA_BASE mod 0x200000 | MM_USER_VA_LIMIT mod 0x200000
        display "user VA domain bounds must be 2 MiB aligned", 13, 10
        err
end if
if MM_USER_VA_BASE < MM_PHYS_LIMIT
        display "user VA domain must sit above the identity map", 13, 10
        err
end if
if (MM_USER_VA_BASE shr 47) <> 0
        display "user VA domain must be canonical low half", 13, 10
        err
end if
if ((MM_USER_VA_BASE shr 39) and 0x1FF) <> MM_USER_PML4_SLOT
        display "user VA domain must live in PML4 slot 0", 13, 10
        err
end if
if ((MM_USER_VA_BASE shr 30) and 0x1FF) <> MM_USER_PDPT_INDEX
        display "user VA domain must use PDPT index 4, clear of identity 0..3", 13, 10
        err
end if
if ((MM_USER_VA_LIMIT - 1) shr 30) <> (MM_USER_VA_BASE shr 30)
        display "user VA domain must fit one PDPT entry", 13, 10
        err
end if
if MM_USER_CODE_BASE <> MM_USER_VA_BASE | MM_USER_CODE_SIZE <> 0x4000
        display "user code region must start the domain with 16 KiB", 13, 10
        err
end if
if MM_USER_DATA_BASE <> MM_USER_VA_BASE + 0x200000 | MM_USER_DATA_SIZE <> 0x200000
        display "user data region must be the second 2 MiB of the domain", 13, 10
        err
end if
if MM_USER_DATA_BASE + MM_USER_DATA_SIZE <> MM_USER_VA_LIMIT
        display "user data region must end at the domain limit", 13, 10
        err
end if
if MM_USER_STACK_SIZE <> MM_USER_CODE_SIZE
        display "user stack budget must stay 16 KiB", 13, 10
        err
end if
if ((MM_USER_CODE_BASE shr 21) and 0x1FF) = ((MM_USER_DATA_BASE shr 21) and 0x1FF)
        display "user code and data must use distinct PD entries", 13, 10
        err
end if
if ((MM_USER_VA_LIMIT - 1) shr 21) <> (MM_USER_DATA_BASE shr 21)
        display "user stack must share the data PD entry", 13, 10
        err
end if
if MM_USER_TABLE_BUDGET <> 3
        display "user domain table budget is frozen at +3 pages", 13, 10
        err
end if

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; status codes, EAX is signed

MM_OK         = 0
MM_EINVAL     = -1
MM_ENOMEM     = -2
MM_ENOENT     = -3
MM_EEXIST     = -4
MM_EPERM      = -5
MM_ENOTSUP    = -6
MM_ENOTREADY  = -7
MM_EALREADY   = -8
MM_EIO        = -9
MM_EOVERFLOW  = -10
MM_EFAULT     = -11
MM_EBUSY      = -12

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; 4 KiB page table entry bits

MM_PTE_P     = 0x001
MM_PTE_RW    = 0x002
MM_PTE_US    = 0x004
MM_PTE_PWT   = 0x008
MM_PTE_PCD   = 0x010
MM_PTE_A     = 0x020
MM_PTE_D     = 0x040
MM_PTE_PS    = 0x080
MM_PTE_G     = 0x100
MM_PTE_PAT   = 0x1000
MM_PTE_NX    = 0x8000000000000000

; physical address bits 51:12, the widest level 4 paging supports
MM_PTE_ADDR_MASK = 0x000FFFFFFFFFF000

; the only flags vmm_map accepts; P is implied and both RW+executable and
; any cache attribute are rejected, see the mapping rules in vmm.inc.
; APP-v1 adds US: a user mapping is either pure US (read only executable
; code) or US|RW|NX (data, stack), every other US mix is EINVAL, and the
; public window never accepts US at all (EPERM). FS-v1 M3 adjudicates the
; user VA domain below: it sits above the low 4 GiB identity map in an
; empty slot of the identity PDPT, so no identity 2 MiB leaf ever needs
; splitting and the +3 table budget holds; vmm_map_user_page serves the
; domain, vmm_map_page keeps refusing US in the public window.
MM_MAP_RW = MM_PTE_RW
MM_MAP_NX = MM_PTE_NX
MM_MAP_US = MM_PTE_US
MM_MAP_ALLOWED = MM_MAP_RW or MM_MAP_NX or MM_MAP_US

; effective query flags, encoded with the PTE bit values themselves;
; only P/RW/US/NX are reported, RW/US are the path AND, NX the path OR
MM_VQ_PRESENT = MM_PTE_P
MM_VQ_RW      = MM_PTE_RW
MM_VQ_US      = MM_PTE_US
MM_VQ_NX      = MM_PTE_NX

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; MMIO mapping contract: device registers live above the heap in their
; own 2 MiB region under the same PML4 slot as the window base.
; vmm_map_mmio fixes the leaf flags to PCD|RW|NX, there is no flags
; argument, and the core test PML4 slot stays off limits.

MM_MMIO_LAPIC_VA  = 0xFFFFC00002000000
MM_MMIO_IOAPIC_VA = 0xFFFFC00003000000
MM_MMIO_FLAGS     = MM_PTE_PCD or MM_PTE_RW or MM_PTE_NX
MM_CORE_TEST_SLOT = 386         ; PML4 slot of the core test region

if MM_MMIO_LAPIC_VA < MM_WINDOW_BASE | MM_MMIO_LAPIC_VA >= MM_WINDOW_LIMIT
        display "LAPIC MMIO VA must stay inside the mapping window", 13, 10
        err
end if
if MM_MMIO_LAPIC_VA < MM_HEAP_LIMIT
        display "LAPIC MMIO VA must sit above the heap limit", 13, 10
        err
end if
if MM_MMIO_LAPIC_VA mod 0x200000
        display "LAPIC MMIO VA must be 2 MiB aligned", 13, 10
        err
end if
if ((MM_MMIO_LAPIC_VA shr 39) and 0x1FF) <> ((MM_WINDOW_BASE shr 39) and 0x1FF)
        display "LAPIC MMIO VA must share the window base PML4 slot", 13, 10
        err
end if
if ((MM_MMIO_LAPIC_VA shr 39) and 0x1FF) = MM_CORE_TEST_SLOT
        display "LAPIC MMIO VA must not use the core test slot", 13, 10
        err
end if
if MM_MMIO_FLAGS <> 0x8000000000000012
        display "MMIO flags must be exactly PCD|RW|NX", 13, 10
        err
end if

; the IOAPIC page follows the same rules as the LAPIC page: inside the
; window, above the heap, 2 MiB aligned, same PML4 slot, never the core
; test slot; it sits strictly above the LAPIC mapping (16 MiB above it,
; so it also clears the whole FB window whose limit is the LAPIC VA)
if MM_MMIO_IOAPIC_VA < MM_WINDOW_BASE | MM_MMIO_IOAPIC_VA >= MM_WINDOW_LIMIT
        display "IOAPIC MMIO VA must stay inside the mapping window", 13, 10
        err
end if
if MM_MMIO_IOAPIC_VA < MM_HEAP_LIMIT
        display "IOAPIC MMIO VA must sit above the heap limit", 13, 10
        err
end if
if MM_MMIO_IOAPIC_VA mod 0x200000
        display "IOAPIC MMIO VA must be 2 MiB aligned", 13, 10
        err
end if
if ((MM_MMIO_IOAPIC_VA shr 39) and 0x1FF) <> ((MM_WINDOW_BASE shr 39) and 0x1FF)
        display "IOAPIC MMIO VA must share the window base PML4 slot", 13, 10
        err
end if
if ((MM_MMIO_IOAPIC_VA shr 39) and 0x1FF) = MM_CORE_TEST_SLOT
        display "IOAPIC MMIO VA must not use the core test slot", 13, 10
        err
end if
if MM_MMIO_IOAPIC_VA <= MM_MMIO_LAPIC_VA
        display "IOAPIC MMIO VA must sit strictly above the LAPIC VA", 13, 10
        err
end if

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; UEFI memory map constants needed by the BootInfo parser

MM_EFI_CONVENTIONAL_MEMORY = 7
MM_EFI_DESC_MIN_SIZE       = 40
MM_MAP_MAX_SIZE            = 0x40000   ; 256 KiB, the contract limit
MM_MAP_MAX_DESCS           = 6553      ; the frozen descriptor count limit
MM_DESC_RECORD_SIZE        = 16        ; start/end pair in the scratch list
MM_IMAGE_MAX               = 0x2000000 ; loader image+stack limit, 32 MiB

MM_EFI_MEMORY_WB      = 0x0000000000000008
MM_EFI_MEMORY_WP      = 0x0000000000001000
MM_EFI_MEMORY_RP      = 0x0000000000002000
MM_EFI_MEMORY_NV      = 0x0000000000008000
MM_EFI_MEMORY_RO      = 0x0000000000020000
MM_EFI_MEMORY_RUNTIME = 0x8000000000000000

; attributes that make a conventional range unusable for the allocator
MM_EFI_MEMORY_RESERVED_ATTR = MM_EFI_MEMORY_RUNTIME or MM_EFI_MEMORY_RP or \
        MM_EFI_MEMORY_RO or MM_EFI_MEMORY_WP or MM_EFI_MEMORY_NV

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; BootInfo v1, a private copy of struct K64BOOTINFO (208 bytes).
;
; The records below are written as explicit byte offsets instead of using
; the struct macro library: the module then needs no include beyond the
; assembler itself, so it can be pulled into any image without fighting
; over a shared macro namespace. Every offset is checked right here.

MM_BOOTINFO_SIGNATURE = 'K64BOOTI'
MM_BOOTINFO_VERSION   = 1
MM_BOOTINFO_SIZE      = 208
MM_BOOTINFO_FLAG_MAP  = 1
MM_BOOTINFO_FLAG_FRAMEBUFFER = 2
MM_BOOTINFO_FLAG_RSDP        = 4
MM_BOOTINFO_FLAG_INITRAMFS   = 8
MM_BOOTINFO_FLAG_KNOWN       = 0x0F
MM_BOOTINFO_RESERVED_QWORDS = 8

; GOP pixel formats of the framebuffer handover: RGBX8888, BGRX8888 and
; BitMask carry a linear framebuffer, BltOnly does not and is refused.
MM_GOP_PIXEL_RGBX8888     = 0
MM_GOP_PIXEL_BGRX8888     = 1
MM_GOP_PIXEL_BITMASK      = 2
MM_GOP_PIXEL_BLT_ONLY     = 3
MM_GOP_PIXEL_FORMAT_MAX   = MM_GOP_PIXEL_BITMASK

if MM_BOOTINFO_FLAG_KNOWN <> (MM_BOOTINFO_FLAG_MAP or MM_BOOTINFO_FLAG_FRAMEBUFFER or MM_BOOTINFO_FLAG_RSDP or MM_BOOTINFO_FLAG_INITRAMFS)
        display "known BootInfo flags must cover exactly the four features", 13, 10
        err
end if
if MM_GOP_PIXEL_FORMAT_MAX <> 2 | MM_GOP_PIXEL_BLT_ONLY <> 3
        display "GOP pixel formats must stay RGBX/BGRX/BitMask, BltOnly refused", 13, 10
        err
end if

MM_BOOTINFO.signature            = 0
MM_BOOTINFO.version              = 8
MM_BOOTINFO.struct_size          = 12
MM_BOOTINFO.flags                = 16
MM_BOOTINFO.memmap_addr          = 24
MM_BOOTINFO.memmap_size          = 32
MM_BOOTINFO.memmap_desc_size     = 40
MM_BOOTINFO.memmap_desc_version  = 44
MM_BOOTINFO.memmap_desc_count    = 48
MM_BOOTINFO.kernel_phys_start    = 56
MM_BOOTINFO.kernel_phys_end      = 64
MM_BOOTINFO.stack_phys_start     = 72
MM_BOOTINFO.stack_phys_end       = 80
MM_BOOTINFO.gop_framebuffer_addr = 88
MM_BOOTINFO.gop_framebuffer_size = 96
MM_BOOTINFO.gop_width            = 104
MM_BOOTINFO.gop_height           = 108
MM_BOOTINFO.gop_pixel_format     = 112
MM_BOOTINFO.gop_pixels_per_line  = 116
MM_BOOTINFO.rsdp_addr            = 120
MM_BOOTINFO.initramfs_addr       = 128
MM_BOOTINFO.initramfs_size       = 136
MM_BOOTINFO.reserved             = 144

if MM_BOOTINFO.reserved + MM_BOOTINFO_RESERVED_QWORDS * 8 <> MM_BOOTINFO_SIZE
        display "private BootInfo copy is not 208 bytes", 13, 10
        err
end if
if MM_BOOTINFO.memmap_desc_size <> 40
        display "memmap descriptor size offset changed", 13, 10
        err
end if
if MM_BOOTINFO.gop_width <> 104 | MM_BOOTINFO.gop_height <> 108
        display "GOP geometry fields must stay dwords at 104 and 108", 13, 10
        err
end if
if MM_BOOTINFO.gop_pixel_format <> 112 | MM_BOOTINFO.gop_pixels_per_line <> 116
        display "GOP format and pitch must stay dwords at 112 and 116", 13, 10
        err
end if

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; one EFI_MEMORY_DESCRIPTOR prefix, only the fields this stage reads

MM_MEM_DESC_size      = MM_EFI_DESC_MIN_SIZE
MM_MEM_DESC.type      = 0
MM_MEM_DESC.pad       = 4
MM_MEM_DESC.phys_start = 8
MM_MEM_DESC.virt_start = 16
MM_MEM_DESC.pages     = 24
MM_MEM_DESC.attributes = 32

if MM_MEM_DESC.attributes + 8 <> MM_MEM_DESC_size
        display "EFI descriptor prefix must be 40 bytes", 13, 10
        err
end if

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; 64 byte INIT_PARAMS handed to pmm_init and vmm_init

MM_INIT_PARAMS_size = 64
MM_INIT_PARAMS.version       = 0
MM_INIT_PARAMS.size          = 4
MM_INIT_PARAMS.bootinfo_va   = 8
MM_INIT_PARAMS.bootinfo_phys = 16
MM_INIT_PARAMS.cr3_phys      = 24
MM_INIT_PARAMS.stack_top_va  = 32
MM_INIT_PARAMS.flags         = 40
MM_INIT_PARAMS.reserved1     = 48
MM_INIT_PARAMS.reserved2     = 56

if MM_INIT_PARAMS.reserved2 + 8 <> MM_INIT_PARAMS_size
        display "INIT_PARAMS must be 64 bytes", 13, 10
        err
end if

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; 64 byte physical allocator state, eight frozen qwords

MM_PMM_STATE_size            = 64
MM_PMM_STATE.ready           = 0
MM_PMM_STATE.managed_limit   = 8
MM_PMM_STATE.total_pages     = 16
MM_PMM_STATE.free_pages      = 24
MM_PMM_STATE.allocated_pages = 32
MM_PMM_STATE.reserved_pages  = 40
MM_PMM_STATE.next_pfn        = 48
MM_PMM_STATE.root_phys       = 56

if MM_PMM_STATE.root_phys + 8 <> MM_PMM_STATE_size
        display "PMM state must be 64 bytes", 13, 10
        err
end if

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; 64 byte virtual memory state

MM_VMM_STATE_size        = 64
MM_VMM_STATE.ready       = 0
MM_VMM_STATE.phys_bits   = 4
MM_VMM_STATE.root_phys   = 8
MM_VMM_STATE.pa_mask     = 16
MM_VMM_STATE.window_base = 24
MM_VMM_STATE.window_limit = 32
MM_VMM_STATE.table_allocs = 40
MM_VMM_STATE.table_drops = 48
MM_VMM_STATE.reserved0   = 56

if MM_VMM_STATE.reserved0 + 8 <> MM_VMM_STATE_size
        display "VMM state must be 64 bytes", 13, 10
        err
end if

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; 48 byte bump heap state, six frozen qwords

MM_HEAP_STATE_size        = 48
MM_HEAP_STATE.ready       = 0
MM_HEAP_STATE.base        = 8
MM_HEAP_STATE.brk         = 16
MM_HEAP_STATE.mapped_end  = 24
MM_HEAP_STATE.limit       = 32
MM_HEAP_STATE.reserved    = 40

if MM_HEAP_STATE.reserved + 8 <> MM_HEAP_STATE_size
        display "heap state must be 48 bytes", 13, 10
        err
end if

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; 128 byte boot information cached from INIT_PARAMS and BootInfo

MM_BOOT_STATE_size           = 128
MM_BOOT_STATE.bootinfo_phys  = 0
MM_BOOT_STATE.cr3_phys       = 8
MM_BOOT_STATE.memmap_phys    = 16
MM_BOOT_STATE.memmap_size    = 24
MM_BOOT_STATE.kernel_start   = 32
MM_BOOT_STATE.kernel_end     = 40
MM_BOOT_STATE.stack_start    = 48
MM_BOOT_STATE.stack_end      = 56
MM_BOOT_STATE.stack_top_va   = 64
MM_BOOT_STATE.desc_stride    = 72
MM_BOOT_STATE.desc_count     = 76
MM_BOOT_STATE.reserved       = 80

if MM_BOOT_STATE.reserved + 48 <> MM_BOOT_STATE_size
        display "boot state must be 128 bytes", 13, 10
        err
end if

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; public statistics and query records

; PMM_STATS: six qwords, counts cover eligible pages only and obey
; total = free + allocated + reserved; the hint is a PFN
MM_PMM_STATS_size = 48
MM_PMM_STATS.managed_limit = 0
MM_PMM_STATS.total         = 8
MM_PMM_STATS.free          = 16
MM_PMM_STATS.allocated     = 24
MM_PMM_STATS.reserved      = 32
MM_PMM_STATS.next_pfn      = 40

if MM_PMM_STATS.next_pfn + 8 <> MM_PMM_STATS_size
        display "PMM statistics must be 48 bytes", 13, 10
        err
end if

; VMM_QUERY: five qwords in the frozen order
MM_VMM_QUERY_size             = 40
MM_VMM_QUERY.physical_address = 0
MM_VMM_QUERY.leaf_entry_va    = 8
MM_VMM_QUERY.raw_leaf         = 16
MM_VMM_QUERY.page_size        = 24
MM_VMM_QUERY.effective_flags  = 32

if MM_VMM_QUERY.effective_flags + 8 <> MM_VMM_QUERY_size
        display "VMM query record must be 40 bytes", 13, 10
        err
end if

; heap statistics: four qwords
MM_HEAP_STATS_size = 32
MM_HEAP_STATS.base       = 0
MM_HEAP_STATS.brk        = 8
MM_HEAP_STATS.mapped_end = 16
MM_HEAP_STATS.limit      = 24

if MM_HEAP_STATS.limit + 8 <> MM_HEAP_STATS_size
        display "heap statistics must be 32 bytes", 13, 10
        err
end if

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; internal limits and test hooks

MM_TABLE_WALK_MAX = 64          ; table pages examined by the CR3 pinner
MM_CR3_PIN_MAX    = 64          ; distinct table pages pinned at most

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; register helper macros

; full frame: saves every callee saved register and keeps RSP 16 byte
; aligned with 32 bytes of shadow space available for nested calls
macro mm_enter
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

macro mm_leave
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
