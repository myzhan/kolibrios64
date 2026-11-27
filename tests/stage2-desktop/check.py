#!/usr/bin/env python3
# Copyright (C) KolibriOS team 2025-2025. All rights reserved.
# Distributed under terms of the GNU General Public License v2
"""Stage 2 desktop shell checks, task 96, contract DESKTOP-v1. No
machine is started.

Everything here is measured, nothing is taken from a comment. The probe
tests/stage2-desktop/layout.asm assembles kernel/desktop/desktop.asm
exactly as the kernel will include it (behind cpu.asm, mm.asm, irq.asm,
input.asm and video.asm, whose services it consumes) and appends a
manifest of label addresses. This script then

  * compares the manifest constants and every data label offset against
    the fixed values of contract DESKTOP-v1, repeated here independently
    of the assembly sources;
  * decodes the emitted bytes: the bring-up gate, the bounded handshake
    wait, the resident loop with its single STI always preceded by the
    LAPIC LVT timer mask, the cursor save/restore/composite/draw call
    order, the echo state machine with the frozen text area, the six
    self test legs with their deterministic final counts, and the
    216 byte sprite pattern byte for byte plus sha256;
  * proves the desktop module owns EXACTLY ONE STI (the second legal
    STI owner of the kernel, after irq_sti_wait) and never writes the
    LAPIC EOI;
  * checks the data region is real zero bytes, page aligned at both ends;
  * checks the module links at the kernel base with identical bytes
    except the absolute-address immediates, which shift by the base;
  * re-runs the whole pipeline on purpose-broken copies of the sources
    and requires every one of them to fail.

The contract values below are deliberately duplicated instead of
imported: if the module and this file drift apart, one of them has to be
fixed explicitly.

Usage: check.py [--root DIR] [--out DIR] [--fasm2-dir DIR] [--no-fixtures]
"""

import argparse
import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

# ----------------------------------------------------------------------
# contract DESKTOP-v1, fixed here independently of the sources
# ----------------------------------------------------------------------

PAGE = 0x1000
KERNEL_BASE = 0xFFFFFFFF80000000

SCREEN_W = 1024
SCREEN_H = 768

BG_COLOR = 0x1A2B3C
BAR_COLOR = 0x2C3E50
BAR_X = 0
BAR_Y = 0
BAR_W = 1024
BAR_H = 32
TITLE_X = 8
TITLE_Y = 8
TITLE_COLOR = 0xFFFFFF
TITLE_LEN = 16
TITLE_TEXT = b'K64 Desktop v0.1\x00'

SPRITE_W = 12
SPRITE_H = 18
SPRITE_BYTES = SPRITE_W * SPRITE_H
CURSOR_BLACK = 0x000000
CURSOR_WHITE = 0xFFFFFF
# the frozen arrow, an independent copy of the cursor.inc pattern:
# '.' transparent, 'B' black outline, 'W' white fill, row major
SPRITE_ROWS = [
    'BB..........',
    'BWBBBB......',
    '.BWWWWBBBBB.',
    '.BWWWWWWWB..',
    '.BWWWWWWB...',
    '.BWWWWWB....',
    '..BWWWWWB...',
    '..BWWBWWWB..',
    '..BWB.BWWWB.',
    '..BB...BWWWB',
    '..B.....BWWB',
    '.........BB.',
    '............',
    '............',
    '............',
    '............',
    '............',
    '............',
]
SPRITE = bytes({'.': 0, 'B': 1, 'W': 2}[ch]
               for row in SPRITE_ROWS for ch in row)
SPRITE_SHA256 = 'f52a399c4986723b620472433c41bf483e4b44aa15c09080d28e9e172b7caabc'

# the initial cursor position DERIVES from the input self test exit
# coordinates (512+5, 384+3), never magic
MOUSE_XINIT = 512
MOUSE_YINIT = 384
ST_MOUSE_DX = 5
ST_MOUSE_DY = -3                          # PS/2 Y is up positive
CURSOR_X0 = MOUSE_XINIT + ST_MOUSE_DX
CURSOR_Y0 = MOUSE_YINIT - ST_MOUSE_DY
CURSOR_XMAX = SCREEN_W - SPRITE_W
CURSOR_YMAX = SCREEN_H - SPRITE_H

ECHO_X = 16
ECHO_Y = 64
ECHO_COLS = 126
ECHO_ROWS = 44
FONT_W = 8
FONT_H = 16
ECHO_COLOR = 0xFFFFFF
ASCII_FIRST = 0x20
ASCII_LAST = 0x7E
KEY_ENTER = 13
KEY_BACKSPACE = 8

# the frozen desktop injection on top of the input sequence
ST_MOVE_DX = 40
ST_MOVE_DY = 24                           # monitor dy, screen space
ST_CURSOR_X = CURSOR_X0 + ST_MOVE_DX
ST_CURSOR_Y = CURSOR_Y0 + ST_MOVE_DY

STAGE_ECHO_H = 1
STAGE_ECHO_I = 2
STAGE_MOUSE_MOVE = 3
STAGE_MOUSE_BUTTON = 4
STAGE_DONE = 5

# frozen final counts, DETERMINISTICALLY derived from the WHOLE
# injection chain: kbd 2+4 (make+break of h and i), mouse 3+3 (move +
# press + release, release gated on the packet count), keys 2 ('h','i'),
# ticks stays 11 (the loop masks the LVT timer BEFORE its STI)
INPUT_KBD_BASE = 2
INPUT_MOUSE_BASE = 3
ST_KBD_FINAL = INPUT_KBD_BASE + 4
ST_MOUSE_FINAL = INPUT_MOUSE_BASE + 3
ST_KEYS_FINAL = 2
ST_KBD_USED_FINAL = INPUT_KBD_BASE + 4
TICKS_FINAL = 11
ST_ALL = 0x3F

POLL_CHUNK = 0x2000000
POLL_ROUNDS = 64

LAPIC_VA = 0xFFFFC00002000000
LAPIC_EOI = 0x0B0
LVT_TIMER = 0x320
LVT_MASK = 0x10000
VEC_KEYBOARD = 0x21
VEC_MOUSE = 0x2C
IDX_KBD_LO = 0x10 + 2 * 1
IDX_MOUSE_LO = 0x10 + 2 * 12
RTE_KBD_MASKED = LVT_MASK | VEC_KEYBOARD
RTE_MOUSE_MASKED = LVT_MASK | VEC_MOUSE
RTE_KBD_LIVE = VEC_KEYBOARD
RTE_MOUSE_LIVE = VEC_MOUSE

# the video_fb_state dword offsets the bring-up gate reads
FB_OFF_READY = 0
FB_OFF_WIDTH = 8
FB_OFF_HEIGHT = 12

ENOTSUP = -6
EIO = -9

# the data layout, offsets from desktop_data_start
OFF_STATE = 0
OFF_TEST_MASK = 8
OFF_TEST_STAGE = 16
OFF_TEST_KEYS = 24
OFF_TEST_CURSOR_X = 32
OFF_TEST_CURSOR_Y = 40
OFF_CURSOR_X = 48
OFF_CURSOR_Y = 56
OFF_ECHO_COL = 64
OFF_ECHO_ROW = 72
OFF_ECHO_DROPS = 80
OFF_KBD_CONSUMED = 88
OFF_ECHO_CHAR = 96
OFF_GUI_FRAME = 104
OFF_CURSOR_SAVE = 144
OFF_CURSOR_DRAW = OFF_CURSOR_SAVE + SPRITE_BYTES * 4
OFF_MESSAGES = OFF_CURSOR_DRAW + SPRITE_BYTES * 4

MSG_SELFTEST = b'K64-DESKTOP-SELFTEST failed legs = \x00'
MSG_NL = b'\r\n\x00'

# ----------------------------------------------------------------------
# contract DESKTOP-v2, task 124: the wallpaper background, the taskbar
# start button and the single entry start menu, duplicated here
# independently of the module sources as well
# ----------------------------------------------------------------------

START_X = 0
START_Y = 0
START_W = 64
START_H = 32
MENU_X = 0
MENU_Y = BAR_H                          # the menu hangs under the bar
MENU_W = 200
MENU_H = 34
MENU_COLOR = 0x2C3E50
ACCENT_COLOR = 0x9594A2                 # the gray band of the wallpaper
MENU_TEXT = b'CHIP-8\x00'
MENU_TEXT_LEN = 6
MENU_TEXT_X = 76
MENU_TEXT_Y = 41
# the entry centre DERIVES from the label placement, never magic
ENTRY_CX = MENU_TEXT_X + MENU_TEXT_LEN * FONT_W // 2
ENTRY_CY = MENU_TEXT_Y + FONT_H // 2
MENU_SAVE_BYTES = MENU_W * MENU_H * 4

# the v2 qword block follows the v1 messages plus one alignment byte
OFF_PREV_BUTTONS = OFF_MESSAGES + len(MSG_SELFTEST) + len(MSG_NL) + 1
OFF_MENU_FLAG = OFF_PREV_BUTTONS + 8
OFF_APP_LAUNCH = OFF_MENU_FLAG + 8
OFF_TEST_CLICKS = OFF_APP_LAUNCH + 8
OFF_TEST_MENU_OPENS = OFF_TEST_CLICKS + 8
OFF_MENU_SAVE = PAGE                    # the menu save owns data page 2
DATA_PAGES = 8

# the self test leg 4 press/release at (557,411) is exactly ONE click
# outside the menu; no menu may have opened during the frozen legs
ST_CLICKS_FINAL = 1
ST_MENU_OPENS_FINAL = 0

WALLPAPER_W = 1024
WALLPAPER_H = 768
WALLPAPER_BYTES = WALLPAPER_W * WALLPAPER_H * 4
WALLPAPER_SHA256 = ('f9f8a12e5406e04b2007028bd168f72e'
                    '9c25386d5bf92ef5ecb1aaeb18f36fec')
WALLPAPER_SHA_QWORDS = struct.unpack('>4Q', bytes.fromhex(WALLPAPER_SHA256))

# ----------------------------------------------------------------------
# contract task 125, DESKTOP-v2 T4/T5: the app launch wiring. The
# click stages extend the frozen handshake 0..5; the boot self test
# never publishes them (it never clicks the start button)
# ----------------------------------------------------------------------

STAGE_START_CLICK = STAGE_DONE + 1
STAGE_ENTRY_CLICK = STAGE_START_CLICK + 1
STAGE_APP_RUNNING = STAGE_ENTRY_CLICK + 1
STAGE_APP_EXIT = STAGE_APP_RUNNING + 1

# the taskbar running indicator, frozen geometry right of the title
RUN_X = 992
RUN_Y = 8
RUN_W = 24
RUN_H = 16
RUN_COLOR = ACCENT_COLOR

# the task 125 evidence qwords extend the v2 block inside data page 1
OFF_TEST_START_CLICKS = OFF_TEST_MENU_OPENS + 8
OFF_TEST_ENTRY_CLICKS = OFF_TEST_START_CLICKS + 8
OFF_TEST_APP_RUNS = OFF_TEST_ENTRY_CLICKS + 8
OFF_TEST_APP_EXITS = OFF_TEST_APP_RUNS + 8
# the T1 probe truth sampled from the ASSET (not a rendered frame):
# 7 deep blue-gray field points and 4 gray band points
WALLPAPER_PROBES = [
    (900, 700, 0x182028), (16, 767, 0x182028), (500, 32, 0x182028),
    (0, 0, 0x182028), (517, 387, 0x182028), (519, 392, 0x182028),
    (16, 64, 0x182028),
    (557, 411, 0x9594A2), (568, 411, 0x9594A2),
    (559, 416, 0x9594A2), (562, 416, 0x9594A2),
]

MAGIC = b'K64DTP1'
MANIFEST_VERSION = 1

# manifest entry order, see tests/stage2-desktop/layout.asm
M = [
    'magic', 'version',
    'code_start', 'code_end', 'data_start', 'data_end',
    'init', 'wait_count', 'pump', 'run',
    'cursor_place', 'cursor_composite', 'cursor_sync',
    'echo_pump', 'echo_key', 'selftest',
    'title', 'title_end', 'sprite', 'sprite_end',
    'state', 'test_mask', 'test_stage', 'test_keys',
    'test_cursor_x', 'test_cursor_y', 'cursor_x', 'cursor_y',
    'echo_col', 'echo_row', 'echo_drops', 'kbd_consumed', 'echo_char',
    'gui_frame',
    'cursor_save', 'cursor_draw', 'msg_selftest', 'msg_nl',
    'video_fill_rect', 'video_blit_rect', 'video_readback_rect',
    'video_puts', 'video_fb_state',
    'input_ioapic_write', 'input_kbd_buf', 'input_kbd_used',
    'input_kbd_drops', 'input_mouse_x', 'input_mouse_y',
    'input_test_kbd_count', 'input_test_mouse_count',
    'irq_sti_wait', 'irq_test_ticks', 'cpu_com1_puts', 'cpu_com1_puthex',
    'bg_color', 'bar_color', 'bar_x', 'bar_y', 'bar_w', 'bar_h',
    'title_x', 'title_y', 'title_color', 'title_len',
    'sprite_w', 'sprite_h', 'sprite_bytes',
    'cursor_x0', 'cursor_y0', 'cursor_xmax', 'cursor_ymax',
    'echo_x', 'echo_y', 'echo_cols', 'echo_rows',
    'font_w', 'font_h', 'echo_color',
    'key_enter', 'key_backspace', 'ascii_first', 'ascii_last',
    'st_move_dx', 'st_move_dy', 'st_cursor_x', 'st_cursor_y',
    'st_kbd_final', 'st_mouse_final', 'st_keys_final', 'st_kbd_used_final',
    'ticks_final', 'stage_echo_h', 'stage_echo_i', 'stage_move',
    'stage_button', 'stage_done', 'st_all',
    'poll_chunk', 'poll_rounds',
    'lapic_va', 'lvt_timer', 'lvt_mask', 'idx_kbd_lo', 'idx_mouse_lo',
    'rte_kbd_masked', 'rte_mouse_masked', 'rte_kbd_live', 'rte_mouse_live',
    'off_test_mask', 'off_test_stage', 'off_test_keys',
    'off_test_cursor_x', 'off_test_cursor_y',
    'off_cursor_save', 'off_cursor_draw', 'off_messages',
    'screen_w', 'screen_h',
    # DESKTOP-v2 tail appendix, task 124, same order as layout.asm
    'click_pump', 'menu_open', 'menu_close', 'menu_text', 'menu_text_end',
    'wallpaper', 'wallpaper_end',
    'prev_buttons', 'menu_flag', 'app_launch',
    'test_clicks', 'test_menu_opens', 'menu_save',
    'input_mouse_b1',
    'start_x', 'start_y', 'start_w', 'start_h',
    'menu_x', 'menu_y', 'menu_w', 'menu_h',
    'entry_cx', 'entry_cy', 'menu_color', 'accent_color',
    'menu_text_x', 'menu_text_y', 'menu_text_len',
    'off_prev_buttons', 'off_menu_flag', 'off_app_launch',
    'off_test_clicks', 'off_test_menu_opens', 'off_menu_save',
    'menu_save_bytes',
    'wallpaper_w', 'wallpaper_h', 'wallpaper_bytes',
    'wallpaper_sha0', 'wallpaper_sha1', 'wallpaper_sha2', 'wallpaper_sha3',
    # task 125 tail appendix, same order as layout.asm
    'repaint_base', 'launch_pump', 'app_boot', 'app_launch_fn',
    'test_start_clicks', 'test_entry_clicks', 'test_app_runs',
    'test_app_exits',
    'stage_start_click', 'stage_entry_click', 'stage_app_running',
    'stage_app_exit',
    'run_x', 'run_y', 'run_w', 'run_h', 'run_color',
    'off_test_start_clicks', 'off_test_entry_clicks',
    'off_test_app_runs', 'off_test_app_exits',
]

