# Security policy

## Scope

This repository is documentation: example contracts, JSON schemas, and policy templates.
It is not a running system.

Security issues in **OpenClaw itself** belong upstream with that project. Issues in the
*architecture described here* — a recommendation that would weaken an adopter's system,
an example that encodes an unsafe default, a missing control in the trust model — are in
scope for this repository.

## Reporting

Open a private security advisory on the repository. Please do not put credentials,
personal data, or live host details into a public issue.

## Reading the trust model first

Before adopting any pattern here, read
[docs/16-security-and-trust-model.md](docs/16-security-and-trust-model.md). Two points
determine whether this architecture is safe for your situation:

- It assumes **one trusted operator on one host**. It is not a multi-tenant boundary, and
  it does not become one by adding accounts.
- An agent with tools treats everything reached *through* those tools — web pages, issue
  bodies, file contents, documents, tool output — as **data, not instructions**. Content
  observed during a task must never widen an approval envelope.

An adopter who copies the mechanics but not the trust model gets the ergonomics of this
system without its safety properties.
