#!/usr/bin/env python3
# Copyright (C) KolibriOS team 2025-2025. All rights reserved.
# Distributed under terms of the GNU General Public License v2
#
# Host model tests for the stage 2 memory management module, task 28.
#
# The module itself cannot be executed here: it needs a CPUID that reports
# a physical width, CR3, EFER and real physical memory. The tests therefore
# work on two things that can be checked without running a machine:
#
#  1. the real assembler output. layout.asm is assembled twice by this
#     script, once at 0 and once at the kernel base, and the image is
#     inspected: contract constants, record offsets, entry prologues, the
#     zeroed data block, the end of the image;
#  2. a host model of the documented algorithms. The model reads the page
#     tables, the BootInfo records and the memory maps out of the very
#     bytes the assembler emitted, and its answers are compared against
#     the expectations that layout.asm stores next to each fixture.
#
# The fixture expectations are written by hand in layout.asm, so a model
# that drifts away from the contract fails the comparison instead of
# agreeing with itself.
#
# Usage:
#   python3 tests/stage2-mm/check.py [--keep] [--verbose]
#
# Environment:
#   FASMG     fasm2 host executable (default: ../fasm2 bootstrap build)
#   FASM2_DIR fasm2 checkout, used for the include directory
#
# No QEMU, no image and no repository file other than build/stage2-b/ is
# touched.

import ctypes
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / 'tests' / 'stage2-mm'
BUILD = ROOT / 'build' / 'stage2-b'

# ------------------------------------------------------------------
# the handover contract, a private copy for the tests
# ------------------------------------------------------------------

PAGE_SHIFT = 12
PAGE_SIZE = 0x1000
PHYS_LIMIT = 0x100000000
PFN_COUNT = PHYS_LIMIT // PAGE_SIZE
BITMAP_BYTES = PFN_COUNT // 8
KERNEL_BASE = 0xFFFFFFFF80000000
WINDOW_BASE = 0xFFFFC00000000000
WINDOW_LIMIT = 0xFFFFC20000000000
WINDOW_SIZE = WINDOW_LIMIT - WINDOW_BASE
HEAP_BASE = WINDOW_BASE
HEAP_LIMIT = 0xFFFFC00001000000
HEAP_MAX = HEAP_LIMIT - HEAP_BASE
HEAP_INITIAL = 0x10000
PHYS_BITS = 52
PA_MASK = ((1 << PHYS_BITS) - 1) & ~(PAGE_SIZE - 1)
MASK64 = 0xFFFFFFFFFFFFFFFF

PTE_P = 0x001
PTE_RW = 0x002
PTE_US = 0x004
PTE_PWT = 0x008
PTE_PCD = 0x010
PTE_PS = 0x080
PTE_G = 0x100
PTE_PAT = 0x1000
PTE_NX = 0x8000000000000000

MAP_RW = PTE_RW
MAP_NX = PTE_NX
MAP_ALLOWED = MAP_RW | MAP_NX

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

VQ_PRESENT = 1
VQ_RW = 2
VQ_US = 4
VQ_NX = PTE_NX

EFI_CONVENTIONAL = 7
EFI_MEMORY_WB = 0x8
EFI_MEMORY_WP = 0x1000
EFI_MEMORY_RP = 0x2000
EFI_MEMORY_NV = 0x8000
EFI_MEMORY_RO = 0x20000
EFI_MEMORY_RUNTIME = 0x8000000000000000
EFI_MEMORY_RESERVED = (EFI_MEMORY_RUNTIME | EFI_MEMORY_RP |
                       EFI_MEMORY_RO | EFI_MEMORY_WP | EFI_MEMORY_NV)

EFI_DESC_MIN = 40
MAP_MAX_SIZE = 0x40000
MAP_MAX_DESCS = 6553
IMAGE_MAX = 0x2000000
DESC_RECORD = 16
CR3_PIN_MAX = 64
TABLE_WALK_MAX = 64

BOOTINFO_SIZE = 208
INIT_PARAMS_SIZE = 64
PMM_STATE_SIZE = 64
VMM_STATE_SIZE = 64
HEAP_STATE_SIZE = 48
BOOT_STATE_SIZE = 128
PMM_STATS_SIZE = 48
VMM_QUERY_SIZE = 40
HEAP_STATS_SIZE = 32

# record offsets, written out again on purpose: the manifest is compared
# against these values, so a silent change of the layout fails the test
EXPECT_OFFSETS = {
    'MM_BOOTINFO.signature': 0,
    'MM_BOOTINFO.version': 8,
    'MM_BOOTINFO.struct_size': 12,
    'MM_BOOTINFO.flags': 16,
    'MM_BOOTINFO.memmap_addr': 24,
    'MM_BOOTINFO.memmap_size': 32,
    'MM_BOOTINFO.memmap_desc_size': 40,
    'MM_BOOTINFO.memmap_desc_version': 44,
    'MM_BOOTINFO.memmap_desc_count': 48,
    'MM_BOOTINFO.kernel_phys_start': 56,
    'MM_BOOTINFO.kernel_phys_end': 64,
    'MM_BOOTINFO.stack_phys_start': 72,
    'MM_BOOTINFO.stack_phys_end': 80,
    'MM_BOOTINFO.gop_framebuffer_addr': 88,
    'MM_BOOTINFO.rsdp_addr': 120,
    'MM_BOOTINFO.initramfs_addr': 128,
    'MM_BOOTINFO.initramfs_size': 136,
    'MM_BOOTINFO.reserved': 144,
    'MM_MEM_DESC.type': 0,
    'MM_MEM_DESC.phys_start': 8,
    'MM_MEM_DESC.virt_start': 16,
    'MM_MEM_DESC.pages': 24,
    'MM_MEM_DESC.attributes': 32,
    'MM_INIT_PARAMS.version': 0,
    'MM_INIT_PARAMS.size': 4,
    'MM_INIT_PARAMS.bootinfo_va': 8,
    'MM_INIT_PARAMS.bootinfo_phys': 16,
    'MM_INIT_PARAMS.cr3_phys': 24,
    'MM_INIT_PARAMS.stack_top_va': 32,
    'MM_INIT_PARAMS.flags': 40,
    'MM_INIT_PARAMS.reserved1': 48,
    'MM_INIT_PARAMS.reserved2': 56,
    'MM_PMM_STATE.ready': 0,
    'MM_PMM_STATE.managed_limit': 8,
    'MM_PMM_STATE.total_pages': 16,
    'MM_PMM_STATE.free_pages': 24,
    'MM_PMM_STATE.allocated_pages': 32,
    'MM_PMM_STATE.reserved_pages': 40,
    'MM_PMM_STATE.next_pfn': 48,
    'MM_PMM_STATE.root_phys': 56,
    'MM_VMM_STATE.ready': 0,
    'MM_VMM_STATE.phys_bits': 4,
    'MM_VMM_STATE.root_phys': 8,
    'MM_VMM_STATE.pa_mask': 16,
    'MM_VMM_STATE.window_base': 24,
    'MM_VMM_STATE.window_limit': 32,
    'MM_VMM_STATE.table_allocs': 40,
    'MM_VMM_STATE.table_drops': 48,
    'MM_HEAP_STATE.ready': 0,
    'MM_HEAP_STATE.base': 8,
    'MM_HEAP_STATE.brk': 16,
    'MM_HEAP_STATE.mapped_end': 24,
    'MM_HEAP_STATE.limit': 32,
    'MM_HEAP_STATE.reserved': 40,
    'MM_BOOT_STATE.bootinfo_phys': 0,
    'MM_BOOT_STATE.cr3_phys': 8,
    'MM_BOOT_STATE.memmap_phys': 16,
    'MM_BOOT_STATE.memmap_size': 24,
    'MM_BOOT_STATE.kernel_start': 32,
    'MM_BOOT_STATE.kernel_end': 40,
    'MM_BOOT_STATE.stack_start': 48,
    'MM_BOOT_STATE.stack_end': 56,
    'MM_BOOT_STATE.stack_top_va': 64,
    'MM_BOOT_STATE.desc_stride': 72,
    'MM_BOOT_STATE.desc_count': 76,
    'MM_PMM_STATS.managed_limit': 0,
    'MM_PMM_STATS.total': 8,
    'MM_PMM_STATS.free': 16,
    'MM_PMM_STATS.allocated': 24,
    'MM_PMM_STATS.reserved': 32,
    'MM_PMM_STATS.next_pfn': 40,
    'MM_VMM_QUERY.physical_address': 0,
    'MM_VMM_QUERY.leaf_entry_va': 8,
    'MM_VMM_QUERY.raw_leaf': 16,
    'MM_VMM_QUERY.page_size': 24,
    'MM_VMM_QUERY.effective_flags': 32,
    'MM_HEAP_STATS.base': 0,
    'MM_HEAP_STATS.brk': 8,
    'MM_HEAP_STATS.mapped_end': 16,
    'MM_HEAP_STATS.limit': 24,
}

EXPECT_CONSTANTS = {
    'MM_PAGE_SHIFT': 12,
    'MM_PAGE_SIZE': 0x1000,
    'MM_PHYS_LIMIT': 0x100000000,
    'MM_PFN_COUNT': 0x100000,
    'MM_BITMAP_BYTES': 0x20000,
    'MM_KERNEL_BASE': 0xFFFFFFFF80000000,
    'MM_WINDOW_BASE': 0xFFFFC00000000000,
    'MM_WINDOW_LIMIT': 0xFFFFC20000000000,
    'MM_WINDOW_SIZE': 0x20000000000,
    'MM_HEAP_BASE': 0xFFFFC00000000000,
    'MM_HEAP_LIMIT': 0xFFFFC00001000000,
    'MM_HEAP_MAX': 0x1000000,
    'MM_HEAP_INITIAL': 0x10000,
    'MM_PTE_NX': 0x8000000000000000,
    'MM_PTE_ADDR_MASK': 0x000FFFFFFFFFF000,
    'MM_MAP_ALLOWED': 0x8000000000000002,
    'MM_BOOTINFO_SIZE': 208,
    'MM_INIT_PARAMS_size': 64,
    'MM_EFI_DESC_MIN_SIZE': 40,
    'MM_MAP_MAX_SIZE': 0x40000,
    'MM_MAP_MAX_DESCS': 6553,
    'MM_IMAGE_MAX': 0x2000000,
    'MM_CR3_PIN_MAX': 64,
    'MM_TABLE_WALK_MAX': 64,
    'MM_OK': 0,
    'MM_EINVAL': -1,
    'MM_ENOMEM': -2,
    'MM_ENOENT': -3,
    'MM_EEXIST': -4,
    'MM_EPERM': -5,
    'MM_ENOTSUP': -6,
    'MM_ENOTREADY': -7,
    'MM_EALREADY': -8,
    'MM_EIO': -9,
    'MM_EOVERFLOW': -10,
    'MM_EFAULT': -11,
    'MM_EBUSY': -12,
}

EXPECT_QUERY_FLAGS = {
    'MM_VQ_PRESENT': 1,
    'MM_VQ_RW': 2,
    'MM_VQ_US': 4,
    'MM_VQ_NX': PTE_NX,
}

# manifest order, identical to the list in layout.asm
MANIFEST_NAMES = """
magic version org
data_start data_end bitmap_eligible bitmap_allocated bitmap_pinned
desc_scratch table_seen table_seen_count pmm_state vmm_state heap_state
heap_allocs heap_grows
boot_state test_fail test_fail_hits test_selftest_mask pmm_stats_scratch
heap_stats_scratch selftest_query test_pages
pmm_init pmm_alloc_page pmm_free_page pmm_get_stats pmm_selftest
vmm_init vmm_query vmm_map_page vmm_unmap_page vmm_boot_protect vmm_selftest
heap_init heap_alloc heap_free heap_get_stats heap_selftest mm_selftest
mm_params_validate mm_cpu_phys_bits mm_desc_is_eligible mm_pin_range
mm_pin_table_page mm_pin_tables mm_memmap_validate mm_memmap_mark
mm_bootmap_build mm_bitmap_clear mm_bitmap_fill mm_bitmap_count mm_pfn_zero
mm_pmm_hook mm_pfn_next_free mm_pfn_alloc mm_pmm_alloc_table
mm_pmm_drop_table mm_pmm_recount mm_vmm_resolve mm_vmm_protect_pass mm_heap_grow
MM_PAGE_SHIFT MM_PAGE_SIZE MM_PHYS_LIMIT MM_PFN_COUNT MM_BITMAP_BYTES
MM_KERNEL_BASE MM_WINDOW_BASE MM_WINDOW_LIMIT MM_WINDOW_SIZE MM_HEAP_BASE
MM_HEAP_LIMIT MM_HEAP_MAX MM_HEAP_INITIAL MM_PTE_NX MM_PTE_ADDR_MASK
MM_MAP_ALLOWED MM_BOOTINFO_SIZE MM_INIT_PARAMS_size MM_EFI_DESC_MIN_SIZE
MM_MAP_MAX_SIZE MM_MAP_MAX_DESCS MM_IMAGE_MAX MM_CR3_PIN_MAX MM_TABLE_WALK_MAX
MM_OK MM_EINVAL MM_ENOMEM MM_ENOENT MM_EEXIST MM_EPERM MM_ENOTSUP
MM_ENOTREADY MM_EALREADY MM_EIO MM_EOVERFLOW MM_EFAULT MM_EBUSY
MM_BOOTINFO.signature MM_BOOTINFO.version MM_BOOTINFO.struct_size
MM_BOOTINFO.flags MM_BOOTINFO.memmap_addr MM_BOOTINFO.memmap_size
MM_BOOTINFO.memmap_desc_size MM_BOOTINFO.memmap_desc_version
MM_BOOTINFO.memmap_desc_count MM_BOOTINFO.kernel_phys_start
MM_BOOTINFO.kernel_phys_end MM_BOOTINFO.stack_phys_start
MM_BOOTINFO.stack_phys_end MM_BOOTINFO.gop_framebuffer_addr
MM_BOOTINFO.rsdp_addr MM_BOOTINFO.initramfs_addr MM_BOOTINFO.initramfs_size
MM_BOOTINFO.reserved MM_MEM_DESC.type MM_MEM_DESC.phys_start
MM_MEM_DESC.virt_start MM_MEM_DESC.pages MM_MEM_DESC.attributes
MM_INIT_PARAMS.version MM_INIT_PARAMS.size MM_INIT_PARAMS.bootinfo_va
MM_INIT_PARAMS.bootinfo_phys MM_INIT_PARAMS.cr3_phys
MM_INIT_PARAMS.stack_top_va MM_INIT_PARAMS.flags MM_INIT_PARAMS.reserved1
MM_INIT_PARAMS.reserved2 MM_PMM_STATE.ready MM_PMM_STATE.managed_limit
MM_PMM_STATE.total_pages MM_PMM_STATE.free_pages MM_PMM_STATE.allocated_pages
MM_PMM_STATE.reserved_pages MM_PMM_STATE.next_pfn MM_PMM_STATE.root_phys
MM_VMM_STATE.ready
MM_VMM_STATE.phys_bits MM_VMM_STATE.root_phys MM_VMM_STATE.pa_mask
MM_VMM_STATE.window_base MM_VMM_STATE.window_limit
MM_VMM_STATE.table_allocs MM_VMM_STATE.table_drops MM_HEAP_STATE.ready
MM_HEAP_STATE.base MM_HEAP_STATE.brk MM_HEAP_STATE.mapped_end
MM_HEAP_STATE.limit MM_HEAP_STATE.reserved
MM_BOOT_STATE.bootinfo_phys MM_BOOT_STATE.cr3_phys
MM_BOOT_STATE.memmap_phys MM_BOOT_STATE.memmap_size MM_BOOT_STATE.kernel_start
MM_BOOT_STATE.kernel_end MM_BOOT_STATE.stack_start MM_BOOT_STATE.stack_end
MM_BOOT_STATE.stack_top_va MM_BOOT_STATE.desc_stride MM_BOOT_STATE.desc_count
MM_PMM_STATS.managed_limit MM_PMM_STATS.total MM_PMM_STATS.free
MM_PMM_STATS.allocated MM_PMM_STATS.reserved MM_PMM_STATS.next_pfn
MM_VMM_QUERY.physical_address MM_VMM_QUERY.leaf_entry_va
MM_VMM_QUERY.raw_leaf MM_VMM_QUERY.page_size MM_VMM_QUERY.effective_flags
MM_HEAP_STATS.base MM_HEAP_STATS.brk MM_HEAP_STATS.mapped_end
MM_HEAP_STATS.limit
MM_VQ_PRESENT MM_VQ_RW MM_VQ_US MM_VQ_NX
fx_pml4 fx_pdpt fx_pd fx_pt fx2_base fx_ps_pml4 fx_4g_pml4 fx_many_base
fx_high_boot fx_high_map fx_count fx_table qry_count qry_table map_count
map_table unmap_count unmap_table ip_stride40 ip_high ip_badver
kern_pml4 kern_pt
""".split()

