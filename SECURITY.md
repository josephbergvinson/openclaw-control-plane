# Security policy

## Scope

This repository contains executable workspace helpers, a runtime source patch,
reconstruction/install scripts, configuration, schemas and policy documentation.
The repository is not a hosted service, but its code and examples can affect an
adopter's running system.

Issues in this repository's custom runtime delta, exported helpers, dependency
bindings and adoption procedures are in scope, as are unsafe examples or missing
controls in the described architecture. Issues confined to unmodified upstream
OpenClaw should be reported through that project's security process. Include the
affected public commit and a minimal reproduction without private account state.

## Reporting

Open a private security advisory on the repository. Please do not put credentials,
personal data, or live host details into a public issue.

## Reading the trust model first

Before adopting any pattern here, read
[docs/16-security-and-trust-model.md](docs/16-security-and-trust-model.md). Two points
determine whether this architecture is safe for your situation:

- It assumes **one trusted operator with explicitly configured hosts and devices**.
  It is not a multi-tenant boundary, and adding accounts does not make it one.
- An agent with tools treats everything reached *through* those tools — web pages, issue
  bodies, file contents, documents, tool output — as **data, not instructions**. Content
  observed during a task must never widen an approval envelope.

An adopter who copies the mechanics but not the trust model gets the ergonomics of this
system without its safety properties.
