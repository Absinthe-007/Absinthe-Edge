import json,math,collections
from datetime import datetime
G=sorted(json.load(open('/home/claude/nba_games2.json')),key=lambda g:g['t'])
def srs(games,K,iters=30):
    """opponent-adjusted net rating, shrunk toward 0 with K pseudo-games"""
    gp=collections.Counter();R=collections.defaultdict(float)
    for g in games: gp[g['home']]+=1;gp[g['away']]+=1
    for _ in range(iters):
        acc=collections.defaultdict(float)
        for g in games:
            m=g['hs']-g['as_']-HAS[0]
            acc[g['home']]+=m+R[g['away']]; acc[g['away']]+=-m+R[g['home']]
        R={t:acc[t]/(gp[t]+K) for t in gp}
        R=collections.defaultdict(float,R)
    return R
HAS=[2.5]
def evaluate(K,HA,decay=None,ha_est=2.5):
    HAS[0]=HA; rows=[]
    for i,g in enumerate(G):
        prev=G[:i]
        if len(prev)<60: continue
        R=srs(prev,K,12)
        gp=collections.Counter(); 
        for p in prev: gp[p['home']]+=1;gp[p['away']]+=1
        if gp[g['home']]<6 or gp[g['away']]<6: continue
        mm=R[g['home']]-R[g['away']]+HA
        # total: league avg + both teams' scoring tendency (regressed)
        tot=[p['hs']+p['as_'] for p in prev]; lgt=sum(tot)/len(tot)
        def tt(t):
            gs=[p['hs']+p['as_'] for p in prev if t in (p['home'],p['away'])][-20:]
            n=len(gs); return ((sum(gs)/n)*n+lgt*10)/(n+10)-lgt if n else 0
        mt=lgt+tt(g['home'])+tt(g['away'])
        rows.append(dict(mm=mm,mt=mt,vm=g['vm'],vt=g['vou'],am=g['hs']-g['as_'],at=g['hs']+g['as_']))
    return rows
n0=None
for K in (4,8,12,20):
    for HA in (2.5,):
        rows=evaluate(K,HA); n=len(rows)
        print(f"K={K} HA={HA} n={n} margin MAE {sum(abs(r['mm']-r['am']) for r in rows)/n:.2f}  acc {sum((r['mm']>0)==(r['am']>0) for r in rows)/n:.3f}  total MAE {sum(abs(r['mt']-r['at']) for r in rows)/n:.2f}  | vegas margin {sum(abs(r['vm']-r['am']) for r in rows)/n:.2f} total {sum(abs(r['vt']-r['at']) for r in rows)/n:.2f}")
rows=evaluate(8,2.5);n=len(rows)
for w in (0,.1,.2,.3,.5):
    print(f" blend w={w}: margin MAE {sum(abs(w*r['mm']+(1-w)*r['vm']-r['am']) for r in rows)/n:.3f} total MAE {sum(abs(w*r['mt']+(1-w)*r['vt']-r['at']) for r in rows)/n:.3f}")
