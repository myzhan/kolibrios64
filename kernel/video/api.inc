;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; GUI-G1 framebuffer video: the private copy of the shared handover
;; contract (K64-FOUNDATION-v1) plus the frozen G1 drawing surface.
;;
;; The contract is not kept in a shared file, so every fixed value the
;; module consumes is duplicated here with the VIDEO_ prefix. The checks
;; at the bottom of each block keep the local copy self consistent; the
;; layout tests compare the public values against hand written copies.
;;
;; Calling convention (Microsoft x64, integer registers only):
;;  - integer arguments in RCX, RDX, R8, R9, the fifth on the stack at
;;    [rsp+40] on entry, right after the return address and the 32 byte
;;    shadow space the caller homes;
;;  - the caller provides 32 bytes of shadow space and leaves RSP 16
;;    byte aligned before the call, so a body starts with RSP mod 16 = 8;
;;  - RBX, RBP, RSI, RDI and R12..R15 are callee saved;
;;  - EAX returns a signed status, 0 is success;
;;  - DF is clear on return, IF is never changed by a returning call.
;;
;; The module calls vmm_map_mmio from kernel/mm and nothing else outside
;; its own files: whoever includes video.asm must include mm.asm first.
;;
;; ------------------------------------------------------------------
;; Frozen GUI-G1 self test pattern, the forensic spec for the QEMU
;; screendump assertions of the integration task. Reference resolution
;; is exactly 1024x768x32bpp BGRX; video_selftest refuses any other
;; geometry so every coordinate below is absolute, never scaled.
;;
;;  layer 1  eight vertical bars, bar i covers x in [i*128, (i+1)*128),
;;           every y in [0,768), colors in this exact order:
;;             bar 0  x   0..127   0xFF0000  red
;;             bar 1  x 128..255   0x00FF00  green
;;             bar 2  x 256..383   0x0000FF  blue
;;             bar 3  x 384..511   0xFFFF00  yellow
;;             bar 4  x 512..639   0x00FFFF  cyan
;;             bar 5  x 640..767   0xFF00FF  magenta
;;             bar 6  x 768..895   0xFFFFFF  white
;;             bar 7  x 896..1023  0x000000  black
;;  layer 2  one pixel white border: x = 0, x = 1023, y = 0, y = 767,
;;           color 0xFFFFFF, drawn over the bars
;;  layer 3  centered rectangle x in [412,612), y in [284,484),
;;           200x200 pixels, color 0x808080, drawn over the border
;;           never overlaps (412 > 0 and 612 < 1023)
;;  layer 4  text "K64 GUI-G1" at (8,8), 8x16 font, color 0xFFFFFF,
;;           10 glyphs, 80x16 pixel cell, only glyph set bits are drawn
;;
;; Colors are 0x00RRGGBB; the framebuffer stores BGRX (B in the low
;; byte, X byte written as zero).
;; ------------------------------------------------------------------
;;

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; kernel virtual base, the shared const.inc value

VIDEO_KERNEL_BASE = 0xFFFFFFFF80000000

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; page geometry

VIDEO_PAGE_SHIFT = 12
VIDEO_PAGE_SIZE  = 0x1000

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; the MM public window, private copy of the MM contract values

VIDEO_WINDOW_BASE   = 0xFFFFC00000000000
VIDEO_WINDOW_LIMIT  = 0xFFFFC20000000000
VIDEO_HEAP_LIMIT    = 0xFFFFC00001000000
VIDEO_MMIO_LAPIC_VA = 0xFFFFC00002000000
VIDEO_CORE_TEST_SLOT = 386        ; PML4 slot of the core test region

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; the framebuffer mapping window
;
; FB_VA starts exactly at the heap limit, PML4 slot 384, and may grow up
; to (but never into) the LAPIC MMIO VA of the IRQ module. That caps the
; framebuffer at 16 MiB; the 1024x768x4 = 3 MiB handover maps 768 pages.

VIDEO_FB_VA        = 0xFFFFC00001000000
VIDEO_FB_VA_LIMIT  = VIDEO_MMIO_LAPIC_VA
VIDEO_FB_MAX_SIZE  = VIDEO_FB_VA_LIMIT - VIDEO_FB_VA
VIDEO_FB_MAX_PAGES = VIDEO_FB_MAX_SIZE / VIDEO_PAGE_SIZE

; the largest geometry G1 accepts, the mapping budget must cover it
VIDEO_MAX_WIDTH  = 2560
VIDEO_MAX_HEIGHT = 1600
VIDEO_BPP        = 4              ; 32bpp only

if VIDEO_FB_VA <> VIDEO_HEAP_LIMIT
        display "FB VA must start exactly at the heap limit", 13, 10
        err
end if
if VIDEO_FB_VA mod VIDEO_PAGE_SIZE
        display "FB VA must be page aligned", 13, 10
        err
