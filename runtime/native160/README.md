# Native160 source and bundled-resource notices

This packet identifies the ARM64 package `codex-darwin-arm64-0.160.0-openclaw.catalog.20261018.2.tgz`, SHA-256
`80e6f41a5ee5e923c71ae3c9fd86d0865a50527c19d098246d0bb7d6f2b6bb42`. Its seven regular members and both digests are
recorded in `native160-provenance.json`. The archive is distributed separately;
its manifest URL identifies the versioned release input after publication.
No package is installed by this packet.

`native160-source.patch` is the complete 35-file change from official Codex
`rust-v0.160.0`, commit `a956835d020762cb2b570053af06f643a11c0ecc`, to producer
commit `b9e328436357d8931fa10bbcfd405b8c16497bf2` and tree `7015cd9b756f88a9abb8fe180d9e38f075b3d0cc`. It ports the
transactional ordered catalog/backfill and checkpoint repair, the caller's
no-false-completion fence and regression fixtures. It retains the official160
thread upsert SQL byte-for-byte. The added native source-preserving turn contract emits model-owned complete
request pressure and sticky effect facts through the existing inference route,
retains original input through HTTP and WebSocket continuations, and fails
post-effect overflow without false completion or replay. A generic explicit
private-copy qualification target uses the existing native store migrators;
it adds no shipping migration engine. Existing test fixtures are hermetic on
space-containing paths. The normal dependency owner generated the lock data.
Codex's Apache licence and OpenAI/Ratatui notice are under `licenses/codex`.

The bundled ripgrep is the ARM64 binary selected by ripgrep15.2.0's official
release. Its MIT/Unlicense choice and original COPYING statement are preserved
under `licenses/ripgrep`.

The bundled zsh is the unchanged resource selected by official Codex160 from
`rust-v0.134.0-alpha.3`. That resource release's workflow pins zsh source commit
`77045ef899e53b9598bebc5a41db93a548a40ca6` and applies the included
`zsh-exec-wrapper.patch`. The original zsh licence is under `licenses/zsh`.
No shell-function files are included. This resource pin does not change the
stable OpenClaw or Codex target to a beta release.

The code-mode host uses the Codex-built sandbox V8 pair from `rusty-v8-v150.4.0`.
The archive and binding digests identify the selected input. That release pins
V8 `15.0.245.2`, three declared Codex source patches, and the recorded libc++,
libc++abi and llvm-libc revisions. Retained notices cover the Rust V8 bindings,
V8 core, Strongtalk, fdlibm and those embedded LLVM runtime owners. Exact source
revisions, primary release/build-job URLs and notice hashes are in the manifest.

This is finite selected-resource provenance and copied upstream notices; it is
not a complete licence inventory of every statically linked Cargo or V8
third-party dependency. A byte-identical rebuild is not established. Exact
archive reconstruction uses the versioned bytes and verifies their digests.
[REBUILD.md](REBUILD.md) describes the existing builders and source/dependency/
linker path remapping. Both final archives use neutral metadata and have been
scanned across all decompressed regular-member bytes for private paths/data.

The package passed both custom executable signature checks, canonical member/
resource identity checks, four public CLI discovery commands, and the source's
confined CLI app-server/local-fake-provider/code-mode/bundled-ripgrep smoke.
That is the CLI package's app-server mode; no separately built app-server
variant is claimed. The affected-owner suite and narrow fixture recovery establish source-level
coverage; one unrelated upstream daemon-default assertion remains diagnosed.
Actual packaged binary/OpenClaw host budget composition, copied-state migration,
matched-state rollback, runtime integration and ordinary-use acceptance remain
independent activation gates.
