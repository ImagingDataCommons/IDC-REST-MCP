"""IDC API — LLM-first REST API and MCP server for NCI Imaging Data Commons.

A single backend-agnostic ``core`` library holds all domain logic; the ``rest`` and
``mcp`` packages are thin adapters over it. See ``dev/api_v3_plan.md`` for the design.
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    # Single source of truth: `version` in pyproject.toml. Do not hardcode it here — the version
    # the *running server* advertises resolves through ``core.version``, which reads the same
    # distribution metadata, so a literal here would silently drift at release time.
    __version__ = _pkg_version("idc-api")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+unknown"
