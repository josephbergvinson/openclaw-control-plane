# Rebuilding the ARM64 native package

This recipe uses the existing Codex package builders. It preserves the catalog
source changes and selected resources, while remapping local source, dependency
and linker paths. It does not promise a byte-identical rebuild. The versioned
public archive and its recorded digests remain the exact distribution input.

Use macOS ARM64, Rust/Cargo 1.95.0, Python 3.13 and an installed, coherent Xcode
compiler/SDK. The reference build used Apple clang 21 and the macOS 27 SDK. Use
a fresh build directory and an ordinary dependency cache containing the locked
dependencies. Avoid concurrent heavyweight builds on a memory-constrained host.

## Source and selected inputs

Clone `https://github.com/openai/codex.git` at `rust-v0.154.0`, verify commit
`6b9826e3aa83b1a5947db50f4332cb9c65f1b340`, and apply the adjacent
`native154-source.patch`. Stage the five changed paths and verify that
`git write-tree` returns `fddfa85f503e260ea386d6eeda54ee100a1b27b5`.

Use the ripgrep and zsh binaries identified in `native154-provenance.json`.
Verify their listed hashes before passing them to the builder. Fetch the
Codex-built `rusty-v8-v150.4.0` sandbox/ptrcomp release archive and matching
binding from the manifest's public URLs and verify their hashes. The default
upstream V8 archive is a different input.

A fresh source builder can populate an ordinary Cargo cache from public locked
dependencies, before enabling the isolated/offline build environment:

```sh
cd codex-rs
cargo fetch --locked --target aarch64-apple-darwin
cd ..
```

Copy only that cache's `registry/` and `git/` data into the fresh build's
`cargo-home/`; omit credentials and configuration. No operator cache is required.
Using the published native archive through the reconstruction helper needs no
Cargo cache or Rust build at all.

## Build environment

Keep the patched source, target output, copied Cargo registry/git cache, selected
V8 inputs, temporary directory and a fresh home/cache directory under one build
root. Copy the target standard library directory
`$(rustc --print sysroot)/lib/rustlib/aarch64-apple-darwin` to
`BUILD_ROOT/sysroot/lib/rustlib/aarch64-apple-darwin`, retaining every byte.
Use the existing compiler binaries; the copied target sysroot ensures that
Apple linker object paths also lie under the remapped root. Do not copy Cargo
credentials or global Git/provider configuration into the build environment.

Set the normal `CODEX_REPO_ROOT`, `CARGO_HOME`, `CARGO_TARGET_DIR`,
`RUSTUP_TOOLCHAIN=1.95.0`, `CARGO_BUILD_JOBS=1`, `CARGO_INCREMENTAL=0` and
`CARGO_NET_OFFLINE=true`. Bind `DEVELOPER_DIR`, `SDKROOT`, `CC`, `CXX` and
`CARGO_TARGET_AARCH64_APPLE_DARWIN_LINKER` to the same installed Xcode toolchain.
Set `RUSTY_V8_ARCHIVE` and `RUSTY_V8_SRC_BINDING_PATH` to the two verified inputs.
Set the child process's home, TMPDIR, XDG and npm cache/config locations to the
fresh build directories. Pass an explicit environment rather than inheriting
account tokens or application state.

The following Python fragment constructs the supported compiler variables. Its
`Path` arguments are the actual build directories; the example names describe
their roles. ASCII unit separators preserve Rust arguments containing spaces;
`cc` requires its shell-escaped parsing option for the C/C++ arguments.

```python
import shlex
from pathlib import Path

build_root = Path("/absolute/path/to/fresh-build").resolve()
source_root = build_root / "source"
cargo_cache = build_root / "cargo-home"
target_root = build_root / "target"
copied_sysroot = build_root / "sysroot"

mapping = [
    (Path.home(), "/home/builder"),
    (build_root, "/build"),
    (source_root, "/codex"),
    (cargo_cache, "/cargo"),
    (target_root, "/target"),
]
rust_flags = [f"--remap-path-prefix={a}={b}" for a, b in mapping]
rust_flags += ["-C", f"link-arg=-Wl,-oso_prefix,{build_root}",
               f"--sysroot={copied_sysroot}"]
compiler_env = {
    "CARGO_ENCODED_RUSTFLAGS": "\x1f".join(rust_flags),
    "CC_SHELL_ESCAPED_FLAGS": "1",
}
c_flags = [f"-{kind}-prefix-map={a}={b}"
           for kind in ("ffile", "fdebug") for a, b in mapping]
compiler_env["CFLAGS"] = compiler_env["CXXFLAGS"] = shlex.join(c_flags)
# Merge compiler_env into the explicit, bounded build environment above.
```

For additional source inputs outside the common build root, append their
specific prefix mappings after the broader mappings. Preserve any existing
required compiler flags. Use `cargo metadata --offline --locked --no-deps` as a
lightweight preflight and verify that `Cargo.lock` is unchanged after the build.

## Existing build and packaging owners

Set the shell variables `BUILD_ROOT`, `VERIFIED_RG` and `VERIFIED_ZSH` to the
prepared locations above. From the patched Codex source, run with the bounded
environment:

```sh
python3 scripts/build_codex_package.py \
  --target aarch64-apple-darwin --variant codex --cargo-profile release \
  --package-version 0.154.0-openclaw.catalog.20261018.1 \
  --package-dir "$BUILD_ROOT/qualified-package" \
  --rg-bin "$VERIFIED_RG" --zsh-bin "$VERIFIED_ZSH"
```

Copy the complete package, preserving executable modes, to
`BUILD_ROOT/vendor/aarch64-apple-darwin`. Then run:

```sh
python3 codex-cli/scripts/build_npm_package.py \
  --package codex-darwin-arm64 \
  --version 0.154.0-openclaw.catalog.20261018.1 \
  --vendor-src "$BUILD_ROOT/vendor" --staging-dir "$BUILD_ROOT/npm-stage" \
  --pack-output "$BUILD_ROOT/codex-darwin-arm64-0.154.0-openclaw.catalog.20261018.1.tgz"
```

The npm package owner appends `-darwin-arm64` once to this base version. The
release profile stays unchanged, including thin LTO, line tables, no
stripping and four codegen units. Verify both custom executables' signatures,
the four public CLI discovery commands, and a confined local app-server/code-mode
smoke using the source's package-smoke fixtures. Verify the seven archive members,
resource hashes and unchanged source/lock. Finally scan all decompressed regular
member bytes for local build paths and sensitive data. Compiler flags alone do
not prove the resulting archive is suitable for publication; generated strings
and linker inputs can bypass source-path remapping. Do not repair such findings
by editing binary bytes or assuming stripping removed them.

For the corresponding source/notices archive, preserve all source and licence
bytes and use a generic `native154/` member prefix. Normalize tar uid/gid to zero,
owner/group names to empty strings, and timestamps to a fixed value. Omit local
PAX attributes and gzip filename/comment fields. Inspect these metadata headers
in both final archives as part of the same content acceptance.
