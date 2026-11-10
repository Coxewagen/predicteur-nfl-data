#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Modèle Elo NFL v3 (+ dashboard HTML) — marge de victoire, récence, EPA saison en cours,
pénalités blessures, calibration des probabilités, backtest walk-forward.
==========================================================================
Usage :  python nfl_elo_v3.py
Dépendances : pandas, numpy, scikit-learn, matplotlib (Python 3.10+)
Données : nflverse (CSV GitHub), mis en cache dans ./nfl_cache

Convention : tout est vu depuis l'équipe à DOMICILE.
  marge = points domicile - points extérieur
  spread_line (nflverse) > 0  => le domicile est favori de ce nombre de points

Principe, semaine par semaine (aucune info future) :
  1. Elo "résultats" : 0 pour tout le monde en semaine 1, mis à jour après
     chaque semaine avec la marge (logistique) et un K décroissant.
  2. Force EPA : EPA nette par match des semaines 1..S-1, pondération exponentielle.
  3. Points marqués - encaissés, même pondération exponentielle.
  4. Malus blessures : (EPA/snap titulaire - EPA/snap remplaçant) x snap share x snaps/match.
  5. Marge attendue = combinaison linéaire (poids appris sur les saisons passées).
  6. Marge -> différence d'Elo -> probabilité brute -> calibration (isotonic / Platt).
"""

import math
import os
import sys
import time
import urllib.error
import urllib.request
import base64
import html
import io
import json
import webbrowser
from collections import defaultdict

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LinearRegression, LogisticRegression

# =============================================================================
# CONFIGURATION
# =============================================================================
SAISON_DEBUT = 2015                  # première saison utilisée (train 2015-2024 pour tester 2025)
SAISONS_BACKTEST = [2022, 2023, 2024, 2025]   # 4 saisons complètes testées en walk-forward
SAISON_TEST_PRINCIPALE = 2025                  # saison de référence pour la comparaison avant / après

# --- Elo ---
ELO_PAR_POINT = 25                   # 25 points d'Elo = 1 point de marge (convention 538)
ELO_AVANTAGE_DOM = 2.0               # avantage domicile (en points) dans l'Elo
ECHELLE_LOGISTIQUE = 7.0             # résultat = 1/(1+exp(-marge/7)) : +1 -> 0.54, +25 -> 0.97
K_DEBUT = 120                        # K en semaine 1 (beaucoup d'incertitude)
K_FIN = 40                           # K plancher en fin de saison
K_DECROISSANCE = 4.0                 # vitesse de baisse du K (en semaines)

# --- Récence ---
DEMI_VIE_SEMAINES = 3.0              # un match d'il y a 3 semaines compte 2x moins que le dernier

# --- Joueurs / blessures ---
POSTES_TITULAIRES = {"QB": 1, "RB": 1, "WR": 3, "TE": 1}
PRIOR_SNAPS = 60                     # rétrécissement de l'EPA/snap vers la moyenne du poste
STATUTS_BLESSURE_ABSENT = {"Out", "Doubtful"}
STATUTS_ROSTER_ABSENT = {"RES"}      # réserve / IR

# --- Calibration ---
CALIBRATION = "isotonic"             # calibration du modèle actuel (l'ancien modèle reste en Platt)
PROBA_MIN, PROBA_MAX = 0.30, 0.75    # plafond de confiance sur la proba de victoire du domicile
SEUIL_CONFIANCE_2 = 0.60             # second seuil pour le % ATS
SEUIL_CONFIANCE = 0.55               # pour le % ATS "quand P >= 55 %"

# Corrections manuelles des absences de la prochaine semaine (fichier facultatif) :
# {"absents": {"BAL": ["Lamar Jackson"]}, "presents": {"MIN": ["Kyler Murray"]}}
FICHIER_ABSENCES = "absences.json"
CORRECTIONS = {"absents": {}, "presents": {}}   # rempli au démarrage (noms -> gsis_id)

DOSSIER_CACHE = "nfl_cache"
CACHE_SAISON_EN_COURS_H = 12

URL_MATCHS = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
URL_REL = "https://github.com/nflverse/nflverse-data/releases/download/"
URL_PBP = URL_REL + "pbp/play_by_play_{s}.csv.gz"
URL_SNAPS = URL_REL + "snap_counts/snap_counts_{s}.csv"
URL_ROSTERS = URL_REL + "weekly_rosters/roster_weekly_{s}.csv"
URL_BLESSURES = URL_REL + "injuries/injuries_{s}.csv"

RENOMMAGE = {"STL": "LA", "SD": "LAC", "OAK": "LV", "LAR": "LA"}
COLONNES_PBP = ["game_id", "season", "week", "posteam", "defteam", "play_type", "epa",
                "qb_epa", "qb_dropback", "id", "passer_player_id",
                "rusher_player_id", "receiver_player_id"]
FEATURES = ["elo_diff_pts", "epa_diff", "points_diff", "malus_diff", "domicile"]
FEATURES_TOTAL = ["pf_somme", "pa_somme"]   # points marqués / encaissés pondérés des 2 équipes
POINTS_MOYENS = 22.0                         # valeur par défaut en semaine 1 (aucune donnée)
SEUIL_RENTABILITE = 0.524                    # pari à -110 (cote 1.91)
FICHIER_HTML = "dashboard_nfl.html"


# =============================================================================
# 1. TÉLÉCHARGEMENT (cache local)
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


# =============================================================================
# 2. PRÉPARATION : stats par équipe-semaine et par joueur-semaine
# =============================================================================
def stats_equipes(pbp, matchs):
    """Par (saison, semaine, équipe) : EPA offensive et défensive totales, points pour/contre."""
    jeu = pbp[pbp["play_type"].isin(["pass", "run"]) & pbp["epa"].notna()]
    off = jeu.groupby(["season", "week", "posteam"])["epa"].sum().rename("epa_off")
    dfn = jeu.groupby(["season", "week", "defteam"])["epa"].sum().rename("epa_def")
    off.index.names = dfn.index.names = ["season", "week", "team"]
    eq = pd.concat([off, dfn], axis=1)

    joues = matchs[matchs["result"].notna()]
    dom = joues[["season", "week", "home_team", "home_score", "away_score"]].set_axis(
        ["season", "week", "team", "pf", "pa"], axis=1)
    ext = joues[["season", "week", "away_team", "away_score", "home_score"]].set_axis(
        ["season", "week", "team", "pf", "pa"], axis=1)
    pts = pd.concat([dom, ext]).set_index(["season", "week", "team"])
    return eq.join(pts, how="inner").reset_index()


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


# =============================================================================
# 3. OUTILS
# =============================================================================
def poids_recence(semaines, semaine_courante):
    """Pondération exponentielle : poids 1 pour la semaine précédente, /2 toutes les 3 semaines."""
    return np.power(0.5, (semaine_courante - 1 - np.asarray(semaines, dtype=float)) / DEMI_VIE_SEMAINES)


def k_semaine(semaine):
    """K élevé en début de saison, décroissant vers K_FIN."""
    return K_FIN + (K_DEBUT - K_FIN) * math.exp(-(semaine - 1) / K_DECROISSANCE)


def logistique(marge):
    return 1.0 / (1.0 + math.exp(-marge / ECHELLE_LOGISTIQUE))


def maj_elo(elo, dom, ext, marge, neutre, semaine):
    """gain = K x (résultat réel - résultat attendu), les deux en logistique de la marge."""
    marge_attendue = (elo[dom] - elo[ext]) / ELO_PAR_POINT + (0 if neutre else ELO_AVANTAGE_DOM)
    gain = k_semaine(semaine) * (logistique(marge) - logistique(marge_attendue))
    elo[dom] += gain
    elo[ext] -= gain


def proba_elo(diff_elo):
    """Différence d'Elo -> probabilité brute de victoire (formule Elo classique)."""
    return 1.0 / (1.0 + np.power(10.0, -np.asarray(diff_elo) / 400.0))


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def proba_moneyline(ml):
    if pd.isna(ml):
        return np.nan
    return -ml / (-ml + 100) if ml < 0 else 100 / (ml + 100)


