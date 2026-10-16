#!/usr/bin/env python3
"""
Teste l'historique des incidents de trafic (spec R-86, D-17 ; tools/reference/v2_traffic_history.py et
tools/pipeline/traffic_history.py), sans réseau :

1. scénario construit, lecture après lecture : début du suivi, incident, doublons d'IDFM, disparition, réapparition,
   lecture répétée, lacune, perturbations planifiées écartées (travaux, information, annoncée, longue), plusieurs
   lignes, fenêtres de 24 h et de 7 jours, conservation de 8 jours ;
2. sur l'info trafic réelle du 14 octobre 2026 (dépôt complet seulement : réponse et base embarquée de l'application) :
   rattachement identique à l'onglet Trafic, et incidents retenus un par un.

    python3 tests/test_traffic_history.py
"""

from __future__ import annotations

import gzip
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tools" / "reference"), str(ROOT / "tools" / "pipeline")]
import traffic_history as pipeline  # noqa: E402
import v2_traffic as tr  # noqa: E402
import v2_traffic_history as th  # noqa: E402

failures, passed = [], 0


def check(name, got, expected):
    global passed
    if got == expected:
        passed += 1
    else:
        failures.append(f"{name}\n    obtenu  : {got!r}\n    attendu : {expected!r}")


# ---------------------------------------------------------------------------------------------------------------
# 1. Scénario construit (heures de Paris : 14 octobre 2026, heure d'été, UTC+2)
# ---------------------------------------------------------------------------------------------------------------
def local(s: str) -> int:
    return tr.parse_local(s)


def disruption(did, title, begin, end, cause="PERTURBATION", severity="PERTURBEE", extra=()):
    periods = [{"begin": begin, "end": end}] + [{"begin": b, "end": e} for b, e in extra]
    return {"id": did, "title": title, "cause": cause, "severity": severity, "shortMessage": "Trafic perturbé",
            "applicationPeriods": periods, "lastUpdate": begin}


def bulk(entries):
    """entries : {route_id: (shortName, [(niveau, identifiant de l'objet, [perturbations])])}"""
    lines, ds = [], {}
    for route, (short, objs) in entries.items():
        impacted = []
        for level, oid, items in objs:
            impacted.append({"type": level, "id": oid, "name": oid.split(":")[-1], "disruptionIds": [d["id"] for d in items]})
            for d in items:
                ds[d["id"]] = d
        lines.append({"id": "line:" + route, "shortName": short, "impactedObjects": impacted})
    return {"lastUpdatedDate": "2026-10-14T18:00:00Z", "lines": lines, "disruptions": list(ds.values())}


L351, L350 = "IDFM:C01299", "IDFM:C01143"
LINES = [("BUS_351", L351, []), ("BUS_350", L350, [])]
accident = disruption("acc", "Bus 351 : Accident - Trafic perturbé", "20261014T180200", "20261014T180300")
accident_bis = disruption("acc-bis", "Bus 351 : Accident - Trafic perturbé", "20261014T181000", "20261014T181100",
                          severity="BLOQUANTE")
travaux = disruption("trv", "Bus 351 : Travaux", "20261014T170000", "20261014T230000", cause="TRAVAUX")
info = disruption("inf", "Bus 351 : Information", "20261014T170000", "20261014T230000", cause="INFORMATION",
                  severity="INFORMATION")
annonce = disruption("ann", "Bus 351 : Manifestation", "20261015T080000", "20261015T120000")
longue = disruption("lng", "Bus 351 : Déviation", "20261014T180000", "20261020T180000")
commune = disruption("com", "Bus 350 et 351 : Manifestation spontanée", "20261014T181200", "20261014T181300")
avant = disruption("old", "Bus 351 : Incident déjà en cours", "20261014T175000", "20261014T175100")

st = th.new_state()
t0 = local("20261014T180000")
th.observe(st, bulk({L351: ("351", [("line", "line:" + L351, [avant])])}), t0, LINES)
check("début du suivi", (st["tracking_since"], st["disruptions"]["old"]["at_tracking_start"]), (t0, True))
check("déjà publiée au début du suivi : pas un incident", th.is_incident(st["disruptions"]["old"]), False)

t1 = local("20261014T180500")
b1 = bulk({L351: ("351", [("line", "line:" + L351, [avant, accident, travaux, info, annonce, longue])])})
th.observe(st, b1, t1, LINES)
recs = st["disruptions"]
check("incident publié 3 min après son début", (recs["acc"]["announced"], th.is_incident(recs["acc"])), (False, True))
check("travaux, information, annoncée, longue : planifiées",
      [th.is_incident(recs[k]) for k in ("trv", "inf", "ann", "lng")], [False, False, False, False])
