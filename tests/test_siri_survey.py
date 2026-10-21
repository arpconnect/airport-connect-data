#!/usr/bin/env python3
"""
Teste le relevé SIRI (tools/pipeline/siri_survey.py ; spec R-159, D-4), sans réseau :

1. scénario construit : classement des passages, courses suivies d'une lecture à l'autre (« ancien puis écart »),
   lectures vides et en erreur, synthèse ;
2. lecture à travers un proxy simulé : écart entre requêtes, série arrêtée au premier 429 (R-83) ;
3. sur les 84 réponses SIRI réelles du 9 octobre 2026 (dépôt complet seulement) : passages mesurés exactement comme
   la lecture de référence de l'application (`tools/reference/v2_realtime.py`) ;
4. sur la base embarquée de l'application (dépôt complet seulement) : quais choisis pour data/siri-survey.json.

    python3 tests/test_siri_survey.py
"""

from __future__ import annotations

import gzip
import io
import json
import sys
import tempfile
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tools" / "reference"), str(ROOT / "tools" / "pipeline")]
import siri_survey as sv  # noqa: E402
import v2_realtime as rt  # noqa: E402

failures, passed = [], 0


def check(name, got, expected):
    global passed
    if got == expected:
        passed += 1
    else:
        failures.append(f"{name}\n    obtenu  : {got!r}\n    attendu : {expected!r}")


# ---------------------------------------------------------------------------------------------------------------
# 1. Scénario construit (21 octobre 2026, 08:00 à Paris = 06:00 UTC)
# ---------------------------------------------------------------------------------------------------------------
T0 = 1792562400                                  # 2026-10-21T06:00:00Z
ROUTE, SP = "IDFM:C00379", "IDFM:488573"
QUAIS = {"schema": sv.SCHEMA, "base_version": "test", "missing": [],
         "quais": [{"group": "cible", "line": "BUS_4121", "route_id": ROUTE, "stop_point": SP, "name": "Orly 1-2-3"}]}


def visit(journey, aimed, expected, status="onTime", recorded=None, line="C00379", stop="488573", dest="423013"):
    call = {"DepartureStatus": status}
    if aimed is not None:
        call["AimedDepartureTime"] = rt.iso(aimed)
    if expected is not None:
        call["ExpectedDepartureTime"] = rt.iso(expected)
    item = {"MonitoringRef": {"value": f"STIF:StopPoint:Q:{stop}:"},
            "MonitoredVehicleJourney": {"LineRef": {"value": f"STIF:Line::{line}:"},
                                        "FramedVehicleJourneyRef": {"DatedVehicleJourneyRef": journey},
                                        "DestinationRef": {"value": f"STIF:StopPoint:Q:{dest}:"},
                                        "MonitoredCall": call}}
    if recorded is not None:
        item["RecordedAtTime"] = rt.iso(recorded)
    return item


def doc(now, visits, error=False):
    smd = {"MonitoredStopVisit": visits}
    if error:
        smd["ErrorCondition"] = {"ErrorText": "x"}
    return json.dumps({"Siri": {"ServiceDelivery": {"ResponseTimestamp": rt.iso(now), "StopMonitoringDelivery": [smd]}}})


def run(state, tmp, now, body, outcome="ok"):
    d = Path(tmp) / str(now)
    d.mkdir()
    entry = {"stop_point": SP, "line": "BUS_4121", "outcome": outcome, "received_at": now}
    if outcome == "ok":
        (d / "000.json").write_text(body, encoding="utf-8")
        entry["file"] = "000.json"
    return sv.update(state, QUAIS, {"entries": [entry]}, d, now)


check("classe : supprimé", sv.classify(10, 10, "cancelled", 0, 10), "supprime")
check("classe : noReport", sv.classify(10, 10, "noReport", 10, 10), "noreport")
check("classe : sans heure annoncée", sv.classify(10, None, "onTime", 10, 10), "noreport")
check("classe : sans heure prévue", sv.classify(None, 10, "onTime", None, 10), "sans_prevue")
check("classe : écart", sv.classify(10, 70, "delayed", 0, 5000), "ecart")
check("classe : récent (10 min pile)", sv.classify(10, 10, "onTime", 400, 1000), "recent")
check("classe : ancien (10 min et 1 s)", sv.classify(10, 10, "onTime", 399, 1000), "ancien")
check("classe : ancien sans enregistrement", sv.classify(10, 10, "onTime", None, 1000), "ancien")

