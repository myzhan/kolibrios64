#!/usr/bin/env python3
"""Checks for the loader fixes that a plain build cannot show.

Every check below is an assertion over the real sources or over the real
artifact, none of them trusts a comment:

  1. the memory map growth releases the old buffer with the old page count and
     only publishes the bigger count after that release succeeded
  2. the ExitBootServices retry republishes the whole BootInfo memory map
     description, and validates the new range, before the next attempt
  3. no failure path after the first ExitBootServices call can reach the UEFI
     console, and the COM1 path really exists in the built image
  4. the fasm2 bootstrap gets its own OUTDIR, never the kernel build one
  5. the GOP handover is staged before ExitBootServices through a provable
     LocateProtocol, its mode walk is bounded, a missing GOP is never fatal,
     BltOnly is refused, SetMode is only reachable behind a validated exact
     match, and BootInfo gains the FRAMEBUFFER flag only together with all
     six framebuffer fields

The source checks walk the include graph of kernel/boot/bootx64.asm, resolving
every include against the directory of the file that spells it out, and build
one control flow graph over every routine and every file level label the graph
pulls in. A console call is therefore seen even when the wrapper lives in an
include: efi_puts and friends are defined in boot/uefi_prints.inc, not in the
entry file.

Nothing is matched as a string and nothing is dropped:

  * call and jump operands are decoded structurally, so a qword dispatch is a
    [base register + Structure.Field] site, and the base register has to be
    provable from EFI_SYSTEM_TABLE.BootServices before it counts as firmware
  * a conditional jump that leaves its routine is an edge like any other, so
    "jnz firmware_error" cannot hide a console call behind a label
  * a direct callee, a symbolic jump target, a register jump, a memory jump
    and a macro the checker cannot expand are all reported when they are
    reachable after the first ExitBootServices attempt; the single modelled
    exception is the final jump of main into the high half, recognised by the
    VIRT_KERNEL_BASE it adds
  * firmware entry points are only legal inside the EFI_INVALID_PARAMETER
    retry arm of the ExitBootServices loop, never on the success branch
  * a firmware base register is only trusted when its nearest preceding
    write is a mov of the full 64 bit register from the exact
    EFI_SYSTEM_TABLE.BootServices field; a call, a pop, an xchg, an
    arithmetic or logical write, a lea (which forms an address, never the
    value stored in the field) and any write of a 32/16/8 bit alias all kill
    the proof instead of being skipped
  * the ExitBootServices answer is only modelled as test rax,rax + jz for
    success and cmp rax,EFI_INVALID_PARAMETER + jnz for the retry arm, with
    no call and no write of rax (or an alias) in between; every other shape
    is reported instead of being half modelled
  * instruction prefixes are decoded before the mnemonic, never skipped:
    rep/repe/repz/repne/repnz are counted loops and write the rcx family,
    lock and the operand/address size overrides are peeled, and an unknown
    leading word leaves the line unmodelled, which fails closed
  * the string instructions write the registers they never name: stos and
    scas write rdi (both read rax), lods writes rax and rsi, movs and cmps
    write rsi and rdi, ins writes rdi, outs writes rsi; a rep prefixed form
    additionally writes rcx

The console check also disassembles the built BOOTX64.EFI, and the OUTDIR check
runs build_fasm2.sh against a stub fasm2 checkout with relative paths.

Usage: check-boot-contract.py [--source ASM] [--artifact BOOTX64.EFI]
                             [--source-only]
Exit status is non zero when any check fails.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

FAILS = []


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        FAILS.append(msg)


def note(msg):
    print("NOTE  " + msg)


def find(lines, pattern, start=0):
    for i in range(start, len(lines)):
        if re.search(pattern, lines[i]):
            return i
    return None


def find_all(lines, pattern):
    return [i for i, line in enumerate(lines) if re.search(pattern, line)]


##########################################################
# sources: include graph, routines, call graph


def strip_line(raw):
    """Drop the comment of one source line and the surrounding whitespace."""
    return raw.split(";", 1)[0].strip()


def parse_file(lines):
    """Split one parsed file into routine bodies, label blocks and macros.

    Three shapes matter for the control flow model:

      * "proc name ... endp"           a routine, the body of a call
      * "name: ..." outside a routine  a block a far jump may land in, the
                                       loader spells several of them this way
                                       (halt_forever for example)
      * "macro name ... endm"          source the checker cannot expand, kept
                                       so a control transfer inside it is
                                       reported instead of being skipped
    """
    procs = {}
    blocks = {}
    macros = {}
    current = None
    for line in lines:
        if not line:
            continue
        if current and current[0] == "macro":
            if re.match(r"end\s+macro\b|endm\b", line):
                current = None
            else:
                macros[current[1]].append(line)
            continue
        m = re.match(r"macro\s+([A-Za-z_][A-Za-z0-9_]*)", line)
        if m:
            macros[m.group(1)] = []
            current = ("macro", m.group(1))
            continue
        if current and current[0] == "proc":
            if re.match(r"endp\b", line):
                current = None
                continue
            procs[current[1]].append(line)
            continue
        m = re.match(r"proc\s+([A-Za-z_][A-Za-z0-9_]*)", line)
        if m:
            procs[m.group(1)] = []
            current = ("proc", m.group(1))
            continue
        m = GLOBAL_LABEL_RE.match(line)
        if m:
            blocks[m.group(1)] = []
            current = ("label", m.group(1))
            continue
        if DIRECTIVE_LINE_RE.match(line):
            current = None
            continue
        if current and current[0] == "label":
            blocks[current[1]].append(line)
    return procs, blocks, macros


INCLUDE_RE = re.compile(r"""include\s+(['"])([^'"]+)\1""")
GLOBAL_LABEL_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):$")
# A file level directive ends a label block: a label only owns the lines that
# follow it until the next proc, label or directive.
DIRECTIVE_LINE_RE = re.compile(
    r"^(?:include|section|format|entry|public|extrn|struct|struc|macro|if|end|align|virtual|store|load|times|repeat|display|err)\b")


class Sources:
    """Everything the checker knows about the loader sources."""

    def __init__(self, entry, root):
        self.entry = entry
        self.root = root
        self.files = []        # absolute paths, in visit order
        self.external = set()  # includes the assembler has to provide
        self.cycles = []       # include cycles cut after the first visit
        self.bodies = {}       # routine name -> stripped body lines
        self.blocks = {}       # file level label -> the lines it owns
        self.transfers = {}    # every place a control transfer may land
        self.macros = {}       # macro name in lower case -> body lines
        self.origin = {}       # routine name -> file that defines it
        self.duplicates = []   # (name, first file, second file)
        self.fstcall_alias = False

    def rel(self, path):
        return os.path.relpath(path, self.root)


def visit(path, model, seen, stack):
    """Parse one file, then every include it spells out, relative to itself."""
    real = os.path.realpath(path)
    if real in stack:
        model.cycles.append(" -> ".join(
            [model.rel(p) for p in stack[stack.index(real):]] + [model.rel(real)]))
        return
    if real in seen:
        return
    seen.add(real)
    model.files.append(real)

    with open(real, "r", errors="replace") as handle:
        lines = [strip_line(raw) for raw in handle.read().splitlines()]

    procs, blocks, macros = parse_file(lines)
    for name, body in procs.items():
        if name in model.bodies:
            model.duplicates.append((name, model.rel(model.origin[name]),
                                     model.rel(real)))
            continue
        model.bodies[name] = body
        model.origin[name] = real
    for name, body in blocks.items():
        if name in model.blocks or name in model.bodies:
            continue
        model.blocks[name] = body
    for name, body in macros.items():
        model.macros.setdefault(name.lower(), body)

    for line in lines:
        if re.match(r"macro\s+fstcall\b", line) or \
                re.match(r"fastcall\s+fix\s+fstcall\b", line):
            model.fstcall_alias = True

    for line in lines:
        m = INCLUDE_RE.match(line)
        if not m:
            continue
        target = os.path.normpath(os.path.join(os.path.dirname(real), m.group(2)))
        if os.path.isfile(target):
            visit(target, model, seen, stack + [real])
        else:
            # not relative to this file: the assembler include path owns it
            model.external.add(m.group(2))


def load_sources(entry):
    """Parse the include graph rooted at the entry file.

    Every path is resolved through realpath first, otherwise a temporary
    directory below /var and its /private/var twin would look like two trees
    and the reported paths would be meaningless.
    """
    entry = os.path.realpath(entry)
    root = os.path.dirname(os.path.dirname(os.path.dirname(entry)))
    model = Sources(entry, root)
    visit(entry, model, set(), [])
    # a proc wins over a label block of the same name, a label block only owns
    # the lines between two routines
    model.transfers = dict(model.blocks)
    model.transfers.update(model.bodies)
    return model


##########################################################
# operand decoding, control transfers, provenance


CALL_RE = re.compile(r"(fstcall|fastcall|call)\s+(.+)")
SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
REGISTER_RE = re.compile(
    r"^(?:[re]?(?:ax|bx|cx|dx|si|di|bp|sp)|r(?:[89]|1[0-5])[dwb]?|"
    r"[abcd][lh]|[sd]il|[sb]pl|rip)$")
MEM_OPERAND_RE = re.compile(r"^(?:(qword|dword|word|byte)\s+)?\[\s*(.*?)\s*\]$")
FIELD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)$")
SIZE_PREFIX_RE = re.compile(r"^(?:qword|dword|word|byte)\s+")
INSTRUCTION_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\b(.*)$")

