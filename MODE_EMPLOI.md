# Secteur VFR — mode d'emploi

**Page :** https://fredododo.github.io/secteur-vfr/

Sans compte ni installation : la page fonctionne dans n'importe quel navigateur, sur PC comme sur téléphone.

> ⚠️ **Outil d'aide à la préparation : il ne remplace pas le briefing officiel** (SOFIA, NOTAM, SUP AIP, carte AZBA).
> Vérifiez toujours avant le vol.

## À quoi ça sert

La page affiche les **zones aériennes actives** (NOTAM, SUP AIP dont les ZRT, RTBA) dans un **secteur en forme de
cône** qui part d'un point de décollage dans une direction, et permet de les **exporter au format OpenAir** (XCTrack,
FlySkyHy, SeeYou…).

Les données sont relevées **toutes les heures** sur SOFIA-Briefing (SIA/DGAC), pour toute la France, sur les 48 h
suivantes. Seules les zones dont le plancher est **sous le FL115** sont relevées.

## 1. Onglet « Secteur » : définir le vol

- **Sommet** (point de départ) :
  - tapez une partie du **nom du site, de la commune ou du code postal** dans le champ **Site** (sans vous soucier
    des accents ni des majuscules), puis choisissez dans la liste. Elle contient les ~1 400 sites de **décollage et de
    treuil** de la liste officielle de la **FFVL** (data.gouv.fr), avec altitude, orientations et, le cas échéant,
    « autorisation requise » ou « interdit ». Exemple : Jeufosse y figure sous son nom officiel
    « NOTRE-DAME-DE-LA-MER - LES-GRANDES-BRUYERES ». Pour changer de site, cliquez dans le champ (le texte est
    sélectionné, la frappe le remplace) ou sur **✕** pour l'effacer ;
  - ou saisissez la **latitude** et la **longitude** : `49.0361` ou `490210N` ;
  - ou cliquez sur **« Placer le sommet sur la carte »**.
- **Axe** : direction du vol, en degrés vrais. Quand vous choisissez un site, la page propose l'axe **sous le vent**,
  à l'opposé de ses orientations de décollage (site O-NO → 113°). Modifiez-le à la main ou cliquez sur
  **« Viser un point sur la carte »** puis sur la destination.
- **Ouverture** (30° par défaut) et **rayon** (400 km par défaut, mesuré depuis le site le long de l'axe).
- **Largeur au départ** (10 km par défaut) : le secteur part d'un **demi-cercle de ce diamètre derrière le site**,
  puis s'ouvre selon l'ouverture choisie. Toutes les zones à moins de 5 km du décollage, devant comme derrière, sont
  donc prises en compte. Avec 0, le secteur est un cône dont la pointe est le site.
- **Jour** et **créneau**, en **UTC**. Par défaut : du lever du soleil −30 min au coucher +30 min. Une fois ce
  créneau passé, la page propose le lendemain.
- Le secteur est **recalculé automatiquement** dès qu'un champ est modifié (en quittant le champ ou avec Entrée) ;
  les zones trouvées sont dans l'onglet **Zones**.

## 2. Onglet « Affichage » : filtrer

- **Fond de carte** : plan, IGN, topographique…
- **Masquer plancher ≥ FL** : masque les zones trop hautes pour vous (FL65 par défaut).
- **Catégories** — le chiffre à droite de chaque case indique combien de zones elle fait apparaître :
  - **Zones actives (interdites VFR)** : zones réglementées activées par NOTAM ou par les horaires d'un SUP ;
  - **SUP activables** : zones activables sans NOTAM ; l'activité réelle se connaît par radio ou par téléphone
    (numéros dans la fiche de la zone) ;
  - **Zones W et D** : voltige, parachutage, drones, zones dangereuses. Le VFR n'y est pas interdit, mais vigilance ;
  - **SUP inactifs** et **NOTAM d'information** : masqués par défaut. Les interdictions qui ne visent que les drones
    (survol interdit aux drones, ZIT drones) et les lâchers de ballons sonde sont classés en NOTAM
    d'information.
- **Pays** : France, Belgique, Allemagne…
- **Afficher aussi les créneaux terminés** : décochée, la page ne montre que les activités en cours ou à venir ;
  cochée, elle garde les activités déjà terminées depuis l'heure du relevé, marquées « terminé ».

Vos réglages (secteur, filtres, onglet) sont gardés **par votre navigateur**, sur votre appareil : rien n'est
envoyé en ligne. Chaque personne, et chaque appareil (téléphone, PC), a donc ses propres réglages ; en navigation
privée ou après effacement des données du site, on repart des réglages par défaut.

## 3. Onglet « Zones » : consulter

- La **liste** donne les zones affichées, avec leurs limites verticales et leurs horaires du jour. Un clic sur une
  zone la centre sur la carte.
- Un **clic sur la carte** liste toutes les zones présentes en ce point. La fiche donne le NOTAM, les horaires, les
  fréquences, les téléphones, et un lien **« lire le SUP »** vers le PDF officiel.
- **« ✕ Carte en grand »** masque le panneau pour voir la carte en plein écran.

## 4. Onglet « Export » : fichier OpenAir

- **Zones affichées (après filtres)** : exactement ce que montre la carte.
- **Toutes les zones du secteur** : tout, sans tenir compte des filtres.
- **France : tout le relevé** : toutes les zones du relevé (France et bande de 100 km autour), sans tenir compte
  du secteur ni des filtres, toutes catégories (la catégorie de chaque zone est en commentaire), avec leurs horaires
  sur les 48 h du relevé en lignes `AA`. Fichier d'environ 400 Ko et 900 zones.

Le fichier `.txt` est au format **OpenAir extended** (comme le fichier planeur-net de la FFVP) : classe `AC UNC`
(espace non classé) et type `AY R` (réglementée), `AY Q` (dangereuse, zones W et D) ou `AY P` (interdite). Il garde
les cercles et arcs d'origine ; la première fréquence de la zone est reprise dans les champs `AF`/`AG`.

Une zone décrite par le NOTAM lui-même (voltige, aéromodélisme, parachutage…) porte le **numéro du NOTAM** comme
nom (ex. « W0240/26 ») ; le lieu (« JOIGNY AERODROME ») est donné à côté dans la liste et la fiche. Les zones
d'une base ou d'un SUP gardent leur nom publié.

Pour une zone de SUP AIP, le nom de la zone se termine par le numéro du SUP (ex. « (R0750/26 SUP 042/26) »),
visible dans les infos de la zone de l'instrument ; le lien du PDF officiel est en commentaire dans le fichier. En
vol, avec du réseau, la fiche de la zone sur la page donne aussi le lien « lire le SUP ».

Chaque zone porte aussi ses **horaires d'activation** (lignes `AA`, en UTC, tous les créneaux du relevé, soit
jusqu'à 48 h) et son champ D en commentaire. Les logiciels qui lisent `AA` (SeeYou ; XCSoar dans une version
future) affichent ou masquent la zone selon l'heure. Les autres les ignorent : testé avec **XCSoar 7.44**, qui
affiche toutes les zones en permanence — vérifiez alors l'horaire dans le nom de la zone.

