#!/usr/bin/env python3
"""Diagnostic des référentiels d'IDFM pour une ligne pas encore suivie (nouvelle ligne à ajouter à data/lines.json) :
référentiel des lignes (code C, nom, mode, statut, exploitant, couleurs) et jeu des plans et fiches horaires (documents
publiés pour ce code). Lancé par le traitement « diagnostic » du dépôt public, d'où les portails sont joignables.

    python3 tools/pipeline/inspect_idfm.py --line 18 --mode metro
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_plans import Http, sniff  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
LINES = ("https://data.iledefrance-mobilites.fr", "referentiel-des-lignes")


def export(http: Http, portal: str, dataset: str, where: str) -> list:
    q = urllib.parse.urlencode({"where": where})
    r = http.get(f"{portal}/api/explore/v2.1/catalog/datasets/{dataset}/exports/json?{q}")
    if r.status != 200:
        raise IOError(f"{dataset} : HTTP {r.status}")
    return json.loads(r.body.decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--line", required=True, help="numéro ou nom court de la ligne (shortname_line)")
    ap.add_argument("--mode", help="transportmode du référentiel (metro, tram, rail, bus, funicular)")
    ap.add_argument("--id", action="append", default=[], help="code C à interroger directement (répétable)")
    args = ap.parse_args()
    http = Http()
    where = f'shortname_line="{args.line}"' + (f' and transportmode="{args.mode}"' if args.mode else "")
    lines = export(http, *LINES, where)
    keys = ("id_line", "name_line", "shortname_line", "transportmode", "transportsubmode", "operatorname",
            "networkname", "status", "colourweb_hexa", "textcolourweb_hexa", "accessibility", "audiblesigns_available")
    print(f"Référentiel des lignes ({where}) : {len(lines)} ligne(s)")
    for rec in lines:
        print("  " + " ; ".join(f"{k}={rec.get(k)!r}" for k in keys if k in rec))
    ids = sorted({rec["id_line"] for rec in lines} | set(args.id))
    sources = json.loads((ROOT / "data" / "plans-sources.json").read_text(encoding="utf-8"))
    ds = sources["datasets"]["line_documents"]
    for cid in ids:
        docs = export(http, ds["portal"], ds["dataset_id"], f'id_line="{cid}"')
        print(f"Plans et fiches de {cid} : {len(docs)} document(s)")
        for d in docs:
            url = d.get("url") or ""
            kind = "?"
            if url.startswith("https://"):
                try:
                    r = http.get(url)
                    kind = f"HTTP {r.status}, {sniff(r.body) or 'pas un document'}, {len(r.body)} o"
                except IOError as e:
                    kind = f"injoignable ({e})"
            print(f"  {d.get('type')!r} {d.get('document_name')!r} {url} → {kind}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
