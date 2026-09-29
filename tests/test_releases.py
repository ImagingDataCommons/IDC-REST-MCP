"""Release history: get_release_changes diffs release N against N-1, and the version-history
tables/columns are discoverable from the tool surface (issue #40)."""

from __future__ import annotations

import pytest

from idc_api.core import schema
from idc_api.core.errors import InvalidQueryError
from idc_api.mcp.server import mcp


def _current(ctx) -> int:
    return int(
        ctx.backend.query("SELECT max(idc_version) v FROM version_metadata_index").rows[0]["v"]
    )


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


def test_version_accepts_v_prefix(ctx):
    cur = _current(ctx)
    assert ctx.releases.release_changes(f"v{cur}") == ctx.releases.release_changes(cur)


@pytest.mark.parametrize("bad", [0, 9999, "latest", "v"])
def test_unknown_version_is_a_clean_error(ctx, bad):
    with pytest.raises(InvalidQueryError):
        ctx.releases.release_changes(bad)


async def test_release_changes_parity(ctx, client, parse_mcp):
    v = _current(ctx) - 1
    core = ctx.releases.release_changes(v).model_dump(mode="json")
    rest = client.get("/v3/releases/changes", params={"version": v}).json()
    mcp_out = parse_mcp(await mcp.call_tool("get_release_changes", {"version": v}))
    assert core == rest == mcp_out


def test_rest_unknown_version_is_400(client):
    r = client.get("/v3/releases/changes", params={"version": 9999})
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


def test_prior_versions_index_is_documented():
    sch = schema.table_schema("prior_versions_index")
    assert sch["description"]
    cols = {c["name"]: c["description"] for c in sch["columns"]}
    assert cols["min_idc_version"] and cols["max_idc_version"]


def test_notable_columns_exist_in_schema():
    # list_tables drops unknown names silently, so catch curation drift here.
    for table, cols in schema.NOTABLE_COLUMNS.items():
        names = {c["name"] for c in schema.table_schema(table)["columns"]}
        assert set(cols) <= names, (table, set(cols) - names)


def test_list_tables_surfaces_version_columns(ctx):
    tables = {t.name: t for t in ctx.query.list_tables().tables}
    assert "series_init_idc_version" in tables["index"].notable_columns
