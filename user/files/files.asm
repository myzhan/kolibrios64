; Read-only FAT16 browser for the KolibriOS64 hosted application ABI.
; The kernel supplies an RX image at 0x100400000 and zeroed RW data.

use64
org 0x100400000

SYS_EXIT = 0
SYS_BLIT = 1
SYS_READ_KEY = 2
SYS_POLL = 3
SYS_OPEN = 4
SYS_READ = 5
SYS_CLOSE = 6
SYS_READDIR = 9
POLL_CLIENT = 0x434C4E54
POLL_PRESS = 0x100
DOUBLE_CLICK_TICKS = 50                 ; 500 ms at 100 Hz
DOUBLE_CLICK_SLOP = 4

CLIENT_X = 192
CLIENT_Y = 224
CLIENT_W = 640
CLIENT_H = 320
STRIDE = CLIENT_W * 4
PAGE_ROWS = 11
DIRENT_SIZE = 32
DIR_ATTR = 0x10
PATH_BYTES = 128
PREVIEW_BYTES = 160
LIST_TOP = 40
ROW_PITCH = 20
ROW_H = 18

COLOR_BACK = 0x182532
COLOR_SELECT = 0x304B65
COLOR_TEXT = 0xE9EDF2
COLOR_MUTED = 0xACBCCD
COLOR_TITLE = 0xADE8F4

virtual at 0x100600000
frame           rd CLIENT_W * CLIENT_H
entries         rb (PAGE_ROWS + 1) * DIRENT_SIZE
directory       rb PATH_BYTES
target          rb PATH_BYTES
entry_name      rb 13
preview         rb PREVIEW_BYTES
preview_line    rb 67
        align 8
message         rq 1
last_click_tick rq 1
last_click_row  rd 1
last_click_xy   rd 1
page_number     rd 1
selected        rd 1
entry_count     rd 1
preview_count   rd 1
more_pages      rb 1
show_preview    rb 1
end virtual

start:
        cld
        mov     byte [directory], '/'
        mov     dword [last_click_row], -1
        call    load_directory
.paint:
        call    paint
.key:
        mov     eax, SYS_READ_KEY
        syscall
        test    edx, edx
        jz      .park
        test    edx, 0x80               ; release
        jnz     .key
        mov     dword [last_click_row], -1
        cmp     edx, 0x01               ; Esc
        je      .exit
        cmp     edx, 0x0E               ; Backspace
        je      .back
        cmp     byte [show_preview], 0
        jne     .key
        cmp     edx, 0x148
        je      .up
        cmp     edx, 0x150
        je      .down
        cmp     edx, 0x14B
        je      .previous
        cmp     edx, 0x14D
        je      .next
        cmp     edx, 0x1C
        jne     .key
        call    open_selection
        jmp     .paint
.park:
        mov     eax, SYS_POLL
        mov     edi, POLL_CLIENT
        syscall
        test    edx, POLL_PRESS
        jz      .key
        cmp     byte [show_preview], 0
        jne     .key
        mov     ecx, eax                ; packed client position
        shr     eax, 16
        sub     eax, LIST_TOP
        cmp     eax, PAGE_ROWS * ROW_PITCH
        jae     .clear_click
        xor     edx, edx
        mov     esi, ROW_PITCH
        div     esi
        cmp     edx, ROW_H
        jae     .clear_click
        cmp     eax, [entry_count]
        jae     .clear_click
        cmp     eax, [last_click_row]
        jne     .select
        mov     r9, r8
        sub     r9, [last_click_tick]
        cmp     r9, DOUBLE_CLICK_TICKS
        ja      .select
        mov     r9d, [last_click_xy]
        movzx   esi, cx
        movzx   edi, r9w
        sub     esi, edi
        add     esi, DOUBLE_CLICK_SLOP
        cmp     esi, DOUBLE_CLICK_SLOP * 2
        ja      .select
        mov     esi, ecx
        shr     esi, 16
        shr     r9d, 16
        sub     esi, r9d
        add     esi, DOUBLE_CLICK_SLOP
        cmp     esi, DOUBLE_CLICK_SLOP * 2
        ja      .select
        mov     dword [last_click_row], -1
        call    open_selection
        jmp     .paint
