#!/usr/bin/env python3
"""GUI-G1 integration judge fixtures (task 78, decision 5).

Proves the extended tools/run-qemu-debug.py judge() catches integration
defects, in the same negative-fixture spirit as tests/stage2-fb/check.py:
a synthetic PASS baseline (serial + counters + a PPM rendered from the
frozen task-77 spec model) must pass, and every mutation below must FAIL.
Nothing here executes target code; the judge is a pure function.

Run: python3 tests/stage2-fb/check-gui-judge.py
"""

import hashlib
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rq = load_module('rq', REPO / 'tools' / 'run-qemu-debug.py')
fb = load_module('fbcheck', HERE / 'check.py')

CHECKS = 0
FAILED = 0


def check(name, cond):
    global CHECKS, FAILED
    CHECKS += 1
    if not cond:
        FAILED += 1
        print('FAIL: %s' % name)


def font_bytes():
    baseline = REPO.parent / 'kolibrios' / 'kernel' / 'trunk' / 'gui' / 'charUni.mt'
    if baseline.is_file():
        sub = baseline.read_bytes()[fb.FONT_FIRST * 16:(fb.FONT_LAST + 1) * 16]
    else:
        # same tolerance as check.py check_font: when the 32 bit baseline
        # is unreachable the frozen sha256 pins the subset, derived here
        # from the module's embedded font through the probe assembly
        sub = derived_font()
    assert hashlib.sha256(sub).hexdigest() == fb.FONT_SHA256, 'font drift'
    return sub


def derived_font():
    """The embedded font subset from a flat probe assembly (org 0)."""
    img, _ = fb.assemble(0, 'flat')
    man = fb.load_manifest(img)
    sub = img[man['video_font']:man['video_font_end']]
    for path in sorted(fb.BUILD.glob('fbprobe-*')):
        path.unlink()
    return sub


def render_ppm(rows):
    w, h = len(rows[0]), len(rows)
    px = bytearray()
    for row in rows:
        for c in row:
            px += bytes(((c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF))
    return b'P6\n%d %d\n255\n' % (w, h) + bytes(px)


def serial_ok():
    return '\r\n'.join([
        'K64-BOOT-OK', 'K64-CPU-OK',
        'K64-CPU-SELFTEST passed = 0x5FF',
        'K64-PMM-OK', 'K64-VMM-OK', 'K64-HEAP-OK', 'K64-IRQ-OK',
        'K64-GUI-OK', 'K64-FOUNDATION-OK', ''])


def serial_skip():
    return serial_ok().replace('K64-GUI-OK', 'K64-GUI-SKIP')


DEBUG = 'CPU Reset (VCPU 0)\nCPU Reset (VCPU 0)\n'


def main():
    font = font_bytes()
    # the fallback derivation must yield the very same frozen bytes, so
    # a missing baseline can never silently change the rendered frame
    check('fallback font derivation matches the frozen subset',
          derived_font() == font)
    frame = fb.spec_frame(font)
    ppm = render_ppm(frame)
    counters = dict(rq.EXPECTED_COUNTERS)

    # structural pins of the extension itself
    check('eight markers', len(rq.MARKERS) == 8)
    check('GUI marker between IRQ and FOUNDATION',
          rq.MARKERS.index('K64-GUI-OK') == rq.MARKERS.index('K64-IRQ-OK') + 1
          and rq.MARKERS.index('K64-FOUNDATION-OK')
          == rq.MARKERS.index('K64-GUI-OK') + 1)
    check('28 frozen probes', len(rq.PPM_PROBES) == 28)
    check('video_selftest_mask expected 0',
          rq.EXPECTED_COUNTERS.get('video_selftest_mask') == 0)
    check('the spec model satisfies every probe',
          all(frame[y][x] == want for x, y, want in rq.PPM_PROBES))

    # positive baselines
    ok, lines = rq.judge(serial_ok(), DEBUG, counters, False, ppm=ppm)
    check('ok baseline PASS', ok)
    if not ok:
        print('\n'.join(l for l in lines if 'FAIL' in l))
    ok, _ = rq.judge(serial_skip(), DEBUG, counters, False,
                     gui_mode='skip', ppm=None)
    check('skip baseline PASS', ok)
    ok, _ = rq.judge(serial_skip(), DEBUG, counters, False,
                     gui_mode='skip', ppm=ppm)
    check('skip mode ignores the PPM (no pixel assertions)', ok)

    # negative fixtures: every one must FAIL
    negatives = []

    s = serial_ok().replace('K64-GUI-OK\r\n', '')
    negatives.append(('GUI-OK marker removed', s, DEBUG, counters, 'ok', ppm))

    s = serial_ok().replace('K64-IRQ-OK\r\nK64-GUI-OK',
                            'K64-GUI-OK\r\nK64-IRQ-OK')
    negatives.append(('GUI-OK emitted before IRQ', s, DEBUG, counters,
                      'ok', ppm))

    s = serial_ok().replace('K64-GUI-OK', 'K64-GUI-OK\r\nK64-GUI-OK')
    negatives.append(('GUI-OK duplicated', s, DEBUG, counters, 'ok', ppm))

    bad = bytearray(ppm)
    off = len(b'P6\n1024 768\n255\n') + (100 * 1024 + 64) * 3
    bad[off] = 0x00  # bar 0 center red -> blue
    negatives.append(('one probe pixel corrupted', serial_ok(), DEBUG,
                      counters, 'ok', bytes(bad)))

    small = b'P6\n800 600\n255\n' + bytes(800 * 600 * 3)
    negatives.append(('PPM geometry 800x600', serial_ok(), DEBUG, counters,
                      'ok', small))

    negatives.append(('PPM missing', serial_ok(), DEBUG, counters, 'ok', None))

    bad_counters = dict(counters, video_selftest_mask=1)
    negatives.append(('video_selftest_mask nonzero', serial_ok(), DEBUG,
                      bad_counters, 'ok', ppm))

    negatives.append(('skip serial judged in ok mode', serial_skip(), DEBUG,
                      counters, 'ok', ppm))
    negatives.append(('ok serial judged in skip mode', serial_ok(), DEBUG,
                      counters, 'skip', None))

    s = serial_skip().replace('K64-IRQ-OK\r\nK64-GUI-SKIP',
                              'K64-GUI-SKIP\r\nK64-IRQ-OK')
    negatives.append(('GUI-SKIP before IRQ', s, DEBUG, counters, 'skip', None))

    s = serial_skip().replace('K64-GUI-SKIP',
                              'K64-GUI-SKIP\r\nK64-GUI-SKIP')
    negatives.append(('GUI-SKIP duplicated', s, DEBUG, counters,
                      'skip', None))

    s = serial_ok() + 'K64-CPU-PANIC\r\n'
    negatives.append(('panic after FOUNDATION', s, DEBUG, counters, 'ok', ppm))

    for name, s, d, c, mode, p in negatives:
        ok, lines = rq.judge(s, d, c, False, gui_mode=mode, ppm=p)
        check('negative fixture caught: %s' % name, not ok)
        if ok:
            print('  (judge wrongly passed: %s)' % name)

    print('check-gui-judge: %d checks, %d failed' % (CHECKS, FAILED))
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())