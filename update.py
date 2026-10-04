#!/usr/bin/env python3
"""Mise à jour automatique du Prédicteur NFL.

Étapes :
  1. télécharge calendrier, résultats, statistiques, blessures et effectifs (nflreadpy) ;
  2. recalcule les statistiques des 32 équipes ;
  3. lance le moteur de calcul (model.js, copié de predicteur-nfl.html) ;
  4. fige chaque prévision au coup d'envoi et la compare ensuite au résultat réel (journal) ;
  5. signale les absences à vérifier (rapport) ;
  6. fabrique la page index.html.

Fichiers lus : state.json (absences retenues + journal), model.js, template.txt
Fichiers écrits : state.json, index.html, rapport.json
"""
import json, re, subprocess, sys, unicodedata
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import nflreadpy as nfl

SEASON = 2026
HERE = Path(__file__).resolve().parent
SAFE = 0.60            # seuil "favori net"
STARTER_PCT = 0.50     # part moyenne de snaps à partir de laquelle un joueur compte comme titulaire

NAMES = {
 'ARI':'Arizona Cardinals','ATL':'Atlanta Falcons','BAL':'Baltimore Ravens','BUF':'Buffalo Bills',
 'CAR':'Carolina Panthers','CHI':'Chicago Bears','CIN':'Cincinnati Bengals','CLE':'Cleveland Browns',
 'DAL':'Dallas Cowboys','DEN':'Denver Broncos','DET':'Detroit Lions','GB':'Green Bay Packers',
 'HOU':'Houston Texans','IND':'Indianapolis Colts','JAX':'Jacksonville Jaguars','KC':'Kansas City Chiefs',
 'LA':'Los Angeles Rams','LAR':'Los Angeles Rams','LAC':'Los Angeles Chargers','LV':'Las Vegas Raiders',
 'MIA':'Miami Dolphins','MIN':'Minnesota Vikings','NE':'New England Patriots','NO':'New Orleans Saints',
 'NYG':'New York Giants','NYJ':'New York Jets','PHI':'Philadelphia Eagles','PIT':'Pittsburgh Steelers',
 'SEA':'Seattle Seahawks','SF':'San Francisco 49ers','TB':'Tampa Bay Buccaneers','TEN':'Tennessee Titans',
 'WAS':'Washington Commanders'}
CONF = {'AFC':['BAL','BUF','CIN','CLE','DEN','HOU','IND','JAX','KC','LAC','LV','MIA','NE','NYJ','PIT','TEN']}
OFF_POS = {'QB','RB','FB','WR','TE','T','G','C','OL','OT','OG'}

def norm(s):
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z ]', '', re.sub(r'\b(jr|sr|ii|iii|iv)\b\.?', '', s)).strip()

def last_name(s):
    p = norm(s).split()
    return p[-1] if p else ''

def kickoff_utc(day, time):
    et = datetime.strptime(f'{day} {time}', '%Y-%m-%d %H:%M').replace(tzinfo=ZoneInfo('America/New_York'))
    return et.astimezone(timezone.utc)

