;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Core integration: the shared read only contract K64-FOUNDATION-v1.
;;
;; This file is included after cpu/cpu.asm and mm/mm.asm, so both private
;; copies of the contract (CPU_* and MM_*) already exist. It publishes the
;; shared K64_* spelling of every frozen value and proves at assembly time
;; that all three copies agree. It emits no bytes at all.
;;
;; Calling convention (the one both modules implement):
;;  - Microsoft x64 integer ABI, RCX/RDX/R8/R9 arguments;
;;  - 32 bytes of shadow space, RSP mod 16 = 0 before a call, no red zone;
;;  - RBX/RBP/RSI/RDI/R12-R15 are callee saved;
;;  - EAX is a signed status, RDX is the main result, 0 is success;
;;  - DF = 0 and IF unchanged on return.
;;

K64_CONTRACT_VERSION = 1

; ------------------------------------------------------------------
; status codes, EAX, signed
; ------------------------------------------------------------------

K64_OK              = 0
K64_EINVAL          = -1
K64_ENOMEM          = -2
K64_ENOENT          = -3
K64_EEXIST          = -4
K64_EPERM           = -5
K64_ENOTSUP         = -6
K64_ENOTREADY       = -7
K64_EALREADY        = -8
K64_EIO             = -9
K64_EOVERFLOW       = -10
K64_EFAULT          = -11
K64_EBUSY           = -12

; ------------------------------------------------------------------
; memory geometry fixed by the contract
; ------------------------------------------------------------------

K64_PAGE_SIZE       = 0x1000
K64_LARGE_PAGE_SIZE = 0x200000
K64_PHYS_LIMIT      = 0x100000000           ; exclusive, low 4 GiB only
K64_LOW_RESERVED_END = 0x100000             ; [0, 1 MiB) permanently reserved
K64_IMAGE_MAX       = 0x2000000             ; loader image + stack, 32 MiB
K64_DATA_ALIGN      = 0x1000
K64_STACK_FRAME     = 128                   ; shadow space plus locals

; ------------------------------------------------------------------
; 4 KiB page table entry bits
; ------------------------------------------------------------------

K64_PTE_P           = 0x001
K64_PTE_RW          = 0x002
K64_PTE_US          = 0x004
K64_PTE_PWT         = 0x008
K64_PTE_PCD         = 0x010
K64_PTE_A           = 0x020
K64_PTE_D           = 0x040
K64_PTE_PS          = 0x080
K64_PTE_G           = 0x100
K64_PTE_NX          = 0x8000000000000000

; ------------------------------------------------------------------
; the public mapping window, the heap and the test region
; ------------------------------------------------------------------

K64_VMM_WINDOW_BASE = 0xFFFFC00000000000
K64_VMM_WINDOW_LIMIT = 0xFFFFC20000000000   ; exclusive
K64_HEAP_BASE       = 0xFFFFC00000000000
K64_HEAP_LIMIT      = 0xFFFFC00001000000    ; exclusive, 16 MiB
K64_HEAP_INITIAL    = 0x10000               ; 64 KiB mapped by heap_init
K64_VMM_TEST_BASE   = 0xFFFFC10000000000
K64_VMM_TEST_LIMIT  = 0xFFFFC10002000000    ; exclusive, test reservation

; ------------------------------------------------------------------
; K64_INIT_PARAMS, 64 bytes, 8 byte aligned, filled by the integration
; ------------------------------------------------------------------

K64_INIT_VERSION        = 1
K64_INIT_SIZE           = 64
K64_INIT_OFF_VERSION    = 0
K64_INIT_OFF_SIZE       = 4
K64_INIT_OFF_BOOTINFO_VA   = 8
K64_INIT_OFF_BOOTINFO_PHYS = 16
K64_INIT_OFF_CR3_PHYS   = 24
K64_INIT_OFF_STACK_TOP  = 32
K64_INIT_OFF_FLAGS      = 40
K64_INIT_OFF_RESERVED0  = 48
K64_INIT_OFF_RESERVED1  = 56
K64_INIT_FLAGS_VALUE    = 0
K64_INIT_RESERVED_VALUE = 0

virtual at 0
k64_init_params_layout:
k64_p_version:
        dd      ?
k64_p_size:
        dd      ?
k64_p_bootinfo_va:
        dq      ?
k64_p_bootinfo_phys:
        dq      ?
k64_p_cr3_phys:
        dq      ?
k64_p_stack_top:
        dq      ?
k64_p_flags:
        dq      ?
k64_p_reserved0:
        dq      ?
k64_p_reserved1:
        dq      ?
k64_init_params_layout_end:
end virtual

if k64_init_params_layout_end - k64_init_params_layout <> K64_INIT_SIZE
        err 'K64_INIT_PARAMS must stay 64 bytes'
end if
if k64_p_version - k64_init_params_layout <> K64_INIT_OFF_VERSION
        err 'K64_INIT_PARAMS.version offset'
end if
if k64_p_size - k64_init_params_layout <> K64_INIT_OFF_SIZE
        err 'K64_INIT_PARAMS.size offset'
