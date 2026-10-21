#!/usr/bin/env python3
"""
Relevé du service « Prochains passages » d'IDFM (SIRI Lite stop-monitoring), pour calibrer la règle stricte du temps
réel (spec R-159, décision D-4) sur des mesures plutôt qu'au jugé.

    python3 tools/pipeline/siri_survey.py active --config data/siri-survey.json
    python3 tools/pipeline/siri_survey.py plan --config data/siri-survey.json --timetable timetable.sqlite.gz \\
        --base-version V --out quais.json
    python3 tools/pipeline/siri_survey.py fetch --quais quais.json --proxy URL --out-dir responses
    python3 tools/pipeline/siri_survey.py update --quais quais.json --responses responses --state state.json.gz \\
        [--summary-json summary.json] [--summary-md summary.md]

- `active` : écrit « true » tant que la date de fin du relevé (`until`, heure de Paris) n'est pas dépassée ;
- `plan` : choisit dans la base d'horaires publiée les quais relevés, `quais_per_line` par ligne, un par sens si
  possible, ceux des aéroports d'abord (les mêmes d'un passage à l'autre tant que la base ne change pas) ;
- `fetch` : une requête par quai à travers le proxy (R-151), l'une après l'autre, 1 s d'écart, série arrêtée au
  premier HTTP 429 (R-83) ; les réponses et leur index sont écrits dans `--out-dir` ;
- `update` : ajoute les réponses à l'état du relevé (absent au premier passage) et récrit les synthèses.

Chaque passage lu de la ligne relevée est classé comme le fait l'application (R-159, `tools/reference/v2_realtime.py`) :
« écart » (heure annoncée différente de l'heure prévue), « récent » (égale, enregistrée 10 min au plus avant la
réponse), « sans prévue » (heure annoncée seule) — les trois sont mesurés — ou « ancien » (égale, enregistrée plus
tôt), « noReport », « supprimé ». L'état garde aussi chaque course (DatedVehicleJourneyRef) et ses lectures
successives, quand elles changent : une course passée en « écart » après des lectures « anciennes » montre que la
règle a caché un suivi réel, ce qui sert à fixer le seuil. Bibliothèque standard uniquement.
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "reference"))
import v2_realtime as rt  # noqa: E402

SCHEMA = "airport-connect/siri-survey@1"
PARIS = ZoneInfo("Europe/Paris")
CLASSES = ["ecart", "recent", "sans_prevue", "ancien", "noreport", "supprime"]
MEASURED = {"ecart", "recent", "sans_prevue"}
# Âge de l'enregistrement d'un passage « ancien » (minutes) : bornes hautes des tranches, la dernière ouverte.
AGE_BUCKETS = [20, 30, 60, 120]
# Tranches horaires de la synthèse (heure de Paris) : début inclus.
HOUR_BANDS = [(0, "0 h – 5 h"), (5, "5 h – 9 h"), (9, "9 h – 16 h"), (16, "16 h – 20 h"), (20, "20 h – 24 h")]
SPACING_S = 1.0
TIMEOUT_S = 30


# -- calendrier --------------------------------------------------------------------------------------------------
def active(config: dict, today: date) -> bool:
    return today <= date.fromisoformat(config["until"])


# -- choix des quais ---------------------------------------------------------------------------------------------
def open_db(timetable: Path, tmp: Path) -> sqlite3.Connection:
    db = tmp / "t.sqlite"
    if timetable.suffix == ".gz":
        with gzip.open(timetable, "rb") as fi, open(db, "wb") as fo:
            shutil.copyfileobj(fi, fo)
    else:
        shutil.copyfile(timetable, db)
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def plan(con: sqlite3.Connection, config: dict, base_version: str) -> dict:
    """Quais relevés : pour chaque ligne, les quais où l'on peut monter, groupés par sens ; dans chaque sens, ceux
    d'un lieu aéroportuaire d'abord (ordre des stop_id), puis les autres, les plus desservis d'abord (nombre de
    courses, puis stop_id). On prend à tour de rôle le premier quai de chaque sens, jusqu'à `quais_per_line`."""
    per_line = int(config["quais_per_line"])
    quais, missing = [], []
    for group, codes in config["groups"].items():
        for code in codes:
            row = con.execute("SELECT route_id FROM line WHERE code = ? AND state = 'active'", (code,)).fetchone()
            if row is None:
                missing.append(code)
                continue
            by_dir = defaultdict(list)
            names = {}
            for direction, sp, name, airport, trips in con.execute(
                    """SELECT p.direction, sp.gtfs_id, sp.name,
                              EXISTS (SELECT 1 FROM place_stop_point x JOIN place pl ON pl.id = x.place_id
                                      WHERE x.stop_point_id = sp.id AND pl.kind = 'airport'),
                              (SELECT COUNT(*) FROM trip t JOIN pattern_stop z ON z.pattern_id = t.pattern_id
                               JOIN pattern q ON q.id = t.pattern_id
                               WHERE z.stop_point_id = sp.id AND z.pickup != 1 AND q.line_id = l.id
                                 AND q.direction IS p.direction)
                       FROM pattern_stop ps JOIN stop_point sp ON sp.id = ps.stop_point_id
                       JOIN pattern p ON p.id = ps.pattern_id JOIN line l ON l.id = p.line_id
                       WHERE l.code = ? AND ps.pickup != 1
                       GROUP BY p.direction, sp.id""", (code,)):
                if rt.monitoring_ref(sp) is not None:
                    by_dir[-1 if direction is None else direction].append((not airport, -trips if not airport else 0, sp))
                    names[sp] = name
            queues = [[sp for *_, sp in sorted(by_dir[d])] for d in sorted(by_dir)]
            chosen = []
            while len(chosen) < per_line and any(queues):
                for q in queues:
                    while q and q[0] in chosen:
                        q.pop(0)
                    if q and len(chosen) < per_line:
                        chosen.append(q.pop(0))
            if not chosen:
                missing.append(code)
            for sp in chosen:
                quais.append({"group": group, "line": code, "route_id": row[0], "stop_point": sp, "name": names[sp]})
    return {"schema": SCHEMA, "base_version": base_version, "quais": quais, "missing": missing}


