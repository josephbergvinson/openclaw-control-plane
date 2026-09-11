# Analytical workbook contract

The tab layout for any workbook that accompanies an operator-facing work product. Copy this structure; do not add tabs.

A workbook is a model someone changes an input in and watches the answer move. It is not a place to store what you found.

## Tab order — fixed, left to right, seven maximum

| # | Tab | Contains |
|---|---|---|
| 1 | `Summary` | The recommendation and the numbers that decide it |
| 2 | `Inputs` | Every operator-changeable assumption, one per row, with unit and source |
| 3 | `Model` | The calculation. Reads only from `Inputs` |
| 4 | `Scenarios` | Base / downside / upside, all pointing at the same `Model` |
| 5 | `Outputs` | Charts or headline series — only if there are more than about three |
| 6 | `Data` | Raw and normalized source extracts |
| 7 | `Sources` | Citations and fetch timestamps |

Tabs named `Coverage`, `Claims`, `Requirements`, `Evidence`, `QA`, `Register`, `Provenance`, or `Classification` do not belong in a deliverable workbook. If that material must exist, it is a separate file alongside the evidence.

## Two hard rules

**No hardcoded number inside a formula on `Model`.** Every literal lives on `Inputs`. This is what converts a warehouse into a model, and it is mechanically checkable.

**`Summary` fits on one screen.** Rows 1–25, columns A–F, default zoom, no scrolling.

- **A1** — the recommendation as a full sentence, not a label. "Add three support hours each week for the next month." Not "Executive Summary."
- **A3:A5** — the three numbers the recommendation turns on, each with unit and base-case value.
- **A7 block** — decision parameters and recommended values. Five to eight rows, hard cap.
- **A16 block** — base / downside / upside, one line each.

Above the fold, never: a caveat, a methodology note, a claim classification, a coverage percentage, a version header, a provenance stamp, a data-limitations block, or a table of contents.

## Acceptance test

Open the file, look at tab 1 without scrolling, and ask whether a colleague could state the recommendation and the three numbers it depends on.

If not, the workbook fails regardless of how correct the arithmetic is.
