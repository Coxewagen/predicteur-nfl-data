#!/usr/bin/env python3
"""Absences automatiques du Prédicteur NFL.

Construit, pour chaque équipe, la liste des titulaires indisponibles (champs qbOut / offAbsences /
defAbsences lus par model.js) à partir des données officielles nflverse, sans saisie manuelle :

  - qui est absent  : statut « Out » ou « Doubtful » au dernier rapport de blessures, ou joueur sur la
                      liste des blessés (IR) dans l'effectif le plus récent ;
  - qui compte      : titulaire = au moins 50 % des snaps (saison en cours, sinon saison précédente) ;
  - niveau          : élite / titulaire / rotation, d'après le contrat (salaire annuel comparé aux autres
                      joueurs du même poste) et le temps de jeu ;
  - secteur         : passe / course / ligne selon le poste (module de style de jeu de model.js) ;
  - matchs joués    : « gp » = matchs joués cette saison par le joueur (sert à la dégressivité) ;
  - quarterback     : qbOut, niveau d'après le contrat, expérience du remplaçant d'après son nombre de
                      départs en carrière (qbReplacementTier), qbGp = matchs joués par le titulaire.

Corrections manuelles facultatives : fichier overrides.json (voir overrides.example.json).
"""
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

import nflreadpy as nfl
import polars as pl

HERE = Path(__file__).resolve().parent

# ----------------------------------------------------------------------------- réglages
STARTER_SHARE = 0.50      # part moyenne de snaps à partir de laquelle un joueur est titulaire
ELITE_PCT = 0.96          # salaire annuel dans les 4 % les mieux payés du poste -> « élite »
ROTATION_SHARE = 0.60     # moins de 60 % des snaps -> « rotation »
QB_ELITE_PCT = 0.93       # quarterback : parmi les 7 % les mieux payés -> « élite »
QB_LIMITE_PCT = 0.35      # quarterback : sous ce niveau de salaire -> « limite »
OUT_STATUSES = ('Out', 'Doubtful')

GROUP = {'WR': 'WR', 'CB': 'CB', 'ED': 'ED', 'IDL': 'IDL', 'LB': 'LB', 'S': 'S', 'TE': 'TE', 'RB': 'RB', 'FB': 'RB',
         'QB': 'QB', 'LT': 'OL', 'RT': 'OL', 'LG': 'OL', 'RG': 'OL', 'C': 'OL'}
# repli quand le poste du contrat est inconnu : poste de l'effectif -> groupe
ROSTER_GROUP = {'QB': 'QB', 'RB': 'RB', 'FB': 'RB', 'WR': 'WR', 'TE': 'TE', 'T': 'OL', 'G': 'OL', 'C': 'OL',
                'OL': 'OL', 'OT': 'OL', 'OG': 'OL', 'DE': 'ED', 'OLB': 'ED', 'DT': 'IDL', 'NT': 'IDL', 'DL': 'IDL',
                'LB': 'LB', 'ILB': 'LB', 'MLB': 'LB', 'CB': 'CB', 'DB': 'CB', 'S': 'S', 'FS': 'S', 'SS': 'S'}
OFFENSE_GROUPS = {'QB', 'RB', 'WR', 'TE', 'OL'}
LANE = {'WR': 'pass', 'CB': 'pass', 'ED': 'pass', 'RB': 'rush', 'OL': 'line'}   # LB, S, TE, IDL : sans secteur
LABEL = {'WR': 'receveur', 'CB': 'cornerback', 'ED': 'edge rusher', 'IDL': 'défenseur de ligne', 'LB': 'linebacker',
         'S': 'safety', 'TE': 'tight end', 'RB': 'coureur', 'OL': 'ligne offensive'}
OL_LABEL = {'LT': 'ailier gauche', 'RT': 'ailier droit', 'LG': 'garde gauche', 'RG': 'garde droit', 'C': 'centre'}
IGNORED_POS = {'K', 'P', 'LS'}


