"""Weather-category leaderboard (data-api.polymarket.com/v2/leaderboard,
time_period=week|month|all, cursor paging) and the wallets in both the
all-time and this month's top 300. Writes leaderboard.json in the cwd.
"""
import json, urllib.request, urllib.parse, time
B = "https://data-api.polymarket.com/v2"
def get(u):
    for a in range(5):
        try:
            return json.loads(urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'Mozilla/5.0'}), timeout=30).read())
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429: raise
            time.sleep(2 ** a)
        except Exception:
            time.sleep(2 ** a)
    raise RuntimeError(u)
out = {}
for per in ('week', 'month', 'all'):
    rows, cur = [], None
    for _ in range(3):
        u = f"{B}/leaderboard?category=weather&time_period={per}&limit=100" + (f"&cursor={urllib.parse.quote(cur)}" if cur else "")
        j = get(u); rows += j['data']; cur = j['pagination'].get('next_cursor')
        if not j['pagination'].get('has_more'): break
    out[per] = rows
json.dump(out, open('leaderboard.json', 'w'))
m = {r['user_id']: r for r in out['month']}; a = {r['user_id']: r for r in out['all']}; w = {r['user_id']: r for r in out['week']}
print({k: len(v) for k, v in out.items()})
both = [u for u in a if u in m]
print(f"in the all-time top {len(a)} AND this month's top {len(m)}: {len(both)} wallets\n")
print(f"{'name':26s} {'all-time pnl':>12s} {'all vol':>11s} {'ret':>6s} | {'month pnl':>9s} {'ret':>6s} | {'week pnl':>8s}")
for u in sorted(both, key=lambda u: -a[u]['pnl'])[:30]:
    ar, mr = a[u], m[u]; wk = w.get(u, {}).get('pnl')
    print(f"{ar['user_name'][:26]:26s} {ar['pnl']:12,.0f} {ar['volume']:11,.0f} {100*ar['pnl']/max(ar['volume'],1):5.1f}% | {mr['pnl']:9,.0f} {100*mr['pnl']/max(mr['volume'],1):5.1f}% | {'—' if wk is None else f'{wk:8,.0f}'}")
