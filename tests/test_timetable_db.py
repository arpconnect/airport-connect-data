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
   Accueil (R-100, décision D-13) : départs d'un lieu, contrôles de structure sur toute base,
   scénarios réels sur la base de juin 2026 (Paris, Noctilien, terminal de CDG), conservation R-55.
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
from datetime import date, datetime, timedelta
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


def _fmt_groups(groups):
    return [(g.line, g.key, g.title, g.board.status, g.board.boundary,
             [datetime.fromtimestamp(x.epoch, PARIS).strftime("%H:%M:%S") for x in g.board.passages]) for g in groups]


def home_structure(tt: Timetable, db_path: Path) -> None:
    """Accueil (R-100, D-13) : invariants valables sur toute base."""
    places = tt.places()
    check("accueil : Paris en tête du sélecteur, lieu ville sans secteur",
          (places[0]["id"], places[0]["kind"], places[0]["sector"]), ("paris", "city", None))
    check("accueil : tout lieu aéroportuaire a un secteur",
          [p["id"] for p in places if p["kind"] == "airport" and not p["sector"]], [])
    start = date.fromisoformat(tt.meta["validity_start"])
    now = datetime(start.year, start.month, start.day, 8, 0, tzinfo=PARIS) if start.weekday() < 5 else \
        datetime(start.year, start.month, start.day, 10, 0, tzinfo=PARIS)
    groups = tt.place_departures("paris", now)
    check("accueil Paris : une carte au moins par aéroport", sorted({g.sector for g in groups}),
          sorted(["ROISSY", "ORLY", "BOURGET", "BEAUVAIS"]))
    # chaque passage d'une carte Paris → secteur S part du point de montée et dessert ensuite un quai de S
    bad = []
    for g in groups:
        for x in g.board.passages:
            ok = tt.con.execute(
                """SELECT 1 FROM pattern_stop b JOIN stop_point bp ON bp.id = b.stop_point_id
                   JOIN stop_area a ON a.id = bp.stop_area_id
                   JOIN pattern_stop d ON d.pattern_id = b.pattern_id AND d.seq > b.seq AND d.dropoff != 1
                   JOIN place_stop_point ps ON ps.stop_point_id = d.stop_point_id
                   JOIN place pl ON pl.id = ps.place_id AND pl.sector = ?
                   WHERE b.pattern_id = ? AND b.seq = ? AND b.pickup != 1 AND a.gtfs_id = ? LIMIT 1""",
                (g.sector, x.pattern_id, x.seq, g.stop_area)).fetchone()
            if not ok:
                bad.append((g.line, g.sector, x.stop_point))
    check("accueil Paris : chaque départ mène à un aéroport du secteur de sa carte", bad, [])
    order = [(g.sector, g.board.passages[0].epoch if g.board.passages else None) for g in groups]
    sorted_ok = all(
        (["ROISSY", "ORLY", "BOURGET", "BEAUVAIS"].index(a[0]), a[1] is None, a[1] or 0) <=
        (["ROISSY", "ORLY", "BOURGET", "BEAUVAIS"].index(b[0]), b[1] is None, b[1] or 0) for a, b in zip(order, order[1:]))
    check("accueil Paris : cartes groupées par aéroport puis triées par prochain départ", sorted_ok, True)
    for pid in ("cdg-t2", "orly-4"):
        gs = tt.place_departures(pid, now)
        firsts = [g.board.passages[0].epoch for g in gs if g.board.passages]
        check(f"accueil {pid} : cartes avec départ d'abord, par prochain départ",
              firsts == sorted(firsts) and all(g.board.passages for g in gs[:len(firsts)]), True)
        check(f"accueil {pid} : une carte par ligne et par sens", len({(g.line, g.key) for g in gs}), len(gs))
    try:
        tt.place_departures("lieu-inexistant", now)
        check("accueil : lieu inconnu refusé", "aucune erreur", "KeyError")
    except KeyError:
        check("accueil : lieu inconnu refusé", "KeyError", "KeyError")
    # point de montée que la ligne ne dessert plus vers un aéroport : cartes « Horaires indisponibles » (R-153)
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "t.sqlite"
        shutil.copyfile(db_path, copy)
        con = sqlite3.connect(copy)
        other = con.execute("""SELECT pb2.stop_area_id FROM place_boarding pb2 JOIN line l2 ON l2.id = pb2.line_id
                               WHERE l2.code = 'METRO_14'""").fetchone()[0]
        con.execute("""UPDATE place_boarding SET stop_area_id = ?
                       WHERE line_id = (SELECT id FROM line WHERE code = 'BUS_351')""", (other,))
        con.commit()
        con.close()
        t2 = Timetable(str(copy))
        g351 = [(g.line, g.key, g.title, g.board.status) for g in t2.place_departures("paris", now) if g.line == "BUS_351"]
        check("accueil Paris : point de montée plus desservi → « Horaires indisponibles » par secteur de la ligne",
              g351, [("BUS_351", "sector:ROISSY", "", "no_data")])
        t2.con.close()


