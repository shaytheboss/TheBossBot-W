"""For a few cities: when the neighbour condition fired, what the holding
bucket's price was (CLOB prices-history) and whether it was inside the
30-85c band. Needs <DATA>/gamma/ht_open.json (open events). Run from repo root.
"""
import json, sys, urllib.request, gzip
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
sys.path.insert(0, '.')
from app.nearby import signal as sig
from app.nearby.stations import NEARBY
S = sys.argv[1]
ev = {e['slug']: e for e in json.load(open(S + '/gamma/ht_open.json'))}
CASES = [('RJTT', 'Asia/Tokyo', 'tokyo'), ('RKSI', 'Asia/Seoul', 'seoul'),
         ('WSSS', 'Asia/Singapore', 'singapore'), ('WMKK', 'Asia/Kuala_Lumpur', 'kuala-lumpur')]
def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip"})
    raw = urllib.request.urlopen(req, timeout=60).read()
    try: raw = gzip.decompress(raw)
    except OSError: pass
    return json.loads(raw or b"[]")
now = datetime.now(timezone.utc)
for tgt, tz, slug in CASES:
    zi = ZoneInfo(tz); day = now.astimezone(zi).date()
    ids = [tgt] + [i for i, *_ in NEARBY[tgt]]
    obs = {}
    for r in get("https://aviationweather.gov/api/data/metar?format=json&hours=30&ids=" + ",".join(ids)):
        if r.get("temp") is None: continue
        t = datetime.fromisoformat(r["reportTime"].replace("Z", "+00:00"))
        if t.astimezone(zi).date() == day:
            obs.setdefault(r["icaoId"], []).append((t, r["temp"] * 9 / 5 + 32))
    e = ev.get(f"highest-temperature-in-{slug}-on-{day:%B}-{day.day}-{day.year}".lower())
    if not e: print(tgt, 'event not found'); continue
    buckets = [(m.get('groupItemTitle'), json.loads(m['clobTokenIds'])[0]) for m in e['markets']]
    hist_cache = {}
    print(f"\n{tgt} ({slug}) — event {e['slug']}")
    t = datetime.combine(day, datetime.min.time(), zi).replace(hour=10).astimezone(timezone.utc)
    end = datetime.combine(day, datetime.min.time(), zi).replace(hour=20).astimezone(timezone.utc)
    shown = 0
    while t < min(end, now) and shown < 6:
        ts = sig.station_state(tgt, obs.get(tgt, []), t)
        v = sig.evaluate(ts, [sig.station_state(i, obs.get(i, []), t) for i, *_ in NEARBY[tgt]])
        if v is not None and v.fires:
            hc = round((ts.high_f - 32) * 5 / 9)
            label, tok = next(((l, k) for l, k in buckets if l and l.split('°')[0].strip().lstrip('≤≥').split(' ')[0] == str(hc)), (None, None))
            mid = None
            if tok:
                if tok not in hist_cache:
                    st = int((t - timedelta(hours=8)).timestamp()); en = int(end.timestamp())
                    hist_cache[tok] = get(f"https://clob.polymarket.com/prices-history?market={tok}&startTs={st}&endTs={en}&fidelity=5").get('history', [])
                pts = [p for p in hist_cache[tok] if p['t'] <= t.timestamp()]
                mid = pts[-1]['p'] if pts else None
            band = None if mid is None else ('IN band 30-85' if sig.mid_in_band(mid) else 'out of band')
            print(f"  {t.astimezone(zi):%H:%M} local  fires {len(v.falling)}/{len(v.nearby)}  high so far {hc}°C  bucket {label}  price {mid}  -> {band}")
            shown += 1
            t += timedelta(minutes=30); continue
        t += timedelta(minutes=5)
