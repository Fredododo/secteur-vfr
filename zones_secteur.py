#!/usr/bin/env python3
"""
Zones actives (NOTAM / SUP AIP) dans un secteur angulaire, pour une journee.

Source : SOFIA-Briefing (SIA/DGAC), recherche NOTAM "cylindre" (sans compte),
         + liste des SUP AIP Metropole du SIA (titres et liens PDF).

Exemple :
    python zones_secteur.py --lat 483842N --lon 0010559E --axe 120 --date 2026-10-20

Sorties (dossier --sortie, par defaut ./resultats) :
    zones_<date>.txt      OpenAir des zones retenues
    secteur_<date>.txt    OpenAir du secteur de recherche
    zones_<date>.csv      Recapitulatif (separateur ;)
    carte_<date>.html     Carte interactive (Leaflet)

OUTIL D'AIDE : ne remplace pas le briefing officiel.
"""
import argparse
import csv
import datetime as dt
import html
import json
import math
import os
import re
import sys
import unicodedata
import uuid

import requests
from pyproj import Geod, Proj

from pages import page_html

SOFIA = "https://sofia-briefing.aviation-civile.gouv.fr"
SUP_LIST = "https://www.sia.aviation-civile.gouv.fr/documents/supaip/aip/id/6"
GEOD = Geod(ellps="WGS84")
NM = 1852.0
PLAFOND_FL = 115     # limite fixe : releve SOFIA et exports OpenAir = zones dont le plancher est sous le FL115
UTC = dt.timezone.utc

# --------------------------------------------------------------------------
# Coordonnees
# --------------------------------------------------------------------------

def parse_coord(txt, is_lat):
    """'48.645', '483842N', '48:38:42N', '0010559E', '-1.5' -> degres decimaux."""
    t = txt.strip().upper().replace(" ", "").replace(":", "")
    m = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)", t)
    if m:
        return float(m.group(1))
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([NSEW])", t)
    if not m:
        raise ValueError(f"Coordonnee illisible : {txt}")
    digits, hemi = m.groups()
    ip, _, frac = digits.partition(".")
    dd = 2 if is_lat else 3
    ip = ip.zfill(dd)
    deg = int(ip[:dd])
    rest = ip[dd:]
    mins = int(rest[:2]) if len(rest) >= 2 else 0
    secs = float(rest[2:] + ("." + frac if frac else "")) if len(rest) > 2 else 0.0
    v = deg + mins / 60 + secs / 3600
    return -v if hemi in "SW" else v


def to_sofia(lat, lon):
    """Format SOFIA : DDMMN / DDDMME (minutes arrondies)."""
    def f(v, d, pos, neg):
        a = abs(v)
        deg = int(a)
        mn = int(round((a - deg) * 60))
        if mn == 60:
            deg, mn = deg + 1, 0
        return f"{deg:0{d}d}{mn:02d}{pos if v >= 0 else neg}"
    return f(lat, 2, "N", "S"), f(lon, 3, "E", "W")


def to_openair(v, is_lat):
    a = abs(v)
    d = int(a)
    m = int((a - d) * 60)
    s = round(((a - d) * 60 - m) * 60)
    if s == 60:
        m, s = m + 1, 0
    if m == 60:
        d, m = d + 1, 0
    h = ("N" if v >= 0 else "S") if is_lat else ("E" if v >= 0 else "W")
    return f"{d:0{2 if is_lat else 3}d}:{m:02d}:{s:02d} {h}"


COORD_RE = re.compile(
    r"(\d{2})(\d{2})(\d{2}(?:[.,]\d+)?)\s?([NS])[\s,/-]*(\d{3})(\d{2})(\d{2}(?:[.,]\d+)?)\s?([EW])")
COORD_RE_SHORT = re.compile(r"\b(\d{2})(\d{2})([NS])\s?(\d{3})(\d{2})([EW])\b")


def coords_in_text(text):
    out = []
    for m in COORD_RE.finditer(text):
        la = int(m[1]) + int(m[2]) / 60 + float(m[3].replace(",", ".")) / 3600
        lo = int(m[5]) + int(m[6]) / 60 + float(m[7].replace(",", ".")) / 3600
        out.append((m.start(), -la if m[4] == "S" else la, -lo if m[8] == "W" else lo))
    return out

# --------------------------------------------------------------------------
# Soleil (algorithme NOAA simplifie, precision ~1-2 min)
# --------------------------------------------------------------------------

def sun_times(date, lat, lon):
    n = date.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1)
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    la = math.radians(lat)
    cos_ha = (math.cos(math.radians(90.833)) / (math.cos(la) * math.cos(decl))
              - math.tan(la) * math.tan(decl))
    cos_ha = max(-1.0, min(1.0, cos_ha))
    ha = math.degrees(math.acos(cos_ha))
    base = dt.datetime(date.year, date.month, date.day, tzinfo=UTC)
    sr = base + dt.timedelta(minutes=720 - 4 * (lon + ha) - eqt)
    ss = base + dt.timedelta(minutes=720 - 4 * (lon - ha) - eqt)
    return sr, ss

# --------------------------------------------------------------------------
# Geometrie : projection azimutale equidistante centree sur le sommet.
# Dans ce repere, le secteur est un vrai secteur circulaire.
# --------------------------------------------------------------------------

class Frame:
    def __init__(self, lat, lon):
        self.p = Proj(proj="aeqd", lat_0=lat, lon_0=lon, datum="WGS84", units="m")

    def xy(self, lat, lon):
        return self.p(lon, lat)

    def ll(self, x, y):
        lon, lat = self.p(x, y, inverse=True)
        return lat, lon


class Ring(list):
    """Contour (lat, lon) densifie, pour les calculs et la carte, qui garde sa description OpenAir
    d'origine (oa : lignes V X= / DC / DA / DB / DP). Sans oa, l'OpenAir est ecrit en DP."""

    def __init__(self, pts, oa=None):
        super().__init__(pts)
        self.oa = oa


def oa_pt(lat, lon):
    return f"{to_openair(lat, True)} {to_openair(lon, False)}"


def circle_ll(lat, lon, radius_m, n=72):
    pts = []
    for i in range(n):
        lo, la, _ = GEOD.fwd(lon, lat, 360 * i / n, radius_m)
        pts.append((la, lo))
    return Ring(pts, [f"V X={oa_pt(lat, lon)}", f"DC {radius_m / NM:.3f}".rstrip("0").rstrip(".")])


def sector_ll(lat, lon, axis, half, radius_m, step=1.0):
    pts = [(lat, lon)]
    a = axis - half
    while a <= axis + half + 1e-9:
        lo, la, _ = GEOD.fwd(lon, lat, a, radius_m)
        pts.append((la, lo))
        a += step
    return pts


def point_in_poly(x, y, poly):
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi + 1e-30) + xi:
            inside = not inside
        j = i
    return inside


def seg_intersect(p1, p2, p3, p4):
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    d1, d2 = cross(p3, p4, p1), cross(p3, p4, p2)
    d3, d4 = cross(p1, p2, p3), cross(p1, p2, p4)
    return (d1 * d2 < 0) and (d3 * d4 < 0)


def polys_intersect(a, b):
    if any(point_in_poly(x, y, b) for x, y in a):
        return True
    if any(point_in_poly(x, y, a) for x, y in b):
        return True
    for i in range(len(a)):
        for j in range(len(b)):
            if seg_intersect(a[i - 1], a[i], b[j - 1], b[j]):
                return True
    return False

# --------------------------------------------------------------------------
# SOFIA
# --------------------------------------------------------------------------

def sofia_query(lat, lon, radius_nm, start, duration_min, fl_low, fl_up, traffic):
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (zones_secteur.py)"
    s.get(SOFIA + "/sofia/pages/notamsearcharea.html", timeout=60)
    la, lo = to_sofia(lat, lon)
    hh, mm = divmod(int(duration_min), 60)
    data = {
        ":operation": "postAreaPibRequest",
        "valid_from": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration": f"{hh:02d}{mm:02d}",
        "traffic": traffic,
        "fl_lower": str(fl_low),
        "fl_upper": str(fl_up),
        "radius": str(int(math.ceil(radius_nm))),
        "lat": la,
        "long": lo,
        "uuid": str(uuid.uuid4()),
        "isFromSofia": "true",
    }
    r = s.post(SOFIA + "/sofia", data=data, timeout=300)
    st = re.search(r'<div id="Status">(.*?)</div>', r.text, re.S)
    msg = re.search(r'<div id="Message">(.*?)</div>', r.text, re.S)
    if not st or not msg:
        raise RuntimeError(f"Reponse SOFIA inattendue (HTTP {r.status_code})")
    body = html.unescape(msg.group(1))
    if st.group(1).strip() != "200":
        raise RuntimeError(f"SOFIA {st.group(1)} : {re.sub('<[^>]+>', ' ', body)[:300]}")
    p = json.loads(body)
    if "listnotams" not in p:
        raise RuntimeError(f"SOFIA : {p.get('message', p)}")
    return p


def iter_notams(o):
    if isinstance(o, dict):
        if "qLine" in o and "itemE" in o:
            yield o
        for v in o.values():
            yield from iter_notams(v)
    elif isinstance(o, list):
        for v in o:
            yield from iter_notams(v)


def query_centres(frame, axis, half, radius_m, q_radius_nm):
    """Cylindres SOFIA le long de l'axe, en nombre suffisant pour couvrir le secteur."""
    qr = q_radius_nm * NM * 0.97
    samples = []
    for i in range(0, 41):
        d = radius_m * i / 40
        for k in range(-6, 7):
            a = math.radians(axis + half * k / 6)
            samples.append((d * math.sin(a), d * math.cos(a)))
    ax = math.radians(axis)
    for n in range(1, 30):
        span = radius_m / n
        cs = [((j + 0.5) * span * math.sin(ax), (j + 0.5) * span * math.cos(ax)) for j in range(n)]
        if all(any(math.hypot(x - cx, y - cy) <= qr for cx, cy in cs) for x, y in samples):
            return [frame.ll(cx, cy) for cx, cy in cs]
    raise RuntimeError("Couverture du secteur impossible")

# --------------------------------------------------------------------------
# SUP AIP
# --------------------------------------------------------------------------

class ListeSup(dict):
    """Liste des SUP AIP ; .info = {lu (date de lecture sur le site du SIA), copie (True : derniere liste lue,
    le site n'a pas repondu), nb}."""
    info = {}


def sup_aip_list(cache_dir=None):
    """Liste des SUP AIP Metropole en vigueur : {'146/26': {titre, du, au, pdf}}. Relue sur le site du SIA a chaque
    appel ; si le site ne repond pas (ou si la page ne se lit plus), la derniere liste lue (cache_dir/sup_liste.json)
    est reprise, avec info["copie"] = True."""
    copie = os.path.join(cache_dir, "sup_liste.json") if cache_dir else None
    try:
        sups = _lire_sup_list()
        if len(sups) < 10:                       # page vide ou format change : pas une vraie liste
            raise ValueError(f"{len(sups)} SUP lus")
    except Exception as e:
        if copie and os.path.exists(copie):
            with open(copie, encoding="utf-8") as f:
                c = json.load(f)
            print(f"  ** Liste des SUP AIP illisible ({e}) : reprise de la liste lue le {c['lu']} **")
            out = ListeSup(c["sups"])
            out.info = {"lu": c["lu"], "copie": True, "nb": len(out)}
            return out
        print(f"  ** Liste des SUP AIP illisible ({e}), et pas de copie : SUP AIP non lus **")
        out = ListeSup()
        out.info = {"lu": "", "copie": True, "nb": 0}
        return out
    lu = dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")
    if copie:
        os.makedirs(cache_dir, exist_ok=True)
        with open(copie + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"lu": lu, "sups": sups}, f, ensure_ascii=False)
        os.replace(copie + ".tmp", copie)
    out = ListeSup(sups)
    out.info = {"lu": lu, "copie": False, "nb": len(out)}
    return out


