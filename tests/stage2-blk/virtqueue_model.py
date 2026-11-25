#!/usr/bin/env python3
"""Pure Python reference model of the FS-v1 legacy virtio-blk virtqueue.

Task 130 (FS-v1 model first). Independent implementation of the split
virtqueue semantics of virtio 0.9.5 / virtio 1.0 transitional legacy
PIO, plus the VIRTIO_BLK_T_IN request layout consumed by the future
kernel/blk driver (M1). No machine is started, no third-party imports.

Memory model: a virtqueue lives inside one flat bytearray image; every
descriptor addr field is modeled as an offset into that same image, so
the whole ring plus header/data/status buffers can be inspected byte by
byte exactly like the guest physical page the driver will hand over via
VIRTIO_PCI_QUEUE_PFN.

Covered semantics (each has a positive test and, where applicable, a
negative fixture that must be judged as an error):

  * legacy split layout: desc table 16*qnum, avail ring 6+2*qnum right
    behind it, used ring 6+8*qnum padded to the legacy 4096 queue align;
  * desc flags NEXT/WRITE, chain walking with cycle and length bounds;
  * avail/used idx are free-running u16 counters, monotonic modulo 65536,
    ring slots indexed by idx % qnum;
  * VIRTIO_BLK_T_IN three-part request: 16 B header (type=0, reserved,
    sector LBA) -> device-writable data (multiple of 512) -> 1 B
    device-writable status (0=OK, 1=IOERR, 2=UNSUPP);
  * used.len counts device-written bytes only (data+status on success,
    status alone on error);
  * read-only backend: T_OUT is refused with UNSUPP, T_FLUSH succeeds;
  * negative fixtures: bad desc idx, out-of-range LBA, missing device,
    descriptor chain loop, non-writable data/status, misaligned data
    length, short header, out-of-image addr, avail idx running ahead.

The legacy PIO register map (BAR0) and PCI identity constants below are
the hand-written contract copy for the M1 gate; they are frozen here
independently of any assembly source, per the project gate style.

Running this file executes the full self-test suite and writes evidence
to build/stage2-blk/model-selftest.log.
"""

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "build" / "stage2-blk"

# ----------------------------------------------------------------------
# contract FS-v1 blk, fixed here independently of the future sources
# ----------------------------------------------------------------------

SECTOR_SIZE = 512
BLK_HEADER_SIZE = 16                    # type u32, reserved u32, sector u64
BLK_STATUS_SIZE = 1

# PCI identity of virtio-blk-pci-transitional (virtio 1.0 4.1.2)
VIRTIO_PCI_VENDOR = 0x1AF4
VIRTIO_PCI_DEVICE_BLK_TRANSITIONAL = 0x1001
VIRTIO_PCI_SUBVENDOR = 0x1AF4
VIRTIO_PCI_SUBDEVICE_BLK = 0x0002
VIRTIO_PCI_REVISION_LEGACY = 0x00
VIRTIO_PCI_CLASS_BLK = 0x018000         # mass storage / other

# legacy PIO register offsets inside BAR0 (virtio 0.9.5 layout)
VIRTIO_PCI_HOST_FEATURES = 0x00         # u32 R
VIRTIO_PCI_GUEST_FEATURES = 0x04        # u32 RW
VIRTIO_PCI_QUEUE_PFN = 0x08             # u32 RW, queue base >> 12
VIRTIO_PCI_QUEUE_NUM = 0x0C             # u16 R
VIRTIO_PCI_QUEUE_SEL = 0x0E             # u16 W
VIRTIO_PCI_QUEUE_NOTIFY = 0x10          # u16 W
VIRTIO_PCI_STATUS = 0x12                # u8 RW
VIRTIO_PCI_ISR = 0x13                   # u8 R, read-clear
VIRTIO_PCI_CONFIG_MSIX_VECTOR = 0x14    # u16 RW, only if MSI-X enabled
VIRTIO_PCI_QUEUE_MSIX_VECTOR = 0x16     # u16 RW, only if MSI-X enabled
VIRTIO_PCI_CONFIG_OFF = 0x14            # device config, MSI-X disabled
VIRTIO_PCI_CONFIG_OFF_MSIX = 0x18       # device config, MSI-X enabled

