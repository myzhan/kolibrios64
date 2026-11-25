#!/usr/bin/env python3
"""Deterministic read-only FAT16 raw image builder for FS-v1, task 130.

Pure standard library, no third-party imports, Python 3.15 clean. The
output is a raw disk image (no partition table) whose bytes are a pure
function of the inputs: every timestamp, the volume id, the OEM name
and all padding are fixed constants, so repeated runs are byte-identical
(the builder asserts this itself before writing).

Default content:
  /ROMS/IBM.CH8       bytes of user/chip8/roms/ibm.ch8    (132 B)
  /ROMS/TETRIS1.CH8   bytes of user/chip8/roms/tetris1.ch8 (494 B)
  /PROGRAMS           empty directory, room for chip8.bin later

The two ROM payloads are asserted against the frozen sha256 qwords in
user/chip8/api.inc before they are embedded, so the image can never
silently drift from the APP-v1 digest contract.

--manifest FILE appends arbitrary files: a JSON object mapping the
image path (8.3 components, e.g. "/PROGRAMS/CHIP8.BIN") to a host path
(absolute, or relative to the repo root). Parent directories are created
on demand. Insertion order is manifest order, keeping allocation
deterministic.

Geometry: 512 B sectors, 1 reserved sector, 2 FATs, 512 root entries,
media 0xF8. The sectors-per-cluster value is the smallest power of two
that keeps the FAT16 cluster count inside [4085, 65524]; the default
8 MiB image lands on 2 sectors/cluster (1 KiB clusters, 8143 clusters).

Usage: build-fat16-image.py [--out FILE] [--size-mib N] [--manifest FILE]
Default output: build/fs-v1/disk.img, sha256 printed on success.
"""

import argparse
import hashlib
import json
import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "build" / "fs-v1" / "disk.img"

# ----------------------------------------------------------------------
# frozen geometry and metadata constants (determinism contract)
# ----------------------------------------------------------------------

BPS = 512                       # bytes per sector
RESERVED_SECTORS = 1            # boot sector only
NUM_FATS = 2
ROOT_ENTRIES = 512              # 32 sectors of fixed root directory
MEDIA = 0xF8                    # fixed disk
SECTORS_PER_TRACK = 63          # cosmetic CHS, LBA is authoritative
NUM_HEADS = 255
HIDDEN_SECTORS = 0

FAT16_MIN_CLUSTERS = 4085
FAT16_MAX_CLUSTERS = 65524
FAT16_EOC = 0xFFFF              # end-of-chain marker written by this builder
FAT16_BAD = 0xFFF7

ATTR_READ_ONLY = 0x01           # files are marked read-only on disk too
ATTR_DIRECTORY = 0x10
ATTR_VOLUME_ID = 0x08

# every date/time field is a fixed constant: 2026-01-01 00:00:00
FAT_DATE = ((2026 - 1980) << 9) | (1 << 5) | 1     # 0x5C21
FAT_TIME = 0
VOLUME_ID = 0x4B4F4C42          # "KOLB", fixed
VOLUME_LABEL = b"KOLIBRI64  "   # 11 bytes, also the root volume entry
OEM_NAME = b"KOLIBR64"          # 8 bytes
FS_TYPE = b"FAT16   "           # 8 bytes, informational only

BOOT_SIG = 0x29                 # extended BPB present
DRIVE_NUMBER = 0x80

DEFAULT_SIZE_MIB = 8


class BuildError(Exception):
    """Invalid geometry, name or content."""


