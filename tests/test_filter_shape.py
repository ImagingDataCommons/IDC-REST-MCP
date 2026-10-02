"""A dropped filter must never look like an answer.

Filter bodies used to come in two shapes — bare for counts/licenses, wrapped for
manifest/citations — and sending one where the other was expected validated cleanly, dropped
every predicate, and returned the whole archive at HTTP 200, indistinguishable from a
legitimately huge cohort. These tests pin the three parts of the fix: one uniform shape whose
violations are hard errors, a filter that compiles to nothing reported as such, and a flat
refusal to enumerate series without a predicate.
"""

from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from idc_api.core.filters import compile_filters, require_filter
from idc_api.core.models import CohortFilters
from idc_api.mcp.server import mcp

_TERMS = {"collection_id": ["rider_pilot"]}
# Below the real archive (>1M series) but far above any single test collection: the number a
# dropped filter would produce.
_ALL_OF_IDC = 1_000_000

# Every filter-taking body, in the one shape they all now share.
_WRAPPED = ("/v3/cohort/counts", "/v3/licenses", "/v3/cohort/manifest", "/v3/citations")


# --- one shape, and violations are hard errors ----------------------------------------------


def test_every_filter_endpoint_takes_the_same_shape(client):
    for path in _WRAPPED:
        r = client.post(path, json={"filters": {"terms": _TERMS}})
        assert r.status_code == 200, (path, r.text[:200])


def test_bare_filter_body_is_rejected_everywhere(client):
    """The mistake that used to return all of IDC at 200 is now a 422 naming the fix."""
    for path in (*_WRAPPED, "/v3/cohort/manifest.txt"):
        r = client.post(path, json={"terms": _TERMS})
        assert r.status_code == 422, (path, r.text[:200])
        assert "goes under `filters`" in r.text, path


def test_misspelled_filter_keys_are_rejected(client):
    """An unrecognized key is refused rather than ignored — each of these would otherwise have
    compiled to no predicate at all."""
    for body in (
        {"filters": {"term": _TERMS}},
        {"filters": {"terms": _TERMS}, "junk": 1},
        {"filters": {"terms": _TERMS, "ranges": {}, "extra": 1}},
        {"filters": {"ranges": {"instanceCount": {"min": 5}}}},
    ):
        assert client.post("/v3/cohort/counts", json=body).status_code == 422, body


def test_non_numeric_bound_on_numeric_range_is_a_400(client):
    """Range bounds admit strings (the date columns are strings); against a numeric column a
    non-numeric one used to fail inside DuckDB's cast and surface as a 500."""
    for attr in ("instanceCount", "series_size_MB"):
        body = {"filters": {"terms": _TERMS, "ranges": {attr: {"gte": "1 AND 1=1"}}}}
        r = client.post("/v3/cohort/counts", json=body)
        assert r.status_code == 400, (attr, r.text[:200])
        assert "is not a number" in r.json()["error"]["message"]

    # A numeric string is still a number, and is echoed as one.
    body = {"filters": {"terms": _TERMS, "ranges": {"instanceCount": {"gte": "2"}}}}
    r = client.post("/v3/cohort/counts", json=body).json()
    assert r["filters_applied"]["ranges"] == {"instanceCount": {"gte": 2.0, "lte": None}}

    # NaN parses as a float but matches nothing; it is refused like any other non-number.
    body = {"filters": {"terms": _TERMS, "ranges": {"instanceCount": {"gte": "nan"}}}}
    assert client.post("/v3/cohort/counts", json=body).status_code == 400


