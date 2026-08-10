"""Citation resolution: batched where possible, complete regardless.

An unfiltered cohort spans every DOI in IDC (237 at v24). One content-negotiation request each
meant 237 serial round-trips holding a worker for minutes, so DataCite's list endpoint — which
honours the same content negotiation and covers every IDC dataset DOI (TCIA 10.7937, Zenodo
10.5281) — resolves them in chunks instead.
"""

from __future__ import annotations

import json

import pytest

import idc_api.core.services.citations as cite_mod
from idc_api.core.models import CohortFilters
from idc_api.core.services.citations import CitationsService

_DOIS = [f"10.7937/fake-{i}" for i in range(3)]


class _Recorder:
    """Stand in for `requests.get`, recording batch vs per-DOI calls."""

    def __init__(self, *, batch_text=None, batch_status=200, per_doi_text="PER-DOI"):
        self.batch_text = batch_text
        self.batch_status = batch_status
        self.per_doi_text = per_doi_text
        self.batch_calls: list[list[str]] = []
        self.per_doi_calls: list[str] = []

    def __call__(self, url, headers=None, timeout=None, params=None):
        recorder = self

        class _Resp:
            def __init__(self, status, text):
                self.status_code = status
                self.text = text

            def json(self):
                return json.loads(self.text)

        if params and "query" in params:  # the DataCite batch endpoint
            recorder.batch_calls.append(params["query"])
            return _Resp(recorder.batch_status, recorder.batch_text or "")
        recorder.per_doi_calls.append(url)
        return _Resp(200, recorder.per_doi_text)


@pytest.fixture
def svc(monkeypatch):
    def _make(recorder):
        monkeypatch.setattr(cite_mod, "requests", type("R", (), {"get": staticmethod(recorder)}))
        monkeypatch.setattr(cite_mod.requests, "RequestException", Exception, raising=False)
        return CitationsService(backend=None)

    return _make


def test_many_dois_resolve_in_one_request(svc):
    """Three DOIs, one call. Entries come back out of order, so each is matched by the DOI it
    carries rather than by position."""
    blob = "\n\n".join(
        f"Author, A. (2024). Title {i}. https://doi.org/{d.upper()}"
        for i, d in reversed(list(enumerate(_DOIS)))
    )
    rec = _Recorder(batch_text=blob)
    out = CitationsService._resolve(svc(rec), _DOIS, "text/x-bibliography", "apa", 30.0)

    assert len(rec.batch_calls) == 1
    assert rec.per_doi_calls == []  # nothing fell back
    # Returned in index order, not DataCite's order.
    assert [c.split("Title ")[1][0] for c in out] == ["0", "1", "2"]


def test_dois_the_batch_missed_fall_back_individually(svc):
    """Batching may only make this faster, never less complete — a DOI DataCite doesn't know
    (e.g. a Crossref one) still gets resolved."""
    blob = f"Author, A. (2024). Title. https://doi.org/{_DOIS[0]}"
    rec = _Recorder(batch_text=blob)
    out = CitationsService._resolve(svc(rec), _DOIS, "text/x-bibliography", "apa", 30.0)

    assert len(rec.batch_calls) == 1
    assert len(rec.per_doi_calls) == 2  # only the two the batch didn't cover
    assert len(out) == 3


def test_batch_failure_degrades_to_per_doi(svc):
    rec = _Recorder(batch_status=503)
    out = CitationsService._resolve(svc(rec), _DOIS, "text/x-bibliography", "apa", 30.0)
    assert len(rec.per_doi_calls) == 3 and out == ["PER-DOI"] * 3


def test_csl_json_batch_maps_by_doi_field(svc):
    rec = _Recorder(batch_text=json.dumps([{"DOI": d.upper(), "title": d} for d in _DOIS]))
    out = CitationsService._resolve(svc(rec), _DOIS, "application/json", "csl-json", 30.0)
    assert rec.per_doi_calls == []
    assert [c["title"] for c in out] == _DOIS


def test_turtle_is_not_batched(svc):
    """Concatenated RDF can't be split back into per-DOI entries, so it keeps the safe path."""
    rec = _Recorder(batch_text="ignored")
    CitationsService._resolve(svc(rec), _DOIS, "text/turtle", "turtle", 30.0)
    assert rec.batch_calls == [] and len(rec.per_doi_calls) == 3


def test_chunking_keeps_the_url_short(svc):
    """237 DOIs in a single OR-query URL gets an HTTP 414 from DataCite, so chunk them."""
    many = [f"10.7937/fake-{i}" for i in range(120)]
    rec = _Recorder(batch_text="")
    CitationsService._resolve(svc(rec), many, "text/x-bibliography", "apa", 30.0)
    assert len(rec.batch_calls) == 3  # 120 / 50, rounded up
    assert all(len(q) < 2500 for q in rec.batch_calls)


def test_citations_echo_the_filter_applied(ctx, monkeypatch):
    """The filter echo reaches citations too, so a caller can see which cohort was cited."""
    monkeypatch.setattr(
        cite_mod, "requests", type("R", (), {"get": staticmethod(_Recorder(batch_text=""))})
    )
    monkeypatch.setattr(cite_mod.requests, "RequestException", Exception, raising=False)
    terms = {"collection_id": ["rider_pilot"]}
    out = ctx.citations.get_citations(CohortFilters(terms=terms))
    assert out.filters_applied.terms == terms and out.warnings == []
