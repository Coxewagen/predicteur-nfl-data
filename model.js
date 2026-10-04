// Moteur de calcul copié tel quel depuis predicteur-nfl.html (version du 04/10/2026, 15h25) :
// absences dégressives/plafonnées/selon le style de jeu, terrain neutre, bonus de série 0,15.
// À RESYNCHRONISER à chaque évolution du modèle dans predicteur-nfl.html (voir README).
let TEAMS = [];
let WEEKS_HISTORY = {};
// ===================== MODÈLE =====================
// Référence stable (pas recalculée sur les 1-2 premiers matchs, trop bruités) :
// moyenne NFL habituelle des dernières saisons, environ 22-23 points/équipe/match.
const LEAGUE_AVG_POINTS = 22.5;
const HOME_ADV_POINTS = 2.0;   // avantage terrain, en points de spread (valeur standard NFL)
// Match joué à l'étranger (Londres, Paris, Madrid, Munich, Mexico, Melbourne, Rio...) = TERRAIN NEUTRAL : l'équipe "à domicile"
// n'a pas de vrai avantage du terrain. Vérifié sur 49 matchs 2015-2025 : marge moyenne de l'équipe nominalement à domicile
// +0,4 pt (contre +1,8 pt à domicile). Le match est repéré par le champ neutral:true dans WEEKS_HISTORY (nflreadpy, location=Neutral).
function isNeutralGame(homeName, awayName){
  for(const w in WEEKS_HISTORY){
    for(const g of WEEKS_HISTORY[w]){ if(g.neutral && g.home===homeName && g.away===awayName) return true; }
  }
  return false;
}
const SPREAD_STD_DEV = 13.5;   // écart-type de l'écart de points final, valeur usuelle en analytics NFL
const TIE_PROB = 0.004;        // les nuls sont très rares en NFL (prolongation à mort subite)
const SHRINKAGE_GAMES = 4;     // poids de la moyenne ligue, exprimé en "matchs virtuels" — s'estompe à mesure que la saison avance

// Seuil "pari sûr" : Hans ne parie que sur des favoris nets et ignore les matchs serrés.
// Ce n'est pas une règle stricte de sa part (il peut descendre en dessous si besoin), donc c'est
// un repère visuel dans l'outil, pas un filtre qui cache les autres matchs. Ajustable ici si besoin.
const SAFE_BET_THRESHOLD = 0.60;

// Fonction de répartition normale standard (approximation d'Abramowitz-Stegun)
function normalCDF(x){
  const t = 1/(1+0.2316419*Math.abs(x));
  const d = 0.3989423*Math.exp(-x*x/2);
  let p = d*t*(0.3193815+t*(-0.3565638+t*(1.781478+t*(-1.821256+t*1.330274))));
  if(x>0) p = 1-p;
  return p;
}

function findTeam(name){ return TEAMS.find(t=>t.name===name); }

// Lisse les stats brutes en début de saison en les ramenant vers la moyenne ligue,
// pondéré par le nombre de matchs déjà joués — pas vers la saison passée (ça avait
// posé problème en Ligue 1 en surpondérant la réputation), juste vers une valeur
// neutre. Avec 4 matchs joués, le poids de la moyenne ligue tombe déjà à 50% ; avec
// 10+ matchs, il devient négligeable et les vraies stats de la saison dominent.
function shrinkToLeagueAverage(rawValue, played){
  const w = SHRINKAGE_GAMES;
  return (rawValue*played + LEAGUE_AVG_POINTS*w) / (played + w);
}

// Série en cours (victoires/défaites consécutives), calculée directement à partir
// de l'historique déjà présent dans le fichier (WEEKS_HISTORY) — pas besoin de
// données supplémentaires. Plafonnée pour éviter qu'une très longue série pèse
// de façon disproportionnée dans un sport où un seul match peut tout changer.
const STREAK_BONUS_PER_GAME = 0.15; // points par match de la série (mesuré 0,14 à 0,19 ± 0,07 sur 3 900 matchs 2006-2025 ; ancienne valeur 0,8 = ~5x trop haute)
const STREAK_MAX_GAMES = 5;        // plafond : au-delà, le bonus n'augmente plus

