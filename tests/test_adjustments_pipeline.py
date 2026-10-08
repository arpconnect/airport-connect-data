#!/usr/bin/env python3
"""
Tests unitaires du traitement des aménagements par le générateur (spec R-90 à R-93), sans GTFS.

    python3 tests/test_adjustments_pipeline.py

Ligne fictive L, deux sens. Ancien itinéraire A → OLD → C, nouvel itinéraire A → NEW → C ;
le GTFS contient encore des trajets par OLD (cas de la 351 en 2026).
"""

from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "pipeline"))
import build_timetable as bt  # noqa: E402

failures, passed = [], 0


def check(name, got, expected):
    global passed
    if got == expected:
        passed += 1
    else:
        failures.append(f"{name}\n    obtenu  : {got!r}\n    attendu : {expected!r}")


def stop(sid, parent, lat, lon):
    return {"stop_id": sid, "parent_station": parent, "stop_name": sid, "stop_lat": str(lat), "stop_lon": str(lon)}


STOPS = {s["stop_id"]: s for s in [
    stop("ZA", "", 48.85, 2.40), stop("A0", "ZA", 48.85, 2.40), stop("A1", "ZA", 48.85, 2.40),
    stop("ZOLD", "", 48.853, 2.41), stop("OLD0", "ZOLD", 48.853, 2.41), stop("OLD1", "ZOLD", 48.853, 2.41),
    stop("ZNEW", "", 48.851, 2.41), stop("NEW0", "ZNEW", 48.851, 2.41), stop("NEW1", "ZNEW", 48.851, 2.41),
    stop("ZC", "", 48.86, 2.42), stop("C0", "ZC", 48.86, 2.42), stop("C1", "ZC", 48.86, 2.42),
]}


def pattern(direction, stops):
    return ("L", direction, "x", tuple((s, 0, 0) for s in stops))


def run(adjustment, patterns_trips):
    """patterns_trips : liste de (mission, nombre de trajets)."""
    con = sqlite3.connect(":memory:")
    con.executescript(bt.SCHEMA_PATH.read_text(encoding="utf-8"))
    cur = con.cursor()
    con.execute("PRAGMA foreign_keys = OFF")
    patterns = [p for p, _ in patterns_trips]
    trip_rows = [(i, 1, ()) for i, (_p, n) in enumerate(patterns_trips, start=1) for _ in range(n)]
    used = {s for p in patterns for s, *_ in p[3]}
    point_id = {sp: i for i, sp in enumerate(sorted(used), start=1)}
    area_id = {a: i for i, a in enumerate(sorted(s for s in STOPS if not STOPS[s]["parent_station"]), start=1)}
    children = defaultdict(list)
    for sid, s in STOPS.items():
        if s["parent_station"]:
            children[s["parent_station"]].append(sid)
    report = bt.Report()
    n = bt.apply_adjustments(cur, [adjustment], {"L": 1}, area_id, point_id, STOPS, children, patterns, trip_rows, report)
    rows = {
        "written": n,
        "lagging": con.execute("SELECT gtfs_lagging_trips FROM adjustment").fetchall(),
        "stops": [r[0] for r in con.execute("SELECT stop_point_id FROM adjustment_stop ORDER BY 1")],
        "gaps": sorted({r[0] for r in con.execute("SELECT stop_point_id FROM adjustment_gap")}),
    }
    inv = {v: k for k, v in point_id.items()}
    rows["stops"] = [inv[i] for i in rows["stops"]]
    rows["gaps"] = [inv[i] for i in rows["gaps"]]
    return rows, [w["code"] for w in report.warnings], report


def adjustment(**over):
    a = {"id": "adj", "status": "published", "kind": "permanent", "line": "L", "title_fr": "t", "summary_fr": "s",
         "valid_from": None, "valid_until": None, "source_url": "https://exemple", "checked_on": "2026-10-08",
         "route_in_force": [{"stop_area": "ZA"}, {"stop_area": "ZNEW"}, {"stop_area": "ZC"}],
         "not_served": [
             {"stop_point": "OLD0", "gtfs_direction_id": 0, "direction_label": "C", "label": "Old",
              "replacement_stop_area": "ZNEW", "replacement_label": "New"},
             {"stop_point": "OLD1", "gtfs_direction_id": 1, "direction_label": "A", "label": "Old",
              "replacement_stop_area": "ZNEW", "replacement_label": "New"}]}
    a.update(over)
    return a


