#!/usr/bin/env python3
"""Stage 2 IRQ module checks, task 70, contract IRQ-v1, extended by
task 89 for the IRQ-v2 input dispatch branches. No machine is started.

Everything here is measured, nothing is taken from a comment. The probe
tests/stage2-irq/layout.asm assembles kernel/irq/irq.asm exactly as the kernel
will include it (behind cpu.asm and mm.asm, whose services it consumes) and
appends a manifest of label addresses. This script then

  * compares the manifest constants and every data label offset against the
    fixed values of contract IRQ-v1, repeated here independently of the
    assembly sources;
  * decodes the emitted bytes: the full 8259 ICW sequence with read back,
    the APIC base MSR check, the frozen LAPIC bring-up write order, the
    descending bounded ISR flush, the PIT calibration window with its bounded poll and
    its explicit zero-divisor guard, and the dispatch EOI that must precede
    the handler return;
  * proves the single-STI ownership: exactly one sti instruction in the whole
    kernel source tree, inside irq_sti_wait, and every return path of that
    helper executes cli first;
  * checks the data region is real zero bytes, page aligned at both ends,
    with the leg 5 skip-EOI switch defaulting to zero at its frozen offset;
  * checks the module links at the kernel base with identical bytes except
    the absolute-address immediates, which must shift by exactly the base;
  * re-runs the whole pipeline on purpose-broken copies of the sources and
    requires every one of them to fail.

The contract values below are deliberately duplicated instead of imported: if
the module and this file drift apart, one of them has to be fixed explicitly.

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
# contract IRQ-v1, fixed here independently of the sources
# ----------------------------------------------------------------------

PAGE = 0x1000
KERNEL_BASE = 0xFFFFFFFF80000000

VEC_TIMER = 0x20
VEC_SPURIOUS = 0xFF
# IRQ-v2: the input vectors, frozen between the timer and the spurious
VEC_KEYBOARD = 0x21
VEC_MOUSE = 0x2C
SVR_VALUE = 0x1FF
SVR_ENABLE = 0x100
LAPIC_PA = 0xFEE00000
LAPIC_VA = 0xFFFFC00002000000

LAPIC_ID = 0x020
LAPIC_VER = 0x030
LAPIC_TPR = 0x080
LAPIC_EOI = 0x0B0
LAPIC_SVR = 0x0F0
LAPIC_ISR = 0x100
LAPIC_ESR = 0x280
LAPIC_LVT_TIMER = 0x320
LAPIC_LVT_LINT0 = 0x350
LAPIC_LVT_LINT1 = 0x360
LAPIC_LVT_ERROR = 0x370
LAPIC_INIT_COUNT = 0x380
LAPIC_CUR_COUNT = 0x390
LAPIC_DIVIDE = 0x3E0
ISR_REGS = 8
ISR_BITS = 32

LVT_MASK = 0x10000
LVT_ONESHOT = 0x00000
LVT_PERIODIC = 0x20000
DIVIDE_16 = 0x3

APIC_BASE_MSR = 0x1B
APIC_BASE_ENABLE = 0x800
APIC_BASE_MASK = 0xFFFFFF000

PIC1_CMD = 0x20
PIC1_DATA = 0x21
PIC2_CMD = 0xA0
PIC2_DATA = 0xA1
PIC_ICW1 = 0x11
PIC_ICW2_MASTER = 0x20
PIC_ICW2_SLAVE = 0x28
PIC_ICW3_MASTER = 0x04
PIC_ICW3_SLAVE = 0x02
PIC_ICW4 = 0x01
PIC_OCW1_MASK = 0xFF
PIC_IO_DELAY = 0x80

PIT_CH2_DATA = 0x42
PIT_CMD = 0x43
PIT_CTRL = 0x61
PIT_MODE0_CH2 = 0xB0
PIT_GATE = 0x01
PIT_SPEAKER = 0x02
PIT_OUT2 = 0x20
PIT_COUNT = 0xFFFF
PIT_HZ = 1193182

CALIB_ROUNDS = 3
CALIB_SPIN = 0x10000000
CALIB_NOMINAL = 62500000
CALIB_TOL = 3125000
# the frozen tick contract is 10 ms (100 Hz); the runtime initial count
# is derived from the measured frequency, the nominal anchor is 625000
TICK_HZ = 100
TIMER_COUNT_NOMINAL = 625000
WAIT_SPIN = 0x70000000
ST_WINDOW = 0x1000000
PERIODIC_N = 20
FINAL_N = 10

EIO = -9
FRAME_VECTOR = 128
FRAME_RFLAGS = 160
RFLAGS_IF = 0x200
STACK_FRAME = 128

# the data layout, offsets from irq_data_start
OFF_STATE = 0
OFF_TICK_COUNT = 8
OFF_SPURIOUS = 16
OFF_TEST_TICKS = 24
OFF_TEST_MASK = 32
OFF_ISR_SEEN = 40
OFF_ISR_AFTER = 48
OFF_IF_VALUE = 56
OFF_SKIP_EOI = 64
OFF_CALIB_HZ = 72
OFF_CALIB_S0 = 80
OFF_CALIB_S1 = 88
OFF_CALIB_S2 = 96
OFF_FRAME_IF = 104
OFF_MESSAGES = 112

MSG_UNKNOWN = b'irq: unexpected vector\x00'
MSG_SELFTEST = b'K64-IRQ-SELFTEST failed legs = \x00'

MAGIC = b'K64IRQP1'
MANIFEST_VERSION = 1

# manifest entry order, see tests/stage2-irq/layout.asm
M = [
    'magic', 'version',
    'code_start', 'code_end', 'data_start', 'data_end',
    'irq_init', 'bringup', 'calibrate', 'dispatch', 'sti_wait', 'selftest',
    'state', 'tick_count', 'spurious', 'test_ticks', 'test_mask',
    'isr_seen', 'isr_after', 'if_value', 'skip_eoi',
    'calib_hz', 'calib_s0', 'calib_s1', 'calib_s2', 'frame_if',
    'msg_unknown', 'msg_selftest',
    'vec_timer', 'vec_spurious', 'svr_value', 'lapic_pa', 'lapic_va',
    'lvt_mask', 'lvt_periodic', 'divide_16', 'tick_hz', 'timer_count_nominal',
    'periodic_n', 'final_n', 'calib_rounds', 'calib_nominal', 'calib_tol',
    'pit_count', 'pit_hz', 'wait_spin', 'st_window', 'calib_spin',
    'vmm_map_mmio', 'cpu_irq_set_handler', 'cpu_panic', 'cpu_halt_forever',
    'cpu_com1_puts', 'cpu_com1_puthex',
    'off_test_ticks', 'off_test_mask', 'off_skip_eoi', 'off_if_value',
    'off_frame_if', 'off_isr_seen', 'off_isr_after', 'off_messages',
    'input_kbd_stub', 'input_mouse_stub', 'vec_keyboard', 'vec_mouse',
]

# label name -> frozen offset inside the data region
DATA_LABELS = {
    'state': OFF_STATE, 'tick_count': OFF_TICK_COUNT, 'spurious': OFF_SPURIOUS,
    'test_ticks': OFF_TEST_TICKS, 'test_mask': OFF_TEST_MASK,
    'isr_seen': OFF_ISR_SEEN, 'isr_after': OFF_ISR_AFTER,
    'if_value': OFF_IF_VALUE, 'skip_eoi': OFF_SKIP_EOI,
    'calib_hz': OFF_CALIB_HZ, 'calib_s0': OFF_CALIB_S0,
    'calib_s1': OFF_CALIB_S1, 'calib_s2': OFF_CALIB_S2,
    'frame_if': OFF_FRAME_IF, 'msg_unknown': OFF_MESSAGES,
}

# manifest constants that must not drift from the copies above
CONSTANTS = {
    'vec_timer': VEC_TIMER, 'vec_spurious': VEC_SPURIOUS,
    'vec_keyboard': VEC_KEYBOARD, 'vec_mouse': VEC_MOUSE,
    'svr_value': SVR_VALUE, 'lapic_pa': LAPIC_PA, 'lapic_va': LAPIC_VA,
    'lvt_mask': LVT_MASK, 'lvt_periodic': LVT_PERIODIC,
    'divide_16': DIVIDE_16, 'tick_hz': TICK_HZ,
    'timer_count_nominal': TIMER_COUNT_NOMINAL,
    'periodic_n': PERIODIC_N, 'final_n': FINAL_N,
    'calib_rounds': CALIB_ROUNDS, 'calib_nominal': CALIB_NOMINAL,
    'calib_tol': CALIB_TOL, 'pit_count': PIT_COUNT, 'pit_hz': PIT_HZ,
    'wait_spin': WAIT_SPIN, 'st_window': ST_WINDOW, 'calib_spin': CALIB_SPIN,
    'off_test_ticks': OFF_TEST_TICKS, 'off_test_mask': OFF_TEST_MASK,
    'off_skip_eoi': OFF_SKIP_EOI, 'off_if_value': OFF_IF_VALUE,
    'off_frame_if': OFF_FRAME_IF, 'off_isr_seen': OFF_ISR_SEEN,
    'off_isr_after': OFF_ISR_AFTER, 'off_messages': OFF_MESSAGES,
}

PROLOGUE = bytes.fromhex('55 48 89 E5 48 81 EC 80 00 00 00')
EPILOGUE = bytes.fromhex('C9 C3')
IRQ_ENTER = bytes.fromhex('55 53 56 57 41 54 41 55 41 56 41 57 48 83 EC 28')
IRQ_LEAVE = bytes.fromhex('48 83 C4 28 41 5F 41 5E 41 5D 41 5C 5F 5E 5B 5D')

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


def lapic_write(off, val):
    """The bytes of 'mov dword [rsi + off], imm32' with a disp32."""
    return b'\xC7\x86' + struct.pack('<i', off) + struct.pack('<I', val)


def pic_triple(port, val):
    """mov dx, port / mov al, val / out dx, al, then the delay port write."""
    return (b'\x66\xBA' + struct.pack('<H', port) +
            b'\xB0' + bytes((val,)) + b'\xEE' + b'\xE6\x80')


def call_sites(img, at, end, opcode=b'\xE8'):
    """Every E8 rel32 (or E9 jmp) in a region: (site, target) file offsets.

    Naive byte scan: candidates whose target leaves the image are dropped,
    the rest would have to land exactly on a label to matter.
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


