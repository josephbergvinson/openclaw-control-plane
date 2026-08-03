# Liveness view template

<!-- Generated from the status layer. Do not edit by hand. Repair the source and regenerate. -->

Copy to `<workspace-root>/HEARTBEAT.md`. This is a **generated, non-authoritative** view of liveness, rebuilt from
the status layer. It informs a heartbeat turn; it never overrides a normative contract, and a stale copy is residue
rather than fact. Editing it in place is a defect: fix the canonical source and regenerate.

## Heartbeat behaviour

A heartbeat is a periodic, low-context turn whose only job is to notice that something needs attention. In a
lightweight run this file may be the only workspace context injected, so it states what to check and what silence
means.

1. Read the state below. If every entry is fresh and healthy, do nothing and produce no visible output. Silence is
   the correct result of a healthy heartbeat.
2. If an entry is stale — its evidence is older than its freshness window — treat it as unknown, not as failing.
   Re-probe if a read-only probe exists; otherwise report it as unknown.
3. If an entry is failing, emit one message on the declared route naming the surface, the observed symptom, the
   evidence path, and the single next action. One message per distinct condition, not one per heartbeat.
4. Never mutate, never repair, and never start a durable lane from a heartbeat turn. A heartbeat reports; the
   operator or an approved lane acts.
5. Do not re-report a condition already reported and unchanged. Repeated identical alerts train the operator to
   ignore the route.

## State

| Surface | Health | Evidence | Observed | Freshness window |
|---|---|---|---|---|
| `<surface-slug>` | healthy | `<artifact-root>/<run-id>/probe.log` | `<timestamp>` | 24h |
| `<job-name>` | degraded-contained | known-issues ledger entry | `<timestamp>` | 24h |

Both rows are shape examples. The Health column carries a health class and nothing else, so a capability readiness
word such as *ready* does not belong there — readiness is a property of a route or capability record, and mixing
the two vocabularies is how a degraded surface gets reported as fine. The generator replaces the whole table from
the status layer on every rebuild, which is why a hand edit here survives exactly until the next regeneration and
misleads until then.

Health classes, containment, and freshness windows are defined in
[guards, health, and restoration](../docs/14-guards-health-and-restoration.md).
