"""Compile structured cohort filters into a parameterized SQL WHERE clause.

Attribute *names* are validated against a fixed allow-list (they can't be parameterized, so
we whitelist + double-quote them); attribute *values* are always passed as bound parameters
— the OWASP-recommended primary defense for the inputs we control.

Compilation also reports what it *dropped*. A filter that compiles to no predicates selects
every series in IDC, which is a plausible-looking answer rather than an error, so the compiler
returns the predicates it actually applied plus caller-facing warnings and every response built
from a filter echoes them (see ``CohortCounts.filters_applied`` / ``.warnings``).
"""

from __future__ import annotations

from typing import Any, NamedTuple

from . import schema
from .errors import InvalidQueryError
from .models import CohortFilters, NumericRange

UNFILTERED_WARNING = (
    "No filter predicates were applied, so this result describes the ENTIRE IDC archive, not a "
    "cohort. If you meant to filter, the filter did not arrive in a usable shape — compare "
    "`filters_applied` with what you sent, then re-send it."
)


class CompiledFilters(NamedTuple):
    """The compiled filter, plus what the caller needs to see to trust it.

    ``where`` is ``TRUE`` when nothing survived compilation — hence ``applied`` and ``warnings``,
    which let a caller tell "no data matched" apart from "my filter was silently dropped".
    """

    where: str
    params: list[Any]
    applied: CohortFilters  # only the predicates that made it into `where`
    warnings: list[str]


def compile_filters(filters: CohortFilters) -> CompiledFilters:
    """Compile ``filters`` into a WHERE clause with bound params, the effective filter, and
    warnings for anything dropped along the way."""
    clauses: list[str] = []
    params: list[Any] = []
    applied_terms: dict[str, list[str]] = {}
    applied_ranges: dict[str, NumericRange] = {}
    warnings: list[str] = []

    for attr, values in (filters.terms or {}).items():
        if attr not in schema.TERM_ATTRIBUTES:
            raise InvalidQueryError(
                f"Unknown or non-term filter attribute: {attr!r}. "
                "Use list_attributes to see valid attributes."
            )
        values = [v for v in (values or []) if v is not None]
        if not values:
            warnings.append(
                f"Term filter {attr!r} was ignored because its value list is empty; it "
                "constrains nothing."
            )
            continue
        placeholders = ", ".join(["?"] * len(values))
        clauses.append(f'"{attr}" IN ({placeholders})')
        params.extend(values)
        applied_terms[attr] = values

    for attr, rng in (filters.ranges or {}).items():
        if attr not in schema.RANGE_ATTRIBUTES:
            raise InvalidQueryError(
                f"Unknown or non-range filter attribute: {attr!r}. "
                "Use list_attributes to see valid attributes."
            )
        if rng.gte is None and rng.lte is None:
            warnings.append(
                f"Range filter {attr!r} was ignored because neither 'gte' nor 'lte' was set; it "
                "constrains nothing."
            )
            continue
        if rng.gte is not None:
            clauses.append(f'"{attr}" >= ?')
            params.append(rng.gte)
        if rng.lte is not None:
            clauses.append(f'"{attr}" <= ?')
            params.append(rng.lte)
        applied_ranges[attr] = rng

    if not clauses:
        warnings.append(UNFILTERED_WARNING)

    return CompiledFilters(
        where=" AND ".join(clauses) if clauses else "TRUE",
        params=params,
        applied=CohortFilters(terms=applied_terms, ranges=applied_ranges),
        warnings=warnings,
    )


def require_filter(compiled: CompiledFilters, action: str) -> None:
    """Refuse ``action`` when no predicate survived compilation.

    The aggregate surfaces (counts, licenses) answer an unfiltered filter honestly — "how big is
    IDC" is a real question, and it costs one query. The surfaces that enumerate *per series* do
    not: an unfiltered manifest is a download payload for the whole archive, which no caller
    means to ask for. Those raise instead, with the dropped-predicate warnings attached so the
    caller can see *why* their filter came out empty.
    """
    if compiled.applied.terms or compiled.applied.ranges:
        return
    dropped = [w for w in compiled.warnings if w != UNFILTERED_WARNING]
    raise InvalidQueryError(
        f"At least one filter predicate is required to {action}: unfiltered, that is every series "
        "in IDC (100+ TB). Use the stats surface for archive-wide totals, or cohort counts to "
        "size a filter first." + ("".join(f" {w}" for w in dropped))
    )