def _lire_sup_list():
    r = requests.get(SUP_LIST, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    t = r.content.decode("utf-8", "replace")
    sups = {}
    for row in re.findall(r'<tr class="tr_ligne_document[^"]*">(.*?)</tr>', t, re.S):
        m = re.search(r'href="([^"]+)"[^>]*>\s*<b>(\d{3})/(20\d{2})</b>\s*<span>(.*?)<i', row, re.S)
        if not m:
            continue
        # date corrigee par le SIA (affichee en rouge) : <strong class="alert-data">
        dates = re.findall(r"<strong([^>]*)>(\d{4}-\d{2}-\d{2})</strong>", row)
        corr = [d for (c, _), d in zip(dates, ("du", "au")) if "alert-data" in c]
        dates = [d for _, d in dates]
        sups[f"{m[2]}/{m[3][2:]}"] = {
            "titre": re.sub(r"<[^>]+>", "", html.unescape(re.sub(r"\s+", " ", m[4]))).strip(),
            "du": dates[0] if dates else "", "au": dates[1] if len(dates) > 1 else "", "pdf": m[1],
            "corrige": corr}
    return sups


# Coordonnees des PDF SUP AIP : 43°14'51''N,006°07'38''E  /  45°22'34.58'' N,001°14'34.09'' W
_Q = r"(?:''|’’|\"|″|”|′′|“)?"
SUP_COORD_RE = re.compile(
    r"(\d{1,2})\s*°\s*(\d{1,2})\s*['’′]\s*(\d{1,2}(?:[.,]\d+)?)\s*" + _Q + r"\s*([NS])\s*[,;/-]?\s*"
    r"(\d{1,3})\s*°\s*(\d{1,2})\s*['’′]\s*(\d{1,2}(?:[.,]\d+)?)\s*" + _Q + r"\s*([EW])")
SUP_CIRCLE_RE = re.compile(r"cercle\s+de\s+(?:rayon\s+)?(\d+(?:[.,]\d+)?)\s*(nm|km|m)\b", re.I)
SUP_ARC_RE = re.compile(
    r"arc\s+(anti[\s-]?horaire|horaire)\s+de\s+(\d+(?:[.,]\d+)?)\s*(nm|km|m)\b", re.I)


def _sup_coords(text):
    out = []
    for m in SUP_COORD_RE.finditer(text):
        la = int(m[1]) + int(m[2]) / 60 + float(m[3].replace(",", ".")) / 3600
        lo = int(m[5]) + int(m[6]) / 60 + float(m[7].replace(",", ".")) / 3600
        out.append((m.start(), m.end(), -la if m[4] == "S" else la, -lo if m[8] == "W" else lo))
    out += [(s, s + 1, la, lo) for s, la, lo in coords_in_text(text)]
    return sorted(out)


def _dist_m(r, unit):
    v = float(r.replace(",", "."))
    return v * (NM if unit.lower() == "nm" else 1000 if unit.lower() == "km" else 1)


def _sup_lateral(text, db):
    """Geometrie d'un bloc 'limites laterales' : (points, note) ou (None, note)."""
    cut = re.search(r"[àa]\s+l[’']exclusion", text, re.I)
    body = text[:cut.start()] if cut else text
    coords = _sup_coords(body)
    c = SUP_CIRCLE_RE.search(body)
    if c:
        after = [k for k in coords if k[0] >= c.end()]
        if after:
            return circle_ll(after[0][2], after[0][3], _dist_m(c[1], c[2])), "cercle"
    arcs = {a.start(): a for a in SUP_ARC_RE.finditer(body)}
    if len(coords) >= 2 or arcs:
        pts, note, oa = [], "polygone", []
        events = sorted([(k[0], "pt", k) for k in coords] + [(p, "arc", a) for p, a in arcs.items()])
        skip_centre = False
        for j, (pos, kind, obj) in enumerate(events):
            if kind == "pt":
                if skip_centre:
                    skip_centre = False
                    continue
                pts.append((obj[2], obj[3]))
                oa.append(f"DP {oa_pt(obj[2], obj[3])}")
            else:
                centre = next((e[2] for e in events[j + 1:] if e[1] == "pt"), None)
                nxt = None
                if centre:
                    k = events.index((centre[0], "pt", centre))
                    nxt = next((e[2] for e in events[k + 1:] if e[1] == "pt"), None)
                if pts and centre and nxt:
                    cw = not obj[1].lower().startswith("anti")
                    cc = (centre[2], centre[3])
                    az1 = GEOD.inv(cc[1], cc[0], pts[-1][1], pts[-1][0])[0] % 360
                    az2 = GEOD.inv(cc[1], cc[0], nxt[3], nxt[2])[0] % 360
                    start = pts[-1]
                    pts += _arc(cc, _dist_m(obj[2], obj[3]), az1, az2, cw)[1:-1]
                    # Arc OpenAir natif : du dernier sommet au point suivant, autour du centre
                    oa += [f"V D={'+' if cw else '-'}", f"V X={oa_pt(*cc)}",
                           f"DB {oa_pt(*start)}, {oa_pt(nxt[2], nxt[3])}"]
                    note = "polygone + arcs"
                skip_centre = True
        if re.search(r"fronti[eè]re|c[oô]te|rivi[eè]re|limite\s+de", body, re.I):
            note += " (frontiere/cote approx.)"
        if len(pts) >= 3:
            if abs(pts[0][0] - pts[-1][0]) < 1e-6 and abs(pts[0][1] - pts[-1][1]) < 1e-6:
                pts = pts[:-1]
            return Ring(pts, oa), note
    if db:
        for k in zone_keys(body):
            if k in db:
                return db[k]["pts"], f"identique a {db[k]['an']}"
    return None, "geometrie non lue"


def _norm_alt(s):
    s = re.sub(r"\s+", " ", s.strip().upper())
    s = re.sub(r"FL\s*0*(\d+)", r"FL\1", s)
    s = re.sub(r"(\d)\s*FT", r"\1FT", s)
    return s.replace("ASFC", "AGL").replace("GND", "GND")


def parse_sup_text(text, db):
    """Zones d'un SUP AIP : liste de {nom, pts, al, ah, note}."""
    text = re.sub(r"SUP AIP[^\n]*Date de publication[^\n]*\n|Page \d+/\d+[^\n]*\n", "\n", text)
    start = re.search(r"LIMITES\s+LAT[ÉE]RALES\s+ET\s+VERTICALES", text, re.I)
    sect = text[start.end():] if start else text
    stop = re.search(r"\n\s*(ORGANISMES\s+A\s+CONTACTER|DISPOSITIONS\s+COMPL|ACTIVIT[ÉE]\s*\n|DATES\s+ET\s+HEURES)", sect, re.I)
    if stop:
        sect = sect[:stop.start()]
    # Evenements dans l'ordre : debut de limites laterales / limites verticales (+ valeur)
    ev = [("LAT", m.start(), m.end()) for m in re.finditer(r"(?im)^[ \t]*limites\s+lat[ée]rales[ \t]*:?[ \t]*$", sect)]
    ev += [("VER", m.start(), m.end()) for m in re.finditer(r"(?im)^[ \t]*limites\s+verticales[ \t]*:?", sect)]
    ev.sort(key=lambda e: e[1])
    zones = []
    i = 0
    prev_end = 0
    while i < len(ev):
        if ev[i][0] != "LAT":
            i += 1
            continue
        # Les noms sont entre la fin du groupe precedent et le premier bloc lateral
        head = sect[prev_end:ev[i][1]]
        lats = []
        while i < len(ev) and ev[i][0] == "LAT":
            nxt = ev[i + 1][1] if i + 1 < len(ev) else len(sect)
            lats.append(sect[ev[i][2]:nxt])
            i += 1
        vers = []
        while i < len(ev) and ev[i][0] == "VER" and len(vers) < len(lats):
            val = re.match(r"\s*([^\n]*/[^\n]*)", sect[ev[i][2]:])
            vers.append(val[1] if val else "")
            prev_end = ev[i][2] + (val.end() if val else 0)
            i += 1
        lines = [l.strip() for l in head.split("\n")
                 if l.strip() and not re.search(r"exclusion|limites|extrait carte|active\.?$|^avec|^est$", l, re.I)]
        names = []
        if lines:
            line = lines[-1]
            if len(lats) > 1:
                # Plusieurs facons de couper la ligne des noms : on garde celle qui donne autant de noms que de zones
                names = []
                for motif in (r"(?<!/)(?=\b(?:ZRT|ZIT|ZDT)(?:/(?:ZRT|ZIT|ZDT))?\b)", r"(?=\b(?:Zone|ZONE)\b)",
                              r"(?=\bLF-?[RDP]\s?\d)", r"(?=\b(?:ZRT|ZIT|ZDT|Zone|ZONE)\b|\bLF-?[RDP]\s?\d)"):
                    cand = [s.strip() for s in re.split(motif, line) if s.strip()]
                    if len(cand) == len(lats):
                        names = cand
                        break
                if not names:
                    names = [f"{line} ({k + 1})" for k in range(len(lats))]
            else:
                names = [line]
        for k, lat_txt in enumerate(lats):
            al = ah = ""
            if k < len(vers) and "/" in vers[k]:
                al, ah = [_norm_alt(x) for x in vers[k].split("/", 1)]
            pts, note = _sup_lateral(lat_txt, db)
            nm = names[k] if k < len(names) else f"zone {len(zones) + 1}"
            zones.append({"nom": nm[:60], "pts": pts, "al": al, "ah": ah, "note": note})
    if not any(z["pts"] for z in zones):
        zones = _sup_blocs(sect, db) or zones
    return zones


# Limites verticales ecrites « SFC / FL 285 », « 1500 FT AMSL / FL 115 », « GND / 3000 FT ASFC »
_SUP_VERT_RE = re.compile(r"(?im)^[ \t]*((?:SFC|GND|FL\s*\d+|\d[\d\s]*(?:FT|M)\b[^/\n]{0,12}))[ \t]*/[ \t]*"
                          r"((?:FL\s*\d+|UNL|\d[\d\s]*(?:FT|M)\b[^\n]{0,12}))[ \t]*$")


def _sup_blocs(sect, db):
    """Autre mise en page des SUP (ex. 094/25) : pas de lignes « Limites laterales » / « Limites verticales », mais
    des blocs « ZRT XXX (ERNIP identification : ...) » suivis de leurs coordonnees, et des limites verticales
    « SFC / FL 285 » propres au bloc ou communes (ecrites une fois apres le dernier bloc)."""
    tetes = list(re.finditer(r"(?m)^[ \t]*(?:ZRT|ZDT|ZIT)\b[^\n]*$", sect))
    if any(len(re.findall(r"\b(?:ZRT|ZDT|ZIT)\b", t.group(0))) > 1 for t in tetes):
        return []                       # en-tete de tableau (« ZRT OLONNE A ZRT OLONNE B ... ») : autre mise en page
    vert_communes = list(_SUP_VERT_RE.finditer(sect))
    zones = []
    for k, t in enumerate(tetes):
        bloc = sect[t.start(): tetes[k + 1].start() if k + 1 < len(tetes) else len(sect)]
        # nom : la ligne d'en-tete et ses suites, jusqu'a « (ERNIP... », une coordonnee ou une ligne vide
        nom = []
        for ligne in bloc.split("\n"):
            ligne = ligne.strip()
            if not ligne or ligne.startswith("(") or "°" in ligne:
                break
            nom.append(ligne)
        pts, note = _sup_lateral(bloc, db)
        if not pts:
            continue
        v = _SUP_VERT_RE.search(bloc) or (vert_communes[-1] if vert_communes else None)
        al, ah = (_norm_alt(v[1]), _norm_alt(v[2])) if v else ("", "")
        zones.append({"nom": re.sub(r"\s+", " ", " ".join(nom))[:60], "pts": pts, "al": al, "ah": ah, "note": note})
    return zones


class SupCache:
    """Telecharge, met en cache et analyse les PDF des SUP AIP."""

    def __init__(self, sups, cache_dir, db):
        self.sups, self.db, self.dir, self.mem = sups, db, os.path.join(cache_dir, "sup"), {}
        self.texts, self.act, self.inf = {}, {}, {}
        os.makedirs(self.dir, exist_ok=True)

    def zones(self, num):
        if num in self.mem:
            return self.mem[num]
        info = self.sups.get(num)
        res = []
        if info and info.get("pdf"):
            try:
                from pypdf import PdfReader
                doc_id = re.findall(r"/d/(\d+)", info["pdf"])
                path = os.path.join(self.dir, f"{num.replace('/', '_')}_{doc_id[0] if doc_id else 'x'}.pdf")
                if not os.path.exists(path):
                    r = requests.get(info["pdf"], timeout=180, headers={"User-Agent": "Mozilla/5.0"})
                    r.raise_for_status()
                    with open(path, "wb") as f:
                        f.write(r.content)
                text = "\n".join(p.extract_text() or "" for p in PdfReader(path).pages)
                if not re.search(r"SUP AIP(?: AIRAC)?\s+" + re.escape(num.split("/")[0]), text):
                    print(f"  (SUP {num} : le PDF ne correspond pas au numero, ignore)")
                else:
                    res = parse_sup_text(text, self.db)
                    self.texts[num] = text
            except ImportError:
                print("  (pypdf absent : geometrie des SUP AIP non lue - pip install pypdf)")
            except Exception as e:
                print(f"  (SUP {num} : lecture impossible : {e})")
        # Zones recopiees a la main (PDF illisible : tableau sur plusieurs colonnes...) : elles priment toujours
        res = complements_sup().get(num) or res
        self.mem[num] = res
        return res

    def infos(self, num):
        """Frequences, gestionnaire, telephones et conditions de penetration du SUP (dict vide si non lu)."""
        if num not in self.inf:
            self.zones(num)
            self.inf[num] = sup_infos(self.texts[num]) if num in self.texts else {}
        return self.inf[num]

    def activite(self, num, zone):
        """Mode d'activation d'une zone du SUP : SupActivite (voir sup_activite)."""
        key = (num, zone)
        if key not in self.act:
            self.zones(num)
            if self.est_itineraire(num):
                self.act[key] = SupActivite("itineraire")
            else:
                noms = [z["nom"] for z in self.mem.get(num, [])]
                self.act[key] = sup_activite(self.texts.get(num, ""), zone, noms)
                caler_sur_listing(self.act[key], self.sups.get(num), self.dates_pdf(num))
        return self.act[key]

    def dates_pdf(self, num):
        """(premier jour, dernier jour) du SUP d'apres les periodes « du ... au ... » de son PDF, ou None."""
        t = unicodedata.normalize("NFKD", self.texts.get(num, "")).encode("ascii", "ignore").decode().upper()
        ds = []
        for m in _PERIODE.finditer(re.sub(r"[ \t]+", " ", t)):
            g = m.groups()
            try:
                ds.append(dt.date(int(g[2]), MOIS_FR[g[1]], int(g[0])) if g[0] else dt.date(int(g[5]), int(g[4]), int(g[3])))
                ds.append(dt.date(int(g[9]), MOIS_FR[g[8]], int(g[7])) if g[7] else dt.date(int(g[12]), int(g[11]), int(g[10])))
            except (ValueError, KeyError):
                pass
        return (min(ds), max(ds)) if ds else None

    def est_itineraire(self, num):
        """SUP dont l'objet est un itineraire (helicopteres, VFR de nuit...) : ligne a suivre, pas une zone.
        Volontairement non interprete."""
        objet = self.sups.get(num, {}).get("titre", "")
        m = re.search(r"Objet\s*:\s*([^\n]+(?:\n[^\n:]+)?)", self.texts.get(num, ""))
        objet += " " + (m.group(1) if m else "")
        return bool(re.search(r"\bitin[ée]raires?\b", objet, re.I))


# --------------------------------------------------------------------------
# Rubrique « Dates et heures d'activite » des SUP AIP
# --------------------------------------------------------------------------

MOIS_FR = {m: i + 1 for i, m in enumerate(
    "JANVIER FEVRIER MARS AVRIL MAI JUIN JUILLET AOUT SEPTEMBRE OCTOBRE NOVEMBRE DECEMBRE".split())}
JOURS_FR = {"LUNDI": 0, "MARDI": 1, "MERCREDI": 2, "JEUDI": 3, "VENDREDI": 4, "SAMEDI": 5, "DIMANCHE": 6,
            "LUN": 0, "MAR": 1, "MER": 2, "JEU": 3, "VEN": 4, "SAM": 5, "DIM": 6}
_DATE_FR = r"(\d{1,2})(?:ER)?\s+(" + "|".join(MOIS_FR) + r")\s+(\d{4})"
_PERIODE = re.compile(
    r"DU\s+(?:" + _DATE_FR + r"|(\d{2})/(\d{2})/(\d{4}))(?:\s+A\s+(\d{4}))?\s+AU\s+(?:" + _DATE_FR +
    r"|(\d{2})/(\d{2})/(\d{4}))(?:\s+A\s+(\d{4}))?")
# Heure d'une plage : 0800, 08H00, 08:00, ou lever/coucher du soleil SR, SS+30, SR-30, SS-1HR, SR+2HR, SR PLUS 02H00
_HEURE = (r"(?:S[RS](?:\s*(?:[+-]|PLUS|MOINS)\s*\d{1,2}(?:H\d{2}|\s*HRS?\b|\s*H\b|\s*MIN\b)?)?|\d{2}[H:]?\d{2})")
_PLAGE = re.compile(r"(?:DE|ENTRE)\s+(" + _HEURE + r")(?:\s*UTC)?\s+(?:A|ET)\s+(" + _HEURE + r")"
                    r"|(?<![\w/+-])(" + _HEURE + r")\s*-\s*(" + _HEURE + r")(?![\w/])|\bH24\b")


def _heure(s):
    """Heure d'une plage sous forme canonique : « 0800 », ou « SR-30 » / « SS+60 » (decalage en minutes)."""
    m = re.match(r"(S[RS])(?:\s*([+-]|PLUS|MOINS)\s*(\d{1,2})(?:H(\d{2})|\s*(HRS?|H|MIN))?)?", s)
    if not m:
        return re.sub(r"\D", "", s)
    v = 0
    if m[2]:
        v = int(m[3]) * 60 + int(m[4]) if m[4] else int(m[3]) * (60 if (m[5] or "").startswith("H") else 1)
        v = -v if m[2] in ("-", "MOINS") else v
    return f"{m[1]}{v:+d}"


def paques(annee):
    """Dimanche de Paques (calendrier gregorien, algorithme de Meeus)."""
    a, b, c = annee % 19, annee // 100, annee % 100
    d, e = b // 4, b % 4
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    mois = (h + l_ - 7 * m + 114) // 31
    jour = (h + l_ - 7 * m + 114) % 31 + 1
    return dt.date(annee, mois, jour)


def jours_feries(annee):
    """Les 11 jours feries de France metropolitaine (hors Alsace-Moselle : vendredi saint, 26 decembre)."""
    p = paques(annee)
    fixes = [(1, 1), (5, 1), (5, 8), (7, 14), (8, 15), (11, 1), (11, 11), (12, 25)]
    return {dt.date(annee, m, j) for m, j in fixes} | {p + dt.timedelta(days=n) for n in (1, 39, 50)}


def est_ferie(d):
    return d in jours_feries(d.year)


def _dst(d):
    """Heure d'ete europeenne (dernier dimanche de mars -> dernier dimanche d'octobre)."""
    def last_sunday(y, m):
        x = dt.date(y, m, 31)
        return x - dt.timedelta(days=(x.weekday() + 1) % 7)
    return last_sunday(d.year, 3) <= d < last_sunday(d.year, 10)


def _jours(txt):
    """Jours de la semaine autorises (ensemble 0-6) d'apres « tous les jours sauf … », « uniquement le … »,
    « du lundi au vendredi », « LUN-VEN »."""
    t = txt
    m = re.search(r"DU\s+(\w+)\s+AU\s+(\w+)", t) or re.search(r"\b(LUN|MAR|MER|JEU|VEN|SAM|DIM)\s*-\s*(LUN|MAR|MER|JEU|VEN|SAM|DIM)\b", t)
    if m and m[1] in JOURS_FR and m[2] in JOURS_FR:
        a, b = JOURS_FR[m[1]], JOURS_FR[m[2]]
        return {k % 7 for k in range(a, b + 1 + (7 if b < a else 0))}
    noms = lambda s: {JOURS_FR[w] for w in re.findall(r"\b(" + "|".join(JOURS_FR) + r")\b", s)}  # noqa
    m = re.search(r"SAUF\s+([A-Z ,ET]+)", t)
    if m and noms(m[1]):
        return set(range(7)) - noms(m[1])
    m = re.search(r"UNIQUEMENT\s+(?:LE|LES)?\s*([A-Z ,ET]+)", t)
    if m and noms(m[1]):
        return noms(m[1])
    return set(range(7))


def _feries(txt):
    """(exclure les jours feries, exclure les veilles de jours feries) d'apres « sauf / hors jours feries »."""
    return (bool(re.search(r"(?:SAUF|HORS|EXCEPTE)[^.;]{0,80}?(?<!VEILLE DE )(?<!VEILLES DE )JOURS? FERIES?", txt)),
            bool(re.search(r"(?:SAUF|HORS|EXCEPTE)[^.;]{0,80}?VEILLES? DE JOURS? FERIES?", txt)))


def _horaires_fixes(bloc):
    """Regles fixes [(date_debut, date_fin, jours, [(hhmm, hhmm), ...]), ...] ou None si illisible."""
    t = bloc
    ete = bool(re.search(r"(?:ETE|SUM(?:MER)?)\s*:?\s*-\s*0?1\s*H", t))
    periodes = list(_PERIODE.finditer(t))
    segments = []
    if periodes:
        for i, m in enumerate(periodes):
            g = m.groups()
            if g[0]:
                d0 = dt.date(int(g[2]), MOIS_FR[g[1]], int(g[0]))
            else:
                d0 = dt.date(int(g[5]), int(g[4]), int(g[3]))
            if g[7]:
                d1 = dt.date(int(g[9]), MOIS_FR[g[8]], int(g[7]))
            else:
                d1 = dt.date(int(g[12]), int(g[11]), int(g[10]))
            fin = periodes[i + 1].start() if i + 1 < len(periodes) else len(t)
            segments.append((d0, d1, t[m.end():fin], g[6], g[13]))
    else:
        segments.append((None, None, t, None, None))
    regles = []
    for d0, d1, s, h0, h1 in segments:
        # Plages d'exclusion « sauf pendant les creneaux horaires suivants : SR - SR plus 02H00, SS moins 02H00 - SS » :
        # retirees des creneaux (9e element de la regle) ; leurs heures ne sont pas des plages d'activite
        excl = ()
        x = _SAUF_PENDANT.search(s)
        if x:
            hs = [_heure(h) for h in re.findall(_HEURE, x[1])]
            excl = tuple(zip(hs[0::2], hs[1::2]))
            s = s[:x.start()] + " " + s[x.end():]
        # Periodes d'un jour de la semaine a un autre : « du lundi 10H00 au vendredi 10H00 »
        semaine = list(_SEMAINE_RE.finditer(s))
        for m in semaine:
            regles += [(d0, d1, jj, pl, ete, (h0, h1)) + _feries(s) + (excl,) for jj, pl in _regles_semaine(m)]
        if semaine:
            s = _SEMAINE_RE.sub(" ", s)
            if not _PLAGE.search(s):
                continue
        plages = list(_PLAGE.finditer(s))
        if not plages and h0 and h1:            # « Du 17 avril 2026 a 0000 au 14 avril 2027 a 2359 » seul
            regles.append((d0, d1, set(range(7)), [("0000", "2400")], ete, (h0, h1)) + _feries(s) + (excl,))
            continue
        if not plages and (len(_jours(s)) < 7 or _feries(s)[0]):
            # jours sans heures : « activables du lundi au vendredi hors jours feries » -> toute la journee ces jours-la
            regles.append((d0, d1, _jours(s), [("0000", "2400")], ete, (h0, h1)) + _feries(s) + (excl,))
            continue
        for j, p in enumerate(plages):
            suite = s[p.end(): plages[j + 1].start() if j + 1 < len(plages) else len(s)]
            if p.group(0) == "H24":
                hh = ("0000", "2400")
            elif p[1]:
                hh = (_heure(p[1]), _heure(p[2]))
            else:
                hh = (_heure(p[3]), _heure(p[4]))
            jours = _jours(suite)
            avant = s[:p.start()] if j == 0 else ""
            if len(jours) == 7 and avant:           # jours ecrits avant la plage : « du lundi au vendredi : 0800-1700 »
                jours = _jours(avant)
            regles.append((d0, d1, jours, [hh], ete, (h0, h1)) + _feries(suite + " " + avant) + (excl,))
    return regles or None


_JOUR_SEM = "LUNDI|MARDI|MERCREDI|JEUDI|VENDREDI|SAMEDI|DIMANCHE"
_SEMAINE_RE = re.compile(r"DU\s+(" + _JOUR_SEM + r")\s+(\d{1,2})\s*[H:]\s*(\d{2})\s+AU\s+(" + _JOUR_SEM + r")\s+"
                         r"(\d{1,2})\s*[H:]\s*(\d{2})")
_SAUF_PENDANT = re.compile(r"(?:,\s*)?(?:ET\s+)?SAUF\s+PENDANT\s+(?:LES\s+)?(?:CRENEAUX|PLAGES)(?:\s+HORAIRES)?"
                           r"(?:\s+SUIVANTS?)?\s*:?(.*?)(?=\bDU\s+\d|\(\s*HORAIRES|\bTOUTES\s+LES\s+ZONES|\Z)", re.S)


def _regles_semaine(m):
    """« du lundi 10H00 au vendredi 10H00 » -> [(jours, plages)] : lundi 1000-2400, mardi a jeudi 0000-2400,
    vendredi 0000-1000 (le passage d'une semaine a l'autre, « du vendredi 18H00 au lundi 0600 », est gere)."""
    a, b = JOURS_FR[m[1]], JOURS_FR[m[4]]
    ha, hb = f"{int(m[2]):02d}{m[3]}", f"{int(m[5]):02d}{m[6]}"
    if a == b and hb > ha:
        return [({a}, [(ha, hb)])]
    n = (b - a) % 7 or 7
    milieu = {(a + k) % 7 for k in range(1, n)}
    out = [({a}, [(ha, "2400")])] + ([(milieu, [("0000", "2400")])] if milieu else [])
    return out + ([({b}, [("0000", hb)])] if hb != "0000" else [])


# Tableau de jours precis : « MARS 2026 : 24,26 : 0700-1200 ET 1700-2259 », « NOVEMBRE 2026 3,5,17,24 0700-12001500-2359 »
_TABLEAU = re.compile(r"\b(" + "|".join(MOIS_FR) + r")\s+(\d{4})\s*:?\s*((?:\d{1,2}\s*,\s*)*\d{1,2})\s*:?\s*"
                      r"((?:\d{4}\s*-\s*\d{4}(?:\s*(?:ET|,)\s*)?)+)")


# Tableau « DATES | ZRT RAPACE ALPHA | ZRT RAPACE BRAVO » : une ligne par date, puis les plages de chaque colonne
# (collees a l'extraction du PDF : « 06 OCTOBRE 2026 0600 - 14001600 - 2359 0600 - 14001600 - 2359 »)
_TABLEAU_DATES = re.compile(r"\b(\d{1,2})(?:ER)?\s+(" + "|".join(MOIS_FR) + r")\s+(\d{4})\s+((?:\d{4}\s*-\s*\d{4}\s*)+)")


def _tableau_dates(texte, zone, noms):
    """{date: plages de la colonne de `zone`} d'un tableau de dates ; les plages d'une ligne sont reparties entre
    les colonnes (zones citees dans l'en-tete « DATES ... »), toutes gardees si la repartition n'est pas nette."""
    lignes = list(_TABLEAU_DATES.finditer(texte))
    if not lignes:
        return {}
    entete = texte[max(0, texte.rfind("DATES", 0, lignes[0].start())): lignes[0].start()]
    cols = sorted((entete.find(_fold(n)), _fold(n)) for n in noms if _fold(n) and _fold(n) in entete)
    k = next((i for i, (_, n) in enumerate(cols) if n == _fold(zone)), None)
    jours = {}
    for m in lignes:
        plages = re.findall(r"(\d{4})\s*-\s*(\d{4})", m[4])
        if cols and k is not None and len(plages) % len(cols) == 0:
            n = len(plages) // len(cols)
            plages = plages[k * n:(k + 1) * n]
        try:
            jours.setdefault(dt.date(int(m[3]), MOIS_FR[m[2]], int(m[1])), []).extend(plages)
        except ValueError:
            pass
    return jours


def _tableau_jours(texte, zone="", noms=()):
    """Regles d'un tableau de jours precis (une regle par jour), limitees a la periode propre a `zone`
    (« ZRT PEGOUD - DU 01 DECEMBRE 2026 A 0700 AU 23 FEVRIER 2027 A 2359 ») quand le SUP en donne une.
    None si la rubrique n'a pas de tableau."""
    jours = {}
    for m in _TABLEAU.finditer(texte):
        mois, annee = MOIS_FR[m[1]], int(m[2])
        plages = re.findall(r"(\d{4})\s*-\s*(\d{4})", m[4])
        for j in re.findall(r"\d{1,2}", m[3]):
            try:
                jours.setdefault(dt.date(annee, mois, int(j)), []).extend(plages)
            except ValueError:
                pass
    if not jours:
        jours = _tableau_dates(texte, zone, noms)
    if not jours:
        return None
    d0 = d1 = None
    w = _fold(zone).split()
    if w:
        p = re.search(r"(?<![A-Z0-9])" + r"\W+".join(map(re.escape, w)) + r"(?![A-Z0-9])\W{0,10}(" + _PERIODE.pattern + ")", texte)
        if p:
            g = _PERIODE.match(p[1]).groups()
            d0 = dt.date(int(g[2]), MOIS_FR[g[1]], int(g[0])) if g[0] else dt.date(int(g[5]), int(g[4]), int(g[3]))
            d1 = dt.date(int(g[9]), MOIS_FR[g[8]], int(g[7])) if g[7] else dt.date(int(g[12]), int(g[11]), int(g[10]))
    regles = [(d, d, set(range(7)), pl, False, (None, None), False, False)
              for d, pl in sorted(jours.items()) if not d0 or d0 <= d <= d1]
    return regles or [(d, d, set(range(7)), pl, False, (None, None), False, False) for d, pl in sorted(jours.items())]


def bornes_listing(info):
    """Validite d'un SUP d'apres le listing du SIA (dates « du / au », corrigees en rouge quand elles changent) :
    (debut, fin) en UTC, ou None si le listing ne donne pas de date. Ce sont ces dates qui font foi, pas celles du
    PDF du SUP ni celles du trigger NOTAM."""
    if not info or not info.get("du"):
        return None
    t0 = dt.datetime.fromisoformat(info["du"]).replace(tzinfo=UTC)
    t1 = (dt.datetime.fromisoformat(info["au"]).replace(tzinfo=UTC) + dt.timedelta(days=1) if info.get("au")
          else dt.datetime.max.replace(tzinfo=UTC))
    return t0, t1


def caler_sur_listing(sa, info, pdf=None):
    """Recale les periodes lues dans le PDF du SUP sur les dates du listing SIA : periodes raccourcies si le listing
    a avance la fin (ou recule le debut). Si le listing a prolonge le SUP lui-meme (dates extremes du PDF `pdf`
    differentes du listing), les periodes de plusieurs jours qui commencent ou finissent avec le SUP sont etendues
    (horaires du PDF reconduits : sa.prolonge = True) ; la periode propre a une zone, plus courte, ne l'est pas."""
    lim = bornes_listing(info)
    if not lim or not sa.regles or not any(r[0] for r in sa.regles):
        return
    du, au = lim[0].date(), (lim[1] - dt.timedelta(days=1)).date()
    mn, mx = pdf or (None, None)
    regles = []
    for r in sa.regles:
        d0, d1, h = r[0], r[1], list(r[5])
        if d0:
            if d0 == mn and d1 > d0 and du < d0:
                d0, h[0], sa.prolonge = du, None, True
            if d1 == mx and d1 > d0 and au > d1:
                d1, h[1], sa.prolonge = au, None, True
            if d0 < du:
                d0, h[0] = du, None
            if d1 > au:
                d1, h[1] = au, None
            if d0 > d1:
                continue
        regles.append((d0, d1) + r[2:5] + (tuple(h),) + r[6:])
    sa.regles = regles


class SupActivite:
    """mode : 'notam' (activable par NOTAM), 'horaires' (horaires fixes), 'verifier' (non interprete)."""

    def __init__(self, mode, regles=None, texte=""):
        self.mode, self.regles, self.texte = mode, regles or [], texte
        self.prolonge = False

    def creneaux(self, day, pos=(46.5, 2.5)):
        """Intervalles UTC d'activite fixe le jour `day` (mode 'horaires'), ou d'activabilite (mode 'radio').
        SR/SS sont calcules a la position `pos` (lat, lon). Les plages de nuit commencees la veille sont incluses."""
        out = []
        d_base = dt.datetime(day.year, day.month, day.day, tzinfo=UTC)
        for jour in (day - dt.timedelta(days=1), day):
            base = dt.datetime(jour.year, jour.month, jour.day, tzinfo=UTC)
            sun = sun_times(jour, *pos)
            ete_j = _dst(jour)

            def heure(h, ete):
                if h[0] == "S":                  # lever / coucher du soleil (deja en UTC : pas de correction d'ete)
                    return (sun[0] if h[:2] == "SR" else sun[1]) + dt.timedelta(minutes=int(h[2:]))
                return base + dt.timedelta(hours=int(h[:2]), minutes=int(h[2:])) - dt.timedelta(hours=1 if ete else 0)

            for r in self.regles:
                d0, d1, jours, plages, ete, (h0, h1), sauf_ferie, sauf_veille = r[:8]
                excl = r[8] if len(r) > 8 else ()
                if d0 and not (d0 <= jour <= d1):
                    continue
                if jour.weekday() not in jours:
                    continue
                if (sauf_ferie and est_ferie(jour)) or (sauf_veille and est_ferie(jour + dt.timedelta(days=1))):
                    continue
                for a, b in plages:
                    x, y = heure(a, ete and ete_j), heure(b, ete and ete_j)
                    if y <= x:
                        y += dt.timedelta(days=1)
                    if d0 and h0 and jour == d0:
                        x = max(x, base + dt.timedelta(hours=int(h0[:2]), minutes=int(h0[2:])))
                    if d1 and h1 and jour == d1:
                        y = min(y, base + dt.timedelta(hours=int(h1[:2]), minutes=int(h1[2:]) + 1))
                    morceaux = [(x, y)]
                    for j2 in (jour, jour + dt.timedelta(days=1)):      # plages d'exclusion du jour et du lendemain
                        for ea, eb in excl:
                            u, v = _heure_jour(ea, j2, pos, ete and _dst(j2)), _heure_jour(eb, j2, pos, ete and _dst(j2))
                            if v <= u:
                                v += dt.timedelta(days=1)
                            morceaux = [m for p_, q_ in morceaux for m in ((p_, min(q_, u)), (max(p_, v), q_)) if m[1] > m[0]]
                    for x, y in morceaux:
                        x, y = max(x, d_base), min(y, d_base + dt.timedelta(days=1))
                        if y > x:
                            out.append((x, y))
        return sorted(out)


def _heure_jour(h, jour, pos, ete):
    """Instant UTC d'une heure canonique (« 0800 », « SR-30 ») le jour `jour` a la position `pos`."""
    if h[0] == "S":
        sun = sun_times(jour, *pos)
        return (sun[0] if h[:2] == "SR" else sun[1]) + dt.timedelta(minutes=int(h[2:]))
    return (dt.datetime(jour.year, jour.month, jour.day, tzinfo=UTC) + dt.timedelta(hours=int(h[:2]), minutes=int(h[2:]))
            - dt.timedelta(hours=1 if ete else 0))


_RUBRIQUES = (r"INFORMATIONS?\s+DES\s+USAGERS|GESTIONNAIRES?|STATUT|CONDITIONS\s+DE\s+P[ÉE]N[ÉE]TRATION|SERVICES\s+RENDUS|"
              r"LIMITES\s+LAT[ÉE]RALES|ORGANISMES?\s+[ÀA]\s+CONTACTER|DISPOSITIONS\s+COMPL[ÉE]MENTAIRES|"
              r"DATES?\s+ET\s+HEURES|ACTIVIT[ÉE]\s*\n|OBJET\s*:")
_FREQ_RE = re.compile(r"([A-ZÀ-Ü][A-ZÀ-Ü'’ \-]{1,30}?\s(?:INFO|APP|TWR|AFIS|SIV|CTL|CONTROL|CONTR[ÔO]LE|APPROCHE|TOUR|"
                      r"INFORMATION|RADAR|SOL|GND|ATIS|TRANSIT|DIRECTEUR))\s*:?\s*(\d{3}[.,]\d{1,3})\s*MHZ", re.I)
_TEL_RE = re.compile(r"(?:\+33\s?\(?0?\)?\s?|\b0)[1-9](?:[\s.]?\d{2}){4}\b")


_SERVICE_RE = re.compile(r"(INFO|APP|TWR|AFIS|SIV|CTL|CONTROL|CONTR[ÔO]LE|APPROCHE|TOUR|INFORMATION|RADAR|SOL|GND|ATIS|"
                         r"TRANSIT|DIRECTEUR)$", re.I)


def _indicatif(brut):
    """Indicatif d'un organisme (« VALENCE INFO », « MONT DE MARSAN APP », « LA ROCHELLE TWR ») a partir du texte qui
    precede la frequence, qui peut contenir le debut de la phrase (« … ou du gestionnaire de la ZRT SPAHIS VALENCE INFO »).
    Garde le dernier mot avant le service, et les mots qui lui sont lies : article (LA, LE, LES), SAINT, ou « X DE Y »."""
    mots = re.sub(r"\s+", " ", brut).strip(" -:'’").split(" ")
    if len(mots) < 2 or not _SERVICE_RE.match(mots[-1]):
        return re.sub(r"\s+", " ", brut).strip(" -:'’").upper()
    service, avant = mots[-1], mots[:-1]
    # Nom isole de deux mots en majuscules (« : ORLEANS BRICY TWR ») : garde les deux
    if len(avant) == 2 and all(re.fullmatch(r"[A-ZÀ-Ü][A-ZÀ-Ü\-]{2,}", w) for w in avant) \
            and not {w.upper() for w in avant} & {"ZRT", "ZONE", "TSA", "TRA", "SUP", "AIP", "RTBA", "NOTAM"}:
        return " ".join(avant + [service]).upper()
    nom = [avant.pop()]
    while avant:
        w = avant[-1].upper()
        if w in ("LA", "LE", "LES", "SAINT", "ST", "STE", "SAINTE"):
            nom.insert(0, avant.pop())
        elif w in ("DE", "DU", "DES") and len(avant) >= 2 and avant[-2][:1].isupper() and avant[-2].isupper() \
                and avant[-2].upper() not in ("ZRT", "ZONE", "TSA", "TRA", "SUP", "AIP", "RTBA", "GESTIONNAIRE"):
            nom[0:0] = [avant.pop(-2), avant.pop()]
        else:
            break
    return " ".join(nom + [service]).upper()


def _rubrique(text, titre):
    m = re.search(r"(?:^|\n)\s*(?:" + titre + r")\s*:?(.*?)(?=\n\s*(?:" + _RUBRIQUES + r")|\Z)", text, re.S | re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def sup_infos(text):
    """Informations pratiques d'un SUP : frequences ou l'activite reelle est connue, gestionnaire, telephones,
    conditions de penetration."""
    text = re.sub(r"SUP AIP[^\n]*Date de publication[^\n]*\n|Page \d+/\d+[^\n]*\n|FR\s*©\s*SIA", "\n", text)
    usagers = _rubrique(text, r"INFORMATIONS?\s+DES\s+USAGERS")
    freqs = []
    for m in _FREQ_RE.finditer(usagers or text):
        nom = _indicatif(m[1])
        f = m[2].replace(",", ".")
        if (nom.upper(), f) not in [(a.upper(), b) for a, b in freqs]:
            freqs.append((nom.upper(), f))
    gest = _rubrique(text, r"GESTIONNAIRES?")
    contacts = _rubrique(text, r"ORGANISMES?\s+[ÀA]\s+CONTACTER")
    tels = []
    for t in _TEL_RE.findall(gest + " " + contacts):
        t = re.sub(r"\D", "", t)
        t = "0" + t[-9:] if len(t) >= 9 else t
        t = " ".join(t[i:i + 2] for i in range(0, 10, 2))
        if t not in tels:
            tels.append(t)
    pen = _rubrique(text, r"CONDITIONS\s+DE\s+P[ÉE]N[ÉE]TRATION")
    pen = re.split(r"(?<=[a-zé])\s*:\s*-|\s-\s", pen)[0][:160]
    return {"freqs": freqs[:6], "gestionnaire": re.split(r"\s(?:T[ÉE]L|Tél)", gest, flags=re.I)[0][:120],
            "tels": tels[:4], "penetration": pen}


def sup_activite(text, zone, noms):
    """Lit la rubrique « Dates et heures d'activite » du SUP et renvoie le mode d'activation de `zone`."""
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().upper()
    t = re.sub(r"SUP AIP[^\n]*DATE DE PUBLICATION[^\n]*\n|PAGE \d+/\d+[^\n]*\n", "\n", t)
    m = re.search(r"DATES?\s+ET\s+HEURES?\s+D.?\s?ACTIVITES?(.*?)(?=\n\s*(?:INFORMATION|GESTIONNAIRE|STATUT|CONDITIONS|"
                  r"SERVICES|LIMITES|ORGANISMES|DISPOSITIONS)\b|\Z)", t, re.S)
    if not m:
        return SupActivite("verifier")
    sect = re.sub(r"[ \t]+", " ", m.group(1))
    # Bloc propre a la zone : texte entre son nom et le nom de zone suivant (sinon toute la rubrique).
    # Les noms sont cherches mot a mot dans le texte d'origine (la ponctuation sert aux horaires).
    mots = lambda s: _fold(s).split()  # noqa
    pos = []
    for n in noms:
        w = mots(n)
        if w:
            mm = re.search(r"(?<![A-Z0-9])" + r"\W+".join(map(re.escape, w)) + r"(?![A-Z0-9])", sect)
            if mm:
                pos.append((mm.start(), mm.end(), _fold(n)))
    pos.sort()
    bloc = None
    core = _fold(zone)
    for i, (p0, p1, n) in enumerate(pos):
        if n == core:
            # zones listees a la suite (meme regle) : le bloc commence apres le dernier nom de la serie
            j = i
            while j + 1 < len(pos) and re.fullmatch(r"[\s/,;ET]*", sect[pos[j][1]:pos[j + 1][0]]):
                j += 1
            debut = pos[j][1]
            fin = next((q0 for q0, _, _ in pos[j + 1:] if q0 > debut), len(sect))
            bloc = sect[debut:fin]
            break
    texte = bloc if bloc and re.search(r"\d|H24", bloc) else sect
    general = sect
    activable = lambda s: any(k in s for k in ("ACTIVABLE", "ACTIVATION", "ACTIVITE REELLE"))  # noqa
    if not activable(texte) and activable(general) and not _horaires_fixes(texte):
        texte = general          # « ZRT A et B activables : » puis un tableau de dates par zone
    if activable(texte):
        if "NOTAM" in texte or ("NOTAM" in general and "PROTOCOLE" not in general):
            return SupActivite("notam", texte=texte)
        # Activable sans NOTAM (H24, preavis, protocole...) : activite reelle connue par radio ou telephone, pendant
        # les creneaux d'activabilite du SUP quand il en donne (plages d'exclusion et periodes « du lundi 10H00 au
        # vendredi 10H00 » comprises) ; sinon la zone reste activable toute la journee.
        return SupActivite("radio", _tableau_jours(texte, zone, noms) or _horaires_fixes(texte), texte)
    if "NOTAM" in texte:
        return SupActivite("notam", texte=texte)
    regles = _tableau_jours(texte, zone, noms) or _horaires_fixes(texte)
    if regles:
        return SupActivite("horaires", regles, texte)
    return SupActivite("verifier", texte=texte)

# --------------------------------------------------------------------------
# Base OpenAir des espaces permanents (planeur-net, issue de l'AIP France)
# pour la geometrie des zones permanentes activees par NOTAM (R, D, RTBA, TSA...)
# --------------------------------------------------------------------------

AIRSPACE_URL = "https://planeur-net.github.io/airspace/france.txt"
LVZC_INDEX = "https://www.lvzc.be/index.php/luchtruim/luchtruimbestanden"
DAEC_INDEX = "https://www.daec.de/fachbereiche/luftraum-flugsicherheit-betrieb/luftraumdaten/"
# LVNL (prestataire de navigation aerienne neerlandais) : couches officielles de son geoportail, licence CC BY 4.0
LVNL_SERVICE = "https://services-eu1.arcgis.com/OtUwzhpKSdeXgRIB/arcgis/rest/services/Airspaces_data/FeatureServer"
LVNL_COUCHES = {3: "TSA", 5: "TRA", 17: "Danger area", 18: "Restricted area", 19: "Prohibited area"}
# Suisse, Italie, Espagne : donnees openAIP (communautaires, CC BY-NC 4.0) converties en OpenAir par AirspaceConverter
OPENAIP_URL = "https://alus.it/AirspaceConverter/openAIP/{}_asp.txt"
OPENAIP_PAYS = {"Suisse": "ch", "Italie": "it", "Espagne": "es"}
OPENAIP_NOMS = {}       # {code pays: {nom normalise: zone}} : zones citees par leur nom
# Identifiants de zones : LF-R45A, R139A, TSA22, EBR67, EBD26, HTA04A, LFA01, TRA23, ELTSA03, CBA1, EDR134, EDD19B, EHTSA55, EHR64, LSR10, LED50, LETSA31, LI TSA113, LI-D313...
ZONE_ID_RE = re.compile(r"\b(?:LF-?|EU-?|EB-?|EL-?|ED-?|EH-?|LS-?|LE-?|LI[-\s]?)?(R|D|P|TSA|CBA|HTA|LFA|TRA)\s?-?\s?(\d{1,3})\s?([A-Z]{1,2}\d?(?:\.\d)?)?(?![\w.])")
OA_COORD_RE = re.compile(r"(\d+):(\d+):(\d+(?:\.\d+)?)\s*([NS])\s*,?\s*(\d+):(\d+):(\d+(?:\.\d+)?)\s*([EW])")


def _oa_pt(txt):
    m = OA_COORD_RE.search(txt)
    if not m:
        return None
    la = int(m[1]) + int(m[2]) / 60 + float(m[3]) / 3600
    lo = int(m[5]) + int(m[6]) / 60 + float(m[7]) / 3600
    return (-la if m[4] == "S" else la, -lo if m[8] == "W" else lo)


def _arc(center, r_m, az1, az2, cw):
    pts = []
    if cw:
        span = (az2 - az1) % 360 or 360
    else:
        span = -((az1 - az2) % 360 or 360)
    n = max(4, int(abs(span) / 5))
    for i in range(n + 1):
        lo, la, _ = GEOD.fwd(center[1], center[0], az1 + span * i / n, r_m)
        pts.append((la, lo))
    return pts


# --------------------------------------------------------------------------
# Bases telechargees (cache) : date du dernier telechargement et edition
# --------------------------------------------------------------------------

RECHARGE_JOURS = 7          # une base est retelechargee si son dernier telechargement date de plus de 7 jours
EDITION_ANCIENNE_JOURS = 60  # au-dela (environ 2 cycles AIRAC), l'edition est signalee comme ancienne
MOIS_EN = {m: i + 1 for i, m in enumerate("JANUARY FEBRUARY MARCH APRIL MAY JUNE JULY AUGUST SEPTEMBER OCTOBER "
                                          "NOVEMBER DECEMBER".split())}


def _telecharge(path):
    """Date du dernier telechargement : fichier « .date » ecrit a cote (garde par le cache GitHub quelle que soit la
    date du fichier restaure), sinon date de modification du fichier."""
    try:
        with open(path + ".date", encoding="utf-8") as f:
            return dt.datetime.fromisoformat(f.read().strip())
    except Exception:
        return dt.datetime.fromtimestamp(os.path.getmtime(path), UTC) if os.path.exists(path) else None


def _a_recharger(path):
    t = _telecharge(path)
    return t is None or (dt.datetime.now(UTC) - t).total_seconds() > RECHARGE_JOURS * 86400


def _marquer(path):
    with open(path + ".date", "w", encoding="utf-8") as f:
        f.write(dt.datetime.now(UTC).isoformat(timespec="seconds"))


def _date_texte(t):
    """2026-10-02 / 31 MARCH 2026 / 15th September 2026 -> date, ou None."""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t or "")
    if m:
        return dt.date(int(m[1]), int(m[2]), int(m[3]))
    m = re.search(r"\b(\d{2})-(\d{2})-(\d{4})\b", t or "")
    if m:
        return dt.date(int(m[3]), int(m[2]), int(m[1]))
    m = re.search(r"(\d{1,2})(?:ST|ND|RD|TH)?\s+([A-Z]+)\s+(\d{4})", (t or "").upper())
    if m and m[2] in MOIS_EN:
        return dt.date(int(m[3]), MOIS_EN[m[2]], int(m[1]))
    return None


def _info_base(nom, path, motif):
    """Edition (lue dans l'en-tete du fichier), date de l'edition et du dernier telechargement d'une base."""
    if not os.path.exists(path):
        return {"base": nom, "edition": "indisponible", "ancienne": True}
    with open(path, encoding="utf-8", errors="replace") as f:
        entete = f.read(3000)
    m = re.search(motif, entete, re.I | re.S)
    edition = re.sub(r"\s+", " ", m.group(1)).strip() if m else "edition inconnue"
    d_ed = _date_texte(edition)
    t = _telecharge(path)
    return {"base": nom, "edition": edition, "date_edition": d_ed.isoformat() if d_ed else None,
            "telecharge": t.strftime("%Y-%m-%dT%H:%MZ") if t else None,
            "ancienne": bool(d_ed and (dt.date.today() - d_ed).days > EDITION_ANCIENNE_JOURS)}


FFVL_DATASETS = "https://www.data.gouv.fr/api/1/organizations/federation-francaise-de-vol-libre/datasets/?page_size=50"


def sites_ffvl(cache_dir):
    """Sites de decollage et de treuil (parapente, delta) de la liste officielle de la FFVL, publiee chaque nuit sur
    data.gouv.fr (« Terrains de pratique de la FFVL », Licence Ouverte v2). Mise en cache 7 jours.
    Liste compacte pour la page : n nom, v commune, cp, la/lo, z altitude, o orientations, t 'T' si treuil seul,
    s statut ('' actif, 'A' autorisation requise, 'I' interdit). None si la liste est indisponible."""
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, "ffvl_terrains.json")
    fresh = not _a_recharger(path)
    if not fresh:
        try:
            ds = requests.get(FFVL_DATASETS, timeout=60).json()["data"]
            ds = next(d for d in ds if d["title"].lower().startswith("terrains de pratique"))
            url = next(r["url"] for r in ds["resources"] if r["url"].endswith(".json"))
            r = requests.get(url, timeout=120)
            r.raise_for_status()
            r.json()                                     # contenu verifie avant de remplacer le cache
            with open(path + ".tmp", "wb") as f:
                f.write(r.content)
            os.replace(path + ".tmp", path)
            _marquer(path)
        except Exception as e:
            print(f"  (sites FFVL indisponibles : {e})")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        terrains = json.load(f)
    out = []
    for t in terrains:
        usages, fonctions = t.get("possible_usages_text") or "", t.get("flying_functions_text") or ""
        if not re.search(r"parapente|delta", usages) or not re.search(r"d.collage|treuil", fonctions):
            continue
        if t.get("latitude") is None or t.get("longitude") is None:
            continue
        st = t.get("status") or ""
        out.append({"n": re.sub(r"\s+", " ", t.get("toponym") or "").strip(), "v": (t.get("city") or "").strip(),
                    "cp": t.get("zip") or "", "la": round(float(t["latitude"]), 5), "lo": round(float(t["longitude"]), 5),
                    "z": t.get("altitude"), "o": (t.get("wind_orientations_ok") or "").replace(";", " "),
                    "t": "T" if "treuil" in fonctions and not re.search(r"d.collage", fonctions) else "",
                    "s": "I" if st.startswith("interdit") else "A" if "autorisation" in st else ""})
    out.sort(key=lambda s: s["n"])
    return out


