#!/usr/bin/env python3
"""Reusable QEMU boot-debug workflow for kolibrios64 (Darwin arm64, TCG).

Builds with build_fasm2.sh into a private per-session artifact directory,
assembles an ESP (EFI/BOOT/BOOTX64.EFI, EFI/KERNEL64.BIN, startup.nsh) and
starts QEMU with the repository OVMF.fd, a vvfat ESP, COM1 captured to
serial.log and a gdbstub bound to 127.0.0.1 only.

Modes:
  default        debug mode: QEMU starts frozen (-S); attach lldb or gdb,
                 set a hardware breakpoint at the kernel entry VA (computed
                 from KERNEL64.BIN header offset 8 of THIS build) and
                 continue. Ctrl-C stops QEMU and cleans up.
  --no-freeze    smoke mode: no -S, bounded by --timeout; waits for a
                 TERMINAL marker (K64-FOUNDATION-OK or K64-CPU-PANIC),
                 not the first K64-BOOT-OK. PASS requires all ten OK
                 markers (BOOT/CPU/PMM/VMM/HEAP/IRQ/INPUT/GUI/DESKTOP/
                 FOUNDATION) exactly once and in order, no K64-CPU-PANIC /
                 K64-BOOT-FAIL, no triple fault or unexpected reset, and
                 the frozen task-60 verdict values: CPU selftest passed =
                 0x5FF (parsed from the serial report line) plus mm_mask =
                 0, hook_hits = 3, core_mask = 0, the IRQ-v2 verdict values
                 irq_mask = 0, ticks = 11 (the desktop loop masks the LVT
                 timer, the count stays at the input exit value), the
                 input verdict values input mask = 0, kbd_count = 6,
                 mouse_count = 6, stage = 6 (the desktop injection adds
                 make+break of h and i plus move/press/release on top of
                 the input exit values 2/3), the GUI-G1 verdict value
                 video_selftest_mask = 0 and the DESKTOP-v1 verdict values
                 desktop mask = 0, stage = 5, keys = 2, cursor = (557,411).
                 The kernel never prints those counters on the success
                 path, so they are read from guest memory through the
                 gdbstub after the terminal marker (task 60 used the same
                 channel). While waiting, the smoke loop drives the IRQ-v2
                 input self test handshake and then the DESKTOP-v1 shell
                 handshake: it polls input_test_stage / desktop_test_stage
                 through the gdbstub and injects sendkey / mouse_move /
                 mouse_button through the monitor socket, once per stage,
                 all bounded. THREE screendumps are captured:
                 screen-gui.ppm inside the bounded GUI frame handshake
                 window (the foundation publishes READY in
                 desktop_gui_frame_stage after K64-GUI-OK and spins a
                 pause budget; the host screendumps the still pristine
                 GUI-G1 pattern frame and writes RELEASE through the
                 gdbstub 'M' packet; a timeout is a guest EIO panic,
                 never a hang), screen-echo.ppm when the echo leg
                 finished (desktop stage 3, before the mouse move:
                 background/title/cursor at (517,387)/"hi") and
                 screen.ppm after the terminal marker (cursor moved to
                 (557,411), the old position restored to the background).
                 All three are judged in pure Python against the frozen
                 pattern/desktop frame probe specs. The -d int log is
                 judged too: the
                 v=0x21/v=0x2c delivery counts must equal the values
                 deterministically derived from the WHOLE injection chain
                 (one delivery per 8042 byte). Any parse/read failure,
                 or a timeout without a terminal marker, is a FAIL with
                 the reason printed.
                 FS-v1 M1 appends the eleventh marker K64-BLK-OK between
                 DESKTOP and FOUNDATION; the ten frozen markers keep
                 their order and verdict values untouched. A
                 virtio-blk-pci-transitional device backed by
                 build/fs-v1/disk.img is attached in BOTH modes, and
                 after the terminal marker the blk counters plus both
                 512-byte guest test buffers are read through the
                 gdbstub and compared byte for byte against the image
                 sectors (LBA0 BPB and the LBA101 ROM pattern); the
                 evidence stays in blk-evidence.log in the artifact
                 directory.
                 FS-v1 M2 appends the twelfth marker K64-FS-OK between
                 BLK and FOUNDATION. With --fs the fs counters and the
                 guest file buffers (IBM.CH8, TETRIS1.CH8, the BIG.BIN
                 cross-cluster span, readdir and stat evidence) are read
                 through the gdbstub and compared byte for byte against
                 the ROM source files and the attached image; the
                 evidence stays in fs-evidence.log. --disk selects the
                 backing image (default build/fs-v1/disk.img; the
                 BIG.BIN leg uses build/fs-v1/disk-big.img, a separately
                 frozen sha). The blk_test_reads expectation derives
                 from the unconditional fs legs: 34 plain / 58 BIG.
                 FS-v1 M3 appends the thirteenth marker K64-FSPROBE-OK
                 between FS and FOUNDATION. With --fsprobe the fsys
                 counters, the eight evidence qwords and the Ring 3
                 guest data page (read/stat/readdir landing buffers and
                 the four errno verdicts) are read through the gdbstub;
                 the IBM.CH8 payload is compared byte for byte against
                 the ROM source and the image, the dirents against the
                 raw image directory. The evidence stays in
                 fsprobe-evidence.log. The frozen fs/blk counter totals
                 are snapshotted and restored by the probe, so every
                 M1/M2 verdict value stays untouched.
  --gui-skip     expect the non-fatal GUI skip path instead: the eight
                 non-GUI/non-desktop OK markers plus exactly one
                 K64-GUI-SKIP and one K64-DESKTOP-SKIP between INPUT and
                 FOUNDATION, no K64-GUI-OK / K64-DESKTOP-OK, no pixel
                 assertions; the input counters stay at their input-exit
                 values (2/3) and the desktop counters at zero, the
                 vector counts at the input-only derivation (2/9);
                 video_fb_state is read through the gdbstub as geometry
                 evidence. Used with a loader built with GOP_MAX_MODES=0.

Nothing outside the artifact directory is written; build.sh, build_fasm2.sh
and the kernel/loader sources are never modified. QEMU runs in its own
session and is killed on exit/interrupt, so no stray process survives.

Minimum kernel CPU features: long mode (x86_64), NX (EFER.NXE) and POPCNT.
The plain TCG "qemu64" model lacks POPCNT, which the MM module uses, so the
default CPU is "qemu64,+popcnt" in BOTH debug and smoke mode; --cpu overrides
it for experiments (e.g. a host-passthrough model or extra feature flags).

Usage:
  run-qemu-debug.py [--no-freeze] [--gui-skip] [--mem MIB] [--port N]
                    [--timeout SEC] [--art-dir DIR] [--qemu PATH]
                    [--cpu MODEL]

Environment: FASMG / FASM2_DIR / FASM1 are forwarded to build_fasm2.sh.
"""

import argparse
import atexit
import datetime
import hashlib
import json
import os
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# the ten milestone markers, in their fixed emission order; IRQ-v2
# INPUT sits between IRQ and GUI, GUI-G1 between INPUT and DESKTOP,
# DESKTOP-v1 between GUI and FOUNDATION (foundation.inc emits them
# after core_selftest)
MARKERS = ("K64-BOOT-OK", "K64-CPU-OK", "K64-PMM-OK", "K64-VMM-OK",
           "K64-HEAP-OK", "K64-IRQ-OK", "K64-INPUT-OK", "K64-GUI-OK",
           "K64-DESKTOP-OK", "K64-FOUNDATION-OK")
GUI_OK_MARKER = "K64-GUI-OK"
GUI_SKIP_MARKER = "K64-GUI-SKIP"
DESKTOP_OK_MARKER = "K64-DESKTOP-OK"
DESKTOP_SKIP_MARKER = "K64-DESKTOP-SKIP"
TERMINAL_MARKERS = ("K64-FOUNDATION-OK", "K64-CPU-PANIC")
FORBIDDEN_MARKERS = ("K64-CPU-PANIC", "K64-BOOT-FAIL")

# frozen screendump spec: 1024x768. THREE captures are judged:
#   screen-gui.ppm   the GUI-G1 pattern frame, captured inside the
#                    bounded foundation handshake window after
#                    K64-GUI-OK, BEFORE desktop_init repaints it
#   screen-echo.ppm  after the echo leg (desktop stage 3, before the
#                    mouse move): cursor at the initial (517,387)
#   screen.ppm       after the terminal marker: cursor at (557,411),
#                    the old position restored to the background
# The GUI probes are the frozen task-77 pattern spec plus the task-108
# mirror-sensitive 'K' row 3 pixels, the same 32 hand written pixels as
# the stage2-fb spec model. Common desktop probes
# hold in BOTH desktop frames. DESKTOP-v2: the background is the full
# screen wallpaper (the T1 asset kernel/desktop/assets/wallpaper.bgrx,
# truth sampled from build/desktop-wallpaper/wallpaper-probes.json and
# the asset itself, NEVER from the renderer): deep blue-gray 0x182028
# with a gray band 0x9594A2 around (557..568,411..416). The taskbar
# 0x2C3E50 (0,0,1024,32), "K64 Desktop v0.1" at (8,8) in white on the
# bar, the echoed "hi" at (16,64) in white on the wallpaper.
PPM_WIDTH, PPM_HEIGHT = 1024, 768
# DESKTOP_BG stays the frozen v1 plain color, kept ONLY as a negative
# reference: task 125 app_redraw_desktop delegates to desktop_repaint_base,
# so the --app final frame is the FULL wallpaper desktop again and the
# *_APP_* probe tables are the identity transform of the common ones
DESKTOP_BG = 0x1A2B3C
DESKTOP_BAR = 0x2C3E50
WALLPAPER_BG = 0x182028     # T1 JSON: the deep blue-gray wallpaper base
WALLPAPER_BAND = 0x9594A2   # T1 JSON: the gray band, also the accent color
# the GUI-G1 pattern frame: 8 color bars x 128 px, white border, the
# centered 200x200 gray rectangle at (412,284), "K64 GUI-G1" at (8,8)
PPM_GUI_PROBES = (
    (64, 100, 0xFF0000),                # bar 0 center is red
    (192, 100, 0x00FF00),               # bar 1 center is green
    (320, 100, 0x0000FF),               # bar 2 center is blue
    (448, 100, 0xFFFF00),               # bar 3 center is yellow
    (576, 100, 0x00FFFF),               # bar 4 center is cyan
    (704, 100, 0xFF00FF),               # bar 5 center is magenta
    (832, 100, 0xFFFFFF),               # bar 6 center is white
    (960, 100, 0x000000),               # bar 7 center is black
    (127, 100, 0xFF0000),               # bar 0/1 boundary left is red
    (128, 100, 0x00FF00),               # bar 0/1 boundary right is green
    (895, 100, 0xFFFFFF),               # bar 6/7 boundary left is white
    (896, 100, 0x000000),               # bar 6/7 boundary right is black
    (0, 0, 0xFFFFFF),                   # top left corner is border white
    (1023, 767, 0xFFFFFF),              # bottom right corner is border white
    (512, 0, 0xFFFFFF),                 # top edge is border white
    (0, 384, 0xFFFFFF),                 # left edge is border white
    (1023, 100, 0xFFFFFF),              # right edge is border white
    (1, 1, 0xFF0000),                   # inside the border bar 0 red shows
    (412, 284, 0x808080),               # rectangle top left is gray
    (611, 483, 0x808080),               # rectangle bottom right is gray
    (411, 284, 0xFFFF00),               # left of the rectangle bar 3 yellow
    (612, 284, 0x00FFFF),               # right of the rectangle bar 4 cyan
    (412, 283, 0xFFFF00),               # above the rectangle bar 3 yellow
    (412, 484, 0xFFFF00),               # below the rectangle bar 3 yellow
    (9, 10, 0xFFFFFF),                  # 'K' glyph row 2 left stroke lit
    (14, 10, 0xFFFFFF),                 # 'K' glyph row 2 right stroke lit
    (8, 10, 0xFF0000),                  # 'K' glyph row 2 edge not lit
    (8, 8, 0xFF0000),                   # 'K' glyph row 0 is empty
    # task-108 mirror-sensitive: 'K' row 3 = 0x22, LSB-first truth lights
    # cols 1 and 5; an MSB-first renderer would light cols 2 and 6
    (9, 11, 0xFFFFFF),                  # 'K' glyph row 3 col 1 lit
    (13, 11, 0xFFFFFF),                 # 'K' glyph row 3 col 5 lit
    (14, 11, 0xFF0000),                 # 'K' glyph row 3 col 6 not lit
    (10, 11, 0xFF0000),                 # 'K' glyph row 3 col 2 not lit
)
PPM_COMMON_PROBES = (
    (900, 700, WALLPAPER_BG),           # wallpaper, far from everything
    (16, 767, WALLPAPER_BG),            # wallpaper, bottom left corner
    (500, 16, DESKTOP_BAR),             # taskbar fill
    (500, 31, DESKTOP_BAR),             # taskbar last row
    (500, 32, WALLPAPER_BG),            # first row below the bar
    (9, 10, 0xFFFFFF), (14, 10, 0xFFFFFF),   # 'K' glyph row 2 lit
    (8, 10, DESKTOP_BAR), (8, 8, DESKTOP_BAR),  # 'K' glyph off pixels
    # task-108 mirror-sensitive: 'K' row 3 = 0x22 lights cols 1 and 5
    (9, 11, 0xFFFFFF), (13, 11, 0xFFFFFF),   # 'K' row 3 cols 1/5 lit
    (14, 11, DESKTOP_BAR), (10, 11, DESKTOP_BAR),  # cols 6/2 not lit
    # task-108: title '1' (cell x=128) row 4 = 0x14, the flag is on the
    # LEFT under the LSB-first truth; row 3 = 0x18 stays symmetric
    (130, 12, 0xFFFFFF),                # '1' row 4 col 2 flag lit
    (133, 12, DESKTOP_BAR),             # '1' row 4 col 5 not lit
    (131, 11, 0xFFFFFF), (132, 11, 0xFFFFFF),  # '1' row 3 symmetric lit
    # task-108: 'h'/'i' probes re-derived from the charUni.mt LSB truth;
    # DESKTOP-v2: the off pixels sit on the wallpaper base (asset truth)
    (17, 69, 0xFFFFFF),                 # 'h' row 5 col 1 lit
    (18, 69, WALLPAPER_BG),             # 'h' row 5 col 2 off
    (16, 69, WALLPAPER_BG),             # 'h' row 5 col 0 off
    (17, 66, 0xFFFFFF),                 # 'h' row 2 col 1 lit
    (22, 66, WALLPAPER_BG),             # 'h' row 2 col 6 off
    (28, 66, 0xFFFFFF),                 # 'i' row 2 col 4 lit
    (27, 66, WALLPAPER_BG),             # 'i' row 2 col 3 off
    (26, 75, 0xFFFFFF),                 # 'i' row 11 col 2 base lit
    (25, 75, WALLPAPER_BG),             # 'i' row 11 col 1 off
    (24, 66, WALLPAPER_BG),             # 'i' row 2 col 0 off
)
# the echo frame: the cursor sprite at the frozen initial (517,387)
PPM_ECHO_PROBES = (
    (517, 387, 0x000000),               # sprite row 0 col 0 outline
    (519, 392, 0xFFFFFF),               # sprite row 5 col 2 white fill
    (528, 387, WALLPAPER_BG),           # sprite row 0 col 11 transparent
)
# the final frame: the cursor moved to (557,411), the old position
# restored to the wallpaper pixel for pixel; (568,411) sits inside the
# T1 JSON gray band, the transparent sprite column shows the band
PPM_FINAL_PROBES = (
    (557, 411, 0x000000),               # sprite row 0 col 0 outline
    (559, 416, 0xFFFFFF),               # sprite row 5 col 2 white fill
    (562, 416, 0xFFFFFF),               # sprite row 5 col 5 white fill
    (568, 411, WALLPAPER_BAND),         # sprite row 0 col 11 transparent
    (517, 387, WALLPAPER_BG),           # old outline pixel restored
    (519, 392, WALLPAPER_BG),           # old white fill pixel restored
    (528, 397, WALLPAPER_BG),           # old row 10 outline pixel restored
)


# the frozen app echo save rectangle (kernel/app/api.inc APP_ECHO_SAVE_*,
# pinned to the desktop echo first line): the teardown blits it back
# VERBATIM, and it was captured from the wallpaper desktop, so pixels
# inside keep the wallpaper truth while the rest of the frame is the
# frozen plain repaint
APP_ECHO_SAVE_RECT = (16, 64, 1008, 16)


def _app_flat(probes):
    # task 125: the teardown redraw delegates to desktop_repaint_base
    # (the full wallpaper + taskbar + accents + title), so the --app
    # final frame keeps the wallpaper truth EVERYWHERE, the echo save
    # rectangle included (the base repaints it identically). The
    # transform is the identity; it is kept so the *_APP_* tables and
    # the frozen --app probe counts survive unchanged.
    return tuple(probes)


PPM_APP_COMMON_PROBES = _app_flat(PPM_COMMON_PROBES)
PPM_APP_FINAL_PROBES = _app_flat(PPM_FINAL_PROBES)

# DESKTOP-v2 T3 menu frames (--menu). Truth derived independently from
# the frozen geometry contract (start button (0,0,64,32), menu rect
# (0,32,200,34), 1px accent border, inner panel (1,33,198,32) in
# DESKTOP_BAR, 'CHIP-8' at (76,41), entry center (100,49)), the
# charUni.mt LSB-first font bytes and the wallpaper asset samples.
PPM_MENU_OPEN_PROBES = (
    # the accent border corners of the menu rectangle
    (0, 32, WALLPAPER_BAND), (199, 32, WALLPAPER_BAND),
    (0, 65, WALLPAPER_BAND), (199, 65, WALLPAPER_BAND),
    # the inner panel
    (1, 33, DESKTOP_BAR), (198, 64, DESKTOP_BAR),
    # the taskbar accents: underline (0,31,64,1), separator (64,24,1,8)
    (10, 31, WALLPAPER_BAND), (64, 24, WALLPAPER_BAND),
    # the wallpaper outside the menu
    (900, 700, WALLPAPER_BG), (16, 767, WALLPAPER_BG),
    (500, 40, WALLPAPER_BG),
    # 'CHIP-8' glyph truth, LSB-first (bit0 = leftmost), text at (76,41)
    (78, 44, 0xFFFFFF),                 # 'C' row 3 = 0x44 col 2 lit
    (77, 44, DESKTOP_BAR),              # 'C' row 3 col 1 off (mirror)
    (85, 43, 0xFFFFFF),                 # 'H' row 2 = 0x42 col 1 lit
    (84, 43, DESKTOP_BAR),              # 'H' row 2 col 0 off
    (94, 43, 0xFFFFFF),                 # 'I' row 2 = 0x7C col 2 lit
    (92, 43, DESKTOP_BAR),              # 'I' row 2 col 0 off
    (101, 43, 0xFFFFFF),                # 'P' row 2 = 0x3E col 1 lit
    (106, 43, DESKTOP_BAR),             # 'P' row 2 col 6 off
    (109, 48, 0xFFFFFF),                # '-' row 7 = 0x7E col 1 lit
    (108, 48, DESKTOP_BAR),             # '-' row 7 col 0 off
    (118, 43, 0xFFFFFF),                # '8' row 2 = 0x3C col 2 lit
    (117, 43, DESKTOP_BAR),             # '8' row 2 col 1 off
    # the cursor sprite on the start button (32,16)
    (32, 16, 0x000000),                 # sprite row 0 col 0 outline
    (34, 21, 0xFFFFFF),                 # sprite row 5 col 2 white fill
    # the echo survives below the menu rectangle
    (17, 69, 0xFFFFFF), (18, 69, WALLPAPER_BG),
)
# the launch frame: the CHIP-8 entry click closed the menu, the whole
# rectangle is restored from the page-2 save, the cursor sits at the
# entry center (100,49)
PPM_MENU_LAUNCH_PROBES = (
    (0, 32, WALLPAPER_BG), (199, 65, WALLPAPER_BG),
    (1, 33, WALLPAPER_BG), (198, 64, WALLPAPER_BG),
    (78, 44, WALLPAPER_BG),             # a former glyph pixel restored
    (109, 48, WALLPAPER_BG),            # a former '-' pixel restored
    (557, 411, WALLPAPER_BAND),         # the gray band intact
    (900, 700, WALLPAPER_BG),
    (500, 16, DESKTOP_BAR), (10, 31, WALLPAPER_BAND),
    (100, 49, 0x000000),                # sprite row 0 col 0 outline
    (102, 54, 0xFFFFFF),                # sprite row 5 col 2 white fill
)
# the close frame: the blank click at (700,600) closed the menu, the
# rectangle (including the two echo rows y=64,65 it covered) is
# restored from the save pixel for pixel
PPM_MENU_CLOSE_PROBES = (
    (1, 33, WALLPAPER_BG), (198, 64, WALLPAPER_BG),
    (78, 44, WALLPAPER_BG),
    (16, 64, WALLPAPER_BG),             # echo top row background restored
    (17, 69, 0xFFFFFF),                 # 'h' row 5 col 1 still lit
    (557, 411, WALLPAPER_BAND), (517, 387, WALLPAPER_BG),
    (900, 700, WALLPAPER_BG),
    (10, 31, WALLPAPER_BAND),
    (700, 600, 0x000000),               # sprite row 0 col 0 outline
    (702, 605, 0xFFFFFF),               # sprite row 5 col 2 white fill
    # task 125: the ENOENT launch cleanup erased the running indicator
    (1000, 12, DESKTOP_BAR),
)