check("annoncée : vue plus de 30 min avant son début", recs["ann"]["announced"], True)
check("période d'une minute : toujours en cours tant que publiée", th.interval(recs["acc"]), (local("20261014T180200"), None))

th.observe(st, b1, t1, LINES)
check("lecture répétée (même heure) ignorée", st["observations"], [t0, t1])

t2 = local("20261014T181500")
b2 = bulk({L351: ("351", [("line", "line:" + L351, [accident, accident_bis, commune])]),
           L350: ("350", [("stop_point", "stop_point:IDFM:1", [commune])])})
th.observe(st, b2, t2, LINES)
check("disparue entre deux lectures : fin à la lecture qui le constate", recs["old"]["gone_at"], t2)
check("une perturbation, deux lignes suivies", sorted(recs["com"]["lines"]), ["BUS_350", "BUS_351"])
check("rattachement à l'arrêt sur la 350 (titre sans numéro de ligne)", recs["com"]["lines"]["BUS_350"],
      {"kind": "stop", "stops": ["1"]})

t3 = local("20261014T182000")
th.observe(st, bulk({L351: ("351", [("line", "line:" + L351, [commune])]),
                     L350: ("350", [("stop_point", "stop_point:IDFM:1", [commune])])}), t3, LINES)
check("incident et son doublon disparus à la même lecture", (recs["acc"]["gone_at"], recs["acc-bis"]["gone_at"]), (t3, t3))
w = th.window(st, t3, th.DAY_S)
check("24 h : doublons regroupés, plus récent d'abord", [(g["ids"], g["start"], g["end"], g["severity"]) for g in w["lines"]["BUS_351"]], [
    (["com"], local("20261014T181200"), None, "PERTURBEE"),
    (["acc", "acc-bis"], local("20261014T180200"), t3, "BLOQUANTE"),
])
check("24 h : la 350 compte l'incident partagé", [g["ids"] for g in w["lines"]["BUS_350"]], [["com"]])
check("24 h : suivi commencé dans la fenêtre, donc partiel", w["coverage"], {"tracking_since": t0, "complete": False, "gaps": []})

t4 = t3 + 45 * 60
th.observe(st, bulk({L351: ("351", [("line", "line:" + L351, [commune, accident])])}), t4, LINES)
check("réapparition : de nouveau en cours", recs["acc"]["gone_at"], None)
check("disparue de la 350 seulement : encore publiée sur la 351", recs["com"]["gone_at"], None)
check("lacune de plus de 20 min signalée", th.gaps(st, t4, th.DAY_S), [[t3, t4]])

day = 86400
t5 = t0 + day + 3600
th.observe(st, bulk({}), t5, LINES)
check("lendemain : disparition constatée à la lecture suivante, après la lacune", (recs["acc"]["gone_at"], recs["com"]["gone_at"]), (t5, t5))
check("lendemain : un incident terminé il y a plus de 24 h (le doublon disparu à 18:20) sort de la fenêtre de 24 h",
      sorted(g["ids"][0] for g in th.window(st, t5, th.DAY_S)["lines"].get("BUS_351", [])), ["acc", "com"])
check("7 jours : même compte", sorted(g["ids"][0] for g in th.window(st, t5, th.WEEK_S)["lines"]["BUS_351"]), ["acc", "com"])

t6 = t5 + 7 * day + 60
th.observe(st, bulk({}), t6, LINES)
check("7 jours glissants : terminé il y a plus de 7 jours, sorti", th.window(st, t6, th.WEEK_S)["lines"], {})
t7 = t5 + 8 * day + 60
th.observe(st, bulk({}), t7, LINES)
check("conservation : 8 jours de lectures et de perturbations", (st["observations"][0] >= t7 - th.RETAIN_S, st["disruptions"]), (True, {}))
check("début du suivi conservé", st["tracking_since"], t0)

