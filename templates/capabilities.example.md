# capabilities.example.md — Generated Capability/Status View Example

Role boundary:
- Generated human-readable capability/status view.
- Use structured registry/status/control JSON as machine-readable source of truth.
- Do not treat this file as normative policy.

## Source surfaces

### control-plane
- Canonical source root: `<OPENCLAW_WORKSPACE>`
- Standing branch: `main`
- Role: host control-plane repo, policy stack, helper scripts, and local operating contracts.
- Validation entrypoint: `python3 -m pytest -q`

### example-project-runtime
- Canonical source root: `<HOME>/<LOCAL_SOURCE_ROOT>/repos/example-project-runtime`
- Standing branch: `main`
- Local authoritative bare origin: `<HOME>/<LOCAL_SOURCE_ROOT>/remotes/example-project-runtime.git`
- Validation entrypoint: `bash scripts/example_project_ci.sh`

### example-project-exporter
- Canonical source root: `<HOME>/<LOCAL_SOURCE_ROOT>/repos/example-project-exporter`
- Standing branch: `main`
- Local authoritative bare origin: `<HOME>/<LOCAL_SOURCE_ROOT>/remotes/example-project-exporter.git`
- Operator handoff path: `<OPERATOR_HANDOFF_ROOT>/<PROJECT_HANDOFF>`
- Validation entrypoint: `bash scripts/example_project_validate.sh`

## Capability probes

| Capability | Status | Lane | Freshness | Evidence | Reprobe rule |
|---|---|---|---|---|---|
| Gateway loopback | ready | built-in | volatile | `openclaw gateway probe` succeeds | Reprobe after restart/update/network changes |
| Discord channel | ready | built-in | volatile | `openclaw channels status --probe` reports connected | Reprobe before live channel mutation |
| Telegram channel | ready | built-in | volatile | `openclaw channels status --probe` reports connected | Reprobe before live channel mutation |
| Local memory search | ready | built-in | volatile | `openclaw memory status --deep` reports embeddings/vector/FTS ready | Reprobe after memory config changes |
| Example project local API | ready | local helper | volatile | `curl http://127.0.0.1:<PORT>/healthz` succeeds | Reprobe before incident response/write-path claims |
| Example project public API | unknown | external | stable | Replace with your project probe | Reprobe before public-health claims |

## Notes

- Replace the example project surfaces with your own project/component boundaries.
- Keep canonical source roots, handoff paths, mirrors, and backups distinct.
- If a generated view conflicts with live registry/status evidence, live evidence wins.
