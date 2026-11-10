# Elo NFL v3 — dashboard

`nfl_elo_v3.py` est lancé automatiquement toutes les 6 heures par
`.github/workflows/elo-v3.yml` et republie `dashboard_nfl.html`.

- Lancer à la main : onglet **Actions** → « Dashboard NFL Elo v3 » → **Run workflow**.
- En local : `pip install -r requirements.txt` puis `python nfl_elo_v3.py`.

## Corriger les absences à la main (`absences.json`)

Le rapport officiel de la semaine sort à partir du mercredi. En attendant, ou pour une info de dernière minute :

```json
{
  "absents":  { "BAL": ["Lamar Jackson"] },
  "presents": { "MIN": ["Kyler Murray"] }
}
```

Noms exacts comme sur nflverse. Ne s'applique qu'à la semaine à venir ; vide-le une fois la semaine passée.
