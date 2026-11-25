;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; FS-v1 M2 self test, eight legs, the blk self test convention:
;; every failing check ORs its leg bit into r12d through a global
;; trampoline and jumps to the leg end, so one run reports every
;; broken leg at once; the mask lands in fs_test_reads' sibling
;; fs_test_mask and a non zero mask is reported on COM1 before the
;; foundation takes the standard fatal path. The module itself never
;; panics.
;;
;; The exact call sequence below is frozen: the stage2-fs gate mirror
;; replays it sector for sector and derives FS_TEST_READS_FINAL (24 on
;; the plain image, 48 on the BIG.BIN image). Any edit here must be
;; mirrored there and in the QEMU judge.
;;
;; r13d holds the IBM.CH8 fd across legs 1..3, r14d is the scratch fd.
;; IF is already 0 (the foundation runs CLI) and is never touched.
;;

fs_selftest:
        fs_enter
        cld
        xor     r12d, r12d              ; the leg failure mask

        ; ---------------- leg 0: geometry + BIG.BIN probe ----------------
        cmp     qword [fs_ready], 1
        jne     fs_st_fail_bpb
        cmp     qword [fs_spc], FS_TEST_SPC
        jne     fs_st_fail_bpb
        cmp     qword [fs_reserved], FS_TEST_RESERVED
        jne     fs_st_fail_bpb
        cmp     qword [fs_nfats], FS_TEST_NFATS
        jne     fs_st_fail_bpb
        cmp     qword [fs_spf], FS_TEST_SPF
        jne     fs_st_fail_bpb
        cmp     qword [fs_root_entries], FS_TEST_ROOT_ENTRIES
        jne     fs_st_fail_bpb
        cmp     qword [fs_clusters], FS_TEST_CLUSTERS
        jne     fs_st_fail_bpb
        cmp     qword [fs_total], FS_TEST_TOTAL
        jne     fs_st_fail_bpb
        cmp     qword [fs_fat_lba], FS_TEST_FAT_LBA
        jne     fs_st_fail_bpb
        cmp     qword [fs_root_lba], FS_TEST_ROOT_LBA
        jne     fs_st_fail_bpb
        cmp     qword [fs_data_lba], FS_TEST_DATA_LBA
        jne     fs_st_fail_bpb
        ; BIG.BIN presence decides which counter total leg 7 expects
        mov     rcx, p_fs_big
        mov     rdx, fs_test_stat
        call    fs_stat
        test    eax, eax
        jz      .big_found
        cmp     eax, FS_ENOENT
        jne     fs_st_fail_bpb
        mov     qword [fs_test_big_present], 0
        jmp     fs_st_bpb_done
.big_found:
        cmp     qword [fs_test_stat], FS_TEST_BIG_SIZE
        jne     fs_st_fail_bpb
        mov     qword [fs_test_big_present], 1
fs_st_bpb_done:

        ; ---------------- leg 1: lowercase /roms/ibm.ch8 ----------------
        mov     rcx, p_fs_ibm_lower
        xor     edx, edx
        call    fs_open
        test    eax, eax
        jnz     fs_st_fail_ibm
        test    edx, edx                ; the first free slot must be 0
        jnz     fs_st_fail_ibm
        mov     r13d, edx
        mov     ecx, r13d
        mov     rdx, fs_test_ibm_buf
        mov     r8d, 512
        call    fs_read
        test    eax, eax
        jnz     fs_st_fail_ibm
        cmp     edx, FS_TEST_IBM_SIZE
        jne     fs_st_fail_ibm
        mov     rax, FS_TEST_ROM_PREFIX
        cmp     rax, [fs_test_ibm_buf]
        jne     fs_st_fail_ibm
        ; a second read sits at EOF and must return 0 without IO
        mov     ecx, r13d
        mov     rdx, fs_test_scratch
        mov     r8d, 16
        call    fs_read
        test    eax, eax
        jnz     fs_st_fail_ibm
        test    edx, edx
        jnz     fs_st_fail_ibm
