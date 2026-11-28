"""Real track export (tab "Real track" -> CSV): results by price, local hour,
confidence, "already held", event date and combinations.
Usage: python -I 01_realtrack_csv_breakdown.py real-track-YYYY-MM-DD.csv
"""
import csv, sys, math, re
from collections import defaultdict
rows = list(csv.DictReader(open(sys.argv[1], encoding='utf-8-sig')))
def cents(s): return float(s.replace('¢','')) / 100 if s and s not in '—' else None
R = []
for r in rows:
    res = r['Result']
    won = True if 'Won' in res else False if 'Lost' in res else None
    R.append(dict(city=r['City'], conf=int(r['Conf.'].rstrip('%')), price=cents(r['Price']), spread=cents(r['Spread']),
                  held='yes' in r['Regular bot already held'], model=r['Model'], side=r['Bet'].split()[0],
                  hour=int(r['Local'][:2]), minute=int(r['Local'][3:5]), won=won, date=r['Event date']))
S = [r for r in R if r['won'] is not None]
print(f"rows {len(R)}, settled {len(S)}, open {len(R)-len(S)}; sides {set(r['side'] for r in R)}; models {dict((m, sum(1 for r in R if r['model']==m)) for m in set(r['model'] for r in R))}")
def line(lab, xs):
    if not xs: print(f"  {lab:34s} n=  0"); return
    n = len(xs); w = sum(r['won'] for r in xs); p = sum(r['price'] for r in xs)/n
    pnl = [(1 if r['won'] else 0) - r['price'] for r in xs]; m = sum(pnl)/n
    sd = math.sqrt(sum((x-m)**2 for x in pnl)/(n-1)) if n > 1 else float('nan')
    print(f"  {lab:34s} n={n:3d}  won {w:3d} ({100*w/n:4.0f}%)  paid {100*p:4.1f}¢  edge {100*m:+5.1f}¢ ±{100*sd/math.sqrt(n):4.1f}  $ {5*sum(pnl):+6.2f}")
print("ALL"); line("all settled", S)
print("BY PRICE"); 
for lo, hi in ((0, .60), (.60, .70), (.70, .80), (.80, 1)): line(f"price {int(lo*100)}-{int(hi*100)}¢", [r for r in S if lo <= r['price'] < hi])
print("BY LOCAL HOUR")
for lo, hi in ((0, 1), (1, 6), (6, 10), (10, 24)): line(f"local {lo:02d}:00-{hi:02d}:00", [r for r in S if lo <= r['hour'] < hi])
print("BY CONFIDENCE")
for lo, hi in ((91, 93), (93, 95), (95, 98)): line(f"conf {lo}-{hi-1}", [r for r in S if lo <= r['conf'] < hi])
print("BY HELD"); line("regular bot already held", [r for r in S if r['held']]); line("not held", [r for r in S if not r['held']])
print("BY EVENT DATE")
for d in sorted(set(r['date'] for r in S)): line(d, [r for r in S if r['date'] == d])
print("COMBOS")
line("price >= 70", [r for r in S if r['price'] >= .70])
line("price < 70", [r for r in S if r['price'] < .70])
line("price 70-88 & local >= 10 (intraday rule)", [r for r in S if .70 <= r['price'] <= .88 and r['hour'] >= 10])
line("price >= 70 & local < 10", [r for r in S if r['price'] >= .70 and r['hour'] < 10])
line("price < 70 & local < 1", [r for r in S if r['price'] < .70 and r['hour'] < 1])
line("price >= 70 & local < 1", [r for r in S if r['price'] >= .70 and r['hour'] < 1])
print("PRICE >= 70 BY EVENT DATE")
for d in sorted(set(r['date'] for r in S)): line(d, [r for r in S if r['date'] == d and r['price'] >= .70])
print("PRICE < 70 BY EVENT DATE")
for d in sorted(set(r['date'] for r in S)): line(d, [r for r in S if r['date'] == d and r['price'] < .70])
print("hours distribution (all rows):", sorted(defaultdict(int, {h: sum(1 for r in R if r['hour']==h) for h in range(24)}).items()))