.select:
        mov     [last_click_row], eax
        mov     [last_click_xy], ecx
        mov     [last_click_tick], r8
        cmp     eax, [selected]
        je      .key
        mov     [selected], eax
        jmp     .paint
.clear_click:
        mov     dword [last_click_row], -1
        jmp     .key
.up:
        cmp     dword [selected], 0
        je      .key
        dec     dword [selected]
        jmp     .paint
.down:
        mov     eax, [selected]
        inc     eax
        cmp     eax, [entry_count]
        jae     .key
        mov     [selected], eax
        jmp     .paint
.previous:
        cmp     dword [page_number], 0
        je      .key
        dec     dword [page_number]
        jmp     .reload
.next:
        cmp     byte [more_pages], 0
        je      .key
        inc     dword [page_number]
        jmp     .reload
.back:
        cmp     byte [show_preview], 0
        je      .parent
        mov     byte [show_preview], 0
        mov     qword [message], 0
        jmp     .paint
.parent:
        cmp     byte [directory + 1], 0
        je      .key
        lea     rsi, [directory + 1]
        mov     rdi, rsi
.scan_parent:
        lodsb
        cmp     al, '/'
        jne     .parent_end
        lea     rdi, [rsi - 1]
.parent_end:
        test    al, al
        jnz     .scan_parent
        mov     byte [rdi], 0
        mov     dword [page_number], 0
.reload:
        call    load_directory
        jmp     .paint
.exit:
        mov     eax, SYS_EXIT
        xor     edi, edi
        syscall

; Pull one bounded page, with one extra entry to detect a subsequent page.
load_directory:
        push    r12
        push    r13
        mov     qword [message], 0
        mov     dword [selected], 0
        mov     dword [entry_count], 0
        mov     byte [more_pages], 0
        lea     rdi, [directory]
        xor     esi, esi
        mov     eax, SYS_OPEN
        syscall
        test    rax, rax
        js      .failed
        mov     r12d, eax
        mov     r13d, [page_number]
.skip:
        mov     edi, r12d
        lea     rsi, [entries]
        mov     edx, PAGE_ROWS * DIRENT_SIZE
        test    r13d, r13d
        jz      .read
        mov     eax, SYS_READDIR
        syscall
        cmp     rax, PAGE_ROWS * DIRENT_SIZE
        jne     .close_with_error
        dec     r13d
        jmp     .skip
.read:
        add     edx, DIRENT_SIZE
        mov     eax, SYS_READDIR
        syscall
        test    rax, rax
        js      .close_with_error
        shr     eax, 5
        cmp     eax, PAGE_ROWS
        jbe     .count
        mov     byte [more_pages], 1
        mov     eax, PAGE_ROWS
.count:
        mov     [entry_count], eax
.close:
        mov     edi, r12d
        mov     eax, SYS_CLOSE
        syscall
        pop     r13
        pop     r12
        ret
.close_with_error:
        lea     rax, [text_read_error]
        mov     [message], rax
        jmp     .close
.failed:
        lea     rax, [text_read_error]
        mov     [message], rax
        pop     r13
        pop     r12
        ret

; rsi points to the raw 11-byte FAT name. Return a terminated short name.
format_name:
        lea     rdi, [entry_name]
        mov     ecx, 8
.base:
        lodsb
        cmp     al, ' '
        je      .next_base
        stosb
.next_base:
        loop    .base
        cmp     byte [rsi], ' '
        je      .done
        mov     al, '.'
        stosb
        mov     ecx, 3
.extension:
        lodsb
        cmp     al, ' '
        je      .done
        stosb
        loop    .extension
.done:
        mov     byte [rdi], 0
        ret

open_selection:
        push    r12
        mov     eax, [selected]
        cmp     eax, [entry_count]
        jae     .done
        shl     eax, 5
        lea     r12, [entries]
        add     r12, rax
        lea     rsi, [r12 + 16]
        call    format_name
        cmp     byte [entry_name], '.'  ; use Backspace to visit the parent
        je      .done
        lea     rsi, [directory]
        lea     rdi, [target]
.copy_directory:
        lodsb
        test    al, al
        jz      .append
        stosb
        jmp     .copy_directory
.append:
        lea     rsi, [entry_name]
        xor     eax, eax
