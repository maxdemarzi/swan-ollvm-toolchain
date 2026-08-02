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
import shutil
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


def find_system_libstdcxx():
    """Locate the libstdc++ matching whatever system C++ compiler built this
    environment's vcpkg dependencies (e.g. libhighs.a), via -print-file-name
    -- the standard GCC/Clang driver mechanism for "where would you look for
    this file", using that compiler's own internal search paths rather than
    guessing a distro-specific location. Returns (dir, path) or None.

    Why this exists, not just a bare -lstdc++: manylinux_2_28 (the actual
    image extension-ci-tools' Linux Docker builds use) ships ONLY the
    versioned runtime object (/usr/lib64/libstdc++.so.6) -- there is no
    "-dev"-package-style bare libstdc++.so symlink anywhere on the default
    linker search path, so a plain -lstdc++ fails outright ("cannot find
    -lstdc++"). Confirmed directly (real manylinux_2_28 container):
    `find / -iname libstdc++.so` finds nothing outside a gcc-toolset's own
    private lib dir. Worse, that system .so.6 itself turned out to be too
    OLD to fix this by itself even when referenced by exact filename
    (-l:libstdc++.so.6): it lacks the std::__throw_bad_array_new_length()
    symbol libhighs.a's real error needs (only the lower-level, differently
    -mangled __cxa_throw_bad_array_new_length from the C++ ABI runtime is
    present) -- confirmed via `nm -D`. That symbol only exists in the SAME
    gcc-toolset's own newer libstdc++ (here, gcc-toolset-14, matching
    "Compiler found: /opt/rh/gcc-toolset-14/root/usr/bin/c++" in the actual
    CMake configure log for vcpkg's own dependency builds) -- reachable at
    its OWN private path via c++/g++'s -print-file-name, not any general
    system location. That path resolves to a GNU ld linker script (not a
    real .so), which itself pulls in both the real versioned .so.6 AND
    -lstdc++_nonshared -- confirmed directly that both the script's own
    directory needs to be on the -L search path (for its internal
    -lstdc++_nonshared reference to resolve) AND the script's own full path
    must be passed as a direct link input (bare -lstdc++ against this same
    -L would resolve to the same OLD system .so.6 first via normal search
    order, missing the symbol again) -- verified end-to-end with a real
    compile+link+run reproducing libhighs.a's exact failure, both without
    this fix (fails identically) and with it (links and runs correctly).
    """
    for cxx in ("c++", "g++"):
        path = shutil.which(cxx)
        if not path:
            continue
        try:
            # stdout=/stderr=PIPE, not capture_output=True: this must run
            # under whatever python3 a minimal build image ships (confirmed
            # directly: manylinux_2_28's is 3.6, predating capture_output).
            proc = subprocess.run(
                [cxx, "-print-file-name=libstdc++.so"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, timeout=10,
            )
            out = proc.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        # GCC/Clang's own convention: -print-file-name echoes the query
        # back unchanged if it has no idea where the file might be.
        if not out or out == "libstdc++.so" or not os.path.isfile(out):
            continue
        return os.path.dirname(out), out
    return None


def extra_link_args(real, kind):
    """-lm always, plus -L<install>/lib and a C++ runtime link for kind=="cxx".

    Three independent problems, all traced to renaming clang/clang++ to
    clang.real/clang++.real (see wrap_toolchain.sh):

    1. clang's default-config-file auto-discovery (clang.cfg/clang++.cfg,
       see clang/docs/UsersManual.md) is keyed off the *invoked*
       executable's exact basename, and empirically does not follow
       through to a renamed binary the way its docs suggest it should --
       so -L<install>/lib is injected explicitly here instead.

    2. clang's driver implicitly adds the C++ runtime (-lc++ on Darwin,
       -lstdc++ on Linux) when it recognizes it's linking C++ -- but that
       recognition is name-based (the invoked executable's own basename
       containing "clang++"), not e.g. based on object file content, and
       "clang++.real" doesn't match whatever pattern the driver actually
       checks for. Linking a .o file (as opposed to compiling a .cpp file
       straight through, where the .cpp extension alone is enough to pick
       the C++ frontend) through "clang++.real" produced a link with
       *zero* C++ runtime symbols resolved at all (undefined
       __gxx_personality_v0/__cxa_throw and missing basic_string/
       exception internals on Linux, the Darwin-mangled equivalents on
       macOS) even though the object file itself was compiled correctly
       -- adding -L alone did not fix it. Darwin's fix is the
       already-bundled static -lc++abi -lc++. Linux's real fix is NOT a
       bare -lstdc++ -- see find_system_libstdcxx()'s own docstring for
       why a plain -lstdc++ silently fails (or worse, silently resolves
       to a too-old libstdc++ missing a symbol a vcpkg dependency like
       libhighs.a actually needs) on the real manylinux_2_28 build image,
       and bare -lstdc++ is kept only as a last-resort fallback when no
       system compiler is available to ask.

    3. The same renamed-basename problem also drops libm off the default
       link line entirely, for BOTH clang.real and clang++.real -- unlike
       problem 2, this is not gated behind kind=="cxx". Confirmed
       directly: a real swan build failed linking a plain math-using
       translation unit with "undefined reference to symbol
       'acos@@GLIBC_2.2.5'" / "libm.so.6: error adding symbols: DSO
       missing from command line", and a minimal repro reproduced the
       identical failure for both the C and C++ cases -- so the earlier
       "-L alone is still correct/sufficient for the C case" assumption
       here was wrong. -lm is placed last (after the C++ runtime, when
       present) since libstdc++/libc++ can themselves reference libm
       symbols, and ld resolves left-to-right.
    """
    libdir = os.path.normpath(os.path.join(os.path.dirname(real), "..", "lib"))
    args = ["-L", libdir] if os.path.isdir(libdir) else []
    if kind == "cxx":
        if os.path.isfile(os.path.join(libdir, "libc++.a")):
            args += ["-lc++abi", "-lc++"]
        else:
            found = find_system_libstdcxx()
            if found:
                stdcxx_dir, stdcxx_path = found
                args += ["-L", stdcxx_dir, stdcxx_path]
            else:
                args += ["-lstdc++"]
    args += ["-lm"]
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
