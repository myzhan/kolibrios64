#!/usr/bin/env python3
"""Reusable QEMU boot-debug workflow for kolibrios64 (Darwin arm64, TCG).

Builds with build_fasm2.sh into a private per-session artifact directory,
assembles an ESP (EFI/BOOT/BOOTX64.EFI, EFI/KERNEL64.BIN, startup.nsh) and
starts QEMU with the repository OVMF.fd, a vvfat ESP, COM1 captured to
serial.log and a gdbstub bound to 127.0.0.1 only.

Modes:
  default        debug mode: QEMU starts frozen (-S); attach lldb or gdb,
                 set a hardware breakpoint at the kernel entry VA (computed
                 from KERNEL64.BIN header offset 8 of THIS build) and
                 continue. Ctrl-C stops QEMU and cleans up.
  --no-freeze    smoke mode: no -S, bounded by --timeout; waits for a
                 TERMINAL marker (K64-FOUNDATION-OK or K64-CPU-PANIC),
                 not the first K64-BOOT-OK. PASS requires all ten OK
                 markers (BOOT/CPU/PMM/VMM/HEAP/IRQ/INPUT/GUI/DESKTOP/
                 FOUNDATION) exactly once and in order, no K64-CPU-PANIC /
                 K64-BOOT-FAIL, no triple fault or unexpected reset, and
                 the frozen task-60 verdict values: CPU selftest passed =
                 0x5FF (parsed from the serial report line) plus mm_mask =
                 0, hook_hits = 3, core_mask = 0, the IRQ-v2 verdict values
                 irq_mask = 0, ticks = 11 (the desktop loop masks the LVT
                 timer, the count stays at the input exit value), the
                 input verdict values input mask = 0, kbd_count = 6,
                 mouse_count = 6, stage = 6 (the desktop injection adds
                 make+break of h and i plus move/press/release on top of
                 the input exit values 2/3), the GUI-G1 verdict value
                 video_selftest_mask = 0 and the DESKTOP-v1 verdict values
                 desktop mask = 0, stage = 5, keys = 2, cursor = (557,411).
                 The kernel never prints those counters on the success
                 path, so they are read from guest memory through the
                 gdbstub after the terminal marker (task 60 used the same
                 channel). While waiting, the smoke loop drives the IRQ-v2
                 input self test handshake and then the DESKTOP-v1 shell
                 handshake: it polls input_test_stage / desktop_test_stage
                 through the gdbstub and injects sendkey / mouse_move /
                 mouse_button through the monitor socket, once per stage,
                 all bounded. THREE screendumps are captured:
                 screen-gui.ppm inside the bounded GUI frame handshake
                 window (the foundation publishes READY in
                 desktop_gui_frame_stage after K64-GUI-OK and spins a
                 pause budget; the host screendumps the still pristine
                 GUI-G1 pattern frame and writes RELEASE through the
                 gdbstub 'M' packet; a timeout is a guest EIO panic,
                 never a hang), screen-echo.ppm when the echo leg
                 finished (desktop stage 3, before the mouse move:
                 background/title/cursor at (517,387)/"hi") and
                 screen.ppm after the terminal marker (cursor moved to
                 (557,411), the old position restored to the background).
                 All three are judged in pure Python against the frozen
                 pattern/desktop frame probe specs. The -d int log is
                 judged too: the
                 v=0x21/v=0x2c delivery counts must equal the values
                 deterministically derived from the WHOLE injection chain
                 (one delivery per 8042 byte). Any parse/read failure,
                 or a timeout without a terminal marker, is a FAIL with
                 the reason printed.
  --gui-skip     expect the non-fatal GUI skip path instead: the eight
                 non-GUI/non-desktop OK markers plus exactly one
                 K64-GUI-SKIP and one K64-DESKTOP-SKIP between INPUT and
                 FOUNDATION, no K64-GUI-OK / K64-DESKTOP-OK, no pixel
                 assertions; the input counters stay at their input-exit
                 values (2/3) and the desktop counters at zero, the
                 vector counts at the input-only derivation (2/9);
                 video_fb_state is read through the gdbstub as geometry
                 evidence. Used with a loader built with GOP_MAX_MODES=0.

Nothing outside the artifact directory is written; build.sh, build_fasm2.sh
and the kernel/loader sources are never modified. QEMU runs in its own
session and is killed on exit/interrupt, so no stray process survives.

Minimum kernel CPU features: long mode (x86_64), NX (EFER.NXE) and POPCNT.
The plain TCG "qemu64" model lacks POPCNT, which the MM module uses, so the
default CPU is "qemu64,+popcnt" in BOTH debug and smoke mode; --cpu overrides
it for experiments (e.g. a host-passthrough model or extra feature flags).

Usage:
  run-qemu-debug.py [--no-freeze] [--gui-skip] [--mem MIB] [--port N]
                    [--timeout SEC] [--art-dir DIR] [--qemu PATH]
                    [--cpu MODEL]

Environment: FASMG / FASM2_DIR / FASM1 are forwarded to build_fasm2.sh.
"""

import argparse
import atexit
import datetime
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# the ten milestone markers, in their fixed emission order; IRQ-v2
# INPUT sits between IRQ and GUI, GUI-G1 between INPUT and DESKTOP,
# DESKTOP-v1 between GUI and FOUNDATION (foundation.inc emits them
# after core_selftest)
MARKERS = ("K64-BOOT-OK", "K64-CPU-OK", "K64-PMM-OK", "K64-VMM-OK",
           "K64-HEAP-OK", "K64-IRQ-OK", "K64-INPUT-OK", "K64-GUI-OK",
           "K64-DESKTOP-OK", "K64-FOUNDATION-OK")
GUI_OK_MARKER = "K64-GUI-OK"
GUI_SKIP_MARKER = "K64-GUI-SKIP"
DESKTOP_OK_MARKER = "K64-DESKTOP-OK"
DESKTOP_SKIP_MARKER = "K64-DESKTOP-SKIP"
TERMINAL_MARKERS = ("K64-FOUNDATION-OK", "K64-CPU-PANIC")
FORBIDDEN_MARKERS = ("K64-CPU-PANIC", "K64-BOOT-FAIL")

