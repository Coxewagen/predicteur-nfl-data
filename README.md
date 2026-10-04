# Flux de données — Prédicteur NFL

Ce dossier, une fois poussé sur un dépôt GitHub (voir GUIDE.md à la racine du projet, étape 2),
exécute automatiquement `update.py` toutes les 3 heures (fichier
`.github/workflows/update.yml`) et republie `app-data.json` dans le dépôt. Avec GitHub Pages
activé sur ce dépôt, ce fichier devient accessible à une adresse stable (ex. :
`https://TON-COMPTE.github.io/predicteur-nfl-data/app-data.json`) — c'est cette adresse qu'il
faut coller dans `src/app.js` (constante `DATA_URL`) côté appli.

Fichiers :
- `update.py` : télécharge les données NFL (nflreadpy), recalcule les stats, lance `model.js`,
  tient le journal des prévisions et leur bilan, et écrit `app-data.json` + `index.html`.
- `model.js` : moteur de calcul (copie exacte de celui de `predicteur-nfl.html`).
  **À resynchroniser à chaque évolution du modèle dans `predicteur-nfl.html`** (dernière synchro : 04/10/2026,
  15 h 25 — absences dégressives/plafonnées/selon le style de jeu, terrain neutre, bonus de série 0,15).
  Les absences de `state.json` portent désormais `gp` (matchs joués par le joueur cette saison, pour la
  dégressivité ; sans `gp` = absence récente, aucune réduction) et `lane` (passe/course).
- `state.json` : les absences que tu renseignes à la main, + le journal des prévisions. C'est le
  seul fichier que tu dois éditer toi-même régulièrement (ouvre-le, modifie `absences`, enregistre
  — le prochain passage du robot s'en servira).
- `template.txt` : la page web (optionnelle, pas utilisée par l'appli mobile elle-même) qui
  reprend les mêmes données en version "site".
- `rapport.json` : écarts entre tes absences retenues et les données officielles (mêmes
  vérifications que celles déjà utilisées pour `predicteur-nfl.html`).
- `backtest.py` : complète le journal pour les semaines déjà jouées que `update.py` n'a jamais
  suivies en direct (il ne suit que la semaine en cours au moment où il tourne). Pour chaque
  semaine manquante, calcule ce que le modèle aurait pronostiqué avec UNIQUEMENT les statistiques
  disponibles avant cette semaine-là (jamais les stats d'aujourd'hui) — un vrai historique, pas
  gonflé après coup. N'écrase jamais une entrée déjà suivie en direct. Lancé automatiquement à
  chaque passage du robot (`update.yml`), donc il comble tout seul une semaine qui aurait été
  manquée (robot en pause, etc.) ; tu peux aussi le lancer à la main (`python backtest.py`) si tu
  veux voir tout de suite ce qu'il ajoute.

Pour relancer une mise à jour immédiatement sans attendre le prochain horaire : onglet
**Actions** du dépôt sur GitHub → **Mise à jour des prévisions NFL** → **Run workflow**.
