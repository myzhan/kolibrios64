;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Boot contract shared by the UEFI loader (boot/bootx64.asm) and the
;; 64-bit kernel (kernel64.asm). Both sides include this file, so the
;; KERNEL64 file header and the BootInfo structure are defined once.
;;
;; Handover rules:
;;  - the kernel image (header included) is loaded contiguously and mapped
;;    at VIRT_KERNEL_BASE, so the kernel runs at link time addresses;
;;  - the kernel stack directly follows the image and is mapped the same way;
;;  - the kernel entry point is called with RCX = physical address of
;;    K64BOOTINFO, which stays readable through the loader's temporary
;;    identity map. The entry point must switch RSP to the high half stack
;;    before using it;
;;  - boot services are already gone, so the loader must not be called back.
;;

; magic stored at the very beginning of KERNEL64.BIN
KERNEL64_MAGIC = 'KERNEL64'

; header at the very beginning of KERNEL64.BIN; at run time it is also the
; first bytes mapped at VIRT_KERNEL_BASE
struct KERNEL64_HEADER
        magic              dq ? ; 'KERNEL64', checked by the loader
        entry_point_offset dq ? ; k64_entry - VIRT_KERNEL_BASE
        stack_size         dq ? ; bytes of stack the kernel asks for, page aligned
        phys_start         dq ? ; filled by the loader: physical base of the image
        phys_end           dq ? ; filled by the loader: physical end of image+stack
ends

K64BOOTINFO_SIGNATURE = 'K64BOOTI'
K64BOOTINFO_VERSION   = 1

; The loader keeps a temporary identity map of the low 4 GiB alive across the
; CR3 switch, so every pointer handed over (K64BOOTINFO itself, the memory map
; copy, the loader code that jumps into the kernel) must stay below this limit.
K64_IDENTITY_MAP_LIMIT = 0x100000000

; Feature flags. A flag may only be set when the matching data is valid,
; so version 1 of the loader sets MEMORY_MAP and leaves the rest clear.
K64BOOTINFO_FLAG_MEMORY_MAP  = 0x01
K64BOOTINFO_FLAG_FRAMEBUFFER = 0x02 ; reserved, not implemented by loader v1
K64BOOTINFO_FLAG_RSDP        = 0x04 ; reserved, not implemented by loader v1
K64BOOTINFO_FLAG_INITRAMFS   = 0x08 ; reserved, not implemented by loader v1
K64BOOTINFO_FLAG_KNOWN       = 0x0F

; Size of the UEFI EFI_MEMORY_DESCRIPTOR that memmap_addr points to:
; Type(4) Pad(4) PhysicalStart(8) VirtualStart(8) NumberOfPages(8)
; Attribute(8). The kernel only needs the stride here, the loader asserts that
; its own struct matches.
K64_MEMORY_DESCRIPTOR_SIZE = 40

; Versioned boot information, filled by the loader right before
; ExitBootServices. Unimplemented features are left as explicit zeros
; with their flag cleared, they are never faked.
; All addresses are physical unless stated otherwise.
struct K64BOOTINFO
        signature            dq ? ; K64BOOTINFO_SIGNATURE
        version              dd ? ; K64BOOTINFO_VERSION
        struct_size          dd ? ; sizeof.K64BOOTINFO, only grows with version
        flags                dq ? ; K64BOOTINFO_FLAG_*

        memmap_addr          dq ? ; physical address of the UEFI memory map copy
        memmap_size          dq ? ; size of that copy in bytes
        memmap_desc_size     dd ? ; size of a single EFI_MEMORY_DESCRIPTOR
        memmap_desc_version  dd ? ; descriptor version returned by GetMemoryMap
        memmap_desc_count    dq ? ; memmap_size / memmap_desc_size

        kernel_phys_start    dq ? ; physical base of the loaded image
        kernel_phys_end      dq ? ; physical end of image plus stack
        stack_phys_start     dq ? ; physical base of the kernel stack
        stack_phys_end       dq ? ; physical end of the kernel stack

        gop_framebuffer_addr dq ? ; zero while GOP handover is not implemented
        gop_framebuffer_size dq ? ; zero
        gop_width            dd ? ; zero
        gop_height           dd ? ; zero
        gop_pixel_format     dd ? ; zero
        gop_pixels_per_line  dd ? ; zero

        rsdp_addr            dq ? ; zero while ACPI handover is not implemented
        initramfs_addr       dq ? ; zero while initramfs loading is not implemented
        initramfs_size       dq ? ; zero

        reserved             rq 8 ; must stay zero in version 1
ends

; the layout above must stay 8-byte aligned and stable within the version
if sizeof.K64BOOTINFO mod 8
        display "K64BOOTINFO must stay 8-byte aligned", 13, 10
        err
end if