def compute_geometry(total_sectors, spc):
    """Fixed-point solve of sectors-per-FAT for the given cluster size."""
    root_sectors = ROOT_ENTRIES * 32 // BPS
    spf = 1
    while True:
        data_sectors = total_sectors - RESERVED_SECTORS - NUM_FATS * spf - root_sectors
        if data_sectors <= 0:
            raise BuildError("image too small for the fixed metadata areas")
        clusters = data_sectors // spc
        need = ((clusters + 2) * 2 + BPS - 1) // BPS
        if need == spf:
            break
        spf = need
    return {"total_sectors": total_sectors, "spc": spc, "spf": spf,
            "root_sectors": root_sectors, "clusters": clusters,
            "data_sectors": data_sectors,
            "fat_start": RESERVED_SECTORS * BPS,
            "root_start": (RESERVED_SECTORS + NUM_FATS * spf) * BPS,
            "data_start": (RESERVED_SECTORS + NUM_FATS * spf + root_sectors) * BPS,
            "cluster_bytes": spc * BPS}


def pick_geometry(size_mib):
    total_sectors = size_mib * 1024 * 1024 // BPS
    spc = 2                     # 1 KiB clusters minimum, a sane FS-v1 floor
    while spc <= 128:
        geo = compute_geometry(total_sectors, spc)
        if FAT16_MIN_CLUSTERS <= geo["clusters"] <= FAT16_MAX_CLUSTERS:
            return geo
        spc *= 2
    raise BuildError(f"{size_mib} MiB cannot be laid out as FAT16")


def encode_83(component):
    """One path component -> 11-byte FAT short name, uppercased."""
    if component in (".", "..") or not component:
        raise BuildError(f"invalid 8.3 component {component!r}")
    up = component.upper()
    if up.count(".") > 1:
        raise BuildError(f"8.3 name has more than one dot: {component!r}")
    base, _, ext = up.partition(".")
    if not 1 <= len(base) <= 8 or len(ext) > 3:
        raise BuildError(f"8.3 name out of bounds: {component!r}")
    if not re.fullmatch(r"[A-Z0-9_\-{}~!#$%&()'@^`]{1,8}", base) or \
       (ext and not re.fullmatch(r"[A-Z0-9_\-{}~!#$%&()'@^`]{1,3}", ext)):
        raise BuildError(f"8.3 name has illegal characters: {component!r}")
    return (base.ljust(8) + ext.ljust(3)).encode("ascii")


def split_image_path(path):
    parts = [p for p in path.split("/") if p]
    if not parts or not path.startswith("/"):
        raise BuildError(f"image path must be absolute: {path!r}")
    for p in parts:
        encode_83(p)
    return parts


def parse_api_digests():
    """Frozen ROM sha256 qwords from user/chip8/api.inc, big-endian."""
    text = (ROOT / "user" / "chip8" / "api.inc").read_text()
    out = {}
    for name in ("IBM", "TETRIS"):
        words = []
        for k in range(4):
            m = re.search(rf"^ROM_{name}_SHA{k}\s*=\s*(0x[0-9A-Fa-f]+)",
                          text, re.M)
            if not m:
                raise BuildError(f"ROM_{name}_SHA{k} missing from api.inc")
            words.append(int(m.group(1), 16))
        out[name] = struct.pack(">4Q", *words).hex()
    return out


def frozen_rom(fname, key, size):
    data = (ROOT / "user" / "chip8" / "roms" / fname).read_bytes()
    if len(data) != size:
        raise BuildError(f"{fname}: {len(data)} bytes, frozen size is {size}")
    digests = parse_api_digests()
    actual = hashlib.sha256(data).hexdigest()
    if actual != digests[key]:
        raise BuildError(f"{fname}: sha256 {actual} != frozen {digests[key]}")
    return data


# ----------------------------------------------------------------------
# image assembly
# ----------------------------------------------------------------------

def dir_entry(name11, attr, cluster, size):
    e = bytearray(32)
    e[0:11] = name11
    e[11] = attr
    e[12] = 0                   # NT reserved
    e[13] = 0                   # creation tenth
    struct.pack_into("<HHH", e, 14, FAT_TIME, FAT_DATE, FAT_DATE)
    struct.pack_into("<H", e, 20, 0)            # first cluster high, FAT16: 0
    struct.pack_into("<HH", e, 22, FAT_TIME, FAT_DATE)
    struct.pack_into("<HI", e, 26, cluster & 0xFFFF, size)
    return bytes(e)


