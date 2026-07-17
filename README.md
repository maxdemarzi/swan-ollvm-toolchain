# swan-ollvm-toolchain

Builds and publishes the OLLVM-hardened LLVM/Clang/LLD toolchain used to compile the
[Swan](https://github.com/RelationalAI/swan) DuckDB extension's hardened release build.

This repo intentionally contains no Swan source code -- it's purely build/publish
infrastructure for the compiler toolchain itself, pinned against a mirror of
[und3ath/ollvm](https://github.com/und3ath/ollvm) (an in-tree LLVM 22 fork adding
source-annotation-driven obfuscation passes) at
[maxdemarzi/ollvm](https://github.com/maxdemarzi/ollvm).

## Why a mirror + a pin

`und3ath/ollvm` is a single-maintainer, young (created 2026-04-15) project. Mirroring it
here and building only from a pinned commit SHA (recorded in `OLLVM_COMMIT`, never a
floating branch) is cheap insurance against upstream disappearing or force-pushing history
out from under a commercial release pipeline.

## Architecture coverage

Only Windows has an official prebuilt binary from upstream
(`ollvm22-windows-Release.7z`, built natively via MSVC 2022) -- confirmed by reading
`und3ath/ollvm`'s own `.github/workflows/ollvm-build.yml`, whose build matrix is only
`windows-2022` and `ubuntu-24.04`; no macOS leg exists or ever existed there. The upstream
Linux prebuilt is built on plain `ubuntu-24.04` (glibc ~2.39), which will not run inside
Swan's actual Linux build environment (a `manylinux_2_28` container, glibc ~2.28) --
glibc symbol versioning isn't backward-compatible. So:

| Target | Source |
|---|---|
| `windows_amd64` | Downloaded directly from `und3ath/ollvm`'s own pinned GitHub Release tag (never `latest`), re-published here with a SHA256 checksum. `gh attestation verify` isn't applicable -- `und3ath/ollvm`'s actual release-producing workflow doesn't generate attestations for these assets |
| `linux_amd64` | Built from source in this repo, inside `quay.io/pypa/manylinux_2_28_x86_64` (matches Swan's actual compile environment for glibc/ABI compatibility) |
| `linux_arm64` | Built from source in this repo, inside `quay.io/pypa/manylinux_2_28_aarch64`, natively on an arm64 runner |
| `osx_amd64` + `osx_arm64` | Built **once**, natively, on `macos-15` (Apple Silicon) with both `X86` and `AArch64` LLVM backends enabled -- Swan's own CI already cross-builds `osx_amd64` from the same arm64 host via `-arch x86_64`, so one universal-capable toolchain build serves both targets |

## Usage

Trigger the `Build OLLVM Toolchain` workflow manually (`workflow_dispatch`) with a
`release_tag` input (e.g. `toolchain-v1`). It builds/fetches all four artifacts, generates
SHA256 checksums and a GitHub Artifact Attestation, and publishes everything as a GitHub
Release under that tag. Swan's own CI (see the `extension-ci-tools` fork at
[maxdemarzi/extension-ci-tools](https://github.com/maxdemarzi/extension-ci-tools)) downloads
and verifies these released artifacts rather than rebuilding LLVM on every extension build.

To bump the pinned `und3ath/ollvm` commit: update `OLLVM_COMMIT` in this repo (pulling the
new commit into the `maxdemarzi/ollvm` mirror first if needed), then re-run the workflow
with a new `release_tag`.
