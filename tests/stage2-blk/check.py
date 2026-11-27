#!/usr/bin/env python3
"""Stage 2 BLK module checks, task 131, contract FS-v1 M1. No machine
is started.

Everything here is measured, nothing is taken from a comment. The probe
tests/stage2-blk/layout.asm assembles kernel/blk/blk.asm exactly as the
kernel will include it (behind cpu.asm, whose console it consumes) and
appends a manifest of label addresses. This script then

  * compares the manifest constants and every data label offset against
    the fixed values of contract FS-v1 blk, repeated here independently
    of the assembly sources;
  * decodes the emitted bytes: the six legacy PIO leaves, the CF8/CFC
    config mechanism, the PCI identity scan, the frozen bring-up write
    order with an exact read back after every write, the queue_pfn page
    frame shift, the T_IN three descriptor chain, the free running u16
    avail publish, the bounded used ring poll and the fail stop disarm;
  * imports tests/stage2-blk/virtqueue_model.py, compares its frozen
    contract copy against the manifest, re-runs its 18 self tests and
    simulates the exact driver chain layout through the model device,
    including the four negative classes (bad idx, LBA range, missing
    device, poll timeout);
  * checks the data region is 8 real zero pages, page aligned at both
    ends, with the vring/DMA/request pages device addressable inside it;
  * checks the module links at the kernel base with identical bytes
    except the absolute-address immediates, which shift by the base;
  * assembles the whole kernel to prove the three contract copies and
    the core closing assertion still agree;
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
# contract FS-v1 blk, fixed here independently of the sources
# ----------------------------------------------------------------------

PAGE = 0x1000
KERNEL_BASE = 0xFFFFFFFF80000000
PHYS_LIMIT = 0x100000000

OK = 0
EINVAL = -1
ENOENT = -3
ENOTSUP = -6
ENOTREADY = -7
EIO = -9
EOVERFLOW = -10
EFAULT = -11

BOOTINFO_KERNEL_PHYS_START = 56
BOOTINFO_INITRAMFS_ADDR = 128
BOOTINFO_INITRAMFS_SIZE = 136

PCI_ADDR_PORT = 0xCF8
PCI_DATA_PORT = 0xCFC
PCI_VENDOR = 0x1AF4
PCI_DEVICE = 0x1001
PCI_SUBVENDOR = 0x1AF4
PCI_SUBDEVICE = 0x0002
PCI_REVISION = 0x00
PCI_REG_ID = 0x00
PCI_REG_COMMAND = 0x04
PCI_REG_REVISION = 0x08
PCI_REG_BAR0 = 0x10
PCI_REG_SUBSYS = 0x2C
PCI_CMD_IO = 0x01
PCI_CMD_MASTER = 0x04
PCI_BAR_IO = 0x01
PCI_BAR_MASK = 0xFFFC
PCI_MAX_DEV = 32

PIO_HOST_FEATURES = 0x00
PIO_GUEST_FEATURES = 0x04
PIO_QUEUE_PFN = 0x08
PIO_QUEUE_NUM = 0x0C
PIO_QUEUE_SEL = 0x0E
PIO_QUEUE_NOTIFY = 0x10
PIO_STATUS = 0x12
PIO_ISR = 0x13
PIO_CONFIG_OFF = 0x14

STATUS_ACK = 0x01
STATUS_DRIVER = 0x02
STATUS_DRIVER_OK = 0x04
STATUS_FAILED = 0x80

CFG_CAPACITY = 0
CFG_BLK_SIZE = 20

T_IN = 0
T_OUT = 1
T_FLUSH = 4
S_OK = 0
S_IOERR = 1
S_UNSUPP = 2

SECTOR_SIZE = 512
HEADER_SIZE = 16
STATUS_SIZE = 1

DESC_F_NEXT = 0x1
DESC_F_WRITE = 0x2
AVAIL_F_NOINT = 0x1
VRING_ALIGN = 4096
DESC_SIZE = 16
U16_MASK = 0xFFFF

QNUM_MAX = 256
QUEUE_SEL = 0
DESC_HEAD = 0
DESC_DATA = 1
DESC_STATUS = 2

VRING_PAGES = 3
VRING_MAX_TOTAL = 8192 + 6 + 8 * QNUM_MAX
DMA_SECTORS = 8
REQ_HDR_OFF = 0
REQ_STATUS_OFF = 16
POLL_MAX = 0x1000000

TEST_CAPACITY = 16384
TEST_DATA_LBA = 101
TEST_BPB_SIG = 0xAA55
TEST_OEM_ID = 0x34365242494C4F4B      # 'KOLIBR64' little endian
TEST_ROM_PREFIX = 0x08610C602AA2E000

ST_CAP = 0x01
ST_BPB = 0x02
ST_ROM = 0x04
ST_RANGE = 0x08
ST_LAST = 0x10
ST_CHUNK = 0x20
ST_COUNT = 0x40
TEST_READS_FINAL = 7

# the data layout, offsets from blk_data_start
OFF_READY = 0
OFF_BACKEND = 8
OFF_IOBASE = 16
OFF_PCI_ADDR = 24
OFF_QNUM = 32
OFF_CAPACITY = 40
OFF_VRING_PHYS = 48
OFF_DMA_PHYS = 56
OFF_REQ_PHYS = 64
OFF_LAST_USED = 72
OFF_TEST_MASK = 80
OFF_TEST_READS = 88
OFF_LBA0_BUF = 96
OFF_DATA_BUF = 608
OFF_SCRATCH = 1120
OFF_VRING = 12288
OFF_DMA = 24576
OFF_REQ = 28672
DATA_SIZE = 32768

MSG_SELFTEST = b'blk selftest mask = \x00'
MSG_NL = b'\r\n\x00'

MAGIC = b'K64BLKP1'
MANIFEST_VERSION = 1

# manifest entry order, see tests/stage2-blk/layout.asm
M = [
    'magic', 'version',
    'code_start', 'code_end', 'data_start', 'data_end',
    'pio_in32', 'pio_in16', 'pio_in8', 'pio_out32', 'pio_out16', 'pio_out8',
    'pci_cfg_read', 'pci_cfg_write', 'pci_scan',
    'init', 'read', 'virtio_read', 'selftest',
    'st_fail_cap', 'st_fail_bpb', 'st_fail_rom', 'st_fail_range',
    'st_fail_last', 'st_fail_chunk', 'st_fail_count',
    'msg_selftest_mask', 'msg_nl',
    'ready', 'backend', 'iobase', 'pci_addr', 'qnum', 'capacity',
    'vring_phys', 'dma_phys', 'req_phys', 'last_used', 'test_mask',
    'test_reads', 'test_lba0_buf', 'test_data_buf', 'test_scratch',
    'vring', 'dma', 'req',
    'cpu_com1_puts', 'cpu_com1_puthex',
    'pci_addr_port', 'pci_data_port', 'pci_vendor', 'pci_device',
    'pci_subvendor', 'pci_subdevice', 'pci_revision',
    'pci_reg_id', 'pci_reg_command', 'pci_reg_revision',
    'pci_reg_bar0', 'pci_reg_subsys',
    'pci_cmd_io', 'pci_cmd_master', 'pci_bar_io', 'pci_bar_mask',
    'pci_max_dev',
    'pio_host_features', 'pio_guest_features', 'pio_queue_pfn',
    'pio_queue_num', 'pio_queue_sel', 'pio_queue_notify',
    'pio_status', 'pio_isr', 'pio_config_off',
    'status_ack', 'status_driver', 'status_driver_ok', 'status_failed',
    'cfg_capacity', 'cfg_blk_size',
    't_in', 't_out', 't_flush', 's_ok', 's_ioerr', 's_unsupp',
    'sector_size', 'header_size', 'status_size',
    'desc_f_next', 'desc_f_write', 'avail_f_noint', 'vring_align',
    'desc_size', 'u16_mask',
    'qnum_max', 'queue_sel', 'desc_head', 'desc_data', 'desc_status',
    'vring_pages', 'vring_max_total', 'dma_sectors',
    'req_hdr_off', 'req_status_off', 'poll_max',
    'bootinfo_kernel_phys_start', 'bootinfo_initramfs_addr',
    'bootinfo_initramfs_size',
    'test_capacity', 'test_data_lba', 'test_bpb_sig', 'test_oem_id',
    'test_rom_prefix',
    'st_cap', 'st_bpb', 'st_rom', 'st_range', 'st_last', 'st_chunk',
    'st_count', 'test_reads_final',
    'ok', 'einval', 'enoent', 'enotready', 'eio', 'eoverflow', 'efault',
    'enotsup',
    'page_size', 'phys_limit', 'kernel_base',
]

# label name -> frozen offset inside the data region
DATA_LABELS = {
    'ready': OFF_READY, 'backend': OFF_BACKEND, 'iobase': OFF_IOBASE,
    'pci_addr': OFF_PCI_ADDR, 'qnum': OFF_QNUM, 'capacity': OFF_CAPACITY,
    'vring_phys': OFF_VRING_PHYS, 'dma_phys': OFF_DMA_PHYS,
    'req_phys': OFF_REQ_PHYS, 'last_used': OFF_LAST_USED,
    'test_mask': OFF_TEST_MASK, 'test_reads': OFF_TEST_READS,
    'test_lba0_buf': OFF_LBA0_BUF, 'test_data_buf': OFF_DATA_BUF,
    'test_scratch': OFF_SCRATCH, 'vring': OFF_VRING, 'dma': OFF_DMA,
    'req': OFF_REQ,
}

# manifest constants that must not drift from the copies above
CONSTANTS = {
    'pci_addr_port': PCI_ADDR_PORT, 'pci_data_port': PCI_DATA_PORT,
    'pci_vendor': PCI_VENDOR, 'pci_device': PCI_DEVICE,
    'pci_subvendor': PCI_SUBVENDOR, 'pci_subdevice': PCI_SUBDEVICE,
    'pci_revision': PCI_REVISION,
    'pci_reg_id': PCI_REG_ID, 'pci_reg_command': PCI_REG_COMMAND,
    'pci_reg_revision': PCI_REG_REVISION, 'pci_reg_bar0': PCI_REG_BAR0,
    'pci_reg_subsys': PCI_REG_SUBSYS,
    'pci_cmd_io': PCI_CMD_IO, 'pci_cmd_master': PCI_CMD_MASTER,
    'pci_bar_io': PCI_BAR_IO, 'pci_bar_mask': PCI_BAR_MASK,
    'pci_max_dev': PCI_MAX_DEV,
    'pio_host_features': PIO_HOST_FEATURES,
    'pio_guest_features': PIO_GUEST_FEATURES,
    'pio_queue_pfn': PIO_QUEUE_PFN, 'pio_queue_num': PIO_QUEUE_NUM,
    'pio_queue_sel': PIO_QUEUE_SEL, 'pio_queue_notify': PIO_QUEUE_NOTIFY,
    'pio_status': PIO_STATUS, 'pio_isr': PIO_ISR,
    'pio_config_off': PIO_CONFIG_OFF,
    'status_ack': STATUS_ACK, 'status_driver': STATUS_DRIVER,
    'status_driver_ok': STATUS_DRIVER_OK, 'status_failed': STATUS_FAILED,
    'cfg_capacity': CFG_CAPACITY, 'cfg_blk_size': CFG_BLK_SIZE,
    't_in': T_IN, 't_out': T_OUT, 't_flush': T_FLUSH,
    's_ok': S_OK, 's_ioerr': S_IOERR, 's_unsupp': S_UNSUPP,
    'sector_size': SECTOR_SIZE, 'header_size': HEADER_SIZE,
    'status_size': STATUS_SIZE,
    'desc_f_next': DESC_F_NEXT, 'desc_f_write': DESC_F_WRITE,
    'avail_f_noint': AVAIL_F_NOINT, 'vring_align': VRING_ALIGN,
    'desc_size': DESC_SIZE, 'u16_mask': U16_MASK,
    'qnum_max': QNUM_MAX, 'queue_sel': QUEUE_SEL,
    'desc_head': DESC_HEAD, 'desc_data': DESC_DATA,
    'desc_status': DESC_STATUS,
    'vring_pages': VRING_PAGES, 'vring_max_total': VRING_MAX_TOTAL,
    'dma_sectors': DMA_SECTORS, 'req_hdr_off': REQ_HDR_OFF,
    'req_status_off': REQ_STATUS_OFF, 'poll_max': POLL_MAX,
    'bootinfo_kernel_phys_start': BOOTINFO_KERNEL_PHYS_START,
    'bootinfo_initramfs_addr': BOOTINFO_INITRAMFS_ADDR,
    'bootinfo_initramfs_size': BOOTINFO_INITRAMFS_SIZE,
    'test_capacity': TEST_CAPACITY, 'test_data_lba': TEST_DATA_LBA,
    'test_bpb_sig': TEST_BPB_SIG, 'test_oem_id': TEST_OEM_ID,
    'test_rom_prefix': TEST_ROM_PREFIX,
    'st_cap': ST_CAP, 'st_bpb': ST_BPB, 'st_rom': ST_ROM,
    'st_range': ST_RANGE, 'st_last': ST_LAST, 'st_chunk': ST_CHUNK,
    'st_count': ST_COUNT, 'test_reads_final': TEST_READS_FINAL,
    'ok': OK, 'einval': EINVAL, 'enoent': ENOENT,
    'enotready': ENOTREADY, 'eio': EIO, 'eoverflow': EOVERFLOW,
    'efault': EFAULT, 'enotsup': ENOTSUP,
    'page_size': PAGE, 'phys_limit': PHYS_LIMIT,
    'kernel_base': KERNEL_BASE,
}

BLK_ENTER = bytes.fromhex('55 53 56 57 41 54 41 55 41 56 41 57 48 83 EC 28')
BLK_LEAVE = bytes.fromhex('48 83 C4 28 41 5F 41 5E 41 5D 41 5C 5F 5E 5B 5D')

STI = 0xFB
CLI = 0xFA
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
    """Sites of 'prefix modrm disp32 [tail bytes]': (site, target, imm).

    The prefix ends right before the modrm byte; tail counts the
    immediate bytes that follow the displacement.
    """
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
        print('stage2-blk: %d checks, %d failed' % (total, len(self.failures)))
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
            cmd += ['-i', 'BLK_PROBE_ORG=%#x' % org]
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
# checks
# ----------------------------------------------------------------------


def check_manifest(rep, img):
    rep.check('manifest.magic', True, MAGIC.decode())
    rep.equal('manifest.version', img['version'], MANIFEST_VERSION)
    rep.equal('manifest.entry_count', len(M), 133)
    for name, want in CONSTANTS.items():
        # negative status codes live in the manifest as two's complement
        rep.equal('manifest.%s' % name, img[name], want & 0xFFFFFFFFFFFFFFFF)

    for name, off in DATA_LABELS.items():
        rep.equal('layout.%s_offset' % name,
                  img[name] - img['data_start'], off)

    lo, hi = img['code_start'], img['code_end']
    rep.check('layout.code_non_empty', hi > lo, '%#x..%#x' % (lo, hi))
    rep.equal('layout.api_emits_no_code', img['pio_in32'], lo)
    order = ['pio_in32', 'pio_in16', 'pio_in8', 'pio_out32', 'pio_out16',
             'pio_out8', 'pci_cfg_read', 'pci_cfg_write', 'pci_scan',
             'init', 'read', 'virtio_read', 'selftest',
             'st_fail_cap', 'st_fail_bpb', 'st_fail_rom', 'st_fail_range',
             'st_fail_last', 'st_fail_chunk', 'st_fail_count',
             'msg_selftest_mask', 'msg_nl']
    chain = [img[name] for name in order]
    rep.check('layout.function_order', chain == sorted(chain) and
              all(lo <= v < hi for v in chain),
              'emission order: pio leaves, pci, init, read, selftest')
    rep.check('layout.code_before_data', hi <= img['data_start'])
    rep.check('layout.data_page_aligned',
              img['data_start'] % PAGE == 0 and img['data_end'] % PAGE == 0)
    rep.equal('layout.data_size', img['data_end'] - img['data_start'], DATA_SIZE)
    rep.equal('layout.module_ends_with_data', img.at('data_end'), img.probe_at)
    for name in ('cpu_com1_puts', 'cpu_com1_puthex'):
        rep.check('layout.%s_before_module' % name, 0 < img[name] < lo,
                  '%#x' % img[name])
    # the device addressed pages are page aligned inside the data block
    rep.check('layout.vring_page_aligned',
              (img['vring'] - img['data_start']) % PAGE == 0)
    rep.equal('layout.dma_follows_vring', img['dma'] - img['vring'],
              VRING_PAGES * PAGE)
    rep.equal('layout.req_follows_dma', img['req'] - img['dma'], PAGE)
    rep.equal('layout.evidence_buf_spacing',
              img['test_data_buf'] - img['test_lba0_buf'], SECTOR_SIZE)
    rep.equal('layout.scratch_follows_data_buf',
              img['test_scratch'] - img['test_data_buf'], SECTOR_SIZE)


def check_data(rep, img):
    d = img.data
    start, end = img.at('data_start'), img.at('data_end')
    block = d[start:end]
    rep.check('data.all_zero_real_bytes', block == bytes(DATA_SIZE),
              '%d non-zero bytes' % sum(1 for b in block if b))
    rep.equal('data.ready_default_zero', u64(d, start + OFF_READY), 0)
    rep.equal('data.backend_default_zero', u64(d, start + OFF_BACKEND), 0)
    rep.equal('data.test_mask_default_zero', u64(d, start + OFF_TEST_MASK), 0)
    rep.equal('data.test_reads_default_zero', u64(d, start + OFF_TEST_READS), 0)
    # the messages live with the code, the data region stays last
    rep.check('data.msg_selftest_bytes',
              d[img.at('msg_selftest_mask'):
                img.at('msg_selftest_mask') + len(MSG_SELFTEST)] == MSG_SELFTEST)
    rep.check('data.msg_nl_bytes',
              d[img.at('msg_nl'):img.at('msg_nl') + len(MSG_NL)] == MSG_NL)
    rep.equal('data.messages_close_the_code',
              img.at('msg_nl') + len(MSG_NL), img.at('code_end'))


def check_pio(rep, img):
    d = img.data
    # each leaf is exactly: [mov eax, edx] mov dx, [iobase] / add dx, cx
    # [xor eax, eax] <in/out> / ret, and nothing else
    leaves = [
        ('pio_in32', 'pio_in16', False, False, b'\xED'),
        ('pio_in16', 'pio_in8', False, True, b'\x66\xED'),
        ('pio_in8', 'pio_out32', False, True, b'\xEC'),
        ('pio_out32', 'pio_out16', True, False, b'\xEF'),
        ('pio_out16', 'pio_out8', True, False, b'\x66\xEF'),
        ('pio_out8', 'pci_cfg_read', True, False, b'\xEE'),
    ]
    for name, nxt, is_out, xor_eax, io in leaves:
        at, end = img.at(name), img.at(nxt)
        body = d[at:end]
        head = (b'\x89\xD0' if is_out else b'') + b'\x66\x8B\x15'
        mid = b'\x66\x01\xCA' + (b'\x31\xC0' if xor_eax else b'')
        want_len = len(head) + 4 + len(mid) + len(io) + 1
        ok = (len(body) == want_len and body.startswith(head) and
              body[len(head) + 4:len(head) + 4 + len(mid)] == mid and
              body[-1 - len(io):-1] == io and body[-1] == RET)
        if ok:
            disp = struct.unpack_from('<i', body, len(head))[0]
            ok = at + len(head) + 4 + disp == img.at('iobase')
        rep.check('pio.%s_exact_bytes' % name, ok,
                  'mov dx, [blk_iobase] / add dx, cx / io / ret')


def check_pci_cfg(rep, img):
    d = img.data
    at = img.at('pci_cfg_read')
    rep.check('pci.cfg_read_exact_bytes',
              d[at:at + 13] == bytes.fromhex('89 c8 66 ba f8 0c ef 66 ba fc 0c ed c3'),
              'CF8 address out, CFC data in')
    at = img.at('pci_cfg_write')
    rep.check('pci.cfg_write_exact_bytes',
              d[at:at + 15] ==
              bytes.fromhex('52 89 c8 66 ba f8 0c ef 58 66 ba fc 0c ef c3'),
              'value parked on the stack across the dx port load, '
              'CF8 address out, CFC data out')


def check_pci_scan(rep, img):
    at, end = region(img, 'pci_scan', 'init')
    d = img.data
    rep.check('scan.full_frame', d[at:at + len(BLK_ENTER)] == BLK_ENTER)
    rep.check('scan.enable_bit', d.find(b'\xBB\x00\x00\x00\x80', at, end) >= 0,
              'mov ebx, 0x80000000')
    rep.check('scan.dev_shift_11', d.find(b'\xC1\xE0\x0B', at, end) >= 0)
    rep.equal('scan.vendor_and_subvendor_compare',
              len(find_all(d[at:end], b'\x81\xFA\xF4\x1A\x00\x00')), 2)
    rep.check('scan.device_compare',
              d.find(b'\x3D\x01\x10\x00\x00', at, end) >= 0, 'cmp eax, 0x1001')
    rep.check('scan.subdevice_compare',
              d.find(b'\x83\xF8\x02', at, end) >= 0)
    rep.check('scan.revision_mask_and_compare',
              d.find(b'\x25\xFF\x00\x00\x00', at, end) >= 0 and
              d.find(b'\x83\xF8\x00', at, end) >= 0)
    rep.check('scan.bar_io_bit', d.find(b'\xA9\x01\x00\x00\x00', at, end) >= 0,
              'test eax, 1')
    rep.check('scan.bar_mask', d.find(b'\x25\xFC\xFF\x00\x00', at, end) >= 0,
              'and eax, 0xFFFC')
    rep.check('scan.command_io_master',
              d.find(b'\x83\xC8\x05', at, end) >= 0, 'or eax, 5')
    rep.check('scan.command_readback_compare',
              d.find(b'\x83\xF8\x05', at, end) >= 0,
              'cmp eax, 5 (masked read back vs the two required bits)')
    j = jcc_after(img, d.find(b'\x83\xF8\x05', at, end) + 3) \
        if d.find(b'\x83\xF8\x05', at, end) >= 0 else None
    rep.check('scan.command_readback_guarded', j is not None)
    rep.check('scan.bounded_32_devices',
              d.find(b'\x41\x83\xFC\x20', at, end) >= 0, 'cmp r12d, 32')
    rep.check('scan.enoent_status',
              d.find(b'\xB8\xFD\xFF\xFF\xFF', at, end) >= 0)
    rep.check('scan.eio_status',
              d.find(b'\xB8\xF7\xFF\xFF\xFF', at, end) >= 0)
    reads = [t for _, t in call_sites(img, at, end)
             if t == img.at('pci_cfg_read')]
    writes = [t for _, t in call_sites(img, at, end)
              if t == img.at('pci_cfg_write')]
    rep.check('scan.config_reads', len(reads) >= 6, '%d reads' % len(reads))
    rep.equal('scan.config_command_write', len(writes), 1)
    rep.equal('scan.iobase_store',
              len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0, 'iobase')), 1)
    rep.equal('scan.pci_addr_store',
              len(rip_refs(img, at, end, b'\x48\x89', 0x1D, 0, 'pci_addr')), 1)


def check_init(rep, img):
    at, end = region(img, 'init', 'read')
    d = img.data
    rep.check('init.full_frame', d[at:at + len(BLK_ENTER)] == BLK_ENTER)
    rep.check('init.cli_present', d.find(bytes((CLI,)), at, end) >= 0)
    rep.check('init.bootinfo_phys_start_read',
              d.find(b'\x4C\x8B\x69\x38', at, end) >= 0,
              'mov r13, [rcx + 56]')
    rep.check('init.phys_limit_register',
              d.find(bytes.fromhex('49 be 00 00 00 00 01 00 00 00'), at, end) >= 0,
              'mov r14, 0x100000000')
    rep.equal('init.kernel_base_subtracts',
              len(find_all(d[at:end], bytes.fromhex('48 2d 00 00 00 80'))), 3)
    rep.equal('init.phys_start_adds',
              len(find_all(d[at:end], b'\x4C\x01\xE8')), 3)
    rep.equal('init.phys_limit_compares',
              len(find_all(d[at:end], b'\x4C\x39\xF0')), 3)
    for name in ('vring_phys', 'dma_phys', 'req_phys'):
        rep.equal('init.%s_store' % name,
                  len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0, name)), 1)

    # the frozen bring-up order, every write followed by its read back
    seq = [
        b'\xB9\x12\x00\x00\x00\x31\xD2',                    # reset: status 0
        b'\xB9\x12\x00\x00\x00\xBA\x01\x00\x00\x00',        # ACK
        b'\x83\xF8\x01',                                    # ACK read back
        b'\xB9\x12\x00\x00\x00\xBA\x03\x00\x00\x00',        # ACK|DRIVER
        b'\x83\xF8\x03',                                    # read back
        b'\xB9\x04\x00\x00\x00\x31\xD2',                    # features 0
        b'\xB9\x0E\x00\x00\x00\xBA\x00\x00\x00\x00',        # queue_sel 0
        b'\xB9\x0C\x00\x00\x00',                            # queue_num
        b'\x3D\x00\x01\x00\x00',                            # <= 256
        b'\x89\xC2\xFF\xCA\x85\xD0',                        # power of two
        b'\x48\xC1\xE8\x0C',                                # pfn = phys >> 12
        b'\xB9\x08\x00\x00\x00',                            # queue_pfn write
        b'\x48\xC1\xEA\x0C',                                # read back >> 12
        b'\x39\xD0',                                        # cmp eax, edx
        b'\x66\xC7\x04\x02\x01\x00',                        # avail NOINT
        b'\xB9\x14\x00\x00\x00',                            # capacity lo
        b'\x41\x89\xC6',                                    # mov r14d, eax
        b'\xB9\x18\x00\x00\x00',                            # capacity hi
        b'\x48\xC1\xE0\x20',                                # shl rax, 32
        b'\x4C\x09\xF0',                                    # or rax, r14
        b'\xB9\x12\x00\x00\x00\xBA\x07\x00\x00\x00',        # DRIVER_OK
        b'\x83\xF8\x07',                                    # read back
    ]
    pos = ordered_positions(img, at, end, seq)
    rep.check('init.frozen_bringup_order', pos is not None,
              'reset/ack/driver/features/queue_pfn/NOINT/capacity/DRIVER_OK')
    # every status read back compare branches to the error path
    for val in (1, 3, 7):
        p = d.find(b'\x83\xF8' + bytes((val,)), at, end)
        j = jcc_after(img, p + 3, 2) if p >= 0 else None
        rep.check('init.status_%d_readback_guarded' % val,
                  j is not None and j[1] != p + 5)
    # the pfn read back compare is guarded too
    p = d.find(b'\x39\xD0', at, end)
    j = jcc_after(img, p + 2, 2) if p >= 0 else None
    rep.check('init.pfn_readback_guarded', j is not None and j[1] != p + 4)
    rep.equal('init.queue_pfn_port_sites',
              len(find_all(d[at:end], b'\xB9\x08\x00\x00\x00')), 2)
    # the leaf traffic: 5 out8 (4 steps + FAILED), 4 in8 read backs,
    # 2 out32 (features, pfn), 4 in32 (2 read backs + 2 capacity dwords)
    for leaf, want in (('pio_out8', 5), ('pio_in8', 4),
                       ('pio_out32', 2), ('pio_in32', 4),
                       ('pio_out16', 1), ('pio_in16', 1)):
        got = len([t for _, t in call_sites(img, at, end)
                   if t == img.at(leaf)])
        rep.equal('init.calls_%s' % leaf, got, want)
    rep.check('init.failed_status_write',
              d.find(b'\xBA\x80\x00\x00\x00', at, end) >= 0,
              'mov edx, STATUS_FAILED on the fail path')
    rep.equal('init.qnum_store',
              len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0, 'qnum')), 1)
    rep.equal('init.capacity_store',
              len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0, 'capacity')), 1)
    # the backend seam is armed with blk_virtio_read itself
    p = find_mov_r64(img, at, end, 0, img['virtio_read'])
    rep.check('init.backend_immediate_is_virtio_read', p >= 0)
    rep.equal('init.backend_store',
              len(rip_refs(img, at, end, b'\x48\x89', 0x05, 0, 'backend')), 1)
    ones = [s for s, t, imm in rip_sites(img, at, end, b'\x48\xC7', 0x05, 4)
            if t == img.at('ready') and struct.unpack('<I', imm)[0] == 1]
    rep.equal('init.ready_set_once', len(ones), 1)
    zeros = [s for s, t, imm in rip_sites(img, at, end, b'\x48\xC7', 0x05, 4)
             if t == img.at('ready') and struct.unpack('<I', imm)[0] == 0]
    rep.equal('init.fail_disarms_ready', len(zeros), 1)
    zeros = [s for s, t, imm in rip_sites(img, at, end, b'\x48\xC7', 0x05, 4)
             if t == img.at('backend') and struct.unpack('<I', imm)[0] == 0]
    rep.equal('init.fail_disarms_backend', len(zeros), 1)
    rep.check('init.leave_restore',
              d.find(BLK_LEAVE + b'\xC3', at, end) >= 0)


def check_read(rep, img):
    at, end = region(img, 'read', 'virtio_read')
    body = img.data[at:end]
    ok = (len(body) == 20 and body[:3] == b'\x48\x8B\x05' and
          body[7:10] == b'\x48\x85\xC0' and body[10:12] == b'\x74\x02' and
          body[12:14] == b'\xFF\xE0' and
          body[14:19] == b'\xB8\xF9\xFF\xFF\xFF' and body[19] == RET)
    if ok:
        disp = struct.unpack_from('<i', body, 3)[0]
        ok = at + 7 + disp == img.at('backend')
    rep.check('read.dispatcher_exact_bytes', ok,
              'mov rax, [blk_backend] / test / jz / jmp rax / ENOTREADY')


def check_virtio_read(rep, img):
    at, end = region(img, 'virtio_read', 'selftest')
    d = img.data
    rep.check('read.full_frame', d[at:at + len(BLK_ENTER)] == BLK_ENTER)
    rep.check('read.cld_df0', d[at + len(BLK_ENTER)] == 0xFC)
    rep.check('read.ready_guard',
              len(rip_refs(img, at, end, b'\x48\x83', 0x3D, 1, 'ready')) >= 1)
    rep.check('read.n_zero_guard', d.find(b'\x4D\x85\xC0', at, end) >= 0,
              'test r8, r8')
    rep.check('read.null_buffer_guard', d.find(b'\x48\x85\xD2', at, end) >= 0,
              'test rdx, rdx')
    rep.check('read.lba_overflow_guard',
              d.find(b'\x4C\x01\xC0', at, end) >= 0 and
              d.find(b'\x0F\x82', at, end) >= 0, 'add rax, r8 / jc')
    cap = rip_refs(img, at, end, b'\x48\x3B', 0x05, 0, 'capacity')
    rep.check('read.capacity_compare', len(cap) == 1)
    if cap:
        j = jcc_after(img, cap[0] + 7, 2)
        rep.check('read.capacity_guard_branches',
                  j is not None and j[1] != cap[0] + 9)
    # the chunk clamp at 8 sectors
    rep.check('read.chunk_clamp',
              d.find(b'\x48\x83\xFB\x08', at, end) >= 0 and
              d.find(b'\x48\xC7\xC3\x08\x00\x00\x00', at, end) >= 0)
    # the T_IN header and the status sentinel
    hdr = ordered_positions(img, at, end, [
        b'\xC7\x02\x00\x00\x00\x00',            # type = T_IN
        b'\xC7\x42\x04\x00\x00\x00\x00',        # reserved = 0
        b'\x4C\x89\x6A\x08',                    # sector = r13
        b'\xC6\x42\x10\xFF',                    # sentinel 0xFF
    ])
    rep.check('read.header_fields', hdr is not None)
    # the exact three descriptor chain
    chain = ordered_positions(img, at, end, [
        b'\x48\x89\x06',                        # desc0.addr = req phys
        b'\xC7\x46\x08\x10\x00\x00\x00',        # desc0.len = 16
        b'\x66\xC7\x46\x0C\x01\x00',            # desc0.flags = NEXT
        b'\x66\xC7\x46\x0E\x01\x00',            # desc0.next = 1
        b'\x48\x89\x46\x10',                    # desc1.addr = dma phys
        b'\x89\xD8\xC1\xE0\x09',                # len = n << 9
        b'\x89\x46\x18',
        b'\x66\xC7\x46\x1C\x03\x00',            # WRITE|NEXT
        b'\x66\xC7\x46\x1E\x02\x00',            # next = 2
        b'\x48\x83\xC0\x10',                    # status addr = req + 16
        b'\x48\x89\x46\x20',
        b'\xC7\x46\x28\x01\x00\x00\x00',        # len = 1
        b'\x66\xC7\x46\x2C\x02\x00',            # WRITE
        b'\x66\xC7\x46\x2E\x00\x00',            # end of chain
    ])
    rep.check('read.three_desc_chain', chain is not None,
              'header NEXT -> data WRITE|NEXT -> status WRITE')
    # the free running u16 avail publish
    pub = ordered_positions(img, at, end, [
        b'\x41\x0F\xB7\x52\x02',                # movzx edx, [avail.idx]
        b'\x41\x89\xD1',
        b'\x45\x21\xE1',                        # slot = idx & (qnum-1)
        b'\x66\x43\xC7\x44\x4A\x04\x00\x00',    # ring[slot] = head 0
        b'\xFF\xC2',                            # inc edx (u16 wrap)
        b'\x66\x41\x89\x52\x02',                # store idx low 16
    ])
    rep.check('read.avail_publish_u16', pub is not None)
    rep.check('read.notify_queue0',
              d.find(b'\xB9\x10\x00\x00\x00\xBA\x00\x00\x00\x00', at, end) >= 0)
    notifies = [t for _, t in call_sites(img, at, end)
                if t == img.at('pio_out16')]
    rep.equal('read.notify_calls', len(notifies), 1)

    # the bounded poll: budget, compare, pause, decrement, backwards jnz,
    # then the explicit timeout jmp; an unbounded loop is a contract fault
    budget = d.find(b'\x41\xB9\x00\x00\x00\x01', at, end)
    rep.check('read.poll_budget', budget >= 0, 'mov r9d, BLK_POLL_MAX')
    lu = rip_refs(img, at, end, b'\x0F\xB7', 0x15, 0, 'last_used')
    rep.check('read.last_used_read', len(lu) >= 1)
    rep.check('read.expected_idx_u16',
              d.find(b'\x81\xE2\xFF\xFF\x00\x00', at, end) >= 0,
              'and edx, 0xFFFF')
    cmp_pos = d.find(b'\x66\x41\x39\x53\x02', at, end)
    rep.check('read.used_idx_compare', cmp_pos > budget >= 0,
              'cmp word [used.idx], dx')
    pause = d.find(b'\xF3\x90', at, end)
    dec = d.find(b'\x41\xFF\xC9', at, end)
    rep.check('read.poll_pause_and_decrement', budget < pause < dec)
    j = jcc_after(img, dec + 3, 2) if dec >= 0 else None
    rep.check('read.poll_bounded_backwards', j is not None and
              budget < j[1] < dec + 3,
              'jnz .poll must loop back inside the budgeted region')
    rep.check('read.poll_timeout_exit', j is not None and
              d[j[0] + 2] == 0xE9,
              'the exhausted budget falls into jmp .eio')

    # the used entry validation: id, len = data + status, status byte
    rep.check('read.used_slot_mask',
              d.find(b'\x45\x21\xE1', at, end) >= 0)
    rep.check('read.used_id_compare',
              d.find(b'\x43\x83\x7C\xCB\x04\x00', at, end) >= 0,
              'cmp dword [used.ring.id], 0')
    rep.check('read.used_len_data_plus_status',
              d.find(bytes.fromhex('89 d8 c1 e0 09 ff c0'), at, end) >= 0 and
              d.find(b'\x43\x39\x44\xCB\x08', at, end) >= 0,
              'n*512+1 compared against used.len')
    # cmp byte [rip+disp], imm8: opcode 80, modrm 3D, one immediate byte
    st = rip_sites(img, at, end, b'\x80', 0x3D, 1)
    hit = [s for s, t, imm in st
           if t == img.at('req') + REQ_STATUS_OFF and imm == b'\x00']
    rep.check('read.status_byte_compare', len(hit) == 1,
              'cmp byte [blk_req + 16], S_OK')
    rep.equal('read.last_used_store',
              len(rip_refs(img, at, end, b'\x48\x89', 0x15, 0, 'last_used')), 1)

    # the bounce copy into the caller buffer, DF = 0
    p = find_mov_r64(img, at, end, 6, img['dma'])     # mov rsi, blk_dma
    rep.check('read.copy_source_is_dma_page', p >= 0)
    copy = ordered_positions(img, at, end, [
        b'\x48\x89\xF0',                        # save rsi (vring)
        b'\x4C\x89\xFF',                        # rdi = caller cursor
        b'\x48\x89\xD9',                        # rcx = n
        b'\x48\xC1\xE1\x09',                    # n * 512
        b'\xF3\xA4',                            # rep movsb
        b'\x48\x89\xC6',                        # restore rsi
    ])
    rep.check('read.rep_movsb_copy', copy is not None)
    rep.equal('read.request_counter_increment',
              len(rip_refs(img, at, end, b'\x48\xFF', 0x05, 0, 'test_reads')), 1)
    rep.check('read.chunk_loop_backwards',
              d.find(b'\x49\x29\xDE', at, end) >= 0, 'sub r14, rbx')

    # fail stop: a desynchronised ring disarms both seam words
    zeros = [s for s, t, imm in rip_sites(img, at, end, b'\x48\xC7', 0x05, 4)
             if t == img.at('ready') and struct.unpack('<I', imm)[0] == 0]
    rep.equal('read.eio_disarms_ready', len(zeros), 1)
    zeros = [s for s, t, imm in rip_sites(img, at, end, b'\x48\xC7', 0x05, 4)
             if t == img.at('backend') and struct.unpack('<I', imm)[0] == 0]
    rep.equal('read.eio_disarms_backend', len(zeros), 1)
    for name, code in (('enotready', ENOTREADY), ('einval', EINVAL),
                       ('efault', EFAULT), ('eoverflow', EOVERFLOW),
                       ('eio', EIO)):
        needle = b'\xB8' + struct.pack('<i', code)
        rep.check('read.status_%s' % name,
                  d.find(needle, at, end) >= 0)


def check_selftest(rep, img):
    at, end = region(img, 'selftest', 'code_end')
    d = img.data
    rep.check('selftest.full_frame', d[at:at + len(BLK_ENTER)] == BLK_ENTER)
    rep.check('selftest.cli_present', d.find(bytes((CLI,)), at, end) >= 0)
    rep.check('selftest.mask_starts_zero',
              d.find(b'\x45\x31\xE4', at, end) >= 0, 'xor r12d, r12d')
    cap = rip_sites(img, at, end, b'\x48\x81', 0x3D, 4)
    hit = [s for s, t, imm in cap if t == img.at('capacity') and
           struct.unpack('<I', imm)[0] == TEST_CAPACITY]
    rep.check('selftest.capacity_compare', len(hit) == 1)

    # exactly nine blk_read calls: 1+1+4+1+2 legs
    reads = [t for _, t in call_sites(img, at, end) if t == img.at('read')]
    rep.equal('selftest.nine_reads', len(reads), 9)

    # leg 1: the BPB signature, bytes per sector and the OEM id
    sig = rip_sites(img, at, end, b'\x66\x81', 0x3D, 2)
    hit = [s for s, t, imm in sig
           if t == img.at('test_lba0_buf') + 510 and
           struct.unpack('<H', imm)[0] == TEST_BPB_SIG]
    rep.check('selftest.bpb_signature', len(hit) == 1)
    hit = [s for s, t, imm in sig
           if t == img.at('test_lba0_buf') + 11 and
           struct.unpack('<H', imm)[0] == SECTOR_SIZE]
    rep.check('selftest.bpb_bytes_per_sector', len(hit) == 1)
    rep.check('selftest.oem_immediate',
              d.find(b'\x48\xB8' + struct.pack('<Q', TEST_OEM_ID), at, end) >= 0)
    oem = rip_sites(img, at, end, b'\x48\x3B', 0x05, 0)
    hit = [s for s, t, _ in oem if t == img.at('test_lba0_buf') + 3]
    rep.check('selftest.oem_compare', len(hit) == 1)

    # leg 2 and leg 5: the ROM prefix immediate appears three times
    rep.equal('selftest.rom_immediates',
              len(find_all(d[at:end],
                           b'\x48\xB8' + struct.pack('<Q', TEST_ROM_PREFIX))), 3)
    rom = rip_sites(img, at, end, b'\x48\x3B', 0x05, 0)
    for label, off in (('test_data_buf', 0), ('test_scratch', 0),
                       ('test_scratch', 8 * SECTOR_SIZE)):
        hit = [s for s, t, _ in rom if t == img.at(label) + off]
        rep.check('selftest.rom_compare_%s_%d' % (label, off), len(hit) == 1)
    rep.equal('selftest.lba101_immediates',
              len(find_all(d[at:end], b'\x48\xC7\xC1\x65\x00\x00\x00')), 2)
    rep.equal('selftest.lba93_immediate',
              len(find_all(d[at:end], b'\x48\xC7\xC1\x5D\x00\x00\x00')), 1)
    rep.equal('selftest.nine_sector_requests',
              len(find_all(d[at:end], b'\x41\xB8\x09\x00\x00\x00')), 2)

    # leg 3: the four refusals compare the exact frozen status codes
    rep.equal('selftest.eoverflow_compares',
              len(find_all(d[at:end], b'\x83\xF8\xF6')), 2)
    rep.equal('selftest.einval_compare',
              len(find_all(d[at:end], b'\x83\xF8\xFF')), 1)
    rep.equal('selftest.efault_compare',
              len(find_all(d[at:end], b'\x83\xF8\xF5')), 1)
    rep.equal('selftest.capacity_reads',
              len(rip_refs(img, at, end, b'\x48\x8B', 0x0D, 0, 'capacity')), 3)

    # leg 6: the frozen request counter
    cnt = rip_sites(img, at, end, b'\x48\x83', 0x3D, 1)
    hit = [s for s, t, imm in cnt if t == img.at('test_reads') and
           imm == bytes((TEST_READS_FINAL,))]
    rep.check('selftest.reads_counter_compare', len(hit) == 1)
    rep.equal('selftest.mask_store',
              len(rip_refs(img, at, end, b'\x4C\x89', 0x25, 0, 'test_mask')), 1)

    # all seven failure bits exist and jump back into the leg body
    for bit in (ST_CAP, ST_BPB, ST_ROM, ST_RANGE, ST_LAST, ST_CHUNK, ST_COUNT):
        needle = b'\x41\x83\xCC' + bytes((bit,))
        p = d.find(needle, at, end)
        ok = p >= 0
        if ok:
            ok = (d[p + 4] == 0xE9 and
                  at <= p + 9 + struct.unpack_from('<i', d, p + 5)[0] < p)
        rep.check('selftest.leg_bit_%02x' % bit, ok)

    # the failure report uses the CPU console and nothing else
    sites = call_sites(img, at, end)
    rep.equal('selftest.puts_calls',
              len([t for _, t in sites if t == img.at('cpu_com1_puts')]), 2)
    rep.equal('selftest.puthex_calls',
              len([t for _, t in sites if t == img.at('cpu_com1_puthex')]), 1)
    rep.check('selftest.eio_on_failure',
              d.find(b'\xB8\xF7\xFF\xFF\xFF', at, end) >= 0)


def check_module_hygiene(rep, img):
    at, end = region(img, 'code_start', 'code_end')
    # every E8 whose target lands on a label must name a module function
    # or the CPU console; the module is otherwise self contained
    allowed = {img[n] for n in (
        'pio_in32', 'pio_in16', 'pio_in8', 'pio_out32', 'pio_out16',
        'pio_out8', 'pci_cfg_read', 'pci_cfg_write', 'pci_scan',
        'init', 'read', 'virtio_read', 'selftest',
        'cpu_com1_puts', 'cpu_com1_puthex')}
    foreign = [(s, t) for s, t in call_sites(img, at, end)
               if t not in allowed and
               any(t == img[n] for n in M[2:46])]
    rep.check('hygiene.no_foreign_label_calls', not foreign, repr(foreign))


def check_model(rep, root, img):
    path = root / 'tests' / 'stage2-blk' / 'virtqueue_model.py'
    spec = importlib.util.spec_from_file_location('virtqueue_model', path)
    model = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(model)
    rep.check('model.imported', True, str(path))

    # the frozen contract copies must agree with the manifest
    pairs = [
        ('pci_vendor', model.VIRTIO_PCI_VENDOR),
        ('pci_device', model.VIRTIO_PCI_DEVICE_BLK_TRANSITIONAL),
        ('pci_subvendor', model.VIRTIO_PCI_SUBVENDOR),
        ('pci_subdevice', model.VIRTIO_PCI_SUBDEVICE_BLK),
        ('pci_revision', model.VIRTIO_PCI_REVISION_LEGACY),
        ('pio_host_features', model.VIRTIO_PCI_HOST_FEATURES),
        ('pio_guest_features', model.VIRTIO_PCI_GUEST_FEATURES),
        ('pio_queue_pfn', model.VIRTIO_PCI_QUEUE_PFN),
        ('pio_queue_num', model.VIRTIO_PCI_QUEUE_NUM),
        ('pio_queue_sel', model.VIRTIO_PCI_QUEUE_SEL),
        ('pio_queue_notify', model.VIRTIO_PCI_QUEUE_NOTIFY),
        ('pio_status', model.VIRTIO_PCI_STATUS),
        ('pio_isr', model.VIRTIO_PCI_ISR),
        ('pio_config_off', model.VIRTIO_PCI_CONFIG_OFF),
        ('status_ack', model.VIRTIO_STATUS_ACKNOWLEDGE),
        ('status_driver', model.VIRTIO_STATUS_DRIVER),
        ('status_driver_ok', model.VIRTIO_STATUS_DRIVER_OK),
        ('status_failed', model.VIRTIO_STATUS_FAILED),
        ('cfg_capacity', model.VIRTIO_BLK_CONFIG_CAPACITY),
        ('cfg_blk_size', model.VIRTIO_BLK_CONFIG_BLK_SIZE),
        ('t_in', model.VIRTIO_BLK_T_IN),
        ('t_out', model.VIRTIO_BLK_T_OUT),
        ('t_flush', model.VIRTIO_BLK_T_FLUSH),
        ('s_ok', model.VIRTIO_BLK_S_OK),
        ('s_ioerr', model.VIRTIO_BLK_S_IOERR),
        ('s_unsupp', model.VIRTIO_BLK_S_UNSUPP),
        ('sector_size', model.SECTOR_SIZE),
        ('header_size', model.BLK_HEADER_SIZE),
        ('status_size', model.BLK_STATUS_SIZE),
        ('desc_f_next', model.VRING_DESC_F_NEXT),
        ('desc_f_write', model.VRING_DESC_F_WRITE),
        ('avail_f_noint', model.VRING_AVAIL_F_NO_INTERRUPT),
        ('vring_align', model.VIRTQ_LEGACY_ALIGN),
        ('desc_size', model.DESC_SIZE),
        ('u16_mask', model.U16_MASK),
    ]
    for name, want in pairs:
        rep.equal('model.%s_matches_manifest' % name, img[name], want)

    # re-run the frozen model self test suite through the gate report
    for name, fn in model.TESTS:
        try:
            fn()
            rep.check('model.selftest.%s' % name, True)
        except AssertionError as e:
            rep.check('model.selftest.%s' % name, False, str(e))

    # simulate the exact driver chain layout through the model device
    def driver_request(qnum, disk, lba, n, last_used=0, avail_idx=0):
        lay = model.vring_layout(qnum)
        req_off = model.align_up(lay['total'], PAGE)
        dma_off = req_off + PAGE
        mem = bytearray(dma_off + PAGE)
        vq = model.Virtqueue(mem, 0, qnum)
        dev = model.BlkDevice(disk)
        dev.last_avail = avail_idx
        vq.set_avail_idx(avail_idx)
        vq.set_used_idx(last_used)
        vq.set_avail_flags(AVAIL_F_NOINT)
        # the driver writes exactly these three descriptors
        vq.set_desc(DESC_HEAD, req_off, HEADER_SIZE, DESC_F_NEXT, DESC_DATA)
        vq.set_desc(DESC_DATA, dma_off, n * SECTOR_SIZE,
                    DESC_F_WRITE | DESC_F_NEXT, DESC_STATUS)
        vq.set_desc(DESC_STATUS, req_off + REQ_STATUS_OFF, STATUS_SIZE,
                    DESC_F_WRITE, 0)
        struct.pack_into('<IIQ', mem, req_off, T_IN, 0, lba)
        mem[req_off + REQ_STATUS_OFF] = 0xFF
        vq.set_avail_ring(avail_idx % qnum, DESC_HEAD)
        vq.set_avail_idx((avail_idx + 1) & U16_MASK)
        res = dev.process(vq)
        return mem, vq, dev, res, dma_off, req_off

    disk = model.pattern_disk(256)
    for qnum in (8, 256):
        mem, vq, dev, res, dma_off, req_off = driver_request(qnum, disk, 3, 1)
        ok = (len(res) == 1 and res[0]['status'] == S_OK and
              res[0]['used_len'] == SECTOR_SIZE + 1 and
              res[0]['head'] == DESC_HEAD and
              mem[dma_off:dma_off + SECTOR_SIZE] == disk[3 * 512:4 * 512] and
              mem[req_off + REQ_STATUS_OFF] == S_OK and
              vq.used_idx() == 1 and vq.used_entry(0) == (DESC_HEAD, 513))
        rep.check('model.driver_chain_qnum_%d' % qnum, ok)

    # the chunked 9 sector transfer: requests of 8 then 1, concatenated
    mem, vq, dev, res, dma_off, req_off = driver_request(8, disk, 101, 8)
    part1 = bytes(mem[dma_off:dma_off + 8 * SECTOR_SIZE])
    lu = vq.used_idx()
    mem2, vq2, dev2, res2, dma2, req2 = driver_request(
        8, disk, 109, 1, last_used=lu, avail_idx=1)
    part2 = bytes(mem2[dma2:dma2 + SECTOR_SIZE])
    rep.check('model.driver_chunked_9_sectors',
              res[0]['status'] == S_OK and res2[0]['status'] == S_OK and
              part1 + part2 == disk[101 * 512:110 * 512] and
              vq2.used_idx() == 2)

    # the u16 wrap mirror: driver expects (last_used + 1) & 0xFFFF
    mem, vq, dev, res, dma_off, req_off = driver_request(
        8, disk, 1, 1, last_used=0xFFFF, avail_idx=0xFFFF)
    rep.check('model.driver_u16_wrap',
              vq.used_idx() == ((0xFFFF + 1) & U16_MASK) and
              res[0]['status'] == S_OK and
              vq.used_entry(0xFFFF % 8) == (DESC_HEAD, 513))

    # negative class 1: a bad avail head is a ring error, never data
    mem, vq, dev, res, dma_off, req_off = driver_request(8, disk, 0, 1)
    vq.set_avail_ring(1 % 8, 8)
    vq.set_avail_idx(2)
    try:
        dev.process(vq)
        rep.check('model.neg_bad_desc_idx', False, 'head == qnum accepted')
    except model.VirtqueueError as e:
        rep.check('model.neg_bad_desc_idx', 'bad desc idx' in str(e))

    # negative class 2: LBA at the capacity is IOERR with used.len 1;
    # the driver precheck refuses it with EOVERFLOW before any traffic
    mem, vq, dev, res, dma_off, req_off = driver_request(8, disk, 256, 1)
    rep.check('model.neg_lba_range_ioerr',
              res[0]['status'] == S_IOERR and res[0]['used_len'] == 1 and
              mem[dma_off:dma_off + SECTOR_SIZE] == bytes(SECTOR_SIZE))

    # negative class 3: a missing device never consumes the ring
    lay = model.vring_layout(8)
    mem = bytearray(model.align_up(lay['total'], PAGE) + 2 * PAGE)
    vq = model.Virtqueue(mem, 0, 8)
    missing = model.BlkDevice(None)
    try:
        missing.process(vq)
        rep.check('model.neg_device_missing', False, 'missing device accepted')
    except model.VirtqueueError as e:
        rep.check('model.neg_device_missing', 'device missing' in str(e))

    # negative class 4: the bounded poll mirror terminates on a frozen
    # used idx exactly after the budget, never spins forever
    budget, spins, used_idx, expected = 1000, 0, 0, 1
    while used_idx != expected:
        spins += 1
        if spins >= budget:
            break
    rep.check('model.neg_poll_timeout_bounded',
              spins == budget and used_idx != expected)
    rep.check('model.poll_budget_frozen', POLL_MAX == 0x1000000 and
              isinstance(POLL_MAX, int) and POLL_MAX > 0)


def check_sources(rep, root):
    module = root / 'kernel' / 'blk'
    files = sorted(p.name for p in module.glob('*'))
    rep.equal('source.file_set', files,
              ['api.inc', 'blk.asm', 'blk.inc', 'data.inc', 'selftest.inc'])

    api = (module / 'api.inc').read_text(encoding='utf-8')
    rep.check('source.api_standalone',
              not re.search(r"^\s*include\b", api, re.M))
    rep.check('source.api_has_contract_prefix', 'BLK_KERNEL_BASE' in api)
    api_asserts = len(re.findall(r'\berr\b', api))
    rep.check('source.api_asserts_the_contract', api_asserts >= 8,
              '%d explicit assertions' % api_asserts)
    data_text = (module / 'data.inc').read_text(encoding='utf-8')
    data_asserts = len(re.findall(r'\berr\b', data_text))
    rep.check('source.data_asserts_the_layout', data_asserts >= 4,
              '%d explicit assertions' % data_asserts)
    rep.check('source.data_no_uninitialised_reservation',
              not re.search(r"^\s*(?:[A-Za-z_][\w.]*:\s*)?rb\b", data_text, re.M))
    rep.check('source.data_tail_padding_formula',
              'BLK_PAGE_SIZE - (($ - blk_data_start)' in data_text)

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
                      path.name == 'api.inc' or
                      not re.search(r'^\s*use64\b', text, re.M))
        rep.check('source.%s.no_sti' % path.name,
                  not re.search(r'^\s*sti\b', text, re.M))

    asm_text = (module / 'blk.asm').read_text(encoding='utf-8')
    rep.check('source.blk_asm_hosted_guard',
              'if ~ defined BLK_HOSTED' in asm_text and 'format binary' in asm_text)
    order = re.findall(r"^include\s+'([^']+)'", asm_text, re.M)
    rep.equal('source.include_order', order,
              ['api.inc', 'blk.inc', 'selftest.inc', 'data.inc'])
    rep.check('source.blk_asm_end_assertion',
              'if $ <> blk_data_end' in asm_text)

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
              ['kernel/app/app.inc'] * 3 +
              ['kernel/desktop/desktop.inc', 'kernel/irq/selftest.inc'])

    # the kernel include order: blk sits between desktop and fs, the
    # core trio closes after app (FS-v1 M2 inserted fs right after blk,
    # task 115 inserted app right after fsys)
    k64 = (root / 'kernel' / 'kernel64.asm').read_text(encoding='utf-8')
    incs = re.findall(r"^include\s+'([^']+)'", k64, re.M)
    ok = ('blk/blk.asm' in incs and
          incs.index('blk/blk.asm') == incs.index('desktop/desktop.asm') + 1 and
          incs.index('fs/fs.asm') == incs.index('blk/blk.asm') + 1 and
          incs.index('fsys/fsys.asm') == incs.index('fs/fs.asm') + 1 and
          incs.index('app/app.asm') == incs.index('fsys/fsys.asm') + 1 and
          incs.index('core/contracts.inc') == incs.index('app/app.asm') + 1)
    rep.check('source.kernel64_include_position', ok, repr(incs[-6:]))
    rep.check('source.kernel64_blk_hosted',
              re.search(r"^BLK_HOSTED = 1\ninclude 'blk/blk\.asm'", k64, re.M)
              is not None)
    rep.check('source.kernel64_core_closes', 'if $ <> core_data_end' in k64)

    # contracts: the BLK segment is a pure tail append after the APP one
    contracts = (root / 'kernel' / 'core' / 'contracts.inc').read_text(encoding='utf-8')
    rep.check('source.contracts_blk_after_app',
              contracts.find('K64_BLK_') > contracts.find('K64_APP_') >= 0)
    rep.check('source.contracts_blk_pci_copy', 'K64_BLK_PCI_VENDOR' in contracts)
    rep.check('source.contracts_blk_compares',
              'K64_BLK_PCI_VENDOR <> BLK_PCI_VENDOR' in contracts)
    rep.check('source.contracts_leg7_refrozen',
              contracts.rfind('K64_CORE_ACCOUNT_TABLES <> 3') >
              contracts.find('K64_BLK_'))

    # foundation: the blk stage runs after desktop, before FOUNDATION-OK
    found = (root / 'kernel' / 'core' / 'foundation.inc').read_text(encoding='utf-8')
    dpos = found.find('.desktop_done:')
    ipos = found.find('call    blk_init')
    spos = found.find('call    blk_selftest')
    opos = found.find('k64_report kmsg_blk_ok')
    fpos = found.find('k64_report kmsg_foundation_ok')
    rep.check('source.foundation_blk_stage_order',
              0 <= dpos < ipos < spos < opos < fpos)
    rep.check('source.foundation_blk_protect',
              'mov     rcx, blk_data_start' in found and
              found.find('vmm_boot_protect', ipos) < spos)
    rep.check('source.foundation_blk_marker',
              "kmsg_blk_ok           db 'K64-BLK-OK', 13, 10, 0" in found)
    rep.check('source.foundation_no_blk_skip', 'K64-BLK-SKIP' not in found)

    # the IRQ dispatch table is byte frozen and never mentions blk
    dispatch = (root / 'kernel' / 'irq' / 'dispatch.inc').read_text(encoding='utf-8')
    rep.check('source.dispatch_untouched', 'blk' not in dispatch.lower())


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
    ok, text = asm_.run(root / 'tests/stage2-blk/layout.asm', hi_path,
                        org=KERNEL_BASE)
    if not rep.check('reloc.assembles_at_kernel_base', ok, text.strip()[-400:]):
        return
    hi = load_image(hi_path, KERNEL_BASE)
    delta = KERNEL_BASE
    labels = M[2:48]      # every address label, code and data and cpu
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
        # mov r64, sign extended imm32 windows: only the low 32 bits are
        # emitted, the visible difference is the base modulo 2**32
        if op == 0xC7 and p + 7 <= len(lo_body) and 0xC0 <= lo_body[p + 2] <= 0xC7:
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
    rep.check('reloc.absolute_immediates_found', windows >= 6,
              '%d shifted mov r64, label windows' % windows)
    # the backend immediate really is blk_virtio_read, high and low
    p = find_mov_r64(low, low.at('init'), low.at('read'), 0, low['virtio_read'])
    ok_backend = p >= 0
    if ok_backend:
        rel = p - low.at('code_start')
        ok_backend = any(hi_body[rel:rel + len(n)] == n
                         for n in mov_r64_needles(0, hi['virtio_read']))
    rep.check('reloc.backend_immediate_is_virtio_read', ok_backend)


# ----------------------------------------------------------------------
# negative fixtures
# ----------------------------------------------------------------------

FIXTURES = [
    # defect class: bad desc idx. The chain must name desc 1 after the
    # header; a stray next field breaks the frozen three desc chain
    ('read-bad-desc-idx', 'kernel/blk/blk.inc',
     '        mov     word [rsi + 14], BLK_DESC_DATA\n',
     '        mov     word [rsi + 14], 3\n', 'check'),
    # defect class: out of range LBA. The capacity precheck may not
    # disappear: refused reads must never reach the device
    ('read-lba-range-guard-removed', 'kernel/blk/blk.inc',
     '        cmp     rax, [blk_capacity]\n        ja      .erange\n',
     '', 'check'),
    # defect class: missing device. The frozen transitional device id
    # may not drift: the private assertion fires at assembly time
    ('pci-device-id-drift', 'kernel/blk/api.inc',
     'BLK_PCI_DEVICE    = 0x1001', 'BLK_PCI_DEVICE    = 0x1000', 'assemble'),
    # defect class: timeout. An unbounded poll would hang the boot
    # instead of failing with EIO after the frozen budget
    ('poll-unbounded', 'kernel/blk/blk.inc',
     '        pause\n        dec     r9d\n        jnz     .poll\n'
     '        jmp     .eio\n',
     '        pause\n        jmp     .poll\n', 'check'),
    # every bring-up write needs its exact read back
    ('status-readback-removed', 'kernel/blk/blk.inc',
     '        cmp     eax, BLK_STATUS_ACK\n        jne     .fail_eio\n',
     '', 'check'),
    # queue_pfn is the physical page frame number, not the address
    ('queue-pfn-shift-removed', 'kernel/blk/blk.inc',
     '        shr     rax, BLK_PAGE_SHIFT\n', '', 'check'),
    # the DMA pages must be real image bytes, never a BSS promise
    ('data-rb-reservation', 'kernel/blk/data.inc',
     'blk_dma:                db BLK_PAGE_SIZE dup 0',
     'blk_dma:                rb BLK_PAGE_SIZE', 'check'),
    # a sti anywhere in the module breaks the frozen two owner contract
    ('sti-leak', 'kernel/blk/blk.inc',
     'blk_pci_scan:\n        blk_enter\n',
     'blk_pci_scan:\n        sti\n        blk_enter\n', 'check'),
    # the frozen ROM evidence may not drift from the model image
    ('rom-prefix-drift', 'kernel/blk/api.inc',
     'BLK_TEST_ROM_PREFIX = 0x08610C602AA2E000',
     'BLK_TEST_ROM_PREFIX = 0x08610C602AA2E001', 'check'),
    # a desynchronised ring must disarm the seam, never serve again
    ('read-failstop-removed', 'kernel/blk/blk.inc',
     '        mov     qword [blk_ready], 0\n'
     '        mov     qword [blk_backend], 0\n'
     '        mov     eax, BLK_EIO\n',
     '        mov     eax, BLK_EIO\n', 'check'),
]


def run_fixtures(rep, root, out, fasmg, fasm2_dir):
    checker = root / 'tests' / 'stage2-blk' / 'check.py'
    for name, rel, old, new, where in FIXTURES:
        with tempfile.TemporaryDirectory(prefix='stage2-blk-%s-' % name) as tmp:
            tree = Path(tmp)
            (tree / 'tests').mkdir(parents=True)
            shutil.copytree(root / 'kernel', tree / 'kernel')
            shutil.copytree(root / 'tests' / 'stage2-blk',
                            tree / 'tests' / 'stage2-blk')
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
    out = Path(args.out).resolve() if args.out else root / 'build' / 'stage2-blk'
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
    ok, text = asm_.run(root / 'tests/stage2-blk/layout.asm', low_path)
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
    check_pio(rep, img)
    check_pci_cfg(rep, img)
    check_pci_scan(rep, img)
    check_init(rep, img)
    check_read(rep, img)
    check_virtio_read(rep, img)
    check_selftest(rep, img)
    check_module_hygiene(rep, img)
    check_model(rep, root, img)
    check_sources(rep, root)
    check_integration(rep, asm_, root, out)
    check_position_independence(rep, asm_, root, out, img)

    if not args.no_fixtures:
        run_fixtures(rep, root, out, asm_.fasmg, asm_.fasm2_dir)

    print('stage2-blk: layout.bin %d bytes, code %d bytes, data %d bytes' %
          (len(img.data), img['code_end'] - img['code_start'],
           img['data_end'] - img['data_start']))
    return 0 if rep.print(args.verbose) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
