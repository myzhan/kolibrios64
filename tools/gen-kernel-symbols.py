#!/usr/bin/env python3
"""Generate a symbol-bearing ELF64 for the flat KERNEL64.BIN, with checks.

kernel64.asm emits a flat binary with no symbol table, and the fasmg host
CLI has no symbol dump option. This script therefore assembles the thin
wrapper tools/kernel64-elf.asm in parallel: it pulls in the unmodified
kernel source under `format elf64`, whose public macro publishes the key
labels into a real .symtab. Because the kernel keeps its absolute
`org VIRT_KERNEL_BASE`, every published symbol is SHN_ABS with the full
runtime address, ready for gdb add-symbol-file / lldb target create against
the QEMU gdbstub.

Consistency checks (any failure exits non zero and says to fall back to
raw-address debugging):

  1. ELF symbol k64_entry == VIRT_KERNEL_BASE + KERNEL64.BIN header offset 8
  2. the ELF .flat section bytes == KERNEL64.BIN (the wrapper emits the 40
     byte header too, so the header-stripped comparison is checked as well)

Usage:
  gen-kernel-symbols.py [--bin KERNEL64.BIN] [--out kernel64.elf] [--build]

  --bin   flat kernel to validate against
          (default: <repo>/build/fasm2/KERNEL64.BIN)
  --out   ELF to produce (default: <bin dir>/kernel64.elf)
  --build   run build_fasm2.sh first, with OUTDIR set to the bin directory

Environment mirrors build_fasm2.sh: FASMG (host executable), FASM2_DIR
(fasm2 checkout, default <repo>/../fasm2).
"""

import argparse
import os
import re
import struct
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHN_ABS = 0xFFF1
SHT_SYMTAB = 2
REQUIRED_SYMBOLS = ("k64_entry", "com1_init", "halt_forever")


def die(msg):
    print("gen-kernel-symbols: FAIL: " + msg, file=sys.stderr)
    print("gen-kernel-symbols: 回退到裸地址调试：hbreak *<入口VA>，"
          "入口VA = 0xFFFFFFFF80000000 + KERNEL64.BIN 头偏移 8",
          file=sys.stderr)
    sys.exit(1)


def virt_kernel_base():
    # read the constant from the source of truth instead of hardcoding it
    path = os.path.join(REPO, "kernel", "const.inc")
    with open(path, "r", encoding="utf-8") as f:
        m = re.search(r"^\s*VIRT_KERNEL_BASE\s*=\s*(0x[0-9A-Fa-f]+|\d+)",
                      f.read(), re.M)
    if not m:
        die("cannot find VIRT_KERNEL_BASE in kernel/const.inc")
    return int(m.group(1), 0)


def read_bin_header(bin_path):
    with open(bin_path, "rb") as f:
        data = f.read()
    if len(data) < 40 or data[:8] != b"KERNEL64":
        die(f"{bin_path}: missing KERNEL64 magic / truncated header")
    entry_off, = struct.unpack_from("<Q", data, 8)
    if not 40 <= entry_off < len(data):
        die(f"{bin_path}: entry_point_offset {entry_off:#x} out of range")
    return data, entry_off


def assemble_elf(bin_dir, fasm2_dir):
    host = os.environ.get("FASMG") or os.path.join(
        fasm2_dir, "build", "fasm2-bootstrap", "fasmg-macos-x64")
    if not os.access(host, os.X_OK):
        die(f"fasmg host {host} not executable; run build_fasm2.sh once "
            "to bootstrap it, or set FASMG")
    wrapper = os.path.join(REPO, "tools", "kernel64-elf.asm")
    env = dict(os.environ, INCLUDE=os.path.join(fasm2_dir, "include"))
    cmd = [host, "-e100", "-iInclude 'fasm2.inc'"]
    if env.get("K64_SMOKE_TEST") == "1":
        cmd.append("-iK64_SMOKE_TEST = 1")
    fd, tmp = tempfile.mkstemp(suffix=".elf", dir=bin_dir)
    os.close(fd)
    try:
        r = subprocess.run(
            cmd + [wrapper, tmp],
            env=env, cwd=REPO)
        if r.returncode != 0:
            die(f"fasmg failed on {wrapper} (exit {r.returncode})")
        with open(tmp, "rb") as f:
            elf = f.read()
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return elf