def load_airspace_db(cache_dir):
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, "france_planeur-net.txt")
    fresh = not _a_recharger(path)
    if not fresh:
        try:
            r = requests.get(AIRSPACE_URL, timeout=120)
            r.raise_for_status()
            with open(path, "w", encoding="utf-8") as f:
                f.write(r.text)
            _marquer(path)
        except Exception as e:
            if not os.path.exists(path):
                print(f"  (base des espaces permanents indisponible : {e})")
                return {}
    db = AirspaceDB()
    fr = _info_base("France (planeur-net)", path, r"version=\s*\S+\s+(\d{4}-\d{2}-\d{2})")
    with open(path, encoding="utf-8", errors="replace") as f:
        src = re.search(r"SOURCE:\s*([^*\n]+?)\s*\*", f.read(3000))
    if src:                                  # date du fichier (fait foi pour l'anciennete) + source annoncee en en-tete
        fr["edition"] = f"fichier du {fr['edition']}, en-tete : {src.group(1).strip().lower()}"
    for rec in parse_openair(path):
        rec["source"] = f"base AIP (planeur-net, {fr['date_edition'] or 'date inconnue'})"
        for key in zone_keys(rec["an"]):
            db.setdefault(key, rec)
    db.be = load_lvzc(cache_dir)
    db.de = load_daec(cache_dir)
    db.nl = load_lvnl(cache_dir)
    db.oaip = {pays: load_openaip(cache_dir, code) for pays, code in OPENAIP_PAYS.items()}
    db.comp = load_complements()
    db.infos = [fr, _info_base("Belgique, Luxembourg (LVZC)", os.path.join(cache_dir, "belgique_lvzc.txt"),
                               r"LAST UPDATED\s+([^\n*]+)"),
                _info_base("Allemagne (DAeC)", os.path.join(cache_dir, "allemagne_daec.txt"),
                           r"effective from\s+([^\n*]+)"),
                _info_base("Pays-Bas (LVNL)", os.path.join(cache_dir, "paysbas_lvnl.json"),
                           r'"edition":\s*"([^"]+)"')] + [
                _info_base(f"{pays} (openAIP)", os.path.join(cache_dir, f"openaip_{code}.txt"), r"\*DATE:\s*(\S+)")
                for pays, code in OPENAIP_PAYS.items()]
    # Edition de plus de 2 mois : signalee dans la source de chaque zone tiree de cette base
    for base, info in zip([db, db.be, db.de, db.nl] + [db.oaip[p] for p in OPENAIP_PAYS], db.infos):
        if info.get("ancienne"):
            for rec in {id(r): r for r in base.values()}.values():
                for zz in [rec] + rec.get("autres", []):
                    if "edition ancienne" not in zz["source"]:
                        zz["source"] += " - edition ancienne, verifier l'AIP"
    return db


