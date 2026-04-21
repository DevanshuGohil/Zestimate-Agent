"""Offline tests for the evaluation harness (evals/run_eval.py)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

# ---------------------------------------------------------------------------
# Load the harness module from its file path
# ---------------------------------------------------------------------------

_HARNESS = Path(__file__).parent.parent / "evals" / "run_eval.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("run_eval", _HARNESS)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_eval"] = mod  # register before exec so @dataclass can find the module
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_harness()
EvalRecord = _mod.EvalRecord
EvalResult = _mod.EvalResult
EvalSummary = _mod.EvalSummary

runner = CliRunner()


# ---------------------------------------------------------------------------
# Unit tests for data structures
# ---------------------------------------------------------------------------


def test_eval_result_success_when_no_error():
    rec = EvalRecord(id="x", address="123 Main St", expected_zpid=None, expected_zestimate=None)
    ev = EvalResult(record=rec, elapsed_ms=500, actual_zestimate=400_000, actual_zpid="abc")
    assert ev.success is True
    assert ev.error is None


def test_eval_result_not_success_when_error():
    rec = EvalRecord(id="x", address="123 Main St", expected_zpid=None, expected_zestimate=None)
    ev = EvalResult(record=rec, elapsed_ms=100, error="ProviderError: 503")
    assert ev.success is False


def test_eval_result_zestimate_match():
    rec = EvalRecord(id="x", address="a", expected_zpid=None, expected_zestimate=500_000)
    ev = EvalResult(record=rec, elapsed_ms=0, actual_zestimate=500_000, actual_zpid="z")
    assert ev.zestimate_match is True


def test_eval_result_zestimate_mismatch():
    rec = EvalRecord(id="x", address="a", expected_zpid=None, expected_zestimate=500_000)
    ev = EvalResult(record=rec, elapsed_ms=0, actual_zestimate=499_999, actual_zpid="z")
    assert ev.zestimate_match is False


def test_eval_result_zestimate_match_skips_nulls():
    rec = EvalRecord(id="x", address="a", expected_zpid=None, expected_zestimate=None)
    ev = EvalResult(record=rec, elapsed_ms=0, actual_zestimate=500_000, actual_zpid="z")
    assert ev.zestimate_match is False  # no expected → never a match


def test_eval_result_zpid_match():
    rec = EvalRecord(id="x", address="a", expected_zpid="123", expected_zestimate=None)
    ev = EvalResult(record=rec, elapsed_ms=0, actual_zestimate=500_000, actual_zpid="123")
    assert ev.zpid_match is True


def test_eval_result_to_dict_keys():
    rec = EvalRecord(id="sf", address="101 Lombard St", expected_zpid="z1", expected_zestimate=800_000)
    ev = EvalResult(record=rec, elapsed_ms=3200, actual_zestimate=800_000, actual_zpid="z1")
    d = ev.to_dict()
    for key in ("id", "address", "success", "actual_zestimate", "expected_zestimate",
                "zestimate_match", "actual_zpid", "expected_zpid", "zpid_match", "elapsed_ms", "error"):
        assert key in d, f"missing key: {key}"


# ---------------------------------------------------------------------------
# Unit tests for EvalSummary
# ---------------------------------------------------------------------------


def _make_summary(results: list[EvalResult]) -> EvalSummary:
    s = EvalSummary()
    s.results = results
    return s


def _rec(expected_zestimate=None, expected_zpid=None):
    return EvalRecord(id="r", address="a", expected_zpid=expected_zpid, expected_zestimate=expected_zestimate)


def test_summary_success_rate():
    s = _make_summary([
        EvalResult(record=_rec(), elapsed_ms=100, actual_zestimate=100_000, actual_zpid="1"),
        EvalResult(record=_rec(), elapsed_ms=100, error="fail"),
    ])
    assert s.success_count == 1
    assert s.success_rate == 0.5


def test_summary_zestimate_accuracy_skips_nulls():
    s = _make_summary([
        EvalResult(record=_rec(expected_zestimate=500_000), elapsed_ms=0, actual_zestimate=500_000, actual_zpid="1"),
        EvalResult(record=_rec(expected_zestimate=None), elapsed_ms=0, actual_zestimate=300_000, actual_zpid="2"),
    ])
    assert s.zestimate_accuracy == 1.0  # only 1 has expected; it matches


def test_summary_avg_latency_only_successful():
    s = _make_summary([
        EvalResult(record=_rec(), elapsed_ms=200, actual_zestimate=1, actual_zpid="1"),
        EvalResult(record=_rec(), elapsed_ms=400, actual_zestimate=1, actual_zpid="2"),
        EvalResult(record=_rec(), elapsed_ms=9999, error="fail"),
    ])
    assert s.avg_latency_ms == 300.0


def test_summary_to_dict_structure():
    s = _make_summary([
        EvalResult(record=_rec(expected_zestimate=100_000), elapsed_ms=500, actual_zestimate=100_000, actual_zpid="z"),
    ])
    d = s.to_dict()
    assert d["total"] == 1
    assert d["success_count"] == 1
    assert d["zestimate_accuracy"] == 1.0
    assert len(d["results"]) == 1


# ---------------------------------------------------------------------------
# Integration: CLI smoke tests
# ---------------------------------------------------------------------------


def _mock_run_one(record):
    """Return a fake successful EvalResult for any record."""
    return EvalResult(
        record=record,
        elapsed_ms=1500,
        actual_zestimate=750_000,
        actual_zpid="9999",
    )


def test_eval_cli_runs_without_error(tmp_path):
    seeds = tmp_path / "known_addresses.jsonl"
    seeds.write_text(
        '{"id": "t1", "address": "101 Main St, Anytown, CA 90210", "expected_zpid": null, "expected_zestimate": null, "notes": ""}\n'
        '{"id": "t2", "address": "202 Oak Ave, Somewhere, TX 78701", "expected_zpid": null, "expected_zestimate": null, "notes": ""}\n'
    )
    with patch.object(_mod, "_SEEDS_FILE", seeds), patch.object(_mod, "_run_one", _mock_run_one):
        result = runner.invoke(_mod.app, [])
    assert result.exit_code == 0, result.output
    assert "2" in result.output  # total addresses


def test_eval_cli_json_output(tmp_path):
    seeds = tmp_path / "known_addresses.jsonl"
    seeds.write_text(
        '{"id": "t1", "address": "101 Main St, CA 90210", "expected_zpid": null, "expected_zestimate": null, "notes": ""}\n'
    )
    with patch.object(_mod, "_SEEDS_FILE", seeds), patch.object(_mod, "_run_one", _mock_run_one):
        result = runner.invoke(_mod.app, ["--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["total"] == 1
    assert data["success_count"] == 1
    assert "results" in data


def test_eval_cli_limit_flag(tmp_path):
    seeds = tmp_path / "known_addresses.jsonl"
    lines = "\n".join(
        f'{{"id": "t{i}", "address": "{i} Main St, CA 90210", "expected_zpid": null, "expected_zestimate": null, "notes": ""}}'
        for i in range(5)
    ) + "\n"
    seeds.write_text(lines)
    with patch.object(_mod, "_SEEDS_FILE", seeds), patch.object(_mod, "_run_one", _mock_run_one):
        result = runner.invoke(_mod.app, ["--limit", "2", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["total"] == 2


def test_eval_cli_fail_under_exits_1(tmp_path):
    seeds = tmp_path / "known_addresses.jsonl"
    seeds.write_text(
        '{"id": "t1", "address": "101 Main St, CA 90210", "expected_zpid": null, "expected_zestimate": null, "notes": ""}\n'
    )

    def _mock_fail(record):
        return EvalResult(record=record, elapsed_ms=100, error="some error")

    with patch.object(_mod, "_SEEDS_FILE", seeds), patch.object(_mod, "_run_one", _mock_fail):
        result = runner.invoke(_mod.app, ["--fail-under", "0.5"])
    assert result.exit_code == 1


def test_eval_cli_refresh_updates_seeds(tmp_path):
    seeds = tmp_path / "known_addresses.jsonl"
    seeds.write_text(
        '{"id": "t1", "address": "101 Main St, CA 90210", "expected_zpid": null, "expected_zestimate": null, "notes": ""}\n'
    )
    with patch.object(_mod, "_SEEDS_FILE", seeds), patch.object(_mod, "_run_one", _mock_run_one):
        result = runner.invoke(_mod.app, ["--refresh"])
    assert result.exit_code == 0, result.output
    updated = json.loads(seeds.read_text().strip())
    assert updated["expected_zpid"] == "9999"
    assert updated["expected_zestimate"] == 750_000


# ---------------------------------------------------------------------------
# Seeds file sanity checks
# ---------------------------------------------------------------------------


def test_seeds_file_has_20_records():
    records = _mod._load_records()
    assert len(records) == 20


def test_seeds_file_all_records_have_required_fields():
    records = _mod._load_records()
    for rec in records:
        assert rec.id, f"missing id in {rec}"
        assert rec.address, f"missing address in {rec}"


def test_seeds_file_no_duplicate_ids():
    records = _mod._load_records()
    ids = [r.id for r in records]
    assert len(ids) == len(set(ids)), "duplicate ids in seeds file"


def test_seeds_file_all_addresses_have_state():
    records = _mod._load_records()
    for rec in records:
        # Each address should contain a 2-letter state abbreviation
        parts = rec.address.split(",")
        assert len(parts) >= 2, f"address too short: {rec.address}"
