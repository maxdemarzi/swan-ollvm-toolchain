#!/bin/sh
# Bundles swan_obf_compiler_wrapper.py into a built/fetched toolchain's
# install/bin directory, renaming the real clang/clang++ to clang.real/
# clang++.real and installing thin trampolines at the original clang/clang++
# paths. This means every existing consumer (Dockerfiles, CI steps) that
# already points CMAKE_C_COMPILER/CMAKE_CXX_COMPILER at .../bin/clang and
# .../bin/clang++ transparently gets the 3-step obfuscation pipeline with no
# further changes -- see swan_obf_compiler_wrapper.py's own docstring for why
# that 3-step dance (emit IR -> opt -passes=obfuscation -> compile) is
# required at all instead of a single clang invocation.
#
# Usage: wrap_toolchain.sh <install-dir>   (expects <install-dir>/bin/{clang,clang++,opt})
set -eu

INSTALL_DIR=$1
BIN_DIR="$INSTALL_DIR/bin"
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

mv "$BIN_DIR/clang" "$BIN_DIR/clang.real"
mv "$BIN_DIR/clang++" "$BIN_DIR/clang++.real"
cp "$SCRIPT_DIR/swan_obf_compiler_wrapper.py" "$BIN_DIR/swan_obf_compiler_wrapper.py"
chmod +x "$BIN_DIR/swan_obf_compiler_wrapper.py"

# macOS builds ship a default config file (clang.cfg/clang++.cfg, see
# clang/docs/UsersManual.md "Configuration files") so the bundled static
# libc++ is found at link time. Clang loads it by the *invoked* executable's
# name -- since the wrapper always execs clang.real/clang++.real, the config
# must follow the rename too, or it silently stops being picked up.
[ -f "$BIN_DIR/clang.cfg" ] && mv "$BIN_DIR/clang.cfg" "$BIN_DIR/clang.real.cfg"
[ -f "$BIN_DIR/clang++.cfg" ] && mv "$BIN_DIR/clang++.cfg" "$BIN_DIR/clang++.real.cfg"

write_trampoline() {
    name=$1
    kind=$2
    cat > "$BIN_DIR/$name" <<TRAMPOLINE
#!/bin/sh
DIR=\$(cd "\$(dirname "\$0")" && pwd)
export SWAN_OBF_KIND=$kind
export SWAN_OBF_REAL_CC="\$DIR/clang.real"
export SWAN_OBF_REAL_CXX="\$DIR/clang++.real"
export SWAN_OBF_REAL_OPT="\$DIR/opt"
exec python3 "\$DIR/swan_obf_compiler_wrapper.py" "\$@"
TRAMPOLINE
    chmod +x "$BIN_DIR/$name"
}

write_trampoline clang cc
write_trampoline clang++ cxx

# Smoke check 1: both entry points must still run (passthrough --version).
"$BIN_DIR/clang" --version >/dev/null
"$BIN_DIR/clang++" --version >/dev/null

# Smoke check 2: a real annotated -c compile must go through the 3-step
# pipeline and produce a working binary, not just fall through to
# passthrough mode. Exercises the exact failure mode this wrapper exists to
# fix (strenc/mba/etc. silently never running).
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT
cat > "$TMP_DIR/smoke.cpp" <<'SMOKE'
[[clang::annotate("obf: mba(prob=80)")]]
int compute(int a, int b) { return a * b + (a ^ b) - 7; }
int main() { return compute(3, 4) == 12 ? 0 : 1; }
SMOKE
"$BIN_DIR/clang++" -std=c++17 -O2 -c "$TMP_DIR/smoke.cpp" -o "$TMP_DIR/smoke.o"
"$BIN_DIR/clang++" "$TMP_DIR/smoke.o" -o "$TMP_DIR/smoke"
"$TMP_DIR/smoke"

# Smoke check 3 (macOS only, where clang.real.cfg exists): confirm the
# renamed-binary + default-config-file combination still resolves the
# bundled static libc++ through the wrapper's passthrough (linking) path,
# not just direct compiles. Exercises the exact scenario that motivated
# bundling libc++ in the first place.
if [ -f "$BIN_DIR/clang++.real.cfg" ]; then
    cat > "$TMP_DIR/stdexcept_smoke.cpp" <<'SMOKE2'
#include <stdexcept>
int main() {
    try { throw std::length_error("x"); }
    catch (const std::length_error&) { return 0; }
    return 1;
}
SMOKE2
    # -mmacosx-version-min must match what RUNTIMES_CMAKE_ARGS built
    # libcxx/libcxxabi for (see build-toolchain.yml) -- a mismatch here
    # changes libc++'s internal hidden-ABI tag hash and produces the exact
    # "undefined std::length_error[abi:...]" symptom this smoke test exists
    # to catch, even though the bundled static lib itself is fine.
    "$BIN_DIR/clang++" -std=c++17 -arch arm64 -mmacosx-version-min=11.0 -c "$TMP_DIR/stdexcept_smoke.cpp" -o "$TMP_DIR/stdexcept_smoke.o"
    "$BIN_DIR/clang++" -arch arm64 -mmacosx-version-min=11.0 "$TMP_DIR/stdexcept_smoke.o" -o "$TMP_DIR/stdexcept_smoke"
    "$TMP_DIR/stdexcept_smoke"
    if otool -L "$TMP_DIR/stdexcept_smoke" | grep -qi 'libc++'; then
        echo "wrap_toolchain.sh: FAIL -- still dynamically linked against a system libc++ after wrapping"
        otool -L "$TMP_DIR/stdexcept_smoke"
        exit 1
    fi
fi

echo "wrap_toolchain.sh: wrapped $BIN_DIR/clang and $BIN_DIR/clang++"
