"""Citation generation from a cohort's source DOIs (mirrors idc-index ``citations_from_selection``).

Resolves distinct ``source_DOI`` values for the selection and fetches formatted citations via
DOI content negotiation. The main IDC publication (10.1148/rg.230180) is fetched separately and
returned as ``idc_acknowledgment`` so callers can present it as the acknowledgment for IDC
itself, distinct from the per-dataset citations.
"""

from __future__ import annotations

import requests

from ..backend.base import QueryBackend
from ..errors import InvalidQueryError
from ..filters import compile_filters
from ..models import CitationsResult, CohortFilters

# Short name -> DOI content-negotiation MIME type (see https://citation.crosscite.org).
CITATION_FORMATS = {
    "apa": "text/x-bibliography; style=apa; locale=en-US",
    "bibtex": "application/x-bibtex",
    "csl-json": "application/vnd.citationstyles.csl+json",
    "turtle": "text/turtle",
}

_MAIN_IDC_DOI = "10.1148/rg.230180"

# Batch resolution. Every IDC *dataset* DOI is DataCite-registered (TCIA 10.7937, Zenodo
# 10.5281), and DataCite's list endpoint honours the same content negotiation as a single-DOI
# resolve — so one request returns N formatted citations instead of N round-trips. This matters:
# an unfiltered cohort spans every DOI in the archive (242 at IDC v25), which one-at-a-time meant
# 242 serial network calls holding a worker for minutes.
_DATACITE_DOIS_URL = "https://api.datacite.org/dois"
# All 242 DOIs in a single OR-query URL gets an HTTP 414 from DataCite; 50 keeps the URL near
# 2 KB and collapses the whole archive into five requests.
_BATCH_CHUNK = 50
# Turtle is excluded on purpose: concatenated RDF can't be split back into per-DOI entries.
_BATCHABLE_FORMATS = {"apa", "bibtex", "csl-json"}


class CitationsService:
    def __init__(self, backend: QueryBackend):
        self.backend = backend

    def get_citations(
        self, filters: CohortFilters, citation_format: str = "apa", timeout: float = 30.0
    ) -> CitationsResult:
        fmt = citation_format.lower()
        if fmt not in CITATION_FORMATS:
            raise InvalidQueryError(
                f"Unknown citation_format {citation_format!r}. "
                f"Choose one of: {', '.join(CITATION_FORMATS)}."
            )
        accept = CITATION_FORMATS[fmt]

        f = compile_filters(filters)
        dataset_dois = [
            r["source_DOI"]
            for r in self.backend.query(
                # `f.where` is compile_filters output: allow-listed columns, values bound below.
                f"SELECT DISTINCT source_DOI FROM index WHERE {f.where} "  # nosec B608
                f"AND source_DOI IS NOT NULL AND source_DOI <> ''",
                params=f.params,
            ).rows
        ]

        citations = self._resolve(dataset_dois, accept, fmt, timeout)
        # The IDC paper is kept separate from the dataset citations so callers can surface it as
        # the acknowledgment for IDC itself, alongside the recommendation on CitationsResult.
        idc_ack = self._fetch(_MAIN_IDC_DOI, accept, fmt, timeout)

        return CitationsResult(
            format=fmt,
            citations=citations,
            idc_acknowledgment=idc_ack,
            filters_applied=f.applied,
            warnings=f.warnings,
        )

    def _resolve(self, dois: list[str], accept: str, fmt: str, timeout: float) -> list:
        """Formatted citations for ``dois``, batched where possible.

        Anything the batch didn't cover — a non-DataCite DOI, a chunk that failed — falls back to
        the per-DOI resolve, so batching can only make this faster, never less complete.
        """
        batched = (
            self._batch_fetch(dois, accept, fmt, timeout)
            if fmt in _BATCHABLE_FORMATS and len(dois) > 1
            else {}
        )
        out = []
        for doi in dois:  # index order, so the same cohort always cites in the same order
            hit = batched.get(doi.lower()) or self._fetch(doi, accept, fmt, timeout)
            if hit:
                out.append(hit)
        return out

    @classmethod
    def _batch_fetch(cls, dois: list[str], accept: str, fmt: str, timeout: float) -> dict:
        """Resolve ``dois`` in chunks against DataCite's list endpoint -> {lowercased doi: entry}.

        Best-effort by construction: a chunk that errors or comes back unparseable is simply
        absent from the result, and ``_resolve`` fills the gap one DOI at a time.
        """
        found: dict[str, object] = {}
        for start in range(0, len(dois), _BATCH_CHUNK):
            chunk = dois[start : start + _BATCH_CHUNK]
            query = "doi:(" + " OR ".join(f'"{d}"' for d in chunk) + ")"
            try:
                resp = requests.get(
                    _DATACITE_DOIS_URL,
                    params={"query": query, "page[size]": len(chunk)},
                    headers={"accept": accept},
                    timeout=timeout,
                )
            except requests.RequestException:
                continue
            if resp.status_code == 200:
                found.update(cls._split_batch(resp, chunk, fmt))
        return found

    @staticmethod
    def _split_batch(resp, chunk: list[str], fmt: str) -> dict:
        """Map a batch response back onto the DOIs that asked for it.

        DataCite doesn't preserve request order, so entries are matched by the DOI each one
        carries: a `DOI` field for csl-json, and the doi.org URL (APA) or `@misc` key (BibTeX)
        embedded in the text formats, which arrive as one blank-line-separated blob.
        """
        if fmt == "csl-json":
            try:
                items = resp.json()
            except ValueError:
                return {}
            if not isinstance(items, list):
                return {}
            return {str(i["DOI"]).lower(): i for i in items if isinstance(i, dict) and i.get("DOI")}

        found: dict[str, object] = {}
        for entry in (e.strip() for e in resp.text.split("\n\n")):
            if not entry:
                continue
            lowered = entry.lower()
            for doi in chunk:
                if doi.lower() in lowered:
                    found[doi.lower()] = entry
                    break
        return found

    @staticmethod
    def _fetch(doi: str, accept: str, fmt: str, timeout: float):
        """Fetch one formatted citation via DOI content negotiation; ``None`` on failure."""
        try:
            resp = requests.get(
                f"https://dx.doi.org/{doi}", headers={"accept": accept}, timeout=timeout
            )
        except requests.RequestException:
            return None
        if resp.status_code == 200:
            return resp.json() if fmt == "csl-json" else resp.text.strip()
        return None
