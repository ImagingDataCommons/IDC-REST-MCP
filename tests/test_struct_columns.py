"""Struct (``RECORD``) columns must advertise their fields.

The upstream idc-index schema JSON describes a struct column as a bare ``RECORD`` with no
field list, so ``RECORD`` alone names nothing an agent could select — and this server's whole
contract is that callers ground on the schema instead of guessing identifiers. These tests
pin the expansion (read from the Parquet footer) and check that SQL written from the
advertised type actually runs.
"""

from __future__ import annotations

import pytest

from idc_api.core import schema
from idc_api.core.schema import _column_type


@pytest.fixture
def clear_schema_caches():
    """``table_schema`` / ``_parquet_column_types`` are lru_cached, so a test that changes what
    they read must clear them on the way in *and* on the way out — otherwise a fake value
    outlives the monkeypatch and leaks into every later test."""
    schema._parquet_column_types.cache_clear()
    schema.table_schema.cache_clear()
    yield
    schema._parquet_column_types.cache_clear()
    schema.table_schema.cache_clear()


def _col(ctx, table, name):
    return {c.name: c for c in ctx.query.get_table_schema(table).columns}[name]


def test_provenance_advertises_its_fields(ctx):
    """IDC v25 added analysis_results_index.provenance, a flat 4-field struct."""
    col = _col(ctx, "analysis_results_index", "provenance")
    assert col.type.startswith("STRUCT("), f"struct not expanded: {col.type}"
    for field in (
        "data_contributor",
        "source_data_provider",
        "deidentification_party",
        "dicom_conversion_by",
    ):
        assert field in col.type, f"{field} missing from advertised type: {col.type}"


def test_sources_advertises_nested_structs_as_an_array(ctx):
    """collections_index.sources is a REPEATED record whose fields include two further
    structs. The DuckDB type already carries the trailing ``[]``, so it must not be doubled."""
    col = _col(ctx, "collections_index", "sources")
    assert col.type.startswith("STRUCT(")
    assert col.type.endswith("[]") and not col.type.endswith("[][]")
    # nested one level down — the reason a bare RECORD is useless here
    assert "license STRUCT(" in col.type
    assert "provenance STRUCT(" in col.type


def test_advertised_struct_fields_are_selectable(ctx):
    """The advertised type is only useful if SQL written from it runs."""
    res = ctx.query.run_sql(
        "SELECT provenance.data_contributor AS c, count(*) AS n "
        "FROM analysis_results_index WHERE provenance IS NOT NULL GROUP BY 1"
    )
    assert res.row_count > 0
    assert {"c", "n"} == set(res.columns)

    nested = ctx.query.run_sql(
        "SELECT s.provenance.dicom_conversion_by AS conv, count(*) AS n "
        "FROM (SELECT unnest(sources) AS s FROM collections_index) GROUP BY 1"
    )
    assert nested.row_count > 0


def test_struct_values_survive_serialization(ctx):
    """A whole struct column must round-trip to JSON-safe dicts/lists, not DuckDB objects."""
    res = ctx.query.run_sql(
        "SELECT provenance FROM analysis_results_index WHERE provenance IS NOT NULL LIMIT 1"
    )
    value = res.rows[0]["provenance"]
    assert isinstance(value, dict)
    assert isinstance(value["data_contributor"], str)


def test_specialized_index_has_no_local_parquet_to_read(ctx):
    """A specialized index resolves to no local Parquet (INDEX_METADATA carries None until it
    is fetched, and fetching puts it in idc-index's cache, not here), so there is nothing to
    expand from — and clinical_index's RECORD column stays bare."""
    assert schema._parquet_column_types("clinical_index") == {}
    cols = {c["name"]: c["type"] for c in schema.table_schema("clinical_index")["columns"]}
    assert cols["values"] == "RECORD[]"


def test_column_type_renders_the_bare_fallback(ctx):
    """With nothing to expand from, a RECORD renders as RECORD/RECORD[] by mode — never as a
    half-formed STRUCT."""
    assert (
        _column_type({"name": "provenance", "type": "RECORD", "mode": "NULLABLE"}, {}) == "RECORD"
    )
    assert _column_type({"name": "sources", "type": "RECORD", "mode": "REPEATED"}, {}) == "RECORD[]"
    # an unrelated expansion must not be borrowed for a column that has none
    assert (
        _column_type(
            {"name": "sources", "type": "RECORD", "mode": "REPEATED"}, {"other": "STRUCT(a INT)"}
        )
        == "RECORD[]"
    )


def test_expansion_falls_back_when_the_parquet_goes_missing(monkeypatch, clear_schema_caches):
    """The real degradation path, forced on a table that otherwise *does* expand: drop its
    Parquet and provenance must fall back to a bare RECORD instead of raising."""
    import idc_index_data

    before = {
        c["name"]: c["type"] for c in schema.table_schema("analysis_results_index")["columns"]
    }
    assert before["provenance"].startswith("STRUCT(")  # the expansion is real to begin with

    monkeypatch.setitem(
        idc_index_data.INDEX_METADATA["analysis_results_index"], "parquet_filepath", None
    )
    schema._parquet_column_types.cache_clear()
    schema.table_schema.cache_clear()

    after = {c["name"]: c["type"] for c in schema.table_schema("analysis_results_index")["columns"]}
    assert after["provenance"] == "RECORD"
