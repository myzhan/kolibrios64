;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                              ;;
;; Copyright (C) KolibriOS team 2025-2025. All rights reserved. ;;
;; Distributed under terms of the GNU General Public License v2 ;;
;;                                                              ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;
;; Input layer: private API, contract IRQ-v2 on top of IRQ-v1.
;;
;; Like the CPU, MM and IRQ modules this file repeats every frozen value
;; under the INPUT_ prefix instead of including a shared header. The core
;; contract file compares this copy with the shared K64_* spelling at
;; assembly time. The module is included after irq/irq.asm: it calls
;; vmm_map_mmio, irq_sti_wait, cpu_panic and the bounded console, reads
;; the public IRQ evidence labels (irq_test_ticks, irq_calib_hz) and
;; nothing else outside its own files.
;;
;; Calling convention (Microsoft x64, integer registers only):
;;  - integer arguments in RCX, RDX, R8, R9;
;;  - 32 bytes of shadow space, RSP mod 16 = 0 before a call, no red zone;
;;  - RBX, RBP, RSI, RDI and R12..R15 are callee saved;
;;  - EAX returns a signed status, RDX the primary result;
;;  - DF = 0 on return, IF unchanged by a returning call.
;;
;; The two IRQ handlers are leaves that run with IF = 0 inside the CPU
;; interrupt frame: caller saved registers only, no heap, no unbounded
;; wait, no serial output, no EOI (irq_dispatch owns the EOI). The module
;; contains zero STI instructions: every interrupt window of the self
;; test goes through the exported irq_sti_wait of the IRQ layer.
;;

INPUT_API_VERSION = 1

; ------------------------------------------------------------------
; status codes, EAX, signed, the shared foundation set
; ------------------------------------------------------------------

INPUT_OK            = 0
INPUT_EINVAL        = -1
INPUT_ENOMEM        = -2
INPUT_ENOENT        = -3
INPUT_EEXIST        = -4
INPUT_EPERM         = -5
INPUT_ENOTSUP       = -6
INPUT_ENOTREADY     = -7
INPUT_EALREADY      = -8
INPUT_EIO           = -9
INPUT_EOVERFLOW     = -10
INPUT_EFAULT        = -11
INPUT_EBUSY         = -12

; ------------------------------------------------------------------
; geometry and the private copies of the shared facts this layer reads
; ------------------------------------------------------------------

INPUT_PAGE_SIZE     = 0x1000
INPUT_DATA_ALIGN    = 0x1000

INPUT_PUTS_MAX      = 1024
INPUT_PUTHEX_DIGITS = 16

; ------------------------------------------------------------------
; the IOAPIC register window
;
; The IOAPIC lives at its architectural physical address, mapped through
; vmm_map_mmio at the frozen MMIO VA (PCD|RW|NX, one 4 KiB page). Access
; is indirect: IOREGSEL selects the register, IOWIN reads or writes it.
; ------------------------------------------------------------------

INPUT_IOAPIC_PA     = 0xFEC00000
INPUT_IOAPIC_VA     = 0xFFFFC00003000000

INPUT_IOAPIC_REGSEL = 0x00
INPUT_IOAPIC_WIN    = 0x10

INPUT_IOAPIC_IDX_VER    = 0x01
INPUT_IOAPIC_IDX_KBD_LO = 0x10 + 2 * 1  ; pin 1,  low dword (0x12)
INPUT_IOAPIC_IDX_KBD_HI = 0x10 + 2 * 1 + 1 ; pin 1,  high dword (0x13)
INPUT_IOAPIC_IDX_MOUSE_LO = 0x10 + 2 * 12 ; pin 12, low dword (0x28)
INPUT_IOAPIC_IDX_MOUSE_HI = 0x10 + 2 * 12 + 1 ; pin 12, high dword (0x29)

; the frozen vectors, private copies of the IRQ layer contract
INPUT_VEC_KEYBOARD  = 0x21
INPUT_VEC_MOUSE     = 0x2C