fs_st_ibm_done:

        ; ---------------- leg 2: /ROMS/TETRIS1.CH8 ----------------
        mov     rcx, p_fs_tetris
        xor     edx, edx
        call    fs_open
        test    eax, eax
        jnz     fs_st_fail_tetris
        mov     r14d, edx
        mov     ecx, r14d
        mov     rdx, fs_test_tetris_buf
        mov     r8d, 512
        call    fs_read
        test    eax, eax
        jnz     fs_st_fail_tetris
        cmp     edx, FS_TEST_TETRIS_SIZE
        jne     fs_st_fail_tetris
        mov     ecx, r14d
        mov     rdx, fs_test_scratch
        mov     r8d, 16
        call    fs_read
        test    eax, eax
        jnz     fs_st_fail_tetris
        test    edx, edx
        jnz     fs_st_fail_tetris
        mov     ecx, r14d
        call    fs_close
        test    eax, eax
        jnz     fs_st_fail_tetris
fs_st_tetris_done:

        ; ---------------- leg 3: seek/read boundaries on IBM.CH8 ----------------
        mov     ecx, r13d
        mov     rdx, 60
        xor     r8d, r8d                ; FS_SEEK_SET
        call    fs_seek
        test    eax, eax
        jnz     fs_st_fail_seek
        cmp     edx, 60
        jne     fs_st_fail_seek
        mov     ecx, r13d
        mov     rdx, fs_test_scratch
        mov     r8d, 60
        call    fs_read
        test    eax, eax
        jnz     fs_st_fail_seek
        cmp     edx, 60
        jne     fs_st_fail_seek
        xor     ecx, ecx
.cmp60:
        mov     al, [fs_test_scratch + rcx]
        cmp     al, [fs_test_ibm_buf + 60 + rcx]
        jne     fs_st_fail_seek
        inc     ecx
        cmp     ecx, 60
        jb      .cmp60
        ; a negative absolute offset is EINVAL
        mov     ecx, r13d
        mov     rdx, -1
        xor     r8d, r8d
        call    fs_seek
        cmp     eax, FS_EINVAL
        jne     fs_st_fail_seek
        ; SEEK_END lands on the size, the read there is EOF
        mov     ecx, r13d
        xor     edx, edx
        mov     r8d, FS_SEEK_END
        call    fs_seek
        test    eax, eax
        jnz     fs_st_fail_seek
        cmp     edx, FS_TEST_IBM_SIZE
        jne     fs_st_fail_seek
        mov     ecx, r13d
        mov     rdx, fs_test_scratch
        mov     r8d, 16
        call    fs_read
        test    eax, eax
        jnz     fs_st_fail_seek
        test    edx, edx
        jnz     fs_st_fail_seek
        ; the last byte: SEEK_CUR -1 from EOF, a 100 byte ask clamps to 1
        mov     ecx, r13d
        mov     rdx, -1
        mov     r8d, FS_SEEK_CUR
        call    fs_seek
        test    eax, eax
        jnz     fs_st_fail_seek
        cmp     edx, FS_TEST_IBM_SIZE - 1
        jne     fs_st_fail_seek
        mov     ecx, r13d
        mov     rdx, fs_test_scratch
        mov     r8d, 100
        call    fs_read
        test    eax, eax
        jnz     fs_st_fail_seek
        cmp     edx, 1
        jne     fs_st_fail_seek
        mov     al, [fs_test_ibm_buf + FS_TEST_IBM_SIZE - 1]
        cmp     al, [fs_test_scratch]
        jne     fs_st_fail_seek
        mov     ecx, r13d
        xor     edx, edx
        xor     r8d, r8d
        call    fs_seek
        test    eax, eax
        jnz     fs_st_fail_seek
        test    edx, edx
        jnz     fs_st_fail_seek
        mov     ecx, r13d
        call    fs_close
        test    eax, eax
        jnz     fs_st_fail_seek
