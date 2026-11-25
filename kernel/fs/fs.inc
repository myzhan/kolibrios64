;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; FS-v1 M2 read-only FAT16 over the blk_read seam, task 132.
;;
;; One static mount, 8.3 short names, case insensitive paths, a static
;; eight slot fd table. The whole first FAT is cached at init (spf is
;; capped at FS_FAT_CACHE_SECTORS); every root/data sector goes through
;; a single sector direct mapped cache, so the IO pattern of any call
;; sequence is deterministic and the gate mirror reproduces it byte for
;; byte. No write path exists anywhere in this file.
;;
;; Error discipline (the blk self test convention): every unexpected
;; condition is a signed EAX status, never a panic, never a hang. Chain
;; corruption (out of range cluster, bad cluster marker, reserved chain
;; value, hop budget exhaustion = loop) is EIO; a missing name is
;; ENOENT; a type or access violation is EPERM; bad arguments are
;; EINVAL. DF must be 0 on entry (the kernel entry and every self test
;; execute CLD); IF is never touched here.
;;

fs_code_start:

; ------------------------------------------------------------------
; single sector cache fetch
; in
; rcx = LBA
; out
; eax = FS_OK or the blk_read status; the sector sits in fs_sec_cache
; a hit issues no device traffic; every completed miss bumps
; fs_test_reads exactly once
; ------------------------------------------------------------------

fs_sec_read:
        fs_enter
        mov     rbx, rcx
        cmp     qword [fs_sec_valid], 0
        je      .miss
        cmp     rbx, [fs_sec_tag]
        je      .hit
.miss:
        mov     rdx, fs_sec_cache
        mov     r8d, 1
        call    blk_read
        test    eax, eax
        jnz     .out
        inc     qword [fs_test_reads]
        mov     [fs_sec_tag], rbx
        mov     qword [fs_sec_valid], 1
.hit:
        xor     eax, eax
.out:
        fs_leave
        ret

; ------------------------------------------------------------------
; FAT16 entry lookup in the init cached first FAT, a leaf
; in
; rcx = cluster number
; out
; eax = FS_OK with edx = the u16 entry, or FS_EIO (out of range)
; ------------------------------------------------------------------

fs_fat_entry:
        cmp     rcx, 2
        jb      .eio
        mov     rax, [fs_clusters]
        add     rax, 2
        cmp     rcx, rax
        jae     .eio
        mov     rax, fs_fat_cache
        movzx   edx, word [rax + rcx*2]
        xor     eax, eax
        ret
.eio:
        mov     eax, FS_EIO
        ret

; ------------------------------------------------------------------
; cluster number -> first LBA, a leaf (no range check, callers guard)
; in
; rcx = cluster number
; out
; rax = data_lba + (cluster - 2) * spc
; ------------------------------------------------------------------

fs_cluster_lba:
        mov     rax, rcx
        sub     rax, 2
        imul    rax, [fs_spc]
        add     rax, [fs_data_lba]
        ret

; ------------------------------------------------------------------
; one path component -> the 11 byte uppercased space padded 8.3 name
; in
; rcx = component bytes (not NUL terminated), rdx = length
; out
; eax = FS_OK (fs_name_buf holds the name) or FS_EINVAL
; "." and ".." map to their literal on-disk names, exactly like the
; oracle; a leaf using only caller saved registers
; ------------------------------------------------------------------

fs_norm_comp:
        mov     r9, rcx                 ; component bytes
        mov     r10, rdx                ; length
        mov     r8, fs_name_buf
        mov     ecx, 11
        mov     al, ' '
.fill:
        mov     [r8], al
        inc     r8
        dec     ecx
        jnz     .fill
        cmp     r10, 1
        jne     .notdot
        cmp     byte [r9], '.'
        jne     .notdot
        mov     byte [fs_name_buf], '.'
        xor     eax, eax
        ret
.notdot:
        cmp     r10, 2
        jne     .bounds
        cmp     word [r9], '..'
        jne     .bounds
        mov     word [fs_name_buf], '..'
        xor     eax, eax
        ret
