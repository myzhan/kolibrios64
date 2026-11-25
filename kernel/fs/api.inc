;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; FS-v1 M2 read-only FAT16: the private copy of the shared handover
;; contract plus the frozen FS-v1 fs contract, task 132.
;;
;; The shared values are duplicated here with the FS_ prefix exactly
;; like BLK_: the assembly time checks keep the local copy self
;; consistent, core/contracts.inc proves the copies agree and the
;; stage2-fs gate compares everything against hand written expectations
;; and the independent oracle tests/stage2-fs/fat16_model.py.
;;
;; The module is a single read-only FAT16 mount over blk_read (8.3 short
;; names only, no LFN, no write path of any kind). It calls nothing
;; outside its own files except blk_read and the CPU console for the
;; self test failure report; it never panics and never executes STI.
;;
;; Calling convention (identical to the block layer):
;;  - Microsoft x64 integer ABI, RCX/RDX/R8/R9 arguments;
;;  - 32 bytes of shadow space, RSP mod 16 = 0 before a call, no red zone;
;;  - RBX/RBP/RSI/RDI/R12-R15 are callee saved;
;;  - EAX is a signed status, RDX is the main result, 0 is success;
;;  - DF = 0 and IF unchanged on return.
;;
;; Public API (M3 syscalls reuse exactly these):
;;   fs_open(rcx=path, edx=flags)      -> eax 0, edx = fd (0..7)
;;   fs_read(ecx=fd, rdx=buf, r8=len)  -> eax 0, edx = bytes read (0=EOF)
;;   fs_seek(ecx=fd, rdx=off, r8=whence)-> eax 0, edx = new absolute offset
;;   fs_close(ecx=fd)                  -> eax 0
;;   fs_stat(rcx=path, rdx=out64)      -> eax 0, size qword@0, attr dword@8
;;   fs_readdir(ecx=fd, rdx=buf, r8=len)-> eax 0, edx = bytes of 32B entries
;;   fs_init()                         -> eax 0, mounts LBA0 of the blk
;;   fs_selftest()                     -> eax 0, or EIO with rdx = leg mask
;;

use64

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; kernel virtual base and page geometry, the shared contract values

FS_KERNEL_BASE = 0xFFFFFFFF80000000
FS_PAGE_SIZE   = 0x1000

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; status codes, EAX is signed, the shared K64 spelling

FS_OK         = 0
FS_EINVAL     = -1
FS_ENOMEM     = -2
FS_ENOENT     = -3
FS_EEXIST     = -4
FS_EPERM      = -5
FS_ENOTSUP    = -6
FS_ENOTREADY  = -7
FS_EALREADY   = -8
FS_EIO        = -9
FS_EOVERFLOW  = -10
FS_EFAULT     = -11
FS_EBUSY      = -12

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; on-disk geometry, FS-v1 supports 512 byte sectors only (the frozen
; builder geometry); a foreign bytes-per-sector is an init EIO

FS_SECTOR_SIZE        = 512
FS_SECTOR_SHIFT       = 9
FS_ENTRY_SIZE         = 32
FS_ENTRY_SHIFT        = 5
FS_ENTRIES_PER_SECTOR = 16

if FS_SECTOR_SIZE <> 1 shl FS_SECTOR_SHIFT
        display "sector size and shift disagree", 13, 10
        err
end if
if FS_ENTRY_SIZE * FS_ENTRIES_PER_SECTOR <> FS_SECTOR_SIZE
        display "directory entries do not tile a sector", 13, 10
        err
end if

; BPB field offsets inside the boot sector
FS_BPB_BPS          = 11        ; u16 bytes per sector
FS_BPB_SPC          = 13        ; u8 sectors per cluster
FS_BPB_RESERVED     = 14        ; u16 reserved sectors
FS_BPB_NFATS        = 16        ; u8 FAT copies
FS_BPB_ROOT_ENTRIES = 17        ; u16 fixed root entry count
FS_BPB_TOTAL16      = 19        ; u16 total sectors (small images)
FS_BPB_SPF          = 22        ; u16 sectors per FAT, nonzero on FAT16
FS_BPB_TOTAL32      = 32        ; u32 total sectors (large images)
FS_BPB_SIG          = 510       ; u16 0xAA55
FS_BPB_SIG_VALUE    = 0xAA55