function computeCurrentStreak(teamName){
  if(typeof WEEKS_HISTORY === 'undefined') return {type:null, len:0};
  const weeks = Object.keys(WEEKS_HISTORY).map(Number).sort((a,b)=>a-b);
  const results = [];
  weeks.forEach(w=>{
    WEEKS_HISTORY[w].forEach(m=>{
      if(m.status !== 'closed') return;
      if(m.home === teamName){
        results.push(m.homeScore > m.awayScore ? 'W' : (m.homeScore === m.awayScore ? 'T' : 'L'));
      } else if(m.away === teamName){
        results.push(m.awayScore > m.homeScore ? 'W' : (m.homeScore === m.awayScore ? 'T' : 'L'));
      }
    });
  });
  if(!results.length) return {type:null, len:0};
  const last = results[results.length-1];
  if(last === 'T') return {type:null, len:0}; // un nul casse la série, trop rare pour compter
  let len = 0;
  for(let i=results.length-1; i>=0; i--){
    if(results[i] === last) len++;
    else break;
  }
  return {type:last, len:Math.min(len, STREAK_MAX_GAMES)};
}

function streakImpact(teamName){
  const s = computeCurrentStreak(teamName);
  if(!s.type || s.len===0) return {points:0, tag:null};
  const points = s.type === 'W' ? s.len*STREAK_BONUS_PER_GAME : -s.len*STREAK_BONUS_PER_GAME;
  const label = s.type === 'W' ? '🔥 '+s.len+' victoire(s) de suite' : '📉 '+s.len+' défaite(s) de suite';
  return {points, tag:{t:label, neg:s.type==='L'}};
}

