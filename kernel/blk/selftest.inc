;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; The block layer self test, seven legs, every wait bounded.
;;
;; The module has zero STI instructions and zero interrupt wiring: the
;; whole test runs with IF = 0 over the polled used ring. The evidence
;; buffers stay in the data region so the host can read them through
;; the gdbstub and compare them byte by byte against build/fs-v1/disk.img.
;;
;; Legs (failure bits of blk_test_mask):
;;   0  the config capacity equals the frozen image geometry (16384)
;;   1  LBA0 reads back the BPB: 55 AA signature, 512 bytes per sector
;;      and the KOLIBR64 OEM id; the sector stays in blk_test_lba0_buf
;;   2  LBA101 reads back the first 8 ROM bytes of IBM.CH8; the sector
;;      stays in blk_test_data_buf
;;   3  argument and range refusal without device traffic: LBA at the
;;      capacity and a partial overrun return EOVERFLOW, n = 0 returns
;;      EINVAL, a null buffer returns EFAULT
;;   4  the last LBA (capacity - 1) reads OK
;;   5  multi chunk copy: 9 sectors from LBA101 (chunks 8+1, the ROM
;;      prefix lands in chunk 1) and 9 sectors from LBA93 (chunks 8+1,
;;      the ROM prefix lands in chunk 2 at offset 8*512)
;;   6  the completed request counter equals the frozen 7
;;
;; On exit: blk_test_mask holds the failure bits, blk_test_reads == 7
;; on a clean run, IF = 0. A failure prints the mask on COM1 through
;; the CPU console before returning EIO, like the input self test.
;;

; ------------------------------------------------------------------
; run the seven legs
; in
; nothing; blk_init must have succeeded, IF must be 0
; out
; eax = BLK_OK, or BLK_EIO with rdx = the failed leg mask
; ------------------------------------------------------------------

blk_selftest:
        blk_enter
        cld
        cli
        xor     r12d, r12d              ; failure mask

        ; ---------------- leg 0: the config capacity ----------------
        cmp     qword [blk_capacity], BLK_TEST_CAPACITY
        jne     blk_st_fail_cap
blk_st_cap_done:

        ; ---------------- leg 1: the LBA0 BPB ----------------
        xor     ecx, ecx
        mov     rdx, blk_test_lba0_buf
        mov     r8d, 1
        call    blk_read
        test    eax, eax
        jnz     blk_st_fail_bpb
        cmp     word [blk_test_lba0_buf + 510], BLK_TEST_BPB_SIG
        jne     blk_st_fail_bpb
        cmp     word [blk_test_lba0_buf + 11], BLK_SECTOR_SIZE
        jne     blk_st_fail_bpb
        mov     rax, BLK_TEST_OEM_ID
        cmp     rax, [blk_test_lba0_buf + 3]
        jne     blk_st_fail_bpb
blk_st_bpb_done:

        ; ---------------- leg 2: the LBA101 ROM prefix ----------------
        mov     rcx, BLK_TEST_DATA_LBA
        mov     rdx, blk_test_data_buf
        mov     r8d, 1
        call    blk_read
        test    eax, eax
        jnz     blk_st_fail_rom
        mov     rax, BLK_TEST_ROM_PREFIX
        cmp     rax, [blk_test_data_buf]
        jne     blk_st_fail_rom
blk_st_rom_done:

        ; ---------------- leg 3: refusal without traffic ----------------
        mov     rcx, [blk_capacity]
        mov     rdx, blk_test_scratch
        mov     r8d, 1
        call    blk_read
        cmp     eax, BLK_EOVERFLOW
        jne     blk_st_fail_range
        ; partial overrun: the last sector fits, the second does not
        mov     rcx, [blk_capacity]
        dec     rcx
        mov     rdx, blk_test_scratch
        mov     r8d, 2
        call    blk_read
        cmp     eax, BLK_EOVERFLOW
        jne     blk_st_fail_range
        xor     ecx, ecx
        mov     rdx, blk_test_scratch
        xor     r8d, r8d
        call    blk_read
        cmp     eax, BLK_EINVAL
        jne     blk_st_fail_range
        xor     ecx, ecx
        xor     edx, edx
        mov     r8d, 1
        call    blk_read
        cmp     eax, BLK_EFAULT
        jne     blk_st_fail_range
blk_st_range_done:

        ; ---------------- leg 4: the last LBA ----------------
        mov     rcx, [blk_capacity]
        dec     rcx
        mov     rdx, blk_test_scratch
        mov     r8d, 1
        call    blk_read
        test    eax, eax
        jnz     blk_st_fail_last
blk_st_last_done:

        ; ---------------- leg 5: multi chunk copy ----------------
        ; 9 sectors from LBA101: chunk 1 carries the ROM prefix
        mov     rcx, BLK_TEST_DATA_LBA
        mov     rdx, blk_test_scratch
        mov     r8d, 9
        call    blk_read
        test    eax, eax
        jnz     blk_st_fail_chunk
        mov     rax, BLK_TEST_ROM_PREFIX
        cmp     rax, [blk_test_scratch]
        jne     blk_st_fail_chunk
        ; 9 sectors from LBA93: the ROM prefix lands in chunk 2
        mov     rcx, BLK_TEST_DATA_LBA - 8
        mov     rdx, blk_test_scratch
        mov     r8d, 9
        call    blk_read
        test    eax, eax
        jnz     blk_st_fail_chunk
        mov     rax, BLK_TEST_ROM_PREFIX
        cmp     rax, [blk_test_scratch + 8 * BLK_SECTOR_SIZE]
        jne     blk_st_fail_chunk
blk_st_chunk_done:

        ; ---------------- leg 6: the request counter ----------------
        cmp     qword [blk_test_reads], BLK_TEST_READS_FINAL
        jne     blk_st_fail_count
blk_st_count_done:

        mov     [blk_test_mask], r12
        test    r12d, r12d
        jnz     .report
        xor     eax, eax
        blk_leave
        ret
.report:
        ; name the failing legs on COM1 before the standard fatal path
        mov     rcx, msg_blk_selftest_mask
        mov     rdx, CPU_PUTS_MAX
        call    cpu_com1_puts
        mov     rcx, [blk_test_mask]
        mov     rdx, CPU_PUTHEX_DIGITS
        call    cpu_com1_puthex
        mov     rcx, msg_blk_nl
        mov     rdx, CPU_PUTS_MAX
        call    cpu_com1_puts
        mov     eax, BLK_EIO
        mov     rdx, [blk_test_mask]
        blk_leave
        ret

; the leg failure trampolines, global labels so the gate can locate
; every leg by name exactly like the input self test

blk_st_fail_cap:
        or      r12d, BLK_ST_CAP
        jmp     blk_st_cap_done
blk_st_fail_bpb:
        or      r12d, BLK_ST_BPB
        jmp     blk_st_bpb_done
blk_st_fail_rom:
        or      r12d, BLK_ST_ROM
        jmp     blk_st_rom_done
blk_st_fail_range:
        or      r12d, BLK_ST_RANGE
        jmp     blk_st_range_done
blk_st_fail_last:
        or      r12d, BLK_ST_LAST
        jmp     blk_st_last_done
blk_st_fail_chunk:
        or      r12d, BLK_ST_CHUNK
        jmp     blk_st_chunk_done
blk_st_fail_count:
        or      r12d, BLK_ST_COUNT
        jmp     blk_st_count_done

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; self test messages, read only constants emitted with the code like
; the foundation kmsg_* block; the mutable data region stays last

msg_blk_selftest_mask db 'blk selftest mask = ', 0
msg_blk_nl            db 13, 10, 0

blk_code_end:
