"""HSTSMiddleware: the shared header injector used by both HTTP adapters."""

from __future__ import annotations

import asyncio

from idc_api.http_headers import HSTSMiddleware


def _run(app, scope):
    """Drive an ASGI app for one request; return the response-start messages."""
    sent = []

    async def receive():  # pragma: no cover - never called for these apps
        return {"type": "http.request"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


def test_replaces_existing_header_instead_of_duplicating():
    # A duplicate Strict-Transport-Security (e.g. injected by a proxy or another middleware)
    # is ambiguous to clients, so the middleware must replace, never append alongside.
    async def inner(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"Strict-Transport-Security", b"max-age=1")],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    (start, _body) = _run(HSTSMiddleware(inner, max_age=3600), {"type": "http"})
    hsts = [v for k, v in start["headers"] if k.lower() == b"strict-transport-security"]
    assert hsts == [b"max-age=3600; includeSubDomains"]


def test_non_http_scopes_pass_through():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    _run(HSTSMiddleware(inner, max_age=3600), {"type": "lifespan"})
    assert seen == ["lifespan"]
