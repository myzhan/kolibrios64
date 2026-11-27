## KolibriOS 64
Operating system for x86-64 platform being developed in FASM 
### Current roadmap [HERE](https://git.kolibrios.org/KolibriOS/kolibrios64/issues/1)

### Run the desktop on macOS

With the fasm2 checkout beside this repository and QEMU on PATH, run

```sh
python3 tools/run-qemu-debug.py --desktop
```

Drag the Cocoa window edge to change the guest scanout resolution. The
desktop keeps its 4:3 proportions and fills any extra area with black.
Open **Start → CHIP-8** and press **Enter** on the IBM screen to start Tetris.
Use **↑** to rotate, **↓** to drop faster, **← / →** to move and **Esc** to
close CHIP-8.

CHIP-8 and Files can run together. Click a window or its taskbar entry to
bring it to the front and give it keyboard focus. Drag its title bar to
move it. Each window has minimize, maximize/restore and close buttons.
Click its taskbar entry to restore it after minimizing.

Open **Start → Files** to browse the mounted FAT16 disk. Use **↑ / ↓** to
select, **← / →** to change pages, **Enter** to open a folder or preview a
file, and **Backspace** to go back. You can also click a row to select it
and double-click to open it. Files previews the first 160 bytes in hex and
ASCII.
