"""CLI entry point: `zestimate` command via Typer.

Usage examples:
    zestimate lookup "123 Main St, Springfield, IL 62701"
    zestimate lookup --no-cache "..."
    zestimate lookup --json "..."
    zestimate eval
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Optional

import typer

from .cache import Cache, CachedFailure
from .config import get_settings
from .fetch import fetch_property
from .models import AmbiguousAddressError, NoZestimateError, ProviderError, ValidationError
from .normalize import normalize_address
from .observability import configure as configure_observability
from .providers.direct import DirectProvider
from .resolve import resolve_zpid
from .validate import validate_result

app = typer.Typer(
    name="zestimate",
    help="Fetch the current Zillow Zestimate for a US property address.",
    add_completion=False,
    no_args_is_help=True,
)


@app.command("lookup")
def lookup(
    address: str = typer.Argument(..., help="US property address"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Bypass the local SQLite cache"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
    trace: bool = typer.Option(False, "--trace", help="Print pipeline node trace"),
) -> None:
    """Look up the current Zillow Zestimate for a US property address."""
    asyncio.run(_lookup_async(address, no_cache, json_output, trace))


async def _lookup_async(
    address: str,
    no_cache: bool,
    json_output: bool,
    trace: bool,
) -> None:
    t0 = time.monotonic()

    try:
        settings = get_settings()
    except Exception as e:
        _err(f"configuration error: {e}\nRun: cp .env.example .env  then set required keys.", json_output)
        raise typer.Exit(1)

    configure_observability(settings)
    cache = Cache(settings.cache_db_path, settings.cache_ttl_hours, settings.cache_failure_ttl_hours)
    provider = DirectProvider(proxy_url=settings.proxy_url.get_secret_value() if settings.proxy_url else None)
    cache_hit = False

    try:
        # Stage 1 always runs (provides stable cache key)
        normalized = await normalize_address(address, settings)
        cache_key = Cache.make_key(normalized.single_line())

        # Cache lookup
        if not no_cache:
            hit = await cache.lookup(cache_key)
            if hit.hit and hit.was_failure:
                _err(
                    "address not found in Zillow (cached). "
                    "Use --no-cache to retry.",
                    json_output,
                )
                raise typer.Exit(1)
            if hit.hit and hit.result is not None:
                cache_hit = True
                result = hit.result
            else:
                result = None
        else:
            result = None

        if result is None:
            # Stages 2–4
            try:
                resolved = await resolve_zpid(normalized, provider)
                detail = await fetch_property(resolved.zpid, provider)
                result = validate_result(normalized, detail, resolved, provider.name)
                if not no_cache:
                    await cache.store(cache_key, result)
            except (AmbiguousAddressError, ValidationError, ProviderError, NoZestimateError):
                if not no_cache:
                    await cache.store_failure(cache_key)
                raise

    except AmbiguousAddressError as e:
        _err(f"could not resolve address: {e}", json_output)
        raise typer.Exit(1)
    except NoZestimateError as e:
        _err(f"no Zestimate available: {e}", json_output)
        raise typer.Exit(1)
    except ValidationError as e:
        _err(f"validation failed: {e}", json_output)
        raise typer.Exit(1)
    except ProviderError as e:
        _err(f"provider error: {e}", json_output)
        raise typer.Exit(1)

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    if json_output:
        out: dict = result.model_dump()
        out["fetched_at"] = result.fetched_at.isoformat()
        out["cache_hit"] = cache_hit
        out["elapsed_ms"] = elapsed_ms
        out["confidence"] = result.confidence.value
        typer.echo(json.dumps(out, indent=2, default=str))
    else:
        sep = "─" * 50
        typer.echo(f"\n{sep}")
        typer.echo(f"  Address     {result.address}")
        typer.echo(f"  Zestimate   ${result.zestimate:>12,.0f}")
        typer.echo(f"  ZPID        {result.zpid}")
        typer.echo(f"  Confidence  {result.confidence.value}")
        typer.echo(f"  Provider    {result.provider_used}")
        typer.echo(f"  Cache       {'HIT ✓' if cache_hit else 'miss'}")
        typer.echo(f"  Elapsed     {elapsed_ms / 1000:.1f}s")
        typer.echo(f"{sep}\n")

    if trace:
        typer.echo(
            "[trace] LangGraph node trace available from Step 7 onward.",
            err=True,
        )


@app.command("eval")
def run_eval(
    refresh: bool = typer.Option(False, "--refresh", help="Update expected values from live data"),
    limit: int = typer.Option(0, "--limit", help="Run only first N addresses (0 = all)"),
    json_output: bool = typer.Option(False, "--json", help="Output results as JSON"),
    fail_under: float = typer.Option(
        0.0,
        "--fail-under",
        help="Exit 1 if success rate is below this fraction (e.g. 0.9)",
    ),
) -> None:
    """Run the accuracy evaluation harness against known addresses."""
    import importlib.util
    import pathlib

    candidates = [
        pathlib.Path(__file__).parent.parent.parent.parent / "evals" / "run_eval.py",
        pathlib.Path(__file__).parent.parent.parent / "evals" / "run_eval.py",
    ]
    harness_path = next((p for p in candidates if p.exists()), None)
    if harness_path is None:
        typer.echo("Error: evals/run_eval.py not found. Run from the repo root.", err=True)
        raise typer.Exit(1)

    spec = importlib.util.spec_from_file_location("run_eval", harness_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    args = []
    if refresh:
        args += ["--refresh"]
    if limit:
        args += ["--limit", str(limit)]
    if json_output:
        args += ["--json"]
    if fail_under:
        args += ["--fail-under", str(fail_under)]

    from typer.testing import CliRunner as _Runner

    result = _Runner(mix_stderr=False).invoke(mod.app, args)
    typer.echo(result.output, nl=False)
    if result.exit_code != 0:
        raise typer.Exit(result.exit_code)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _err(msg: str, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps({"error": msg}), err=True)
    else:
        typer.echo(f"Error: {msg}", err=True)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
