"""Response payloads shaped like the Polymarket Gamma and CLOB APIs.

Two quirks of the real API are reproduced here on purpose, because both have
already caused bugs in this codebase and a fixture that smooths them over
would hide the next one:

1. Gamma encodes `outcomes`, `outcomePrices` and `clobTokenIds` as JSON
   *strings*, not arrays: `'["Yes", "No"]'`. Indexing the raw value yields a
   single character.
2. The outcome order is not fixed. `["No", "Yes"]` happens, so index 0 is not
   reliably "Yes" — `yes_first=False` builds that case.
"""
from __future__ import annotations

import json
from typing import Optional, Sequence

GAMMA = "gamma-api.polymarket.com"
GAMMA_EVENTS = "gamma-api.polymarket.com/events"
GAMMA_MARKETS = "gamma-api.polymarket.com/markets"
CLOB = "clob.polymarket.com"
CLOB_MIDPOINT = "clob.polymarket.com/midpoint"
CLOB_PRICE = "clob.polymarket.com/price"
CLOB_BOOK = "clob.polymarket.com/book"
CLOB_MARKETS = "clob.polymarket.com/markets"


def midpoint(price: float) -> dict:
    """CLOB `/midpoint` returns the price as a STRING, not a float."""
    return {"mid": f"{price:.4f}"}


def price(value: float) -> dict:
    return {"price": f"{value:.4f}"}


def book(
    bid: Optional[float] = 0.60,
    ask: Optional[float] = 0.64,
    *,
    depth: int = 3,
) -> dict:
    """CLOB `/book`. Pass bid=None or ask=None for a one-sided book, which
    `get_book_summary` must reject rather than half-price."""
    bids = [
        {"price": f"{bid - i * 0.01:.4f}", "size": f"{100 + i * 50}"}
        for i in range(depth)
    ] if bid is not None else []
    asks = [
        {"price": f"{ask + i * 0.01:.4f}", "size": f"{100 + i * 50}"}
        for i in range(depth)
    ] if ask is not None else []
    return {"bids": bids, "asks": asks}


def crossed_book(bid: float = 0.70, ask: float = 0.60) -> dict:
    """Bid above ask — malformed, and `get_book_summary` returns None."""
    return {"bids": [{"price": f"{bid}", "size": "100"}],
            "asks": [{"price": f"{ask}", "size": "100"}]}


def sub_market(
    *,
    question: str = "Will the highest temperature in Austin be 95-96°F on July 28?",
    slug: str = "will-the-highest-temperature-in-austin-be-95-96-on-july-28",
    token_ids: Sequence[str] = ("11111", "22222"),
    yes_price: float = 0.62,
    yes_first: bool = True,
    closed: bool = False,
    resolved: bool = False,
    won: Optional[bool] = None,
) -> dict:
    """One bucket of a temperature event.

    `won` settles the market: True means Yes paid out. It implies
    resolved/closed, and drives outcomePrices to the definitive 1/0 split the
    resolution job looks for.
    """
    labels = ["Yes", "No"] if yes_first else ["No", "Yes"]
    if won is None:
        p_yes, p_no = yes_price, 1.0 - yes_price
    else:
        p_yes, p_no = (1.0, 0.0) if won else (0.0, 1.0)
        resolved = closed = True

    prices = [p_yes, p_no] if yes_first else [p_no, p_yes]
    tokens = list(token_ids) if yes_first else list(reversed(token_ids))

    return {
        "question": question,
        "slug": slug,
        "closed": closed,
        "resolved": resolved,
        # JSON-encoded strings, exactly as Gamma sends them.
        "outcomes": json.dumps(labels),
        "outcomePrices": json.dumps([f"{p}" for p in prices]),
        "clobTokenIds": json.dumps(tokens),
    }


def event(
    *,
    slug: str = "highest-temperature-in-austin-on-july-28",
    title: str = "Highest temperature in Austin on July 28?",
    markets: Optional[list[dict]] = None,
    closed: bool = False,
) -> dict:
    return {
        "slug": slug,
        "title": title,
        "closed": closed,
        "markets": markets if markets is not None else [sub_market()],
    }


def events_response(*events_: dict) -> list[dict]:
    """Gamma `/events?slug=...` returns a LIST even for a single slug."""
    return list(events_) if events_ else [event()]


def no_events() -> list:
    """Slug not found. Distinct from an error: HTTP 200 with an empty list."""
    return []


def gamma_markets_page(
    *,
    city: str = "austin",
    count: int = 2,
    event_slug: str = "highest-temperature-in-austin-on-july-28",
    temperature: bool = True,
) -> list[dict]:
    """A page of Gamma `/markets?closed=false`, as the discovery scan reads it.

    `temperature=False` yields non-weather markets, so a test can prove the
    keyword filter actually excludes them instead of ingesting everything.
    """
    if temperature:
        q = "Will the highest temperature in {city} be {lo}-{hi}°F on July 28?"
    else:
        q = "Will {city} win the {lo}-{hi} playoff series?"
    return [
        {
            "question": q.format(city=city.title(), lo=90 + i * 2, hi=91 + i * 2),
            "slug": f"{city}-market-{i}",
            "closed": False,
            "events": [{"slug": event_slug}],
            "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps(["0.5", "0.5"]),
            "clobTokenIds": json.dumps([f"tok{i}a", f"tok{i}b"]),
        }
        for i in range(count)
    ]


def clob_markets_page(*, next_cursor: str = "LTE=", count: int = 2) -> dict:
    """CLOB `/markets` is cursor-paginated and wraps rows in `data`.
    `next_cursor == "LTE="` is Polymarket's end-of-list sentinel."""
    return {
        "next_cursor": next_cursor,
        "data": [
            {
                "question": f"Will the highest temperature in Dallas be {95 + i}°F on July 28?",
                "market_slug": f"dallas-market-{i}",
                "closed": False,
                "event": {"slug": "highest-temperature-in-dallas-on-july-28"},
            }
            for i in range(count)
        ],
    }


def rate_limited() -> dict:
    """Body served with HTTP 429 — the discovery scan backs off on this."""
    return {"error": "rate limit exceeded"}