# name -> (offset, width bytes, access), the frozen contract table
PIO_REGS = {
    "host_features": (VIRTIO_PCI_HOST_FEATURES, 4, "R"),
    "guest_features": (VIRTIO_PCI_GUEST_FEATURES, 4, "RW"),
    "queue_pfn": (VIRTIO_PCI_QUEUE_PFN, 4, "RW"),
    "queue_num": (VIRTIO_PCI_QUEUE_NUM, 2, "R"),
    "queue_sel": (VIRTIO_PCI_QUEUE_SEL, 2, "W"),
    "queue_notify": (VIRTIO_PCI_QUEUE_NOTIFY, 2, "W"),
    "status": (VIRTIO_PCI_STATUS, 1, "RW"),
    "isr": (VIRTIO_PCI_ISR, 1, "R"),
}

# device status bits (legacy: no FEATURES_OK)
VIRTIO_STATUS_ACKNOWLEDGE = 0x01
VIRTIO_STATUS_DRIVER = 0x02
VIRTIO_STATUS_DRIVER_OK = 0x04
VIRTIO_STATUS_FAILED = 0x80

# virtio-blk device config, offsets relative to VIRTIO_PCI_CONFIG_OFF
VIRTIO_BLK_CONFIG_CAPACITY = 0          # u64, in 512 B sectors
VIRTIO_BLK_CONFIG_SIZE_MAX = 8          # u32
VIRTIO_BLK_CONFIG_SEG_MAX = 12          # u32
VIRTIO_BLK_CONFIG_CYLINDERS = 16        # u16
VIRTIO_BLK_CONFIG_HEADS = 18            # u8
VIRTIO_BLK_CONFIG_SECTORS = 19          # u8
VIRTIO_BLK_CONFIG_BLK_SIZE = 20         # u32

# request types and status bytes
VIRTIO_BLK_T_IN = 0
VIRTIO_BLK_T_OUT = 1
VIRTIO_BLK_T_FLUSH = 4
VIRTIO_BLK_T_GET_ID = 8
VIRTIO_BLK_S_OK = 0
VIRTIO_BLK_S_IOERR = 1
VIRTIO_BLK_S_UNSUPP = 2

# split virtqueue constants
VRING_DESC_F_NEXT = 0x1
VRING_DESC_F_WRITE = 0x2
VRING_DESC_F_INDIRECT = 0x4
VRING_AVAIL_F_NO_INTERRUPT = 0x1
VRING_USED_F_NO_NOTIFY = 0x1
VIRTQ_LEGACY_ALIGN = 4096               # legacy queue align, used ring pad
DESC_SIZE = 16
U16_MASK = 0xFFFF


class VirtqueueError(Exception):
    """A request or ring state the device must judge as an error."""


def align_up(value, alignment):
    return (value + alignment - 1) // alignment * alignment


def vring_layout(qnum):
    """Legacy split layout offsets/size for a power-of-two queue size."""
    if qnum < 1 or qnum > 32768 or qnum & (qnum - 1):
        raise VirtqueueError(f"queue size {qnum} is not a power of two <= 32768")
    desc_off = 0
    avail_off = DESC_SIZE * qnum
    used_off = align_up(avail_off + 6 + 2 * qnum, VIRTQ_LEGACY_ALIGN)
    total = used_off + 6 + 8 * qnum
    return {"qnum": qnum, "desc_off": desc_off, "avail_off": avail_off,
            "used_off": used_off, "total": total}