# The 64 bit name every register spelling belongs to. A write of a 32/16/8 bit
# alias is a write of the register itself, so the provenance model never treats
# a partial write as harmless.
REGISTER_FAMILY = {}
for _name, _aliases in (
        ("rax", ("rax", "eax", "ax", "al", "ah")),
        ("rbx", ("rbx", "ebx", "bx", "bl", "bh")),
        ("rcx", ("rcx", "ecx", "cx", "cl", "ch")),
        ("rdx", ("rdx", "edx", "dx", "dl", "dh")),
        ("rsi", ("rsi", "esi", "si", "sil")),
        ("rdi", ("rdi", "edi", "di", "dil")),
        ("rbp", ("rbp", "ebp", "bp", "bpl")),
        ("rsp", ("rsp", "esp", "sp", "spl"))):
    for _alias in _aliases:
        REGISTER_FAMILY[_alias] = _name
for _number in range(8, 16):
    for _suffix in ("", "d", "w", "b"):
        REGISTER_FAMILY["r%d%s" % (_number, _suffix)] = "r%d" % _number

CALL_MNEMONICS = {"call", "fstcall", "fastcall"}
# Prefixes that turn the following instruction into a counted loop over rcx.
REPEAT_PREFIXES = {"rep", "repe", "repz", "repne", "repnz"}
# Operand and address size overrides. They never change which register family
# a form touches, but they are prefixes and have to be peeled before decoding.
SIZE_PREFIXES = {"o16", "o32", "o64", "a16", "a32", "a64"}
# Lock is a prefix as well, but it never makes the instruction repeat.
BARE_PREFIXES = {"lock"}
INSTRUCTION_PREFIXES = REPEAT_PREFIXES | SIZE_PREFIXES | BARE_PREFIXES

# String instructions write registers they never spell out as operands. A
# repeat prefix additionally writes rcx, see instruction_writes. The suffixes
# are the fasm spellings: b/w/d/q for the block movers, b/w/d for the port
# transfers, which have no qword form.
STRING_IMPLICIT_WRITES = {}
for _base, _suffixes, _writes in (
        ("stos", "bwdq", ("rdi",)),
        ("lods", "bwdq", ("rax", "rsi")),
        ("movs", "bwdq", ("rsi", "rdi")),
        ("scas", "bwdq", ("rdi",)),
        ("cmps", "bwdq", ("rsi", "rdi")),
        ("ins", "bwd", ("rdi",)),
        ("outs", "bwd", ("rsi",))):
    for _suffix in _suffixes:
        STRING_IMPLICIT_WRITES[_base + _suffix] = _writes

# An instruction whose first operand is only read. Everything else that names
# a register first may write it, which is the direction that fails closed.
NON_DEST_FIRST = {
    "cmp", "test", "bt", "push", "pushf", "pushfq", "jmp", "call",
    "fstcall", "fastcall", "out", "int", "int3", "ret", "retf",
    "prefetch", "clflush", "invd", "wbinvd", "invlpg", "pause",
}

# Registers an instruction also writes without naming them as its operands.
IMPLICIT_WRITES = {
    "mul": ("rax", "rdx"),
    "imul": ("rax", "rdx"),
    "div": ("rax", "rdx"),
    "idiv": ("rax", "rdx"),
    "cdq": ("rdx",),
    "cqo": ("rdx",),
    "cwd": ("rdx",),
    "cwde": ("rax",),
    "cbw": ("rax",),
    "cmpxchg": ("rax",),
    "cmpxchg8b": ("rax", "rdx"),
    "cmpxchg16b": ("rax", "rdx"),
    "rdmsr": ("rax", "rdx"),
    "rdtsc": ("rax", "rdx"),
    "cpuid": ("rax", "rbx", "rcx", "rdx"),
    "leave": ("rbp", "rsp"),
    "enter": ("rbp", "rsp"),
    "xlat": ("rax",),
    "loop": ("rcx",), "loope": ("rcx",), "loopne": ("rcx",),
    "loopnz": ("rcx",), "loopz": ("rcx",),
}
IMPLICIT_WRITES.update(STRING_IMPLICIT_WRITES)
RETURN_MNEMONICS = {"ret", "retf", "iret", "iretq", "syscall", "int",
                    "int3"}


def canonical_register(name):
    """The 64 bit name a register spelling belongs to, None otherwise."""
    return REGISTER_FAMILY.get(name.strip().lower())


def is_label_line(line):
    """Whether one source line is a label and not an instruction."""
    text = line.strip()
    return text.endswith(":") and " " not in text


def split_operands(text):
    """Split one Intel syntax operand list at its top level commas."""
    operands = []
    current = ""
    depth = 0
    for ch in text:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if ch == "," and depth == 0:
            operands.append(current)
            current = ""
            continue
        current += ch
    if current.strip():
        operands.append(current)
    return [SIZE_PREFIX_RE.sub("", op.strip()) for op in operands if op.strip()]


def instruction_head(line):
    """The lower case mnemonic and the operand text of one source line."""
    text = line.strip()
    if not text or is_label_line(text):
        return None, ""
    m = INSTRUCTION_RE.match(text)
    if m is None:
        return None, ""
    return m.group(1).lower(), m.group(2).strip()


def strip_prefixes(line):
    """Peel instruction prefixes: (prefixes seen, remaining text).

    Rep, lock and the operand/address size overrides are all prefixes here. An
    unknown leading word stops the peel and is handed back as the mnemonic, so
    instruction_writes fails closed on it instead of guessing.
    """
    prefixes = set()
    rest = line.strip()
    while True:
        m = INSTRUCTION_RE.match(rest)
        if m is None:
            break
        word = m.group(1).lower()
        if word not in INSTRUCTION_PREFIXES:
            break
        prefixes.add(word)
        rest = m.group(2).strip()
    return prefixes, rest


def parse_instruction(line):
    """(prefixes, mnemonic, operand text) of one source line."""
    prefixes, rest = strip_prefixes(line)
    mnemonic, operands = instruction_head(rest)
    return prefixes, mnemonic, operands


def instruction_writes(line, register):
    """Whether one source line may write a register or one of its aliases.

    The model is one sided on purpose: a call clobbers every register, a write
    of a 32/16/8 bit alias is a write of the register, and a line the checker
    does not model is assumed to write what it names. Only a mov/lea of the
    full 64 bit register is a source the provenance model is willing to follow.

    Prefixes are decoded, not skipped: rep/repe/repz/repne/repnz run the
    following instruction as a counted loop and therefore write the rcx family
    even for a string operation that never spells rcx, while an unrecognised
    prefix leaves the whole line unmodelled, which fails closed.
    """
    family = canonical_register(register) or register
    prefixes, mnemonic, rest = parse_instruction(line)
    if mnemonic is None:
        return False
    if mnemonic in CALL_MNEMONICS:
        return True
    if mnemonic in DIRECTIVES:
        return False
    if mnemonic not in MNEMONICS:
        # Unknown instruction or unknown prefix: nothing is provably read only.
        return True
    repeat = bool(prefixes & REPEAT_PREFIXES)
    operands = split_operands(rest)
    implicit = IMPLICIT_WRITES.get(mnemonic, ())
    if mnemonic == "imul" and len(operands) != 1:
        implicit = ()
    if repeat:
        # The counter is written before the first iteration and hardware
        # decrements it to zero, so rep always kills the rcx family.
        implicit = tuple(implicit) + ("rcx",)
    if family in implicit:
        return True
    if mnemonic in NON_DEST_FIRST:
        return False
    if operands and canonical_register(operands[0]) == family:
        return True
    if mnemonic in ("xchg", "xadd") and len(operands) > 1 and \
            canonical_register(operands[1]) == family:
        return True
    return False


def mov_source(line, register):
    """The value source of a plain mov into the full 64 bit register, else None.

    Only a mov forwards the value of its source. A lea computes an address,
    possibly with constant arithmetic folded in, so it never proves that the
    destination holds the value stored in a field; treating lea as an
    equivalent load is the bug this model rejects on purpose. A partial
    destination such as mov r10d, ... is not a source either: it zero extends,
    so it cannot carry a 64 bit table pointer.
    """
    family = canonical_register(register) or register
    prefixes, mnemonic, rest = parse_instruction(line)
    if prefixes & REPEAT_PREFIXES:
        return None
    if mnemonic != "mov":
        return None
    operands = split_operands(rest)
    if len(operands) != 2 or operands[0].lower() != family:
        return None
    return operands[1]


# every jump the model knows, unconditional and conditional alike: a jcc that
# leaves its routine is an edge exactly like a jmp
JUMPS = {
    "jmp",
    "ja", "jae", "jb", "jbe", "jc", "je", "jg", "jge", "jl", "jle",
    "jna", "jnae", "jnb", "jnbe", "jnc", "jne", "jng", "jnge", "jnl",
    "jnle", "jno", "jnp", "jns", "jnz", "jo", "jp", "jpe", "jpo", "js",
    "jz", "jcxz", "jecxz", "jrcxz", "loop", "loope", "loopne", "loopnz",
    "loopz",
}
COND_JUMPS = JUMPS - {"jmp"}
BRANCH_RE = re.compile(r"^([a-z][a-z0-9]*)\s+(\S.*)$")


