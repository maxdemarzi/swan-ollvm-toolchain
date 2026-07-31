# obfuscator-llvm20

A vendored copy of the OLLVM obfuscator's `Obfuscator` module (source and headers), ported to
build against LLVM commit [`LLVM_COMMIT`](./LLVM_COMMIT) instead of the LLVM 22 base the native
(`clang`/`lld`-bundling) toolchain uses.

## Why this exists, separately from `OLLVM_COMMIT` at the repo root

The wasm obfuscation path (`swan/cmake/swan_obf_wasm_compiler_wrapper.py`) runs `opt
-passes=obfuscation` between Emscripten's IR emission and codegen steps. `opt`'s IR printer
re-serializes the whole module using its own LLVM version's attribute syntax, so `opt` and the
`em++` it feeds IR to must share exactly one LLVM version -- any skew either fails to parse, or
worse, parses and links "successfully" while producing a `.wasm` silently ABI-incompatible with
the rest of a real Emscripten-built module. Emscripten 3.1.71 (the version swan's wasm build
pins, matching its `duckdb` submodule) bundles LLVM 20, not the LLVM 22 the native
`swan-ollvm-toolchain` releases (`toolchain-v1` onward) are built against -- so the wasm path
needs its own `opt`, built from this same obfuscator source against a matching LLVM 20 base.

## Contents

- `src/include/llvm/Transforms/Obfuscator.h`, `src/include/llvm/Transforms/Obfuscator/`,
  `src/lib/Transforms/Obfuscator/` -- the obfuscator module itself, vendored from
  [`maxdemarzi/ollvm`](https://github.com/maxdemarzi/ollvm) at commit
  [`OLLVM_SOURCE_COMMIT`](./OLLVM_SOURCE_COMMIT), with the minimum adaptations needed to build
  against LLVM 20 instead of that commit's LLVM 22 base. Of 90 vendored files, only 4 needed real
  API adaptations (all mechanical LLVM-20-vs-22 signature changes):
  - `BasicBlock::getFirstNonPHIOrDbgOrLifetime()` returns an `Instruction*` on LLVM 20 (needs
    `->getIterator()`), not an iterator directly
    (`BogusControlFlow.cpp`, `ADec/Tech_DeadDecoy.cpp`, `ADec/Tech_FakeLoop.cpp`)
  - `Module::getTargetTriple()` returns a plain `std::string` on LLVM 20, not a `Triple` object
    (`ObfReport.cpp`)
- `integration.patch` -- the same small (~10-line) integration footprint the native toolchain
  uses (`PassBuilder.cpp` includes, `PassRegistry.def` registrations,
  `Transforms/CMakeLists.txt`'s `add_subdirectory`), rebased for LLVM 20's version of those files.
- `LLVM_COMMIT` -- the exact `llvm/llvm-project` commit this is meant to be grafted onto
  (matches Emscripten 3.1.71's bundled LLVM exactly -- re-verify via `em++ --version` if swan's
  Emscripten pin ever changes).
- `OLLVM_SOURCE_COMMIT` -- the `maxdemarzi/ollvm` commit this was ported from, for provenance.

## Known gap: `strenc`'s AES stub

The `strenc` (string encryption) pass's AES runtime stub ships as a precompiled bitcode blob
built against LLVM 22.1.8. Under this LLVM 20 `opt`, linking that stub fails to parse
(`Unknown attribute kind (102)`) and `strenc` silently falls back to skipping encryption rather
than hard-failing the whole build. Every other pass (`bcf`, `flattening`, `mba`, `vcall`, `vm`,
etc.) is unaffected. Not yet fixed -- would need either a bitcode stub rebuilt against LLVM 20,
or a source-level (non-bitcode) fallback for the AES runtime.

## Building

See `.github/workflows/build-toolchain-wasm.yml` at the repo root, which grafts this onto a
fresh `llvm/llvm-project` checkout at `LLVM_COMMIT` and builds `opt` only (the wasm path never
needs `clang`/`lld` from this toolchain -- compilation still goes through Emscripten's own
`em++`).