# Ligne de commande : état écrit, relu, format vérifié
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    (tmp / "bulk.json").write_text(json.dumps(b1), encoding="utf-8")
    base = ROOT / "app/src/main/assets/timetable/timetable.sqlite.gz"
    if base.exists():
        tool = [sys.executable, "-I", str(ROOT / "tools/pipeline/traffic_history.py"), "--state", str(tmp / "h.json"),
                "--axes", str(tmp / "axes.json")]
        r = subprocess.run(tool + ["--bulk", str(tmp / "bulk.json"), "--observed", "2026-10-14T16:05:00Z",
                                   "--timetable", str(base), "--base-version", "v1"], capture_output=True, text=True)
        axes = json.loads((tmp / "axes.json").read_text())
        check("ligne de commande : premier passage, missions des RER écrites",
              (r.returncode, json.loads((tmp / "h.json").read_text())["tracking_since"], axes["base_version"], sorted(axes["patterns"])),
              (0, local("20261014T180500"), "v1", ["RER_B", "RER_C"]))
        r = subprocess.run(tool + ["--bulk", str(tmp / "bulk.json"), "--observed", "2026-10-14T16:10:00Z"], capture_output=True, text=True)
        check("ligne de commande : missions relues sans la base", (r.returncode, len(json.loads((tmp / "h.json").read_text())["observations"])), (0, 2))
        (tmp / "bad.json").write_text("{}", encoding="utf-8")
        r = subprocess.run(tool + ["--bulk", str(tmp / "bad.json"), "--observed", "2026-10-14T16:15:00Z"], capture_output=True, text=True)
        check("ligne de commande : réponse illisible refusée, état intact",
              (r.returncode, len(json.loads((tmp / "h.json").read_text())["observations"])), (1, 2))

# ---------------------------------------------------------------------------------------------------------------
# 2. Info trafic réelle du 14 octobre 2026 (dépôt complet)
# ---------------------------------------------------------------------------------------------------------------
FIXTURE = ROOT / "app/src/test/resources/traffic/2026-10-14/bulk-lignes-suivies.json.gz"
BASE = ROOT / "app/src/main/assets/timetable/timetable.sqlite.gz"
if FIXTURE.exists() and BASE.exists():
    real = json.loads(gzip.decompress(FIXTURE.read_bytes()).decode("utf-8"))
    lines = pipeline.tracked_lines(ROOT / "data", pipeline.axes_patterns(BASE))
    now = 1792002017                                   # 2026-10-14T18:20:17Z, lecture du relevé
    same = True
    for code, route, pats in lines:
        shown = {x["id"] for x in tr.line_disruptions(real, code, route, pats, now)}
        attached = {a["id"]: a for a in tr.attached_disruptions(real, code, route, pats)}
        hidden = {i for i, a in attached.items() if tr.timing(a["disruption"], now)["state"] == "hidden"}
        same &= shown == set(attached) - hidden
    check("réel : rattachement et filtre d'axe identiques à l'onglet Trafic", same, True)
    st = th.new_state()
    th.observe(st, {"lines": [], "disruptions": []}, now - 300, lines)
    th.observe(st, real, now, lines)
    w = th.window(st, now, th.DAY_S)
    check("réel : incidents sur 24 h (suivi commencé 5 min avant)", {k: [(len(g["ids"]), g["title"]) for g in v] for k, v in w["lines"].items()}, {
        "BUS_152": [(3, "Bus 152 : Mesures de sécurité - Arrêt(s) non desservi(s)")],
        "T7": [(1, "Tramway T7 : Malaise voyageur - Trafic perturbé")],
    })
    planned = {r["title"][:40] for r in st["disruptions"].values() if r["cause"] == "PERTURBATION" and not th.is_incident(r)}
    check("réel : perturbations « PERTURBATION » planifiées écartées", sorted(planned), sorted({
        "Bus 152 : Manifestation - Arrêt(s) non d",          # annoncée : périodes du 16 octobre au 13 novembre
        "Modifications de vos horaires durant les",          # annoncée : à partir du 17 octobre
        "Terres d'Envol - Ligne 609 - Perturbatio",          # annoncée : le lendemain
        "Terres d'Envol - Lignes 1, 619, 642, TB1",          # annoncée : le lendemain
        "Terres d'Envol - Travaux - Lignes 610 et",          # longue : du 2 septembre au 2 novembre
        "#Infotrafic ligne 9517",                            # longue
        "Transdev Express Roissy Ligne 9517",                # longue
        "⚠️🚍#InfoTrafic - Ligne 9112 - Du 24 avri",           # longue (40 caractères, émojis compris)
        "Info trafic 351 : arrêts non desservis B",          # longue : du 12 août au 30 octobre
        "Tramway T11 -  Mouvement social du 12/10",          # longue : une semaine
        "Tram T9 - Travaux : Service partiellemen",          # sévérité « INFORMATION », annoncée
    }))
    check("réel : aucune lacune", w["coverage"]["gaps"], [])
else:
    print("Info trafic réelle absente (dépôt public) : partie 2 non jouée")

for f in failures:
    print("ÉCHEC", f)
print(f"{passed} contrôles conformes, {len(failures)} échec(s)")
sys.exit(1 if failures else 0)
