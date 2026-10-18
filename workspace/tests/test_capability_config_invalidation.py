from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "capability_config_invalidation.py"
SPEC = importlib.util.spec_from_file_location("capability_config_invalidation_test", SCRIPT)
assert SPEC and SPEC.loader
subject = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(subject)


def _write_status(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "capabilities": [
                    {
                        "capability_id": "pdf",
                        "domain": "research",
                        "last_verified_utc": "2026-08-02T00:00:00Z",
                        "config_dependencies": ["agents.defaults.pdfModel"],
                    },
                    {
                        "capability_id": "no-dependencies",
                        "domain": "control-plane",
                        "last_verified_utc": "2026-01-01T00:00:00Z",
                        "config_dependencies": [],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )


def test_changes_after_verification_and_child_keys_invalidate(tmp_path: Path) -> None:
    status = tmp_path / "status.json"
    log = tmp_path / "gateway.log"
    _write_status(status)
    log.write_text(
        "2026-08-01T12:00:00Z [reload] config change detected; evaluating reload (agents.defaults.pdfModel.primary)\n"
        "2026-08-03T12:00:00Z [reload] config change detected; evaluating reload (agents.defaults.pdfModel.primary, meta.lastTouchedAt)\n",
        encoding="utf-8",
    )
    payload = subject.report_invalidations(status, [log])
    assert [row["capability_id"] for row in payload["invalidated"]] == ["pdf"]
    changes = payload["invalidated"][0]["invalidating_changes"]
    assert len(changes) == 1
    assert changes[0]["key"] == "agents.defaults.pdfModel.primary"
    assert payload["coverage_complete"] is True


def test_parent_key_change_matches_specific_dependency(tmp_path: Path) -> None:
    status = tmp_path / "status.json"
    _write_status(status)
    data = json.loads(status.read_text(encoding="utf-8"))
    data["capabilities"][0]["config_dependencies"] = ["agents.defaults.pdfModel.primary"]
    status.write_text(json.dumps(data), encoding="utf-8")
    log = tmp_path / "gateway.log"
    log.write_text(
        "2026-08-03T12:00:00Z [reload] config change detected; evaluating reload (agents.defaults.pdfModel)\n",
        encoding="utf-8",
    )
    payload = subject.report_invalidations(status, [log])
    assert payload["invalidated"][0]["capability_id"] == "pdf"


def test_pdf_capability_tracks_inherited_image_model_changes(tmp_path: Path) -> None:
    # A declared capability can depend on both an explicit PDF model and the
    # image model it inherits; the reference contains no operator-ready row.
    pdf = {
        "capability_id": "pdf-with-inherited-image-model",
        "domain": "research",
        "last_verified_utc": "2026-09-05T00:55:53Z",
        "config_dependencies": ["agents.defaults.pdfModel", "agents.defaults.imageModel"],
    }
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"capabilities": [pdf]}), encoding="utf-8")
    log = tmp_path / "gateway.log"
    log.write_text(
        "2026-09-04T00:00:00Z [gateway] fixture coverage starts\n"
        "2026-09-08T00:00:00Z [reload] config change detected; evaluating reload "
        "(agents.defaults.imageModel.primary)\n",
        encoding="utf-8",
    )
    payload = subject.report_invalidations(status, [log])
    assert [row["capability_id"] for row in payload["invalidated"]] == [
        pdf["capability_id"]
    ]


def test_ignored_metadata_and_empty_dependencies_never_invalidate(tmp_path: Path) -> None:
    status = tmp_path / "status.json"
    log = tmp_path / "gateway.log"
    _write_status(status)
    log.write_text(
        "2026-08-03T12:00:00Z [reload] config change detected; evaluating reload (meta.lastTouchedAt)\n",
        encoding="utf-8",
    )
    payload = subject.report_invalidations(status, [log])
    assert payload["invalidated"] == []


def test_missing_log_is_reported_as_coverage_gap(tmp_path: Path) -> None:
    status = tmp_path / "status.json"
    _write_status(status)
    payload = subject.report_invalidations(status, [tmp_path / "missing.log"])
    assert payload["coverage_complete"] is False
    assert payload["log_coverage"][0]["exists"] is False
    assert [row["capability_id"] for row in payload["unknown"]] == ["pdf"]


