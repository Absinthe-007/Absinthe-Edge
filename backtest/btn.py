import json,math,collections
G=sorted(json.load(open('/home/claude/nfl_games.json')),key=lambda g:g['t'])
G=[g for g in G if g['vm'] is not None and g['vou']]
def srs(prev,K,HA,iters=15):
    gp=collections.Counter()
    for g in prev: gp[g['home']]+=1;gp[g['away']]+=1
    R=collections.defaultdict(float)
    for _ in range(iters):
        acc=collections.defaultdict(float)
        for g in prev:
            m=g['hs']-g['as_']-(0 if g['neutral'] else HA)
            acc[g['home']]+=m+R[g['away']];acc[g['away']]+=-m+R[g['home']]
        R=collections.defaultdict(float,{t:acc[t]/(gp[t]+K) for t in gp})
    return R,gp
for K in (3,6,10):
    rows=[]
    for i,g in enumerate(G):
        prev=G[:i]
        if g['wk']<4: continue
        R,gp=srs(prev,K,1.5)
        lgt=sum(p['hs']+p['as_'] for p in prev)/len(prev)
        def tt(t):
            gs=[p['hs']+p['as_'] for p in prev if t in(p['home'],p['away'])]; n=len(gs); return ((sum(gs)/n)*n+lgt*4)/(n+4)-lgt if n else 0
        rows.append(dict(mm=R[g['home']]-R[g['away']]+(0 if g['neutral'] else 1.5),mt=lgt+tt(g['home'])+tt(g['away']),vm=g['vm'],vt=g['vou'],am=g['hs']-g['as_'],at=g['hs']+g['as_']))
    n=len(rows)
    print(f"K={K} n={n} model margin MAE {sum(abs(r['mm']-r['am']) for r in rows)/n:.2f} total {sum(abs(r['mt']-r['at']) for r in rows)/n:.2f} | vegas margin {sum(abs(r['vm']-r['am']) for r in rows)/n:.2f} total {sum(abs(r['vt']-r['at']) for r in rows)/n:.2f}")
    if K==6:
        for w in (0,.1,.2,.3,.5):
            print(f"  w={w}: margin MAE {sum(abs(w*r['mm']+(1-w)*r['vm']-r['am']) for r in rows)/n:.3f}  total MAE {sum(abs(w*r['mt']+(1-w)*r['vt']-r['at']) for r in rows)/n:.3f}  acc {sum((w*r['mm']+(1-w)*r['vm']>0)==(r['am']>0) for r in rows)/n:.3f}")
        e=[r['am']-r['vm'] for r in rows]; print("  vegas margin err sd %.2f"%math.sqrt(sum(x*x for x in e)/n), " home margin avg %.2f"%(sum(r['am'] for r in rows)/n))
        e=[r['at']-r['vt'] for r in rows]; print("  vegas total err sd %.2f bias %.2f"%(math.sqrt(sum(x*x for x in e)/n),sum(e)/n))
