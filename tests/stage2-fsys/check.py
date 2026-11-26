#!/usr/bin/env python3
"""Stage 2 FSYS module checks, task 133, contract FS-v1 M3. No machine
is started.

Everything here is measured, nothing is taken from a comment. The probe
tests/stage2-fsys/layout.asm assembles kernel/fsys/fsys.asm exactly as
the kernel will include it (behind cpu.asm, mm.asm, blk.asm and fs.asm,
whose seams it consumes) and appends a manifest of label addresses.
This script then

  * compares the manifest constants and every data label offset against
    the fixed values of contract FS-v1 M3, repeated here independently
    of the assembly sources;
  * decodes the emitted bytes: the full register frames, the frozen
    error map, the full-range user pointer validation (domain bounds,
    4 KiB leaf, US, RW), the bounce discipline of every handler, the
    six registrations on numbers 4..9, the probe's nine page footprint,
    the +3 table budget compare, the private RSP0 switch, the iretq
    frame and the evidence read-back; every call target must stay
    inside the module or name a frozen cpu/mm/fs seam, and every RIP
    relative write must land inside the fsys data region (or restore
    the two frozen sector counters);
  * replays the frozen semantics in a pure Python model: the whole
    error map including out-of-table statuses, the range validator
    matrix, the READ/READDIR bounce chunking with the short-read and
    short-emit rules and the adapter decision table;
  * checks the embedded fsprobe image byte for byte against a fresh
    assembly of user/fsprobe/fsprobe.asm and against the frozen ROM
    reference bytes;
  * checks the data region is 3 real zero pages, page aligned at both
    ends, with the bounce page and the private syscall stack inside;
  * checks the module links at the kernel base with identical bytes
    except the absolute-address immediates, which shift by the base;
  * assembles the whole kernel to prove the contract copies and the
    core closing assertion still agree;
  * re-runs the whole pipeline on purpose-broken copies of the sources
    and requires every one of them to fail.

The contract values below are deliberately duplicated instead of
imported: if the module and this file drift apart, one of them has to be
fixed explicitly.

Usage: check.py [--root DIR] [--out DIR] [--fasm2-dir DIR] [--no-fixtures]
"""

import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

# ----------------------------------------------------------------------
# contract FS-v1 M3, fixed here independently of the sources
# ----------------------------------------------------------------------

PAGE = 0x1000
KERNEL_BASE = 0xFFFFFFFF80000000

SYS_EXIT, SYS_OPEN, SYS_READ = 0, 4, 5
SYS_CLOSE, SYS_SEEK, SYS_STAT, SYS_READDIR = 6, 7, 8, 9
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

# the frozen K64 mapping of every FS_E* status, index = -status
FS_TO_K64 = [0, -1, -9, -3, -1, -6, -6, -9, -1, -9, -1, -11, -9]
ERRMAP_BYTES = bytes(v & 0xFF for v in FS_TO_K64)

VA_BASE = 0x100400000
VA_LIMIT = 0x100800000
CODE_BASE = 0x100400000
CODE_SIZE = 0x4000
DATA_BASE = 0x100600000
STACK_TOP = 0x100800000
STACK_SIZE = 0x4000
CODE_PAGES, DATA_PAGES, STACK_PAGES = 4, 1, 4
PROBE_PAGES = 9
TABLE_BUDGET = 3
FP_MASK_ALL = 0x7FF
FP_EVIDENCE = 8
USER_CS, USER_SS, USER_RFLAGS = 0x33, 0x2B, 0x2

DATA_PAGES_TOTAL = 3
BOUNCE_SIZE = 4096
SYSCALL_STACK = 4096
DATA_SIZE = DATA_PAGES_TOTAL * PAGE

FRAME_RDI, FRAME_RSI, FRAME_RDX, FRAME_RAX, FRAME_SIZE = 72, 80, 104, 120, 184
MAP_US, MAP_RW, MAP_NX = 4, 2, 1 << 63
STATE_TABLE_ALLOCS = 40
STATE_TABLE_DROPS = 48
MAX_FDS, PATH_MAX, STAT_SIZE, DIRENT_SIZE, SEEK_END = 8, 128, 64, 32, 2

# the data layout, offsets from fsys_data_start
DATA_LABELS = {
    'ready': 0, 'probe_rsp': 8, 'probe_rip': 16, 'exit_code': 24,
    'saved_rsp0': 32, 'saved_fs_reads': 40, 'saved_blk_reads': 48,
    'table_base': 56, 'table_delta': 64, 'probe_mask': 72, 'probe_fail': 80,
    'resolve_rec': 88, 'path_buf': 152, 'stat_buf': 280, 'pages': 344,
    'evidence': 416, 'pages_held': 480, 'drops_base': 488,
    'table_released': 496, 'bounce': 4096, 'syscall_stack': 8192,
    'syscall_stack_top': 12288,
}

MAGIC = b'K64FSY1'
MANIFEST_VERSION = 1

# manifest entry order, see tests/stage2-fsys/layout.asm
M = [
    'magic', 'version',
    'map_error', 'check_range', 'copy_path', 'copy_to_user',
    'h_open', 'h_read', 'h_close', 'h_seek', 'h_stat', 'h_readdir',
    'h_exit', 'init', 'probe', 'errmap', 'errmap_end',
    'probe_image', 'probe_image_end', 'data_start', 'data_end',
    'ready', 'probe_rsp', 'probe_rip', 'exit_code', 'saved_rsp0',
    'saved_fs_reads', 'saved_blk_reads', 'table_base', 'table_delta',
    'probe_mask', 'probe_fail', 'resolve_rec', 'path_buf', 'stat_buf',
    'pages', 'evidence', 'pages_held', 'drops_base', 'table_released',
    'bounce', 'syscall_stack', 'syscall_stack_top',
    'fs_open', 'fs_read', 'fs_seek', 'fs_close', 'fs_stat', 'fs_readdir',
    'fs_test_reads', 'blk_test_reads',
    'mm_vmm_resolve', 'vmm_map_user_page', 'pmm_alloc_page',
    'vmm_unmap_user_page', 'pmm_free_page', 'mm_vmm_state',
    'cpu_syscall_set_handler', 'cpu_set_rsp0', 'cpu_tss_rsp0',
    'cpu_syscall_handlers',
    'sys_exit', 'sys_open', 'sys_read', 'sys_close', 'sys_seek',
    'sys_stat', 'sys_readdir',
    'ok', 'einval', 'enomem', 'enoent', 'eperm', 'enotsup', 'enotready',
    'ealready', 'eio', 'efault',
    'va_base', 'va_limit', 'code_base', 'code_size', 'data_base',
    'stack_top', 'stack_size',
    'code_pages', 'data_pages', 'stack_pages', 'probe_pages',
    'table_budget', 'fp_mask_all', 'fp_evidence',
    'user_cs', 'user_ss', 'user_rflags',
    'data_pages_total', 'bounce_size', 'syscall_stack_size',
    'cpu_sys_call_max', 'frame_rdi', 'frame_rsi', 'frame_rdx',
    'frame_rax', 'frame_size',
    'map_us', 'map_rw', 'map_nx', 'mm_table_budget', 'state_table_allocs',
    'state_table_drops',
    'max_fds', 'path_max', 'stat_size', 'dirent_size', 'seek_end',
    'kernel_base',
]

CONSTANTS = {
    'sys_exit': SYS_EXIT, 'sys_open': SYS_OPEN, 'sys_read': SYS_READ,
    'sys_close': SYS_CLOSE, 'sys_seek': SYS_SEEK, 'sys_stat': SYS_STAT,
    'sys_readdir': SYS_READDIR,
    'ok': OK, 'einval': EINVAL, 'enomem': ENOMEM, 'enoent': ENOENT,
    'eperm': EPERM, 'enotsup': ENOTSUP, 'enotready': ENOTREADY,
    'ealready': EALREADY, 'eio': EIO, 'efault': EFAULT,
    'va_base': VA_BASE, 'va_limit': VA_LIMIT, 'code_base': CODE_BASE,
    'code_size': CODE_SIZE, 'data_base': DATA_BASE,
    'stack_top': STACK_TOP, 'stack_size': STACK_SIZE,
    'code_pages': CODE_PAGES, 'data_pages': DATA_PAGES,
    'stack_pages': STACK_PAGES, 'probe_pages': PROBE_PAGES,
    'table_budget': TABLE_BUDGET, 'fp_mask_all': FP_MASK_ALL,
    'fp_evidence': FP_EVIDENCE,
    'user_cs': USER_CS, 'user_ss': USER_SS, 'user_rflags': USER_RFLAGS,
    'data_pages_total': DATA_PAGES_TOTAL, 'bounce_size': BOUNCE_SIZE,
    'syscall_stack_size': SYSCALL_STACK,
    'cpu_sys_call_max': SYS_CALL_MAX, 'frame_rdi': FRAME_RDI,
    'frame_rsi': FRAME_RSI, 'frame_rdx': FRAME_RDX,
    'frame_rax': FRAME_RAX, 'frame_size': FRAME_SIZE,
    'map_us': MAP_US, 'map_rw': MAP_RW, 'map_nx': MAP_NX,
    'mm_table_budget': TABLE_BUDGET, 'state_table_allocs': STATE_TABLE_ALLOCS,
    'state_table_drops': STATE_TABLE_DROPS,
    'max_fds': MAX_FDS, 'path_max': PATH_MAX, 'stat_size': STAT_SIZE,
    'dirent_size': DIRENT_SIZE, 'seek_end': SEEK_END,
    'kernel_base': KERNEL_BASE,
}

