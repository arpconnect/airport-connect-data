"""
Port Python de référence des règles métier de la v1 d'Airport Connect.

But : rendre exécutables les règles relevées dans le code Kotlin archivé, pour que les
fichiers de caractérisation (tests/characterization/*.json) soient vérifiables, et pour
que la v2 dispose d'un oracle indépendant de l'ancien code Android.

Chaque fonction cite son origine dans l'archive (chemin relatif à
app/src/main/java/com/airportconnect/app/). Les écarts volontaires sont signalés
« ÉCART ». Aucune dépendance externe : Python 3.9+ (zoneinfo).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")

# Bloc Unicode « Combining Diacritical Marks » (U+0300–U+036F), équivalent exact de
# \p{InCombiningDiacriticalMarks} utilisé côté Kotlin.
_COMBINING = re.compile(r"[̀-ͯ]+")


def _strip_accents(value: str) -> str:
    return _COMBINING.sub("", unicodedata.normalize("NFD", value))


def _trunc_div(a: int, b: int) -> int:
    """Division entière Kotlin/Java (troncature vers zéro), différente de // en Python."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


# ---------------------------------------------------------------------------
# 1. Affichage du temps d'attente
#    Source : ui/components/StationsTimelineRows.kt → etaUiForStationDeparture
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EtaUi:
    kind: str  # "minutes" | "approaching" | "at_stop" | "hidden"
    minutes: Optional[int] = None
    blink: bool = False

    def as_dict(self) -> dict:
        out = {"kind": self.kind, "blink": self.blink}
        if self.kind == "minutes":
            out["minutes"] = self.minutes
        return out


def eta_ui(seconds_until: Optional[int], realtime: bool, eta_min_fallback: Optional[int] = None) -> EtaUi:
    """seconds_until = (epoch départ - maintenant) en secondes, tronqué vers zéro.

    seconds_until None reproduit le cas « epochMillis absent » : affichage brut de etaMin.
    """
    if seconds_until is None:
        return EtaUi("minutes", eta_min_fallback)
    sec = seconds_until
    if not realtime:
        if sec >= 0:
            return EtaUi("minutes", max(1, _trunc_div(sec + 59, 60)))
        if sec >= -59:
            return EtaUi("minutes", 0)
        return EtaUi("hidden")
    if sec > 25:
        return EtaUi("minutes", _trunc_div(sec + 59, 60))
    if sec > 0:
        return EtaUi("approaching", blink=sec <= 13)
    if sec >= -24:
        return EtaUi("at_stop")
    return EtaUi("hidden")


def seconds_between_millis(departure_epoch_ms: int, now_ms: int) -> int:
    """((epoch - now) / 1000L).toInt() en Kotlin."""
    return _trunc_div(departure_epoch_ms - now_ms, 1000)


# ---------------------------------------------------------------------------
# 2. Fenêtre de service et messages de frontière (horaires théoriques GTFS)
#    Source : data/gtfs/IdfmGtfsTheoreticalRepository.kt → getStationSnapshot
#    Constantes : SERVICE_START_MESSAGE_WINDOW_MINUTES = 60, SERVICE_START_REVEAL_MINUTES = 15
# ---------------------------------------------------------------------------
SERVICE_START_MESSAGE_WINDOW_MINUTES = 60
SERVICE_START_REVEAL_MINUTES = 15
SNAPSHOT_LIMIT = 4


def gtfs_time_to_epoch_ms(service_date: date, gtfs_time: str) -> int:
    """Reproduit la v1 : minuit local (atStartOfDay Europe/Paris) + secondes GTFS.

    ÉCART connu : la norme GTFS mesure depuis « midi moins 12 h ». Les deux coïncident sauf
    les jours de changement d'heure ; les cas de caractérisation évitent ces jours-là.
    """
    h, m, s = (int(x) for x in gtfs_time.split(":"))
    midnight = datetime(service_date.year, service_date.month, service_date.day, tzinfo=PARIS)
    return int(midnight.timestamp() * 1000) + (h * 3600 + m * 60 + s) * 1000


