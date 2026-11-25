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

; ------------------------------------------------------------------
; IRQ-v2: the shared K64_* spelling of the input layer contract and
; the three copy comparison against the private IRQ_*/INPUT_*/MM_*
; values. Appended by task 89; every assertion above stays untouched.
; Pure constant math, zero bytes emitted.
;
; The input stage runs AFTER core_selftest (like the GUI stage), so
; the IOAPIC page table never enters the leg 7 baseline and
; K64_CORE_ACCOUNT_TABLES stays frozen at three.
;
; Handler contract: irq_dispatch calls input_keyboard_irq /
; input_mouse_irq with RCX = frame; both are leaves (IF = 0, caller
; saved registers only, no heap, no serial, no unbounded wait) and
; never write the LAPIC EOI: the dispatch .input_eoi site owns it.
; The input module contains zero STI instructions; its self test
; windows go through the exported irq_sti_wait.
; ------------------------------------------------------------------

K64_IRQ_VEC_KEYBOARD = 0x21
K64_IRQ_VEC_MOUSE    = 0x2C
K64_MMIO_IOAPIC_VA   = 0xFFFFC00003000000
K64_IOAPIC_PA        = 0xFEC00000

K64_INPUT_RTE_KBD_MASKED   = 0x10021
K64_INPUT_RTE_MOUSE_MASKED = 0x1002C
K64_INPUT_IOAPIC_IDX_KBD_LO   = 0x10 + 2 * 1
K64_INPUT_IOAPIC_IDX_KBD_HI   = 0x10 + 2 * 1 + 1
K64_INPUT_IOAPIC_IDX_MOUSE_LO = 0x10 + 2 * 12
K64_INPUT_IOAPIC_IDX_MOUSE_HI = 0x10 + 2 * 12 + 1
K64_INPUT_CCB_SET          = 0x43
K64_INPUT_CCB_CLEAR        = 0x30
K64_INPUT_KBD_CAP          = 120
K64_INPUT_ACK_RETRY        = 5
K64_INPUT_STAGE_DONE       = 6
K64_INPUT_ST_KBD_FINAL     = 2
K64_INPUT_ST_MOUSE_FINAL   = 3
; ticks final is DETERMINISTIC, not observed: the IRQ-v1 exit value 10
; plus exactly one tick of the masked-oneshot leg. Under either QEMU
; masked-expiry behavior the count is exact: (a) expiry discarded ->
; unmask + re-arm delivers one tick; (b) expiry latched in IRR -> the
; unmask delivers the phantom as tick 11, the mode-0 wait exits within
; a few instructions and masks the LVT long before the re-armed 10 ms
; countdown could deliver a 12th. kbd = 2 is the make+break of sendkey
; a (sendkey b is fully drained under the mask); mouse = 3 is move +
; press + release with the release gated on mouse_count >= 2 so QEMU
; cannot coalesce press+release.
K64_INPUT_TICKS_BASE       = 10
K64_INPUT_TICKS_FINAL      = 11

; the mouse packet facts and the DERIVED move byte 1: sync plus the
; sign bit of every negative delta (QEMU sets bit 5 for the negative
; PS/2 Y, measured 0x28 for mouse_move 5 3 in the task 89 experiment)
K64_INPUT_MOUSE_SYNC   = 0x08
K64_INPUT_MOUSE_SIGN_X = 0x10
K64_INPUT_MOUSE_SIGN_Y = 0x20
K64_INPUT_ST_MOUSE_DX  = 5
K64_INPUT_ST_MOUSE_DY  = -3
K64_INPUT_ST_MOUSE_B1 = K64_INPUT_MOUSE_SYNC
if K64_INPUT_ST_MOUSE_DX < 0
        K64_INPUT_ST_MOUSE_B1 = K64_INPUT_ST_MOUSE_B1 or K64_INPUT_MOUSE_SIGN_X
end if
if K64_INPUT_ST_MOUSE_DY < 0
        K64_INPUT_ST_MOUSE_B1 = K64_INPUT_ST_MOUSE_B1 or K64_INPUT_MOUSE_SIGN_Y
end if