end if
if VIDEO_FB_VA < VIDEO_WINDOW_BASE | VIDEO_FB_VA_LIMIT > VIDEO_WINDOW_LIMIT
        display "FB window must stay inside the MM public window", 13, 10
        err
end if
if (VIDEO_FB_VA shr 47) <> 0x1FFFF
        display "FB window must be canonical", 13, 10
        err
end if
if ((VIDEO_FB_VA shr 39) and 0x1FF) <> 384
        display "FB VA must live in PML4 slot 384", 13, 10
        err
end if
if ((VIDEO_FB_VA shr 39) and 0x1FF) = VIDEO_CORE_TEST_SLOT
        display "FB VA must not use the core test slot", 13, 10
        err
end if
if VIDEO_FB_VA + VIDEO_FB_MAX_SIZE > VIDEO_MMIO_LAPIC_VA
        display "FB window must not reach the LAPIC MMIO VA", 13, 10
        err
end if
if VIDEO_FB_MAX_SIZE <> 0x1000000 | VIDEO_FB_MAX_PAGES <> 4096
        display "FB window must be exactly 16 MiB / 4096 pages", 13, 10
        err
end if
if VIDEO_MAX_WIDTH * VIDEO_MAX_HEIGHT * VIDEO_BPP > VIDEO_FB_MAX_SIZE
        display "the maximum geometry must fit the FB window", 13, 10
        err
end if

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; GOP pixel formats of the BootInfo handover, G1 accepts BGRX only

VIDEO_FMT_RGBX    = 0
VIDEO_FMT_BGRX    = 1
VIDEO_FMT_BITMASK = 2
VIDEO_FMT_BLTONLY = 3

if VIDEO_FMT_BGRX <> 1
        display "G1 must accept exactly the BGRX8888 format", 13, 10
        err
end if

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; BootInfo v1 fields the module reads, private copy of the 208 byte
; record; only the framebuffer flag and the GOP block are consumed

VIDEO_BOOTINFO_SIZE = 208
VIDEO_FLAG_FRAMEBUFFER = 0x02

VIDEO_BOOTINFO.flags               = 16
VIDEO_BOOTINFO.gop_framebuffer_addr = 88
VIDEO_BOOTINFO.gop_framebuffer_size = 96
VIDEO_BOOTINFO.gop_width            = 104
VIDEO_BOOTINFO.gop_height           = 108
VIDEO_BOOTINFO.gop_pixel_format     = 112
VIDEO_BOOTINFO.gop_pixels_per_line  = 116

if VIDEO_BOOTINFO.gop_framebuffer_addr <> 88 | VIDEO_BOOTINFO.gop_framebuffer_size <> 96
        display "GOP address and size must stay qwords at 88 and 96", 13, 10
        err
end if
if VIDEO_BOOTINFO.gop_width <> 104 | VIDEO_BOOTINFO.gop_height <> 108
        display "GOP geometry must stay dwords at 104 and 108", 13, 10
        err
end if
if VIDEO_BOOTINFO.gop_pixel_format <> 112 | VIDEO_BOOTINFO.gop_pixels_per_line <> 116
        display "GOP format and pitch must stay dwords at 112 and 116", 13, 10
        err
end if

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; status codes, EAX is signed, the numeric values match the MM codes.
; Frozen task-85 verdict policy: video_init returns ENOTSUP ONLY from
; its geometry gates (format, width/height caps, size/page window); any
; vmm_map_mmio failure inside the mapping loop returns VIDEO_EIO, so a
; structural MM fault (including MM_ENOTSUP on a large page rollback)
; takes the fatal foundation path and can never masquerade as the
; non-fatal K64-GUI-SKIP

VIDEO_OK         = 0
VIDEO_EINVAL     = -1
VIDEO_ENOMEM     = -2
VIDEO_ENOENT     = -3
VIDEO_EEXIST     = -4
VIDEO_EPERM      = -5
VIDEO_ENOTSUP    = -6
VIDEO_ENOTREADY  = -7
VIDEO_EALREADY   = -8
VIDEO_EIO        = -9
VIDEO_EOVERFLOW  = -10
VIDEO_EFAULT     = -11
VIDEO_EBUSY      = -12

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; 64 byte framebuffer state, the frozen data.inc record
;
; stride is pitch * 4, the byte distance between two scan lines; every
; row offset must be computed from it, never from width * 4.

VIDEO_FB_STATE_size         = 64
VIDEO_FB_STATE.ready        = 0   ; dd
VIDEO_FB_STATE.mapped_pages = 4   ; dd, pages mapped so far, diagnostics
VIDEO_FB_STATE.width        = 8   ; dd
VIDEO_FB_STATE.height       = 12  ; dd
VIDEO_FB_STATE.pitch        = 16  ; dd, pixels per line
VIDEO_FB_STATE.stride       = 20  ; dd, pitch * 4 bytes
VIDEO_FB_STATE.format       = 24  ; dd, always VIDEO_FMT_BGRX when ready
VIDEO_FB_STATE.pad          = 28  ; dd, zero
VIDEO_FB_STATE.fb_pa        = 32  ; dq
VIDEO_FB_STATE.fb_va        = 40  ; dq, always VIDEO_FB_VA when ready
VIDEO_FB_STATE.fb_size      = 48  ; dq
VIDEO_FB_STATE.reserved     = 56  ; dq, zero

