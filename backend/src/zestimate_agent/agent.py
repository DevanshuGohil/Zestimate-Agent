"""LangGraph agent: orchestrates the 4-stage Zestimate pipeline.

Graph nodes
-----------
normalize   → Stage 1: raw address → NormalizedAddress
resolve     → Stage 2: NormalizedAddress → ResolvedProperty (zpid)
disambiguate→ LLM disambiguation when resolve finds multiple HIGH-confidence matches
fetch       → Stage 3: zpid → PropertyDetail
validate    → Stage 4: cross-checks and builds ZestimateResult
retry       → increments attempt counter; routes back to failed stage or to clarify
clarify     → terminal failure node; populates ClarificationRequest
finalize    → terminal success node; logs result

Entry: normalize   Terminals: clarify, finalize (both → END)

Public API
----------
  run_agent(address)  → ZestimateResult | ClarificationRequest
  build_graph(checkpointer=None)  → compiled CompiledStateGraph
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mistralai import ChatMistralAI
from langgraph.graph import END, StateGraph

from .config import get_settings
from .fetch import fetch_property
from .models import (
    AmbiguousAddressError,
    Candidate,
    ClarificationRequest,
    DisambiguationChoice,
    GraphState,
    NoZestimateError,
    ProviderError,
    ResolvedProperty,
    ValidationError,
    ZestimateResult,
)
from .normalize import normalize_address
from .providers.direct import DirectProvider
from .providers.rapidapi import RapidAPIProvider
from .resolve import resolve_zpid
from .validate import validate_result

log = structlog.get_logger(__name__)

_ROTATION_IMPERSONATIONS = ["chrome124", "chrome110", "chrome107", "safari15_5_4"]


def _build_provider(attempt: int, settings: Any) -> DirectProvider | RapidAPIProvider:
    """Return a provider for the given retry attempt.

    attempt 0-1: DirectProvider (primary; reads exact live Zillow HTML)
    attempt 2+:  RapidAPIProvider fallback when DirectProvider is blocked
    """
    if attempt >= 2:
        try:
            key = settings.rapidapi_key.get_secret_value()
            if key:
                log.info("agent.provider_rotation.rapidapi", attempt=attempt)
                return RapidAPIProvider(api_key=key, host=settings.rapidapi_host)
        except Exception:
            pass

    impersonation = _ROTATION_IMPERSONATIONS[attempt % len(_ROTATION_IMPERSONATIONS)]
    raw_proxy = settings.proxy_url
    proxy_url = (raw_proxy.get_secret_value() if raw_proxy else None) if attempt >= 1 else None
    log.debug(
        "agent.provider_rotation.direct",
        attempt=attempt,
        impersonation=impersonation,
        proxy=bool(proxy_url),
    )
    return DirectProvider(proxy_url=proxy_url, impersonation=impersonation)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _path(state: GraphState, label: str) -> list[str]:
    return (state.get("graph_path") or []) + [label]


def _errors(state: GraphState, msg: str) -> list[str]:
    return (state.get("errors") or []) + [msg]


def _fmt_candidate(c: Candidate) -> str:
    parts = [f"{c.street_number} {c.street_name}".strip()] if c.street_name else []
    if c.city:
        parts.append(c.city)
    tail = " ".join(filter(None, [c.state, c.zip5]))
    if tail:
        parts.append(tail)
    return ", ".join(parts) or c.zpid


# ---------------------------------------------------------------------------
# Nodes (all async)
# ---------------------------------------------------------------------------


async def normalize_node(state: GraphState) -> dict[str, Any]:
    path = _path(state, "normalize")
    try:
        settings = get_settings()
        normalized = await normalize_address(state["input_address"], settings)
        log.info("agent.normalize.ok", address=normalized.single_line())
        return {"normalized": normalized, "failed_at": None, "graph_path": path}
    except AmbiguousAddressError as e:
        log.warning("agent.normalize.ambiguous", error=str(e))
        return {
            "errors": _errors(state, f"normalize: {e}"),
            "clarification": ClarificationRequest(
                reason=str(e),
                original_input=state.get("input_address", ""),
                candidates=[],
            ),
            "graph_path": path,
        }
    except Exception as e:
        log.warning("agent.normalize.error", error=str(e))
        return {
            "errors": _errors(state, f"normalize: {e}"),
            "failed_at": "normalize",
            "graph_path": path,
        }


async def resolve_node(state: GraphState) -> dict[str, Any]:
    path = _path(state, "resolve")
    normalized = state["normalized"]
    try:
        settings = get_settings()
        provider = _build_provider(state.get("attempt") or 0, settings)
        resolved = await resolve_zpid(normalized, provider)
        log.info("agent.resolve.ok", zpid=resolved.zpid, confidence=resolved.confidence)
        return {
            "resolved": resolved,
            "provider_used": provider.name,
            "failed_at": None,
            "graph_path": path,
        }
    except AmbiguousAddressError as e:
        log.warning("agent.resolve.ambiguous", count=len(e.candidates))
        return {
            "errors": _errors(state, f"resolve: {e}"),
            "candidates": e.candidates,
            "failed_at": "resolve_ambiguous",
            "graph_path": path,
        }
    except (ProviderError, Exception) as e:
        log.warning("agent.resolve.error", error=str(e))
        return {
            "errors": _errors(state, f"resolve: {e}"),
            "failed_at": "resolve",
            "graph_path": path,
        }


async def disambiguate_node(state: GraphState) -> dict[str, Any]:
    """LLM disambiguation: calls Mistral to pick the best candidate zpid."""
    path = _path(state, "disambiguate")
    candidates = state.get("candidates") or []
    input_address = state.get("input_address", "")

    if not candidates:
        return {
            "clarification": ClarificationRequest(
                reason="No candidates available for disambiguation.",
                original_input=input_address,
                candidates=[],
            ),
            "graph_path": path,
        }

    try:
        settings = get_settings()
        choice = await _llm_pick(input_address, candidates, settings)

        chosen = next((c for c in candidates if c.zpid == choice.chosen_zpid), None)
        if chosen is None:
            raise ValueError(f"LLM chose unknown zpid {choice.chosen_zpid!r}")

        resolved = ResolvedProperty(
            zpid=choice.chosen_zpid,
            matched_address=_fmt_candidate(chosen),
            confidence=choice.confidence,
        )
        log.info(
            "agent.disambiguate.ok",
            zpid=resolved.zpid,
            confidence=resolved.confidence,
            reasoning=choice.reasoning,
        )
        return {"resolved": resolved, "failed_at": None, "graph_path": path}

    except Exception as e:
        log.warning("agent.disambiguate.error", error=str(e))
        return {
            "clarification": ClarificationRequest(
                reason=f"LLM disambiguation failed: {e}",
                original_input=input_address,
                candidates=[{"zpid": c.zpid, "address": _fmt_candidate(c)} for c in candidates],
            ),
            "graph_path": path,
        }


async def _llm_pick(
    input_address: str,
    candidates: list[Candidate],
    settings: Any,
) -> DisambiguationChoice:
    """Call Mistral with structured output to pick the best matching candidate."""
    llm = ChatMistralAI(
        model="mistral-small-latest",
        temperature=0,
        mistral_api_key=settings.mistral_api_key.get_secret_value(),
    )
    structured_llm = llm.with_structured_output(DisambiguationChoice)

    candidates_text = "\n".join(
        f"  - zpid={c.zpid}: {_fmt_candidate(c)}" for c in candidates
    )
    messages = [
        SystemMessage(
            content=(
                "You are a real estate address disambiguation assistant. "
                "Given a query address and a list of candidate properties, "
                "identify which candidate best matches the query. "
                "Return the zpid of the chosen candidate, your confidence "
                "(HIGH, MEDIUM, or LOW), and a brief one-sentence reasoning."
            )
        ),
        HumanMessage(
            content=(
                f"Query address: {input_address}\n\n"
                f"Candidates:\n{candidates_text}\n\n"
                "Which candidate zpid best matches the query address?"
            )
        ),
    ]
    result = await structured_llm.ainvoke(messages)
    return result  # type: ignore[return-value]


async def fetch_node(state: GraphState) -> dict[str, Any]:
    path = _path(state, "fetch")
    resolved = state["resolved"]
    try:
        settings = get_settings()
        provider = _build_provider(state.get("attempt") or 0, settings)
        detail = await fetch_property(resolved.zpid, provider)
        log.info("agent.fetch.ok", zpid=resolved.zpid, zestimate=detail.zestimate)
        return {"property_detail": detail, "failed_at": None, "graph_path": path}
    except (ProviderError, Exception) as e:
        log.warning("agent.fetch.error", error=str(e))
        return {
            "errors": _errors(state, f"fetch: {e}"),
            "failed_at": "fetch",
            "graph_path": path,
        }


async def validate_node(state: GraphState) -> dict[str, Any]:
    path = _path(state, "validate")
    try:
        provider_name = state.get("provider_used") or "direct"
        result = validate_result(
            state["normalized"],
            state["property_detail"],
            state["resolved"],
            provider_name,
        )
        log.info("agent.validate.ok", zestimate=result.zestimate)
        return {"result": result, "failed_at": None, "graph_path": path}
    except NoZestimateError as e:
        log.warning("agent.validate.no_zestimate", zpid=e.zpid, error=str(e))
        return {
            "errors": _errors(state, f"no_zestimate: {e}"),
            "clarification": ClarificationRequest(
                reason=str(e),
                original_input=state.get("input_address", ""),
                candidates=[],
                zpid=e.zpid,
            ),
            "graph_path": path,
        }
    except ValidationError as e:
        log.warning("agent.validate.error", error=str(e))
        return {
            "errors": _errors(state, f"validate: {e}"),
            "clarification": ClarificationRequest(
                reason=f"Validation failed: {e}",
                original_input=state.get("input_address", ""),
                candidates=[],
            ),
            "graph_path": path,
        }
    except Exception as e:
        log.warning("agent.validate.error", error=str(e))
        return {
            "errors": _errors(state, f"validate: {e}"),
            "clarification": ClarificationRequest(
                reason=f"Unexpected validation error: {e}",
                original_input=state.get("input_address", ""),
                candidates=[],
            ),
            "graph_path": path,
        }


async def retry_node(state: GraphState) -> dict[str, Any]:
    attempt = (state.get("attempt") or 0) + 1
    path = _path(state, f"retry({attempt})")
    settings = get_settings()
    max_attempts = settings.max_retry_attempts

    log.info("agent.retry", attempt=attempt, max=max_attempts, failed_at=state.get("failed_at"))

    if attempt >= max_attempts:
        errors = state.get("errors") or []
        return {
            "attempt": attempt,
            "clarification": ClarificationRequest(
                reason=f"Max retries ({max_attempts}) exceeded: "
                + "; ".join(errors[-3:]),
                original_input=state.get("input_address", ""),
                candidates=[],
            ),
            "graph_path": path,
        }
    return {"attempt": attempt, "graph_path": path}


async def clarify_node(state: GraphState) -> dict[str, Any]:
    path = _path(state, "clarify")
    if not state.get("clarification"):
        errors = state.get("errors") or []
        clarification = ClarificationRequest(
            reason="; ".join(errors) if errors else "Unknown error",
            original_input=state.get("input_address", ""),
            candidates=[],
        )
        log.warning("agent.clarify", reason=clarification.reason)
        return {"clarification": clarification, "graph_path": path}
    log.warning("agent.clarify", reason=state["clarification"].reason)
    return {"graph_path": path}


async def finalize_node(state: GraphState) -> dict[str, Any]:
    path = _path(state, "finalize")
    result = state.get("result")
    if result:
        log.info(
            "agent.finalize",
            address=result.address,
            zestimate=result.zestimate,
            confidence=result.confidence,
        )
    return {"graph_path": path}


# ---------------------------------------------------------------------------
# Routing (conditional edges — pure, sync)
# ---------------------------------------------------------------------------


def _route_normalize(state: GraphState) -> str:
    if state.get("clarification"):
        return "clarify"
    if state.get("normalized") is None:
        return "retry"
    return "resolve"


def _route_resolve(state: GraphState) -> str:
    if state.get("clarification"):
        return "clarify"
    failed_at = state.get("failed_at")
    if failed_at == "resolve_ambiguous":
        return "disambiguate"
    if failed_at == "resolve":
        return "retry"
    return "fetch"


def _route_disambiguate(state: GraphState) -> str:
    if state.get("clarification"):
        return "clarify"
    if state.get("resolved"):
        return "fetch"
    return "clarify"


def _route_fetch(state: GraphState) -> str:
    if state.get("clarification"):
        return "clarify"
    if state.get("property_detail") is None:
        return "retry"
    return "validate"


def _route_validate(state: GraphState) -> str:
    if state.get("clarification"):
        return "clarify"
    if state.get("result"):
        return "finalize"
    return "retry"


def _route_retry(state: GraphState) -> str:
    if state.get("clarification"):
        return "clarify"
    failed_at = state.get("failed_at")
    if failed_at == "normalize":
        return "normalize"
    if failed_at == "resolve":
        return "resolve"
    if failed_at == "fetch":
        return "fetch"
    return "clarify"


# ---------------------------------------------------------------------------
# Graph compilation
# ---------------------------------------------------------------------------


def build_graph(checkpointer=None):  # type: ignore[no-untyped-def]
    """Build and compile the Zestimate StateGraph."""
    builder: StateGraph = StateGraph(GraphState)

    builder.add_node("normalize", normalize_node)
    builder.add_node("resolve", resolve_node)
    builder.add_node("disambiguate", disambiguate_node)
    builder.add_node("fetch", fetch_node)
    builder.add_node("validate", validate_node)
    builder.add_node("retry", retry_node)
    builder.add_node("clarify", clarify_node)
    builder.add_node("finalize", finalize_node)

    builder.set_entry_point("normalize")

    builder.add_conditional_edges(
        "normalize",
        _route_normalize,
        {"resolve": "resolve", "retry": "retry", "clarify": "clarify"},
    )
    builder.add_conditional_edges(
        "resolve",
        _route_resolve,
        {"fetch": "fetch", "disambiguate": "disambiguate", "retry": "retry", "clarify": "clarify"},
    )
    builder.add_conditional_edges(
        "disambiguate",
        _route_disambiguate,
        {"fetch": "fetch", "clarify": "clarify"},
    )
    builder.add_conditional_edges(
        "fetch",
        _route_fetch,
        {"validate": "validate", "retry": "retry", "clarify": "clarify"},
    )
    builder.add_conditional_edges(
        "validate",
        _route_validate,
        {"finalize": "finalize", "retry": "retry", "clarify": "clarify"},
    )
    builder.add_conditional_edges(
        "retry",
        _route_retry,
        {
            "normalize": "normalize",
            "resolve": "resolve",
            "fetch": "fetch",
            "clarify": "clarify",
        },
    )

    builder.add_edge("clarify", END)
    builder.add_edge("finalize", END)

    return builder.compile(checkpointer=checkpointer)


_graph = None


def _get_graph():  # type: ignore[no-untyped-def]
    global _graph
    if _graph is None:
        _graph = build_graph(checkpointer=None)
    return _graph


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


NODE_LABELS: dict[str, str] = {
    "normalize": "Normalizing address",
    "resolve": "Searching Zillow",
    "disambiguate": "Disambiguating with AI",
    "fetch": "Fetching property details",
    "validate": "Validating result",
    "retry": "Retrying",
    "clarify": "Processing error",
    "finalize": "Complete",
}

_NODE_NAMES = frozenset(NODE_LABELS)


def _step_detail(node: str, accumulated: dict[str, Any]) -> dict[str, Any] | None:
    """Extract a human-readable output summary for a just-completed node."""
    try:
        if node == "normalize":
            norm = accumulated.get("normalized")
            if norm:
                return {"address": norm.single_line(), "confidence": norm.confidence.value}

        elif node in ("resolve", "disambiguate"):
            res = accumulated.get("resolved")
            if res:
                return {
                    "zpid": res.zpid,
                    "matched": res.matched_address,
                    "confidence": res.confidence.value,
                }

        elif node == "fetch":
            pd = accumulated.get("property_detail")
            if pd:
                return {
                    "zpid": pd.zpid_echo,
                    "zestimate": pd.zestimate,
                    "address": pd.full_address,
                }

        elif node == "validate":
            result = accumulated.get("result")
            if result:
                return {"zestimate": result.zestimate, "confidence": result.confidence.value}
            cr = accumulated.get("clarification")
            if cr:
                return {"reason": cr.reason[:150]}

        elif node == "retry":
            errors = accumulated.get("errors") or []
            return {
                "attempt": accumulated.get("attempt"),
                "failed_at": accumulated.get("failed_at"),
                "last_error": errors[-1][:120] if errors else "",
            }

        elif node == "clarify":
            cr = accumulated.get("clarification")
            if cr:
                return {"reason": cr.reason[:150]}

        elif node == "finalize":
            result = accumulated.get("result")
            if result:
                return {"zestimate": result.zestimate, "address": result.address}

    except Exception:
        pass
    return None


async def stream_agent(address: str) -> AsyncGenerator[dict[str, Any], None]:
    """Yield real-time progress events for each LangGraph node.

    Event shapes:
      {"type": "step",    "node": str, "status": "running"|"done"|"error", "label": str}
      {"type": "result",  "_result": ZestimateResult}
      {"type": "clarify", "_clarification": ClarificationRequest}
    """
    graph = _get_graph()
    initial_state: GraphState = {
        "input_address": address,
        "normalized": None,
        "candidates": [],
        "resolved": None,
        "property_detail": None,
        "result": None,
        "clarification": None,
        "provider_used": None,
        "errors": [],
        "messages": [],
        "attempt": 0,
        "failed_at": None,
        "graph_path": [],
    }

    accumulated: dict[str, Any] = {}

    async for event in graph.astream_events(initial_state, version="v2"):
        kind = event["event"]
        # Accept node name from event["name"] or metadata["langgraph_node"] as fallback
        raw_name = event.get("name", "")
        meta_name = event.get("metadata", {}).get("langgraph_node", "")
        name = raw_name if raw_name in _NODE_NAMES else (meta_name if meta_name in _NODE_NAMES else "")
        if not name:
            continue

        label = NODE_LABELS[name]

        if kind == "on_chain_start":
            yield {"type": "step", "node": name, "status": "running", "label": label}

        elif kind == "on_chain_end":
            output = event.get("data", {}).get("output", {})
            if isinstance(output, dict):
                accumulated.update(output)

            has_failure = isinstance(output, dict) and bool(output.get("failed_at"))
            detail = _step_detail(name, accumulated)
            step_event: dict[str, Any] = {
                "type": "step",
                "node": name,
                "status": "error" if has_failure else "done",
                "label": label,
            }
            if detail:
                step_event["detail"] = detail
            yield step_event

            if name == "finalize":
                yield {"type": "result", "_result": accumulated.get("result")}
                return
            if name == "clarify":
                yield {"type": "clarify", "_clarification": accumulated.get("clarification")}
                return


async def run_agent(address: str) -> ZestimateResult | ClarificationRequest:
    """Run the full LangGraph pipeline for the given US property address."""
    graph = _get_graph()
    initial_state: GraphState = {
        "input_address": address,
        "normalized": None,
        "candidates": [],
        "resolved": None,
        "property_detail": None,
        "result": None,
        "clarification": None,
        "provider_used": None,
        "errors": [],
        "messages": [],
        "attempt": 0,
        "failed_at": None,
        "graph_path": [],
    }
    final_state = await graph.ainvoke(initial_state)

    if final_state.get("result"):
        return final_state["result"]
    if final_state.get("clarification"):
        return final_state["clarification"]

    errors = final_state.get("errors") or []
    return ClarificationRequest(
        reason="; ".join(errors) if errors else "Agent terminated without result",
        original_input=address,
        candidates=[],
    )