class Virtqueue:
    """Typed accessors over the three rings inside a flat memory image."""

    def __init__(self, mem, base=0, qnum=8):
        lay = vring_layout(qnum)
        if base < 0 or base + lay["total"] > len(mem):
            raise VirtqueueError("virtqueue does not fit the memory image")
        self.mem = mem
        self.base = base
        self.qnum = qnum
        self.desc_off = base + lay["desc_off"]
        self.avail_off = base + lay["avail_off"]
        self.used_off = base + lay["used_off"]
        self.total = lay["total"]

    # descriptor table: addr u64, len u32, flags u16, next u16
    def desc(self, i):
        if not 0 <= i < self.qnum:
            raise VirtqueueError(f"desc index {i} out of range")
        return struct.unpack_from("<QIHH", self.mem, self.desc_off + DESC_SIZE * i)

    def set_desc(self, i, addr, length, flags=0, nxt=0):
        if not 0 <= i < self.qnum:
            raise VirtqueueError(f"desc index {i} out of range")
        struct.pack_into("<QIHH", self.mem, self.desc_off + DESC_SIZE * i,
                         addr, length, flags, nxt)

    # avail ring: flags u16, idx u16, ring[qnum] u16
    def avail_flags(self):
        return struct.unpack_from("<H", self.mem, self.avail_off)[0]

    def set_avail_flags(self, value):
        struct.pack_into("<H", self.mem, self.avail_off, value & U16_MASK)

    def avail_idx(self):
        return struct.unpack_from("<H", self.mem, self.avail_off + 2)[0]

    def set_avail_idx(self, value):
        struct.pack_into("<H", self.mem, self.avail_off + 2, value & U16_MASK)

    def avail_ring(self, slot):
        return struct.unpack_from("<H", self.mem, self.avail_off + 4 + 2 * slot)[0]

    def set_avail_ring(self, slot, head):
        struct.pack_into("<H", self.mem, self.avail_off + 4 + 2 * slot, head & U16_MASK)

    # used ring: flags u16, idx u16, ring[qnum] of {id u32, len u32}
    def used_flags(self):
        return struct.unpack_from("<H", self.mem, self.used_off)[0]

    def set_used_flags(self, value):
        struct.pack_into("<H", self.mem, self.used_off, value & U16_MASK)

    def used_idx(self):
        return struct.unpack_from("<H", self.mem, self.used_off + 2)[0]

    def set_used_idx(self, value):
        struct.pack_into("<H", self.mem, self.used_off + 2, value & U16_MASK)

    def used_entry(self, slot):
        return struct.unpack_from("<II", self.mem, self.used_off + 4 + 8 * slot)

    def set_used_entry(self, slot, head, length):
        struct.pack_into("<II", self.mem, self.used_off + 4 + 8 * slot,
                         head & 0xFFFFFFFF, length & 0xFFFFFFFF)


