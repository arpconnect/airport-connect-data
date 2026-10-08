#!/usr/bin/env python3
"""
Valide la base d'horaires produite par tools/pipeline/build_timetable.py.

    python3 tests/test_timetable_db.py dist/timetable-<version>.sqlite.gz

1. Complétude du modèle : les cas réels de l'étape 1 (tests/characterization/gtfs-2026-06/,
   calculés directement sur le GTFS brut) sont recalculés depuis la base et doivent être
   identiques. Ce bloc ne s'applique qu'à la base issue du GTFS de référence (même empreinte).
2. Règles de lecture : prochains passages, frontières de service, expiration, absence de données.
   Aménagements de desserte (R-90 à R-93) : contrôles de structure sur toute base, scénarios de la
   ligne 351 sur la base de juin 2026.
3. Performance : temps de la requête « prochains passages » sur les quais les plus chargés.
"""

from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "pipeline"))
from timetable_reader import Timetable  # noqa: E402

PARIS = ZoneInfo("Europe/Paris")
CASES = ROOT / "tests" / "characterization" / "gtfs-2026-06"
AIRPORT_HINTS = ("orly", "gaulle", "roissy", "terminal", "cdg", "aéroport", "aeroport", "bourget", "drancy", "garonor")
failures, passed = [], 0


def check(name, got, expected):
    global passed
    if got == expected:
        passed += 1
    else:
        failures.append(f"{name}\n    obtenu  : {got!r}\n    attendu : {expected!r}")


def hm(s: int) -> str:
    s %= 86400
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}"


def at(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=PARIS)


def active_trips(tt: Timetable, line: str, d: date):
    idx = (d - tt.base).days
    return tt.con.execute(
        """SELECT t.id, p.id, p.direction, p.headsign FROM trip t JOIN pattern p ON p.id = t.pattern_id
           JOIN line l ON l.id = p.line_id JOIN service s ON s.id = t.service_id
           WHERE l.code = ? AND ((s.days >> ?) & 1) = 1""", (line, idx)).fetchall()


def trip_stops(tt: Timetable, trip_id: int):
    return tt.con.execute(
        """SELECT sp.name, st.departure, ps.pickup FROM stop_time st JOIN trip t ON t.id = st.trip_id
           JOIN pattern_stop ps ON ps.pattern_id = t.pattern_id AND ps.seq = st.seq
           JOIN stop_point sp ON sp.id = ps.stop_point_id WHERE st.trip_id = ? ORDER BY st.seq""", (trip_id,)).fetchall()


