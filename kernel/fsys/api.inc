;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2026. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; FS-v1 M3 (task 133) contract of the fsys module: the file syscall
;; adapter over the frozen M2 fs API, the user pointer safety rules
;; and the Ring 3 fsprobe leg.
;;
;; The syscall numbers 4..9 and the K64 errno set are frozen by the
;; task contract; the FSYS section appended to kernel/core/contracts.inc
;; mirrors every constant below and cross checks it at assembly time,
;; exactly like the CPU/MM module copies. This file is self contained:
;; the cross checks against cpu/mm/fs symbols only run when those
;; modules were included first (the kernel and the stage2-fsys probe
;; both include cpu, mm, blk and fs before fsys).
;;
;; Error mapping, frozen: the fs module reports FS_E* statuses in EAX;
;; the adapter maps them onto the five code K64 set, never the raw
;; 32-bit values. Index = -status into fsys_errmap:
;;   FS_OK        ->  0          FS_EPERM     -> ENOTSUP (read-only v1)
;;   FS_EINVAL    -> EINVAL      FS_ENOTREADY -> EIO
;;   FS_ENOMEM    -> EIO         FS_EALREADY  -> EINVAL
;;   FS_ENOENT    -> ENOENT      FS_EIO       -> EIO
;;   FS_EEXIST    -> EINVAL      FS_EOVERFLOW -> EINVAL
;;   FS_ENOTSUP   -> ENOTSUP     FS_EFAULT    -> EFAULT
;;   FS_EBUSY     -> EIO         anything else-> EIO
;;

; ------------------------------------------------------------------
; syscall numbers, frozen FS-v1 M3 (0..3 stay the frozen APP-v1 set)
; ------------------------------------------------------------------

FSYS_SYS_EXIT    = 0            ; probe-private exit, never SYSRETs
FSYS_SYS_OPEN    = 4
FSYS_SYS_READ    = 5
FSYS_SYS_CLOSE   = 6
FSYS_SYS_SEEK    = 7
FSYS_SYS_STAT    = 8
FSYS_SYS_READDIR = 9

; the frozen K64 errno set plus the module-private probe statuses
FSYS_OK          = 0
FSYS_EINVAL      = -1
FSYS_ENOMEM      = -2
FSYS_ENOENT      = -3
FSYS_EPERM       = -5
FSYS_ENOTSUP     = -6
FSYS_ENOTREADY   = -7
FSYS_EALREADY    = -8
FSYS_EIO         = -9
FSYS_EFAULT      = -11

; ------------------------------------------------------------------
; user VA domain mirror of the frozen mm constants (task 120 plan B);
; the cross checks below fire whenever mm/api.inc was included first
; ------------------------------------------------------------------

FSYS_USER_VA_BASE   = 0x100400000
FSYS_USER_VA_LIMIT  = 0x100800000
FSYS_USER_CODE_BASE = 0x100400000
FSYS_USER_CODE_SIZE = 0x4000
FSYS_USER_DATA_BASE = 0x100600000
FSYS_USER_STACK_TOP = 0x100800000
FSYS_USER_STACK_SIZE = 0x4000

; the probe footprint: 4 code pages + 1 data page + 4 stack pages,
; and the frozen +3 table budget (1 PD + 2 PT, the identity PDPT slot
; PDPT[4] is reused, see the mm/api.inc adjudication note)
FSYS_CODE_PAGES   = 4
FSYS_DATA_PAGES   = 1
FSYS_STACK_PAGES  = 4
FSYS_PROBE_PAGES  = FSYS_CODE_PAGES + FSYS_DATA_PAGES + FSYS_STACK_PAGES
FSYS_TABLE_BUDGET = 3

; the fsprobe evidence page, mirrored from user/fsprobe/api.inc
FSYS_FP_MASK_ALL  = 0x7FF       ; eleven leg success bits
FSYS_FP_EVIDENCE  = 8           ; qwords read back from +16..+72

; ring 3 selectors of the frozen GDT plan A, loaded form
FSYS_USER_CS      = 0x33        ; 0x30 | 3
FSYS_USER_SS      = 0x2B        ; 0x28 | 3
FSYS_USER_RFLAGS  = 0x2         ; IF stays 0 for the whole probe

; ------------------------------------------------------------------
; data region layout, three pages: state, bounce, syscall stack
; ------------------------------------------------------------------

FSYS_DATA_PAGES_TOTAL = 3
FSYS_BOUNCE_SIZE      = 4096
FSYS_SYSCALL_STACK    = 4096

; ------------------------------------------------------------------
; assembly time cross checks against the modules included before fsys
; ------------------------------------------------------------------

if defined CPU_SYS_CALL_MAX
        if FSYS_SYS_READDIR >= CPU_SYS_CALL_MAX
                err 'CPU_SYS_CALL_MAX must cover the file syscalls'
        end if
        if CPU_FRAME_RDI <> 72 | CPU_FRAME_RSI <> 80 | CPU_FRAME_RDX <> 104
                err 'the fsys handlers read a different frame layout'
        end if
        if CPU_FRAME_RAX <> 120 | CPU_FRAME_SIZE <> 184
                err 'the fsys handlers read a different frame layout'
        end if
end if

if defined MM_USER_VA_BASE
        if FSYS_USER_VA_BASE <> MM_USER_VA_BASE | FSYS_USER_VA_LIMIT <> MM_USER_VA_LIMIT
                err 'the fsys domain mirror disagrees with mm'
        end if
        if FSYS_USER_CODE_BASE <> MM_USER_CODE_BASE | FSYS_USER_CODE_SIZE <> MM_USER_CODE_SIZE
                err 'the fsys code domain mirror disagrees with mm'
        end if
        if FSYS_USER_DATA_BASE <> MM_USER_DATA_BASE
                err 'the fsys data domain mirror disagrees with mm'
        end if
        if FSYS_TABLE_BUDGET <> MM_USER_TABLE_BUDGET
                err 'the fsys table budget disagrees with mm'
        end if
        if FSYS_USER_STACK_TOP <> MM_USER_VA_LIMIT | FSYS_USER_STACK_SIZE <> MM_USER_STACK_SIZE
                err 'the fsys stack mirror disagrees with mm'
        end if
end if

if defined FS_MAX_FDS
        if FS_MAX_FDS <> 8 | FS_PATH_MAX <> 128
                err 'the fsys adapter assumes the frozen fd table and path budget'
        end if
        if FS_STAT_SIZE <> 64 | FS_DIRENT_SIZE <> 32
                err 'the fsys adapter assumes the frozen stat/dirent sizes'
        end if
end if

if FSYS_PROBE_PAGES <> 9
        err 'the probe footprint is frozen at nine leaf pages'
end if
if FSYS_USER_STACK_TOP - FSYS_USER_STACK_SIZE < FSYS_USER_DATA_BASE
        err 'the probe stack overlaps the data domain base'
end if

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; register helper macros, the full frame of the other modules

macro fsys_enter
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

macro fsys_leave
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