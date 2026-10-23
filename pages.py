#!/usr/bin/env python3
"""
Generation des pages publiees : carte interactive (carte_template.html), page autonome avec le releve integre,
mode d'emploi (MODE_EMPLOI.md mis en forme) et liste des sites.

Module separe du calcul des zones : une modification ici (ou de carte_template.html, MODE_EMPLOI.md) republie les
pages sans nouveau releve SOFIA (voir .github/workflows/releve.yml).
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))


def page_html(snap, preset=None, sites=None):
    """Page interactive (carte_template.html) avec le releve integre et, en option, un secteur pre-regle et la
    liste des sites FFVL (page autonome)."""
    with open(os.path.join(HERE, "carte_template.html"), encoding="utf-8") as f:
        tpl = f.read()
    js = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")  # noqa
    page = tpl.replace("/*__RELEVE__*/null", js(snap))
    if preset:
        page = page.replace("/*__PRESET__*/null", js(preset))
    if sites:
        page = page.replace("/*__SITES__*/null", js(sites))
    return page


AIDE_CSS = """
:root{--paper:#f4f5f2;--panel:#fff;--ink:#14233a;--muted:#5b6778;--line:#d9dde3;--navy:#0b4f8a;--magenta:#b0186a;
  --warn:#a85a00;--f-ui:"IBM Plex Sans Condensed","Arial Narrow",Arial,sans-serif;
  --f-data:"IBM Plex Mono",Consolas,"Courier New",monospace;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--paper:#0f1520;--panel:#161e2b;--ink:#e3e8ef;
  --muted:#9aa6b6;--line:#2a3546;--navy:#6fb0ee;--magenta:#ee5aa8;--warn:#f0a83a;color-scheme:dark}}
:root[data-theme="dark"]{--paper:#0f1520;--panel:#161e2b;--ink:#e3e8ef;--muted:#9aa6b6;--line:#2a3546;
  --navy:#6fb0ee;--magenta:#ee5aa8;--warn:#f0a83a;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 var(--f-ui);
  padding:24px max(16px,env(safe-area-inset-right,0px)) 48px max(16px,env(safe-area-inset-left,0px))}
main{max-width:760px;margin:0 auto;background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:8px 28px 28px}
.retour{display:inline-block;margin:0 auto 14px;max-width:760px;width:100%;font-weight:600;color:var(--navy);text-decoration:none}
.retour:hover{text-decoration:underline}
h1{font-size:28px;line-height:1.2;margin:18px 0 8px;text-wrap:balance}
h2{font-size:20px;margin:30px 0 8px;padding-top:12px;border-top:1px solid var(--line);color:var(--navy)}
p,li{max-width:68ch} ul{padding-left:22px} li{margin:3px 0}
a{color:var(--navy)} strong{font-weight:600}
code{font-family:var(--f-data);font-size:.9em;background:var(--paper);border:1px solid var(--line);border-radius:3px;padding:0 4px}
blockquote{margin:16px 0;padding:10px 14px;border-left:4px solid var(--warn);background:var(--paper);color:var(--ink)}
blockquote p{margin:4px 0}
table{border-collapse:collapse;font-size:15px} td,th{border:1px solid var(--line);padding:4px 8px}
@media (max-width:600px){main{padding:4px 16px 20px} h1{font-size:24px}}
"""


def mode_emploi_html():
    """Page HTML du mode d'emploi (MODE_EMPLOI.md mis en forme, meme apparence que la carte), ou None."""
    path = os.path.join(HERE, "MODE_EMPLOI.md")
    if not os.path.exists(path):
        return None
    import markdown
    with open(path, encoding="utf-8") as f:
        texte = f.read()
    # adresses web nues -> liens
    texte = re.sub(r"(?<![(<\"])(https?://[^\s)>*]+)", r"<\1>", texte)
    # listes imbriquees ecrites avec 2 espaces : le convertisseur en attend 4
    texte = re.sub(r"(?m)^( +)", lambda m: m.group(1) * 2, texte)
    corps = markdown.markdown(texte, extensions=["tables", "sane_lists"])
    return ("<!doctype html>\n<html lang=\"fr\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
            "<title>Secteur VFR - mode d'emploi</title>\n"
            "<link rel=\"stylesheet\" href=\"https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&"
            "family=IBM+Plex+Sans+Condensed:wght@400;500;600;700&display=swap\">\n"
            f"<style>{AIDE_CSS}</style>\n</head>\n<body>\n"
            "<div style=\"max-width:760px;margin:0 auto\"><a class=\"retour\" href=\"./\">&larr; Retour à la carte</a></div>\n"
            f"<main>\n{corps}\n</main>\n</body>\n</html>\n")


def ecrire_pages(snap, dossier, sites=None):
    """Page interactive : carte.html (releve et sites integres, autonome, a partager), index.html (lit latest.json
    et sites.json, pour l'hebergement en ligne), mode_emploi.html et sites.json (sites FFVL `sites`)."""
    with open(os.path.join(HERE, "carte_template.html"), encoding="utf-8") as f:
        tpl = f.read()
    pages = [("carte.html", page_html(snap, sites=sites)), ("index.html", tpl)]
    aide = mode_emploi_html()
    if aide:
        pages.append(("mode_emploi.html", aide))
    if sites:
        pages.append(("sites.json", json.dumps(sites, ensure_ascii=False, separators=(",", ":"))))
    for name, content in pages:
        tmp = os.path.join(dossier, name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, os.path.join(dossier, name))
