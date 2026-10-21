#!/usr/bin/env python3
"""
Releve horaire des zones actives sur toute la France (metropole + Corse).

Interroge SOFIA-Briefing avec des cylindres de 100 NM qui couvrent le territoire, sur une fenetre
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


def couverture(frame, radius_nm=Q_RADIUS_NM):
    """Centres de cylindres (grille hexagonale) couvrant la France et la Corse."""
    r = radius_nm * zs.NM * 0.97
    polys = [[frame.xy(*p) for p in FRANCE], [frame.xy(*p) for p in CORSE]]
    xs = [x for pg in polys for x, _ in pg]
    ys = [y for pg in polys for _, y in pg]
    # Points a couvrir : grille fine a l'interieur des contours + les contours eux-memes
    pts = []
    step = 20000
    for x in range(int(min(xs)), int(max(xs)) + step, step):
        for y in range(int(min(ys)), int(max(ys)) + step, step):
            if any(zs.point_in_poly(x, y, pg) for pg in polys):
                pts.append((x, y))
    for pg in polys:
        for i in range(len(pg)):
            (x1, y1), (x2, y2) = pg[i - 1], pg[i]
            for k in range(10):
                pts.append((x1 + (x2 - x1) * k / 10, y1 + (y2 - y1) * k / 10))
    # Grille hexagonale ; on ne garde que les centres utiles, puis on comble les trous eventuels
    dx = r * math.sqrt(3) * 0.98
    dy = r * 1.5 * 0.98
    centres = []
    row = 0
    y = min(ys) - r
    while y <= max(ys) + r:
        x = min(xs) - r + (dx / 2 if row % 2 else 0)
        while x <= max(xs) + r:
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


def merge_slots(slots):
    out = []
    for a, b in sorted(slots):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def ecrire_pages(snap, dossier):
    """Page interactive : carte.html (releve integre, autonome, a partager) et index.html (lit latest.json,
    pour l'hebergement en ligne)."""
    with open(os.path.join(HERE, "carte_template.html"), encoding="utf-8") as f:
        tpl = f.read()
    for name, content in (("carte.html", zs.page_html(snap)), ("index.html", tpl)):
        tmp = os.path.join(dossier, name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, os.path.join(dossier, name))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--heures", type=int, default=48, help="Fenetre couverte a partir de maintenant (48)")
    ap.add_argument("--codes", default="R,W", help="Prefixes de code Q retenus (R,W)")
    ap.add_argument("--dossier", default=os.path.join(HERE, "releves"), help="Dossier des releves")
    ap.add_argument("--garder", type=int, default=24, help="Nombre de releves conserves (24)")
    ap.add_argument("--pause", type=float, default=2.0, help="Pause entre deux requetes SOFIA, en s (2)")
    ap.add_argument("--accepter-incomplet", action="store_true",
                    help="Ecrire le releve meme si des cylindres ont echoue (deconseille)")
    a = ap.parse_args()

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

    ecrire_pages(snap, a.dossier)

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