FSYS_ENTER = bytes.fromhex('55 53 56 57 41 54 41 55 41 56 41 57 48 83 EC 28')
FSYS_LEAVE = bytes.fromhex('48 83 C4 28 41 5F 41 5E 41 5D 41 5C 5F 5E 5B 5D')
MOVSXD_RAX_EAX = bytes.fromhex('48 63 C0')
RET = 0xC3

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
        print('stage2-fsys: %d checks, %d failed' % (total, len(self.failures)))
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
            cmd += ['-i', 'FSYS_PROBE_ORG=%#x' % org]
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
    rep.equal('manifest.entry_count', len(M), 116)
    for name, want in CONSTANTS.items():
        rep.equal('manifest.%s' % name, img[name], want & 0xFFFFFFFFFFFFFFFF)

    for name, off in DATA_LABELS.items():
        rep.equal('layout.%s_offset' % name,
                  img[name] - img['data_start'], off)

    lo, hi = img['map_error'], img['probe_image_end']
    rep.check('layout.code_non_empty', hi > lo, '%#x..%#x' % (lo, hi))
    order = ['map_error', 'check_range', 'copy_path', 'copy_to_user',
             'h_open', 'h_read', 'h_close', 'h_seek', 'h_stat', 'h_readdir',
             'h_exit', 'init', 'probe', 'errmap', 'probe_image']
    chain = [img[name] for name in order]
    rep.check('layout.function_order', chain == sorted(chain) and
              all(lo <= v < hi for v in chain),
              'emission order: mapper, safety, handlers, init, probe')
    rep.check('layout.code_before_data', hi <= img['data_start'])
    rep.check('layout.data_page_aligned',
              img['data_start'] % PAGE == 0 and img['data_end'] % PAGE == 0)
    rep.equal('layout.data_size', img['data_end'] - img['data_start'], DATA_SIZE)
    rep.equal('layout.module_ends_with_data', img.at('data_end'), img.probe_at)
    for name in ('fs_open', 'fs_read', 'fs_seek', 'fs_close', 'fs_stat',
                 'fs_readdir', 'mm_vmm_resolve', 'vmm_map_user_page',
                 'pmm_alloc_page', 'vmm_unmap_user_page', 'pmm_free_page',
                 'cpu_syscall_set_handler', 'cpu_set_rsp0'):
        rep.check('layout.%s_before_module' % name, 0 < img[name] < lo,
                  '%#x' % img[name])
    rep.equal('layout.bounce_is_page_two', img['bounce'] - img['data_start'], PAGE)
    rep.equal('layout.stack_is_page_three',
              img['syscall_stack'] - img['data_start'], 2 * PAGE)
    rep.equal('layout.stack_top_closes_data', img['syscall_stack_top'], img['data_end'])
    rep.equal('layout.path_buf_budget', img['stat_buf'] - img['path_buf'], PATH_MAX)
    rep.equal('layout.stat_buf_budget', img['pages'] - img['stat_buf'], STAT_SIZE)
    rep.equal('layout.pages_array', img['evidence'] - img['pages'], PROBE_PAGES * 8)
    rep.equal('layout.evidence_array', img['bounce'] - img['evidence'],
              FP_EVIDENCE * 8 + (PAGE - 480))


def check_data(rep, img, root):
    d = img.data
    start, end = img.at('data_start'), img.at('data_end')
    block = d[start:end]
    rep.check('data.all_zero_real_bytes', block == bytes(DATA_SIZE),
              '%d non-zero bytes' % sum(1 for b in block if b))
    # the frozen error map, byte for byte
    a, b = img.at('errmap'), img.at('errmap_end')
    rep.equal('data.errmap_size', b - a, 13)
    rep.check('data.errmap_bytes', d[a:b] == ERRMAP_BYTES,
              d[a:b].hex(' '))
    # the embedded user image is the freshly assembled fsprobe, and it
    # carries the frozen ROM reference bytes
    ia, ib = img.at('probe_image'), img.at('probe_image_end')
    embedded = d[ia:ib]
    fresh = (root / 'build' / 'user-fsprobe' / 'fsprobe.bin').read_bytes()
    rep.check('data.probe_image_matches_fsprobe', embedded == fresh,
              '%d vs %d bytes' % (len(embedded), len(fresh)))
    rep.check('data.probe_image_fits_code_domain', 0 < len(embedded) <= CODE_SIZE,
              '%d bytes' % len(embedded))
    rom = (root / 'user' / 'chip8' / 'roms' / 'ibm.ch8').read_bytes()
    rep.equal('data.rom_reference_size', len(rom), 132)
    rep.check('data.probe_carries_rom_reference', rom in embedded,
              'the frozen ibm.ch8 bytes inside the user image')
    # task 138 W1: the probe drives SEEK_SET past 4 GiB and compares
    # the full 64 bit return against the requested offset
    rep.check('data.probe_seek_beyond_4g',
              bytes.fromhex('48 BE 00 00 00 00 01 00 00 00') in embedded and
              bytes.fromhex('48 39 F0') in embedded,
              'mov rsi, 0x100000000; cmp rax, rsi')


def check_frames(rep, img):
    d = img.data
    framed = ['check_range', 'copy_path', 'copy_to_user',
              'h_open', 'h_read', 'h_close', 'h_seek', 'h_stat',
              'h_readdir', 'init', 'probe']
    nxt = {'check_range': 'copy_path', 'copy_path': 'copy_to_user',
           'copy_to_user': 'h_open', 'h_open': 'h_read', 'h_read': 'h_close',
           'h_close': 'h_seek', 'h_seek': 'h_stat', 'h_stat': 'h_readdir',
           'h_readdir': 'h_exit', 'init': 'probe', 'probe': 'errmap'}
    for name in framed:
        at = img.at(name)
        rep.check('frame.%s_full_frame' % name,
                  d[at:at + len(FSYS_ENTER)] == FSYS_ENTER)
        end = img.at(nxt[name])
        rep.check('frame.%s_leave_ret' % name,
                  d.find(FSYS_LEAVE + bytes((RET,)), at, end) >= 0)
    # the EXIT handler is a leaf that never returns
    at = img.at('h_exit')
    rep.check('frame.h_exit_is_leaf',
              d[at:at + len(FSYS_ENTER)] != FSYS_ENTER)
    # every syscall handler sign extends its 32 bit errno into RAX on
    # the error path only (task 138 W1/S1)
    for name in ('h_open', 'h_read', 'h_close', 'h_seek', 'h_stat',
                 'h_readdir'):
        at, end = region(img, name, nxt[name])
        rep.check('frame.%s_movsxd_error_tail' % name,
                  d.find(MOVSXD_RAX_EAX + FSYS_LEAVE, at, end) >= 0)
    # the success paths return the full 64 bit result, never through
    # the movsxd: SEEK returns rdx, READ/READDIR return r12
    at, end = region(img, 'h_seek', nxt['h_seek'])
    rep.check('frame.h_seek_full_width_success',
              d.find(bytes.fromhex('48 89 D0') + FSYS_LEAVE + bytes((RET,)),
                     at, end) >= 0, 'mov rax, rdx; fsys_leave; ret')
    for name in ('h_read', 'h_readdir'):
        at, end = region(img, name, nxt[name])
        rep.check('frame.%s_full_width_success' % name,
                  d.find(bytes.fromhex('4C 89 E0') + FSYS_LEAVE + bytes((RET,)),
                         at, end) >= 0, 'mov rax, r12; fsys_leave; ret')


def check_map_error(rep, img):
    at, end = region(img, 'map_error', 'check_range')
    d = img.data
    body = d[at:end]
    rep.check('map_error.negates_the_status', body.startswith(
        bytes.fromhex('F7 D8 83 F8 0C 77')), 'neg eax; cmp eax, 12; ja')
    rep.equal('map_error.loads_the_table',
              len(rip_refs(img, at, end, b'\x48\x8D', 0x0D, 0, 'errmap')), 1)
    rep.check('map_error.sign_extends_the_byte',
              bytes.fromhex('0F BE 04 01') in body,
              'movsx eax, byte [rcx+rax]')
    rep.check('map_error.out_of_table_is_eio',
              (b'\xB8' + struct.pack('<i', EIO)) in body)
    rep.equal('map_error.two_returns', body.count(bytes((RET,))), 2)


def check_check_range(rep, img):
    at, end = region(img, 'check_range', 'copy_path')
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
    # the compare alone is not the guard: the jne .fault must sit
    # directly behind it (short 75 or near 0F 85), or a supervisor/RO
    # page walks straight through
    def jne_at(pos):
        return d[pos] == 0x75 or d[pos:pos + 2] == b'\x0F\x85'
    rep.check('range.demands_4k_leaves',
              any(t == rec + 8 and imm == struct.pack('<I', PAGE) and
                  jne_at(s + 11)
                  for s, t, imm in sites), 'cmp qword [rec+8], 0x1000; jne')
    us = rip_sites(img, at, end, b'\x48\x83', 0x3D, 1)
    rep.check('range.demands_user_pages',
              any(t == rec + 24 and imm == b'\x01' and
                  jne_at(s + 8) for s, t, imm in us),
              'cmp qword [rec+24], 1; jne .fault')
    rep.check('range.write_targets_demand_rw',
              any(t == rec + 16 and imm == b'\x01' and
                  jne_at(s + 8) for s, t, imm in us),
              'cmp qword [rec+16], 1; jne .fault')
    rep.check('range.write_flag_gates_rw',
              bytes.fromhex('45 85 F6 74') in body, 'test r14d, r14d; jz')
    rep.check('range.page_walk_advance',
              bytes.fromhex('4C 89 E0 48 25 00 F0 FF FF 48 05 00 10 00 00 49 89 C4')
              in body, 'cursor = (cursor & -4096) + 4096')
    rep.check('range.fault_is_efault',
              (b'\xB8' + struct.pack('<i', EFAULT)) in body)


def check_copy_path(rep, img):
    at, end = region(img, 'copy_path', 'copy_to_user')
    d = img.data
    body = d[at:end]
    rep.check('path.budget_compare',
              (b'\x41\x81\xFE' + struct.pack('<I', PATH_MAX) + b'\x73') in body,
              'cmp r14d, FS_PATH_MAX; jae .einval')
    rep.check('path.lands_in_the_bounce_buffer',
              abs32(bytes.fromhex('49 C7 C5'), img['path_buf']) in body,
              'mov r13, fsys_path_buf')
    rep.check('path.resolves_every_byte',
              abs32(bytes.fromhex('48 C7 C2'), img['resolve_rec']) in body)
    calls = [t for _, t in call_sites(img, at, end)]
    rep.equal('path.only_resolves', calls, [img.at('mm_vmm_resolve')])
    rec = img.at('resolve_rec')
    sites = rip_sites(img, at, end, b'\x48\x81', 0x3D, 4)
    rep.check('path.demands_4k_leaves',
              any(t == rec + 8 and imm == struct.pack('<I', PAGE)
                  for _, t, imm in sites))
    us = rip_sites(img, at, end, b'\x48\x83', 0x3D, 1)
    rep.check('path.demands_user_pages',
              any(t == rec + 24 and imm == b'\x01' for _, t, imm in us))
    rep.check('path.identity_read_of_the_user_byte',
              bytes.fromhex('8A 00 41 88 45 00') in body,
              'mov al, [rax]; mov [r13], al')
    # rec+48 already carries the page offset: adding it a second time
    # reads a byte 2*offset into the page (the task-133 QEMU catch)
    rep.check('path.no_double_page_offset',
              bytes.fromhex('4C 89 E0 25 FF 0F 00 00') not in body,
              'the cursor offset must not be folded in again')
    rep.equal('path.leaf_address_load',
              len(rip_refs(img, at, end, b'\x48\x8B', 0x05, 0,
                           'resolve_rec', 48)), 1)
    rep.check('path.stops_at_the_nul',
              bytes.fromhex('84 C0 75') in body, 'test al, al; jnz .loop')
    rep.check('path.no_nul_is_einval',
              (b'\xB8' + struct.pack('<i', EINVAL)) in body)
    rep.check('path.bad_page_is_efault',
              (b'\xB8' + struct.pack('<i', EFAULT)) in body)