def parse_elf(elf):
    if elf[:6] != b"\x7fELF\x02\x01":
        die("wrapper output is not a little-endian ELF64 image")
    e_type, e_machine = struct.unpack_from("<HH", elf, 16)
    if e_machine != 62:
        die(f"ELF e_machine {e_machine} is not EM_X86_64")
    e_shoff, = struct.unpack_from("<Q", elf, 0x28)
    e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", elf, 0x3A)
    shdrs = [struct.unpack_from("<IIQQQQIIQQ", elf, e_shoff + i * e_shentsize)
             for i in range(e_shnum)]
    shstr = shdrs[e_shstrndx]
    names = elf[shstr[4]:shstr[4] + shstr[5]]

    def name(off):
        return names[off:names.index(b"\0", off)].decode()

    sections = {name(s[0]): s for s in shdrs}
    symtab = next((s for s in shdrs if s[1] == SHT_SYMTAB), None)
    if symtab is None:
        die("ELF has no .symtab")
    strtab = shdrs[symtab[6]]
    strs = elf[strtab[4]:strtab[4] + strtab[5]]
    symbols = {}
    for i in range(symtab[5] // 24):
        st_name, st_info, _, st_shndx, st_value, _ = struct.unpack_from(
            "<IBBHQQ", elf, symtab[4] + i * 24)
        nm = strs[st_name:strs.index(b"\0", st_name)].decode()
        if nm:
            symbols[nm] = (st_shndx, st_value)
    return e_type, sections, symbols


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bin", default=os.path.join(REPO, "build", "fasm2",
                                                  "KERNEL64.BIN"))
    ap.add_argument("--out")
    ap.add_argument("--build", action="store_true",
                    help="run build_fasm2.sh into the bin directory first")
    args = ap.parse_args()

    bin_path = os.path.abspath(args.bin)
    bin_dir = os.path.dirname(bin_path)
    out_path = os.path.abspath(args.out or os.path.join(bin_dir,
                                                        "kernel64.elf"))
    fasm2_dir = os.path.abspath(os.environ.get("FASM2_DIR",
                                               os.path.join(REPO, "..",
                                                            "fasm2")))

    if args.build or not os.path.exists(bin_path):
        os.makedirs(bin_dir, exist_ok=True)
        r = subprocess.run(["sh", os.path.join(REPO, "build_fasm2.sh")],
                           env=dict(os.environ, OUTDIR=bin_dir), cwd=REPO)
        if r.returncode != 0:
            die(f"build_fasm2.sh failed (exit {r.returncode})")
    if not os.path.exists(bin_path):
        die(f"{bin_path} not found; build it first or pass --build")

    bin_data, entry_off = read_bin_header(bin_path)
    base = virt_kernel_base()
    entry_va = base + entry_off

    elf = assemble_elf(bin_dir, fasm2_dir)
    e_type, sections, symbols = parse_elf(elf)

    for nm in REQUIRED_SYMBOLS:
        if nm not in symbols:
            die(f"ELF symbol {nm} missing from .symtab")
        shndx, _ = symbols[nm]
        if shndx != SHN_ABS:
            die(f"ELF symbol {nm} is not SHN_ABS (shndx={shndx:#x}); "
                "absolute org did not survive the ELF format")

    got = symbols["k64_entry"][1]
    if got != entry_va:
        die(f"k64_entry {got:#x} != VIRT_KERNEL_BASE + header offset "
            f"{entry_va:#x}")
    print(f"gen-kernel-symbols: PASS  k64_entry = {got:#x} "
          f"== {base:#x} + {entry_off:#x} (BIN header offset 8)")

    flat = sections.get(".flat")
    if flat is None:
        die("ELF has no .flat code section")
    code = elf[flat[4]:flat[4] + flat[5]]
    if code == bin_data:
        print("gen-kernel-symbols: PASS  .flat bytes == KERNEL64.BIN "
              f"({len(code)} bytes, header included)")
        print("gen-kernel-symbols: PASS  .flat[40:] == KERNEL64.BIN[40:] "
              "(header-stripped code)")
    elif code[40:] == bin_data[40:]:
        print("gen-kernel-symbols: PASS  .flat[40:] == KERNEL64.BIN[40:] "
              "(header-stripped code; header copy differs)")
    else:
        die("ELF code bytes differ from KERNEL64.BIN beyond the header")

    with open(out_path, "wb") as f:
        f.write(elf)
    print(f"gen-kernel-symbols: wrote {out_path} "
          f"(ET_REL={e_type == 1}, {len(symbols)} symbols)")
    for nm in ("kernel_header",) + REQUIRED_SYMBOLS + (
            "com1_putc", "com1_puts", "com1_puthex"):
        if nm in symbols:
            print(f"  {nm:16s} {symbols[nm][1]:#018x}")
    print("gen-kernel-symbols: lldb: target create "
          f"{out_path} ; gdb-remote 127.0.0.1:1234")
    print(f"gen-kernel-symbols: gdb:  add-symbol-file {out_path} "
          "(symbols are absolute, no section address needed)")


if __name__ == "__main__":
    main()