class AirspaceDB(dict):
    """Base France (planeur-net) ; .be = base Belgique / Luxembourg (LVZC) ; .de = base Allemagne (DAeC) ;
    .nl = base Pays-Bas (LVNL, officielle) ;
    .comp = complements officiels saisis a la main (zones_complements.txt), prioritaires."""
    be = {}
    de = {}
    nl = {}
    oaip = {}           # Suisse, Italie, Espagne (openAIP) : {pays: base}
    comp = {}
    infos = []          # edition et date de telechargement de chaque base (publiees dans le releve)


_COMPLEMENTS_SUP = None


def complements_sup():
    """Zones de SUP AIP recopiees a la main dans zones_complements.txt (ligne « * SUP AIP : NNN/AA » dans le
    commentaire qui precede la zone) : {numero du SUP: [zones au format de parse_sup_text]}."""
    global _COMPLEMENTS_SUP
    if _COMPLEMENTS_SUP is None:
        _COMPLEMENTS_SUP = {}
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zones_complements.txt")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                lignes = f.read().splitlines()
            recs = {r["an"]: r for r in parse_openair(path)}
            for i, l in enumerate(lignes):
                if not l.startswith("AN "):
                    continue
                j = i - 1                   # commentaires juste au-dessus de la zone (avant la ligne AC)
                while j >= 0 and (lignes[j].startswith("*") or lignes[j].startswith("AC ")):
                    j -= 1
                bloc = "\n".join(lignes[j + 1:i])
                m = re.search(r"\*\s*SUP AIP\s*:\s*(\d{3}/\d{2})", bloc)
                r = recs.get(l[3:].strip())
                if m and r:
                    _COMPLEMENTS_SUP.setdefault(m.group(1), []).append(
                        {"nom": r["an"], "pts": r["pts"], "al": r["al"], "ah": r["ah"], "note": "saisie manuelle"})
    return _COMPLEMENTS_SUP