; RTE low dword: bit 16 masks, bit 13 polarity (0 = active high),
; bit 15 trigger (0 = edge), bits 10..8 delivery mode Fixed (0),
; bit 11 destination mode Physical (0), low byte the vector.
; Bring-up writes the masked form, high dword (destination) first.
INPUT_RTE_MASK      = 0x10000
INPUT_RTE_KBD_MASKED   = INPUT_RTE_MASK or INPUT_VEC_KEYBOARD
INPUT_RTE_MOUSE_MASKED = INPUT_RTE_MASK or INPUT_VEC_MOUSE
INPUT_RTE_KBD_LIVE     = INPUT_VEC_KEYBOARD
INPUT_RTE_MOUSE_LIVE   = INPUT_VEC_MOUSE

; sanity floors for the measured VER register, never hardcoded exact
; values: a real IOAPIC reports version >= 0x10 and enough redirection
; entries to cover pin 12 (RTE index 12, the register pair 0x28/0x29,
; so at least 13 entries)
INPUT_VER_MIN       = 0x10
INPUT_RTE_MIN       = 13

; ------------------------------------------------------------------
; the 8042 PS/2 controller
; ------------------------------------------------------------------

INPUT_PS2_DATA      = 0x60
INPUT_PS2_STATUS    = 0x64
INPUT_PS2_CMD       = 0x64

INPUT_PS2_STAT_OBF  = 0x01              ; output buffer full, data ready
INPUT_PS2_STAT_IBF  = 0x02              ; input buffer full, wait
; bit 5 marks the output buffer byte as AUX (mouse) sourced; 0 means
; keyboard sourced. Each handler consumes ONLY its own source: the
; keyboard leaf returns without reading 0x60 when bit 5 is set, the
; mouse leaf returns when it is clear. The unconsumed byte keeps OBF
; full, so the 8042 keeps the other port's IRQ line asserted (a masked
; RTE latches the edge in the IOAPIC IRR), and the owning handler
; consumes it after its own delivery: no byte is lost, no byte is
; counted by the wrong stream, the frozen counts stay derived.
INPUT_PS2_STAT_AUX  = 0x20              ; OBF byte came from the AUX port

INPUT_PS2_CMD_READ_CCB   = 0x20
INPUT_PS2_CMD_WRITE_CCB  = 0x60
INPUT_PS2_CMD_DISABLE_KBD = 0xAD
INPUT_PS2_CMD_DISABLE_AUX = 0xA7
INPUT_PS2_CMD_ENABLE_KBD  = 0xAE
INPUT_PS2_CMD_ENABLE_AUX  = 0xA8
INPUT_PS2_CMD_WRITE_AUX   = 0xD4        ; next data byte goes to the mouse

; command configuration byte: bit 0 keyboard interrupt enable, bit 1
; mouse interrupt enable, bit 4 keyboard clock disable, bit 5 mouse
; clock disable, bit 6 set 2 -> set 1 translation for CPU reads.
; Bring-up forces KIE|MIE|translation on and both clock disables off.
INPUT_CCB_SET       = 0x43
INPUT_CCB_CLEAR     = 0x30
INPUT_CCB_CHECK_MASK  = 0x73            ; bits 0,1,4,5,6
INPUT_CCB_CHECK_VALUE = 0x43            ; KIE=1 MIE=1 clocks on translation on

INPUT_MOUSE_CMD_RESET   = 0xFF
INPUT_MOUSE_CMD_DEFAULT = 0xF6
INPUT_MOUSE_CMD_ENABLE  = 0xF4
INPUT_MOUSE_ACK         = 0xFA
INPUT_MOUSE_RESEND      = 0xFE
INPUT_MOUSE_RESET_OK    = 0xAA
INPUT_MOUSE_ID_STANDARD = 0x00

; ------------------------------------------------------------------
; bounded budgets, every loop in this module is finite
;
; PORT_SPIN covers one 8042 status poll under TCG jitter; FLUSH_MAX caps
; the drain loop; ACK_RETRY caps the 0xFE resend protocol. The self test
; windows are sized for the 0.5 s host polling cadence plus the 100 ms
; sendkey hold: POLL_CHUNK * POLL_ROUNDS is the total injection budget,
; TIMER_WINDOW spans at least two 10 ms tick periods like IRQ_ST_WINDOW.
; ------------------------------------------------------------------

