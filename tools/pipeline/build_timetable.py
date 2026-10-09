#!/usr/bin/env python3
"""
Générateur unique de la base d'horaires Airport Connect (spec § 17, R-170 / R-171).

    python3 tools/pipeline/build_timetable.py IDFM-gtfs.zip --out dist/
        [--previous-manifest dist-prev/manifest.json] [--today AAAA-MM-JJ]
        [--base-url 'https://github.com/OWNER/REPO/releases/download/timetable-{version}']

Produit dans --out :
    timetable-<version>.sqlite.gz   base SQLite (schéma schema/timetable.sql), compressée gzip
    manifest.json                   description, empreintes, fenêtre de validité, compteurs
    report.json                     résultat des contrôles (toujours écrit, même en cas d'échec)

Code de sortie : 0 si tous les contrôles bloquants passent, 2 sinon (rien n'est alors à publier).
La sortie est déterministe : même GTFS, mêmes fichiers data/ et même jour de construction → mêmes octets de base.
Stdlib uniquement, Python 3.9+.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import os
import sqlite3
import sys
import tempfile
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "schema" / "timetable.sql"
SCHEMA_VERSION = 3
MAX_WINDOW_DAYS = 62            # bits utilisables dans un INTEGER SQLite signé (63) avec marge
MIN_REMAINING_DAYS_WARN = 14    # alerte si la validité restante est plus courte
MAX_TRIP_LOSS_RATIO = 0.5       # une ligne qui perd plus de la moitié de ses trajets bloque la publication
SOURCE_URL = "https://eu.ftp.opendatasoft.com/stif/GTFS/IDFM-gtfs.zip"
TIMEZONE = "Europe/Paris"


# --------------------------------------------------------------------------- lecture GTFS
def _rows(z: zipfile.ZipFile, name: str):
    with z.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))


def _sec(value: str) -> int:
    h, m, s = value.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def _d(s: str) -> date:
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Report:
    def __init__(self) -> None:
        self.errors: list = []
        self.warnings: list = []

    def error(self, code: str, detail) -> None:
        self.errors.append({"code": code, "detail": detail})

    def warn(self, code: str, detail) -> None:
        self.warnings.append({"code": code, "detail": detail})


# --------------------------------------------------------------------------- construction
def load_inputs(data_dir: Path):
    lines = json.loads((data_dir / "lines.json").read_text(encoding="utf-8"))
    places = json.loads((data_dir / "places.json").read_text(encoding="utf-8"))
    adjustments = json.loads((data_dir / "adjustments.json").read_text(encoding="utf-8"))
    return lines, places, adjustments


def read_feed(zip_path: Path, lines_doc: dict, places_doc: dict, report: Report):
    z = zipfile.ZipFile(zip_path)
    active = [l for l in lines_doc["lines"] if l["state"] == "active"]
    route_to_code = {l["idfm_route_id"]: l["id"] for l in active}

    routes, agencies = {}, {a["agency_id"]: a["agency_name"] for a in _rows(z, "agency.txt")}
    for r in _rows(z, "routes.txt"):
        if r["route_id"] in route_to_code:
            routes[r["route_id"]] = r
    for rid, code in sorted(route_to_code.items(), key=lambda x: x[1]):
        if rid not in routes:
            report.error("route_missing", {"line": code, "route_id": rid})
    for l in active:
        r = routes.get(l["idfm_route_id"])
        if r and l.get("gtfs") and r["route_short_name"] != l["gtfs"]["short_name"]:
            report.warn("route_renamed", {"line": l["id"], "before": l["gtfs"]["short_name"], "now": r["route_short_name"]})

    trips = {}
    for t in _rows(z, "trips.txt"):
        if t["route_id"] in routes:
            trips[t["trip_id"]] = t

    stop_times = defaultdict(list)
    for s in _rows(z, "stop_times.txt"):
        if s["trip_id"] in trips:
            dep = _sec(s["departure_time"] or s["arrival_time"])
            arr = _sec(s["arrival_time"] or s["departure_time"])
            stop_times[s["trip_id"]].append((int(s["stop_sequence"]), s["stop_id"], arr, dep,
                                             int(s.get("pickup_type") or 0), int(s.get("drop_off_type") or 0)))
    for v in stop_times.values():
        v.sort()

    wanted_points = {st[1] for v in stop_times.values() for st in v}
    for p in places_doc["places"]:
        wanted_points.update(p.get("stop_points", []))
    stops_all = {}
    for s in _rows(z, "stops.txt"):
        stops_all[s["stop_id"]] = s

    services = {t["service_id"] for t in trips.values()}
    cal = {c["service_id"]: c for c in _rows(z, "calendar.txt") if c["service_id"] in services}
    cald = defaultdict(list)
    for c in _rows(z, "calendar_dates.txt"):
        if c["service_id"] in services:
            cald[c["service_id"]].append((c["date"], c["exception_type"]))
    return routes, agencies, trips, stop_times, stops_all, wanted_points, cal, cald


def active_dates(sid: str, cal: dict, cald: dict) -> set:
    out = set()
    c = cal.get(sid)
    if c:
        cur, end = _d(c["start_date"]), _d(c["end_date"])
        dows = [c[k] == "1" for k in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")]
        while cur <= end:
            if dows[cur.weekday()]:
                out.add(cur)
            cur += timedelta(days=1)
    for ds, typ in cald.get(sid, []):
        (out.add if typ == "1" else out.discard)(_d(ds))
    return out


def build_db(db_path: Path, zip_path: Path, lines_doc, places_doc, adjustments_doc, report: Report,
             today: date | None = None) -> dict:
    """Construit la base. [today] (jour de la construction, heure de Paris) borne le début de la validité :
    le GTFS IDFM publie aussi quelques jours passés, de façon partielle (constaté le 8 octobre 2026 : du 5 au
    7 octobre, seuls les trains SNCF y figurent). Ces jours restent dans les masques de service, mais la base
    ne les déclare pas valides : l'application n'en tire ni passage ni fin de service (R-153, R-173)."""
    routes, agencies, trips, stop_times, stops_all, wanted_points, cal, cald = read_feed(zip_path, lines_doc, places_doc, report)

    # Services → masques de jours
    svc_dates = {sid: active_dates(sid, cal, cald) for sid in sorted({t["service_id"] for t in trips.values()})}
    all_dates = sorted(set().union(*svc_dates.values())) if svc_dates else []
    if not all_dates:
        report.error("no_service_dates", "aucune date de service pour les lignes suivies")
        return {}
    base, last = all_dates[0], all_dates[-1]
    window = (last - base).days + 1
    if window > MAX_WINDOW_DAYS:
        report.error("window_too_long", {"days": window, "max": MAX_WINDOW_DAYS})
        return {}
    start = min(max(base, today), last) if today else base

    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    cur = con.cursor()
    meta = {
        "schema_version": str(SCHEMA_VERSION), "base_date": base.isoformat(),
        "validity_start": start.isoformat(), "validity_end": last.isoformat(),
        "source_sha256": sha256_file(zip_path), "source_url": SOURCE_URL, "timezone": TIMEZONE,
    }
    cur.executemany("INSERT INTO meta VALUES (?,?)", sorted(meta.items()))

    # Lignes (ordre déterministe : code)
    line_id = {}
    trips_line_codes = {l["id"] for l in lines_doc["lines"] if l["state"] == "active"}
    for i, l in enumerate(sorted(lines_doc["lines"], key=lambda x: x["id"]), start=1):
        line_id[l["id"]] = i
        r = routes.get(l["idfm_route_id"]) if l["idfm_route_id"] else None
        cur.execute("INSERT INTO line VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            i, l["id"], l["idfm_route_id"], l["display_name"],
            r["route_short_name"] if r else None, r["route_long_name"] if r else None,
            l["app_type"], l["category"], agencies.get(r["agency_id"]) if r else None,
            ("#" + r["route_color"].upper()) if r and r["route_color"] else None,
            ("#" + r["route_text_color"].upper()) if r and r["route_text_color"] else None,
            l["state"], l.get("group")))
        for sec in l["sectors"]:
            cur.execute("INSERT INTO line_sector VALUES (?,?,?,?)", (i, sec, l["sort_order"].get(sec), int(sec in l["shortcut_sectors"])))
        if l["state"] == "future":
            f = l["future"]
            cur.execute("INSERT INTO line_future VALUES (?,?,?,?,?)", (i, f["opening"], f.get("scope"), f["source"], lines_doc["verified"]["future_dates_checked_on"]))

    # Arrêts (zones puis quais), identifiants internes par tri des identifiants GTFS
    for sp in sorted(wanted_points):
        if sp not in stops_all:
            report.error("stop_missing", sp)
    areas_needed = set()
    for sp in wanted_points:
        s = stops_all.get(sp)
        if s:
            areas_needed.add(s["parent_station"] or sp)
    for p in places_doc["places"]:
        areas_needed.update(p.get("stop_areas", []))
        areas_needed.update(b["stop_area"] for b in p.get("boarding", []))
    published_adj = [a for a in adjustments_doc["adjustments"] if a["status"] == "published"]
    for a in published_adj:                      # zones de report : présentes dans la base si le GTFS les connaît
        for ns in a["not_served"]:
            if ns.get("replacement_stop_area") in stops_all:
                areas_needed.add(ns["replacement_stop_area"])
    area_id, point_id = {}, {}
    for i, a in enumerate(sorted(areas_needed), start=1):
        s = stops_all.get(a)
        if not s:
            report.error("stop_area_missing", a)
            continue
        area_id[a] = i
        cur.execute("INSERT INTO stop_area VALUES (?,?,?,?,?)", (i, a, s["stop_name"], float(s["stop_lat"]), float(s["stop_lon"])))
    # quais : tous ceux desservis + tous les quais enfants des zones citées par les lieux
    children = defaultdict(list)
    for sid, s in stops_all.items():
        if s.get("parent_station"):
            children[s["parent_station"]].append(sid)
    place_area_points = {sp for p in places_doc["places"] for a in p.get("stop_areas", []) for sp in children.get(a, [])
                         if sp in wanted_points}
    for i, sp in enumerate(sorted(wanted_points | place_area_points), start=1):
        s = stops_all.get(sp)
        if not s:
            continue
        parent = s["parent_station"] or sp
        if parent not in area_id:
            report.error("stop_point_without_area", sp)
            continue
        point_id[sp] = i
        cur.execute("INSERT INTO stop_point VALUES (?,?,?,?,?,?)", (i, sp, area_id[parent], s["stop_name"], float(s["stop_lat"]), float(s["stop_lon"])))

    # Lieux : « airport » (quais propres) ou « city » (Paris : un point de montée par ligne, R-100, D-13).
    # L'ordre de data/places.json est celui du sélecteur de l'accueil (sort_order).
    order = {p["id"]: i for i, p in enumerate(places_doc["places"])}
    city_boarding = []                                   # (lieu, code ligne, zone d'arrêt GTFS, nom attendu)
    for p in sorted(places_doc["places"], key=lambda x: x["id"]):
        kind = p.get("kind")
        if kind not in ("airport", "city"):
            report.error("place_kind_invalid", {"place": p["id"], "kind": kind})
            continue
        former = (p.get("former_fr") or None, p.get("former_en") or None)
        if (former[0] is None) != (former[1] is None):
            report.error("place_former_incomplete", p["id"])     # bloque la publication ; base écrite sans ancien nom
            former = (None, None)
        if kind == "city" and (p.get("sector") or p.get("stop_areas") or p.get("stop_points") or p.get("parent")):
            report.error("place_city_invalid", {"place": p["id"], "detail": "un lieu ville n'a ni secteur, ni arrêts, ni parent"})
            continue
        if kind == "airport" and (not p.get("sector") or p.get("boarding")):
            report.error("place_airport_invalid", {"place": p["id"], "detail": "un lieu aéroportuaire a un secteur et pas de point de montée"})
            continue
        cur.execute("INSERT INTO place VALUES (?,?,?,?,?,?,?,?,?,?)", (
            p["id"], p.get("parent"), kind, p.get("sector") if kind == "airport" else None, order[p["id"]],
            p["label_fr"], p["label_en"], p["short_label"], *former))
        if kind == "city":
            boarding = p.get("boarding") or []
            if not boarding:
                report.error("place_without_boarding", p["id"])
            seen = set()
            for b in boarding:
                code, area = b.get("line"), b.get("stop_area")
                if code in seen:
                    report.error("place_boarding_duplicate", {"place": p["id"], "line": code})
                    continue
                seen.add(code)
                if code not in line_id or code not in trips_line_codes:
                    report.error("place_boarding_line_unknown", {"place": p["id"], "line": code})
                    continue
                if area not in area_id:
                    report.error("place_boarding_stop_missing", {"place": p["id"], "line": code, "stop_area": area})
                    continue
                cur.execute("INSERT INTO place_boarding VALUES (?,?,?)", (p["id"], line_id[code], area_id[area]))
                city_boarding.append((p["id"], code, area, b.get("name")))
            continue
        pts = set(p.get("stop_points", []))
        for a in p.get("stop_areas", []):
            pts.update(c for c in children.get(a, []) if c in point_id)
        if not pts:
            report.error("place_without_stop", p["id"])
        for sp in sorted(pts):
            if sp not in point_id:
                report.error("place_stop_missing", {"place": p["id"], "stop": sp})
                continue
            cur.execute("INSERT INTO place_stop_point VALUES (?,?)", (p["id"], point_id[sp]))

    # Services
    svc_id = {}
    for i, (sid, dates) in enumerate(sorted(svc_dates.items()), start=1):
        mask = 0
        for d in dates:
            mask |= 1 << (d - base).days
        svc_id[sid] = i
        cur.execute("INSERT INTO service VALUES (?,?)", (i, mask))

    # Missions (patterns), trajets, horaires
    pattern_key_to_id, patterns = {}, []
    trip_rows, st_rows, seen_trips = [], [], set()
    duplicates = 0
    code_of_route = {l["idfm_route_id"]: l["id"] for l in lines_doc["lines"] if l["idfm_route_id"]}
    trips_per_line = defaultdict(int)
    for tid in sorted(trips):
        t = trips[tid]
        sts = stop_times.get(tid)
        if not sts or len(sts) < 2:
            report.warn("trip_too_short", tid)
            continue
        code = code_of_route[t["route_id"]]
        key = (code, t.get("direction_id", ""), t.get("trip_headsign", ""),
               tuple((s[1], s[4], s[5]) for s in sts))
        if key not in pattern_key_to_id:
            pattern_key_to_id[key] = len(patterns) + 1
            patterns.append(key)
        pid = pattern_key_to_id[key]
        times = tuple((s[3], s[3] - s[2]) for s in sts)
        sig = (pid, t["service_id"], times)
        if sig in seen_trips:
            duplicates += 1
            continue
        seen_trips.add(sig)
        trip_rows.append((pid, svc_id[t["service_id"]], times))
        trips_per_line[code] += 1
    # tri déterministe des trajets : mission, service, premier départ
    trip_rows.sort(key=lambda x: (x[0], x[1], x[2]))
    for pid, key in enumerate(patterns, start=1):
        code, direction, headsign, stops = key
        cur.execute("INSERT INTO pattern VALUES (?,?,?,?)", (pid, line_id[code], int(direction) if direction != "" else None, headsign))
        for seq, (sp, pu, do) in enumerate(stops):
            cur.execute("INSERT INTO pattern_stop VALUES (?,?,?,?,?)", (pid, seq, point_id[sp], pu, do))
    for tid, (pid, sid, times) in enumerate(trip_rows, start=1):
        cur.execute("INSERT INTO trip VALUES (?,?,?)", (tid, pid, sid))
        for seq, (dep, dwell) in enumerate(times):
            if dwell < 0:
                report.error("negative_dwell", {"pattern": pid, "seq": seq})
            st_rows.append((tid, seq, dep, max(dwell, 0)))
    cur.executemany("INSERT INTO stop_time VALUES (?,?,?,?)", st_rows)

    # Correspondances officielles entre quais de la base (R-119) : transfers.txt, type 2 (temps minimal publié).
    # Une ligne en double garde le temps le plus long ; un fichier absent ne bloque pas la publication (alerte),
    # les itinéraires hors connexion n'ayant alors que les changements au même quai.
    transfers, ignored = {}, Counter()
    with zipfile.ZipFile(zip_path) as zf:
        if "transfers.txt" not in zf.namelist():
            report.warn("transfers_missing", "transfers.txt absent du GTFS")
        else:
            for t in _rows(zf, "transfers.txt"):
                a, b = t.get("from_stop_id", ""), t.get("to_stop_id", "")
                if a not in point_id or b not in point_id:
                    continue
                if a == b:
                    ignored["same_stop"] += 1
                    continue
                if t.get("transfer_type", "") != "2":
                    ignored["type_" + (t.get("transfer_type") or "vide")] += 1
                    continue
                try:
                    secs = int(t.get("min_transfer_time") or "")
                except ValueError:
                    ignored["time_invalid"] += 1
                    continue
                if secs < 0:
                    ignored["time_invalid"] += 1
                    continue
                key = (point_id[a], point_id[b])
                if key in transfers:
                    ignored["duplicate"] += 1
                transfers[key] = max(secs, transfers.get(key, 0))
    if ignored:
        report.warn("transfers_ignored", dict(sorted(ignored.items())))
    cur.executemany("INSERT INTO transfer VALUES (?,?,?)", [(a, b, t) for (a, b), t in sorted(transfers.items())])

    # Aménagements publiés uniquement (R-90 à R-93). Ils ne bloquent jamais la publication :
    # une incohérence avec le GTFS du jour produit une alerte et l'élément concerné est ignoré.
    published = apply_adjustments(cur, published_adj, line_id, area_id, point_id, stops_all, children,
                                  patterns, trip_rows, report)
    check_city_boarding(cur, city_boarding, stops_all, report)

    con.commit()
    # Contrôles d'intégrité de la base produite
    fk = con.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        report.error("foreign_key_violation", fk[:20])
    ic = con.execute("PRAGMA integrity_check").fetchone()[0]
    if ic != "ok":
        report.error("integrity_check", ic)
    nonmono = con.execute("""SELECT COUNT(*) FROM stop_time a JOIN stop_time b
        ON b.trip_id = a.trip_id AND b.seq = a.seq + 1 WHERE b.departure - b.dwell < a.departure""").fetchone()[0]
    if nonmono:
        report.error("non_monotonic_times", nonmono)
    bad_terminus = con.execute("""SELECT COUNT(*) FROM pattern p
        JOIN pattern_stop f ON f.pattern_id = p.id AND f.seq = 0
        JOIN (SELECT pattern_id, MAX(seq) m FROM pattern_stop GROUP BY pattern_id) x ON x.pattern_id = p.id
        JOIN pattern_stop l ON l.pattern_id = p.id AND l.seq = x.m
        WHERE l.pickup != 1 OR f.dropoff != 1""").fetchone()[0]
    if bad_terminus:
        report.warn("terminus_flags_unusual", {"patterns": bad_terminus, "note": "R-32 suppose pickup_type=1 au terminus"})
    con.execute("ANALYZE")
    con.commit()
    con.execute("VACUUM")
    con.close()

    for l in lines_doc["lines"]:
        if l["state"] == "active" and trips_per_line.get(l["id"], 0) == 0:
            report.error("line_without_trips", l["id"])

    return {
        "meta": meta, "window_days": (last - start).days + 1, "first_service_date": base.isoformat(),
        "counts": {
            "lines_active": sum(1 for l in lines_doc["lines"] if l["state"] == "active"),
            "lines_future": sum(1 for l in lines_doc["lines"] if l["state"] == "future"),
            "stop_areas": len(area_id), "stop_points": len(point_id), "places": len(places_doc["places"]),
            "services": len(svc_id), "patterns": len(patterns), "trips": len(trip_rows), "stop_times": len(st_rows),
            "duplicate_trips_removed": duplicates, "adjustments_published": published, "transfers": len(transfers),
            "trips_per_line": dict(sorted(trips_per_line.items())),
        },
    }


def check_city_boarding(cur, city_boarding: list, stops_all: dict, report: Report) -> None:
    """Points de montée d'un lieu « ville » (R-100, D-13), contrôlés sur le GTFS du jour, sans bloquer :
    un point de montée que la ligne ne dessert plus vers un aéroport reste dans la base, l'accueil y affiche
    « Horaires indisponibles » (R-153), et l'alerte « boarding_not_served » demande de corriger data/places.json."""
    for place, code, area, name in city_boarding:
        served = cur.execute(
            """SELECT 1 FROM pattern p JOIN line l ON l.id = p.line_id
               JOIN pattern_stop b ON b.pattern_id = p.id AND b.pickup != 1
               JOIN stop_point bp ON bp.id = b.stop_point_id JOIN stop_area ba ON ba.id = bp.stop_area_id
               JOIN pattern_stop d ON d.pattern_id = p.id AND d.seq > b.seq AND d.dropoff != 1
               JOIN place_stop_point ps ON ps.stop_point_id = d.stop_point_id
               JOIN place pl ON pl.id = ps.place_id AND pl.kind = 'airport'
               WHERE l.code = ? AND ba.gtfs_id = ? LIMIT 1""", (code, area)).fetchone()
        if not served:
            report.warn("boarding_not_served", {"place": place, "line": code, "stop_area": area})
        gtfs_name = stops_all.get(area, {}).get("stop_name")
        if name and gtfs_name and name != gtfs_name:
            report.warn("boarding_name_differs", {"place": place, "line": code, "stop_area": area,
                                                  "data": name, "gtfs": gtfs_name})


