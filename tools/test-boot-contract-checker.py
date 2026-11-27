#!/usr/bin/env python3
"""Negative mutation fixtures for check-boot-contract.py.

Every fixture copies kernel/ into a temporary directory, applies one textual
mutation, and runs the checker in --source-only mode against the copy. The real
tree is never touched, nothing is assembled.

The fixtures pin the models the checker claims to have:

  pristine                        the real sources still pass
  direct console call             fstcall efi_puts in the exit path fails
  console on the success branch   the exact mutation QA used to expose the gap
  console through a helper        an intermediate routine fails too
  console in the com1 fallback    post_exit_fail_status stays COM1 only
  include only wrapper            efi_putc, defined in the include, fails
  console in the caller tail      efi_puts after fstcall exit_boot_services
  indirect console dispatch       qword [OutputString] is not allow listed
  indirect boot service           a GetMemoryMap dispatch in the retry arm is
                                  accepted, the same dispatch outside it is not
  unknown direct callee           fails closed instead of being ignored
  com1 helper                     an unrelated COM1 helper must not trip it
  include cycle                   a self include is cut, the check still runs

and the review traps:

  conditional jump to a printer    "jnz firmware_error", the label prints
  jump through a register          jmp r11 is not modelled
  jump through a memory slot       jmp qword [..] is not modelled
  jump to an undefined label       jmp missing_label is not modelled
  kernel jump outside the entry    the VIRT_KERNEL_BASE jump belongs to main
  entry jump without the offset    main without its add is not modelled
  firmware call after .done        GetMemoryMap after the successful exit
  console base, service offset     ConOut base with an AllocatePages offset
  service call before the attempt  GetMemoryMap before ExitBootServices
  macro hiding a control transfer  an unexpanded macro that calls efi_puts
  statement nobody models          a bare name the checker cannot classify
  safe conditional branch          jnz into a COM1 helper is a fine branch

and the task 31 models:

  add to the base register          an add r10,8 after the proven mov is not
                                    skipped, the AllocatePages base dies
  32/16/8 bit alias write            xor r10d/r10w/r10b kills the same proof
  pop or a call                     both kill the proof too
  mov of the same register          a full register mov keeps it
  rax overwritten before the test   mov rax, EFI_INVALID_PARAMETER fails
  call before the status test       a call clobbers the returned status
  reversed status branches          jz/jnz swapped fails in both places
  call before the status compare    cmp rax, rcx after a call fails
  eax/ax/al written before cmp      every alias fails closed
  reordered status checks           compare before test fails
  wrong compare constant            EFI_BUFFER_TOO_SMALL is not the retry
                                    status either
  strict shape positive             the current shape and the immediate
                                    compare form still pass

and the task 34 models:

  rep prefixes                       rep/repe/repz/repne/repnz write rcx,
                                    and the whole rcx family with it
  string implicit writes             stos/scas write rdi, lods writes rax
                                    and rsi, movs/cmps write both index
                                    registers, ins writes rdi, outs rsi
  the non rep form                   stosb alone writes rdi, never rcx
  size and unknown prefixes          a16/a32/o16/o64 are peeled and an
                                    unknown leading word fails closed

and the task 34 mutations, each one isolated in its own sandbox:

  rep stosb before the compare       the QA reproduction: the stale compare
                                    constant must not be trusted any more
  repe cmpsq, repne scasb            both are counted loops over rcx too
  rep movsq                          writes rcx, rsi and rdi
  lodsq before the compare           writes rax, so the returned status dies
  bare stosb before the compare      writes rdi only and still passes

and the task 41 models:

  only a mov is a pointer load       lea r10,[r10+BootServices] forms an
                                    address, never the field value, so the
                                    AllocatePages base has to be rejected
  a lea through a copy chain         mov r11,r10 still follows the lea, so
                                    the dispatch through r11 is rejected too
  the mov copy chain                 the same chain built from the real mov
                                    keeps the proof and passes

and the task 76 GOP handover fixtures:

  handover after the exit call       staging must precede ExitBootServices
  unbounded mode walk                GOP_MAX_MODES has to bound the scan
  fatal GOP failure                  a missing GOP must never call boot_fail
  BltOnly accepted                   the BltOnly refusal must stay in place
  flag without data                  all six BootInfo fields are published
  unprovable LocateProtocol base     the GOP locate uses the proven path

and the task 81 SetMode guard fixtures:

  SetMode without a match            the miss paths must never reach SetMode
  match marker never raised          the hit path must still reach SetMode
  match marker raised early          only the exact match may raise it

Two more checks run the checker itself: a full pass must leave no scratch
directory behind, and a failing run must clean up just as well.

Usage: test-boot-contract-checker.py
Exit status is non zero when a fixture does not produce the expected verdict.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CHECKER = os.path.join(HERE, "check-boot-contract.py")
ENTRY = os.path.join("kernel", "boot", "bootx64.asm")
ARTIFACT = os.path.join(REPO, "build", "fasm2", "BOOTX64.EFI")

BOOT = "kernel/boot/bootx64.asm"
PRINTS = "kernel/boot/uefi_prints.inc"

STATE = "        mov     [boot_services_state], BOOT_SERVICES_EXIT_ATTEMPTED"
MAIN_PROC = "proc main _efi_handle, _efi_table"
EXIT_CALL = "        fstcall exit_boot_services"
CYCLE_ANCHOR = "; rcx - color = fore | back"
COM1_INIT = "        fstcall com1_init"

# the exact mutation task 18 used to expose the blind spot: the success branch
# of the retry loop, right after "jz .done"
QA_ANCHOR = """        fstcall qword [r10 + EFI_BOOT_SERVICES.ExitBootServices], [efi_handle], [memory_map_key]
        test    rax, rax
        jz      .done"""

# the retry arm of the same loop: EFI_INVALID_PARAMETER has just been taken
RETRY_ARM = """        jnz     .failed
        ; the map key is stale"""

# the initial attempt: the boot services table is in r10, the exit call follows
BEFORE_ATTEMPT = """        mov     r10, qword [r10 + EFI_SYSTEM_TABLE.BootServices]
        fstcall qword [r10 + EFI_BOOT_SERVICES.ExitBootServices]"""

# the tail of the loop: .done is the path on which boot services are gone
EXIT_DONE = """.map_not_mapped:
        mov     rcx, msg_error_memmap_not_mapped
        xor     edx, edx
        fstcall post_exit_fail_status, rcx, rdx
        jmp     halt_forever
