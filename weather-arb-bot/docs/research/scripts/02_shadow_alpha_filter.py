"""Does alpha's >=91% NO confidence add anything beyond the price? Buckets whose
NO cost 70-85c on the city's event day (first such hour), split by alpha's
confidence at that hour. Input: the shadow study CSV (Stats -> Shadow study ZIP).
Usage: python -I 02_shadow_alpha_filter.py shadow.csv
"""
import sys, numpy as np, pandas as pd
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
d = pd.read_csv(sys.argv[1], usecols=['taken_at','city','event_date','market_id','outcome_id','bucket','local_hour','model_p','bid','ask','price_live','won'])
d = d[d.won.notna() & d.bid.notna() & d.ask.notna() & (d.price_live == 1)].copy()
d['t'] = pd.to_datetime(d.taken_at, utc=True)
# local date of each snapshot, per city
d['local_date'] = ''
for c, g in d.groupby('city'):
    d.loc[g.index, 'local_date'] = g.t.dt.tz_convert(ZoneInfo(TZ[c])).dt.date.astype(str)
d = d[d.local_date == d.event_date]                       # the event day, in the city's own calendar
d['no_price'] = (1 - d.bid).round(4)                      # what buying NO costs: 1 - YES bid
d['spread'] = d.ask - d.bid
d['alpha_no'] = 1 - d.model_p                             # alpha's NO confidence
d['no_won'] = (d.won == 0).astype(int)
d = d[d.spread <= 0.10].sort_values('t')
print(f"event-day live snapshots: {len(d):,}; markets {d.market_id.nunique()}, buckets {d.outcome_id.nunique()}, dates {d.event_date.min()}..{d.event_date.max()}")

def first_entry(mask):
    """One entry per bucket: the first hour the condition holds."""
    return d[mask].drop_duplicates('outcome_id', keep='first')
def stat(lab, e):
    if len(e) == 0: print(f"  {lab:56s} n=0"); return
    pnl = e.no_won - e.no_price
    g = pd.DataFrame({'p': pnl.values, 'm': e.market_id.values}).groupby('m').p.agg(['sum','count']); m = pnl.mean(); G = len(g)
    se = np.sqrt(G/(G-1) * ((g['sum'] - g['count']*m)**2).sum()) / len(pnl) if G > 1 else np.nan
    print(f"  {lab:56s} n={len(e):5d} mkts={G:4d}  won {100*e.no_won.mean():5.1f}%  paid {100*e.no_price.mean():5.1f}¢  edge {100*m:+5.2f}¢  t={m/se:+5.2f}")

band = d.no_price.between(0.70, 0.85)
A = first_entry(band)
print("\n1) Every bucket whose NO cost 70-85c on the event day (first such hour):")
stat("A  all (no alpha)", A)
acc = A[A.alpha_no >= 0.905]; rej = A[A.alpha_no < 0.905]
stat("   of these, alpha said NO >= 91% at that hour", acc)
stat("   of these, alpha did NOT (rejected)", rej)
print("\n2) Strategy 'alpha >= 91% AND NO at 70-85c' (first hour both hold):")
B = first_entry(band & (d.alpha_no >= 0.905)); stat("B  alpha-filtered", B)
B15 = first_entry(band & (d.alpha_no >= 0.905) & (d.alpha_no - d.no_price >= 0.15)); stat("B' + alpha edge >= 15c (the bot's own alert gate)", B15)
print("\n3) Alpha's confidence at the entry hour, within A:")
for lo, hi in ((0, .5), (.5, .8), (.8, .905), (.905, .95), (.95, 1.01)):
    stat(f"   alpha NO {int(lo*100)}-{int(hi*100)}%", A[(A.alpha_no >= lo) & (A.alpha_no < hi)])
print("\n4) By price band (A, accepted vs rejected):")
for lo, hi in ((.70, .75), (.75, .80), (.80, .85)):
    x = A[A.no_price.between(lo, hi - 1e-9)]
    stat(f"   {int(lo*100)}-{int(hi*100)}c all", x); stat(f"   {int(lo*100)}-{int(hi*100)}c alpha>=91", x[x.alpha_no >= .905]); stat(f"   {int(lo*100)}-{int(hi*100)}c alpha<91", x[x.alpha_no < .905])
print("\n5) Time split (event dates):")
mid = '2026-11-14'
for lab, x in (("1-13 Nov", A[A.event_date < mid]), ("14-27 Nov", A[A.event_date >= mid])):
    stat(f"   {lab} all", x); stat(f"   {lab} alpha>=91", x[x.alpha_no >= .905]); stat(f"   {lab} alpha<91", x[x.alpha_no < .905])
print("\n6) By local hour of entry (A):")
for lo, hi in ((0, 6), (6, 10), (10, 14), (14, 24)):
    x = A[A.local_hour.between(lo, hi - 1)]
    stat(f"   {lo:02d}-{hi:02d}h all", x); stat(f"   {lo:02d}-{hi:02d}h alpha>=91", x[x.alpha_no >= .905])
