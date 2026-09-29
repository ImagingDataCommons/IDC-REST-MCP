"""Release history: what changed in a given IDC data release.

Computed entirely from the current release's bundled tables. Every *series version* IDC has
ever served is either the current row in ``index`` (valid from ``series_revised_idc_version``
to now) or a row in ``prior_versions_index`` (a superseded or removed version, valid from
``min_idc_version`` to ``max_idc_version``). A series is in release N if one of its versions
spans N, so comparing release N with N-1 gives added / revised / removed series exactly.
"""

from __future__ import annotations

from ..backend.base import QueryBackend
from ..errors import InvalidQueryError
from ..models import AnalysisResultChange, CollectionChange, ReleaseChanges

_MB_PER_TB = 1_000_000

# Params: current, N, N, N-1, N-1, N.
_DIFF_SQL = """
WITH v AS (
  SELECT SeriesInstanceUID, collection_id, PatientID, series_size_MB,
         series_revised_idc_version AS lo, ? AS hi
  FROM index
  UNION ALL
  SELECT SeriesInstanceUID, collection_id, PatientID, series_size_MB,
         min_idc_version AS lo, max_idc_version AS hi
  FROM prior_versions_index
),
cur AS (SELECT * FROM v WHERE lo <= ? AND hi >= ?),
prev AS (SELECT * FROM v WHERE lo <= ? AND hi >= ?),
s AS (
  SELECT COALESCE(c.collection_id, p.collection_id) AS collection_id,
         COALESCE(c.PatientID, p.PatientID) AS PatientID,
         c.SeriesInstanceUID IS NOT NULL AS in_cur,
         p.SeriesInstanceUID IS NOT NULL AS in_prev,
         CASE WHEN p.SeriesInstanceUID IS NULL THEN 'added'
              WHEN c.SeriesInstanceUID IS NULL THEN 'removed'
              WHEN c.lo = ? THEN 'revised' END AS change,
         c.series_size_MB AS cur_mb,
         p.series_size_MB AS prev_mb
  FROM cur c FULL OUTER JOIN prev p ON c.SeriesInstanceUID = p.SeriesInstanceUID
)
SELECT GROUPING(collection_id) AS is_total,
       collection_id,
       count(*) FILTER (WHERE in_prev) AS prev_series,
       count(*) FILTER (WHERE in_cur) AS cur_series,
       count(*) FILTER (WHERE change = 'added') AS series_added,
       count(*) FILTER (WHERE change = 'revised') AS series_revised,
       count(*) FILTER (WHERE change = 'removed') AS series_removed,
       COALESCE(sum(cur_mb) FILTER (WHERE change = 'added'), 0) AS added_mb,
       COALESCE(sum(cur_mb) FILTER (WHERE change = 'revised'), 0) AS revised_mb,
       COALESCE(sum(prev_mb) FILTER (WHERE change = 'removed'), 0) AS removed_mb,
       count(DISTINCT PatientID) FILTER (WHERE change IS NOT NULL) AS patients_affected
FROM s
GROUP BY GROUPING SETS ((collection_id), ())
HAVING count(*) FILTER (WHERE change IS NOT NULL) > 0 OR GROUPING(collection_id) = 1
"""

_NOTE = (
    "Computed from the current release: series in `index` plus superseded/removed series "
    "versions in `prior_versions_index`. 'revised' means the series is in both releases but its "
    "content changed (new crdc_series_uuid). For per-series detail, query those tables with "
    "run_sql (series_init_idc_version / series_revised_idc_version on `index`; min_idc_version / "
    "max_idc_version on `prior_versions_index`)."
)


def _tb(mb: float) -> float:
    return round(mb / _MB_PER_TB, 3)


def _parse_version(version: int | str) -> int:
    s = str(version).strip().lower().removeprefix("v")
    if not s.isdigit():
        raise InvalidQueryError(f"Invalid IDC version: {version!r}. Use an integer such as 24.")
    return int(s)


class ReleaseService:
    def __init__(self, backend: QueryBackend):
        self.backend = backend

    def _release_dates(self) -> dict[int, str | None]:
        rows = self.backend.query(
            "SELECT idc_version, version_timestamp FROM version_metadata_index"
        ).rows
        return {
            int(r["idc_version"]): (str(r["version_timestamp"]) if r["version_timestamp"] else None)
            for r in rows
        }

    def release_changes(self, version: int | str | None = None) -> ReleaseChanges:
        dates = self._release_dates()
        current = max(dates)
        n = current if version is None else _parse_version(version)
        if n not in dates:
            raise InvalidQueryError(
                f"Unknown IDC version: {version!r}. Releases available: v{min(dates)}–v{current}."
            )

        rows = self.backend.query(_DIFF_SQL, [current, n, n, n - 1, n - 1, n]).rows
        total = next(r for r in rows if r["is_total"])
        per_coll = [r for r in rows if not r["is_total"]]

        def status(r: dict) -> str:
            if r["prev_series"] == 0:
                return "new"
            if r["cur_series"] == 0:
                return "removed"
            return "updated"

        collections = sorted(
            (
                CollectionChange(
                    collection_id=r["collection_id"],
                    status=status(r),
                    series_added=r["series_added"],
                    series_revised=r["series_revised"],
                    series_removed=r["series_removed"],
                    size_TB_added=_tb(r["added_mb"]),
                    size_TB_revised=_tb(r["revised_mb"]),
                    size_TB_removed=_tb(r["removed_mb"]),
                    patients_affected=r["patients_affected"],
                )
                for r in per_coll
            ),
            key=lambda c: (-c.size_TB_added, -c.series_added, c.collection_id),
        )

        analysis = self.backend.query(
            "SELECT analysis_result_id, "
            "count(*) FILTER (WHERE series_init_idc_version = ?) AS series_added, "
            "min(series_init_idc_version) = ? AS is_new "
            "FROM index WHERE analysis_result_id IS NOT NULL "
            "GROUP BY 1 HAVING series_added > 0 ORDER BY series_added DESC, 1",
            [n, n],
        ).rows

        prev = n - 1 if (n - 1) in dates else None
        return ReleaseChanges(
            idc_version=f"v{n}",
            release_date=dates[n],
            previous_version=f"v{prev}" if prev is not None else None,
            previous_release_date=dates.get(prev) if prev is not None else None,
            current_version=f"v{current}",
            series_added=total["series_added"],
            series_revised=total["series_revised"],
            series_removed=total["series_removed"],
            size_TB_added=_tb(total["added_mb"]),
            size_TB_revised=_tb(total["revised_mb"]),
            size_TB_removed=_tb(total["removed_mb"]),
            patients_affected=total["patients_affected"],
            new_collections=sorted(c.collection_id for c in collections if c.status == "new"),
            removed_collections=sorted(
                c.collection_id for c in collections if c.status == "removed"
            ),
            collections=collections,
            analysis_results=[
                AnalysisResultChange(
                    analysis_result_id=r["analysis_result_id"],
                    is_new=bool(r["is_new"]),
                    series_added=r["series_added"],
                )
                for r in analysis
            ],
            note=_NOTE,
        )
