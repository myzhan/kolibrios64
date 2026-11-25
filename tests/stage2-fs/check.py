#!/usr/bin/env python3
"""Stage 2 FS module checks, task 132, contract FS-v1 M2. No machine
is started.

Everything here is measured, nothing is taken from a comment. The probe
tests/stage2-fs/layout.asm assembles kernel/fs/fs.asm exactly as the
kernel will include it (behind cpu.asm and blk.asm, whose seams it
consumes) and appends a manifest of label addresses. This script then

  * compares the manifest constants and every data label offset against
    the fixed values of contract FS-v1 fs, repeated here independently
    of the assembly sources;
  * decodes the emitted bytes: the full register frames, the single
    sector cache hit/miss shape, the BPB validation chain, the FAT16
    EOC/bad/range guards, the hop budget cycle guard, the kanji fold,
    the read-only open refusal, the readdir slot walk and the eight
    self test leg trampolines; every call target must stay inside the
    module or name blk_read / the CPU console, and every RIP relative
    write must land inside the fs data region;
  * imports tests/stage2-fs/fat16_model.py, re-runs its 15 self tests,
    then replays the exact kernel leg sequence through KernelFsMirror,
    a call-for-call mirror of kernel/fs/fs.inc with the same cache
    discipline: the derived sector fetch totals (24 plain, 48 BIG)
    must equal the frozen manifest constants, and every payload byte
    must equal the independent oracle on the same disk.img;
  * exercises the negative classes on both sides: illegal BPB variants,
    cluster chain loop, out-of-range cluster, bad cluster marker,
    reserved chain value, short chain, missing path, EOF, cross cluster
    boundaries, write refusal and bad arguments;
  * checks the data region is 8 real zero pages, page aligned at both
    ends, with the 16 KiB FAT cache page aligned inside it;
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
import importlib.util
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

# ----------------------------------------------------------------------
# contract FS-v1 fs, fixed here independently of the sources
# ----------------------------------------------------------------------

PAGE = 0x1000
KERNEL_BASE = 0xFFFFFFFF80000000

OK = 0
EINVAL = -1
ENOMEM = -2
ENOENT = -3
EEXIST = -4
EPERM = -5
ENOTSUP = -6
ENOTREADY = -7
EALREADY = -8
EIO = -9
EOVERFLOW = -10
EFAULT = -11
EBUSY = -12

SECTOR_SIZE = 512
SECTOR_SHIFT = 9
ENTRY_SIZE = 32
ENTRY_SHIFT = 5
ENTRIES_PER_SECTOR = 16

BPB_BPS = 11
BPB_SPC = 13
BPB_RESERVED = 14
BPB_NFATS = 16
BPB_ROOT_ENTRIES = 17
BPB_TOTAL16 = 19
BPB_SPF = 22
BPB_TOTAL32 = 32
BPB_SIG = 510
BPB_SIG_VALUE = 0xAA55

FAT16_MIN_CLUSTERS = 4085
FAT16_MAX_CLUSTERS = 65524
FAT16_BAD = 0xFFF7
FAT16_EOC_MIN = 0xFFF8

ATTR_READ_ONLY = 0x01
ATTR_VOLUME_ID = 0x08
ATTR_LFN = 0x0F
ATTR_DIRECTORY = 0x10
DIR_END = 0x00
DIR_DELETED = 0xE5
DIR_KANJI = 0x05

MAX_FDS = 8
FD_SIZE = 64
PATH_MAX = 128
COMP_MAX = 12
FD_IN_USE = 0
FD_IS_DIR = 8
FD_CLUSTER = 16
FD_SIZE_OFF = 24
FD_POS = 32
FAT_CACHE_SECTORS = 32

O_RDONLY = 0
O_WRONLY = 1
O_RDWR = 2
O_ACCMODE = 3
SEEK_SET = 0
SEEK_CUR = 1
SEEK_END = 2
DIRENT_SIZE = 32
STAT_SIZE = 64

# frozen images: build/fs-v1/disk.img and the --manifest BIG.BIN image
IMAGE_SHA = 'e9e83dad3f6d5193ac15c7a35834cc5190ca34e978b537d80be9fa9beb7bc42b'
BIG_IMAGE_SHA = 'a810280509cd05f67dce37dc46e67cbfeb2e9432b73a105108f9a2d298500543'

TEST_IBM_SIZE = 132
TEST_TETRIS_SIZE = 494
TEST_BIG_SIZE = 8229
TEST_ROM_PREFIX = 0x08610C602AA2E000
TEST_BIG_PREFIX = 0x342D261F18110A03

TEST_SPC = 2
TEST_RESERVED = 1
TEST_NFATS = 2
TEST_SPF = 32
TEST_ROOT_ENTRIES = 512
TEST_CLUSTERS = 8143
TEST_TOTAL = 16384
TEST_FAT_LBA = 1
TEST_ROOT_LBA = 65
TEST_DATA_LBA = 97

ST_BPB = 0x01
ST_IBM = 0x02
ST_TETRIS = 0x04
ST_SEEK = 0x08
ST_NEG = 0x10
ST_BIG = 0x20
ST_STAT = 0x40
ST_COUNT = 0x80

# the deterministic totals of the frozen leg sequence: init BPB + init
# FAT + every single sector cache miss (22 plain, 46 BIG); the blk
# counter sees 7 self test chunks + 5 init chunks + the same misses
TEST_READS_FINAL = 24
TEST_READS_FINAL_BIG = 48
BLK_READS_FINAL = 34
BLK_READS_FINAL_BIG = 58

# the data layout, offsets from fs_data_start
DATA_LABELS = {
    'ready': 0, 'spc': 8, 'reserved': 16, 'nfats': 24, 'spf': 32,
    'root_entries': 40, 'root_sectors': 48, 'total': 56, 'clusters': 64,
    'fat_lba': 72, 'root_lba': 80, 'data_lba': 88,
    'sec_tag': 96, 'sec_valid': 104,
    'test_mask': 112, 'test_reads': 120, 'test_big_present': 128,
    'fd_table': 136, 'ent_buf': 648, 'name_buf': 680, 'bpb_buf': 696,
    'sec_cache': 1208, 'test_ibm_buf': 1720, 'test_tetris_buf': 2232,
    'test_big_buf': 2744, 'test_scratch': 11960, 'test_dirents': 12472,
    'test_stat': 12568, 'fat_cache': 16384,
}
DATA_SIZE = 32768

MSG_SELFTEST = b'fs selftest mask = \x00'
MSG_NL = b'\r\n\x00'

PATHS = {
    'p_ibm_lower': b'/roms/ibm.ch8\x00',
    'p_ibm': b'/ROMS/IBM.CH8\x00',
    'p_tetris': b'/ROMS/TETRIS1.CH8\x00',
    'p_missing': b'/ROMS/MISSING.CH8\x00',
    'p_ibm_x': b'/ROMS/IBM.CH8/X\x00',
    'p_nope': b'/NOPE\x00',
    'p_roms': b'/ROMS\x00',
    'p_root': b'/\x00',
    'p_programs': b'/PROGRAMS\x00',
    'p_big': b'/PROGRAMS/BIG.BIN\x00',
}
NAMES = {
    'n_roms': b'ROMS       ',
    'n_programs': b'PROGRAMS   ',
    'n_ibm': b'IBM     CH8',
    'n_tetris': b'TETRIS1 CH8',
    'n_big': b'BIG     BIN',
}

MAGIC = b'K64FSP1'
MANIFEST_VERSION = 1

# manifest entry order, see tests/stage2-fs/layout.asm
M = [
    'magic', 'version',
    'code_start', 'code_end', 'data_start', 'data_end',
    'sec_read', 'fat_entry', 'cluster_lba', 'norm_comp', 'dir_find',
    'resolve', 'open', 'read', 'seek', 'close', 'stat', 'readdir',
    'init', 'selftest',
    'st_fail_bpb', 'st_fail_ibm', 'st_fail_tetris', 'st_fail_seek',
    'st_fail_neg', 'st_fail_big', 'st_fail_stat', 'st_fail_count',
    'st_bpb_done', 'st_ibm_done', 'st_tetris_done', 'st_seek_done',
    'st_neg_done', 'st_big_done', 'st_stat_done', 'st_count_done',
    'msg_selftest_mask', 'msg_nl',
    'p_ibm_lower', 'p_ibm', 'p_tetris', 'p_missing', 'p_ibm_x',
    'p_nope', 'p_roms', 'p_root', 'p_programs', 'p_big',
    'n_roms', 'n_programs', 'n_ibm', 'n_tetris', 'n_big',
    'ready', 'spc', 'reserved', 'nfats', 'spf', 'root_entries',
    'root_sectors', 'total', 'clusters', 'fat_lba', 'root_lba',
    'data_lba', 'sec_tag', 'sec_valid', 'test_mask', 'test_reads',
    'test_big_present', 'fd_table', 'ent_buf', 'name_buf', 'bpb_buf',
    'sec_cache', 'test_ibm_buf', 'test_tetris_buf', 'test_big_buf',
    'test_scratch', 'test_dirents', 'test_stat', 'fat_cache',
    'blk_read', 'blk_ready', 'blk_capacity', 'blk_test_reads',
    'cpu_com1_puts', 'cpu_com1_puthex',
    'kernel_base', 'page_size',
    'ok', 'einval', 'enomem', 'enoent', 'eexist', 'eperm', 'enotsup',
    'enotready', 'ealready', 'eio', 'eoverflow', 'efault', 'ebusy',
    'sector_size', 'sector_shift', 'entry_size', 'entry_shift',
    'entries_per_sector',
    'bpb_bps', 'bpb_spc', 'bpb_reserved', 'bpb_nfats', 'bpb_root_entries',
    'bpb_total16', 'bpb_spf', 'bpb_total32', 'bpb_sig', 'bpb_sig_value',
    'fat16_min_clusters', 'fat16_max_clusters', 'fat16_bad',
    'fat16_eoc_min',
    'attr_read_only', 'attr_volume_id', 'attr_lfn', 'attr_directory',
    'dir_end', 'dir_deleted', 'dir_kanji',
    'max_fds', 'fd_size', 'path_max', 'comp_max',
    'fd_in_use', 'fd_is_dir', 'fd_cluster', 'fd_size_off', 'fd_pos',
    'fat_cache_sectors',
    'o_rdonly', 'o_wronly', 'o_rdwr', 'o_accmode',
    'seek_set', 'seek_cur', 'seek_end',
    'dirent_size', 'stat_size',
    'test_ibm_size', 'test_tetris_size', 'test_big_size',
    'test_rom_prefix', 'test_big_prefix',
    'test_spc', 'test_reserved', 'test_nfats', 'test_spf',
    'test_root_entries', 'test_clusters', 'test_total',
    'test_fat_lba', 'test_root_lba', 'test_data_lba',
    'st_bpb', 'st_ibm', 'st_tetris', 'st_seek', 'st_neg', 'st_big',
    'st_stat', 'st_count',
    'test_reads_final', 'test_reads_final_big',
]

# manifest constants that must not drift from the copies above
CONSTANTS = {
    'kernel_base': KERNEL_BASE, 'page_size': PAGE,
    'ok': OK, 'einval': EINVAL, 'enomem': ENOMEM, 'enoent': ENOENT,
    'eexist': EEXIST, 'eperm': EPERM, 'enotsup': ENOTSUP,
    'enotready': ENOTREADY, 'ealready': EALREADY, 'eio': EIO,
    'eoverflow': EOVERFLOW, 'efault': EFAULT, 'ebusy': EBUSY,
    'sector_size': SECTOR_SIZE, 'sector_shift': SECTOR_SHIFT,
    'entry_size': ENTRY_SIZE, 'entry_shift': ENTRY_SHIFT,
    'entries_per_sector': ENTRIES_PER_SECTOR,
    'bpb_bps': BPB_BPS, 'bpb_spc': BPB_SPC, 'bpb_reserved': BPB_RESERVED,
    'bpb_nfats': BPB_NFATS, 'bpb_root_entries': BPB_ROOT_ENTRIES,
    'bpb_total16': BPB_TOTAL16, 'bpb_spf': BPB_SPF,
    'bpb_total32': BPB_TOTAL32, 'bpb_sig': BPB_SIG,
    'bpb_sig_value': BPB_SIG_VALUE,
    'fat16_min_clusters': FAT16_MIN_CLUSTERS,
    'fat16_max_clusters': FAT16_MAX_CLUSTERS,
    'fat16_bad': FAT16_BAD, 'fat16_eoc_min': FAT16_EOC_MIN,
    'attr_read_only': ATTR_READ_ONLY, 'attr_volume_id': ATTR_VOLUME_ID,
    'attr_lfn': ATTR_LFN, 'attr_directory': ATTR_DIRECTORY,
    'dir_end': DIR_END, 'dir_deleted': DIR_DELETED, 'dir_kanji': DIR_KANJI,
    'max_fds': MAX_FDS, 'fd_size': FD_SIZE, 'path_max': PATH_MAX,
    'comp_max': COMP_MAX,
    'fd_in_use': FD_IN_USE, 'fd_is_dir': FD_IS_DIR,
    'fd_cluster': FD_CLUSTER, 'fd_size_off': FD_SIZE_OFF, 'fd_pos': FD_POS,
    'fat_cache_sectors': FAT_CACHE_SECTORS,
    'o_rdonly': O_RDONLY, 'o_wronly': O_WRONLY, 'o_rdwr': O_RDWR,
    'o_accmode': O_ACCMODE,
    'seek_set': SEEK_SET, 'seek_cur': SEEK_CUR, 'seek_end': SEEK_END,
    'dirent_size': DIRENT_SIZE, 'stat_size': STAT_SIZE,
    'test_ibm_size': TEST_IBM_SIZE, 'test_tetris_size': TEST_TETRIS_SIZE,
    'test_big_size': TEST_BIG_SIZE,
    'test_rom_prefix': TEST_ROM_PREFIX, 'test_big_prefix': TEST_BIG_PREFIX,
    'test_spc': TEST_SPC, 'test_reserved': TEST_RESERVED,
    'test_nfats': TEST_NFATS, 'test_spf': TEST_SPF,
    'test_root_entries': TEST_ROOT_ENTRIES, 'test_clusters': TEST_CLUSTERS,
    'test_total': TEST_TOTAL, 'test_fat_lba': TEST_FAT_LBA,
    'test_root_lba': TEST_ROOT_LBA, 'test_data_lba': TEST_DATA_LBA,
    'st_bpb': ST_BPB, 'st_ibm': ST_IBM, 'st_tetris': ST_TETRIS,
    'st_seek': ST_SEEK, 'st_neg': ST_NEG, 'st_big': ST_BIG,
    'st_stat': ST_STAT, 'st_count': ST_COUNT,
    'test_reads_final': TEST_READS_FINAL,
    'test_reads_final_big': TEST_READS_FINAL_BIG,
}

FS_ENTER = bytes.fromhex('55 53 56 57 41 54 41 55 41 56 41 57 48 83 EC 28')
FS_LEAVE = bytes.fromhex('48 83 C4 28 41 5F 41 5E 41 5D 41 5C 5F 5E 5B 5D')
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


def rip_refs(img, at, end, prefix, modrm, tail, label):
    return [s for s, t, _ in rip_sites(img, at, end, prefix, modrm, tail)
            if t == img.at(label)]


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


def jcc_after(img, pos, limit=8):
    """The (site, target) of the conditional jump right after pos."""
    body = img.data
    for j in range(pos, min(pos + limit, len(body) - 2)):
        if body[j] == 0x0F and 0x80 <= body[j + 1] <= 0x8F:
            rel = struct.unpack_from('<i', body, j + 2)[0]
            return j, j + 6 + rel
        if 0x70 <= body[j] <= 0x7F:
            rel = struct.unpack_from('<b', body, j + 1)[0]
            return j, j + 2 + rel
    return None


def mov_r64_needles(reg_lo, value):
    """'mov r64, value': sign extended imm32 (48 C7 C0+r) when it fits,
    full imm64 (48 B8+r) otherwise."""
    return [b'\x48\xC7' + bytes((0xC0 + reg_lo,)) + struct.pack('<I', value & 0xFFFFFFFF),
            b'\x48' + bytes((0xB8 + reg_lo,)) + struct.pack('<Q', value)]


def find_mov_r64(img, at, end, reg_lo, value):
    for needle in mov_r64_needles(reg_lo, value):
        p = img.data.find(needle, at, end)
        if p >= 0:
            return p
    return -1


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
        print('stage2-fs: %d checks, %d failed' % (total, len(self.failures)))
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
            cmd += ['-i', 'FS_PROBE_ORG=%#x' % org]
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
    rep.equal('manifest.entry_count', len(M), 173)
    for name, want in CONSTANTS.items():
        # negative status codes live in the manifest as two's complement
        rep.equal('manifest.%s' % name, img[name], want & 0xFFFFFFFFFFFFFFFF)

    for name, off in DATA_LABELS.items():
        rep.equal('layout.%s_offset' % name,
                  img[name] - img['data_start'], off)

    lo, hi = img['code_start'], img['code_end']
    rep.check('layout.code_non_empty', hi > lo, '%#x..%#x' % (lo, hi))
    rep.equal('layout.api_emits_no_code', img['sec_read'], lo)
    order = ['sec_read', 'fat_entry', 'cluster_lba', 'norm_comp',
             'dir_find', 'resolve', 'open', 'read', 'seek', 'close',
             'stat', 'readdir', 'init', 'selftest',
             'st_fail_bpb', 'st_fail_ibm', 'st_fail_tetris',
             'st_fail_seek', 'st_fail_neg', 'st_fail_big',
             'st_fail_stat', 'st_fail_count',
             'msg_selftest_mask', 'msg_nl']
    chain = [img[name] for name in order]
    rep.check('layout.function_order', chain == sorted(chain) and
              all(lo <= v < hi for v in chain),
              'emission order: cache leaves, resolution, file calls, init')
    rep.check('layout.code_before_data', hi <= img['data_start'])
    rep.check('layout.data_page_aligned',
              img['data_start'] % PAGE == 0 and img['data_end'] % PAGE == 0)
    rep.equal('layout.data_size', img['data_end'] - img['data_start'], DATA_SIZE)
    rep.equal('layout.module_ends_with_data', img.at('data_end'), img.probe_at)
    for name in ('blk_read', 'cpu_com1_puts', 'cpu_com1_puthex'):
        rep.check('layout.%s_before_module' % name, 0 < img[name] < lo,
                  '%#x' % img[name])
    rep.check('layout.fat_cache_page_aligned',
              (img['fat_cache'] - img['data_start']) % PAGE == 0)
    rep.equal('layout.fat_cache_size',
              img['data_start'] + DATA_SIZE - img['fat_cache'],
              FAT_CACHE_SECTORS * SECTOR_SIZE)
    rep.equal('layout.fd_table_size', img['ent_buf'] - img['fd_table'],
              MAX_FDS * FD_SIZE)
    rep.equal('layout.big_buf_size',
              img['test_scratch'] - img['test_big_buf'], 9216)
    # the leg done labels sit in leg order inside the self test
    dones = [img['st_%s_done' % n] for n in
             ('bpb', 'ibm', 'tetris', 'seek', 'neg', 'big', 'stat', 'count')]
    rep.check('layout.leg_done_order',
              dones == sorted(dones) and
              img['selftest'] < dones[0] and dones[-1] < img['st_fail_bpb'])


def check_data(rep, img):
    d = img.data
    start, end = img.at('data_start'), img.at('data_end')
    block = d[start:end]
    rep.check('data.all_zero_real_bytes', block == bytes(DATA_SIZE),
              '%d non-zero bytes' % sum(1 for b in block if b))
    for name in ('ready', 'test_mask', 'test_reads', 'test_big_present',
                 'sec_valid'):
        rep.equal('data.%s_default_zero' % name,
                  u64(d, start + DATA_LABELS[name]), 0)
    rep.check('data.msg_selftest_bytes',
              d[img.at('msg_selftest_mask'):
                img.at('msg_selftest_mask') + len(MSG_SELFTEST)] == MSG_SELFTEST)
    rep.check('data.msg_nl_bytes',
              d[img.at('msg_nl'):img.at('msg_nl') + len(MSG_NL)] == MSG_NL)
    for name, blob in PATHS.items():
        rep.check('data.%s_bytes' % name,
                  d[img.at(name):img.at(name) + len(blob)] == blob)
    for name, blob in NAMES.items():
        rep.check('data.%s_bytes' % name,
                  d[img.at(name):img.at(name) + len(blob)] == blob)
    # each raw 8.3 name constant is EXACTLY 11 bytes: consecutive name
    # labels must sit 11 bytes apart, a stray pad byte would shift the
    # whole tail without breaking the per-label byte compare above
    order = ['n_roms', 'n_programs', 'n_ibm', 'n_tetris', 'n_big']
    gaps = [img.at(b) - img.at(a) for a, b in zip(order, order[1:])]
    rep.equal('data.name_constants_are_11_bytes', gaps, [11, 11, 11, 11])
    rep.equal('data.names_close_the_code',
              img.at('n_big') + len(NAMES['n_big']), img.at('code_end'))


def check_frames(rep, img):
    d = img.data
    framed = ['sec_read', 'dir_find', 'resolve', 'open', 'read', 'seek',
              'close', 'stat', 'readdir', 'init', 'selftest']
    for name in framed:
        at = img.at(name)
        rep.check('frame.%s_full_frame' % name,
                  d[at:at + len(FS_ENTER)] == FS_ENTER)
    for name in ('fat_entry', 'cluster_lba', 'norm_comp'):
        at = img.at(name)
        rep.check('frame.%s_is_leaf' % name,
                  d[at:at + len(FS_ENTER)] != FS_ENTER)
    # read and readdir keep two private locals below the shadow space
    for name in ('read', 'readdir'):
        at = img.at(name)
        rep.check('frame.%s_private_locals' % name,
                  d[at + len(FS_ENTER):at + len(FS_ENTER) + 4] ==
                  bytes.fromhex('48 83 EC 10'))
        end = img.at('seek') if name == 'read' else img.at('init')
        rep.check('frame.%s_locals_released' % name,
                  d.find(bytes.fromhex('48 83 C4 10') + FS_LEAVE, at, end) >= 0)
    # every framed function returns through the full leave + ret
    for name in framed:
        at = img.at(name)
        nxt = {'sec_read': 'fat_entry', 'dir_find': 'resolve',
               'resolve': 'open', 'open': 'read', 'read': 'seek',
               'seek': 'close', 'close': 'stat', 'stat': 'readdir',
               'readdir': 'init', 'init': 'selftest',
               'selftest': 'st_fail_bpb'}[name]
        end = img.at(nxt)
        rep.check('frame.%s_leave_ret' % name,
                  d.find(FS_LEAVE + bytes((RET,)), at, end) >= 0)


def check_sec_read(rep, img):
    at, end = region(img, 'sec_read', 'fat_entry')
    d = img.data
    calls = [t for _, t in call_sites(img, at, end)]
    rep.equal('cache.single_blk_read_call', calls, [img.at('blk_read')])
    rep.equal('cache.miss_counter_sites',
              len(rip_refs(img, at, end, b'\x48\xFF', 0x05, 0, 'test_reads')), 1)
    rep.equal('cache.tag_store',
              len(rip_refs(img, at, end, b'\x48\x89', 0x1D, 0, 'sec_tag')), 1)
    rep.equal('cache.valid_store',
              len(rip_refs(img, at, end, b'\x48\xC7', 0x05, 4, 'sec_valid')), 1)
    # the hit path compares the tag before any device traffic
    tag_cmp = d.find(bytes.fromhex('48 3B 1D'), at, end)  # cmp rbx, [rip+x]
    ok = tag_cmp >= 0
    if ok:
        disp = struct.unpack_from('<i', d, tag_cmp + 3)[0]
        ok = tag_cmp + 7 + disp == img.at('sec_tag')
    rep.check('cache.tag_compare', ok)
    j = jcc_after(img, tag_cmp + 7) if tag_cmp >= 0 else None
    rep.check('cache.hit_branch', j is not None)


def check_fat_leaves(rep, img):
    d = img.data
    at, end = region(img, 'fat_entry', 'cluster_lba')
    # range guard: cmp rcx, 2 / jb and cmp rcx, clusters+2 / jae
    rep.check('fat.range_low_guard',
              d.find(bytes.fromhex('48 83 F9 02'), at, end) >= 0)
    rep.check('fat.range_high_guard',
              d.find(bytes.fromhex('48 83 C0 02 48 39 C1'), at, end) >= 0,
              'add rax, 2 / cmp rcx, rax')
    rep.check('fat.entry_load',
              d.find(bytes.fromhex('0F B7 14 48'), at, end) >= 0,
              'movzx edx, word [rax + rcx*2]')
    at, end = region(img, 'cluster_lba', 'norm_comp')
    rep.check('lba.cluster_minus_two',
              d.find(bytes.fromhex('48 83 E8 02'), at, end) >= 0)
    rep.check('lba.spc_multiply',
              d.find(bytes.fromhex('48 0F AF 05'), at, end) >= 0,
              'imul rax, [fs_spc]')


def check_norm_comp(rep, img):
    at, end = region(img, 'norm_comp', 'dir_find')
    d = img.data
    rep.check('norm.space_fill', d.find(bytes.fromhex('B0 20'), at, end) >= 0)
    rep.check('norm.dot_literal',
              d.find(bytes.fromhex('C6 05'), at, end) >= 0 and
              d.find(bytes.fromhex('80 39 2E'), at, end) >= 0,
              "cmp byte [r9], '.'")
    rep.check('norm.dotdot_literal',
              d.find(bytes.fromhex('81 39 2E 2E'), at, end) >= 0,
              "cmp word [r9], '..'")
    rep.check('norm.comp_budget',
              d.find(bytes.fromhex('49 83 FA 0C'), at, end) >= 0,
              'cmp r10, 12')
    rep.check('norm.base_budget',
              d.find(bytes.fromhex('49 83 F8 08'), at, end) >= 0,
              'cmp r8, 8')
    rep.check('norm.ext_budget',
              d.find(bytes.fromhex('48 83 FA 03'), at, end) >= 0,
              'cmp rdx, 3')
    rep.check('norm.case_folds',
              len(find_all(d[at:end], bytes.fromhex('2C 20'))) == 2,
              'sub al, 32 in base and extension loops')


def check_dir_find(rep, img):
    at, end = region(img, 'dir_find', 'resolve')
    d = img.data
    rep.check('dir.end_mark',
              d.find(bytes.fromhex('84 C0'), at, end) >= 0, 'test al, al')
    rep.check('dir.deleted_skip',
              d.find(bytes.fromhex('3C E5'), at, end) >= 0, 'cmp al, 0xE5')
    rep.check('dir.volume_skip',
              d.find(bytes.fromhex('A8 08'), at, end) >= 0, 'test al, 8')
    rep.check('dir.lfn_skip',
              d.find(bytes.fromhex('24 0F 3C 0F'), at, end) >= 0,
              'and al, 0xF / cmp al, 0xF')
    rep.check('dir.kanji_fold',
              d.find(bytes.fromhex('3C 05'), at, end) >= 0 and
              d.find(bytes.fromhex('B0 E5'), at, end) >= 0,
              'cmp al, 5 / mov al, 0xE5')
    rep.check('dir.hop_budget_load',
              d.find(bytes.fromhex('4C 8B 25'), at, end) >= 0,
              'mov r12, [fs_clusters]')
    dec_at = d.find(bytes.fromhex('49 FF CC'), at, end)
    rep.check('dir.hop_budget_dec', dec_at >= 0, 'dec r12')
    j = jcc_after(img, dec_at + 3) if dec_at >= 0 else None
    rep.check('dir.hop_budget_guarded', j is not None)
    rep.check('dir.eoc_compare',
              d.find(bytes.fromhex('81 FA F8 FF 00 00'), at, end) >= 0,
              'cmp edx, 0xFFF8')
    rep.check('dir.bad_compare',
              d.find(bytes.fromhex('81 FA F7 FF 00 00'), at, end) >= 0,
              'cmp edx, 0xFFF7')
    rep.check('dir.reserved_compare',
              d.find(bytes.fromhex('83 FA 02'), at, end) >= 0,
              'cmp edx, 2')
    rep.check('dir.entry_publish',
              d.find(bytes.fromhex('F3 A4'), at, end) >= 0, 'rep movsb')


def check_read(rep, img):
    at, end = region(img, 'read', 'seek')
    d = img.data
    rep.check('read.fd_range_guard',
              d.find(bytes.fromhex('83 F9 08'), at, end) >= 0,
              'cmp ecx, 8')
    rep.check('read.negative_length_guard',
              d.find(bytes.fromhex('4D 85 C0'), at, end) >= 0 and
              d.find(bytes.fromhex('0F 88'), at, end) >= 0,
              'test r8, r8 / js')
    rep.check('read.eof_clamp',
              d.find(bytes.fromhex('4C 0F 47 E0'), at, end) >= 0,
              'cmova r12, rax: n = min(len, size - pos)')
    rep.check('read.eof_compare',
              d.find(bytes.fromhex('4D 39 F5'), at, end) >= 0,
              'cmp r13, r14: pos vs size')
    rep.equal('read.chain_eoc_guards',
              len(find_all(d[at:end], bytes.fromhex('81 FA F8 FF 00 00'))), 2,
              'the advance walk and the cross cluster step both refuse EOC')
    rep.equal('read.chain_bad_guards',
              len(find_all(d[at:end], bytes.fromhex('81 FA F7 FF 00 00'))), 2)
    rep.check('read.sector_fetch_call',
              any(t == img.at('sec_read')
                  for _, t in call_sites(img, at, end)))
    rep.check('read.byte_copy_loop',
              d.find(bytes.fromhex('8A 02 88 45 00'), at, end) >= 0,
              'mov al, [rdx] / mov [rbp], al')
    # the copy loop advances rbp per byte (inc rbp = 48 FF C5); a second
    # 'add rbp, r9' (4C 01 CD) after the loop would double the cursor
    # stride, zeroing every odd sector and overshooting the buffer. The
    # QEMU BIG.BIN leg caught exactly this; freeze the single advance.
    rep.check('read.copy_loop_inc_rbp',
              d.find(bytes.fromhex('8A 02 88 45 00 48 FF C2 48 FF C5'),
                     at, end) >= 0,
              'mov al,[rdx] / mov [rbp],al / inc rdx / inc rbp')
    rep.check('read.cursor_single_advance',
              d.find(bytes.fromhex('4C 01 CD'), at, end) < 0,
              'no add rbp, r9 after the per-byte copy loop')


def check_seek_close(rep, img):
    d = img.data
    at, end = region(img, 'seek', 'close')
    rep.check('seek.whence_guard',
              d.find(bytes.fromhex('41 83 F8 02'), at, end) >= 0 and
              (d.find(bytes.fromhex('0F 87'), at, end) >= 0 or
               d.find(bytes.fromhex('77'), at, end) >= 0),
              'cmp r8d, 2 / ja')
    rep.check('seek.negative_result_guard',
              d.find(bytes.fromhex('48 85 C0'), at, end) >= 0 and
              (d.find(bytes.fromhex('0F 88'), at, end) >= 0 or
               d.find(bytes.fromhex('78'), at, end) >= 0),
              'test rax, rax / js')
    at, end = region(img, 'close', 'stat')
    rep.check('close.slot_clear',
              d.find(bytes.fromhex('48 C7 03 00 00 00 00'), at, end) >= 0,
              'mov qword [rbx], 0')


def check_open_stat(rep, img):
    d = img.data
    at, end = region(img, 'open', 'read')
    rep.check('open.ready_guard',
              d.find(bytes.fromhex('48 83 3D'), at, end) >= 0,
              'cmp qword [fs_ready], 0')
    p = d.find(bytes.fromhex('83 E0 03'), at, end)
    rep.check('open.write_refusal', p >= 0, 'and eax, FS_O_ACCMODE')
    # the JNZ must sit immediately after the AND and land on the EPERM
    # stub (mov eax, -5); a later unrelated JNZ must not satisfy this
    j = jcc_after(img, p + 3, limit=1) if p >= 0 else None
    ok = False
    if j is not None:
        site, target = j
        op = d[site]
        is_jnz = op == 0x75 or (op == 0x0F and d[site + 1] == 0x85)
        ok = is_jnz and d[target:target + 5] == bytes.fromhex('B8 FB FF FF FF')
    rep.check('open.write_refusal_guarded', ok,
              'jnz .eperm right after and eax, FS_O_ACCMODE')
    rep.check('open.slot_budget',
              d.find(bytes.fromhex('41 83 F9 08'), at, end) >= 0,
              'cmp r9d, 8')
    rep.check('open.enomem_status',
              d.find(bytes.fromhex('B8 FE FF FF FF'), at, end) >= 0)
    at, end = region(img, 'stat', 'readdir')
    rep.check('stat.reserved_zero_fill',
              d.find(bytes.fromhex('48 C1 E9 03'), at, end) >= 0 or
              d.find(bytes.fromhex('B9 08 00 00 00'), at, end) >= 0,
              'mov ecx, 8 before rep stosq')
    rep.check('stat.zero_fill',
              d.find(bytes.fromhex('F3 48 AB'), at, end) >= 0, 'rep stosq')


def check_readdir(rep, img):
    at, end = region(img, 'readdir', 'init')
    d = img.data
    rep.check('readdir.capacity_floor',
              d.find(bytes.fromhex('49 83 F8 20'), at, end) >= 0,
              'cmp r8, 32')
    rep.check('readdir.capacity_rounding',
              d.find(bytes.fromhex('48 F7 F1'), at, end) >= 0, 'div rcx')
    rep.check('readdir.entry_stride',
              d.find(bytes.fromhex('4C 6B E8 20'), at, end) >= 0,
              'imul r13, rax, 32')
    rep.check('readdir.sentinel_compare',
              d.find(bytes.fromhex('49 83 FE FF'), at, end) >= 0,
              'cmp r14, -1')
    rep.check('readdir.dot_skip',
              d.find(bytes.fromhex('80 3A 2E'), at, end) >= 0,
              "cmp byte [rdx], '.'")
    rep.check('readdir.kanji_fold',
              d.find(bytes.fromhex('3C 05'), at, end) >= 0 and
              d.find(bytes.fromhex('B0 E5'), at, end) >= 0)
    rep.check('readdir.name_copy',
              d.find(bytes.fromhex('88 44 29 10'), at, end) >= 0,
              'mov [rbp + rcx + 16], al')
    rep.check('readdir.chain_eoc_ends',
              d.find(bytes.fromhex('81 FA F8 FF 00 00'), at, end) >= 0)


def check_init(rep, img):
    at, end = region(img, 'init', 'selftest')
    d = img.data
    rep.check('init.cld', d.find(b'\xFC', at, end) >= 0)
    calls = [t for _, t in call_sites(img, at, end)
             if t == img.at('blk_read')]
    rep.equal('init.two_blk_reads', len(calls), 2)
    rep.equal('init.counter_sites',
              len(rip_refs(img, at, end, b'\x48\xFF', 0x05, 0, 'test_reads')), 2)
    # the BPB validation chain, in the frozen order
    sig = rip_sites(img, at, end, b'\x66\x81', 0x3D, 2)
    sig_ok = any(t == img.at('bpb_buf') + BPB_SIG and
                 imm == struct.pack('<H', BPB_SIG_VALUE)
                 for _, t, imm in sig)
    rep.check('init.signature_check', sig_ok)
    bps_ok = any(t == img.at('bpb_buf') + BPB_BPS and
                 imm == struct.pack('<H', SECTOR_SIZE)
                 for _, t, imm in sig)
    rep.check('init.sector_size_check', bps_ok)
    rep.check('init.spc_power_of_two',
              d.find(bytes.fromhex('89 C2 FF CA 85 D0'), at, end) >= 0,
              'mov edx, eax / dec edx / test eax, edx')
    rep.check('init.spc_cap',
              d.find(bytes.fromhex('83 F8 80'), at, end) >= 0 or
              d.find(bytes.fromhex('3D 80 00 00 00'), at, end) >= 0,
              'cmp eax, 128')
    rep.check('init.spf_cache_cap',
              d.find(bytes.fromhex('83 F8 20'), at, end) >= 0,
              'cmp eax, 32')
    rep.check('init.root_whole_sectors',
              d.find(bytes.fromhex('F7 C2 FF 01 00 00'), at, end) >= 0,
              'test edx, 511')
    rep.check('init.capacity_compare',
              any(t == img.at('blk_capacity')
                  for _, t, _ in rip_sites(img, at, end, b'\x48\x3B', 0x05, 0)),
              'cmp rax, [blk_capacity]')
    rep.check('init.cluster_range_low',
              d.find(bytes.fromhex('48 3D F5 0F 00 00'), at, end) >= 0,
              'cmp rax, 4085')
    rep.check('init.cluster_range_high',
              d.find(bytes.fromhex('48 3D F4 FF 00 00'), at, end) >= 0,
              'cmp rax, 65524')
    # task 138 W2: clusters + 2 <= spf * 256, the cached FAT must cover
    # every cluster entry or fs_fat_entry could index past the cache
    rep.equal('init.fat_covers_clusters_load',
              len(rip_refs(img, at, end, b'\x48\x8B', 0x15, 0, 'spf')), 1)
    rep.check('init.fat_covers_clusters',
              d.find(bytes.fromhex(
                  '48 C1 E2 08 48 89 C1 48 83 C1 02 48 39 D1'), at, end) >= 0,
              'shl rdx,8 / mov rcx,rax / add rcx,2 / cmp rcx,rdx')
    cmp_at = d.find(bytes.fromhex('48 39 D1'), at, end)
    rep.check('init.fat_covers_clusters_guard',
              cmp_at >= 0 and d[cmp_at + 3] in (0x77, 0x0F),
              'ja .eio right after the compare')
    rep.check('init.ready_store_last',
              len(rip_refs(img, at, end, b'\x48\xC7', 0x05, 4, 'ready')) == 1)
    # every geometry field is published exactly once
    for name in ('spc', 'reserved', 'nfats', 'spf', 'root_entries',
                 'root_sectors', 'total', 'clusters', 'fat_lba',
                 'root_lba', 'data_lba'):
        stores = []
        for modrm in (0x05, 0x0D, 0x15, 0x1D, 0x25, 0x2D, 0x35, 0x3D):
            stores += rip_refs(img, at, end, b'\x48\x89', modrm, 0, name)
        rep.equal('init.%s_store' % name, len(stores), 1)


def check_selftest(rep, img):
    at, end = region(img, 'selftest', 'st_fail_bpb')
    d = img.data
    bits = [('bpb', ST_BPB), ('ibm', ST_IBM), ('tetris', ST_TETRIS),
            ('seek', ST_SEEK), ('neg', ST_NEG), ('big', ST_BIG),
            ('stat', ST_STAT), ('count', ST_COUNT)]
    for name, bit in bits:
        t_at, t_end = region(img, 'st_fail_%s' % name,
                             'st_fail_%s' % bits[bits.index((name, bit)) + 1][0]
                             if (name, bit) != bits[-1] else 'msg_selftest_mask')
        body = d[t_at:t_end]
        # or r12d, imm8 for bits below 0x80, imm32 for the count bit
        if bit < 0x80:
            want = bytes.fromhex('41 83 CC %02X' % bit) + b'\xE9'
            jmp_at = 5
        else:
            want = b'\x41\x81\xCC' + struct.pack('<I', bit) + b'\xE9'
            jmp_at = 8
        rep.check('selftest.%s_trampoline' % name, body.startswith(want),
                  'or r12d, %#x / jmp' % bit)
        if body.startswith(want):
            rel = struct.unpack_from('<i', body, jmp_at)[0]
            rep.equal('selftest.%s_trampoline_target' % name,
                      t_at + jmp_at + 4 + rel, img.at('st_%s_done' % name))
    # the frozen leg call sequence: the paths are consumed in order
    path_order = ['p_big', 'p_ibm_lower', 'p_tetris', 'p_missing',
                  'p_ibm_x', 'p_ibm', 'p_nope', 'p_roms', 'p_ibm',
                  'p_root', 'p_roms', 'p_programs']
    sites = []
    body = d[at:end]
    # mov rcx, path: fasmg emits the sign extended imm32 form when the
    # address fits (both at ORG 0 and at the kernel base), imm64 otherwise
    for p in find_all(body, b'\x48\xB9'):
        if p + 10 <= len(body):
            sites.append(struct.unpack_from('<Q', body, p + 2)[0])
    for p in find_all(body, b'\x48\xC7\xC1'):
        if p + 7 <= len(body):
            sites.append(struct.unpack_from('<i', body, p + 3)[0])
    consumed = []
    for name in path_order:
        if img[name] in sites:
            consumed.append(name)
    rep.check('selftest.path_constants_used',
              all(img[n] in sites for n in set(path_order)),
              '%d mov rcx, path immediates' % len(sites))
    # leg 7 compares the counter against the register-selected total
    rep.check('selftest.counter_compare',
              d.find(bytes.fromhex('48 39 15'), at, end) >= 0,
              'cmp [fs_test_reads], rdx')
    rep.check('selftest.plain_total',
              d.find(bytes.fromhex('BA 18 00 00 00'), at, end) >= 0,
              'mov edx, 24')
    rep.check('selftest.big_total',
              d.find(bytes.fromhex('BA 30 00 00 00'), at, end) >= 0,
              'mov edx, 48')
    rep.check('selftest.mask_store',
              len(rip_refs(img, at, end, b'\x4C\x89', 0x25, 0, 'test_mask')) == 1)
    rep.check('selftest.big_present_store',
              len(rip_refs(img, at, end, b'\x48\xC7', 0x05, 4,
                           'test_big_present')) == 2)
    # the failure report goes through the CPU console only
    calls = [t for _, t in call_sites(img, at, end)]
    allowed = {img['open'], img['read'], img['seek'], img['close'],
               img['stat'], img['readdir'], img['cpu_com1_puts'],
               img['cpu_com1_puthex']}
    rep.check('selftest.call_whitelist', all(t in allowed for t in calls),
              '%d calls' % len(calls))


def check_module_hygiene(rep, img):
    lo, hi = img.at('code_start'), img.at('code_end')
    internal = {img[n] for n in
                ('sec_read', 'fat_entry', 'cluster_lba', 'norm_comp',
                 'dir_find', 'resolve', 'open', 'read', 'seek', 'close',
                 'stat', 'readdir', 'init', 'selftest')}
    allowed = internal | {img['blk_read'], img['cpu_com1_puts'],
                          img['cpu_com1_puthex']}
    calls = call_sites(img, lo, hi)
    stray = [(s, t) for s, t in calls if t not in allowed]
    rep.check('hygiene.call_targets', not stray,
              '%d stray call targets' % len(stray))
    rep.check('hygiene.calls_exist', len(calls) >= 40,
              '%d E8 sites' % len(calls))
    # every RIP relative write must land inside the fs data region:
    # the module never mutates blk/cpu state
    dstart, dend = img.at('data_start'), img.at('data_end')
    writes = []
    for prefix, modrms, tail in (
            (b'\x48\x89', (0x05, 0x0D, 0x15, 0x1D, 0x25, 0x2D, 0x35, 0x3D), 0),
            (b'\x4C\x89', (0x05, 0x0D, 0x15, 0x1D, 0x25, 0x2D, 0x35, 0x3D), 0),
            (b'\x48\xC7', (0x05,), 4),
            (b'\x48\xFF', (0x05,), 0),
            (b'\xC6\x05', None, 1),
            (b'\x66\xC7', (0x05,), 2),
            (b'\xC7\x05', None, 4)):
        if modrms is None:
            body = img.data[lo:hi]
            for p in find_all(body, prefix):
                m = p + len(prefix) - 1
                if m + 5 + tail <= len(body):
                    disp = struct.unpack_from('<i', body, m + 1)[0]
                    writes.append(lo + m + 5 + tail + disp)
        else:
            for modrm in modrms:
                writes += [t for _, t, _ in
                           rip_sites(img, lo, hi, prefix, modrm, tail)]
    stray_w = [t for t in writes if not dstart <= t < dend]
    rep.check('hygiene.writes_stay_in_fs_data', not stray_w,
              '%d stray write targets: %s' %
              (len(stray_w), ['%#x' % t for t in stray_w[:4]]))
    rep.check('hygiene.writes_exist', len(writes) >= 20,
              '%d RIP write sites' % len(writes))
    # exactly three counter increment sites: sec_read miss + init twice
    incs = []
    for modrm in (0x05, 0x0D, 0x15, 0x1D, 0x25, 0x2D, 0x35, 0x3D):
        incs += rip_refs(img, lo, hi, b'\x48\xFF', modrm, 0, 'test_reads')
    rep.equal('hygiene.counter_increment_sites', len(incs), 3)# ----------------------------------------------------------------------
# KernelFsMirror: a call for call mirror of kernel/fs/fs.inc
# ----------------------------------------------------------------------


class KernelFsMirror:
    """Reproduces the kernel algorithm including its IO discipline.

    Every method returns (status, result) with the signed K64 codes,
    never raises: the negative classes are judged by status exactly
    like the guest judges them. The single sector direct mapped cache
    and the init-time whole-FAT cache are reproduced literally, so
    self.reads / self.blk_reads / self.misses are the deterministic
    evidence the frozen counters are compared against. blk_reads
    starts at 7: the frozen blk self test ran before fs_init.
    """

    def __init__(self, data, capacity=None, blk_ready=1, blk_reads=7):
        self.data = bytes(data)
        self.capacity = (len(self.data) // SECTOR_SIZE
                         if capacity is None else capacity)
        self.blk_ready = blk_ready
        self.reads = 0
        self.blk_reads = blk_reads
        self.misses = []
        self.tag = None
        self.sec = None
        self.ready = 0
        self.g = {}
        self.fat = b''
        self.fds = [None] * MAX_FDS
        self.ent = bytes(ENTRY_SIZE)
        self.bufs = {'ibm': bytearray(512), 'tetris': bytearray(512),
                     'big': bytearray(9216), 'scratch': bytearray(512),
                     'dirents': bytearray(96), 'stat': bytearray(STAT_SIZE)}

    # ---- the blk seam -------------------------------------------------

    def blk_read(self, lba, n):
        if not self.blk_ready:
            return ENOTREADY, None
        if lba + n > self.capacity:
            return EOVERFLOW, None
        self.blk_reads += (n + 7) // 8      # BLK_DMA_SECTORS chunks
        off = lba * SECTOR_SIZE
        return OK, self.data[off:off + n * SECTOR_SIZE]

    def sec_read(self, lba):
        if self.tag == lba:
            return OK
        st, blob = self.blk_read(lba, 1)
        if st:
            return st
        self.reads += 1
        self.misses.append(lba)
        self.tag = lba
        self.sec = blob
        return OK

    # ---- fs_init ------------------------------------------------------

    def init(self):
        if not self.blk_ready:
            return ENOTREADY
        st, bpb = self.blk_read(0, 1)
        if st:
            return EIO
        self.reads += 1
        if struct.unpack_from('<H', bpb, BPB_SIG)[0] != BPB_SIG_VALUE:
            return EIO
        if struct.unpack_from('<H', bpb, BPB_BPS)[0] != SECTOR_SIZE:
            return EIO
        spc = bpb[BPB_SPC]
        if spc == 0 or spc > 128 or spc & (spc - 1):
            return EIO
        reserved = struct.unpack_from('<H', bpb, BPB_RESERVED)[0]
        if reserved == 0:
            return EIO
        nfats = bpb[BPB_NFATS]
        if nfats == 0:
            return EIO
        root_entries = struct.unpack_from('<H', bpb, BPB_ROOT_ENTRIES)[0]
        if root_entries == 0 or (root_entries * ENTRY_SIZE) % SECTOR_SIZE:
            return EIO
        spf = struct.unpack_from('<H', bpb, BPB_SPF)[0]
        if spf == 0 or spf > FAT_CACHE_SECTORS:
            return EIO
        total = struct.unpack_from('<H', bpb, BPB_TOTAL16)[0]
        if total == 0:
            total = u32(bpb, BPB_TOTAL32)
        if total == 0 or total > self.capacity:
            return EIO
        root_sectors = root_entries * ENTRY_SIZE // SECTOR_SIZE
        fat_lba = reserved
        root_lba = reserved + nfats * spf
        data_lba = root_lba + root_sectors
        if data_lba >= total:
            return EIO
        clusters = (total - data_lba) // spc
        if not FAT16_MIN_CLUSTERS <= clusters <= FAT16_MAX_CLUSTERS:
            return EIO
        # task 138 W2: the cached FAT must cover every cluster entry,
        # clusters + 2 <= spf * 256, exactly the new fs_init guard
        if clusters + 2 > spf * (SECTOR_SIZE // 2):
            return EIO
        self.g = dict(spc=spc, reserved=reserved, nfats=nfats, spf=spf,
                      root_entries=root_entries, root_sectors=root_sectors,
                      total=total, clusters=clusters, fat_lba=fat_lba,
                      root_lba=root_lba, data_lba=data_lba)
        st, fat = self.blk_read(fat_lba, spf)
        if st:
            return EIO
        self.reads += 1
        self.fat = fat
        self.ready = 1
        return OK

    # ---- the FAT leaves -----------------------------------------------

    def fat_entry(self, c):
        if c < 2 or c >= self.g['clusters'] + 2:
            return EIO, None
        return OK, struct.unpack_from('<H', self.fat, 2 * c)[0]

    def cluster_lba(self, c):
        return self.g['data_lba'] + (c - 2) * self.g['spc']

    def chain_guard(self, nxt):
        """The shared advance guard: EOC/BAD/reserved/range are all EIO
        on the read path (dir_find maps EOC to ENOENT instead)."""
        return (nxt >= FAT16_EOC_MIN or nxt == FAT16_BAD or nxt < 2 or
                nxt >= self.g['clusters'] + 2)

    # ---- names and directories ----------------------------------------

    def norm(self, comp):
        if comp == b'.':
            return OK, b'.          '
        if comp == b'..':
            return OK, b'..         '
        n = len(comp)
        if n == 0 or n > COMP_MAX:
            return EINVAL, None
        dot = comp.find(b'.')
        if dot >= 0 and comp.find(b'.', dot + 1) >= 0:
            return EINVAL, None
        base = comp[:dot] if dot >= 0 else comp
        ext = comp[dot + 1:] if dot >= 0 else b''
        if len(base) == 0 or len(base) > 8 or len(ext) > 3:
            return EINVAL, None
        name = (base + b' ' * 8)[:8] + (ext + b' ' * 3)[:3]
        return OK, bytes(b - 32 if 0x61 <= b <= 0x7A else b for b in name)

    def _scan_sector(self, name):
        """'end', 'found' or None, over the cached sector."""
        for k in range(ENTRIES_PER_SECTOR):
            e = self.sec[k * ENTRY_SIZE:(k + 1) * ENTRY_SIZE]
            first = e[0]
            if first == DIR_END:
                return 'end'
            if first == DIR_DELETED:
                continue
            attr = e[11]
            if attr & ATTR_VOLUME_ID:
                continue
            if attr & ATTR_LFN == ATTR_LFN:
                continue
            for i in range(11):
                b = e[i]
                if i == 0 and b == DIR_KANJI:
                    b = DIR_DELETED
                if b != name[i]:
                    break
            else:
                self.ent = e
                return 'found'
        return None

    def dir_find(self, cluster, name):
        if cluster == 0:
            for k in range(self.g['root_sectors']):
                st = self.sec_read(self.g['root_lba'] + k)
                if st:
                    return st
                r = self._scan_sector(name)
                if r == 'end':
                    return ENOENT
                if r == 'found':
                    return OK
            return ENOENT
        budget = self.g['clusters']
        while True:
            if cluster < 2 or cluster >= self.g['clusters'] + 2:
                return EIO
            lba = self.cluster_lba(cluster)
            for k in range(self.g['spc']):
                st = self.sec_read(lba + k)
                if st:
                    return st
                r = self._scan_sector(name)
                if r == 'end':
                    return ENOENT
                if r == 'found':
                    return OK
            budget -= 1
            if budget == 0:
                return EIO              # hop budget spent: chain loop
            st, nxt = self.fat_entry(cluster)
            if st:
                return st
            if nxt >= FAT16_EOC_MIN:
                return ENOENT
            if nxt == FAT16_BAD or nxt < 2 or nxt >= self.g['clusters'] + 2:
                return EIO
            cluster = nxt

    def resolve(self, path):
        if path is None:
            return EFAULT
        if isinstance(path, str):
            path = path.encode('ascii')
        if len(path) > PATH_MAX:
            return EINVAL
        cur, matched, i = 0, False, 0
        while i < len(path):
            if path[i] == 0x2F:
                i += 1
                continue
            j = i
            while j < len(path) and path[j] != 0x2F:
                j += 1
                if j - i > COMP_MAX:
                    return EINVAL
            st, name = self.norm(path[i:j])
            if st:
                return st
            st = self.dir_find(cur, name)
            if st:
                return st
            matched = True
            if j < len(path):
                if not self.ent[11] & ATTR_DIRECTORY:
                    return EPERM
                cur = struct.unpack_from('<H', self.ent, 26)[0]
            i = j
        if not matched:
            self.ent = (bytes(11) + bytes((ATTR_DIRECTORY,)) + bytes(20))
        return OK

    # ---- the file API ---------------------------------------------------

    def open(self, path, flags):
        if not self.ready:
            return ENOTREADY, None
        if path is None:
            return EFAULT, None
        if flags & O_ACCMODE:
            return EPERM, None
        st = self.resolve(path)
        if st:
            return st, None
        for slot in range(MAX_FDS):
            if self.fds[slot] is None:
                self.fds[slot] = {
                    'dir': bool(self.ent[11] & ATTR_DIRECTORY),
                    'cluster': struct.unpack_from('<H', self.ent, 26)[0],
                    'size': u32(self.ent, 28), 'pos': 0}
                return OK, slot
        return ENOMEM, None

    def _fd(self, fd):
        if fd >= MAX_FDS or self.fds[fd] is None:
            return None
        return self.fds[fd]

    def read(self, fd, buf, length):
        f = self._fd(fd)
        if f is None:
            return EINVAL, 0
        if f['dir']:
            return EPERM, 0
        if length < 0:
            return EINVAL, 0
        if length == 0:
            return OK, 0
        if buf is None:
            return EFAULT, 0
        pos, size = f['pos'], f['size']
        if pos >= size:
            return OK, 0
        n = min(length, size - pos)
        total = n
        cb = self.g['spc'] * SECTOR_SIZE
        cluster, base = f['cluster'], 0
        if cluster < 2 or cluster >= self.g['clusters'] + 2:
            return EIO, 0
        while pos >= base + cb:
            st, nxt = self.fat_entry(cluster)
            if st:
                return st, 0
            if self.chain_guard(nxt):
                return EIO, 0
            cluster, base = nxt, base + cb
        base_lba = self.cluster_lba(cluster)
        out = bytearray()
        while n > 0:
            off = pos - base
            st = self.sec_read(base_lba + (off >> SECTOR_SHIFT))
            if st:
                return st, 0
            soff = off & (SECTOR_SIZE - 1)
            chunk = min(n, SECTOR_SIZE - soff, cb - off)
            out += self.sec[soff:soff + chunk]
            pos += chunk
            n -= chunk
            if n == 0:
                break
            if pos - base >= cb:
                st, nxt = self.fat_entry(cluster)
                if st:
                    return st, 0
                if self.chain_guard(nxt):
                    return EIO, 0
                cluster = nxt
                base = pos
                base_lba = self.cluster_lba(cluster)
        f['pos'] = pos
        self.bufs[buf][:len(out)] = out
        return OK, total

    def seek(self, fd, off, whence):
        f = self._fd(fd)
        if f is None:
            return EINVAL, 0
        if f['dir']:
            return EINVAL, 0
        if whence > SEEK_END:
            return EINVAL, 0
        new = off
        if whence == SEEK_CUR:
            new += f['pos']
        elif whence == SEEK_END:
            new += f['size']
        if new < 0:
            return EINVAL, 0
        f['pos'] = new
        return OK, new

    def close(self, fd):
        if self._fd(fd) is None:
            return EINVAL, 0
        self.fds[fd] = None
        return OK, 0

    def stat(self, path, buf):
        if not self.ready:
            return ENOTREADY, 0
        if path is None or buf is None:
            return EFAULT, 0
        st = self.resolve(path)
        if st:
            return st, 0
        out = bytearray(STAT_SIZE)
        struct.pack_into('<Q', out, 0, u32(self.ent, 28))
        struct.pack_into('<Q', out, 8, self.ent[11])
        self.bufs[buf] = out
        return OK, 0

    def readdir(self, fd, buf, length):
        f = self._fd(fd)
        if f is None:
            return EINVAL, 0
        if not f['dir']:
            return EPERM, 0
        if length < DIRENT_SIZE:
            return EINVAL, 0
        if buf is None:
            return EFAULT, 0
        cap = (length // DIRENT_SIZE) * DIRENT_SIZE
        written, pos = 0, f['pos']
        cluster, base = f['cluster'], 0
        out = bytearray()
        ended = False
        while written < cap and pos != -1 and not ended:
            if cluster != 0:
                spc16 = self.g['spc'] * ENTRIES_PER_SECTOR
                while pos >= base + spc16:
                    st, nxt = self.fat_entry(cluster)
                    if st:
                        return st, 0
                    if nxt >= FAT16_EOC_MIN:
                        ended = True
                        break
                    if (nxt == FAT16_BAD or nxt < 2 or
                            nxt >= self.g['clusters'] + 2):
                        return EIO, 0
                    cluster, base = nxt, base + spc16
                if ended:
                    break
                if cluster < 2 or cluster >= self.g['clusters'] + 2:
                    return EIO, 0
                lba = self.cluster_lba(cluster) + ((pos - base) >> 4)
            else:
                if pos >= self.g['root_entries']:
                    ended = True
                    break
                lba = self.g['root_lba'] + (pos >> 4)
            st = self.sec_read(lba)
            if st:
                return st, 0
            e = self.sec[(pos & 15) * ENTRY_SIZE:
                         (pos & 15) * ENTRY_SIZE + ENTRY_SIZE]
            first = e[0]
            if first == DIR_END:
                ended = True
                break
            skip = (first == DIR_DELETED or e[11] & ATTR_VOLUME_ID or
                    e[11] & ATTR_LFN == ATTR_LFN or
                    (first == 0x2E and e[1] in (0x20, 0x2E)))
            if not skip:
                name = bytes(DIR_DELETED if i == 0 and e[0] == DIR_KANJI
                             else e[i] for i in range(11))
                out += struct.pack('<QQ', e[11], u32(e, 28))
                out += name + bytes(5)
                written += DIRENT_SIZE
            pos += 1
        f['pos'] = -1 if ended else pos
        self.bufs[buf][:written] = out
        return OK, written


# ----------------------------------------------------------------------
# the frozen self test leg sequence, replayed through the mirror
# ----------------------------------------------------------------------


def replay_selftest(m):
    """The exact call order of kernel/fs/selftest.inc. Returns the
    evidence dict; every leg failure ORs its bit exactly like the
    guest trampolines, so one run reports every broken leg."""
    ev = {'mask': 0}

    def fail(bit):
        ev['mask'] |= bit

    st = m.init()
    if st:
        ev['mask'] = ST_BPB
        ev['init_status'] = st
        return ev
    ev['init_status'] = OK

    # leg 0: geometry + BIG.BIN probe
    def leg0():
        want = dict(spc=TEST_SPC, reserved=TEST_RESERVED, nfats=TEST_NFATS,
                    spf=TEST_SPF, root_entries=TEST_ROOT_ENTRIES,
                    clusters=TEST_CLUSTERS, total=TEST_TOTAL,
                    fat_lba=TEST_FAT_LBA, root_lba=TEST_ROOT_LBA,
                    data_lba=TEST_DATA_LBA)
        if m.ready != 1 or any(m.g[k] != v for k, v in want.items()):
            return False
        st, _ = m.stat('/PROGRAMS/BIG.BIN', 'stat')
        if st == OK:
            if u64(m.bufs['stat'], 0) != TEST_BIG_SIZE:
                return False
            ev['big_present'] = 1
        elif st == ENOENT:
            ev['big_present'] = 0
        else:
            return False
        return True
    if not leg0():
        fail(ST_BPB)
        ev.setdefault('big_present', 0)

    # leg 1: lowercase /roms/ibm.ch8
    ibm_fd = None

    def leg1():
        nonlocal ibm_fd
        st, fd = m.open('/roms/ibm.ch8', O_RDONLY)
        if st or fd != 0:
            return False
        ibm_fd = fd
        st, n = m.read(fd, 'ibm', 512)
        if st or n != TEST_IBM_SIZE:
            return False
        if u64(m.bufs['ibm'], 0) != TEST_ROM_PREFIX:
            return False
        st, n = m.read(fd, 'scratch', 16)
        return st == OK and n == 0
    if not leg1():
        fail(ST_IBM)
    ev['ibm'] = bytes(m.bufs['ibm'][:TEST_IBM_SIZE])

    # leg 2: /ROMS/TETRIS1.CH8
    def leg2():
        st, fd = m.open('/ROMS/TETRIS1.CH8', O_RDONLY)
        if st:
            return False
        st, n = m.read(fd, 'tetris', 512)
        if st or n != TEST_TETRIS_SIZE:
            return False
        st, n = m.read(fd, 'scratch', 16)
        if st or n != 0:
            return False
        st, _ = m.close(fd)
        return st == OK
    if not leg2():
        fail(ST_TETRIS)
    ev['tetris'] = bytes(m.bufs['tetris'][:TEST_TETRIS_SIZE])

    # leg 3: seek/read boundaries on IBM.CH8
    def leg3():
        if ibm_fd is None:
            return False
        st, off = m.seek(ibm_fd, 60, SEEK_SET)
        if st or off != 60:
            return False
        st, n = m.read(ibm_fd, 'scratch', 60)
        if st or n != 60:
            return False
        if m.bufs['scratch'][:60] != m.bufs['ibm'][60:120]:
            return False
        st, _ = m.seek(ibm_fd, -1, SEEK_SET)
        if st != EINVAL:
            return False
        st, off = m.seek(ibm_fd, 0, SEEK_END)
        if st or off != TEST_IBM_SIZE:
            return False
        st, n = m.read(ibm_fd, 'scratch', 16)
        if st or n != 0:
            return False
        st, off = m.seek(ibm_fd, -1, SEEK_CUR)
        if st or off != TEST_IBM_SIZE - 1:
            return False
        st, n = m.read(ibm_fd, 'scratch', 100)
        if st or n != 1:
            return False
        if m.bufs['scratch'][0] != m.bufs['ibm'][TEST_IBM_SIZE - 1]:
            return False
        st, off = m.seek(ibm_fd, 0, SEEK_SET)
        if st or off != 0:
            return False
        st, _ = m.close(ibm_fd)
        return st == OK
    if not leg3():
        fail(ST_SEEK)

    # leg 4: refusal paths + readdir evidence
    def leg4():
        st, _ = m.open('/ROMS/MISSING.CH8', O_RDONLY)
        if st != ENOENT:
            return False
        st, _ = m.open('/ROMS/IBM.CH8/X', O_RDONLY)
        if st != EPERM:
            return False
        st, _ = m.open('/ROMS/IBM.CH8', O_WRONLY)
        if st != EPERM:
            return False
        for st, _ in (m.read(MAX_FDS - 1, 'scratch', 16),
                      m.seek(MAX_FDS - 1, 0, SEEK_SET),
                      m.close(MAX_FDS - 1),
                      m.readdir(MAX_FDS - 1, 'dirents', 96)):
            if st != EINVAL:
                return False
        st, _ = m.stat('/NOPE', 'stat')
        if st != ENOENT:
            return False
        st, fd = m.open('/ROMS', O_RDONLY)
        if st:
            return False
        if m.read(fd, 'scratch', 16)[0] != EPERM:
            return False
        if m.seek(fd, 0, SEEK_SET)[0] != EINVAL:
            return False
        if m.close(fd)[0]:
            return False
        st, fd = m.open('/ROMS/IBM.CH8', O_RDONLY)
        if st:
            return False
        if m.readdir(fd, 'dirents', 96)[0] != EPERM:
            return False
        if m.close(fd)[0]:
            return False
        st, fd = m.open('/', O_RDONLY)
        if st:
            return False
        st, n = m.readdir(fd, 'dirents', 96)
        if st or n != 2 * DIRENT_SIZE:
            return False
        d = m.bufs['dirents']
        if not (u32(d, 0) & ATTR_DIRECTORY) or u64(d, 8) != 0:
            return False
        if not (u32(d, 32) & ATTR_DIRECTORY) or u64(d, 40) != 0:
            return False
        if d[16:27] != NAMES['n_roms'] or d[48:59] != NAMES['n_programs']:
            return False
        ev['root_dirents'] = bytes(d[:64])
        if m.readdir(fd, 'dirents', 96) != (OK, 0):
            return False
        if m.close(fd)[0]:
            return False
        st, fd = m.open('/ROMS', O_RDONLY)
        if st:
            return False
        st, n = m.readdir(fd, 'dirents', 96)
        if st or n != 2 * DIRENT_SIZE:
            return False
        d = m.bufs['dirents']
        if u64(d, 8) != TEST_IBM_SIZE or u64(d, 40) != TEST_TETRIS_SIZE:
            return False
        if d[16:27] != NAMES['n_ibm'] or d[48:59] != NAMES['n_tetris']:
            return False
        ev['roms_dirents'] = bytes(d[:64])
        if m.readdir(fd, 'dirents', 96) != (OK, 0):
            return False
        if m.close(fd)[0]:
            return False
        st, fd = m.open('/PROGRAMS', O_RDONLY)
        if st:
            return False
        st, n = m.readdir(fd, 'dirents', 96)
        if st:
            return False
        if ev['big_present']:
            if n != DIRENT_SIZE:
                return False
            d = m.bufs['dirents']
            if u64(d, 8) != TEST_BIG_SIZE or d[16:27] != NAMES['n_big']:
                return False
        elif n != 0:
            return False
        ev['prog_dirents'] = bytes(m.bufs['dirents'][:n])
        if m.readdir(fd, 'dirents', 96) != (OK, 0):
            return False
        if m.close(fd)[0]:
            return False
        return True
    if not leg4():
        fail(ST_NEG)

    # leg 5: BIG.BIN cross cluster spans, only on the big image
    def leg5():
        if not ev['big_present']:
            return True
        st, fd = m.open('/PROGRAMS/BIG.BIN', O_RDONLY)
        if st:
            return False
        st, n = m.read(fd, 'big', 9216)
        if st or n != TEST_BIG_SIZE:
            return False
        if u64(m.bufs['big'], 0) != TEST_BIG_PREFIX:
            return False
        if m.bufs['big'][TEST_BIG_SIZE - 1] != 0xFF:
            return False
        if m.read(fd, 'scratch', 16) != (OK, 0):
            return False
        st, off = m.seek(fd, 1021, SEEK_SET)
        if st or off != 1021:
            return False
        st, n = m.read(fd, 'scratch', 6)
        if st or n != 6:
            return False
        if m.bufs['scratch'][:6] != m.bufs['big'][1021:1027]:
            return False
        if m.seek(fd, 2047, SEEK_SET)[0]:
            return False
        st, n = m.read(fd, 'scratch', 2)
        if st or n != 2:
            return False
        if m.bufs['scratch'][:2] != m.bufs['big'][2047:2049]:
            return False
        if m.seek(fd, TEST_BIG_SIZE - 5, SEEK_SET)[0]:
            return False
        st, n = m.read(fd, 'scratch', 100)
        if st or n != 5:
            return False
        if m.bufs['scratch'][:5] != m.bufs['big'][TEST_BIG_SIZE - 5:TEST_BIG_SIZE]:
            return False
        if m.seek(fd, TEST_BIG_SIZE, SEEK_SET)[0]:
            return False
        if m.read(fd, 'scratch', 16) != (OK, 0):
            return False
        st, _ = m.close(fd)
        return st == OK
    if not leg5():
        fail(ST_BIG)
    if ev['big_present']:
        ev['big'] = bytes(m.bufs['big'][:TEST_BIG_SIZE])

    # leg 6: stat size/attr/reserved zeros
    def leg6():
        st, _ = m.stat('/ROMS/IBM.CH8', 'stat')
        if st:
            return False
        s = m.bufs['stat']
        if u64(s, 0) != TEST_IBM_SIZE:
            return False
        attr = u32(s, 8)
        if not attr & ATTR_READ_ONLY or attr & ATTR_DIRECTORY:
            return False
        ev['stat_ibm'] = bytes(s)
        st, _ = m.stat('/ROMS', 'stat')
        if st:
            return False
        s = m.bufs['stat']
        if not u32(s, 8) & ATTR_DIRECTORY or u64(s, 0) != 0:
            return False
        st, _ = m.stat('/', 'stat')
        if st:
            return False
        s = m.bufs['stat']
        if not u32(s, 8) & ATTR_DIRECTORY or u64(s, 0) != 0:
            return False
        return s[12:STAT_SIZE] == bytes(STAT_SIZE - 12)
    if not leg6():
        fail(ST_STAT)

    # leg 7: the sector fetch counter
    want = TEST_READS_FINAL_BIG if ev['big_present'] else TEST_READS_FINAL
    if m.reads != want:
        fail(ST_COUNT)

    ev['reads'] = m.reads
    ev['blk_reads'] = m.blk_reads
    ev['misses'] = list(m.misses)
    return ev


# ----------------------------------------------------------------------
# model cross validation and the negative classes
# ----------------------------------------------------------------------


def load_model(root):
    path = root / 'tests' / 'stage2-fs' / 'fat16_model.py'
    spec = importlib.util.spec_from_file_location('fat16_model', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def dirent_rows(blob):
    """(attr, size, name11) triples of a readdir evidence buffer."""
    return [(u32(blob, k), u64(blob, k + 8), blob[k + 16:k + 27])
            for k in range(0, len(blob), DIRENT_SIZE)]


def oracle_rows(entries):
    out = []
    for e in entries:
        base, _, ext = e.name.partition('.')
        name = (base.encode() + b' ' * 8)[:8] + (ext.encode() + b' ' * 3)[:3]
        out.append((e.attr, e.size, name))
    return out


def check_model(rep, root, img, out):
    import hashlib
    model = load_model(root)
    disk_path = root / 'build' / 'fs-v1' / 'disk.img'
    big_path = root / 'build' / 'fs-v1' / 'disk-big.img'
    if not rep.check('model.disk_image_present', disk_path.is_file(),
                     str(disk_path)):
        return
    if not rep.check('model.big_image_present', big_path.is_file(),
                     str(big_path)):
        return
    disk = disk_path.read_bytes()
    big = big_path.read_bytes()
    rep.equal('model.disk_image_sha', hashlib.sha256(disk).hexdigest(),
              IMAGE_SHA)
    rep.equal('model.big_image_sha', hashlib.sha256(big).hexdigest(),
              BIG_IMAGE_SHA)

    # the 15 independent oracle self tests, on the frozen images
    model.BIMG = disk
    model.BIMG_BIG = big
    model.MINI = model.mini_image()
    lines = []
    for name, fn in model.TESTS:
        try:
            fn()
            ok, detail = True, ''
        except Exception as e:                      # noqa: BLE001
            ok, detail = False, '%s: %s' % (type(e).__name__, e)
        rep.check('model.%s' % name, ok, detail)
        lines.append('%-4s %s%s' % ('ok' if ok else 'FAIL', name,
                                    (': ' + detail) if detail else ''))
    (out / 'model-selftest.log').write_text('\n'.join(lines) + '\n')
    rep.equal('model.selftest_count', len(model.TESTS), 15)

    oracle = model.Fat16(disk)
    oracle_big = model.Fat16(big)

    # the plain image replay: mask, counters and every payload byte
    m = KernelFsMirror(disk)
    ev = replay_selftest(m)
    rep.equal('mirror.plain_mask', ev['mask'], 0)
    rep.equal('mirror.plain_reads', ev['reads'], TEST_READS_FINAL)
    rep.equal('mirror.plain_reads_frozen', ev['reads'],
              img['test_reads_final'])
    rep.equal('mirror.plain_blk_reads', ev['blk_reads'], BLK_READS_FINAL)
    rep.equal('mirror.plain_big_present', ev['big_present'], 0)
    rep.check('mirror.plain_ibm_bytes',
              ev.get('ibm') == oracle.read('/ROMS/IBM.CH8'))
    rep.check('mirror.plain_tetris_bytes',
              ev.get('tetris') == oracle.read('/ROMS/TETRIS1.CH8'))
    rep.check('mirror.plain_root_dirents',
              dirent_rows(ev.get('root_dirents', b'')) ==
              oracle_rows(oracle.readdir('/')))
    rep.check('mirror.plain_roms_dirents',
              dirent_rows(ev.get('roms_dirents', b'')) ==
              oracle_rows(oracle.readdir('/ROMS')))
    rep.equal('mirror.plain_prog_dirents', ev.get('prog_dirents'), b'')
    st = oracle.stat('/ROMS/IBM.CH8')
    rep.check('mirror.plain_stat_ibm',
              ev.get('stat_ibm') ==
              struct.pack('<QQ', st['size'], st['attr']) + bytes(48))
    # the miss trace is frozen too: it is the cache discipline evidence
    plain_misses = ([65, 99] + [65, 97, 101] + [65, 97, 103] + [101] +
                    [65, 97, 65, 97, 65, 97, 65, 97, 65, 99] +
                    [65, 97, 65])
    rep.equal('mirror.plain_miss_trace', ev['misses'], plain_misses)

    # the big image replay: the cross cluster leg runs in full
    mb = KernelFsMirror(big)
    evb = replay_selftest(mb)
    rep.equal('mirror.big_mask', evb['mask'], 0)
    rep.equal('mirror.big_reads', evb['reads'], TEST_READS_FINAL_BIG)
    rep.equal('mirror.big_reads_frozen', evb['reads'],
              img['test_reads_final_big'])
    rep.equal('mirror.big_blk_reads', evb['blk_reads'], BLK_READS_FINAL_BIG)
    rep.equal('mirror.big_big_present', evb['big_present'], 1)
    rep.check('mirror.big_payload_bytes',
              evb.get('big') == model.BIG_DATA ==
              oracle_big.read('/PROGRAMS/BIG.BIN'))
    rep.check('mirror.big_prog_dirents',
              dirent_rows(evb.get('prog_dirents', b'')) ==
              oracle_rows(oracle_big.readdir('/PROGRAMS')))
    big_misses = (plain_misses[:-3] +
                  [65, 99] + list(range(105, 122)) +
                  [106, 107, 108, 109, 121] + [65, 97, 65])
    rep.equal('mirror.big_miss_trace', evb['misses'], big_misses)

    # case insensitive resolution cross checked against the oracle
    mc = KernelFsMirror(disk)
    mc.init()
    ref = oracle.read('/ROMS/IBM.CH8')
    for path in ('/roms/ibm.ch8', '/Roms/Ibm.Ch8', 'ROMS/IBM.CH8',
                 '/ROMS//IBM.CH8', '/ROMS/IBM.CH8/'):
        if path.endswith('/'):
            rep.equal('mirror.trailing_slash_eperm',
                      mc.open(path, O_RDONLY)[0], EPERM)
            continue
        st, fd = mc.open(path, O_RDONLY)
        ok = st == OK
        if ok:
            st, n = mc.read(fd, 'ibm', 512)
            ok = st == OK and bytes(mc.bufs['ibm'][:n]) == ref
            mc.close(fd)
        rep.check('mirror.case_path_%s' % path, ok)

    # negative API classes on a fresh mount
    mn = KernelFsMirror(disk)
    mn.init()
    neg = [
        ('write_wronly', mn.open('/ROMS/IBM.CH8', O_WRONLY)[0], EPERM),
        ('write_rdwr', mn.open('/ROMS/IBM.CH8', O_RDWR)[0], EPERM),
        ('null_path', mn.open(None, O_RDONLY)[0], EFAULT),
        ('overlong_path', mn.open('/' + 'A' * 128, O_RDONLY)[0], EINVAL),
        ('overlong_comp', mn.open('/ROMS/' + 'A' * 13, O_RDONLY)[0], EINVAL),
        ('double_dot', mn.open('/ROMS/A..B', O_RDONLY)[0], EINVAL),
        ('missing', mn.open('/ROMS/MISSING.CH8', O_RDONLY)[0], ENOENT),
        ('file_as_dir', mn.open('/ROMS/IBM.CH8/X', O_RDONLY)[0], EPERM),
        ('stat_missing', mn.stat('/NOPE', 'stat')[0], ENOENT),
        ('stat_null', mn.stat(None, 'stat')[0], EFAULT),
    ]
    st, fd = mn.open('/ROMS/IBM.CH8', O_RDONLY)
    rep.equal('mirror.neg_open_ok', st, OK)
    neg += [
        ('fd_out_of_range', mn.read(MAX_FDS, 'scratch', 16)[0], EINVAL),
        ('fd_never_opened', mn.read(MAX_FDS - 1, 'scratch', 16)[0], EINVAL),
        ('negative_len', mn.read(fd, 'scratch', -1)[0], EINVAL),
        ('null_buf', mn.read(fd, None, 16)[0], EFAULT),
        ('seek_negative', mn.seek(fd, -1, SEEK_SET)[0], EINVAL),
        ('seek_whence', mn.seek(fd, 0, 3)[0], EINVAL),
        ('file_readdir', mn.readdir(fd, 'dirents', 96)[0], EPERM),
        ('close_twice', mn.close(MAX_FDS - 1)[0], EINVAL),
    ]
    st, dfd = mn.open('/ROMS', O_RDONLY)
    neg.append(('readdir_small_buf', mn.readdir(dfd, 'dirents', 31)[0], EINVAL))
    mn.close(dfd)
    mn.seek(fd, TEST_IBM_SIZE + 100, SEEK_SET)
    neg.append(('read_past_eof', mn.read(fd, 'scratch', 16), (OK, 0)))
    mn.close(fd)
    st, fd = mn.open('/ROMS', O_RDONLY)
    neg += [
        ('dir_read', mn.read(fd, 'scratch', 16)[0], EPERM),
        ('dir_seek', mn.seek(fd, 0, SEEK_SET)[0], EINVAL),
    ]
    mn.close(fd)
    for name, got, want in neg:
        rep.equal('mirror.neg_%s' % name, got, want)
    # an unmounted module refuses opens, it never panics
    mu = KernelFsMirror(disk, blk_ready=0)
    rep.equal('mirror.neg_no_blk_init', mu.init(), ENOTREADY)
    rep.equal('mirror.neg_notready_open',
              mu.open('/ROMS/IBM.CH8', O_RDONLY)[0], ENOTREADY)

    # negative image classes: illegal BPB variants, each an init EIO
    # with fs_ready left 0 (the self test failure path, never a panic)
    def p(img_, off, blob):
        return model.patched(img_, off, blob)
    bpb_neg = [
        ('sig', p(disk, BPB_SIG, b'\x00')),
        ('bps_1024', p(disk, BPB_BPS, struct.pack('<H', 1024))),
        ('spc_3', p(disk, BPB_SPC, b'\x03')),
        ('spc_0', p(disk, BPB_SPC, b'\x00')),
        ('reserved_0', p(disk, BPB_RESERVED, struct.pack('<H', 0))),
        ('nfats_0', p(disk, BPB_NFATS, b'\x00')),
        ('root_entries_0', p(disk, BPB_ROOT_ENTRIES, struct.pack('<H', 0))),
        ('spf_0', p(disk, BPB_SPF, struct.pack('<H', 0))),
        ('spf_33', p(disk, BPB_SPF, struct.pack('<H', 33))),
        # task 138 W2: spf inside the cache cap but too small for the
        # cluster count, the FAT would not cover clusters + 2 entries
        ('spf_clusters_mismatch', p(disk, BPB_SPF, struct.pack('<H', 16))),
        ('total_small', p(disk, BPB_TOTAL16, struct.pack('<H', 2000))),
        ('total_zero', p(p(disk, BPB_TOTAL16, struct.pack('<H', 0)),
                         BPB_TOTAL32, struct.pack('<I', 0))),
        ('total_over_capacity',
         p(p(disk, BPB_TOTAL16, struct.pack('<H', 0)),
           BPB_TOTAL32, struct.pack('<I', 20000))),
    ]
    for name, bad in bpb_neg:
        mm = KernelFsMirror(bad)
        st = mm.init()
        rep.check('mirror.neg_bpb_%s' % name,
                  st == EIO and mm.ready == 0, 'init=%d ready=%d' %
                  (st, mm.ready))

    # chain corruption classes. The ROMS directory cluster is filled
    # with deleted slots so the scan must walk the chain and hit the
    # corrupted FAT[2]; the hop budget bounds the loop leg.
    fat2_off = TEST_FAT_LBA * SECTOR_SIZE + 2 * 2
    roms_off = TEST_DATA_LBA * SECTOR_SIZE
    wiped = p(disk, roms_off, b'\xE5' * (TEST_SPC * SECTOR_SIZE))
    chain_neg = [
        ('loop', p(wiped, fat2_off, struct.pack('<H', 2))),
        ('out_of_range', p(wiped, fat2_off, struct.pack('<H', 9000))),
        ('bad_marker', p(wiped, fat2_off, struct.pack('<H', FAT16_BAD))),
        ('reserved_value', p(wiped, fat2_off, struct.pack('<H', 1))),
    ]
    for name, bad in chain_neg:
        mm = KernelFsMirror(bad)
        mm.init()
        st, _ = mm.open('/ROMS/IBM.CH8', O_RDONLY)
        rep.equal('mirror.neg_chain_%s' % name, st, EIO)
    # an out of range first cluster in a root entry
    root_roms_cluster = TEST_ROOT_LBA * SECTOR_SIZE + ENTRY_SIZE + 26
    mm = KernelFsMirror(p(disk, root_roms_cluster, struct.pack('<H', 60000)))
    mm.init()
    rep.equal('mirror.neg_root_cluster_range',
              mm.open('/ROMS/IBM.CH8', O_RDONLY)[0], EIO)
    # a chain shorter than the directory size: read must be EIO, never
    # a short read (IBM.CH8 lives in ROMS slot 2, cluster 4)
    ibm_size_off = roms_off + 2 * ENTRY_SIZE + 28
    mm = KernelFsMirror(p(disk, ibm_size_off, struct.pack('<I', 2000)))
    mm.init()
    st, fd = mm.open('/ROMS/IBM.CH8', O_RDONLY)
    rep.equal('mirror.neg_short_chain_open', st, OK)
    rep.equal('mirror.neg_short_chain_read', mm.read(fd, 'big', 4096)[0], EIO)

    # the hand built mini image: spc=1 geometry through the same mirror
    mini = model.MINI
    mm = KernelFsMirror(mini)
    rep.equal('mirror.mini_init', mm.init(), OK)
    rep.equal('mirror.mini_spc', mm.g['spc'], 1)
    st, fd = mm.open('/A.TXT', O_RDONLY)
    rep.equal('mirror.mini_open', st, OK)
    st, n = mm.read(fd, 'big', 4096)
    rep.check('mirror.mini_cross_cluster',
              st == OK and n == 1300 and
              bytes(mm.bufs['big'][:n]) == model.MINI_A_DATA)
    mm.close(fd)
    for off, ln in ((510, 4), (1022, 6), (1299, 10)):
        st, fd = mm.open('/a.txt', O_RDONLY)
        mm.seek(fd, off, SEEK_SET)
        st, n = mm.read(fd, 'scratch', ln)
        rep.check('mirror.mini_boundary_%d' % off,
                  st == OK and bytes(mm.bufs['scratch'][:n]) ==
                  model.MINI_A_DATA[off:off + ln])
        mm.close(fd)
    st, fd = mm.open('/', O_RDONLY)
    st, n = mm.readdir(fd, 'dirents', 96)
    rep.check('mirror.mini_root_readdir',
              st == OK and dirent_rows(bytes(mm.bufs['dirents'][:n])) ==
              oracle_rows(model.Fat16(mini).readdir('/')))
    mm.close(fd)
    st, fd = mm.open('/EMPTY', O_RDONLY)
    rep.equal('mirror.mini_empty_dir', mm.readdir(fd, 'dirents', 96), (OK, 0))
    mm.close(fd)
    st, fd = mm.open('/sub/b.bin', O_RDONLY)
    st, n = mm.read(fd, 'scratch', 512)
    rep.check('mirror.mini_subdir_file',
              st == OK and bytes(mm.bufs['scratch'][:n]) == model.MINI_B_DATA)
    mm.close(fd)
    # any EOC marker >= 0xFFF8 terminates the chain
    eoc_img = p(mini, 512 + 2 * 4, struct.pack('<H', 0xFFF8))
    mm = KernelFsMirror(eoc_img)
    mm.init()
    st, fd = mm.open('/A.TXT', O_RDONLY)
    st, n = mm.read(fd, 'big', 4096)
    rep.check('mirror.mini_eoc_variant',
              st == OK and bytes(mm.bufs['big'][:n]) == model.MINI_A_DATA)# ----------------------------------------------------------------------
# source hygiene and the closing files
# ----------------------------------------------------------------------


def check_sources(rep, root):
    asm_text = (root / 'kernel' / 'fs' / 'fs.asm').read_text(encoding='utf-8')
    rep.check('source.fs_asm_hosted_guard',
              'if ~ defined FS_HOSTED' in asm_text and
              'format binary' in asm_text)
    order = re.findall(r"^include\s+'([^']+)'", asm_text, re.M)
    rep.equal('source.include_order', order,
              ['api.inc', 'fs.inc', 'selftest.inc', 'data.inc'])
    rep.check('source.fs_asm_end_assertion',
              'if $ <> fs_data_end' in asm_text)

    api = (root / 'kernel' / 'fs' / 'api.inc').read_text(encoding='utf-8')
    rep.check('source.api_private_assertions',
              len(re.findall(r'^\s*err\b', api, re.M)) >= 10,
              '%d err sites' % len(re.findall(r'^\s*err\b', api, re.M)))

    # the kernel include order: fs sits between blk and fsys, the M3
    # file-syscall module that consumes the fs API; contracts closes the
    # module chain right after fsys
    k64 = (root / 'kernel' / 'kernel64.asm').read_text(encoding='utf-8')
    incs = re.findall(r"^include\s+'([^']+)'", k64, re.M)
    ok = ('fs/fs.asm' in incs and
          incs.index('fs/fs.asm') == incs.index('blk/blk.asm') + 1 and
          incs.index('fsys/fsys.asm') == incs.index('fs/fs.asm') + 1 and
          incs.index('core/contracts.inc') == incs.index('fsys/fsys.asm') + 1)
    rep.check('source.kernel64_include_position', ok, repr(incs[-6:]))
    rep.check('source.kernel64_fs_hosted',
              re.search(r"^FS_HOSTED = 1\ninclude 'fs/fs\.asm'", k64, re.M)
              is not None)
    rep.check('source.kernel64_core_closes', 'if $ <> core_data_end' in k64)

    # contracts: the FS segment is a pure tail append after the BLK one
    contracts = (root / 'kernel' / 'core' / 'contracts.inc').read_text(
        encoding='utf-8')
    rep.check('source.contracts_fs_after_blk',
              contracts.find('K64_FS_') > contracts.find('K64_BLK_') >= 0)
    rep.check('source.contracts_fs_compares',
              'K64_FS_SECTOR_SIZE <> FS_SECTOR_SIZE' in contracts and
              'K64_FS_SECTOR_SIZE <> K64_BLK_SECTOR_SIZE' in contracts)
    rep.check('source.contracts_fs_blk_geometry',
              'K64_BLK_TEST_DATA_LBA <> K64_FS_TEST_DATA_LBA + '
              '2 * K64_FS_TEST_SPC' in contracts)
    rep.check('source.contracts_leg7_refrozen',
              contracts.rfind('K64_CORE_ACCOUNT_TABLES <> 3') >
              contracts.find('K64_FS_'))

    # foundation: the fs stage runs after blk, before FOUNDATION-OK
    found = (root / 'kernel' / 'core' / 'foundation.inc').read_text(
        encoding='utf-8')
    bpos = found.find('k64_report kmsg_blk_ok')
    ipos = found.find('call    fs_init')
    ppos = found.find('vmm_boot_protect', ipos)
    spos = found.find('call    fs_selftest')
    opos = found.find('k64_report kmsg_fs_ok')
    fpos = found.find('k64_report kmsg_foundation_ok')
    rep.check('source.foundation_fs_stage_order',
              0 <= bpos < ipos < ppos < spos < opos < fpos)
    rep.check('source.foundation_fs_protect',
              'mov     rcx, fs_data_start' in found and
              'mov     r8, MM_MAP_RW or MM_MAP_NX' in found)
    rep.check('source.foundation_fs_marker',
              "kmsg_fs_ok            db 'K64-FS-OK', 13, 10, 0" in found)
    rep.check('source.foundation_no_fs_skip', 'K64-FS-SKIP' not in found)

    # the frozen STI ownership across the whole kernel source tree is
    # unchanged: the fs module adds no third owner
    hits = []
    for path in sorted((root / 'kernel').rglob('*')):
        if path.suffix not in ('.asm', '.inc') or not path.is_file():
            continue
        text = path.read_text(encoding='utf-8')
        for m in re.finditer(r'^\s*sti\b', text, re.M):
            hits.append(str(path.relative_to(root)))
    rep.equal('source.kernel_sti_sites', hits,
              ['kernel/desktop/desktop.inc', 'kernel/irq/selftest.inc'])

    # the IRQ dispatch table is byte frozen and never mentions fs
    dispatch = (root / 'kernel' / 'irq' / 'dispatch.inc').read_text(
        encoding='utf-8')
    rep.check('source.dispatch_untouched', 'fs_' not in dispatch)

    # the data region is real zero bytes, never a BSS promise
    data = (root / 'kernel' / 'fs' / 'data.inc').read_text(encoding='utf-8')
    rep.check('source.data_no_rb',
              re.search(r'^\s*(\w+:)?\s*rb\s', data, re.M) is None)

    # every call in the module names its own code, blk_read or the
    # CPU console; nothing else is reachable
    allowed = {'fs_sec_read', 'fs_fat_entry', 'fs_cluster_lba',
               'fs_norm_comp', 'fs_dir_find', 'fs_resolve', 'fs_open',
               'fs_read', 'fs_seek', 'fs_close', 'fs_stat', 'fs_readdir',
               'fs_init', 'fs_selftest', 'blk_read', 'cpu_com1_puts',
               'cpu_com1_puthex'}
    stray = []
    for name in ('fs.inc', 'selftest.inc'):
        text = (root / 'kernel' / 'fs' / name).read_text(encoding='utf-8')
        for m in re.finditer(r'^\s*call\s+(\S+)', text, re.M):
            if m.group(1) not in allowed:
                stray.append('%s:%s' % (name, m.group(1)))
    rep.check('source.call_targets', not stray, repr(stray))
    # no write path vocabulary anywhere in the module body
    body = (root / 'kernel' / 'fs' / 'fs.inc').read_text(encoding='utf-8')
    rep.check('source.no_write_path',
              not re.search(r'^\s*(fs_write|fs_create|fs_mkdir|fs_unlink)',
                            body, re.M))


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
    ok, text = asm_.run(root / 'tests/stage2-fs/layout.asm', hi_path,
                        org=KERNEL_BASE)
    if not rep.check('reloc.assembles_at_kernel_base', ok, text.strip()[-400:]):
        return
    hi = load_image(hi_path, KERNEL_BASE)
    delta = KERNEL_BASE
    labels = M[2:88]      # every address label: code, data, blk and cpu
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
    # mov r64, imm64 windows: the whole immediate shifts by the base
    for p in find_all(lo_body, b'\x48'):
        if p + 10 > len(lo_body):
            continue
        op = lo_body[p + 1]
        if 0xB8 <= op <= 0xBF:
            lv = struct.unpack_from('<Q', lo_body, p + 2)[0]
            hv = struct.unpack_from('<Q', hi_body, p + 2)[0]
            if (low['code_start'] <= lv < low['data_end'] and
                    hv == lv + delta):
                allowed.update(range(p + 2, p + 10))
                windows += 1
        # mov r64, sign extended imm32 windows are handled below for
        # both the bare REX.W and the REX.W+REX.B (r8-r15) forms
    # mov r64, sign extended imm32 windows: only the low 32 bits are
    # emitted, the visible difference is the base modulo 2**32; the
    # REX.W+REX.B form (49 C7) addresses r8-r15, e.g. mov r8, fs_fd_table
    for prefix in (b'\x48\xC7', b'\x41\xC7', b'\x49\xC7'):
        for p in find_all(lo_body, prefix):
            if p + 7 > len(lo_body) or not 0xC0 <= lo_body[p + 2] <= 0xC7:
                continue
            lv = struct.unpack_from('<I', lo_body, p + 3)[0]
            hv = struct.unpack_from('<I', hi_body, p + 3)[0]
            if (lv < low['data_end'] and
                    (hv - lv) & 0xFFFFFFFF == delta & 0xFFFFFFFF):
                allowed.update(range(p + 3, p + 7))
                windows += 1
    # absolute disp32 addressing with an rcx index (mod=10, rm=001):
    # mov [label+rcx], al / mov al, [label+rcx] / cmp dil, [label+rcx]
    # / cmp byte [label+rcx], imm8; the disp32 shifts by the base mod 2**32
    for prefix in (b'\x88\x81', b'\x8A\x81', b'\x3A\x81', b'\x80\xB9'):
        for p in find_all(lo_body, prefix):
            if p + 6 > len(lo_body):
                continue
            lv = struct.unpack_from('<I', lo_body, p + 2)[0]
            hv = struct.unpack_from('<I', hi_body, p + 2)[0]
            if (low['code_start'] <= lv < low['data_end'] and
                    (hv - lv) & 0xFFFFFFFF == delta & 0xFFFFFFFF):
                allowed.update(range(p + 2, p + 6))
                windows += 1
    diffs = [i for i in range(len(lo_body)) if lo_body[i] != hi_body[i]]
    rep.check('reloc.code_diffs_only_shifted_immediates',
              all(i in allowed for i in diffs),
              '%d stray difference bytes' % sum(1 for i in diffs if i not in allowed))
    rep.check('reloc.absolute_immediates_found', windows >= 6,
              '%d shifted mov r64, label windows' % windows)


# ----------------------------------------------------------------------
# negative fixtures
# ----------------------------------------------------------------------

FIXTURES = [
    # defect class: counter drift. The frozen sector fetch total may
    # not move without re-deriving the mirror and the QEMU judge
    ('reads-final-drift', 'kernel/fs/api.inc',
     'FS_TEST_READS_FINAL     = 24', 'FS_TEST_READS_FINAL     = 25',
     'check'),
    # defect class: chain loop. The EOC guard of the directory walk
    # may not disappear: an exhausted chain is ENOENT, not a fallthrough
    ('dir-eoc-guard-removed', 'kernel/fs/fs.inc',
     '        cmp     edx, FS_FAT16_EOC_MIN\n        jae     .enoent\n',
     '', 'check'),
    # defect class: kanji lead byte. A leading 0x05 must compare as
    # 0xE5 or deleted-slot names leak into resolution
    ('kanji-fold-removed', 'kernel/fs/fs.inc',
     '        cmp     al, FS_DIR_KANJI\n        jne     .nocanji\n'
     '        mov     al, FS_DIR_DELETED\n',
     '', 'check'),
    # defect class: chain loop again, the hop budget itself. Without
    # it a FAT cycle spins forever instead of failing EIO
    ('hop-budget-removed', 'kernel/fs/fs.inc',
     '        dec     r12\n'
     '        jz      .eio                    ; hop budget spent: chain loop\n',
     '', 'check'),
    # defect class: illegal BPB. The boot signature check is the first
    # link of the init validation chain
    ('bpb-sig-check-removed', 'kernel/fs/fs.inc',
     '        cmp     word [fs_bpb_buf + FS_BPB_SIG], FS_BPB_SIG_VALUE\n'
     '        jne     .eio\n',
     '', 'check'),
    # defect class: illegal BPB, the FAT cache budget. spf above the
    # cache capacity must be an init EIO, never a truncated cache
    ('fat-cache-cap-removed', 'kernel/fs/fs.inc',
     '        cmp     eax, FS_FAT_CACHE_SECTORS\n        ja      .eio\n',
     '', 'check'),
    # defect class: dirent geometry. 24 bytes cannot carry attr+size+
    # the raw 8.3 name: the private assertion fires at assembly time
    ('dirent-size-drift', 'kernel/fs/api.inc',
     'FS_DIRENT_SIZE = 32', 'FS_DIRENT_SIZE = 24', 'assemble'),
    # defect class: the data pages must be real image bytes, never a
    # BSS promise the loader would not clear
    ('data-rb-reservation', 'kernel/fs/data.inc',
     'fs_sec_cache:        db FS_SECTOR_SIZE dup 0',
     'fs_sec_cache:        rb FS_SECTOR_SIZE', 'check'),
    # defect class: write refusal. The read-only mount must refuse
    # every write access mode before any resolution
    ('write-refusal-guard-removed', 'kernel/fs/fs.inc',
     '        and     eax, FS_O_ACCMODE\n        jnz     .eperm\n',
     '        and     eax, FS_O_ACCMODE\n', 'check'),
    # defect class: a sti anywhere in the module breaks the frozen
    # two owner STI contract
    ('sti-leak', 'kernel/fs/fs.inc',
     'fs_sec_read:\n        fs_enter\n',
     'fs_sec_read:\n        sti\n        fs_enter\n', 'check'),
    # defect class: the miss counter itself. Every completed miss must
    # bump fs_test_reads exactly once or the frozen totals drift
    ('miss-counter-removed', 'kernel/fs/fs.inc',
     '        call    blk_read\n        test    eax, eax\n'
     '        jnz     .out\n        inc     qword [fs_test_reads]\n',
     '        call    blk_read\n        test    eax, eax\n'
     '        jnz     .out\n', 'check'),
]


def run_fixtures(rep, root, out, fasmg, fasm2_dir):
    checker = root / 'tests' / 'stage2-fs' / 'check.py'
    for name, rel, old, new, where in FIXTURES:
        with tempfile.TemporaryDirectory(prefix='stage2-fs-%s-' % name) as tmp:
            tree = Path(tmp)
            (tree / 'tests').mkdir(parents=True)
            shutil.copytree(root / 'kernel', tree / 'kernel')
            shutil.copytree(root / 'tests' / 'stage2-fs',
                            tree / 'tests' / 'stage2-fs')
            # the oracle self tests need the builder, the ROM sources
            # and the frozen images inside the sandbox too
            (tree / 'tools').mkdir()
            shutil.copy2(root / 'tools' / 'build-fat16-image.py',
                         tree / 'tools' / 'build-fat16-image.py')
            shutil.copytree(root / 'user' / 'chip8', tree / 'user' / 'chip8')
            shutil.copytree(root / 'build' / 'fs-v1', tree / 'build' / 'fs-v1')
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
    out = Path(args.out).resolve() if args.out else root / 'build' / 'stage2-fs'
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
    ok, text = asm_.run(root / 'tests/stage2-fs/layout.asm', low_path)
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
    check_frames(rep, img)
    check_sec_read(rep, img)
    check_fat_leaves(rep, img)
    check_norm_comp(rep, img)
    check_dir_find(rep, img)
    check_read(rep, img)
    check_seek_close(rep, img)
    check_open_stat(rep, img)
    check_readdir(rep, img)
    check_init(rep, img)
    check_selftest(rep, img)
    check_module_hygiene(rep, img)
    check_model(rep, root, img, out)
    check_sources(rep, root)
    check_integration(rep, asm_, root, out)
    check_position_independence(rep, asm_, root, out, img)

    if not args.no_fixtures:
        run_fixtures(rep, root, out, asm_.fasmg, asm_.fasm2_dir)

    print('stage2-fs: layout.bin %d bytes, code %d bytes, data %d bytes' %
          (len(img.data), img['code_end'] - img['code_start'],
           img['data_end'] - img['data_start']))
    return 0 if rep.print(args.verbose) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))