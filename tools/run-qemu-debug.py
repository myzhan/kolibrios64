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
                 not the first K64-BOOT-OK. PASS requires all seven OK
                 markers (BOOT/CPU/PMM/VMM/HEAP/IRQ/FOUNDATION) exactly once
                 and in order, no K64-CPU-PANIC / K64-BOOT-FAIL, no triple
                 fault or unexpected reset, and the frozen task-60 verdict
                 values: CPU selftest passed = 0x5FF (parsed from the
                 serial report line) plus mm_mask = 0, hook_hits = 3,
                 core_mask = 0, and the frozen IRQ-v1 verdict values
                 irq_mask = 0, ticks = 10. The kernel never prints those
                 counters on the success path, so they are read from
                 guest memory through the gdbstub after the terminal
                 marker (task 60 used the same channel). Any parse/read
                 failure, or a timeout without a terminal marker, is a
                 FAIL with the reason printed.

Nothing outside the artifact directory is written; build.sh, build_fasm2.sh
and the kernel/loader sources are never modified. QEMU runs in its own
session and is killed on exit/interrupt, so no stray process survives.

Minimum kernel CPU features: long mode (x86_64), NX (EFER.NXE) and POPCNT.
The plain TCG "qemu64" model lacks POPCNT, which the MM module uses, so the
default CPU is "qemu64,+popcnt" in BOTH debug and smoke mode; --cpu overrides
it for experiments (e.g. a host-passthrough model or extra feature flags).

Usage:
  run-qemu-debug.py [--no-freeze] [--port N] [--timeout SEC]
                    [--art-dir DIR] [--qemu PATH] [--cpu MODEL]

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

# the seven milestone markers, in their fixed emission order
MARKERS = ("K64-BOOT-OK", "K64-CPU-OK", "K64-PMM-OK", "K64-VMM-OK",
           "K64-HEAP-OK", "K64-IRQ-OK", "K64-FOUNDATION-OK")
TERMINAL_MARKERS = ("K64-FOUNDATION-OK", "K64-CPU-PANIC")
FORBIDDEN_MARKERS = ("K64-CPU-PANIC", "K64-BOOT-FAIL")

# frozen task-60 verdict values. passed comes from the serial report line;
# the counters are memory-only on the success path, so their VAs are
# resolved via a private ELF wrapper and read through the gdbstub 'm' packet
COUNTER_SYMBOLS = (("mm_test_selftest_mask", 8), ("mm_test_fail_hits", 4),
                   ("core_selftest_mask", 8),
                   ("irq_test_selftest_mask", 8), ("irq_test_ticks", 8))
EXPECTED_PASSED = 0x5FF
EXPECTED_COUNTERS = {"mm_test_selftest_mask": 0, "mm_test_fail_hits": 3,
                     "core_selftest_mask": 0,
                     "irq_test_selftest_mask": 0, "irq_test_ticks": 10}
PASSED_RE = re.compile(r"K64-CPU-SELFTEST passed = (0x[0-9A-Fa-f]+)")

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


def counter_addresses(art):
    """Resolve the three counter VAs; returns ({name: VA}, error strings).

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
    with open(wrapper, "w", encoding="utf-8") as f:
        f.write("format elf64\n")
        f.write("include '%s'\n" % os.path.join(REPO, "kernel", "kernel64.asm"))
        for name, _ in COUNTER_SYMBOLS:
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
    wanted = {name for name, _ in COUNTER_SYMBOLS}
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
    ('D') resumes the guest before the socket closes.
    """
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.settimeout(timeout)
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
        s.sendall(b"$D#44")
        return value


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


def judge(serial, debug, counters, early_exit):
    """Pure smoke verdict: (ok, report lines). No I/O in here, so the
    decision can be exercised unit-style against mutated serial/debug
    fixtures (the task-67 sensitivity evidence)."""
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
    for m in MARKERS:
        n = serial.count(m)
        req(n == 1, f"{m} count = {n} (need exactly 1)")
        positions.append(serial.find(m))
    ordered = (all(p >= 0 for p in positions)
               and positions == sorted(positions)
               and len(set(positions)) == len(positions))
    req(ordered, "the seven OK markers appear in order")
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
    for name, _ in COUNTER_SYMBOLS:
        want = EXPECTED_COUNTERS[name]
        if name not in counters:
            req(False, f"{name} unreadable (gdbstub memory read failed)")
        else:
            got = counters[name]
            req(got == want, f"{name} = {got:#x} (need {want:#x})")
    return ok, lines


def qemu_argv(args, art, esp):
    # the kernel needs long mode, NX and POPCNT; bare "qemu64" has no
    # POPCNT and the MM module would take #UD, hence the +popcnt default
    argv = [args.qemu,
            "-accel", "tcg",
            "-m", "128",
            "-cpu", args.cpu,
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


def smoke(args, art, proc, counter_addrs, counter_errors):
    serial_path = os.path.join(art, "serial.log")
    deadline = time.monotonic() + args.timeout
    early_exit = False
    terminal_seen = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            early_exit = True  # QEMU died on its own, e.g. -no-reboot on fault
            break
        # wait for the TERMINAL marker, not the first K64-BOOT-OK: the
        # whole foundation chain must have run before judging
        if any(t in read_log(serial_path) for t in TERMINAL_MARKERS):
            terminal_seen = True
            break
        time.sleep(0.5)
    # give the kernel a moment to finish its report, then read the three
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
    cleanup()

    serial = read_log(serial_path)
    debug = read_log(os.path.join(art, "qemu-debug.log"))
    ok, lines = judge(serial, debug, counters, early_exit)
    print()
    print("=" * 72)
    print(f"run-qemu-debug: smoke verdict: {'PASS' if ok else 'FAIL'}")
    for line in lines:
        print(line)
    for e in read_errors:
        print("  counter read: " + e)
    print(f"  terminal marker before timeout : {terminal_seen}")
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
    print(f"run-qemu-debug: session artifacts: {art}")

    bin_dir = build(art)
    va, off = entry_va(os.path.join(bin_dir, "KERNEL64.BIN"))
    esp = make_esp(art, bin_dir)
    elf = gen_symbols(art, bin_dir)
    counter_addrs, counter_errors = {}, []
    if args.no_freeze:
        counter_addrs, counter_errors = counter_addresses(art)
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