def load_complements():
    """zones_complements.txt (a cote du script) : zones recopiees de l'AIP officiel, absentes des autres bases."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zones_complements.txt")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        texte = f.read()
    comp = {}
    for rec in parse_openair(path):
        # source = ligne « * Source : … » qui precede la zone dans le fichier
        i = texte.find("AN " + rec["an"])
        m = list(re.finditer(r"\*\s*Source\s*:\s*([^\n]+)", texte[:i]))
        rec["source"] = (m[-1].group(1).strip() if m else "AIP officiel") + " (complement)"
        pm = list(re.finditer(r"\*\s*Pays\s*:\s*([^\n]+)", texte[:i]))
        pays = pm[-1].group(1).strip() if pm else "France"
        for key in zone_keys(rec["an"]):
            comp.setdefault(pays, {}).setdefault(key, rec)    # complements ranges par pays
    return comp


def load_lvzc(cache_dir):
    """Espace aerien Belgique + Luxembourg en OpenAir (LVZC, federation belge de vol a voile, NON OFFICIEL),
    fichier « BELLUX WEEK ALL » de la derniere edition. Cache de 7 jours.
    Une zone decoupee en plusieurs parties (BEL / FRA) garde ses parties dans rec["autres"]."""
    path = os.path.join(cache_dir, "belgique_lvzc.txt")
    fresh = not _a_recharger(path)
    if not fresh:
        try:
            s = requests.Session()
            s.headers["User-Agent"] = "Mozilla/5.0 (zones_secteur.py)"
            idx = s.get(LVZC_INDEX, timeout=60).text
            annees = sorted(set(re.findall(r'href="/index\.php/luchtruim/luchtruimbestanden/(\d{4})"', idx)))
            editions = set()
            for annee in annees[-2:]:              # editions de l'annee en cours et de la precedente
                t_an = s.get(f"{LVZC_INDEX}/{annee}", timeout=60).text
                editions |= set(re.findall(
                    r'href="(/index\.php/luchtruim/luchtruimbestanden/\d{4}/vanaf-(\d{2})-(\d{2})-(\d{4}))"', t_an))
            if not editions:
                raise RuntimeError("aucune edition trouvee")
            page = max(editions, key=lambda e: (e[3], e[2], e[1]))[0]
            t = s.get("https://www.lvzc.be" + page, timeout=60).text
            lien = re.search(r'href="([^"]*bellux-week-all[^"]*/file)"', t)
            if not lien:
                raise RuntimeError("fichier BELLUX WEEK ALL introuvable")
            r = s.get("https://www.lvzc.be" + lien.group(1), timeout=120)
            r.raise_for_status()
            with open(path, "w", encoding="utf-8") as f:
                f.write(r.content.decode("utf-8", "replace"))
            _marquer(path)
        except Exception as e:
            if not os.path.exists(path):
                print(f"  (base Belgique LVZC indisponible : {e})")
                return {}
    with open(path, encoding="utf-8", errors="replace") as f:
        entete = f.read(400)
    m = re.search(r"LAST UPDATED\s+([^\n*]+)", entete)
    label = f"base LVZC non officielle ({m.group(1).strip().lower() if m else 'date inconnue'})"
    return _base_par_identifiant(path, label)


def _base_par_identifiant(path, label, recs=None):
    """Zones d'un fichier OpenAir indexees par identifiant (TSA25C, EDR134...). Les parties d'une meme zone (meme
    identifiant exact, ex. TSA25C BEL / FRA) sont regroupees dans rec["autres"] ; une cle generique (HTA10)
    designe la premiere zone seulement, sans fusion."""
    base = {}
    exacts = {}
    for rec in (recs if recs is not None else parse_openair(path)):
        rec["source"] = label
        cles = zone_keys(rec["an"])
        if not cles:
            continue
        if cles[0] in exacts:
            exacts[cles[0]].setdefault("autres", []).append(rec)
            continue
        exacts[cles[0]] = rec
        for key in cles:
            base.setdefault(key, rec)
    return base


def load_daec(cache_dir):
    """Espace aerien Allemagne en OpenAir (DAeC, Deutscher Aero Club, fichier de la commission vol a voile publie
    gratuitement, NON OFFICIEL) : zones ED-R (dont les TRA, EDR2xx) et ED-D. Derniere edition, cache de 7 jours."""
    path = os.path.join(cache_dir, "allemagne_daec.txt")
    fresh = not _a_recharger(path)
    if not fresh:
        try:
            s = requests.Session()
            s.headers["User-Agent"] = "Mozilla/5.0 (zones_secteur.py)"
            idx = s.get(DAEC_INDEX, timeout=60).text
            lien = re.search(r'href="([^"]*Airspace_Germany[^"]*\.txt)"', idx)
            if not lien:
                raise RuntimeError("fichier Airspace_Germany introuvable")
            r = s.get(requests.compat.urljoin(DAEC_INDEX, lien.group(1)), timeout=120)
            r.raise_for_status()
            if "AC " not in r.text[:20000]:
                raise RuntimeError("contenu inattendu")
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                f.write(r.content.decode("utf-8", "replace"))
            os.replace(path + ".tmp", path)
            _marquer(path)
        except Exception as e:
            if not os.path.exists(path):
                print(f"  (base Allemagne DAeC indisponible : {e})")
                return {}
    with open(path, encoding="utf-8", errors="replace") as f:
        entete = f.read(1500)
    m = re.search(r"effective from\s+([^\n*]+)", entete, re.I)
    base = _base_par_identifiant(path, f"base DAeC non officielle ({m.group(1).strip() if m else 'date inconnue'})")
    # Noms DAeC suivis de l'horaire de l'AIP (« EDR134 Wildflecken MON-FRI+ », « … H24 », « … (2.-3.10.) ») :
    # retire, l'horaire qui compte est celui du NOTAM
    horaire = re.compile(r"\s+(?:(?:MON|TUE|WED|THU|FRI|SAT|SUN)[-A-Z+]*|H24|HX|NOTAM|\([^)]*\))\s*$")
    for rec in {id(r): r for r in base.values()}.values():
        for zz in [rec] + rec.get("autres", []):
            while horaire.search(zz["an"]):
                zz["an"] = horaire.sub("", zz["an"])
    return base


def _limite_lvnl(val, unite, ref):
    """Limite verticale LVNL (valeur, FT/FL, SFC/MSL/STD) au format OpenAir : SFC, 1500FT AMSL, FL55."""
    if val is None:
        return ""
    if (unite or "").upper() == "FL":
        return f"FL{int(val)}"
    if not val and (ref or "").upper() in ("SFC", "GND"):
        return "SFC"
    return f"{int(val)}FT {'AGL' if (ref or '').upper() in ('SFC', 'GND') else 'AMSL'}"


def _anneau_lvnl(a):
    """Contour LVNL ([lon, lat]...) en Ring. LVNL livre ses cercles densifies (~1800 points) sans dire que ce sont
    des cercles : un contour dont tous les points sont a la meme distance du centre (a 20 m pres) redevient un
    cercle OpenAir V X= / DC (rayon arrondi au millieme de NM). Les autres contours sont alleges a ~360 points."""
    if len(a) >= 200:
        pts = a[:-1] if a[0] == a[-1] else a
        clo, cla = sum(x for x, _ in pts) / len(pts), sum(y for _, y in pts) / len(pts)
        ds = [GEOD.inv(clo, cla, x, y)[2] for x, y in pts]
        r = sum(ds) / len(ds)
        if max(ds) - min(ds) < max(20.0, 0.005 * r):
            return circle_ll(cla, clo, round(r / NM, 3) * NM)
    pas = max(1, len(a) // 360)
    pts = [(la, lo) for lo, la in a[::pas]]
    if pts[-1] != (a[-1][1], a[-1][0]):
        pts.append((a[-1][1], a[-1][0]))
    return Ring(pts, [f"DP {oa_pt(la, lo)}" for la, lo in pts])


def load_lvnl(cache_dir):
    """Espace aerien Pays-Bas (LVNL, geoportail officiel, CC BY 4.0) : TSA, TRA, zones R, D et P, indexees par
    designateur (EHTSA55, EHTRA59A, EHR64, EHD41B, EHP26A). Cache de 7 jours (paysbas_lvnl.json)."""
    path = os.path.join(cache_dir, "paysbas_lvnl.json")
    if _a_recharger(path):
        try:
            feats = []
            for couche in LVNL_COUCHES:
                r = requests.get(f"{LVNL_SERVICE}/{couche}/query", timeout=120, params={
                    "where": "1=1", "outFields": "*", "outSR": 4326, "f": "geojson"})
                r.raise_for_status()
                feats += r.json()["features"]
            if not feats:
                raise RuntimeError("aucune zone recue")
            # date d'effet du cycle AIRAC : millisecondes depuis 1970 (requete) ou texte (export du geoportail)
            dates = [f["properties"].get("EffectiveDate") for f in feats if f["properties"].get("EffectiveDate")]
            dates = [dt.datetime.fromtimestamp(x / 1000, UTC).date() if isinstance(x, (int, float)) else _date_texte(x)
                     for x in dates]
            ed = max((x for x in dates if x), default=None)
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"edition": ed.isoformat() if ed else "date inconnue", "features": feats}, f,
                          ensure_ascii=False)
            os.replace(path + ".tmp", path)
            _marquer(path)
        except Exception as e:
            if not os.path.exists(path):
                print(f"  (base Pays-Bas LVNL indisponible : {e})")
                return {}
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    label = f"base LVNL officielle ({d.get('edition', 'date inconnue')})"
    base = {}
    for f in d["features"]:
        p, g = f["properties"], f.get("geometry") or {}
        anneaux = [g["coordinates"][0]] if g.get("type") == "Polygon" else \
            [c[0] for c in g.get("coordinates", [])] if g.get("type") == "MultiPolygon" else []
        recs = []
        for a in anneaux:
            recs.append({"ac": "R", "an": f"{p.get('Designator', '')} {p.get('Name') or ''}".strip(),
                         "al": _limite_lvnl(p.get("LowerLimit"), p.get("LowerLimitUnit"), p.get("LowerLimitReference")),
                         "ah": _limite_lvnl(p.get("UpperLimit"), p.get("UpperLimitUnit"), p.get("UpperLimitReference")),
                         "pts": _anneau_lvnl(a), "source": label})
        if not recs:
            continue
        rec = recs[0]
        if len(recs) > 1:
            rec["autres"] = recs[1:]
        for key in zone_keys(p.get("Designator") or ""):
            base.setdefault(key, rec)
    return base


def load_openaip(cache_dir, code):
    """Espace aerien d'un pays (ch, it, es) : donnees openAIP converties en OpenAir par AirspaceConverter (alus.it),
    communautaires et NON OFFICIELLES (licence CC BY-NC 4.0). Indexees par identifiant (LSR10, LED50, LETSA31,
    LI TSA113...). Cache de 7 jours (openaip_<code>.txt)."""
    path = os.path.join(cache_dir, f"openaip_{code}.txt")
    if _a_recharger(path):
        try:
            r = requests.get(OPENAIP_URL.format(code), timeout=120, headers={"User-Agent": "Mozilla/5.0 (zones_secteur.py)"})
            r.raise_for_status()
            if "AC " not in r.text[:20000]:
                raise RuntimeError("contenu inattendu")
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                f.write(r.content.decode("utf-8", "replace"))
            os.replace(path + ".tmp", path)
            _marquer(path)
        except Exception as e:
            if not os.path.exists(path):
                print(f"  (base openAIP {code} indisponible : {e})")
                return {}
    with open(path, encoding="utf-8", errors="replace") as f:
        m = re.search(r"\*DATE:\s*(\S+)", f.read(2000))
    d = _date_texte(m.group(1)) if m else None
    label = f"base openAIP non officielle ({d.isoformat() if d else 'date inconnue'})"
    if d and (dt.date.today() - d).days > EDITION_ANCIENNE_JOURS:
        label += " - edition ancienne, verifier l'AIP"
    recs = parse_openair(path)
    # zones sans identifiant (zones de tir italiennes « CLOU NEF (FIRINGAREA) ») : index par nom
    OPENAIP_NOMS[code] = {_fold(rec["an"]): rec for rec in recs}
    return _base_par_identifiant(path, label, recs)


def _zone_tir_italienne(e, base):
    """« MIL FIRING AREA /LAGO OMODEO-SW NUORO/ » : zone « LAGO OMODEO (FIRINGAREA) » de la base openAIP italienne."""
    m = re.search(r"FIRING\s+AREA\s*/\s*([A-Z' ]+?)\s*(?:-[^/]*)?/", e or "")
    if not m or not base:
        return []
    cle = _fold(m.group(1) + " (FIRINGAREA)")
    z = OPENAIP_NOMS.get("it", {}).get(cle)
    return [(cle, z, m.start())] if z else []


def parse_openair(path):
    """Lit un fichier OpenAir : [{ac, an, al, ah, pts (Ring densifie, avec .oa = lignes d'origine)}]."""
    recs = []
    cur = None
    center, cw = None, True

    def close():
        if cur and len(cur["pts"]) >= 3:
            cur["pts"] = Ring(cur["pts"], cur.pop("oa"))      # garde les lignes OpenAir d'origine
            recs.append(cur)

    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.split("*")[0].strip()
            if not line:
                continue
            tag, _, val = line.partition(" ")
            tag = tag.upper()
            if tag == "AC":
                close()
                cur = {"ac": val.strip(), "an": "", "al": "", "ah": "", "pts": [], "oa": []}
                center, cw = None, True
            elif cur is None:
                continue
            elif tag in ("V", "DP", "DC", "DA", "DB"):
                cur["oa"].append(f"{tag} {val.strip()}")
                if tag == "V":
                    v = val.replace(" ", "").upper()
                    if v.startswith("X="):
                        center = _oa_pt(val)
                    elif v.startswith("D="):
                        cw = not v.endswith("-")
                elif tag == "DP":
                    p = _oa_pt(val)
                    if p:
                        cur["pts"].append(p)
                elif tag == "DC" and center:
                    cur["pts"] += circle_ll(center[0], center[1], float(val.strip()) * NM)
                elif tag == "DA" and center:
                    r_nm, a1, a2 = [float(x) for x in val.split(",")]
                    cur["pts"] += _arc(center, r_nm * NM, a1, a2, cw)
                elif tag == "DB" and center:
                    ms = list(OA_COORD_RE.finditer(val))
                    if len(ms) == 2:
                        p1, p2 = _oa_pt(ms[0].group(0)), _oa_pt(ms[1].group(0))
                        az1, _, r_m = GEOD.inv(center[1], center[0], p1[1], p1[0])
                        az2, _, _ = GEOD.inv(center[1], center[0], p2[1], p2[0])
                        cur["pts"] += _arc(center, r_m, az1 % 360, az2 % 360, cw)
            elif tag == "AN":
                cur["an"] = val.strip()
            elif tag == "AL":
                cur["al"] = val.strip()
            elif tag == "AH":
                cur["ah"] = val.strip()
        close()
    return recs


def _normaliser_ids(text):
    """Variantes d'ecriture des identifiants : TSA espagnole « LETS31 » dans les NOTAM, « LETSA31 » dans la base."""
    return re.sub(r"\bLETS\s?(\d)", r"LETSA\1", (text or "").upper())


# Zone regroupant plusieurs sous-zones : « LF-R203ABC » (nom de la base) ou « LF-R203A/B/C » (texte d'un NOTAM)
ZONE_GROUPE_RE = re.compile(r"\b(?:LF-?)?(R|D|P)\s?-?\s?(\d{1,3})(?:([A-H]{3,5})|([A-H])((?:/[A-H])+))(?![\w.])")


def _groupes(text):
    """[(cle de la zone groupee, [cles des sous-zones]), ...] : R203ABC -> R203A, R203B, R203C."""
    out = []
    for m in ZONE_GROUPE_RE.finditer(text):
        lettres = m[3] or (m[4] + m[5].replace("/", ""))
        out.append((f"{m[1]}{int(m[2])}{lettres}", [f"{m[1]}{int(m[2])}{x}" for x in lettres], m.start()))
    return out


def zone_keys(text):
    t = _normaliser_ids(text)
    keys = []
    for k, sous, _ in _groupes(t):
        keys += [k] + sous + [k[:len(k) - len(sous)]]
    for m in ZONE_ID_RE.finditer(t):
        k = f"{m[1]}{int(m[2])}{m[3] or ''}"
        keys.append(k)
        if m[3]:
            keys.append(f"{m[1]}{int(m[2])}")
    return list(dict.fromkeys(keys))


def named_zones_in_text(text, db):
    """Zones permanentes citees dans le texte et presentes dans la base (« R203A/B/C » : chaque sous-zone ; une zone
    de la base qui les regroupe toutes, « R203ABC », n'est retenue qu'une fois)."""
    t = _normaliser_ids(text)
    found = []
    cites = [(k, pos) for g, sous, pos in _groupes(t) for k in [g] + sous]
    cites += [(f"{m[1]}{int(m[2])}{m[3] or ''}", m.start()) for m in ZONE_ID_RE.finditer(t)]
    for k, pos in sorted(cites, key=lambda c: c[1]):
        z = db.get(k)
        if z and all(z is not f[1] for f in found):
            found.append((k, z, pos))
    return found

# --------------------------------------------------------------------------
# Horaires (item D)
# --------------------------------------------------------------------------

MONTHS = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
WDAYS = {d: i for i, d in enumerate("MON TUE WED THU FRI SAT SUN".split())}
TIME_RE = re.compile(
    r"(SR|SS|\d{4})(?:\s*(MINUS|PLUS)\s*(\d+))?\s*-\s*(SR|SS|\d{4})(?:\s*(MINUS|PLUS)\s*(\d+))?")


def _t(tok, op, val, date, sr, ss):
    if tok == "SR":
        t = sr
    elif tok == "SS":
        t = ss
    else:
        h, m = int(tok[:2]), int(tok[2:])
        t = dt.datetime(date.year, date.month, date.day, tzinfo=UTC) + dt.timedelta(hours=h, minutes=m)
    if op:
        t += dt.timedelta(minutes=int(val) * (1 if op == "PLUS" else -1))
    return t


_JOURS_SEM = "MON|TUE|WED|THU|FRI|SAT|SUN"
_CONT_SEM_RE = re.compile(r"\b(" + _JOURS_SEM + r")\s*(\d{4})\s*-\s*(" + _JOURS_SEM + r")\s*(\d{4})\b")
_CONT_JOUR_RE = re.compile(r"(?<![\d:])(?:\b([A-Z]{3})\s+)?(\d{1,2})\s+(\d{4})\s*-\s*(?:([A-Z]{3})\s+)?(\d{1,2})\s+(\d{4})\b")


def _periodes_continues(d, date):
    """Periodes continues sur plusieurs jours, decoupees sur la journee `date` :
    « MON 0645-FRI 1045 » (chaque semaine), « 12 2200-15 2200 », « OCT 30 2200-NOV 02 0600 ».
    Renvoie (intervalles ou None si aucune periode, texte restant)."""
    jour0 = dt.datetime(date.year, date.month, date.day, tzinfo=UTC)
    jour1 = jour0 + dt.timedelta(days=1)
    out, trouve = [], False

    def garder(a, b):
        x, y = max(a, jour0), min(b, jour1)
        if y > x:
            out.append((x, y))

    def heure(j, hhmm):
        return dt.datetime(j.year, j.month, j.day, tzinfo=UTC) + dt.timedelta(hours=int(hhmm[:2]), minutes=int(hhmm[2:]))

    def rep_sem(m):
        nonlocal trouve
        trouve = True
        w0, w1 = WDAYS[m[1]], WDAYS[m[3]]
        for decal in range(-7, 1):                    # occurrence de la semaine en cours ou precedente
            debut = date + dt.timedelta(days=decal)
            if debut.weekday() != w0:
                continue
            fin = debut + dt.timedelta(days=(w1 - w0) % 7)
            a, b = heure(debut, m[2]), heure(fin, m[4])
            if b <= a:
                b += dt.timedelta(days=7)
            garder(a, b)
        return " "

    def rep_jour(m):
        nonlocal trouve
        m1 = MONTHS.get(m[1]) if m[1] else None
        m2 = MONTHS.get(m[4]) if m[4] else None
        if (m[1] and not m1) or (m[4] and not m2):
            return m.group(0)
        trouve = True
        d1, d2 = int(m[2]), int(m[5])
        for annee, mois in ((date.year, date.month), ((date - dt.timedelta(days=31)).year, (date - dt.timedelta(days=31)).month)):
            mois_a = m1 or mois
            try:
                a_j = dt.date(annee, mois_a, d1)
            except ValueError:
                continue
            mois_b = m2 or (mois_a if d2 >= d1 else mois_a % 12 + 1)
            annee_b = annee + (1 if mois_b < mois_a else 0)
            try:
                b_j = dt.date(annee_b, mois_b, d2)
            except ValueError:
                continue
            garder(heure(a_j, m[3]), heure(b_j, m[6]))
        return " "

    reste = _CONT_SEM_RE.sub(rep_sem, d)
    reste = _CONT_JOUR_RE.sub(rep_jour, reste)
    if not trouve:
        return None, d
    return sorted(set(out)), reste


_PERIODE_MOIS_RE = re.compile(r"\b([A-Z]{3})\s*(\d{1,2})\s*-\s*([A-Z]{3})\s*(\d{1,2})\b")


def _periodes_mois(d):
    """« SEP 10-OCT 23 » -> jeton « P0910-1023 » (periode a cheval sur deux mois)."""
    def rep(m):
        if m[1] in MONTHS and m[3] in MONTHS:
            return f" P{MONTHS[m[1]]:02d}{int(m[2]):02d}-{MONTHS[m[3]]:02d}{int(m[4]):02d} "
        return m.group(0)
    return _PERIODE_MOIS_RE.sub(rep, d)


def _dans_periode(tok, date):
    a, b = int(tok[1:5]), int(tok[6:10])
    x = date.month * 100 + date.day
    return a <= x <= b if a <= b else (x >= a or x <= b)


def _clauses_avec_exceptions(d, date, sr, ss):
    """Decoupe l'item D en clauses (virgules), chacune avec ses propres EXC, et reunit les creneaux."""
    morceaux = [m.strip() for m in d.split(",") if m.strip()]
    clauses = []
    for m in morceaux:
        a_heure = bool(_HEURES_RE.search(m)) or "H24" in m
        if clauses and not a_heure and re.search(r"\bEXC\b", clauses[-1]) and not re.search(r"\bEXC\b", m):
            clauses[-1] += " " + m                  # suite d'une liste d'exceptions : « EXC SAT, SUN »
        elif clauses and not re.search(r"\bEXC\b", clauses[-1]) and not _HEURES_RE.search(clauses[-1]) \
                and "H24" not in clauses[-1]:
            clauses[-1] += " " + m                  # jours sans heure suivis de la suite : « OCT 19, 21 0800-1200 »
        else:
            clauses.append(m)
    total, ok_total, mois = [], True, None
    for c in clauses:
        principal = re.split(r"\bEXC\b", c)[0]
        mm = re.findall(r"\b(" + "|".join(MONTHS) + r")\b", principal)
        if not mm and mois and re.match(r"\s*\d", principal):
            c = f"{mois} {c}"                       # « OCT 19 0800-1200, 21 1000-1500 » : le mois se propage
        elif mm:
            mois = mm[-1]
        iv, ok = _avec_exceptions(c, date, sr, ss)
        total += iv
        ok_total = ok_total and ok
    return sorted(total), ok_total


def _soustraire(intervals, retrait):
    out = []
    for a, b in intervals:
        morceaux = [(a, b)]
        for x, y in retrait:
            morceaux = [p for m0, m1 in morceaux for p in ((m0, min(m1, x)), (max(m0, y), m1)) if p[1] > p[0]]
        out += morceaux
    return out


_HEURES_RE = re.compile(r"(?:SR|SS|\d{4})(?:\s*(?:MINUS|PLUS)\s*\d+)?\s*-\s*(?:SR|SS|\d{4})(?:\s*(?:MINUS|PLUS)\s*\d+)?")


def _avec_exceptions(d, date, sr, ss):
    """Item D avec EXC : « MON-FRI 0800-1700 EXC HOL », « DLY 0600-1800 EXC SAT SUN », « H24 EXC 1200-1300 »,
    « OCT 20-31 0800-1600 EXC 25 », « 0700-1900 EXC PUB HOL »."""
    morceaux = re.split(r"\bEXC\b", d)
    principal = morceaux[0].strip(" ,")
    intervals, ok = schedule_for_day(principal, date, sr, ss) if principal else (None, True)
    base = dt.datetime(date.year, date.month, date.day, tzinfo=UTC)
    if intervals is None:
        intervals = [(base, base + dt.timedelta(days=1))]
    for ex in morceaux[1:]:
        ex = ex.strip(" ,")
        heures = [TIME_RE.fullmatch(m.group(0)) for m in _HEURES_RE.finditer(ex)]
        reste = _HEURES_RE.sub(" ", ex)
        tokens = re.findall(r"P\d{4}-\d{4}|[A-Z]{3}(?:\s*-\s*[A-Z]{3})?|\d{1,2}\s*-\s*\d{1,2}|\d{1,2}|\S", reste)
        month, jours_ex, periodes, wd, hol, compris = None, [], [], set(), False, True
        for tok in tokens:
            tok = tok.strip()
            if tok in (",", "AND", "&", "."):
                continue
            if tok == "HOL":
                hol = True
            elif re.fullmatch(r"P\d{4}-\d{4}", tok):
                periodes.append(tok)
            elif tok in MONTHS:
                month = MONTHS[tok]
            elif tok in WDAYS:
                wd.add(WDAYS[tok])
            elif re.fullmatch(r"[A-Z]{3}\s*-\s*[A-Z]{3}", tok) and all(x.strip() in WDAYS for x in tok.split("-")):
                i, j = [WDAYS[x.strip()] for x in tok.split("-")]
                wd |= {k % 7 for k in range(i, j + 1 + (7 if j < i else 0))}
            elif re.fullmatch(r"\d{1,2}\s*-\s*\d{1,2}", tok):
                a, b = [int(x) for x in tok.split("-")]
                jours_ex += [(month, k) for k in range(a, b + 1)]
            elif re.fullmatch(r"\d{1,2}", tok):
                jours_ex.append((month, int(tok)))       # le jour garde le mois cite juste avant
            else:
                compris = False
        if not compris:
            ok = False
            continue
        jour_vise = (hol and est_ferie(date)) or date.weekday() in wd or \
            any(j == date.day and (m is None or m == date.month) for m, j in jours_ex) or \
            any(_dans_periode(p, date) for p in periodes)
        sans_jour = not (hol or wd or jours_ex or periodes)
        if heures:
            if jour_vise or sans_jour:
                retrait = []
                for tm in heures:
                    a = _t(tm[1], tm[2], tm[3], date, sr, ss)
                    b = _t(tm[4], tm[5], tm[6], date, sr, ss)
                    retrait.append((a, b if b > a else b + dt.timedelta(days=1)))
                intervals = _soustraire(intervals, retrait)
        elif jour_vise:
            intervals = []
        elif sans_jour:
            ok = False
    return intervals, ok


def schedule_for_day(item_d, date, sr, ss):
    """Renvoie (liste d'intervalles UTC ce jour, interprete_ok)."""
    if not item_d or not item_d.strip():
        return None, True                      # pas d'item D : actif sur toute la validite
    d = item_d.upper().replace("\n", " ")
    d = re.sub(r"\bPUB(?:LIC)?\s+HOL(?:IDAYS?)?\b|\bHOLIDAYS?\b", "HOL", d)
    d = re.sub(r"\bWEEK\s*-?\s*ENDS?\b", "SAT SUN", d)          # EXC WEEKENDS = EXC SAT SUN
    d = re.sub(r"\bEVERY\b", " ", d)
    continues, d = _periodes_continues(d, date)
    if continues is not None:
        if not d.strip(" ,"):                   # l'item D ne contient que des periodes continues
            return continues, True
        reste, ok = schedule_for_day(d, date, sr, ss)
        base = dt.datetime(date.year, date.month, date.day, tzinfo=UTC)
        reste = [(base, base + dt.timedelta(days=1))] if reste is None else reste
        return sorted(continues + reste), ok
    d = _periodes_mois(d)
    if re.search(r"\bEXC\b", d):
        return _clauses_avec_exceptions(d, date, sr, ss)
    d = re.sub(r"\bDAILY\b|\bDLY\b", " ", d)
    if re.fullmatch(r"\s*H24\s*", d):
        return None, True
    tokens = re.findall(
        r"(?:SR|SS|\d{4})(?:\s*(?:MINUS|PLUS)\s*\d+)?\s*-\s*(?:SR|SS|\d{4})(?:\s*(?:MINUS|PLUS)\s*\d+)?"
        r"|P\d{4}-\d{4}|H24|[A-Z]{3}(?:\s*-\s*[A-Z]{3})?|\d{1,2}\s*-\s*\d{1,2}|\d{1,2}|,", d)
    periodes = []
    month = None
    days, wdays = [], []
    hol = False
    have_days = False
    applies_any_rule = False
    intervals = []
    pending_reset = False
    understood = True
    for tok in tokens:
        tok = tok.strip()
        if tok == ",":
            continue
        tm = TIME_RE.fullmatch(tok)
        if tm or tok == "H24":
            if not days and not wdays and not hol and not periodes:
                match = True
            else:
                match = ((date.day in days and (month is None or month == date.month)) or (date.weekday() in wdays)
                         or (hol and est_ferie(date)) or any(_dans_periode(p, date) for p in periodes))
            applies_any_rule = True
            if match:
                if tok == "H24":
                    base = dt.datetime(date.year, date.month, date.day, tzinfo=UTC)
                    intervals.append((base, base + dt.timedelta(days=1)))
                else:
                    a = _t(tm[1], tm[2], tm[3], date, sr, ss)
                    b = _t(tm[4], tm[5], tm[6], date, sr, ss)
                    if b <= a:
                        b += dt.timedelta(days=1)
                    intervals.append((a, b))
            pending_reset = True
            continue
        if pending_reset:
            days, wdays, hol, periodes = [], [], False, []
            pending_reset = False
        if re.fullmatch(r"P\d{4}-\d{4}", tok):
            periodes.append(tok)
            continue
        if re.fullmatch(r"[A-Z]{3}\s*-\s*[A-Z]{3}", tok):
            a, b = [x.strip() for x in tok.split("-")]
            if a in WDAYS and b in WDAYS:
                i, j = WDAYS[a], WDAYS[b]
                wdays += [k % 7 for k in range(i, j + 1 + (7 if j < i else 0))]
            elif a in MONTHS and b in MONTHS:
                understood = False
            else:
                understood = False
        elif tok in MONTHS:
            month = MONTHS[tok]
        elif tok == "HOL":
            hol = True
        elif tok in WDAYS:
            wdays.append(WDAYS[tok])
        elif re.fullmatch(r"\d{1,2}\s*-\s*\d{1,2}", tok):
            a, b = [int(x) for x in tok.split("-")]
            days += list(range(a, b + 1)) if a <= b else list(range(a, 32)) + list(range(1, b + 1))
            have_days = True
        elif re.fullmatch(r"\d{1,2}", tok):
            days.append(int(tok))
            have_days = True
        else:
            understood = False
    if not applies_any_rule:
        return None, False
    return intervals, understood

# --------------------------------------------------------------------------
# Geometrie d'un NOTAM
# --------------------------------------------------------------------------

# Bloc d'un NOTAM RTBA : "ZONE R45A BOURGOGNE" suivi d'une ou plusieurs lignes "0800-1000:ACTIVE"
RTBA_ZONE_RE = re.compile(r"^\s*ZONE\s+(R\s?\d{1,3}\s?(?:[A-Z]{1,2}\d?(?:\.\d)?)?)(?![\w.])\s*([^\n]*)$", re.M)


def rtba_blocks(e):
    """[(id, nom, [(HHMM, HHMM), ...]), ...] pour un NOTAM d'activation RTBA."""
    heads = list(RTBA_ZONE_RE.finditer(e))
    out = []
    for i, h in enumerate(heads):
        body = e[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(e)]
        times = re.findall(r"(\d{4})\s*-\s*(\d{4})\s*:?\s*ACTIVE", body)
        out.append((re.sub(r"\s+", "", h[1]), h[2].strip(), times, (h.group(0).strip() + "\n" + body.strip()).strip()))
    return out


def rtba_display_id(zid):
    """'R45B' -> 'R 45 B', 'R45S6.1' -> 'R 45 S6.1' (format de la carte AZBA)."""
    m = re.fullmatch(r"R(\d+)(.*)", zid)
    return f"R {m[1]}" + (f" {m[2]}" if m and m[2] else "") if m else zid


def _fold(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().upper()
    return re.sub(r"[^A-Z0-9]+", " ", s).strip()


def _zone_in_text(name, efold):
    core = re.sub(r"^(ZRT|ZIT|ZDT|ZONE)\s+", "", _fold(name))
    return len(core) >= 3 and re.search(r"\b" + re.escape(core) + r"\b", efold) is not None


def notam_parts(n, db, day, sc=None, sup_refs_seen=()):
    """Decoupe un NOTAM en zones : liste de dicts {pts, label, src, al, ah, hours, alt_sup}.
    hours = None (horaires du NOTAM) ou liste d'intervalles propres a la zone."""
    e = (n.get("itemE") or "").upper()
    efold = _fold(e)
    trig = n["qLine"]["code45"] == "TT" or "TRIGGER NOTAM" in e
    base = dt.datetime(day.year, day.month, day.day, tzinfo=UTC)

    # 1) Activation RTBA : une zone par ligne, avec ses propres horaires
    rt = rtba_blocks(e) if "RTBA" in e else []
    if rt and db:
        parts = []
        for zid, zname, times, raw in rt:
            z = next((db[k] for k in zone_keys(zid) if k in db), None)
            hrs = []
            for a_, b_ in times:
                t0 = base + dt.timedelta(hours=int(a_[:2]), minutes=int(a_[2:]))
                t1 = base + dt.timedelta(hours=int(b_[:2]), minutes=int(b_[2:]))
                hrs.append((t0, t1 if t1 > t0 else t1 + dt.timedelta(days=1)))
            if not z:
                print(f"  (RTBA {zid} absente de la base des espaces permanents : ignoree)")
                continue
            parts.append({"pts": z["pts"], "label": f"RTBA {zid} {zname}".strip(),
                          "titre": f"RTBA - {rtba_display_id(zid)}", "sous_titre": zname.title(), "texte": raw,
                          "src": f"base AIP (planeur-net : {z['an']})",
                          "al": z["al"], "ah": z["ah"], "hours": hrs, "alt_sup": True})
        if parts:
            return parts

    # 2) Geometrie ecrite dans le NOTAM
    polys, src = notam_geometry(n)
    if "texte NOTAM" in src:
        return [{"pts": p, "label": "", "src": src, "al": "", "ah": "", "hours": None} for p in polys]

    # 2b) Zones definies dans un SUP AIP (PDF) : SUP cite, ou nom de ZRT d'un SUP vu dans la zone
    if sc:
        refs = re.findall(r"(?:AIP\s+)?SUP\s+(?:AIP\s+)?(?:AIRAC\s+)?(\d{3}/\d{2})", e)
        cands = []
        for ref in (refs[:1] or sup_refs_seen):
            for zz in sc.zones(ref):
                if zz["pts"]:
                    cands.append((ref, zz))
        named_sup = [(r, zz) for r, zz in cands if _zone_in_text(zz["nom"], efold)]
        sel = named_sup if (named_sup and not trig) else (cands if refs else [])
        if sel:
            return [{"pts": zz["pts"], "label": zz["nom"],
                     "src": f"SUP AIP {r} (" + ("recopie manuelle" if zz["note"] == "saisie manuelle" else f"PDF, {zz['note']}") + ")",
                     "al": zz["al"], "ah": zz["ah"], "hours": None, "alt_sup": True} for r, zz in sel]

    # 3) Zone permanente citee (LF-R602, R139A, TSA...) : seulement pour un NOTAM de restriction (code Q en R..).
    #    Un avertissement (W..) qui mentionne une zone (« en dehors de l'activation de la LF-R45N3 ») garde sa
    #    propre geometrie (PSN, ligne Q).
    restriction = n["qLine"]["code23"].startswith("R")
    named = []
    if db and restriction:
        # NOTAM belge ou luxembourgeois : base LVZC d'abord (meme numero possible en France : R67, TSA22...)
        # NOTAM allemand : base DAeC seule (EDR95 n'est pas la R95 francaise)
        be, de, nl = (getattr(db, k, {}) or {} for k in ("be", "de", "nl"))
        pays = notam_country(n)
        oaip = getattr(db, "oaip", {}) or {}
        bases = ([be, db] if pays in ("Belgique", "Luxembourg") else [de] if pays == "Allemagne"
                 else [nl] if pays == "Pays-Bas" else [oaip.get(pays, {})] if pays in OPENAIP_PAYS else [db, be])
        # complements officiels du meme pays que le NOTAM d'abord
        bases = [(getattr(db, "comp", {}) or {}).get(notam_country(n), {})] + bases
        for base in bases:
            named = named_zones_in_text(e, base) if base else []
            if named:
                break
        if not named and pays == "Italie":
            named = _zone_tir_italienne(e, oaip.get("Italie", {}))
    if named:
        parts = []
        for _, z, _ in named:
            morceaux = [z] + z.get("autres", [])
            # Zone en plusieurs parties aux limites differentes (ex. TSA26B : plancher 4500 ft AMSL en Belgique,
            # FL65 dans la CBA16B cote France) : chaque partie garde ses limites, celles du NOTAM sont l'enveloppe.
            propres = len({(m["al"], m["ah"]) for m in morceaux}) > 1
            for zz in morceaux:
                parts.append({"pts": zz["pts"], "label": zz["an"], "src": zz.get("source", "base AIP (planeur-net)"),
                              "al": zz["al"], "ah": zz["ah"], "hours": None, "alt_sup": propres})
        return parts

    # 4) Cercle de la ligne Q (approximation)
    return [{"pts": p, "label": "", "src": src, "al": "", "ah": "", "hours": None} for p in polys]


def notam_geometry(n):
    """Liste de polygones (lat, lon) + description de la source."""
    e = (n.get("itemE") or "").upper()
    coords = coords_in_text(e)
    circles = []
    for m in re.finditer(
            r"(\d+(?:[.,]\d+)?)\s?(NM|KM|M)\s+RADIUS|RADIUS\s*(?:OF\s*)?:?\s*(\d+(?:[.,]\d+)?)\s?(NM|KM|M)\b", e):
        val = float((m[1] or m[3]).replace(",", "."))
        unit = m[2] or m[4]
        r_m = val * (NM if unit == "NM" else 1000 if unit == "KM" else 1)
        after = [c for c in coords if c[0] >= m.start()]
        before = [c for c in coords if c[0] < m.start()]
        c = after[0] if after else (before[-1] if before else None)
        if c:
            circles.append((c[1], c[2], r_m))
    if circles:
        return [circle_ll(la, lo, r) for la, lo, r in circles], "texte NOTAM (cercle)"
    if len(coords) >= 3:
        pts = [(c[1], c[2]) for c in coords]
        if len(pts) > 3 and abs(pts[0][0] - pts[-1][0]) < 1e-6 and abs(pts[0][1] - pts[-1][1]) < 1e-6:
            pts = pts[:-1]
        note = "texte NOTAM (polygone" + (", arcs approxim.)" if "ARC" in e else ")")
        return [pts], note
    q = n.get("coordinates", "")
    m = re.fullmatch(r"(\d{4}[NS])(\d{5}[EW])", q)
    if not m:
        return [], "aucune"
    la, lo = parse_coord(m[1], True), parse_coord(m[2], False)
    r_nm = max(float(n.get("radius") or 1), 0.5)
    if len(coords) == 1:
        # Une seule position dans le texte (PSN) : plus precise que le centre de la ligne Q.
        la, lo = coords[0][1], coords[0][2]
        lm = re.search(r"LENGTH\s*:?\s*(\d+)\s?M\b", e)
        if lm:
            return [circle_ll(la, lo, int(lm[1]) / 2 + 500)], "PSN du texte + longueur d'axe (approx.)"
        return [circle_ll(la, lo, r_nm * NM)], f"PSN du texte, rayon ligne Q {int(r_nm)} NM (approx.)"
    return [circle_ll(la, lo, r_nm * NM)], f"ligne Q (cercle {int(r_nm)} NM, approx.)"


def vertical(n):
    f = (n.get("itemF") or "").strip().upper()
    g = (n.get("itemG") or "").strip().upper()
    q = n["qLine"]
    if not f:
        f = "SFC" if q["lower"] == 0 else f"FL{q['lower']}"
    if not g:
        g = f"FL{q['upper']}"
    return re.sub(r"^FL0*(\d+)", r"FL\1", f), re.sub(r"^FL0*(\d+)", r"FL\1", g)


COUNTRY_PREFIX = {"LF": "France", "EB": "Belgique", "EL": "Luxembourg", "ED": "Allemagne", "ET": "Allemagne",
                  "EH": "Pays-Bas", "EG": "Royaume-Uni", "LS": "Suisse", "LI": "Italie", "LE": "Espagne",
                  "LO": "Autriche", "LN": "Monaco"}


def notam_country(n):
    """Pays d'un NOTAM d'apres l'item A (ou la FIR de la ligne Q) : EB -> Belgique, LF -> France..."""
    for code in ((n.get("itemA") or "").split() + [n["qLine"].get("fir", "")]):
        if code[:2] in COUNTRY_PREFIX:
            return COUNTRY_PREFIX[code[:2]]
    return "Autre"


def floor_ft(s):
    """Plancher approximatif en pieds : FLn -> n*100, 'xFT AMSL/AGL' -> x, SFC/GND -> 0.
    (AGL compte comme AMSL : approximation qui ne masque jamais une zone basse.)"""
    s = (s or "").upper()
    m = re.search(r"FL\s*(\d+)", s)
    if m:
        return int(m[1]) * 100
    m = re.search(r"(\d+)\s*FT", s)
    if m:
        return int(m[1])
    m = re.search(r"(\d+)\s*M\b", s)
    return round(int(m[1]) * 3.28084) if m else 0


def floor_fl(s):
    """Plancher approximatif en FL (AGL/SFC -> 0)."""
    s = (s or "").upper()
    m = re.search(r"FL\s*(\d+)", s)
    if m:
        return int(m[1])
    m = re.search(r"(\d+)\s*FT", s)
    if m and "AGL" not in s and "ASFC" not in s:
        return int(m[1]) // 100
    return 0


def openair_class(code23):
    if code23 == "RP":
        return "P"
    if code23 == "RD":
        return "Q"
    if code23.startswith("R"):
        return "R"
    return "Q"           # avertissements (W..) : classe "danger" pour l'affichage


def zone_name(n):
    e = n.get("itemE") or ""
    m = re.search(r"'([^']{3,60})'", e)
    base = m.group(1) if m else e.split("\n")[0][:50]
    return re.sub(r"\s+", " ", base).strip()

# --------------------------------------------------------------------------
# Principal
# --------------------------------------------------------------------------

def prepare_context(notams):
    """SUP AIP en vigueur, base des espaces permanents, PDF des SUP cites par les NOTAM."""
    here = os.path.dirname(os.path.abspath(__file__))
    sups = sup_aip_list(os.path.join(here, "cache"))
    print(f"Liste des SUP AIP : {sups.info['nb']} SUP, lue le {sups.info['lu'] or '?'}"
          + (" (COPIE : site du SIA indisponible)" if sups.info["copie"] else ""))
    db = load_airspace_db(os.path.join(here, "cache"))
    print(f"Base des espaces permanents : {len(db)} identifiants")
    sc = SupCache(sups, os.path.join(here, "cache"), db)
    refs = sorted({r for n in notams.values()
                   for r in re.findall(r"SUP\s+(?:AIP\s+)?(?:AIRAC\s+)?(\d{3}/\d{2})", (n.get("itemE") or "").upper())
                   if r in sups})
    print(f"SUP AIP cites par les NOTAM : {len(refs)} - lecture des PDF ...")
    for r in refs:
        sc.zones(r)
    return {"sups": sups, "db": db, "sc": sc, "refs": refs}


CAT_SUP_VERIFIER = "SUP AIP - non interprete"
CAT_SUP_NON_ACTIVEE = "SUP AIP - activable, non activee"
CAT_SUP_INACTIVE = "SUP AIP - inactive (horaires)"
CAT_SUP_RADIO = "SUP AIP - activables"
CATEGORIES = ["active", CAT_SUP_RADIO, CAT_SUP_VERIFIER, CAT_SUP_NON_ACTIVEE, CAT_SUP_INACTIVE, "info"]
NOTAM_ACTIVATION_RE = re.compile(r"(ACTIVABLE|ACTIVATED|ACTIVATION|ANNOUNCED)[^.]{0,60}\bBY\s+(?:A\s+)?(?:[\w-]+\s+)?NOTAM|"
                                 r"BY\s+NOTAM\s+(?:WITH|AT)")

# NOTAM dont l'interdiction ne vise que les drones (« PROHIBITED FOR ALL RPAS », « OVERFLIGHT PROHIBITED FOR UAS AND
# MODEL ACFT », « ZONES INTERDITES TEMPORAIRES AUX AERONEFS SANS EQUIPAGE », « FOR UAS ONLY ») : classes en NOTAM
# d'information. Une zone que tout aeronef doit eviter (ZRT pour vols de drones, contournement obligatoire, zone
# reservee aux vols de drones « TEMPO RESTRICTED AREA ACT FOR UNMANNED ACFT ») reste active.
_DRONE = (r"(?:RPAS|UAS|UAV|DRONES?|UNMANNED(?:\s+AIRCRAFT|\s+ACFT)?|MODEL\s+(?:ACFT|AIRCRAFT|FLYING)|AEROMODEL\w*|"
          r"A[EÉ]RONEFS?\s+SANS\s+(?:[EÉ]QUIPAGE|PILOTE)|T[EÉ]L[EÉ]PILOT\w*)")
DRONES_SEULS_RE = re.compile(r"(?:PROHIBITED|NOT\s+(?:ALLOWED|PERMITTED)|FORBIDDEN|BANNED|INTERDITE?S?|INTERDICTION)\W+"
                             r"(?:\w+\W+){0,3}?(?:FOR|TO|AUX?|[AÀ]|POUR|PAR)\W+"
                             r"(?:(?:ALL|ANY|LES|TOUS|TOUTE?S?|DES|THE)\W+)?" + _DRONE +
                             r"|FOR\s+" + _DRONE + r"\s+ONLY")
POUR_TOUS_RE = re.compile(r"CONTOURNEMENT\s+OBLIGATOIRE|COMPULSORY\s+(?:AREA\s+)?AVOIDANCE|AREA\s+PROHIBITED\b|ZRT\b|"
                          r"TEMPO(?:RARY)?\s+RESTRICTED\s+AREA\s+ACT|ZONE\s+REGLEMENTEE|ALL\s+(?:ACFT|AIRCRAFT|FLIGHTS)|"
                          r"TOUS\s+(?:LES\s+)?A[EÉ]RONEFS")


# Activites sportives et recreatives et drones (AIP ENR 5.5) : parachutage (WP), voltige (WB), vol a voile,
# parapente et treuillage (WG), aeromodelisme et vols de drones (WU). L'outil n'affiche pas les activites permanentes
# de l'ENR 5.5 : il n'affiche pas non plus les NOTAM qui les creent, les modifient, les suspendent ou annoncent une
# activite ponctuelle du meme type. Le VFR n'y est pas interdit.
CODES_LOISIRS = ("WP", "WB", "WG", "WU")


# Garde-fou : un NOTAM d'activite qui cree ou active une zone interdite au VFR (ZRT, ZIT, zone reglementee ou
# interdite, contournement obligatoire...) n'est jamais ecarte. (Une zone permanente seulement citee, « EN DEHORS
# ACTIVITE LF R 589 », ne compte pas : elle a ses propres NOTAM ; une interdiction qui ne vise que les drones non plus.)
INTERDIT_VFR_RE = re.compile(r"\bZ[RI]T\b|\bZDT\b|RESTRICTED\s+AREA|PROHIBITED\s+AREA|AREA\s+PROHIBITED|"
                             r"ZONE\s+(?:REGLEMENTEE|INTERDITE)|CONTOURNEMENT\s+OBLIGATOIRE|COMPULSORY\s+(?:AREA\s+)?AVOIDANCE|"
                             r"(?:FLIGHTS?|OVERFLIGHT|PENETRATION|ACCESS)\s+(?:IS\s+)?(?:PROHIBITED|FORBIDDEN|NOT\s+(?:ALLOWED|PERMITTED))|"
                             r"\bINTERDI[TC]")


def loisir(n):
    if n["qLine"]["code23"] not in CODES_LOISIRS:
        return False
    e = unicodedata.normalize("NFKD", " ".join([n.get("itemE") or "", ((n.get("multiLanguage") or {}).get("itemE") or "")]))
    e = e.encode("ascii", "ignore").decode().upper()
    return not INTERDIT_VFR_RE.search(e) or drones_seulement(e)


ZONE_SUP_RE = re.compile(r"\bZ[RDI]T\b|\bTRA\b|\bTSA\b|R[ÉE]GLEMENT[ÉE]E|DANGEREUSE|INTERDITE", re.I)


def sups_non_lus(notams, sups, sc, w0, w1):
    """SUP en vigueur dans [w0, w1] d'apres le listing, qui creent des zones (titre), cites par aucun trigger NOTAM
    et dont le PDF ne donne aucune zone tracee : ils n'apparaitraient nulle part, la page les liste « a verifier ».
    Ecartes : itineraires, et SUP dont toutes les altitudes ecrites sont au-dessus du plafond (FL115)."""
    cites = set()
    for n in notams.values():
        e = (n.get("itemE") or "").upper()
        if n["qLine"]["code45"] == "TT" or "TRIGGER NOTAM" in e:
            cites.update(re.findall(r"SUP\s+(?:AIP\s+)?(?:AIRAC\s+)?(\d{3}/\d{2})", e))
    out = []
    for num, info in sorted(sups.items()):
        lim = bornes_listing(info)
        if num in cites or not lim or lim[1] <= w0 or lim[0] >= w1 or not ZONE_SUP_RE.search(info.get("titre", "")):
            continue
        if any(z["pts"] for z in sc.zones(num)) or sc.est_itineraire(num):
            continue
        t = unicodedata.normalize("NFKD", sc.texts.get(num, "")).encode("ascii", "ignore").decode().upper()
        bas = re.search(r"\b(?:SFC|GND|SOL)\b|\d\s*(?:FT|M)\s+(?:AMSL|ASFC|AGL|SOL)", t)
        fls = [int(x) for x in re.findall(r"\bFL\s*(\d{2,3})\b", t)]
        if t and not bas and fls and min(fls) >= PLAFOND_FL:
            continue
        out.append({"sup": num, "titre": info.get("titre", ""), "du": info["du"], "au": info.get("au", ""),
                    "pdf": info.get("pdf", "")})
    return out


def sups_sans_trigger(notams, sups, sc, w0, w1):
    """SUP en vigueur dans [w0, w1] d'apres le listing SIA mais cites par aucun trigger NOTAM valide dans le creneau
    (trigger expire, jamais publie...) : un pseudo trigger NOTAM par SUP, pour que ses zones soient traitees comme
    les autres. Seulement les SUP dont le PDF donne des zones tracees (pas les travaux d'aerodrome)."""
    if not sc:
        return []
    cites = set()
    for n in notams.values():
        e = (n.get("itemE") or "").upper()
        if not (n["qLine"]["code45"] == "TT" or "TRIGGER NOTAM" in e):
            continue
        cites.update(re.findall(r"SUP\s+(?:AIP\s+)?(?:AIRAC\s+)?(\d{3}/\d{2})", e))
    out = []
    for num, info in sorted(sups.items()):
        lim = bornes_listing(info)
        if num in cites or not lim or lim[1] <= w0 or lim[0] >= w1:
            continue
        if not any(z["pts"] for z in sc.zones(num)) or sc.est_itineraire(num):
            continue
        out.append({"id": f"SUP{num}", "_sans_notam": True, "series": "", "number": 0, "year": "",
                    "qLine": {"code23": "RT", "code45": "TT", "lower": 0, "upper": 999, "fir": "LFFF"},
                    "itemA": "LFFF", "coordinates": "", "radius": 0,
                    "startValidity": f"{info['du']}T00:00:00Z", "endValidity": "PERM",
                    "itemE": f"SUP AIP {num} en vigueur du {info['du']} au {info.get('au') or '?'} (listing SIA), "
                             f"sans trigger NOTAM : {info.get('titre', '')}",
                    "itemD": "", "itemF": "", "itemG": ""})
    return out


def drones_seulement(texte):
    e = re.sub(r"\s+", " ", (texte or "").upper())
    return bool(DRONES_SEULS_RE.search(e)) and not POUR_TOUS_RE.search(e)


def build_zones(notams, ctx, day, w0, w1, q_start, codes="R,W", plafond_fl=PLAFOND_FL, regles="V"):
    """Zones (une ligne par zone) actives le jour `day` dans le creneau [w0, w1], sans filtre geographique.
    Les creneaux termines avant q_start sont retires. SR/SS de l'item D : calcules au centre du NOTAM."""
    sups, db, sc, sup_refs_seen = ctx["sups"], ctx["db"], ctx["sc"], ctx["refs"]
    prefixes = tuple(x.strip().upper() for x in codes.split(",") if x.strip())
    rows = []
    for n in list(notams.values()) + sups_sans_trigger(notams, sups, sc, w0, w1):
        sans_notam = n.get("_sans_notam", False)
        q = n["qLine"]
        code = q["code23"] + q["code45"]
        if not q["code23"].startswith(prefixes):
            continue
        if loisir(n):                            # activites sportives et recreatives (ENR 5.5) : non affichees
            ctx.setdefault("loisirs", set()).add(n["id"])
            continue
        if q["lower"] >= plafond_fl:
            continue
        if int(n.get("radius") or 0) >= 999:     # rayon 999 NM : information generale (FIR entiere, securite...)
            continue
        b = dt.datetime.fromisoformat(n["startValidity"].replace("Z", "+00:00"))
        c_raw = n["endValidity"]
        c = dt.datetime.max.replace(tzinfo=UTC) if c_raw == "PERM" else dt.datetime.fromisoformat(c_raw.replace("Z", "+00:00"))
        e_up = (n.get("itemE") or "").upper()
        trig = q["code45"] == "TT" or "TRIGGER NOTAM" in e_up
        if trig:
            # Le trigger NOTAM ne fait qu'annoncer le SUP : sa validite est celle du listing SIA, qui fait foi
            lim = bornes_listing(sups.get((re.findall(r"SUP\s+(?:AIP\s+)?(?:AIRAC\s+)?(\d{3}/\d{2})", e_up) or [""])[0]))
            if lim:
                b, c = lim
                c_raw = "PERM" if c.year == dt.MAXYEAR else f"{c - dt.timedelta(minutes=1):%Y-%m-%dT%H:%M}"
        if c <= w0 or b >= w1:
            continue
        if trig:
            cat = CAT_SUP_VERIFIER
        elif q["code45"] in ("CA", "LW", "LP", "AW", "LT"):
            cat = "active"
        else:
            cat = "info"
        qc = re.fullmatch(r"(\d{4}[NS])(\d{5}[EW])", n.get("coordinates", ""))
        sr, ss = sun_times(day, parse_coord(qc[1], True), parse_coord(qc[2], False)) if qc else sun_times(day, 46.5, 2.5)
        intervals, ok = schedule_for_day(n.get("itemD"), day, sr, ss)
        sup_ref = re.findall(r"(?:AIP\s+)?SUP\s+(?:AIP\s+)?(\d{3}/\d{2})", e_up)
        sup_ref = sup_ref[0] if sup_ref else ""
        sup = sups.get(sup_ref, {})
        n_lo, n_hi = (n.get("itemF") or "").strip(), (n.get("itemG") or "").strip()
        q_lo, q_hi = vertical(n)
        parts = notam_parts(n, db, day, sc, sup_refs_seen)
        for k, part in enumerate(parts):
            if part.get("alt_sup") and floor_fl(part["al"]) >= plafond_fl:
                continue
            ivs = part["hours"] if part["hours"] is not None else intervals
            if ivs is None:
                act = [(max(b, w0), min(c, w1))]
            else:
                act = [(max(x, b, w0), min(y, c, w1)) for x, y in ivs]
                act = [(x, y) for x, y in act if y > x]
                if not act and (ok or part["hours"] is not None):
                    continue
                if not act:
                    act = [(max(b, w0), min(c, w1))]
            # Creneaux deja termines au moment de la requete : retires (comme la carte AZBA)
            act = [(x, y) for x, y in act if y > q_start and y > x]
            if not act:
                continue
            # Zones d'un SUP annonce par trigger NOTAM : classement d'apres la rubrique d'activite du SUP
            zcat, activation, motif = cat, ("NOTAM" if cat == "active" else ""), ""
            ref = (re.findall(r"SUP AIP (\d{3}/\d{2})", part["src"]) or [sup_ref])[0]
            if trig:
                # Rubrique d'activite du SUP, meme si la geometrie vient du NOTAM ou de la ligne Q (zone = toute la rubrique)
                sa = (sc.activite(ref, part["label"] if part["src"].startswith("SUP AIP") else "")
                      if (sc and ref and ref in sups) else None)
                mode = sa.mode if sa else "verifier"
                if mode == "itineraire":         # itineraire (ligne a suivre) : pas de zone a afficher
                    continue
                if mode == "verifier" and NOTAM_ACTIVATION_RE.search(e_up):
                    mode = "notam"
                pos = ((parse_coord(qc[1], True), parse_coord(qc[2], False)) if qc else
                       (sum(p[0] for p in part["pts"]) / len(part["pts"]), sum(p[1] for p in part["pts"]) / len(part["pts"]))
                       if part["pts"] else (46.5, 2.5))
                hs = [(max(x, b, w0), min(y, c, w1)) for x, y in sa.creneaux(day, pos)] if sa and sa.regles else []
                hs = [(x, y) for x, y in hs if y > x and y > q_start]
                if mode == "horaires":
                    if hs:
                        zcat, act, activation = "active", hs, "horaires SUP"
                    else:
                        zcat, motif = CAT_SUP_INACTIVE, "Hors des horaires d'activité du SUP"
                elif mode == "notam":
                    zcat, motif = CAT_SUP_NON_ACTIVEE, "Activable par NOTAM : aucun NOTAM d'activation publié"
                elif mode == "radio" and sa.regles and not hs:
                    zcat, motif = CAT_SUP_INACTIVE, "Hors des horaires d'activabilité du SUP"
                elif mode == "radio":
                    zcat, motif = CAT_SUP_RADIO, "Activable sans NOTAM : activité réelle connue par radio ou téléphone"
                    if hs:
                        act, motif = hs, motif + " (créneaux d'activabilité du SUP)"
                else:
                    zcat, motif = CAT_SUP_VERIFIER, "Mode d'activation non interprété : lire le SUP"
                if sa and sa.prolonge and sa.regles:
                    motif = (motif + " ; " if motif else "") + ("dates prolongées par le listing SIA : horaires du PDF "
                                                                "reconduits, à vérifier")
                if sans_notam:
                    motif = (motif + " ; " if motif else "") + "SUP en vigueur d'après le listing SIA (pas de trigger NOTAM)"
            # Informations pratiques du SUP (frequences, gestionnaire, telephones, penetration)
            inf = sc.infos(ref) if (sc and ref and ref in sups) else {}
            freqs = " / ".join(f"{a} {f}" for a, f in inf.get("freqs", []))
            if part.get("alt_sup"):
                lo_, hi_ = part["al"] or n_lo or q_lo, part["ah"] or n_hi or q_hi
            else:
                lo_, hi_ = n_lo or part["al"] or q_lo, n_hi or part["ah"] or q_hi
            # Nom : zone d'une base ou d'un SUP -> son nom publie ; trigger NOTAM -> titre du SUP ; NOTAM qui decrit
            # lui-meme sa zone -> son numero (le lieu, « 'COMMUNE DE MALAY-LE-GRAND' », passe en sous-titre)
            lieu = ""
            if part["label"]:
                name = part["label"]
            elif trig and sup.get("titre"):
                name = sup["titre"]
            else:
                name, lieu = f"{n['series']}{n['number']:04d}/{n['year']}", zone_name(n)
            if len(parts) > 1 and not part["label"]:
                name += f" ({k + 1})"
            if zcat != "info" and drones_seulement(e_up):
                zcat, act, motif = "info", act, "Interdiction visant seulement les drones (RPAS, UAS, aeromodeles)"
            # Lachers de ballons libres (code Q WL : radiosondages...) : sans interet pour le vol libre
            if zcat != "info" and code.startswith("WL"):
                zcat, motif = "info", "Lacher de ballons libres (radiosondage) : classe en information"
            rows.append({
                "categorie": zcat,
                "activation": activation,
                "motif": motif,
                "frequences": freqs,
                "gestionnaire": inf.get("gestionnaire", ""),
                "telephones": " / ".join(inf.get("tels", [])),
                "penetration": inf.get("penetration", ""),
                "pays": notam_country(n),
                "notam": "" if sans_notam else f"{n['series']}{n['number']:04d}/{n['year']}",
                "code": code,
                # NFKC : ligatures des PDF (« ﬁ » -> « fi ») et espaces speciaux, mal affiches par les instruments
                "nom": re.sub(r"\s+", " ", unicodedata.normalize("NFKC", name)).strip(),
                "plancher": re.sub(r"^FL\s*0*(\d+)", r"FL\1",
                                   re.sub(r"^0+(?=\d)", "", re.sub(r"(\d)\s+FT\b", r"\1FT", lo_.strip()))),
                "plafond": re.sub(r"^FL\s*0*(\d+)", r"FL\1",
                                  re.sub(r"^0+(?=\d)", "", re.sub(r"(\d)\s+FT\b", r"\1FT", hi_.strip()))),
                "validite": (f"{b:%Y-%m-%dT%H:%M} -> {c_raw[:16]}" + (" (listing SIA)" if trig and lim else "")
                             if trig else f"{n['startValidity'][:16]} -> {c_raw[:16]}"),
                "item_d": (n.get("itemD") or "").replace("\n", " "),
                "actif_ce_jour_utc": " ".join(f"{x:%H%M}-{y:%H%M}" for x, y in act),
                "horaire_interprete": "oui" if (ok or part["hours"] is not None) else "NON - verifier item D",
                "sup_aip": ref,
                "sup_titre": sups.get(ref, {}).get("titre", ""),
                "sup_du": sups.get(ref, {}).get("du", ""),
                "sup_au": sups.get(ref, {}).get("au", ""),
                # dates du listing corrigees par le SIA (en rouge) : "du", "au" ou "du,au"
                "sup_corrige": ",".join(sups.get(ref, {}).get("corrige", [])),
                "sup_pdf": sups.get(ref, {}).get("pdf", ""),
                "geometrie": part["src"],
                "dist_km": "",
                "relevement": "",
                "texte": part.get("texte") or (n.get("multiLanguage", {}) or {}).get("itemE") or n.get("itemE", ""),
                "_pts": part["pts"],
                "_titre": part.get("titre") or re.sub(r"\s+", " ", name).strip(),
                "_sous_titre": part.get("sous_titre") or re.sub(r"\s+", " ", unicodedata.normalize("NFKC", lieu)).strip(),
                "_slots": act,
                "_jour": day,
                "_class": openair_class(q["code23"]),
                "_id": f"{n['id']}#{k}",
                "_trig": trig,
                "_ref": ref,
            })
    return dedoublonner(rows)


def dedoublonner(rows):
    """Une zone de SUP activee par un NOTAM n'apparait qu'une fois : la copie issue du trigger NOTAM est retiree."""
    def cles(r):
        p = r["_pts"]
        return {("nom", r["_ref"], _fold(r["nom"])), ("geo", round(p[0][0], 3), round(p[0][1], 3), len(p))}
    actives = set()
    for r in rows:
        if r["categorie"] == "active" and not r["_trig"]:
            actives |= cles(r)
    return [r for r in rows if not (r["_trig"] and cles(r) & actives)]


def load_releve(ref):
    """Releve France : 'latest' (releves/latest.json a cote du script), chemin d'un fichier, ou URL."""
    if ref == "latest":
        ref = os.path.join(os.path.dirname(os.path.abspath(__file__)), "releves", "latest.json")
    if re.match(r"https?://", ref):
        r = requests.get(ref, timeout=60)
        r.raise_for_status()
        return r.json()
    with open(ref, encoding="utf-8") as f:
        return json.load(f)


def zones_from_releve(snap, day, w0, w1, now, plafond_fl=PLAFOND_FL):
    """Zones du releve actives dans [w0, w1], au meme format que build_zones (sans filtre geographique)."""
    rows = []
    for ft in snap["features"]:
        p = ft["properties"]
        if floor_fl(p["plancher"]) >= plafond_fl:
            continue
        slots = []
        for a_, b_ in p["creneaux"]:
            x = max(dt.datetime.strptime(a_, "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC), w0)
            y = min(dt.datetime.strptime(b_, "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC), w1)
            if y > x and y > now:
                slots.append((x, y))
        if not slots:
            continue
        ring = ft["geometry"]["coordinates"][0][:-1]
        r = {k: p.get(k, "") for k in ("categorie", "pays", "notam", "code", "nom", "plancher", "plafond", "validite",
                                       "item_d", "horaire_interprete", "sup_aip", "sup_titre", "sup_pdf",
                                       "sup_du", "sup_au", "sup_corrige",
                                       "geometrie", "activation", "motif", "frequences", "gestionnaire",
                                       "telephones", "penetration")}
        r.update({
            "actif_ce_jour_utc": " ".join(f"{x:%H%M}-{y:%H%M}" for x, y in slots),
            "dist_km": "", "relevement": "", "texte": p.get("texte", ""),
            "_pts": Ring([(la, lo) for lo, la in ring], p.get("openair")),
            "_titre": p.get("titre") or p["nom"], "_sous_titre": p.get("sous_titre", ""),
            "_slots": slots, "_jour": day, "_class": p.get("classe", "Q"), "_id": p.get("id", ""),
        })
        rows.append(r)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", required=True, help="Latitude du sommet (48.645, 483842N...)")
    ap.add_argument("--lon", required=True, help="Longitude du sommet (1.1, 0010559E...)")
    ap.add_argument("--axe", required=True, type=float, help="Axe du secteur, degres vrais")
    ap.add_argument("--date", required=True, help="Jour etudie AAAA-MM-JJ (UTC)")
    ap.add_argument("--ouverture", type=float, default=30.0, help="Ouverture totale en degres (30)")
    ap.add_argument("--rayon-km", type=float, default=400.0, help="Rayon du secteur en km (400)")
    ap.add_argument("--debut", help="Debut du creneau HHMM UTC (defaut : lever du soleil - 30 min)")
    ap.add_argument("--fin", help="Fin du creneau HHMM UTC (defaut : coucher du soleil + 30 min)")
    ap.add_argument("--regles", default="V", choices=["V", "I", "IV"], help="Trafic SOFIA (V)")
    ap.add_argument("--codes", default="R,W",
                    help="Prefixes de code Q retenus (defaut R,W : restrictions et avertissements)")
    ap.add_argument("--sortie", default="resultats", help="Dossier de sortie")
    ap.add_argument("--json-brut", action="store_true", help="Sauver aussi la reponse SOFIA brute")
    ap.add_argument("--releve", nargs="?", const="latest",
                    help="Calculer depuis un releve France (releve_france.py) au lieu d'interroger SOFIA : "
                         "'latest' (defaut si l'option est donnee seule), un fichier ou une URL")
    a = ap.parse_args()

    lat0, lon0 = parse_coord(a.lat, True), parse_coord(a.lon, False)
    day = dt.date.fromisoformat(a.date)
    half = a.ouverture / 2
    radius_m = a.rayon_km * 1000
    sr, ss = sun_times(day, lat0, lon0)
    base = dt.datetime(day.year, day.month, day.day, tzinfo=UTC)
    w0 = base + dt.timedelta(hours=int(a.debut[:2]), minutes=int(a.debut[2:])) if a.debut else sr - dt.timedelta(minutes=30)
    w1 = base + dt.timedelta(hours=int(a.fin[:2]), minutes=int(a.fin[2:])) if a.fin else ss + dt.timedelta(minutes=30)
    if not a.debut and not a.fin:
        pass
    elif w0 < sr - dt.timedelta(minutes=30) or w1 > ss + dt.timedelta(minutes=30):
        print("Attention : creneau en dehors de [SR-30, SS+30].")
    now = dt.datetime.now(UTC)
    if w1 <= now and not a.releve:
        sys.exit("Le creneau demande est deja passe : SOFIA ne donne que les NOTAM a venir.")
    q_start = max(w0, (now + dt.timedelta(minutes=3)).replace(second=0, microsecond=0))
    q_dur = int((w1 - q_start).total_seconds() // 60) + 1

    print(f"Sommet  : {to_openair(lat0, True)}  {to_openair(lon0, False)}")
    print(f"Secteur : axe {a.axe:.0f}deg +/-{half:.0f}deg, rayon {a.rayon_km:.0f} km")
    print(f"Jour    : {day}  SR {sr:%H%M}Z  SS {ss:%H%M}Z  -> creneau {w0:%H%M}-{w1:%H%M} UTC")
    if q_start > w0:
        print(f"          (interrogation SOFIA a partir de {q_start:%H%M}Z : heure courante)")
    if (day - now.date()).days > 2:
        print("          Attention : les NOTAM d'activation sont publies ~48 h a l'avance ; resultat incomplet.")

    frame = Frame(lat0, lon0)
    sect_ll = sector_ll(lat0, lon0, a.axe, half, radius_m)
    sect_xy = [frame.xy(*p) for p in sect_ll]

    if a.releve:
        snap = load_releve(a.releve)
        meta = snap["meta"]
        print(f"Releve  : {meta['genere']} (fenetre {meta['fenetre'][0]} -> {meta['fenetre'][1]}, "
              f"{meta['nb_zones']} zones France)")
        f0, f1 = (dt.datetime.strptime(x, "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC) for x in meta["fenetre"])
        if w1 <= f0 or w0 >= f1:
            sys.exit("Le creneau demande est hors de la fenetre du releve.")
        if w0 < f0 or w1 > f1:
            print("          Attention : creneau partiellement hors de la fenetre du releve.")
        source = f"releve France du {meta['genere']} (SOFIA-Briefing), planeur-net"
        candidats = zones_from_releve(snap, day, w0, w1, now, PLAFOND_FL)
    else:
        source = "SOFIA-Briefing (SIA), planeur-net"
        candidats = None
    centres = [] if a.releve else query_centres(frame, a.axe, half, radius_m, 100)
    notams = {}
    for i, (la, lo) in enumerate(centres, 1):
        print(f"SOFIA {i}/{len(centres)} : cylindre {to_sofia(la, lo)} R100NM ...", end=" ", flush=True)
        p = sofia_query(la, lo, 100, q_start, q_dur, 0, PLAFOND_FL, a.regles)
        if a.json_brut:
            os.makedirs(a.sortie, exist_ok=True)
            with open(os.path.join(a.sortie, f"sofia_brut_{day}_{i}.json"), "w", encoding="utf-8") as f:
                json.dump(p, f, ensure_ascii=False, indent=1)
        k = 0
        for n in iter_notams(p):
            if n["id"] not in notams:
                notams[n["id"]] = n
                k += 1
        print(f"{k} nouveaux NOTAM")
    if candidats is None:
        print(f"Total : {len(notams)} NOTAM distincts")
        candidats = build_zones(notams, prepare_context(notams), day, w0, w1, q_start, a.codes, PLAFOND_FL, a.regles)
    rows = []
    for r in candidats:
        xy = [frame.xy(la, lo) for la, lo in r["_pts"]]
        if not polys_intersect(xy, sect_xy):
            continue
        cx = sum(p[0] for p in xy) / len(xy)
        cy = sum(p[1] for p in xy) / len(xy)
        r["dist_km"] = round(math.hypot(cx, cy) / 1000)
        r["relevement"] = round((math.degrees(math.atan2(cx, cy)) + 360) % 360)
        rows.append(r)
    order = {c: i for i, c in enumerate(CATEGORIES)}
    rows.sort(key=lambda r: (order[r["categorie"]], r["dist_km"]))

    os.makedirs(a.sortie, exist_ok=True)
    tag = f"{day}"
    head = [f"* Genere par zones_secteur.py le {now:%Y-%m-%d %H%MZ} - sources {source}",
            f"* Sommet {to_openair(lat0, True)} {to_openair(lon0, False)} axe {a.axe:.0f} ouverture {a.ouverture:.0f} rayon {a.rayon_km:.0f} km",
            f"* Jour {day} creneau {w0:%H%M}-{w1:%H%M} UTC, plancher < FL{PLAFOND_FL}",
            "* OUTIL D'AIDE - verifier le briefing officiel avant le vol", ""]

    with open(os.path.join(a.sortie, f"secteur_{tag}.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(head))
        f.write(f"AC W\nAN SECTEUR RECHERCHE {a.axe:.0f}/{a.ouverture:.0f}/{a.rayon_km:.0f}KM\nAL SFC\nAH FL{PLAFOND_FL}\n")
        # Secteur natif : sommet puis arc DA (sens horaire) de axe-ouverture/2 a axe+ouverture/2
        f.write(f"DP {oa_pt(lat0, lon0)}\nV D=+\nV X={oa_pt(lat0, lon0)}\n"
                f"DA {radius_m / NM:.2f}, {(a.axe - half) % 360:.1f}, {(a.axe + half) % 360:.1f}\n"
                f"DP {oa_pt(lat0, lon0)}\n")

    def write_oa(fname, sel, title):
        with open(os.path.join(a.sortie, fname), "w", encoding="utf-8") as f:
            f.write("\n".join(head))
            f.write(f"* {title}\n")
            for r in sel:
                f.write(f"\n* {r['notam']} Q{r['code']} - {r['categorie']} - geometrie : {r['geometrie']}\n")
                if r["sup_aip"]:
                    f.write(f"* SUP AIP {r['sup_aip']} {r['sup_titre'][:90]}\n")
                    if r.get("sup_du"):
                        f.write(f"* SUP valide du {r['sup_du']} au {r['sup_au'] or '?'} (listing SIA"
                                + (", dates corrigees par le SIA" if r.get("sup_corrige") else "") + ")\n")
                    if r.get("sup_pdf"):
                        f.write(f"* SUP AIP {r['sup_aip']} (PDF officiel) : {r['sup_pdf']}\n")
                if r["item_d"]:
                    f.write(f"* Item D : {r['item_d'][:150]}\n")
                if r.get("motif"):
                    f.write(f"* {r['motif']}\n")
                if r.get("frequences"):
                    f.write(f"* Activite reelle connue de : {r['frequences']}\n")
                if r.get("telephones"):
                    f.write(f"* Telephone : {r['telephones']}\n")
                # OpenAir extended (Naviter, comme planeur-net) : AC = classe OACI (UNC : non classe), AY = type
                f.write(f"AC UNC\nAY {r['_class'] if r['_class'] in ('R', 'Q', 'P') else 'R'}\n")
                # numero du SUP dans le nom : seule information affichee par XCSoar / XCTrack pour retrouver le SUP
                # (le numero du NOTAM n'est pas repete quand il est deja le nom de la zone)
                ref = " ".join(x for x in ("" if r["nom"].startswith(r["notam"] or "-") else r["notam"],
                                           f"SUP {r['sup_aip']}" if r.get("sup_aip") else "") if x)
                f.write(f"AN {r['nom'][:45]} {r['actif_ce_jour_utc']}Z" + (f" ({ref})" if ref else "") + "\n")
                fq = re.match(r"(.+) (\d{3}\.\d{1,3})", (r.get("frequences") or "").split(" / ")[0])
                if fq:                                   # premiere frequence : champs OpenAir AF (frequence) et AG (indicatif)
                    f.write(f"AF {fq[2]}\nAG {fq[1]}\n")
                # Horaires d'activation (OpenAir extended, champ AA, UTC) : creneaux du jour, tries et fusionnes
                for x, y in merge_slots(r["_slots"]):
                    f.write(f"AA {x:%Y-%m-%dT%H:%M}Z/{y:%Y-%m-%dT%H:%M}Z\n")
                f.write(f"AL {r['plancher']}\nAH {r['plafond']}\n")
                # Geometrie d'origine (cercle DC, arcs DA/DB) quand elle est connue, sinon polygone DP
                oa = getattr(r["_pts"], "oa", None) or [f"DP {oa_pt(la, lo)}" for la, lo in r["_pts"]]
                f.write("\n".join(oa) + "\n")

    est_w = lambda r: r["code"].startswith(("W", "RD"))  # noqa  (avertissements et zones dangereuses : VFR non interdit)
    write_oa(f"zones_{tag}.txt", [r for r in rows if r["categorie"] == "active" and not est_w(r)],
             "Zones actives interdites au VFR (restrictions, hors zones dangereuses D)")
    write_oa(f"zones_w_{tag}.txt", [r for r in rows if est_w(r) and r["categorie"] != "info"],
             "Zones W et D (avertissements : voltige, parachutage... ; zones dangereuses) - VFR non interdit")
    write_oa(f"zones_sup_{tag}.txt", [r for r in rows if r["categorie"] in (CAT_SUP_RADIO, CAT_SUP_VERIFIER)],
             "Zones SUP AIP activables sans NOTAM (radio) et SUP non interpretes : verifier l'activite avant le vol")

    cols = [k for k in rows[0].keys() if not k.startswith("_")] if rows else []
    with open(os.path.join(a.sortie, f"zones_{tag}.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter=";", extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: (v.replace("\n", " ") if isinstance(v, str) else v) for k, v in r.items()})

    # Carte : meme interface que la page interactive, secteur pre-regle, zones du secteur integrees
    snap = {"type": "FeatureCollection",
            "meta": {"genere": (dt.datetime.strptime(meta["genere"], "%Y-%m-%dT%H:%MZ") if a.releve else now
                                ).strftime("%Y-%m-%dT%H:%MZ"),
                     "fenetre": [dt.datetime(day.year, day.month, day.day, tzinfo=UTC).strftime("%Y-%m-%dT%H:%MZ"),
                                 (dt.datetime(day.year, day.month, day.day, tzinfo=UTC) + dt.timedelta(days=1)
                                  ).strftime("%Y-%m-%dT%H:%MZ")],
                     "nb_zones": len(rows), "source": source,
                     # zone relevee (cylindres SOFIA) : la page signale la partie du secteur qui en sort
                     **({"cylindres": meta["cylindres"], "rayon_nm": meta.get("rayon_nm", 100)}
                        if a.releve and meta.get("cylindres") else {})},
            "features": [zone_feature(r) for r in rows]}
    preset = {"lat": round(lat0, 6), "lon": round(lon0, 6), "axe": a.axe, "ouv": a.ouverture, "ray": a.rayon_km,
              "jour": day.isoformat(), "deb": f"{w0:%H:%M}", "fin": f"{w1:%H:%M}", "source": source}
    with open(os.path.join(a.sortie, f"carte_{tag}.html"), "w", encoding="utf-8") as f:
        f.write(page_html(snap, preset, sites_ffvl(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache"))))

    for cat in order:
        sel = [r for r in rows if r["categorie"] == cat]
        print(f"\n== {cat} : {len(sel)}")
        for r in sel:
            flag = "" if r["horaire_interprete"] == "oui" else "  [horaire a verifier]"
            approx = "  [geom. approx.]" if "approx" in r["geometrie"] else ""
            print(f"  {r['notam']:<12} {r['dist_km']:>4} km/{r['relevement']:03d}  {r['plancher']:>13} - {r['plafond']:<13}"
                  f" {r['actif_ce_jour_utc']:<20} {r['nom'][:50]}{flag}{approx}")
    print(f"\nFichiers dans : {os.path.abspath(a.sortie)}")


def zone_feature(r):
    """Zone (ligne de build_zones / zones_from_releve) -> Feature GeoJSON lue par la page interactive."""
    pts = [[round(lo, 5), round(la, 5)] for la, lo in r["_pts"]]
    pts.append(pts[0])
    props = {k: v for k, v in r.items() if not k.startswith("_") and k not in ("dist_km", "relevement",
                                                                                "actif_ce_jour_utc")}
    props.update({
        "id": r["_id"],
        "titre": r["_titre"],
        "sous_titre": r["_sous_titre"],
        "classe": r["_class"],
        "plancher_ft": floor_ft(r["plancher"]),
        "creneaux": [[a.strftime("%Y-%m-%dT%H:%MZ"), b.strftime("%Y-%m-%dT%H:%MZ")] for a, b in r["_slots"]],
        "openair": getattr(r["_pts"], "oa", None) or [f"DP {oa_pt(la, lo)}" for la, lo in r["_pts"]],
    })
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [pts]}, "properties": props}


def merge_slots(slots):
    """Creneaux tries, ceux qui se chevauchent ou se touchent etant fusionnes."""
    out = []
    for a, b in sorted(slots):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


if __name__ == "__main__":
    main()