with tempfile.TemporaryDirectory() as tmp:
    st = sv.empty_state()
    a1, a2 = T0 + 1200, T0 + 2400
    # 1re lecture : J1 égale à la prévue, enregistrée 1 h plus tôt ; J2 noReport ; passage d'une autre ligne écarté
    st = run(st, tmp, T0, doc(T0, [visit("J1", a1, a1, recorded=T0 - 3600), visit("J2", a2, a2, "noReport", T0 - 60),
                                   visit("X", a1, a1 + 60, "delayed", T0, line="C00001")]))
    # 2e lecture : rien n'a changé (aucune nouvelle lecture gardée pour J1)
    st = run(st, tmp, T0 + 1800, doc(T0 + 1800, [visit("J1", a1, a1, recorded=T0 - 3600)]))
    # 3e lecture : J1 annoncé 2 min plus tard (écart) ; 4e : réponse en erreur ; 5e : échec réseau ; 6e : vide
    st = run(st, tmp, T0 + 1900, doc(T0 + 1900, [visit("J1", a1, a1 + 120, "delayed", T0 + 1880)]))
    st = run(st, tmp, T0 + 2000, doc(T0 + 2000, [], error=True))
    st = run(st, tmp, T0 + 2100, None, outcome="network_error")
    st = run(st, tmp, T0 + 2200, doc(T0 + 2200, []))
    j1 = st["journeys"][f"{SP}|BUS_4121|J1|{a1}"]
    check("course J1 : lectures gardées quand elles changent",
          [s[4] for s in j1["samples"]], ["ancien", "ecart"])
    check("course J2 : noReport", [s[4] for s in st["journeys"][f"{SP}|BUS_4121|J2|{a2}"]["samples"]], ["noreport"])
    check("autre ligne écartée", sorted(st["journeys"]), sorted([f"{SP}|BUS_4121|J1|{a1}", f"{SP}|BUS_4121|J2|{a2}"]))
    band = st["counts"]["BUS_4121"]["bands"]["5 h – 9 h"]
    check("comptes de la tranche 5 h – 9 h", {k: v for k, v in band.items() if v},
          {"ancien": 2, "noreport": 1, "ecart": 1})
    check("âges des passages « ancien » (1 h, puis 1 h 30)", st["counts"]["BUS_4121"]["ages"],
          {"≤ 60 min": 1, "≤ 120 min": 1})
    s = sv.summarize(st)
    line = s["lines"]["BUS_4121"]
    check("synthèse : passages et part mesurée", (line["observations"], line["measured_share"]), (4, 0.25))
    check("synthèse : courses", (line["journeys"], line["journeys_measured"], line["journeys_updated"],
                                 line["journeys_ancien_then_ecart"]), (2, 1, 1, 1))
    check("synthèse : lectures vides", line["empty_reads"], 1)
    check("synthèse : requêtes", s["requests"],
          {"ok": 5, "http_error": 0, "network_error": 1, "rate_limited": 0, "skipped": 0, "error_condition": 1})
    md = sv.markdown(s)
    check("synthèse lisible : ligne du tableau", "| BUS_4121 | cible | 4 | 25 % | 1 | 0 | 2 | 1 | 2 | 1 | 1 | 1 |" in md, True)
    path = Path(tmp) / "state.json.gz"
    sv.save_state(st, path)
    first = path.read_bytes()
    sv.save_state(sv.load_state(path), path)
    check("état : relu à l'identique, même fichier", (sv.load_state(path) == st, path.read_bytes() == first), (True, True))
    check("état absent : relevé commencé", sv.load_state(Path(tmp) / "absent.json.gz"), sv.empty_state())

check("relevé actif jusqu'à sa date de fin incluse",
      [sv.active({"until": "2026-11-04"}, sv.date.fromisoformat(d)) for d in ["2026-10-21", "2026-11-04", "2026-11-05"]],
      [True, True, False])


# ---------------------------------------------------------------------------------------------------------------
# 2. Proxy simulé
# ---------------------------------------------------------------------------------------------------------------
class Resp(io.BytesIO):
    def __init__(self, body, status):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def opener_of(statuses):
    calls = []

    def opener(req, timeout):
        calls.append(req.full_url)
        status = statuses[len(calls) - 1]
        if status == "net":
            raise urllib.error.URLError("hors ligne")
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(b"{}"))
        return Resp(doc(T0, []).encode(), 200)
    return opener, calls


