#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Signaux d'edge NFL — on ne prédit plus le résultat, on cherche où le book a pu se tromper.
==========================================================================================
Usage :  python nfl_signaux.py
Dépendances : pandas, numpy, matplotlib (Python 3.10+) — scikit-learn n'est plus nécessaire
Données : nflverse (CSV GitHub, cache ./nfl_cache) + prévisions de vent Open-Meteo (sans clé)

Le spread du book est la référence. Quatre signaux sont détectés :
  a) QB  : le QB confirmé n'est pas le titulaire probable (dernier titulaire)
  b) Blessure : titulaire Out/Doubtful (ou IR) avec un remplaçant nettement moins bon (≥ 3 pts)
  c) Vent : > 30 km/h dans un stade extérieur (signal sur le total : under)
  d) Coast : équipe déjà qualifiée ou éliminée contre une équipe encore en course

Pour chaque type de signal, l'historique (2015 -> saison passée) mesure s'il bat la ligne de
clôture. L'ajustement suggéré vient de cet historique (aucune info future) :
  - a, b : ajustement = β x taille du signal (β appris sur les saisons passées, entre 0 et 1)
  - c, d : ajustement = écart moyen passé vs la ligne, rétréci selon le nombre de cas
  - proba ajustée = loi normale autour de la ligne du book décalée de l'ajustement

