"""Small helpers shared by several services, so every endpoint reports the same thing the same
way."""

from __future__ import annotations

import idc_index_data

_MB_PER_TB = 1_000_000


def mb_to_tb(mb: float) -> float:
    """Megabytes (as ``series_size_MB`` sums are) to the ``size_TB`` every response reports."""
    return round(mb / _MB_PER_TB, 3)


def served_idc_release() -> int:
    """The IDC data release this server serves: the major version of the pinned
    ``idc-index-data`` (25.0.5 → 25)."""
    return int(idc_index_data.__version__.split(".")[0])
