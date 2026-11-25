;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2026. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; fsprobe, the FS-v1 M3 (task 133) Ring 3 file syscall probe.
;;
;; Flat binary, org 0x100400000, entry at 0x100400000, exactly the
;; frozen APP-v1 scheme B domains of ../chip8/api.inc: code
;; [0x100400000, +16KiB) RX+US, data [0x100600000, +2MiB) RW+NX+US
;; (the kernel leg maps only the first data page), entry RSP =
;; 0x100800000 on a kernel-built 16 KiB stack.
;;
;; Eleven legs, one success bit each in fp_mask (FP_MASK_ALL=0x7FF):
;;   0 OPEN /ROMS/IBM.CH8            -> fd 0
;;   1 READ 512                      -> 132 bytes, byte-for-byte
;;                                      against the embedded ROM
;;   2 READ again                    -> 0 (EOF)
;;   3 SEEK SET 4 / END -8 / CUR +2  -> 4 / 124 / 126, SET
;;                                      0x100000000 -> full 64 bit
;;                                      return (task 138 W1), then SET 0
;;   4 CLOSE                         -> 0
;;   5 STAT                          -> size 132, attr 1 (read only)
;;   6 OPEN /ROMS + READDIR 64       -> two 32 B entries, then 0
;;   7 OPEN a missing file           -> K64_ENOENT
;;   8 three bad pointers            -> K64_EFAULT (unmapped in-domain
;;                                      page, kernel VA, RO code page)
;;   9 bad fd / out of range fd      -> K64_EINVAL
;;  10 write flags / numbers 10, 16  -> K64_ENOTSUP
;;
;; A failing leg parks its index in fp_fail and exits with index+1;
;; a clean run exits 0 through SYS_EXIT. The kernel fsys_probe leg
;; reads fp_mask and the evidence qwords back through the identity map.
;;
;; Assemble exactly like user/chip8/chip8.asm:
;;   INCLUDE=<fasm2>/include fasmg -e100 -i"Include 'fasm2.inc'" \
;;       user/fsprobe/fsprobe.asm build/user-fsprobe/fsprobe.bin
;;

use64
org     APP_CODE_BASE

include 'api.inc'

; ------------------------------------------------------------------
; entry point, must stay at APP_CODE_BASE (api.inc emits no bytes)
; ------------------------------------------------------------------

