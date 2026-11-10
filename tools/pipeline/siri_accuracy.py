#!/usr/bin/env python3
"""
Relevé d'exactitude du temps réel (spec R-159, 10 novembre 2026) : ce que le service « Prochains passages » d'IDFM
annonçait 10, 5, 3, 2 et 1 min avant le départ d'un véhicule, comparé à sa dernière annonce avant qu'il quitte le quai.

    python3 tools/pipeline/siri_accuracy.py active --config data/siri-accuracy.json
    python3 tools/pipeline/siri_accuracy.py plan --config data/siri-accuracy.json --timetable timetable.sqlite.gz \\
        --base-version V --out quais.json
    python3 tools/pipeline/siri_accuracy.py watch --quais quais.json --proxy URL --minutes 30 --out track.json
    python3 tools/pipeline/siri_accuracy.py update --track track.json --state state.json.gz \\
        [--summary-json summary.json] [--summary-md summary.md]

- `plan` : les quais suivis, choisis comme ceux du relevé SIRI (`siri_survey.plan`) ;
- `watch` : relit chaque quai toutes les `INTERVAL_S` secondes pendant `--minutes`, à travers le proxy (R-151), comme
  l'application (mêmes réponses partagées, R-84), série arrêtée au premier HTTP 429 (R-83) ; garde pour chaque course
  (DatedVehicleJourneyRef) la suite de ses annonces ;
- `update` : ajoute les courses parties pendant la veille à l'état du relevé et récrit les synthèses.

Mesures, par ligne :

- **écart d'annonce** à 10, 5, 3, 2 et 1 min : dernière annonce de la course moins l'annonce lue à cette distance du
  départ (à `LEAD_TOLERANCE_S` près), en secondes ; positif, le véhicule est parti plus tard qu'annoncé. Seules comptent
  les annonces que l'application afficherait « temps réel » (R-159) ;
- **âge de la mesure** : heure de la réponse moins `RecordedAtTime`, pour ces mêmes annonces.

Limite, à garder en tête en lisant les chiffres : le service ne donne pas l'heure de départ réelle. Le « départ » est
la dernière annonce lue avant que la course disparaisse du quai, connue à l'intervalle de lecture près (30 s) plus la
durée de partage du proxy (25 s). Une course qui disparaît alors que son départ annoncé est à plus de
`DEPARTED_WINDOW_S` de sa dernière lecture n'est pas comptée (« disparue »). Bibliothèque standard uniquement.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "pipeline"))
sys.path.insert(0, str(ROOT / "tools" / "reference"))
import siri_survey as ss  # noqa: E402
import v2_realtime as rt  # noqa: E402

SCHEMA = "airport-connect/siri-accuracy@1"
INTERVAL_S = 30
SPACING_S = 0.3
LEADS_S = [600, 300, 180, 120, 60]
LEAD_TOLERANCE_S = 20
DEPARTED_WINDOW_S = 120          # la course a disparu du quai à 2 min au plus de son départ annoncé : elle est partie
MIN_MISSED_POLLS = 2             # absente de deux lectures réussies de suite : elle n'est plus au quai
AGE_BOUNDS_S = [15, 30, 60, 120, 300]
AGE_STEP_S = 5                   # l'âge des mesures est compté par tranches de 5 s, jusqu'à AGE_CAP_S
AGE_CAP_S = 900
ERROR_BOUNDS_S = [30, 60, 120]


# -- veille ------------------------------------------------------------------------------------------------------
def read_quai(proxy: str, sp: str, opener=urllib.request.urlopen):
    """(statut HTTP, texte) d'une lecture de quai ; (None, None) sur une erreur de réseau."""
    url = f"{proxy.rstrip('/')}/stop-monitoring?" + urllib.parse.urlencode({"MonitoringRef": rt.monitoring_ref(sp)})
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "AirportConnect-SiriAccuracy/1"})
    try:
        with opener(req, timeout=ss.TIMEOUT_S) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, TimeoutError, OSError):
        return None, None


