"""
Règles temps réel de la v2 (spec § 16.2), en Python exécutable de référence.

Le temps réel vient du service « Prochains passages » d'IDFM (SIRI Lite stop-monitoring), lu à travers le proxy
PRIM (R-151, décision technique ADR-10) :

- monitoring_ref, parse_stop_monitoring : requête par quai et lecture d'une réponse (R-150, R-151, R-154, R-51) ;
- merge : fusion des horaires théoriques de la base et des passages lus (R-152, R-158, R-55) ;
- CircuitBreaker : disjoncteur par quai interrogé (R-155).

L'application Kotlin reproduit ces règles à l'identique (data/realtime/), vérifiée par les cas de
tests/rules-v2/realtime_merge.json et par les réponses réelles de PRIM du 9 octobre 2026
(tools/app/realtime_goldens.py). Les seuils marqués « proposé » sont à calibrer avec la journalisation (R-157).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

STALE_AFTER_S = 120          # R-154, proposé : réponse ignorée au-delà de 120 s après sa réception
BREAKER_FAILURES = 3         # R-155, proposé
BREAKER_OPEN_S = 300         # R-155, proposé
RT_HIDE_AFTER_S = 24         # R-55 : temps réel retiré à s < −24
TH_HIDE_AFTER_S = 59         # R-55 : théorique (et supprimé) retiré à s < −59
PAIR_TIGHT_S = 90            # R-152, mesuré le 9 octobre 2026 : 374 heures prévues sur 418 égales au GTFS, 42 à 29 s au plus
PAIR_WIDE_S = 300            # R-152 : second passage, horaires adaptés (−170 s vu sur le RER B, −120 s sur le T11)
RECORDED_FRESH_S = 600       # R-51, proposé : une heure égale à l'heure prévue n'est « mesurée » que si elle date de 10 min au plus
GHOST_S = 6 * 3600           # R-154 : un départ à plus de 6 h de maintenant est rejeté
LIMIT = 4                    # R-56

_Q = re.compile(r"^STIF:StopPoint:(?:Q|BP):(\d+):$")
_SP = re.compile(r"^STIF:StopArea:SP:(\d+):$")
_LINE = re.compile(r"^STIF:Line::(C\d{5}):$")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})$")


# -- identifiants (R-150) -------------------------------------------------------------------------
def monitoring_ref(stop_id: str) -> Optional[str]:
    """Référence SIRI d'un quai de la base : « IDFM:34198 » → « STIF:StopPoint:Q:34198: » ; quai de gare des RER
    « IDFM:monomodalStopPlace:43231 » → « STIF:StopArea:SP:43231: ». Toute autre forme : aucune requête."""
    m = re.fullmatch(r"IDFM:(\d+)", stop_id)
    if m:
        return f"STIF:StopPoint:Q:{m.group(1)}:"
    m = re.fullmatch(r"IDFM:monomodalStopPlace:(\d+)", stop_id)
    if m:
        return f"STIF:StopArea:SP:{m.group(1)}:"
    return None


def stop_id_of_ref(ref: Optional[str]) -> Optional[str]:
    """Inverse de monitoring_ref ; None pour toute autre forme. Certains exploitants répondent pour un quai
    « STIF:StopPoint:BP:n: » au lieu de « Q:n: » (même numéro, tramway T9 le 9 octobre 2026) : c'est le même quai."""
    if not isinstance(ref, str):
        return None
    m = _Q.match(ref)
    if m:
        return f"IDFM:{m.group(1)}"
    m = _SP.match(ref)
    if m:
        return f"IDFM:monomodalStopPlace:{m.group(1)}"
    return None


def route_id_of_line_ref(ref: Optional[str]) -> Optional[str]:
    """« STIF:Line::C01743: » → « IDFM:C01743 » (code C, R-10) ; None pour toute autre forme."""
    if not isinstance(ref, str):
        return None
    m = _LINE.match(ref)
    return f"IDFM:{m.group(1)}" if m else None


