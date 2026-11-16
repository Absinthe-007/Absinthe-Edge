import json,math,collections
G=sorted(json.load(open('/home/claude/nba_games2.json')),key=lambda g:g['t'])
def phi(x): return 0.5*(1+math.erf(x/math.sqrt(2)))
def run(K=10,HA=2.5,B2B=1.5):
    pf=collections.defaultdict(float);pa=collections.defaultdict(float);gp=collections.Counter();last={}
    allpts=0;alln=0;rows=[]
    for g in G:
        h,a=g['home'],g['away']
        lg=allpts/alln if alln else 114
        def reg(t,key): 
            n=gp[t]; v=(pf[t] if key=='pf' else pa[t])/n if n else None
            return lg if v is None else (v*n+lg*K)/(n+K)
        he=(reg(h,'pf')+reg(a,'pa'))/2+HA/2; ae=(reg(a,'pf')+reg(h,'pa'))/2-HA/2
        from datetime import datetime
        d=datetime.fromisoformat(g['t'].replace('Z','+00:00'))
        for t,side in((h,'h'),(a,'a')):
            if t in last and (d-last[t]).total_seconds()<30*3600:
                if side=='h': he-=B2B
                else: ae-=B2B
        if gp[h]>=6 and gp[a]>=6:
            rows.append(dict(mm=he-ae,mt=he+ae,vm=g['vm'],vt=g['vou'],am=g['hs']-g['as_'],at=g['hs']+g['as_']))
        for t,f,x in((h,g['hs'],g['as_']),(a,g['as_'],g['hs'])):
            pf[t]+=f;pa[t]+=x;gp[t]+=1;last[t]=d
        allpts+=g['hs']+g['as_'];alln+=2
    return rows
if __name__=='__main__':
    rows=run(); n=len(rows); print("games evaluated",n)
    mae=lambda f,key:sum(abs(f(r)-r[key]) for r in rows)/n
    print("MARGIN MAE: model %.2f vegas %.2f"%(mae(lambda r:r['mm'],'am'),mae(lambda r:r['vm'],'am')))
    print("TOTAL  MAE: model %.2f vegas %.2f"%(mae(lambda r:r['mt'],'at'),mae(lambda r:r['vt'],'at')))
    print("margin bias model %.2f vegas %.2f | total bias model %.2f vegas %.2f"%(sum(r['mm']-r['am'] for r in rows)/n,sum(r['vm']-r['am'] for r in rows)/n,sum(r['mt']-r['at'] for r in rows)/n,sum(r['vt']-r['at'] for r in rows)/n))
    print("w(model)  margin MAE  total MAE   winner acc")
    for w in (0,.1,.2,.3,.4,.5,.7,1):
        mm=sum(abs(w*r['mm']+(1-w)*r['vm']-r['am']) for r in rows)/n
        tt=sum(abs(w*r['mt']+(1-w)*r['vt']-r['at']) for r in rows)/n
        acc=sum((w*r['mm']+(1-w)*r['vm']>0)==(r['am']>0) for r in rows)/n
        print(f"  {w:.1f}      {mm:.3f}     {tt:.3f}     {acc:.3f}")
    # error sd of vegas, for win prob scale
    for nm,f in(("vegas",lambda r:r['vm']),("blend .2",lambda r:.2*r['mm']+.8*r['vm'])):
        e=[r['am']-f(r) for r in rows]; sd=math.sqrt(sum(x*x for x in e)/n); print(nm,"margin error sd %.2f"%sd)
    e=[r['at']-r['vt'] for r in rows]; print("total error sd vs vegas %.2f"%math.sqrt(sum(x*x for x in e)/n))
    e=[r['at']-(.5*r['mt']+.5*r['vt']) for r in rows]; print("total error sd current blend %.2f"%math.sqrt(sum(x*x for x in e)/n))
    # logloss of win prob under SD choices using current 50/50 blend vs 20/80
    for w in (.5,.2):
        for sd in (10.5,11.5,12.5,13.5):
            ll=0
            for r in rows:
                m=w*r['mm']+(1-w)*r['vm']; p=min(max(phi(m/sd),1e-3),1-1e-3); y=r['am']>0; ll-=math.log(p if y else 1-p)
            print(f" w={w} sd={sd}: logloss {ll/n:.4f}")