end if
if k64_p_bootinfo_va - k64_init_params_layout <> K64_INIT_OFF_BOOTINFO_VA
        err 'K64_INIT_PARAMS.bootinfo_va offset'
end if
if k64_p_bootinfo_phys - k64_init_params_layout <> K64_INIT_OFF_BOOTINFO_PHYS
        err 'K64_INIT_PARAMS.bootinfo_phys offset'
end if
if k64_p_cr3_phys - k64_init_params_layout <> K64_INIT_OFF_CR3_PHYS
        err 'K64_INIT_PARAMS.cr3_phys offset'
end if
if k64_p_stack_top - k64_init_params_layout <> K64_INIT_OFF_STACK_TOP
        err 'K64_INIT_PARAMS.stack_top_va offset'
end if
if k64_p_flags - k64_init_params_layout <> K64_INIT_OFF_FLAGS
        err 'K64_INIT_PARAMS.flags offset'
end if
if k64_p_reserved0 - k64_init_params_layout <> K64_INIT_OFF_RESERVED0
        err 'K64_INIT_PARAMS.reserved0 offset'
end if
if k64_p_reserved1 - k64_init_params_layout <> K64_INIT_OFF_RESERVED1
        err 'K64_INIT_PARAMS.reserved1 offset'
end if

; ------------------------------------------------------------------
; the three copies of the contract must agree
;
; Every check compares the shared value with both private copies, so a
; drift in any module is a build failure instead of a run time surprise.
; ------------------------------------------------------------------

if K64_OK <> CPU_OK | K64_OK <> MM_OK
        err 'K64_OK disagrees with a module copy'
end if
if K64_EINVAL <> CPU_EINVAL | K64_EINVAL <> MM_EINVAL
        err 'K64_EINVAL disagrees with a module copy'
end if
if K64_ENOMEM <> CPU_ENOMEM | K64_ENOMEM <> MM_ENOMEM
        err 'K64_ENOMEM disagrees with a module copy'
end if
if K64_ENOENT <> CPU_ENOENT | K64_ENOENT <> MM_ENOENT
        err 'K64_ENOENT disagrees with a module copy'
end if
if K64_EEXIST <> CPU_EEXIST | K64_EEXIST <> MM_EEXIST
        err 'K64_EEXIST disagrees with a module copy'
end if
if K64_EPERM <> CPU_EPERM | K64_EPERM <> MM_EPERM
        err 'K64_EPERM disagrees with a module copy'
end if
if K64_ENOTSUP <> CPU_ENOTSUP | K64_ENOTSUP <> MM_ENOTSUP
        err 'K64_ENOTSUP disagrees with a module copy'
end if
if K64_ENOTREADY <> CPU_ENOTREADY | K64_ENOTREADY <> MM_ENOTREADY
        err 'K64_ENOTREADY disagrees with a module copy'
end if
if K64_EALREADY <> CPU_EALREADY | K64_EALREADY <> MM_EALREADY
        err 'K64_EALREADY disagrees with a module copy'
end if
if K64_EIO <> CPU_EIO | K64_EIO <> MM_EIO
        err 'K64_EIO disagrees with a module copy'
end if
if K64_EOVERFLOW <> CPU_EOVERFLOW | K64_EOVERFLOW <> MM_EOVERFLOW
        err 'K64_EOVERFLOW disagrees with a module copy'
end if
if K64_EFAULT <> CPU_EFAULT | K64_EFAULT <> MM_EFAULT
        err 'K64_EFAULT disagrees with a module copy'
end if
if K64_EBUSY <> CPU_EBUSY | K64_EBUSY <> MM_EBUSY
        err 'K64_EBUSY disagrees with a module copy'
end if

if K64_PAGE_SIZE <> CPU_PAGE_SIZE | K64_PAGE_SIZE <> MM_PAGE_SIZE
        err 'page size disagrees with a module copy'
end if
if K64_PHYS_LIMIT <> CPU_PHYS_LIMIT | K64_PHYS_LIMIT <> MM_PHYS_LIMIT
        err 'physical limit disagrees with a module copy'
end if
if K64_DATA_ALIGN <> CPU_DATA_ALIGN
        err 'data alignment disagrees with the CPU copy'
end if
if K64_STACK_FRAME <> CPU_STACK_FRAME
        err 'stack frame disagrees with the CPU copy'
end if
if K64_IMAGE_MAX <> MM_IMAGE_MAX
        err 'image limit disagrees with the MM copy'
end if

if K64_PTE_P <> MM_PTE_P | K64_PTE_RW <> MM_PTE_RW | K64_PTE_US <> MM_PTE_US
        err 'PTE permission bits disagree with the MM copy'
end if
if K64_PTE_PWT <> MM_PTE_PWT | K64_PTE_PCD <> MM_PTE_PCD
        err 'PTE cache bits disagree with the MM copy'
end if
if K64_PTE_A <> MM_PTE_A | K64_PTE_D <> MM_PTE_D | K64_PTE_PS <> MM_PTE_PS
        err 'PTE status bits disagree with the MM copy'
