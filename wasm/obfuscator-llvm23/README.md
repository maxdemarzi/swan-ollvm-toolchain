# obfuscator-llvm23

A vendored copy of the OLLVM obfuscator's `Obfuscator` module (source and headers), ported to
build against LLVM commit [`LLVM_COMMIT`](./LLVM_COMMIT) -- the LLVM version bundled by
Emscripten 5.0.3, which the `wasm_eh_pyodide` extension target (swan's Pyodide/browser-Python
build, see `docs/user/core/browser-pyodide.md` in the main `swan` repo) requires.

**Status (2026-08-04): `opt` builds cleanly (`opt --version` reports `LLVM version 23.0.0git`) AND
obfuscation correctness is verified.** A real annotated test function (using swan's actual
`SWAN_OBF_FN`/`SWAN_OBF_STRENC_MARKER` macros) was run through the full real pipeline (Emscripten
5.0.3 IR emission -> this `opt` -> Emscripten 5.0.3 codegen): `flattening` demonstrably
restructures control flow (basic-block/dispatcher growth), `strenc` demonstrably encrypts string
literals at rest with correct runtime `__decrypt_string` call wiring (confirmed by inspecting the
actual output IR, not the pass's own summary log -- see "A logging quirk" below), and the pipeline
produces a valid final `.wasm` object with no errors. `mba`/`substitution`/`bcf` reported zero
transformations on that specific small test function -- initially concerning, but reproduced
*identically* (same "0 operations"/"0 blocks", including the same misleading log wording) by the
already-in-production `obfuscator-llvm20` `opt` given the same input, proving it's a property of
that small test function's shape (nsw/nuw poison flags excluding most sites, the rest apparently
just not eligible), not an LLVM 23-specific regression. Not yet verified: a *real* swan extension
source file (not a toy test function) end-to-end, and x86_64 (built/tested on aarch64 for
iteration speed) -- see "Known gaps" below.

### A logging quirk (pre-existing, not introduced by this port)

`strenc`'s own verbose output prints `[strenc] done: 0 strings, 0 call sites` even when it
successfully encrypts every eligible string literal in the function -- confirmed present
identically on `obfuscator-llvm20`'s already-shipped `opt` too, so it's a pre-existing quirk in
the pass's own logging (whatever counter that message reports isn't the one that actually gates
encryption), not something this port broke. Don't trust that specific log line's literal "0" as
evidence encryption didn't happen -- check the output IR for `@.enc_str`-style globals and a
`__decrypt_string` call instead, the way this verification did.

## Why this exists, separately from `obfuscator-llvm20`

`obfuscator-llvm20` matches Emscripten 3.1.71 (the version swan's `wasm_mvp`/`wasm_eh` targets
pin). `wasm_eh_pyodide` needs Emscripten 5.0.3 specifically, to match the Pyodide 314.0.3 runtime
it ships alongside -- a materially newer Emscripten, bundling LLVM 23 instead of LLVM 20. Per
`obfuscator-llvm20`'s own README: `opt` and the compiler it feeds IR to must share exactly one
LLVM version, or the result is either a parse failure or a silently ABI-incompatible `.wasm` --
so this target needs its own `opt`, built from the same obfuscator source against a matching
LLVM 23 base, the same way `obfuscator-llvm20` does for its own target.

## Contents