# =============================================================================
# 4. FORCES D'ÉQUIPE ET MALUS BLESSURES À UNE SEMAINE DONNÉE
# =============================================================================
def forces_equipes(eq_saison, semaine):
    """EPA nette et points (pour - contre) par match, pondérés, semaines 1..semaine-1."""
    passe = eq_saison[eq_saison["week"] < semaine]
    if passe.empty:
        return {}
    w = poids_recence(passe["week"], semaine)
    p = passe.assign(w=w, epa_net=passe["epa_off"] - passe["epa_def"], pts=passe["pf"] - passe["pa"])
    res = {}
    for t, g in p.groupby("team"):
        res[t] = (np.average(g["epa_net"], weights=g["w"]), np.average(g["pts"], weights=g["w"]),
                  np.average(g["pf"], weights=g["w"]), np.average(g["pa"], weights=g["w"]))
    return res


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


def malus_equipe(equipe, v, moy_poste, snaps_eq, absents):
    """
    Malus (en points) = Σ (EPA/snap titulaire - EPA/snap remplaçant) x snap share x snaps/match
    pour chaque titulaire absent. Remplaçant = suivant au même poste, non blessé ;
    à défaut (rookie, aucun joueur) = moyenne du poste.
    """
    if v is None or len(v) == 0:
        return 0.0, []
    total, details = 0.0, []
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
                rempl = reserve.pop(0)
                epa_r, nom_r = rempl.epa_snap, rempl.nom
            else:
                epa_r, nom_r = moy_poste.get(poste, 0.0), f"moyenne {poste}"
            # Une absence ne peut pas rendre l'équipe meilleure : malus plancher à 0
            m = max(0.0, (t.epa_snap - epa_r) * t.share * snaps)
            total += m
            if m >= 0.1:                    # on n'affiche que les absences qui coûtent
                    details.append(f"{t.nom}→{nom_r} ({m:+.1f})")
    return total, details


# =============================================================================
# 5. CONSTRUCTION DES FEATURES SEMAINE PAR SEMAINE
# =============================================================================
def construire_features(matchs, eq, joueurs, absents, semaines_rapport):
    lignes = []
    for saison, ms in matchs.groupby("season"):
        elo = defaultdict(float)                     # tout le monde part de 0
        eq_s = eq[eq["season"] == saison]
        j_s = joueurs[joueurs["season"] == saison]
        for semaine, mw in ms.groupby("week"):
            forces = forces_equipes(eq_s, semaine)
            v, moy_poste, snaps_eq = valeurs_joueurs(j_s, semaine)
            for g in mw.itertuples(index=False):
                neutre = str(g.location) == "Neutral"
                feats = {}
                for cote, t in (("dom", g.home_team), ("ext", g.away_team)):
                    abs_t = absents.get((saison, semaine, t), set())
                    # Semaine future sans rapport publié : on reprend les absents de S-1
                    if (saison, semaine) not in semaines_rapport:
                        abs_t = abs_t | absents.get((saison, semaine - 1, t), set())
                    # Corrections manuelles (uniquement pour la semaine à venir)
                    if (saison, semaine) == CORRECTIONS.get("cible"):
                        abs_t = (abs_t | CORRECTIONS["absents"].get(t, set())) - CORRECTIONS["presents"].get(t, set())
                    m, det = malus_equipe(t, v, moy_poste, snaps_eq, abs_t)
                    epa_net, pts, pf, pa = forces.get(t, (0.0, 0.0, POINTS_MOYENS, POINTS_MOYENS))
                    feats[cote] = dict(elo=elo[t], epa=epa_net, pts=pts, pf=pf, pa=pa, malus=m, det=det)
                d, e = feats["dom"], feats["ext"]
                lignes.append({
                    "game_id": g.game_id, "season": saison, "week": semaine,
                    "dom": g.home_team, "ext": g.away_team,
                    "elo_dom": d["elo"], "elo_ext": e["elo"],
                    "elo_diff_pts": (d["elo"] - e["elo"]) / ELO_PAR_POINT,
                    "epa_diff": d["epa"] - e["epa"],
                    "points_diff": d["pts"] - e["pts"],
                    "malus_dom": d["malus"], "malus_ext": e["malus"],
                    "malus_diff": -(d["malus"] - e["malus"]),    # positif = avantage domicile
                    "absents_dom": "; ".join(d["det"]), "absents_ext": "; ".join(e["det"]),
                    "domicile": 0 if neutre else 1,
                    "pf_somme": d["pf"] + e["pf"], "pa_somme": d["pa"] + e["pa"],
                    "marge": g.result, "total": g.total,
                    "spread_line": g.spread_line, "total_line": g.total_line,
                    "ml_dom": g.home_moneyline, "ml_ext": g.away_moneyline,
                })
            # Mise à jour Elo APRÈS la semaine (les matchs d'une même semaine ne s'influencent pas)
            for g in mw.itertuples(index=False):
                if not pd.isna(g.result):
                    maj_elo(elo, g.home_team, g.away_team, g.result,
                            str(g.location) == "Neutral", semaine)
    return pd.DataFrame(lignes)