# task 125 T5: the taskbar running indicator while the app runs, the
# frozen geometry (DESKTOP_RUN_X/Y/W/H = 992,8,24,16) in the accent
# color, the surrounding bar pixels untouched
PPM_RUN_LIT_PROBES = (
    (992, 8, WALLPAPER_BAND), (1015, 23, WALLPAPER_BAND),
    (1000, 12, WALLPAPER_BAND),
    (991, 8, DESKTOP_BAR), (1016, 8, DESKTOP_BAR),
    (992, 7, DESKTOP_BAR), (992, 24, DESKTOP_BAR),
)

# task 143 (DEFECT-1): the start-button click box (32,16)-(43,33), the
# exact 12x18 sprite footprint of the start press. While the cursor is
# SUSPENDED (game/game2 frames) or elsewhere (menu launch/close frames)
# this box must hold the bar / button-white / underline / wallpaper
# truth; a sprite pattern here is the captured-cursor residue defect.
# The truth derives from the clean full-repaint exit frame, NOT from
# the defective capture.
RESIDUE_BOX_X, RESIDUE_BOX_Y = 32, 16
RESIDUE_BOX_W, RESIDUE_BOX_H = 12, 18
PPM_RESIDUE_BOX_PROBES = (
    (32, 16, DESKTOP_BAR),              # sprite r0c0 outline -> bar
    (33, 17, DESKTOP_BAR),              # r1c1 white fill -> bar
    (34, 18, DESKTOP_BAR),              # r2c2 white fill -> bar
    (40, 19, DESKTOP_BAR),              # r3c8 transparent -> bar
    (42, 26, DESKTOP_BAR),              # r10c10 white fill -> bar
    (43, 26, DESKTOP_BAR),              # r10c11 outline -> bar
    (37, 28, DESKTOP_BAR),              # r12c5 outline -> bar
    (41, 16, 0xFFFFFF),                 # the start button white graphic
    (43, 19, 0xFFFFFF),                 # the start button white graphic
    (32, 31, WALLPAPER_BAND),           # r15c0 outline -> underline
    (38, 32, WALLPAPER_BG),             # r16c6 outline -> wallpaper
    (39, 33, WALLPAPER_BG),             # r17c7 outline -> wallpaper
    (40, 33, WALLPAPER_BG),             # r17c8 outline -> wallpaper
)


def residue_box(raw):
    """The flat 12x18 start-click box of a captured PPM (task 143), or
    None on any parse/size defect: the full-box residue comparison."""
    if raw is None:
        return None
    try:
        w, h, px = parse_ppm(raw)
    except ValueError:
        return None
    if (w, h) != (PPM_WIDTH, PPM_HEIGHT):
        return None
    out = []
    for y in range(RESIDUE_BOX_Y, RESIDUE_BOX_Y + RESIDUE_BOX_H):
        for x in range(RESIDUE_BOX_X, RESIDUE_BOX_X + RESIDUE_BOX_W):
            off = (y * w + x) * 3
            out.append((px[off] << 16) | (px[off + 1] << 8) | px[off + 2])
    return tuple(out)

# frozen task-60 verdict values. passed comes from the serial report line;
# the counters are memory-only on the success path, so their VAs are
# resolved via a private ELF wrapper and read through the gdbstub 'm' packet
COUNTER_SYMBOLS = (("mm_test_selftest_mask", 8), ("mm_test_fail_hits", 4),
                   ("core_selftest_mask", 8),
                   ("irq_test_selftest_mask", 8), ("irq_test_ticks", 8),
                   ("input_test_selftest_mask", 8),
                   ("input_test_kbd_count", 8),
                   ("input_test_mouse_count", 8),
                   ("input_test_stage", 8),
                   ("video_selftest_mask", 8),
                   ("desktop_test_selftest_mask", 8),
                   ("desktop_test_stage", 8),
                   ("desktop_test_keys", 8),
                   ("desktop_test_cursor_x", 8),
                   ("desktop_test_cursor_y", 8),
                   ("desktop_gui_frame_stage", 8))
EXPECTED_PASSED = 0x5FF
# ok mode: the WHOLE injection chain (input self test + desktop self
# test). kbd = 2 + make/break of h and i = 6; mouse = 3 + move/press/
# release = 6; ticks stays 11 (the desktop loop masks the LVT timer);
# the desktop counters derive from the frozen contract (keys = 2, the
# cursor moves 517+40, 387+24).
EXPECTED_COUNTERS = {"mm_test_selftest_mask": 0, "mm_test_fail_hits": 3,
                     "core_selftest_mask": 0,
                     "irq_test_selftest_mask": 0, "irq_test_ticks": 11,
                     "input_test_selftest_mask": 0,
                     "input_test_kbd_count": 6,
                     "input_test_mouse_count": 6,
                     "input_test_stage": 6,
                     "video_selftest_mask": 0,
                     "desktop_test_selftest_mask": 0,
                     "desktop_test_stage": 5,
                     "desktop_test_keys": 2,
                     "desktop_test_cursor_x": 557,
                     "desktop_test_cursor_y": 411,
                     "desktop_gui_frame_stage": 3}
# skip mode: no GOP, no GUI, no desktop; the input counters stay at
# their input-exit values and every desktop counter stays zero
EXPECTED_COUNTERS_SKIP = dict(EXPECTED_COUNTERS,
                              input_test_kbd_count=2,
                              input_test_mouse_count=3,
                              desktop_test_stage=0,
                              desktop_test_keys=0,
                              desktop_test_cursor_x=0,
                              desktop_test_cursor_y=0,
                              desktop_gui_frame_stage=0)
# DESKTOP-v2 --menu mode: the frozen chain above plus the 21-packet
# host menu drive (5 moves + press/release to the start button, 1 move
# + press/release to the CHIP-8 entry, 1 move + press/release back to
# the start button, 6 moves + press/release to the blank (700,600)):
# mouse = 6 + 21 = 27, the cursor evidence ends at (700,600); ticks
# stays 11 (the resident loop keeps the LVT timer masked). Task 125:
# the entry click REALLY launches through desktop_launch_pump; on the
# plain disk.img app_launch reports ENOENT (pixel-neutral cleanup), the
# click-mode pump publishes APP_RUNNING/APP_EXIT (8/9) and the LAST
# start press re-publishes START_CLICK (6), so the final stage is 6
EXPECTED_COUNTERS_MENU = dict(EXPECTED_COUNTERS,
                              input_test_mouse_count=27,
                              desktop_test_stage=6,
                              desktop_test_cursor_x=700,
                              desktop_test_cursor_y=600)
# DESKTOP-v2 --five mode (task 125 T5): the boot hosted run (--app
# handshake, runs=1/exits=1, boot mode keeps stage 5) plus TWO resident
# click rounds of 8 packets each: 5 moves + start press/release + 1 move
# + entry press/release (round 1), 1 move back + start press/release +
# 1 move + entry press/release (round 2). mouse = 6 + 16 = 22, the
# cursor ends at the entry center (100,49), the last click-mode pump
# published APP_EXIT (9); ticks stays 11, kbd stays 6
EXPECTED_COUNTERS_FIVE = dict(EXPECTED_COUNTERS,
                              input_test_mouse_count=22,
                              desktop_test_stage=9,
                              desktop_test_cursor_x=100,
                              desktop_test_cursor_y=49)
# the nine DESKTOP-v2 private labels, read on the --menu and --five legs
MENU_COUNTER_SYMBOLS = (("desktop_test_clicks", 8),
                        ("desktop_test_menu_opens", 8),
                        ("desktop_menu_flag", 8),
                        ("desktop_app_launch_requested", 8),
                        ("desktop_prev_buttons", 8),
                        ("desktop_test_start_clicks", 8),
                        ("desktop_test_entry_clicks", 8),
                        ("desktop_test_app_runs", 8),
                        ("desktop_test_app_exits", 8))
# the frozen --menu end state: clicks = 1 (the selftest leg 4 press at
# (557,411), frozen by the guest selftest) + 4 menu presses = 5; 2 menu
# opens, 2 start presses, 1 entry press; task 125: the launch pump
# CONSUMED the flag (0); runs counts EVERY launch attempt in BOTH modes:
# the boot attempt (ENOENT on the plain image) + the entry click = 2,
# exits 0 (neither found /PROGRAMS/CHIP8.BIN), the menu closed, no
# button held
EXPECTED_MENU_FINAL = {"desktop_test_clicks": 5,
                       "desktop_test_menu_opens": 2,
                       "desktop_menu_flag": 0,
                       "desktop_app_launch_requested": 0,
                       "desktop_prev_buttons": 0,
                       "desktop_test_start_clicks": 2,
                       "desktop_test_entry_clicks": 1,
                       "desktop_test_app_runs": 2,
                       "desktop_test_app_exits": 0}
# the frozen --five end state: the boot run plus two click rounds, all
# three clean teardowns accounted (runs 3 / exits 3), the relaunch reset
# proven by the second IBM_READY and the pixel-identical exit frames
EXPECTED_FIVE_FINAL = {"desktop_test_clicks": 5,
                       "desktop_test_menu_opens": 2,
                       "desktop_menu_flag": 0,
                       "desktop_app_launch_requested": 0,
                       "desktop_prev_buttons": 0,
                       "desktop_test_start_clicks": 2,
                       "desktop_test_entry_clicks": 2,
                       "desktop_test_app_runs": 3,
                       "desktop_test_app_exits": 3}
# -d int vector delivery expectations, DETERMINISTICALLY derived from
# the whole injection chain (one delivery per 8042 byte, the task-94
# measured structure): ok mode v=0x21 = 2 (sendkey a) + 4 (h and i
# make+break) = 6, v=0x2c = 9 (input move/press/release, 3 bytes per
# packet) + 9 (desktop move/press/release) = 18. sendkey b is fully
# drained under the mask, it never delivers. skip mode: input only.
EXPECTED_VECTORS = {"ok": {"21": 6, "2c": 18},
                    "skip": {"21": 2, "2c": 9},
                    # --menu: 18 + 21 packets * 3 bytes = 81, kbd unchanged
                    "menu": {"21": 6, "2c": 81},
                    # --five: 18 + 16 packets * 3 bytes = 66, kbd unchanged
                    "five": {"21": 6, "2c": 66}}
PASSED_RE = re.compile(r"K64-CPU-SELFTEST passed = (0x[0-9A-Fa-f]+)")

# the IRQ-v2 host injection script, one monitor command per handshake
# stage (input/api.inc INPUT_STAGE_*). Stage 4 is special: the press is
# sent first, the release only after the guest consumed the press packet
# (mouse_count >= 2), so QEMU can never coalesce press+release into one
# packet. Stage 5 (timer) and 6 (done) need no host action.
INPUT_STAGE_INJECT = {1: "sendkey b", 2: "sendkey a", 3: "mouse_move 5 3"}

# the DESKTOP-v1 host injection script, one monitor command per
# handshake stage (desktop/api.inc DESKTOP_STAGE_*), polled only after
# K64-GUI-OK. Stage 3 is special: the echo frame screendump is captured
# FIRST (cursor still at the initial position), then the move is sent.
# Stage 4 splits press/release: the release only after the guest
# consumed the press packet (mouse_count >= 5 = input exit 3 + move 1
# + press 1), so QEMU can never coalesce press+release. Stage 5 (done)
# needs no host action.
DESKTOP_STAGE_INJECT = {1: "sendkey h", 2: "sendkey i", 3: "mouse_move 40 24"}

# DESKTOP-v2 T3 (--menu): the host-side menu drive, run ONLY after the
# terminal marker against the resident desktop_run loop. Zero new guest
# handshake stages: every step is gated on input_test_mouse_count (one
# PS/2 packet per monitor command) and, for the presses, on the desktop
# private labels the click pump publishes. Path (screen coordinates,
# QEMU mouse_move deltas are down-positive, the input module sign
# extends the PS/2 deltas): (557,411) -> 5 moves -> (32,16) start button
# press = menu open frame -> release -> 1 move -> (100,49) CHIP-8 entry
# press = launch flag + menu close frame -> release -> 1 move -> (32,16)
# press = reopen -> release -> 6 moves -> (700,600) blank press = menu
# close frame -> release. Every move delta stays <= 127 in absolute
# value: the QEMU PS/2 device splits larger deltas into MULTIPLE packets
# (measured: 125 -> 1 packet, 200 -> 2), which would drift the derived
# delivery counts. 21 packets: mouse 6 -> 27, v=0x2c 18 -> 81.
MENU_LEG_TIMEOUT = 60.0
# task 125 T5 (--five): two resident click rounds, each waits for a full
# hosted run (IBM_READY -> EXIT_REQ -> DONE) on top of the packet gates
FIVE_LEG_TIMEOUT = 120.0
MENU_INJECT_SCRIPT = (
    # (monitor command, mouse_count gate, guest label gates, capture)
    ("mouse_move -125 -100", 7, (), None),
    ("mouse_move -125 -100", 8, (), None),
    ("mouse_move -125 -100", 9, (), None),
    ("mouse_move -125 -100", 10, (), None),
    ("mouse_move -25 5", 11, (), None),
    ("mouse_button 1", 12, (("desktop_test_clicks", ">=", 1),
                            ("desktop_menu_flag", "==", 1)), "open"),
    ("mouse_button 0", 13, (), None),
    ("mouse_move 68 33", 14, (), None),
    # task 125: the launch pump consumes the flag synchronously inside
    # the resident loop, so the persistent entry_clicks counter is the
    # gate (the flag == 1 window is never observable from the host)
    ("mouse_button 1", 15, (("desktop_test_entry_clicks", ">=", 1),
                            ("desktop_menu_flag", "==", 0)), "launch"),
    ("mouse_button 0", 16, (), None),
    ("mouse_move -68 -33", 17, (), None),
    ("mouse_button 1", 18, (("desktop_menu_flag", "==", 1),
                            ("desktop_test_menu_opens", ">=", 2)), None),
    ("mouse_button 0", 19, (), None),
    ("mouse_move 125 125", 20, (), None),
    ("mouse_move 125 125", 21, (), None),
    ("mouse_move 125 125", 22, (), None),
    ("mouse_move 125 125", 23, (), None),
    ("mouse_move 125 84", 24, (), None),
    ("mouse_move 43 0", 25, (), None),
    ("mouse_button 1", 26, (("desktop_test_clicks", ">=", 4),
                            ("desktop_menu_flag", "==", 0)), "close"),
    ("mouse_button 0", 27, (), None),
)

# the GUI frame forensic handshake stages (core/contracts.inc
# K64_GUI_FRAME_*): after K64-GUI-OK the guest publishes READY (1) in
# desktop_gui_frame_stage and spins a bounded pause window; the host
# captures screen-gui.ppm and writes RELEASE (2), the guest marks
# DONE (3) and only then lets desktop_init repaint the pattern frame
GUI_FRAME_READY = 1
GUI_FRAME_RELEASE = 2

# forensic evidence for a failed input handshake: the raw mouse packet
# state, read through the gdbstub while the panicked kernel still lives
INPUT_FORENSIC_SYMBOLS = (("input_test_mouse_last", 8),
                          ("input_mouse_x", 8), ("input_mouse_y", 8),
                          ("input_mouse_bad", 8), ("input_mouse_state", 8),
                          ("input_mouse_b1", 8), ("input_mouse_b2", 8))

# FS-v1 M1: the eleventh OK marker and its evidence symbols. Kept OUT of
# MARKERS / COUNTER_SYMBOLS / EXPECTED_COUNTERS on purpose: the frozen
# ten-marker judge fixtures (tests/stage2-fb/check-gui-judge.py) stay
# untouched; judge() asserts the blk verdict only when blk= is passed.
# The image hash is the frozen task-130 build-fat16-image.py output.
BLK_OK_MARKER = "K64-BLK-OK"
BLK_IMAGE_SHA256 = ("e9e83dad3f6d5193ac15c7a35834cc51"
                    "90ca34e978b537d80be9fa9beb7bc42b")
BLK_TEST_DATA_LBA = 101
BLK_TEST_CAPACITY = 16384
BLK_TEST_READS = 7
BLK_COUNTER_SYMBOLS = (("blk_test_mask", 8), ("blk_test_reads", 8),
                       ("blk_capacity", 8), ("blk_ready", 8),
                       ("blk_qnum", 8),
                       ("blk_test_lba0_buf", 512),
                       ("blk_test_data_buf", 512))
# bring-up diagnostics, logged as evidence only (never judged): they
# pinpoint a failed scan/bring-up step without touching the kernel
BLK_DIAG_SYMBOLS = (("blk_iobase", 8), ("blk_pci_addr", 8),
                    ("blk_vring_phys", 8), ("blk_backend", 8))

# FS-v1 M2: the twelfth OK marker and its evidence symbols. Kept OUT of
# MARKERS / COUNTER_SYMBOLS / EXPECTED_COUNTERS exactly like the M1 blk
# block: the frozen ten-marker judge fixtures stay untouched; judge()
# asserts the fs verdict only when fs= is passed (--fs). The foundation
# runs fs_init/fs_selftest unconditionally after the blk leg, so the
# blk_test_reads total now includes the fs init chunks (1 BPB + 4 FAT)
# and every fs single-sector cache miss: 7 + 5 + 22 = 34 on the plain
# image, 7 + 5 + 46 = 58 on the BIG.BIN image (the stage2-fs gate mirror
# derives the same totals sector for sector).
FS_OK_MARKER = "K64-FS-OK"
FS_IMAGE_SHA256 = ("e9e83dad3f6d5193ac15c7a35834cc51"
                   "90ca34e978b537d80be9fa9beb7bc42b")
FS_BIG_IMAGE_SHA256 = ("a810280509cd05f67dce37dc46e67cbf"
                       "eb2e9432b73a105108f9a2d298500543")
FS_TEST_IBM_SIZE = 132
FS_TEST_TETRIS_SIZE = 494
FS_TEST_BIG_SIZE = 8229
FS_TEST_CLUSTERS = 8143
FS_TEST_READS = 24
FS_TEST_READS_BIG = 48
FS_INIT_BLK_CHUNKS = 5
FS_MISS_PLAIN = 22
FS_MISS_BIG = 46
BLK_TEST_READS_FS = BLK_TEST_READS + FS_INIT_BLK_CHUNKS + FS_MISS_PLAIN
BLK_TEST_READS_FS_BIG = BLK_TEST_READS + FS_INIT_BLK_CHUNKS + FS_MISS_BIG
# the frozen BIG.BIN payload formula of tools/build-fat16-image.py
FS_BIG_DATA = bytes(((i * 7 + 3) & 0xFF) for i in range(FS_TEST_BIG_SIZE))
FS_COUNTER_SYMBOLS = (("fs_test_mask", 8), ("fs_test_reads", 8),
                      ("fs_test_big_present", 8), ("fs_ready", 8),
                      ("fs_clusters", 8),
                      ("fs_test_ibm_buf", 512),
                      ("fs_test_tetris_buf", 512),
                      ("fs_test_big_buf", 9216),
                      ("fs_test_dirents", 96),
                      ("fs_test_stat", 64))
# the ROM source files the guest buffers are compared against, plus the
# cluster-2 LBA of each file on the frozen geometry (IBM cluster 4 =
# LBA 101, TETRIS1 cluster 5 = LBA 103)
FS_ROM_SOURCES = (("ibm", "user/chip8/roms/ibm.ch8",
                   FS_TEST_IBM_SIZE, 101, "fs_test_ibm_buf"),
                  ("tetris", "user/chip8/roms/tetris1.ch8",
                   FS_TEST_TETRIS_SIZE, 103, "fs_test_tetris_buf"))

