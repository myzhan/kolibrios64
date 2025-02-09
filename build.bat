@echo off
fasm kernel\boot\bootx64.asm
fasm kernel\kernel64.asm
if errorlevel 1 (
    echo build failed
    exit /b 1
)
rd /s /q image
mkdir image\EFI\BOOT
copy kernel\boot\bootx64.efi image\EFI\BOOT\BOOTX64.EFI
copy kernel\kernel64.bin image\EFI\KERNEL64.BIN
qemu-system-x86_64 -cpu qemu64 -bios OVMF.fd -drive driver=vvfat,rw=on,dir=image\
