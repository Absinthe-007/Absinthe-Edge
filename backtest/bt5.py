import math,sys,json
sys.argv=['x']
from bt import *
import model as M
def phi(x): return 0.5*(1+math.erf(x/math.sqrt(2)))
res=run(lambda h,home,k,o:cur_proj(h,home,k,o))
def sd_of(k,p,h):
    sample=vals(h[:12],k); sd_base=max(M.CV.get(k,.5)*max(p,1),1.0 if k in M.COUNT else 2.5)
    if len(sample)>=5:
        m_=mean(sample); s=math.sqrt(sum((x-m_)**2 for x in sample)/(len(sample)-1)); return math.sqrt(.5*s*s+.5*sd_base**2)
    return sd_base*1.15
def ll(P,Y): return -sum(math.log(min(max(p if y else 1-p,1e-4),1)) for p,y in zip(P,Y))/len(Y)
fit={}
for k in ("points","rebounds","assists","pra","min"):
    rows=res[k];curve=[]
    for sm in [x/100 for x in range(80,201,10)]:
        best=None
        for sh in [x/100 for x in range(-20,11,2)]:
            P=[];Y=[]
            for p,a,h in rows:
                sd=sd_of(k,p,h)
                for off in (-.6,-.3,0,.3,.6):
                    line=math.floor(p+off*sd)+.5; P.append(phi((p+sh*sd-line)/(sd*sm)));Y.append(a>line)
            l=ll(P,Y)
            if best is None or l<best[0]: best=(l,sh)
        curve.append((best[0],sm,best[1]))
    b=min(curve); cur=[c for c in curve if abs(c[1]-1.0)<1e-9][0]
    print(f" {k:9s} sd=x1.0 ll {cur[0]:.4f} | best x{b[1]:.2f} shift {b[2]:+.2f} ll {b[0]:.4f} | ll at 1.2: {[c[0] for c in curve if abs(c[1]-1.2)<1e-9][0]:.4f}")
    fit[k]=(b[1],b[2])
json.dump(fit,open('/home/claude/fit.json','w'))