quais4 = {"base_version": "test", "quais": [dict(QUAIS["quais"][0], stop_point=f"IDFM:{n}") for n in (1, 2, 3, 4, 5)]}
quais4["quais"].insert(1, dict(QUAIS["quais"][0], line="BUS_350", stop_point="IDFM:1"))     # même quai, deux lignes
with tempfile.TemporaryDirectory() as tmp:
    sleeps = []
    opener, calls = opener_of([200, 500, "net", 429])
    index = sv.fetch(quais4, "https://proxy.example/", Path(tmp), sleep=sleeps.append, opener=opener)
    check("proxy : issues", [e["outcome"] for e in index["entries"]],
          ["ok", "http_error", "network_error", "rate_limited", "skipped"])
    check("proxy : série arrêtée au 429", len(calls), 4)
    check("proxy : 1 s entre deux requêtes", sleeps, [1.0, 1.0, 1.0])
    check("proxy : requête du premier quai", calls[0],
          "https://proxy.example/stop-monitoring?MonitoringRef=STIF%3AStopPoint%3AQ%3A1%3A")
    check("proxy : réponse et index écrits", sorted(p.name for p in Path(tmp).iterdir()), ["000.json", "index.json"])
    check("proxy : un quai relevé pour deux lignes n'est lu qu'une fois", [e["stop_point"] for e in index["entries"]],
          [f"IDFM:{n}" for n in (1, 2, 3, 4, 5)])


# ---------------------------------------------------------------------------------------------------------------
# 3. Réponses réelles du 9 octobre 2026
# ---------------------------------------------------------------------------------------------------------------
GOLDENS = ROOT / "app" / "src" / "test" / "resources" / "realtime-goldens.json"
SIRI = ROOT / "app" / "src" / "test" / "resources" / "siri" / "2026-10-09"
if GOLDENS.exists():
    cases = {(c["file"], c["route_id"]): c for c in json.loads(GOLDENS.read_text(encoding="utf-8"))["cases"]}
    compared = mismatched = 0
    for (name, route), c in sorted(cases.items()):
        text = gzip.decompress((SIRI / name).read_bytes()).decode("utf-8")
        now = c["now_ms"] // 1000
        ref = rt.parse_stop_monitoring(text, c["stop_point"], now)
        _, obs = sv.observations(text, {"stop_point": c["stop_point"], "route_id": route}, now)
        mine = {}
        for o in obs:
            mine.setdefault((o["destination"], o["aimed"] if o["aimed"] is not None else o["expected"]), set()).add(
                o["class"] in sv.MEASURED)
        theirs = {(v.destination, v.aimed if v.aimed is not None else v.expected): v.measured
                  for v in ref.visits if v.line == route}
        for key, measured in theirs.items():
            compared += 1
            mismatched += measured not in mine.get(key, set())
        mismatched += len(set(mine) - set(theirs))
    check("réponses réelles : passages comparés à la lecture de l'application", compared, 454)
    check("réponses réelles : même classement mesuré / théorique", mismatched, 0)
else:
    print("Réponses SIRI réelles absentes (dépôt public) : partie 3 non jouée")


# ---------------------------------------------------------------------------------------------------------------
# 4. Base embarquée de l'application
# ---------------------------------------------------------------------------------------------------------------
EMBEDDED = ROOT / "app" / "src" / "main" / "assets" / "timetable" / "timetable.sqlite.gz"
if EMBEDDED.exists():
    config = json.loads((ROOT / "data" / "siri-survey.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        con = sv.open_db(EMBEDDED, Path(tmp))
        result = sv.plan(con, config, "embarquée")
        per_line = {}
        for q in result["quais"]:
            per_line.setdefault(q["line"], []).append(q["stop_point"])
            check(f"quai {q['stop_point']} ({q['line']}) : référence SIRI", rt.monitoring_ref(q["stop_point"]) is not None, True)
        directions = {code: {d for (d,) in con.execute(
            """SELECT DISTINCT p.direction FROM pattern_stop ps JOIN stop_point sp ON sp.id = ps.stop_point_id
               JOIN pattern p ON p.id = ps.pattern_id JOIN line l ON l.id = p.line_id
               WHERE l.code = ? AND sp.gtfs_id IN (%s) AND ps.pickup != 1""" % ",".join("?" * len(sps)), (code, *sps))}
            for code, sps in per_line.items()}
        con.close()
    lines = [c for codes in config["groups"].values() for c in codes]
    check("base embarquée : toutes les lignes ont leurs quais", result["missing"], [])
    check("base embarquée : 2 quais distincts par ligne", {c: len(set(per_line.get(c, []))) for c in lines},
          {c: 2 for c in lines})
    check("base embarquée : les deux sens quand la ligne en a deux",
          [c for c, d in directions.items() if len(d) < 2], [])
    check("base embarquée : au plus 28 requêtes par passage", len(result["quais"]) <= 28, True)
else:
    print("Base embarquée absente (dépôt public) : partie 4 non jouée")

for f in failures:
    print("ÉCHEC", f)
print(f"{passed} contrôles conformes, {len(failures)} échec(s)")
sys.exit(1 if failures else 0)