if FS_BPB_BPS >= FS_BPB_SPC | FS_BPB_SPC >= FS_BPB_RESERVED
        display "BPB offset map out of order", 13, 10
        err
end if
if FS_BPB_RESERVED >= FS_BPB_NFATS | FS_BPB_NFATS >= FS_BPB_ROOT_ENTRIES
        display "BPB offset map out of order", 13, 10
        err
end if
if FS_BPB_ROOT_ENTRIES >= FS_BPB_TOTAL16 | FS_BPB_TOTAL16 >= FS_BPB_SPF
        display "BPB offset map out of order", 13, 10
        err
end if
if FS_BPB_SPF >= FS_BPB_TOTAL32 | FS_BPB_TOTAL32 >= FS_BPB_SIG
        display "BPB offset map out of order", 13, 10
        err
end if

; FAT16 cluster count range and chain markers
FS_FAT16_MIN_CLUSTERS = 4085
FS_FAT16_MAX_CLUSTERS = 65524
FS_FAT16_BAD          = 0xFFF7
FS_FAT16_EOC_MIN      = 0xFFF8  ; any entry >= this ends the chain

if FS_FAT16_MIN_CLUSTERS >= FS_FAT16_MAX_CLUSTERS
        display "FAT16 cluster range inverted", 13, 10
        err
end if
if FS_FAT16_BAD >= FS_FAT16_EOC_MIN
        display "bad cluster marker inside the EOC range", 13, 10
        err
end if

; directory entry attributes and first byte markers
FS_ATTR_READ_ONLY = 0x01
FS_ATTR_VOLUME_ID = 0x08
FS_ATTR_LFN       = 0x0F
FS_ATTR_DIRECTORY = 0x10

FS_DIR_END     = 0x00           ; end of directory mark
FS_DIR_DELETED = 0xE5           ; deleted slot, skipped
FS_DIR_KANJI   = 0x05           ; leading 0xE5 escape, folded on compare

if FS_ATTR_LFN <> (FS_ATTR_READ_ONLY or FS_ATTR_VOLUME_ID or 0x02 or 0x04)
        display "LFN attribute mask is not the four low bits", 13, 10
        err
end if

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; fd table and request limits, all static module data

FS_MAX_FDS   = 8
FS_FD_SIZE   = 64
FS_PATH_MAX  = 128              ; NUL terminated path byte budget
FS_COMP_MAX  = 12               ; one 8.3 component: 8 + '.' + 3

; fd slot field offsets inside one FS_FD_SIZE slot
FS_FD_IN_USE   = 0
FS_FD_IS_DIR   = 8
FS_FD_CLUSTER  = 16
FS_FD_SIZE_OFF = 24
FS_FD_POS      = 32

if FS_FD_POS + 8 > FS_FD_SIZE
        display "fd slot fields overflow the slot", 13, 10
        err
end if
if FS_COMP_MAX <> 8 + 1 + 3
        display "8.3 component budget changed", 13, 10
        err
end if

; the whole first FAT is cached at init: spf above this is an init EIO
FS_FAT_CACHE_SECTORS = 32
if FS_FAT_CACHE_SECTORS * FS_SECTOR_SIZE <> 4 * FS_PAGE_SIZE
        display "FAT cache is not four whole pages", 13, 10
        err
end if

; open flags, FS-v1 is read only: any write access mode is EPERM
FS_O_RDONLY  = 0
FS_O_WRONLY  = 1
FS_O_RDWR    = 2
FS_O_ACCMODE = 3

; seek whence
FS_SEEK_SET = 0
FS_SEEK_CUR = 1
FS_SEEK_END = 2

; readdir fixed 32 byte entry: attr u32@0, pad@4, size u64@8, raw 8.3
; name@16, pad@27..31; stat output: size qword@0, attr dword@8, 52
; reserved zero bytes
FS_DIRENT_SIZE = 32
FS_STAT_SIZE   = 64

if FS_DIRENT_SIZE < 16 + 11
        display "dirent does not carry the raw 8.3 name", 13, 10
        err