.bounds:
        test    r10, r10
        jz      .einval
        cmp     r10, FS_COMP_MAX
        ja      .einval
        ; locate the single dot, a second one is EINVAL
        mov     r11, -1                 ; dot index
        xor     ecx, ecx
.dot:
        cmp     rcx, r10
        jae     .dotdone
        cmp     byte [r9 + rcx], '.'
        jne     .dotnext
        cmp     r11, -1
        jne     .einval
        mov     r11, rcx
.dotnext:
        inc     rcx
        jmp     .dot
.dotdone:
        mov     r8, r10                 ; base length
        xor     edx, edx                ; extension length
        cmp     r11, -1
        je      .lens
        mov     r8, r11
        mov     rdx, r10
        sub     rdx, r11
        dec     rdx
.lens:
        test    r8, r8
        jz      .einval
        cmp     r8, 8
        ja      .einval
        cmp     rdx, 3
        ja      .einval
        xor     ecx, ecx
.base:
        cmp     rcx, r8
        jae     .ext
        mov     al, [r9 + rcx]
        cmp     al, 'a'
        jb      .bstore
        cmp     al, 'z'
        ja      .bstore
        sub     al, 32
.bstore:
        mov     [fs_name_buf + rcx], al
        inc     rcx
        jmp     .base
.ext:
        xor     ecx, ecx
.eloop:
        cmp     rcx, rdx
        jae     .ok
        mov     rax, r11
        inc     rax
        add     rax, rcx
        mov     al, [r9 + rax]
        cmp     al, 'a'
        jb      .estore
        cmp     al, 'z'
        ja      .estore
        sub     al, 32
.estore:
        mov     [fs_name_buf + 8 + rcx], al
        inc     rcx
        jmp     .eloop
.ok:
        xor     eax, eax
        ret
.einval:
        mov     eax, FS_EINVAL
        ret

; ------------------------------------------------------------------
; linear directory scan for one 11 byte name
; in
; rcx = directory cluster (0 = the fixed root area), rdx = 11 byte name
; out
; eax = FS_OK (the raw 32 byte entry sits in fs_ent_buf), FS_ENOENT,
;       FS_EIO (chain corruption) or the sector fetch status
; scan rules: 0x00 ends the directory, 0xE5 slots are skipped, volume
; labels and LFN slots are skipped, a leading 0x05 compares as 0xE5.
; the subdirectory chain walk carries a hop budget of fs_clusters: a
; loop can never spin longer than the cluster count.
; ------------------------------------------------------------------

fs_dir_find:
        fs_enter
        mov     rbp, rdx                ; the 11 byte name
        mov     rbx, rcx                ; directory cluster, 0 = root
        test    rbx, rbx
        jz      .root
        mov     r12, [fs_clusters]      ; hop budget, the cycle guard
.chain:
        cmp     rbx, 2
        jb      .eio
        mov     rax, [fs_clusters]
        add     rax, 2
        cmp     rbx, rax
        jae     .eio
        mov     rcx, rbx
        call    fs_cluster_lba
        mov     r14, rax                ; sector cursor
        mov     r13, [fs_spc]           ; sectors left in this cluster
        jmp     .sec
.root:
        mov     r14, [fs_root_lba]
        mov     r13, [fs_root_sectors]
.sec:
        mov     rcx, r14
        call    fs_sec_read
        test    eax, eax
        jnz     .out
        mov     rdi, fs_sec_cache
        mov     r15d, FS_ENTRIES_PER_SECTOR
.slot:
        mov     al, [rdi]
        test    al, al
        jz      .enoent
        cmp     al, FS_DIR_DELETED
        je      .next
        mov     al, [rdi + 11]
        test    al, FS_ATTR_VOLUME_ID
        jnz     .next
        and     al, FS_ATTR_LFN
        cmp     al, FS_ATTR_LFN
        je      .next
        xor     ecx, ecx
.cmp:
        mov     al, [rdi + rcx]
        test    ecx, ecx
        jnz     .nocanji
        cmp     al, FS_DIR_KANJI
        jne     .nocanji
        mov     al, FS_DIR_DELETED
