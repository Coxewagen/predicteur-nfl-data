#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cotes Betclic (France) des matchs NFL à venir, via The Odds API (bookmaker « betclic_fr »).
Écrit cotes_betclic.json, lu par le dashboard (elo-v3/nfl_signaux.py) et par l'appli (update.py).

La clé API est lue dans la variable d'environnement ODDS_API_KEY (secret GitHub du même nom).
Sans clé, ou en cas d'erreur, le fichier existant est gardé tel quel.
Coût : 1 crédit par appel (1 marché « h2h » x 1 bookmaker).
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

FICHIER = Path(__file__).with_name("cotes_betclic.json")
URL = ("https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds"
       "?apiKey={cle}&bookmakers=betclic_fr&markets=h2h&oddsFormat=decimal&dateFormat=iso")

# Nom complet (The Odds API) -> abréviation nflverse
EQUIPES = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL", "Buffalo Bills": "BUF",
    "Carolina Panthers": "CAR", "Chicago Bears": "CHI", "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE",
    "Dallas Cowboys": "DAL", "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX", "Kansas City Chiefs": "KC",
    "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC", "Los Angeles Rams": "LA", "Miami Dolphins": "MIA",
    "Minnesota Vikings": "MIN", "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT", "San Francisco 49ers": "SF",
    "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB", "Tennessee Titans": "TEN", "Washington Commanders": "WAS",
}


def main():
    cle = os.environ.get("ODDS_API_KEY", "").strip()
    if not cle:
        print("Cotes Betclic : pas de clé ODDS_API_KEY, fichier inchangé.")
        return
    try:
        req = urllib.request.Request(URL.format(cle=cle), headers={"User-Agent": "predicteur-nfl"})
        with urllib.request.urlopen(req, timeout=60) as r:
            donnees = json.loads(r.read())
            restants = r.headers.get("x-requests-remaining")
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        print(f"Cotes Betclic : échec de la requête ({e}), fichier inchangé.")
        return

    matchs = []
    for ev in donnees:
        dom, ext = EQUIPES.get(ev.get("home_team")), EQUIPES.get(ev.get("away_team"))
        if not dom or not ext:
            continue
        for b in ev.get("bookmakers", []):
            if b.get("key") != "betclic_fr":
                continue
            for m in b.get("markets", []):
                if m.get("key") != "h2h":
                    continue
                prix = {o["name"]: o["price"] for o in m.get("outcomes", [])}
                cd, ce = prix.get(ev["home_team"]), prix.get(ev["away_team"])
                if cd and ce:
                    matchs.append({"dom": dom, "ext": ext, "debut": ev.get("commence_time"),
                                   "cote_dom": cd, "cote_ext": ce, "maj_book": b.get("last_update")})
    if not matchs:
        print(f"Cotes Betclic : aucune cote Betclic reçue ({len(donnees)} matchs), fichier inchangé.")
        return
    sortie = {"maj": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "credits_restants": restants, "matchs": matchs}
    FICHIER.write_text(json.dumps(sortie, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Cotes Betclic : {len(matchs)} matchs, crédits restants ce mois-ci : {restants}")


if __name__ == "__main__":
    sys.exit(main())