class BlkDevice:
    """Polling legacy virtio-blk device over one virtqueue, read-only.

    disk is the backend byte string (a multiple of 512); None models a
    missing device. last_avail is the device-private free-running u16
    consume counter, exactly like the driver-side last_used counter.
    """

    def __init__(self, disk):
        if disk is not None:
            disk = bytes(disk)
            if len(disk) % SECTOR_SIZE:
                raise VirtqueueError("backend size is not a multiple of 512")
        self.disk = disk
        self.last_avail = 0

    @property
    def capacity_sectors(self):
        return 0 if self.disk is None else len(self.disk) // SECTOR_SIZE

    def process(self, vq):
        """Consume every new avail entry, publish used entries, return
        one result dict per request. Ring-level defects raise
        VirtqueueError; LBA range errors surface as status IOERR."""
        if self.disk is None:
            raise VirtqueueError("device missing: no backend disk")
        avail_idx = vq.avail_idx()
        pending = (avail_idx - self.last_avail) & U16_MASK
        if pending > vq.qnum:
            raise VirtqueueError("avail idx runs ahead of the queue size")
        results = []
        for _ in range(pending):
            head = vq.avail_ring(self.last_avail % vq.qnum)
            results.append(self._one(vq, head))
            self.last_avail = (self.last_avail + 1) & U16_MASK
        return results

    def _walk(self, vq, head):
        if head >= vq.qnum:
            raise VirtqueueError(f"bad desc idx {head} >= queue size {vq.qnum}")
        chain = []
        seen = set()
        i = head
        while True:
            if i >= vq.qnum:
                raise VirtqueueError(f"bad desc idx {i} in chain")
            if i in seen:
                raise VirtqueueError(f"desc chain loop at {i}")
            seen.add(i)
            addr, length, flags, nxt = vq.desc(i)
            if flags & VRING_DESC_F_INDIRECT:
                raise VirtqueueError("indirect descriptors unsupported in FS-v1")
            if addr + length > len(vq.mem):
                raise VirtqueueError(f"desc {i} addr+len exceeds the memory image")
            chain.append((addr, length, flags))
            if len(chain) > vq.qnum:
                raise VirtqueueError("desc chain longer than the queue size")
            if not flags & VRING_DESC_F_NEXT:
                return chain
            i = nxt

    def _one(self, vq, head):
        chain = self._walk(vq, head)
        if len(chain) < 2:
            raise VirtqueueError("request needs at least header and status")
        h_addr, h_len, h_flags = chain[0]
        if h_flags & VRING_DESC_F_WRITE or h_len < BLK_HEADER_SIZE:
            raise VirtqueueError("header desc must be device-readable, >= 16 B")
        rtype, _reserved, sector = struct.unpack_from("<IIQ", vq.mem, h_addr)
        s_addr, s_len, s_flags = chain[-1]
        if not s_flags & VRING_DESC_F_WRITE or s_len < BLK_STATUS_SIZE:
            raise VirtqueueError("status desc must be device-writable, >= 1 B")
        data = chain[1:-1]

        status = VIRTIO_BLK_S_OK
        written = 0
        if rtype == VIRTIO_BLK_T_IN:
            if not data:
                raise VirtqueueError("T_IN without a data segment")
            total = 0
            for addr, length, flags in data:
                if not flags & VRING_DESC_F_WRITE:
                    raise VirtqueueError("T_IN data desc must be device-writable")
                if length == 0 or length % SECTOR_SIZE:
                    raise VirtqueueError("T_IN data len must be a positive multiple of 512")
                total += length
            if sector + total // SECTOR_SIZE > self.capacity_sectors:
                status = VIRTIO_BLK_S_IOERR
            else:
                src = sector * SECTOR_SIZE
                for addr, length, _flags in data:
                    vq.mem[addr:addr + length] = self.disk[src:src + length]
                    src += length
                written = total
        elif rtype == VIRTIO_BLK_T_OUT:
            for addr, length, flags in data:
                if flags & VRING_DESC_F_WRITE:
                    raise VirtqueueError("T_OUT data desc must be device-readable")
            status = VIRTIO_BLK_S_UNSUPP      # FS-v1 backend is read-only
        elif rtype == VIRTIO_BLK_T_FLUSH:
            if data:
                raise VirtqueueError("T_FLUSH must not carry a data segment")
        else:
            status = VIRTIO_BLK_S_UNSUPP

        vq.mem[s_addr] = status
        used_len = written + BLK_STATUS_SIZE
        slot = vq.used_idx() % vq.qnum
        vq.set_used_entry(slot, head, used_len)
        vq.set_used_idx((vq.used_idx() + 1) & U16_MASK)
        return {"head": head, "type": rtype, "sector": sector,
                "status": status, "used_len": used_len}


# ----------------------------------------------------------------------
# driver-side helpers: build a T_IN chain and publish it in the avail ring
# ----------------------------------------------------------------------

class Bump:
    """Deterministic bump allocator for buffers behind the rings."""

    def __init__(self, start):
        self.next = start

    def alloc(self, size, align=1):
        addr = align_up(self.next, align)
        self.next = addr + size
        return addr


def build_in_request(vq, head, hdr_addr, sector, segments, status_addr):
    """segments: list of (addr, len) data buffers, in transfer order."""
    n = len(segments)
    vq.set_desc(head, hdr_addr, BLK_HEADER_SIZE, VRING_DESC_F_NEXT, head + 1)
    struct.pack_into("<IIQ", vq.mem, hdr_addr, VIRTIO_BLK_T_IN, 0, sector)
    for k, (addr, length) in enumerate(segments):
        di = head + 1 + k
        vq.set_desc(di, addr, length,
                    VRING_DESC_F_WRITE | VRING_DESC_F_NEXT, di + 1)
    vq.set_desc(head + 1 + n, status_addr, BLK_STATUS_SIZE, VRING_DESC_F_WRITE, 0)


