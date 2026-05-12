# 06 — Polymarket Market Discovery & Question Parsing

## Discovery API

**Endpoint:** `GET https://gamma-api.polymarket.com/events`

**Query for every enabled station:**
```
?q={city}&active=true&closed=false&limit=50
```

Use the station's primary city name **and** each alias. A market may appear under multiple cities (e.g., "New York" vs "NYC"); dedupe by `event.id`.

**Schedule:** every 15 minutes. Run all city queries in parallel via `asyncio.gather`. Total ~20 requests/run.

**IP blocking:** If Railway IPs are blocked, fall back to `POLYMARKET_RELAY_URL` (Cloudflare Worker, same pattern as the tennis bot).

## Event → markets

Each Gamma event contains nested `markets[]`. We're interested only in markets where:

```python
def is_weather_temp_market(market: dict) -> bool:
    text = ((market.get("question") or "") + " " + (market.get("groupItemTitle") or "")).lower()
    return WEATHER_RX.search(text) is not None
```

where

```python
WEATHER_RX = re.compile(
    r"\b(temperature|temp\b|high temp|low temp|degrees?|°[fc]|fahrenheit|celsius|"
    r"hot|cold|reach|exceed|above|below|at most|no more than|between)\b"
)
```

Reject markets containing `rain|snow|hurricane|storm|wind|humidity|precipitation` — different physics, future feature.

## Station resolution

Order of attempts in `app/collectors/polymarket.py:resolve_station(market, event) -> str | None`:

1. **Wunderground ICAO regex** — search `event.description` (plus market description) for:
   ```
   wunderground\.com/history/daily/[^/]+/[^/]+/[^/]+/(K[A-Z]{3})
   ```
   This is the most reliable signal because Polymarket frequently links to a specific station for clarity.
2. **City alias match** — for each station, check if any alias appears as a substring in `event.title`. Use the **longest** alias to avoid false hits ("New York" vs "York").
3. **Question-text match** — same as above on the market's question.
4. **None** — log `unparsed` and skip the market until manually pinned.

Manual pin: admin sends `/setstation <slug> KBKF`, which writes `markets.station_override`.

## Bucket parser (`app/engine/bucket_parser.py`)

Returns a `Bucket` object or `None`:

```python
@dataclass
class Bucket:
    comparator: Literal['gte', 'lte', 'btw']
    lo: float                  # in unit
    hi: float | None           # only for 'btw'
    unit: Literal['F', 'C']
```

### Regex set (port of JS WEATHER patterns)

| # | Pattern                                                           | Yields                                |
| - | ----------------------------------------------------------------- | ------------------------------------- |
| 1 | `between\s+(\d+)\s*(?:°|degrees?)?\s*([fc])?\s+and\s+(\d+)\s*([fc])?` | `btw lo hi unit`                  |
| 2 | `(?:above|over|more than|exceed(?:s|ing)?|greater than)\s+(\d+)\s*(?:°|degrees?)?\s*([fc])?` | `gte lo unit`        |
| 3 | `(?:below|under|less than|at most|no more than|no higher than)\s+(\d+)\s*(?:°|degrees?)?\s*([fc])?` | `lte lo unit` |
| 4 | `reach\s+(\d+)\s*(?:°|degrees?)?\s*([fc])?`                       | `gte lo unit`                          |
| 5 | `(\d+)\s*(?:°|degrees?)\s*([fc])?\s+or\s+(?:higher|more|above|hotter)` | `gte lo unit`                    |
| 6 | `(\d+)\s*(?:°|degrees?)\s*([fc])?\s+or\s+(?:lower|less|cooler)` | `lte lo unit`                          |
| 7 | `at\s+least\s+(\d+)\s*(?:°|degrees?)?\s*([fc])?`                  | `gte lo unit`                          |
| 8 | `(?:hit|reach)\s+(\d+)\s*(?:°|degrees?)?\s*([fc])?`               | `gte lo unit`                          |
| 9 | Single-number fallback when question contains "Will" and a unit  | log as `unparsed`, manual review only  |

If `unit` is missing, infer:
- `unit = 'F'` if the question mentions "Fahrenheit" or no unit but US city.
- `unit = 'C'` if mentions "Celsius".

Both pricer and storage normalize to °C internally; we keep the unit for display.

### Test cases (must pass before Phase 7)

```python
@pytest.mark.parametrize("text,expected", [
    ("Will Denver reach 75°F on May 14?",                  Bucket("gte", 75, None, "F")),
    ("Will NYC be above 90 degrees on July 4?",            Bucket("gte", 90, None, "F")),
    ("Will Phoenix temperature exceed 110°F on Tue?",      Bucket("gte", 110, None, "F")),
    ("Chicago: high between 60 and 70°F on Friday?",       Bucket("btw", 60, 70, "F")),
    ("Will LA stay below 75°F on Saturday?",               Bucket("lte", 75, None, "F")),
    ("Boston no higher than 85°F on July 1?",              Bucket("lte", 85, None, "F")),
    ("Miami at least 90°F on April 22?",                   Bucket("gte", 90, None, "F")),
    ("Will Seattle hit 30°C on June 1?",                   Bucket("gte", 30, None, "C")),
    ("Denver high 80 or higher on May 14?",                Bucket("gte", 80, None, "F")),
])
def test_parse(text, expected): ...
```

If <90% of a manually labeled sample passes, the parser ships with `unparsed` rate logged + dashboard view of unparsed markets for manual override.

## Resolution date parsing

Polymarket questions almost always include a date. Try in order:

1. ISO format `YYYY-MM-DD` (rare in questions but check first).
2. Long format `on May 14, 2026` / `on May 14`.
3. Short format `on Friday` (resolve to the *next* Friday from `created_at`).
4. From the slug: many slugs end with `-2026-05-14` — last resort.
5. From `market.endDate` field in Gamma response (most reliable for the actual resolution timestamp).

Store **station-local date** in `markets.resolution_date`. Convert `endDate` UTC → station local using `stations.iana_tz`.

## Price extraction

For each market we want **YES price** and **NO price** (with `NO = 1 - YES`). Try in order:

1. `market.outcomePrices` array (JSON-encoded string sometimes).
2. CLOB `/prices` endpoint with `yes_token_id` if `clobTokenIds` is present.
3. Skip (set to NULL, log warning) if both fail.

Volume: `market.volume24hr` and `market.volume`. Optional for display.

## What if we have NO and YES markets as separate Polymarket events?

Some Polymarket weather markets are structured with one YES/NO market in one event. Others split into multiple buckets (`<70`, `70-80`, `>80`) as siblings.

For V1, treat each Polymarket "market" (in the `markets[]` array of the event) as its own betting unit with its own YES/NO. If we see split buckets we open at most one trade per market — the one with the strongest edge.

## What we do NOT parse (V1)

- "Will it rain..." markets.
- "How much snow..." markets.
- Markets resolving on **MULTIPLE** days (e.g. "average over the week").
- Markets resolving on **hourly** readings.
- Hurricane / named-storm markets.

These get filtered out by the keyword regex up front.

## Manual override workflow

If a market is misparsed:
1. Find its `condition_id` via the dashboard's "Unparsed Markets" panel.
2. Send `/setstation <condition_id> KBKF` (admin).
3. Or directly: `UPDATE markets SET station_override='KBKF', comparator='gte', threshold_lo_f=80, unit='F' WHERE condition_id='0xabc...'`.
4. Next eval cycle picks up the override.
