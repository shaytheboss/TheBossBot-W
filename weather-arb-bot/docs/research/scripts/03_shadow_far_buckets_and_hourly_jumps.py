"""(1) Buckets 0/1/2/3+ levels from the bot's forecast: does NO win more than its
price? (2) The hour after YES jumped/dropped 10-20c: fade or follow?
Input: the shadow study CSV. Usage: python -I 03_... shadow.csv
"""
import sys, re, math, numpy as np, pandas as pd
from zoneinfo import ZoneInfo
TZ = {'Amsterdam':'Europe/Amsterdam','Ankara':'Europe/Istanbul','Atlanta':'America/New_York','Austin':'America/Chicago',
 'Beijing':'Asia/Shanghai','Buenos Aires':'America/Argentina/Buenos_Aires','Busan':'Asia/Seoul','Cape Town':'Africa/Johannesburg',
 'Chengdu':'Asia/Shanghai','Chicago':'America/Chicago','Chongqing':'Asia/Shanghai','Dallas':'America/Chicago','Denver':'America/Denver',
 'Guangzhou':'Asia/Shanghai','Helsinki':'Europe/Helsinki','Hong Kong':'Asia/Hong_Kong','Houston':'America/Chicago','Istanbul':'Europe/Istanbul',
 'Jeddah':'Asia/Riyadh','Karachi':'Asia/Karachi','Kuala Lumpur':'Asia/Kuala_Lumpur','London':'Europe/London','Los Angeles':'America/Los_Angeles',
 'Lucknow':'Asia/Kolkata','Madrid':'Europe/Madrid','Manila':'Asia/Manila','Mexico City':'America/Mexico_City','Miami':'America/New_York',
 'Milan':'Europe/Rome','Munich':'Europe/Berlin','New York':'America/New_York','Panama City':'America/Panama','Paris':'Europe/Paris',
 'Qingdao':'Asia/Shanghai','San Francisco':'America/Los_Angeles','Seattle':'America/Los_Angeles','Seoul':'Asia/Seoul','Shanghai':'Asia/Shanghai',
 'Shenzhen':'Asia/Shanghai','Singapore':'Asia/Singapore','São Paulo':'America/Sao_Paulo','Taipei':'Asia/Taipei','Tel Aviv':'Asia/Jerusalem',
 'Tokyo':'Asia/Tokyo','Toronto':'America/Toronto','Warsaw':'Europe/Warsaw','Wellington':'Pacific/Auckland','Wuhan':'Asia/Shanghai'}
d = pd.read_csv(sys.argv[1], usecols=['taken_at','city','event_date','market_id','outcome_id','bucket','local_hour',
                                      'model_p','market_p','bid','ask','price_live','forecast_high_f','won'])
d = d[d.won.notna()].copy()
d['t'] = pd.to_datetime(d.taken_at, utc=True)
d['local_date'] = ''
for c, g in d.groupby('city'):
    d.loc[g.index, 'local_date'] = g.t.dt.tz_convert(ZoneInfo(TZ[c])).dt.date.astype(str)
def parse(lab):
    unit = 'C' if '°C' in lab else 'F'
    nums = [int(x) for x in re.findall(r'-?\d+', lab.replace('°', ' '))]
    if 'or higher' in lab or 'or above' in lab: return unit, nums[0], None
    if 'or below' in lab or 'or lower' in lab: return unit, None, nums[0]
    if len(nums) == 2 and unit == 'F': return unit, nums[0], nums[1]
    return unit, nums[0], nums[0]
P = {b: parse(b) for b in d.bucket.unique()}
def levels(row):
    unit, lo, hi = P[row.bucket]
    v = row.forecast_high_f if unit == 'F' else (row.forecast_high_f - 32) * 5 / 9
    r = round(v); w = 2 if unit == 'F' else 1
    if lo is not None and r < lo: return math.ceil((lo - r) / w)
    if hi is not None and r > hi: return math.ceil((r - hi) / w)
    return 0
