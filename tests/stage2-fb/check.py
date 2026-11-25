#!/usr/bin/env python3
# Copyright (C) KolibriOS team 2025-2025. All rights reserved.
# Distributed under terms of the GNU General Public License v2
#
# Host model tests for the GUI-G1 video module, task 77.
#
# The module cannot be executed here: it needs a real CR3, the MM
# allocator and a linear framebuffer. The tests therefore work on the
# real assembler output only:
#
#  1. layout.asm is assembled twice, once at 0 and once at the kernel
#     base; the manifest, the contract constants, the record offsets,
#     the entry prologues, the font subset, the pattern data and the
#     zeroed data block are inspected in the emitted bytes;
#  2. every contract clause of the function bodies is pinned by static
#     byte assertions: the clipping compares, the stride = pitch * 4 row
#     offsets, the BGRX store order, the glyph index * 16, the mapping
#     loop bound and the format == 1 refusal;
#  3. a pure Python model of the frozen pattern spec answers probe
#     pixels against hand written expectations, the forensic base of
#     the QEMU screendump assertions of the integration task;
#  4. negative fixtures: purpose-broken copies of the module sources
#     must every one be caught by the static assertions.
#
# Usage:
#   python3 tests/stage2-fb/check.py [--keep] [--verbose]
#                                    [--root DIR] [--out DIR] [--no-fixtures]
#
# Environment:
#   FASMG     fasm2 host executable (default: ../fasm2 bootstrap build)
#   FASM2_DIR fasm2 checkout, used for the include directory
#
# No QEMU, no machine code execution, no repository file outside
# build/stage2-fb/ is touched.

import hashlib
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / 'tests' / 'stage2-fb'
BUILD = ROOT / 'build' / 'stage2-fb'

# ------------------------------------------------------------------
# the contract, a private hand written copy for the tests
# ------------------------------------------------------------------

PAGE_SIZE = 0x1000
KERNEL_BASE = 0xFFFFFFFF80000000
MASK64 = 0xFFFFFFFFFFFFFFFF

WINDOW_BASE = 0xFFFFC00000000000
WINDOW_LIMIT = 0xFFFFC20000000000
HEAP_LIMIT = 0xFFFFC00001000000
MMIO_LAPIC_VA = 0xFFFFC00002000000
CORE_TEST_SLOT = 386

FB_VA = 0xFFFFC00001000000
FB_VA_LIMIT = MMIO_LAPIC_VA
FB_MAX_SIZE = 0x1000000                 # 16 MiB
FB_MAX_PAGES = 4096
MAX_WIDTH = 2560
MAX_HEIGHT = 1600
BPP = 4

FMT_RGBX = 0
FMT_BGRX = 1
FMT_BITMASK = 2
FMT_BLTONLY = 3
FLAG_FRAMEBUFFER = 0x02
BOOTINFO_SIZE = 208

OK = 0
EINVAL = -1
ENOTSUP = -6
ENOTREADY = -7
EALREADY = -8
EIO = -9

# BootInfo offsets, written out again on purpose
EXPECT_OFFSETS = {
    'VIDEO_BOOTINFO.flags': 16,
    'VIDEO_BOOTINFO.gop_framebuffer_addr': 88,
    'VIDEO_BOOTINFO.gop_framebuffer_size': 96,
    'VIDEO_BOOTINFO.gop_width': 104,
    'VIDEO_BOOTINFO.gop_height': 108,
    'VIDEO_BOOTINFO.gop_pixel_format': 112,
    'VIDEO_BOOTINFO.gop_pixels_per_line': 116,
    'VIDEO_FB_STATE.ready': 0,
    'VIDEO_FB_STATE.mapped_pages': 4,
    'VIDEO_FB_STATE.width': 8,
    'VIDEO_FB_STATE.height': 12,
    'VIDEO_FB_STATE.pitch': 16,
    'VIDEO_FB_STATE.stride': 20,
    'VIDEO_FB_STATE.format': 24,
    'VIDEO_FB_STATE.fb_pa': 32,
    'VIDEO_FB_STATE.fb_va': 40,
    'VIDEO_FB_STATE.fb_size': 48,
}

FB_STATE_SIZE = 64

FONT_WIDTH = 8
FONT_HEIGHT = 16
FONT_STRIDE = 16
FONT_FIRST = 0x20
FONT_LAST = 0x7E
FONT_GLYPHS = 95
FONT_BYTES = 1520
# sha256 of glyphs 0x20..0x7E of kolibrios/kernel/trunk/gui/charUni.mt
FONT_SHA256 = 'a61bf766ab3e8ab088e961a39be6116b40b1f29093c73eef86f6cdcef18739f4'

# task-108: the independent bit-order oracle. The charUni.mt truth is
# LSB-first (bit0 = leftmost pixel), proven by the 32 bit drawChar
# renderer in kolibrios/kernel/trunk/gui/font.inc: 'bsf eax,edx' finds
# the LOWEST set bit and multiplies that bit index by the pixel pitch.
# The two glyphs below are a hand written copy of the RAW charUni.mt
# bytes ('K' then '1', code point * 16 indexing), pinned by sha256 so
# the oracle survives an unreachable baseline. It is independent of
# both the assembly renderer and the Python spec model: a renderer and
# a model that are wrong in lockstep can no longer pass.
ORACLE_TRUTH = bytes.fromhex(
    '00 00 42 22 12 0a 06 06 0a 12 22 42 00 00 00 00'   # 'K' 0x4B
    '00 00 10 18 14 10 10 10 10 10 10 10 00 00 00 00')  # '1' 0x31
ORACLE_SHA256 = '2aa1d9ca8e4d38341800d0232b8ff565bfa09db43263c4019a133d33004613ff'

# the frozen GUI-G1 pattern spec, see kernel/video/api.inc
ST_WIDTH = 1024
ST_HEIGHT = 768
ST_BARS = 8
ST_BAR_W = 128
ST_COLORS = [0xFF0000, 0x00FF00, 0x0000FF, 0xFFFF00,
             0x00FFFF, 0xFF00FF, 0xFFFFFF, 0x000000]
ST_BORDER = 0xFFFFFF
ST_RECT_X, ST_RECT_Y = 412, 284
ST_RECT_W, ST_RECT_H = 200, 200
ST_RECT_COLOR = 0x808080
ST_TEXT_X, ST_TEXT_Y = 8, 8
ST_TEXT_COLOR = 0xFFFFFF
ST_TEXT = b'K64 GUI-G1\x00'