def test_date_range_bounds_must_be_dates(client):
    """The date columns are strings, so a non-date bound used to compare lexically and answer
    with a plausible-looking zero instead of an error."""
    for bad in ("nope", "01/31/2020", "2020", 20200101):
        body = {"filters": {"terms": _TERMS, "ranges": {"StudyDate": {"gte": bad}}}}
        r = client.post("/v3/cohort/counts", json=body)
        assert r.status_code == 400, (bad, r.text[:200])
        assert "YYYY-MM-DD" in r.json()["error"]["message"]

    # Correctly formatted but impossible: say so, rather than repeat the format it already has.
    for bad in ("2020-02-30", "20201301"):
        body = {"filters": {"terms": _TERMS, "ranges": {"StudyDate": {"lte": bad}}}}
        r = client.post("/v3/cohort/counts", json=body)
        assert r.status_code == 400, (bad, r.text[:200])
        message = r.json()["error"]["message"]
        assert "not a real calendar date" in message and "YYYY-MM-DD" not in message

    # ISO dates pass; DICOM DA (YYYYMMDD) is normalized to the stored form and echoed as such.
    iso = {"filters": {"terms": _TERMS, "ranges": {"StudyDate": {"gte": "1900-01-01"}}}}
    dicom = {"filters": {"terms": _TERMS, "ranges": {"StudyDate": {"gte": "19000101"}}}}
    a = client.post("/v3/cohort/counts", json=iso).json()
    b = client.post("/v3/cohort/counts", json=dicom).json()
    assert a["series"] > 0 and a["series"] == b["series"]
    assert b["filters_applied"]["ranges"] == {"StudyDate": {"gte": "1900-01-01", "lte": None}}


async def test_mcp_bad_range_bounds_are_clean_tool_errors(parse_mcp):
    with pytest.raises(ToolError, match="is not a number"):
        await mcp.call_tool(
            "build_cohort", {"terms": _TERMS, "ranges": {"instanceCount": {"gte": "1 AND 1=1"}}}
        )
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        await mcp.call_tool(
            "build_cohort", {"terms": _TERMS, "ranges": {"SeriesDate": {"lte": "yesterday"}}}
        )


# --- what survived compilation is always reported ------------------------------------------


def test_counts_echo_the_filter_they_applied(client):
    body = {"filters": {"terms": _TERMS, "ranges": {"instanceCount": {"gte": 2}}}}
    r = client.post("/v3/cohort/counts", json=body).json()
    assert r["series"] > 0
    assert r["filters_applied"]["terms"] == _TERMS
    assert r["filters_applied"]["ranges"] == {"instanceCount": {"gte": 2.0, "lte": None}}
    assert r["warnings"] == []


def test_empty_filter_says_it_covers_everything(client):
    """Aggregates still answer an empty filter — "how big is IDC" is a real question — but it can
    no longer be mistaken for a cohort."""
    r = client.post("/v3/cohort/counts", json={"filters": {}}).json()
    assert r["series"] > _ALL_OF_IDC
    assert r["filters_applied"] == {"terms": {}, "ranges": {}}
    assert any("ENTIRE IDC archive" in w for w in r["warnings"])


def test_predicates_that_constrain_nothing_are_reported(client):
    """An empty value list is dropped by the compiler; unreported, it is the same footgun with a
    correctly-shaped body."""
    r = client.post("/v3/cohort/counts", json={"filters": {"terms": {"collection_id": []}}}).json()
    assert r["series"] > _ALL_OF_IDC
    assert r["filters_applied"]["terms"] == {}
    assert any("'collection_id' was ignored" in w for w in r["warnings"])
    assert any("ENTIRE IDC archive" in w for w in r["warnings"])


def test_licenses_echo_the_filter_they_applied(client):
    r = client.post("/v3/licenses", json={"filters": {"terms": _TERMS}}).json()
    assert r["licenses"] and r["filters_applied"]["terms"] == _TERMS
    assert r["warnings"] == []


def test_wrong_case_value_is_explained_not_just_zeroed(client):
    """`Modality: ['mr']` counts zero exactly like a genuinely empty cohort. Saying so is the
    difference between "no such data" and "wrong casing"."""
    r = client.post("/v3/cohort/counts", json={"filters": {"terms": {"Modality": ["mr"]}}}).json()
    assert r["series"] == 0
    assert any("case-sensitively" in w and "MR" in w for w in r["warnings"]), r["warnings"]

    # A genuinely empty cohort stays quiet — the hint must not fire on correct casing.
    empty = client.post(
        "/v3/cohort/counts",
        json={"filters": {"terms": {"collection_id": ["rider_pilot"], "Modality": ["MR"]}}},
    ).json()
    assert empty["series"] == 0 and empty["warnings"] == []


# --- enumerating series requires a predicate ------------------------------------------------