# label name -> frozen offset inside the data region
DATA_LABELS = {
    'state': OFF_STATE, 'test_mask': OFF_TEST_MASK,
    'test_stage': OFF_TEST_STAGE, 'test_keys': OFF_TEST_KEYS,
    'test_cursor_x': OFF_TEST_CURSOR_X, 'test_cursor_y': OFF_TEST_CURSOR_Y,
    'cursor_x': OFF_CURSOR_X, 'cursor_y': OFF_CURSOR_Y,
    'echo_col': OFF_ECHO_COL, 'echo_row': OFF_ECHO_ROW,
    'echo_drops': OFF_ECHO_DROPS, 'kbd_consumed': OFF_KBD_CONSUMED,
    'echo_char': OFF_ECHO_CHAR,
    'gui_frame': OFF_GUI_FRAME,
    'cursor_save': OFF_CURSOR_SAVE, 'cursor_draw': OFF_CURSOR_DRAW,
    'msg_selftest': OFF_MESSAGES,
    'prev_buttons': OFF_PREV_BUTTONS, 'menu_flag': OFF_MENU_FLAG,
    'app_launch': OFF_APP_LAUNCH,
    'test_clicks': OFF_TEST_CLICKS, 'test_menu_opens': OFF_TEST_MENU_OPENS,
    'test_start_clicks': OFF_TEST_START_CLICKS,
    'test_entry_clicks': OFF_TEST_ENTRY_CLICKS,
    'test_app_runs': OFF_TEST_APP_RUNS, 'test_app_exits': OFF_TEST_APP_EXITS,
    'menu_save': OFF_MENU_SAVE,
}

# manifest constants that must not drift from the copies above
CONSTANTS = {
    'bg_color': BG_COLOR, 'bar_color': BAR_COLOR,
    'bar_x': BAR_X, 'bar_y': BAR_Y, 'bar_w': BAR_W, 'bar_h': BAR_H,
    'title_x': TITLE_X, 'title_y': TITLE_Y,
    'title_color': TITLE_COLOR, 'title_len': TITLE_LEN,
    'sprite_w': SPRITE_W, 'sprite_h': SPRITE_H, 'sprite_bytes': SPRITE_BYTES,
    'cursor_x0': CURSOR_X0, 'cursor_y0': CURSOR_Y0,
    'cursor_xmax': CURSOR_XMAX, 'cursor_ymax': CURSOR_YMAX,
    'echo_x': ECHO_X, 'echo_y': ECHO_Y,
    'echo_cols': ECHO_COLS, 'echo_rows': ECHO_ROWS,
    'font_w': FONT_W, 'font_h': FONT_H, 'echo_color': ECHO_COLOR,
    'key_enter': KEY_ENTER, 'key_backspace': KEY_BACKSPACE,
    'ascii_first': ASCII_FIRST, 'ascii_last': ASCII_LAST,
    'st_move_dx': ST_MOVE_DX, 'st_move_dy': ST_MOVE_DY,
    'st_cursor_x': ST_CURSOR_X, 'st_cursor_y': ST_CURSOR_Y,
    'st_kbd_final': ST_KBD_FINAL, 'st_mouse_final': ST_MOUSE_FINAL,
    'st_keys_final': ST_KEYS_FINAL, 'st_kbd_used_final': ST_KBD_USED_FINAL,
    'ticks_final': TICKS_FINAL,
    'stage_echo_h': STAGE_ECHO_H, 'stage_echo_i': STAGE_ECHO_I,
    'stage_move': STAGE_MOUSE_MOVE, 'stage_button': STAGE_MOUSE_BUTTON,
    'stage_done': STAGE_DONE, 'st_all': ST_ALL,
    'poll_chunk': POLL_CHUNK, 'poll_rounds': POLL_ROUNDS,
    'lapic_va': LAPIC_VA, 'lvt_timer': LVT_TIMER, 'lvt_mask': LVT_MASK,
    'idx_kbd_lo': IDX_KBD_LO, 'idx_mouse_lo': IDX_MOUSE_LO,
    'rte_kbd_masked': RTE_KBD_MASKED, 'rte_mouse_masked': RTE_MOUSE_MASKED,
    'rte_kbd_live': RTE_KBD_LIVE, 'rte_mouse_live': RTE_MOUSE_LIVE,
    'off_test_mask': OFF_TEST_MASK, 'off_test_stage': OFF_TEST_STAGE,
    'off_test_keys': OFF_TEST_KEYS,
    'off_test_cursor_x': OFF_TEST_CURSOR_X,
    'off_test_cursor_y': OFF_TEST_CURSOR_Y,
    'off_cursor_save': OFF_CURSOR_SAVE, 'off_cursor_draw': OFF_CURSOR_DRAW,
    'off_messages': OFF_MESSAGES,
    'screen_w': SCREEN_W, 'screen_h': SCREEN_H,
    'start_x': START_X, 'start_y': START_Y,
    'start_w': START_W, 'start_h': START_H,
    'menu_x': MENU_X, 'menu_y': MENU_Y, 'menu_w': MENU_W, 'menu_h': MENU_H,
    'entry_cx': ENTRY_CX, 'entry_cy': ENTRY_CY,
    'menu_color': MENU_COLOR, 'accent_color': ACCENT_COLOR,
    'menu_text_x': MENU_TEXT_X, 'menu_text_y': MENU_TEXT_Y,
    'menu_text_len': MENU_TEXT_LEN,
    'off_prev_buttons': OFF_PREV_BUTTONS, 'off_menu_flag': OFF_MENU_FLAG,
    'off_app_launch': OFF_APP_LAUNCH,
    'off_test_clicks': OFF_TEST_CLICKS,
    'off_test_menu_opens': OFF_TEST_MENU_OPENS,
    'off_menu_save': OFF_MENU_SAVE,
    'menu_save_bytes': MENU_SAVE_BYTES,
    'wallpaper_w': WALLPAPER_W, 'wallpaper_h': WALLPAPER_H,
    'wallpaper_bytes': WALLPAPER_BYTES,
    'wallpaper_sha0': WALLPAPER_SHA_QWORDS[0],
    'wallpaper_sha1': WALLPAPER_SHA_QWORDS[1],
    'wallpaper_sha2': WALLPAPER_SHA_QWORDS[2],
    'wallpaper_sha3': WALLPAPER_SHA_QWORDS[3],
    'stage_start_click': STAGE_START_CLICK,
    'stage_entry_click': STAGE_ENTRY_CLICK,
    'stage_app_running': STAGE_APP_RUNNING,
    'stage_app_exit': STAGE_APP_EXIT,
    'run_x': RUN_X, 'run_y': RUN_Y, 'run_w': RUN_W, 'run_h': RUN_H,
    'run_color': RUN_COLOR,
    'off_test_start_clicks': OFF_TEST_START_CLICKS,
    'off_test_entry_clicks': OFF_TEST_ENTRY_CLICKS,
    'off_test_app_runs': OFF_TEST_APP_RUNS,
    'off_test_app_exits': OFF_TEST_APP_EXITS,
}

FRAME_ENTER = bytes.fromhex('55 53 56 57 41 54 41 55 41 56 41 57 48 83 EC 28')
FRAME_LEAVE = bytes.fromhex('48 83 C4 28 41 5F 41 5E 41 5D 41 5C 5F 5E 5B 5D')

# the functions the module may call; used to tell a real STI opcode
# apart from an FB byte inside a call rel32 operand
CALL_TARGETS = [
    'init', 'wait_count', 'pump', 'run', 'cursor_place',
    'cursor_composite', 'cursor_sync', 'echo_pump', 'echo_key', 'selftest',
    'click_pump', 'menu_open', 'menu_close',
    'repaint_base', 'launch_pump', 'app_boot', 'app_launch_fn',
    'video_fill_rect', 'video_blit_rect', 'video_readback_rect',
    'video_puts', 'input_ioapic_write', 'irq_sti_wait',
    'cpu_com1_puts', 'cpu_com1_puthex',
]

# ----------------------------------------------------------------------
# tiny binary helpers
# ----------------------------------------------------------------------


def u32(data, off):
    return struct.unpack_from('<I', data, off)[0]


def u64(data, off):
    return struct.unpack_from('<Q', data, off)[0]


def find_all(data, needle):
    found, start = [], 0
    while True:
        at = data.find(needle, start)
        if at < 0:
            return found
        found.append(at)
        start = at + 1


def mov_ecx(val):
    return b'\xB9' + struct.pack('<I', val)


def mov_edx(val):
    return b'\xBA' + struct.pack('<I', val)


def stack_color(val):
    """mov dword [rsp+32], imm32: the frozen fifth argument."""
    return b'\xC7\x44\x24\x20' + struct.pack('<I', val)


def lvt_mask_rax():
    return b'\xC7\x80' + struct.pack('<i', LVT_TIMER) + struct.pack('<I', LVT_MASK)


def lvt_mask_r13():
    return b'\x41\xC7\x85' + struct.pack('<i', LVT_TIMER) + struct.pack('<I', LVT_MASK)


def call_sites(img, at, end, opcode=b'\xE8'):
    """Every E8 rel32 in a region: (site, target) file offsets."""
    body = img.data[at:end]
    out = []
    for p in find_all(body, opcode):
        if p + 5 <= len(body):
            rel = struct.unpack_from('<i', body, p + 1)[0]
            target = at + p + 5 + rel
            if 0 <= target < len(img.data):
                out.append((at + p, target))
    return out


def calls_to(img, at, end, label):
    return [s for s, t in call_sites(img, at, end) if t == img.at(label)]


def rip_refs(img, at, end, prefix, modrm, tail, label):
    """Sites of 'prefix modrm disp32 [tail bytes]' that address label."""
    body = img.data[at:end]
    want = img.at(label)
    out = []
    for p in find_all(body, prefix):
        m = p + len(prefix)
        if m + 5 > len(body) or body[m] != modrm:
            continue
        disp = struct.unpack_from('<i', body, m + 1)[0]
        if at + m + 5 + tail + disp == want:
            out.append(at + p)
    return out


def rip_dword_cmps(img, at, end, want):
    """(site, imm) of every 'cmp dword [rip+disp], imm' addressing want,
    both the imm8 and the imm32 encoding."""
    body = img.data[at:end]
    out = []
    for p in find_all(body, b'\x83\x3D'):
        if p + 7 <= len(body):
            disp = struct.unpack_from('<i', body, p + 2)[0]
            if at + p + 7 + disp == want:
                out.append((at + p, struct.unpack_from('<b', body, p + 6)[0]))
    for p in find_all(body, b'\x81\x3D'):
        if p + 10 <= len(body):
            disp = struct.unpack_from('<i', body, p + 2)[0]
            if at + p + 10 + disp == want:
                out.append((at + p, u32(body, p + 6)))
    return out


def ordered_positions(img, at, end, needles, start=None):
    """Find each needle strictly after the previous one; None if absent."""
    body = img.data[at:end]
    out, cur = [], (start - at) if start is not None else 0
    for needle in needles:
        p = body.find(needle, cur)
        if p < 0:
            return None
        out.append(at + p)
        cur = p + len(needle)
    return out


def jcc_after(img, pos, limit=8):
    body = img.data
    for j in range(pos, min(pos + limit, len(body) - 2)):
        if body[j] == 0x0F and 0x80 <= body[j + 1] <= 0x8F:
            rel = struct.unpack_from('<i', body, j + 2)[0]
            return j, j + 6 + rel
        if 0x70 <= body[j] <= 0x7F:
            rel = struct.unpack_from('<b', body, j + 1)[0]
            return j, j + 2 + rel
    return None


