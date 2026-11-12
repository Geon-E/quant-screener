# ADDENDUM_H2 4판 2장 표의 모의실험 (합성 난수만, 실제 수익률 미사용). 실행: python reviews/round5/mc_interval_length.py
import numpy as np
# 4.1판: 구간 달력일 = 체결일 2017-04-10·05-23·08-23·11-22 → 다음 체결일, 마지막은 2018-03-30
rng=np.random.default_rng(0); d=np.array([43,92,91,128]); T=3.182446; N=200000; sd=0.006
def raw(x): m=x.mean(1); se=x.std(1,ddof=1)/2; return m/se
def gls(x):
    mu=x.sum(1)/d.sum(); r=(x-mu[:,None]*d)/np.sqrt(d); s2=(r**2).sum(1)/3; return mu/np.sqrt(s2/d.sum())
def ann(x):
    z=x/d*91.25; return z.mean(1)/(z.std(1,ddof=1)/2)
for mu in [0,0.0001,0.0002,0.0003]:
    x=mu*d+sd*np.sqrt(d)*rng.standard_normal((N,4))
    print(f"daily mu {mu*1e4:.0f}bp ({mu*365*100:.1f}%/yr):", *(f"{n} {np.mean(f(x)>T)*100:.2f}%" for n,f in [("raw",raw),("gls",gls),("per-day",ann)]))