def control_kind(line):
    """'call', 'branch', 'return' or None for one source line."""
    _, mnemonic, _ = parse_instruction(line)
    if mnemonic is None:
        return None
    if mnemonic in CALL_MNEMONICS:
        return "call"
    if mnemonic in JUMPS:
        return "branch"
    if mnemonic in RETURN_MNEMONICS:
        return "return"
    return None


def first_operand(text):
    depth = 0
    for i, ch in enumerate(text):
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        elif ch == "," and depth == 0:
            return text[:i].strip()
    return text.strip()


def split_terms(text):
    """Split a memory operand at its top level + signs."""
    terms = []
    current = ""
    depth = 0
    for ch in text:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if ch == "+" and depth == 0:
            terms.append(current.strip())
            current = ""
            continue
        current += ch
    terms.append(current.strip())
    return [term for term in terms if term]


def parse_dispatch(target):
    """Decode a call or jump operand without trusting its spelling.

    Returns (kind, payload):

      ("direct",  (symbol,))                   a call or jump to a label
      ("field",   (size, base, struct, field)) the [base + Struct.Field] shape
      ("memory",  (size, text))                any other memory operand
      ("dynamic", (text,))                     a register or an expression

    Only the "field" shape names a firmware entry point, and it does so through
    its structure: the base register and the field are parsed apart, so a
    console base with a boot service offset can never pass as firmware.
    """
    target = target.strip()
    m = MEM_OPERAND_RE.match(target)
    if m is None:
        if REGISTER_RE.match(target):
            return ("dynamic", (target,))
        if SYMBOL_RE.match(target):
            return ("direct", (target,))
        return ("dynamic", (target,))
    size = m.group(1) or ""
    terms = split_terms(m.group(2))
    if len(terms) == 2 and REGISTER_RE.match(terms[0]):
        field = FIELD_RE.match(terms[1])
        if field:
            return ("field", (size, terms[0], field.group(1), field.group(2)))
    return ("memory", (size, m.group(2)))


def firmware_entry(target):
    """(structure, field) a dispatch operand structurally names, or None."""
    kind, payload = parse_dispatch(target)
    if kind != "field":
        return None
    return (payload[2], payload[3])


def call_sites(lines, start=0):
    """Call sites of one body as (line index, mnemonic, target, kind).

    kind is "direct" for a symbolic target, otherwise the operand shape from
    parse_dispatch: "field", "memory" or "dynamic".
    """
    sites = []
    for i in range(start, len(lines)):
        m = CALL_RE.match(lines[i])
        if not m:
            continue
        target = first_operand(m.group(2).strip())
        sites.append((i, m.group(1), target, parse_dispatch(target)[0]))
    return sites


def branches(lines):
    """Every jump of one body as (line index, mnemonic, operand)."""
    out = []
    for i, line in enumerate(lines):
        m = BRANCH_RE.match(line)
        if m and m.group(1) in JUMPS:
            out.append((i, m.group(1), first_operand(m.group(2))))
    return out


def callees(lines):
    """Direct callees of one routine, in source order."""
    return [target for _, _, target, kind in call_sites(lines)
            if kind == "direct"]


def reachable(transfers, seeds):
    """Routines reachable from (name, first line) seeds.

    Calls, tail jumps and conditional jumps are all followed: a jcc that lands
    on another routine or on a file level label is a real edge, so a console
    call behind "jnz firmware_error" is found like any other.

    Returns the routine names and every (name, first line) slice that was
    expanded, so a caller can tell post exit call sites apart.
    """
    names = set()
    slices = set()
    work = list(seeds)
    while work:
        name, start = work.pop()
        if (name, start) in slices or name not in transfers:
            continue
        slices.add((name, start))
        names.add(name)
        for _, _, target, kind in call_sites(transfers[name], start):
            if kind == "direct":
                work.append((target, 0))
        for index, _, target in branches(transfers[name]):
            if index < start or target.startswith((".", "@")):
                continue
            if target in transfers:
                work.append((target, 0))
    return names, slices


def unresolved_callees(model):
    """Direct callees that no parsed file defines, the fail closed case."""
    missing = {}
    for name, lines in model.bodies.items():
        for _, _, target, kind in call_sites(lines):
            if kind == "direct" and target not in model.transfers:
                missing.setdefault(target, set()).add(name)
    return missing


def unresolved_jumps(model):
    """Symbolic jump targets no parsed file defines.

    A far jump to a label the sources do not spell out cannot be analysed, so
    it is reported instead of being dropped.
    """
    missing = {}
    for name, lines in model.transfers.items():
        for _, _, target in branches(lines):
            if target.startswith((".", "@")) or REGISTER_RE.match(target):
                continue
            if SYMBOL_RE.match(target) and target not in model.transfers:
                missing.setdefault(target, set()).add(name)
    return missing


def last_write(body, index, register):
    """The nearest preceding write to a register and its source.

    Returns (line index, source) only when the nearest write is a plain mov of
    the full 64 bit register, because that is the only source the provenance
    model follows: a mov forwards the value of the field it reads, a lea only
    computes an address. A write of an alias, a pop, an xchg, arithmetic or a
    logical instruction, a call, a lea, or a line the model does not
    understand all return (line index, None): the register was written, but to
    a value no proof covers. (None, None) means nothing in the routine writes
    it.
    """
    family = canonical_register(register)
    if family is None:
        return None, None
    for i in range(index - 1, -1, -1):
        if not instruction_writes(body[i], family):
            continue
        return i, mov_source(body[i], family)
    return None, None


SYSTEM_TABLE_FIELD_RE = re.compile(
    r"^\[\s*([A-Za-z_][A-Za-z0-9_]*)\s*\+\s*EFI_SYSTEM_TABLE\.BootServices\s*\]$")


def variable_provenance(model, variable, depth=0):
    """What a global variable provably holds: the table or None."""
    if depth > 6:
        return None
    pattern = re.compile(r"^mov\s+(?:qword\s+)?\[%s\]\s*,\s*(.+)$" %
                         re.escape(variable))
    writers = []
    for body in model.transfers.values():
        for index, line in enumerate(body):
            m = pattern.match(line)
            if m:
                writers.append((body, index, m.group(1).strip()))
    if len(writers) != 1:
        return None
    body, index, source = writers[0]
    return operand_provenance(model, body, index, source, depth + 1)


def operand_provenance(model, body, index, operand, depth=0):
    """What an operand provably refers to at one line.

    "system_table" for [efi_table], "boot_services" for the BootServices field
    of such a register (or a copy of it, or the boot_services variable), and
    None for everything else. The lookup is local and only follows a register
    back to its nearest preceding write in the same routine; anything it does
    not recognise stays unknown, which fails the firmware allow list.
    """
    if depth > 6:
        return None
    operand = SIZE_PREFIX_RE.sub("", operand.strip())
    if operand in ("[efi_table]", "efi_table"):
        return "system_table"
    m = SYSTEM_TABLE_FIELD_RE.match(operand)
    if m:
        where, source = last_write(body, index, m.group(1))
        if source is not None and \
                operand_provenance(model, body, where, source, depth + 1) == "system_table":
            return "boot_services"
        return None
    if operand in ("[boot_services]", "boot_services"):
        return variable_provenance(model, "boot_services", depth + 1)
    if REGISTER_RE.match(operand) or SYMBOL_RE.match(operand):
        where, source = last_write(body, index, operand)
        if source is not None:
            return operand_provenance(model, body, where, source, depth + 1)
    return None


def post_exit_seeds(bodies):
    """Conservative roots of the analysis that starts at the first attempt.

    A routine that marks the state is taken as a whole, a mutation may sit
    before or after the marker. A routine that calls exit_boot_services is
    analysed from the call on: its earlier code still owns the console.
    """
    seeds = set()
    for name, lines in bodies.items():
        if find(lines, r"\[boot_services_state\],\s*BOOT_SERVICES_EXIT_ATTEMPTED") is not None:
            seeds.add((name, 0))
        for index, _, target, kind in call_sites(lines):
            if kind == "direct" and target == SHUTDOWN_ROUTINE:
                seeds.add((name, index + 1))
    for name in ("post_exit_fail", "post_exit_fail_status"):
        if name in bodies:
            seeds.add((name, 0))
    return seeds


##########################################################
# constants of the contract under test

CONSOLE = {"efi_puts", "efi_putc", "efi_set_text_color",
           "efi_print_hex_fixed", "efi_print_hex_no_lz"}
PRE_EXIT_ONLY = {"boot_fail", "boot_fail_status"}
REQUIRED_INCLUDES = {"uefi_prints.inc"}
BOOTINFO_MEMMAP_FIELDS = ("memmap_addr", "memmap_size", "memmap_desc_size",
                          "memmap_desc_version", "memmap_desc_count")

# The GOP handover contract: staging happens while boot services still run,
# the protocol pointer comes from LocateProtocol through the proven boot
# services path, and the flag is only published together with the data.
GOP_HANDOVER_ROUTINE = "gop_handover"
GOP_PUBLISH_ROUTINE = "store_bootinfo_gop"
GOP_STRUCT = "EFI_GRAPHICS_OUTPUT_PROTOCOL"
BOOTINFO_GOP_FIELDS = ("gop_framebuffer_addr", "gop_framebuffer_size",
                       "gop_width", "gop_height", "gop_pixel_format",
                       "gop_pixels_per_line")