# -- lecture à travers le proxy ----------------------------------------------------------------------------------
def fetch(quais: dict, proxy: str, out_dir: Path, sleep=time.sleep, opener=urllib.request.urlopen) -> dict:
    """Une requête par quai distinct (un quai relevé pour deux lignes n'est lu qu'une fois)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    entries, stopped = [], False
    for i, sp in enumerate(dict.fromkeys(q["stop_point"] for q in quais["quais"])):
        entry = {"stop_point": sp}
        if stopped:
            entry["outcome"] = "skipped"
            entries.append(entry)
            continue
        if i:
            sleep(SPACING_S)
        url = f"{proxy.rstrip('/')}/stop-monitoring?" + urllib.parse.urlencode({"MonitoringRef": rt.monitoring_ref(sp)})
        req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "AirportConnect-SiriSurvey/1"})
        try:
            with opener(req, timeout=TIMEOUT_S) as resp:
                body, status = resp.read(), resp.status
        except urllib.error.HTTPError as e:
            body, status = e.read() if e.fp else b"", e.code
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            entry.update(outcome="network_error", error=str(e)[:200], received_at=int(time.time()))
            entries.append(entry)
            continue
        entry.update(http_status=status, received_at=int(time.time()))
        if status == 429:
            entry["outcome"] = "rate_limited"
            stopped = True                       # R-83 : arrêt immédiat de la série
        elif status == 200:
            name = f"{i:03d}.json"
            (out_dir / name).write_bytes(body)
            entry.update(outcome="ok", file=name)
        else:
            entry["outcome"] = "http_error"
        entries.append(entry)
    index = {"schema": SCHEMA, "entries": entries}
    (out_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return index


# -- classement des passages -------------------------------------------------------------------------------------
def classify(aimed, expected, status: str, recorded, reference: int) -> str:
    """Classe d'un passage, alignée sur R-159 (`rt.parse_stop_monitoring`) : mesuré ⇔ classe dans MEASURED."""
    if status == "cancelled":
        return "supprime"
    if expected is None or status == "noReport":
        return "noreport"
    if aimed is None:
        return "sans_prevue"
    if expected != aimed:
        return "ecart"
    if recorded is not None and reference - recorded <= rt.RECORDED_FRESH_S:
        return "recent"
    return "ancien"


