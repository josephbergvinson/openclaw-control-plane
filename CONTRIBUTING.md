# Contributing

This repository is documentation, templates, and example contracts. Contributions that
sharpen the architecture, correct an inaccuracy, or improve an example are welcome.

## Principles

- **Architecture over anecdote.** Write invariants and failure classes. A rule is worth
  documenting when you can name the failure it prevents.
- **State provenance.** When you document a capability, say whether it is policy-only,
  helper-backed, or runtime-backed. An unlabelled capability claim reads as stronger than it
  is. The fourth level, live-proven, is different in kind: it belongs on a capability status
  row recorded against a specific installation, never in a chapter table, because no document
  can confer it. See [docs/03-capability-provenance.md](docs/03-capability-provenance.md).
- **Keep examples adaptable.** Everything in `examples/`, `templates/`, and `docs/` uses
  angle-bracket notation so a reader can drop it into their own environment and fill in the
  blanks. Write for the general case.
- **Don't overstate.** Distinguish a proposed pattern from an implemented one, and say which
  you are describing.
- **Change contracts together.** When a contract changes, update the schema, the example,
  the affected chapter, and the tests in the same commit.

## Style

- Terse and declarative. Present tense. One H1 per document.
- Sentence-case headings, no emoji, no first person.
- Relative links must resolve. Mermaid blocks must render.
- Every capability table row needs an implementation level.

## Checks

```bash
python3 scripts/validate_repo.py
python3 -m unittest discover -s tests -p 'test_*.py'
git diff --check
```

`validate_repo.py` checks that the tree hangs together: required files, JSON validity,
example/schema agreement, link integrity, and Mermaid well-formedness. It is a lint, not a
proof — a human still has to decide whether a change is accurate.