def watch(quais: dict, proxy: str, minutes: float, clock=time.time, sleep=time.sleep, opener=urllib.request.urlopen) -> dict:
    """Suite des annonces de chaque course, par quai : `polls` (heures des lectures réussies du quai) et `journeys`
    (course → liste de [heure de la réponse, heure annoncée, mesurée (R-159), heure d'enregistrement])."""
    start = clock()
    track = {q["stop_point"]: {"line": q["line"], "polls": [], "journeys": {}} for q in quais["quais"]}
    by_sp = {q["stop_point"]: q for q in quais["quais"]}
    requests = errors = 0
    limited = False
    while not limited and clock() - start < minutes * 60:
        cycle = clock()
        for i, (sp, q) in enumerate(by_sp.items()):
            if i:
                sleep(SPACING_S)
            status, text = read_quai(proxy, sp, opener)
            requests += 1
            if status == 429:
                limited = True               # R-83 : arrêt immédiat de la veille
                break
            parsed = ss.observations(text, q, int(clock())) if status == 200 else None
            if parsed is None:
                errors += 1
                continue
            reference, visits = parsed
            t = track[sp]
            t["polls"].append(reference)
            for v in visits:
                if v["journey"] is None or v["expected"] is None or v["class"] in ("supprime", "noreport"):
                    continue
                t["journeys"].setdefault(v["journey"], []).append(
                    [reference, v["expected"], v["class"] in ss.MEASURED, v["recorded"]])
        rest = INTERVAL_S - (clock() - cycle)
        if rest > 0 and not limited:
            sleep(rest)
    return {"schema": SCHEMA, "started_at": int(start), "ended_at": int(clock()), "requests": requests,
            "errors": errors, "rate_limited": limited, "quais": track}


# -- mesures -----------------------------------------------------------------------------------------------------
def departed(samples: list, polls: list) -> bool:
    """La course a quitté le quai pendant la veille : absente d'au moins MIN_MISSED_POLLS lectures réussies après sa
    dernière annonce, laquelle donnait un départ à DEPARTED_WINDOW_S au plus de cette lecture."""
    last_ref, last_expected = samples[-1][0], samples[-1][1]
    missed = sum(1 for p in polls if p > last_ref)
    return missed >= MIN_MISSED_POLLS and abs(last_expected - last_ref) <= DEPARTED_WINDOW_S


def lead_errors(samples: list) -> dict:
    """Écart d'annonce à chaque distance du départ : {600: +35, …}, pour les distances où une annonce mesurée a été lue."""
    final = samples[-1][1]
    out = {}
    for lead in LEADS_S:
        target = final - lead
        ref, expected, measured, _ = min(samples, key=lambda s: abs(s[0] - target))
        if abs(ref - target) <= LEAD_TOLERANCE_S and measured:
            out[lead] = final - expected
    return out


def empty_state() -> dict:
    return {"schema": SCHEMA, "runs": [], "lines": {}}


def load_state(path: Path) -> dict:
    if not path.exists() or path.stat().st_size == 0:
        return empty_state()
    with gzip.open(path, "rt", encoding="utf-8") as f:
        state = json.load(f)
    if state.get("schema") != SCHEMA:
        raise SystemExit(f"état d'un autre format : {state.get('schema')}")
    return state