INPUT_PORT_SPIN     = 0x100000
INPUT_FLUSH_MAX     = 64
INPUT_ACK_RETRY     = 5
INPUT_WAIT_SPIN     = 0x70000000
INPUT_POLL_CHUNK    = 0x2000000
INPUT_POLL_ROUNDS   = 64
INPUT_TIMER_WINDOW  = 0x1000000

; ------------------------------------------------------------------
; keyboard and mouse decoding facts
; ------------------------------------------------------------------

INPUT_KBD_CAP       = 120               ; key buffer entries

; rax = queue sequence on entry, packed slot byte offset on return.
; Clobbers rcx and rdx. Producer and consumers run with IF clear.
macro input_kbd_slot
        xor     edx, edx
        mov     ecx, INPUT_KBD_CAP
        div     rcx
        mov     rax, rdx
        shl     rax, 4
end macro
INPUT_KBD_ENTRY     = 16                ; ascii qword + raw scancode qword
INPUT_SCANCODE_E0   = 0xE0
; 0xE1 introduces the Pause sequence (E1 1D 45, break E1 9D C5): the
; two payload bytes are swallowed raw, they must never enter the
; modifier state machine (E1 1D would otherwise pollute CTRL)
INPUT_SCANCODE_E1   = 0xE1
INPUT_SCANCODE_BREAK = 0x80
INPUT_RAW_E0_FLAG   = 0x100             ; extended keys store raw | 0x100

if defined K64_RUNTIME
INPUT_MOUSE_EDGE_CAP = 32              ; power of two, keeps the newest edges
end if

; modifier bits of input_kbd_mods
INPUT_MOD_LSHIFT    = 0x01
INPUT_MOD_RSHIFT    = 0x02
INPUT_MOD_CTRL      = 0x04
INPUT_MOD_ALT       = 0x08
INPUT_MOD_CAPS      = 0x10

; set 1 make codes of the modifiers, breaks are make | 0x80
INPUT_SC_LSHIFT     = 0x2A
INPUT_SC_RSHIFT     = 0x36
INPUT_SC_CTRL       = 0x1D
INPUT_SC_ALT        = 0x38
INPUT_SC_CAPS       = 0x3A

; mouse packet: byte 1 bit 3 always set, bits 0..2 buttons, bit 4/5 the
; X/Y sign bits, bit 6/7 the X/Y overflow flags: an overflow byte means
; the deltas are unreliable, the whole packet is dropped and counted;
; coordinates accumulate in screen space (Y down) and clamp to the
; frozen 1024x768 desktop geometry
INPUT_MOUSE_SYNC    = 0x08
INPUT_MOUSE_SIGN_X  = 0x10
INPUT_MOUSE_SIGN_Y  = 0x20
INPUT_MOUSE_OVERFLOW = 0xC0             ; byte 1 bits 6/7, X/Y overflow
INPUT_MOUSE_XMAX    = 1023
INPUT_MOUSE_YMAX    = 767
INPUT_MOUSE_XINIT   = 512
INPUT_MOUSE_YINIT   = 384

; the frozen host injection of the self test: sendkey a, mouse_move 5 3,
; mouse_button 1 then 0. DY is the PS/2 packet byte (up positive), the
; monitor dy is screen space (down positive), hence the negation.
INPUT_ST_KBD_SCANCODE = 0x1E            ; 'a' make, break 0x9E
INPUT_ST_MOUSE_DX   = 5
INPUT_ST_MOUSE_DY   = -3

; byte 1 of the move packet, DERIVED from the frozen deltas: the sync
; bit plus the sign bit of every negative delta. QEMU sets bit 5 for
; the negative PS/2 Y (measured byte 1 = 0x28 for mouse_move 5 3, task
; 89 early experiment); the sign bits are part of the standard packet,
; never magic constants. The button packets carry zero deltas and QEMU
; may still set a sign bit on a zero delta, so the final packet check
; masks the sign bits out instead of demanding an exact byte 1.
INPUT_ST_MOUSE_B1 = INPUT_MOUSE_SYNC
if INPUT_ST_MOUSE_DX < 0
        INPUT_ST_MOUSE_B1 = INPUT_ST_MOUSE_B1 or INPUT_MOUSE_SIGN_X
