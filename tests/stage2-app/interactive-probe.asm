;; task 141 W1: interactive pacing probe. Assemble the hosted chip8
;; program exactly like build_fasm2.sh, then append the offset of the
;; interactive main loop (start.main_loop) from APP_CODE_BASE. The
;; integration judge sets a hardware breakpoint at APP_CODE_BASE + this
;; offset to catch deterministic round boundaries. check.py reads the
;; trailing qword and verifies the leading bytes equal the fresh program.
include '../../user/chip8/chip8.asm'
        dq      start.main_loop - APP_CODE_BASE