# =============================================================================
# 6. MODÈLE : poids des composantes + calibration (appris sur le passé uniquement)
# =============================================================================
class ModeleElo:
    """
    Modèle v4 :
      1. marge "modèle" = combinaison des composantes Elo/EPA/points/blessures
      2. feature écart = marge modèle - spread du book ; marge finale = f(marge modèle, écart)
      3. marge -> diff Elo -> proba brute -> calibration isotonic (apprise hors échantillon)
      4. proba de victoire plafonnée dans [PROBA_MIN, PROBA_MAX]
      5. proba de couvrir le spread calibrée elle aussi (isotonic)
    """

    @staticmethod
    def _x2(m1, spread):
        spread = np.asarray(spread, dtype=float)
        ecart = np.where(np.isnan(spread), 0.0, np.asarray(m1) - spread)   # pas de spread -> écart nul
        return np.column_stack([m1, ecart])

    def fit(self, train):
        train = train.reset_index(drop=True)
        # 1) Marge modèle hors échantillon : chaque saison prédite par un modèle appris sans elle
        m1_oof = np.full(len(train), np.nan)
        for s_ in train["season"].unique():
            m = (train["season"] == s_).to_numpy()
            if m.all():
                continue
            r = LinearRegression(positive=True).fit(train.loc[~m, FEATURES], train.loc[~m, "marge"])
            m1_oof[m] = r.predict(train.loc[m, FEATURES])
        self.reg = LinearRegression(positive=True).fit(train[FEATURES], train["marge"])
        m1_oof = np.where(np.isnan(m1_oof), self.reg.predict(train[FEATURES]), m1_oof)

        # 2) Marge finale avec l'écart vs spread : le modèle apprend quand sa divergence est fiable
        x2 = self._x2(m1_oof, train["spread_line"])
        self.reg2 = LinearRegression().fit(x2, train["marge"])
        m2 = self.reg2.predict(x2)
        self.sigma = float(np.std(train["marge"] - m2, ddof=1))

        # 3) Calibration isotonic de la proba de victoire
        p_brute = proba_elo(m2 * ELO_PAR_POINT)
        ok = (train["marge"] != 0).to_numpy()
        self.cal = IsotonicRegression(out_of_bounds="clip", y_min=0.01, y_max=0.99)
        self.cal.fit(p_brute[ok], (train["marge"] > 0).to_numpy()[ok].astype(int))

        # 4) Calibration isotonic de la proba de couvrir le spread
        sp = train["spread_line"].to_numpy(dtype=float)
        okc = ~np.isnan(sp) & (train["marge"].to_numpy() != sp)
        pc_brut = 1 - np.array([phi(x) for x in (sp[okc] - m2[okc]) / self.sigma])
        self.cal_couvre = IsotonicRegression(out_of_bounds="clip", y_min=0.01, y_max=0.99)
        self.cal_couvre.fit(pc_brut, (train["marge"].to_numpy()[okc] > sp[okc]).astype(int))

        # Modèle du total (inchangé)
        tr = train[train["total"].notna()]
        self.reg_total = LinearRegression().fit(tr[FEATURES_TOTAL], tr["total"])
        self.sigma_total = float(np.std(tr["total"] - self.reg_total.predict(tr[FEATURES_TOTAL]), ddof=1))
        return self

    def predict(self, df):
        out = df.copy()
        m1 = self.reg.predict(df[FEATURES])
        out["marge_modele"] = m1
        out["ecart_spread"] = m1 - df["spread_line"]
        out["pred_marge"] = self.reg2.predict(self._x2(m1, df["spread_line"]))
        out["diff_elo_total"] = out["pred_marge"] * ELO_PAR_POINT
        out["p_brute"] = proba_elo(out["diff_elo_total"])
        out["p_victoire"] = np.clip(self.cal.predict(out["p_brute"]), PROBA_MIN, PROBA_MAX)
        pc_brut = [np.nan if pd.isna(sp) else 1 - phi((sp - m) / self.sigma)
                   for m, sp in zip(out["pred_marge"], out["spread_line"])]
        out["p_couvre_brute"] = pc_brut
        out["p_couvre"] = [np.nan if pd.isna(x) else float(self.cal_couvre.predict([x])[0]) for x in pc_brut]
        out["pred_total"] = self.reg_total.predict(df[FEATURES_TOTAL])
        out["p_over"] = [np.nan if pd.isna(l) else 1 - phi((l - t) / self.sigma_total)
                         for t, l in zip(out["pred_total"], out["total_line"])]
        p_d = out["ml_dom"].apply(proba_moneyline)
        p_e = out["ml_ext"].apply(proba_moneyline)
        out["p_book"] = p_d / (p_d + p_e)
        return out

    def params_js(self):
        """Paramètres pour recalculer dans la page quand on modifie le spread."""
        return {"c0": float(self.reg2.intercept_), "a": float(self.reg2.coef_[0]), "b": float(self.reg2.coef_[1]),
                "sigma": self.sigma, "elo_pt": ELO_PAR_POINT, "pmin": PROBA_MIN, "pmax": PROBA_MAX,
                "calw": [list(map(float, self.cal.X_thresholds_)), list(map(float, self.cal.y_thresholds_))],
                "calc": [list(map(float, self.cal_couvre.X_thresholds_)), list(map(float, self.cal_couvre.y_thresholds_))]}


class ModeleEloAvant:
    """Ancien modèle (v3) : Platt, sans écart vs spread, sans plafond — gardé pour comparaison."""
    def fit(self, train):
        """Apprend les poids (positifs) des composantes, l'écart-type et la calibration."""
        self.reg = LinearRegression(positive=True).fit(train[FEATURES], train["marge"])
        marge_pred = self.reg.predict(train[FEATURES])
        self.sigma = float(np.std(train["marge"] - marge_pred, ddof=1))
        p_brute = proba_elo(marge_pred * ELO_PAR_POINT)
        # Modèle du total (over/under)
        tr = train[train["total"].notna()]
        self.reg_total = LinearRegression().fit(tr[FEATURES_TOTAL], tr["total"])
        self.sigma_total = float(np.std(tr["total"] - self.reg_total.predict(tr[FEATURES_TOTAL]), ddof=1))
        y = (train["marge"] > 0).astype(int)
        ok = train["marge"] != 0                       # on retire les nuls
        if True:
            x = np.log(p_brute / (1 - p_brute)).reshape(-1, 1)
            self.cal = LogisticRegression().fit(x[ok], y[ok])
        else:
            self.cal = IsotonicRegression(out_of_bounds="clip", y_min=0.01, y_max=0.99)
            self.cal.fit(p_brute[ok], y[ok])
        return self

    def predict(self, df):
        out = df.copy()
        out["pred_marge"] = self.reg.predict(df[FEATURES])
        out["diff_elo_total"] = out["pred_marge"] * ELO_PAR_POINT
        out["p_brute"] = proba_elo(out["diff_elo_total"])
        if True:
            x = np.log(out["p_brute"] / (1 - out["p_brute"])).to_numpy().reshape(-1, 1)
            out["p_victoire"] = self.cal.predict_proba(x)[:, 1]
        else:
            out["p_victoire"] = self.cal.predict(out["p_brute"])
        # Probabilité que le domicile couvre le spread (loi normale autour de la marge prédite)
        out["p_couvre"] = [np.nan if pd.isna(s) else 1 - phi((s - m) / self.sigma)
                           for m, s in zip(out["pred_marge"], out["spread_line"])]
        out["pred_total"] = self.reg_total.predict(df[FEATURES_TOTAL])
        out["p_over"] = [np.nan if pd.isna(l) else 1 - phi((l - t) / self.sigma_total)
                         for t, l in zip(out["pred_total"], out["total_line"])]
        p_d = out["ml_dom"].apply(proba_moneyline)
        p_e = out["ml_ext"].apply(proba_moneyline)
        out["p_book"] = p_d / (p_d + p_e)              # proba bookmaker sans marge
        return out


# =============================================================================
# 7. BACKTEST WALK-FORWARD
# =============================================================================
def metriques(t):
    issue = np.where(t["marge"] > 0, 1.0, np.where(t["marge"] < 0, 0.0, 0.5))
    a = t[t["spread_line"].notna() & (t["marge"] != t["spread_line"])]
    pick_dom = a["p_couvre"] >= 0.5                                   # côté que le modèle préfère
    gagne = pick_dom == (a["marge"] > a["spread_line"])
    p_cote = np.where(pick_dom, a["p_couvre"], 1 - a["p_couvre"])   # proba du côté choisi
    conf = p_cote >= SEUIL_CONFIANCE
    conf2 = p_cote >= SEUIL_CONFIANCE_2
    ok_b = t["p_book"].notna()
    o = t[t["total_line"].notna() & (t["total"] != t["total_line"])]
    ou = np.mean((o["pred_total"] > o["total_line"]) == (o["total"] > o["total_line"]))
    return {
        "Matchs": len(t),
        "MAE modèle": np.mean(np.abs(t["marge"] - t["pred_marge"])),
        "MAE book": np.mean(np.abs(t["marge"] - t["spread_line"])),
        "Brier brut": np.mean((t["p_brute"] - issue) ** 2),
        "Brier calibré": np.mean((t["p_victoire"] - issue) ** 2),
        "Brier book": np.mean((t["p_book"][ok_b] - issue[ok_b]) ** 2),
        "% ATS": gagne.mean(),
        "Paris P≥55%": int(conf.sum()),
        "% ATS P≥55%": gagne[conf].mean() if conf.any() else np.nan,
        "Paris P≥60%": int(conf2.sum()),
        "% ATS P≥60%": gagne[conf2].mean() if conf2.any() else np.nan,
        "% O/U": ou,
    }


def _mise_en_forme(tab):
    tab = tab.copy()
    for c in ("MAE modèle", "MAE book"):
        tab[c] = tab[c].astype(float).round(2)
    for c in ("Brier brut", "Brier calibré", "Brier book"):
        tab[c] = tab[c].astype(float).round(4)
    for c in ("% ATS", "% ATS P≥55%", "% ATS P≥60%", "% O/U"):
        tab[c] = tab[c].astype(float).map(lambda x: "" if pd.isna(x) else f"{x:.1%}")
    for c in ("Matchs", "Paris P≥55%", "Paris P≥60%"):
        tab[c] = tab[c].astype(int)
    return tab


