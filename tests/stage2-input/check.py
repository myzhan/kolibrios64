#!/usr/bin/env python3
"""Stage 2 input module checks, task 89, contract IRQ-v2. No machine is
started.

Everything here is measured, nothing is taken from a comment. The probe
tests/stage2-input/layout.asm assembles kernel/input/input.asm exactly as
the kernel will include it (behind cpu.asm, mm.asm and irq.asm, whose
services it consumes; the dispatch branches call the REAL input leaves)
and appends a manifest of label addresses. This script then

  * compares the manifest constants and every data label offset against
    the fixed values of contract IRQ-v2, repeated here independently of
    the assembly sources;
  * decodes the emitted bytes: the indirect IOAPIC window, the measured
    VER floors, the masked RTE write order (high dword first), the frozen
    8042 bring-up sequence with the translation bit, the bounded ACK /
    resend protocol, the keyboard decode and the en_US key maps byte for
    byte, the mouse packet state machine with the mandatory sync bit,
    and the seven self test legs with their deterministic final counts;
  * proves the input module owns no STI and never writes the LAPIC EOI:
    both belong to the IRQ layer;
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
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

# ----------------------------------------------------------------------
# contract IRQ-v2, fixed here independently of the sources
# ----------------------------------------------------------------------

PAGE = 0x1000
KERNEL_BASE = 0xFFFFFFFF80000000

IOAPIC_PA = 0xFEC00000
IOAPIC_VA = 0xFFFFC00003000000
REGSEL = 0x00
WIN = 0x10
IDX_VER = 0x01
# the RTE register pairs DERIVE from the pin numbers, never magic
IDX_KBD_LO = 0x10 + 2 * 1
IDX_KBD_HI = IDX_KBD_LO + 1
IDX_MOUSE_LO = 0x10 + 2 * 12
IDX_MOUSE_HI = IDX_MOUSE_LO + 1
VER_MIN = 0x10
RTE_MIN = 13

VEC_KEYBOARD = 0x21
VEC_MOUSE = 0x2C
RTE_MASK = 0x10000
RTE_KBD_MASKED = RTE_MASK | VEC_KEYBOARD
RTE_MOUSE_MASKED = RTE_MASK | VEC_MOUSE
RTE_KBD_LIVE = VEC_KEYBOARD
RTE_MOUSE_LIVE = VEC_MOUSE

PS2_DATA = 0x60
PS2_STATUS = 0x64
STAT_OBF = 0x01
STAT_IBF = 0x02
# status bit 5: the OBF byte is AUX (mouse) sourced. Each handler
# consumes only its own source and returns without reading 0x60 on a
# mismatch; the unconsumed byte keeps OBF full so the owning port's
# IRQ stays asserted (masked RTEs latch the edge in the IOAPIC IRR).
STAT_AUX = 0x20
CMD_READ_CCB = 0x20
CMD_WRITE_CCB = 0x60
CMD_DISABLE_KBD = 0xAD
CMD_DISABLE_AUX = 0xA7
CMD_ENABLE_KBD = 0xAE
CMD_ENABLE_AUX = 0xA8
CMD_WRITE_AUX = 0xD4
CCB_SET = 0x43
CCB_CLEAR = 0x30
CCB_CHECK_MASK = 0x73
CCB_CHECK_VALUE = 0x43
MOUSE_CMD_RESET = 0xFF
MOUSE_CMD_DEFAULT = 0xF6
MOUSE_CMD_ENABLE = 0xF4
MOUSE_ACK = 0xFA
MOUSE_RESEND = 0xFE
MOUSE_RESET_OK = 0xAA
MOUSE_ID_STANDARD = 0x00

PORT_SPIN = 0x100000
FLUSH_MAX = 64
ACK_RETRY = 5
WAIT_SPIN = 0x70000000
POLL_CHUNK = 0x2000000
POLL_ROUNDS = 64
TIMER_WINDOW = 0x1000000

KBD_CAP = 120
KBD_ENTRY = 16
SCANCODE_E0 = 0xE0
SCANCODE_E1 = 0xE1                        # Pause prefix, 2 payload bytes
SCANCODE_BREAK = 0x80
ST_KBD_SCANCODE = 0x1E                    # 'a' make, break 0x9E

MOUSE_SYNC = 0x08
MOUSE_SIGN_X = 0x10
MOUSE_SIGN_Y = 0x20
MOUSE_OVERFLOW = 0xC0                     # byte 1 bits 6/7: drop the packet
MOUSE_XMAX = 1023
MOUSE_YMAX = 767
MOUSE_XINIT = 512
MOUSE_YINIT = 384
ST_MOUSE_DX = 5
ST_MOUSE_DY = -3                          # PS/2 Y is up positive
# byte 1 of the move packet DERIVES from the deltas: sync plus the sign
# bit of every negative delta (QEMU sets bit 5 for mouse_move 5 3)
ST_MOUSE_B1 = MOUSE_SYNC
if ST_MOUSE_DX < 0:
    ST_MOUSE_B1 |= MOUSE_SIGN_X
if ST_MOUSE_DY < 0:
    ST_MOUSE_B1 |= MOUSE_SIGN_Y
MOUSE_B1_MASK = 0xFF & ~(MOUSE_SIGN_X | MOUSE_SIGN_Y)
# frozen verdict semantics: the release packet assertion masks the two
# sign bits out of byte 1 (0xFFFFCF keeps sync/buttons and both delta
# bytes exact). A sign bit on a zero delta carries no information, so it
# must not fail the leg; conversely the mask must NEVER be widened
# (dropping the sync/button/delta bits would silently weaken the gate).
ST_MOUSE_LAST_MASK = (0xFF << 16) | (0xFF << 8) | MOUSE_B1_MASK
ST_MOUSE_PACKET = (ST_MOUSE_B1 | ((ST_MOUSE_DX & 0xFF) << 8) |
                   ((ST_MOUSE_DY & 0xFF) << 16))

# frozen final counts, DETERMINISTICALLY derived from the injection
# sequence, never from a runtime observation:
#  - kbd = 2: the masked leg drains BOTH bytes of sendkey b (contribute
#    0), the live leg counts exactly the make + break of sendkey a;
#  - mouse = 3: one move + one press + one release packet; the host
#    sends the release only after mouse_count >= 2, so QEMU can never
#    coalesce press+release into one packet;
#  - ticks = 11: the IRQ-v1 exit value 10 plus EXACTLY one tick of the
#    masked-oneshot leg. Deterministic under both QEMU masked-expiry
#    behaviors: (a) expiry discarded -> the unmask+re-arm delivers one
#    tick; (b) expiry latched in IRR -> the unmask delivers the phantom
#    as tick 11 and the mode-0 wait exits within a few instructions,
#    masking the LVT long before the re-armed 10 ms countdown could
#    deliver a 12th. A one-shot fires at most once per arm, so no
#    timing window is relied on.
ST_KBD_FINAL = 2
ST_MOUSE_FINAL = 3
IRQ_ST_FINAL_N = 10                       # the IRQ-v1 exit contract
TICKS_BASE = IRQ_ST_FINAL_N
TICKS_FINAL = TICKS_BASE + 1

STAGE_DONE = 6
LAPIC_VA = 0xFFFFC00002000000
LAPIC_EOI = 0x0B0
LAPIC_LVT_TIMER = 0x320
LAPIC_INIT_COUNT = 0x380
LAPIC_DIVIDE = 0x3E0
LVT_MASK = 0x10000
LVT_ONESHOT = 0x00000
DIVIDE_16 = 0x3
VEC_TIMER = 0x20
TICK_HZ = 100

EIO = -9
EPERM = -5
EALREADY = -8

# the data layout, offsets from input_data_start
OFF_STATE = 0
OFF_TEST_MASK = 8
OFF_TEST_KBD = 16
OFF_TEST_MOUSE = 24
OFF_TEST_STAGE = 32
OFF_MOUSE_LAST = 40
OFF_MOUSE_BUTTONS = 48
OFF_IOAPIC_VER = 56
OFF_IOAPIC_MAX = 64
OFF_MOUSE_ID = 72
OFF_KBD_MODS = 80
OFF_KBD_E0 = 88
OFF_KBD_USED = 96
OFF_KBD_DROPS = 104
OFF_MOUSE_STATE = 112
OFF_MOUSE_B1 = 120
OFF_MOUSE_B2 = 128
OFF_MOUSE_BAD = 136
OFF_MOUSE_X = 144
OFF_MOUSE_Y = 152
OFF_KBD_E1 = 160
OFF_KBD_BUF = 168
OFF_MESSAGES = OFF_KBD_BUF + KBD_CAP * KBD_ENTRY

MSG_SELFTEST = b'K64-INPUT-SELFTEST failed legs = \x00'
MSG_NL = b'\r\n\x00'

MAGIC = b'K64INPP1'
MANIFEST_VERSION = 1

# manifest entry order, see tests/stage2-input/layout.asm
M = [
    'magic', 'version',
    'code_start', 'code_end', 'data_start', 'data_end',
    'init', 'ioapic_read', 'ioapic_write', 'ioapic_bringup',
    'ps2_wait_ibf', 'ps2_wait_obf', 'ps2_cmd', 'ps2_write_data',
    'ps2_read_data', 'ps2_flush', 'ps2_mouse_write', 'ps2_mouse_ack',
    'ps2_bringup', 'keyboard_irq', 'mouse_irq', 'wait_count', 'selftest',
    'keymap', 'keymap_shift',
    'state', 'test_mask', 'test_kbd', 'test_mouse', 'test_stage',
    'mouse_last', 'mouse_buttons', 'ioapic_ver', 'ioapic_max', 'mouse_id',
    'kbd_mods', 'kbd_e0', 'kbd_used', 'kbd_drops',
    'mouse_state', 'mouse_b1', 'mouse_b2', 'mouse_bad', 'mouse_x', 'mouse_y',
    'kbd_e1', 'kbd_buf', 'msg_selftest', 'msg_nl',
    'vmm_map_mmio', 'irq_sti_wait', 'cpu_panic', 'cpu_com1_puts',
    'cpu_com1_puthex', 'irq_dispatch', 'irq_test_ticks', 'irq_calib_hz',
    'ioapic_pa', 'ioapic_va', 'vec_keyboard', 'vec_mouse',
    'idx_kbd_lo', 'idx_kbd_hi', 'idx_mouse_lo', 'idx_mouse_hi',
    'rte_kbd_masked', 'rte_mouse_masked', 'rte_kbd_live', 'rte_mouse_live',
    'ccb_set', 'ccb_clear', 'ccb_check_mask', 'ccb_check_value',
    'stat_aux', 'mouse_sync', 'mouse_overflow',
    'st_mouse_b1', 'mouse_b1_mask', 'st_mouse_last_mask',
    'st_kbd_final', 'st_mouse_final', 'ticks_base', 'ticks_final',
    'kbd_cap', 'kbd_entry', 'scancode_e1',
    'port_spin', 'flush_max', 'ack_retry', 'wait_spin',
    'poll_chunk', 'poll_rounds', 'timer_window',
    'mouse_xmax', 'mouse_ymax', 'mouse_xinit', 'mouse_yinit',
    'st_mouse_dx', 'st_mouse_dy', 'st_kbd_scancode', 'stage_done',
    'off_test_mask', 'off_test_kbd', 'off_test_mouse', 'off_test_stage',
    'off_kbd_e1', 'off_kbd_buf', 'off_messages',
    'irq_vec_keyboard', 'irq_vec_mouse', 'irq_final_n', 'lapic_va',
]

# label name -> frozen offset inside the data region
DATA_LABELS = {
    'state': OFF_STATE, 'test_mask': OFF_TEST_MASK,
    'test_kbd': OFF_TEST_KBD, 'test_mouse': OFF_TEST_MOUSE,
    'test_stage': OFF_TEST_STAGE, 'mouse_last': OFF_MOUSE_LAST,
    'mouse_buttons': OFF_MOUSE_BUTTONS, 'ioapic_ver': OFF_IOAPIC_VER,
    'ioapic_max': OFF_IOAPIC_MAX, 'mouse_id': OFF_MOUSE_ID,
    'kbd_mods': OFF_KBD_MODS, 'kbd_e0': OFF_KBD_E0,
    'kbd_used': OFF_KBD_USED, 'kbd_drops': OFF_KBD_DROPS,
    'mouse_state': OFF_MOUSE_STATE, 'mouse_b1': OFF_MOUSE_B1,
    'mouse_b2': OFF_MOUSE_B2, 'mouse_bad': OFF_MOUSE_BAD,
    'mouse_x': OFF_MOUSE_X, 'mouse_y': OFF_MOUSE_Y,
    'kbd_e1': OFF_KBD_E1,
    'kbd_buf': OFF_KBD_BUF, 'msg_selftest': OFF_MESSAGES,
}

# manifest constants that must not drift from the copies above
CONSTANTS = {
    'ioapic_pa': IOAPIC_PA, 'ioapic_va': IOAPIC_VA,
    'vec_keyboard': VEC_KEYBOARD, 'vec_mouse': VEC_MOUSE,
    'idx_kbd_lo': IDX_KBD_LO, 'idx_kbd_hi': IDX_KBD_HI,
    'idx_mouse_lo': IDX_MOUSE_LO, 'idx_mouse_hi': IDX_MOUSE_HI,
    'rte_kbd_masked': RTE_KBD_MASKED, 'rte_mouse_masked': RTE_MOUSE_MASKED,
    'rte_kbd_live': RTE_KBD_LIVE, 'rte_mouse_live': RTE_MOUSE_LIVE,
    'ccb_set': CCB_SET, 'ccb_clear': CCB_CLEAR,
    'ccb_check_mask': CCB_CHECK_MASK, 'ccb_check_value': CCB_CHECK_VALUE,
    'stat_aux': STAT_AUX, 'mouse_sync': MOUSE_SYNC,
    'mouse_overflow': MOUSE_OVERFLOW, 'st_mouse_b1': ST_MOUSE_B1,
    'mouse_b1_mask': MOUSE_B1_MASK, 'st_mouse_last_mask': ST_MOUSE_LAST_MASK,
    'st_kbd_final': ST_KBD_FINAL, 'st_mouse_final': ST_MOUSE_FINAL,
    'ticks_base': TICKS_BASE, 'ticks_final': TICKS_FINAL,
    'kbd_cap': KBD_CAP, 'kbd_entry': KBD_ENTRY,
    'scancode_e1': SCANCODE_E1,
    'port_spin': PORT_SPIN, 'flush_max': FLUSH_MAX, 'ack_retry': ACK_RETRY,
    'wait_spin': WAIT_SPIN, 'poll_chunk': POLL_CHUNK,
    'poll_rounds': POLL_ROUNDS, 'timer_window': TIMER_WINDOW,
    'mouse_xmax': MOUSE_XMAX, 'mouse_ymax': MOUSE_YMAX,
    'mouse_xinit': MOUSE_XINIT, 'mouse_yinit': MOUSE_YINIT,
    'st_mouse_dx': ST_MOUSE_DX, 'st_mouse_dy': ST_MOUSE_DY & (2**64 - 1),
    'st_kbd_scancode': ST_KBD_SCANCODE, 'stage_done': STAGE_DONE,
    'off_test_mask': OFF_TEST_MASK, 'off_test_kbd': OFF_TEST_KBD,
    'off_test_mouse': OFF_TEST_MOUSE, 'off_test_stage': OFF_TEST_STAGE,
    'off_kbd_e1': OFF_KBD_E1,
    'off_kbd_buf': OFF_KBD_BUF, 'off_messages': OFF_MESSAGES,
    'irq_vec_keyboard': VEC_KEYBOARD, 'irq_vec_mouse': VEC_MOUSE,
    'irq_final_n': IRQ_ST_FINAL_N, 'lapic_va': LAPIC_VA,
}

INPUT_ENTER = bytes.fromhex('55 53 56 57 41 54 41 55 41 56 41 57 48 83 EC 28')
INPUT_LEAVE = bytes.fromhex('48 83 C4 28 41 5F 41 5E 41 5D 41 5C 5F 5E 5B 5D')

# the frozen en_US set 1 maps, an independent copy of the semantics of
# the 32 bit data32.inc keymap/keymap_shift rows, 128 bytes each
KEYMAP = (bytes([0, 27]) + b'1234567890-=' + bytes([8, 9]) +
          b'qwertyuiop[]' + bytes([13, 0]) +
          b'asdfghjkl;' + bytes([39, 96, 0]) +
          bytes([92]) + b'zxcvbnm,./' + bytes([0]) +
          b'*' + bytes([0]) + b' ' + bytes([0]) +
          bytes(10) + bytes(2) + b'789-456+1230.' + bytes(6) +
          bytes(30) + bytes(8))
KEYMAP_SHIFT = (bytes([0, 27]) + b'!@#$%^&*()_+' + bytes([8, 9]) +
                b'QWERTYUIOP{}' + bytes([13, 0]) +
                b'ASDFGHJKL:' + bytes([34]) + b'~' + bytes([0]) +
                b'|ZXCVBNM<>?' + bytes([0]) +
                b'*' + bytes([0]) + b' ' + bytes([0]) +
                bytes(10) + bytes(2) + b'789-456+1230.' + bytes(6) +
                bytes(30) + bytes(8))

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


def lapic_write_r13(off, val):
    """The bytes of 'mov dword [r13 + off], imm32' with a disp32."""
    return b'\x41\xC7\x85' + struct.pack('<i', off) + struct.pack('<I', val)


def lapic_write_rsi(off, val):
    """The bytes of 'mov dword [rsi + off], imm32' with a disp32."""
    return b'\xC7\x86' + struct.pack('<i', off) + struct.pack('<I', val)


def mov_ecx(val):
    return b'\xB9' + struct.pack('<I', val)


def mov_edx(val):
    return b'\xBA' + struct.pack('<I', val)


def mov_r8b(val):
    return b'\x41\xB0' + bytes((val,))


def mov_r9b(val):
    return b'\x41\xB1' + bytes((val,))


def call_sites(img, at, end, opcode=b'\xE8'):
    """Every E8 rel32 (or E9 jmp) in a region: (site, target) file offsets.

    Naive byte scan: candidates whose target leaves the image are
    dropped, the rest would have to land exactly on a label to matter.
    """
    body = img.data[at:end]
    out = []
    for p in find_all(body, opcode):
        if p + 5 <= len(body):
            rel = struct.unpack_from('<i', body, p + 1)[0]
            target = at + p + 5 + rel
            if 0 <= target < len(img.data):
                out.append((at + p, target))
    return out


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


def ordered_positions(img, at, end, needles, start=None):
    """Find each needle strictly after the previous one; None if absent.
    start optionally skips a prefix of the region (file offset)."""
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
    """The (site, target) of the short or near conditional jump right
    after pos; None when there is none."""
    body = img.data
    for j in range(pos, min(pos + limit, len(body) - 2)):
        if body[j] == 0x0F and 0x80 <= body[j + 1] <= 0x8F:
            rel = struct.unpack_from('<i', body, j + 2)[0]
            return j, j + 6 + rel
        if 0x70 <= body[j] <= 0x7F:
            rel = struct.unpack_from('<b', body, j + 1)[0]
            return j, j + 2 + rel
    return None


def jcc_opcode(d, site):
    """The condition opcode normalized to the short form: fasmg may
    emit either 74/75 (rel8) or 0F 84/85 (rel32) for the same jz/jnz."""
    return d[site + 1] - 0x10 if d[site] == 0x0F else d[site]


def cmp_qword_sites(img, at, end):
    """Every 'cmp qword [rip + disp32], imm' in a region:
    (site, target file offset, immediate). Both the imm8 and the imm32
    encoding, so the frozen final counts can be read out of the bytes."""
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
        print('stage2-input: %d checks, %d failed' % (total, len(self.failures)))
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
            cmd += ['-i', 'INPUT_PROBE_ORG=%#x' % org]
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
    for name, want in CONSTANTS.items():
        rep.equal('manifest.%s' % name, img[name], want)
    # the frozen mask semantics, pinned here as well: the sign bits are
    # excluded from the zero-delta assertion and nothing else may be
    rep.equal('contract.b1_mask_is_cf', MOUSE_B1_MASK, 0xCF)
    rep.equal('contract.last_mask_is_ffffcf', ST_MOUSE_LAST_MASK, 0xFFFFCF)
    rep.equal('contract.derived_move_b1', ST_MOUSE_B1, 0x28)
    rep.equal('contract.ticks_derives_from_irq_exit', TICKS_FINAL, IRQ_ST_FINAL_N + 1)

    for name, off in DATA_LABELS.items():
        rep.equal('layout.%s_offset' % name,
                  img[name] - img['data_start'], off)

    lo, hi = img['code_start'], img['code_end']
    rep.check('layout.code_non_empty', hi > lo, '%#x..%#x' % (lo, hi))
    rep.equal('layout.api_emits_no_code', img['ioapic_read'], lo)
    order = ['ioapic_read', 'ioapic_write', 'ioapic_bringup',
             'ps2_wait_ibf', 'ps2_wait_obf', 'ps2_cmd', 'ps2_write_data',
             'ps2_read_data', 'ps2_flush', 'ps2_mouse_write',
             'ps2_mouse_ack', 'ps2_bringup', 'keyboard_irq', 'mouse_irq',
             'wait_count', 'selftest', 'init']
    chain = [img[name] for name in order]
    rep.check('layout.function_order', chain == sorted(chain) and
              all(lo <= v < hi for v in chain),
              'emission order follows the include order')
    rep.check('layout.keymaps_inside_code',
              lo <= img['keymap'] < img['keymap_shift'] < hi)
    rep.equal('layout.keymaps_adjacent', img['keymap_shift'] - img['keymap'], 128)
    rep.check('layout.code_before_data', hi <= img['data_start'])
    rep.check('layout.data_page_aligned',
              img['data_start'] % PAGE == 0 and img['data_end'] % PAGE == 0)
    rep.check('layout.data_size_page_multiple',
              (img['data_end'] - img['data_start']) % PAGE == 0 and
              img['data_end'] - img['data_start'] >= PAGE)
    rep.equal('layout.module_ends_with_data', img.at('data_end'), img.probe_at)
    for name in ('vmm_map_mmio', 'irq_sti_wait', 'cpu_panic',
                 'cpu_com1_puts', 'cpu_com1_puthex', 'irq_dispatch',
                 'irq_test_ticks', 'irq_calib_hz'):
        rep.check('layout.%s_before_module' % name, 0 < img[name] < lo,
                  '%#x' % img[name])


def check_data(rep, img):
    d = img.data
    start, end = img.at('data_start'), img.at('data_end')
    block = d[start:start + OFF_MESSAGES]
    rep.check('data.qword_block_all_zero', block == bytes(OFF_MESSAGES),
              '%d bytes of counters and key buffer must be real zeros' % OFF_MESSAGES)
    rep.equal('data.key_buffer_size', OFF_MESSAGES - OFF_KBD_BUF, KBD_CAP * KBD_ENTRY)
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


def check_ioapic(rep, img):
    d = img.data
    va_mov = b'\x49\xB8' + struct.pack('<Q', IOAPIC_VA)

    # the indirect window: IOREGSEL selects, IOWIN reads or writes
    at, end = region(img, 'ioapic_read', 'ioapic_write')
    pos = ordered_positions(img, at, end,
                            [va_mov, b'\x41\x89\x08', b'\x41\x8B\x40\x10'])
    rep.check('ioapic.read_window', pos is not None,
              'mov r8, VA / [r8+0]=ecx / eax=[r8+0x10]')
    at, end = region(img, 'ioapic_write', 'ioapic_bringup')
    pos = ordered_positions(img, at, end,
                            [va_mov, b'\x41\x89\x08', b'\x41\x89\x50\x10'])
    rep.check('ioapic.write_window', pos is not None,
              'mov r8, VA / [r8+0]=ecx / [r8+0x10]=edx')

    at, end = region(img, 'ioapic_bringup', 'ps2_wait_ibf')
    rep.check('ioapic.bringup_full_frame', d[at:at + len(INPUT_ENTER)] == INPUT_ENTER)
    # VER measured with sanity floors, never an exact hardcoded value
    # 0xFF does not fit a sign extended imm8, so the compare and the
    # mask use the imm32 encodings
    pos = ordered_positions(img, at, end, [
        mov_ecx(IDX_VER),
        b'\x81\xE2\xFF\x00\x00\x00',            # and edx, 0xFF
        b'\x83\xFA' + bytes((VER_MIN,)),        # cmp edx, 0x10
        b'\x81\xFA\xFF\x00\x00\x00',            # cmp edx, 0xFF
        b'\xC1\xE8\x10',                        # shr eax, 16
        b'\x25\xFF\x00\x00\x00',                # and eax, 0xFF
        b'\xFF\xC0',                            # inc eax
        b'\x83\xF8' + bytes((RTE_MIN,)),        # cmp eax, 13
    ])
    rep.check('ioapic.ver_sanity_floors', pos is not None,
              'version >= 0x10 and != 0xFF, entries >= 13')
    # the frozen RTE write order: high dword (destination 0) first,
    # then the low dword with mask = 1, keyboard then mouse
    pos = ordered_positions(img, at, end, [
        mov_ecx(IDX_KBD_HI), b'\x31\xD2',
        mov_ecx(IDX_KBD_LO), mov_edx(RTE_KBD_MASKED),
        mov_ecx(IDX_MOUSE_HI), b'\x31\xD2',
        mov_ecx(IDX_MOUSE_LO), mov_edx(RTE_MOUSE_MASKED),
    ])
    rep.check('ioapic.rte_masked_write_order', pos is not None,
              'high first, low masked, pin 1 then pin 12')
    # read back all four dwords AFTER the write sequence (the write
    # stage loads the same indices, so the search starts past it)
    back = None
    if pos:
        back = ordered_positions(img, at, end, [
            mov_ecx(IDX_KBD_HI), mov_ecx(IDX_KBD_LO),
            b'\x3D' + struct.pack('<I', RTE_KBD_MASKED),
            mov_ecx(IDX_MOUSE_HI), mov_ecx(IDX_MOUSE_LO),
            b'\x3D' + struct.pack('<I', RTE_MOUSE_MASKED),
        ], start=pos[-1])
    rep.check('ioapic.rte_readback', back is not None)
    reads = [s for s, t in call_sites(img, at, end) if t == img.at('ioapic_read')]
    writes = [s for s, t in call_sites(img, at, end) if t == img.at('ioapic_write')]
    rep.equal('ioapic.bringup_four_reads', len(reads), 5)   # VER + 4 RTE
    rep.equal('ioapic.bringup_four_writes', len(writes), 4)
    rep.check('ioapic.leave_restore',
              d[end - len(INPUT_LEAVE) - 1:end] == INPUT_LEAVE + b'\xC3')


def check_ps2(rep, img):
    d = img.data
    at, end = region(img, 'ps2_wait_ibf', 'keyboard_irq')

    # the port primitives use the frozen ports and bounded spins
    rep.check('ps2.status_port_reads', d.find(b'\xE4\x64', at, end) >= 0)
    rep.check('ps2.data_port_reads', d.find(b'\xE4\x60', at, end) >= 0)
    rep.check('ps2.cmd_port_writes', d.find(b'\xE6\x64', at, end) >= 0)
    rep.check('ps2.data_port_writes', d.find(b'\xE6\x60', at, end) >= 0)
    rep.check('ps2.ibf_bit_test', d.find(b'\xA8\x02', at, end) >= 0)
    rep.check('ps2.obf_bit_test', d.find(b'\xA8\x01', at, end) >= 0)
    budgets = find_all(d[at:end], mov_ecx(PORT_SPIN))
    rep.check('ps2.port_spin_budgets', len(budgets) >= 3,
              '%d bounded port waits' % len(budgets))
    decs = find_all(d[at:end], b'\xFF\xC9')
    bounded = 0
    for p in decs:
        j = jcc_after(img, at + p + 2, 2)
        if j is not None and j[1] < at + p + 2:
            bounded += 1
    rep.check('ps2.port_waits_loop_backwards', bounded >= 3,
              '%d dec ecx / jnz backwards' % bounded)

    # a zero spin budget fails immediately instead of wrapping into
    # 2^32 spins: test ecx, ecx on entry, jz onto mov eax, INPUT_EIO
    for name, stop in (('ps2_wait_ibf', 'ps2_wait_obf'),
                       ('ps2_wait_obf', 'ps2_cmd')):
        wa, we = region(img, name, stop)
        rep.check('ps2.%s_zero_budget_guard' % name,
                  d[wa:wa + 2] == b'\x85\xC9', 'test ecx, ecx on entry')
        j = jcc_after(img, wa + 2, 2)
        rep.check('ps2.%s_zero_budget_fails_eio' % name,
                  j is not None and jcc_opcode(d, j[0]) == 0x74 and
                  d[j[1]:j[1] + 5] == b'\xB8' + struct.pack('<i', EIO),
                  'jz lands on mov eax, INPUT_EIO')

    # the flush is bounded and returns the drained count
    fa, fe = region(img, 'ps2_flush', 'ps2_mouse_write')
    pos = ordered_positions(img, fa, fe, [
        mov_ecx(FLUSH_MAX), b'\xE4\x64', b'\xA8\x01', b'\xE4\x60',
        b'\xFF\xC9', b'\xB8' + struct.pack('<I', FLUSH_MAX), b'\x29\xC8',
    ])
    rep.check('ps2.flush_bounded_counts_drain', pos is not None,
              'cap 64, drain, return 64 - remaining as arrival evidence')

    # the 0xD4 prefix routes the next data byte to the mouse
    wa, we = region(img, 'ps2_mouse_write', 'ps2_mouse_ack')
    rep.check('ps2.mouse_write_prefix', d.find(mov_r8b(CMD_WRITE_AUX), wa, we) >= 0)

    # the ACK protocol: 0xFE resends capped at 5, 0xFA is the only pass
    aa, ae = region(img, 'ps2_mouse_ack', 'ps2_bringup')
    rep.check('ps2.ack_retry_budget',
              d.find(b'\x41\xBA' + struct.pack('<I', ACK_RETRY), aa, ae) >= 0)
    # 0xFE / 0xFA do not fit a sign extended imm8: cmp eax, imm32
    resend = d.find(b'\x3D' + struct.pack('<I', MOUSE_RESEND), aa, ae)
    ack = d.find(b'\x3D' + struct.pack('<I', MOUSE_ACK), aa, ae)
    rep.check('ps2.resend_compare', resend >= 0, 'cmp eax, 0xFE')
    rep.check('ps2.ack_compare', ack >= 0, 'cmp eax, 0xFA')
    dec = d.find(b'\x41\xFF\xCA', aa, ae)
    j = jcc_after(img, dec + 3, 2) if dec >= 0 else None
    rep.check('ps2.resend_bounded_backwards',
              j is not None and j[1] < dec + 3,
              'dec r10d / jnz backwards inside the retry budget')

    # the frozen bring-up sequence, every step in order
    ba, be = region(img, 'ps2_bringup', 'keyboard_irq')
    rep.check('ps2.bringup_full_frame', d[ba:ba + len(INPUT_ENTER)] == INPUT_ENTER)
    pos = ordered_positions(img, ba, be, [
        mov_r8b(CMD_DISABLE_KBD), mov_r8b(CMD_DISABLE_AUX),
        mov_r8b(CMD_READ_CCB),
        b'\x83\xC8' + bytes((CCB_SET,)),        # or eax, 0x43
        b'\x83\xE0' + bytes((~CCB_CLEAR & 0xFF,)),  # and eax, ~0x30
        mov_r8b(CMD_WRITE_CCB),
        mov_r8b(CMD_ENABLE_KBD), mov_r8b(CMD_ENABLE_AUX),
        mov_r9b(MOUSE_CMD_RESET), mov_r9b(MOUSE_CMD_DEFAULT),
        mov_r9b(MOUSE_CMD_ENABLE),
    ])
    rep.check('ps2.frozen_bringup_sequence', pos is not None,
              'disable both, read CCB, set 0x43 clear 0x30, write back, '
              'enable both, reset/defaults/enable')
    reset = d.find(b'\x3D' + struct.pack('<I', MOUSE_RESET_OK), ba, be)
    rep.check('ps2.reset_selftest_pass_checked', reset >= 0, 'cmp eax, 0xAA')
    if pos and reset >= 0:
        rep.check('ps2.reset_pass_after_reset_cmd', reset > pos[8])
    id_store = rip_refs(img, ba, be, b'\x48\x89', 0x05, 0, 'mouse_id')
    rep.equal('ps2.mouse_id_recorded', len(id_store), 1)
    acks = [s for s, t in call_sites(img, ba, be) if t == img.at('ps2_mouse_ack')]
    rep.equal('ps2.three_ackd_mouse_commands', len(acks), 3)
    flushes = [s for s, t in call_sites(img, ba, be) if t == img.at('ps2_flush')]
    rep.equal('ps2.two_flushes', len(flushes), 2)


def check_leaf_purity(rep, img, name, stop):
    """A handler leaf: no calls to any known service, no console, no
    panic, no EOI write, no STI/CLI. The naive E8 scan may see stray
    bytes inside displacements, so only targets that land exactly on a
    known label count as calls."""
    at, end = region(img, name, stop)
    d = img.data
    known = {img.at(n) for n in M if n not in
             ('magic', 'version') and img.at(n) >= 0}
    calls = [(s, t) for s, t in call_sites(img, at, end) if t in known]
    rep.check('%s.no_service_calls' % name, not calls,
              ' '.join('%#x->%#x' % st for st in calls))
    rep.check('%s.no_eoi_write' % name,
              d.find(lapic_write_rsi(LAPIC_EOI, 0), at, end) < 0 and
              d.find(lapic_write_r13(LAPIC_EOI, 0), at, end) < 0)
    rep.check('%s.obf_phantom_guard' % name,
              ordered_positions(img, at, end,
                                [b'\xE4\x64', b'\xA8\x01', b'\xE4\x60']) is not None,
              'in al, 0x64 / test al, OBF / in al, 0x60')
    j = jcc_after(img, at + 4, 2)
    rep.check('%s.empty_obf_branches_out' % name, j is not None and j[1] > at + 6)


def check_aux_source_guard(rep, img, name, stop, opcode):
    """Status bit 5 filters the byte source before the 0x60 read: the
    AUX test sits between the OBF test and the data read, and its
    branch lands on exactly the phantom .out ret, so a mismatched
    source is never consumed and never counted. The keyboard branch
    is jnz (bit 5 set = mouse byte), the mouse branch jz (clear =
    keyboard byte); the polarities are pinned per handler."""
    at, end = region(img, name, stop)
    d = img.data
    obf = d.find(b'\xA8\x01', at, end)
    aux = d.find(b'\xA8\x20', at, end)
    read = d.find(b'\xE4\x60', at, end)
    rep.check('%s.aux_bit5_test' % name, aux >= 0, 'test al, 0x20')
    if aux < 0 or obf < 0 or read < 0:
        return
    rep.check('%s.aux_test_between_obf_and_read' % name,
              obf < aux < read)
    jo = jcc_after(img, obf + 2, 2)
    j = jcc_after(img, aux + 2, 2)
    rep.check('%s.aux_branch_returns_without_consuming' % name,
              j is not None and jo is not None and
              jcc_opcode(d, j[0]) == opcode and
              j[1] == jo[1] and d[j[1]] == 0xC3 and j[1] > read,
              'the source-mismatch branch lands on the phantom .out ret')


def check_keyboard(rep, img):
    d = img.data
    check_leaf_purity(rep, img, 'keyboard_irq', 'mouse_irq')
    check_aux_source_guard(rep, img, 'keyboard_irq', 'mouse_irq', 0x75)
    at, end = region(img, 'keyboard_irq', 'mouse_irq')
    inc = rip_refs(img, at, end, b'\x48\xFF', 0x05, 0, 'test_kbd')
    rep.equal('keyboard.one_count_inc', len(inc), 1)
    rep.check('keyboard.e0_prefix_compare', d.find(b'\x3C\xE0', at, end) >= 0)
    # the Pause prefix: E1 arms a two byte swallow counter, each payload
    # byte is consumed raw (dec then ret), never modifier-decoded
    rep.check('keyboard.e1_prefix_compare', d.find(b'\x3C\xE1', at, end) >= 0)
    e1_set = rip_refs(img, at, end, b'\x48\xC7', 0x05, 4, 'kbd_e1')
    rep.check('keyboard.e1_arms_two_payload_bytes',
              len(e1_set) == 1 and u32(d, e1_set[0] + 7) == 2)
    if e1_set:
        rep.check('keyboard.e1_prefix_returns', d[e1_set[0] + 11] == 0xC3,
                  'the prefix itself stores no buffer entry')
    e1_cmp = rip_refs(img, at, end, b'\x48\x83', 0x3D, 1, 'kbd_e1')
    rep.check('keyboard.e1_payload_guard',
              len(e1_cmp) == 1 and d[e1_cmp[0] + 7] == 0 and
              bool(e1_set) and e1_cmp[0] > e1_set[0])
    e1_dec = rip_refs(img, at, end, b'\x48\xFF', 0x0D, 0, 'kbd_e1')
    rep.check('keyboard.e1_payload_swallowed_raw',
              len(e1_dec) == 1 and d[e1_dec[0] + 7] == 0xC3,
              'dec then ret: no modifier decode, no buffer entry')
    rep.check('keyboard.break_bit_test',
              d.find(b'\x41\xF7\xC1\x80\x00\x00\x00', at, end) >= 0,
              'test r9d, 0x80')
    rep.check('keyboard.entry_stride_16',
              d.find(b'\x48\xC1\xE0\x04', at, end) >= 0, 'shl rax, 4')
    rep.check('keyboard.buffer_cap_compare',
              d.find(b'\x48\x83\xF8' + bytes((KBD_CAP,)), at, end) >= 0,
              'cmp rax, 120: a full buffer drops, never overwrites')
    drops = rip_refs(img, at, end, b'\x48\xFF', 0x05, 0, 'kbd_drops')
    rep.equal('keyboard.drop_counter', len(drops), 1)
    used = rip_refs(img, at, end, b'\x48\xFF', 0x05, 0, 'kbd_used')
    rep.equal('keyboard.fill_counter', len(used), 1)

    # the key maps, byte for byte against the independent copy
    ka = img.at('keymap')
    sa = img.at('keymap_shift')
    rep.check('keyboard.keymap_128_bytes', d[ka:ka + 128] == KEYMAP)
    rep.check('keyboard.keymap_shift_128_bytes', d[sa:sa + 128] == KEYMAP_SHIFT)
    rep.equal('keyboard.keymap_a', KEYMAP[ST_KBD_SCANCODE], ord('a'))
    rep.equal('keyboard.keymap_shift_a', KEYMAP_SHIFT[ST_KBD_SCANCODE], ord('A'))


def check_mouse(rep, img):
    d = img.data
    check_leaf_purity(rep, img, 'mouse_irq', 'wait_count')
    check_aux_source_guard(rep, img, 'mouse_irq', 'wait_count', 0x74)
    at, end = region(img, 'mouse_irq', 'wait_count')
    sync = d.find(b'\x41\xF7\xC1\x08\x00\x00\x00', at, end)
    rep.check('mouse.sync_bit3_test', sync >= 0, 'test r9d, 8')
    if sync >= 0:
        j = jcc_after(img, sync + 7, 2)
        rep.check('mouse.sync_failure_branches', j is not None and j[1] > sync + 9,
                  'a byte without the sync bit must branch to the drop path')
    rep.check('mouse.state_compares',
              d.find(b'\x48\x83\xF8\x01', at, end) >= 0 and
              d.find(b'\x48\x83\xF8\x02', at, end) >= 0)
    rep.check('mouse.sign_x_test',
              d.find(b'\x49\xF7\xC2\x10\x00\x00\x00', at, end) >= 0)
    rep.check('mouse.sign_y_test',
              d.find(b'\x49\xF7\xC2\x20\x00\x00\x00', at, end) >= 0)
    rep.check('mouse.sign_extension_sub_256',
              d.find(b'\x48\x81\xEA\x00\x01\x00\x00', at, end) >= 0 and
              d.find(b'\x48\x81\xE9\x00\x01\x00\x00', at, end) >= 0)
    rep.check('mouse.x_clamp',
              d.find(b'\x48\x3D' + struct.pack('<I', MOUSE_XMAX), at, end) >= 0)
    rep.check('mouse.y_clamp',
              d.find(b'\x48\x3D' + struct.pack('<I', MOUSE_YMAX), at, end) >= 0)
    rep.check('mouse.button_mask',
              d.find(b'\x83\xE0\x07', at, end) >= 0, 'and eax, 7')
    inc = rip_refs(img, at, end, b'\x48\xFF', 0x05, 0, 'test_mouse')
    rep.equal('mouse.one_packet_count_inc', len(inc), 1)
    bad = rip_refs(img, at, end, b'\x48\xFF', 0x05, 0, 'mouse_bad')
    rep.equal('mouse.bad_packet_counter', len(bad), 1)
    # byte 1 bits 6/7 (X/Y overflow) poison the whole packet: the test
    # follows the sync check and branches to the same .bad drop path
    ovf = d.find(b'\x41\xF7\xC1\xC0\x00\x00\x00', at, end)
    rep.check('mouse.overflow_bits67_test', ovf >= 0, 'test r9d, 0xC0')
    if ovf >= 0 and sync >= 0:
        rep.check('mouse.overflow_after_sync', ovf > sync)
        j = jcc_after(img, ovf + 7, 2)
        rep.check('mouse.overflow_branches_to_bad',
                  j is not None and jcc_opcode(d, j[0]) == 0x75 and
                  bool(bad) and j[1] == bad[0],
                  'an overflow byte drops the packet and counts it')
    if inc and bad:
        rep.check('mouse.bad_path_does_not_count', bad[0] > inc[0],
                  'the drop path lives after the complete packet path')


def check_wait_count(rep, img):
    at, end = region(img, 'wait_count', 'selftest')
    d = img.data
    rep.check('wait.compares_counter',
              d.find(b'\x4D\x39\x11', at, end) >= 0, 'cmp [r9], r10')
    rep.check('wait.round_budget',
              d.find(b'\x41\xBB' + struct.pack('<I', POLL_ROUNDS), at, end) >= 0)
    rep.check('wait.chunk_budget',
              d.find(b'\x48\xC7\xC2' + struct.pack('<I', POLL_CHUNK), at, end) >= 0)
    rep.check('wait.mode1_full_budget_spin',
              d.find(b'\x41\xB8\x01\x00\x00\x00', at, end) >= 0)
    dec = d.find(b'\x41\xFF\xCB', at, end)
    j = jcc_after(img, dec + 3, 2) if dec >= 0 else None
    rep.check('wait.bounded_backwards',
              j is not None and j[1] < dec + 3, 'dec r11d / jnz backwards')
    waits = [s for s, t in call_sites(img, at, end) if t == img.at('irq_sti_wait')]
    rep.equal('wait.single_window_owner', len(waits), 1)


def check_selftest(rep, img):
    at, end = region(img, 'selftest', 'init')
    d = img.data
    rep.check('selftest.full_frame', d[at:at + len(INPUT_ENTER)] == INPUT_ENTER)
    rep.check('selftest.cld_cli_on_entry',
              d[at + len(INPUT_ENTER):at + len(INPUT_ENTER) + 2] == b'\xFC\xFA')

    # the tick count derives from the public IRQ calibration evidence
    hz = rip_refs(img, at, end, b'\x48\x8B', 0x05, 0, 'irq_calib_hz')
    rep.equal('selftest.reads_irq_calib_hz', len(hz), 1)
    dpos = ordered_positions(img, at, end, [
        mov_ecx(TICK_HZ), b'\x48\xF7\xF1', b'\x49\x89\xC6',
    ])
    rep.check('selftest.derivation_divides_by_tick_hz', dpos is not None)
    if hz and dpos:
        rep.check('selftest.derivation_reads_measured_hz', hz[0] < dpos[0])

    # every window goes through the IRQ helper, never a private sti
    waits = [s for s, t in call_sites(img, at, end) if t == img.at('irq_sti_wait')]
    rep.equal('selftest.three_direct_windows', len(waits), 3)
    wcalls = [s for s, t in call_sites(img, at, end) if t == img.at('wait_count')]
    rep.equal('selftest.three_counter_waits', len(wcalls), 3)
    allowed = {img.at(n) for n in ('irq_sti_wait', 'wait_count', 'ps2_flush',
                                   'ps2_cmd', 'ps2_read_data', 'ioapic_read',
                                   'ioapic_write', 'cpu_com1_puts',
                                   'cpu_com1_puthex')}
    foreign = [(s, t) for s, t in call_sites(img, at, end) if t not in allowed]
    rep.check('selftest.no_foreign_calls', not foreign,
              ' '.join('%#x->%#x' % st for st in foreign))
    flushes = [s for s, t in call_sites(img, at, end) if t == img.at('ps2_flush')]
    rep.check('selftest.masked_leg_drains', len(flushes) >= 1)
    rte_writes = [s for s, t in call_sites(img, at, end) if t == img.at('ioapic_write')]
    rep.equal('selftest.eight_rte_writes', len(rte_writes), 8)
    rte_reads = [s for s, t in call_sites(img, at, end) if t == img.at('ioapic_read')]
    rep.equal('selftest.four_rte_reads', len(rte_reads), 4)

    # leg 0/1 evidence: the measured VER and the CCB read back
    cmps = cmp_qword_sites(img, at, end)

    def cmp_count(label, val, delta=0):
        want = img.at(label) + delta
        return sum(1 for _, t, v in cmps if t == want and v == val)

    rep.equal('selftest.ver_recorded_nonzero', cmp_count('ioapic_ver', 0), 1)
    rep.equal('selftest.entry_floor_compare', cmp_count('ioapic_max', RTE_MIN), 1)
    rep.check('selftest.ccb_readback_mask',
              d.find(b'\x83\xE0' + bytes((CCB_CHECK_MASK,)), at, end) >= 0)
    rep.check('selftest.ccb_readback_value',
              d.find(b'\x83\xF8' + bytes((CCB_CHECK_VALUE,)), at, end) >= 0)
    rep.equal('selftest.mouse_id_standard', cmp_count('mouse_id', MOUSE_ID_STANDARD), 1)

    # leg 2: the masked keyboard leg demands count 0 AND two drained
    # bytes (make + break of sendkey b) before pin 1 may go live
    rep.equal('selftest.masked_count_zero', cmp_count('test_kbd', 0), 1)
    rep.check('selftest.masked_drain_two_bytes',
              d.find(b'\x83\xFB\x02', at, end) >= 0, 'cmp ebx, 2')
    rep.check('selftest.masked_poll_budget',
              d.find(b'\x41\xBF' + struct.pack('<I', POLL_ROUNDS), at, end) >= 0)

    # leg 3: the live RTE value and the frozen buffer contents
    rep.check('selftest.kbd_live_rte',
              d.find(mov_edx(RTE_KBD_LIVE), at, end) >= 0)
    rep.equal('selftest.kbd_final_compares', cmp_count('test_kbd', ST_KBD_FINAL), 2)
    rep.equal('selftest.kbd_used_compare', cmp_count('kbd_used', ST_KBD_FINAL), 1)
    rep.equal('selftest.kbd_drops_zero', cmp_count('kbd_drops', 0), 1)
    rep.equal('selftest.buffer_ascii_a', cmp_count('kbd_buf', ord('a')), 1)
    rep.equal('selftest.buffer_raw_make',
              cmp_count('kbd_buf', ST_KBD_SCANCODE, 8), 1)
    rep.equal('selftest.buffer_break_ascii_zero', cmp_count('kbd_buf', 0, 16), 1)
    rep.equal('selftest.buffer_raw_break',
              cmp_count('kbd_buf', ST_KBD_SCANCODE | SCANCODE_BREAK, 24), 1)

    # leg 4: the derived move packet, the masked release verdict and
    # the exact coordinate deltas
    # fasm may pick the imm64 or the sign extended imm32 mov form
    pp = d.find(b'\x48\xB8' + struct.pack('<Q', ST_MOUSE_PACKET), at, end)
    if pp < 0:
        pp = d.find(b'\x48\xC7\xC0' + struct.pack('<I', ST_MOUSE_PACKET), at, end)
    rep.check('selftest.derived_move_packet', pp >= 0,
              'mov rax, sync|signs|dx|dy = %#x' % ST_MOUSE_PACKET)
    if pp >= 0:
        rep.check('selftest.move_packet_compared',
                  d.find(b'\x48\x39\x05', pp, min(pp + 20, end)) >= 0,
                  'cmp [input_test_mouse_last], rax')
    mask_needle = b'\x48\x25' + struct.pack('<I', ST_MOUSE_LAST_MASK)
    mp = d.find(mask_needle, at, end)
    rep.check('selftest.release_mask_ffffcf', mp >= 0,
              'and rax, 0xFFFFCF: the sign bits carry no information on '
              'a zero delta, every other bit stays exact')
    if mp >= 0:
        rep.check('selftest.release_sync_compared',
                  d.find(b'\x48\x83\xF8' + bytes((MOUSE_SYNC,)),
                         mp, min(mp + 16, end)) >= 0)
    rep.equal('selftest.mouse_final_compares',
              cmp_count('test_mouse', ST_MOUSE_FINAL), 2)
    rep.equal('selftest.mouse_bad_zero', cmp_count('mouse_bad', 0), 1)
    rep.equal('selftest.left_button_seen', cmp_count('mouse_buttons', 1), 1)
    rep.equal('selftest.x_moved_by_dx',
              cmp_count('mouse_x', MOUSE_XINIT + ST_MOUSE_DX), 1)
    rep.equal('selftest.y_moved_by_dy',
              cmp_count('mouse_y', MOUSE_YINIT - ST_MOUSE_DY), 1)
    rep.check('selftest.mouse_live_rte',
              d.find(mov_edx(RTE_MOUSE_LIVE), at, end) >= 0)

    # leg 5: masked one-shot, mode 2 silence window, mode 0 delivery,
    # then the mask freezes; the arming count comes from r14d
    rep.check('selftest.divide_16',
              d.find(lapic_write_r13(LAPIC_DIVIDE, DIVIDE_16), at, end) >= 0)
    masks = find_all(d[at:end], lapic_write_r13(LAPIC_LVT_TIMER, LVT_MASK))
    rep.equal('selftest.four_lvt_mask_freezes', len(masks), 4)
    oneshot = find_all(d[at:end],
                       lapic_write_r13(LAPIC_LVT_TIMER, VEC_TIMER | LVT_ONESHOT))
    rep.equal('selftest.one_oneshot_arm', len(oneshot), 1)
    arms = find_all(d[at:end],
                    b'\x45\x89\xB5' + struct.pack('<i', LAPIC_INIT_COUNT))
    rep.equal('selftest.two_derived_count_arms', len(arms), 2)
    rep.check('selftest.no_hardcoded_initial_count',
              d.find(lapic_write_r13(LAPIC_INIT_COUNT, 0), at, end) < 0)
    rep.check('selftest.silence_window_mode2',
              d.find(b'\x41\xB8\x02\x00\x00\x00', at, end) >= 0)
    rep.check('selftest.delivery_wait_mode0',
              d.find(b'\x45\x31\xC0', at, end) >= 0, 'xor r8d, r8d')
    elevens = find_all(d[at:end], b'\x48\xC7\xC1' + struct.pack('<I', TICKS_FINAL))
    rep.equal('selftest.two_waits_target_11', len(elevens), 2)
    rep.check('selftest.silence_window_budget',
              d.find(b'\x48\xC7\xC2' + struct.pack('<I', TIMER_WINDOW), at, end) >= 0)
    rep.check('selftest.delivery_wait_budget',
              d.find(b'\x48\xC7\xC2' + struct.pack('<I', WAIT_SPIN), at, end) >= 0)
    rep.equal('selftest.ticks_final_compares',
              cmp_count('irq_test_ticks', TICKS_FINAL), 2)
    rep.equal('selftest.no_stale_ten_compare',
              cmp_count('irq_test_ticks', TICKS_BASE), 0)
    if masks and oneshot:
        rep.check('selftest.final_mask_after_delivery', masks[-1] > oneshot[0])

    # the host handshake stages 1..6, each published exactly once
    stages = rip_refs(img, at, end, b'\x48\xC7', 0x05, 4, 'test_stage')
    vals = sorted(u32(d, p + 7) for p in stages)
    rep.equal('selftest.stage_sequence', vals, [1, 2, 3, 4, 5, 6])

    # all seven failure bits exist and land in the mask qword
    for bit in range(7):
        needle = b'\x49\x83\xCC' + bytes((1 << bit,))
        rep.check('selftest.leg_%d_failure_bit' % bit,
                  d.find(needle, at, end) >= 0)
    rep.equal('selftest.mask_stores',
              len(rip_refs(img, at, end, b'\x4C\x89', 0x25, 0, 'test_mask')), 2)
    rep.check('selftest.failure_prints',
              any(t == img.at('cpu_com1_puts') for _, t in call_sites(img, at, end)) and
              any(t == img.at('cpu_com1_puthex') for _, t in call_sites(img, at, end)))
    rep.check('selftest.timeout_status',
              d.find(b'\xB8' + struct.pack('<i', EIO), at, end) >= 0)


def check_init(rep, img):
    at, end = region(img, 'init', 'code_end')
    d = img.data
    rep.check('init.full_frame', d[at:at + len(INPUT_ENTER)] == INPUT_ENTER)
    rep.check('init.if_precheck',
              d.find(b'\x9C\x58\xA9\x00\x02\x00\x00', at, end) >= 0,
              'pushfq / pop rax / test eax, IF')
    rep.check('init.ealready_guard',
              len(rip_refs(img, at, end, b'\x48\x83', 0x3D, 1, 'state')) >= 1)
    movs = (b'\x48\xB9' + struct.pack('<Q', IOAPIC_VA) +
            b'\x48\xBA' + struct.pack('<Q', IOAPIC_PA))
    p = d.find(movs, at, end)
    rep.check('init.frozen_va_pa_pair', p >= 0,
              'mov rcx, VA / mov rdx, PA back to back')
    sites = call_sites(img, at, end)
    allowed = {img.at(n): n for n in
               ('vmm_map_mmio', 'ioapic_bringup', 'ps2_bringup')}
    named = [(s, allowed[t]) for s, t in sites if t in allowed]
    unknown = [(s, t) for s, t in sites if t not in allowed]
    rep.check('init.no_foreign_calls', not unknown,
              ' '.join('%#x->%#x' % st for st in unknown))
    rep.equal('init.call_order', [n for _, n in named],
              ['vmm_map_mmio', 'ioapic_bringup', 'ps2_bringup'])
    state_set = rip_refs(img, at, end, b'\x48\xC7', 0x05, 4, 'state')
    rep.check('init.state_set_once',
              len(state_set) == 1 and u32(d, state_set[0] + 7) == 1)
    rep.check('init.eperm_status',
              d.find(b'\xB8' + struct.pack('<i', EPERM), at, end) >= 0)
    rep.check('init.ealready_status',
              d.find(b'\xB8' + struct.pack('<i', EALREADY), at, end) >= 0)


def check_dispatch(rep, img):
    """The IRQ dispatch branches must call the REAL input leaves with
    RCX = frame, and the input EOI site must exist (the IRQ layer owns
    the EOI, the input module never writes it)."""
    at, end = region(img, 'irq_dispatch', 'irq_sti_wait')
    d = img.data
    kbd_cmp = d.find(b'\x48\x83\xF8' + bytes((VEC_KEYBOARD,)), at, end)
    mouse_cmp = d.find(b'\x48\x83\xF8' + bytes((VEC_MOUSE,)), at, end)
    rep.check('dispatch.keyboard_vector_compare', kbd_cmp >= 0)
    rep.check('dispatch.mouse_vector_compare', mouse_cmp >= 0)
    kbd_calls = [s for s, t in call_sites(img, at, end) if t == img.at('keyboard_irq')]
    mouse_calls = [s for s, t in call_sites(img, at, end) if t == img.at('mouse_irq')]
    rep.equal('dispatch.calls_real_keyboard_leaf', len(kbd_calls), 1)
    rep.equal('dispatch.calls_real_mouse_leaf', len(mouse_calls), 1)
    frame_loads = find_all(d[at:end], b'\x48\x8B\x4D\xF8')
    rep.equal('dispatch.input_rcx_frame_loads', len(frame_loads), 2)
    if kbd_calls and mouse_calls and frame_loads:
        rep.check('dispatch.keyboard_frame_load_before_call',
                  any(at + f < kbd_calls[0] for f in frame_loads))
        rep.check('dispatch.mouse_frame_load_before_call',
                  any(kbd_calls[0] < at + f < mouse_calls[0] for f in frame_loads))
    eoi = find_all(d[at:end], lapic_write_rsi(LAPIC_EOI, 0))
    rep.equal('dispatch.two_eoi_sites', len(eoi), 2)


def check_ownership(rep, img, root):
    """The input module owns no STI and never writes the LAPIC EOI."""
    at, end = region(img, 'code_start', 'code_end')
    d = img.data
    rep.check('ownership.no_eoi_write_rsi',
              d.find(lapic_write_rsi(LAPIC_EOI, 0), at, end) < 0)
    rep.check('ownership.no_eoi_write_r13',
              d.find(lapic_write_r13(LAPIC_EOI, 0), at, end) < 0)
    hits = []
    for path in sorted((root / 'kernel' / 'input').rglob('*')):
        if path.suffix not in ('.asm', '.inc') or not path.is_file():
            continue
        text = path.read_text(encoding='utf-8')
        for m in re.finditer(r'^\s*sti\b', text, re.M):
            hits.append(path.name)
        for m in re.finditer(r'^\s*mov\s+dword\s+\[[^\]]*EOI[^\]]*\]', text, re.M):
            hits.append(path.name + ':eoi')
    rep.equal('ownership.source_no_sti_no_eoi', hits, [])
    kb = (root / 'kernel' / 'input' / 'keyboard.inc').read_text(encoding='utf-8')
    mo = (root / 'kernel' / 'input' / 'mouse.inc').read_text(encoding='utf-8')
    for name, text in (('keyboard', kb), ('mouse', mo)):
        rep.check('ownership.%s_no_interrupt_instructions' % name,
                  not re.search(r'^\s*(sti|cli)\b', text, re.M))
    st = (root / 'kernel' / 'input' / 'selftest.inc').read_text(encoding='utf-8')
    rep.check('ownership.selftest_owns_cli',
              len(re.findall(r'^\s*cli\b', st, re.M)) >= 2)


def check_sources(rep, root):
    module = root / 'kernel' / 'input'
    files = sorted(p.name for p in module.glob('*'))
    rep.equal('source.file_set', files, [
        'api.inc', 'data.inc', 'input.asm', 'ioapic.inc',
        'keyboard.inc', 'mouse.inc', 'ps2.inc', 'selftest.inc'])

    api = (module / 'api.inc').read_text(encoding='utf-8')
    rep.check('source.api_standalone',
              not re.search(r"^\s*include\b", api, re.M))
    rep.check('source.api_has_contract_prefix', 'INPUT_API_VERSION' in api)
    api_asserts = len(re.findall(r'\berr\b', api))
    rep.check('source.api_asserts_the_contract', api_asserts >= 30,
              '%d explicit assertions' % api_asserts)
    data_text = (module / 'data.inc').read_text(encoding='utf-8')
    data_asserts = len(re.findall(r'\berr\b', data_text))
    rep.check('source.data_asserts_the_layout', data_asserts >= 20,
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

    agg = (module / 'input.asm').read_text(encoding='utf-8')
    rep.check('source.standalone_block_guarded',
              'if ~ defined INPUT_HOSTED' in agg)
    order = re.findall(r"^include\s+'([^']+)'", agg, re.M)
    rep.equal('source.include_order', order, [
        'api.inc', 'ioapic.inc', 'ps2.inc', 'keyboard.inc',
        'mouse.inc', 'selftest.inc', 'data.inc'])


def check_position_independence(rep, asm_, root, out, low):
    hi_path = out / 'layout-hi.bin'
    ok, text = asm_.run(root / 'tests/stage2-input/layout.asm', hi_path,
                        org=KERNEL_BASE)
    if not rep.check('reloc.assembles_at_kernel_base', ok, text.strip()[-400:]):
        return
    hi = load_image(hi_path, KERNEL_BASE)
    delta = KERNEL_BASE
    labels = ['code_start', 'code_end', 'data_start', 'data_end',
              'init', 'ioapic_read', 'ioapic_write', 'ioapic_bringup',
              'ps2_wait_ibf', 'ps2_wait_obf', 'ps2_cmd', 'ps2_write_data',
              'ps2_read_data', 'ps2_flush', 'ps2_mouse_write',
              'ps2_mouse_ack', 'ps2_bringup', 'keyboard_irq', 'mouse_irq',
              'wait_count', 'selftest', 'keymap', 'keymap_shift',
              'msg_selftest', 'msg_nl',
              'vmm_map_mmio', 'irq_sti_wait', 'cpu_panic', 'irq_dispatch',
              'irq_test_ticks', 'irq_calib_hz']
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
    for p in find_all(lo_body, b'\x48\xB9'):
        if p + 10 > len(lo_body):
            continue
        lv = struct.unpack_from('<Q', lo_body, p + 2)[0]
        hv = struct.unpack_from('<Q', hi_body, p + 2)[0]
        if (low['code_start'] <= lv < low['data_end'] and
                hv == lv + delta):
            allowed.update(range(p + 2, p + 10))
            windows += 1
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
    # the bring-up RTE must carry mask = 1: a live pin at bring-up would
    # let firmware noise race the configuration
    ('ioapic-rte-mask-removed', 'kernel/input/ioapic.inc',
     '        mov     ecx, INPUT_IOAPIC_IDX_KBD_LO\n'
     '        mov     edx, INPUT_RTE_KBD_MASKED\n'
     '        call    input_ioapic_write\n',
     '        mov     ecx, INPUT_IOAPIC_IDX_KBD_LO\n'
     '        mov     edx, INPUT_RTE_KBD_LIVE\n'
     '        call    input_ioapic_write\n', 'check'),
    # the frozen keyboard vector may not drift, the private assertion fires
    ('keyboard-vector-drift', 'kernel/input/api.inc',
     'INPUT_VEC_KEYBOARD  = 0x21', 'INPUT_VEC_KEYBOARD  = 0x22', 'assemble'),
    # the CCB must force the translation bit 6 on: dropping it from the
    # OR assembles fine, the byte gate must catch the weakened sequence
    ('translation-bit-drift', 'kernel/input/ps2.inc',
     '        or      eax, INPUT_CCB_SET\n',
     '        or      eax, INPUT_CCB_SET and not 0x40\n', 'check'),
    # the ACK protocol must accept 0xFA, nothing else
    ('mouse-ack-missing', 'kernel/input/ps2.inc',
     '        cmp     eax, INPUT_MOUSE_ACK\n',
     '        cmp     eax, INPUT_MOUSE_RESET_OK\n', 'check'),
    # the keyboard dispatch branch may not disappear
    ('dispatch-kbd-branch-removed', 'kernel/irq/dispatch.inc',
     '        cmp     rax, IRQ_VEC_KEYBOARD\n'
     '        je      .keyboard\n', '', 'check'),
    # the mandatory sync bit 3 check may not disappear
    ('packet-bit3-check-removed', 'kernel/input/mouse.inc',
     '        test    r9d, INPUT_MOUSE_SYNC\n'
     '        jz      .bad\n', '', 'check'),
    # the mouse RTE index must derive from pin 12, the assertion fires
    ('mouse-rte-index-drift', 'kernel/input/api.inc',
     'INPUT_IOAPIC_IDX_MOUSE_LO = 0x10 + 2 * 12 ; pin 12, low dword (0x28)',
     'INPUT_IOAPIC_IDX_MOUSE_LO = 0x38 ; pin 12, low dword (0x28)', 'assemble'),
    # the zero-delta mask may only drop the two sign bits: widening it
    # to also drop the sync bit would silently weaken the release verdict
    ('mouse-last-mask-widened', 'kernel/input/api.inc',
     'INPUT_MOUSE_B1_MASK = 0xFF and not (INPUT_MOUSE_SIGN_X or INPUT_MOUSE_SIGN_Y)',
     'INPUT_MOUSE_B1_MASK = 0xFF and not (INPUT_MOUSE_SIGN_X or INPUT_MOUSE_SIGN_Y or INPUT_MOUSE_SYNC)',
     'assemble'),
    # the timer leg adds EXACTLY one tick, the derivation is frozen
    ('timer-final-drift', 'kernel/input/api.inc',
     'INPUT_IRQ_TICKS_FINAL = INPUT_IRQ_TICKS_BASE + 1',
     'INPUT_IRQ_TICKS_FINAL = INPUT_IRQ_TICKS_BASE + 2', 'assemble'),
    # each handler must filter the AUX source bit before reading 0x60;
    # the polarities differ (keyboard jnz, mouse jz), so each guard is
    # pinned and fixture-broken separately: removing either one alone
    # must be caught, a shared pin could not prove both handlers
    ('keyboard-aux-check-removed', 'kernel/input/keyboard.inc',
     '        test    al, INPUT_PS2_STAT_AUX\n'
     '        jnz     .out                    ; mouse byte: leave it for the mouse IRQ\n',
     '', 'check'),
    ('mouse-aux-check-removed', 'kernel/input/mouse.inc',
     '        test    al, INPUT_PS2_STAT_AUX\n'
     '        jz      .out                    ; keyboard byte: leave it for the kbd IRQ\n',
     '', 'check'),
    # the Pause payload must never reach the modifier state machine:
    # disarming the swallow counter lets E1 1D set CTRL
    ('pause-e1-guard-disarmed', 'kernel/input/keyboard.inc',
     '        mov     qword [input_kbd_e1], 2 ; Pause: two payload bytes follow',
     '        mov     qword [input_kbd_e1], 0 ; Pause: two payload bytes follow',
     'check'),
    # byte 1 overflow bits 6/7 must drop the whole packet
    ('mouse-overflow-check-removed', 'kernel/input/mouse.inc',
     '        test    r9d, INPUT_MOUSE_OVERFLOW\n'
     '        jnz     .bad\n', '', 'check'),
]


def run_fixtures(rep, root, out, fasmg, fasm2_dir):
    checker = root / 'tests' / 'stage2-input' / 'check.py'
    for name, rel, old, new, where in FIXTURES:
        with tempfile.TemporaryDirectory(prefix='stage2-input-%s-' % name) as tmp:
            tree = Path(tmp)
            (tree / 'kernel').mkdir(parents=True)
            (tree / 'tests').mkdir(parents=True)
            for mod in ('cpu', 'mm', 'irq', 'input'):
                shutil.copytree(root / 'kernel' / mod, tree / 'kernel' / mod)
            shutil.copytree(root / 'tests' / 'stage2-input', tree / 'tests' / 'stage2-input')
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
    out = Path(args.out).resolve() if args.out else root / 'build' / 'stage2-input'
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
    ok, text = asm_.run(root / 'tests/stage2-input/layout.asm', low_path)
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
    check_ioapic(rep, img)
    check_ps2(rep, img)
    check_keyboard(rep, img)
    check_mouse(rep, img)
    check_wait_count(rep, img)
    check_selftest(rep, img)
    check_init(rep, img)
    check_dispatch(rep, img)
    check_ownership(rep, img, root)
    check_sources(rep, root)
    check_position_independence(rep, asm_, root, out, img)

    if not args.no_fixtures:
        run_fixtures(rep, root, out, asm_.fasmg, asm_.fasm2_dir)

    print('stage2-input: layout.bin %d bytes, code %d bytes, data %d bytes' %
          (len(img.data), img['code_end'] - img['code_start'],
           img['data_end'] - img['data_start']))
    return 0 if rep.print(args.verbose) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
