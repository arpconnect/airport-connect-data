#!/usr/bin/env python3
"""
Collecteur de l'historique des incidents de trafic (spec R-86, décision D-17, ADR-14).

    python3 tools/pipeline/traffic_history.py --state history.json --bulk bulk.json --observed 2026-10-16T08:02:11Z \\
        --axes axes.json [--timetable timetable.sqlite.gz --base-version V] [--data data] [--out history.json]

- `--bulk` : réponse de l'info trafic globale lue à travers le proxy (`/disruptions_bulk/disruptions/v2`) ;
- `--observed` : heure de sa lecture chez IDFM (`x-airportconnect-fetched-at` du proxy, sinon l'en-tête `Date`) ;
- `--axes` : missions des RER à axes (filtre d'axe R-82), tirées de la base d'horaires publiée. Avec `--timetable`
  (et `--base-version`), elles sont relues dans cette base et le fichier est réécrit ; sinon, le fichier est lu :
  la base (5 Mo) n'est téléchargée que lorsqu'une nouvelle version est publiée ;
- `--state` : état précédent (absent au premier passage : le suivi commence) ; `--out` : nouvel état (par défaut,
  le même fichier).

Les règles sont celles de `tools/reference/v2_traffic_history.py`. Bibliothèque standard uniquement.
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "reference"))
import v2_traffic as tr  # noqa: E402
import v2_traffic_history as th  # noqa: E402


def patterns_of(con, code: str) -> list:
    """Missions de la ligne dans l'ordre de la base, chacune la liste ordonnée de ses quais `(zone d'arrêt, quai)` ;
    une suite de quais identique n'est gardée qu'une fois (à la première) : R-82 n'en dépend pas."""
    out, seen = [], set()
    for (pid,) in con.execute("SELECT p.id FROM pattern p JOIN line l ON l.id = p.line_id WHERE l.code = ? ORDER BY p.id",
                              (code,)):
        stops = tuple((a, sp) for a, sp in con.execute(
            """SELECT a.gtfs_id, sp.gtfs_id FROM pattern_stop ps JOIN stop_point sp ON sp.id = ps.stop_point_id
               JOIN stop_area a ON a.id = sp.stop_area_id WHERE ps.pattern_id = ? ORDER BY ps.seq""", (pid,)))
        if stops not in seen:
            seen.add(stops)
            out.append([list(x) for x in stops])
    return out


def axes_patterns(timetable: Path) -> dict:
    """Missions des RER à axes (R-82), lues dans une base d'horaires (gzip ou non), au format JSON."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.sqlite"
        if timetable.suffix == ".gz":
            with gzip.open(timetable, "rb") as fi, open(db, "wb") as fo:
                shutil.copyfileobj(fi, fo)
        else:
            shutil.copyfile(timetable, db)
        con = sqlite3.connect(db)
        try:
            pats = {code: patterns_of(con, code) for code in sorted(tr.AXES)}
        finally:
            con.close()
    for code, p in pats.items():
        if not tr.covered_areas(code, [[tuple(x) for x in m] for m in p]):
            raise SystemExit(f"Base sans mission couvrant l'axe de {code} : filtre d'axe impossible (R-82)")
    return pats


def tracked_lines(data_dir: Path, patterns: dict) -> list:
    """Lignes suivies `(code, route_id, missions)` : les lignes actives de `lines.json`, avec les missions des RER à
    axes (`axes_patterns`) ; les autres lignes n'en ont pas besoin."""
    lines = json.loads((data_dir / "lines.json").read_text(encoding="utf-8"))["lines"]
    active = [(l["id"], l["idfm_route_id"]) for l in lines if l.get("state") == "active" and l.get("idfm_route_id")]
    missing = sorted(set(tr.AXES) - set(patterns))
    if missing:
        raise SystemExit(f"Missions absentes pour {', '.join(missing)} (filtre d'axe R-82)")
    return [(code, route, [[tuple(x) for x in m] for m in patterns.get(code, [])]) for code, route in active]


def parse_instant(value: str) -> int:
    value = value.strip()
    if value.isdigit():
        return int(value)
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--state", type=Path, required=True)
    ap.add_argument("--bulk", type=Path, required=True)
    ap.add_argument("--observed", required=True)
    ap.add_argument("--axes", type=Path, required=True)
    ap.add_argument("--timetable", type=Path)
    ap.add_argument("--base-version")
    ap.add_argument("--data", type=Path, default=ROOT / "data")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    try:
        bulk = json.loads(args.bulk.read_text(encoding="utf-8"))
        if not isinstance(bulk, dict) or not isinstance(bulk.get("lines"), list) or not isinstance(bulk.get("disruptions"), list):
            raise ValueError("structure inattendue")
    except (OSError, ValueError) as e:
        print(f"Réponse de l'info trafic illisible : {e}", file=sys.stderr)
        return 1
    state = json.loads(args.state.read_text(encoding="utf-8")) if args.state.exists() else th.new_state()
    if state.get("schema") != th.SCHEMA:
        print(f"État d'un autre format ({state.get('schema')}) : refusé", file=sys.stderr)
        return 1
    if args.timetable:
        axes = {"base_version": args.base_version, "patterns": axes_patterns(args.timetable)}
        args.axes.write_text(json.dumps(axes, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    else:
        axes = json.loads(args.axes.read_text(encoding="utf-8"))
    observed = parse_instant(args.observed)
    before = len(state["observations"])
    th.observe(state, bulk, observed, tracked_lines(args.data, axes["patterns"]))
    out = args.out or args.state
    tmp = out.with_suffix(".part")
    tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    tmp.replace(out)
    day = th.window(state, observed, th.DAY_S)
    n_day = sum(len(v) for v in day["lines"].values())
    print(f"Lecture du {datetime.fromtimestamp(observed).isoformat()} {'enregistrée' if len(state['observations']) > before else 'déjà connue'} ; "
          f"{len(state['disruptions'])} perturbation(s) suivie(s), {n_day} incident(s) sur 24 h "
          f"({'suivi complet' if day['coverage']['complete'] else 'suivi partiel'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