// Impact des blessures : le QB pèse à lui seul bien plus que n'importe quel autre
// poste (c'est un fait établi en analytics NFL — un remplaçant au poste de QB coûte
// typiquement 5 à 8 points de production offensive, largement plus que la perte
// d'un WR1 ou RB1). Les deux facteurs sont donc traités séparément, pas mélangés
// dans un même "nombre d'absents" comme en Ligue 1.
// Malus pondéré par l'importance du joueur (contrat + statut), pas un chiffre fixe pour tout le monde.
// "titulaire" = valeur par défaut (comportement identique à l'ancien malus fixe) si le poste n'a pas de niveau précisé.
const QB_OUT_MALUS = { elite: 9, titulaire: 6.5, limite: 4 };
// KEY_SKILL_MALUS / DEFENSE_KEY_MALUS : malus par titulaire absent, à N'IMPORTE QUEL POSTE
// (receveur, coureur, ligne offensive, cornerback, safety, linebacker, ligne défensive...).
// Le poste exact importe moins que son NIVEAU d'importance pour l'attaque ou la défense :
// "elite" = joueur de très haut niveau à son poste (All-Pro/Pro Bowl régulier, pièce
// difficilement remplaçable, ex. Micah Parsons, un centre élite comme Elgton Jenkins...),
// "titulaire" = titulaire solide standard, "rotation" = joueur de profondeur/rotation dont
// l'absence pèse mais reste absorbable. Ce n'est donc PAS limité aux postes "stars" (WR1/RB1/CB1) :
// perdre 2 titulaires de ligne offensive peut peser autant qu'un WR1, juste réparti différemment.
const KEY_SKILL_MALUS = { elite: 4, titulaire: 2.5, rotation: 1.2 };
// Si le remplaçant (2e QB) est LUI AUSSI absent (blessure/protocole commotion...), on ne peut plus
// se baser sur le niveau du titulaire : l'équipe se retrouve avec un 3e choix / joueur de practice
// squad, ce qui est toujours pire que la pire absence "simple" (tier "elite" = malus max, -9 pts).
// RÈGLE CONSERVÉE TELLE QUELLE (qbBackupOut) — s'ajoute la règle ci-dessous, elle ne la remplace pas :
// même quand le 2e QB n'est PAS blessé, il peut représenter une grosse perte de niveau (rookie non
// drafté, 0 série NFL jouée) qu'un simple malus de titulaire ne capture pas. On quantifie ça avec un
// critère objectif et vérifiable : le nombre de départs en carrière en saison régulière NFL du
// remplaçant qui prend effectivement les snaps. Ce malus s'AJOUTE au malus de base du titulaire absent
// (qbReplacementTier), sauf si qbBackupOut est vrai (cas encore pire, qui prime et plafonne à "elite").
const QB_REPLACEMENT_MALUS = { aucune_experience: 3, limite: 1.5, standard: 0 }; // par nb de départs carrière du remplaçant : 0 / 1-5 / 6+
// ===================== STYLE DE JEU : le malus d'une absence dépend de la spécialisation de l'équipe (ajout du 04/10) =====================
// styleFactor(offTeam, lane) : multiplicateur (0,6 à 1,4) selon la part de jeu au sol de l'équipe qui a le ballon.
//  - Absence DÉFENSIVE taguée pass/rush : on regarde l'ATTAQUE ADVERSE (offTeam = adversaire). Un défenseur contre la passe
//    absent pèse plus face à une attaque aérienne ET efficace dans ce secteur (withEff = true : +/-20 % max selon son EPA).
//  - Absence OFFENSIVE taguée pass/rush (receveur, running back) et QB : on regarde la PROPRE équipe (offTeam = elle-même) :
//    perdre un receveur ou le QB coûte plus à une équipe qui joue surtout la passe, moins à une équipe qui court beaucoup
//    (reliance seule, sans terme d'efficacité). Ligne offensive (lane "line") : pèse plus sur une équipe très spécialisée
//    dans un sens ou dans l'autre (la ligne bloque pour la passe comme pour la course).
// Les stats sont lissées vers la moyenne ligue (SHRINKAGE_GAMES) : l'effet grandit à mesure que la saison avance.
const STYLE_SENSITIVITY = 3;   // sensibilité à l'écart de part de jeu au sol vs moyenne ligue
const STYLE_EFF_WEIGHT = 0.2;  // poids de l'efficacité EPA (défenseurs uniquement)
const STYLE_RANGE = { min: 0.6, max: 1.4 };
function styleFactor(offTeam, lane, withEff){
  if(lane !== 'pass' && lane !== 'rush' && lane !== 'line') return 1;
  const rr = shrinkStat(offTeam && offTeam.rushRate, offTeam && offTeam.played, LEAGUE_AVG_RUSH_RATE);
  const dev = rr - LEAGUE_AVG_RUSH_RATE;
  if(lane === 'line') return Math.min(1.3, 1 + STYLE_SENSITIVITY*Math.abs(dev));
  let mult = 1 + (lane === 'rush' ? 1 : -1)*dev*STYLE_SENSITIVITY;
  if(withEff){
    const key = lane==='pass' ? 'offPassEPA' : 'offRushEPA';
    const avg = lane==='pass' ? LEAGUE_AVG_PASS_EPA : LEAGUE_AVG_RUSH_EPA;
    const scale = lane==='pass' ? 6 : 4;
    mult += STYLE_EFF_WEIGHT*Math.max(-1, Math.min(1, (shrinkStat(offTeam && offTeam[key], offTeam && offTeam.played, avg) - avg)/scale));
  }
  return Math.min(STYLE_RANGE.max, Math.max(STYLE_RANGE.min, mult));
}
// QB : même logique dégressive que les autres absences (qbGp = matchs joués par le QB titulaire cette saison).
function qbDiscount(team){
  if(!team || team.qbGp === undefined) return 1;
  const p = team.played || 0;
  const missed = Math.max(0, p - team.qbGp);
  return (1 - missed/(p + SHRINKAGE_GAMES)) * Math.max(ADAPT_FLOOR, 1 - ADAPT_DECAY*missed);
}
function qbMalus(team){
  let m;
  if(team && team.qbOut && team.qbBackupOut) m = QB_OUT_MALUS.elite;
  else {
    m = QB_OUT_MALUS[team && team.qbTier] ?? QB_OUT_MALUS.titulaire;
    if(team && team.qbReplacementTier) m += QB_REPLACEMENT_MALUS[team.qbReplacementTier] ?? 0;
  }
  // un QB perdu coûte plus à une équipe qui joue surtout la passe, et moins quand l'absence dure (équipe qui s'adapte)
  m *= styleFactor(team, 'pass', false) * qbDiscount(team);
  return Math.round(m*10)/10;
}
// offAbsences : liste d'absences côté attaque (hors QB, qui a son propre flag dédié ci-dessus),
// ex. [{tier:"elite", pos:"centre titulaire (Elgton Jenkins)"}, {tier:"rotation", pos:"receveur (Tylan Wallace)"}].
// Chaque entrée a SON propre tier (plus de tier unique imposé à tout le groupe) — le malus total
// est la somme des malus individuels, retirée du score attendu de l'équipe elle-même.
// "opponent" (optionnel) : si fourni ET que l'entrée a un "lane" (pass/rush), le malus de CETTE
// entrée est modulé par TENDENCY_MULTIPLIER (0.75x-1.25x) selon la tendance passe/course réelle de
// l'adversaire — un titulaire spécialiste "rush" absent coûte plus cher contre une équipe qui court
// beaucoup, moins contre une équipe qui passe surtout. Une entrée sans "lane" (ou "both") n'est pas
// modulée (x1), c'est le comportement par défaut, identique à avant l'ajout du split passe/course.
// ===================== MALUS DÉGRESSIF DES ABSENCES LONGUES (ajout du 04/10) =====================
// Deux corrections s'appliquent au malus de base de chaque absence (offAbsences / defAbsences),
// qui est donc multiplié par : baselineDiscount x adaptationFactor, puis plafonné (ABSENCE_CAP).
// Chaque absence peut porter "gp" = nombre de matchs joués par ce joueur cette saison ; les matchs
// manqués = team.played - gp (mis à jour tout seul quand l'équipe joue). Sans "gp" (nouvel absent) : pas de réduction.
// 1) baselineDiscount : évite le DOUBLE COMPTAGE. Les stats de l'équipe pèsent played/(played+SHRINKAGE_GAMES)
//    dans l'estimation (le reste = moyenne ligue) et reflètent déjà les matchs joués sans ce joueur :
//    la part déjà "dans les stats" est donc manqués/(played+SHRINKAGE_GAMES), qu'on retire du malus.
// 2) adaptationFactor : une équipe sans un titulaire pour plusieurs semaines COMPENSE (signature, échange,
//    schéma, remplaçant qui monte en puissance) -> le malus est DÉGRESSIF avec la durée de l'absence :
//    -ADAPT_DECAY par match manqué, jusqu'à un plancher ADAPT_FLOOR. Coefficients = choix de modélisation
//    (pas calibrés sur un historique) : à ajuster ici si besoin.
const ADAPT_DECAY = 0.08;  // réduction du malus par match déjà manqué
const ADAPT_FLOOR = 0.55;  // le malus ne descend jamais sous 55 % de sa valeur après correction d'adaptation
// 3) ABSENCE_CAP : plafond du cumul des absences hors QB, par côté (attaque / défense) = malus d'un QB titulaire absent.
const ABSENCE_CAP = 6.5;
function baselineDiscount(team, ab){
  if(!ab || ab.gp === undefined) return 1;
  const p = (team && team.played) || 0;
  const missed = Math.max(0, p - ab.gp);
  return 1 - missed/(p + SHRINKAGE_GAMES);
}
function adaptationFactor(team, ab){
  if(!ab || ab.gp === undefined) return 1;
  const p = (team && team.played) || 0;
  const missed = Math.max(0, p - ab.gp);
  return Math.max(ADAPT_FLOOR, 1 - ADAPT_DECAY*missed);
}
function offAbsenceMalus(team, opponent){
  if(!team || !team.offAbsences || !team.offAbsences.length) return 0;
  return Math.min(ABSENCE_CAP, team.offAbsences.reduce((s,ab)=> {
    const base = KEY_SKILL_MALUS[ab.tier] ?? KEY_SKILL_MALUS.titulaire;
    const disc = baselineDiscount(team, ab) * adaptationFactor(team, ab);
    const mult = styleFactor(team, ab.lane, false);
    return s + base*mult*disc;
  }, 0));
}
// Malus "par unité" utilisé par le prédicteur manuel (saisie d'un simple nombre d'absents, sans
// sélection de tier individuel) : moyenne des tiers déjà connus pour l'équipe (modulée si opponent
// fourni), sinon "titulaire" par défaut.
function offUnitMalus(team, opponent){
  if(team && team.offAbsences && team.offAbsences.length) return offAbsenceMalus(team, opponent)/team.offAbsences.length;
  return KEY_SKILL_MALUS.titulaire;
}

