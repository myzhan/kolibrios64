#!/usr/bin/env python3
"""Deterministic wallpaper asset pipeline for the 64-bit desktop shell.

DESKTOP-v2 T1 (task 123). Converts the 32-bit default wallpaper
kolibrios/data/common/wallpapers/T_Home.png (1920x1080, PNG colortype 3,
2-bit palette, 4 entries) into the flat BGRX asset the 64-bit kernel
embeds next to its code via kernel/desktop/wallpaper.inc.

The 32-bit shell tiles the wallpaper from the top left corner, so on the
frozen 1024x768 reference screen the visible pixels are exactly the top
left 1024x768 crop of the source image. Each pixel becomes one
little-endian dword B,G,R,0 (byte order in the file: B, G, R, 0), the
framebuffer format of kernel/video/fb.inc. Output size is exactly
1024 * 768 * 4 = 3145728 bytes.

Pure standard library only (zlib/struct/hashlib/json/argparse/pathlib),
no PIL, no third party packages. The conversion is deterministic: running
the script twice produces byte-identical outputs.

Usage:
  python3 tools/gen-wallpaper.py            # regenerate asset + probe JSON
  python3 tools/gen-wallpaper.py --check    # self-verification, no writes

Outputs:
  kernel/desktop/assets/wallpaper.bgrx      # the frozen kernel asset
  build/desktop-wallpaper/wallpaper-probes.json
                                            # host probe truth, follows the
                                            # build/user-chip8/ibm-probes.json
                                            # organisation
"""

import argparse
import hashlib
import json
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT.parent / "kolibrios" / "data" / "common" / "wallpapers" / "T_Home.png"
ASSET = ROOT / "kernel" / "desktop" / "assets" / "wallpaper.bgrx"
OUT = ROOT / "build" / "desktop-wallpaper"
PROBES_JSON = OUT / "wallpaper-probes.json"

SRC_W, SRC_H = 1920, 1080
CROP_W, CROP_H = 1024, 768
ASSET_BYTES = CROP_W * CROP_H * 4          # 3145728
SRC_STRIDE = (SRC_W * 2 + 7) // 8          # 480 bytes of packed 2-bit pixels

# The frozen host probe sample points (x, y) in crop coordinates.
PROBE_POINTS = [
    (900, 700), (16, 767), (500, 32), (0, 0), (517, 387), (519, 392),
    (16, 64), (557, 411), (568, 411), (559, 416), (562, 416),
]