def observations(text: str, quai: dict, now: int) -> tuple:
    """Passages de la ligne relevée au quai, dans l'ordre de la réponse : (heure de la réponse, liste) ; None si la
    réponse est en erreur. Mêmes garde-fous que l'application (R-154), sans le dédoublonnage : chaque course compte."""
    sp, route = quai["stop_point"], quai["route_id"]
    try:
        doc = json.loads(text)
        delivery = doc["Siri"]["ServiceDelivery"]
        smd = delivery["StopMonitoringDelivery"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    if not isinstance(smd, dict) or smd.get("ErrorCondition") is not None:
        return None
    reference = rt.parse_time(delivery.get("ResponseTimestamp"))
    reference = reference if reference is not None else now
    raw = smd.get("MonitoredStopVisit") or []
    if not isinstance(raw, list):
        return None
    out = []
    for item in raw:
        try:
            mvj = item["MonitoredVehicleJourney"]
            call = mvj["MonitoredCall"]
        except (KeyError, TypeError):
            continue
        if not isinstance(call, dict) or rt.stop_id_of_ref(rt._value(item.get("MonitoringRef"))) != sp:
            continue
        if rt.route_id_of_line_ref(rt._value(mvj.get("LineRef"))) != route:
            continue
        aimed_raw, expected_raw = call.get("AimedDepartureTime"), call.get("ExpectedDepartureTime")
        if aimed_raw is None and expected_raw is None:
            continue                                              # arrivée seule (terminus du véhicule)
        aimed = rt.parse_time(aimed_raw) if aimed_raw is not None else None
        expected = rt.parse_time(expected_raw) if expected_raw is not None else None
        if (aimed_raw is not None and aimed is None) or (expected_raw is not None and expected is None):
            continue
        t = expected if expected is not None else aimed
        if abs(t - now) > rt.GHOST_S:
            continue
        status = call.get("DepartureStatus") if isinstance(call.get("DepartureStatus"), str) else ""
        recorded = rt.parse_time(item.get("RecordedAtTime"))
        fvj = mvj.get("FramedVehicleJourneyRef") if isinstance(mvj.get("FramedVehicleJourneyRef"), dict) else {}
        journey = fvj.get("DatedVehicleJourneyRef") if isinstance(fvj.get("DatedVehicleJourneyRef"), str) else None
        out.append({"journey": journey, "destination": rt.stop_id_of_ref(rt._value(mvj.get("DestinationRef"))),
                    "aimed": aimed, "expected": expected, "status": status, "recorded": recorded,
                    "class": classify(aimed, expected, status, recorded, reference)})
    return reference, out


# -- état du relevé ----------------------------------------------------------------------------------------------
def empty_state() -> dict:
    return {"schema": SCHEMA, "runs": [], "counts": {}, "journeys": {}}


def load_state(path: Path) -> dict:
    if not path.exists() or path.stat().st_size == 0:
        return empty_state()
    with gzip.open(path, "rt", encoding="utf-8") as f:
        state = json.load(f)
    if state.get("schema") != SCHEMA:
        raise SystemExit(f"état d'un autre format : {state.get('schema')}")
    return state


def save_state(state: dict, path: Path) -> None:
    # mtime=0 : même contenu, même fichier (le workflow ne publie que s'il change)
    with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as f:
        f.write(json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def hour_band(epoch: int) -> str:
    h = datetime.fromtimestamp(epoch, PARIS).hour
    return [label for start, label in HOUR_BANDS if h >= start][-1]


def age_bucket(age_s: int) -> str:
    minutes = age_s / 60
    for upper in AGE_BUCKETS:
        if minutes <= upper:
            return f"≤ {upper} min"
    return f"> {AGE_BUCKETS[-1]} min"


def update(state: dict, quais: dict, index: dict, responses: Path, now: int) -> dict:
    by_sp = defaultdict(list)
    for q in quais["quais"]:
        by_sp[q["stop_point"]].append(q)
    run = Counter()
    for entry in index["entries"]:
        run[entry["outcome"]] += 1
        if entry["outcome"] != "ok":
            continue
        text = (responses / entry["file"]).read_text(encoding="utf-8")
        for q in by_sp.get(entry["stop_point"], []):
            if not record(state, q, observations(text, q, entry.get("received_at", now))):
                run["error_condition"] += 1
                break
    state["runs"].append({"at": now, "base_version": quais["base_version"], **dict(sorted(run.items()))})
    return state


def record(state: dict, q: dict, parsed) -> bool:
    """Ajoute à l'état les passages lus d'une ligne à un quai ; faux si la réponse est en erreur."""
    if parsed is None:
        return False
    reference, obs = parsed
    line = state["counts"].setdefault(q["line"], {"group": q["group"], "bands": {}, "ages": {}, "empty_reads": 0})
    if not obs:
        line["empty_reads"] += 1
        return True
    band = line["bands"].setdefault(hour_band(reference), {c: 0 for c in CLASSES})
    for o in obs:
        band[o["class"]] += 1
        if o["class"] == "ancien" and o["recorded"] is not None:
            b = age_bucket(reference - o["recorded"])
            line["ages"][b] = line["ages"].get(b, 0) + 1
        when = o["aimed"] if o["aimed"] is not None else o["expected"]
        key = "|".join([q["stop_point"], q["line"], o["journey"] or "", str(when)])
        j = state["journeys"].setdefault(key, {"line": q["line"], "stop_point": q["stop_point"],
                                                "journey": o["journey"], "destination": o["destination"],
                                                "aimed": o["aimed"], "samples": []})
        sample = [reference, o["expected"], o["status"], o["recorded"], o["class"]]
        if not j["samples"] or j["samples"][-1][1:4] != sample[1:4]:
            j["samples"].append(sample)
        j["last_seen"] = reference
    return True


# -- synthèse ----------------------------------------------------------------------------------------------------
def summarize(state: dict) -> dict:
    lines = {}
    journeys_of = defaultdict(list)
    for j in state["journeys"].values():
        journeys_of[j["line"]].append(j)
    for code, c in sorted(state["counts"].items()):
        totals = Counter()
        for band in c["bands"].values():
            totals.update(band)
        n = sum(totals.values())
        js = journeys_of[code]
        tracked = [j for j in js if any(s[4] in MEASURED for s in j["samples"])]
        updated = [j for j in js if len({(s[1], s[3]) for s in j["samples"]}) > 1]
        hidden = [j for j in js if any(s[4] == "ancien" for s in j["samples"])
                  and any(s[4] == "ecart" for s in j["samples"])]
        lines[code] = {
            "group": c["group"], "observations": n, "empty_reads": c["empty_reads"],
            "classes": {k: totals.get(k, 0) for k in CLASSES},
            "measured_share": round(sum(totals[k] for k in MEASURED) / n, 3) if n else None,
            "bands": {label: {"observations": sum(b.values()),
                              "measured_share": round(sum(b[k] for k in MEASURED) / sum(b.values()), 3)
                              if sum(b.values()) else None}
                      for _, label in HOUR_BANDS if (b := c["bands"].get(label))},
            "ancien_ages": {k: c["ages"][k] for k in [f"≤ {u} min" for u in AGE_BUCKETS] + [f"> {AGE_BUCKETS[-1]} min"]
                            if k in c["ages"]},
            "journeys": len(js), "journeys_measured": len(tracked), "journeys_updated": len(updated),
            "journeys_ancien_then_ecart": len(hidden),
        }
    runs = state["runs"]
    return {"schema": SCHEMA, "runs": len(runs),
            "first_run": runs[0]["at"] if runs else None, "last_run": runs[-1]["at"] if runs else None,
            "requests": {k: sum(r.get(k, 0) for r in runs)
                         for k in ["ok", "http_error", "network_error", "rate_limited", "skipped", "error_condition"]},
            "lines": lines}


def iso(epoch) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if epoch else "—"


def pct(x) -> str:
    return "—" if x is None else f"{round(100 * x)} %"


def markdown(summary: dict) -> str:
    out = ["# Relevé SIRI (R-159, D-4)", "",
           f"{summary['runs']} passage(s), du {iso(summary['first_run'])} au {iso(summary['last_run'])}. "
           f"Requêtes : {summary['requests']['ok']} lues, {summary['requests']['http_error']} en erreur HTTP, "
           f"{summary['requests']['network_error']} en échec réseau, {summary['requests']['rate_limited']} refusées "
           f"(429), {summary['requests']['skipped']} sautées, {summary['requests']['error_condition']} en erreur SIRI.",
           "",
           "Mesuré au sens de R-159 : « écart », « récent » ou « sans prévue ». Une course « ancien puis écart » a "
           "d'abord été lue avec une heure annoncée égale à l'heure prévue et un enregistrement de plus de 10 min, "
           "puis avec un écart : la règle stricte en a caché le suivi.", "",
           "| Ligne | Groupe | Passages | Mesurés | Écart | Récent | Ancien | noReport | Courses | Courses mesurées "
           "| Courses mises à jour | Ancien puis écart |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for code, l in summary["lines"].items():
        c = l["classes"]
        out.append(f"| {code} | {l['group']} | {l['observations']} | {pct(l['measured_share'])} | {c['ecart']} | "
                   f"{c['recent']} | {c['ancien']} | {c['noreport']} | {l['journeys']} | {l['journeys_measured']} | "
                   f"{l['journeys_updated']} | {l['journeys_ancien_then_ecart']} |")
    out += ["", "## Part mesurée par tranche horaire (heure de Paris)", "",
            "| Ligne | " + " | ".join(label for _, label in HOUR_BANDS) + " |",
            "|---|" + "---|" * len(HOUR_BANDS)]
    for code, l in summary["lines"].items():
        cells = [f"{pct(b['measured_share'])} ({b['observations']})" if (b := l["bands"].get(label)) else "—"
                 for _, label in HOUR_BANDS]
        out.append(f"| {code} | " + " | ".join(cells) + " |")
    labels = [f"≤ {u} min" for u in AGE_BUCKETS] + [f"> {AGE_BUCKETS[-1]} min"]
    out += ["", "## Âge de l'enregistrement des passages « ancien »", "",
            "| Ligne | " + " | ".join(labels) + " |", "|---|" + "---|" * len(labels)]
    for code, l in summary["lines"].items():
        if l["ancien_ages"]:
            out.append(f"| {code} | " + " | ".join(str(l["ancien_ages"].get(k, 0)) for k in labels) + " |")
    return "\n".join(out) + "\n"


# -- ligne de commande -------------------------------------------------------------------------------------------
def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1].strip())
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("active")
    a.add_argument("--config", type=Path, required=True)
    p = sub.add_parser("plan")
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--timetable", type=Path, required=True)
    p.add_argument("--base-version", required=True)
    p.add_argument("--out", type=Path, required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--quais", type=Path, required=True)
    f.add_argument("--proxy", required=True)
    f.add_argument("--out-dir", type=Path, required=True)
    u = sub.add_parser("update")
    u.add_argument("--quais", type=Path, required=True)
    u.add_argument("--responses", type=Path, required=True)
    u.add_argument("--state", type=Path, required=True)
    u.add_argument("--summary-json", type=Path)
    u.add_argument("--summary-md", type=Path)
    args = ap.parse_args()

    if args.cmd == "active":
        print("true" if active(read_json(args.config), datetime.now(PARIS).date()) else "false")
    elif args.cmd == "plan":
        with tempfile.TemporaryDirectory() as tmp:
            con = open_db(args.timetable, Path(tmp))
            result = plan(con, read_json(args.config), args.base_version)
            con.close()
        write_json(args.out, result)
        print(f"{len(result['quais'])} quais relevés ; lignes sans quai : {', '.join(result['missing']) or 'aucune'}")
    elif args.cmd == "fetch":
        index = fetch(read_json(args.quais), args.proxy, args.out_dir)
        print(dict(Counter(e["outcome"] for e in index["entries"])))
    else:
        state = update(load_state(args.state), read_json(args.quais), read_json(args.responses / "index.json"),
                       args.responses, int(time.time()))
        save_state(state, args.state)
        summary = summarize(state)
        if args.summary_json:
            write_json(args.summary_json, summary)
        if args.summary_md:
            args.summary_md.write_text(markdown(summary), encoding="utf-8")
        print(f"{summary['runs']} passage(s) ; {len(state['journeys'])} course(s) suivie(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