def norm(s):
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z ]', '', re.sub(r'\b(jr|sr|ii|iii|iv)\b\.?', '', s)).strip()


class Donnees:
    """Charge une fois toutes les tables nflverse nécessaires."""

    def __init__(self, season, names, played):
        self.season, self.names, self.played = season, names, played
        # joueurs : correspondance gsis <-> pfr (les snaps sont indexés par pfr)
        pl_tab = nfl.load_players().select(['gsis_id', 'pfr_id', 'display_name', 'position']).to_dicts()
        self.pfr_to_gsis = {r['pfr_id']: r['gsis_id'] for r in pl_tab if r['pfr_id'] and r['gsis_id']}
        # effectifs (dernière semaine de saison régulière)
        ros = [r for r in nfl.load_rosters_weekly(season).to_dicts() if r['game_type'] == 'REG']
        self.roster_week = max(r['week'] for r in ros)
        self.roster = [r for r in ros if r['week'] == self.roster_week]
        # blessures (dernier rapport)
        inj = nfl.load_injuries(season).to_dicts()
        self.injury_week = max((r['week'] for r in inj), default=None)
        self.injuries = [r for r in inj if r['week'] == self.injury_week]
        # snaps : saison en cours et précédente
        self.snaps = {season: self._snap_table(season), season - 1: self._snap_table(season - 1)}
        # contrats actifs : salaire annuel (en millions) par groupe de poste
        con = nfl.load_contracts().filter(pl.col('is_active') == True).to_dicts()  # noqa: E712
        self.contract = {}
        self.apy_by_group = defaultdict(list)
        for r in con:
            g = GROUP.get(r['position'])
            if r['gsis_id'] and r['apy']:
                if r['gsis_id'] not in self.contract or r['apy'] > self.contract[r['gsis_id']]['apy']:
                    self.contract[r['gsis_id']] = dict(apy=r['apy'], group=g, pos=r['position'])
            if g and r['apy']:
                self.apy_by_group[g].append(r['apy'])
        for g in self.apy_by_group:
            self.apy_by_group[g].sort()
        # départs de QB en carrière
        sched = nfl.load_schedules(list(range(2006, season + 1))).filter(
            (pl.col('game_type') == 'REG') & pl.col('home_score').is_not_null()).to_dicts()
        self.starts = defaultdict(int)
        self.opening_qb = {}      # équipe -> quarterback du premier match de la saison
        for g in sorted(sched, key=lambda x: (x['season'], x['week'], str(x['gameday']))):
            for k, tk in (('home_qb_id', 'home_team'), ('away_qb_id', 'away_team')):
                if g.get(k):
                    self.starts[g[k]] += 1
                    if g['season'] == season and names.get(g[tk]) and names[g[tk]] not in self.opening_qb:
                        self.opening_qb[names[g[tk]]] = g[k]
        # statut courant des joueurs sur la liste des blessés (IR)
        self.ir = {r['gsis_id']: r for r in self.roster if r['status'] == 'RES' and r['gsis_id']}

    def _snap_table(self, season):
        """gsis_id -> dict(n=matchs joués, share=part moyenne de snaps, qb_snaps=snaps offensifs)."""
        try:
            rows = [r for r in nfl.load_snap_counts(season).to_dicts() if r['game_type'] == 'REG']
        except Exception:
            return {}
        acc = {}
        for r in rows:
            off, de = r['offense_snaps'] or 0, r['defense_snaps'] or 0
            if off + de <= 0:
                continue
            gid = self.pfr_to_gsis.get(r['pfr_player_id'])
            if not gid:
                continue
            a = acc.setdefault(gid, dict(n=0, s=0.0, off=0, team=r['team']))
            a['n'] += 1
            a['s'] += max(r['offense_pct'] or 0, r['defense_pct'] or 0)
            a['off'] += off
            a['team'] = r['team']
        return {g: dict(n=a['n'], share=a['s'] / a['n'], off=a['off'], team=a['team']) for g, a in acc.items()}

    # ------------------------------------------------------------------ utilitaires
    def share(self, gid):
        """Part de snaps du joueur : la plus élevée entre la saison en cours et la précédente (4 matchs
        minimum). Un joueur blessé tôt dans un match, ou revenu de blessure, a une part de snaps
        artificiellement basse cette saison alors qu'il était titulaire."""
        vals = []
        now = self.snaps[self.season].get(gid)
        if now:
            vals.append(now['share'])
        prev = self.snaps[self.season - 1].get(gid)
        if prev and prev['n'] >= 4:
            vals.append(prev['share'])
        return max(vals) if vals else None

    def gp(self, gid, team_name):
        now = self.snaps[self.season].get(gid)
        n = now['n'] if now else 0
        return min(n, self.played.get(team_name, n))

    def pct(self, group, apy):
        xs = self.apy_by_group.get(group)
        if not xs:
            return None
        return sum(1 for x in xs if x <= apy) / len(xs)