# The routine that makes the first attempt, and the entry points that may still
# be dispatched to afterwards. ExitBootServices itself is the attempt, the
# other three are only legal inside the EFI_INVALID_PARAMETER retry arm, where
# an error answer by definition leaves boot services running. The console is
# never in this set.
SHUTDOWN_ROUTINE = "exit_boot_services"
EXIT_ENTRY_POINT = "ExitBootServices"
RETRY_ENTRY_POINTS = ("GetMemoryMap", "AllocatePages", "FreePages")
FIRMWARE_STRUCT = "EFI_BOOT_SERVICES"

# The three states of the walk over the post exit control flow.
INITIAL = "initial"   # nothing consumed yet, boot services still run
RETRY = "retry"       # the ExitBootServices answer was EFI_INVALID_PARAMETER
CLOSED = "closed"     # the attempt succeeded or failed for good

# The macros the checker expands itself, everything else is unexpanded source.
EXPANDABLE_MACROS = {"fstcall", "fastcall"}

# How far back the final jump of main is allowed to look for the VIRT_KERNEL_BASE
# it adds to the kernel entry point.
KERNEL_JUMP_WINDOW = 8
KERNEL_ENTRY_ROUTINE = "main"

# Everything a post exit statement may be. A statement outside this set that is
# not a modelled macro is reported, so a new control transfer cannot slip in as
# "some directive the checker does not know".
MNEMONICS = set("""
adc add and bsf bsr bt btc btr bts bswap call cbw cdq cld cli clflush cmc cmp
cmpsb cmpsd cmpsq cmpsw cpuid cqo cwd cwde dec div enter hlt idiv imul in inc
insb insd insw int int3 invd invlpg iret iretq ja jae jb jbe jc jcxz je jecxz jg
jge jl jle jmp jna jnae jnb jnbe jnc jne jng jnge jnl jnle jno jnp jns jnz jo jp
jpe jpo jrcxz js jz lea leave lfence lodsb lodsd lodsq lodsw loop loope loopne
loopnz loopz mfence mov movsb movsd movsq movsw movsx movsxd movzx mul neg nop
not or out outsb outsd outsw pause pop popf popfq push pushf pushfq rcl rcr rdmsr rdtsc rep repe repne
repnz ret retf rol ror sahf sal sar sbb scasb scasd scasq scasw sfence shl shld
shr shrd stc std sti stosb stosd stosq stosw sub syscall test ud2 wbinvd wrmsr
xadd xchg xor
""".split())
DIRECTIVES = set("""
locals endl db dw dd dq du dp df rb rw rd rq rt align label times repeat
include section if else end display err struc struct ends macro endm proc endp
virtual at org use16 use32 use64 format entry public extrn store load equ fix
restore define purge
""".split())
DATA_DIRECTIVES = {"db", "dw", "dd", "dq", "du", "dp", "df",
                   "rb", "rw", "rd", "rq", "rt"}


STATUS_TEST_RE = re.compile(r"^test\s+rax,\s*rax$")
SUCCESS_JUMP_RE = re.compile(r"^jz\s+(\.[A-Za-z_][A-Za-z0-9_]*)$")
STATUS_COMPARE_RE = re.compile(r"^cmp\s+rax,\s*(.+)$")
INVALID_JUMP_RE = re.compile(r"^jnz\s+(\.[A-Za-z_][A-Za-z0-9_]*)$")


class ExitShape:
    """The status handling of the ExitBootServices loop, strictly modelled."""

    __slots__ = ("attempt", "success_jump", "invalid_jump", "problems")

    def __init__(self, attempt=None, success_jump=None, invalid_jump=None,
                 problems=None):
        self.attempt = attempt
        self.success_jump = success_jump
        self.invalid_jump = invalid_jump
        self.problems = list(problems or ())

    def __repr__(self):
        return ("ExitShape(attempt=%r, success_jump=%r, invalid_jump=%r, "
                "problems=%r)" % (self.attempt, self.success_jump,
                                  self.invalid_jump, self.problems))


def proven_invalid_parameter(body, index, operand):
    """Whether the right hand side of cmp provably holds EFI_INVALID_PARAMETER.

    The immediate form is accepted directly. A register is accepted only when
    its nearest preceding write is a mov of that full 64 bit register from
    EFI_INVALID_PARAMETER; a call or any other write in between kills the
    proof, so a stale compare operand cannot pass as the modelled shape.
    """
    operand = SIZE_PREFIX_RE.sub("", operand.strip())
    if operand == "EFI_INVALID_PARAMETER":
        return True
    if canonical_register(operand) is None:
        return False
    _, source = last_write(body, index, operand)
    return source is not None and source.strip() == "EFI_INVALID_PARAMETER"


def exit_attempt_shape(body):
    """The strictly modelled status handling of the ExitBootServices loop.

    The only accepted shape is

        fstcall qword [.. ExitBootServices], [efi_handle], [memory_map_key]
        test    rax, rax     ; the status the firmware returned is still in rax
        jz      .done        ; zero means the call succeeded
        ...                  ; no call, no write of rax, no branch
        cmp     rax, EFI_INVALID_PARAMETER
        jnz     .failed      ; only equality falls through into RETRY

    The compare may name EFI_INVALID_PARAMETER directly or through a register
    that provably holds it, see proven_invalid_parameter. A reversed condition,
    a reordered compare, a write of rax (any alias) and a call anywhere before
    either consumer are all reported in problems: the shape is never half
    modelled. Returns None when the routine does not call ExitBootServices at
    all, and an ExitShape with problems when it does but the status handling
    is not the modelled one.
    """
    attempts = []
    for index, _, target, _ in call_sites(body):
        kind, payload = parse_dispatch(target)
        if kind == "field" and payload[2] == FIRMWARE_STRUCT and \
                payload[3] == EXIT_ENTRY_POINT:
            attempts.append(index)
    if not attempts:
        return None
    shape = ExitShape(attempt=attempts[0])
    if len(attempts) > 1:
        shape.problems.append("%d ExitBootServices call sites" % len(attempts))
        return shape

    size = len(body)
    attempt = attempts[0]

    # the returned status has to survive untouched until the first consumer
    test_index = None
    for i in range(attempt + 1, size):
        if STATUS_TEST_RE.match(body[i]):
            test_index = i
            break
        kind = control_kind(body[i])
        if kind is not None:
            shape.problems.append("a %s before the status test" % kind)
            return shape
        if instruction_writes(body[i], "rax"):
            shape.problems.append("rax is overwritten before the status test")
            return shape
    if test_index is None:
        shape.problems.append("no test rax, rax follows the call")
        return shape

    # zero is success: the test has to be consumed by exactly jz .label
    jump = test_index + 1
    match = SUCCESS_JUMP_RE.match(body[jump]) if jump < size else None
    if match is None:
        other = BRANCH_RE.match(body[jump]) if jump < size else None
        if other and other.group(1) in COND_JUMPS:
            shape.problems.append("the zero status branch is %s, not jz"
                                  % other.group(1))
        else:
            shape.problems.append("the status test is not followed by jz")
        return shape
    if local_label(body, jump, match.group(1)) is None:
        shape.problems.append("the success branch target %s is undefined"
                              % match.group(1))
        return shape
    shape.success_jump = jump

    # the same return value has to reach a compare that names only
    # EFI_INVALID_PARAMETER, with no call and no write of rax in between
    compare = None
    for i in range(jump + 1, size):
        kind = control_kind(body[i])
        if kind is not None:
            shape.problems.append("a %s before the status compare" % kind)
            return shape
        if instruction_writes(body[i], "rax"):
            shape.problems.append("rax is overwritten before the status compare")
            return shape
        match = STATUS_COMPARE_RE.match(body[i])
        if match:
            if not proven_invalid_parameter(body, i, match.group(1)):
                shape.problems.append(
                    "the status compare does not test EFI_INVALID_PARAMETER")
                return shape
            compare = i
            break
    if compare is None:
        shape.problems.append("no cmp rax, EFI_INVALID_PARAMETER follows "
                              "the success branch")
        return shape

    # only equality is the retry arm: the compare has to be consumed by jnz
    jump = compare + 1
    match = INVALID_JUMP_RE.match(body[jump]) if jump < size else None
    if match is None:
        other = BRANCH_RE.match(body[jump]) if jump < size else None
        if other and other.group(1) in COND_JUMPS:
            shape.problems.append("the error branch is %s, not jnz"
                                  % other.group(1))
        else:
            shape.problems.append("the status compare is not followed by jnz")
        return shape
    if local_label(body, jump, match.group(1)) is None:
        shape.problems.append("the error branch target %s is undefined"
                              % match.group(1))
        return shape
    shape.invalid_jump = jump
    return shape


