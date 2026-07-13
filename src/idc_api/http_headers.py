"""Shared ASGI middleware for the two hosted HTTP surfaces (REST and MCP streamable-http).

Lives beside ``settings.py`` rather than under ``rest/`` or ``mcp/`` because both adapters use
it and neither may import the other. Pure ASGI — no FastAPI/Starlette types — so it wraps
either app.
"""

from __future__ import annotations


class HSTSMiddleware:
    """Add ``Strict-Transport-Security`` to every HTTP response.

    NCI security policy requires HSTS on all sites, and it is the application's job: the
    hosting layer (Cloud Run / the load balancer) terminates TLS but does not inject the
    header. ``max_age`` is per-tier — a year in prod, short (e.g. 3600) in dev/test so a
    misconfigured deploy can't lock browsers out of the domain for a year. Browsers ignore
    the header over plain HTTP, so sending it unconditionally (local runs included) is safe.
    """

    def __init__(self, app, max_age: int) -> None:
        self.app = app
        self._header = (
            b"strict-transport-security",
            f"max-age={max_age}; includeSubDomains".encode(),
        )

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_hsts(message) -> None:
            if message["type"] == "http.response.start":
                # Replace, never append alongside: a duplicate Strict-Transport-Security
                # (e.g. from a proxy or a future middleware) is ambiguous to clients.
                message["headers"] = [
                    *(
                        (name, value)
                        for name, value in message.get("headers", [])
                        if name.lower() != b"strict-transport-security"
                    ),
                    self._header,
                ]
            await send(message)

        await self.app(scope, receive, send_with_hsts)
