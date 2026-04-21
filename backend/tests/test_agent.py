"""LangGraph agent tests — fully offline (all external calls mocked)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from zestimate_agent.agent import build_graph
from zestimate_agent.models import (
    AmbiguousAddressError,
    Candidate,
    ClarificationRequest,
    Confidence,
    DisambiguationChoice,
    NormalizedAddress,
    PropertyDetail,
    ProviderError,
    ResolvedProperty,
    ValidationError,
    ZestimateResult,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

ADDRESS = "101 Lombard St, San Francisco, CA 94111"


def _norm() -> NormalizedAddress:
    return NormalizedAddress(
        street_number="101",
        street_name="Lombard Street",
        city="San Francisco",
        state="CA",
        zip5="94111",
        confidence=Confidence.HIGH,
    )


def _resolved() -> ResolvedProperty:
    return ResolvedProperty(
        zpid="2101967478",
        matched_address="101 Lombard St, San Francisco, CA 94111",
        confidence=Confidence.HIGH,
    )


def _detail() -> PropertyDetail:
    return PropertyDetail(
        zpid_echo="2101967478",
        zestimate=799_900,
        full_address="101 Lombard St, San Francisco, CA 94111",
        raw={},
    )


def _result() -> ZestimateResult:
    return ZestimateResult(
        address="101 Lombard St, San Francisco, CA 94111",
        zestimate=799_900,
        zpid="2101967478",
        fetched_at=datetime.now(tz=timezone.utc),
        provider_used="direct",
        confidence=Confidence.HIGH,
    )


def _settings(max_retry: int = 2) -> MagicMock:
    s = MagicMock()
    s.proxy_url = None
    s.max_retry_attempts = max_retry
    return s


def _run(address: str = ADDRESS, max_retry: int = 2, **node_overrides):
    """Invoke the graph with all external calls mocked.

    node_overrides may be:
        normalize=..., resolve=..., fetch=..., validate=...
    each value is either a return value or a side_effect (Exception).
    """
    norm = node_overrides.get("normalize", _norm())
    resolved = node_overrides.get("resolve", _resolved())
    detail = node_overrides.get("fetch", _detail())
    result = node_overrides.get("validate", _result())

    def _side_or_return(v, name):
        m = MagicMock()
        if isinstance(v, BaseException) or (isinstance(v, type) and issubclass(v, BaseException)):
            m.side_effect = v
        else:
            m.return_value = v
        return m

    llm_pick = node_overrides.get("llm_pick", RuntimeError("_llm_pick not expected"))

    patches = [
        patch("zestimate_agent.agent.get_settings", return_value=_settings(max_retry)),
        patch("zestimate_agent.agent.normalize_address", _side_or_return(norm, "normalize")),
        patch("zestimate_agent.agent.resolve_zpid", _side_or_return(resolved, "resolve")),
        patch("zestimate_agent.agent.fetch_property", _side_or_return(detail, "fetch")),
        patch("zestimate_agent.agent.validate_result", _side_or_return(result, "validate")),
        patch("zestimate_agent.agent.DirectProvider"),
        patch("zestimate_agent.agent._llm_pick", _side_or_return(llm_pick, "llm_pick")),
    ]

    graph = build_graph()  # no checkpointer for tests
    import uuid
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    from zestimate_agent.models import GraphState

    initial: GraphState = {
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

    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
    ):
        return graph.invoke(initial, config=config)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_returns_result():
    state = _run()
    assert state["result"] is not None
    assert state["result"].zestimate == 799_900
    assert state["result"].zpid == "2101967478"
    assert state.get("clarification") is None


def test_happy_path_graph_nodes_visited():
    state = _run()
    path = state["graph_path"]
    assert "normalize" in path
    assert "resolve" in path
    assert "fetch" in path
    assert "validate" in path
    assert "finalize" in path
    assert "clarify" not in path
    assert "retry" not in path


def test_happy_path_attempt_unchanged():
    state = _run()
    assert state.get("attempt", 0) == 0


# ---------------------------------------------------------------------------
# Normalize failures
# ---------------------------------------------------------------------------


def test_normalize_ambiguous_address_error_returns_clarification():
    err = AmbiguousAddressError("too vague")
    state = _run(normalize=err)
    assert state.get("result") is None
    assert state["clarification"] is not None
    assert isinstance(state["clarification"], ClarificationRequest)
    assert "too vague" in state["clarification"].reason
    assert "clarify" in state["graph_path"]


def test_normalize_generic_error_retries_then_clarifies():
    # Two failures → retry budget (2) exhausted → clarify
    err = RuntimeError("Nominatim down")
    state = _run(normalize=err, max_retry=2)
    assert state.get("result") is None
    assert state["clarification"] is not None
    assert any(n.startswith("retry") for n in state["graph_path"])
    assert "clarify" in state["graph_path"]


# ---------------------------------------------------------------------------
# Resolve failures
# ---------------------------------------------------------------------------


def test_resolve_ambiguous_llm_error_routes_to_clarify():
    """When resolve is ambiguous and the LLM call fails, route to clarify."""
    err = AmbiguousAddressError(
        "two HIGH candidates",
        candidates=[
            Candidate(zpid="111", street_number="101", street_name="Lombard St", zip5="94111"),
            Candidate(zpid="222", street_number="101", street_name="Lombard St", zip5="94111"),
        ],
    )
    state = _run(resolve=err, llm_pick=RuntimeError("Mistral unavailable"))
    assert state.get("result") is None
    assert state["clarification"] is not None
    assert "disambiguate" in state["graph_path"]
    assert "clarify" in state["graph_path"]
    assert len(state["clarification"].candidates) == 2


def test_resolve_provider_error_retries_then_clarifies():
    err = ProviderError("HTTP 503")
    state = _run(resolve=err, max_retry=2)
    assert state.get("result") is None
    assert state["clarification"] is not None
    assert any(n.startswith("retry") for n in state["graph_path"])


# ---------------------------------------------------------------------------
# Fetch failures
# ---------------------------------------------------------------------------


def test_fetch_error_retries_then_clarifies():
    err = ProviderError("connection timeout")
    state = _run(fetch=err, max_retry=2)
    assert state.get("result") is None
    assert state["clarification"] is not None
    assert any(n.startswith("retry") for n in state["graph_path"])


# ---------------------------------------------------------------------------
# Validate failures
# ---------------------------------------------------------------------------


def test_validate_error_returns_clarification_without_retry():
    err = ValidationError("street_number mismatch", {})
    state = _run(validate=err)
    assert state.get("result") is None
    assert state["clarification"] is not None
    assert "validation" in state["clarification"].reason.lower() or "validate" in " ".join(
        state["graph_path"]
    )


# ---------------------------------------------------------------------------
# Retry budget
# ---------------------------------------------------------------------------


def test_retry_budget_1_allows_one_retry():
    """With max_retry=1, the first failure immediately exhausts budget."""
    err = ProviderError("timeout")
    state = _run(fetch=err, max_retry=1)
    assert state.get("result") is None
    assert state["clarification"] is not None
    # attempt should reach 1 (= max)
    assert state.get("attempt", 0) >= 1


def test_retry_budget_3_retries_twice_before_clarify():
    err = ProviderError("persistent error")
    state = _run(fetch=err, max_retry=3)
    assert state.get("result") is None
    assert state["clarification"] is not None
    assert state.get("attempt", 0) >= 2


# ---------------------------------------------------------------------------
# build_graph smoke test
# ---------------------------------------------------------------------------


def test_build_graph_compiles_without_error():
    graph = build_graph()
    assert graph is not None


def test_build_graph_with_checkpointer():
    from langgraph.checkpoint.memory import MemorySaver

    graph = build_graph(checkpointer=MemorySaver())
    assert graph is not None


# ---------------------------------------------------------------------------
# Provider rotation (_build_provider)
# ---------------------------------------------------------------------------


def test_build_provider_attempt_0_uses_chrome124_no_proxy():
    from zestimate_agent.agent import _build_provider

    s = _settings()
    s.proxy_url = None
    with patch("zestimate_agent.agent.DirectProvider") as mock_cls:
        _build_provider(0, s)
        mock_cls.assert_called_once_with(proxy_url=None, impersonation="chrome124")


def test_build_provider_attempt_1_rotates_impersonation():
    from zestimate_agent.agent import _build_provider

    s = _settings()
    s.proxy_url = None
    with patch("zestimate_agent.agent.DirectProvider") as mock_cls:
        _build_provider(1, s)
        mock_cls.assert_called_once_with(proxy_url=None, impersonation="chrome110")


def test_build_provider_attempt_1_enables_proxy_when_configured():
    from zestimate_agent.agent import _build_provider

    s = _settings()
    s.proxy_url = "http://proxy.example.com:8080"
    with patch("zestimate_agent.agent.DirectProvider") as mock_cls:
        _build_provider(1, s)
        mock_cls.assert_called_once_with(
            proxy_url="http://proxy.example.com:8080", impersonation="chrome110"
        )


def test_build_provider_attempt_0_never_uses_proxy():
    from zestimate_agent.agent import _build_provider

    s = _settings()
    s.proxy_url = "http://proxy.example.com:8080"  # configured but should be ignored at attempt 0
    with patch("zestimate_agent.agent.DirectProvider") as mock_cls:
        _build_provider(0, s)
        mock_cls.assert_called_once_with(proxy_url=None, impersonation="chrome124")


def test_build_provider_wraps_around_impersonation_list():
    from zestimate_agent.agent import _ROTATION_IMPERSONATIONS, _build_provider

    s = _settings()
    s.proxy_url = None
    n = len(_ROTATION_IMPERSONATIONS)
    with patch("zestimate_agent.agent.DirectProvider") as mock_cls:
        _build_provider(n, s)  # wraps back to index 0
        mock_cls.assert_called_once_with(proxy_url=None, impersonation=_ROTATION_IMPERSONATIONS[0])


# ---------------------------------------------------------------------------
# Mistral LLM disambiguation
# ---------------------------------------------------------------------------

_CANDIDATES = [
    Candidate(zpid="111", street_number="101", street_name="Lombard St", city="San Francisco", state="CA", zip5="94111"),
    Candidate(zpid="222", street_number="101", street_name="Lombard Ave", city="San Francisco", state="CA", zip5="94111"),
]

_AMBIGUOUS_ERR = AmbiguousAddressError("two HIGH candidates", candidates=_CANDIDATES)


def test_disambiguate_llm_picks_correct_candidate():
    """LLM returns a valid zpid → agent proceeds to fetch and returns a result."""
    choice = DisambiguationChoice(
        chosen_zpid="111",
        confidence=Confidence.HIGH,
        reasoning="Lombard St matches the query exactly.",
    )
    state = _run(resolve=_AMBIGUOUS_ERR, llm_pick=choice)
    assert state.get("result") is not None
    assert state["result"].zestimate == 799_900
    assert "disambiguate" in state["graph_path"]
    assert "fetch" in state["graph_path"]
    assert "finalize" in state["graph_path"]
    assert state.get("clarification") is None


def test_disambiguate_llm_unknown_zpid_returns_clarification():
    """LLM returns a zpid not in the candidate list → clarify."""
    choice = DisambiguationChoice(
        chosen_zpid="999",  # not in _CANDIDATES
        confidence=Confidence.HIGH,
        reasoning="Hallucinated zpid.",
    )
    state = _run(resolve=_AMBIGUOUS_ERR, llm_pick=choice)
    assert state.get("result") is None
    assert state["clarification"] is not None
    assert "disambiguate" in state["graph_path"]
    assert "clarify" in state["graph_path"]


def test_disambiguate_no_candidates_returns_clarification():
    """When candidates list is empty, disambiguate routes to clarify immediately."""
    err = AmbiguousAddressError("empty", candidates=[])
    state = _run(resolve=err, llm_pick=RuntimeError("should not be called"))
    assert state.get("result") is None
    assert state["clarification"] is not None


def test_disambiguate_low_confidence_still_proceeds():
    """LLM may return LOW confidence — agent still proceeds (validate will catch bad data)."""
    choice = DisambiguationChoice(
        chosen_zpid="111",
        confidence=Confidence.LOW,
        reasoning="Best guess only.",
    )
    state = _run(resolve=_AMBIGUOUS_ERR, llm_pick=choice)
    assert state.get("result") is not None
    assert state["resolved"].confidence == Confidence.LOW
