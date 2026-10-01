# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html) with one house rule: the
**MAJOR version is pinned to the served URL prefix** — `/v3` ↔ `3.y.z`. A breaking change to the
REST or MCP contract means a new prefix (`/v4`) and a new major (`4.0.0`), never a silent break
under `/v3`. See [CONTRIBUTING.md](CONTRIBUTING.md#versioning) for the full policy.

Entries describe **user-visible** change — endpoints, MCP tools, response shapes, configuration.
Refactors, CI, and formatting land in the git history, not here.

## [Unreleased]

## [3.0.0b4] — 2026-10-01

Security-maintenance release; no API or MCP contract changes.

### Security

- Dependency updates, including PyJWT 2.13.0 → 2.15.1 (CVE-2026-102274).

## [3.0.0b3] — 2026-08-10

Beta iteration: one shape for every cohort filter, and no request that silently answers with the
whole archive instead of your cohort (both beta contract changes).

### Changed

- **Breaking (beta): `POST /v3/cohort/counts` and `POST /v3/licenses` now take the filter under
  `filters`**, like every other filter endpoint: `{"filters": {"terms": …}}`. The old bare body
  (`{"terms": …}`) returns `422` naming the fix. The shapes used to differ per endpoint, which is
  what made the bug below possible.
- **Breaking (beta): the series-enumerating surfaces require at least one filter predicate** —
  `POST /v3/cohort/manifest`, `POST /v3/cohort/manifest.txt`, and the MCP `build_cohort` /
  `get_cohort_urls`. Unfiltered they returned a download payload for the whole archive; they now
  return `400`. The aggregate surfaces (`cohort/counts`, `licenses`) still answer an unfiltered
  filter, with a warning.

### Added

- Responses built from a cohort filter now carry `filters_applied` (the predicates that reached
  SQL) and `warnings` (predicates dropped, whether nothing was filtered, and — when a cohort
  matches nothing only because of letter case — the casing that does exist): `POST /v3/cohort/counts`,
  `POST /v3/licenses`, `POST /v3/citations`, the `counts` object in `POST /v3/cohort/manifest`,
  and the MCP `build_cohort` / `get_licenses` / `get_citations` results.
- A "Limits" section in the user guide: the per-request caps (SQL timeout and row ceiling,
  manifest cap, page size, memory) and the fact that there is no per-caller rate limit or `429`.

### Fixed

- A mis-shaped filter body no longer returns all of IDC at HTTP 200. Unrecognized keys in a
  filter body (`{"term": …}`, a range bound misspelled `{"min": …}`) and malformed MCP filter
  arguments are now errors rather than silently dropped predicates.
- `POST /v3/citations` / `get_citations` resolve DOIs in batches via DataCite instead of one
  request each — a cohort spanning every DOI in IDC took 237 serial round-trips, now 5. Any DOI
  the batch doesn't cover still falls back to a per-DOI resolve, so citations stay complete.
- The `/v3/viewer-url` OpenAPI examples (the values Swagger UI's "Try it out" pre-fills) used a StudyInstanceUID and SeriesInstanceUID that are not present in IDC, so running the example returned a `not_found` error instead of a viewer link. Both now use resolvable UIDs.

## [3.0.0b2] — 2026-07-14

Beta iteration: drops the local-download surface (a beta contract change) and fixes
`source=gcs` manifests.

### Removed

- **The local-download surface: `POST /v3/download` (REST) and the `download_cohort` MCP tool**
  (beta contract change). Both only worked when the server ran on the caller's own machine and
  errored everywhere else — on the hosted deployment (the common case) the tool's mere presence
  misled agents into calling it. Downloading through the server is also never the right path:
  every IDC bucket is public, so direct S3/GCS transfer is strictly more efficient. Retrieval is
  now manifests/URLs only on every surface — use `get_cohort_urls` / `POST /v3/cohort/manifest.txt`
  or the ready-to-run `idc` CLI commands in the `build_cohort` response. The
  `IDC_API_ENABLE_LOCAL_DOWNLOAD` config variable is gone with it.

### Fixed

- `get_cohort_urls` / `POST /v3/cohort/manifest.txt` with `source=gcs` now return `s3://` URLs
  (GCS's S3-compatible endpoint) instead of `gs://` URLs. This matches how `idc-index` itself
  reaches GCS, and fixes a real breakage: `idc download-from-manifest` only recognizes `s3://`
  lines in a manifest file, so a saved `gs://` manifest silently downloaded nothing.

## [3.0.0b1] — 2026-07-13

First public release of the v3 API: a rewrite that replaces the v1/v2 service with a single
backend-agnostic core behind two thin adapters (REST + MCP), served from the `idc-index` Parquet
index queried locally with DuckDB.

**This is a beta.** The `/v3` contract may still change in response to feedback before `3.0.0`.
Pin to an exact version if you need stability. Legacy v1/v2 endpoints are unaffected — they are
served by a different backend and v3 lives only under `/v3/*`.

### Added

- **REST API**, entirely under the `/v3` prefix: discovery (`/v3/version`, `/v3/stats`,
  `/v3/collections`, `/v3/analysis_results`, `/v3/attributes`, `/v3/tables`), cohort building,
  retrieval (manifests / cohort URLs, viewer URLs), citations and licenses, guarded SQL, and
  `/v3/health`. Interactive docs at `/v3/docs`; the bare domain redirects there.
- **MCP server** over stdio (local) and streamable-http (hosted at `/mcp`), exposing the same
  capabilities as tools — `list_collections`, `get_collection`, `list_attributes`,
  `get_attribute_values`, `build_cohort`, `get_cohort_urls`, `download_cohort`, `get_viewer_url`,
  `run_sql`, `get_citations`, `get_licenses`, and the clinical/table introspection tools — plus
  an `idc://guide` resource describing the data model and workflow.
- **Guarded SQL** (`POST /v3/sql`, `run_sql`): read-only DuckDB with external access and
  extension loading disabled, single-statement enforcement, a server row cap, and a timeout.
  See [SECURITY.md](SECURITY.md).
- **Specialized indices** joinable to `index` on `SeriesInstanceUID` (`seg_index`, `ann_index`,
  `ct_index`, `mr_index`, `pt_index`, `sm_index`, …) and per-collection **clinical tables** under
  a `clinical` schema, both fetched from `idc-index` releases at build time.
- **Software version reporting**, distinct from the IDC *data* version: `/v3/version` returns
  `api_version` (and `build`, when a deploy stamps `IDC_API_BUILD`); the same string appears in
  the OpenAPI `info.version` and the MCP `initialize` handshake (`serverInfo.version`).
- **Structured audit logging** — one JSON line per REST request and MCP tool call.
  `IDC_API_SQL_LOG_MODE` selects how the guarded SQL query is rendered (`snippet` or `hash`).
- **HSTS**: every REST and hosted-MCP response carries a `Strict-Transport-Security` header
  (NCI security policy). Max-age is configurable via `IDC_API_HSTS_MAX_AGE` — default one year;
  dev/test deploys use 3600.

[Unreleased]: https://github.com/ImagingDataCommons/IDC-REST-MCP/compare/v3.0.0b4...HEAD
[3.0.0b4]: https://github.com/ImagingDataCommons/IDC-REST-MCP/compare/v3.0.0b3...v3.0.0b4
[3.0.0b3]: https://github.com/ImagingDataCommons/IDC-REST-MCP/compare/v3.0.0b2...v3.0.0b3
[3.0.0b2]: https://github.com/ImagingDataCommons/IDC-REST-MCP/compare/v3.0.0b1...v3.0.0b2
[3.0.0b1]: https://github.com/ImagingDataCommons/IDC-REST-MCP/releases/tag/v3.0.0b1
