import pickle,math,sys,collections
sys.path.insert(0,'/home/claude/absinthe-edge')
L=pickle.load(open('/home/claude/logs_NBA.pkl','rb'))
KEYS=["points","rebounds","assists","threes","steals","blocks","pra","min"]
def mean(x): x=[v for v in x if isinstance(v,(int,float))]; return sum(x)/len(x) if x else None
def vals(gs,k): return [g['s'][k] for g in gs if isinstance(g['s'].get(k),(int,float))]
def cur_proj(hist,is_home,k,opp,W=dict(season=.40,l5=.30,loc=.15,vs=.15)):
    season=hist[0]['season']; cur=[g for g in hist if g['season']==season]; prev=[g for g in hist if g['season']==season-1]
    vc,vp=vals(cur,k),vals(prev,k)
    if not vc and not vp: return None
    s=mean(vc) if (len(vc)>=4 or not vp) else (sum(vc)+mean(vp)*(4-len(vc)))/4
    loc=[g for g in hist if g['season']>=season-1 and g['home']==is_home][:16]
    vs=[g for g in hist if g['opp']==opp][:10]
    parts={'season':s,'l5':mean(vals(hist[:5],k)),'loc':mean(vals(loc,k)),'vs':mean(vals(vs,k))}
    w=dict(W); w['vs']=W['vs']*min(len(vals(vs,k)),4)/4
    tot=sum(w[x] for x in w if parts[x] is not None)
    return sum(parts[x]*w[x] for x in w if parts[x] is not None)/tot
def ew(hist,k,hl,n=30):
    v=vals(hist[:n],k); 
    if not v: return None
    ws=[0.5**(i/hl) for i in range(len(v))]; return sum(a*b for a,b in zip(v,ws))/sum(ws)
def run(pred_fn):
    res=collections.defaultdict(list)
    for aid,(pos,team,gl) in L.items():
        if not gl: continue
        top=gl[0]['season']
        for j,g in enumerate(gl):
            if g['season']!=top or g.get('post'): continue
            hist=gl[j+1:]
            if len(hist)<5: continue
            for k in KEYS:
                a=g['s'].get(k)
                if not isinstance(a,(int,float)): continue
                p=pred_fn(hist,g['home'],k,g['opp'])
                if p is None: continue
                res[k].append((p,a,hist))
    return res
def report(name,res):
    out=[]
    for k in KEYS:
        r=res[k]; n=len(r)
        bias=sum(p-a for p,a,_ in r)/n; mae=sum(abs(p-a) for p,a,_ in r)/n
        out.append((k,n,bias,mae))
    print(name); 
    for k,n,b,m in out: print(f"  {k:9s} n={n} bias {b:+.3f} MAE {m:.3f}")
    return {k:(b,m) for k,n,b,m in out}
if __name__=='__main__':
    base=report("CURRENT BLEND",run(lambda h,home,k,o:cur_proj(h,home,k,o)))
