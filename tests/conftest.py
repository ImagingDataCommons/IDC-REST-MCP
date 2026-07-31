"""Shared fixtures. The default build (IDC_API_INCLUDE_INDICES=all) fetches the specialized
idc-index parquet on first run; set IDC_API_INCLUDE_INDICES=none for a bundled-only build."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from idc_api.core.context import get_context
from idc_api.rest.app import app


@pytest.fixture(scope="session")
def ctx():
    return get_context()


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


def mcp_json(result) -> object:
    """Normalize an MCP ``call_tool`` return (``CallToolResult``) into plain Python."""
    structured = result.structured_content
    if structured is not None:
        # The SDK wraps bare list/scalar returns under "result"; dict returns come through as-is.
        return structured.get("result", structured)
    for block in result.content:  # Sequence[ContentBlock]
        text = getattr(block, "text", None)
        if text is not None:
            return json.loads(text)
    return None


@pytest.fixture
def parse_mcp():
    return mcp_json