def classify_dispatch(model, body, index, kind, payload, state):
    """Decide one non direct call site of the post exit path.

    Returns (accepted, description). Acceptance needs all of:

      * the operand to be the qword [base + EFI_BOOT_SERVICES.Field] shape
      * the field to be one of the four legal entry points
      * the base register to be provable from EFI_SYSTEM_TABLE.BootServices
      * the path state to be the retry arm (or, for ExitBootServices alone, an
        attempt that was not already consumed)

    Nothing here looks at the operand as a string, so a console base with a
    boot service offset is rejected on the base and not accepted on the field.
    """
    if kind != "field":
        return False, "does not dispatch through a [base + Structure.Field] slot"
    size, base, struct, field = payload
    if size != "qword":
        return False, "dispatches through a %s slot instead of a qword" % (size or "plain")
    if struct != FIRMWARE_STRUCT:
        return False, "dispatches through %s.%s" % (struct, field)
    if field not in RETRY_ENTRY_POINTS and field != EXIT_ENTRY_POINT:
        return False, "dispatches through %s.%s" % (struct, field)
    if operand_provenance(model, body, index, base) != "boot_services":
        return False, ("base %s does not come from EFI_SYSTEM_TABLE.BootServices"
                       % base)
    if state == CLOSED:
        return False, "is a firmware call after the exit answer was consumed"
    if field == EXIT_ENTRY_POINT:
        return True, "%s.%s" % (struct, field)
    if state != RETRY:
        return False, ("%s is called outside the EFI_INVALID_PARAMETER retry arm"
                       % field)
    return True, "%s.%s" % (struct, field)


def hides_control(line):
    """Whether one unexpanded macro line could transfer control."""
    m = BRANCH_RE.match(line)
    if m and m.group(1) in JUMPS:
        return True
    return bool(CALL_RE.match(line))


def local_label(body, index, target):
    """The line a local label target stands for, None when absent."""
    if target.startswith("@"):
        label = re.compile(r"^@@:$")
        order = range(index, -1, -1) if target.startswith(("@b", "@B")) \
            else range(index + 1, len(body))
        for i in order:
            if label.match(body[i]):
                return i
        return None
    for i, line in enumerate(body):
        if line == target + ":":
            return i
    return None


def kernel_jump(name, body, index, register):
    """Whether a register jump is the modelled switch into the high half.

    Only the entry routine may take it, and only with a register whose nearest
    preceding write is the one that adds VIRT_KERNEL_BASE: the loader reaches
    the kernel as

        mov     rax, [kernel_entry_offset]
        add     rax, VIRT_KERNEL_BASE
        jmp     rax

    Nothing else is assumed about the target, so a register jump anywhere else,
    or one whose register was last written by something other than that add,
    stays a finding.
    """
    if name != KERNEL_ENTRY_ROUTINE:
        return False
    floor = max(0, index - KERNEL_JUMP_WINDOW)
    write = re.compile(r"^(mov|lea|add|or|xor|sub)\s+%s\s*,\s*(.+)$" % register)
    for i in range(index - 1, floor - 1, -1):
        m = write.match(body[i])
        if not m:
            continue
        return m.group(1) in ("add", "or") and "VIRT_KERNEL_BASE" in m.group(2)
    return False


def post_exit_walk(model):
    """Follow every control transfer reachable after the first exit attempt.

    The walk carries one of the three states with every queued line, so a
    dispatch is judged by the path that reached it and not by the routine it
    happens to sit in. Returns two lists, the control flow findings and the
    firmware findings; both are reported verbatim and both have to be empty.
    """
    transfers = model.transfers
    shapes = {name: exit_attempt_shape(body) for name, body in transfers.items()}
    flow = []
    firmware = []
    modelled = set()

    def report(bucket, where, message):
        item = "%s: %s" % (where, message)
        if item not in bucket:
            bucket.append(item)

    seeds = [(name, start, INITIAL)
             for name, start in sorted(post_exit_seeds(model.bodies))]
    for name, body in transfers.items():
        for index, _, target, kind in call_sites(body):
            if kind == "direct" and target == SHUTDOWN_ROUTINE:
                seeds.append((name, index + 1, CLOSED))

    seen = set()
    work = list(seeds)
    while work:
        name, index, state = work.pop()
        body = transfers.get(name)
        if body is None:
            report(flow, name, "is a control transfer target no parsed file defines")
            continue
        if index >= len(body) or (name, index, state) in seen:
            continue
        seen.add((name, index, state))
        line = body[index]
        shape = shapes.get(name)
        if shape is None:
            attempt, success_jump, invalid_jump = None, None, None
        else:
            attempt, success_jump, invalid_jump = (
                shape.attempt, shape.success_jump, shape.invalid_jump)
            if shape.problems:
                report(flow, name, "the ExitBootServices status checks are not "
                       "the modelled test/jz/cmp/jnz shape (%s)" %
                       ", ".join(shape.problems))

        if line.endswith(":") and " " not in line:
            work.append((name, index + 1, state))
            continue
        if line == "ret" or line.startswith("ret "):
            continue

        m = BRANCH_RE.match(line)
        if m and m.group(1) in JUMPS:
            mnemonic = m.group(1)
            target = first_operand(m.group(2))
            target_state = state
            fall_state = state
            if index == success_jump:
                target_state = CLOSED
            if index == invalid_jump:
                target_state = CLOSED
                fall_state = RETRY
            if REGISTER_RE.match(target):
                if not (mnemonic == "jmp" and
                        kernel_jump(name, body, index, target)):
                    report(flow, name, "%s jumps through the register %s" %
                           (mnemonic, target))
                continue
            if target.startswith((".", "@")):
                label = local_label(body, index, target)
                if label is None:
                    report(flow, name, "%s jumps to %s, which this routine does "
                           "not define" % (mnemonic, target))
                else:
                    work.append((name, label, target_state))
            elif SYMBOL_RE.match(target):
                if target in transfers:
                    work.append((target, 0,
                                 INITIAL if target == SHUTDOWN_ROUTINE else target_state))
                else:
                    report(flow, name, "%s jumps to %s, which no parsed file "
                           "defines" % (mnemonic, target))
            else:
                report(flow, name, "%s jumps through %s, which is not a modelled "
                       "transfer" % (mnemonic, target))
            if mnemonic in COND_JUMPS:
                work.append((name, index + 1, fall_state))
            continue

        m = CALL_RE.match(line)
        if m:
            target = first_operand(m.group(2).strip())
            kind, payload = parse_dispatch(target)
            if kind == "direct":
                if target in CONSOLE:
                    report(firmware, name, "calls the UEFI console wrapper %s "
                           "after the exit attempt" % target)
                if target not in transfers:
                    report(flow, name, "calls %s, which no parsed file defines" % target)
                    work.append((name, index + 1, state))
                    continue
                work.append((target, 0,
                             INITIAL if target == SHUTDOWN_ROUTINE else state))
                work.append((name, index + 1,
                             CLOSED if target == SHUTDOWN_ROUTINE else state))
                continue
            accepted, detail = classify_dispatch(model, body, index, kind,
                                                 payload, state)
            if accepted:
                modelled.add("%s -> %s" % (name, detail))
            else:
                report(firmware, name, "%s %s" % (target, detail))
            work.append((name, index + 1, state))
            continue

        head, _, rest = line.partition(" ")
        macro = model.macros.get(head.lower())
        if macro is not None and head.lower() not in EXPANDABLE_MACROS:
            hidden = [inner for inner in macro if hides_control(inner)]
            if hidden:
                report(flow, name, "invokes the macro %s, which hides a control "
                       "transfer (%s)" % (head, hidden[0]))
            work.append((name, index + 1, state))
            continue
        _, mnemonic, _ = parse_instruction(line)
        if mnemonic in MNEMONICS or mnemonic in DIRECTIVES:
            work.append((name, index + 1, state))
            continue
        if re.split(r"\s+", rest.strip())[0] in DATA_DIRECTIVES:
            work.append((name, index + 1, state))
            continue
        if index > 0 and body[index - 1].endswith("\\"):
            work.append((name, index + 1, state))
            continue
        report(flow, name, "%r is neither an instruction nor a modelled macro" % line)
        work.append((name, index + 1, state))

    return flow, firmware, sorted(modelled)



##########################################################
# checks


def check_include_graph(model):
    print("--- 0. include graph of %s ---" % model.rel(model.entry))
    check(len(model.files) > 1,
          "the entry file pulls its includes in (%d files parsed)" % len(model.files))
    check(not model.duplicates, "no routine is defined twice (%s)" % model.duplicates)
    names = {os.path.basename(path) for path in model.files}
    missing = sorted(REQUIRED_INCLUDES - names)
    check(not missing, "the console wrapper include is part of the graph (%s)" % missing)
    if model.external:
        note("left to the fasm2 include path: %s" % ", ".join(sorted(model.external)))
    if model.cycles:
        note("include cycles cut after the first visit: %s" % "; ".join(model.cycles))
    note("parsed: %s" % ", ".join(model.rel(path) for path in model.files))


def check_growth(bodies):
    print("--- 1. memory map growth ---")
    grow = bodies.get("grow_memory_map")
    free = bodies.get("free_memory_map")
    check(grow is not None, "grow_memory_map exists")
    check(free is not None, "free_memory_map exists")
    if grow is None or free is None:
        return

    release = find(free, r"EFI_BOOT_SERVICES\.FreePages")
    check(release is not None, "free_memory_map calls FreePages")
    if release is None:
        return
    check("[memory_map_pages]" in free[release],
          "FreePages is given the page count of the buffer that is mapped")
    status = find(free, r"^test\s+rax, rax$", release + 1)
    check(status is not None, "free_memory_map inspects the FreePages status")
    clears = [i for i in find_all(free, r"mov\s+(qword\s+)?\[memory_map_(addr|pages)\],\s*0")
              if status is not None and i > status]
    check(len(clears) == 2,
          "free_memory_map clears address and page count only after success")

    release = find(grow, r"free_memory_map")
    reads = find_all(grow, r"\[memory_map_pages\]")
    stores = find_all(grow, r"mov\s+(qword\s+)?\[memory_map_pages\]")
    check(release is not None, "grow_memory_map releases the live buffer")
    if release is None or not stores:
        return
    check(bool(reads) and min(reads) < release,
          "grow_memory_map snapshots the old page count before the release")
    check(min(stores) > release,
          "grow_memory_map publishes the new page count only after the release")
    status = find(grow, r"^test\s+rax, rax$", release + 1)
    check(status is not None and min(stores) > status,
          "the new page count is published only after a successful release")
    check(find(grow, r"shl\s+rax, 1") is not None,
          "grow_memory_map doubles the page count")
    check(find(grow, r"EFI_BOOT_SERVICES\.AllocatePages") is None,
          "grow_memory_map allocates through alloc_memory_map only")
    check(find(grow, r"jz\s+\.failed") is not None or
          find(grow, r"jnz\s+\.free_failed") is not None,
          "grow_memory_map keeps the old buffer when the release failed")