end if
if INPUT_ST_MOUSE_DY < 0
        INPUT_ST_MOUSE_B1 = INPUT_ST_MOUSE_B1 or INPUT_MOUSE_SIGN_Y
end if
INPUT_MOUSE_B1_MASK = 0xFF and not (INPUT_MOUSE_SIGN_X or INPUT_MOUSE_SIGN_Y)
; frozen verdict semantics: the release packet assertion masks the two
; sign bits out of byte 1 (0xFFFFCF keeps sync/buttons and both delta
; bytes exact). A sign bit on a zero delta carries no information, so
; it must not fail the leg; conversely the mask must NEVER be widened
; (dropping the sync/button/delta bits would silently weaken the gate).
INPUT_ST_MOUSE_LAST_MASK = (0xFF shl 16) or (0xFF shl 8) or INPUT_MOUSE_B1_MASK

; ------------------------------------------------------------------
; the host handshake stages of input_test_stage
;
; The guest publishes the stage, the host polls it through the gdbstub
; and injects through the monitor; every guest wait is bounded and a
; timeout is a self test failure, never a silent pass.
; ------------------------------------------------------------------

INPUT_STAGE_IDLE        = 0
INPUT_STAGE_KBD_MASKED  = 1             ; host: sendkey b
INPUT_STAGE_KBD_LIVE    = 2             ; host: sendkey a
INPUT_STAGE_MOUSE_MOVE  = 3             ; host: mouse_move 5 3
INPUT_STAGE_MOUSE_BUTTON = 4            ; host: mouse_button 1, then 0
INPUT_STAGE_TIMER       = 5             ; no host action
INPUT_STAGE_DONE        = 6

; frozen final counts, DETERMINISTICALLY derived from the injection
; sequence, never from a runtime observation:
;  - kbd = 2: leg 2 drains BOTH bytes of sendkey b under the mask (they
;    contribute 0), leg 3 counts exactly the make + break of sendkey a;
;  - mouse = 3: one move packet + one press packet + one release
;    packet; the host sends the release only after mouse_count >= 2,
;    so QEMU can never coalesce press+release into one packet;
;  - ticks = 11: the IRQ-v1 exit value 10 plus EXACTLY one tick of the
;    masked-oneshot leg. Structural reason it is exact under both QEMU
;    masked-expiry behaviors: (a) expiry discarded -> the unmask+re-arm
;    delivers one tick; (b) expiry latched in IRR -> the unmask
;    delivers the phantom as tick 11 and the mode-0 wait exits within
;    a few instructions, masking the LVT long before the re-armed
;    10 ms countdown could deliver a 12th. No timing window is relied
;    on: the one-shot fires at most once per arm and the LVT is masked
;    immediately after the wait returns.
INPUT_ST_KBD_FINAL   = 2
INPUT_ST_MOUSE_FINAL = 3
INPUT_IRQ_TICKS_BASE = 10               ; irq_selftest exit contract
INPUT_IRQ_TICKS_FINAL = INPUT_IRQ_TICKS_BASE + 1

; private copies of the IRQ LAPIC facts the timer leg uses
INPUT_LAPIC_VA        = 0xFFFFC00002000000
INPUT_LAPIC_LVT_TIMER = 0x320
INPUT_LAPIC_INIT_COUNT = 0x380
INPUT_LAPIC_DIVIDE    = 0x3E0
INPUT_LVT_MASK        = 0x10000
INPUT_LVT_ONESHOT     = 0x00000
INPUT_DIVIDE_16       = 0x3
INPUT_VEC_TIMER       = 0x20
INPUT_TICK_HZ         = 100

; self test leg numbers, the failure mask is a qword
INPUT_ST_BIT_IOAPIC     = 0
INPUT_ST_BIT_PS2        = 1
INPUT_ST_BIT_KBD_MASKED = 2
INPUT_ST_BIT_KBD_LIVE   = 3
INPUT_ST_BIT_MOUSE      = 4
INPUT_ST_BIT_TIMER      = 5
INPUT_ST_BIT_FINAL      = 6
INPUT_ST_ALL            = (1 shl (INPUT_ST_BIT_FINAL + 1)) - 1

; ------------------------------------------------------------------
; the static data region layout, offsets from input_data_start
; ------------------------------------------------------------------