# FS-v1 M3: the thirteenth OK marker and its evidence symbols. Kept OUT
# of MARKERS / COUNTER_SYMBOLS / EXPECTED_COUNTERS exactly like the M1/M2
# blocks: the frozen ten-marker judge fixtures stay untouched; judge()
# asserts the fsprobe verdict only when fsprobe= is passed (--fsprobe).
# The foundation runs fsys_init/fsys_probe unconditionally after the fs
# leg; the probe snapshots and restores fs_test_reads/blk_test_reads, so
# the M2 finals (24/48, 34/58) are still what the blk/fs blocks read.
FSPROBE_OK_MARKER = "K64-FSPROBE-OK"
FSYS_TABLE_BUDGET = 3
FSYS_FP_MASK_ALL = 0x7FF
FSYS_USER_CODE_BASE = 0x100400000
FSYS_USER_DATA_BASE = 0x100600000
FSYS_USER_STACK_TOP = 0x100800000
FSYS_COUNTER_SYMBOLS = (("fsys_ready", 8), ("fsys_probe_mask", 8),
                        ("fsys_probe_fail", 8), ("fsys_exit_code", 8),
                        ("fsys_table_base", 8), ("fsys_table_delta", 8),
                        ("fsys_saved_fs_reads", 8),
                        ("fsys_saved_blk_reads", 8),
                        ("fsys_probe_evidence", 64),
                        # task 138 W3: the teardown receipts and the
                        # bounce snapshot of the guest data page (the
                        # original user VA is unmapped after the probe)
                        ("fsys_pages_held", 8), ("fsys_drops_base", 8),
                        ("fsys_table_released", 8), ("fsys_pages", 72),
                        ("fsys_bounce", 4096), ("mm_vmm_state", 56))
# the guest evidence page layout, frozen in user/fsprobe/api.inc; the
# kernel copy fsys_probe_evidence mirrors guest +16..+80 (8 qwords)
FSYS_FP_EVIDENCE_WANT = (0, 132, 0, 124, 132, 1, 64, (1 << 64) - 3)
FSYS_GUEST_ERRNO = (("enoent", 72, -3), ("efault", 80, -11),
                    ("einval", 88, -1), ("enotsup", 96, -6))
FSYS_GUEST_READBUF = (128, 512)
FSYS_GUEST_STATBUF = (640, 64)
FSYS_GUEST_DIRENTS = (704, 256)

# APP-v1: the fourteenth OK marker and its evidence symbols. Kept OUT
# of MARKERS / COUNTER_SYMBOLS / EXPECTED_COUNTERS exactly like the
# M1/M2/M3 blocks: the frozen ten-marker judge fixtures stay untouched;
# judge() asserts the app verdict only when app= is passed (--app).
# The foundation runs the app leg after the fsys probe; a disk without
# /PROGRAMS/CHIP8.BIN reports ENOENT and the leg skips non-fatally
# (K64-APP-SKIP), so the FS-v1 images keep their 13-marker caliber.
# The app image is the plain ROMS plus /PROGRAMS/CHIP8.BIN, no BIG.BIN,
# so the restored fs/blk finals are the plain ones (24 / 34).
APP_OK_MARKER = "K64-APP-OK"
APP_SKIP_MARKER = "K64-APP-SKIP"
APP_IMAGE_SHA256 = ("aec9f70167e6e18170a638bf4af3d5bf"
                    "28817cf2ff37d6fec5a137cec12f3170")
APP_TABLE_BUDGET = 3
APP_LAUNCH_PAGES = 516
APP_PROGRAM_SIZE = 3255
APP_STAGE_IDLE = 0
APP_STAGE_IBM_READY = 1
APP_STAGE_EXIT_REQ = 2
APP_STAGE_DONE = 3
# the frozen BLIT destination rectangle and cell size (user/chip8)
APP_FB_X, APP_FB_Y = 192, 224
APP_FB_W, APP_FB_H = 640, 320
APP_CELL = 10
APP_COUNTER_SYMBOLS = (("app_ready", 8), ("app_stage", 8),
                       ("app_exit_code", 8), ("app_fault_vector", 8),
                       ("app_blit_count", 8), ("app_key_count", 8),
                       ("app_poll_count", 8),
                       ("app_saved_fs_reads", 8),
                       ("app_saved_blk_reads", 8),
                       ("app_table_base", 8), ("app_drops_base", 8),
                       ("app_table_delta", 8), ("app_table_released", 8),
                       ("app_pages_held", 8), ("app_image_size", 8),
                       ("app_kbd_consumed", 8), ("app_interactive", 8),
                       ("app_fd_swept", 8), ("app_echo_saved", 8),
                       ("app_fs_snapshot", 4096),
                       ("app_pages", APP_LAUNCH_PAGES * 8),
                       ("fs_test_reads", 8), ("blk_test_reads", 8),
                       ("mm_vmm_state", 56))
# the exit frame: task 125 the teardown redraw delegates to
# desktop_repaint_base, the game rectangle is repainted to the FULL
# wallpaper desktop (the base color, never the v1 plain DESKTOP_BG)
APP_EXIT_PROBES = (
    (APP_FB_X + 5, APP_FB_Y + 5, WALLPAPER_BG),
    (500, 400, WALLPAPER_BG),
    (APP_FB_X + APP_FB_W - 1, APP_FB_Y + APP_FB_H - 1, WALLPAPER_BG),
)

# task 125 T5: the --five exit frame, the full wallpaper desktop after
# the teardown redraw with the cursor at the entry center (100,49), the
# running indicator erased by the taskbar repaint, the game rectangle
# residue-free and the old cursor positions restored to the asset truth
PPM_FIVE_EXIT_PROBES = (
    (100, 49, 0x000000),                # sprite row 0 col 0 outline
    (102, 54, 0xFFFFFF),                # sprite row 5 col 2 white fill
    (111, 49, WALLPAPER_BG),            # sprite row 0 col 11 transparent
    (992, 8, DESKTOP_BAR), (1015, 23, DESKTOP_BAR),
    (1000, 12, DESKTOP_BAR),            # the running indicator erased
    (APP_FB_X + 5, APP_FB_Y + 5, WALLPAPER_BG),
    (500, 400, WALLPAPER_BG),
    (APP_FB_X + APP_FB_W - 1, APP_FB_Y + APP_FB_H - 1, WALLPAPER_BG),
    (557, 411, WALLPAPER_BAND),         # the gray band intact
    (517, 387, WALLPAPER_BG),           # the initial position restored
)

# task 141 W1: the interactive tetris leg (--app-interactive). Keys are
# injected by writing the public input key buffer directly through the
# gdbstub, bypassing the 8042 IRQ handler so the 13 frozen marker
# counters and the -d int delivery counts never drift. Round boundaries
# are caught with a hardware breakpoint at the user main_loop VA published
# by tests/stage2-app/check.py (build/user-chip8/interactive-probes.json).
APP_INT_KEY_COUNT = 4          # Enter make + 7 make + 7 break + ESC make
APP_INT_MIN_BLIT = 2           # the IBM logo plus at least one tetris frame
APP_INT_MIN_POLL = 72          # the capture boundary is round 71 + 1
APP_SC_ENTER = 0x1C
APP_SC_ESC = 0x01
APP_SC_KEY7_MAKE = 0x08
APP_SC_KEY7_BREAK = 0x88
APP_ASCII_ENTER = 0x0D
APP_ASCII_ESC = 27
APP_ASCII_KEY7 = 0x37
# The break carries a NON-ZERO ascii so the frozen user key_drain (which
# stops draining on ascii 0, treating it as the empty queue) actually
# processes the release edge; the raw scancode keeps the 0x80 break bit.
# This is a debug-buffer injection convention, not the IRQ handler path
# (which stores ascii 0 for breaks); it exercises the true press/release
# level semantics the chip8 model replays.
APP_INT_SYMBOLS = (("input_kbd_buf", 8), ("input_kbd_used", 8))

_qemu_proc = None


def cleanup():
    """Kill the QEMU session on any exit path; never leave a stray QEMU."""
    global _qemu_proc
    p = _qemu_proc
    if p is None or p.poll() is not None:
        return
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()
    print(f"run-qemu-debug: QEMU (pid {p.pid}) stopped and cleaned up")