end if
if K64_PTE_G <> MM_PTE_G | K64_PTE_NX <> MM_PTE_NX
        err 'PTE global/NX bits disagree with the MM copy'
end if

if K64_VMM_WINDOW_BASE <> MM_WINDOW_BASE
        err 'mapping window base disagrees with the MM copy'
end if
if K64_VMM_WINDOW_LIMIT <> MM_WINDOW_LIMIT
        err 'mapping window limit disagrees with the MM copy'
end if
if K64_HEAP_BASE <> MM_HEAP_BASE | K64_HEAP_LIMIT <> MM_HEAP_LIMIT
        err 'heap range disagrees with the MM copy'
end if
if K64_HEAP_INITIAL <> MM_HEAP_INITIAL
        err 'initial heap commit disagrees with the MM copy'
end if
if K64_VMM_TEST_BASE < MM_WINDOW_BASE | K64_VMM_TEST_LIMIT > MM_WINDOW_LIMIT
        err 'the test region must stay inside the mapping window'
end if
if K64_VMM_TEST_BASE mod K64_PAGE_SIZE | K64_VMM_TEST_LIMIT mod K64_PAGE_SIZE
        err 'the test region must be page aligned'
end if

; layout premises the core self test (leg7 accounting) relies on: the
; test region sits clear of the heap, owns a PML4 slot of its own so its
; teardown can never touch the mm selftest window, and its four probe
; pages live under one PDPT entry and one page table (pinned table count
; stays exact). Pure constant math, zero bytes emitted.
if K64_VMM_TEST_BASE < K64_HEAP_LIMIT
        err 'the test region must start at or above the heap limit'
end if
if ((K64_VMM_TEST_BASE shr 39) and 0x1FF) = ((K64_VMM_WINDOW_BASE shr 39) and 0x1FF)
        err 'the test region must use a PML4 slot separate from the mm window'
end if
if (K64_VMM_TEST_BASE shr 30) <> ((K64_VMM_TEST_BASE + 3 * K64_PAGE_SIZE) shr 30)
        err 'the four test pages must share one PDPT entry'
end if
if (K64_VMM_TEST_BASE shr 21) <> ((K64_VMM_TEST_BASE + 3 * K64_PAGE_SIZE) shr 21)
        err 'the four test pages must share one page table'
end if

if K64_INIT_VERSION <> CPU_INIT_VERSION | K64_INIT_SIZE <> CPU_INIT_SIZE
        err 'INIT_PARAMS version/size disagree with the CPU copy'
end if
if K64_INIT_SIZE <> MM_INIT_PARAMS_size
        err 'INIT_PARAMS size disagrees with the MM copy'
end if
if K64_INIT_OFF_VERSION <> CPU_INIT_OFF_VERSION
        err 'INIT_PARAMS.version disagrees with the CPU copy'
end if
if K64_INIT_OFF_VERSION <> MM_INIT_PARAMS.version
        err 'INIT_PARAMS.version disagrees with the MM copy'
end if
if K64_INIT_OFF_SIZE <> CPU_INIT_OFF_SIZE
        err 'INIT_PARAMS.size disagrees with the CPU copy'
end if
if K64_INIT_OFF_SIZE <> MM_INIT_PARAMS.size
        err 'INIT_PARAMS.size disagrees with the MM copy'
end if
if K64_INIT_OFF_BOOTINFO_VA <> CPU_INIT_OFF_BOOTINFO_VA
        err 'INIT_PARAMS.bootinfo_va disagrees with the CPU copy'
end if
if K64_INIT_OFF_BOOTINFO_VA <> MM_INIT_PARAMS.bootinfo_va
        err 'INIT_PARAMS.bootinfo_va disagrees with the MM copy'
end if
if K64_INIT_OFF_BOOTINFO_PHYS <> CPU_INIT_OFF_BOOTINFO_PHYS
        err 'INIT_PARAMS.bootinfo_phys disagrees with the CPU copy'
end if
if K64_INIT_OFF_BOOTINFO_PHYS <> MM_INIT_PARAMS.bootinfo_phys
        err 'INIT_PARAMS.bootinfo_phys disagrees with the MM copy'
end if
if K64_INIT_OFF_CR3_PHYS <> CPU_INIT_OFF_CR3_PHYS
        err 'INIT_PARAMS.cr3_phys disagrees with the CPU copy'
end if
if K64_INIT_OFF_CR3_PHYS <> MM_INIT_PARAMS.cr3_phys
        err 'INIT_PARAMS.cr3_phys disagrees with the MM copy'
end if
if K64_INIT_OFF_STACK_TOP <> CPU_INIT_OFF_STACK_TOP
        err 'INIT_PARAMS.stack_top_va disagrees with the CPU copy'
end if
if K64_INIT_OFF_STACK_TOP <> MM_INIT_PARAMS.stack_top_va
        err 'INIT_PARAMS.stack_top_va disagrees with the MM copy'
end if
if K64_INIT_OFF_FLAGS <> CPU_INIT_OFF_FLAGS
        err 'INIT_PARAMS.flags disagrees with the CPU copy'