def niveau(d, gid, group, share):
    """Niveau du joueur : « elite », « titulaire » ou « rotation »."""
    c = d.contract.get(gid)
    pct = d.pct(group, c['apy']) if c else None
    if pct is not None and pct >= ELITE_PCT:
        return 'elite', pct
    if share is not None and share < ROTATION_SHARE:
        return 'rotation', pct
    return 'titulaire', pct


def raison(r):
    txt = r.get('report_primary_injury') or r.get('practice_primary_injury')
    return txt.lower() if txt else None


def construire(season, names, played, overrides=None):
    """Renvoie (absences par équipe, journal détaillé)."""
    d = Donnees(season, names, played)
    overrides = overrides or {}
    ignore = {norm(x) for x in overrides.get('ignorer', [])}
    forced = {norm(k): v for k, v in overrides.get('niveau', {}).items()}
    team_of_gsis = {r['gsis_id']: names[r['team']] for r in d.roster if r['gsis_id'] and r['team'] in names}
    pos_of = {r['gsis_id']: r['position'] for r in d.roster if r['gsis_id']}
    name_of = {r['gsis_id']: r['full_name'] for r in d.roster if r['gsis_id']}

    # --- indisponibles : « Out »/« Doubtful » + liste des blessés
    unavailable = {}   # gsis -> (équipe, raison)
    for r in d.injuries:
        gid = r['gsis_id']
        if gid and r['report_status'] in OUT_STATUSES and r['team'] in names:
            unavailable[gid] = (names[r['team']], raison(r) or r['report_status'])
            name_of.setdefault(gid, r['full_name'])
            pos_of.setdefault(gid, r['position'])
    for gid, r in d.ir.items():
        if r['team'] in names:
            unavailable.setdefault(gid, (names[r['team']], 'IR'))

    out = defaultdict(dict)
    journal = []

    # --- joueurs hors quarterback
    for gid, (team, why) in unavailable.items():
        pos = pos_of.get(gid)
        nm = name_of.get(gid, gid)
        if pos in IGNORED_POS or pos == 'QB' or norm(nm) in ignore:
            continue
        c = d.contract.get(gid)
        group = (c['group'] if c and c['group'] else None) or ROSTER_GROUP.get(pos)
        share = d.share(gid)
        if group is None or share is None or share < STARTER_SHARE:
            journal.append(f"{team} : {nm} ({pos}, {why}) ignoré — pas titulaire")
            continue
        tier, pct = niveau(d, gid, group, share)
        tier = forced.get(norm(nm), tier)
        side = 'offAbsences' if group in OFFENSE_GROUPS else 'defAbsences'
        label = LABEL[group] if group != 'OL' else OL_LABEL.get(c['pos'] if c else '', LABEL['OL'])
        entry = dict(tier=tier, pos=f"{label} titulaire ({nm}, {why})")
        if group in LANE:
            entry['lane'] = LANE[group]
        entry['gp'] = d.gp(gid, team)
        out[team].setdefault(side, []).append(entry)
        journal.append(f"{team} : {nm} ({pos}, {why}) -> {side[:3]} {tier}"
                       + (f", salaire {c['apy']:.1f} M$ (top {100 - round(pct * 100)} % du poste)" if c and pct is not None else '')
                       + f", {round(share * 100)} % des snaps, {entry['gp']} match(s) joué(s)")

    # --- quarterbacks
    qb_by_team = defaultdict(set)
    for r in d.roster:
        if r['position'] == 'QB' and r['gsis_id'] and r['team'] in names:
            qb_by_team[names[r['team']]].add(r['gsis_id'])
    for gid, a in d.snaps[season].items():          # QB ayant joué pour l'équipe cette saison
        if a['off'] > 0 and (pos_of.get(gid) == 'QB' or gid in d.contract and d.contract[gid]['group'] == 'QB'):
            tm = a['team']
            if tm in names:
                qb_by_team[names[tm]].add(gid)
    for team, qbs in qb_by_team.items():
        played_n = played.get(team, 0)

        def score(g):
            now = d.snaps[season].get(g)
            prev = d.snaps[season - 1].get(g)
            return (now['off'] if now else 0) + (0.5 * prev['off'] * (played_n + 1) / 17 if prev else 0)
        # titulaire = quarterback du premier match de la saison de l'équipe (c'est le titulaire désigné) ;
        # à défaut, celui qui a le plus joué.
        starter = d.opening_qb.get(team)
        if not starter or starter not in qbs:
            starter = max(qbs, key=score)
        if starter not in unavailable or norm(name_of.get(starter, '')) in ignore:
            continue
        nm = name_of.get(starter, starter)
        c = d.contract.get(starter)
        pct = d.pct('QB', c['apy']) if c else None
        tier = 'titulaire'
        if pct is not None and pct >= QB_ELITE_PCT:
            tier = 'elite'
        elif pct is not None and pct < QB_LIMITE_PCT:
            tier = 'limite'
        tier = forced.get(norm(nm), tier)
        others = [g for g in qbs if g != starter]
        healthy = [g for g in others if g not in unavailable]
        entry = out[team]
        entry['qbOut'] = True
        entry['qbTier'] = tier
        entry['qbGp'] = d.gp(starter, team)
        if not healthy and not others:
            entry['qbBackupOut'] = True      # aucun autre quarterback connu
        elif not healthy:
            entry['qbBackupOut'] = True      # tous les autres sont indisponibles aussi
        else:
            rep = max(healthy, key=lambda g: (d.snaps[season].get(g, {}).get('off', 0), d.starts.get(g, 0)))
            n = d.starts.get(rep, 0)
            entry['qbReplacementTier'] = 'aucune_experience' if n == 0 else ('limite' if n <= 5 else 'standard')
            journal.append(f"{team} : remplaçant {name_of.get(rep, rep)}, {n} départ(s) en carrière")
        journal.append(f"{team} : QB {nm} ({why_of(unavailable, starter)}) -> {tier}, {entry['qbGp']} match(s) joué(s)")

    # --- corrections manuelles : ajouts
    for team, extra in overrides.get('ajouts', {}).items():
        for k, v in extra.items():
            if isinstance(v, list):
                out[team].setdefault(k, []).extend(v)
            else:
                out[team][k] = v
        journal.append(f"{team} : ajout manuel ({', '.join(extra)})")
    return dict(out), journal


def why_of(unavailable, gid):
    return unavailable.get(gid, ('', '?'))[1]


def charger_overrides():
    p = HERE / 'overrides.json'
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception as e:  # fichier mal formé : on l'ignore plutôt que de bloquer le robot
        print('overrides.json illisible, ignoré :', e)
        return {}