start:
        mov     qword [fp_fail], -1

        ; leg 0: OPEN, the fd table is empty after the fs self test,
        ; so the first free slot must be fd 0
        mov     eax, SYS_OPEN
        mov     rdi, path_ibm
        xor     esi, esi
        syscall
        test    rax, rax
        jnz     .fail0
        mov     [fp_open_fd], rax
        or      qword [fp_mask], 1 shl 0

        ; leg 1: READ the whole file, then compare every byte against
        ; the frozen ROM embedded in this image
        mov     eax, SYS_READ
        xor     edi, edi
        mov     rsi, FP_READBUF_VA
        mov     edx, 512
        syscall
        cmp     rax, FP_IBM_SIZE
        jne     .fail1
        mov     [fp_read_len], rax
        mov     rsi, FP_READBUF_VA
        mov     rdi, rom_ref
        mov     ecx, FP_IBM_SIZE
        repe    cmpsb
        jne     .fail1
        or      qword [fp_mask], 1 shl 1

        ; leg 2: a second read sits at EOF and returns zero bytes
        mov     eax, SYS_READ
        xor     edi, edi
        mov     rsi, FP_READBUF_VA
        mov     edx, 512
        syscall
        test    rax, rax
        jnz     .fail2
        mov     [fp_eof_len], rax
        or      qword [fp_mask], 1 shl 2

        ; leg 3: all three whence modes, then rewind for the close
        mov     eax, SYS_SEEK
        xor     edi, edi
        mov     rsi, 4
        xor     edx, edx                ; SEEK_SET
        syscall
        cmp     rax, 4
        jne     .fail3
        mov     eax, SYS_SEEK
        xor     edi, edi
        mov     rsi, -8
        mov     edx, SEEK_END
        syscall
        cmp     rax, FP_IBM_SIZE - 8
        jne     .fail3
        mov     [fp_seek_end], rax
        mov     eax, SYS_SEEK
        xor     edi, edi
        mov     rsi, 2
        mov     edx, SEEK_CUR
        syscall
        cmp     rax, FP_IBM_SIZE - 6
        jne     .fail3
        ; task 138 W1: SEEK_SET beyond 4 GiB must return the full 64
        ; bit offset, never a 32 bit truncation; rsi survives SYSCALL
        mov     eax, SYS_SEEK
        xor     edi, edi
        mov     rsi, 0x100000000
        xor     edx, edx                ; SEEK_SET
        syscall
        cmp     rax, rsi
        jne     .fail3
        mov     eax, SYS_SEEK
        xor     edi, edi
        xor     esi, esi
        xor     edx, edx
        syscall
        test    rax, rax
        jnz     .fail3
        or      qword [fp_mask], 1 shl 3

        ; leg 4: CLOSE the file
        mov     eax, SYS_CLOSE
        xor     edi, edi
        syscall
        test    rax, rax
        jnz     .fail4
        or      qword [fp_mask], 1 shl 4

        ; leg 5: STAT, size and attr land in the 64 B output buffer
        mov     eax, SYS_STAT
        mov     rdi, path_ibm
        mov     rsi, FP_STATBUF_VA
        syscall
        test    rax, rax
        jnz     .fail5
        mov     rax, [FP_STATBUF_VA]
        cmp     rax, FP_IBM_SIZE
        jne     .fail5
        mov     [fp_stat_size], rax
        mov     eax, [FP_STATBUF_VA + 8]
        cmp     eax, FP_ATTR_RO
        jne     .fail5
        mov     [fp_stat_attr], rax
        or      qword [fp_mask], 1 shl 5

        ; leg 6: READDIR of /ROMS, exactly the two frozen ROM entries
        mov     eax, SYS_OPEN
        mov     rdi, path_dir
        xor     esi, esi
        syscall
        test    rax, rax
        jnz     .fail6
        mov     eax, SYS_READDIR
        xor     edi, edi
        mov     rsi, FP_DIRENTS_VA
        mov     edx, 64
        syscall
        cmp     rax, 64
        jne     .fail6
        mov     [fp_dir_bytes], rax
        ; entry 0: attr 1, size 132, name "IBM     CH8"
        cmp     qword [FP_DIRENTS_VA], FP_ATTR_RO
        jne     .fail6
        cmp     qword [FP_DIRENTS_VA + 8], FP_IBM_SIZE
        jne     .fail6
        mov     rsi, FP_DIRENTS_VA + 16
        mov     rdi, name_ibm
        mov     ecx, 11
        repe    cmpsb
        jne     .fail6
        ; entry 1: attr 1, size 494, name "TETRIS1 CH8"
        cmp     qword [FP_DIRENTS_VA + 32], FP_ATTR_RO
        jne     .fail6
        cmp     qword [FP_DIRENTS_VA + 40], FP_TETRIS_SIZE
        jne     .fail6
        mov     rsi, FP_DIRENTS_VA + 48
        mov     rdi, name_tetris
        mov     ecx, 11
        repe    cmpsb
        jne     .fail6
        ; the directory is exhausted, the next call returns zero
        mov     eax, SYS_READDIR
        xor     edi, edi
        mov     rsi, FP_DIRENTS_VA
        mov     edx, 64
        syscall
        test    rax, rax
        jnz     .fail6
        mov     eax, SYS_CLOSE
        xor     edi, edi
        syscall
        test    rax, rax
        jnz     .fail6
        or      qword [fp_mask], 1 shl 6

        ; leg 7: a well-formed but missing path is ENOENT
        mov     eax, SYS_OPEN
        mov     rdi, path_bad
        xor     esi, esi
        syscall
        cmp     rax, K64_ENOENT
        jne     .fail7
        mov     [fp_enoent], rax
        or      qword [fp_mask], 1 shl 7

        ; leg 8: three bad pointers, all EFAULT. Reopen the file so
        ; the READ has a valid fd and only the buffer is at fault.
        mov     eax, SYS_OPEN
        mov     rdi, path_ibm
        xor     esi, esi
        syscall
        test    rax, rax
        jnz     .fail8
        ; a) an in-domain page the kernel never mapped
        mov     eax, SYS_READ
        xor     edi, edi
        mov     rsi, FP_BADBUF_VA
        mov     edx, 16
        syscall
        cmp     rax, K64_EFAULT
        jne     .fail8
        ; b) a kernel-half path pointer
        mov     eax, SYS_OPEN
        mov     rdi, 0xFFFFFFFF80000000
        xor     esi, esi
        syscall
        cmp     rax, K64_EFAULT
        jne     .fail8
        ; c) a write target on the read-only code page
        mov     eax, SYS_STAT
        mov     rdi, path_ibm
        mov     rsi, APP_CODE_BASE
        syscall
        cmp     rax, K64_EFAULT
        jne     .fail8
        mov     [fp_efault], rax
        mov     eax, SYS_CLOSE
        xor     edi, edi
        syscall
        test    rax, rax
        jnz     .fail8
        or      qword [fp_mask], 1 shl 8

        ; leg 9: a never-opened slot and an out-of-range fd are EINVAL
        mov     eax, SYS_CLOSE
        mov     edi, 7
        syscall
        cmp     rax, K64_EINVAL
        jne     .fail9
        mov     eax, SYS_READ
        mov     edi, 8
        mov     rsi, FP_READBUF_VA
        mov     edx, 16
        syscall
        cmp     rax, K64_EINVAL
        jne     .fail9
        mov     [fp_einval], rax
        or      qword [fp_mask], 1 shl 9

        ; leg 10: write flags and unregistered numbers are ENOTSUP;
        ; number 16 sits exactly on the frozen SYS_CALL_MAX boundary
        mov     eax, SYS_OPEN
        mov     rdi, path_ibm
        mov     esi, 1                  ; O_WRONLY, refused in v1
        syscall
        cmp     rax, K64_ENOTSUP
        jne     .fail10
        mov     eax, 10
        syscall
        cmp     rax, K64_ENOTSUP
        jne     .fail10
        mov     eax, 16
        syscall
        cmp     rax, K64_ENOTSUP
        jne     .fail10
        mov     [fp_enotsup], rax
        or      qword [fp_mask], 1 shl 10

        ; every leg passed: exit 0, anything else is a probe failure
        cmp     qword [fp_mask], FP_MASK_ALL
        jne     .fail99
        xor     edi, edi
        jmp     do_exit

