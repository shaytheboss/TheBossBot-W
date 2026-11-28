"""Moves of >=10c within 15 minutes on the event day (1-minute prices): average
move after 30/60 min, and the result of fading or following the move held to
settlement, at the mid and after the median half-spread (from the shadow CSV).
Usage: python -I 06_minute_jumps.py <DATA>
"""
import json, sys, numpy as np, pandas as pd
from zoneinfo import ZoneInfo
from datetime import datetime, timezone
S = sys.argv[1]
meta = json.load(open(S + '/ph/ph_meta.json'))
z = np.load(S + '/ph/ph_series.npz')
SH_CITY = {'nyc': 'New York', 'sao-paulo': 'São Paulo'}
def shadow_city(c): return SH_CITY.get(c, ' '.join(w.capitalize() for w in c.split('-')))

sh = pd.read_csv(S + '/shadow_in/shadow.csv', usecols=['bid', 'ask', 'price_live'])
sh = sh[(sh.price_live == 1) & sh.bid.notna() & sh.ask.notna()]
sh['mid'] = (sh.bid + sh.ask) / 2; sh['spr'] = sh.ask - sh.bid
bands = np.array([0, .05, .15, .30, .50, .70, .85, .95, 1.01])
sh['b'] = np.digitize(sh.mid, bands)
HALF = (sh.groupby('b').spr.median() / 2).to_dict()
print("median half-spread by YES price:", {f"{bands[k-1]:.2f}-{bands[k]:.2f}": round(100*v, 2) for k, v in sorted(HALF.items())})
def half_spread(p): return HALF.get(int(np.digitize([p], bands)[0]), 0.02)

# ── events: a move of >= X within 15 minutes, on the city's local event day
rows = []
for m in meta:
    k = f"t_{m['token']}"
    if k not in z.files: continue
    t, p = z[k], z[f"p_{m['token']}"].astype(float)
    if len(t) < 30: continue
    grid = np.arange(m['start'] + 3600, m['end'], 60)                 # local 00:00 -> 24:00, 1-min grid
    idx = np.searchsorted(t, grid, side='right') - 1
    ok = idx >= 0
    grid, idx = grid[ok], idx[ok]
    if len(grid) < 60: continue
    g = p[idx]
    tz = None
    y = 1.0 if m['yes_won'] else 0.0
    for direction in (1, -1):
        last = -10**9
        for i in range(15, len(g) - 61):
            d15 = g[i] - g[i - 15]
            if direction * d15 >= 0.10 and grid[i] - last >= 3600:
                last = grid[i]
                p1 = g[i + 1]                                          # one minute later
                if not (0.02 <= p1 <= 0.98): continue
                rows.append(dict(slug=m['slug'], city=m['city'], dir=direction, move=abs(d15), p1=p1,
                                 p30=g[i + 31], p60=g[i + 61], y=y, ts=grid[i]))
E = pd.DataFrame(rows)
E['h'] = [half_spread(x) for x in E.p1]
print(f"\njumps found: {len(E)} (up {int((E.dir==1).sum())}, down {int((E.dir==-1).sum())}) on {E.slug.nunique()} markets")

def stat(lab, pnl, cl):
    pnl = np.asarray(pnl); n = len(pnl)
    if n < 10: print(f"  {lab:58s} n={n}"); return
    g = pd.DataFrame({'p': pnl, 'c': np.asarray(cl)}).groupby('c').p.agg(['sum', 'count']); m = pnl.mean(); G = len(g)
    se = np.sqrt(G/(G-1) * ((g['sum'] - g['count']*m)**2).sum()) / n
    print(f"  {lab:58s} n={n:5d}  edge {100*m:+6.2f}¢  t={m/se:+5.2f}")
for direction, name in ((1, "YES jumped UP"), (-1, "YES dropped DOWN")):
    x = E[E.dir == direction]
    print(f"\n{name} >= 10c in 15 min (entry 1 min later):")
    print(f"  average move after: +30 min {100*(x.p30-x.p1).mean():+.2f}c, +60 min {100*(x.p60-x.p1).mean():+.2f}c (same direction = positive)" if direction == 1 else
          f"  average move after: +30 min {100*(x.p30-x.p1).mean():+.2f}c, +60 min {100*(x.p60-x.p1).mean():+.2f}c")
    follow = direction * (x.y - x.p1)            # buy the side that moved up (YES if up, NO if down), held to settlement
    fade = -follow
    stat("FOLLOW the move, at mid (no spread)", follow, x.slug); stat("FOLLOW the move, after paying half-spread", follow - x.h, x.slug)
    stat("FADE the move, at mid (no spread)", fade, x.slug); stat("FADE the move, after paying half-spread", fade - x.h, x.slug)
    for lo, hi in ((0.10, 0.20), (0.20, 0.30), (0.30, 1.01)):
        xx = x[(x.move >= lo) & (x.move < hi)]
        stat(f"   move {int(lo*100)}-{int(hi*100) if hi<1 else 100}c: FADE after half-spread", (-direction * (xx.y - xx.p1)) - xx.h, xx.slug)
    for lo, hi in ((0.02, 0.15), (0.15, 0.50), (0.50, 0.85), (0.85, 0.98)):
        xx = x[(x.p1 >= lo) & (x.p1 < hi)]
        stat(f"   YES price {int(lo*100)}-{int(hi*100)}c after the move: FADE after half-spread", (-direction * (xx.y - xx.p1)) - xx.h, xx.slug)
    first, second = x[x.ts < x.ts.median()], x[x.ts >= x.ts.median()]
    stat("   first half of the month: FADE after half-spread", (-direction * (first.y - first.p1)) - first.h, first.slug)
    stat("   second half of the month: FADE after half-spread", (-direction * (second.y - second.p1)) - second.h, second.slug)