@dataclass(frozen=True)
class SnapshotResult:
    visible_epochs_ms: list
    boundary: Optional[str]  # None | "service_not_started" | "service_ended"


def station_snapshot(events: Iterable[tuple], now_ms: int, limit: int = SNAPSHOT_LIMIT) -> SnapshotResult:
    """events : itérable de (service_date: date, epoch_ms: int), déjà filtrés sur les services actifs."""
    evs = sorted(set(events), key=lambda e: e[1])
    if not evs:
        return SnapshotResult([], None)
    by_date: dict = {}
    for sd, ep in evs:
        by_date.setdefault(sd, []).append(ep)
    blocks = sorted(((sd, sorted(eps)) for sd, eps in by_date.items()), key=lambda b: b[1][0])

    status_window = SERVICE_START_MESSAGE_WINDOW_MINUTES * 60_000
    reveal_window = SERVICE_START_REVEAL_MINUTES * 60_000
    active = next((b for b in blocks if b[1][0] - reveal_window <= now_ms <= b[1][-1]), None)
    upcoming = next((b for b in blocks if now_ms < b[1][0]), None)
    past = next((b for b in reversed(blocks) if now_ms > b[1][-1]), None)

    if active is not None:
        selected = active
    elif upcoming is not None and upcoming[1][0] - now_ms <= reveal_window:
        selected = upcoming
    else:
        selected = None

    visible = [ep for ep in selected[1] if ep >= now_ms][:limit] if selected else []

    if visible:
        boundary = None
    elif upcoming is not None and upcoming[1][0] > now_ms:
        boundary = "service_not_started" if upcoming[1][0] - now_ms <= status_window else "service_ended"
    elif past is not None:
        boundary = "service_ended"
    else:
        boundary = None
    return SnapshotResult(visible, boundary)


# ---------------------------------------------------------------------------
# 3. Périmètre directionnel RER B / RER C
#    Source : data/gtfs/AirportConnectDirectionScope.kt
# ---------------------------------------------------------------------------
def normalize_scope_text(raw: str) -> str:
    s = _strip_accents(raw.lower())
    s = s.replace("œ", "oe").replace("æ", "ae")
    s = s.replace("’", " ").replace("'", " ")
    s = s.replace("–", "-").replace("—", "-").replace("-", " ").replace("/", " ")
    s = s.replace("aeroport charles de gaulle", "aeroport charles gaulle")
    s = s.replace("cdg", "cdg charles gaulle")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


DIRECTION_SCOPES = {
    "RER_B": {
        "included": [["aeroport", "charles", "gaulle"], ["cdg"], ["saint", "remy"], ["massy"]],
        "excluded": [["mitry"], ["robinson"]],
    },
    "RER_C": {
        "included": [["massy"], ["pont", "rungis"], ["paris", "austerlitz"], ["bibliotheque", "mitterrand"]],
        "excluded": [["dourdan"], ["etampes"], ["pontoise"], ["saint", "quentin"], ["versailles"], ["montigny"]],
    },
}


def _matches_group(text: str, group: list) -> bool:
    return bool(group) and all(tok in text for tok in group)


def direction_supported(line_id: str, direction: str) -> bool:
    scope = DIRECTION_SCOPES.get(line_id.strip().upper())
    if scope is None:
        return True
    n = normalize_scope_text(direction)
    if not n:
        return False
    if any(_matches_group(n, g) for g in scope["excluded"]):
        return False
    return any(_matches_group(n, g) for g in scope["included"])


def direction_compatible_with_selection(line_id: str, direction: str, keywords: list, match_score: int) -> bool:
    if not direction_supported(line_id, direction):
        return False
    if not keywords:
        return True
    if match_score >= 25:
        return True
    canonical = line_id.strip().upper()
    if canonical == "RER_B":
        kw = " ".join(normalize_scope_text(k) for k in keywords)
        return "saint remy" in kw and "massy" in normalize_scope_text(direction)
    return False