def main():
    now = datetime.now(timezone.utc)
    state = json.loads((HERE/'state.json').read_text(encoding='utf-8'))
    absences, journal = state['absences'], state.setdefault('journal', {})

    # ---------- 1. calendrier et résultats ----------
    sched = nfl.load_schedules(SEASON).filter(lambda_reg()).to_dicts()
    games, weeks = [], {}
    for g in sched:
        ko = kickoff_utc(g['gameday'], g['gametime'])
        closed = g['home_score'] is not None and g['away_score'] is not None
        row = dict(id=g['game_id'], week=g['week'], home=NAMES[g['home_team']], away=NAMES[g['away_team']],
                   homeScore=g['home_score'], awayScore=g['away_score'],
                   status='closed' if closed else 'scheduled', kickoff=ko.strftime('%Y-%m-%dT%H:%M:%SZ'))
        if g.get('location') == 'Neutral':
            row['neutral'] = True   # match à l'étranger : pas d'avantage du terrain (voir model.js, isNeutralGame)
        games.append(row)
        weeks.setdefault(str(g['week']), []).append(row)
    games.sort(key=lambda r: r['kickoff'])
    for w in weeks.values():
        w.sort(key=lambda r: r['kickoff'])

    # ---------- 2. statistiques des équipes ----------
    T = {n: dict(name=n, conf='AFC' if a in CONF['AFC'] else 'NFC', played=0, wins=0, losses=0, ties=0,
                 pointsFor=0, pointsAgainst=0) for a, n in NAMES.items() if a != 'LAR'}
    for g in games:
        if g['status'] != 'closed':
            continue
        for me, opp, pf, pa in ((g['home'], g['away'], g['homeScore'], g['awayScore']),
                                (g['away'], g['home'], g['awayScore'], g['homeScore'])):
            t = T[me]; t['played'] += 1; t['pointsFor'] += pf; t['pointsAgainst'] += pa
            t['wins' if pf > pa else 'losses' if pf < pa else 'ties'] += 1
    ts = [r for r in nfl.load_team_stats(SEASON, summary_level='week').to_dicts() if r['season_type'] == 'REG']
    by_game = {(r['game_id'], r['team']): r for r in ts}
    agg = {}
    for r in ts:
        o = by_game.get((r['game_id'], r['opponent_team']))
        if o is None:
            continue
        a = agg.setdefault(NAMES[r['team']], dict(n=0, op=0, orr=0, dp=0, dr=0, car=0, att=0))
        a['n'] += 1; a['op'] += r['passing_epa'] or 0; a['orr'] += r['rushing_epa'] or 0
        a['dp'] += o['passing_epa'] or 0; a['dr'] += o['rushing_epa'] or 0
        tot = (r['carries'] or 0) + (r['attempts'] or 0)
        a['car'] += (r['carries'] or 0)/tot if tot else 0  # moyenne des taux par match, comme dans le fichier d'origine
    for n, a in agg.items():
        if a['n']:
            T[n].update(offPassEPA=round(a['op']/a['n'], 2), offRushEPA=round(a['orr']/a['n'], 2),
                        defPassEPA=round(a['dp']/a['n'], 2), defRushEPA=round(a['dr']/a['n'], 2),
                        rushRate=round(a['car']/a['n'], 2))
    # ---------- 2b. absences : calculées automatiquement (voir absences_auto.py) ----------
    # Si le calcul échoue (données indisponibles ce jour-là), on garde les absences du passage précédent.
    journal_abs = []
    try:
        import absences_auto
        absences, journal_abs = absences_auto.construire(
            SEASON, NAMES, {n: t['played'] for n, t in T.items()}, absences_auto.charger_overrides())
        state['absences'] = absences
    except Exception as e:  # noqa: BLE001
        print(f"Absences automatiques indisponibles ({e}) : absences du passage précédent conservées.")
        journal_abs = [f"calcul automatique indisponible : {e}"]
    for n, ab in absences.items():
        T[n].update({k: v for k, v in ab.items() if not (v is None or v is False or v == [])})  # (0 doit rester valide : qbGp, gp)
    teams = sorted(T.values(), key=lambda t: t['name'])

    # ---------- 3. prévisions ----------
    todo = [g for g in games if g['status'] == 'scheduled' and g['kickoff'] > now.strftime('%Y-%m-%dT%H:%M:%SZ')
            and T[g['home']]['played'] and T[g['away']]['played']]
    (HERE/'_in.json').write_text(json.dumps(dict(teams=teams, weeks=weeks, games=todo)), encoding='utf-8')
    subprocess.run(['node', str(HERE/'model.js'), str(HERE/'_in.json'), str(HERE/'_out.json')], check=True)
    preds = json.loads((HERE/'_out.json').read_text(encoding='utf-8'))

    # ---------- 4. journal : figé au coup d'envoi, noté après le match ----------
    stamp = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    cur_week = min((g['week'] for g in todo), default=max(g['week'] for g in games))
    for g in todo:
        if g['week'] != cur_week and g['id'] not in journal:
            continue  # on ne suit que la semaine en cours (les suivantes entreront à leur tour)
        p = preds.get(g['id'])
        if p:
            journal[g['id']] = dict(week=g['week'], home=g['home'], away=g['away'], kickoff=g['kickoff'],
                                    pHome=round(p['pHome'], 4), pAway=round(p['pAway'], 4),
                                    expHome=p['expHome'], expAway=p['expAway'], computedAt=stamp)
    by_id = {g['id']: g for g in games}
    for gid, j in journal.items():
        g = by_id.get(gid)
        if not g:
            continue
        j['frozen'] = g['kickoff'] <= stamp
        if g['status'] == 'closed' and j['frozen']:
            j['homeScore'], j['awayScore'] = g['homeScore'], g['awayScore']
            if g['homeScore'] == g['awayScore']:
                j['correct'] = None
            else:
                j['correct'] = (j['pHome'] > j['pAway']) == (g['homeScore'] > g['awayScore'])

    graded = [j for j in journal.values() if j.get('correct') is not None and 'homeScore' in j]
    safe = [j for j in graded if max(j['pHome'], j['pAway']) >= SAFE]
    bilan = dict(total=len(graded), correct=sum(j['correct'] for j in graded),
                 safeTotal=len(safe), safeCorrect=sum(j['correct'] for j in safe),
                 list=sorted(graded, key=lambda j: j['kickoff'], reverse=True))

    # ---------- 5. contrôle des absences ----------
    rapport = controle_absences(absences, T, cur_week)
    rapport['absencesAuto'] = journal_abs

    # ---------- 6. page ----------
    # "weeks" couvre chaque semaine jouée jusqu'à la semaine en cours (pas les semaines futures,
    # pas encore jouées) — c'est ce qui permet à l'appli de proposer "journées précédentes".
    # Les absences retenues (T) sont celles d'AUJOURD'HUI : pas d'historique semaine par semaine
    # de qui était absent à l'époque, donc les chips QB/absences sur un match déjà joué reflètent
    # l'état actuel des blessures, pas forcément celui du jour du match (limite connue, pas grave
    # pour un match déjà au score, mais à garder en tête).
    def build_week_games(week_games):
        out = []
        for g in week_games:
            j = journal.get(g['id'])
            out.append(dict(g, pred=j and {k: j[k] for k in ('pHome', 'pAway', 'expHome', 'expAway')},
                             abs={s: resume_abs(T[g[s]]) for s in ('home', 'away')}))
        return out
    weeks_available = sorted(int(w) for w in weeks if int(w) <= cur_week)
    all_weeks_games = {str(w): build_week_games(weeks[str(w)]) for w in weeks_available}
    page_games = all_weeks_games[str(cur_week)]
    lp = now.astimezone(ZoneInfo('Europe/Paris'))
    jours = ['lundi','mardi','mercredi','jeudi','vendredi','samedi','dimanche']
    mois = ['janvier','février','mars','avril','mai','juin','juillet','août','septembre','octobre','novembre','décembre']
    label = f"{jours[lp.weekday()]} {lp.day} {mois[lp.month-1]} {lp.year} à {lp.hour} h {lp.minute:02d}"
    data = dict(updatedAt=lp.strftime('%Y-%m-%dT%H:%M'), updatedLabel=label,
                season=SEASON, week=cur_week, weeksAvailable=weeks_available, weeks=all_weeks_games,
                teams=infos_equipes(), safe=SAFE, games=page_games, bilan=bilan,
                absences=[dict(team=t['name'], items=liste_abs(t)) for t in teams if liste_abs(t)],
                rapport=rapport)
    tpl = (HERE/'template.txt').read_text(encoding='utf-8')
    (HERE/'index.html').write_text(tpl.replace('__DATA__', json.dumps(data, ensure_ascii=False).replace('</', '<\\/')), encoding='utf-8')
    # Même dict "data" que celui injecté dans la page web, publié à part en JSON brut : c'est le
    # fichier que l'appli mobile télécharge (voir www/app.js, DATA_URL) pour rester à jour sans
    # nouvelle version d'appli à chaque mise à jour des stats/blessures.
    # "rapport" (contrôle des absences) en est délibérément exclu : il détaille des noms de joueurs
    # et les raisons des écarts retenus/ignorés, bien plus parlant sur la méthode que le reste —
    # il n'a rien à faire dans un fichier public. Il reste dans rapport.json, à ton seul usage.
    app_data = {k: v for k, v in data.items() if k != 'rapport'}
    (HERE/'app-data.json').write_text(json.dumps(app_data, ensure_ascii=False), encoding='utf-8')
    (HERE/'state.json').write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding='utf-8')
    (HERE/'rapport.json').write_text(json.dumps(rapport, ensure_ascii=False, indent=1), encoding='utf-8')
    for f in ('_in.json', '_out.json'):
        (HERE/f).unlink(missing_ok=True)

    print(f"Semaine {cur_week} : {len(todo)} matchs à venir calculés, {len(journal)} prévisions au journal.")
    print(f"Bilan : {bilan['correct']}/{bilan['total']} ; favoris nets : {bilan['safeCorrect']}/{bilan['safeTotal']}.")
    print(f"Absences à vérifier : {len(rapport['nouveaux'])} nouvelles possibles, {len(rapport['revenus'])} retours possibles, {len(rapport['qb'])} alertes QB.")
    for k in ('qb', 'nouveaux', 'revenus'):
        for r in rapport[k]:
            print(f"  [{k}] {r}")

