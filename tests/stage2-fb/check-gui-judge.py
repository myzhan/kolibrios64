#!/usr/bin/env python3
"""GUI-G1 integration judge fixtures (task 78, decision 5), extended
by task 89 for the IRQ-v2 INPUT marker/counters, by task 96 for the
DESKTOP-v1 marker/counters/two desktop frames/vector derivations, and
by task 102 for the third capture: the GUI-G1 pattern frame
(screen-gui.ppm) taken inside the bounded foundation handshake window
before desktop_init repaints it, and by task 108 for the LSB-first
font bit order: the frame models decode bit0 as the leftmost pixel
(the charUni.mt truth) and mirrored MSB-first frames must FAIL, and
by task 124 for DESKTOP-v2: the desktop frame model derives its base
from the T1 wallpaper asset itself (never from the renderer), the
frozen plain v1 model survives only as the app teardown reference,
and the three menu frames (--menu) get their own models, positive
baseline and negative fixtures, and by task 125 for DESKTOP-v2 T4+T5:
the app teardown repaint delegates to desktop_repaint_base so the
--app final frame is the FULL wallpaper desktop (the plain v1 model
survives only as a negative reference), the menu entry REALLY launches
(the flag is consumed, runs/exits accounted), and the --five leg gets
five frame models (menu/game/exit/game2/exit2), a positive baseline
with a synthetic chip8 reference rectangle and negative fixtures for
the redraw order swap, the exit residue and the relaunch leak.

Proves the extended tools/run-qemu-debug.py judge() catches integration
defects, in the same negative-fixture spirit as tests/stage2-fb/check.py:
a synthetic PASS baseline (serial + counters + THREE PPMs rendered from
the frozen task-77 pattern spec and the frozen desktop frame spec) must
pass, and every mutation below must FAIL. Nothing here executes target
code; the judge is a pure function.

Run: python3 tests/stage2-fb/check-gui-judge.py
Add --five-art-dir DIR to compare both QEMU game captures against the
full desktop and CHIP-8 models. Run tests/stage2-app/check.py first.
"""

import argparse
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


def stamp_sprite(ppm, x0, y0):
    """task 143 (DEFECT-1) negative: the frozen 12x18 sprite drawn into
    a captured PPM box -- the exact residue a captured-cursor save
    buffer writes back as background (transparent codes keep the pixel).
    """
    bad = bytearray(ppm)
    for r in range(dt.SPRITE_H):
        for c in range(dt.SPRITE_W):
            v = dt.SPRITE[r * dt.SPRITE_W + c]
            if v == 0:
                continue
            rgb = 0x000000 if v == 1 else 0xFFFFFF
            off = len(PPM_HEADER) + ((y0 + r) * 1024 + (x0 + c)) * 3
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


_WP_CACHE = None


def wallpaper_rows():
    """DESKTOP-v2 truth source: the T1 asset itself, decoded BGRX -> RGB
    independently of the kernel blit implementation (task 124)."""
    global _WP_CACHE
    if _WP_CACHE is None:
        raw = (REPO / 'kernel' / 'desktop' / 'assets'
               / 'wallpaper.bgrx').read_bytes()
        assert len(raw) == dt.SCREEN_W * dt.SCREEN_H * 4, 'wallpaper size'
        rows = []
        for y in range(dt.SCREEN_H):
            base = y * dt.SCREEN_W * 4
            row = []
            for x in range(dt.SCREEN_W):
                b, g, r, _ = raw[base + x * 4:base + x * 4 + 4]
                row.append((r << 16) | (g << 8) | b)
            rows.append(row)
        _WP_CACHE = rows
    return _WP_CACHE


def place_cursor(rows, cursor_xy):
    cx, cy = cursor_xy
    for r in range(dt.SPRITE_H):
        for c in range(dt.SPRITE_W):
            v = dt.SPRITE[r * dt.SPRITE_W + c]
            if v == 1:
                rows[cy + r][cx + c] = dt.CURSOR_BLACK
            elif v == 2:
                rows[cy + r][cx + c] = dt.CURSOR_WHITE