end if
if K64_INIT_OFF_FLAGS <> MM_INIT_PARAMS.flags
        err 'INIT_PARAMS.flags disagrees with the MM copy'
end if
if K64_INIT_OFF_RESERVED0 <> CPU_INIT_OFF_RESERVED0
        err 'INIT_PARAMS.reserved0 disagrees with the CPU copy'
end if
if K64_INIT_OFF_RESERVED0 <> MM_INIT_PARAMS.reserved1
        err 'INIT_PARAMS.reserved0 disagrees with the MM copy'
end if
if K64_INIT_OFF_RESERVED1 <> CPU_INIT_OFF_RESERVED1
        err 'INIT_PARAMS.reserved1 disagrees with the CPU copy'
end if
if K64_INIT_OFF_RESERVED1 <> MM_INIT_PARAMS.reserved2
        err 'INIT_PARAMS.reserved1 disagrees with the MM copy'
end if

; the shared BootInfo struct and the MM private copy must describe the
; same 208 byte record, the integration reads the ranges through it
if sizeof.K64BOOTINFO <> MM_BOOTINFO_SIZE
        err 'BootInfo size disagrees with the MM copy'
end if
if K64BOOTINFO.kernel_phys_start <> MM_BOOTINFO.kernel_phys_start
        err 'BootInfo.kernel_phys_start disagrees with the MM copy'
end if
if K64BOOTINFO.kernel_phys_end <> MM_BOOTINFO.kernel_phys_end
        err 'BootInfo.kernel_phys_end disagrees with the MM copy'
end if
if K64BOOTINFO.stack_phys_start <> MM_BOOTINFO.stack_phys_start
        err 'BootInfo.stack_phys_start disagrees with the MM copy'
end if
if K64BOOTINFO.stack_phys_end <> MM_BOOTINFO.stack_phys_end
        err 'BootInfo.stack_phys_end disagrees with the MM copy'
end if
if VIRT_KERNEL_BASE <> MM_KERNEL_BASE
        err 'the kernel virtual base disagrees with the MM copy'
end if

; ------------------------------------------------------------------
; MMIO mapping contract (vmm_map_mmio), shared copies and the placement
; premises of the LAPIC window: above the heap, 2 MiB aligned, under the
; window base PML4 slot and never under the core test slot. Pure
; constant math, zero bytes emitted.
; ------------------------------------------------------------------

K64_MMIO_LAPIC_VA  = 0xFFFFC00002000000
K64_MMIO_FLAGS     = K64_PTE_PCD or K64_PTE_RW or K64_PTE_NX
K64_CORE_TEST_SLOT = 386

if K64_MMIO_LAPIC_VA <> MM_MMIO_LAPIC_VA
        err 'LAPIC MMIO VA disagrees with the MM copy'
end if
if K64_MMIO_FLAGS <> MM_MMIO_FLAGS
        err 'MMIO flags disagree with the MM copy'
end if
if K64_CORE_TEST_SLOT <> MM_CORE_TEST_SLOT
        err 'core test PML4 slot disagrees with the MM copy'
end if
if K64_MMIO_LAPIC_VA < K64_VMM_WINDOW_BASE | K64_MMIO_LAPIC_VA >= K64_VMM_WINDOW_LIMIT
        err 'the LAPIC MMIO VA must stay inside the mapping window'
end if
if K64_MMIO_LAPIC_VA < K64_HEAP_LIMIT
        err 'the LAPIC MMIO VA must sit above the heap limit'
end if
if K64_MMIO_LAPIC_VA mod K64_LARGE_PAGE_SIZE
        err 'the LAPIC MMIO VA must be 2 MiB aligned'
end if
if ((K64_MMIO_LAPIC_VA shr 39) and 0x1FF) <> ((K64_VMM_WINDOW_BASE shr 39) and 0x1FF)
        err 'the LAPIC MMIO VA must share the window base PML4 slot'
end if
if ((K64_MMIO_LAPIC_VA shr 39) and 0x1FF) = ((K64_VMM_TEST_BASE shr 39) and 0x1FF)
        err 'the LAPIC MMIO VA must not use the core test PML4 slot'
end if
if K64_CORE_TEST_SLOT <> ((K64_VMM_TEST_BASE shr 39) and 0x1FF)
        err 'the core test slot constant must match the test region placement'
end if
if K64_MMIO_FLAGS <> 0x8000000000000012
        err 'the MMIO flags must be exactly PCD|RW|NX'
end if

; ------------------------------------------------------------------
; BootInfo feature flags: the loader copy (boot/bootinfo.inc) and the
; private MM copy must agree, the known mask covers exactly the four
; features, and the GOP format ceiling stays below BltOnly. Pure
; constant math, zero bytes emitted.
; ------------------------------------------------------------------

if K64BOOTINFO_FLAG_MEMORY_MAP <> MM_BOOTINFO_FLAG_MAP
        err 'BootInfo MEMORY_MAP flag disagrees with the MM copy'
