"""Release history: get_release_changes diffs release N against N-1, and the version-history
tables/columns are discoverable from the tool surface (issue #40)."""

from __future__ import annotations

import pytest

from idc_api.core import schema
from idc_api.core.errors import InvalidQueryError
from idc_api.mcp.server import mcp


def _current(ctx) -> int:
    return int(ctx.discovery.version().idc_version.removeprefix("v"))


def test_latest_release_is_default_and_self_consistent(ctx):
    cur = _current(ctx)
    rc = ctx.releases.release_changes()
    assert rc.idc_version == rc.current_version == f"v{cur}"
    assert rc.previous_version == f"v{cur - 1}"
    assert rc.release_date and rc.previous_release_date
    # Totals are the sum of the per-collection rows.
    assert rc.series_added == sum(c.series_added for c in rc.collections)
    assert rc.series_revised == sum(c.series_revised for c in rc.collections)
    assert rc.series_removed == sum(c.series_removed for c in rc.collections)
    assert rc.series_added > 0
    # In the served release, a series whose current content dates from this release was either
    # added (absent before) or revised (present before) — nothing else.
    n = ctx.backend.query(
        "SELECT count(*) n FROM index WHERE series_revised_idc_version = ?", [cur]
    ).rows[0]["n"]
    assert rc.series_added + rc.series_revised == n
    revised = ctx.backend.query(
        "SELECT count(*) n FROM index WHERE series_init_idc_version < ? "
        "AND series_revised_idc_version = ?",
        [cur, cur],
    ).rows[0]["n"]
    assert rc.series_revised == revised
    by_id = {c.collection_id: c for c in rc.collections}
    for cid in rc.new_collections:
        c = by_id[cid]
        assert c.status == "new" and c.series_added > 0 and c.series_removed == 0


def test_first_release_has_no_predecessor(ctx):
    rc = ctx.releases.release_changes(1)
    assert rc.idc_version == "v1" and rc.previous_version is None
    assert rc.series_revised == rc.series_removed == 0
    assert rc.series_added > 0
    assert all(c.status == "new" for c in rc.collections)


def test_early_releases_warn_that_v1_is_partly_recorded(ctx):
    assert ctx.releases.release_changes(1).warnings
    assert ctx.releases.release_changes(2).warnings
    assert ctx.releases.release_changes(3).warnings == []


# --- past releases: the cases the served release cannot exercise ---------------------------


def test_bucket_moves_are_not_revisions(ctx):
    """prior_versions_index starts a new row when a version only changed storage bucket, which
    v19→v20 did for thousands of series. Revised means a different crdc_series_uuid."""
    rc = ctx.releases.release_changes(20)
    # Independent formulation: series in both releases whose sets of version ids differ.
    expected = ctx.backend.query(
        """
        WITH v AS (
          SELECT SeriesInstanceUID s, crdc_series_uuid u, series_revised_idc_version lo,
                 ? AS hi FROM index
          UNION ALL
          SELECT SeriesInstanceUID, crdc_series_uuid, min_idc_version, max_idc_version
          FROM prior_versions_index
        ),
        c AS (SELECT s, list(DISTINCT u) us FROM v WHERE lo <= 20 AND hi >= 20 GROUP BY s),
        p AS (SELECT s, list(DISTINCT u) us FROM v WHERE lo <= 19 AND hi >= 19 GROUP BY s)
        SELECT count(*) n FROM c JOIN p USING (s) WHERE c.us <> p.us
        """,
        [_current(ctx)],
    ).rows[0]["n"]
    assert rc.series_revised == expected
    rows_ending = ctx.backend.query(
        "SELECT count(DISTINCT SeriesInstanceUID) n FROM prior_versions_index "
        "WHERE max_idc_version = 19 AND min_idc_version <= 19 "
        "AND SeriesInstanceUID IN (SELECT SeriesInstanceUID FROM index)"
    ).rows[0]["n"]
    assert rc.series_revised < rows_ending  # the naive reading overcounts


def test_collection_move_is_removed_then_new(ctx):
    """At v5 the 43 `apollo` series moved to `apollo_5_lscc`."""
    rc = ctx.releases.release_changes(5)
    assert "apollo_5_lscc" in rc.new_collections
    assert "apollo" in rc.removed_collections
    by_id = {c.collection_id: c for c in rc.collections}
    assert by_id["apollo"].series_removed == by_id["apollo_5_lscc"].series_added == 43
    # In the totals the move is neither an addition to IDC nor a removal from it.
    assert rc.series_added == sum(c.series_added for c in rc.collections) - 43
    assert rc.series_removed == sum(c.series_removed for c in rc.collections) - 43


