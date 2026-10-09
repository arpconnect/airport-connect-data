#!/usr/bin/env python3
"""
Teste les itinéraires hors connexion du lecteur de référence (spec R-119) :

    python3 tests/test_offline_router.py                          # réseau fictif minimal, sans réseau
    python3 tests/test_offline_router.py base-juin-2026.sqlite.gz   # et scénarios réels sur la base de juin 2026

Réseau fictif (construit par le générateur, un jour de service du 1er au 10 juillet 2026, base construite le 1er) :
  - changement au même quai : 2 min au moins (le bus qui part 1 min après l'arrivée est manqué) ;
  - correspondance vers un autre quai par transfers.txt (60 s), plus rapide que le changement au même quai ;
  - marche de départ (transfers.txt depuis le quai de départ), placée juste avant la montée ;
  - montée interdite (pickup_type = 1) : aucun trajet ;
  - trajet de nuit du jour de service de la veille (24:30) ;
  - veille non couverte par la base (R-153) : résultat signalé incomplet ;
  - base expirée (R-173), déjà sur place, quai inconnu ;
  - trajets dominés écartés (même arrivée, départ plus tôt).
Base de juin 2026 (si fournie) : Orly 4 → Terminal 1 par Orlyval, RER B et CDGVal ; quais non desservis de la 351
(R-91) jamais utilisés ; heures croissantes et étapes cohérentes sur tous les trajets d'un ensemble de lieux.
"""

from __future__ import annotations

import csv
import gzip
import io
import shutil
import sys
import tempfile
import zipfile
from datetime import date, datetime
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


STOPS = ["A1", "B1", "C1", "C2", "D1", "E1", "F1", "G1", "H1"]


def make_gtfs(path: Path) -> None:
    stops = []
    for sp in STOPS:
        stops.append({"stop_id": "Z" + sp, "stop_name": "Zone " + sp, "stop_lat": "48.9", "stop_lon": "2.4",
                      "location_type": "1", "parent_station": ""})
        stops.append({"stop_id": sp, "stop_name": "Quai " + sp, "stop_lat": "48.9", "stop_lon": "2.4",
                      "location_type": "0", "parent_station": "Z" + sp})
    trips, times = [], []

    def trip(tid, route, calls, pickup=None):
        trips.append({"route_id": route, "service_id": "S", "trip_id": tid, "trip_headsign": tid, "direction_id": "0"})
        for i, (sp, t) in enumerate(calls):
            pu = "1" if i == len(calls) - 1 else (pickup if pickup is not None and i == 0 else "0")
            times.append({"trip_id": tid, "arrival_time": t, "departure_time": t, "stop_id": sp, "stop_sequence": str(i + 1),
                          "pickup_type": pu, "drop_off_type": "1" if i == 0 else "0"})

    trip("L1a", "R1", [("A1", "10:00:00"), ("B1", "10:10:00"), ("C1", "10:20:00")])
    trip("L1b", "R1", [("A1", "10:30:00"), ("B1", "10:40:00"), ("C1", "10:50:00")])
    trip("L2a", "R2", [("B1", "10:11:00"), ("D1", "10:25:00")])          # 1 min après l'arrivée du L1a : manqué
    trip("L2b", "R2", [("B1", "10:12:00"), ("D1", "10:31:00")])          # 2 min après : possible
    trip("L3a", "R3", [("C2", "10:22:00"), ("D1", "10:28:00")])          # par la correspondance C1 → C2 (60 s)
    trip("L4a", "R4", [("E1", "10:00:00"), ("D1", "10:20:00")], pickup="1")
    trip("L5a", "R5", [("A1", "24:30:00"), ("D1", "24:50:00")])          # nuit : 00:30 le lendemain
    trip("L6a", "R6", [("H1", "15:00:00"), ("G1", "15:30:00")])          # H1 présent dans la base
    trip("L7a", "R7", [("F1", "11:00:00"), ("G1", "11:30:00")])
    trip("L8a", "R8", [("F1", "11:10:00"), ("G1", "11:30:00")])          # domine L7a
    trip("L9a", "R9", [("F1", "11:40:00"), ("G1", "12:10:00")])
    routes = [{"route_id": f"R{i}", "agency_id": "A", "route_short_name": str(i), "route_long_name": f"Ligne {i}",
               "route_type": "3", "route_color": "112233", "route_text_color": "FFFFFF"} for i in (1, 2, 3, 4, 5, 6, 7, 8, 9)]
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("agency.txt", _csv([{"agency_id": "A", "agency_name": "Réseau test", "agency_url": "https://example.org",
                                        "agency_timezone": "Europe/Paris"}]))
        z.writestr("routes.txt", _csv(routes))
        z.writestr("stops.txt", _csv(stops))
        z.writestr("trips.txt", _csv(trips))
        z.writestr("stop_times.txt", _csv(times))
        z.writestr("calendar.txt", _csv([{"service_id": "S", "monday": "1", "tuesday": "1", "wednesday": "1",
                                          "thursday": "1", "friday": "1", "saturday": "1", "sunday": "1",
                                          "start_date": "20260701", "end_date": "20260710"}]))
        z.writestr("calendar_dates.txt", "service_id,date,exception_type\n")
        z.writestr("transfers.txt", _csv([
            {"from_stop_id": "C1", "to_stop_id": "C2", "transfer_type": "2", "min_transfer_time": "60"},
            {"from_stop_id": "H1", "to_stop_id": "A1", "transfer_type": "2", "min_transfer_time": "120"},
        ]))