def build_typed_request(vq, head, hdr_addr, rtype, sector, status_addr):
    """Header + status only (T_FLUSH / unknown types)."""
    vq.set_desc(head, hdr_addr, BLK_HEADER_SIZE, VRING_DESC_F_NEXT, head + 1)
    struct.pack_into("<IIQ", vq.mem, hdr_addr, rtype, 0, sector)
    vq.set_desc(head + 1, status_addr, BLK_STATUS_SIZE, VRING_DESC_F_WRITE, 0)


def submit(vq, head):
    idx = vq.avail_idx()
    vq.set_avail_ring(idx % vq.qnum, head)
    vq.set_avail_idx((idx + 1) & U16_MASK)


def blk_config_bytes(capacity_sectors, size_max=1 << 20, seg_max=128,
                     blk_size=SECTOR_SIZE):
    """virtio-blk config region as the legacy PIO window exposes it."""
    return struct.pack("<QIIHBBI", capacity_sectors, size_max, seg_max,
                       0, 0, 0, blk_size)


def blk_config_capacity(cfg):
    return struct.unpack_from("<Q", cfg, VIRTIO_BLK_CONFIG_CAPACITY)[0]


# ----------------------------------------------------------------------
# self-test suite
# ----------------------------------------------------------------------

def pattern_disk(nsectors):
    return bytes(((s * 131 + b * 7 + 11) & 0xFF)
                 for s in range(nsectors) for b in range(SECTOR_SIZE))


def rig(qnum=8, nsectors=64, mem_size=1 << 16):
    mem = bytearray(mem_size)
    vq = Virtqueue(mem, 0, qnum)
    bump = Bump(vq.total)
    dev = BlkDevice(pattern_disk(nsectors))
    return mem, vq, bump, dev


def read_request(vq, bump, head, sector, seg_lens):
    hdr = bump.alloc(BLK_HEADER_SIZE, 8)
    segs = [(bump.alloc(n, 8), n) for n in seg_lens]
    sta = bump.alloc(BLK_STATUS_SIZE)
    build_in_request(vq, head, hdr, sector, segs, sta)
    submit(vq, head)
    return hdr, segs, sta


def test_layout():
    lay = vring_layout(8)
    assert lay["desc_off"] == 0 and lay["avail_off"] == 128
    assert lay["used_off"] == 4096, "legacy used ring pads to 4096"
    assert lay["total"] == 4096 + 6 + 64
    lay = vring_layout(256)
    assert lay["avail_off"] == 4096 and lay["used_off"] == 8192
    assert lay["total"] == 8192 + 6 + 8 * 256
    for bad in (0, 3, 65536):
        try:
            vring_layout(bad)
        except VirtqueueError:
            pass
        else:
            raise AssertionError(f"qnum {bad} accepted")


def test_single_sector_read():
    mem, vq, bump, dev = rig()
    hdr, segs, sta = read_request(vq, bump, 0, 3, [512])
    res = dev.process(vq)
    assert len(res) == 1 and res[0]["status"] == VIRTIO_BLK_S_OK
    assert res[0]["used_len"] == 513
    addr, n = segs[0]
    assert mem[addr:addr + n] == dev.disk[3 * 512:4 * 512]
    assert mem[sta] == VIRTIO_BLK_S_OK
    assert vq.used_idx() == 1
    assert vq.used_entry(0) == (0, 513)
    assert dev.last_avail == 1
    # polling again with no new avail entries must be a no-op
    assert dev.process(vq) == [] and vq.used_idx() == 1


def test_multi_segment_read():
    mem, vq, bump, dev = rig()
    hdr, segs, sta = read_request(vq, bump, 0, 10, [512, 1024])
    res = dev.process(vq)
    assert res[0]["status"] == VIRTIO_BLK_S_OK and res[0]["used_len"] == 1537
    got = b"".join(mem[a:a + n] for a, n in segs)
    assert got == dev.disk[10 * 512:13 * 512]
    assert mem[sta] == 0
    assert vq.used_entry(0) == (0, 1537)