EXPECT_CONSTANTS = {
    'VIDEO_FB_VA': FB_VA,
    'VIDEO_FB_VA_LIMIT': FB_VA_LIMIT,
    'VIDEO_FB_MAX_SIZE': FB_MAX_SIZE,
    'VIDEO_FB_MAX_PAGES': FB_MAX_PAGES,
    'VIDEO_MAX_WIDTH': MAX_WIDTH,
    'VIDEO_MAX_HEIGHT': MAX_HEIGHT,
    'VIDEO_BPP': BPP,
    'VIDEO_FMT_BGRX': FMT_BGRX,
    'VIDEO_FLAG_FRAMEBUFFER': FLAG_FRAMEBUFFER,
    'VIDEO_BOOTINFO_SIZE': BOOTINFO_SIZE,
    'VIDEO_FB_STATE_size': FB_STATE_SIZE,
    'VIDEO_FONT_WIDTH': FONT_WIDTH,
    'VIDEO_FONT_HEIGHT': FONT_HEIGHT,
    'VIDEO_FONT_STRIDE': FONT_STRIDE,
    'VIDEO_FONT_FIRST': FONT_FIRST,
    'VIDEO_FONT_LAST': FONT_LAST,
    'VIDEO_FONT_GLYPHS': FONT_GLYPHS,
    'VIDEO_FONT_BYTES': FONT_BYTES,
    'VIDEO_ST_WIDTH': ST_WIDTH,
    'VIDEO_ST_HEIGHT': ST_HEIGHT,
    'VIDEO_ST_BARS': ST_BARS,
    'VIDEO_ST_BAR_W': ST_BAR_W,
    'VIDEO_ST_RECT_X': ST_RECT_X,
    'VIDEO_ST_RECT_Y': ST_RECT_Y,
    'VIDEO_ST_RECT_W': ST_RECT_W,
    'VIDEO_ST_RECT_H': ST_RECT_H,
    'VIDEO_ST_RECT_COLOR': ST_RECT_COLOR,
    'VIDEO_ST_BORDER': ST_BORDER,
    'VIDEO_ST_TEXT_X': ST_TEXT_X,
    'VIDEO_ST_TEXT_Y': ST_TEXT_Y,
    'VIDEO_ST_TEXT_COLOR': ST_TEXT_COLOR,
    'VIDEO_ST_TEXT_LEN': len(ST_TEXT) - 1,
    'VIDEO_OK': OK,
    'VIDEO_EINVAL': EINVAL,
    'VIDEO_ENOTSUP': ENOTSUP,
    'VIDEO_ENOTREADY': ENOTREADY,
    'VIDEO_EALREADY': EALREADY,
    'VIDEO_EIO': EIO,
    'MM_MMIO_LAPIC_VA': MMIO_LAPIC_VA,
    'MM_HEAP_LIMIT': HEAP_LIMIT,
    'VIDEO_PAGE_SIZE': PAGE_SIZE,
}

# manifest order, identical to the list in layout.asm
MANIFEST_NAMES = """
magic version org
video_data_start video_data_end video_fb_state video_selftest_mask
mm_data_start mm_data_end
video_init video_putpixel video_fill_rect video_blit_rect video_readback_rect
video_puts video_selftest video_bgrx video_clip_rect vmm_map_mmio
video_font video_font_end video_st_colors video_st_text
VIDEO_FB_VA VIDEO_FB_VA_LIMIT VIDEO_FB_MAX_SIZE VIDEO_FB_MAX_PAGES
VIDEO_MAX_WIDTH VIDEO_MAX_HEIGHT VIDEO_BPP VIDEO_FMT_BGRX
VIDEO_FLAG_FRAMEBUFFER VIDEO_BOOTINFO_SIZE
VIDEO_BOOTINFO.flags VIDEO_BOOTINFO.gop_framebuffer_addr
VIDEO_BOOTINFO.gop_framebuffer_size VIDEO_BOOTINFO.gop_width
VIDEO_BOOTINFO.gop_height VIDEO_BOOTINFO.gop_pixel_format
VIDEO_BOOTINFO.gop_pixels_per_line
VIDEO_FB_STATE.ready VIDEO_FB_STATE.mapped_pages VIDEO_FB_STATE.width
VIDEO_FB_STATE.height VIDEO_FB_STATE.pitch VIDEO_FB_STATE.stride
VIDEO_FB_STATE.format VIDEO_FB_STATE.fb_pa VIDEO_FB_STATE.fb_va
VIDEO_FB_STATE.fb_size VIDEO_FB_STATE_size
VIDEO_FONT_WIDTH VIDEO_FONT_HEIGHT VIDEO_FONT_STRIDE VIDEO_FONT_FIRST
VIDEO_FONT_LAST VIDEO_FONT_GLYPHS VIDEO_FONT_BYTES
VIDEO_ST_WIDTH VIDEO_ST_HEIGHT VIDEO_ST_BARS VIDEO_ST_BAR_W
VIDEO_ST_RECT_X VIDEO_ST_RECT_Y VIDEO_ST_RECT_W VIDEO_ST_RECT_H
VIDEO_ST_RECT_COLOR VIDEO_ST_BORDER VIDEO_ST_TEXT_X VIDEO_ST_TEXT_Y
VIDEO_ST_TEXT_COLOR VIDEO_ST_TEXT_LEN
VIDEO_OK VIDEO_EINVAL VIDEO_ENOTSUP VIDEO_ENOTREADY VIDEO_EALREADY
VIDEO_EIO MM_MMIO_LAPIC_VA MM_HEAP_LIMIT VIDEO_PAGE_SIZE
""".split()

MANIFEST_BYTES = len(MANIFEST_NAMES) * 8

VIDEO_ENTRIES = ['video_init', 'video_putpixel', 'video_fill_rect',
                 'video_blit_rect', 'video_readback_rect', 'video_puts',
                 'video_selftest']
VIDEO_HELPERS = ['video_bgrx', 'video_clip_rect']

# the uniform video_enter frame, and the fifth argument load in front
FRAME = bytes.fromhex('55 53 56 57 41 54 41 55 41 56 41 57 48 83 EC 28')
STACK_ARG = bytes.fromhex('8B 44 24 28')     # mov eax, [rsp+40]
PROLOGUES = {
    'video_fill_rect': STACK_ARG + FRAME,
    'video_blit_rect': STACK_ARG + FRAME,
    'video_readback_rect': STACK_ARG + FRAME,
    'video_bgrx': bytes.fromhex('25'),          # and eax, imm32
    'video_clip_rect': bytes.fromhex('89 C9'),  # mov ecx, ecx
}

# hand written glyph expectations, independent of the embedded subset
GLYPH_A = bytes([0x00, 0x00, 0x18, 0x18, 0x24, 0x24, 0x42, 0x42,
                 0x7E, 0x42, 0x42, 0x42, 0x00, 0x00, 0x00, 0x00])
GLYPH_K = bytes([0x00, 0x00, 0x42, 0x22, 0x12, 0x0A, 0x06, 0x06,
                 0x0A, 0x12, 0x22, 0x42, 0x00, 0x00, 0x00, 0x00])


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
    driver = BUILD / ('fbprobe-%s.asm' % tag)
    output = BUILD / ('fbprobe-%s.bin' % tag)
    driver.write_text(
        '; generated by tests/stage2-fb/check.py\n'
        'FB_PROBE_ORG = 0x%X\n'
        'include "%s"\n' % (org & MASK64, str(TESTS / 'layout.asm')))
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
        return self.ok(got == want,
                       '%s: expected %r, got %r' % (what, want, got))

    def report(self):
        for line in self.failures:
            print('  FAIL %s' % line)
        print('%d checks, %d failed' % (self.checks, len(self.failures)))
        return not self.failures


def load_manifest(img):
    man = {}
    for index, name in enumerate(MANIFEST_NAMES):
        man[name] = struct.unpack_from('<Q', img, index * 8)[0]
    return man


def signed(value):
    return value - (1 << 64) if value & (1 << 63) else value


# ------------------------------------------------------------------
# tiny binary helpers
# ------------------------------------------------------------------

def find_all(data, needle):
    found, start = [], 0
    while True:
        at = data.find(needle, start)
        if at < 0:
            return found
        found.append(at)
        start = at + 1


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
    """A rip-relative disp32 inside the body that resolves to the target.

    The disp32 sits at offset i; the instruction ends at i + 4 when the
    displacement is the last field (mov / lea), at i + 5 with a trailing
    imm8 (cmp dword [rip+x], imm8) and at i + 8 with a trailing imm32
    (mov dword [rip+x], imm32). All three endings are tried.
    """
    for i in range(len(body) - 3):
        disp = struct.unpack_from('<i', body, i)[0]
        for end in (4, 5, 8):
            if addr + i + end + disp == target:
                return True
    return False