if VIDEO_FB_STATE.reserved + 8 <> VIDEO_FB_STATE_size
        display "FB state must be 64 bytes", 13, 10
        err
end if

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; the 8x16 ASCII font
;
; Glyphs 0x20..0x7E of the 32 bit baseline font, 16 bytes per glyph, one
; byte per row, most significant bit is the leftmost pixel. The glyph
; index is (c - 0x20) * 16.
;
; Out of range policy, frozen: a byte outside [0x20,0x7E] draws nothing
; but still advances the cursor by 8 columns (skip, not space render).

VIDEO_FONT_WIDTH  = 8
VIDEO_FONT_HEIGHT = 16
VIDEO_FONT_STRIDE = 16
VIDEO_FONT_FIRST  = 0x20
VIDEO_FONT_LAST   = 0x7E
VIDEO_FONT_GLYPHS = VIDEO_FONT_LAST - VIDEO_FONT_FIRST + 1
VIDEO_FONT_BYTES  = 1520

if VIDEO_FONT_GLYPHS <> 95 | VIDEO_FONT_GLYPHS * VIDEO_FONT_STRIDE <> VIDEO_FONT_BYTES
        display "the font subset must be 95 glyphs of 16 bytes", 13, 10
        err
end if
if VIDEO_FONT_STRIDE <> VIDEO_FONT_HEIGHT
        display "one byte per row means stride equals height", 13, 10
        err
end if

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; the frozen self test pattern, see the spec table at the top

VIDEO_ST_WIDTH   = 1024
VIDEO_ST_HEIGHT  = 768
VIDEO_ST_BARS    = 8
VIDEO_ST_BAR_W   = 128

VIDEO_ST_BAR0 = 0xFF0000          ; red
VIDEO_ST_BAR1 = 0x00FF00          ; green
VIDEO_ST_BAR2 = 0x0000FF          ; blue
VIDEO_ST_BAR3 = 0xFFFF00          ; yellow
VIDEO_ST_BAR4 = 0x00FFFF          ; cyan
VIDEO_ST_BAR5 = 0xFF00FF          ; magenta
VIDEO_ST_BAR6 = 0xFFFFFF          ; white
VIDEO_ST_BAR7 = 0x000000          ; black

VIDEO_ST_BORDER = 0xFFFFFF

VIDEO_ST_RECT_X = 412
VIDEO_ST_RECT_Y = 284
VIDEO_ST_RECT_W = 200
VIDEO_ST_RECT_H = 200
VIDEO_ST_RECT_COLOR = 0x808080

VIDEO_ST_TEXT_X = 8
VIDEO_ST_TEXT_Y = 8
VIDEO_ST_TEXT_COLOR = 0xFFFFFF
VIDEO_ST_TEXT_LEN = 10            ; "K64 GUI-G1"

if VIDEO_ST_WIDTH <> VIDEO_ST_BARS * VIDEO_ST_BAR_W
        display "the bars must tile the reference width exactly", 13, 10
        err
end if
if VIDEO_ST_RECT_X <> (VIDEO_ST_WIDTH - VIDEO_ST_RECT_W) / 2
        display "the rectangle must be horizontally centered", 13, 10
        err
end if
if VIDEO_ST_RECT_Y <> (VIDEO_ST_HEIGHT - VIDEO_ST_RECT_H) / 2
        display "the rectangle must be vertically centered", 13, 10
        err
end if
if VIDEO_ST_RECT_X + VIDEO_ST_RECT_W >= VIDEO_ST_WIDTH | VIDEO_ST_RECT_Y + VIDEO_ST_RECT_H >= VIDEO_ST_HEIGHT
        display "the rectangle must stay clear of the border", 13, 10
        err
end if
if VIDEO_ST_TEXT_X + VIDEO_ST_TEXT_LEN * VIDEO_FONT_WIDTH > VIDEO_ST_WIDTH
        display "the text cell must fit the reference width", 13, 10
        err
end if
if VIDEO_ST_TEXT_Y + VIDEO_FONT_HEIGHT > VIDEO_ST_HEIGHT
        display "the text cell must fit the reference height", 13, 10
        err
end if

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
; register helper macros, identical frame to the MM module: eight
; pushes keep RSP mod 16 = 8, sub 40 realigns to 0 and provides the
; 32 byte shadow space plus one 8 byte slot for a fifth argument

macro video_enter
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

macro video_leave
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