Convention : spread_line (nflverse) > 0 => le domicile est favori de ce nombre de points.
Handicap affiché = handicap de l'équipe concernée (ex. WAS +2,5).
"""

import base64
import html
import io
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

# =============================================================================
# CONFIGURATION
# =============================================================================
SAISON_DEBUT = 2015
SAISONS_DETAIL = [2022, 2023, 2024, 2025]     # saisons détaillées dans le tableau d'historique

DEMI_VIE_SEMAINES = 3.0                       # pondération exponentielle des stats joueurs
POSTES_TITULAIRES = {"QB": 1, "RB": 1, "WR": 3, "TE": 1}
PRIOR_SNAPS = 60
STATUTS_BLESSURE_ABSENT = {"Out", "Doubtful"}
STATUTS_ROSTER_ABSENT = {"RES"}

SEUIL_QB_PTS = 1.0                            # a) écart de valeur minimum entre les deux QB (pts)
SEUIL_BLESSURE_PTS = 3.0                      # b) écart titulaire / remplaçant minimum (pts)
BLESSURE_CUMUL = True                         # b) additionner les absences d'une même équipe (hors QB)
SEUIL_VENT_KMH = 30.0                         # c) vent minimum en extérieur
SEMAINE_MIN_COAST = 12                        # d) on ne cherche les équipes "coast" qu'en fin de saison
RETRECISSEMENT = 50                           # c, d : ajustement x n / (n + 50)
COTE_DEFAUT = 1.91                            # cote -110 si le book n'en donne pas
SEUIL_RENTABILITE = 0.524

FICHIER_HTML = "dashboard_nfl.html"
VERSION_PAGE = "2.4"                          # à augmenter à chaque modification de la page
FICHIER_ABSENCES = "absences.json"
CORRECTIONS = {"absents": {}, "presents": {}}

DOSSIER_CACHE = "nfl_cache"
CACHE_SAISON_EN_COURS_H = 12
URL_MATCHS = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
URL_REL = "https://github.com/nflverse/nflverse-data/releases/download/"
URL_PBP = URL_REL + "pbp/play_by_play_{s}.csv.gz"
URL_SNAPS = URL_REL + "snap_counts/snap_counts_{s}.csv"
URL_ROSTERS = URL_REL + "weekly_rosters/roster_weekly_{s}.csv"
URL_BLESSURES = URL_REL + "injuries/injuries_{s}.csv"
URL_METEO = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
             "&hourly=wind_speed_10m&wind_speed_unit=kmh&timezone=America%2FNew_York&forecast_days=16")

RENOMMAGE = {"STL": "LA", "SD": "LAC", "OAK": "LV", "LAR": "LA"}
COLONNES_PBP = ["game_id", "season", "week", "posteam", "defteam", "play_type", "epa",
                "qb_epa", "qb_dropback", "id", "passer_player_id",
                "rusher_player_id", "receiver_player_id"]

AFC = {"BUF", "MIA", "NE", "NYJ", "BAL", "CIN", "CLE", "PIT", "HOU", "IND", "JAX", "TEN",
       "DEN", "KC", "LV", "LAC"}
DIVISIONS = [{"BUF", "MIA", "NE", "NYJ"}, {"BAL", "CIN", "CLE", "PIT"}, {"HOU", "IND", "JAX", "TEN"},
             {"DEN", "KC", "LV", "LAC"}, {"DAL", "NYG", "PHI", "WAS"}, {"CHI", "DET", "GB", "MIN"},
             {"ATL", "CAR", "NO", "TB"}, {"ARI", "LA", "SF", "SEA"}]
# Stades (latitude, longitude) et stades couverts — pour la prévision de vent
STADES = {"ARI": (33.528, -112.263), "ATL": (33.755, -84.401), "BAL": (39.278, -76.623),
          "BUF": (42.774, -78.787), "CAR": (35.226, -80.853), "CHI": (41.862, -87.617),
          "CIN": (39.096, -84.516), "CLE": (41.506, -81.700), "DAL": (32.747, -97.095),
          "DEN": (39.744, -105.020), "DET": (42.340, -83.046), "GB": (44.501, -88.062),
          "HOU": (29.685, -95.411), "IND": (39.760, -86.164), "JAX": (30.324, -81.637),
          "KC": (39.049, -94.484), "LV": (36.091, -115.183), "LAC": (33.954, -118.339),
          "LA": (33.954, -118.339), "MIA": (25.958, -80.239), "MIN": (44.974, -93.258),
          "NE": (42.091, -71.264), "NO": (29.951, -90.081), "NYG": (40.813, -74.074),
          "NYJ": (40.813, -74.074), "PHI": (39.901, -75.168), "PIT": (40.447, -80.016),
          "SF": (37.403, -121.970), "SEA": (47.595, -122.332), "TB": (27.976, -82.503),
          "TEN": (36.167, -86.771), "WAS": (38.908, -76.865)}
STADES_COUVERTS = {"ARI", "ATL", "DAL", "DET", "HOU", "IND", "LV", "LAC", "LA", "MIN", "NO"}

TYPES = {"qb": "QB", "blessure": "Blessure", "vent": "Vent", "coast": "Coast"}


# =============================================================================
# 1. DONNÉES (repris du modèle précédent)
# =============================================================================
def telecharger(url, nom, saison_en_cours=False):
    """Télécharge dans le cache si absent ou périmé (saison en cours). Renvoie le chemin ou None."""
    os.makedirs(DOSSIER_CACHE, exist_ok=True)
    chemin = os.path.join(DOSSIER_CACHE, nom)
    if os.path.exists(chemin):
        age_h = (time.time() - os.path.getmtime(chemin)) / 3600
        if not saison_en_cours or age_h < CACHE_SAISON_EN_COURS_H:
            return chemin
    print(f"  téléchargement : {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=180) as r, open(chemin + ".tmp", "wb") as f:
            f.write(r.read())
        os.replace(chemin + ".tmp", chemin)
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"  ⚠ échec ({e})")
    return chemin if os.path.exists(chemin) else None


def lire_saisons(url, prefixe, saisons, courante, **kw):
    """Lit un fichier par saison et concatène."""
    morceaux = []
    for s in saisons:
        ext = ".csv.gz" if url.endswith(".gz") else ".csv"
        chemin = telecharger(url.format(s=s), f"{prefixe}_{s}{ext}", s == courante)
        if chemin:
            morceaux.append(pd.read_csv(chemin, low_memory=False, **kw))
    return pd.concat(morceaux, ignore_index=True) if morceaux else pd.DataFrame()


def charger_donnees():
    chemin = telecharger(URL_MATCHS, "games.csv", saison_en_cours=True)
    if not chemin:
        sys.exit("games.csv introuvable")
    matchs = pd.read_csv(chemin, low_memory=False)
    matchs = matchs[matchs["season"] >= SAISON_DEBUT].copy()
    for c in ("home_team", "away_team"):
        matchs[c] = matchs[c].replace(RENOMMAGE)
    courante = int(matchs["season"].max())
    saisons = list(range(SAISON_DEBUT, courante + 1))

    print("  play-by-play…")
    pbp = lire_saisons(URL_PBP, "pbp", saisons, courante, usecols=lambda c: c in COLONNES_PBP)
    print("  snaps, rosters, blessures…")
    snaps = lire_saisons(URL_SNAPS, "snaps", saisons, courante)
    rosters = lire_saisons(URL_ROSTERS, "rosters", saisons, courante,
                           usecols=["season", "week", "team", "position", "status",
                                    "full_name", "gsis_id", "pfr_id"])
    blessures = lire_saisons(URL_BLESSURES, "injuries", saisons, courante)

    for df, cols in ((pbp, ["posteam", "defteam"]), (snaps, ["team"]),
                     (rosters, ["team"]), (blessures, ["team"])):
        for c in cols:
            if c in df:
                df[c] = df[c].replace(RENOMMAGE)
    return matchs, pbp, snaps, rosters, blessures, courante


def stats_joueurs(pbp, snaps, rosters):
    """Par (saison, semaine, équipe, joueur) : EPA, snaps offensifs, snap share, poste."""
    # EPA attribuée : QB sur ses dropbacks, porteur sur les courses, receveur sur ses cibles
    db = pbp[(pbp["qb_dropback"] == 1) & pbp["qb_epa"].notna()]
    qb_id = db["id"].fillna(db["passer_player_id"]) if "id" in db else db["passer_player_id"]
    e1 = pd.DataFrame({"season": db["season"], "week": db["week"], "gsis_id": qb_id, "epa": db["qb_epa"]})
    ru = pbp[(pbp["qb_dropback"] != 1) & pbp["rusher_player_id"].notna() & pbp["epa"].notna()]
    e2 = pd.DataFrame({"season": ru["season"], "week": ru["week"],
                       "gsis_id": ru["rusher_player_id"], "epa": ru["epa"]})
    rc = pbp[pbp["receiver_player_id"].notna() & pbp["epa"].notna()]
    e3 = pd.DataFrame({"season": rc["season"], "week": rc["week"],
                       "gsis_id": rc["receiver_player_id"], "epa": rc["epa"]})
    epa = pd.concat([e1, e2, e3]).dropna().groupby(["season", "week", "gsis_id"])["epa"].sum()

    # Snaps (identifiant PFR) -> identifiant GSIS via les rosters
    corresp = rosters.dropna(subset=["pfr_id", "gsis_id"]).drop_duplicates("pfr_id")
    corresp = corresp.set_index("pfr_id")["gsis_id"]
    sn = snaps[snaps["offense_snaps"] > 0].copy()
    sn["gsis_id"] = sn["pfr_player_id"].map(corresp)
    sn = sn.dropna(subset=["gsis_id"])
    sn["share"] = sn["offense_pct"].astype(float)
    sn["share"] = np.where(sn["share"] > 1.5, sn["share"] / 100, sn["share"])  # % -> fraction
    poste = rosters.dropna(subset=["gsis_id"]).drop_duplicates("gsis_id", keep="last")
    sn["poste"] = sn["gsis_id"].map(poste.set_index("gsis_id")["position"]).fillna(sn["position"])
    sn["nom"] = sn["player"]

    j = sn[["season", "week", "team", "gsis_id", "nom", "poste", "offense_snaps", "share"]]
    j = j.merge(epa.reset_index(), on=["season", "week", "gsis_id"], how="left")
    j["epa"] = j["epa"].fillna(0.0)
    # Snaps offensifs de l'équipe ce match-là (snaps joueur / share)
    j["snaps_equipe"] = j["offense_snaps"] / j["share"].replace(0, np.nan)
    return j


def absents_par_semaine(blessures, rosters):
    """
    {(saison, semaine, équipe): {gsis_id absents}} connus AVANT le match :
    rapport de blessures (Out/Doubtful) + joueurs en réserve (IR) sur le roster de la semaine.
    """
    absents = defaultdict(set)
    semaines_rapport = set()
    if not blessures.empty:
        b = blessures[blessures["report_status"].isin(STATUTS_BLESSURE_ABSENT)]
        for r in b.itertuples(index=False):
            absents[(int(r.season), int(r.week), r.team)].add(r.gsis_id)
        semaines_rapport = set(zip(blessures["season"].astype(int), blessures["week"].astype(int)))
    r = rosters[rosters["status"].isin(STATUTS_ROSTER_ABSENT)]
    for x in r.itertuples(index=False):
        absents[(int(x.season), int(x.week), x.team)].add(x.gsis_id)
    return absents, semaines_rapport


def poids_recence(semaines, semaine_courante):
    """Pondération exponentielle : poids 1 pour la semaine précédente, /2 toutes les 3 semaines."""
    return np.power(0.5, (semaine_courante - 1 - np.asarray(semaines, dtype=float)) / DEMI_VIE_SEMAINES)


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def proba_moneyline(ml):
    if pd.isna(ml):
        return np.nan
    return -ml / (-ml + 100) if ml < 0 else 100 / (ml + 100)


def valeurs_joueurs(j_saison, semaine):
    """
    EPA/snap (rétrécie vers la moyenne du poste) et snap share pondérés, semaines 1..semaine-1.
    Le snap share est moyenné sur les matchs de l'ÉQUIPE (0 si le joueur n'a pas joué) :
    un titulaire déjà absent depuis plusieurs semaines n'est donc plus compté comme titulaire
    (son absence est déjà dans l'EPA de l'équipe -> pas de double peine).
    """
    passe = j_saison[j_saison["week"] < semaine].copy()
    if passe.empty:
        return pd.DataFrame(), {}, {}
    passe["w"] = poids_recence(passe["week"], semaine)
    passe["wE"] = passe["w"] * passe["epa"]
    passe["wS"] = passe["w"] * passe["offense_snaps"]
    passe["wP"] = passe["w"] * passe["share"]

    # Moyenne de chaque poste (sert aux rookies / joueurs sans données)
    g = passe.groupby("poste")[["wE", "wS"]].sum()
    moy_poste = (g["wE"] / g["wS"]).to_dict()

    # Poids total des matchs de chaque équipe (pour le snap share avec 0 si absent)
    semaines_eq = passe.drop_duplicates(["team", "week"])
    poids_eq = semaines_eq.groupby("team")["w"].sum().to_dict()
    snaps_eq = (semaines_eq.assign(x=semaines_eq["snaps_equipe"].fillna(63))
                .groupby("team").apply(lambda d: np.average(d["x"], weights=d["w"]),
                                       include_groups=False).to_dict())

    # Équipe actuelle du joueur = celle de son match le plus récent
    derniere = passe.sort_values("week").drop_duplicates("gsis_id", keep="last")
    equipe_act = derniere.set_index("gsis_id")["team"]
    avec_eq = passe[passe["team"] == passe["gsis_id"].map(equipe_act)]
    v = avec_eq.groupby("gsis_id").agg(team=("team", "first"), nom=("nom", "last"),
                                       poste=("poste", "last"), wE=("wE", "sum"),
                                       wS=("wS", "sum"), wP=("wP", "sum")).reset_index()
    v["moy_poste"] = v["poste"].map(moy_poste).fillna(0.0)
    v["epa_snap"] = (v["wE"] + PRIOR_SNAPS * v["moy_poste"]) / (v["wS"] + PRIOR_SNAPS)
    v["share"] = v["wP"] / v["team"].map(poids_eq)
    return v, moy_poste, snaps_eq


def ml_vers_decimale(ml):
    """Cote américaine -> cote décimale (ex. -150 -> 1.67, +130 -> 2.30)."""
    if pd.isna(ml):
        return np.nan
    return 1 + 100 / -ml if ml < 0 else 1 + ml / 100


# =============================================================================
# 2. VALEUR DES JOUEURS ET ABSENCES
# =============================================================================
def absences_detaillees(equipe, v, moy_poste, snaps_eq, absents):
    """
    Titulaires absents avec leur coût en points :
    (EPA/snap titulaire - EPA/snap remplaçant) x snap share x snaps/match.
    Renvoie une liste de dicts (poste, nom, remplaçant, pts, id du remplaçant).
    """
    if v is None or len(v) == 0:
        return []
    items = []
    snaps = snaps_eq.get(equipe, 63.0)
    eq = v[v["team"] == equipe]
    for poste, n_tit in POSTES_TITULAIRES.items():
        cand = eq[eq["poste"] == poste].sort_values("share", ascending=False)
        titulaires = cand.head(n_tit)
        reserve = [r for r in cand.iloc[n_tit:].itertuples(index=False) if r.gsis_id not in absents]
        for t in titulaires.itertuples(index=False):
            if t.gsis_id not in absents:
                continue
            if reserve:
                r = reserve.pop(0)
                epa_r, nom_r, id_r = r.epa_snap, r.nom, r.gsis_id
            else:
                epa_r, nom_r, id_r = moy_poste.get(poste, 0.0), f"moyenne {poste}", None
            items.append({"poste": poste, "nom": t.nom, "rempl": nom_r, "id_rempl": id_r,
                          "pts": max(0.0, (t.epa_snap - epa_r) * t.share * snaps)})
    return items


def valeur_qb(v, moy_poste, gsis):
    """EPA/snap d'un QB (où qu'il ait joué cette saison) ; inconnu -> moyenne du poste (rookie)."""
    if v is not None and len(v) and gsis in v.index:
        return float(v.loc[gsis, "epa_snap"])
    return float(moy_poste.get("QB", 0.0))


# =============================================================================
# 3. CLASSEMENTS : équipes qualifiées / éliminées (fin de saison)
# =============================================================================
def statut_equipes(joues_reg, calendrier_reg):
    """
    Statut avant la semaine en cours (calcul prudent, sans départages) :
      - 'qualifiée' : division assurée, ou au plus 2 autres équipes de la conférence peuvent l'égaler
      - 'éliminée'  : 7 équipes de la conférence ont déjà plus de victoires que son maximum possible
                      ET un rival de division aussi
      - 'en course' sinon
    """
    victoires = defaultdict(float)
    for g in joues_reg.itertuples(index=False):
        if g.result > 0:
            victoires[g.home_team] += 1
        elif g.result < 0:
            victoires[g.away_team] += 1
        else:
            victoires[g.home_team] += 0.5
            victoires[g.away_team] += 0.5
    restants = defaultdict(int)
    for g in calendrier_reg.itertuples(index=False):
        restants[g.home_team] += 1
        restants[g.away_team] += 1
    equipes = set(victoires) | set(restants)
    vmax = {t: victoires[t] + restants[t] for t in equipes}
    division = {t: d for d in DIVISIONS for t in d}
    statut = {}
    for t in equipes:
        conf = [x for x in equipes if (x in AFC) == (t in AFC) and x != t]
        rivaux = [x for x in division.get(t, set()) if x != t and x in equipes]
        div_assuree = all(vmax[x] < victoires[t] for x in rivaux)
        peuvent_egaler = sum(vmax[x] >= victoires[t] for x in conf)
        devant = sum(victoires[x] > vmax[t] for x in conf)
        rival_devant = any(victoires[x] > vmax[t] for x in rivaux)
        if div_assuree or peuvent_egaler <= 2:
            statut[t] = "qualifiée"
        elif devant >= 7 and rival_devant:
            statut[t] = "éliminée"
        else:
            statut[t] = "en course"
    return statut


# =============================================================================
# 4. MÉTÉO (prévision de vent pour les matchs à venir)
# =============================================================================
_cache_meteo = {}


def precharger_meteo(equipes):
    """Une seule requête Open-Meteo pour tous les stades (plusieurs coordonnées), avec 3 tentatives."""
    equipes = [t for t in sorted(set(equipes)) if t in STADES and t not in _cache_meteo]
    if not equipes:
        return
    lat = ",".join(str(STADES[t][0]) for t in equipes)
    lon = ",".join(str(STADES[t][1]) for t in equipes)
    url = URL_METEO.format(lat=lat, lon=lon)
    for essai in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=90) as r:
                d = json.loads(r.read())
            d = d if isinstance(d, list) else [d]
            for t, x in zip(equipes, d):
                _cache_meteo[t] = dict(zip(x["hourly"]["time"], x["hourly"]["wind_speed_10m"]))
            print(f"  météo : prévisions récupérées pour {len(equipes)} stades")
            return
        except Exception as e:
            print(f"  ⚠ météo, essai {essai + 1}/3 : {e}")
            time.sleep(5 * (essai + 1))
    for t in equipes:
        _cache_meteo[t] = {}
    print("  ⚠ météo indisponible : pas de signal vent pour les matchs à venir")


def vent_prevu(equipe, jour, heure):
    """Vent prévu (km/h) au stade de l'équipe à domicile, à l'heure du coup d'envoi (heure de New York)."""
    if equipe not in STADES:
        return np.nan
    if equipe not in _cache_meteo:
        precharger_meteo([equipe])
    try:
        debut = datetime.strptime(f"{jour} {heure}", "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return np.nan
    # Vent maximum sur les 3 heures du match
    vals = [_cache_meteo[equipe].get((debut + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00")) for h in range(3)]
    vals = [x for x in vals if x is not None]
    return max(vals) if vals else np.nan


def stade_exterieur(g):
    roof = str(g.roof) if not pd.isna(g.roof) else ""
    if roof in ("dome", "closed"):
        return False
    if roof in ("outdoors", "open"):
        return True
    return g.home_team not in STADES_COUVERTS


# =============================================================================
# 5. DÉTECTION DES SIGNAUX, SEMAINE PAR SEMAINE (aucune info future)
# =============================================================================
def detecter_signaux(matchs, joueurs, absents, semaines_rapport, courante):
    """
    Renvoie un DataFrame : un signal par ligne, avec le côté qu'il favorise ('dom' / 'ext' / 'under'),
    sa taille (pts) quand elle est mesurable, et les lignes du book.
    """
    signaux = []
    for saison, ms in matchs.groupby("season"):
        j_s = joueurs[joueurs["season"] == saison]
        dernier_qb = {}                                   # dernier titulaire de chaque équipe
        reg = ms[ms["game_type"] == "REG"]
        for semaine, mw in ms.groupby("week"):
            v, moy_poste, snaps_eq = valeurs_joueurs(j_s, semaine)
            v_idx = v.set_index("gsis_id") if len(v) else v
            statut = {}
            if semaine >= SEMAINE_MIN_COAST and (mw["game_type"] == "REG").all():
                statut = statut_equipes(reg[(reg["week"] < semaine) & reg["result"].notna()],
                                        reg[reg["week"] >= semaine])
            for g in mw.itertuples(index=False):
                joue = not pd.isna(g.result)
                if not joue and saison != courante:
                    continue
                base = {"game_id": g.game_id, "season": saison, "week": semaine,
                        "dom": g.home_team, "ext": g.away_team, "joue": joue,
                        "marge": g.result, "total": g.total, "spread_line": g.spread_line,
                        "total_line": g.total_line, "cote_spread_dom": g.home_spread_odds,
                        "cote_spread_ext": g.away_spread_odds, "cote_under": g.under_odds,
                        "ml_dom": g.home_moneyline, "ml_ext": g.away_moneyline}
                for cote, t, adv in (("dom", g.home_team, g.away_team), ("ext", g.away_team, g.home_team)):
                    autre = "ext" if cote == "dom" else "dom"
                    abs_t = absents.get((saison, semaine, t), set())
                    if (saison, semaine) not in semaines_rapport:
                        abs_t = abs_t | absents.get((saison, semaine - 1, t), set())
                    if (saison, semaine) == CORRECTIONS.get("cible"):
                        abs_t = (abs_t | CORRECTIONS["absents"].get(t, set())) - CORRECTIONS["presents"].get(t, set())
                    items = absences_detaillees(t, v, moy_poste, snaps_eq, abs_t)

                    # --- a) QB confirmé différent du titulaire probable ---
                    probable = dernier_qb.get(t)
                    qb_annonce = getattr(g, f"{'home' if cote == 'dom' else 'away'}_qb_id")
                    qb_annonce = qb_annonce if isinstance(qb_annonce, str) else None
                    confirme = qb_annonce
                    if not joue and confirme and confirme in abs_t:          # annoncé mais blessé
                        rempl = [i for i in items if i["poste"] == "QB"]
                        confirme = rempl[0]["id_rempl"] if rempl else None
                    if probable and confirme and confirme != probable:
                        share = float(v_idx.loc[probable, "share"]) if len(v_idx) and probable in v_idx.index else 1.0
                        ecart = (valeur_qb(v_idx, moy_poste, probable) - valeur_qb(v_idx, moy_poste, confirme)) \
                            * share * snaps_eq.get(t, 63.0)
                        if abs(ecart) >= SEUIL_QB_PTS:
                            nom_p = noms_joueurs.get(probable, probable)
                            nom_c = noms_joueurs.get(confirme, confirme)
                            perd = ecart > 0
                            signaux.append({**base, "type": "qb", "favorise": autre if perd else cote,
                                            "taille": abs(ecart), "equipe": t,
                                            "texte": f"{t} : QB {nom_c} au lieu de {nom_p} "
                                                     f"({'-' if perd else '+'}{abs(ecart):.1f} pts)"})

                    # --- b) titulaires absents (hors QB) avec des remplaçants nettement moins bons ---
                    hors_qb = [i for i in items if i["poste"] != "QB" and i["pts"] > 0]
                    if BLESSURE_CUMUL:
                        # Cumul par équipe : plusieurs absences moyennes peuvent faire un signal
                        total = sum(i["pts"] for i in hors_qb)
                        if total >= SEUIL_BLESSURE_PTS:
                            noms = ", ".join(f"{i['nom']} (-{i['pts']:.1f})" for i in
                                             sorted(hors_qb, key=lambda x: -x["pts"]) if i["pts"] >= 0.3)
                            signaux.append({**base, "type": "blessure", "favorise": autre, "taille": total,
                                            "equipe": t, "texte": f"{t} sans {noms} : -{total:.1f} pts"})
                    else:
                        for i in hors_qb:
                            if i["pts"] >= SEUIL_BLESSURE_PTS:
                                signaux.append({**base, "type": "blessure", "favorise": autre, "taille": i["pts"],
                                                "equipe": t, "texte": f"{t} : {i['nom']} ({i['poste']}) absent, "
                                                                      f"remplaçant {i['rempl']} (-{i['pts']:.1f} pts)"})

                    # --- d) équipe "coast" contre une équipe en course ---
                    if statut.get(t) in ("qualifiée", "éliminée") and statut.get(adv) == "en course":
                        signaux.append({**base, "type": "coast", "favorise": autre, "taille": np.nan,
                                        "equipe": t, "texte": f"{t} déjà {statut[t]}, {adv} encore en course"})

                # --- c) vent > 30 km/h en extérieur (total : under) ---
                if stade_exterieur(g) and str(g.location) != "Neutral":
                    vent = g.wind * 1.609 if joue and not pd.isna(g.wind) else (
                        vent_prevu(g.home_team, g.gameday, g.gametime)
                        if not joue and g.home_team in _cache_meteo else np.nan)
                    if not pd.isna(vent) and vent > SEUIL_VENT_KMH:
                        signaux.append({**base, "type": "vent", "favorise": "under", "taille": vent,
                                        "equipe": g.home_team, "texte": f"Vent {vent:.0f} km/h à {g.home_team}"})

            # Titulaires réels de la semaine -> titulaires probables de la semaine suivante
            for g in mw.itertuples(index=False):
                if pd.isna(g.result):
                    continue
                for c, t in (("home", g.home_team), ("away", g.away_team)):
                    q = getattr(g, f"{c}_qb_id")
                    if isinstance(q, str):
                        dernier_qb[t] = q
                        noms_joueurs.setdefault(q, getattr(g, f"{c}_qb_name"))
    return pd.DataFrame(signaux)


noms_joueurs = {}


# =============================================================================
# 6. HISTORIQUE DES SIGNAUX ET AJUSTEMENTS (appris sur les saisons passées)
# =============================================================================
def residu_favorable(s):
    """Écart (pts) du résultat vs la ligne du book, dans le sens du signal (positif = signal gagnant)."""
    if s["favorise"] == "under":
        return s["total_line"] - s["total"]
    r = s["marge"] - s["spread_line"]
    return r if s["favorise"] == "dom" else -r


def preparer_historique(sig):
    h = sig[sig["joue"]].copy()
    h = h[np.where(h["favorise"] == "under", h["total_line"].notna(), h["spread_line"].notna())]
    h["residu"] = h.apply(residu_favorable, axis=1)
    return h


def apprendre_ajustements(h, sigma_spread, sigma_total):
    """Ajustement par type de signal, à partir de l'historique fourni (saisons passées uniquement)."""
    aj = {}
    for typ in TYPES:
        x = h[h["type"] == typ]
        n = len(x)
        if typ in ("qb", "blessure"):
            # β : part de la taille du signal que le book n'a pas intégrée (régression par l'origine)
            beta = float(np.dot(x["residu"], x["taille"]) / np.dot(x["taille"], x["taille"])) if n else 0.0
            aj[typ] = {"beta": min(max(beta, 0.0), 1.0), "n": n}
        else:
            moy = float(x["residu"].mean()) if n else 0.0
            aj[typ] = {"fixe": max(0.0, moy * n / (n + RETRECISSEMENT)), "n": n}
        aj[typ]["sigma"] = sigma_total if typ == "vent" else sigma_spread
    return aj


def ajustement(s, aj):
    a = aj[s["type"]]
    return a["beta"] * s["taille"] if "beta" in a else a["fixe"]


def tableau_historique(h, courante):
    """Par type : nb de signaux, % gagné vs la ligne de clôture (hors push), écart moyen, détail par saison."""
    lignes = []
    for typ, nom in TYPES.items():
        x = h[(h["type"] == typ) & (h["season"] < courante) & (h["residu"] != 0)]
        ligne = {"Signal": nom, "Signaux": len(x),
                 "% gagné": (x["residu"] > 0).mean() if len(x) else np.nan,
                 "Écart moyen (pts)": x["residu"].mean() if len(x) else np.nan}
        for s_ in SAISONS_DETAIL:
            y = x[x["season"] == s_]
            ligne[str(s_)] = f"{(y['residu'] > 0).mean():.0%} ({len(y)})" if len(y) else "—"
        lignes.append(ligne)
    return pd.DataFrame(lignes)


# =============================================================================
# 7. SIGNAUX DE LA SEMAINE
# =============================================================================
def am_vers_dec(x):
    if pd.isna(x):
        return COTE_DEFAUT
    return round(1 + 100 / -x if x < 0 else 1 + x / 100, 2)


def signaux_semaine(sig, h, courante):
    a_venir = sig[(sig["season"] == courante) & ~sig["joue"]]
    if a_venir.empty:
        return pd.DataFrame(), None, {}
    semaine = int(a_venir["week"].min())
    cette = a_venir[a_venir["week"] == semaine].copy()
    passe = h[h["season"] < courante]
    jm = passe.drop_duplicates("game_id")
    sigma_spread = float(np.std(jm["marge"] - jm["spread_line"])) if len(jm) else 13.5
    sigma_total = float(np.std(jm["total"] - jm["total_line"])) if len(jm) else 13.5
    aj = apprendre_ajustements(passe, sigma_spread, sigma_total)
    rows = []
    for s in cette.to_dict("records"):
        a = ajustement(s, aj)
        sigma = aj[s["type"]]["sigma"]
        if s["favorise"] == "under":
            ligne, cote = s["total_line"], am_vers_dec(s["cote_under"])
            pari = "Under"
        else:
            eq = s["dom"] if s["favorise"] == "dom" else s["ext"]
            ligne = (-s["spread_line"] if s["favorise"] == "dom" else s["spread_line"])   # handicap de l'équipe
            cote = am_vers_dec(s["cote_spread_dom"] if s["favorise"] == "dom" else s["cote_spread_ext"])
            pari = eq
        p = 0.5 * (1 + math.erf((a / sigma) / math.sqrt(2)))
        x = passe[(passe["type"] == s["type"]) & (passe["residu"] != 0)]
        rows.append({**s, "pari": pari, "ligne": ligne, "cote": cote, "ajust": a, "sigma": sigma,
                     "p_ajustee": p, "hist_taux": (x["residu"] > 0).mean() if len(x) else np.nan,
                     "hist_n": len(x)})
    return pd.DataFrame(rows), semaine, aj


# =============================================================================
# 8. DASHBOARD HTML
# =============================================================================
CSS = """
*{box-sizing:border-box}
body{margin:0;background:#1a1a2e;color:#fff;font-family:Segoe UI,Roboto,Arial,sans-serif;font-size:18px}
.page{max-width:1200px;margin:auto;padding:16px}
h1{font-size:30px;margin:6px 0 16px}
h2{font-size:25px;margin:34px 0 14px;border-left:6px solid #4cc9f0;padding-left:12px}
.compte{font-size:28px;font-weight:800;background:#16324f;border:2px solid #4cc9f0;border-radius:14px;padding:18px;text-align:center}
.compte.zero{background:#3a3a5a;border-color:#55557a}
.bandeau{border-radius:10px;padding:10px 14px;font-weight:700;margin-bottom:12px}
#signaux{display:grid;gap:16px;margin-top:16px}
.sig{background:#22223b;border:2px solid #4cc9f0;border-radius:16px;padding:16px}
.gris .sig{border-color:#55557a;opacity:.85}
.gris .badge{background:#8888aa}
.sig .haut{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:baseline}
.sig .match{font-size:26px;font-weight:800}
.badge{font-size:15px;font-weight:800;padding:5px 12px;border-radius:20px;background:#4cc9f0;color:#0b1d2b}
.sig .texte{font-size:21px;font-weight:700;margin:10px 0 12px;color:#ffe08a}
.grille{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}
.case{background:#1a1a2e;border-radius:10px;padding:10px 12px}
.case .lib{font-size:14px;color:#9ad6f0;font-weight:700}
.case .val{font-size:22px;font-weight:800;margin-top:4px}
input{width:90px;background:#11111f;color:#fff;border:2px solid #4cc9f0;border-radius:8px;
      padding:5px 6px;font-size:19px;font-weight:800}
.bon{color:#2ecc71}.mauvais{color:#ff6b6b}.neutre{color:#d0d0e0}
.tab{overflow-x:auto;background:#22223b;border-radius:12px;padding:6px}
table{border-collapse:collapse;width:100%}
th{background:#2d2d50;font-size:16px;text-align:left;padding:12px 10px;white-space:nowrap}
td{padding:11px 10px;border-top:1px solid #3a3a5a;font-size:18px;white-space:nowrap}
.num{font-variant-numeric:tabular-nums;font-weight:700}
@media (max-width:700px){body{font-size:17px}.sig .match{font-size:23px}.sig .texte{font-size:19px}}
"""

JS = """
function erf(x){const s=Math.sign(x);x=Math.abs(x);const t=1/(1+0.3275911*x);
 return s*(1-(((((1.061405429*t-1.453152027)*t)+1.421413741)*t-0.284496736)*t+0.254829592)*t*Math.exp(-x*x))}
function phi(x){return 0.5*(1+erf(x/Math.SQRT2))}
function pc(p){return (p*100).toFixed(1).replace('.',',')+' %'}
function sg(x,d){const v=x.toFixed(d===undefined?1:d).replace('.',',');return (x>0?'+':'')+v}
function calc(el){
 const d=el.dataset, a=+d.ajust, s=+d.sigma, l0=+d.ligne;
 const l=parseFloat(el.querySelector('.in-ligne').value.replace(',','.'));
 const c=parseFloat(el.querySelector('.in-cote').value.replace(',','.'));
 // Ligne plus favorable que celle du book -> proba en hausse (et inversement)
 const dl=isNaN(l)?0:(d.under==='1'?(l-l0):(l-l0));
 const p=phi((a+dl)/s);
 el.querySelector('.p').innerHTML=pc(p);
 el.querySelector('.p').className='val p '+(p>0.524?'bon':(p<0.5?'mauvais':'neutre'));
 const ev=isNaN(c)?NaN:p*c-1;
 const e=el.querySelector('.ev');
 e.innerHTML=isNaN(ev)?'':sg(ev*100)+' %'; e.className='val ev '+(ev>0?'bon':'mauvais');
 const juste=d.under==='1'?(l0-a):(l0+a);
 el.querySelector('.juste').innerHTML=(d.under==='1'?'Under ':d.pari+' ')+(d.under==='1'?juste.toFixed(1).replace('.',','):sg(juste));
}
document.querySelectorAll('.sig').forEach(el=>{el.querySelectorAll('input').forEach(i=>i.addEventListener('input',()=>calc(el)));calc(el)});
"""


def f1(x):
    return "" if pd.isna(x) else f"{x:.1f}".replace(".", ",")


def sgn(x):
    return "" if pd.isna(x) else (f"+{x:.1f}" if x > 0 else f"{x:.1f}").replace(".", ",")


def carte_signal(r):
    e = html.escape
    under = r["favorise"] == "under"
    if under:
        aj_txt = f"Total {f1(r['ajust'])} pts plus bas" if r["ajust"] > 0.05 else "Aucun (déjà dans la ligne)"
        ligne_lib, ligne_aff = "Total book (under)", f"{r['ligne']:.1f}"
    else:
        aj_txt = (f"{f1(r['ajust'])} pts plus favorable à {r['pari']}" if r["ajust"] > 0.05
                  else "Aucun (déjà dans la ligne)")
        ligne_lib, ligne_aff = f"Spread book {r['pari']}", f"{r['ligne']:.1f}"
    hist = ("" if pd.isna(r["hist_taux"]) else
            f'<span class="{"bon" if r["hist_taux"] > SEUIL_RENTABILITE else "mauvais"}">'
            f'{r["hist_taux"]*100:.1f} %</span> ({r["hist_n"]})'.replace(".", ","))
    return f"""
<div class="sig" data-ajust="{r['ajust']:.4f}" data-sigma="{r['sigma']:.4f}" data-ligne="{r['ligne']}"
     data-under="{1 if under else 0}" data-pari="{e(str(r['pari']))}">
 <div class="haut"><div class="match">{e(r['dom'])} – {e(r['ext'])}</div><div class="badge">{TYPES[r['type']]}</div></div>
 <div class="texte">{e(r['texte'])}</div>
 <div class="grille">
  <div class="case"><div class="lib">{e(ligne_lib)}</div><div class="val"><input class="in-ligne" type="number" step="0.5" value="{ligne_aff}"></div></div>
  <div class="case"><div class="lib">Ajustement suggéré</div><div class="val" style="font-size:18px">{e(aj_txt)}</div></div>
  <div class="case"><div class="lib">Ligne juste</div><div class="val juste"></div></div>
  <div class="case"><div class="lib">Proba ajustée</div><div class="val p"></div></div>
  <div class="case"><div class="lib">Cote</div><div class="val"><input class="in-cote" type="number" step="0.01" value="{r['cote']:.2f}"></div></div>
  <div class="case"><div class="lib">EV</div><div class="val ev"></div></div>
  <div class="case"><div class="lib">Historique</div><div class="val" style="font-size:19px">{hist}</div></div>
 </div>
</div>"""


def table_html(df, fmt=None):
    fmt = fmt or {}
    tete = "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns)
    corps = ""
    for row in df.itertuples(index=False):
        cells = []
        for c, v in zip(df.columns, row):
            if c in fmt:
                v = fmt[c](v)
            cells.append(f"<td class='num'>{html.escape(str(v))}</td>")
        corps += "<tr>" + "".join(cells) + "</tr>"
    return f"<div class='tab'><table><thead><tr>{tete}</tr></thead><tbody>{corps}</tbody></table></div>"


def bandeau_blessures(semaine):
    r = CORRECTIONS.get("rapport")
    if not semaine or r is None:
        return ""
    corr = sum(len(v) for v in CORRECTIONS["absents"].values()) + sum(len(v) for v in CORRECTIONS["presents"].values())
    txt = f"🩹 Rapport blessures : semaine {r}" + (f" · {corr} correction(s) manuelle(s)" if corr else "")
    coul = "#1e5631" if r >= semaine else "#5a1e1e"
    return f'<div class="bandeau" style="background:{coul}">{txt}</div>'


SEUIL_FAVORI = 0.65                           # onglet Matchs : favori mis en avant au-dessus de ce %


FICHIER_BETCLIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cotes_betclic.json")


def charger_betclic():
    """Cotes Betclic écrites par cotes_betclic.py : {(dom, ext): [(date, cote_dom, cote_ext)]}."""
    try:
        with open(FICHIER_BETCLIC, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    out = defaultdict(list)
    for m in d.get("matchs", []):
        try:
            jour = datetime.strptime(m["debut"][:10], "%Y-%m-%d")
        except (KeyError, ValueError, TypeError):
            continue
        out[(m["dom"], m["ext"])].append((jour, float(m["cote_dom"]), float(m["cote_ext"])))
    return out


BETCLIC = {}


def cotes_betclic(g):
    """Cotes Betclic du match (même affiche, date à ±2 jours), sinon None."""
    try:
        jour = datetime.strptime(str(g.gameday), "%Y-%m-%d")
    except ValueError:
        return None
    for d, cd, ce in BETCLIC.get((g.home_team, g.away_team), []):
        if abs((d - jour).days) <= 2:
            return cd, ce
    return None


def carte_match(g, sig):
    """Une carte match : % de victoire de chaque équipe d'après les cotes (marge du book retirée)."""
    from zoneinfo import ZoneInfo
    e = html.escape
    jours = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]
    cd = "" if pd.isna(g.home_moneyline) else f"{am_vers_dec(g.home_moneyline):.2f}"   # marché (référence)
    ce = "" if pd.isna(g.away_moneyline) else f"{am_vers_dec(g.away_moneyline):.2f}"
    bc = cotes_betclic(g) if pd.isna(g.result) else None
    vd, ve = (f"{bc[0]:.2f}", f"{bc[1]:.2f}") if bc else (cd, ce)                      # affiché : Betclic si dispo
    cl_bc = " bc" if bc else ""
    try:   # heure de New York -> heure de Paris
        dt = datetime.strptime(f"{g.gameday} {g.gametime}", "%Y-%m-%d %H:%M").replace(
            tzinfo=ZoneInfo("America/New_York")).astimezone(ZoneInfo("Europe/Paris"))
        quand = f"{jours[dt.weekday()]} {dt.strftime('%d/%m')} · {dt.strftime('%H:%M').replace(':', ' h ')}"
    except (ValueError, TypeError):
        quand = ""
    score = ""
    if not pd.isna(g.result):                      # match déjà joué : score final
        score = f'<span class="score">{int(g.away_score)} – {int(g.home_score)}</span>'
    alertes = sig[sig["game_id"] == g.game_id] if sig is not None and len(sig) else []
    alerte = "".join(f'<div class="alerte">⚠️ {e(t)}</div>' for t in alertes["texte"]) if len(alertes) else ""
    joue = "" if pd.isna(g.result) else (' data-gagnant="dom"' if g.result > 0 else ' data-gagnant="ext"')
    gid = e(g.game_id)
    return f"""
<div class="mt" data-ordre="0" data-g="{gid}" data-dom="{e(g.home_team)}" data-ext="{e(g.away_team)}"{joue}>
 <div class="mt-haut"><span class="quand">{e(quand)}</span>{score}<span class="fiche">📋</span></div>
 <div class="eq" data-cote="e"><div class="nom">{e(g.away_team)}</div><div class="barre"><span></span><b></b></div>
   <input class="cote-e{cl_bc}" type="number" step="0.01" value="{ve}" data-defaut="{ce}" data-init="{ve}" placeholder="Betclic" title="Cote Betclic">
   <label class="pari"><input type="checkbox" class="chk" data-cote="e"><span>J'ai parié</span></label></div>
 <div class="eq" data-cote="d"><div class="nom">🏠 {e(g.home_team)}</div><div class="barre"><span></span><b></b></div>
   <input class="cote-d{cl_bc}" type="number" step="0.01" value="{vd}" data-defaut="{cd}" data-init="{vd}" placeholder="Betclic" title="Cote Betclic">
   <label class="pari"><input type="checkbox" class="chk" data-cote="d"><span>J'ai parié</span></label></div>
 <div class="contre-tag">⚠️ Contre le marché</div>
 {alerte}
</div>"""


def recap_html(w):
    """Récap des paris de la journée : cotes et boost modifiables, mise, gains potentiels."""
    return f"""
<div class="recap" data-sem="{w}">
 <h2>Mes paris J{w}</h2>
 <div class="recap-vide">Aucun pari</div>
 <div class="recap-liste"></div>
 <div class="recap-calc">
  <div class="case"><div class="lib">Mise (€)</div><div class="val"><input class="mise" type="number" step="1" value="10"></div></div>
  <div class="case"><div class="lib">Boost combiné (%)</div><div class="val"><input class="boost" type="number" step="1" value="0"></div></div>
  <div class="case"><div class="lib">Combiné : cote</div><div class="val r-ctot"></div></div>
  <div class="case"><div class="lib">Combiné : proba</div><div class="val r-proba"></div></div>
  <div class="case"><div class="lib">Combiné : gains</div><div class="val r-comb"></div></div>
 </div>
</div>"""


def onglets_journees(matchs, sig, semaine, courante):
    """Un onglet par journée (de la journée en cours à la fin de la saison), les matchs dedans."""
    m = matchs[(matchs["season"] == courante) & (matchs["week"] >= semaine)]
    boutons, panneaux = [], []
    for w, mw in m.groupby("week"):
        mw = mw.sort_values(["gameday", "gametime"])
        boutons.append(f'<button class="onglet" data-o="j{w}">J{w}</button>')
        cartes = "".join(carte_match(g, sig) for g in mw.itertuples(index=False))
        panneaux.append(f'<div class="panneau" id="j{w}"><div class="liste-matchs">{cartes}</div>{recap_html(w)}</div>')
    return boutons, panneaux


def fiches_matchs(matchs, blessures, rosters, joueurs, absents, semaines_rapport, courante, semaine):
    """
    Fiche de chaque match à venir : série de victoires/défaites, bilan, points pour/contre,
    joueurs absents (rapport de blessures + IR) avec le coût des titulaires.
    """
    s = matchs[matchs["season"] == courante]
    joues = s[s["result"].notna()].sort_values(["week", "gameday", "gametime"])
    j_s = joueurs[joueurs["season"] == courante]
    v, moy_poste, snaps_eq = valeurs_joueurs(j_s, semaine)
    part = v.set_index("gsis_id")["share"].to_dict() if len(v) else {}
    b = blessures[blessures["season"] == courante]
    sem_rapport = int(b["week"].max()) if len(b) else None
    ros = rosters[rosters["season"] == courante]
    sem_ros = ros["week"].max() if len(ros) else None
    ir = ros[(ros["week"] == sem_ros) & ros["status"].isin(STATUTS_ROSTER_ABSENT)] if sem_ros else ros.iloc[0:0]

    def forme(t, avant):
        g = joues[((joues["home_team"] == t) | (joues["away_team"] == t)) & (joues["week"] < avant)]
        out = []
        for x in g.itertuples(index=False):
            dom = x.home_team == t
            pf, pa = (x.home_score, x.away_score) if dom else (x.away_score, x.home_score)
            out.append({"s": int(x.week), "adv": x.away_team if dom else x.home_team, "dom": bool(dom),
                        "pf": int(pf), "pa": int(pa), "r": "V" if pf > pa else ("D" if pf < pa else "N")})
        return out

    def absences(t, w):
        sr = min(w, sem_rapport) if sem_rapport else None
        abs_t = set(absents.get((courante, sr, t), set())) if sr else set()
        if (courante, w) == CORRECTIONS.get("cible"):
            abs_t = (abs_t | CORRECTIONS["absents"].get(t, set())) - CORRECTIONS["presents"].get(t, set())
        couts = {i["nom"]: i for i in absences_detaillees(t, v, moy_poste, snaps_eq, abs_t)}
        liste, vus = [], set()
        rep = b[(b["team"] == t) & (b["week"] == sr) & b["report_status"].isin(["Out", "Doubtful", "Questionable"])] if sr else b.iloc[0:0]
        for x in rep.itertuples(index=False):
            vus.add(x.gsis_id)
            liste.append({"nom": x.full_name, "pos": x.position, "statut": x.report_status,
                          "blessure": "" if pd.isna(x.report_primary_injury) else str(x.report_primary_injury)})
        for x in ir[ir["team"] == t].itertuples(index=False):
            if x.gsis_id not in vus and part.get(x.gsis_id, 0) >= 0.30:      # titulaires sur IR seulement
                liste.append({"nom": x.full_name, "pos": x.position, "statut": "IR", "blessure": ""})
        for a in liste:
            c = couts.get(a["nom"])
            if c and c["pts"] >= 0.1:
                a["cout"] = round(c["pts"], 1)
                a["rempl"] = c["rempl"]
        ordre = {"Out": 0, "IR": 1, "Doubtful": 2, "Questionable": 3}
        return sorted(liste, key=lambda a: (-a.get("cout", 0), ordre.get(a["statut"], 9)))

    fiches = {}
    for g in s[(s["week"] >= semaine) & s["result"].isna()].itertuples(index=False):
        f = {}
        for cote, t in (("dom", g.home_team), ("ext", g.away_team)):
            fo = forme(t, g.week)
            v_, d_ = sum(x["r"] == "V" for x in fo), sum(x["r"] == "D" for x in fo)
            serie = ""
            if fo:
                dern = fo[-1]["r"]
                n = 0
                for x in reversed(fo):
                    if x["r"] != dern:
                        break
                    n += 1
                serie = f"{n} {dern}"
            pts_pour = round(sum(x["pf"] for x in fo) / len(fo), 1) if fo else None
            pts_contre = round(sum(x["pa"] for x in fo) / len(fo), 1) if fo else None
            f[cote] = {"eq": t, "bilan": f"{v_}-{d_}", "serie": serie, "pour": pts_pour, "contre": pts_contre,
                       "derniers": fo[-5:][::-1], "absents": absences(t, g.week)}
        f["rapport"] = sem_rapport
        fiches[g.game_id] = f
    return fiches


JS_MATCHS = """
const FICHES=JSON.parse(document.getElementById('fiches').textContent);
function lireLS(k){try{return JSON.parse(localStorage.getItem(k)||'{}')}catch(e){return {}}}
function ecrireLS(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}
let PARIS=lireLS('paris_nfl'), COTES=lireLS('cotes_betclic_nfl');
const fr=(x,d)=>x.toFixed(d===undefined?2:d).replace('.',',');

function majMatch(el){
 const g=el.dataset.gagnant;
 if(g){el.querySelectorAll('.eq')[g==='dom'?1:0].classList.add('gagnant')}
 const cd=parseFloat(el.querySelector('.cote-d').value.replace(',','.')), ce=parseFloat(el.querySelector('.cote-e').value.replace(',','.'));
 if(isNaN(cd)||isNaN(ce)||cd<=1||ce<=1)return;
 const pd=(1/cd)/((1/cd)+(1/ce)), pe=1-pd, eqs=el.querySelectorAll('.eq');
 el.dataset.pd=pd;
 [[eqs[0],pe],[eqs[1],pd]].forEach(([q,p])=>{
   const c=p>=%FAV%?'vert':(p>=0.5?'orange':'rouge');
   q.querySelector('.barre span').className=c; q.querySelector('.barre span').style.width=(p*100).toFixed(1)+'%';
   q.querySelector('.barre b').textContent=(p*100).toFixed(1).replace('.',',')+' %';
   q.classList.toggle('fav',p>=%FAV%);
 });
 el.dataset.ordre=Math.max(pd,pe); el.classList.toggle('sur',Math.max(pd,pe)>=%FAV%);
}
function trier(){document.querySelectorAll('.liste-matchs').forEach(L=>
 [...L.children].sort((a,b)=>b.dataset.ordre-a.dataset.ordre).forEach(x=>L.appendChild(x)))}

// --- Paris : bouton "Parier" -> "✓ Parié", récap par journée ---
// Favori du marché = cotes d'origine (marge retirée), pas les cotes modifiées
function pMarche(el){const d=parseFloat(el.querySelector('.cote-d').dataset.defaut), e=parseFloat(el.querySelector('.cote-e').dataset.defaut);
 if(isNaN(d)||isNaN(e))return NaN; return (1/d)/((1/d)+(1/e))}
function contreMarche(el,cote){const pd=pMarche(el); if(isNaN(pd))return false; return (cote==='d'?pd:1-pd)<0.5}
function majBoutons(el){const p=PARIS[el.dataset.g];
 el.querySelectorAll('.pari').forEach(b=>{const c=b.querySelector('.chk'), on=!!(p&&p.cote===c.dataset.cote);
   c.checked=on; b.classList.toggle('on',on)});
 el.classList.toggle('contre',!!(p&&contreMarche(el,p.cote)))}
function majRecap(R){
 // Récap = combiné uniquement ; cotes et probas bloquées au moment où « J'ai parié » est coché
 const w=R.dataset.sem, L=R.querySelector('.recap-liste'); L.innerHTML='';
 const mats=[...document.querySelectorAll('#j'+w+' .mt')].filter(el=>PARIS[el.dataset.g]);
 R.querySelector('.recap-vide').style.display=mats.length?'none':'block';
 R.querySelector('.recap-calc').style.display=mats.length?'grid':'none';
 mats.forEach(el=>{
   const P=PARIS[el.dataset.g], c=P.cote, eq=c==='d'?el.dataset.dom:el.dataset.ext, adv=c==='d'?el.dataset.ext:el.dataset.dom;
   const ligne=document.createElement('div'); ligne.className='r-ligne';
   ligne.innerHTML='<div class="r-eq">'+eq+' <span>vs '+adv+'</span>'+(contreMarche(el,c)?' <em class="r-contre">⚠️ Contre le marché</em>':'')+'</div>'
     +'<div class="r-p">'+(isNaN(P.p)?'':fr(P.p*100,1)+' %')+'</div>'
     +'<div class="r-cote-fixe">🔒 '+(isNaN(P.valeur)?'—':fr(P.valeur))+'</div><button class="r-x">✕</button>';
   ligne.querySelector('.r-x').addEventListener('click',()=>{delete PARIS[el.dataset.g];ecrireLS('paris_nfl',PARIS);majBoutons(el);majRecap(R)});
   L.appendChild(ligne);
 });
 calcRecap(R);
}
function calcRecap(R){
 const w=R.dataset.sem, mise=parseFloat(R.querySelector('.mise').value.replace(',','.'))||0;
 const boost=parseFloat(R.querySelector('.boost').value.replace(',','.'))||0;
 let prodC=1, prodP=1, n=0;
 [...document.querySelectorAll('#j'+w+' .mt')].filter(el=>PARIS[el.dataset.g]).forEach(el=>{
   const P=PARIS[el.dataset.g]; if(!isNaN(P.valeur)){prodC*=P.valeur;n++} if(!isNaN(P.p))prodP*=P.p;
 });
 const coteB=prodC*(1+boost/100);
 R.querySelector('.r-ctot').textContent=n?fr(coteB):'';
 R.querySelector('.r-proba').textContent=n?fr(prodP*100,1)+' %':'';
 R.querySelector('.r-comb').textContent=n?fr(mise*coteB)+' €':'';
 const S=lireLS('mises_nfl');S[w]={mise:mise,boost:boost};ecrireLS('mises_nfl',S);
}
function sauverCote(el,inp){const k=el.dataset.g+'_'+(inp.classList.contains('cote-d')?'d':'e');
 if(inp.value&&inp.value!==inp.dataset.init)COTES[k]={v:inp.value,base:inp.dataset.init};else delete COTES[k];ecrireLS('cotes_betclic_nfl',COTES)}
function recapDe(el){return el.closest('.panneau').querySelector('.recap')}

document.querySelectorAll('.mt').forEach(el=>{
 majMatch(el);
 const P0=PARIS[el.dataset.g];
 if(P0&&P0.valeur===undefined){const v=parseFloat(el.querySelector(P0.cote==='d'?'.cote-d':'.cote-e').value), pd=parseFloat(el.dataset.pd);
   P0.valeur=v; P0.p=isNaN(pd)?NaN:(P0.cote==='d'?pd:1-pd); ecrireLS('paris_nfl',PARIS)}
 // Cote tapée à la main : reprise seulement si la cote d'origine (Betclic ou marché) n'a pas bougé depuis
 ['d','e'].forEach(c=>{const k=el.dataset.g+'_'+c, inp=el.querySelector('.cote-'+c), s=COTES[k];
   if(s&&typeof s==='object'&&s.base===inp.dataset.init)inp.value=s.v; else if(s){delete COTES[k];ecrireLS('cotes_betclic_nfl',COTES)}});
 majMatch(el); majBoutons(el);
 el.querySelectorAll('.cote-d,.cote-e').forEach(i=>{
   i.addEventListener('input',()=>{majMatch(el);sauverCote(el,i)}); i.addEventListener('change',trier)});
 el.querySelectorAll('.chk').forEach(c=>c.addEventListener('change',()=>{
   const g=el.dataset.g;
   if(c.checked){const v=parseFloat(el.querySelector(c.dataset.cote==='d'?'.cote-d':'.cote-e').value.replace(',','.')), pd=parseFloat(el.dataset.pd);
     PARIS[g]={cote:c.dataset.cote, valeur:v, p:isNaN(pd)?NaN:(c.dataset.cote==='d'?pd:1-pd)}} else delete PARIS[g];
   ecrireLS('paris_nfl',PARIS); majBoutons(el); majRecap(recapDe(el))}));
 el.addEventListener('click',ev=>{if(ev.target.closest('input,button,label'))return; ouvrirFiche(el)});
});
document.querySelectorAll('.recap').forEach(R=>{
 const S=lireLS('mises_nfl')[R.dataset.sem]; if(S){R.querySelector('.mise').value=S.mise;R.querySelector('.boost').value=S.boost}
 R.querySelectorAll('.mise,.boost').forEach(i=>i.addEventListener('input',()=>calcRecap(R)));
 majRecap(R)});
trier();

// --- Fiche match ---
function colEquipe(f,titre){
 const der=f.derniers.map(x=>'<div class="d-l"><span class="pastille '+x.r+'">'+x.r+'</span> J'+x.s+' '+(x.dom?'vs ':'@ ')+x.adv
   +' <b>'+x.pf+'-'+x.pa+'</b></div>').join('')||'<div class="d-l">—</div>';
 const abs=f.absents.map(a=>'<div class="d-abs"><span class="st st-'+a.statut+'">'+a.statut+'</span> <b>'+a.nom+'</b> '+a.pos
   +(a.blessure?' · '+a.blessure:'')+(a.cout?' <span class="cout">-'+fr(a.cout,1)+' pts</span>':'')+'</div>').join('')||'<div class="d-l">—</div>';
 return '<div class="d-col"><div class="d-eq">'+titre+'</div>'
  +'<div class="d-chiffres"><div><div class="lib">Bilan</div><div class="val">'+f.bilan+'</div></div>'
  +'<div><div class="lib">Série</div><div class="val">'+(f.serie||'—')+'</div></div>'
  +'<div><div class="lib">Pts pour</div><div class="val">'+(f.pour===null?'—':fr(f.pour,1))+'</div></div>'
  +'<div><div class="lib">Pts contre</div><div class="val">'+(f.contre===null?'—':fr(f.contre,1))+'</div></div></div>'
  +'<div class="d-titre">5 derniers matchs</div>'+der+'<div class="d-titre">Absents</div>'+abs+'</div>';
}
function ouvrirFiche(el){
 const f=FICHES[el.dataset.g]; if(!f)return;
 document.getElementById('fiche-contenu').innerHTML='<div class="d-haut">'+el.dataset.ext+' @ '+el.dataset.dom
   +'<span class="d-rap">🩹 Rapport J'+(f.rapport||'—')+'</span></div><div class="d-grille">'
   +colEquipe(f.ext,el.dataset.ext)+colEquipe(f.dom,'🏠 '+el.dataset.dom)+'</div>';
 document.getElementById('fiche').style.display='flex';
}
function fermerFiche(){document.getElementById('fiche').style.display='none'}
document.getElementById('fiche').addEventListener('click',e=>{if(e.target.id==='fiche')fermerFiche()});
document.getElementById('fiche-x').addEventListener('click',fermerFiche);
document.addEventListener('keydown',e=>{if(e.key==='Escape')fermerFiche()});

function onglet(n){document.querySelectorAll('.onglet').forEach(x=>x.classList.toggle('actif',x.dataset.o===n));
 document.querySelectorAll('.panneau').forEach(x=>x.style.display=x.id===n?'block':'none');
 try{localStorage.setItem('onglet_nfl',n)}catch(e){}}
document.querySelectorAll('.onglet').forEach(b=>b.addEventListener('click',()=>onglet(b.dataset.o)));
let o0=document.querySelector('.onglet').dataset.o;
try{const x=localStorage.getItem('onglet_nfl');if(x&&document.getElementById(x))o0=x}catch(e){}
onglet(o0);
""".replace("%FAV%", str(SEUIL_FAVORI))

CSS_MATCHS = """
.onglets{display:flex;gap:8px;margin:4px 0 18px;flex-wrap:wrap}
.onglet{flex:1 0 auto;min-width:76px;font-size:20px;font-weight:800;padding:13px;border-radius:12px;border:2px solid #4cc9f0;
        background:#22223b;color:#fff;cursor:pointer}
.onglet.actif{background:#4cc9f0;color:#0b1d2b}
.onglet.sig-o{border-color:#f39c12}.onglet.sig-o.actif{background:#f39c12}
.liste-matchs{display:grid;grid-template-columns:repeat(auto-fill,minmax(400px,1fr));gap:14px}
.mt{background:#22223b;border:2px solid #33335a;border-radius:16px;padding:14px;cursor:pointer}
.mt:hover{background:#2a2a48}
.mt.sur{border-color:#2ecc71}
.mt-haut{font-size:15px;color:#9ad6f0;font-weight:700;margin-bottom:6px;display:flex;gap:10px;align-items:center}
.mt-haut .fiche{margin-left:auto;font-size:18px}
.score{font-size:18px;font-weight:800;color:#fff}
.eq{display:grid;grid-template-columns:90px 1fr 74px 104px;gap:8px;align-items:center;padding:7px 0}
.eq .nom{font-size:22px;font-weight:800;white-space:nowrap}
.eq.fav .nom{color:#2ecc71}
.eq.gagnant .nom::after{content:" ✅"}
.eq .barre{position:relative;background:#3a3a5a;border-radius:8px;height:32px;overflow:hidden}
.eq .barre span{position:absolute;left:0;top:0;bottom:0;border-radius:8px}
.eq .barre b{position:relative;display:block;text-align:center;line-height:32px;font-size:19px;text-shadow:0 0 4px #000}
.vert{background:#2ecc71}.orange{background:#f39c12}.rouge{background:#e74c3c}
.eq input{width:74px}
.eq input.bc{border-color:#2ecc71}
.pari{display:flex;align-items:center;gap:6px;font-size:14px;font-weight:800;padding:5px 6px;border-radius:9px;
       border:2px solid #8888aa;background:#1a1a2e;color:#fff;cursor:pointer;line-height:1.1}
.pari .chk{width:22px;height:22px;accent-color:#2ecc71;cursor:pointer;flex:none}
.pari.on{background:#2ecc71;border-color:#2ecc71;color:#0b2914}
.contre-tag{display:none;margin-top:8px;font-size:17px;font-weight:800;color:#ff8a8a}
.mt.contre{border-color:#e74c3c}.mt.contre .contre-tag{display:block}
.r-contre{font-style:normal;font-size:15px;color:#ff8a8a;font-weight:800}
.alerte{margin-top:8px;font-size:16px;font-weight:700;color:#ffcf70}
.recap{background:#16324f;border:2px solid #4cc9f0;border-radius:16px;padding:6px 16px 16px;margin-top:22px}
.recap h2{margin:14px 0 10px;border:none;padding:0}
.recap-vide{font-size:19px;font-weight:700;color:#c8c8e0;padding:6px 0}
.r-ligne{display:grid;grid-template-columns:1fr 90px 110px 44px;gap:10px;align-items:center;padding:9px 0;border-top:1px solid #2d5a80}
.r-eq{font-size:21px;font-weight:800}.r-eq span{font-size:16px;color:#c8c8e0;font-weight:600}
.r-p{font-size:18px;font-weight:700}.r-cote-fixe{font-size:20px;font-weight:800}
.r-x{background:#5a1e1e;color:#fff;border:none;border-radius:8px;font-size:18px;padding:6px;cursor:pointer}
.r-cote{width:84px}
.recap-calc{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-top:12px}
.recap-calc .case{background:#1a1a2e}
.r-comb{color:#2ecc71}
.version{font-size:16px;font-weight:700;color:#9ad6f0;margin-left:12px;white-space:nowrap}
@media (max-width:760px){.version{display:block;margin:4px 0 0}}
#fiche{position:fixed;inset:0;background:rgba(5,5,15,.85);display:none;align-items:flex-start;justify-content:center;z-index:50;overflow:auto;padding:20px 10px}
#fiche-boite{background:#1f1f38;border:2px solid #4cc9f0;border-radius:18px;max-width:1000px;width:100%;padding:18px;position:relative}
#fiche-x{position:absolute;top:10px;right:12px;font-size:26px;background:none;border:none;color:#fff;cursor:pointer}
.d-haut{font-size:27px;font-weight:800;margin-bottom:14px;padding-right:40px}
.d-rap{font-size:15px;font-weight:700;color:#9ad6f0;margin-left:14px}
.d-grille{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.d-col{background:#22223b;border-radius:14px;padding:14px}
.d-eq{font-size:24px;font-weight:800;margin-bottom:10px}
.d-chiffres{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:8px}
.d-chiffres .lib{font-size:14px;color:#9ad6f0;font-weight:700}.d-chiffres .val{font-size:22px;font-weight:800}
.d-titre{font-size:16px;color:#9ad6f0;font-weight:800;margin:12px 0 6px;text-transform:uppercase}
.d-l{font-size:18px;padding:4px 0}
.pastille{display:inline-block;width:28px;text-align:center;border-radius:6px;font-weight:800;color:#0b1d2b}
.pastille.V{background:#2ecc71}.pastille.D{background:#e74c3c;color:#fff}.pastille.N{background:#aaa}
.d-abs{font-size:17px;padding:5px 0;border-top:1px solid #33335a}
.st{display:inline-block;font-size:13px;font-weight:800;padding:2px 7px;border-radius:6px;background:#555}
.st-Out,.st-IR{background:#e74c3c}.st-Doubtful{background:#f39c12;color:#111}.st-Questionable{background:#f1c40f;color:#111}
.cout{color:#ff8a8a;font-weight:800}
@media (max-width:760px){.liste-matchs{grid-template-columns:1fr}.onglet{font-size:17px;padding:10px}
 .eq{grid-template-columns:62px 1fr 64px 92px;gap:6px}.eq .nom{font-size:19px}.eq input{width:66px}
 .d-grille{grid-template-columns:1fr}.d-chiffres{grid-template-columns:repeat(2,1fr)}
 .r-ligne{grid-template-columns:1fr 64px 84px 36px}}
"""


def maj_paris():
    """Date et heure de génération, heure de Paris."""
    from zoneinfo import ZoneInfo
    jours = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]
    t = datetime.now(ZoneInfo("Europe/Paris"))
    return f"{jours[t.weekday()]} {t.strftime('%d/%m %H:%M').replace(':', ' h ')}"


def generer_html(semaine_df, semaine, courante, hist_tab, matchs=None, sig=None, fiches=None, fichier=FICHIER_HTML):
    print("5) Génération du dashboard HTML…")
    boutons, panneaux = onglets_journees(matchs, sig, semaine, courante) if matchs is not None and semaine else ([], [])
    # Edge = signal dont l'historique donne un ajustement > 0 ; les autres sont déjà dans la ligne du book
    edge = semaine_df[semaine_df["ajust"] > 0.05] if len(semaine_df) else semaine_df
    autres = semaine_df[semaine_df["ajust"] <= 0.05] if len(semaine_df) else semaine_df
    n = len(edge)
    titre = f"NFL {courante}"
    if semaine is None:
        compte = '<div class="compte zero">Aucun match à venir</div>'
    elif n == 0:
        compte = '<div class="compte zero">Cette semaine, aucun signal d\'edge détecté</div>'
    else:
        compte = f'<div class="compte">Cette semaine, {n} signal{"s" if n > 1 else ""} d\'edge détecté{"s" if n > 1 else ""}</div>'
    cartes = ""
    if n:
        edge = edge.sort_values("p_ajustee", ascending=False)
        cartes = '<div id="signaux">' + "".join(carte_signal(r) for r in edge.to_dict("records")) + "</div>"
    if len(autres):
        cartes += (f"<h2>Déjà dans la ligne ({len(autres)})</h2>"
                   + '<div id="signaux" class="gris">' + "".join(carte_signal(r) for r in autres.to_dict("records")) + "</div>")
    pct = lambda x: "" if pd.isna(x) else f"{x*100:.1f} %".replace(".", ",")
    tab = table_html(hist_tab, {"% gagné": pct, "Écart moyen (pts)": sgn})
    page = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(titre)}</title><style>{CSS}{CSS_MATCHS}</style></head>
<body><div class="page">
<h1>🏈 {html.escape(titre)}<span class="version">v{VERSION_PAGE} · {maj_paris()}</span></h1>
<div class="onglets">{"".join(boutons)}<button class="onglet sig-o" data-o="p-signaux">Signaux</button></div>
{"".join(panneaux)}
<div class="panneau" id="p-signaux">
{bandeau_blessures(semaine)}
{compte}
{cartes}
<h2>Historique des signaux {SAISON_DEBUT}–{courante - 1}</h2>
{tab}
</div>
</div>
<div id="fiche"><div id="fiche-boite"><button id="fiche-x">✕</button><div id="fiche-contenu"></div></div></div>
<script type="application/json" id="fiches">{json.dumps(fiches or {}, ensure_ascii=False).replace("</", "<\\/")}</script>
<script>{JS}{JS_MATCHS}</script></body></html>"""
    with open(fichier, "w", encoding="utf-8") as f:
        f.write(page)
    chemin = os.path.abspath(fichier)
    print(f"  Dashboard : {chemin}")
    if not os.environ.get("CI"):
        try:
            webbrowser.open("file://" + chemin)
        except Exception:
            pass
    return chemin


# =============================================================================
# PROGRAMME PRINCIPAL
# =============================================================================
def charger_corrections(matchs, rosters, blessures, courante):
    a_venir = matchs[(matchs["season"] == courante) & matchs["result"].isna()]
    if a_venir.empty:
        return
    CORRECTIONS["cible"] = (courante, int(a_venir["week"].min()))
    rapports = blessures[blessures["season"] == courante]["week"]
    CORRECTIONS["rapport"] = int(rapports.max()) if len(rapports) else None
    if not os.path.exists(FICHIER_ABSENCES):
        return
    with open(FICHIER_ABSENCES, encoding="utf-8") as f:
        brut = json.load(f)
    noms = rosters[rosters["season"] == courante].dropna(subset=["gsis_id"])
    noms = noms.drop_duplicates("full_name", keep="last").set_index("full_name")["gsis_id"]
    for cle in ("absents", "presents"):
        for eq_, liste in brut.get(cle, {}).items():
            for n in liste:
                if n not in noms:
                    print(f"  ⚠ {FICHIER_ABSENCES} : joueur inconnu « {n} »")
            CORRECTIONS[cle][eq_] = {noms[n] for n in liste if n in noms}


def main():
    print("1) Chargement des données nflverse…")
    matchs, pbp, snaps, rosters, blessures, courante = charger_donnees()
    print("2) Valeur des joueurs et absences…")
    joueurs = stats_joueurs(pbp, snaps, rosters)
    absents, semaines_rapport = absents_par_semaine(blessures, rosters)
    del pbp
    for r in rosters.dropna(subset=["gsis_id"]).drop_duplicates("gsis_id", keep="last").itertuples(index=False):
        noms_joueurs[r.gsis_id] = r.full_name
    charger_corrections(matchs, rosters, blessures, courante)

    a_venir = matchs[(matchs["season"] == courante) & matchs["result"].isna()]
    if len(a_venir):
        prochains = a_venir[a_venir["week"] == a_venir["week"].min()]
        precharger_meteo([g.home_team for g in prochains.itertuples(index=False)
                          if stade_exterieur(g) and str(g.location) != "Neutral"])
    print("3) Détection des signaux, semaine par semaine…")
    sig = detecter_signaux(matchs, joueurs, absents, semaines_rapport, courante)
    h = preparer_historique(sig)
    hist_tab = tableau_historique(h, courante)
    print("\n" + "=" * 100)
    print(f"HISTORIQUE DES SIGNAUX vs LIGNE DE CLÔTURE ({SAISON_DEBUT}-{courante - 1})")
    print("=" * 100)
    aff = hist_tab.copy()
    aff["% gagné"] = aff["% gagné"].map(lambda x: "" if pd.isna(x) else f"{x:.1%}")
    aff["Écart moyen (pts)"] = aff["Écart moyen (pts)"].map(lambda x: "" if pd.isna(x) else f"{x:+.2f}")
    print(aff.to_string(index=False))
    print("  Seuil de rentabilité à -110 : 52,4 %")

    print("\n4) Signaux de la semaine…")
    sem_df, semaine, aj = signaux_semaine(sig, h, courante)
    for typ, a in aj.items():
        print(f"   {TYPES[typ]:9s} : " + (f"β = {a['beta']:.2f}" if "beta" in a else f"ajustement = {a['fixe']:+.2f} pts")
              + f" (appris sur {a['n']} signaux)")
    if len(sem_df):
        for r in sem_df.sort_values("p_ajustee", ascending=False).itertuples(index=False):
            print(f"   [{TYPES[r.type]}] {r.dom} vs {r.ext} : {r.texte} | ligne {r.pari} {r.ligne:+.1f} "
                  f"| ajustement {r.ajust:.1f} pts | proba {r.p_ajustee:.1%}")
        sem_df.to_csv(f"signaux_{courante}_S{semaine}.csv", index=False)
    else:
        print("   Aucun signal.")
    h.to_csv("historique_signaux.csv", index=False)
    BETCLIC.update(charger_betclic())
    print(f"  cotes Betclic : {sum(len(v) for v in BETCLIC.values())} matchs")
    a_venir_sig = sig[(sig["season"] == courante) & ~sig["joue"]] if len(sig) else sig
    fiches = fiches_matchs(matchs, blessures, rosters, joueurs, absents, semaines_rapport, courante, semaine) if semaine else {}
    generer_html(sem_df, semaine, courante, hist_tab, matchs, a_venir_sig, fiches)


if __name__ == "__main__":
    main()
