import json, urllib.request
H={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36","Accept":"application/json"}
def get(u):
    try:
        r=urllib.request.urlopen(urllib.request.Request(u,headers=H),timeout=30); b=r.read(); return r.status,b
    except Exception as e: return str(e)[:150],b""
base="https://offer.cdn.begmedia.com/api/pub/v4/"
for q in ["sports?application=2&countrycode=fr&language=fr&sitecode=frfr",
          "events?application=2&countrycode=fr&language=fr&sitecode=frfr&limit=50&offset=0&sortBy=ByLiveRankingPreliveDate&sportIds=14",
          "events?application=2&countrycode=fr&language=fr&sitecode=frfr&limit=50&offset=0&sortBy=ByLiveRankingPreliveDate&sportIds=8"]:
    st,b=get(base+q); print("==",q[:60],st,len(b))
    try:
        d=json.loads(b)
        if isinstance(d,list):
            for x in d[:40]:
                nm=x.get("name"); cid=x.get("id"); comp=(x.get("competition") or {}).get("name")
                odds=[(s.get("name"),s.get("odds")) for m in (x.get("markets") or [])[:1] for s in m.get("selections",[])[:3]]
                print("  ",cid,nm,comp,odds)
        else: print(str(d)[:400])
    except Exception as e: print("  non JSON:",b[:200])
st,b=get("https://www.betclic.fr/football-americain-s14"); print("== page betclic.fr",st,len(b), b[:150])