// Malus défensif : miroir du malus offensif ci-dessus, mais appliqué en sens inverse.
// Une absence côté attaque réduit directement la PROPRE production de l'équipe, donc on la
// soustrait de son propre score attendu. Une absence côté défense (titulaire clé en défense :
// safety, cornerback, linebacker, ligne défensive...) ne change rien à la production offensive de
// l'équipe elle-même — elle rend sa défense plus perméable, donc c'est l'ADVERSAIRE qui marque
// davantage. Mêmes paliers que KEY_SKILL_MALUS (même ordre de grandeur d'impact pour un titulaire
// clé absent, attaque ou défense), mais le malus est AJOUTÉ au score attendu de l'adversaire.
const DEFENSE_KEY_MALUS = { elite: 4, titulaire: 2.5, rotation: 1.2 };
// defAbsences : même principe que offAbsences, côté défense — liste de {tier, pos, lane?}, malus
// sommé, modulé par la tendance de l'ADVERSAIRE (celui qui affronte cette défense) si "lane" est
// renseigné : un défenseur "rush" absent pèse plus lourd face à une attaque qui court beaucoup.
function defAbsenceMalus(team, opponent){
  if(!team || !team.defAbsences || !team.defAbsences.length) return 0;
  return Math.min(ABSENCE_CAP, team.defAbsences.reduce((s,ab)=> {
    const base = DEFENSE_KEY_MALUS[ab.tier] ?? DEFENSE_KEY_MALUS.titulaire;
    const disc = baselineDiscount(team, ab) * adaptationFactor(team, ab);
    const mult = opponent ? styleFactor(opponent, ab.lane, true) : 1;
    return s + base*mult*disc;
  }, 0));
}
function defUnitMalus(team, opponent){
  if(team && team.defAbsences && team.defAbsences.length) return defAbsenceMalus(team, opponent)/team.defAbsences.length;
  return DEFENSE_KEY_MALUS.titulaire;
}