def test_existing_but_truncated_log_reports_capability_unknown(tmp_path: Path) -> None:
    status = tmp_path / "status.json"
    log = tmp_path / "gateway.log"
    _write_status(status)
    log.write_text(
        "2026-08-04T12:00:00Z [gateway] ordinary line after rotation\n",
        encoding="utf-8",
    )
    payload = subject.report_invalidations(status, [log])
    assert payload["invalidated"] == []
    assert payload["coverage_complete"] is False
    assert payload["log_coverage"][0]["first_observed_at"] == "2026-08-04T12:00:00Z"
    coverage = payload["coverage_by_capability"][0]
    assert coverage["capability_id"] == "pdf"
    assert coverage["complete"] is False
    assert [row["capability_id"] for row in payload["unknown"]] == ["pdf"]


def _native_entry(*, paths=None, kind="config-write", **extra):
    return {"id": "config-audit:12", "at": 1785758400000, "kind": kind,
            "source": "config-rpc", "summary": "private diagnostic must not escape",
            **({"changedPaths": paths} if paths is not None else {}), **extra}


def test_default_cli_reads_native_history_instead_of_stale_log(tmp_path, monkeypatch, capsys):
    status = tmp_path / "status.json"
    stale = tmp_path / "gateway.log"
    _write_status(status)
    stale.write_text("2026-08-01T00:00:00Z [gateway] old process\n")
    calls = []
    def page(params):
        calls.append(params)
        return {"entries": [_native_entry(paths=["agents.defaults.pdfModel.primary"])]}
    monkeypatch.setattr(subject, "read_native_page", page, raising=False)
    monkeypatch.setattr(subject, "DEFAULT_LOG", stale, raising=False)
    monkeypatch.setattr(sys, "argv", ["helper", "--status", str(status), "--json"])

    assert subject.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert calls == [{"limit": 200}]
    assert [row["capability_id"] for row in payload["invalidated"]] == ["pdf"]
    assert "private diagnostic" not in json.dumps(payload)
    assert payload["coverage_complete"] is False


@pytest.mark.parametrize("kind", ["config-write", "external-edit", "operation"])
def test_native_history_invalidates_exact_dependency_without_claiming_complete_coverage(tmp_path, monkeypatch, kind):
    status = tmp_path / "status.json"
    _write_status(status)
    monkeypatch.setattr(subject, "read_native_page", lambda params:
                        {"entries": [_native_entry(kind=kind, paths=["agents.defaults.pdfModel.primary", "meta.lastTouchedAt"])]}, raising=False)
    payload = subject.report_invalidations(status)
    assert [row["capability_id"] for row in payload["invalidated"]] == ["pdf"]
    assert len(payload["invalidated"][0]["invalidating_changes"]) == 1
    assert payload["coverage_complete"] is False
    assert payload["coverage_by_capability"][0]["complete"] is False
    assert "private diagnostic" not in json.dumps(payload)


@pytest.mark.parametrize("entries", [[], [_native_entry(at=1785456000000, paths=["browser.enabled"])],
                                      [_native_entry(opaqueChange=True)],
                                      [_native_entry(paths=["browser.enabled", "…+7 more"])],
                                      [_native_entry(at="bad", paths=["agents.defaults.pdfModel"])]] )
def test_native_retained_empty_opaque_capped_or_malformed_history_stays_unknown(tmp_path, monkeypatch, entries):
    status = tmp_path / "status.json"
    _write_status(status)
    monkeypatch.setattr(subject, "read_native_page", lambda params: {"entries": entries}, raising=False)
    payload = subject.report_invalidations(status)
    assert payload["coverage_complete"] is False
    assert payload["invalidated"] == []
    assert [row["capability_id"] for row in payload["unknown"]] == ["pdf"]
    assert all("…+" not in change["key"] for row in payload["invalidated"] for change in row["invalidating_changes"])


def test_native_pagination_is_bounded_and_keeps_positive_evidence(tmp_path, monkeypatch):
    status = tmp_path / "status.json"
    _write_status(status)
    calls = []
    def page(params):
        calls.append(params)
        return {"entries": [_native_entry(paths=["agents.defaults.pdfModel"])],
                "nextCursor": f"cursor-{len(calls)}"}
    monkeypatch.setattr(subject, "read_native_page", page, raising=False)
    payload = subject.report_invalidations(status)
    assert len(calls) == 4
    assert calls[0] == {"limit": 200}
    assert calls[1] == {"limit": 200, "beforeCursor": "cursor-1"}
    assert len(payload["invalidated"]) == 1
    assert payload["coverage_complete"] is False
    assert payload["log_coverage"][0]["stop_reason"] == "page_limit"


@pytest.mark.parametrize("failure", [OSError("synthetic-native-error"), ValueError("synthetic-native-error"),
                                     subprocess.TimeoutExpired(["synthetic-native-error"], 30)])
