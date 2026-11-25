#!/usr/bin/env python3
"""Independent read-only FAT16 oracle for FS-v1, task 130.

Pure standard library, Python 3.15 clean. This model parses the raw
image bytes directly and imports nothing from tools/build-fat16-image.py
(no shared structures, no shared constants beyond the on-disk format
itself): the builder and this oracle are two independent implementations
of the FAT16 spec, and the self-test cross-checks them against each
other and against the frozen ROM digests in user/chip8/api.inc.

Covered semantics:
  * BPB validation: signature, sector size, power-of-two cluster size,
    reserved/FAT/root areas, FAT16 cluster count range [4085, 65524];
  * FAT16 cluster chain walking with EOC >= 0xFFF8, bad-cluster 0xFFF7,
    cycle detection and out-of-range cluster numbers;
  * fixed root directory linear scan, 8.3 short names, 0x00 end mark,
    0xE5 deleted slots, volume-label and LFN slots skipped;
  * case-insensitive path resolution through subdirectories;
  * read(offset, len) with cross-cluster spans, EOF clamping and seek
    boundaries; stat(size/attr); readdir as fixed 32-byte-derived entries.

Negative fixtures (all must be judged as errors, built inline on a
self-made mini image, independent of the builder): illegal BPB variants,
cluster chain loop, out-of-range cluster number, bad cluster marker,
missing path, file used as directory, empty directory, negative offset.

Running this file executes the full self-test suite and writes evidence
to build/stage2-fs/model-selftest.log.
"""

import hashlib
import json
import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "build" / "stage2-fs"

ATTR_READ_ONLY = 0x01
ATTR_DIRECTORY = 0x10
ATTR_VOLUME_ID = 0x08
ATTR_LFN = 0x0F

FAT16_MIN_CLUSTERS = 4085
FAT16_MAX_CLUSTERS = 65524
FAT16_BAD_CLUSTER = 0xFFF7
FAT16_EOC_MIN = 0xFFF8

ENTRY_SIZE = 32


class Fat16Error(Exception):
    """Any image or request state the oracle must judge as an error."""


class DirEntry:
    __slots__ = ("name", "attr", "cluster", "size")

    def __init__(self, name, attr, cluster, size):
        self.name = name
        self.attr = attr
        self.cluster = cluster
        self.size = size

    @property
    def is_dir(self):
        return bool(self.attr & ATTR_DIRECTORY)

    def as_dict(self):
        return {"name": self.name, "attr": self.attr, "cluster": self.cluster,
                "size": self.size, "is_dir": self.is_dir}


