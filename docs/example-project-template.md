# Example Project Template

The public control plane includes a generic `example-project-*` surface to show how a project can be incorporated without baking in any one operator's private project structure.

Use it as a pattern for your own project:

- `example-project-runtime` — canonical source/runtime repo
- `example-project-exporter` — optional companion/exporter component
- `example-project-docs` — optional public/internal docs surface
- `<PROJECT_HANDOFF>` — optional operator-facing handoff copy

For each real project, define:

1. canonical source root
2. standing branch
3. local authoritative remote, if any
4. mirror/backup/handoff paths, if any
5. validation entrypoint
6. write/mutation approval gates
7. cleanup and closeout rules

Do not copy the placeholder project names literally unless you are building a toy example. Replace them with your own project slugs and probes.
