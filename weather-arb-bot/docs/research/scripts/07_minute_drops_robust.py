"""Moves of >=20c within 15 minutes: fade, by entry delay (1/5/10/15 min), with
half and full spread, by period, local hour, price, and without the busiest
cities. Usage: python -I 07_minute_drops_robust.py <DATA>
"""
import json, sys, numpy as np, pandas as pd
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
S = sys.argv[1]
meta = json.load(open(S + '/ph/ph_meta.json')); z = np.load(S + '/ph/ph_series.npz')
src = open(__import__('os').path.join(__import__('os').path.dirname(__import__('os').path.abspath(__file__)), '04_fetch_minute_prices.py')).read(); TZ = eval(src[src.index("TZ = {") + 5: src.index("}\n", src.index("TZ = {")) + 1])
HALF = {0: .0045, 1: .018, 2: .02, 3: .02, 4: .02, 5: .035, 6: .025, 7: .004}
bands = np.array([.05, .15, .30, .50, .70, .85, .95])
def half(p): return HALF[int(np.searchsorted(bands, p, side='right'))]
rows = []
for m in meta:
    k = f"t_{m['token']}"
    if k not in z.files: continue
    t, p = z[k], z[f"p_{m['token']}"].astype(float)
    if len(t) < 30: continue
    grid = np.arange(m['start'] + 3600, m['end'], 60)
    idx = np.searchsorted(t, grid, side='right') - 1; ok = idx >= 0; grid, idx = grid[ok], idx[ok]
    if len(grid) < 100: continue
    g = p[idx]; y = 1.0 if m['yes_won'] else 0.0; tz = ZoneInfo(TZ[m['city']])
    for direction in (1, -1):
        last = -10**9
        for i in range(15, len(g) - 61):
            d15 = g[i] - g[i - 15]
            if direction * d15 >= 0.20 and grid[i] - last >= 3600:
                last = grid[i]
                ent = {lag: g[i + lag] for lag in (1, 5, 10, 15)}
                if not (0.02 <= ent[1] <= 0.98): continue
                lh = datetime.fromtimestamp(grid[i], tz).hour
                rows.append(dict(slug=m['slug'], city=m['city'], day=m['day'], dir=direction, move=abs(d15), y=y, lh=lh,
                                 **{f"p{l}": v for l, v in ent.items()}, pre=g[i - 15], p60=g[i + 61]))
E = pd.DataFrame(rows)
def stat(lab, pnl, cl):
    pnl = np.asarray(pnl, float); n = len(pnl)
    if n < 10: print(f"  {lab:62s} n={n}"); return
    g = pd.DataFrame({'p': pnl, 'c': np.asarray(cl)}).groupby('c').p.agg(['sum','count']); m = pnl.mean(); G = len(g)
    se = np.sqrt(G/(G-1) * ((g['sum'] - g['count']*m)**2).sum()) / n
    print(f"  {lab:62s} n={n:4d}  edge {100*m:+6.2f}¢  t={m/se:+5.2f}")
for direction, name, side in ((-1, "YES DROPPED 20c+ in 15 min -> buy YES (fade)", 'YES'), (1, "YES JUMPED 20c+ in 15 min -> buy NO (fade)", 'NO')):
    x = E[E.dir == direction].copy()
    print(f"\n{name}: {len(x)} moves on {x.slug.nunique()} markets, {x.day.nunique()} days")
    fade = lambda pe: (-direction) * (x.y - pe)            # buy the side that just got cheaper
    for lag in (1, 5, 10, 15):
        pe = x[f"p{lag}"]; h = [half(v) for v in pe]
        stat(f"entry {lag:2d} min after: at mid", fade(pe), x.slug)
        stat(f"entry {lag:2d} min after: minus half-spread", fade(pe) - h, x.slug)
        stat(f"entry {lag:2d} min after: minus a FULL spread (2x, stress)", fade(pe) - 2*np.array(h), x.slug)
    pe = x.p5; h = np.array([half(v) for v in pe]); pnl = fade(pe) - h
    days = sorted(x.day.unique()); cut = days[len(days)//2]
    print(" robustness (entry 5 min after, minus half-spread):")
    stat(f"   event days before {cut}", pnl[x.day < cut], x.slug[x.day < cut]); stat(f"   event days from {cut}", pnl[x.day >= cut], x.slug[x.day >= cut])
    for lo, hi in ((0, 10), (10, 16), (16, 24)):
        mm = (x.lh >= lo) & (x.lh < hi); stat(f"   local hour {lo:02d}-{hi:02d}", pnl[mm], x.slug[mm])
    for lo, hi in ((0.02, 0.15), (0.15, 0.50), (0.50, 0.98)):
        mm = (x.p5 >= lo) & (x.p5 < hi); stat(f"   YES price {int(lo*100)}-{int(hi*100)}c at entry", pnl[mm], x.slug[mm])
    top = x.groupby('city').size().sort_values(ascending=False)
    print(f"   cities with most moves: {top.head(6).to_dict()}  (of {x.city.nunique()} cities)")
    exc = x[~x.city.isin(top.head(3).index)]; pe2 = exc.p5; pnl2 = (-direction) * (exc.y - pe2) - np.array([half(v) for v in pe2])
    stat("   without the 3 busiest cities", pnl2, exc.slug)