; frozen release-packet verdict semantics: the last-packet assertion
; masks the two byte-1 sign bits out (0xFFFFCF keeps sync/buttons and
; both delta bytes exact). A sign bit on a zero delta carries no
; information and must not fail the leg; the mask must never be
; widened, dropping sync/button/delta bits would weaken the gate.
K64_INPUT_MOUSE_B1_MASK = 0xFF and not (K64_INPUT_MOUSE_SIGN_X or K64_INPUT_MOUSE_SIGN_Y)
K64_INPUT_ST_MOUSE_LAST_MASK = (0xFF shl 16) or (0xFF shl 8) or K64_INPUT_MOUSE_B1_MASK

if K64_IRQ_VEC_KEYBOARD <> IRQ_VEC_KEYBOARD | K64_IRQ_VEC_KEYBOARD <> INPUT_VEC_KEYBOARD
        err 'keyboard vector disagrees with the IRQ/INPUT copies'
end if
if K64_IRQ_VEC_MOUSE <> IRQ_VEC_MOUSE | K64_IRQ_VEC_MOUSE <> INPUT_VEC_MOUSE
        err 'mouse vector disagrees with the IRQ/INPUT copies'
end if
if K64_MMIO_IOAPIC_VA <> MM_MMIO_IOAPIC_VA | K64_MMIO_IOAPIC_VA <> INPUT_IOAPIC_VA
        err 'IOAPIC MMIO VA disagrees with the MM/INPUT copies'
end if
if K64_IOAPIC_PA <> INPUT_IOAPIC_PA
        err 'IOAPIC physical base disagrees with the INPUT copy'
end if
if K64_INPUT_RTE_KBD_MASKED <> INPUT_RTE_KBD_MASKED | K64_INPUT_RTE_MOUSE_MASKED <> INPUT_RTE_MOUSE_MASKED
        err 'masked RTE values disagree with the INPUT copy'
end if
if K64_INPUT_RTE_KBD_MASKED <> (K64_IRQ_LVT_MASK or K64_IRQ_VEC_KEYBOARD)
        err 'the keyboard RTE is the shared mask bit plus the frozen vector'
end if
if K64_INPUT_RTE_MOUSE_MASKED <> (K64_IRQ_LVT_MASK or K64_IRQ_VEC_MOUSE)
        err 'the mouse RTE is the shared mask bit plus the frozen vector'
end if
if K64_INPUT_IOAPIC_IDX_KBD_LO <> INPUT_IOAPIC_IDX_KBD_LO | K64_INPUT_IOAPIC_IDX_KBD_HI <> INPUT_IOAPIC_IDX_KBD_HI
        err 'pin 1 RTE indices disagree with the INPUT copy'
end if
if K64_INPUT_IOAPIC_IDX_MOUSE_LO <> INPUT_IOAPIC_IDX_MOUSE_LO | K64_INPUT_IOAPIC_IDX_MOUSE_HI <> INPUT_IOAPIC_IDX_MOUSE_HI
        err 'pin 12 RTE indices disagree with the INPUT copy'
end if
if K64_INPUT_IOAPIC_IDX_KBD_LO <> 0x12 | K64_INPUT_IOAPIC_IDX_MOUSE_LO <> 0x28
        err 'the RTE indices derive from the pins: 0x10+2*1 and 0x10+2*12'
end if
if K64_INPUT_CCB_SET <> INPUT_CCB_SET | K64_INPUT_CCB_CLEAR <> INPUT_CCB_CLEAR
        err 'CCB bring-up bits disagree with the INPUT copy'
end if
if K64_INPUT_KBD_CAP <> INPUT_KBD_CAP | K64_INPUT_ACK_RETRY <> INPUT_ACK_RETRY
        err 'buffer cap / resend cap disagree with the INPUT copy'
end if
if K64_INPUT_STAGE_DONE <> INPUT_STAGE_DONE
        err 'handshake end stage disagrees with the INPUT copy'
end if
if K64_INPUT_ST_KBD_FINAL <> INPUT_ST_KBD_FINAL | K64_INPUT_ST_MOUSE_FINAL <> INPUT_ST_MOUSE_FINAL
        err 'frozen final counts disagree with the INPUT copy'
