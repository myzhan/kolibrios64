#!/usr/bin/env python3
"""Freeze gate for the DESKTOP-v2 wallpaper asset (task 123).

Follows the font/cursor sha256 freeze organisation of this directory
(SPRITE_SHA256 in check.py) without touching the existing gate: the
3145728-byte BGRX asset kernel/desktop/assets/wallpaper.bgrx is pinned
byte for byte by WALLPAPER_SHA256 below, mirrored as big-endian qword
constants in kernel/desktop/wallpaper.inc (the user/chip8/api.inc ROM
digest convention).

The probe truth build/desktop-wallpaper/wallpaper-probes.json (the
build/user-chip8/ibm-probes.json organisation) is re-derived here by an
INDEPENDENT third PNG decoder written from the PNG spec, sharing no
code with tools/gen-wallpaper.py: every probe coordinate, the palette
and both histograms must match the JSON exactly.

Pure standard library only. Run:
  python3 tests/stage2-desktop/check-wallpaper.py
"""

import hashlib
import json
import re
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ASSET = ROOT / "kernel" / "desktop" / "assets" / "wallpaper.bgrx"
INC = ROOT / "kernel" / "desktop" / "wallpaper.inc"
PROBES_JSON = ROOT / "build" / "desktop-wallpaper" / "wallpaper-probes.json"
SRC = ROOT.parent / "kolibrios" / "data" / "common" / "wallpapers" / "T_Home.png"

WALLPAPER_W, WALLPAPER_H = 1024, 768
WALLPAPER_BYTES = 3145728
# sha256 of the 3145728 asset bytes, frozen 2026-11 by task 123
WALLPAPER_SHA256 = 'f9f8a12e5406e04b2007028bd168f72e9c25386d5bf92ef5ecb1aaeb18f36fec'
PALETTE_IDX0 = (24, 32, 40)

PROBE_POINTS = [
    (900, 700), (16, 767), (500, 32), (0, 0), (517, 387), (519, 392),
    (16, 64), (557, 411), (568, 411), (559, 416), (562, 416),
]


