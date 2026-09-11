#!/usr/bin/env python3
"""Run archive-v3 retention while preserving both frozen clone-v2 proofs.

This is the standing scheduler entrypoint.  Its clone-v2 pins are immutable
transition evidence, not part of the archive-v3 two-copy floor and never
deletion candidates.
"""
from __future__ import annotations
try:
    from .operator_contract import load_operator_contract
except ImportError:
    from operator_contract import load_operator_contract
OPERATOR = load_operator_contract()


import argparse
import json
import re
from pathlib import Path

try:
    from . import openclaw_archive_v3_retention as archive_retention
except ImportError:
    import openclaw_archive_v3_retention as archive_retention


def effect_module():
    try:
        from . import operation_effect_predicate
    except ImportError:
        import operation_effect_predicate
    return operation_effect_predicate


DEFAULT_INDEPENDENT_BACKUP_RECEIPT = Path(
    (str(OPERATOR.require_path('paths.workspace')) + '/artifacts/openclaw_archive_v3_retention/independent-backup-receipt.json')
)


# Legacy generation identities belong to the adopter's inventory. An explicit
# empty list means no frozen clone-v2 generations; absence is not that decision.
def frozen_clone_v2_pins() -> tuple[tuple[str, str], ...]:
    rows = OPERATOR.require_list('backup.frozen_clone_v2_pins')
    result = []
    names = set()
    for row in rows:
        if (not isinstance(row, list) or len(row) != 2
                or any(not isinstance(value, str) for value in row)
                or re.fullmatch(r'openclaw-backup-[0-9]{8}T[0-9]{6}Z', row[0]) is None
                or re.fullmatch(r'[0-9a-f]{64}', row[1]) is None
                or row[0] in names):
            raise ValueError('frozen clone pins require unique generation names and SHA-256 digests')
        names.add(row[0])
        result.append((row[0], row[1]))
    return tuple(result)


FROZEN_CLONE_V2_PINS = frozen_clone_v2_pins()


def validate_archive_retention_receipt(
    receipt: dict,
    *,
    apply_requested: bool,
) -> list[dict]:
    effect = effect_module()
    results: list[dict] = []
    result = receipt.get("result")
    safe_result = result in {"verified", "waiting_external_backup"}
    results.append(
        effect.readback_matches(
            "archive_retention_terminal_result",
            expected=True,
            actual=safe_result,
        )
    )
    results.append(
        effect.readback_matches(
            "archive_retention_apply_mode",
            expected=apply_requested,
            actual=receipt.get("apply", receipt.get("apply_requested")),
        )
    )
    weekly_root_value = receipt.get("weekly_root")
    weekly_root = Path(str(weekly_root_value)) if weekly_root_value else None
    if weekly_root is None:
        results.append(effect.unreadable("archive_retention_weekly_root", "receipt omitted weekly_root"))
        return results

    pinned = {
        str(row.get("name")): row
        for row in receipt.get("pinned_clone_v2", [])
        if isinstance(row, dict)
    }
    for name, digest in FROZEN_CLONE_V2_PINS:
        row = pinned.get(name)
        results.append(
            effect.readback_matches(
                "pinned_clone_v2:{}".format(name),
                expected={"manifest_sha256": digest, "result": "verified"},
                actual=(
                    {
                        "manifest_sha256": row.get("manifest_sha256"),
                        "result": row.get("result"),
                    }
                    if row
                    else None
                ),
            )
        )
        results.append(
            effect.object_present(
                "pinned_clone_v2_path:{}".format(name),
                str(weekly_root / name),
                present=(weekly_root / name).is_dir() and not (weekly_root / name).is_symlink(),
                readable=True,
            )
        )

    for name in receipt.get("removed", []):
        target = weekly_root / str(name)
        results.append(
            effect.readback_matches(
                "removed_generation_absent:{}".format(name),
                expected=False,
                actual=target.exists() or target.is_symlink(),
            )
        )
    for field in ("protected_v3", "remaining_v3"):
        for name in receipt.get(field, []):
            target = weekly_root / str(name)
            results.append(
                effect.object_present(
                    "{}:{}".format(field, name),
                    str(target),
                    present=target.is_dir() and not target.is_symlink(),
                    readable=True,
                )
            )
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "after deep verification of every archive-v3 generation, remove "
            "only archive-v3 candidates older than the newest two"
        ),
    )
    parser.add_argument(
        "--independent-backup-receipt",
        type=Path,
        default=DEFAULT_INDEPENDENT_BACKUP_RECEIPT,
        help=(
            "owner-private verified different-device backup receipt required "
            "before deletion (default: %(default)s)"
        ),
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    engine_runner=None,
) -> int:
    args = build_parser().parse_args(argv)
    runner = archive_retention.run_from_args if engine_runner is None else engine_runner
    delegated: list[str] = []
    if args.apply:
        delegated.extend(
            [
                "--apply",
                "--independent-backup-receipt",
                str(args.independent_backup_receipt),
            ]
        )
    for name, digest in FROZEN_CLONE_V2_PINS:
        delegated.extend(
            ["--pinned-clone-v2", "{}={}".format(name, digest)]
        )
    code, report = runner(delegated)
    if code != 0:
        effect = effect_module()
        archive_retention.archive_backup.emit_backup_effect(
            [effect.unreadable("archive_retention", "; ".join(report.get("blockers", [])))],
            what="maintain weekly backups",
        )
        print(archive_retention.report_message(code, report))
        return code
    if report.get("result") == "waiting_external_backup" and report.get("retry_suppressed") is True:
        print("NO_REPLY")
        return 0
    effect = effect_module()
    try:
        receipt_path = report.get("receipt")
        if not receipt_path:
            raise ValueError("engine result omitted receipt path")
        receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
        if not isinstance(receipt, dict):
            raise ValueError("receipt is not a JSON object")
        results = validate_archive_retention_receipt(receipt, apply_requested=args.apply)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        results = [
            effect.unreadable(
                "archive_retention_receipt",
                "{}: {}".format(type(exc).__name__, exc),
            )
        ]
    effect_code = archive_retention.archive_backup.emit_backup_effect(
        results, what="apply verified OWC archive-v3 retention",
    )
    if effect_code != 0:
        print("Backup maintenance failed its final verification. Its outcome is not confirmed.")
        return effect_code
    print(archive_retention.report_message(code, receipt))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