def lambda_reg():
    import polars as pl
    return pl.col('game_type') == 'REG'

def infos_equipes():
    """Sigle, surnom et couleurs de chaque équipe (pour les blasons d'illustration)."""
    out = {}
    for r in nfl.load_teams().to_dicts():
        n = NAMES.get(r['team_abbr'])
        if n and n == r['team_name'] and (n not in out or r['team_abbr'] != 'LAR'):
            out[n] = dict(abbr='LAR' if r['team_abbr'] == 'LA' else r['team_abbr'], nick=r['team_nick'],
                          c1=r['team_color'] or '#445566', c2=r['team_color2'] or '#FFFFFF')
    return out

def liste_abs(t):
    out = []
    if t.get('qbOut'):
        txt = 'Quarterback titulaire absent (niveau ' + t.get('qbTier', 'titulaire') + ')'
        if t.get('qbBackupOut'): txt += ', remplaçant absent lui aussi'
        elif t.get('qbReplacementTier'): txt += ', remplaçant : ' + t['qbReplacementTier'].replace('_', ' ')
        out.append(dict(side='QB', txt=txt))
    out += [dict(side='Attaque', txt=a['pos'], tier=a.get('tier', 'titulaire')) for a in t.get('offAbsences', [])]
    out += [dict(side='Défense', txt=a['pos'], tier=a.get('tier', 'titulaire')) for a in t.get('defAbsences', [])]
    return out