def test_batch_and_head_ids():
    mem, vq, bump, dev = rig(qnum=16)
    heads = [0, 3, 6]
    for k, head in enumerate(heads):
        read_request(vq, bump, head, k, [512])
    res = dev.process(vq)
    assert [r["head"] for r in res] == heads
    assert vq.used_idx() == 3
    for slot, head in enumerate(heads):
        assert vq.used_entry(slot) == (head, 513)


def test_idx_u16_wrap():
    mem, vq, bump, dev = rig()
    # driver published 65535 entries, device consumed all of them
    vq.set_avail_idx(0xFFFF)
    dev.last_avail = 0xFFFF
    vq.set_used_idx(0xFFFF)
    read_request(vq, bump, 0, 1, [512])   # avail idx 0xFFFF -> 0
    read_request(vq, bump, 3, 2, [512])   # avail idx 0 -> 1
    assert vq.avail_idx() == 1
    res = dev.process(vq)
    assert len(res) == 2 and all(r["status"] == 0 for r in res)
    assert dev.last_avail == 1, "consume counter wraps 0xFFFF -> 0 -> 1"
    assert vq.used_idx() == 1, "used idx wraps 0xFFFF -> 0 -> 1"
    assert vq.used_entry(0xFFFF % vq.qnum) == (0, 513)
    assert vq.used_entry(0 % vq.qnum) == (3, 513)


def test_ring_flags_untouched():
    mem, vq, bump, dev = rig()
    vq.set_avail_flags(VRING_AVAIL_F_NO_INTERRUPT)
    vq.set_used_flags(VRING_USED_F_NO_NOTIFY)
    read_request(vq, bump, 0, 0, [512])
    dev.process(vq)
    assert vq.avail_flags() == VRING_AVAIL_F_NO_INTERRUPT
    assert vq.used_flags() == VRING_USED_F_NO_NOTIFY


def test_last_lba_boundary():
    mem, vq, bump, dev = rig(nsectors=64)
    hdr, segs, sta = read_request(vq, bump, 0, 63, [512])
    res = dev.process(vq)
    assert res[0]["status"] == VIRTIO_BLK_S_OK
    assert mem[segs[0][0]:segs[0][0] + 512] == dev.disk[63 * 512:64 * 512]


def expect_error(fn, needle):
    try:
        fn()
    except VirtqueueError as e:
        assert needle in str(e), f"error '{e}' does not mention '{needle}'"
        return
    raise AssertionError(f"expected VirtqueueError containing '{needle}'")


def test_neg_bad_desc_idx():
    mem, vq, bump, dev = rig()
    read_request(vq, bump, 0, 0, [512])
    vq.set_avail_ring(0, vq.qnum)        # head == qnum is out of range
    expect_error(lambda: dev.process(vq), "bad desc idx")
    vq.set_avail_ring(0, 0xFFFF)
    expect_error(lambda: dev.process(vq), "bad desc idx")


def test_neg_lba_out_of_range():
    mem, vq, bump, dev = rig(nsectors=64)
    hdr, segs, sta = read_request(vq, bump, 0, 64, [512])
    before = bytes(mem[segs[0][0]:segs[0][0] + 512])
    res = dev.process(vq)
    assert res[0]["status"] == VIRTIO_BLK_S_IOERR, "LBA == capacity must fail"
    assert res[0]["used_len"] == 1, "only the status byte is device-written"
    assert mem[sta] == VIRTIO_BLK_S_IOERR
    assert bytes(mem[segs[0][0]:segs[0][0] + 512]) == before, "no data write"
    assert vq.used_entry(0) == (0, 1)
    # partial overrun: last sector fits, the second does not
    mem, vq, bump, dev = rig(nsectors=64)
    hdr, segs, sta = read_request(vq, bump, 0, 63, [1024])
    res = dev.process(vq)
    assert res[0]["status"] == VIRTIO_BLK_S_IOERR and res[0]["used_len"] == 1


def test_neg_device_missing():
    mem, vq, bump, _ = rig()
    dev = BlkDevice(None)
    assert dev.capacity_sectors == 0
    read_request(vq, bump, 0, 0, [512])
    expect_error(lambda: dev.process(vq), "device missing")


