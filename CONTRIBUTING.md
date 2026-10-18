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
node --test tests/native-reference-checks.test.mjs
git diff --check
```

Structural checks verify the documentation and examples. The reconstruction helper has
its own isolated Git tests. Workspace tests exercise the exported helper contracts.
Runtime verification follows the pinned source's native commands. Run the checks that
cover the change and retain their actual outcomes.

## Workspace behavioral fixtures

The root tests do not replace the exported helpers' behavioral suite. From this
reference repository, create a separate test environment and run its existing
fixture runner:

```bash
python3 -m venv /path/to/openclaw-reference-test-env
/path/to/openclaw-reference-test-env/bin/python -m pip install \
  -r workspace/dependencies/requirements.txt pytest
/path/to/openclaw-reference-test-env/bin/python workspace/tests/run_periphery_tests.py
```

Node.js is also required for launcher fixture children. The runner replaces
inherited operator/state bindings with a disposable contract and denies live
provider/service executables. Tests exercise classification, controlled fixture
deletion, archives, process receipts, account-health diagnostics and related
helper contracts. They do not need operator credentials or permission to clean
the host, install launchd services or access real accounts. Keep this runner's
confinement when adding tests; do not run host maintenance as a test substitute.

## Publish a reviewed runtime reference

Take the intended source identities from the independently reviewed source and
distribution checkouts, rather than reading the expected values back from the
manifest being checked:

```bash
expected_source=$(git -C /path/to/reviewed-runtime-source rev-parse HEAD)
expected_distribution=$(git -C /path/to/reviewed-runtime-distribution rev-parse HEAD)
python3 scripts/validate_repo.py \
  --expected-source-commit "$expected_source" \
  --expected-distribution-commit "$expected_distribution"
```

This detects a self-consistent but stale publication. The ordinary newcomer
checks above remain flag-free. For a manually dispatched reference workflow,
supply the same independently selected expected source/distribution identities in
its release-check inputs.

Review the patch, normalized trees, native archive binding and notices together.
Keep the current reconstruction contract in the manifest, mechanisms in the
documentation and concise historical context behind its index. Do not append an
operator's deployment diary or private proof inventory to the README or manifest.
After merging, inspect all required hosted checks on the exact final head and read
back the public source/asset bindings. Source publication does not itself change
an installed runtime or qualify another host's accounts and user workflows.

[GitHub Actions](https://github.com/josephbergvinson/openclaw-control-plane/actions/workflows/verify-reference.yml) runs the repository checks
and reconstructs and builds the pinned runtime in a separate Ubuntu checkout.
It accepts manual dispatch and runs on pull requests, main, and reference rebuild
branches. Standard hosted runners are free for public repositories; a private fork
uses the owner's Actions allowance. See [GitHub's runner documentation](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
The native plan adapter changes only full-lint shard selection and preserves every
other command, including checks after lint. Its tests use fake command execution.
Independent native-check and build jobs feed the existing `reconstruct-and-build`
check; each qualification job must succeed. Workflow artifacts retain command plans
and actual results without environment values.
No operator credentials or private preservation data belong in that workflow.