def resume_abs(t):
    return dict(qb=bool(t.get('qbOut')), off=len(t.get('offAbsences', [])), deff=len(t.get('defAbsences', [])))

def controle_absences(absences, T, cur_week):
    """Compare les absences retenues aux données officielles. Ne modifie rien : produit une liste à examiner."""
    inj = nfl.load_injuries(SEASON).to_dicts()
    wk = max((r['week'] for r in inj), default=None)
    out_now = {}   # équipe -> {nom normalisé: (nom, poste, motif)}
    for r in inj:
        if r['week'] == wk and r['report_status'] in ('Out', 'Doubtful'):
            out_now.setdefault(NAMES[r['team']], {})[norm(r['full_name'])] = (r['full_name'], r['position'], r['report_status'] + ' (' + (r['report_primary_injury'] or '?') + ')')
    ros = nfl.load_rosters_weekly(SEASON).to_dicts()
    rw = max(r['week'] for r in ros if r['game_type'] == 'REG')
    for r in ros:
        if r['week'] == rw and r['game_type'] == 'REG' and r['status'] == 'RES' and r['team'] in NAMES:
            out_now.setdefault(NAMES[r['team']], {}).setdefault(norm(r['full_name']), (r['full_name'], r['position'], 'effectif : ' + str(r.get('status_description_abbr') or r['status'])))
    # part de snaps : saison en cours, sinon saison précédente
    share = {}
    for season in (SEASON, SEASON - 1):
        acc = {}
        for r in nfl.load_snap_counts(season).to_dicts():
            if r['game_type'] != 'REG': continue
            a = acc.setdefault(norm(r['player']), [0, 0.0])
            a[0] += 1; a[1] += max(r['offense_pct'] or 0, r['defense_pct'] or 0)
        for k, (n, s) in acc.items():
            if n >= (1 if season == SEASON else 6):
                share.setdefault(k, (round(s/n, 2), season))

    rapport = dict(semaineBlessures=wk, nouveaux=[], revenus=[], qb=[])
    for team in sorted(n for a, n in NAMES.items() if a != 'LAR'):
        ab = absences.get(team, {})
        known = ' '.join(norm(x['pos']) for x in ab.get('offAbsences', []) + ab.get('defAbsences', []))
        unavailable = out_now.get(team, {})
        qbs_out = [v[0] for k, v in unavailable.items() if v[1] == 'QB' and share.get(k, (0,))[0] >= STARTER_PCT]
        if qbs_out and not ab.get('qbOut'):
            rapport['qb'].append(f"{team} : quarterback titulaire indisponible ({', '.join(qbs_out)}), aucun malus QB retenu")
        if ab.get('qbOut') and not any(v[1] == 'QB' for v in unavailable.values()):
            rapport['qb'].append(f"{team} : malus QB retenu, mais aucun quarterback indisponible dans les données officielles")
        for k, (name, pos, why) in unavailable.items():
            sh = share.get(k)
            if pos in ('QB', 'K', 'P', 'LS') or not sh or sh[0] < STARTER_PCT: continue
            if last_name(name) and last_name(name) in known.split(): continue
            rapport['nouveaux'].append(f"{team} : {name} ({pos}), {why}, {int(sh[0]*100)} % des snaps en {sh[1]}")
        out_last = {last_name(v[0]) for v in unavailable.values()}
        for x in ab.get('offAbsences', []) + ab.get('defAbsences', []):
            m = re.search(r'\(([^,)]+)', x['pos'])
            if m and last_name(m.group(1)) not in out_last:
                rapport['revenus'].append(f"{team} : {m.group(1)} n'apparaît plus comme indisponible")
    return rapport

if __name__ == '__main__':
    sys.exit(main())
