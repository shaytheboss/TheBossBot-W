"""Replay app/nearby/signal.py on live aviationweather METARs (last 30 h) for
every target in NEARBY: when the neighbour condition first fired today.
Run from the repo root (imports app.nearby).
"""
import json, sys, urllib.request, gzip
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
sys.path.insert(0, '.')
from app.nearby import signal as sig
from app.nearby.stations import NEARBY

TZ = {'RJ':'Asia/Tokyo','RK':'Asia/Seoul','ZB':'Asia/Shanghai','ZS':'Asia/Shanghai','ZG':'Asia/Shanghai','ZU':'Asia/Shanghai',
      'ZH':'Asia/Shanghai','ZL':'Asia/Shanghai','ZY':'Asia/Shanghai','ZW':'Asia/Shanghai','VH':'Asia/Hong_Kong','RC':'Asia/Taipei',
      'WS':'Asia/Singapore','WM':'Asia/Kuala_Lumpur','RP':'Asia/Manila','VI':'Asia/Kolkata','VA':'Asia/Kolkata','VO':'Asia/Kolkata',
      'OP':'Asia/Karachi','OE':'Asia/Riyadh','LT':'Europe/Istanbul','LL':'Asia/Jerusalem','NZ':'Pacific/Auckland','VT':'Asia/Bangkok',
      'WI':'Asia/Jakarta','OM':'Asia/Dubai','OT':'Asia/Qatar','UU':'Europe/Moscow','FA':'Africa/Johannesburg','EF':'Europe/Helsinki',
      'EP':'Europe/Warsaw','ED':'Europe/Berlin','LF':'Europe/Paris','EG':'Europe/London','EH':'Europe/Amsterdam','LE':'Europe/Madrid',
      'LI':'Europe/Rome','LO':'Europe/Vienna','LK':'Europe/Prague','EK':'Europe/Copenhagen','ES':'Europe/Stockholm','EN':'Europe/Oslo',
      'LG':'Europe/Athens','LH':'Europe/Budapest','LR':'Europe/Bucharest','EB':'Europe/Brussels','LS':'Europe/Zurich','EI':'Europe/Dublin',
      'LP':'Europe/Lisbon','SA':'America/Argentina/Buenos_Aires','SB':'America/Sao_Paulo'}
now = datetime.now(timezone.utc)
targets = sorted(NEARBY)
ids = sorted(set(targets) | {i for t in targets for i, *_ in NEARBY[t]})
recs = []
for k in range(0, len(ids), 8):
    url = "https://aviationweather.gov/api/data/metar?format=json&hours=30&ids=" + ",".join(ids[k:k+8])
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip"})
    raw = urllib.request.urlopen(req, timeout=60).read()
    try: raw = gzip.decompress(raw)
    except OSError: pass
    recs += json.loads(raw or b"[]")
obs = {}
for r in recs:
    if r.get("temp") is None or not r.get("reportTime"): continue
    t = datetime.fromisoformat(r["reportTime"].replace("Z", "+00:00"))
    obs.setdefault(r["icaoId"], []).append((t, r["temp"] * 9 / 5 + 32))
print(f"now {now:%H:%M} UTC; METAR records {len(recs)}, stations with data {len(obs)} of {len(ids)}")
for tgt in targets:
    tz = TZ.get(tgt[:2]) or TZ.get(tgt[:1])
    if not tz: continue
    zi = ZoneInfo(tz); loc_now = now.astimezone(zi); day = loc_now.date()
    if loc_now.hour < 10: continue            # window not started today
    day_obs = lambda i: [(t, v) for t, v in obs.get(i, []) if t.astimezone(zi).date() == day]
    start = datetime.combine(day, datetime.min.time(), zi).replace(hour=10).astimezone(timezone.utc)
    end = min(now, datetime.combine(day, datetime.min.time(), zi).replace(hour=20).astimezone(timezone.utc))
    first = None; best = (0, 0, None)
    t = start
    while t <= end:
        tgt_s = sig.station_state(tgt, day_obs(tgt), t)
        nb = [sig.station_state(i, day_obs(i), t) for i, *_ in NEARBY[tgt]]
        v = sig.evaluate(tgt_s, nb)
        if v is not None:
            if v.fires and first is None: first = (t.astimezone(zi).strftime('%H:%M'), len(v.falling), len(v.nearby))
            if v.share > best[0]: best = (v.share, len(v.nearby), t.astimezone(zi).strftime('%H:%M'))
        t += timedelta(minutes=5)
    live = sum(1 for i, *_ in NEARBY[tgt] if day_obs(i))
    print(f"{tgt} {tz:<22} local {loc_now:%H:%M}  nbrs with data today {live}/{len(NEARBY[tgt])}  "
          f"first fire {first or '—'}   best share {best[0]:.0%} of {best[1]} at {best[2]}")