def png_chunks(data):
    """Yield (type, payload) with the CRC of every chunk verified."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    off = 8
    while off < len(data):
        length, ctype = struct.unpack(">I4s", data[off:off + 8])
        payload = data[off + 8:off + 8 + length]
        crc, = struct.unpack(">I", data[off + 8 + length:off + 12 + length])
        if zlib.crc32(ctype + payload) & 0xFFFFFFFF != crc:
            raise ValueError(f"chunk {ctype!r} fails CRC")
        yield ctype, payload
        off += 12 + length


def paeth(a, b, c):
    """The PNG Paeth predictor (filter type 4)."""
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def decode_source(path):
    """Primary decoder: return (palette, rows) with rows[y][x] = palette
    index of the pixel, full 1920x1080, all five PNG filters supported."""
    data = Path(path).read_bytes()
    ihdr = plte = None
    idat = bytearray()
    for ctype, payload in png_chunks(data):
        if ctype == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", payload)
        elif ctype == b"PLTE":
            plte = [tuple(payload[i:i + 3]) for i in range(0, len(payload), 3)]
        elif ctype == b"IDAT":
            idat += payload
    w, h, depth, ctype_, comp, filt, interlace = ihdr
    if (w, h, depth, ctype_, comp, filt, interlace) != \
            (SRC_W, SRC_H, 2, 3, 0, 0, 0):
        raise ValueError(f"unexpected IHDR {ihdr}, expected 1920x1080 "
                         "2-bit colortype-3 non-interlaced")
    if plte is None or len(plte) != 4:
        raise ValueError("expected a 4-entry PLTE")
    raw = zlib.decompress(bytes(idat))
    if len(raw) != h * (1 + SRC_STRIDE):
        raise ValueError("decompressed IDAT size mismatch")

    # Unfilter byte-wise; for sub-byte pixels the PNG filter unit (bpp)
    # is 1 byte, so left/up/upleft are plain byte neighbours.
    rows = []
    prev = bytearray(SRC_STRIDE)
    for y in range(h):
        base = y * (1 + SRC_STRIDE)
        ftype = raw[base]
        line = bytearray(raw[base + 1:base + 1 + SRC_STRIDE])
        if ftype == 0:
            pass
        elif ftype == 1:                      # Sub
            for i in range(1, SRC_STRIDE):
                line[i] = (line[i] + line[i - 1]) & 0xFF
        elif ftype == 2:                      # Up
            for i in range(SRC_STRIDE):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:                      # Average
            for i in range(SRC_STRIDE):
                left = line[i - 1] if i else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:                      # Paeth
            for i in range(SRC_STRIDE):
                left = line[i - 1] if i else 0
                upleft = prev[i - 1] if i else 0
                line[i] = (line[i] + paeth(left, prev[i], upleft)) & 0xFF
        else:
            raise ValueError(f"row {y}: unknown filter type {ftype}")
        # 2-bit extraction, MSB-first within each byte
        row = bytearray(w)
        for x in range(w):
            row[x] = (line[x >> 2] >> (6 - (x & 3) * 2)) & 3
        rows.append(row)
        prev = line
    return plte, rows


def decode_source_alt(path):
    """Independent second decoder used by --check. Different structure:
    filters applied through per-type closures over integer lists, pixel
    extraction through a flat bit-position formula instead of the
    byte/shift pair of the primary decoder."""
    data = Path(path).read_bytes()
    off = 8
    plte = None
    idat = bytearray()
    while off < len(data):
        length = int.from_bytes(data[off:off + 4], "big")
        ctype = data[off + 4:off + 8]
        payload = data[off + 8:off + 8 + length]
        if ctype == b"PLTE":
            plte = [tuple(payload[i:i + 3]) for i in range(0, len(payload), 3)]
        elif ctype == b"IDAT":
            idat.extend(payload)
        off += 12 + length
    raw = zlib.decompress(bytes(idat))

    def predictor(kind, left, up, upleft):
        if kind == 0:
            return 0
        if kind == 1:
            return left
        if kind == 2:
            return up
        if kind == 3:
            return (left + up) // 2
        p = left + up - upleft
        dist = (abs(p - left), abs(p - up), abs(p - upleft))
        return (left, up, upleft)[dist.index(min(dist))]

    rows = []
    prev = [0] * SRC_STRIDE
    for y in range(SRC_H):
        base = y * (1 + SRC_STRIDE)
        kind = raw[base]
        cur = []
        for i in range(SRC_STRIDE):
            left = cur[i - 1] if i else 0
            upleft = prev[i - 1] if i else 0
            cur.append((raw[base + 1 + i]
                        + predictor(kind, left, prev[i], upleft)) % 256)
        row = []
        for x in range(SRC_W):
            bit = 2 * x                       # flat MSB-first bit position
            row.append((cur[bit // 8] >> (6 - bit % 8)) % 4)
        rows.append(row)
        prev = cur
    return plte, rows


def pack_asset(palette, rows):
    """Crop the top left 1024x768 and pack little-endian BGRX dwords."""
    out = bytearray(ASSET_BYTES)
    pos = 0
    for y in range(CROP_H):
        row = rows[y]
        for x in range(CROP_W):
            r, g, b = palette[row[x]]
            out[pos] = b
            out[pos + 1] = g
            out[pos + 2] = r
            out[pos + 3] = 0
            pos += 4
    return bytes(out)


def rgb_hex(rgb):
    return "0x%02X%02X%02X" % rgb


def histogram(palette, rows, w, h):
    counts = [0] * len(palette)
    for y in range(h):
        for x in range(w):
            counts[rows[y][x]] += 1
    return {rgb_hex(palette[i]): counts[i] for i in range(len(palette))}


def build_outputs():
    """Decode, pack and compose every output byte (nothing written yet)."""
    palette, rows = decode_source(SRC)
    asset = pack_asset(palette, rows)
    probes = [{"x": x, "y": y, "rgb": rgb_hex(palette[rows[y][x]])}
              for x, y in PROBE_POINTS]
    truth = {
        "source": "kolibrios/data/common/wallpapers/T_Home.png",
        "source_sha256": hashlib.sha256(SRC.read_bytes()).hexdigest(),
        "source_size": [SRC_W, SRC_H],
        "asset": "kernel/desktop/assets/wallpaper.bgrx",
        "asset_sha256": hashlib.sha256(asset).hexdigest(),
        "asset_size": len(asset),
        "crop": {"x": 0, "y": 0, "w": CROP_W, "h": CROP_H},
        "pixel_format": "little-endian dword B,G,R,0 (BGRX)",
        "palette": [{"idx": i, "rgb": list(palette[i]),
                     "hex": rgb_hex(palette[i])}
                    for i in range(len(palette))],
        "histogram_asset": histogram(palette, rows, CROP_W, CROP_H),
        "histogram_source": histogram(palette, rows, SRC_W, SRC_H),
        "probes": probes,
    }
    js = json.dumps(truth, indent=2) + "\n"
    return palette, rows, asset, truth, js


def generate():
    palette, rows, asset, truth, js = build_outputs()
    ASSET.parent.mkdir(parents=True, exist_ok=True)
    ASSET.write_bytes(asset)
    OUT.mkdir(parents=True, exist_ok=True)
    PROBES_JSON.write_text(js)
    print(f"asset  {ASSET.relative_to(ROOT)}  {len(asset)} bytes")
    print(f"sha256 {truth['asset_sha256']}")
    print(f"probes {PROBES_JSON.relative_to(ROOT)}")
    for p in truth["probes"]:
        print(f"  ({p['x']:4d},{p['y']:4d}) = {p['rgb']}")
    print(f"palette {[e['hex'] for e in truth['palette']]}")
    print(f"histogram(asset) {truth['histogram_asset']}")
    return 0


def check():
    """Self-verification, writes nothing. Every failure raises."""
    failures = []

    def ok(name):
        print(f"PASS  {name}")

    def require(cond, name):
        if cond:
            ok(name)
        else:
            failures.append(name)
            print(f"FAIL  {name}")

    palette, rows, asset, truth, js = build_outputs()

    # 1. determinism: a fresh recomputation matches the on-disk bytes
    require(ASSET.read_bytes() == asset,
            "on-disk asset is byte-identical to a fresh recomputation")
    require(PROBES_JSON.read_text() == js,
            "on-disk probe JSON is byte-identical to a fresh recomputation")

    # 2. frozen geometry and palette
    require(len(asset) == ASSET_BYTES == 3145728,
            "asset size is exactly 3145728 bytes")
    require(palette[0] == (24, 32, 40),
            "palette idx0 is (24,32,40) = 0x182028")
    require(truth["asset_sha256"]
            == hashlib.sha256(ASSET.read_bytes()).hexdigest(),
            "JSON sha256 matches the on-disk asset")

    # 3. independent second decode: every probe point plus a 16-point
    #    deterministic grid, cross-checked against BOTH the JSON and the
    #    packed asset dword
    pal2, rows2 = decode_source_alt(SRC)
    require(pal2 == palette, "second decoder agrees on the palette")
    extra = [(x, y) for y in (0, 255, 511, 767) for x in (0, 341, 682, 1023)]
    points = PROBE_POINTS + extra
    json_at = {(p["x"], p["y"]): p["rgb"] for p in truth["probes"]}
    mismatches = []
    for x, y in points:
        want = rgb_hex(pal2[rows2[y][x]])
        off = (y * CROP_W + x) * 4
        b, g, r, pad = asset[off:off + 4]
        got_asset = rgb_hex((r, g, b))
        if want != got_asset or pad != 0:
            mismatches.append((x, y, want, got_asset, pad))
        if (x, y) in json_at and json_at[(x, y)] != want:
            mismatches.append((x, y, want, json_at[(x, y)], "json"))
    require(not mismatches,
            f"independent re-decode agrees on {len(points)} coordinates "
            f"(11 probes + 16 grid) vs JSON and asset; bad={mismatches}")

    # 4. whole-asset invariants: X byte zero, colours inside the palette,
    #    histogram sums match the pixel counts
    pal_set = {rgb_hex(c) for c in palette}
    bad_pad = sum(1 for i in range(3, len(asset), 4) if asset[i] != 0)
    require(bad_pad == 0, "every dword has a zero X byte")
    hist = truth["histogram_asset"]
    require(set(hist) <= pal_set and sum(hist.values()) == CROP_W * CROP_H,
            "asset histogram covers only palette colours and sums to 786432")
    require(sum(truth["histogram_source"].values()) == SRC_W * SRC_H,
            "source histogram sums to 2073600")

    if failures:
        print(f"WALLPAPER SELF-CHECK FAILED ({len(failures)} failures)")
        return 1
    print("WALLPAPER SELF-CHECK PASSED")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="self-verification only, writes nothing")
    args = ap.parse_args()
    return check() if args.check else generate()


if __name__ == "__main__":
    sys.exit(main())