def cmp_qword_sites(img, at, end):
    """Every 'cmp qword [rip + disp32], imm': (site, target, immediate),
    both the imm8 and the imm32 encoding."""
    body = img.data[at:end]
    out = []
    for p in find_all(body, b'\x48\x83\x3D'):
        if p + 8 <= len(body):
            disp = struct.unpack_from('<i', body, p + 3)[0]
            imm = struct.unpack_from('<b', body, p + 7)[0]
            out.append((at + p, at + p + 8 + disp, imm))
    for p in find_all(body, b'\x48\x81\x3D'):
        if p + 11 <= len(body):
            disp = struct.unpack_from('<i', body, p + 3)[0]
            imm = struct.unpack_from('<i', body, p + 7)[0]
            out.append((at + p, at + p + 11 + disp, imm))
    return out


def operand_cover(img, at, end):
    """Byte positions inside instruction OPERANDS (call rel32 targeting
    a known function, rip-relative disp32, imm32/imm8 immediates of the
    REX.W forms the module emits). An FB/F4/FA byte inside an operand
    is data, never an STI/HLT/CLI instruction."""
    body = img.data[at:end]
    covered = set()
    targets = {img.at(n) for n in CALL_TARGETS}
    for s, t in call_sites(img, at, end):
        if t in targets:
            covered.update(range(s + 1, s + 5))
    rex = {0x48, 0x49, 0x4A, 0x4B, 0x4C, 0x4D, 0x4E, 0x4F}
    rip_ops = {0x8B, 0x89, 0x8D, 0x3B, 0x39, 0x01, 0x29, 0x03, 0x2B,
               0x33, 0x31, 0x09, 0x0B, 0xFF}
    for p in range(len(body) - 7):
        if body[p] not in rex:
            continue
        op, modrm = body[p + 1], body[p + 2]
        q = at + p
        if (modrm & 0xC7) == 0x05 and (op in rip_ops or op in (0x81, 0x83, 0xC7)):
            covered.update(range(q + 3, q + 7))     # the disp32
            if op == 0x83:
                covered.add(q + 7)                  # imm8
            elif op in (0x81, 0xC7):
                covered.update(range(q + 7, q + 11))  # imm32
        elif (modrm & 0xC0) == 0xC0:
            if op == 0xC7:
                covered.update(range(q + 3, q + 7))   # mov r64, imm32
            elif op == 0x81:
                covered.update(range(q + 3, q + 7))   # cmp r64, imm32
            elif op == 0x83:
                covered.add(q + 3)                    # cmp r64, imm8
    return covered


def real_sti_sites(img):
    """FB bytes in the code region that are NOT instruction operand
    bytes: the real STI instructions. The embedded wallpaper is DATA
    inside the code region: its 3 MiB of pixels carry thousands of
    stray FB bytes, so the wallpaper extent is excluded entirely."""
    at, end = img.at('code_start'), img.at('code_end')
    wp, we = img.at('wallpaper'), img.at('wallpaper_end')
    covered = operand_cover(img, at, wp)
    return [at + p for p in find_all(img.data[at:wp], b'\xFB')
            if at + p not in covered]


# ----------------------------------------------------------------------
# report
# ----------------------------------------------------------------------


class Report:
    def __init__(self):
        self.rows = []
        self.failures = []

    def check(self, name, ok, detail=''):
        ok = bool(ok)
        self.rows.append((name, ok, detail))
        if not ok:
            self.failures.append((name, detail))
        return ok

    def equal(self, name, got, want):
        try:
            detail = 'got %#x, want %#x' % (got, want)
        except TypeError:
            detail = 'got %r, want %r' % (got, want)
        return self.check(name, got == want, detail)

    def print(self, verbose):
        for name, ok, detail in self.rows:
            if ok and not verbose:
                continue
            line = '%-4s %s' % ('ok' if ok else 'FAIL', name)
            if detail and (not ok or verbose):
                line += ': ' + detail
            print(line)
        total = len(self.rows)
        print('stage2-desktop: %d checks, %d failed' % (total, len(self.failures)))
        return not self.failures


# ----------------------------------------------------------------------
# assembler plumbing
# ----------------------------------------------------------------------


class Assembler:
    def __init__(self, fasmg, fasm2_dir):
        self.fasmg = Path(fasmg)
        self.fasm2_dir = Path(fasm2_dir)

    def run(self, source, output, org=None):
        env = dict(os.environ)
        env['INCLUDE'] = str(self.fasm2_dir / 'include')
        cmd = [str(self.fasmg), '-e100', '-i', "Include 'fasm2.inc'"]
        if org is not None:
            cmd += ['-i', 'DESKTOP_PROBE_ORG=%#x' % org]
        cmd += [str(source), str(output)]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
        text = (proc.stdout or '') + (proc.stderr or '')
        return proc.returncode == 0, text


def find_assembler(root, explicit_fasm2):
    fasm2_dir = Path(explicit_fasm2) if explicit_fasm2 else (root / '..' / 'fasm2').resolve()
    candidates = []
    if os.environ.get('FASMG'):
        candidates.append(Path(os.environ['FASMG']))
    candidates += [
        fasm2_dir / 'build' / 'fasm2-bootstrap' / 'fasmg-macos-x64',
        fasm2_dir / 'build' / 'fasm2-bootstrap' / 'fasmg-macos-x64.gen3',
    ]
    for host in candidates:
        if host.is_file() and os.access(host, os.X_OK):
            if not (fasm2_dir / 'include' / 'fasm2.inc').is_file():
                continue
            return Assembler(host, fasm2_dir)
    return None


# ----------------------------------------------------------------------
# manifest and image
# ----------------------------------------------------------------------


class Image:
    def __init__(self, data, probe_at, base=0):
        self.data = data
        self.base = base
        self.probe_at = probe_at
        values = struct.unpack_from('<%dQ' % len(M), data, probe_at)
        self.raw = dict(zip(M, values))

    def __getitem__(self, name):
        return self.raw[name]

    def at(self, name):
        return self[name] - self.base


def load_image(path, base=0):
    data = Path(path).read_bytes()
    at = max(find_all(data, MAGIC), default=-1)
    assert at >= 0, 'manifest magic not found'
    assert at + len(M) * 8 <= len(data), 'manifest truncated'
    return Image(data, at, base)


def region(img, start, stop):
    return img.at(start), img.at(stop)


# ----------------------------------------------------------------------
# checks
# ----------------------------------------------------------------------


def check_manifest(rep, img):
    rep.check('manifest.magic', True, MAGIC.decode())
    rep.equal('manifest.version', img['version'], MANIFEST_VERSION)
    rep.equal('manifest.entry_count', len(M), 183)
    for name, want in CONSTANTS.items():
        rep.equal('manifest.%s' % name, img[name], want)
    # the frozen derivations, pinned here as well
    rep.equal('contract.cursor_x0_derived', CURSOR_X0, 517)
    rep.equal('contract.cursor_y0_derived', CURSOR_Y0, 387)
    rep.equal('contract.st_cursor_derived', (ST_CURSOR_X, ST_CURSOR_Y), (557, 411))
    rep.equal('contract.echo_tiles_width', ECHO_X + ECHO_COLS * FONT_W, SCREEN_W)
    rep.equal('contract.echo_tiles_height', ECHO_Y + ECHO_ROWS * FONT_H, SCREEN_H)
    rep.equal('contract.kbd_final_derived', ST_KBD_FINAL, INPUT_KBD_BASE + 4)
    rep.equal('contract.mouse_final_derived', ST_MOUSE_FINAL, INPUT_MOUSE_BASE + 3)
    # the v2 derivations, pinned here as well
    rep.equal('contract.entry_centre_derived', (ENTRY_CX, ENTRY_CY), (100, 49))
    rep.equal('contract.menu_hangs_under_bar', MENU_Y, BAR_H)
    rep.equal('contract.prev_buttons_derived', OFF_PREV_BUTTONS, 1912)
    rep.equal('contract.menu_save_bytes_derived', MENU_SAVE_BYTES, 27200)
    rep.equal('contract.wallpaper_bytes_derived', WALLPAPER_BYTES, 3145728)
    # the task 125 derivations, pinned here as well
    rep.equal('contract.click_stages_derived',
              (STAGE_START_CLICK, STAGE_ENTRY_CLICK,
               STAGE_APP_RUNNING, STAGE_APP_EXIT), (6, 7, 8, 9))
    rep.equal('contract.start_clicks_derived', OFF_TEST_START_CLICKS, 1952)
    rep.equal('contract.app_exits_derived', OFF_TEST_APP_EXITS, 1976)
    rep.check('contract.evidence_inside_page1', OFF_TEST_APP_EXITS + 8 <= PAGE)
    rep.check('contract.run_indicator_fits_bar',
              RUN_X + RUN_W <= BAR_W and RUN_Y + RUN_H <= BAR_H and
              RUN_X > START_W)

    for name, off in DATA_LABELS.items():
        rep.equal('layout.%s_offset' % name,
                  img[name] - img['data_start'], off)

    lo, hi = img['code_start'], img['code_end']
    rep.check('layout.code_non_empty', hi > lo, '%#x..%#x' % (lo, hi))
    rep.equal('layout.api_emits_no_code', img['init'], lo)
    order = ['init', 'wait_count', 'pump', 'run', 'cursor_place',
             'cursor_composite', 'cursor_sync', 'echo_pump', 'echo_key',
             'click_pump', 'menu_open', 'menu_close',
             'repaint_base', 'launch_pump', 'app_boot', 'selftest']
    chain = [img[name] for name in order]
    rep.check('layout.function_order', chain == sorted(chain) and
              all(lo <= v < hi for v in chain),
              'emission order follows the include order')
    rep.check('layout.constants_inside_code',
              lo <= img['sprite'] < img['sprite_end'] <= img['echo_pump'] <
              img['echo_key'] < img['title'] < img['title_end'] <=
              img['click_pump'] < img['menu_open'] < img['menu_close'] <
              img['menu_text'] < img['menu_text_end'] <=
              img['repaint_base'] < img['launch_pump'] < img['app_boot'] <=
              img['selftest'] <
              img['wallpaper'] < img['wallpaper_end'] <= hi)
    rep.equal('layout.title_bytes', img['title_end'] - img['title'], TITLE_LEN + 1)
    rep.equal('layout.sprite_bytes', img['sprite_end'] - img['sprite'], SPRITE_BYTES)
    rep.equal('layout.menu_text_bytes',
              img['menu_text_end'] - img['menu_text'], MENU_TEXT_LEN + 1)
    rep.equal('layout.selftest_after_taskbar',
              img['app_boot'] < img['selftest'] and
              img['menu_text_end'] <= img['repaint_base'], True)
    rep.equal('layout.wallpaper_bytes',
              img['wallpaper_end'] - img['wallpaper'], WALLPAPER_BYTES)
    rep.check('layout.code_before_data', hi <= img['data_start'])
    rep.check('layout.data_page_aligned',
              img['data_start'] % PAGE == 0 and img['data_end'] % PAGE == 0)
    rep.check('layout.data_size_page_multiple',
              (img['data_end'] - img['data_start']) % PAGE == 0 and
              img['data_end'] - img['data_start'] >= PAGE)
    rep.equal('layout.module_ends_with_data', img.at('data_end'), img.probe_at)
    rep.equal('layout.data_region_eight_pages',
              img['data_end'] - img['data_start'], DATA_PAGES * PAGE)
    for name in ('video_fill_rect', 'video_blit_rect', 'video_readback_rect',
                 'video_puts', 'video_fb_state', 'input_ioapic_write',
                 'input_kbd_buf', 'input_kbd_used', 'input_kbd_drops',
                 'input_mouse_x', 'input_mouse_y', 'input_mouse_b1',
                 'input_test_kbd_count',
                 'input_test_mouse_count', 'irq_sti_wait', 'irq_test_ticks',
                 'cpu_com1_puts', 'cpu_com1_puthex', 'app_launch_fn'):
        rep.check('layout.%s_before_module' % name, 0 < img[name] < lo,
                  '%#x' % img[name])


def check_data(rep, img):
    d = img.data
    start, end = img.at('data_start'), img.at('data_end')
    block = d[start:start + OFF_MESSAGES]
    rep.check('data.qword_block_all_zero', block == bytes(OFF_MESSAGES),
              '%d bytes of counters and save/draw buffers must be real zeros'
              % OFF_MESSAGES)
    rep.equal('data.save_buffer_size', OFF_CURSOR_DRAW - OFF_CURSOR_SAVE,
              SPRITE_BYTES * 4)
    rep.equal('data.draw_buffer_size', OFF_MESSAGES - OFF_CURSOR_DRAW,
              SPRITE_BYTES * 4)
    rep.check('data.msg_selftest_bytes',
              d[img.at('msg_selftest'):img.at('msg_selftest') + len(MSG_SELFTEST)]
              == MSG_SELFTEST)
    rep.equal('data.msg_nl_offset', img['msg_nl'] - img['data_start'],
              OFF_MESSAGES + len(MSG_SELFTEST))
    rep.check('data.msg_nl_bytes',
              d[img.at('msg_nl'):img.at('msg_nl') + len(MSG_NL)] == MSG_NL)
    tail_from = img.at('msg_nl') + len(MSG_NL)
    rest = d[tail_from:end]
    rep.check('data.tail_zero_after_messages', rest.strip(b'\x00') == b'',
              '%d non-zero tail bytes' % len(rest.strip(b'\x00')))
    rep.check('data.messages_ascii',
              all(0x20 <= ch < 0x80 for ch in MSG_SELFTEST[:-1]))
    # the v2 qword block starts on the 8 byte boundary after the one
    # alignment pad; the whole tail (state + page 2 save) stays zero
    rep.equal('data.v2_alignment_pad',
              d[img.at('msg_nl') + len(MSG_NL)], 0)
    rep.equal('data.menu_save_size',
              img.at('data_end') - img.at('menu_save') -
              (DATA_PAGES * PAGE - OFF_MENU_SAVE - MENU_SAVE_BYTES),
              MENU_SAVE_BYTES)
    rep.check('data.menu_save_inside_region',
              img.at('menu_save') + MENU_SAVE_BYTES <= img.at('data_end'))