class Fat16:
    """Read-only FAT16 view over raw image bytes."""

    def __init__(self, data):
        self.data = bytes(data)
        if len(self.data) < 512:
            raise Fat16Error("image shorter than one boot sector")
        if self.data[510:512] != b"\x55\xAA":
            raise Fat16Error("bad boot signature")
        (bps, spc, reserved, nfats, root_entries, total16, media, spf,
         _spt, _heads, _hidden, total32) = struct.unpack_from(
            "<HBHBHHBHHHII", self.data, 11)
        if bps not in (512, 1024, 2048, 4096):
            raise Fat16Error(f"illegal bytes-per-sector {bps}")
        if spc == 0 or spc > 128 or spc & (spc - 1):
            raise Fat16Error(f"illegal sectors-per-cluster {spc}")
        if reserved < 1:
            raise Fat16Error("no reserved sectors")
        if nfats < 1:
            raise Fat16Error("no FAT copies")
        if root_entries == 0 or (root_entries * ENTRY_SIZE) % bps:
            raise Fat16Error("root directory area is not a whole sector count")
        if spf == 0:
            raise Fat16Error("sectors-per-FAT is zero: not FAT16")
        total = total16 if total16 else total32
        if total == 0:
            raise Fat16Error("total sector count is zero")
        if len(self.data) < total * bps:
            raise Fat16Error("image truncated against the BPB total")
        root_sectors = root_entries * ENTRY_SIZE // bps
        meta = reserved + nfats * spf + root_sectors
        if meta >= total:
            raise Fat16Error("metadata areas consume the whole image")
        clusters = (total - meta) // spc
        if not FAT16_MIN_CLUSTERS <= clusters <= FAT16_MAX_CLUSTERS:
            raise Fat16Error(f"cluster count {clusters} outside the FAT16 range")
        # task 138 W2: the FAT must cover every cluster entry, exactly
        # the kernel fs_init guard clusters + 2 <= spf * (bps / 2)
        if clusters + 2 > spf * (bps // 2):
            raise Fat16Error("FAT too small for the cluster count")
        self.bps = bps
        self.spc = spc
        self.reserved = reserved
        self.nfats = nfats
        self.root_entries = root_entries
        self.spf = spf
        self.total_sectors = total
        self.clusters = clusters
        self.cluster_bytes = spc * bps
        self.fat_start = reserved * bps
        self.root_start = (reserved + nfats * spf) * bps
        self.root_bytes = root_entries * ENTRY_SIZE
        self.data_start = self.root_start + self.root_bytes

    # ------------------------------------------------------------------
    # FAT and cluster chains
    # ------------------------------------------------------------------

    def _check_cluster(self, c):
        if not 2 <= c < self.clusters + 2:
            raise Fat16Error(f"cluster number {c} out of range")

    def fat_entry(self, c):
        self._check_cluster(c)
        return struct.unpack_from("<H", self.data, self.fat_start + 2 * c)[0]

    def cluster_chain(self, start):
        if start == 0:
            return []                   # empty file, no clusters assigned
        chain = []
        seen = set()
        c = start
        while True:
            self._check_cluster(c)
            if c in seen:
                raise Fat16Error(f"cluster chain loop at {c}")
            seen.add(c)
            chain.append(c)
            nxt = self.fat_entry(c)
            if nxt >= FAT16_EOC_MIN:
                return chain            # any EOC marker >= 0xFFF8 ends here
            if nxt == FAT16_BAD_CLUSTER:
                raise Fat16Error(f"bad cluster marker at {c}")
            if nxt < 2:
                raise Fat16Error(f"reserved cluster value {nxt} in chain")
            c = nxt

    def cluster_off(self, c):
        self._check_cluster(c)
        return self.data_start + (c - 2) * self.cluster_bytes

    # ------------------------------------------------------------------
    # directory scanning and path resolution
    # ------------------------------------------------------------------

    def _dir_bytes(self, cluster):
        if cluster == 0:
            return self.data[self.root_start:self.root_start + self.root_bytes]
        out = bytearray()
        for c in self.cluster_chain(cluster):
            off = self.cluster_off(c)
            out += self.data[off:off + self.cluster_bytes]
        return bytes(out)

    def _scan(self, blob):
        entries = []
        for k in range(len(blob) // ENTRY_SIZE):
            e = blob[k * ENTRY_SIZE:(k + 1) * ENTRY_SIZE]
            first = e[0]
            if first == 0x00:
                break                   # end-of-directory mark
            if first == 0xE5:
                continue                # deleted slot
            attr = e[11]
            if attr & ATTR_VOLUME_ID:
                continue                # volume label, not a file
            if attr & ATTR_LFN == ATTR_LFN:
                continue                # FS-v1 is 8.3 only, skip LFN slots
            base = bytes([0xE5 if first == 0x05 else first]) + e[1:8]
            base = base.decode("ascii", "replace").rstrip()
            ext = e[8:11].decode("ascii", "replace").rstrip()
            name = f"{base}.{ext}" if ext else base
            cluster, size = struct.unpack_from("<HI", e, 26)
            entries.append(DirEntry(name, attr, cluster, size))
        return entries

    def root(self):
        return self._scan(self._dir_bytes(0))

    def resolve(self, path):
        parts = [p for p in path.upper().split("/") if p]
        if not parts:
            return DirEntry("/", ATTR_DIRECTORY, 0, 0)
        entries = self.root()
        for depth, part in enumerate(parts):
            match = None
            for e in entries:
                if e.name == part:
                    match = e
                    break
            if match is None:
                raise Fat16Error(f"path not found: {path}")
            if depth + 1 < len(parts):
                if not match.is_dir:
                    raise Fat16Error(f"not a directory: {part} in {path}")
                entries = self._scan(self._dir_bytes(match.cluster))
        return match

    def readdir(self, path="/"):
        e = self.resolve(path)
        if not e.is_dir:
            raise Fat16Error(f"not a directory: {path}")
        return [x for x in self._scan(self._dir_bytes(e.cluster))
                if x.name not in (".", "..")]

    def stat(self, path):
        return self.resolve(path).as_dict()

    # ------------------------------------------------------------------
    # file reads
    # ------------------------------------------------------------------

    def read(self, path, offset=0, length=None):
        e = self.resolve(path)
        if e.is_dir:
            raise Fat16Error(f"is a directory: {path}")
        if offset < 0:
            raise Fat16Error("negative read offset")
        if length is not None and length < 0:
            raise Fat16Error("negative read length")
        if offset >= e.size:
            return b""                  # seek at or past EOF reads nothing
        end = e.size if length is None else min(e.size, offset + length)
        if end <= offset:
            return b""
        chain = self.cluster_chain(e.cluster)
        cb = self.cluster_bytes
        if len(chain) * cb < e.size:
            raise Fat16Error(f"cluster chain shorter than the size of {path}")
        out = bytearray()
        pos = 0
        for c in chain:
            lo = max(offset, pos)
            hi = min(end, pos + cb)
            if lo < hi:
                base = self.cluster_off(c)
                out += self.data[base + lo - pos:base + hi - pos]
            pos += cb
            if pos >= end:
                break
        return bytes(out)


# ----------------------------------------------------------------------
# self-test fixtures
# ----------------------------------------------------------------------

def parse_api_digests():
    """Independent copy of the frozen ROM digest contract."""
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


def run_builder(tmpdir, name, manifest=None):
    out = Path(tmpdir) / name
    cmd = [sys.executable, str(ROOT / "tools" / "build-fat16-image.py"),
           "--out", str(out)]
    if manifest is not None:
        mf = Path(tmpdir) / (name + ".manifest.json")
        mf.write_text(json.dumps(manifest))
        cmd += ["--manifest", str(mf)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"builder failed: {proc.stderr}"
    return out.read_bytes()


def mini_entry(name11, attr, cluster, size):
    e = bytearray(ENTRY_SIZE)
    e[0:11] = name11
    e[11] = attr
    struct.pack_into("<HI", e, 26, cluster, size)
    return bytes(e)


MINI_A_DATA = bytes(((i * 11 + 5) & 0xFF) for i in range(1300))
MINI_B_DATA = b"0123456789"


def mini_image():
    """Hand-built minimal valid FAT16, independent of the builder.

    512 B sectors, 1 sector/cluster, 4200 clusters, spf 17. Layout:
    A.TXT clusters 2->3->4 (1300 B, crosses two cluster boundaries),
    SUB dir cluster 5 holding B.BIN cluster 6, EMPTY dir cluster 7.
    """
    bps, spc, reserved, nfats, root_entries, spf, clusters = \
        512, 1, 1, 2, 512, 17, 4200
    root_sectors = root_entries * ENTRY_SIZE // bps
    total = reserved + nfats * spf + root_sectors + clusters
    img = bytearray(total * bps)
    fat_start = reserved * bps
    root_start = (reserved + nfats * spf) * bps
    data_start = root_start + root_entries * ENTRY_SIZE

    def coff(c):
        return data_start + (c - 2) * bps

    boot = img
    boot[0:3] = b"\xEB\x3C\x90"
    boot[3:11] = b"MINI16  "
    struct.pack_into("<HBHBHHBHHHII", boot, 11,
                     bps, spc, reserved, nfats, root_entries, total,
                     0xF8, spf, 63, 255, 0, 0)
    boot[510:512] = b"\x55\xAA"

    fat = bytearray(spf * bps)
    for c, v in ((0, 0xFFF8), (1, 0xFFFF), (2, 3), (3, 4), (4, 0xFFFF),
                 (5, 0xFFFF), (6, 0xFFFF), (7, 0xFFFF)):
        struct.pack_into("<H", fat, 2 * c, v)
    for k in range(nfats):
        img[fat_start + k * spf * bps:fat_start + (k + 1) * spf * bps] = fat

    root = [mini_entry(b"A       TXT", 0x20, 2, len(MINI_A_DATA)),
            mini_entry(b"SUB        ", ATTR_DIRECTORY, 5, 0),
            mini_entry(b"EMPTY      ", ATTR_DIRECTORY, 7, 0)]
    for k, e in enumerate(root):
        img[root_start + k * ENTRY_SIZE:root_start + (k + 1) * ENTRY_SIZE] = e

    img[coff(2):coff(2) + bps] = MINI_A_DATA[0:512]
    img[coff(3):coff(3) + bps] = MINI_A_DATA[512:1024]
    img[coff(4):coff(4) + 276] = MINI_A_DATA[1024:1300]
    sub = [mini_entry(b".          ", ATTR_DIRECTORY, 5, 0),
           mini_entry(b"..         ", ATTR_DIRECTORY, 0, 0),
           mini_entry(b"B       BIN", 0x20, 6, len(MINI_B_DATA))]
    for k, e in enumerate(sub):
        img[coff(5) + k * ENTRY_SIZE:coff(5) + (k + 1) * ENTRY_SIZE] = e
    img[coff(6):coff(6) + len(MINI_B_DATA)] = MINI_B_DATA
    empty = [mini_entry(b".          ", ATTR_DIRECTORY, 7, 0),
             mini_entry(b"..         ", ATTR_DIRECTORY, 0, 0)]
    for k, e in enumerate(empty):
        img[coff(7) + k * ENTRY_SIZE:coff(7) + (k + 1) * ENTRY_SIZE] = e
    return bytes(img)


def patched(img, offset, blob):
    out = bytearray(img)
    out[offset:offset + len(blob)] = blob
    return bytes(out)


def expect_error(fn, needle):
    try:
        fn()
    except Fat16Error as e:
        assert needle in str(e), f"error '{e}' does not mention '{needle}'"
        return
    raise AssertionError(f"expected Fat16Error containing '{needle}'")


# ----------------------------------------------------------------------
# self-test suite (BIMG / BIMG_BIG / MINI are populated by main)
# ----------------------------------------------------------------------

BIMG = None
BIMG_BIG = None
BIG_DATA = bytes(((i * 7 + 3) & 0xFF) for i in range(8192 + 37))
MINI = None


def test_bpb_geometry():
    fs = Fat16(BIMG)
    assert fs.bps == 512
    assert fs.reserved >= 1 and fs.nfats == 2 and fs.spf > 0
    assert FAT16_MIN_CLUSTERS <= fs.clusters <= FAT16_MAX_CLUSTERS
    meta = fs.reserved + fs.nfats * fs.spf + fs.root_bytes // fs.bps
    assert fs.clusters == (fs.total_sectors - meta) // fs.spc
    assert fs.fat_start == fs.reserved * fs.bps
    assert fs.root_start == fs.fat_start + fs.nfats * fs.spf * fs.bps
    assert fs.data_start == fs.root_start + fs.root_bytes
    assert fs.data_start + fs.clusters * fs.cluster_bytes <= len(BIMG)
    assert struct.unpack_from("<HH", BIMG, fs.fat_start) == (0xFFF8, 0xFFFF)
    # both FAT copies must agree on every allocated entry
    for c in range(2, 16):
        a, b = (struct.unpack_from("<H", BIMG, fs.fat_start + k * fs.spf * fs.bps
                                   + 2 * c)[0] for k in range(2))
        assert a == b


def test_rom_full_read():
    fs = Fat16(BIMG)
    digests = parse_api_digests()
    for path, fname, key, size in (("/ROMS/IBM.CH8", "ibm.ch8", "IBM", 132),
                                   ("/ROMS/TETRIS1.CH8", "tetris1.ch8", "TETRIS", 494)):
        src = (ROOT / "user" / "chip8" / "roms" / fname).read_bytes()
        got = fs.read(path)
        assert len(got) == size
        assert got == src, f"{path} bytes differ from the source ROM"
        assert hashlib.sha256(got).hexdigest() == digests[key]


def test_case_insensitive_paths():
    fs = Fat16(BIMG)
    ref = fs.read("/ROMS/IBM.CH8")
    for path in ("/roms/ibm.ch8", "/Roms/Ibm.Ch8", "ROMS/IBM.CH8", "/ROMS//IBM.CH8"):
        assert fs.read(path) == ref, path


def test_readdir():
    fs = Fat16(BIMG)
    assert [e.name for e in fs.readdir("/")] == ["ROMS", "PROGRAMS"]
    assert [e.name for e in fs.readdir("/ROMS")] == ["IBM.CH8", "TETRIS1.CH8"]
    assert fs.readdir("/PROGRAMS") == [], "PROGRAMS must be empty"
    names = [e.name for e in fs.root()]
    assert "KOLIBRI64" not in names, "volume label must not be a file entry"


def test_stat():
    fs = Fat16(BIMG)
    st = fs.stat("/ROMS/IBM.CH8")
    assert st["size"] == 132 and not st["is_dir"]
    assert st["attr"] & ATTR_READ_ONLY
    st = fs.stat("/ROMS")
    assert st["is_dir"] and st["attr"] & ATTR_DIRECTORY
    st = fs.stat("/ROMS/TETRIS1.CH8")
    assert st["size"] == 494


def test_eof_and_seek():
    fs = Fat16(BIMG)
    full = fs.read("/ROMS/IBM.CH8")
    assert fs.read("/ROMS/IBM.CH8", 132) == b""
    assert fs.read("/ROMS/IBM.CH8", 200, 10) == b""
    assert fs.read("/ROMS/IBM.CH8", 131, 100) == full[131:132]
    assert fs.read("/ROMS/IBM.CH8", 100) == full[100:]
    assert fs.read("/ROMS/IBM.CH8", 0, 0) == b""
    assert fs.read("/ROMS/IBM.CH8", 60, 60) == full[60:120]
    expect_error(lambda: fs.read("/ROMS/IBM.CH8", -1), "negative")
    expect_error(lambda: fs.read("/ROMS/IBM.CH8", 0, -5), "negative")


def test_cross_cluster_builder():
    fs = Fat16(BIMG_BIG)
    cb = fs.cluster_bytes
    assert fs.stat("/PROGRAMS/BIG.BIN")["size"] == len(BIG_DATA)
    assert fs.read("/PROGRAMS/BIG.BIN") == BIG_DATA
    assert len(fs.cluster_chain(fs.stat("/PROGRAMS/BIG.BIN")["cluster"])) == \
        (len(BIG_DATA) + cb - 1) // cb
    for off, ln in ((cb - 3, 6), (2 * cb - 1, 2), (4 * cb, cb),
                    (len(BIG_DATA) - 5, 100), (0, len(BIG_DATA) + 4096)):
        assert fs.read("/PROGRAMS/BIG.BIN", off, ln) == BIG_DATA[off:off + ln], (off, ln)
    assert fs.read("/PROGRAMS/BIG.BIN", len(BIG_DATA)) == b""


def test_builder_determinism():
    with tempfile.TemporaryDirectory() as td:
        a = run_builder(td, "a.img")
        b = run_builder(td, "b.img")
    assert a == b, "repeated builder runs must be byte-identical"
    assert a == BIMG


def test_mini_cross_cluster():
    fs = Fat16(MINI)
    assert fs.cluster_chain(2) == [2, 3, 4]
    assert fs.read("/A.TXT") == MINI_A_DATA
    assert fs.read("/A.TXT", 510, 4) == MINI_A_DATA[510:514]
    assert fs.read("/A.TXT", 1022, 6) == MINI_A_DATA[1022:1028]
    assert fs.read("/A.TXT", 1299, 10) == MINI_A_DATA[1299:1300]
    assert fs.read("/A.TXT", 1300) == b""
    assert fs.read("/A.TXT", 512, 512) == MINI_A_DATA[512:1024]


def test_mini_tree_and_empty_dir():
    fs = Fat16(MINI)
    assert [e.name for e in fs.readdir("/")] == ["A.TXT", "SUB", "EMPTY"]
    assert [e.name for e in fs.readdir("/SUB")] == ["B.BIN"]
    assert fs.read("/sub/b.bin") == MINI_B_DATA
    assert fs.readdir("/EMPTY") == [], "empty directory reads as zero entries"
    expect_error(lambda: fs.read("/EMPTY"), "is a directory")
    # EOC threshold: any marker >= 0xFFF8 terminates the chain
    img = patched(MINI, 512 + 2 * 4, struct.pack("<H", 0xFFF8))
    assert Fat16(img).read("/A.TXT") == MINI_A_DATA


def test_neg_illegal_bpb():
    expect_error(lambda: Fat16(patched(MINI, 510, b"\x00")), "signature")
    expect_error(lambda: Fat16(patched(MINI, 22, struct.pack("<H", 0))), "not FAT16")
    expect_error(lambda: Fat16(patched(MINI, 19, struct.pack("<H", 2000))),
                 "outside the FAT16 range")
    expect_error(lambda: Fat16(patched(MINI, 11, struct.pack("<H", 513))),
                 "bytes-per-sector")
    expect_error(lambda: Fat16(patched(MINI, 13, b"\x03")), "sectors-per-cluster")
    # task 138 W2: a legal-range cluster count with a too small FAT
    expect_error(lambda: Fat16(patched(MINI, 22, struct.pack("<H", 8))),
                 "FAT too small")
    expect_error(lambda: Fat16(MINI[:4096]), "truncated")
    expect_error(lambda: Fat16(b""), "boot sector")


def test_neg_chain_loop():
    # FAT[3] = 2 turns 2->3->4 into 2->3->2
    img = patched(MINI, 512 + 2 * 3, struct.pack("<H", 2))
    expect_error(lambda: Fat16(img).read("/A.TXT"), "chain loop")
    # self-loop FAT[2] = 2
    img = patched(MINI, 512 + 2 * 2, struct.pack("<H", 2))
    expect_error(lambda: Fat16(img).read("/A.TXT"), "chain loop")


def test_neg_cluster_out_of_range():
    root_off = (1 + 2 * 17) * 512
    img = patched(MINI, root_off + 26, struct.pack("<H", 60000))
    expect_error(lambda: Fat16(img).read("/A.TXT"), "out of range")
    img = patched(MINI, 512 + 2 * 2, struct.pack("<H", 60000))
    expect_error(lambda: Fat16(img).read("/A.TXT"), "out of range")
    expect_error(lambda: Fat16(MINI).fat_entry(1), "out of range")


def test_neg_bad_cluster_marker():
    img = patched(MINI, 512 + 2 * 3, struct.pack("<H", 0xFFF7))
    expect_error(lambda: Fat16(img).read("/A.TXT"), "bad cluster")
    img = patched(MINI, 512 + 2 * 3, struct.pack("<H", 0))
    expect_error(lambda: Fat16(img).read("/A.TXT"), "reserved cluster value")


def test_neg_missing_path():
    fs = Fat16(MINI)
    expect_error(lambda: fs.resolve("/NOPE.TXT"), "path not found")
    expect_error(lambda: fs.resolve("/SUB/NOPE.BIN"), "path not found")
    expect_error(lambda: fs.resolve("/A.TXT/X"), "not a directory")
    expect_error(lambda: fs.readdir("/A.TXT"), "not a directory")
    fs2 = Fat16(BIMG)
    expect_error(lambda: fs2.read("/ROMS/MISSING.CH8"), "path not found")
    expect_error(lambda: fs2.read("/PROGRAMS/CHIP8.BIN"), "path not found")


TESTS = [
    ("BPB parse + geometry + dual FAT agreement", test_bpb_geometry),
    ("ROM full read == source bytes == api.inc sha256", test_rom_full_read),
    ("case-insensitive path resolution", test_case_insensitive_paths),
    ("readdir root/ROMS/PROGRAMS, volume label skipped", test_readdir),
    ("stat size/attr/is_dir", test_stat),
    ("EOF clamping and seek boundaries", test_eof_and_seek),
    ("cross-cluster reads on builder BIG.BIN", test_cross_cluster_builder),
    ("builder repeated runs byte-identical", test_builder_determinism),
    ("mini image 3-cluster chain spans", test_mini_cross_cluster),
    ("mini tree, empty dir, EOC >= 0xFFF8", test_mini_tree_and_empty_dir),
    ("NEG illegal BPB variants", test_neg_illegal_bpb),
    ("NEG cluster chain loop / self-loop", test_neg_chain_loop),
    ("NEG cluster number out of range", test_neg_cluster_out_of_range),
    ("NEG bad cluster / reserved chain value", test_neg_bad_cluster_marker),
    ("NEG missing path / file as directory", test_neg_missing_path),
]


def main():
    global BIMG, BIMG_BIG, MINI
    lines = []

    def log(msg):
        print(msg)
        lines.append(msg)

    with tempfile.TemporaryDirectory() as td:
        BIMG = run_builder(td, "plain.img")
        big_src = Path(td) / "big.bin"
        big_src.write_bytes(BIG_DATA)
        BIMG_BIG = run_builder(td, "big.img", {"/PROGRAMS/BIG.BIN": str(big_src)})
        MINI = mini_image()
        # sanity: the fixtures themselves must parse before testing
        Fat16(BIMG)
        Fat16(BIMG_BIG)
        Fat16(MINI)

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