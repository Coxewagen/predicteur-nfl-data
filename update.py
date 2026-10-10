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

def _num(x):
    try:
        v = float(x)
        return None if v != v else v
    except (TypeError, ValueError):
        return None


def _proba_ml(m):
    return -m/(-m+100) if m < 0 else 100/(m+100)


def marche(g):
    """Marché du match d'après le calendrier nflverse (ligne + moneylines) : probabilité de victoire du domicile
    sans la marge du bookmaker, et ligne d'écart de points (positive = domicile favori). Rien si indisponible."""
    line, mh, ma = _num(g.get('spread_line')), _num(g.get('home_moneyline')), _num(g.get('away_moneyline'))
    out = {}
    if line is not None:
        out['mkLine'] = round(line, 1)
    if mh is not None and ma is not None:
        ph, pa = _proba_ml(mh), _proba_ml(ma)
        out['mkPH'] = round(ph/(ph+pa), 4)
        out['mkSrc'] = 'ml'
    elif line is not None:
        from statistics import NormalDist
        out['mkPH'] = round(NormalDist().cdf(line/13.86), 4)
        out['mkSrc'] = 'ligne'
    return out


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
        if not closed:
            mk = marche(g)
            if mk:
                row.update(mk)   # mkLine (ligne, + = domicile favori), mkPH (proba domicile sans marge), mkSrc
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
        prochaine = {}   # semaine du prochain match de chaque équipe (le rapport de blessures doit être celui-là)
        for g in games:
            if g['status'] != 'closed':
                for n in (g['home'], g['away']):
                    prochaine[n] = min(prochaine.get(n, 99), g['week'])
        absences, journal_abs = absences_auto.construire(
            SEASON, NAMES, {n: t['played'] for n, t in T.items()}, absences_auto.charger_overrides(), prochaine)
        state['absences'] = absences
    except Exception as e:  # noqa: BLE001
        print(f"Absences automatiques indisponibles ({e}) : absences du passage précédent conservées.")
        journal_abs = [f"calcul automatique indisponible : {e}"]
    for n, ab in absences.items():
        T[n].update({k: v for k, v in ab.items() if not (v is None or v is False or v == [])})  # (0 doit rester valide : qbGp, gp)
    # Fraîcheur des blessures pour chaque match à venir : « final » si le rapport final de la semaine du match est
    # publié pour les deux équipes, sinon « provisoire » (le robot s'appuie sur le rapport final précédent).
    for g in games:
        if g['status'] != 'closed':
            ok = all(T[n].get('injReport') == 'final' and T[n].get('injWeek') == g['week'] for n in (g['home'], g['away']))
            g['blessures'] = 'final' if ok else 'provisoire'
    try:
        for n, pr in prior_features(SEASON).items():
            if n in T:
                T[n]['prior'] = pr
    except Exception as e:  # noqa: BLE001
        print(f"Saison précédente indisponible ({e}) : ancienne formule utilisée.")
    teams = sorted(T.values(), key=lambda t: t['name'])
    # Infos pour la fiche détaillée d'un match : bilan, points par match, 3 derniers résultats.
    closed_by_team = {n: [] for n in T}
    for g in games:
        if g['status'] == 'closed':
            for me, opp, pf, pa in ((g['home'], g['away'], g['homeScore'], g['awayScore']),
                                    (g['away'], g['home'], g['awayScore'], g['homeScore'])):
                closed_by_team[me].append((g['kickoff'], 'V' if pf > pa else 'D' if pf < pa else 'N', opp, pf, pa))
    fiche = {}
    for n, t in T.items():
        r = sorted(closed_by_team[n])
        fiche[n] = dict(rec=f"{t['wins']}-{t['losses']}" + (f"-{t['ties']}" if t['ties'] else ''),
                        pf=round(t['pointsFor']/t['played'], 1) if t['played'] else None,
                        pa=round(t['pointsAgainst']/t['played'], 1) if t['played'] else None,
                        form=[dict(r=x[1], opp=x[2], s=f"{x[3]}-{x[4]}") for x in r[-3:]][::-1])

    # ---------- 3. prévisions ----------
    todo = [g for g in games if g['status'] == 'scheduled' and g['kickoff'] > now.strftime('%Y-%m-%dT%H:%M:%SZ')
            and T[g['home']]['played'] and T[g['away']]['played']]
    week_now = min((g['week'] for g in todo), default=max(g['week'] for g in games))
    (HERE/'_in.json').write_text(json.dumps(dict(teams=teams, weeks=weeks, games=todo, week=week_now)), encoding='utf-8')
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
                                    expHome=p['expHome'], expAway=p['expAway'], computedAt=stamp,
                                    **({'spread': round(p['spread'], 2)} if p.get('spread') is not None else {}))
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
    # arrivées en cours de saison (signatures, échanges, promotions) : information seulement, le calcul ne s'en sert pas
    # (rejeu 2020-2026 : ni la profondeur d'effectif ni un crédit de remplaçant n'améliorent les prévisions).
    try:
        rapport['arrivees'] = arrivees_effectif()
    except Exception as e:
        rapport['arrivees'] = [f"calcul indisponible : {e}"]

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
            pred = j and {k: j[k] for k in ('pHome', 'pAway', 'expHome', 'expAway', 'spread') if k in j}
            row = dict(g, pred=pred, abs={s: resume_abs(T[g[s]]) for s in ('home', 'away')})
            if not pred and g['status'] == 'scheduled' and g['id'] in preds:   # semaine à venir : prévision provisoire
                p = preds[g['id']]
                row['pred'] = dict(pHome=round(p['pHome'], 4), pAway=round(p['pAway'], 4), expHome=p['expHome'], expAway=p['expAway'])
                if p.get('spread') is not None:
                    row['pred']['spread'] = round(p['spread'], 2)
                row['prov'] = True
            out.append(row)
        return out
    # toutes les semaines du calendrier : passées (journal/historique), en cours, et à venir (prévisions provisoires,
    # recalculées à chaque passage du robot avec les absences et statistiques du moment)
    weeks_available = sorted(int(w) for w in weeks)
    all_weeks_games = {str(w): build_week_games(weeks[str(w)]) for w in weeks_available}
    page_games = all_weeks_games[str(cur_week)]
    lp = now.astimezone(ZoneInfo('Europe/Paris'))
    jours = ['lundi','mardi','mercredi','jeudi','vendredi','samedi','dimanche']
    mois = ['janvier','février','mars','avril','mai','juin','juillet','août','septembre','octobre','novembre','décembre']
    label = f"{jours[lp.weekday()]} {lp.day} {mois[lp.month-1]} {lp.year} à {lp.hour} h {lp.minute:02d}"
    data = dict(updatedAt=lp.strftime('%Y-%m-%dT%H:%M'), updatedLabel=label,
                season=SEASON, week=cur_week, weeksAvailable=weeks_available, weeks=all_weeks_games,
                teams=infos_equipes(), fiche=fiche, safe=SAFE, games=page_games, bilan=bilan,
                absences=[dict(team=t['name'], items=liste_abs(t)) for t in teams if liste_abs(t)],
                rapport=rapport)
    app_data = {k: v for k, v in data.items() if k != 'rapport'}
    tpl = (HERE/'template.txt').read_text(encoding='utf-8')
    # La page web n'embarque plus le "rapport" (contrôle des absences, noms de joueurs) : elle ne l'affiche plus
    # et index.html est public. Il reste dans rapport.json, à ton seul usage.
    (HERE/'index.html').write_text(tpl.replace('__DATA__', json.dumps(app_data, ensure_ascii=False).replace('</', '<\\/')), encoding='utf-8')
    # Même dict "data" que celui injecté dans la page web, publié à part en JSON brut : c'est le
    # fichier que l'appli mobile télécharge (voir www/app.js, DATA_URL) pour rester à jour sans
    # nouvelle version d'appli à chaque mise à jour des stats/blessures.
    # "rapport" (contrôle des absences) en est délibérément exclu : il détaille des noms de joueurs
    # et les raisons des écarts retenus/ignorés, bien plus parlant sur la méthode que le reste —
    # il n'a rien à faire dans un fichier public. Il reste dans rapport.json, à ton seul usage.
    # Section lue par le bouton "Mettre à jour" de predicteur-nfl.html : équipes (stats + absences) et calendrier/
    # résultats, dans le format même du prédicteur. Elle est ajoutée au seul fichier app-data.json (déjà publié
    # par le robot : aucun changement du workflow GitHub). Les noms de joueurs sont retirés (fichier public).
    def sans_nom(lst):
        return [dict(x, pos=re.sub(r' \(.*\)$', '', x.get('pos', ''))) for x in (lst or [])]
    equipes_pub = []
    for t in teams:
        t2 = dict(t)
        for k in ('offAbsences', 'defAbsences'):
            if k in t2:
                t2[k] = sans_nom(t2[k])
        equipes_pub.append(t2)
    app_file = dict(app_data, predicteur=dict(week=cur_week, updatedAt=data['updatedAt'], teams=equipes_pub, weeksHistory=weeks))
    (HERE/'app-data.json').write_text(json.dumps(app_file, ensure_ascii=False), encoding='utf-8')
    (HERE/'state.json').write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding='utf-8')
    (HERE/'rapport.json').write_text(json.dumps(rapport, ensure_ascii=False, indent=1), encoding='utf-8')
    for f in ('_in.json', '_out.json'):
        (HERE/f).unlink(missing_ok=True)

    print(f"Semaine {cur_week} : {len(todo)} matchs à venir calculés, {len(journal)} prévisions au journal.")
    print(f"Bilan : {bilan['correct']}/{bilan['total']} ; favoris nets : {bilan['safeCorrect']}/{bilan['safeTotal']}.")
    print(f"Absences à vérifier : {len(rapport['nouveaux'])} nouvelles possibles, {len(rapport['revenus'])} retours possibles, {len(rapport['qb'])} alertes QB.")
    for k in ('qb', 'nouveaux', 'revenus', 'arrivees'):
        for r in rapport.get(k, []):
            print(f"  [{k}] {r}")