def _distance_m(a: dict, b: dict) -> int:
    """Distance à vol d'oiseau entre deux arrêts GTFS, arrondie à 10 m (approximation équirectangulaire,
    écart < 0,1 % sous 5 km)."""
    la, lo, lb, lob = (math.radians(float(x)) for x in (a["stop_lat"], a["stop_lon"], b["stop_lat"], b["stop_lon"]))
    d = 6371008.8 * math.hypot((lob - lo) * math.cos((la + lb) / 2), lb - la)
    return int(round(d / 10.0)) * 10


def apply_adjustments(cur, adjustments: list, line_id: dict, area_id: dict, point_id: dict, stops_all: dict,
                      children: dict, patterns: list, trip_rows: list, report: Report) -> int:
    """Écrit les aménagements publiés et calcule l'écart avec le GTFS (R-92).

    - quais non desservis : ceux que le GTFS ne dessert plus sont ignorés (alerte « adjustment_stop_unused ») ;
    - trajets en retard : trajets du GTFS qui desservent encore un quai déclaré non desservi ;
    - lacunes : couples (quai de l'itinéraire en vigueur, mission en retard du même sens qui ne dessert pas
      sa zone d'arrêt alors qu'elle parcourt ce tronçon) ; le lecteur s'en sert pour signaler des horaires
      théoriques incomplets.
    """
    trips_per_pattern = defaultdict(int)
    for pid, _sid, _times in trip_rows:
        trips_per_pattern[pid] += 1
    written = 0
    for a in sorted(adjustments, key=lambda x: x["id"]):
        lid = line_id.get(a["line"])
        if lid is None:
            report.warn("adjustment_line_unknown", {"adjustment": a["id"], "line": a["line"]})
            continue
        # missions de la ligne : (id 1-based, sens, quais desservis)
        line_patterns = [(pid, key[1], [s[0] for s in key[3]]) for pid, key in enumerate(patterns, start=1)
                         if key[0] == a["line"]]
        rows, not_served = [], set()
        for ns in a["not_served"]:
            sp = ns["stop_point"]
            if sp not in stops_all:
                report.warn("adjustment_stop_missing", {"adjustment": a["id"], "stop_point": sp})
                continue
            if sp not in point_id:
                report.warn("adjustment_stop_unused", {"adjustment": a["id"], "stop_point": sp,
                                                       "note": "le GTFS ne dessert plus ce quai"})
                continue
            dirs = {d for _pid, d, stops in line_patterns if sp in stops}
            if str(ns["gtfs_direction_id"]) not in dirs:
                report.warn("adjustment_direction_mismatch", {"adjustment": a["id"], "stop_point": sp,
                                                              "declared": ns["gtfs_direction_id"], "gtfs": sorted(dirs)})
            rep = ns.get("replacement_stop_area")
            rep_id, dist = area_id.get(rep), None
            if rep is not None and rep_id is None:
                report.warn("adjustment_replacement_missing", {"adjustment": a["id"], "stop_area": rep})
            elif rep_id is not None:
                dist = _distance_m(stops_all[sp], stops_all[rep])
            rows.append((a["id"], point_id[sp], ns["direction_label"], rep_id,
                         ns.get("replacement_label") if rep_id is not None else None, dist))
            not_served.add(sp)
        stale = [(pid, d, stops) for pid, d, stops in line_patterns if not_served.intersection(stops)]
        lagging = sum(trips_per_pattern[pid] for pid, _d, _s in stale)
        gaps = set()                 # (quai de l'itinéraire en vigueur, mission en retard qui le contourne)
        if lagging:
            # comparaison par zone d'arrêt : un trajet en retard « couvre » une zone s'il y dessert un quai
            # encore desservi (un autre quai de la même zone suffit, ex. Gallieni 37464 / 493407)
            # Seul le tronçon que la mission en retard parcourt compte : une zone de l'itinéraire en vigueur
            # est contournée si elle se trouve, dans l'ordre de route_in_force, entre deux zones que la
            # mission dessert (une mission partielle ne crée pas de fausse lacune à ses extrémités).
            def area_of(sp):
                return stops_all[sp]["parent_station"] or sp
            order = {ar["stop_area"]: i for i, ar in enumerate(a["route_in_force"])}
            in_force = {c for ar in a["route_in_force"] for c in children.get(ar["stop_area"], [])
                        if c in point_id and c not in not_served}
            stale_areas = [(sp_id, sd, {area_of(x) for x in sstops if x not in not_served}) for sp_id, sd, sstops in stale]
            for _pid, d, stops in line_patterns:
                for sp in in_force.intersection(stops):
                    pos = order[area_of(sp)]
                    for stale_pid, sd, covered in stale_areas:
                        idx = [order[x] for x in covered if x in order]
                        if sd == d and area_of(sp) not in covered and idx and min(idx) < pos < max(idx):
                            gaps.add((sp, stale_pid))
            report.warn("adjustment_overrides_gtfs", {
                "adjustment": a["id"], "lagging_trips": lagging,
                "trips_per_stop": {sp: sum(trips_per_pattern[pid] for pid, _d, st in stale if sp in st)
                                   for sp in sorted(not_served)},
                "incomplete_stop_points": sorted({sp for sp, _p in gaps})})
        else:
            report.warn("adjustment_redundant", {"adjustment": a["id"],
                                                 "note": "le GTFS reflète l'aménagement : il peut être retiré"})
        cur.execute("INSERT INTO adjustment VALUES (?,?,?,?,?,?,?,?,?,?)", (
            a["id"], lid, a["kind"], a["title_fr"], a["summary_fr"], a["valid_from"], a["valid_until"],
            a["source_url"], a["checked_on"], lagging))
        cur.executemany("INSERT INTO adjustment_stop VALUES (?,?,?,?,?,?)", sorted(rows, key=lambda r: r[1]))
        cur.executemany("INSERT INTO adjustment_gap VALUES (?,?,?)",
                        sorted((a["id"], point_id[sp], pid) for sp, pid in gaps))
        written += 1
    return written