def check_copy_to_user(rep, img):
    at, end = region(img, 'copy_to_user', 'h_open')
    d = img.data
    body = d[at:end]
    rep.check('copy.forward_direction', body[len(FSYS_ENTER)] == 0xFC, 'cld')
    calls = [t for _, t in call_sites(img, at, end)]
    rep.equal('copy.only_resolves', calls, [img.at('mm_vmm_resolve')])
    rec = img.at('resolve_rec')
    # as in check_range: the jne .fault must follow the compare directly
    def jne_at(pos):
        return d[pos] == 0x75 or d[pos:pos + 2] == b'\x0F\x85'
    sites = rip_sites(img, at, end, b'\x48\x81', 0x3D, 4)
    rep.check('copy.demands_4k_leaves',
              any(t == rec + 8 and imm == struct.pack('<I', PAGE) and
                  jne_at(s + 11) for s, t, imm in sites))
    us = rip_sites(img, at, end, b'\x48\x83', 0x3D, 1)
    rep.check('copy.demands_user_pages',
              any(t == rec + 24 and imm == b'\x01' and
                  jne_at(s + 8) for s, t, imm in us))
    rep.check('copy.rechecks_rw',
              any(t == rec + 16 and imm == b'\x01' and
                  jne_at(s + 8) for s, t, imm in us))
    rep.check('copy.page_offset_and_room',
              bytes.fromhex('4C 89 E7 81 E7 FF 0F 00 00 48 C7 C3 00 10 00 00 48 29 FB')
              in body, 'offset = dst & 0xFFF; room = 4096 - offset')
    rep.equal('copy.leaf_address_load',
              len(rip_refs(img, at, end, b'\x48\x8B', 0x3D, 0,
                           'resolve_rec', 48)), 1)
    rep.check('copy.no_double_page_offset',
              bytes.fromhex('48 03 3D') not in body,
              'rec+48 already carries the page offset')
    rep.check('copy.chunk_is_min_room_left',
              bytes.fromhex('4C 39 F3 49 0F 47 DE') in body,
              'cmp rbx, r14; cmova rbx, r14')
    rep.check('copy.rep_movsb', bytes.fromhex('F3 A4') in body)
    rep.check('copy.cursor_updates',
              bytes.fromhex('49 01 DC 49 01 DD 49 29 DE') in body)
    rep.check('copy.bad_page_is_efault',
              (b'\xB8' + struct.pack('<i', EFAULT)) in body)


def check_handlers(rep, img):
    d = img.data

    def partial_read_guards(body):
        # both error paths test the delivered total first (S2): a
        # nonzero total jumps to .done, never to the errno mapping
        sites = find_all(body, bytes.fromhex('4D 85 E4'))
        return len(sites) == 2 and all(
            body[p + 3] == 0x75 or body[p + 3:p + 5] == b'\x0F\x85'
            for p in sites)

    def delivered_after_copy(body, at, end):
        # the total advances only after copy_to_user succeeded (S2)
        adds = [at + p for p in find_all(body, bytes.fromhex('49 01 DC'))]
        copies = [s for s, t in call_sites(img, at, end)
                  if t == img.at('copy_to_user')]
        return len(adds) == 1 and len(copies) == 1 and adds[0] > copies[0]

    # ---------------- OPEN ------------------------------------------
    at, end = region(img, 'h_open', 'h_read')
    body = d[at:end]
    rep.check('open.frame_in_r12', bytes.fromhex('49 89 CC') in body)
    rep.check('open.flags_from_frame',
              bytes.fromhex('41 8B 74 24 50 85 F6 75') in body,
              'mov esi, [r12+80]; test esi, esi; jnz .enotsup')
    rep.check('open.path_from_frame', bytes.fromhex('49 8B 4C 24 48') in body,
              'mov rcx, [r12+72]')
    calls = [t for _, t in call_sites(img, at, end)]
    rep.equal('open.call_chain', calls,
              [img.at('copy_path'), img.at('fs_open'), img.at('map_error')])
    rep.check('open.opens_the_bounce_path_readonly',
              (abs32(bytes.fromhex('48 C7 C1'), img['path_buf']) +
               bytes.fromhex('31 D2')) in body,
              'mov rcx, fsys_path_buf; xor edx, edx')
    rep.check('open.fd_from_rdx', bytes.fromhex('89 D0') in body,
              'mov eax, edx')
    rep.check('open.write_flags_enotsup',
              (b'\xB8' + struct.pack('<i', ENOTSUP)) in body)

    # ---------------- READ ------------------------------------------
    at, end = region(img, 'h_read', 'h_close')
    body = d[at:end]
    rep.check('read.frame_arguments',
              bytes.fromhex('4C 8B 79 48 4C 8B 69 50 4C 8B 71 68') in body,
              'fd=[rcx+72], buf=[rcx+80], len=[rcx+104]')
    rep.check('read.fd_bound', bytes.fromhex('49 83 FF 08 0F 83') in body,
              'cmp r15, 8; jae .einval')
    rep.check('read.negative_length', bytes.fromhex('4D 85 F6 0F 88') in body,
              'test r14, r14; js .einval')
    rep.check('read.write_target_flag',
              bytes.fromhex('41 B8 01 00 00 00') in body, 'mov r8d, 1')
    calls = [t for _, t in call_sites(img, at, end)]
    rep.equal('read.call_chain', calls,
              [img.at('check_range'), img.at('fs_read'),
               img.at('copy_to_user'), img.at('map_error')])
    rep.equal('read.payload_through_the_bounce_page',
              body.count(abs32(bytes.fromhex('48 C7 C2'), img['bounce'])), 2,
              'fs_read and copy_to_user both name fsys_bounce')
    rep.check('read.chunk_cap',
              bytes.fromhex('48 81 FB 00 10 00 00 76 05 BB 00 10 00 00') in body,
              'chunk = min(left, 4096)')
    rep.check('read.count_from_rdx', bytes.fromhex('48 89 D3') in body,
              'mov rbx, rdx')
    rep.check('read.short_read_rule',
              bytes.fromhex('4C 89 F0 48 01 D8 48 3D 00 10 00 00 76 05 '
                            'B8 00 10 00 00 48 39 C3 72') in body,
              'n < min(before, 4096) ends the read')
    rep.check('read.total_from_r12', bytes.fromhex('4C 89 E0') in body,
              'mov rax, r12, full width (S1)')
    rep.check('read.partial_read_guards', partial_read_guards(body),
              'test r12, r12; jnz .done on both error paths (S2)')
    rep.check('read.counts_only_delivered',
              delivered_after_copy(body, at, end),
              'add r12, rbx sits after the copy_to_user call (S2)')
    rep.check('read.copy_fault_is_efault',
              (b'\xB8' + struct.pack('<i', EFAULT)) in body,
              'a first-chunk copy fault reports EFAULT (S2)')

    # ---------------- CLOSE -----------------------------------------
    at, end = region(img, 'h_close', 'h_seek')
    body = d[at:end]
    rep.check('close.fd_from_frame', bytes.fromhex('48 8B 49 48') in body,
              'mov rcx, [rcx+72]')
    rep.check('close.fd_bound', bytes.fromhex('48 83 F9 08 73') in body,
              'cmp rcx, 8; jae .einval')
    calls = [t for _, t in call_sites(img, at, end)]
    rep.equal('close.call_chain', calls,
              [img.at('fs_close'), img.at('map_error')])
    rep.check('close.success_zero', bytes.fromhex('31 C0') in body)

    # ---------------- SEEK ------------------------------------------
    at, end = region(img, 'h_seek', 'h_stat')
    body = d[at:end]
    rep.check('seek.fd_from_frame', bytes.fromhex('49 8B 4C 24 48') in body)
    rep.check('seek.fd_bound', bytes.fromhex('48 83 F9 08 73') in body)
    rep.check('seek.signed_offset_full_width',
              bytes.fromhex('49 8B 54 24 50') in body, 'mov rdx, [r12+80]')
    rep.check('seek.whence_from_frame',
              bytes.fromhex('45 8B 44 24 68 41 83 F8 02 77') in body,
              'mov r8d, [r12+104]; cmp r8d, 2; ja .einval')
    calls = [t for _, t in call_sites(img, at, end)]
    rep.equal('seek.call_chain', calls,
              [img.at('fs_seek'), img.at('map_error')])
    rep.check('seek.offset_from_rdx', bytes.fromhex('48 89 D0') in body,
              'mov rax, rdx, full 64 bit offset (W1)')

    # ---------------- STAT ------------------------------------------
    at, end = region(img, 'h_stat', 'h_readdir')
    body = d[at:end]
    rep.check('stat.buffer_from_frame', bytes.fromhex('4D 8B 6C 24 50') in body,
              'mov r13, [r12+80]')
    rep.check('stat.validates_64_bytes_for_write',
              bytes.fromhex('BA 40 00 00 00 41 B8 01 00 00 00') in body,
              'mov edx, 64; mov r8d, 1')
    calls = [t for _, t in call_sites(img, at, end)]
    rep.equal('stat.call_chain', calls,
              [img.at('check_range'), img.at('copy_path'), img.at('fs_stat'),
               img.at('copy_to_user'), img.at('map_error')])
    rep.check('stat.record_through_the_bounce_area',
              (abs32(bytes.fromhex('48 C7 C1'), img['path_buf']) +
               abs32(bytes.fromhex('48 C7 C2'), img['stat_buf'])) in body)
    rep.check('stat.copies_out_64_bytes',
              (abs32(bytes.fromhex('48 C7 C2'), img['stat_buf']) +
               bytes.fromhex('41 B8 40 00 00 00')) in body)

    # ---------------- READDIR ---------------------------------------
    at, end = region(img, 'h_readdir', 'h_exit')
    body = d[at:end]
    rep.check('readdir.frame_arguments',
              bytes.fromhex('4C 8B 79 48 4C 8B 69 50 4C 8B 71 68') in body)
    rep.check('readdir.fd_bound', bytes.fromhex('49 83 FF 08 0F 83') in body)
    rep.check('readdir.minimum_one_entry',
              bytes.fromhex('49 83 FE 20 0F 82') in body,
              'cmp r14, 32; jb .einval')
    calls = [t for _, t in call_sites(img, at, end)]
    rep.equal('readdir.call_chain', calls,
              [img.at('check_range'), img.at('fs_readdir'),
               img.at('copy_to_user'), img.at('map_error')])
    rep.equal('readdir.payload_through_the_bounce_page',
              body.count(abs32(bytes.fromhex('48 C7 C2'), img['bounce'])), 2)
    rep.check('readdir.whole_entries_only',
              bytes.fromhex('48 83 E3 E0 74') in body,
              'and rbx, -32; jz .done')
    rep.check('readdir.short_emit_rule',
              bytes.fromhex('48 83 E0 E0 48 39 C3 72') in body,
              'want &= -32; n < want ends the directory')
    rep.check('readdir.total_from_r12', bytes.fromhex('4C 89 E0') in body,
              'mov rax, r12, full width (S1)')
    rep.check('readdir.partial_read_guards', partial_read_guards(body),
              'test r12, r12; jnz .done on both error paths (S2)')
    rep.check('readdir.counts_only_delivered',
              delivered_after_copy(body, at, end),
              'add r12, rbx sits after the copy_to_user call (S2)')
    rep.check('readdir.copy_fault_is_efault',
              (b'\xB8' + struct.pack('<i', EFAULT)) in body,
              'a first-chunk copy fault reports EFAULT (S2)')