# frozen screendump spec: 1024x768. THREE captures are judged:
#   screen-gui.ppm   the GUI-G1 pattern frame, captured inside the
#                    bounded foundation handshake window after
#                    K64-GUI-OK, BEFORE desktop_init repaints it
#   screen-echo.ppm  after the echo leg (desktop stage 3, before the
#                    mouse move): cursor at the initial (517,387)
#   screen.ppm       after the terminal marker: cursor at (557,411),
#                    the old position restored to the background
# The GUI probes are the frozen task-77 pattern spec, the same 28 hand
# written pixels as the stage2-fb spec model. Common desktop probes
# hold in BOTH desktop frames: deep blue background 0x1A2B3C,
# title bar 0x2C3E50 (0,0,1024,32), "K64 Desktop v0.1" at (8,8) in
# white on the bar, the echoed "hi" at (16,64) in white on the blue.
PPM_WIDTH, PPM_HEIGHT = 1024, 768
DESKTOP_BG = 0x1A2B3C
DESKTOP_BAR = 0x2C3E50
# the GUI-G1 pattern frame: 8 color bars x 128 px, white border, the
# centered 200x200 gray rectangle at (412,284), "K64 GUI-G1" at (8,8)
PPM_GUI_PROBES = (
    (64, 100, 0xFF0000),                # bar 0 center is red
    (192, 100, 0x00FF00),               # bar 1 center is green
    (320, 100, 0x0000FF),               # bar 2 center is blue
    (448, 100, 0xFFFF00),               # bar 3 center is yellow
    (576, 100, 0x00FFFF),               # bar 4 center is cyan
    (704, 100, 0xFF00FF),               # bar 5 center is magenta
    (832, 100, 0xFFFFFF),               # bar 6 center is white
    (960, 100, 0x000000),               # bar 7 center is black
    (127, 100, 0xFF0000),               # bar 0/1 boundary left is red
    (128, 100, 0x00FF00),               # bar 0/1 boundary right is green
    (895, 100, 0xFFFFFF),               # bar 6/7 boundary left is white
    (896, 100, 0x000000),               # bar 6/7 boundary right is black
    (0, 0, 0xFFFFFF),                   # top left corner is border white
    (1023, 767, 0xFFFFFF),              # bottom right corner is border white
    (512, 0, 0xFFFFFF),                 # top edge is border white
    (0, 384, 0xFFFFFF),                 # left edge is border white
    (1023, 100, 0xFFFFFF),              # right edge is border white
    (1, 1, 0xFF0000),                   # inside the border bar 0 red shows
    (412, 284, 0x808080),               # rectangle top left is gray
    (611, 483, 0x808080),               # rectangle bottom right is gray
    (411, 284, 0xFFFF00),               # left of the rectangle bar 3 yellow
    (612, 284, 0x00FFFF),               # right of the rectangle bar 4 cyan
    (412, 283, 0xFFFF00),               # above the rectangle bar 3 yellow
    (412, 484, 0xFFFF00),               # below the rectangle bar 3 yellow
    (9, 10, 0xFFFFFF),                  # 'K' glyph row 2 left stroke lit
    (14, 10, 0xFFFFFF),                 # 'K' glyph row 2 right stroke lit
    (8, 10, 0xFF0000),                  # 'K' glyph row 2 edge not lit
    (8, 8, 0xFF0000),                   # 'K' glyph row 0 is empty
)
PPM_COMMON_PROBES = (
    (900, 700, DESKTOP_BG),             # background, far from everything
    (16, 767, DESKTOP_BG),              # background, bottom left corner
    (500, 16, DESKTOP_BAR),             # title bar fill
    (500, 31, DESKTOP_BAR),             # title bar last row
    (500, 32, DESKTOP_BG),              # first row below the bar
    (9, 10, 0xFFFFFF), (14, 10, 0xFFFFFF),   # 'K' glyph row 2 lit
    (8, 10, DESKTOP_BAR), (8, 8, DESKTOP_BAR),  # 'K' glyph off pixels
    (18, 69, 0xFFFFFF),                 # 'h' row 5 col 2 lit
    (16, 69, DESKTOP_BG),               # 'h' row 5 col 0 off
    (22, 66, 0xFFFFFF),                 # 'h' row 2 col 6 lit
    (27, 66, 0xFFFFFF),                 # 'i' row 2 col 3 lit
    (25, 75, 0xFFFFFF),                 # 'i' row 11 base lit
    (24, 66, DESKTOP_BG),               # 'i' row 2 col 0 off
)
# the echo frame: the cursor sprite at the frozen initial (517,387)
PPM_ECHO_PROBES = (
    (517, 387, 0x000000),               # sprite row 0 col 0 outline
    (519, 392, 0xFFFFFF),               # sprite row 5 col 2 white fill
    (528, 387, DESKTOP_BG),             # sprite row 0 col 11 transparent
)
# the final frame: the cursor moved to (557,411), the old position
# restored to the background pixel for pixel
PPM_FINAL_PROBES = (
    (557, 411, 0x000000),               # sprite row 0 col 0 outline
    (559, 416, 0xFFFFFF),               # sprite row 5 col 2 white fill
    (562, 416, 0xFFFFFF),               # sprite row 5 col 5 white fill
    (568, 411, DESKTOP_BG),             # sprite row 0 col 11 transparent
    (517, 387, DESKTOP_BG),             # old outline pixel restored
    (519, 392, DESKTOP_BG),             # old white fill pixel restored
    (528, 397, DESKTOP_BG),             # old row 10 outline pixel restored
)

# frozen task-60 verdict values. passed comes from the serial report line;
# the counters are memory-only on the success path, so their VAs are
# resolved via a private ELF wrapper and read through the gdbstub 'm' packet
COUNTER_SYMBOLS = (("mm_test_selftest_mask", 8), ("mm_test_fail_hits", 4),
                   ("core_selftest_mask", 8),
                   ("irq_test_selftest_mask", 8), ("irq_test_ticks", 8),
                   ("input_test_selftest_mask", 8),
                   ("input_test_kbd_count", 8),
                   ("input_test_mouse_count", 8),
                   ("input_test_stage", 8),
                   ("video_selftest_mask", 8),
                   ("desktop_test_selftest_mask", 8),
                   ("desktop_test_stage", 8),
                   ("desktop_test_keys", 8),
                   ("desktop_test_cursor_x", 8),
                   ("desktop_test_cursor_y", 8),
                   ("desktop_gui_frame_stage", 8))
EXPECTED_PASSED = 0x5FF
# ok mode: the WHOLE injection chain (input self test + desktop self
# test). kbd = 2 + make/break of h and i = 6; mouse = 3 + move/press/
# release = 6; ticks stays 11 (the desktop loop masks the LVT timer);
# the desktop counters derive from the frozen contract (keys = 2, the
# cursor moves 517+40, 387+24).
EXPECTED_COUNTERS = {"mm_test_selftest_mask": 0, "mm_test_fail_hits": 3,
                     "core_selftest_mask": 0,
                     "irq_test_selftest_mask": 0, "irq_test_ticks": 11,
                     "input_test_selftest_mask": 0,
                     "input_test_kbd_count": 6,
                     "input_test_mouse_count": 6,
                     "input_test_stage": 6,
                     "video_selftest_mask": 0,
                     "desktop_test_selftest_mask": 0,
                     "desktop_test_stage": 5,
                     "desktop_test_keys": 2,
                     "desktop_test_cursor_x": 557,
                     "desktop_test_cursor_y": 411,
                     "desktop_gui_frame_stage": 3}
