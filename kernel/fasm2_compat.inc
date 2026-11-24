;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; fasm2 (fasmg) compatibility layer for the 64-bit entry points.
;;
;; kernel/struct.inc and kernel/proc64.inc are FASM 1.x macro libraries: they
;; are built from brace macros and escapes that fasm2 does not accept at all.
;; The 64-bit loader and kernel therefore load the native fasm2 headers here
;; instead. The old files stay in the tree untouched for the sources that are
;; still assembled with FASM 1.x.
;;
;; The assembler has to be started with the fasm2 include directory, see
;; build_fasm2.sh:
;;
;;   INCLUDE=<fasm2>/include <host> -e100 -i"Include 'fasm2.inc'" ...
;;

include 'macro/struct.inc'
include 'macro/proc64.inc'

; UEFI strings are CHAR16 arrays, so TCHAR must stay two bytes wide. These two
; definitions are the ones fasm2 uses in win64w.inc, repeated here because the
; 64-bit build imports no Windows API and does not include the win64 package.
struc? TCHAR args:?&
        . du args
end struc

macro TCHAR args:?&
        du args
end macro

sizeof.TCHAR = 2

; native sized pointer, the type the UEFI structure declarations use
DN fix dq

; The 64-bit sources spell the Microsoft x64 calling convention helper
; "fstcall". FASM 1.x expressed that as "fastcall fix fstcall", but fasm2
; cannot publish a macro as an alias, so forward the call instead. The fasm2
; "fastcall" always reserves the 32 byte shadow space and keeps RSP 16 byte
; aligned, which is what the UEFI x64 calling convention requires.
macro fstcall proc*,args&
        fastcall proc, args
end macro