def check_sprite(rep, img):
    d = img.data
    at, end = img.at('sprite'), img.at('sprite_end')
    got = d[at:end]
    rep.equal('sprite.length', len(got), SPRITE_BYTES)
    rep.check('sprite.bytes_frozen', got == SPRITE,
              'the 12x18 arrow pattern, byte for byte')
    rep.equal('sprite.sha256', hashlib.sha256(got).hexdigest(), SPRITE_SHA256)
    rep.check('sprite.codes_valid', set(got) <= {0, 1, 2})
    t = d[img.at('title'):img.at('title_end')]
    rep.check('sprite.title_text_frozen', t == TITLE_TEXT)


def check_ownership(rep, img, root):
    """Exactly one real STI, in desktop_run, never an EOI write."""
    d = img.data
    # the instruction scan stops at the wallpaper: pixel bytes are not
    # code and may spell any opcode by accident
    at, end = region(img, 'code_start', 'wallpaper')
    stis = real_sti_sites(img)
    rep.equal('ownership.single_real_sti', len(stis), 1)
    ra, re_ = region(img, 'run', 'cursor_place')
    if stis:
        rep.check('ownership.sti_inside_run', ra <= stis[0] < re_,
                  '%#x' % stis[0])
        rep.check('ownership.sti_hlt_cli_sequence',
                  d[stis[0]:stis[0] + 3] == b'\xFB\xF4\xFA')
    rep.check('ownership.no_eoi_write_rax',
              d.find(b'\xC7\x80' + struct.pack('<i', LAPIC_EOI), at, end) < 0)
    rep.check('ownership.no_eoi_write_r13',
              d.find(b'\x41\xC7\x85' + struct.pack('<i', LAPIC_EOI), at, end) < 0)
    pa, pe = region(img, 'pump', 'run')
    pcov = operand_cover(img, pa, pe)
    rep.check('ownership.pump_no_interrupt_bytes',
              not any(pa + p not in pcov
                      for b in (b'\xFB', b'\xF4', b'\xFA')
                      for p in find_all(d[pa:pe], b)))
    hits = []
    for path in sorted((root / 'kernel' / 'desktop').rglob('*')):
        if path.suffix not in ('.asm', '.inc') or not path.is_file():
            continue
        text = path.read_text(encoding='utf-8')
        for _ in re.finditer(r'^\s*sti\b', text, re.M):
            hits.append(path.name)
        for _ in re.finditer(r'^\s*hlt\b', text, re.M):
            if path.name != 'desktop.inc':
                hits.append(path.name + ':hlt')
        for _ in re.finditer(r'^\s*mov\s+dword\s+\[[^\]]*EOI[^\]]*\]', text, re.M):
            hits.append(path.name + ':eoi')
    rep.equal('ownership.source_exactly_one_sti', hits, ['desktop.inc'])


def check_init(rep, img):
    at, end = region(img, 'init', 'wait_count')
    d = img.data
    rep.check('init.full_frame', d[at:at + len(FRAME_ENTER)] == FRAME_ENTER)
    fb = img.at('video_fb_state')
    ready = rip_dword_cmps(img, at, end, fb + FB_OFF_READY)
    width = rip_dword_cmps(img, at, end, fb + FB_OFF_WIDTH)
    height = rip_dword_cmps(img, at, end, fb + FB_OFF_HEIGHT)
    rep.check('init.fb_ready_gate', any(imm == 0 for _, imm in ready))
    rep.check('init.fb_width_gate', any(imm == SCREEN_W for _, imm in width))
    rep.check('init.fb_height_gate', any(imm == SCREEN_H for _, imm in height))
    state_set = rip_refs(img, at, end, b'\x48\xC7', 0x05, 4, 'state')
    rep.check('init.state_set_once',
              len(state_set) == 1 and u32(d, state_set[0] + 7) == 1)
    rep.check('init.enotsup_status',
              d.find(b'\xB8' + struct.pack('<i', ENOTSUP), at, end) >= 0)
    rep.equal('init.no_calls', len(call_sites(img, at, end)), 0)


def check_wait_count(rep, img):
    at, end = region(img, 'wait_count', 'pump')
    d = img.data
    rep.check('wait.full_frame', d[at:at + len(FRAME_ENTER)] == FRAME_ENTER)
    rep.check('wait.rounds_budget',
              d.find(b'\x41\xBE' + struct.pack('<I', POLL_ROUNDS), at, end) >= 0)
    pumps = calls_to(img, at, end, 'pump')
    cmp_pos = d.find(b'\x4D\x39\x2C\x24', at, end)      # cmp [r12], r13
    rep.check('wait.pump_before_compare',
              len(pumps) == 1 and cmp_pos >= 0 and pumps[0] < cmp_pos)
    pos = ordered_positions(img, at, end, [
        b'\x31\xC9',                                    # xor ecx, ecx
        b'\x48\xC7\xC2' + struct.pack('<I', POLL_CHUNK),
        b'\x41\xB8\x01\x00\x00\x00',                    # mov r8d, 1 (spin window)
    ])
    rep.check('wait.sti_wait_mode1_budget', pos is not None)
    waits = calls_to(img, at, end, 'irq_sti_wait')
    rep.equal('wait.single_sti_wait_call', len(waits), 1)
    if pos and waits:
        rep.check('wait.args_before_call', pos[-1] < waits[0])
    dec = d.find(b'\x41\xFF\xCE', at, end)              # dec r14d
    j = jcc_after(img, dec + 3, 2) if dec >= 0 else None
    rep.check('wait.bounded_backwards', j is not None and j[1] < dec + 3)
    rep.check('wait.timeout_eio',
              d.find(b'\xB8' + struct.pack('<i', EIO), at, end) >= 0)


def check_run(rep, img):
    at, end = region(img, 'run', 'cursor_place')
    d = img.data
    rep.check('run.full_frame', d[at:at + len(FRAME_ENTER)] == FRAME_ENTER)
    pos = ordered_positions(img, at, end, [
        b'\xFA',                                        # cli
        b'\x48\xB8' + struct.pack('<Q', LAPIC_VA),      # mov rax, LAPIC VA
        lvt_mask_rax(),                                 # LVT timer mask
        mov_ecx(IDX_KBD_LO), mov_edx(RTE_KBD_LIVE),
        mov_ecx(IDX_MOUSE_LO), mov_edx(RTE_MOUSE_LIVE),
        b'\xFB\xF4\xFA',                                # sti / hlt / cli
    ])
    rep.check('run.mask_before_sti_sequence', pos is not None,
              'cli / LVT mask / both RTEs live / sti hlt cli, in order')
    writes = calls_to(img, at, end, 'input_ioapic_write')
    rep.equal('run.two_rte_writes', len(writes), 2)
    if pos:
        rep.check('run.lvt_mask_strictly_before_sti', pos[2] < pos[7])
        rep.check('run.rte_writes_before_sti',
                  len(writes) == 2 and writes[1] < pos[7])
    sti = d.find(b'\xFB\xF4\xFA', at, end)
    pumps = calls_to(img, at, end, 'pump')
    rep.check('run.pump_after_cli', len(pumps) == 1 and sti >= 0 and
              pumps[0] > sti + 2)
    # task 125: the launch pump follows the redraw pump in the same
    # IF = 0 window, click mode (ecx = 1), then the loop jumps back
    launches = calls_to(img, at, end, 'launch_pump')
    rep.equal('run.launch_pump_once', len(launches), 1)
    if pumps and launches:
        rep.check('run.launch_pump_after_pump', pumps[0] < launches[0])
        rep.check('run.click_mode_arg',
                  d[pumps[0] + 5:pumps[0] + 10] == mov_ecx(1) and
                  launches[0] == pumps[0] + 10)
        j = launches[0] + 5
        ok = (d[j] == 0xEB and
              j + 2 + struct.unpack_from('<b', d, j + 1)[0] == sti)
        rep.check('run.jmp_back_to_sti', ok, 'the resident loop never exits')
    rep.check('run.never_returns', d.find(b'\xC3', at, end) < 0)


def check_pump(rep, img):
    """The DESKTOP-v2 frozen redraw order, decoded from the bytes:
    restore OLD -> click_pump (menu/taskbar) -> echo_pump -> place NEW."""
    at, end = region(img, 'pump', 'run')
    d = img.data
    rep.check('pump.full_frame', d[at:at + len(FRAME_ENTER)] == FRAME_ENTER)
    # the clamp/evidence publication mirrors the frozen cursor_sync
    rep.equal('pump.reads_input_x',
              len(rip_refs(img, at, end, b'\x4C\x8B', 0x25, 0, 'input_mouse_x')), 1)
    rep.equal('pump.reads_input_y',
              len(rip_refs(img, at, end, b'\x4C\x8B', 0x2D, 0, 'input_mouse_y')), 1)
    rep.equal('pump.publishes_evidence_x',
              len(rip_refs(img, at, end, b'\x4C\x89', 0x25, 0, 'test_cursor_x')), 1)
    rep.equal('pump.publishes_evidence_y',
              len(rip_refs(img, at, end, b'\x4C\x89', 0x2D, 0, 'test_cursor_y')), 1)
    # step 1: the OLD position restore, save buffer blit
    restores = rip_refs(img, at, end, b'\x48\x8D', 0x0D, 0, 'cursor_save')
    old_x = rip_refs(img, at, end, b'\x48\x8B', 0x15, 0, 'cursor_x')
    old_y = rip_refs(img, at, end, b'\x4C\x8B', 0x05, 0, 'cursor_y')
    blits = calls_to(img, at, end, 'video_blit_rect')
    clicks = calls_to(img, at, end, 'click_pump')
    echoes = calls_to(img, at, end, 'echo_pump')
    places = calls_to(img, at, end, 'cursor_place')
    rep.check('pump.frozen_redraw_order',
              len(restores) == 1 and len(old_x) == 1 and len(old_y) == 1 and
              len(blits) == 1 and len(clicks) == 1 and len(echoes) == 1 and
              len(places) == 1 and
              restores[0] < blits[0] < clicks[0] < echoes[0] < places[0],
              'restore OLD -> menu/taskbar -> echo -> place NEW')
    rep.equal('pump.no_other_calls', len(call_sites(img, at, end)), 4)
    # task 143 (DEFECT-1): the stationary path FALLS THROUGH to the OLD
    # restore -- no menu_flag gate, no shortcut to .restored. A start
    # press with a stationary cursor opens the menu; menu_open's
    # readback and the place readback must never capture a live sprite
    # (the task-125 menu-open gate only covered the menu-close sister)
    menu_gates = [(s, t, v) for s, t, v in cmp_qword_sites(img, at, end)
                  if t == img.at('menu_flag')]
    rep.equal('pump.no_menu_flag_gate', len(menu_gates), 0)
    ycmp = find_all(d[at:end], b'\x4C\x3B\x2D')
    jne_ok = False
    fall_ok = False
    if len(ycmp) == 1:
        p = at + ycmp[0] + 7
        nb = d[p:p + 2]
        after = p
        if nb[0] == 0x75:
            jne_ok = True
            after = p + 2
        elif nb == b'\x0F\x85':
            jne_ok = True
            after = p + 6
        # right after the jne .moved: mov r14d,1 + lea rcx,[cursor_save]
        # -- the fall-through IS the restore, no conditional skip
        fall_ok = d[after:after + 9] == b'\x41\xBE\x01\x00\x00\x00\x48\x8D\x0D'
    rep.check('pump.stationary_y_branches_moved',
              len(ycmp) == 1 and jne_ok,
              'cmp r13,[cursor_y] / jne .moved')
    rep.check('pump.stationary_falls_through_to_restore',
              len(ycmp) == 1 and fall_ok,
              'mov r14d,1 / lea rcx,[cursor_save] right after the y jne')
    # a menu redraw under a stationary cursor forces the re-place:
    # or r14d, eax right after the click pump returns
    if clicks:
        rep.check('pump.menu_redraw_forces_replace',
                  d.find(b'\x41\x09\xC6', clicks[0], end) >= 0)
    # step 4 args: the clamped NEW coordinates from r12/r13
    if echoes:
        pos = ordered_positions(img, at, end,
                                [b'\x4C\x89\xE1', b'\x4C\x89\xEA'],
                                start=echoes[0])
        rep.check('pump.place_new_position_args', pos is not None)
    upd_x = rip_refs(img, at, end, b'\x4C\x89', 0x25, 0, 'cursor_x')
    upd_y = rip_refs(img, at, end, b'\x4C\x89', 0x2D, 0, 'cursor_y')
    rep.check('pump.position_update_after_place',
              len(upd_x) == 1 and len(upd_y) == 1 and len(places) == 1 and
              places[0] < upd_x[0] and places[0] < upd_y[0])