# skip mode: no GOP, no GUI, no desktop; the input counters stay at
# their input-exit values and every desktop counter stays zero
EXPECTED_COUNTERS_SKIP = dict(EXPECTED_COUNTERS,
                              input_test_kbd_count=2,
                              input_test_mouse_count=3,
                              desktop_test_stage=0,
                              desktop_test_keys=0,
                              desktop_test_cursor_x=0,
                              desktop_test_cursor_y=0,
                              desktop_gui_frame_stage=0)
# -d int vector delivery expectations, DETERMINISTICALLY derived from
# the whole injection chain (one delivery per 8042 byte, the task-94
# measured structure): ok mode v=0x21 = 2 (sendkey a) + 4 (h and i
# make+break) = 6, v=0x2c = 9 (input move/press/release, 3 bytes per
# packet) + 9 (desktop move/press/release) = 18. sendkey b is fully
# drained under the mask, it never delivers. skip mode: input only.
EXPECTED_VECTORS = {"ok": {"21": 6, "2c": 18},
                    "skip": {"21": 2, "2c": 9}}
PASSED_RE = re.compile(r"K64-CPU-SELFTEST passed = (0x[0-9A-Fa-f]+)")

# the IRQ-v2 host injection script, one monitor command per handshake
# stage (input/api.inc INPUT_STAGE_*). Stage 4 is special: the press is
# sent first, the release only after the guest consumed the press packet
# (mouse_count >= 2), so QEMU can never coalesce press+release into one
# packet. Stage 5 (timer) and 6 (done) need no host action.
INPUT_STAGE_INJECT = {1: "sendkey b", 2: "sendkey a", 3: "mouse_move 5 3"}

# the DESKTOP-v1 host injection script, one monitor command per
# handshake stage (desktop/api.inc DESKTOP_STAGE_*), polled only after
# K64-GUI-OK. Stage 3 is special: the echo frame screendump is captured
# FIRST (cursor still at the initial position), then the move is sent.
# Stage 4 splits press/release: the release only after the guest
# consumed the press packet (mouse_count >= 5 = input exit 3 + move 1
# + press 1), so QEMU can never coalesce press+release. Stage 5 (done)
# needs no host action.
DESKTOP_STAGE_INJECT = {1: "sendkey h", 2: "sendkey i", 3: "mouse_move 40 24"}

# the GUI frame forensic handshake stages (core/contracts.inc
# K64_GUI_FRAME_*): after K64-GUI-OK the guest publishes READY (1) in
# desktop_gui_frame_stage and spins a bounded pause window; the host
# captures screen-gui.ppm and writes RELEASE (2), the guest marks
# DONE (3) and only then lets desktop_init repaint the pattern frame
GUI_FRAME_READY = 1
GUI_FRAME_RELEASE = 2

# forensic evidence for a failed input handshake: the raw mouse packet
# state, read through the gdbstub while the panicked kernel still lives
INPUT_FORENSIC_SYMBOLS = (("input_test_mouse_last", 8),
                          ("input_mouse_x", 8), ("input_mouse_y", 8),
                          ("input_mouse_bad", 8), ("input_mouse_state", 8),
                          ("input_mouse_b1", 8), ("input_mouse_b2", 8))

_qemu_proc = None


def cleanup():
    """Kill the QEMU session on any exit path; never leave a stray QEMU."""
    global _qemu_proc
    p = _qemu_proc
    if p is None or p.poll() is not None:
        return
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()
    print(f"run-qemu-debug: QEMU (pid {p.pid}) stopped and cleaned up")


