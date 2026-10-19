"""Release history: what changed in a given IDC data release.

Computed entirely from the current release's bundled tables. Every *series version* IDC has
ever served is either the current row in ``index`` (valid from ``series_revised_idc_version``
to now) or a row in ``prior_versions_index`` (a version no longer served, valid from
``min_idc_version`` to ``max_idc_version``). A series is in release N if one of its versions
spans N, so comparing release N with N-1 gives added / revised / removed series exactly.

Two properties of ``prior_versions_index`` shape the SQL. It starts a new row whenever a
version only moved storage bucket, so a row ending at N-1 does not mean N changed the series:
"revised" is decided by ``crdc_series_uuid``, never by where a row starts. And a few versions
have two overlapping rows, so each release is collapsed to one row per series before joining.
"""

from __future__ import annotations

from ..backend.base import QueryBackend
from ..errors import InvalidQueryError
from ..models import AnalysisResultChange, CollectionChange, ReleaseChanges
from ._common import mb_to_tb, served_idc_release

# prior_versions_index has no row starting at v1 (its build drops the original idc-open-idc
# bucket), so a v1 version that was later replaced is invisible.
_EARLY_RELEASE_WARNING = (
    "IDC v1 is only partly recorded: series versions served in v1 and replaced later are not in "
    "prior_versions_index. The v1 counts include only series still served, and the v2 counts may "
    "report as added some series that were revised from v1."
)

# Aggregates over a joined set of (current, previous) series rows. Shared verbatim by the total,
# per-collection, and per-analysis-result groupings below; contains no caller values.
_AGGS = """
       count(*) FILTER (WHERE in_prev) AS prev_series,
       count(*) FILTER (WHERE change = 'added') AS series_added,
       count(*) FILTER (WHERE change = 'revised') AS series_revised,
       count(*) FILTER (WHERE change = 'removed') AS series_removed,
       COALESCE(sum(cur_mb) FILTER (WHERE change = 'added'), 0) AS added_mb,
       COALESCE(sum(cur_mb) FILTER (WHERE change = 'revised'), 0) AS revised_mb,
       COALESCE(sum(prev_mb) FILTER (WHERE change = 'removed'), 0) AS removed_mb,
       count(*) FILTER (WHERE in_cur) AS cur_series,
       count(DISTINCT [collection_id, PatientID]) FILTER (WHERE change IS NOT NULL)
         AS patients_affected"""

# PatientID is unique only within a collection, hence the [collection_id, PatientID] pairs.
_JOINED = """
  SELECT COALESCE(c.collection_id, p.collection_id) AS collection_id,
         COALESCE(c.PatientID, p.PatientID) AS PatientID,
         COALESCE(c.analysis_result_id, p.analysis_result_id) AS analysis_result_id,
         c.SeriesInstanceUID IS NOT NULL AS in_cur,
         p.SeriesInstanceUID IS NOT NULL AS in_prev,
         CASE WHEN p.SeriesInstanceUID IS NULL THEN 'added'
              WHEN c.SeriesInstanceUID IS NULL THEN 'removed'
              WHEN c.crdc_series_uuid <> p.crdc_series_uuid THEN 'revised' END AS change,
         c.mb AS cur_mb,
         p.mb AS prev_mb
  FROM cur c FULL OUTER JOIN prev p ON c.SeriesInstanceUID = p.SeriesInstanceUID"""

# The only placeholders are the two in `params`, bound once. Rows of `index` are already one
# per series; only prior_versions_index needs collapsing to one row per series and release (its
# rows for one release share a crdc_series_uuid). `analysis_result_id` of a version no longer
# served comes from the series' current row, so it is NULL for removed series.
_DIFF_SQL = f"""
WITH params AS (SELECT ?::INTEGER AS served, ?::INTEGER AS n),
rel AS (SELECT n AS r FROM params UNION ALL SELECT n - 1 FROM params),
versions AS (
  SELECT r, SeriesInstanceUID, collection_id, PatientID, analysis_result_id, crdc_series_uuid,
         series_size_MB AS mb
  FROM index JOIN rel ON series_revised_idc_version <= r
  UNION ALL
  SELECT r, pv.SeriesInstanceUID, pv.collection_id, pv.PatientID, i.analysis_result_id,
         pv.crdc_series_uuid, max(pv.series_size_MB)
  FROM prior_versions_index pv
  JOIN rel ON pv.min_idc_version <= r AND pv.max_idc_version >= r
  LEFT JOIN index i USING (SeriesInstanceUID)
  GROUP BY ALL
),
cur AS (SELECT * FROM versions WHERE r = (SELECT n FROM params)),
prev AS (SELECT * FROM versions WHERE r = (SELECT n FROM params) - 1),
-- Series level, for the totals: a series that moved collection is neither added nor removed.
by_series AS ({_JOINED}
),
-- Collection level: a series that moved is removed from one collection and added to the other.
by_collection AS ({_JOINED} AND c.collection_id = p.collection_id
)
SELECT 'total' AS level, NULL AS id, {_AGGS} FROM by_series
UNION ALL
SELECT 'collection', collection_id, {_AGGS} FROM by_collection GROUP BY collection_id
UNION ALL
SELECT 'analysis_result', analysis_result_id, {_AGGS} FROM by_collection
WHERE analysis_result_id IS NOT NULL GROUP BY analysis_result_id
"""