fs_st_seek_done:

        ; ---------------- leg 4: refusal paths + readdir evidence ----------------
        ; (1) a missing name is ENOENT
        mov     rcx, p_fs_missing
        xor     edx, edx
        call    fs_open
        cmp     eax, FS_ENOENT
        jne     fs_st_fail_neg
        ; (2) a file used as a directory is EPERM
        mov     rcx, p_fs_ibm_x
        xor     edx, edx
        call    fs_open
        cmp     eax, FS_EPERM
        jne     fs_st_fail_neg
        ; (3) a write open mode is EPERM on the read-only mount
        mov     rcx, p_fs_ibm
        mov     edx, FS_O_WRONLY
        call    fs_open
        cmp     eax, FS_EPERM
        jne     fs_st_fail_neg
        ; (4) the never opened slot 7 refuses every fd call with EINVAL
        mov     ecx, FS_MAX_FDS - 1
        mov     rdx, fs_test_scratch
        mov     r8d, 16
        call    fs_read
        cmp     eax, FS_EINVAL
        jne     fs_st_fail_neg
        mov     ecx, FS_MAX_FDS - 1
        xor     edx, edx
        xor     r8d, r8d
        call    fs_seek
        cmp     eax, FS_EINVAL
        jne     fs_st_fail_neg
        mov     ecx, FS_MAX_FDS - 1
        call    fs_close
        cmp     eax, FS_EINVAL
        jne     fs_st_fail_neg
        mov     ecx, FS_MAX_FDS - 1
        mov     rdx, fs_test_dirents
        mov     r8d, 96
        call    fs_readdir
        cmp     eax, FS_EINVAL
        jne     fs_st_fail_neg
        ; (5) stat of a missing path is ENOENT
        mov     rcx, p_fs_nope
        mov     rdx, fs_test_stat
        call    fs_stat
        cmp     eax, FS_ENOENT
        jne     fs_st_fail_neg
        ; (6) a directory fd refuses read and seek
        mov     rcx, p_fs_roms
        xor     edx, edx
        call    fs_open
        test    eax, eax
        jnz     fs_st_fail_neg
        mov     r14d, edx
        mov     ecx, r14d
        mov     rdx, fs_test_scratch
        mov     r8d, 16
        call    fs_read
        cmp     eax, FS_EPERM
        jne     fs_st_fail_neg
        mov     ecx, r14d
        xor     edx, edx
        xor     r8d, r8d
        call    fs_seek
        cmp     eax, FS_EINVAL
        jne     fs_st_fail_neg
        mov     ecx, r14d
        call    fs_close
        test    eax, eax
        jnz     fs_st_fail_neg
        ; (7) a file fd refuses readdir
        mov     rcx, p_fs_ibm
        xor     edx, edx
        call    fs_open
        test    eax, eax
        jnz     fs_st_fail_neg
        mov     r14d, edx
        mov     ecx, r14d
        mov     rdx, fs_test_dirents
        mov     r8d, 96
        call    fs_readdir
        cmp     eax, FS_EPERM
        jne     fs_st_fail_neg
        mov     ecx, r14d
        call    fs_close
        test    eax, eax
        jnz     fs_st_fail_neg
        ; (8) the root lists ROMS and PROGRAMS, then parks at the sentinel
        mov     rcx, p_fs_root
        xor     edx, edx
        call    fs_open
        test    eax, eax
        jnz     fs_st_fail_neg
        mov     r14d, edx
        mov     ecx, r14d
        mov     rdx, fs_test_dirents
        mov     r8d, 96
        call    fs_readdir
        test    eax, eax
        jnz     fs_st_fail_neg
        cmp     edx, 2 * FS_DIRENT_SIZE
        jne     fs_st_fail_neg
        test    dword [fs_test_dirents], FS_ATTR_DIRECTORY
        jz      fs_st_fail_neg
        cmp     qword [fs_test_dirents + 8], 0
        jne     fs_st_fail_neg
        test    dword [fs_test_dirents + 32], FS_ATTR_DIRECTORY
        jz      fs_st_fail_neg
        cmp     qword [fs_test_dirents + 40], 0
        jne     fs_st_fail_neg
        lea     rsi, [fs_test_dirents + 16]
        mov     rdi, n_fs_roms
        mov     ecx, 11
        repe cmpsb
        jne     fs_st_fail_neg
        lea     rsi, [fs_test_dirents + 48]
        mov     rdi, n_fs_programs
        mov     ecx, 11
        repe cmpsb
        jne     fs_st_fail_neg
        mov     ecx, r14d
        mov     rdx, fs_test_dirents
        mov     r8d, 96
        call    fs_readdir
        test    eax, eax
        jnz     fs_st_fail_neg
        test    edx, edx
        jnz     fs_st_fail_neg
        mov     ecx, r14d
        call    fs_close
        test    eax, eax
        jnz     fs_st_fail_neg
        ; (9) /ROMS lists IBM.CH8 and TETRIS1.CH8 with their sizes
        mov     rcx, p_fs_roms
        xor     edx, edx
        call    fs_open
        test    eax, eax
        jnz     fs_st_fail_neg
        mov     r14d, edx
        mov     ecx, r14d
        mov     rdx, fs_test_dirents
        mov     r8d, 96
        call    fs_readdir
        test    eax, eax
        jnz     fs_st_fail_neg
        cmp     edx, 2 * FS_DIRENT_SIZE
        jne     fs_st_fail_neg
        cmp     qword [fs_test_dirents + 8], FS_TEST_IBM_SIZE
        jne     fs_st_fail_neg
        cmp     qword [fs_test_dirents + 40], FS_TEST_TETRIS_SIZE
        jne     fs_st_fail_neg
        lea     rsi, [fs_test_dirents + 16]
        mov     rdi, n_fs_ibm
        mov     ecx, 11
        repe cmpsb
        jne     fs_st_fail_neg
        lea     rsi, [fs_test_dirents + 48]
        mov     rdi, n_fs_tetris
        mov     ecx, 11
        repe cmpsb
        jne     fs_st_fail_neg
        mov     ecx, r14d
        mov     rdx, fs_test_dirents
        mov     r8d, 96
        call    fs_readdir
        test    eax, eax
        jnz     fs_st_fail_neg
        test    edx, edx
        jnz     fs_st_fail_neg
        mov     ecx, r14d
        call    fs_close
        test    eax, eax
        jnz     fs_st_fail_neg
        ; (10) /PROGRAMS is empty on the plain image, BIG.BIN on the big one
        mov     rcx, p_fs_programs
        xor     edx, edx
        call    fs_open
        test    eax, eax
        jnz     fs_st_fail_neg
        mov     r14d, edx
        mov     ecx, r14d
        mov     rdx, fs_test_dirents
        mov     r8d, 96
        call    fs_readdir
        test    eax, eax
        jnz     fs_st_fail_neg
        cmp     qword [fs_test_big_present], 0
        je      .prog_empty
        cmp     edx, FS_DIRENT_SIZE
        jne     fs_st_fail_neg
        cmp     qword [fs_test_dirents + 8], FS_TEST_BIG_SIZE
        jne     fs_st_fail_neg
        lea     rsi, [fs_test_dirents + 16]
        mov     rdi, n_fs_big
        mov     ecx, 11
        repe cmpsb
        jne     fs_st_fail_neg
        jmp     .prog_again
