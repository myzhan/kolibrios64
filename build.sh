#!/bin/sh
if fasm kernel/boot/bootx64.asm && fasm kernel/kernel64.asm; then
    rm -rf image
    mkdir -p image/EFI/BOOT
    cp kernel/boot/bootx64.efi image/EFI/BOOT/BOOTX64.EFI
    cp kernel/kernel64.bin image/EFI/KERNEL64.BIN
    qemu-system-x86_64 -m 128 -cpu qemu64 -bios OVMF.fd -drive driver=vvfat,rw=on,dir=image/
else
    echo "build failed"
fi