def arrivees_effectif():
    """Joueurs apparus dans l'effectif actif (hors équipe d'entraînement) depuis la semaine 1, par équipe, avec leur
    équipe précédente la saison dernière quand elle est connue. Sert à repérer les signatures/échanges qui changent
    la valeur d'un remplaçant (ex. un vétéran qui arrive quand un titulaire se blesse)."""
    ros = [r for r in nfl.load_rosters_weekly(SEASON).to_dicts() if r['game_type'] == 'REG' and r['gsis_id']]
    wk = sorted({r['week'] for r in ros})
    if len(wk) < 2:
        return []
    first, last = wk[0], wk[-1]
    base = {(r['team'], r['gsis_id']) for r in ros if r['week'] == first}
    prev = {}
    try:
        for r in nfl.load_rosters_weekly(SEASON - 1).to_dicts():
            if r['game_type'] == 'REG':
                prev[r['gsis_id']] = r['team']
    except Exception:
        pass
    out = []
    for r in ros:
        if r['week'] != last or r['team'] not in NAMES or (r['team'], r['gsis_id']) in base:
            continue
        if r['status'] not in ('ACT', 'RES', 'INA') or r['position'] in ('K', 'P', 'LS'):
            continue
        old = prev.get(r['gsis_id'])
        origine = f", ex-{NAMES[old]}" if old in NAMES and old != r['team'] else ''
        out.append(f"{NAMES[r['team']]} : {r['full_name']} ({r['position']}){origine}")
    return sorted(out)


