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