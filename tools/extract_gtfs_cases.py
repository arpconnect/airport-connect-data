#!/usr/bin/env python3
"""
Extrait les cas de caractérisation « terrain » d'un GTFS IDFM.

Usage :
    python3 tools/extract_gtfs_cases.py IDFM-gtfs.zip --out tests/characterization/gtfs-2026-06
    python3 tools/extract_gtfs_cases.py IDFM-gtfs.zip --check tests/characterization/gtfs-2026-06

--out    écrit les fichiers JSON (écrase).
--check  recalcule et compare aux fichiers existants ; code de sortie 1 au premier écart.

Les lignes suivies et leur code C viennent de data/lines.json (aucune liste recopiée ici).
Stdlib uniquement, Python 3.9+. Lecture en flux du zip (≈ 1 min sur le GTFS IDFM complet).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "reference"))
from v1_rules import v1_name_match  # noqa: E402

WEEKDAY_REF = "2026-07-01"       # mercredi, dans la fenêtre de validité du snapshot, hors changement d'heure
FRIDAY_NIGHT_REF = "2026-07-03"  # nuit de vendredi à samedi (grille « week-end » du Noctilien)

# Grilles Noctilien codées en dur dans la v1 (ui/noctilien/NoctilienTimetableRegistry.kt).
# Listes « semaine » et « week-end » explicites ; les branches à cadence générée sont recalculées
# exactement comme buildCadencedExplicitDepartures(first, last, cadence).
V1_NOCTILIEN = {
    "BUS_N22": {"board": "Châtelet",
                "week": "00:25 00:45 01:05 01:25 01:45 02:05 02:25 02:45 03:05 03:25 03:45 04:05 04:25 04:45 05:15 05:35 05:55",
                "weekend": "00:25 00:45 01:05 01:25 01:45 02:05 02:25 02:45 03:05 03:25 03:45 04:05 04:25 04:45 05:05 05:25 05:45"},
    "BUS_N31": {"board": "Gare de Lyon - Van Gogh", "cadence": ("00:30", "05:30", 20)},
    "BUS_N131": {"board": "Gare de Lyon - Van Gogh", "week": "01:35 02:35 03:35 04:35", "weekend": "01:35 02:35 03:35 04:35"},
    "BUS_N139": {"board": "Gare de Lyon - Diderot", "cadence": ("01:35", "05:35", 30)},
    "BUS_N140": {"board": "Gare de l'Est", "week": "01:00 01:30 02:00 03:00 03:40", "weekend": "01:00 01:30 02:00 03:00 03:40"},
    "BUS_N143": {"board": "Gare de l'Est",
                 "week": "00:55 01:25 01:55 02:10 02:25 02:55 03:25 03:55 04:10 04:25 04:45 05:08",
                 "weekend": "00:55 01:25 01:55 02:10 02:25 02:55 03:25 03:55 04:10 04:25 04:45 05:08"},
    "BUS_N42": {"board": "Gare de l'Est", "cadence": ("00:15", "05:55", 15)},
}
# Indices de pôle aéroport dans les noms d'arrêts GTFS. « cdg » et « aéroport » sont indispensables :
# l'arrêt « Aéroport CDG T2 » des N140/N143 ne contient ni « gaulle » ni « terminal » ni « roissy ».
AIRPORT_HINTS = ("orly", "gaulle", "roissy", "terminal", "cdg", "aéroport", "aeroport", "bourget", "drancy", "garonor")
PROFILE_LINES = ["CDGVAL", "ORLYVAL", "BUS_N1", "BUS_N2", "BUS_A01", "BUS_A04"]

# Manuels v1 (data/gtfs/IdfmGtfsLineRegistry.kt → manualCandidates) pour mesurer la pollution.
V1_CANDIDATES_OVERRIDE = {
    "RER_B": ["B", "RER B"], "RER_C": ["C", "RER C"], "METRO_14": ["14", "M14", "METRO 14", "METRO14"],
    "CDGVAL": ["CDGVAL", "CDG VAL"], "ORLYVAL": ["ORLYVAL", "ORLY VAL"],
    "BUS_9517": ["9517", "EXPRESS 9517"], "BUS_9509": ["9509", "EXPRESS 9509"],
    "BUS_EX100": ["EX100", "EX 100"], "BUS_EX19": ["EX19", "EX 19"], "BUS_EX93": ["EX93", "EX 93"],
    "BUS_TBUS1": ["TBUS1", "TBUS 1", "T BUS 1"], "BUS_A01": ["A01", "A 01"], "BUS_A04": ["A04", "A 04"],
    "T7": ["T7", "T 7"], "T9": ["T9", "T 9"], "T11": ["T11", "T 11"],
}
_EXCLUDED_MODES = {"RER": {"BUS", "TRAM"}, "METRO": {"BUS", "TRAM"}, "TRAM": {"BUS"}, "BUS": {"METRO", "TRAM"}}


def _route_mode(route_type: str) -> str:
    try:
        n = int(route_type)
    except ValueError:
        return "OTHER"
    if n == 0 or 900 <= n <= 999:
        return "TRAM"
    if n == 1 or 400 <= n <= 499:
        return "METRO"
    if n == 2 or 100 <= n <= 199:
        return "RAIL"
    if n == 3 or 200 <= n <= 299 or 700 <= n <= 799:
        return "BUS"
    return "OTHER"


def _family(line_id: str):
    if line_id.startswith("RER_"):
        return "RER"
    if line_id.startswith("METRO_"):
        return "METRO"
    if line_id in ("T7", "T9", "T11"):
        return "TRAM"
    if line_id.startswith("BUS_"):
        return "BUS"
    return None


def v1_candidates(line_id: str) -> list:
    if line_id in V1_CANDIDATES_OVERRIDE:
        return V1_CANDIDATES_OVERRIDE[line_id]
    for prefix in ("BUS_", "RER_", "METRO_"):
        if line_id.startswith(prefix):
            return [line_id[len(prefix):].replace("_", " ")]
    return [line_id]


def hm(seconds: int) -> str:
    seconds %= 86400
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}"


def cadence(first: str, last: str, step: int) -> list:
    def m(s):
        h, mi = s.split(":")
        return int(h) * 60 + int(mi)
    a, b = m(first), m(last)
    if b < a:
        b += 1440
    out, cur = [], a
    while cur <= b:
        out.append(f"{(cur % 1440) // 60:02d}:{cur % 60:02d}")
        cur += step
    if out[-1] != last:
        out.append(last)
    return list(dict.fromkeys(out))


class Feed:
    def __init__(self, zip_path: Path, lines: list):
        self.zip_path = zip_path
        self.z = zipfile.ZipFile(zip_path)
        self.lines = {l["id"]: l for l in lines}
        routes = list(self._rows("routes.txt"))
        self.routes = {r["route_id"]: r for r in routes}
        self.route_to_line = {l["idfm_route_id"]: lid for lid, l in self.lines.items() if l.get("idfm_route_id")}
        # Routes captées à tort par le matching v1 (mesure de pollution).
        self.v1_extra = {}
        for lid, l in self.lines.items():
            if not l.get("idfm_route_id"):
                continue
            fam = _family(lid)
            for r in routes:
                if r["route_id"] == l["idfm_route_id"]:
                    continue
                if fam and _route_mode(r["route_type"]) in _EXCLUDED_MODES[fam]:
                    continue
                if v1_name_match(v1_candidates(lid), r["route_short_name"], r["route_long_name"]):
                    self.v1_extra.setdefault(lid, []).append(r["route_id"])
        wanted = set(self.route_to_line) | {rid for rids in self.v1_extra.values() for rid in rids}
        self.trips = {}
        for t in self._rows("trips.txt"):
            if t["route_id"] in wanted:
                self.trips[t["trip_id"]] = t
        self.stop_times = defaultdict(list)
        for s in self._rows("stop_times.txt"):
            if s["trip_id"] in self.trips:
                self.stop_times[s["trip_id"]].append((
                    int(s["stop_sequence"]), s["stop_id"], _sec(s["departure_time"]),
                    s.get("pickup_type", "") or "0", s.get("drop_off_type", "") or "0"))
        for v in self.stop_times.values():
            v.sort()
        self.stops = {s["stop_id"]: s["stop_name"] for s in self._rows("stops.txt")}
        services = {t["service_id"] for t in self.trips.values()}
        self.cal = {c["service_id"]: c for c in self._rows("calendar.txt") if c["service_id"] in services}
        self.cald = defaultdict(dict)
        for c in self._rows("calendar_dates.txt"):
            if c["service_id"] in services:
                self.cald[c["date"]][c["service_id"]] = c["exception_type"]

    def _rows(self, name):
        with self.z.open(name) as f:
            yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))

    def active(self, iso: str) -> set:
        d = date.fromisoformat(iso)
        key = d.strftime("%Y%m%d")
        dow = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][d.weekday()]
        out = {sid for sid, c in self.cal.items() if c[dow] == "1" and c["start_date"] <= key <= c["end_date"]}
        for sid, typ in self.cald.get(key, {}).items():
            (out.add if typ == "1" else out.discard)(sid)
        return out

    def line_trips(self, line_id: str, services=None):
        rid = self.lines[line_id]["idfm_route_id"]
        for tid, t in self.trips.items():
            if t["route_id"] == rid and (services is None or t["service_id"] in services):
                yield tid, t

    def names(self, tid):
        return [self.stops.get(sid, sid) for _, sid, _, _, _ in self.stop_times[tid]]


def _sec(value: str):
    if not value:
        return None
    h, m, s = value.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build(feed: Feed) -> dict:
    files = {}
    # 1. Méta + couverture
    starts = [c["start_date"] for c in feed.cal.values()]
    ends = [c["end_date"] for c in feed.cal.values()]
    coverage = {}
    for lid, l in sorted(feed.lines.items()):
        if not l.get("idfm_route_id"):
            coverage[lid] = {"route_id": None, "trips": 0, "stop_times": 0}
            continue
        tids = [tid for tid, _ in feed.line_trips(lid)]
        coverage[lid] = {"route_id": l["idfm_route_id"], "trips": len(tids),
                         "stop_times": sum(len(feed.stop_times[t]) for t in tids)}
    files["snapshot.json"] = {
        "schema": "airport-connect/gtfs-snapshot@1",
        "source_zip_sha256": sha256(feed.zip_path),
        "calendar_window": {"min_start": min(starts), "max_end": max(ends)},
        "reference_dates": {"weekday": WEEKDAY_REF, "friday_night": FRIDAY_NIGHT_REF},
        "coverage": coverage,
        "lines_without_route": sorted(lid for lid, c in coverage.items() if c["route_id"] is None),
    }

    # 2. Pollution du matching par libellé v1
    pollution = []
    for lid, extra in sorted(feed.v1_extra.items()):
        own = sum(1 for _ in feed.line_trips(lid))
        others = []
        for rid in extra:
            r = feed.routes[rid]
            n = sum(1 for t in feed.trips.values() if t["route_id"] == rid)
            others.append({"route_id": rid, "short_name": r["route_short_name"], "long_name": r["route_long_name"], "trips": n})
        total = own + sum(o["trips"] for o in others)
        pollution.append({"line": lid, "own_route_id": feed.lines[lid]["idfm_route_id"], "own_trips": own,
                          "homonym_routes": others, "share_of_wrong_trips": round(1 - own / total, 3) if total else 0})
    files["name_matching_pollution.json"] = {
        "schema": "airport-connect/characterization@1",
        "rule": "Le matching par libellé de la v1 agrège des lignes homonymes ; la v2 identifie par code C",
        "cases": pollution,
    }

    # 3. Profils journaliers (navettes)
    services = feed.active(WEEKDAY_REF)
    profiles = {}
    for lid in PROFILE_LINES:
        trips = list(feed.line_trips(lid, services))
        by_dir = defaultdict(list)
        for tid, t in trips:
            by_dir[t.get("direction_id", "")].append(tid)
        dirs = {}
        for d, tids in sorted(by_dir.items()):
            longest = max(tids, key=lambda x: (len(feed.stop_times[x]), x))
            starts_ = sorted(feed.stop_times[t][0][2] for t in tids)
            gaps = sorted(b - a for a, b in zip(starts_, starts_[1:]))
            heads = sorted({feed.trips[t].get("trip_headsign", "") for t in tids})
            dirs[d or "-"] = {
                "stops_longest_trip": feed.names(longest),
                "headsigns": heads,
                "departures_from_origin": len(starts_),
                "first": hm(starts_[0]), "last": hm(starts_[-1]),
                "headway_minutes": {"min": gaps[0] // 60, "median": gaps[len(gaps) // 2] // 60, "max": gaps[-1] // 60} if gaps else None,
                "duplicate_departures": sum(1 for g in gaps if g == 0),
            }
        loop = sorted({(feed.stop_times[t][-1][2] - feed.stop_times[t][0][2]) // 60 for t, _ in trips})
        profiles[lid] = {"trips_on_reference_day": len(trips), "trip_duration_minutes": {"min": loop[0], "max": loop[-1]} if loop else None,
                         "directions": dirs}
    # Règle N2 / Terminal 2E : heures où l'arrêt est desservi
    hours = Counter()
    for tid, _ in feed.line_trips("BUS_N2", services):
        for _, sid, dep, _, _ in feed.stop_times[tid]:
            if feed.stops.get(sid) == "Terminal 2E":
                hours[f"{(dep // 3600) % 24:02d}h"] += 1
    profiles["BUS_N2"]["terminal_2E_calls_by_hour"] = dict(sorted(hours.items()))
    files["shuttle_profiles.json"] = {
        "schema": "airport-connect/characterization@1",
        "rule": "Profil réel d'une journée de semaine pour les navettes aéroportuaires",
        "reference_date": WEEKDAY_REF,
        "lines": profiles,
    }

    # 4. Noctilien : départs vers l'aéroport depuis l'arrêt de montée, comparés aux grilles v1
    noct = {}
    for lid, spec in V1_NOCTILIEN.items():
        noct[lid] = {"boarding_stop": spec["board"], "nights": {}}
        airport_stops = set()
        for label, ref, key in (("semaine", WEEKDAY_REF, "week"), ("vendredi", FRIDAY_NIGHT_REF, "weekend")):
            svc = feed.active(ref)
            deps = set()
            for tid, _ in feed.line_trips(lid, svc):
                st = feed.stop_times[tid]
                names = [feed.stops.get(x[1], x[1]) for x in st]
                for i, (seq, sid, dep, pickup, _) in enumerate(st):
                    if names[i] != spec["board"] or pickup == "1":
                        continue
                    after = [n for n in names[i + 1:] if any(h in n.lower() for h in AIRPORT_HINTS)]
                    if after:
                        deps.add(dep)
                        airport_stops.update(after)
            gtfs = [hm(d) for d in sorted(deps)]
            v1 = spec[key].split() if key in spec else cadence(*spec["cadence"])
            noct[lid]["nights"][label] = {"service_date": ref, "gtfs": gtfs, "v1_hardcoded": v1, "identical": gtfs == v1}
        noct[lid]["airport_stops_served"] = sorted(airport_stops)
    files["noctilien_boarding.json"] = {
        "schema": "airport-connect/characterization@1",
        "rule": "Départs Noctilien vers le pôle aéroport depuis l'arrêt de montée parisien (GTFS) vs grilles codées en dur de la v1",
        "lines": noct,
    }

    # 5. RER B : trains nord au départ de Châtelet et desserte de la gare du Bourget
    counts = Counter()
    for tid, _ in feed.line_trips("RER_B", services):
        names = feed.names(tid)
        idx = next((i for i, n in enumerate(names) if n == "Châtelet - Les Halles"), None)
        if idx is None or idx == len(names) - 1:
            continue
        after = names[idx + 1:]
        if not any(("Gaulle" in n) or ("Mitry" in n) for n in after):
            continue
        counts[(names[-1], "Le Bourget" in after)] += 1
    files["rer_b_le_bourget.json"] = {
        "schema": "airport-connect/characterization@1",
        "rule": "« Paris → Le Bourget » ne doit retenir que les trains qui desservent réellement la gare du Bourget",
        "reference_date": WEEKDAY_REF,
        "origin": "Châtelet - Les Halles",
        "trains_northbound": [{"terminus": t, "calls_at_le_bourget": b, "count": n} for (t, b), n in sorted(counts.items())],
    }
    return files


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("gtfs_zip", type=Path)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--out", type=Path)
    g.add_argument("--check", type=Path)
    args = ap.parse_args()
    lines = json.loads((ROOT / "data" / "lines.json").read_text(encoding="utf-8"))["lines"]
    files = build(Feed(args.gtfs_zip, lines))
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            (args.out / name).write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print("écrit", args.out / name)
        return 0
    bad = 0
    for name, content in files.items():
        path = args.check / name
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        if current != content:
            print("ÉCART :", name)
            bad += 1
        else:
            print("OK    :", name)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