def check_cursor(rep, img):
    d = img.data
    # place: save (readback) -> composite -> draw (blit), frozen order
    at, end = region(img, 'cursor_place', 'cursor_composite')
    rep.check('cursor.place_full_frame', d[at:at + len(FRAME_ENTER)] == FRAME_ENTER)
    saves = rip_refs(img, at, end, b'\x48\x8D', 0x0D, 0, 'cursor_save')
    draws = rip_refs(img, at, end, b'\x48\x8D', 0x0D, 0, 'cursor_draw')
    rep.equal('cursor.place_save_lea', len(saves), 1)
    rep.equal('cursor.place_draw_lea', len(draws), 1)
    rep.equal('cursor.sprite_w_arg',
              len(find_all(d[at:end], b'\x41\xB9' + struct.pack('<I', SPRITE_W))), 2)
    rep.equal('cursor.sprite_h_stack_arg',
              len(find_all(d[at:end], stack_color(SPRITE_H))), 2)
    reads = calls_to(img, at, end, 'video_readback_rect')
    comps = calls_to(img, at, end, 'cursor_composite')
    blits = calls_to(img, at, end, 'video_blit_rect')
    rep.check('cursor.save_composite_draw_order',
              len(reads) == 1 and len(comps) == 1 and len(blits) == 1 and
              reads[0] < comps[0] < blits[0])
    if saves and draws and reads and blits:
        rep.check('cursor.save_lea_before_readback', saves[0] < reads[0])
        rep.check('cursor.draw_lea_before_blit', comps[0] < draws[0] < blits[0])

    # composite: transparent keeps the saved pixel, black/white replace
    at, end = region(img, 'cursor_composite', 'cursor_sync')
    rep.check('cursor.composite_full_frame', d[at:at + len(FRAME_ENTER)] == FRAME_ENTER)
    rep.equal('cursor.composite_sprite_lea',
              len(rip_refs(img, at, end, b'\x48\x8D', 0x35, 0, 'sprite')), 1)
    rep.equal('cursor.composite_save_lea',
              len(rip_refs(img, at, end, b'\x48\x8D', 0x3D, 0, 'cursor_save')), 1)
    rep.equal('cursor.composite_draw_lea',
              len(rip_refs(img, at, end, b'\x48\x8D', 0x15, 0, 'cursor_draw')), 1)
    rep.check('cursor.composite_pixel_count',
              d.find(mov_ecx(SPRITE_BYTES), at, end) >= 0)
    rep.check('cursor.white_store',
              d.find(b'\xC7\x02' + struct.pack('<I', CURSOR_WHITE), at, end) >= 0)
    rep.check('cursor.black_store',
              d.find(b'\xC7\x02' + struct.pack('<I', CURSOR_BLACK), at, end) >= 0)
    rep.check('cursor.transparent_keeps_saved',
              d.find(b'\x8B\x07\x89\x02', at, end) >= 0)
    dec = d.find(b'\xFF\xC9', at, end)
    j = jcc_after(img, dec + 2, 2) if dec >= 0 else None
    rep.check('cursor.composite_bounded_loop', j is not None and j[1] < dec + 2)

    # sync: clamp, publish evidence, restore OLD, then place NEW
    at, end = region(img, 'cursor_sync', 'sprite')
    rep.check('cursor.sync_full_frame', d[at:at + len(FRAME_ENTER)] == FRAME_ENTER)
    rep.equal('cursor.sync_reads_input_x',
              len(rip_refs(img, at, end, b'\x4C\x8B', 0x25, 0, 'input_mouse_x')), 1)
    rep.equal('cursor.sync_reads_input_y',
              len(rip_refs(img, at, end, b'\x4C\x8B', 0x2D, 0, 'input_mouse_y')), 1)
    rep.check('cursor.clamp_x',
              d.find(b'\x49\x81\xFC' + struct.pack('<I', CURSOR_XMAX), at, end) >= 0
              and d.find(b'\x49\xC7\xC4' + struct.pack('<I', CURSOR_XMAX), at, end) >= 0)
    rep.check('cursor.clamp_y',
              d.find(b'\x49\x81\xFD' + struct.pack('<I', CURSOR_YMAX), at, end) >= 0
              and d.find(b'\x49\xC7\xC5' + struct.pack('<I', CURSOR_YMAX), at, end) >= 0)
    rep.equal('cursor.publishes_evidence_x',
              len(rip_refs(img, at, end, b'\x4C\x89', 0x25, 0, 'test_cursor_x')), 1)
    rep.equal('cursor.publishes_evidence_y',
              len(rip_refs(img, at, end, b'\x4C\x89', 0x2D, 0, 'test_cursor_y')), 1)
    rep.equal('cursor.restore_reads_old_x',
              len(rip_refs(img, at, end, b'\x48\x8B', 0x15, 0, 'cursor_x')), 1)
    rep.equal('cursor.restore_reads_old_y',
              len(rip_refs(img, at, end, b'\x4C\x8B', 0x05, 0, 'cursor_y')), 1)
    blits = calls_to(img, at, end, 'video_blit_rect')
    places = calls_to(img, at, end, 'cursor_place')
    rep.check('cursor.restore_before_place',
              len(blits) == 1 and len(places) == 1 and blits[0] < places[0],
              'the OLD background is restored before the NEW position is saved')
    rep.equal('cursor.sync_updates_position_x',
              len(rip_refs(img, at, end, b'\x4C\x89', 0x25, 0, 'cursor_x')), 1)
    rep.equal('cursor.sync_updates_position_y',
              len(rip_refs(img, at, end, b'\x4C\x89', 0x2D, 0, 'cursor_y')), 1)
    # task 143 sister audit: the sync stationary shortcut jumps to .out
    # BEFORE any restore/place -- a stationary sync never readbacks, so
    # it can neither capture nor restore a live sprite (the app POLL
    # path stays frozen)
    sync_ycmp = find_all(d[at:end], b'\x4C\x3B\x2D')
    sync_je = False
    if len(sync_ycmp) == 1:
        nb = d[at + sync_ycmp[0] + 7:at + sync_ycmp[0] + 9]
        sync_je = nb[0] == 0x74 or nb == b'\x0F\x84'
    rep.check('cursor.sync_stationary_skips_place',
              len(sync_ycmp) == 1 and sync_je,
              'cmp r13,[cursor_y] / je .out (no readback when stationary)')


def check_echo(rep, img):
    d = img.data
    # pump: consume through the private index, 16 byte entries
    at, end = region(img, 'echo_pump', 'echo_key')
    rep.check('echo.pump_full_frame', d[at:at + len(FRAME_ENTER)] == FRAME_ENTER)
    rep.equal('echo.pump_reads_consumed',
              len(rip_refs(img, at, end, b'\x4C\x8B', 0x25, 0, 'kbd_consumed')), 1)
    rep.equal('echo.pump_compares_used',
              len(rip_refs(img, at, end, b'\x4C\x3B', 0x25, 0, 'input_kbd_used')), 1)
    rep.check('echo.entry_stride_16', d.find(b'\x48\xC1\xE0\x04', at, end) >= 0)
    rep.equal('echo.pump_buf_lea',
              len(rip_refs(img, at, end, b'\x48\x8D', 0x15, 0, 'input_kbd_buf')), 1)
    rep.check('echo.ascii_qword_load', d.find(b'\x4C\x8B\x2C\x02', at, end) >= 0)
    rep.equal('echo.pump_advances_consumed',
              len(rip_refs(img, at, end, b'\x48\xFF', 0x05, 0, 'kbd_consumed')), 1)
    rep.equal('echo.pump_calls_key', len(calls_to(img, at, end, 'echo_key')), 1)

    # key: Enter / Backspace / printable range / full screen stop
    at, end = region(img, 'echo_key', 'title')
    rep.check('echo.key_full_frame', d[at:at + len(FRAME_ENTER)] == FRAME_ENTER)
    for name, val in (('enter', KEY_ENTER), ('backspace', KEY_BACKSPACE),
                      ('ascii_first', ASCII_FIRST), ('ascii_last', ASCII_LAST)):
        rep.check('echo.cmp_%s' % name,
                  d.find(b'\x49\x83\xFC' + bytes((val,)), at, end) >= 0)
    cmps = {(t, imm) for _, t, imm in cmp_qword_sites(img, at, end)}
    rep.check('echo.full_screen_stop', (img.at('echo_row'), ECHO_ROWS) in cmps)
    rep.check('echo.line_wrap_compare', (img.at('echo_col'), ECHO_COLS) in cmps)
    rep.check('echo.backspace_line_start_guard', (img.at('echo_col'), 0) in cmps)
    rep.equal('echo.char_scratch_store',
              len(rip_refs(img, at, end, b'\x44\x88', 0x25, 0, 'echo_char')), 1)
    rep.check('echo.column_pixel_stride', d.find(b'\x48\xC1\xE0\x03', at, end) >= 0)
    rep.check('echo.row_pixel_stride', d.find(b'\x48\xC1\xE0\x04', at, end) >= 0)
    rep.check('echo.text_origin_x', d.find(b'\x48\x83\xC0' + bytes((ECHO_X,)), at, end) >= 0)
    rep.check('echo.text_origin_y', d.find(b'\x48\x83\xC0' + bytes((ECHO_Y,)), at, end) >= 0)
    rep.check('echo.white_color',
              d.find(b'\x41\xB8' + struct.pack('<I', ECHO_COLOR), at, end) >= 0)
    rep.equal('echo.scratch_lea',
              len(rip_refs(img, at, end, b'\x4C\x8D', 0x0D, 0, 'echo_char')), 1)
    rep.equal('echo.puts_call', len(calls_to(img, at, end, 'video_puts')), 1)
    rep.equal('echo.keys_counted',
              len(rip_refs(img, at, end, b'\x48\xFF', 0x05, 0, 'test_keys')), 1)
    rep.equal('echo.column_advance',
              len(rip_refs(img, at, end, b'\x48\xFF', 0x05, 0, 'echo_col')), 1)
    rep.equal('echo.backspace_column_retreat',
              len(rip_refs(img, at, end, b'\x48\xFF', 0x0D, 0, 'echo_col')), 1)
    rep.equal('echo.row_advance_wrap_and_enter',
              len(rip_refs(img, at, end, b'\x48\xFF', 0x05, 0, 'echo_row')), 2)
    rep.equal('echo.drops_counted',
              len(rip_refs(img, at, end, b'\x48\xFF', 0x05, 0, 'echo_drops')), 1)
    zeros = [p for p in rip_refs(img, at, end, b'\x48\xC7', 0x05, 4, 'echo_col')
             if u32(d, p + 7) == 0]
    rep.equal('echo.column_reset_wrap_and_enter', len(zeros), 2)
    # the backspace erase, DESKTOP-v2: restore the glyph cell from the
    # wallpaper source, one packed 8 pixel row per blit, 16 rows
    rep.check('echo.erase_cell_width',
              d.find(b'\x41\xB9' + struct.pack('<I', FONT_W), at, end) >= 0)
    rep.check('echo.erase_row_count',
              d.find(b'\x41\xBD' + struct.pack('<I', FONT_H), at, end) >= 0)
    rep.equal('echo.erase_wallpaper_lea',
              len(rip_refs(img, at, end, b'\x48\x8D', 0x0D, 0, 'wallpaper')), 1)
    rep.check('echo.erase_row_stride_1024',
              d.find(b'\x48\xC1\xE0\x0A', at, end) >= 0)
    rep.check('echo.erase_dword_stride',
              d.find(b'\x48\xC1\xE0\x02', at, end) >= 0)
    rep.check('echo.erase_single_row_height',
              d.find(stack_color(1), at, end) >= 0)
    rep.equal('echo.erase_blit_call',
              len(calls_to(img, at, end, 'video_blit_rect')), 1)
    rep.equal('echo.erase_no_flat_fill',
              len(calls_to(img, at, end, 'video_fill_rect')), 0)
    dec = d.find(b'\x41\xFF\xCD', at, end)              # dec r13d
    j = jcc_after(img, dec + 3, 2) if dec >= 0 else None
    rep.check('echo.erase_bounded_16_rows', j is not None and j[1] < dec + 3)


