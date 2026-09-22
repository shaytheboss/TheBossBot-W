"""Deterministic HTTP layer for the test suite.

Every outbound call in this codebase goes through `httpx.AsyncClient`, but not
all of them go through `BaseCollector._get`. Three places build their own
client:

    app/api/admin.py:371,406       — Polymarket resolution / discovery helpers
    app/workers/jobs.py:440,926    — market sync + intraday
    app/utils/polymarket_discovery — takes a client as a parameter

Patching `_get` would therefore leave holes. Instead we patch the transport
inside `httpx.AsyncClient.__init__`, which is the one thing all four routes
have in common: whoever constructs the client, and whatever they pass, the
bytes end up in this router.

A request that matches no route raises `UnmockedRequest`. That is deliberate:
a silent fallback response would let a test "pass" against a payload nobody
wrote, which is exactly the class of bug the harness exists to prevent.
"""
from __future__ import annotations

import json as _json
from typing import Any, Callable, Optional

import httpx


class UnmockedRequest(AssertionError):
    """Raised when a test makes an HTTP call it did not register a route for."""


class _Route:
    __slots__ = ("method", "match", "responses", "once", "hits")

    def __init__(self, method: str, match: str, responses: list, once: bool):
        self.method = method.upper()
        self.match = match
        self.responses = responses
        self.once = once
        self.hits = 0

    def accepts(self, request: httpx.Request) -> bool:
        if self.method != "ANY" and request.method.upper() != self.method:
            return False
        if self.once and self.hits >= len(self.responses):
            return False
        return self.match in str(request.url)

    def take(self, request: httpx.Request) -> httpx.Response:
        # Sticky: the last registered response repeats forever unless `once`.
        idx = min(self.hits, len(self.responses) - 1)
        self.hits += 1
        entry = self.responses[idx]
        return entry(request) if callable(entry) else entry


class HttpRouter:
    """Matches requests by substring and returns canned responses.

    Routes are tried in registration order, so a specific route registered
    first wins over a broader one registered later.
    """

    def __init__(self) -> None:
        self._routes: list[_Route] = []
        self.requests: list[httpx.Request] = []

    # ── registration ──────────────────────────────────────────────────────

    def add(
        self,
        match: str,
        json: Any = None,
        *,
        status: int = 200,
        text: Optional[str] = None,
        method: str = "GET",
        once: bool = False,
    ) -> "HttpRouter":
        """Register one response for every URL containing `match`."""
        return self.add_sequence(
            match, [self._build(status, json, text)], method=method, once=once
        )

    def add_sequence(
        self,
        match: str,
        responses: list,
        *,
        method: str = "GET",
        once: bool = False,
    ) -> "HttpRouter":
        """Register responses served in order; the last one repeats.

        Use this to drive retry paths, e.g. `[resp(429), resp(200, payload)]`
        asserts that `BaseCollector._get` really does retry and then succeed.
        """
        if not responses:
            raise ValueError("add_sequence needs at least one response")
        self._routes.append(_Route(method, match, list(responses), once))
        return self

    def prepend(
        self,
        match: str,
        json: Any = None,
        *,
        status: int = 200,
        text: Optional[str] = None,
        method: str = "GET",
        once: bool = False,
    ) -> "HttpRouter":
        """Register a route that WINS over anything already registered.

        Routes are matched in registration order, so a fixture that has
        already wired the happy path cannot be overridden with `add`. This is
        how a test breaks one provider on an otherwise healthy world.
        """
        return self.prepend_sequence(
            match, [self._build(status, json, text)], method=method, once=once
        )

    def prepend_sequence(
        self, match: str, responses: list, *, method: str = "GET", once: bool = False,
    ) -> "HttpRouter":
        if not responses:
            raise ValueError("prepend_sequence needs at least one response")
        self._routes.insert(0, _Route(method, match, list(responses), once))
        return self

    def add_handler(
        self, match: str, handler: Callable[[httpx.Request], httpx.Response],
        *, method: str = "GET",
    ) -> "HttpRouter":
        """Register a callable that builds the response from the request.

        Needed when the answer depends on a query parameter — a `token_id`, or
        an ICAO code — rather than being fixed per URL.
        """
        self._routes.append(_Route(method, match, [handler], once=False))
        return self

    @staticmethod
    def _build(status: int, json: Any, text: Optional[str]) -> httpx.Response:
        if text is not None:
            return httpx.Response(status, text=text)
        if json is not None:
            return httpx.Response(status, json=json)
        return httpx.Response(status)

    # ── dispatch ──────────────────────────────────────────────────────────

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for route in self._routes:
            if route.accepts(request):
                return route.take(request)
        raise UnmockedRequest(
            f"No route registered for {request.method} {request.url}\n"
            f"Registered: {[r.match for r in self._routes] or '(none)'}"
        )

    # ── assertions ────────────────────────────────────────────────────────

    def calls_to(self, match: str) -> list[httpx.Request]:
        return [r for r in self.requests if match in str(r.url)]

    def count(self, match: str = "") -> int:
        return len(self.calls_to(match)) if match else len(self.requests)

    def last(self, match: str = "") -> Optional[httpx.Request]:
        hits = self.calls_to(match) if match else self.requests
        return hits[-1] if hits else None

    def param(self, match: str, name: str) -> Optional[str]:
        """Query-string value of `name` on the most recent call to `match`."""
        req = self.last(match)
        return req.url.params.get(name) if req else None

    def reset(self) -> None:
        self.requests.clear()
        for route in self._routes:
            route.hits = 0


def install(monkeypatch, router: HttpRouter) -> HttpRouter:
    """Force every `httpx.AsyncClient` built from now on onto `router`.

    The caller's own `transport=` argument is intentionally discarded — the
    point is that no code path can opt out of the mock.
    """
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(router.handle)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    return router


def json_bytes(payload: Any) -> bytes:
    """Serialise a payload the way an API would, for raw-body assertions."""
    return _json.dumps(payload).encode()