.nocanji:
        cmp     al, [rbp + rcx]
        jne     .next
        inc     ecx
        cmp     ecx, 11
        jb      .cmp
        ; match: publish the raw 32 byte entry
        mov     rsi, rdi
        mov     rdi, fs_ent_buf
        mov     ecx, FS_ENTRY_SIZE
        rep movsb
        xor     eax, eax
        jmp     .out
.next:
        add     rdi, FS_ENTRY_SIZE
        dec     r15d
        jnz     .slot
        inc     r14
        dec     r13
        jnz     .sec
        test    rbx, rbx
        jz      .enoent                 ; the root area is exhausted
        dec     r12
        jz      .eio                    ; hop budget spent: chain loop
        mov     rcx, rbx
        call    fs_fat_entry
        test    eax, eax
        jnz     .out
        cmp     edx, FS_FAT16_EOC_MIN
        jae     .enoent
        cmp     edx, FS_FAT16_BAD
        je      .eio
        cmp     edx, 2
        jb      .eio
        mov     rax, [fs_clusters]
        add     rax, 2
        cmp     rdx, rax
        jae     .eio
        mov     rbx, rdx
        jmp     .chain
.enoent:
        mov     eax, FS_ENOENT
        jmp     .out
.eio:
        mov     eax, FS_EIO
.out:
        fs_leave
        ret

; ------------------------------------------------------------------
; absolute path -> the raw 32 byte entry in fs_ent_buf
; in
; rcx = NUL terminated path, at most FS_PATH_MAX bytes
; out
; eax = FS_OK, FS_ENOENT, FS_EPERM (a file used as a directory),
;       FS_EINVAL (overlong path/component) or FS_EFAULT (null)
; empty components are skipped, matching is case insensitive, "/" and
; "" resolve to a synthesized root directory entry (attr 0x10, size 0)
; ------------------------------------------------------------------

fs_resolve:
        fs_enter
        test    rcx, rcx
        jz      .efault
        mov     rax, rcx
        xor     edx, edx
.len:
        cmp     byte [rax], 0
        je      .lenok
        inc     rax
        inc     edx
        cmp     edx, FS_PATH_MAX
        ja      .einval
        jmp     .len
.lenok:
        mov     r12, rcx                ; path cursor
        xor     ebx, ebx                ; current directory cluster
        xor     r15d, r15d              ; any component matched yet
.comp:
        mov     al, [r12]
        test    al, al
        jz      .done
        cmp     al, '/'
        jne     .start
        inc     r12
        jmp     .comp
.start:
        mov     r13, r12
        xor     r14d, r14d
.scan:
        mov     al, [r12]
        test    al, al
        jz      .endcomp
        cmp     al, '/'
        je      .endcomp
        inc     r12
        inc     r14
        cmp     r14, FS_COMP_MAX
        ja      .einval
        jmp     .scan
.endcomp:
        mov     rcx, r13
        mov     rdx, r14
        call    fs_norm_comp
        test    eax, eax
        jnz     .out
        mov     rcx, rbx
        mov     rdx, fs_name_buf
        call    fs_dir_find
        test    eax, eax
        jnz     .out
        mov     r15d, 1
        cmp     byte [r12], 0
        je      .done
        test    byte [fs_ent_buf + 11], FS_ATTR_DIRECTORY
        jz      .eperm
        movzx   ebx, word [fs_ent_buf + 26]
        jmp     .comp
.done:
        test    r15d, r15d
        jnz     .ok
        ; the root itself: synthesize a directory entry
        mov     rdi, fs_ent_buf
        xor     eax, eax
        mov     ecx, 4
        rep stosq
        mov     byte [fs_ent_buf + 11], FS_ATTR_DIRECTORY
.ok:
        xor     eax, eax
        jmp     .out
.eperm:
        mov     eax, FS_EPERM
        jmp     .out
.einval:
        mov     eax, FS_EINVAL
        jmp     .out
.efault:
        mov     eax, FS_EFAULT
.out:
        fs_leave
        ret

