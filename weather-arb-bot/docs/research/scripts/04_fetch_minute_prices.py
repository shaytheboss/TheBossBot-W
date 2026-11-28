"""1-minute YES price history (CLOB /prices-history, fidelity=1) for every bucket
of every closed temperature market in <DATA>/gamma, over the city's local event
day. Writes <DATA>/ph/ph_series.npz and ph_meta.json. ~12.8k requests, ~5 min.
Usage: python -I 04_fetch_minute_prices.py <DATA>
"""
"""Fetch 1-minute YES price history for every bucket of every closed November
temperature market, over the city's local event day. Writes $S/ph/ph_*.npz."""
import asyncio, glob, json, sys, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import httpx, numpy as np
S = sys.argv[1]
TZ = {'amsterdam':'Europe/Amsterdam','ankara':'Europe/Istanbul','atlanta':'America/New_York','austin':'America/Chicago',
 'beijing':'Asia/Shanghai','buenos-aires':'America/Argentina/Buenos_Aires','busan':'Asia/Seoul','cape-town':'Africa/Johannesburg',
 'chengdu':'Asia/Shanghai','chicago':'America/Chicago','chongqing':'Asia/Shanghai','dallas':'America/Chicago','denver':'America/Denver',
 'guangzhou':'Asia/Shanghai','helsinki':'Europe/Helsinki','hong-kong':'Asia/Hong_Kong','houston':'America/Chicago','istanbul':'Europe/Istanbul',
 'jeddah':'Asia/Riyadh','karachi':'Asia/Karachi','kuala-lumpur':'Asia/Kuala_Lumpur','london':'Europe/London','los-angeles':'America/Los_Angeles',
 'lucknow':'Asia/Kolkata','madrid':'Europe/Madrid','manila':'Asia/Manila','mexico-city':'America/Mexico_City','miami':'America/New_York',
 'milan':'Europe/Rome','moscow':'Europe/Moscow','munich':'Europe/Berlin','nyc':'America/New_York','panama-city':'America/Panama','paris':'Europe/Paris',
 'qingdao':'Asia/Shanghai','san-francisco':'America/Los_Angeles','sao-paulo':'America/Sao_Paulo','seattle':'America/Los_Angeles','seoul':'Asia/Seoul',
 'shanghai':'Asia/Shanghai','shenzhen':'Asia/Shanghai','singapore':'Asia/Singapore','taipei':'Asia/Taipei','tel-aviv':'Asia/Jerusalem',
 'tokyo':'Asia/Tokyo','toronto':'America/Toronto','warsaw':'Europe/Warsaw','wellington':'Pacific/Auckland','wuhan':'Asia/Shanghai'}
evs = {}
for f in glob.glob(S + '/gamma/closed_*.json'):
    for e in (json.load(open(f)) or []):
        if isinstance(e, dict) and 'highest-temperature' in e.get('slug', ''):
            evs[e['slug']] = e
jobs = []
for slug, e in evs.items():
    city = slug.split('highest-temperature-in-')[1].split('-on-')[0]
    day = datetime.strptime(slug.rsplit('-on-', 1)[1], '%B-%d-%Y').date()
    tz = ZoneInfo(TZ[city])
    start = datetime(day.year, day.month, day.day, tzinfo=tz) - timedelta(hours=1)
    end = start + timedelta(hours=25)
    for m in e.get('markets', []):
        try:
            toks = json.loads(m['clobTokenIds']); outs = json.loads(m['outcomes']); prices = json.loads(m['outcomePrices'])
        except Exception:
            continue
        yi = [o.lower() for o in outs].index('yes')
        jobs.append(dict(slug=slug, city=city, day=str(day), bucket=m.get('groupItemTitle'), token=toks[yi],
                         yes_won=float(prices[yi]) >= 0.99, start=int(start.timestamp()), end=int(end.timestamp())))
print(len(jobs), 'tokens', flush=True)
out, meta, done, fails = {}, [], 0, 0
async def one(client, sem, j):
    global done, fails
    async with sem:
        for attempt in range(6):
            try:
                r = await client.get('https://clob.polymarket.com/prices-history',
                                     params={'market': j['token'], 'startTs': j['start'], 'endTs': j['end'], 'fidelity': 1})
                if r.status_code == 429 or r.status_code >= 500:
                    await asyncio.sleep(2 ** attempt); continue
                h = r.json().get('history', []) if r.status_code == 200 else []
                out[j['token']] = (np.array([x['t'] for x in h], dtype=np.int64), np.array([x['p'] for x in h], dtype=np.float32))
                meta.append(j); done += 1
                if done % 500 == 0: print(done, 'done', fails, 'failed', time.strftime('%H:%M:%S'), flush=True)
                return
            except Exception:
                await asyncio.sleep(2 ** attempt)
        fails += 1
async def main():
    sem = asyncio.Semaphore(8)
    async with httpx.AsyncClient(headers={'User-Agent': 'Mozilla/5.0'}, timeout=40) as client:
        await asyncio.gather(*(one(client, sem, j) for j in jobs))
    np.savez_compressed(S + '/ph/ph_series.npz', **{f"t_{k}": v[0] for k, v in out.items()}, **{f"p_{k}": v[1] for k, v in out.items()})
    json.dump(meta, open(S + '/ph/ph_meta.json', 'w'))
    print('FINISHED', done, 'ok', fails, 'failed', flush=True)
asyncio.run(main())