.name_length:
        cmp     byte [rsi + rax], 0
        je      .check_length
        inc     eax
        jmp     .name_length
.check_length:
        cmp     byte [rdi - 1], '/'
        setne   dl
        movzx   edx, dl
        add     rax, rdx
        lea     rax, [rdi + rax + 1]    ; include the terminator
        lea     rdx, [target + PATH_BYTES]
        cmp     rax, rdx
        ja      .too_long
        cmp     byte [rdi - 1], '/'
        je      .copy_name
        mov     al, '/'
        stosb
.copy_name:
        lea     rsi, [entry_name]
.name:
        lodsb
        stosb
        test    al, al
        jnz     .name
        test    byte [r12], DIR_ATTR
        jz      .file
        lea     rsi, [target]
        lea     rdi, [directory]
        mov     ecx, PATH_BYTES
        rep     movsb
        mov     dword [page_number], 0
        call    load_directory
        jmp     .done
.file:
        call    load_preview
        jmp     .done
.too_long:
        lea     rax, [text_long_path]
        mov     [message], rax
.done:
        pop     r12
        ret

load_preview:
        push    r12
        mov     byte [show_preview], 1
        mov     dword [preview_count], 0
        lea     rax, [text_read_error]
        mov     [message], rax
        lea     rdi, [target]
        xor     esi, esi
        mov     eax, SYS_OPEN
        syscall
        test    rax, rax
        js      .done
        mov     r12d, eax
        mov     edi, r12d
        lea     rsi, [preview]
        mov     edx, PREVIEW_BYTES
        mov     eax, SYS_READ
        syscall
        test    rax, rax
        js      .close
        mov     [preview_count], eax
        mov     qword [message], 0
.close:
        mov     eax, SYS_CLOSE
        mov     edi, r12d
        syscall
.done:
        pop     r12
        ret

; Paint the complete private client buffer, then submit it via SYS_BLIT.
paint:
        push    r12
        push    r13
        lea     rdi, [frame]
        mov     eax, COLOR_BACK
        mov     ecx, CLIENT_W * CLIENT_H
        rep     stosd
        mov     edi, 12
        mov     esi, 8
        mov     edx, COLOR_TITLE
        lea     rcx, [directory]
        cmp     byte [show_preview], 0
        je      .heading
        lea     rcx, [target]
.heading:
        call    draw_text
        cmp     byte [show_preview], 0
        jne     .preview
        xor     r12d, r12d
.row:
        cmp     r12d, [entry_count]
        jae     .directory_footer
        imul    r13d, r12d, ROW_PITCH
        add     r13d, LIST_TOP
        cmp     r12d, [selected]
        jne     .label
        mov     eax, r13d
        imul    eax, STRIDE
        lea     rdi, [frame]
        add     rdi, rax
        mov     eax, COLOR_SELECT
        mov     ecx, CLIENT_W * ROW_H
        rep     stosd
.label:
        mov     eax, r12d
        shl     eax, 5
        lea     rsi, [entries]
        add     rsi, rax
        lea     rcx, [text_file]
        test    byte [rsi], DIR_ATTR
        jz      .type
        lea     rcx, [text_dir]
.type:
        add     rsi, 16
        push    rcx
        call    format_name
        pop     rcx
        mov     edi, 12
        lea     esi, [r13d + 1]
        mov     edx, COLOR_MUTED
        call    draw_text
        mov     edi, 76
        lea     esi, [r13d + 1]
        mov     edx, COLOR_TEXT
        lea     rcx, [entry_name]
        call    draw_text
        inc     r12d
        jmp     .row
.directory_footer:
        lea     rcx, [text_browse_keys]
        cmp     dword [entry_count], 0
        jne     .footer
        cmp     qword [message], 0
        jne     .footer
        lea     rax, [text_empty_dir]
        mov     [message], rax
        jmp     .footer
.preview:
        xor     r12d, r12d
        mov     r13d, 36
.preview_row:
        cmp     r12d, [preview_count]
        jae     .preview_footer
        mov     edi, r12d
        call    format_preview_line
        mov     edi, 12
        mov     esi, r13d
        mov     edx, COLOR_TEXT
        lea     rcx, [preview_line]
        call    draw_text
        add     r12d, 16
        add     r13d, 21
        jmp     .preview_row