; ------------------------------------------------------------------
; fs_open(path, flags) -> fd
; in
; rcx = path, edx = flags (only FS_O_RDONLY is legal, write modes are
;       refused with EPERM: this is a read-only mount)
; out
; eax = FS_OK with edx = fd 0..FS_MAX_FDS-1, or a negative status
; ------------------------------------------------------------------

fs_open:
        fs_enter
        cmp     qword [fs_ready], 0
        je      .enotready
        test    rcx, rcx
        jz      .efault
        mov     eax, edx
        and     eax, FS_O_ACCMODE
        jnz     .eperm
        call    fs_resolve
        test    eax, eax
        jnz     .out
        mov     r8, fs_fd_table
        xor     r9d, r9d
.slot:
        cmp     qword [r8 + FS_FD_IN_USE], 0
        je      .found
        add     r8, FS_FD_SIZE
        inc     r9d
        cmp     r9d, FS_MAX_FDS
        jb      .slot
        mov     eax, FS_ENOMEM
        jmp     .out
.found:
        mov     qword [r8 + FS_FD_IN_USE], 1
        xor     edx, edx
        test    byte [fs_ent_buf + 11], FS_ATTR_DIRECTORY
        setne   dl
        mov     [r8 + FS_FD_IS_DIR], rdx
        movzx   edx, word [fs_ent_buf + 26]
        mov     [r8 + FS_FD_CLUSTER], rdx
        mov     edx, [fs_ent_buf + 28]
        mov     [r8 + FS_FD_SIZE_OFF], rdx
        mov     qword [r8 + FS_FD_POS], 0
        mov     qword [r8 + 40], 0
        mov     qword [r8 + 48], 0
        mov     qword [r8 + 56], 0
        mov     edx, r9d
        xor     eax, eax
        jmp     .out
.enotready:
        mov     eax, FS_ENOTREADY
        jmp     .out
.eperm:
        mov     eax, FS_EPERM
        jmp     .out
.efault:
        mov     eax, FS_EFAULT
.out:
        fs_leave
        ret

; ------------------------------------------------------------------
; fs_read(fd, buf, len) -> bytes read
; in
; ecx = fd, rdx = buffer, r8 = length (signed: negative is EINVAL)
; out
; eax = FS_OK with edx = the clamped byte count (0 at or past EOF),
;       or a negative status
; the cluster chain is walked from the file start to fd.pos, then the
; payload is copied sector by sector through the single sector cache;
; a chain shorter than the directory size is EIO, never a short read
; ------------------------------------------------------------------

fs_read:
        fs_enter
        sub     rsp, 16                 ; [rsp+0] base LBA, [rsp+8] n
        cmp     ecx, FS_MAX_FDS
        jae     .einval
        mov     eax, ecx
        shl     eax, 6
        mov     rbx, fs_fd_table
        add     rbx, rax
        cmp     qword [rbx + FS_FD_IN_USE], 0
        je      .einval
        cmp     qword [rbx + FS_FD_IS_DIR], 0
        jne     .eperm
        test    r8, r8
        js      .einval
        jz      .zero
        test    rdx, rdx
        jz      .efault
        mov     rbp, rdx                ; buffer cursor
        mov     r13, [rbx + FS_FD_POS]  ; position
        mov     r14, [rbx + FS_FD_SIZE_OFF]
        cmp     r13, r14
        jae     .zero                   ; at or past EOF
        mov     r12, r8                 ; bytes left
        mov     rax, r14
        sub     rax, r13
        cmp     r12, rax
        cmova   r12, rax                ; n = min(len, size - pos)
        mov     [rsp + 8], r12
        mov     r15, [fs_spc]
        shl     r15, FS_SECTOR_SHIFT    ; cluster bytes
        mov     rsi, [rbx + FS_FD_CLUSTER]
        xor     edi, edi                ; file offset of the cluster base
        cmp     rsi, 2
        jb      .eio
        mov     rax, [fs_clusters]
        add     rax, 2
        cmp     rsi, rax
        jae     .eio