end if
if K64BOOTINFO_FLAG_FRAMEBUFFER <> MM_BOOTINFO_FLAG_FRAMEBUFFER
        err 'BootInfo FRAMEBUFFER flag disagrees with the MM copy'
end if
if K64BOOTINFO_FLAG_RSDP <> MM_BOOTINFO_FLAG_RSDP
        err 'BootInfo RSDP flag disagrees with the MM copy'
end if
if K64BOOTINFO_FLAG_INITRAMFS <> MM_BOOTINFO_FLAG_INITRAMFS
        err 'BootInfo INITRAMFS flag disagrees with the MM copy'
end if
if K64BOOTINFO_FLAG_KNOWN <> MM_BOOTINFO_FLAG_KNOWN
        err 'BootInfo known flag mask disagrees with the MM copy'
end if
if K64BOOTINFO_FLAG_KNOWN <> (K64BOOTINFO_FLAG_MEMORY_MAP or K64BOOTINFO_FLAG_FRAMEBUFFER or K64BOOTINFO_FLAG_RSDP or K64BOOTINFO_FLAG_INITRAMFS)
        err 'the known flag mask must cover exactly the four features'
end if
if K64BOOTINFO.gop_width <> MM_BOOTINFO.gop_width | K64BOOTINFO.gop_height <> MM_BOOTINFO.gop_height
        err 'GOP geometry offsets disagree with the MM copy'
end if
if K64BOOTINFO.gop_pixel_format <> MM_BOOTINFO.gop_pixel_format | K64BOOTINFO.gop_pixels_per_line <> MM_BOOTINFO.gop_pixels_per_line
        err 'GOP format and pitch offsets disagree with the MM copy'
end if
if MM_GOP_PIXEL_FORMAT_MAX >= MM_GOP_PIXEL_BLT_ONLY
        err 'the GOP format ceiling must refuse BltOnly'
end if

; ------------------------------------------------------------------
; IRQ-v1: the shared K64_* spelling of the IRQ layer contract and the
; three copy comparison against the private IRQ_* values. Appended by
; task 70; every assertion above stays untouched. Pure constant math,
; zero bytes emitted.
; ------------------------------------------------------------------

K64_IRQ_VEC_TIMER    = 0x20
K64_IRQ_VEC_SPURIOUS = 0xFF
K64_IRQ_SVR_VALUE    = 0x1FF
K64_IRQ_LVT_MASK     = 0x10000
K64_IRQ_LVT_PERIODIC = 0x20000
K64_IRQ_DIVIDE_16    = 0x3
K64_IRQ_LAPIC_PA     = 0xFEE00000

K64_LAPIC_ID         = 0x020
K64_LAPIC_VER        = 0x030
K64_LAPIC_TPR        = 0x080
K64_LAPIC_EOI        = 0x0B0
K64_LAPIC_SVR        = 0x0F0
K64_LAPIC_ISR        = 0x100
K64_LAPIC_ISR_LAST   = 0x170
K64_LAPIC_ESR        = 0x280
K64_LAPIC_LVT_TIMER  = 0x320
K64_LAPIC_LVT_LINT0  = 0x350
K64_LAPIC_LVT_LINT1  = 0x360
K64_LAPIC_LVT_ERROR  = 0x370
K64_LAPIC_INIT_COUNT = 0x380
K64_LAPIC_CUR_COUNT  = 0x390
K64_LAPIC_DIVIDE     = 0x3E0

; the leg 7 accounting expectation, frozen: irq_init builds exactly one
; new table page (the PT of the LAPIC 2 MiB range; PDPT and PD are shared
; with the heap), and it runs BEFORE the core self test takes its leg 2
; baseline, so the leg 7 delta stays the three slot 386 tables.
K64_CORE_ACCOUNT_TABLES = 3

if K64_IRQ_VEC_TIMER <> IRQ_VEC_TIMER
        err 'timer vector disagrees with the IRQ copy'
end if
if K64_IRQ_VEC_SPURIOUS <> IRQ_VEC_SPURIOUS
        err 'spurious vector disagrees with the IRQ copy'
end if
if K64_IRQ_SVR_VALUE <> IRQ_SVR_VALUE
        err 'SVR value disagrees with the IRQ copy'
end if
if K64_IRQ_LVT_MASK <> IRQ_LVT_MASK | K64_IRQ_LVT_PERIODIC <> IRQ_LVT_PERIODIC
        err 'LVT mask/mode bits disagree with the IRQ copy'
end if
if K64_IRQ_DIVIDE_16 <> IRQ_DIVIDE_16
        err 'divide encoding disagrees with the IRQ copy'
end if
if K64_IRQ_LAPIC_PA <> IRQ_LAPIC_PA | K64_IRQ_LAPIC_PA <> IRQ_APIC_BASE_PA
        err 'LAPIC physical base disagrees with the IRQ copy'
end if
if IRQ_LAPIC_VA <> K64_MMIO_LAPIC_VA | IRQ_LAPIC_VA <> MM_MMIO_LAPIC_VA
        err 'LAPIC MMIO VA disagrees with the IRQ/MM copies'
end if

