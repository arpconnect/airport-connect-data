#!/usr/bin/env python3
"""
Teste les alertes de lignes (D-5, tools/pipeline/line_alerts.py), sans réseau :

1. sur le rapport réel de la base construite le 15 octobre 2026 depuis le GTFS de juin 2026
   (tests/alerts/report-gtfs-2026-06.json) : IDFM y annonçait « EX 100 (future 700) » et « EX 19 (future 7719) »,
   renumérotations effectives le 24 août 2026 (spec R-14) ;
2. sur des rapports fictifs : chaque motif, dédoublonnage, lignes sans identifiant ignorées ;
3. face à une API GitHub simulée : une issue par alerte nouvelle, aucune pour une alerte déjà ouverte.

    python3 tests/test_line_alerts.py
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "pipeline"))
import line_alerts as la  # noqa: E402

failures, passed = [], 0


def check(name, got, expected):
    global passed
    if got == expected:
        passed += 1
    else:
        failures.append(f"{name}\n    obtenu  : {got!r}\n    attendu : {expected!r}")


# 1. Rapport réel
june = json.loads((ROOT / "tests" / "alerts" / "report-gtfs-2026-06.json").read_text(encoding="utf-8"))
found = la.alerts([june])
check("juin 2026 : deux renumérotations annoncées, rien d'autre", [a["key"] for a in found],
      ["route_renumbering_announced:BUS_EX100", "route_renumbering_announced:BUS_EX19"])
check("juin 2026 : titre", found[0]["title"], "Ligne BUS_EX100 : renumérotation annoncée « EX 100 » → « 700 »")

# 2. Motifs
timetable = {"errors": [{"code": "route_missing", "detail": {"line": "BUS_350", "route_id": "IDFM:C01110"}},
                        {"code": "line_without_trips", "detail": "BUS_351"},
                        {"code": "place_stop_missing", "detail": {"place": "cdg-t1", "stop": "IDFM:1"}}],
             "warnings": [{"code": "route_renamed", "detail": {"line": "BUS_9501", "before": "9501", "now": "1601"}},
                          {"code": "line_service_ends", "detail": {"line": "T9", "last_service_date": "2026-11-01",
                                                                   "operator_last_date": "2026-11-20", "operator": "Keolis"}},
                          {"code": "line_retired", "detail": {"line": "BUS_A01", "before": 120}},
                          {"code": "route_renamed", "detail": {"line": "BUS_9501", "before": "9501", "now": "1601"}}]}
plans = {"errors": [], "warnings": [{"code": "line_name_differs", "detail": {"line": "BUS_9501", "lines_json": "9501",
                                                                             "idfm_referential": "1601"}},
                                    {"code": "plan_old", "detail": {"plan": "BUS_N1", "date": "2021-01-01"}}]}
got = la.alerts([timetable, plans])
check("motifs : une alerte par (ligne, motif), triées, autres codes ignorés", [a["key"] for a in got], [
    "route_missing:BUS_350", "line_without_trips:BUS_351", "line_name_differs:BUS_9501", "route_renamed:BUS_9501",
    "line_service_ends:T9"])
check("fin de service : transporteur et dates dans le corps",
      all(x in got[-1]["body"] for x in ("Keolis", "2026-11-01", "2026-11-20")), True)
check("rapports vides", la.alerts([{"errors": [], "warnings": []}]), [])


# 3. API simulée
class FakeApi:
    def __init__(self, open_bodies):
        self.open_bodies, self.created, self.calls = open_bodies, [], []

    def __call__(self, req, timeout=30):
        self.calls.append((req.get_method(), req.full_url))
        check(f"en-tête d'authentification ({req.get_method()})", req.get_header("Authorization"), "Bearer jeton")
        if req.get_method() == "GET":
            body = [{"number": i + 1, "body": b} for i, b in enumerate(self.open_bodies)]
        else:
            payload = json.loads(req.data.decode("utf-8"))
            self.created.append(payload)
            body = {"number": 100 + len(self.created)}

        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return R(json.dumps(body).encode("utf-8"))


api = FakeApi([f"Déjà signalé.\n\n{la.KEY_PREFIX}route_missing:BUS_350 -->", "issue sans clé"])
opened = la.publish(got, la.GitHub("arpconnect/airport-connect-data", "jeton", opener=api))
check("API : alerte déjà ouverte non republiée", [a["key"] for a in opened], [a["key"] for a in got[1:]])
check("API : numéros des issues créées", [a["number"] for a in opened], [101, 102, 103, 104])
check("API : étiquette et clé dans le corps",
      all(c["labels"] == [la.LABEL] and f"{la.KEY_PREFIX}{k} -->" in c["body"] for c, k in zip(api.created, [a["key"] for a in opened])),
      True)
check("API : lecture des issues ouvertes de l'étiquette", api.calls[0],
      ("GET", "https://api.github.com/repos/arpconnect/airport-connect-data/issues?state=open&labels=alerte-ligne&per_page=100&page=1"))

for f in failures:
    print("ÉCHEC", f)
print(f"{passed} contrôles conformes, {len(failures)} échec(s)")
sys.exit(1 if failures else 0)