def test_native_read_failure_is_sanitized_unknown(tmp_path, monkeypatch, failure):
    status = tmp_path / "status.json"
    _write_status(status)
    def page(params):
        raise failure
    monkeypatch.setattr(subject, "read_native_page", page, raising=False)
    payload = subject.report_invalidations(status)
    assert payload["invalidated"] == []
    assert payload["coverage_complete"] is False
    assert [row["capability_id"] for row in payload["unknown"]] == ["pdf"]
    assert "synthetic-native-error" not in json.dumps(payload)


def test_native_cli_transport_is_the_supported_bounded_read(monkeypatch):
    calls = []
    monkeypatch.setattr(subject, "resolve_openclaw_bin", lambda: "/fixture/openclaw")
    monkeypatch.setattr(subject, "build_openclaw_env", lambda: {"PATH": "/fixture"})
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, '{"entries": []}', "private stderr")
    monkeypatch.setattr(subject.subprocess, "run", run)
    assert subject.read_native_page({"limit": 200}) == {"entries": []}
    argv, kwargs = calls[0]
    assert argv[0] == "/fixture/openclaw"
    assert argv[1:5] == ["gateway", "call", "openclaw.changes.list", "--params"]
    assert json.loads(argv[5]) == {"limit": 200}
    assert argv[6:] == ["--json"]
    assert kwargs["timeout"] == 30
    assert kwargs["env"] == {"PATH": "/fixture"}


def test_native_empty_page_continuation_is_followed(tmp_path, monkeypatch):
    status = tmp_path / "status.json"
    _write_status(status)
    calls = []
    def page(params):
        calls.append(params)
        if len(calls) == 1:
            return {"entries": [], "nextCursor": "native-opaque-cursor"}
        return {"entries": [_native_entry(paths=["agents.defaults.pdfModel"])]}
    monkeypatch.setattr(subject, "read_native_page", page)
    payload = subject.report_invalidations(status)
    assert calls == [{"limit": 200}, {"limit": 200, "beforeCursor": "native-opaque-cursor"}]
    assert len(payload["invalidated"]) == 1
    assert payload["coverage_complete"] is False


def test_native_partial_failure_keeps_prior_exact_positive(tmp_path, monkeypatch):
    status = tmp_path / "status.json"
    _write_status(status)
    def page(params):
        if "beforeCursor" in params:
            raise ValueError("synthetic-native-error")
        return {"entries": [_native_entry(paths=["agents.defaults.pdfModel"])], "nextCursor": "next"}
    monkeypatch.setattr(subject, "read_native_page", page)
    payload = subject.report_invalidations(status)
    assert len(payload["invalidated"]) == 1
    assert payload["coverage_complete"] is False
    assert payload["log_coverage"][0]["stop_reason"] == "native_history_unavailable"
    assert "synthetic-native-error" not in json.dumps(payload)


@pytest.mark.parametrize("response", [None, [], {"entries": {}}, {"entries": [], "nextCursor": ""}])
def test_native_malformed_page_is_unknown(tmp_path, monkeypatch, response):
    status = tmp_path / "status.json"
    _write_status(status)
    monkeypatch.setattr(subject, "read_native_page", lambda params: response)
    payload = subject.report_invalidations(status)
    assert payload["invalidated"] == []
    assert [row["capability_id"] for row in payload["unknown"]] == ["pdf"]
    assert payload["log_coverage"][0]["stop_reason"] == "native_history_unavailable"


def test_historical_log_ending_before_verification_does_not_cover_it(tmp_path):
    status = tmp_path / "status.json"
    log = tmp_path / "old.log"
    _write_status(status)
    log.write_text("2026-08-01T00:00:00Z [gateway] previous process ended\n")
    payload = subject.report_invalidations(status, [log])
    assert payload["coverage_scope"] == "historical_log_window"
    assert payload["coverage_complete"] is False
    assert [row["capability_id"] for row in payload["unknown"]] == ["pdf"]


@pytest.mark.parametrize("extra", [{"kind": []}, {"kind": {}}, {"at": 10 ** 400}])
def test_malformed_native_entry_does_not_escape_as_exception(tmp_path, monkeypatch, extra):
    status = tmp_path / "status.json"
    _write_status(status)
    monkeypatch.setattr(subject, "read_native_page", lambda params:
                        {"entries": [_native_entry(paths=["agents.defaults.pdfModel"], **extra)]})
    payload = subject.report_invalidations(status)
    assert payload["invalidated"] == []
    assert [row["capability_id"] for row in payload["unknown"]] == ["pdf"]
