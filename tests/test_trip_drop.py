#!/usr/bin/env python3
"""
Teste le garde-fou de publication de la base d'horaires (build_timetable.compare_previous), sans réseau, sur les
nombres réels du blocage du 3 au 10 novembre 2026 : le nombre de trajets des 133, 143 et 249 avait chuté de plus de
moitié (une période d'horaires sortie de la fenêtre) à service constant, 305, 243 et 239 trajets le jour le plus chargé.

    python3 tests/test_trip_drop.py
"""

from __future__ import annotations

import sys
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


def run(prev_counts, counts, active):
    report = bt.Report()
    bt.compare_previous({"counts": prev_counts}, counts, report, active)
    return [(e["code"], e["detail"]["line"]) for e in report.errors], [(w["code"], w["detail"]["line"]) for w in report.warnings]


ACTIVE = {"BUS_133", "BUS_143", "BUS_249"}
now = {"trips_per_line": {"BUS_133": 641, "BUS_143": 550, "BUS_249": 757},
       "peak_day_trips_per_line": {"BUS_133": 305, "BUS_143": 243, "BUS_249": 239}}

# Manifeste du 2 novembre 2026, antérieur à la mesure : pas de comparaison, donc pas de blocage.
old = {"trips_per_line": {"BUS_133": 1552, "BUS_143": 1332, "BUS_249": 1626, "BUS_A01": 120}}
check("ancien manifeste : aucun blocage, ligne retirée signalée", run(old, now, ACTIVE), ([], [("line_retired", "BUS_A01")]))

# Même service le jour le plus chargé, moitié moins de trajets dans la base : pas de blocage.
prev = {"trips_per_line": {"BUS_133": 1552, "BUS_143": 1332, "BUS_249": 1626},
        "peak_day_trips_per_line": {"BUS_133": 305, "BUS_143": 243, "BUS_249": 239}}
check("période sortie de la fenêtre, service constant : aucun blocage", run(prev, now, ACTIVE), ([], []))

# Vraie chute : le jour le plus chargé du 133 perd plus de la moitié de ses trajets, ou la ligne disparaît.
cut = {"trips_per_line": {"BUS_133": 300, "BUS_249": 757}, "peak_day_trips_per_line": {"BUS_133": 150, "BUS_249": 239}}
check("service coupé de plus de moitié, ligne disparue : blocage", run(prev, cut, ACTIVE),
      ([("line_trip_drop", "BUS_133"), ("line_trip_drop", "BUS_143")], []))
half = {"trips_per_line": {}, "peak_day_trips_per_line": {"BUS_133": 153, "BUS_143": 243, "BUS_249": 239}}
check("perte de la moitié au plus (dimanche seul dans la fenêtre) : aucun blocage", run(prev, half, ACTIVE), ([], []))
# Ligne retirée du référentiel : jamais bloquante.
check("ligne retirée du référentiel", run(prev, cut, {"BUS_249"})[0], [])

for f in failures:
    print("ÉCHEC", f)
print(f"{passed} contrôles conformes, {len(failures)} échec(s)")
sys.exit(1 if failures else 0)