def function_bodies(man, img, org):
    """Slice the video function bodies out of the image by manifest."""
    funcs = sorted((man[name], name)
                   for name in VIDEO_ENTRIES + VIDEO_HELPERS)
    bodies = {}
    for index, (addr, name) in enumerate(funcs):
        end = funcs[index + 1][0] if index + 1 < len(funcs) else man['video_data_start']
        # constant data follows two of the functions, keep it out
        if name == 'video_puts':
            end = min(end, man['video_font'])
        if name == 'video_selftest':
            end = min(end, man['video_st_colors'])
        bodies[name] = (addr, img[addr - org:end - org])
    return bodies


# ------------------------------------------------------------------
# section 1: image shape, contract constants, entry points
# ------------------------------------------------------------------

def check_image(chk, man, img, org, label):
    chk.start('image %s' % label)
    chk.eq(bytes(struct.pack('<Q', man['magic'])), b'K64FBP1\x00',
           'manifest magic')
    chk.eq(man['version'], 1, 'manifest version')
    chk.eq(man['org'], org & MASK64, 'manifest org')
    chk.eq(len(MANIFEST_NAMES), 81, 'the manifest keeps 81 entries')
    chk.ok(len(img) >= MANIFEST_BYTES, 'the image holds the manifest')

    data_start, data_end = man['video_data_start'], man['video_data_end']
    chk.eq(data_start % PAGE_SIZE, 0, 'video data start is page aligned')
    chk.eq(data_end % PAGE_SIZE, 0, 'video data end is page aligned')
    chk.eq(data_end - org, len(img), 'the image ends exactly on video_data_end')
    block = img[data_start - org:data_end - org]
    chk.ok(block == bytes(len(block)), 'the whole video data block is zero filled')

    chk.eq(man['video_fb_state'], data_start, 'the FB state starts the data block')
    chk.eq(man['video_selftest_mask'] - man['video_fb_state'], FB_STATE_SIZE,
           'the selftest mask follows the 64 byte state')
    chk.ok(man['video_selftest_mask'] + 8 <= data_end,
           'the selftest mask fits into the data block')

    # the MM dependency sits in front of the video code, its own data
    # block stays page aligned and zeroed
    chk.eq(man['mm_data_start'] % PAGE_SIZE, 0, 'MM data start is page aligned')
    chk.eq(man['mm_data_end'] % PAGE_SIZE, 0, 'MM data end is page aligned')
    chk.ok(man['mm_data_end'] <= man['video_init'],
           'the video code is emitted after the MM module')
    mm_block = img[man['mm_data_start'] - org:man['mm_data_end'] - org]
    chk.ok(mm_block == bytes(len(mm_block)), 'the MM data block stays zero filled')

    # every video entry is code between the MM data and the video data
    code = {}
    for entry in VIDEO_ENTRIES + VIDEO_HELPERS + ['vmm_map_mmio']:
        addr = man[entry]
        chk.ok(org <= addr < data_start, '%s is inside the code' % entry)
        code[entry] = addr
    for entry in VIDEO_ENTRIES + VIDEO_HELPERS:
        chk.ok(man[entry] >= man['mm_data_end'],
               '%s belongs to the video module' % entry)
        want = PROLOGUES.get(entry, FRAME)
        got = img[man[entry] - org:man[entry] - org + len(want)]
        chk.eq(got, want, '%s starts with its documented prologue' % entry)
    chk.ok(man['vmm_map_mmio'] < man['mm_data_start'],
           'vmm_map_mmio belongs to the MM code')
    chk.eq(len(set(code.values())), len(code),
           'no two entry points share an address')
    chk.ok(man['video_selftest'] > man['video_puts'],
           'the selftest is emitted after the primitives')

    # constant data lives outside the mutable block
    for name in ('video_font', 'video_font_end', 'video_st_colors',
                 'video_st_text'):
        chk.ok(org <= man[name] < data_start,
               '%s is constant data next to the code' % name)