def check_selftest(rep, img):
    # the region stops at the wallpaper: the embedded image is data,
    # its stray bytes must never decode as self test instructions
    at, end = region(img, 'selftest', 'wallpaper')
    d = img.data
    rep.check('selftest.full_frame', d[at:at + len(FRAME_ENTER)] == FRAME_ENTER)
    rep.check('selftest.entry_cli', d[at + len(FRAME_ENTER):at + len(FRAME_ENTER) + 2]
              == b'\xFC\xFA')
    rep.check('selftest.lapic_base_r13',
              d.find(b'\x49\xBD' + struct.pack('<Q', LAPIC_VA), at, end) >= 0)
    # the consumed snapshot: load input_kbd_used, store kbd_consumed
    loads = rip_refs(img, at, end, b'\x48\x8B', 0x05, 0, 'input_kbd_used')
    stores = rip_refs(img, at, end, b'\x48\x89', 0x05, 0, 'kbd_consumed')
    rep.check('selftest.consumed_snapshot',
              len(loads) == 1 and len(stores) == 1 and loads[0] < stores[0],
              'the input self test keys are never re-echoed')
    # leg 0, DESKTOP-v2: the full screen wallpaper blit from the
    # embedded asset, never a flat color fill
    wp_lea = rip_refs(img, at, end, b'\x48\x8D', 0x0D, 0, 'wallpaper')
    rep.equal('selftest.wallpaper_lea', len(wp_lea), 1)
    pos = ordered_positions(img, at, end, [
        b'\x31\xD2',                                    # xor edx, edx
        b'\x45\x31\xC0',                                # xor r8d, r8d
        b'\x41\xB9' + struct.pack('<I', WALLPAPER_W),
        stack_color(WALLPAPER_H),
    ], start=wp_lea[0] if wp_lea else None)
    rep.check('selftest.wallpaper_blit_args', pos is not None)
    st_blits = calls_to(img, at, end, 'video_blit_rect')
    rep.check('selftest.wallpaper_blit_call',
              len(st_blits) == 1 and wp_lea and wp_lea[0] < st_blits[0])
    # leg 1: the bar rectangle, the start button accents, the title
    pos = ordered_positions(img, at, end, [
        mov_ecx(BAR_X), mov_edx(BAR_Y),
        b'\x41\xB8' + struct.pack('<I', BAR_W),
        b'\x41\xB9' + struct.pack('<I', BAR_H),
        stack_color(BAR_COLOR),
        # the underline: (0, BAR_H-1, START_W, 1) in the accent gray
        b'\x31\xC9',                                    # xor ecx, ecx
        mov_edx(BAR_H - 1),
        b'\x41\xB8' + struct.pack('<I', START_W),
        b'\x41\xB9' + struct.pack('<I', 1),
        stack_color(ACCENT_COLOR),
        # the separator: (START_W, TITLE_Y+FONT_H, 1, 8), below the text
        mov_ecx(START_W), mov_edx(TITLE_Y + FONT_H),
        b'\x41\xB8' + struct.pack('<I', 1),
        b'\x41\xB9' + struct.pack('<I', BAR_H - TITLE_Y - FONT_H),
        stack_color(ACCENT_COLOR),
        mov_ecx(TITLE_X), mov_edx(TITLE_Y),
        b'\x41\xB8' + struct.pack('<I', TITLE_COLOR),
    ])
    rep.check('selftest.taskbar_args', pos is not None)
    rep.equal('selftest.title_lea',
              len(rip_refs(img, at, end, b'\x4C\x8D', 0x0D, 0, 'title')), 1)
    fills = calls_to(img, at, end, 'video_fill_rect')
    puts = calls_to(img, at, end, 'video_puts')
    rep.equal('selftest.three_fills_one_puts', (len(fills), len(puts)), (3, 1))
    # leg 2: the initial cursor at the derived position
    cx = rip_refs(img, at, end, b'\x48\xC7', 0x05, 4, 'cursor_x')
    cy = rip_refs(img, at, end, b'\x48\xC7', 0x05, 4, 'cursor_y')
    rep.check('selftest.initial_cursor_stores',
              len(cx) == 1 and u32(d, cx[0] + 7) == CURSOR_X0 and
              len(cy) == 1 and u32(d, cy[0] + 7) == CURSOR_Y0)
    rep.check('selftest.initial_cursor_args',
              d.find(b'\x48\xC7\xC1' + struct.pack('<I', CURSOR_X0), at, end) >= 0 and
              d.find(b'\x48\xC7\xC2' + struct.pack('<I', CURSOR_Y0), at, end) >= 0)
    rep.equal('selftest.initial_place_call',
              len(calls_to(img, at, end, 'cursor_place')), 1)
    # the host handshake stages 1..5, each published exactly once
    stages = rip_refs(img, at, end, b'\x48\xC7', 0x05, 4, 'test_stage')
    vals = sorted(u32(d, p + 7) for p in stages)
    rep.equal('selftest.stage_sequence', vals, [1, 2, 3, 4, 5])
    # four bounded waits on the right counters (keys 1, keys 2, cursor
    # X, mouse packet count)
    rep.equal('selftest.four_waits', len(calls_to(img, at, end, 'wait_count')), 4)
    rcx_loads = [u32(d, at + p + 3) for p in find_all(d[at:end], b'\x48\xC7\xC1')]
    rep.equal('selftest.waits_on_keys', rcx_loads.count(img.at('test_keys')), 2)
    rep.equal('selftest.waits_on_cursor_x', rcx_loads.count(img.at('test_cursor_x')), 1)
    rep.equal('selftest.waits_on_mouse_count',
              rcx_loads.count(img.at('input_test_mouse_count')), 1)
    for name, val in (('keys_first', 1), ('keys_final', ST_KEYS_FINAL),
                      ('cursor_x', ST_CURSOR_X), ('mouse_final', ST_MOUSE_FINAL)):
        rep.check('selftest.wait_target_%s' % name,
                  d.find(b'\x48\xC7\xC2' + struct.pack('<I', val), at, end) >= 0)
    # the RTE choreography: live twice (echo/mouse legs), masked twice
    # (final freeze + failure path)
    rep.equal('selftest.kbd_live_once',
              len(find_all(d[at:end], mov_edx(RTE_KBD_LIVE))), 1)
    rep.equal('selftest.mouse_live_once',
              len(find_all(d[at:end], mov_edx(RTE_MOUSE_LIVE))), 1)
    rep.equal('selftest.kbd_masked_twice',
              len(find_all(d[at:end], mov_edx(RTE_KBD_MASKED))), 2)
    rep.equal('selftest.mouse_masked_twice',
              len(find_all(d[at:end], mov_edx(RTE_MOUSE_MASKED))), 2)
    freezes = find_all(d[at:end], b'\xFA' + lvt_mask_r13())
    rep.equal('selftest.cli_then_lvt_mask_twice', len(freezes), 2)
    # the frozen final verdict, every compare read out of the bytes
    want = Counter({
        (img.at('input_test_kbd_count'), ST_KBD_FINAL): 1,
        (img.at('input_test_mouse_count'), ST_MOUSE_FINAL): 1,
        (img.at('irq_test_ticks'), TICKS_FINAL): 1,
        (img.at('input_kbd_used'), ST_KBD_USED_FINAL): 1,
        (img.at('input_kbd_drops'), 0): 1,
        (img.at('test_keys'), ST_KEYS_FINAL): 1,
        (img.at('test_cursor_x'), ST_CURSOR_X): 1,
        (img.at('test_cursor_y'), ST_CURSOR_Y): 2,
        (img.at('cursor_x'), ST_CURSOR_X): 1,
        (img.at('cursor_y'), ST_CURSOR_Y): 1,
        (img.at('echo_col'), ST_KEYS_FINAL): 2,
        (img.at('echo_row'), 0): 2,
        (img.at('echo_drops'), 0): 2,
        # DESKTOP-v2: the leg 4 click landed outside the menu
        (img.at('test_clicks'), ST_CLICKS_FINAL): 1,
        (img.at('test_menu_opens'), ST_MENU_OPENS_FINAL): 1,
        (img.at('menu_flag'), 0): 1,
        (img.at('app_launch'), 0): 1,
        (img.at('prev_buttons'), 0): 1,
    })
    got = Counter((t, imm) for _, t, imm in cmp_qword_sites(img, at, end))
    rep.check('selftest.final_compares_exact', got == want,
              'missing %r, extra %r' % (want - got, got - want))
    # all six failure bits exist and land in the mask qword
    for bit in range(6):
        needle = b'\x49\x83\xCC' + bytes((1 << bit,))
        rep.check('selftest.leg_%d_failure_bit' % bit,
                  d.find(needle, at, end) >= 0)
    rep.equal('selftest.mask_stores',
              len(rip_refs(img, at, end, b'\x4C\x89', 0x25, 0, 'test_mask')), 2)
    rep.check('selftest.failure_prints',
              len(calls_to(img, at, end, 'cpu_com1_puts')) == 2 and
              len(calls_to(img, at, end, 'cpu_com1_puthex')) == 1)
    rep.check('selftest.timeout_status',
              d.find(b'\xB8' + struct.pack('<i', EIO), at, end) >= 0)


def check_menu(rep, img):
    """DESKTOP-v2 T3: the click edge pump and the menu open/close,
    every geometry immediate and every state write read from bytes."""
    d = img.data
    # click pump: private edge memory, press-only clicks, frozen rects
    at, end = region(img, 'click_pump', 'menu_open')
    rep.check('menu.click_full_frame', d[at:at + len(FRAME_ENTER)] == FRAME_ENTER)
    rep.equal('menu.reads_mouse_b1',
              len(rip_refs(img, at, end, b'\x4C\x8B', 0x25, 0, 'input_mouse_b1')), 1)
    rep.check('menu.button_mask_1',
              d.find(b'\x49\x83\xE4\x01', at, end) >= 0)   # and r12, 1
    rep.equal('menu.prev_read',
              len(rip_refs(img, at, end, b'\x4C\x8B', 0x2D, 0, 'prev_buttons')), 1)
    rep.equal('menu.prev_write',
              len(rip_refs(img, at, end, b'\x4C\x89', 0x25, 0, 'prev_buttons')), 1)
    rep.check('menu.edge_compare',
              d.find(b'\x4D\x39\xEC', at, end) >= 0)       # cmp r12, r13
    rep.equal('menu.clicks_counted',
              len(rip_refs(img, at, end, b'\x48\xFF', 0x05, 0, 'test_clicks')), 1)
    # the start button hit test: x < 64 and y < 32
    rep.check('menu.start_hit_x',
              d.find(b'\x49\x83\xFE' + bytes((START_W,)), at, end) >= 0)
    rep.check('menu.start_hit_y',
              d.find(b'\x49\x83\xFF' + bytes((START_H,)), at, end) >= 0)
    # the menu rectangle hit test: x < 200, 32 <= y < 66
    rep.check('menu.entry_hit_x',
              d.find(b'\x49\x81\xFE' + struct.pack('<I', MENU_W), at, end) >= 0)
    rep.check('menu.entry_hit_ylo',
              d.find(b'\x49\x83\xFF' + bytes((MENU_Y,)), at, end) >= 0)
    rep.check('menu.entry_hit_yhi',
              d.find(b'\x49\x83\xFF' + bytes((MENU_Y + MENU_H,)), at, end) >= 0)
    launch = rip_refs(img, at, end, b'\x48\xC7', 0x05, 4, 'app_launch')
    rep.check('menu.launch_flag_store',
              len(launch) == 1 and u32(d, launch[0] + 7) == 1,
              'the CHIP-8 entry only sets the private task 125 label')
    # task 125: the start/entry press edges advance their own counters
    # and publish the click stages 6/7 (the boot legs never see them)
    rep.equal('menu.start_clicks_counted',
              len(rip_refs(img, at, end, b'\x48\xFF', 0x05, 0,
                           'test_start_clicks')), 1)
    entry_clicks = rip_refs(img, at, end, b'\x48\xFF', 0x05, 0,
                            'test_entry_clicks')
    rep.equal('menu.entry_clicks_counted', len(entry_clicks), 1)
    stages = rip_refs(img, at, end, b'\x48\xC7', 0x05, 4, 'test_stage')
    vals = sorted(u32(d, p + 7) for p in stages)
    rep.equal('menu.click_stage_sequence', vals,
              [STAGE_START_CLICK, STAGE_ENTRY_CLICK])
    rep.check('menu.entry_evidence_after_flag',
              bool(launch) and bool(entry_clicks) and
              launch[0] < entry_clicks[0])
    rep.equal('menu.open_call_once', len(calls_to(img, at, end, 'menu_open')), 1)
    rep.equal('menu.close_call_twice', len(calls_to(img, at, end, 'menu_close')), 2)
    rep.equal('menu.click_no_direct_video',
              len(calls_to(img, at, end, 'video_fill_rect')) +
              len(calls_to(img, at, end, 'video_blit_rect')) +
              len(calls_to(img, at, end, 'video_readback_rect')) +
              len(calls_to(img, at, end, 'video_puts')), 0)

    # open: readback save FIRST, then accent frame, panel, label
    at, end = region(img, 'menu_open', 'menu_close')
    rep.check('menu.open_full_frame', d[at:at + len(FRAME_ENTER)] == FRAME_ENTER)
    saves = rip_refs(img, at, end, b'\x48\x8D', 0x0D, 0, 'menu_save')
    rep.equal('menu.open_save_lea', len(saves), 1)
    reads = calls_to(img, at, end, 'video_readback_rect')
    fills = calls_to(img, at, end, 'video_fill_rect')
    puts = calls_to(img, at, end, 'video_puts')
    rep.check('menu.save_before_draw',
              len(reads) == 1 and len(fills) == 2 and len(puts) == 1 and
              saves and saves[0] < reads[0] < fills[0] < fills[1] < puts[0])
    pos = ordered_positions(img, at, end, [
        b'\x41\xB8' + struct.pack('<I', MENU_Y),
        b'\x41\xB9' + struct.pack('<I', MENU_W),
        stack_color(MENU_H),
    ])
    rep.check('menu.save_rect_args', pos is not None)
    rep.check('menu.accent_frame_color',
              d.find(stack_color(ACCENT_COLOR), at, end) >= 0)
    pos = ordered_positions(img, at, end, [
        mov_ecx(MENU_X + 1), mov_edx(MENU_Y + 1),
        b'\x41\xB8' + struct.pack('<I', MENU_W - 2),
        b'\x41\xB9' + struct.pack('<I', MENU_H - 2),
        stack_color(MENU_COLOR),
    ])
    rep.check('menu.inner_panel_args', pos is not None)
    pos = ordered_positions(img, at, end, [
        mov_ecx(MENU_TEXT_X), mov_edx(MENU_TEXT_Y),
        b'\x41\xB8' + struct.pack('<I', TITLE_COLOR),
    ])
    rep.check('menu.entry_label_args', pos is not None)
    rep.equal('menu.entry_label_lea',
              len(rip_refs(img, at, end, b'\x4C\x8D', 0x0D, 0, 'menu_text')), 1)
    flag = rip_refs(img, at, end, b'\x48\xC7', 0x05, 4, 'menu_flag')
    rep.check('menu.open_sets_flag',
              len(flag) == 1 and u32(d, flag[0] + 7) == 1)
    rep.equal('menu.opens_counted',
              len(rip_refs(img, at, end, b'\x48\xFF', 0x05, 0, 'test_menu_opens')), 1)

    # close: blit the saved background back, then clear the flag
    at, end = region(img, 'menu_close', 'menu_text')
    rep.check('menu.close_full_frame', d[at:at + len(FRAME_ENTER)] == FRAME_ENTER)
    saves = rip_refs(img, at, end, b'\x48\x8D', 0x0D, 0, 'menu_save')
    blits = calls_to(img, at, end, 'video_blit_rect')
    rep.check('menu.close_restore_order',
              len(saves) == 1 and len(blits) == 1 and saves[0] < blits[0])
    pos = ordered_positions(img, at, end, [
        b'\x41\xB8' + struct.pack('<I', MENU_Y),
        b'\x41\xB9' + struct.pack('<I', MENU_W),
        stack_color(MENU_H),
    ])
    rep.check('menu.close_rect_args', pos is not None)
    flag = rip_refs(img, at, end, b'\x48\xC7', 0x05, 4, 'menu_flag')
    rep.check('menu.close_clears_flag',
              len(flag) == 1 and u32(d, flag[0] + 7) == 0 and
              blits and blits[0] < flag[0])
    rep.equal('menu.close_no_readback',
              len(calls_to(img, at, end, 'video_readback_rect')), 0)
    # the frozen entry label bytes
    t = d[img.at('menu_text'):img.at('menu_text_end')]
    rep.check('menu.entry_text_frozen', t == MENU_TEXT)


