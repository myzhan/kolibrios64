#!/usr/bin/env python3
"""Pure Python reference model of the kolibrios64 CHIP-8 user program.

Task 114 (APP-v1 track 2). Independent implementation of the frozen
semantics ported from the 32-bit KolibriOS emulator (rgimad, GPL v2),
including the mandatory fixes:

  * 8xy5/8xy7: VF is the no-borrow flag of the OLD operands
  * DXYN: out-of-bounds pixels are skipped (no wrap, no write-through),
    VF counts collisions of actually drawn pixels only
  * call/ret stack depth bounded to 16, faults raise Chip8Exit(3)
  * unknown opcodes raise Chip8Exit(2)

Frozen original school: font set at RAM 0x000 (FX29 I=V[x]*5), PC
starts at 0x200, FX55/FX65 increment I by x+1, 8xy6/8xyE shift V[x]
in place, Bnnn uses V0, Fx0A does not advance PC without a key,
Cxkk uses the MSVC LCG (214013/2531011) with fixed seed 1.

Key table holds true press/release levels; Ex9E/ExA1/Fx0A never
consume keys. All RAM indices are masked to 12 bits, PC arithmetic
is 16-bit, matching user/chip8/emu.inc instruction for instruction.

This module imports nothing from the assembled artifacts. Running it
executes the full self-test suite and writes evidence to
build/user-chip8/model-selftest.log plus the IBM 256-step expected
frame/gfx and the probe list consumed by the integration judge.
"""

import hashlib
import json
import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "build" / "user-chip8"

MEM_SIZE = 4096
ROM_LOAD_ADDR = 0x200
FONTSET_ADDR = 0x000
STACK_DEPTH = 16
KEY_COUNT = 16
GFX_COLS = 64
GFX_ROWS = 32
GFX_SIZE = GFX_COLS * GFX_ROWS
GFX_PIX = 10
FB_WIDTH = 640
FB_HEIGHT = 320
COLOR_BACK = 0x00000000
COLOR_CELL = 0x00ADE8F4
RAND_MUL = 214013
RAND_ADD = 2531011
RAND_SEED = 1
IBM_STEPS = 256

EXIT_BAD_OPCODE = 2
EXIT_STACK_FAULT = 3

FONTSET = bytes([
    0xF0, 0x90, 0x90, 0x90, 0xF0, 0x20, 0x60, 0x20, 0x20, 0x70,
    0xF0, 0x10, 0xF0, 0x80, 0xF0, 0xF0, 0x10, 0xF0, 0x10, 0xF0,
    0x90, 0x90, 0xF0, 0x10, 0x10, 0xF0, 0x80, 0xF0, 0x10, 0xF0,
    0xF0, 0x80, 0xF0, 0x90, 0xF0, 0xF0, 0x10, 0x20, 0x40, 0x40,
    0xF0, 0x90, 0xF0, 0x90, 0xF0, 0xF0, 0x90, 0xF0, 0x10, 0xF0,
    0xF0, 0x90, 0xF0, 0x90, 0x90, 0xE0, 0x90, 0xE0, 0x90, 0xE0,
    0xF0, 0x80, 0x80, 0x80, 0xF0, 0xE0, 0x90, 0x90, 0x90, 0xE0,
    0xF0, 0x80, 0xF0, 0x80, 0xF0, 0xF0, 0x80, 0xF0, 0x80, 0x80])


class Chip8Exit(Exception):
    """SYS_EXIT with a non-zero code (fatal emulator fault)."""

    def __init__(self, code):
        super().__init__(f"SYS_EXIT({code})")
        self.code = code