class Tree:
    """Insertion-ordered directory tree; dict order fixes allocation."""

    def __init__(self):
        self.root = {}          # name -> node; node = {"dir": {...}} or {"data": bytes}

    def add_dir(self, parts):
        node = self.root
        for p in parts:
            child = node.setdefault(p, {"dir": {}})
            if "data" in child:
                raise BuildError(f"directory {p!r} collides with a file")
            node = child["dir"]
        return node

    def add_file(self, parts, data):
        parent = self.add_dir(parts[:-1])
        name = parts[-1]
        if name in parent:
            raise BuildError(f"duplicate image entry {'/'.join(parts)!r}")
        parent[name] = {"data": data}


def build_image(size_mib=DEFAULT_SIZE_MIB, manifest=None):
    geo = pick_geometry(size_mib)
    cb = geo["cluster_bytes"]

    tree = Tree()
    tree.add_dir(["ROMS"])
    tree.add_dir(["PROGRAMS"])
    tree.add_file(["ROMS", "IBM.CH8"], frozen_rom("ibm.ch8", "IBM", 132))
    tree.add_file(["ROMS", "TETRIS1.CH8"], frozen_rom("tetris1.ch8", "TETRIS", 494))
    if manifest:
        for image_path, host_path in manifest.items():
            parts = split_image_path(image_path)
            host = Path(host_path)
            if not host.is_absolute():
                host = ROOT / host
            tree.add_file(parts, host.read_bytes())

    img = bytearray(geo["total_sectors"] * BPS)
    fat = bytearray(geo["spf"] * BPS)
    struct.pack_into("<HH", fat, 0, 0xFFF8, 0xFFFF)   # media + EOC reserved entries
    next_cluster = 2

    def alloc(count):
        nonlocal next_cluster
        first = next_cluster
        if first + count > geo["clusters"] + 2:
            raise BuildError("image full: cluster heap exhausted")
        for c in range(first, first + count - 1):
            struct.pack_into("<H", fat, c * 2, c + 1)
        struct.pack_into("<H", fat, (first + count - 1) * 2, FAT16_EOC)
        next_cluster += count
        return first

    def cluster_off(c):
        return geo["data_start"] + (c - 2) * cb

    def write_dir(node, own_cluster, parent_cluster):
        entries = [dir_entry(b".          ", ATTR_DIRECTORY, own_cluster, 0),
                   dir_entry(b"..         ", ATTR_DIRECTORY, parent_cluster, 0)]
        for name, child in node.items():
            name11 = encode_83(name)
            if "dir" in child:
                if (len(child["dir"]) + 2) * 32 > cb:
                    raise BuildError(f"directory {name!r} exceeds one cluster")
                c = alloc(1)
                child["cluster"] = c
                entries.append(dir_entry(name11, ATTR_DIRECTORY, c, 0))
            else:
                data = child["data"]
                n = (len(data) + cb - 1) // cb
                c = alloc(n) if n else 0
                child["cluster"] = c
                entries.append(dir_entry(name11, ATTR_READ_ONLY, c, len(data)))
                for k in range(n):
                    off = cluster_off(c + k)
                    chunk = data[k * cb:(k + 1) * cb]
                    img[off:off + len(chunk)] = chunk
        if len(entries) * 32 > cb:
            raise BuildError("directory does not fit one cluster")
        buf = bytearray(cb)
        for k, e in enumerate(entries):
            buf[k * 32:(k + 1) * 32] = e
        img[cluster_off(own_cluster):cluster_off(own_cluster) + cb] = buf
        for child in node.values():
            if "dir" in child:
                write_dir(child["dir"], child["cluster"], own_cluster)

    # root directory: fixed area, volume label first, then the tree
    root_buf = bytearray(ROOT_ENTRIES * 32)
    root_buf[0:32] = dir_entry(VOLUME_LABEL, ATTR_VOLUME_ID, 0, 0)
    slot = 1
    for name, child in tree.root.items():
        name11 = encode_83(name)
        if "dir" in child:
            if (len(child["dir"]) + 2) * 32 > cb:
                raise BuildError(f"directory {name!r} exceeds one cluster")
            c = alloc(1)
            child["cluster"] = c
            root_buf[slot * 32:(slot + 1) * 32] = dir_entry(name11, ATTR_DIRECTORY, c, 0)
        else:
            data = child["data"]
            n = (len(data) + cb - 1) // cb
            c = alloc(n) if n else 0
            child["cluster"] = c
            root_buf[slot * 32:(slot + 1) * 32] = dir_entry(name11, ATTR_READ_ONLY, c, len(data))
            for k in range(n):
                off = cluster_off(c + k)
                chunk = data[k * cb:(k + 1) * cb]
                img[off:off + len(chunk)] = chunk
        slot += 1
    img[geo["root_start"]:geo["root_start"] + len(root_buf)] = root_buf
    for child in tree.root.values():
        if "dir" in child:
            write_dir(child["dir"], child["cluster"], 0)

    # both FATs, byte-identical
    for k in range(NUM_FATS):
        off = geo["fat_start"] + k * geo["spf"] * BPS
        img[off:off + len(fat)] = fat

    # boot sector / BPB
    boot = bytearray(BPS)
    boot[0:3] = b"\xEB\x3C\x90"
    boot[3:11] = OEM_NAME
    total16 = geo["total_sectors"] if geo["total_sectors"] < 0x10000 else 0
    total32 = 0 if total16 else geo["total_sectors"]
    struct.pack_into("<HBHBHHBHHHII", boot, 11,
                     BPS, geo["spc"], RESERVED_SECTORS, NUM_FATS, ROOT_ENTRIES,
                     total16, MEDIA, geo["spf"], SECTORS_PER_TRACK, NUM_HEADS,
                     HIDDEN_SECTORS, total32)
    boot[36] = DRIVE_NUMBER
    boot[37] = 0
    boot[38] = BOOT_SIG
    struct.pack_into("<I", boot, 39, VOLUME_ID)
    boot[43:54] = VOLUME_LABEL
    boot[54:62] = FS_TYPE
    boot[510:512] = b"\x55\xAA"
    img[0:BPS] = boot

    if not FAT16_MIN_CLUSTERS <= geo["clusters"] <= FAT16_MAX_CLUSTERS:
        raise BuildError("cluster count outside the FAT16 range")
    return bytes(img), geo