## Bon à savoir

- Le relevé couvre la France et une **bande de 100 km** autour. Si le secteur va plus loin à l'étranger, la partie
  non couverte est **grisée** (pointillé orange) avec un avertissement : les NOTAM n'y sont pas connus, une absence
  de zone n'y veut rien dire.
- L'**âge du relevé** est indiqué en haut (« il y a 20 min »). Au-delà de 2 h, la mise à jour a eu un incident.
- Tous les horaires sont en **UTC** : heure française = UTC + 1 h en hiver, UTC + 2 h en été.
- Le bas de la page indique l'**édition de chaque base de zones** utilisée (France, Belgique-Luxembourg,
  Allemagne). Une édition de plus de 2 mois est signalée « édition ancienne », aussi dans la fiche des zones
  concernées : vérifiez alors leur tracé dans l'AIP.
- Les **zones belges et luxembourgeoises** viennent d'une base non officielle (LVZC) complétée par l'AIP belge, les
  **zones allemandes** (ED-R, TRA, ED-D) de la base du DAeC, non officielle elle aussi, les **zones néerlandaises**
  (TSA, TRA, EHR, EHD, EHP) des données officielles de LVNL, les **zones suisses, italiennes et espagnoles** de
  la base communautaire openAIP (non officielle) ; c'est indiqué dans la fiche
  de chaque zone. Ailleurs à l'étranger, une zone citée sans coordonnées est tracée avec le cercle du NOTAM
  (« approx. », pointillé).
- Une zone désactivée **avant l'heure du relevé** n'y figure plus, même avec « créneaux terminés ».
- Pour les **SUP AIP**, ce sont les dates du **listing des SUP du SIA** qui font foi (elles peuvent être corrigées
  en rouge, plus courtes ou plus longues que dans le PDF du SUP). La page les applique ; un SUP en vigueur sans
  trigger NOTAM apparaît aussi (« SUP en vigueur d'après le listing SIA »). Quand le listing a prolongé un SUP, la
  fiche indique « horaires du PDF reconduits, à vérifier ». Une date corrigée par le SIA est signalée en orange
  dans la fiche de la zone (« Date de fin du SUP … corrigée par le SIA »). La liste est relue à chaque relevé ; le bas de la page
  donne l'heure de lecture. Si le site du SIA n'a pas répondu, la page l'indique sous l'heure du relevé
  (« Liste des SUP AIP non mise à jour ») : les dates des SUP sont alors celles de la dernière lecture.
