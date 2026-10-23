#!/usr/bin/env python3
"""
Releve horaire des zones actives sur toute la France (metropole + Corse).

Interroge SOFIA-Briefing avec des cylindres de 100 NM qui couvrent le territoire et une bande de 100 km autour, sur une fenetre
de 48 h a partir de maintenant, puis ecrit un fichier GeoJSON pret a l'emploi :

    releves/AAAA-MM-JJTHHZ.json   un fichier par releve (les 24 plus recents sont conserves)
    releves/latest.json           copie du dernier releve
    releves/index.json            liste des releves disponibles

Chaque zone porte sa geometrie (polygone densifie + lignes OpenAir d'origine), ses limites
verticales et ses creneaux d'activite UTC sur la fenetre.

    python releve_france.py                 # releve complet
    python releve_france.py --garder 24     # nombre de releves conserves

OUTIL D'AIDE : ne remplace pas le briefing officiel.
"""
import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import time

import pages
import zones_secteur as zs

UTC = dt.timezone.utc
HERE = os.path.dirname(os.path.abspath(__file__))

# Contour grossier de la France metropolitaine et de la Corse (lat, lon), un peu elargi vers l'exterieur.
FRANCE = [
    (51.15, 2.45), (50.80, 3.20), (50.45, 4.25), (49.95, 4.95), (49.55, 5.90), (49.45, 6.45),
    (49.15, 7.05), (49.05, 8.25), (48.00, 7.65), (47.55, 7.65), (47.00, 6.95), (46.40, 6.15),
    (46.20, 6.85), (45.90, 7.05), (45.20, 7.15), (44.40, 6.95), (44.10, 7.75), (43.75, 7.55),
    (43.00, 6.30), (43.30, 4.50), (43.30, 3.30), (42.40, 3.25), (42.45, 1.70), (42.75, 0.00),
    (43.30, -1.85), (43.60, -1.55), (45.60, -1.30), (46.30, -1.85), (47.25, -2.65), (47.75, -4.45),
    (48.40, -4.85), (48.85, -3.50), (48.65, -1.60), (49.75, -1.95), (49.40, -1.00), (49.50, 0.10),
    (50.00, 1.40), (50.95, 1.55),
]
CORSE = [(43.05, 9.45), (42.00, 9.60), (41.30, 9.20), (41.90, 8.55), (42.65, 8.55)]
Q_RADIUS_NM = 100
BANDE_KM = 100          # bande couverte autour de la France (zones etrangeres proches)


def _dist_seg(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy or 1)))
    return math.hypot(px - x1 - t * dx, py - y1 - t * dy)


def couverture(frame, radius_nm=Q_RADIUS_NM, bande_km=BANDE_KM):
    """Centres de cylindres (grille hexagonale) couvrant la France, la Corse et une bande de `bande_km` autour
    (secteurs qui partent pres d'une frontiere vers l'etranger)."""
    r = radius_nm * zs.NM * 0.97
    bande = bande_km * 1000
    polys = [[frame.xy(*p) for p in FRANCE], [frame.xy(*p) for p in CORSE]]
    xs = [x for pg in polys for x, _ in pg]
    ys = [y for pg in polys for _, y in pg]
    # Points a couvrir : grille fine sur la France, la Corse et la bande autour
    pts = []
    step = 20000
    for x in range(int(min(xs) - bande), int(max(xs) + bande) + step, step):
        for y in range(int(min(ys) - bande), int(max(ys) + bande) + step, step):
            if any(zs.point_in_poly(x, y, pg) for pg in polys) or any(
                    _dist_seg(x, y, *pg[i - 1], *pg[i]) <= bande for pg in polys for i in range(len(pg))):
                pts.append((x, y))
    # Grille hexagonale ; on ne garde que les centres utiles, puis on comble les trous eventuels
    dx = r * math.sqrt(3) * 0.98
    dy = r * 1.5 * 0.98
    centres = []
    row = 0
    y = min(ys) - bande - r
    while y <= max(ys) + bande + r:
        x = min(xs) - bande - r + (dx / 2 if row % 2 else 0)
        while x <= max(xs) + bande + r:
            if any(math.hypot(px - x, py - y) <= r for px, py in pts):
                centres.append((x, y))
            x += dx
        y += dy
        row += 1
    for p in pts:
        if not any(math.hypot(p[0] - cx, p[1] - cy) <= r for cx, cy in centres):
            centres.append(p)
    return [frame.ll(x, y) for x, y in centres]


