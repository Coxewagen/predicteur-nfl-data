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

**Bouton « Mettre à jour » du prédicteur (predicteur-nfl.html) :** `update.py` ajoute une section `predicteur` (équipes, statistiques, absences automatiques sans noms de joueurs, calendrier et résultats) au fichier `app-data.json`. Le bouton de la page HTML télécharge ce fichier sur ton site GitHub Pages et rafraîchit la page sans rien remplacer à la main. Aucun changement du workflow GitHub n'est nécessaire. Il ne remplace pas les données si le site n'en a pas de plus récentes que celles affichées.

**Marché et avertissements (appli, site, extension) :** `update.py` ajoute à chaque match à venir la ligne du marché et les moneylines (nflverse) : `mkLine` (écart de points, + = domicile favori), `mkPH` (probabilité de victoire du domicile sans marge du bookmaker). Les lignes ne sont publiées que quelques jours avant chaque match : les semaines lointaines n'ont pas de marché. L'appli et le site affichent « Marché : … % » et deux avertissements : « Contredit le marché » (le modèle prend l'autre favori avec ≥ 6 pts d'écart : modèle juste 34 % du temps en 2020-2026) et « Prudence » (favori du modèle ≥ 75 % et ≥ 5 pts au-dessus du marché). Remplace `update.py` ET `model.js` ensemble (model.js renvoie maintenant l'écart exact du modèle).

## Saison précédente dans le modèle (05/10)
`update.py` calcule, pour chaque équipe, la marge moyenne par match de la saison précédente et de celle d'avant (70 % / 30 %), un éventuel nouvel entraîneur, et un éventuel nouveau QB titulaire (recrue ou écart de qualité). Ces éléments sont écrits dans `prior` (équipes, fichier `app-data.json`) et lus par `model.js` (robot) et par l'extension.
Le poids de la saison précédente s'estompe avec la semaine (exp(-(semaine-1)/12)) et l'écart brut du modèle reprend la main. Rejeu 2020-2025, chaque saison testée à part : 64,9 % de bons vainqueurs (61,3 % avant), écart moyen avec le marché à la semaine 5 : 2,7 pts (3,9 avant).
**À remplacer ensemble sur GitHub : `update.py` ET `model.js`.** Sans champ `prior`, l'ancienne formule est utilisée.

## Blessures en milieu de semaine (10/10)
`absences_auto.py` choisit le rapport de blessures **équipe par équipe** : le rapport final (statuts Out/Doubtful/Questionable) de la semaine du prochain match s'il est publié, sinon le dernier rapport final, en retirant les joueurs absents du nouveau rapport d'entraînement ou qui s'entraînent normalement. L'ancienne version ne lisait que la toute dernière semaine publiée : dès le mercredi (rapports d'entraînement sans statut), ou après une semaine de repos, les absents disparaissaient (vérifié le 10/10 : 79 titulaires absents au lieu de 109).
Chaque match à venir porte `blessures: "final"` ou `"provisoire"` (affiché dans l'appli, le site et l'extension). Ligne offensive : un seul titulaire absent compté par poste.
À remplacer sur GitHub : `update.py`, `absences_auto.py`, `template.txt` (et ce README).

## Scores en direct (10/10)
La page et l'appli lisent le tableau des scores public d'ESPN (sans clé) toutes les 30 secondes, seulement entre 10 minutes avant un coup d'envoi et 5 heures après. Une carte en cours affiche « En direct · 3e QT · 8:42 » et le score ; un match fini affiche le résultat « provisoire, ESPN » jusqu'au passage suivant du robot, qui reste la référence pour le bilan. Si ESPN ne répond pas, rien ne change.
