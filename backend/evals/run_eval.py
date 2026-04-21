"""Evaluation harness: runs the Zestimate pipeline against known addresses.

Usage:
    # Run eval against current expected values (requires prior --refresh):
    uv run python evals/run_eval.py

    # Populate / update expected values from live data:
    uv run python evals/run_eval.py --refresh

    # Limit to first N addresses (useful during development):
    uv run python evals/run_eval.py --limit 5

    # Machine-readable output:
    uv run python evals/run_eval.py --json
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer

_EVALS_DIR = Path(__file__).parent
_SEEDS_FILE = _EVALS_DIR / "known_addresses.jsonl"

app = typer.Typer(add_completion=False, no_args_is_help=False)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class EvalRecord:
    id: str
    address: str
    expected_zpid: str | None
    expected_zestimate: int | None
    notes: str = ""


@dataclass
class EvalResult:
    record: EvalRecord
    elapsed_ms: int
    actual_zestimate: int | None = None
    actual_zpid: str | None = None
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.error is None and self.actual_zestimate is not None

    @property
    def zpid_match(self) -> bool:
        if self.record.expected_zpid is None or self.actual_zpid is None:
            return False
        return self.record.expected_zpid == self.actual_zpid

    @property
    def zestimate_match(self) -> bool:
        if self.record.expected_zestimate is None or self.actual_zestimate is None:
            return False
        return self.record.expected_zestimate == self.actual_zestimate

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.record.id,
            "address": self.record.address,
            "success": self.success,
            "actual_zestimate": self.actual_zestimate,
            "expected_zestimate": self.record.expected_zestimate,
            "zestimate_match": self.zestimate_match,
            "actual_zpid": self.actual_zpid,
            "expected_zpid": self.record.expected_zpid,
            "zpid_match": self.zpid_match,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
        }


@dataclass
class EvalSummary:
    results: list[EvalResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def success_count(self) -> int:
        return sum(1 for r in self.results if r.success)

    @property
    def zestimate_match_count(self) -> int:
        return sum(1 for r in self.results if r.zestimate_match)

    @property
    def zpid_match_count(self) -> int:
        return sum(1 for r in self.results if r.zpid_match)

    @property
    def error_count(self) -> int:
        return sum(1 for r in self.results if r.error)

    @property
    def success_rate(self) -> float:
        return self.success_count / self.total if self.total else 0.0

    @property
    def zestimate_accuracy(self) -> float:
        """Exact-match accuracy vs expected_zestimate (skips nulls)."""
        with_expected = [r for r in self.results if r.record.expected_zestimate is not None]
        if not with_expected:
            return 0.0
        return sum(1 for r in with_expected if r.zestimate_match) / len(with_expected)

    @property
    def avg_latency_ms(self) -> float:
        successful = [r for r in self.results if r.success]
        if not successful:
            return 0.0
        return sum(r.elapsed_ms for r in successful) / len(successful)

    @property
    def p95_latency_ms(self) -> float:
        latencies = sorted(r.elapsed_ms for r in self.results if r.success)
        if not latencies:
            return 0.0
        idx = max(0, int(len(latencies) * 0.95) - 1)
        return float(latencies[idx])

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "success_count": self.success_count,
            "success_rate": round(self.success_rate, 4),
            "zestimate_match_count": self.zestimate_match_count,
            "zestimate_accuracy": round(self.zestimate_accuracy, 4),
            "zpid_match_count": self.zpid_match_count,
            "error_count": self.error_count,
            "avg_latency_ms": round(self.avg_latency_ms),
            "p95_latency_ms": round(self.p95_latency_ms),
            "results": [r.to_dict() for r in self.results],
        }


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def _load_records(limit: int | None = None) -> list[EvalRecord]:
    records = []
    with open(_SEEDS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            records.append(
                EvalRecord(
                    id=obj["id"],
                    address=obj["address"],
                    expected_zpid=obj.get("expected_zpid"),
                    expected_zestimate=obj.get("expected_zestimate"),
                    notes=obj.get("notes", ""),
                )
            )
            if limit and len(records) >= limit:
                break
    return records


def _save_records(records: list[EvalRecord], results: list[EvalResult]) -> None:
    """Write updated expected values back to the seeds file."""
    result_map = {r.record.id: r for r in results}
    lines = []
    with open(_SEEDS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            rid = obj["id"]
            if rid in result_map:
                ev = result_map[rid]
                if ev.success:
                    obj["expected_zpid"] = ev.actual_zpid
                    obj["expected_zestimate"] = ev.actual_zestimate
            lines.append(json.dumps(obj))
    with open(_SEEDS_FILE, "w") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------


def _run_one(record: EvalRecord) -> EvalResult:
    import asyncio

    from zestimate_agent.config import get_settings
    from zestimate_agent.models import (
        AmbiguousAddressError,
        ClarificationRequest,
        ProviderError,
        ValidationError,
    )
    from zestimate_agent.pipeline import run_pipeline
    from zestimate_agent.providers.direct import DirectProvider

    t0 = time.monotonic()
    try:
        settings = get_settings()
        provider = DirectProvider(proxy_url=settings.proxy_url)
        result = asyncio.run(run_pipeline(record.address, provider, settings))
        elapsed = int((time.monotonic() - t0) * 1000)
        return EvalResult(
            record=record,
            elapsed_ms=elapsed,
            actual_zestimate=result.zestimate,
            actual_zpid=result.zpid,
        )
    except (AmbiguousAddressError, ValidationError, ProviderError) as e:
        elapsed = int((time.monotonic() - t0) * 1000)
        return EvalResult(record=record, elapsed_ms=elapsed, error=str(e))
    except Exception as e:
        elapsed = int((time.monotonic() - t0) * 1000)
        return EvalResult(record=record, elapsed_ms=elapsed, error=f"unexpected: {e}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@app.command()
def main(
    refresh: bool = typer.Option(False, "--refresh", help="Update expected values from live data"),
    limit: int = typer.Option(0, "--limit", help="Run only first N addresses (0 = all)"),
    json_output: bool = typer.Option(False, "--json", help="Output results as JSON"),
    fail_under: float = typer.Option(
        0.0,
        "--fail-under",
        help="Exit 1 if success rate is below this fraction (e.g. 0.9 for 90%%)",
    ),
) -> None:
    """Run the Zestimate accuracy evaluation harness."""
    from zestimate_agent.config import get_settings
    from zestimate_agent.observability import configure as configure_observability

    try:
        configure_observability(get_settings())
    except Exception:
        pass  # don't fail eval if .env is incomplete

    n = limit if limit > 0 else None
    records = _load_records(n)

    if not json_output:
        mode = "REFRESH" if refresh else "EVAL"
        typer.echo(f"\n[{mode}] {len(records)} addresses  —  {_SEEDS_FILE.name}\n")

    summary = EvalSummary()

    for i, record in enumerate(records, 1):
        if not json_output:
            typer.echo(f"  [{i:>2}/{len(records)}] {record.address[:60]:<60} ", nl=False)

        ev = _run_one(record)
        summary.results.append(ev)

        if not json_output:
            if ev.success:
                match_icon = "✓" if ev.zestimate_match else "~"
                typer.echo(
                    f"{match_icon}  ${ev.actual_zestimate:>10,.0f}  "
                    f"{ev.elapsed_ms:>5}ms  zpid={ev.actual_zpid}"
                )
            else:
                typer.echo(f"✗  ERROR: {ev.error}")

    if refresh:
        _save_records(records, summary.results)
        if not json_output:
            updated = sum(1 for r in summary.results if r.success)
            typer.echo(f"\nRefreshed {updated}/{len(records)} expected values in {_SEEDS_FILE.name}")

    if json_output:
        typer.echo(json.dumps(summary.to_dict(), indent=2))
    else:
        _print_summary(summary)

    if fail_under > 0 and summary.success_rate < fail_under:
        raise typer.Exit(1)


def _print_summary(summary: EvalSummary) -> None:
    sep = "─" * 60
    typer.echo(f"\n{sep}")
    typer.echo(f"  Total addresses   {summary.total}")
    typer.echo(f"  Successful        {summary.success_count} / {summary.total}  ({summary.success_rate:.1%})")
    if any(r.record.expected_zestimate is not None for r in summary.results):
        typer.echo(
            f"  Exact-match acc.  {summary.zestimate_match_count} addresses  "
            f"({summary.zestimate_accuracy:.1%} of those with expected values)"
        )
    typer.echo(f"  Errors            {summary.error_count}")
    typer.echo(f"  Avg latency       {summary.avg_latency_ms:.0f}ms")
    typer.echo(f"  p95 latency       {summary.p95_latency_ms:.0f}ms")
    typer.echo(f"{sep}\n")


if __name__ == "__main__":
    app()