end if
if K64_INPUT_MOUSE_SYNC <> INPUT_MOUSE_SYNC | K64_INPUT_MOUSE_SIGN_X <> INPUT_MOUSE_SIGN_X | K64_INPUT_MOUSE_SIGN_Y <> INPUT_MOUSE_SIGN_Y
        err 'mouse packet bit facts disagree with the INPUT copy'
end if
if K64_INPUT_ST_MOUSE_DX <> INPUT_ST_MOUSE_DX | K64_INPUT_ST_MOUSE_DY <> INPUT_ST_MOUSE_DY
        err 'the frozen injection deltas disagree with the INPUT copy'
end if
if K64_INPUT_ST_MOUSE_B1 <> INPUT_ST_MOUSE_B1
        err 'the derived move byte 1 disagrees with the INPUT copy'
end if
if K64_INPUT_ST_MOUSE_B1 <> 0x28
        err 'the derived move byte 1 must match the measured QEMU packet'
end if
if K64_INPUT_MOUSE_B1_MASK <> INPUT_MOUSE_B1_MASK | K64_INPUT_ST_MOUSE_LAST_MASK <> INPUT_ST_MOUSE_LAST_MASK
        err 'the release packet masks disagree with the INPUT copy'
end if
if K64_INPUT_MOUSE_B1_MASK <> 0xCF | K64_INPUT_ST_MOUSE_LAST_MASK <> 0xFFFFCF
        err 'the release mask keeps sync/buttons/deltas, drops only the sign bits'
end if
if K64_INPUT_TICKS_BASE <> IRQ_ST_FINAL_N | K64_INPUT_TICKS_BASE <> INPUT_IRQ_TICKS_BASE
        err 'the input timer leg starts from the frozen IRQ-v1 exit count'
end if
if K64_INPUT_TICKS_FINAL <> INPUT_IRQ_TICKS_FINAL | K64_INPUT_TICKS_FINAL <> K64_INPUT_TICKS_BASE + 1
        err 'the input timer leg adds exactly one tick'
end if

; the input status codes are the shared ones, a vmm_map_mmio failure
; propagates through input_init without translation
if K64_OK <> INPUT_OK | K64_EINVAL <> INPUT_EINVAL | K64_EPERM <> INPUT_EPERM
        err 'input status codes disagree with the shared copy'
end if
if K64_EALREADY <> INPUT_EALREADY | K64_EIO <> INPUT_EIO | K64_ENOTSUP <> INPUT_ENOTSUP
        err 'input status codes disagree with the shared copy'
end if
if INPUT_PAGE_SIZE <> K64_PAGE_SIZE | INPUT_DATA_ALIGN <> K64_DATA_ALIGN
        err 'input geometry disagrees with the shared copy'
end if
if INPUT_PUTS_MAX <> CPU_PUTS_MAX | INPUT_PUTHEX_DIGITS <> CPU_PUTHEX_DIGITS
        err 'input console caps disagree with the CPU copy'
end if
if INPUT_VEC_KEYBOARD < CPU_IRQ_VECTOR_MIN | INPUT_VEC_MOUSE < CPU_IRQ_VECTOR_MIN
        err 'the input vectors must sit inside the CPU asynchronous window'
end if
if INPUT_LAPIC_VA <> K64_MMIO_LAPIC_VA | INPUT_LAPIC_LVT_TIMER <> K64_LAPIC_LVT_TIMER
        err 'the input private LAPIC copies disagree with the shared contract'
end if
if INPUT_LAPIC_INIT_COUNT <> K64_LAPIC_INIT_COUNT | INPUT_LAPIC_DIVIDE <> K64_LAPIC_DIVIDE
        err 'the input private timer count/divide copies disagree'
end if
if INPUT_LVT_MASK <> K64_IRQ_LVT_MASK | INPUT_DIVIDE_16 <> K64_IRQ_DIVIDE_16
        err 'the input private mask/divide copies disagree'
end if
if INPUT_VEC_TIMER <> K64_IRQ_VEC_TIMER | INPUT_TICK_HZ <> IRQ_TICK_HZ
        err 'the input private timer vector/tick copies disagree'