def check_exit(rep, img):
    at, end = region(img, 'h_exit', 'init')
    d = img.data
    body = d[at:end]
    rep.check('exit.code_from_frame', body.startswith(bytes.fromhex('48 8B 41 48')),
              'mov rax, [rcx+72]')
    rep.equal('exit.parks_the_code',
              len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0, 'exit_code')), 1)
    rep.equal('exit.restores_the_kernel_stack',
              len(rip_refs(img, at, end, b'\x48\x8B', 0x25, 0, 'probe_rsp')), 1)
    rep.equal('exit.jumps_to_the_parked_rip',
              len(rip_refs(img, at, end, b'\xFF', 0x25, 0, 'probe_rip')), 1)
    # the whole body is four instructions: load, park, restore, jump;
    # there is no room for a ret, sysretq or iretq opcode
    rep.equal('exit.body_size', len(body), 24)
    rep.check('exit.never_returns',
              body.startswith(bytes.fromhex('48 8B 41 48 48 89 05')) and
              body[11:14] == bytes.fromhex('48 8B 25') and
              body[18:20] == bytes.fromhex('FF 25'),
              'load / park / mov rsp / jmp [rip], nothing else')


def check_init(rep, img):
    at, end = region(img, 'init', 'probe')
    d = img.data
    body = d[at:end]
    rep.check('init.checks_cpl', bytes.fromhex('66 8C C8 A8 03') in body)
    rep.check('init.checks_if', bytes.fromhex('9C 58 A9 00 02 00 00') in body)
    rep.equal('init.ready_guard',
              len([1 for _, t, imm in
                   rip_sites(img, at, end, b'\x48\x83', 0x3D, 1)
                   if t == img.at('ready') and imm == b'\x00']), 1)
    calls = [t for _, t in call_sites(img, at, end)]
    rep.equal('init.six_registrations', calls,
              [img.at('cpu_syscall_set_handler')] * 6)
    numbers = ordered_positions(img, at, end,
                                [b'\xB9' + struct.pack('<I', n)
                                 for n in (4, 5, 6, 7, 8, 9)])
    rep.check('init.numbers_4_to_9_in_order', numbers is not None)
    targets = [t for _, t, _ in rip_sites(img, at, end, b'\x48\x8D', 0x15, 0)]
    rep.equal('init.handler_addresses', targets,
              [img.at(n) for n in ('h_open', 'h_read', 'h_close',
                                   'h_seek', 'h_stat', 'h_readdir')])
    armed = [(t, imm) for _, t, imm in
             rip_sites(img, at, end, b'\x48\xC7', 0x05, 4)
             if t == img.at('ready')]
    rep.check('init.marks_ready_once',
              armed == [(img.at('ready'), struct.pack('<I', 1))])
    rep.check('init.eperm', (b'\xB8' + struct.pack('<i', EPERM)) in body)
    rep.check('init.ealready', (b'\xB8' + struct.pack('<i', EALREADY)) in body)