if K64_LAPIC_ID <> IRQ_LAPIC_ID | K64_LAPIC_VER <> IRQ_LAPIC_VER
        err 'LAPIC ID/VER offsets disagree with the IRQ copy'
end if
if K64_LAPIC_TPR <> IRQ_LAPIC_TPR | K64_LAPIC_EOI <> IRQ_LAPIC_EOI
        err 'LAPIC TPR/EOI offsets disagree with the IRQ copy'
end if
if K64_LAPIC_SVR <> IRQ_LAPIC_SVR | K64_LAPIC_ESR <> IRQ_LAPIC_ESR
        err 'LAPIC SVR/ESR offsets disagree with the IRQ copy'
end if
if K64_LAPIC_ISR <> IRQ_LAPIC_ISR | K64_LAPIC_ISR_LAST <> IRQ_LAPIC_ISR_LAST
        err 'LAPIC ISR span disagrees with the IRQ copy'
end if
if K64_LAPIC_LVT_TIMER <> IRQ_LAPIC_LVT_TIMER | K64_LAPIC_LVT_LINT0 <> IRQ_LAPIC_LVT_LINT0
        err 'LAPIC timer/LINT0 LVT offsets disagree with the IRQ copy'
end if
if K64_LAPIC_LVT_LINT1 <> IRQ_LAPIC_LVT_LINT1 | K64_LAPIC_LVT_ERROR <> IRQ_LAPIC_LVT_ERROR
        err 'LAPIC LINT1/error LVT offsets disagree with the IRQ copy'
end if
if K64_LAPIC_INIT_COUNT <> IRQ_LAPIC_INIT_COUNT | K64_LAPIC_CUR_COUNT <> IRQ_LAPIC_CUR_COUNT
        err 'LAPIC count offsets disagree with the IRQ copy'
end if
if K64_LAPIC_DIVIDE <> IRQ_LAPIC_DIVIDE
        err 'LAPIC divide offset disagrees with the IRQ copy'
end if

if K64_OK <> IRQ_OK | K64_EINVAL <> IRQ_EINVAL | K64_ENOMEM <> IRQ_ENOMEM
        err 'IRQ status codes disagree with the shared copy'
end if
if K64_ENOENT <> IRQ_ENOENT | K64_EEXIST <> IRQ_EEXIST | K64_EPERM <> IRQ_EPERM
        err 'IRQ status codes disagree with the shared copy'
end if
if K64_ENOTSUP <> IRQ_ENOTSUP | K64_ENOTREADY <> IRQ_ENOTREADY
        err 'IRQ status codes disagree with the shared copy'
end if
if K64_EALREADY <> IRQ_EALREADY | K64_EIO <> IRQ_EIO | K64_EOVERFLOW <> IRQ_EOVERFLOW
        err 'IRQ status codes disagree with the shared copy'
end if
if K64_EFAULT <> IRQ_EFAULT | K64_EBUSY <> IRQ_EBUSY
        err 'IRQ status codes disagree with the shared copy'
end if

if IRQ_FRAME_VECTOR <> CPU_FRAME_VECTOR | IRQ_FRAME_RFLAGS <> CPU_FRAME_RFLAGS
        err 'IRQ frame offsets disagree with the CPU copy'
end if
if IRQ_RFLAGS_IF <> CPU_RFLAGS_IF
        err 'IRQ IF bit disagrees with the CPU copy'
end if
if IRQ_VEC_TIMER < CPU_IRQ_VECTOR_MIN
        err 'the timer vector must sit inside the CPU asynchronous window'
end if
if IRQ_PUTS_MAX <> CPU_PUTS_MAX | IRQ_PUTHEX_DIGITS <> CPU_PUTHEX_DIGITS
        err 'IRQ console caps disagree with the CPU copy'
end if
if IRQ_PAGE_SIZE <> K64_PAGE_SIZE | IRQ_DATA_ALIGN <> K64_DATA_ALIGN
        err 'IRQ geometry disagrees with the shared copy'
end if
if IRQ_STACK_FRAME <> K64_STACK_FRAME
        err 'IRQ stack frame disagrees with the shared copy'
end if

; the counter layout the gdbstub smoke reads: test ticks and the self
; test mask are qwords at their frozen offsets, the leg 5 switch defaults
; to zero and sits at its own frozen offset
K64_IRQ_DATA_OFF_TEST_TICKS = 24
K64_IRQ_DATA_OFF_TEST_MASK  = 32
K64_IRQ_DATA_OFF_SKIP_EOI   = 64
K64_IRQ_DATA_OFF_FRAME_IF   = 104

if K64_IRQ_DATA_OFF_TEST_TICKS <> IRQ_DATA_OFF_TEST_TICKS
        err 'test tick offset disagrees with the IRQ copy'
end if
if K64_IRQ_DATA_OFF_TEST_MASK <> IRQ_DATA_OFF_TEST_MASK
        err 'selftest mask offset disagrees with the IRQ copy'
end if
if K64_IRQ_DATA_OFF_SKIP_EOI <> IRQ_DATA_OFF_SKIP_EOI
        err 'leg5 switch offset disagrees with the IRQ copy'
