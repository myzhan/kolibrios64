#!/usr/bin/env python3
"""GUI-G1 integration judge fixtures (task 78, decision 5), extended
by task 89 for the IRQ-v2 INPUT marker/counters, by task 96 for the
DESKTOP-v1 marker/counters/two desktop frames/vector derivations, and
by task 102 for the third capture: the GUI-G1 pattern frame
(screen-gui.ppm) taken inside the bounded foundation handshake window
before desktop_init repaints it, and by task 108 for the LSB-first
font bit order: the frame models decode bit0 as the leftmost pixel
(the charUni.mt truth) and mirrored MSB-first frames must FAIL.

Proves the extended tools/run-qemu-debug.py judge() catches integration
defects, in the same negative-fixture spirit as tests/stage2-fb/check.py:
a synthetic PASS baseline (serial + counters + THREE PPMs rendered from
the frozen task-77 pattern spec and the frozen desktop frame spec) must
pass, and every mutation below must FAIL. Nothing here executes target
code; the judge is a pure function.

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
dt = load_module('dtcheck', REPO / 'tests' / 'stage2-desktop' / 'check.py')

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


PPM_HEADER = b'P6\n1024 768\n255\n'


def set_pixel(ppm, x, y, rgb):
    """A copy of the 1024x768 PPM with one pixel overwritten."""
    bad = bytearray(ppm)
    off = len(PPM_HEADER) + (y * 1024 + x) * 3
    bad[off] = (rgb >> 16) & 0xFF
    bad[off + 1] = (rgb >> 8) & 0xFF
    bad[off + 2] = rgb & 0xFF
    return bytes(bad)


def draw_text(rows, font, text, x0, y0, color):
    for index, ch in enumerate(text):
        glyph = font[(ch - fb.FONT_FIRST) * fb.FONT_STRIDE:
                     (ch - fb.FONT_FIRST + 1) * fb.FONT_STRIDE]
        for row in range(fb.FONT_HEIGHT):
            for col in range(fb.FONT_WIDTH):
                if glyph[row] & (1 << col):  # LSB-first: bit0 = leftmost
                    rows[y0 + row][x0 + index * fb.FONT_WIDTH + col] = color


def mirror_font(font):
    """task-108 negative model: every bitmap row byte bit-reversed, so
    the LSB-first models render each glyph horizontally mirrored (the
    exact output of the old MSB-first renderer on the correct data)."""
    return bytes(int(format(b, '08b')[::-1], 2) for b in font)


def desktop_frame(font, cursor_xy, echo_text=b'hi'):
    """The frozen DESKTOP-v1 frame model: background, title bar, title
    text, the echoed keys and the cursor sprite composited at cursor_xy
    ('.' transparent, black outline, white fill)."""
    rows = [[dt.BG_COLOR] * dt.SCREEN_W for _ in range(dt.SCREEN_H)]
    for y in range(dt.BAR_Y, dt.BAR_Y + dt.BAR_H):
        for x in range(dt.BAR_X, dt.BAR_X + dt.BAR_W):
            rows[y][x] = dt.BAR_COLOR
    draw_text(rows, font, dt.TITLE_TEXT[:-1], dt.TITLE_X, dt.TITLE_Y,
              dt.TITLE_COLOR)
    draw_text(rows, font, echo_text, dt.ECHO_X, dt.ECHO_Y, dt.ECHO_COLOR)
    cx, cy = cursor_xy
    for r in range(dt.SPRITE_H):
        for c in range(dt.SPRITE_W):
            v = dt.SPRITE[r * dt.SPRITE_W + c]
            if v == 1:
                rows[cy + r][cx + c] = dt.CURSOR_BLACK
            elif v == 2:
                rows[cy + r][cx + c] = dt.CURSOR_WHITE
    return rows


def serial_ok():
    return '\r\n'.join([
        'K64-BOOT-OK', 'K64-CPU-OK',
        'K64-CPU-SELFTEST passed = 0x5FF',
        'K64-PMM-OK', 'K64-VMM-OK', 'K64-HEAP-OK', 'K64-IRQ-OK',
        'K64-INPUT-OK', 'K64-GUI-OK', 'K64-DESKTOP-OK',
        'K64-FOUNDATION-OK', ''])


def serial_skip():
    return (serial_ok().replace('K64-GUI-OK', 'K64-GUI-SKIP')
            .replace('K64-DESKTOP-OK', 'K64-DESKTOP-SKIP'))


def debug_log(n21, n2c):
    """Two CPU Reset lines (power-on + OVMF) plus the -d int vector
    delivery evidence, one line per 8042 byte delivered."""
    return ('CPU Reset (VCPU 0)\n' * 2
            + 'v=21\n' * n21 + 'v=2c\n' * n2c)


DEBUG_OK = debug_log(6, 18)
DEBUG_SKIP = debug_log(2, 9)

KEEP = object()


def main():
    font = font_bytes()
    # the fallback derivation must yield the very same frozen bytes, so
    # a missing baseline can never silently change the rendered frame
    check('fallback font derivation matches the frozen subset',
          derived_font() == font)

    # the THREE frozen frames: the GUI-G1 pattern (task-77 spec model),
    # the desktop echo frame (cursor at the initial 517,387) and the
    # desktop final frame (cursor moved to 557,411)
    gui_rows = fb.spec_frame(font)
    echo_rows = desktop_frame(font, (dt.CURSOR_X0, dt.CURSOR_Y0))
    final_rows = desktop_frame(font, (dt.ST_CURSOR_X, dt.ST_CURSOR_Y))
    ppm_gui = render_ppm(gui_rows)
    ppm_echo = render_ppm(echo_rows)
    ppm_final = render_ppm(final_rows)
    counters = dict(rq.EXPECTED_COUNTERS)
    counters_skip = dict(rq.EXPECTED_COUNTERS_SKIP)

    # structural pins of the extension itself
    check('ten markers', len(rq.MARKERS) == 10)
    check('INPUT marker between IRQ and GUI',
          rq.MARKERS.index('K64-INPUT-OK') == rq.MARKERS.index('K64-IRQ-OK') + 1
          and rq.MARKERS.index('K64-GUI-OK')
          == rq.MARKERS.index('K64-INPUT-OK') + 1)
    check('DESKTOP marker between GUI and FOUNDATION',
          rq.MARKERS.index('K64-DESKTOP-OK')
          == rq.MARKERS.index('K64-GUI-OK') + 1
          and rq.MARKERS.index('K64-FOUNDATION-OK')
          == rq.MARKERS.index('K64-DESKTOP-OK') + 1)
    check('32 frozen GUI probes', len(rq.PPM_GUI_PROBES) == 32)
    check('27 common desktop probes', len(rq.PPM_COMMON_PROBES) == 27)
    check('3 echo frame probes', len(rq.PPM_ECHO_PROBES) == 3)
    check('7 final frame probes', len(rq.PPM_FINAL_PROBES) == 7)
    check('the GUI spec model satisfies every GUI probe',
          all(gui_rows[y][x] == want for x, y, want in rq.PPM_GUI_PROBES))
    check('the echo frame model satisfies every echo probe',
          all(echo_rows[y][x] == want for x, y, want
              in rq.PPM_COMMON_PROBES + rq.PPM_ECHO_PROBES))
    check('the final frame model satisfies every final probe',
          all(final_rows[y][x] == want for x, y, want
              in rq.PPM_COMMON_PROBES + rq.PPM_FINAL_PROBES))
    # the ok-mode counters derive from the WHOLE injection chain:
    # kbd 2+4, mouse 3+3, ticks frozen 11, desktop keys 2, cursor
    # 517+40/387+24, the GUI frame handshake ended at DONE (3)
    for name, want in (('video_selftest_mask', 0),
                       ('input_test_selftest_mask', 0),
                       ('input_test_kbd_count', 6),
                       ('input_test_mouse_count', 6),
                       ('input_test_stage', 6),
                       ('irq_test_ticks', 11),
                       ('desktop_test_selftest_mask', 0),
                       ('desktop_test_stage', 5),
                       ('desktop_test_keys', 2),
                       ('desktop_test_cursor_x', 557),
                       ('desktop_test_cursor_y', 411),
                       ('desktop_gui_frame_stage', 3)):
        check('%s expected %d' % (name, want),
              rq.EXPECTED_COUNTERS.get(name) == want)
    for name, want in (('input_test_kbd_count', 2),
                       ('input_test_mouse_count', 3),
                       ('desktop_test_stage', 0),
                       ('desktop_test_keys', 0),
                       ('desktop_gui_frame_stage', 0)):
        check('skip %s expected %d' % (name, want),
              rq.EXPECTED_COUNTERS_SKIP.get(name) == want)
    check('gui_frame_stage counter symbol present',
          ('desktop_gui_frame_stage', 8) in rq.COUNTER_SYMBOLS)
    check('ok vectors derived 6/18',
          rq.EXPECTED_VECTORS['ok'] == {'21': 6, '2c': 18})
    check('skip vectors derived 2/9',
          rq.EXPECTED_VECTORS['skip'] == {'21': 2, '2c': 9})
    check('GUI frame handshake stages 1/2',
          rq.GUI_FRAME_READY == 1 and rq.GUI_FRAME_RELEASE == 2)
    check('GUI frame slot offset agrees with the desktop copy',
          dt.OFF_GUI_FRAME == 104)

    # positive baselines
    ok, lines = rq.judge(serial_ok(), DEBUG_OK, counters, False,
                         ppm=ppm_final, ppm_echo=ppm_echo, ppm_gui=ppm_gui)
    check('ok baseline PASS (three frames)', ok)
    if not ok:
        print('\n'.join(l for l in lines if 'FAIL' in l))
    ok, _ = rq.judge(serial_skip(), DEBUG_SKIP, counters_skip, False,
                     gui_mode='skip')
    check('skip baseline PASS', ok)
    ok, _ = rq.judge(serial_skip(), DEBUG_SKIP, counters_skip, False,
                     gui_mode='skip', ppm=ppm_final, ppm_echo=ppm_echo,
                     ppm_gui=ppm_gui)
    check('skip mode ignores the PPMs (no pixel assertions)', ok)

    # negative fixtures: every one must FAIL
    negatives = []

    def neg(name, s=None, d=None, c=None, mode='ok',
            p=KEEP, pe=KEEP, pg=KEEP):
        negatives.append((name,
                          serial_ok() if s is None else s,
                          DEBUG_OK if d is None else d,
                          counters if c is None else c,
                          mode,
                          ppm_final if p is KEEP else p,
                          ppm_echo if pe is KEEP else pe,
                          ppm_gui if pg is KEEP else pg))

    # ---- marker order / count / hygiene ----------------------------
    neg('DESKTOP-OK marker removed',
        s=serial_ok().replace('K64-DESKTOP-OK\r\n', ''))
    neg('DESKTOP-OK emitted before GUI',
        s=serial_ok().replace('K64-GUI-OK\r\nK64-DESKTOP-OK',
                              'K64-DESKTOP-OK\r\nK64-GUI-OK'))
    neg('DESKTOP-OK duplicated',
        s=serial_ok().replace('K64-DESKTOP-OK',
                              'K64-DESKTOP-OK\r\nK64-DESKTOP-OK'))
    neg('GUI-OK marker removed',
        s=serial_ok().replace('K64-GUI-OK\r\n', ''))
    neg('GUI-OK emitted before INPUT',
        s=serial_ok().replace('K64-INPUT-OK\r\nK64-GUI-OK',
                              'K64-GUI-OK\r\nK64-INPUT-OK'))
    neg('GUI-OK duplicated',
        s=serial_ok().replace('K64-GUI-OK', 'K64-GUI-OK\r\nK64-GUI-OK'))
    neg('INPUT-OK marker removed',
        s=serial_ok().replace('K64-INPUT-OK\r\n', ''))
    neg('INPUT-OK emitted before IRQ',
        s=serial_ok().replace('K64-IRQ-OK\r\nK64-INPUT-OK',
                              'K64-INPUT-OK\r\nK64-IRQ-OK'))
    neg('INPUT-OK duplicated',
        s=serial_ok().replace('K64-INPUT-OK', 'K64-INPUT-OK\r\nK64-INPUT-OK'))
    neg('panic after FOUNDATION', s=serial_ok() + 'K64-CPU-PANIC\r\n')
    neg('skip serial judged in ok mode', s=serial_skip())
    neg('ok serial judged in skip mode', mode='skip', c=counters_skip,
        d=DEBUG_SKIP)

    # ---- the GUI pattern frame (task 102) ---------------------------
    neg('gui PPM missing', pg=None)
    neg('gui probe pixel corrupted (bar 0 center)',
        pg=set_pixel(ppm_gui, 64, 100, 0x0000FF))
    neg('gui rectangle pixel corrupted',
        pg=set_pixel(ppm_gui, 412, 284, 0xFF00FF))
    neg('gui text pixel corrupted',
        pg=set_pixel(ppm_gui, 9, 10, 0xFF0000))
    neg('gui PPM geometry 800x600',
        pg=b'P6\n800 600\n255\n' + bytes(800 * 600 * 3))
    neg('gui capture taken after the desktop repaint', pg=ppm_final)

    # ---- the desktop echo frame -------------------------------------
    neg('echo PPM missing', pe=None)
    neg('echo cursor pixel corrupted',
        pe=set_pixel(ppm_echo, 517, 387, dt.BG_COLOR))
    neg('echo glyph pixel corrupted',
        pe=set_pixel(ppm_echo, 17, 69, dt.BG_COLOR))
    neg('echo capture taken after the mouse move', pe=ppm_final)

    # ---- the desktop final frame ------------------------------------
    neg('final PPM missing', p=None)
    neg('final cursor pixel corrupted',
        p=set_pixel(ppm_final, 557, 411, dt.BG_COLOR))
    neg('final old-position pixel not restored',
        p=set_pixel(ppm_final, 517, 387, 0x000000))
    neg('final capture taken before the mouse move', p=ppm_echo)
    neg('final PPM geometry 800x600',
        p=b'P6\n800 600\n255\n' + bytes(800 * 600 * 3))

    # ---- task-108: mirrored text frames (MSB-first decode) ----------
    mirrored = mirror_font(font)
    check('the mirror model actually differs from the truth',
          mirrored != font)
    neg('mirrored GUI text (MSB-first decode)',
        pg=render_ppm(fb.spec_frame(mirrored)))
    neg('mirrored echo text (MSB-first decode)',
        pe=render_ppm(desktop_frame(mirrored,
                                    (dt.CURSOR_X0, dt.CURSOR_Y0))))
    neg('mirrored final text (MSB-first decode)',
        p=render_ppm(desktop_frame(mirrored,
                                   (dt.ST_CURSOR_X, dt.ST_CURSOR_Y))))

    # ---- counter drift ----------------------------------------------
    for name, value in (('video_selftest_mask', 1),
                        ('input_test_selftest_mask', 0x10),
                        ('input_test_kbd_count', 5),
                        ('input_test_mouse_count', 5),
                        ('input_test_stage', 5),
                        ('irq_test_ticks', 10),
                        ('desktop_test_selftest_mask', 1),
                        ('desktop_test_stage', 4),
                        ('desktop_test_keys', 1),
                        ('desktop_test_cursor_x', 517),
                        ('desktop_test_cursor_y', 387),
                        ('desktop_gui_frame_stage', 0),
                        ('desktop_gui_frame_stage', 1),
                        ('desktop_gui_frame_stage', 2)):
        neg('%s drifted to %d' % (name, value),
            c=dict(counters, **{name: value}))
    missing = dict(counters)
    del missing['desktop_gui_frame_stage']
    neg('gui_frame_stage counter unreadable', c=missing)

    # ---- vector delivery drift --------------------------------------
    neg('v=21 deliveries 5 (need 6)', d=debug_log(5, 18))
    neg('v=21 deliveries 7 (need 6)', d=debug_log(7, 18))
    neg('v=2c deliveries 17 (need 18)', d=debug_log(6, 17))
    neg('v=2c deliveries 19 (need 18)', d=debug_log(6, 19))

    # ---- skip mode ---------------------------------------------------
    neg('GUI-SKIP before INPUT', mode='skip', c=counters_skip, d=DEBUG_SKIP,
        s=serial_skip().replace('K64-INPUT-OK\r\nK64-GUI-SKIP',
                                'K64-GUI-SKIP\r\nK64-INPUT-OK'))
    neg('GUI-SKIP duplicated', mode='skip', c=counters_skip, d=DEBUG_SKIP,
        s=serial_skip().replace('K64-GUI-SKIP',
                                'K64-GUI-SKIP\r\nK64-GUI-SKIP'))
    neg('DESKTOP-SKIP removed', mode='skip', c=counters_skip, d=DEBUG_SKIP,
        s=serial_skip().replace('K64-DESKTOP-SKIP\r\n', ''))
    neg('DESKTOP-SKIP duplicated', mode='skip', c=counters_skip, d=DEBUG_SKIP,
        s=serial_skip().replace('K64-DESKTOP-SKIP',
                                'K64-DESKTOP-SKIP\r\nK64-DESKTOP-SKIP'))
    neg('DESKTOP-SKIP before GUI-SKIP', mode='skip', c=counters_skip,
        d=DEBUG_SKIP,
        s=serial_skip().replace('K64-GUI-SKIP\r\nK64-DESKTOP-SKIP',
                                'K64-DESKTOP-SKIP\r\nK64-GUI-SKIP'))
    neg('skip serial with a GUI-OK mixed in', mode='skip', c=counters_skip,
        d=DEBUG_SKIP, s=serial_skip() + 'K64-GUI-OK\r\n')
    neg('skip desktop_test_stage drifted to 5', mode='skip', d=DEBUG_SKIP,
        c=dict(counters_skip, desktop_test_stage=5))
    neg('skip gui_frame_stage drifted to 1', mode='skip', d=DEBUG_SKIP,
        c=dict(counters_skip, desktop_gui_frame_stage=1))
    neg('skip input kbd drifted to 6', mode='skip', d=DEBUG_SKIP,
        c=dict(counters_skip, input_test_kbd_count=6))
    neg('skip vectors at the ok derivation', mode='skip', c=counters_skip,
        d=DEBUG_OK)

    for name, s, d, c, mode, p, pe, pg in negatives:
        ok, lines = rq.judge(s, d, c, False, gui_mode=mode, ppm=p,
                             ppm_echo=pe, ppm_gui=pg)
        check('negative fixture caught: %s' % name, not ok)
        if ok:
            print('  (judge wrongly passed: %s)' % name)

    print('check-gui-judge: %d checks, %d failed' % (CHECKS, FAILED))
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