def check_probe(rep, img):
    at, end = region(img, 'probe', 'errmap')
    d = img.data
    body = d[at:end]
    state = img.at('mm_vmm_state') + STATE_TABLE_ALLOCS

    rep.equal('probe.ready_guard',
              len([1 for _, t, imm in
                   rip_sites(img, at, end, b'\x48\x83', 0x3D, 1)
                   if t == img.at('ready') and imm == b'\x01']), 1)
    # snapshot the frozen counters and the table baseline
    for src, dst in (('fs_test_reads', 'saved_fs_reads'),
                     ('blk_test_reads', 'saved_blk_reads')):
        rep.equal('probe.snapshots_%s' % src,
                  len(rip_refs(img, at, end, b'\x48\x8B', 0x05, 0, src)), 1)
        rep.equal('probe.parks_%s' % dst,
                  len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0, dst)), 1)
    rep.equal('probe.table_baseline_loads',
              len([1 for _, t, _ in rip_sites(img, at, end, b'\x48\x8B', 0x05, 0)
                   if t == state]), 3,
              'snapshot, delta and the W3 net invariant')
    rep.equal('probe.parks_table_base',
              len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0, 'table_base')), 1)
    drops = img.at('mm_vmm_state') + STATE_TABLE_DROPS
    rep.equal('probe.drops_baseline_loads',
              len([1 for _, t, _ in rip_sites(img, at, end, b'\x48\x8B', 0x05, 0)
                   if t == drops]), 2,
              'snapshot and the released delta (W3)')
    rep.equal('probe.parks_drops_base',
              len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0, 'drops_base')), 1)
    # nine zeroed frames from the public allocator
    calls = [t for _, t in call_sites(img, at, end)]
    rep.equal('probe.alloc_loop', calls.count(img.at('pmm_alloc_page')), 1)
    rep.check('probe.stores_the_frames',
              abs32(bytes.fromhex('4A 89 14 E5'), img['pages']) in body,
              'mov [fsys_pages+r12*8], rdx')
    rep.check('probe.nine_pages', bytes.fromhex('41 83 FC 09 72') in body,
              'cmp r12d, 9; jb .alloc')
    # stage the embedded image through the identity map
    rep.check('probe.stages_the_image',
              abs32(bytes.fromhex('49 C7 C5'), img['probe_image']) in body)
    size = img['probe_image_end'] - img['probe_image']
    rep.check('probe.image_size_immediate',
              (b'\x41\xBE' + struct.pack('<I', size)) in body,
              'mov r14d, %d' % size)
    # the three map loops with the frozen flags
    rep.equal('probe.three_map_sites', calls.count(img.at('vmm_map_user_page')), 3)
    rep.check('probe.code_base_immediate',
              imm64(b'\x48\xB9', CODE_BASE) in body)
    rep.check('probe.code_pages_pure_us',
              bytes.fromhex('49 C7 C0 04 00 00 00') in body, 'mov r8, MM_MAP_US')
    rep.check('probe.data_base_immediate',
              imm64(b'\x48\xB9', DATA_BASE) in body)
    rep.check('probe.data_and_stack_us_rw_nx',
              body.count(imm64(b'\x49\xB8', MAP_US | MAP_RW | MAP_NX)) == 2,
              'mov r8, US|RW|NX twice')
    rep.check('probe.stack_base_immediate',
              imm64(b'\x48\xB9', STACK_TOP - STACK_SIZE) in body)
    rep.check('probe.four_page_loops',
              body.count(bytes.fromhex('41 83 FC 04 72')) == 3,
              'cmp r12d, 4; jb: code, stack and the teardown VA dispatch')
    rep.equal('probe.data_frame_load',
              len(rip_refs(img, at, end, b'\x48\x8B', 0x15, 0, 'pages',
                           CODE_PAGES * 8)), 1)
    rep.check('probe.stack_frame_index',
              abs32(bytes.fromhex('4A 8B 14 E5'),
                    img['pages'] + (CODE_PAGES + DATA_PAGES) * 8) in body)
    # the frozen +3 budget
    rep.equal('probe.budget_subtract',
              len(rip_refs(img, at, end, b'\x48\x2B', 0x05, 0, 'table_base')), 1)
    rep.equal('probe.publishes_the_delta',
              len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0, 'table_delta')), 1)
    rep.check('probe.budget_compare_is_three',
              bytes.fromhex('48 83 F8 03 0F 85') in body,
              'cmp rax, 3; jne .fail_eio')
    # the private syscall stack
    rep.equal('probe.reads_the_live_rsp0',
              len(rip_refs(img, at, end, b'\x48\x8B', 0x05, 0, 'cpu_tss_rsp0')), 1)
    rep.equal('probe.parks_the_old_rsp0',
              len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0, 'saved_rsp0')), 1)
    rep.check('probe.switches_to_the_private_stack',
              abs32(bytes.fromhex('48 C7 C1'), img['syscall_stack_top']) in body)
    rep.equal('probe.three_rsp0_calls', calls.count(img.at('cpu_set_rsp0')), 3,
              'switch before the probe, restore after, S3 failure restore')
    rep.equal('probe.restores_the_old_rsp0',
              len(rip_refs(img, at, end, b'\x48\x8B', 0x0D, 0, 'saved_rsp0')), 2)
    # the probe-private EXIT registration and its clear
    rep.equal('probe.two_syscall_registrations',
              calls.count(img.at('cpu_syscall_set_handler')), 2)
    zeros = ordered_positions(img, at, end,
                              [b'\xB9\x00\x00\x00\x00', b'\xB9\x00\x00\x00\x00'])
    rep.check('probe.registers_and_clears_number_zero', zeros is not None)
    rep.equal('probe.exit_handler_address',
              len(rip_refs(img, at, end, b'\x48\x8D', 0x15, 0, 'h_exit')), 1)
    # park the kernel context and enter ring 3
    rep.equal('probe.parks_rsp',
              len(rip_refs(img, at, end, b'\x48\x89', 0x25, 0, 'probe_rsp')), 1)
    resume = [t for _, t, _ in rip_sites(img, at, end, b'\x48\x8D', 0x05, 0)
              if at < t < end]
    rep.check('probe.parks_a_resume_inside_itself', len(resume) == 1,
              'lea rax, [.resume]')
    rep.equal('probe.parks_rip',
              len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0, 'probe_rip')), 1)
    frame = ordered_positions(img, at, end, [
        b'\x6A\x2B',                                    # push SS
        imm64(b'\x48\xB8', STACK_TOP) + b'\x50',        # push RSP
        b'\x6A\x02',                                    # push RFLAGS, IF=0
        b'\x6A\x33',                                    # push CS
        imm64(b'\x48\xB8', CODE_BASE) + b'\x50',        # push RIP
        bytes.fromhex('48 CF'),                         # iretq
    ])
    rep.check('probe.iretq_frame_byte_by_byte', frame is not None,
              'SS/RSP/RFLAGS/CS/RIP then iretq')
    # the evidence read-back
    rep.equal('probe.data_frame_for_evidence',
              len(rip_refs(img, at, end, b'\x4C\x8B', 0x2D, 0, 'pages',
                           CODE_PAGES * 8)), 1)
    rep.check('probe.reads_mask_and_fail',
              bytes.fromhex('49 8B 45 00') in body and
              bytes.fromhex('49 8B 45 08') in body)
    rep.equal('probe.parks_the_mask',
              len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0, 'probe_mask')), 1)
    rep.equal('probe.parks_the_fail',
              len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0, 'probe_fail')), 1)
    rep.equal('probe.evidence_destination',
              len(rip_refs(img, at, end, b'\x4C\x8D', 0x25, 0, 'evidence')), 1)
    rep.check('probe.eight_evidence_qwords',
              bytes.fromhex('41 BE 08 00 00 00 49 83 C5 10') in body,
              'mov r14d, 8; add r13, 16')
    rep.check('probe.evidence_loop',
              bytes.fromhex('49 8B 45 00 49 89 04 24 49 83 C5 08 49 83 C4 08 41 FF CE 75')
              in body)
    # restore the frozen counters before the verdict
    for src, dst in (('saved_fs_reads', 'fs_test_reads'),
                     ('saved_blk_reads', 'blk_test_reads')):
        rep.equal('probe.restores_from_%s' % src,
                  len(rip_refs(img, at, end, b'\x48\x8B', 0x05, 0, src)), 1)
        rep.equal('probe.restores_%s' % dst,
                  len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0, dst)), 1)
    # the verdict: exit code zero and all eleven legs
    verdict = rip_sites(img, at, end, b'\x48\x83', 0x3D, 1)
    rep.check('probe.exit_code_must_be_zero',
              any(t == img.at('exit_code') and imm == b'\x00'
                  for _, t, imm in verdict))
    mask = rip_sites(img, at, end, b'\x48\x81', 0x3D, 4)
    rep.check('probe.mask_must_be_complete',
              any(t == img.at('probe_mask') and
                  imm == struct.pack('<I', FP_MASK_ALL) for _, t, imm in mask))
    for name, code in (('enotready', ENOTREADY), ('eio', EIO)):
        rep.check('probe.reports_%s' % name,
                  (b'\xB8' + struct.pack('<i', code)) in body)
    rep.check('probe.reports_enomem',
              (b'\x41\xBF' + struct.pack('<i', ENOMEM)) in body,
              'mov r15d, ENOMEM parks the verdict for the teardown')

    # ---- task 138 W3: the teardown on every exit path ----
    rep.equal('probe.unmap_call',
              calls.count(img.at('vmm_unmap_user_page')), 1)
    rep.equal('probe.free_call', calls.count(img.at('pmm_free_page')), 1)
    rep.check('probe.nine_unmap_loop',
              bytes.fromhex('41 83 FC 09 73') in body,
              'cmp r12d, 9; jae .unmapped')
    dispatch = [p for p in find_all(body, bytes.fromhex('41 83 FC 04'))
                if body[p + 4] == 0x72 and body[p + 6] == 0x74]
    rep.check('probe.unmap_va_dispatch', len(dispatch) == 1,
              'cmp r12d, 4; jb .code_va; je .data_va, no offset on data')
    enoent = find_all(body, bytes.fromhex('83 F8 FD'))
    rep.check('probe.enoent_tolerated',
              len(enoent) == 1 and
              (body[enoent[0] + 3] == 0x75 or
               body[enoent[0] + 3:enoent[0] + 5] == b'\x0F\x85'),
              'cmp eax, ENOENT; jne .teardown_eio, partial paths share the loop')
    # the net table invariant: allocs - drops back at the baseline
    rep.equal('probe.net_invariant_subtracts_drops',
              len([1 for _, t, _ in rip_sites(img, at, end, b'\x48\x2B', 0x05, 0)
                   if t == drops]), 1)
    rep.equal('probe.net_invariant_loads_table_base',
              len(rip_refs(img, at, end, b'\x48\x8B', 0x15, 0, 'table_base')), 1)
    rep.equal('probe.net_invariant_subtracts_drops_base',
              len(rip_refs(img, at, end, b'\x48\x2B', 0x15, 0, 'drops_base')), 1)
    rep.check('probe.net_invariant_compare',
              bytes.fromhex('48 39 D0') in body, 'cmp rax, rdx; jne .teardown_eio')
    rep.equal('probe.released_delta',
              len(rip_refs(img, at, end, b'\x48\x2B', 0x05, 0, 'drops_base')), 1)
    rep.equal('probe.parks_table_released',
              len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0,
                           'table_released')), 1)
    # free only the frames actually held, then zero the array
    rep.equal('probe.free_guard',
              len([1 for _, t, _ in rip_sites(img, at, end, b'\x4C\x3B', 0x25, 0)
                   if t == img.at('pages_held')]), 1,
              'cmp r12, [fsys_pages_held]; jae .freed')
    rep.check('probe.zeroes_the_page_array',
              (abs32(bytes.fromhex('4A C7 04 E5'), img['pages']) +
               bytes(4)) in body,
              'mov qword [fsys_pages+r12*8], 0')
    rep.equal('probe.parks_pages_held',
              len(rip_refs(img, at, end, b'\x4C\x89', 0x25, 0, 'pages_held')), 2,
              'after the alloc loop and on the ENOMEM path')
    held_zero = [(t, imm) for _, t, imm in
                 rip_sites(img, at, end, b'\x48\xC7', 0x05, 4)
                 if t == img.at('pages_held')]
    rep.check('probe.clears_pages_held',
              held_zero == [(img.at('pages_held'), bytes(4))])
    # the guest evidence page is snapshotted into the bounce page
    # while the mapping is still live, before the teardown
    rep.equal('probe.snapshot_loads_the_guest_frame',
              len(rip_refs(img, at, end, b'\x48\x8B', 0x35, 0, 'pages',
                           CODE_PAGES * 8)), 1)
    rep.check('probe.snapshot_into_the_bounce',
              (abs32(bytes.fromhex('48 C7 C7'), img['bounce']) +
               bytes.fromhex('B9 00 02 00 00 F3 48 A5')) in body,
              'mov rdi, fsys_bounce; mov ecx, 512; rep movsq')
    # the verdict travels through r15d across the teardown
    rep.check('probe.success_verdict_zero',
              bytes.fromhex('45 31 FF') in body, 'xor r15d, r15d')
    rep.equal('probe.parks_failure_verdicts',
              body.count(bytes.fromhex('41 89 C7')), 2,
              'mov r15d, eax on the S3 and the common failure path')
    rep.equal('probe.returns_the_parked_verdict',
              body.count(bytes.fromhex('44 89 F8')), 2,
              'mov eax, r15d after the teardown and the S3 restore')
    # S3: the failure restore sits behind the EXIT registration
    rsp0_sites = [s for s, t in call_sites(img, at, end)
                  if t == img.at('cpu_set_rsp0')]
    reg_sites = [s for s, t in call_sites(img, at, end)
                 if t == img.at('cpu_syscall_set_handler')]
    rep.check('probe.s3_restore_after_exit_registration',
              len(rsp0_sites) == 3 and rsp0_sites[2] > reg_sites[-1],
              'the third cpu_set_rsp0 restores TSS.RSP0 on the S3 path')
    # the branch after the EXIT registration must land on the restore
    # block itself (mov r15d, eax; mov rcx, [saved_rsp0]; call), not on
    # the bare .fail that would leak the private RSP0
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
                     body[t:t + 3] == bytes.fromhex('41 89 C7') and
                     len(rsp0_sites) == 3 and rsp0_sites[2] == target + 10)
    rep.check('probe.s3_branch_targets_the_restore', s3_ok,
              'jnz after the EXIT registration lands on .fail_rsp0')


def check_module_hygiene(rep, img):
    # the function bodies only: the errmap and the embedded user image
    # are data bytes, not instructions
    at, end = img.at('map_error'), img.at('errmap')
    d = img.data
    code = d[at:end]
    seams = {img.at(n) for n in
             ('fs_open', 'fs_read', 'fs_seek', 'fs_close', 'fs_stat',
              'fs_readdir', 'mm_vmm_resolve', 'vmm_map_user_page',
              'pmm_alloc_page', 'vmm_unmap_user_page', 'pmm_free_page',
              'cpu_syscall_set_handler', 'cpu_set_rsp0')}
    stray = [(s, t) for s, t in call_sites(img, at, end)
             if not (at <= t < end) and t not in seams]
    rep.check('hygiene.call_targets', not stray, repr(stray[:4]))
    # every RIP relative write lands in the fsys data region, except
    # the two frozen counter restores
    allowed = {img.at('fs_test_reads'), img.at('blk_test_reads')}
    lo, hi = img.at('data_start'), img.at('data_end')
    bad = []
    for prefix, modrm, tail in ((b'\x48\x89', 0x05, 0), (b'\x48\x89', 0x25, 0),
                                (b'\x4C\x89', 0x25, 0), (b'\x48\xC7', 0x05, 4)):
        for s, t, _ in rip_sites(img, at, end, prefix, modrm, tail):
            if not (lo <= t < hi) and t not in allowed:
                bad.append((s, t))
    rep.check('hygiene.rip_writes_stay_in_data', not bad, repr(bad[:4]))
    rep.check('hygiene.no_sysret', bytes.fromhex('48 0F 07') not in code)
    rep.check('hygiene.no_swapgs', bytes.fromhex('0F 01 F8') not in code)
    rep.equal('hygiene.one_iretq', code.count(bytes.fromhex('48 CF')), 1,
              'the single ring 3 entry of the probe')
    # single byte opcodes are scanned outside the displacement and
    # immediate windows: a 0xFB inside a disp32 is data, not an STI
    skip = set()
    forms = [(b'\x48\x8B', 0x05, 0), (b'\x48\x8B', 0x0D, 0),
             (b'\x48\x8B', 0x15, 0), (b'\x48\x8B', 0x25, 0),
             (b'\x48\x8B', 0x35, 0), (b'\x4C\x8B', 0x2D, 0),
             (b'\x48\x89', 0x05, 0), (b'\x48\x89', 0x25, 0),
             (b'\x4C\x89', 0x25, 0), (b'\x4C\x3B', 0x25, 0),
             (b'\x48\x8D', 0x05, 0),
             (b'\x48\x8D', 0x0D, 0), (b'\x48\x8D', 0x15, 0),
             (b'\x4C\x8D', 0x25, 0), (b'\x48\x2B', 0x05, 0),
             (b'\x48\x2B', 0x15, 0),
             (b'\x48\x03', 0x05, 0), (b'\x48\x03', 0x3D, 0),
             (b'\xFF', 0x25, 0), (b'\x48\x83', 0x3D, 1),
             (b'\x48\x81', 0x3D, 4), (b'\x48\xC7', 0x05, 4)]
    for prefix, modrm, tail in forms:
        for p in find_all(code, prefix):
            m = p + len(prefix)
            if m + 5 + tail <= len(code) and code[m] == modrm:
                skip.update(range(m + 1, m + 5 + tail))
    for prefix in (b'\x48\xC7\xC1', b'\x48\xC7\xC2', b'\x48\xC7\xC3',
                   b'\x48\xC7\xC7', b'\x49\xC7\xC5'):
        for p in find_all(code, prefix):
            skip.update(range(p + 3, p + 7))
    # register ModRM bytes are data too: sub rbx, rdi (48 29 FB) and
    # cmp rbx, imm32 (48 81 FB) both carry a 0xFB ModRM
    for p in find_all(code, b'\x48\x29\xFB'):
        skip.add(p + 2)
    for p in find_all(code, b'\x48\x81\xFB'):
        skip.update(range(p + 2, p + 7))
    for prefix in (b'\x4A\x89\x14\xE5', b'\x4A\x8B\x3C\xE5', b'\x4A\x8B\x14\xE5',
                   b'\x4A\x8B\x0C\xE5'):
        for p in find_all(code, prefix):
            skip.update(range(p + 4, p + 8))
    # mov qword [fsys_pages+r12*8], 0: disp32 plus a zero imm32
    for p in find_all(code, b'\x4A\xC7\x04\xE5'):
        skip.update(range(p + 4, p + 12))
    for prefix in (b'\x48\xB8', b'\x48\xB9', b'\x49\xB8'):
        for p in find_all(code, prefix):
            skip.update(range(p + 2, p + 10))
    for p in find_all(code, b'\xE8'):
        skip.update(range(p + 1, p + 5))
    for p in find_all(code, b'\x0F'):
        if p + 6 <= len(code) and 0x80 <= code[p + 1] <= 0x8F:
            skip.update(range(p + 2, p + 6))
    # mov r32, imm32 (B8..BF): e.g. mov eax, -5 carries a 0xFB in the
    # immediate, not an STI
    for i, b in enumerate(code):
        if 0xB8 <= b <= 0xBF and i + 5 <= len(code):
            skip.update(range(i + 1, i + 5))
    for i, b in enumerate(code):
        if 0x70 <= b <= 0x7F or b == 0xEB:
            skip.add(i + 1)
    rep.check('hygiene.no_sti',
              all(code[i] != 0xFB for i in range(len(code)) if i not in skip))
    rep.check('hygiene.no_hlt',
              all(code[i] != 0xF4 for i in range(len(code)) if i not in skip))