def load_manifest(path):
    obj = json.loads(Path(path).read_text())
    if not isinstance(obj, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in obj.items()):
        raise BuildError("manifest must be a JSON object of path -> host path")
    return obj


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help="output image path (default build/fs-v1/disk.img)")
    ap.add_argument("--size-mib", type=int, default=DEFAULT_SIZE_MIB,
                    help="image size in MiB (default 8)")
    ap.add_argument("--manifest", default=None,
                    help="JSON object mapping image paths to host files")
    args = ap.parse_args(argv)

    manifest = load_manifest(args.manifest) if args.manifest else None
    img, geo = build_image(args.size_mib, manifest)
    # determinism gate: a second build must be byte-identical
    img2, _ = build_image(args.size_mib, manifest)
    if img != img2:
        print("FAIL: repeated build is not byte-identical", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(img)
    sha = hashlib.sha256(img).hexdigest()
    print(f"image: {out}")
    print(f"size: {len(img)} bytes ({len(img) // (1024 * 1024)} MiB)")
    print(f"sha256: {sha}")
    print(f"geometry: bps={BPS} spc={geo['spc']} reserved={RESERVED_SECTORS} "
          f"fats={NUM_FATS} spf={geo['spf']} root_entries={ROOT_ENTRIES} "
          f"total_sectors={geo['total_sectors']} clusters={geo['clusters']} "
          f"cluster_bytes={geo['cluster_bytes']}")
    print(f"areas: fat_start={geo['fat_start']} root_start={geo['root_start']} "
          f"data_start={geo['data_start']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())