def mov_rcx_needles(value):
    """The encodings fasm picks for 'mov rcx, value': sign extended imm32
    when it fits, full imm64 otherwise."""
    return [b'\x48\xC7\xC1' + struct.pack('<I', value & 0xFFFFFFFF),
            b'\x48\xB9' + struct.pack('<Q', value)]


def find_mov_rcx(img, at, end, value):
    for needle in mov_rcx_needles(value):
        p = img.data.find(needle, at, end)
        if p >= 0:
            return p
    return -1


def rip_refs(img, at, end, prefix, modrm, tail, label):
    """Sites of 'prefix modrm disp32 [tail bytes]' that address label.

    The prefix ends right before the modrm byte; tail counts the immediate
    bytes that follow the displacement and belong to the same instruction.
    """
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
    """The (target, end) of the short or near conditional jump right after
    pos, searching a few bytes; None when there is none."""
    body = img.data
    for j in range(pos, min(pos + limit, len(body) - 2)):
        if body[j] == 0x0F and 0x80 <= body[j + 1] <= 0x8F:
            rel = struct.unpack_from('<i', body, j + 2)[0]
            return j, j + 6 + rel
        if 0x70 <= body[j] <= 0x7F:
            rel = struct.unpack_from('<b', body, j + 1)[0]
            return j, j + 2 + rel
    return None


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
        print('stage2-irq: %d checks, %d failed' % (total, len(self.failures)))
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
            cmd += ['-i', 'IRQ_PROBE_ORG=%#x' % org]
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

    for name, off in DATA_LABELS.items():
        rep.equal('layout.%s_offset' % name,
                  img[name] - img['data_start'], off)

    lo, hi = img['code_start'], img['code_end']
    rep.check('layout.code_non_empty', hi > lo, '%#x..%#x' % (lo, hi))
    rep.equal('layout.api_emits_no_code', img['bringup'], lo)
    order = ['bringup', 'calibrate', 'dispatch', 'sti_wait', 'selftest', 'irq_init']
    chain = [img[name] for name in order]
    rep.check('layout.function_order', chain == sorted(chain) and
              all(lo <= v < hi for v in chain),
              'emission order: bringup calib dispatch sti_wait selftest init')
    rep.check('layout.code_before_data', hi <= img['data_start'])
    rep.check('layout.data_page_aligned',
              img['data_start'] % PAGE == 0 and img['data_end'] % PAGE == 0)
    rep.check('layout.data_size_page_multiple',
              (img['data_end'] - img['data_start']) % PAGE == 0 and
              img['data_end'] - img['data_start'] >= PAGE)
    rep.equal('layout.module_ends_with_data', img.at('data_end'), img.probe_at)
    for name in ('vmm_map_mmio', 'cpu_irq_set_handler', 'cpu_panic',
                 'cpu_halt_forever', 'cpu_com1_puts', 'cpu_com1_puthex'):
        rep.check('layout.%s_before_module' % name, 0 < img[name] < lo,
                  '%#x' % img[name])
    # IRQ-v2: the input leaf stubs sit between mm.asm and irq.asm, and
    # each is a single ret byte
    for name in ('input_kbd_stub', 'input_mouse_stub'):
        rep.check('layout.%s_before_module' % name, 0 < img[name] < lo,
                  '%#x' % img[name])
        rep.check('layout.%s_is_ret' % name,
                  img.data[img.at(name):img.at(name) + 1] == b'\xC3')


