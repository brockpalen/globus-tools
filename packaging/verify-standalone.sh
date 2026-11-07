#!/bin/sh
# Check a tree built by build-standalone.sh before putting it on PATH.
#
#   packaging/verify-standalone.sh PREFIX
#
# Proves what INSTALL.md promises about the tree: it starts with an empty
# environment, a PYTHONPATH or working directory holding a foreign
# globus_sdk cannot replace its own, it still works when moved or reached
# through a symlink, nothing in it is writable by group or other, and no
# compiled file needs a newer glibc than the one it was built for.
#
# Environment:
#   GLIBC   newest glibc symbol version allowed (default: from BUILD-INFO)
set -eu

die() {
    echo "verify-standalone: FAIL: $*" >&2
    exit 1
}

ok() {
    echo "ok    $*"
}

[ $# -eq 1 ] || die "usage: $0 PREFIX"
PREFIX=$(CDPATH= cd -- "$1" && pwd -P) || die "no such directory: $1"
[ -f "$PREFIX/BUILD-INFO" ] || die "$PREFIX/BUILD-INFO missing; not a standalone build"

# target x86_64-manylinux_2_28 -> 2.28
target=$(sed -n 's/^target //p' "$PREFIX/BUILD-INFO")
GLIBC=${GLIBC:-$(echo "${target##*manylinux_}" | tr _ .)}

scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT

starts() {
    # starts LAUNCHER_DIR [env assignments...]: both commands print --help
    dir=$1
    shift
    for tool in globus-fs globus-console; do
        env "$@" "$dir/$tool" --help >/dev/null 2>"$scratch/err" ||
            die "$dir/$tool --help: $(cat "$scratch/err")"
    done
}

# 1. An empty environment: no PATH, HOME or module settings needed.
starts "$PREFIX/bin" -i
ok "starts with an empty environment"

# 1b. Tab completion in globus-fs shell needs the bundled readline.
"$PREFIX/python/bin/python3" -I -c "import readline" 2>"$scratch/err" ||
    die "no readline in the bundled Python: $(cat "$scratch/err")"
ok "readline present, for globus-fs shell"

# 2. A foreign globus_sdk, as an old globus-cli module would supply, on
#    PYTHONPATH and in the working directory.
mkdir -p "$scratch/poison/globus_sdk"
echo "raise ImportError('foreign globus_sdk imported')" \
    >"$scratch/poison/globus_sdk/__init__.py"
(cd "$scratch/poison" && starts "$PREFIX/bin" PYTHONPATH="$scratch/poison")
ok "ignores a foreign globus_sdk on PYTHONPATH and in the current directory"

# 3. Moved, and run through a symlink from another bin directory.
cp -a "$PREFIX" "$scratch/moved"
mkdir "$scratch/links"
ln -s "$scratch/moved/bin/globus-fs" "$scratch/links/globus-fs"
ln -s "$scratch/moved/bin/globus-console" "$scratch/links/globus-console"
starts "$scratch/links"
ok "relocatable, and runs through a symlink"

# 4. Every user runs this code holding their own Globus tokens.
writable=$(find "$PREFIX" -perm /022 ! -type l | head -5)
[ -z "$writable" ] || die "writable by group or other: $writable"
ok "nothing writable by group or other"

# 5. No compiled file may need a newer glibc than the target.
if command -v objdump >/dev/null 2>&1; then
    needed=$(find "$PREFIX" -type f \( -name '*.so' -o -name '*.so.*' \
        -o -path '*/python/bin/python3*' \) -exec objdump -T {} + 2>/dev/null |
        grep -o 'GLIBC_[0-9][0-9.]*' | sed 's/GLIBC_//' | sort -uV | tail -1)
    [ -n "$needed" ] || die "objdump found no glibc symbols; is this a Linux build?"
    newest=$(printf '%s\n%s\n' "$needed" "$GLIBC" | sort -V | tail -1)
    [ "$newest" = "$GLIBC" ] || die "needs glibc $needed, newer than $GLIBC"
    ok "needs glibc $needed at most (target $GLIBC)"
else
    echo "skip  glibc check: objdump not found (binutils)"
fi

echo "verified $PREFIX"