def compare_previous(prev_manifest: dict, counts: dict, report: Report) -> None:
    prev = prev_manifest.get("counts", {}).get("trips_per_line", {})
    for code, before in prev.items():
        now = counts["trips_per_line"].get(code, 0)
        if before and (before - now) / before > MAX_TRIP_LOSS_RATIO:
            report.error("line_trip_drop", {"line": code, "before": before, "now": now})


def file_url(base_url: str, version: str, name: str) -> str:
    """Adresse publique de la base : base_url (où {version} et {name} sont remplacés) suivie du nom de fichier."""
    if not base_url:
        return name
    base = base_url.replace("{version}", version).replace("{name}", name)
    return base if "{name}" in base_url else base.rstrip("/") + "/" + name


def inputs_sha256(data_dir: Path) -> str:
    """Empreinte de tout ce qui, hors GTFS, détermine la base : référentiel, schéma et générateur.

    Le traitement quotidien reconstruit la base quand elle change, même si le GTFS n'a pas bougé."""
    h = hashlib.sha256()
    for path in (data_dir / "lines.json", data_dir / "places.json", data_dir / "adjustments.json",
                 SCHEMA_PATH, Path(__file__).resolve()):
        h.update(path.name.encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("gtfs_zip", type=Path, nargs="?")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--data", type=Path, default=ROOT / "data")
    ap.add_argument("--previous-manifest", type=Path)
    ap.add_argument("--today", type=date.fromisoformat, default=None)
    # Adresse publique du dossier de la base ; {version} et {name} y sont remplacés (release GitHub, ADR-8).
    ap.add_argument("--base-url", default="")
    ap.add_argument("--print-inputs-sha256", action="store_true",
                    help="affiche l'empreinte du référentiel, du schéma et du générateur, puis s'arrête")
    args = ap.parse_args()
    if args.print_inputs_sha256:
        print(inputs_sha256(args.data))
        return 0
    if args.gtfs_zip is None or args.out is None:
        ap.error("le fichier GTFS et --out sont requis")

    report = Report()
    lines_doc, places_doc, adjustments_doc = load_inputs(args.data)
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "timetable.sqlite"
        # Jour de la construction, à l'heure de Paris : début de validité au plus tôt, et contrôle d'expiration.
        today = args.today or datetime.now(ZoneInfo(TIMEZONE)).date()
        result = build_db(db_path, args.gtfs_zip, lines_doc, places_doc, adjustments_doc, report, today)
        manifest = None
        if result:
            end = date.fromisoformat(result["meta"]["validity_end"])
            remaining = (end - today).days
            if remaining < 0:
                report.error("feed_expired", {"validity_end": end.isoformat(), "today": today.isoformat()})
            elif remaining < MIN_REMAINING_DAYS_WARN:
                report.warn("feed_short_validity", {"remaining_days": remaining})
            if args.previous_manifest and args.previous_manifest.exists():
                compare_previous(json.loads(args.previous_manifest.read_text(encoding="utf-8")), result["counts"], report)

            raw = db_path.read_bytes()
            db_sha = hashlib.sha256(raw).hexdigest()
            version = f"{result['meta']['validity_start'].replace('-', '')}-{db_sha[:12]}"
            gz_name = f"timetable-{version}.sqlite.gz"
            gz_path = args.out / gz_name
            with open(gz_path, "wb") as f:
                with gzip.GzipFile(filename="", mode="wb", fileobj=f, compresslevel=9, mtime=0) as g:
                    g.write(raw)
            manifest = {
                "schema": "airport-connect/timetable-manifest@1",
                "db_schema_version": SCHEMA_VERSION,
                "version": version,
                "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "source": {"url": SOURCE_URL, "sha256": result["meta"]["source_sha256"]},
                "inputs": {"sha256": inputs_sha256(args.data)},
                "validity": {"start": result["meta"]["validity_start"], "end": result["meta"]["validity_end"], "days": result["window_days"],
                             "first_service_date": result["first_service_date"]},
                "file": {"name": gz_name, "url": file_url(args.base_url, version, gz_name),
                         "encoding": "gzip", "bytes_gzip": gz_path.stat().st_size, "sha256_gzip": sha256_file(gz_path),
                         "bytes": len(raw), "sha256": db_sha},
                "counts": result["counts"],
                "warnings": report.warnings,
            }
    report_doc = {"ok": not report.errors, "errors": report.errors, "warnings": report.warnings}
    (args.out / "report.json").write_text(json.dumps(report_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if report.errors or manifest is None:
        if manifest is not None:
            (args.out / manifest["file"]["name"]).unlink(missing_ok=True)   # rien de publiable ne reste
        print("ÉCHEC : publication bloquée", json.dumps(report.errors, ensure_ascii=False)[:2000])
        return 2
    (args.out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    c = manifest["counts"]
    print(f"OK {manifest['version']} : {c['trips']} trajets, {c['stop_times']} passages, "
          f"{manifest['file']['bytes'] / 1e6:.1f} Mo ({manifest['file']['bytes_gzip'] / 1e6:.1f} Mo gzip), "
          f"validité {manifest['validity']['start']} → {manifest['validity']['end']}, {len(report.warnings)} alerte(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