.adv:
        mov     rax, rdi
        add     rax, r15
        cmp     r13, rax
        jb      .have
        mov     rcx, rsi
        call    fs_fat_entry
        test    eax, eax
        jnz     .out
        cmp     edx, FS_FAT16_EOC_MIN
        jae     .eio                    ; chain shorter than the size
        cmp     edx, FS_FAT16_BAD
        je      .eio
        cmp     edx, 2
        jb      .eio
        mov     rax, [fs_clusters]
        add     rax, 2
        cmp     rdx, rax
        jae     .eio
        mov     rsi, rdx
        add     rdi, r15
        jmp     .adv
.have:
        mov     rcx, rsi
        call    fs_cluster_lba
        mov     [rsp + 0], rax
.copy:
        mov     rax, r13
        sub     rax, rdi
        mov     rcx, rax
        shr     rcx, FS_SECTOR_SHIFT
        add     rcx, [rsp + 0]
        call    fs_sec_read
        test    eax, eax
        jnz     .out
        mov     rax, r13
        sub     rax, rdi
        and     eax, FS_SECTOR_SIZE - 1
        mov     r9d, FS_SECTOR_SIZE
        sub     r9d, eax
        cmp     r12, r9
        cmovb   r9, r12
        mov     r10, r15
        sub     r10, rax
        cmp     r9, r10
        cmova   r9, r10                 ; chunk within sector/cluster/n
        mov     rdx, fs_sec_cache
        add     rdx, rax
        mov     rcx, r9
.cbyte:
        mov     al, [rdx]
        mov     [rbp], al
        inc     rdx
        inc     rbp
        dec     rcx
        jnz     .cbyte
        add     r13, r9
        sub     r12, r9
        test    r12, r12
        jz      .done
        mov     rax, r13
        sub     rax, rdi
        cmp     rax, r15
        jb      .copy
        ; the span crosses into the next cluster
        mov     rcx, rsi
        call    fs_fat_entry
        test    eax, eax
        jnz     .out
        cmp     edx, FS_FAT16_EOC_MIN
        jae     .eio
        cmp     edx, FS_FAT16_BAD
        je      .eio
        cmp     edx, 2
        jb      .eio
        mov     rax, [fs_clusters]
        add     rax, 2
        cmp     rdx, rax
        jae     .eio
        mov     rsi, rdx
        mov     rdi, r13
        mov     rcx, rsi
        call    fs_cluster_lba
        mov     [rsp + 0], rax
        jmp     .copy
.done:
        mov     [rbx + FS_FD_POS], r13
        mov     rdx, [rsp + 8]
        sub     rdx, r12
        xor     eax, eax
        jmp     .out
.zero:
        xor     edx, edx
        xor     eax, eax
        jmp     .out
.einval:
        mov     eax, FS_EINVAL
        jmp     .out
.eperm:
        mov     eax, FS_EPERM
        jmp     .out
.efault:
        mov     eax, FS_EFAULT
        jmp     .out
.eio:
        mov     eax, FS_EIO
.out:
        add     rsp, 16
        fs_leave
        ret

; ------------------------------------------------------------------
; fs_seek(fd, off, whence) -> the new absolute offset
; in
; ecx = fd, rdx = signed offset, r8d = FS_SEEK_SET/CUR/END
; out
; eax = FS_OK with edx = the new offset, or FS_EINVAL
; seeking past EOF is legal (a later read returns 0); a negative
; result is EINVAL; directory fds refuse seeks
; ------------------------------------------------------------------

fs_seek:
        fs_enter
        cmp     ecx, FS_MAX_FDS
        jae     .einval
        mov     eax, ecx
        shl     eax, 6
        mov     rbx, fs_fd_table
        add     rbx, rax
        cmp     qword [rbx + FS_FD_IN_USE], 0
        je      .einval
        cmp     qword [rbx + FS_FD_IS_DIR], 0
        jne     .einval
        cmp     r8d, FS_SEEK_END
        ja      .einval
        mov     rax, rdx
        test    r8d, r8d
        jz      .store
        cmp     r8d, FS_SEEK_CUR
        jne     .end
        add     rax, [rbx + FS_FD_POS]
        jmp     .store
.end:
        add     rax, [rbx + FS_FD_SIZE_OFF]