def check_exit_retry(bodies):
    print("--- 2. ExitBootServices retry ---")
    body = bodies.get("exit_boot_services")
    check(body is not None, "exit_boot_services exists")
    if body is None:
        return
    attempts = find_all(body, r"EFI_BOOT_SERVICES\.ExitBootServices")
    fetch = find_all(body, r"get_memory_map")
    rng = find_all(body, r"check_memory_map_range")
    publish = find_all(body, r"store_bootinfo_memmap")
    check(len(attempts) == 1, "the retry loop has a single exit call site")
    check(len(fetch) == 1 and len(rng) == 1 and len(publish) == 1,
          "one shared refresh path serves every retry")
    if not (attempts and fetch and rng and publish):
        return
    check(attempts[0] < fetch[0] < rng[0] < publish[0],
          "each retry refreshes, validates, publishes and then retries")
    check(find(body, r"^test\s+rax, rax$", fetch[0]) is not None,
          "the refresh result is inspected before it is used")
    check(find(body, r"jnz\s+\.\w+", publish[0]) is not None,
          "the loop retries ExitBootServices after publishing")
    check(find(body, r"memory_map_key") is not None,
          "the retry uses the key that came with the fresh map")

    publish_body = bodies.get("store_bootinfo_memmap") or []
    for field in BOOTINFO_MEMMAP_FIELDS:
        pattern = r"\[bootinfo \+ K64BOOTINFO\.%s\]" % field
        check(any(re.search(pattern, line) for line in publish_body),
              "store_bootinfo_memmap writes %s" % field)
    range_body = bodies.get("check_memory_map_range") or []
    check(find(range_body, r"IDENTITY_MAP_LIMIT") is not None,
          "check_memory_map_range enforces the identity map limit")
    check(find(range_body, r"\[memory_map_pages\]") is not None,
          "check_memory_map_range also checks the allocated size")


def local_flow(body):
    """Line index CFG of one routine restricted to its local labels.

    Returns (labels, edges, problems): labels maps ".name" to its label line,
    edges maps every line to its successors (a jcc owns both arms, a ret
    none, everything else falls through). A branch the local model cannot
    place is a problem, so the caller fails closed instead of guessing.
    """
    labels = {}
    for i, line in enumerate(body):
        if is_label_line(line) and line.startswith("."):
            labels[line[:-1]] = i
    edges = {}
    problems = []
    for i, line in enumerate(body):
        kind = control_kind(line)
        if kind == "return":
            edges[i] = []
        elif kind == "branch":
            mnemonic, operands = instruction_head(line)
            target = first_operand(operands)
            if target not in labels:
                problems.append(line)
                edges[i] = []
            elif mnemonic == "jmp":
                edges[i] = [labels[target]]
            else:
                edges[i] = [labels[target]]
                if i + 1 < len(body):
                    edges[i].append(i + 1)
        else:
            edges[i] = [i + 1] if i + 1 < len(body) else []
    return labels, edges, problems


def local_dominators(edges, count):
    """Dominator sets of the line CFG rooted at line 0, unreachable lines
    excluded. A node dominates another when every path from the entry to it
    passes through the first one."""
    reach = set()
    work = [0]
    while work:
        i = work.pop()
        if i in reach or i >= count:
            continue
        reach.add(i)
        work.extend(edges.get(i, []))
    preds = {i: [] for i in reach}
    for i in reach:
        for j in edges.get(i, []):
            preds[j].append(i)
    dom = {i: set(reach) for i in reach}
    dom[0] = {0}
    changed = True
    while changed:
        changed = False
        for i in sorted(reach - {0}):
            new = {i}
            if preds[i]:
                new |= set.intersection(*(dom[p] for p in preds[i]))
            if new != dom[i]:
                dom[i] = new
                changed = True
    return dom


def check_gop_handover(model):
    print("--- 5. GOP handover before ExitBootServices ---")
    bodies = model.bodies
    body = bodies.get(GOP_HANDOVER_ROUTINE)
    check(body is not None, "%s exists" % GOP_HANDOVER_ROUTINE)
    main = bodies.get(KERNEL_ENTRY_ROUTINE) or []
    call = find(main, r"fstcall\s+%s\b" % GOP_HANDOVER_ROUTINE)
    exit_call = find(main, r"fstcall\s+%s\b" % SHUTDOWN_ROUTINE)
    check(call is not None and exit_call is not None and call < exit_call,
          "main stages the GOP handover before the ExitBootServices call")
    if body is None:
        return

    # the protocol pointer must come from LocateProtocol(gop_guid, ...), and
    # the dispatch base must be provable from EFI_SYSTEM_TABLE.BootServices
    locate = None
    locate_target = None
    for index, _, target, kind in call_sites(body):
        entry = firmware_entry(target)
        if entry == (FIRMWARE_STRUCT, "LocateProtocol"):
            locate = index
            locate_target = target
            break
    check(locate is not None, "the handover locates the GOP through LocateProtocol")
    if locate is not None:
        check("gop_guid" in body[locate],
              "LocateProtocol is given the GOP GUID")
        kind, payload = parse_dispatch(locate_target)
        base = payload[1] if kind == "field" else ""
        check(operand_provenance(model, body, locate, base) == "boot_services",
              "the LocateProtocol base comes from EFI_SYSTEM_TABLE.BootServices")

    check(find(body, GOP_STRUCT + r"\.QueryMode") is not None,
          "the mode table is walked through QueryMode")
    check(find(body, GOP_STRUCT + r"\.SetMode") is not None,
          "a matching mode is switched to through SetMode")
    check(find(body, r"GOP_MAX_MODES") is not None,
          "the mode walk is bounded by GOP_MAX_MODES")
    check(find(body, r"EFI_GRAPHICS_OUTPUT_PROTOCOL_MODE\.MaxMode") is not None,
          "the mode walk also respects the firmware MaxMode")
    check(find(body, r"EFI_GRAPHICS_PIXEL_FORMAT\.PixelBltOnly") is not None,
          "BltOnly is refused: it has no linear framebuffer")

    # SetMode may only run behind a validated exact match. The scan has
    # three miss paths (budget spent, QueryMode failure, empty Info) that
    # share .scan_done with the hit path, so a textual check is not enough:
    # walk the local flow with the gop_match staging byte as a two valued
    # abstract state and prove the switch is unreachable with the marker
    # clear. Anything the little model cannot decode fails closed.
    setmode = find(body, GOP_STRUCT + r"\.SetMode")
    labels, edges, problems = local_flow(body)
    check(not problems,
          "every gop_handover branch stays inside the routine (%s)" % problems)
    unknown = [body[i] for i in find_all(body, r"\bgop_match\b")
               if not re.match(r"mov\s+\[gop_match\],\s*[01]$", body[i])
               and not re.match(r"cmp\s+\[gop_match\],\s*0$", body[i])]
    guards = find_all(body, r"cmp\s+\[gop_match\],\s*0$")
    for g in guards:
        nxt = body[g + 1] if g + 1 < len(body) else ""
        m = BRANCH_RE.match(nxt)
        if not (m and m.group(1) in ("je", "jz", "jne", "jnz")
                and first_operand(m.group(2)) in labels):
            unknown.append("unmodelled guard behind: " + body[g])
    check(guards and not unknown,
          "the mode switch is guarded by a decodable match marker test (%s)"
          % unknown)
    raised = find_all(body, r"mov\s+\[gop_match\],\s*1$")
    check(len(raised) == 1,
          "exactly one site raises the GOP match marker (%d)" % len(raised))
    width_cmp = find(body, r"cmp\s+eax,\s*GOP_TARGET_WIDTH")
    height_cmp = find(body, r"cmp\s+eax,\s*GOP_TARGET_HEIGHT")
    dom = local_dominators(edges, len(body))
    check(len(raised) == 1 and width_cmp is not None
          and height_cmp is not None
          and width_cmp in dom.get(raised[0], set())
          and height_cmp in dom.get(raised[0], set()),
          "the match marker is only raised behind both resolution comparisons")
    states = set()
    work = [(0, 0)]
    while work:
        i, flag = work.pop()
        if (i, flag) in states or i >= len(body):
            continue
        states.add((i, flag))
        m = re.match(r"mov\s+\[gop_match\],\s*([01])$", body[i])
        if m:
            work.append((i + 1, int(m.group(1))))
            continue
        mnemonic, operands = instruction_head(body[i])
        if mnemonic in ("je", "jz", "jne", "jnz") and i > 0 and \
                re.match(r"cmp\s+\[gop_match\],\s*0$", body[i - 1]):
            target = labels.get(first_operand(operands))
            if target is not None:
                taken = (flag == 0) if mnemonic in ("je", "jz") else (flag == 1)
                work.append((target if taken else i + 1, flag))
                continue
        for j in edges.get(i, []):
            work.append((j, flag))
    check(setmode is not None and (setmode, 0) not in states,
          "SetMode is unreachable without a validated exact match")
    check(setmode is not None and (setmode, 1) in states,
          "the validated match path still reaches SetMode")

    # graphics is optional: no failure path of the handover may be fatal
    fatal = sorted(set(callees(body)) & (PRE_EXIT_ONLY | {"halt_forever"}))
    check(not fatal, "a missing or failing GOP is never fatal (%s)" % fatal)

    publish = bodies.get(GOP_PUBLISH_ROUTINE)
    check(publish is not None, "%s exists" % GOP_PUBLISH_ROUTINE)
    if publish is None:
        return
    for field in BOOTINFO_GOP_FIELDS:
        pattern = r"\[bootinfo \+ K64BOOTINFO\.%s\]" % field
        check(any(re.search(pattern, line) for line in publish),
              "%s writes %s" % (GOP_PUBLISH_ROUTINE, field))
    check(find(publish, r"K64BOOTINFO_FLAG_FRAMEBUFFER") is not None,
          "%s sets FLAG_FRAMEBUFFER" % GOP_PUBLISH_ROUTINE)
    guard = find(publish, r"\[gop_valid\]")
    flag = find(publish, r"K64BOOTINFO_FLAG_FRAMEBUFFER")
    check(guard is not None and flag is not None and guard < flag,
          "the FRAMEBUFFER flag is guarded by the staging validity")
    # the flag must not be set anywhere else, e.g. unconditionally in main
    writers = [name for name, lines in bodies.items()
               if name != GOP_PUBLISH_ROUTINE
               and find(lines, r"K64BOOTINFO_FLAG_FRAMEBUFFER") is not None]
    check(not writers,
          "no other routine touches FLAG_FRAMEBUFFER (%s)" % writers)
    publish_call = find(main, r"fstcall\s+%s\b" % GOP_PUBLISH_ROUTINE)
    check(publish_call is not None and exit_call is not None
          and publish_call < exit_call,
          "main publishes the GOP fields before the ExitBootServices call")


