#!/usr/bin/env python3
"""
Teste le relevé d'exactitude du temps réel (tools/pipeline/siri_accuracy.py), sans réseau : course partie ou
disparue, écart d'annonce à chaque distance du départ, âge des mesures, veille face à un proxy simulé (réponses SIRI
construites, horloge simulée, arrêt sur 429).

    python3 tests/test_siri_accuracy.py
"""

from __future__ import annotations

import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "pipeline"))
import siri_accuracy as sa  # noqa: E402

failures, passed = [], 0


def check(name, got, expected):
    global passed
    if got == expected:
        passed += 1
    else:
        failures.append(f"{name}\n    obtenu  : {got!r}\n    attendu : {expected!r}")


T0 = 1_794_000_000          # instant de référence des cas (secondes Unix)

# 1. Course partie, disparue, encore au quai
polls = [T0 + 30 * i for i in range(30)]
# Annoncée à T0+600 puis T0+640 ; lue jusqu'à T0+630 (départ annoncé 10 s plus tard), absente ensuite.
samples = [[T0 + 30 * i, T0 + 600 if i < 10 else T0 + 640, True, T0 + 30 * i - 20] for i in range(22)]
check("partie : absente de deux lectures, départ annoncé proche de la dernière lecture", sa.departed(samples, polls), True)
check("encore au quai : une seule lecture manquée", sa.departed(samples, polls[:23]), False)
check("disparue : départ annoncé 5 min après la dernière lecture", sa.departed([[T0, T0 + 300, True, None]], polls), False)

# 2. Écarts : dernière annonce T0+640. À 10 min (T0+40) on lisait T0+600 → +40 s ; à 5 min (T0+340) on lisait T0+640 → 0.
errs = sa.lead_errors(samples)
check("écarts à 10, 5, 3, 2 et 1 min", errs, {600: 40, 300: 0, 180: 0, 120: 0, 60: 0})
check("annonce non mesurée à la distance voulue : pas d'écart",
      sa.lead_errors([[T0, T0 + 600, False, None], [T0 + 590, T0 + 600, True, None]]), {})
check("aucune lecture à 20 s près de la distance : pas d'écart", sa.lead_errors([[T0 + 575, T0 + 600, True, None]]), {})

# 3. État et synthèse
track = {"started_at": T0, "ended_at": T0 + 900, "requests": 60, "errors": 1, "rate_limited": False, "quais": {
    "IDFM:1": {"line": "T9", "polls": polls, "journeys": {
        "a": samples,
        "b": [[T0, T0 + 30, False, None]],                                   # partie, jamais mesurée
        "c": [[T0, T0 + 900, True, T0 - 5]],                                 # disparue loin de son départ
        "d": [[polls[-1], polls[-1] + 60, True, polls[-1]]]}}}}              # encore au quai à la fin
state = sa.update(sa.empty_state(), track)
line = state["lines"]["T9"]
check("état : parties, disparues, jamais mesurées", (line["departed"], line["vanished"], line["unmeasured"]), (2, 1, 1))
check("état : écarts gardés par distance", line["errors"], {"600": [40], "300": [0], "180": [0], "120": [0], "60": [0]})
check("état : âges des 20 annonces lues dans les 10 min avant le départ (640 s), tranche de 20 s", line["ages"], {"20": 20})
summary = sa.summarize(state)
t9 = summary["lines"]["T9"]
check("synthèse : écart à 10 min", (t9["leads"]["600"]["n"], t9["leads"]["600"]["median_abs_s"], t9["leads"]["600"]["within_30s"]), (1, 40, 0.0))
check("synthèse : âge médian et part sous 30 s", (t9["age_median_s"], t9["age_within_30s"], t9["age_within_15s"]), (20, 1.0, 0.0))
check("centile par rang le plus proche", (sa.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90), sa.median([1, 2, 3, 4])), (9, 2.5))
check("synthèse lisible", "| T9 | 2 | 1 | 1 | 40 s ; 40 s ; 100 % (1) |" in sa.markdown(summary), True)


# 4. Veille face à un proxy simulé
def iso(e):
    return datetime.fromtimestamp(e, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Proxy:
    def __init__(self, limit_after=None):
        self.now, self.calls, self.limit_after = float(T0), 0, limit_after

    def clock(self):
        return self.now

    def sleep(self, s):
        self.now += s

    def __call__(self, req, timeout=30):
        self.calls += 1
        if self.limit_after is not None and self.calls > self.limit_after:
            import urllib.error
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b""))
        now = int(self.now)
        visits = []
        if now < T0 + 200:                  # le véhicule part à T0+185 : visible jusque-là, annoncé avec 15 s de retard
            visits.append({"RecordedAtTime": iso(now - 10), "MonitoringRef": {"value": "STIF:StopPoint:Q:1:"},
                           "MonitoredVehicleJourney": {
                               "LineRef": {"value": "STIF:Line::C01999:"}, "DestinationRef": {"value": "STIF:StopPoint:Q:9:"},
                               "FramedVehicleJourneyRef": {"DatedVehicleJourneyRef": "course-1"},
                               "MonitoredCall": {"AimedDepartureTime": iso(T0 + 170), "ExpectedDepartureTime": iso(T0 + 185),
                                                 "DepartureStatus": "delayed"}}})
        doc = {"Siri": {"ServiceDelivery": {"ResponseTimestamp": iso(now), "StopMonitoringDelivery": [{"MonitoredStopVisit": visits}]}}}

        class R(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return R(json.dumps(doc).encode("utf-8"))


quais = {"quais": [{"group": "ferre", "line": "T11", "route_id": "IDFM:C01999", "stop_point": "IDFM:1", "name": "Quai"}]}
px = Proxy()
tr = sa.watch(quais, "https://proxy.example", 5, clock=px.clock, sleep=px.sleep, opener=px)
q = tr["quais"]["IDFM:1"]
check("veille : une lecture toutes les 30 s pendant 5 min", (tr["requests"], len(q["polls"]), tr["rate_limited"]), (10, 10, False))
check("veille : la course est suivie jusqu'à son départ", [(s[0] - T0, s[1] - T0, s[2]) for s in q["journeys"]["course-1"]],
      [(0, 185, True), (30, 185, True), (60, 185, True), (90, 185, True), (120, 185, True), (150, 185, True), (180, 185, True)])
st = sa.update(sa.empty_state(), tr)
check("veille : course partie, écarts nuls à 3, 2 et 1 min", (st["lines"]["T11"]["departed"], st["lines"]["T11"]["errors"]),
      (1, {"180": [0], "120": [0], "60": [0]}))
check("veille : âge de 10 s à chaque lecture", st["lines"]["T11"]["ages"], {"10": 7})
px = Proxy(limit_after=3)
tr = sa.watch(quais, "https://proxy.example", 5, clock=px.clock, sleep=px.sleep, opener=px)
check("veille : arrêt immédiat sur 429", (tr["requests"], tr["rate_limited"], len(tr["quais"]["IDFM:1"]["polls"])), (4, True, 3))

for f in failures:
    print("ÉCHEC", f)
print(f"{passed} contrôles conformes, {len(failures)} échec(s)")
sys.exit(1 if failures else 0)
