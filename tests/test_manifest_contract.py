"""The text manifest is an idc-index input format (architecture invariant #8).

`POST /v3/cohort/manifest.txt` and `get_cohort_urls` feed `idc download` /
`idc download-from-manifest`, which read each line with `pd.read_csv(header=None)` as a single
column. A line carrying one more field (a series number, a licence) makes them fail with a
pandas "Length mismatch" error. The JSON `cohort/manifest` rows are where extra per-series fields
go. These tests pin the line format, and feed our output through idc-index's own manifest
handling.
"""

from __future__ import annotations

import re

import pytest

from idc_api.mcp.server import mcp

# One bare series URL: bucket, crdc_series_uuid, and the s5cmd series wildcard. Nothing else.
_LINE = re.compile(
    r"s3://[a-z0-9][a-z0-9.-]*/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/\*"
)


@pytest.fixture(scope="module")
def series(ctx):
    """The two smallest series in each AWS bucket. That covers the buckets that source=gcs remaps
    and the one it doesn't, and keeps the size well below any disk-space check."""
    rows = ctx.query.run_sql(
        "SELECT SeriesInstanceUID, series_size_MB FROM index "
        "QUALIFY row_number() OVER (PARTITION BY aws_bucket ORDER BY series_size_MB) <= 2",
        None,
        max_rows=100,
    ).rows
    assert len(rows) >= 6, "expected at least three buckets"
    return rows


def _manifest(client, series, source: str) -> str:
    uids = [r["SeriesInstanceUID"] for r in series]
    body = {"filters": {"terms": {"SeriesInstanceUID": uids}}, "source": source}
    r = client.post("/v3/cohort/manifest.txt", json=body)
    assert r.status_code == 200, r.text[:200]
    return r.text


@pytest.mark.parametrize("source", ["aws", "gcs"])
async def test_every_line_is_one_bare_series_url(client, series, source, parse_mcp):
    text = _manifest(client, series, source)
    lines = text.splitlines()
    assert len(lines) == len(series)
    for line in lines:
        assert _LINE.fullmatch(line), f"not a bare series URL: {line!r}"
    assert text.endswith("\n")

    # get_cohort_urls hands out the same lines.
    uids = [r["SeriesInstanceUID"] for r in series]
    urls = parse_mcp(
        await mcp.call_tool(
            "get_cohort_urls", {"terms": {"SeriesInstanceUID": uids}, "source": source}
        )
    )["urls"]
    assert urls == lines


@pytest.mark.parametrize(
    ("source", "endpoint"),
    [("aws", "https://s3.amazonaws.com"), ("gcs", "https://storage.googleapis.com")],
)
def test_idc_index_reads_our_manifest(client, series, source, endpoint, tmp_path, monkeypatch):
    """Run the manifest through IDCClient.download_from_manifest, the path both CLI commands
    take, with only the transfer stubbed out (as idc-index's own tests do). Every line must
    resolve to an IDC series and the right endpoint."""
    IDCClient = pytest.importorskip("idc_index").IDCClient
    client_idc = IDCClient()
    captured = {}
    monkeypatch.setattr(client_idc, "_s5cmd_run", lambda **kw: captured.update(kw))

    manifest = tmp_path / "idc_manifest.txt"
    manifest.write_text(_manifest(client, series, source))
    # validate_manifest=True would `s5cmd ls` a GCS URL over the network; parsing and series
    # resolution run either way.
    client_idc.download_from_manifest(
        str(manifest), downloadDir=str(tmp_path / "out"), validate_manifest=(source == "aws")
    )

    resolved = captured["s5cmd_sync_helper_df"]
    assert len(resolved) == len(series)
    assert resolved["index_crdc_series_uuid"].notna().all()
    assert captured["endpoint_to_use"] == endpoint
    assert captured["total_size"] == pytest.approx(
        sum(r["series_size_MB"] for r in series), abs=0.01
    )