.prog_empty:
        test    edx, edx
        jnz     fs_st_fail_neg
.prog_again:
        mov     ecx, r14d
        mov     rdx, fs_test_dirents
        mov     r8d, 96
        call    fs_readdir
        test    eax, eax
        jnz     fs_st_fail_neg
        test    edx, edx
        jnz     fs_st_fail_neg
        mov     ecx, r14d
        call    fs_close
        test    eax, eax
        jnz     fs_st_fail_neg
fs_st_neg_done:

        ; ---------------- leg 5: BIG.BIN cross cluster spans ----------------
        cmp     qword [fs_test_big_present], 0
        je      fs_st_big_done
        mov     rcx, p_fs_big
        xor     edx, edx
        call    fs_open
        test    eax, eax
        jnz     fs_st_fail_big
        mov     r14d, edx
        mov     ecx, r14d
        mov     rdx, fs_test_big_buf
        mov     r8d, 9216
        call    fs_read
        test    eax, eax
        jnz     fs_st_fail_big
        cmp     edx, FS_TEST_BIG_SIZE
        jne     fs_st_fail_big
        mov     rax, FS_TEST_BIG_PREFIX
        cmp     rax, [fs_test_big_buf]
        jne     fs_st_fail_big
        cmp     byte [fs_test_big_buf + FS_TEST_BIG_SIZE - 1], 0xFF
        jne     fs_st_fail_big
        mov     ecx, r14d
        mov     rdx, fs_test_scratch
        mov     r8d, 16
        call    fs_read
        test    eax, eax
        jnz     fs_st_fail_big
        test    edx, edx
        jnz     fs_st_fail_big
        ; a 6 byte span across the 1024 byte cluster boundary at 1021
        mov     ecx, r14d
        mov     rdx, 1021
        xor     r8d, r8d
        call    fs_seek
        test    eax, eax
        jnz     fs_st_fail_big
        cmp     edx, 1021
        jne     fs_st_fail_big
        mov     ecx, r14d
        mov     rdx, fs_test_scratch
        mov     r8d, 6
        call    fs_read
        test    eax, eax
        jnz     fs_st_fail_big
        cmp     edx, 6
        jne     fs_st_fail_big
        xor     ecx, ecx