INPUT_DATA_OFF_STATE         = 0
INPUT_DATA_OFF_TEST_MASK     = 8
INPUT_DATA_OFF_TEST_KBD      = 16
INPUT_DATA_OFF_TEST_MOUSE    = 24
INPUT_DATA_OFF_TEST_STAGE    = 32
INPUT_DATA_OFF_MOUSE_LAST    = 40
INPUT_DATA_OFF_MOUSE_BUTTONS = 48
INPUT_DATA_OFF_IOAPIC_VER    = 56
INPUT_DATA_OFF_IOAPIC_MAX    = 64
INPUT_DATA_OFF_MOUSE_ID      = 72
INPUT_DATA_OFF_KBD_MODS      = 80
INPUT_DATA_OFF_KBD_E0        = 88
INPUT_DATA_OFF_KBD_USED      = 96
INPUT_DATA_OFF_KBD_DROPS     = 104
INPUT_DATA_OFF_MOUSE_STATE   = 112
INPUT_DATA_OFF_MOUSE_B1      = 120
INPUT_DATA_OFF_MOUSE_B2      = 128
INPUT_DATA_OFF_MOUSE_BAD     = 136
INPUT_DATA_OFF_MOUSE_X       = 144
INPUT_DATA_OFF_MOUSE_Y       = 152
INPUT_DATA_OFF_KBD_E1        = 160
INPUT_DATA_OFF_KBD_BUF       = 168
INPUT_DATA_OFF_MESSAGES      = INPUT_DATA_OFF_KBD_BUF + INPUT_KBD_CAP * INPUT_KBD_ENTRY

virtual at 0
input_data_layout:
i_state:
        dq      ?
i_test_mask:
        dq      ?
i_test_kbd:
        dq      ?
i_test_mouse:
        dq      ?
i_test_stage:
        dq      ?
i_mouse_last:
        dq      ?
i_mouse_buttons:
        dq      ?
i_ioapic_ver:
        dq      ?
i_ioapic_max:
        dq      ?
i_mouse_id:
        dq      ?
i_kbd_mods:
        dq      ?
i_kbd_e0:
        dq      ?
i_kbd_used:
        dq      ?
i_kbd_drops:
        dq      ?
i_mouse_state:
        dq      ?
i_mouse_b1:
        dq      ?
i_mouse_b2:
        dq      ?
i_mouse_bad:
        dq      ?
i_mouse_x:
        dq      ?
i_mouse_y:
        dq      ?
i_kbd_e1:
        dq      ?
i_kbd_buf:
        dq      INPUT_KBD_CAP * 2 dup ?
input_data_layout_end:
end virtual

if i_state - input_data_layout <> INPUT_DATA_OFF_STATE
        err 'INPUT data state offset'
end if
if i_test_mask - input_data_layout <> INPUT_DATA_OFF_TEST_MASK
        err 'INPUT data selftest mask offset'
end if
if i_test_kbd - input_data_layout <> INPUT_DATA_OFF_TEST_KBD
        err 'INPUT data keyboard count offset'
end if
if i_test_mouse - input_data_layout <> INPUT_DATA_OFF_TEST_MOUSE
        err 'INPUT data mouse count offset'
end if
if i_test_stage - input_data_layout <> INPUT_DATA_OFF_TEST_STAGE
        err 'INPUT data handshake stage offset'
end if
if i_mouse_last - input_data_layout <> INPUT_DATA_OFF_MOUSE_LAST
        err 'INPUT data last mouse packet offset'
end if
if i_mouse_buttons - input_data_layout <> INPUT_DATA_OFF_MOUSE_BUTTONS
        err 'INPUT data mouse buttons offset'
end if
if i_ioapic_ver - input_data_layout <> INPUT_DATA_OFF_IOAPIC_VER
        err 'INPUT data IOAPIC version offset'
end if
if i_ioapic_max - input_data_layout <> INPUT_DATA_OFF_IOAPIC_MAX
        err 'INPUT data IOAPIC entry count offset'
end if
if i_mouse_id - input_data_layout <> INPUT_DATA_OFF_MOUSE_ID
        err 'INPUT data mouse id offset'