def desktop_frame(font, cursor_xy, echo_text=b'hi', menu_open=False):
    """The DESKTOP-v2 frame model: the wallpaper asset as the base
    truth, the taskbar fill, the start button accents (underline
    (0,31,64,1), separator (64,24,1,8)), the title, the echoed keys,
    the optional menu rectangle (1px accent border, inner panel, the
    CHIP-8 label) and the cursor sprite composited LAST (the frozen
    redraw order). Derived from the geometry contract, never from the
    rendering implementation."""
    rows = [row[:] for row in wallpaper_rows()]
    for y in range(dt.BAR_Y, dt.BAR_Y + dt.BAR_H):
        for x in range(dt.BAR_X, dt.BAR_X + dt.BAR_W):
            rows[y][x] = dt.BAR_COLOR
    for x in range(dt.START_X, dt.START_X + dt.START_W):
        rows[dt.BAR_H - 1][x] = dt.ACCENT_COLOR
    for y in range(dt.TITLE_Y + fb.FONT_HEIGHT, dt.BAR_H):
        rows[y][dt.START_W] = dt.ACCENT_COLOR
    draw_text(rows, font, dt.TITLE_TEXT[:-1], dt.TITLE_X, dt.TITLE_Y,
              dt.TITLE_COLOR)
    draw_text(rows, font, echo_text, dt.ECHO_X, dt.ECHO_Y, dt.ECHO_COLOR)
    if menu_open:
        for y in range(dt.MENU_Y, dt.MENU_Y + dt.MENU_H):
            for x in range(dt.MENU_X, dt.MENU_X + dt.MENU_W):
                border = (y in (dt.MENU_Y, dt.MENU_Y + dt.MENU_H - 1)
                          or x in (dt.MENU_X, dt.MENU_X + dt.MENU_W - 1))
                rows[y][x] = dt.ACCENT_COLOR if border else dt.MENU_COLOR
        draw_text(rows, font, dt.MENU_TEXT[:-1], dt.MENU_TEXT_X,
                  dt.MENU_TEXT_Y, 0xFFFFFF)
    if cursor_xy is not None:
        place_cursor(rows, cursor_xy)
    return rows


def five_game_rows(font, frame):
    """Composite the model's BGRX game rectangle over the cursor-free desktop."""
    if len(frame) != rq.APP_FB_W * rq.APP_FB_H * 4:
        raise ValueError('incorrect CHIP-8 model frame size')
    rows = desktop_frame(font, None)
    for row in range(rq.APP_FB_H):
        for col in range(rq.APP_FB_W):
            off = (row * rq.APP_FB_W + col) * 4
            b, g, r, _ = frame[off:off + 4]
            rows[rq.APP_FB_Y + row][rq.APP_FB_X + col] = \
                (r << 16) | (g << 8) | b
    for y in range(dt.RUN_Y, dt.RUN_Y + dt.RUN_H):
        for x in range(dt.RUN_X, dt.RUN_X + dt.RUN_W):
            rows[y][x] = dt.RUN_COLOR
    return rows


