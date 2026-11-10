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
SAISON_DEBUT = 2016                  # première saison utilisée pour l'apprentissage
SAISONS_BACKTEST = [2022, 2023, 2024, 2025]   # 4 saisons complètes testées en walk-forward

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
CALIBRATION = "platt"                # "isotonic" ou "platt" (modifiable par: python nfl_elo_v3.py platt)
SEUIL_CONFIANCE = 0.55               # pour le % ATS "quand P >= 55 %"

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
        if CALIBRATION == "platt":
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
        if CALIBRATION == "platt":
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
    pick_dom = a["pred_marge"] > a["spread_line"]
    gagne = pick_dom == (a["marge"] > a["spread_line"])
    p_cote = np.where(pick_dom, a["p_couvre"], 1 - a["p_couvre"])   # proba du côté choisi
    conf = p_cote >= SEUIL_CONFIANCE
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
        "% O/U": ou,
    }


def backtest(df):
    resultats = []
    for s in SAISONS_BACKTEST:
        train = df[(df["season"] < s) & df["marge"].notna()]
        test = df[(df["season"] == s) & df["marge"].notna()]
        modele = ModeleElo().fit(train)
        resultats.append(modele.predict(test))
    tout = pd.concat(resultats)

    lignes = {str(s): metriques(tout[tout["season"] == s]) for s in SAISONS_BACKTEST}
    lignes["TOTAL"] = metriques(tout)
    lignes["TOTAL sem. ≥ 4"] = metriques(tout[tout["week"] >= 4])
    tab = pd.DataFrame(lignes).T
    for c in ("MAE modèle", "MAE book"):
        tab[c] = tab[c].astype(float).round(2)
    for c in ("Brier brut", "Brier calibré", "Brier book"):
        tab[c] = tab[c].astype(float).round(4)
    for c in ("% ATS", "% ATS P≥55%", "% O/U"):
        tab[c] = tab[c].astype(float).map(lambda x: f"{x:.1%}")
    tab["Matchs"] = tab["Matchs"].astype(int)
    tab["Paris P≥55%"] = tab["Paris P≥55%"].astype(int)

    print("\n" + "=" * 100)
    print(f"BACKTEST WALK-FORWARD (chaque saison testée avec un modèle appris sur les saisons d'avant)")
    print("=" * 100)
    print(tab.to_string())
    print("  Seuil de rentabilité ATS à -110 : 52,4 %")
    return tout, tab


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
CSS = """
*{box-sizing:border-box}
body{margin:0;background:#1a1a2e;color:#fff;font-family:Segoe UI,Roboto,Arial,sans-serif;font-size:18px}
.page{max-width:1500px;margin:auto;padding:18px}
h1{font-size:30px;margin:6px 0 18px}
h2{font-size:24px;margin:34px 0 14px;border-left:6px solid #4cc9f0;padding-left:12px}
.tab{overflow-x:auto;background:#22223b;border-radius:12px;padding:6px}
table{border-collapse:collapse;width:100%}
th{background:#2d2d50;color:#fff;font-size:16px;text-align:left;padding:12px 10px;white-space:nowrap}
td{padding:12px 10px;border-top:1px solid #3a3a5a;vertical-align:middle;font-size:18px}
tr:hover td{background:#2a2a48}
.match{font-weight:700;font-size:20px;white-space:nowrap}
.qb{font-size:15px;color:#c8c8e0}
.num{font-variant-numeric:tabular-nums;font-weight:700}
.barre{position:relative;background:#3a3a5a;border-radius:8px;height:30px;min-width:120px;overflow:hidden}
.barre span{position:absolute;left:0;top:0;bottom:0;border-radius:8px}
.barre b{position:relative;display:block;text-align:center;line-height:30px;font-size:17px;text-shadow:0 0 4px #000}
.vert{background:#2ecc71}.orange{background:#f39c12}.rouge{background:#e74c3c}
input{width:84px;background:#11111f;color:#fff;border:2px solid #4cc9f0;border-radius:8px;
      padding:7px;font-size:18px;font-weight:700}
.cotes input{width:76px;margin:2px 0}
.edge div{white-space:nowrap;font-weight:700;padding:3px 8px;border-radius:6px;margin:3px 0;font-size:16px}
.ok{background:#1e5631;color:#9ff5b8}.ko{background:#5a1e1e;color:#ffb3b3}
.blesse{font-size:14px;color:#ffcf70}
.graph{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.graph img,.full img{width:100%;border-radius:12px;background:#22223b}
.full{margin-top:16px}
@media (max-width:820px){
  body{font-size:17px}
  .graph{grid-template-columns:1fr}
  #semaine table,#semaine thead,#semaine tbody,#semaine tr,#semaine td{display:block;width:100%}
  #semaine thead{display:none}
  #semaine tr{border-bottom:4px solid #1a1a2e;padding:6px 0}
  #semaine td{border:none;display:flex;justify-content:space-between;align-items:center;gap:12px}
  #semaine td::before{content:attr(data-label);font-weight:700;color:#9ad6f0;font-size:15px}
  #semaine .barre{flex:1;max-width:60%}
  #semaine td.edge{flex-wrap:wrap;justify-content:flex-start}
}
"""

