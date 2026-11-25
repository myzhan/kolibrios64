#!/usr/bin/env python3
"""Stage 2 CPU module checks, task 27, task 113. No machine is started.

Everything here is measured, nothing is taken from a comment. The probe
tests/stage2-cpu/layout.asm assembles kernel/cpu/cpu.asm exactly as the kernel
will include it and appends a manifest of label addresses. This script then

  * reads the emitted binary and compares the GDT, TSS, both descriptor table
    registers, all 256 IDT gates and all 256 stubs against the fixed values of
    contract K64-FOUNDATION-v1, which are repeated here independently of the
    assembly sources;
  * rebuilds every stub byte by byte from the vector number alone, including
    the dummy error code rule, the push order that produces the 184 byte frame,
    the 16 byte alignment, the 32 byte shadow space and the iretq tail, and
    proves that the stubs are contiguous but not equally long;
  * checks that every exported function uses the uniform 128 byte frame that
    makes RSP 16 byte aligned for its nested calls, and that the trap stubs
    align before calling the dispatcher;
  * checks that the IST stacks are real zero bytes inside the data region, that
    the TSS descriptor is built from the real TSS label, and that the module
    links at VIRT_KERNEL_BASE with the same relative layout;
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
# contract K64-FOUNDATION-v1, fixed here independently of the sources
# ----------------------------------------------------------------------

PAGE = 0x1000
PHYS_LIMIT = 0x100000000
KERNEL_BASE = 0xFFFFFFFF80000000
KERNEL_SHIFT = 47
KERNEL_TOP = 0x1FFFF

GDT_SIZE = 56
GDT_ALIGN = 16
GDT_CODE_VALUE = 0x00AF9A000000FFFF
GDT_DATA_VALUE = 0x00CF92000000FFFF
GDT_USER_DATA_VALUE = 0x00CFF2000000FFFF
GDT_USER_CODE_VALUE = 0x00AFFA000000FFFF
GDT_DESC_ACCESSED = 1 << 40
GDT_RUNTIME_MASK = ((1 << 64) - 1) ^ GDT_DESC_ACCESSED
SEL_CODE = 0x08
SEL_DATA = 0x10
SEL_TSS = 0x18
SEL_USER_DATA = 0x28
SEL_USER_CODE = 0x30
DTR_SIZE = 10

TSS_SIZE = 104
TSS_ALIGN = 16
TSS_LIMIT = TSS_SIZE - 1
TSS_RSP0 = 4
TSS_IST = {i: 36 + (i - 1) * 8 for i in range(1, 8)}
TSS_IOMAP = 102
TSS_IOMAP_NONE = 104
TSS_DESC_TYPE = 0x89
TSS_DESC_TYPE_BUSY = 0x8B

IDT_VECTORS = 256
IDT_GATE_SIZE = 16
IDT_SIZE = IDT_VECTORS * IDT_GATE_SIZE
IDT_ALIGN = 16
IDT_TYPE_ATTR = 0x8E

FRAME_SIZE = 184
FRAME_CR2 = 0
FRAME_VECTOR = 128
FRAME_ERROR = 136
FRAME_RIP = 144
FRAME_CS = 152
FRAME_RFLAGS = 160
FRAME_RSP = 168
FRAME_SS = 176
FRAME_GPR = {
    'r15': 8, 'r14': 16, 'r13': 24, 'r12': 32, 'r11': 40, 'r10': 48,
    'r9': 56, 'r8': 64, 'rdi': 72, 'rsi': 80, 'rbp': 88, 'rbx': 96,
    'rdx': 104, 'rcx': 112, 'rax': 120,
}

ERROR_VECTORS = frozenset((8, 10, 11, 12, 13, 14, 17, 21, 29, 30))
IST_MAP = {8: 1, 2: 2, 18: 3, 0xF1: 4}
IST_SIZE = 0x4000
IST_COUNT = 5

# CPU_EXPECT, 32 bytes, and the only vectors cpu_trap_expect may arm
EXPECT_SIZE = 32
EXPECT_ERROR_MASK = 0
EXPECT_ERROR_VALUE = 8
EXPECT_CR2_MASK = 16
EXPECT_CR2_VALUE = 24
VECTOR_ALLOWED = (0, 3, 6, 14, 0xF0, 0xF1)

# the asynchronous window the IRQ layer may own, contract IRQ-v1
IRQ_VECTOR_MIN = 0x20

# APP-v1, task 113: the SYSCALL/SYSRET pair and the syscall ABI, fixed
# here independently of the sources. The ring 3 pair is 0x30 code / 0x28
# data (the provisional layout reported to the lead), so the STAR base is
# 0x20: 64-bit SYSRET loads CS = base + 16 | 3 and SS = base + 8 | 3.
EFER_SCE = 1
STAR_MSR = 0xC0000081
LSTAR_MSR = 0xC0000082
SFMASK_MSR = 0xC0000084
STAR_VALUE = (0x20 << 48) | (0x08 << 32)
SYSRET_CS = 0x33
SYSRET_SS = 0x2B
SFMASK_VALUE = 0x200
# FS-v1 M3 (task 133): the table grows 4 -> 16; numbers 0..3 stay the
# frozen APP-v1 set, 4..9 are the file syscalls, 10..15 stay unregistered
SYS_CALL_MAX = 16
SYS_VECTOR = 0x100

# opcodes whose mod=00 rm=101 form is followed by an immediate, and how many
# bytes it takes; without this an operand would be decoded one instruction too
# early and point at the wrong variable
REX_TAIL = {0x80: 1, 0xC6: 1, 0xC7: 4}

# the status codes the trap API uses, EAX
EINVAL = -1
ENOTSUP = -6
EPERM = -5
ENOTREADY = -7
EALREADY = -8
EIO = -9
EFAULT = -11
EBUSY = -12

STACK_FRAME = 128
SHADOW_SPACE = 32
STUB_ALIGN = 16

MAGIC = b'K64CPUP1'
MANIFEST_VERSION = 5

# manifest names that differ from the label the source exports
ALIASES = {'cpu_trap_dispatch': 'dispatch'}

# manifest entry order, see tests/stage2-cpu/layout.asm
M = [
    'magic', 'version',
    'gdt', 'gdt_size', 'gdt_null', 'gdt_code', 'gdt_data', 'gdt_tss',
    'gdtr', 'gdtr_size',
    'tss', 'tss_size', 'tss_rsp0', 'tss_iomap', 'tss_ist1',
    'idt', 'idt_size', 'idtr', 'idtr_size',
    'data_start', 'data_end',
    'ist1', 'ist2', 'ist3', 'ist4', 'panic_stack',
    'stubs_start', 'stubs_end', 'dispatch',
    'code_start', 'code_end',
    'frame_size', 'frame_cr2', 'frame_vector', 'frame_error', 'frame_rip',
    'vectors', 'iomap_none',
    'cpu_init', 'cpu_gdt_init', 'cpu_idt_init', 'cpu_set_rsp0',
    'cpu_com1_init', 'cpu_com1_putc', 'cpu_com1_puts', 'cpu_com1_puthex',
    'cpu_trap_expect', 'cpu_trap_finish', 'cpu_selftest',
    'cpu_panic', 'cpu_halt_forever',
    'expect_size', 'expect_err_mask', 'expect_err_value',
    'expect_cr2_mask', 'expect_cr2_value',
    'trap_armed', 'trap_result_ready', 'trap_vector',
    'trap_expect_rip', 'trap_resume_rip',
    'trap_err_mask', 'trap_err_value', 'trap_cr2_mask', 'trap_cr2_value',
    'trap_handler_frame', 'trap_result',
    'selftest_expect', 'selftest_out',
    'tss_desc_type', 'tss_desc_type_busy',
    'cpu_irq_set_handler', 'irq_handler', 'irq_vector_min',
    'gdt_user_data', 'gdt_user_code',
    'cpu_syscall_init', 'cpu_syscall_set_handler',
    'cpu_user_fault_set_handler', 'cpu_syscall_entry',
    'syscall_ready', 'user_fault_handler', 'syscall_handlers',
    'syscall_scratch_rsp',
    'gdt_user_data_value', 'gdt_user_code_value',
    'sel_user_data', 'sel_user_code',
    'star_value', 'sysret_cs', 'sysret_ss', 'sfmask_value',
    'sys_call_max', 'sys_vector', 'efer_sce',
    'star_msr', 'lstar_msr', 'sfmask_msr',
]

# the trap state variables, in the order the module declares them; the
# dispatcher may only consult these ones and no other private data
TRAP_STATE = (
    'trap_armed', 'trap_result_ready', 'trap_vector',
    'trap_expect_rip', 'trap_resume_rip',
    'trap_err_mask', 'trap_err_value', 'trap_cr2_mask', 'trap_cr2_value',
    'trap_handler_frame', 'trap_result',
)

# function regions, in emission order; each region must start with the
# uniform frame and end with leave/ret, except the two noreturn ones
FUNCTIONS = [
    'cpu_gdt_init', 'cpu_set_rsp0',
    'cpu_trap_expect', 'cpu_trap_finish', 'cpu_trap_dispatch',
    'cpu_irq_set_handler',
    'cpu_syscall_init', 'cpu_syscall_set_handler',
    'cpu_user_fault_set_handler', 'cpu_syscall_entry',
    'cpu_com1_init', 'cpu_com1_putc', 'cpu_com1_puts', 'cpu_com1_puthex',
    'cpu_halt_forever', 'cpu_panic', 'cpu_selftest',
    'cpu_init', 'cpu_idt_init',
]

PROLOGUE = bytes.fromhex('55 48 89 E5 48 81 EC 80 00 00 00')
EPILOGUE = bytes.fromhex('C9 C3')
HALT_BODY = bytes.fromhex('FA F4 EB FD')

# cpu_selftest keeps RDI in its own frame slot because rep stosq clobbers it
SELFTEST_SAVE_RDI = bytes.fromhex('48 89 7D E0')     # mov [rbp-32], rdi
SELFTEST_RESTORE_RDI = bytes.fromhex('48 8B 7D E0')  # mov rdi, [rbp-32]

STUB_PUSH_GPR = bytes.fromhex(
    '50 51 52 53 55 56 57'
    '41 50 41 51 41 52 41 53 41 54 41 55 41 56 41 57'
)
STUB_HEADER_SIZE = 16          # vector and error code push ahead of the GPRs
STUB_SAVE_CR2 = bytes.fromhex('0F 20 D0 50')
STUB_MID = bytes.fromhex('FC 48 89 E5 48 83 E4 F0 48 83 EC 20 48 89 E9')
STUB_TAIL = bytes.fromhex(
    '48 89 EC'
    '58'
    '41 5F 41 5E 41 5D 41 5C 41 5B 41 5A 41 59 41 58'
    '5F 5E 5D 5B 5A 59 58'
    '48 83 C4 10'
    '48 CF'
)

COM1_PATTERNS = (
    bytes.fromhex('66 BA F8 03'),           # mov dx, 0x3f8
    bytes.fromhex('BA F8 03 00 00'),        # mov edx, 0x3f8
)

# ----------------------------------------------------------------------
# tiny binary helpers
# ----------------------------------------------------------------------


def u8(data, off):
    return data[off]


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


def find_one(data, needle):
    return data.find(needle)


def mov_eax(value):
    """The bytes of 'mov eax, imm32'."""
    return b'\xB8' + struct.pack('<i', value)


def cmp_eax(value):
    """The bytes of 'cmp eax, imm', in the form the assembler picks."""
    if -128 <= value <= 127:
        return b'\x83\xF8' + bytes((value & 0xFF,))
    return b'\x3D' + struct.pack('<i', value)


def rip_targets(img, at, end, opcode, tail=0):
    """Where every 'opcode + mod=00 rm=101 + disp32' in a region points.

    The opcode argument ends with the modrm byte, so the displacement follows
    it directly; tail is how many immediate bytes sit after the displacement
    (they are part of the instruction, so they move the RIP of the next one).
    Returns (target, offset of the opcode in the file). Nothing here decodes
    x86: a false positive would have to land exactly on a state variable, and
    then it is a reference to it anyway.
    """
    d = img.data
    body = d[at:end]
    out = []
    for p in find_all(body, opcode):
        disp = struct.unpack_from('<i', body, p + len(opcode))[0]
        out.append((at + p + len(opcode) + 4 + tail + disp, at + p))
    return out


def rip_state_refs(img, at, end, names):
    """The state variables a region really refers to, by their labels.

    Every mod=00 rm=101 operand is decoded by hand. The bytes that follow the
    displacement belong to the same instruction, so they move the RIP the next
    instruction is relative to; REX_TAIL names the opcodes that carry one.
    """
    d = img.data
    wanted = {name: img.at(name) for name in names}
    found = []
    for j in range(at + 1, end - 5):
        if (d[j] & 0xC7) != 0x05:
            continue
        disp = struct.unpack_from('<i', d, j + 1)[0]
        target = j + 5 + REX_TAIL.get(d[j - 1], 0) + disp
        for name, addr in wanted.items():
            if addr == target:
                found.append(name)
                break
    return found


def gate(data, idt, vec):
    """Return (target, selector, ist, type_attr, reserved) of one IDT gate."""
    at = idt + vec * IDT_GATE_SIZE
    low, high = struct.unpack_from('<QQ', data, at)
    target = (low & 0xFFFF) | (((low >> 48) & 0xFFFF) << 16) | ((high & 0xFFFFFFFF) << 32)
    selector = (low >> 16) & 0xFFFF
    ist = (low >> 32) & 0x7
    type_attr = (low >> 40) & 0xFF
    reserved = (high >> 32) & 0xFFFFFFFF
    return target, selector, ist, type_attr, reserved


def vector_push(vec):
    """The exact bytes that push one vector without sign extension."""
    if vec < 0x80:
        return bytes((0x6A, vec))
    return b'\x68' + struct.pack('<I', vec)


def reverse_gpr_block(block):
    """The pops that undo a GPR push block, in the order the stub emits them."""
    pops, i = [], 0
    while i < len(block):
        if block[i] == 0x41:
            pops.append(bytes((0x41, 0x58 + block[i + 1] - 0x50)))
            i += 2
        else:
            pops.append(bytes((0x58 + block[i] - 0x50,)))
            i += 1
    return b''.join(reversed(pops))


def expected_stub(vec, dispatch, at):
    """Rebuild one stub byte by byte from its vector number."""
    out = b'' if vec in ERROR_VECTORS else b'\x6A\x00'
    out += vector_push(vec)
    out += STUB_PUSH_GPR
    out += STUB_SAVE_CR2
    out += STUB_MID
    call_at = len(out)
    out += b'\xE8\x00\x00\x00\x00'
    rel = (dispatch - (at + call_at + 5)) & 0xFFFFFFFF
    out = out[:call_at] + b'\xE8' + struct.pack('<I', rel) + out[call_at + 5:]
    out += STUB_TAIL
    return out


def expected_syscall_head(img, at):
    """Rebuild the syscall entry head byte by byte, APP-v1.

    Park the untrusted user RSP, switch to TSS.RSP0, push the hardware
    part of the 184 byte frame (SS, user RSP, RFLAGS, CS, RIP, error 0,
    the sentinel vector), then the GPR block and CR2 exactly like a trap
    stub, then the stub call protocol without the RCX load: the entry
    passes RBP itself after the number dispatch.
    """
    scratch = img.at('syscall_scratch_rsp')
    rsp0 = img.at('tss_rsp0')

    def disp(target, rip):
        return struct.pack('<i', target - rip)

    out = b'\x48\x89\x25' + disp(scratch, at + 7)    # mov [scratch], rsp
    out += b'\x48\x8B\x25' + disp(rsp0, at + 14)     # mov rsp, [tss_rsp0]
    out += b'\x6A' + bytes((SYSRET_SS,))             # push SS
    out += b'\xFF\x35' + disp(scratch, at + 22)      # push qword [scratch]
    out += b'\x41\x53'                               # push r11, the RFLAGS
    out += b'\x6A' + bytes((SYSRET_CS,))             # push CS
    out += b'\x51'                                   # push rcx, the RIP
    out += b'\x6A\x00'                               # push error code 0
    out += b'\x68' + struct.pack('<I', SYS_VECTOR)   # push the sentinel
    out += STUB_PUSH_GPR
    out += STUB_SAVE_CR2
    out += bytes.fromhex('FC 48 89 E5 48 83 E4 F0 48 83 EC 20')
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
        print('stage2-cpu: %d checks, %d failed' % (total, len(self.failures)))
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
            cmd += ['-i', 'CPU_PROBE_ORG=%#x' % org]
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
        """The label value, which is base relative when the probe was linked high."""
        return self.raw[ALIASES.get(name, name)]

    def at(self, name):
        """The file offset of a label."""
        return self[name] - self.base

    def slice(self, name):
        start, size = self.at(name), self.raw[name + '_size']
        return self.data[start:start + size]


def load_image(path, base=0):
    data = Path(path).read_bytes()
    at = max(find_all(data, MAGIC), default=-1)
    assert at >= 0, 'manifest magic not found'
    assert at + len(M) * 8 <= len(data), 'manifest truncated'
    return Image(data, at, base)


# ----------------------------------------------------------------------
# checks
# ----------------------------------------------------------------------


def check_manifest(rep, img):
    d = img.data
    rep.check('manifest.magic', True, MAGIC.decode())
    rep.equal('manifest.version', img['version'], MANIFEST_VERSION)
    rep.equal('manifest.gdt_size', img['gdt_size'], GDT_SIZE)
    rep.equal('manifest.tss_size', img['tss_size'], TSS_SIZE)
    rep.equal('manifest.idt_size', img['idt_size'], IDT_SIZE)
    rep.equal('manifest.frame_size', img['frame_size'], FRAME_SIZE)
    rep.equal('manifest.frame_cr2', img['frame_cr2'], FRAME_CR2)
    rep.equal('manifest.frame_vector', img['frame_vector'], FRAME_VECTOR)
    rep.equal('manifest.frame_error', img['frame_error'], FRAME_ERROR)
    rep.equal('manifest.frame_rip', img['frame_rip'], FRAME_RIP)
    rep.equal('manifest.vectors', img['vectors'], IDT_VECTORS)
    rep.equal('manifest.iomap_none', img['iomap_none'], TSS_IOMAP_NONE)

    # the TSS descriptor has two types and they must not be confused: the
    # image is built with the available one, LTR leaves the busy one behind
    rep.equal('manifest.tss_desc_type', img['tss_desc_type'], TSS_DESC_TYPE)
    rep.equal('manifest.tss_desc_type_busy', img['tss_desc_type_busy'],
              TSS_DESC_TYPE_BUSY)
    rep.equal('manifest.tss_desc_type_busy_is_bit1',
              img['tss_desc_type_busy'], img['tss_desc_type'] | 2)

    # CPU_EXPECT, the 32 byte block the trap API takes a pointer to
    rep.equal('manifest.expect_size', img['expect_size'], EXPECT_SIZE)
    rep.equal('manifest.expect_error_mask', img['expect_err_mask'], EXPECT_ERROR_MASK)
    rep.equal('manifest.expect_error_value', img['expect_err_value'], EXPECT_ERROR_VALUE)
    rep.equal('manifest.expect_cr2_mask', img['expect_cr2_mask'], EXPECT_CR2_MASK)
    rep.equal('manifest.expect_cr2_value', img['expect_cr2_value'], EXPECT_CR2_VALUE)

    # APP-v1: the user segments and the syscall ABI, repeated independently
    rep.equal('manifest.gdt_user_data_value', img['gdt_user_data_value'],
              GDT_USER_DATA_VALUE)
    rep.equal('manifest.gdt_user_code_value', img['gdt_user_code_value'],
              GDT_USER_CODE_VALUE)
    rep.equal('manifest.sel_user_data', img['sel_user_data'], SEL_USER_DATA)
    rep.equal('manifest.sel_user_code', img['sel_user_code'], SEL_USER_CODE)
    rep.equal('manifest.star_value', img['star_value'], STAR_VALUE)
    rep.equal('manifest.sysret_cs', img['sysret_cs'], SYSRET_CS)
    rep.equal('manifest.sysret_ss', img['sysret_ss'], SYSRET_SS)
    rep.equal('manifest.sfmask_value', img['sfmask_value'], SFMASK_VALUE)
    rep.equal('manifest.sys_call_max', img['sys_call_max'], SYS_CALL_MAX)
    rep.equal('manifest.sys_vector', img['sys_vector'], SYS_VECTOR)
    rep.equal('manifest.efer_sce', img['efer_sce'], EFER_SCE)
    rep.equal('manifest.star_msr', img['star_msr'], STAR_MSR)
    rep.equal('manifest.lstar_msr', img['lstar_msr'], LSTAR_MSR)
    rep.equal('manifest.sfmask_msr', img['sfmask_msr'], SFMASK_MSR)
    rep.check('manifest.sys_vector_past_the_idt',
              img['sys_vector'] >= IDT_VECTORS,
              'the sentinel must not collide with a real vector')
    rep.check('manifest.star_derives_the_sysret_pair',
              (((img['star_value'] >> 48) + 16) | 3) == img['sysret_cs'] and
              (((img['star_value'] >> 48) + 8) | 3) == img['sysret_ss'] and
              ((img['star_value'] >> 32) & 0xFFFF) == SEL_CODE,
              'SDM volume 2, SYSCALL/SYSRET')
    rep.check('manifest.sysret_lands_on_the_user_selectors',
              img['sysret_cs'] == (img['sel_user_code'] | 3) and
              img['sysret_ss'] == (img['sel_user_data'] | 3),
              'SYSRET must return into the ring 3 pair')

    # the block the self test passes: any error code at all, must be zero;
    # any CR2 at all
    block = img.at('selftest_expect')
    rep.equal('expect.selftest_error_mask', u64(d, block + EXPECT_ERROR_MASK),
              (1 << 64) - 1)
    rep.equal('expect.selftest_error_value', u64(d, block + EXPECT_ERROR_VALUE), 0)
    rep.equal('expect.selftest_cr2_mask', u64(d, block + EXPECT_CR2_MASK), 0)
    rep.equal('expect.selftest_cr2_value', u64(d, block + EXPECT_CR2_VALUE), 0)
    rep.check('expect.state_qwords_aligned',
              all(img[name] % 8 == 0 for name in
                  ('trap_expect_rip', 'trap_resume_rip', 'trap_err_mask',
                   'trap_err_value', 'trap_cr2_mask', 'trap_cr2_value',
                   'trap_handler_frame', 'trap_result', 'selftest_expect',
                   'selftest_out', 'irq_handler', 'user_fault_handler',
                   'syscall_handlers', 'syscall_scratch_rsp')),
              'the dispatcher reads these as qwords')
    rep.check('expect.result_buffers_are_one_frame_each',
              img['selftest_out'] >= img['trap_result'] + FRAME_SIZE,
              '%#x and %#x' % (img['trap_result'], img['selftest_out']))
    rep.check('expect.state_before_the_result_buffer',
              img['trap_handler_frame'] + 8 <= img['trap_result'],
              'the result copy must not overlap the armed state')

    # every region the manifest names must be inside the file and disjoint
    lo, hi = img['code_start'], img['code_end']
    rep.check('layout.code_non_empty', hi > lo, '%#x..%#x' % (lo, hi))
    rep.check('layout.data_after_code', img['data_start'] >= hi,
              'data %#x, code end %#x' % (img['data_start'], hi))
    for name in ('gdt', 'tss', 'idt', 'gdtr', 'idtr', 'data_start', 'data_end',
                 'ist1', 'ist2', 'ist3', 'ist4', 'panic_stack', 'stubs_start',
                 'stubs_end', 'dispatch', 'trap_result', 'selftest_out',
                 'selftest_expect', 'irq_handler', 'gdt_user_data',
                 'gdt_user_code', 'user_fault_handler', 'syscall_handlers',
                 'syscall_scratch_rsp'):
        value = img[name]
        rep.check('layout.%s_inside_image' % name, 0 < value <= len(d),
                  '%#x of %#x' % (value, len(d)))
    rep.check('layout.stubs_inside_code',
              lo <= img['stubs_start'] < img['stubs_end'] <= hi)
    rep.check('layout.dispatch_inside_code', lo <= img['dispatch'] < hi)
    rep.check('layout.data_page_aligned',
              img['data_start'] % PAGE == 0 and img['data_end'] % PAGE == 0)
    rep.check('layout.code_before_data', img['code_end'] <= img['data_start'])
    rep.equal('layout.module_ends_with_data', img.at('data_end'), img.probe_at)
    rep.check('layout.data_holds_every_stack',
              img['data_end'] - img['data_start'] >= IST_COUNT * IST_SIZE,
              '%d bytes of data for %d stacks' % (img['data_end'] - img['data_start'],
                                                  IST_COUNT))


def check_gdt(rep, img):
    d = img.data
    gdt = img['gdt']
    rep.check('gdt.aligned', gdt % GDT_ALIGN == 0,
              'GDT at %#x' % gdt)
    rep.equal('gdt.code_offset', img['gdt_code'] - gdt, SEL_CODE)
    rep.equal('gdt.data_offset', img['gdt_data'] - gdt, SEL_DATA)
    rep.equal('gdt.tss_offset', img['gdt_tss'] - gdt, SEL_TSS)
    rep.check('gdt.null_descriptor', u64(d, img['gdt_null']) == 0)
    rep.equal('gdt.code_value', u64(d, img['gdt_code']), GDT_CODE_VALUE)
    rep.equal('gdt.data_value', u64(d, img['gdt_data']), GDT_DATA_VALUE)

    # APP-v1: the ring 3 pair sits right behind the 16 byte TSS descriptor
    rep.equal('gdt.user_data_offset', img['gdt_user_data'] - gdt, SEL_USER_DATA)
    rep.equal('gdt.user_code_offset', img['gdt_user_code'] - gdt, SEL_USER_CODE)
    rep.equal('gdt.user_data_value', u64(d, img['gdt_user_data']),
              GDT_USER_DATA_VALUE)
    rep.equal('gdt.user_code_value', u64(d, img['gdt_user_code']),
              GDT_USER_CODE_VALUE)
    rep.equal('gdt.user_data_follows_tss', img['gdt_user_data'] - img['gdt_tss'], 16)
    rep.equal('gdt.ends_after_user_code', gdt + GDT_SIZE - img['gdt_user_code'], 8)
    for name, slot in (('user_data', 'gdt_user_data'),
                       ('user_code', 'gdt_user_code')):
        value = u64(d, img[slot])
        rep.check('gdt.%s_dpl3' % name, (value >> 45) & 3 == 3,
                  'a ring 3 segment needs DPL 3, got %d' % ((value >> 45) & 3))
        rep.check('gdt.%s_image_accessed_clear' % name,
                  value & GDT_DESC_ACCESSED == 0,
                  'the image keeps the built value; only hardware sets A')

    tss_at = img['gdt_tss']
    limit = u16(d, tss_at)
    base = u16(d, tss_at + 2)
    base |= u8(d, tss_at + 4) << 16
    base |= u8(d, tss_at + 7) << 24
    base |= u32(d, tss_at + 8) << 32
    rep.equal('gdt.tss_limit', limit, TSS_LIMIT)
    rep.equal('gdt.tss_type', u8(d, tss_at + 5), TSS_DESC_TYPE)
    rep.equal('gdt.tss_flags', u8(d, tss_at + 6), 0)
    rep.equal('gdt.tss_base', base, img['tss'])
    rep.equal('gdt.tss_reserved', u32(d, tss_at + 12), 0)

    gdtr = img['gdtr']
    rep.equal('gdtr.limit', u16(d, gdtr), GDT_SIZE - 1)
    rep.equal('gdtr.base', u64(d, gdtr + 2), gdt)
    rep.equal('gdtr.size', img['gdtr_size'], DTR_SIZE)


def check_tss(rep, img, stacks):
    d = img.data
    tss = img['tss']
    rep.check('tss.aligned', tss % TSS_ALIGN == 0, 'TSS at %#x' % tss)
    rep.equal('tss.rsp0_offset', img['tss_rsp0'] - tss, TSS_RSP0)
    rep.equal('tss.iomap_offset', img['tss_iomap'] - tss, TSS_IOMAP)
    rep.equal('tss.iomap_value', u16(d, tss + TSS_IOMAP), TSS_IOMAP_NONE)
    rep.check('tss.no_bitmap', TSS_IOMAP_NONE > TSS_LIMIT,
              'iomap %d, limit %d' % (TSS_IOMAP_NONE, TSS_LIMIT))
    rep.equal('tss.rsp0_start_value', u64(d, tss + TSS_RSP0), 0)
    rep.equal('tss.rsp1_zero', u64(d, tss + 12), 0)
    rep.equal('tss.rsp2_zero', u64(d, tss + 20), 0)
    rep.equal('tss.reserved1_zero', u64(d, tss + 28), 0)

    # ist1..ist4 point at the top of their own stack, ist5..7 stay unused
    for ist in range(1, 8):
        value = u64(d, tss + TSS_IST[ist])
        if ist <= 4:
            base = stacks[ist - 1]
            rep.equal('tss.ist%d_top' % ist, value, base + IST_SIZE)
            rep.check('tss.ist%d_page_aligned' % ist, value % PAGE == 0)
        else:
            rep.equal('tss.ist%d_unused' % ist, value, 0)


def check_stacks(rep, img):
    d = img.data
    stacks = [img['ist1'], img['ist2'], img['ist3'], img['ist4'], img['panic_stack']]
    ok_order = True
    for index, base in enumerate(stacks):
        rep.check('stack%d.page_aligned' % index, base % PAGE == 0, '%#x' % base)
        rep.check('stack%d.inside_data' % index,
                  img['data_start'] <= base and base + IST_SIZE <= img['data_end'],
                  '%#x..%#x' % (base, base + IST_SIZE))
        body = d[base:base + IST_SIZE]
        rep.check('stack%d.emitted_zero_bytes' % index, len(body) == IST_SIZE,
                  '%d bytes' % len(body))
        rep.check('stack%d.all_zero' % index, body.count(0) == len(body),
                  '%d non zero bytes' % (len(body) - body.count(0)))
        if index:
            ok_order = ok_order and base == stacks[index - 1] + IST_SIZE
    rep.check('stack.contiguous', ok_order,
              ' '.join('%#x' % base for base in stacks))
    rep.equal('stack.count', len(stacks), IST_COUNT)


def check_idt(rep, img):
    d = img.data
    idt = img['idt']
    rep.check('idt.aligned', idt % IDT_ALIGN == 0, 'IDT at %#x' % idt)
    targets, ok_sel, ok_type, ok_ist, ok_res, ok_range = [], True, True, True, True, True
    for vec in range(IDT_VECTORS):
        target, selector, ist, type_attr, reserved = gate(d, idt, vec)
        targets.append(target)
        ok_sel = ok_sel and selector == SEL_CODE
        ok_type = ok_type and type_attr == IDT_TYPE_ATTR
        ok_ist = ok_ist and ist == IST_MAP.get(vec, 0)
        ok_res = ok_res and reserved == 0
        ok_range = ok_range and img['stubs_start'] <= target < img['stubs_end']
    rep.check('idt.selector_is_code', ok_sel, 'selector %#x' % SEL_CODE)
    rep.check('idt.type_is_0x8e', ok_type, 'DPL0, present, interrupt gate')
    rep.check('idt.ist_map', ok_ist,
              ' '.join('%#x->%d' % (v, i) for v, i in sorted(IST_MAP.items())))
    rep.check('idt.reserved_zero', ok_res)
    rep.check('idt.targets_inside_stubs', ok_range,
              '%#x..%#x' % (img['stubs_start'], img['stubs_end']))
    rep.check('idt.targets_strictly_increasing',
              all(targets[i] < targets[i + 1] for i in range(IDT_VECTORS - 1)))
    rep.check('idt.all_targets_distinct', len(set(targets)) == IDT_VECTORS,
              '%d distinct' % len(set(targets)))
    lengths = sorted({targets[i + 1] - targets[i] for i in range(IDT_VECTORS - 1)})
    rep.check('idt.stubs_not_equal_length', len(lengths) >= 2,
              'stub lengths %s, the table must not assume one size'
              % ' '.join(str(n) for n in lengths))

    idtr = img['idtr']
    rep.equal('idtr.limit', u16(d, idtr), IDT_SIZE - 1)
    rep.equal('idtr.base', u64(d, idtr + 2), idt)
    rep.equal('idtr.size', img['idtr_size'], DTR_SIZE)


def stub_dummy_flag(code, vec):
    """Read the first two pushes of a stub and say whether a dummy came first.

    Returns True when the stub pushed a dummy error code, False when the
    hardware pushed one, None when the stub does not look like the contract.
    The vector push must come second and must not be sign extended, which is
    checked here as well.
    """
    if code[0] == 0x6A:
        first, at = code[1], 2
    elif code[0] == 0x68:
        first, at = struct.unpack_from('<I', code, 1)[0], 5
    else:
        return None
    if code[at] not in (0x6A, 0x68):
        # no second push: the vector itself came first, the CPU pushed the
        # error code before entering the stub
        return False if first == vec else None
    if code[at] == 0x6A:
        second = code[at + 1]
    else:
        second = struct.unpack_from('<I', code, at + 1)[0]
    if second != vec:
        return None
    return first == 0


def check_stubs(rep, img):
    d = img.data
    idt = img['idt']
    targets = [gate(d, idt, vec)[0] for vec in range(IDT_VECTORS)]
    dispatch = img['dispatch']

    rebuilt, ok_bytes, ok_len, ok_call = 0, True, True, True
    derived_errors = set()
    for vec, at in enumerate(targets):
        want = expected_stub(vec, dispatch, at)
        got = d[at:at + len(want)]
        if got != want:
            ok_bytes = False
            rebuilt += 1
        dummy = stub_dummy_flag(d[at:at + 8], vec)
        if dummy is None:
            ok_bytes = False
            rebuilt += 1
        elif not dummy:
            derived_errors.add(vec)
        nxt = targets[vec + 1] if vec + 1 < IDT_VECTORS else img['stubs_end']
        if nxt != at + len(want):
            ok_len = False
        call_at = at + len(want) - len(STUB_TAIL) - 5
        if d[call_at] != 0xE8:
            ok_call = False
            continue
        rel = struct.unpack_from('<i', d, call_at + 1)[0]
        if (call_at + 5 + rel) & 0xFFFFFFFFFFFFFFFF != dispatch & 0xFFFFFFFFFFFFFFFF:
            ok_call = False

    rep.check('stub.rebuilt_from_vector', ok_bytes,
              '%d stubs differ from the rebuilt bytes' % rebuilt)
    rep.check('stub.contiguous_variable_length', ok_len,
              'stubs must follow each other without gaps, lengths differ')
    rep.check('stub.call_targets_dispatch', ok_call)
    rep.check('stub.error_vector_set', derived_errors == set(ERROR_VECTORS),
              'derived %s, want %s' % (sorted(derived_errors), sorted(ERROR_VECTORS)))

    region = d[img['stubs_start']:img['stubs_end']]
    rep.equal('stub.iretq_count', region.count(bytes.fromhex('48 CF')), IDT_VECTORS)
    rep.equal('stub.no_iretd', region.count(bytes.fromhex('66 CF')), 0)
    dummies = sum(1 for vec, at in enumerate(targets)
                  if stub_dummy_flag(d[at:at + 8], vec) is True)
    rep.equal('stub.dummy_count', dummies, IDT_VECTORS - len(ERROR_VECTORS))

    # the frame offsets are a consequence of the push order, prove it directly:
    # after the two header pushes the 15 GPR pushes follow, then CR2
    rep.equal('stub.gpr_push_bytes', len(STUB_PUSH_GPR), 23)
    rep.check('stub.restore_is_reverse_of_save',
              reverse_gpr_block(STUB_PUSH_GPR) in STUB_TAIL,
              'pops must undo the pushes in reverse order')
    rep.equal('frame.gpr_follows_cr2', FRAME_GPR['r15'], FRAME_CR2 + 8)
    rep.equal('frame.gpr_block_bytes', FRAME_GPR['rax'] - FRAME_GPR['r15'] + 8, 15 * 8)
    rep.equal('frame.vector_after_gprs', FRAME_VECTOR, FRAME_GPR['rax'] + 8)
    rep.check('frame.cr2_after_gpr', FRAME_CR2 == 0 and FRAME_GPR['r15'] == 8)
    rep.check('frame.error_after_vector', FRAME_ERROR == FRAME_VECTOR + 8)
    rep.check('frame.rip_after_error', FRAME_RIP == FRAME_ERROR + 8)
    rep.check('frame.size_matches_hardware_slots',
              FRAME_SIZE == FRAME_SS + 8 and FRAME_SIZE % 8 == 0)
    rep.equal('frame.size_built_from_the_stub',
              STUB_HEADER_SIZE // 8 + 15 + 1 + (FRAME_SS + 8 - FRAME_RIP) // 8,
              FRAME_SIZE // 8)
    rep.check('frame.needs_alignment', FRAME_SIZE % 16 != 0,
              'a 184 byte frame lands RSP off 16, hence and rsp, not 15')
    rep.check('frame.drops_vector_and_error_before_iretq',
              STUB_TAIL.endswith(bytes.fromhex('48 83 C4 10 48 CF')),
              'add rsp, 16; iretq')

    # alignment and shadow space are visible in the stub body
    rep.check('stub.align_and_shadow_present',
              bytes.fromhex('48 83 E4 F0 48 83 EC 20') in d[targets[0]:targets[0] + 128])
    rep.check('stub.shadow_is_32',
              STUB_MID[8:11] == bytes.fromhex('48 83 EC') and
              STUB_MID[11] == SHADOW_SPACE,
              'sub rsp, %d after the alignment' % SHADOW_SPACE)


# where each function region ends; the stubs sit between cpu_set_rsp0 and
# cpu_trap_expect and are measured separately
REGION_END = {
    'cpu_gdt_init': 'cpu_set_rsp0',
    'cpu_set_rsp0': 'stubs_start',
    'cpu_trap_expect': 'cpu_trap_finish',
    'cpu_trap_finish': 'cpu_trap_dispatch',
    'cpu_trap_dispatch': 'cpu_irq_set_handler',
    'cpu_irq_set_handler': 'cpu_syscall_init',
    'cpu_syscall_init': 'cpu_syscall_set_handler',
    'cpu_syscall_set_handler': 'cpu_user_fault_set_handler',
    'cpu_user_fault_set_handler': 'cpu_syscall_entry',
    'cpu_syscall_entry': 'cpu_com1_init',
    'cpu_com1_init': 'cpu_com1_putc',
    'cpu_com1_putc': 'cpu_com1_puts',
    'cpu_com1_puts': 'cpu_com1_puthex',
    'cpu_com1_puthex': 'cpu_halt_forever',
    'cpu_halt_forever': 'cpu_panic',
    'cpu_panic': 'cpu_selftest',
    'cpu_selftest': 'cpu_init',
    'cpu_init': 'cpu_idt_init',
    'cpu_idt_init': 'code_end',
}

# the functions that end in the halt loop instead of returning
TAIL = {'cpu_panic': 'halt', 'cpu_trap_dispatch': 'halt'}

# the two functions that must not build the uniform frame: one is four bytes
# long, the other switches to its own stack before it touches memory
NO_FRAME = ('cpu_halt_forever', 'cpu_panic')


def call_sites(img, at, end):
    """Every E8 in a region whose target lands inside this module."""
    lo, hi = img['code_start'], img['code_end']
    out = []
    for p in find_all(img.data[at:end], b'\xE8'):
        rel = struct.unpack_from('<i', img.data, at + p + 1)[0]
        target = (at + p + 5 + rel) & 0xFFFFFFFFFFFFFFFF
        if lo <= target < hi:
            out.append((at + p, target))
    return out


def region_tail(img, at, end):
    """How a function leaves: ('ret'|'halt', padding) or (None, None)."""
    body = img.data[at:end]
    for pad in range(0, 16):
        sub = body[:len(body) - pad] if pad else body
        if not sub or body[len(sub):].strip(b'\x00'):
            break
        if sub.endswith(EPILOGUE):
            return 'ret', pad
        if len(sub) >= 5 and sub[-5] == 0xE9:
            rel = struct.unpack_from('<i', sub, len(sub) - 4)[0]
            if (at + len(sub) + rel) & 0xFFFFFFFFFFFFFFFF == img['cpu_halt_forever']:
                return 'halt', pad
    return None, None


def check_functions(rep, img):
    d = img.data
    order = [(name, img.at(name)) for name in FUNCTIONS]
    rep.check('function.order',
              all(order[i][1] < order[i + 1][1] for i in range(len(order) - 1)),
              ' '.join('%s=%#x' % (n, a) for n, a in order))

    for name, at in order:
        end = img.at(REGION_END[name])
        body = d[at:end]
        rep.check('%s.non_empty' % name, len(body) > 0, '%d bytes' % len(body))
        if name == 'cpu_halt_forever':
            rep.check('cpu_halt_forever.masked_halt_loop', body == HALT_BODY,
                      body.hex(' '))
            continue
        if name == 'cpu_syscall_entry':
            # the LSTAR target is never called: it switches to TSS.RSP0
            # before it touches memory and leaves through SYSRETQ, so it
            # has no caller frame; check_syscall_api pins it byte by byte
            rep.check('cpu_syscall_entry.builds_no_frame',
                      not body.startswith(PROLOGUE),
                      'the entry must not assume a caller frame')
            continue
        kind, pad = region_tail(img, at, end)
        want = TAIL.get(name, 'ret')
        rep.check('%s.leaves_by_%s' % (name, want), kind == want,
                  '%s after %d padding bytes' % (kind, pad or 0))
        if name in NO_FRAME:
            rep.check('%s.builds_no_frame' % name, not body.startswith(PROLOGUE),
                      'this function must not assume a caller frame')
        else:
            rep.check('%s.uniform_prologue' % name, body.startswith(PROLOGUE),
                      body[:11].hex(' '))
            rep.equal('%s.frame_size' % name,
                      struct.unpack_from('<I', body, 7)[0], STACK_FRAME)
            rep.equal('%s.frame_keeps_rsp_aligned' % name, STACK_FRAME % 16, 0)
            rep.check('%s.shadow_space' % name, STACK_FRAME >= SHADOW_SPACE,
                      '%d >= %d' % (STACK_FRAME, SHADOW_SPACE))
            rep.equal('%s.prologue_once' % name, body.count(PROLOGUE), 1)

    # the panic path: its own stack, a recursion guard, and the halt loop
    panic_at, panic_end = img.at('cpu_panic'), img.at('cpu_selftest')
    panic = d[panic_at:panic_end]
    rep.check('cpu_panic.masks_interrupts', panic[0:1] == b'\xFA', 'cli first')
    stack_ok = False
    own = panic.find(b'\x48\xC7\xC4')
    if own >= 0:
        stack_ok = u32(panic, own + 3) == (img['panic_stack'] + IST_SIZE) & 0xFFFFFFFF
    wide = panic.find(b'\x48\xBC')
    if wide >= 0:
        stack_ok = u64(panic, wide + 2) == img['panic_stack'] + IST_SIZE
    rep.check('cpu_panic.switches_to_own_stack', stack_ok,
              'rsp <- %#x' % (img['panic_stack'] + IST_SIZE))
    rep.check('cpu_panic.aligns_and_reserves',
              bytes.fromhex('48 83 E4 F0 48 83 EC 20') in panic,
              'and rsp, not 15; sub rsp, 32')
    rep.check('cpu_panic.recursion_guard', bytes.fromhex('80 3D') in panic,
              'cmp byte [rip+..], 0')
    targets = [target for _, target in call_sites(img, panic_at, panic_end)]
    rep.check('cpu_panic.reports_over_com1',
              targets.count(img['cpu_com1_puts']) >= 2 and
              targets.count(img['cpu_com1_puthex']) >= 1,
              '%d puts, %d puthex' % (targets.count(img['cpu_com1_puts']),
                                      targets.count(img['cpu_com1_puthex'])))

    # the GDT window: LGDT, far return, then LTR
    gdt_body = d[img.at('cpu_gdt_init'):img.at('cpu_set_rsp0')]
    lgdt_at = find_one(gdt_body, bytes.fromhex('0F 01 15'))
    retfq_at = find_one(gdt_body, bytes.fromhex('48 CB'))
    ltr_at = find_one(gdt_body, bytes.fromhex('0F 00 D8'))
    rep.check('gdt_init.lgdt_present', lgdt_at >= 0)
    rep.check('gdt_init.retfq_reloads_cs', retfq_at > lgdt_at,
              'retfq +%d after lgdt +%d' % (retfq_at, lgdt_at))
    rep.check('gdt_init.far_return_reload',
              bytes.fromhex('6A 08 50 48 CB') in gdt_body,
              'push selector 0x08, push target, retfq')
    rep.check('gdt_init.ltr_after_retfq', ltr_at > retfq_at, 'ltr at +%d' % ltr_at)
    for name, needle in (('clears_fs_gs', bytes.fromhex('8E E0 8E E8')),
                         ('clears_ldtr', bytes.fromhex('0F 00 D0')),
                         ('loads_tr', bytes.fromhex('0F 00 D8'))):
        rep.check('gdt_init.%s' % name, needle in gdt_body, needle.hex(' '))
    ds_at = find_one(gdt_body, bytes.fromhex('8E D8 8E C0 8E D0'))
    fg_at = find_one(gdt_body, bytes.fromhex('31 C0 8E E0 8E E8'))
    lldt_at = find_one(gdt_body, bytes.fromhex('0F 00 D0'))
    rep.check('gdt_init.reload_order',
              -1 < lgdt_at < retfq_at < ds_at < fg_at < lldt_at < ltr_at,
              'lgdt %d, retfq %d, ds/es/ss %d, fs/gs %d, lldt %d, ltr %d'
              % (lgdt_at, retfq_at, ds_at, fg_at, lldt_at, ltr_at))

    # the IDT loader: every gate is checked before LIDT
    idt_body = d[img.at('cpu_idt_init'):img.at('code_end')]
    lidt_at = find_one(idt_body, bytes.fromhex('0F 01 1D'))
    rep.check('idt_init.lidt_present', lidt_at >= 0)
    rep.check('idt_init.bounded_gate_loop',
              bytes.fromhex('49 81 F8 00 01 00 00') in idt_body, 'cmp r8, 0x100')
    rep.check('idt_init.checks_selector',
              bytes.fromhex('0F B7 4A 02 83 F9 08') in idt_body,
              'cmp [gate+2], selector 0x08')
    rep.check('idt_init.checks_type', bytes.fromhex('81 F9 8E 00 00 00') in idt_body,
              'cmp [gate+5], 0x8e')
    rep.check('idt_init.checks_ist', bytes.fromhex('83 F9 07') in idt_body, 'ist <= 7')
    lo = b'\x48\x81\xF9' + struct.pack('<I', img['stubs_start'])
    hi = b'\x48\x81\xF9' + struct.pack('<I', img['stubs_end'])
    rep.check('idt_init.checks_target_range', lo in idt_body and hi in idt_body,
              'targets inside stubs_start..stubs_end')
    rep.check('idt_init.lidt_after_the_loop',
              lidt_at > idt_body.find(bytes.fromhex('49 81 F8')) > 0)

    # cpu_init: no console between the GDT and the IDT
    init_at, init_end = img.at('cpu_init'), img.at('cpu_idt_init')
    init_body = d[init_at:init_end]
    rep.check('cpu_init.masks_master_pic',
              bytes.fromhex('B0 FF E6 21') in init_body, 'mov al, 0xff; out 0x21')
    rep.check('cpu_init.masks_slave_pic', bytes.fromhex('E6 A1') in init_body,
              'out 0xa1')
    rep.check('cpu_init.checks_cpl', bytes.fromhex('66 8C C8 A8 03') in init_body,
              'mov ax, cs; test al, 3')
    rep.check('cpu_init.checks_if', bytes.fromhex('A9 00 02 00 00') in init_body,
              'test eax, RFLAGS.IF')
    rep.check('cpu_init.reads_efer',
              bytes.fromhex('B9 80 00 00 C0 0F 32') in init_body, 'rdmsr IA32_EFER')
    rep.check('cpu_init.requires_long_mode',
              bytes.fromhex('A9 00 04 00 00') in init_body, 'EFER.LMA')
    rep.check('cpu_init.reads_cr4', bytes.fromhex('0F 20 E0') in init_body,
              'mov rax, cr4')
    rep.check('cpu_init.requires_pae', bytes.fromhex('A9 20 00 00 00') in init_body,
              'CR4.PAE')
    rep.check('cpu_init.rejects_cet', bytes.fromhex('A9 00 00 80 00') in init_body,
              'CR4.CET must be clear')
    rep.check('cpu_init.reads_cr0', bytes.fromhex('0F 20 C0') in init_body,
              'mov rax, cr0')
    rep.check('cpu_init.requires_paging', bytes.fromhex('A9 00 00 00 80') in init_body,
              'CR0.PG')
    rep.check('cpu_init.rejects_again', bytes.fromhex('80 3D') in init_body,
              'cmp byte [rip+..], 0')

    sites = call_sites(img, init_at, init_end)
    def site(label):
        for offset, target in sites:
            if target == img[label]:
                return offset
        return -1
    gdt_call = site('cpu_gdt_init')
    rsp0_call = site('cpu_set_rsp0')
    idt_call = site('cpu_idt_init')
    syscall_call = site('cpu_syscall_init')
    rep.check('cpu_init.calls_gdt_rsp0_idt', -1 < gdt_call < rsp0_call < idt_call,
              'gdt %d, rsp0 %d, idt %d' % (gdt_call, rsp0_call, idt_call))
    rep.check('cpu_init.calls_syscall_after_idt', syscall_call > idt_call,
              'the MSRs need the GDT, RSP0 and the IDT alive first')
    setup = {img['cpu_gdt_init'], img['cpu_set_rsp0'], img['cpu_idt_init'],
             img['cpu_syscall_init']}
    rep.check('cpu_init.calls_nothing_else',
              all(target in setup for _, target in sites),
              '%d call sites' % len(sites))
    window = d[gdt_call:idt_call] if gdt_call >= 0 and idt_call > gdt_call else b''
    rep.check('init.silent_between_gdt_and_idt',
              not any(pattern in window for pattern in COM1_PATTERNS),
              '%d bytes, no UART writes' % len(window))


def check_uart(rep, img):
    d = img.data
    putc = d[img['cpu_com1_putc']:img['cpu_com1_puts']]
    spin = find_one(putc, b'\xB9')
    rep.check('uart.putc_spin_budget', spin >= 0, 'mov ecx, budget')
    rep.check('uart.putc_polls_thre', bytes.fromhex('A8 20') in putc,
              'test al, LSR.THRE')
    rep.check('uart.putc_bounded_loop', bytes.fromhex('FF C9 75 F3') in putc,
              'dec ecx; jnz back to the poll')
    rep.check('uart.putc_zero_on_timeout',
              bytes.fromhex('B8 F7 FF FF FF 31 D2') in putc, 'EIO with rdx = 0')
    rep.check('uart.putc_one_byte_on_success',
              bytes.fromhex('BA 01 00 00 00') in putc, 'rdx = 1')
    rep.check('uart.putc_returns_status', putc.endswith(EPILOGUE))

    puts_at, puts_end = img.at('cpu_com1_puts'), img.at('cpu_com1_puthex')
    puts = d[puts_at:puts_end]
    rep.check('uart.puts_cap_check', bytes.fromhex('48 83 7D F0 00') in puts,
              'cmp qword [rbp-16], 0 when the budget runs out')
    rep.check('uart.puts_reports_overflow', bytes.fromhex('B8 F6 FF FF FF') in puts,
              'EOVERFLOW once the cap is reached')
    rep.check('uart.puts_bounded_call',
              [t for _, t in call_sites(img, puts_at, puts_end)].count(
                  img['cpu_com1_putc']) == 1,
              'one bounded putc per byte')

    puthex_at, puthex_end = img.at('cpu_com1_puthex'), img.at('cpu_halt_forever')
    puthex = d[puthex_at:puthex_end]
    rep.check('uart.puthex_digit_cap', bytes.fromhex('48 83 FA 10') in puthex,
              'cmp rdx, 16')
    rep.check('uart.puthex_rejects_zero', bytes.fromhex('48 85 D2 0F 84') in puthex,
              'zero digits is EINVAL')
    rep.check('uart.puthex_prefix',
              bytes.fromhex('48 C7 C1 30 00 00 00') in puthex and
              bytes.fromhex('48 C7 C1 78 00 00 00') in puthex,
              "'0' then 'x'")
    rep.check('uart.puthex_digit_loop', bytes.fromhex('48 83 6D E0 04 79') in puthex,
              'the shift count walks down 4 bits at a time')
    rep.check('uart.puthex_bounded_call',
              [t for _, t in call_sites(img, puthex_at, puthex_end)].count(
                  img['cpu_com1_putc']) >= 2,
              'prefix and digits go through putc')
    for name in ('cpu_com1_init', 'cpu_com1_putc', 'cpu_com1_puts', 'cpu_com1_puthex'):
        body = d[img.at(name):img.at(name) + 8]
        rep.check('%s.uniform_entry' % name, body.startswith(PROLOGUE[:6]),
                  body.hex(' '))


def check_trap_api(rep, img):
    """The three trap calls, checked against the contract, not the source.

    Which state variable a function reads is decided by resolving the RIP
    relative displacement of every operand back to a manifest label, so the
    checks survive register allocation and instruction selection but not a
    wrong variable.
    """
    d = img.data
    expect_at, expect_end = img.at('cpu_trap_expect'), img.at('cpu_trap_finish')
    finish_at, finish_end = img.at('cpu_trap_finish'), img.at('cpu_trap_dispatch')
    dispatch_at, dispatch_end = img.at('cpu_trap_dispatch'), img.at('cpu_irq_set_handler')
    expect = d[expect_at:expect_end]
    finish = d[finish_at:finish_end]
    dispatch = d[dispatch_at:dispatch_end]

    names = {img.at(name): name for name in TRAP_STATE}

    def label(target):
        return names.get(target, '%#x' % target)

    by_name = {name: img.at(name) for name in TRAP_STATE}

    # ---------------- cpu_trap_expect: what may be armed --------------
    for vec in VECTOR_ALLOWED:
        needle = (b'\x48\x81\xF9' + struct.pack('<I', vec)) if vec > 0x7F \
            else b'\x48\x83\xF9' + bytes((vec,))
        rep.check('trap_expect.accepts_%#x' % vec, needle in expect, needle.hex(' '))
    compared = set()
    for p in find_all(expect, b'\x48\x83\xF9'):
        compared.add(expect[p + 3])
    for p in find_all(expect, b'\x48\x81\xF9'):
        compared.add(struct.unpack_from('<I', expect, p + 3)[0])
    rep.check('trap_expect.compares_only_the_six', compared == set(VECTOR_ALLOWED),
              'compared %s' % sorted(compared))

    # ---------------- both addresses must be canonical -----------------
    rep.equal('trap_expect.canonical_check_on_both_rips',
              len(find_all(expect, bytes.fromhex('48 C1 E8 2F'))), 2)
    rep.equal('trap_expect.canonical_limit_is_the_kernel_top',
              len(find_all(expect, bytes.fromhex('48 3D') +
                           struct.pack('<I', KERNEL_TOP))), 2)

    # ---------------- one armed trap and one pending result -----------
    guard = [t for t, _ in rip_targets(img, expect_at, expect_end, b'\x80\x3D', 1)]
    rep.check('trap_expect.busy_on_armed', guard.count(by_name['trap_armed']) == 1,
              'cmp byte [%s], 0' % ' + '.join(label(t) for t in guard))
    rep.check('trap_expect.busy_on_unconsumed_result',
              guard.count(by_name['trap_result_ready']) == 1,
              'cmp byte [%s], 0' % ' + '.join(label(t) for t in guard))
    rep.check('trap_expect.reports_ebusy', mov_eax(EBUSY) in expect, 'EAX = EBUSY')
    rep.check('trap_expect.reports_einval', mov_eax(EINVAL) in expect, 'EAX = EINVAL')

    # ---------------- the expectation is copied, not referenced -------
    for off, name in ((EXPECT_ERROR_MASK, 'mask'), (EXPECT_ERROR_VALUE, 'value'),
                      (EXPECT_CR2_MASK, 'mask'), (EXPECT_CR2_VALUE, 'value')):
        needle = b'\x49\x8B\x41' + bytes((off,)) if off else b'\x49\x8B\x01'
        rep.check('trap_expect.reads_the_expect_block_%s%d' % (name, off),
                  needle in expect, 'mov rax, [r9+%d]' % off)
    zeroed = [t for t, _ in rip_targets(img, expect_at, expect_end, b'\x48\xC7\x05', 4)]
    rep.check('trap_expect.defaults_accept_any_hardware_value',
              sorted(zeroed) == sorted(by_name[name] for name in
                                       ('trap_err_mask', 'trap_err_value',
                                        'trap_cr2_mask', 'trap_cr2_value')),
              ' '.join(label(t) for t in zeroed))
    copied = [t for t, _ in rip_targets(img, expect_at, expect_end, b'\x48\x89\x05')]
    rep.check('trap_expect.stores_the_whole_expect_block',
              sorted(copied) == sorted(by_name[name] for name in
                                       ('trap_err_mask', 'trap_err_value',
                                        'trap_cr2_mask', 'trap_cr2_value')),
              ' '.join(label(t) for t in copied))
    rep.check('trap_expect.stores_the_saved_rip',
              [t for t, _ in rip_targets(img, expect_at, expect_end, b'\x48\x89\x15')] ==
              [by_name['trap_expect_rip']], 'mov [rip+..], rdx')
    rep.check('trap_expect.stores_the_resume_rip',
              [t for t, _ in rip_targets(img, expect_at, expect_end, b'\x4C\x89\x05')] ==
              [by_name['trap_resume_rip']], 'mov [rip+..], r8')
    rep.check('trap_expect.stores_the_vector',
              [t for t, _ in rip_targets(img, expect_at, expect_end, b'\x88\x0D')] ==
              [by_name['trap_vector']], 'mov [rip+..], cl')
    armed_writes = [t for t, _ in rip_targets(img, expect_at, expect_end, b'\xC6\x05', 1)]
    rep.check('trap_expect.arms_once', armed_writes == [by_name['trap_armed']],
              ' '.join(label(t) for t in armed_writes))
    touched = {n for n in rip_state_refs(img, expect_at, expect_end, TRAP_STATE)}
    rep.check('trap_expect.touches_only_the_armed_state',
              touched == {'trap_armed', 'trap_result_ready', 'trap_vector',
                          'trap_expect_rip', 'trap_resume_rip', 'trap_err_mask',
                          'trap_err_value', 'trap_cr2_mask', 'trap_cr2_value'},
              ' '.join(sorted(touched)))

    # ---------------- cpu_trap_finish: one result, consumed once -------
    touched = {n for n in rip_state_refs(img, finish_at, finish_end, TRAP_STATE)}
    rep.check('trap_finish.touches_only_the_result_state',
              touched == {'trap_result_ready', 'trap_armed',
                          'trap_handler_frame', 'trap_result'},
              ' '.join(sorted(touched)))
    rep.check('trap_finish.copies_from_the_internal_result',
              [t for t, _ in rip_targets(img, finish_at, finish_end, b'\x48\x8D\x35')] ==
              [by_name['trap_result']], 'lea rsi, [rip+cpu_trap_frame_result]')
    rep.check('trap_finish.copies_one_full_frame',
              bytes.fromhex('B9 17 00 00 00 FC F3 48 A5') in finish,
              'mov ecx, %d; cld; rep movsq' % (FRAME_SIZE // 8))
    rep.check('trap_finish.reports_the_handler_frame',
              [t for t, _ in rip_targets(img, finish_at, finish_end, b'\x48\x8B\x15')] ==
              [by_name['trap_handler_frame']], 'mov rdx, [rip+..]')
    cleared = [t for t, _ in rip_targets(img, finish_at, finish_end, b'\xC6\x05', 1)]
    rep.check('trap_finish.clears_the_result_and_the_arming',
              sorted(cleared) == sorted([by_name['trap_result_ready'],
                                         by_name['trap_armed']]),
              ' '.join(label(t) for t in cleared))
    rep.check('trap_finish.reports_efault', mov_eax(EFAULT) in finish,
              'an armed trap that never fired is EFAULT')
    rep.check('trap_finish.reports_enotready', mov_eax(ENOTREADY) in finish,
              'no arming and no result is ENOTREADY')
    rep.equal('trap_finish.single_exit', finish.count(EPILOGUE), 1)

    # ---------------- cpu_trap_dispatch: four independent matches ------
    rep.check('dispatch.checks_cpl',
              b'\x48\x8B\x81' + struct.pack('<I', FRAME_CS) in dispatch,
              'reads frame.cs')
    rep.check('dispatch.reads_the_frame_cr2', bytes.fromhex('48 8B 01') in dispatch,
              'reads frame.cr2 at offset 0')
    rep.check('dispatch.matches_vector_and_saved_rip',
              [t for t, _ in rip_targets(img, dispatch_at, dispatch_end, b'\x48\x3B\x05')] ==
              [by_name['trap_expect_rip'], by_name['trap_err_value'],
               by_name['trap_cr2_value']],
              'cmp rax with the expected RIP, the masked error, the masked CR2')

    and_sites = find_all(dispatch, bytes.fromhex('48 21 D0'))
    rep.equal('dispatch.and_sites', len(and_sites), 2)
    pairs = []
    for p in and_sites:
        before = struct.unpack_from('<i', dispatch, p - 4)[0]
        after = struct.unpack_from('<i', dispatch, p + 6)[0]
        mask = dispatch[p - 7:p - 4] == bytes.fromhex('48 8B 15')
        value = dispatch[p + 3:p + 6] == bytes.fromhex('48 3B 05')
        pairs.append((mask, value, label(dispatch_at + p + before),
                      label(dispatch_at + p + 10 + after)))
    rep.check('dispatch.masks_before_comparing',
              pairs == [(True, True, 'trap_err_mask', 'trap_err_value'),
                        (True, True, 'trap_cr2_mask', 'trap_cr2_value')],
              ' '.join('%s & %s' % (m, v) for _, _, m, v in pairs))

    rep.check('dispatch.copies_one_full_frame',
              bytes.fromhex('B9 17 00 00 00 FC F3 48 A5') in dispatch,
              'mov ecx, %d; cld; rep movsq' % (FRAME_SIZE // 8))
    rep.check('dispatch.copies_to_the_internal_result',
              [t for t, _ in rip_targets(img, dispatch_at, dispatch_end, b'\x48\x8D\x3D')] ==
              [by_name['trap_result']], 'lea rdi, [rip+cpu_trap_frame_result]')

    handler = [p for t, p in rip_targets(img, dispatch_at, dispatch_end, b'\x48\x89\x0D')
               if t == by_name['trap_handler_frame']]
    rep.check('dispatch.remembers_the_handler_frame', len(handler) == 1,
              'mov [rip+cpu_trap_handler_frame], rcx')

    store = find_all(dispatch, b'\x48\x89\x81')
    rep.check('dispatch.rewrites_the_frame_rip',
              len(store) == 1 and u32(dispatch, store[0] + 3) == FRAME_RIP,
              'mov [rcx+%d], rax' % FRAME_RIP)
    disp32_stores = [p for p in find_all(dispatch, b'\x48\x89')
                     if p + 2 < len(dispatch) and (dispatch[p + 2] & 0xC0) == 0x80]
    rep.equal('dispatch.writes_nothing_else_in_the_frame', len(disp32_stores), 1)

    flags = [(label(t), d[p + 6]) for t, p in
             rip_targets(img, dispatch_at, dispatch_end, b'\xC6\x05', 1)]
    rep.check('dispatch.consumes_the_arming_and_marks_the_result',
              flags == [('trap_armed', 0), ('trap_result_ready', 1)],
              ' '.join('%s<-%d' % f for f in flags))

    copy_at = dispatch_at + find_one(dispatch, bytes.fromhex('F3 48 A5'))
    rip_at = dispatch_at + store[0] if store else -1
    rep.check('dispatch.saves_before_copying_before_rewriting',
              handler and handler[0] < copy_at and store and rip_at > copy_at,
              'handler %s, copy %s, rip write %s'
              % (handler[0] if handler else -1, copy_at, rip_at))

    touched = {n for n in rip_state_refs(img, dispatch_at, dispatch_end,
                                         TRAP_STATE + ('irq_handler',
                                                       'user_fault_handler'))}
    rep.check('dispatch.touches_only_the_declared_state',
              touched == set(TRAP_STATE) | {'irq_handler', 'user_fault_handler'},
              ' '.join(sorted(touched)))

    targets = [t for _, t in call_sites(img, dispatch_at, dispatch_end)]
    rep.equal('dispatch.panic_sites', targets.count(img['cpu_panic']), 2)
    rep.equal('dispatch.single_accepting_exit', dispatch.count(EPILOGUE), 1)


def check_irq_api(rep, img):
    """The asynchronous IRQ hook: registration contract and dispatch branch.

    The expect path of the dispatcher is locked byte by byte in
    check_trap_api; here the asynchronous branch is proven to exist, to sit
    behind the expect path, to bound the vector, to consult the registered
    handler and to call it with the frame under the same uniform frame that
    provides the shadow space. No EOI may appear in the dispatcher.
    """
    d = img.data
    at, end = img.at('cpu_irq_set_handler'), img.at('cpu_syscall_init')
    body = d[at:end]
    handler = img.at('irq_handler')

    # ---------------- cpu_irq_set_handler: the registration contract ----
    rep.check('irq_set_handler.checks_cpl',
              bytes.fromhex('66 8C C8 A8 03') in body, 'mov ax, cs; test al, 3')
    rep.check('irq_set_handler.checks_if',
              bytes.fromhex('9C 58 A9 00 02 00 00') in body,
              'pushfq; pop rax; test eax, RFLAGS.IF')
    rep.check('irq_set_handler.reports_eperm', mov_eax(EPERM) in body,
              'a call with IF set or from ring 3 is EPERM')
    rep.check('irq_set_handler.reports_einval', mov_eax(EINVAL) in body)
    rep.check('irq_set_handler.reports_ealready', mov_eax(EALREADY) in body)
    rep.equal('irq_set_handler.canonical_check_once',
              len(find_all(body, bytes.fromhex('48 C1 E8 2F'))), 1)
    rep.equal('irq_set_handler.canonical_limit_is_the_kernel_top',
              len(find_all(body, bytes.fromhex('48 3D') +
                           struct.pack('<I', KERNEL_TOP))), 1)
    rep.check('irq_set_handler.zero_selects_unregister',
              bytes.fromhex('48 85 C9') in body, 'test rcx, rcx')
    stored = [t for t, _ in rip_targets(img, at, end, b'\x48\x89\x0D')]
    rep.check('irq_set_handler.stores_the_handler', stored == [handler],
              'mov [rip+cpu_irq_handler], rcx')
    cleared = [(t, p) for t, p in rip_targets(img, at, end, b'\x48\xC7\x05', 4)
               if t == handler]
    rep.check('irq_set_handler.clears_with_zero',
              len(cleared) == 1 and
              struct.unpack_from('<i', d, cleared[0][1] + 7)[0] == 0,
              'mov qword [rip+cpu_irq_handler], 0')
    guard = [t for t, _ in rip_targets(img, at, end, b'\x48\x83\x3D', 1)]
    rep.check('irq_set_handler.ealready_guard', guard == [handler],
              'cmp qword [rip+cpu_irq_handler], 0')
    touched = {n for n in rip_state_refs(img, at, end,
                                         TRAP_STATE + ('irq_handler',))}
    rep.check('irq_set_handler.touches_only_the_handler_slot',
              touched == {'irq_handler'}, ' '.join(sorted(touched)))
    rep.check('irq_set_handler.success_zeroes_rdx',
              bytes.fromhex('31 C0 31 D2') in body, 'xor eax, eax; xor edx, edx')
    rep.check('irq_set_handler.slot_starts_empty', u64(d, handler) == 0,
              'no handler until one is registered')
    rep.check('irq_set_handler.slot_inside_data',
              img['data_start'] <= img['irq_handler'] < img['data_end'])

    # ---------------- the asynchronous dispatch branch ------------------
    dispatch_at, dispatch_end = img.at('cpu_trap_dispatch'), at
    dispatch = d[dispatch_at:dispatch_end]
    rep.equal('manifest.irq_vector_min', img['irq_vector_min'], IRQ_VECTOR_MIN)
    floor = b'\x48\x83\xFA' + bytes((IRQ_VECTOR_MIN,))
    floor_p = find_one(dispatch, floor)
    rep.check('dispatch.async_vector_floor', floor_p >= 0,
              'cmp rdx, %#x' % IRQ_VECTOR_MIN)
    rep.check('dispatch.async_vector_ceiling',
              (b'\x48\x81\xFA' + struct.pack('<I', IDT_VECTORS)) in dispatch,
              'cmp rdx, %d' % IDT_VECTORS)
    loads = [t for t, _ in rip_targets(img, dispatch_at, dispatch_end,
                                       b'\x48\x8B\x05')]
    rep.check('dispatch.async_loads_the_handler',
              loads == [img.at('trap_resume_rip'), handler,
                        img.at('user_fault_handler')],
              'the resume RIP, the IRQ handler, then the user fault hook')
    rep.check('dispatch.async_null_check',
              bytes.fromhex('48 85 C0') in dispatch,
              'test rax, rax before the call')
    iload = [p - dispatch_at for t, p in
             rip_targets(img, dispatch_at, dispatch_end, b'\x48\x8B\x05')
             if t == handler]
    rep.check('dispatch.async_null_guard',
              len(iload) == 1 and
              dispatch[iload[0] + 7:iload[0] + 10] == bytes.fromhex('48 85 C0') and
              dispatch[iload[0] + 10] == 0x74,
              'test rax, rax; jz to the panic right after the IRQ handler load')
    calls = find_all(dispatch, b'\xFF\xD0')
    rep.equal('dispatch.async_and_user_indirect_calls', len(calls), 2)
    if calls:
        call_p = calls[0]
        rep.check('dispatch.async_passes_the_frame',
                  dispatch[call_p - 4:call_p] == bytes.fromhex('48 8B 4D F8'),
                  'mov rcx, [rbp-8] right before call rax')
        # the uniform frame is never readjusted: RSP stays 16 byte aligned
        # and the low 32 bytes of the frame are the callee's shadow space
        rep.check('dispatch.async_call_keeps_the_uniform_frame',
                  dispatch.count(bytes.fromhex('48 81 EC')) == 1 and
                  bytes.fromhex('48 83 EC') not in dispatch and
                  bytes.fromhex('48 83 C4') not in dispatch,
                  'only the prologue touches RSP, shadow space intact')
        # the expect path comes first: the arming guard and both flag stores
        # precede the vector floor, the indirect call sits behind the floor
        guard_p = find_one(dispatch, b'\x80\x3D')
        flags_p = [p - dispatch_at for _, p in
                   rip_targets(img, dispatch_at, dispatch_end, b'\xC6\x05', 1)]
        rep.check('dispatch.async_behind_the_expect_path',
                  -1 < guard_p < floor_p and
                  all(p < floor_p for p in flags_p) and floor_p < call_p,
                  'guard %d, flags %s, floor %d, call %d'
                  % (guard_p, flags_p, floor_p, call_p))
        # the callback returns into the one shared accepting exit: a jmp to
        # the xor eax, eax that sits right before the single leave/ret
        exit_p = find_one(dispatch, EPILOGUE)
        jump_ok = False
        if dispatch[call_p + 2] == 0xEB:
            rel = struct.unpack_from('<b', dispatch, call_p + 3)[0]
            jump_ok = call_p + 4 + rel == exit_p - 2
        elif dispatch[call_p + 2] == 0xE9:
            rel = struct.unpack_from('<i', dispatch, call_p + 3)[0]
            jump_ok = call_p + 7 + rel == exit_p - 2
        rep.check('dispatch.async_joins_the_shared_exit', jump_ok,
                  'jmp .return_ok, one leave/ret for both paths')

    # ---------------- the ring 3 fault branch, APP-v1 --------------------
    # the user hook load sits behind the IRQ branch, a null guard falls
    # back to the pre APP-v1 panic, and the callback joins the same exit
    uload = [p - dispatch_at for t, p in rip_targets(img, dispatch_at, dispatch_end,
                                                     b'\x48\x8B\x05')
             if t == img.at('user_fault_handler')]
    rep.equal('dispatch.user_hook_load_once', len(uload), 1)
    if uload and len(calls) == 2:
        up = uload[0]
        rep.check('dispatch.user_null_guard',
                  dispatch[up + 7:up + 10] == bytes.fromhex('48 85 C0') and
                  dispatch[up + 10] == 0x74,
                  'test rax, rax; jz to the fallback right after the load')
        user_call = calls[1]
        rep.check('dispatch.user_branch_behind_the_irq_branch',
                  calls[0] < up < user_call,
                  'irq call %d, user load %d, user call %d'
                  % (calls[0], up, user_call))
        rep.check('dispatch.user_passes_the_frame',
                  dispatch[user_call - 4:user_call] == bytes.fromhex('48 8B 4D F8'),
                  'mov rcx, [rbp-8] right before call rax')
        exit_p = find_one(dispatch, EPILOGUE)
        jump_ok = False
        if dispatch[user_call + 2] == 0xEB:
            rel = struct.unpack_from('<b', dispatch, user_call + 3)[0]
            jump_ok = user_call + 4 + rel == exit_p - 2
        elif dispatch[user_call + 2] == 0xE9:
            rel = struct.unpack_from('<i', dispatch, user_call + 3)[0]
            jump_ok = user_call + 7 + rel == exit_p - 2
        rep.check('dispatch.user_joins_the_shared_exit', jump_ok,
                  'jmp .return_ok, one leave/ret for every path')
        if dispatch[up + 10] == 0x74:
            rel = struct.unpack_from('<b', dispatch, up + 11)[0]
            target = up + 12 + rel
            block = dispatch[target:target + 24]
            cp = block.find(b'\xE8')
            rep.check('dispatch.user_null_guard_panics',
                      target > user_call and cp >= 0 and
                      dispatch_at + target + cp + 5 +
                      struct.unpack_from('<i', block, cp + 1)[0] ==
                      img['cpu_panic'],
                      'the fallback behind the callback still calls cpu_panic')
    rep.check('dispatch.no_eoi_writes',
              b'\xE6\x21' not in dispatch and b'\xE6\xA1' not in dispatch,
              'the EOI belongs to the IRQ layer, not to the dispatcher')


def check_syscall_api(rep, img):
    """APP-v1: the MSR setup, both registrations and the thin ring 3 entry.

    The entry is pinned byte by byte where it matters: the stack switch,
    the frame head, the number dispatch and the SYSRET tail. The MSR
    writes are pinned by their immediates, so a drifted STAR or SFMASK
    value fails here even when the assembly time assertions are bypassed.
    """
    d = img.data

    # ---------------- cpu_syscall_init: four MSRs, all read back --------
    at, end = img.at('cpu_syscall_init'), img.at('cpu_syscall_set_handler')
    body = d[at:end]
    rep.check('syscall_init.checks_cpl',
              bytes.fromhex('66 8C C8 A8 03') in body, 'mov ax, cs; test al, 3')
    rep.check('syscall_init.checks_if',
              bytes.fromhex('9C 58 A9 00 02 00 00') in body,
              'pushfq; pop rax; test eax, RFLAGS.IF')
    for name, code in (('eperm', EPERM), ('ealready', EALREADY), ('eio', EIO)):
        rep.check('syscall_init.reports_%s' % name, mov_eax(code) in body)
    rep.equal('syscall_init.five_rdmsr', body.count(bytes.fromhex('0F 32')), 5)
    rep.equal('syscall_init.four_wrmsr', body.count(bytes.fromhex('0F 30')), 4)
    rep.equal('syscall_init.full_width_read_back',
              body.count(bytes.fromhex('48 C1 E2 20')), 5)
    rep.check('syscall_init.efer_read_modify_write',
              bytes.fromhex('48 83 C8 01') in body and
              bytes.fromhex('48 3B 45 F8') in body,
              'or rax, SCE; cmp rax, [rbp-8] preserves every other EFER bit')
    star_hi = (STAR_VALUE >> 32) & 0xFFFFFFFF
    rep.check('syscall_init.writes_the_star_pair',
              (b'\xBA' + struct.pack('<I', star_hi)) in body,
              'mov edx, STAR[63:32]')
    rep.check('syscall_init.compares_the_whole_star',
              (bytes.fromhex('48 B9') + struct.pack('<Q', STAR_VALUE)) in body and
              bytes.fromhex('48 39 C8') in body,
              'mov rcx, STAR; cmp rax, rcx')
    rep.check('syscall_init.lstar_points_at_the_entry',
              [t for t, _ in rip_targets(img, at, end, b'\x48\x8D\x05')] ==
              [img.at('cpu_syscall_entry')], 'lea rax, [rip+cpu_syscall_entry]')
    rep.check('syscall_init.lstar_read_back',
              bytes.fromhex('48 3B 45 F0') in body, 'cmp rax, [rbp-16]')
    rep.check('syscall_init.sfmask_clears_exactly_if',
              (b'\xB8' + struct.pack('<I', SFMASK_VALUE)) in body and
              (bytes.fromhex('48 3D') + struct.pack('<I', SFMASK_VALUE)) in body,
              'mov eax, SFMASK; cmp rax, SFMASK')
    for name, msr, count in (('efer', 0xC0000080, 3), ('star', STAR_MSR, 2),
                             ('lstar', LSTAR_MSR, 2), ('sfmask', SFMASK_MSR, 2)):
        rep.equal('syscall_init.selects_%s' % name,
                  body.count(b'\xB9' + struct.pack('<I', msr)), count)
    guard = [t for t, _ in rip_targets(img, at, end, b'\x80\x3D', 1)]
    rep.check('syscall_init.ealready_guard', guard == [img.at('syscall_ready')],
              'cmp byte [rip+cpu_syscall_ready], 0')
    armed = [(t, d[p + 6]) for t, p in rip_targets(img, at, end, b'\xC6\x05', 1)]
    rep.check('syscall_init.marks_ready_once',
              armed == [(img.at('syscall_ready'), 1)], 'ready <- 1 on success')
    touched = {n for n in rip_state_refs(img, at, end, ('syscall_ready',))}
    rep.check('syscall_init.touches_only_the_ready_flag',
              touched == {'syscall_ready'}, ' '.join(sorted(touched)))
    rep.check('syscall_init.success_zeroes_rdx',
              bytes.fromhex('31 C0 31 D2') in body, 'xor eax, eax; xor edx, edx')

    # ---------------- cpu_syscall_set_handler: the table contract -------
    at, end = img.at('cpu_syscall_set_handler'), img.at('cpu_user_fault_set_handler')
    body = d[at:end]
    table = img.at('syscall_handlers')
    rep.check('syscall_set_handler.checks_cpl',
              bytes.fromhex('66 8C C8 A8 03') in body)
    rep.check('syscall_set_handler.checks_if',
              bytes.fromhex('9C 58 A9 00 02 00 00') in body)
    for name, code in (('eperm', EPERM), ('einval', EINVAL),
                       ('ealready', EALREADY)):
        rep.check('syscall_set_handler.reports_%s' % name, mov_eax(code) in body)
    rep.check('syscall_set_handler.bounds_the_number',
              (bytes.fromhex('48 83 F9') + bytes((SYS_CALL_MAX,)) + b'\x73') in body,
              'cmp rcx, CPU_SYS_CALL_MAX; jae .einval')
    rep.equal('syscall_set_handler.canonical_check_once',
              len(find_all(body, bytes.fromhex('48 C1 E8 2F'))), 1)
    rep.equal('syscall_set_handler.canonical_limit_is_the_kernel_top',
              len(find_all(body, bytes.fromhex('48 3D') +
                           struct.pack('<I', KERNEL_TOP))), 1)
    rep.check('syscall_set_handler.zero_selects_clear',
              bytes.fromhex('48 85 D2') in body, 'test rdx, rdx')
    # [rcx*8 + disp32] with no base register: the disp32 is the absolute
    # address of the table, not a RIP relative displacement
    guard = find_all(body, bytes.fromhex('48 83 3C CD'))
    rep.check('syscall_set_handler.ealready_guard',
              len(guard) == 1 and
              struct.unpack_from('<i', body, guard[0] + 4)[0] == table - img.base and
              body[guard[0] + 8] == 0,
              'cmp qword [cpu_syscall_handlers + rcx*8], 0')
    stored = find_all(body, bytes.fromhex('48 89 14 CD'))
    rep.check('syscall_set_handler.stores_into_the_table',
              len(stored) == 1 and
              struct.unpack_from('<i', body, stored[0] + 4)[0] == table - img.base,
              'mov [cpu_syscall_handlers + rcx*8], rdx')
    cleared = find_all(body, bytes.fromhex('48 C7 04 CD'))
    rep.check('syscall_set_handler.clears_with_zero',
              len(cleared) == 1 and
              struct.unpack_from('<i', body, cleared[0] + 4)[0] == table - img.base and
              u32(body, cleared[0] + 8) == 0,
              'mov qword [cpu_syscall_handlers + rcx*8], 0')
    rep.check('syscall_set_handler.success_zeroes_rdx',
              bytes.fromhex('31 C0 31 D2') in body)
    rep.check('syscall_set_handler.table_starts_empty',
              all(u64(d, table + i * 8) == 0 for i in range(SYS_CALL_MAX)),
              'no handler until one is registered')
    rep.check('syscall_set_handler.table_inside_data',
              img['data_start'] <= table and
              table + SYS_CALL_MAX * 8 <= img['data_end'])

    # ---------------- cpu_user_fault_set_handler: one slot --------------
    at, end = img.at('cpu_user_fault_set_handler'), img.at('cpu_syscall_entry')
    body = d[at:end]
    slot = img.at('user_fault_handler')
    rep.check('user_fault_set_handler.checks_cpl',
              bytes.fromhex('66 8C C8 A8 03') in body)
    rep.check('user_fault_set_handler.checks_if',
              bytes.fromhex('9C 58 A9 00 02 00 00') in body)
    for name, code in (('eperm', EPERM), ('einval', EINVAL),
                       ('ealready', EALREADY)):
        rep.check('user_fault_set_handler.reports_%s' % name, mov_eax(code) in body)
    rep.equal('user_fault_set_handler.canonical_check_once',
              len(find_all(body, bytes.fromhex('48 C1 E8 2F'))), 1)
    rep.equal('user_fault_set_handler.canonical_limit_is_the_kernel_top',
              len(find_all(body, bytes.fromhex('48 3D') +
                           struct.pack('<I', KERNEL_TOP))), 1)
    rep.check('user_fault_set_handler.zero_selects_unregister',
              bytes.fromhex('48 85 C9') in body, 'test rcx, rcx')
    stored = [t for t, _ in rip_targets(img, at, end, b'\x48\x89\x0D')]
    rep.check('user_fault_set_handler.stores_the_handler', stored == [slot],
              'mov [rip+cpu_user_fault_handler], rcx')
    cleared = [(t, p) for t, p in rip_targets(img, at, end, b'\x48\xC7\x05', 4)
               if t == slot]
    rep.check('user_fault_set_handler.clears_with_zero',
              len(cleared) == 1 and
              struct.unpack_from('<i', d, cleared[0][1] + 7)[0] == 0,
              'mov qword [rip+cpu_user_fault_handler], 0')
    guard = [t for t, _ in rip_targets(img, at, end, b'\x48\x83\x3D', 1)]
    rep.check('user_fault_set_handler.ealready_guard', guard == [slot],
              'cmp qword [rip+cpu_user_fault_handler], 0')
    touched = {n for n in rip_state_refs(img, at, end, ('user_fault_handler',))}
    rep.check('user_fault_set_handler.touches_only_the_slot',
              touched == {'user_fault_handler'}, ' '.join(sorted(touched)))
    rep.check('user_fault_set_handler.success_zeroes_rdx',
              bytes.fromhex('31 C0 31 D2') in body)
    rep.check('user_fault_set_handler.slot_starts_empty', u64(d, slot) == 0)
    rep.check('user_fault_set_handler.slot_inside_data',
              img['data_start'] <= slot < img['data_end'])

    # ---------------- cpu_syscall_entry: the thin ring 3 path -----------
    at, end = img.at('cpu_syscall_entry'), img.at('cpu_com1_init')
    body = d[at:end].rstrip(b'\x00')
    head = expected_syscall_head(img, at)
    rep.check('syscall_entry.head_rebuilt_byte_by_byte', body.startswith(head),
              'park RSP, switch to RSP0, the frame head and the call protocol')
    rax_off = bytes((FRAME_GPR['rax'],))
    rep.check('syscall_entry.reads_the_number_from_the_frame',
              (bytes.fromhex('48 8B 45') + rax_off) in body, 'mov rax, [rbp+120]')
    rep.check('syscall_entry.bounds_the_number',
              (bytes.fromhex('48 83 F8') + bytes((SYS_CALL_MAX,)) + b'\x73') in body,
              'cmp rax, CPU_SYS_CALL_MAX; jae .unknown')
    loads = find_all(body, bytes.fromhex('48 8B 04 C5'))
    rep.check('syscall_entry.indexes_the_handler_table',
              len(loads) == 1 and
              struct.unpack_from('<i', body, loads[0] + 4)[0] == table - img.base,
              'mov rax, [cpu_syscall_handlers + rax*8]')
    rep.check('syscall_entry.null_slot_check',
              bytes.fromhex('48 85 C0 74') in body,
              'test rax, rax; jz .unknown')
    rep.equal('syscall_entry.one_indirect_call',
              body.count(bytes.fromhex('FF D0')), 1)
    rep.check('syscall_entry.passes_the_frame',
              bytes.fromhex('48 89 E9 FF D0') in body, 'mov rcx, rbp; call rax')
    rep.check('syscall_entry.writes_the_result_back',
              (bytes.fromhex('48 89 45') + rax_off) in body, 'mov [rbp+120], rax')
    rep.check('syscall_entry.unknown_returns_enotsup',
              (bytes.fromhex('48 C7 45') + rax_off +
               struct.pack('<i', ENOTSUP)) in body,
              'mov qword [rbp+120], CPU_ENOTSUP')
    tail = (bytes.fromhex('48 89 EC 48 83 C4 08') +
            reverse_gpr_block(STUB_PUSH_GPR) +
            bytes.fromhex('48 83 C4 10 59 48 83 C4 08 41 5B 5C 48 0F 07'))
    rep.check('syscall_entry.leaves_by_sysretq', body.endswith(tail),
              'pops undo the frame, RCX/R11 from RIP/RFLAGS, pop rsp, sysretq')
    code = d[img.at('code_start'):img.at('code_end')]
    rep.equal('syscall_entry.one_sysret_in_the_module',
              code.count(bytes.fromhex('48 0F 07')), 1)
    rep.check('syscall_entry.no_swapgs',
              bytes.fromhex('0F 01 F8') not in code,
              'this kernel never programs the GS base')


def check_dispatch_boundary(rep, img):
    """FS-v1 M3: the dispatch and registration boundaries at 16 slots.

    A pure model of the two unsigned compares the entry and the
    registration perform, replayed over the interesting numbers: the
    frozen APP-v1 set, the file syscall range, the last legal slot,
    the first refused number and the wrap-around cases. The byte
    assertions above pin the compares themselves; this pins the
    semantics a drifted compare would break.
    """
    mask = (1 << 64) - 1

    def dispatch(number, slots):
        # cmp rax, CPU_SYS_CALL_MAX; jae .unknown  (unsigned)
        if number >= SYS_CALL_MAX:
            return ENOTSUP
        handler = slots.get(number, 0)
        if handler == 0:
            return ENOTSUP
        return handler

    def register(number, slots):
        # cmp rcx, CPU_SYS_CALL_MAX; jae .einval  (unsigned)
        if number >= SYS_CALL_MAX:
            return EINVAL
        if slots.get(number, 0) != 0:
            return EALREADY
        return 0

    empty = {}
    for number in (0, 3, 4, 9, 10, 15):
        rep.equal('dispatch_boundary.empty_slot_%d' % number,
                  dispatch(number, empty), ENOTSUP)
    rep.equal('dispatch_boundary.first_refused', dispatch(16, empty), ENOTSUP)
    rep.equal('dispatch_boundary.negative_wraps',
              dispatch((-1) & mask, empty), ENOTSUP)
    rep.equal('dispatch_boundary.huge_refused', dispatch(1 << 40, empty), ENOTSUP)
    live = {9: 0x1234}
    rep.equal('dispatch_boundary.registered_calls', dispatch(9, live), 0x1234)
    rep.equal('dispatch_boundary.neighbour_stays_empty',
              dispatch(10, live), ENOTSUP)
    rep.equal('dispatch_boundary.last_slot_registerable',
              register(15, empty), 0)
    rep.equal('dispatch_boundary.boundary_refused', register(16, empty), EINVAL)
    rep.equal('dispatch_boundary.negative_register_refused',
              register((-1) & mask, empty), EINVAL)
    rep.equal('dispatch_boundary.double_register', register(9, live), EALREADY)
    # the image itself: all sixteen slots start empty and inside data
    d = img.data
    table = img.at('syscall_handlers')
    rep.check('dispatch_boundary.image_table_shape',
              all(u64(d, table + i * 8) == 0 for i in range(16)) and
              table + 16 * 8 <= img['data_end'],
              'sixteen zero slots, the last one inside the data region')


def check_selftest_abi(rep, img):
    """The self test is an ordinary function and keeps the ABI it was given.

    RDI is callee saved and the #F1 leg poisons its buffer with rep stosq, so
    every path out of cpu_selftest has to put back what came in. A missing
    restore is invisible until a caller keeps a string destination across the
    call, which is why it is checked structurally here instead of observed.
    """
    d = img.data
    at, end = img.at('cpu_selftest'), img.at('cpu_init')
    body = d[at:end]

    rep.check('selftest.uses_rdi', bytes.fromhex('F3 48 AB') in body,
              'rep stosq is why RDI has to be saved')
    saves = find_all(body, SELFTEST_SAVE_RDI)
    rep.check('selftest.saves_rdi_in_the_frame', len(saves) == 1,
              'mov [rbp-32], rdi')
    rep.check('selftest.saves_rdi_on_entry', bool(saves) and saves[0] <= 16,
              'saved %d bytes into the body' % (saves[0] if saves else -1))
    exits = find_all(body, EPILOGUE)
    rep.check('selftest.has_return_paths', len(exits) >= 4,
              '%d leave/ret' % len(exits))
    missing = [p for p in exits if body[p - 4:p] != SELFTEST_RESTORE_RDI]
    rep.check('selftest.restores_rdi_on_every_return', not missing,
              'no restore before leave/ret at %s'
              % ' '.join(str(p) for p in missing))
    restores = find_all(body, SELFTEST_RESTORE_RDI)
    rep.equal('selftest.one_restore_per_exit', len(restores), len(exits))


def check_tss_runtime_type(rep, img):
    """0x89 is what the image holds, 0x8B is what LTR leaves behind.

    The two must not be confused: the static GDT check may see either, but the
    TR check runs after LTR and must see the busy bit, otherwise the TSS was
    never loaded. The image itself is still built with the available type.
    """
    d = img.data
    at, end = img.at('cpu_selftest'), img.at('cpu_init')
    body = d[at:end]
    type_at = img['gdt_tss'] + 5

    reads = [off - at for target, off in rip_targets(img, at, end, b'\x0F\xB6\x05')
             if target == type_at]
    rep.equal('selftest.reads_the_tss_type_twice', len(reads), 2)
    if len(reads) != 2:
        return

    def cmp_imm(p):
        if body[p:p + 3] == b'\x48\x81\xF8':
            return struct.unpack_from('<I', body, p + 3)[0]
        if body[p:p + 2] == b'\x48\x3D':
            return struct.unpack_from('<I', body, p + 2)[0]
        return None

    def cmp_after(p):
        """The immediate of the first cmp rax in the bytes after p."""
        for q in range(p, p + 16):
            value = cmp_imm(q)
            if value is not None:
                return value
        return None

    static_at, runtime_at = reads
    rep.equal('selftest.static_type_accepts_available', cmp_after(static_at),
              TSS_DESC_TYPE)
    rep.check('selftest.static_type_accepts_busy_too',
              bytes.fromhex('48 3D') + struct.pack('<I', TSS_DESC_TYPE_BUSY)
              in body[static_at:static_at + 24],
              'the image may already carry what the CPU wrote')
    rep.equal('selftest.runtime_type_demands_busy', cmp_after(runtime_at),
              TSS_DESC_TYPE_BUSY)
    rep.check('tss.desc_type_in_the_image_is_available',
              u8(d, type_at) == TSS_DESC_TYPE,
              'the image is built with 0x89; LTR changes it at run time')


def check_gdt_runtime_mask(rep, img):
    """Loading a segment sets its accessed bit; the compare must mask it.

    retfq into CS and mov into DS/ES/SS make the CPU write the A bit
    (qword bit 40) into the GDT in memory, so an exact compare against the
    built value fails on real hardware. The self test has to AND the
    observed descriptor with a mask that clears exactly that bit before
    comparing, while the image itself stays built with the bit clear.
    """
    d = img.data
    at, end = img.at('cpu_selftest'), img.at('cpu_init')
    body = d[at:end]
    for name, slot, value in (('code', 'gdt_code', GDT_CODE_VALUE),
                              ('data', 'gdt_data', GDT_DATA_VALUE)):
        reads = [off - at for target, off in
                 rip_targets(img, at, end, b'\x48\x8B\x05')
                 if target == img[slot]]
        rep.equal('selftest.%s_read_once' % name, len(reads), 1)
        if len(reads) != 1:
            continue
        p = reads[0]
        rep.check('selftest.%s_masks_the_observed_value' % name,
                  body[p + 7:p + 9] == b'\x48\xBA' and
                  body[p + 17:p + 20] == bytes.fromhex('48 21 D0'),
                  'mov rdx, mask / and rax, rdx right after the read')
        mask = u64(body, p + 9) if body[p + 7:p + 9] == b'\x48\xBA' else None
        rep.check('selftest.%s_mask_clears_only_accessed' % name,
                  mask == GDT_RUNTIME_MASK,
                  'mask %s' % (hex(mask) if mask is not None else 'missing'))
        rep.check('selftest.%s_compares_the_built_value' % name,
                  body[p + 20:p + 22] == b'\x48\xBA' and
                  u64(body, p + 22) == value and
                  body[p + 30:p + 33] == bytes.fromhex('48 39 D0'),
                  'mov rdx, built value / cmp rax, rdx')
        rep.check('gdt.%s_image_accessed_clear' % name,
                  u64(d, img[slot]) & GDT_DESC_ACCESSED == 0,
                  'the image keeps the built value; only hardware sets A')


def check_selftest(rep, img):
    """The self test must take the six resumable vectors, nothing else.

    The strongest evidence here is not the call count but the constant each
    arming puts in RDX: the low half of the probe gives every label its file
    offset, so the expected saved RIP of a leg must be the offset of the
    instruction the hardware really blames, for faults the instruction itself
    and for int n the one that follows.
    """
    d = img.data
    at, end = img.at('cpu_selftest'), img.at('cpu_init')
    body = d[at:end]
    targets = [target for _, target in call_sites(img, at, end)]
    # state leg: two armings (the second is EBUSY) and three finishes
    # (ENOTREADY, EFAULT, ENOTREADY), six vector legs, six finishes
    rep.equal('selftest.expect_calls', targets.count(img['cpu_trap_expect']), 8)
    rep.equal('selftest.finish_calls', targets.count(img['cpu_trap_finish']), 10)
    rep.equal('selftest.console_init_calls', targets.count(img['cpu_com1_init']), 1)
    rep.check('selftest.requires_interrupts_off',
              bytes.fromhex('A9 00 02 00 00') in body, 'RFLAGS.IF must be clear')
    rep.check('selftest.requires_cpu_init', bytes.fromhex('80 3D') in body,
              'cpu_state is read before anything else')
    rep.check('selftest.reads_gdtr_idtr',
              bytes.fromhex('0F 01 45') in body and bytes.fromhex('0F 01 4D') in body,
              'sgdt and sidt into the frame')
    rep.check('selftest.reads_tr', bytes.fromhex('66 0F 00 C8') in body, 'str ax')
    rep.check('selftest.walks_all_gates',
              bytes.fromhex('49 81 F8 00 01 00 00') in body, 'cmp r8, 0x100')

    # ---------------- the state machine ---------------------------------
    for name, value in (('enotready', ENOTREADY), ('efault', EFAULT),
                        ('ebusy', EBUSY)):
        rep.check('selftest.compares_%s' % name, cmp_eax(value) in body,
                  cmp_eax(value).hex(' '))

    # ---------------- the traps themselves ------------------------------
    rep.check('selftest.takes_int_f0', bytes.fromhex('CD F0') in body, 'int 0xF0')
    rep.check('selftest.takes_int_f1', bytes.fromhex('CD F1') in body,
              'int 0xF1, the gate that carries IST4')
    rep.check('selftest.takes_int3', bytes.fromhex('CC 31 C9 E8') in body, 'int3')
    rep.check('selftest.divides_by_zero', bytes.fromhex('F7 F1') in body, 'div ecx')
    rep.check('selftest.takes_ud2', bytes.fromhex('0F 0B') in body, 'ud2')
    rep.check('selftest.page_fault_leg_is_optional',
              bytes.fromhex('48 8B 45 F8 48 85 C0 74') in body,
              'the caller supplies the unmapped address, zero skips the leg')
    rep.check('selftest.judges_by_mandatory_mask',
              bytes.fromhex('48 C7 C2 FF 01 00 00') in body and
              bytes.fromhex('48 21 D0') in body,
              'the skipped leg must not decide the result')

    # ---------------- the expected saved RIPs are the real ones ---------
    def imm_present(value):
        return struct.pack('<I', value) in body or struct.pack('<Q', value) in body

    def rdx_imm_before(p):
        """The immediate the instruction closest before p loads into RDX."""
        p = p - at                         # p arrives as a file offset
        best = None
        for needle, size in ((b'\x48\xC7\xC2', 4), (b'\xBA', 4),
                             (b'\x48\xBA', 8)):
            q = body.rfind(needle, max(0, p - 32), p)
            while q >= 0:
                end = q + len(needle) + size
                if end <= p and (best is None or end > best[0]):
                    imm = struct.unpack_from('<I' if size == 4 else '<Q',
                                             body, q + len(needle))[0]
                    best = (end, imm)
                q = body.rfind(needle, max(0, p - 32), q + 1)
        return best[1] if best else None

    markers = (b'\xCD\xF0', b'\xCD\xF1', b'\xCC\x31\xC9\xE8',
               b'\x31\xC9\xF7\xF1', b'\x0F\x0B', b'\x48\x8B\x00')
    rep.check('selftest.one_site_per_leg',
              all(len(find_all(body, m)) == 1 for m in markers),
              'the saved RIP check needs exactly one instruction per leg')
    # a trap instruction is located by a byte pattern of its own; the RIP the
    # hardware saves is the address of the next instruction for int n and
    # int3, and the address of the instruction itself for a fault
    legs = {}
    for name, needle, add in (('int_f0', b'\xCD\xF0', 2),
                              ('int_f1', b'\xCD\xF1', 2),
                              ('int3', b'\xCC\x31\xC9\xE8', 1)):
        p = find_one(body, needle)
        legs[name] = p if p < 0 else at + p + add
        rep.check('selftest.expects_the_saved_rip_of_%s' % name,
                  p >= 0 and imm_present(legs[name]),
                  'the hardware saves %#x' % legs[name])
    for name, needle, add in (('div', b'\x31\xC9\xF7\xF1', 2),
                              ('ud2', b'\x0F\x0B', 0),
                              ('load', b'\x48\x8B\x00', 0)):
        p = find_one(body, needle)
        legs[name] = p if p < 0 else at + p + add
        rep.check('selftest.expects_the_faulting_rip_of_%s' % name,
                  p >= 0 and imm_present(legs[name]),
                  'the hardware saves %#x' % legs[name])

    # an immediate somewhere in the body is not enough: every arming must load
    # its own leg's address into RDX. The state leg arms #UD three times and
    # never fires it, the other five legs arm themselves once each.
    expect_calls = [off for off, tgt in call_sites(img, at, end)
                    if tgt == img['cpu_trap_expect']]
    armed = [rdx_imm_before(off) for off in expect_calls]
    wanted = ([legs['ud2']] * 3 +
              [legs[n] for n in ('int_f0', 'int_f1', 'int3', 'div', 'load')])
    rep.check('selftest.arms_each_trap_with_its_own_saved_rip',
              None not in armed and sorted(armed) == sorted(wanted),
              'armed %s' % ' '.join('?' if v is None else '%#x' % v
                                    for v in armed))

    # ---------------- the result buffer ---------------------------------
    out = img.at('selftest_out')
    rep.check('selftest.uses_the_expect_block',
              [t for t, _ in rip_targets(img, at, end, b'\x4C\x8D\x0D')] ==
              [img.at('selftest_expect')], 'lea r9, [rip+cpu_selftest_expect]')
    rep.check('selftest.poisons_the_result_buffer',
              bytes.fromhex('B8 AA AA AA AA') in body and
              bytes.fromhex('F3 48 AB') in body,
              'the copy must overwrite every byte, poison first')
    rep.check('selftest.collects_into_its_own_buffer',
              [t for t, _ in rip_targets(img, at, end, b'\x48\x8D\x0D')].count(out) >= 2,
              'lea rcx, [rip+cpu_selftest_out]')
    reads = {t - out for t, _ in rip_targets(img, at, end, b'\x48\x8B\x05')}
    rep.check('selftest.inspects_the_copied_frame',
              {FRAME_CR2, FRAME_VECTOR, FRAME_ERROR, FRAME_RIP, FRAME_CS,
               FRAME_RSP, FRAME_SS} <= reads,
              'fields read back: %s' % sorted(reads))
    rep.check('selftest.bounds_the_handler_frame_to_the_ist_stack',
              imm_present(img['ist4']) and imm_present(img['ist4'] + IST_SIZE),
              'the reported frame address must lie inside IST4: %#x..%#x'
              % (img['ist4'], img['ist4'] + IST_SIZE))


def check_sources(rep, root):
    module = root / 'kernel' / 'cpu'
    files = sorted(p.name for p in module.glob('*'))
    rep.equal('source.file_set', files, [
        'api.inc', 'cpu.asm', 'data.inc', 'gdt_tss.inc',
        'idt_traps.inc', 'panic.inc', 'selftest.inc', 'syscall.inc'])

    api = (module / 'api.inc').read_text(encoding='utf-8')
    rep.check('source.api_standalone',
              not re.search(r"^\s*include\b", api, re.M),
              'api.inc must not include anything')
    rep.check('source.api_has_contract_prefix', 'CPU_API_VERSION' in api)
    api_asserts = len(re.findall(r'\berr\b', api))
    rep.check('source.api_asserts_the_contract', api_asserts >= 20,
              '%d explicit assertions' % api_asserts)

    data_text = (module / 'data.inc').read_text(encoding='utf-8')
    data_asserts = len(re.findall(r'\berr\b', data_text))
    rep.check('source.data_asserts_the_layout', data_asserts >= 10,
              '%d explicit assertions' % data_asserts)
    rep.check('source.data_emits_zero_stacks',
              len(re.findall(r"db\s+CPU_IST_SIZE\s+dup\s*\(\s*0\s*\)", data_text)) == IST_COUNT,
              'every stack reserves real zero bytes, not rb')
    rep.check('source.data_no_uninitialised_reservation',
              not re.search(r"^\s*rb\b", data_text, re.M),
              'rb would leave a hole in the image')

    for path in sorted(module.glob('*')):
        text = path.read_text(encoding='utf-8')
        rep.check('source.%s.no_tabs' % path.name, '\t' not in text)
        rep.check('source.%s.trailing_newline' % path.name, text.endswith('\n'))
        rep.check('source.%s.ascii_comments' % path.name,
                  all(ord(ch) < 0x80 for ch in text),
                  'comments and strings stay in English ASCII')
        rep.check('source.%s.no_fasm1_macros' % path.name,
                  not re.search(r'^\s*(proc|endp|struc|ends)\b', text, re.M),
                  'no FASM 1.x struct/proc macros')
        rep.check('source.%s.no_org' % path.name,
                  not re.search(r'^\s*org\b', text, re.M),
                  'the includer owns the link base')
        rep.check('source.%s.no_use64' % path.name,
                  not re.search(r'^\s*use64\b', text, re.M),
                  'the includer selects the mode')
        rep.check('source.%s.no_interrupt_enable' % path.name,
                  not re.search(r'^\s*sti\b', text, re.M),
                  'the module never enables interrupts on its own')

    order = re.findall(r"^include\s+'([^']+)'", (module / 'cpu.asm').read_text(encoding='utf-8'), re.M)
    rep.equal('source.include_order', order, [
        'api.inc', 'gdt_tss.inc', 'idt_traps.inc', 'syscall.inc', 'panic.inc',
        'selftest.inc', 'data.inc'])


def check_position_independence(rep, asm_, root, out, low):
    hi_path = out / 'layout-hi.bin'
    ok, text = asm_.run(root / 'tests/stage2-cpu/layout.asm', hi_path, org=KERNEL_BASE)
    if not rep.check('reloc.assembles_at_kernel_base', ok, text.strip()[-400:]):
        return
    hi = load_image(hi_path, KERNEL_BASE)
    delta = KERNEL_BASE
    shifted = True
    for name in ('gdt', 'tss', 'idt', 'gdtr', 'idtr', 'data_start', 'data_end',
                 'ist1', 'ist2', 'ist3', 'ist4', 'panic_stack', 'stubs_start',
                 'stubs_end', 'dispatch', 'code_start', 'code_end',
                 'cpu_init', 'cpu_trap_dispatch', 'cpu_panic',
                 'cpu_irq_set_handler', 'irq_handler',
                 'cpu_syscall_init', 'cpu_syscall_entry', 'gdt_user_data',
                 'gdt_user_code', 'user_fault_handler', 'syscall_handlers',
                 'syscall_scratch_rsp'):
        shifted = shifted and hi[name] == low[name] + delta
    rep.check('reloc.labels_shift_by_kernel_base', shifted)
    for name in ('gdt_size', 'tss_size', 'idt_size', 'frame_size', 'vectors'):
        rep.equal('reloc.%s_unchanged' % name, hi[name], low[name])

    d = hi.data
    idt, tss = hi.at('idt'), hi.at('tss')
    gates_ok = True
    for vec in range(IDT_VECTORS):
        target, selector, ist, type_attr, reserved = gate(d, idt, vec)
        low_target = gate(low.data, low.at('idt'), vec)[0]
        gates_ok = gates_ok and selector == SEL_CODE and type_attr == IDT_TYPE_ATTR and \
            ist == IST_MAP.get(vec, 0) and target == low_target + delta
    rep.check('reloc.gates_follow_the_base', gates_ok)

    gdt_tss = hi.at('gdt_tss')
    base = u16(d, gdt_tss + 2) | (u8(d, gdt_tss + 4) << 16) | \
        (u8(d, gdt_tss + 7) << 24) | (u32(d, gdt_tss + 8) << 32)
    rep.equal('reloc.tss_descriptor_base', base, hi['tss'])
    rep.equal('reloc.gdtr_base', u64(d, hi.at('gdtr') + 2), hi['gdt'])
    rep.equal('reloc.idtr_base', u64(d, hi.at('idtr') + 2), hi['idt'])
    ist = u64(d, tss + TSS_IST[1])
    rep.equal('reloc.ist1_top', ist, hi['ist1'] + IST_SIZE)


# ----------------------------------------------------------------------
# negative fixtures
# ----------------------------------------------------------------------

FIXTURES = [
    # name, file, old text, new text, where the failure must show up
    ('tss-size', 'kernel/cpu/api.inc',
     'CPU_TSS_OFF_IOMAP   = 102', 'CPU_TSS_OFF_IOMAP   = 100', 'assemble'),
    ('selector', 'kernel/cpu/api.inc',
     'CPU_SEL_TSS         = 0x18', 'CPU_SEL_TSS         = 0x20', 'assemble'),
    ('missing-vector', 'kernel/cpu/api.inc',
     'CPU_IDT_VECTORS     = 256', 'CPU_IDT_VECTORS     = 255', 'check'),
    ('frame-offset', 'kernel/cpu/api.inc',
     'CPU_FRAME_VECTOR    = 128', 'CPU_FRAME_VECTOR    = 120', 'assemble'),
    # one byte immediate: the assembler sign extends it, so every vector from
    # 0x80 up arrives in the frame as 0xFFFFFFFFFFFFFF80
    ('imm8-sign', 'kernel/cpu/idt_traps.inc',
     'push    qword v', 'db      6Ah, v', 'check'),
    ('error-set', 'kernel/cpu/api.inc',
     '(v = 17) | (v = 21)', '(v = 17) | (v = 18) | (v = 21)', 'check'),
    ('iretd', 'kernel/cpu/idt_traps.inc',
     'add     rsp, 16\n        iretq', 'add     rsp, 16\n        iretd', 'either'),

    # ---- task 38: the trap API contract -------------------------------
    # the dispatcher matches the vector but no longer the saved RIP
    ('saved-rip-ignored', 'kernel/cpu/idt_traps.inc',
     '        mov     rax, [rcx + CPU_FRAME_RIP]\n'
     '        cmp     rax, [cpu_trap_expect_rip]\n'
     '        jne     .try_async\n',
     '        mov     rax, [rcx + CPU_FRAME_RIP]\n', 'check'),
    # the arming names the resume RIP where the hardware saves the faulting
    # instruction: #UD would never match at run time
    ('saved-rip-wrong', 'kernel/cpu/selftest.inc',
     '        mov     rdx, cpu_selftest_ud_fault       ; #UD saves the ud2 itself\n',
     '        mov     rdx, cpu_selftest_ud_resume      ; deliberately wrong\n',
     'check'),
    # the error code is compared without masking it
    ('error-mask-ignored', 'kernel/cpu/idt_traps.inc',
     '        mov     rax, [rcx + CPU_FRAME_ERROR]\n'
     '        mov     rdx, [cpu_trap_err_mask]\n'
     '        and     rax, rdx\n'
     '        cmp     rax, [cpu_trap_err_value]\n',
     '        mov     rax, [rcx + CPU_FRAME_ERROR]\n'
     '        mov     rdx, [cpu_trap_err_mask]\n'
     '        cmp     rax, [cpu_trap_err_value]\n', 'check'),
    # CR2 is compared without masking it
    ('cr2-mask-ignored', 'kernel/cpu/idt_traps.inc',
     '        mov     rax, [rcx]\n'
     '        mov     rdx, [cpu_trap_cr2_mask]\n'
     '        and     rax, rdx\n'
     '        cmp     rax, [cpu_trap_cr2_value]\n',
     '        mov     rax, [rcx]\n'
     '        mov     rdx, [cpu_trap_cr2_mask]\n'
     '        cmp     rax, [cpu_trap_cr2_value]\n', 'check'),
    # the 184 byte copy is not made at all
    ('result-not-copied', 'kernel/cpu/idt_traps.inc',
     '        mov     rsi, rcx\n'
     '        lea     rdi, [cpu_trap_frame_result]\n'
     '        mov     ecx, CPU_FRAME_SIZE / 8\n'
     '        cld\n'
     '        rep movsq\n',
     '        mov     rsi, rcx\n'
     '        lea     rdi, [cpu_trap_frame_result]\n'
     '        mov     ecx, CPU_FRAME_SIZE / 8\n'
     '        cld\n', 'check'),
    # only part of CPU_EXPECT is taken from the caller
    ('expect-block-partial', 'kernel/cpu/idt_traps.inc',
     '        mov     rax, [r9 + CPU_EXPECT_OFF_CR2_VALUE]\n'
     '        mov     [cpu_trap_cr2_value], rax\n',
     '        mov     rax, [r9 + CPU_EXPECT_OFF_CR2_VALUE]\n', 'check'),
    # the second arming is allowed: the armed guard is gone
    ('arm-guard-missing', 'kernel/cpu/idt_traps.inc',
     '        cmp     byte [cpu_trap_armed], 0\n'
     '        jne     .busy\n'
     '        cmp     byte [cpu_trap_result_ready], 0\n',
     '        cmp     byte [cpu_trap_result_ready], 0\n', 'check'),
    # the result is ready but never marked, so finish would say ENOTREADY
    ('result-flag-stuck', 'kernel/cpu/idt_traps.inc',
     '        mov     byte [cpu_trap_result_ready], 1\n',
     '        mov     byte [cpu_trap_result_ready], 0\n', 'check'),
    # an armed trap that never fired must not report EIO
    ('status-efault', 'kernel/cpu/idt_traps.inc',
     '        ; the trap never fired: report it and disarm, so the next arming works\n'
     '        mov     byte [cpu_trap_armed], 0\n'
     '        mov     eax, CPU_EFAULT\n',
     '        ; the trap never fired: report it and disarm, so the next arming works\n'
     '        mov     byte [cpu_trap_armed], 0\n'
     '        mov     eax, CPU_EIO\n', 'check'),
    ('status-enotready', 'kernel/cpu/idt_traps.inc',
     '        jmp     .done\n.not_ready:\n        mov     eax, CPU_ENOTREADY\n',
     '        jmp     .done\n.not_ready:\n        mov     eax, CPU_EIO\n', 'check'),
    # RDI is callee saved: without the frame slot a caller loses its own value
    ('selftest-rdi-unsaved', 'kernel/cpu/selftest.inc',
     '        mov     [rbp-32], rdi               ; callee saved, rep stosq clobbers it\n',
     '', 'check'),
    # one return path without the restore is a broken ABI for that path only
    ('selftest-rdi-unrestored', 'kernel/cpu/selftest.inc',
     'cpu_selftest_precondition:\n'
     '        mov     eax, CPU_EPERM\n'
     '        xor     edx, edx\n'
     '        xor     r8d, r8d\n'
     '        mov     rdi, [rbp-32]\n'
     '        leave\n',
     'cpu_selftest_precondition:\n'
     '        mov     eax, CPU_EPERM\n'
     '        xor     edx, edx\n'
     '        xor     r8d, r8d\n'
     '        leave\n', 'check'),
    # after LTR the descriptor is busy; demanding the built type fails at run
    ('tss-type-stale', 'kernel/cpu/selftest.inc',
     '        movzx   eax, byte [cpu_gdt_tss + 5]\n'
     '        cmp     rax, CPU_TSS_DESC_TYPE_BUSY\n'
     '        jne     cpu_selftest_tr_bad\n',
     '        movzx   eax, byte [cpu_gdt_tss + 5]\n'
     '        cmp     rax, CPU_TSS_DESC_TYPE\n'
     '        jne     cpu_selftest_tr_bad\n', 'check'),
    # the static check accepts both, it must not demand the busy type only
    ('tss-type-busy-only', 'kernel/cpu/selftest.inc',
     '        cmp     rax, CPU_TSS_DESC_TYPE\n'
     '        je      cpu_selftest_tss_type_ok\n',
     '        cmp     rax, CPU_TSS_DESC_TYPE_BUSY\n'
     '        je      cpu_selftest_tss_type_ok\n', 'check'),
    # the image must be built available: LTR is what sets the busy bit
    ('tss-type-image-busy', 'kernel/cpu/data.inc',
     '        db      CPU_TSS_DESC_TYPE\n', '        db      CPU_TSS_DESC_TYPE_BUSY\n',
     'check'),

    # ---- task 64: the accessed bit the CPU writes on segment load ------
    # an exact compare of the data descriptor fails once DS/ES/SS are loaded
    ('gdt-data-exact-compare', 'kernel/cpu/selftest.inc',
     '        mov     rax, [cpu_gdt + CPU_GDT_OFF_DATA]\n'
     '        mov     rdx, CPU_GDT_DESC_RUNTIME_MASK\n'
     '        and     rax, rdx                    ; DS/ES/SS were loaded with mov\n',
     '        mov     rax, [cpu_gdt + CPU_GDT_OFF_DATA]\n', 'check'),
    # CS is reloaded through retfq, so the code descriptor gets A set too
    ('gdt-code-exact-compare', 'kernel/cpu/selftest.inc',
     '        mov     rax, [cpu_gdt + CPU_GDT_OFF_CODE]\n'
     '        mov     rdx, CPU_GDT_DESC_RUNTIME_MASK\n'
     '        and     rax, rdx                    ; CS was reloaded through retfq\n',
     '        mov     rax, [cpu_gdt + CPU_GDT_OFF_CODE]\n', 'check'),
    # a mask that clears some other bit leaves A in the compare
    ('gdt-mask-wrong-bit', 'kernel/cpu/api.inc',
     'CPU_GDT_DESC_RUNTIME_MASK = not CPU_GDT_DESC_ACCESSED',
     'CPU_GDT_DESC_RUNTIME_MASK = not (1 shl 41)', 'check'),
    # the image must stay built with A clear; only hardware sets it
    ('gdt-data-image-accessed', 'kernel/cpu/data.inc',
     '        dq      CPU_GDT_DATA_VALUE\n',
     '        dq      (CPU_GDT_DATA_VALUE or CPU_GDT_DESC_ACCESSED)\n',
     'check'),
    # the built constant itself must keep A clear: the assertion fires
    ('gdt-data-constant-accessed', 'kernel/cpu/api.inc',
     'CPU_GDT_DATA_VALUE  = 0x00CF92000000FFFF',
     'CPU_GDT_DATA_VALUE  = 0x00CF93000000FFFF', 'assemble'),

    # ---- task 69: the asynchronous IRQ hook ----------------------------
    # without the vector floor an architectural trap could reach the handler
    ('irq-vector-floor-removed', 'kernel/cpu/idt_traps.inc',
     '        cmp     rdx, CPU_IRQ_VECTOR_MIN\n'
     '        jb      .unexpected\n',
     '', 'check'),
    # the registration must refuse to run with interrupts enabled
    ('irq-if-precheck-removed', 'kernel/cpu/idt_traps.inc',
     '        pushfq\n'
     '        pop     rax\n'
     '        test    eax, CPU_RFLAGS_IF\n'
     '        jnz     .eperm\n',
     '', 'check'),
    # a second registration must be EALREADY, not a silent overwrite
    ('irq-ealready-removed', 'kernel/cpu/idt_traps.inc',
     '        cmp     qword [cpu_irq_handler], 0\n'
     '        jne     .ealready\n',
     '', 'check'),
    # an unregistered handler must panic, not call address zero
    ('irq-null-handler-called', 'kernel/cpu/idt_traps.inc',
     '        test    rax, rax\n'
     '        jz      .unexpected\n',
     '', 'check'),

    # ---- task 113: APP-v1, the user segments and the syscall path ------
    # a ring 3 data descriptor built DPL 0 can never be loaded from ring 3
    ('user-data-dpl0', 'kernel/cpu/api.inc',
     'CPU_GDT_USER_DATA_VALUE = 0x00CFF2000000FFFF',
     'CPU_GDT_USER_DATA_VALUE = 0x00CF92000000FFFF', 'assemble'),
    # a STAR base that no longer derives the ring 3 selector pair
    ('star-drift', 'kernel/cpu/api.inc',
     'CPU_STAR_VALUE      = (0x20 shl 48) or (0x08 shl 32)',
     'CPU_STAR_VALUE      = (0x18 shl 48) or (0x08 shl 32)', 'assemble'),
    # the read back compare drifted even though the constant stayed right
    ('star-code-drift', 'kernel/cpu/syscall.inc',
     '        mov     rcx, CPU_STAR_VALUE\n',
     '        mov     rcx, CPU_STAR_VALUE or (1 shl 48)\n', 'check'),
    # SFMASK must clear exactly IF, nothing else
    ('sfmask-drift', 'kernel/cpu/api.inc',
     'CPU_SFMASK_VALUE    = CPU_RFLAGS_IF         ; SYSCALL clears IF, nothing else',
     'CPU_SFMASK_VALUE    = CPU_RFLAGS_IF or 0x100', 'assemble'),
    ('sfmask-code-drift', 'kernel/cpu/syscall.inc',
     '        mov     eax, CPU_SFMASK_VALUE\n',
     '        mov     eax, CPU_SFMASK_VALUE or 0x100\n', 'check'),
    # the entry runs with IF cleared by SFMASK; an STI leaks interrupts
    # into a half built frame on the kernel stack
    ('entry-sti-leak', 'kernel/cpu/syscall.inc',
     'cpu_syscall_entry:\n',
     'cpu_syscall_entry:\n        sti\n', 'check'),
    # staying on the untrusted user stack instead of switching to RSP0
    ('entry-rsp0-drift', 'kernel/cpu/syscall.inc',
     '        mov     rsp, [cpu_tss_rsp0]\n',
     '        mov     rsp, [cpu_syscall_scratch_rsp]\n', 'check'),
    # the frame sentinel must stay 0x100, past every IDT vector
    ('sentinel-drift', 'kernel/cpu/api.inc',
     'CPU_SYS_VECTOR      = 0x100',
     'CPU_SYS_VECTOR      = 0x101', 'check'),
    # the image must stay built with A clear; only hardware sets it
    ('user-code-image-accessed', 'kernel/cpu/data.inc',
     '        dq      CPU_GDT_USER_CODE_VALUE\n',
     '        dq      (CPU_GDT_USER_CODE_VALUE or CPU_GDT_DESC_ACCESSED)\n',
     'check'),
    # an unregistered user fault hook must panic, not call address zero
    ('user-fault-guard-removed', 'kernel/cpu/idt_traps.inc',
     '        mov     rax, [cpu_user_fault_handler]\n'
     '        test    rax, rax\n'
     '        jz      .user_panic\n',
     '        mov     rax, [cpu_user_fault_handler]\n'
     '        test    rax, rax\n', 'check'),
    # a second registration must be EALREADY, not a silent overwrite
    ('syscall-ealready-removed', 'kernel/cpu/syscall.inc',
     '        cmp     qword [cpu_syscall_handlers + rcx*8], 0\n'
     '        jne     .ealready\n',
     '', 'check'),
    # SYSRET is the only way back: IRETQ would consume the wrong frame
    ('sysret-to-iret', 'kernel/cpu/syscall.inc',
     '        sysretq\n',
     '        iretq\n', 'check'),

    # ---- task 133: FS-v1 M3, the 16 slot table and its boundaries ------
    # shrinking the table back to 4 breaks the manifest shape and both
    # pinned dispatch compares
    ('syscall-max-drift', 'kernel/cpu/api.inc',
     'CPU_SYS_CALL_MAX    = 16',
     'CPU_SYS_CALL_MAX    = 4', 'check'),
    # jae -> ja lets number 16 index one slot past the table
    ('syscall-dispatch-bound-drift', 'kernel/cpu/syscall.inc',
     '        cmp     rax, CPU_SYS_CALL_MAX\n'
     '        jae     .unknown\n',
     '        cmp     rax, CPU_SYS_CALL_MAX\n'
     '        ja      .unknown\n', 'check'),
    # jae -> ja lets registration write one slot past the table
    ('syscall-set-bound-drift', 'kernel/cpu/syscall.inc',
     '        cmp     rcx, CPU_SYS_CALL_MAX\n'
     '        jae     .einval\n',
     '        cmp     rcx, CPU_SYS_CALL_MAX\n'
     '        ja      .einval\n', 'check'),
]


def run_fixtures(rep, root, out, fasmg, fasm2_dir):
    checker = root / 'tests' / 'stage2-cpu' / 'check.py'
    for name, rel, old, new, where in FIXTURES:
        with tempfile.TemporaryDirectory(prefix='stage2-cpu-%s-' % name) as tmp:
            tree = Path(tmp)
            (tree / 'kernel').mkdir(parents=True)
            shutil.copytree(root / 'kernel' / 'cpu', tree / 'kernel' / 'cpu')
            shutil.copytree(root / 'tests' / 'stage2-cpu', tree / 'tests' / 'stage2-cpu')
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
    out = Path(args.out).resolve() if args.out else root / 'build' / 'stage2-a'
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
    ok, text = asm_.run(root / 'tests/stage2-cpu/layout.asm', low_path)
    if not rep.check('assembler.probe', ok, text.strip()[-600:]):
        rep.print(True)
        return 1
    rep.check('assembler.probe_bytes', low_path.stat().st_size > 0,
              '%d bytes' % low_path.stat().st_size)
    rep.check('assembler.probe_quiet', 'error' not in text.lower(),
              text.strip()[-200:] or 'no diagnostics')

    img = load_image(low_path)
    stacks = [img['ist1'], img['ist2'], img['ist3'], img['ist4'], img['panic_stack']]
    check_manifest(rep, img)
    check_gdt(rep, img)
    check_tss(rep, img, stacks)
    check_stacks(rep, img)
    check_idt(rep, img)
    check_stubs(rep, img)
    check_functions(rep, img)
    check_uart(rep, img)
    check_trap_api(rep, img)
    check_irq_api(rep, img)
    check_syscall_api(rep, img)
    check_dispatch_boundary(rep, img)
    check_selftest_abi(rep, img)
    check_tss_runtime_type(rep, img)
    check_gdt_runtime_mask(rep, img)
    check_selftest(rep, img)
    check_sources(rep, root)
    check_position_independence(rep, asm_, root, out, img)

    if not args.no_fixtures:
        run_fixtures(rep, root, out, asm_.fasmg, asm_.fasm2_dir)

    print('stage2-cpu: layout.bin %d bytes, %d stubs, %d gates' %
          (len(img.data), IDT_VECTORS, IDT_VECTORS))
    return 0 if rep.print(args.verbose) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
