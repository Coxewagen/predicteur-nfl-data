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
  **À resynchroniser à chaque évolution du modèle dans `predicteur-nfl.html`** (dernière synchro : 04/10/2026, 20 h 49 — malus d'absence recalibrés (QB 4 / 5,5 / 2,5, hors QB 3 / 1,25 / 0,6, plafond 4), couche passe/course retirée (PASSRUSH_CAP = 0), pourcentages calibrés (écart-type 17,5 au lieu de 13,5), le tout après rejeu de 1 549 matchs ; avant : absences dégressives/plafonnées/selon le style de jeu, terrain neutre, bonus de série 0,15).
  Les absences de `state.json` portent désormais `gp` (matchs joués par le joueur cette saison, pour la
  dégressivité ; sans `gp` = absence récente, aucune réduction) et `lane` (passe/course).
- `absences_auto.py` : calcule TOUT SEUL, à chaque passage du robot, les absences de chaque équipe
  (titulaires « Out »/« Doubtful » au rapport officiel de blessures, ou sur la liste des blessés), leur niveau
  (élite / titulaire / rotation, d'après le contrat comparé aux joueurs du même poste et le temps de jeu), leur
  secteur (passe / course / ligne), les matchs joués par le joueur (pour la dégressivité) et, pour le
  quarterback, son niveau et l'expérience du remplaçant. Plus aucune saisie hebdomadaire.
  Limites connues : le rapport officiel n'est mis à jour qu'une fois par jour, donc les inactifs annoncés le
  jour du match n'y sont pas ; le niveau repose sur le salaire (un joueur très bien payé mais moyen sur le
  terrain sera classé « élite »).
- `overrides.json` (facultatif, modèle : `overrides.example.json`) : pour corriger une erreur seulement —
  ignorer un joueur, forcer son niveau, ou ajouter une absence que les données officielles ne montrent pas.
- `state.json` : le journal des prévisions (figées au coup d'envoi puis comparées au résultat) et les
  absences calculées. Ne se modifie plus à la main.
- `template.txt` : la page web (le site), avec les mêmes cartes, la même fiche au clic, les semaines passées et à venir
  et la case « J'ai parié » que l'appli. **Ce fichier est généré** : ne le modifie pas à la main, il vient de `src/ui.js`,
  `src/site.js`, `src/site-shell.html` et `www/app.css` (dossier de l'appli) par la commande `node build_site.js`.
  Les semaines à venir portent des prévisions provisoires, recalculées à chaque passage du robot.
- `rapport.json` : écarts entre tes absences retenues et les données officielles (mêmes
  vérifications que celles déjà utilisées pour `predicteur-nfl.html`).
  Contient aussi la liste `arrivees` : joueurs apparus dans l'effectif actif depuis la semaine 1 (signatures,
  échanges), avec leur ancienne équipe. Information seulement : elle n'entre pas dans le calcul (testé : aucun gain).
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

**Mise en ligne d'une nouvelle version :** remplace seulement les fichiers de code (`model.js`, `update.py`, `absences_auto.py`, `backtest.py`, `template.txt`, `README.md`, `.github/`). Ne remplace PAS `state.json`, `app-data.json`, `index.html` et `rapport.json` du dépôt : ce sont les fichiers du robot (journal des prévisions figées au coup d'envoi). Ceux de ce zip sont une copie de 15 h 27, déjà périmée.
