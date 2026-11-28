"""November trades (v2/trades?user=...) of the leading consistent wallets.
Reads leaderboard.json, writes trades_nov.json in the cwd.
"""
import json, urllib.request, urllib.parse, time
from datetime import datetime, timezone
B = "https://data-api.polymarket.com/v2"
NOV1 = int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp())
lb = json.load(open('leaderboard.json'))
a = {r['user_id']: r for r in lb['all']}; m = {r['user_id']: r for r in lb['month']}
both = sorted([u for u in a if u in m], key=lambda u: -a[u]['pnl'])[:17]
top_all = [r['user_id'] for r in lb['all'][:3]]
wallets = list(dict.fromkeys(both + top_all))
def get(u):
    for k in range(6):
        try:
            return json.loads(urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'Mozilla/5.0'}), timeout=40).read())
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504): raise
        except Exception:
            pass
        time.sleep(2 ** k)
    raise RuntimeError(u)
out = {}
for w in wallets:
    name = (a.get(w) or m.get(w))['user_name']; trades, cur = [], None
    for page in range(25):
        u = f"{B}/trades?user={w}&limit=1000" + (f"&cursor={urllib.parse.quote(cur)}" if cur else "")
        j = get(u); d = j['data']; trades += d
        cur = j['pagination'].get('next_cursor')
        if not d or min(x['timestamp'] for x in d) < NOV1 or not j['pagination'].get('has_more'): break
        time.sleep(0.3)
    nov = [t for t in trades if t['timestamp'] >= NOV1]
    temp = [t for t in nov if 'highest-temperature' in (t.get('event_slug') or '')]
    out[w] = {'name': name, 'trades': temp, 'n_all_nov': len(nov), 'capped': page == 24}
    print(f"{name[:24]:24s} Nov trades {len(nov):6d}, of them highest-temperature {len(temp):6d}{'  (capped)' if page == 24 else ''}", flush=True)
json.dump(out, open('trades_nov.json', 'w'))
