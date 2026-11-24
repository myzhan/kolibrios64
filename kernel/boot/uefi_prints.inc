
; rcx - color = fore | back
proc efi_set_text_color uses rax rdx
        mov     rax, [efi_table]
        mov     rax, [rax+EFI_SYSTEM_TABLE.ConOut]
        mov     rdx, rcx ; arg2 - color
        mov     rcx, rax ; arg1 - this
        fstcall qword [rax+EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL.SetAttribute]
        ret
endp

; rcx - null-terminated string
proc efi_puts uses rax rdx r10 ; TODO maybe other regs that are clobbered by inner calls
        mov     rax, [efi_table]
        mov     rax, [rax+EFI_SYSTEM_TABLE.ConOut]
        mov     rdx, rcx ; arg2 - string
        mov     rcx, rax ; arg1 - this
        fstcall qword [rcx+EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL.OutputString]
        ret
endp

; rcx - char
proc efi_putc uses rax rdx r10
locals
        chr     dq 0
endl
        mov     rax, [efi_table]
        mov     rax, [rax+EFI_SYSTEM_TABLE.ConOut]
        mov     byte [chr], cl
        lea     rdx, [chr] ; arg2 - string
        mov     rcx, rax   ; arg1 - this
        fstcall qword [rcx+EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL.OutputString]
        ret
endp

; print hex with fixed width of 16 digits
; rcx - number
proc efi_print_hex_fixed uses rax rbx rcx rdx r8
locals
        buf     dq 5 dup(0)
endl
        mov     rdx, rcx
        mov     rcx, 64 ; how many nibbles in qword
        xor     r8, r8
        mov     byte [buf + r8*2], '0'
        mov     byte [buf + r8*2 + 1], 0 
        inc     r8
        mov     byte [buf + r8*2], 'x'
        mov     byte [buf + r8*2 + 1], 0
        inc     r8
@@:
        sub     rcx, 4

        mov     rbx, rdx
        shr     rbx, cl
        and     rbx, 0xf

        lea     rax, [hex_codes + rbx]
        movzx   rax, byte [rax]
        mov     byte [buf + r8*2], al
        mov     byte [buf + r8*2 + 1], 0 ; set high byte to 0 bc UEFI OutputString needs CHAR16
        inc     r8

        test    rcx, rcx
        jnz     @b

        lea     rcx, [buf]
        fstcall efi_puts, rcx

        ret
endp

; print hex without leading zeros
; rcx - number
proc efi_print_hex_no_lz uses rax rbx rcx rdx r8 r10
locals
        buf     dq 5 dup(0)
endl
        xor     r10, r10 ; leading zeros end flag
        mov     rdx, rcx
        mov     rcx, 64 ; how many nibbles in qword
        xor     r8, r8
        mov     byte [buf + r8*2], '0'
        mov     byte [buf + r8*2 + 1], 0 
        inc     r8
        mov     byte [buf + r8*2], 'x'
        mov     byte [buf + r8*2 + 1], 0
        inc     r8
.lp:
        sub     rcx, 4

        mov     rbx, rdx
        shr     rbx, cl
        and     rbx, 0xf

        test    r10, r10
        jnz     @f

        test    rbx, rbx
        jz      .lp_cont
        mov     r10, 1
@@:
        lea     rax, [hex_codes + rbx]
        movzx   rax, byte [rax]
        mov     byte [buf + r8*2 ], al
        mov     byte [buf + r8*2 + 1], 0 ; set high byte to 0 bc UEFI OutputString needs CHAR16
        inc     r8

.lp_cont:
        test    rcx, rcx
        jnz     .lp

        cmp     r8, 2
        ja      @f
        mov     byte [buf + r8*2 ], '0'
        mov     byte [buf + r8*2 + 1], 0 
@@:
        lea     rcx, [buf]
        fstcall efi_puts, rcx

        ret
endp
