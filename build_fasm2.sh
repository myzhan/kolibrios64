#!/bin/sh
# Build the 64-bit KolibriOS loader and kernel with fasm2 (fasmg).
#
# The 64-bit sources load the native fasm2 macro headers, see
# kernel/fasm2_compat.inc, so the fasm2 checkout next to this repository is
# required. This script only assembles the two artifacts: it never touches
# image/ and never starts QEMU, the boot test is a separate step.
#
# Environment:
#   FASMG      fasm2 host executable, overrides the default location
#   FASM2_DIR  fasm2 checkout (default: <repo>/../fasm2)
#   FASM1      flat assembler 1.x executable used when the host is bootstrapped
#   OUTDIR     output directory (default: <repo>/build/fasm2)
#
# All three may be given as a relative path. They are made absolute against
# the directory this script was started from before any other path is used,
# because tools/bootstrap-macos.sh changes into the fasm2 source tree and a
# relative FASM2_DIR, FASM1 or OUTDIR would silently point somewhere else
# there.
#
# When no usable host exists at <fasm2>/build/fasm2-bootstrap/fasmg-macos-x64,
# the official <fasm2>/tools/bootstrap-macos.sh is called to create one. The
# bootstrap builds into <fasm2>/build/fasm2-bootstrap, never into OUTDIR, so the
# kernel build tree only ever receives the two artifacts below.
#
# Artifacts:
#   $OUTDIR/BOOTX64.EFI   PE32+ EFI application, machine 0x8664, subsystem 10
#   $OUTDIR/KERNEL64.BIN  flat binary linked at VIRT_KERNEL_BASE

set -eu

# Print the usage block above.
usage() {
        sed -n '2,28p' "$0"
}

root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# Print $1 as an absolute path with . and .. folded away.
#
# The path is only touched lexically: nothing is resolved through the file
# system, so a directory that does not exist keeps its name and the error
# messages below stay meaningful instead of turning into a getcwd failure.
normalise() {
        case $1 in
                /*) path=$1 ;;
                *) path=${PWD:-$(pwd -P)}/$1 ;;
        esac
        out=
        rest=$path
        while [ -n "$rest" ]; do
                case $rest in
                        */*) head=${rest%%/*}; rest=${rest#*/} ;;
                        *) head=$rest; rest= ;;
                esac
                case $head in
                        ''|.) ;;
                        ..) out=${out%/*} ;;
                        *) out=$out/$head ;;
                esac
        done
        printf '%s\n' "${out:-/}"
}

# An empty value stays empty, a bare name is left alone so the shell still
# searches PATH for it, and a path is made absolute.
abspath_or_name() {
        case $1 in
                '') ;;
                */*) normalise "$1" ;;
                *) printf '%s\n' "$1" ;;
        esac
}

fasm2_dir=$(normalise "${FASM2_DIR:-$root/../fasm2}")
outdir=$(normalise "${OUTDIR:-$root/build/fasm2}")

for arg in "$@"; do
        case $arg in
                -h|--help)
                        usage
                        exit 0
                        ;;
                *)
                        echo "build_fasm2.sh: unknown option: $arg" >&2
                        exit 2
                        ;;
        esac
done

# The macro headers are part of the fasm2 include directory.
if [ ! -f "$fasm2_dir/include/macro/struct.inc" ] || \
   [ ! -f "$fasm2_dir/include/macro/proc64.inc" ]; then
        echo "build_fasm2.sh: $fasm2_dir is not a fasm2 checkout" >&2
        echo "build_fasm2.sh: set FASM2_DIR to the fasm2 repository" >&2
        exit 1
fi

host=$(abspath_or_name "${FASMG:-}")
if [ -z "$host" ]; then
        # The bootstrap installs the host into the fasm2 checkout, so its output
        # directory is spelled out here instead of being inherited: OUTDIR names
        # the kernel artifacts and must not leak into the bootstrap script.
        bootstrap_outdir=$fasm2_dir/build/fasm2-bootstrap
        host=$bootstrap_outdir/fasmg-macos-x64
        if [ ! -x "$host" ]; then
                if [ ! -f "$fasm2_dir/tools/bootstrap-macos.sh" ]; then
                        echo "build_fasm2.sh: no host and no bootstrap script in $fasm2_dir" >&2
                        echo "build_fasm2.sh: set FASMG to a fasm2 host executable" >&2
                        exit 1
                fi
                fasm1=${FASM1:-}
                if [ -z "$fasm1" ]; then
                        if command -v fasm >/dev/null 2>&1; then
                                fasm1=fasm
                        elif [ -x "$root/../.tools/fasm-fixed" ]; then
                                fasm1=$root/../.tools/fasm-fixed
                        fi
                fi
                if [ -z "$fasm1" ]; then
                        echo "build_fasm2.sh: the fasm2 bootstrap needs flat assembler 1.x" >&2
                        echo "build_fasm2.sh: set FASM1 to the executable to use" >&2
                        exit 1
                fi
                # bootstrap-macos.sh changes into <fasm2>/source/macos/x64, so
                # a relative FASM1 would be looked up from the wrong directory
                fasm1=$(abspath_or_name "$fasm1")
                echo "build_fasm2.sh: bootstrapping the fasm2 host with $fasm1"
                FASM1=$fasm1 OUTDIR=$bootstrap_outdir \
                        sh "$fasm2_dir/tools/bootstrap-macos.sh"
        fi
fi

if [ ! -x "$host" ]; then
        echo "build_fasm2.sh: $host is not executable" >&2
        exit 1
fi

# Assemble one source into one artifact, the exit status is propagated.
assemble() {
        src=$1
        out=$2
        echo "build_fasm2.sh: $src -> $out"
        INCLUDE="$fasm2_dir/include" "$host" -e100 \
                -i"Include 'fasm2.inc'" "$root/$src" "$out"
}

mkdir -p "$outdir"

assemble kernel/boot/bootx64.asm "$outdir/BOOTX64.EFI"
assemble kernel/kernel64.asm "$outdir/KERNEL64.BIN"

for artifact in "$outdir/BOOTX64.EFI" "$outdir/KERNEL64.BIN"; do
        if [ ! -s "$artifact" ]; then
                echo "build_fasm2.sh: $artifact was not produced" >&2
                exit 1
        fi
done

echo "build_fasm2.sh: host     $host"
echo "build_fasm2.sh: BOOTX64.EFI  $(wc -c < "$outdir/BOOTX64.EFI" | tr -d ' ') bytes"
echo "build_fasm2.sh: KERNEL64.BIN $(wc -c < "$outdir/KERNEL64.BIN" | tr -d ' ') bytes"
echo "build_fasm2.sh: done"