def check_data(rep, img):
    d = img.data
    start, end = img.at('data_start'), img.at('data_end')
    block = d[start:start + OFF_MESSAGES]
    rep.check('data.qword_block_all_zero', block == bytes(OFF_MESSAGES),
              '%d qwords must be real zero bytes' % (OFF_MESSAGES // 8))
    rep.equal('data.skip_eoi_default_zero',
              u64(d, start + OFF_SKIP_EOI), 0)
    rep.check('data.msg_unknown_bytes',
              d[img.at('msg_unknown'):img.at('msg_unknown') + len(MSG_UNKNOWN)]
              == MSG_UNKNOWN)
    rep.check('data.msg_selftest_bytes',
              d[img.at('msg_selftest'):img.at('msg_selftest') + len(MSG_SELFTEST)]
              == MSG_SELFTEST)
    tail_from = img.at('msg_selftest') + len(MSG_SELFTEST)
    # the newline message and the tail padding are the only bytes left;
    # everything after the two ASCII messages must be zero or the CR LF NL
    rest = d[tail_from:end]
    stripped = rest.lstrip(b'\r\n\x00')
    rep.check('data.tail_zero_after_messages', stripped == b'',
              '%d non-zero tail bytes' % len(stripped))
    rep.check('data.messages_ascii',
              all(0x20 <= ch < 0x80 for ch in
                  d[img.at('msg_unknown'):img.at('msg_unknown') + len(MSG_UNKNOWN) - 1]))


def check_pic(rep, img):
    at, end = region(img, 'irq_init', 'code_end')
    sequence = [
        (PIC1_CMD, PIC_ICW1), (PIC2_CMD, PIC_ICW1),
        (PIC1_DATA, PIC_ICW2_MASTER), (PIC2_DATA, PIC_ICW2_SLAVE),
        (PIC1_DATA, PIC_ICW3_MASTER), (PIC2_DATA, PIC_ICW3_SLAVE),
        (PIC1_DATA, PIC_ICW4), (PIC2_DATA, PIC_ICW4),
        (PIC1_DATA, PIC_OCW1_MASK), (PIC2_DATA, PIC_OCW1_MASK),
    ]
    needles = [pic_triple(port, val) for port, val in sequence]
    pos = ordered_positions(img, at, end, needles)
    if rep.check('pic.full_icw_sequence_in_order', pos is not None,
                 'ICW1/2/3/4 then OCW1, each with the delay write'):
        rep.check('pic.mask_writes_last', pos[-1] > pos[-3],
                  'the two 0xFF mask writes close the sequence')
    # read back of both mask registers, each guarded by a conditional jump
    for name, port in (('master', PIC1_DATA), ('slave', PIC2_DATA)):
        needle = (b'\x66\xBA' + struct.pack('<H', port) +
                  b'\xEC\x3C' + bytes((PIC_OCW1_MASK,)))
        p = img.data.find(needle, at, end)
        if rep.check('pic.readback_%s' % name, p >= 0,
                     'in al, dx / cmp al, 0xFF at port %#x' % port):
            j = jcc_after(img, p + len(needle))
            rep.check('pic.readback_%s_guarded' % name,
                      j is not None and j[1] != p + len(needle) + 2,
                      'a failed read back must branch to the error path')


def check_msr_and_map(rep, img):
    at, end = region(img, 'irq_init', 'code_end')
    d = img.data

    # step a: map the LAPIC page through vmm_map_mmio with the frozen pair
    movs = (b'\x48\xB9' + struct.pack('<Q', LAPIC_VA) +
            b'\x48\xBA' + struct.pack('<Q', LAPIC_PA))
    p = d.find(movs, at, end)
    if rep.check('map.frozen_va_pa_pair', p >= 0,
                 'mov rcx, VA / mov rdx, PA back to back'):
        calls = [t for s, t in call_sites(img, p, min(p + 40, end))
                 if t == img.at('vmm_map_mmio')]
        rep.check('map.calls_vmm_map_mmio', len(calls) == 1)

    # step c: the APIC base MSR is read, checked and written back enabled
    rep.check('msr.number_1b', d.find(b'\xB9\x1B\x00\x00\x00', at, end) >= 0)
    rd = d.find(b'\x0F\x32', at, end)
    wr = d.find(b'\x0F\x30', at, end)
    rep.check('msr.rdmsr_present', rd >= 0)
    rep.check('msr.wrmsr_after_rdmsr', wr > rd >= 0)
    rep.check('msr.enable_bit_tested',
              d.find(b'\xA9\x00\x08\x00\x00', at, end) >= 0,
              'test eax, 0x800')
    rep.check('msr.enable_bit_written_back',
              d.find(b'\x48\x0D\x00\x08\x00\x00', at, end) >= 0,
              'or rax, 0x800 before wrmsr')
    rep.check('msr.base_mask_compared',
              d.find(b'\x49\xB8' + struct.pack('<Q', APIC_BASE_MASK), at, end) >= 0)
    rep.check('msr.base_pa_compared',
              d.find(b'\x49\xB8' + struct.pack('<Q', LAPIC_PA), at, end) >= 0)

    # step f: the handler registration passes irq_dispatch itself
    ph = find_mov_rcx(img, at, end, img['dispatch'])
    if rep.check('handler.mov_rcx_dispatch', ph >= 0):
        calls = [t for s, t in call_sites(img, ph, min(ph + 40, end))
                 if t == img.at('cpu_irq_set_handler')]
        rep.check('handler.calls_set_handler', len(calls) == 1)

    # irq_init calls exactly the four contract services, in the frozen order
    sites = call_sites(img, at, end)
    allowed = {img.at(n): n for n in
               ('vmm_map_mmio', 'bringup', 'calibrate', 'cpu_irq_set_handler')}
    named = []
    unknown = []
    for s, t in sites:
        if t in allowed:
            named.append((s, allowed[t]))
        else:
            unknown.append((s, t))
    rep.check('init.no_foreign_calls', not unknown,
              ' '.join('%#x->%#x' % st for st in unknown))
    rep.equal('init.call_order', [n for _, n in named],
              ['vmm_map_mmio', 'bringup', 'calibrate', 'cpu_irq_set_handler'])
    rep.check('init.uniform_frame', img.data[at:at + len(PROLOGUE)] == PROLOGUE)
    rep.check('init.if_precheck',
              img.data.find(b'\x9C\x58\xA9\x00\x02\x00\x00', at, end) >= 0,
              'pushfq / pop rax / test eax, IF')
    rep.check('init.ealready_guard',
              len(rip_refs(img, at, end, b'\x48\x83', 0x3D, 1, 'state')) >= 1,
              'cmp qword [irq_state], 0')


def check_bringup(rep, img):
    at, end = region(img, 'bringup', 'calibrate')
    d = img.data
    rep.check('bringup.full_frame', d[at:at + len(IRQ_ENTER)] == IRQ_ENTER)
    sequence = [
        lapic_write(LAPIC_TPR, 0),
        lapic_write(LAPIC_SVR, SVR_VALUE),
        lapic_write(LAPIC_LVT_TIMER, LVT_MASK),
        lapic_write(LAPIC_LVT_LINT0, LVT_MASK),
        lapic_write(LAPIC_LVT_LINT1, LVT_MASK),
        lapic_write(LAPIC_LVT_ERROR, LVT_MASK),
    ]
    pos = ordered_positions(img, at, end, sequence)
    rep.check('bringup.frozen_write_order', pos is not None,
              'TPR=0, SVR=0x1FF, then timer/LINT0/LINT1/error masked')
    eoi = find_all(d[at:end], lapic_write(LAPIC_EOI, 0))
    rep.equal('bringup.one_eoi_site', len(eoi), 1)
    if pos is not None and eoi:
        rep.check('bringup.flush_after_masks', at + eoi[0] > pos[-1])
    # the flush is bounded: 32 bits per register, 8 registers, and the
    # register walk MUST descend ISR7..ISR0: an EOI clears the highest
    # in-service vector, so an ascending walk would spend EOIs on higher
    # registers and leave low bits in service forever
    rep.check('bringup.bit_budget_32',
              d.find(b'\xB9\x20\x00\x00\x00', at, end) >= 0)
    rep.check('bringup.register_start_isr7',
              d.find(b'\x41\xB8\x07\x00\x00\x00', at, end) >= 0,
              'mov r8d, IRQ_ISR_REGS - 1')
    dec8 = d.find(b'\x41\xFF\xC8', at, end)
    j8 = jcc_after(img, dec8 + 3, 2) if dec8 >= 0 else None
    rep.check('bringup.register_descends',
              dec8 >= 0 and j8 is not None and at < j8[1] < dec8 + 3,
              'dec r8d / jns backwards to the next lower register')
    rep.check('bringup.no_ascending_register_walk',
              d.find(b'\x41\x83\xF8\x08', at, end) < 0 and
              d.find(b'\x41\xFF\xC0', at, end) < 0,
              'cmp r8d, 8 / inc r8d are the broken ascending walk')
    dec = d.find(b'\xFF\xC9', at, end)
    rep.check('bringup.bounded_bit_loop', dec >= 0 and
              jcc_after(img, dec + 2, 2) is not None and
              jcc_after(img, dec + 2, 2)[1] < dec + 2,
              'dec ecx / jnz backwards')
    rep.check('bringup.leave_restore',
              d[end - len(IRQ_LEAVE) - 1:end] == IRQ_LEAVE + b'\xC3')


def check_dispatch(rep, img):
    at, end = region(img, 'dispatch', 'sti_wait')
    d = img.data
    rep.check('dispatch.uniform_frame', d[at:at + len(PROLOGUE)] == PROLOGUE)
    rep.check('dispatch.frame_vector_read',
              d.find(b'\x48\x8B\x81\x80\x00\x00\x00', at, end) >= 0,
              'mov rax, [rcx + 128]')
    rep.check('dispatch.timer_vector_compare',
              d.find(b'\x48\x83\xF8\x20', at, end) >= 0)
    rep.check('dispatch.spurious_vector_compare',
              d.find(b'\x48\x3D\xFF\x00\x00\x00', at, end) >= 0)

    # IRQ-v2: the keyboard and mouse vector compares, in the frozen
    # order after the spurious compare and before the unknown-vector
    # panic; the branch bodies live after the ret so the timer and
    # spurious byte sequences stay untouched
    kbd_cmp = d.find(b'\x48\x83\xF8' + bytes((VEC_KEYBOARD,)), at, end)
    mouse_cmp = d.find(b'\x48\x83\xF8' + bytes((VEC_MOUSE,)), at, end)
    sp_cmp = d.find(b'\x48\x3D\xFF\x00\x00\x00', at, end)
    rets = find_all(d[at:end], b'\xC3')
    rep.check('dispatch.keyboard_vector_compare', kbd_cmp >= 0)
    rep.check('dispatch.mouse_vector_compare', mouse_cmp >= 0)
    rep.check('dispatch.input_compares_after_spurious',
              sp_cmp < kbd_cmp < mouse_cmp)
    frame_loads = find_all(d[at:end], b'\x48\x8B\x4D\xF8')
    rep.equal('dispatch.input_rcx_frame_loads', len(frame_loads), 2)
    kbd_calls = [s for s, t in call_sites(img, at, end)
                 if t == img.at('input_kbd_stub')]
    mouse_calls = [s for s, t in call_sites(img, at, end)
                   if t == img.at('input_mouse_stub')]
    rep.equal('dispatch.calls_keyboard_leaf_once', len(kbd_calls), 1)
    rep.equal('dispatch.calls_mouse_leaf_once', len(mouse_calls), 1)
    if kbd_calls and mouse_calls and frame_loads and rets:
        rep.check('dispatch.input_bodies_after_return',
                  kbd_calls[0] > at + rets[0])
        rep.check('dispatch.keyboard_frame_load_before_call',
                  any(at + f < kbd_calls[0] for f in frame_loads))
        rep.check('dispatch.mouse_frame_load_before_call',
                  any(kbd_calls[0] < at + f < mouse_calls[0]
                      for f in frame_loads))

    # the unknown vector path panics inside the handler, before .timer
    panic = [s for s, t in call_sites(img, at, end) if t == img.at('cpu_panic')]
    rep.equal('dispatch.unknown_vector_panics', len(panic), 1)
    halt = [s for s, t in call_sites(img, at, end, b'\xE9')
            if t == img.at('cpu_halt_forever')]
    serial = [s for s, t in call_sites(img, at, end)
              if t in (img.at('cpu_com1_puts'), img.at('cpu_com1_puthex'))]
    rep.check('dispatch.halt_after_panic', len(halt) == 1 and
              panic and halt[0] > panic[0])
    rep.check('dispatch.no_serial_output', not serial)

    ticks = rip_refs(img, at, end, b'\xFF', 0x05, 0, 'tick_count')
    test_ticks = rip_refs(img, at, end, b'\xFF', 0x05, 0, 'test_ticks')
    spurious = rip_refs(img, at, end, b'\xFF', 0x05, 0, 'spurious')
    rep.equal('dispatch.lifetime_tick_inc', len(ticks), 1)
    rep.equal('dispatch.test_tick_inc', len(test_ticks), 1)
    rep.equal('dispatch.spurious_inc', len(spurious), 1)
    if panic and ticks:
        rep.check('dispatch.panic_before_timer_path', panic[0] < ticks[0])

    # the leg 6 evidence pair: current RFLAGS via pushfq, and the frame copy
    rep.check('dispatch.pushfq_evidence', d.find(b'\x9C\x58', at, end) >= 0)
    rep.equal('dispatch.if_mask_applied_twice',
              len(find_all(d[at:end], b'\x25\x00\x02\x00\x00')), 2)
    rep.check('dispatch.frame_rflags_read',
              d.find(b'\x48\x8B\x80\xA0\x00\x00\x00', at, end) >= 0,
              'mov rax, [rax + 160]')
    ifv = rip_refs(img, at, end, b'\x48\x89', 0x05, 0, 'if_value')
    frf = rip_refs(img, at, end, b'\x48\x89', 0x05, 0, 'frame_if')
    rep.equal('dispatch.if_value_store', len(ifv), 1)
    rep.equal('dispatch.frame_if_store', len(frf), 1)

    # ISR evidence: the timer bit of ISR1 (0x110) read before and after EOI
    isr = find_all(d[at:end], b'\x8B\x96\x10\x01\x00\x00')
    rep.equal('dispatch.isr_reads', len(isr), 2)
    seen = rip_refs(img, at, end, b'\x48\x89', 0x15, 0, 'isr_seen')
    after = rip_refs(img, at, end, b'\x48\x89', 0x15, 0, 'isr_after')
    rep.equal('dispatch.isr_seen_store', len(seen), 1)
    rep.equal('dispatch.isr_after_store', len(after), 1)

    # the leg 5 switch guards exactly the timer EOI write; IRQ-v2 adds
    # the second, unguarded .input_eoi site after the ret: the input
    # branches always EOI, the skip switch never touches them
    guard = rip_refs(img, at, end, b'\x48\x83', 0x3D, 1, 'skip_eoi')
    rep.equal('dispatch.skip_eoi_guard', len(guard), 1)
    eoi = find_all(d[at:end], lapic_write(LAPIC_EOI, 0))
    rep.equal('dispatch.two_eoi_sites', len(eoi), 2)
    rep.equal('dispatch.single_return', len(rets), 1)
    if len(eoi) == 2 and rets:
        rep.check('dispatch.input_eoi_after_return',
                  at + eoi[1] > at + rets[0],
                  'the .input_eoi site lives after the leave/ret')
        # the jmp back to .done may be short (EB) or near (E9); either
        # way it must land exactly on the leave of the single exit
        j = at + eoi[1] + 10
        done = at + rets[0] - 1
        if d[j] == 0xEB:
            tgt = j + 2 + struct.unpack_from('<b', d, j + 1)[0]
        elif d[j] == 0xE9:
            tgt = j + 5 + struct.unpack_from('<i', d, j + 1)[0]
        else:
            tgt = -1
        rep.check('dispatch.input_eoi_jmps_to_done', tgt == done,
                  'the input EOI write jumps straight to the leave/ret')
    if guard and eoi and rets:
        g, e, r = guard[0], at + eoi[0], at + rets[0]
        rep.check('dispatch.guard_before_eoi', g < e)
        rep.check('dispatch.eoi_before_return', e < r,
                  'the EOI write must precede the handler return')
        j = jcc_after(img, g + 8, 2)
        rep.check('dispatch.guard_jumps_over_eoi', j is not None and
                  j[1] > e and j[1] <= r,
                  'the skip branch lands past the EOI, at or before the exit')
        rep.check('dispatch.return_is_leave_ret', d[r - 1:r + 1] == EPILOGUE)
    if eoi and isr and after and spurious:
        e = eoi[0]
        rep.check('dispatch.isr_before_eoi', isr[0] < e)
        rep.check('dispatch.isr_after_eoi', isr[1] > e)
        rep.check('dispatch.after_store_follows_eoi', after[0] > at + e)
        rep.check('dispatch.spurious_path_has_no_eoi', spurious[0] > at + e,
                  'the spurious increment sits after the only EOI site')


def check_sti_wait(rep, img):
    at, end = region(img, 'sti_wait', 'selftest')
    body = img.data[at:end]
    rep.equal('dispatch.no_sti_in_wait_body_except_head', body[0], STI)
    stis = find_all(body, bytes((STI,)))
    clis = find_all(body, bytes((CLI,)))
    rets = find_all(body, bytes((RET,)))
    rep.equal('sti_wait.single_sti', len(stis), 1)
    rep.equal('sti_wait.two_cli_exits', len(clis), 2)
    rep.equal('sti_wait.three_returns', len(rets), 3)
    paired = all(any(c < r and c > stis[0] for c in clis) for r in rets)
    rep.check('sti_wait.every_return_after_cli', paired,
              'no path may return with IF = 1')
    rep.check('sti_wait.bounded_spin',
              body.find(b'\x48\xFF\xCA') >= 0,
              'dec rdx budget')
    dec = body.find(b'\x48\xFF\xCA')
    j = jcc_after(img, at + dec + 3, 2) if dec >= 0 else None
    rep.check('sti_wait.spin_loops_backwards',
              j is not None and j[1] < at + dec + 3)
    rep.equal('sti_wait.counts_test_ticks',
              len(rip_refs(img, at, end, b'\x48\x39', 0x0D, 0, 'test_ticks')), 1)
    for mode in (0, 1, 2):
        rep.check('sti_wait.mode_%d_compare' % mode,
                  body.find(b'\x41\x83\xF8' + bytes((mode,))) >= 0)
    rep.check('sti_wait.timeout_status',
              body.find(b'\xB8' + struct.pack('<i', EIO)) >= 0)


def check_selftest(rep, img):
    at, end = region(img, 'selftest', 'irq_init')
    d = img.data
    rep.check('selftest.full_frame', d[at:at + len(IRQ_ENTER)] == IRQ_ENTER)
    rep.check('selftest.cli_on_entry',
              d[at + len(IRQ_ENTER):at + len(IRQ_ENTER) + 2] == b'\xFC\xFA')

    # every window opens through the single helper, nine times
    waits = [s for s, t in call_sites(img, at, end) if t == img.at('sti_wait')]
    rep.equal('selftest.nine_windows', len(waits), 9)
    allowed = {img.at(n) for n in ('sti_wait', 'cpu_com1_puts', 'cpu_com1_puthex')}
    foreign = [(s, t) for s, t in call_sites(img, at, end) if t not in allowed]
    rep.check('selftest.no_foreign_calls', not foreign)
    rep.check('selftest.failure_prints',
              any(t == img.at('cpu_com1_puts') for _, t in call_sites(img, at, end)) and
              any(t == img.at('cpu_com1_puthex') for _, t in call_sites(img, at, end)))

    # arming writes: one-shot, periodic, masked, and the initial count
    rep.equal('selftest.oneshot_arms',
              len(find_all(d[at:end], lapic_write(LAPIC_LVT_TIMER,
                                                  VEC_TIMER | LVT_ONESHOT))), 3)
    rep.equal('selftest.periodic_arms',
              len(find_all(d[at:end], lapic_write(LAPIC_LVT_TIMER,
                                                  VEC_TIMER | LVT_PERIODIC))), 3)
    rep.check('selftest.mask_freezes',
              len(find_all(d[at:end], lapic_write(LAPIC_LVT_TIMER, LVT_MASK))) >= 4)
    rep.equal('selftest.periodic_mask_freezes',
              len(find_all(d[at:end], lapic_write(LAPIC_LVT_TIMER,
                                                  LVT_PERIODIC | LVT_MASK))), 3)
    # every arming site programs the derived 10 ms count from r14d;
    # a hardcoded immediate initial count is a contract violation
    arm = b'\x44\x89\xB6' + struct.pack('<i', LAPIC_INIT_COUNT)
    arms = find_all(d[at:end], arm)
    rep.equal('selftest.initial_count_arms', len(arms), 6)
    rep.check('selftest.no_hardcoded_initial_count',
              d.find(b'\xC7\x86' + struct.pack('<i', LAPIC_INIT_COUNT),
                     at, end) < 0,
              'mov dword [rsi + 0x380], imm32 must not appear')
    # the derivation: r14 = irq_calib_hz / IRQ_TICK_HZ, measured not
    # hardcoded; calib_hz is read twice: the derivation and the leg 7 band
    hz_reads = rip_refs(img, at, end, b'\x48\x8B', 0x05, 0, 'calib_hz')
    rep.equal('selftest.calib_hz_reads', len(hz_reads), 2)
    dpos = ordered_positions(img, at, end, [
        b'\xB9' + struct.pack('<I', TICK_HZ),
        b'\x48\xF7\xF1',
        b'\x49\x89\xC6',
    ])
    rep.check('selftest.derivation_divides_by_tick_hz', dpos is not None,
              'mov ecx, 100 / div rcx / mov r14, rax in order')
    if dpos and hz_reads:
        rep.check('selftest.derivation_reads_calib_hz',
                  hz_reads[0] < dpos[0],
                  'the measured frequency feeds the division')
    if dpos and arms:
        rep.check('selftest.derivation_before_first_arm',
                  at + arms[0] > dpos[-1])
    rep.equal('selftest.divide_16',
              len(find_all(d[at:end], lapic_write(LAPIC_DIVIDE, DIVIDE_16))), 1)

    # per-leg resets and the leg 5 switch traffic
    resets = rip_refs(img, at, end, b'\x48\xC7', 0x05, 4, 'test_ticks')
    zero_resets = [p for p in resets if u32(d, p + 7) == 0]
    rep.equal('selftest.tick_resets', len(zero_resets), 5)
    switch = rip_refs(img, at, end, b'\x48\xC7', 0x05, 4, 'skip_eoi')
    ones = [p for p in switch if u32(d, p + 7) == 1]
    zeros = [p for p in switch if u32(d, p + 7) == 0]
    rep.equal('selftest.skip_eoi_set_once', len(ones), 1)
    rep.equal('selftest.skip_eoi_cleared_twice', len(zeros), 2)

    # the frozen exit counts: 20 for the periodic leg, 10 for the final leg
    cmps = rip_refs(img, at, end, b'\x48\x83', 0x3D, 1, 'test_ticks')
    vals = [d[p + 7] for p in cmps]
    rep.check('selftest.periodic_20_compare', vals.count(PERIODIC_N) >= 1)
    rep.check('selftest.final_10_compare', vals.count(FINAL_N) >= 1)

    # leg 6: handler IF must be 0, interrupted frame IF must be 0x200
    rep.check('selftest.if_value_zero_compare',
              len(rip_refs(img, at, end, b'\x48\x83', 0x3D, 1, 'if_value')) >= 1)
    fif = rip_refs(img, at, end, b'\x48\x81', 0x3D, 4, 'frame_if')
    rep.check('selftest.frame_if_0x200_compare',
              len(fif) == 1 and u32(d, fif[0] + 7) == RFLAGS_IF)

    # the observation windows: mode 1 (masked window) and mode 2 (leg 5)
    rep.check('selftest.masked_window_mode',
              d.find(b'\x41\xB8\x01\x00\x00\x00', at, end) >= 0)
    rep.check('selftest.blocked_window_mode',
              d.find(b'\x41\xB8\x02\x00\x00\x00', at, end) >= 0)
    rep.check('selftest.window_budget',
              d.find(b'\x48\xC7\xC2' + struct.pack('<I', ST_WINDOW), at, end) >= 0)
    rep.check('selftest.wait_budget',
              d.find(b'\x48\xC7\xC2' + struct.pack('<I', WAIT_SPIN), at, end) >= 0)

    # all nine failure bits exist and land in the mask qword
    for bit in range(9):
        if bit < 7:
            needle = b'\x49\x83\xCC' + bytes((1 << bit,))
        else:
            needle = b'\x49\x81\xCC' + struct.pack('<I', 1 << bit)
        rep.check('selftest.leg_%d_failure_bit' % bit,
                  d.find(needle, at, end) >= 0)
    rep.equal('selftest.mask_stores',
              len(rip_refs(img, at, end, b'\x4C\x89', 0x25, 0, 'test_mask')), 2)

    # leg 7: the sanity band is the frozen 5 percent of 62.5 MHz
    rep.check('selftest.calib_band_compare',
              d.find(b'\x48\x3D' + struct.pack('<I', CALIB_TOL), at, end) >= 0)
    rep.check('selftest.calib_nominal_subtract',
              d.find(b'\x48\x2D' + struct.pack('<I', CALIB_NOMINAL), at, end) >= 0)


def check_calib(rep, img):
    at, end = region(img, 'calibrate', 'dispatch')
    d = img.data
    rep.check('calib.full_frame', d[at:at + len(IRQ_ENTER)] == IRQ_ENTER)
    rep.check('calib.no_calls', not call_sites(img, at, end),
              'a leaf of irq_init: no nested calls, no console')

    # PIT ch2 mode 0, gate on, speaker off, known count lo then hi
    rep.check('calib.pit_mode0_command',
              d.find(b'\x66\xBA\x43\x00\xB0\xB0\xEE', at, end) >= 0)
    rep.check('calib.gate_on_speaker_off',
              d.find(b'\x66\xBA\x61\x00\xEC\x24\xFC\x0C\x01\xEE', at, end) >= 0)
    rep.check('calib.known_count_load',
              d.find(b'\x66\xBA\x42\x00\xB0\xFF\xEE\xB0\xFF\xEE', at, end) >= 0)

    # the LAPIC counter setup order: divide, masked, maximum initial count
    pos = ordered_positions(img, at, end, [
        lapic_write(LAPIC_DIVIDE, DIVIDE_16),
        lapic_write(LAPIC_LVT_TIMER, LVT_MASK),
        lapic_write(LAPIC_INIT_COUNT, 0xFFFFFFFF),
    ])
    rep.check('calib.counter_setup_order', pos is not None)

    # the bounded OUT2 poll: budget, test, decrement, backwards jnz, fail jmp
    budget = d.find(b'\xB9' + struct.pack('<I', CALIB_SPIN), at, end)
    rep.check('calib.poll_budget', budget >= 0, 'mov ecx, IRQ_CALIB_SPIN')
    test = d.find(b'\xA8\x20', at, end)
    rep.check('calib.out2_bit_test', test > budget >= 0, 'test al, 0x20')
    dec = d.find(b'\xFF\xC9', at, end)
    rep.check('calib.poll_decrement', dec > test >= 0)
    j = jcc_after(img, dec + 2, 2) if dec >= 0 else None
    rep.check('calib.poll_bounded_backwards', j is not None and
              budget < j[1] < dec + 2,
              'jnz .poll must loop back inside the budgeted region')
    rep.check('calib.poll_timeout_exit', j is not None and
              d[j[0] + 2] == 0xE9,
              'the exhausted budget falls into jmp .fail')

    # the conversion and its explicit zero-divisor guard
    rep.check('calib.current_count_read',
              d.find(b'\x8B\x96\x90\x03\x00\x00', at, end) >= 0)
    mul = d.find(b'\x48\xF7\xE1', at, end)
    guard = d.find(b'\x48\x85\xC9', at, end)
    div = d.find(b'\x48\xF7\xF1', at, end)
    rep.check('calib.multiply_pit_hz', mul >= 0)
    rep.check('calib.zero_divisor_guard', mul < guard < div,
              'test rcx, rcx between mul and div')
    if guard >= 0:
        jg = jcc_after(img, guard + 3, 2)
        rep.check('calib.guard_branches_past_div',
                  jg is not None and jg[1] > div)

    # three rounds, median of three, sanity band
    rep.check('calib.three_rounds',
              d.find(b'\x41\xBC\x03\x00\x00\x00', at, end) >= 0)
    rep.check('calib.round_loop_bounded',
              d.find(b'\x41\xFF\xCC', at, end) >= 0,
              'dec r12d')
    rep.check('calib.sample_slots',
              len(rip_refs(img, at, end, b'\x48\x8D', 0x0D, 0, 'calib_s0')) == 1,
              'lea rcx, [irq_calib_s0]')
    rep.check('calib.median_stored',
              len(rip_refs(img, at, end, b'\x4C\x89', 0x0D, 0, 'calib_hz')) == 1)
    rep.check('calib.nominal_subtract',
              d.find(b'\x48\x2D' + struct.pack('<I', CALIB_NOMINAL), at, end) >= 0)
    rep.check('calib.absolute_value', d.find(b'\x48\xF7\xD8', at, end) >= 0)
    rep.check('calib.tolerance_compare',
              d.find(b'\x48\x3D' + struct.pack('<I', CALIB_TOL), at, end) >= 0)
    rep.check('calib.leave_restore',
              d[end - len(IRQ_LEAVE) - 1:end] == IRQ_LEAVE + b'\xC3')


def check_sources(rep, root):
    module = root / 'kernel' / 'irq'
    files = sorted(p.name for p in module.glob('*'))
    rep.equal('source.file_set', files, [
        'api.inc', 'calib.inc', 'data.inc', 'dispatch.inc',
        'irq.asm', 'lapic.inc', 'selftest.inc'])

    api = (module / 'api.inc').read_text(encoding='utf-8')
    rep.check('source.api_standalone',
              not re.search(r"^\s*include\b", api, re.M))
    rep.check('source.api_has_contract_prefix', 'IRQ_API_VERSION' in api)
    api_asserts = len(re.findall(r'\berr\b', api))
    rep.check('source.api_asserts_the_contract', api_asserts >= 30,
              '%d explicit assertions' % api_asserts)
    data_text = (module / 'data.inc').read_text(encoding='utf-8')
    data_asserts = len(re.findall(r'\berr\b', data_text))
    rep.check('source.data_asserts_the_layout', data_asserts >= 10,
              '%d explicit assertions' % data_asserts)
    rep.check('source.data_no_uninitialised_reservation',
              not re.search(r"^\s*rb\b", data_text, re.M))
    rep.check('source.data_skip_eoi_defaults_zero',
              re.search(r"^irq_test_skip_eoi:\s+dq\s+0\b", data_text, re.M)
              is not None)

    for path in sorted(module.glob('*')):
        text = path.read_text(encoding='utf-8')
        rep.check('source.%s.no_tabs' % path.name, '\t' not in text)
        rep.check('source.%s.trailing_newline' % path.name, text.endswith('\n'))
        rep.check('source.%s.ascii_only' % path.name,
                  all(ord(ch) < 0x80 for ch in text))
        rep.check('source.%s.no_org' % path.name,
                  not re.search(r'^\s*org\b', text, re.M))
        rep.check('source.%s.no_use64' % path.name,
                  not re.search(r'^\s*use64\b', text, re.M))

    order = re.findall(r"^include\s+'([^']+)'",
                       (module / 'irq.asm').read_text(encoding='utf-8'), re.M)
    rep.equal('source.include_order', order, [
        'api.inc', 'lapic.inc', 'calib.inc', 'dispatch.inc',
        'selftest.inc', 'data.inc'])

    # the frozen STI ownership across the whole kernel source tree:
    # irq_sti_wait (IRQ-v1 self test window) and the desktop resident
    # loop (DESKTOP-v1, task 96) are the ONLY two legal STI owners
    hits = []
    for path in sorted((root / 'kernel').rglob('*')):
        if path.suffix not in ('.asm', '.inc') or not path.is_file():
            continue
        text = path.read_text(encoding='utf-8')
        for m in re.finditer(r'^\s*sti\b', text, re.M):
            hits.append(str(path.relative_to(root)))
    rep.equal('source.kernel_sti_sites', hits,
              ['kernel/desktop/desktop.inc', 'kernel/irq/selftest.inc'])
    st = (module / 'selftest.inc').read_text(encoding='utf-8')
    rep.check('source.selftest_owns_cli',
              len(re.findall(r'^\s*cli\b', st, re.M)) >= 3)
    dp = (module / 'dispatch.inc').read_text(encoding='utf-8')
    rep.check('source.dispatch_no_interrupt_instructions',
              not re.search(r'^\s*(sti|cli)\b', dp, re.M))


def check_position_independence(rep, asm_, root, out, low):
    hi_path = out / 'layout-hi.bin'
    ok, text = asm_.run(root / 'tests/stage2-irq/layout.asm', hi_path,
                        org=KERNEL_BASE)
    if not rep.check('reloc.assembles_at_kernel_base', ok, text.strip()[-400:]):
        return
    hi = load_image(hi_path, KERNEL_BASE)
    delta = KERNEL_BASE
    labels = ['code_start', 'code_end', 'data_start', 'data_end',
              'irq_init', 'bringup', 'calibrate', 'dispatch', 'sti_wait',
              'selftest', 'msg_unknown', 'msg_selftest',
              'vmm_map_mmio', 'cpu_irq_set_handler', 'cpu_panic']
    labels += list(DATA_LABELS)
    shifted = all(hi[n] == low[n] + delta for n in labels)
    rep.check('reloc.labels_shift_by_kernel_base', shifted)
    for name in CONSTANTS:
        rep.equal('reloc.%s_unchanged' % name, hi[name], low[name])

    # the data bytes are identical: all zero qwords plus the ASCII messages
    rep.check('reloc.data_bytes_identical',
              hi.data[hi.at('data_start'):hi.at('data_end')] ==
              low.data[low.at('data_start'):low.at('data_end')])

    # the code bytes are identical except imm64 windows that shift by delta
    lo_body = low.data[low.at('code_start'):low.at('code_end')]
    hi_body = hi.data[hi.at('code_start'):hi.at('code_end')]
    if not rep.check('reloc.code_same_length', len(lo_body) == len(hi_body)):
        return
    allowed = set()
    windows = 0
    # mov rcx, imm64 windows: the whole immediate shifts by the base
    for p in find_all(lo_body, b'\x48\xB9'):
        if p + 10 > len(lo_body):
            continue
        lv = struct.unpack_from('<Q', lo_body, p + 2)[0]
        hv = struct.unpack_from('<Q', hi_body, p + 2)[0]
        if (low['code_start'] <= lv < low['data_end'] and
                hv == lv + delta):
            allowed.update(range(p + 2, p + 10))
            windows += 1
    # mov rcx, sign extended imm32 windows: only the low 32 bits are
    # emitted, so the visible difference is the base modulo 2**32
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
              '%d shifted mov rcx, imm64 windows' % windows)
    # the registered handler immediate really is irq_dispatch, high and low
    ph = find_mov_rcx(low, low.at('code_start'), low.at('code_end'),
                      low['dispatch'])
    ok_handler = ph >= 0
    if ok_handler:
        rel = ph - low.at('code_start')
        ok_handler = any(hi_body[rel:rel + len(n)] == n
                         for n in mov_rcx_needles(hi['dispatch']))
    rep.check('reloc.handler_immediate_is_dispatch', ok_handler)


# ----------------------------------------------------------------------
# negative fixtures
# ----------------------------------------------------------------------

FIXTURES = [
    # the timer path must EOI before it returns, or the second tick never
    # comes (the jne .done context pins the guarded timer site, IRQ-v2
    # added a second EOI write at .input_eoi)
    ('dispatch-eoi-removed', 'kernel/irq/dispatch.inc',
     '        jne     .done\n'
     '        mov     dword [rsi + IRQ_LAPIC_EOI], 0\n',
     '        jne     .done\n', 'check'),
    # IRQ-v2: the keyboard dispatch branch may not disappear
    ('dispatch-kbd-branch-removed', 'kernel/irq/dispatch.inc',
     '        cmp     rax, IRQ_VEC_KEYBOARD\n'
     '        je      .keyboard\n', '', 'check'),
    # IRQ-v2: the input EOI site may not disappear either
    ('dispatch-input-eoi-removed', 'kernel/irq/dispatch.inc',
     '.input_eoi:\n'
     '        mov     dword [rsi + IRQ_LAPIC_EOI], 0\n',
     '.input_eoi:\n', 'check'),
    # IRQ-v2: the frozen keyboard vector may not drift, the private
    # assertion fires
    ('keyboard-vector-drift', 'kernel/irq/api.inc',
     'IRQ_VEC_KEYBOARD    = 0x21', 'IRQ_VEC_KEYBOARD    = 0x22', 'assemble'),
    # the bring-up must start with the timer masked
    ('bringup-mask-removed', 'kernel/irq/lapic.inc',
     '        mov     dword [rsi + IRQ_LAPIC_LVT_TIMER], IRQ_LVT_MASK\n',
     '', 'check'),
    # the frozen vector may not drift: the private assertion fires
    ('vector-drift', 'kernel/irq/api.inc',
     'IRQ_VEC_TIMER       = 0x20', 'IRQ_VEC_TIMER       = 0x21', 'assemble'),
    # a second sti anywhere in the kernel breaks the single-owner contract
    ('sti-leak', 'kernel/irq/irq.asm',
     'irq_init:\n        push    rbp\n',
     'irq_init:\n        sti\n        push    rbp\n', 'check'),
    # the division needs its explicit zero-divisor guard
    ('calib-divguard-removed', 'kernel/irq/calib.inc',
     '        test    rcx, rcx                ; explicit zero-divisor guard\n'
     '        jz      .fail\n', '', 'check'),
    # an unbounded poll would hang the boot instead of failing the leg
    ('calib-unbounded', 'kernel/irq/calib.inc',
     '        dec     ecx\n        jnz     .poll\n        jmp     .fail\n',
     '        jmp     .poll\n', 'check'),
    # the leg 5 hook must default to zero: the normal path always EOIs
    ('skip-eoi-default-one', 'kernel/irq/data.inc',
     'irq_test_skip_eoi:      dq 0', 'irq_test_skip_eoi:      dq 1', 'check'),
    # the master vector base is frozen at 0x20, right above the traps
    ('pic-icw2-drift', 'kernel/irq/api.inc',
     'IRQ_PIC_ICW2_MASTER = 0x20', 'IRQ_PIC_ICW2_MASTER = 0x30', 'check'),
    # the ISR flush must descend ISR7..ISR0: an ascending walk spends
    # each EOI on the highest in-service vector and never drains the
    # low bits (e.g. ISR0.bit5 survives while ISR7.bit31 eats its EOI)
    ('bringup-ascending-flush', 'kernel/irq/lapic.inc',
     '        mov     r8d, IRQ_ISR_REGS - 1\n'
     '.reg_loop:\n'
     '        mov     eax, r8d\n'
     '        shl     eax, 4\n'
     '        mov     edx, dword [rsi + rax + IRQ_LAPIC_ISR]\n'
     '        mov     ecx, IRQ_ISR_BITS\n'
     '.bit_loop:\n'
     '        test    edx, 1\n'
     '        jz      .next_bit\n'
     '        mov     dword [rsi + IRQ_LAPIC_EOI], 0\n'
     '.next_bit:\n'
     '        shr     edx, 1\n'
     '        dec     ecx\n'
     '        jnz     .bit_loop\n'
     '        dec     r8d\n'
     '        jns     .reg_loop\n',
     '        xor     r8d, r8d\n'
     '.reg_loop:\n'
     '        mov     eax, r8d\n'
     '        shl     eax, 4\n'
     '        mov     edx, dword [rsi + rax + IRQ_LAPIC_ISR]\n'
     '        mov     ecx, IRQ_ISR_BITS\n'
     '.bit_loop:\n'
     '        test    edx, 1\n'
     '        jz      .next_bit\n'
     '        mov     dword [rsi + IRQ_LAPIC_EOI], 0\n'
     '.next_bit:\n'
     '        shr     edx, 1\n'
     '        dec     ecx\n'
     '        jnz     .bit_loop\n'
     '        inc     r8d\n'
     '        cmp     r8d, IRQ_ISR_REGS\n'
     '        jb      .reg_loop\n', 'check'),
]


def run_fixtures(rep, root, out, fasmg, fasm2_dir):
    checker = root / 'tests' / 'stage2-irq' / 'check.py'
    for name, rel, old, new, where in FIXTURES:
        with tempfile.TemporaryDirectory(prefix='stage2-irq-%s-' % name) as tmp:
            tree = Path(tmp)
            (tree / 'kernel').mkdir(parents=True)
            (tree / 'tests').mkdir(parents=True)
            shutil.copytree(root / 'kernel' / 'cpu', tree / 'kernel' / 'cpu')
            shutil.copytree(root / 'kernel' / 'mm', tree / 'kernel' / 'mm')
            shutil.copytree(root / 'kernel' / 'irq', tree / 'kernel' / 'irq')
            shutil.copytree(root / 'tests' / 'stage2-irq', tree / 'tests' / 'stage2-irq')
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
    out = Path(args.out).resolve() if args.out else root / 'build' / 'stage2-irq'
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
    ok, text = asm_.run(root / 'tests/stage2-irq/layout.asm', low_path)
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
    check_pic(rep, img)
    check_msr_and_map(rep, img)
    check_bringup(rep, img)
    check_dispatch(rep, img)
    check_sti_wait(rep, img)
    check_selftest(rep, img)
    check_calib(rep, img)
    check_sources(rep, root)
    check_position_independence(rep, asm_, root, out, img)

    if not args.no_fixtures:
        run_fixtures(rep, root, out, asm_.fasmg, asm_.fasm2_dir)

    print('stage2-irq: layout.bin %d bytes, code %d bytes, data %d bytes' %
          (len(img.data), img['code_end'] - img['code_start'],
           img['data_end'] - img['data_start']))
    return 0 if rep.print(args.verbose) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