.preview_footer:
        lea     rcx, [text_preview_keys]
        cmp     dword [preview_count], 0
        jne     .footer
        cmp     qword [message], 0
        jne     .footer
        lea     rax, [text_empty_file]
        mov     [message], rax
.footer:
        mov     edi, 12
        mov     esi, 298
        mov     edx, COLOR_MUTED
        call    draw_text
        mov     rcx, [message]
        test    rcx, rcx
        jz      .blit
        mov     edi, 12
        mov     esi, 272
        mov     edx, COLOR_TITLE
        call    draw_text
.blit:
        mov     eax, SYS_BLIT
        lea     rdi, [frame]
        mov     esi, CLIENT_X
        mov     edx, CLIENT_Y
        mov     r10d, CLIENT_W
        mov     r8d, CLIENT_H
        syscall
        pop     r13
        pop     r12
        ret

; Format up to 16 bytes as hex pairs and printable ASCII. edi = offset.
format_preview_line:
        mov     r8d, edi
        lea     rdi, [preview_line]
        mov     al, ' '
        mov     ecx, 66
        rep     stosb
        mov     byte [rdi], 0
        lea     rdi, [preview_line]
        lea     rsi, [preview]
        add     rsi, r8
        lea     r10, [hex_digits]
        lea     r9, [preview_line + 50]
        mov     ecx, 16
.byte:
        cmp     r8d, [preview_count]
        jae     .done
        movzx   eax, byte [rsi]
        inc     rsi
        mov     edx, eax
        shr     eax, 4
        mov     al, [r10 + rax]
        stosb
        mov     eax, edx
        and     eax, 0x0F
        mov     al, [r10 + rax]
        stosb
        inc     rdi
        cmp     edx, 0x20
        jb      .dot
        cmp     edx, 0x7E
        jbe     .ascii
.dot:
        mov     dl, '.'
.ascii:
        mov     [r9], dl
        inc     r9
        inc     r8d
        loop    .byte
.done:
        ret

; Draw a clipped sequence of 8x16 glyphs, leaving the background intact.
; edi = x, esi = y, edx = color, rcx = terminated string.
draw_text:
        push    r12
        push    r13
        push    r14
        push    r15
        mov     r12, rcx
        mov     r14d, edx
        mov     r15d, CLIENT_W
        sub     r15d, edi
        shr     r15d, 3
        imul    esi, STRIDE
        lea     r13, [frame]
        add     r13, rsi
        lea     r13, [r13 + rdi*4]
.glyph:
        test    r15d, r15d
        jz      .done
        movzx   eax, byte [r12]
        test    al, al
        jz      .done
        cmp     al, 0x20
        jb      .unknown
        cmp     al, 0x7E
        jbe     .font
.unknown:
        mov     al, '?'
.font:
        sub     eax, 0x20
        shl     eax, 4
        lea     rsi, [video_font]
        add     rsi, rax
        mov     rdi, r13
        mov     ecx, 16
.glyph_row:
        lodsb
        mov     edx, 8
.bit:
        test    al, 1
        jz      .transparent
        mov     [rdi], r14d
.transparent:
        add     rdi, 4
        shr     al, 1
        dec     edx
        jnz     .bit
        add     rdi, STRIDE - 8 * 4
        loop    .glyph_row
        inc     r12
        add     r13, 8 * 4
        dec     r15d
        jmp     .glyph
.done:
        pop     r15
        pop     r14
        pop     r13
        pop     r12
        ret

text_dir          db '[DIR]', 0
text_file         db '[FILE]', 0
text_read_error   db 'Could not read this path.', 0
text_long_path    db 'Path is too long.', 0
text_empty_dir    db 'Empty folder.', 0
text_empty_file   db 'Empty file.', 0
text_browse_keys  db 'UP/DOWN Select   LEFT/RIGHT Page   ENTER Open   BACKSPACE Parent   ESC Exit', 0
text_preview_keys db 'First 160 bytes   BACKSPACE Folder   ESC Exit', 0
hex_digits        db '0123456789ABCDEF'

VIDEO_FONT_BYTES = 1520
include '../../kernel/video/font.inc'

if $ - start > 16 * 1024
        err 'Files program exceeds the hosted code domain'
end if
