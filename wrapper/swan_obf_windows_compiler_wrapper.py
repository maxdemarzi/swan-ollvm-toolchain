#!/usr/bin/env python3
"""Drop-in clang-cl replacement (via CMAKE_C(XX)_COMPILER_LAUNCHER) that runs
the OLLVM "obfuscation" module pass between IR emission and codegen, for the
Windows (clang-cl frontend) build. Windows sibling of
swan_obf_compiler_wrapper.py -- same 3-step dance, but clang-cl's MSVC-style
command-line syntax needs its own argument handling throughout:

    clang-cl /c <src> /Fo<obj>              (a normal compile, MSVC syntax)
 -> clang-cl /clang:-S /clang:-emit-llvm
            -Xclang -disable-llvm-passes
            /clang:-o /clang:<ir path> <src> (emit un-obfuscated IR)
    opt -passes=obfuscation ...              (run the obfuscation pass)
    clang-cl /c <obf ir> /Fo<obj>            (real optimization + codegen)

Confirmed directly (this session, real clang-cl --driver-mode=cl targeting
x86_64-pc-windows-msvc, run on Linux -- clang-cl is a flag-parsing frontend,
not something that needs to actually run on Windows to cross-compile for it):

  1. clang-cl's own parser does NOT understand bare GNU-style -S/-emit-llvm
     at all -- silently ignored with a "-Wunknown-argument" warning, not an
     error, so a naive swap-in from the GNU wrapper would silently produce
     an ordinary .obj instead of IR and the whole 3-step dance would no-op.
     The real mechanism is /clang:<flag>, clang-cl's own documented
     passthrough that forwards a single flag straight to the underlying
     clang cc1 driver, bypassing the CL-style parser entirely for that flag.
     -Xclang itself IS recognized directly by clang-cl's own parser (no
     /clang: prefix needed for it specifically) since it's such a common
     passthrough case, but -S/-emit-llvm are not.

  2. /Fo<path> (MSVC's output-path flag, no space before the path) has no
     effect at all once /clang:-emit-llvm is active -- confirmed directly:
     clang-cl silently ignores it and defaults to
     "<source-basename-without-ext>.ll" in the current directory instead.
     The only mechanism that actually controls the path in that mode is
     /clang:-o /clang:<path> (two separate /clang: tokens, one per word of
     "-o <path>" -- a single "/clang:-o <path>" does NOT work, the path
     itself also needs its own /clang: prefix).

  3. Step 3 (compiling the obfuscated .ll back into a real .obj) works with
     an entirely ordinary /c /Fo<path> invocation -- clang-cl accepts a .ll
     file as a source argument the same as a native compiler would, no
     special-casing needed there.
"""
import os
import re
import subprocess
import sys
import tempfile

SOURCE_EXTS = (".cpp", ".cc", ".cxx", ".c++", ".c", ".C", ".cppm")


def find_compile_invocation(args):
    """Returns (source_index, fo_index) if this is a single-source
    compile-to-object invocation (MSVC /c /Fo<path> syntax), else None.
    Deliberately conservative -- anything ambiguous (linking, --version
    probing, an explicit /clang:-S / -E requested by the caller, multiple
    sources) falls through to a plain passthrough call, mirroring
    swan_obf_compiler_wrapper.py.

    Accepts both /c and -c (and /E,/P and -E,-P below) -- confirmed directly
    against a real cmake+ninja+clang-cl invocation that CMake's own Clang-MSVC
    frontend module generates -c, not /c, for the compile step (clang-cl
    accepts both interchangeably; CMake just happens to prefer the GNU-style
    spelling here). Checking only /c meant this function returned None on
    every real compile, so the whole 3-step obfuscation dance silently never
    ran at all -- the same "Windows never actually obfuscated" bug, just
    hidden one layer deeper behind a wrapper that looked like it was doing
    something."""
    if "/c" not in args and "-c" not in args:
        return None
    for a in args:
        if a in ("/E", "/P", "-E", "-P") or a.startswith("/clang:-S") or a.startswith("/clang:-E"):
            return None  # caller already wants something other than an object file

    source_idx = None
    for i, a in enumerate(args):
        if a.startswith("/") or a.startswith("-"):
            continue
        if a.endswith(SOURCE_EXTS):
            if source_idx is not None:
                return None  # more than one source file -- bail, passthrough
            source_idx = i

    if source_idx is None:
        return None

    fo_idx = None
    for i, a in enumerate(args):
        if a.startswith("/Fo"):
            if fo_idx is not None:
                return None
            fo_idx = i
    if fo_idx is None:
        return None

    return source_idx, fo_idx