to_feature = zs.zone_feature      # conversion zone -> GeoJSON, partagee avec zones_secteur.py
merge_slots = zs.merge_slots


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--heures", type=int, default=48, help="Fenetre couverte a partir de maintenant (48)")
    ap.add_argument("--codes", default="R,W", help="Prefixes de code Q retenus (R,W)")
    ap.add_argument("--dossier", default=os.path.join(HERE, "releves"), help="Dossier des releves")
    ap.add_argument("--garder", type=int, default=24, help="Nombre de releves conserves (24)")
    ap.add_argument("--pause", type=float, default=2.0, help="Pause entre deux requetes SOFIA, en s (2)")
    ap.add_argument("--accepter-incomplet", action="store_true",
                    help="Ecrire le releve meme si des cylindres ont echoue (deconseille)")
    ap.add_argument("--republier", action="store_true",
                    help="Sans interroger SOFIA : regenere les pages (index.html, carte.html, sites.json) a partir du "
                         "latest.json deja present dans le dossier (modification de la page seulement)")
    a = ap.parse_args()

    if a.republier:
        with open(os.path.join(a.dossier, "latest.json"), encoding="utf-8") as f:
            snap = json.load(f)
        pages.ecrire_pages(snap, a.dossier, zs.sites_ffvl(os.path.join(HERE, "cache")))
        print(f"Pages regenerees a partir du releve {snap['meta']['genere']} (aucune requete SOFIA)")
        return

    t0 = time.time()
    now = dt.datetime.now(UTC)
    start = (now + dt.timedelta(minutes=3)).replace(second=0, microsecond=0)
    end = start + dt.timedelta(hours=a.heures)
    frame = zs.Frame(46.5, 2.5)
    centres = couverture(frame)
    print(f"Releve France {now:%Y-%m-%d %H:%MZ} : fenetre {start:%d/%m %H:%MZ} -> {end:%d/%m %H:%MZ}, "
          f"{len(centres)} cylindres de {Q_RADIUS_NM} NM")

    notams, erreurs = {}, []
    for i, (la, lo) in enumerate(centres, 1):
        for essai in (1, 2, 3):
            try:
                p = zs.sofia_query(la, lo, Q_RADIUS_NM, start, int((end - start).total_seconds() // 60),
                                   0, zs.PLAFOND_FL, "V")
                k = 0
                for n in zs.iter_notams(p):
                    if n["id"] not in notams:
                        notams[n["id"]] = n
                        k += 1
                print(f"  {i:2d}/{len(centres)} {zs.to_sofia(la, lo)} : {k} nouveaux NOTAM")
                break
            except Exception as e:
                if essai == 3:
                    erreurs.append(f"{zs.to_sofia(la, lo)} : {e}")
                    print(f"  {i:2d}/{len(centres)} {zs.to_sofia(la, lo)} : ECHEC ({e})")
                else:
                    time.sleep(10 * essai)
        time.sleep(a.pause)
    print(f"Total : {len(notams)} NOTAM distincts")
    if not notams:
        sys.exit("Aucun NOTAM recu : releve abandonne (le precedent est conserve).")
    if erreurs and not a.accepter_incomplet:
        # Un releve incomplet ne remplace jamais le dernier releve complet (latest.json reste inchange)
        print("\n".join(f"  echec : {x}" for x in erreurs))
        sys.exit(f"{len(erreurs)} cylindre(s) en echec : releve incomplet abandonne, le precedent est conserve.")

    ctx = zs.prepare_context(notams)
    for b in ctx["db"].infos:
        print(f"  base {b['base']} : {b['edition']}, telechargee {b.get('telecharge')}"
              + ("  ** EDITION ANCIENNE **" if b.get("ancienne") else ""))
    zones = {}
    d = start.date()
    while dt.datetime(d.year, d.month, d.day, tzinfo=UTC) < end:
        d0 = dt.datetime(d.year, d.month, d.day, tzinfo=UTC)
        w0, w1 = max(d0, start), min(d0 + dt.timedelta(days=1), end)
        for r in zs.build_zones(notams, ctx, d, w0, w1, start, a.codes, zs.PLAFOND_FL, "V"):
            if (r["_id"], r["categorie"]) in zones:
                zones[(r["_id"], r["categorie"])]["_slots"] += r["_slots"]
            else:
                zones[(r["_id"], r["categorie"])] = r
        d += dt.timedelta(days=1)
    for r in zones.values():
        r["_slots"] = merge_slots(r["_slots"])

    feats = [to_feature(r) for r in zones.values()]
    snap = {
        "type": "FeatureCollection",
        "meta": {
            "genere": now.strftime("%Y-%m-%dT%H:%MZ"),
            "fenetre": [start.strftime("%Y-%m-%dT%H:%MZ"), end.strftime("%Y-%m-%dT%H:%MZ")],
            "plafond_fl": zs.PLAFOND_FL,
            "codes": a.codes,
            "nb_notam": len(notams),
            "nb_zones": len(feats),
            "cylindres": [[round(la, 4), round(lo, 4)] for la, lo in centres],
            "rayon_nm": Q_RADIUS_NM,                 # rayon des cylindres : zone relevee = leur reunion
            "bases": ctx["db"].infos,                # editions des bases de zones permanentes
            "sup_liste": ctx["sups"].info,           # lecture de la liste des SUP AIP du SIA (copie si echec)
            "erreurs": erreurs,
            "sources": ["SOFIA-Briefing (SIA/DGAC)", "SUP AIP (SIA)", "planeur-net (AIP France)"],
            "avertissement": "Outil d'aide : ne remplace pas le briefing officiel.",
        },
        "features": feats,
    }
    os.makedirs(a.dossier, exist_ok=True)
    name = now.strftime("%Y-%m-%dT%HZ") + ".json"
    tmp = os.path.join(a.dossier, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, os.path.join(a.dossier, name))
    with open(os.path.join(a.dossier, "latest.json.tmp"), "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(os.path.join(a.dossier, "latest.json.tmp"), os.path.join(a.dossier, "latest.json"))

    pages.ecrire_pages(snap, a.dossier, zs.sites_ffvl(os.path.join(HERE, "cache")))

    # Rotation : on ne garde que les N releves les plus recents (fichiers AAAA-MM-JJTHHZ.json uniquement)
    files = sorted(f for f in os.listdir(a.dossier) if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}Z\.json", f))
    for f in files[:-a.garder]:
        os.remove(os.path.join(a.dossier, f))
    files = files[-a.garder:]
    with open(os.path.join(a.dossier, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"dernier": name, "releves": files[::-1]}, f, ensure_ascii=False, indent=1)

    size = os.path.getsize(os.path.join(a.dossier, name)) / 1e6
    cats = {}
    for ft in feats:
        cats[ft["properties"]["categorie"]] = cats.get(ft["properties"]["categorie"], 0) + 1
    print(f"Releve ecrit : {name} ({size:.1f} Mo, {len(feats)} zones : "
          + ", ".join(f"{k} {v}" for k, v in cats.items()) + f") en {time.time() - t0:.0f} s"
          + (f" - {len(erreurs)} cylindre(s) en echec" if erreurs else ""))


if __name__ == "__main__":
    main()
