# zones_secteur.py — zones actives dans un secteur angulaire

Outil d'aide à la préparation : **ne remplace pas le briefing officiel**.

## En ligne

Page interactive : **https://fredododo.github.io/secteur-vfr/** (saisie du sommet et de l'axe, carte, filtres,
export OpenAir), mise à jour toutes les heures (3 essais par heure).

Le relevé est fait par l'Action GitHub `.github/workflows/releve.yml` : à hh:07, hh:27 et hh:47 UTC (GitHub saute souvent des déclenchements programmés ; les essais d'une même heure se remplacent), à la main
(onglet Actions → « Relevé horaire » → Run workflow) et après chaque modification du code. Elle récupère les relevés
déjà en ligne, lance `releve_france.py` (24 relevés conservés) et publie le dossier sur GitHub Pages. Un relevé en
échec ou incomplet n'est pas publié : la version en ligne reste la précédente. GitHub suspend les tâches
programmées d'un dépôt public sans activité pendant 60 jours : les réactiver depuis l'onglet Actions.

## Lancer

```bash
python zones_secteur.py --lat 483842N --lon 0010559E --axe 120 --date 2026-10-20
```

| Paramètre | Défaut | Rôle |
|---|---|---|
| `--lat`, `--lon` | — | Sommet du secteur (`48.645`, `483842N`, `48:38:42N`…) |
| `--axe` | — | Axe du secteur, degrés vrais |
| `--date` | — | Jour étudié `AAAA-MM-JJ` (aujourd'hui → J+6 environ) |
| `--ouverture` | 30 | Ouverture totale (°) |
| `--rayon-km` | 400 | Rayon du secteur |
| `--debut`, `--fin` | SR−30 / SS+30 | Créneau en UTC `HHMM` (soleil calculé au sommet) |
| `--plafond-fl` | 115 | Ne garde que les zones dont le plancher est sous ce FL |
| `--codes` | `R,W` | Préfixes de code NOTAM Q retenus (restrictions, avertissements) |
| `--sortie` | `resultats` | Dossier de sortie |
| `--json-brut` | — | Sauve aussi les réponses SOFIA brutes |

## Relevé horaire France (`releve_france.py`)

```bash
python releve_france.py
```

Interroge SOFIA avec 17 cylindres de 100 NM couvrant la métropole et la Corse, sur 48 h à partir de maintenant
(pause de 2 s entre requêtes), et écrit `releves/AAAA-MM-JJTHHZ.json` (GeoJSON : géométrie, lignes OpenAir d'origine,
limites, créneaux UTC), `releves/latest.json` et `releves/index.json`. Les 24 relevés les plus récents sont conservés.
Durée : environ 4 min la première fois (téléchargement des PDF SUP AIP), moins ensuite. Taille : environ 2 Mo.

Si un ou plusieurs cylindres SOFIA échouent (après 3 essais), le relevé est abandonné et `latest.json` garde le
dernier relevé complet (option `--accepter-incomplet` pour forcer l'écriture).

Calcul d'une carte depuis le dernier relevé (2 s au lieu de 20 à 45 s, sans interroger SOFIA) :

```bash
python zones_secteur.py --lat 49.0361 --lon 1.5394 --axe 221.3 --date 2026-10-21 --releve
```

`--releve` seul prend `releves/latest.json` ; on peut aussi donner un fichier ou une URL.
Vérifié le 20/10/2026 : résultats identiques au calcul direct pour Jeufosse et La Comté (21/10).

## Page interactive (`releves/carte.html`, `releves/index.html`)

Générées à chaque relevé à partir de `carte_template.html` :
- `carte.html` contient le relevé : fichier autonome, à ouvrir par double-clic ou à partager ;
- `index.html` lit `latest.json` à côté d'elle : version à héberger (GitHub Pages).

La page a quatre onglets :
- **Secteur** : sommet (site, coordonnées décimales ou DMS, ou clic sur la carte), axe (degrés ou « viser un point »),
  ouverture, rayon, jour (dans la fenêtre du relevé), créneau (SR−30 / SS+30 au sommet par défaut) ;
- **Affichage** : fond de carte, filtres (plancher ≥ FL, catégories, zones W et D, pays) ;
- **Zones** : liste cliquable des zones affichées (clic sur la carte = toutes les zones du point) ;
- **Export** : OpenAir avec cercles et arcs natifs, deux boutons : « Zones affichées (après filtres) » et
« Toutes les zones du secteur » (sans filtres, plancher < FL115, suffixe `_complet`).
Vérifié le 20/10/2026 : mêmes zones et créneaux que `zones_secteur.py --releve` (Jeufosse, 21/10).

## Sorties (`resultats/`)

- `zones_<date>.txt` — OpenAir des zones **actives interdites au VFR** (activées par NOTAM ou horaires SUP)
- `zones_w_<date>.txt` — OpenAir des zones W et D actives (avertissements, zones dangereuses : VFR non interdit)
- `zones_sup_<date>.txt` — OpenAir des zones SUP AIP annoncées par *trigger NOTAM* (activation à vérifier)
- `secteur_<date>.txt` — OpenAir du secteur de recherche
- `zones_<date>.csv` — récapitulatif complet (catégorie, horaires du jour, géométrie, SUP AIP + lien PDF, texte)
- `carte_<date>.html` — carte interactive (ouvrir dans un navigateur)

## Fonctionnement

1. Le secteur est couvert par des cylindres de 100 NM interrogés sur **SOFIA-Briefing** (recherche NOTAM « Cylindre », sans compte).
2. Filtres : codes Q, plancher, validité B/C, horaires de l'item D (SR/SS, jours, jours de semaine) sur le créneau.
3. Géométrie, par ordre de préférence :
   - cercle ou polygone écrit dans le texte du NOTAM ;
   - zones définies dans un **SUP AIP** cité par le NOTAM (ou dont le nom de ZRT apparaît dans le texte) : le PDF est téléchargé depuis le SIA (cache `cache/sup/`) et lu avec `pypdf` — points, cercles, arcs, renvois « identique à la LF-R… », tableaux multi-zones ; altitudes prises dans le SUP ; zones dont le plancher dépasse `--plafond-fl` écartées ;
   - zone permanente citée (LF-R602, RTBA R45A…) prise dans la base OpenAir **planeur-net** (AIP France, mise en cache 7 jours dans `cache/`) — pour les NOTAM RTBA, horaires zone par zone ;
   - sinon cercle de la ligne Q (marqué « approx. », pointillé sur la carte).
4. Intersection exacte avec le secteur (projection azimutale équidistante centrée sur le sommet).

## Catégories des zones

| Catégorie | Règle |
|---|---|
| active | NOTAM d'activation (`…CA`, `…LW`…), ou zone d'un SUP AIP à horaires fixes dans le créneau (« active selon les horaires du SUP »). Sur la page et dans les exports, les avertissements (code Q `W…` : voltige, parachutage, drones…) et les zones dangereuses (code Q `RD`) actifs forment la case « Zones W et D » (VFR non interdit) ; les autres zones actives forment « Zones actives (interdites VFR) » |
| SUP AIP - activable, non activée | SUP « activable par NOTAM » (rubrique « Dates et heures d'activité » du PDF, ou trigger NOTAM « … BY NOTAM ») sans NOTAM d'activation publié |
| SUP AIP - inactive (horaires) | SUP à horaires fixes, aucun horaire dans le créneau ; ou SUP activable sans NOTAM dont les créneaux d'activabilité ne tombent pas dans la journée (ex. activable de SS+30 à SR-30) |
| SUP AIP - activables | SUP « activable » (H24, avec préavis, selon protocole…) sans activation par NOTAM : activité réelle connue par radio ou téléphone (fréquences et téléphones du SUP affichés dans le pop-up, première fréquence en `AF`/`AG` dans l'OpenAir). Quand la rubrique donne des créneaux d'activabilité simples, la zone n'est affichée que sur ces créneaux |
| SUP AIP - non interprété | rubrique d'activité non lue (SUP étranger, rubrique absente) |
| (ignoré) itinéraire | SUP dont l'objet est un itinéraire (hélicoptères, VFR de nuit…) : ligne à suivre, pas une zone ; volontairement non interprété, aucune zone produite |
| info | NOTAM d'information (modification, suspension…) |

Une zone de SUP activée par NOTAM n'apparaît qu'une fois (la copie issue du trigger NOTAM est retirée).
Horaires fixes lus : périodes « du … au … », plages « de HHMM à HHMM » / « HHMM-HHMM » / H24, lever et coucher du soleil (« SR-30 », « SS+30 », « SR+2HR », « SS-1HR », « SR PLUS 02H00 », calculés à la position du NOTAM ; une plage de nuit commencée la veille compte pour la journée), jours (« sauf samedi, dimanche »,
« uniquement le … », « du lundi au vendredi »), décalage « été : -1 h » (ou « - 01H00 »), « sauf / hors jours fériés » et
« hors veilles de jours fériés », tableaux de jours précis (« MARS 2026 : 24,26 : 0700-1200 ET 1700-2259 ») ; quand le tableau couvre plusieurs zones dont les colonnes sont perdues à la lecture du PDF, chaque zone garde les jours compris dans sa propre période (« ZRT … - du … au … »). Les jours fériés de France métropolitaine sont calculés (dates fixes + Pâques,
Ascension, Pentecôte), sans téléchargement ; vérifié identique au calendrier officiel (calendrier.api.gouv.fr)
pour 2026 à 2028. Les jours fériés propres à l'Alsace-Moselle (vendredi saint, 26 décembre) ne sont pas comptés.
Page : seules les zones actives et « à vérifier » sont affichées par défaut.

## Horaires des NOTAM (item D)

Lus : plages HHMM-HHMM, SR/SS (± minutes), H24, DLY, jours de semaine (MON-FRI…), jours du mois, périodes sur deux mois
(« SEP 10-OCT 23 »), HOL (jour férié), et les exceptions EXC : jours fériés (« EXC HOL », « EXC PUB HOL »),
jours (« EXC SAT SUN », « EXC WEEKENDS » = samedi et dimanche), dates (« EXC JUL 14 », « EXC APR 06 MAY 01 08 »),
plages (« H24 EXC 1200-1300 »). Une virgule sépare les clauses, chacune avec ses propres EXC.
Périodes continues sur plusieurs jours, découpées jour par jour : « MON 0645-FRI 1045 » (chaque semaine),
« FRI 1800-MON 0600 », « 12 2200-15 2200 », « OCT 30 2200-NOV 02 0600 ».
Un format non reconnu garde la zone sur toute sa validité, marquée « horaire à vérifier ».
Relevé du 20/10/2026 16:00Z : 0 horaire non interprété sur 838 zones.

Dans le pop-up, « lire le SUP » est un lien vers le PDF officiel du SUP AIP sur le site du SIA.

## RTBA et cartes AZBA

La référence de l'activité RTBA est la carte AZBA du SIA (https://www.sia.aviation-civile.gouv.fr/schedules).
Le script lit les NOTAM d'activation RTBA (série Z, « ZONES AIRFORCE RTBA ACT »), qui donnent les mêmes créneaux,
zone par zone (vérifié le 19/10/2026 : 23 zones identiques au tableau AZBA, y compris les doubles créneaux).
Libellé : `RTBA R142A NIEVRE EST`, limites verticales de la zone (AIP), créneaux propres à chaque zone.
L'API interne de la carte AZBA n'est pas utilisée (accès protégé) : pour un contrôle, ouvrir le tableau
« Liste des zones activées » de la carte AZBA.

## Géométrie OpenAir

Les zones gardent leur forme publiée : cercles en `V X=` + `DC`, arcs des SUP AIP et de la base AIP en `V D=` / `V X=` + `DB` / `DA`,
le secteur de recherche en `DA`. Les polygones `DP` ne servent que quand la source ne donne que des points.
Vérifié le 20/10/2026 : 56 zones relues depuis l'OpenAir, écart < 300 m avec la géométrie de la carte.

## Zones belges et luxembourgeoises

Géométrie des zones activées par NOTAM belges / luxembourgeois (EBR, EBD, TSA, TRA, HTA, LFA, ELTSA, CBA) :
fichier OpenAir « BELLUX WEEK ALL » de la LVZC (fédération belge de vol à voile), **non officiel**, téléchargé depuis
la dernière édition publiée sur lvzc.be (cache `cache/belgique_lvzc.txt`, 7 jours). Pour un NOTAM belge, cette base
est consultée avant la base française. Les zones à cheval sur la frontière gardent leurs parties BEL et FRA.
Limites du fichier LVZC : rien au-dessus du FL095, rien en mer, pas de zones uniquement de nuit ; ces zones restent
en cercle approximatif (ligne Q). Référence officielle : eAIP Belgique (skeyes), ENR 5.1 / 5.2, non lisible par
script (accès automatique refusé).

### Compléments officiels (`zones_complements.txt`)

Zones recopiées à la main depuis l'AIP officiel quand elles manquent dans les bases planeur-net et LVZC
(au-dessus du FL095, en mer…). Format OpenAir, prioritaire sur les autres bases ; chaque zone porte une ligne
`* Source : …` reprise dans le pop-up. À vérifier à chaque cycle AIRAC.
Chaque zone porte aussi `* Pays : …` : elle n'est reprise que pour un NOTAM du même pays.
Contenu actuel (Belgique, AIP valide à partir du 01/10/2026) : TSA26A ARDENNES 01 (ENR 5.2, FL95-UNL) et
EBR17D LOMBARDSIJDE SECTOR DELTA (ENR 5.1, SFC-500 ft AMSL, polygone + arc de 2,5 NM, bord sud sur la plage).

## Fond de carte

Esri (plan ou topographique) par défaut, OpenTopoMap en option. Les tuiles openstreetmap.org ne sont pas utilisées :
elles exigent un en-tête Referer et sont refusées quand la carte est ouverte par double-clic
(« not following the tile usage policy »). CARTO demande désormais une clé d'API.

## Limites connues

- Les créneaux déjà terminés ne sont plus renvoyés par SOFIA : lancé à 10:01Z, le script ne montre plus une zone RTBA active 0730-1000.

- SOFIA ne fournit que les NOTAM à venir : le créneau démarre à l'heure courante si le jour est aujourd'hui ; pas d'historique ; au-delà de ~J+6 la requête échoue. Les NOTAM d'activation sont souvent publiés 48 h (parfois D-4/D-7) avant.
- Zones SUP AIP : les limites « à l'exclusion de… » ne sont pas soustraites, les tronçons suivant une frontière ou une côte sont tracés en ligne droite (signalé dans la colonne `geometrie`). Les créneaux d'activabilité rédigés de façon complexe (plages d'exclusion « sauf pendant les créneaux suivants », périodes « du lundi 10H00 au vendredi 10H00 ») ne sont pas lus : la zone reste activable toute la journée. Si un PDF ne se lit pas, retour au cercle de la ligne Q.
- Dépendance : `pip install pypdf` (sans elle, le script fonctionne mais sans géométrie SUP).
- Items D complexes (`EXC`, plages de mois…) : zone gardée et marquée « horaire à vérifier ».
- Les NOTAM « info » (suspension, modification) sont listés dans le CSV et la carte, pas dans l'OpenAir.