end if
if i_kbd_mods - input_data_layout <> INPUT_DATA_OFF_KBD_MODS
        err 'INPUT data modifier state offset'
end if
if i_kbd_e0 - input_data_layout <> INPUT_DATA_OFF_KBD_E0
        err 'INPUT data E0 prefix state offset'
end if
if i_kbd_used - input_data_layout <> INPUT_DATA_OFF_KBD_USED
        err 'INPUT data buffer fill offset'
end if
if i_kbd_drops - input_data_layout <> INPUT_DATA_OFF_KBD_DROPS
        err 'INPUT data buffer drop count offset'
end if
if i_mouse_state - input_data_layout <> INPUT_DATA_OFF_MOUSE_STATE
        err 'INPUT data packet state offset'
end if
if i_mouse_b1 - input_data_layout <> INPUT_DATA_OFF_MOUSE_B1
        err 'INPUT data packet byte 1 offset'
end if
if i_mouse_b2 - input_data_layout <> INPUT_DATA_OFF_MOUSE_B2
        err 'INPUT data packet byte 2 offset'
end if
if i_mouse_bad - input_data_layout <> INPUT_DATA_OFF_MOUSE_BAD
        err 'INPUT data bad packet count offset'
end if
if i_mouse_x - input_data_layout <> INPUT_DATA_OFF_MOUSE_X
        err 'INPUT data X coordinate offset'
end if
if i_mouse_y - input_data_layout <> INPUT_DATA_OFF_MOUSE_Y
        err 'INPUT data Y coordinate offset'
end if
if i_kbd_e1 - input_data_layout <> INPUT_DATA_OFF_KBD_E1
        err 'INPUT data E1 prefix state offset'
end if
if i_kbd_buf - input_data_layout <> INPUT_DATA_OFF_KBD_BUF
        err 'INPUT data key buffer offset'
end if
if input_data_layout_end - input_data_layout <> INPUT_DATA_OFF_MESSAGES
        err 'the INPUT data block must end where the messages start'
end if

; ------------------------------------------------------------------
; self consistency of the frozen facts
; ------------------------------------------------------------------

if INPUT_VEC_KEYBOARD <> 0x21 | INPUT_VEC_MOUSE <> 0x2C
        err 'the input vectors are frozen at 0x21 and 0x2C'
end if
if INPUT_IOAPIC_PA <> 0xFEC00000 | INPUT_IOAPIC_VA <> 0xFFFFC00003000000
        err 'the IOAPIC addresses are frozen'
end if
if INPUT_IOAPIC_REGSEL <> 0x00 | INPUT_IOAPIC_WIN <> 0x10
        err 'IOREGSEL/IOWIN offsets'
end if
if INPUT_IOAPIC_IDX_KBD_LO <> 0x12 | INPUT_IOAPIC_IDX_KBD_HI <> 0x13
        err 'pin 1 RTE register pair'
end if
if INPUT_IOAPIC_IDX_MOUSE_LO <> 0x28 | INPUT_IOAPIC_IDX_MOUSE_HI <> 0x29
        err 'pin 12 RTE register pair'
end if
if INPUT_IOAPIC_IDX_KBD_LO <> 0x10 + 2 * 1 | INPUT_IOAPIC_IDX_MOUSE_LO <> 0x10 + 2 * 12
        err 'RTE indices must derive from the pin numbers'
end if
if INPUT_IOAPIC_IDX_KBD_HI <> INPUT_IOAPIC_IDX_KBD_LO + 1 | INPUT_IOAPIC_IDX_MOUSE_HI <> INPUT_IOAPIC_IDX_MOUSE_LO + 1
        err 'RTE high dword follows the low dword'
end if
if INPUT_RTE_KBD_MASKED <> 0x10021 | INPUT_RTE_MOUSE_MASKED <> 0x1002C
        err 'masked RTE values: Fixed/Physical/active-high/edge/masked'
end if
if INPUT_RTE_KBD_MASKED and (0x2000 or 0x8000 or 0x700 or 0x800)
        err 'RTE polarity/trigger/delivery/destination bits must stay clear'
end if
if INPUT_CCB_SET <> 0x43 | INPUT_CCB_CLEAR <> 0x30
        err 'CCB bring-up forces KIE|MIE|translation, clears both clock disables'