# ----------------------------------------------------------------------
# pure model replay of the frozen semantics
# ----------------------------------------------------------------------


def model_map_error(status):
    """neg eax; cmp eax, 12; ja .eio; movsx eax, [errmap+idx]"""
    idx = (-status) & 0xFFFFFFFF
    if idx > 12:
        return EIO
    return FS_TO_K64[idx]


def model_check_range(va, length, write, pages):
    """The full-range validator: domain, overflow, 4 KiB user leaves.
    pages maps page VA -> (leaf_size, us, rw); absent means unmapped."""
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


def model_read(req, file_bytes):
    """The READ bounce loop: chunk = min(left, 4096), fs_read clamps to
    the file, a short read against min(before, 4096) ends the loop."""
    total, left, chunks = 0, req, []
    while left > 0:
        chunk = min(left, BOUNCE_SIZE)
        chunks.append(chunk)
        n = min(chunk, file_bytes)
        file_bytes -= n
        total += n
        before = left
        left -= n
        if n < min(before, BOUNCE_SIZE):
            break
    return total, chunks


def model_read_fault(req, file_bytes, fail_at, fault=EIO):
    """The READ loop with an injected EIO/EFAULT on chunk fail_at
    (task 138 S2, POSIX): delivered bytes win once N > 0, only a
    failure before the first delivered byte returns -errno."""
    total, left, delivered, idx = 0, req, 0, 0
    while left > 0:
        chunk = min(left, BOUNCE_SIZE)
        if idx == fail_at:
            return (total if total > 0 else fault), delivered
        n = min(chunk, file_bytes)
        file_bytes -= n
        total += n
        delivered += 1
        before = left
        left -= n
        idx += 1
        if n < min(before, BOUNCE_SIZE):
            break
    return total, delivered


def model_seek(new_offset):
    """The SEEK adapter returns rdx verbatim (task 138 W1): no 32 bit
    truncation, no sign extension of a >= 2 GiB offset."""
    return new_offset


def model_readdir(req, dir_bytes):
    """The READDIR bounce loop with whole 32 byte entries only."""
    if req < DIRENT_SIZE:
        return EINVAL, []
    total, left, chunks = 0, req, []
    while left > 0:
        chunk = min(left, BOUNCE_SIZE) & -DIRENT_SIZE
        if chunk == 0:
            break
        chunks.append(chunk)
        n = min(chunk, dir_bytes)
        dir_bytes -= n
        total += n
        before = left
        left -= n
        if n < (min(before, BOUNCE_SIZE) & -DIRENT_SIZE):
            break
    return total, chunks