.cmp6:
        mov     al, [fs_test_scratch + rcx]
        cmp     al, [fs_test_big_buf + 1021 + rcx]
        jne     fs_st_fail_big
        inc     ecx
        cmp     ecx, 6
        jb      .cmp6
        ; a 2 byte span across the 512 byte sector boundary at 2047
        mov     ecx, r14d
        mov     rdx, 2047
        xor     r8d, r8d
        call    fs_seek
        test    eax, eax
        jnz     fs_st_fail_big
        mov     ecx, r14d
        mov     rdx, fs_test_scratch
        mov     r8d, 2
        call    fs_read
        test    eax, eax
        jnz     fs_st_fail_big
        cmp     edx, 2
        jne     fs_st_fail_big
        mov     ax, [fs_test_big_buf + 2047]
        cmp     ax, [fs_test_scratch]
        jne     fs_st_fail_big
        ; the tail: a 100 byte ask at 8224 clamps to the last 5 bytes
        mov     ecx, r14d
        mov     rdx, FS_TEST_BIG_SIZE - 5
        xor     r8d, r8d
        call    fs_seek
        test    eax, eax
        jnz     fs_st_fail_big
        mov     ecx, r14d
        mov     rdx, fs_test_scratch
        mov     r8d, 100
        call    fs_read
        test    eax, eax
        jnz     fs_st_fail_big
        cmp     edx, 5
        jne     fs_st_fail_big
        xor     ecx, ecx
.cmp5:
        mov     al, [fs_test_scratch + rcx]
        cmp     al, [fs_test_big_buf + FS_TEST_BIG_SIZE - 5 + rcx]
        jne     fs_st_fail_big
        inc     ecx
        cmp     ecx, 5
        jb      .cmp5
        ; seeking exactly to the size reads nothing
        mov     ecx, r14d
        mov     rdx, FS_TEST_BIG_SIZE
        xor     r8d, r8d
        call    fs_seek
        test    eax, eax
        jnz     fs_st_fail_big
        mov     ecx, r14d
        mov     rdx, fs_test_scratch
        mov     r8d, 16
        call    fs_read
        test    eax, eax
        jnz     fs_st_fail_big
        test    edx, edx
        jnz     fs_st_fail_big
        mov     ecx, r14d
        call    fs_close
        test    eax, eax
        jnz     fs_st_fail_big
fs_st_big_done:

        ; ---------------- leg 6: stat size/attr/reserved zeros ----------------
        mov     rcx, p_fs_ibm
        mov     rdx, fs_test_stat
        call    fs_stat
        test    eax, eax
        jnz     fs_st_fail_stat
        cmp     qword [fs_test_stat], FS_TEST_IBM_SIZE
        jne     fs_st_fail_stat
        mov     eax, [fs_test_stat + 8]
        test    eax, FS_ATTR_READ_ONLY
        jz      fs_st_fail_stat
        test    eax, FS_ATTR_DIRECTORY
        jnz     fs_st_fail_stat
        mov     rcx, p_fs_roms
        mov     rdx, fs_test_stat
        call    fs_stat
        test    eax, eax
        jnz     fs_st_fail_stat
        test    dword [fs_test_stat + 8], FS_ATTR_DIRECTORY
        jz      fs_st_fail_stat
        cmp     qword [fs_test_stat], 0
        jne     fs_st_fail_stat
        mov     rcx, p_fs_root
        mov     rdx, fs_test_stat
        call    fs_stat
        test    eax, eax
        jnz     fs_st_fail_stat
        test    dword [fs_test_stat + 8], FS_ATTR_DIRECTORY
        jz      fs_st_fail_stat
        cmp     qword [fs_test_stat], 0
        jne     fs_st_fail_stat
        ; the 52 reserved bytes must all be zero
        mov     ecx, 12