end if
if INPUT_CCB_CHECK_MASK <> 0x73 | INPUT_CCB_CHECK_VALUE <> 0x43
        err 'CCB read back checks bits 0,1,4,5,6'
end if
if INPUT_MOUSE_SYNC <> 0x08
        err 'mouse packet byte 1 bit 3 is the sync check'
end if
if INPUT_PS2_STAT_AUX <> 0x20
        err 'status bit 5 is the AUX source flag both handlers filter on'
end if
if INPUT_SCANCODE_E1 <> 0xE1
        err 'the Pause prefix is 0xE1'
end if
if INPUT_MOUSE_OVERFLOW <> 0xC0 | INPUT_MOUSE_OVERFLOW and (INPUT_MOUSE_SYNC or INPUT_MOUSE_SIGN_X or INPUT_MOUSE_SIGN_Y)
        err 'the overflow mask is byte 1 bits 6/7, disjoint from sync/sign'
end if
if INPUT_ST_MOUSE_B1 <> 0x28
        err 'the derived move byte 1 must match the measured QEMU packet (sync + Y sign)'
end if
if INPUT_MOUSE_B1_MASK <> 0xCF | INPUT_ST_MOUSE_LAST_MASK <> 0xFFFFCF
        err 'the final packet mask keeps sync/buttons/deltas, drops the sign bits'
end if
if INPUT_KBD_CAP <> 120 | INPUT_KBD_ENTRY <> 16
        err 'the key buffer is 120 entries of 16 bytes'
end if
if INPUT_MOUSE_XMAX <> 1023 | INPUT_MOUSE_YMAX <> 767
        err 'the coordinate clamp is the frozen 1024x768 geometry'
end if
if INPUT_ACK_RETRY <> 5 | INPUT_FLUSH_MAX <> 64
        err 'the resend cap is 5 and the flush cap is 64'
end if
if INPUT_POLL_CHUNK * INPUT_POLL_ROUNDS < INPUT_WAIT_SPIN
        err 'the injection budget must not undercut the IRQ wait budget'
end if
if INPUT_TIMER_WINDOW < 0x1000000
        err 'the timer mask window spans at least two tick periods'
end if
if INPUT_ST_KBD_FINAL <> 2 | INPUT_ST_MOUSE_FINAL <> 3
        err 'the frozen final counts derive from the injection sequence'
end if
if INPUT_IRQ_TICKS_BASE <> 10 | INPUT_IRQ_TICKS_FINAL <> 11
        err 'the timer leg adds exactly one tick to the IRQ-v1 exit value'
end if
if INPUT_STAGE_DONE <> 6
        err 'the handshake protocol ends at stage 6'
end if
if INPUT_ST_ALL <> 0x7F
        err 'the self test mask covers seven legs'
end if
if INPUT_LAPIC_VA <> 0xFFFFC00002000000 | INPUT_LAPIC_LVT_TIMER <> 0x320
        err 'the private IRQ LAPIC copies drifted'
end if
if INPUT_LAPIC_INIT_COUNT <> 0x380 | INPUT_LAPIC_DIVIDE <> 0x3E0
        err 'the private IRQ timer count/divide copies drifted'
end if
if INPUT_LVT_MASK <> 0x10000 | INPUT_VEC_TIMER <> 0x20 | INPUT_TICK_HZ <> 100
        err 'the private IRQ mask/vector/tick copies drifted'
end if
if INPUT_DIVIDE_16 <> 0x3
        err 'divide by 16 must be encoded as 0x3'
end if
if INPUT_PUTS_MAX <> 1024 | INPUT_PUTHEX_DIGITS <> 16
        err 'the private console caps drifted'
end if

; ------------------------------------------------------------------
; register helper macros, the same full frame the IRQ module uses
; ------------------------------------------------------------------

macro input_enter
        push    rbp
        push    rbx
        push    rsi
        push    rdi
        push    r12
        push    r13
        push    r14
        push    r15
        sub     rsp, 40
end macro

macro input_leave
        add     rsp, 40
        pop     r15
        pop     r14
        pop     r13
        pop     r12
        pop     rdi
        pop     rsi
        pop     rbx
        pop     rbp
end macro