def check_console_boundary(model):
    print("--- 3. no UEFI console after the first exit attempt ---")
    bodies = model.bodies
    body = bodies.get("exit_boot_services") or []
    state = find(body, r"mov\s+\[boot_services_state\],\s*BOOT_SERVICES_EXIT_ATTEMPTED")
    attempt = find(body, r"EFI_BOOT_SERVICES\.ExitBootServices")
    check(state is not None and attempt is not None and state < attempt,
          "the state is set before the first ExitBootServices call")

    writers = [(name, i) for name, lines in bodies.items()
               for i in find_all(lines, r"mov\s+\[boot_services_state\]")]
    check(len(writers) == 1 and writers[0][0] == "exit_boot_services",
          "only exit_boot_services ever changes the state (%s)" %
          [name for name, _ in writers])

    # a routine the graph cannot resolve could be anything, so it fails closed
    missing = unresolved_callees(model)
    check(not missing, "every direct callee resolves inside the parsed sources (%s)" %
          sorted(missing))
    missing = unresolved_jumps(model)
    check(not missing, "every symbolic jump resolves inside the parsed sources (%s)" %
          sorted(missing))
    absent = sorted(CONSOLE - set(bodies))
    check(not absent,
          "the include graph defines every UEFI console wrapper (%s)" % absent)
    check(model.fstcall_alias,
          "fstcall is still declared as an alias of fastcall in the parsed sources")

    transfers = model.transfers
    seeds = post_exit_seeds(bodies)
    after, slices = reachable(transfers, seeds)
    note("post exit analysis starts at: %s" %
         ", ".join(sorted(name for name, _ in seeds)))
    forbidden = CONSOLE | PRE_EXIT_ONLY
    check(not (after & forbidden),
          "nothing reachable after the first exit attempt prints through UEFI (%s)" %
          sorted(after & forbidden))

    classified = set()
    unclassified = []
    for name, start in sorted(slices):
        # a console wrapper that is reachable at all already failed above,
        # its own dispatch is not a second finding
        if name in forbidden:
            continue
        for _, _, target, kind in call_sites(transfers[name], start):
            if kind == "direct":
                continue
            entry = firmware_entry(target)
            if entry and entry[0] == FIRMWARE_STRUCT and \
                    entry[1] in RETRY_ENTRY_POINTS + (EXIT_ENTRY_POINT,):
                classified.add("%s -> %s.%s" % (name, entry[0], entry[1]))
            else:
                unclassified.append("%s: %s" % (name, target))
    check(not unclassified,
          "every non direct post exit call is classified (%s)" % unclassified)
    if classified:
        note("classified post exit dispatch: %s" % ", ".join(sorted(classified)))

    for root in ("get_memory_map", "grow_memory_map", "free_memory_map",
                 "alloc_memory_map", "check_memory_map_range",
                 "store_bootinfo_memmap"):
        hit = reachable(transfers, [(root, 0)])[0] & forbidden
        check(not hit, "%s never reports through UEFI (%s)" % (root, sorted(hit)))

    for name, entry in (("boot_fail", "post_exit_fail"),
                        ("boot_fail_status", "post_exit_fail_status")):
        lines = bodies.get(name) or []
        guard = find(lines, r"\[boot_services_state\]")
        console = find(lines, r"efi_puts")
        check(guard is not None and console is not None and guard < console,
              "%s tests the state before touching the console" % name)
        check(entry in callees(lines),
              "%s falls back to %s once boot services were tried" % (name, entry))

    post = bodies.get("post_exit_fail_status") or []
    used = set(callees(post))
    check(used and used <= {"com1_init", "com1_puts_tchar", "com1_puthex",
                            "halt_forever"},
          "post_exit_fail_status only uses the bounded COM1 helpers (%s)" %
          sorted(used))
    for helper in ("com1_puts", "com1_puts_tchar", "com1_putc"):
        lines = bodies.get(helper) or []
        check(find(lines, r"POST_EXIT_MAX_CHARS|COM1_SPIN_LIMIT") is not None,
              "%s bounds its wait or its length" % helper)


def check_post_exit_control(model):
    print("--- 3c. control flow and firmware calls after the exit attempt ---")
    shape = exit_attempt_shape(model.bodies.get(SHUTDOWN_ROUTINE) or [])
    check(shape is not None, "the retry loop calls ExitBootServices (%s)" %
          (shape,))
    if shape is None:
        return
    check(not shape.problems,
          "the status checks keep the strict test rax,rax + jz / "
          "cmp rax,EFI_INVALID_PARAMETER + jnz shape (%s)" %
          (", ".join(shape.problems) or "clean"))
    check(shape.success_jump is not None,
          "zero is the success branch of the retry loop (test rax,rax + jz)")
    check(shape.invalid_jump is not None,
          "only EFI_INVALID_PARAMETER falls through into the retry arm "
          "(cmp rax,EFI_INVALID_PARAMETER + jnz)")

    flow, firmware, modelled = post_exit_walk(model)
    check(not flow,
          "every post exit control transfer is modelled, no macro hides one (%s)" %
          flow)
    check(not firmware,
          "every firmware call after the first exit attempt is a legal retry (%s)" %
          firmware)
    if modelled:
        note("modelled post exit firmware calls: %s" % ", ".join(modelled))

    # the model must not be vacuous: the real retry arm has to be reached and
    # the boot services table has to be proven, otherwise the two checks above
    # would pass for the wrong reason
    expected = {"%s -> %s.%s" % (name, FIRMWARE_STRUCT, field)
                for name, field in (("exit_boot_services", EXIT_ENTRY_POINT),
                                    ("get_memory_map", "GetMemoryMap"),
                                    ("alloc_memory_map", "AllocatePages"),
                                    ("free_memory_map", "FreePages"))}
    check(expected <= set(modelled),
          "the model reaches and accepts the four legal entry points (%s)" %
          sorted(expected - set(modelled)))


def check_artifact(path):
    print("--- 3b. COM1 path in %s ---" % path)
    if not os.path.isfile(path):
        check(False, "artifact %s exists" % path)
        return
    image = open(path, "rb").read()
    check(b"K64-LOADER-FAIL".decode("ascii").encode("utf-16-le") in image,
          "the post exit banner is part of the image")
    try:
        dump = subprocess.run(["objdump", "-d", path], capture_output=True,
                              text=True, check=False).stdout
    except FileNotFoundError:
        check(False, "objdump is available for the COM1 audit")
        return
    outs = len(re.findall(r"\bout[bwl]?\s", dump))
    ins = len(re.findall(r"\bin[bwl]?\s", dump))
    # objdump prints the UART port as "movw $1016, %dx # imm = 0x3F8"
    ports = len(re.findall(r"imm = 0x3[fF][89a-fA-F]", dump))
    check(outs >= 8 and ins >= 1,
          "the image drives the UART itself (%d out, %d in)" % (outs, ins))
    check(ports >= 8, "the UART port pair is spelled out (%d references)" % ports)