def independent_decode():
    """Third decoder, independent of tools/gen-wallpaper.py. Returns
    (palette, pixel(x, y) accessor) over the full 1920x1080 source."""
    blob = SRC.read_bytes()
    assert blob[:8] == b'\x89PNG\r\n\x1a\n'
    pos = 8
    palette = None
    stream = b''
    width = height = None
    while pos < len(blob):
        size = struct.unpack('>I', blob[pos:pos + 4])[0]
        tag = blob[pos + 4:pos + 8]
        body = blob[pos + 8:pos + 8 + size]
        if tag == b'IHDR':
            width, height, depth, color, comp, filt, lace = \
                struct.unpack('>IIBBBBB', body)
            assert (width, height, depth, color, comp, filt, lace) == \
                (1920, 1080, 2, 3, 0, 0, 0)
        elif tag == b'PLTE':
            palette = [tuple(body[i:i + 3]) for i in range(0, len(body), 3)]
        elif tag == b'IDAT':
            stream += body
        pos += size + 12
    assert palette is not None and len(palette) == 4
    stride = (width * 2 + 7) // 8
    inflated = zlib.decompress(stream)
    assert len(inflated) == height * (stride + 1)

    # Reconstruct the filtered scanlines into one flat bytearray first,
    # then extract pixels on demand: a different shape from both
    # decoders of the generator.
    flat = bytearray(height * stride)
    for y in range(height):
        ft = inflated[y * (stride + 1)]
        src_off = y * (stride + 1) + 1
        dst_off = y * stride
        up_off = dst_off - stride
        for i in range(stride):
            a = flat[dst_off + i - 1] if i > 0 else 0
            b = flat[up_off + i] if y > 0 else 0
            c = flat[up_off + i - 1] if (i > 0 and y > 0) else 0
            x = inflated[src_off + i]
            if ft == 0:
                pred = 0
            elif ft == 1:
                pred = a
            elif ft == 2:
                pred = b
            elif ft == 3:
                pred = (a + b) >> 1
            elif ft == 4:
                p = a + b - c
                da, db, dc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (da <= db and da <= dc) else (b if db <= dc else c)
            else:
                raise AssertionError(f'bad filter {ft} on row {y}')
            flat[dst_off + i] = (x + pred) & 0xFF

    def pixel(x, y):
        byte = flat[y * stride + x // 4]
        shift = 6 - 2 * (x % 4)
        return palette[(byte >> shift) & 3]

    return palette, pixel


def parse_inc_digest():
    text = INC.read_text()
    words = []
    for k in range(4):
        m = re.search(rf'^DESKTOP_WALLPAPER_SHA{k}\s*=\s*(0x[0-9A-Fa-f]+)',
                      text, re.M)
        assert m, f'DESKTOP_WALLPAPER_SHA{k} missing from wallpaper.inc'
        words.append(int(m.group(1), 16))
    digest = struct.pack('>4Q', *words).hex()
    assert WALLPAPER_SHA256 in text, 'sha256 comment missing from wallpaper.inc'
    assert "file    'assets/wallpaper.bgrx'" in text, \
        'wallpaper.inc must incbin assets/wallpaper.bgrx'
    return digest


def main():
    failed = 0

    def check(name, cond):
        nonlocal failed
        if cond:
            print(f'PASS  {name}')
        else:
            failed += 1
            print(f'FAIL  {name}')

    data = ASSET.read_bytes()
    check('asset size is exactly 3145728 bytes', len(data) == WALLPAPER_BYTES)
    sha = hashlib.sha256(data).hexdigest()
    check('asset sha256 matches the frozen WALLPAPER_SHA256',
          sha == WALLPAPER_SHA256)
    check('wallpaper.inc qword constants mirror the frozen digest',
          parse_inc_digest() == WALLPAPER_SHA256)

    palette, pixel = independent_decode()
    check('independent decode: palette idx0 = (24,32,40)',
          palette[0] == PALETTE_IDX0)

    truth = json.loads(PROBES_JSON.read_text())
    check('probe JSON asset_sha256 matches the frozen digest',
          truth['asset_sha256'] == WALLPAPER_SHA256)
    check('probe JSON asset_size is 3145728',
          truth['asset_size'] == WALLPAPER_BYTES)
    check('probe JSON palette matches the independent decode',
          [tuple(e['rgb']) for e in truth['palette']] == palette)

    # every JSON probe re-derived from the independent decode AND from
    # the packed asset dword
    json_probes = {(p['x'], p['y']): p['rgb'] for p in truth['probes']}
    check('probe JSON covers all 11 frozen sample points',
          set(json_probes) == set(PROBE_POINTS))
    bad = []
    for x, y in PROBE_POINTS:
        r, g, b = pixel(x, y)
        want = f'0x{r:02X}{g:02X}{b:02X}'
        off = (y * WALLPAPER_W + x) * 4
        ab, ag, ar, ax = data[off:off + 4]
        if json_probes.get((x, y)) != want or (ab, ag, ar, ax) != (b, g, r, 0):
            bad.append((x, y, want, json_probes.get((x, y))))
    check('all 11 probes agree: JSON == independent decode == asset', not bad)

    # independent histograms over the crop and the full source
    hist_crop = {f'0x{r:02X}{g:02X}{b:02X}': 0 for r, g, b in palette}
    hist_full = dict(hist_crop)
    for y in range(1080):
        for x in range(1920):
            r, g, b = pixel(x, y)
            key = f'0x{r:02X}{g:02X}{b:02X}'
            hist_full[key] += 1
            if x < WALLPAPER_W and y < WALLPAPER_H:
                hist_crop[key] += 1
    check('asset histogram matches the independent recount',
          truth['histogram_asset'] == hist_crop)
    check('source histogram matches the independent recount',
          truth['histogram_source'] == hist_full)
    check('crop histogram sums to 786432 pixels',
          sum(hist_crop.values()) == WALLPAPER_W * WALLPAPER_H)

    if failed:
        print(f'WALLPAPER FREEZE GATE FAILED ({failed} failures)')
        return 1
    print('WALLPAPER FREEZE GATE PASSED')
    return 0


if __name__ == '__main__':
    sys.exit(main())