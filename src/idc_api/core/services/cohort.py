"""Cohort building: structured filters -> distinct counts + a page of series rows + a
download payload, without any SQL string surgery."""

from __future__ import annotations

from ..backend.base import QueryBackend
from ..filters import compile_filters, require_filter
from ..models import (
    CohortCounts,
    CohortFilters,
    ManifestResponse,
    SeriesManifestRow,
)
from .manifest import ManifestService

_MB_PER_TB = 1_000_000

_ROW_COLUMNS = [
    "collection_id",
    "PatientID",
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "Modality",
    "SeriesDescription",
    "instanceCount",
    "series_size_MB",
    "aws_bucket",
    "crdc_series_uuid",
    "series_aws_url",
]


class CohortService:
    def __init__(self, backend: QueryBackend, settings):
        self.backend = backend
        self.settings = settings
        self.manifest = ManifestService(backend, settings)

    def counts(self, filters: CohortFilters) -> CohortCounts:
        f = compile_filters(filters)
        # `f.where` is compile_filters output: allow-listed columns, values bound below.
        row = self.backend.query(
            f"SELECT count(DISTINCT PatientID) patients, "  # nosec B608
            f"count(DISTINCT StudyInstanceUID) studies, "
            f"count(DISTINCT SeriesInstanceUID) series, "
            f"COALESCE(sum(instanceCount),0) instances, "
            f"COALESCE(sum(series_size_MB),0) size_mb FROM index WHERE {f.where}",
            params=f.params,
        ).rows[0]
        warnings = list(f.warnings)
        if row["series"] == 0:
            warnings.extend(self._casing_hints(f.applied))
        return CohortCounts(
            patients=row["patients"],
            studies=row["studies"],
            series=row["series"],
            instances=int(row["instances"]),
            size_TB=round(row["size_mb"] / _MB_PER_TB, 3),
            # Echo the effective filter so a caller can tell an empty cohort apart from a
            # dropped filter without guessing from the magnitude of the numbers.
            filters_applied=f.applied,
            warnings=warnings,
        )

    def _casing_hints(self, applied: CohortFilters) -> list[str]:
        """Explain a zero-row cohort when the only thing wrong was letter case.

        Values are matched exactly, so `Modality=['mr']` counts zero the same way a genuinely
        empty cohort does. Only reached when nothing matched, so the extra probe — one query per
        term attribute — never costs anything on a query that worked.
        """
        hints: list[str] = []
        for attr, values in applied.terms.items():
            # `attr` came through compile_filters, so it is already allow-listed; quote it the
            # same way and bind the values.
            placeholders = ", ".join(["?"] * len(values))
            rows = self.backend.query(
                f'SELECT DISTINCT "{attr}" v FROM index '  # nosec B608
                f'WHERE lower(CAST("{attr}" AS VARCHAR)) IN ({placeholders}) LIMIT 5',
                params=[v.lower() for v in values],
            ).rows
            actual = sorted({r["v"] for r in rows if r["v"] is not None and r["v"] not in values})
            if actual:
                hints.append(
                    f"No series matched {attr}={values}, but {actual} exists — values are matched "
                    "case-sensitively. Use the attribute-values surface to get the exact casing."
                )
        return hints

    def build_manifest(
        self,
        filters: CohortFilters,
        page: int = 0,
        page_size: int | None = None,
        include_rows: bool = True,
    ) -> ManifestResponse:
        page = max(0, int(page))
        page_size = page_size if page_size is not None else self.settings.default_page_size
        page_size = max(1, min(int(page_size), self.settings.max_page_size))

        f = compile_filters(filters)
        # Checked before the counting scan, not after: a manifest of the whole archive is never
        # what a caller meant, so refuse it rather than pay for it.
        require_filter(f, "build a manifest")
        counts = self.counts(filters)

        series: list[SeriesManifestRow] = []
        if include_rows:
            cols = ", ".join(f'"{c}"' for c in _ROW_COLUMNS)
            # `cols` is a fixed constant list (_ROW_COLUMNS); `f.where` is compile_filters output
            # (allow-listed columns, values bound below); page/page_size are clamped ints.
            rows = self.backend.query(
                f"SELECT {cols} FROM index WHERE {f.where} "  # nosec B608
                f"ORDER BY collection_id, PatientID, StudyInstanceUID, SeriesInstanceUID "
                f"LIMIT {page_size} OFFSET {page * page_size}",
                params=f.params,
            ).rows
            series = [SeriesManifestRow(**r) for r in rows]

        download = self.manifest.download_info(filters, counts.series, counts.size_TB)

        return ManifestResponse(
            counts=counts,
            page=page,
            page_size=page_size,
            returned=len(series),
            total_series=counts.series,
            series=series,
            download=download,
        )
