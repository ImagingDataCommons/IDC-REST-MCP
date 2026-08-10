"""A dropped filter must never look like an answer.

Both adapters used to accept a mis-shaped filter body, ignore every predicate, and return the
whole archive at HTTP 200 — indistinguishable from a legitimately huge cohort. These tests pin
the two halves of the fix: a mis-shaped filter is a hard error, and any filter that compiles to
no predicates says so in the response.
"""

from __future__ import annotations

import pytest

from idc_api.core.filters import compile_filters
from idc_api.core.models import CohortFilters
from idc_api.mcp.server import mcp

_TERMS = {"collection_id": ["rider_pilot"]}
# Below the real archive (>1M series) but far above any single test collection: the number a
# dropped filter would produce.
_ALL_OF_IDC = 1_000_000


# --- the shape mistakes are hard errors ----------------------------------------------------


def test_wrapped_filter_body_rejected_by_bare_endpoints(client):
    """`/cohort/counts` and `/licenses` take the filter object directly; the wrapped shape is a
    422 that names the right one, not a 200 covering all of IDC."""
    for path in ("/v3/cohort/counts", "/v3/licenses"):
        r = client.post(path, json={"filters": {"terms": _TERMS}})
        assert r.status_code == 422, path
        assert "do not wrap it" in r.text, path


def test_bare_filter_body_rejected_by_wrapped_endpoints(client):
    """The mirror image: `manifest`, `manifest.txt` and `citations` take it under `filters`."""
    for path in ("/v3/cohort/manifest", "/v3/cohort/manifest.txt", "/v3/citations"):
        r = client.post(path, json={"terms": _TERMS})
        assert r.status_code == 422, path
        assert "under `filters`" in r.text, path


def test_misspelled_filter_keys_rejected(client):
    """Any unrecognized key is refused rather than ignored — `term`/`min` would each have
    silently compiled to no predicate at all."""
    for body in ({"term": _TERMS}, {"terms": _TERMS, "junk": 1}):
        assert client.post("/v3/cohort/counts", json=body).status_code == 422, body
    bad_range = {"ranges": {"instanceCount": {"min": 5}}}
    assert client.post("/v3/cohort/counts", json=bad_range).status_code == 422


# --- what survived compilation is always reported ------------------------------------------


def test_counts_echo_the_filter_they_applied(client):
    body = {"terms": _TERMS, "ranges": {"instanceCount": {"gte": 2}}}
    r = client.post("/v3/cohort/counts", json=body).json()
    assert r["series"] > 0
    assert r["filters_applied"]["terms"] == _TERMS
    assert r["filters_applied"]["ranges"] == {"instanceCount": {"gte": 2.0, "lte": None}}
    assert r["warnings"] == []


def test_empty_filter_says_it_covers_everything(client):
    """An explicitly empty filter stays legal — it is how you ask about the whole archive — but
    it can no longer be mistaken for a cohort."""
    r = client.post("/v3/cohort/counts", json={}).json()
    assert r["series"] > _ALL_OF_IDC
    assert r["filters_applied"] == {"terms": {}, "ranges": {}}
    assert any("ENTIRE IDC archive" in w for w in r["warnings"])


def test_predicates_that_constrain_nothing_are_reported(client):
    """An empty value list is dropped by the compiler; unreported, it is the same footgun with a
    correctly-shaped body."""
    r = client.post("/v3/cohort/counts", json={"terms": {"collection_id": []}}).json()
    assert r["series"] > _ALL_OF_IDC
    assert r["filters_applied"]["terms"] == {}
    assert any("'collection_id' was ignored" in w for w in r["warnings"])
    assert any("ENTIRE IDC archive" in w for w in r["warnings"])


def test_licenses_echo_the_filter_they_applied(client):
    r = client.post("/v3/licenses", json={"terms": _TERMS}).json()
    assert r["licenses"] and r["filters_applied"]["terms"] == _TERMS
    assert r["warnings"] == []


def test_unfiltered_download_payload_warns_before_the_commands(client):
    """The manifest is where an unfiltered selection does real damage, so the caveat rides on
    the download note the agent reads out, not only in counts.warnings."""
    r = client.post("/v3/cohort/manifest", json={"filters": {}, "include_rows": False}).json()
    assert r["download"]["note"].startswith("WARNING: no filter was applied")
    assert any("ENTIRE IDC archive" in w for w in r["counts"]["warnings"])

    filtered = client.post(
        "/v3/cohort/manifest", json={"filters": {"terms": _TERMS}, "page_size": 1}
    ).json()
    assert not filtered["download"]["note"].startswith("WARNING")
    assert filtered["counts"]["warnings"] == []


# --- core + MCP see the same guarantees ----------------------------------------------------


def test_compiler_reports_what_it_dropped():
    ok = compile_filters(CohortFilters(terms=_TERMS))
    assert ok.where == '"collection_id" IN (?)' and ok.params == ["rider_pilot"]
    assert ok.applied.terms == _TERMS and ok.warnings == []

    dropped = compile_filters(
        CohortFilters(terms={"collection_id": []}, ranges={"instanceCount": {}})
    )
    assert dropped.where == "TRUE"  # selects everything, so it must be flagged
    assert dropped.applied.terms == {} and dropped.applied.ranges == {}
    assert len(dropped.warnings) == 3  # both dropped predicates, plus "no filter applied"


async def test_mcp_build_cohort_without_filters_warns(parse_mcp):
    out = parse_mcp(await mcp.call_tool("build_cohort", {"page_size": 1}))
    assert out["total_series"] > _ALL_OF_IDC
    assert any("ENTIRE IDC archive" in w for w in out["counts"]["warnings"])


async def test_mcp_malformed_filter_argument_is_actionable():
    """A rejected filter must reach the model as the expected shape — `guard`'s generic
    "Internal error" would tell it nothing to act on."""
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError) as exc:
        await mcp.call_tool("build_cohort", {"ranges": {"instanceCount": {"min": 5}}})
    assert "Malformed filter arguments" in str(exc.value)
    assert "gte" in str(exc.value)
