#!/usr/bin/env python3
"""Drive a fresh --desktop QEMU session through its emulated PS/2 devices."""

import argparse
import re
import runpy
import socket
import struct
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
runner = runpy.run_path(str(REPO / 'tools' / 'run-qemu-debug.py'))
capture = runner['screendump']
monitor = runner['monitor_command']
parse_ppm = runner['parse_ppm']


def crop(frame, x, y, width, height):
    stride, _, rgb = parse_ppm(frame)
    return b''.join(rgb[(row * stride + x) * 3:(row * stride + x + width) * 3]
                    for row in range(y, y + height))


def logical_capture(art, name):
    width, height, rgb = parse_ppm(capture(art, name=name))
    if (width, height) != (2048, 1536):
        raise ValueError(f'{name}: expected 2048x1536, got {width}x{height}')
    stride = width * 3
    logical = bytearray(1024 * 768 * 3)
    for y in range(768):
        start = y * stride * 2
        top = rgb[start:start + stride]
        bottom = rgb[start + stride:start + 2 * stride]
        for channel in range(3):
            left = top[channel::6]
            if top != bottom or left != top[channel + 3::6]:
                raise ValueError(f'{name}: non-uniform 2x2 pixels on row {y}')
            offset = y * 1024 * 3 + channel
            logical[offset:offset + 1024 * 3:3] = left
    return b'P6\n1024 768\n255\n' + logical


def glyph_line(text, fg, bg):
    font_source = (REPO / 'kernel/video/font.inc').read_text()
    font = bytes(int(value, 16) for line in font_source.splitlines()
                 if line.lstrip().startswith('db ')
                 for value in re.findall(r'0x([0-9A-Fa-f]{2})', line.split(';', 1)[0]))
    return b''.join(fg if font[(ch - 32) * 16 + y] & (1 << x) else bg
                    for y in range(16) for ch in text.encode('ascii') for x in range(8))