def update(state: dict, track: dict) -> dict:
    state["runs"].append({"started_at": track["started_at"], "ended_at": track["ended_at"],
                          "requests": track["requests"], "errors": track["errors"], "rate_limited": track["rate_limited"]})
    for t in track["quais"].values():
        line = state["lines"].setdefault(t["line"], {"departed": 0, "vanished": 0, "unmeasured": 0, "errors": {}, "ages": {}})
        for samples in t["journeys"].values():
            if not any(p > samples[-1][0] for p in t["polls"]):
                continue                                   # encore au quai à la fin de la veille : ni partie ni disparue
            if not departed(samples, t["polls"]):
                line["vanished"] += 1
                continue
            line["departed"] += 1
            if not any(s[2] for s in samples):
                line["unmeasured"] += 1                    # jamais affichée « temps réel » : horaire recopié
                continue
            for lead, err in lead_errors(samples).items():
                line["errors"].setdefault(str(lead), []).append(err)
            final = samples[-1][1]
            for ref, _, measured, rec in samples:
                # âge des annonces mesurées lues dans les 10 min avant le départ, celles qui décident le voyageur
                if measured and rec is not None and ref >= rec and final - ref <= LEADS_S[0]:
                    bucket = str(min(ref - rec, AGE_CAP_S) // AGE_STEP_S * AGE_STEP_S)
                    line["ages"][bucket] = line["ages"].get(bucket, 0) + 1
    return state


def median(values: list):
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def percentile(values: list, p: float):
    """Centile par rang le plus proche (p entre 0 et 100)."""
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, max(0, math.ceil(len(s) * p / 100) - 1))]


def histogram_percentile(hist: dict, p: float):
    """Centile d'un histogramme {borne basse de la tranche: nombre} (borne basse de la tranche atteinte)."""
    total = sum(hist.values())
    if not total:
        return None
    rank, seen = math.ceil(total * p / 100), 0
    for bucket in sorted(hist, key=int):
        seen += hist[bucket]
        if seen >= rank:
            return int(bucket)
    return None


def summarize(state: dict) -> dict:
    lines = {}
    for code, line in sorted(state["lines"].items()):
        leads = {}
        for lead in LEADS_S:
            errs = line["errors"].get(str(lead), [])
            absolute = [abs(e) for e in errs]
            leads[str(lead)] = {
                "n": len(errs), "median_abs_s": median(absolute), "p90_abs_s": percentile(absolute, 90),
                "median_signed_s": median(errs),
                **{f"within_{b}s": (sum(1 for a in absolute if a <= b) / len(errs) if errs else None) for b in ERROR_BOUNDS_S},
            }
        ages = line["ages"]
        n = sum(ages.values())
        lines[code] = {"departed": line["departed"], "vanished": line["vanished"], "unmeasured": line["unmeasured"],
                       "leads": leads, "age_n": n, "age_median_s": histogram_percentile(ages, 50),
                       "age_p90_s": histogram_percentile(ages, 90),
                       **{f"age_within_{b}s": (sum(c for a, c in ages.items() if int(a) < b) / n if n else None)
                          for b in AGE_BOUNDS_S}}
    runs = state["runs"]
    return {"schema": SCHEMA, "runs": len(runs), "from": runs[0]["started_at"] if runs else None,
            "to": runs[-1]["ended_at"] if runs else None, "requests": sum(r["requests"] for r in runs),
            "errors": sum(r["errors"] for r in runs), "rate_limited": sum(1 for r in runs if r["rate_limited"]), "lines": lines}


def _s(x) -> str:
    return "—" if x is None else f"{x:.0f} s"


def _pct(x) -> str:
    return "—" if x is None else f"{round(x * 100)} %"