def prior_features(season):
    """Éléments de la saison précédente pour chaque équipe (voir model.js, bloc "SAISON PRÉCÉDENTE").
    pm/pm2 : marge moyenne par match en N-1 / N-2 ; pmb = 0,7*pm + 0,3*pm2 ; coachnew : l'entraîneur n'est pas
    celui de la fin de N-1 ; qbnew/qbd/qbrook : le QB titulaire actuel n'est pas le titulaire principal de N-1,
    écart de qualité (marge des matchs qu'il a démarrés en N-1, lissée) et QB sans saison de titulaire en N-1."""
    from collections import Counter, defaultdict
    rows = nfl.load_schedules([season - 2, season - 1, season]).filter(lambda_reg()).to_dicts()
    tg = []   # une ligne par équipe et par match
    for g in rows:
        played = g['home_score'] is not None and g['away_score'] is not None
        for side, opp in (('home', 'away'), ('away', 'home')):
            m = (g[side + '_score'] - g[opp + '_score']) if played else None
            tg.append(dict(s=g['season'], w=g['week'], team=NAMES[g[side + '_team']], qb=g.get(side + '_qb_id'),
                           coach=g.get(side + '_coach'), m=m))
    def margins(s, team):
        return [x['m'] for x in tg if x['s'] == s and x['team'] == team and x['m'] is not None]
    # qualité des QB en N-1 : marge des matchs démarrés (toutes équipes), lissée vers 0 (n/(n+6))
    qm = defaultdict(list)
    for x in tg:
        if x['s'] == season - 1 and x['m'] is not None and x['qb']:
            qm[x['qb']].append(x['m'])
    Q = {q: sum(v) / len(v) * len(v) / (len(v) + 6) for q, v in qm.items()}
    out = {}
    for team in set(NAMES.values()):
        m1, m2 = margins(season - 1, team), margins(season - 2, team)
        pm = sum(m1) / len(m1) if m1 else None
        pm2 = sum(m2) / len(m2) if m2 else None
        pmb = 0.7 * (pm or 0) + 0.3 * (pm2 or 0)
        prev = sorted([x for x in tg if x['s'] == season - 1 and x['team'] == team and x['m'] is not None], key=lambda x: x['w'])
        mainqb = Counter(x['qb'] for x in prev if x['qb']).most_common(1)
        mainqb = mainqb[0][0] if mainqb else None
        lastcoach = prev[-1]['coach'] if prev else None
        cur = sorted([x for x in tg if x['s'] == season and x['team'] == team], key=lambda x: x['w'])
        played = [x for x in cur if x['m'] is not None]
        qb_now = played[-1]['qb'] if played else None          # titulaire du dernier match joué
        coach_now = (played[-1] if played else (cur[0] if cur else {})).get('coach')
        qbnew = bool(mainqb and qb_now and qb_now != mainqb)
        qbd = (Q.get(qb_now, 0.0) - Q.get(mainqb, 0.0)) if qbnew else 0.0
        out[team] = dict(pm=None if pm is None else round(pm, 2), pm2=None if pm2 is None else round(pm2, 2),
                         pmb=round(pmb, 2), coachnew=1 if (lastcoach and coach_now and coach_now != lastcoach) else 0,
                         qbnew=1 if qbnew else 0, qbd=round(qbd, 2),
                         qbrook=1 if (qbnew and qb_now not in Q) else 0)
    return out

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