def check_bootstrap_outdir(root):
    print("--- 4. bootstrap OUTDIR separation ---")
    build = os.path.join(root, "build_fasm2.sh")
    check(os.path.isfile(build), "build_fasm2.sh exists")
    if not os.path.isfile(build):
        return
    text = open(build).read()
    lines = text.splitlines()
    call = [line for line in lines
            if "bootstrap-macos.sh" in line and "tools/" in line]
    check(bool(call), "build_fasm2.sh calls the official bootstrap script")
    check(any("OUTDIR=" in line for line in lines
              if "bootstrap-macos.sh" in line.split(";")[0]) or
          "OUTDIR=$bootstrap_outdir" in text,
          "the bootstrap call spells its own OUTDIR out")
    # a relative FASM2_DIR, FASM1 or OUTDIR would be re-interpreted by the
    # bootstrap script after its cd, so build_fasm2.sh has to make them
    # absolute before it calls anything
    check(re.search(r"^fasm2_dir=.*normalise", text, re.M) is not None,
          "FASM2_DIR is normalised before it is used")
    check(re.search(r"^outdir=.*normalise", text, re.M) is not None,
          "OUTDIR is normalised before it is used")
    check(re.search(r"fasm1=.*abspath_or_name", text) is not None,
          "FASM1 is normalised before the bootstrap changes directory")

    with tempfile.TemporaryDirectory(prefix="k64-outdir-") as tmp:
        fasm2 = os.path.join(tmp, "fasm2")
        os.makedirs(os.path.join(fasm2, "include", "macro"))
        os.makedirs(os.path.join(fasm2, "tools"))
        for name in ("struct.inc", "proc64.inc"):
            open(os.path.join(fasm2, "include", "macro", name), "w").close()
        record = os.path.join(tmp, "bootstrap.record")
        # the stub behaves like the real bootstrap: it records what it was
        # given, then leaves the working directory before it writes its host
        stub = [
            "#!/bin/sh",
            "set -eu",
            "printf 'OUTDIR=%s\\n' \"$OUTDIR\" > '" + record + "'",
            "printf 'FASM1=%s\\n' \"${FASM1:-}\" >> '" + record + "'",
            "printf 'CWD=%s\\n' \"$(pwd)\" >> '" + record + "'",
            "mkdir -p \"$OUTDIR\"",
            "cd /",
            "printf '#!/bin/sh\\nfor last in \"$@\"; do out=$last; done\\n"
            "printf x > \"$out\"\\n' > \"$OUTDIR/fasmg-macos-x64\"",
            "chmod +x \"$OUTDIR/fasmg-macos-x64\"",
            "",
        ]
        with open(os.path.join(fasm2, "tools", "bootstrap-macos.sh"), "w") as f:
            f.write("\n".join(stub))
        fasm1 = os.path.join(tmp, "fasm1-stub")
        with open(fasm1, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(fasm1, 0o755)

        # task 141: build_fasm2.sh also prebuilds repo-side artifacts
        # (fsprobe.bin, chip8.bin, disk-app.img) under its own root, so
        # the fixture must run a COPY of the script inside the sandbox:
        # the stub host writes 'x' to every output it is handed, and the
        # real script would poison the real build tree (the S3 image
        # step would then bake the poisoned chip8.bin into disk-app.img)
        sandbox_build = os.path.join(tmp, "build_fasm2.sh")
        shutil.copy2(build, sandbox_build)
        os.makedirs(os.path.join(tmp, "tools"))
        shutil.copy2(os.path.join(root, "tools", "build-fat16-image.py"),
                     os.path.join(tmp, "tools", "build-fat16-image.py"))
        for stub_src in ("user/fsprobe/fsprobe.asm", "user/chip8/chip8.asm",
                         "kernel/boot/bootx64.asm", "kernel/kernel64.asm"):
            path = os.path.join(tmp, stub_src)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").close()
        # the image builder verifies the frozen ROM digests against
        # api.inc, so the sandbox carries the real constants and ROMs
        os.makedirs(os.path.join(tmp, "user", "chip8", "roms"))
        shutil.copy2(os.path.join(root, "user", "chip8", "api.inc"),
                     os.path.join(tmp, "user", "chip8", "api.inc"))
        for rom in ("ibm.ch8", "tetris1.ch8"):
            shutil.copy2(os.path.join(root, "user", "chip8", "roms", rom),
                         os.path.join(tmp, "user", "chip8", "roms", rom))
        os.makedirs(os.path.join(tmp, "tests", "stage2-app"))
        with open(os.path.join(tmp, "tests", "stage2-app",
                               "app-manifest.json"), "w") as f:
            f.write('{"/PROGRAMS/CHIP8.BIN": "build/user-chip8/chip8.bin"}')
        repo_chip8 = os.path.join(root, "build", "user-chip8", "chip8.bin")
        chip8_before = (open(repo_chip8, "rb").read()
                        if os.path.isfile(repo_chip8) else None)

        # the whole fixture is relative: this is the case a user hits when
        # calling ./build_fasm2.sh from next to the checkout
        kernel_out = "kernel-out"
        kernel_abs = os.path.join(tmp, kernel_out)
        env = dict(os.environ)
        env.pop("FASMG", None)
        env.update({"OUTDIR": kernel_out, "FASM2_DIR": "fasm2",
                    "FASM1": "./fasm1-stub"})
        proc = subprocess.run([sandbox_build], cwd=tmp, env=env,
                              capture_output=True, text=True)
        check(proc.returncode == 0,
              "build_fasm2.sh bootstraps from relative paths (status %d)" %
              proc.returncode)
        if proc.returncode != 0:
            print(proc.stdout[-800:] + proc.stderr[-800:])
            return
        fields = {}
        if os.path.isfile(record):
            for line in open(record):
                key, _, value = line.strip().partition("=")
                fields[key] = value
        expected = os.path.realpath(os.path.join(fasm2, "build",
                                                 "fasm2-bootstrap"))
        recorded = fields.get("OUTDIR", "")
        check(os.path.realpath(recorded) == expected if recorded else False,
              "the bootstrap gets its own absolute OUTDIR (%s)" %
              (recorded or "not called"))
        check(bool(recorded) and os.path.isabs(recorded),
              "the bootstrap OUTDIR is absolute (%s)" % (recorded or "unset"))
        check(recorded != kernel_out and
              os.path.realpath(recorded) != os.path.realpath(kernel_abs),
              "the kernel OUTDIR is not handed to the bootstrap")
        given = fields.get("FASM1", "")
        check(bool(given) and os.path.isabs(given),
              "the bootstrap gets an absolute FASM1 (%s)" % (given or "unset"))
        check(os.path.realpath(fields.get("CWD", "")) == os.path.realpath(tmp),
              "the bootstrap starts in the calling directory (%s)" %
              (fields.get("CWD") or "unset"))
        host = os.path.join(recorded, "fasmg-macos-x64")
        check(os.path.isfile(host),
              "the host is looked for where the bootstrap installed it")
        check(os.path.isfile(os.path.join(kernel_abs, "BOOTX64.EFI")) and
              os.path.isfile(os.path.join(kernel_abs, "KERNEL64.BIN")),
              "the kernel artifacts land in OUTDIR")
        check(not os.path.exists(os.path.join(kernel_abs, "fasmg-macos-x64")),
              "no host is written into the kernel build tree")
        # task 141 regression: the stub host must never reach the real
        # repo build tree, the sandbox copy keeps every 'x' write local
        chip8_after = (open(repo_chip8, "rb").read()
                       if os.path.isfile(repo_chip8) else None)
        check(chip8_before == chip8_after,
              "the OUTDIR fixture leaves the real chip8.bin untouched")
    check(not os.path.exists(os.path.join(tempfile.gettempdir(),
                                         os.path.basename(tmp))),
          "the bootstrap scratch directory is gone again")


def parse_args(argv):
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(here)
    options = {"source": os.path.join(repo, "kernel", "boot", "bootx64.asm"),
               "artifact": os.path.join(repo, "build", "fasm2", "BOOTX64.EFI"),
               "source_only": False,
               "repo": repo}
    rest = list(argv)
    while rest:
        arg = rest.pop(0)
        if arg in ("-h", "--help"):
            return None
        if arg == "--source":
            if not rest:
                return "missing value for --source"
            options["source"] = os.path.abspath(rest.pop(0))
        elif arg == "--artifact":
            if not rest:
                return "missing value for --artifact"
            options["artifact"] = os.path.abspath(rest.pop(0))
        elif arg == "--source-only":
            options["source_only"] = True
        elif arg.startswith("-"):
            return "unknown option %s" % arg
        else:
            options["artifact"] = os.path.abspath(arg)
    return options


def main(argv=None):
    options = parse_args(sys.argv[1:] if argv is None else argv)
    if options is None:
        print(__doc__)
        return 0
    if isinstance(options, str):
        print("FAIL  " + options)
        return 2

    source = options["source"]
    if not os.path.isfile(source):
        print("FAIL  missing %s" % source)
        return 1

    model = load_sources(source)
    print("--- %s: %d routines from %d files ---" %
          (model.rel(model.entry), len(model.bodies), len(model.files)))
    check_include_graph(model)
    check_growth(model.bodies)
    check_exit_retry(model.bodies)
    check_gop_handover(model)
    check_console_boundary(model)
    check_post_exit_control(model)
    if not options["source_only"]:
        check_artifact(options["artifact"])
        check_bootstrap_outdir(options["repo"])
    print("---")
    print("FAILURES: %d" % len(FAILS))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