def home_june(tt: Timetable) -> None:
    """Accueil : scénarios réels du GTFS de juin 2026 (mercredi 1er juillet 2026)."""
    snap = json.loads((CASES / "snapshot.json").read_text(encoding="utf-8"))
    if snap["source_zip_sha256"] != tt.meta["source_sha256"]:
        print("(scénarios d'accueil de juin ignorés : la base ne provient pas du GTFS de référence)")
        return
    cdg2 = "Aéroport Charles de Gaulle 2 (Terminal 2)"
    got = _fmt_groups(tt.place_departures("paris", at("2026-07-01T08:00")))
    check("accueil Paris 08:00 : cartes, destinations et premiers départs", [(l, k, t, s, b, x[:2]) for l, k, t, s, b, x in got], [
        ("RER_B", "sector:ROISSY", cdg2, "ok", None, ["08:00:00", "08:06:00"]),
        ("BUS_351", "sector:ROISSY", "Roissypôle", "ok", None, ["08:05:00", "08:33:00"]),
        ("BUS_350", "sector:ROISSY", "Roissypôle", "ok", None, ["08:12:00", "08:27:00"]),
        ("BUS_N140", "sector:ROISSY", "Gare de Roissypole - Aéroport CDG 1", "ok", "service_ended", []),
        ("BUS_N143", "sector:ROISSY", "Gare de Roissypole - Aéroport CDG 1 (B2)", "ok", "service_ended", []),
        ("METRO_14", "sector:ORLY", "Aéroport d'Orly", "ok", None, ["08:00:28", "08:02:08"]),
        ("BUS_N131", "sector:ORLY", "Gare de Brétigny Place", "ok", "service_ended", []),
        ("BUS_N139", "sector:ORLY", "Gare de Corbeil-Essonnes Henri Barbusse", "ok", "service_ended", []),
        ("BUS_N22", "sector:ORLY", "Juvisy RER", "ok", "service_ended", []),
        ("BUS_N31", "sector:ORLY", "Aéroport Orly 4", "ok", "service_ended", []),
        ("RER_B", "sector:BOURGET", cdg2, "ok", None, ["08:00:00", "08:03:00"]),
        ("BUS_152", "sector:BOURGET", "ZAC Les Tulipes Nord", "ok", None, ["08:10:00", "08:20:00"]),
        ("BUS_350", "sector:BOURGET", "Roissypôle", "ok", None, ["08:12:00", "08:27:00"]),
        ("BUS_N42", "sector:BOURGET", "Garonor", "ok", "service_ended", []),
        ("BUS_A01", "sector:BEAUVAIS", "Aéroport Paris Beauvais", "ok", None, ["08:00:00", "08:30:00"]),
        ("BUS_A04", "sector:BEAUVAIS", "Aéroport Paris Beauvais", "ok", None, ["09:00:00", "11:00:00"]),
    ])
    # RER B depuis la gare du Nord : la carte Roissy ne garde que les trains de l'aéroport ; la carte Le Bourget
    # garde tous les trains qui s'y arrêtent (aéroport, Mitry-Claye, Aulnay-sous-Bois) et écarte les autres (R-43)
    rer = {k: x for l, k, t, s, b, x in got if l == "RER_B"}
    dests = {k: sorted({tt.terminus(p.pattern_id) for p in g.board.passages}) for g in tt.place_departures(
        "paris", at("2026-07-01T08:00")) if g.line == "RER_B" for k in [g.key]}
    check("accueil Paris : RER B vers Roissy, trains de l'aéroport seulement", dests["sector:ROISSY"], [cdg2])
    check("accueil Paris : RER B vers Le Bourget, trains de l'aéroport, de Mitry-Claye et d'Aulnay-sous-Bois",
          dests["sector:BOURGET"], sorted([cdg2, "Mitry - Claye", "Aulnay-sous-Bois"]))
    check("accueil Paris : RER B, quatre départs vers chaque aéroport",
          (rer["sector:ROISSY"], rer["sector:BOURGET"]),
          (["08:00:00", "08:06:00", "08:12:00", "08:18:00"], ["08:00:00", "08:03:00", "08:06:00", "08:09:00"]))
    # Noctilien : depuis Paris, les départs de la carte suivent le cas réel de l'étape 1 (spec R-70)
    noct = json.loads((CASES / "noctilien_boarding.json").read_text(encoding="utf-8"))
    night = {g.line: [datetime.fromtimestamp(x.epoch, PARIS).strftime("%H:%M") for x in g.board.passages]
             for g in tt.place_departures("paris", at("2026-07-02T01:30")) if g.line in noct["lines"]}
    for line, v in noct["lines"].items():
        expected = [t for t in v["nights"]["semaine"]["gtfs"] if t >= "01:30"][:4]
        check(f"accueil Paris 01:30 : {line} comme la grille GTFS de l'étape 1", night.get(line), expected)
    # Terminal 5 (ex-2E) : la N2 ne le dessert que le soir (R-61) ; la règle v1 dit « Service terminé » (D-6)
    check("accueil Terminal 5 à 08:00", _fmt_groups(tt.place_departures("cdg-t2e", at("2026-07-01T08:00"))), [
        ("BUS_N1", "direction:1", "Terminal 2 F", "ok", None, ["08:07:00", "08:15:00", "08:23:00", "08:31:00"]),
        ("BUS_N2", "direction:0", "Terminal 2 F", "ok", "service_ended", []),
        ("BUS_N2", "direction:1", "Terminal 2 G", "ok", "service_ended", []),
    ])
    # R-55 : un passage théorique reste affiché jusqu'à 59 s après son heure (« 0 min »), puis disparaît
    def first(iso):
        g = [g for g in tt.place_departures("paris", at(iso)) if g.key == "sector:ROISSY" and g.line == "RER_B"][0]
        return datetime.fromtimestamp(g.board.passages[0].epoch, PARIS).strftime("%H:%M:%S")
    check("R-55 : départ de 08:00:00 encore affiché à 08:00:59", first("2026-07-01T08:00:59"), "08:00:00")
    check("R-55 : départ de 08:00:00 retiré à 08:01:00", first("2026-07-01T08:01:00"), "08:06:00")