old0, new0 = pattern("0", ["A0", "OLD0", "C0"]), pattern("0", ["A0", "NEW0", "C0"])
old1, new1 = pattern("1", ["C1", "OLD1", "A1"]), pattern("1", ["C1", "NEW1", "A1"])

# 1. GTFS en retard dans les deux sens
rows, warns, rep = run(adjustment(), [(old0, 10), (new0, 4), (old1, 7), (new1, 3)])
check("retard : aménagement écrit", rows["written"], 1)
check("retard : 17 trajets en retard", rows["lagging"], [(17,)])
check("retard : deux quais non desservis", rows["stops"], ["OLD0", "OLD1"])
check("retard : lacunes aux deux quais du nouvel itinéraire", rows["gaps"], ["NEW0", "NEW1"])
check("retard : alerte d'écart avec le GTFS", warns, ["adjustment_overrides_gtfs"])
check("retard : distance calculée, arrondie à 10 m",
      bt._distance_m(STOPS["OLD0"], STOPS["ZNEW"]), 220)

# 2. GTFS aligné : plus aucun trajet par OLD, les quais OLD ont disparu des trajets
rows, warns, _ = run(adjustment(), [(new0, 4), (new1, 3)])
check("aligné : aménagement conservé (bandeau)", rows["written"], 1)
check("aligné : quais ignorés, aucun retard ni lacune", (rows["stops"], rows["lagging"], rows["gaps"]), ([], [(0,)], []))
check("aligné : alertes « quai inutilisé » puis « aménagement redondant »", warns,
      ["adjustment_stop_unused", "adjustment_stop_unused", "adjustment_redundant"])

# 3. Quai inconnu du GTFS et sens incohérent : alerte, jamais d'échec
bad = adjustment(not_served=[
    {"stop_point": "GHOST", "gtfs_direction_id": 0, "direction_label": "C", "label": "x"},
    {"stop_point": "OLD0", "gtfs_direction_id": 1, "direction_label": "C", "label": "Old",
     "replacement_stop_area": "ZNEW", "replacement_label": "New"}])
rows, warns, rep = run(bad, [(old0, 2), (new0, 2)])
check("incohérences : aucune erreur bloquante", rep.errors, [])
check("incohérences : alertes", warns, ["adjustment_stop_missing", "adjustment_direction_mismatch", "adjustment_overrides_gtfs"])
check("incohérences : le quai connu reste traité", rows["stops"], ["OLD0"])

# 4. Ligne inconnue : aménagement ignoré avec alerte
rows, warns, rep = run(adjustment(line="X"), [(old0, 1)])
check("ligne inconnue : rien d'écrit", rows["written"], 0)
check("ligne inconnue : alerte", warns, ["adjustment_line_unknown"])

# 5. Un autre quai de la même zone suffit à couvrir la zone (pas de fausse lacune)
same_zone = pattern("1", ["C1", "OLD1", "A0"])   # dessert A0, pas A1 : même zone ZA
rows, warns, _ = run(adjustment(), [(same_zone, 5), (pattern("1", ["C1", "NEW1", "A1"]), 2)])
check("même zone : A1 n'est pas une lacune, NEW1 en est une", rows["gaps"], ["NEW1"])

# 6. Mission en retard partielle : elle ne parcourt pas tout le tronçon, aucune fausse lacune à ses extrémités
#    (itinéraire en vigueur ZA → ZNEW → ZC ; mission OLD0 → C0 sans passer par ZA)
partial = pattern("0", ["OLD0", "C0"])
rows, warns, _ = run(adjustment(), [(partial, 3), (pattern("0", ["A0", "NEW0", "C0"]), 2)])
check("mission partielle : A0 et NEW0 ne sont pas des lacunes", rows["gaps"], [])

for f in failures:
    print("ÉCHEC", f)
print(f"{passed} contrôles conformes, {len(failures)} échec(s)")
sys.exit(1 if failures else 0)
