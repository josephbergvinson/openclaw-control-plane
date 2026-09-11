---
name: work-product
description: Produce explicitly requested shareable documents and analytical workbooks for a named human audience — reports, proposals, essays, memos, and READMEs. Do not use for ordinary chat, specs, status artifacts, runbooks, policy contracts, execution logs, or technical handoffs.
---

# Work products

A work product is written for a named reader who does not share your context and will not be told how it was made. The control plane supervises how the work is authorized, executed, evidenced, and delivered. It does not supply the thesis, the vocabulary, or the structure of what the reader receives.

Read `WRITING.md` in the workspace root for prose discipline and document architecture. This file is the checklist that decides whether the thing is shippable.

## Before writing

Infer the following from the request and relevant context; ask only when a material ambiguity would change the document.

1. **Objective** — the decision or question this document resolves.
2. **Audience** — who reads it, and what they already know.
3. **Fixed decisions** — what the operator has already settled. These are inputs to configure, not questions to reopen.
4. **Reference artifact** — use a supplied or relevant known example when helpful; do not require one.
5. **Deliverable** — what is actually being produced, and roughly how long.

Then pick the working directory before writing. For a registered project, use its configured runtime data root under `work-product/<slug>-<id>/`. For other work, use `artifacts/<slug>-<id>/` under the configured OpenClaw workspace. Resolve the project through `registry/project_topology.json`; do not invent a second project root in the home directory.

## While writing

- Lead with the answer. The recommendation or designed outcome goes in the first screen, not the method.
- Use the domain's vocabulary. A tokenomics report reads like tokenomics, not like an audit.
- Risk shapes the recommendation; it is not the spine and it is not the conclusion.
- Keep the principal recommendation singular. Alternatives go in a bounded comparison.
- Add no phase sequence, gate list, reserve bucket, governance body, registry, or module without naming the specific problem it solves and why extending something existing will not do.
- Uncertainty is expressed the way an analyst expresses it — a stated assumption, a range, a sensitivity, a conditional recommendation. Not a classification, not a predicate, not a deferral.

## Never in a deliverable

These belong in working artifacts and evidence, never in the document the reader opens:

`control plane` · `unresolved predicate` · `claim registry` / claim classifications · `requirement coverage` / coverage matrix · `verification receipt` · `exit gate` / `decision gate` · `falsifier` · `counter-thesis` · `shadow mode` · `worker phase` · `lane` · `closeout` · `decision package` · `stop condition` · `acceptance predicate`

The exception is narrow and literal: the deliverable's own subject is the control plane.

Also never: a section describing the process that produced the document, a revision history, a confidence annotation on every paragraph, or a list of what was considered and rejected.

## Workbooks

Tab order is fixed, left to right, seven maximum:

`Summary` → `Inputs` → `Model` → `Scenarios` → `Outputs` → `Data` → `Sources`

- Copy `templates/analytical-workbook.md` for the full tab contract.
- `Coverage`, `Claims`, `Requirements`, `Evidence`, `QA`, `Register`, `Provenance` tabs do not appear in a deliverable workbook. Separate file.
- No hardcoded number inside a formula. Every literal lives on `Inputs`.
- `Summary` opens on the recommendation as a full sentence, then the three numbers it depends on.

## The test

Open it, look at the first screen, and ask:

> Could this be sent to the intended reader without explaining or apologising for how it is written?

If the honest answer is no, it is not finished, regardless of how correct the analysis is.