end if

; the counter layout the gdbstub smoke reads: the input mask, the two
; event counters and the handshake stage are qwords at frozen offsets
K64_INPUT_DATA_OFF_TEST_MASK  = 8
K64_INPUT_DATA_OFF_TEST_KBD   = 16
K64_INPUT_DATA_OFF_TEST_MOUSE = 24
K64_INPUT_DATA_OFF_TEST_STAGE = 32

if K64_INPUT_DATA_OFF_TEST_MASK <> INPUT_DATA_OFF_TEST_MASK
        err 'input selftest mask offset disagrees with the INPUT copy'
end if
if K64_INPUT_DATA_OFF_TEST_KBD <> INPUT_DATA_OFF_TEST_KBD
        err 'input keyboard count offset disagrees with the INPUT copy'
end if
if K64_INPUT_DATA_OFF_TEST_MOUSE <> INPUT_DATA_OFF_TEST_MOUSE
        err 'input mouse count offset disagrees with the INPUT copy'
end if
if K64_INPUT_DATA_OFF_TEST_STAGE <> INPUT_DATA_OFF_TEST_STAGE
        err 'input handshake stage offset disagrees with the INPUT copy'
end if

; IOAPIC window placement premises: above the heap, 2 MiB aligned,
; under the window base PML4 slot, never under the core test slot,
; strictly above the LAPIC page (16 MiB above it, clear of the whole
; FB window whose limit is the LAPIC VA); it shares the heap PDPT
; entry but owns a page table of its own (one new PT at input_init
; time, after the leg 7 baseline, so the frozen delta stays three)
if K64_MMIO_IOAPIC_VA < K64_VMM_WINDOW_BASE | K64_MMIO_IOAPIC_VA >= K64_VMM_WINDOW_LIMIT
        err 'the IOAPIC MMIO VA must stay inside the mapping window'
end if
if K64_MMIO_IOAPIC_VA < K64_HEAP_LIMIT
        err 'the IOAPIC MMIO VA must sit above the heap limit'
end if
if K64_MMIO_IOAPIC_VA mod K64_LARGE_PAGE_SIZE
        err 'the IOAPIC MMIO VA must be 2 MiB aligned'
end if
if ((K64_MMIO_IOAPIC_VA shr 39) and 0x1FF) <> ((K64_VMM_WINDOW_BASE shr 39) and 0x1FF)
        err 'the IOAPIC MMIO VA must share the window base PML4 slot'
end if
if ((K64_MMIO_IOAPIC_VA shr 39) and 0x1FF) = K64_CORE_TEST_SLOT
        err 'the IOAPIC MMIO VA must not use the core test PML4 slot'
end if
if K64_MMIO_IOAPIC_VA <= K64_MMIO_LAPIC_VA
        err 'the IOAPIC page must sit strictly above the LAPIC page'
end if
if (K64_MMIO_IOAPIC_VA shr 30) <> (K64_HEAP_BASE shr 30)
        err 'the IOAPIC page must share the heap PDPT entry'
end if
if (K64_MMIO_IOAPIC_VA shr 21) = (K64_HEAP_BASE shr 21) | (K64_MMIO_IOAPIC_VA shr 21) = (K64_MMIO_LAPIC_VA shr 21)
        err 'the IOAPIC page must own a page table of its own'
end if
if K64_CORE_ACCOUNT_TABLES <> 3
        err 'the leg7 accounting stays frozen at three tables'
end if