def check_resize(art, socket_path, logical_frame):
    _, _, source = parse_ppm(logical_frame)

    def read_exact(stream, count):
        value = stream.read(count)
        if len(value) != count:
            raise ValueError('short RFB handshake')
        return value

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(socket_path))
        with client.makefile('rb') as stream:
            version = read_exact(stream, 12)
            if version != b'RFB 003.008\n':
                raise ValueError(f'unexpected RFB version {version!r}')
            client.sendall(version)
            security_types = read_exact(stream, read_exact(stream, 1)[0])
            if 1 not in security_types:
                raise ValueError('the private QEMU test socket did not offer NoAuth')
            client.sendall(b'\x01')
            if read_exact(stream, 4) != bytes(4):
                raise ValueError('RFB security handshake failed')
            client.sendall(b'\x01')     # shared access
            header = read_exact(stream, 24)
            read_exact(stream, int.from_bytes(header[20:24], 'big'))
            client.sendall(struct.pack('!BBHi', 2, 0, 1, -308))  # ExtendedDesktopSize

        sizes = ((1280, 800), (901, 677), (1600, 900), (4096, 3072), (2048, 1536))
        for width, height in sizes:
            client.sendall(struct.pack('!BBHHBBIHHHHI',
                                       251, 0, width, height, 1, 0,
                                       0, 0, 0, width, height, 0))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                frame = capture(art, name=f'runtime-resize-{width}x{height}.ppm')
                actual_width, actual_height, actual = parse_ppm(frame, qemu_row_padding=True)
                if (actual_width, actual_height) == (width, height):
                    break
                time.sleep(0.12)
            else:
                raise ValueError(f'requested {width}x{height}, got {actual_width}x{actual_height}')

            fit_w = min(width, height * 1024 // 768)
            fit_h = min(height, width * 768 // 1024)
            left = (width - fit_w) // 2
            top = (height - fit_h) // 2
            columns = [x * 1024 // fit_w * 3 for x in range(fit_w)]
            for y in range(height):
                expected = bytes(width * 3)
                if top <= y < top + fit_h:
                    row = (y - top) * 768 // fit_h * 1024 * 3
                    pixels = b''.join(source[row + x:row + x + 3] for x in columns)
                    expected = bytes(left * 3) + pixels + bytes((width - left - fit_w) * 3)
                if actual[y * width * 3:(y + 1) * width * 3] != expected:
                    raise ValueError(f'{width}x{height}: altered or stale pixels on row {y}')
            print(f'PASS: QEMU resized the guest to {width}x{height}, all pixels match')


def check_desktop(art, vnc_socket=None):
    serial_path = art / 'serial.log'
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if serial_path.exists() and 'K64-FOUNDATION-OK' in serial_path.read_text():
            break
        time.sleep(0.15)
    else:
        raise ValueError('normal boot did not reach K64-FOUNDATION-OK')
    print('PASS: normal boot without debugger or input injection')

    def send(command, delay=0.10):
        reply = monitor(art, command)
        if 'unknown command:' in reply.lower() or 'invalid parameter' in reply.lower():
            raise ValueError(f'QEMU rejected {command}')
        time.sleep(delay)

    def wait_frame(name, predicate):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            frame = logical_capture(art, name)
            if predicate(frame):
                return frame
            time.sleep(0.15)
        raise ValueError(f'expected pixels did not appear in {name}')

    mouse_x, mouse_y = 517, 387

    def move(x, y):
        nonlocal mouse_x, mouse_y
        while (mouse_x, mouse_y) != (x, y):
            dx = min(127, max(-127, x - mouse_x))
            dy = min(127, max(-127, y - mouse_y))
            send(f'mouse_move {dx} {dy}')
            mouse_x += dx
            mouse_y += dy

    def click(x, y, delay=0.25):
        move(x, y)
        send('mouse_button 1', delay=delay)
        send('mouse_button 0', delay=delay)

    def double_click(x, y):
        click(x, y, 0.10)
        click(x, y, 0.10)

    def drag(x, y, to_x, to_y):
        move(x, y)
        send('mouse_button 1', delay=0.20)
        move(to_x, to_y)
        send('mouse_button 0', delay=0.20)

    def launch_app():
        click(32, 16)
        click(100, 49)

    initial = logical_capture(art, 'runtime-initial.ppm')
    print('PASS: initial 2048x1536 output with exact 2x2 logical pixels')
    send('sendkey ret 10')
    for _ in range(70):
        send('sendkey a 10', delay=0.05)
    last_cell = (16 + 69 * 8, 80, 8, 16)
    wait_frame('runtime-queue.ppm', lambda frame:
               crop(frame, *last_cell) != crop(initial, *last_cell))
    print('PASS: 140 keyboard events reach the second echo row')

    # Put text inside the region that CHIP-8 will paint over.
    for _ in range(9):
        send('sendkey ret 10')
    for _ in range(26):
        send('sendkey w 10', delay=0.05)
    game_rect = tuple(runner[key] for key in
                      ('APP_FB_X', 'APP_FB_Y', 'APP_FB_W', 'APP_FB_H'))
    game_echo = (*game_rect[:3], 16)
    echo = wait_frame('runtime-echo.ppm', lambda frame:
                      crop(frame, *game_echo) != crop(initial, *game_echo))

    launch_app()

    app_running = (1000, 12, 1, 1)
    run_pixel = runner['PPM_RUN_LIT_PROBES'][0][2].to_bytes(3, 'big')
    ibm_colors = b'\xad\xe8\xf4'
    ibm = wait_frame('runtime-ibm.ppm', lambda frame:
                     crop(frame, *app_running) == run_pixel
                     and ibm_colors in crop(frame, *game_rect))
    send('sendkey ret 10')
    wait_frame('runtime-tetris.ppm', lambda frame:
               crop(frame, *app_running) == run_pixel
               and crop(frame, *game_rect) != crop(ibm, *game_rect)
               and runner['app_frame_lit'](frame))
    print('PASS: menu launch after multiline echo and PS/2 Enter into Tetris')

    normal_title = (193, 201, 1, 1)
    max_title = (1, 33, 1, 1)
    title_color = b'\x34\x46\x5a'
    wait_frame('runtime-window.ppm', lambda frame:
               crop(frame, *normal_title) == title_color)
    drag(300, 210, 420, 270)
    moved_title = (313, 261, 1, 1)
    wait_frame('runtime-dragged.ppm', lambda frame:
               crop(frame, *moved_title) == title_color
               and crop(frame, *normal_title) == crop(echo, *normal_title))
    drag(420, 270, 1010, 750)
    wait_frame('runtime-drag-clamped.ppm', lambda frame:
               crop(frame, 385, 425, 1, 1) == title_color)
    drag(500, 434, 428, 270)
    wait_frame('runtime-drag-repositioned.ppm', lambda frame:
               crop(frame, *moved_title) == title_color)
    click(915, 270)
    wait_frame('runtime-maximized.ppm', lambda frame:
               crop(frame, *max_title) == title_color)
    click(960, 42)
    wait_frame('runtime-minimized.ppm', lambda frame:
               crop(frame, *max_title) == crop(initial, *max_title)
               and crop(frame, *game_echo) == crop(echo, *game_echo)
               and crop(frame, *app_running) == run_pixel)
    click(940, 16)
    wait_frame('runtime-restored.ppm', lambda frame:
               crop(frame, *max_title) == title_color)
    click(985, 42)
    wait_frame('runtime-unmaximized.ppm', lambda frame:
               crop(frame, *moved_title) == title_color
               and crop(frame, *normal_title) == crop(echo, *normal_title))
    print('PASS: title drag, screen-edge clamp and restore to the dragged position')

    send('sendkey esc 10')
    wait_frame('runtime-exit.ppm', lambda frame:
               crop(frame, *app_running) == crop(initial, *app_running)
               and crop(frame, *last_cell) == crop(echo, *last_cell)
               and crop(frame, *game_echo) == crop(echo, *game_echo))
    send('sendkey z 10')
    next_cell = (16 + 26 * 8, 224, 8, 16)
    wait_frame('runtime-resumed.ppm', lambda frame:
               crop(frame, *next_cell) != crop(echo, *next_cell))
    print('PASS: PS/2 Esc restores multiline echo and leaves keyboard input live')
    launch_app()
    wait_frame('runtime-relaunched.ppm', lambda frame:
               crop(frame, *normal_title) == title_color)
    click(820, 210)
    wait_frame('runtime-closed.ppm', lambda frame:
               crop(frame, *normal_title) == crop(initial, *normal_title)
               and crop(frame, *app_running) == crop(initial, *app_running))
    print('PASS: title-bar close exits the relaunched app')

    click(32, 16)
    click(100, 81)
    title = glyph_line('Files', b'\xff\xff\xff', title_color)
    wait_frame('runtime-files-root.ppm', lambda frame:
               crop(frame, 200, 204, 40, 16) == title)
    primary, background = b'\xad\xe8\xf4', b'\x18\x25\x32'

    def wait_path(name, path, client_x=264, client_y=264):
        expected = glyph_line(path + ' ', primary, background)
        return wait_frame(name, lambda frame:
                          crop(frame, client_x + 12, client_y + 8,
                               (len(path) + 1) * 8, 16) == expected)

    wait_path('runtime-files-root.ppm', '/', 192, 224)
    drag(328, 210, 400, 250)
    files_title = (265, 241, 1, 1)
    wait_path('runtime-files-moved.ppm', '/')
    click(460, 330)
    selected = glyph_line('PROGRAMS', b'\xe9\xed\xf2', b'\x30\x4b\x65')
    wait_frame('runtime-files-selected.ppm', lambda frame:
               crop(frame, 340, 325, 64, 16) == selected)
    time.sleep(0.65)
    click(460, 330)
    wait_path('runtime-files-slow-clicks.ppm', '/')
    time.sleep(0.65)
    double_click(460, 330)
    wait_path('runtime-files-programs.ppm', '/PROGRAMS')
    send('sendkey backspace 30', delay=0.25)
    click(867, 250)                    # maximize the moved window
    wait_frame('runtime-files-maximized.ppm', lambda frame:
               crop(frame, *max_title) == title_color)
    double_click(160, 228)              # client (100,45) in the scaled frame
    # The header glyphs are scaled too, so look at the logical client
    # after restoring the frame to its dragged position.
    click(985, 42)
    wait_path('runtime-files-roms.ppm', '/ROMS')
    send('sendkey backspace 30', delay=0.25)
    double_click(460, 350)
    wait_path('runtime-files-preview.ppm', '/README.TXT')
    prefix = (REPO / 'README.md').read_bytes()[:16]
    preview = ''.join(chr(ch) if 32 <= ch <= 126 else '.' for ch in prefix)
    expected = glyph_line(preview, b'\xe9\xed\xf2', background)
    wait_frame('runtime-files-preview.ppm', lambda frame:
               crop(frame, 676, 300, len(preview) * 8, 16) == expected)
    click(844, 250)
    wait_frame('runtime-files-minimized.ppm', lambda frame:
               crop(frame, *files_title) == crop(initial, *files_title)
               and crop(frame, *app_running) == run_pixel)
    click(840, 16)
    wait_frame('runtime-files-restored.ppm', lambda frame:
               crop(frame, 676, 300, len(preview) * 8, 16) == expected)
    send('sendkey esc 30', delay=0.25)
    wait_frame('runtime-files-exit.ppm', lambda frame:
               crop(frame, *app_running) == crop(initial, *app_running))
    print('PASS: Files selects by mouse, distinguishes slow clicks, opens in both window modes and restores its preview')

    launch_app()
    exposed_game = (192, 224, 640, 96)
    first = wait_frame('runtime-dual-ibm.ppm', lambda frame:
                       ibm_colors in crop(frame, *exposed_game))
    chip_title = glyph_line('CHIP-8', b'\xff\xff\xff', title_color)
    click(32, 16)
    click(100, 81)
    dual = wait_frame('runtime-dual-open.ppm', lambda frame:
                      crop(frame, 200, 204, 48, 16) == chip_title
                      and crop(frame, 328, 324, 40, 16) == title)
    if vnc_socket:
        check_resize(art, vnc_socket, dual)

    # Enter opens the selected ROM directory in Files. The same key
    # would switch CHIP-8 out of its IBM screen if it leaked.
    send('sendkey ret 30', delay=0.25)
    pair = wait_path('runtime-dual-files-focus.ppm', '/ROMS', 320, 344)
    if crop(pair, *exposed_game) != crop(first, *exposed_game):
        raise ValueError('Files Enter also reached the background CHIP-8')

    click(300, 210)
    overlap = (321, 321, 1, 1)
    wait_frame('runtime-dual-raised.ppm', lambda frame:
               crop(frame, *overlap) == crop(first, *overlap))
    send('sendkey backspace 30', delay=0.25)
    send('sendkey ret 30', delay=0.25)
    wait_frame('runtime-dual-game-focus.ppm', lambda frame:
               crop(frame, *exposed_game) != crop(first, *exposed_game)
               and runner['app_frame_lit'](frame))

    # Grab the exposed right side of Files, raise it, and leave enough
    # of the game visible to observe it advancing in the background.
    drag(858, 330, 538, 42)
    before = wait_path('runtime-dual-background-start.ppm', '/ROMS', 0, 56)
    game_lower = (192, 376, 640, 168)
    wait_frame('runtime-dual-background-step.ppm', lambda frame:
               crop(frame, *game_lower) != crop(before, *game_lower))
    print('PASS: overlapping windows raise independently, keep their state and route Enter only to the focused app')
    print('PASS: Tetris keeps advancing while Files has focus')

    click(580, 42)
    moved_files_title = (8, 36, 40, 16)
    wait_frame('runtime-dual-minimized.ppm', lambda frame:
               crop(frame, *moved_files_title) != title
               and crop(frame, 200, 204, 48, 16) == chip_title)
    click(840, 16)
    wait_path('runtime-dual-restored.ppm', '/ROMS', 0, 56)
    send('sendkey esc 30', delay=0.25)
    wait_frame('runtime-dual-files-closed.ppm', lambda frame:
               crop(frame, *moved_files_title) != title
               and crop(frame, 200, 204, 48, 16) == chip_title
               and crop(frame, *app_running) == run_pixel)
    click(32, 16)
    click(100, 81)
    wait_path('runtime-dual-relaunched.ppm', '/', 320, 344)
    click(820, 210)
    wait_frame('runtime-dual-game-closed.ppm', lambda frame:
               crop(frame, 200, 204, 48, 16) != chip_title
               and crop(frame, 328, 324, 40, 16) == title)
    send('sendkey ret 30', delay=0.25)
    wait_path('runtime-dual-survivor.ppm', '/ROMS', 320, 344)
    send('sendkey esc 30', delay=0.25)
    wait_frame('runtime-dual-last-closed.ppm', lambda frame:
               crop(frame, *app_running) == crop(initial, *app_running))
    print('PASS: per-window minimize, restore, relaunch and close leave the other task running')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('art_dir', type=Path,
                        help='artifact directory of a fresh --desktop session with the default disk')
    parser.add_argument('--vnc-socket', type=Path,
                        help='private RFB socket for guest resize requests')
    args = parser.parse_args()
    try:
        check_desktop(args.art_dir.resolve(), args.vnc_socket)
    except (OSError, ValueError) as exc:
        parser.exit(1, f'FAIL: {exc}\n')