def line(code, route):
    return {"id": code, "display_name": code, "app_type": "BUS", "category": "SURFACE", "sectors": ["ROISSY"],
            "sort_order": {"ROISSY": 1}, "shortcut_sectors": [], "idfm_route_id": route,
            "gtfs": {"short_name": route[1:]}, "state": "active", "future": None}


LINES = {"verified": {"future_dates_checked_on": "2026-07-01"},
         "lines": [line(f"BUS_{i}", f"R{i}") for i in (1, 2, 3, 4, 5, 6, 7, 8, 9)]}
PLACES = {"places": [
    {"id": "paris", "kind": "city", "label_fr": "Paris", "label_en": "Paris", "short_label": "Paris",
     "boarding": [{"line": "BUS_1", "stop_area": "ZA1", "name": "Zone A1"}]},
    {"id": "aero", "kind": "airport", "sector": "ROISSY", "label_fr": "Terminal", "label_en": "Terminal",
     "short_label": "T", "stop_areas": ["ZD1"], "stop_points": []},
]}


def at(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=PARIS)


def hm(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, PARIS).strftime("%H:%M:%S")


def shape(j: dict) -> list:
    """Résumé lisible d'un trajet : étapes avec heures et durées."""
    out = []
    for l in j["legs"]:
        if l["kind"] == "pt":
            out.append(f"{l['code']} {l['from']['name']} {hm(l['departure'])} > {l['to']['name']} {hm(l['arrival'])}")
        elif l["kind"] == "walk":
            out.append(f"marche {l['from']} > {l['to']} {hm(l['departure'])}-{hm(l['arrival'])}")
        else:
            out.append(f"attente {l['duration']} s")
    return out


def mini() -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        gtfs = tmp / "gtfs.zip"
        make_gtfs(gtfs)
        report = bt.Report()
        db = tmp / "mini.sqlite"
        bt.build_db(db, gtfs, LINES, PLACES, {"adjustments": []}, report, date(2026, 7, 1))
        check("réseau fictif : base sans erreur", [e["code"] for e in report.errors], [])
        tt = Timetable(str(db))

        r = tt.offline_journeys(["A1"], ["D1"], at("2026-07-01T09:55"))
        check("A → D : statut", (r["status"], r["incomplete"]), ("ok", False))
        check("A → D : meilleur trajet par la correspondance C1 → C2 (60 s)", shape(r["journeys"][0]), [
            "1 Quai A1 10:00:00 > Quai C1 10:20:00", "marche Quai C1 > Quai C2 10:20:00-10:21:00",
            "attente 60 s", "3 Quai C2 10:22:00 > Quai D1 10:28:00"])
        check("A → D : arrivée, correspondances, marche", (hm(r["journeys"][0]["arrival"]), r["journeys"][0]["transfers"],
                                                         r["journeys"][0]["walking"]), ("10:28:00", 1, 60))
        check("A → D : aucun trajet par le bus qui part 1 min après l'arrivée au même quai",
              any("10:11:00" in s for j in r["journeys"] for s in shape(j)), False)
        check("A → D : format des étapes de transport", sorted(r["journeys"][0]["legs"][0]), sorted([
            "kind", "line", "code", "mode", "physical_mode", "network", "color", "text_color", "direction",
            "vehicle_journey", "from", "to", "departure", "arrival", "base_departure", "base_arrival", "realtime",
            "stops", "stop_names", "wheelchair", "notices"]))
        first = r["journeys"][0]["legs"][0]
        check("A → D : ligne, quais et terminus (R-30)", (first["line"], first["from"], first["direction"], first["stop_names"]),
              ("line:R1", {"stop_point": "stop_point:A1", "stop_area": "stop_area:ZA1", "name": "Quai A1"}, "Quai C1",
               ["Quai A1", "Quai B1", "Quai C1"]))

        # sans la correspondance C1 → C2 (départ après le dernier L3) : changement au même quai B1 en 2 min
        r = tt.offline_journeys(["B1"], ["D1"], at("2026-07-01T10:11:30"))
        check("B → D : le bus de 10:12 au quai B1", [shape(j) for j in r["journeys"]],
              [["2 Quai B1 10:12:00 > Quai D1 10:31:00"]])

        r = tt.offline_journeys(["H1"], ["D1"], at("2026-07-01T09:50"))
        check("H → D : marche de départ juste avant la montée", shape(r["journeys"][0])[:2], [
            "marche Quai H1 > Quai A1 09:58:00-10:00:00", "1 Quai A1 10:00:00 > Quai C1 10:20:00"])

        r = tt.offline_journeys(["E1"], ["D1"], at("2026-07-01T09:55"))
        check("E → D : montée interdite (R-32)", (r["status"], r["journeys"]), ("no_solution", []))

        r = tt.offline_journeys(["A1"], ["D1"], at("2026-07-02T00:20"))
        check("A → D à 00:20 : trajet de nuit du jour de service de la veille", [shape(j) for j in r["journeys"]][:1],
              [["5 Quai A1 00:30:00 > Quai D1 00:50:00"]])
        check("A → D à 00:20 le 2 juillet : veille couverte", r["incomplete"], False)
        r = tt.offline_journeys(["A1"], ["D1"], at("2026-07-01T00:10"))
        check("A → D à 00:10 le 1er juillet : veille non couverte (R-153)", r["incomplete"], True)

        check("base expirée (R-173)", tt.offline_journeys(["A1"], ["D1"], at("2026-07-20T10:00"))["status"], "base_expired")
        check("déjà sur place", tt.offline_journeys(["A1", "D1"], ["D1"], at("2026-07-01T10:00"))["status"], "already_there")
        check("aucun quai", tt.offline_journeys([], ["D1"], at("2026-07-01T10:00"))["status"], "no_stop")

        r = tt.offline_journeys(["F1"], ["G1"], at("2026-07-01T10:55"))
        check("F → G : trajet dominé écarté, ordre par arrivée", [shape(j) for j in r["journeys"]], [
            ["8 Quai F1 11:10:00 > Quai G1 11:30:00"], ["9 Quai F1 11:40:00 > Quai G1 12:10:00"]])
        tt.con.close()


