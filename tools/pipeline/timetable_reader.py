"""
Lecteur de référence de la base d'horaires (schéma 1).

Il fixe, en Python exécutable, les requêtes que l'application Kotlin devra reproduire :
prochains passages à un ou plusieurs quais pour une ligne, avec la règle de frontière de
service R-56, la règle d'absence de données R-153, l'expiration R-173 et les aménagements
de desserte R-90 à R-93.

Toutes les heures sont calculées selon la norme GTFS : midi local moins 12 h + secondes (R-58).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

REVEAL_MIN = 15          # R-56
NOT_STARTED_MIN = 60     # R-56
LIMIT = 4                # R-56


@dataclass(frozen=True)
class Passage:
    epoch: float                 # secondes Unix
    service_date: date
    departure_s: int             # secondes GTFS dans le jour de service (clé de rattachement temps réel, R-152)
    headsign: str
    stop_point: str              # stop_id GTFS
    pattern_id: int
    seq: int


@dataclass(frozen=True)
class Replacement:
    stop_point: str              # quai non desservi (stop_id GTFS)
    direction_label: str
    stop_area: Optional[str]     # zone de report (parent_station GTFS)
    label: Optional[str]         # nom affiché du report (celui du plan de ligne)
    distance_m: Optional[int]    # à vol d'oiseau, arrondie à 10 m


@dataclass(frozen=True)
class Adjustment:
    id: str
    kind: str                    # "temporary" | "permanent"
    title_fr: str
    summary_fr: str
    valid_from: Optional[date]
    valid_until: Optional[date]
    source_url: str
    checked_on: date
    gtfs_lagging_trips: int
    not_served: tuple            # tuple[Replacement]
    incomplete_stop_points: frozenset    # quais aux horaires théoriques possiblement incomplets (R-92)


@dataclass
class StopBoard:
    status: str                  # "ok" | "base_expired" | "no_service_soon" | "no_data" | "not_served"
    passages: list = field(default_factory=list)
    boundary: Optional[str] = None           # None | "service_not_started" | "service_ended"
    next_service_start: Optional[float] = None   # pour « Reprise à HH:MM » (D-6)
    incomplete: bool = False                 # R-92 : horaires théoriques incomplets (itinéraire modifié)
    adjustment_id: Optional[str] = None      # aménagement à l'origine de "not_served" ou de incomplete
    replacements: list = field(default_factory=list)   # list[Replacement] si status == "not_served"


class Timetable:
    def __init__(self, path: str) -> None:
        self.con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        meta = dict(self.con.execute("SELECT key, value FROM meta"))
        self.tz = ZoneInfo(meta["timezone"])
        self.base = date.fromisoformat(meta["base_date"])
        self.valid_start = date.fromisoformat(meta["validity_start"])
        self.valid_end = date.fromisoformat(meta["validity_end"])
        self.meta = meta

    # -- conversions -------------------------------------------------------------------
    def service_day_origin(self, d: date) -> float:
        """Midi local moins 12 h (norme GTFS), en secondes Unix."""
        noon = datetime(d.year, d.month, d.day, 12, tzinfo=self.tz)
        return noon.timestamp() - 12 * 3600

    def epoch(self, d: date, seconds: int) -> float:
        return self.service_day_origin(d) + seconds

    # -- identifiants ------------------------------------------------------------------
    def line_internal_id(self, code: str) -> int:
        row = self.con.execute("SELECT id FROM line WHERE code = ?", (code,)).fetchone()
        if not row:
            raise KeyError(code)
        return row[0]

    def stop_points_of_place(self, place_id: str) -> list:
        return [r[0] for r in self.con.execute(
            """SELECT sp.gtfs_id FROM place_stop_point ps JOIN stop_point sp ON sp.id = ps.stop_point_id
               WHERE ps.place_id = ? OR ps.place_id IN (SELECT id FROM place WHERE parent_id = ?)
               ORDER BY sp.gtfs_id""", (place_id, place_id))]

    def place_label(self, place_id: str) -> dict:
        """Libellé d'un lieu (R-35) : nom affiché, et ancien nom en mention secondaire s'il est porté par la base.

        Rend label_fr, label_en, short_label, secondary_fr et secondary_en (None sans ancien nom)."""
        row = self.con.execute(
            "SELECT label_fr, label_en, short_label, former_fr, former_en FROM place WHERE id = ?",
            (place_id,)).fetchone()
        if row is None:
            raise KeyError(f"lieu inconnu dans la base : {place_id}")
        label_fr, label_en, short, former_fr, former_en = row
        return {"label_fr": label_fr, "label_en": label_en, "short_label": short,
                "secondary_fr": former_fr, "secondary_en": former_en}

    def stop_points_named(self, name: str, line_code: str) -> list:
        return [r[0] for r in self.con.execute(
            """SELECT DISTINCT sp.gtfs_id FROM stop_point sp JOIN pattern_stop ps ON ps.stop_point_id = sp.id
               JOIN pattern p ON p.id = ps.pattern_id JOIN line l ON l.id = p.line_id
               WHERE sp.name = ? AND l.code = ? ORDER BY sp.gtfs_id""", (name, line_code))]

    # -- aménagements (R-90 à R-93) ----------------------------------------------------
    def adjustments(self, line_code: str, d: date) -> list:
        """Aménagements publiés de la ligne, en vigueur le jour d (bornes incluses)."""
        out = []
        rows = self.con.execute(
            """SELECT a.id, a.kind, a.title_fr, a.summary_fr, a.valid_from, a.valid_until, a.source_url,
                      a.checked_on, a.gtfs_lagging_trips
               FROM adjustment a JOIN line l ON l.id = a.line_id WHERE l.code = ? ORDER BY a.id""",
            (line_code,)).fetchall()
        for aid, kind, title, summary, vf, vu, url, checked, lagging in rows:
            vf_d = date.fromisoformat(vf) if vf else None
            vu_d = date.fromisoformat(vu) if vu else None
            if (vf_d and d < vf_d) or (vu_d and d > vu_d):
                continue
            stops = tuple(Replacement(*r) for r in self.con.execute(
                """SELECT sp.gtfs_id, s.direction_label, ar.gtfs_id, s.replacement_label, s.distance_m
                   FROM adjustment_stop s JOIN stop_point sp ON sp.id = s.stop_point_id
                   LEFT JOIN stop_area ar ON ar.id = s.replacement_area_id
                   WHERE s.adjustment_id = ? ORDER BY sp.gtfs_id""", (aid,)))
            gaps = frozenset(r[0] for r in self.con.execute(
                """SELECT DISTINCT sp.gtfs_id FROM adjustment_gap g JOIN stop_point sp ON sp.id = g.stop_point_id
                   WHERE g.adjustment_id = ?""", (aid,)))
            out.append(Adjustment(aid, kind, title, summary, vf_d, vu_d, url, date.fromisoformat(checked),
                                  lagging, stops, gaps))
        return out

    def _bypass_runs(self, adjustment_id: str, stop_points: list, d: date, start: float, end: float) -> bool:
        """Une mission en retard qui contourne l'un de ces quais circule-t-elle le jour de service d,
        sur une plage horaire qui chevauche [start, end] ? (R-92)"""
        idx = (d - self.base).days
        if not 0 <= idx <= 62 or not stop_points:
            return False
        marks = ",".join("?" * len(stop_points))
        origin = self.service_day_origin(d)
        row = self.con.execute(
            f"""SELECT 1 FROM adjustment_gap g
                JOIN stop_point sp ON sp.id = g.stop_point_id
                JOIN trip t        ON t.pattern_id = g.pattern_id
                JOIN service sv    ON sv.id = t.service_id
                JOIN stop_time a   ON a.trip_id = t.id AND a.seq = 0
                JOIN stop_time z   ON z.trip_id = t.id
                                  AND z.seq = (SELECT MAX(seq) FROM stop_time WHERE trip_id = t.id)
                WHERE g.adjustment_id = ? AND sp.gtfs_id IN ({marks}) AND ((sv.days >> ?) & 1) = 1
                  AND ? + a.departure - a.dwell <= ? AND ? + z.departure >= ?
                LIMIT 1""",
            (adjustment_id, *stop_points, idx, origin, end, origin, start)).fetchone()
        return row is not None

    # -- requêtes ----------------------------------------------------------------------
    def passages_on_service_day(self, line_code: str, stop_points: Iterable[str], d: date,
                                pickup_only: bool = True) -> list:
        idx = (d - self.base).days
        if idx < 0 or idx > 62:
            return []
        sps = list(stop_points)
        if not sps:
            return []
        marks = ",".join("?" * len(sps))
        rows = self.con.execute(
            f"""SELECT st.departure, p.headsign, sp.gtfs_id, p.id, ps.seq
                FROM stop_point sp
                JOIN pattern_stop ps ON ps.stop_point_id = sp.id
                JOIN pattern p       ON p.id = ps.pattern_id
                JOIN line l          ON l.id = p.line_id
                JOIN trip t          ON t.pattern_id = ps.pattern_id
                JOIN service s       ON s.id = t.service_id
                JOIN stop_time st    ON st.trip_id = t.id AND st.seq = ps.seq
                WHERE sp.gtfs_id IN ({marks}) AND l.code = ?
                  AND ((s.days >> ?) & 1) = 1
                  {"AND ps.pickup != 1" if pickup_only else ""}""",
            (*sps, line_code, idx)).fetchall()
        return [Passage(self.epoch(d, dep), d, dep, h, sp, pid, seq) for dep, h, sp, pid, seq in rows]

    def board(self, line_code: str, stop_points: Iterable[str], now: datetime, limit: int = LIMIT,
              headsign_filter=None) -> StopBoard:
        """Prochains passages à afficher pour un quai (ou un groupe de quais) d'une ligne."""
        today = now.astimezone(self.tz).date()
        if today < self.valid_start or today > self.valid_end:
            return StopBoard(status="base_expired")                               # R-173
        now_s = now.timestamp()
        sps = list(stop_points)
        days = [d for d in (today - timedelta(days=1), today, today + timedelta(days=1))
                if self.valid_start <= d <= self.valid_end]
        # R-91 : un quai non desservi n'a jamais d'horaire, même si le GTFS en publie encore
        # Les dates d'un aménagement sont des dates calendaires locales : on les évalue au jour de la
        # consultation, pas au jour de service (un bus de 00:30 relève du même aménagement que celui de 23:30).
        adjs = self.adjustments(line_code, today)
        dropped, first_adj = [], None
        for adj in adjs:
            hit = [r for r in adj.not_served if r.stop_point in sps]
            if hit:
                dropped += hit
                first_adj = first_adj or adj.id
        if dropped:
            sps = [sp for sp in sps if sp not in {r.stop_point for r in dropped}]
            if not sps:
                return StopBoard(status="not_served", adjustment_id=first_adj, replacements=dropped)
        board = self._timetable_board(line_code, sps, days, now_s, limit, headsign_filter)
        if board.status in ("ok", "no_service_soon"):
            # R-92 : des missions du GTFS contournent encore ces quais. Si l'une circule sur la plage
            # couverte par l'affichage, la liste peut manquer des bus : on le signale, et on ne conclut
            # jamais à un début ou une fin de service.
            horizon = board.passages[-1].epoch if len(board.passages) >= limit else float("inf")
            for adj in adjs:
                gap = sorted(adj.incomplete_stop_points.intersection(sps))
                if gap and any(self._bypass_runs(adj.id, gap, d, now_s, horizon) for d in days):
                    board.incomplete, board.adjustment_id = True, adj.id
                    board.boundary = board.next_service_start = None
                    if not board.passages:
                        board.status = "no_data"
                    break
        return board

    def _timetable_board(self, line_code: str, sps: list, days: list, now_s: float, limit: int,
                         headsign_filter) -> StopBoard:
        events = []
        for d in days:
            events += self.passages_on_service_day(line_code, sps, d)
        if headsign_filter:
            events = [e for e in events if headsign_filter(e.headsign)]
        # dé-doublonnage d'affichage (R-62) : même quai, même heure, même girouette
        uniq = {}
        for e in events:
            uniq.setdefault((e.stop_point, e.epoch, e.headsign), e)
        events = sorted(uniq.values(), key=lambda e: (e.epoch, e.stop_point, e.headsign))
        if not events:
            # Base valide : soit la ligne ne dessert pas ces quais (donnée absente, R-153),
            # soit elle les dessert mais aucun passage n'est prévu sur la fenêtre J-1 → J+1.
            marks = ",".join("?" * len(sps)) or "''"
            served = self.con.execute(
                f"""SELECT 1 FROM stop_point sp JOIN pattern_stop ps ON ps.stop_point_id = sp.id
                    JOIN pattern p ON p.id = ps.pattern_id JOIN line l ON l.id = p.line_id
                    WHERE sp.gtfs_id IN ({marks}) AND l.code = ? LIMIT 1""", (*sps, line_code)).fetchone()
            return StopBoard(status="no_service_soon" if served else "no_data")
        blocks = {}
        for e in events:
            blocks.setdefault(e.service_date, []).append(e)
        ordered = sorted(blocks.values(), key=lambda b: b[0].epoch)
        reveal, window = REVEAL_MIN * 60, NOT_STARTED_MIN * 60
        active = next((b for b in ordered if b[0].epoch - reveal <= now_s <= b[-1].epoch), None)
        upcoming = next((b for b in ordered if now_s < b[0].epoch), None)
        past = next((b for b in reversed(ordered) if now_s > b[-1].epoch), None)
        selected = active or (upcoming if upcoming and upcoming[0].epoch - now_s <= reveal else None)
        visible = [e for e in selected if e.epoch >= now_s][:limit] if selected else []
        board = StopBoard(status="ok", passages=visible)
        if not visible:
            if upcoming is not None:
                board.boundary = "service_not_started" if upcoming[0].epoch - now_s <= window else "service_ended"
                board.next_service_start = upcoming[0].epoch
            elif past is not None:
                board.boundary = "service_ended"
        return board