; ------------------------------------------------------------------
; DESKTOP-v1: the shared K64_* spelling of the minimal shell contract
; and the three copy comparison against the private DESKTOP_* values.
; Appended by task 96; every assertion above stays untouched.
; Pure constant math, zero bytes emitted.
;;
; The desktop stage runs AFTER the GUI stage (and core_selftest): the
; module maps no MMIO of its own, it reuses the video FB window, the
; IRQ LAPIC page and the input IOAPIC page; the sprite constant and the
; save/draw buffers live in the code/data regions, so there is no page
; table delta and K64_CORE_ACCOUNT_TABLES stays frozen at three.
;
; STI ownership, frozen: desktop_run is the SECOND legal STI owner of
; the whole kernel (the first is the irq_sti_wait self test window).
; The desktop module contains exactly one STI, in the resident loop,
; always preceded by the LAPIC LVT timer mask: irq_test_ticks stays
; frozen at 11. The handlers still run with IF = 0 and only write
; buffers/counters; every redraw happens in the main loop.
;
; Readback contract: video_readback_rect is the ONLY sanctioned
; framebuffer read path of the kernel, used exclusively by the cursor
; background save. MS x64 mirror of video_blit_rect: RCX = destination
; kernel buffer, EDX = x, R8D = y, R9D = w, [rsp+40] = h; clipped to the
; FB geometry, source rows advance by the FB stride, destination rows
; are packed at the ORIGINAL w * 4. The video self test stays
; draw-only; the desktop pixel evidence is the external screendump.
; ------------------------------------------------------------------

; the frozen desktop frame
K64_DESKTOP_BG_COLOR    = 0x1A2B3C
K64_DESKTOP_BAR_COLOR   = 0x2C3E50
K64_DESKTOP_BAR_X       = 0
K64_DESKTOP_BAR_Y       = 0
K64_DESKTOP_BAR_W       = 1024
K64_DESKTOP_BAR_H       = 32
K64_DESKTOP_TITLE_X     = 8
K64_DESKTOP_TITLE_Y     = 8
K64_DESKTOP_TITLE_COLOR = 0xFFFFFF
K64_DESKTOP_TITLE_LEN   = 16            ; "K64 Desktop v0.1"

; the frozen 12x18 arrow sprite, one byte per pixel
K64_DESKTOP_SPRITE_W     = 12
K64_DESKTOP_SPRITE_H     = 18
K64_DESKTOP_SPRITE_BYTES = K64_DESKTOP_SPRITE_W * K64_DESKTOP_SPRITE_H

; the initial cursor position DERIVES from the input self test frozen
; exit coordinates (512+5, 384-(-3)); the final position derives from
; the injected desktop move (40,24) on top of it, never magic numbers
K64_DESKTOP_MOUSE_XINIT = 512
K64_DESKTOP_MOUSE_YINIT = 384
K64_DESKTOP_CURSOR_X0 = K64_DESKTOP_MOUSE_XINIT + K64_INPUT_ST_MOUSE_DX
K64_DESKTOP_CURSOR_Y0 = K64_DESKTOP_MOUSE_YINIT - K64_INPUT_ST_MOUSE_DY
K64_DESKTOP_CURSOR_XMAX = K64_VIDEO_ST_WIDTH - K64_DESKTOP_SPRITE_W
K64_DESKTOP_CURSOR_YMAX = K64_VIDEO_ST_HEIGHT - K64_DESKTOP_SPRITE_H
K64_DESKTOP_ST_MOVE_DX = 40
K64_DESKTOP_ST_MOVE_DY = 24             ; monitor dy, screen space
K64_DESKTOP_ST_CURSOR_X = K64_DESKTOP_CURSOR_X0 + K64_DESKTOP_ST_MOVE_DX
K64_DESKTOP_ST_CURSOR_Y = K64_DESKTOP_CURSOR_Y0 + K64_DESKTOP_ST_MOVE_DY

; the frozen echo text area: (16,64), 126 columns x 44 rows of 8x16
K64_DESKTOP_ECHO_X    = 16
K64_DESKTOP_ECHO_Y    = 64
K64_DESKTOP_ECHO_COLS = 126
K64_DESKTOP_ECHO_ROWS = 44

; frozen final counts, DETERMINISTICALLY derived from the WHOLE
; injection chain: the input exit values plus the desktop sequence
; (sendkey h, sendkey i = 4 kbd bytes; move + press + release = 3
; mouse packets; 2 echoed printable chars). Ticks stay at the input
; exit value: the resident loop masks the LVT timer BEFORE its STI.
K64_DESKTOP_ST_KBD_FINAL      = K64_INPUT_ST_KBD_FINAL + 4
K64_DESKTOP_ST_MOUSE_FINAL    = K64_INPUT_ST_MOUSE_FINAL + 3
K64_DESKTOP_ST_KEYS_FINAL     = 2
K64_DESKTOP_ST_KBD_USED_FINAL = K64_INPUT_ST_KBD_FINAL + 4
K64_DESKTOP_TICKS_FINAL       = K64_INPUT_TICKS_FINAL
K64_DESKTOP_STAGE_DONE        = 5