// ===================== SPLIT PASSE / COURSE (couche additive) =====================
// Nouvelle couche ajoutée au modèle de base (points bruts + blessures ci-dessus), qui ne le
// remplace pas : elle ajoute un bonus/malus borné selon l'efficacité réelle passe/course de chaque
// attaque face à la défense adverse dans ce secteur précis (EPA, via nflreadpy), au lieu de se fier
// uniquement aux points bruts qui ne distinguent pas "bonne attaque à la passe contre mauvaise
// défense contre la passe" d'un simple hasard de calendrier.
const LEAGUE_AVG_PASS_EPA = 2.3;   // moyenne ligue EPA passe par match, recalculée périodiquement via nflreadpy
const LEAGUE_AVG_RUSH_EPA = -1.6;  // moyenne ligue EPA course par match, idem
const LEAGUE_AVG_RUSH_RATE = 0.45; // part moyenne d'actions au sol dans la ligue
const PASS_EPA_POINT_FACTOR = 0.82; // conversion EPA passe -> points, calibrée sur régression réelle 2021-2024 (2174 matchs, r=0.63)
const RUSH_EPA_POINT_FACTOR = 0.54; // idem pour la course
const PASSRUSH_CAP = 3;             // plafond par secteur (passe OU course), en points — prudence tant que la calibration pré-match n'est pas validée sur plusieurs semaines
const TENDENCY_RANGE = { min: 0.75, max: 1.25 }; // bornes du multiplicateur de tendance adverse
const TENDENCY_SENSITIVITY = 2.5; // sensibilité du multiplicateur à l'écart de tendance adverse (borné par TENDENCY_RANGE de toute façon)

// Lissage générique vers une moyenne ligue donnée (même logique que shrinkToLeagueAverage,
// généralisée pour s'appliquer aussi aux stats EPA passe/course, pas seulement aux points).
function shrinkStat(value, played, leagueAvg){
  const w = SHRINKAGE_GAMES;
  const v = (value===undefined || value===null) ? leagueAvg : value;
  const p = played || 0;
  return (v*p + leagueAvg*w) / (p + w);
}

// Modulation bornée (0.75x-1.25x) du malus d'une absence taguée "pass" ou "rush" selon la tendance
// course/passe réelle de l'ADVERSAIRE affronté. Une absence sans lane (ou "both") n'est pas modulée
// (retourne 1x) : c'est le comportement par défaut pour toute entrée non taguée.
function tendencyMultiplier(opponent, lane){
  if(lane !== 'pass' && lane !== 'rush') return 1;
  const oppRushRate = shrinkStat(opponent && opponent.rushRate, opponent && opponent.played, LEAGUE_AVG_RUSH_RATE);
  const dev = oppRushRate - LEAGUE_AVG_RUSH_RATE; // >0 = adversaire plus "course" que la moyenne ligue
  const sign = (lane === 'rush') ? 1 : -1; // absence "rush" pèse + si adversaire court plus ; "pass" c'est l'inverse
  const mult = 1 + sign*dev*TENDENCY_SENSITIVITY;
  return Math.min(TENDENCY_RANGE.max, Math.max(TENDENCY_RANGE.min, mult));
}