def test_neg_chain_loop():
    mem, vq, bump, dev = rig()
    hdr, segs, sta = read_request(vq, bump, 0, 0, [512])
    # status desc points back to the header: 0 -> 1 -> 2 -> 0
    vq.set_desc(2, sta, BLK_STATUS_SIZE,
                VRING_DESC_F_WRITE | VRING_DESC_F_NEXT, 0)
    expect_error(lambda: dev.process(vq), "chain loop")
    # self-loop on a single desc
    mem, vq, bump, dev = rig()
    hdr, segs, sta = read_request(vq, bump, 0, 0, [512])
    vq.set_desc(1, segs[0][0], 512,
                VRING_DESC_F_WRITE | VRING_DESC_F_NEXT, 1)
    expect_error(lambda: dev.process(vq), "chain loop")


def test_neg_data_flags_and_len():
    mem, vq, bump, dev = rig()
    hdr, segs, sta = read_request(vq, bump, 0, 0, [512])
    vq.set_desc(1, segs[0][0], 512, VRING_DESC_F_NEXT, 2)   # WRITE dropped
    expect_error(lambda: dev.process(vq), "device-writable")
    mem, vq, bump, dev = rig()
    hdr, segs, sta = read_request(vq, bump, 0, 0, [512])
    vq.set_desc(1, segs[0][0], 500, VRING_DESC_F_WRITE | VRING_DESC_F_NEXT, 2)
    expect_error(lambda: dev.process(vq), "multiple of 512")


def test_neg_header_and_status():
    mem, vq, bump, dev = rig()
    hdr, segs, sta = read_request(vq, bump, 0, 0, [512])
    vq.set_desc(0, hdr, 8, VRING_DESC_F_NEXT, 1)            # short header
    expect_error(lambda: dev.process(vq), "header")
    mem, vq, bump, dev = rig()
    hdr, segs, sta = read_request(vq, bump, 0, 0, [512])
    vq.set_desc(0, hdr, BLK_HEADER_SIZE,
                VRING_DESC_F_WRITE | VRING_DESC_F_NEXT, 1)  # writable header
    expect_error(lambda: dev.process(vq), "header")
    mem, vq, bump, dev = rig()
    hdr, segs, sta = read_request(vq, bump, 0, 0, [512])
    vq.set_desc(2, sta, BLK_STATUS_SIZE, 0, 0)              # readable status
    expect_error(lambda: dev.process(vq), "status")


def test_neg_addr_out_of_image():
    mem, vq, bump, dev = rig()
    hdr, segs, sta = read_request(vq, bump, 0, 0, [512])
    vq.set_desc(1, len(mem) - 100, 512,
                VRING_DESC_F_WRITE | VRING_DESC_F_NEXT, 2)
    expect_error(lambda: dev.process(vq), "exceeds the memory image")


def test_neg_avail_runs_ahead():
    mem, vq, bump, dev = rig(qnum=8)
    vq.set_avail_idx(9)                  # 9 pending on an 8-slot queue
    expect_error(lambda: dev.process(vq), "runs ahead")


def test_readonly_and_flush():
    mem, vq, bump, dev = rig()
    hdr = bump.alloc(BLK_HEADER_SIZE, 8)
    sta = bump.alloc(BLK_STATUS_SIZE)
    build_typed_request(vq, 0, hdr, VIRTIO_BLK_T_OUT, 0, sta)
    submit(vq, 0)
    hdr2 = bump.alloc(BLK_HEADER_SIZE, 8)
    sta2 = bump.alloc(BLK_STATUS_SIZE)
    build_typed_request(vq, 2, hdr2, VIRTIO_BLK_T_FLUSH, 0, sta2)
    submit(vq, 2)
    hdr3 = bump.alloc(BLK_HEADER_SIZE, 8)
    sta3 = bump.alloc(BLK_STATUS_SIZE)
    build_typed_request(vq, 4, hdr3, 0x77, 0, sta3)
    submit(vq, 4)
    res = dev.process(vq)
    assert res[0]["status"] == VIRTIO_BLK_S_UNSUPP, "read-only backend refuses T_OUT"
    assert res[1]["status"] == VIRTIO_BLK_S_OK, "T_FLUSH succeeds"
    assert res[2]["status"] == VIRTIO_BLK_S_UNSUPP, "unknown type refused"
    assert [mem[a] for a in (sta, sta2, sta3)] == [2, 0, 2]
    assert all(r["used_len"] == 1 for r in res)