end if
if FS_DIRENT_SIZE and (FS_DIRENT_SIZE - 1)
        display "dirent size is not a power of two", 13, 10
        err
end if

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; self test evidence, frozen against build/fs-v1/disk.img (8 MiB FAT16,
; sha256 e9e83dad3f6d5193ac15c7a35834cc5190ca34e978b537d80be9fa9beb7bc42b)
; and the --manifest BIG.BIN image (same geometry, one 9 cluster file,
; sha256 a810280509cd05f67dce37dc46e67cbfeb2e9432b73a105108f9a2d298500543)

FS_TEST_IBM_SIZE    = 132
FS_TEST_TETRIS_SIZE = 494
FS_TEST_BIG_SIZE    = 8229      ; 8 * 1024 + 37, nine clusters
FS_TEST_ROM_PREFIX  = 0x08610C602AA2E000        ; IBM.CH8 first 8 bytes
FS_TEST_BIG_PREFIX  = 0x342D261F18110A03        ; ((i*7+3)&0xFF), i=0..7

FS_TEST_SPC          = 2
FS_TEST_RESERVED     = 1
FS_TEST_NFATS        = 2
FS_TEST_SPF          = 32
FS_TEST_ROOT_ENTRIES = 512
FS_TEST_CLUSTERS     = 8143
FS_TEST_TOTAL        = 16384
FS_TEST_FAT_LBA      = 1
FS_TEST_ROOT_LBA     = 65
FS_TEST_DATA_LBA     = 97

if FS_TEST_FAT_LBA <> FS_TEST_RESERVED
        display "frozen FAT LBA disagrees with the reserved count", 13, 10
        err
end if
if FS_TEST_ROOT_LBA <> FS_TEST_RESERVED + FS_TEST_NFATS * FS_TEST_SPF
        display "frozen root LBA disagrees with the FAT area", 13, 10
        err
end if
if FS_TEST_DATA_LBA <> FS_TEST_ROOT_LBA + FS_TEST_ROOT_ENTRIES * FS_ENTRY_SIZE / FS_SECTOR_SIZE
        display "frozen data LBA disagrees with the root area", 13, 10
        err
end if
if FS_TEST_CLUSTERS <> (FS_TEST_TOTAL - FS_TEST_DATA_LBA) / FS_TEST_SPC
        display "frozen cluster count disagrees with the geometry", 13, 10
        err
end if

; leg failure bits of fs_test_mask
FS_ST_BPB    = 0x01             ; leg 0: geometry + BIG.BIN presence probe
FS_ST_IBM    = 0x02             ; leg 1: /roms/ibm.ch8 full read + EOF
FS_ST_TETRIS = 0x04             ; leg 2: /ROMS/TETRIS1.CH8 full read + EOF
FS_ST_SEEK   = 0x08             ; leg 3: seek/read boundaries on IBM.CH8
FS_ST_NEG    = 0x10             ; leg 4: refusal paths + readdir evidence
FS_ST_BIG    = 0x20             ; leg 5: BIG.BIN cross cluster spans
FS_ST_STAT   = 0x40             ; leg 6: stat size/attr/reserved zeros
FS_ST_COUNT  = 0x80             ; leg 7: sector fetch counter evidence

; completed fs sector fetches after a clean self test run: init BPB +
; init FAT + every single sector cache miss (22 plain, 46 BIG), derived
; by the stage2-fs gate mirror of the exact leg sequence; the matching
; blk_test_reads totals are 7 + 5 init chunks + misses = 34 / 58
FS_TEST_READS_FINAL     = 24
FS_TEST_READS_FINAL_BIG = 48

if FS_TEST_READS_FINAL_BIG <= FS_TEST_READS_FINAL
        display "BIG image must fetch more sectors than the plain one", 13, 10
        err
end if

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; register helper macros, the full frame of the other modules

macro fs_enter
        push    rbp
        push    rbx
        push    rsi
        push    rdi
        push    r12
        push    r13
        push    r14
        push    r15
        sub     rsp, 40
end macro

macro fs_leave
        add     rsp, 40
        pop     r15
        pop     r14
        pop     r13
        pop     r12
        pop     rdi
        pop     rsi
        pop     rbx
        pop     rbp
end macro