.reszero:
        cmp     byte [fs_test_stat + rcx], 0
        jne     fs_st_fail_stat
        inc     ecx
        cmp     ecx, FS_STAT_SIZE
        jb      .reszero
fs_st_stat_done:

        ; ---------------- leg 7: the sector fetch counter ----------------
        mov     edx, FS_TEST_READS_FINAL
        cmp     qword [fs_test_big_present], 0
        je      .count
        mov     edx, FS_TEST_READS_FINAL_BIG
.count:
        cmp     [fs_test_reads], rdx
        jne     fs_st_fail_count
fs_st_count_done:

        mov     [fs_test_mask], r12
        test    r12d, r12d
        jnz     .report
        xor     eax, eax
        fs_leave
        ret
.report:
        ; name the failing legs on COM1 before the standard fatal path
        mov     rcx, msg_fs_selftest_mask
        mov     rdx, CPU_PUTS_MAX
        call    cpu_com1_puts
        mov     rcx, [fs_test_mask]
        mov     rdx, CPU_PUTHEX_DIGITS
        call    cpu_com1_puthex
        mov     rcx, msg_fs_nl
        mov     rdx, CPU_PUTS_MAX
        call    cpu_com1_puts
        mov     eax, FS_EIO
        mov     rdx, [fs_test_mask]
        fs_leave
        ret

; the leg failure trampolines, global labels so the gate can locate
; every leg by name exactly like the blk self test

fs_st_fail_bpb:
        or      r12d, FS_ST_BPB
        jmp     fs_st_bpb_done
fs_st_fail_ibm:
        or      r12d, FS_ST_IBM
        jmp     fs_st_ibm_done
fs_st_fail_tetris:
        or      r12d, FS_ST_TETRIS
        jmp     fs_st_tetris_done
fs_st_fail_seek:
        or      r12d, FS_ST_SEEK
        jmp     fs_st_seek_done
fs_st_fail_neg:
        or      r12d, FS_ST_NEG
        jmp     fs_st_neg_done
fs_st_fail_big:
        or      r12d, FS_ST_BIG
        jmp     fs_st_big_done
fs_st_fail_stat:
        or      r12d, FS_ST_STAT
        jmp     fs_st_stat_done
fs_st_fail_count:
        or      r12d, FS_ST_COUNT
        jmp     fs_st_count_done

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; self test messages and request constants, read only bytes emitted
; with the code like the blk module; the mutable data region stays last

msg_fs_selftest_mask db 'fs selftest mask = ', 0
msg_fs_nl            db 13, 10, 0

p_fs_ibm_lower db '/roms/ibm.ch8', 0
p_fs_ibm       db '/ROMS/IBM.CH8', 0
p_fs_tetris    db '/ROMS/TETRIS1.CH8', 0
p_fs_missing   db '/ROMS/MISSING.CH8', 0
p_fs_ibm_x     db '/ROMS/IBM.CH8/X', 0
p_fs_nope      db '/NOPE', 0
p_fs_roms      db '/ROMS', 0
p_fs_root      db '/', 0
p_fs_programs  db '/PROGRAMS', 0
p_fs_big       db '/PROGRAMS/BIG.BIN', 0

; the expected raw 11 byte 8.3 names of the readdir evidence
n_fs_roms     db 'ROMS       '
n_fs_programs db 'PROGRAMS   '
n_fs_ibm      db 'IBM     CH8'
n_fs_tetris   db 'TETRIS1 CH8'
n_fs_big      db 'BIG     BIN'

fs_code_end: