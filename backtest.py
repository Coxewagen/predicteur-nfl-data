#!/usr/bin/env python3
"""Reconstitue un historique de pronos pour les semaines DÉJÀ JOUÉES cette saison, pour lesquelles
le suivi en direct (update.py) n'a jamais rien enregistré (il ne suit que la semaine en cours au
moment où il tourne, voir son commentaire "on ne suit que la semaine en cours").

Point clé, pour que ce soit un vrai test et pas un historique gonflé après coup : pour calculer le
prono de la journée W, on ne donne au modèle QUE les statistiques des équipes telles qu'elles
étaient AVANT que la journée W commence (uniquement les matchs des journées 1 à W-1). Jamais les
statistiques d'aujourd'hui. Donc :
  - journée 1 : aucune statistique disponible avant elle -> pas de prono (comme en direct).
  - journée 2 : calculée avec les stats de la seule journée 1.
  - journée 3 : calculée avec les stats des journées 1 et 2.
  - etc., jusqu'à la dernière journée déjà jouée (la semaine en cours, pas encore terminée, n'est
    pas concernée : elle est déjà suivie en direct par update.py).

Ne modifie ni model.js ni update.py : réutilise juste le même moteur de calcul (model.js) et les
mêmes définitions (équipes, dates, etc.) importées depuis update.py.

N'écrase JAMAIS une entrée déjà présente dans le journal (state.json) : si update.py a déjà suivi
un match en direct, ce calcul-là reste la référence, celui-ci ne s'y substitue pas.

Les absences (blessures) ne sont PAS appliquées dans ce calcul rétroactif : on n'a pas d'historique
de qui était blessé semaine par semaine (même limite déjà notée dans update.py pour les chips
affichées sur les matchs déjà joués), donc les y ajouter aujourd'hui serait injuste dans un sens ou
dans l'autre. Le prono rétroactif reflète donc la forme des équipes (résultats, EPA passe/course,
série en cours), pas les blessures de l'époque.

Fichiers lus : state.json (journal existant, jamais écrasé), model.js, calendrier/stats nflreadpy.
Fichier écrit : state.json (journal complété avec les semaines manquantes).
À lancer une fois pour combler l'historique (ou de nouveau plus tard si de nouvelles semaines
passées manquent encore) : python3 backtest.py
"""
import json, subprocess
from update import NAMES, CONF, kickoff_utc, lambda_reg, SEASON, HERE
import nflreadpy as nfl


def team_stats_before(games_before, ts_before):
    """Mêmes calculs que l'étape 2 de update.py, mais limités aux matchs donnés (déjà joués AVANT
    la journée qu'on s'apprête à calculer — jamais les matchs de cette journée ou d'après)."""
    T = {n: dict(name=n, conf='AFC' if a in CONF['AFC'] else 'NFC', played=0, wins=0, losses=0, ties=0,
                 pointsFor=0, pointsAgainst=0) for a, n in NAMES.items() if a != 'LAR'}
    for g in games_before:
        for me, opp, pf, pa in ((g['home'], g['away'], g['homeScore'], g['awayScore']),
                                (g['away'], g['home'], g['awayScore'], g['homeScore'])):
            t = T[me]; t['played'] += 1; t['pointsFor'] += pf; t['pointsAgainst'] += pa
            t['wins' if pf > pa else 'losses' if pf < pa else 'ties'] += 1
    by_game = {(r['game_id'], r['team']): r for r in ts_before}
    agg = {}
    for r in ts_before:
        o = by_game.get((r['game_id'], r['opponent_team']))
        if o is None:
            continue
        a = agg.setdefault(NAMES[r['team']], dict(n=0, op=0, orr=0, dp=0, dr=0, car=0))
        a['n'] += 1; a['op'] += r['passing_epa'] or 0; a['orr'] += r['rushing_epa'] or 0
        a['dp'] += o['passing_epa'] or 0; a['dr'] += o['rushing_epa'] or 0
        tot = (r['carries'] or 0) + (r['attempts'] or 0)
        a['car'] += (r['carries'] or 0)/tot if tot else 0
    for n, a in agg.items():
        if a['n']:
            T[n].update(offPassEPA=round(a['op']/a['n'], 2), offRushEPA=round(a['orr']/a['n'], 2),
                        defPassEPA=round(a['dp']/a['n'], 2), defRushEPA=round(a['dr']/a['n'], 2),
                        rushRate=round(a['car']/a['n'], 2))
    return T