def backtest(df):
    """Walk-forward : chaque saison testée avec un modèle appris sur les saisons d'avant (2025 -> 2015-2024)."""
    res_new, res_old = [], []
    for s_ in SAISONS_BACKTEST:
        train = df[(df["season"] < s_) & df["marge"].notna()]
        test = df[(df["season"] == s_) & df["marge"].notna()]
        res_new.append(ModeleElo().fit(train).predict(test))
        res_old.append(ModeleEloAvant().fit(train).predict(test))
    tout, ancien = pd.concat(res_new), pd.concat(res_old)

    lignes = {str(s_): metriques(tout[tout["season"] == s_]) for s_ in SAISONS_BACKTEST}
    lignes["TOTAL"] = metriques(tout)
    lignes["TOTAL sem. ≥ 4"] = metriques(tout[tout["week"] >= 4])
    tab = _mise_en_forme(pd.DataFrame(lignes).T)

    s25 = SAISON_TEST_PRINCIPALE
    comp = _mise_en_forme(pd.DataFrame({
        f"{s25} avant": metriques(ancien[ancien["season"] == s25]),
        f"{s25} après": metriques(tout[tout["season"] == s25]),
        "2022-25 avant": metriques(ancien),
        "2022-25 après": metriques(tout),
    }).T)

    print("\n" + "=" * 110)
    print("BACKTEST WALK-FORWARD — modèle calibré (isotonic + écart vs spread + plafond)")
    print("=" * 110)
    print(tab.to_string())
    print("\n" + "=" * 110)
    print("COMPARAISON AVANT / APRÈS")
    print("=" * 110)
    print(comp.to_string())
    t25 = tout[tout["season"] == s25].copy()
    a = t25[t25["spread_line"].notna() & (t25["marge"] != t25["spread_line"])]
    p_cote = np.maximum(a["p_couvre"], 1 - a["p_couvre"])
    r60 = ((a["p_couvre"] >= 0.5) == (a["marge"] > a["spread_line"]))[p_cote >= 0.60]
    verdict = "EXPLOITABLE" if len(r60) and r60.mean() > 0.55 else "PAS EXPLOITABLE"
    print(f"\n  % ATS {s25} quand P ≥ 60 % : {r60.mean():.1%} sur {len(r60)} paris -> {verdict} (seuil 55 %)")
    print("  Calibration victoire " + str(s25) + " (proba prédite -> fréquence réelle) :")
    t = t25[t25["marge"] != 0]
    for lo, hi in ((0.3, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.76)):
        m = (t["p_victoire"] >= lo) & (t["p_victoire"] < hi)
        if m.sum():
            print(f"    {lo:.0%}-{min(hi,0.75):.0%} : prédit {t['p_victoire'][m].mean():.1%} / réel {(t['marge'][m] > 0).mean():.1%} ({m.sum()} matchs)")
    print("  Seuil de rentabilité ATS à -110 : 52,4 %")
    return tout, tab, comp


def ml_vers_decimale(ml):
    """Cote américaine -> cote décimale (ex. -150 -> 1.67, +130 -> 2.30)."""
    if pd.isna(ml):
        return np.nan
    return 1 + 100 / -ml if ml < 0 else 1 + ml / 100


# -----------------------------------------------------------------------------
# Graphiques matplotlib (thème sombre) -> PNG en base64
# -----------------------------------------------------------------------------
FOND = "#1a1a2e"
CARTE = "#22223b"
TEXTE = "#ffffff"
ACCENT = "#4cc9f0"
VERT, ORANGE, ROUGE = "#2ecc71", "#f39c12", "#e74c3c"


def style_sombre():
    plt.rcParams.update({
        "font.size": 15, "figure.facecolor": FOND, "axes.facecolor": CARTE,
        "axes.edgecolor": "#8888aa", "axes.labelcolor": TEXTE, "text.color": TEXTE,
        "xtick.color": TEXTE, "ytick.color": TEXTE, "grid.color": "#3a3a5a",
        "axes.grid": True, "axes.titleweight": "bold",
    })


def fig_base64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def graphique_calibration(tout):
    style_sombre()
    t = tout[tout["marge"] != 0]
    y = (t["marge"] > 0).astype(float).to_numpy()
    p = t["p_victoire"].to_numpy()
    bins = np.linspace(0, 1, 11)
    idx = np.clip(np.digitize(p, bins) - 1, 0, 9)
    xs, ys, ns = [], [], []
    for b in range(10):
        m = idx == b
        if m.sum() >= 10:
            xs.append(p[m].mean()); ys.append(y[m].mean()); ns.append(m.sum())
    fig, ax = plt.subplots(figsize=(7.5, 6.5))
    ax.plot([0, 1], [0, 1], ":", color="#aaaacc", lw=2)
    ax.plot(xs, ys, "o-", color=ACCENT, lw=3, ms=10)
    for x, yy, n in zip(xs, ys, ns):
        ax.annotate(str(n), (x, yy), textcoords="offset points", xytext=(0, 12),
                    ha="center", fontsize=12, color="#ccccee")
    ax.set(xlim=(0, 1), ylim=(0, 1), xlabel="Proba prédite", ylabel="Fréquence réelle",
           title="Calibration")
    return fig_base64(fig)


def graphique_marges(tout):
    style_sombre()
    fig, ax = plt.subplots(figsize=(7.5, 6.5))
    ax.scatter(tout["pred_marge"], tout["marge"], s=16, alpha=0.45, color=ACCENT)
    ax.plot([-30, 30], [-30, 30], ":", color="#aaaacc", lw=2)
    ax.set(xlabel="Marge prédite", ylabel="Marge réelle", title="Marge prédite vs réelle")
    return fig_base64(fig)


def graphique_elo(df, saison):
    """Une petite courbe par équipe (32 vignettes, triées par Elo actuel)."""
    style_sombre()
    s = df[df["season"] == saison]
    # On s'arrête à la prochaine semaine à jouer (pas de semaines futures plates)
    joues = s[s["marge"].notna()]
    if not joues.empty:
        s = s[s["week"] <= joues["week"].max() + 1]
    histo = pd.concat([
        s[["week", "dom", "elo_dom"]].set_axis(["week", "team", "elo"], axis=1),
        s[["week", "ext", "elo_ext"]].set_axis(["week", "team", "elo"], axis=1),
    ]).sort_values("week")
    dernier = histo.groupby("team")["elo"].last().sort_values(ascending=False)
    equipes = list(dernier.index)
    ncol = 4
    nlig = math.ceil(len(equipes) / ncol)
    fig, axes = plt.subplots(nlig, ncol, figsize=(14, 2.3 * nlig), sharex=True, sharey=True)
    ymin, ymax = histo["elo"].min() - 20, histo["elo"].max() + 20
    for ax, t in zip(axes.flat, equipes):
        h = histo[histo["team"] == t]
        c = VERT if dernier[t] > 0 else ROUGE
        ax.axhline(0, color="#8888aa", lw=1)
        ax.plot(h["week"], h["elo"], "-o", color=c, lw=2.5, ms=4)
        ax.set_title(f"{t}  {dernier[t]:+.0f}", fontsize=15, color=TEXTE)
        ax.set_ylim(ymin, ymax)
        ax.tick_params(labelsize=11)
    for ax in list(axes.flat)[len(equipes):]:
        ax.axis("off")
    fig.suptitle(f"Elo {saison}", fontsize=20, fontweight="bold", color=TEXTE)
    fig.tight_layout()
    return fig_base64(fig)