.done:
        ret"""

HELPER_EFI = """proc post_exit_debug
        fstcall efi_puts, msg_newline
        ret
endp

"""

HELPER_COM1 = """proc post_exit_probe
        lea     rcx, [msg_newline]
        fstcall com1_puts_tchar, rcx
        ret
endp

"""

# a file level label that prints, reached through a conditional jump
PRINT_LABEL = """firmware_error:
        fstcall efi_puts, msg_newline
        jmp     halt_forever

"""

# a macro the checker cannot expand, hiding a console call
PRINT_MACRO = """macro firmware_debug
        fstcall efi_puts, msg_newline
end macro

"""

CONSOLE_BASE = """        mov     rax, [efi_table]
        mov     rax, qword [rax + EFI_SYSTEM_TABLE.ConOut]
        fstcall qword [rax + EFI_BOOT_SERVICES.AllocatePages], EFI_ALLOCATE_ANY_PAGES, EFI_LOADER_DATA, [memory_map_pages], memory_map_addr"""

GET_MEMMAP_DISPATCH = ("        fstcall qword [r10 + "
                       "EFI_BOOT_SERVICES.GetMemoryMap], memory_map_size")

# A firmware call after ExitBootServices cannot reuse the r10 it was loaded
# into before the call: a call clobbers the register, so the legal shape loads
# the boot services table again at the dispatch site.
GET_MEMMAP_RELOADED = """        mov     r10, [efi_table]
        mov     r10, qword [r10 + EFI_SYSTEM_TABLE.BootServices]
""" + GET_MEMMAP_DISPATCH

# the documented switch into the high half, copied into a routine that is not
# the entry point: the same three instructions are only legal in main
HELPER_KERNEL_JUMP = """proc post_exit_chain
        mov     rax, [kernel_entry_offset]
        add     rax, VIRT_KERNEL_BASE
        jmp     rax
endp