def on_signal(signum, frame):
    # Never wait/poll the child from the handler: the main thread may sit
    # in Popen.wait() holding subprocess's internal waitpid lock, and any
    # wait/poll here would deadlock on it. Only kill the process group;
    # the atexit cleanup reaps and escalates once the lock is released.
    if _qemu_proc is not None:
        try:
            os.killpg(_qemu_proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    sys.exit(128 + signum)


def build(art):
    bin_dir = os.path.join(art, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    r = subprocess.run(["sh", os.path.join(REPO, "build_fasm2.sh")],
                       env=dict(os.environ, OUTDIR=bin_dir), cwd=REPO)
    if r.returncode != 0:
        print(f"run-qemu-debug: build_fasm2.sh failed (exit {r.returncode})",
              file=sys.stderr)
        sys.exit(1)
    return bin_dir


def entry_va(bin_path):
    with open(bin_path, "rb") as f:
        header = f.read(40)
    if len(header) < 40 or header[:8] != b"KERNEL64":
        print(f"run-qemu-debug: {bin_path}: bad KERNEL64 header",
              file=sys.stderr)
        sys.exit(1)
    off, = struct.unpack_from("<Q", header, 8)
    # read the base from the source of truth, not a hardcoded copy
    with open(os.path.join(REPO, "kernel", "const.inc"),
              encoding="utf-8") as f:
        m = re.search(r"^\s*VIRT_KERNEL_BASE\s*=\s*(0x[0-9A-Fa-f]+|\d+)",
                      f.read(), re.M)
    if not m:
        print("run-qemu-debug: VIRT_KERNEL_BASE not found in const.inc",
              file=sys.stderr)
        sys.exit(1)
    return int(m.group(1), 0) + off, off


def make_esp(art, bin_dir):
    esp = os.path.join(art, "esp")
    os.makedirs(os.path.join(esp, "EFI", "BOOT"), exist_ok=True)
    shutil.copyfile(os.path.join(bin_dir, "BOOTX64.EFI"),
                    os.path.join(esp, "EFI", "BOOT", "BOOTX64.EFI"))
    shutil.copyfile(os.path.join(bin_dir, "KERNEL64.BIN"),
                    os.path.join(esp, "EFI", "KERNEL64.BIN"))
    with open(os.path.join(esp, "startup.nsh"), "w", encoding="ascii") as f:
        f.write("fs0:\\EFI\\BOOT\\BOOTX64.EFI\n")
    return esp


def gen_symbols(art, bin_dir):
    elf = os.path.join(art, "kernel64.elf")
    r = subprocess.run(
        [sys.executable, os.path.join(REPO, "tools", "gen-kernel-symbols.py"),
         "--bin", os.path.join(bin_dir, "KERNEL64.BIN"), "--out", elf],
        cwd=REPO)
    if r.returncode != 0:
        print("run-qemu-debug: WARNING: symbol ELF failed validation, "
              "falling back to raw-address debugging (hbreak *<entry VA>)",
              file=sys.stderr)
        return None
    return elf


def counter_addresses(art, extra=()):
    """Resolve the counter VAs; returns ({name: VA}, error strings).

    `extra` adds (name, size) pairs to the same wrapper, e.g. the 64-byte
    video_fb_state block read as geometry evidence in --gui-skip mode.

    tools/kernel64-elf.asm only publishes the entry/console labels, so a
    private wrapper is generated INSIDE the artifact directory: the same
    `format elf64` + absolute-org trick turns the added public labels into
    SHN_ABS symbols carrying the full runtime addresses. No repo file is
    touched.
    """
    fasm2_dir = os.path.abspath(os.environ.get(
        "FASM2_DIR", os.path.join(REPO, "..", "fasm2")))
    host = os.environ.get("FASMG") or os.path.join(
        fasm2_dir, "build", "fasm2-bootstrap", "fasmg-macos-x64")
    if not os.access(host, os.X_OK):
        return {}, [f"fasmg host {host} not executable"]
    wrapper = os.path.join(art, "kernel64-counters.asm")
    elf_path = os.path.join(art, "kernel64-counters.elf")
    symbols = COUNTER_SYMBOLS + tuple(extra)
    with open(wrapper, "w", encoding="utf-8") as f:
        f.write("format elf64\n")
        f.write("include '%s'\n" % os.path.join(REPO, "kernel", "kernel64.asm"))
        for name, _ in symbols:
            f.write("public %s\n" % name)
    r = subprocess.run(
        [host, "-e100", "-iInclude 'fasm2.inc'", wrapper, elf_path],
        env=dict(os.environ, INCLUDE=os.path.join(fasm2_dir, "include")),
        cwd=REPO, capture_output=True, text=True)
    if r.returncode != 0:
        return {}, ["counter wrapper assembly failed: "
                    + (r.stderr or r.stdout).strip()]
    with open(elf_path, "rb") as f:
        elf = f.read()
    if elf[:6] != b"\x7fELF\x02\x01":
        return {}, ["counter wrapper output is not a little-endian ELF64"]
    e_shoff, = struct.unpack_from("<Q", elf, 0x28)
    e_shentsize, e_shnum, _ = struct.unpack_from("<HHH", elf, 0x3A)
    shdrs = [struct.unpack_from("<IIQQQQIIQQ", elf, e_shoff + i * e_shentsize)
             for i in range(e_shnum)]
    symtab = next((s for s in shdrs if s[1] == 2), None)  # SHT_SYMTAB
    if symtab is None:
        return {}, ["counter wrapper ELF has no .symtab"]
    strtab = shdrs[symtab[6]]
    strs = elf[strtab[4]:strtab[4] + strtab[5]]
    wanted = {name for name, _ in symbols}
    addrs, errors = {}, []
    for i in range(symtab[5] // 24):
        st_name, _, _, st_shndx, st_value, _ = struct.unpack_from(
            "<IBBHQQ", elf, symtab[4] + i * 24)
        nm = strs[st_name:strs.index(b"\0", st_name)].decode()
        if nm in wanted:
            if st_shndx != 0xFFF1:  # SHN_ABS: absolute org survived
                errors.append(f"counter symbol {nm} is not SHN_ABS")
            else:
                addrs[nm] = st_value
    for nm in sorted(wanted - set(addrs)):
        if not any(nm in e for e in errors):
            errors.append(f"counter symbol {nm} missing from wrapper .symtab")
    return addrs, errors


def gdb_read_mem(port, addr, size, timeout=5.0):
    """One little-endian guest memory read via the gdbstub 'm' packet.

    Connecting pauses the VM; after a terminal marker the kernel sits in
    cli/hlt, so the counters are stable. Stop replies ('T'/'S') and console
    ('O') packets may precede the read reply and are skipped. A detach
    ('D') resumes the guest; it is sent on EVERY exit path, because a
    bare socket close after an 'E' error reply would leave the VM paused
    forever (the early-boot stage polling hits unmapped addresses).
    """
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.settimeout(timeout)
        try:
            body = ("m%x,%x" % (addr, size)).encode()
            s.sendall(b"$" + body + b"#%02x" % (sum(body) & 0xFF))
            buf = b""
            deadline = time.monotonic() + timeout
            value = None
            while time.monotonic() < deadline:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while True:
                    m = re.search(rb"\$([^#]*)#([0-9a-fA-F]{2})", buf)
                    if not m:
                        break
                    payload = m.group(1)
                    buf = buf[m.end():]
                    if (len(payload) == 2 * size
                            and re.fullmatch(rb"[0-9a-fA-F]+", payload)):
                        value = int.from_bytes(bytes.fromhex(payload.decode()),
                                               "little")
                        break
                    if payload[:1] == b"E":
                        raise OSError(f"gdbstub error reply {payload.decode()!r} "
                                      f"for m{addr:x},{size:x}")
                if value is not None:
                    break
            if value is None:
                raise OSError(f"gdbstub read m{addr:x},{size:x}: no reply "
                              "before timeout")
            return value
        finally:
            try:
                s.sendall(b"$D#44")
            except OSError:
                pass


def gdb_write_mem(port, addr, value, size, timeout=5.0):
    """One little-endian guest memory write via the gdbstub 'M' packet.

    Used by the GUI frame handshake to release the bounded guest spin
    window after screen-gui.ppm was captured. Same detach discipline
    as gdb_read_mem: 'D' is sent on EVERY exit path so the VM never
    stays paused after an error reply.
    """
    data = value.to_bytes(size, "little").hex()
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.settimeout(timeout)
        try:
            body = ("M%x,%x:%s" % (addr, size, data)).encode()
            s.sendall(b"$" + body + b"#%02x" % (sum(body) & 0xFF))
            buf = b""
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while True:
                    m = re.search(rb"\$([^#]*)#([0-9a-fA-F]{2})", buf)
                    if not m:
                        break
                    payload = m.group(1)
                    buf = buf[m.end():]
                    if payload == b"OK":
                        return
                    if payload[:1] == b"E":
                        raise OSError(f"gdbstub error reply {payload.decode()!r} "
                                      f"for M{addr:x},{size:x}")
            raise OSError(f"gdbstub write M{addr:x},{size:x}: no OK reply "
                          "before timeout")
        finally:
            try:
                s.sendall(b"$D#44")
            except OSError:
                pass


def read_counters(port, addrs):
    counters, errors = {}, []
    for name, size in COUNTER_SYMBOLS:
        if name not in addrs:
            continue  # address resolution errors are reported separately
        try:
            counters[name] = gdb_read_mem(port, addrs[name], size)
        except OSError as e:
            errors.append(f"counter {name}: {e}")
    return counters, errors


def screendump(art, timeout=15.0, name="screen.ppm"):
    """Capture the guest screen through the unix-socket QEMU monitor.

    Returns the raw PPM bytes; the named file stays in the artifact
    directory as forensic evidence (screen-echo.ppm for the echo frame,
    screen.ppm for the final frame). The command completes before the
    next (qemu) prompt is printed, so the file is whole when the prompt
    arrives.
    """
    sock_path = os.path.join(art, "monitor.sock")
    ppm_path = os.path.join(art, name)
    if os.path.exists(ppm_path):
        os.unlink(ppm_path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(sock_path)
        buf = b""

        def wait_prompt():
            nonlocal buf
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if b"(qemu)" in buf:
                    return
                chunk = s.recv(4096)
                if not chunk:
                    raise OSError("monitor socket closed before prompt")
                buf += chunk
            raise OSError("no (qemu) prompt before timeout")

        wait_prompt()  # the monitor greeting ends with the first prompt
        buf = b""
        s.sendall(("screendump " + ppm_path + "\n").encode())
        wait_prompt()
    with open(ppm_path, "rb") as f:
        return f.read()


def monitor_command(art, cmd, timeout=10.0):
    """Send one HMP command through the unix-socket monitor, bounded.

    Same prompt discipline as screendump(): the greeting prompt is
    consumed first, the command has completed when the next (qemu)
    prompt arrives. Returns the raw reply text as injection evidence.
    """
    sock_path = os.path.join(art, "monitor.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(sock_path)
        buf = b""

        def wait_prompt():
            nonlocal buf
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if b"(qemu)" in buf:
                    return
                chunk = s.recv(4096)
                if not chunk:
                    raise OSError("monitor socket closed before prompt")
                buf += chunk
            raise OSError("no (qemu) prompt before timeout")

        wait_prompt()
        buf = b""
        s.sendall((cmd + "\n").encode())
        wait_prompt()
    return buf.decode(errors="replace").strip()


def parse_ppm(data):
    """Pure-Python PPM P6 parser -> (width, height, rgb bytes).

    No PIL or any other dependency: header tokens are read with comment
    skipping, exactly one whitespace byte separates maxval from the
    binary payload, maxval must be 255. Raises ValueError on any defect.
    """
    if not data.startswith(b"P6"):
        raise ValueError("not a P6 PPM")
    pos = 2
    tokens = []
    while len(tokens) < 3:
        while pos < len(data):
            c = data[pos:pos + 1]
            if c in b" \t\r\n":
                pos += 1
            elif c == b"#":
                nl = data.find(b"\n", pos)
                if nl < 0:
                    raise ValueError("unterminated PPM comment")
                pos = nl + 1
            else:
                break
        start = pos
        while pos < len(data) and data[pos:pos + 1] not in b" \t\r\n#":
            pos += 1
        if pos == start:
            raise ValueError("truncated PPM header")
        tokens.append(int(data[start:pos]))
    w, h, maxval = tokens
    if w <= 0 or h <= 0 or maxval != 255:
        raise ValueError(f"unsupported PPM header {w}x{h} maxval {maxval}")
    pos += 1  # the single whitespace byte after maxval
    px = data[pos:]
    if len(px) != w * h * 3:
        raise ValueError(f"PPM payload {len(px)} bytes, need {w * h * 3}")
    return w, h, px


def judge(serial, debug, counters, early_exit, gui_mode="ok", ppm=None,
          ppm_echo=None, ppm_gui=None):
    """Pure smoke verdict: (ok, report lines). No I/O in here, so the
    decision can be exercised unit-style against mutated serial/debug
    fixtures (the task-67 sensitivity evidence).

    gui_mode="ok"   the default ten-marker PASS path plus the frozen
                    frame PPM pixel assertions on THREE captures:
                    `ppm_gui` (the GUI-G1 pattern frame, captured in
                    the bounded handshake window before the desktop
                    repaint), `ppm_echo` (echo frame, cursor at the
                    initial position) and `ppm` (final frame, cursor
                    moved and the old position restored), raw PPM bytes.
    gui_mode="skip" the non-fatal GOP fallback path: eight OK markers,
                    exactly one K64-GUI-SKIP and one K64-DESKTOP-SKIP
                    between INPUT and FOUNDATION, no K64-GUI-OK /
                    K64-DESKTOP-OK, no pixel assertions. Never mixed
                    into the default PASS path.
    The -d int vector delivery counts (v=0x21/v=0x2c) are asserted in
    both modes against the deterministic injection-chain derivation.
    """
    lines = []
    ok = True

    def req(cond, label):
        nonlocal ok
        lines.append(f"  [{'ok' if cond else 'FAIL'}] {label}")
        if not cond:
            ok = False

    terminal = [t for t in TERMINAL_MARKERS if t in serial]
    req(bool(terminal),
        "terminal marker seen (K64-FOUNDATION-OK or K64-CPU-PANIC)")
    positions = []
    markers = (MARKERS if gui_mode == "ok"
               else tuple(m for m in MARKERS
                          if m not in (GUI_OK_MARKER, DESKTOP_OK_MARKER)))
    for m in markers:
        n = serial.count(m)
        req(n == 1, f"{m} count = {n} (need exactly 1)")
        positions.append(serial.find(m))
    ordered = (all(p >= 0 for p in positions)
               and positions == sorted(positions)
               and len(set(positions)) == len(positions))
    req(ordered, f"the {len(markers)} OK markers appear in order")
    for m in FORBIDDEN_MARKERS:
        req(m not in serial, f"no {m} in serial")
    req("Triple fault" not in debug, "no triple fault in qemu-debug.log")
    # -d cpu_reset logs the QEMU power-on reset AND one OVMF platform reset
    # during firmware init; anything beyond those two is unexpected
    resets = len(re.findall(r"CPU Reset", debug))
    req(resets <= 2, f"CPU Reset lines = {resets} (<=2: power-on + OVMF)")
    req(not early_exit, "QEMU did not exit early")

    matches = PASSED_RE.findall(serial)
    if len(matches) != 1:
        req(False, "CPU selftest passed line parse: "
                   f"{len(matches)} matches (need exactly 1)")
    else:
        passed = int(matches[0], 16)
        req(passed == EXPECTED_PASSED,
            f"CPU selftest passed = {passed:#x} (need {EXPECTED_PASSED:#x})")
    expected = EXPECTED_COUNTERS if gui_mode == "ok" else EXPECTED_COUNTERS_SKIP
    for name, _ in COUNTER_SYMBOLS:
        want = expected[name]
        if name not in counters:
            req(False, f"{name} unreadable (gdbstub memory read failed)")
        else:
            got = counters[name]
            req(got == want, f"{name} = {got:#x} (need {want:#x})")

    # -d int vector delivery evidence: one delivery per 8042 byte, the
    # expectation derives from the whole injection chain, never from a
    # runtime observation (the IRQ-v2 deferred item, landed by task 96)
    for vec, want in EXPECTED_VECTORS[gui_mode].items():
        got = len(re.findall(r"\bv=" + vec + r"\b", debug))
        req(got == want,
            f"-d int v={vec} deliveries = {got} (need {want}, derived)")

    def judge_ppm(raw, probes, label):
        if raw is None:
            req(False, f"{label} screendump PPM captured")
            return
        try:
            w, h, px = parse_ppm(raw)
        except ValueError as e:
            req(False, f"{label} PPM parse: {e}")
            return
        req((w, h) == (PPM_WIDTH, PPM_HEIGHT),
            f"{label} PPM size {w}x{h} (need {PPM_WIDTH}x{PPM_HEIGHT})")
        if (w, h) == (PPM_WIDTH, PPM_HEIGHT):
            for x, y, want in probes:
                off = (y * w + x) * 3
                got = (px[off] << 16) | (px[off + 1] << 8) | px[off + 2]
                req(got == want,
                    f"{label} pixel ({x},{y}) = {got:#08x} "
                    f"(need {want:#08x})")

    # DESKTOP-v1 verdict, kept separate from the shared checks above
    if gui_mode == "ok":
        req(GUI_SKIP_MARKER not in serial,
            "no K64-GUI-SKIP in serial (ok mode)")
        req(DESKTOP_SKIP_MARKER not in serial,
            "no K64-DESKTOP-SKIP in serial (ok mode)")
        judge_ppm(ppm_gui, PPM_GUI_PROBES, "gui")
        judge_ppm(ppm_echo, PPM_COMMON_PROBES + PPM_ECHO_PROBES, "echo")
        judge_ppm(ppm, PPM_COMMON_PROBES + PPM_FINAL_PROBES, "final")
    else:
        req(GUI_OK_MARKER not in serial,
            "no K64-GUI-OK in serial (skip mode)")
        req(DESKTOP_OK_MARKER not in serial,
            "no K64-DESKTOP-OK in serial (skip mode)")
        n = serial.count(GUI_SKIP_MARKER)
        req(n == 1, f"{GUI_SKIP_MARKER} count = {n} (need exactly 1)")
        n = serial.count(DESKTOP_SKIP_MARKER)
        req(n == 1, f"{DESKTOP_SKIP_MARKER} count = {n} (need exactly 1)")
        p_input = serial.find("K64-INPUT-OK")
        p_skip = serial.find(GUI_SKIP_MARKER)
        p_dskip = serial.find(DESKTOP_SKIP_MARKER)
        p_found = serial.find("K64-FOUNDATION-OK")
        req(p_input >= 0 and p_skip > p_input and p_found > p_skip,
            "K64-GUI-SKIP sits between K64-INPUT-OK and K64-FOUNDATION-OK")
        req(p_skip >= 0 and p_dskip > p_skip and p_found > p_dskip,
            "K64-DESKTOP-SKIP sits between K64-GUI-SKIP and "
            "K64-FOUNDATION-OK")
    return ok, lines


def qemu_argv(args, art, esp):
    # the kernel needs long mode, NX and POPCNT; bare "qemu64" has no
    # POPCNT and the MM module would take #UD, hence the +popcnt default
    argv = [args.qemu,
            "-accel", "tcg",
            "-m", str(args.mem),
            "-cpu", args.cpu,
            "-smp", "1",
            "-bios", os.path.join(REPO, "OVMF.fd"),
            "-drive", f"driver=vvfat,rw=on,dir={esp}",
            "-nic", "none",
            "-display", "none",
            "-monitor", ("unix:" + os.path.join(art, "monitor.sock")
                         + ",server=on,wait=off"),
            "-serial", "file:" + os.path.join(art, "serial.log"),
            "-d", "int,guest_errors,cpu_reset",
            "-D", os.path.join(art, "qemu-debug.log"),
            "-no-reboot", "-no-shutdown",
            "-gdb", f"tcp:127.0.0.1:{args.port}"]
    if not args.no_freeze:
        argv.append("-S")
    return argv


def print_guidance(args, art, va, off, elf):
    sym = elf if elf else os.path.join(art, "kernel64.elf")
    print()
    print("=" * 72)
    print("run-qemu-debug: QEMU frozen (-S), gdbstub on 127.0.0.1:%d"
          % args.port)
    print(f"  kernel entry VA = {va:#x} "
          f"(VIRT_KERNEL_BASE + header offset {off:#x}, from THIS build)")
    print(f"  serial log      : {os.path.join(art, 'serial.log')}")
    print(f"  qemu debug log  : {os.path.join(art, 'qemu-debug.log')}")
    print()
    print("  lldb (Apple /usr/bin/lldb, no install needed):")
    print(f"    target create {sym}")
    print("    # if the ELF is unavailable: settings set target.triple "
          "x86_64-unknown-elf")
    print(f"    gdb-remote 127.0.0.1:{args.port}")
    print(f"    breakpoint set -H -a {va:#x}   # hardware breakpoint (hbreak)")
    print("    continue")
    print(f"    disassemble -s $pc -c 10       # expect cli / cld / "
          "mov r15, rcx")
    print("    # ET_REL caveat: lldb leaves symbol-name breakpoints "
          "(-n halt_forever)")
    print("    # pending; use -a with the addresses printed by "
          "gen-kernel-symbols.py")
    print()
    print("  gdb (e.g. brew x86_64-elf-gdb, only if lldb misbehaves):")
    print("    set architecture i386:x86-64   # before target, avoids an "
          "oversized 'g' packet in long mode")
    if elf:
        print(f"    add-symbol-file {sym}")
    print(f"    target remote 127.0.0.1:{args.port}")
    print(f"    hbreak *{va:#x}")
    print("    continue")
    print("    x/10i $pc")
    print()
    print("  Notes: TCG has no watchpoints, use hardware breakpoints; with "
          "-S the")
    print("  serial log stays empty until you continue -- that is expected.")
    print("  Ctrl-C here stops QEMU and cleans up the session.")
    print("=" * 72)
    print()


def read_log(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def smoke(args, art, proc, counter_addrs, counter_errors):
    serial_path = os.path.join(art, "serial.log")
    deadline = time.monotonic() + args.timeout
    early_exit = False
    terminal_seen = False
    # IRQ-v2 injection handshake: poll input_test_stage through the
    # gdbstub, send one monitor command per stage, all bounded by the
    # smoke deadline. Stage 4 splits press/release: the release is only
    # sent after the guest consumed the press packet (mouse_count >= 2)
    # so QEMU can never coalesce press+release into a single packet.
    injected = set()
    press_sent = False
    release_sent = False
    inject_log = []
    # DESKTOP-v1 handshake state: polled only after K64-GUI-OK; the
    # echo frame screendump is captured at stage 3 BEFORE the move is
    # injected, so the cursor is still at the initial position
    d_injected = set()
    d_press_sent = False
    d_release_sent = False
    echo_ppm = None
    # GUI frame forensic handshake state, task 102: after K64-GUI-OK
    # the foundation publishes READY in desktop_gui_frame_stage and
    # spins a bounded pause window; capture screen-gui.ppm first, then
    # write RELEASE. A failed screendump never releases the guest: the
    # window times out into the EIO panic path, a FAIL, never a hang.
    gui_ppm = None
    gui_released = False

    def inject(cmd, label):
        # a monitor reply starting with Error/unknown means the command
        # was rejected: log FAILED and return False so the handshake
        # retries it on a later poll round, never record a fake 'sent'
        try:
            reply = monitor_command(art, cmd)
        except OSError as e:
            inject_log.append(f"{label}: {cmd!r} FAILED: {e}")
            return False
        low = reply.lower()
        if low.startswith("error") or low.startswith("unknown"):
            inject_log.append(f"{label}: {cmd!r} FAILED: {reply!r}")
            return False
        inject_log.append(f"{label}: {cmd!r} sent")
        return True

    stage_addr = counter_addrs.get("input_test_stage")
    mouse_addr = counter_addrs.get("input_test_mouse_count")
    d_stage_addr = counter_addrs.get("desktop_test_stage")
    gui_stage_addr = counter_addrs.get("desktop_gui_frame_stage")
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            early_exit = True  # QEMU died on its own, e.g. -no-reboot on fault
            break
        # wait for the TERMINAL marker, not the first K64-BOOT-OK: the
        # whole foundation chain must have run before judging
        serial_now = read_log(serial_path)
        if any(t in serial_now for t in TERMINAL_MARKERS):
            terminal_seen = True
            break
        # drive the handshake only after the IRQ stage: no gdbstub
        # interference with OVMF or the earlier kernel stages. Early in
        # the input stage the data VA may still be unmapped: the gdbstub
        # answers 'E' and the read raises OSError -> skip this round
        if stage_addr is not None and "K64-IRQ-OK" in serial_now:
            try:
                stage = gdb_read_mem(args.port, stage_addr, 8)
            except OSError:
                stage = None
            if stage in INPUT_STAGE_INJECT and stage not in injected:
                if inject(INPUT_STAGE_INJECT[stage], f"stage {stage}"):
                    injected.add(stage)
            elif stage == 4 and mouse_addr is not None:
                if not press_sent:
                    press_sent = inject("mouse_button 1", "stage 4 press")
                elif not release_sent:
                    try:
                        n = gdb_read_mem(args.port, mouse_addr, 8)
                    except OSError:
                        n = 0
                    if n >= 2:
                        release_sent = inject("mouse_button 0",
                                              "stage 4 release")
        # the GUI frame capture runs BEFORE the desktop handshake: the
        # guest sits in the bounded window and desktop_test_stage stays
        # 0 until RELEASE is written, so the two never interleave
        if (not args.gui_skip and gui_stage_addr is not None
                and not gui_released and "K64-GUI-OK" in serial_now):
            try:
                g_stage = gdb_read_mem(args.port, gui_stage_addr, 8)
            except OSError:
                g_stage = None
            if g_stage == GUI_FRAME_READY:
                if gui_ppm is None:
                    try:
                        gui_ppm = screendump(art, name="screen-gui.ppm")
                        inject_log.append("gui frame: screen-gui.ppm "
                                          "captured")
                    except OSError as e:
                        inject_log.append("gui frame: screendump FAILED: "
                                          f"{e}")
                if gui_ppm is not None:
                    try:
                        gdb_write_mem(args.port, gui_stage_addr,
                                      GUI_FRAME_RELEASE, 8)
                        gui_released = True
                        inject_log.append("gui frame: RELEASE written")
                    except OSError as e:
                        inject_log.append(f"gui frame: release FAILED: {e}")
        # the desktop handshake runs after the GUI milestone; the same
        # bounded poll/inject discipline as the input handshake above
        if (not args.gui_skip and d_stage_addr is not None
                and "K64-GUI-OK" in serial_now):
            try:
                d_stage = gdb_read_mem(args.port, d_stage_addr, 8)
            except OSError:
                d_stage = None
            if d_stage in DESKTOP_STAGE_INJECT and d_stage not in d_injected:
                if d_stage == 3 and echo_ppm is None:
                    # the echo leg finished: capture the frame with the
                    # cursor at (517,387) BEFORE injecting the move
                    try:
                        echo_ppm = screendump(art, name="screen-echo.ppm")
                        inject_log.append("echo frame: screen-echo.ppm "
                                          "captured")
                    except OSError as e:
                        inject_log.append("echo frame: screendump FAILED: "
                                          f"{e}")
                if inject(DESKTOP_STAGE_INJECT[d_stage],
                          f"desktop stage {d_stage}"):
                    d_injected.add(d_stage)
            elif d_stage == 4 and mouse_addr is not None:
                if not d_press_sent:
                    d_press_sent = inject("mouse_button 1",
                                          "desktop stage 4 press")
                elif not d_release_sent:
                    try:
                        n = gdb_read_mem(args.port, mouse_addr, 8)
                    except OSError:
                        n = 0
                    # input exit 3 + move 1 + press 1 = 5: the release
                    # only after the guest consumed the press packet
                    if n >= 5:
                        d_release_sent = inject("mouse_button 0",
                                                "desktop stage 4 release")
        time.sleep(0.5)
    # give the kernel a moment to finish its report, then read the
    # memory-only counters while QEMU (and the halted kernel) still lives
    time.sleep(1.0)
    counters = {}
    read_errors = list(counter_errors)
    if proc.poll() is None and counter_addrs:
        got, errs = read_counters(args.port, counter_addrs)
        counters.update(got)
        read_errors.extend(errs)
    elif not terminal_seen:
        read_errors.append("counters not read: no terminal marker "
                           "before timeout")
    # forensic evidence: on any input counter mismatch, dump the raw
    # mouse packet state before killing QEMU
    forensic = []
    expected = EXPECTED_COUNTERS_SKIP if args.gui_skip else EXPECTED_COUNTERS
    if proc.poll() is None and any(
            counters.get(n) != expected[n]
            for n in ("input_test_selftest_mask", "input_test_kbd_count",
                      "input_test_mouse_count", "input_test_stage",
                      "desktop_test_selftest_mask", "desktop_test_stage",
                      "desktop_test_keys")):
        for name, size in INPUT_FORENSIC_SYMBOLS:
            if name not in counter_addrs:
                continue
            try:
                v = gdb_read_mem(args.port, counter_addrs[name], size)
                forensic.append(f"{name} = {v:#x}")
            except OSError as e:
                forensic.append(f"{name}: {e}")
    # capture the screen before killing QEMU; screen.ppm stays in the
    # artifact directory as forensic evidence in both GUI modes
    ppm = None
    ppm_path = os.path.join(art, "screen.ppm")
    if proc.poll() is None:
        try:
            ppm = screendump(art)
        except OSError as e:
            read_errors.append(f"screendump failed: {e}")
    # geometry evidence for the skip path: the live GOP mode the loader
    # handed over (or zeros when the FRAMEBUFFER flag was never set)
    fb_note = None
    if (args.gui_skip and proc.poll() is None
            and "video_fb_state" in counter_addrs):
        try:
            raw = gdb_read_mem(args.port, counter_addrs["video_fb_state"], 64)
            b = raw.to_bytes(64, "little")
            ready, = struct.unpack_from("<I", b, 0)
            width, height = struct.unpack_from("<II", b, 8)
            fb_note = (f"video_fb_state: ready={ready} "
                       f"geometry={width}x{height} (skip evidence)")
        except OSError as e:
            read_errors.append(f"video_fb_state read: {e}")
    cleanup()

    serial = read_log(serial_path)
    debug = read_log(os.path.join(art, "qemu-debug.log"))
    ok, lines = judge(serial, debug, counters, early_exit,
                      gui_mode="skip" if args.gui_skip else "ok", ppm=ppm,
                      ppm_echo=echo_ppm, ppm_gui=gui_ppm)
    print()
    print("=" * 72)
    print(f"run-qemu-debug: smoke verdict: {'PASS' if ok else 'FAIL'}")
    for line in lines:
        print(line)
    for e in read_errors:
        print("  counter read: " + e)
    print("  input injection log:")
    if inject_log:
        for entry in inject_log:
            print("    " + entry)
    else:
        print("    (none)")
    if forensic:
        print("  input forensic state:")
        for entry in forensic:
            print("    " + entry)
    print(f"  terminal marker before timeout : {terminal_seen}")
    print(f"  gui screendump PPM  : {os.path.join(art, 'screen-gui.ppm')} "
          f"({'captured' if gui_ppm is not None else 'MISSING'})")
    print(f"  echo screendump PPM : {os.path.join(art, 'screen-echo.ppm')} "
          f"({'captured' if echo_ppm is not None else 'MISSING'})")
    print(f"  screendump PPM : {ppm_path} "
          f"({'captured' if ppm is not None else 'MISSING'})")
    if fb_note:
        print("  " + fb_note)
    if serial:
        print("  serial.log:")
        for line in serial.splitlines():
            print("    " + line)
    print("=" * 72)
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-freeze", action="store_true",
                    help="smoke mode: run without -S under a timeout")
    ap.add_argument("--gui-skip", action="store_true",
                    help="expect the non-fatal GUI skip path "
                         "(K64-GUI-SKIP) instead of K64-GUI-OK; no pixel "
                         "assertions, video_fb_state is read as evidence")
    ap.add_argument("--mem", type=int, default=128,
                    help="guest RAM in MiB (default 128)")
    ap.add_argument("--port", type=int, default=1234,
                    help="gdbstub port on 127.0.0.1 (default 1234)")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="smoke mode budget in seconds (default 120)")
    ap.add_argument("--art-dir",
                    help="artifact directory (default: "
                         "build/qemu-debug/<timestamp>-<pid>)")
    ap.add_argument("--qemu", default="qemu-system-x86_64")
    ap.add_argument("--cpu", default="qemu64,+popcnt",
                    help="QEMU -cpu model (default qemu64,+popcnt: the "
                         "kernel needs long mode, NX and POPCNT)")
    args = ap.parse_args()

    if shutil.which(args.qemu) is None and not os.path.exists(args.qemu):
        print(f"run-qemu-debug: {args.qemu} not found", file=sys.stderr)
        sys.exit(1)

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    art = os.path.abspath(args.art_dir or os.path.join(
        REPO, "build", "qemu-debug", f"{stamp}-{os.getpid()}"))
    os.makedirs(art, exist_ok=True)
    # a reused art-dir must not leak a previous session's terminal marker
    # into the smoke poll loop, nor a stale socket into the monitor bind
    for stale in ("serial.log", "qemu-debug.log", "screen.ppm",
                  "screen-echo.ppm", "screen-gui.ppm", "monitor.sock"):
        p = os.path.join(art, stale)
        if os.path.exists(p):
            os.unlink(p)
    print(f"run-qemu-debug: session artifacts: {art}")

    bin_dir = build(art)
    va, off = entry_va(os.path.join(bin_dir, "KERNEL64.BIN"))
    esp = make_esp(art, bin_dir)
    elf = gen_symbols(art, bin_dir)
    counter_addrs, counter_errors = {}, []
    if args.no_freeze:
        extra = INPUT_FORENSIC_SYMBOLS
        if args.gui_skip:
            extra += (("video_fb_state", 64),)
        counter_addrs, counter_errors = counter_addresses(art, extra)
        for e in counter_errors:
            print("run-qemu-debug: WARNING: " + e, file=sys.stderr)

    argv = qemu_argv(args, art, esp)
    print("run-qemu-debug: " + " ".join(argv))

    global _qemu_proc
    atexit.register(cleanup)
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    _qemu_proc = subprocess.Popen(argv, cwd=art, start_new_session=True)
    print(f"run-qemu-debug: QEMU pid {_qemu_proc.pid} "
          f"(own session, port {args.port})")

    if args.no_freeze:
        ok = smoke(args, art, _qemu_proc, counter_addrs, counter_errors)
        sys.exit(0 if ok else 1)

    print_guidance(args, art, va, off, elf)
    try:
        rc = _qemu_proc.wait()
    except KeyboardInterrupt:
        cleanup()
        sys.exit(130)
    print(f"run-qemu-debug: QEMU exited with {rc}")
    sys.exit(rc)


if __name__ == "__main__":
    main()