def on_signal(signum, frame):
    # Never wait/poll the child from the handler: the main thread may sit
    # in Popen.wait() holding subprocess's internal waitpid lock, and any
    # wait/poll here would deadlock on it. Only kill the process group;
    # the atexit cleanup reaps and escalates once the lock is released.
    if _qemu_proc is not None:
        try:
            os.killpg(_qemu_proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    sys.exit(128 + signum)


def build(art):
    bin_dir = os.path.join(art, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    r = subprocess.run(["sh", os.path.join(REPO, "build_fasm2.sh")],
                       env=dict(os.environ, OUTDIR=bin_dir), cwd=REPO)
    if r.returncode != 0:
        print(f"run-qemu-debug: build_fasm2.sh failed (exit {r.returncode})",
              file=sys.stderr)
        sys.exit(1)
    return bin_dir


def entry_va(bin_path):
    with open(bin_path, "rb") as f:
        header = f.read(40)
    if len(header) < 40 or header[:8] != b"KERNEL64":
        print(f"run-qemu-debug: {bin_path}: bad KERNEL64 header",
              file=sys.stderr)
        sys.exit(1)
    off, = struct.unpack_from("<Q", header, 8)
    # read the base from the source of truth, not a hardcoded copy
    with open(os.path.join(REPO, "kernel", "const.inc"),
              encoding="utf-8") as f:
        m = re.search(r"^\s*VIRT_KERNEL_BASE\s*=\s*(0x[0-9A-Fa-f]+|\d+)",
                      f.read(), re.M)
    if not m:
        print("run-qemu-debug: VIRT_KERNEL_BASE not found in const.inc",
              file=sys.stderr)
        sys.exit(1)
    return int(m.group(1), 0) + off, off


def make_esp(art, bin_dir):
    esp = os.path.join(art, "esp")
    os.makedirs(os.path.join(esp, "EFI", "BOOT"), exist_ok=True)
    # a stale OVMF NvVars from a SIGTERM'd session can trap the next
    # boot in a UEFI shell clear-screen loop; always start clean
    try:
        os.remove(os.path.join(esp, "NvVars"))
    except FileNotFoundError:
        pass
    shutil.copyfile(os.path.join(bin_dir, "BOOTX64.EFI"),
                    os.path.join(esp, "EFI", "BOOT", "BOOTX64.EFI"))
    shutil.copyfile(os.path.join(bin_dir, "KERNEL64.BIN"),
                    os.path.join(esp, "EFI", "KERNEL64.BIN"))
    with open(os.path.join(esp, "startup.nsh"), "w", encoding="ascii") as f:
        f.write("fs0:\\EFI\\BOOT\\BOOTX64.EFI\n")
    return esp


def gen_symbols(art, bin_dir):
    elf = os.path.join(art, "kernel64.elf")
    r = subprocess.run(
        [sys.executable, os.path.join(REPO, "tools", "gen-kernel-symbols.py"),
         "--bin", os.path.join(bin_dir, "KERNEL64.BIN"), "--out", elf],
        cwd=REPO)
    if r.returncode != 0:
        print("run-qemu-debug: WARNING: symbol ELF failed validation, "
              "falling back to raw-address debugging (hbreak *<entry VA>)",
              file=sys.stderr)
        return None
    return elf


def counter_addresses(art, extra=()):
    """Resolve the counter VAs; returns ({name: VA}, error strings).

    `extra` adds (name, size) pairs to the same wrapper, e.g. the 64-byte
    video_fb_state block read as geometry evidence in --gui-skip mode.

    tools/kernel64-elf.asm only publishes the entry/console labels, so a
    private wrapper is generated INSIDE the artifact directory: the same
    `format elf64` + absolute-org trick turns the added public labels into
    SHN_ABS symbols carrying the full runtime addresses. No repo file is
    touched.
    """
    fasm2_dir = os.path.abspath(os.environ.get(
        "FASM2_DIR", os.path.join(REPO, "..", "fasm2")))
    host = os.environ.get("FASMG") or os.path.join(
        fasm2_dir, "build", "fasm2-bootstrap", "fasmg-macos-x64")
    if not os.access(host, os.X_OK):
        return {}, [f"fasmg host {host} not executable"]
    wrapper = os.path.join(art, "kernel64-counters.asm")
    elf_path = os.path.join(art, "kernel64-counters.elf")
    # dedupe by name: --app shares fs_test_reads/blk_test_reads/mm_vmm_state
    # with --fs/--fsprobe, a duplicate 'public' line would break fasmg
    seen = {}
    for name, size in COUNTER_SYMBOLS + tuple(extra):
        seen.setdefault(name, size)
    symbols = tuple(seen.items())
    with open(wrapper, "w", encoding="utf-8") as f:
        f.write("format elf64\n")
        f.write("include '%s'\n" % os.path.join(REPO, "kernel", "kernel64.asm"))
        for name, _ in symbols:
            f.write("public %s\n" % name)
    r = subprocess.run(
        [host, "-e100", "-iInclude 'fasm2.inc'", wrapper, elf_path],
        env=dict(os.environ, INCLUDE=os.path.join(fasm2_dir, "include")),
        cwd=REPO, capture_output=True, text=True)
    if r.returncode != 0:
        return {}, ["counter wrapper assembly failed: "
                    + (r.stderr or r.stdout).strip()]
    with open(elf_path, "rb") as f:
        elf = f.read()
    if elf[:6] != b"\x7fELF\x02\x01":
        return {}, ["counter wrapper output is not a little-endian ELF64"]
    e_shoff, = struct.unpack_from("<Q", elf, 0x28)
    e_shentsize, e_shnum, _ = struct.unpack_from("<HHH", elf, 0x3A)
    shdrs = [struct.unpack_from("<IIQQQQIIQQ", elf, e_shoff + i * e_shentsize)
             for i in range(e_shnum)]
    symtab = next((s for s in shdrs if s[1] == 2), None)  # SHT_SYMTAB
    if symtab is None:
        return {}, ["counter wrapper ELF has no .symtab"]
    strtab = shdrs[symtab[6]]
    strs = elf[strtab[4]:strtab[4] + strtab[5]]
    wanted = {name for name, _ in symbols}
    addrs, errors = {}, []
    for i in range(symtab[5] // 24):
        st_name, _, _, st_shndx, st_value, _ = struct.unpack_from(
            "<IBBHQQ", elf, symtab[4] + i * 24)
        nm = strs[st_name:strs.index(b"\0", st_name)].decode()
        if nm in wanted:
            if st_shndx != 0xFFF1:  # SHN_ABS: absolute org survived
                errors.append(f"counter symbol {nm} is not SHN_ABS")
            else:
                addrs[nm] = st_value
    for nm in sorted(wanted - set(addrs)):
        if not any(nm in e for e in errors):
            errors.append(f"counter symbol {nm} missing from wrapper .symtab")
    return addrs, errors


def gdb_read_mem(port, addr, size, timeout=5.0):
    """One little-endian guest memory read via the gdbstub 'm' packet.

    Connecting pauses the VM; after a terminal marker the kernel sits in
    cli/hlt, so the counters are stable. Stop replies ('T'/'S') and console
    ('O') packets may precede the read reply and are skipped. A detach
    ('D') resumes the guest; it is sent on EVERY exit path, because a
    bare socket close after an 'E' error reply would leave the VM paused
    forever (the early-boot stage polling hits unmapped addresses).
    """
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.settimeout(timeout)
        try:
            body = ("m%x,%x" % (addr, size)).encode()
            s.sendall(b"$" + body + b"#%02x" % (sum(body) & 0xFF))
            buf = b""
            deadline = time.monotonic() + timeout
            value = None
            while time.monotonic() < deadline:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while True:
                    m = re.search(rb"\$([^#]*)#([0-9a-fA-F]{2})", buf)
                    if not m:
                        break
                    payload = m.group(1)
                    buf = buf[m.end():]
                    if (len(payload) == 2 * size
                            and re.fullmatch(rb"[0-9a-fA-F]+", payload)):
                        value = int.from_bytes(bytes.fromhex(payload.decode()),
                                               "little")
                        break
                    if payload[:1] == b"E":
                        raise OSError(f"gdbstub error reply {payload.decode()!r} "
                                      f"for m{addr:x},{size:x}")
                if value is not None:
                    break
            if value is None:
                raise OSError(f"gdbstub read m{addr:x},{size:x}: no reply "
                              "before timeout")
            return value
        finally:
            try:
                s.sendall(b"$D#44")
            except OSError:
                pass


def gdb_write_mem(port, addr, value, size, timeout=5.0):
    """One little-endian guest memory write via the gdbstub 'M' packet.

    Used by the GUI frame handshake to release the bounded guest spin
    window after screen-gui.ppm was captured. Same detach discipline
    as gdb_read_mem: 'D' is sent on EVERY exit path so the VM never
    stays paused after an error reply.
    """
    data = value.to_bytes(size, "little").hex()
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.settimeout(timeout)
        try:
            body = ("M%x,%x:%s" % (addr, size, data)).encode()
            s.sendall(b"$" + body + b"#%02x" % (sum(body) & 0xFF))
            buf = b""
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while True:
                    m = re.search(rb"\$([^#]*)#([0-9a-fA-F]{2})", buf)
                    if not m:
                        break
                    payload = m.group(1)
                    buf = buf[m.end():]
                    if payload == b"OK":
                        return
                    if payload[:1] == b"E":
                        raise OSError(f"gdbstub error reply {payload.decode()!r} "
                                      f"for M{addr:x},{size:x}")
            raise OSError(f"gdbstub write M{addr:x},{size:x}: no OK reply "
                          "before timeout")
        finally:
            try:
                s.sendall(b"$D#44")
            except OSError:
                pass


def read_counters(port, addrs):
    counters, errors = {}, []
    for name, size in COUNTER_SYMBOLS:
        if name not in addrs:
            continue  # address resolution errors are reported separately
        try:
            counters[name] = gdb_read_mem(port, addrs[name], size)
        except OSError as e:
            errors.append(f"counter {name}: {e}")
    # task 124: the five --menu private desktop labels resolve through
    # the same wrapper; read them only when their addresses are present
    # (--menu), the frozen legs never pass them and stay unchanged
    for name, size in MENU_COUNTER_SYMBOLS:
        if name in addrs and name not in counters:
            try:
                counters[name] = gdb_read_mem(port, addrs[name], size)
            except OSError as e:
                errors.append(f"counter {name}: {e}")
    return counters, errors


def collect_blk_evidence(args, art, counter_addrs):
    """FS-v1 M1 evidence: the blk counters plus both 512-byte guest test
    buffers, read through the gdbstub while the halted kernel still
    lives, compared byte for byte against the matching disk.img sectors
    (LBA0 BPB and the LBA101 ROM pattern). Returns a verdict dict for
    judge(); every read failure is recorded, never silently skipped.
    blk-evidence.log stays in the artifact directory as evidence.
    """
    ev = {"errors": []}
    image_path = args.disk
    image = None
    try:
        with open(image_path, "rb") as f:
            image = f.read()
        ev["image_sha256"] = hashlib.sha256(image).hexdigest()
        ev["expect_big"] = ev["image_sha256"] == FS_BIG_IMAGE_SHA256
        ev["expect_app"] = (getattr(args, "app", False)
                            and ev["image_sha256"] == APP_IMAGE_SHA256)
        allowed = (FS_IMAGE_SHA256, FS_BIG_IMAGE_SHA256)
        if getattr(args, "app", False):
            allowed += (APP_IMAGE_SHA256,)
        if ev["image_sha256"] not in allowed:
            ev["errors"].append("disk image sha256 matches neither frozen "
                                "FS-v1 image (plain or BIG.BIN)")
    except OSError as e:
        ev["errors"].append(f"disk image read: {e}")
    for name, size in BLK_COUNTER_SYMBOLS:
        if name not in counter_addrs:
            ev["errors"].append(f"{name}: address unresolved")
            continue
        try:
            raw = gdb_read_mem(args.port, counter_addrs[name], size)
        except OSError as e:
            ev["errors"].append(f"{name}: {e}")
            continue
        # the 'm' reply is the raw memory byte string; for the sector
        # buffers the int round trip restores exactly those bytes
        ev[name] = raw.to_bytes(size, "little") if size == 512 else raw
    diag = []
    for name, size in BLK_DIAG_SYMBOLS:
        if name not in counter_addrs:
            continue
        try:
            diag.append(f"{name} = {gdb_read_mem(args.port, counter_addrs[name], size):#x}")
        except OSError as e:
            diag.append(f"{name}: {e}")
    try:
        info_pci = monitor_command(art, "info pci")
    except OSError as e:
        info_pci = f"info pci failed: {e}"
    for name, lba, key in (("blk_test_lba0_buf", 0, "lba0_match"),
                           ("blk_test_data_buf", BLK_TEST_DATA_LBA,
                            "data_match")):
        buf = ev.get(name)
        if buf is None or image is None:
            ev[key] = False
            continue
        want = image[lba * 512:(lba + 1) * 512]
        ev[key] = buf == want
        if not ev[key]:
            first = next((i for i in range(512) if buf[i] != want[i]), -1)
            ev["errors"].append(
                f"{name} vs disk.img LBA {lba}: first diff at byte {first}")
    lines = [f"image sha256 = {ev.get('image_sha256', '(unreadable)')}"]
    for name, size in BLK_COUNTER_SYMBOLS:
        v = ev.get(name)
        if v is None:
            lines.append(f"{name} = (unreadable)")
        elif size == 512:
            lines.append(f"{name}: 512 bytes, first 16 = {v[:16].hex()}")
        else:
            lines.append(f"{name} = {v:#x}")
    lines.append(f"LBA0 BPB byte compare    : {ev['lba0_match']}")
    lines.append(f"LBA{BLK_TEST_DATA_LBA} data byte compare : "
                 f"{ev['data_match']}")
    lines.extend(diag)
    lines.append("monitor info pci:")
    lines.extend("  " + ln for ln in info_pci.splitlines())
    lines.extend("error: " + e for e in ev["errors"])
    with open(os.path.join(art, "blk-evidence.log"), "w",
              encoding="ascii") as f:
        f.write("\n".join(lines) + "\n")
    ev["log"] = lines
    return ev


def gdb_read_bytes(port, addr, size, chunk=4096):
    """Read `size` guest bytes as a bytes object, chunked so every 'm'
    reply stays well under the QEMU gdbstub packet buffer (the 9216
    byte BIG.BIN buffer would otherwise overflow a single reply)."""
    out = bytearray()
    off = 0
    while off < size:
        n = min(chunk, size - off)
        v = gdb_read_mem(port, addr + off, n)
        out += v.to_bytes(n, "little")
        off += n
    return bytes(out)


def fs_dirent(image, lba, slot):
    """One fixed 32-byte readdir entry derived from the raw on-disk
    directory slot: attr u32@0, pad@4, size u64@8, raw 8.3 name@16,
    five zero pad bytes. Independent of the kernel: the host parses
    the image directly, exactly like the stage2-fs oracle."""
    e = image[lba * 512 + slot * 32:lba * 512 + (slot + 1) * 32]
    attr = e[11]
    size, = struct.unpack_from("<I", e, 28)
    return struct.pack("<IIQ", attr, 0, size) + e[:11] + b"\x00" * 5


def collect_fs_evidence(args, art, counter_addrs):
    """FS-v1 M2 evidence: the fs counters plus the guest test buffers,
    read through the gdbstub while the halted kernel still lives.
    IBM.CH8/TETRIS1.CH8 are compared byte for byte against BOTH the ROM
    source files and the matching image sectors; BIG.BIN against the
    frozen payload formula and the image cluster span; the readdir and
    stat buffers against entries derived from the raw image directories.
    fs-evidence.log stays in the artifact directory as evidence.
    """
    ev = {"errors": []}
    image = None
    try:
        with open(args.disk, "rb") as f:
            image = f.read()
        ev["image_sha256"] = hashlib.sha256(image).hexdigest()
        ev["expect_big"] = ev["image_sha256"] == FS_BIG_IMAGE_SHA256
        ev["expect_app"] = (getattr(args, "app", False)
                            and ev["image_sha256"] == APP_IMAGE_SHA256)
    except OSError as e:
        ev["errors"].append(f"disk image read: {e}")
    for name, size in FS_COUNTER_SYMBOLS:
        if name not in counter_addrs:
            ev["errors"].append(f"{name}: address unresolved")
            continue
        try:
            raw = gdb_read_bytes(args.port, counter_addrs[name], size)
        except OSError as e:
            ev["errors"].append(f"{name}: {e}")
            continue
        ev[name] = raw if size > 8 else int.from_bytes(raw, "little")
    # ROM file legs: guest buffer == source file == image sector span,
    # and the untouched tail of each 512-byte buffer stays zero
    for key, rel, size, lba, bufname in FS_ROM_SOURCES:
        buf = ev.get(bufname)
        src = None
        try:
            with open(os.path.join(REPO, rel), "rb") as f:
                src = f.read()
        except OSError as e:
            ev["errors"].append(f"{rel} read: {e}")
        ok = (buf is not None and src is not None and image is not None
              and len(src) == size
              and buf[:size] == src
              and buf[:size] == image[lba * 512:lba * 512 + size]
              and buf[size:] == b"\x00" * (512 - size))
        ev[key + "_match"] = ok
        if not ok:
            ev["errors"].append(f"{bufname} vs {rel} / image LBA {lba}")
    # BIG.BIN leg: on the big image the 8229 payload bytes must match
    # the formula and the contiguous cluster 6..14 span (LBA 105..122),
    # the buffer tail stays zero; on the plain image the whole buffer
    # was never written and must be all zero
    big = ev.get("fs_test_big_buf")
    if ev.get("expect_big"):
        ok = (big is not None and image is not None
              and big[:FS_TEST_BIG_SIZE] == FS_BIG_DATA
              and big[:FS_TEST_BIG_SIZE] ==
              image[105 * 512:105 * 512 + FS_TEST_BIG_SIZE]
              and big[FS_TEST_BIG_SIZE:] ==
              b"\x00" * (9216 - FS_TEST_BIG_SIZE))
        ev["big_match"] = ok
        if not ok:
            ev["errors"].append("fs_test_big_buf vs BIG.BIN formula/image")
            if big is not None:
                diff = next((i for i in range(FS_TEST_BIG_SIZE)
                             if big[i] != FS_BIG_DATA[i]), -1)
                fill = next((i for i in range(9216 - 1, -1, -1)
                             if big[i] != 0), -1) + 1
                ev["errors"].append(
                    f"big forensic: first diff at {diff}, last nonzero "
                    f"byte + 1 = {fill}")
                if diff >= 0:
                    lo = max(0, diff - 8)
                    ev["errors"].append(
                        "big got  " + big[lo:diff + 8].hex())
                    ev["errors"].append(
                        "big want " + FS_BIG_DATA[lo:diff + 8].hex())
                blocks = []
                for k in range(18):
                    seg = big[k * 512:(k + 1) * 512]
                    want = FS_BIG_DATA[k * 512:(k + 1) * 512]
                    tag = ("ok" if seg == want else
                           "zero" if seg == b"\x00" * 512 else "bad")
                    blocks.append(f"{k}:{tag}")
                ev["errors"].append("big blocks " + " ".join(blocks))
    else:
        ok = big is not None and big == b"\x00" * 9216
        ev["big_zero"] = ok
        if not ok:
            ev["errors"].append("fs_test_big_buf not pristine on plain image")
    # readdir evidence: leg 4 (9) leaves the two /ROMS entries in the
    # buffer; leg 4 (10) overwrites the first slot with the BIG.BIN
    # entry on the big image only (a 0-byte readdir never writes), so
    # bytes 32..63 keep the TETRIS1 entry and 64..96 stay zero
    dirents = ev.get("fs_test_dirents")
    if dirents is not None and image is not None:
        ibm_ent = fs_dirent(image, 97, 2)     # /ROMS slot 2: IBM.CH8
        tetris_ent = fs_dirent(image, 97, 3)  # /ROMS slot 3: TETRIS1.CH8
        if ev.get("expect_big"):
            big_ent = fs_dirent(image, 99, 2)  # /PROGRAMS slot 2: BIG.BIN
            want = big_ent + tetris_ent + b"\x00" * 32
        elif ev.get("expect_app"):
            # APP-v1 third state: leg 4 (10) leaves the CHIP8.BIN entry
            chip8_ent = fs_dirent(image, 99, 2)  # /PROGRAMS slot 2
            want = chip8_ent + tetris_ent + b"\x00" * 32
        else:
            want = ibm_ent + tetris_ent + b"\x00" * 32
        ev["dirents_match"] = dirents == want
        if not ev["dirents_match"]:
            ev["errors"].append("fs_test_dirents vs image directory entries")
    else:
        ev["dirents_match"] = False
    # stat evidence: leg 6 ends on stat('/'), the synthesized root: size
    # 0, attr DIRECTORY, every other byte zero
    stat = ev.get("fs_test_stat")
    want_stat = struct.pack("<QI", 0, 0x10) + b"\x00" * 52
    ev["stat_match"] = stat == want_stat
    if not ev["stat_match"]:
        ev["errors"].append("fs_test_stat vs the synthesized root stat")
    lines = [f"image sha256 = {ev.get('image_sha256', '(unreadable)')}",
             f"expect_big = {ev.get('expect_big')}"]
    for name, size in FS_COUNTER_SYMBOLS:
        v = ev.get(name)
        if v is None:
            lines.append(f"{name} = (unreadable)")
        elif size > 8:
            lines.append(f"{name}: {size} bytes, first 16 = {v[:16].hex()}")
        else:
            lines.append(f"{name} = {v:#x}")
    for key in ("ibm_match", "tetris_match", "big_match", "big_zero",
                "dirents_match", "stat_match"):
        if key in ev:
            lines.append(f"{key:14s}: {ev[key]}")
    lines.extend("error: " + e for e in ev["errors"])
    with open(os.path.join(art, "fs-evidence.log"), "w",
              encoding="ascii") as f:
        f.write("\n".join(lines) + "\n")
    ev["log"] = lines
    return ev


def collect_fsprobe_evidence(args, art, counter_addrs):
    """FS-v1 M3 evidence: the fsys counters, the eight kernel-side
    evidence qwords and the Ring 3 guest data page, read through the
    gdbstub while the halted kernel still lives. The guest page is
    supervisor-readable through the shared page tables: the IBM.CH8
    landing buffer is compared byte for byte against the ROM source and
    image LBA 101, the stat buffer against size 132 / attr 1, the readdir
    buffer against the raw image /ROMS entries, and the four errno
    verdicts against the frozen K64 values. fsprobe-evidence.log stays
    in the artifact directory as evidence.
    """
    ev = {"errors": []}
    image = None
    try:
        with open(args.disk, "rb") as f:
            image = f.read()
        ev["image_sha256"] = hashlib.sha256(image).hexdigest()
        ev["expect_big"] = ev["image_sha256"] == FS_BIG_IMAGE_SHA256
        ev["expect_app"] = (getattr(args, "app", False)
                            and ev["image_sha256"] == APP_IMAGE_SHA256)
    except OSError as e:
        ev["errors"].append(f"disk image read: {e}")
    for name, size in FSYS_COUNTER_SYMBOLS:
        if name not in counter_addrs:
            ev["errors"].append(f"{name}: address unresolved")
            continue
        try:
            raw = gdb_read_bytes(args.port, counter_addrs[name], size)
        except OSError as e:
            ev["errors"].append(f"{name}: {e}")
            continue
        ev[name] = raw if size > 8 else int.from_bytes(raw, "little")
    # the kernel copy of the guest evidence head (+16..+80)
    raw = ev.get("fsys_probe_evidence")
    if raw is not None:
        got = struct.unpack("<8Q", raw)
        ev["evidence_match"] = got == FSYS_FP_EVIDENCE_WANT
        if not ev["evidence_match"]:
            ev["errors"].append(
                "fsys_probe_evidence = " + repr(tuple(hex(v) for v in got)))
    else:
        ev["evidence_match"] = False
    # task 138 W3: the probe tears the user domain down before the
    # verdict, so the guest evidence is the bounce snapshot the kernel
    # took while the mapping was still live; the original user VAs must
    # now be unreadable through the gdbstub. APP-v1: the hosted program
    # reuses fsys_bounce for its file payloads, so app_launch preserved
    # the snapshot in app_fs_snapshot; read that copy instead.
    if getattr(args, "app", False):
        if "app_fs_snapshot" in counter_addrs:
            try:
                ev["app_fs_snapshot"] = gdb_read_bytes(
                    args.port, counter_addrs["app_fs_snapshot"], 4096)
            except OSError as e:
                ev["errors"].append(f"app_fs_snapshot: {e}")
        else:
            ev["errors"].append("app_fs_snapshot: address unresolved")
        page = ev.get("app_fs_snapshot")
    else:
        page = ev.get("fsys_bounce")
    ev["user_va_unmapped"] = True
    for va in (FSYS_USER_CODE_BASE, FSYS_USER_DATA_BASE,
               FSYS_USER_STACK_TOP - 4096):
        try:
            gdb_read_bytes(args.port, va, 8)
        except OSError:
            continue
        ev["user_va_unmapped"] = False
        ev["errors"].append(
            f"user VA {va:#x} still readable after the teardown")
    if page is not None:
        g_mask, g_fail = struct.unpack_from("<QQ", page, 0)
        ev["guest_mask"] = g_mask
        ev["guest_fail"] = g_fail
        ev["guest_head_match"] = (g_mask == ev.get("fsys_probe_mask") and
                                  g_fail == ev.get("fsys_probe_fail"))
        if not ev["guest_head_match"]:
            ev["errors"].append("guest mask/fail vs the kernel copy")
        for key, off, want in FSYS_GUEST_ERRNO:
            got, = struct.unpack_from("<Q", page, off)
            ok = got == want & ((1 << 64) - 1)
            ev[key + "_match"] = ok
            if not ok:
                ev["errors"].append(
                    f"guest {key} verdict = {got:#x} (need {want})")
        off, size = FSYS_GUEST_READBUF
        buf = page[off:off + size]
        src = None
        try:
            with open(os.path.join(REPO, "user/chip8/roms/ibm.ch8"),
                      "rb") as f:
                src = f.read()
        except OSError as e:
            ev["errors"].append(f"ibm.ch8 read: {e}")
        ok = (src is not None and image is not None
              and len(src) == FS_TEST_IBM_SIZE
              and buf[:FS_TEST_IBM_SIZE] == src
              and buf[:FS_TEST_IBM_SIZE] ==
              image[101 * 512:101 * 512 + FS_TEST_IBM_SIZE]
              and buf[FS_TEST_IBM_SIZE:] ==
              b"\x00" * (size - FS_TEST_IBM_SIZE))
        ev["read_match"] = ok
        if not ok:
            ev["errors"].append("guest read buffer vs ROM source / LBA 101")
        off, size = FSYS_GUEST_STATBUF
        want = struct.pack("<QI", FS_TEST_IBM_SIZE, 1) + b"\x00" * 52
        ev["stat_match"] = page[off:off + size] == want
        if not ev["stat_match"]:
            ev["errors"].append("guest stat buffer vs size 132 / attr 1")
        off, size = FSYS_GUEST_DIRENTS
        if image is not None:
            want = (fs_dirent(image, 97, 2) + fs_dirent(image, 97, 3)
                    + b"\x00" * (size - 64))
            ev["dirents_match"] = page[off:off + size] == want
            if not ev["dirents_match"]:
                ev["errors"].append(
                    "guest readdir buffer vs image /ROMS entries")
        else:
            ev["dirents_match"] = False
    # the W3 teardown receipts: every frame back, the array zeroed, the
    # +3 budget reclaimed and the user PDPT slot clear again
    held = ev.get("fsys_pages_held")
    ev["frames_released"] = held == 0
    if not ev["frames_released"]:
        ev["errors"].append(f"fsys_pages_held = {held} (need 0)")
    pages = ev.get("fsys_pages")
    ev["pages_zeroed"] = pages == b"\x00" * 72
    if not ev["pages_zeroed"]:
        ev["errors"].append("fsys_pages array not zeroed after the teardown")
    released = ev.get("fsys_table_released")
    ev["released_is_budget"] = released == FSYS_TABLE_BUDGET
    if not ev["released_is_budget"]:
        ev["errors"].append(
            f"fsys_table_released = {released} (need {FSYS_TABLE_BUDGET})")
    state = ev.get("mm_vmm_state")
    if state is not None:
        root, = struct.unpack_from("<Q", state, 8)
        pa_mask, = struct.unpack_from("<Q", state, 16)
        allocs, drops = struct.unpack_from("<QQ", state, 40)
        base = ev.get("fsys_table_base")
        drops_base = ev.get("fsys_drops_base")
        ev["net_table_invariant"] = (base is not None
                                     and drops_base is not None
                                     and allocs - drops == base - drops_base)
        if not ev["net_table_invariant"]:
            ev["errors"].append(
                "net allocs-drops != the probe baseline after the teardown")
        # walk PML4[0] -> PDPT through the identity map: slot 4 must be
        # clear again, the one-way anchor 0 US bit must stay set
        try:
            pml4e, = struct.unpack(
                "<Q", gdb_read_bytes(args.port, root, 8))
            pdpt = pml4e & pa_mask
            slot4, = struct.unpack(
                "<Q", gdb_read_bytes(args.port, pdpt + 4 * 8, 8))
            ev["pdpt4_clear"] = slot4 == 0
            ev["anchor0_us_stays"] = bool(pml4e & 4)
            if not ev["pdpt4_clear"]:
                ev["errors"].append(
                    f"user PDPT slot 4 = {slot4:#x} (need 0)")
            if not ev["anchor0_us_stays"]:
                ev["errors"].append("anchor 0 US bit lost after the teardown")
        except OSError as e:
            ev["pdpt4_clear"] = False
            ev["errors"].append(f"page directory walk: {e}")
    else:
        ev["net_table_invariant"] = False
    lines = [f"image sha256 = {ev.get('image_sha256', '(unreadable)')}",
             f"expect_big = {ev.get('expect_big')}"]
    for name, size in FSYS_COUNTER_SYMBOLS:
        v = ev.get(name)
        if v is None:
            lines.append(f"{name} = (unreadable)")
        elif name == "fsys_probe_evidence":
            lines.append(f"{name}: " +
                         " ".join(f"{q:#x}" for q in struct.unpack("<8Q", v)))
        elif size > 8:
            lines.append(f"{name}: {size} bytes, first 16 = {v[:16].hex()}")
        else:
            lines.append(f"{name} = {v:#x}")
    for key in ("guest_mask", "guest_fail"):
        if key in ev:
            lines.append(f"{key} = {ev[key]:#x}")
    for key in ("evidence_match", "guest_head_match", "read_match",
                "stat_match", "dirents_match", "enoent_match",
                "efault_match", "einval_match", "enotsup_match",
                "user_va_unmapped", "frames_released", "pages_zeroed",
                "released_is_budget", "net_table_invariant",
                "pdpt4_clear", "anchor0_us_stays"):
        if key in ev:
            lines.append(f"{key:16s}: {ev[key]}")
    lines.extend("error: " + e for e in ev["errors"])
    with open(os.path.join(art, "fsprobe-evidence.log"), "w",
              encoding="ascii") as f:
        f.write("\n".join(lines) + "\n")
    ev["log"] = lines
    return ev


def collect_app_evidence(args, art, counter_addrs):
    """APP-v1 evidence: the app counters, the teardown receipts and the
    game-frame reference, read through the gdbstub while the halted
    kernel still lives. The 516 page user footprint is mapped AND torn
    down inside app_launch, so the code/data/top-stack user VAs must be
    unreadable again, the app_pages frame array zeroed, the +3 table
    budget reclaimed and the frozen fs/blk sector counters restored.
    The game frame probes and the full expected rectangle come from the
    chip8 reference model (build/user-chip8), the same artifacts the
    stage2-app gate consumes. app-evidence.log stays as evidence.
    """
    ev = {"errors": []}
    image = None
    try:
        with open(args.disk, "rb") as f:
            image = f.read()
        ev["image_sha256"] = hashlib.sha256(image).hexdigest()
    except OSError as e:
        ev["errors"].append(f"disk image read: {e}")
    for name, size in APP_COUNTER_SYMBOLS:
        if name not in counter_addrs:
            ev["errors"].append(f"{name}: address unresolved")
            continue
        try:
            raw = gdb_read_bytes(args.port, counter_addrs[name], size)
        except OSError as e:
            ev["errors"].append(f"{name}: {e}")
            continue
        ev[name] = raw if size > 8 else int.from_bytes(raw, "little")
    # the user domain must be gone after the teardown
    ev["user_va_unmapped"] = True
    for va in (FSYS_USER_CODE_BASE, FSYS_USER_DATA_BASE,
               FSYS_USER_STACK_TOP - 4096):
        try:
            gdb_read_bytes(args.port, va, 8)
        except OSError:
            continue
        ev["user_va_unmapped"] = False
        ev["errors"].append(
            f"user VA {va:#x} still readable after the app teardown")
    pages = ev.get("app_pages")
    ev["pages_zeroed"] = pages == b"\x00" * (APP_LAUNCH_PAGES * 8)
    if not ev["pages_zeroed"]:
        ev["errors"].append("app_pages frame array not zeroed")
    held = ev.get("app_pages_held")
    ev["frames_released"] = held == 0
    if not ev["frames_released"]:
        ev["errors"].append(f"app_pages_held = {held} (need 0)")
    released = ev.get("app_table_released")
    ev["released_is_budget"] = released == APP_TABLE_BUDGET
    if not ev["released_is_budget"]:
        ev["errors"].append(
            f"app_table_released = {released} (need {APP_TABLE_BUDGET})")
    state = ev.get("mm_vmm_state")
    if state is not None:
        root, = struct.unpack_from("<Q", state, 8)
        pa_mask, = struct.unpack_from("<Q", state, 16)
        allocs, drops = struct.unpack_from("<QQ", state, 40)
        base = ev.get("app_table_base")
        drops_base = ev.get("app_drops_base")
        ev["net_table_invariant"] = (base is not None
                                     and drops_base is not None
                                     and allocs - drops == base - drops_base)
        if not ev["net_table_invariant"]:
            ev["errors"].append(
                "net allocs-drops != the app baseline after the teardown")
        try:
            pml4e, = struct.unpack(
                "<Q", gdb_read_bytes(args.port, root, 8))
            pdpt = pml4e & pa_mask
            slot4, = struct.unpack(
                "<Q", gdb_read_bytes(args.port, pdpt + 4 * 8, 8))
            ev["pdpt4_clear"] = slot4 == 0
            ev["anchor0_us_stays"] = bool(pml4e & 4)
            if not ev["pdpt4_clear"]:
                ev["errors"].append(f"user PDPT slot 4 = {slot4:#x} (need 0)")
            if not ev["anchor0_us_stays"]:
                ev["errors"].append("anchor 0 US bit lost after the teardown")
        except OSError as e:
            ev["pdpt4_clear"] = False
            ev["errors"].append(f"page directory walk: {e}")
    else:
        ev["net_table_invariant"] = False
    # the game-frame reference from the chip8 model artifacts
    game_probes = []
    ev["expected_frame"] = None
    model_dir = os.path.join(REPO, "build", "user-chip8")
    try:
        with open(os.path.join(model_dir, "ibm-probes.json"),
                  encoding="utf-8") as f:
            probes = json.load(f)
        lit = int(probes["lit_pixel_dword"], 16)
        for cx, cy in probes["sample_cells"]:
            game_probes.append((APP_FB_X + cx * APP_CELL + 5,
                                APP_FB_Y + cy * APP_CELL + 5, lit))
        with open(os.path.join(model_dir, "ibm-expected-gfx.bin"),
                  "rb") as f:
            gfx = f.read()
        for cy in range(32):
            for cx in range(64):
                if not gfx[cy * 64 + cx]:
                    game_probes.append((APP_FB_X + cx * APP_CELL + 5,
                                        APP_FB_Y + cy * APP_CELL + 5, 0))
                    break
            else:
                continue
            break
        with open(os.path.join(model_dir, "ibm-expected-frame.bin"),
                  "rb") as f:
            ev["expected_frame"] = f.read()
    except (OSError, ValueError, KeyError) as e:
        ev["errors"].append(f"chip8 model evidence: {e}")
    ev["game_probes"] = tuple(game_probes)
    # task 141 W1: the interactive tetris reference frames, built by the
    # same chip8 model run (tests/stage2-app/check.py) as the IBM ones
    if args.app_interactive:
        for key, fname in (("int_expected1", "tetris-int-frame-20.bin"),
                           ("int_expected2", "tetris-int-frame-71-key.bin"),
                           ("int_expected_nokey",
                            "tetris-int-frame-71-nokey.bin")):
            try:
                with open(os.path.join(model_dir, fname), "rb") as f:
                    ev[key] = f.read()
            except OSError as e:
                ev[key] = None
                ev["errors"].append(f"interactive model evidence: {e}")
    lines = [f"image sha256 = {ev.get('image_sha256', '(unreadable)')}"]
    for name, size in APP_COUNTER_SYMBOLS:
        v = ev.get(name)
        if v is None:
            lines.append(f"{name} = (unreadable)")
        elif size > 8:
            lines.append(f"{name}: {size} bytes, first 16 = {v[:16].hex()}")
        else:
            lines.append(f"{name} = {v:#x}")
    for key in ("user_va_unmapped", "pages_zeroed", "frames_released",
                "released_is_budget", "net_table_invariant",
                "pdpt4_clear", "anchor0_us_stays"):
        if key in ev:
            lines.append(f"{key:18s}: {ev[key]}")
    lines.append(f"game_probes        : {len(game_probes)} pixels")
    lines.extend("error: " + e for e in ev["errors"])
    with open(os.path.join(art, "app-evidence.log"), "w",
              encoding="ascii") as f:
        f.write("\n".join(lines) + "\n")
    ev["log"] = lines
    return ev


def screendump(art, timeout=15.0, name="screen.ppm"):
    """Capture the guest screen through the unix-socket QEMU monitor.

    Returns the raw PPM bytes; the named file stays in the artifact
    directory as forensic evidence (screen-echo.ppm for the echo frame,
    screen.ppm for the final frame). The command completes before the
    next (qemu) prompt is printed, so the file is whole when the prompt
    arrives.
    """
    sock_path = os.path.join(art, "monitor.sock")
    ppm_path = os.path.join(art, name)
    if os.path.exists(ppm_path):
        os.unlink(ppm_path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(sock_path)
        buf = b""

        def wait_prompt():
            nonlocal buf
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if b"(qemu)" in buf:
                    return
                chunk = s.recv(4096)
                if not chunk:
                    raise OSError("monitor socket closed before prompt")
                buf += chunk
            raise OSError("no (qemu) prompt before timeout")

        wait_prompt()  # the monitor greeting ends with the first prompt
        buf = b""
        s.sendall(("screendump " + ppm_path + "\n").encode())
        wait_prompt()
    with open(ppm_path, "rb") as f:
        return f.read()


def monitor_command(art, cmd, timeout=10.0):
    """Send one HMP command through the unix-socket monitor, bounded.

    Same prompt discipline as screendump(): the greeting prompt is
    consumed first, the command has completed when the next (qemu)
    prompt arrives. Returns the raw reply text as injection evidence.
    """
    sock_path = os.path.join(art, "monitor.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(sock_path)
        buf = b""

        def wait_prompt():
            nonlocal buf
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if b"(qemu)" in buf:
                    return
                chunk = s.recv(4096)
                if not chunk:
                    raise OSError("monitor socket closed before prompt")
                buf += chunk
            raise OSError("no (qemu) prompt before timeout")

        wait_prompt()
        buf = b""
        s.sendall((cmd + "\n").encode())
        wait_prompt()
    return buf.decode(errors="replace").strip()


def parse_ppm(data):
    """Pure-Python PPM P6 parser -> (width, height, rgb bytes).

    No PIL or any other dependency: header tokens are read with comment
    skipping, exactly one whitespace byte separates maxval from the
    binary payload, maxval must be 255. Raises ValueError on any defect.
    """
    if not data.startswith(b"P6"):
        raise ValueError("not a P6 PPM")
    pos = 2
    tokens = []
    while len(tokens) < 3:
        while pos < len(data):
            c = data[pos:pos + 1]
            if c in b" \t\r\n":
                pos += 1
            elif c == b"#":
                nl = data.find(b"\n", pos)
                if nl < 0:
                    raise ValueError("unterminated PPM comment")
                pos = nl + 1
            else:
                break
        start = pos
        while pos < len(data) and data[pos:pos + 1] not in b" \t\r\n#":
            pos += 1
        if pos == start:
            raise ValueError("truncated PPM header")
        tokens.append(int(data[start:pos]))
    w, h, maxval = tokens
    if w <= 0 or h <= 0 or maxval != 255:
        raise ValueError(f"unsupported PPM header {w}x{h} maxval {maxval}")
    pos += 1  # the single whitespace byte after maxval
    px = data[pos:]
    if len(px) != w * h * 3:
        raise ValueError(f"PPM payload {len(px)} bytes, need {w * h * 3}")
    return w, h, px


class GdbSession:
    """One persistent gdbstub connection for the interactive tetris leg.

    The one-shot gdb_read_mem/gdb_write_mem helpers connect-detach per
    access, which would resume the guest between the round-boundary
    stops. This session keeps the VM under debugger control across the
    whole key-injection sequence: connect, then the standard '?' halt
    query (a RUNNING guest emits no spontaneous stop reply on connect;
    '?' pauses the VM and answers with the S/T stop packet, verified
    against the QEMU gdbstub), then m/M/Z1/z1/c packets on the same
    socket. '+' acks are sent for every received packet and the stub's
    own acks are skipped by the packet regex (a '+' can share a TCP
    chunk with the reply, seen as b'+$OK#9a'). Console-output packets
    are 'O' + hex text; the skip test must NOT swallow the 'OK' reply
    of M/Z packets ('K' is not a hex digit) -- an earlier version that
    skipped every 'O'-prefixed payload ate every write/breakpoint OK
    and looked like a swallowed-reply gdbstub quirk. detach() is
    mandatory on every exit path: a bare close would leave the VM
    paused forever.
    """

    def __init__(self, port, timeout=30.0):
        self.timeout = timeout
        self.sock = socket.create_connection(("127.0.0.1", port),
                                             timeout=timeout)
        self.sock.settimeout(0.5)
        self.buf = b""
        self.t0 = time.monotonic()
        self.rx = []  # (elapsed, chunk) traffic trace for timeout diagnosis
        self.tx = []  # (elapsed, packet)
        p = self.cmd("?")  # halt query: pauses the VM, returns S/T
        if p[:1] not in (b"T", b"S"):
            raise OSError(f"gdbstub halt query '?' -> {p!r}")

    def _drain(self, dur):
        """Keep reading into the buffer for a bounded window (select-
        based, so a quiet socket never stretches the window to the
        recv timeout): separates the ack read event from the next
        command."""
        end = time.monotonic() + dur
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            rl, _, _ = select.select([self.sock], [], [], left)
            if not rl:
                return
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("gdbstub closed the connection")
            self.buf += chunk
            self.rx.append((round(time.monotonic() - self.t0, 3), chunk))

    def _recv_packet(self, deadline):
        while time.monotonic() < deadline:
            # the stub's '+' ack can share a TCP chunk with the reply
            # (seen as b'+$OK#9a'): skip any leading acks before the '$'
            m = re.search(rb"\+*\$([^#]*)#([0-9a-fA-F]{2})", self.buf)
            if m:
                payload = m.group(1)
                self.buf = self.buf[m.end():]
                self._drain(0.05)  # let the stub settle before the ack
                self.sock.sendall(b"+")
                self._drain(0.02)  # the ack gets its own read event
                return payload
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                raise OSError("gdbstub closed the connection")
            self.buf += chunk
            self.rx.append((round(time.monotonic() - self.t0, 3), chunk))
        raise OSError("gdbstub packet timeout (buf=%r rx=%r tx=%r)"
                      % (self.buf[-200:], self.rx[-12:], self.tx[-8:]))

    def _recv_reply(self, timeout=None):
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            p = self._recv_packet(deadline)
            # skip hex-encoded console output ONLY: 'O' + nonempty hex
            # text. 'OK' (the M/Z reply) must pass through -- 'K' is
            # not a hex digit, so the fullmatch test keeps it.
            if (p[:1] == b"O" and p[1:]
                    and re.fullmatch(rb"[0-9a-fA-F]+", p[1:])):
                continue
            return p

    def cmd(self, body, timeout=None):
        data = body.encode()
        pkt = b"$" + data + b"#%02x" % (sum(data) & 0xFF)
        self.tx.append((round(time.monotonic() - self.t0, 3), pkt[:48]))
        self.sock.sendall(pkt)
        return self._recv_reply(timeout)

    def read_mem(self, addr, size):
        p = self.cmd("m%x,%x" % (addr, size))
        if (len(p) != 2 * size
                or not re.fullmatch(rb"[0-9a-fA-F]+", p)):
            raise OSError(f"gdbstub read m{addr:x},{size:x} -> {p[:16]!r}")
        return bytes.fromhex(p.decode())

    def read_reg(self, regnum):
        p = self.cmd("p%x" % regnum)
        if not re.fullmatch(rb"[0-9a-fA-F]+", p):
            raise OSError(f"gdbstub p{regnum:x} -> {p[:16]!r}")
        # the stub answers register values in TARGET byte order (little
        # endian for x86_64), reversed from the 'g' packet hex stream
        return int.from_bytes(bytes.fromhex(p.decode()), "little")

    def write_mem(self, addr, data):
        p = self.cmd("M%x,%x:%s" % (addr, len(data), data.hex()))
        if p != b"OK":
            raise OSError(f"gdbstub write M{addr:x},{len(data):x} -> {p!r}")

    def set_hbreak(self, va):
        p = self.cmd("Z1,%x,1" % va)
        if p != b"OK":
            raise OSError(f"gdbstub Z1 at {va:#x} -> {p!r}")

    def clear_hbreak(self, va):
        p = self.cmd("z1,%x,1" % va)
        if p not in (b"OK", b""):
            raise OSError(f"gdbstub z1 at {va:#x} -> {p!r}")

    def continue_(self, timeout=None, bp_va=None):
        """Resume and wait for the next stop reply (T/S). 'c' has no
        synchronous reply: the stop packet arrives when the hardware
        breakpoint fires. W/X mean the guest died -> hard error.

        bp_va: GDB-style step-over. QEMU TCG does NOT step past a
        hardware breakpoint on a plain 'c' when the PC already sits on
        it -- the breakpoint re-triggers immediately and the guest
        never advances (72 fake stops at one RIP, pinned by a live rip
        trace). Real gdb removes the breakpoint, single-steps, then
        reinserts it; with bp_va set this does the same z1/s/Z1 dance
        before every continue, so each stop is one real round."""
        if bp_va is not None:
            self.cmd("z1,%x,1" % bp_va)
            self.cmd("s")  # single-step over the breakpoint site
            self.cmd("Z1,%x,1" % bp_va)
        self.sock.sendall(b"$c#63")
        self.tx.append((round(time.monotonic() - self.t0, 3), b"$c#63"))
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            p = self._recv_packet(deadline)
            if p[:1] in (b"T", b"S"):
                return p
            if p[:1] in (b"W", b"X"):
                raise OSError(f"guest exited during continue: {p!r}")

    def detach(self):
        try:
            self.sock.sendall(b"$D#44")
            try:
                self._recv_reply(timeout=2.0)
            except OSError:
                pass  # the resume matters, not the reply
        except OSError:
            pass
        finally:
            self.sock.close()


def interactive_leg(args, art, counter_addrs, deadline):
    """Task 141 W1: drive the interactive tetris leg to completion.

    Runs while the guest sits at IBM_READY (the IBM logo frame is
    already captured). Stop #h at the main_loop hardware breakpoint
    means h-1 interactive rounds have completed (the breakpoint sits
    at the loop top, before key_drain). Sequence, frozen against the
    chip8 model replay (interactive-probes.json):
      inject Enter -> the wait_enter leg consumes it, tetris loads,
      stop #1..#21: 20 rounds -> capture screen-int-20.ppm
      stop #68 (67 rounds, pc 0x23C delay 10): inject key7 MAKE
      stop #69 (68 rounds, the make consumed): inject key7 BREAK
      stop #72 (71 rounds, last key draw presented at round 70):
      capture screen-int-71-key.ppm
      clear the breakpoint, inject ESC, detach -> round 72 drains ESC,
      SYS_EXIT, the kernel teardown republishes the desktop.
    Keys go straight into input_kbd_buf/used (see APP_INT_SYMBOLS), so
    the frozen IRQ counters never move. Returns {ppm1, ppm2, log,
    errors}; on any failure the breakpoint is still cleared and ESC
    injected so the guest can exit instead of hanging the smoke loop.
    """
    log, errors = [], []
    ppm1 = ppm2 = None
    model_dir = os.path.join(REPO, "build", "user-chip8")
    try:
        with open(os.path.join(model_dir, "interactive-probes.json"),
                  encoding="utf-8") as f:
            probes = json.load(f)
    except (OSError, ValueError) as e:
        return {"ppm1": None, "ppm2": None, "log": log,
                "errors": [f"interactive-probes.json: {e} "
                           "(run tests/stage2-app/check.py first)"]}
    va = int(probes["main_loop_va"])
    stop_k1 = int(probes["k1_rounds"]) + 1
    stop_make = int(probes["key_make_round"])
    stop_break = int(probes["key_break_round"])
    stop_cap2 = int(probes["capture_rounds"]) + 1
    buf_addr = counter_addrs.get("input_kbd_buf")
    used_addr = counter_addrs.get("input_kbd_used")
    if buf_addr is None or used_addr is None:
        return {"ppm1": None, "ppm2": None, "log": log,
                "errors": ["input_kbd_buf/input_kbd_used address unresolved"]}
    sess = None
    try:
        sess = GdbSession(args.port)
        log.append("gdb session up (halt query answered)")

        def inject_key(raw, ascii_code, label):
            used = int.from_bytes(sess.read_mem(used_addr, 8), "little")
            log.append(f"{label}: kbd used={used} read")
            sess.write_mem(buf_addr + used * 16,
                           struct.pack("<QQ", ascii_code, raw))
            sess.write_mem(used_addr, struct.pack("<Q", used + 1))
            log.append(f"{label}: raw={raw:#x} ascii={ascii_code:#x} "
                       f"-> kbd slot {used}")

        inject_key(APP_SC_ENTER, APP_ASCII_ENTER, "enter make")
        sess.set_hbreak(va)
        log.append(f"hbreak at main_loop VA {va:#x}")
        poll_addr = counter_addrs.get("app_poll_count")
        blit_addr = counter_addrs.get("app_blit_count")
        stops = 0
        while stops < stop_cap2:
            left = deadline - time.monotonic()
            if left <= 0:
                errors.append(f"interactive leg deadline at stop {stops} "
                              f"(need {stop_cap2})")
                break
            sess.continue_(timeout=min(30.0, left), bp_va=va)
            stops += 1
            if stops <= 3 or stops in (stop_k1, stop_make, stop_cap2):
                rip = sess.read_reg(16)  # x86_64 gdb regnum 16 = rip
                pc = int.from_bytes(sess.read_mem(rip, 8), "little")
                pn = (int.from_bytes(sess.read_mem(poll_addr, 8), "little")
                      if poll_addr else -1)
                bn = (int.from_bytes(sess.read_mem(blit_addr, 8), "little")
                      if blit_addr else -1)
                log.append(f"stop {stops}: rip={rip:#x} bytes={pc:#x} "
                           f"poll={pn} blit={bn}")
            else:
                log.append(f"stop {stops} at main_loop")
            if stops == stop_k1:
                ppm1 = screendump(art, name="screen-int-20.ppm")
                log.append(f"stop {stops}: screen-int-20.ppm captured "
                           f"({stop_k1 - 1} rounds)")
            elif stops == stop_make:
                inject_key(APP_SC_KEY7_MAKE, APP_ASCII_KEY7, "key7 make")
            elif stops == stop_break:
                inject_key(APP_SC_KEY7_BREAK, APP_ASCII_KEY7, "key7 break")
            elif stops == stop_cap2:
                ppm2 = screendump(art, name="screen-int-71-key.ppm")
                log.append(f"stop {stops}: screen-int-71-key.ppm captured "
                           f"({stop_cap2 - 1} rounds)")
        sess.clear_hbreak(va)
        inject_key(APP_SC_ESC, APP_ASCII_ESC, "ESC make")
        log.append("hbreak cleared, ESC injected, detaching")
    except OSError as e:
        errors.append(f"interactive leg: {e}")
    finally:
        if sess is not None:
            sess.detach()
    return {"ppm1": ppm1, "ppm2": ppm2, "log": log, "errors": errors}


def app_frame_match(ppm_data, exp):
    """True when the 640x320 APP rectangle inside a PPM equals a BGRX
    model frame pixel for pixel (task 141 interactive tetris evidence,
    the same comparison the frozen IBM game frame uses)."""
    if ppm_data is None or exp is None:
        return False
    try:
        w, h, px = parse_ppm(ppm_data)
    except ValueError:
        return False
    if ((w, h) != (PPM_WIDTH, PPM_HEIGHT)
            or len(exp) != APP_FB_W * APP_FB_H * 4):
        return False
    for row in range(APP_FB_H):
        for col in range(APP_FB_W):
            soff = ((APP_FB_Y + row) * w + (APP_FB_X + col)) * 3
            foff = (row * APP_FB_W + col) * 4
            if (px[soff] != exp[foff + 2]
                    or px[soff + 1] != exp[foff + 1]
                    or px[soff + 2] != exp[foff]):
                return False
    return True


def app_frame_lit(ppm_data):
    """Non-black pixel count inside the APP rectangle, None on a parse
    failure: the 'not all black' half of the W1 stepping evidence."""
    if ppm_data is None:
        return None
    try:
        w, h, px = parse_ppm(ppm_data)
    except ValueError:
        return None
    if (w, h) != (PPM_WIDTH, PPM_HEIGHT):
        return None
    n = 0
    for row in range(APP_FB_H):
        base = ((APP_FB_Y + row) * w + APP_FB_X) * 3
        rowpx = px[base:base + APP_FB_W * 3]
        n += sum(1 for i in range(0, len(rowpx), 3)
                 if rowpx[i:i + 3] != b"\x00\x00\x00")
    return n


def judge(serial, debug, counters, early_exit, gui_mode="ok", ppm=None,
          ppm_echo=None, ppm_gui=None, blk=None, fs=None, fsprobe=None,
          app=None, ppm_game=None, app_interactive=False, menu=None,
          five=None):
    """Pure smoke verdict: (ok, report lines). No I/O in here, so the
    decision can be exercised unit-style against mutated serial/debug
    fixtures (the task-67 sensitivity evidence).

    gui_mode="ok"   the default ten-marker PASS path plus the frozen
                    frame PPM pixel assertions on THREE captures:
                    `ppm_gui` (the GUI-G1 pattern frame, captured in
                    the bounded handshake window before the desktop
                    repaint), `ppm_echo` (echo frame, cursor at the
                    initial position) and `ppm` (final frame, cursor
                    moved and the old position restored), raw PPM bytes.
    gui_mode="skip" the non-fatal GOP fallback path: eight OK markers,
                    exactly one K64-GUI-SKIP and one K64-DESKTOP-SKIP
                    between INPUT and FOUNDATION, no K64-GUI-OK /
                    K64-DESKTOP-OK, no pixel assertions. Never mixed
                    into the default PASS path.
    The -d int vector delivery counts (v=0x21/v=0x2c) are asserted in
    both modes against the deterministic injection-chain derivation.

    blk= is the FS-v1 M1 evidence dict from collect_blk_evidence(); the
    appended verdict block runs ONLY when it is not None, so the frozen
    judge fixtures of tests/stage2-fb/check-gui-judge.py (which never
    pass it) keep their exact check count and decisions. fs= is the
    FS-v1 M2 evidence dict from collect_fs_evidence(), the same trailing
    optional pattern: the twelfth marker K64-FS-OK and the fs verdict
    are asserted only when it is passed (--fs). fsprobe= is the FS-v1 M3
    evidence dict from collect_fsprobe_evidence(), the same pattern once
    more: the thirteenth marker K64-FSPROBE-OK and the Ring 3 verdict
    are asserted only when it is passed (--fsprobe).
    """
    lines = []
    ok = True

    def req(cond, label):
        nonlocal ok
        lines.append(f"  [{'ok' if cond else 'FAIL'}] {label}")
        if not cond:
            ok = False

    terminal = [t for t in TERMINAL_MARKERS if t in serial]
    req(bool(terminal),
        "terminal marker seen (K64-FOUNDATION-OK or K64-CPU-PANIC)")
    positions = []
    markers = (MARKERS if gui_mode == "ok"
               else tuple(m for m in MARKERS
                          if m not in (GUI_OK_MARKER, DESKTOP_OK_MARKER)))
    for m in markers:
        n = serial.count(m)
        req(n == 1, f"{m} count = {n} (need exactly 1)")
        positions.append(serial.find(m))
    ordered = (all(p >= 0 for p in positions)
               and positions == sorted(positions)
               and len(set(positions)) == len(positions))
    req(ordered, f"the {len(markers)} OK markers appear in order")
    for m in FORBIDDEN_MARKERS:
        req(m not in serial, f"no {m} in serial")
    req("Triple fault" not in debug, "no triple fault in qemu-debug.log")
    # -d cpu_reset logs the QEMU power-on reset AND one OVMF platform reset
    # during firmware init; anything beyond those two is unexpected
    resets = len(re.findall(r"CPU Reset", debug))
    req(resets <= 2, f"CPU Reset lines = {resets} (<=2: power-on + OVMF)")
    req(not early_exit, "QEMU did not exit early")

    matches = PASSED_RE.findall(serial)
    if len(matches) != 1:
        req(False, "CPU selftest passed line parse: "
                   f"{len(matches)} matches (need exactly 1)")
    else:
        passed = int(matches[0], 16)
        req(passed == EXPECTED_PASSED,
            f"CPU selftest passed = {passed:#x} (need {EXPECTED_PASSED:#x})")
    expected = (EXPECTED_COUNTERS_FIVE if five is not None
                else EXPECTED_COUNTERS_MENU if menu is not None
                else EXPECTED_COUNTERS if gui_mode == "ok"
                else EXPECTED_COUNTERS_SKIP)
    for name, _ in COUNTER_SYMBOLS:
        want = expected[name]
        if name not in counters:
            req(False, f"{name} unreadable (gdbstub memory read failed)")
        else:
            got = counters[name]
            req(got == want, f"{name} = {got:#x} (need {want:#x})")

    # -d int vector delivery evidence: one delivery per 8042 byte, the
    # expectation derives from the whole injection chain, never from a
    # runtime observation (the IRQ-v2 deferred item, landed by task 96)
    vec_mode = ("five" if five is not None
                else "menu" if menu is not None else gui_mode)
    for vec, want in EXPECTED_VECTORS[vec_mode].items():
        got = len(re.findall(r"\bv=" + vec + r"\b", debug))
        req(got == want,
            f"-d int v={vec} deliveries = {got} (need {want}, derived)")

    def judge_ppm(raw, probes, label):
        if raw is None:
            req(False, f"{label} screendump PPM captured")
            return
        try:
            w, h, px = parse_ppm(raw)
        except ValueError as e:
            req(False, f"{label} PPM parse: {e}")
            return
        req((w, h) == (PPM_WIDTH, PPM_HEIGHT),
            f"{label} PPM size {w}x{h} (need {PPM_WIDTH}x{PPM_HEIGHT})")
        if (w, h) == (PPM_WIDTH, PPM_HEIGHT):
            for x, y, want in probes:
                off = (y * w + x) * 3
                got = (px[off] << 16) | (px[off + 1] << 8) | px[off + 2]
                req(got == want,
                    f"{label} pixel ({x},{y}) = {got:#08x} "
                    f"(need {want:#08x})")

    # DESKTOP-v1 verdict, kept separate from the shared checks above
    if gui_mode == "ok":
        req(GUI_SKIP_MARKER not in serial,
            "no K64-GUI-SKIP in serial (ok mode)")
        req(DESKTOP_SKIP_MARKER not in serial,
            "no K64-DESKTOP-SKIP in serial (ok mode)")
        judge_ppm(ppm_gui, PPM_GUI_PROBES, "gui")
        judge_ppm(ppm_echo, PPM_COMMON_PROBES + PPM_ECHO_PROBES, "echo")
        # DESKTOP-v2: the default final frame is the wallpaper desktop;
        # the --app final frame is the frozen plain app_redraw_desktop
        # repaint (same coordinates, same count, the *_APP_* tables);
        # the --menu final frame is the blank-click close frame: the
        # cursor ended at (700,600) and the menu rectangle is restored
        if menu is not None:
            judge_ppm(ppm, PPM_COMMON_PROBES + PPM_MENU_CLOSE_PROBES,
                      "final")
        elif five is not None:
            # task 125 T5: the --five final capture is the SECOND exit
            # frame, the full wallpaper desktop with the cursor at the
            # entry center (100,49) and the running indicator erased
            judge_ppm(ppm, PPM_COMMON_PROBES + PPM_FIVE_EXIT_PROBES,
                      "final")
        else:
            final_common = (PPM_APP_COMMON_PROBES if app is not None
                            else PPM_COMMON_PROBES)
            final_extra = (PPM_APP_FINAL_PROBES if app is not None
                           else PPM_FINAL_PROBES)
            judge_ppm(ppm, final_common + final_extra
                      + (APP_EXIT_PROBES if app is not None else ()), "final")
    else:
        req(GUI_OK_MARKER not in serial,
            "no K64-GUI-OK in serial (skip mode)")
        req(DESKTOP_OK_MARKER not in serial,
            "no K64-DESKTOP-OK in serial (skip mode)")
        n = serial.count(GUI_SKIP_MARKER)
        req(n == 1, f"{GUI_SKIP_MARKER} count = {n} (need exactly 1)")
        n = serial.count(DESKTOP_SKIP_MARKER)
        req(n == 1, f"{DESKTOP_SKIP_MARKER} count = {n} (need exactly 1)")
        p_input = serial.find("K64-INPUT-OK")
        p_skip = serial.find(GUI_SKIP_MARKER)
        p_dskip = serial.find(DESKTOP_SKIP_MARKER)
        p_found = serial.find("K64-FOUNDATION-OK")
        req(p_input >= 0 and p_skip > p_input and p_found > p_skip,
            "K64-GUI-SKIP sits between K64-INPUT-OK and K64-FOUNDATION-OK")
        req(p_skip >= 0 and p_dskip > p_skip and p_found > p_dskip,
            "K64-DESKTOP-SKIP sits between K64-GUI-SKIP and "
            "K64-FOUNDATION-OK")

    # FS-v1 M1 verdict, appended AFTER every frozen decision above
    if blk is not None:
        n = serial.count(BLK_OK_MARKER)
        req(n == 1, f"{BLK_OK_MARKER} count = {n} (need exactly 1)")
        p_prev = serial.find(DESKTOP_OK_MARKER if gui_mode == "ok"
                             else DESKTOP_SKIP_MARKER)
        p_blk = serial.find(BLK_OK_MARKER)
        p_found = serial.find("K64-FOUNDATION-OK")
        req(p_prev >= 0 and p_blk > p_prev and p_found > p_blk,
            "K64-BLK-OK sits between the desktop marker and "
            "K64-FOUNDATION-OK")
        # FS-v1 M2: the foundation runs the fs legs unconditionally, so
        # blk_test_reads now totals 7 + 5 init chunks + the fs cache
        # misses (22 plain / 46 BIG); the image sha follows the attached
        # disk (the BIG.BIN image is a separately frozen sha)
        expect_big = blk.get("expect_big") is True
        want_reads = BLK_TEST_READS_FS_BIG if expect_big else BLK_TEST_READS_FS
        want_sha = (FS_BIG_IMAGE_SHA256 if expect_big else
                    APP_IMAGE_SHA256 if blk.get("expect_app") else
                    BLK_IMAGE_SHA256)
        for name, want in (("blk_test_mask", 0),
                           ("blk_test_reads", want_reads),
                           ("blk_capacity", BLK_TEST_CAPACITY),
                           ("blk_ready", 1)):
            got = blk.get(name)
            req(got == want,
                f"{name} = {got if got is None else hex(got)} "
                f"(need {want:#x})")
        qnum = blk.get("blk_qnum")
        req(qnum is not None and 0 < qnum <= 256
            and qnum & (qnum - 1) == 0,
            f"blk_qnum = {qnum} (need a power of two <= 256)")
        req(blk.get("lba0_match") is True,
            "guest LBA0 buffer == disk.img sector 0 byte for byte")
        req(blk.get("data_match") is True,
            f"guest LBA{BLK_TEST_DATA_LBA} buffer == disk.img sector "
            f"{BLK_TEST_DATA_LBA} byte for byte")
        req(blk.get("image_sha256") == want_sha,
            "disk image sha256 matches the frozen FS-v1 image in use")
        for e in blk.get("errors", ()):
            req(False, f"blk evidence: {e}")

    # FS-v1 M2 verdict, appended AFTER every frozen decision and the
    # blk block above; runs only when fs= is passed (--fs)
    if fs is not None:
        n = serial.count(FS_OK_MARKER)
        req(n == 1, f"{FS_OK_MARKER} count = {n} (need exactly 1)")
        p_blk = serial.find(BLK_OK_MARKER)
        p_fs = serial.find(FS_OK_MARKER)
        p_found = serial.find("K64-FOUNDATION-OK")
        req(p_blk >= 0 and p_fs > p_blk and p_found > p_fs,
            "K64-FS-OK sits between K64-BLK-OK and K64-FOUNDATION-OK")
        expect_big = fs.get("expect_big") is True
        for name, want in (("fs_test_mask", 0),
                           ("fs_ready", 1),
                           ("fs_clusters", FS_TEST_CLUSTERS),
                           ("fs_test_reads",
                            FS_TEST_READS_BIG if expect_big else FS_TEST_READS),
                           ("fs_test_big_present", 1 if expect_big else 0)):
            got = fs.get(name)
            req(got == want,
                f"{name} = {got if got is None else hex(got)} "
                f"(need {want:#x})")
        req(fs.get("ibm_match") is True,
            "guest IBM.CH8 buffer == ROM source and image LBA 101 "
            "byte for byte")
        req(fs.get("tetris_match") is True,
            "guest TETRIS1.CH8 buffer == ROM source and image LBA 103 "
            "byte for byte")
        if expect_big:
            req(fs.get("big_match") is True,
                "guest BIG.BIN buffer == frozen payload formula and "
                "image cluster span byte for byte")
        else:
            req(fs.get("big_zero") is True,
                "guest BIG.BIN buffer pristine zero on the plain image")
        req(fs.get("dirents_match") is True,
            "guest readdir buffer == the raw image directory entries")
        req(fs.get("stat_match") is True,
            "guest stat buffer == the synthesized root stat")
        req(fs.get("image_sha256") ==
            (FS_BIG_IMAGE_SHA256 if expect_big else
             APP_IMAGE_SHA256 if fs.get("expect_app") else FS_IMAGE_SHA256),
            "fs evidence image sha256 matches the frozen image in use")
        for e in fs.get("errors", ()):
            req(False, f"fs evidence: {e}")

    # FS-v1 M3 verdict, appended AFTER every frozen decision and the
    # blk/fs blocks above; runs only when fsprobe= is passed (--fsprobe)
    if fsprobe is not None:
        n = serial.count(FSPROBE_OK_MARKER)
        req(n == 1, f"{FSPROBE_OK_MARKER} count = {n} (need exactly 1)")
        p_fs = serial.find(FS_OK_MARKER)
        p_fp = serial.find(FSPROBE_OK_MARKER)
        p_found = serial.find("K64-FOUNDATION-OK")
        req(p_fs >= 0 and p_fp > p_fs and p_found > p_fp,
            "K64-FSPROBE-OK sits between K64-FS-OK and K64-FOUNDATION-OK")
        expect_big = fsprobe.get("expect_big") is True
        for name, want in (("fsys_ready", 1),
                           ("fsys_probe_mask", FSYS_FP_MASK_ALL),
                           ("fsys_probe_fail", (1 << 64) - 1),
                           ("fsys_exit_code", 0),
                           ("fsys_table_delta", FSYS_TABLE_BUDGET),
                           ("fsys_saved_fs_reads",
                            FS_TEST_READS_BIG if expect_big
                            else FS_TEST_READS),
                           ("fsys_saved_blk_reads",
                            BLK_TEST_READS_FS_BIG if expect_big
                            else BLK_TEST_READS_FS),
                           # task 138 W3: the teardown receipts
                           ("fsys_pages_held", 0),
                           ("fsys_table_released", FSYS_TABLE_BUDGET)):
            got = fsprobe.get(name)
            req(got == want,
                f"{name} = {got if got is None else hex(got)} "
                f"(need {want:#x})")
        req(fsprobe.get("evidence_match") is True,
            "the eight evidence qwords == fd 0 / 132 / EOF 0 / seek 124 / "
            "size 132 / attr 1 / dir 64 / ENOENT -3")
        req(fsprobe.get("guest_head_match") is True,
            "guest mask/fail page head == the kernel evidence copy")
        req(fsprobe.get("read_match") is True,
            "guest IBM.CH8 landing buffer == ROM source and image "
            "LBA 101 byte for byte")
        req(fsprobe.get("stat_match") is True,
            "guest stat buffer == size 132 / attr 1 / zero reserved")
        req(fsprobe.get("dirents_match") is True,
            "guest readdir buffer == the raw image /ROMS entries")
        for key in ("enoent", "efault", "einval", "enotsup"):
            req(fsprobe.get(key + "_match") is True,
                f"guest {key} verdict matches the frozen K64 errno")
        # task 138 W3: no user domain residue after the probe verdict
        req(fsprobe.get("user_va_unmapped") is True,
            "the code, data and top stack user VAs are unreadable "
            "after the teardown")
        req(fsprobe.get("pages_zeroed") is True,
            "the fsys_pages frame array is zeroed after the teardown")
        req(fsprobe.get("net_table_invariant") is True,
            "net table allocs-drops is back at the probe baseline")
        req(fsprobe.get("pdpt4_clear") is True,
            "the user PDPT slot 4 is clear again after the teardown")
        req(fsprobe.get("anchor0_us_stays") is True,
            "the one-way anchor 0 US bit survives the teardown")
        for e in fsprobe.get("errors", ()):
            req(False, f"fsprobe evidence: {e}")

    # APP-v1 verdict, appended AFTER every frozen decision and the
    # blk/fs/fsprobe blocks above; runs only when app= is passed (--app)
    if app is not None:
        n = serial.count(APP_OK_MARKER)
        req(n == 1, f"{APP_OK_MARKER} count = {n} (need exactly 1)")
        p_fp = serial.find(FSPROBE_OK_MARKER)
        p_app = serial.find(APP_OK_MARKER)
        p_found = serial.find("K64-FOUNDATION-OK")
        req(p_fp >= 0 and p_app > p_fp and p_found > p_app,
            "K64-APP-OK sits between K64-FSPROBE-OK and K64-FOUNDATION-OK")
        req(APP_SKIP_MARKER not in serial,
            "no K64-APP-SKIP in serial (the app image carries the program)")
        for name, want in (("app_ready", 1),
                           ("app_stage", APP_STAGE_DONE),
                           ("app_exit_code", 0),
                           ("app_fault_vector", 0),
                           ("app_image_size", APP_PROGRAM_SIZE),
                           ("app_table_delta", APP_TABLE_BUDGET),
                           ("app_table_released", APP_TABLE_BUDGET),
                           ("app_pages_held", 0),
                           ("app_interactive",
                            1 if app_interactive else 0),
                           ("app_fd_swept", 0),
                           ("app_echo_saved", 1),
                           ("app_key_count",
                            APP_INT_KEY_COUNT if app_interactive else 0),
                           ("app_saved_fs_reads", FS_TEST_READS),
                           ("app_saved_blk_reads", BLK_TEST_READS_FS),
                           ("fs_test_reads", FS_TEST_READS),
                           ("blk_test_reads", BLK_TEST_READS_FS)):
            got = app.get(name)
            req(got == want,
                f"{name} = {got if got is None else hex(got)} "
                f"(need {want:#x})")
        blit = app.get("app_blit_count")
        blit_need = APP_INT_MIN_BLIT if app_interactive else 1
        blit_note = (", the IBM logo plus tetris frames)" if app_interactive
                     else ", the IBM logo)")
        req(blit is not None and blit >= blit_need,
            f"app_blit_count = {blit} (need >= {blit_need}" + blit_note)
        poll = app.get("app_poll_count")
        poll_need = APP_INT_MIN_POLL if app_interactive else 1
        req(poll is not None and poll >= poll_need,
            f"app_poll_count = {poll} (need >= {poll_need})")
        req(app.get("user_va_unmapped") is True,
            "the code, data and top stack user VAs are unreadable "
            "after the app teardown")
        req(app.get("pages_zeroed") is True,
            "the app_pages frame array is zeroed after the teardown")
        req(app.get("frames_released") is True,
            "app_pages_held is back at 0 after the teardown")
        req(app.get("released_is_budget") is True,
            "app_table_released == the +3 table budget")
        req(app.get("net_table_invariant") is True,
            "net table allocs-drops is back at the app baseline")
        req(app.get("pdpt4_clear") is True,
            "the user PDPT slot 4 is clear again after the teardown")
        req(app.get("anchor0_us_stays") is True,
            "the one-way anchor 0 US bit survives the teardown")
        req(app.get("image_sha256") == APP_IMAGE_SHA256,
            "disk image sha256 matches the frozen APP-v1 image")
        # the game frame: the common desktop probes still hold outside
        # the BLIT rectangle, plus the model lit/unlit cell probes
        judge_ppm(ppm_game,
                  PPM_COMMON_PROBES + app.get("game_probes", ()), "game")
        # the whole 640x320 rectangle == the chip8 model expected frame
        exp = app.get("expected_frame")
        if ppm_game is None or exp is None:
            req(False, "game frame rectangle reference available")
        else:
            try:
                w, h, px = parse_ppm(ppm_game)
            except ValueError as e:
                req(False, f"game frame PPM parse: {e}")
                w, h, px = 0, 0, b""
            match = ((w, h) == (PPM_WIDTH, PPM_HEIGHT)
                     and len(exp) == APP_FB_W * APP_FB_H * 4)
            if match:
                for row in range(APP_FB_H):
                    for col in range(APP_FB_W):
                        soff = ((APP_FB_Y + row) * w
                                + (APP_FB_X + col)) * 3
                        foff = (row * APP_FB_W + col) * 4
                        if (px[soff] != exp[foff + 2]
                                or px[soff + 1] != exp[foff + 1]
                                or px[soff + 2] != exp[foff]):
                            match = False
                            break
                    if not match:
                        break
            req(match, "game frame rectangle == the chip8 model expected "
                       "frame pixel for pixel (640x320 BGRX)")
        # task 141 W1: the interactive tetris stepping evidence. Only
        # when --app-interactive drove the round loop; the frozen --app
        # fixtures never pass the flag, their check count is unchanged.
        if app_interactive:
            req(app_frame_match(app.get("int_ppm1"),
                                app.get("int_expected1")),
                "interactive frame @20 rounds == the model tetris frame "
                "pixel for pixel (640x320 BGRX)")
            req(app_frame_match(app.get("int_ppm2"),
                                app.get("int_expected2")),
                "interactive frame @71 rounds (key7 tap) == the model "
                "key frame pixel for pixel (640x320 BGRX)")
            exp2 = app.get("int_expected2")
            nokey = app.get("int_expected_nokey")
            req(exp2 is not None and nokey is not None and exp2 != nokey,
                "the key7 tap frame differs from the no-key reference "
                "(the injected direction key changed the game)")
            lit1 = app_frame_lit(app.get("int_ppm1"))
            req(lit1 is not None and lit1 > 0,
                f"the round-20 tetris frame is not all black "
                f"({lit1} lit pixels)")
            for e in app.get("int_errors", ()):
                req(False, f"interactive leg: {e}")
        for e in app.get("errors", ()):
            req(False, f"app evidence: {e}")

    # DESKTOP-v2 T3 verdict, appended AFTER every frozen decision above;
    # runs only when menu= is passed (--menu), so the frozen judge
    # fixtures keep their exact check count and decisions
    if menu is not None:
        for name, want in EXPECTED_MENU_FINAL.items():
            got = counters.get(name)
            req(got == want,
                f"{name} = {got if got is None else hex(got)} "
                f"(need {want:#x})")
        judge_ppm(menu.get("open"), PPM_MENU_OPEN_PROBES, "menu-open")
        judge_ppm(menu.get("launch"), PPM_MENU_LAUNCH_PROBES, "menu-launch")
        judge_ppm(menu.get("close"), PPM_MENU_CLOSE_PROBES, "menu-close")
        # task 143 (DEFECT-1): the start-click box must be sprite-free
        # once the cursor moved on to the entry / away to (700,600)
        judge_ppm(menu.get("launch"), PPM_RESIDUE_BOX_PROBES,
                  "menu-launch-residue")
        judge_ppm(menu.get("close"), PPM_RESIDUE_BOX_PROBES,
                  "menu-close-residue")
        mb = residue_box(ppm)
        lb = residue_box(menu.get("launch"))
        req(mb is not None and lb == mb,
            "menu launch start-click box == the final frame box "
            "pixel for pixel (no cursor residue)")
        for e in menu.get("errors", ()):
            req(False, f"menu leg: {e}")

    # DESKTOP-v2 T5 verdict (task 125), appended AFTER every frozen
    # decision above; runs only when five= is passed (--five), so the
    # frozen judge fixtures keep their exact check count and decisions
    if five is not None:
        for name, want in EXPECTED_FIVE_FINAL.items():
            got = counters.get(name)
            req(got == want,
                f"{name} = {got if got is None else hex(got)} "
                f"(need {want:#x})")
        judge_ppm(five.get("menu"), PPM_MENU_OPEN_PROBES, "five-menu")
        judge_ppm(five.get("game"), PPM_RUN_LIT_PROBES,
                  "five-game-indicator")
        judge_ppm(five.get("game2"), PPM_RUN_LIT_PROBES,
                  "five-game2-indicator")
        # task 143 (DEFECT-1): the cursor is SUSPENDED in both game
        # frames -- the start-click box must equal the clean exit frame
        # box (the full-repaint truth) pixel for pixel, all 216 pixels
        judge_ppm(five.get("game"), PPM_RESIDUE_BOX_PROBES,
                  "five-game-residue")
        judge_ppm(five.get("game2"), PPM_RESIDUE_BOX_PROBES,
                  "five-game2-residue")
        base_box = residue_box(ppm)
        gb = residue_box(five.get("game"))
        g2b = residue_box(five.get("game2"))
        req(base_box is not None and gb == base_box,
            "five game start-click box == the exit frame box "
            "pixel for pixel (no cursor residue)")
        req(base_box is not None and g2b == base_box,
            "five game2 start-click box == the exit frame box "
            "pixel for pixel (no relaunch residue)")
        # the relaunch game frame == the chip8 model expected frame: the
        # DONE-gate reset re-published IBM_READY and blitted the logo
        # again (a one-shot blit gate would leave the rectangle black)
        req(app_frame_match(five.get("game2"),
                            (app or {}).get("expected_frame")),
            "five game2 frame == the chip8 model expected frame "
            "pixel for pixel (the relaunch really re-ran)")
        g1, g2 = five.get("game"), five.get("game2")
        req(g1 is not None and g2 is not None and g1 == g2,
            "five game2 frame == game frame byte for byte "
            "(no relaunch drawing leak)")
        judge_ppm(five.get("exit"),
                  PPM_COMMON_PROBES + PPM_FIVE_EXIT_PROBES, "five-exit")
        e1, e2 = five.get("exit"), five.get("exit2")
        req(e1 is not None and e2 is not None and e1 == e2,
            "five exit2 frame == exit frame pixel for pixel "
            "(no relaunch leak, the full repaint is deterministic)")
        for e in five.get("errors", ()):
            req(False, f"five leg: {e}")
    return ok, lines


def qemu_argv(args, art, esp):
    # the kernel needs long mode, NX and POPCNT; bare "qemu64" has no
    # POPCNT and the MM module would take #UD, hence the +popcnt default
    argv = [args.qemu,
            "-accel", "tcg",
            "-m", str(args.mem),
            "-cpu", args.cpu,
            "-smp", "1",
            "-bios", os.path.join(REPO, "OVMF.fd"),
            "-drive", f"driver=vvfat,rw=on,dir={esp}",
            # FS-v1 M1: the transitional virtio-blk device is mandatory
            # in the smoke matrix, there is no blk skip path; FS-v1 M2
            # adds --disk to select the plain or BIG.BIN frozen image
            "-drive", ("if=none,id=blk0,file=" + args.disk
                       + ",format=raw"),
            "-device", "virtio-blk-pci-transitional,drive=blk0",
            "-nic", "none",
            "-display", "none",
            "-monitor", ("unix:" + os.path.join(art, "monitor.sock")
                         + ",server=on,wait=off"),
            "-serial", "file:" + os.path.join(art, "serial.log"),
            "-d", "int,guest_errors,cpu_reset",
            "-D", os.path.join(art, "qemu-debug.log"),
            "-no-reboot", "-no-shutdown",
            "-gdb", f"tcp:127.0.0.1:{args.port}"]
    if not args.no_freeze:
        argv.append("-S")
    return argv


def print_guidance(args, art, va, off, elf):
    sym = elf if elf else os.path.join(art, "kernel64.elf")
    print()
    print("=" * 72)
    print("run-qemu-debug: QEMU frozen (-S), gdbstub on 127.0.0.1:%d"
          % args.port)
    print(f"  kernel entry VA = {va:#x} "
          f"(VIRT_KERNEL_BASE + header offset {off:#x}, from THIS build)")
    print(f"  serial log      : {os.path.join(art, 'serial.log')}")
    print(f"  qemu debug log  : {os.path.join(art, 'qemu-debug.log')}")
    print()
    print("  lldb (Apple /usr/bin/lldb, no install needed):")
    print(f"    target create {sym}")
    print("    # if the ELF is unavailable: settings set target.triple "
          "x86_64-unknown-elf")
    print(f"    gdb-remote 127.0.0.1:{args.port}")
    print(f"    breakpoint set -H -a {va:#x}   # hardware breakpoint (hbreak)")
    print("    continue")
    print(f"    disassemble -s $pc -c 10       # expect cli / cld / "
          "mov r15, rcx")
    print("    # ET_REL caveat: lldb leaves symbol-name breakpoints "
          "(-n halt_forever)")
    print("    # pending; use -a with the addresses printed by "
          "gen-kernel-symbols.py")
    print()
    print("  gdb (e.g. brew x86_64-elf-gdb, only if lldb misbehaves):")
    print("    set architecture i386:x86-64   # before target, avoids an "
          "oversized 'g' packet in long mode")
    if elf:
        print(f"    add-symbol-file {sym}")
    print(f"    target remote 127.0.0.1:{args.port}")
    print(f"    hbreak *{va:#x}")
    print("    continue")
    print("    x/10i $pc")
    print()
    print("  Notes: TCG has no watchpoints, use hardware breakpoints; with "
          "-S the")
    print("  serial log stays empty until you continue -- that is expected.")
    print("  Ctrl-C here stops QEMU and cleans up the session.")
    print("=" * 72)
    print()


def read_log(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def menu_leg(args, art, counter_addrs):
    """DESKTOP-v2 T3: drive the frozen MENU_INJECT_SCRIPT against the
    resident desktop loop and capture the three menu frames. Returns
    the evidence dict the judge menu= block asserts; every failure is
    recorded, never silently skipped."""
    ev = {"open": None, "launch": None, "close": None,
          "errors": [], "log": []}
    if counter_addrs.get("input_test_mouse_count") is None:
        ev["errors"].append("input_test_mouse_count address unresolved")
        return ev
    deadline = time.monotonic() + MENU_LEG_TIMEOUT

    def read(name):
        addr = counter_addrs.get(name)
        if addr is None:
            return None
        try:
            return gdb_read_mem(args.port, addr, 8)
        except OSError:
            return None

    def gates_ok(gates):
        for name, op, want in gates:
            v = read(name)
            if v is None:
                return False
            if op == ">=" and v < want:
                return False
            if op == "==" and v != want:
                return False
        return True

    for i, (cmd, count_gate, gates, capture) in enumerate(MENU_INJECT_SCRIPT):
        sent = False
        while time.monotonic() < deadline:
            try:
                reply = monitor_command(art, cmd)
            except OSError as e:
                reply = f"Error: {e}"
            low = reply.lower()
            if not (low.startswith("error") or low.startswith("unknown")):
                sent = True
                break
            time.sleep(0.2)
        if not sent:
            ev["errors"].append(f"menu step {i}: {cmd!r} never accepted")
            return ev
        ev["log"].append(f"menu step {i}: {cmd!r} sent")
        reached = False
        while time.monotonic() < deadline:
            n = read("input_test_mouse_count")
            if n is not None and n >= count_gate and gates_ok(gates):
                reached = True
                break
            time.sleep(0.05)
        if not reached:
            ev["errors"].append(
                f"menu step {i}: guest gate never reached "
                f"(mouse_count >= {count_gate}, gates {gates})")
            return ev
        ev["log"].append(f"menu step {i}: mouse_count={n} "
                         f"(gate {count_gate})")
        if capture:
            # let the pump finish the redraw after the published labels
            time.sleep(0.2)
            name = f"screen-menu-{capture}.ppm"
            try:
                ev[capture] = screendump(art, name=name)
                ev["log"].append(f"menu {capture}: {name} captured")
            except OSError as e:
                ev["errors"].append(f"menu {capture}: screendump failed: {e}")
    return ev


def five_leg(args, art, counter_addrs):
    """DESKTOP-v2 T5 (task 125): drive TWO resident click rounds against
    desktop_run after the boot hosted run (--app handshake, runs=1).
    Each round: start button press (menu frame) -> CHIP-8 entry press ->
    wait IBM_READY (game frame, running indicator lit) -> write EXIT_REQ
    -> wait DONE + the exits counter (exit frame, full wallpaper
    repaint). Round 2 proves the relaunch: the DONE-gate reset must
    re-publish IBM_READY and the exit frames must be pixel identical.
    16 packets total: mouse 6 -> 22, gates 7..22. Every failure is
    recorded, never silently skipped."""
    ev = {"menu": None, "game": None, "exit": None,
          "game2": None, "exit2": None, "errors": [], "log": []}
    for name in ("input_test_mouse_count", "app_stage",
                 "desktop_test_app_exits"):
        if counter_addrs.get(name) is None:
            ev["errors"].append(f"{name} address unresolved")
            return ev
    deadline = time.monotonic() + FIVE_LEG_TIMEOUT

    def read(name):
        addr = counter_addrs.get(name)
        if addr is None:
            return None
        try:
            return gdb_read_mem(args.port, addr, 8)
        except OSError:
            return None

    def gates_ok(gates):
        for name, op, want in gates:
            v = read(name)
            if v is None:
                return False
            if op == ">=" and v < want:
                return False
            if op == "==" and v != want:
                return False
        return True

    def send(i, cmd):
        while time.monotonic() < deadline:
            try:
                reply = monitor_command(art, cmd)
            except OSError as e:
                reply = f"Error: {e}"
            low = reply.lower()
            if not (low.startswith("error") or low.startswith("unknown")):
                ev["log"].append(f"five step {i}: {cmd!r} sent")
                return True
            time.sleep(0.2)
        ev["errors"].append(f"five step {i}: {cmd!r} never accepted")
        return False

    def wait(i, count_gate, gates=()):
        while time.monotonic() < deadline:
            n = read("input_test_mouse_count")
            if n is not None and n >= count_gate and gates_ok(gates):
                ev["log"].append(f"five step {i}: mouse_count={n} "
                                 f"(gate {count_gate})")
                return True
            time.sleep(0.05)
        ev["errors"].append(
            f"five step {i}: guest gate never reached "
            f"(mouse_count >= {count_gate}, gates {gates})")
        return False

    def cap(key):
        # let the pump finish the redraw after the published labels
        time.sleep(0.2)
        name = f"screen-five-{key}.ppm"
        try:
            ev[key] = screendump(art, name=name)
            ev["log"].append(f"five {key}: {name} captured")
        except OSError as e:
            ev["errors"].append(f"five {key}: screendump failed: {e}")

    def app_round(tag, exits_gate, gkey, ekey):
        # the entry press launched through desktop_launch_pump; the app
        # runs with IF = 0, the gdbstub still answers. Wait IBM_READY
        # (the relaunch reset re-publishes it), capture, request the
        # exit, wait the clean teardown accounting, capture the repaint
        while time.monotonic() < deadline:
            if read("app_stage") == APP_STAGE_IBM_READY:
                break
            time.sleep(0.05)
        else:
            ev["errors"].append(f"five {tag}: IBM_READY never published")
            return False
        cap(gkey)
        try:
            gdb_write_mem(args.port, counter_addrs["app_stage"],
                          APP_STAGE_EXIT_REQ, 8)
            ev["log"].append(f"five {tag}: EXIT_REQ written")
        except OSError as e:
            ev["errors"].append(f"five {tag}: EXIT_REQ failed: {e}")
            return False
        while time.monotonic() < deadline:
            if (read("app_stage") == APP_STAGE_DONE
                    and gates_ok((("desktop_test_app_exits", ">=",
                                   exits_gate),))):
                break
            time.sleep(0.05)
        else:
            ev["errors"].append(
                f"five {tag}: DONE/exits >= {exits_gate} never reached")
            return False
        cap(ekey)
        return True

    # round 1: (557,411) -> 5 moves -> (32,16) start press -> release ->
    # 1 move -> (100,49) entry press -> hosted run -> release
    moves1 = (("mouse_move -125 -100", 7), ("mouse_move -125 -100", 8),
              ("mouse_move -125 -100", 9), ("mouse_move -125 -100", 10),
              ("mouse_move -25 5", 11))
    for cmd, gate in moves1:
        if not send(gate, cmd) or not wait(gate, gate):
            return ev
    if not send(12, "mouse_button 1") or not wait(
            12, 12, (("desktop_test_start_clicks", ">=", 1),
                     ("desktop_menu_flag", "==", 1))):
        return ev
    cap("menu")
    if not send(13, "mouse_button 0") or not wait(13, 13):
        return ev
    if not send(14, "mouse_move 68 33") or not wait(14, 14):
        return ev
    if not send(15, "mouse_button 1") or not wait(
            15, 15, (("desktop_test_entry_clicks", ">=", 1),
                     ("desktop_test_app_runs", ">=", 2),
                     ("desktop_menu_flag", "==", 0))):
        return ev
    if not app_round("round1", 2, "game", "exit"):
        return ev
    if not send(16, "mouse_button 0") or not wait(16, 16):
        return ev
    # round 2: 1 move back -> (32,16) start press -> release -> 1 move ->
    # (100,49) entry press -> the RELAUNCH hosted run -> release
    if not send(17, "mouse_move -68 -33") or not wait(17, 17):
        return ev
    if not send(18, "mouse_button 1") or not wait(
            18, 18, (("desktop_test_start_clicks", ">=", 2),
                     ("desktop_menu_flag", "==", 1))):
        return ev
    if not send(19, "mouse_button 0") or not wait(19, 19):
        return ev
    if not send(20, "mouse_move 68 33") or not wait(20, 20):
        return ev
    if not send(21, "mouse_button 1") or not wait(
            21, 21, (("desktop_test_entry_clicks", ">=", 2),
                     ("desktop_test_app_runs", ">=", 3),
                     ("desktop_menu_flag", "==", 0))):
        return ev
    if not app_round("round2", 3, "game2", "exit2"):
        return ev
    if not send(22, "mouse_button 0") or not wait(22, 22):
        return ev
    return ev


def smoke(args, art, proc, counter_addrs, counter_errors):
    serial_path = os.path.join(art, "serial.log")
    deadline = time.monotonic() + args.timeout
    early_exit = False
    terminal_seen = False
    # IRQ-v2 injection handshake: poll input_test_stage through the
    # gdbstub, send one monitor command per stage, all bounded by the
    # smoke deadline. Stage 4 splits press/release: the release is only
    # sent after the guest consumed the press packet (mouse_count >= 2)
    # so QEMU can never coalesce press+release into a single packet.
    injected = set()
    press_sent = False
    release_sent = False
    inject_log = []
    # DESKTOP-v1 handshake state: polled only after K64-GUI-OK; the
    # echo frame screendump is captured at stage 3 BEFORE the move is
    # injected, so the cursor is still at the initial position
    d_injected = set()
    d_press_sent = False
    d_release_sent = False
    echo_ppm = None
    # GUI frame forensic handshake state, task 102: after K64-GUI-OK
    # the foundation publishes READY in desktop_gui_frame_stage and
    # spins a bounded pause window; capture screen-gui.ppm first, then
    # write RELEASE. A failed screendump never releases the guest: the
    # window times out into the EIO panic path, a FAIL, never a hang.
    gui_ppm = None
    gui_released = False
    # APP-v1 handshake state: after K64-FSPROBE-OK the foundation enters
    # app_launch and publishes IBM_READY (1) in app_stage on the first
    # BLIT; capture screen-game.ppm, then write EXIT_REQ (2). The guest
    # READ_KEY fabricates ESC, the program exits, the teardown repaints
    # the desktop and app_stage reaches DONE (3) before K64-APP-OK.
    game_ppm = None
    app_exit_written = False
    # task 141 W1 interactive leg state: the two tetris round-boundary
    # captures and the leg errors, merged into app_ev for the judge
    int_ppm1 = None
    int_ppm2 = None
    int_errors = []

    def inject(cmd, label):
        # a monitor reply starting with Error/unknown means the command
        # was rejected: log FAILED and return False so the handshake
        # retries it on a later poll round, never record a fake 'sent'
        try:
            reply = monitor_command(art, cmd)
        except OSError as e:
            inject_log.append(f"{label}: {cmd!r} FAILED: {e}")
            return False
        low = reply.lower()
        if low.startswith("error") or low.startswith("unknown"):
            inject_log.append(f"{label}: {cmd!r} FAILED: {reply!r}")
            return False
        inject_log.append(f"{label}: {cmd!r} sent")
        return True

    stage_addr = counter_addrs.get("input_test_stage")
    mouse_addr = counter_addrs.get("input_test_mouse_count")
    d_stage_addr = counter_addrs.get("desktop_test_stage")
    gui_stage_addr = counter_addrs.get("desktop_gui_frame_stage")
    app_stage_addr = counter_addrs.get("app_stage")
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            early_exit = True  # QEMU died on its own, e.g. -no-reboot on fault
            break
        # wait for the TERMINAL marker, not the first K64-BOOT-OK: the
        # whole foundation chain must have run before judging
        serial_now = read_log(serial_path)
        if any(t in serial_now for t in TERMINAL_MARKERS):
            terminal_seen = True
            break
        # drive the handshake only after the IRQ stage: no gdbstub
        # interference with OVMF or the earlier kernel stages. Early in
        # the input stage the data VA may still be unmapped: the gdbstub
        # answers 'E' and the read raises OSError -> skip this round
        if stage_addr is not None and "K64-IRQ-OK" in serial_now:
            try:
                stage = gdb_read_mem(args.port, stage_addr, 8)
            except OSError:
                stage = None
            if stage in INPUT_STAGE_INJECT and stage not in injected:
                if inject(INPUT_STAGE_INJECT[stage], f"stage {stage}"):
                    injected.add(stage)
            elif stage == 4 and mouse_addr is not None:
                if not press_sent:
                    press_sent = inject("mouse_button 1", "stage 4 press")
                elif not release_sent:
                    try:
                        n = gdb_read_mem(args.port, mouse_addr, 8)
                    except OSError:
                        n = 0
                    if n >= 2:
                        release_sent = inject("mouse_button 0",
                                              "stage 4 release")
        # the GUI frame capture runs BEFORE the desktop handshake: the
        # guest sits in the bounded window and desktop_test_stage stays
        # 0 until RELEASE is written, so the two never interleave
        if (not args.gui_skip and gui_stage_addr is not None
                and not gui_released and "K64-GUI-OK" in serial_now):
            try:
                g_stage = gdb_read_mem(args.port, gui_stage_addr, 8)
            except OSError:
                g_stage = None
            if g_stage == GUI_FRAME_READY:
                if gui_ppm is None:
                    try:
                        gui_ppm = screendump(art, name="screen-gui.ppm")
                        inject_log.append("gui frame: screen-gui.ppm "
                                          "captured")
                    except OSError as e:
                        inject_log.append("gui frame: screendump FAILED: "
                                          f"{e}")
                if gui_ppm is not None:
                    try:
                        gdb_write_mem(args.port, gui_stage_addr,
                                      GUI_FRAME_RELEASE, 8)
                        gui_released = True
                        inject_log.append("gui frame: RELEASE written")
                    except OSError as e:
                        inject_log.append(f"gui frame: release FAILED: {e}")
        # the desktop handshake runs after the GUI milestone; the same
        # bounded poll/inject discipline as the input handshake above
        if (not args.gui_skip and d_stage_addr is not None
                and "K64-GUI-OK" in serial_now):
            try:
                d_stage = gdb_read_mem(args.port, d_stage_addr, 8)
            except OSError:
                d_stage = None
            if d_stage in DESKTOP_STAGE_INJECT and d_stage not in d_injected:
                if d_stage == 3 and echo_ppm is None:
                    # the echo leg finished: capture the frame with the
                    # cursor at (517,387) BEFORE injecting the move
                    try:
                        echo_ppm = screendump(art, name="screen-echo.ppm")
                        inject_log.append("echo frame: screen-echo.ppm "
                                          "captured")
                    except OSError as e:
                        inject_log.append("echo frame: screendump FAILED: "
                                          f"{e}")
                if inject(DESKTOP_STAGE_INJECT[d_stage],
                          f"desktop stage {d_stage}"):
                    d_injected.add(d_stage)
            elif d_stage == 4 and mouse_addr is not None:
                if not d_press_sent:
                    d_press_sent = inject("mouse_button 1",
                                          "desktop stage 4 press")
                elif not d_release_sent:
                    try:
                        n = gdb_read_mem(args.port, mouse_addr, 8)
                    except OSError:
                        n = 0
                    # input exit 3 + move 1 + press 1 = 5: the release
                    # only after the guest consumed the press packet
                    if n >= 5:
                        d_release_sent = inject("mouse_button 0",
                                                "desktop stage 4 release")
        # the APP handshake runs after the fsys probe milestone; the
        # game frame is captured at IBM_READY before EXIT_REQ is written
        if (args.app and not args.gui_skip and app_stage_addr is not None
                and "K64-FSPROBE-OK" in serial_now):
            try:
                a_stage = gdb_read_mem(args.port, app_stage_addr, 8)
            except OSError:
                a_stage = None
            if a_stage == APP_STAGE_IBM_READY and game_ppm is None:
                try:
                    game_ppm = screendump(art, name="screen-game.ppm")
                    inject_log.append("game frame: screen-game.ppm "
                                      "captured")
                except OSError as e:
                    inject_log.append("game frame: screendump FAILED: "
                                      f"{e}")
                if game_ppm is not None and not app_exit_written:
                    if args.app_interactive:
                        # the ESC injection inside the leg replaces the
                        # EXIT_REQ write: the program exits through the
                        # real key path, app_interactive stays 1
                        res = interactive_leg(args, art, counter_addrs,
                                              deadline)
                        int_ppm1 = res["ppm1"]
                        int_ppm2 = res["ppm2"]
                        int_errors = res["errors"]
                        inject_log.extend(res["log"])
                        inject_log.extend("interactive leg: " + e
                                          for e in res["errors"])
                        app_exit_written = True
                    else:
                        try:
                            gdb_write_mem(args.port, app_stage_addr,
                                          APP_STAGE_EXIT_REQ, 8)
                            app_exit_written = True
                            inject_log.append("app frame: EXIT_REQ written")
                        except OSError as e:
                            inject_log.append(
                                f"app frame: EXIT_REQ FAILED: {e}")
        time.sleep(0.5)
    # DESKTOP-v2 T3: the menu drive runs only after the terminal marker,
    # against the resident desktop_run (sti+hlt) loop; the guest needs
    # no new handshake stage, the host gates on the public counters
    menu_ev = None
    if args.menu and not args.gui_skip:
        if terminal_seen and proc.poll() is None:
            menu_ev = menu_leg(args, art, counter_addrs)
            inject_log.extend(menu_ev["log"])
        else:
            menu_ev = {"open": None, "launch": None, "close": None,
                       "errors": ["menu leg not driven: no terminal marker "
                                  "or QEMU exited early"], "log": []}
    # DESKTOP-v2 T5 (task 125): the five-frame drive, two resident click
    # rounds after the boot hosted run; the same terminal-marker gate
    five_ev = None
    if args.five and not args.gui_skip:
        if terminal_seen and proc.poll() is None:
            five_ev = five_leg(args, art, counter_addrs)
            inject_log.extend(five_ev["log"])
        else:
            five_ev = {"menu": None, "game": None, "exit": None,
                       "game2": None, "exit2": None,
                       "errors": ["five leg not driven: no terminal marker "
                                  "or QEMU exited early"], "log": []}
    # give the kernel a moment to finish its report, then read the
    # memory-only counters while QEMU (and the halted kernel) still lives
    time.sleep(1.0)
    counters = {}
    read_errors = list(counter_errors)
    if proc.poll() is None and counter_addrs:
        got, errs = read_counters(args.port, counter_addrs)
        counters.update(got)
        read_errors.extend(errs)
    elif not terminal_seen:
        read_errors.append("counters not read: no terminal marker "
                           "before timeout")
    # forensic evidence: on any input counter mismatch, dump the raw
    # mouse packet state before killing QEMU
    forensic = []
    expected = (EXPECTED_COUNTERS_FIVE if args.five
                else EXPECTED_COUNTERS_MENU if args.menu
                else EXPECTED_COUNTERS_SKIP if args.gui_skip
                else EXPECTED_COUNTERS)
    if proc.poll() is None and any(
            counters.get(n) != expected[n]
            for n in ("input_test_selftest_mask", "input_test_kbd_count",
                      "input_test_mouse_count", "input_test_stage",
                      "desktop_test_selftest_mask", "desktop_test_stage",
                      "desktop_test_keys")):
        for name, size in INPUT_FORENSIC_SYMBOLS:
            if name not in counter_addrs:
                continue
            try:
                v = gdb_read_mem(args.port, counter_addrs[name], size)
                forensic.append(f"{name} = {v:#x}")
            except OSError as e:
                forensic.append(f"{name}: {e}")
    # capture the screen before killing QEMU; screen.ppm stays in the
    # artifact directory as forensic evidence in both GUI modes
    ppm = None
    ppm_path = os.path.join(art, "screen.ppm")
    if proc.poll() is None:
        try:
            ppm = screendump(art)
        except OSError as e:
            read_errors.append(f"screendump failed: {e}")
    # geometry evidence for the skip path: the live GOP mode the loader
    # handed over (or zeros when the FRAMEBUFFER flag was never set)
    fb_note = None
    if (args.gui_skip and proc.poll() is None
            and "video_fb_state" in counter_addrs):
        try:
            raw = gdb_read_mem(args.port, counter_addrs["video_fb_state"], 64)
            b = raw.to_bytes(64, "little")
            ready, = struct.unpack_from("<I", b, 0)
            width, height = struct.unpack_from("<II", b, 8)
            fb_note = (f"video_fb_state: ready={ready} "
                       f"geometry={width}x{height} (skip evidence)")
        except OSError as e:
            read_errors.append(f"video_fb_state read: {e}")
    # FS-v1 M1 evidence: the blk counters and both sector buffers are
    # read while the halted kernel still lives, in BOTH gui modes (the
    # blk stage runs after the desktop skip too)
    if proc.poll() is None:
        blk_ev = collect_blk_evidence(args, art, counter_addrs)
        fs_ev = (collect_fs_evidence(args, art, counter_addrs)
                 if args.fs else None)
        fsprobe_ev = (collect_fsprobe_evidence(args, art, counter_addrs)
                      if args.fsprobe else None)
        app_ev = (collect_app_evidence(args, art, counter_addrs)
                  if args.app else None)
    else:
        blk_ev = {"errors": ["blk evidence not read: QEMU exited early"]}
        fs_ev = ({"errors": ["fs evidence not read: QEMU exited early"]}
                 if args.fs else None)
        fsprobe_ev = (
            {"errors": ["fsprobe evidence not read: QEMU exited early"]}
            if args.fsprobe else None)
        app_ev = ({"errors": ["app evidence not read: QEMU exited early"]}
                  if args.app else None)
    if args.app_interactive and app_ev is not None:
        app_ev["int_ppm1"] = int_ppm1
        app_ev["int_ppm2"] = int_ppm2
        app_ev["int_errors"] = int_errors
    cleanup()

    serial = read_log(serial_path)
    debug = read_log(os.path.join(art, "qemu-debug.log"))
    ok, lines = judge(serial, debug, counters, early_exit,
                      gui_mode="skip" if args.gui_skip else "ok", ppm=ppm,
                      ppm_echo=echo_ppm, ppm_gui=gui_ppm, blk=blk_ev,
                      fs=fs_ev, fsprobe=fsprobe_ev, app=app_ev,
                      ppm_game=game_ppm,
                      app_interactive=args.app_interactive, menu=menu_ev,
                      five=five_ev)
    print()
    print("=" * 72)
    print(f"run-qemu-debug: smoke verdict: {'PASS' if ok else 'FAIL'}")
    for line in lines:
        print(line)
    for e in read_errors:
        print("  counter read: " + e)
    print("  input injection log:")
    if inject_log:
        for entry in inject_log:
            print("    " + entry)
    else:
        print("    (none)")
    if forensic:
        print("  input forensic state:")
        for entry in forensic:
            print("    " + entry)
    print(f"  terminal marker before timeout : {terminal_seen}")
    print(f"  gui screendump PPM  : {os.path.join(art, 'screen-gui.ppm')} "
          f"({'captured' if gui_ppm is not None else 'MISSING'})")
    print(f"  echo screendump PPM : {os.path.join(art, 'screen-echo.ppm')} "
          f"({'captured' if echo_ppm is not None else 'MISSING'})")
    if menu_ev is not None:
        for cap in ("open", "launch", "close"):
            print(f"  menu-{cap} screendump : "
                  f"{os.path.join(art, f'screen-menu-{cap}.ppm')} "
                  f"({'captured' if menu_ev[cap] is not None else 'MISSING'})")
    if five_ev is not None:
        for cap in ("menu", "game", "exit", "game2", "exit2"):
            print(f"  five-{cap} screendump : "
                  f"{os.path.join(art, f'screen-five-{cap}.ppm')} "
                  f"({'captured' if five_ev[cap] is not None else 'MISSING'})")
    print(f"  screendump PPM : {ppm_path} "
          f"({'captured' if ppm is not None else 'MISSING'})")
    if fb_note:
        print("  " + fb_note)
    print("  blk evidence log  : " + os.path.join(art, "blk-evidence.log"))
    for entry in (blk_ev.get("log")
                  or ["error: " + e for e in blk_ev.get("errors", ())]):
        print("    " + entry)
    if fs_ev is not None:
        print("  fs evidence log   : " + os.path.join(art, "fs-evidence.log"))
        for entry in (fs_ev.get("log")
                      or ["error: " + e for e in fs_ev.get("errors", ())]):
            print("    " + entry)
    if fsprobe_ev is not None:
        print("  fsprobe evidence  : "
              + os.path.join(art, "fsprobe-evidence.log"))
        for entry in (fsprobe_ev.get("log")
                      or ["error: " + e
                          for e in fsprobe_ev.get("errors", ())]):
            print("    " + entry)
    if app_ev is not None:
        print(f"  game screendump PPM : "
              f"{os.path.join(art, 'screen-game.ppm')} "
              f"({'captured' if game_ppm is not None else 'MISSING'})")
        if args.app_interactive:
            print(f"  int-20 screendump   : "
                  f"{os.path.join(art, 'screen-int-20.ppm')} "
                  f"({'captured' if int_ppm1 is not None else 'MISSING'})")
            print(f"  int-71 screendump   : "
                  f"{os.path.join(art, 'screen-int-71-key.ppm')} "
                  f"({'captured' if int_ppm2 is not None else 'MISSING'})")
        print("  app evidence log  : " + os.path.join(art, "app-evidence.log"))
        for entry in (app_ev.get("log")
                      or ["error: " + e
                          for e in app_ev.get("errors", ())]):
            print("    " + entry)
    if serial:
        print("  serial.log:")
        for line in serial.splitlines():
            print("    " + line)
    print("=" * 72)
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-freeze", action="store_true",
                    help="smoke mode: run without -S under a timeout")
    ap.add_argument("--gui-skip", action="store_true",
                    help="expect the non-fatal GUI skip path "
                         "(K64-GUI-SKIP) instead of K64-GUI-OK; no pixel "
                         "assertions, video_fb_state is read as evidence")
    ap.add_argument("--mem", type=int, default=128,
                    help="guest RAM in MiB (default 128)")
    ap.add_argument("--port", type=int, default=1234,
                    help="gdbstub port on 127.0.0.1 (default 1234)")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="smoke mode budget in seconds (default 120)")
    ap.add_argument("--art-dir",
                    help="artifact directory (default: "
                         "build/qemu-debug/<timestamp>-<pid>)")
    ap.add_argument("--qemu", default="qemu-system-x86_64")
    ap.add_argument("--cpu", default="qemu64,+popcnt",
                    help="QEMU -cpu model (default qemu64,+popcnt: the "
                         "kernel needs long mode, NX and POPCNT)")
    ap.add_argument("--disk",
                    default=os.path.join(REPO, "build", "fs-v1", "disk.img"),
                    help="virtio-blk backing image (default "
                         "build/fs-v1/disk.img; the BIG.BIN cross-cluster "
                         "leg uses build/fs-v1/disk-big.img)")
    ap.add_argument("--fs", action="store_true",
                    help="assert the FS-v1 M2 verdict: the twelfth marker "
                         "K64-FS-OK, the fs counters and the guest file "
                         "buffers compared against the ROM sources/image")
    ap.add_argument("--fsprobe", action="store_true",
                    help="assert the FS-v1 M3 verdict: the thirteenth "
                         "marker K64-FSPROBE-OK, the fsys counters, the "
                         "evidence qwords and the Ring 3 guest data page "
                         "compared against the ROM source/image")
    ap.add_argument("--app", action="store_true",
                    help="assert the APP-v1 verdict: the fourteenth marker "
                         "K64-APP-OK, the app counters, the teardown "
                         "receipts and the game/exit frames (use "
                         "build/fs-v1/disk-app.img; a disk without "
                         "/PROGRAMS/CHIP8.BIN reports K64-APP-SKIP). "
                         "Run tests/stage2-app/check.py first: it builds "
                         "the model probe artifacts ibm-probes.json and "
                         "ibm-expected-frame.bin this leg compares "
                         "against (task 141 S4)")
    ap.add_argument("--app-interactive", action="store_true",
                    help="task 141 W1: implies --app. After the IBM logo "
                         "frame, inject Enter/key7/ESC through the gdbstub "
                         "key buffer and catch the tetris round boundaries "
                         "with a hardware breakpoint at main_loop; asserts "
                         "the two model tetris frames pixel for pixel and "
                         "the full teardown receipts. Run "
                         "tests/stage2-app/check.py first: it builds "
                         "interactive-probes.json and the tetris-int-* "
                         "reference frames this leg compares against")
    ap.add_argument("--menu", action="store_true",
                    help="DESKTOP-v2 T3: after the terminal marker, drive "
                         "the 21-packet host menu script (start button "
                         "open, CHIP-8 entry launch flag, blank close), "
                         "capture screen-menu-open/launch/close.ppm and "
                         "assert the menu frames plus the five private "
                         "desktop labels (clicks 5 = selftest 1 + menu 4, opens 2, launch 1)")
    ap.add_argument("--five", action="store_true",
                    help="DESKTOP-v2 T5 (task 125): implies --app and "
                         "switches the default plain disk to "
                         "build/fs-v1/disk-app.img. After the boot hosted "
                         "run, drive TWO resident click rounds (start "
                         "button -> CHIP-8 entry -> IBM_READY -> EXIT_REQ "
                         "-> DONE), capture screen-five-menu/game/exit/"
                         "game2/exit2.ppm and assert the relaunch (runs 3 "
                         "/ exits 3, pixel-identical exit frames)")
    args = ap.parse_args()
    if args.app_interactive:
        args.app = True
    if args.five:
        args.app = True
        # the five-frame leg needs /PROGRAMS/CHIP8.BIN on the disk; the
        # default plain image would ENOENT every click round
        if args.disk == os.path.join(REPO, "build", "fs-v1", "disk.img"):
            args.disk = os.path.join(REPO, "build", "fs-v1", "disk-app.img")

    if shutil.which(args.qemu) is None and not os.path.exists(args.qemu):
        print(f"run-qemu-debug: {args.qemu} not found", file=sys.stderr)
        sys.exit(1)
    args.disk = os.path.abspath(args.disk)
    if not os.path.isfile(args.disk):
        print(f"run-qemu-debug: {args.disk} missing; run "
              "tools/build-fat16-image.py first (FS-v1 M1/M2)",
              file=sys.stderr)
        sys.exit(1)

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    art = os.path.abspath(args.art_dir or os.path.join(
        REPO, "build", "qemu-debug", f"{stamp}-{os.getpid()}"))
    os.makedirs(art, exist_ok=True)
    # a reused art-dir must not leak a previous session's terminal marker
    # into the smoke poll loop, nor a stale socket into the monitor bind
    for stale in ("serial.log", "qemu-debug.log", "screen.ppm",
                  "screen-echo.ppm", "screen-gui.ppm", "monitor.sock",
                  "blk-evidence.log", "fs-evidence.log",
                  "fsprobe-evidence.log", "screen-game.ppm",
                  "app-evidence.log", "screen-int-20.ppm",
                  "screen-int-71-key.ppm", "screen-menu-open.ppm",
                  "screen-menu-launch.ppm", "screen-menu-close.ppm",
                  "screen-five-menu.ppm", "screen-five-game.ppm",
                  "screen-five-exit.ppm", "screen-five-game2.ppm",
                  "screen-five-exit2.ppm"):
        p = os.path.join(art, stale)
        if os.path.exists(p):
            os.unlink(p)
    print(f"run-qemu-debug: session artifacts: {art}")

    bin_dir = build(art)
    va, off = entry_va(os.path.join(bin_dir, "KERNEL64.BIN"))
    esp = make_esp(art, bin_dir)
    elf = gen_symbols(art, bin_dir)
    counter_addrs, counter_errors = {}, []
    if args.no_freeze:
        extra = INPUT_FORENSIC_SYMBOLS + BLK_COUNTER_SYMBOLS + BLK_DIAG_SYMBOLS
        if args.fs:
            extra += FS_COUNTER_SYMBOLS
        if args.fsprobe:
            extra += FSYS_COUNTER_SYMBOLS
        if args.app:
            extra += APP_COUNTER_SYMBOLS
            if args.app_interactive:
                extra += APP_INT_SYMBOLS
        if args.menu or args.five:
            extra += MENU_COUNTER_SYMBOLS
        if args.gui_skip:
            extra += (("video_fb_state", 64),)
        counter_addrs, counter_errors = counter_addresses(art, extra)
        for e in counter_errors:
            print("run-qemu-debug: WARNING: " + e, file=sys.stderr)

    argv = qemu_argv(args, art, esp)
    print("run-qemu-debug: " + " ".join(argv))

    global _qemu_proc
    atexit.register(cleanup)
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    _qemu_proc = subprocess.Popen(argv, cwd=art, start_new_session=True)
    print(f"run-qemu-debug: QEMU pid {_qemu_proc.pid} "
          f"(own session, port {args.port})")

    if args.no_freeze:
        ok = smoke(args, art, _qemu_proc, counter_addrs, counter_errors)
        sys.exit(0 if ok else 1)

    print_guidance(args, art, va, off, elf)
    try:
        rc = _qemu_proc.wait()
    except KeyboardInterrupt:
        cleanup()
        sys.exit(130)
    print(f"run-qemu-debug: QEMU exited with {rc}")
    sys.exit(rc)


if __name__ == "__main__":
    main()