.store:
        test    rax, rax
        js      .einval
        mov     [rbx + FS_FD_POS], rax
        mov     rdx, rax
        xor     eax, eax
        jmp     .out
.einval:
        mov     eax, FS_EINVAL
.out:
        fs_leave
        ret

; ------------------------------------------------------------------
; fs_close(fd)
; in
; ecx = fd
; out
; eax = FS_OK or FS_EINVAL (never used / out of range slot)
; ------------------------------------------------------------------

fs_close:
        fs_enter
        cmp     ecx, FS_MAX_FDS
        jae     .einval
        mov     eax, ecx
        shl     eax, 6
        mov     rbx, fs_fd_table
        add     rbx, rax
        cmp     qword [rbx + FS_FD_IN_USE], 0
        je      .einval
        mov     qword [rbx + FS_FD_IN_USE], 0
        xor     eax, eax
        jmp     .out
.einval:
        mov     eax, FS_EINVAL
.out:
        fs_leave
        ret

; ------------------------------------------------------------------
; fs_stat(path, out)
; in
; rcx = path, rdx = FS_STAT_SIZE byte output buffer
; out
; eax = FS_OK or a negative status; on success size qword@0, attr
; dword@8, the remaining 52 bytes are zero
; ------------------------------------------------------------------

fs_stat:
        fs_enter
        cmp     qword [fs_ready], 0
        je      .enotready
        test    rcx, rcx
        jz      .efault
        test    rdx, rdx
        jz      .efault
        mov     rbx, rdx
        call    fs_resolve
        test    eax, eax
        jnz     .out
        mov     rdi, rbx
        xor     eax, eax
        mov     ecx, FS_STAT_SIZE / 8
        rep stosq
        mov     eax, [fs_ent_buf + 28]
        mov     [rbx], rax
        movzx   eax, byte [fs_ent_buf + 11]
        mov     [rbx + 8], rax
        xor     eax, eax
        jmp     .out
.enotready:
        mov     eax, FS_ENOTREADY
        jmp     .out
.efault:
        mov     eax, FS_EFAULT
.out:
        fs_leave
        ret

; ------------------------------------------------------------------
; fs_readdir(fd, buf, len) -> fixed 32 byte entries
; in
; ecx = fd (a directory), rdx = buffer, r8 = length (>= FS_DIRENT_SIZE)
; out
; eax = FS_OK with edx = the emitted byte count (a multiple of 32,
;       0 at the end of the directory), or a negative status
; fd.pos is the raw 32 byte slot index; deleted/volume/LFN slots and
; the "." ".." entries are skipped, 0x00 ends the directory and parks
; pos at the -1 sentinel so every later call returns 0 without IO
; ------------------------------------------------------------------

fs_readdir:
        fs_enter
        sub     rsp, 16                 ; [rsp+0] entry pointer
        cmp     ecx, FS_MAX_FDS
        jae     .einval
        mov     eax, ecx
        shl     eax, 6
        mov     rbx, fs_fd_table
        add     rbx, rax
        cmp     qword [rbx + FS_FD_IN_USE], 0
        je      .einval
        cmp     qword [rbx + FS_FD_IS_DIR], 0
        je      .eperm
        cmp     r8, FS_DIRENT_SIZE
        jb      .einval
        test    rdx, rdx
        jz      .efault
        mov     rbp, rdx                ; output cursor
        mov     rax, r8
        xor     edx, edx
        mov     rcx, FS_DIRENT_SIZE
        div     rcx
        imul    r13, rax, FS_DIRENT_SIZE ; byte capacity
        xor     r12d, r12d              ; bytes written
        mov     r14, [rbx + FS_FD_POS]  ; raw slot index
        mov     rsi, [rbx + FS_FD_CLUSTER]
        xor     edi, edi                ; cluster base slot index
.loop:
        cmp     r12, r13
        jae     .finish
        cmp     r14, -1
        je      .finish
        test    rsi, rsi
        jz      .root
        ; subdirectory: walk the chain to the cluster holding the slot
        mov     r8, [fs_spc]
        shl     r8, 4                   ; slots per cluster
