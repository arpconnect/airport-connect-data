"""
Règles temps réel de la v2 (spec § 16.2), en Python exécutable de référence.

- merge_board : fusion GTFS + réponse temps réel (R-150, R-152, R-154, R-55)
- CircuitBreaker : disjoncteur par ligne (R-155)

Les seuils marqués « proposé » sont des valeurs de départ, à calibrer avec la journalisation (R-157).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

STALE_AFTER_S = 120          # R-154, proposé
BREAKER_FAILURES = 3         # R-155, proposé
BREAKER_OPEN_S = 300         # R-155, proposé
RT_HIDE_AFTER_S = 24         # R-55 : temps réel retiré à s < −24
TH_HIDE_AFTER_S = 59         # R-55 : théorique retiré à s < −59


def navitia_line_id(route_id: str) -> str:
    return "line:" + route_id


def navitia_stop_point_id(stop_id: str) -> str:
    return "stop_point:" + stop_id


def is_realtime_strict(data_freshness: Optional[str]) -> bool:
    """R-51, règle proposée (D-4)."""
    return (data_freshness or "").strip().lower() == "realtime"


@dataclass
class MergeResult:
    passages: list                    # [{"epoch", "state": "realtime"|"theoretical", "headsign", "base_epoch"}]
    realtime_used: bool
    rejected: int = 0
    reason: Optional[str] = None      # None | "stale" | "no_response"


def merge_board(theoretical: list, response: Optional[dict], route_id: str, stop_id: str,
                now: float, limit: int = 4) -> MergeResult:
    """theoretical : [{"epoch", "headsign"}] issus de la base (déjà filtrés ligne + quai).
    response : {"received_at", "departures": [{"line_id", "stop_point_id", "base_epoch", "epoch",
                "data_freshness", "headsign"}]} ou None."""
    rejected = 0
    reason = None
    matched_rt = {}
    extras = []
    if response is None:
        reason = "no_response"
    elif now - response["received_at"] > STALE_AFTER_S:
        reason = "stale"
    else:
        want_line, want_stop = navitia_line_id(route_id), navitia_stop_point_id(stop_id)
        theo_epochs = {round(t["epoch"]) for t in theoretical}
        for d in response["departures"]:
            if d.get("line_id") != want_line or d.get("stop_point_id") != want_stop:
                rejected += 1
                continue
            if not is_realtime_strict(d.get("data_freshness")):
                continue  # un horaire non temps réel n'apporte rien : la base GTFS fait foi
            base = round(d["base_epoch"]) if d.get("base_epoch") is not None else None
            if base is not None and base in theo_epochs and base not in matched_rt:
                matched_rt[base] = d
            else:
                extras.append(d)
    out = []
    for t in theoretical:
        rt = matched_rt.get(round(t["epoch"]))
        if rt:
            out.append({"epoch": rt["epoch"], "state": "realtime", "headsign": t["headsign"], "base_epoch": t["epoch"]})
        else:
            out.append({"epoch": t["epoch"], "state": "theoretical", "headsign": t["headsign"], "base_epoch": t["epoch"]})
    for d in extras:
        out.append({"epoch": d["epoch"], "state": "realtime", "headsign": d.get("headsign", ""), "base_epoch": d.get("base_epoch")})

    def visible(p):
        s = p["epoch"] - now
        return s >= -RT_HIDE_AFTER_S if p["state"] == "realtime" else s >= -TH_HIDE_AFTER_S
    out = sorted((p for p in out if visible(p)), key=lambda p: (p["epoch"], p["headsign"]))[:limit]
    return MergeResult(passages=out, realtime_used=any(p["state"] == "realtime" for p in out),
                       rejected=rejected, reason=reason)


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
