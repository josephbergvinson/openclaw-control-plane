from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "operation_effect_predicate.py"


def _load():
    spec = importlib.util.spec_from_file_location("operation_effect_predicate_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


effect = _load()


def test_observed_advance_satisfied_unsatisfied_and_unreadable() -> None:
    assert effect.observed_advance("watermark", 1, 2)["satisfied"] is True
    assert effect.observed_advance("watermark", 2, 2)["reason"] == "no_observed_advance"
    result = effect.observed_advance("watermark", None, 2)
    assert result["satisfied"] is False and result["fail_closed"] is True


def test_count_predicates_cover_both_branches_and_none() -> None:
    assert effect.count_decreased("count", 3, 2)["satisfied"] is True
    assert effect.count_decreased("count", 2, 2)["reason"] == "count_did_not_decrease"
    assert effect.count_decreased("count", 2, None)["fail_closed"] is True
    assert effect.count_shrank_by("count", 5, 3, 2)["satisfied"] is True
    assert effect.count_shrank_by("count", 5, 4, 2)["reason"] == "count_did_not_shrink_by_expected"
    assert effect.count_shrank_by("count", None, 4, 2)["fail_closed"] is True


def test_object_and_readback_predicates_cover_all_outcomes() -> None:
    assert effect.object_present("object", "x", True, True)["satisfied"] is True
    assert effect.object_present("object", "x", False, True)["reason"] == "object_absent"
    assert effect.object_present("object", None, True, True)["fail_closed"] is True
    assert effect.readback_matches("value", "a", "a")["satisfied"] is True
    assert effect.readback_matches("value", "a", "b")["reason"] == "readback_mismatch"
    assert effect.readback_matches("value", "a", None)["fail_closed"] is True


def test_emit_returns_reserved_code_and_names_first_failure(capsys) -> None:
    ok = effect.readback_matches("first", 1, 1)
    failed = effect.readback_matches("second", 1, 2)
    assert effect.emit([ok], what="publish") == 0
    assert "effect_satisfied" in capsys.readouterr().out
    assert effect.emit([ok, failed], what="publish") == 4
    output = capsys.readouterr().out
    assert "reason: readback_mismatch" in output
    assert "EFFECT_PREDICATES " in output
