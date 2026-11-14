# Signaux d'edge NFL

`nfl_signaux.py` ne prédit plus le résultat des matchs : il prend le spread du book comme référence
et cherche les matchs où le book a pu se tromper. Il est lancé toutes les 6 heures par
`.github/workflows/elo-v3.yml` et republie `dashboard_nfl.html`.

Signaux : changement de QB, titulaire absent avec un remplaçant nettement moins bon (≥ 3 pts),
vent > 30 km/h en extérieur (prévision Open-Meteo), équipe déjà qualifiée/éliminée contre une
équipe en course. Chaque signal est mesuré contre la ligne de clôture depuis 2015 ; seuls ceux
dont l'historique justifie un ajustement sont comptés comme « edge ».

- Lancer à la main : onglet **Actions** → « Dashboard NFL signaux d'edge » → **Run workflow**.
- En local : `pip install -r requirements.txt` puis `python nfl_signaux.py`.

## Corriger les absences à la main (`absences.json`)

```json
{
  "absents":  { "BAL": ["Lamar Jackson"] },
  "presents": { "MIN": ["Kyler Murray"] }
}
```

Noms exacts comme sur nflverse. Ne s'applique qu'à la semaine à venir ; vide-le une fois la semaine passée.
