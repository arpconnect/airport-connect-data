#!/usr/bin/env python3
"""
Teste le générateur sur les lieux (schéma 2) avec un GTFS fictif minimal, sans réseau :

    python3 tests/test_places_pipeline.py

- lieu aéroportuaire et lieu « ville » (Paris) écrits avec leur sorte, leur secteur et l'ordre du sélecteur ;
- points de montée de Paris (spec R-100, décision D-13) : écrits, puis contrôlés sur le GTFS ;
- erreurs bloquantes : sorte inconnue, ville avec secteur, ligne ou zone d'arrêt inconnue, doublon ;
- alertes : point de montée que la ligne ne dessert plus vers un aéroport, nom différent du GTFS ;
- départs du lieu par le lecteur de référence, y compris une ligne qui ne mène plus à l'aéroport.
"""

from __future__ import annotations

import copy
import csv
import io
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "pipeline"))
import build_timetable as bt  # noqa: E402
from timetable_reader import Timetable  # noqa: E402

PARIS = ZoneInfo("Europe/Paris")
failures, passed = [], 0


def check(name, got, expected):
    global passed
    if got == expected:
        passed += 1
    else:
        failures.append(f"{name}\n    obtenu  : {got!r}\n    attendu : {expected!r}")


def _csv(rows: list) -> str:
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=list(rows[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return out.getvalue()


def make_gtfs(path: Path) -> None:
    """Ligne X (ville → aéroport, et retour) et ligne Y (ville → banlieue, sans aéroport), un jour de service."""
    stops = [
        {"stop_id": "ZC", "stop_name": "Gare Centrale", "stop_lat": "48.88", "stop_lon": "2.35", "location_type": "1", "parent_station": ""},
        {"stop_id": "C1", "stop_name": "Gare Centrale", "stop_lat": "48.88", "stop_lon": "2.35", "location_type": "0", "parent_station": "ZC"},
        {"stop_id": "ZM", "stop_name": "Milieu", "stop_lat": "48.90", "stop_lon": "2.40", "location_type": "1", "parent_station": ""},
        {"stop_id": "M1", "stop_name": "Milieu", "stop_lat": "48.90", "stop_lon": "2.40", "location_type": "0", "parent_station": "ZM"},
        {"stop_id": "ZA", "stop_name": "Aéroport Terminal", "stop_lat": "49.00", "stop_lon": "2.55", "location_type": "1", "parent_station": ""},
        {"stop_id": "A1", "stop_name": "Aéroport Terminal", "stop_lat": "49.00", "stop_lon": "2.55", "location_type": "0", "parent_station": "ZA"},
        {"stop_id": "ZB", "stop_name": "Banlieue", "stop_lat": "48.80", "stop_lon": "2.30", "location_type": "1", "parent_station": ""},
        {"stop_id": "B1", "stop_name": "Banlieue", "stop_lat": "48.80", "stop_lon": "2.30", "location_type": "0", "parent_station": "ZB"},
    ]
    trips, times = [], []

    def trip(tid, route, direction, headsign, calls):
        trips.append({"route_id": route, "service_id": "S", "trip_id": tid, "trip_headsign": headsign, "direction_id": direction})
        for i, (sp, t) in enumerate(calls):
            times.append({"trip_id": tid, "arrival_time": t, "departure_time": t, "stop_id": sp, "stop_sequence": str(i + 1),
                          "pickup_type": "1" if i == len(calls) - 1 else "0", "drop_off_type": "1" if i == 0 else "0"})

    trip("X1", "RX", "0", "AERO", [("C1", "08:00:00"), ("M1", "08:10:00"), ("A1", "08:30:00")])
    trip("X2", "RX", "0", "AERO", [("C1", "08:20:00"), ("M1", "08:30:00"), ("A1", "08:50:00")])
    trip("X3", "RX", "1", "CITY", [("A1", "09:00:00"), ("M1", "09:20:00"), ("C1", "09:30:00")])
    trip("Y1", "RY", "0", "Banlieue", [("C1", "08:05:00"), ("B1", "08:25:00")])
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("agency.txt", _csv([{"agency_id": "A", "agency_name": "Réseau test", "agency_url": "https://example.org", "agency_timezone": "Europe/Paris"}]))
        z.writestr("routes.txt", _csv([
            {"route_id": "RX", "agency_id": "A", "route_short_name": "X", "route_long_name": "Ligne X", "route_type": "3", "route_color": "112233", "route_text_color": "FFFFFF"},
            {"route_id": "RY", "agency_id": "A", "route_short_name": "Y", "route_long_name": "Ligne Y", "route_type": "3", "route_color": "445566", "route_text_color": "FFFFFF"}]))
        z.writestr("stops.txt", _csv(stops))
        z.writestr("trips.txt", _csv(trips))
        z.writestr("stop_times.txt", _csv(times))
        z.writestr("calendar.txt", _csv([{"service_id": "S", "monday": "1", "tuesday": "1", "wednesday": "1", "thursday": "1",
                                          "friday": "1", "saturday": "1", "sunday": "1", "start_date": "20260701", "end_date": "20260710"}]))
        z.writestr("calendar_dates.txt", "service_id,date,exception_type\n")


def line(code, route, sectors):
    return {"id": code, "display_name": code, "app_type": "BUS", "category": "SURFACE", "sectors": sectors,
            "sort_order": {s: 1 for s in sectors}, "shortcut_sectors": [], "idfm_route_id": route,
            "gtfs": {"short_name": route[1:]}, "state": "active", "future": None}


LINES = {"verified": {"future_dates_checked_on": "2026-07-01"}, "lines": [line("BUS_X", "RX", ["ROISSY"]), line("BUS_Y", "RY", ["ROISSY"])]}
PLACES = {"places": [
    {"id": "paris", "kind": "city", "label_fr": "Paris", "label_en": "Paris", "short_label": "Paris",
     "boarding": [{"line": "BUS_X", "stop_area": "ZC", "name": "Gare Centrale"}]},
    {"id": "aero", "kind": "airport", "sector": "ROISSY", "label_fr": "Terminal", "label_en": "Terminal", "short_label": "T",
     "stop_areas": ["ZA"], "stop_points": []},
]}
ADJ = {"adjustments": []}


def build(tmp: Path, gtfs: Path, places: dict, name: str):
    report = bt.Report()
    db = tmp / f"{name}.sqlite"
    bt.build_db(db, gtfs, LINES, places, ADJ, report)
    codes = sorted({e["code"] for e in report.errors})
    warns = sorted({w["code"] for w in report.warnings})
    return db, codes, warns


def main() -> int:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        gtfs = tmp / "gtfs.zip"
        make_gtfs(gtfs)

        db, errors, warns = build(tmp, gtfs, PLACES, "ok")
        check("base valide : aucune erreur", errors, [])
        check("base valide : aucune alerte de point de montée", [w for w in warns if w.startswith("boarding")], [])
        con = sqlite3.connect(db)
        check("schéma 2 : user_version", con.execute("PRAGMA user_version").fetchone()[0], 2)
        check("lieux : sorte, secteur et ordre du sélecteur",
              con.execute("SELECT id, kind, sector, sort_order FROM place ORDER BY sort_order").fetchall(),
              [("paris", "city", None, 0), ("aero", "airport", "ROISSY", 1)])
        check("Paris : point de montée écrit (ligne, zone d'arrêt)",
              con.execute("""SELECT p.place_id, l.code, a.gtfs_id FROM place_boarding p JOIN line l ON l.id = p.line_id
                             JOIN stop_area a ON a.id = p.stop_area_id""").fetchall(), [("paris", "BUS_X", "ZC")])
        check("Paris : aucun quai propre", con.execute("SELECT COUNT(*) FROM place_stop_point WHERE place_id = 'paris'").fetchone()[0], 0)
        con.close()

        tt = Timetable(str(db))
        g = [(x.line, x.key, x.title, x.board.status, [datetime.fromtimestamp(p.epoch, PARIS).strftime("%H:%M") for p in x.board.passages])
             for x in tt.place_departures("paris", datetime(2026, 7, 2, 7, 50, tzinfo=PARIS))]
        check("départs de Paris : seuls les trajets vers l'aéroport, destination = dernier quai",
              g, [("BUS_X", "sector:ROISSY", "Aéroport Terminal", "ok", ["08:00", "08:20"])])
        g = [(x.line, x.key, x.title, [datetime.fromtimestamp(p.epoch, PARIS).strftime("%H:%M") for p in x.board.passages])
             for x in tt.place_departures("aero", datetime(2026, 7, 2, 8, 50, tzinfo=PARIS))]
        check("départs de l'aéroport : une carte par sens, montée interdite au terminus (R-32)",
              g, [("BUS_X", "direction:1", "Gare Centrale", ["09:00"])])
        tt.con.close()

        # point de montée d'une ligne qui ne mène pas à l'aéroport : alerte, carte « Horaires indisponibles »
        p = copy.deepcopy(PLACES)
        p["places"][0]["boarding"].append({"line": "BUS_Y", "stop_area": "ZC", "name": "Gare Centrale"})
        db, errors, warns = build(tmp, gtfs, p, "notserved")
        check("ligne qui ne mène plus à l'aéroport : pas d'erreur", errors, [])
        check("ligne qui ne mène plus à l'aéroport : alerte boarding_not_served", "boarding_not_served" in warns, True)
        tt = Timetable(str(db))
        g = [(x.line, x.key, x.title, x.board.status) for x in tt.place_departures("paris", datetime(2026, 7, 2, 7, 50, tzinfo=PARIS))]
        check("ligne qui ne mène plus à l'aéroport : carte par secteur de la ligne, « Horaires indisponibles »",
              g, [("BUS_X", "sector:ROISSY", "Aéroport Terminal", "ok"), ("BUS_Y", "sector:ROISSY", "", "no_data")])
        tt.con.close()

        cases = [
            ("nom différent du GTFS : alerte", lambda q: q["places"][0]["boarding"][0].update(name="Ancien nom"), [], "boarding_name_differs"),
            ("zone d'arrêt absente du GTFS : erreur", lambda q: q["places"][0]["boarding"][0].update(stop_area="ZZ"),
             ["place_boarding_stop_missing", "stop_area_missing"], None),
            ("ligne inconnue : erreur", lambda q: q["places"][0]["boarding"][0].update(line="BUS_Z"), ["place_boarding_line_unknown"], None),
            ("ligne en double : erreur", lambda q: q["places"][0]["boarding"].append(dict(q["places"][0]["boarding"][0])),
             ["place_boarding_duplicate"], None),
            ("ville sans point de montée : erreur", lambda q: q["places"][0].update(boarding=[]), ["place_without_boarding"], None),
            ("ville avec un secteur : erreur", lambda q: q["places"][0].update(sector="ROISSY"), ["place_city_invalid"], None),
            ("aéroport sans secteur : erreur", lambda q: q["places"][1].pop("sector"), ["place_airport_invalid"], None),
            ("sorte inconnue : erreur", lambda q: q["places"][1].update(kind="port"), ["place_kind_invalid"], None),
        ]
        for i, (name, mutate, exp_errors, exp_warn) in enumerate(cases):
            q = copy.deepcopy(PLACES)
            mutate(q)
            _db, errors, warns = build(tmp, gtfs, q, f"case{i}")
            check(name, errors, exp_errors)
            if exp_warn:
                check(f"{name} ({exp_warn})", exp_warn in warns, True)
    for f in failures:
        print("ÉCHEC", f)
    print(f"{passed} contrôles conformes, {len(failures)} échec(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
