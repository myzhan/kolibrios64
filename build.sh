if fasm kernel/boot/bootx64.asm; then
    mkdir -p image/EFI/BOOT
    cp kernel/boot/bootx64.efi image/EFI/BOOT
    qemu-system-x86_64 -cpu qemu64 -bios OVMF.fd -drive driver=vvfat,rw=on,dir=image/
else
    echo "build failed"
fi