class Chip8:
    def __init__(self, rom=None):
        self.ram = bytearray(MEM_SIZE)
        self.gfx = bytearray(GFX_SIZE)
        self.v = bytearray(16)
        self.stack = [0] * STACK_DEPTH
        self.key = bytearray(KEY_COUNT)
        self.i = 0
        self.pc = ROM_LOAD_ADDR
        self.sp = 0
        self.delay = 0
        self.sound = 0
        self.draw = 1
        self.rand_state = RAND_SEED
        self.ram[FONTSET_ADDR:FONTSET_ADDR + len(FONTSET)] = FONTSET
        if rom is not None:
            self.load_rom(rom)

    def load_rom(self, data):
        if len(data) > MEM_SIZE - ROM_LOAD_ADDR:
            raise ValueError("ROM does not fit")
        self.ram[ROM_LOAD_ADDR:ROM_LOAD_ADDR + len(data)] = data

    def _rand(self):
        self.rand_state = (self.rand_state * RAND_MUL + RAND_ADD) & 0xFFFFFFFF
        return (self.rand_state >> 16) & 0x7FFF

    def _pc_add(self, n):
        self.pc = (self.pc + n) & 0xFFFF

    def step(self):
        pc = self.pc & 0xFFF
        op = (self.ram[pc] << 8) | self.ram[(pc + 1) & 0xFFF]
        x = (op >> 8) & 0xF
        y = (op >> 4) & 0xF
        n = op & 0xF
        kk = op & 0xFF
        nnn = op & 0xFFF
        top = op >> 12
        v = self.v
        if top == 0x0:
            if kk == 0xE0:
                self.gfx[:] = bytes(GFX_SIZE)
                self.draw = 1
                self._pc_add(2)
            elif kk == 0xEE:
                if self.sp == 0:
                    raise Chip8Exit(EXIT_STACK_FAULT)
                self.sp -= 1
                self.pc = self.stack[self.sp]
            else:
                raise Chip8Exit(EXIT_BAD_OPCODE)
        elif top == 0x1:
            self.pc = nnn
        elif top == 0x2:
            if self.sp >= STACK_DEPTH:
                raise Chip8Exit(EXIT_STACK_FAULT)
            self.stack[self.sp] = (self.pc + 2) & 0xFFFF
            self.sp += 1
            self.pc = nnn
        elif top == 0x3:
            self._pc_add(4 if v[x] == kk else 2)
        elif top == 0x4:
            self._pc_add(2 if v[x] == kk else 4)
        elif top == 0x5:
            if n != 0:
                raise Chip8Exit(EXIT_BAD_OPCODE)
            self._pc_add(4 if v[x] == v[y] else 2)
        elif top == 0x6:
            v[x] = kk
            self._pc_add(2)
        elif top == 0x7:
            v[x] = (v[x] + kk) & 0xFF
            self._pc_add(2)
        elif top == 0x8:
            if n == 0x0:
                v[x] = v[y]
            elif n == 0x1:
                v[x] |= v[y]
            elif n == 0x2:
                v[x] &= v[y]
            elif n == 0x3:
                v[x] ^= v[y]
            elif n == 0x4:
                s = v[x] + v[y]
                v[x] = s & 0xFF
                v[0xF] = s >> 8
            elif n == 0x5:
                flag = 1 if v[x] >= v[y] else 0
                v[x] = (v[x] - v[y]) & 0xFF
                v[0xF] = flag
            elif n == 0x6:
                v[0xF] = v[x] & 1
                v[x] >>= 1
            elif n == 0x7:
                flag = 1 if v[y] >= v[x] else 0
                v[x] = (v[y] - v[x]) & 0xFF
                v[0xF] = flag
            elif n == 0xE:
                v[0xF] = v[x] >> 7
                v[x] = (v[x] << 1) & 0xFF
            else:
                raise Chip8Exit(EXIT_BAD_OPCODE)
            self._pc_add(2)
        elif top == 0x9:
            if n != 0:
                raise Chip8Exit(EXIT_BAD_OPCODE)
            self._pc_add(2 if v[x] == v[y] else 4)
        elif top == 0xA:
            self.i = nnn
            self._pc_add(2)
        elif top == 0xB:
            self.pc = (nnn + v[0]) & 0xFFF
        elif top == 0xC:
            v[x] = self._rand() & kk
            self._pc_add(2)
        elif top == 0xD:
            v[0xF] = 0
            x0, y0 = v[x], v[y]
            for row in range(n):
                py = y0 + row
                if py >= GFX_ROWS:
                    continue
                sprite = self.ram[(self.i + row) & 0xFFF]
                for bit in range(8):
                    px = x0 + bit
                    if px >= GFX_COLS:
                        continue
                    if sprite & (0x80 >> bit):
                        idx = py * GFX_COLS + px
                        if self.gfx[idx]:
                            v[0xF] = 1
                        self.gfx[idx] ^= 1
            self.draw = 1
            self._pc_add(2)
        elif top == 0xE:
            if kk == 0x9E:
                self._pc_add(4 if self.key[v[x] & 0xF] == 1 else 2)
            elif kk == 0xA1:
                self._pc_add(2 if self.key[v[x] & 0xF] == 1 else 4)
            else:
                raise Chip8Exit(EXIT_BAD_OPCODE)
        elif top == 0xF:
            if kk == 0x07:
                v[x] = self.delay
                self._pc_add(2)
            elif kk == 0x0A:
                for idx in range(KEY_COUNT):
                    if self.key[idx] == 1:
                        v[x] = idx
                        self._pc_add(2)
                        break
                # no key: PC does not advance
            elif kk == 0x15:
                self.delay = v[x]
                self._pc_add(2)
            elif kk == 0x18:
                self.sound = v[x]
                self._pc_add(2)
            elif kk == 0x1E:
                self.i = (self.i + v[x]) & 0xFFFF
                self._pc_add(2)
            elif kk == 0x29:
                self.i = (FONTSET_ADDR + v[x] * 5) & 0xFFFF
                self._pc_add(2)
            elif kk == 0x33:
                val = v[x]
                for j, div in enumerate((100, 10, 1)):
                    self.ram[(self.i + j) & 0xFFF] = (val // div) % 10
                self._pc_add(2)
            elif kk == 0x55:
                for j in range(x + 1):
                    self.ram[(self.i + j) & 0xFFF] = v[j]
                self.i = (self.i + x + 1) & 0xFFFF
                self._pc_add(2)
            elif kk == 0x65:
                for j in range(x + 1):
                    v[j] = self.ram[(self.i + j) & 0xFFF]
                self.i = (self.i + x + 1) & 0xFFFF
                self._pc_add(2)
            else:
                raise Chip8Exit(EXIT_BAD_OPCODE)
        else:
            raise Chip8Exit(EXIT_BAD_OPCODE)

    def run(self, count):
        for _ in range(count):
            self.step()

    def render(self):
        """Expand gfx to the 640x320 BGRX frame, 10x10 solid blocks."""
        fb = bytearray(FB_WIDTH * FB_HEIGHT * 4)
        back = struct.pack("<I", COLOR_BACK)
        cell = struct.pack("<I", COLOR_CELL)
        stride = FB_WIDTH * 4
        for cy in range(GFX_ROWS):
            for cx in range(GFX_COLS):
                color = cell if self.gfx[cy * GFX_COLS + cx] else back
                base = cy * GFX_PIX * stride + cx * GFX_PIX * 4
                row = color * GFX_PIX
                for py in range(GFX_PIX):
                    off = base + py * stride
                    fb[off:off + GFX_PIX * 4] = row
        return bytes(fb)


def prog(*ops):
    return b"".join(struct.pack(">H", op) for op in ops)


def machine(*ops, ram_patches=()):
    m = Chip8(prog(*ops))
    for addr, data in ram_patches:
        m.ram[addr:addr + len(data)] = data
    return m


def expect_exit(m, steps, code):
    try:
        m.run(steps)
    except Chip8Exit as e:
        assert e.code == code, f"exit {e.code}, expected {code}"
        return
    raise AssertionError(f"expected Chip8Exit({code}), none raised")


# ------------------------------------------------------------------
# self-test suite
# ------------------------------------------------------------------

def test_dxyn_collision():
    m = machine(0x6005, 0x6105, 0xA300, 0xD013, 0xD013,
                ram_patches=[(0x300, bytes([0xF0, 0xF0, 0xF0]))])
    m.run(4)
    assert m.v[0xF] == 0
    assert [m.gfx[5 * 64 + c] for c in range(4, 9)] == [0, 1, 1, 1, 1]
    assert sum(m.gfx) == 12
    m.step()
    assert m.v[0xF] == 1, "second draw must collide"
    assert sum(m.gfx) == 0, "XOR toggle must clear the sprite"


def test_dxyn_clip():
    # sprite at (62,31), two rows of 0xFF: only 2 pixels fit, row 32 skipped
    m = machine(0x603E, 0x611F, 0xA300, 0xD012,
                ram_patches=[(0x300, bytes([0xFF, 0xFF]))])
    m.run(4)
    assert sum(m.gfx) == 2
    assert m.gfx[31 * 64 + 62] == 1 and m.gfx[31 * 64 + 63] == 1
    assert m.gfx[0] == 0, "no wrap into row 0"
    assert m.v[0xF] == 0
    # collision only on drawn pixels: gfx[0] preset must not raise VF
    m2 = machine(0x603F, 0x611F, 0xA300, 0xD011,
                 ram_patches=[(0x300, bytes([0xFF]))])
    m2.gfx[0] = 1
    m2.run(4)
    assert m2.v[0xF] == 0, "clipped pixels must not count as collision"
    assert m2.gfx[0] == 1, "clipped pixels must not write through"
    assert m2.gfx[31 * 64 + 63] == 1


def test_sub_vf():
    for vx, vy, res, vf in ((5, 3, 2, 1), (3, 5, 0xFE, 0), (7, 7, 0, 1)):
        m = machine(0x6100 | vx, 0x6200 | vy, 0x8125)
        m.run(3)
        assert (m.v[1], m.v[0xF]) == (res, vf), (vx, vy, m.v[1], m.v[0xF])
        m = machine(0x6100 | vx, 0x6200 | vy, 0x8127)
        m.run(3)
        exp = (vy - vx) & 0xFF
        assert (m.v[1], m.v[0xF]) == (exp, 1 if vy >= vx else 0)


def test_shifts_in_place():
    m = machine(0x6105, 0x8126)
    m.run(2)
    assert (m.v[1], m.v[0xF]) == (2, 1)
    m = machine(0x6181, 0x812E)
    m.run(2)
    assert (m.v[1], m.v[0xF]) == (0x02, 1)


def test_bcd():
    for val, digits in ((255, (2, 5, 5)), (0, (0, 0, 0)), (99, (0, 9, 9))):
        m = machine(0x6100 | val, 0xA300, 0xF133)
        m.run(3)
        assert tuple(m.ram[0x300:0x303]) == digits


def test_fx55_fx65_i_increment():
    m = machine(0x6001, 0x6102, 0x6203, 0xA300, 0xF255, 0xA300, 0xF265)
    m.run(5)
    assert tuple(m.ram[0x300:0x303]) == (1, 2, 3)
    assert m.i == 0x303, "old VIP school: I += x + 1"
    m.v[0] = m.v[1] = m.v[2] = 0
    m.step()                      # A300: I back to 0x300
    assert m.i == 0x300
    m.step()                      # F265: reload, I advances again
    assert (m.v[0], m.v[1], m.v[2]) == (1, 2, 3)
    assert m.i == 0x303


def test_key_skips():
    # 6200 fillers occupy the skip targets so PC stays inside the program
    m = machine(0x6105, 0xE19E, 0x6200, 0xE1A1, 0x6200)
    m.key[5] = 1
    m.step()
    pc = m.pc
    m.step()                      # E19E pressed: skip
    assert m.pc == pc + 4, "Ex9E skips while pressed"
    assert m.key[5] == 1, "level semantics: no consume"
    m.step()                      # E1A1 pressed: no skip
    assert m.pc == pc + 6, "ExA1 does not skip while pressed"
    m2 = machine(0x6105, 0xE19E, 0x6200, 0xE1A1, 0x6200)
    m2.step()
    pc2 = m2.pc
    m2.step()                     # E19E released: no skip
    assert m2.pc == pc2 + 2
    m2.step()                     # 6200 filler
    m2.step()                     # E1A1 released: skip
    assert m2.pc == pc2 + 8


def test_fx0a():
    m = machine(0x6100, 0xF10A)
    m.step()
    pc = m.pc
    m.step()
    assert m.pc == pc, "Fx0A without key must not advance PC"
    m.key[3] = 1
    m.step()
    assert m.v[1] == 3 and m.pc == pc + 2
    assert m.key[3] == 1, "level semantics: no consume"


def test_stack_bounds():
    expect_exit(machine(0x00EE), 1, EXIT_STACK_FAULT)
    expect_exit(machine(0x2200), 20, EXIT_STACK_FAULT)  # self-call recursion
    m = machine(0x2206, 0x0000, 0x0000, 0x00EE)  # call 0x206, ret
    m.step()
    assert m.sp == 1 and m.pc == 0x206
    m.pc = 0x206
    m.ram[0x206:0x208] = prog(0x00EE)
    m.step()
    assert m.sp == 0 and m.pc == 0x202


def test_unknown_opcodes():
    for op in (0x0000, 0x00FD, 0x8128, 0x812F, 0xF100, 0x5121, 0x9121, 0xE100):
        expect_exit(machine(op), 1, EXIT_BAD_OPCODE)


def test_bnnn_fx29_rand():
    m = machine(0x6004, 0xB300)
    m.run(2)
    assert m.pc == 0x304
    m = machine(0x6103, 0xF129)
    m.run(2)
    assert m.i == 15
    m = machine(0xC0FF)
    m.step()
    assert m.v[0] == 41, "LCG seed 1 first draw is 41"


def test_timers_regs():
    m = machine(0x6142, 0xF115, 0xF118, 0xF207)
    m.run(3)
    assert m.delay == 0x42 and m.sound == 0x42
    m.step()
    assert m.v[2] == 0x42


def parse_api_digests():
    text = (ROOT / "user" / "chip8" / "api.inc").read_text()
    out = {}
    for name in ("IBM", "TETRIS"):
        words = []
        for k in range(4):
            m = re.search(rf"^ROM_{name}_SHA{k}\s*=\s*(0x[0-9A-Fa-f]+)",
                          text, re.M)
            assert m, f"ROM_{name}_SHA{k} missing from api.inc"
            words.append(int(m.group(1), 16))
        out[name] = struct.pack(">4Q", *words).hex()
    return out


def test_rom_hashes():
    digests = parse_api_digests()
    for fname, key, size in (("ibm.ch8", "IBM", 132),
                             ("tetris1.ch8", "TETRIS", 494)):
        data = (ROOT / "user" / "chip8" / "roms" / fname).read_bytes()
        assert len(data) == size
        actual = hashlib.sha256(data).hexdigest()
        assert actual == digests[key], f"{fname}: {actual} != {digests[key]}"


def ibm_leg():
    data = (ROOT / "user" / "chip8" / "roms" / "ibm.ch8").read_bytes()
    m = Chip8(data)
    m.run(IBM_STEPS)
    assert m.pc == 0x228, "IBM logo ends in the jump-to-self loop"
    assert m.sp == 0
    cells = [(cx, cy) for cy in range(GFX_ROWS)
             for cx in range(GFX_COLS) if m.gfx[cy * GFX_COLS + cx]]
    assert cells, "IBM logo must be visible"
    xs = [c[0] for c in cells]
    ys = [c[1] for c in cells]
    gfx_sha = hashlib.sha256(bytes(m.gfx)).hexdigest()
    frame = m.render()
    frame_sha = hashlib.sha256(frame).hexdigest()
    probes = {
        "rom": "ibm.ch8",
        "rom_sha256": hashlib.sha256(data).hexdigest(),
        "steps": IBM_STEPS,
        "final_pc": hex(m.pc),
        "final_sp": m.sp,
        "final_i": hex(m.i),
        "final_v0": m.v[0],
        "nonzero_cells": len(cells),
        "bbox": {"x0": min(xs), "y0": min(ys),
                 "x1": max(xs), "y1": max(ys)},
        "gfx_sha256": gfx_sha,
        "frame_sha256": frame_sha,
        "frame_size": len(frame),
        "lit_pixel_dword": hex(COLOR_CELL),
        "sample_cells": cells[:8],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "ibm-expected-gfx.bin").write_bytes(bytes(m.gfx))
    (OUT / "ibm-expected-frame.bin").write_bytes(frame)
    (OUT / "ibm-probes.json").write_text(json.dumps(probes, indent=2) + "\n")
    return probes


def tetris_zero():
    data = (ROOT / "user" / "chip8" / "roms" / "tetris1.ch8").read_bytes()
    m = Chip8(data)
    assert sum(m.gfx) == 0
    frame = m.render()
    assert frame == bytes(FB_WIDTH * FB_HEIGHT * 4)
    (OUT / "tetris-zero-frame.bin").write_bytes(frame)
    return hashlib.sha256(frame).hexdigest()


# ------------------------------------------------------------------
# task 141 W1: the interactive tetris leg, fixed-budget round replay
#
# One round mirrors user/chip8/chip8.asm .main_loop exactly: drain keys
# (level semantics), POLL, execute IPS_PER_TICK instructions, add
# TIMER_HZ units to the 60 Hz accumulator and drain it by
# TIMER_ACC_BASE (decrement delay/sound when non-zero), then present
# (render + BLIT) iff the draw flag was raised. The frozen POLL r8d
# snapshot is never consumed: the budget is fixed, not a tick delta.
# ------------------------------------------------------------------

IPS_PER_TICK = 5
TIMER_HZ = 60
TIMER_ACC_BASE = 100
# round boundaries derived from the ROM and frozen for the QEMU leg:
#   K1: after 20 rounds the intro animation has 24 lit cells (non-black)
#   K2: after 67 rounds PC sits at the key loop 0x23C with delay 10
#   key 7 (scancode 0x08) make before round 68, break before round 69;
#   the key leg last presents at round 70, the no-key leg at round 59,
#   so the screen at the round-71 boundary differs between the two
INT_K1_ROUNDS = 20
INT_K1_CELLS = 24
INT_K2_ROUNDS = 67
INT_K2_PC = 0x23C
INT_K2_DELAY = 10
INT_KEY_INDEX = 7
INT_KEY_MAKE_ROUND = 68
INT_KEY_BREAK_ROUND = 69
INT_CAPTURE_ROUNDS = 71
INT_KEY_LAST_PRESENT = 70
INT_NOKEY_LAST_PRESENT = 59


def interactive_round(m, acc):
    """Execute one fixed-budget round; return (acc, drew)."""
    for _ in range(IPS_PER_TICK):
        m.step()
    acc += TIMER_HZ
    while acc >= TIMER_ACC_BASE:
        acc -= TIMER_ACC_BASE
        if m.delay:
            m.delay -= 1
        if m.sound:
            m.sound -= 1
    drew = bool(m.draw)
    m.draw = 0
    return acc, drew


def tetris_interactive():
    data = (ROOT / "user" / "chip8" / "roms" / "tetris1.ch8").read_bytes()

    def replay(upto, key_tap):
        m = Chip8(data)
        acc = 0
        last_gfx = bytes(GFX_SIZE)
        last_present = 0
        for r in range(1, upto + 1):
            if key_tap and r == INT_KEY_MAKE_ROUND:
                m.key[INT_KEY_INDEX] = 1
            if key_tap and r == INT_KEY_BREAK_ROUND:
                m.key[INT_KEY_INDEX] = 0
            acc, drew = interactive_round(m, acc)
            if drew:
                last_gfx = bytes(m.gfx)
                last_present = r
        return m, last_gfx, last_present

    # K1: the intro animation is visible (non-black) after 20 rounds
    m1, _, _ = replay(INT_K1_ROUNDS, False)
    cells1 = sum(m1.gfx)
    assert cells1 == INT_K1_CELLS, f"K1 cells {cells1} != {INT_K1_CELLS}"
    assert m1.pc != ROM_LOAD_ADDR, "tetris must step after Enter"
    gfx1 = bytes(m1.gfx)
    frame1 = m1.render()
    assert frame1 != bytes(FB_WIDTH * FB_HEIGHT * 4), "K1 frame not all black"

    # K2: the key loop is reached with the frozen delay timer value
    m2, _, _ = replay(INT_K2_ROUNDS, False)
    assert m2.pc == INT_K2_PC, f"K2 pc {m2.pc:#x} != {INT_K2_PC:#x}"
    assert m2.delay == INT_K2_DELAY, f"K2 delay {m2.delay}"

    # the key tap moves a piece: the presented screen diverges
    mk, gfxk, present_k = replay(INT_CAPTURE_ROUNDS, True)
    mn, gfxn, present_n = replay(INT_CAPTURE_ROUNDS, False)
    assert present_k == INT_KEY_LAST_PRESENT, present_k
    assert present_n == INT_NOKEY_LAST_PRESENT, present_n
    assert gfxk != gfxn, "key 7 tap must change the presented screen"
    framek = mk.render()
    framen = mn.render()
    assert framek != framen
    # the capture boundary is past the last present, so the live gfx
    # equals the last presented gfx (no draw between present and capture)
    assert bytes(mk.gfx) == gfxk and bytes(mn.gfx) == gfxn

    probes = {
        "rom": "tetris1.ch8",
        "rom_sha256": hashlib.sha256(data).hexdigest(),
        "ips_per_tick": IPS_PER_TICK,
        "timer_hz": TIMER_HZ,
        "timer_acc_base": TIMER_ACC_BASE,
        "k1_rounds": INT_K1_ROUNDS,
        "k1_nonzero_cells": cells1,
        "k1_gfx_sha256": hashlib.sha256(gfx1).hexdigest(),
        "k1_frame_sha256": hashlib.sha256(frame1).hexdigest(),
        "k2_rounds": INT_K2_ROUNDS,
        "k2_pc": hex(INT_K2_PC),
        "k2_delay": INT_K2_DELAY,
        "key_index": INT_KEY_INDEX,
        "key_make_round": INT_KEY_MAKE_ROUND,
        "key_break_round": INT_KEY_BREAK_ROUND,
        "capture_rounds": INT_CAPTURE_ROUNDS,
        "key_last_present": present_k,
        "nokey_last_present": present_n,
        "key_gfx_sha256": hashlib.sha256(gfxk).hexdigest(),
        "key_frame_sha256": hashlib.sha256(framek).hexdigest(),
        "nokey_gfx_sha256": hashlib.sha256(gfxn).hexdigest(),
        "nokey_frame_sha256": hashlib.sha256(framen).hexdigest(),
        "frame_size": len(frame1),
        "lit_pixel_dword": hex(COLOR_CELL),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tetris-int-gfx-20.bin").write_bytes(gfx1)
    (OUT / "tetris-int-frame-20.bin").write_bytes(frame1)
    (OUT / "tetris-int-gfx-71-key.bin").write_bytes(gfxk)
    (OUT / "tetris-int-frame-71-key.bin").write_bytes(framek)
    (OUT / "tetris-int-gfx-71-nokey.bin").write_bytes(gfxn)
    (OUT / "tetris-int-frame-71-nokey.bin").write_bytes(framen)
    (OUT / "tetris-int-probes.json").write_text(
        json.dumps(probes, indent=2) + "\n")
    return probes


TESTS = [
    ("DXYN collision + XOR toggle", test_dxyn_collision),
    ("DXYN clipping, no wrap, VF on drawn pixels only", test_dxyn_clip),
    ("8xy5/8xy7 VF no-borrow fix", test_sub_vf),
    ("8xy6/8xyE in-place shifts", test_shifts_in_place),
    ("FX33 BCD", test_bcd),
    ("FX55/FX65 I += x+1", test_fx55_fx65_i_increment),
    ("Ex9E/ExA1 level semantics", test_key_skips),
    ("Fx0A no-key PC stall", test_fx0a),
    ("call/ret stack depth 16 bounds", test_stack_bounds),
    ("unknown opcodes exit 2", test_unknown_opcodes),
    ("Bnnn V0 / FX29 font / Cxkk LCG", test_bnnn_fx29_rand),
    ("Fx07/Fx15/Fx18 timer registers", test_timers_regs),
    ("ROM sha256 vs api.inc constants", test_rom_hashes),
]


def main():
    lines = []

    def log(msg):
        print(msg)
        lines.append(msg)

    failed = 0
    for name, fn in TESTS:
        try:
            fn()
            log(f"PASS  {name}")
        except AssertionError as e:
            failed += 1
            log(f"FAIL  {name}: {e}")
    probes = ibm_leg()
    log("PASS  IBM 256-step leg")
    log("IBM probes: " + json.dumps(probes))
    zero_sha = tetris_zero()
    log(f"PASS  tetris1 zero-step frame (all black), sha256={zero_sha}")
    int_probes = tetris_interactive()
    log("PASS  tetris interactive fixed-budget leg (task 141 W1)")
    log("tetris interactive probes: " + json.dumps(int_probes))
    if failed:
        log(f"MODEL SELFTEST FAILED ({failed} failures)")
        rc = 1
    else:
        log(f"MODEL SELFTEST PASSED ({len(TESTS) + 3} checks)")
        rc = 0
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "model-selftest.log").write_text("\n".join(lines) + "\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())