def markdown(summary: dict) -> str:
    iso = lambda e: "—" if e is None else datetime.fromtimestamp(e, ss.PARIS).strftime("%Y-%m-%d %H:%M")  # noqa: E731
    out = ["# Relevé d'exactitude du temps réel", "",
           f"{summary['runs']} veille(s), du {iso(summary['from'])} au {iso(summary['to'])} (heure de Paris). "
           f"Requêtes : {summary['requests']}, dont {summary['errors']} sans réponse exploitable ; "
           f"{summary['rate_limited']} veille(s) arrêtée(s) par un 429.", "",
           "Le « départ » est la dernière annonce lue avant que la course quitte le quai, connue à 30 s près (intervalle "
           "de lecture) plus 25 s (partage du proxy). Écart positif : parti plus tard qu'annoncé.", "",
           "## Écart entre l'annonce et le départ (médiane, 9 départs sur 10, part à 60 s près)", "",
           "| Ligne | Courses | Jamais mesurées | Disparues | " + " | ".join(f"{l // 60} min avant" for l in LEADS_S) + " |",
           "|---|---|---|---|" + "---|" * len(LEADS_S)]
    for code, line in summary["lines"].items():
        cells = []
        for lead in LEADS_S:
            c = line["leads"][str(lead)]
            cells.append("—" if not c["n"] else
                         f"{c['median_abs_s']:.0f} s ; {c['p90_abs_s']:.0f} s ; {_pct(c['within_60s'])} ({c['n']})")
        out.append(f"| {code} | {line['departed']} | {line['unmeasured']} | {line['vanished']} | " + " | ".join(cells) + " |")
    out += ["", "## Sens de l'écart (médiane signée)", "",
            "| Ligne | " + " | ".join(f"{l // 60} min avant" for l in LEADS_S) + " |", "|---|" + "---|" * len(LEADS_S)]
    for code, line in summary["lines"].items():
        out.append(f"| {code} | " + " | ".join(
            "—" if line["leads"][str(l)]["median_signed_s"] is None else f"{line['leads'][str(l)]['median_signed_s']:+.0f} s"
            for l in LEADS_S) + " |")
    out += ["", "## Âge de la mesure à la lecture (dans les 10 min avant le départ, par tranches de 5 s)", "",
            "| Ligne | Annonces | Médiane | 9 sur 10 | " + " | ".join(f"< {b} s" for b in AGE_BOUNDS_S) + " |",
            "|---|---|---|---|" + "---|" * len(AGE_BOUNDS_S)]
    for code, line in summary["lines"].items():
        out.append(f"| {code} | {line['age_n']} | {_s(line['age_median_s'])} | {_s(line['age_p90_s'])} | "
                   + " | ".join(_pct(line[f"age_within_{b}s"]) for b in AGE_BOUNDS_S) + " |")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("active")
    a.add_argument("--config", type=Path, required=True)
    p = sub.add_parser("plan")
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--timetable", type=Path, required=True)
    p.add_argument("--base-version", required=True)
    p.add_argument("--out", type=Path, required=True)
    w = sub.add_parser("watch")
    w.add_argument("--quais", type=Path, required=True)
    w.add_argument("--proxy", required=True)
    w.add_argument("--minutes", type=float, required=True)
    w.add_argument("--out", type=Path, required=True)
    u = sub.add_parser("update")
    u.add_argument("--track", type=Path, required=True)
    u.add_argument("--state", type=Path, required=True)
    u.add_argument("--summary-json", type=Path)
    u.add_argument("--summary-md", type=Path)
    args = ap.parse_args()

    if args.cmd == "active":
        print("true" if ss.active(ss.read_json(args.config), datetime.now(ss.PARIS).date()) else "false")
    elif args.cmd == "plan":
        with tempfile.TemporaryDirectory() as tmp:
            con = ss.open_db(args.timetable, Path(tmp))
            result = ss.plan(con, ss.read_json(args.config), args.base_version)
            con.close()
        result["schema"] = SCHEMA
        ss.write_json(args.out, result)
        print(f"{len(result['quais'])} quais suivis ; lignes sans quai : {', '.join(result['missing']) or 'aucune'}")
    elif args.cmd == "watch":
        track = watch(ss.read_json(args.quais), args.proxy, args.minutes)
        ss.write_json(args.out, track)
        print(f"{track['requests']} requêtes, {track['errors']} sans réponse exploitable, "
              f"{sum(len(t['journeys']) for t in track['quais'].values())} course(s) vue(s)"
              f"{' ; arrêt sur 429' if track['rate_limited'] else ''}")
    else:
        state = update(load_state(args.state), ss.read_json(args.track))
        ss.save_state(state, args.state)
        summary = summarize(state)
        if args.summary_json:
            ss.write_json(args.summary_json, summary)
        if args.summary_md:
            args.summary_md.write_text(markdown(summary), encoding="utf-8")
        print(f"{summary['runs']} veille(s) ; {sum(l['departed'] for l in summary['lines'].values())} course(s) parties")
    return 0


if __name__ == "__main__":
    sys.exit(main())
