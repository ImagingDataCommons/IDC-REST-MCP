"""Struct (``RECORD``) columns must advertise their fields.

The upstream idc-index schema JSON describes a struct column as a bare ``RECORD`` with no
field list, so ``RECORD`` alone names nothing an agent could select — and this server's whole
contract is that callers ground on the schema instead of guessing identifiers. These tests
pin the expansion (read from the Parquet footer) and check that SQL written from the
advertised type actually runs.
"""

from __future__ import annotations

from idc_api.core import schema


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


def test_record_without_local_parquet_degrades(ctx):
    """clinical_index is a specialized index: no local Parquet until it is fetched, so its
    RECORD column stays un-expanded rather than breaking schema discovery."""
    assert schema._parquet_column_types("seg_index") == {}
    cols = {c["name"]: c["type"] for c in schema.table_schema("clinical_index")["columns"]}
    assert cols["values"] in ("RECORD[]", "RECORD") or cols["values"].startswith("STRUCT(")