"""

KERNEL_ADD = "        add     rax, VIRT_KERNEL_BASE"

# The status handling task 31 pins down strictly: test rax,rax + jz is success,
# cmp rax,EFI_INVALID_PARAMETER + jnz is the only retry arm.
STATUS_BLOCK = """        test    rax, rax
        jz      .done
        mov     rbx, rax
        mov     rcx, EFI_INVALID_PARAMETER
        cmp     rax, rcx
        jnz     .failed"""

# The AllocatePages dispatch of alloc_memory_map. The two moves above it are
# the only source proof the base register has; everything else must kill it.
ALLOC_CALL = """        ; EfiLoaderData is the only legal type for pages owned by an image
        fstcall qword [r10 + EFI_BOOT_SERVICES.AllocatePages], EFI_ALLOCATE_ANY_PAGES, \\
                EFI_LOADER_DATA, [memory_map_pages], memory_map_addr"""

# The one line that has to be a mov: it reads the BootServices field value.
LOAD_BOOT_SERVICES = "        mov     r10, qword [r10 + EFI_SYSTEM_TABLE.BootServices]"

# The whole AllocatePages setup, unique because of the loader data comment
# above the call; task 41 swaps only the load and/or the dispatch base.
ALLOC_BLOCK = ("        mov     r10, [efi_table]\n" + LOAD_BOOT_SERVICES +
               "\n" + ALLOC_CALL)

# task 76: the GOP handover call site in main and the LocateProtocol setup
# in gop_handover, unique because of the dispatch that follows the loads.
GOP_CALL = "        fstcall gop_handover"
GOP_LOCATE_BLOCK = ("        mov     r10, [efi_table]\n" + LOAD_BOOT_SERVICES +
                    "\n        fstcall qword [r10 + "
                    "EFI_BOOT_SERVICES.LocateProtocol], gop_guid, 0, gop_interface")

# task 81: the match marker guard in front of the mode switch and the two
# marker writes, each unique because no other line touches gop_match this way.
GOP_MATCH_GUARD = ("        cmp     [gop_match], 0\n"
                   "        je      .mode_ready")
GOP_MATCH_RAISE = "        mov     [gop_match], 1"
GOP_MATCH_INIT = "        mov     [gop_match], 0"


def alloc_load(load):
    """Replace only the BootServices load line of alloc_memory_map."""
    return (BOOT, ALLOC_BLOCK, ALLOC_BLOCK.replace(LOAD_BOOT_SERVICES, load))


def alloc_copy_chain(load):
    """Load the base with `load`, copy r10 to r11, dispatch through r11."""
    block = ALLOC_BLOCK.replace(LOAD_BOOT_SERVICES, load)
    block = block.replace("        ; EfiLoaderData",
                          "        mov     r11, r10\n        ; EfiLoaderData")
    block = block.replace("[r10 + EFI_BOOT_SERVICES.AllocatePages]",
                          "[r11 + EFI_BOOT_SERVICES.AllocatePages]")
    return (BOOT, ALLOC_BLOCK, block)


def before_alloc(insert):
    """Insert one line into alloc_memory_map before its AllocatePages call."""
    return (BOOT, ALLOC_CALL,
            ALLOC_CALL.replace("        fstcall qword",
                               insert + "\n        fstcall qword"))


# The retry compare reads a register that still has to hold the constant. A
# counted string op between the mov and the cmp rewrites rcx, which the model
# has to see; the non rep form writes rdi only and leaves the compare alone.
INVALID_CONSTANT = "        mov     rcx, EFI_INVALID_PARAMETER"


# One instruction and one register per row, scored by the checker's own
# instruction_writes, so the alias and suffix questions are pinned directly.
IMPLICIT_MODEL_CASES = [
    ("rep stosb", "rcx", True),
    ("rep stosb", "ecx", True),
    ("rep stosb", "cx", True),
    ("rep stosb", "cl", True),
    ("rep stosb", "rdi", True),
    ("rep stosb", "edi", True),
    ("rep stosb", "di", True),
    ("rep stosb", "dil", True),
    ("rep stosb", "rax", False),
    ("rep stosb", "rsi", False),
    ("repe cmpsq", "rcx", True),
    ("repe cmpsq", "rsi", True),
    ("repe cmpsq", "rdi", True),
    ("repe cmpsq", "rax", False),
    ("repne scasb", "rcx", True),
    ("repne scasb", "rdi", True),
    ("repne scasb", "rax", False),
    ("rep movsq", "rcx", True),
    ("rep movsq", "rsi", True),
    ("rep movsq", "rdi", True),
    ("rep movsq", "rax", False),
    ("lodsq", "rax", True),
    ("lodsq", "eax", True),
    ("lodsq", "ax", True),
    ("lodsq", "al", True),
    ("lodsq", "rsi", True),
    ("lodsq", "esi", True),
    ("lodsq", "rdi", False),
    ("stosb", "rcx", False),
    ("stosb", "rdi", True),
    ("stosb", "rax", False),
    ("movsb", "rsi", True),
    ("movsb", "rdi", True),
    ("movsb", "rcx", False),
    ("cmpsq", "rsi", True),
    ("cmpsq", "rdi", True),
    ("scasb", "rdi", True),
    ("scasb", "rax", False),
    ("insb", "rdi", True),
    ("insw", "rdi", True),
    ("insd", "rdi", True),
    ("outsb", "rsi", True),
    ("outsw", "rsi", True),
    ("outsd", "rsi", True),
    ("rep insb", "rcx", True),
    ("rep outsd", "rcx", True),
    ("repz scasb", "rcx", True),
    ("repnz cmpsw", "rcx", True),
    ("a32 rep movsb", "rcx", True),
    ("a32 rep movsb", "rsi", True),
    ("o16 rep stosw", "rdi", True),
    ("o16 rep stosw", "rcx", True),
    ("lock xadd [rsp], rax", "rax", True),
    ("lock add [rsp], rax", "rax", False),
    ("rep nop", "rcx", True),
    ("xacquire stosb", "rdi", True),
    ("xacquire stosb", "rcx", True),
]


def before_invalid_compare(insert):
    """Insert one instruction between the compare constant and the compare."""
    return (BOOT, INVALID_CONSTANT, INVALID_CONSTANT + "\n" + insert)


def in_exit_path(call):
    """Insert one line right after the state marker of exit_boot_services."""
    return (BOOT, STATE, STATE + "\n" + call)


def before_main(body):
    """Define something at file level right before the entry routine."""
    return (BOOT, MAIN_PROC, body + MAIN_PROC)


def with_helper(body, call):
    """Define a helper routine and call it from the exit path."""
    return [before_main(body), in_exit_path(call)]


FIXTURES = [
    {
        "name": "pristine sources pass",
        "expect": 0,
        "edits": [],
        "needle": "PASS  nothing reachable after the first exit attempt "
                  "prints through UEFI ([])",
    },
    {
        "name": "direct efi_puts on the exit success branch",
        "expect": 1,
        "edits": [(BOOT, QA_ANCHOR, QA_ANCHOR +
                   "\n        fstcall efi_puts, msg_newline")],
        "needle": "prints through UEFI (['efi_puts'])",
        "forbidden": "FAIL  every direct callee resolves",
    },
    {
        "name": "efi_puts on the exit failure branch",
        "expect": 1,
        "edits": [in_exit_path("        fstcall efi_puts, msg_newline")],
        "needle": "prints through UEFI (['efi_puts'])",
        "forbidden": "FAIL  every direct callee resolves",
    },
    {
        "name": "efi_puts inside the COM1 fallback",
        "expect": 1,
        "edits": [(BOOT, COM1_INIT,
                   COM1_INIT + "\n        fstcall efi_puts, msg_newline")],
        "needle": "prints through UEFI (['efi_puts'])",
    },
    {
        "name": "efi_puts behind an intermediate helper",
        "expect": 1,
        "edits": with_helper(HELPER_EFI, "        fstcall post_exit_debug, rcx"),
        "needle": "prints through UEFI (['efi_puts'])",
        "forbidden": "FAIL  every direct callee resolves",
    },
    {
        "name": "include only wrapper efi_putc",
        "expect": 1,
        "edits": [in_exit_path("        fstcall efi_putc, rcx")],
        "needle": "prints through UEFI (['efi_putc'])",
        "forbidden": "FAIL  every direct callee resolves",
    },
    {
        "name": "console in the caller tail after the exit call",
        "expect": 1,
        "edits": [(BOOT, EXIT_CALL,
                   EXIT_CALL + "\n        fstcall efi_print_hex_fixed, rax")],
        "needle": "prints through UEFI (['efi_print_hex_fixed', 'efi_puts'])",
        "forbidden": "post exit call is classified (['"
    },
    {
        "name": "indirect console dispatch after the exit attempt",
        "expect": 1,
        "edits": [in_exit_path("        fstcall qword [rax + "
                               "EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL.OutputString], "
                               "rax, rdx")],
        "needle": "every non direct post exit call is classified",
    },
    {
        "name": "indirect boot service dispatch in the retry arm stays allowed",
        "expect": 0,
        "edits": [(BOOT, RETRY_ARM,
                   "        jnz     .failed\n" + GET_MEMMAP_RELOADED +
                   "\n        ; the map key is stale")],
        "needle": "PASS  every firmware call after the first exit attempt is a "
                  "legal retry",
    },
    {
        "name": "unknown direct callee fails closed",
        "expect": 1,
        "edits": [in_exit_path("        fstcall efi_puts_typo, rcx")],
        "needle": "every direct callee resolves inside the parsed sources "
                  "(['efi_puts_typo'])",
    },
    {
        "name": "unrelated COM1 helper is not a false positive",
        "expect": 0,
        "edits": with_helper(HELPER_COM1, "        fstcall post_exit_probe, rcx"),
        "needle": "PASS  nothing reachable after the first exit attempt "
                  "prints through UEFI ([])",
    },
    {
        "name": "include cycle is cut instead of looping",
        "expect": 0,
        "edits": [(PRINTS, CYCLE_ANCHOR,
                   "include 'uefi_prints.inc'\n\n" + CYCLE_ANCHOR)],
        "needle": "include cycles cut after the first visit",
    },
    # ------------------------------------------------------------------
    # the traps of the final review
    {
        "name": "conditional jump to a label that prints",
        "expect": 1,
        "edits": [before_main(PRINT_LABEL),
                  in_exit_path("        jnz     firmware_error")],
        "needle": "calls the UEFI console wrapper efi_puts",
    },
    {
        "name": "conditional jump into a COM1 helper is a fine branch",
        "expect": 0,
        "edits": [before_main(HELPER_COM1),
                  in_exit_path("        jnz     post_exit_probe")],
        "needle": "PASS  every post exit control transfer is modelled",
    },
    {
        "name": "jump through a register is not modelled",
        "expect": 1,
        "edits": [in_exit_path("        jmp     r11")],
        "needle": "jumps through the register r11",
    },
    {
        "name": "jump through a memory slot is not modelled",
        "expect": 1,
        "edits": [in_exit_path("        jmp     qword [r10 + "
                               "EFI_BOOT_SERVICES.HandleProtocol]")],
        "needle": "which is not a modelled transfer",
    },
    {
        "name": "jump to an undefined label fails closed",
        "expect": 1,
        "edits": [in_exit_path("        jmp     missing_label")],
        "needle": "which no parsed file defines",
    },
    {
        "name": "the kernel switch is only modelled in the entry routine",
        "expect": 1,
        "edits": with_helper(HELPER_KERNEL_JUMP,
                             "        fstcall post_exit_chain"),
        "needle": "jmp jumps through the register rax",
    },
    {
        "name": "the entry jump without the VIRT_KERNEL_BASE add fails",
        "expect": 1,
        "edits": [(BOOT, KERNEL_ADD, "        nop")],
        "needle": "jmp jumps through the register rax",
    },
    {
        "name": "GetMemoryMap after the successful exit",
        "expect": 1,
        # the dispatch goes before the ret, still inside .done: the path on
        # which the exit answer was already consumed
        "edits": [(BOOT, EXIT_DONE,
                   EXIT_DONE.replace("        ret",
                                     GET_MEMMAP_RELOADED + "\n        ret"))],
        "needle": "is a firmware call after the exit answer was consumed",
    },
    {
        "name": "console base with a boot service offset",
        "expect": 1,
        "edits": [(BOOT, RETRY_ARM,
                   "        jnz     .failed\n" + CONSOLE_BASE +
                   "\n        ; the map key is stale")],
        "needle": "base rax does not come from EFI_SYSTEM_TABLE.BootServices",
    },
    {
        "name": "boot service call before the first attempt",
        "expect": 1,
        "edits": [(BOOT, BEFORE_ATTEMPT,
                   "        mov     r10, qword [r10 + "
                   "EFI_SYSTEM_TABLE.BootServices]\n" + GET_MEMMAP_DISPATCH +
                   "\n        fstcall qword [r10 + "
                   "EFI_BOOT_SERVICES.ExitBootServices]")],
        "needle": "GetMemoryMap is called outside the "
                  "EFI_INVALID_PARAMETER retry arm",
    },
    {
        "name": "macro hiding a control transfer",
        "expect": 1,
        "edits": [before_main(PRINT_MACRO),
                  in_exit_path("        firmware_debug")],
        "needle": "invokes the macro firmware_debug, which hides a control "
                  "transfer",
    },
    {
        "name": "a statement the checker cannot classify fails closed",
        "expect": 1,
        "edits": [in_exit_path("        firmware_hook")],
        "needle": "is neither an instruction nor a modelled macro",
    },
    # ------------------------------------------------------------------
    # task 31: the base register proof is a conservative dataflow
    {
        "name": "an add of the boot services base register kills the proof",
        "expect": 1,
        "edits": [before_alloc("        add     r10, 8")],
        "needle": "base r10 does not come from EFI_SYSTEM_TABLE.BootServices",
    },
    {
        "name": "a 32 bit xor of the base register kills the proof",
        "expect": 1,
        "edits": [before_alloc("        xor     r10d, r10d")],
        "needle": "base r10 does not come from EFI_SYSTEM_TABLE.BootServices",
    },
    {
        "name": "a 16 bit write of the base register kills the proof",
        "expect": 1,
        "edits": [before_alloc("        xor     r10w, r10w")],
        "needle": "base r10 does not come from EFI_SYSTEM_TABLE.BootServices",
    },
    {
        "name": "an 8 bit write of the base register kills the proof",
        "expect": 1,
        "edits": [before_alloc("        xor     r10b, r10b")],
        "needle": "base r10 does not come from EFI_SYSTEM_TABLE.BootServices",
    },
    {
        "name": "a pop of the base register kills the proof",
        "expect": 1,
        "edits": [before_alloc("        pop     r10")],
        "needle": "base r10 does not come from EFI_SYSTEM_TABLE.BootServices",
    },
    {
        "name": "a call clobbers the base register proof",
        "expect": 1,
        "edits": [before_alloc("        fstcall com1_init")],
        "needle": "base r10 does not come from EFI_SYSTEM_TABLE.BootServices",
    },
    {
        "name": "a mov of the same register keeps the proof",
        "expect": 0,
        "edits": [before_alloc("        mov     r10, r10")],
        "needle": "PASS  every firmware call after the first exit attempt is a "
                  "legal retry",
    },
    # ------------------------------------------------------------------
    # task 31: the ExitBootServices status handling is modelled strictly
    {
        "name": "rax is overwritten before the status test",
        "expect": 1,
        "edits": [(BOOT, QA_ANCHOR,
                   QA_ANCHOR.replace("        test    rax, rax",
                                     "        mov     rax, "
                                     "EFI_INVALID_PARAMETER\n"
                                     "        test    rax, rax"))],
        "needle": "rax is overwritten before the status test",
    },
    {
        "name": "a call before the status test fails closed",
        "expect": 1,
        "edits": [(BOOT, QA_ANCHOR,
                   QA_ANCHOR.replace("        test    rax, rax",
                                     "        fstcall com1_init\n"
                                     "        test    rax, rax"))],
        "needle": "a call before the status test",
    },
    {
        "name": "the zero status branch has to be jz, not jnz",
        "expect": 1,
        "edits": [(BOOT, STATUS_BLOCK,
                   STATUS_BLOCK.replace("        jz      .done",
                                        "        jnz     .done"))],
        "needle": "the zero status branch is jnz, not jz",
    },
    {
        "name": "the error branch has to be jnz, not jz",
        "expect": 1,
        "edits": [(BOOT, STATUS_BLOCK,
                   STATUS_BLOCK.replace("        jnz     .failed",
                                        "        jz      .failed"))],
        "needle": "the error branch is jz, not jnz",
    },
    {
        "name": "a call before the status compare clobbers rax",
        "expect": 1,
        "edits": [(BOOT, STATUS_BLOCK,
                   STATUS_BLOCK.replace("        mov     rbx, rax",
                                        "        fstcall com1_init\n"
                                        "        mov     rbx, rax"))],
        "needle": "a call before the status compare",
    },
    {
        "name": "xor eax before the status compare fails closed",
        "expect": 1,
        "edits": [(BOOT, STATUS_BLOCK,
                   STATUS_BLOCK.replace("        cmp     rax, rcx",
                                        "        xor     eax, eax\n"
                                        "        cmp     rax, rcx"))],
        "needle": "rax is overwritten before the status compare",
    },
    {
        "name": "xor ax before the status compare fails closed",
        "expect": 1,
        "edits": [(BOOT, STATUS_BLOCK,
                   STATUS_BLOCK.replace("        cmp     rax, rcx",
                                        "        xor     ax, ax\n"
                                        "        cmp     rax, rcx"))],
        "needle": "rax is overwritten before the status compare",
    },
    {
        "name": "xor al before the status compare fails closed",
        "expect": 1,
        "edits": [(BOOT, STATUS_BLOCK,
                   STATUS_BLOCK.replace("        cmp     rax, rcx",
                                        "        xor     al, al\n"
                                        "        cmp     rax, rcx"))],
        "needle": "rax is overwritten before the status compare",
    },
    {
        "name": "a reordered status test and compare fails closed",
        "expect": 1,
        "edits": [(BOOT, STATUS_BLOCK,
                   "        mov     rbx, rax\n"
                   "        mov     rcx, EFI_INVALID_PARAMETER\n"
                   "        cmp     rax, rcx\n"
                   "        jnz     .failed\n"
                   "        test    rax, rax\n"
                   "        jz      .done")],
        "needle": "a branch before the status test",
    },
    {
        "name": "the status compare has to name EFI_INVALID_PARAMETER",
        "expect": 1,
        "edits": [(BOOT, STATUS_BLOCK,
                   STATUS_BLOCK.replace(
                       "        mov     rcx, EFI_INVALID_PARAMETER",
                       "        mov     rcx, EFI_BUFFER_TOO_SMALL"))],
        "needle": "the status compare does not test EFI_INVALID_PARAMETER",
    },
    {
        "name": "the current status checks pass the strict shape",
        "expect": 0,
        "edits": [],
        "needle": "PASS  zero is the success branch of the retry loop",
    },
    {
        "name": "the immediate compare form passes the strict shape",
        "expect": 0,
        "edits": [(BOOT, STATUS_BLOCK,
                   "        test    rax, rax\n"
                   "        jz      .done\n"
                   "        mov     rbx, rax\n"
                   "        cmp     rax, EFI_INVALID_PARAMETER\n"
                   "        jnz     .failed")],
        "needle": "PASS  the status checks keep the strict",
    },
    # ------------------------------------------------------------------
    # task 34: rep prefixes and the implicit registers of string instructions
    {
        "name": "rep stosb before the exit status compare kills rcx",
        "expect": 1,
        "edits": [before_invalid_compare("        rep     stosb")],
        "needle": "the status compare does not test EFI_INVALID_PARAMETER",
    },
    {
        "name": "repe cmpsq before the exit status compare kills rcx",
        "expect": 1,
        "edits": [before_invalid_compare("        repe    cmpsq")],
        "needle": "the status compare does not test EFI_INVALID_PARAMETER",
    },
    {
        "name": "repne scasb before the exit status compare kills rcx",
        "expect": 1,
        "edits": [before_invalid_compare("        repne   scasb")],
        "needle": "the status compare does not test EFI_INVALID_PARAMETER",
    },
    {
        "name": "rep movsq before the exit status compare kills rcx",
        "expect": 1,
        "edits": [before_invalid_compare("        rep     movsq")],
        "needle": "the status compare does not test EFI_INVALID_PARAMETER",
    },
    {
        "name": "lodsq before the exit status compare overwrites rax",
        "expect": 1,
        "edits": [before_invalid_compare("        lodsq")],
        "needle": "rax is overwritten before the status compare",
    },
    {
        "name": "bare stosb writes rdi and leaves the compare alone",
        "expect": 0,
        "edits": [before_invalid_compare("        stosb")],
        "needle": "PASS  the status checks keep the strict",
    },
    # ------------------------------------------------------------------
    # task 41: a pointer load is a mov from the field, a lea is only an address
    {
        "name": "a lea of the BootServices field is not a pointer load",
        "expect": 1,
        "edits": [alloc_load(
            "        lea     r10, [r10 + EFI_SYSTEM_TABLE.BootServices]")],
        "needle": "base r10 does not come from "
                  "EFI_SYSTEM_TABLE.BootServices",
    },
    {
        "name": "a lea load copied through a register still fails",
        "expect": 1,
        "edits": [alloc_copy_chain(
            "        lea     r10, [r10 + EFI_SYSTEM_TABLE.BootServices]")],
        "needle": "base r11 does not come from "
                  "EFI_SYSTEM_TABLE.BootServices",
    },
    {
        "name": "a mov load copied through a register keeps the proof",
        "expect": 0,
        "edits": [alloc_copy_chain(LOAD_BOOT_SERVICES)],
        "needle": "PASS  every firmware call after the first exit attempt is "
                  "a legal retry",
    },
    # ------------------------------------------------------------------
    # task 76: the GOP handover is staged before ExitBootServices and is
    # optional, bounded and truthful
    {
        "name": "the pristine GOP handover passes its contract",
        "expect": 0,
        "edits": [],
        "needle": "PASS  main stages the GOP handover before the "
                  "ExitBootServices call",
    },
    {
        "name": "the GOP handover after the exit call fails",
        "expect": 1,
        "edits": [(BOOT, GOP_CALL, "        nop"),
                  (BOOT, EXIT_CALL, EXIT_CALL + "\n" + GOP_CALL)],
        "needle": "main stages the GOP handover before the ExitBootServices call",
    },
    {
        "name": "an unbounded GOP mode walk fails",
        "expect": 1,
        "edits": [(BOOT, "        mov     ebx, GOP_MAX_MODES", "        nop")],
        "needle": "the mode walk is bounded by GOP_MAX_MODES",
    },
    {
        "name": "a fatal GOP failure path fails",
        "expect": 1,
        "edits": [(BOOT, "        fstcall efi_puts, msg_gop_absent",
                   "        fstcall boot_fail, msg_gop_absent\n"
                   "        fstcall efi_puts, msg_gop_absent")],
        "needle": "a missing or failing GOP is never fatal",
    },
    {
        "name": "accepting BltOnly fails",
        "expect": 1,
        "edits": [(BOOT,
                   "        cmp     ecx, EFI_GRAPHICS_PIXEL_FORMAT.PixelBltOnly",
                   "        nop")],
        "needle": "BltOnly is refused",
    },
    {
        "name": "the FRAMEBUFFER flag without all six fields fails",
        "expect": 1,
        "edits": [(BOOT,
                   "        mov     dword [bootinfo + K64BOOTINFO.gop_pixel_format], eax",
                   "        nop")],
        "needle": "store_bootinfo_gop writes gop_pixel_format",
    },
    {
        "name": "a lea LocateProtocol base is not a pointer load",
        "expect": 1,
        "edits": [(BOOT, GOP_LOCATE_BLOCK,
                   GOP_LOCATE_BLOCK.replace(
                       LOAD_BOOT_SERVICES,
                       "        lea     r10, [r10 + EFI_SYSTEM_TABLE.BootServices]"))],
        "needle": "the LocateProtocol base comes from "
                  "EFI_SYSTEM_TABLE.BootServices",
    },
    # ------------------------------------------------------------------
    # task 81: the scan miss paths share .scan_done with the hit path, so
    # SetMode must be provably unreachable without a validated exact match
    {
        "name": "a SetMode reachable without a match fails",
        "expect": 1,
        "edits": [(BOOT, GOP_MATCH_GUARD, "        nop\n        nop")],
        "needle": "FAIL  SetMode is unreachable without a validated exact match",
    },
    {
        "name": "a match marker that is never raised fails",
        "expect": 1,
        "edits": [(BOOT, GOP_MATCH_RAISE, "        nop")],
        "needle": "FAIL  the validated match path still reaches SetMode",
    },
    {
        "name": "a match marker raised before the scan fails",
        "expect": 1,
        "edits": [(BOOT, GOP_MATCH_INIT, "        mov     [gop_match], 1")],
        "needle": "FAIL  exactly one site raises the GOP match marker",
    },
]


def apply_edits(sandbox, edits):
    for relpath, old, new in edits:
        path = os.path.join(sandbox, relpath)
        with open(path) as handle:
            text = handle.read()
        if text.count(old) != 1:
            raise SystemExit("fixture anchor is not unique in %s: %r" %
                             (relpath, old))
        with open(path, "w") as handle:
            handle.write(text.replace(old, new, 1))


def scratch_entries(parent):
    """What is still lying around in one directory, sorted."""
    return sorted(os.listdir(parent)) if os.path.isdir(parent) else []


def run_checker_isolated(checker, args, cwd):
    """Run the checker with TMPDIR pointed at a private directory.

    tempfile reads TMPDIR when the checker process starts, so every scratch
    directory the checker makes is created inside that private parent and
    what is left there afterwards is exactly what leaked.
    """
    parent = tempfile.mkdtemp(prefix="k64-selftest-tmp-")
    env = dict(os.environ)
    env["TMPDIR"] = parent
    try:
        proc = subprocess.run([sys.executable, checker] + args, cwd=cwd,
                              env=env, capture_output=True, text=True)
        left = scratch_entries(parent)
    finally:
        shutil.rmtree(parent, ignore_errors=True)
    return proc, left


def sandbox_repo(prefix, build_script=None):
    """A throw away repository root with the kernel, the checker and a build."""
    sandbox = tempfile.mkdtemp(prefix=prefix)
    shutil.copytree(os.path.join(REPO, "kernel"),
                    os.path.join(sandbox, "kernel"))
    os.makedirs(os.path.join(sandbox, "tools"))
    shutil.copy(CHECKER, os.path.join(sandbox, "tools",
                                     os.path.basename(CHECKER)))
    # The full checker builds a nested bootstrap fixture with the FAT16
    # builder and ROM inputs from its own repository root.
    shutil.copy(os.path.join(REPO, "tools", "build-fat16-image.py"),
                os.path.join(sandbox, "tools", "build-fat16-image.py"))
    chip8 = os.path.join(sandbox, "user", "chip8")
    shutil.copytree(os.path.join(REPO, "user", "chip8", "roms"),
                    os.path.join(chip8, "roms"))
    shutil.copy(os.path.join(REPO, "user", "chip8", "api.inc"), chip8)
    build = os.path.join(sandbox, "build_fasm2.sh")
    if build_script is None:
        shutil.copy(os.path.join(REPO, "build_fasm2.sh"), build)
    else:
        with open(build, "w") as handle:
            handle.write(build_script)
    os.chmod(build, 0o755)
    return sandbox


def report(name, ok, detail):
    print(("PASS  " if ok else "FAIL  ") + name + " (" + detail + ")")
    return ok


def check_full_run_cleanup():
    """A whole passing run must not leave a scratch directory behind."""
    sandbox = sandbox_repo("k64-selftest-pass-")
    try:
        checker = os.path.join(sandbox, "tools",
                               os.path.basename(CHECKER))
        proc, left = run_checker_isolated(
            checker, ["--source", os.path.join(sandbox, ENTRY),
                      "--artifact", ARTIFACT], sandbox)
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
    ok = proc.returncode == 0 and not left
    if not report("a full checker run leaves no scratch directory behind",
                  ok, "status %d, left behind %s" % (proc.returncode, left)):
        print("----- checker output -----")
        print(proc.stdout + proc.stderr)
        print("--------------------------")
    return ok


# A bootstrap that dies before it installs a host: the checker has already
# made its scratch directory when this runs, so the failure path is the one
# under test.
BROKEN_BUILD = "#!/bin/sh\necho 'build_fasm2.sh: forced failure' >&2\nexit 1\n"


def check_failing_run_cleanup():
    """A failing checker run has to clean up just as well."""
    sandbox = sandbox_repo("k64-selftest-fail-", BROKEN_BUILD)
    try:
        checker = os.path.join(sandbox, "tools",
                               os.path.basename(CHECKER))
        proc, left = run_checker_isolated(
            checker, ["--source", os.path.join(sandbox, ENTRY),
                      "--artifact", ARTIFACT], sandbox)
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
    ok = proc.returncode != 0 and not left
    if not report("a failing checker run leaves no scratch directory behind",
                  ok, "status %d, left behind %s" % (proc.returncode, left)):
        print("----- checker output -----")
        print(proc.stdout + proc.stderr)
        print("--------------------------")
    return ok


def load_checker_module():
    """Import the checker without running any of its checks."""
    spec = importlib.util.spec_from_file_location(
        "check_boot_contract_under_test", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_implicit_register_model():
    """Score instruction_writes directly, one instruction and alias per row."""
    module = load_checker_module()
    failed = 0
    for line, register, expected in IMPLICIT_MODEL_CASES:
        got = module.instruction_writes(line, register)
        if got is not expected:
            print("FAIL  instruction_writes(%r, %r) is %r, expected %r" %
                  (line, register, got, expected))
            failed += 1
    total = len(IMPLICIT_MODEL_CASES)
    print(("PASS  " if not failed else "FAIL  ") +
          "%d/%d implicit register model queries" % (total - failed, total))
    return failed, total


def run_fixture(fixture, workdir):
    sandbox = os.path.join(workdir, "repo")
    shutil.copytree(os.path.join(REPO, "kernel"), os.path.join(sandbox, "kernel"))
    apply_edits(sandbox, fixture["edits"])
    proc = subprocess.run(
        [sys.executable, CHECKER, "--source", os.path.join(sandbox, ENTRY),
         "--source-only"],
        capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    ok = proc.returncode == fixture["expect"]
    if ok and fixture.get("needle"):
        ok = fixture["needle"] in out
    if ok and fixture.get("forbidden"):
        ok = fixture["forbidden"] not in out
    print(("PASS  " if ok else "FAIL  ") + fixture["name"] +
          " (expected status %d, got %d)" % (fixture["expect"], proc.returncode))
    if not ok:
        print("----- checker output -----")
        print(out)
        print("--------------------------")
    return ok


def main():
    if not os.path.isfile(CHECKER):
        print("FAIL  missing %s" % CHECKER)
        return 1
    print("--- implicit register model ---")
    model_failed, model_total = check_implicit_register_model()
    print("--- isolated source mutations ---")
    fixture_failed = 0
    for fixture in FIXTURES:
        with tempfile.TemporaryDirectory(prefix="k64-check-selftest-") as workdir:
            if not run_fixture(fixture, workdir):
                fixture_failed += 1
    print("--- scratch directory hygiene ---")
    hygiene = [check_full_run_cleanup(), check_failing_run_cleanup()]
    hygiene_failed = hygiene.count(False)
    print("---")
    print("%d/%d fixtures produced the expected verdict" %
          (len(FIXTURES) - fixture_failed, len(FIXTURES)))
    print("%d/%d cleanup checks passed" %
          (len(hygiene) - hygiene_failed, len(hygiene)))
    print("%d/%d implicit register model queries passed" %
          (model_total - model_failed, model_total))
    return 1 if fixture_failed or hygiene_failed or model_failed else 0


if __name__ == "__main__":
    sys.exit(main())