; the counter layout the gdbstub smoke reads: qwords at frozen offsets
K64_DESKTOP_DATA_OFF_TEST_MASK   = 8
K64_DESKTOP_DATA_OFF_TEST_STAGE  = 16
K64_DESKTOP_DATA_OFF_TEST_KEYS   = 24
K64_DESKTOP_DATA_OFF_TEST_CURSOR_X = 32
K64_DESKTOP_DATA_OFF_TEST_CURSOR_Y = 40

; the GUI frame forensic handshake, task 102: after K64-GUI-OK and
; before desktop_init repaints, the foundation publishes READY in the
; desktop data slot and spins a bounded pause window until the host
; screendumps screen-gui.ppm and writes RELEASE; a timeout is EIO and
; takes the standard fatal path, never a hang, never a new sti
K64_GUI_FRAME_OFF_STAGE   = 104
K64_GUI_FRAME_READY       = 1
K64_GUI_FRAME_RELEASE     = 2
K64_GUI_FRAME_DONE        = 3
K64_GUI_FRAME_POLL_CHUNK  = 0x2000000
K64_GUI_FRAME_POLL_ROUNDS = 64

; ---------------- three copy comparison ----------------

if K64_DESKTOP_BG_COLOR <> DESKTOP_BG_COLOR | K64_DESKTOP_BAR_COLOR <> DESKTOP_BAR_COLOR
        err 'the desktop colors disagree with the DESKTOP copy'
end if
if K64_DESKTOP_BAR_X <> DESKTOP_BAR_X | K64_DESKTOP_BAR_Y <> DESKTOP_BAR_Y | K64_DESKTOP_BAR_W <> DESKTOP_BAR_W | K64_DESKTOP_BAR_H <> DESKTOP_BAR_H
        err 'the title bar rectangle disagrees with the DESKTOP copy'
end if
if K64_DESKTOP_TITLE_X <> DESKTOP_TITLE_X | K64_DESKTOP_TITLE_Y <> DESKTOP_TITLE_Y | K64_DESKTOP_TITLE_COLOR <> DESKTOP_TITLE_COLOR | K64_DESKTOP_TITLE_LEN <> DESKTOP_TITLE_LEN
        err 'the title text spec disagrees with the DESKTOP copy'
end if
if K64_DESKTOP_SPRITE_W <> DESKTOP_SPRITE_W | K64_DESKTOP_SPRITE_H <> DESKTOP_SPRITE_H | K64_DESKTOP_SPRITE_BYTES <> DESKTOP_SPRITE_BYTES
        err 'the cursor sprite spec disagrees with the DESKTOP copy'
end if
if K64_DESKTOP_SPRITE_BYTES <> 216
        err 'the cursor sprite is the frozen 12x18 arrow'
end if
if K64_DESKTOP_MOUSE_XINIT <> DESKTOP_MOUSE_XINIT | K64_DESKTOP_MOUSE_YINIT <> DESKTOP_MOUSE_YINIT
        err 'the mouse init coordinates disagree with the DESKTOP copy'
end if
if K64_DESKTOP_CURSOR_X0 <> DESKTOP_CURSOR_X0 | K64_DESKTOP_CURSOR_Y0 <> DESKTOP_CURSOR_Y0
        err 'the initial cursor position disagrees with the DESKTOP copy'
end if
if K64_DESKTOP_CURSOR_X0 <> 517 | K64_DESKTOP_CURSOR_Y0 <> 387
        err 'the initial cursor position derives from the input exit coordinates'
end if
if K64_DESKTOP_CURSOR_XMAX <> DESKTOP_CURSOR_XMAX | K64_DESKTOP_CURSOR_YMAX <> DESKTOP_CURSOR_YMAX
        err 'the hotspot clamp disagrees with the DESKTOP copy'