end if
if K64_IRQ_DATA_OFF_FRAME_IF <> IRQ_DATA_OFF_FRAME_IF
        err 'interrupted-frame IF evidence offset disagrees with the IRQ copy'
end if
if IRQ_ST_FINAL_N <> 10
        err 'the frozen exit state counts exactly ten ticks'
end if

; leg 7 premises: the LAPIC page shares the heap PDPT entry and PD page
; (one new PT only), and the frozen delta stays three because irq_init
; precedes the baseline. Verified against the real boot in the smoke.
if (K64_MMIO_LAPIC_VA shr 30) <> (K64_HEAP_BASE shr 30)
        err 'the LAPIC page must share the heap PDPT entry'
end if
if (K64_MMIO_LAPIC_VA shr 21) = (K64_HEAP_BASE shr 21)
        err 'the LAPIC page must own a page table of its own'
end if
if K64_CORE_ACCOUNT_TABLES <> 3
        err 'the leg7 accounting expectation is frozen at three tables'
end if

; ------------------------------------------------------------------
; GUI-G1: the shared K64_VIDEO_* spelling of the video layer contract
; and the three copy comparison against the private VIDEO_* values.
; Appended by task 78; every assertion above stays untouched. Pure
; constant math, zero bytes emitted.
;
; The GUI stage runs AFTER core_selftest, so the framebuffer page
; tables never enter the leg 7 baseline and K64_CORE_ACCOUNT_TABLES
; stays frozen at three.
; ------------------------------------------------------------------

K64_VIDEO_FB_VA        = 0xFFFFC00001000000
K64_VIDEO_FB_VA_LIMIT  = 0xFFFFC00002000000
K64_VIDEO_FB_MAX_SIZE  = 0x1000000
K64_VIDEO_FB_MAX_PAGES = 4096
K64_VIDEO_MAX_WIDTH    = 2560
K64_VIDEO_MAX_HEIGHT   = 1600
K64_VIDEO_BPP          = 4
K64_VIDEO_FMT_BGRX     = 1
K64_VIDEO_FONT_STRIDE  = 16
K64_VIDEO_FONT_BYTES   = 1520
K64_VIDEO_ST_WIDTH     = 1024
K64_VIDEO_ST_HEIGHT    = 768
K64_VIDEO_ST_BARS      = 8
K64_VIDEO_ST_BAR_W     = 128
K64_VIDEO_ST_RECT_X    = 412
K64_VIDEO_ST_RECT_Y    = 284
K64_VIDEO_ST_RECT_W    = 200
K64_VIDEO_ST_RECT_H    = 200
K64_VIDEO_ST_RECT_COLOR = 0x808080
K64_VIDEO_ST_TEXT_X    = 8
K64_VIDEO_ST_TEXT_Y    = 8
K64_VIDEO_ST_TEXT_LEN  = 10

if K64_VIDEO_FB_VA <> VIDEO_FB_VA
        err 'FB VA disagrees with the video copy'
end if
if K64_VIDEO_FB_VA_LIMIT <> VIDEO_FB_VA_LIMIT
        err 'FB VA limit disagrees with the video copy'
end if
if K64_VIDEO_FB_MAX_SIZE <> VIDEO_FB_MAX_SIZE | K64_VIDEO_FB_MAX_PAGES <> VIDEO_FB_MAX_PAGES
        err 'FB window size/page count disagree with the video copy'
end if
if K64_VIDEO_MAX_WIDTH <> VIDEO_MAX_WIDTH | K64_VIDEO_MAX_HEIGHT <> VIDEO_MAX_HEIGHT
        err 'maximum geometry disagrees with the video copy'
end if
if K64_VIDEO_BPP <> VIDEO_BPP | K64_VIDEO_FMT_BGRX <> VIDEO_FMT_BGRX
        err 'pixel depth/format disagree with the video copy'
end if
if K64_VIDEO_FONT_STRIDE <> VIDEO_FONT_STRIDE | K64_VIDEO_FONT_BYTES <> VIDEO_FONT_BYTES
        err 'font stride/size disagree with the video copy'
end if
if K64_VIDEO_ST_WIDTH <> VIDEO_ST_WIDTH | K64_VIDEO_ST_HEIGHT <> VIDEO_ST_HEIGHT
        err 'pattern geometry disagrees with the video copy'
end if
if K64_VIDEO_ST_BARS <> VIDEO_ST_BARS | K64_VIDEO_ST_BAR_W <> VIDEO_ST_BAR_W
        err 'pattern bar layout disagrees with the video copy'
end if
if K64_VIDEO_ST_RECT_X <> VIDEO_ST_RECT_X | K64_VIDEO_ST_RECT_Y <> VIDEO_ST_RECT_Y
        err 'pattern rectangle origin disagrees with the video copy'
end if
if K64_VIDEO_ST_RECT_W <> VIDEO_ST_RECT_W | K64_VIDEO_ST_RECT_H <> VIDEO_ST_RECT_H
        err 'pattern rectangle size disagrees with the video copy'
