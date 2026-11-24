;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; GDT and TSS.
;;
;; cpu_gdt_init rebuilds the 16 byte TSS descriptor from the real address of
;; cpu_tss, loads the table, reloads CS through a far return, reloads the data
;; segments, clears the FS/GS selectors and the LDTR, and finally loads TR.
;;
;; The order matters:
;;  - the far return is the only way to reload CS with a new descriptor;
;;  - FS and GS are cleared, they must not keep selectors of a table that is
;;    gone;
;;  - LTR happens after LGDT and after the segment reloads, because a task
;;    switch would otherwise use half an old table.
;; Between LGDT and the IDT being loaded there is no console output at all.
;;

; ------------------------------------------------------------------
; load the GDT and the TSS
; in
; out
; eax = 0 on success, EALREADY when the GDT is already loaded
; destroys nothing that the caller owns, keeps RBX/RBP/RSI/RDI/R12..R15
; ------------------------------------------------------------------

cpu_gdt_init:
        push    rbp
        mov     rbp, rsp
        sub     rsp, CPU_STACK_FRAME
        cmp     byte [cpu_gdt_ready], 0
        jne     .already
        ; the TSS descriptor is 16 bytes: limit 0..15, base 0..15, base 16..23,
        ; type, limit 16..19 | flags, base 24..31, base 32..63
        mov     rdx, cpu_tss
        mov     word [cpu_gdt_tss + 0], CPU_TSS_LIMIT
        mov     word [cpu_gdt_tss + 2], dx
        shr     rdx, 16
        mov     byte [cpu_gdt_tss + 4], dl
        mov     byte [cpu_gdt_tss + 5], CPU_TSS_DESC_TYPE
        mov     byte [cpu_gdt_tss + 6], 0
        shr     rdx, 8
        mov     byte [cpu_gdt_tss + 7], dl
        shr     rdx, 8
        mov     dword [cpu_gdt_tss + 8], edx
        mov     dword [cpu_gdt_tss + 12], 0
        ; load the table itself
        lgdt    [cpu_gdtr]
        ; reload CS: far return to the next instruction with the new selector
        mov     rax, .cs_reloaded
        push    qword CPU_SEL_CODE
        push    rax
        retfq
.cs_reloaded:
        ; reload the flat data segments, the old ones belong to the loader
        mov     ax, CPU_SEL_DATA
        mov     ds, ax
        mov     es, ax
        mov     ss, ax
        ; no user TLS and no LDT in this stage
        xor     eax, eax
        mov     fs, ax
        mov     gs, ax
        lldt    ax
        ; publish the TSS
        mov     ax, CPU_SEL_TSS
        ltr     ax
        mov     byte [cpu_gdt_ready], 1
        xor     eax, eax
        leave
        ret
.already:
        mov     eax, CPU_EALREADY
        leave
        ret

; ------------------------------------------------------------------
; set TSS.RSP0, the stack the CPU switches to on a ring 3 to ring 0 entry
; in
; rcx = stack top, 16 byte aligned kernel address
; out
; eax = 0, EINVAL when the address cannot be a kernel stack top
; ------------------------------------------------------------------

cpu_set_rsp0:
        push    rbp
        mov     rbp, rsp
        sub     rsp, CPU_STACK_FRAME
        test    rcx, rcx
        jz      .invalid
        test    rcx, CPU_STACK_ALIGN - 1
        jnz     .invalid
        mov     rax, rcx
        shr     rax, CPU_KERNEL_SHIFT
        cmp     rax, CPU_KERNEL_TOP
        jne     .invalid
        mov     [cpu_tss_rsp0], rcx
        xor     eax, eax
        leave
        ret
.invalid:
        mov     eax, CPU_EINVAL
        leave
        ret
