#!/usr/bin/env python3
"""Diagnostic d'un GTFS IDFM : quels quais existent près d'un point, sous un nom ou dans une zone d'arrêt,
et quelles lignes suivies (data/lines.json) les desservent. Sert quand le générateur signale une référence
disparue (stop_missing, place_stop_missing) : on retrouve le quai qui l'a remplacée.

    python3 tools/pipeline/inspect_gtfs.py IDFM-gtfs.zip --near 49.00396,2.57918 [--radius 300]
    python3 tools/pipeline/inspect_gtfs.py IDFM-gtfs.zip --name "CDG T2" --area IDFM:73699
    python3 tools/pipeline/inspect_gtfs.py IDFM-gtfs.zip --stop IDFM:487507

Les filtres se cumulent (ET). Bibliothèque standard uniquement.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def rows(z: zipfile.ZipFile, name: str):
    with z.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("gtfs_zip", type=Path)
    ap.add_argument("--near", help="LAT,LON")
    ap.add_argument("--radius", type=float, default=300.0, help="mètres (défaut 300)")
    ap.add_argument("--name", help="sous-chaîne du nom, sans tenir compte de la casse")
    ap.add_argument("--area", action="append", default=[], help="parent_station (répétable)")
    ap.add_argument("--stop", action="append", default=[], help="stop_id exact (répétable)")
    args = ap.parse_args()
    if not (args.near or args.name or args.area or args.stop):
        ap.error("au moins un filtre : --near, --name, --area ou --stop")

    z = zipfile.ZipFile(args.gtfs_zip)
    near = tuple(float(x) for x in args.near.split(",")) if args.near else None
    found = {}
    for s in rows(z, "stops.txt"):
        if args.stop and s["stop_id"] not in args.stop:
            continue
        if args.area and s.get("parent_station") not in args.area and s["stop_id"] not in args.area:
            continue
        if args.name and args.name.lower() not in s["stop_name"].lower():
            continue
        d = None
        if near:
            try:
                d = distance_m(near[0], near[1], float(s["stop_lat"]), float(s["stop_lon"]))
            except ValueError:
                continue
            if d > args.radius:
                continue
        found[s["stop_id"]] = (s, d)
    for sid in args.stop:
        if sid not in found:
            print(f"ABSENT du GTFS : {sid}")

    lines = json.loads((ROOT / "data" / "lines.json").read_text(encoding="utf-8"))["lines"]
    route_code = {l["idfm_route_id"]: l["id"] for l in lines if l.get("idfm_route_id")}
    trip_code = {t["trip_id"]: route_code[t["route_id"]] for t in rows(z, "trips.txt") if t["route_id"] in route_code}
    served = defaultdict(set)
    for st in rows(z, "stop_times.txt"):
        if st["stop_id"] in found and st["trip_id"] in trip_code:
            served[st["stop_id"]].add(trip_code[st["trip_id"]])

    order = sorted(found.items(), key=lambda kv: (kv[1][1] if kv[1][1] is not None else 0, kv[0]))
    print(f"{len(order)} arrêt(s) :")
    for sid, (s, d) in order:
        dist = f"{d:6.0f} m" if d is not None else "        "
        lines_txt = ", ".join(sorted(served.get(sid, ()))) or "aucune ligne suivie"
        print(f"{dist}  {sid:<34} {s['stop_name']:<40} parent={s.get('parent_station') or '-':<16} "
              f"type={s.get('location_type') or '0'}  {lines_txt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