def check_taskbar(rep, img):
    """Task 125 T4: the repaint base, the launch pump and the boot
    wrapper, every state write and call order read from the bytes."""
    d = img.data
    # repaint base: the exact self test leg 0+1 sequence, no legs
    at, end = region(img, 'repaint_base', 'launch_pump')
    rep.check('taskbar.repaint_full_frame',
              d[at:at + len(FRAME_ENTER)] == FRAME_ENTER)
    wp_lea = rip_refs(img, at, end, b'\x48\x8D', 0x0D, 0, 'wallpaper')
    rep.equal('taskbar.repaint_wallpaper_lea', len(wp_lea), 1)
    pos = ordered_positions(img, at, end, [
        b'\x31\xD2',                                    # xor edx, edx
        b'\x45\x31\xC0',                                # xor r8d, r8d
        b'\x41\xB9' + struct.pack('<I', WALLPAPER_W),
        stack_color(WALLPAPER_H),
    ], start=wp_lea[0] if wp_lea else None)
    rep.check('taskbar.repaint_wallpaper_args', pos is not None)
    pos = ordered_positions(img, at, end, [
        mov_ecx(BAR_X), mov_edx(BAR_Y),
        b'\x41\xB8' + struct.pack('<I', BAR_W),
        b'\x41\xB9' + struct.pack('<I', BAR_H),
        stack_color(BAR_COLOR),
        b'\x31\xC9',                                    # xor ecx, ecx
        mov_edx(BAR_H - 1),
        b'\x41\xB8' + struct.pack('<I', START_W),
        b'\x41\xB9' + struct.pack('<I', 1),
        stack_color(ACCENT_COLOR),
        mov_ecx(START_W), mov_edx(TITLE_Y + FONT_H),
        b'\x41\xB8' + struct.pack('<I', 1),
        b'\x41\xB9' + struct.pack('<I', BAR_H - TITLE_Y - FONT_H),
        stack_color(ACCENT_COLOR),
        mov_ecx(TITLE_X), mov_edx(TITLE_Y),
        b'\x41\xB8' + struct.pack('<I', TITLE_COLOR),
    ])
    rep.check('taskbar.repaint_taskbar_args', pos is not None)
    blits = calls_to(img, at, end, 'video_blit_rect')
    fills = calls_to(img, at, end, 'video_fill_rect')
    puts = calls_to(img, at, end, 'video_puts')
    rep.check('taskbar.repaint_order',
              len(blits) == 1 and len(fills) == 3 and len(puts) == 1 and
              wp_lea and wp_lea[0] < blits[0] < fills[0] < fills[1] <
              fills[2] < puts[0],
              'wallpaper -> bar -> underline -> separator -> title')
    rep.equal('taskbar.repaint_title_lea',
              len(rip_refs(img, at, end, b'\x4C\x8D', 0x0D, 0, 'title')), 1)
    rep.equal('taskbar.repaint_no_readback',
              len(calls_to(img, at, end, 'video_readback_rect')), 0)

    # launch pump: the single app_launch consumer
    at, end = region(img, 'launch_pump', 'app_boot')
    rep.check('taskbar.launch_full_frame',
              d[at:at + len(FRAME_ENTER)] == FRAME_ENTER)
    cmps = {(t, imm) for _, t, imm in cmp_qword_sites(img, at, end)}
    rep.check('taskbar.launch_flag_gate', (img.at('app_launch'), 0) in cmps)
    clears = [p for p in rip_refs(img, at, end, b'\x48\xC7', 0x05, 4,
                                  'app_launch') if u32(d, p + 7) == 0]
    rep.equal('taskbar.launch_flag_clear_once', len(clears), 1)
    rep.check('taskbar.launch_mode_save',
              d.find(b'\x41\x89\xCD', at, end) >= 0)     # mov r13d, ecx
    saves = rip_refs(img, at, end, b'\x48\x8D', 0x0D, 0, 'cursor_save')
    blits = calls_to(img, at, end, 'video_blit_rect')
    fills = calls_to(img, at, end, 'video_fill_rect')
    places = calls_to(img, at, end, 'cursor_place')
    launches = calls_to(img, at, end, 'app_launch_fn')
    rep.equal('taskbar.launch_single_app_call', len(launches), 1)
    rep.equal('taskbar.launch_two_save_blits', (len(saves), len(blits)), (2, 2))
    rep.equal('taskbar.launch_two_fills', len(fills), 2)
    rep.equal('taskbar.launch_one_place', len(places), 1)
    rep.equal('taskbar.launch_no_readback',
              len(calls_to(img, at, end, 'video_readback_rect')), 0)
    # the running indicator draw args, then the cleanup erase args
    pos = ordered_positions(img, at, end, [
        mov_ecx(RUN_X), mov_edx(RUN_Y),
        b'\x41\xB8' + struct.pack('<I', RUN_W),
        b'\x41\xB9' + struct.pack('<I', RUN_H),
        stack_color(RUN_COLOR),
    ])
    rep.check('taskbar.indicator_draw_args', pos is not None)
    pos = ordered_positions(img, at, end, [
        mov_ecx(RUN_X), mov_edx(RUN_Y),
        b'\x41\xB8' + struct.pack('<I', RUN_W),
        b'\x41\xB9' + struct.pack('<I', RUN_H),
        stack_color(BAR_COLOR),
    ])
    rep.check('taskbar.indicator_erase_args', pos is not None)
    runs = rip_refs(img, at, end, b'\x48\xFF', 0x05, 0, 'test_app_runs')
    exits = rip_refs(img, at, end, b'\x48\xFF', 0x05, 0, 'test_app_exits')
    rep.equal('taskbar.runs_counted', len(runs), 1)
    rep.equal('taskbar.exits_counted', len(exits), 1)
    stages = rip_refs(img, at, end, b'\x48\xC7', 0x05, 4, 'test_stage')
    vals = sorted(u32(d, p + 7) for p in stages)
    rep.equal('taskbar.launch_stage_sequence', vals,
              [STAGE_APP_RUNNING, STAGE_APP_EXIT])
    # the frozen order: clear flag -> suspend cursor (blit OLD) ->
    # indicator -> runs++ -> app_launch -> exits++ on success; the
    # failure cleanup erases the indicator, blits the save FIRST and
    # places SECOND (the save can never capture a drawn sprite)
    rep.check('taskbar.launch_order',
              bool(clears) and len(blits) == 2 and len(fills) == 2 and
              len(launches) == 1 and len(runs) == 1 and len(exits) == 1 and
              clears[0] < blits[0] < fills[0] < runs[0] < launches[0] <
              exits[0],
              'clear -> suspend -> indicator -> run -> launch -> exit')
    rep.check('taskbar.cleanup_blit_before_place',
              len(blits) == 2 and len(places) == 1 and len(fills) == 2 and
              launches[0] < fills[1] < blits[1] < places[0],
              'the idempotent cleanup never re-saves a drawn cursor')
    rep.check('taskbar.launch_no_pump_calls',
              not calls_to(img, at, end, 'pump') and
              not calls_to(img, at, end, 'click_pump') and
              not calls_to(img, at, end, 'echo_pump'))

    # boot wrapper: set the flag, boot mode (ecx = 0), one pump call
    at, end = region(img, 'app_boot', 'selftest')
    rep.check('taskbar.boot_full_frame',
              d[at:at + len(FRAME_ENTER)] == FRAME_ENTER)
    sets = [p for p in rip_refs(img, at, end, b'\x48\xC7', 0x05, 4,
                                'app_launch') if u32(d, p + 7) == 1]
    rep.equal('taskbar.boot_flag_set_once', len(sets), 1)
    pumps = calls_to(img, at, end, 'launch_pump')
    rep.equal('taskbar.boot_single_pump_call', len(pumps), 1)
    xors = [at + p for p in find_all(d[at:end], b'\x31\xC9')
            if not sets or at + p >= sets[0] + 11]
    rep.check('taskbar.boot_mode_zero',
              bool(xors) and bool(pumps) and xors[0] < pumps[0] and
              bool(sets) and sets[0] < xors[0])
    rep.equal('taskbar.boot_no_direct_app_call',
              len(calls_to(img, at, end, 'app_launch_fn')), 0)
    rep.equal('taskbar.boot_no_video',
              len(calls_to(img, at, end, 'video_fill_rect')) +
              len(calls_to(img, at, end, 'video_blit_rect')) +
              len(calls_to(img, at, end, 'video_puts')), 0)


def check_wallpaper(rep, img, root):
    """The embedded asset byte for byte, its sha256, the manifest sha
    mirror and the independently frozen T1 probe truth."""
    asset_path = root / 'kernel' / 'desktop' / 'assets' / 'wallpaper.bgrx'
    if not rep.check('wallpaper.asset_present', asset_path.is_file(),
                     str(asset_path)):
        return
    asset = asset_path.read_bytes()
    rep.equal('wallpaper.asset_size', len(asset), WALLPAPER_BYTES)
    rep.equal('wallpaper.asset_sha256',
              hashlib.sha256(asset).hexdigest(), WALLPAPER_SHA256)
    got = img.data[img.at('wallpaper'):img.at('wallpaper_end')]
    rep.check('wallpaper.embedded_bytes_identical', got == asset,
              'the incbin image must equal the gated asset byte for byte')
    digest = struct.unpack('>4Q', bytes.fromhex(WALLPAPER_SHA256))
    for i in range(4):
        rep.equal('wallpaper.sha_qword_%d' % i,
                  img['wallpaper_sha%d' % i], digest[i])
    # the T1 truth table sampled from the ASSET, independent of any
    # rendering implementation: one little-endian BGRX dword per pixel
    for x, y, color in WALLPAPER_PROBES:
        rep.equal('wallpaper.probe_%d_%d' % (x, y),
                  u32(asset, (y * WALLPAPER_W + x) * 4), color)


def check_sources(rep, root):
    module = root / 'kernel' / 'desktop'
    # the frozen nine-file module set of DESKTOP-v2 (task 124 added
    # menu.inc and wallpaper.inc). Whitelist style (task 138 W4): only
    # directory entries (the assets/ tree) are exempt; any other foreign
    # file stays in the list and fails file_set instead of silently
    # passing a name filter.
    module_files = ['api.inc', 'cursor.inc', 'data.inc', 'desktop.asm',
                    'desktop.inc', 'echo.inc', 'menu.inc', 'selftest.inc',
                    'taskbar.inc', 'wallpaper.inc']
    exempt = set()
    files = sorted(p.name for p in module.glob('*')
                   if p.is_file() and p.name not in exempt)
    rep.equal('source.file_set', files, module_files)

    api = (module / 'api.inc').read_text(encoding='utf-8')
    rep.check('source.api_standalone',
              not re.search(r"^\s*include\b", api, re.M))
    rep.check('source.api_has_contract_prefix', 'DESKTOP_API_VERSION' in api)
    api_asserts = len(re.findall(r'\berr\b', api))
    rep.check('source.api_asserts_the_contract', api_asserts >= 30,
              '%d explicit assertions' % api_asserts)
    data_text = (module / 'data.inc').read_text(encoding='utf-8')
    data_asserts = len(re.findall(r'\berr\b', data_text))
    rep.check('source.data_asserts_the_layout', data_asserts >= 15,
              '%d explicit assertions' % data_asserts)
    rep.check('source.data_no_uninitialised_reservation',
              not re.search(r"^\s*rb\b", data_text, re.M))

    for path in sorted(module.glob('*')):
        if not path.is_file() or path.name in exempt:
            continue
        text = path.read_text(encoding='utf-8')
        rep.check('source.%s.no_tabs' % path.name, '\t' not in text)
        rep.check('source.%s.trailing_newline' % path.name, text.endswith('\n'))
        rep.check('source.%s.ascii_only' % path.name,
                  all(ord(ch) < 0x80 for ch in text))
        if path.suffix == '.inc':
            rep.check('source.%s.no_org' % path.name,
                      not re.search(r'^\s*org\b', text, re.M))
            rep.check('source.%s.no_use64' % path.name,
                      not re.search(r'^\s*use64\b', text, re.M))

    agg = (module / 'desktop.asm').read_text(encoding='utf-8')
    rep.check('source.standalone_block_guarded',
              'if ~ defined DESKTOP_HOSTED' in agg)
    order = re.findall(r"^include\s+'([^']+)'", agg, re.M)
    rep.equal('source.include_order', order, [
        'api.inc', 'desktop.inc', 'cursor.inc', 'echo.inc', 'menu.inc',
        'taskbar.inc', 'selftest.inc', 'wallpaper.inc', 'data.inc'])
    wp_text = (module / 'wallpaper.inc').read_text(encoding='utf-8')
    rep.check('source.wallpaper_incbin',
              re.search(r"^\s*file\s+'assets/wallpaper\.bgrx'", wp_text, re.M)
              is not None)


