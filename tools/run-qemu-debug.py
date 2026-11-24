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
  --no-freeze    smoke mode: no -S, bounded by --timeout; success requires
                 exactly one K64-BOOT-OK in serial.log AND no triple fault
                 or unexpected reset in qemu-debug.log. A timeout alone is
                 never a success.

Nothing outside the artifact directory is written; build.sh, build_fasm2.sh
and the kernel/loader sources are never modified. QEMU runs in its own
session and is killed on exit/interrupt, so no stray process survives.

Usage:
  run-qemu-debug.py [--no-freeze] [--port N] [--timeout SEC]
                    [--art-dir DIR] [--qemu PATH]

Environment: FASMG / FASM2_DIR / FASM1 are forwarded to build_fasm2.sh.
"""

import argparse
import atexit
import datetime
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKER = "K64-BOOT-OK"

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


def qemu_argv(args, art, esp):
    argv = [args.qemu,
            "-accel", "tcg",
            "-m", "128",
            "-cpu", "qemu64",
            "-smp", "1",
            "-bios", os.path.join(REPO, "OVMF.fd"),
            "-drive", f"driver=vvfat,rw=on,dir={esp}",
            "-nic", "none",
            "-display", "none",
            "-monitor", "none",
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


def smoke(args, art, proc):
    serial_path = os.path.join(art, "serial.log")
    deadline = time.monotonic() + args.timeout
    found = False
    early_exit = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            early_exit = True  # QEMU died on its own, e.g. -no-reboot on fault
            break
        if MARKER in read_log(serial_path):
            found = True
            break
        time.sleep(0.5)
    # give the kernel a moment to finish its report, then stop QEMU
    time.sleep(1.0)
    cleanup()

    serial = read_log(serial_path)
    debug = read_log(os.path.join(art, "qemu-debug.log"))
    n_marker = serial.count(MARKER)
    triple = "Triple fault" in debug
    # -d cpu_reset logs the QEMU power-on reset AND one OVMF platform reset
    # during firmware init; both sit at the reset vector (EIP=0000fff0)
    # before any firmware code runs. Anything beyond those two, or any
    # triple fault / spontaneous exit, is an unexpected reset.
    resets = len(re.findall(r"CPU Reset", debug))
    ok = (n_marker == 1 and not triple and resets <= 2 and not early_exit
          and "K64-BOOT-FAIL" not in serial)
    print()
    print("=" * 72)
    print(f"run-qemu-debug: smoke verdict: {'PASS' if ok else 'FAIL'}")
    print(f"  {MARKER} count in serial.log : {n_marker} (need exactly 1)")
    print(f"  triple fault in qemu-debug.log : {triple}")
    print(f"  CPU Reset lines (<=2 = power-on + OVMF init) : {resets}")
    print(f"  QEMU exited early : {early_exit}")
    print(f"  marker seen before timeout : {found}")
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
    ap.add_argument("--port", type=int, default=1234,
                    help="gdbstub port on 127.0.0.1 (default 1234)")
    ap.add_argument("--timeout", type=float, default=90.0,
                    help="smoke mode budget in seconds (default 90)")
    ap.add_argument("--art-dir",
                    help="artifact directory (default: "
                         "build/qemu-debug/<timestamp>-<pid>)")
    ap.add_argument("--qemu", default="qemu-system-x86_64")
    args = ap.parse_args()

    if shutil.which(args.qemu) is None and not os.path.exists(args.qemu):
        print(f"run-qemu-debug: {args.qemu} not found", file=sys.stderr)
        sys.exit(1)

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    art = os.path.abspath(args.art_dir or os.path.join(
        REPO, "build", "qemu-debug", f"{stamp}-{os.getpid()}"))
    os.makedirs(art, exist_ok=True)
    print(f"run-qemu-debug: session artifacts: {art}")

    bin_dir = build(art)
    va, off = entry_va(os.path.join(bin_dir, "KERNEL64.BIN"))
    esp = make_esp(art, bin_dir)
    elf = gen_symbols(art, bin_dir)

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
        ok = smoke(args, art, _qemu_proc)
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