def parse_time(value) -> Optional[int]:
    """Instant ISO 8601 de SIRI (« 2026-10-09T17:10:11.700Z ») en secondes Unix, fraction tronquée."""
    if not isinstance(value, str) or not _ISO.match(value):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return int(dt.timestamp() // 1)


def _value(obj) -> Optional[str]:
    if isinstance(obj, dict):
        v = obj.get("value")
        return v if isinstance(v, str) else None
    return None


# -- lecture d'une réponse (R-151, R-154, R-51) ------------------------------------------------------
@dataclass(frozen=True)
class Visit:
    line: str                    # route_id (IDFM:Cxxxxx)
    stop_point: str              # stop_id du quai interrogé
    destination: Optional[str]   # stop_id du terminus annoncé, ou None
    aimed: Optional[int]         # heure de départ prévue par l'exploitant
    expected: Optional[int]      # heure de départ annoncée
    status: str                  # DepartureStatus (« onTime », « delayed », « cancelled », « noReport »…)
    recorded: Optional[int]
    cancelled: bool
    measured: bool               # R-51 : heure réellement mesurée (temps réel)

    @property
    def time(self) -> int:
        return self.expected if self.expected is not None else self.aimed

    @property
    def rank(self) -> int:
        return 3 if self.cancelled else 2 if self.measured else 1


@dataclass
class Reading:
    status: str                  # "ok" | "error"
    response_ts: Optional[int] = None
    visits: list = field(default_factory=list)
    rejected: int = 0


def parse_stop_monitoring(text: Optional[str], requested_stop: str, now: int) -> Reading:
    """Lit une réponse stop-monitoring (corps JSON d'un HTTP 200) pour le quai demandé.

    Un passage sans heure de départ (arrivée seule : terminus du véhicule, R-32) est ignoré. Est rejeté
    (R-154) : un passage d'un autre quai que celui demandé, d'une ligne illisible, aux heures illisibles, ou
    partant à plus de 6 h de maintenant. Deux passages de même ligne, même terminus et même heure (deux
    systèmes de l'exploitant qui publient le même bus) n'en font qu'un : supprimé, puis mesuré, puis le plus
    récemment enregistré."""
    want = monitoring_ref(requested_stop)
    try:
        doc = json.loads(text) if text else None
        delivery = doc["Siri"]["ServiceDelivery"]
        smd = delivery["StopMonitoringDelivery"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        return Reading("error")
    if not isinstance(smd, dict) or smd.get("ErrorCondition") is not None:
        return Reading("error")
    response_ts = parse_time(delivery.get("ResponseTimestamp")) if isinstance(delivery, dict) else None
    reference = response_ts if response_ts is not None else now
    visits, rejected = [], 0
    raw = smd.get("MonitoredStopVisit") or []
    if not isinstance(raw, list):
        return Reading("error")
    for item in raw:
        try:
            mvj = item["MonitoredVehicleJourney"]
            call = mvj["MonitoredCall"]
        except (KeyError, TypeError):
            rejected += 1
            continue
        if not isinstance(call, dict):
            rejected += 1
            continue
        aimed_raw, expected_raw = call.get("AimedDepartureTime"), call.get("ExpectedDepartureTime")
        if aimed_raw is None and expected_raw is None:
            continue                                    # arrivée seule (terminus du véhicule)
        if stop_id_of_ref(_value(item.get("MonitoringRef"))) != requested_stop or want is None:
            rejected += 1
            continue
        line = route_id_of_line_ref(_value(mvj.get("LineRef")))
        aimed = parse_time(aimed_raw) if aimed_raw is not None else None
        expected = parse_time(expected_raw) if expected_raw is not None else None
        if line is None or (aimed_raw is not None and aimed is None) or (expected_raw is not None and expected is None):
            rejected += 1
            continue
        t = expected if expected is not None else aimed
        if abs(t - now) > GHOST_S:
            rejected += 1
            continue
        status = call.get("DepartureStatus") if isinstance(call.get("DepartureStatus"), str) else ""
        recorded = parse_time(item.get("RecordedAtTime"))
        cancelled = status == "cancelled"
        measured = (expected is not None and status not in ("noReport", "cancelled")
                    and (aimed is None or expected != aimed
                         or (recorded is not None and reference - recorded <= RECORDED_FRESH_S)))
        visits.append(Visit(line, requested_stop, stop_id_of_ref(_value(mvj.get("DestinationRef"))),
                            aimed, expected, status, recorded, cancelled, measured))
    best = {}
    for v in visits:
        key = (v.line, v.destination, v.aimed if v.aimed is not None else v.expected)
        cur = best.get(key)
        if cur is None or (v.rank, v.recorded or 0, v.time) > (cur.rank, cur.recorded or 0, cur.time):
            best[key] = v
    kept_visits = sorted(best.values(), key=lambda v: (v.time, v.line, v.destination or "", v.aimed or 0))
    return Reading("ok", response_ts, kept_visits, rejected)


# -- fusion (R-152, R-158) ----------------------------------------------------------------------------
@dataclass(frozen=True)
class Item:
    epoch: int                   # heure affichée
    base_epoch: Optional[int]    # heure du GTFS (ou prévue par l'exploitant pour un passage ajouté)
    state: str                   # "realtime" | "theoretical" | "cancelled"
    stop_point: str
    destination: str             # nom GTFS du terminus (R-30)
    pattern_id: Optional[int]
    seq: Optional[int]
    source: str                  # "timetable" | "paired" | "added"


@dataclass
class MergeResult:
    items: list
    realtime: bool               # au moins un passage affiché vient du temps réel (mesuré ou supprimé)
    unplaced: int                # passages lus que l'on ne peut rattacher sans deviner (R-150)


def visible(state: str, epoch: int, now: int) -> bool:
    s = int(epoch - now)
    return s >= -RT_HIDE_AFTER_S if state == "realtime" else s >= -TH_HIDE_AFTER_S


def merge(events: list, eligible: frozenset, visits: Iterable[Visit], route_id: str,
          line_patterns_at: Callable[[str], list], serves: Callable[[int, int, str], bool],
          stop_name: Callable[[str], str], allowed: Optional[frozenset], now: int, limit: int = LIMIT) -> MergeResult:
    """Fusion des passages théoriques d'un tableau et des passages lus pour ses quais.

    events : passages théoriques candidats (objets à attributs epoch, stop_point, pattern_id, seq, destination,
    headsign), tous jours de service consultés confondus ; eligible : clés (stop_point, epoch, headsign) de ceux que
    la base afficherait (bloc de service retenu, R-56) ; line_patterns_at(quai) : couples (mission, rang) de la ligne
    où l'on peut monter à ce quai ; serves(mission, rang, quai) : la mission dessert-elle ensuite la zone d'arrêt de
    ce quai ; allowed : couples (mission, rang) du tableau, ou None pour toutes les missions de la ligne.

    Un passage lu est « placé » si son terminus est connu et que toutes les missions de la ligne qui y mènent depuis
    ce quai appartiennent au tableau (sinon il pourrait appartenir à une autre carte : rien n'est deviné, R-150).
    1. Un passage lu avec une heure prévue est rattaché au passage théorique du même quai dont l'heure du GTFS est à
       90 s au plus, et dont la mission mène à son terminus ; puis, pour ceux qui restent, à 300 s au plus. Au plus
       proche d'abord, chacun une seule fois.
    2. Un passage lu sans heure prévue (heure annoncée seule, réseau RATP), mesuré ou supprimé et placé, fait foi
       pour son terminus : à ce quai, les passages théoriques non rattachés qui mènent à ce terminus, jusqu'à la
       dernière heure annoncée pour lui, sont retirés.
    3. Un passage lu non rattaché est ajouté s'il est mesuré ou supprimé, et placé ; sinon il n'est pas affiché.
    4. Un passage rattaché à une heure non mesurée garde l'heure du GTFS et reste théorique. Un passage théorique
       n'est affiché que s'il fait partie de ceux que la base afficherait ; un passage mesuré ou supprimé l'est
       toujours. Retrait selon R-55, puis les « limit » premiers passages non supprimés et les suppressions qui les
       précèdent."""
    vs = [v for v in visits if v.line == route_id]
    ev = sorted(events, key=lambda e: (e.epoch, e.stop_point, e.headsign, e.pattern_id, e.seq))

    def placeable(v: Visit) -> bool:
        if v.destination is None:
            return False
        at = line_patterns_at(v.stop_point)
        serving = [(p, s) for p, s in at if serves(p, s, v.destination)]
        scope = allowed if allowed is not None else frozenset(at)
        return bool(serving) and all(ps in scope for ps in serving)

    placed = [placeable(v) for v in vs]
    pairs, used = {}, set()
    for tolerance in (PAIR_TIGHT_S, PAIR_WIDE_S):
        cand = []
        for vi, v in enumerate(vs):
            if vi in used or v.aimed is None:
                continue
            for ei, e in enumerate(ev):
                if ei in pairs or e.stop_point != v.stop_point:
                    continue
                d = abs(int(e.epoch) - v.aimed)
                if d <= tolerance and (v.destination is None or serves(e.pattern_id, e.seq, v.destination)):
                    cand.append((d, vi, ei))
        for d, vi, ei in sorted(cand):
            if vi in used or ei in pairs:
                continue
            pairs[ei] = vs[vi]
            used.add(vi)

    horizon = {}                    # (quai, terminus) -> dernière heure annoncée (étape 2)
    for vi, v in enumerate(vs):
        if v.aimed is None and placed[vi] and (v.measured or v.cancelled):
            key = (v.stop_point, v.destination)
            horizon[key] = max(horizon.get(key, v.expected), v.expected)

    items, unplaced = [], 0
    for ei, e in enumerate(ev):
        v = pairs.get(ei)
        key = (e.stop_point, e.epoch, e.headsign)
        if v is None:
            if any(sp == e.stop_point and e.epoch <= h and serves(e.pattern_id, e.seq, dest)
                   for (sp, dest), h in horizon.items()):
                continue
            if key in eligible:
                items.append(Item(int(e.epoch), int(e.epoch), "theoretical", e.stop_point, e.destination,
                                  e.pattern_id, e.seq, "timetable"))
            continue
        if v.cancelled or v.measured:
            items.append(Item(v.time, int(e.epoch), "cancelled" if v.cancelled else "realtime", e.stop_point,
                              e.destination, e.pattern_id, e.seq, "paired"))
        elif key in eligible:
            items.append(Item(int(e.epoch), int(e.epoch), "theoretical", e.stop_point, e.destination,
                              e.pattern_id, e.seq, "paired"))
    for vi, v in enumerate(vs):
        if vi in used:
            continue
        if not placed[vi] or not (v.measured or v.cancelled):
            unplaced += 1
            continue
        items.append(Item(v.time, v.aimed, "cancelled" if v.cancelled else "realtime", v.stop_point,
                          stop_name(v.destination), None, None, "added"))

    items = [i for i in items if visible(i.state, i.epoch, now)]
    items.sort(key=lambda i: (i.epoch, i.stop_point, i.destination, i.state, i.base_epoch or 0))
    out, count = [], 0
    for i in items:
        if count >= limit:
            break
        out.append(i)
        if i.state != "cancelled":
            count += 1
    return MergeResult(out, any(i.state != "theoretical" for i in out), unplaced)


# -- cas de règle (tests/rules-v2/realtime_merge.json) ------------------------------------------------
def iso(epoch: int) -> str:
    """Instant en secondes Unix → ISO 8601 UTC à la milliseconde, comme le service d'IDFM."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def siri_document(visits: list, now: int, error: bool = False) -> str:
    """Réponse SIRI Lite écrite à partir des passages compacts d'un cas de règle."""
    delivery = {"ResponseTimestamp": iso(now), "Version": "2.0", "Status": "true"}
    if error:
        delivery["ErrorCondition"] = {"ErrorInformation": {"ErrorText": "La requête contient des identifiants qui sont inconnus"}}
    else:
        out = []
        for v in visits:
            call = {"DepartureStatus": v.get("status", "")}
            for key, name in (("aimed", "AimedDepartureTime"), ("expected", "ExpectedDepartureTime"),
                              ("arrival", "ExpectedArrivalTime")):
                if key in v:
                    call[name] = iso(v[key])
            out.append({"RecordedAtTime": iso(v["recorded"]), "MonitoringRef": {"value": v["monitoring"]},
                        "MonitoredVehicleJourney": {"LineRef": {"value": v["line"]},
                                                    "DestinationRef": {"value": v["destination"]},
                                                    "MonitoredCall": call}})
        delivery["MonitoredStopVisit"] = out
    return json.dumps({"Siri": {"ServiceDelivery": {"ResponseTimestamp": iso(now), "StopMonitoringDelivery": [delivery]}}})


@dataclass(frozen=True)
class _Event:
    epoch: int
    stop_point: str
    pattern_id: int
    seq: int
    headsign: str
    destination: str


def run_rule_case(ctx: dict, case: dict) -> dict:
    """Exécute un cas de tests/rules-v2/realtime_merge.json : écrit la réponse, la lit, fusionne."""
    now, stop = ctx["now"], ctx["stop_point"]
    zones = ctx["zones"]
    patterns = {int(k): v for k, v in ctx["patterns"].items()}
    events = [_Event(e["epoch"], stop, e["pattern"], e["seq"], e["headsign"], e["destination"]) for e in ctx["events"]]
    if "events" in case:
        events = [events[i] for i in case["events"]]
    allowed = frozenset(tuple(x) for x in case["allowed"]) if "allowed" in case else None
    reading = parse_stop_monitoring(siri_document(case["visits"], now, case.get("error", False)), stop, now)
    if reading.status != "ok":
        return {"status": reading.status}

    def zone(sp):
        return zones.get(sp, sp)

    def serves(pid, seq, dest):
        return any(i > seq and zone(sp) == zone(dest) for i, sp in enumerate(patterns[pid]))

    def at(sp):
        return sorted((pid, i) for pid, stops in patterns.items() for i, x in enumerate(stops) if x == sp
                      and i < len(stops) - 1)

    eligible = frozenset((e.stop_point, e.epoch, e.headsign) for e in events)
    res = merge(events, eligible, reading.visits, ctx["route_id"], at, serves, lambda sp: ctx["names"].get(sp, sp),
                allowed, now)
    return {"items": [[i.epoch, i.state, i.source, i.destination] for i in res.items],
            "rejected": reading.rejected, "unplaced": res.unplaced}


# -- disjoncteur (R-155) ------------------------------------------------------------------------------
@dataclass
class CircuitBreaker:
    failures: int = 0
    open_until: float = 0.0
    events: list = field(default_factory=list)

    def allow(self, now: float) -> bool:
        return now >= self.open_until

    def record(self, now: float, ok: bool, retry_after_s: Optional[float] = None) -> None:
        if ok:
            self.failures = 0
            return
        self.failures += 1
        if retry_after_s is not None:
            self.open_until = max(self.open_until, now + retry_after_s)
        if self.failures >= BREAKER_FAILURES:
            self.open_until = max(self.open_until, now + BREAKER_OPEN_S)
            self.failures = 0
            self.events.append(("open", now))
