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

        where, params = compile_filters(filters)
        dataset_dois = [
            r["source_DOI"]
            for r in self.backend.query(
                f"SELECT DISTINCT source_DOI FROM index WHERE {where} "
                f"AND source_DOI IS NOT NULL AND source_DOI <> ''",
                params,
            ).rows
        ]

        citations = [c for doi in dataset_dois if (c := self._fetch(doi, accept, fmt, timeout))]
        # The IDC paper is kept separate from the dataset citations so callers can surface it as
        # the acknowledgment for IDC itself, alongside the recommendation on CitationsResult.
        idc_ack = self._fetch(_MAIN_IDC_DOI, accept, fmt, timeout)

        return CitationsResult(format=fmt, citations=citations, idc_acknowledgment=idc_ack)

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