PUBLIC_ENTRIES = [
    'pmm_init', 'pmm_alloc_page', 'pmm_free_page', 'pmm_get_stats',
    'pmm_selftest',
    'vmm_init', 'vmm_query', 'vmm_map_page', 'vmm_unmap_page',
    'vmm_boot_protect',
    'vmm_selftest', 'heap_init', 'heap_alloc', 'heap_free', 'heap_get_stats',
    'heap_selftest', 'mm_selftest',
]

INTERNAL_HELPERS = [
    'mm_params_validate', 'mm_cpu_phys_bits', 'mm_desc_is_eligible',
    'mm_pin_range', 'mm_pin_table_page', 'mm_pin_tables',
    'mm_memmap_validate', 'mm_memmap_mark', 'mm_bootmap_build',
    'mm_bitmap_clear', 'mm_bitmap_fill', 'mm_bitmap_count', 'mm_pfn_zero',
    'mm_pmm_hook',
    'mm_pfn_next_free', 'mm_pfn_alloc', 'mm_pmm_alloc_table',
    'mm_pmm_drop_table', 'mm_pmm_recount', 'mm_vmm_resolve',
    'mm_vmm_protect_pass',
    'mm_heap_grow',
]

# prologue bytes of every public entry: the callee saved frame of mm_enter,
# or the leading cld of the entries that save registers by hand
FRAME = bytes([0x55, 0x53, 0x56, 0x57])
PROLOGUES = {
    'pmm_free_page': bytes([0xFC]),
    'heap_free': bytes([0xFC]), 'heap_get_stats': bytes([0xFC]),
}


def frame_prologue(entry):
    return PROLOGUES.get(entry, FRAME)


# ------------------------------------------------------------------
# a sparse model of physical memory
# ------------------------------------------------------------------
# The image occupies the low part of the space and is the only memory the
# assembler produced. Everything the model allocates later (page table
# pages, heap data pages) lives in zero filled pages it creates on demand.

class Mem:
    def __init__(self, image):
        self.image = bytearray(image)
        self.pages = {}

    def read_bytes(self, addr, size):
        if addr < 0:
            raise ValueError('negative address')
        out = bytearray(size)
        pos = 0
        while pos < size:
            a = addr + pos
            pfn, off = a >> PAGE_SHIFT, a & (PAGE_SIZE - 1)
            n = min(size - pos, PAGE_SIZE - off)
            start = pfn << PAGE_SHIFT
            if start + PAGE_SIZE <= len(self.image):
                out[pos:pos + n] = self.image[start + off:start + off + n]
            else:
                blk = self.pages.get(pfn)
                if blk is not None:
                    out[pos:pos + n] = blk[off:off + n]
            pos += n
        return bytes(out)

    def write_bytes(self, addr, data):
        pos = 0
        while pos < len(data):
            a = addr + pos
            pfn, off = a >> PAGE_SHIFT, a & (PAGE_SIZE - 1)
            n = min(len(data) - pos, PAGE_SIZE - off)
            start = pfn << PAGE_SHIFT
            if start + PAGE_SIZE <= len(self.image):
                self.image[start + off:start + off + n] = data[pos:pos + n]
            else:
                blk = self.pages.setdefault(pfn, bytearray(PAGE_SIZE))
                blk[off:off + n] = data[pos:pos + n]
            pos += n

    def q(self, addr):
        return struct.unpack('<Q', self.read_bytes(addr, 8))[0]

    def d(self, addr):
        return struct.unpack('<I', self.read_bytes(addr, 4))[0]

    def sq(self, addr):
        v = self.q(addr)
        return v - (1 << 64) if v & (1 << 63) else v

    def setq(self, addr, value):
        self.write_bytes(addr, struct.pack('<Q', value & MASK64))

    def page_zeroed(self, addr):
        return self.read_bytes(addr, PAGE_SIZE) == bytes(PAGE_SIZE)

    def zero_page(self, addr):
        self.write_bytes(addr, bytes(PAGE_SIZE))

    def snapshot(self):
        return (bytes(self.image), {k: bytes(v) for k, v in self.pages.items()})

    def restore(self, snap):
        self.image = bytearray(snap[0])
        self.pages = {k: bytearray(v) for k, v in snap[1].items()}

# ------------------------------------------------------------------
# assembler driver
# ------------------------------------------------------------------

def fasm2_dir():
    env = os.environ.get('FASM2_DIR')
    return Path(env).resolve() if env else ROOT.parent / 'fasm2'


def find_host():
    env = os.environ.get('FASMG')
    if env:
        path = Path(env)
        if not path.is_file():
            raise SystemExit('FASMG does not name a file: %s' % env)
        return str(path)
    host = fasm2_dir() / 'build' / 'fasm2-bootstrap' / 'fasmg-macos-x64'
    if host.is_file():
        return str(host)
    for name in ('fasm2', 'fasmg'):
        found = shutil.which(name)
        if found:
            return found
    raise SystemExit('no fasm2 host found, set FASMG to the executable')


