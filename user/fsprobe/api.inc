;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2026. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; FS-v1 M3 (task 133) frozen constants of the fsprobe Ring 3 user
;; program: the file syscall numbers 4..9, the K64 errno set and the
;; data-domain layout of the probe evidence page.
;;
;; The VA domains, the stack top and the self-contained MS x64 macros
;; come from the frozen APP-v1 scheme B layout in ../chip8/api.inc;
;; this file includes nothing from kernel/. The kernel-side mirror of
;; the domain is MM_USER_VA_BASE..MM_USER_VA_LIMIT in kernel/mm/api.inc
;; and the FSYS section of kernel/core/contracts.inc.
;;

include '../chip8/api.inc'

; ------------------------------------------------------------------
; file syscall numbers, frozen FS-v1 M3 contract
; rax = number, arguments in rdi/rsi/rdx, result in rax (fd, byte
; count, new offset or 0), negative K64 errno on failure.
; Task 138 freezes the return width and the partial read semantics:
; READ/READDIR/SEEK results are full 64 bit wide (a byte count or an
; offset >= 2 GiB returns verbatim, movsxd serves the error path
; only), and READ/READDIR follow POSIX partial reads: once N > 0
; bytes were delivered, a later EIO/EFAULT returns N, never -errno;
; only a failure before the first delivered byte returns -errno.
; ------------------------------------------------------------------

SYS_OPEN        = 4     ; rdi = path VA (ASCIIZ, absolute), esi = flags
                        ; (v1: only O_RDONLY = 0), rax = fd 0..7
SYS_READ        = 5     ; rdi = fd, rsi = buffer VA, rdx = length,
                        ; rax = bytes read (0 = EOF), full 64 bit;
                        ; POSIX partial read: N > 0 delivered bytes
                        ; win over a later EIO/EFAULT (task 138 S2)
SYS_CLOSE       = 6     ; rdi = fd, rax = 0
SYS_SEEK        = 7     ; rdi = fd, rsi = signed offset, edx = whence
                        ; (0 SET / 1 CUR / 2 END), rax = new offset,
                        ; full 64 bit wide (task 138 W1)
SYS_STAT        = 8     ; rdi = path VA, rsi = 64 B buffer
                        ; (size qword + attr dword + reserved), rax = 0
SYS_READDIR     = 9     ; rdi = directory fd, rsi = buffer, rdx = length,
                        ; rax = bytes written (fixed 32 B entries),
                        ; full 64 bit, POSIX partial read like READ

; the frozen K64 errno set, returned as negative values in rax
K64_EINVAL      = -1
K64_ENOENT      = -3
K64_ENOTSUP     = -6
K64_EIO         = -9
K64_EFAULT      = -11

O_RDONLY        = 0
SEEK_SET        = 0
SEEK_CUR        = 1
SEEK_END        = 2

; ------------------------------------------------------------------
; probe evidence page, APP_DATA_BASE page 0, all fields in one 4 KiB
;; page so the kernel leg maps exactly one data page for the program
; ------------------------------------------------------------------

FP_MASK_VA      = APP_DATA_BASE + 0     ; leg success bits, 11 legs
FP_FAIL_VA      = APP_DATA_BASE + 8     ; failing leg index, -1 = none
FP_OPEN_VA      = APP_DATA_BASE + 16    ; leg 0: the opened fd
FP_READ_VA      = APP_DATA_BASE + 24    ; leg 1: bytes read (132)
FP_EOF_VA       = APP_DATA_BASE + 32    ; leg 2: the EOF read (0)
FP_SEEK_VA      = APP_DATA_BASE + 40    ; leg 3: SEEK_END - 8 (124)
FP_SIZE_VA      = APP_DATA_BASE + 48    ; leg 5: stat size (132)
FP_ATTR_VA      = APP_DATA_BASE + 56    ; leg 5: stat attr (1, read only)
FP_DIR_VA       = APP_DATA_BASE + 64    ; leg 6: readdir bytes (64)
FP_ENOENT_VA    = APP_DATA_BASE + 72    ; leg 7: the ENOENT verdict
FP_EFAULT_VA    = APP_DATA_BASE + 80    ; leg 8: the EFAULT verdict
FP_EINVAL_VA    = APP_DATA_BASE + 88    ; leg 9: the EINVAL verdict
FP_ENOTSUP_VA   = APP_DATA_BASE + 96    ; leg 10: the ENOTSUP verdict
FP_READBUF_VA   = APP_DATA_BASE + 128   ; 512 B read landing buffer
FP_STATBUF_VA   = APP_DATA_BASE + 640   ; 64 B stat output
FP_DIRENTS_VA   = APP_DATA_BASE + 704   ; 256 B readdir landing buffer

FP_MASK_ALL     = 0x7FF                 ; all eleven legs passed
FP_IBM_SIZE     = 132                   ; frozen /ROMS/IBM.CH8 size
FP_TETRIS_SIZE  = 494                   ; frozen /ROMS/TETRIS1.CH8 size
FP_ATTR_RO      = 1                     ; FAT ATTR_READ_ONLY of both ROMs
FP_BADBUF_VA    = APP_DATA_BASE + 0x1000 ; in-domain, deliberately unmapped

if FP_DIRENTS_VA + 256 > APP_DATA_BASE + 4096
        err 'probe evidence must stay inside the first data page'
end if
if FP_BADBUF_VA >= APP_DATA_BASE + APP_DATA_LIMIT
        err 'the unmapped probe buffer must stay inside the data domain'
end if

virtual at FP_MASK_VA
fp_mask         rq 1
fp_fail         rq 1
fp_open_fd      rq 1
fp_read_len     rq 1
fp_eof_len      rq 1
fp_seek_end     rq 1
fp_stat_size    rq 1
fp_stat_attr    rq 1
fp_dir_bytes    rq 1
fp_enoent       rq 1
fp_efault       rq 1
fp_einval       rq 1
fp_enotsup      rq 1
fp_head_end     rb 0
end virtual

if fp_head_end > FP_READBUF_VA
        err 'evidence head overlaps the read buffer'
end if