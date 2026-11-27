#!/usr/bin/env python3
"""Stage 2 APP module checks, task 115, contract APP-v1. No machine
is started.

Everything here is measured, nothing is taken from a comment. The probe
tests/stage2-app/layout.asm assembles kernel/app/app.asm exactly as the
kernel will include it (behind cpu/mm/irq/input/video/desktop/blk/fs and
fsys, whose seams it consumes) and appends a manifest of label
addresses. This script then

  * compares the manifest constants and every data label offset against
    the fixed values of contract APP-v1, repeated here independently of
    the assembly sources;
  * decodes the emitted bytes: the full register frames, the full-range
    user pointer validation, the four handlers 0..3 (EXIT park, BLIT
    geometry + bounce rows, READ_KEY queue/fabricated ESC, POLL wake
    split with the single sti pause-spin window and the RTE re-mask), the launch
    gates, the FS read of /PROGRAMS/CHIP8.BIN (never an embedded
    image), the 516 page footprint with the +3 table budget, the iretq
    frame, the handler registration/unregistration shape, the fd sweep,
    the frozen counter restore and the full teardown; every call target
    must stay inside the module or name a frozen seam, and every RIP
    relative write must land inside the app data region (or restore the
    two frozen sector counters);
  * replays the frozen semantics in pure Python models: the range
    validator matrix, the BLIT decision table, the READ_KEY queue and
    fabricated-exit rules, the POLL snapshot packing, the program size
    gate and the launch read chunking;
  * runs the chip8 reference model self test, then feeds the ROM bytes
    READ FROM THE APP DISK IMAGE through the model and compares the
    256-step IBM frame against the frozen probe expectations; the
    program bytes on the image must equal a fresh assembly of
    user/chip8/chip8.asm, and the three disk sha256 finals are frozen;
  * checks the data region is 22 real zero pages, page aligned at both
    ends, with the frame array, bounce page, echo save and the private
    syscall stack inside;
  * checks the module links at the kernel base with identical bytes
    except the absolute-address immediates, which shift by the base;
  * assembles the whole kernel to prove the contract copies and the
    core closing assertions still agree;
  * re-runs the whole pipeline on purpose-broken copies of the sources
    and requires every one of them to fail.

The contract values below are deliberately duplicated instead of
imported: if the module and this file drift apart, one of them has to be
fixed explicitly.

Usage: check.py [--root DIR] [--out DIR] [--fasm2-dir DIR] [--no-fixtures]
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

# ----------------------------------------------------------------------
# contract APP-v1, fixed here independently of the sources
# ----------------------------------------------------------------------

PAGE = 0x1000
KERNEL_BASE = 0xFFFFFFFF80000000

SYS_EXIT, SYS_BLIT, SYS_READ_KEY, SYS_POLL = 0, 1, 2, 3
SYS_CALL_MAX = 16

OK = 0
EINVAL = -1
ENOMEM = -2
ENOENT = -3
EPERM = -5
ENOTSUP = -6
ENOTREADY = -7
EALREADY = -8
EIO = -9
EFAULT = -11

VA_BASE = 0x100400000
VA_LIMIT = 0x100800000
CODE_BASE = 0x100400000
CODE_SIZE = 0x4000
DATA_BASE = 0x100600000
DATA_SIZE = 0x200000
STACK_TOP = 0x100800000
STACK_SIZE = 0x4000
# the mm contract places the stack at the TOP of the data domain, so
# the footprint is 4 code + 512 data (the data loop covers the stack
# tail), never 520: a separate stack map would hit EALREADY
CODE_PAGES, DATA_PAGES, STACK_PAGES = 4, 512, 4
LAUNCH_PAGES = 516
TABLE_BUDGET = 3
USER_CS, USER_SS, USER_RFLAGS = 0x33, 0x2B, 0x2

BLIT_DST_X, BLIT_DST_Y = 192, 224
FB_WIDTH, FB_HEIGHT = 640, 320
SCREEN_W, SCREEN_H = 1024, 768

STAGE_IDLE, STAGE_IBM_READY, STAGE_EXIT_REQ, STAGE_DONE = 0, 1, 2, 3
SC_ESC, SC_ENTER, ASCII_ESC = 0x01, 0x1C, 27
POLL_CHUNK = 0x2000000
# task 141 W1: the interactive POLL park is a bounded sti pause-spin
# window, ~12 ms per round, no HLT (a gdbstub write cannot wake it)
IPOLL_CHUNK = 0x40000

LAPIC_VA = 0xFFFFC00002000000
LAPIC_LVT_TIMER = 0x320
LVT_MASK = 0x10000
IOAPIC_IDX_KBD_LO, IOAPIC_IDX_MOUSE_LO = 0x12, 0x28
RTE_KBD_MASKED, RTE_MOUSE_MASKED = 0x10021, 0x1002C
RTE_KBD_LIVE, RTE_MOUSE_LIVE = 0x21, 0x2C

ECHO_SAVE_X, ECHO_SAVE_Y = 16, 64
ECHO_SAVE_W, ECHO_SAVE_H = 1008, 16
ECHO_SAVE_BYTES = ECHO_SAVE_W * ECHO_SAVE_H * 4
BOUNCE_ROW = FB_WIDTH * 4
PROGRAM_PATH_MAX = 24
PROGRAM_PATH = b'/PROGRAMS/CHIP8.BIN\x00'
CHIP8_SIZE = 3315

DATA_PAGES_TOTAL = 22
SYSCALL_STACK = 4096
DATA_REGION_SIZE = DATA_PAGES_TOTAL * PAGE

FRAME_RDI, FRAME_RSI, FRAME_RDX = 72, 80, 104
FRAME_R10, FRAME_R8, FRAME_RAX = 48, 64, 120
FRAME_VECTOR, FRAME_SIZE = 128, 184
MAP_US, MAP_RW, MAP_NX = 4, 2, 1 << 63
STATE_TABLE_ALLOCS = 40
STATE_TABLE_DROPS = 48
MM_ENOENT = -3
MAX_FDS, STAT_SIZE, FD_SIZE, FD_IN_USE = 8, 64, 64, 0

DESKTOP_BG_COLOR = 0x1A2B3C
DESKTOP_BAR_COLOR = 0x2C3E50
DESKTOP_BAR_W, DESKTOP_BAR_H = 1024, 32
DESKTOP_TITLE_X, DESKTOP_TITLE_Y = 8, 8
DESKTOP_TITLE_COLOR = 0xFFFFFF
VIDEO_FB_STATE_READY = 0

# the frozen disk finals: the two FS-v1 images stay untouched, the app
# image is a new independent freeze (task 115)
DISK_SHA = 'e9e83dad3f6d5193ac15c7a35834cc5190ca34e978b537d80be9fa9beb7bc42b'
DISK_BIG_SHA = 'a810280509cd05f67dce37dc46e67cbfeb2e9432b73a105108f9a2d298500543'
DISK_APP_SHA = 'a846e27f77057ab5a31c83dcc2894818e550d5bcc8876c3337ea07a5ac7090b3'

# the data layout, offsets from app_data_start
DATA_LABELS = {
    'ready': 0, 'stage': 8, 'exit_code': 16, 'fault_vector': 24,
    'blit_count': 32, 'key_count': 40, 'poll_count': 48,
    'launch_rsp': 56, 'launch_rip': 64, 'saved_rsp0': 72,
    'saved_fs_reads': 80, 'saved_blk_reads': 88, 'table_base': 96,
    'drops_base': 104, 'table_delta': 112, 'table_released': 120,
    'pages_held': 128, 'image_size': 136, 'kbd_consumed': 144,
    'interactive': 152, 'fd_swept': 160, 'echo_saved': 168,
    'resolve_rec': 176, 'stat_buf': 240,
    'pages': 4096, 'bounce': 12288, 'echo_save': 16384,
    'fs_snapshot': 81920,
    'syscall_stack': 86016, 'syscall_stack_top': 90112,
}

MAGIC = b'K64APP1'
MANIFEST_VERSION = 1

# manifest entry order, see tests/stage2-app/layout.asm
M = [
    'magic', 'version', 'count',
    'check_range', 'copy_from_user', 'h_exit', 'h_fault', 'h_blit',
    'h_read_key', 'h_poll', 'init', 'redraw_desktop', 'launch',
    'program_path', 'program_path_end', 'selftest',
    'data_start', 'data_end',
    'ready', 'stage', 'exit_code', 'fault_vector', 'blit_count',
    'key_count', 'poll_count', 'launch_rsp', 'launch_rip', 'saved_rsp0',
    'saved_fs_reads', 'saved_blk_reads', 'table_base', 'drops_base',
    'table_delta', 'table_released', 'pages_held', 'image_size',
    'kbd_consumed', 'interactive', 'fd_swept', 'echo_saved',
    'resolve_rec', 'stat_buf', 'pages', 'bounce', 'echo_save',
    'fs_snapshot', 'syscall_stack', 'syscall_stack_top',
    'fs_open', 'fs_read', 'fs_close', 'fs_stat', 'fs_fd_table',
    'fs_test_reads', 'blk_test_reads',
    'mm_vmm_resolve', 'vmm_map_user_page', 'vmm_unmap_user_page',
    'pmm_alloc_page', 'pmm_free_page', 'mm_vmm_state',
    'cpu_syscall_set_handler', 'cpu_user_fault_set_handler',
    'cpu_set_rsp0', 'cpu_tss_rsp0', 'cpu_syscall_handlers',
    'video_fill_rect', 'video_blit_rect', 'video_readback_rect',
    'video_puts', 'video_fb_state',
    'desktop_cursor_sync', 'desktop_cursor_place', 'desktop_repaint_base',
    'desktop_title',
    'desktop_state', 'desktop_echo_row', 'desktop_cursor_x',
    'desktop_cursor_y',
    'input_kbd_buf', 'input_kbd_used', 'input_mouse_x', 'input_mouse_y',
    'input_mouse_b1', 'input_ioapic_write',
    'irq_test_ticks', 'fsys_ready', 'fs_ready',
    'sys_exit', 'sys_blit', 'sys_read_key', 'sys_poll',
    'ok', 'einval', 'enomem', 'enoent', 'eperm', 'enotsup', 'enotready',
    'ealready', 'eio', 'efault',
    'va_base', 'va_limit', 'code_base', 'code_size', 'data_base',
    'data_size', 'stack_top', 'stack_size',
    'code_pages', 'data_pages', 'stack_pages', 'launch_pages',
    'table_budget',
    'user_cs', 'user_ss', 'user_rflags',
    'blit_dst_x', 'blit_dst_y', 'fb_width', 'fb_height',
    'screen_w', 'screen_h',
    'stage_idle', 'stage_ibm_ready', 'stage_exit_req', 'stage_done',
    'sc_esc', 'sc_enter', 'ascii_esc',
    'poll_chunk', 'ipoll_chunk',
    'lapic_va', 'lapic_lvt_timer', 'lvt_mask', 'ioapic_idx_kbd_lo',
    'ioapic_idx_mouse_lo', 'rte_kbd_masked', 'rte_mouse_masked',
    'rte_kbd_live', 'rte_mouse_live',
    'echo_save_x', 'echo_save_y', 'echo_save_w', 'echo_save_h',
    'echo_save_bytes',
    'bounce_row', 'program_path_max',
    'data_pages_total', 'syscall_stack_size',
    'cpu_sys_call_max', 'frame_rdi', 'frame_rsi', 'frame_rdx',
    'frame_r10', 'frame_r8', 'frame_rax', 'frame_vector', 'frame_size',
    'map_us', 'map_rw', 'map_nx', 'mm_table_budget', 'state_table_allocs',
    'state_table_drops', 'mm_enoent',
    'max_fds', 'stat_size', 'fd_size', 'fd_in_use',
    'desktop_bg_color', 'desktop_bar_color', 'desktop_bar_w',
    'desktop_bar_h', 'desktop_title_x', 'desktop_title_y',
    'desktop_title_color', 'video_fb_state_ready',
    'kernel_base',
    'input_kbd_head', 'desktop_kbd_consumed',
]

CONSTANTS = {
    'sys_exit': SYS_EXIT, 'sys_blit': SYS_BLIT,
    'sys_read_key': SYS_READ_KEY, 'sys_poll': SYS_POLL,
    'ok': OK, 'einval': EINVAL, 'enomem': ENOMEM, 'enoent': ENOENT,
    'eperm': EPERM, 'enotsup': ENOTSUP, 'enotready': ENOTREADY,
    'ealready': EALREADY, 'eio': EIO, 'efault': EFAULT,
    'va_base': VA_BASE, 'va_limit': VA_LIMIT, 'code_base': CODE_BASE,
    'code_size': CODE_SIZE, 'data_base': DATA_BASE, 'data_size': DATA_SIZE,
    'stack_top': STACK_TOP, 'stack_size': STACK_SIZE,
    'code_pages': CODE_PAGES, 'data_pages': DATA_PAGES,
    'stack_pages': STACK_PAGES, 'launch_pages': LAUNCH_PAGES,
    'table_budget': TABLE_BUDGET,
    'user_cs': USER_CS, 'user_ss': USER_SS, 'user_rflags': USER_RFLAGS,
    'blit_dst_x': BLIT_DST_X, 'blit_dst_y': BLIT_DST_Y,
    'fb_width': FB_WIDTH, 'fb_height': FB_HEIGHT,
    'screen_w': SCREEN_W, 'screen_h': SCREEN_H,
    'stage_idle': STAGE_IDLE, 'stage_ibm_ready': STAGE_IBM_READY,
    'stage_exit_req': STAGE_EXIT_REQ, 'stage_done': STAGE_DONE,
    'sc_esc': SC_ESC, 'sc_enter': SC_ENTER, 'ascii_esc': ASCII_ESC,
    'poll_chunk': POLL_CHUNK, 'ipoll_chunk': IPOLL_CHUNK,
    'lapic_va': LAPIC_VA, 'lapic_lvt_timer': LAPIC_LVT_TIMER,
    'lvt_mask': LVT_MASK, 'ioapic_idx_kbd_lo': IOAPIC_IDX_KBD_LO,
    'ioapic_idx_mouse_lo': IOAPIC_IDX_MOUSE_LO,
    'rte_kbd_masked': RTE_KBD_MASKED, 'rte_mouse_masked': RTE_MOUSE_MASKED,
    'rte_kbd_live': RTE_KBD_LIVE, 'rte_mouse_live': RTE_MOUSE_LIVE,
    'echo_save_x': ECHO_SAVE_X, 'echo_save_y': ECHO_SAVE_Y,
    'echo_save_w': ECHO_SAVE_W, 'echo_save_h': ECHO_SAVE_H,
    'echo_save_bytes': ECHO_SAVE_BYTES,
    'bounce_row': BOUNCE_ROW, 'program_path_max': PROGRAM_PATH_MAX,
    'data_pages_total': DATA_PAGES_TOTAL,
    'syscall_stack_size': SYSCALL_STACK,
    'cpu_sys_call_max': SYS_CALL_MAX, 'frame_rdi': FRAME_RDI,
    'frame_rsi': FRAME_RSI, 'frame_rdx': FRAME_RDX, 'frame_r10': FRAME_R10,
    'frame_r8': FRAME_R8, 'frame_rax': FRAME_RAX,
    'frame_vector': FRAME_VECTOR, 'frame_size': FRAME_SIZE,
    'map_us': MAP_US, 'map_rw': MAP_RW, 'map_nx': MAP_NX,
    'mm_table_budget': TABLE_BUDGET, 'state_table_allocs': STATE_TABLE_ALLOCS,
    'state_table_drops': STATE_TABLE_DROPS, 'mm_enoent': MM_ENOENT,
    'max_fds': MAX_FDS, 'stat_size': STAT_SIZE, 'fd_size': FD_SIZE,
    'fd_in_use': FD_IN_USE,
    'desktop_bg_color': DESKTOP_BG_COLOR, 'desktop_bar_color': DESKTOP_BAR_COLOR,
    'desktop_bar_w': DESKTOP_BAR_W, 'desktop_bar_h': DESKTOP_BAR_H,
    'desktop_title_x': DESKTOP_TITLE_X, 'desktop_title_y': DESKTOP_TITLE_Y,
    'desktop_title_color': DESKTOP_TITLE_COLOR,
    'video_fb_state_ready': VIDEO_FB_STATE_READY,
    'kernel_base': KERNEL_BASE,
}

APP_ENTER = bytes.fromhex('55 53 56 57 41 54 41 55 41 56 41 57 48 83 EC 28')
APP_LEAVE = bytes.fromhex('48 83 C4 28 41 5F 41 5E 41 5D 41 5C 5F 5E 5B 5D')
MOVSXD_RAX_EAX = bytes.fromhex('48 63 C0')
RET = 0xC3

# ----------------------------------------------------------------------
# tiny binary helpers
# ----------------------------------------------------------------------


def u16(data, off):
    return struct.unpack_from('<H', data, off)[0]


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


def call_sites(img, at, end, opcode=b'\xE8'):
    """Every E8 rel32 in a region whose target lands inside the image."""
    body = img.data[at:end]
    out = []
    for p in find_all(body, opcode):
        if p + 5 <= len(body):
            rel = struct.unpack_from('<i', body, p + 1)[0]
            target = at + p + 5 + rel
            if 0 <= target < len(img.data):
                out.append((at + p, target))
    return out


def rip_sites(img, at, end, prefix, modrm, tail):
    """Sites of 'prefix modrm disp32 [tail bytes]': (site, target, imm)."""
    body = img.data[at:end]
    out = []
    for p in find_all(body, prefix):
        m = p + len(prefix)
        if m + 5 + tail > len(body) or body[m] != modrm:
            continue
        disp = struct.unpack_from('<i', body, m + 1)[0]
        out.append((at + p, at + m + 5 + tail + disp, body[m + 5:m + 5 + tail]))
    return out


def rip_refs(img, at, end, prefix, modrm, tail, label, off=0):
    return [s for s, t, _ in rip_sites(img, at, end, prefix, modrm, tail)
            if t == img.at(label) + off]


def ordered_positions(img, at, end, needles):
    """Find each needle strictly after the previous one; None if absent."""
    body = img.data[at:end]
    out, cur = [], 0
    for needle in needles:
        p = body.find(needle, cur)
        if p < 0:
            return None
        out.append(at + p)
        cur = p + len(needle)
    return out


def imm64(reg_prefix, value):
    return reg_prefix + struct.pack('<Q', value)


def abs32(prefix, label_addr):
    return prefix + struct.pack('<I', label_addr & 0xFFFFFFFF)


def jne_at(d, pos):
    return d[pos] == 0x75 or d[pos:pos + 2] == b'\x0F\x85'


def je_at(d, pos):
    return d[pos] == 0x74 or d[pos:pos + 2] == b'\x0F\x84'


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

    def equal(self, name, got, want, detail=None):
        if detail is None:
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
        print('stage2-app: %d checks, %d failed' % (total, len(self.failures)))
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
            cmd += ['-i', 'APP_PROBE_ORG=%#x' % org]
        cmd += [str(source), str(output)]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
        text = (proc.stdout or '') + (proc.stderr or '')
        return proc.returncode == 0, text


def find_assembler(root, explicit_fasm2):
    """Locate fasmg and the fasm2 include directory."""
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
# static checks
# ----------------------------------------------------------------------


def check_manifest(rep, img):
    rep.check('manifest.magic', True, MAGIC.decode())
    rep.equal('manifest.version', img['version'], MANIFEST_VERSION)
    rep.equal('manifest.entry_count', len(M), 182)
    rep.equal('manifest.count_field', img['count'], 182)
    for name, want in CONSTANTS.items():
        rep.equal('manifest.%s' % name, img[name], want & 0xFFFFFFFFFFFFFFFF)

    for name, off in DATA_LABELS.items():
        rep.equal('layout.%s_offset' % name,
                  img[name] - img['data_start'], off)

    lo, hi = img['check_range'], img['data_start']
    rep.check('layout.code_non_empty', hi > lo, '%#x..%#x' % (lo, hi))
    order = ['check_range', 'copy_from_user', 'h_exit', 'h_fault', 'h_blit',
             'h_read_key', 'h_poll', 'init', 'redraw_desktop', 'launch',
             'program_path', 'selftest']
    chain = [img[name] for name in order]
    rep.check('layout.function_order', chain == sorted(chain) and
              all(lo <= v < hi for v in chain),
              'emission order: safety, handlers, init, redraw, launch')
    rep.check('layout.code_before_data', hi <= img['data_start'])
    rep.check('layout.data_page_aligned',
              img['data_start'] % PAGE == 0 and img['data_end'] % PAGE == 0)
    rep.equal('layout.data_size', img['data_end'] - img['data_start'],
              DATA_REGION_SIZE)
    rep.equal('layout.module_ends_with_data', img.at('data_end'), img.probe_at)
    for name in M[48:87]:
        rep.check('layout.%s_before_module' % name, 0 < img[name] < lo,
                  '%#x' % img[name])
    rep.equal('layout.path_bytes', img['program_path_end'] - img['program_path'],
              len(PROGRAM_PATH))
    rep.check('layout.path_budget',
              len(PROGRAM_PATH) <= PROGRAM_PATH_MAX)
    rep.equal('layout.pages_array', img['bounce'] - img['pages'], 2 * PAGE)
    rep.equal('layout.bounce_is_page_three',
              img['bounce'] - img['data_start'], 3 * PAGE)
    rep.equal('layout.echo_save_is_page_four',
              img['echo_save'] - img['data_start'], 4 * PAGE)
    rep.equal('layout.echo_save_budget',
              img['syscall_stack'] - img['echo_save'], 17 * PAGE)
    rep.equal('layout.snapshot_is_page_twenty',
              img['fs_snapshot'] - img['data_start'], 20 * PAGE)
    rep.equal('layout.stack_is_last_page',
              img['syscall_stack'] - img['data_start'], 21 * PAGE)
    rep.equal('layout.stack_top_closes_data',
              img['syscall_stack_top'], img['data_end'])
    rep.equal('layout.stat_buf_budget', img['pages'] - img['stat_buf'],
              PAGE - 240)
    # the frozen 516 split: the stack is the data domain tail
    rep.equal('layout.launch_pages_split', LAUNCH_PAGES,
              CODE_PAGES + DATA_PAGES)
    rep.equal('layout.stack_inside_data', STACK_TOP, DATA_BASE + DATA_SIZE)


def check_data(rep, img, root, fresh_chip8):
    d = img.data
    start, end = img.at('data_start'), img.at('data_end')
    block = d[start:end]
    rep.check('data.all_zero_real_bytes', block == bytes(DATA_REGION_SIZE),
              '%d non-zero bytes' % sum(1 for b in block if b))
    # the program path string, byte for byte
    a, b = img.at('program_path'), img.at('program_path_end')
    rep.check('data.program_path_bytes', d[a:b] == PROGRAM_PATH,
              repr(d[a:b]))
    # the user ruling: the program is loaded through the FS, never an
    # embedded image; neither the fresh chip8 bytes nor the ROM bytes
    # may appear anywhere in the module
    module = d[img.at('check_range'):end]
    rep.check('data.no_embedded_program', fresh_chip8 not in module,
              '%d fresh chip8 bytes' % len(fresh_chip8))
    rom = (root / 'user' / 'chip8' / 'roms' / 'ibm.ch8').read_bytes()
    rep.check('data.no_embedded_rom', rom not in module,
              'the frozen ibm.ch8 bytes must stay on the disk image')
    rep.check('data.program_fits_code_domain',
              0 < len(fresh_chip8) <= CODE_SIZE,
              '%d bytes' % len(fresh_chip8))


def check_frames(rep, img):
    d = img.data
    framed = ['check_range', 'copy_from_user', 'h_blit', 'h_read_key',
              'h_poll', 'init', 'redraw_desktop', 'launch', 'selftest']
    nxt = {'check_range': 'copy_from_user', 'copy_from_user': 'h_exit',
           'h_blit': 'h_read_key', 'h_read_key': 'h_poll', 'h_poll': 'init',
           'init': 'redraw_desktop', 'redraw_desktop': 'launch',
           'launch': 'program_path', 'selftest': 'data_start'}
    for name in framed:
        at = img.at(name)
        rep.check('frame.%s_full_frame' % name,
                  d[at:at + len(APP_ENTER)] == APP_ENTER)
        end = img.at(nxt[name])
        rep.check('frame.%s_leave_ret' % name,
                  d.find(APP_LEAVE + bytes((RET,)), at, end) >= 0)
    # EXIT and the fault seam are leaves that never return
    for name in ('h_exit', 'h_fault'):
        at = img.at(name)
        rep.check('frame.%s_is_leaf' % name,
                  d[at:at + len(APP_ENTER)] != APP_ENTER)
    # the errno returning functions sign extend on the error tail
    for name in ('h_blit', 'init', 'launch', 'selftest'):
        at, end = region(img, name, nxt[name])
        rep.check('frame.%s_movsxd_error_tail' % name,
                  d.find(MOVSXD_RAX_EAX + APP_LEAVE, at, end) >= 0)


def check_check_range(rep, img):
    at, end = region(img, 'check_range', 'copy_from_user')
    d = img.data
    body = d[at:end]
    rep.check('range.zero_length_passes',
              bytes.fromhex('48 85 D2 0F 84') in body, 'test rdx, rdx; jz .ok')
    rep.check('range.negative_length_faults',
              bytes.fromhex('0F 88') in body, 'js .fault')
    rep.check('range.overflow_faults',
              bytes.fromhex('49 01 D5 72') in body, 'add r13, rdx; jc .fault')
    rep.check('range.domain_base_compare',
              (imm64(b'\x48\xB8', VA_BASE) + bytes.fromhex('49 39 C4 72'))
              in body, 'mov rax, VA_BASE; cmp r12, rax; jb .fault')
    rep.check('range.domain_limit_compare',
              (imm64(b'\x48\xB8', VA_LIMIT) + bytes.fromhex('49 39 C5 77'))
              in body, 'mov rax, VA_LIMIT; cmp r13, rax; ja .fault')
    calls = [t for _, t in call_sites(img, at, end)]
    rep.equal('range.only_resolves', calls, [img.at('mm_vmm_resolve')])
    rec = img.at('resolve_rec')
    sites = rip_sites(img, at, end, b'\x48\x81', 0x3D, 4)
    rep.check('range.demands_4k_leaves',
              any(t == rec + 8 and imm == struct.pack('<I', PAGE) and
                  jne_at(d, s + 11) for s, t, imm in sites),
              'cmp qword [rec+8], 0x1000; jne .fault')
    us = rip_sites(img, at, end, b'\x48\x83', 0x3D, 1)
    rep.check('range.demands_user_pages',
              any(t == rec + 24 and imm == b'\x01' and
                  jne_at(d, s + 8) for s, t, imm in us),
              'cmp qword [rec+24], 1; jne .fault')
    rep.check('range.write_targets_demand_rw',
              any(t == rec + 16 and imm == b'\x01' and
                  jne_at(d, s + 8) for s, t, imm in us),
              'cmp qword [rec+16], 1; jne .fault')
    rep.check('range.write_flag_gates_rw',
              bytes.fromhex('45 85 F6 74') in body, 'test r14d, r14d; jz')
    rep.check('range.page_walk_advance',
              bytes.fromhex('4C 89 E0 48 25 00 F0 FF FF 48 05 00 10 00 00 49 89 C4')
              in body, 'cursor = (cursor & -4096) + 4096')
    rep.check('range.fault_is_efault',
              (b'\xB8' + struct.pack('<i', EFAULT)) in body)


def check_copy_from_user(rep, img):
    at, end = region(img, 'copy_from_user', 'h_exit')
    d = img.data
    body = d[at:end]
    rep.check('copy.forward_direction', body[len(APP_ENTER)] == 0xFC, 'cld')
    calls = [t for _, t in call_sites(img, at, end)]
    rep.equal('copy.only_resolves', calls, [img.at('mm_vmm_resolve')])
    rec = img.at('resolve_rec')
    sites = rip_sites(img, at, end, b'\x48\x81', 0x3D, 4)
    rep.check('copy.demands_4k_leaves',
              any(t == rec + 8 and imm == struct.pack('<I', PAGE) and
                  jne_at(d, s + 11) for s, t, imm in sites))
    us = rip_sites(img, at, end, b'\x48\x83', 0x3D, 1)
    rep.check('copy.demands_user_pages',
              any(t == rec + 24 and imm == b'\x01' and
                  jne_at(d, s + 8) for s, t, imm in us))
    rep.check('copy.page_offset_and_room',
              bytes.fromhex('4C 89 E7 81 E7 FF 0F 00 00 48 C7 C3 00 10 00 00 48 29 FB')
              in body, 'offset = src & 0xFFF; room = 4096 - offset')
    rep.equal('copy.leaf_address_load',
              len(rip_refs(img, at, end, b'\x48\x8B', 0x35, 0,
                           'resolve_rec', 48)), 1,
              'mov rsi, [app_resolve_rec+48]')
    rep.check('copy.no_double_page_offset',
              bytes.fromhex('48 03 35') not in body,
              'rec+48 already carries the page offset')
    rep.check('copy.chunk_is_min_room_left',
              bytes.fromhex('4C 39 F3 49 0F 47 DE') in body,
              'cmp rbx, r14; cmova rbx, r14')
    rep.check('copy.rep_movsb', bytes.fromhex('F3 A4') in body)
    rep.check('copy.cursor_updates',
              bytes.fromhex('49 01 DC 49 01 DD 49 29 DE') in body)
    rep.check('copy.bad_page_is_efault',
              (b'\xB8' + struct.pack('<i', EFAULT)) in body)


def check_exit_and_fault(rep, img):
    d = img.data
    # EXIT: load the code from the frame, park, restore, jump; 24 bytes
    at, end = region(img, 'h_exit', 'h_fault')
    body = d[at:end]
    rep.check('exit.code_from_frame',
              body.startswith(bytes.fromhex('48 8B 41 48')),
              'mov rax, [rcx+72]')
    rep.equal('exit.parks_the_code',
              len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0, 'exit_code')), 1)
    rep.equal('exit.restores_the_kernel_stack',
              len(rip_refs(img, at, end, b'\x48\x8B', 0x25, 0, 'launch_rsp')), 1)
    rep.equal('exit.jumps_to_the_parked_rip',
              len(rip_refs(img, at, end, b'\xFF', 0x25, 0, 'launch_rip')), 1)
    rep.equal('exit.body_size', len(body), 24)
    rep.check('exit.never_returns',
              body.startswith(bytes.fromhex('48 8B 41 48 48 89 05')) and
              body[11:14] == bytes.fromhex('48 8B 25') and
              body[18:20] == bytes.fromhex('FF 25'),
              'load / park / mov rsp / jmp [rip], nothing else')
    # the fault seam: vector + 1 parked, same park discipline, 30 bytes
    at, end = region(img, 'h_fault', 'h_blit')
    body = d[at:end]
    rep.check('fault.vector_from_frame',
              body.startswith(bytes.fromhex('48 8B 81 80 00 00 00')),
              'mov rax, [rcx+128]')
    rep.check('fault.vector_plus_one',
              bytes.fromhex('48 FF C0') in body, 'inc rax, 0 stays none')
    rep.equal('fault.parks_the_vector',
              len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0,
                           'fault_vector')), 1)
    rep.equal('fault.restores_the_kernel_stack',
              len(rip_refs(img, at, end, b'\x48\x8B', 0x25, 0, 'launch_rsp')), 1)
    rep.equal('fault.jumps_to_the_parked_rip',
              len(rip_refs(img, at, end, b'\xFF', 0x25, 0, 'launch_rip')), 1)
    rep.equal('fault.body_size', len(body), 30)


def check_blit(rep, img):
    at, end = region(img, 'h_blit', 'h_read_key')
    d = img.data
    body = d[at:end]
    rep.check('blit.frame_in_r12', bytes.fromhex('49 89 CC') in body)
    rep.check('blit.frame_arguments',
              bytes.fromhex('4D 8B 6C 24 48') in body and
              bytes.fromhex('45 8B 74 24 50') in body and
              bytes.fromhex('45 8B 7C 24 68') in body and
              bytes.fromhex('41 8B 5C 24 30') in body and
              bytes.fromhex('41 8B 6C 24 40') in body,
              'src=[r12+72], x=[r12+80], y=[r12+104], w=[r12+48], h=[r12+64]')
    rep.check('blit.zero_dimensions_refused',
              bytes.fromhex('85 DB') in body and
              bytes.fromhex('85 ED') in body, 'test ebx, ebx; test ebp, ebp')
    rep.check('blit.screen_fit_x',
              (b'\x3D' + struct.pack('<I', SCREEN_W)) in body,
              'cmp eax, 1024')
    rep.check('blit.screen_fit_y',
              (b'\x3D' + struct.pack('<I', SCREEN_H)) in body,
              'cmp eax, 768')
    rep.check('blit.carry_guards',
              body.count(bytes.fromhex('0F 82')) +
              body.count(b'\x72') >= 2, 'jc .einval on both sums')
    rep.check('blit.source_byte_count',
              bytes.fromhex('0F AF C5 48 C1 E0 02') in body,
              'imul eax, ebp; shl rax, 2: w * h * 4')
    rep.check('blit.read_only_validation',
              bytes.fromhex('45 31 C0') in body, 'xor r8d, r8d, write = 0')
    calls = [t for _, t in call_sites(img, at, end)]
    rep.equal('blit.call_chain', calls,
              [img.at('check_range'), img.at('copy_from_user'),
               img.at('video_blit_rect')])
    # the range verdict must abort the copy: test eax, eax; jnz .out
    rng = [s for s, t in call_sites(img, at, end)
           if t == img.at('check_range')]
    ok = False
    if rng:
        p = rng[0] + 5 - at
        ok = (body[p:p + 2] == bytes.fromhex('85 C0') and
              jne_at(body, p + 2))
    rep.check('blit.range_fault_aborts', ok,
              'a bad source pointer never reaches the bounce copy')
    rep.check('blit.row_through_the_bounce_page',
              abs32(bytes.fromhex('48 C7 C2'), img['bounce']) in body,
              'mov rdx, app_bounce')
    rep.check('blit.row_width_bytes',
              bytes.fromhex('49 C1 E0 02') in body, 'shl r8, 2: w * 4')
    rep.check('blit.one_row_per_call',
              bytes.fromhex('C7 44 24 20 01 00 00 00') in body,
              'mov dword [rsp+32], 1: h = 1')
    rep.check('blit.row_cursor_advance',
              bytes.fromhex('48 C1 E0 02') in body and
              bytes.fromhex('49 01 C5') in body,
              'src += w * 4 after every delivered row')
    # the first successful BLIT publishes IBM_READY for the host
    first = [(t, imm) for _, t, imm in
             rip_sites(img, at, end, b'\x48\x83', 0x3D, 1)
             if t == img.at('blit_count') and imm == b'\x01']
    rep.check('blit.first_success_compare', len(first) == 1,
              'cmp qword [app_blit_count], 1')
    armed = [(t, imm) for _, t, imm in
             rip_sites(img, at, end, b'\x48\xC7', 0x05, 4)
             if t == img.at('stage')]
    rep.check('blit.publishes_ibm_ready',
              armed == [(img.at('stage'), struct.pack('<I', STAGE_IBM_READY))])
    rep.check('blit.einval', (b'\xB8' + struct.pack('<i', EINVAL)) in body)


def check_read_key(rep, img):
    at, end = region(img, 'h_read_key', 'h_poll')
    d = img.data
    body = d[at:end]
    rep.check('read_key.frame_in_r12', bytes.fromhex('49 89 CC') in body)
    rep.equal('read_key.consumed_cursor_load',
              len(rip_refs(img, at, end, b'\x48\x8B', 0x05, 0,
                           'kbd_consumed')), 1)
    rep.equal('read_key.queue_fill_compare',
              len(rip_refs(img, at, end, b'\x48\x3B', 0x05, 0,
                           'input_kbd_used')), 1,
              'cmp rax, [input_kbd_used]; jae .empty')
    rep.check('read_key.sixteen_byte_entries',
              bytes.fromhex('48 C1 E0 04') in body, 'shl rax, 4')
    rep.equal('read_key.queue_base',
              len(rip_refs(img, at, end, b'\x48\x8D', 0x0D, 0,
                           'input_kbd_buf')), 1)
    rep.check('read_key.ascii_and_raw_load',
              bytes.fromhex('48 8B 11') in body and
              bytes.fromhex('4C 8B 41 08') in body,
              'mov rdx, [rcx]; mov r8, [rcx+8]')
    rep.check('read_key.enter_flips_interactive',
              bytes.fromhex('49 83 F8 1C') in body, 'cmp r8, APP_SC_ENTER')
    armed = [(t, imm) for _, t, imm in
             rip_sites(img, at, end, b'\x48\xC7', 0x05, 4)
             if t == img.at('interactive')]
    rep.check('read_key.interactive_armed_once',
              armed == [(img.at('interactive'), struct.pack('<I', 1))])
    rep.check('read_key.raw_to_frame_rdx',
              bytes.fromhex('4D 89 44 24 68') in body,
              'mov [r12+104], r8')
    rep.check('read_key.ascii_to_rax',
              bytes.fromhex('48 89 D0') in body, 'mov rax, rdx')
    # the fabricated ESC: only when the queue is empty AND EXIT_REQ
    fab = [(s, t, imm) for s, t, imm in
           rip_sites(img, at, end, b'\x48\x83', 0x3D, 1)
           if t == img.at('stage') and imm == bytes((STAGE_EXIT_REQ,))]
    rep.check('read_key.exit_req_gate', len(fab) == 1,
              'cmp qword [app_stage], APP_STAGE_EXIT_REQ')
    rep.check('read_key.fabricated_raw_esc',
              bytes.fromhex('49 C7 44 24 68 01 00 00 00') in body,
              'mov qword [r12+104], APP_SC_ESC')
    rep.check('read_key.fabricated_ascii_esc',
              (b'\xB8' + struct.pack('<I', ASCII_ESC)) in body,
              'mov eax, 27')
    # the fabricated key never advances the consumed cursor or the
    # counter: every inc site sits BEFORE the EXIT_REQ compare
    incs = [s for s, t, _ in rip_sites(img, at, end, b'\x48\xFF', 0x05, 0)
            if t in (img.at('kbd_consumed'), img.at('key_count'))]
    rep.check('read_key.two_real_key_counters', len(incs) == 2,
              'inc consumed + inc key_count on the real path only')
    rep.check('read_key.fabrication_never_advances',
              bool(fab) and all(s < fab[0][0] for s in incs),
              'no counter inc behind the EXIT_REQ gate')
    rep.check('read_key.empty_is_zero',
              bytes.fromhex('49 C7 44 24 68 00 00 00 00') in body and
              bytes.fromhex('31 C0') in body,
              'raw 0 / ascii 0 when the queue is empty')


def check_poll(rep, img):
    at, end = region(img, 'h_poll', 'init')
    d = img.data
    body = d[at:end]
    calls = [t for _, t in call_sites(img, at, end)]
    rep.equal('poll.call_chain', calls,
              [img.at('desktop_cursor_sync')] +
              [img.at('input_ioapic_write')] * 4,
              'cursor sync first, then the four RTE writes')
    rep.equal('poll.counts_every_call',
              len(rip_refs(img, at, end, b'\x48\xFF', 0x05, 0,
                           'poll_count')), 1)
    wake = [(s, t, imm) for s, t, imm in
            rip_sites(img, at, end, b'\x48\x83', 0x3D, 1)
            if t == img.at('interactive') and imm == b'\x00']
    rep.check('poll.wake_split_gate', len(wake) == 1,
              'cmp qword [app_interactive], 0; jne .interactive')
    rep.check('poll.ibm_pause_window',
              (b'\xB9' + struct.pack('<I', POLL_CHUNK)) in body and
              bytes.fromhex('F3 90') in body,
              'mov ecx, APP_POLL_CHUNK; pause, IF stays 0')
    # the LVT timer is masked before the window, never unmasked
    rep.check('poll.lvt_mask_write',
              imm64(b'\x48\xB8', LAPIC_VA) +
              bytes.fromhex('C7 80 20 03 00 00 00 00 01 00') in body,
              'mov dword [rax+0x320], 0x10000')
    # task 141 W1: the interactive park is a bounded sti pause-spin
    # window (sti; mov ecx, APP_IPOLL_CHUNK; pause; dec ecx; jnz; cli),
    # never HLT: a gdbstub key injection cannot wake HLT and the masked
    # LVT leaves no other wake source
    spin = (b'\xFB\xB9' + struct.pack('<I', IPOLL_CHUNK) +
            bytes.fromhex('F3 90 FF C9 75 FA FA'))
    window = find_all(body, spin)
    rep.equal('poll.one_sti_spin_cli', len(window), 1,
              'the task 141 interactive POLL semantics')
    live_kbd = b'\xBA' + struct.pack('<I', RTE_KBD_LIVE)
    live_mouse = b'\xBA' + struct.pack('<I', RTE_MOUSE_LIVE)
    masked_kbd = b'\xBA' + struct.pack('<I', RTE_KBD_MASKED)
    masked_mouse = b'\xBA' + struct.pack('<I', RTE_MOUSE_MASKED)
    rep.check('poll.rtes_live_inside_window',
              live_kbd in body and live_mouse in body and
              bool(window) and
              body.find(live_kbd) < window[0] and
              body.find(live_mouse) < window[0])
    rep.check('poll.rtes_remasked_after_window',
              masked_kbd in body and masked_mouse in body and
              bool(window) and
              body.find(masked_kbd) > window[0] and
              body.find(masked_mouse) > window[0],
              'irq_test_ticks stays frozen at 11')
    rep.check('poll.ioapic_indices',
              (b'\xB9' + struct.pack('<I', IOAPIC_IDX_KBD_LO)) in body and
              (b'\xB9' + struct.pack('<I', IOAPIC_IDX_MOUSE_LO)) in body)
    # the snapshot: x | y<<16, buttons & 7, ticks verbatim
    rep.equal('poll.mouse_x_load',
              len(rip_refs(img, at, end, b'\x48\x8B', 0x05, 0,
                           'input_mouse_x')), 1)
    rep.equal('poll.mouse_y_load',
              len(rip_refs(img, at, end, b'\x48\x8B', 0x15, 0,
                           'input_mouse_y')), 1)
    rep.check('poll.snapshot_packing',
              bytes.fromhex('48 C1 E2 10 48 09 D0') in body,
              'shl rdx, 16; or rax, rdx')
    rep.check('poll.buttons_masked',
              bytes.fromhex('48 83 E2 07') in body, 'and rdx, 7')
    rep.equal('poll.ticks_snapshot',
              len(rip_refs(img, at, end, b'\x4C\x8B', 0x05, 0,
                           'irq_test_ticks')), 1)
    rep.check('poll.frame_writes',
              bytes.fromhex('49 89 54 24 68') in body and
              bytes.fromhex('4D 89 44 24 40') in body,
              'buttons -> frame.RDX, ticks -> frame.R8')


def check_init(rep, img):
    at, end = region(img, 'init', 'redraw_desktop')
    d = img.data
    body = d[at:end]
    rep.check('init.checks_cpl', bytes.fromhex('66 8C C8 A8 03') in body)
    rep.check('init.checks_if', bytes.fromhex('9C 58 A9 00 02 00 00') in body)
    rep.equal('init.ready_guard',
              len([1 for _, t, imm in
                   rip_sites(img, at, end, b'\x48\x83', 0x3D, 1)
                   if t == img.at('ready') and imm == b'\x00']), 1)
    calls = [t for _, t in call_sites(img, at, end)]
    rep.equal('init.no_registrations', calls, [],
              'the handlers live only while a program runs')
    armed = [(t, imm) for _, t, imm in
             rip_sites(img, at, end, b'\x48\xC7', 0x05, 4)
             if t == img.at('ready')]
    rep.check('init.marks_ready_once',
              armed == [(img.at('ready'), struct.pack('<I', 1))])
    # task 125 relaunch fix: the fsprobe guest evidence snapshot is
    # taken ONCE here at init (the foundation runs fsys_probe right
    # before app_init), never per launch: a relaunch would re-copy the
    # previous run's file payloads out of the reused fsys_bounce page
    snap = body.find(b'\xF3\x48\xA5')
    rep.check('init.snapshot_rep_movsq', snap >= 0)
    rep.check('init.snapshot_cld_before_copy',
              snap >= 0 and b'\xFC' in body[:snap])
    rep.check('init.snapshot_count_512',
              bytes.fromhex('B9 00 02 00 00') in body)
    rsi = body.find(b'\x48\xC7\xC6')
    rdi = body.find(b'\x48\xC7\xC7')
    rep.check('init.snapshot_source_load', rsi >= 0)
    rep.check('init.snapshot_target_page20',
              rdi >= 0 and
              struct.unpack('<I', body[rdi + 3:rdi + 7])[0] ==
              img['fs_snapshot'])
    rep.check('init.snapshot_order',
              0 <= rsi < rdi < snap,
              'source load, target load, rep movsq')
    rep.check('init.eperm', (b'\xB8' + struct.pack('<i', EPERM)) in body)
    rep.check('init.ealready', (b'\xB8' + struct.pack('<i', EALREADY)) in body)


def check_redraw(rep, img):
    at, end = region(img, 'redraw_desktop', 'launch')
    d = img.data
    body = d[at:end]
    # task 125: the FULL v2 base (wallpaper, taskbar, start accents,
    # title) is delegated to desktop_repaint_base; its pixel content is
    # pinned by the stage2-desktop gate (taskbar.repaint_*), here the
    # delegation itself and the app-owned tail are pinned
    calls = [t for _, t in call_sites(img, at, end)]
    rep.equal('redraw.call_chain', calls,
              [img.at('desktop_repaint_base'), img.at('video_blit_rect'),
               img.at('desktop_cursor_place')],
              'delegated full base, saved echo line, cursor')
    sites = [s for s, _ in call_sites(img, at, end)]
    rep.check('redraw.base_repaint_first',
              len(sites) == 3 and sites[0] < sites[1] < sites[2],
              'base before the echo blit before the cursor place')
    rep.check('redraw.no_local_base_draw',
              bytes.fromhex('C7 44 24 20') +
              struct.pack('<I', DESKTOP_BG_COLOR) not in body and
              bytes.fromhex('C7 44 24 20') +
              struct.pack('<I', DESKTOP_BAR_COLOR) not in body,
              'the base colors live in desktop_repaint_base, not here')
    gate = [(s, t, imm) for s, t, imm in
            rip_sites(img, at, end, b'\x48\x83', 0x3D, 1)
            if t == img.at('echo_saved') and imm == b'\x01']
    rep.check('redraw.echo_saved_gate', len(gate) == 1)
    rep.check('redraw.echo_from_the_save_rectangle',
              abs32(bytes.fromhex('48 C7 C1'), img['echo_save']) in body)
    rep.check('redraw.echo_geometry',
              (b'\xBA' + struct.pack('<I', ECHO_SAVE_X)) in body and
              (b'\x41\xB8' + struct.pack('<I', ECHO_SAVE_Y)) in body and
              (b'\x41\xB9' + struct.pack('<I', ECHO_SAVE_W)) in body and
              bytes.fromhex('C7 44 24 20') +
              struct.pack('<I', ECHO_SAVE_H) in body)
    # the cursor is REPLACED at the desktop's own coordinates, no
    # stale save is reused
    rep.equal('redraw.cursor_x_load',
              len(rip_refs(img, at, end, b'\x48\x8B', 0x0D, 0,
                           'desktop_cursor_x')), 1)
    rep.equal('redraw.cursor_y_load',
              len(rip_refs(img, at, end, b'\x48\x8B', 0x15, 0,
                           'desktop_cursor_y')), 1)


def check_launch(rep, img):
    at, end = region(img, 'launch', 'program_path')
    d = img.data
    body = d[at:end]
    state = img.at('mm_vmm_state') + STATE_TABLE_ALLOCS
    drops = img.at('mm_vmm_state') + STATE_TABLE_DROPS

    # ---- the environment gates, nothing allocated before them ----
    rep.check('launch.checks_cpl', bytes.fromhex('66 8C C8 A8 03') in body)
    rep.check('launch.checks_if', bytes.fromhex('9C 58 A9 00 02 00 00') in body)
    for label in ('ready', 'fsys_ready', 'fs_ready', 'desktop_state'):
        sites = [(s, t, imm) for s, t, imm in
                 rip_sites(img, at, end, b'\x48\x83', 0x3D, 1)
                 if t == img.at(label) and imm == b'\x01']
        rep.check('launch.%s_gate' % label,
                  len(sites) == 1 and jne_at(d, sites[0][0] + 8),
                  'cmp qword [%s], 1; jne .enotready' % label)
    fb = [(s, t, imm) for s, t, imm in
          rip_sites(img, at, end, b'\x83', 0x3D, 1)
          if t == img.at('video_fb_state') + VIDEO_FB_STATE_READY and
          imm == b'\x00']
    rep.check('launch.video_ready_gate',
              len(fb) == 1 and je_at(d, fb[0][0] + 7),
              'cmp dword [video_fb_state.ready], 0; je .enotready')
    echo = [(s, t, imm) for s, t, imm in
            rip_sites(img, at, end, b'\x48\x83', 0x3D, 1)
            if t == img.at('desktop_echo_row') and imm == b'\x00']
    rep.check('launch.echo_row_gate',
              len(echo) == 1 and jne_at(d, echo[0][0] + 8),
              'the echo save covers only row 0')
    # task 125 relaunch: IDLE passes directly, DONE (a finished run)
    # passes through the reset, everything else is EALREADY
    idle = [(s, t, imm) for s, t, imm in
            rip_sites(img, at, end, b'\x48\x83', 0x3D, 1)
            if t == img.at('stage') and imm == bytes((STAGE_IDLE,))]
    rep.check('launch.stage_idle_gate',
              len(idle) == 1 and je_at(d, idle[0][0] + 8),
              'cmp qword [app_stage], IDLE; je .stage_ok')
    done_gate = [(s, t, imm) for s, t, imm in
                 rip_sites(img, at, end, b'\x48\x83', 0x3D, 1)
                 if t == img.at('stage') and imm == bytes((STAGE_DONE,))]
    rep.check('launch.stage_done_gate',
              len(done_gate) == 1 and jne_at(d, done_gate[0][0] + 8) and
              bool(idle) and done_gate[0][0] > idle[0][0],
              'cmp qword [app_stage], DONE; jne .ealready')
    # the per-run state is reset with pure stores behind the gate; the
    # cumulative evidence counters stay additive across runs
    for label in ('stage', 'exit_code', 'fault_vector', 'blit_count',
                  'interactive'):
        resets = [(s, t, imm) for s, t, imm in
                  rip_sites(img, at, end, b'\x48\xC7', 0x05, 4)
                  if t == img.at(label) and imm == bytes(4)]
        rep.check('launch.relaunch_resets_%s' % label,
                  len(resets) == 1 and bool(done_gate) and
                  resets[0][0] > done_gate[0][0],
                  'mov qword [%s], 0 after the gate' % label)
    for label in ('key_count', 'poll_count', 'fd_swept'):
        rep.check('launch.relaunch_keeps_%s' % label,
                  not [1 for _, t, _ in
                       rip_sites(img, at, end, b'\x48\xC7', 0x05, 4)
                       if t == img.at(label)],
                  'the cumulative evidence counters stay additive')
    # task 125: the one-shot fsys_bounce snapshot lives in app_init
    # ONLY; a per-launch copy would clobber the fsprobe guest evidence
    # with the previous run's file payloads on every relaunch
    rep.check('launch.no_snapshot_copy',
              b'\xF3\x48\xA5' not in body and
              abs32(bytes.fromhex('48 C7 C7'),
                    img['fs_snapshot']) not in body,
              'the relaunch never re-copies the bounce page')

    # ---- the frozen counter and table baselines ----
    for src, dst in (('fs_test_reads', 'saved_fs_reads'),
                     ('blk_test_reads', 'saved_blk_reads')):
        rep.equal('launch.snapshots_%s' % src,
                  len(rip_refs(img, at, end, b'\x48\x8B', 0x05, 0, src)), 1)
        rep.equal('launch.parks_%s' % dst,
                  len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0, dst)), 1)
    rep.equal('launch.table_baseline_loads',
              len([1 for _, t, _ in rip_sites(img, at, end, b'\x48\x8B', 0x05, 0)
                   if t == state]), 3,
              'snapshot, delta and the net invariant')
    rep.equal('launch.parks_table_base',
              len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0, 'table_base')), 1)
    rep.equal('launch.drops_baseline_loads',
              len([1 for _, t, _ in rip_sites(img, at, end, b'\x48\x8B', 0x05, 0)
                   if t == drops]), 2,
              'baseline snapshot and the released-tables receipt')
    rep.equal('launch.parks_drops_base',
              len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0, 'drops_base')), 1)

    # ---- the FS read of the program, never an embedded image ----
    rep.equal('launch.path_lea_sites',
              len(rip_refs(img, at, end, b'\x48\x8D', 0x0D, 0,
                           'program_path')), 2,
              'fs_stat and fs_open both name the frozen path')
    rep.check('launch.stat_buffer',
              abs32(bytes.fromhex('48 C7 C2'), img['stat_buf']) in body)
    rep.equal('launch.size_load',
              len(rip_refs(img, at, end, b'\x48\x8B', 0x05, 0, 'stat_buf')), 1)
    rep.check('launch.size_nonzero',
              bytes.fromhex('48 85 C0') in body, 'test rax, rax; jz .early_eio')
    rep.check('launch.size_gate_16k',
              (bytes.fromhex('48 3D') + struct.pack('<I', CODE_SIZE)) in body,
              'cmp rax, APP_USER_CODE_SIZE; ja .early_eio')
    rep.equal('launch.parks_image_size',
              len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0, 'image_size')), 1)
    rep.check('launch.open_readonly',
              bytes.fromhex('31 D2') in body, 'xor edx, edx: FS_O_RDONLY')
    rep.check('launch.fd_from_rdx', bytes.fromhex('41 89 D5') in body,
              'mov r13d, edx')
    rep.check('launch.read_chunk_cap',
              bytes.fromhex('48 81 FB 00 10 00 00 76 05 BB 00 10 00 00') in body,
              'chunk = min(left, 4096)')
    rep.check('launch.read_into_the_code_frames',
              abs32(bytes.fromhex('4A 8B 14 E5'), img['pages']) in body,
              'the fs core reads into identity pmm frames, never user VA')
    rep.check('launch.exact_read_count',
              bytes.fromhex('48 39 DA') in body,
              'cmp rdx, rbx; jne .read_fail, no short read')

    # ---- the 516 page footprint ----
    rep.check('launch.alloc_store',
              abs32(bytes.fromhex('4A 89 14 E5'), img['pages']) in body)
    rep.check('launch.alloc_516_pages',
              bytes.fromhex('41 81 FC 04 02 00 00 72') in body,
              'cmp r12d, 516; jb .alloc')
    rep.check('launch.code_base_immediate',
              body.count(imm64(b'\x48\xB9', CODE_BASE)) == 2,
              'the code map loop and the teardown VA dispatch')
    rep.check('launch.code_pages_pure_us',
              bytes.fromhex('49 C7 C0 04 00 00 00') in body, 'mov r8, MM_MAP_US')
    rep.check('launch.data_base_immediate',
              body.count(imm64(b'\x48\xB9', DATA_BASE)) == 2,
              'the data map loop and the teardown VA dispatch')
    rep.check('launch.data_us_rw_nx_once',
              body.count(imm64(b'\x49\xB8', MAP_US | MAP_RW | MAP_NX)) == 1,
              'one data loop covers the whole domain incl. the stack tail')
    rep.equal('launch.data_frame_load',
              body.count(abs32(bytes.fromhex('4A 8B 14 E5'),
                               img['pages'] + CODE_PAGES * 8)), 1)
    rep.check('launch.data_512_pages',
              bytes.fromhex('41 81 FC 00 02 00 00 72') in body,
              'cmp r12d, 512; jb .data_loop')
    rep.check('launch.four_page_loops',
              body.count(bytes.fromhex('41 83 FC 04 72')) == 2,
              'cmp r12d, 4; jb: the code loop and the teardown dispatch')
    # the frozen +3 budget
    rep.equal('launch.budget_subtract',
              len(rip_refs(img, at, end, b'\x48\x2B', 0x05, 0, 'table_base')), 1)
    rep.equal('launch.publishes_the_delta',
              len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0, 'table_delta')), 1)
    rep.check('launch.budget_compare_is_three',
              bytes.fromhex('48 83 F8 03 0F 85') in body,
              'cmp rax, 3; jne .fail_eio')

    # ---- the echo save and the READ_KEY cursor snapshot ----
    rep.check('launch.echo_save_rectangle',
              abs32(bytes.fromhex('48 C7 C1'), img['echo_save']) in body and
              (b'\xBA' + struct.pack('<I', ECHO_SAVE_X)) in body and
              (b'\x41\xB8' + struct.pack('<I', ECHO_SAVE_Y)) in body and
              (b'\x41\xB9' + struct.pack('<I', ECHO_SAVE_W)) in body)
    armed = [(t, imm) for _, t, imm in
             rip_sites(img, at, end, b'\x48\xC7', 0x05, 4)
             if t == img.at('echo_saved')]
    rep.check('launch.echo_saved_armed',
              armed == [(img.at('echo_saved'), struct.pack('<I', 1))])
    rep.equal('launch.kbd_cursor_snapshot',
              len(rip_refs(img, at, end, b'\x48\x8B', 0x05, 0,
                           'input_kbd_used')), 1)
    rep.equal('launch.kbd_cursor_park',
              len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0,
                           'kbd_consumed')), 1)

    # ---- the private syscall stack ----
    rep.equal('launch.reads_the_live_rsp0',
              len(rip_refs(img, at, end, b'\x48\x8B', 0x05, 0, 'cpu_tss_rsp0')), 1)
    rep.equal('launch.parks_the_old_rsp0',
              len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0, 'saved_rsp0')), 1)
    rep.check('launch.switches_to_the_private_stack',
              abs32(bytes.fromhex('48 C7 C1'), img['syscall_stack_top']) in body)
    rep.equal('launch.restores_the_old_rsp0',
              len(rip_refs(img, at, end, b'\x48\x8B', 0x0D, 0, 'saved_rsp0')), 2)

    # ---- the full ordered call chain ----
    calls = [t for _, t in call_sites(img, at, end)]
    sh = img.at('cpu_syscall_set_handler')
    want = ([img.at('fs_stat'), img.at('pmm_alloc_page'), img.at('fs_open'),
             img.at('fs_read'), img.at('fs_close'), img.at('fs_close'),
             img.at('vmm_map_user_page'), img.at('vmm_map_user_page'),
             img.at('video_readback_rect'), img.at('cpu_set_rsp0')] +
            [sh] * 4 + [img.at('cpu_user_fault_set_handler')] +
            [sh] * 4 + [img.at('cpu_user_fault_set_handler'),
                        img.at('cpu_set_rsp0'), img.at('fs_close'),
                        sh, img.at('cpu_set_rsp0'),
                        img.at('vmm_unmap_user_page'), img.at('pmm_free_page'),
                        img.at('redraw_desktop')])
    rep.equal('launch.ordered_call_chain', calls, want,
              'stat/alloc/open/read/close, 2 maps, readback, rsp0, '
              '4+4+1 registrations, 2 fault seams, sweep, teardown, redraw')
    rep.equal('launch.nine_handler_calls', calls.count(sh), 9,
              '4 register + 4 unregister + the S3 clear loop')
    rep.equal('launch.three_rsp0_calls',
              calls.count(img.at('cpu_set_rsp0')), 3)
    rep.equal('launch.two_fault_seam_calls',
              calls.count(img.at('cpu_user_fault_set_handler')), 2)
    rep.equal('launch.two_map_sites',
              calls.count(img.at('vmm_map_user_page')), 2)

    # ---- registration numbers and handler addresses ----
    numbers = ordered_positions(img, at, end,
                                [b'\xB9' + struct.pack('<I', n)
                                 for n in (0, 1, 2, 3)])
    rep.check('launch.numbers_0_to_3_in_order', numbers is not None)
    targets = [t for _, t, _ in rip_sites(img, at, end, b'\x48\x8D', 0x15, 0)]
    rep.equal('launch.handler_addresses', targets,
              [img.at(n) for n in ('h_exit', 'h_blit', 'h_read_key', 'h_poll')])
    rep.equal('launch.fault_handler_address',
              len(rip_refs(img, at, end, b'\x48\x8D', 0x0D, 0, 'h_fault')), 1)

    # ---- park the kernel context and enter ring 3 ----
    rep.equal('launch.parks_rsp',
              len(rip_refs(img, at, end, b'\x48\x89', 0x25, 0, 'launch_rsp')), 1)
    resume = [t for _, t, _ in rip_sites(img, at, end, b'\x48\x8D', 0x05, 0)
              if at < t < end]
    rep.check('launch.parks_a_resume_inside_itself', len(resume) == 1,
              'lea rax, [.resume]')
    rep.equal('launch.parks_rip',
              len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0, 'launch_rip')), 1)
    frame = ordered_positions(img, at, end, [
        b'\x6A\x2B',                                    # push SS
        imm64(b'\x48\xB8', STACK_TOP) + b'\x50',        # push RSP
        b'\x6A\x02',                                    # push RFLAGS, IF=0
        b'\x6A\x33',                                    # push CS
        imm64(b'\x48\xB8', CODE_BASE) + b'\x50',        # push RIP
        bytes.fromhex('48 CF'),                         # iretq
    ])
    rep.check('launch.iretq_frame_byte_by_byte', frame is not None,
              'SS/RSP/RFLAGS/CS/RIP then iretq')
    # the unregistration behind the iretq: numbers 0..3 with rdx = 0
    iretq_at = body.find(bytes.fromhex('48 CF'))
    tail = body[iretq_at:]
    zeros = ordered_positions(img, at + iretq_at, end,
                              [b'\xB9' + struct.pack('<I', n) +
                               bytes.fromhex('31 D2') for n in (0, 1, 2, 3)])
    rep.check('launch.unregisters_0_to_3', zeros is not None,
              'mov ecx, n; xor edx, edx behind the iretq')
    rep.check('launch.unregisters_the_fault_seam',
              bytes.fromhex('31 C9') in tail,
              'xor ecx, ecx; call cpu_user_fault_set_handler')

    # ---- the fd safety sweep ----
    rep.check('launch.sweep_eight_slots',
              bytes.fromhex('41 83 FC 08 73') in body,
              'cmp r12d, FS_MAX_FDS; jae .swept')
    rep.check('launch.sweep_slot_stride',
              bytes.fromhex('48 C1 E0 06') in body, 'shl rax, 6: FS_FD_SIZE')
    rep.equal('launch.sweep_table_base',
              len(rip_refs(img, at, end, b'\x48\x8D', 0x0D, 0, 'fs_fd_table')), 1)
    rep.check('launch.sweep_in_use_compare',
              bytes.fromhex('48 83 3C 01 00 74') in body,
              'cmp qword [rcx+rax+FS_FD_IN_USE], 0; je .sweep_next')
    rep.equal('launch.sweep_counter',
              len(rip_refs(img, at, end, b'\x48\xFF', 0x05, 0, 'fd_swept')), 1)

    # ---- restore the frozen counters before the verdict ----
    for src, dst in (('saved_fs_reads', 'fs_test_reads'),
                     ('saved_blk_reads', 'blk_test_reads')):
        rep.equal('launch.restores_from_%s' % src,
                  len(rip_refs(img, at, end, b'\x48\x8B', 0x05, 0, src)), 1)
        rep.equal('launch.restores_%s' % dst,
                  len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0, dst)), 1)
    # the ENOENT skip path restores the same snapshot via rdx before
    # reporting the non-fatal absence to the foundation
    for src, dst in (('saved_fs_reads', 'fs_test_reads'),
                     ('saved_blk_reads', 'blk_test_reads')):
        rep.equal('launch.skip_restores_from_%s' % src,
                  len(rip_refs(img, at, end, b'\x48\x8B', 0x15, 0, src)), 1)
        rep.equal('launch.skip_restores_%s' % dst,
                  len(rip_refs(img, at, end, b'\x48\x89', 0x15, 0, dst)), 1)
    verdict = rip_sites(img, at, end, b'\x48\x83', 0x3D, 1)
    rep.check('launch.exit_code_must_be_zero',
              any(t == img.at('exit_code') and imm == b'\x00'
                  for _, t, imm in verdict))
    rep.check('launch.fault_vector_must_be_zero',
              any(t == img.at('fault_vector') and imm == b'\x00'
                  for _, t, imm in verdict))
    rep.check('launch.blit_evidence_required',
              any(t == img.at('blit_count') and imm == b'\x01'
                  for _, t, imm in verdict),
              'cmp qword [app_blit_count], 1; jb .fail_eio')
    for name, code in (('enotready', ENOTREADY), ('eperm', EPERM),
                       ('ealready', EALREADY), ('eio', EIO)):
        rep.check('launch.reports_%s' % name,
                  (b'\xB8' + struct.pack('<i', code)) in body)
    rep.check('launch.reports_enomem',
              (b'\x41\xBF' + struct.pack('<i', ENOMEM)) in body,
              'mov r15d, ENOMEM parks the verdict for the teardown')

    # ---- the teardown on every post-snapshot exit path ----
    rep.check('launch.unmap_516_loop',
              bytes.fromhex('41 81 FC 04 02 00 00 73') in body,
              'cmp r12d, 516; jae .unmapped')
    rep.check('launch.unmap_va_dispatch',
              bytes.fromhex('41 83 FC 04 72') in body and
              bytes.fromhex('83 E8 04') in body,
              'idx < 4 -> code VA, else DATA_BASE + (idx-4)*4096')
    enoent = find_all(body, bytes.fromhex('83 F8 FD'))
    teardown_enoent = [p for p in enoent if jne_at(body, p + 3)]
    rep.check('launch.enoent_tolerated',
              len(teardown_enoent) == 1,
              'cmp eax, MM_ENOENT; jne .teardown_eio')
    skip_enoent = [p for p in enoent if je_at(body, p + 3)]
    rep.check('launch.stat_absent_is_enoent',
              len(skip_enoent) == 1,
              'cmp eax, APP_ENOENT; je .stat_absent (non-fatal skip)')
    rep.equal('launch.net_invariant_subtracts_drops',
              len([1 for _, t, _ in rip_sites(img, at, end, b'\x48\x2B', 0x05, 0)
                   if t == drops]), 1)
    rep.equal('launch.net_invariant_loads_table_base',
              len(rip_refs(img, at, end, b'\x48\x8B', 0x15, 0, 'table_base')), 1)
    rep.equal('launch.net_invariant_subtracts_drops_base',
              len(rip_refs(img, at, end, b'\x48\x2B', 0x15, 0, 'drops_base')), 1)
    rep.check('launch.net_invariant_compare',
              bytes.fromhex('48 39 D0') in body, 'cmp rax, rdx')
    rep.equal('launch.released_delta',
              len(rip_refs(img, at, end, b'\x48\x2B', 0x05, 0, 'drops_base')), 1)
    rep.equal('launch.parks_table_released',
              len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0,
                           'table_released')), 1)
    rep.equal('launch.free_guard',
              len([1 for _, t, _ in rip_sites(img, at, end, b'\x4C\x3B', 0x25, 0)
                   if t == img.at('pages_held')]), 1,
              'cmp r12, [app_pages_held]; jae .freed')
    rep.check('launch.zeroes_the_page_array',
              (abs32(bytes.fromhex('4A C7 04 E5'), img['pages']) +
               bytes(4)) in body,
              'mov qword [app_pages+r12*8], 0')
    rep.equal('launch.parks_pages_held',
              len(rip_refs(img, at, end, b'\x4C\x89', 0x25, 0, 'pages_held')), 2,
              'after the alloc loop and on the ENOMEM path')
    held_zero = [(t, imm) for _, t, imm in
                 rip_sites(img, at, end, b'\x48\xC7', 0x05, 4)
                 if t == img.at('pages_held')]
    rep.check('launch.clears_pages_held',
              held_zero == [(img.at('pages_held'), bytes(4))])
    done = [(s, t, imm) for s, t, imm in
            rip_sites(img, at, end, b'\x48\xC7', 0x05, 4)
            if t == img.at('stage') and
            imm == struct.pack('<I', STAGE_DONE)]
    rep.check('launch.publishes_done',
              len(done) == 1 and bool(idle) and done[0][0] > idle[0][0],
              'the DONE store sits in the teardown, after the reset')
    # the verdict travels through r15d across the teardown
    rep.check('launch.success_verdict_zero',
              bytes.fromhex('45 31 FF') in body, 'xor r15d, r15d')
    rep.equal('launch.parks_failure_verdict',
              body.count(bytes.fromhex('41 89 C7')), 1,
              'mov r15d, eax on the common failure path')
    rep.equal('launch.returns_the_parked_verdict',
              body.count(bytes.fromhex('44 89 F8')), 1,
              'mov eax, r15d after the teardown')
    # S3: the registration failure path clears exactly what was
    # registered and restores the foundation RSP0
    rsp0_sites = [s for s, t in call_sites(img, at, end)
                  if t == img.at('cpu_set_rsp0')]
    reg_sites = [s for s, t in call_sites(img, at, end)
                 if t == sh]
    rep.check('launch.s3_restore_after_the_clear_loop',
              len(rsp0_sites) == 3 and rsp0_sites[2] > reg_sites[-1],
              'the third cpu_set_rsp0 restores TSS.RSP0 on the S3 path')
    s3_ok = False
    if reg_sites:
        b = reg_sites[0] + 5 - at
        if body[b:b + 2] == bytes.fromhex('85 C0'):
            j = b + 2
            if body[j] == 0x75:
                target = at + j + 2 + struct.unpack_from('<b', body, j + 1)[0]
            elif body[j:j + 2] == b'\x0F\x85':
                target = at + j + 6 + struct.unpack_from('<i', body, j + 2)[0]
            else:
                target = -1
            t = target - at
            s3_ok = (0 <= t and
                     body[t:t + 6] == bytes.fromhex('41 89 C6 45 31 E4'))
    rep.check('launch.s3_branch_targets_the_restore', s3_ok,
              'jnz after the EXIT registration lands on .fail_rsp0 '
              '(mov r14d, eax; xor r12d, r12d)')


def check_selftest(rep, img):
    at, end = region(img, 'selftest', 'data_start')
    d = img.data
    body = d[at:end]
    pristine = [('ready', 1), ('stage', STAGE_IDLE), ('pages_held', 0),
                ('table_delta', 0), ('blit_count', 0), ('exit_code', 0),
                ('fault_vector', 0)]
    for label, want in pristine:
        sites = [(s, t, imm) for s, t, imm in
                 rip_sites(img, at, end, b'\x48\x83', 0x3D, 1)
                 if t == img.at(label) and imm == bytes((want,))]
        rep.check('selftest.%s_is_%d' % (label, want), len(sites) == 1)
    # the handler table shape: 0..3 empty, 4..9 resident
    rep.check('selftest.handler_table_compares',
              body.count(abs32(bytes.fromhex('4A 83 3C E5'),
                               img['cpu_syscall_handlers']) + b'\x00') == 2,
              'cmp qword [cpu_syscall_handlers+r12*8], 0 twice')
    rep.check('selftest.empty_loop_bound',
              bytes.fromhex('41 83 FC 04 72') in body, 'cmp r12d, 4; jb')
    rep.check('selftest.resident_loop_bound',
              bytes.fromhex('41 83 FC 0A 72') in body, 'cmp r12d, 10; jb')
    rep.check('selftest.eio', (b'\xB8' + struct.pack('<i', EIO)) in body)


def check_module_hygiene(rep, img):
    # the function bodies only: the path string is data, not code
    windows = [(img.at('check_range'), img.at('program_path')),
               (img.at('selftest'), img.at('data_start'))]
    d = img.data
    seams = {img.at(n) for n in M[48:88]} - {
        img.at('fs_test_reads'), img.at('blk_test_reads'),
        img.at('fs_fd_table'), img.at('mm_vmm_state'),
        img.at('cpu_tss_rsp0'), img.at('cpu_syscall_handlers'),
        img.at('video_fb_state'), img.at('desktop_title'),
        img.at('desktop_state'), img.at('desktop_echo_row'),
        img.at('desktop_cursor_x'), img.at('desktop_cursor_y'),
        img.at('input_kbd_buf'), img.at('input_kbd_used'),
        img.at('input_mouse_x'), img.at('input_mouse_y'),
        img.at('input_mouse_b1'), img.at('irq_test_ticks'),
        img.at('fsys_ready'), img.at('fs_ready')}
    stray = []
    bad_writes = []
    sti_sites = []
    hlt_sites = []
    iretq = 0
    sysret = 0
    swapgs = 0
    allowed = {img.at('fs_test_reads'), img.at('blk_test_reads')}
    launch_lo, launch_hi = region(img, 'launch', 'program_path')
    launch_loads = {s: t for s, t, _ in
                    rip_sites(img, launch_lo, launch_hi, b'\x48\x8B', 0x05, 0)}
    launch_stores = {s: t for s, t, _ in
                     rip_sites(img, launch_lo, launch_hi, b'\x48\x89', 0x05, 0)}
    head = img.at('input_kbd_head')
    desktop_tail = img.at('desktop_kbd_consumed')
    handoff = {(s, t) for s, t in launch_stores.items()
               if (t == head and launch_stores.get(s - 7) == img.at('kbd_consumed')
                   and launch_loads.get(s - 14) == img.at('input_kbd_used'))
               or (t == desktop_tail and launch_loads.get(s - 7) == head)}
    key_lo, key_hi = region(img, 'h_read_key', 'h_poll')
    key_increments = {s: t for s, t, _ in
                      rip_sites(img, key_lo, key_hi, b'\x48\xFF', 0x05, 0)}
    release = {(s, t) for s, t in key_increments.items()
               if t == head and key_increments.get(s - 7) == img.at('kbd_consumed')}
    rep.check('hygiene.keyboard_handoff_writes', {t for _, t in handoff}
              == {head, desktop_tail} and len(handoff) == 2 and len(release) == 1)
    lo, hi = img.at('data_start'), img.at('data_end')
    for at, end in windows:
        code = d[at:end]
        for s, t in call_sites(img, at, end):
            inside = any(a <= t < b for a, b in windows)
            if not inside and t not in seams:
                stray.append((s, t))
        for prefix, modrm, tail in ((b'\x48\x89', 0x05, 0),
                                    (b'\x48\x89', 0x25, 0),
                                    (b'\x4C\x89', 0x25, 0),
                                    (b'\x48\xFF', 0x05, 0),
                                    (b'\x48\xC7', 0x05, 4)):
            for s, t, _ in rip_sites(img, at, end, prefix, modrm, tail):
                if (not (lo <= t < hi) and t not in allowed
                        and (s, t) not in handoff and (s, t) not in release):
                    bad_writes.append((s, t))
        iretq += code.count(bytes.fromhex('48 CF'))
        sysret += code.count(bytes.fromhex('48 0F 07'))
        swapgs += code.count(bytes.fromhex('0F 01 F8'))
        # single byte opcodes are scanned outside the displacement and
        # immediate windows: a 0xFB inside a disp32 is data, not an STI
        skip = set()
        forms = [(b'\x48\x8B', 0x05, 0), (b'\x48\x8B', 0x0D, 0),
                 (b'\x48\x8B', 0x15, 0), (b'\x48\x8B', 0x25, 0),
                 (b'\x48\x8B', 0x35, 0), (b'\x4C\x8B', 0x05, 0),
                 (b'\x48\x89', 0x05, 0), (b'\x48\x89', 0x25, 0),
                 (b'\x48\xFF', 0x05, 0),
                 (b'\x4C\x89', 0x25, 0), (b'\x4C\x3B', 0x25, 0),
                 (b'\x48\x3B', 0x05, 0),
                 (b'\x48\x8D', 0x05, 0), (b'\x48\x8D', 0x0D, 0),
                 (b'\x48\x8D', 0x15, 0), (b'\x4C\x8D', 0x0D, 0),
                 (b'\x48\x2B', 0x05, 0), (b'\x48\x2B', 0x15, 0),
                 (b'\xFF', 0x25, 0), (b'\x48\x83', 0x3D, 1),
                 (b'\x48\x81', 0x3D, 4), (b'\x48\xC7', 0x05, 4),
                 (b'\x83', 0x3D, 4)]
        for prefix, modrm, tail in forms:
            for p in find_all(code, prefix):
                m = p + len(prefix)
                if m + 5 + tail <= len(code) and code[m] == modrm:
                    skip.update(range(m + 1, m + 5 + tail))
        # SIB absolute forms: the page array and the handler table
        for prefix, tail in ((b'\x4A\x89\x14\xE5', 0),
                             (b'\x4A\x8B\x14\xE5', 0),
                             (b'\x4A\x8B\x0C\xE5', 0),
                             (b'\x4A\xC7\x04\xE5', 4),
                             (b'\x48\x83\x3C\xC5', 1),
                             (b'\x48\x83\x3C\x01', 1)):
            for p in find_all(code, prefix):
                if p + 8 + tail <= len(code):
                    skip.update(range(p + 4, p + 8 + tail))
        for prefix in (b'\x48\xC7\xC1', b'\x48\xC7\xC2', b'\x49\xC7\xC0'):
            for p in find_all(code, prefix):
                skip.update(range(p + 3, p + 7))
        for p in find_all(code, b'\x48\x29\xFB'):
            skip.add(p + 2)
        for p in find_all(code, b'\x48\x81\xFB'):
            skip.update(range(p + 2, p + 7))
        for p in find_all(code, b'\xC7\x44\x24\x20'):
            skip.update(range(p + 4, p + 8))
        for p in find_all(code, b'\xC7\x80\x20\x03\x00\x00'):
            skip.update(range(p + 2, p + 10))
        for p in find_all(code, b'\x49\xC7\x44\x24\x68'):
            skip.update(range(p + 5, p + 9))
        for prefix in (b'\x48\xB8', b'\x48\xB9', b'\x49\xB8'):
            for p in find_all(code, prefix):
                skip.update(range(p + 2, p + 10))
        for p in find_all(code, b'\xE8'):
            skip.update(range(p + 1, p + 5))
        for p in find_all(code, b'\x0F'):
            if p + 6 <= len(code) and 0x80 <= code[p + 1] <= 0x8F:
                skip.update(range(p + 2, p + 6))
        for p in find_all(code, b'\x3D'):
            skip.update(range(p + 1, p + 5))
        # task 124: the B8-BF mov-imm scan must not re-interpret call
        # rel32 operand bytes as opcodes. The kernel grew +3MiB (the
        # embedded wallpaper), so a cross-module call displacement byte
        # (e.g. 0xBC) can land in the B8-BF range and its phantom imm32
        # window would swallow the real STI right after the call. The
        # call operand windows are already excluded above; scan B8-BF
        # only on the remaining bytes (strictly tighter, never weaker).
        for i, b in enumerate(code):
            if 0xB8 <= b <= 0xBF and i + 5 <= len(code) and i not in skip:
                skip.update(range(i + 1, i + 5))
        for i, b in enumerate(code):
            if 0x70 <= b <= 0x7F or b == 0xEB:
                skip.add(i + 1)
        sti_sites += [at + i for i in range(len(code))
                      if code[i] == 0xFB and i not in skip]
        hlt_sites += [at + i for i in range(len(code))
                      if code[i] == 0xF4 and i not in skip]
    rep.check('hygiene.call_targets', not stray, repr(stray[:4]))
    rep.check('hygiene.rip_writes_stay_in_data', not bad_writes,
              repr(bad_writes[:4]))
    rep.check('hygiene.no_sysret', sysret == 0)
    rep.check('hygiene.no_swapgs', swapgs == 0)
    rep.equal('hygiene.one_iretq', iretq, 1, 'the single ring 3 entry')
    # exactly one STI, the interactive POLL leg, opening the bounded
    # pause-spin window that CLI closes (task 141 W1: HLT is gone)
    rep.equal('hygiene.one_sti', len(sti_sites), 1,
              'the frozen interactive POLL semantics')
    rep.equal('hygiene.zero_hlt', len(hlt_sites), 0,
              'no HLT anywhere: a gdbstub write cannot wake it')
    rep.check('hygiene.sti_spin_cli_adjacent',
              len(sti_sites) == 1 and
              d[sti_sites[0]:sti_sites[0] + 13] ==
              b'\xFB\xB9' + struct.pack('<I', IPOLL_CHUNK) +
              bytes.fromhex('F3 90 FF C9 75 FA FA'))


# ----------------------------------------------------------------------
# pure model replay of the frozen semantics
# ----------------------------------------------------------------------


def model_check_range(va, length, write, pages):
    """The full-range validator: domain, overflow, 4 KiB user leaves."""
    mask = (1 << 64) - 1
    if length == 0:
        return OK
    if length & (1 << 63):
        return EFAULT
    end = va + length
    if end > mask:
        return EFAULT
    if va < VA_BASE:
        return EFAULT
    if end > VA_LIMIT:
        return EFAULT
    cur = va
    while cur < end:
        page = cur & ~0xFFF
        rec = pages.get(page)
        if rec is None:
            return EFAULT
        size, us, rw = rec
        if size != PAGE or us != 1:
            return EFAULT
        if write and rw != 1:
            return EFAULT
        cur = page + PAGE
    return OK


def model_blit(x, y, w, h, src, pages):
    """The BLIT decision table: geometry first, then the read-only
    full-range validation; a bad pointer is EFAULT, never a copy."""
    if w == 0 or h == 0:
        return EINVAL
    if (x + w) & 0xFFFFFFFF > SCREEN_W or x + w > SCREEN_W:
        return EINVAL
    if (y + h) & 0xFFFFFFFF > SCREEN_H or y + h > SCREEN_H:
        return EINVAL
    return model_check_range(src, w * h * 4, 0, pages)


def model_read_key(consumed, queue, stage):
    """(ascii, raw, new_consumed, interactive_flip). The fabricated
    ESC never advances the cursor; only a real Enter flips the mode."""
    if consumed < len(queue):
        ascii_, raw = queue[consumed]
        return ascii_, raw, consumed + 1, raw == SC_ENTER
    if stage == STAGE_EXIT_REQ:
        return ASCII_ESC, SC_ESC, consumed, False
    return 0, 0, consumed, False


def model_poll(x, y, buttons, ticks):
    return x | (y << 16), buttons & 7, ticks


def model_size_gate(size):
    return OK if 0 < size <= CODE_SIZE else EIO


def model_relaunch_gate(stage):
    """task 125: a fresh module and a finished run may both launch."""
    return OK if stage in (STAGE_IDLE, STAGE_DONE) else EALREADY


def model_launch_chunks(size):
    out, left = [], size
    while left > 0:
        chunk = min(left, PAGE)
        out.append(chunk)
        left -= chunk
    return out


def full_domain_pages():
    """The frozen 516 page footprint: code RO+US, data RW+US."""
    pages = {}
    for i in range(CODE_PAGES):
        pages[CODE_BASE + i * PAGE] = (PAGE, 1, 0)
    for i in range(DATA_PAGES):
        pages[DATA_BASE + i * PAGE] = (PAGE, 1, 1)
    return pages


def check_model(rep, img):
    pages = full_domain_pages()
    rep.equal('model.footprint_pages', len(pages), LAUNCH_PAGES)
    # the range validator matrix over the frozen domain
    hole = dict(pages)
    del hole[DATA_BASE + 2 * PAGE]
    supervisor = dict(pages)
    supervisor[DATA_BASE + PAGE] = (PAGE, 0, 1)
    big = dict(pages)
    big[DATA_BASE] = (0x200000, 1, 1)
    cases = [
        ('zero_length_anywhere', 0, 0, 1, pages, OK),
        ('zero_length_outside', VA_BASE - 1, 0, 1, pages, OK),
        ('whole_domain_span', VA_BASE, VA_LIMIT - VA_BASE, 0, pages, EFAULT),
        ('whole_code_domain', CODE_BASE, CODE_SIZE, 0, pages, OK),
        ('whole_data_domain', DATA_BASE, DATA_SIZE, 1, pages, OK),
        ('last_byte', VA_LIMIT - 1, 1, 1, pages, OK),
        ('below_base', VA_BASE - 1, 1, 0, pages, EFAULT),
        ('one_past_limit', VA_LIMIT - 1, 2, 0, pages, EFAULT),
        ('kernel_pointer', KERNEL_BASE, 64, 1, pages, EFAULT),
        ('negative_length', VA_BASE, (1 << 64) - 1, 0, pages, EFAULT),
        ('overflow', VA_LIMIT - 0x10, 0x20, 0, pages, EFAULT),
        ('unmapped_hole', DATA_BASE, 4 * PAGE, 0, hole, EFAULT),
        ('two_mib_leaf', DATA_BASE, PAGE, 0, big, EFAULT),
        ('supervisor_page', DATA_BASE, 2 * PAGE, 0, supervisor, EFAULT),
        ('write_to_ro_code', CODE_BASE, PAGE, 1, pages, EFAULT),
        ('read_from_ro_code', CODE_BASE, PAGE, 0, pages, OK),
        ('cross_page_write', DATA_BASE + PAGE - 8, 16, 1, pages, OK),
    ]
    for name, va, length, write, pg, want in cases:
        rep.equal('model.range_%s' % name,
                  model_check_range(va, length, write, pg), want)

    # the BLIT decision table
    fb_src = DATA_BASE + 0x2000       # CHIP8_FB_VA inside the data domain
    blit_cases = [
        ('frozen_rectangle', BLIT_DST_X, BLIT_DST_Y, FB_WIDTH, FB_HEIGHT,
         fb_src, pages, OK),
        ('x_overflow', 385, BLIT_DST_Y, FB_WIDTH, FB_HEIGHT, fb_src, pages,
         EINVAL),
        ('y_overflow', BLIT_DST_X, 449, FB_WIDTH, FB_HEIGHT, fb_src, pages,
         EINVAL),
        ('zero_width', BLIT_DST_X, BLIT_DST_Y, 0, FB_HEIGHT, fb_src, pages,
         EINVAL),
        ('zero_height', BLIT_DST_X, BLIT_DST_Y, FB_WIDTH, 0, fb_src, pages,
         EINVAL),
        ('kernel_source', BLIT_DST_X, BLIT_DST_Y, FB_WIDTH, FB_HEIGHT,
         KERNEL_BASE, pages, EFAULT),
        ('source_past_limit', BLIT_DST_X, BLIT_DST_Y, FB_WIDTH, FB_HEIGHT,
         VA_LIMIT - 16, pages, EFAULT),
        ('ro_code_source_is_legal', BLIT_DST_X, BLIT_DST_Y, 64,
         16, CODE_BASE, pages, OK),
        ('ro_code_full_frame_overruns', BLIT_DST_X, BLIT_DST_Y, FB_WIDTH,
         FB_HEIGHT, CODE_BASE, pages, EFAULT),
        ('hole_source', BLIT_DST_X, BLIT_DST_Y, FB_WIDTH, FB_HEIGHT,
         DATA_BASE, hole, EFAULT),
    ]
    for name, x, y, w, h, src, pg, want in blit_cases:
        rep.equal('model.blit_%s' % name,
                  model_blit(x, y, w, h, src, pg), want)

    # the READ_KEY queue and the fabricated exit
    queue = [(ord('a'), 0x1E), (13, SC_ENTER), (0, 0x9E)]
    rep.equal('model.key_real_ascii', model_read_key(0, queue, STAGE_IDLE),
              (ord('a'), 0x1E, 1, False))
    rep.equal('model.key_enter_flips', model_read_key(1, queue, STAGE_IDLE),
              (13, SC_ENTER, 2, True))
    rep.equal('model.key_empty_idle', model_read_key(3, queue, STAGE_IDLE),
              (0, 0, 3, False))
    rep.equal('model.key_empty_ibm_ready',
              model_read_key(3, queue, STAGE_IBM_READY), (0, 0, 3, False))
    rep.equal('model.key_fabricated_esc',
              model_read_key(3, queue, STAGE_EXIT_REQ),
              (ASCII_ESC, SC_ESC, 3, False))
    rep.equal('model.key_real_before_fabrication',
              model_read_key(0, queue, STAGE_EXIT_REQ),
              (ord('a'), 0x1E, 1, False))

    # the POLL snapshot packing
    rep.equal('model.poll_packing', model_poll(557, 411, 0x1FF, 11),
              (557 | 411 << 16, 7, 11))
    rep.equal('model.poll_zero', model_poll(0, 0, 0, 11), (0, 0, 11))

    # the program size gate and the launch read chunking
    for size, want in ((0, EIO), (1, OK), (CHIP8_SIZE, OK), (CODE_SIZE, OK),
                       (CODE_SIZE + 1, EIO), (1 << 20, EIO)):
        rep.equal('model.size_gate_%d' % size, model_size_gate(size), want)
    rep.equal('model.chunks_chip8', model_launch_chunks(CHIP8_SIZE),
              [CHIP8_SIZE])
    rep.equal('model.chunks_exact_16k', model_launch_chunks(CODE_SIZE),
              [PAGE] * 4)
    rep.equal('model.chunks_ragged', model_launch_chunks(8229),
              [PAGE, PAGE, 37])

    # task 125 relaunch decision table: IDLE/DONE launch, a live run
    # refuses with EALREADY, unknown stages never slip through
    for stage, want in ((STAGE_IDLE, OK), (STAGE_DONE, OK),
                        (STAGE_IBM_READY, EALREADY),
                        (STAGE_EXIT_REQ, EALREADY), (4, EALREADY),
                        (99, EALREADY)):
        rep.equal('model.relaunch_gate_%d' % stage,
                  model_relaunch_gate(stage), want)


# ----------------------------------------------------------------------
# the disk images and the chip8 reference model integration
# ----------------------------------------------------------------------


class Fat16:
    """A minimal read-only FAT16 reader, independent of the builder."""

    def __init__(self, data):
        self.data = data
        self.bps = u16(data, 11)
        self.spc = data[13]
        reserved = u16(data, 14)
        nfats = data[16]
        root_entries = u16(data, 17)
        fat_size = u16(data, 22)
        self.fat_start = reserved * self.bps
        self.root_start = self.fat_start + nfats * fat_size * self.bps
        self.root_size = root_entries * 32
        self.data_start = self.root_start + self.root_size
        self.cluster_size = self.spc * self.bps

    def chain(self, cluster):
        while 2 <= cluster < 0xFFF8:
            yield cluster
            cluster = u16(self.data, self.fat_start + cluster * 2)

    def cluster_bytes(self, cluster):
        off = self.data_start + (cluster - 2) * self.cluster_size
        return self.data[off:off + self.cluster_size]

    def dir_bytes(self, cluster):
        if cluster == 0:
            return self.data[self.root_start:self.root_start + self.root_size]
        return b''.join(self.cluster_bytes(c) for c in self.chain(cluster))

    @staticmethod
    def name83(part):
        if '.' in part:
            base, ext = part.split('.', 1)
        else:
            base, ext = part, ''
        return (base.ljust(8) + ext.ljust(3)).encode('ascii')

    def find(self, dir_bytes, part):
        want = self.name83(part)
        for off in range(0, len(dir_bytes), 32):
            entry = dir_bytes[off:off + 32]
            if entry[0] == 0x00:
                break
            if entry[0] == 0xE5 or entry[11] == 0x0F:
                continue
            if entry[0:11] == want:
                return entry[11], u16(entry, 26), u32(entry, 28)
        raise KeyError(part)

    def read(self, path):
        parts = path.strip('/').split('/')
        cluster = 0
        for part in parts[:-1]:
            attr, cluster, _ = self.find(self.dir_bytes(cluster), part)
            if not attr & 0x10:
                raise KeyError(part + ' is not a directory')
        attr, cluster, size = self.find(self.dir_bytes(cluster), parts[-1])
        if attr & 0x10:
            raise KeyError(path + ' is a directory')
        blob = b''.join(self.cluster_bytes(c) for c in self.chain(cluster))
        return blob[:size]


def check_disk_and_model(rep, root, fresh_chip8):
    import chip8_model as cm

    fs_dir = root / 'build' / 'fs-v1'
    images = {}
    for name, want in (('disk.img', DISK_SHA), ('disk-big.img', DISK_BIG_SHA),
                       ('disk-app.img', DISK_APP_SHA)):
        path = fs_dir / name
        if not rep.check('disk.%s_present' % name, path.is_file(), str(path)):
            return
        data = path.read_bytes()
        images[name] = data
        rep.equal('disk.%s_sha256_frozen' % name,
                  hashlib.sha256(data).hexdigest(), want)

    app = Fat16(images['disk-app.img'])
    plain = Fat16(images['disk.img'])
    ibm = app.read('/ROMS/IBM.CH8')
    tetris = app.read('/ROMS/TETRIS1.CH8')
    program = app.read('/PROGRAMS/CHIP8.BIN')
    rom_dir = root / 'user' / 'chip8' / 'roms'
    rep.check('disk.ibm_matches_rom_file',
              ibm == (rom_dir / 'ibm.ch8').read_bytes(),
              '%d bytes read through the FAT16 chain' % len(ibm))
    rep.check('disk.tetris_matches_rom_file',
              tetris == (rom_dir / 'tetris1.ch8').read_bytes(),
              '%d bytes' % len(tetris))
    rep.check('disk.app_roms_equal_plain',
              ibm == plain.read('/ROMS/IBM.CH8') and
              tetris == plain.read('/ROMS/TETRIS1.CH8'),
              'the FS-v1 ROM content is untouched on the app image')
    rep.check('disk.program_matches_fresh_chip8', program == fresh_chip8,
              '%d vs %d bytes' % (len(program), len(fresh_chip8)))
    rep.equal('disk.program_size_gate', model_size_gate(len(program)), OK)

    # task 141 W1: the interactive loop carries a FIXED per-wake budget
    # (mov ebx, IPS_PER_TICK; add [st_timer_acc], TIMER_HZ; dec ebx/jnz),
    # never a tick-delta budget off the frozen POLL r8d snapshot
    rep.check('chip8.fixed_budget_mov',
              bytes.fromhex('BB 05 00 00 00') in fresh_chip8,
              'mov ebx, IPS_PER_TICK')
    acc_sites = []
    for p in find_all(fresh_chip8, b'\x83\x05'):
        if p + 7 <= len(fresh_chip8) and fresh_chip8[p + 6] == 0x3C:
            disp = struct.unpack_from('<i', fresh_chip8, p + 2)[0]
            acc_sites.append(CODE_BASE + p + 7 + disp)
    rep.equal('chip8.timer_acc_per_round', acc_sites, [0x1006CA054],
              'add dword [st_timer_acc], TIMER_HZ once per round')
    rep.check('chip8.budget_loop_dec_jnz',
              bytes.fromhex('FF CB 75') in fresh_chip8,
              'dec ebx; jnz .run_instr')

    # the sha256 constants stay the gate expectations (user ruling)
    digests = cm.parse_api_digests()
    rep.equal('disk.ibm_sha_matches_api', hashlib.sha256(ibm).hexdigest(),
              digests['IBM'])
    rep.equal('disk.tetris_sha_matches_api',
              hashlib.sha256(tetris).hexdigest(), digests['TETRIS'])

    # the model consumes the bytes READ FROM THE IMAGE, not the files
    probes_path = root / 'build' / 'user-chip8' / 'ibm-probes.json'
    if not rep.check('model.probes_present', probes_path.is_file(),
                     str(probes_path)):
        return
    probes = json.loads(probes_path.read_text())
    m = cm.Chip8(ibm)
    m.run(probes['steps'])
    rep.equal('model.ibm_final_pc', m.pc, int(probes['final_pc'], 16))
    rep.equal('model.ibm_final_sp', m.sp, probes['final_sp'])
    rep.equal('model.ibm_gfx_sha', hashlib.sha256(bytes(m.gfx)).hexdigest(),
              probes['gfx_sha256'])
    frame = m.render()
    rep.equal('model.ibm_frame_sha', hashlib.sha256(frame).hexdigest(),
              probes['frame_sha256'])
    rep.equal('model.ibm_frame_size', len(frame), probes['frame_size'])
    cells = [(cx, cy) for cy in range(cm.GFX_ROWS)
             for cx in range(cm.GFX_COLS) if m.gfx[cy * cm.GFX_COLS + cx]]
    rep.equal('model.ibm_nonzero_cells', len(cells), probes['nonzero_cells'])
    rep.equal('model.ibm_bbox',
              {'x0': min(c[0] for c in cells), 'y0': min(c[1] for c in cells),
               'x1': max(c[0] for c in cells), 'y1': max(c[1] for c in cells)},
              probes['bbox'])
    rep.equal('model.ibm_rom_sha_probe', probes['rom_sha256'],
              hashlib.sha256(ibm).hexdigest())
    expected = (root / 'build' / 'user-chip8' / 'ibm-expected-frame.bin')
    rep.check('model.ibm_frame_file_matches',
              expected.is_file() and expected.read_bytes() == frame)
    # the tetris leg starts from an all black frame
    t = cm.Chip8(tetris)
    rep.equal('model.tetris_zero_gfx', sum(t.gfx), 0)
    rep.check('model.tetris_zero_frame',
              t.render() == bytes(FB_WIDTH * FB_HEIGHT * 4))

    # task 141 W1: the interactive fixed-budget tetris leg. Replay the
    # rounds independently of the model artifacts and compare: non-zero
    # stepping after Enter, a non-black intro frame, and a key-7 tap
    # that changes the presented screen versus the no-key run.
    ip_path = root / 'build' / 'user-chip8' / 'tetris-int-probes.json'
    if not rep.check('model.int_probes_present', ip_path.is_file(),
                     str(ip_path)):
        return
    ip = json.loads(ip_path.read_text())
    rep.equal('model.int_ips_per_tick', ip['ips_per_tick'], 5)
    rep.equal('model.int_timer_hz', ip['timer_hz'], 60)
    rep.equal('model.int_timer_acc_base', ip['timer_acc_base'], 100)

    def int_replay(upto, tap):
        m = cm.Chip8(tetris)
        acc = 0
        last_gfx = bytes(cm.GFX_SIZE)
        last_present = 0
        for r in range(1, upto + 1):
            if tap and r == ip['key_make_round']:
                m.key[ip['key_index']] = 1
            if tap and r == ip['key_break_round']:
                m.key[ip['key_index']] = 0
            acc, drew = cm.interactive_round(m, acc)
            if drew:
                last_gfx = bytes(m.gfx)
                last_present = r
        return m, last_gfx, last_present

    m1, _, _ = int_replay(ip['k1_rounds'], False)
    rep.equal('model.int_k1_cells', sum(m1.gfx), ip['k1_nonzero_cells'])
    rep.check('model.int_k1_steps_nonzero', m1.pc != cm.ROM_LOAD_ADDR)
    rep.equal('model.int_k1_gfx_sha',
              hashlib.sha256(bytes(m1.gfx)).hexdigest(), ip['k1_gfx_sha256'])
    f1 = m1.render()
    rep.equal('model.int_k1_frame_sha',
              hashlib.sha256(f1).hexdigest(), ip['k1_frame_sha256'])
    rep.check('model.int_k1_nonblack', f1 != bytes(FB_WIDTH * FB_HEIGHT * 4))
    rep.check('model.int_k1_frame_file',
              (root / 'build' / 'user-chip8' /
               'tetris-int-frame-20.bin').read_bytes() == f1)

    m2, _, _ = int_replay(ip['k2_rounds'], False)
    rep.equal('model.int_k2_pc', m2.pc, int(ip['k2_pc'], 16))
    rep.equal('model.int_k2_delay', m2.delay, ip['k2_delay'])

    mk, gk, pk = int_replay(ip['capture_rounds'], True)
    mn, gn, pn = int_replay(ip['capture_rounds'], False)
    rep.equal('model.int_key_last_present', pk, ip['key_last_present'])
    rep.equal('model.int_nokey_last_present', pn, ip['nokey_last_present'])
    rep.check('model.int_screen_diverges', gk != gn,
              'the key-7 tap must change the presented screen')
    fk, fn = mk.render(), mn.render()
    rep.equal('model.int_key_frame_sha',
              hashlib.sha256(fk).hexdigest(), ip['key_frame_sha256'])
    rep.equal('model.int_nokey_frame_sha',
              hashlib.sha256(fn).hexdigest(), ip['nokey_frame_sha256'])
    rep.check('model.int_key_frame_file',
              (root / 'build' / 'user-chip8' /
               'tetris-int-frame-71-key.bin').read_bytes() == fk)
    rep.check('model.int_nokey_frame_file',
              (root / 'build' / 'user-chip8' /
               'tetris-int-frame-71-nokey.bin').read_bytes() == fn)
    rep.check('model.int_key_frame_not_nokey', fk != fn)


# ----------------------------------------------------------------------
# source level discipline
# ----------------------------------------------------------------------


def check_sources(rep, root):
    asm_text = (root / 'kernel' / 'app' / 'app.asm').read_text(encoding='utf-8')
    rep.check('source.app_asm_hosted_guard',
              'if ~ defined APP_HOSTED' in asm_text and
              'format binary' in asm_text)
    order = re.findall(r"^include\s+'([^']+)'", asm_text, re.M)
    rep.equal('source.include_order', order,
              ['api.inc', 'app.inc', 'tasks.inc', 'window.inc', 'selftest.inc', 'data.inc'])
    rep.check('source.app_asm_end_assertion',
              'if $ <> app_data_end' in asm_text)

    api = (root / 'kernel' / 'app' / 'api.inc').read_text(encoding='utf-8')
    rep.check('source.api_private_assertions',
              len(re.findall(r'^\s*err\b', api, re.M)) >= 10,
              '%d err sites' % len(re.findall(r'^\s*err\b', api, re.M)))

    # the user ruling: no embedded program anywhere in the module
    embedded = []
    for path in sorted((root / 'kernel' / 'app').glob('*')):
        if path.suffix in ('.asm', '.inc'):
            if re.search(r'^\s*file\s', path.read_text(encoding='utf-8'), re.M):
                embedded.append(path.name)
    rep.check('source.app_no_file_directive', not embedded, repr(embedded))

    # the kernel include order: app sits between fsys and contracts
    k64 = (root / 'kernel' / 'kernel64.asm').read_text(encoding='utf-8')
    incs = re.findall(r"^include\s+'([^']+)'", k64, re.M)
    ok = ('app/app.asm' in incs and
          incs.index('app/app.asm') == incs.index('fsys/fsys.asm') + 1 and
          incs.index('core/contracts.inc') == incs.index('app/app.asm') + 1)
    rep.check('source.kernel64_include_position', ok, repr(incs[-6:]))
    rep.check('source.kernel64_app_hosted',
              re.search(r"^APP_HOSTED = 1\ninclude 'app/app\.asm'", k64, re.M)
              is not None)

    # SYS_CALL_MAX is 16 in both frozen copies
    cpu_api = (root / 'kernel' / 'cpu' / 'api.inc').read_text(encoding='utf-8')
    rep.check('source.cpu_api_max_16', 'CPU_SYS_CALL_MAX    = 16' in cpu_api)
    contracts = (root / 'kernel' / 'core' / 'contracts.inc').read_text(
        encoding='utf-8')
    rep.check('source.contracts_max_16',
              'K64_APP_SYS_CALL_MAX    = 16' in contracts)

    # contracts: the APP segment is a pure tail append after the FSYS one
    rep.check('source.contracts_app_after_fsys',
              contracts.find('K64_APP_CODE_PAGES') >
              contracts.find('K64_FSYS_') >= 0)
    rep.check('source.contracts_app_footprint',
              'K64_APP_LAUNCH_PAGES     = 516' in contracts and
              'K64_APP_CODE_PAGES       = 4' in contracts and
              'K64_APP_DATA_PAGES       = 512' in contracts)
    rep.check('source.contracts_app_cross_checks',
              'K64_APP_LAUNCH_PAGES <> APP_LAUNCH_PAGES' in contracts and
              'K64_APP_LAUNCH_PAGES <> K64_APP_CODE_PAGES + K64_APP_DATA_PAGES'
              in contracts and
              'APP_USER_STACK_TOP <> APP_USER_DATA_BASE + APP_USER_DATA_SIZE'
              in contracts)
    rep.check('source.contracts_app_stages',
              'K64_APP_STAGE_IDLE      = 0' in contracts and
              'K64_APP_STAGE_DONE      = 3' in contracts)
    for marker in ('A1 POLL wake split', 'A2 exit handshake', 'A3 teardown',
                   'A4 fault seam', 'A5 RSP0 discipline', 'A6 launch gates',
                   'A7 interactive pacing', 'A8 interactive park'):
        rep.check('source.contracts_%s' % marker.split()[0],
                  marker in contracts)
    # task 141: the interactive park budget is a pure tail append with
    # cross assertions against the module copy and the IBM window
    rep.check('source.contracts_ipoll_chunk',
              'K64_APP_IPOLL_CHUNK     = 0x40000' in contracts and
              'K64_APP_IPOLL_CHUNK <> APP_IPOLL_CHUNK' in contracts and
              'K64_APP_IPOLL_CHUNK >= APP_POLL_CHUNK' in contracts and
              contracts.find('K64_APP_IPOLL_CHUNK') >
              contracts.find('A6 launch gates'))

    # foundation: the app leg runs after the fsys probe, before
    # FOUNDATION-OK; a skipped desktop means a skipped app
    found = (root / 'kernel' / 'core' / 'foundation.inc').read_text(
        encoding='utf-8')
    fpos = found.find('k64_report kmsg_fsysprobe_ok')
    apos = found.find('call    app_init')
    ppos = found.find('vmm_boot_protect', apos)
    spos = found.find('call    app_selftest')
    lpos = found.find('call    desktop_app_boot')
    opos = found.find('k64_report kmsg_app_ok')
    tpos = found.find('k64_report kmsg_foundation_ok')
    rep.check('source.foundation_app_stage_order',
              0 <= fpos < apos < ppos < spos < lpos < opos < tpos)
    rep.check('source.foundation_app_protect',
              'mov     rcx, app_data_start' in found)
    skip = found.find('cmp     qword [desktop_state], 0', fpos)
    rep.check('source.foundation_app_skip_gate', fpos < skip < apos)
    enoent_skip = found.find('cmp     eax, APP_ENOENT', lpos)
    rep.check('source.foundation_app_enoent_skip',
              lpos < enoent_skip < opos,
              'a missing program skips the leg, never panics')
    rep.check('source.foundation_app_markers',
              "db 'K64-APP-OK', 13, 10, 0" in found and
              "db 'K64-APP-SKIP', 13, 10, 0" in found)
    # task 125: the foundation hands the launch to the desktop pump;
    # the direct app_launch call site is gone (the boot leg and the
    # CHIP-8 menu entry share one consumer)
    rep.check('source.foundation_delegates_to_desktop',
              'call    desktop_app_boot' in found and
              'call    app_launch' not in found)

    # the fs self test leg 4 (10) third state: the APP-v1 image carries
    # /PROGRAMS/CHIP8.BIN instead of BIG.BIN, so the frozen plain/b BIG
    # readdir evidence gains one legal branch. The plain (empty) and big
    # (BIG.BIN) paths stay byte for byte; only the new branch is added.
    fs_st = (root / 'kernel' / 'fs' / 'selftest.inc').read_text(
        encoding='utf-8')
    pe = fs_st.find('.prog_empty:')
    pa = fs_st.find('.prog_again:', pe)
    rep.check('source.fs_leg4_prog_labels', 0 <= pe < pa)
    branch = fs_st[pe:pa]
    rep.check('source.fs_leg4_empty_still_legal',
              re.search(r'test\s+edx, edx\s*\n\s*jz\s+\.prog_again',
                        branch) is not None)
    rep.check('source.fs_leg4_chip8_size',
              'cmp     qword [fs_test_dirents + 8], %d' % CHIP8_SIZE in branch)
    rep.check('source.fs_leg4_chip8_name',
              'lea     rdi, [n_fs_chip8]' in branch and
              'repe cmpsb' in branch)
    rep.check('source.fs_leg4_chip8_name_constant',
              "n_fs_chip8    db 'CHIP8   BIN'" in fs_st)
    rep.check('source.fs_leg4_big_branch_intact',
              'cmp     qword [fs_test_big_present], 0' in fs_st and
              'cmp     qword [fs_test_dirents + 8], FS_TEST_BIG_SIZE'
              in fs_st)

    # The desktop owns regular IRQ waits. POLL yields to the scheduler
    # there and owns its own IRQ window in the diagnostic build.
    # The app layout probe separately checks the emitted diagnostic code.
    hits = []
    for path in sorted((root / 'kernel').rglob('*')):
        if path.suffix not in ('.asm', '.inc') or not path.is_file():
            continue
        text = path.read_text(encoding='utf-8')
        for m in re.finditer(r'^\s*sti\b', text, re.M):
            hits.append(str(path.relative_to(root)))
    rep.equal('source.kernel_sti_sites', hits,
              ['kernel/app/app.inc'] +
              ['kernel/desktop/desktop.inc', 'kernel/irq/selftest.inc'])

    # the data region is real zero bytes, never a BSS promise
    data = (root / 'kernel' / 'app' / 'data.inc').read_text(encoding='utf-8')
    rep.check('source.data_no_rb',
              re.search(r'^\s*(\w+:)?\s*rb\s', data, re.M) is None)

    # task 115 adjudication: the fsprobe guest evidence snapshot page.
    # The hosted program's file syscalls reuse fsys_bounce as their
    # payload page, so app_launch preserves the probe snapshot in a
    # private data page before the program can clobber the bounce.
    rep.check('source.data_snapshot_page',
              'app_fs_snapshot:     db 4096 dup 0' in data and
              '(app_fs_snapshot - app_data_start) <> 20 * 4096' in data)
    rep.check('source.data_stack_page_21',
              '(app_syscall_stack - app_data_start) <> 21 * 4096' in data)
    rep.check('source.api_data_pages_22',
              'APP_DATA_PAGES_TOTAL = 22' in api)
    rep.check('source.contracts_data_pages_22',
              'K64_APP_DATA_PAGES_TOTAL = 22' in contracts)

    # every call in the module names its own code or a frozen seam
    allowed = {'app_check_range', 'app_copy_from_user', 'app_redraw_desktop',
               'fs_stat', 'fs_open', 'fs_read', 'fs_close',
               'pmm_alloc_page', 'pmm_free_page',
               'vmm_map_user_page', 'vmm_unmap_user_page', 'mm_vmm_resolve',
               'cpu_set_rsp0', 'cpu_syscall_set_handler',
               'cpu_user_fault_set_handler',
               'video_fill_rect', 'video_blit_rect', 'video_readback_rect',
               'video_puts', 'desktop_cursor_sync', 'desktop_cursor_place',
               'desktop_repaint_base', 'desktop_repaint_echo',
               'input_ioapic_write', 'app_window_copy_row', 'app_window_present',
               'app_task_yield', 'app_window_poll_client'}
    stray = []
    text = (root / 'kernel' / 'app' / 'app.inc').read_text(encoding='utf-8')
    poll_start = text.index('app_h_poll:')
    poll = text[poll_start:text.index('.snapshot:', poll_start)]
    rep.check('source.poll_irq_windows', len(re.findall(r'^\s*sti\b', poll, re.M)) == 1
              and 'call    app_task_yield' in poll
              and not re.search(r'^\s*hlt\b', poll, re.M))
    for m in re.finditer(r'^\s*call\s+(\S+)', text, re.M):
        if m.group(1) not in allowed:
            stray.append(m.group(1))
    rep.check('source.call_targets', not stray, repr(stray))

    # task 125: the snapshot copy sits inside app_init (ONE shot, the
    # foundation runs fsys_probe right before it), NEVER inside
    # app_launch: a relaunch would copy the previous run's file payloads
    # out of the reused bounce page and destroy the guest evidence
    ip = text.find('app_init:')
    rp = text.find('app_redraw_desktop:', ip)
    cp = text.find('mov     rdi, app_fs_snapshot', ip)
    rep.check('source.init_snapshot_copy',
              0 <= ip < cp < rp and
              'mov     rsi, fsys_bounce' in text[ip:rp] and
              'rep     movsq' in text[ip:rp])
    lp = text.find('app_launch:')
    op = text.find('call    fs_open', lp)
    rep.check('source.launch_no_snapshot_copy',
              0 <= lp < op and
              'mov     rdi, app_fs_snapshot' not in text[lp:op] and
              'rep     movsq' not in text[lp:op])

    # the hosted chip8: FS wrappers, no embedded ROM, frozen mirrors
    chip8_asm = (root / 'user' / 'chip8' / 'chip8.asm').read_text(
        encoding='utf-8')
    rep.check('source.chip8_no_file_directive',
              re.search(r'^\s*file\s', chip8_asm, re.M) is None)
    for wrapper in ('sys_open:', 'sys_read:', 'sys_close:'):
        rep.check('source.chip8_%s' % wrapper.rstrip(':'),
                  wrapper in chip8_asm)
    rep.check('source.chip8_rom_paths',
              "db '/ROMS/IBM.CH8', 0" in chip8_asm and
              "db '/ROMS/TETRIS1.CH8', 0" in chip8_asm)
    rep.check('source.chip8_ram_load_target',
              'CHIP8_RAM_VA + ROM_LOAD_ADDR' in chip8_asm)
    rep.check('source.chip8_fs_numbers',
              'SYS_FS_OPEN  = 4' in chip8_asm and
              'SYS_FS_READ  = 5' in chip8_asm and
              'SYS_FS_CLOSE = 6' in chip8_asm)
    rep.check('source.chip8_fs_load_exit', 'EXIT_FS_LOAD = 4' in chip8_asm)
    # task 141 W1: the interactive loop is a FIXED per-wake budget with
    # a per-round 60 Hz accumulator, never a tick-delta off the frozen
    # POLL r8d snapshot (the W1 dead end)
    rep.check('source.chip8_interactive_fixed_budget',
              'mov     ebx, IPS_PER_TICK' in chip8_asm and
              'add     dword [st_timer_acc], TIMER_HZ' in chip8_asm)
    rep.check('source.chip8_no_tick_delta_budget',
              'sub     ecx, dword [st_last_tick]' not in chip8_asm and
              'lea     ebx, [rcx + rcx*4]' not in chip8_asm)
    for inc in ('emu.inc', 'gui.inc', 'input.inc', 'api.inc'):
        t = (root / 'user' / 'chip8' / inc).read_text(encoding='utf-8')
        rep.check('source.chip8_%s_no_file' % inc,
                  re.search(r'^\s*file\s', t, re.M) is None)
    # the DXYN pixel offset is row*64 + (col+bit): the clipped column
    # is saved in r9d before eax becomes the shifted sprite byte, and
    # neither the sprite value nor the bare bit index may be the offset
    # (task 115 game-frame fix)
    emu = (root / 'user' / 'chip8' / 'emu.inc').read_text(encoding='utf-8')
    rep.check('source.chip8_draw_bit_index',
              'mov     r9d, eax' in emu and
              'lea     ecx, [rdi + r9]' in emu and
              'lea     ecx, [rdi + r8]' not in emu and
              'lea     ecx, [rdi + rax]' not in emu)
    uapi = (root / 'user' / 'chip8' / 'api.inc').read_text(encoding='utf-8')
    rep.check('source.chip8_sha_constants_kept',
              len(re.findall(r'^ROM_(IBM|TETRIS)_SHA[0-3]\s*=', uapi, re.M))
              == 8)
    rep.check('source.chip8_syscall_mirrors',
              re.search(r'^SYS_EXIT\s+= 0', uapi, re.M) is not None and
              re.search(r'^SYS_BLIT\s+= 1', uapi, re.M) is not None and
              re.search(r'^SYS_READ_KEY\s+= 2', uapi, re.M) is not None and
              re.search(r'^SYS_POLL\s+= 3', uapi, re.M) is not None)
    rep.check('source.chip8_geometry_mirrors',
              re.search(r'^BLIT_DST_X\s+= 192', uapi, re.M) is not None and
              re.search(r'^FB_WIDTH\s+= 640', uapi, re.M) is not None and
              re.search(r'^FB_HEIGHT\s+= 320', uapi, re.M) is not None and
              re.search(r'^APP_DATA_BASE\s+= 0x100600000', uapi, re.M)
              is not None)

    # the build prebuilds the ring 3 program image
    build = (root / 'build_fasm2.sh').read_text(encoding='utf-8')
    rep.check('source.build_prebuilds_chip8',
              'user/chip8/chip8.asm' in build and
              'build/user-chip8/chip8.bin' in build)
    # task 141 S3: the build also rebuilds the app disk image so a clean
    # checkout can run the gate without a manual image step
    rep.check('source.build_prebuilds_app_image',
              'tools/build-fat16-image.py' in build and
              'tests/stage2-app/app-manifest.json' in build and
              'build/fs-v1/disk-app.img' in build)


def check_integration(rep, asm_, root, out):
    path = out / 'kernel64.bin'
    ok, text = asm_.run(root / 'kernel' / 'kernel64.asm', path)
    if not rep.check('integration.kernel_assembles', ok, text.strip()[-400:]):
        return
    rep.check('integration.kernel_bytes', path.stat().st_size > 0,
              '%d bytes' % path.stat().st_size)
    rep.check('integration.kernel_quiet', 'error' not in text.lower(),
              text.strip()[-200:] or 'no diagnostics')


def check_position_independence(rep, asm_, root, out, low):
    hi_path = out / 'layout-hi.bin'
    ok, text = asm_.run(root / 'tests/stage2-app/layout.asm', hi_path,
                        org=KERNEL_BASE)
    if not rep.check('reloc.assembles_at_kernel_base', ok, text.strip()[-400:]):
        return
    hi = load_image(hi_path, KERNEL_BASE)
    delta = KERNEL_BASE
    labels = M[3:88]      # every address label: app, seams, data
    shifted = all(hi[n] == low[n] + delta for n in labels)
    rep.check('reloc.labels_shift_by_kernel_base', shifted)
    for name in CONSTANTS:
        rep.equal('reloc.%s_unchanged' % name, hi[name], low[name])

    rep.check('reloc.data_bytes_identical',
              hi.data[hi.at('data_start'):hi.at('data_end')] ==
              low.data[low.at('data_start'):low.at('data_end')])

    lo_body = low.data[low.at('check_range'):low.at('data_start')]
    hi_body = hi.data[hi.at('check_range'):hi.at('data_start')]
    if not rep.check('reloc.code_same_length', len(lo_body) == len(hi_body)):
        return
    allowed = set()
    windows = 0
    # mov r64, sign extended imm32 windows: only the low 32 bits are
    # emitted, the visible difference is the base modulo 2**32
    for prefix in (b'\x48\xC7', b'\x41\xC7', b'\x49\xC7'):
        for p in find_all(lo_body, prefix):
            if p + 7 > len(lo_body) or not 0xC0 <= lo_body[p + 2] <= 0xC7:
                continue
            lv = struct.unpack_from('<I', lo_body, p + 3)[0]
            hv = struct.unpack_from('<I', hi_body, p + 3)[0]
            if (lv <= low['data_end'] and
                    (hv - lv) & 0xFFFFFFFF == delta & 0xFFFFFFFF):
                allowed.update(range(p + 3, p + 7))
                windows += 1
    # absolute disp32 addressing with an r12 index (SIB e5) and the
    # selftest handler table compare (SIB c5): the disp32 shifts
    for prefix in (b'\x4A\x89\x14\xE5', b'\x4A\x8B\x14\xE5',
                   b'\x4A\x8B\x0C\xE5', b'\x4A\xC7\x04\xE5',
                   b'\x48\x83\x3C\xC5', b'\x4A\x83\x3C\xE5'):
        for p in find_all(lo_body, prefix):
            if p + 8 > len(lo_body):
                continue
            lv = struct.unpack_from('<I', lo_body, p + 4)[0]
            hv = struct.unpack_from('<I', hi_body, p + 4)[0]
            if (lv <= low['data_end'] and
                    (hv - lv) & 0xFFFFFFFF == delta & 0xFFFFFFFF):
                allowed.update(range(p + 4, p + 8))
                windows += 1
    diffs = [i for i in range(len(lo_body)) if lo_body[i] != hi_body[i]]
    rep.check('reloc.code_diffs_only_shifted_immediates',
              all(i in allowed for i in diffs),
              '%d stray difference bytes' % sum(1 for i in diffs if i not in allowed))
    rep.check('reloc.absolute_immediates_found', windows >= 6,
              '%d shifted absolute windows' % windows)


# ----------------------------------------------------------------------
# negative fixtures
# ----------------------------------------------------------------------

FIXTURES = [
    # defect class: footprint drift. The 516 split is frozen, the mm
    # cross checks and the private 516 assertion fire
    ('launch-pages-drift', 'kernel/app/api.inc',
     'APP_CODE_PAGES   = 4', 'APP_CODE_PAGES   = 5', 'assemble'),
    ('va-domain-drift', 'kernel/app/api.inc',
     'APP_USER_VA_BASE   = 0x100400000', 'APP_USER_VA_BASE   = 0x100401000',
     'assemble'),
    ('budget-drift', 'kernel/app/api.inc',
     'APP_TABLE_BUDGET = 3', 'APP_TABLE_BUDGET = 4', 'assemble'),
    # defect class: the stack is the data domain tail, never a separate
    # map; moving the top breaks the frozen mm layout
    ('stack-top-drift', 'kernel/app/api.inc',
     'APP_USER_STACK_TOP = 0x100800000', 'APP_USER_STACK_TOP = 0x1007FF000',
     'assemble'),
    # defect class: teardown leak. Without the unmap call the frozen
    # user domain stays occupied forever
    ('teardown-unmap-removed', 'kernel/app/app.inc',
     '        call    vmm_unmap_user_page\n', '', 'check'),
    # defect class: frame leak. The physical frames must go back to
    # the allocator on every exit path
    ('teardown-free-removed', 'kernel/app/app.inc',
     '        call    pmm_free_page\n', '', 'check'),
    # defect class: counter discipline. The frozen M1/M2 judge totals
    # must be restored before the verdict
    ('counter-restore-removed', 'kernel/app/app.inc',
     '        mov     rax, [app_saved_fs_reads]\n'
     '        mov     [fs_test_reads], rax\n', '', 'check'),
    # defect class: the ENOENT skip path must also restore the frozen
    # sector counters the directory walk advanced (rdx form)
    ('skip-counter-restore-removed', 'kernel/app/app.inc',
     '        mov     rdx, [app_saved_fs_reads]\n'
     '        mov     [fs_test_reads], rdx\n', '', 'check'),
    # defect class: a missing program must skip non-fatally, never
    # panic the foundation (the FS-v1 images stay bootable)
    ('foundation-enoent-skip-removed', 'kernel/core/foundation.inc',
     '        cmp     eax, APP_ENOENT\n        je      .app_skip\n',
     '        jmp     .fail\n', 'check'),
    # defect class: RSP0 collision. Without the private stack switch
    # the SYSCALL frame lands on the live foundation stack
    ('rsp0-switch-removed', 'kernel/app/app.inc',
     '        mov     rcx, app_syscall_stack_top\n        call    cpu_set_rsp0\n',
     '        mov     rcx, app_syscall_stack_top\n', 'check'),
    # defect class: the EXIT handler must park back into the kernel,
    # a SYSRET would return into a dead user context
    ('exit-sysret-leak', 'kernel/app/app.inc',
     '        mov     [app_exit_code], rax\n'
     '        mov     rsp, [app_launch_rsp]\n'
     '        jmp     qword [app_launch_rip]\n',
     '        mov     [app_exit_code], rax\n'
     '        mov     rsp, [app_launch_rsp]\n        sysretq\n', 'check'),
    # defect class: the fabricated ESC must never advance the real
    # key counters (the frozen evidence contract)
    ('fabricated-key-counter-advance', 'kernel/app/app.inc',
     '        mov     qword [r12 + CPU_FRAME_RDX], APP_SC_ESC\n',
     '        inc     qword [app_key_count]\n'
     '        mov     qword [r12 + CPU_FRAME_RDX], APP_SC_ESC\n', 'check'),
    # defect class: BLIT range guard. A bad source pointer must abort
    # before the bounce copy, never read an unmapped user page
    ('blit-range-guard-removed', 'kernel/app/app.inc',
     '        call    app_check_range\n        test    eax, eax\n'
     '        jnz     .out\n',
     '        call    app_check_range\n', 'check'),
    # defect class: the echo save covers only row 0, a scrolled echo
    # must refuse the launch
    ('echo-row-gate-removed', 'kernel/app/app.inc',
     '        cmp     qword [desktop_echo_row], 0\n'
     '        jne     .early_eio              ; the save covers only row 0\n',
     '        cmp     qword [desktop_echo_row], 0\n', 'check'),
    # defect class: the user ruling. The program is loaded through the
    # FS, an embedded ROM violates the frozen contract
    ('embedded-program', 'kernel/app/app.inc',
     'app_program_path:\n',
     "app_embedded_rom: file '../../user/chip8/roms/ibm.ch8'\n"
     'app_program_path:\n', 'check'),
    # defect class: table budget. The +3 adjudication is frozen
    ('budget-guard-removed', 'kernel/app/app.inc',
     '        cmp     rax, APP_TABLE_BUDGET\n        jne     .fail_eio\n',
     '', 'check'),
    # defect class: the 16 KiB code domain gate
    ('size-gate-removed', 'kernel/app/app.inc',
     '        cmp     rax, APP_USER_CODE_SIZE\n        ja      .early_eio\n',
     '', 'check'),
    # defect class: S3 RSP0 leak. The registration failure path must
    # clear the handlers and restore the foundation TSS.RSP0
    ('rsp0-restore-removed', 'kernel/app/app.inc',
     '        call    cpu_syscall_set_handler\n        test    eax, eax\n'
     '        jnz     .fail_rsp0\n        inc     r13d\n'
     '        mov     ecx, APP_SYS_BLIT\n',
     '        call    cpu_syscall_set_handler\n        test    eax, eax\n'
     '        jnz     .fail\n        inc     r13d\n'
     '        mov     ecx, APP_SYS_BLIT\n', 'check'),
    # defect class: the fault seam must be registered, a ring 3 fault
    # would otherwise panic the kernel instead of returning home
    ('fault-seam-removed', 'kernel/app/app.inc',
     '        lea     rcx, [app_h_fault]\n'
     '        call    cpu_user_fault_set_handler\n',
     '        lea     rcx, [app_h_fault]\n', 'check'),
    # defect class: fd leak. The post-exit sweep must close every
    # leaked slot through fs_close
    ('sweep-close-removed', 'kernel/app/app.inc',
     '        mov     ecx, r12d\n        call    fs_close\n'
     '        inc     qword [app_fd_swept]\n',
     '        mov     ecx, r12d\n        inc     qword [app_fd_swept]\n',
     'check'),
    # defect class: the RTEs must be re-masked right after the wake
    # window, or irq_test_ticks drifts past the frozen 11
    ('poll-remask-removed', 'kernel/app/app.inc',
     '        sti\n        mov     ecx, APP_IPOLL_CHUNK\n'
     '.ispin:\n        pause\n        dec     ecx\n        jnz     .ispin\n'
     '        cli\n'
     '        mov     ecx, APP_IOAPIC_IDX_KBD_LO\n'
     '        mov     edx, APP_RTE_KBD_MASKED\n'
     '        call    input_ioapic_write\n',
     '        sti\n        mov     ecx, APP_IPOLL_CHUNK\n'
     '.ispin:\n        pause\n        dec     ecx\n        jnz     .ispin\n'
     '        cli\n', 'check'),
    # defect class: task 141 W1 regression. Going back to sti;hlt;cli
    # deadlocks under a gdbstub key injection (HLT cannot be woken by a
    # debugger write and the masked LVT leaves no other wake source)
    ('poll-hlt-regression', 'kernel/app/app.inc',
     '        sti\n        mov     ecx, APP_IPOLL_CHUNK\n'
     '.ispin:\n        pause\n        dec     ecx\n        jnz     .ispin\n'
     '        cli\n',
     '        sti\n        hlt\n        cli\n', 'check'),
    # defect class: task 141 W1 regression. A tick-delta budget off the
    # frozen POLL r8d snapshot (irq_test_ticks stays 11) is always zero:
    # tetris would never execute a single instruction after Enter
    ('interactive-tick-delta-regression', 'user/chip8/chip8.asm',
     '        mov     ebx, IPS_PER_TICK     ; fixed budget per wake round\n',
     '        mov     ecx, r8d\n'
     '        sub     ecx, dword [st_last_tick]\n'
     '        mov     dword [st_last_tick], r8d\n'
     '        mov     ebx, ecx\n', 'check'),
    # defect class: the handlers must be unregistered on exit, the
    # next launch (or the resident shell) would call a dead frame
    ('unregister-removed', 'kernel/app/app.inc',
     '        mov     ecx, APP_SYS_EXIT\n        xor     edx, edx\n'
     '        call    cpu_syscall_set_handler\n'
     '        mov     ecx, APP_SYS_BLIT\n',
     '        mov     ecx, APP_SYS_BLIT\n', 'check'),
    # defect class: task 125 relaunch gate. Without the DONE compare a
    # finished run can never launch again (IDLE-only gate -> EALREADY)
    ('relaunch-done-gate-removed', 'kernel/app/app.inc',
     '        cmp     qword [app_stage], APP_STAGE_DONE\n'
     '        jne     .ealready\n', '', 'check'),
    # defect class: task 125 relaunch reset. Stale BLIT evidence would
    # let a second run pass the verdict without drawing anything
    ('relaunch-reset-removed', 'kernel/app/app.inc',
     '        mov     qword [app_blit_count], 0\n', '', 'check'),
    # defect class: task 125 redraw delegation. The exit frame must be
    # the full v2 base, not a partial or flat-color desktop
    ('redraw-delegation-removed', 'kernel/app/app.inc',
     '        call    desktop_repaint_base\n', '', 'check'),
    # defect class: task 125 call-site migration regression. Calling
    # app_launch directly bypasses the desktop launch pump
    ('foundation-direct-launch-regression', 'kernel/core/foundation.inc',
     '        call    desktop_app_boot\n', '        call    app_launch\n',
     'check'),
    # defect class: task 125 hidden one-shot state. Removing the init
    # snapshot leaves the judge reading a clobbered bounce page
    ('init-snapshot-removed', 'kernel/app/app.inc',
     '        cld\n'
     '        mov     rsi, fsys_bounce\n'
     '        mov     rdi, app_fs_snapshot\n'
     '        mov     ecx, 4096 / 8\n'
     '        rep     movsq\n', '', 'check'),
    # defect class: task 125 relaunch snapshot regression. A per-launch
    # copy re-snapshots the previous run's payloads on every relaunch
    ('launch-snapshot-regression', 'kernel/app/app.inc',
     '        mov     qword [app_interactive], 0\n',
     '        mov     qword [app_interactive], 0\n'
     '        cld\n'
     '        mov     rsi, fsys_bounce\n'
     '        mov     rdi, app_fs_snapshot\n'
     '        mov     ecx, 4096 / 8\n'
     '        rep     movsq\n', 'check'),
]


def run_fixtures(rep, root, out, fasmg, fasm2_dir):
    checker = root / 'tests' / 'stage2-app' / 'check.py'
    for name, rel, old, new, where in FIXTURES:
        with tempfile.TemporaryDirectory(prefix='stage2-app-%s-' % name) as tmp:
            tree = Path(tmp)
            (tree / 'tests').mkdir(parents=True)
            shutil.copytree(root / 'kernel', tree / 'kernel')
            shutil.copytree(root / 'tests' / 'stage2-app',
                            tree / 'tests' / 'stage2-app')
            # the model, the disk images and the fresh program image
            # need the user sources and the build artifacts too
            shutil.copytree(root / 'user' / 'chip8', tree / 'user' / 'chip8')
            # fsys.inc embeds the fsprobe image: the sandbox needs the
            # source and the prebuilt artifact exactly like stage2-fsys
            shutil.copytree(root / 'user' / 'fsprobe', tree / 'user' / 'fsprobe')
            (tree / 'build' / 'user-fsprobe').mkdir(parents=True)
            shutil.copy2(root / 'build' / 'user-fsprobe' / 'fsprobe.bin',
                         tree / 'build' / 'user-fsprobe' / 'fsprobe.bin')
            (tree / 'build' / 'user-chip8').mkdir(parents=True)
            shutil.copy2(root / 'build' / 'user-chip8' / 'chip8.bin',
                         tree / 'build' / 'user-chip8' / 'chip8.bin')
            (tree / 'build' / 'fs-v1').mkdir(parents=True)
            for img in ('disk.img', 'disk-big.img', 'disk-app.img'):
                shutil.copy2(root / 'build' / 'fs-v1' / img,
                             tree / 'build' / 'fs-v1' / img)
            shutil.copy2(root / 'build_fasm2.sh', tree / 'build_fasm2.sh')
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
    out = Path(args.out).resolve() if args.out else root / 'build' / 'stage2-app'
    out.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(Path(__file__).resolve().parent))

    asm_ = find_assembler(root, args.fasm2_dir)
    if asm_ is None:
        print('check.py: no fasm2 host found, set FASMG or --fasm2-dir', file=sys.stderr)
        return 2

    rep = Report()
    rep.check('assembler.available', True, str(asm_.fasmg))
    rep.check('assembler.include', (asm_.fasm2_dir / 'include' / 'fasm2.inc').is_file(),
              str(asm_.fasm2_dir))

    # the program image must be fresh: rebuild it exactly like
    # build_fasm2.sh does before the disk comparison consumes it
    chip8_bin = root / 'build' / 'user-chip8' / 'chip8.bin'
    chip8_bin.parent.mkdir(parents=True, exist_ok=True)
    ok, text = asm_.run(root / 'user' / 'chip8' / 'chip8.asm', chip8_bin)
    if not rep.check('assembler.chip8', ok, text.strip()[-400:]):
        rep.print(True)
        return 1
    fresh_chip8 = chip8_bin.read_bytes()

    low_path = out / 'layout.bin'
    ok, text = asm_.run(root / 'tests/stage2-app/layout.asm', low_path)
    if not rep.check('assembler.probe', ok, text.strip()[-600:]):
        rep.print(True)
        return 1
    rep.check('assembler.probe_bytes', low_path.stat().st_size > 0,
              '%d bytes' % low_path.stat().st_size)
    rep.check('assembler.probe_quiet', 'error' not in text.lower(),
              text.strip()[-200:] or 'no diagnostics')

    img = load_image(low_path)
    check_manifest(rep, img)
    check_data(rep, img, root, fresh_chip8)
    check_frames(rep, img)
    check_check_range(rep, img)
    check_copy_from_user(rep, img)
    check_exit_and_fault(rep, img)
    check_blit(rep, img)
    check_read_key(rep, img)
    check_poll(rep, img)
    check_init(rep, img)
    check_redraw(rep, img)
    check_launch(rep, img)
    check_selftest(rep, img)
    check_module_hygiene(rep, img)
    check_model(rep, img)

    # the chip8 reference model self test, then the FS-read integration
    model = subprocess.run(
        [sys.executable, str(root / 'tests' / 'stage2-app' / 'chip8_model.py')],
        capture_output=True, text=True)
    rep.check('model.selftest_passed',
              model.returncode == 0 and
              'MODEL SELFTEST PASSED' in (model.stdout or ''),
              (model.stdout or model.stderr).strip()[-300:])
    check_disk_and_model(rep, root, fresh_chip8)

    # task 141 W1: the interactive round-boundary probe. Assemble the
    # program with a trailing dq of the main_loop offset, verify the
    # leading bytes still equal the fresh program, and publish the
    # offset merged with the model probes for the QEMU interactive leg.
    probe_path = out / 'interactive-probe.bin'
    ok, text = asm_.run(root / 'tests' / 'stage2-app' / 'interactive-probe.asm',
                        probe_path)
    if rep.check('assembler.interactive_probe', ok, text.strip()[-400:]):
        blob = probe_path.read_bytes()
        rep.check('probe.interactive_program_prefix',
                  blob[:-8] == fresh_chip8,
                  '%d vs %d bytes' % (len(blob) - 8, len(fresh_chip8)))
        main_loop_off = struct.unpack_from('<Q', blob, len(blob) - 8)[0]
        rep.check('probe.main_loop_inside_code',
                  0 < main_loop_off < CODE_SIZE,
                  '%#x' % main_loop_off)
        ip_path = root / 'build' / 'user-chip8' / 'tetris-int-probes.json'
        if ip_path.is_file():
            probes = json.loads(ip_path.read_text())
            probes['main_loop_offset'] = main_loop_off
            probes['main_loop_va'] = CODE_BASE + main_loop_off
            (root / 'build' / 'user-chip8' / 'interactive-probes.json'
             ).write_text(json.dumps(probes, indent=2) + '\n')
            rep.check('probe.interactive_probes_written', True,
                      'main_loop VA %#x' % (CODE_BASE + main_loop_off))

    check_sources(rep, root)
    check_integration(rep, asm_, root, out)
    check_position_independence(rep, asm_, root, out, img)

    if not args.no_fixtures:
        run_fixtures(rep, root, out, asm_.fasmg, asm_.fasm2_dir)

    print('stage2-app: layout.bin %d bytes, code %d bytes, data %d bytes' %
          (len(img.data), img['data_start'] - img['check_range'],
           img['data_end'] - img['data_start']))
    return 0 if rep.print(args.verbose) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
