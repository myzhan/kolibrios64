@echo off
setlocal

set "files=kernel\boot\bootx64.asm kernel\kernel64.asm"  

for %%a in (%files%) do (
  echo Compiling %%a...
  fasm "%%a"
  if errorlevel 1 (
    echo Error compiling %%a!
    goto :error
  )
)

rd /s /q image
mkdir image\EFI\BOOT
copy kernel\boot\bootx64.efi image\EFI\BOOT\BOOTX64.EFI
copy kernel\kernel64.bin image\EFI\KERNEL64.BIN
qemu-system-x86_64 -cpu qemu64 -bios OVMF.fd -drive driver=vvfat,rw=on,dir=image\
goto :eof

:error
echo Build failed.
exit /b 1
endlocal