.adv:
        mov     rax, rdi
        add     rax, r8
        cmp     r14, rax
        jb      .have
        mov     rcx, rsi
        call    fs_fat_entry
        test    eax, eax
        jnz     .out
        cmp     edx, FS_FAT16_EOC_MIN
        jae     .endmark
        cmp     edx, FS_FAT16_BAD
        je      .eio
        cmp     edx, 2
        jb      .eio
        mov     rax, [fs_clusters]
        add     rax, 2
        cmp     rdx, rax
        jae     .eio
        mov     rsi, rdx
        add     rdi, r8
        jmp     .adv
.have:
        cmp     rsi, 2
        jb      .eio
        mov     rax, [fs_clusters]
        add     rax, 2
        cmp     rsi, rax
        jae     .eio
        mov     rcx, rsi
        call    fs_cluster_lba
        mov     rcx, r14
        sub     rcx, rdi
        shr     rcx, 4                  ; sector inside the cluster
        add     rcx, rax
        jmp     .sector
.root:
        cmp     r14, [fs_root_entries]
        jae     .endmark
        mov     rcx, r14
        shr     rcx, 4
        add     rcx, [fs_root_lba]
.sector:
        call    fs_sec_read
        test    eax, eax
        jnz     .out
        mov     rax, r14
        and     eax, FS_ENTRIES_PER_SECTOR - 1
        shl     eax, FS_ENTRY_SHIFT
        mov     rdx, fs_sec_cache
        add     rdx, rax
        mov     [rsp + 0], rdx
        mov     al, [rdx]
        test    al, al
        jz      .endmark
        cmp     al, FS_DIR_DELETED
        je      .nextslot
        mov     al, [rdx + 11]
        test    al, FS_ATTR_VOLUME_ID
        jnz     .nextslot
        and     al, FS_ATTR_LFN
        cmp     al, FS_ATTR_LFN
        je      .nextslot
        cmp     byte [rdx], '.'
        jne     .emit
        mov     al, [rdx + 1]
        cmp     al, ' '
        je      .nextslot
        cmp     al, '.'
        je      .nextslot
.emit:
        movzx   eax, byte [rdx + 11]
        mov     [rbp], rax              ; attr u32 plus the pad qword
        mov     eax, [rdx + 28]
        mov     [rbp + 8], rax          ; size u64
        xor     ecx, ecx
.name:
        mov     al, [rdx + rcx]
        test    ecx, ecx
        jnz     .nstore
        cmp     al, FS_DIR_KANJI
        jne     .nstore
        mov     al, FS_DIR_DELETED
.nstore:
        mov     [rbp + 16 + rcx], al
        inc     ecx
        cmp     ecx, 11
        jb      .name
        mov     dword [rbp + 27], 0
        mov     byte [rbp + 31], 0
        add     rbp, FS_DIRENT_SIZE
        add     r12, FS_DIRENT_SIZE
.nextslot:
        inc     r14
        jmp     .loop
.endmark:
        mov     r14, -1
.finish:
        mov     [rbx + FS_FD_POS], r14
        mov     rdx, r12
        xor     eax, eax
        jmp     .out
.einval:
        mov     eax, FS_EINVAL
        jmp     .out
.eperm:
        mov     eax, FS_EPERM
        jmp     .out
.efault:
        mov     eax, FS_EFAULT
        jmp     .out
.eio:
        mov     eax, FS_EIO
.out:
        add     rsp, 16
        fs_leave
        ret

; ------------------------------------------------------------------
; fs_init: mount the single read-only FAT16 volume from LBA0
; in
; nothing; blk_init must have succeeded
; out
; eax = FS_OK (fs_ready = 1, geometry published, first FAT cached),
;       FS_ENOTREADY (no blk backend) or FS_EIO (illegal BPB/geometry,
;       fs_ready stays 0: the self test failure path, never a panic)
; the kernel supports 512 byte sectors and spf <= FS_FAT_CACHE_SECTORS
; only; both limits are reported deviations, the frozen builder image
; sits inside them
; ------------------------------------------------------------------

