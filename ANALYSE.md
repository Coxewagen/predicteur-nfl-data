# Consignes — analyse automatique des matchs serrés (mercredi et samedi)

Lu par la tâche planifiée Claude à chaque passage. Modifier ce fichier suffit pour changer la façon d'analyser.

## 1. Quels matchs
Lancer `python matchs_serres.py` : il écrit `a_analyser.json` avec les matchs pas encore commencés des 7 prochains
jours qui sont **serrés** (favori du modèle entre 45 et 60 %) ou en **désaccord** avec le marché (favori différent,
ou au moins 10 points d'écart). Ne pas en ajouter d'autres.

- **Mercredi** : analyser tous les matchs de la liste.
- **Samedi** : refaire l'analyse des matchs de la liste (rapports de blessures finaux du vendredi). Le match du jeudi,
  déjà joué, n'est plus dans la liste : garder son analyse telle quelle.

## 2. Ce qu'il faut regarder pour chaque match
1. Les chiffres du robot (`app-data.json`, semaine du match) : probabilités et score attendu du modèle, marché
   (`mkPH`, `mkLine`), blessures « final » / « provisoire », absences (`absences`, fiche `fiche` : bilan, points
   marqués/encaissés, 5 derniers matchs).
2. Une recherche web courte et récente (moins de 4 jours) : statut des QB et titulaires incertains, météo si match
   en extérieur (vent fort, neige), changements de dernière minute (entraîneur, suspension).
3. Le contexte : repos (match jeudi précédent, semaine de repos), déplacement long, enjeu.

## 3. Ce qu'il faut écrire (en français simple, du point de vue d'un parieur prudent)
Pour chaque match, une entrée dans `analyses.json` → `matchs` → `<id du match>` :

```json
{
  "faitLe": "2026-10-14T23:45:00+02:00",
  "raisons": ["serré", "désaccord"],
  "resume": "Une phrase : pourquoi ce match est difficile à lire.",
  "points": ["3 à 5 éléments concrets et vérifiés, une ligne chacun (blessure, forme, météo, marché…)"],
  "penchant": "Nom complet de l'équipe vers laquelle on penche, ou null si aucun",
  "confiance": "faible | moyenne",
  "avis": "Une ou deux phrases : penchant et conseil (ex. « petite mise seulement », « passer son tour »)."
}
```

Règles :
- Jamais « confiance forte » : ce sont par définition des matchs incertains.
- Si le modèle contredit le marché, rappeler que sur 2020-2026 le marché a eu raison 66 % du temps dans ce cas.
- Ne rien inventer : un fait non vérifié n'entre pas dans `points`. Pas de lien, pas de nom de site.
- Ne jamais toucher aux autres fichiers du robot (`app-data.json`, `index.html`, `state.json`, `rapport.json`).
- `faitLe` : date et heure de Paris AVEC le décalage (+02:00 en heure d'été, +01:00 en heure d'hiver).
- Mettre à jour aussi `analyses.json` → `faitLe` / `faitLeLabel` (ex. « mercredi 14 octobre à 23 h 45 »).
- Supprimer les entrées des matchs joués il y a plus de 14 jours.

## 4. Publier
`git pull --rebase`, puis commit de `analyses.json` seul (message : « Analyse des matchs serrés — <jour> »), puis
`git push`. Le robot se relance tout seul après ce push (déclencheur dans `update.yml`) et l'analyse apparaît
dans la fiche du match (site et appli) quelques minutes plus tard.