def coherent(j: dict) -> bool:
    """Heures croissantes, étapes enchaînées, durées cohérentes."""
    t = j["departure"]
    for l in j["legs"]:
        if l["kind"] == "wait":
            t += l["duration"]
            continue
        if l["departure"] < t or l["arrival"] < l["departure"]:
            return False
        if l["kind"] == "walk" and l["arrival"] - l["departure"] != l["duration"]:
            return False
        t = l["arrival"]
    return t == j["arrival"] and j["duration"] == j["arrival"] - j["departure"]


def june(path: Path) -> None:
    with tempfile.TemporaryDirectory() as t:
        db = Path(t) / "june.sqlite"
        with gzip.open(path, "rb") as fi, open(db, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        tt = Timetable(str(db))
        if tt.meta["validity_start"] != "2026-06-15":
            print("(scénarios réels ignorés : cas écrits pour la base de juin 2026)")
            return
        r = tt.offline_journeys(tt.stop_points_of_place("orly-4"), tt.destination_stop_points(("place", "cdg-t1")),
                                at("2026-07-01T08:00"))
        j = r["journeys"][0]
        check("Orly 4 → Terminal 1 : lignes", [l["code"] for l in j["legs"] if l["kind"] == "pt"], ["ORLYVAL", "B", "CDG VAL"])
        check("Orly 4 → Terminal 1 : horaires", (hm(j["departure"]), hm(j["arrival"])), ("08:00:00", "09:24:00"))
        # R-91 : un quai non desservi de la 351 n'est jamais un quai de descente. Le mercredi 24 juin, les bus du GTFS
        # passent encore par l'ancien itinéraire, dont le seul quai de la zone Porte de Montreuil est non desservi :
        # aucun trajet (et non une descente à ce quai). Le samedi 20 juin, l'itinéraire en vigueur dessert la zone.
        r = tt.offline_journeys(tt.stop_points_of_place("cdg-t3"), tt.destination_stop_points(("stop_area", "IDFM:71710")),
                                at("2026-06-24T10:00"))
        check("351, mercredi : quai non desservi jamais utilisé (R-91)", (r["status"], r["journeys"]), ("no_solution", []))
        r = tt.offline_journeys(tt.stop_points_of_place("cdg-t3"), tt.destination_stop_points(("stop_area", "IDFM:71710")),
                                at("2026-06-20T10:00"))
        check("351, samedi : descente à Porte de Montreuil (itinéraire en vigueur)",
              {(l["code"], l["to"]["stop_point"]) for j in r["journeys"] for l in j["legs"] if l["kind"] == "pt"},
              {("351", "stop_point:IDFM:36720")})
        places = [p["id"] for p in tt.places() if p["kind"] == "airport"]
        bad, count = [], 0
        for o in places[::3]:
            for d in places[1::4]:
                if o == d:
                    continue
                for when in ("2026-07-01T07:30", "2026-07-03T23:40"):
                    res = tt.offline_journeys(tt.stop_points_of_place(o), tt.destination_stop_points(("place", d)), at(when))
                    for j in res["journeys"]:
                        count += 1
                        if not coherent(j) or j["departure"] < at(when).timestamp():
                            bad.append((o, d, when, j["signature"]))
        check(f"trajets cohérents entre lieux ({count} trajets)", bad, [])
        check("au moins 20 trajets vérifiés", count >= 20, True)
        tt.con.close()


def main() -> int:
    mini()
    if len(sys.argv) > 1:
        june(Path(sys.argv[1]))
    for f in failures:
        print("ÉCHEC", f)
    print(f"{passed} contrôles conformes, {len(failures)} échec(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