end if
if K64_VIDEO_ST_RECT_COLOR <> VIDEO_ST_RECT_COLOR
        err 'pattern rectangle color disagrees with the video copy'
end if
if K64_VIDEO_ST_TEXT_X <> VIDEO_ST_TEXT_X | K64_VIDEO_ST_TEXT_Y <> VIDEO_ST_TEXT_Y
        err 'pattern text origin disagrees with the video copy'
end if
if K64_VIDEO_ST_TEXT_LEN <> VIDEO_ST_TEXT_LEN
        err 'pattern text length disagrees with the video copy'
end if

; the video status codes are the shared ones, a vmm_map_mmio failure
; propagates through video_init without translation
if K64_OK <> VIDEO_OK | K64_EINVAL <> VIDEO_EINVAL | K64_ENOTSUP <> VIDEO_ENOTSUP
        err 'video status codes disagree with the shared copy'
end if
if K64_ENOTREADY <> VIDEO_ENOTREADY | K64_EALREADY <> VIDEO_EALREADY | K64_EIO <> VIDEO_EIO
        err 'video status codes disagree with the shared copy'
end if

; the video private BootInfo copy must describe the same record and the
; same framebuffer feature flag the loader publishes
if VIDEO_BOOTINFO_SIZE <> sizeof.K64BOOTINFO
        err 'BootInfo size disagrees with the video copy'
end if
if VIDEO_FLAG_FRAMEBUFFER <> K64BOOTINFO_FLAG_FRAMEBUFFER
        err 'FRAMEBUFFER flag disagrees with the video copy'
end if
if VIDEO_BOOTINFO.flags <> K64BOOTINFO.flags
        err 'BootInfo.flags offset disagrees with the video copy'
end if
if VIDEO_BOOTINFO.gop_framebuffer_addr <> K64BOOTINFO.gop_framebuffer_addr | VIDEO_BOOTINFO.gop_framebuffer_size <> K64BOOTINFO.gop_framebuffer_size
        err 'GOP address/size offsets disagree with the video copy'
end if
if VIDEO_BOOTINFO.gop_width <> K64BOOTINFO.gop_width | VIDEO_BOOTINFO.gop_height <> K64BOOTINFO.gop_height
        err 'GOP geometry offsets disagree with the video copy'
end if
if VIDEO_BOOTINFO.gop_pixel_format <> K64BOOTINFO.gop_pixel_format | VIDEO_BOOTINFO.gop_pixels_per_line <> K64BOOTINFO.gop_pixels_per_line
        err 'GOP format/pitch offsets disagree with the video copy'
end if

; FB window placement premises: above the heap, inside the public
; window, under the window base PML4 slot, never under the core test
; slot, and never reaching into the LAPIC MMIO page of the IRQ layer
if K64_VIDEO_FB_VA <> K64_HEAP_LIMIT
        err 'the FB window must start exactly at the heap limit'
end if
if K64_VIDEO_FB_VA < K64_VMM_WINDOW_BASE | K64_VIDEO_FB_VA_LIMIT > K64_VMM_WINDOW_LIMIT
        err 'the FB window must stay inside the mapping window'
end if
if K64_VIDEO_FB_VA mod K64_PAGE_SIZE
        err 'the FB window must be page aligned'
end if
if ((K64_VIDEO_FB_VA shr 39) and 0x1FF) <> ((K64_VMM_WINDOW_BASE shr 39) and 0x1FF)
        err 'the FB window must share the window base PML4 slot'
end if
if ((K64_VIDEO_FB_VA shr 39) and 0x1FF) = K64_CORE_TEST_SLOT
        err 'the FB window must not use the core test PML4 slot'
end if
if K64_VIDEO_FB_VA >= K64_MMIO_LAPIC_VA
        err 'the FB window must start below the LAPIC MMIO VA'
end if
if K64_VIDEO_FB_VA + K64_VIDEO_FB_MAX_SIZE > K64_MMIO_LAPIC_VA
        err 'the FB window must never reach the LAPIC MMIO VA'
end if
if K64_VIDEO_FB_VA_LIMIT <> K64_MMIO_LAPIC_VA
        err 'the FB window limit is the LAPIC MMIO VA'
end if
if K64_VIDEO_MAX_WIDTH * K64_VIDEO_MAX_HEIGHT * K64_VIDEO_BPP > K64_VIDEO_FB_MAX_SIZE
        err 'the maximum geometry must fit the FB window'
end if

; the frozen pattern relations the screendump forensics rely on
if K64_VIDEO_ST_WIDTH <> K64_VIDEO_ST_BARS * K64_VIDEO_ST_BAR_W
        err 'the bars must tile the reference width exactly'
end if
if K64_VIDEO_ST_RECT_X <> (K64_VIDEO_ST_WIDTH - K64_VIDEO_ST_RECT_W) / 2 | K64_VIDEO_ST_RECT_Y <> (K64_VIDEO_ST_HEIGHT - K64_VIDEO_ST_RECT_H) / 2
        err 'the pattern rectangle must stay centered'
end if
