# Native154 source and bundled-resource notices

Historical packet for earlier references. The current [runtime manifest](../manifest.json) selects the [Native160 packet](../native160/README.md); use its declared source and archive bindings for the current reference.

This packet identifies the source-path-remapped ARM64 package `codex-darwin-arm64-0.154.0-openclaw.catalog.20261018.1.tgz`, SHA256 `c5ed17c225d73bbdbf746b34f4775ffd056c81ab7629ff91a8fd69ec268d8ff2`. The archive is distributed separately; its seven regular members and both digests are recorded in `native154-provenance.json`. The [public release](https://github.com/josephbergvinson/openclaw-control-plane/releases/download/native-codex154-catalog-20261018.1/codex-darwin-arm64-0.154.0-openclaw.catalog.20261018.1.tgz) supplies these bytes for the manifest-driven reconstruction helper, which verifies their size and both digests.

`native154-source.patch` is the complete five-file change from official Codex `rust-v0.154.0`, commit `6b9826e3aa83b1a5947db50f4332cb9c65f1b340`, to producer commit `1173583aca98e98355d3292f6e9c4226fecfdb94` and tree `fddfa85f503e260ea386d6eeda54ee100a1b27b5`. It preserves the qualified source bytes. The changes concern catalog metadata, backfill and thread query behavior, their regression cases, dependency lock data and the existing build recipe. Codex's source Apache licence and OpenAI/Ratatui notice are under `licenses/codex`.

The bundled ripgrep is the ARM64 binary selected by ripgrep15.2.0's official release, with its MIT/Unlicense choice and original COPYING statement preserved under `licenses/ripgrep`.

The bundled zsh is selected by Codex's `rust-v0.134.0-alpha.3` release. That release's actual workflow pins source commit `77045ef899e53b9598bebc5a41db93a548a40ca6` and applies the included `zsh-exec-wrapper.patch`. The original zsh licence is preserved under `licenses/zsh`. The package contains the zsh binary and contains no shell-function files.

The code-mode host uses the Codex-built sandbox V8 pair from `rusty-v8-v150.4.0`, not the default upstream prebuilt pair. Its checksum manifest, archive and binding digests identify the selected input. The release source pins V8 `15.0.245.2` with Codex's three declared patches and the recorded libc++, libc++abi and llvm-libc revisions. Notices retained here cover the Rust V8 bindings, V8 core, Strongtalk and fdlibm notices, and those three embedded LLVM runtime source owners. Exact source revisions, primary release URLs, build-job URLs and notice hashes are in the manifest.

This finite packet provides the selected resource provenance and copied upstream notices. It does not assert a complete licence inventory of every statically linked Cargo or V8 third-party dependency. It also does not establish a byte-identical rebuild; exact distribution reconstruction consumes the versioned archive and verifies its digests.

The replacement is rebuilt from the same catalog source tree using the existing builders. [REBUILD.md](REBUILD.md) records the compiler, copied-sysroot and linker path-remapping procedure. Resource and source/licence bytes are preserved. Corresponding-source tar headers use neutral ownership and omit local path metadata.