fs_init:
        fs_enter
        cld
        cmp     qword [blk_ready], 0
        je      .enotready
        xor     ecx, ecx
        mov     rdx, fs_bpb_buf
        mov     r8d, 1
        call    blk_read
        test    eax, eax
        jnz     .eio
        inc     qword [fs_test_reads]
        ; BPB validation, every failure is EIO with fs_ready left 0
        cmp     word [fs_bpb_buf + FS_BPB_SIG], FS_BPB_SIG_VALUE
        jne     .eio
        cmp     word [fs_bpb_buf + FS_BPB_BPS], FS_SECTOR_SIZE
        jne     .eio
        movzx   eax, byte [fs_bpb_buf + FS_BPB_SPC]
        test    eax, eax
        jz      .eio
        cmp     eax, 128
        ja      .eio
        mov     edx, eax
        dec     edx
        test    eax, edx
        jnz     .eio                    ; a power of two
        mov     [fs_spc], rax
        movzx   eax, word [fs_bpb_buf + FS_BPB_RESERVED]
        test    eax, eax
        jz      .eio
        mov     [fs_reserved], rax
        movzx   eax, byte [fs_bpb_buf + FS_BPB_NFATS]
        test    eax, eax
        jz      .eio
        mov     [fs_nfats], rax
        movzx   eax, word [fs_bpb_buf + FS_BPB_ROOT_ENTRIES]
        test    eax, eax
        jz      .eio
        mov     edx, eax
        shl     edx, FS_ENTRY_SHIFT
        test    edx, FS_SECTOR_SIZE - 1
        jnz     .eio                    ; whole sector root area
        mov     [fs_root_entries], rax
        movzx   eax, word [fs_bpb_buf + FS_BPB_SPF]
        test    eax, eax
        jz      .eio
        cmp     eax, FS_FAT_CACHE_SECTORS
        ja      .eio
        mov     [fs_spf], rax
        movzx   eax, word [fs_bpb_buf + FS_BPB_TOTAL16]
        test    eax, eax
        jnz     .total
        mov     eax, [fs_bpb_buf + FS_BPB_TOTAL32]
.total:
        test    eax, eax
        jz      .eio
        cmp     rax, [blk_capacity]
        ja      .eio
        mov     [fs_total], rax
        mov     rdx, [fs_root_entries]
        shl     rdx, FS_ENTRY_SHIFT
        shr     rdx, FS_SECTOR_SHIFT
        mov     [fs_root_sectors], rdx
        mov     rdx, [fs_reserved]
        mov     [fs_fat_lba], rdx
        mov     rcx, [fs_nfats]
        imul    rcx, [fs_spf]
        add     rcx, rdx
        mov     [fs_root_lba], rcx
        add     rcx, [fs_root_sectors]
        mov     [fs_data_lba], rcx
        cmp     rcx, rax
        jae     .eio                    ; metadata consumes the image
        sub     rax, rcx
        xor     edx, edx
        div     qword [fs_spc]
        cmp     rax, FS_FAT16_MIN_CLUSTERS
        jb      .eio
        cmp     rax, FS_FAT16_MAX_CLUSTERS
        ja      .eio
        ; task 138 W2: the cached FAT must cover every cluster entry,
        ; clusters + 2 <= spf * 256; a corrupt BPB with a small spf and
        ; a large cluster count would let fs_fat_entry index past the
        ; 16 KiB cache, so the geometry is refused here with EIO
        mov     rdx, [fs_spf]
        shl     rdx, 8
        mov     rcx, rax
        add     rcx, 2
        cmp     rcx, rdx
        ja      .eio
        mov     [fs_clusters], rax
        ; cache the whole first FAT, one bounded blk_read
        mov     rcx, [fs_fat_lba]
        mov     rdx, fs_fat_cache
        mov     r8, [fs_spf]
        call    blk_read
        test    eax, eax
        jnz     .eio
        inc     qword [fs_test_reads]
        mov     qword [fs_ready], 1
        xor     eax, eax
        jmp     .out
.enotready:
        mov     eax, FS_ENOTREADY
        jmp     .out
.eio:
        mov     eax, FS_EIO
.out:
        fs_leave
        ret