JS = """
const SEUIL=%SEUIL%;
function erf(x){const s=Math.sign(x);x=Math.abs(x);const t=1/(1+0.3275911*x);
 const y=1-(((((1.061405429*t-1.453152027)*t)+1.421413741)*t-0.284496736)*t+0.254829592)*t*Math.exp(-x*x);return s*y}
function phi(x){return 0.5*(1+erf(x/Math.SQRT2))}
function classe(p){return p>0.55?'vert':(p>=0.5?'orange':'rouge')}
function barre(el,p){if(isNaN(p)){el.innerHTML='';return}
 el.innerHTML='<div class="barre"><span class="'+classe(p)+'" style="width:'+(p*100).toFixed(1)+'%"></span><b>'+(p*100).toFixed(1)+' %</b></div>'}
function ligne(txt,v){return '<div class="'+(v>0?'ok':'ko')+'">'+txt+'</div>'}
function calcule(tr){
 const d=tr.dataset, m=+d.marge, s=+d.sigma, t=+d.total, st=+d.sigmat, pv=+d.pvic;
 const sp=parseFloat(tr.querySelector('.in-spread').value);
 const to=parseFloat(tr.querySelector('.in-total').value);
 const cd=parseFloat(tr.querySelector('.in-cd').value), ce=parseFloat(tr.querySelector('.in-ce').value);
 let html='';
 const pc=isNaN(sp)?NaN:1-phi((sp-m)/s);
 barre(tr.querySelector('.p-couvre'),pc);
 if(!isNaN(pc)){const dom=pc>=0.5, p=dom?pc:1-pc, e=p-SEUIL;
   html+=ligne('ATS '+(dom?d.dom:d.ext)+' '+(e>=0?'+':'')+(e*100).toFixed(1)+' pts',e)}
 const po=isNaN(to)?NaN:1-phi((to-t)/st);
 barre(tr.querySelector('.p-over'),po);
 if(!isNaN(po)){const ov=po>=0.5, p=ov?po:1-po, e=p-SEUIL;
   html+=ligne((ov?'Over ':'Under ')+(e>=0?'+':'')+(e*100).toFixed(1)+' pts',e)}
 if(!isNaN(cd)&&!isNaN(ce)){const evd=pv*cd-1, eve=(1-pv)*ce-1, dom=evd>=eve, ev=dom?evd:eve;
   html+=ligne('ML '+(dom?d.dom:d.ext)+' EV '+(ev>=0?'+':'')+(ev*100).toFixed(1)+' %',ev)}
 tr.querySelector('.edge').innerHTML=html;
}
document.querySelectorAll('#semaine tbody tr').forEach(tr=>{
 tr.querySelectorAll('input').forEach(i=>i.addEventListener('input',()=>calcule(tr)));calcule(tr)});
""".replace("%SEUIL%", str(SEUIL_RENTABILITE))


def fmt(x, nd=1):
    return "" if pd.isna(x) else f"{x:.{nd}f}"


