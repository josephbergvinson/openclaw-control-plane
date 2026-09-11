# Contributing

This repository includes runtime source changes, workspace helpers, policy templates,
configuration and architecture documentation. Keep each layer grounded in the code
that owns its behavior.

## Principles

- Preserve substantial procedures while using consistent example identities and
  adopter-supplied paths and account references.
- Distinguish policy, helper implementation, native runtime behavior and live proof.
  Tests and source reconstruction do not establish another operator's account or host.
- Change contracts and their callers, configuration, tests and documentation together.
- Keep one owner for shared settings and lifecycle actions. Remove replaced mechanisms.
- Use fixture directories and mocked host/provider boundaries for destructive helper
  tests. A documentation or export test must not clean up or reconfigure its host.
- Keep credentials, private narratives, runtime histories and operational receipts out
  of this public tree. Review candidate contents before publication. Do not put private
  scan terms or redaction maps in the repository.

## Style and checks

Write clear prose with one title per chapter. Explain concrete behavior and evidence.
Relative links must resolve, and Mermaid diagrams must render.

```bash
python3 scripts/validate_repo.py
python3 -m unittest discover -s tests -p 'test_*.py'
git diff --check
```

Structural checks verify the documentation and examples. The reconstruction helper has
its own isolated Git tests. Workspace tests exercise the exported helper contracts.
Runtime verification follows the pinned source's native commands. Run the checks that
cover the change and retain their actual outcomes.

[GitHub Actions](https://github.com/josephbergvinson/openclaw-control-plane/actions/workflows/verify-reference.yml) runs the repository checks
and reconstructs and builds the pinned runtime in a separate Ubuntu checkout.
It accepts manual dispatch and runs on pull requests, main, and reference rebuild
branches. Standard hosted runners are free for public repositories; a private fork
uses the owner's Actions allowance. See [GitHub's runner documentation](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
No operator credentials or private preservation data belong in that workflow.