def home_destination(tt: Timetable) -> None:
    """Destination (R-102, D-14) : invariants sur toute base, puis scénarios réels de juin 2026."""
    start = date.fromisoformat(tt.meta["validity_start"])
    day = start + timedelta(days=1)              # lendemain du premier jour : la veille est couverte (R-153)
    now = datetime(day.year, day.month, day.day, 10, 0, tzinfo=PARIS)
    cases = [("paris", ("airport", "ORLY")), ("paris", ("airport", "ROISSY")), ("paris", ("place", "bourget-gare")),
             ("cdg-t2-gare", ("place", "paris")), ("orly-4", ("place", "paris")), ("cdg-t1", ("place", "cdg-t2"))]
    bad = []
    for origin, dest in cases:
        targets = tt.destination_stop_points(dest)
        for g in tt.place_departures(origin, now, dest):
            for x in g.board.passages:
                if not tt.reaches(x.pattern_id, x.seq, targets):
                    bad.append((origin, dest, g.line, x.stop_point))
            if origin == "paris" and (g.key, g.sector) != ("destination", None):
                bad.append((origin, dest, g.line, g.key))
    check("destination : chaque départ affiché dessert ensuite la destination (descente autorisée)", bad, [])
    full = {(g.line, g.key) for g in tt.place_departures("cdg-t2-gare", now)}
    sub = {(g.line, g.key) for g in tt.place_departures("cdg-t2-gare", now, ("place", "paris"))}
    check("destination : sous-ensemble des cartes sans destination", sub <= full, True)
    for bad_dest in (("place", "lieu-inexistant"), ("stop_area", "IDFM:0"), ("airport", "LUNE")):
        try:
            tt.place_departures("paris", now, bad_dest)
            check(f"destination inconnue refusée {bad_dest}", "aucune erreur", "KeyError")
        except KeyError:
            check(f"destination inconnue refusée {bad_dest}", "KeyError", "KeyError")

    snap = json.loads((CASES / "snapshot.json").read_text(encoding="utf-8"))
    if snap["source_zip_sha256"] != tt.meta["source_sha256"]:
        print("(scénarios de destination de juin ignorés : la base ne provient pas du GTFS de référence)")
        return
    at8 = at("2026-07-01T08:00")
    lines = lambda o, d: [(g.line, g.stop_area) for g in tt.place_departures(o, at8, d)]  # noqa: E731
    check("Paris → aéroport d'Orly : métro 14 depuis la gare de Lyon et les Noctilien",
          lines("paris", ("airport", "ORLY")),
          [("METRO_14", "IDFM:73626"), ("BUS_N131", "IDFM:73626"), ("BUS_N139", "IDFM:73626"), ("BUS_N22", "IDFM:71264"),
           ("BUS_N31", "IDFM:73626")])
    check("Paris → Orly 4 : le métro 14 s'arrête aux terminaux 1-2-3 (zone IDFM:63284), pas à Orly 4",
          "METRO_14" in [l for l, _ in lines("paris", ("place", "orly-4"))], False)
    dests = sorted({tt.terminus(x.pattern_id) for g in tt.place_departures("paris", at8, ("place", "bourget-gare"))
                    if g.line == "RER_B" for x in g.board.passages})
    check("Paris → gare du Bourget : une seule carte RER B, trains qui s'y arrêtent (R-43)",
          ([l for l, _ in lines("paris", ("place", "bourget-gare"))].count("RER_B"), dests),
          (1, sorted(["Aéroport Charles de Gaulle 2 (Terminal 2)", "Mitry - Claye", "Aulnay-sous-Bois"])))
    check("Terminal 5 → Terminal 1 : aucune ligne directe (correspondance à la gare)",
          tt.place_departures("cdg-t2e", at8, ("place", "cdg-t1")), [])


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
        home_structure(tt, db)
        home_june(tt)
        home_destination(tt)
        perf = performance(tt)
        tt.con.close()
    for f in failures:
        print("ÉCHEC", f)
    print(f"{passed} contrôles conformes, {len(failures)} échec(s)")
    print("Médiane « prochains passages » (ms, Python + SQLite) :", json.dumps(perf, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
