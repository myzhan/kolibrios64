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
    'B...........',
    'BWB.........',
    'BWWB........',
    'BWWWB.......',
    'BWWWWB......',
    'BWWWWWB.....',
    'BWWWWWWB....',
    'BWWWWWWWB...',
    'BWWWWWWWWB..',
    'BWWWWWWWWWB.',
    'BWWWWWWWWWWB',
    'BWWWWWWWWWWB',
    'BWWWWBBBBBB.',
    'BWWBWWB.....',
    'BWB.BWWB....',
    'BB...BWWB...',
    '......BWWB..',
    '.......BB...',
]
SPRITE = bytes({'.': 0, 'B': 1, 'W': 2}[ch]
               for row in SPRITE_ROWS for ch in row)
SPRITE_SHA256 = 'f46ce0e0921bb78e2ffc420f062d21575ce4ea1e62ff5e65cd12c81965497ad7'

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
}

FRAME_ENTER = bytes.fromhex('55 53 56 57 41 54 41 55 41 56 41 57 48 83 EC 28')
FRAME_LEAVE = bytes.fromhex('48 83 C4 28 41 5F 41 5E 41 5D 41 5C 5F 5E 5B 5D')

# the functions the module may call; used to tell a real STI opcode
# apart from an FB byte inside a call rel32 operand
CALL_TARGETS = [
    'init', 'wait_count', 'pump', 'run', 'cursor_place',
    'cursor_composite', 'cursor_sync', 'echo_pump', 'echo_key', 'selftest',
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


def real_sti_sites(img):
    """FB bytes in the code region that are NOT part of a call rel32
    operand targeting a known function: the real STI instructions."""
    at, end = img.at('code_start'), img.at('code_end')
    targets = {img.at(n) for n in CALL_TARGETS}
    covered = set()
    for s, t in call_sites(img, at, end):
        if t in targets:
            covered.update(range(s + 1, s + 5))
    return [at + p for p in find_all(img.data[at:end], b'\xFB')
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
    rep.equal('manifest.entry_count', len(M), 119)
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

    for name, off in DATA_LABELS.items():
        rep.equal('layout.%s_offset' % name,
                  img[name] - img['data_start'], off)

    lo, hi = img['code_start'], img['code_end']
    rep.check('layout.code_non_empty', hi > lo, '%#x..%#x' % (lo, hi))
    rep.equal('layout.api_emits_no_code', img['init'], lo)
    order = ['init', 'wait_count', 'pump', 'run', 'cursor_place',
             'cursor_composite', 'cursor_sync', 'echo_pump', 'echo_key',
             'selftest']
    chain = [img[name] for name in order]
    rep.check('layout.function_order', chain == sorted(chain) and
              all(lo <= v < hi for v in chain),
              'emission order follows the include order')
    rep.check('layout.constants_inside_code',
              lo <= img['sprite'] < img['sprite_end'] <= img['echo_pump'] <
              img['echo_key'] < img['title'] < img['title_end'] <= img['selftest'])
    rep.equal('layout.title_bytes', img['title_end'] - img['title'], TITLE_LEN + 1)
    rep.equal('layout.sprite_bytes', img['sprite_end'] - img['sprite'], SPRITE_BYTES)
    rep.equal('layout.selftest_after_title', img['selftest'], img['title_end'])
    rep.check('layout.code_before_data', hi <= img['data_start'])
    rep.check('layout.data_page_aligned',
              img['data_start'] % PAGE == 0 and img['data_end'] % PAGE == 0)
    rep.check('layout.data_size_page_multiple',
              (img['data_end'] - img['data_start']) % PAGE == 0 and
              img['data_end'] - img['data_start'] >= PAGE)
    rep.equal('layout.module_ends_with_data', img.at('data_end'), img.probe_at)
    for name in ('video_fill_rect', 'video_blit_rect', 'video_readback_rect',
                 'video_puts', 'video_fb_state', 'input_ioapic_write',
                 'input_kbd_buf', 'input_kbd_used', 'input_kbd_drops',
                 'input_mouse_x', 'input_mouse_y', 'input_test_kbd_count',
                 'input_test_mouse_count', 'irq_sti_wait', 'irq_test_ticks',
                 'cpu_com1_puts', 'cpu_com1_puthex'):
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
    at, end = region(img, 'code_start', 'code_end')
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
    rep.check('ownership.pump_no_interrupt_bytes',
              not any(b in d[pa:pe] for b in (b'\xFB', b'\xF4', b'\xFA')))
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
    if pumps:
        j = pumps[0] + 5
        ok = (d[j] == 0xEB and
              j + 2 + struct.unpack_from('<b', d, j + 1)[0] == sti)
        rep.check('run.jmp_back_to_sti', ok, 'the resident loop never exits')
    rep.check('run.never_returns', d.find(b'\xC3', at, end) < 0)


def check_pump(rep, img):
    at, end = region(img, 'pump', 'run')
    d = img.data
    rep.check('pump.full_frame', d[at:at + len(FRAME_ENTER)] == FRAME_ENTER)
    echoes = calls_to(img, at, end, 'echo_pump')
    syncs = calls_to(img, at, end, 'cursor_sync')
    rep.check('pump.echo_then_cursor',
              len(echoes) == 1 and len(syncs) == 1 and echoes[0] < syncs[0])
    rep.equal('pump.no_other_calls', len(call_sites(img, at, end)), 2)


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
    # the backspace erase: one glyph cell of the frozen background color
    rep.check('echo.erase_cell_width',
              d.find(b'\x41\xB8' + struct.pack('<I', FONT_W), at, end) >= 0)
    rep.check('echo.erase_cell_height',
              d.find(b'\x41\xB9' + struct.pack('<I', FONT_H), at, end) >= 0)
    rep.check('echo.erase_bg_color', d.find(stack_color(BG_COLOR), at, end) >= 0)
    rep.equal('echo.erase_fill_call', len(calls_to(img, at, end, 'video_fill_rect')), 1)


def check_selftest(rep, img):
    at, end = region(img, 'selftest', 'data_start')
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
    # leg 0: the full screen background fill
    pos = ordered_positions(img, at, end, [
        b'\x31\xC9\x31\xD2',
        b'\x41\xB8' + struct.pack('<I', SCREEN_W),
        b'\x41\xB9' + struct.pack('<I', SCREEN_H),
        stack_color(BG_COLOR),
    ])
    rep.check('selftest.bg_fill_args', pos is not None)
    # leg 1: the bar rectangle and the title text
    pos = ordered_positions(img, at, end, [
        mov_ecx(BAR_X), mov_edx(BAR_Y),
        b'\x41\xB8' + struct.pack('<I', BAR_W),
        b'\x41\xB9' + struct.pack('<I', BAR_H),
        stack_color(BAR_COLOR),
        mov_ecx(TITLE_X), mov_edx(TITLE_Y),
        b'\x41\xB8' + struct.pack('<I', TITLE_COLOR),
    ])
    rep.check('selftest.title_args', pos is not None)
    rep.equal('selftest.title_lea',
              len(rip_refs(img, at, end, b'\x4C\x8D', 0x0D, 0, 'title')), 1)
    fills = calls_to(img, at, end, 'video_fill_rect')
    puts = calls_to(img, at, end, 'video_puts')
    rep.equal('selftest.two_fills_one_puts', (len(fills), len(puts)), (2, 1))
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


def check_sources(rep, root):
    module = root / 'kernel' / 'desktop'
    files = sorted(p.name for p in module.glob('*'))
    rep.equal('source.file_set', files, [
        'api.inc', 'cursor.inc', 'data.inc', 'desktop.asm',
        'desktop.inc', 'echo.inc', 'selftest.inc'])

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
        'api.inc', 'desktop.inc', 'cursor.inc', 'echo.inc',
        'selftest.inc', 'data.inc'])


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
              'selftest', 'title', 'title_end', 'sprite', 'sprite_end',
              'msg_selftest', 'msg_nl',
              'video_fill_rect', 'video_blit_rect', 'video_readback_rect',
              'video_puts', 'video_fb_state', 'input_ioapic_write',
              'input_kbd_buf', 'input_kbd_used', 'input_kbd_drops',
              'input_mouse_x', 'input_mouse_y', 'input_test_kbd_count',
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
     '        call    desktop_echo_pump\n'
     '        call    desktop_cursor_sync\n',
     '        sti\n'
     '        call    desktop_echo_pump\n'
     '        call    desktop_cursor_sync\n', 'check'),
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
    # Backspace must erase the glyph cell with the background color:
    # dropping the fill would leave the old glyph on screen
    ('echo-backspace-removed', 'kernel/desktop/echo.inc',
     '        mov     r8d, DESKTOP_FONT_W\n'
     '        mov     r9d, DESKTOP_FONT_H\n'
     '        mov     dword [rsp+32], DESKTOP_BG_COLOR\n'
     '        call    video_fill_rect\n', '', 'check'),
    # the sprite pattern is frozen byte for byte plus sha256
    ('sprite-data-drift', 'kernel/desktop/cursor.inc',
     '        db 1,2,2,2,2,1,1,1,1,1,1,0',
     '        db 1,2,2,2,2,1,1,1,1,1,2,0', 'check'),
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
    check_selftest(rep, img)
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
