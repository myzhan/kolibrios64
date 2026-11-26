;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; DESKTOP-v2 wallpaper: the embedded 1024x768 BGRX background image.
;;
;; Converted from the 32 bit KolibriOS default wallpaper
;; kolibrios/data/common/wallpapers/T_Home.png (GPL v2, same license as
;; this repository): 1920x1080, PNG colortype 3, 2-bit palette, 4
;; entries. The 32 bit shell tiles the image from the top left corner,
;; so on the frozen 1024x768 reference screen the visible pixels are
;; exactly the top left 1024x768 crop. tools/gen-wallpaper.py performs
;; the deterministic conversion (pure Python standard library); the
;; asset is one little-endian dword B,G,R,0 per pixel, row major, the
;; framebuffer format of kernel/video/fb.inc. 1024*768*4 = 3145728
;; bytes.
;;
;; This is constant data emitted next to the code, exactly like the
;; video_font subset of kernel/video/font.inc and the ROM images of
;; user/chip8/chip8.asm: whoever includes this file must place it inside
;; the code region (for the desktop module: between desktop_code_start
;; and desktop_code_end, before data.inc). The mutable zeroed block of
;; the module lives in data.inc alone; the wallpaper must NEVER be
;; placed in the data region, the loader expects it all zero.
;;
;; sha256 of the 3145728 asset bytes (frozen by
;; tests/stage2-desktop/check-wallpaper.py):
;;   f9f8a12e5406e04b2007028bd168f72e9c25386d5bf92ef5ecb1aaeb18f36fec
;;

; plain '=' constants: redefinable in both fasm1 and fasm2, so a later
; api.inc may publish the same values without a clash
DESKTOP_WALLPAPER_W = 1024
DESKTOP_WALLPAPER_H = 768
DESKTOP_WALLPAPER_BYTES = DESKTOP_WALLPAPER_W * DESKTOP_WALLPAPER_H * 4

; sha256 digest, big-endian qwords of the sha256 byte string, the
; mirroring convention of the ROM digests in user/chip8/api.inc
DESKTOP_WALLPAPER_SHA0 = 0xF9F8A12E5406E04B
DESKTOP_WALLPAPER_SHA1 = 0x2007028BD168F72E
DESKTOP_WALLPAPER_SHA2 = 0x9C25386D5BF92EF5
DESKTOP_WALLPAPER_SHA3 = 0xECB1AAEB18F36FEC

desktop_wallpaper:
        file    'assets/wallpaper.bgrx'
desktop_wallpaper_end:

if desktop_wallpaper_end - desktop_wallpaper <> DESKTOP_WALLPAPER_BYTES
        err 'the wallpaper asset must stay exactly 3145728 bytes'
end if
