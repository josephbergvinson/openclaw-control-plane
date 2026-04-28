# Validation Checklist

Run validation before claiming the control plane is installed, healthy, or ready for mutation-heavy work.

## Basic repo checks

From the public template repo:

```bash
python3 -m pytest -q
```

From your adapted OpenClaw workspace, run the relevant local tests you maintain.

## Gateway and channels

Recommended read-only probes:

```bash
openclaw --version
openclaw gateway status
openclaw gateway probe
openclaw gateway health
openclaw channels status --probe
openclaw status --deep
```

Expected outcome:

- gateway is reachable
- expected channels are configured and connected
- channel probes work
- no unexpected active/queued/running task rows for an idle setup

## Memory

If you use memory:

```bash
openclaw memory status --deep
```

Expected outcome:

- provider is the one you intended
- index count is plausible
- dirty status is understood
- embeddings/vector/FTS readiness is known

## Durable Discord slice helper

If using `scripts/discord_coding_slice.py`, validate it in the least invasive mode available for your setup.

Suggested checks:

```bash
python3 scripts/discord_coding_slice.py --help
python3 scripts/discord_coding_slice.py closeout-check --help
```

If your local version supports dry-run/start modes, use dry-run first.

## Inbound deadline guard

If your control plane includes an inbound deadline/liveness guard, validate the guard tests or equivalent local helper.

Example pattern:

```bash
python3 -m pytest -q tests/test_discord_inbound_deadline_guard.py
```

If your repo does not include that test, document what replaces it.

## Closeout gate

Before reporting completion for a non-trivial slice, run your closeout gate.

Example pattern:

```bash
python3 scripts/slice_closeout_gate.py --help
python3 scripts/discord_coding_slice.py closeout-check <args>
```

A completion report should not claim fully closed out unless the gate says complete or the remaining blocker is named explicitly.

## Public-safety scan

Before publishing a template repo, run a scan for obvious secrets and private identifiers.

Example:

```bash
grep -RInE 'gho_|ntn_|sk-[A-Za-z0-9]{12,}|TOKEN|PASSWORD|SECRET|/Users/|[0-9]{17,20}' . \
  --exclude-dir=.git \
  --exclude-dir=.pytest_cache
```

This is not a substitute for careful review, but it catches common mistakes.
