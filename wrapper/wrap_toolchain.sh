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

echo "wrap_toolchain.sh: wrapped $BIN_DIR/clang and $BIN_DIR/clang++"
