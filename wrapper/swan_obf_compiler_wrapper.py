#!/usr/bin/env python3
"""Drop-in clang/clang++ replacement that runs the OLLVM "obfuscation" module
pass between IR emission and codegen.

Why this exists: the custom OLLVM toolchain (github.com/maxdemarzi/ollvm,
mirroring und3ath/ollvm) only exposes its obfuscation pass to the standalone
`opt` tool's `-passes=` textual pipeline (see PassRegistry.def:
MODULE_PASS("obfuscation", ObfuscationModulePass())) -- there is no working
single-invocation clang flag for it, despite upstream's own README showing
`clang -mllvm -passes=obfuscation ...` as if it were one (verified: this
toolchain's clang hard-rejects that flag as an unrecognized -mllvm option).
The only mechanism that actually works is the 3-step pipeline upstream's own
README documents as the `opt`-based alternative:

    clang -S -emit-llvm -O0 ...     (emit un-obfuscated IR)
    opt -passes=obfuscation ...     (run the obfuscation pass)
    clang -O2 -c obf.ll -o out.o    (real optimization + codegen)

This script makes that transparent to CMake/Ninja: point CMAKE_C_COMPILER /
CMAKE_CXX_COMPILER at this script (via two thin sibling entry points, see
swan_obf_clang / swan_obf_clang++) instead of the raw compiler binary. Only
an actual "compile one source file to one object file" invocation (-c, one
source, -o) goes through the 3-step dance; everything else (linking,
--version probing for CMake's compiler-id detection, -E, -S requested by the
caller, multiple sources) passes straight through to the real compiler
unmodified.
"""
import os
import subprocess
import sys
import tempfile

SOURCE_EXTS = (".cpp", ".cc", ".cxx", ".c++", ".c", ".C", ".cppm")


def real_compiler():
    kind = os.environ.get("SWAN_OBF_KIND")
    if kind == "cxx":
        path = os.environ.get("SWAN_OBF_REAL_CXX")
    elif kind == "cc":
        path = os.environ.get("SWAN_OBF_REAL_CC")
    else:
        sys.exit("swan_obf_compiler_wrapper: SWAN_OBF_KIND must be 'cc' or 'cxx'")
    if not path:
        sys.exit("swan_obf_compiler_wrapper: real compiler path not set "
                  "(SWAN_OBF_REAL_CXX/SWAN_OBF_REAL_CC)")
    return path


def find_compile_invocation(args):
    """Returns (source_index, output_index) if this is a single-source
    compile-to-object invocation, else None. Deliberately conservative --
    anything ambiguous falls through to a plain passthrough call."""
    if "-c" not in args:
        return None
    if "-E" in args or "-S" in args or "-emit-llvm" in args:
        return None  # caller already wants something other than an object file

    source_idx = None
    for i, a in enumerate(args):
        if a.startswith("-"):
            continue
        if a.endswith(SOURCE_EXTS):
            if source_idx is not None:
                return None  # more than one source file -- bail, passthrough
            source_idx = i

    if source_idx is None:
        return None

    try:
        output_idx = args.index("-o") + 1
    except ValueError:
        return None

    return source_idx, output_idx


def run(cmd):
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        sys.exit(proc.returncode)


def extra_link_args(real, kind):
    """-L<install>/lib plus, for C++, explicit -lc++abi -lc++.

    Two independent problems, both traced to renaming clang++ to
    clang++.real (see wrap_toolchain.sh):

    1. clang's default-config-file auto-discovery (clang.cfg/clang++.cfg,
       see clang/docs/UsersManual.md) is keyed off the *invoked*
       executable's exact basename, and empirically does not follow
       through to a renamed binary the way its docs suggest it should --
       so -L<install>/lib is injected explicitly here instead.

    2. Apple's Darwin driver implicitly adds -lc++ (and transitively
       libc++abi) when it recognizes it's linking C++ -- but that
       recognition also appears to be name-based (argv[0] containing
       "clang++"), not e.g. based on object file content. Linking a .o
       file (as opposed to compiling a .cpp file straight through)
       through "clang++.real" produced a link with *zero* C++ runtime
       symbols resolved at all (undefined ___gxx_personality_v0,
       ___cxa_throw, vtable/typeinfo for the thrown type, etc.) even
       though the object file itself was compiled correctly -- adding -L
       alone did not fix it. Confirmed directly (bypassing this wrapper
       entirely) that -lc++abi -lc++ resolves it completely. -L alone is
       still correct/sufficient for the C case, which has no such
       implicit-runtime-linking behavior to lose.
    """
    libdir = os.path.normpath(os.path.join(os.path.dirname(real), "..", "lib"))
    if not os.path.isdir(libdir):
        return []
    args = ["-L", libdir]
    if kind == "cxx" and os.path.isfile(os.path.join(libdir, "libc++.a")):
        args += ["-lc++abi", "-lc++"]
    return args


def main():
    args = sys.argv[1:]
    kind = os.environ.get("SWAN_OBF_KIND")
    real = real_compiler()
    link_args = extra_link_args(real, kind)

    invocation = find_compile_invocation(args)
    if invocation is None:
        os.execv(real, [real] + args + link_args)  # linking, --version, -E/-S, etc.

    source_idx, output_idx = invocation
    source_file = args[source_idx]
    final_output = args[output_idx]

    opt = os.environ.get("SWAN_OBF_REAL_OPT")
    if not opt:
        sys.exit("swan_obf_compiler_wrapper: SWAN_OBF_REAL_OPT not set")
    seed = os.environ.get("SWAN_OBF_SEED", "42")
    verbose = os.environ.get("SWAN_OBF_VERBOSE") == "1"

    fd_ir, ir_path = tempfile.mkstemp(suffix=".ll", prefix="swan_obf_")
    os.close(fd_ir)
    fd_obf, obf_path = tempfile.mkstemp(suffix=".obf.ll", prefix="swan_obf_")
    os.close(fd_obf)

    try:
        # Step 1: emit un-obfuscated IR (mirrors upstream's own recipe of
        # emitting at a minimal optimization level so the *final* compile in
        # step 3 does the real optimization work on top of obfuscated code).
        ir_args = list(args)
        ir_args[source_idx] = source_file
        ir_args[args.index("-c")] = "-S"
        ir_args += ["-emit-llvm", "-Xclang", "-disable-llvm-passes"]
        ir_args[output_idx] = ir_path
        run([real] + ir_args)

        # Step 2: run the obfuscation pass. A no-op (fast) if this TU has no
        # obf: annotations -- ObfuscationModulePass bails out immediately via
        # ObfuscationAnnotationAnalysis::hasAnyConfig().
        opt_cmd = [opt, "-S", "-passes=obfuscation",
                   f"-obf-seed={seed}", "-obf-deterministic",
                   ir_path, "-o", obf_path]
        if verbose:
            opt_cmd.append("-obf-verbose")
        run(opt_cmd)

        # Step 3: real codegen from the obfuscated IR, keeping every original
        # flag (opt level, target, etc.) so normal optimization still applies.
        final_args = list(args)
        final_args[source_idx] = obf_path
        final_args[output_idx] = final_output
        run([real] + final_args)
    finally:
        for p in (ir_path, obf_path):
            try:
                os.remove(p)
            except OSError:
                pass


if __name__ == "__main__":
    main()