end if
if K64_DESKTOP_ST_MOVE_DX <> DESKTOP_ST_MOVE_DX | K64_DESKTOP_ST_MOVE_DY <> DESKTOP_ST_MOVE_DY
        err 'the frozen desktop injection deltas disagree with the DESKTOP copy'
end if
if K64_DESKTOP_ST_CURSOR_X <> DESKTOP_ST_CURSOR_X | K64_DESKTOP_ST_CURSOR_Y <> DESKTOP_ST_CURSOR_Y
        err 'the final cursor position disagrees with the DESKTOP copy'
end if
if K64_DESKTOP_ST_CURSOR_X <> 557 | K64_DESKTOP_ST_CURSOR_Y <> 411
        err 'the final cursor position derives from the injected move'
end if
if K64_DESKTOP_ST_CURSOR_X + K64_DESKTOP_SPRITE_W > K64_VIDEO_ST_WIDTH | K64_DESKTOP_ST_CURSOR_Y + K64_DESKTOP_SPRITE_H > K64_VIDEO_ST_HEIGHT
        err 'the final sprite must stay fully on screen'
end if
if K64_DESKTOP_ECHO_X <> DESKTOP_ECHO_X | K64_DESKTOP_ECHO_Y <> DESKTOP_ECHO_Y | K64_DESKTOP_ECHO_COLS <> DESKTOP_ECHO_COLS | K64_DESKTOP_ECHO_ROWS <> DESKTOP_ECHO_ROWS
        err 'the echo text area disagrees with the DESKTOP copy'
end if
if K64_DESKTOP_ECHO_X + K64_DESKTOP_ECHO_COLS * DESKTOP_FONT_W <> K64_VIDEO_ST_WIDTH
        err 'the echo columns must tile the remaining width exactly'
end if
if K64_DESKTOP_ECHO_Y + K64_DESKTOP_ECHO_ROWS * DESKTOP_FONT_H <> K64_VIDEO_ST_HEIGHT
        err 'the echo rows must tile the remaining height exactly'
end if
if K64_DESKTOP_ST_KBD_FINAL <> DESKTOP_ST_KBD_FINAL | K64_DESKTOP_ST_MOUSE_FINAL <> DESKTOP_ST_MOUSE_FINAL | K64_DESKTOP_ST_KEYS_FINAL <> DESKTOP_ST_KEYS_FINAL
        err 'the frozen final counts disagree with the DESKTOP copy'
end if
if K64_DESKTOP_ST_KBD_USED_FINAL <> DESKTOP_ST_KBD_USED_FINAL
        err 'the frozen buffer fill level disagrees with the DESKTOP copy'
end if
if K64_DESKTOP_ST_KBD_FINAL <> 6 | K64_DESKTOP_ST_MOUSE_FINAL <> 6 | K64_DESKTOP_ST_KEYS_FINAL <> 2
        err 'the final counts derive from the whole injection chain'
end if
if K64_DESKTOP_TICKS_FINAL <> DESKTOP_IRQ_TICKS_FINAL | K64_DESKTOP_TICKS_FINAL <> 11
        err 'the resident loop masks the timer, ticks stay frozen at 11'
end if
if K64_DESKTOP_STAGE_DONE <> DESKTOP_STAGE_DONE
        err 'the desktop handshake end stage disagrees with the DESKTOP copy'
end if
if K64_DESKTOP_DATA_OFF_TEST_MASK <> DESKTOP_DATA_OFF_TEST_MASK | K64_DESKTOP_DATA_OFF_TEST_STAGE <> DESKTOP_DATA_OFF_TEST_STAGE
        err 'the desktop mask/stage offsets disagree with the DESKTOP copy'
end if
if K64_DESKTOP_DATA_OFF_TEST_KEYS <> DESKTOP_DATA_OFF_TEST_KEYS | K64_DESKTOP_DATA_OFF_TEST_CURSOR_X <> DESKTOP_DATA_OFF_TEST_CURSOR_X | K64_DESKTOP_DATA_OFF_TEST_CURSOR_Y <> DESKTOP_DATA_OFF_TEST_CURSOR_Y
        err 'the desktop keys/cursor offsets disagree with the DESKTOP copy'
