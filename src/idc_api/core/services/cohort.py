"""Cohort building: structured filters -> distinct counts + a page of series rows + a
download payload, without any SQL string surgery."""

from __future__ import annotations

from ..backend.base import QueryBackend
from ..filters import compile_filters
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
        return CohortCounts(
            patients=row["patients"],
            studies=row["studies"],
            series=row["series"],
            instances=int(row["instances"]),
            size_TB=round(row["size_mb"] / _MB_PER_TB, 3),
            # Echo the effective filter so a caller can tell an empty cohort apart from a
            # dropped filter without guessing from the magnitude of the numbers.
            filters_applied=f.applied,
            warnings=f.warnings,
        )

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

        counts = self.counts(filters)
        f = compile_filters(filters)

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
