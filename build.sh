fasm bootx64.asm
mkdir -p image/EFI/BOOT
cp bootx64.efi image/EFI/BOOT
qemu-system-x86_64 -cpu qemu64 -bios OVMF.fd -drive driver=vvfat,rw=on,dir=image/
