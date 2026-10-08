#!/bin/sh
# Build a self-contained install of globus-fs and globus-console.
#
#   packaging/build-standalone.sh PREFIX
#
# PREFIX gets its own Python (a relocatable python-build-standalone
# interpreter, fetched by uv) with every dependency installed into it at
# the versions and hashes in uv.lock, and a bin/ holding only the two
# commands.
# Nothing is read from the user's Python, PYTHONPATH, ~/.local or modules,
# so an Lmod module need only prepend PREFIX/bin to PATH.
#
# The tree is relocatable: build it anywhere, then copy or move it.
#
# Environment:
#   PYTHON_VERSION  interpreter to bundle            (default 3.12)
#   GLIBC           oldest glibc the install must run on; compiled
#                   dependencies are chosen to match  (default 2.28, RHEL 8)
#   ARCH            target architecture               (default: this host)
set -eu

PYTHON_VERSION=${PYTHON_VERSION:-3.12}
GLIBC=${GLIBC:-2.28}
ARCH=${ARCH:-$(uname -m)}

die() {
    echo "build-standalone: $*" >&2
    exit 1
}

[ $# -eq 1 ] || die "usage: $0 PREFIX"
PREFIX=$1
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)

command -v uv >/dev/null 2>&1 ||
    die "uv is required: https://docs.astral.sh/uv/getting-started/installation/"

# Refuse to build over something that is already there: a half-replaced
# install in use by other people is worse than an error.
if [ -e "$PREFIX" ] && [ -n "$(ls -A "$PREFIX" 2>/dev/null)" ]; then
    die "$PREFIX exists and is not empty; choose a new directory"
fi
mkdir -p "$PREFIX"
PREFIX=$(CDPATH= cd -- "$PREFIX" && pwd -P)

# 1. The interpreter. uv installs python-build-standalone builds, which
#    need only glibc 2.17 and are relocatable, into a directory of its
#    own; move it to a fixed name so the launchers do not need the version.
#    Staged inside PREFIX so the move is a rename on one filesystem.
stage=$(mktemp -d "$PREFIX/.stage.XXXXXX")
trap 'rm -rf "$stage"' EXIT
uv python install "$PYTHON_VERSION" --install-dir "$stage" --no-bin --quiet
interp=
for candidate in "$stage"/cpython-*; do
    # uv adds a cpython-X.Y symlink beside the real cpython-X.Y.Z directory
    [ -d "$candidate" ] && [ ! -L "$candidate" ] || continue
    [ -z "$interp" ] || die "more than one interpreter in $stage"
    interp=$candidate
done
[ -n "$interp" ] || die "uv installed no interpreter into $stage"
mv "$interp" "$PREFIX/python"
py="$PREFIX/python/bin/python3"

# 2. Dependencies, exactly as locked in uv.lock. --locked refuses to build
#    if the lock no longer matches pyproject.toml, rather than quietly
#    resolving something new. The install is restricted to wheels that run
#    on the target glibc -- not whatever this build host supports.
#    --break-system-packages: this interpreter belongs to this install.
#    --compile-bytecode: the tree will be read-only, so Python could never
#    cache .pyc files itself and every run would recompile.
platform="${ARCH}-manylinux_$(echo "$GLIBC" | tr . _)"
uv export --project "$root" --locked --no-dev --no-emit-project \
    --format requirements-txt --no-header --quiet \
    -o "$stage/requirements.txt"
uv pip install --python "$py" --break-system-packages --quiet \
    --python-platform "$platform" --require-hashes --compile-bytecode \
    -r "$stage/requirements.txt"
uv pip install --python "$py" --break-system-packages --quiet \
    --python-platform "$platform" --no-deps --compile-bytecode \
    "$root"

# 3. Launchers. -I is isolated mode: PYTHONPATH, PYTHONHOME and the user's
#    site-packages are ignored, so a user's own environment cannot shadow a
#    dependency. The path is found relative to the launcher itself, which
#    is what keeps the tree relocatable; readlink -f follows a symlink to
#    the launcher from another bin directory. `command -p` finds readlink
#    and dirname on the system default path, so a user's broken or unusual
#    PATH cannot break the launcher either.
mkdir -p "$PREFIX/bin"
for tool in fs console; do
    cat >"$PREFIX/bin/globus-$tool" <<EOF
#!/bin/sh
here=\$(command -p dirname -- "\$(command -p readlink -f -- "\$0")")
exec "\$here/../python/bin/python3" -I -m globus_tools.$tool "\$@"
EOF
    chmod 755 "$PREFIX/bin/globus-$tool"
done

# 4. Precompile the bundled standard library too. Some of it (sqlite3,
#    concurrent, ...) ships without bytecode, and a read-only install
#    would otherwise try, and fail, to write those caches on every run.
"$py" -I -m compileall -q "$PREFIX/python/lib" >/dev/null

# 5. Record what was built, and prove the result starts.
version=$("$py" -I -c 'import globus_tools as g; print(g.__version__)')
{
    echo "globus-tools $version"
    echo "built $(date -u +%Y-%m-%dT%H:%M:%SZ) on $(uname -n)"
    echo "python $("$py" -I -c 'import platform; print(platform.python_version())')"
    echo "target ${platform}"
    echo "commit $(git -C "$root" rev-parse --short HEAD 2>/dev/null || echo unknown)"
} >"$PREFIX/BUILD-INFO"

"$PREFIX/bin/globus-fs" --help >/dev/null
"$PREFIX/bin/globus-console" --help >/dev/null

# 6. Last of all, so nothing written above escapes it: no file may be
#    writable by anyone else, since every user runs this code holding their
#    own Globus tokens. uv writes some metadata (dist-info RECORD) 664
#    whatever the umask, so enforce it rather than trust the builder's.
chmod -R go-w "$PREFIX"
writable=$(find "$PREFIX" -perm /022 ! -type l | head -5)
[ -z "$writable" ] || die "still writable by group or other: $writable"

echo "built $PREFIX"
cat "$PREFIX/BUILD-INFO"
echo
echo "Add to PATH:  $PREFIX/bin"