def run(cmd):
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        sys.exit(proc.returncode)


def main():
    # Launcher invocation: argv[1] is the real compiler CMake resolved,
    # everything after is the actual MSVC-style argument list -- matching
    # exactly how swan_obf_wasm_compiler_wrapper.py already established
    # CMAKE_C(XX)_COMPILER_LAUNCHER invokes "<launcher> <real-compiler>
    # <args...>" for every real compile.
    if len(sys.argv) < 2:
        sys.exit("swan_obf_windows_compiler_wrapper: expected the real compiler as argv[1] "
                  "(invoke this as a CMAKE_C(XX)_COMPILER_LAUNCHER, not CMAKE_C(XX)_COMPILER)")
    real = sys.argv[1]
    args = sys.argv[2:]

    invocation = find_compile_invocation(args)
    if invocation is None:
        os.execv(real, [real] + args)  # linking, --version, /E, etc.

    source_idx, fo_idx = invocation

    opt = os.environ.get("SWAN_OBF_REAL_OPT")
    if not opt:
        sys.exit("swan_obf_windows_compiler_wrapper: SWAN_OBF_REAL_OPT not set")
    seed = os.environ.get("SWAN_OBF_SEED", "42")
    verbose = os.environ.get("SWAN_OBF_VERBOSE") == "1"

    fd_ir, ir_path = tempfile.mkstemp(suffix=".ll", prefix="swan_obf_win_")
    os.close(fd_ir)
    fd_obf, obf_path = tempfile.mkstemp(suffix=".obf.ll", prefix="swan_obf_win_")
    os.close(fd_obf)

    try:
        # Step 1: emit un-obfuscated IR. Drop /c (or -c, see find_compile_invocation),
        # the original /Fo<...> token, and a bare "--" token entirely.
        # /c and /Fo<...> have no effect once /clang:-emit-llvm is active
        # (confirmed directly, see module docstring point 2), so leaving them
        # in is at best inert clutter. "--" (CMake's own "end of options,
        # everything after is positional" marker, confirmed present in a
        # real cmake-generated invocation immediately before the source
        # path) is actively harmful here if left in: this step APPENDS new
        # flags (/clang:-S etc.) after the filtered original args, and
        # clang-cl -- like any conforming "--" implementation -- treats
        # everything after an inherited "--" as a positional filename, not a
        # flag, so those appended flags would be misread as (nonexistent)
        # input files instead of being parsed as flags at all. Confirmed
        # directly: "error: no such file or directory: '/clang:-S'".
        ir_args = [a for i, a in enumerate(args) if i not in (fo_idx,) and a not in ("/c", "-c", "--")]
        ir_args += ["/clang:-S", "/clang:-emit-llvm", "-Xclang", "-disable-llvm-passes",
                    "/clang:-o", "/clang:" + ir_path]
        run([real] + ir_args)

        # Windows-only: force the legacy XOR strenc path, never AES/ChaCha,
        # by textually rewriting every bare (unqualified, AES-defaulting)
        # "strenc" request in the freshly emitted IR's obf: annotation
        # strings to "strenc(aes=0)" before opt ever sees them -- a simple,
        # reliable string edit (every swan source annotation is confirmed
        # to end in exactly this pattern: 'obf: strenc' for the file-scope
        # anchor, or 'obf: ..., strenc' for a real function, always as the
        # last comma-separated token, immediately followed by the LLVM IR
        # string literal's own null terminator and closing quote --
        # "strenc\00\"") rather than editing swan's own source annotations
        # across 100+ files, or the obfuscation pass's own source (not an
        # option here: unlike every other platform, this Windows opt/
        # clang-cl pair is a third-party und3ath/ollvm prebuilt release,
        # not something built from maxdemarzi/ollvm's own source, so there
        # is nothing of ours to patch there). Needed because this stub was
        # never built for a windows-msvc target at all -- confirmed
        # directly: linking it in fails outright with "linking module
        # flags 'wchar_size': IDs have conflicting values: 'i32 4' from
        # aes_stub.bc, and 'i32 2'" (MSVC's own 2-byte wchar_t vs. the
        # stub's 4-byte one) -- the same underlying "AES stub compiled for
        # the wrong target" class of bug already found and fixed the same
        # way (force XOR, no stub needed at all) for wasm.
        # LLVM IR string-literal globals declare their own byte length up
        # front (e.g. "[12 x i8] c\"obf: strenc\\00\"") and require it to
        # exactly match the literal's content including the null
        # terminator -- a naive content-only text replace (tried first,
        # confirmed directly) leaves the old, now-too-small length in
        # place and produces IR opt then rejects outright ("constant
        # expression type mismatch: got type '[19 x i8]' but expected
        # '[12 x i8]'"). Match and grow the array-length prefix by exactly
        # len("(aes=0)") for every "...strenc\00\"" this pattern touches.
        SUFFIX = "(aes=0)"
        # Anchored on the literal 'c"obf: ' prefix every real annotation
        # string starts with (not just "...strenc\00\"" alone) so this can
        # only ever touch a genuine obf: annotation, never an unrelated
        # ordinary string constant that happens to end in the word strenc.
        strenc_re = re.compile(r'\[(\d+) x i8\](?P<body> c"obf: [^\n"]*?strenc)\\00"')

        def grow(m):
            new_len = int(m.group(1)) + len(SUFFIX)
            return f'[{new_len} x i8]{m.group("body")}{SUFFIX}\\00"'

        with open(ir_path, "r") as f:
            ir_text = f.read()
        ir_text = strenc_re.sub(grow, ir_text)
        with open(ir_path, "w") as f:
            f.write(ir_text)

        # Step 2: run the obfuscation pass. A no-op (fast) if this TU has no
        # obf: annotations -- ObfuscationModulePass bails out immediately via
        # ObfuscationAnnotationAnalysis::hasAnyConfig(). Identical to every
        # other platform's wrapper.
        opt_cmd = [opt, "-S", "-passes=obfuscation",
                   f"-obf-seed={seed}", "-obf-deterministic",
                   ir_path, "-o", obf_path]
        if verbose:
            opt_cmd.append("-obf-verbose")
        run(opt_cmd)

        # Step 3: real codegen from the obfuscated IR, keeping every original
        # flag (opt level, target, etc.) so normal optimization still
        # applies -- clang-cl accepts a .ll file as an ordinary source
        # argument here, EXCEPT /TP or /TC (force-C++/force-C mode, which
        # CMake's clang-cl frontend always adds for a real compile -- e.g.
        # "-TP") must be dropped: confirmed directly, that flag overrides
        # clang-cl's normal extension-based file-type detection, so with it
        # still present clang-cl tried to parse the obfuscated IR text as
        # C++ source instead of recognizing the .ll extension ("error: a
        # type specifier is required for all declarations" on the IR's own
        # "; ModuleID = ..." header comment). Dropping it is safe -- the
        # language is now unambiguous from the .ll extension itself.
        final_args = [obf_path if i == source_idx else a
                      for i, a in enumerate(args) if a not in ("/TP", "/TC", "-TP", "-TC")]
        run([real] + final_args)
    finally:
        for p in (ir_path, obf_path):
            try:
                os.remove(p)
            except OSError:
                pass


if __name__ == "__main__":
    main()