def test_analysis_results_are_consistent_with_collections(ctx):
    for v in (_current(ctx), 20):
        rc = ctx.releases.release_changes(v)
        for a in rc.analysis_results:
            assert a.series_added + a.series_revised + a.series_removed > 0
            if a.is_new:
                assert a.series_revised == a.series_removed == 0
        assert sum(a.series_added for a in rc.analysis_results) <= rc.series_added


def test_patients_are_counted_per_collection(ctx):
    rc = ctx.releases.release_changes(_current(ctx))
    assert rc.patients_affected <= sum(c.patients_affected for c in rc.collections)


def test_results_are_memoized(ctx):
    assert ctx.releases.release_changes(20) is ctx.releases.release_changes("v20")


def test_version_accepts_v_prefix(ctx):
    cur = _current(ctx)
    assert ctx.releases.release_changes(f"v{cur}") == ctx.releases.release_changes(cur)


@pytest.mark.parametrize("bad", [0, 9999, "latest", "v", "²", "v-1"])
def test_unknown_version_is_a_clean_error(ctx, bad):
    with pytest.raises(InvalidQueryError):
        ctx.releases.release_changes(bad)


async def test_release_changes_parity(ctx, client, parse_mcp):
    v = _current(ctx) - 1
    core = ctx.releases.release_changes(v).model_dump(mode="json")
    rest = client.get("/v3/releases/changes", params={"version": v}).json()
    mcp_out = parse_mcp(await mcp.call_tool("get_release_changes", {"version": v}))
    assert core == rest == mcp_out


async def test_both_adapters_accept_the_version_they_return(ctx, client, parse_mcp):
    """previous_version / get_idc_version use "v23"; passing it back must work."""
    prev = ctx.releases.release_changes().previous_version
    rest = client.get("/v3/releases/changes", params={"version": prev})
    assert rest.status_code == 200 and rest.json()["idc_version"] == prev
    mcp_out = parse_mcp(await mcp.call_tool("get_release_changes", {"version": prev}))
    assert mcp_out["idc_version"] == prev


@pytest.mark.parametrize("bad", [9999, "latest", "²"])
def test_rest_bad_version_is_400(client, bad):
    r = client.get("/v3/releases/changes", params={"version": bad})
    assert r.status_code == 400


# --- discoverability ------------------------------------------------------------------------


async def test_version_tool_points_at_release_history():
    tools = {t.name: t for t in await mcp.list_tools()}
    assert "get_release_changes" in tools
    assert "get_release_changes" in tools["get_idc_version"].description
    desc = " ".join(tools["get_release_changes"].description.split()).lower()
    for word in ("new", "changed", "added", "removed", "since"):
        assert word in desc


async def test_whats_new_prompt():
    prompts = {p.name for p in await mcp.list_prompts()}
    assert "whats_new" in prompts
    msg = await mcp.get_prompt("whats_new", {"version": "v24"})
    assert "get_release_changes(version=24)" in msg.messages[0].content.text
    # Unparseable input falls back to the served release instead of reaching the call.
    msg = await mcp.get_prompt("whats_new", {"version": "latest"})
    assert "get_release_changes()" in msg.messages[0].content.text


def test_prior_versions_index_is_documented():
    sch = schema.table_schema("prior_versions_index")
    # It must steer callers away from reading it as a list of what is new.
    assert "not a record of what is new" in sch["description"]
    cols = {c["name"]: c["description"] for c in sch["columns"]}
    assert all(cols.values()), [n for n, d in cols.items() if not d]
    assert "bucket" in cols["max_idc_version"] and "bucket" in cols["crdc_series_uuid"]


def test_notable_columns_exist_in_schema():
    # list_tables drops unknown names silently, so catch curation drift here.
    for table, cols in schema.NOTABLE_COLUMNS.items():
        names = {c["name"] for c in schema.table_schema(table)["columns"]}
        assert set(cols) <= names, (table, set(cols) - names)


def test_list_tables_surfaces_version_columns(ctx):
    tables = {t.name: t for t in ctx.query.list_tables().tables}
    assert "series_init_idc_version" in tables["index"].notable_columns