// Écart (mismatch) attaque vs défense dans un secteur donné ("pass" ou "rush"), en unités EPA de
// match, lissé vers la moyenne ligue selon le nombre de matchs joués par chaque équipe.
function laneMismatch(offTeam, defTeam, lane){
  const offKey = lane==='pass' ? 'offPassEPA' : 'offRushEPA';
  const defKey = lane==='pass' ? 'defPassEPA' : 'defRushEPA';
  const leagueAvg = lane==='pass' ? LEAGUE_AVG_PASS_EPA : LEAGUE_AVG_RUSH_EPA;
  const offVal = shrinkStat(offTeam[offKey], offTeam.played, leagueAvg);
  const defVal = shrinkStat(defTeam[defKey], defTeam.played, leagueAvg);
  return offVal + defVal - leagueAvg;
}

// Évite le double-comptage avec le système de blessures : si l'équipe attaquante a DÉJÀ une
// absence taguée dans ce même secteur (offAbsences ou defAbsences confondus), on réduit (pas on
// annule) le bonus/malus stat de moitié, car une partie de l'écart est déjà explicitement comptée
// via le malus d'absence ci-dessus. Pas un simple "on/off" : l'autre moitié du signal statistique
// (mismatch réel d'efficacité, pas seulement l'absence connue) continue de compter.
function laneAbsenceDiscount(team, lane){
  const list = [...(team.offAbsences||[]), ...(team.defAbsences||[])];
  return list.some(ab => ab.lane === lane) ? 0.5 : 1;
}

// Bonus/malus stat pour un secteur donné, borné à ±PASSRUSH_CAP points.
function passRushBonus(offTeam, defTeam, lane){
  const factor = lane==='pass' ? PASS_EPA_POINT_FACTOR : RUSH_EPA_POINT_FACTOR;
  const mismatch = laneMismatch(offTeam, defTeam, lane);
  let pts = factor * mismatch * laneAbsenceDiscount(offTeam, lane);
  return Math.min(PASSRUSH_CAP, Math.max(-PASSRUSH_CAP, pts));
}

// Total passe+course pour l'attaque de "team" face à la défense de "opponent" (somme des 2 secteurs,
// chacun déjà plafonné individuellement -> plafond effectif total de ±2*PASSRUSH_CAP = ±6 points).
function passRushAdjustment(team, opponent){
  return passRushBonus(team, opponent, 'pass') + passRushBonus(team, opponent, 'rush');
}

function predictQuick(homeName, awayName){
  const h=findTeam(homeName), a=findTeam(awayName);
  if(!h||!a) return null;
  const hOff=shrinkToLeagueAverage(h.pointsFor/h.played, h.played);
  const hDef=shrinkToLeagueAverage(h.pointsAgainst/h.played, h.played);
  const aOff=shrinkToLeagueAverage(a.pointsFor/a.played, a.played);
  const aDef=shrinkToLeagueAverage(a.pointsAgainst/a.played, a.played);
  let expHome = LEAGUE_AVG_POINTS*(hOff/LEAGUE_AVG_POINTS)*(aDef/LEAGUE_AVG_POINTS);
  let expAway = LEAGUE_AVG_POINTS*(aOff/LEAGUE_AVG_POINTS)*(hDef/LEAGUE_AVG_POINTS);
  const homeAdvQ = isNeutralGame(homeName, awayName) ? 0 : HOME_ADV_POINTS;
  expHome += homeAdvQ/2; expAway -= homeAdvQ/2;
  if(h.qbOut) expHome -= qbMalus(h);
  if(a.qbOut) expAway -= qbMalus(a);
  expHome -= offAbsenceMalus(h,a);
  expAway -= offAbsenceMalus(a,h);
  expAway += defAbsenceMalus(h,a);
  expHome += defAbsenceMalus(a,h);
  expHome += passRushAdjustment(h,a);
  expAway += passRushAdjustment(a,h);
  expHome += streakImpact(h.name).points;
  expAway += streakImpact(a.name).points;
  expHome=Math.max(3,expHome); expAway=Math.max(3,expAway);
  const spread = expHome-expAway;
  let pHome = normalCDF(spread/SPREAD_STD_DEV);
  let pAway = 1-pHome;
  pHome -= TIE_PROB/2; pAway -= TIE_PROB/2;
  return { expHome:Math.round(expHome), expAway:Math.round(expAway), pHome, pAway };
}


// ===================== LANCEUR (ajouté pour la mise à jour automatique) =====================
// Lit {teams, weeks, games} et renvoie la prévision de chaque match demandé.
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
TEAMS = input.teams; WEEKS_HISTORY = input.weeks;
const out = {};
for (const g of input.games) {
  const p = predictQuick(g.home, g.away);
  if (p) out[g.id] = p;
}
fs.writeFileSync(process.argv[3], JSON.stringify(out));