# --------------------------------------------------------------------------- 1. complétude
def completeness(tt: Timetable) -> None:
    snap = json.loads((CASES / "snapshot.json").read_text(encoding="utf-8"))
    if snap["source_zip_sha256"] != tt.meta["source_sha256"]:
        print("(bloc 1 ignoré : la base ne provient pas du GTFS de référence)")
        return
    per_line = dict(tt.con.execute(
        "SELECT l.code, COUNT(t.id) FROM line l LEFT JOIN pattern p ON p.line_id = l.id LEFT JOIN trip t ON t.pattern_id = p.id GROUP BY l.code"))
    for code, c in snap["coverage"].items():
        check(f"couverture {code}", per_line.get(code, 0), c["trips"])

    # Noctilien : départs vers le pôle depuis l'arrêt de montée
    noct = json.loads((CASES / "noctilien_boarding.json").read_text(encoding="utf-8"))
    for line, v in noct["lines"].items():
        for label, night in v["nights"].items():
            deps = set()
            for tid, *_ in active_trips(tt, line, date.fromisoformat(night["service_date"])):
                st = trip_stops(tt, tid)
                for i, (name, dep, pickup) in enumerate(st):
                    if name == v["boarding_stop"] and pickup != 1 and any(
                            any(h in n.lower() for h in AIRPORT_HINTS) for n, _, _ in st[i + 1:]):
                        deps.add(dep)
            check(f"noctilien {line} {label}", [hm(d) for d in sorted(deps)], night["gtfs"])

    # Navettes : profil d'une journée
    prof = json.loads((CASES / "shuttle_profiles.json").read_text(encoding="utf-8"))
    d = date.fromisoformat(prof["reference_date"])
    for line, p in prof["lines"].items():
        trips = active_trips(tt, line, d)
        check(f"navette {line} trajets", len(trips), p["trips_on_reference_day"])
        by_dir = defaultdict(list)
        for tid, pid, direction, head in trips:
            by_dir["-" if direction is None else str(direction)].append((tid, head))
        for dkey, exp in p["directions"].items():
            tids = by_dir.get(dkey, [])
            starts = sorted(trip_stops(tt, t)[0][1] for t, _ in tids)
            check(f"navette {line} dir {dkey} départs", (len(starts), hm(starts[0]), hm(starts[-1])),
                  (exp["departures_from_origin"], exp["first"], exp["last"]))
            check(f"navette {line} dir {dkey} girouettes", sorted({h for _, h in tids}), exp["headsigns"])
            longest = max((trip_stops(tt, t) for t, _ in tids), key=len)
            check(f"navette {line} dir {dkey} desserte", [n for n, _, _ in longest], exp["stops_longest_trip"])
        durations = sorted({(trip_stops(tt, t)[-1][1] - trip_stops(tt, t)[0][1]) // 60 for t, *_ in trips})
        check(f"navette {line} durée", {"min": durations[0], "max": durations[-1]}, p["trip_duration_minutes"])
    hours = Counter()
    for tid, *_ in active_trips(tt, "BUS_N2", d):
        for name, dep, _ in trip_stops(tt, tid):
            if name == "Terminal 2E":
                hours[f"{(dep // 3600) % 24:02d}h"] += 1
    check("N2 Terminal 2E par heure", dict(sorted(hours.items())), prof["lines"]["BUS_N2"]["terminal_2E_calls_by_hour"])

    # RER B : desserte du Bourget
    rb = json.loads((CASES / "rer_b_le_bourget.json").read_text(encoding="utf-8"))
    counts = Counter()
    for tid, *_ in active_trips(tt, "RER_B", date.fromisoformat(rb["reference_date"])):
        names = [n for n, _, _ in trip_stops(tt, tid)]
        if rb["origin"] not in names or names.index(rb["origin"]) == len(names) - 1:
            continue
        after = names[names.index(rb["origin"]) + 1:]
        if any(("Gaulle" in n) or ("Mitry" in n) for n in after):
            counts[(names[-1], "Le Bourget" in after)] += 1
    check("RER B Le Bourget", [{"terminus": t, "calls_at_le_bourget": b, "count": n} for (t, b), n in sorted(counts.items())],
          rb["trains_northbound"])


# --------------------------------------------------------------------------- 2. lecture
def reading_rules(tt: Timetable) -> None:
    if tt.meta["validity_start"] != "2026-06-15":
        print("(bloc 2 ignoré : cas écrits pour la base de juin 2026)")
        return
    t1 = tt.stop_points_of_place("cdg-t1")
    b = tt.board("CDGVAL", t1, at("2026-07-01T08:00"))
    check("CDGVal T1 08:00 statut", (b.status, len(b.passages), b.boundary), ("ok", 4, None))
    check("CDGVal T1 08:00 premier passage ≥ maintenant", all(p.epoch >= at("2026-07-01T08:00").timestamp() for p in b.passages), True)
    check("CDGVal T1 : aucun départ affiché au terminus côté arrivée (R-32)",
          all(p.headsign != "Terminal 1" for p in b.passages), True)

    t2e = ["IDFM:10227"]
    b = tt.board("BUS_N2", t2e, at("2026-07-01T12:00"))
    check("N2 T2E midi (R-61 + R-56 v1)", (b.status, b.boundary, datetime.fromtimestamp(b.next_service_start, PARIS).strftime("%H:%M")),
          ("ok", "service_ended", "21:33"))
    b = tt.board("BUS_N2", t2e, at("2026-07-01T20:30"))
    check("N2 T2E 20:30 : reprise dans 63 min → « terminé » (règle v1, cf. D-6)", (b.status, b.boundary), ("ok", "service_ended"))
    b = tt.board("BUS_N2", t2e, at("2026-07-01T20:40"))
    check("N2 T2E 20:40 : reprise dans 53 min", (b.status, b.boundary), ("ok", "service_not_started"))
    b = tt.board("BUS_N2", t2e, at("2026-07-01T21:20"))
    check("N2 T2E 21:20 : passages révélés 15 min avant (premier 21:33)",
          (b.status, datetime.fromtimestamp(b.passages[0].epoch, PARIS).strftime("%H:%M") if b.passages else None), ("ok", "21:33"))

    orly4 = tt.stop_points_of_place("orly-4")
    b = tt.board("ORLYVAL", orly4, at("2026-07-01T03:00"))
    check("Orlyval Orly 4 à 03:00", (b.status, b.boundary), ("ok", "service_ended"))
    b = tt.board("ORLYVAL", orly4, at("2026-07-18T12:00"))
    check("base expirée (R-173)", b.status, "base_expired")
    b = tt.board("ORLYVAL", tt.stop_points_of_place("cdg-t1"), at("2026-07-01T12:00"))
    check("ligne qui ne dessert pas le quai (R-153)", b.status, "no_data")

    chatelet = tt.stop_points_named("Châtelet", "BUS_N22")
    b = tt.board("BUS_N22", chatelet, at("2026-07-02T00:20"))
    check("N22 Châtelet nuit : passages après minuit du bloc de la veille",
          [datetime.fromtimestamp(p.epoch, PARIS).strftime("%H:%M") for p in b.passages][:1] != [], True)


# --------------------------------------------------------------------------- 2 bis. aménagements
def adjustments_structure(tt: Timetable) -> None:
    """Valable pour toute base produite (R-90 à R-93)."""
    rows = tt.con.execute("SELECT a.id, l.code, a.gtfs_lagging_trips FROM adjustment a JOIN line l ON l.id = a.line_id").fetchall()
    for aid, line, lagging in rows:
        foreign = tt.con.execute(
            """SELECT COUNT(*) FROM adjustment_stop s WHERE s.adjustment_id = ? AND NOT EXISTS (
                 SELECT 1 FROM pattern_stop ps JOIN pattern p ON p.id = ps.pattern_id JOIN line l ON l.id = p.line_id
                 WHERE ps.stop_point_id = s.stop_point_id AND l.code = ?)""", (aid, line)).fetchone()[0]
        check(f"{aid} : quais non desservis tous issus de la ligne {line}", foreign, 0)
        recount = tt.con.execute(
            """SELECT COUNT(DISTINCT t.id) FROM adjustment_stop s JOIN pattern_stop ps ON ps.stop_point_id = s.stop_point_id
               JOIN pattern p ON p.id = ps.pattern_id JOIN line l ON l.id = p.line_id JOIN trip t ON t.pattern_id = p.id
               WHERE s.adjustment_id = ? AND l.code = ?""", (aid, line)).fetchone()[0]
        check(f"{aid} : trajets en retard recomptés", recount, lagging)
        overlap = tt.con.execute(
            """SELECT COUNT(*) FROM adjustment_gap g JOIN adjustment_stop s
               ON s.adjustment_id = g.adjustment_id AND s.stop_point_id = g.stop_point_id WHERE g.adjustment_id = ?""",
            (aid,)).fetchone()[0]
        check(f"{aid} : aucun quai à la fois non desservi et lacunaire", overlap, 0)
        bad_gap = tt.con.execute(
            """SELECT COUNT(*) FROM adjustment_gap g WHERE g.adjustment_id = ? AND (
                 NOT EXISTS (SELECT 1 FROM pattern_stop ps JOIN adjustment_stop s ON s.stop_point_id = ps.stop_point_id
                             WHERE ps.pattern_id = g.pattern_id AND s.adjustment_id = g.adjustment_id)
                 OR EXISTS (SELECT 1 FROM pattern_stop ps WHERE ps.pattern_id = g.pattern_id
                            AND ps.stop_point_id = g.stop_point_id))""", (aid,)).fetchone()[0]
        check(f"{aid} : chaque lacune vient d'une mission en retard qui contourne le quai", bad_gap, 0)
        if not lagging:
            check(f"{aid} : pas de lacune quand le GTFS est aligné",
                  tt.con.execute("SELECT COUNT(*) FROM adjustment_gap WHERE adjustment_id = ?", (aid,)).fetchone()[0], 0)


def adjustments_351(tt: Timetable, db_path: Path) -> None:
    if tt.meta["validity_start"] != "2026-06-15":
        print("(scénarios 351 ignorés : cas écrits pour la base de juin 2026)")
        return
    def fmt(b):
        return (b.status, b.incomplete, b.boundary,
                [datetime.fromtimestamp(p.epoch, PARIS).strftime("%a %H:%M") for p in b.passages],
                [(r.label, r.distance_m) for r in b.replacements])
    adj = tt.adjustments("BUS_351", date(2026, 6, 24))
    check("351 : un aménagement en vigueur", [(a.id, a.kind, a.valid_from, a.valid_until) for a in adj],
          [("bus-351-porte-de-montreuil", "permanent", None, None)])
    a = adj[0]
    check("351 : huit quais non desservis, reports et distances",
          [(r.stop_point, r.direction_label, r.label, r.distance_m) for r in a.not_served], [
              ("IDFM:24730", "Nation - Trône", "Porte de Montreuil", 50),
              ("IDFM:27048", "Nation - Trône", "Gallieni", 360),
              ("IDFM:27062", "Nation - Trône", "Porte de Montreuil", 600),
              ("IDFM:28370", "Gare de Roissypôle", "Davout - Lagny", 380),
              ("IDFM:28373", "Gare de Roissypôle", "Porte de Montreuil", 330),
              ("IDFM:28374", "Gare de Roissypôle", "Porte de Montreuil", 540),
              ("IDFM:39739", "Gare de Roissypôle", "Gallieni", 340),
              ("IDFM:7766", "Gare de Roissypôle", "Gallieni", 310)])
    check("351 : trajets du GTFS qui desservent encore l'ancien itinéraire", a.gtfs_lagging_trips, 516)
    check("351 : quais aux horaires incomplets (Porte de Montreuil, Porte de Bagnolet, Échangeur, deux sens)",
          sorted(a.incomplete_stop_points),
          ["IDFM:24859", "IDFM:24861", "IDFM:36720", "IDFM:36721", "IDFM:37462", "IDFM:471489"])
    b = tt.board("BUS_351", ["IDFM:28370"], at("2026-06-24T10:00"))
    check("351 Erignac : non desservi même si le GTFS y publie des horaires (R-91)", fmt(b),
          ("not_served", False, None, [], [("Davout - Lagny", 380)]))
    b = tt.board("BUS_351", ["IDFM:24730", "IDFM:36720"], at("2026-06-20T10:00"))
    check("351 Porte de Montreuil (zone) samedi : seul le quai desservi compte, horaires complets", fmt(b),
          ("ok", False, None, ["Sat 10:08", "Sat 10:23", "Sat 10:50", "Sat 11:08"], []))
    b = tt.board("BUS_351", ["IDFM:36721"], at("2026-06-24T10:00"))
    check("351 Porte de Montreuil mercredi : horaires manquants, jamais « service terminé » (R-92, R-153)", fmt(b),
          ("no_data", True, None, [], []))
    b = tt.board("BUS_351", ["IDFM:36721"], at("2026-06-21T23:50"))
    check("351 Porte de Montreuil dimanche 23:50 : pas de « reprise à » calculée sur un lundi incomplet", fmt(b),
          ("no_data", True, None, [], []))
    b = tt.board("BUS_351", ["IDFM:36721"], at("2026-06-19T23:30"))
    check("351 Porte de Montreuil vendredi 23:30 : missions en retard terminées, la fin de service reste fiable",
          (b.status, b.incomplete, b.boundary), ("ok", False, "service_ended"))
    b = tt.board("BUS_351", tt.stop_points_of_place("cdg-t1"), at("2026-06-24T10:00"))
    check("351 CDG T1 mercredi : horaires de l'aéroport conservés", fmt(b),
          ("ok", False, None, ["Wed 10:04", "Wed 10:31", "Wed 11:05", "Wed 11:38"], []))
    b = tt.board("BUS_351", ["IDFM:37464"], at("2026-06-24T10:00"))
    check("351 Gallieni : desservi par les deux itinéraires, pas de lacune", (b.status, b.incomplete), ("ok", False))

    # Bornes de validité : un aménagement échu ne s'applique plus (copie modifiée de la base)
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "t.sqlite"
        shutil.copyfile(db_path, copy)
        con = sqlite3.connect(copy)
        con.execute("UPDATE adjustment SET valid_until = '2026-06-23'")
        con.commit()
        con.close()
        t2 = Timetable(str(copy))
        check("aménagement échu : plus en vigueur", [x.id for x in t2.adjustments("BUS_351", date(2026, 6, 24))], [])
        check("aménagement échu : en vigueur la veille (borne incluse)",
              [x.id for x in t2.adjustments("BUS_351", date(2026, 6, 23))], ["bus-351-porte-de-montreuil"])
        b = t2.board("BUS_351", ["IDFM:28370"], at("2026-06-24T10:00"))
        check("aménagement échu : le GTFS reprend la main à Erignac", (b.status, len(b.passages)), ("ok", 4))
        t2.con.close()


# --------------------------------------------------------------------------- 2 ter. renommage des lieux
def place_names(tt: Timetable) -> None:
    """R-35 : noms des terminaux de CDG au 16 mars 2027, appliqués dès la première version (D-11)."""
    check("noms de lieux : Terminal 2E devenu Terminal 5, ancien nom en second", tt.place_label("cdg-t2e"),
          {"label_fr": "Terminal 5", "label_en": "Terminal 5", "short_label": "T5",
           "secondary_fr": "ex-Terminal 2E", "secondary_en": "formerly Terminal 2E"})
    check("noms de lieux : Terminal 3 devenu Terminal 2", tt.place_label("cdg-t3"),
          {"label_fr": "Terminal 2 - Roissypôle", "label_en": "Terminal 2 - Roissypôle", "short_label": "T2",
           "secondary_fr": "ex-Terminal 3", "secondary_en": "formerly Terminal 3"})
    check("noms de lieux : ancien ensemble du Terminal 2", tt.place_label("cdg-t2")["label_fr"], "Terminaux 3 à 7")
    check("noms de lieux : lieu jamais renommé, sans mention secondaire", tt.place_label("orly-4"),
          {"label_fr": "Orly 4", "label_en": "Orly 4", "short_label": "Orly 4", "secondary_fr": None, "secondary_en": None})
    shorts = [r[0] for r in tt.con.execute("SELECT short_label FROM place")]
    check("noms de lieux : libellés courts uniques", len(shorts) == len(set(shorts)), True)
    try:
        tt.place_label("lieu-inexistant")
        check("noms de lieux : lieu inconnu refusé", "aucune erreur", "KeyError")
    except KeyError:
        check("noms de lieux : lieu inconnu refusé", "KeyError", "KeyError")


# --------------------------------------------------------------------------- 3. performance
def performance(tt: Timetable) -> dict:
    busiest = tt.con.execute(
        """SELECT l.code, sp.gtfs_id, COUNT(*) n FROM pattern_stop ps JOIN trip t ON t.pattern_id = ps.pattern_id
           JOIN pattern p ON p.id = ps.pattern_id JOIN line l ON l.id = p.line_id
           JOIN stop_point sp ON sp.id = ps.stop_point_id GROUP BY l.code, sp.gtfs_id ORDER BY n DESC LIMIT 5""").fetchall()
    now = datetime.combine(tt.valid_start, datetime.min.time()).replace(hour=8, tzinfo=PARIS)
    out = {}
    for code, sp, n in busiest:
        samples = []
        for _ in range(20):
            t0 = time.perf_counter()
            tt.board(code, [sp], now)
            samples.append((time.perf_counter() - t0) * 1000)
        out[f"{code} {sp} ({n} passages)"] = round(statistics.median(samples), 2)
    return out


def main() -> int:
    src = Path(sys.argv[1])
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "timetable.sqlite"
        if src.suffix == ".gz":
            with gzip.open(src, "rb") as fi, open(db, "wb") as fo:
                shutil.copyfileobj(fi, fo)
        else:
            shutil.copyfile(src, db)
        tt = Timetable(str(db))
        completeness(tt)
        reading_rules(tt)
        adjustments_structure(tt)
        adjustments_351(tt, db)
        place_names(tt)
        perf = performance(tt)
        tt.con.close()
    for f in failures:
        print("ÉCHEC", f)
    print(f"{passed} contrôles conformes, {len(failures)} échec(s)")
    print("Médiane « prochains passages » (ms, Python + SQLite) :", json.dumps(perf, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
