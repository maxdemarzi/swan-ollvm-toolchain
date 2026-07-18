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

# Smoke check 3 (macOS only, where clang.real.cfg exists): diagnostic
# matrix isolating which variable actually causes the intermittent
# "undefined std::length_error[abi:...]" failure -- seen through the
# wrapper with -std=c++17, but NOT in the equivalent unwrapped single-shot
# compile+link without -std=c++17. Crosses: wrapped vs unwrapped compiler,
# -std=c++17 vs compiler default, and single-invocation vs split
# compile-then-link. Deliberately non-fatal (temporarily) so the full
# matrix runs and reports before this becomes a hard gate again.
if [ -f "$BIN_DIR/clang++.real.cfg" ]; then
    cat > "$TMP_DIR/stdexcept_smoke.cpp" <<'SMOKE2'
#include <stdexcept>
int main() {
    try { throw std::length_error("x"); }
    catch (const std::length_error&) { return 0; }
    return 1;
}
SMOKE2

    run_variant() {
        label=$1
        cxx=$2
        std_flag=$3
        split=$4
        out="$TMP_DIR/out_$label"
        if [ "$split" = "split" ]; then
            if "$cxx" $std_flag -arch arm64 -mmacosx-version-min=11.0 -c "$TMP_DIR/stdexcept_smoke.cpp" -o "$out.o" > "$TMP_DIR/$label.compile.log" 2>&1 \
               && "$cxx" -arch arm64 -mmacosx-version-min=11.0 "$out.o" -o "$out" > "$TMP_DIR/$label.link.log" 2>&1; then
                echo "DIAG $label: PASS"
            else
                echo "DIAG $label: FAIL"
                tail -5 "$TMP_DIR/$label.compile.log" "$TMP_DIR/$label.link.log" 2>/dev/null | sed "s/^/DIAG $label:   /"
            fi
        else
            if "$cxx" $std_flag -arch arm64 -mmacosx-version-min=11.0 "$TMP_DIR/stdexcept_smoke.cpp" -o "$out" > "$TMP_DIR/$label.log" 2>&1; then
                echo "DIAG $label: PASS"
            else
                echo "DIAG $label: FAIL"
                tail -5 "$TMP_DIR/$label.log" | sed "s/^/DIAG $label:   /"
            fi
        fi
    }

    run_variant unwrapped_nostd_combined  "$BIN_DIR/clang++.real" ""            combined
    run_variant unwrapped_std17_combined  "$BIN_DIR/clang++.real" "-std=c++17"  combined
    run_variant unwrapped_nostd_split     "$BIN_DIR/clang++.real" ""            split
    run_variant unwrapped_std17_split     "$BIN_DIR/clang++.real" "-std=c++17"  split
    run_variant wrapped_nostd_split       "$BIN_DIR/clang++"      ""            split
    run_variant wrapped_std17_split       "$BIN_DIR/clang++"      "-std=c++17"  split
fi

echo "wrap_toolchain.sh: wrapped $BIN_DIR/clang and $BIN_DIR/clang++"