d['lev'] = [levels(r) for r in d.itertuples()]
d['no_price'] = 1 - d.bid
d['no_won'] = (d.won == 0).astype(int)
d['day'] = np.where(d.local_date == d.event_date, 'event', np.where(d.local_date < d.event_date, 'before', 'after'))
d = d.sort_values('t')
live = d[(d.price_live == 1) & d.bid.notna() & d.ask.notna() & ((d.ask - d.bid) <= 0.10)]

def stat(lab, e):
    if len(e) < 5: print(f"  {lab:46s} n={len(e)}"); return
    pnl = e.no_won - e.no_price
    g = pd.DataFrame({'p': pnl.values, 'm': e.market_id.values}).groupby('m').p.agg(['sum','count']); m = pnl.mean(); G = len(g)
    se = np.sqrt(G/(G-1) * ((g['sum'] - g['count']*m)**2).sum()) / len(pnl) if G > 1 else np.nan
    print(f"  {lab:46s} n={len(e):5d}  NO won {100*e.no_won.mean():6.2f}%  paid {100*e.no_price.mean():5.1f}¢  edge {100*m:+5.2f}¢  t={m/se:+5.2f}")

BANDS = ((.50, .70), (.70, .85), (.85, .93), (.93, .97), (.97, .995))
def grid(title, e):
    print(f"\n{title}")
    for lo, hi in BANDS:
        x = e[e.no_price.between(lo, hi - 1e-9)]
        print(f" NO price {int(lo*100)}-{hi*100:g}¢")
        for L, lab in ((0, 'forecast IN this bucket'), (1, '1 level away'), (2, '2 levels away'), (3, '3+ levels away')):
            stat(f"   {lab}", x[(x.lev == L) if L < 3 else (x.lev >= 3)])

# ── Test 1: buckets far from the forecast ─────────────────────────────────
ev = live[live.day == 'event']
first = ev.drop_duplicates('outcome_id', keep='first')                     # start of the city's day
grid("TEST 1a — buy NO at the first hour of the event day (one per bucket)", first)
morning = ev[ev.local_hour == 8].drop_duplicates('outcome_id', keep='first')
grid("TEST 1b — buy NO at 08:00 local on the event day", morning)
b4 = live[(live.day == 'before') & (live.local_hour == 12)].drop_duplicates('outcome_id', keep='first')
grid("TEST 1c — buy NO at 12:00 local, the day before", b4)
print("\nTEST 1 — time split, NO 93-99.5¢, 2+ levels, first hour of event day:")
x = first[first.no_price.between(.93, .995) & (first.lev >= 2)]
stat("   1-13 Nov", x[x.event_date < '2026-11-14']); stat("   14-27 Nov", x[x.event_date >= '2026-11-14'])

# ── Test 2: after a sharp move ────────────────────────────────────────────
d2 = d[d.day.isin(['event', 'before'])].copy()
d2['prev_mid'] = d2.groupby('outcome_id').market_p.shift(1)
d2['prev_t'] = d2.groupby('outcome_id').t.shift(1)
d2['next_mid'] = d2.groupby('outcome_id').market_p.shift(-1)
d2 = d2[(d2.t - d2.prev_t) <= pd.Timedelta(minutes=75)]
d2['jump'] = d2.market_p - d2.prev_mid
s = d2[(d2.price_live == 1) & d2.bid.notna() & ((d2.ask - d2.bid) <= 0.10)]
print("\nTEST 2 — the hour after YES jumped up (NO got cheaper): buy NO")
for th in (0.10, 0.20):
    for lab, m in (("any bucket", s.jump >= th), ("forecast 2+ levels away", (s.jump >= th) & (s.lev >= 2))):
        e = s[m].drop_duplicates('outcome_id', keep='first')
        stat(f"   YES +{int(th*100)}¢ in 1h, {lab}", e)
        nm = (e.next_mid - e.market_p).dropna()
        print(f"     … next hour YES moved {100*nm.mean():+.1f}¢ on average (n={len(nm)})")
print("\nTEST 2b — the hour after YES dropped (NO got dearer): buy NO anyway (follow the move)")
for th in (0.10, 0.20):
    e = s[s.jump <= -th].drop_duplicates('outcome_id', keep='first')
    stat(f"   YES -{int(th*100)}¢ in 1h, any bucket", e)