def check_position_independence(rep, asm_, root, out, low):
    hi_path = out / 'layout-hi.bin'
    ok, text = asm_.run(root / 'tests/stage2-desktop/layout.asm', hi_path,
                        org=KERNEL_BASE)
    if not rep.check('reloc.assembles_at_kernel_base', ok, text.strip()[-400:]):
        return
    hi = load_image(hi_path, KERNEL_BASE)
    delta = KERNEL_BASE
    labels = ['code_start', 'code_end', 'data_start', 'data_end',
              'init', 'wait_count', 'pump', 'run', 'cursor_place',
              'cursor_composite', 'cursor_sync', 'echo_pump', 'echo_key',
              'click_pump', 'menu_open', 'menu_close',
              'repaint_base', 'launch_pump', 'app_boot', 'app_launch_fn',
              'menu_text', 'menu_text_end', 'wallpaper', 'wallpaper_end',
              'selftest', 'title', 'title_end', 'sprite', 'sprite_end',
              'msg_selftest', 'msg_nl',
              'video_fill_rect', 'video_blit_rect', 'video_readback_rect',
              'video_puts', 'video_fb_state', 'input_ioapic_write',
              'input_kbd_buf', 'input_kbd_used', 'input_kbd_drops',
              'input_mouse_x', 'input_mouse_y', 'input_mouse_b1',
              'input_test_kbd_count',
              'input_test_mouse_count', 'irq_sti_wait', 'irq_test_ticks',
              'cpu_com1_puts', 'cpu_com1_puthex']
    labels += list(DATA_LABELS)
    shifted = all(hi[n] == low[n] + delta for n in labels)
    rep.check('reloc.labels_shift_by_kernel_base', shifted)
    for name in CONSTANTS:
        rep.equal('reloc.%s_unchanged' % name, hi[name], low[name])

    rep.check('reloc.data_bytes_identical',
              hi.data[hi.at('data_start'):hi.at('data_end')] ==
              low.data[low.at('data_start'):low.at('data_end')])

    lo_body = low.data[low.at('code_start'):low.at('code_end')]
    hi_body = hi.data[hi.at('code_start'):hi.at('code_end')]
    if not rep.check('reloc.code_same_length', len(lo_body) == len(hi_body)):
        return
    allowed = set()
    windows = 0
    # mov rcx, imm64 (none today, kept for symmetry with the other gates)
    for p in find_all(lo_body, b'\x48\xB9'):
        if p + 10 > len(lo_body):
            continue
        lv = struct.unpack_from('<Q', lo_body, p + 2)[0]
        hv = struct.unpack_from('<Q', hi_body, p + 2)[0]
        if (low['code_start'] <= lv < low['data_end'] and
                hv == lv + delta):
            allowed.update(range(p + 2, p + 10))
            windows += 1
    # mov rcx, imm32: the counter/message addresses of the handshake
    for p in find_all(lo_body, b'\x48\xC7\xC1'):
        if p + 7 > len(lo_body):
            continue
        lv = struct.unpack_from('<I', lo_body, p + 3)[0]
        hv = struct.unpack_from('<I', hi_body, p + 3)[0]
        if (lv < low['data_end'] and
                (hv - lv) & 0xFFFFFFFF == delta & 0xFFFFFFFF):
            allowed.update(range(p + 3, p + 7))
            windows += 1
    diffs = [i for i in range(len(lo_body)) if lo_body[i] != hi_body[i]]
    rep.check('reloc.code_diffs_only_shifted_immediates',
              all(i in allowed for i in diffs),
              '%d stray difference bytes' % sum(1 for i in diffs if i not in allowed))
    rep.check('reloc.absolute_immediates_found', windows >= 2,
              '%d shifted mov rcx, imm windows' % windows)


# ----------------------------------------------------------------------
# negative fixtures
# ----------------------------------------------------------------------

FIXTURES = [
    # the pump runs with IF = 0: a second STI anywhere in the module
    # breaks the single-owner contract (source scan + byte decode)
    ('desktop-sti-leak', 'kernel/desktop/desktop.inc',
     '        ; 2. the menu / taskbar click operations\n'
     '        call    desktop_click_pump\n',
     '        sti\n'
     '        ; 2. the menu / taskbar click operations\n'
     '        call    desktop_click_pump\n', 'check'),
    # the LVT timer mask must stay set BEFORE the loop STI: unmasking
    # it would let ticks advance past the frozen 11
    ('timer-unmask-drift', 'kernel/desktop/desktop.inc',
     '        mov     dword [rax + DESKTOP_LAPIC_LVT_TIMER], DESKTOP_LVT_MASK\n',
     '        mov     dword [rax + DESKTOP_LAPIC_LVT_TIMER], 0\n', 'check'),
    # the OLD background must be restored before the NEW save: dropping
    # the restore blit would smear the cursor trail over the desktop
    ('cursor-restore-removed', 'kernel/desktop/cursor.inc',
     '        ; restore the background under the OLD position first\n'
     '        lea     rcx, [desktop_cursor_save]\n'
     '        mov     rdx, [desktop_cursor_x]\n'
     '        mov     r8, [desktop_cursor_y]\n'
     '        mov     r9d, DESKTOP_SPRITE_W\n'
     '        mov     dword [rsp+32], DESKTOP_SPRITE_H\n'
     '        call    video_blit_rect\n', '', 'check'),
    # Backspace must restore the glyph cell from the wallpaper source:
    # dropping the row blit would leave the old glyph on screen
    ('echo-backspace-removed', 'kernel/desktop/echo.inc',
     '        mov     dword [rsp+32], 1\n'
     '        call    video_blit_rect\n',
     '        mov     dword [rsp+32], 1\n', 'check'),
    # the sprite pattern is frozen byte for byte plus sha256
    ('sprite-data-drift', 'kernel/desktop/cursor.inc',
     '        db 0,0,0,0,0,0,0,0,0,1,1,0',
     '        db 0,0,0,0,0,0,0,0,0,1,0,0', 'check'),
    # the frozen keys count derives from the injection, the private
    # assertion fires on drift
    ('desktop-final-count-drift', 'kernel/desktop/api.inc',
     'DESKTOP_ST_KEYS_FINAL  = 2', 'DESKTOP_ST_KEYS_FINAL  = 3', 'assemble'),
    # the hotspot clamp keeps the whole sprite on screen: removing the
    # X clamp assembles fine, the byte gate must catch it
    ('cursor-clamp-x-removed', 'kernel/desktop/cursor.inc',
     '        cmp     r12, DESKTOP_CURSOR_XMAX\n'
     '        jbe     .x_ok\n'
     '        mov     r12, DESKTOP_CURSOR_XMAX\n', '', 'check'),
    # DESKTOP-v2: the wallpaper digest is frozen; a flipped sha qword
    # assembles fine, the gate must catch the drift
    ('wallpaper-sha-flip', 'kernel/desktop/wallpaper.inc',
     'DESKTOP_WALLPAPER_SHA0 = 0xF9F8A12E5406E04B',
     'DESKTOP_WALLPAPER_SHA0 = 0xF9F8A12E5406E04C', 'check'),
    # DESKTOP-v2: the menu hit rectangle is frozen at (0,32,200,34);
    # widening the click hit test assembles fine, the byte gate catches it
    ('menu-geometry-drift', 'kernel/desktop/menu.inc',
     '        cmp     r14, DESKTOP_MENU_W\n',
     '        cmp     r14, DESKTOP_MENU_W + 8\n', 'check'),
    # DESKTOP-v2: the frozen redraw order is restore OLD -> menu/taskbar
    # -> echo -> place NEW; swapping the two pumps assembles fine, the
    # byte order gate must catch it
    ('redraw-order-swap', 'kernel/desktop/desktop.inc',
     '        call    desktop_click_pump\n'
     '        or      r14d, eax\n'
     '        ; 3. the keyboard echo\n'
     '        call    desktop_echo_pump\n',
     '        call    desktop_echo_pump\n'
     '        call    desktop_click_pump\n'
     '        or      r14d, eax\n', 'check'),
    # task 125: the launch request must be consumed exactly once; a
    # missing flag clear would relaunch the app on every loop pass
    ('launch-flag-leak', 'kernel/desktop/taskbar.inc',
     '        mov     qword [desktop_app_launch_requested], 0\n',
     '', 'check'),
    # task 125: the failure cleanup must blit the save BEFORE the
    # place; swapping them re-saves a drawn sprite and smears ghosts
    ('cleanup-order-swap', 'kernel/desktop/taskbar.inc',
     '        call    video_blit_rect\n'
     '        mov     rcx, [desktop_cursor_x]\n'
     '        mov     rdx, [desktop_cursor_y]\n'
     '        call    desktop_cursor_place\n'
     '.out:\n'
     '        mov     eax, r12d\n',
     '        mov     rcx, [desktop_cursor_x]\n'
     '        mov     rdx, [desktop_cursor_y]\n'
     '        call    desktop_cursor_place\n'
     '        call    video_blit_rect\n'
     '.out:\n'
     '        mov     eax, r12d\n', 'check'),
    # task 125: the resident loop must consume the launch request; a
    # dropped launch pump call leaves the menu entry dead
    ('run-loop-launch-removed', 'kernel/desktop/desktop.inc',
     '        call    desktop_pump\n'
     '        ; task 125: consume a CHIP-8 launch request in the same IF = 0\n'
     '        ; window (click mode: publish the APP_RUNNING/APP_EXIT stages)\n'
     '        mov     ecx, 1\n'
     '        call    desktop_launch_pump\n'
     'if defined K64_RUNTIME\n'
     '        call    app_tasks_pump\n'
     '        call    app_window_flush\n'
     '        call    video_gpu_pump\n'
     'end if\n'
     '        jmp     .loop\n',
     '        call    desktop_pump\n'
     '        jmp     .loop\n', 'check'),
    # task 125: the running indicator geometry is frozen; the private
    # api assertion fires on drift
    ('run-indicator-drift', 'kernel/desktop/api.inc',
     'DESKTOP_RUN_X     = 992', 'DESKTOP_RUN_X     = 984', 'assemble'),
    # task 143 (DEFECT-1): re-introducing the task-125 menu_flag gate
    # leaves the start-press (stationary + menu CLOSED) path unrestored:
    # menu_open/place readbacks capture the live sprite again
    ('defect1-menu-gate-regression', 'kernel/desktop/desktop.inc',
     '        ; task 143 (DEFECT-1): the stationary path FALLS THROUGH to the\n'
     '        ; OLD restore as well. A start press with a stationary cursor\n'
     '        ; opens the menu: menu_open\'s readback and the place readback\n'
     '        ; below must never capture the live sprite (the task-125 gate\n'
     '        ; only covered the menu-CLOSE sister). Restoring first keeps\n'
     '        ; EVERY readback save sprite-free; the forced re-place is\n'
     '        ; pixel-neutral because the cursor is drawn last.\n',
     '        cmp     qword [desktop_menu_flag], 0\n'
     '        je      .restored\n', 'check'),
    # task 143: the pre-125 stationary shortcut (je .restored on equal
    # coordinates) skips the restore for BOTH menu sisters
    ('stationary-skip-restore', 'kernel/desktop/desktop.inc',
     '        cmp     r13, [desktop_cursor_y]\n'
     '        jne     .moved\n',
     '        cmp     r13, [desktop_cursor_y]\n'
     '        je      .restored\n', 'check'),
]


def run_fixtures(rep, root, out, fasmg, fasm2_dir):
    checker = root / 'tests' / 'stage2-desktop' / 'check.py'
    for name, rel, old, new, where in FIXTURES:
        with tempfile.TemporaryDirectory(prefix='stage2-desktop-%s-' % name) as tmp:
            tree = Path(tmp)
            (tree / 'kernel').mkdir(parents=True)
            (tree / 'tests').mkdir(parents=True)
            for mod in ('cpu', 'mm', 'irq', 'input', 'video', 'desktop'):
                shutil.copytree(root / 'kernel' / mod, tree / 'kernel' / mod)
            shutil.copytree(root / 'tests' / 'stage2-desktop',
                            tree / 'tests' / 'stage2-desktop')
            target = tree / rel
            text = target.read_text(encoding='utf-8')
            if text.count(old) != 1:
                rep.check('fixture.%s.single_site' % name, False,
                          '%d occurrences of %r' % (text.count(old), old))
                continue
            target.write_text(text.replace(old, new), encoding='utf-8')
            cmd = [sys.executable, str(checker), '--root', str(tree),
                   '--out', str(tree / 'build'), '--fasm2-dir', str(fasm2_dir),
                   '--no-fixtures']
            env = dict(os.environ)
            env['FASMG'] = str(fasmg)
            proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
            output = (proc.stdout or '') + (proc.stderr or '')
            failed = proc.returncode != 0
            if where == 'assemble':
                ok = failed and 'assembler' in output.lower()
            elif where == 'check':
                ok = failed and 'FAIL' in output and 'assembler' not in output.lower()
            else:
                ok = failed
            rep.check('fixture.%s.detected' % name, ok,
                      'exit %d, expected %s' % (proc.returncode, where))


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=None, help='repository root')
    parser.add_argument('--out', default=None, help='artifact directory')
    parser.add_argument('--fasm2-dir', default=None, help='fasm2 checkout')
    parser.add_argument('--no-fixtures', action='store_true')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[2]
    out = Path(args.out).resolve() if args.out else root / 'build' / 'stage2-desktop'
    out.mkdir(parents=True, exist_ok=True)

    asm_ = find_assembler(root, args.fasm2_dir)
    if asm_ is None:
        print('check.py: no fasm2 host found, set FASMG or --fasm2-dir', file=sys.stderr)
        return 2

    rep = Report()
    rep.check('assembler.available', True, str(asm_.fasmg))
    rep.check('assembler.include', (asm_.fasm2_dir / 'include' / 'fasm2.inc').is_file(),
              str(asm_.fasm2_dir))

    low_path = out / 'layout.bin'
    ok, text = asm_.run(root / 'tests/stage2-desktop/layout.asm', low_path)
    if not rep.check('assembler.probe', ok, text.strip()[-600:]):
        rep.print(True)
        return 1
    rep.check('assembler.probe_bytes', low_path.stat().st_size > 0,
              '%d bytes' % low_path.stat().st_size)
    rep.check('assembler.probe_quiet', 'error' not in text.lower(),
              text.strip()[-200:] or 'no diagnostics')

    img = load_image(low_path)
    check_manifest(rep, img)
    check_data(rep, img)
    check_sprite(rep, img)
    check_ownership(rep, img, root)
    check_init(rep, img)
    check_wait_count(rep, img)
    check_run(rep, img)
    check_pump(rep, img)
    check_cursor(rep, img)
    check_echo(rep, img)
    check_menu(rep, img)
    check_taskbar(rep, img)
    check_selftest(rep, img)
    check_wallpaper(rep, img, root)
    check_sources(rep, root)
    check_position_independence(rep, asm_, root, out, img)

    if not args.no_fixtures:
        run_fixtures(rep, root, out, asm_.fasmg, asm_.fasm2_dir)

    print('stage2-desktop: layout.bin %d bytes, code %d bytes, data %d bytes' %
          (len(img.data), img['code_end'] - img['code_start'],
           img['data_end'] - img['data_start']))
    return 0 if rep.print(args.verbose) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