def barre_html(p):
    if pd.isna(p):
        return ""
    c = "vert" if p > 0.55 else ("orange" if p >= 0.5 else "rouge")
    return (f'<div class="barre"><span class="{c}" style="width:{p*100:.1f}%"></span>'
            f'<b>{p*100:.1f} %</b></div>')


def tableau_semaine(p, modele):
    e = html.escape
    lignes = []
    for r in p.itertuples(index=False):
        cd, ce = ml_vers_decimale(r.ml_dom), ml_vers_decimale(r.ml_ext)
        blessures = "".join(f'<div class="blesse">🩹 {e(t)}: {e(x)}</div>'
                            for t, x in ((r.dom, r.absents_dom), (r.ext, r.absents_ext)) if x)
        lignes.append(f"""
<tr data-marge="{r.pred_marge:.4f}" data-sigma="{modele.sigma:.4f}" data-total="{r.pred_total:.4f}"
    data-sigmat="{modele.sigma_total:.4f}" data-pvic="{r.p_victoire:.4f}" data-dom="{e(r.dom)}" data-ext="{e(r.ext)}">
 <td data-label="Match"><div class="match">{e(r.dom)} vs {e(r.ext)}</div>{blessures}</td>
 <td data-label="Elo" class="num">{r.elo_dom:+.0f} / {r.elo_ext:+.0f}</td>
 <td data-label="Marge" class="num">{r.pred_marge:+.1f}</td>
 <td data-label="P victoire {e(r.dom)}">{barre_html(r.p_victoire)}</td>
 <td data-label="Spread"><input class="in-spread" type="number" step="0.5" value="{fmt(r.spread_line)}"></td>
 <td data-label="P couvre {e(r.dom)}" class="p-couvre"></td>
 <td data-label="Total"><input class="in-total" type="number" step="0.5" value="{fmt(r.total_line)}"></td>
 <td data-label="Total prédit" class="num">{r.pred_total:.1f}</td>
 <td data-label="P over" class="p-over"></td>
 <td data-label="Cotes {e(r.dom)} / {e(r.ext)}" class="cotes">
   <input class="in-cd" type="number" step="0.01" value="{fmt(cd, 2)}">
   <input class="in-ce" type="number" step="0.01" value="{fmt(ce, 2)}"></td>
 <td data-label="Écart vs cote" class="edge"></td>
</tr>""")
    return f"""
<div class="tab" id="semaine"><table>
<thead><tr><th>Match</th><th>Elo dom / ext</th><th>Marge</th><th>P victoire dom</th><th>Spread</th>
<th>P couvre dom</th><th>Total</th><th>Total prédit</th><th>P over</th><th>Cotes dom / ext</th><th>Écart vs cote</th></tr></thead>
<tbody>{''.join(lignes)}</tbody></table></div>"""


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


def generer_html(p, modele, semaine, courante, df, tout, tab, fichier=FICHIER_HTML):
    """Construit le dashboard autonome (CSS + images base64 + JS inline) et l'ouvre."""
    print("5) Génération du dashboard HTML…")
    img_cal = graphique_calibration(tout)
    img_marge = graphique_marges(tout)
    img_elo = graphique_elo(df, courante)
    titre = f"NFL {courante} — Semaine {semaine}" if semaine else f"NFL {courante}"
    section_semaine = tableau_semaine(p, modele) if p is not None else ""
    page = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(titre)}</title><style>{CSS}</style></head>
<body><div class="page">
<h1>🏈 {html.escape(titre)}</h1>
{section_semaine}
<h2>Elo</h2>
<div class="full"><img alt="Elo" src="data:image/png;base64,{img_elo}"></div>
<h2>Backtest {SAISONS_BACKTEST[0]}–{SAISONS_BACKTEST[-1]}</h2>
{tableau_backtest(tab)}
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
    print("3) Calcul semaine par semaine (Elo, EPA, blessures)…")
    df = construire_features(matchs, eq, joueurs, absents, semaines_rapport)
    print("4) Backtest…")
    tout, tab = backtest(df)
    tout.to_csv("backtest_elo.csv", index=False)
    p, modele, semaine = predire_semaine(df, courante)
    generer_html(p, modele, semaine, courante, df, tout, tab)


if __name__ == "__main__":
    main()
