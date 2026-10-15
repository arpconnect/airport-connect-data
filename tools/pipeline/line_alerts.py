#!/usr/bin/env python3
"""
Alertes de lignes (spec R-14, décision D-5) : chaque renumérotation ou suppression de ligne qu'IDFM annonce ou applique
ouvre une issue dans le dépôt de publication, une seule par ligne et par motif tant qu'elle reste ouverte. GitHub en
avertit les personnes qui suivent le dépôt (courriel, application).

    python3 tools/pipeline/line_alerts.py --report dist/report.json [--report dist-plans/report.json] \\
        [--repo owner/name] [--dry-run]

Motifs lus dans les rapports des traitements quotidiens :

- base d'horaires (`build_timetable.py`) :
  - `route_missing` (erreur) : le code C de la ligne n'existe plus dans le GTFS ;
  - `line_without_trips` (erreur) : la ligne n'a plus aucun trajet ;
  - `route_renamed` : le numéro de la ligne a changé dans le GTFS ;
  - `route_renumbering_announced` : IDFM annonce le futur numéro dans le nom long (« EX 100 (future 700) ») ;
  - `line_service_ends` : la ligne s'arrête 7 jours ou plus avant les autres lignes de son transporteur ;
- catalogue des plans (`fetch_plans.py`) : `line_name_differs`, le référentiel IDFM nomme la ligne autrement.

Sans `--repo` ou avec `--dry-run`, les alertes sont seulement affichées. Le jeton vient de `GITHUB_TOKEN`
(droit `issues: write`). Bibliothèque standard uniquement.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

LABEL = "alerte-ligne"
API = "https://api.github.com"
KEY_PREFIX = "<!-- airport-connect:alerte "


def _line(detail) -> str | None:
    if isinstance(detail, dict):
        return detail.get("line")
    return detail if isinstance(detail, str) else None


def alerts(reports: list) -> list:
    """Alertes à signaler, dans l'ordre (ligne, motif) : clé stable, titre et corps de l'issue."""
    out = {}

    def add(code: str, line: str, title: str, body: str) -> None:
        key = f"{code}:{line}"
        out.setdefault(key, {"key": key, "line": line, "code": code, "title": title, "body": body})

    for rep in reports:
        for e in rep.get("errors", []):
            line, d = _line(e.get("detail")), e.get("detail")
            if not line:
                continue
            if e["code"] == "route_missing":
                add("route_missing", line, f"Ligne {line} : code C {d.get('route_id')} absent du GTFS d'IDFM",
                    "Le GTFS du jour ne contient plus le code C de cette ligne : suppression, ou nouveau code après une "
                    "refonte. La publication de la base est bloquée jusqu'à la mise à jour de `data/lines.json` "
                    "(nouveau code C, ou retrait de la ligne).")
            elif e["code"] == "line_without_trips":
                add("line_without_trips", line, f"Ligne {line} : plus aucun trajet dans le GTFS d'IDFM",
                    "Le code C existe encore, mais aucun trajet n'est publié : ligne suspendue ou supprimée. La "
                    "publication de la base est bloquée jusqu'à la mise à jour de `data/lines.json`.")
        for w in rep.get("warnings", []):
            d = w.get("detail")
            line = _line(d)
            if not line or not isinstance(d, dict):
                continue
            if w["code"] == "route_renamed":
                add("route_renamed", line, f"Ligne {line} : renumérotée « {d.get('before')} » → « {d.get('now')} »",
                    "Le numéro de la ligne a changé dans le GTFS du jour. L'application affiche déjà le nouveau numéro "
                    "(R-14) ; aligner `data/lines.json` (nom, couleurs, plans) et vérifier ses points de montée.")
            elif w["code"] == "route_renumbering_announced":
                add("route_renumbering_announced", line,
                    f"Ligne {line} : renumérotation annoncée « {d.get('now')} » → « {d.get('future')} »",
                    f"IDFM annonce dans le GTFS le futur numéro de la ligne (« {d.get('future')} »). Préparer "
                    "`data/lines.json` ; l'alerte « renumérotée » suivra le jour du changement.")
            elif w["code"] == "line_service_ends":
                add("line_service_ends", line, f"Ligne {line} : fin de service annoncée le {d.get('last_service_date')}",
                    f"Le dernier trajet publié de la ligne est le {d.get('last_service_date')}, alors que son transporteur "
                    f"({d.get('operator')}) publie des trajets jusqu'au {d.get('operator_last_date')} : suppression ou "
                    "remplacement probable. Vérifier l'annonce d'IDFM avant de retirer ou de remplacer la ligne.")
            elif w["code"] == "line_name_differs":
                add("line_name_differs", line,
                    f"Ligne {line} : le référentiel IDFM la nomme « {d.get('idfm_referential')} »",
                    f"Le référentiel des plans d'IDFM nomme la ligne « {d.get('idfm_referential')} » et "
                    f"`data/lines.json` « {d.get('lines_json')} » : renumérotation probable, à confirmer sur le GTFS.")
    return [out[k] for k in sorted(out, key=lambda k: (out[k]["line"], out[k]["code"]))]


class GitHub:
    """Appels REST minimaux : issues ouvertes de l'étiquette, création d'une issue."""

    def __init__(self, repo: str, token: str, opener=urllib.request.urlopen):
        self.repo, self.token, self.opener = repo, token, opener

    def _call(self, method: str, path: str, payload=None):
        req = urllib.request.Request(API + path, method=method,
                                     data=None if payload is None else json.dumps(payload).encode("utf-8"))
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if payload is not None:
            req.add_header("Content-Type", "application/json")
        with self.opener(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8") or "null")

    def open_keys(self) -> set:
        keys, page = set(), 1
        while True:
            items = self._call("GET", f"/repos/{self.repo}/issues?state=open&labels={LABEL}&per_page=100&page={page}")
            for it in items:
                body = it.get("body") or ""
                if KEY_PREFIX in body:
                    keys.add(body.split(KEY_PREFIX, 1)[1].split(" -->", 1)[0].strip())
            if len(items) < 100:
                return keys
            page += 1

    def create(self, a: dict) -> int:
        body = f"{a['body']}\n\n{KEY_PREFIX}{a['key']} -->"
        return self._call("POST", f"/repos/{self.repo}/issues", {"title": a["title"], "body": body, "labels": [LABEL]})["number"]


def publish(found: list, gh: GitHub) -> list:
    """Ouvre une issue par alerte sans issue ouverte de même clé ; rend les alertes ouvertes."""
    known = gh.open_keys()
    opened = []
    for a in found:
        if a["key"] in known:
            continue
        a = {**a, "number": gh.create(a)}
        opened.append(a)
    return opened


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--report", action="append", type=Path, default=[])
    ap.add_argument("--repo")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    reports = [json.loads(p.read_text(encoding="utf-8")) for p in args.report if p.exists()]
    found = alerts(reports)
    for a in found:
        print(f"alerte {a['key']} : {a['title']}")
    if not found or args.dry_run or not args.repo:
        print(f"{len(found)} alerte(s) de ligne{'' if args.repo and not args.dry_run else ' (affichage seulement)'}")
        return 0
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN absent : alertes non publiées", file=sys.stderr)
        return 1
    try:
        opened = publish(found, GitHub(args.repo, token))
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"Alertes non publiées : {e}", file=sys.stderr)
        return 1
    for a in opened:
        print(f"issue #{a['number']} ouverte : {a['title']}")
    print(f"{len(found)} alerte(s) de ligne, {len(opened)} nouvelle(s) issue(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