.fail0:
        mov     edi, 1
        jmp     leg_fail
.fail1:
        mov     edi, 2
        jmp     leg_fail
.fail2:
        mov     edi, 3
        jmp     leg_fail
.fail3:
        mov     edi, 4
        jmp     leg_fail
.fail4:
        mov     edi, 5
        jmp     leg_fail
.fail5:
        mov     edi, 6
        jmp     leg_fail
.fail6:
        mov     edi, 7
        jmp     leg_fail
.fail7:
        mov     edi, 8
        jmp     leg_fail
.fail8:
        mov     edi, 9
        jmp     leg_fail
.fail9:
        mov     edi, 10
        jmp     leg_fail
.fail10:
        mov     edi, 11
        jmp     leg_fail
.fail99:
        mov     edi, 99
leg_fail:
        mov     rax, rdi
        sub     rax, 1
        mov     [fp_fail], rax
do_exit:
        mov     eax, SYS_EXIT
        syscall
        jmp     do_exit               ; SYS_EXIT never returns

; ------------------------------------------------------------------
; inline data, read-only, inside the code image
; ------------------------------------------------------------------

path_ibm:       db '/ROMS/IBM.CH8', 0
path_dir:       db '/ROMS', 0
path_bad:       db '/ROMS/NOPE.CH8', 0
name_ibm:       db 'IBM     CH8'
name_tetris:    db 'TETRIS1 CH8'

; the frozen reference bytes, the same file the disk image carries
rom_ref:
        file    '../chip8/roms/ibm.ch8'
rom_ref_end:
if rom_ref_end - rom_ref <> FP_IBM_SIZE
        err 'ibm.ch8 must be the frozen 132 byte image'
end if

; ------------------------------------------------------------------
; image size gate, the code domain is 16 KiB
; ------------------------------------------------------------------

fp_image_end:
if fp_image_end - APP_CODE_BASE > APP_CODE_LIMIT
        err 'fsprobe image exceeds the 16 KiB code domain'
end if
if start <> APP_CODE_BASE
        err 'entry must sit at the code domain base'
end if