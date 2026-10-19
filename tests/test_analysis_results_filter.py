"""Cohort filters can keep or drop analysis results (#43).

Analysis result series carry the collection_id of the images they analyze, so a collection_id
filter alone returns both, and no term value means "no analysis result": original data has a NULL
analysis_result_id. ``analysis_results`` ('include' / 'exclude' / 'only', as in idc-index's
download_from_selection) is the cohort-level switch, and ``get_attribute_values`` reports the
NULLs it cannot list as values.
"""

from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from idc_api.core.models import CohortFilters
from idc_api.mcp.server import mcp

# rider_pilot has original series and a qiba_volct_1b analysis result, and is small.
_TERMS = {"collection_id": ["rider_pilot"]}


def _series(ctx, mode: str) -> int:
    return ctx.cohort.counts(CohortFilters(terms=_TERMS, analysis_results=mode)).series


def test_exclude_and_only_partition_the_cohort(ctx):
    include, exclude, only = (_series(ctx, m) for m in ("include", "exclude", "only"))
    assert exclude > 0 and only > 0
    assert include == exclude + only

    original = ctx.query.run_sql(
        "SELECT count(*) AS n FROM index WHERE collection_id = ? AND analysis_result_id IS NULL",
        ["rider_pilot"],
    ).rows[0]["n"]
    assert exclude == original


async def test_rest_and_mcp_apply_and_echo_it(ctx, client, parse_mcp):
    expected = _series(ctx, "exclude")

    rest = client.post(
        "/v3/cohort/counts", json={"filters": {"terms": _TERMS, "analysis_results": "exclude"}}
    ).json()
    mcp_out = parse_mcp(
        await mcp.call_tool("build_cohort", {"terms": _TERMS, "analysis_results": "exclude"})
    )
    assert rest["series"] == mcp_out["total_series"] == expected
    assert rest["filters_applied"]["analysis_results"] == "exclude"
    assert mcp_out["counts"]["filters_applied"]["analysis_results"] == "exclude"

    urls = client.post(
        "/v3/cohort/manifest.txt",
        json={"filters": {"terms": _TERMS, "analysis_results": "exclude"}, "limit": 10_000},
    ).text.split()
    assert len(urls) == expected


def test_download_command_carries_the_split(client):
    def commands(mode):
        body = {"filters": {"terms": _TERMS, "analysis_results": mode}, "include_rows": False}
        return client.post("/v3/cohort/manifest", json=body).json()["download"]["idc_commands"]

    assert "idc download rider_pilot --download-dir ./idc-data" in commands("include")
    # `idc download <collection>` would fetch the analysis results too.
    assert (
        "idc download-from-selection --collection-id rider_pilot --analysis-results exclude "
        "--download-dir ./idc-data"
    ) in commands("exclude")


async def test_exclude_with_an_analysis_result_id_is_refused(client):
    """Always empty; a zero would read as "no such data"."""
    filters = {"terms": {"analysis_result_id": ["qiba_volct_1b"]}, "analysis_results": "exclude"}
    r = client.post("/v3/cohort/counts", json={"filters": filters})
    assert r.status_code == 400
    assert "cannot be combined" in r.json()["error"]["message"]
    with pytest.raises(ToolError, match="cannot be combined"):
        await mcp.call_tool("build_cohort", filters)


async def test_it_is_not_a_predicate_on_its_own(client):
    """Either side of the split is about half of IDC, so it does not unlock enumeration."""
    r = client.post("/v3/cohort/manifest", json={"filters": {"analysis_results": "only"}})
    assert r.status_code == 400
    assert "not a predicate" in r.json()["error"]["message"]
    with pytest.raises(ToolError, match="not a predicate"):
        await mcp.call_tool("get_cohort_urls", {"analysis_results": "exclude"})


async def test_unknown_mode_is_rejected(client):
    r = client.post(
        "/v3/cohort/counts", json={"filters": {"terms": _TERMS, "analysis_results": "none"}}
    )
    assert r.status_code == 422
    with pytest.raises(ToolError):
        await mcp.call_tool("build_cohort", {"terms": _TERMS, "analysis_results": "none"})


def test_top_level_analysis_results_names_the_fix(client):
    r = client.post("/v3/cohort/counts", json={"analysis_results": "exclude"})
    assert r.status_code == 422
    assert "goes under `filters`" in r.text


async def test_attribute_values_report_the_nulls(ctx, client, parse_mcp):
    """Before, the original data (over half of `index`) was simply absent from the response."""
    total = ctx.discovery.stats().series
    full = ctx.discovery.get_attribute_values("analysis_result_id", limit=10_000)
    assert not full.truncated
    assert full.null_count > 0
    assert full.null_count + sum(v.count for v in full.values) == total
    assert "analysis_results" in full.note

    rest = client.get("/v3/attributes/analysis_result_id/values?limit=3").json()
    mcp_out = parse_mcp(
        await mcp.call_tool("get_attribute_values", {"attribute": "analysis_result_id", "limit": 3})
    )
    assert rest["null_count"] == mcp_out["null_count"] == full.null_count

    # A column with no NULLs says so.
    assert ctx.discovery.get_attribute_values("collection_id", limit=1).null_count == 0