def check_model(rep, img):
    # the error map, every frozen status plus the out-of-table class
    names = ['OK', 'EINVAL', 'ENOMEM', 'ENOENT', 'EEXIST', 'EPERM',
             'ENOTSUP', 'ENOTREADY', 'EALREADY', 'EIO', 'EOVERFLOW',
             'EFAULT', 'EBUSY']
    for i, name in enumerate(names):
        rep.equal('model.map_%s' % name, model_map_error(-i), FS_TO_K64[i])
    for status in (-13, -99, 1, 7):
        rep.equal('model.map_out_of_table_%d' % status,
                  model_map_error(status), EIO)
    d = img.data
    table = d[img.at('errmap'):img.at('errmap_end')]
    rep.check('model.errmap_bytes_match_the_model',
              table == bytes(model_map_error(-i) & 0xFF for i in range(13)))

    # the range validator matrix over a fake user domain
    full = {VA_BASE + i * PAGE: (PAGE, 1, 1)
            for i in range((VA_LIMIT - VA_BASE) // PAGE)}
    ro = dict(full)
    ro[VA_BASE] = (PAGE, 1, 0)
    supervisor = dict(full)
    supervisor[VA_BASE + PAGE] = (PAGE, 0, 1)
    big = dict(full)
    big[VA_BASE] = (0x200000, 1, 1)
    hole = dict(full)
    del hole[VA_BASE + 2 * PAGE]
    cases = [
        ('zero_length_anywhere', 0, 0, 1, full, OK),
        ('zero_length_outside', VA_BASE - 1, 0, 1, full, OK),
        ('whole_domain', VA_BASE, VA_LIMIT - VA_BASE, 0, full, OK),
        ('last_byte', VA_LIMIT - 1, 1, 1, full, OK),
        ('below_base', VA_BASE - 1, 1, 0, full, EFAULT),
        ('one_past_limit', VA_LIMIT - 1, 2, 0, full, EFAULT),
        ('kernel_pointer', KERNEL_BASE, 64, 1, full, EFAULT),
        ('negative_length', VA_BASE, (1 << 64) - 1, 0, full, EFAULT),
        ('overflow', VA_LIMIT - 0x10, 0x20, 0, full, EFAULT),
        ('unmapped_hole', VA_BASE, 4 * PAGE, 0, hole, EFAULT),
        ('two_mib_leaf', VA_BASE, PAGE, 0, big, EFAULT),
        ('supervisor_page', VA_BASE, 2 * PAGE, 0, supervisor, EFAULT),
        ('write_to_ro', VA_BASE, PAGE, 1, ro, EFAULT),
        ('read_from_ro', VA_BASE, PAGE, 0, ro, OK),
        ('cross_page_write', VA_BASE + PAGE - 8, 16, 1, full, OK),
    ]
    for name, va, length, write, pages, want in cases:
        rep.equal('model.range_%s' % name,
                  model_check_range(va, length, write, pages), want)

    # the READ chunking against the frozen file sizes
    rep.equal('model.read_ibm_512', model_read(512, 132), (132, [512]))
    rep.equal('model.read_zero', model_read(0, 132), (0, []))
    rep.equal('model.read_eof_again', model_read(512, 0), (0, [512]))
    rep.equal('model.read_exact', model_read(132, 132), (132, [132]))
    rep.equal('model.read_big_two_chunks', model_read(8192, 8229),
              (8192, [4096, 4096]))
    rep.equal('model.read_big_short_tail', model_read(16384, 8229),
              (8229, [4096, 4096, 4096]))
    rep.equal('model.read_tetris', model_read(4096, 494), (494, [4096]))

    # SEEK returns the full 64 bit offset (W1): the frozen truncation
    # would have turned 0x80000000 into a negative errno lookalike
    for off in (0, 132, 0x7FFFFFFF, 0x80000000, 0x100000000, 0x100000084):
        rep.equal('model.seek_full_width_%#x' % off, model_seek(off), off)
    rep.check('model.seek_no_sign_extension',
              model_seek(0x80000000) != 0xFFFFFFFF80000000)
    rep.check('model.seek_no_truncation',
              model_seek(0x100000000) != 0)

    # POSIX partial reads (S2): a mid-transfer EIO/EFAULT keeps the
    # delivered count, a first-chunk failure returns the errno
    rep.equal('model.read_fault_first_chunk', model_read_fault(8192, 8229, 0),
              (EIO, 0))
    rep.equal('model.read_fault_second_chunk', model_read_fault(8192, 8229, 1),
              (4096, 1))
    rep.equal('model.read_fault_third_chunk', model_read_fault(16384, 8229, 2),
              (8192, 2))
    rep.equal('model.read_fault_small_file', model_read_fault(512, 132, 0),
              (EIO, 0))
    rep.equal('model.read_copy_fault_partial',
              model_read_fault(8192, 8229, 1, EFAULT), (4096, 1))
    rep.equal('model.read_copy_fault_first',
              model_read_fault(8192, 8229, 0, EFAULT), (EFAULT, 0))
    rep.equal('model.read_fault_never_reached',
              model_read_fault(8192, 8229, 5), (8192, 2))

    # the READDIR chunking with the frozen two entry directory
    rep.equal('model.readdir_two_entries', model_readdir(64, 64), (64, [64]))
    rep.equal('model.readdir_ragged_request', model_readdir(33, 64), (32, [32]))
    rep.equal('model.readdir_too_small', model_readdir(31, 64), (EINVAL, []))
    rep.equal('model.readdir_exhausted', model_readdir(64, 0), (0, [64]))
    rep.equal('model.readdir_one_entry', model_readdir(32, 64), (32, [32]))

    # the adapter decision table pinned by the byte checks above
    rep.equal('model.open_write_flags', ENOTSUP, ENOTSUP)
    for fd in (8, 9, -1, 1 << 40):
        rep.check('model.fd_%d_refused' % fd, (fd & ((1 << 64) - 1)) >= MAX_FDS)
    for whence in (3, 4, -1):
        rep.check('model.whence_%d_refused' % whence,
                  (whence & 0xFFFFFFFF) > SEEK_END)


# ----------------------------------------------------------------------
# source level discipline
# ----------------------------------------------------------------------


def check_sources(rep, root):
    asm_text = (root / 'kernel' / 'fsys' / 'fsys.asm').read_text(encoding='utf-8')
    rep.check('source.fsys_asm_hosted_guard',
              'if ~ defined FSYS_HOSTED' in asm_text and
              'format binary' in asm_text)
    order = re.findall(r"^include\s+'([^']+)'", asm_text, re.M)
    rep.equal('source.include_order', order, ['api.inc', 'fsys.inc', 'data.inc'])
    rep.check('source.fsys_asm_end_assertion',
              'if $ <> fsys_data_end' in asm_text)

    api = (root / 'kernel' / 'fsys' / 'api.inc').read_text(encoding='utf-8')
    rep.check('source.api_private_assertions',
              len(re.findall(r'^\s*err\b', api, re.M)) >= 10,
              '%d err sites' % len(re.findall(r'^\s*err\b', api, re.M)))

    # the kernel include order: fsys sits between fs and app, the
    # task 115 hosted loader that consumes the frozen file syscalls;
    # contracts closes the module chain right after app
    k64 = (root / 'kernel' / 'kernel64.asm').read_text(encoding='utf-8')
    incs = re.findall(r"^include\s+'([^']+)'", k64, re.M)
    ok = ('fsys/fsys.asm' in incs and
          incs.index('fsys/fsys.asm') == incs.index('fs/fs.asm') + 1 and
          incs.index('app/app.asm') == incs.index('fsys/fsys.asm') + 1 and
          incs.index('core/contracts.inc') == incs.index('app/app.asm') + 1)
    rep.check('source.kernel64_include_position', ok, repr(incs[-6:]))
    rep.check('source.kernel64_fsys_hosted',
              re.search(r"^FSYS_HOSTED = 1\ninclude 'fsys/fsys\.asm'", k64, re.M)
              is not None)

    # SYS_CALL_MAX is 16 in both frozen copies
    cpu_api = (root / 'kernel' / 'cpu' / 'api.inc').read_text(encoding='utf-8')
    rep.check('source.cpu_api_max_16', 'CPU_SYS_CALL_MAX    = 16' in cpu_api)
    contracts = (root / 'kernel' / 'core' / 'contracts.inc').read_text(
        encoding='utf-8')
    rep.check('source.contracts_max_16',
              'K64_APP_SYS_CALL_MAX    = 16' in contracts)
    rep.check('source.contracts_max_cross_check',
              'K64_APP_SYS_CALL_MAX <> CPU_SYS_CALL_MAX' in contracts)

    # contracts: the FSYS segment is a pure tail append after the FS one
    rep.check('source.contracts_fsys_after_fs',
              contracts.find('K64_FSYS_') > contracts.find('K64_FS_') >= 0)
    rep.check('source.contracts_fsys_numbers',
              'K64_FSYS_SYS_OPEN    = 4' in contracts and
              'K64_FSYS_SYS_READDIR = 9' in contracts)
    rep.check('source.contracts_fsys_domain',
              'K64_FSYS_USER_VA_BASE    = 0x100400000' in contracts and
              'K64_FSYS_USER_VA_LIMIT   = 0x100800000' in contracts)
    rep.check('source.contracts_fsys_budget',
              'K64_FSYS_TABLE_BUDGET   = 3' in contracts and
              'K64_FSYS_TABLE_BUDGET <> MM_USER_TABLE_BUDGET' in contracts)
    rep.check('source.contracts_fsys_mapping',
              'K64_FSYS_MAP_EPERM     = K64_ENOTSUP' in contracts and
              'K64_FSYS_MAP_ENOMEM    = K64_EIO' in contracts and
              'K64_FSYS_MAP_EBUSY     = K64_EIO' in contracts)
    rep.check('source.contracts_leg7_refrozen',
              contracts.rfind('K64_CORE_ACCOUNT_TABLES <> 3') >
              contracts.find('K64_FSYS_'))

    # task 138: the frozen review revisions live in the FSYS tail
    rep.check('source.contracts_task138_section',
              'task 138 review fixes, frozen revisions' in contracts)
    rep.check('source.contracts_task138_full_width',
              'SEEK_SET 0x100000000 returns 0x100000000' in contracts)
    rep.check('source.contracts_task138_posix_partial',
              'S2 POSIX partial reads' in contracts and
              'never -errno' in contracts)
    rep.check('source.contracts_task138_w2_guard',
              'clusters + 2 <= spf * 256' in contracts)
    rep.check('source.contracts_task138_w3_contract',
              'vmm_unmap_user_page(rcx = VA)' in contracts and
              'mm_pmm_drop_table' in contracts and
              'table_drops incremented by the caller' in contracts)
    rep.check('source.contracts_task138_s3',
              'including the EXIT registration failure path' in contracts)

    # foundation: the fsys stage runs after fs, before FOUNDATION-OK
    found = (root / 'kernel' / 'core' / 'foundation.inc').read_text(
        encoding='utf-8')
    fpos = found.find('k64_report kmsg_fs_ok')
    ipos = found.find('call    fsys_init')
    ppos = found.find('vmm_boot_protect', ipos)
    spos = found.find('call    fsys_probe')
    opos = found.find('k64_report kmsg_fsysprobe_ok')
    tpos = found.find('k64_report kmsg_foundation_ok')
    rep.check('source.foundation_fsys_stage_order',
              0 <= fpos < ipos < ppos < spos < opos < tpos)
    rep.check('source.foundation_fsys_protect',
              'mov     rcx, fsys_data_start' in found)
    rep.check('source.foundation_fsysprobe_marker',
              "kmsg_fsysprobe_ok     db 'K64-FSPROBE-OK', 13, 10, 0" in found)
    rep.check('source.foundation_no_fsys_skip', 'K64-FSYS-SKIP' not in found)
    # S4: the header spells out the M2/M3 stage order
    rep.check('source.foundation_header_m2_m3',
              'FS-v1 M2 stage' in found and 'M3 stage (fsys_init' in found)

    # the frozen STI ownership across the whole kernel source tree;
    # task 115 adjudication: the APP-v1 interactive POLL leg is the
    # third legal owner (LVT masked, RTEs re-masked, ticks stay 11)
    hits = []
    for path in sorted((root / 'kernel').rglob('*')):
        if path.suffix not in ('.asm', '.inc') or not path.is_file():
            continue
        text = path.read_text(encoding='utf-8')
        for m in re.finditer(r'^\s*sti\b', text, re.M):
            hits.append(str(path.relative_to(root)))
    rep.equal('source.kernel_sti_sites', hits,
              ['kernel/app/app.inc', 'kernel/desktop/desktop.inc',
               'kernel/irq/selftest.inc'])

    # the data region is real zero bytes, never a BSS promise
    data = (root / 'kernel' / 'fsys' / 'data.inc').read_text(encoding='utf-8')
    rep.check('source.data_no_rb',
              re.search(r'^\s*(\w+:)?\s*rb\s', data, re.M) is None)

    # every call in the module names its own code or a frozen seam
    allowed = {'fsys_map_error', 'fsys_check_range', 'fsys_copy_path',
               'fsys_copy_to_user', 'fs_open', 'fs_read', 'fs_seek',
               'fs_close', 'fs_stat', 'fs_readdir', 'mm_vmm_resolve',
               'vmm_map_user_page', 'pmm_alloc_page',
               'vmm_unmap_user_page', 'pmm_free_page',
               'cpu_syscall_set_handler', 'cpu_set_rsp0'}
    stray = []
    text = (root / 'kernel' / 'fsys' / 'fsys.inc').read_text(encoding='utf-8')
    for m in re.finditer(r'^\s*call\s+(\S+)', text, re.M):
        if m.group(1) not in allowed:
            stray.append(m.group(1))
    rep.check('source.call_targets', not stray, repr(stray))
    rep.check('source.no_write_path',
              not re.search(r'^\s*(fsys_write|fsys_create|fsys_unlink)',
                            text, re.M))

    # the user probe: eleven legs, the full mask, all four error classes
    probe = (root / 'user' / 'fsprobe' / 'fsprobe.asm').read_text(encoding='utf-8')
    bits = re.findall(r'or\s+qword \[fp_mask\], 1 shl (\d+)', probe)
    rep.equal('source.fsprobe_eleven_legs', bits, [str(i) for i in range(11)])
    rep.check('source.fsprobe_full_mask_verdict',
              'cmp     qword [fp_mask], FP_MASK_ALL' in probe)
    for name, count in (('K64_ENOENT', 1), ('K64_EFAULT', 3),
                        ('K64_EINVAL', 2), ('K64_ENOTSUP', 3)):
        rep.equal('source.fsprobe_%s_verdicts' % name,
                  len(re.findall(r'cmp\s+rax, %s\b' % name, probe)), count)
    rep.check('source.fsprobe_rom_reference',
              "file    '../chip8/roms/ibm.ch8'" in probe)
    rep.check('source.fsprobe_exit_through_syscall',
              'mov     eax, SYS_EXIT' in probe)
    # W1: the probe drives SEEK_SET past 4 GiB and demands the full
    # 64 bit return
    rep.check('source.fsprobe_seek_beyond_4g',
              'mov     rsi, 0x100000000' in probe and
              'cmp     rax, rsi' in probe)

    # the build prebuilds the embedded image
    build = (root / 'build_fasm2.sh').read_text(encoding='utf-8')
    rep.check('source.build_prebuilds_fsprobe',
              'user/fsprobe/fsprobe.asm' in build and
              'build/user-fsprobe/fsprobe.bin' in build)


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
    ok, text = asm_.run(root / 'tests/stage2-fsys/layout.asm', hi_path,
                        org=KERNEL_BASE)
    if not rep.check('reloc.assembles_at_kernel_base', ok, text.strip()[-400:]):
        return
    hi = load_image(hi_path, KERNEL_BASE)
    delta = KERNEL_BASE
    labels = M[2:61]      # every address label: fsys, fs, mm and cpu
    shifted = all(hi[n] == low[n] + delta for n in labels)
    rep.check('reloc.labels_shift_by_kernel_base', shifted)
    for name in CONSTANTS:
        rep.equal('reloc.%s_unchanged' % name, hi[name], low[name])

    rep.check('reloc.data_bytes_identical',
              hi.data[hi.at('data_start'):hi.at('data_end')] ==
              low.data[low.at('data_start'):low.at('data_end')])

    lo_body = low.data[low.at('map_error'):low.at('probe_image_end')]
    hi_body = hi.data[hi.at('map_error'):hi.at('probe_image_end')]
    if not rep.check('reloc.code_same_length', len(lo_body) == len(hi_body)):
        return
    allowed = set()
    windows = 0
    # mov r64, sign extended imm32 windows: only the low 32 bits are
    # emitted, the visible difference is the base modulo 2**32; the
    # REX.W+REX.B form (49 C7) addresses r8-r15
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
    # absolute disp32 addressing with an r12 index (SIB e5): the probe
    # page array stores and loads, the disp32 shifts by the base
    for prefix in (b'\x4A\x89\x14\xE5', b'\x4A\x8B\x3C\xE5', b'\x4A\x8B\x14\xE5',
                   b'\x4A\x8B\x0C\xE5', b'\x4A\xC7\x04\xE5'):
        for p in find_all(lo_body, prefix):
            if p + 8 > len(lo_body):
                continue
            lv = struct.unpack_from('<I', lo_body, p + 4)[0]
            hv = struct.unpack_from('<I', hi_body, p + 4)[0]
            if (low['data_start'] <= lv <= low['data_end'] and
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
    # defect class: error map drift. ENOENT must stay -3, never the
    # 32-bit value or a neighbouring code
    ('errmap-drift', 'kernel/fsys/fsys.inc',
     'db      0, -1, -9, -3, -1, -6, -6, -9, -1, -9, -1, -11, -9',
     'db      0, -1, -9, -1, -1, -6, -6, -9, -1, -9, -1, -11, -9', 'check'),
    # defect class: domain escape. The upper bound compare is the only
    # thing keeping a buffer inside the frozen user domain
    ('domain-upper-guard-removed', 'kernel/fsys/fsys.inc',
     '        mov     rax, FSYS_USER_VA_LIMIT\n'
     '        cmp     r13, rax\n        ja      .fault\n',
     '        mov     rax, FSYS_USER_VA_LIMIT\n', 'check'),
    # defect class: supervisor page leak. A kernel page inside the
    # domain window must still be refused
    ('us-guard-removed', 'kernel/fsys/fsys.inc',
     '        cmp     qword [fsys_resolve_rec + 24], 1\n'
     '        jne     .fault                  ; effective US over the whole path\n',
     '        cmp     qword [fsys_resolve_rec + 24], 1\n', 'check'),
    # defect class: write to a read-only user page. The copy-out path
    # rechecks RW even after the range validation
    ('rw-guard-removed', 'kernel/fsys/fsys.inc',
     '        cmp     qword [fsys_resolve_rec + 16], 1\n        jne     .fault\n',
     '        cmp     qword [fsys_resolve_rec + 16], 1\n', 'check'),
    # defect class: bounce bypass. The fs core must never see a user
    # pointer, the payload lands in the bounce page first
    ('bounce-bypass', 'kernel/fsys/fsys.inc',
     '        mov     rdx, fsys_bounce\n        mov     r8, rbx\n        call    fs_read\n',
     '        mov     rdx, r13\n        mov     r8, rbx\n        call    fs_read\n',
     'check'),
    # defect class: double page offset. rec+48 already carries the
    # page offset; folding it in again reads 2*offset into the page
    ('double-page-offset', 'kernel/fsys/fsys.inc',
     '        mov     rax, [fsys_resolve_rec + 48]\n        mov     al, [rax]',
     '        mov     rax, r12\n        and     eax, 0xFFF\n'
     '        add     rax, [fsys_resolve_rec + 48]\n        mov     al, [rax]',
     'check'),
    # defect class: table budget. The +3 adjudication is frozen, the
    # probe must refuse any other delta
    ('budget-guard-removed', 'kernel/fsys/fsys.inc',
     '        cmp     rax, FSYS_TABLE_BUDGET\n        jne     .fail_eio\n',
     '', 'check'),
    # the budget constant itself: the mm cross check fires
    ('budget-drift', 'kernel/fsys/api.inc',
     'FSYS_TABLE_BUDGET = 3', 'FSYS_TABLE_BUDGET = 4', 'assemble'),
    # the domain mirror: the mm cross check fires
    ('va-domain-drift', 'kernel/fsys/api.inc',
     'FSYS_USER_VA_BASE   = 0x100400000', 'FSYS_USER_VA_BASE   = 0x100401000',
     'assemble'),
    # defect class: write modes. v1 is read only, flags must be refused
    # before any path resolution
    ('open-flags-guard-removed', 'kernel/fsys/fsys.inc',
     '        test    esi, esi\n'
     '        jnz     .enotsup                ; write modes are refused in v1\n',
     '        test    esi, esi\n', 'check'),
    # defect class: fd table overrun. jae -> ja lets fd 8 index one
    # slot past the frozen eight slot table
    ('fd-bound-drift', 'kernel/fsys/fsys.inc',
     '        jae     .einval\n        test    r14, r14\n'
     '        js      .einval\n        mov     rcx, r13\n',
     '        ja      .einval\n        test    r14, r14\n'
     '        js      .einval\n        mov     rcx, r13\n', 'check'),
    # defect class: the EXIT handler must park back into the kernel,
    # a SYSRET would return into a dead user context
    ('exit-sysret-leak', 'kernel/fsys/fsys.inc',
     '        jmp     qword [fsys_probe_rip]\n', '        sysretq\n', 'check'),
    # defect class: counter discipline. The frozen M1/M2 judge totals
    # must be restored before the verdict
    ('counter-restore-removed', 'kernel/fsys/fsys.inc',
     '        mov     rax, [fsys_saved_fs_reads]\n        mov     [fs_test_reads], rax\n',
     '', 'check'),
    # defect class: dirent geometry. A ragged chunk would tear entries
    ('readdir-align-removed', 'kernel/fsys/fsys.inc',
     '        and     rbx, -FS_DIRENT_SIZE    ; whole entries only\n        jz      .done\n',
     '', 'check'),
    # defect class: RSP0 collision. Without the private stack switch
    # the SYSCALL frame lands on the live foundation stack
    ('rsp0-switch-removed', 'kernel/fsys/fsys.inc',
     '        mov     rcx, fsys_syscall_stack_top\n        call    cpu_set_rsp0\n',
     '        mov     rcx, fsys_syscall_stack_top\n', 'check'),
    # defect class: W1 truncation. The SEEK success path must return
    # the full rdx, a 32 bit mov turns >= 2 GiB offsets into garbage
    ('seek-truncation', 'kernel/fsys/fsys.inc',
     '        mov     rax, rdx                ; full 64 bit offset (W1)\n'
     '        fsys_leave\n        ret\n',
     '        mov     eax, edx\n        jmp     .out\n', 'check'),
    # defect class: W3 leak. Without the unmap call the frozen user
    # domain stays occupied and APP-v1 can never load at the same VA
    ('teardown-unmap-removed', 'kernel/fsys/fsys.inc',
     '        call    vmm_unmap_user_page\n', '', 'check'),
    # defect class: W3 frame leak. The physical frames must go back
    # to the allocator on every exit path
    ('teardown-free-removed', 'kernel/fsys/fsys.inc',
     '        call    pmm_free_page\n', '', 'check'),
    # defect class: S3 RSP0 leak. The EXIT registration failure path
    # must restore the foundation TSS.RSP0, not fall into .fail bare
    ('rsp0-restore-removed', 'kernel/fsys/fsys.inc',
     '        jnz     .fail_rsp0\n', '        jnz     .fail\n', 'check'),
]


def run_fixtures(rep, root, out, fasmg, fasm2_dir):
    checker = root / 'tests' / 'stage2-fsys' / 'check.py'
    for name, rel, old, new, where in FIXTURES:
        with tempfile.TemporaryDirectory(prefix='stage2-fsys-%s-' % name) as tmp:
            tree = Path(tmp)
            (tree / 'tests').mkdir(parents=True)
            shutil.copytree(root / 'kernel', tree / 'kernel')
            shutil.copytree(root / 'tests' / 'stage2-fsys',
                            tree / 'tests' / 'stage2-fsys')
            # the embedded image and the ROM reference need the user
            # sources and the prebuilt image inside the sandbox too
            shutil.copytree(root / 'user' / 'chip8', tree / 'user' / 'chip8')
            shutil.copytree(root / 'user' / 'fsprobe', tree / 'user' / 'fsprobe')
            (tree / 'build' / 'user-fsprobe').mkdir(parents=True)
            shutil.copy2(root / 'build' / 'user-fsprobe' / 'fsprobe.bin',
                         tree / 'build' / 'user-fsprobe' / 'fsprobe.bin')
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
    out = Path(args.out).resolve() if args.out else root / 'build' / 'stage2-fsys'
    out.mkdir(parents=True, exist_ok=True)

    asm_ = find_assembler(root, args.fasm2_dir)
    if asm_ is None:
        print('check.py: no fasm2 host found, set FASMG or --fasm2-dir', file=sys.stderr)
        return 2

    rep = Report()
    rep.check('assembler.available', True, str(asm_.fasmg))
    rep.check('assembler.include', (asm_.fasm2_dir / 'include' / 'fasm2.inc').is_file(),
              str(asm_.fasm2_dir))

    # the embedded image must be fresh: rebuild it exactly like
    # build_fasm2.sh does before the probe consumes it
    fsprobe_bin = root / 'build' / 'user-fsprobe' / 'fsprobe.bin'
    fsprobe_bin.parent.mkdir(parents=True, exist_ok=True)
    ok, text = asm_.run(root / 'user' / 'fsprobe' / 'fsprobe.asm', fsprobe_bin)
    if not rep.check('assembler.fsprobe', ok, text.strip()[-400:]):
        rep.print(True)
        return 1

    low_path = out / 'layout.bin'
    ok, text = asm_.run(root / 'tests/stage2-fsys/layout.asm', low_path)
    if not rep.check('assembler.probe', ok, text.strip()[-600:]):
        rep.print(True)
        return 1
    rep.check('assembler.probe_bytes', low_path.stat().st_size > 0,
              '%d bytes' % low_path.stat().st_size)
    rep.check('assembler.probe_quiet', 'error' not in text.lower(),
              text.strip()[-200:] or 'no diagnostics')

    img = load_image(low_path)
    check_manifest(rep, img)
    check_data(rep, img, root)
    check_frames(rep, img)
    check_map_error(rep, img)
    check_check_range(rep, img)
    check_copy_path(rep, img)
    check_copy_to_user(rep, img)
    check_handlers(rep, img)
    check_exit(rep, img)
    check_init(rep, img)
    check_probe(rep, img)
    check_module_hygiene(rep, img)
    check_model(rep, img)
    check_sources(rep, root)
    check_integration(rep, asm_, root, out)
    check_position_independence(rep, asm_, root, out, img)

    if not args.no_fixtures:
        run_fixtures(rep, root, out, asm_.fasmg, asm_.fasm2_dir)

    print('stage2-fsys: layout.bin %d bytes, code %d bytes, data %d bytes' %
          (len(img.data), img['probe_image_end'] - img['map_error'],
           img['data_end'] - img['data_start']))
    return 0 if rep.print(args.verbose) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))