def test_pio_and_pci_contract():
    # frozen offsets, widths and ordering of the legacy BAR0 map
    assert PIO_REGS["host_features"] == (0x00, 4, "R")
    assert PIO_REGS["guest_features"] == (0x04, 4, "RW")
    assert PIO_REGS["queue_pfn"] == (0x08, 4, "RW")
    assert PIO_REGS["queue_num"] == (0x0C, 2, "R")
    assert PIO_REGS["queue_sel"] == (0x0E, 2, "W")
    assert PIO_REGS["queue_notify"] == (0x10, 2, "W")
    assert PIO_REGS["status"] == (0x12, 1, "RW")
    assert PIO_REGS["isr"] == (0x13, 1, "R")
    assert VIRTIO_PCI_CONFIG_OFF == 0x14
    assert VIRTIO_PCI_CONFIG_OFF_MSIX == 0x18
    offs = [v[0] for v in PIO_REGS.values()]
    assert offs == sorted(offs), "register map must be offset-ordered"
    assert (VIRTIO_PCI_VENDOR, VIRTIO_PCI_DEVICE_BLK_TRANSITIONAL) == (0x1AF4, 0x1001)
    assert VIRTIO_PCI_SUBDEVICE_BLK == 0x0002
    assert VIRTIO_PCI_REVISION_LEGACY == 0x00
    # queue_pfn semantics: page number of the queue base
    base = 0x12345000
    assert base >> 12 == 0x12345


def test_config_capacity():
    cfg = blk_config_bytes(16384)
    assert len(cfg) == 24
    assert blk_config_capacity(cfg) == 16384
    assert struct.unpack_from("<I", cfg, VIRTIO_BLK_CONFIG_BLK_SIZE)[0] == 512
    dev = BlkDevice(pattern_disk(16384))
    assert blk_config_bytes(dev.capacity_sectors) == cfg


TESTS = [
    ("legacy split layout, 4096 used-ring align", test_layout),
    ("T_IN single sector, used.len = data+status", test_single_sector_read),
    ("T_IN two data segments, 3 sectors", test_multi_segment_read),
    ("batch consume, used id = chain head", test_batch_and_head_ids),
    ("avail/used idx u16 wrap at 0xFFFF", test_idx_u16_wrap),
    ("NO_INTERRUPT/NO_NOTIFY flags untouched", test_ring_flags_untouched),
    ("last LBA reads OK", test_last_lba_boundary),
    ("NEG bad desc idx", test_neg_bad_desc_idx),
    ("NEG LBA out of range -> IOERR, used.len 1", test_neg_lba_out_of_range),
    ("NEG device missing", test_neg_device_missing),
    ("NEG desc chain loop / self-loop", test_neg_chain_loop),
    ("NEG data desc flags / 512 alignment", test_neg_data_flags_and_len),
    ("NEG short/writable header, readable status", test_neg_header_and_status),
    ("NEG desc addr beyond memory image", test_neg_addr_out_of_image),
    ("NEG avail idx ahead of queue size", test_neg_avail_runs_ahead),
    ("read-only T_OUT UNSUPP, FLUSH OK, unknown UNSUPP", test_readonly_and_flush),
    ("legacy PIO map + PCI identity contract", test_pio_and_pci_contract),
    ("blk config capacity in 512 B sectors", test_config_capacity),
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
    if failed:
        log(f"MODEL SELFTEST FAILED ({failed} failures)")
        rc = 1
    else:
        log(f"MODEL SELFTEST PASSED ({len(TESTS)} checks)")
        rc = 0
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "model-selftest.log").write_text("\n".join(lines) + "\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())