def main():
    state = json.loads((HERE/'state.json').read_text(encoding='utf-8'))
    journal = state.setdefault('journal', {})

    sched = nfl.load_schedules(SEASON).filter(lambda_reg()).to_dicts()
    games = []
    for g in sched:
        ko = kickoff_utc(g['gameday'], g['gametime'])
        closed = g['home_score'] is not None and g['away_score'] is not None
        games.append(dict(id=g['game_id'], week=g['week'], home=NAMES[g['home_team']], away=NAMES[g['away_team']],
                           homeScore=g['home_score'], awayScore=g['away_score'],
                           status='closed' if closed else 'scheduled', kickoff=ko.strftime('%Y-%m-%dT%H:%M:%SZ'),
                           **({'neutral': True} if g.get('location') == 'Neutral' else {})))
    games.sort(key=lambda r: r['kickoff'])
    weeks_dict = {}
    for g in games:
        weeks_dict.setdefault(g['week'], []).append(g)

    ts_all = [r for r in nfl.load_team_stats(SEASON, summary_level='week').to_dicts() if r['season_type'] == 'REG']
    week_of_game = {g['id']: g['week'] for g in games}
    for r in ts_all:
        r['_week'] = week_of_game.get(r['game_id'])

    played_weeks = sorted({g['week'] for g in games if g['status'] == 'closed'})
    n_new, n_skipped_deja_suivi = 0, 0

    for w in played_weeks:
        games_w = [g for g in games if g['week'] == w and g['status'] == 'closed']
        a_calculer = [g for g in games_w if g['id'] not in journal]
        n_skipped_deja_suivi += len(games_w) - len(a_calculer)
        if not a_calculer:
            continue

        games_before = [g for g in games if g['status'] == 'closed' and g['week'] < w]
        ts_before = [r for r in ts_all if r['_week'] is not None and r['_week'] < w]
        T = team_stats_before(games_before, ts_before)
        weeks_before = {str(wk): gs for wk, gs in weeks_dict.items() if wk < w}
        # la journée en cours est ajoutée SANS scores (statut 'scheduled') : le moteur y repère les matchs sur terrain neutre
        # sans rien apprendre du résultat (la série en cours ne compte que les matchs 'closed').
        weeks_before[str(w)] = [dict(g, homeScore=None, awayScore=None, status='scheduled') for g in weeks_dict[w]]

        todo = [g for g in a_calculer if T[g['home']]['played'] and T[g['away']]['played']]
        if not todo:
            continue  # pas assez de matchs joués avant cette journée pour calculer quoi que ce soit (ex. journée 1)

        teams = sorted(T.values(), key=lambda t: t['name'])
        (HERE/'_bt_in.json').write_text(json.dumps(dict(teams=teams, weeks=weeks_before, games=todo)), encoding='utf-8')
        subprocess.run(['node', str(HERE/'model.js'), str(HERE/'_bt_in.json'), str(HERE/'_bt_out.json')], check=True)
        preds = json.loads((HERE/'_bt_out.json').read_text(encoding='utf-8'))

        for g in todo:
            p = preds.get(g['id'])
            if not p:
                continue
            j = dict(week=g['week'], home=g['home'], away=g['away'], kickoff=g['kickoff'],
                     pHome=round(p['pHome'], 4), pAway=round(p['pAway'], 4),
                     expHome=p['expHome'], expAway=p['expAway'],
                     computedAt=g['kickoff'], frozen=True, backtest=True,
                     homeScore=g['homeScore'], awayScore=g['awayScore'])
            j['correct'] = None if g['homeScore'] == g['awayScore'] else (j['pHome'] > j['pAway']) == (g['homeScore'] > g['awayScore'])
            journal[g['id']] = j
            n_new += 1

    for f in ('_bt_in.json', '_bt_out.json'):
        (HERE/f).unlink(missing_ok=True)
    (HERE/'state.json').write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f"{n_new} prévisions historiques ajoutées au journal (semaines déjà jouées, jamais suivies en direct).")
    print(f"{n_skipped_deja_suivi} match(s) déjà présents dans le journal (suivi en direct) : non recalculés.")


if __name__ == '__main__':
    main()