def require_frame_match(capture, model):
    """Compare decoded RGB, reporting the first mismatching pixel."""
    w, h, actual = rq.parse_ppm(capture)
    mw, mh, expected = rq.parse_ppm(model)
    if (w, h) != (mw, mh):
        raise ValueError(f'capture size {w}x{h}, expected {mw}x{mh}')
    if actual == expected:
        return
    for off in range(0, len(expected), 3):
        got, want = actual[off:off + 3], expected[off:off + 3]
        if got != want:
            y, x = divmod(off // 3, w)
            raise ValueError(
                f'pixel ({x},{y}) is #{got.hex()}, expected #{want.hex()}')


def check_five_captures(art_dir):
    try:
        frame = (REPO / 'build' / 'user-chip8' /
                 'ibm-expected-frame.bin').read_bytes()
        model = render_ppm(five_game_rows(font_bytes(), frame))
    except (OSError, ValueError) as exc:
        print(f'FAIL: five-game model: {exc}')
        return 1

    failed = False
    for name in ('screen-five-game.ppm', 'screen-five-game2.ppm'):
        path = art_dir / name
        try:
            require_frame_match(path.read_bytes(), model)
        except (OSError, ValueError) as exc:
            print(f'FAIL: {path}: {exc}')
            failed = True
        else:
            print(f'PASS: {path}: {dt.SCREEN_W * dt.SCREEN_H} RGB pixels match')
    return int(failed)


def app_flat_frame(font, cursor_xy, echo_text=b'hi'):
    """The frozen v1 plain model: app_redraw_desktop (kernel/app, zero
    change in task 124) repaints the solid background without the
    wallpaper or the accents; the --app final frame reference. ONE
    exception derived from the frozen teardown: the echo first line is
    blitted back VERBATIM from app_echo_save, captured from the
    wallpaper desktop before the launch, so inside the echo save
    rectangle (the desktop echo first line geometry) the wallpaper
    truth survives the plain repaint."""
    rows = [[dt.BG_COLOR] * dt.SCREEN_W for _ in range(dt.SCREEN_H)]
    wp = wallpaper_rows()
    ex, ey = dt.ECHO_X, dt.ECHO_Y
    ew, eh = dt.ECHO_COLS * dt.FONT_W, dt.FONT_H
    for y in range(ey, ey + eh):
        for x in range(ex, ex + ew):
            rows[y][x] = wp[y][x]
    for y in range(dt.BAR_Y, dt.BAR_Y + dt.BAR_H):
        for x in range(dt.BAR_X, dt.BAR_X + dt.BAR_W):
            rows[y][x] = dt.BAR_COLOR
    draw_text(rows, font, dt.TITLE_TEXT[:-1], dt.TITLE_X, dt.TITLE_Y,
              dt.TITLE_COLOR)
    draw_text(rows, font, echo_text, dt.ECHO_X, dt.ECHO_Y, dt.ECHO_COLOR)
    place_cursor(rows, cursor_xy)
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

    # DESKTOP-v2: the menu drive path derives from the frozen injection
    # script itself (accumulate the monitor deltas from the self test
    # exit, record every press position), the start button centre from
    # the geometry contract
    mx, my = dt.ST_CURSOR_X, dt.ST_CURSOR_Y
    menu_press_xy = []
    for cmd, _gate, _gates, _cap in rq.MENU_INJECT_SCRIPT:
        if cmd.startswith('mouse_move'):
            dx, dy = map(int, cmd.split()[1:])
            mx += dx
            my += dy
        elif cmd == 'mouse_button 1':
            menu_press_xy.append((mx, my))
    start_cx = dt.START_X + dt.START_W // 2
    start_cy = dt.START_Y + dt.START_H // 2

    def is_start(x, y):
        return x < dt.START_W and y < dt.START_H

    def is_entry(x, y):
        return (dt.MENU_X <= x < dt.MENU_X + dt.MENU_W
                and dt.MENU_Y <= y < dt.MENU_Y + dt.MENU_H)

    start_presses = sum(1 for x, y in menu_press_xy if is_start(x, y))
    entry_presses = sum(1 for x, y in menu_press_xy if is_entry(x, y))
    # the stage derivation walks the press sequence: a start press
    # publishes START_CLICK (6), an entry press publishes ENTRY_CLICK
    # then the launch pump APP_RUNNING/APP_EXIT (9), a blank press
    # publishes nothing; the LAST menu press is the second start press
    menu_stage = rq.EXPECTED_COUNTERS['desktop_test_stage']
    for x, y in menu_press_xy:
        if is_start(x, y):
            menu_stage = 6
        elif is_entry(x, y):
            menu_stage = 9
    # task 125: the --app final frame is the FULL wallpaper desktop,
    # the teardown redraw delegates to desktop_repaint_base
    app_final_rows = desktop_frame(font, (dt.ST_CURSOR_X, dt.ST_CURSOR_Y))
    menu_open_rows = desktop_frame(font, (start_cx, start_cy),
                                   menu_open=True)
    menu_launch_rows = desktop_frame(font, (dt.ENTRY_CX, dt.ENTRY_CY))
    menu_close_rows = desktop_frame(font, (mx, my))

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
    # ---- DESKTOP-v2 structural pins and model truths (task 124) ----
    check('27 app common probes', len(rq.PPM_APP_COMMON_PROBES) == 27)
    check('7 app final probes', len(rq.PPM_APP_FINAL_PROBES) == 7)
    check('the app probe tables are the identity of the desktop tables '
          '(task 125 delegation)',
          rq.PPM_APP_COMMON_PROBES == rq.PPM_COMMON_PROBES
          and rq.PPM_APP_FINAL_PROBES == rq.PPM_FINAL_PROBES)
    check('the app exit model satisfies every app common probe',
          all(app_final_rows[y][x] == want for x, y, want
              in rq.PPM_APP_COMMON_PROBES))
    check('the app exit model satisfies every app final probe',
          all(app_final_rows[y][x] == want for x, y, want
              in rq.PPM_APP_FINAL_PROBES))
    check('the app exit probes are wallpaper truth and the model '
          'satisfies them',
          all(want == rq.WALLPAPER_BG for _x, _y, want
              in rq.APP_EXIT_PROBES)
          and all(app_final_rows[y][x] == want for x, y, want
                  in rq.APP_EXIT_PROBES))
    check('the menu open model satisfies every open probe',
          all(menu_open_rows[y][x] == want for x, y, want
              in rq.PPM_MENU_OPEN_PROBES))
    check('the menu launch model satisfies every launch probe',
          all(menu_launch_rows[y][x] == want for x, y, want
              in rq.PPM_MENU_LAUNCH_PROBES))
    check('the menu close model satisfies every close probe',
          all(menu_close_rows[y][x] == want for x, y, want
              in rq.PPM_MENU_CLOSE_PROBES))
    check('menu script path ends at (700,600)', (mx, my) == (700, 600))
    check('app echo save rect derives from the echo first line',
          rq.APP_ECHO_SAVE_RECT == (dt.ECHO_X, dt.ECHO_Y,
                                    dt.ECHO_COLS * dt.FONT_W, dt.FONT_H))
    check('menu script 21 steps, packet gates 7..27',
          len(rq.MENU_INJECT_SCRIPT) == 21
          and [g for _c, g, _gs, _cap in rq.MENU_INJECT_SCRIPT]
          == list(range(7, 28)))
    check('menu counters derive mouse 27 / cursor (700,600)',
          rq.EXPECTED_COUNTERS_MENU['input_test_mouse_count'] == 27
          and rq.EXPECTED_COUNTERS_MENU['desktop_test_cursor_x'] == 700
          and rq.EXPECTED_COUNTERS_MENU['desktop_test_cursor_y'] == 600)
    # clicks DERIVE from the whole chain: the guest selftest freezes 1
    # press at (557,411), then every 'mouse_button 1' in the menu script
    # is one more press edge (4) -> 5; task 125: the launch pump CONSUMED
    # the flag (0); runs counts EVERY attempt in BOTH modes -- the boot
    # attempt (ENOENT on the plain image) plus the entry click -> 2,
    # exits 0; the start/entry presses derive from the recorded press
    # positions
    menu_presses = sum(1 for c, _g, _gs, _cap in rq.MENU_INJECT_SCRIPT
                       if c == 'mouse_button 1')
    check('menu final labels derive from the press positions',
          menu_presses == 4 and start_presses == 2 and entry_presses == 1
          and rq.EXPECTED_MENU_FINAL == {
              'desktop_test_clicks': 1 + menu_presses,
              'desktop_test_menu_opens': 2,
              'desktop_menu_flag': 0,
              'desktop_app_launch_requested': 0,
              'desktop_prev_buttons': 0,
              'desktop_test_start_clicks': start_presses,
              'desktop_test_entry_clicks': entry_presses,
              'desktop_test_app_runs': 1 + entry_presses,
              'desktop_test_app_exits': 0})
    check('menu final stage derives 6 (the last start press)',
          menu_stage == 6
          and rq.EXPECTED_COUNTERS_MENU['desktop_test_stage'] == menu_stage)
    check('menu vectors derived 6/81',
          rq.EXPECTED_VECTORS['menu'] == {'21': 6, '2c': 81})
    check('menu counter symbols cover the nine private labels',
          {n for n, _s in rq.MENU_COUNTER_SYMBOLS}
          == set(rq.EXPECTED_MENU_FINAL) == set(rq.EXPECTED_FIVE_FINAL))
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

    # the DESKTOP-v2 --menu positive baseline: the three menu frames
    # from the independent models, the close frame as the final capture
    counters_menu = dict(rq.EXPECTED_COUNTERS_MENU, **rq.EXPECTED_MENU_FINAL)
    menu_ev = {'open': render_ppm(menu_open_rows),
               'launch': render_ppm(menu_launch_rows),
               'close': render_ppm(menu_close_rows), 'errors': []}
    ok, lines = rq.judge(serial_ok(), debug_log(6, 81), counters_menu, False,
                         ppm=render_ppm(menu_close_rows), ppm_echo=ppm_echo,
                         ppm_gui=ppm_gui, menu=menu_ev)
    check('menu baseline PASS (three menu frames)', ok)
    if not ok:
        print('\n'.join(l for l in lines if 'FAIL' in l))

    # ---- DESKTOP-v2 T5: the --five positive baseline (task 125) ----
    # the five frames from the independent models: the menu frame is
    # the open model at the start centre, the game frame is the desktop
    # with the cursor SUSPENDED (bbox back to the asset truth), the
    # chip8 rectangle from a synthetic reference and the RUN indicator
    # lit; the exit frame is the full wallpaper desktop with the cursor
    # at the entry centre. The synthetic checkerboard reference is NOT
    # the renderer: in production the rectangle truth comes from the
    # chip8 model (ibm-expected-frame.bin), here it only exercises the
    # comparison logic.
    five_cursor = (dt.ENTRY_CX, dt.ENTRY_CY)
    five_exit_rows = desktop_frame(font, five_cursor)

    exp_frame = b''.join(
        bytes((0xFF, 0xFF, 0xFF, 0))
        if ((cx // rq.APP_CELL + cy // rq.APP_CELL) % 2) == 0 else bytes(4)
        for cy in range(rq.APP_FB_H) for cx in range(rq.APP_FB_W))
    five_game = five_game_rows(font, exp_frame)
    ppm_five_game = render_ppm(five_game)
    # Sprite outline at (32,17), inside the start-click box but off the probes.
    one_pixel_residue = set_pixel(ppm_five_game, 32, 17, dt.CURSOR_BLACK)
    try:
        require_frame_match(one_pixel_residue, ppm_five_game)
    except ValueError as exc:
        check('full-frame comparison catches one pixel of cursor residue',
              'pixel (32,17)' in str(exc))
    else:
        check('full-frame comparison catches one pixel of cursor residue', False)
    ppm_five_exit = render_ppm(five_exit_rows)
    app_ev_five = {
        'app_ready': 1, 'app_stage': rq.APP_STAGE_DONE,
        'app_exit_code': 0, 'app_fault_vector': 0,
        'app_image_size': rq.APP_PROGRAM_SIZE,
        'app_table_delta': rq.APP_TABLE_BUDGET,
        'app_table_released': rq.APP_TABLE_BUDGET,
        'app_pages_held': 0, 'app_interactive': 0, 'app_fd_swept': 0,
        'app_echo_saved': 1, 'app_key_count': 0,
        'app_saved_fs_reads': rq.FS_TEST_READS,
        'app_saved_blk_reads': rq.BLK_TEST_READS_FS,
        'fs_test_reads': rq.FS_TEST_READS,
        'blk_test_reads': rq.BLK_TEST_READS_FS,
        'app_blit_count': 1, 'app_poll_count': 3,
        'user_va_unmapped': True, 'pages_zeroed': True,
        'frames_released': True, 'released_is_budget': True,
        'net_table_invariant': True, 'pdpt4_clear': True,
        'anchor0_us_stays': True, 'image_sha256': rq.APP_IMAGE_SHA256,
        'game_probes': (), 'expected_frame': exp_frame, 'errors': []}
    serial_five = serial_ok().replace(
        'K64-FOUNDATION-OK',
        'K64-FSPROBE-OK\r\nK64-APP-OK\r\nK64-FOUNDATION-OK')
    counters_five = dict(rq.EXPECTED_COUNTERS_FIVE, **rq.EXPECTED_FIVE_FINAL)
    five_ev = {'menu': render_ppm(menu_open_rows),
               'game': ppm_five_game, 'exit': ppm_five_exit,
               'game2': ppm_five_game, 'exit2': ppm_five_exit,
               'errors': []}
    # structural pins of the five extension
    check('five counters derive mouse 22 / cursor (100,49) / stage 9',
          rq.EXPECTED_COUNTERS_FIVE['input_test_mouse_count'] == 22
          and rq.EXPECTED_COUNTERS_FIVE['desktop_test_cursor_x'] == 100
          and rq.EXPECTED_COUNTERS_FIVE['desktop_test_cursor_y'] == 49
          and rq.EXPECTED_COUNTERS_FIVE['desktop_test_stage'] == 9)
    check('five final labels runs 3 / exits 3 / start 2 / entry 2',
          rq.EXPECTED_FIVE_FINAL['desktop_test_app_runs'] == 3
          and rq.EXPECTED_FIVE_FINAL['desktop_test_app_exits'] == 3
          and rq.EXPECTED_FIVE_FINAL['desktop_test_start_clicks'] == 2
          and rq.EXPECTED_FIVE_FINAL['desktop_test_entry_clicks'] == 2
          and rq.EXPECTED_FIVE_FINAL['desktop_test_clicks'] == 5
          and rq.EXPECTED_FIVE_FINAL['desktop_app_launch_requested'] == 0)
    check('five vectors derived 6/66',
          rq.EXPECTED_VECTORS['five'] == {'21': 6, '2c': 66})
    check('the five exit model satisfies every five exit probe',
          all(five_exit_rows[y][x] == want for x, y, want
              in rq.PPM_COMMON_PROBES + rq.PPM_FIVE_EXIT_PROBES))
    check('the five game model satisfies every run-lit probe',
          all(five_game[y][x] == want for x, y, want
              in rq.PPM_RUN_LIT_PROBES))
    # task 143 (DEFECT-1): the independent game model is sprite-free in
    # the start-click box, and the runner pins the same truth probes
    check('the five game model satisfies every residue-box probe',
          all(five_game[y][x] == want for x, y, want
              in rq.PPM_RESIDUE_BOX_PROBES))
    check('the menu launch/close models satisfy the residue-box probes',
          all(menu_launch_rows[y][x] == want for x, y, want
              in rq.PPM_RESIDUE_BOX_PROBES)
          and all(menu_close_rows[y][x] == want for x, y, want
                  in rq.PPM_RESIDUE_BOX_PROBES))
    check('residue box geometry is the frozen 12x18 start-click footprint',
          (rq.RESIDUE_BOX_X, rq.RESIDUE_BOX_Y, rq.RESIDUE_BOX_W,
           rq.RESIDUE_BOX_H) == (32, 16, 12, 18)
          and len(rq.PPM_RESIDUE_BOX_PROBES) == 13
          and len(rq.residue_box(ppm_five_game)) == 216
          and rq.residue_box(ppm_five_game)
          == rq.residue_box(ppm_five_exit))
    ok, lines = rq.judge(serial_five, debug_log(6, 66), counters_five,
                         False, ppm=ppm_five_exit, ppm_echo=ppm_echo,
                         ppm_gui=ppm_gui, app=app_ev_five,
                         ppm_game=ppm_five_game, five=five_ev)
    check('five baseline PASS (five frames + relaunch receipts)', ok)
    if not ok:
        print('\n'.join(l for l in lines if 'FAIL' in l))

    # negative fixtures: every one must FAIL
    negatives = []

    def neg(name, s=None, d=None, c=None, mode='ok',
            p=KEEP, pe=KEEP, pg=KEEP, m=None, a=None, fv=None, pgm=None):
        negatives.append((name,
                          serial_ok() if s is None else s,
                          DEBUG_OK if d is None else d,
                          counters if c is None else c,
                          mode,
                          ppm_final if p is KEEP else p,
                          ppm_echo if pe is KEEP else pe,
                          ppm_gui if pg is KEEP else pg,
                          m, a, fv, pgm))

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

    # ---- DESKTOP-v2: wallpaper truth drift (task 124) ----------------
    neg('echo frame rendered on the v1 plain background',
        pe=render_ppm(app_flat_frame(font, (dt.CURSOR_X0, dt.CURSOR_Y0))))
    neg('final frame rendered on the v1 plain background',
        p=render_ppm(app_flat_frame(font, (dt.ST_CURSOR_X, dt.ST_CURSOR_Y))))
    neg('final gray-band pixel flattened to the base',
        p=set_pixel(ppm_final, 568, 411, rq.WALLPAPER_BG))
    neg('echo wallpaper pixel corrupted',
        pe=set_pixel(ppm_echo, 900, 700, dt.BG_COLOR))

    # ---- DESKTOP-v2: the menu frames and labels (task 124) -----------
    def menu_neg(name, ev=None, c=None, d=None, p=KEEP):
        neg(name, c=dict(counters_menu) if c is None else c,
            d=debug_log(6, 81) if d is None else d,
            p=render_ppm(menu_close_rows) if p is KEEP else p,
            m=dict(menu_ev) if ev is None else ev)

    menu_neg('menu open frame missing', ev=dict(menu_ev, open=None))
    menu_neg('menu launch frame missing', ev=dict(menu_ev, launch=None))
    menu_neg('menu close frame missing', ev=dict(menu_ev, close=None))
    menu_neg('menu border pixel corrupted',
             ev=dict(menu_ev, open=set_pixel(menu_ev['open'], 0, 32,
                                             dt.MENU_COLOR)))
    menu_neg('menu inner panel pixel corrupted',
             ev=dict(menu_ev, open=set_pixel(menu_ev['open'], 1, 33,
                                             dt.ACCENT_COLOR)))
    menu_neg('menu glyph mirrored (MSB-first decode)',
             ev=dict(menu_ev, open=render_ppm(
                 desktop_frame(mirrored, (start_cx, start_cy),
                               menu_open=True))))
    menu_neg('menu launch frame not restored (still open)',
             ev=dict(menu_ev, launch=menu_ev['open']))
    menu_neg('menu close frame not restored (still open)',
             ev=dict(menu_ev, close=menu_ev['open']))
    menu_neg('menu close cursor pixel corrupted',
             ev=dict(menu_ev, close=set_pixel(menu_ev['close'], 700, 600,
                                              dt.BG_COLOR)))
    # A single sprite pixel shared by all three captures used to satisfy
    # both the sampled probes and launch-to-final box equality.
    menu_launch_residue = set_pixel(menu_ev['launch'], 32, 17, dt.CURSOR_BLACK)
    menu_close_residue = set_pixel(menu_ev['close'], 32, 17, dt.CURSOR_BLACK)
    menu_neg('menu launch and close share one off-probe cursor pixel',
             ev=dict(menu_ev, launch=menu_launch_residue, close=menu_close_residue),
             p=menu_close_residue)
    menu_neg('menu leg error recorded',
             ev=dict(menu_ev, errors=['step 3 gate never reached']))
    for name, value in (('desktop_test_clicks', 3),
                        ('desktop_test_menu_opens', 1),
                        ('desktop_menu_flag', 1),
                        ('desktop_app_launch_requested', 1),
                        ('desktop_prev_buttons', 1),
                        ('desktop_test_start_clicks', 1),
                        ('desktop_test_entry_clicks', 0),
                        ('desktop_test_app_runs', 0),
                        ('desktop_test_app_runs', 1),
                        ('desktop_test_app_exits', 1)):
        menu_neg('menu %s drifted to %d' % (name, value),
                 c=dict(counters_menu, **{name: value}))
    menu_neg('menu mouse_count stayed 26',
             c=dict(counters_menu, input_test_mouse_count=26))
    menu_neg('menu v=2c deliveries 78 (need 81)', d=debug_log(6, 78))
    menu_neg('menu v=2c deliveries 84 (need 81)', d=debug_log(6, 84))
    menu_neg('menu final frame still at (557,411)', p=ppm_final)
    menu_neg('menu final stage stayed 5 (no START_CLICK publication)',
             c=dict(counters_menu, desktop_test_stage=5))
    # task 143 (DEFECT-1): the captured-cursor residue in the start box
    menu_neg('menu launch cursor residue at the start box',
             ev=dict(menu_ev,
                     launch=stamp_sprite(menu_ev['launch'], 32, 16)))
    menu_neg('menu close cursor residue at the start box',
             ev=dict(menu_ev,
                     close=stamp_sprite(menu_ev['close'], 32, 16)))

    # ---- DESKTOP-v2 T5: the five frames and labels (task 125) -------
    def five_neg(name, ev=None, c=None, d=None, p=KEEP, a=None, pgm=KEEP):
        neg(name, s=serial_five,
            c=dict(counters_five) if c is None else c,
            d=debug_log(6, 66) if d is None else d,
            p=ppm_five_exit if p is KEEP else p,
            a=dict(app_ev_five) if a is None else a,
            pgm=ppm_five_game if pgm is KEEP else pgm,
            fv=dict(five_ev) if ev is None else ev)

    # the THREE mandatory FAILs of the task book
    five_exit_nocursor = render_ppm(desktop_frame(font, None))
    five_neg('five redraw order swapped (base repaint erased the cursor)',
             ev=dict(five_ev, exit=five_exit_nocursor,
                     exit2=five_exit_nocursor))
    five_neg('five exit game residue after the teardown',
             ev=dict(five_ev, exit=set_pixel(
                 ppm_five_exit, rq.APP_FB_X + 5, rq.APP_FB_Y + 5,
                 0xFFFFFF)))
    five_neg('five relaunch leak (exit2 differs from exit)',
             ev=dict(five_ev, exit2=set_pixel(ppm_five_exit, 500, 400,
                                              0xFFFFFF)))
    # frame presence and pixel integrity
    five_neg('five menu frame missing', ev=dict(five_ev, menu=None))
    five_neg('five game frame missing', ev=dict(five_ev, game=None))
    five_neg('five game2 frame missing', ev=dict(five_ev, game2=None))
    five_neg('five exit frame missing', ev=dict(five_ev, exit=None))
    five_neg('five exit2 frame missing', ev=dict(five_ev, exit2=None))
    five_neg('five exit indicator not erased',
             ev=dict(five_ev, exit=set_pixel(ppm_five_exit, 1000, 12,
                                             rq.WALLPAPER_BAND)))
    five_neg('five game indicator not lit',
             ev=dict(five_ev, game=set_pixel(ppm_five_game, 1000, 12,
                                             rq.DESKTOP_BAR)))
    five_neg('five game2 rectangle black (one-shot blit gate)',
             ev=dict(five_ev, game2=render_ppm(
                 five_game_rows(font, bytes(rq.APP_FB_W * rq.APP_FB_H * 4)))))
    five_neg('five game2 pixel shifted (relaunch drawing leak)',
             ev=dict(five_ev, game2=set_pixel(
                 ppm_five_game, rq.APP_FB_X + 15, rq.APP_FB_Y + 5,
                 0xFFFFFF)))
    five_neg('five exit cursor pixel corrupted',
             ev=dict(five_ev, exit=set_pixel(ppm_five_exit, 100, 49,
                                             rq.WALLPAPER_BG)))
    five_neg('five leg error recorded',
             ev=dict(five_ev, errors=['round2 IBM_READY never published']))
    five_neg('five final frame still the menu frame',
             p=render_ppm(menu_open_rows))
    # the relaunch accounting and the app receipts
    for name, value in (('desktop_test_app_runs', 2),
                        ('desktop_test_app_exits', 2),
                        ('desktop_test_entry_clicks', 1),
                        ('desktop_test_start_clicks', 1),
                        ('desktop_test_clicks', 4),
                        ('desktop_test_menu_opens', 1),
                        ('desktop_menu_flag', 1),
                        ('desktop_app_launch_requested', 1),
                        ('desktop_prev_buttons', 1)):
        five_neg('five %s drifted to %d' % (name, value),
                 c=dict(counters_five, **{name: value}))
    five_neg('five stage stayed 8 (APP_EXIT never published)',
             c=dict(counters_five, desktop_test_stage=8))
    five_neg('five mouse_count stayed 21',
             c=dict(counters_five, input_test_mouse_count=21))
    five_neg('five cursor_x stayed 32',
             c=dict(counters_five, desktop_test_cursor_x=32))
    five_neg('five v=2c deliveries 63 (need 66)', d=debug_log(6, 63))
    five_neg('five v=2c deliveries 69 (need 66)', d=debug_log(6, 69))
    five_neg('five app blit_count 0 (relaunch never blitted)',
             a=dict(app_ev_five, app_blit_count=0))
    five_neg('five app stage not DONE',
             a=dict(app_ev_five, app_stage=rq.APP_STAGE_IBM_READY))
    five_neg('five app pages_held 1 (teardown leak)',
             a=dict(app_ev_five, app_pages_held=1))
    # task 143 (DEFECT-1): the captured-cursor residue in the start box
    # of a game frame -- the defect sequence the 125 gate missed
    five_neg('five game cursor residue at the start box',
             ev=dict(five_ev,
                     game=stamp_sprite(ppm_five_game, 32, 16)))
    five_neg('five game2 cursor residue at the start box',
             ev=dict(five_ev,
                     game2=stamp_sprite(ppm_five_game, 32, 16)))

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

    for name, s, d, c, mode, p, pe, pg, m, a, fv, pgm in negatives:
        ok, lines = rq.judge(s, d, c, False, gui_mode=mode, ppm=p,
                             ppm_echo=pe, ppm_gui=pg, menu=m,
                             app=a, five=fv, ppm_game=pgm)
        check('negative fixture caught: %s' % name, not ok)
        if ok:
            print('  (judge wrongly passed: %s)' % name)

    print('check-gui-judge: %d checks, %d failed' % (CHECKS, FAILED))
    return 1 if FAILED else 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--five-art-dir', type=Path,
                        help='QEMU --five artifact directory')
    args = parser.parse_args()
    sys.exit(check_five_captures(args.five_art_dir)
             if args.five_art_dir is not None else main())