# ---------------------------------------------------------------------------
# 4. Filtre des incidents trafic par axe desservi (RER B / RER C)
#    Source : ui/traffic/TrafficBranchFiltering.kt (hors dé-doublonnage)
# ---------------------------------------------------------------------------
_TRAFFIC_HINTS = {
    "RER_B": {"allow": ["chevreuse", "roissy"], "disallow": ["villeparisis"]},
    "RER_C": {
        "allow": ["austerlitz", "bibliotheque", "palaiseau", "rungis", "ivry", "vitry", "choisy", "juvisy", "les ardoines"],
        "disallow": ["invalides"],
    },
}


def _keep_for_line(line_id: str, text: str) -> bool:
    scope = DIRECTION_SCOPES[line_id]
    hints = _TRAFFIC_HINTS[line_id]
    has_allow = any(_matches_group(text, g) for g in scope["included"]) or any(h in text for h in hints["allow"])
    has_disallow = any(_matches_group(text, g) for g in scope["excluded"]) or any(h in text for h in hints["disallow"])
    return has_allow or not has_disallow


def keep_traffic_incident(line_names: list, stop_names: list, summary: str, details: str = "") -> bool:
    names = " ".join(line_names).lower()
    is_b, is_c = "rer b" in names, "rer c" in names
    if not is_b and not is_c:
        return True
    raw = (" | ".join(stop_names) + " | " + summary + " | " + (details or "")).strip()
    if not raw:
        return True
    text = normalize_scope_text(raw)
    if is_b and is_c:
        return _keep_for_line("RER_B", text) or _keep_for_line("RER_C", text)
    return _keep_for_line("RER_B" if is_b else "RER_C", text)


# ---------------------------------------------------------------------------
# 5. Classement temps réel d'un passage Navitia
#    Source : data/remote/NavitiaDepartureFilteringSupport.kt → inferRealtime (état final v1)
# ---------------------------------------------------------------------------
def infer_realtime_v1(requested_data_freshness: str, freshness: str) -> bool:
    f = (freshness or "").lower()
    explicit_rt = "realtime" in f or "real_time" in f
    explicit_theo = (
        requested_data_freshness.lower() == "base_schedule"
        or "base_schedule" in f
        or "schedule" in f
        or "theorique" in f
        or "theoretical" in f
    )
    adapted = "adapted" in f
    if explicit_theo or adapted:
        return False
    if explicit_rt:
        return True
    return requested_data_freshness.lower() == "realtime"


def infer_realtime_strict(freshness: str) -> bool:
    """Règle proposée pour la v2 : temps réel uniquement si Navitia le dit explicitement."""
    return (freshness or "").strip().lower() == "realtime"


# ---------------------------------------------------------------------------
# 6. Matching v1 par libellé de ligne (à NE PAS reproduire, sert à documenter les faux positifs)
#    Source : data/gtfs/IdfmGtfsLineRegistry.kt → matchesRoute / routeLabelMatchesCandidate
# ---------------------------------------------------------------------------
def normalize_line_label(raw: str) -> str:
    s = _strip_accents(raw.strip().upper())
    s = s.replace("Œ", "OE").replace("Æ", "AE")
    for ch in "-_'/":
        s = s.replace(ch, " ")
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _route_label_matches_candidate(route_label: str, candidate: str) -> bool:
    if not route_label or not candidate:
        return False
    if route_label == candidate:
        return True
    rc, cc = route_label.replace(" ", ""), candidate.replace(" ", "")
    if rc == cc:
        return True
    rt = [t for t in route_label.split(" ") if t]
    ct = [t for t in candidate.split(" ") if t]
    if ct and all(t in rt for t in ct):
        return True
    short = len(cc) <= 5 and any(ch.isdigit() for ch in cc)
    return False if short else (candidate in route_label or cc in rc)


def v1_name_match(candidates: list, route_short_name: str, route_long_name: str) -> bool:
    ns, nl = normalize_line_label(route_short_name), normalize_line_label(route_long_name)
    for cand in (normalize_line_label(c) for c in candidates):
        if not cand:
            continue
        if len(cand) == 1:
            if ns == cand:
                return True
        elif _route_label_matches_candidate(ns, cand) or _route_label_matches_candidate(nl, cand):
            return True
    return False
