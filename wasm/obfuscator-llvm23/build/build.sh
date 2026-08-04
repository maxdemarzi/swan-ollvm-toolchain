#!/bin/bash
# Self-contained build: clones LLVM at LLVM_COMMIT, grafts this directory's
# vendored Obfuscator module + integration.patch on, builds `opt` only.
#
# Run inside the swan-ollvm-llvm23-base image (see ./Dockerfile.base), with
# this obfuscator-llvm23/ directory bind-mounted at /work/obfuscator-llvm23:
#
#   docker build -t swan-ollvm-llvm23-base -f Dockerfile.base .
#   docker run --rm -v "$(pwd)":/work/obfuscator-llvm23 \
#     -e CCACHE_DIR=/work/obfuscator-llvm23/build/ccache \
#     swan-ollvm-llvm23-base bash /work/obfuscator-llvm23/build/build.sh
set -euxo pipefail

OBF_DIR=/work/obfuscator-llvm23
LLVM_COMMIT="$(cat "$OBF_DIR/LLVM_COMMIT")"

gcc --version
ninja --version

# Single clone, then a plain directory copy for the second (patched) tree --
# cheaper than cloning twice and equally correct, since both start from a
# byte-identical unpatched checkout at the same commit.
mkdir -p /work/build
cd /work/build
git init llvm-src-clean
git -C llvm-src-clean remote add origin https://github.com/llvm/llvm-project.git
git -C llvm-src-clean fetch --depth 1 origin "$LLVM_COMMIT"
git -C llvm-src-clean checkout FETCH_HEAD
cp -r llvm-src-clean llvm-src

# Graft the vendored Obfuscator module + integration patch onto the second
# (patched) tree only. See obfuscator-llvm20's build-toolchain-wasm.yml for
# why the bootstrap clang below MUST come from the unpatched tree: a clang
# built from the patched tree would transitively depend on aes_stub.bc, the
# file the patched opt build is supposed to compile -- a circular dependency.
cp -r "$OBF_DIR/src/include/llvm/Transforms/Obfuscator.h" llvm-src/llvm/include/llvm/Transforms/Obfuscator.h
cp -r "$OBF_DIR/src/include/llvm/Transforms/Obfuscator" llvm-src/llvm/include/llvm/Transforms/Obfuscator
cp -r "$OBF_DIR/src/lib/Transforms/Obfuscator" llvm-src/llvm/lib/Transforms/Obfuscator
patch -p1 -d llvm-src < "$OBF_DIR/integration.patch"

# Bootstrap clang, from the CLEAN (unpatched) tree.
cd /work/build/llvm-src-clean
cmake -S llvm -B build-bootstrap -G Ninja \
  -DLLVM_ENABLE_PROJECTS=clang \
  -DLLVM_TARGETS_TO_BUILD='X86;AArch64' \
  -DLLVM_INCLUDE_TESTS=OFF -DLLVM_BUILD_TESTS=OFF \
  -DLLVM_INCLUDE_BENCHMARKS=OFF -DLLVM_INCLUDE_EXAMPLES=OFF \
  -DLLVM_ENABLE_ASSERTIONS=OFF \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER_LAUNCHER=ccache \
  -DCMAKE_CXX_COMPILER_LAUNCHER=ccache \
  -DLLVM_PARALLEL_LINK_JOBS=1
cmake --build build-bootstrap --parallel 8 --target clang
build-bootstrap/bin/clang --version

# Patched opt build, from the PATCHED tree, using the clean bootstrap clang
# to compile the strenc pass's aes_stub.c.
cd /work/build/llvm-src
cmake -S llvm -B build -G Ninja \
  -DAES_STUB_CLANG_OVERRIDE=/work/build/llvm-src-clean/build-bootstrap/bin/clang \
  -DLLVM_ENABLE_RTTI=ON \
  -DLLVM_ENABLE_EH=ON \
  -DLLVM_TARGETS_TO_BUILD='X86;AArch64' \
  -DLLVM_INCLUDE_TESTS=OFF -DLLVM_BUILD_TESTS=OFF \
  -DLLVM_INCLUDE_BENCHMARKS=OFF -DLLVM_INCLUDE_EXAMPLES=OFF \
  -DLLVM_ENABLE_ASSERTIONS=OFF \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER_LAUNCHER=ccache \
  -DCMAKE_CXX_COMPILER_LAUNCHER=ccache \
  -DLLVM_PARALLEL_LINK_JOBS=1
cmake --build build --parallel 8 --target opt
ccache -s

build/bin/opt --version