- `src/include/llvm/Transforms/Obfuscator.h`, `src/include/llvm/Transforms/Obfuscator/`,
  `src/lib/Transforms/Obfuscator/` -- the obfuscator module, starting from
  `obfuscator-llvm20`'s already-adapted copy (itself vendored from
  [`maxdemarzi/ollvm`](https://github.com/maxdemarzi/ollvm) at
  [`OLLVM_SOURCE_COMMIT`](./OLLVM_SOURCE_COMMIT)) and further adapted for LLVM 23. Of the same 90
  files, only 3 needed real API adaptations (all mechanical LLVM-20-vs-23 signature/API changes --
  none touch obfuscation logic itself):
  - `Module::getTargetTriple()` returns a `Triple` object on LLVM 23, not the plain `std::string`
    LLVM 20 returns (`ObfReport.cpp`) -- fixed with an explicit `.str()`. Note this is *not* the
    same direction as `obfuscator-llvm20`'s equivalent fix: LLVM 20 is the outlier here (the
    original upstream `maxdemarzi/ollvm` source targeted LLVM 22, which -- like 23 -- already
    returns `Triple`), so this LLVM 23 code is closer to the original unadapted source than
    `obfuscator-llvm20`'s downgraded-to-`std::string` version is.
  - `Instruction::Br` no longer exists as of LLVM 23 -- unconditional and conditional branches
    were split into distinct opcodes/classes, `Instruction::UncondBr`/`UncondBrInst` and
    `Instruction::CondBr`/`CondBrInst` (both still subclasses of a common `BranchInst` base, so
    every other `BranchInst`/`isConditional()`/`getSuccessor()` call site elsewhere in this module
    is unaffected -- only the two direct `Op == Instruction::Br` opcode comparisons in
    `VMPass_Emitter.cpp` needed to become `Op == Instruction::UncondBr || Op ==
    Instruction::CondBr`).
  - `IRBuilder<>::CreateGlobalStringPtr(...)` was removed; replaced with
    `CreateGlobalString(...)` (`VMPass_Impl.cpp`, one call site). Confirmed a value-preserving,
    drop-in rename under LLVM's opaque-pointer model (already the only pointer representation
    this codebase uses) -- `CreateGlobalString`'s `GlobalVariable*` return is bit-identical to
    what `CreateGlobalStringPtr` used to produce; the old method existed only to save callers an
    explicit GEP-to-first-element in the typed-pointer era, which opaque pointers made moot.
- `integration.patch` -- byte-identical to `obfuscator-llvm20`'s copy (`PassBuilder.cpp`
  includes, `PassRegistry.def` registrations, `Transforms/CMakeLists.txt`'s `add_subdirectory`).
  Confirmed applying cleanly against `LLVM_COMMIT` with **zero conflicts**, unmodified.
- `LLVM_COMMIT` -- the exact `llvm/llvm-project` commit this is meant to be grafted onto (matches
  Emscripten 5.0.3's bundled LLVM; re-verify via `em++ --version` if swan's Pyodide-target
  Emscripten pin ever changes).
- `OLLVM_SOURCE_COMMIT` -- unchanged from `obfuscator-llvm20`; same ultimate upstream provenance.

## Known gaps

- **Only a small, standalone test function verified, not a real swan extension source file.**
  Obfuscation correctness above was proven via a real annotated test function compiled through
  the real pipeline, not by running an actual `wasm_eh_pyodide` extension build through this
  `opt` end-to-end. Next step: point `PublishWasmPyodide.yml`'s `build-extension-wasm-pyodide` job
  at this toolchain and gate on `scripts/verify_obfuscation.py` (from the main `swan` repo), the
  same way every other obfuscated target is.
- **`strenc`'s AES cipher path specifically is untested.** The verification above ran with
  `aes=0` (a non-AES cipher was used for that test's string encryption, per the pass's own
  verbose log) -- `obfuscator-llvm20` documents a known gap where its AES runtime stub
  (precompiled bitcode built against LLVM 22.1.8) fails to link under LLVM 20's `opt`, falling
  back to skipping encryption silently. Whether the same stub links cleanly under LLVM 23, and
  whether `aes=1` actually engages it correctly, has not been checked.
- **x86_64 untested locally**, though `.github/workflows/build-toolchain-wasm-llvm23.yml` (added
  alongside this update) builds on a real `ubuntu-24.04` (x86_64) runner, not Rosetta emulation --
  once that workflow has run successfully at least once, this gap is closed.

## Building

`build/Dockerfile.base` + `build/build.sh` reproduce the exact CMake invocations
`build-toolchain-wasm.yml` uses for `obfuscator-llvm20` (bootstrap clang from a clean checkout of
`LLVM_COMMIT` first, needed to compile the `strenc` pass's `aes_stub.c` at a matching LLVM
version, then this module + `integration.patch` grafted onto a second copy of the same commit,
building `opt` only via `-DAES_STUB_CLANG_OVERRIDE=<bootstrap clang path>`) -- see that
workflow's own comments for why this two-clone ordering is required (a clang built from the
patched tree would transitively depend on `aes_stub.bc`, the file it's supposed to compile).

```bash
docker build -t swan-ollvm-llvm23-base -f build/Dockerfile.base build/
docker run --rm -v "$(pwd)/build":/work -e CCACHE_DIR=/work/ccache swan-ollvm-llvm23-base bash /work/build.sh
```

Requires `LLVM_COMMIT` checked out twice under `build/llvm-src` (patched: this directory's
`src/`+`integration.patch` grafted on) and `build/llvm-src-clean` (unpatched, for the bootstrap
clang) -- not included in this repo (LLVM's full source is far too large to vendor); clone fresh
at build time.