end if
if K64_GUI_FRAME_OFF_STAGE <> DESKTOP_DATA_OFF_GUI_FRAME
        err 'the GUI frame handshake slot disagrees with the DESKTOP copy'
end if
if K64_GUI_FRAME_POLL_CHUNK <> DESKTOP_POLL_CHUNK | K64_GUI_FRAME_POLL_ROUNDS <> DESKTOP_POLL_ROUNDS
        err 'the GUI frame wait budget matches the frozen injection sizing'
end if
if K64_GUI_FRAME_READY <> 1 | K64_GUI_FRAME_RELEASE <> 2 | K64_GUI_FRAME_DONE <> 3
        err 'the GUI frame stages are the frozen 1/2/3 sequence'
end if

; the private IRQ/INPUT copies the shell consumes
if DESKTOP_LAPIC_VA <> K64_MMIO_LAPIC_VA | DESKTOP_LAPIC_LVT_TIMER <> K64_LAPIC_LVT_TIMER | DESKTOP_LVT_MASK <> K64_IRQ_LVT_MASK
        err 'the desktop private LAPIC copies disagree with the shared contract'
end if
if DESKTOP_IOAPIC_IDX_KBD_LO <> K64_INPUT_IOAPIC_IDX_KBD_LO | DESKTOP_IOAPIC_IDX_MOUSE_LO <> K64_INPUT_IOAPIC_IDX_MOUSE_LO
        err 'the desktop RTE indices disagree with the shared input contract'
end if
if DESKTOP_VEC_KEYBOARD <> K64_IRQ_VEC_KEYBOARD | DESKTOP_VEC_MOUSE <> K64_IRQ_VEC_MOUSE
        err 'the desktop vectors disagree with the shared IRQ contract'
end if
if DESKTOP_RTE_KBD_MASKED <> K64_INPUT_RTE_KBD_MASKED | DESKTOP_RTE_MOUSE_MASKED <> K64_INPUT_RTE_MOUSE_MASKED
        err 'the desktop masked RTEs disagree with the shared input contract'
end if
if DESKTOP_RTE_KBD_LIVE <> K64_IRQ_VEC_KEYBOARD | DESKTOP_RTE_MOUSE_LIVE <> K64_IRQ_VEC_MOUSE
        err 'the live RTEs are the frozen vectors'
end if
if DESKTOP_SCREEN_W <> K64_VIDEO_ST_WIDTH | DESKTOP_SCREEN_H <> K64_VIDEO_ST_HEIGHT
        err 'the desktop geometry is the frozen video reference'
end if
if DESKTOP_KBD_ENTRY <> 16 | INPUT_KBD_CAP < K64_DESKTOP_ST_KBD_USED_FINAL
        err 'the key buffer entry size/cap must hold the whole injection chain'
end if

; the desktop status codes are the shared foundation set
if K64_OK <> DESKTOP_OK | K64_EINVAL <> DESKTOP_EINVAL | K64_EPERM <> DESKTOP_EPERM
        err 'desktop status codes disagree with the shared copy'
end if
if K64_EALREADY <> DESKTOP_EALREADY | K64_EIO <> DESKTOP_EIO | K64_ENOTSUP <> DESKTOP_ENOTSUP
        err 'desktop status codes disagree with the shared copy'
end if
if DESKTOP_PAGE_SIZE <> K64_PAGE_SIZE | DESKTOP_DATA_ALIGN <> K64_DATA_ALIGN
        err 'desktop geometry disagrees with the shared copy'
end if
if DESKTOP_PUTS_MAX <> CPU_PUTS_MAX | DESKTOP_PUTHEX_DIGITS <> CPU_PUTHEX_DIGITS
        err 'desktop console caps disagree with the CPU copy'
end if

; no new page tables: the leg 7 baseline stays frozen at three
if K64_CORE_ACCOUNT_TABLES <> 3
        err 'the desktop adds no MMIO mapping, the leg7 accounting stays three'
end if