def test_unfiltered_enumeration_is_refused(client):
    """Counts describe the archive; a manifest *enumerates* it. No caller means to ask for a
    download payload covering 100+ TB, so it is a 400 rather than a warning."""
    for path in ("/v3/cohort/manifest", "/v3/cohort/manifest.txt"):
        r = client.post(path, json={"filters": {}})
        assert r.status_code == 400, (path, r.text[:200])
        assert "At least one filter predicate is required" in r.text, path

    # ... and the refusal explains a filter that *looks* present but compiled to nothing.
    r = client.post("/v3/cohort/manifest", json={"filters": {"terms": {"collection_id": []}}})
    assert r.status_code == 400
    assert "'collection_id' was ignored" in r.json()["error"]["message"]


def test_filtered_manifest_still_works(client):
    m = client.post(
        "/v3/cohort/manifest", json={"filters": {"terms": _TERMS}, "page_size": 3}
    ).json()
    assert 0 < m["returned"] <= 3
    assert m["counts"]["filters_applied"]["terms"] == _TERMS
    assert m["counts"]["warnings"] == []


# --- core + MCP see the same guarantees ----------------------------------------------------


def test_compiler_reports_what_it_dropped():
    ok = compile_filters(CohortFilters(terms=_TERMS))
    assert ok.where == '"collection_id" IN (?)' and ok.params == ["rider_pilot"]
    assert ok.applied.terms == _TERMS and ok.warnings == []

    dropped = compile_filters(
        CohortFilters(terms={"collection_id": []}, ranges={"instanceCount": {}})
    )
    assert dropped.where == "TRUE"  # selects everything, so it must never pass require_filter
    assert dropped.applied.terms == {} and dropped.applied.ranges == {}
    assert len(dropped.warnings) == 3  # both dropped predicates, plus "no filter applied"

    with pytest.raises(Exception, match="At least one filter predicate"):
        require_filter(dropped, "do the dangerous thing")
    require_filter(ok, "do the dangerous thing")  # a real predicate passes


async def test_mcp_refuses_to_enumerate_the_whole_archive(parse_mcp):
    for tool in ("build_cohort", "get_cohort_urls"):
        with pytest.raises(ToolError, match="At least one filter predicate"):
            await mcp.call_tool(tool, {})
    # Aggregates stay available unfiltered, with the warning attached.
    lic = parse_mcp(await mcp.call_tool("get_licenses", {}))
    assert any("ENTIRE IDC archive" in w for w in lic["warnings"])


def test_openapi_documents_the_filter_contract(client):
    """Swagger UI and the post-deploy example smoke test read this spec, and an agent may be
    working from it alone — so the uniform shape and the refusals have to be *in* it, not just in
    the implementation."""
    spec = client.get("/v3/openapi.json").json()
    schemas = spec["components"]["schemas"]

    for name in ("CountsRequest", "LicensesRequest", "ManifestRequest", "CitationsRequest"):
        assert list(schemas[name]["properties"])[0] == "filters", name
        assert schemas[name]["additionalProperties"] is False, name
        # The declared example is what Swagger pre-fills and what the smoke test fires.
        assert "filters" in schemas[name]["examples"][0], name
    for name in ("CohortFilters", "NumericRange"):
        assert schemas[name]["additionalProperties"] is False, name

    def error_examples(path, code):
        responses = spec["paths"][path]["post"]["responses"]
        assert {"400", "422"} <= set(responses), path
        return responses[code]["content"]["application/json"]["examples"]

    for path in (*_WRAPPED, "/v3/cohort/manifest.txt"):
        assert "filter not under `filters`" in error_examples(path, "422"), path
    # Only the enumerating endpoints document the unfiltered refusal.
    assert "no filter predicate" in error_examples("/v3/cohort/manifest", "400")
    assert "no filter predicate" in error_examples("/v3/cohort/manifest.txt", "400")
    assert "no filter predicate" not in error_examples("/v3/cohort/counts", "400")

    for name in ("CohortCounts", "LicensesResult", "CitationsResult"):
        props = schemas[name]["properties"]
        assert props["filters_applied"]["description"] and props["warnings"]["description"], name


async def test_mcp_malformed_filter_argument_is_actionable():
    """A rejected filter must reach the model as the expected shape — `guard`'s generic
    "Internal error" would tell it nothing to act on."""
    with pytest.raises(ToolError) as exc:
        await mcp.call_tool("build_cohort", {"ranges": {"instanceCount": {"min": 5}}})
    assert "Malformed filter arguments" in str(exc.value)
    assert "gte" in str(exc.value)