def check_contract(chk, man):
    chk.start('contract')
    for name, want in EXPECT_CONSTANTS.items():
        chk.eq(man[name] & MASK64, want & MASK64, 'constant %s' % name)
    for name, want in EXPECT_OFFSETS.items():
        chk.eq(man[name], want, 'record offset %s' % name)
    # the frozen geometry relations, checked again on this side
    chk.eq(FB_VA, HEAP_LIMIT, 'FB VA starts at the heap limit')
    chk.ok(FB_VA + FB_MAX_SIZE <= MMIO_LAPIC_VA,
           'the FB window never reaches the LAPIC MMIO VA')
    chk.eq((FB_VA >> 39) & 0x1FF, 384, 'FB VA lives in PML4 slot 384')
    chk.ok(((FB_VA >> 39) & 0x1FF) != CORE_TEST_SLOT,
           'FB VA avoids the core test slot')
    chk.ok(MAX_WIDTH * MAX_HEIGHT * BPP <= FB_MAX_SIZE,
           'the maximum geometry fits the FB window')
    chk.eq(ST_WIDTH, ST_BARS * ST_BAR_W, 'the bars tile the reference width')
    chk.eq(ST_RECT_X, (ST_WIDTH - ST_RECT_W) // 2, 'the rectangle is centered x')
    chk.eq(ST_RECT_Y, (ST_HEIGHT - ST_RECT_H) // 2, 'the rectangle is centered y')


def check_font(chk, man, img, org):
    chk.start('font')
    lo, hi = man['video_font'] - org, man['video_font_end'] - org
    font = img[lo:hi]
    chk.eq(len(font), FONT_BYTES, 'the font subset is 1520 bytes')
    chk.eq(hashlib.sha256(font).hexdigest(), FONT_SHA256,
           'the font subset matches the charUni.mt extraction')
    chk.eq(font[0:16], bytes(16), 'the space glyph is empty')
    chk.eq(font[(0x41 - FONT_FIRST) * 16:(0x41 - FONT_FIRST + 1) * 16],
           GLYPH_A, "the 'A' glyph matches the hand written copy")
    chk.eq(font[(0x4B - FONT_FIRST) * 16:(0x4B - FONT_FIRST + 1) * 16],
           GLYPH_K, "the 'K' glyph matches the hand written copy")
    # cross check against the 32 bit baseline when it is reachable
    baseline = ROOT.parent / 'kolibrios' / 'kernel' / 'trunk' / 'gui' / 'charUni.mt'
    if baseline.is_file():
        sub = baseline.read_bytes()[FONT_FIRST * 16:(FONT_LAST + 1) * 16]
        chk.eq(font, sub, 'the subset equals the baseline file extraction')
    else:
        chk.ok(True, 'baseline file absent, the frozen hash pins the subset')


def check_font_oracle(chk):
    """task-108: the LSB-first truth, independent of renderer and model."""
    chk.start('font bit-order oracle')
    chk.eq(hashlib.sha256(ORACLE_TRUTH).hexdigest(), ORACLE_SHA256,
           'the hand written truth copy matches its sha256 pin')
    baseline = ROOT.parent / 'kolibrios' / 'kernel' / 'trunk' / 'gui' / 'charUni.mt'
    if baseline.is_file():
        raw = baseline.read_bytes()
        chk.eq(len(raw), 22704, 'charUni.mt is 1419 glyphs of 16 bytes')
        for i, cp in enumerate((0x4B, 0x31)):
            chk.eq(raw[cp * 16:(cp + 1) * 16],
                   ORACLE_TRUTH[i * 16:(i + 1) * 16],
                   "the '%s' truth copy equals the raw charUni.mt glyph"
                   % chr(cp))
    else:
        chk.ok(True, 'baseline absent, the pinned truth copy stands in')
    k, one = ORACLE_TRUTH[0:16], ORACLE_TRUTH[16:32]
    # LSB-first decode of the RAW truth: bit0 is the leftmost pixel
    chk.ok(k[3] & (1 << 1), "truth 'K' row 3 col 1 is lit (LSB-first)")
    chk.ok(not k[3] & (1 << 6), "truth 'K' row 3 col 6 is off (LSB-first)")
    chk.ok(one[4] & (1 << 2), "truth '1' row 4 flag col 2 is lit (left)")
    chk.ok(not one[4] & (1 << 5), "truth '1' row 4 col 5 is off (right)")
    # mirror discrimination: at these very pixels an MSB-first decode
    # disagrees with the LSB-first truth, so they can catch a mirror
    chk.ok(bool(k[3] & (1 << 1)) != bool(k[3] & (0x80 >> 1)),
           "'K' row 3 col 1 discriminates LSB from MSB")
    chk.ok(bool(one[4] & (1 << 2)) != bool(one[4] & (0x80 >> 2)),
           "'1' row 4 col 2 discriminates LSB from MSB")


def check_pattern_data(chk, man, img, org):
    chk.start('pattern data')
    at = man['video_st_colors'] - org
    table = struct.unpack_from('<8I', img, at)
    chk.eq(list(table), ST_COLORS, 'the bar color table is the frozen sequence')
    at = man['video_st_text'] - org
    chk.eq(img[at:at + len(ST_TEXT)], ST_TEXT, 'the selftest text is K64 GUI-G1')


# ------------------------------------------------------------------
# section 2: static machine code assertions over the real bodies
# ------------------------------------------------------------------

def check_bodies(chk, man, img, org, label):
    chk.start('code bodies %s' % label)
    bodies = function_bodies(man, img, org)
    addr_of = {man[name]: name for name in bodies}
    addr_of[man['vmm_map_mmio']] = 'vmm_map_mmio'
    known = set(addr_of)

    def calls(name):
        addr, body = bodies[name]
        return [addr_of[t] for t in call_targets(addr, body, known)]

    def field(name, off, what):
        addr, body = bodies[name]
        target = man['video_fb_state'] + off
        chk.ok(has_rip_ref(addr, body, target),
               '%s references %s' % (name, what))

    # every body that calls another function reserves the shadow space
    for name, (addr, body) in sorted(bodies.items()):
        if call_targets(addr, body, known):
            chk.ok(b'\x48\x83\xEC\x28' in body,
                   '%s reserves shadow space before its calls' % name)

    # ---- video_init: the handover checks and the mapping loop -------
    addr, body = bodies['video_init']
    chk.eq(calls('video_init').count('vmm_map_mmio'), 1,
           'init maps through exactly one vmm_map_mmio call site')
    for pattern, what in (
            (b'\x48\xA9\x02\x00\x00\x00\x0F\x84',
             'test rax, FLAG_FRAMEBUFFER / jz refuses a missing feature'),
            (b'\x41\x83\xFB\x01\x0F\x85',
             'cmp r11d, FMT_BGRX / jne refuses every foreign format'),
            (b'\x45\x39\xC2\x0F\x82',
             'cmp pitch, width / jb refuses a short pitch'),
            (b'\x41\x81\xF8\x00\x0A\x00\x00',
             'cmp width, 2560 caps the geometry'),
            (b'\x41\x81\xF9\x40\x06\x00\x00',
             'cmp height, 1600 caps the geometry'),
            (b'\x49\xF7\xC5\xFF\x0F\x00\x00',
             'test fb_pa, 0xFFF demands a page aligned PA'),
            (b'\x44\x89\xD0\x49\x0F\xAF\xC1\x48\xC1\xE0\x02\x49\x39\xC6\x0F\x82',
             'pitch * height * 4 budget / jb refuses a short buffer'),
            (b'\x49\x81\xC7\xFF\x0F\x00\x00\x49\xC1\xEF\x0C',
             'add 4095 / shr 12 rounds the page count up'),
            (b'\x49\x81\xFF\x00\x10\x00\x00\x0F\x87',
             'cmp pages, 4096 / ja refuses a mapping past the FB window'),
            (b'\x4C\x39\xFB\x73',
             'cmp rbx, pages / jae ends the mapping loop'),
            (b'\x48\xB9\x00\x00\x00\x01\x00\xC0\xFF\xFF',
             'mov rcx, FB_VA forms the mapping target'),
            (b'\x48\xC1\xE0\x0C\x48\x01\xC1',
             'shl 12 / add rcx walks the VA page by page'),
            (b'\x4C\x89\xEA\x48\x01\xC2',
             'mov rdx, fb_pa / add walks the PA page by page'),
            (b'\x44\x89\xD0\xC1\xE0\x02',
             'stride is published as pitch * 4')):
        chk.ok(pattern in body, 'init %s' % what)
    field('video_init', EXPECT_OFFSETS['VIDEO_FB_STATE.mapped_pages'],
          'the mapping progress counter')
    field('video_init', EXPECT_OFFSETS['VIDEO_FB_STATE.stride'],
          'the published stride')
    field('video_init', EXPECT_OFFSETS['VIDEO_FB_STATE.ready'],
          'the ready flag')
    # the progress counter is stored before the call, so a failure
    # leaves the exact number of finished pages behind
    store = body.find(b'\x89\x1D')
    call = body.find(b'\xE8')
    chk.ok(0 <= store < call, 'init publishes the progress before the call')
    # the geometry must be reloaded from BootInfo AFTER the mapping loop:
    # r8..r11 are caller-saved, vmm_map_mmio clobbers them, and publishing
    # the stale registers hands garbage geometry to every draw call (the
    # task-78 QEMU smoke caught exactly that: width 1338 instead of 1024)
    reload = bytes.fromhex('45 8B 44 24 68'      # mov r8d, [r12+104] width
                           '45 8B 4C 24 6C'      # mov r9d, [r12+108] height
                           '45 8B 54 24 74'      # mov r10d, [r12+116] pitch
                           '45 8B 5C 24 70')     # mov r11d, [r12+112] format
    # the same four loads also appear before the loop (the validation
    # read), so only an occurrence AFTER the call site counts
    chk.ok(call >= 0 and body.find(reload, call) > call,
           'init reloads the geometry from BootInfo after the map loop')
    # task-85: the size must be bounded by the 16 MiB window BEFORE the
    # add 4095 round up, otherwise a size near 2^64 wraps to 0 pages and
    # ready is published over unmapped VAs
    guard = bytes.fromhex('49 81 FE 00 00 00 01 0F 87')  # cmp r14,16MiB / ja
    round_up = body.find(bytes.fromhex('49 81 C7 FF 0F 00 00'))
    guard_at = body.find(guard)
    chk.ok(0 <= guard_at < round_up,
           'init bounds the size by the FB window before the page round up')
    # task-85: the map-loop failure branch must land on the EIO return,
    # never pass a raw MM status through (MM_ENOTSUP would masquerade as
    # a geometry skip in foundation)
    test_at = body.find(b'\x85\xC0', call)
    eio = b'\xB8\xF7\xFF\xFF\xFF'                 # mov eax, VIDEO_EIO
    branch_ok = False
    if test_at >= 0 and body[test_at + 2:test_at + 4] == b'\x0F\x85':
        disp = struct.unpack_from('<i', body, test_at + 4)[0]
        # the jnz rel32 starts at test_at + 2 and is 6 bytes long
        target = test_at + 8 + disp
        branch_ok = body[target:target + 5] == eio
    chk.ok(branch_ok,
           'init turns a mapping fault into EIO, not a raw MM status')

    # ---- video_bgrx: the frozen byte order --------------------------
    # BGRX (B in the low byte) is the plain little-endian store of
    # 0x00RRGGBB; a bswap would emit R,G,B,X and swap red/blue on screen
    chk.eq(bodies['video_bgrx'][1], bytes.fromhex('25 FF FF FF 00 C3'),
           'bgrx is exactly and eax, 0x00FFFFFF / ret')

    # ---- video_clip_rect: the shared rectangle clipping -------------
    addr, body = bodies['video_clip_rect']
    for pattern, what in (
            (b'\x4C\x39\xD1\x73', 'cmp x, width / jae clips the left edge'),
            (b'\x4C\x39\xDA\x73', 'cmp y, height / jae clips the top edge'),
            (b'\x48\x89\xC8\x4C\x01\xC0\x4C\x39\xD0\x76',
             '64 bit x + w compare cannot wrap'),
            (b'\x4D\x89\xD0\x49\x29\xC8', 'w shrinks to width - x'),
            (b'\x4D\x89\xD9\x49\x29\xD1', 'h shrinks to height - y')):
        chk.ok(pattern in body, 'clip %s' % what)
    field('video_clip_rect', EXPECT_OFFSETS['VIDEO_FB_STATE.width'], 'width')
    field('video_clip_rect', EXPECT_OFFSETS['VIDEO_FB_STATE.height'], 'height')

    # ---- video_putpixel: clip first, stride row offset, BGRX store --
    addr, body = bodies['video_putpixel']
    for pattern, what in (
            (b'\x4C\x39\xD1\x73', 'cmp x, width / jae clips the point'),
            (b'\x4C\x39\xDA\x73', 'cmp y, height / jae clips the point'),
            (b'\x48\x0F\xAF\xC2', 'imul rax, rdx forms y * stride'),
            (b'\x48\x8D\x3C\x8F', 'lea rdi, [rdi + rcx*4] adds x * 4'),
            (b'\x89\x07', 'mov [rdi], eax stores one BGRX dword')):
        chk.ok(pattern in body, 'putpixel %s' % what)
    chk.ok('video_bgrx' in calls('video_putpixel'),
           'putpixel converts the color through video_bgrx')
    field('video_putpixel', EXPECT_OFFSETS['VIDEO_FB_STATE.stride'],
          'the stride, never the width, for the row offset')
    field('video_putpixel', EXPECT_OFFSETS['VIDEO_FB_STATE.fb_va'], 'fb_va')

    # ---- video_fill_rect: clipped rep stosd over stride rows --------
    addr, body = bodies['video_fill_rect']
    chk.ok('video_clip_rect' in calls('video_fill_rect'),
           'fill_rect clips through video_clip_rect')
    chk.ok(b'\x85\xC0\x74' in body,
           'fill_rect skips the draw when the clip is empty')
    chk.ok('video_bgrx' in calls('video_fill_rect'),
           'fill_rect converts the color through video_bgrx')
    chk.ok(b'\xF3\xAB' in body, 'fill_rect writes with rep stosd')
    chk.ok(b'\x4C\x89\xF8\x48\xC1\xE0\x02\x49\x29\xC5' in body,
           'fill_rect skips stride - w * 4 bytes of padding per row')
    field('video_fill_rect', EXPECT_OFFSETS['VIDEO_FB_STATE.stride'],
          'the stride for the row offset')

    # ---- video_blit_rect: clipped rep movsd, no conversion ----------
    addr, body = bodies['video_blit_rect']
    chk.ok('video_clip_rect' in calls('video_blit_rect'),
           'blit_rect clips through video_clip_rect')
    chk.ok(b'\xF3\xA5' in body, 'blit_rect copies with rep movsd')
    chk.ok(b'\xFC' in body, 'blit_rect clears DF before the copy')
    chk.ok('video_bgrx' not in calls('video_blit_rect'),
           'blit_rect copies raw BGRX dwords without conversion')
    # task-85: the source rows stay packed at the ORIGINAL w; the save
    # must precede the clip and every row must skip the clipped-away
    # source bytes, otherwise row 2+ reads the wrong source pixels
    save_w = body.find(bytes.fromhex('45 89 CE'))   # mov r14d, r9d
    clip_call = body.find(b'\xE8')
    chk.ok(0 <= save_w < clip_call,
           'blit_rect saves the original w before the clip')
    comp = bytes.fromhex('4D 29 C6 49 C1 E6 02')    # sub r14,r8 / shl r14,2
    movsd = body.find(b'\xF3\xA5')
    advance = body.find(bytes.fromhex('4C 01 F6'), movsd)  # add rsi, r14
    chk.ok(comp in body and movsd >= 0 and advance > movsd,
           'blit_rect compensates the source row pitch by the original w')
    field('video_blit_rect', EXPECT_OFFSETS['VIDEO_FB_STATE.stride'],
          'the stride for the destination row offset')

    # ---- video_readback_rect: the task-96 mirror of the blit --------
    # the ONLY sanctioned FB read path, the desktop cursor background
    # save; the direction is reversed, so the stride compensation moves
    # to the SOURCE (rsi += r13) and the original-w packing compensation
    # moves to the DESTINATION (rdi += r14)
    addr, body = bodies['video_readback_rect']
    chk.ok('video_clip_rect' in calls('video_readback_rect'),
           'readback_rect clips through video_clip_rect')
    chk.ok(b'\xF3\xA5' in body, 'readback_rect copies with rep movsd')
    chk.ok(b'\xFC' in body, 'readback_rect clears DF before the copy')
    chk.ok('video_bgrx' not in calls('video_readback_rect'),
           'readback_rect copies raw BGRX dwords without conversion')
    chk.ok(b'\x4D\x85\xE4' in body,
           'readback_rect refuses a null destination buffer')
    save_w = body.find(bytes.fromhex('45 89 CE'))   # mov r14d, r9d
    clip_call = body.find(b'\xE8')
    chk.ok(0 <= save_w < clip_call,
           'readback_rect saves the original w before the clip')
    comp = bytes.fromhex('4D 29 C6 49 C1 E6 02')    # sub r14,r8 / shl r14,2
    movsd = body.find(b'\xF3\xA5')
    advance = body.find(bytes.fromhex('4C 01 F7'), movsd)  # add rdi, r14
    chk.ok(comp in body and movsd >= 0 and advance > movsd,
           'readback_rect packs the destination rows at the original w')
    src_adv = body.find(bytes.fromhex('4C 01 EE'), movsd)  # add rsi, r13
    chk.ok(movsd >= 0 and src_adv > movsd,
           'readback_rect compensates the source row pitch by the stride')
    # task-102 D2: pin the copy-loop register ROLES byte for byte.
    # rdi must be established from the destination buffer (r12), rsi
    # from fb_va + y * stride + x * 4, both BEFORE the rep movsd; an
    # xchg rsi, rdi anywhere in the body swaps source and destination
    # and must be caught here statically, never only by a runtime #PF.
    rdi_load = body.find(bytes.fromhex('4C 89 E7'))    # mov rdi, r12
    chk.ok(rdi_load >= 0,
           'readback_rect establishes rdi from the destination buffer')
    rsi_load = -1
    for i in find_all(body, bytes.fromhex('48 8B 35')):  # mov rsi, [rip+x]
        disp = struct.unpack_from('<i', body, i + 3)[0]
        if addr + i + 7 + disp == man['video_fb_state'] \
                + EXPECT_OFFSETS['VIDEO_FB_STATE.fb_va']:
            rsi_load = i
            break
    chk.ok(rsi_load >= 0,
           'readback_rect loads rsi from fb_va, the framebuffer source')
    add_rsi = body.find(bytes.fromhex('48 01 C6'))     # add rsi, rax
    lea_rsi = body.find(bytes.fromhex('48 8D 34 8E'))  # lea rsi,[rsi+rcx*4]
    chk.ok(0 <= rdi_load < rsi_load < add_rsi < lea_rsi < movsd,
           'readback_rect establishes rdi=buffer then rsi=FB+stride before the copy')
    chk.ok(bytes.fromhex('48 87') not in body,
           'readback_rect never exchanges rsi/rdi (no source/dest role swap)')
    chk.ok(0 <= movsd < src_adv < advance,
           'readback_rect advances rsi by stride before rdi by the packed w')
    field('video_readback_rect', EXPECT_OFFSETS['VIDEO_FB_STATE.stride'],
          'the stride for the source row offset')
    field('video_readback_rect', EXPECT_OFFSETS['VIDEO_FB_STATE.fb_va'],
          'fb_va is the readback source')
    chk.ok('video_readback_rect' not in calls('video_selftest'),
           'the selftest stays draw-only, it never reads the FB back')

    # ---- video_puts: glyph index, range skip, per pixel clipping ----
    addr, body = bodies['video_puts']
    for pattern, what in (
            (b'\x83\xF8\x20\x72', 'cmp c, 0x20 / jb skips a low byte'),
            (b'\x83\xF8\x7E\x77', 'cmp c, 0x7E / ja skips a high byte'),
            (b'\x83\xE8\x20\x6B\xC0\x10',
             'sub 0x20 / imul 16 forms the glyph index'),
            (b'\x41\xF6\xC1\x01', 'test r9b, 0x01 reads the LSB first'),
            (b'\x41\xD0\xE9', 'shr r9b, 1 walks the bitmap row'),
            (b'\x44\x89\x34\x8F',
             'mov [rdi + rcx*4], r14d stores one BGRX pixel'),
            (b'\x41\x83\xFA\x08\x72', 'the column loop runs 8 times'),
            (b'\x48\x83\xFB\x10\x72', 'the row loop runs 16 times'),
            (b'\x49\x83\xC4\x08', 'the cursor advances 8 columns')):
        chk.ok(pattern in body, 'puts %s' % what)
    chk.ok(has_rip_ref(addr, body, man['video_font']),
           'puts addresses the embedded font')
    chk.ok('video_bgrx' in calls('video_puts'),
           'puts converts the color through video_bgrx')
    field('video_puts', EXPECT_OFFSETS['VIDEO_FB_STATE.width'],
          'the per pixel width clip')
    field('video_puts', EXPECT_OFFSETS['VIDEO_FB_STATE.height'],
          'the per row height clip')
    field('video_puts', EXPECT_OFFSETS['VIDEO_FB_STATE.stride'],
          'the stride for the text row offset')

    # ---- video_selftest: the frozen draw call sequence --------------
    addr, body = bodies['video_selftest']
    chk.eq(calls('video_selftest').count('video_fill_rect'), 6,
           'selftest has 6 fill sites: bar loop + 4 border + rectangle')
    chk.eq(calls('video_selftest').count('video_puts'), 1,
           'selftest draws the text exactly once')
    for pattern, what in (
            (b'\x83\xFB\x08\x72', 'the bar loop runs 8 times'),
            (b'\x69\xC9\x80\x00\x00\x00', 'bar x is i * 128'),
            (b'\xB9\x9C\x01\x00\x00', 'mov ecx, 412 the rectangle x'),
            (b'\xBA\x1C\x01\x00\x00', 'mov edx, 284 the rectangle y'),
            (b'\x41\xB8\xC8\x00\x00\x00', 'mov r8d, 200 the rectangle w'),
            (b'\x41\xB9\xC8\x00\x00\x00', 'mov r9d, 200 the rectangle h'),
            (b'\xC7\x44\x24\x20\x80\x80\x80\x00',
             'the rectangle color 0x808080 goes to the fifth slot'),
            (b'\xB9\x08\x00\x00\x00\xBA\x08\x00\x00\x00',
             'the text starts at (8,8)')):
        chk.ok(pattern in body, 'selftest %s' % what)
    chk.eq(body.count(b'\xC7\x44\x24\x20\xFF\xFF\xFF\x00'), 4,
           'the four border edges are white')
    chk.ok(has_rip_ref(addr, body, man['video_st_colors']),
           'selftest reads the frozen color table')
    chk.ok(has_rip_ref(addr, body, man['video_st_text']),
           'selftest addresses the frozen text')
    # the geometry gate: exactly 1024x768, cmp dword [rip+state], imm
    gate_w = gate_h = False
    for i in find_all(body, b'\x81\x3D'):
        disp = struct.unpack_from('<i', body, i + 2)[0]
        target = addr + i + 10 + disp
        imm = struct.unpack_from('<I', body, i + 6)[0]
        if target == man['video_fb_state'] + EXPECT_OFFSETS['VIDEO_FB_STATE.width'] \
                and imm == ST_WIDTH:
            gate_w = True
        if target == man['video_fb_state'] + EXPECT_OFFSETS['VIDEO_FB_STATE.height'] \
                and imm == ST_HEIGHT:
            gate_h = True
    chk.ok(gate_w, 'selftest demands the reference width 1024')
    chk.ok(gate_h, 'selftest demands the reference height 768')


# ------------------------------------------------------------------
# section 3: the frozen pattern spec, a pure Python probe pixel model
# ------------------------------------------------------------------
# The model renders the spec, not the assembly: the static assertions
# above tie the emitted code to the same spec. The probe pixels below
# are hand written, they are the forensic base of the QEMU screendump
# assertions of the integration task.

def init_size_model(size):
    """Pure Python model of the video_init size gate (task-85).

    Returns the mapped page count, or None for the ENOTSUP refusal.
    The 16 MiB window bound is applied BEFORE the round up, exactly
    like the pinned assembly, so a size near 2^64 can never wrap.
    """
    if size == 0 or size > FB_MAX_SIZE:
        return None
    pages = (size + PAGE_SIZE - 1) // PAGE_SIZE
    return None if pages > FB_MAX_PAGES else pages


def check_init_size_model(chk):
    chk.start('init size gate model')
    wrap = ((1 << 64) - 1 + PAGE_SIZE - 1) & MASK64
    chk.eq(wrap // PAGE_SIZE, 0,
           'an unguarded round up of 2^64-1 wraps to zero pages')
    chk.ok(init_size_model((1 << 64) - 1) is None,
           'the model rejects size = 2^64-1 (ENOTSUP, no wrap)')
    chk.ok(init_size_model(FB_MAX_SIZE + 1) is None,
           'the model rejects one byte past the 16 MiB window')
    chk.eq(init_size_model(FB_MAX_SIZE), FB_MAX_PAGES,
           'the model accepts the exact window as 4096 pages')
    chk.eq(init_size_model(1024 * 768 * 4), 768,
           'the model accepts the reference framebuffer as 768 pages')


def spec_frame(font):
    """The expected 1024x768 frame as a list of rows of 0x00RRGGBB."""
    rows = [[ST_COLORS[x // ST_BAR_W] for x in range(ST_WIDTH)]
            for _ in range(ST_HEIGHT)]
    for x in range(ST_WIDTH):
        rows[0][x] = ST_BORDER
        rows[ST_HEIGHT - 1][x] = ST_BORDER
    for y in range(ST_HEIGHT):
        rows[y][0] = ST_BORDER
        rows[y][ST_WIDTH - 1] = ST_BORDER
    for y in range(ST_RECT_Y, ST_RECT_Y + ST_RECT_H):
        for x in range(ST_RECT_X, ST_RECT_X + ST_RECT_W):
            rows[y][x] = ST_RECT_COLOR
    for index, ch in enumerate(ST_TEXT[:-1]):
        glyph = font[(ch - FONT_FIRST) * FONT_STRIDE:
                     (ch - FONT_FIRST + 1) * FONT_STRIDE]
        for row in range(FONT_HEIGHT):
            for col in range(FONT_WIDTH):
                if glyph[row] & (1 << col):  # LSB-first: bit0 = leftmost
                    x = ST_TEXT_X + index * FONT_WIDTH + col
                    y = ST_TEXT_Y + row
                    rows[y][x] = ST_TEXT_COLOR
    return rows


def check_spec_model(chk, man, img, org):
    chk.start('pattern spec model')
    font = img[man['video_font'] - org:man['video_font_end'] - org]
    frame = spec_frame(font)
    # hand written probe pixels: (x, y, expected color, why)
    probes = [
        (64, 100, 0xFF0000, 'bar 0 center is red'),
        (192, 100, 0x00FF00, 'bar 1 center is green'),
        (320, 100, 0x0000FF, 'bar 2 center is blue'),
        (448, 100, 0xFFFF00, 'bar 3 center is yellow'),
        (576, 100, 0x00FFFF, 'bar 4 center is cyan'),
        (704, 100, 0xFF00FF, 'bar 5 center is magenta'),
        (832, 100, 0xFFFFFF, 'bar 6 center is white'),
        (960, 100, 0x000000, 'bar 7 center is black'),
        (127, 100, 0xFF0000, 'the bar 0/1 boundary left side is red'),
        (128, 100, 0x00FF00, 'the bar 0/1 boundary right side is green'),
        (895, 100, 0xFFFFFF, 'the bar 6/7 boundary left side is white'),
        (896, 100, 0x000000, 'the bar 6/7 boundary right side is black'),
        (0, 0, 0xFFFFFF, 'the top left corner is border white'),
        (1023, 767, 0xFFFFFF, 'the bottom right corner is border white'),
        (512, 0, 0xFFFFFF, 'the top edge is border white'),
        (0, 384, 0xFFFFFF, 'the left edge is border white'),
        (1023, 100, 0xFFFFFF, 'the right edge is border white'),
        (1, 1, 0xFF0000, 'inside the border the bar 0 red shows'),
        (412, 284, 0x808080, 'the rectangle top left corner is gray'),
        (611, 483, 0x808080, 'the rectangle bottom right corner is gray'),
        (411, 284, 0xFFFF00, 'left of the rectangle the bar 3 yellow shows'),
        (612, 284, 0x00FFFF, 'right of the rectangle the bar 4 cyan shows'),
        (412, 283, 0xFFFF00, 'above the rectangle the bar 3 yellow shows'),
        (412, 484, 0xFFFF00, 'below the rectangle the bar 3 yellow shows'),
        (9, 10, 0xFFFFFF, "the 'K' glyph row 2 left stroke is lit"),
        (14, 10, 0xFFFFFF, "the 'K' glyph row 2 right stroke is lit"),
        (8, 10, 0xFF0000, "the 'K' glyph row 2 edge column is not lit"),
        (8, 8, 0xFF0000, "the 'K' glyph row 0 is empty"),
        # task-108 mirror-sensitive probes: 'K' row 3 is 0x22, under the
        # LSB-first truth cols 1 and 5 are lit; an MSB-first renderer
        # would light cols 2 and 6 instead
        (9, 11, 0xFFFFFF, "the 'K' glyph row 3 col 1 stroke is lit"),
        (13, 11, 0xFFFFFF, "the 'K' glyph row 3 col 5 stroke is lit"),
        (14, 11, 0xFF0000, "the 'K' glyph row 3 col 6 is not lit"),
        (10, 11, 0xFF0000, "the 'K' glyph row 3 col 2 is not lit"),
    ]
    for x, y, want, why in probes:
        chk.eq(frame[y][x], want, why)
    # the text cell must not touch the rectangle or the border
    chk.ok(ST_TEXT_X + len(ST_TEXT[:-1]) * FONT_WIDTH < ST_RECT_X,
           'the text cell stays clear of the rectangle')


# ------------------------------------------------------------------
# section 4: dual origin, the module links at the kernel base
# ------------------------------------------------------------------

def check_reloc(chk, images, mans):
    chk.start('kernel base relocation')
    low, hi = images[0], images[KERNEL_BASE]
    ml, mh = mans[0], mans[KERNEL_BASE]
    labels = ['video_data_start', 'video_data_end', 'video_fb_state',
              'video_selftest_mask', 'mm_data_start', 'mm_data_end',
              'video_font', 'video_font_end', 'video_st_colors',
              'video_st_text'] + VIDEO_ENTRIES + VIDEO_HELPERS + \
        ['vmm_map_mmio']
    for name in labels:
        chk.eq(mh[name], (ml[name] + KERNEL_BASE) & MASK64,
               '%s shifts by the kernel base' % name)
    for name in EXPECT_CONSTANTS:
        chk.eq(mh[name], ml[name], '%s is origin independent' % name)
    chk.eq(len(low), len(hi), 'both images have the same size')
    # the video module is fully rip relative: every byte from its first
    # function to the image end must be identical at both bases. The MM
    # dependency in front of it is frozen and uses sign extended imm32
    # absolutes of its own, so it is deliberately left out here.
    lo_at = ml['video_init']
    hi_at = mh['video_init'] - KERNEL_BASE
    chk.eq(low[lo_at:], hi[hi_at:],
           'the video code and data bytes are position independent')


# ------------------------------------------------------------------
# section 5: negative fixtures, a weakened module must be caught
# ------------------------------------------------------------------
# Each fixture copies kernel/video, kernel/mm and tests/stage2-fb into
# a sandbox, applies one single site mutation and re-runs this checker
# against the sandbox. The detection comes from the static assertions
# of sections 1-2: every mutated clause is pinned there and must FAIL
# while the mutated probe still assembles.

FIXTURES = [
    # the fill no longer clips: a rectangle past the edge writes freely
    ('clip-removed', 'kernel/video/draw.inc',
     '        cmp     dword [video_fb_state + VIDEO_FB_STATE.ready], 0\n'
     '        je      .notready\n'
     '        call    video_clip_rect\n'
     '        test    eax, eax\n'
     '        jz      .clipped\n'
     '        mov     r13, rcx                ; x\n',
     '        cmp     dword [video_fb_state + VIDEO_FB_STATE.ready], 0\n'
     '        je      .notready\n'
     '        mov     r13, rcx                ; x\n'),
    # the putpixel row offset uses width instead of stride = pitch * 4
    ('pitch-as-width', 'kernel/video/draw.inc',
     '        mov     eax, dword [video_fb_state + VIDEO_FB_STATE.stride]\n'
     '        imul    rax, rdx\n'
     '        mov     rdi, qword [video_fb_state + VIDEO_FB_STATE.fb_va]\n'
     '        add     rdi, rax\n'
     '        lea     rdi, [rdi + rcx*4]\n'
     '        mov     eax, r8d\n',
     '        mov     eax, dword [video_fb_state + VIDEO_FB_STATE.width]\n'
     '        imul    rax, rdx\n'
     '        mov     rdi, qword [video_fb_state + VIDEO_FB_STATE.fb_va]\n'
     '        add     rdi, rax\n'
     '        lea     rdi, [rdi + rcx*4]\n'
     '        mov     eax, r8d\n'),
    # the glyph stride becomes 9: every glyph after the first is garbage
    ('font-stride-9', 'kernel/video/text.inc',
     '        imul    eax, VIDEO_FONT_STRIDE\n',
     '        imul    eax, 9\n'),
    # the format gate is gone: RGBX/BitMask/BltOnly slip through
    ('format-check-removed', 'kernel/video/fb.inc',
     '        cmp     r11d, VIDEO_FMT_BGRX\n'
     '        jne     .notsup\n',
     ''),
    # the mapping bound is gone: an oversized framebuffer maps past the
    # FB window into the LAPIC MMIO region
    ('map-bound-removed', 'kernel/video/fb.inc',
     '        cmp     r15, VIDEO_FB_MAX_PAGES\n'
     '        ja      .notsup\n',
     ''),
    # a bswap sneaks back into the color path: the store byte order
    # becomes R,G,B,X and every red/blue probe pixel swaps
    ('bgrx-byteswap', 'kernel/video/draw.inc',
     '        and     eax, 0x00FFFFFF\n',
     '        bswap   eax\n'
     '        shr     eax, 8\n'),
    # the first bar swaps its color: the screendump spec breaks
    ('bar-color-swap', 'kernel/video/selftest.inc',
     '        dd      VIDEO_ST_BAR0\n',
     '        dd      VIDEO_ST_BAR1\n'),
    # task-85: the size window guard is gone, a size near 2^64 wraps
    # the round up to 0 pages and ready publishes over unmapped VAs
    ('size-wrap-unguarded', 'kernel/video/fb.inc',
     '        cmp     r14, VIDEO_FB_MAX_SIZE\n'
     '        ja      .notsup\n',
     ''),
    # task-85: the blit source row compensation is gone, row 2+ reads
    # the wrong source pixels after a right edge clip
    ('blit-src-stride', 'kernel/video/draw.inc',
     '        add     rsi, r14                ; source row pitch compensation\n',
     ''),
    # task-96: the readback source stride compensation is gone, row 2+
    # of a cursor background save reads the wrong framebuffer pixels
    ('readback-src-stride', 'kernel/video/draw.inc',
     '        add     rsi, r13                ; source row pitch compensation\n',
     ''),
    # task-102 D2: the copy-loop register roles are swapped after the
    # address establishment; the copy writes INTO the framebuffer from
    # the buffer and reads the buffer as if it were the FB. Must be
    # caught statically, not only by a runtime #PF
    ('readback-src-dst-swap', 'kernel/video/draw.inc',
     '        lea     rsi, [rsi + rcx*4]\n',
     '        lea     rsi, [rsi + rcx*4]\n'
     '        xchg    rsi, rdi\n'),
    # task-96: the readback no longer clips, a save rectangle past the
    # right/bottom edge reads freely past the framebuffer mapping
    ('readback-clip-missing', 'kernel/video/draw.inc',
     '        cmp     dword [video_fb_state + VIDEO_FB_STATE.ready], 0\n'
     '        je      .notready\n'
     '        call    video_clip_rect\n'
     '        test    eax, eax\n'
     '        jz      .clipped\n'
     '        mov     r15, r8                 ; w\n'
     '        mov     rbx, r9                 ; h\n'
     '        mov     rdi, r12\n',
     '        cmp     dword [video_fb_state + VIDEO_FB_STATE.ready], 0\n'
     '        je      .notready\n'
     '        mov     r15, r8                 ; w\n'
     '        mov     rbx, r9                 ; h\n'
     '        mov     rdi, r12\n'),
    # task-85: the mapping fault passes the raw MM status through again,
    # MM_ENOTSUP would reach foundation as a non-fatal geometry skip
    ('map-fail-passthrough', 'kernel/video/fb.inc',
     '        jnz     .map_fail',
     '        jnz     .done'),
    # task-108: the renderer walks the bitmap MSB-first again, every
    # glyph is horizontally mirrored while the data stays correct; the
    # LSB static pins and the mirror-sensitive probes must catch it
    ('msb-bitorder', 'kernel/video/text.inc',
     '        test    r9b, 0x01\n'
     '        jz      .col_next\n'
     '        mov     dword [rdi + rcx*4], r14d\n'
     '.col_next:\n'
     '        shr     r9b, 1\n',
     '        test    r9b, 0x80\n'
     '        jz      .col_next\n'
     '        mov     dword [rdi + rcx*4], r14d\n'
     '.col_next:\n'
     '        shl     r9b, 1\n'),
    # the post-map geometry reload is gone: the published width/height/
    # pitch/format are whatever vmm_map_mmio left in caller-saved r8..r11
    ('stale-geometry', 'kernel/video/fb.inc',
     '        ; r12 still holds before anything is published\n'
     '        mov     r8d, dword [r12 + VIDEO_BOOTINFO.gop_width]\n'
     '        mov     r9d, dword [r12 + VIDEO_BOOTINFO.gop_height]\n'
     '        mov     r10d, dword [r12 + VIDEO_BOOTINFO.gop_pixels_per_line]\n'
     '        mov     r11d, dword [r12 + VIDEO_BOOTINFO.gop_pixel_format]\n',
     '        ; r12 still holds before anything is published\n'),
]


def run_fixtures(chk, root):
    chk.start('negative fixtures')
    checker = root / 'tests' / 'stage2-fb' / 'check.py'
    host = find_host()
    fdir = fasm2_dir()
    for name, rel, old, new in FIXTURES:
        with tempfile.TemporaryDirectory(prefix='stage2-fb-%s-' % name) as tmp:
            tree = Path(tmp)
            (tree / 'kernel').mkdir(parents=True)
            shutil.copytree(root / 'kernel' / 'video', tree / 'kernel' / 'video')
            shutil.copytree(root / 'kernel' / 'mm', tree / 'kernel' / 'mm')
            shutil.copytree(root / 'tests' / 'stage2-fb',
                            tree / 'tests' / 'stage2-fb',
                            ignore=shutil.ignore_patterns('__pycache__'))
            target = tree / rel
            text = target.read_text(encoding='utf-8')
            if text.count(old) != 1:
                chk.ok(False, 'fixture %s: %d occurrences of the anchor'
                       % (name, text.count(old)))
                continue
            target.write_text(text.replace(old, new), encoding='utf-8')
            cmd = [sys.executable, str(checker), '--root', str(tree),
                   '--no-fixtures']
            env = dict(os.environ)
            env['FASMG'] = host
            env['FASM2_DIR'] = str(fdir)
            proc = subprocess.run(cmd, env=env, capture_output=True,
                                  text=True)
            output = (proc.stdout or '') + (proc.stderr or '')
            caught = (proc.returncode != 0 and 'FAIL' in output
                      and 'did not assemble' not in output)
            chk.ok(caught, 'fixture %s is caught (exit %d)'
                   % (name, proc.returncode))


# ------------------------------------------------------------------
# driver
# ------------------------------------------------------------------

def main(argv):
    global ROOT, TESTS, BUILD
    keep = '--keep' in argv
    verbose = '--verbose' in argv
    no_fixtures = '--no-fixtures' in argv
    if '--root' in argv:
        ROOT = Path(argv[argv.index('--root') + 1]).resolve()
        TESTS = ROOT / 'tests' / 'stage2-fb'
        BUILD = ROOT / 'build' / 'stage2-fb'
    if '--out' in argv:
        BUILD = Path(argv[argv.index('--out') + 1]).resolve()
    chk = Checker()
    print('stage 2 framebuffer video, independent layout tests')
    images, mans = {}, {}
    for org, label in ((0, 'flat'), (KERNEL_BASE, 'kernel base')):
        tag = 'flat' if org == 0 else 'kernel'
        img, text = assemble(org, tag)
        images[org] = img
        man = load_manifest(img)
        mans[org] = man
        check_image(chk, man, img, org, label)
        check_bodies(chk, man, img, org, label)
        if verbose:
            print(text)
    man = mans[0]
    check_contract(chk, man)
    check_font(chk, man, images[0], 0)
    check_font_oracle(chk)
    check_pattern_data(chk, man, images[0], 0)
    check_spec_model(chk, man, images[0], 0)
    check_init_size_model(chk)
    check_reloc(chk, images, mans)
    if not no_fixtures:
        run_fixtures(chk, ROOT)
    ok = chk.report()
    if not keep:
        for path in sorted(BUILD.glob('fbprobe-*')):
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
