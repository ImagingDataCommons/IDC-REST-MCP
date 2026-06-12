"""Contract parity: core service, REST endpoint, and MCP tool agree for the same filter.

This is the guarantee that the two adapters stay in sync because they share one core.
"""

from __future__ import annotations

from idc_api.core.models import CohortFilters
from idc_api.mcp.server import mcp

_TERMS = {"collection_id": ["rider_pilot"], "Modality": ["CT"]}


async def test_counts_parity(ctx, client, parse_mcp):
    core_series = ctx.cohort.counts(CohortFilters(terms=_TERMS)).series

    rest_series = client.post("/v3/cohort/counts", json={"terms": _TERMS}).json()["series"]

    mcp_series = parse_mcp(await mcp.call_tool("build_cohort", {"terms": _TERMS}))["total_series"]

    assert core_series == rest_series == mcp_series > 0


def _fake_doi_get(url, headers=None, timeout=None):
    """Stub DOI content negotiation so citation tests don't touch the network."""

    class _Resp:
        status_code = 200
        text = "FAKE CITATION"

        @staticmethod
        def json():
            return {"id": "fake"}

    return _Resp()


async def test_citations_parity_and_idc_acknowledgment(ctx, client, parse_mcp, monkeypatch):
    import idc_api.core.services.citations as cite_mod

    monkeypatch.setattr(cite_mod.requests, "get", _fake_doi_get)
    terms = {"collection_id": ["rider_pilot"]}

    core = ctx.citations.get_citations(CohortFilters(terms=terms)).model_dump(mode="json")
    rest = client.post("/v3/citations", json={"filters": {"terms": terms}}).json()
    mcp_out = parse_mcp(await mcp.call_tool("get_citations", {"terms": terms}))

    # Same model serialized by both adapters (every stubbed citation is identical, so list
    # ordering can't make this spuriously fail).
    assert core == rest == mcp_out
    # The IDC paper is surfaced separately from the per-dataset citations, with guidance.
    assert core["idc_acknowledgment"] == "FAKE CITATION"
    assert core["citations"]  # per-dataset citations present, distinct from idc_acknowledgment
    assert "10.1148/rg.230180" in core["recommendation"]