# -----------------------------------------------------------------------------
# Génération du HTML autonome
# -----------------------------------------------------------------------------
SEUIL_PARI = 0.55          # proba minimale pour conseiller un pari handicap / total
SEUIL_EV_ML = 0.05         # espérance de gain minimale pour conseiller un pari "vainqueur"

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#1a1a2e;color:#fff;font-family:Segoe UI,Roboto,Arial,sans-serif;font-size:18px}
.page{max-width:1300px;margin:auto;padding:16px}
h1{font-size:30px;margin:6px 0 16px}
h2{font-size:25px;margin:34px 0 14px;border-left:6px solid #4cc9f0;padding-left:12px}
.tab{overflow-x:auto;background:#22223b;border-radius:12px;padding:6px}
table{border-collapse:collapse;width:100%}
th{background:#2d2d50;font-size:16px;text-align:left;padding:12px 10px;white-space:nowrap}
td{padding:11px 10px;border-top:1px solid #3a3a5a;font-size:18px}
.num{font-variant-numeric:tabular-nums;font-weight:700}
.match{font-weight:700;white-space:nowrap}
/* --- Paris de la semaine --- */
#paris{display:grid;gap:12px}
.pari{display:flex;align-items:center;gap:16px;background:#1e5631;border:2px solid #2ecc71;border-radius:14px;padding:14px 18px}
.sel{background:#16324f;border-color:#4cc9f0}
.sel .rang{color:#9ad6f0}
.pari .rang{font-size:30px;font-weight:800;color:#9ff5b8;min-width:40px}
.pari .quoi{font-size:26px;font-weight:800}
.pari .type{font-size:16px;color:#c8f5d6}
.pari .chiffres{margin-left:auto;text-align:right;font-size:19px;font-weight:700;white-space:nowrap}
.aucun{background:#3a3a5a;border-radius:14px;padding:18px;font-size:22px;font-weight:700;text-align:center}
.histo{display:flex;flex-wrap:wrap;gap:10px;margin-top:12px}
.histo div{background:#22223b;border-radius:10px;padding:9px 14px;font-size:16px}
.histo b{font-size:19px}
.bon{color:#2ecc71}.mauvais{color:#ff6b6b}
/* --- Cartes match --- */
#cartes{display:grid;grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:16px}
.carte{background:#22223b;border-radius:16px;padding:16px;border:2px solid #33335a}
.carte.conseil{border-color:#2ecc71}
.entete{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:10px}
.equipes{font-size:26px;font-weight:800}
.elo{font-size:15px;color:#c8c8e0}
.ligne{display:grid;grid-template-columns:105px 1fr auto;align-items:center;gap:10px;
       padding:10px 0;border-top:1px solid #3a3a5a}
.ligne .lib{font-size:15px;color:#9ad6f0;font-weight:700}
.choix{font-size:21px;font-weight:800}
.reglage{font-size:14px;color:#c8c8e0;margin-top:5px}
.verdict{font-weight:800;font-size:17px;padding:7px 12px;border-radius:10px;white-space:nowrap}
.jouer{background:#2ecc71;color:#0b2914}.passer{background:#3a3a5a;color:#d0d0e0}
.barre{position:relative;background:#3a3a5a;border-radius:8px;height:26px;margin-top:5px;overflow:hidden}
.barre span{position:absolute;left:0;top:0;bottom:0}
.barre b{position:relative;display:block;text-align:center;line-height:26px;font-size:16px;text-shadow:0 0 4px #000}
.vert{background:#2ecc71}.orange{background:#f39c12}.rouge{background:#e74c3c}
input{width:78px;background:#11111f;color:#fff;border:2px solid #4cc9f0;border-radius:8px;
      padding:5px 6px;font-size:17px;font-weight:700}
.blesse{font-size:15px;color:#ffcf70;padding-top:8px;border-top:1px solid #3a3a5a}
.graph{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.graph img,.full img{width:100%;border-radius:12px}
.full{margin-top:16px}
@media (max-width:820px){
  body{font-size:17px}
  .graph{grid-template-columns:1fr}
  #cartes{grid-template-columns:1fr}
  .pari{flex-wrap:wrap}.pari .chiffres{margin-left:0;text-align:left}
  .pari .quoi{font-size:23px}
}
"""

JS = """
const SEUIL=%SEUIL%, SEUIL_EV=%SEUIL_EV%, RENTA=%RENTA%;
const M=JSON.parse(document.getElementById('donnees').textContent);
const H=JSON.parse(document.getElementById('histo').textContent);
function tranche(cle,x){for(const t of H[cle]){if(x>=t.lo&&x<t.hi)return t}return null}
function histoTxt(cle,t){if(!t||t.v===null)return '';const v=cle==='ml'?sg((t.v*100).toFixed(1))+' %':pc(t.v);
 return 'Historique : <b class="'+(t.ok?'bon':'mauvais')+'">'+v+'</b> ('+t.n+' paris)'}
function erf(x){const s=Math.sign(x);x=Math.abs(x);const t=1/(1+0.3275911*x);
 return s*(1-(((((1.061405429*t-1.453152027)*t)+1.421413741)*t-0.284496736)*t+0.254829592)*t*Math.exp(-x*x))}
function phi(x){return 0.5*(1+erf(x/Math.SQRT2))}
function pc(p){return (p*100).toFixed(1).replace('.',',')+' %'}
function sg(x){return (x>0?'+':'')+String(x).replace('.',',')}
function barre(p){const c=p>0.55?'vert':(p>=0.5?'orange':'rouge');
 return '<div class="barre"><span class="'+c+'" style="width:'+(p*100).toFixed(1)+'%"></span><b>'+pc(p)+'</b></div>'}
function verdict(ok){return '<span class="verdict '+(ok?'jouer':'passer')+'">'+(ok?'✅ JOUER':'❌ PASSER')+'</span>'}
function val(i,cl){return parseFloat(document.querySelector('#c'+i+' .'+cl).value.replace(',','.'))}
const P=JSON.parse(document.getElementById('params').textContent);
function interp(x,c){const X=c[0],Y=c[1];if(x<=X[0])return Y[0];if(x>=X[X.length-1])return Y[Y.length-1];
 let k=1;while(X[k]<x)k++;const t=(x-X[k-1])/((X[k]-X[k-1])||1);return Y[k-1]+t*(Y[k]-Y[k-1])}
// Recalcule tout depuis le handicap saisi : écart vs spread -> marge finale -> probas calibrées
function etat(m,i){
 const h=val(i,'in-hand'), sp=isNaN(h)?NaN:-h, ec=isNaN(sp)?0:m.m1-sp;
 const marge=P.c0+P.a*m.m1+P.b*ec;
 const pb=1/(1+Math.pow(10,-marge*P.elo_pt/400));
 const pvic=Math.min(P.pmax,Math.max(P.pmin,interp(pb,P.calw)));
 const pcouv=isNaN(sp)?NaN:interp(1-phi((sp-marge)/P.sigma),P.calc);
 return {marge:marge,pvic:pvic,pcouv:pcouv};
}

function analyse(m,i){
 const r={i:i,paris:[]};
 // Vainqueur (moneyline)
 const E=etat(m,i), cd=val(i,'in-cd'), ce=val(i,'in-ce'), pd=E.pvic;
 const fav=pd>=0.5?m.dom:m.ext, pf=Math.max(pd,1-pd);
 r.vain={fav:fav,p:pf};
 if(!isNaN(cd)&&!isNaN(ce)){
   const evd=pd*cd-1, eve=(1-pd)*ce-1, dom=evd>=eve;
   r.ml={eq:dom?m.dom:m.ext, cote:dom?cd:ce, p:dom?pd:1-pd, ev:dom?evd:eve};
   r.ml.t=tranche('ml',r.ml.ev); r.ml.ok=!!(r.ml.t&&r.ml.t.ok);
   if(r.ml.ok) r.paris.push({quoi:r.ml.eq+' gagne', type:'Vainqueur du match', p:r.ml.p,
                              detail:'cote '+String(r.ml.cote).replace('.',',')+' · historique '+sg((r.ml.t.v*100).toFixed(1))+' %', score:r.ml.ev});
 }
 // Handicap : saisi comme le handicap de l'équipe à domicile (ex. -2.5 = domicile favori de 2,5)
 const h=val(i,'in-hand');
 if(!isNaN(h)){
   const pdom=E.pcouv;                                // proba calibrée que le domicile couvre
   const dom=pdom>=0.5, p=dom?pdom:1-pdom, hh=dom?h:-h;
   const ta=tranche('hand',p); r.ats={eq:dom?m.dom:m.ext, hand:hh, p:p, t:ta, ok:!!(ta&&ta.ok)};
   if(r.ats.ok) r.paris.push({quoi:r.ats.eq+' '+sg(hh), type:'Handicap', p:p,
                              detail:'historique '+pc(ta.v), score:p-RENTA});
 }
 // Total de points
 const t=val(i,'in-tot');
 if(!isNaN(t)){
   const po=1-phi((t-m.total)/m.sigmat), ov=po>=0.5, p=ov?po:1-po;
   const tt=tranche('tot',p); r.tot={sens:ov?'Plus de':'Moins de', ligne:t, p:p, t:tt, ok:!!(tt&&tt.ok)};
   if(r.tot.ok) r.paris.push({quoi:(ov?'Plus de ':'Moins de ')+String(t).replace('.',',')+' pts', type:'Total de points ('+m.dom+' vs '+m.ext+')', p:p,
                              detail:'historique '+pc(tt.v), score:p-RENTA});
 }
 return r;
}

function rendCarte(m,i,r){
 const el=document.querySelector('#c'+i+' .calc');
 let h='';
 h+='<div class="ligne"><div class="lib">Vainqueur</div><div><div class="choix">'+r.vain.fav+'</div>'+barre(r.vain.p)
   +(r.ml?'<div class="reglage">'+(r.ml.ev>0?'Valeur sur '+r.ml.eq+' (cote '+String(r.ml.cote).replace('.',',')+') · EV '+sg((r.ml.ev*100).toFixed(1))+' %'+(r.ml.t?' · '+histoTxt('ml',r.ml.t):''):'Pas de valeur')+'</div>':'')
   +'</div>'+verdict(r.ml&&r.ml.ok)+'</div>';
 if(r.ats) h+='<div class="ligne"><div class="lib">Handicap</div><div><div class="choix">'+r.ats.eq+' '+sg(r.ats.hand)+'</div>'+barre(r.ats.p)+'<div class="reglage">'+histoTxt('hand',r.ats.t)+'</div></div>'+verdict(r.ats.ok)+'</div>';
 if(r.tot) h+='<div class="ligne"><div class="lib">Total</div><div><div class="choix">'+r.tot.sens+' '+String(r.tot.ligne).replace('.',',')+'</div>'+barre(r.tot.p)+'<div class="reglage">'+histoTxt('tot',r.tot.t)+'</div></div>'+verdict(r.tot.ok)+'</div>';
 el.innerHTML=h;
 document.getElementById('c'+i).classList.toggle('conseil',r.paris.length>0);
}

function tout(){
 let liste=[];
 M.forEach((m,i)=>{const r=analyse(m,i);rendCarte(m,i,r);liste=liste.concat(r.paris)});
 liste.sort((a,b)=>b.score-a.score);
 const z=document.getElementById('paris');
 if(!liste.length){z.innerHTML='<div class="aucun">❌ Aucun pari conseillé cette semaine</div>';return}
 z.innerHTML=liste.map((p,k)=>'<div class="pari"><div class="rang">'+(k+1)+'</div><div><div class="quoi">'+p.quoi
   +'</div><div class="type">'+p.type+'</div></div><div class="chiffres">'+pc(p.p)+'<br>'+p.detail+'</div></div>').join('');
}
// --- Sélections moneyline >= 60 % (après calibration) ---
const H60=JSON.parse(document.getElementById('histo60').textContent);
function ev60(tr){const p=+tr.dataset.p, c=parseFloat(tr.querySelector('input').value.replace(',','.'));
 const td=tr.querySelector('.ev');if(isNaN(c)){td.innerHTML='';return}const ev=p*c-1;
 td.innerHTML='<span class="'+(ev>0?'bon':'mauvais')+'">'+sg((ev*100).toFixed(1))+' %</span>'}
function ml60(){
 const L=[];
 M.forEach((m,i)=>{const E=etat(m,i), dom=E.pvic>=0.5, p=dom?E.pvic:1-E.pvic; if(p<%SEL%)return;
   L.push({i:i, cl:dom?'in-cd':'in-ce', eq:dom?m.dom:m.ext, adv:dom?m.ext:m.dom, p:p, c:val(i,dom?'in-cd':'in-ce')})});
 L.sort((a,b)=>b.p-a.p);
 const z=document.getElementById('ml60');
 if(!L.length){z.innerHTML='<div class="aucun">Aucune sélection cette semaine</div>';return}
 z.innerHTML='<div class="tab"><table><thead><tr><th>Équipe</th><th>Adversaire</th><th>Proba calibrée</th><th>Cote book</th><th>EV</th></tr></thead><tbody>'
  +L.map(x=>'<tr data-i="'+x.i+'" data-cl="'+x.cl+'" data-p="'+x.p+'"><td class="match">'+x.eq+'</td><td>'+x.adv+'</td><td style="min-width:150px">'+barre(x.p)
  +'</td><td><input type="number" step="0.01" value="'+(isNaN(x.c)?'':x.c)+'"></td><td class="ev num"></td></tr>').join('')+'</tbody></table></div>';
 z.querySelectorAll('tbody tr').forEach(tr=>{ev60(tr);tr.querySelector('input').addEventListener('input',()=>{
   document.querySelector('#c'+tr.dataset.i+' .'+tr.dataset.cl).value=tr.querySelector('input').value; ev60(tr); tout()})});
}
function majTout(){tout();ml60()}
document.querySelectorAll('#cartes input').forEach(x=>x.addEventListener('input',majTout));
tout();
ml60();
""".replace("%SEUIL%", str(SEUIL_PARI)).replace("%SEL%", "0.60").replace("%SEUIL_EV%", str(SEUIL_EV_ML)).replace(
    "%RENTA%", str(SEUIL_RENTABILITE))


def fmt(x, nd=1):
    return "" if pd.isna(x) else f"{x:.{nd}f}"


TRANCHES_P = [0.55, 0.60, 0.65, 1.01]        # tranches de proba (handicap / total)
TRANCHES_EV = [0.05, 0.15, 0.30, 99.0]       # tranches d'espérance de gain (vainqueur)


def _tranches(valeur, resultat, saisons, bornes, rentable):
    """Pour chaque tranche : taux (ou rendement), nb de paris, et si c'est rentable de façon régulière."""
    out = []
    for lo, hi in zip(bornes[:-1], bornes[1:]):
        m = (valeur >= lo) & (valeur < hi)
        n = int(m.sum())
        if n == 0:
            out.append({"lo": lo, "hi": hi, "v": None, "n": 0, "ok": False})
            continue
        v = float(np.mean(resultat[m]))
        par_saison = pd.Series(resultat[m]).groupby(np.asarray(saisons)[m]).mean()
        ok = bool(rentable(v) and (par_saison.apply(rentable).sum() >= 3) and n >= 30)
        out.append({"lo": lo, "hi": hi, "v": round(v, 4), "n": n, "ok": ok})
    return out


def historique_paris(tout):
    """
    Réussite passée (backtest walk-forward) des paris, par tranche de confiance.
    Une tranche n'est « à jouer » que si elle a été rentable au global ET sur au moins 3 saisons sur 4.
    """
    t = tout.copy()
    res = {}
    a = t[t["spread_line"].notna() & (t["marge"] != t["spread_line"])]
    pick = a["p_couvre"] >= 0.5
    pa = np.where(pick, a["p_couvre"], 1 - a["p_couvre"])
    ga = (pick == (a["marge"] > a["spread_line"])).to_numpy().astype(float)
    res["hand"] = _tranches(pa, ga, a["season"], TRANCHES_P, lambda v: v >= SEUIL_RENTABILITE)

    o = t[t["total_line"].notna() & (t["total"] != t["total_line"])]
    ov = o["p_over"] >= 0.5
    po = np.where(ov, o["p_over"], 1 - o["p_over"])
    go = (ov == (o["total"] > o["total_line"])).to_numpy().astype(float)
    res["tot"] = _tranches(po, go, o["season"], TRANCHES_P, lambda v: v >= SEUIL_RENTABILITE)

    v = t[t["ml_dom"].notna() & t["ml_ext"].notna() & (t["marge"] != 0)]
    cd, ce = v["ml_dom"].apply(ml_vers_decimale), v["ml_ext"].apply(ml_vers_decimale)
    evd, eve = v["p_victoire"] * cd - 1, (1 - v["p_victoire"]) * ce - 1
    dom = (evd >= eve).to_numpy()
    ev = np.where(dom, evd, eve)
    cote = np.where(dom, cd, ce)
    gain = np.where(dom == (v["marge"] > 0).to_numpy(), cote - 1, -1.0)
    res["ml"] = _tranches(ev, gain, v["season"], TRANCHES_EV, lambda x: x > 0)
    return res


SEUIL_ML_SELECTION = 0.60   # section « Sélections moneyline ≥ 60 % »


def historique_ml60(tout):
    """Bilan passé : équipe donnée gagnante à ≥ 60 % par le modèle, jouée à la cote de clôture."""
    v = tout[tout["ml_dom"].notna() & tout["ml_ext"].notna() & (tout["marge"] != 0)]
    dom = v["p_victoire"] >= 0.5
    p = np.where(dom, v["p_victoire"], 1 - v["p_victoire"])
    cote = np.where(dom, v["ml_dom"].apply(ml_vers_decimale), v["ml_ext"].apply(ml_vers_decimale))
    gagne = (dom == (v["marge"] > 0)).to_numpy()
    m = p >= SEUIL_ML_SELECTION
    gain = np.where(gagne, cote - 1, -1.0)
    par_saison = pd.Series(gain[m]).groupby(v["season"].to_numpy()[m]).mean()
    return {"n": int(m.sum()), "taux": round(float(gagne[m].mean()), 4),
            "roi": round(float(gain[m].mean()), 4),
            "saisons": {int(k): round(float(x), 4) for k, x in par_saison.items()}}


def bloc_ml60(h):
    def v(x):                         # format français, sans toucher au JSON
        return f"{x*100:+.1f} %".replace(".", ",")
    saisons = "".join(f'<div>{k} : <b class="{"bon" if x > 0 else "mauvais"}">{v(x)}</b></div>'
                      for k, x in h["saisons"].items())
    taux = f"{h['taux']*100:.1f} %".replace(".", ",")
    cl = "bon" if h["roi"] > 0 else "mauvais"
    return (f'<script type="application/json" id="histo60">{json.dumps(h)}</script>'
            f'<div id="ml60"></div><div class="histo">'
            f'<div>Historique : <b>{taux}</b> gagnés ({h["n"]})</div>'
            f'<div>Rendement : <b class="{cl}">{v(h["roi"])}</b></div>{saisons}</div>')


def bloc_paris(hist):
    noms = {"hand": "Handicap", "tot": "Total", "ml": "Vainqueur"}
    cases = []
    for cle, tranches in hist.items():
        for tr in tranches:
            if tr["v"] is None:
                continue
            if cle == "ml":
                lib = f"EV {tr['lo']*100:.0f}–{tr['hi']*100:.0f} %" if tr["hi"] < 99 else f"EV ≥ {tr['lo']*100:.0f} %"
                txt = f"{tr['v']*100:+.1f} %".replace(".", ",")
            else:
                lib = f"{tr['lo']*100:.0f}–{min(tr['hi'],1)*100:.0f} %"
                txt = f"{tr['v']*100:.1f} %".replace(".", ",")
            cl = "bon" if tr["ok"] else "mauvais"
            cases.append(f'<div>{noms[cle]} {lib} : <b class="{cl}">{txt}</b> ({tr["n"]})</div>')
    json_h = json.dumps(hist).replace("</", "<\\/")
    return (f'<script type="application/json" id="histo">{json_h}</script>'
            f'<div id="paris"></div><div class="histo">{"".join(cases)}</div>')


def cartes_semaine(p, modele):
    e = html.escape
    donnees, cartes = [], []
    for i, r in enumerate(p.itertuples(index=False)):
        cd, ce = ml_vers_decimale(r.ml_dom), ml_vers_decimale(r.ml_ext)
        donnees.append({"dom": r.dom, "ext": r.ext, "marge": round(r.pred_marge, 3), "m1": round(r.marge_modele, 4),
                        "sigma": round(modele.sigma, 3), "total": round(r.pred_total, 3),
                        "sigmat": round(modele.sigma_total, 3), "pvic": round(r.p_victoire, 4)})
        hand = "" if pd.isna(r.spread_line) else fmt(-r.spread_line)   # handicap du domicile
        blessures = "".join(f"<div>🩹 {e(t)} : {e(x)}</div>"
                            for t, x in ((r.dom, r.absents_dom), (r.ext, r.absents_ext)) if x)
        cartes.append(f"""
<div class="carte" id="c{i}">
 <div class="entete"><div class="equipes">🏠 {e(r.dom)} – {e(r.ext)}</div>
   <div class="elo">Elo {r.elo_dom:+.0f} / {r.elo_ext:+.0f}</div></div>
 <div class="calc"></div>
 <div class="reglage">Handicap {e(r.dom)} <input class="in-hand" type="number" step="0.5" value="{hand}">
   Total <input class="in-tot" type="number" step="0.5" value="{fmt(r.total_line)}"></div>
 <div class="reglage">Cote {e(r.dom)} <input class="in-cd" type="number" step="0.01" value="{fmt(cd, 2)}">
   Cote {e(r.ext)} <input class="in-ce" type="number" step="0.01" value="{fmt(ce, 2)}"></div>
 {f'<div class="blesse">{blessures}</div>' if blessures else ''}
</div>""")
    json_txt = json.dumps(donnees, ensure_ascii=False).replace("</", "<\\/")
    params = json.dumps(modele.params_js()).replace("</", "<\\/")
    return (f'<script type="application/json" id="params">{params}</script>'
            f'<script type="application/json" id="donnees">{json_txt}</script>'
            f'<div id="cartes">{"".join(cartes)}</div>')


def tableau_backtest(tab):
    tete = "".join(f"<th>{html.escape(str(c))}</th>" for c in ["Saison"] + list(tab.columns))
    corps = "".join("<tr><td class='num'>" + html.escape(str(i)) + "</td>"
                    + "".join(f"<td class='num'>{html.escape(str(v))}</td>" for v in row) + "</tr>"
                    for i, row in zip(tab.index, tab.values))
    return f'<div class="tab"><table><thead><tr>{tete}</tr></thead><tbody>{corps}</tbody></table></div>'


def tableau_erreurs(tout, n=15):
    t = tout.assign(erreur=(tout["marge"] - tout["pred_marge"]).abs()).nlargest(n, "erreur")
    lignes = "".join(
        f"<tr><td class='num'>{r.season}</td><td class='num'>S{r.week}</td>"
        f"<td class='match'>{html.escape(r.dom)} vs {html.escape(r.ext)}</td>"
        f"<td class='num'>{r.pred_marge:+.1f}</td><td class='num'>{r.marge:+.0f}</td>"
        f"<td class='num'>{fmt(r.spread_line)}</td><td class='num'>{r.erreur:.1f}</td></tr>"
        for r in t.itertuples(index=False))
    return ("<div class='tab'><table><thead><tr><th>Saison</th><th>Sem.</th><th>Match</th>"
            "<th>Marge prédite</th><th>Marge réelle</th><th>Spread</th><th>Erreur</th></tr></thead>"
            f"<tbody>{lignes}</tbody></table></div>")


def bandeau_blessures(semaine):
    r = CORRECTIONS.get("rapport")
    if not semaine or r is None:
        return ""
    ok = r >= semaine
    corr = sum(len(v) for v in CORRECTIONS["absents"].values()) + sum(len(v) for v in CORRECTIONS["presents"].values())
    txt = f"🩹 Rapport blessures : semaine {r}" + (f" · {corr} correction(s) manuelle(s)" if corr else "")
    coul = "#1e5631" if ok else "#5a1e1e"
    return f'<div style="background:{coul};border-radius:10px;padding:10px 14px;font-weight:700;margin-bottom:8px">{txt}</div>'


def generer_html(p, modele, semaine, courante, df, tout, tab, comp=None, fichier=FICHIER_HTML):
    """Construit le dashboard autonome (CSS + images base64 + JS inline) et l'ouvre."""
    print("5) Génération du dashboard HTML…")
    img_cal = graphique_calibration(tout)
    img_marge = graphique_marges(tout)
    img_elo = graphique_elo(df, courante)
    titre = f"NFL {courante} — Semaine {semaine}" if semaine else f"NFL {courante}"
    hist = historique_paris(tout)
    h60 = historique_ml60(tout)
    section_semaine = (f"<h2>Sélections moneyline ≥ 60 %</h2>{bloc_ml60(h60)}"
                       f"<h2>Paris de la semaine</h2>{bloc_paris(hist)}"
                       f"<h2>Tous les matchs</h2>{cartes_semaine(p, modele)}") if p is not None else ""
    page = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(titre)}</title><style>{CSS}</style></head>
<body><div class="page">
<h1>🏈 {html.escape(titre)}</h1>
{bandeau_blessures(semaine)}
{section_semaine}
<h2>Elo des équipes</h2>
<div class="full"><img alt="Elo" src="data:image/png;base64,{img_elo}"></div>
<h2>Backtest {SAISONS_BACKTEST[0]}–{SAISONS_BACKTEST[-1]}</h2>
{tableau_backtest(tab)}
{('<h2>Avant / après calibration</h2>' + tableau_backtest(comp)) if comp is not None else ''}
<div class="graph full">
 <img alt="Calibration" src="data:image/png;base64,{img_cal}">
 <img alt="Marges" src="data:image/png;base64,{img_marge}">
</div>
<h2>Plus grosses erreurs</h2>
{tableau_erreurs(tout)}
</div><script>{JS}</script></body></html>"""
    with open(fichier, "w", encoding="utf-8") as f:
        f.write(page)
    chemin = os.path.abspath(fichier)
    print(f"  Dashboard : {chemin}")
    if not os.environ.get("CI"):            # pas d'ouverture du navigateur sur GitHub Actions
        try:
            webbrowser.open("file://" + chemin)
        except Exception:
            pass
    return chemin


# =============================================================================
# 8. PRÉDICTIONS DE LA PROCHAINE SEMAINE
# =============================================================================
def pct(x):
    return "" if pd.isna(x) else f"{x:.1%}"


def predire_semaine(df, courante):
    a_venir = df[(df["season"] == courante) & df["marge"].isna()]
    if a_venir.empty:
        print("\nAucun match à venir.")
        return None, None, None
    semaine = int(a_venir["week"].min())
    prochains = a_venir[a_venir["week"] == semaine]
    # Poids et calibration appris sur les saisons passées, appliqués à la saison courante
    modele = ModeleElo().fit(df[(df["season"] < courante) & df["marge"].notna()])
    p = modele.predict(prochains)
    print("\n  Poids appris :", ", ".join(f"{f}={c:.2f}" for f, c in zip(FEATURES, modele.reg.coef_)))
    tab = pd.DataFrame({
        "Match": p["dom"] + " vs " + p["ext"],
        "Elo dom": p["elo_dom"].round(0).astype(int),
        "Elo ext": p["elo_ext"].round(0).astype(int),
        "Malus dom": p["malus_dom"].round(1),
        "Malus ext": p["malus_ext"].round(1),
        "Marge": p["pred_marge"].round(1),
        "P(vic dom)": p["p_victoire"].map(pct),
        "P(book)": p["p_book"].map(pct),
        "Spread": p["spread_line"],
        "P(dom couvre)": p["p_couvre"].map(pct),
    })
    print("\n" + "=" * 100)
    print(f"PRÉDICTIONS — saison {courante}, semaine {semaine}")
    print("=" * 100)
    print(tab.to_string(index=False))
    for r in p.itertuples(index=False):
        for cote, txt in ((r.dom, r.absents_dom), (r.ext, r.absents_ext)):
            if txt:
                print(f"  🩹 {cote} : {txt}")
    fichier = f"predictions_elo_{courante}_S{semaine}.csv"
    p.to_csv(fichier, index=False)
    print(f"\n  Fichier : {fichier}")
    return p, modele, semaine


# =============================================================================
# PROGRAMME PRINCIPAL
# =============================================================================
def main():
    global CALIBRATION
    if len(sys.argv) > 1 and sys.argv[1] in ("isotonic", "platt"):
        CALIBRATION = sys.argv[1]
    print(f"Calibration : {CALIBRATION}")
    print("1) Chargement des données nflverse…")
    matchs, pbp, snaps, rosters, blessures, courante = charger_donnees()
    print("2) Préparation des stats équipes / joueurs…")
    eq = stats_equipes(pbp, matchs)
    joueurs = stats_joueurs(pbp, snaps, rosters)
    absents, semaines_rapport = absents_par_semaine(blessures, rosters)
    del pbp
    # Semaine à venir + corrections manuelles d'absences
    a_venir = matchs[(matchs["season"] == courante) & matchs["result"].isna()]
    if not a_venir.empty:
        cible = (courante, int(a_venir["week"].min()))
        CORRECTIONS["cible"] = cible
        rapports = blessures[blessures["season"] == courante]["week"]
        CORRECTIONS["rapport"] = int(rapports.max()) if len(rapports) else None
        if os.path.exists(FICHIER_ABSENCES):
            with open(FICHIER_ABSENCES, encoding="utf-8") as f:
                brut = json.load(f)
            noms = rosters[rosters["season"] == courante].dropna(subset=["gsis_id"])
            noms = noms.drop_duplicates("full_name", keep="last").set_index("full_name")["gsis_id"]
            for cle in ("absents", "presents"):
                for eq_, liste in brut.get(cle, {}).items():
                    ids = {noms[n] for n in liste if n in noms}
                    for n in liste:
                        if n not in noms:
                            print(f"  ⚠ {FICHIER_ABSENCES} : joueur inconnu « {n} »")
                    CORRECTIONS[cle][eq_] = ids
    print("3) Calcul semaine par semaine (Elo, EPA, blessures)…")
    df = construire_features(matchs, eq, joueurs, absents, semaines_rapport)
    print("4) Backtest…")
    tout, tab, comp = backtest(df)
    tout.to_csv("backtest_elo.csv", index=False)
    p, modele, semaine = predire_semaine(df, courante)
    generer_html(p, modele, semaine, courante, df, tout, tab, comp)


if __name__ == "__main__":
    main()
