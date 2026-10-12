"""Liste les matchs à analyser (mercredi et samedi) : matchs serrés ou désaccord modèle / marché.

Critères (sur les matchs pas encore commencés des 7 prochains jours) :
  - « serré »     : le favori du modèle a entre 45 % et 60 % (donc les deux équipes entre 40 et 60 %) ;
  - « désaccord » : le modèle et le marché n'ont pas le même favori, ou leurs pourcentages
                    diffèrent d'au moins 10 points.
Lit app-data.json (publié par le robot) et écrit a_analyser.json + un résumé lisible à l'écran.
Usage : python matchs_serres.py            (ou --jours 7 pour changer la fenêtre)
"""
import json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERRE_MIN, SERRE_MAX = 0.45, 0.60   # pourcentage du favori du modèle
ECART_MARCHE = 0.10                 # écart modèle / marché (en probabilité) jugé important


def raisons(g):
    p = g.get('pred') or {}
    if p.get('pHome') is None:
        return []
    ph = p['pHome']
    fav = max(ph, 1 - ph)
    r = []
    if SERRE_MIN <= fav <= SERRE_MAX:
        r.append('serré')
    mk = g.get('mkPH')
    if mk is not None and ((ph >= 0.5) != (mk >= 0.5) or abs(ph - mk) >= ECART_MARCHE):
        r.append('désaccord')
    return r


def selection(data, now=None, jours=7):
    now = now or datetime.now(timezone.utc)
    fin = now + timedelta(days=jours)
    out = []
    for wk, games in sorted((data.get('weeks') or {}).items(), key=lambda kv: int(kv[0])):
        for g in games:
            if g.get('status') != 'scheduled' or not g.get('kickoff'):
                continue
            k = datetime.fromisoformat(g['kickoff'].replace('Z', '+00:00'))
            if not (now < k <= fin):
                continue
            r = raisons(g)
            if r:
                p = g['pred']
                out.append(dict(id=g['id'], semaine=g['week'], kickoff=g['kickoff'], domicile=g['home'], exterieur=g['away'],
                                raisons=r, modele_pDomicile=p['pHome'], score_attendu=f"{p['expHome']}-{p['expAway']}",
                                marche_pDomicile=g.get('mkPH'), ligne_marche=g.get('mkLine'),
                                blessures=g.get('blessures'), absences=g.get('abs')))
    return out


def main():
    jours = 7
    if '--jours' in sys.argv:
        jours = int(sys.argv[sys.argv.index('--jours') + 1])
    data = json.loads((HERE / 'app-data.json').read_text(encoding='utf-8'))
    sel = selection(data, jours=jours)
    (HERE / 'a_analyser.json').write_text(json.dumps(sel, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f"{len(sel)} match(s) à analyser (données du {data.get('updatedLabel')}) :")
    for m in sel:
        mk = '–' if m['marche_pDomicile'] is None else f"{round(m['marche_pDomicile'] * 100)} %"
        print(f"  {m['id']} : {m['domicile']} {round(m['modele_pDomicile'] * 100)} % (marché {mk}) — {', '.join(m['raisons'])}")


if __name__ == '__main__':
    main()