def assemble(org, tag):
    """Assemble the probe at one link base and return the image bytes."""
    BUILD.mkdir(parents=True, exist_ok=True)
    driver = BUILD / ('mmprobe-%s.asm' % tag)
    output = BUILD / ('mmprobe-%s.bin' % tag)
    driver.write_text(
        '; generated by tests/stage2-mm/check.py\n'
        'MM_PROBE_ORG = 0x%X\n'
        'include "../../tests/stage2-mm/layout.asm"\n' % (org & MASK64))
    env = dict(os.environ)
    env['INCLUDE'] = str(fasm2_dir() / 'include')
    if output.exists():
        output.unlink()
    cmd = [find_host(), '-e100', "-iInclude 'fasm2.inc'",
           str(driver), str(output)]
    result = subprocess.run(cmd, env=env, cwd=str(ROOT),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    text = result.stdout.decode('utf-8', 'replace')
    if result.returncode != 0 or not output.is_file():
        raise SystemExit('the probe did not assemble at 0x%X\n%s'
                         % (org & MASK64, text))
    return output.read_bytes(), text


# the compact manifest of the execution probe, in the order execprobe.asm
# emits it; only the symbols the in-process scenarios touch are listed
EXEC_MANIFEST_NAMES = """
magic data_start bitmap_eligible bitmap_allocated bitmap_pinned desc_scratch
pmm_state vmm_state heap_state boot_state test_fail test_fail_hits
pmm_stats_scratch heap_stats_scratch selftest_query
mm_pmm_hook mm_bitmap_fill mm_bitmap_clear mm_bitmap_count
mm_pfn_next_free mm_pfn_alloc pmm_free_page pmm_get_stats mm_pmm_drop_table
heap_get_stats heap_free heap_alloc vmm_query vmm_map_page vmm_unmap_page
vmm_boot_protect
""".split()


def load_exec_manifest(img):
    man = {}
    for index, name in enumerate(EXEC_MANIFEST_NAMES):
        man[name] = struct.unpack_from('<Q', img, index * 8)[0]
    return man


def assemble_exec():
    """Assemble the execution probe at org 0; its RIP-relative references
    are position independent, so the bytes run unchanged at the arena."""
    BUILD.mkdir(parents=True, exist_ok=True)
    driver = BUILD / 'mmprobe-exec.asm'
    output = BUILD / 'mmprobe-exec.bin'
    driver.write_text(
        '; generated by tests/stage2-mm/check.py\n'
        'MM_EXEC_ORG = 0\n'
        'include "../../tests/stage2-mm/execprobe.asm"\n')
    env = dict(os.environ)
    env['INCLUDE'] = str(fasm2_dir() / 'include')
    if output.exists():
        output.unlink()
    cmd = [find_host(), '-e100', "-iInclude 'fasm2.inc'",
           str(driver), str(output)]
    result = subprocess.run(cmd, env=env, cwd=str(ROOT),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    text = result.stdout.decode('utf-8', 'replace')
    if result.returncode != 0 or not output.is_file():
        raise SystemExit('the execution probe did not assemble\n%s' % text)
    return output.read_bytes()


# ------------------------------------------------------------------
# result bookkeeping
# ------------------------------------------------------------------

class Checker:
    def __init__(self):
        self.failures = []
        self.checks = 0
        self.section = ''

    def start(self, name):
        self.section = name
        print('  [%s]' % name)

    def ok(self, condition, what):
        self.checks += 1
        if not condition:
            self.failures.append('%s: %s' % (self.section, what))
        return condition

    def eq(self, got, want, what):
        if isinstance(got, (bytes, bytearray)) or isinstance(want, (bytes, bytearray)):
            # a whole image compared byte by byte: report a summary, never the
            # two megabyte blobs, or the log becomes unreadable
            got, want = bytes(got), bytes(want)
            if got == want:
                return self.ok(True, what)
            span = min(len(got), len(want))
            first = next((i for i in range(span) if got[i] != want[i]), span)
            differ = sum(1 for a, b in zip(got, want) if a != b)
            return self.ok(False, '%s: %d bytes differ, first at %#x (%#x != %#x)'
                           % (what, differ + abs(len(got) - len(want)), first,
                              got[first] if first < len(got) else -1,
                              want[first] if first < len(want) else -1))
        return self.ok(got == want,
                       '%s: expected %r, got %r' % (what, want, got))

    def report(self):
        for line in self.failures:
            print('  FAIL %s' % line)
        print('%d checks, %d failed' % (self.checks, len(self.failures)))
        return not self.failures


# ------------------------------------------------------------------
# manifest
# ------------------------------------------------------------------

def load_manifest(img):
    """Read the manifest, which always starts the image file."""
    man = {}
    for index, name in enumerate(MANIFEST_NAMES):
        man[name] = struct.unpack_from('<Q', img, index * 8)[0]
    return man


def manifest_slots(img):
    """How many qwords the file can hold, to catch a truncated manifest."""
    return len(img) // 8


def signed(value):
    return value - (1 << 64) if value & (1 << 63) else value


# ------------------------------------------------------------------
# section 1: image shape, contract constants, entry points
# ------------------------------------------------------------------

def check_image(chk, man, img, org, label):
    chk.start('image %s' % label)
    chk.eq(bytes(struct.pack('<Q', man['magic'])), b'K64MMP01',
           'manifest magic')
    chk.eq(man['version'], 1, 'manifest version')
    chk.eq(man['org'], org, 'manifest org')

    data_start, data_end = man['data_start'], man['data_end']
    chk.eq(data_start % PAGE_SIZE, 0, 'data block start is page aligned')
    chk.eq(data_end % PAGE_SIZE, 0, 'data block end is page aligned')
    chk.eq(data_end - org, len(img), 'the image ends exactly on mm_data_end')
    chk.ok(data_end - data_start >= 3 * BITMAP_BYTES + MAP_MAX_DESCS * DESC_RECORD,
           'the data block holds the bitmaps and the scratch area')

    # the data block is emitted as zero bytes, nothing in it is initialised
    block = img[data_start - org:data_end - org]
    chk.ok(block == bytes(len(block)), 'the whole data block is zero filled')

    # the three bitmaps are contiguous and cover the low 4 GiB
    chk.eq(man['bitmap_eligible'], data_start, 'eligible bitmap address')
    chk.eq(man['bitmap_allocated'], data_start + BITMAP_BYTES,
           'allocated bitmap address')
    chk.eq(man['bitmap_pinned'], data_start + 2 * BITMAP_BYTES,
           'pinned bitmap address')
    chk.eq(man['desc_scratch'], data_start + 3 * BITMAP_BYTES,
           'descriptor scratch address')
    chk.eq(man['table_seen'], man['desc_scratch'] + MAP_MAX_DESCS * DESC_RECORD,
           'table scratch follows the descriptor scratch')
    chk.eq(man['table_seen'] + CR3_PIN_MAX * 8, man['table_seen_count'],
           'the table scratch holds exactly one entry per pinnable page')

    # state blocks are adjacent, page internal and have the documented size
    chk.eq(man['vmm_state'] - man['pmm_state'], PMM_STATE_SIZE,
           'PMM state size')
    chk.eq(man['heap_state'] - man['vmm_state'], VMM_STATE_SIZE,
           'VMM state size')
    chk.eq(man['heap_allocs'] - man['heap_state'], HEAP_STATE_SIZE,
           'heap state size')
    chk.eq(man['heap_grows'] - man['heap_allocs'], 8,
           'the private allocation counter follows the heap state')
    chk.eq(man['boot_state'] - man['heap_grows'], 8,
           'the private growth counter follows the allocation counter')
    chk.eq(man['test_fail'] - man['boot_state'], BOOT_STATE_SIZE,
           'boot state size')
    chk.eq(man['pmm_stats_scratch'] - man['test_selftest_mask'], 8,
           'the test hooks are three words and one mask')
    chk.eq(man['heap_stats_scratch'] - man['pmm_stats_scratch'],
           PMM_STATS_SIZE, 'PMM statistics size')
    chk.eq(man['selftest_query'] - man['heap_stats_scratch'],
           HEAP_STATS_SIZE, 'heap statistics size')
    chk.eq(man['test_pages'] - man['selftest_query'], VMM_QUERY_SIZE,
           'query record size')
    for name in ('table_seen', 'table_seen_count', 'pmm_state', 'vmm_state',
                 'heap_state', 'heap_allocs', 'heap_grows', 'boot_state',
                 'test_fail', 'test_fail_hits',
                 'test_selftest_mask', 'pmm_stats_scratch',
                 'heap_stats_scratch', 'selftest_query', 'test_pages'):
        chk.ok(data_start <= man[name] < data_end,
               '%s lives inside the data block' % name)
    chk.ok(man['test_pages'] + 32 <= data_end,
           'the four test pages fit into the data block')

    # every entry point is code, before the data block, and starts with the
    # documented prologue so a misplaced label is caught
    code = {}
    for entry in PUBLIC_ENTRIES + INTERNAL_HELPERS:
        addr = man[entry]
        chk.ok(org <= addr < data_start, '%s is inside the code' % entry)
        code[entry] = addr
    for entry in PUBLIC_ENTRIES:
        addr = man[entry]
        want = frame_prologue(entry)
        got = img[addr - org:addr - org + len(want)]
        chk.eq(got, want, '%s starts with its documented prologue' % entry)
    chk.eq(len(set(code.values())), len(code),
           'no two entry points share an address')
    chk.ok(man['mm_selftest'] > man['heap_selftest'],
           'the aggregate selftest is emitted after the module')


def check_contract(chk, man):
    chk.start('contract')
    for name, want in EXPECT_CONSTANTS.items():
        # the manifest is read unsigned, the table may hold negative codes
        chk.eq(man[name] & MASK64, want & MASK64, 'constant %s' % name)
    for name, want in EXPECT_QUERY_FLAGS.items():
        chk.eq(man[name], want, 'query flag %s' % name)
    for name, want in EXPECT_OFFSETS.items():
        chk.eq(man[name], want, 'record offset %s' % name)


# ------------------------------------------------------------------
# section 2: BootInfo parsing, the page maps and the pin sets
# ------------------------------------------------------------------

class State:
    """What pmm_init builds: the two maps, the state block and the tables.

    The pinned bitmap starts as all ones once the parameters validated;
    marking conventional memory clears the pin of every eligible page and
    the reservations set it again. The model therefore keeps the explicit
    pin set only and derives the effective pin of a frame from it: a frame
    is pinned when it is not eligible or when a reservation named it.
    """

    def __init__(self):
        self.eligible = set()
        self.pinned = set()
        self.filled = False
        self.table_seen = []
        self.boot = {}
        self.params = {}

    def is_pinned(self, pfn):
        return self.filled and (pfn not in self.eligible
                                or pfn in self.pinned)

    def pinned_count(self):
        if not self.filled:
            return 0
        return PFN_COUNT - len(self.eligible - self.pinned)


def mm_pin_range(state, start, end):
    """Mirror of mm_pin_range: every page the byte range touches."""
    end = min(end, PHYS_LIMIT)
    if start >= end:
        return
    first = start >> PAGE_SHIFT
    last = min((end + PAGE_SIZE - 1) >> PAGE_SHIFT, PFN_COUNT)
    for pfn in range(first, last):
        state.pinned.add(pfn)


def mm_pin_table_page(chk, mem, state, paddr):
    """Mirror of mm_pin_table_page, plus the model's own read check."""
    if paddr >= PHYS_LIMIT or paddr & (PAGE_SIZE - 1):
        return ENOTSUP
    if paddr in state.table_seen:
        return EALREADY
    if len(state.table_seen) >= CR3_PIN_MAX:
        return ENOTSUP
    state.table_seen.append(paddr)
    state.pinned.add(paddr >> PAGE_SHIFT)
    return OK


def mm_pin_tables(chk, mem, state, cr3):
    status = mm_pin_table_page(chk, mem, state, cr3)
    if status != OK:
        return status
    pa_mask = PA_MASK
    for i4 in range(512):
        entry = mem.q(cr3 + i4 * 8)
        if not entry & PTE_P:
            continue
        if entry & PTE_PS:
            return EINVAL
        pdpt = entry & pa_mask
        status = mm_pin_table_page(chk, mem, state, pdpt)
        if status == EALREADY:
            continue
        if status != OK:
            return status
        for i3 in range(512):
            e3 = mem.q(pdpt + i3 * 8)
            if not e3 & PTE_P or e3 & PTE_PS:
                continue
            pd = e3 & pa_mask
            status = mm_pin_table_page(chk, mem, state, pd)
            if status == EALREADY:
                continue
            if status != OK:
                return status
            for i2 in range(512):
                e2 = mem.q(pd + i2 * 8)
                if not e2 & PTE_P or e2 & PTE_PS:
                    continue
                status = mm_pin_table_page(chk, mem, state, e2 & pa_mask)
                if status == EALREADY:
                    continue
                if status != OK:
                    return status
    return OK


def mm_params_validate(mem, ip):
    if ip == 0:
        return EINVAL
    ip &= MASK64
    if mem.d(ip) != 1 or mem.d(ip + 4) != INIT_PARAMS_SIZE:
        return EINVAL
    if mem.q(ip + 40):
        return EINVAL
    if mem.q(ip + 48) or mem.q(ip + 56):
        return EINVAL
    cr3 = mem.q(ip + 24)
    if cr3 & (PAGE_SIZE - 1) or cr3 == 0 or cr3 >= PHYS_LIMIT:
        return EINVAL
    va, phys = mem.q(ip + 8), mem.q(ip + 16)
    if va != phys or va == 0 or va & 7:
        return EINVAL
    if va + BOOTINFO_SIZE > PHYS_LIMIT:
        return EINVAL
    top = mem.q(ip + 32)
    if top == 0 or top & 15 or top < KERNEL_BASE:
        return EINVAL
    return OK


def mm_bootmap_build(chk, mem, state, ip):
    status = mm_params_validate(mem, ip)
    if status != OK:
        return status
    # pmm_init fills the pinned bitmap with ones right after the parameter
    # check and before the BootInfo parser runs
    state.filled = True
    ip &= MASK64
    boot_phys = mem.q(ip + 16)
    state.params = {'cr3': mem.q(ip + 24),
                    'stack_top': mem.q(ip + 32),
                    'boot': boot_phys}
    b = boot_phys
    if mem.q(b) != int.from_bytes(b'K64BOOTI', 'little'):
        return EINVAL
    if mem.d(b + 8) != 1 or mem.d(b + 12) != BOOTINFO_SIZE:
        return EINVAL
    if mem.q(b + 16) != 1:
        return EINVAL
    features = 0
    for off in (88, 96, 104, 108, 112, 116, 120, 128, 136):
        features |= mem.q(b + off)
    if features:
        return EINVAL
    for i in range(8):
        if mem.q(b + 144 + i * 8):
            return EINVAL

    map_addr, map_size = mem.q(b + 24), mem.q(b + 32)
    if map_addr == 0 or map_size == 0:
        return EINVAL
    if map_addr >= PHYS_LIMIT or map_size > MAP_MAX_SIZE:
        return EINVAL
    if map_addr + map_size > PHYS_LIMIT:
        return EINVAL
    stride = mem.d(b + 40)
    if stride < EFI_DESC_MIN or stride & 7:
        return EINVAL
    if mem.d(b + 44) != 1:
        return EINVAL
    count = mem.q(b + 48)
    if count == 0 or count > MAP_MAX_DESCS:
        return EINVAL
    if count * stride != map_size:
        return EINVAL
    state.boot.update({'memmap': map_addr, 'map_size': map_size,
                       'stride': stride, 'count': count})

    kstart, kend = mem.q(b + 56), mem.q(b + 64)
    if kstart == 0 or kstart & (PAGE_SIZE - 1) or kend & (PAGE_SIZE - 1):
        return EINVAL
    if kend <= kstart or kend - kstart > IMAGE_MAX:
        return EINVAL
    sstart, send = mem.q(b + 72), mem.q(b + 80)
    if sstart & (PAGE_SIZE - 1) or send & (PAGE_SIZE - 1):
        return EINVAL
    if send <= sstart or sstart < kstart or send != kend:
        return EINVAL
    state.boot.update({'kernel': (kstart, kend), 'stack': (sstart, send)})

    top = state.params['stack_top']
    if top < KERNEL_BASE:
        return EINVAL
    # the stack top is the exact conversion of the BootInfo range, not
    # just any address inside the stack
    if top - KERNEL_BASE + kstart != send:
        return EINVAL
    state.boot['stack_top_phys'] = send

    physical_limit = 1 << PHYS_BITS
    ranges = []
    for index in range(count):
        desc = map_addr + index * stride
        pages = mem.q(desc + 24)
        if pages == 0:
            return EINVAL
        phys = mem.q(desc + 8)
        if phys & (PAGE_SIZE - 1):
            return EINVAL
        size = pages * PAGE_SIZE
        if size > MASK64:
            return EINVAL
        end = phys + size
        if end > MASK64:
            return EINVAL
        if end > physical_limit:
            return EINVAL
        ranges.append((phys, end))
    ranges.sort()
    for i in range(1, len(ranges)):
        if ranges[i - 1][1] > ranges[i][0]:
            return EINVAL

    # only conventional WB memory without a reserved attribute is ours
    for index in range(count):
        desc = map_addr + index * stride
        kind = mem.d(desc)
        attrs = mem.q(desc + 32)
        if kind != EFI_CONVENTIONAL or not attrs & EFI_MEMORY_WB:
            continue
        if attrs & EFI_MEMORY_RESERVED:
            continue
        start = mem.q(desc + 8)
        if start >= PHYS_LIMIT:
            continue
        end = min(start + mem.q(desc + 24) * PAGE_SIZE, PHYS_LIMIT)
        for pfn in range(start >> PAGE_SHIFT, end >> PAGE_SHIFT):
            state.eligible.add(pfn)

    mm_pin_range(state, 0, 0x100000)
    mm_pin_range(state, state.boot['kernel'][0], state.boot['kernel'][1])
    mm_pin_range(state, boot_phys & ~(PAGE_SIZE - 1), boot_phys + BOOTINFO_SIZE)
    mm_pin_range(state, map_addr & ~(PAGE_SIZE - 1), map_addr + map_size)
    return mm_pin_tables(chk, mem, state, state.params['cr3'])


def run_fixture(chk, mem, params_addr, label):
    state = State()
    status = mm_bootmap_build(chk, mem, state, params_addr)
    free = sorted(state.eligible - state.pinned)
    return status, state, (free[0] if free else -1)


def check_fixtures(chk, mem, man):
    chk.start('BootInfo fixtures')
    count = mem.q(man['fx_count'])
    chk.eq(count, 36, 'the probe carries thirty six fixtures')
    base = man['fx_table']
    for index in range(count):
        row = base + index * 16 * 8
        tag = mem.read_bytes(row, 8).decode('ascii', 'replace')
        params = mem.q(row + 8)
        want_status = signed(mem.q(row + 16))
        want_eligible = mem.q(row + 24)
        want_pinned = mem.q(row + 32)
        want_free = signed(mem.q(row + 40))
        want_seen = mem.q(row + 48)
        probes = []
        for k in range(3):
            probes.append((mem.q(row + 56 + k * 24),
                           mem.q(row + 64 + k * 24),
                           mem.q(row + 72 + k * 24)))
        status, state, first_free = run_fixture(chk, mem, params, tag)
        chk.eq(status, want_status, '%s status' % tag)
        chk.eq(len(state.eligible), want_eligible, '%s eligible count' % tag)
        chk.eq(state.pinned_count(), want_pinned, '%s pinned count' % tag)
        chk.eq(first_free, want_free, '%s first free PFN' % tag)
        chk.eq(len(state.table_seen), want_seen,
               '%s page table pages visited' % tag)
        for pfn, want_e, want_p in probes:
            if pfn == 0:
                continue                    # unused slot, PFN 0 is never probed
            chk.eq(1 if pfn in state.eligible else 0, want_e,
                   '%s probe PFN %#x eligible' % (tag, pfn))
            chk.eq(1 if state.is_pinned(pfn) else 0, want_p,
                   '%s probe PFN %#x pinned' % (tag, pfn))

    # the fixtures that share the low arena must all agree on its pin set
    state = State()
    mm_pin_range(state, 0xFFFFF000, PHYS_LIMIT + 0x1000)
    chk.eq(sorted(state.pinned), [PFN_COUNT - 1],
           'a range that ends on 4 GiB pins its last frame only')
    state = State()
    mm_pin_range(state, 0x00100000, 0x00101001)
    chk.eq(sorted(state.pinned), [0x100, 0x101],
           'a byte range pins every page it touches')
    state = State()
    mm_pin_range(state, 0x00100000, 0x00101000)
    chk.eq(sorted(state.pinned), [0x100],
           'a page aligned range pins its page only')
    chk.eq(man['fx_pml4'] % PAGE_SIZE, 0, 'the low arena is page aligned')
    chk.eq(man['fx_pdpt'] - man['fx_pml4'], PAGE_SIZE,
           'the low PDPT follows its PML4')
    chk.eq(man['fx_pd'] - man['fx_pdpt'], PAGE_SIZE,
           'the low PD follows its PDPT')
    chk.eq(man['fx_pt'] - man['fx_pd'], PAGE_SIZE,
           'the low PT follows its PD')
    chk.eq(man['fx2_base'], 0x140000, 'the high arena address')
    chk.eq(man['fx_ps_pml4'], 0x150000, 'the PS arena address')
    chk.eq(man['fx_4g_pml4'], 0x160000, 'the 4 GiB arena address')
    chk.eq(man['fx_high_boot'], 0x180000, 'the high BootInfo address')
    chk.eq(man['fx_high_map'], 0x181000, 'the high memory map address')
    chk.eq(man['fx_many_base'], 0x190000, 'the many table arena address')
    # the cycle in the high arena must be visible in the bytes
    pdpt_win = man['fx2_base'] + 0x4000
    chk.eq(mem.q(pdpt_win + 2 * 8), man['fx2_base'] + 3,
           'the window PDPT points back at its PML4')
    chk.eq(mem.q(pdpt_win) & PTE_US, PTE_US,
           'the window PDPT entry carries U/S')
    chk.eq(mem.q(man['fx2_base'] + 0x180 * 8) & PTE_US, PTE_US,
           'the window PML4 entry carries U/S')


# ------------------------------------------------------------------
# section 3: the page table walk, the query record, map and unmap
# ------------------------------------------------------------------
# The model below is a transcription of mm_vmm_resolve, vmm_query,
# vmm_map and vmm_unmap. It reads and writes the real fixture tables, so
# a mapping it makes is a mapping the kernel would make in the same bytes,
# and the expectations stored next to each case are still hand written.

def walk(mem, root, pa_mask, va):
    """Mirror of mm_vmm_resolve: returns (status, record)."""
    rec = {'slot': 0, 'page_size': 0, 'rw': 0, 'us': 0, 'nx': 0, 'pte': 0, 'pa': 0}
    # only canonical addresses can be walked
    if va >> 47 not in (0, 0x1FFFF):
        return EINVAL, rec
    rw, us, nx = 1, 1, 0
    base = root
    for shift in (39, 30, 21):
        slot = base + ((va >> shift) & 0x1FF) * 8
        entry = mem.q(slot)
        if not entry & PTE_P:
            return ENOENT, rec
        rw &= (entry >> 1) & 1
        us &= (entry >> 2) & 1
        nx |= (entry >> 63) & 1
        if entry & PTE_PS:
            if shift == 39:
                return EINVAL, rec          # reserved PS bit in the PML4
            size = 1 << shift
            # a large leaf keeps PA in bits 51:shift; bit 12 is PAT and
            # must be masked out together with the offset bits
            leaf_pa = (entry & pa_mask) & ~(size - 1) & MASK64
            rec.update(slot=slot, page_size=size, rw=rw, us=us, nx=nx,
                       pte=entry, pa=leaf_pa + (va & (size - 1)))
            return OK, rec
        base = entry & pa_mask
        if base >= PHYS_LIMIT:
            return ENOTSUP, rec             # table page outside the identity map
    slot = base + ((va >> 12) & 0x1FF) * 8
    entry = mem.q(slot)
    if not entry & PTE_P:
        return ENOENT, rec
    rw &= (entry >> 1) & 1
    us &= (entry >> 2) & 1
    nx |= (entry >> 63) & 1
    rec.update(slot=slot, page_size=PAGE_SIZE, rw=rw, us=us, nx=nx, pte=entry,
               pa=(entry & pa_mask) + (va & (PAGE_SIZE - 1)))
    return OK, rec


def query(mem, root, pa_mask, va):
    """Mirror of vmm_query: returns (status, 40 byte record fields)."""
    status, rec = walk(mem, root, pa_mask, va)
    empty = {'leaf': 0, 'pa': 0, 'page_size': 0, 'flags': 0, 'pte': 0}
    if status != OK:
        return status, empty                  # every failure zeroes the record
    flags = VQ_PRESENT
    if rec['rw']:
        flags |= VQ_RW
    if rec['us']:
        flags |= VQ_US
    if rec['nx']:
        flags |= VQ_NX
    return OK, {'leaf': rec['slot'], 'pa': rec['pa'],
                'page_size': rec['page_size'], 'flags': flags, 'pte': rec['pte']}


def check_query(chk, mem, man):
    chk.start('query')
    count = mem.q(man['qry_count'])
    chk.eq(count, 20, 'the probe carries twenty query cases')
    base = man['qry_table']
    root = man['fx2_base']
    negative = 0
    for index in range(count):
        row = base + index * 9 * 8
        tag = mem.read_bytes(row, 8).decode('ascii', 'replace')
        cr3 = mem.q(row + 8)
        va = mem.q(row + 16)
        want_status = signed(mem.q(row + 24))
        want_pa = mem.q(row + 32)
        want_leaf = mem.q(row + 40)
        want_size = mem.q(row + 48)
        want_flags = mem.q(row + 56)
        want_pte = mem.q(row + 64)
        status, got = query(mem, cr3, PA_MASK, va)
        chk.eq(status, want_status, '%s status' % tag)
        if want_status != OK:
            negative += 1
            chk.eq(got, {'leaf': 0, 'pa': 0, 'page_size': 0, 'flags': 0,
                         'pte': 0}, '%s zeroes the record on failure' % tag)
            continue
        chk.eq(got['pa'], want_pa, '%s physical address' % tag)
        chk.eq(got['leaf'], want_leaf, '%s leaf entry address' % tag)
        chk.eq(got['page_size'], want_size, '%s page size' % tag)
        chk.eq(got['flags'], want_flags, '%s path permissions' % tag)
        chk.eq(got['pte'], want_pte, '%s raw leaf entry' % tag)
    chk.ok(negative >= 8, 'the query set contains failing cases')

    # the walk must mask a large page leaf and keep the offset inside it
    status, rec = query(mem, root, PA_MASK, WINDOW_BASE + 0x2FFFFF)
    chk.eq(status, OK, 'the last byte of the 2 MiB leaf resolves')
    chk.eq(rec['pa'], 0x2FFFFF, 'the 2 MiB offset is kept, the leaf is masked')
    chk.eq(rec['page_size'], 0x200000, 'the 2 MiB leaf reports its size')
    status, rec = query(mem, root, PA_MASK, WINDOW_BASE + (2 << 30) - 1)
    chk.eq(rec['pa'], 0x7FFFFFFF, 'the last byte of the 1 GiB leaf')
    # and a 4 KiB leaf never reports a large page size
    status, rec = query(mem, root, PA_MASK, WINDOW_BASE + 0x2FFC)
    chk.eq(rec['page_size'], PAGE_SIZE, 'the 4 KiB leaf reports 0x1000')
    # the PAT bit of a large leaf is not part of the physical address
    status, rec = query(mem, root, PA_MASK, WINDOW_BASE + 0x400000 + 0x1234)
    chk.eq(status, OK, 'the 2 MiB PAT leaf resolves')
    chk.eq(rec['pa'], 0x401234, 'the 2 MiB PAT bit is masked out of the PA')
    status, rec = query(mem, root, PA_MASK, WINDOW_BASE + (3 << 30) + 0x5678)
    chk.eq(status, OK, 'the 1 GiB PAT leaf resolves')
    chk.eq(rec['pa'], 0x80005678, 'the 1 GiB PAT bit is masked out of the PA')
    # a non canonical address is refused before any table is read
    status, rec = query(mem, root, PA_MASK, 0x0000800000000000)
    chk.eq(status, EINVAL, 'a non canonical address is refused')
    chk.eq(rec, {'leaf': 0, 'pa': 0, 'page_size': 0, 'flags': 0, 'pte': 0},
           'and the record stays zeroed')
    # the physical address mask covers the full 52 bit width
    chk.eq(query(mem, root, PA_MASK, WINDOW_BASE + 0x2000)[1]['pa'],
           0x000FFFFFFFFFF000, 'a 52 bit leaf address is not truncated')
    # a narrower CPU width must clip that same leaf
    narrow = ((1 << 36) - 1) & ~(PAGE_SIZE - 1)
    chk.eq(query(mem, root, narrow, WINDOW_BASE + 0x2000)[1]['pa'],
           0x000FFFFFFFFFF000 & narrow,
           'a 36 bit pa mask trims the leaf address')


# ------------------------------------------------------------------
# section 4: physical allocator, mappings and unmappings
# ------------------------------------------------------------------
# The pool starts above the probe image, so the pages the model hands out
# never overwrite a fixture.

POOL_BASE = 0x260000
POOL_PAGES = 32


def pool_pfns(count, base=POOL_BASE):
    first = base >> PAGE_SHIFT
    return set(range(first, first + count))


class Pmm:
    """The three bitmaps of pmm.inc, as sets of page frame numbers."""

    def __init__(self, mem, pool, pinned=()):
        self.mem = mem
        self.eligible = set(pool)
        self.pinned = set(pinned)
        self.allocated = set()
        free = self.eligible - self.pinned
        self.next_hint = min(free) if free else 0
        self.fail = 0                          # mm_test_fail
        self.hits = 0                          # mm_test_fail_hits

    def next_free(self, start):
        def usable(pfn):
            return (pfn in self.eligible and pfn not in self.pinned
                    and pfn not in self.allocated)
        pfn = start
        while pfn < PFN_COUNT:
            if usable(pfn):
                return pfn
            pfn += 1
        pfn = 0
        while pfn < start:
            if usable(pfn):
                return pfn
            pfn += 1
        return -1

    def alloc_pfn(self):
        pfn = self.next_free(self.next_hint)
        if pfn < 0:
            return -1
        self.allocated.add(pfn)
        self.next_hint = 0 if pfn + 1 >= PFN_COUNT else pfn + 1
        return pfn

    def alloc_page(self):
        pfn = self.alloc_pfn()
        if pfn < 0:
            return None
        pa = pfn << PAGE_SHIFT
        self.mem.zero_page(pa)                 # mm_pfn_zero, identity map
        return pa

    def alloc_table(self):
        """mm_pmm_alloc_table: allocated, then pinned, and never handed out."""
        if self.fail:
            self.fail -= 1
            self.hits += 1
            if self.fail == 0:
                return None
        pfn = self.alloc_pfn()
        if pfn < 0:
            return None
        self.allocated.discard(pfn)
        self.pinned.add(pfn)
        pa = pfn << PAGE_SHIFT
        self.mem.zero_page(pa)
        return pa

    def drop_table(self, pa):
        pfn = pa >> PAGE_SHIFT
        if pfn in self.pinned and pfn not in self.allocated:
            self.pinned.discard(pfn)
            self.next_hint = min(self.next_hint, pfn)
            return OK
        return EIO

    def free(self, pa):
        if pa & (PAGE_SIZE - 1):
            return EINVAL
        pfn = pa >> PAGE_SHIFT
        if pfn >= PFN_COUNT:
            return EINVAL
        if pfn not in self.eligible:
            return EPERM
        if pfn in self.pinned:
            return EPERM
        if pfn not in self.allocated:
            return EALREADY
        self.allocated.discard(pfn)
        self.next_hint = min(self.next_hint, pfn)
        return OK

    def free_count(self):
        return len(self.eligible - self.pinned - self.allocated)

    def stats(self):
        """pmm_get_stats: six frozen qwords, eligible pages only.

        The counters must obey total = free + allocated + reserved, and
        the allocated set must stay inside the eligible set and outside
        the pinned set; any violation is reported as EIO.
        """
        free = self.eligible - self.pinned - self.allocated
        record = {
            'managed_limit': PHYS_LIMIT,
            'total': len(self.eligible),
            'free': len(free),
            'allocated': len(self.allocated),
            'reserved': len(self.eligible & self.pinned),
            'next_pfn': self.next_hint,
        }
        broken = (len(self.allocated - self.eligible)
                  or len(self.allocated & self.pinned)
                  or record['total'] != (record['free'] + record['allocated']
                                         + record['reserved']))
        return (EIO if broken else OK), record


def check_pmm(chk, mem, man):
    chk.start('pmm')
    pristine = mem.snapshot()

    # one bit per 4 KiB frame of the low 4 GiB, and the three bitmaps are
    # 128 KiB of really emitted zero bytes inside the data block
    chk.eq(BITMAP_BYTES * 8, PFN_COUNT, 'one bit per page frame')
    chk.eq(PFN_COUNT, PHYS_LIMIT >> PAGE_SHIFT,
           'the bitmap covers the low 4 GiB exactly')
    chk.eq(man['bitmap_pinned'] - man['bitmap_eligible'], 2 * BITMAP_BYTES,
           'the three bitmaps are contiguous')
    chk.ok(man['data_start'] - 0x1000 < man['bitmap_eligible'] < man['data_end'],
           'the bitmaps start inside the data block')

    # the head of the pool is pinned and must never be handed out; a dirty
    # free page comes back zeroed through the identity map
    dirty = bytes([0xA5]) * PAGE_SIZE
    mem.write_bytes(POOL_BASE, dirty)
    mem.write_bytes(POOL_BASE + PAGE_SIZE, dirty)
    pmm = Pmm(mem, pool_pfns(8), pinned=(POOL_BASE >> PAGE_SHIFT,))
    first = pmm.alloc_page()
    chk.eq(first, POOL_BASE + PAGE_SIZE, 'a pinned frame is skipped')
    chk.ok(mem.page_zeroed(first), 'the fresh page is zeroed')
    chk.eq(mem.read_bytes(POOL_BASE, PAGE_SIZE), dirty,
           'the pinned page keeps its bytes')

    # next fit: four frames in a row, then the hint
    seq = [pmm.alloc_page() for _ in range(3)]
    chk.eq(seq, [POOL_BASE + i * PAGE_SIZE for i in (2, 3, 4)],
           'next fit walks upwards')
    chk.eq(pmm.next_hint, (POOL_BASE >> PAGE_SHIFT) + 5,
           'the hint follows the last frame')
    chk.ok(all(p is not None and p < PHYS_LIMIT for p in seq),
           'every allocation stays below 4 GiB')

    # the scan wraps around the top of the frame space instead of failing
    pmm.next_hint = PFN_COUNT - 1
    chk.eq(pmm.alloc_page(), POOL_BASE + 5 * PAGE_SIZE,
           'the scan wraps around the top of the frame space')

    # release: alignment, range, eligibility, pinning and duplicates
    chk.eq(pmm.free(POOL_BASE + 4), EINVAL, 'an unaligned address is refused')
    chk.eq(pmm.free(0x00900000), EPERM, 'a frame outside the eligible set is refused')
    chk.eq(pmm.free(1 << 32), EINVAL, 'a frame at 4 GiB is refused')
    chk.eq(pmm.free(POOL_BASE), EPERM, 'a pinned frame is refused')
    last = seq[-1]
    chk.eq(pmm.free(last), OK, 'an allocated frame is released')
    chk.eq(pmm.free(last), EALREADY, 'a duplicate release is refused')
    chk.eq(pmm.alloc_page(), last, 'the released frame comes back first')
    chk.eq(pmm.free_count(), 2, 'two frames are still free')

    # an exhausted pool reports the empty frame instead of a bad one
    small = Pmm(mem, pool_pfns(3, POOL_BASE + 0x8000))
    got = [small.alloc_page() for _ in range(3)]
    chk.ok(all(p is not None for p in got), 'three frames come out')
    chk.eq(small.alloc_page(), None, 'the empty pool hands out nothing')
    chk.eq(small.free(got[1]), OK, 'a frame released after exhaustion is accepted')
    chk.eq(small.alloc_page(), got[1], 'and comes back on the next call')

    # the statistics record and the identity it has to satisfy
    status, rec = pmm.stats()
    chk.eq(status, OK, 'the record is consistent')
    chk.eq(rec['managed_limit'], PHYS_LIMIT, 'the managed limit is reported')
    chk.eq(rec['total'], 8, 'total covers the eligible frames only')
    chk.eq(rec['reserved'], 1, 'reserved counts the pinned eligible frames')
    chk.eq(rec['allocated'], len(pmm.allocated), 'allocated frames')
    chk.eq(rec['free'], pmm.free_count(), 'free frames')
    chk.eq(rec['next_pfn'], pmm.next_hint, 'the hint is reported')
    chk.eq(rec['total'], rec['free'] + rec['allocated'] + rec['reserved'],
           'total = free + allocated + reserved')

    # the same checks the assembly makes return EIO when they are broken
    pmm.allocated.add(0x90000)
    chk.eq(pmm.stats()[0], EIO, 'a frame outside the map is an error')
    pmm.allocated.discard(0x90000)
    pmm.pinned.add(min(pmm.allocated))
    chk.eq(pmm.stats()[0], EIO, 'an allocated and pinned frame is an error')
    pmm.pinned.discard(min(pmm.allocated))
    chk.eq(pmm.stats()[0], OK, 'and the record is clean again')

    # the dirty-page probe above wrote into the pool: give the later
    # sections back the pristine image, they build their roots from it
    mem.restore(pristine)


class Vmm:
    """vmm.inc on one root page: map, unmap and the accounting."""

    def __init__(self, mem, pmm, root):
        self.mem = mem
        self.pmm = pmm
        self.root = root
        self.ready = True
        self.pa_mask = PA_MASK
        self.window_base = WINDOW_BASE
        self.window_limit = WINDOW_LIMIT
        self.table_allocs = 0
        self.table_drops = 0

    def _drop(self, status, new):
        self.table_drops += len(new)
        for page in reversed(new):
            self.pmm.drop_table(page)
        return status

    def map(self, va, pa, flags):
        if not self.ready:
            return ENOTREADY
        if va < self.window_base or va >= self.window_limit:
            return EPERM
        if va & (PAGE_SIZE - 1):
            return EINVAL
        if pa & (PAGE_SIZE - 1):
            return EINVAL
        if flags & ~MAP_ALLOWED:
            return EINVAL
        if flags & MAP_RW and not flags & MAP_NX:
            return EINVAL
        # ownership: handed out by the public allocator and not pinned
        pfn = pa >> PAGE_SHIFT
        if (pfn >= PFN_COUNT or pfn not in self.pmm.allocated
                or pfn in self.pmm.pinned):
            return EPERM
        if pa & ~self.pa_mask & MASK64:
            return EOVERFLOW
        base = self.root
        publish = 0
        new = []
        for shift in (39, 30, 21):
            slot = base + ((va >> shift) & 0x1FF) * 8
            entry = self.mem.q(slot)
            if not entry & PTE_P:
                page = self.pmm.alloc_table()
                if page is None:
                    return self._drop(ENOMEM, new)
                if new:
                    self.mem.setq(slot, page | PTE_P | PTE_RW)
                else:
                    publish = slot
                new.append(page)
                base = page
            else:
                if entry & PTE_PS:
                    return self._drop(ENOTSUP, new)   # a large page is in the way
                base = entry & self.pa_mask
                if base >= PHYS_LIMIT:
                    return self._drop(ENOTSUP, new)
        slot = base + ((va >> 12) & 0x1FF) * 8
        if self.mem.q(slot) & PTE_P:
            return self._drop(EEXIST, new)
        self.mem.setq(slot, pa | PTE_P | flags)
        if new:
            self.mem.setq(publish, new[0] | PTE_P | PTE_RW)   # atomic publish
        self.table_allocs += len(new)
        return OK

    def unmap(self, va):
        if not self.ready:
            return ENOTREADY, 0
        if va < self.window_base or va >= self.window_limit:
            return EPERM, 0
        if va & (PAGE_SIZE - 1):
            return EINVAL, 0
        status, rec = walk(self.mem, self.root, self.pa_mask, va)
        if status != OK:
            return status, 0
        if rec['page_size'] != PAGE_SIZE:
            return ENOTSUP, 0
        self.mem.setq(rec['slot'], 0)
        return OK, rec['pa']


def image_q(img, man, slot):
    return struct.unpack_from('<Q', img, slot - man['org'])[0]


def new_machine(mem, root, pages=POOL_PAGES):
    pmm = Pmm(mem, pool_pfns(pages))
    return pmm, Vmm(mem, pmm, root)


def map_machine(mem, root):
    """A machine whose data page 0x900000 was handed out by the allocator.

    vmm_map_page only accepts physical pages the public allocator owns,
    so every mapping case starts with that page allocated; the table
    pages still come from the pool above the image.
    """
    pmm = Pmm(mem, pool_pfns(POOL_PAGES) | {0x900})
    pmm.next_hint = 0x900
    pmm.alloc_page()
    pmm.next_hint = POOL_BASE >> PAGE_SHIFT
    return pmm, Vmm(mem, pmm, root)


def check_map(chk, mem, man):
    chk.start('map')
    count = mem.q(man['map_count'])
    chk.eq(count, 26, 'the probe carries twenty six map cases')
    base = man['map_table']
    root = man['fx2_base']
    pdpt = mem.q(root + 0x180 * 8) & PA_MASK
    pd = mem.q(pdpt) & PA_MASK
    # where the freshly built chain has to appear, per case
    boundaries = {
        'MP3TAB': ((root, 0x182), (root, 0x180)),
        'MP2TAB': ((pdpt, 5), (pdpt, 1)),
        'MP1TAB': ((pd, 3), (pd, 1)),
    }
    pristine = mem.snapshot()
    negative = 0
    for index in range(count):
        row = base + index * 9 * 8
        tag = mem.read_bytes(row, 8).decode('ascii', 'replace')
        va, pa = mem.q(row + 8), mem.q(row + 16)
        flags = mem.q(row + 24)
        want_status = signed(mem.q(row + 32))
        hook = mem.q(row + 40)
        want_tables = mem.q(row + 48)
        want_leaf = mem.q(row + 56)
        want_present = mem.q(row + 64)

        mem.restore(pristine)
        pmm, vmm = map_machine(mem, root)
        pmm.fail = hook
        was_status, was_rec = walk(mem, root, PA_MASK, va)
        before = bytes(mem.image)
        status = vmm.map(va, pa, flags)
        chk.eq(status, want_status, '%s status' % tag)
        chk.ok(vmm.table_allocs <= 3, '%s built at most three tables' % tag)
        chk.eq(vmm.table_allocs, want_tables, '%s new table pages' % tag)

        if want_present:
            chk.eq(mem.q(walk(mem, root, PA_MASK, va)[1]['slot']), want_leaf,
                   '%s leaf entry' % tag)
            status, rec = walk(mem, root, PA_MASK, va)
            chk.eq(status, OK, '%s leaf is reachable' % tag)
            chk.eq(rec['page_size'], PAGE_SIZE, '%s leaf is 4 KiB' % tag)
            chk.eq(rec['pa'], pa, '%s leaf maps the requested page' % tag)
            chk.eq(want_leaf & (PTE_US | PTE_G | PTE_PAT | PTE_PS | PTE_PWT
                                | PTE_PCD), 0, '%s never sets a reserved bit' % tag)
            chk.eq(want_leaf & PTE_RW != 0, flags & MAP_RW != 0,
                   '%s leaf takes the requested RW' % tag)
            chk.eq(want_leaf & PTE_NX != 0, flags & MAP_NX != 0,
                   '%s leaf takes the requested NX' % tag)
            # the new tables come from the pool and stay pinned
            chk.eq(len(pmm.pinned), want_tables,
                   '%s table pages were pinned' % tag)
            chk.eq(len(pmm.allocated), 1,
                   '%s only the data page stays allocated' % tag)
            chk.eq(pmm.free_count(), POOL_PAGES - want_tables,
                   '%s the pool lost exactly the new tables' % tag)
            if tag in boundaries:
                (parent, pindex), (neighbour, nindex) = boundaries[tag]
                entry = mem.q(parent + pindex * 8)
                chk.ok(entry & PTE_P, '%s published the table at index %#x'
                       % (tag, pindex))
                chk.eq(entry & (PTE_US | PTE_PS | PTE_NX), 0,
                       '%s table link is plain supervisor RW' % tag)
                chk.eq(pmm.pinned.__contains__((entry & PA_MASK) >> 12), 1,
                       '%s table link points at a pinned page' % tag)
                chk.eq(mem.q(neighbour + nindex * 8),
                       image_q(before, man, neighbour + nindex * 8),
                       '%s leaves the neighbouring slot alone' % tag)
        else:
            negative += 1
            # nothing at all was published into the existing tables
            chk.eq(bytes(mem.image), before,
                   '%s changes no byte of the existing tables' % tag)
            chk.eq(len(pmm.pinned), 0, '%s keeps no table page' % tag)
            chk.eq(len(pmm.allocated), 1, '%s keeps only the data page' % tag)
            chk.eq(pmm.free_count(), POOL_PAGES, '%s frees the whole pool' % tag)
            chk.eq(vmm.table_allocs, 0, '%s counts no published table' % tag)
            chk.eq(vmm.table_drops, max(0, pmm.hits - (1 if hook else 0)),
                   '%s dropped every table it built' % tag)
            status, rec = walk(mem, root, PA_MASK, va)
            # the refusal must leave the leaf exactly as it was: an address
            # that was already mapped stays mapped, one that was absent stays
            # absent, and no half built chain shows up
            chk.eq((status, rec['pte']), (was_status, was_rec['pte']),
                   '%s leaves the leaf entry untouched' % tag)
    chk.ok(negative >= 12, 'the map set contains failing cases')

    # the same address works once the hook is gone: no half built chain
    mem.restore(pristine)
    pmm, vmm = map_machine(mem, root)
    va = WINDOW_BASE + (2 << 39)
    pmm.fail = 2
    chk.eq(vmm.map(va, 0x900000, MAP_NX), ENOMEM, 'a hooked build fails')
    pmm.fail = 0
    chk.eq(vmm.map(va, 0x900000, MAP_NX), OK, 'the retry succeeds')
    chk.eq(vmm.table_allocs, 3, 'the retry builds the whole chain')
    chk.eq(len(pmm.pinned), 3, 'the retry pins the whole chain')
    chk.eq(walk(mem, root, PA_MASK, va)[1]['pa'], 0x900000,
           'the retried mapping is correct')

    # a pinned data page is refused even while it is marked allocated
    mem.restore(pristine)
    pmm, vmm = map_machine(mem, root)
    pmm.pinned.add(0x900)
    before = bytes(mem.image)
    chk.eq(vmm.map(WINDOW_BASE + 0x3000, 0x900000, MAP_NX), EPERM,
           'a pinned physical page cannot be mapped')
    chk.eq(bytes(mem.image), before, 'and the refusal writes nothing')
    chk.eq(vmm.table_allocs, 0, 'no table was built for the pinned page')


def check_unmap(chk, mem, man):
    chk.start('unmap')
    count = mem.q(man['unmap_count'])
    chk.eq(count, 6, 'the probe carries six unmap cases')
    base = man['unmap_table']
    root = man['fx2_base']
    pristine = mem.snapshot()
    negative = 0
    for index in range(count):
        row = base + index * 5 * 8
        tag = mem.read_bytes(row, 8).decode('ascii', 'replace')
        va = mem.q(row + 8)
        want_status = signed(mem.q(row + 16))
        want_cleared = mem.q(row + 24)
        want_pa = mem.q(row + 32)

        mem.restore(pristine)
        pmm, vmm = new_machine(mem, root)
        before = bytes(mem.image)
        status, rec = walk(mem, root, PA_MASK, va)
        slot = rec['slot']
        old = mem.q(slot)
        status, got_pa = vmm.unmap(va)
        chk.eq(status, want_status, '%s status' % tag)
        chk.eq(got_pa, want_pa, '%s reports the unmapped physical address' % tag)
        if want_status != OK:
            negative += 1
            chk.eq(bytes(mem.image), before, '%s changes nothing' % tag)
            continue
        chk.eq(mem.q(slot), 0, '%s clears the leaf' % tag)
        chk.ok(old & PTE_P, '%s had a mapped leaf before' % tag)
        # the leaf page and the tables stay: unmap only clears the entry
        chk.eq(len(pmm.pinned), 0, '%s pins nothing' % tag)
        chk.eq(len(pmm.allocated), 0, '%s frees no page' % tag)
        chk.eq(pmm.free_count(), POOL_PAGES, '%s leaves the pool alone' % tag)
        chk.eq(walk(mem, root, PA_MASK, va)[0], ENOENT, '%s is gone' % tag)
        # and the table that held it is still linked
        pdpt = mem.q(root + 0x180 * 8) & PA_MASK
        chk.ok(mem.q(pdpt) & PTE_P, '%s keeps the empty table chain' % tag)
    chk.ok(negative >= 4, 'the unmap set contains failing cases')


# ------------------------------------------------------------------
# section 5: the early bump heap
# ------------------------------------------------------------------

class Heap:
    """heap.inc: a bump pointer over lazily mapped RW/NX pages."""

    def __init__(self, vmm):
        self.vmm = vmm
        self.ready = False
        self.base, self.limit = HEAP_BASE, HEAP_LIMIT
        self.mapped = 0
        self.used = 0
        self.allocs = 0
        self.grows = 0

    def grow(self, need):
        rounded = (need + PAGE_SIZE - 1) & ~(PAGE_SIZE - 1)
        if self.mapped >= rounded:
            return OK
        keep = self.mapped
        page = keep
        added = []
        while page < rounded:
            pa = self.vmm.pmm.alloc_page()
            if pa is None:
                return self._rollback(added, keep)
            status = self.vmm.map(self.base + page, pa, MAP_RW | MAP_NX)
            if status != OK:
                self.vmm.pmm.free(pa)
                return self._rollback(added, keep)
            added.append((self.base + page, pa))
            page += PAGE_SIZE
        self.mapped = page
        self.grows += 1
        return OK

    def _rollback(self, added, keep):
        for va, pa in reversed(added):
            self.vmm.unmap(va)
            self.vmm.pmm.free(pa)
        self.mapped = keep
        return ENOMEM

    def init(self):
        if not self.vmm.ready:
            return ENOTREADY
        if self.ready:
            return EALREADY
        self.mapped = self.used = self.allocs = self.grows = 0
        status = self.grow(HEAP_INITIAL)
        if status != OK:
            return status
        self.ready = True
        return OK

    def alloc(self, size, align):
        if not self.ready:
            return ENOTREADY, 0
        if size == 0:
            return EINVAL, 0
        if align == 0:
            align = 16
        if align < 16 or align > PAGE_SIZE or align & (align - 1):
            return EINVAL, 0
        start = (self.used + align - 1) & ~(align - 1)
        end = start + size
        if end > HEAP_MAX:
            return ENOMEM, 0
        if end > self.mapped:
            status = self.grow(end)
            if status != OK:
                return status, 0
        self.used = end
        self.allocs += 1
        return OK, self.base + start

    def free(self, addr):
        return ENOTSUP

    def stats(self):
        # the frozen 32 byte record keeps absolute addresses only
        return {'base': self.base, 'brk': self.base + self.used,
                'mapped_end': self.base + self.mapped, 'limit': self.limit}


def heap_machine(mem, snapshot, pages=POOL_PAGES, root_pfn=0x260):
    """A fresh address space whose root is one zeroed page of the pool."""
    mem.restore(snapshot)
    root = root_pfn << PAGE_SHIFT
    pmm = Pmm(mem, pool_pfns(pages, root + PAGE_SIZE), pinned=(root_pfn,))
    vmm = Vmm(mem, pmm, root)
    return pmm, vmm, Heap(vmm)


def heap_pages(mem, vmm, heap, size=HEAP_INITIAL):
    """Physical address of every page of the mapped part of the heap."""
    out = []
    for page in range(0, size, PAGE_SIZE):
        status, rec = walk(mem, vmm.root, PA_MASK, heap.base + page)
        out.append(None if status else rec['pa'])
    return out


def check_heap(chk, mem, man):
    chk.start('heap')
    pristine = mem.snapshot()
    pmm, vmm, heap = heap_machine(mem, pristine)

    # a heap before its VMM is ready is refused, and free is not supported
    cold = Heap(Vmm(mem, pmm, vmm.root))
    cold.vmm.ready = False
    chk.eq(cold.init(), ENOTREADY, 'heap_init needs a ready VMM')
    chk.eq(heap.free(HEAP_BASE), ENOTSUP, 'heap_free is not supported')
    status, _ = heap.alloc(16, 16)
    chk.eq(status, ENOTREADY, 'alloc needs a ready heap')

    # init: 64 KiB mapped RW/NX through three fresh tables
    chk.eq(heap.init(), OK, 'heap_init succeeds')
    chk.eq(heap.init(), EALREADY, 'heap_init is not idempotent')
    stats = heap.stats()
    chk.eq(stats['base'], HEAP_BASE, 'heap base')
    chk.eq(stats['limit'], HEAP_LIMIT, 'heap limit')
    chk.eq(stats['mapped_end'], HEAP_BASE + HEAP_INITIAL,
           'the first mapping is 64 KiB')
    chk.eq(stats['brk'], HEAP_BASE, 'nothing is used yet')
    chk.eq(heap.allocs, 0, 'no allocation yet')
    chk.eq(heap.grows, 1, 'one growth so far')
    chk.eq(vmm.table_allocs, 3, 'the heap needed three tables')
    chk.eq(len(pmm.pinned), 4, 'three tables and the root are pinned')
    pages = heap_pages(mem, vmm, heap)
    chk.eq(len(set(pages)), 16, 'sixteen distinct data pages')
    chk.ok(all(p is not None for p in pages), 'every leaf is present')
    chk.ok(all(mem.page_zeroed(p) for p in pages), 'every page is zeroed')
    for page in range(0, HEAP_INITIAL, PAGE_SIZE):
        entry = walk(mem, vmm.root, PA_MASK, HEAP_BASE + page)[1]['pte']
        chk.eq(entry & (PTE_P | PTE_RW | PTE_NX), PTE_P | PTE_RW | PTE_NX,
               'heap leaf %#x is present, RW and NX' % page)
        chk.eq(entry & (PTE_US | PTE_PS), 0,
               'heap leaf %#x is supervisor and 4 KiB' % page)

    # every allowed alignment is honoured and the block comes back zeroed
    for align in (16, 32, 64, 128, 256, 512, 1024, 2048, 4096):
        status, addr = heap.alloc(32, align)
        chk.eq(status, OK, 'alloc with alignment %d' % align)
        chk.eq(addr % align, 0, 'the block respects alignment %d' % align)
        chk.ok(HEAP_BASE <= addr < HEAP_BASE + heap.mapped,
               'the block is inside the mapped part')
        chk.ok(mem.read_bytes(walk(mem, vmm.root, PA_MASK, addr)[1]['pa'],
                              32) == bytes(32), 'the block is zeroed')
    # alignment 0 selects the default alignment 16
    status, addr = heap.alloc(16, 0)
    chk.eq(status, OK, 'alignment 0 is accepted')
    chk.eq(addr % 16, 0, 'alignment 0 means 16')
    chk.eq(heap.used <= heap.mapped, True, 'used stays inside mapped')
    used_before, mapped_before = heap.used, heap.mapped
    allocs_before = heap.allocs

    # rejected arguments must not move the bump pointer
    for size, align, want in ((0, 16, EINVAL), (16, 8, EINVAL),
                              (16, 8192, EINVAL), (16, 24, EINVAL)):
        chk.eq(heap.alloc(size, align)[0], want,
               'alloc(%d, %d) is refused' % (size, align))
    chk.eq((heap.used, heap.mapped, heap.allocs),
           (used_before, mapped_before, allocs_before),
           'a refused allocation changes nothing')

    # a request above the 16 MiB window is refused without any growth
    chk.eq(heap.alloc(HEAP_MAX + 1, 16)[0], ENOMEM, 'above the window is refused')
    chk.eq(heap.alloc(HEAP_MAX - heap.used + 1, 16)[0], ENOMEM,
           'a request that ends past the window is refused')
    chk.eq((heap.used, heap.mapped), (used_before, mapped_before),
           'a refused request maps nothing')

    # growth maps on demand and keeps the statistics consistent
    need = heap.mapped - heap.used + 0x8000
    status, addr = heap.alloc(need, 16)
    chk.eq(status, OK, 'a request past the mapping grows the heap')
    chk.ok(heap.mapped >= heap.used, 'the heap grew far enough')
    chk.ok(heap.mapped <= HEAP_MAX, 'the heap stays inside its window')
    chk.ok(addr + need <= HEAP_BASE + heap.mapped, 'the block is fully mapped')
    chk.eq(heap.used, used_before + need, 'the bump pointer moved by the request')
    grown = heap.grows
    chk.eq(grown, 2, 'the growth counter advanced')

    # a growth that runs out of memory rolls every page back
    pmm2, vmm2, heap2 = heap_machine(mem, pristine, pages=3 + 16 + 5)
    chk.eq(heap2.init(), OK, 'a small pool still serves the first 64 KiB')
    used2, mapped2 = heap2.used, heap2.mapped
    free2 = pmm2.free_count()
    alloc2 = len(pmm2.allocated)
    chk.eq(free2, 5, 'five pages are left')
    chk.eq(heap2.alloc(mapped2 + 0x8000, 16)[0], ENOMEM,
           'growth fails when the allocator runs dry')
    chk.eq((heap2.used, heap2.mapped), (used2, mapped2),
           'the failed growth leaves the heap unchanged')
    chk.eq(pmm2.free_count(), free2, 'the failed growth frees every data page')
    chk.eq(len(pmm2.allocated), alloc2,
           'the failed growth keeps only the pages already in use')
    for page in range(mapped2, mapped2 + 0x8000, PAGE_SIZE):
        chk.eq(walk(mem, vmm2.root, PA_MASK, HEAP_BASE + page)[0], ENOENT,
               'page %#x was unmapped by the rollback' % page)
    chk.eq(heap2.alloc(5 * PAGE_SIZE, 16)[0], OK,
           'what fits still works after a rollback')

    # a growth whose page table allocation fails rolls back the same way
    pmm3, vmm3, heap3 = heap_machine(mem, pristine)
    pmm3.fail = 2
    chk.eq(heap3.init(), ENOMEM, 'a hooked init fails')
    chk.eq(heap3.ready, False, 'a failed init leaves the heap unready')
    chk.eq(heap3.mapped, 0, 'a failed init maps nothing')
    chk.eq(len(pmm3.pinned), 1, 'the first table page stays pinned')
    chk.eq(len(pmm3.allocated), 0, 'no data page stays allocated')
    chk.eq(walk(mem, vmm3.root, PA_MASK, HEAP_BASE)[0], ENOENT,
           'nothing is published after a failed init')
    pmm3.fail = 0
    chk.eq(heap3.init(), OK, 'the same heap initialises once the hook is gone')
    chk.eq(heap3.mapped, HEAP_INITIAL, 'and maps the first 64 KiB')
    chk.eq(heap3.grows, 1, 'one growth was counted')


# ------------------------------------------------------------------
# section 5: protecting the kernel image and the stack
# ------------------------------------------------------------------
# The probe maps KERNEL_BASE + n * 4 KiB onto 0x500000 + n * 4 KiB, so the
# pass has to walk the kernel range and normalise three leaves that start
# with three different permissions.

KERNEL_START = 0x00500000
KERNEL_END = 0x00503000
STACK_START = 0x00502000
STACK_END = 0x00503000


def protect_pass(mem, boot, root, va, count, flags, apply_it):
    """Mirror of mm_vmm_protect_pass over [va, va + count * 4 KiB)."""
    plan = []
    for index in range(count):
        cursor = va + index * PAGE_SIZE
        status, rec = walk(mem, root, PA_MASK, cursor)
        if status == ENOENT:
            return EINVAL, []                 # a hole in the range
        if status != OK:
            return status, []
        if rec['page_size'] != PAGE_SIZE:
            return ENOTSUP, []                # a large page cannot be split
        if rec['pa'] != boot['kernel_start'] + (cursor - KERNEL_BASE):
            return EIO, []                    # the leaf maps another frame
        entry = (rec['pte'] & ~MAP_ALLOWED & MASK64) | flags
        plan.append((rec['slot'], entry))
    if apply_it:
        for slot, entry in plan:
            mem.setq(slot, entry)
    return OK, plan


def boot_protect(mem, boot, root, va, count, flags):
    """vmm_boot_protect(RCX=va, RDX=count, R8=flags): validate the arguments,
    verify every page of the range, only then apply the whole plan."""
    if not boot.get('ready', True) or boot['kernel_end'] == 0:
        return ENOTREADY
    if count == 0 or va & (PAGE_SIZE - 1):
        return EINVAL
    if flags & ~MAP_ALLOWED & MASK64:
        return EINVAL
    end = KERNEL_BASE + (boot['kernel_end'] - boot['kernel_start'])
    if va < KERNEL_BASE or va >= end:
        return EINVAL
    if count >> 52:
        return EINVAL
    if va + count * PAGE_SIZE > end:
        return EINVAL
    status, _ = protect_pass(mem, boot, root, va, count, flags, False)
    if status != OK:
        return status
    return protect_pass(mem, boot, root, va, count, flags, True)[0]


def check_protect(chk, mem, man):
    chk.start('boot protect')
    root = man['kern_pml4']
    pt = man['kern_pt']
    boot = {'kernel_start': KERNEL_START, 'kernel_end': KERNEL_END}
    pristine = mem.snapshot()

    # the arena really maps the kernel base and the stack page above it
    chk.eq(walk(mem, root, PA_MASK, KERNEL_BASE)[1]['pa'], KERNEL_START,
           'the arena maps the kernel base')
    chk.eq(walk(mem, root, PA_MASK, KERNEL_BASE + 0x2000)[1]['pa'], STACK_START,
           'and the stack page')

    # the verification pass builds the whole plan and writes nothing
    status, plan = protect_pass(mem, boot, root, KERNEL_BASE, 2, MAP_RW, False)
    chk.eq(status, OK, 'the kernel pages resolve')
    chk.eq([slot for slot, _ in plan], [pt, pt + 8],
           'the plan names the two kernel leaves')
    chk.eq([entry for _, entry in plan],
           [KERNEL_START | PTE_P | PTE_RW,
            KERNEL_START + 0x1000 | PTE_P | PTE_RW],
           'RW is set and NX is cleared on the kernel pages')
    chk.eq(mem.q(pt), 0x00500001, 'the verification pass writes nothing')
    chk.eq(mem.q(pt + 8), 0x8000000000501003, 'nor does it touch the second leaf')

    # two calls: the kernel pages become RW+executable, the stack RW+NX
    chk.eq(boot_protect(mem, boot, root, KERNEL_BASE, 2, MAP_RW), OK,
           'the kernel pages are protected')
    chk.eq(mem.q(pt), 0x00500003,
           'the read only kernel page becomes writable')
    chk.eq(mem.q(pt + 8), 0x00501003,
           'the non executable kernel page becomes executable')
    chk.eq(mem.q(pt + 16), 0x00502001,
           'the stack leaf is untouched by the kernel call')
    chk.eq(boot_protect(mem, boot, root, KERNEL_BASE + 0x2000, 1,
                        MAP_RW | MAP_NX), OK, 'the stack page is protected')
    chk.eq(mem.q(pt + 16), 0x8000000000502003,
           'the stack page becomes writable and non executable')
    after = bytes(mem.image)
    chk.eq(boot_protect(mem, boot, root, KERNEL_BASE, 2, MAP_RW), OK,
           'a second kernel pass is harmless')
    chk.eq(boot_protect(mem, boot, root, KERNEL_BASE + 0x2000, 1,
                        MAP_RW | MAP_NX), OK, 'so is a second stack pass')
    chk.eq(bytes(mem.image), after, 'and no further byte changes')

    # rejected arguments write nothing
    mem.restore(pristine)
    for va, count, flags, why in (
            (KERNEL_BASE, 0, MAP_RW, 'a zero page count'),
            (KERNEL_BASE + 8, 1, MAP_RW, 'an unaligned start'),
            (KERNEL_BASE, 1, PTE_US, 'flags outside the allowed set'),
            (KERNEL_BASE - PAGE_SIZE, 1, MAP_RW, 'a start below the kernel base'),
            (KERNEL_BASE + 0x3000, 1, MAP_RW, 'a start past the kernel range'),
            (KERNEL_BASE, 1 << 52, MAP_RW, 'a page count that overflows'),
            (KERNEL_BASE, 4, MAP_RW, 'a range that ends past the image')):
        chk.eq(boot_protect(mem, boot, root, va, count, flags), EINVAL,
               '%s is refused' % why)
    chk.eq(mem.q(pt), 0x00500001, 'the argument refusals leave the image alone')

    # all or nothing: page 1 is a hole, so page 0 must not be written
    mem.restore(pristine)
    mem.setq(pt + 8, 0)
    chk.eq(boot_protect(mem, boot, root, KERNEL_BASE, 3, MAP_RW), EINVAL,
           'a hole in the range is refused')
    chk.eq(mem.q(pt), 0x00500001, 'and not one earlier leaf was written')

    # bit 7 of a PT entry is PAT, not PS: it stays a 4 KiB leaf, and the
    # frame it names no longer matches, so the pass reports EIO
    mem.restore(pristine)
    mem.setq(pt + 16, 0x00200000 | PTE_P | PTE_RW | PTE_PS)
    chk.eq(boot_protect(mem, boot, root, KERNEL_BASE, 3, MAP_RW), EIO,
           'the PS bit of a PT entry is PAT, not a large page')
    chk.eq(mem.q(pt), 0x00500001, 'and nothing was written')

    # a real 2 MiB leaf in the PD cannot be split and stops the whole pass
    pdpt = mem.q(root + 511 * 8) & PA_MASK
    pd = mem.q(pdpt + 510 * 8) & PA_MASK
    mem.restore(pristine)
    mem.setq(pd, 0x00200000 | PTE_P | PTE_RW | PTE_PS)
    chk.eq(boot_protect(mem, boot, root, KERNEL_BASE, 3, MAP_RW), ENOTSUP,
           'a 2 MiB leaf in the range is refused')
    chk.eq(mem.q(pt), 0x00500001, 'and not one earlier leaf was written')

    mem.restore(pristine)
    mem.setq(pt + 8, 0x00600000 | PTE_P | PTE_RW)
    chk.eq(boot_protect(mem, boot, root, KERNEL_BASE, 3, MAP_RW), EIO,
           'a leaf that maps the wrong frame is refused')
    chk.eq(mem.q(pt), 0x00500001, 'and nothing was written')

    mem.restore(pristine)
    mem.setq(root + 511 * 8, 0)
    chk.eq(boot_protect(mem, boot, root, KERNEL_BASE, 3, MAP_RW), EINVAL,
           'an unmapped kernel range is refused')

    mem.restore(pristine)
    mem.setq(root + 511 * 8, 0x100000000 | PTE_P | PTE_RW)
    chk.eq(boot_protect(mem, boot, root, KERNEL_BASE, 3, MAP_RW), ENOTSUP,
           'a table page above 4 GiB is refused')

    mem.restore(pristine)
    chk.eq(boot_protect(mem, dict(boot, kernel_end=0), root,
                        KERNEL_BASE, 3, MAP_RW), ENOTREADY,
           'a boot without a kernel range is refused')
    chk.eq(boot_protect(mem, dict(boot, ready=False), root,
                        KERNEL_BASE, 3, MAP_RW), ENOTREADY,
           'a pass before vmm_init is refused')
    chk.eq(mem.q(pt), 0x00500001, 'the refusals leave the image alone')


# ------------------------------------------------------------------
# section 6: machine code assertions over the real function bodies
# ------------------------------------------------------------------
# The host model above proves the algorithms, but a mutation that guts a
# function body (a hook that always succeeds, a map that skips its work)
# would leave the model green. These assertions look at the emitted bytes
# of every function: the instruction sequences the contract demands and
# the call/rip-reference graph between the functions.

def function_bodies(man, img, org):
    """Slice every function body out of the image by manifest address."""
    funcs = sorted((man[name], name)
                   for name in PUBLIC_ENTRIES + INTERNAL_HELPERS)
    bodies = {}
    for index, (addr, name) in enumerate(funcs):
        end = funcs[index + 1][0] if index + 1 < len(funcs) else man['data_start']
        bodies[name] = (addr, img[addr - org:end - org])
    return bodies


def call_targets(addr, body, known):
    """Every E8 rel32 whose computed target is a known function address."""
    out = []
    for i in range(len(body) - 4):
        if body[i] == 0xE8:
            rel = struct.unpack_from('<i', body, i + 1)[0]
            target = addr + i + 5 + rel
            if target in known:
                out.append(target)
    return out


def has_rip_ref(addr, body, target):
    """A rip-relative disp32 inside the body that resolves to the target."""
    for i in range(len(body) - 3):
        disp = struct.unpack_from('<i', body, i)[0]
        if addr + i + 4 + disp == target:
            return True
    return False


def has_abs_ref(body, target):
    """An absolute disp32 equal to the target (fasmg uses it when it fits)."""
    return struct.pack('<I', target & 0xFFFFFFFF) in body


def has_ref_in_range(addr, body, lo, hi):
    """A rip-relative or absolute reference landing inside [lo, hi)."""
    for i in range(len(body) - 3):
        disp = struct.unpack_from('<i', body, i)[0]
        if lo <= addr + i + 4 + disp < hi:
            return True
        if lo <= (disp & MASK64) < hi:
            return True
    return False


def check_bodies(chk, man, img, org, label):
    chk.start('code bodies %s' % label)
    bodies = function_bodies(man, img, org)
    addr_of = {man[name]: name for name in bodies}
    known = set(addr_of)

    def calls(name):
        addr, body = bodies[name]
        return [addr_of[t] for t in call_targets(addr, body, known)]

    def rip(name, symbol):
        addr, body = bodies[name]
        return (has_rip_ref(addr, body, man[symbol])
                or has_abs_ref(body, man[symbol]))

    def rip_field(name, symbol, size):
        addr, body = bodies[name]
        return has_ref_in_range(addr, body, man[symbol], man[symbol] + size)

    # every body that calls another function must reserve the 32 byte
    # shadow space and realign RSP (mm_enter, or sub rsp,40 by hand)
    for name, (addr, body) in sorted(bodies.items()):
        if call_targets(addr, body, known):
            chk.ok(b'\x48\x83\xEC\x28' in body,
                   '%s reserves shadow space before its calls' % name)

    # the failure hook must branch on the decremented countdown, never
    # on the flags of the hits increment
    body = bodies['mm_pmm_hook'][1]
    chk.ok(b'\xFF\xC8' in body, 'the hook decrements the countdown (dec eax)')
    chk.ok(b'\x85\xC0' in body, 'the hook tests the countdown (test eax,eax)')
    chk.ok(b'\xB8\xFE\xFF\xFF\xFF' in body, 'the hook can return ENOMEM')
    chk.ok(rip('mm_pmm_hook', 'test_fail'), 'the hook reads the countdown')
    chk.ok(rip('mm_pmm_hook', 'test_fail_hits'), 'the hook counts the hits')

    # vmm_map_page: ownership checks, offline build, rollback, invlpg
    chk.ok(b'\x41\x0F\x01\x3F' in bodies['vmm_map_page'][1],
           'map invalidates the leaf (invlpg [r15])')
    chk.ok('mm_pmm_alloc_table' in calls('vmm_map_page'),
           'map builds tables through the allocator')
    chk.ok('mm_pmm_drop_table' in calls('vmm_map_page'),
           'map rolls tables back through drop_table')
    chk.ok(rip('vmm_map_page', 'bitmap_allocated'),
           'map checks the allocated bitmap')
    chk.ok(rip('vmm_map_page', 'bitmap_pinned'), 'map checks the pinned bitmap')
    chk.ok(rip_field('vmm_map_page', 'vmm_state', VMM_STATE_SIZE),
           'map reads the vmm state')

    # unmap: resolve, clear the leaf, invlpg, report the physical address
    chk.ok(b'\x41\x0F\x01\x3C\x24' in bodies['vmm_unmap_page'][1],
           'unmap invalidates the leaf (invlpg [r12])')
    chk.ok('mm_vmm_resolve' in calls('vmm_unmap_page'),
           'unmap resolves the leaf')

    # vmm_init: the actual CR3, CPUID, EFER and the whole stack range
    body = bodies['vmm_init'][1]
    for pattern, what in ((b'\x0F\x20\xC0', 'reads the actual CR3'),
                          (b'\x0F\xA2', 'queries CPUID'),
                          (b'\x0F\x32', 'reads EFER (rdmsr)'),
                          (b'\x0F\x30', 'writes EFER (wrmsr)'),
                          (b'\x41\x0F\x01\x3E',
                           'invalidates every stack page (invlpg [r14])')):
        chk.ok(pattern in body, 'vmm_init %s' % what)
    chk.ok('mm_params_validate' in calls('vmm_init'),
           'vmm_init validates the parameters')
    chk.ok('mm_vmm_resolve' in calls('vmm_init'),
           'vmm_init walks the window slots')

    # the protect pass and its verify/apply call sites
    chk.ok(b'\x41\x0F\x01\x3E' in bodies['mm_vmm_protect_pass'][1],
           'the protect pass invalidates each applied leaf')
    chk.ok('mm_vmm_resolve' in calls('mm_vmm_protect_pass'),
           'the protect pass resolves every page')
    chk.eq(calls('vmm_boot_protect').count('mm_vmm_protect_pass'), 2,
           'boot_protect verifies then applies')

    # free page: the three bitmaps and the frozen error codes
    for symbol in ('bitmap_eligible', 'bitmap_pinned', 'bitmap_allocated'):
        chk.ok(rip('pmm_free_page', symbol), 'pmm_free_page reads %s' % symbol)
    body = bodies['pmm_free_page'][1]
    for pattern, what in ((b'\xB8\xFF\xFF\xFF\xFF', 'EINVAL'),
                          (b'\xB8\xFB\xFF\xFF\xFF', 'EPERM'),
                          (b'\xB8\xF8\xFF\xFF\xFF', 'EALREADY')):
        chk.ok(pattern in body, 'pmm_free_page can return %s' % what)

    # the allocation path and the RDI saving zero helper
    chk.ok('mm_pfn_alloc' in calls('pmm_alloc_page'),
           'pmm_alloc_page allocates a frame')
    chk.ok('mm_pfn_zero' in calls('pmm_alloc_page'),
           'pmm_alloc_page zeroes the page')
    body = bodies['mm_pfn_zero'][1]
    chk.ok(b'\x57' in body and b'\x5F' in body,
           'mm_pfn_zero saves and restores RDI')
    for helper in ('mm_pmm_hook', 'mm_pfn_alloc', 'mm_pfn_zero'):
        chk.ok(helper in calls('mm_pmm_alloc_table'),
               'alloc_table calls %s' % helper)

    # heap: the default alignment and the growth/rollback call graph
    chk.ok(b'\x41\xBE\x10\x00\x00\x00' in bodies['heap_alloc'][1],
           'heap alignment 0 defaults to 16')
    for helper in ('pmm_alloc_page', 'vmm_map_page', 'vmm_unmap_page',
                   'pmm_free_page'):
        chk.ok(helper in calls('mm_heap_grow'), 'heap growth calls %s' % helper)
    chk.ok(rip('mm_heap_grow', 'heap_grows'), 'heap growth counts successes')


# ------------------------------------------------------------------
# section 7: in-process execution of the real machine code (x86_64)
# ------------------------------------------------------------------
# The image is assembled a third time at the address of an anonymous
# mapping, copied there, and the code pages turn RX while the data block
# stays RW. A small thunk sets RCX/RDX/R8, calls the function and stores
# RAX/RDX/R8 back into ctypes cells. Only paths that never touch the
# identity map, privileged instructions or the high half window are run:
# the hook, the bitmaps, the PFN helpers, free/stats/drop, the heap
# observers and the refusal paths of the VMM (with a fake zeroed PML4).

def check_execution(chk):
    chk.start('in-process execution')
    libc = ctypes.CDLL(None)
    libc.mmap.restype = ctypes.c_void_p
    libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int, ctypes.c_long]
    libc.mprotect.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    PROT_RW, PROT_RX = 3, 5
    MAP_PRIVATE_ANON = 0x1002           # macOS MAP_PRIVATE | MAP_ANON
    bad = ctypes.c_void_p(-1).value
    area = libc.mmap(None, 0x400000, PROT_RW, MAP_PRIVATE_ANON, -1, 0)
    if area in (None, bad):
        chk.ok(False, 'the execution arena could not be mapped')
        return
    thunk = libc.mmap(None, 0x1000, PROT_RW, MAP_PRIVATE_ANON, -1, 0)
    if thunk in (None, bad):
        libc.munmap(area, 0x400000)
        chk.ok(False, 'the thunk page could not be mapped')
        return
    try:
        img = assemble_exec()
        chk.ok(len(img) <= 0x400000, 'the execution image fits the arena')
        man = load_exec_manifest(img)
        chk.eq(man['magic'], int.from_bytes(b'K64MMEX1', 'little'),
               'the execution probe magic')
        # RIP-relative memory operands relocate themselves, but address
        # loads ("mov reg, label") hold the org-0 offset as an immediate.
        # At org 0 fasmg emits the short REX.W C7 /0 imm32 form, which
        # cannot hold the arena address: each such load is rewritten in
        # place as a jmp to a stub page that loads the relocated address
        # with the imm64 form and jumps back. A rare imm64 form load is
        # patched directly. Only qword aligned immediates pointing into
        # the data block are treated as address loads.
        ds, de = man['data_start'], len(img)
        code = bytearray(img)
        stub_off = (len(img) + PAGE_SIZE - 1) & ~(PAGE_SIZE - 1)
        stub = bytearray()
        i = len(EXEC_MANIFEST_NAMES) * 8
        while i < ds - 10:
            if code[i] in (0x48, 0x49) and 0xB8 <= code[i + 1] <= 0xBF:
                imm = struct.unpack_from('<Q', code, i + 2)[0]
                if ds <= imm < de:
                    struct.pack_into('<Q', code, i + 2, imm + area)
                    i += 10
                    continue
            i += 1
        i = len(EXEC_MANIFEST_NAMES) * 8
        while i < ds - 7:
            if (code[i] in (0x48, 0x49) and code[i + 1] == 0xC7
                    and 0xC0 <= code[i + 2] <= 0xC7):
                imm = struct.unpack_from('<i', code, i + 3)[0] & MASK64
                if ds <= imm < de and imm % 8 == 0:
                    # REX.B extends the ModRM reg field: 49 C7 C4 is r12
                    reg = (code[i + 2] - 0xC0) + (code[i] & 1) * 8
                    here = stub_off + len(stub)
                    stub += bytes((0x48 | (reg >> 3), 0xB8 | (reg & 7)))
                    stub += struct.pack('<Q', imm + area)
                    stub += b'\xE9' + struct.pack('<i', (i + 7) - (here + 15))
                    code[i:i + 7] = (b'\xE9'
                                     + struct.pack('<i', here - (i + 5))
                                     + b'\x90\x90')
                    i += 7
                    continue
            i += 1
        chk.ok(stub_off + len(stub) <= 0x400000,
               'the relocation stubs fit the arena')
        ctypes.memmove(area, bytes(code), len(code))
        if stub:
            ctypes.memmove(area + stub_off, bytes(stub), len(stub))
        # the manifest holds org-0 offsets; the image now lives at the arena
        for key in man:
            if key != 'magic':
                man[key] += area
        chk.eq(libc.mprotect(area, man['data_start'] - area, PROT_RX), 0,
               'the code pages become RX')
        if stub:
            chk.eq(libc.mprotect(area + stub_off,
                                 (len(stub) + PAGE_SIZE - 1)
                                 & ~(PAGE_SIZE - 1), PROT_RX), 0,
                   'the relocation stub page becomes RX')

        def pokeq(addr, value):
            ctypes.c_uint64.from_address(addr).value = value & MASK64

        def peekq(addr):
            return ctypes.c_uint64.from_address(addr).value

        def poked(addr, value):
            ctypes.c_uint32.from_address(addr).value = value & 0xFFFFFFFF

        def peekd(addr):
            return ctypes.c_uint32.from_address(addr).value

        def set_bit(base, pfn):
            word = base + (pfn // 64) * 8
            pokeq(word, peekq(word) | (1 << (pfn % 64)))

        def clear_bit(base, pfn):
            word = base + (pfn // 64) * 8
            pokeq(word, peekq(word) & ~(1 << (pfn % 64)) & MASK64)

        def bit(base, pfn):
            return (peekq(base + (pfn // 64) * 8) >> (pfn % 64)) & 1

        def call(fn, rcx=0, rdx=0, r8=0):
            rax_out = ctypes.c_uint64(0)
            rdx_out = ctypes.c_uint64(0)
            r8_out = ctypes.c_uint64(0)
            code = b'\x48\xB9' + struct.pack('<Q', rcx & MASK64)
            code += b'\x48\xBA' + struct.pack('<Q', rdx & MASK64)
            code += b'\x49\xB8' + struct.pack('<Q', r8 & MASK64)
            code += b'\x48\xB8' + struct.pack('<Q', fn)
            code += b'\x48\x83\xEC\x08'      # RSP 16 aligned at the call
            code += b'\xFF\xD0'
            code += b'\x48\x83\xC4\x08'
            code += b'\x49\xBA' + struct.pack('<Q', ctypes.addressof(rdx_out))
            code += b'\x49\x89\x12'
            code += b'\x49\xBA' + struct.pack('<Q', ctypes.addressof(rax_out))
            code += b'\x49\x89\x02'
            code += b'\x49\xBA' + struct.pack('<Q', ctypes.addressof(r8_out))
            code += b'\x49\x89\x02'
            code += b'\xC3'
            libc.mprotect(thunk, 0x1000, PROT_RW)
            ctypes.memmove(thunk, code, len(code))
            libc.mprotect(thunk, 0x1000, PROT_RX)
            ctypes.CFUNCTYPE(None)(thunk)()
            eax = rax_out.value & 0xFFFFFFFF
            if eax & 0x80000000:
                eax -= 1 << 32
            return eax, rdx_out.value, r8_out.value

        # --- the failure hook really fires and disarms itself ---
        hook = man['mm_pmm_hook']
        fail_addr, hits_addr = man['test_fail'], man['test_fail_hits']
        poked(fail_addr, 0)
        poked(hits_addr, 0)
        chk.eq(call(hook)[0], OK, 'the hook allows when disarmed')
        poked(fail_addr, 1)
        chk.eq(call(hook)[0], ENOMEM, 'armed with 1 the hook fires at once')
        chk.eq(peekd(fail_addr), 0, 'the countdown reached zero')
        chk.eq(peekd(hits_addr), 1, 'one hit was counted')
        poked(fail_addr, 3)
        poked(hits_addr, 0)
        chk.eq([call(hook)[0] for _ in range(3)], [OK, OK, ENOMEM],
               'the hook fires exactly on the N-th call')
        chk.eq(peekd(hits_addr), 3, 'every armed call was counted')
        chk.eq(peekd(fail_addr), 0, 'the hook disarms itself')
        chk.eq(call(hook)[0], OK, 'and stays disarmed')

        # --- bitmap helpers ---
        eligible = man['bitmap_eligible']
        allocated = man['bitmap_allocated']
        pinned = man['bitmap_pinned']
        call(man['mm_bitmap_fill'], rcx=pinned)
        chk.eq(call(man['mm_bitmap_count'], rcx=pinned)[1], PFN_COUNT,
               'a filled bitmap counts 1M bits')
        call(man['mm_bitmap_clear'], rcx=pinned)
        chk.eq(call(man['mm_bitmap_count'], rcx=pinned)[1], 0,
               'a cleared bitmap counts zero')
        for base in (eligible, allocated):
            call(man['mm_bitmap_clear'], rcx=base)

        # --- next-fit search and the internal frame allocator ---
        nf = man['mm_pfn_next_free']
        chk.eq(signed(call(nf, rcx=0)[1]), -1, 'nothing is free in an empty space')
        set_bit(eligible, 0x900)
        chk.eq(call(nf, rcx=0)[1], 0x900, 'the only eligible page is found')
        chk.eq(call(nf, rcx=0x901)[1], 0x900, 'the search wraps once')
        set_bit(allocated, 0x900)
        chk.eq(signed(call(nf, rcx=0)[1]), -1, 'an allocated page is not free')
        clear_bit(allocated, 0x900)
        set_bit(pinned, 0x900)
        chk.eq(signed(call(nf, rcx=0)[1]), -1, 'a pinned page is not free')
        clear_bit(pinned, 0x900)

        pmm = man['pmm_state']
        pokeq(pmm + 0, 1)               # ready
        pokeq(pmm + 8, PHYS_LIMIT)      # managed_limit
        pokeq(pmm + 16, 1)              # total
        pokeq(pmm + 24, 1)              # free
        pokeq(pmm + 32, 0)              # allocated
        pokeq(pmm + 40, 0)              # reserved
        pokeq(pmm + 48, 0x900)          # hint
        # the internal helper has no status word: success is rdx != NONE
        _, rdx, _ = call(man['mm_pfn_alloc'])
        chk.ok(rdx != MASK64, 'pfn_alloc succeeds')
        chk.eq(rdx, 0x900, 'pfn_alloc returns the PFN')
        chk.eq(bit(allocated, 0x900), 1, 'the frame is marked allocated')
        chk.eq(peekq(pmm + 32), 1, 'the allocated counter advanced')
        chk.eq(peekq(pmm + 24), 0, 'the free counter dropped')
        chk.eq(peekq(pmm + 48), 0x901, 'the hint moved past the page')
        chk.eq(signed(call(man['mm_pfn_alloc'])[1]), -1,
               'a second allocation finds nothing')

        # --- the public release and its frozen error order ---
        free = man['pmm_free_page']
        chk.eq(call(free, rcx=0x900001)[0], EINVAL, 'a misaligned release is EINVAL')
        chk.eq(call(free, rcx=PHYS_LIMIT)[0], EINVAL, 'an out of range release is EINVAL')
        chk.eq(call(free, rcx=0x800000)[0], EPERM, 'a non eligible release is EPERM')
        set_bit(pinned, 0x900)
        chk.eq(call(free, rcx=0x900000)[0], EPERM, 'a pinned release is EPERM')
        clear_bit(pinned, 0x900)
        status, rdx, _ = call(free, rcx=0x900000)
        chk.eq(status, OK, 'an allocated page is released')
        chk.eq(rdx, 0, 'release returns rdx = 0')
        chk.eq(bit(allocated, 0x900), 0, 'the allocated bit is gone')
        chk.eq(peekq(pmm + 24), 1, 'the free counter came back')
        chk.eq(peekq(pmm + 48), 0x900, 'the hint moved back to the released page')
        chk.eq(call(free, rcx=0x900000)[0], EALREADY, 'a double release is EALREADY')
        pokeq(pmm + 0, 0)
        chk.eq(call(free, rcx=0x900000)[0], ENOTREADY, 'release before init is ENOTREADY')
        pokeq(pmm + 0, 1)

        # --- statistics: six frozen fields, cross checked counters ---
        set_bit(eligible, 0x901)
        set_bit(eligible, 0x902)
        set_bit(allocated, 0x901)
        set_bit(pinned, 0x902)
        pokeq(pmm + 16, 3)
        pokeq(pmm + 24, 1)
        pokeq(pmm + 32, 1)
        pokeq(pmm + 40, 1)
        pokeq(pmm + 48, 0x905)
        scratch = man['pmm_stats_scratch']
        status, rdx, _ = call(man['pmm_get_stats'], rcx=scratch)
        chk.eq(status, OK, 'get_stats succeeds')
        chk.eq([peekq(scratch + off) for off in (0, 8, 16, 24, 32, 40)],
               [PHYS_LIMIT, 3, 1, 1, 1, 0x905],
               'the six fields: limit/total/free/allocated/reserved/hint')
        pokeq(pmm + 16, 4)
        chk.eq(call(man['pmm_get_stats'], rcx=scratch)[0], EIO,
               'a broken counter is EIO')
        pokeq(pmm + 16, 3)
        pokeq(pmm + 0, 0)
        chk.eq(call(man['pmm_get_stats'], rcx=scratch)[0], ENOTREADY,
               'stats before init are ENOTREADY')
        pokeq(pmm + 0, 1)
        chk.eq(call(man['pmm_get_stats'], rcx=0)[0], EFAULT,
               'a null stats buffer is EFAULT')

        # --- dropping a pinned table page ---
        drop = man['mm_pmm_drop_table']
        chk.eq(call(drop, rcx=0x902001)[0], EINVAL, 'a misaligned drop is EINVAL')
        chk.eq(call(drop, rcx=PHYS_LIMIT)[0], EINVAL, 'an out of range drop is EINVAL')
        chk.eq(call(drop, rcx=0x901000)[0], EIO, 'dropping an allocated page is EIO')
        chk.eq(call(drop, rcx=0x902000)[0], OK, 'a pinned table page is dropped')
        chk.eq(bit(pinned, 0x902), 0, 'the pin is gone')
        chk.eq(peekq(pmm + 40), 0, 'the reserved counter dropped')
        chk.eq(peekq(pmm + 24), 2, 'the free counter advanced')
        chk.eq(peekq(pmm + 48), 0x902, 'the hint moved back to the dropped page')
        chk.eq(call(drop, rcx=0x902000)[0], EIO, 'a second drop is EIO')

        # --- heap observers and the refused allocations ---
        heap_state = man['heap_state']
        pokeq(heap_state + 0, 1)
        pokeq(heap_state + 8, HEAP_BASE)
        pokeq(heap_state + 16, HEAP_BASE + 0x100)
        pokeq(heap_state + 24, HEAP_BASE + HEAP_INITIAL)
        pokeq(heap_state + 32, HEAP_LIMIT)
        hscratch = man['heap_stats_scratch']
        status, rdx, _ = call(man['heap_get_stats'], rcx=hscratch)
        chk.eq(status, OK, 'heap_get_stats succeeds')
        chk.eq([peekq(hscratch + off) for off in (0, 8, 16, 24)],
               [HEAP_BASE, HEAP_BASE + 0x100, HEAP_BASE + HEAP_INITIAL,
                HEAP_LIMIT],
               'the four heap fields are absolute addresses')
        pokeq(heap_state + 0, 0)
        chk.eq(call(man['heap_get_stats'], rcx=hscratch)[0], ENOTREADY,
               'heap stats before init are ENOTREADY')
        chk.eq(call(man['heap_get_stats'], rcx=0)[0], EFAULT,
               'a null heap stats buffer is EFAULT')
        status, rdx, _ = call(man['heap_free'], rcx=HEAP_BASE)
        chk.eq((status, rdx), (ENOTSUP, 0), 'heap_free is ENOTSUP with rdx = 0')
        chk.eq(call(man['heap_alloc'], rcx=16, rdx=16)[0], ENOTREADY,
               'alloc before init is ENOTREADY')
        pokeq(heap_state + 0, 1)
        chk.eq(call(man['heap_alloc'], rcx=0, rdx=16)[0], EINVAL,
               'a zero size is EINVAL')
        chk.eq(call(man['heap_alloc'], rcx=16, rdx=8)[0], EINVAL,
               'alignment 8 is EINVAL')
        chk.eq(call(man['heap_alloc'], rcx=16, rdx=24)[0], EINVAL,
               'alignment 24 is EINVAL')
        chk.eq(call(man['heap_alloc'], rcx=16, rdx=8192)[0], EINVAL,
               'alignment 8192 is EINVAL')
        chk.eq(call(man['heap_alloc'], rcx=HEAP_MAX + 1, rdx=16)[0], ENOMEM,
               'above the window is ENOMEM')
        pokeq(heap_state + 0, 0)

        # --- VMM refusal paths against a fake zeroed PML4 ---
        vmm = man['vmm_state']
        root = man['desc_scratch']
        ctypes.memset(root, 0, PAGE_SIZE)
        poked(vmm + 0, 1)
        poked(vmm + 4, PHYS_BITS)
        pokeq(vmm + 8, root)
        pokeq(vmm + 16, PA_MASK)
        pokeq(vmm + 24, WINDOW_BASE)
        pokeq(vmm + 32, WINDOW_LIMIT)
        qbuf = man['selftest_query']
        query = man['vmm_query']
        ctypes.memset(qbuf, 0xAA, 40)
        status, rdx, _ = call(query, rcx=WINDOW_BASE, rdx=qbuf)
        chk.eq(status, ENOENT, 'query of an unmapped window is ENOENT')
        chk.eq(ctypes.string_at(qbuf, 40), bytes(40),
               'the failed query zeroes the record')
        ctypes.memset(qbuf, 0xAA, 40)
        chk.eq(call(query, rcx=0x0000800000000000, rdx=qbuf)[0], EINVAL,
               'a non canonical query is EINVAL')
        chk.eq(ctypes.string_at(qbuf, 40), bytes(40),
               'the canonical refusal zeroes the record')
        chk.eq(call(query, rcx=WINDOW_BASE, rdx=0)[0], EFAULT,
               'a null query record is EFAULT')
        unmap = man['vmm_unmap_page']
        status, rdx, _ = call(unmap, rcx=WINDOW_BASE)
        chk.eq((status, rdx), (ENOENT, 0), 'unmap of a hole is ENOENT, rdx = 0')
        chk.eq(call(unmap, rcx=KERNEL_BASE)[0], EPERM,
               'unmap outside the window is EPERM')
        chk.eq(call(unmap, rcx=WINDOW_BASE + 8)[0], EINVAL,
               'a misaligned unmap is EINVAL')
        mapf = man['vmm_map_page']
        chk.eq(call(mapf, rcx=KERNEL_BASE, rdx=0x900000,
                    r8=MAP_RW | MAP_NX)[0], EPERM,
               'map outside the window is EPERM')
        chk.eq(call(mapf, rcx=WINDOW_BASE + 8, rdx=0x900000,
                    r8=MAP_RW | MAP_NX)[0], EINVAL, 'a misaligned map VA is EINVAL')
        chk.eq(call(mapf, rcx=WINDOW_BASE, rdx=0x900001,
                    r8=MAP_RW | MAP_NX)[0], EINVAL, 'a misaligned map PA is EINVAL')
        chk.eq(call(mapf, rcx=WINDOW_BASE, rdx=0x900000, r8=PTE_US)[0],
               EINVAL, 'map flags outside the set are EINVAL')
        chk.eq(call(mapf, rcx=WINDOW_BASE, rdx=0x900000, r8=MAP_RW)[0],
               EINVAL, 'writable plus executable is EINVAL')
        chk.eq(call(mapf, rcx=WINDOW_BASE, rdx=0x900000,
                    r8=MAP_RW | MAP_NX)[0], EPERM, 'an unowned page is EPERM')
        set_bit(allocated, 0x900)
        set_bit(pinned, 0x900)
        chk.eq(call(mapf, rcx=WINDOW_BASE, rdx=0x900000,
                    r8=MAP_RW | MAP_NX)[0], EPERM, 'a pinned page is EPERM')
        clear_bit(pinned, 0x900)
        # a PS bit in the PML4 slot stops the walk before any allocation
        slot = root + ((WINDOW_BASE >> 39) & 0x1FF) * 8
        pokeq(slot, PTE_P | PTE_RW | PTE_PS)
        chk.eq(call(mapf, rcx=WINDOW_BASE, rdx=0x900000,
                    r8=MAP_RW | MAP_NX)[0], ENOTSUP,
               'a PS bit in the PML4 stops the map')
        chk.eq(peekq(vmm + 40), 0, 'no table was allocated by the refused map')
        chk.eq(call(query, rcx=WINDOW_BASE, rdx=qbuf)[0], EINVAL,
               'a PS bit in the PML4 is EINVAL for resolve')
        chk.eq(call(unmap, rcx=WINDOW_BASE)[0], EINVAL, 'and for unmap')
        pokeq(slot, 0)
        # an armed hook fails the first table allocation: the map must
        # refuse with ENOMEM and publish nothing (the hook fires before
        # any page is zeroed, so this stays off the identity map)
        poked(fail_addr, 1)
        hits_before = peekd(hits_addr)
        chk.eq(call(mapf, rcx=WINDOW_BASE, rdx=0x900000,
                    r8=MAP_RW | MAP_NX)[0], ENOMEM,
               'the armed hook fails the map at the first table')
        chk.eq(peekd(fail_addr), 0, 'the hook disarmed after the refusal')
        chk.eq(peekd(hits_addr), hits_before + 1,
               'the refusal counted one hit')
        chk.eq(peekq(slot), 0, 'the refused map published no table')
        chk.eq(peekq(vmm + 40), 0, 'the refused map kept the table count')

        # --- boot_protect argument validation and the hole refusal ---
        boot = man['boot_state']
        pokeq(boot + 32, 0x500000)      # kernel_start
        pokeq(boot + 40, 0x503000)      # kernel_end
        prot = man['vmm_boot_protect']
        for rcx, rdx, r8, why in (
                (KERNEL_BASE, 0, MAP_RW, 'a zero page count'),
                (KERNEL_BASE + 8, 1, MAP_RW, 'an unaligned start'),
                (KERNEL_BASE, 1, PTE_US, 'flags outside the allowed set'),
                (KERNEL_BASE - PAGE_SIZE, 1, MAP_RW, 'a start below the base'),
                (KERNEL_BASE + 0x3000, 1, MAP_RW, 'a start past the range'),
                (KERNEL_BASE, 1 << 52, MAP_RW, 'a count that overflows'),
                (KERNEL_BASE, 4, MAP_RW, 'a range past the image')):
            chk.eq(call(prot, rcx=rcx, rdx=rdx, r8=r8)[0], EINVAL,
                   'boot_protect refuses %s' % why)
        chk.eq(call(prot, rcx=KERNEL_BASE, rdx=3, r8=MAP_RW)[0], EINVAL,
               'a hole in the kernel range is EINVAL')
        pokeq(boot + 40, 0)
        chk.eq(call(prot, rcx=KERNEL_BASE, rdx=3, r8=MAP_RW)[0], ENOTREADY,
               'no kernel range is ENOTREADY')
    finally:
        libc.munmap(thunk, 0x1000)
        libc.munmap(area, 0x400000)


def run_exec_child():
    """Entry point of the x86_64 child: run the execution checks alone."""
    chk = Checker()
    check_execution(chk)
    print('EXECCHK %d %d' % (chk.checks, len(chk.failures)))
    for line in chk.failures:
        print('EXECFAIL %s' % line)
    return 0 if not chk.failures else 1


def run_exec_parent(chk):
    """Merge the execution checks, re-executing under Rosetta if needed."""
    if platform.machine() == 'x86_64':
        sub = Checker()
        check_execution(sub)
        chk.checks += sub.checks
        chk.failures.extend(sub.failures)
        return
    chk.start('in-process execution (x86_64 child)')
    cmd = ['/usr/bin/arch', '-x86_64', '/usr/bin/python3',
           str(TESTS / 'check.py'), '--exec-child']
    result = subprocess.run(cmd, env=dict(os.environ), cwd=str(ROOT),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    text = result.stdout.decode('utf-8', 'replace')
    total = None
    for line in text.splitlines():
        if line.startswith('EXECCHK '):
            _, count, failed = line.split()
            total = int(count)
        elif line.startswith('EXECFAIL '):
            chk.failures.append('in-process execution: ' + line[9:])
    if total is None:
        chk.ok(False, 'the x86_64 execution child did not report:\n' + text)
        return
    chk.checks += total


# ------------------------------------------------------------------
# driver
# ------------------------------------------------------------------

def main(argv):
    if '--exec-child' in argv:
        return run_exec_child()
    keep = '--keep' in argv
    verbose = '--verbose' in argv
    chk = Checker()
    print('stage 2 memory management, independent layout tests')
    images = {}
    for org, label in ((0, 'flat'), (KERNEL_BASE, 'kernel base')):
        tag = 'flat' if org == 0 else 'kernel'
        img, text = assemble(org, tag)
        images[org] = img
        man = load_manifest(img)
        check_image(chk, man, img, org, label)
        check_bodies(chk, man, img, org, label)
        if verbose:
            print(text)
    man = load_manifest(images[0])
    check_contract(chk, man)
    mem = Mem(images[0])
    check_pmm(chk, mem, man)
    check_fixtures(chk, mem, man)
    check_query(chk, mem, man)
    check_map(chk, mem, man)
    check_unmap(chk, mem, man)
    check_protect(chk, mem, man)
    check_heap(chk, mem, man)
    run_exec_parent(chk)
    ok = chk.report()
    if not keep:
        for path in sorted(BUILD.glob('mmprobe-*')):
            path.unlink()
    print('layout tests %s' % ('PASSED' if ok else 'FAILED'))
    return 0 if ok else 1


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(2)