_NOTE = (
    "Computed from the current release: series in `index` plus the series versions no longer "
    "served in `prior_versions_index`. 'revised' means the series is in both releases with "
    "different content (a new crdc_series_uuid). Do not derive changes from "
    "prior_versions_index row ranges: a row also ends when a version only moved storage bucket. "
    "For per-series detail, use series_init_idc_version / series_revised_idc_version on `index`."
)


def parse_idc_version(version: int | str) -> int:
    """An IDC release number from ``24``, ``"24"`` or ``"v24"``; raises InvalidQueryError
    otherwise."""
    s = str(version).strip().lower().removeprefix("v")
    if not (s.isascii() and s.isdigit()):
        raise InvalidQueryError(f"Invalid IDC version: {version!r}. Use a release such as 24.")
    return int(s)


def _status(r: dict) -> str:
    if r["prev_series"] == 0:
        return "new"
    if r["cur_series"] == 0:
        return "removed"
    return "updated"


class ReleaseService:
    """Stateless apart from a per-version memo: the data is fixed for the life of the
    process, and the diff scans every series version."""

    def __init__(self, backend: QueryBackend):
        self.backend = backend
        self._memo: dict[int, ReleaseChanges] = {}

    def _release_dates(self) -> dict[int, str | None]:
        rows = self.backend.query(
            "SELECT idc_version, version_timestamp FROM version_metadata_index"
        ).rows
        return {
            int(r["idc_version"]): (str(r["version_timestamp"]) if r["version_timestamp"] else None)
            for r in rows
        }

    def release_changes(self, version: int | str | None = None) -> ReleaseChanges:
        served = served_idc_release()
        n = served if version is None else parse_idc_version(version)
        if not 1 <= n <= served:
            raise InvalidQueryError(
                f"Unknown IDC version: {version!r}. Releases available: v1–v{served}."
            )
        if n not in self._memo:
            self._memo[n] = self._compute(n, served)
        return self._memo[n]

    def _compute(self, n: int, served: int) -> ReleaseChanges:
        dates = self._release_dates()
        rows = self.backend.query(_DIFF_SQL, [served, n]).rows
        total = next(r for r in rows if r["level"] == "total")
        changed = [
            r
            for r in rows
            if r["level"] != "total"
            and (r["series_added"] or r["series_revised"] or r["series_removed"])
        ]

        collections = sorted(
            (
                CollectionChange(
                    collection_id=r["id"],
                    status=_status(r),
                    series_added=r["series_added"],
                    series_revised=r["series_revised"],
                    series_removed=r["series_removed"],
                    size_TB_added=mb_to_tb(r["added_mb"]),
                    size_TB_revised=mb_to_tb(r["revised_mb"]),
                    size_TB_removed=mb_to_tb(r["removed_mb"]),
                    patients_affected=r["patients_affected"],
                )
                for r in changed
                if r["level"] == "collection"
            ),
            key=lambda c: (-c.size_TB_added, -c.series_added, c.collection_id),
        )
        analysis_results = sorted(
            (
                AnalysisResultChange(
                    analysis_result_id=r["id"],
                    is_new=r["prev_series"] == 0,
                    series_added=r["series_added"],
                    series_revised=r["series_revised"],
                    series_removed=r["series_removed"],
                )
                for r in changed
                if r["level"] == "analysis_result"
            ),
            key=lambda a: (-a.series_added, a.analysis_result_id),
        )

        prev = n - 1 if n > 1 else None
        return ReleaseChanges(
            idc_version=f"v{n}",
            release_date=dates.get(n),
            previous_version=f"v{prev}" if prev else None,
            previous_release_date=dates.get(prev),
            current_version=f"v{served}",
            series_added=total["series_added"],
            series_revised=total["series_revised"],
            series_removed=total["series_removed"],
            size_TB_added=mb_to_tb(total["added_mb"]),
            size_TB_revised=mb_to_tb(total["revised_mb"]),
            size_TB_removed=mb_to_tb(total["removed_mb"]),
            patients_affected=total["patients_affected"],
            new_collections=sorted(c.collection_id for c in collections if c.status == "new"),
            removed_collections=sorted(
                c.collection_id for c in collections if c.status == "removed"
            ),
            collections=collections,
            analysis_results=analysis_results,
            warnings=[_EARLY_RELEASE_WARNING] if n <= 2 else [],
            note=_NOTE,
        )
