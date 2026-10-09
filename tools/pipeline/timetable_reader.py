"""
Lecteur de référence de la base d'horaires (schéma 3).

Il fixe, en Python exécutable, les requêtes que l'application Kotlin reproduit à l'identique
(app/src/main/java/com/airportconnect/app/data/timetable/TimetableReader.kt, vérifié par les cas de
tools/app/reader_goldens.py) : prochains passages à un ou plusieurs quais pour une ligne, avec la règle
de frontière de service R-56, la conservation des passages théoriques R-55, la règle d'absence de données
R-153, l'expiration R-173 et les aménagements de desserte R-90 à R-93 ; départs d'un lieu pour l'accueil
(R-100, décision D-13).

Toutes les heures sont calculées selon la norme GTFS : midi local moins 12 h + secondes (R-58).
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

REVEAL_MIN = 15          # R-56
NOT_STARTED_MIN = 60     # R-56
LIMIT = 4                # R-56
RETAIN_S = 59            # R-55 : un passage théorique reste affiché (« 0 min ») jusqu'à 59 s après son heure
SECTORS = ("ROISSY", "ORLY", "BOURGET", "BEAUVAIS")
OFFLINE_LIMIT = 3                # R-119 : trajets proposés hors connexion
OFFLINE_HORIZON_S = 12 * 3600    # R-119 : départs pris en compte, à partir de maintenant
OFFLINE_SAME_STOP_S = 120        # R-119 : changement de véhicule au même quai, temps minimal
OFFLINE_MAX_SCANS = 6            # R-119 : recherches successives au plus (départs de plus en plus tardifs)


def _undominated(journeys: list) -> list:
    """R-119 : un trajet est écarté si un autre part au plus tôt en même temps, arrive au plus tard en même temps
    et ne demande pas plus de correspondances (et l'un des trois strictement mieux). L'ordre est conservé."""
    def dominates(k, j):
        return (k["departure"] >= j["departure"] and k["arrival"] <= j["arrival"] and k["transfers"] <= j["transfers"]
                and (k["departure"], k["arrival"], k["transfers"]) != (j["departure"], j["arrival"], j["transfers"]))
    return [j for j in journeys if not any(dominates(k, j) for k in journeys if k is not j)]


def kept(epoch: float, now_s: float) -> bool:
    """R-55 : s, secondes avant le départ tronquées vers zéro ; un passage théorique est retiré si s < -59."""
    return int(epoch - now_s) >= -RETAIN_S


@dataclass(frozen=True)
class Passage:
    epoch: float                 # secondes Unix
    service_date: date
    departure_s: int             # secondes GTFS dans le jour de service (clé de rattachement temps réel, R-152)
    headsign: str                # trip_headsign GTFS : nom de mission pour les RER (« ELFE »), destination pour les bus
    stop_point: str              # stop_id GTFS
    pattern_id: int
    seq: int
    destination: str             # nom GTFS du dernier quai de la mission : la destination affichée (R-30)


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


@dataclass
class DepartureGroup:
    """Une carte de l'accueil : une ligne et un sens (lieu aéroportuaire) ou un aéroport desservi (lieu ville)."""
    line: str                    # code de la ligne (RER_B…)
    key: str                     # "direction:0", "direction:1", "direction:none" ; "sector:ROISSY"… pour un lieu ville
    sector: Optional[str]        # aéroport desservi (lieu ville), None pour un lieu aéroportuaire
    stop_area: Optional[str]     # point de montée (lieu ville, parent_station GTFS), None sinon
    title: str                   # destination du premier passage affiché, sinon la plus fréquente des missions retenues
    board: StopBoard


class Timetable:
    def __init__(self, path: str) -> None:
        self.con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        meta = dict(self.con.execute("SELECT key, value FROM meta"))
        self.tz = ZoneInfo(meta["timezone"])
        self.base = date.fromisoformat(meta["base_date"])
        self.valid_start = date.fromisoformat(meta["validity_start"])
        self.valid_end = date.fromisoformat(meta["validity_end"])
        self.meta = meta
        self._terminus = None
        self._max_departure = None
        self._pattern_stops = {}
        self._stops = None

    def max_departure(self) -> int:
        """Heure GTFS la plus tardive de la base, en secondes (au-delà de 24 h pour les services de nuit)."""
        if self._max_departure is None:
            self._max_departure = self.con.execute("SELECT MAX(departure) FROM stop_time").fetchone()[0] or 0
        return self._max_departure

    def terminus(self, pattern_id: int) -> str:
        """Nom GTFS du dernier quai d'une mission : destination affichée (R-30). Les girouettes GTFS des RER
        sont des noms de mission (« ELFE », « EPAU ») et ne sont jamais affichées."""
        if self._terminus is None:
            self._terminus = dict(self.con.execute(
                """SELECT ps.pattern_id, sp.name FROM pattern_stop ps JOIN stop_point sp ON sp.id = ps.stop_point_id
                   WHERE ps.seq = (SELECT MAX(seq) FROM pattern_stop z WHERE z.pattern_id = ps.pattern_id)"""))
        return self._terminus[pattern_id]

    def trip_count(self, pattern_id: int) -> int:
        return self.con.execute("SELECT COUNT(*) FROM trip WHERE pattern_id = ?", (pattern_id,)).fetchone()[0]

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

    def journey_ref(self, place_id: str) -> str | None:
        """Point Navitia d'un lieu pour le calcul d'itinéraires (R-115) ; None pour un lieu ville (Paris), qui n'a
        pas de point précis.

        Un lieu qui couvre exactement une zone d'arrêt (tous ses quais présents dans la base, et rien d'autre) est
        désigné par cette zone : « stop_area:IDFM:69212 ». Sinon (un terminal formé de quelques quais d'une grande
        zone, ou un lieu à cheval sur deux zones), il l'est par le centre de ses quais, « lon;lat » à 6 décimales :
        Navitia y ajoute la marche jusqu'aux quais voisins. Les quais du lieu comprennent ceux de ses sous-lieux,
        comme pour une destination (R-102), et la moyenne se fait dans l'ordre des stop_id GTFS."""
        rows = self.con.execute(
            """SELECT DISTINCT sp.gtfs_id, a.gtfs_id, sp.lat, sp.lon FROM place_stop_point x
               JOIN stop_point sp ON sp.id = x.stop_point_id JOIN stop_area a ON a.id = sp.stop_area_id
               WHERE x.place_id = ? OR x.place_id IN (SELECT c.id FROM place c WHERE c.parent_id = ?)
               ORDER BY sp.gtfs_id""", (place_id, place_id)).fetchall()
        if not rows:
            return None
        areas = {r[1] for r in rows}
        if len(areas) == 1:
            area = next(iter(areas))
            whole = {r[0] for r in self.con.execute(
                "SELECT sp.gtfs_id FROM stop_point sp JOIN stop_area a ON a.id = sp.stop_area_id WHERE a.gtfs_id = ?",
                (area,))}
            if whole == {r[0] for r in rows}:
                return f"stop_area:{area}"
        lat = sum(r[2] for r in rows) / len(rows)
        lon = sum(r[3] for r in rows) / len(rows)
        return f"{lon:.6f};{lat:.6f}"

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

    def places(self) -> list:
        """Lieux du sélecteur de l'accueil, dans l'ordre de data/places.json (sort_order)."""
        cols = ("id", "parent_id", "kind", "sector", "label_fr", "label_en", "short_label", "former_fr", "former_en")
        return [dict(zip(cols, r)) for r in self.con.execute(
            f"SELECT {', '.join(cols)} FROM place ORDER BY sort_order")]

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
        return [Passage(self.epoch(d, dep), d, dep, h, sp, pid, seq, self.terminus(pid)) for dep, h, sp, pid, seq in rows]

    def board(self, line_code: str, stop_points: Iterable[str], now: datetime, limit: int = LIMIT,
              headsign_filter=None, allowed: Optional[frozenset] = None) -> StopBoard:
        """Prochains passages à afficher pour un quai (ou un groupe de quais) d'une ligne.

        allowed : si fourni, seuls les passages dont le couple (mission, rang) y figure sont retenus
        (départs d'un lieu, R-100) ; la ligne n'est alors « desservie » que par ces couples."""
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
        board = self._timetable_board(line_code, sps, days, now_s, limit, headsign_filter, allowed)
        # R-153 : le jour de service de la veille n'est pas couvert par la base (premier jour de validité) ; tant
        # que ses trajets peuvent encore circuler, la liste peut manquer des passages et aucune frontière de
        # service n'est déduite.
        yesterday = today - timedelta(days=1)
        if yesterday < self.valid_start and now_s < self.epoch(yesterday, self.max_departure()) \
                and board.status in ("ok", "no_service_soon"):
            board.boundary = board.next_service_start = None
            if board.passages:
                board.incomplete = True
            else:
                board.status = "no_data"
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
                         headsign_filter, allowed: Optional[frozenset] = None) -> StopBoard:
        events = []
        for d in days:
            events += self.passages_on_service_day(line_code, sps, d)
        if allowed is not None:
            events = [e for e in events if (e.pattern_id, e.seq) in allowed]
        if headsign_filter:
            events = [e for e in events if headsign_filter(e.headsign)]
        # dé-doublonnage d'affichage (R-62) : même quai, même heure, même girouette ; le passage gardé est le
        # premier dans un ordre total (heure, quai, girouette, mission, rang), indépendant du plan de requête
        uniq = {}
        for e in sorted(events, key=lambda e: (e.epoch, e.stop_point, e.headsign, e.pattern_id, e.seq)):
            uniq.setdefault((e.stop_point, e.epoch, e.headsign), e)
        events = list(uniq.values())
        if not events:
            # Base valide : soit la ligne ne dessert pas ces quais (donnée absente, R-153),
            # soit elle les dessert mais aucun passage n'est prévu sur la fenêtre J-1 → J+1.
            if allowed is not None:
                served = bool(allowed)
            else:
                marks = ",".join("?" * len(sps)) or "''"
                served = self.con.execute(
                    f"""SELECT 1 FROM stop_point sp JOIN pattern_stop ps ON ps.stop_point_id = sp.id
                        JOIN pattern p ON p.id = ps.pattern_id JOIN line l ON l.id = p.line_id
                        WHERE sp.gtfs_id IN ({marks}) AND l.code = ? LIMIT 1""", (*sps, line_code)).fetchone() is not None
            return StopBoard(status="no_service_soon" if served else "no_data")
        blocks = {}
        for e in events:
            blocks.setdefault(e.service_date, []).append(e)
        ordered = sorted(blocks.values(), key=lambda b: b[0].epoch)
        reveal, window = REVEAL_MIN * 60, NOT_STARTED_MIN * 60
        # R-55 : un passage théorique reste visible jusqu'à 59 s après son heure (« 0 min »)
        active = next((b for b in ordered if b[0].epoch - reveal <= now_s and kept(b[-1].epoch, now_s)), None)
        upcoming = next((b for b in ordered if now_s < b[0].epoch), None)
        past = next((b for b in reversed(ordered) if not kept(b[-1].epoch, now_s)), None)
        selected = active or (upcoming if upcoming and upcoming[0].epoch - now_s <= reveal else None)
        visible = [e for e in selected if kept(e.epoch, now_s)][:limit] if selected else []
        board = StopBoard(status="ok", passages=visible)
        if not visible:
            if upcoming is not None:
                board.boundary = "service_not_started" if upcoming[0].epoch - now_s <= window else "service_ended"
                board.next_service_start = upcoming[0].epoch
            elif past is not None:
                board.boundary = "service_ended"
        return board

    # -- accueil : départs d'un lieu (R-100, décision D-13) ------------------------------------
    # -- destination (R-102, décision D-14) -----------------------------------------------------
    def destination_stop_points(self, destination: tuple) -> frozenset:
        """Quais d'une destination : ("place", id), ("airport", secteur) ou ("stop_area", parent_station GTFS).

        Lieu aéroportuaire : ses quais et ceux de ses sous-lieux. Lieu ville (Paris) : les quais de ses
        points de montée, toutes lignes confondues (gares et terminus parisiens). Aéroport : les quais de tous
        ses lieux (une gare comme celle du métro 14 à Orly, rattachée par IDFM aux terminaux 1-2-3, compte pour
        tout l'aéroport). Zone d'arrêt : ses quais. Une destination inconnue de la base lève une erreur."""
        kind, ident = destination
        if kind == "airport":
            if ident not in SECTORS:
                raise KeyError(f"aéroport inconnu : {ident}")
            return frozenset(r[0] for r in self.con.execute(
                """SELECT DISTINCT sp.gtfs_id FROM place_stop_point x JOIN place pl ON pl.id = x.place_id
                   JOIN stop_point sp ON sp.id = x.stop_point_id WHERE pl.sector = ?""", (ident,)))
        if kind == "place":
            row = self.con.execute("SELECT kind FROM place WHERE id = ?", (ident,)).fetchone()
            if row is None:
                raise KeyError(f"lieu inconnu dans la base : {ident}")
            if row[0] == "airport":
                return frozenset(self.stop_points_of_place(ident))
            return frozenset(r[0] for r in self.con.execute(
                """SELECT sp.gtfs_id FROM place_boarding pb JOIN stop_point sp ON sp.stop_area_id = pb.stop_area_id
                   WHERE pb.place_id = ?""", (ident,)))
        if kind == "stop_area":
            if self.con.execute("SELECT 1 FROM stop_area WHERE gtfs_id = ?", (ident,)).fetchone() is None:
                raise KeyError(f"zone d'arrêt inconnue dans la base : {ident}")
            return frozenset(r[0] for r in self.con.execute(
                """SELECT sp.gtfs_id FROM stop_point sp JOIN stop_area a ON a.id = sp.stop_area_id
                   WHERE a.gtfs_id = ?""", (ident,)))
        raise ValueError(f"destination invalide : {destination}")

    def pattern_stops(self, pattern_id: int) -> list:
        """Quais d'une mission dans l'ordre : (rang, stop_id GTFS, descente interdite)."""
        if pattern_id not in self._pattern_stops:
            self._pattern_stops[pattern_id] = [(seq, sp, dropoff == 1) for seq, sp, dropoff in self.con.execute(
                """SELECT ps.seq, sp.gtfs_id, ps.dropoff FROM pattern_stop ps JOIN stop_point sp ON sp.id = ps.stop_point_id
                   WHERE ps.pattern_id = ? ORDER BY ps.seq""", (pattern_id,))]
        return self._pattern_stops[pattern_id]

    def reaches(self, pattern_id: int, seq: int, targets: frozenset) -> bool:
        """La mission, montée au rang seq, dessert-elle ensuite l'un de ces quais, en descente autorisée ?"""
        return any(s > seq and sp in targets and not no_drop for s, sp, no_drop in self.pattern_stops(pattern_id))

    def place_departures(self, place_id: str, now: datetime, destination: Optional[tuple] = None) -> list:
        """Cartes de l'accueil pour un lieu, dans l'ordre d'affichage.

        destination (R-102, D-14) : si fournie, seuls restent les départs dont la mission dessert ensuite,
        en descente autorisée, un quai de la destination (destination_stop_points) ; une carte sans aucun
        de ces départs disparaît, y compris celle d'un point de montée qui ne mène plus à un aéroport. Depuis
        Paris, les cartes ne sont alors plus groupées par aéroport : une carte par point de montée (clé
        « destination », secteur nul), pour les missions qui mènent à la destination.

        - Lieu aéroportuaire : une carte par ligne et par sens (direction_id GTFS), pour les missions qui
          permettent de monter à un quai du lieu ou de ses sous-lieux ; triées par prochain départ.
        - Lieu ville (Paris) : pour chaque point de montée (ligne, zone d'arrêt), une carte par aéroport
          desservi ensuite en descente autorisée ; groupées par aéroport (ROISSY, ORLY, BOURGET, BEAUVAIS),
          puis triées par prochain départ. Un point de montée que la ligne ne dessert plus vers un aéroport
          garde ses cartes (une par secteur de la ligne), en « Horaires indisponibles » (R-153).
        Une carte sans passage visible vient après celles qui en ont, dans l'ordre des lignes du secteur."""
        row = self.con.execute("SELECT kind, sector FROM place WHERE id = ?", (place_id,)).fetchone()
        if row is None:
            raise KeyError(f"lieu inconnu dans la base : {place_id}")
        kind, place_sector = row
        groups = {}      # (code, key) -> {"sector", "stop_area", "allowed": {(mission, rang): quai}}
        targets = self.destination_stop_points(destination) if destination is not None else None

        def add(code, key, sector, area, pid, seq, sp):
            g = groups.setdefault((code, key), {"sector": sector, "stop_area": area, "allowed": {}})
            if pid is not None and (targets is None or self.reaches(pid, seq, targets)):
                g["allowed"][(pid, seq)] = sp

        if kind == "airport":
            for code, direction, pid, seq, sp in self.con.execute(
                    """SELECT DISTINCT l.code, p.direction, ps.pattern_id, ps.seq, sp.gtfs_id
                       FROM place_stop_point x JOIN stop_point sp ON sp.id = x.stop_point_id
                       JOIN pattern_stop ps ON ps.stop_point_id = sp.id AND ps.pickup != 1
                       JOIN pattern p ON p.id = ps.pattern_id JOIN line l ON l.id = p.line_id
                       WHERE x.place_id = ? OR x.place_id IN (SELECT id FROM place WHERE parent_id = ?)""",
                    (place_id, place_id)):
                add(code, f"direction:{'none' if direction is None else direction}", None, None, pid, seq, sp)
        else:
            boarding = self.con.execute(
                """SELECT l.code, a.gtfs_id FROM place_boarding pb JOIN line l ON l.id = pb.line_id
                   JOIN stop_area a ON a.id = pb.stop_area_id WHERE pb.place_id = ?""", (place_id,)).fetchall()
            for code, area in boarding:
                if targets is not None:
                    for pid, seq, sp in self.con.execute(
                            """SELECT DISTINCT b.pattern_id, b.seq, bp.gtfs_id
                               FROM stop_area a JOIN stop_point bp ON bp.stop_area_id = a.id
                               JOIN pattern_stop b ON b.stop_point_id = bp.id AND b.pickup != 1
                               JOIN pattern p ON p.id = b.pattern_id JOIN line l ON l.id = p.line_id
                               WHERE a.gtfs_id = ? AND l.code = ?""", (area, code)):
                        add(code, "destination", None, area, pid, seq, sp)
                    continue
                found = False
                for sector, pid, seq, sp in self.con.execute(
                        """SELECT DISTINCT pl.sector, b.pattern_id, b.seq, bp.gtfs_id
                           FROM stop_area a JOIN stop_point bp ON bp.stop_area_id = a.id
                           JOIN pattern_stop b ON b.stop_point_id = bp.id AND b.pickup != 1
                           JOIN pattern p ON p.id = b.pattern_id JOIN line l ON l.id = p.line_id
                           JOIN pattern_stop d ON d.pattern_id = p.id AND d.seq > b.seq AND d.dropoff != 1
                           JOIN place_stop_point ps ON ps.stop_point_id = d.stop_point_id
                           JOIN place pl ON pl.id = ps.place_id AND pl.kind = 'airport'
                           WHERE a.gtfs_id = ? AND l.code = ?""", (area, code)):
                    found = True
                    add(code, f"sector:{sector}", sector, area, pid, seq, sp)
                if not found:
                    for (sector,) in self.con.execute(
                            """SELECT s.sector FROM line_sector s JOIN line l ON l.id = s.line_id
                               WHERE l.code = ? ORDER BY s.sector""", (code,)):
                        add(code, f"sector:{sector}", sector, area, None, None, None)

        out = []
        for (code, key), g in groups.items():
            if targets is not None and not g["allowed"]:
                continue
            patterns = {pid for pid, _ in g["allowed"]}
            board = self.board(code, sorted(set(g["allowed"].values())), now, allowed=frozenset(g["allowed"]))
            if board.passages:
                title = board.passages[0].destination
            elif patterns:
                # destination la plus fréquente des missions retenues (nombre de trajets), puis ordre alphabétique
                weights = defaultdict(int)
                for pid in patterns:
                    weights[self.terminus(pid)] += self.trip_count(pid)
                title = sorted(weights.items(), key=lambda x: (-x[1], x[0]))[0][0]
            else:
                title = ""
            out.append(DepartureGroup(code, key, g["sector"], g["stop_area"], title, board))

        sort_sector = place_sector
        orders = {}
        for code, sector, order in self.con.execute(
                "SELECT l.code, s.sector, s.sort_order FROM line_sector s JOIN line l ON l.id = s.line_id"):
            orders[(code, sector)] = order

        def line_rank(grp):
            order = orders.get((grp.line, grp.sector or sort_sector))
            return (order is None, order if order is not None else 0, grp.line)

        def rank(grp):
            first = grp.board.passages[0].epoch if grp.board.passages else None
            return (SECTORS.index(grp.sector) if grp.sector else 0,
                    first is None, first if first is not None else 0.0, *line_rank(grp), grp.key)
        return sorted(out, key=rank)

    # -- itinéraires hors connexion (R-119) ------------------------------------------------------
    def offline_journeys(self, origins: Iterable[str], targets: Iterable[str], now: datetime,
                         limit: int = OFFLINE_LIMIT) -> dict:
        """Itinéraires théoriques sur les seules lignes de la base, quand PRIM est injoignable (R-119).

        origins : quais de départ (stop_id GTFS), où l'on se trouve à l'instant `now` ; targets : quais d'arrivée.
        Algorithme « Connection Scan » à l'arrivée la plus tôt, sur les jours de service de la veille, du jour et du
        lendemain, pour les départs des 12 h qui suivent. Une correspondance se fait au même quai (2 min au moins)
        ou vers un autre quai par une ligne de transfers.txt (temps publié par IDFM, marche comprise), une seule
        à la fois ; jamais par une distance estimée. La montée et la descente suivent R-32, les quais non desservis
        R-91. Jusqu'à `limit` trajets, chacun partant au moins une seconde après le précédent ; une marche seule
        clôt la recherche. Classement comme en ligne (R-118).

        Rend {"status", "incomplete", "journeys"} ; status : "ok", "no_solution", "base_expired" (R-173),
        "already_there" (un quai de départ est un quai d'arrivée) ou "no_stop" (aucun quai d'un côté).
        incomplete : le jour de service de la veille n'est pas couvert et ses trajets peuvent encore circuler
        (R-153) ; des trajets peuvent manquer. Les trajets ont la forme de la lecture des itinéraires en ligne
        (tools/app/journeys_reference.py) : heures théoriques, sans temps réel ni perturbation."""
        origins, targets = sorted(set(origins)), frozenset(targets)
        today = now.astimezone(self.tz).date()
        if today < self.valid_start or today > self.valid_end:
            return {"status": "base_expired", "incomplete": False, "journeys": []}
        if not origins or not targets:
            return {"status": "no_stop", "incomplete": False, "journeys": []}
        if targets.intersection(origins):
            return {"status": "already_there", "incomplete": False, "journeys": []}
        now_s = math.ceil(now.timestamp())
        yesterday = today - timedelta(days=1)
        incomplete = yesterday < self.valid_start and now_s < self.epoch(yesterday, self.max_departure())
        conns = self._offline_connections(today, now_s)
        transfers = self._offline_transfers()
        found, start = [], now_s
        for _ in range(OFFLINE_MAX_SCANS):
            j = self._scan(conns, transfers, origins, targets, start)
            if j is None:
                break
            if j["signature"] not in {f["signature"] for f in found}:
                found.append(j)
            if j["walk_only"] or len(_undominated(found)) >= limit:
                break
            start = j["departure"] + 1
        if not found:
            return {"status": "no_solution", "incomplete": incomplete, "journeys": []}
        kept_ = sorted(_undominated(found), key=lambda j: (j["arrival"], -j["departure"], j["transfers"], j["walking"],
                                                          j["signature"]))
        return {"status": "ok", "incomplete": incomplete, "journeys": kept_[:limit]}

    def _offline_transfers(self) -> dict:
        """Correspondances par quai de départ, dans l'ordre des stop_id GTFS d'arrivée."""
        out = defaultdict(list)
        for a, b, t in self.con.execute(
                """SELECT a.gtfs_id, b.gtfs_id, t.min_time FROM transfer t
                   JOIN stop_point a ON a.id = t.from_stop_point_id JOIN stop_point b ON b.id = t.to_stop_point_id
                   ORDER BY a.gtfs_id, b.gtfs_id"""):
            out[a].append((b, t))
        return out

    def _offline_connections(self, today: date, now_s: int) -> list:
        """Connexions (départ d'un quai, arrivée au quai suivant d'un même trajet) des jours de service de la
        veille, du jour et du lendemain, au départ dans [now_s, now_s + 12 h], triées par (départ, arrivée, jour,
        trajet, rang). Chaque connexion dit si l'on peut monter au premier quai et descendre au second."""
        end = now_s + OFFLINE_HORIZON_S
        # R-91 : quais non desservis par l'aménagement en vigueur le jour de la consultation (comme board()).
        blocked = set()
        for (code,) in self.con.execute("SELECT code FROM line ORDER BY code").fetchall():
            for adj in self.adjustments(code, today):
                blocked.update((code, r.stop_point) for r in adj.not_served)
        conns = []
        for d in (today - timedelta(days=1), today, today + timedelta(days=1)):
            if not self.valid_start <= d <= self.valid_end:
                continue
            idx = (d - self.base).days
            origin = int(self.service_day_origin(d))
            prev = None
            for tid, pid, seq, dep, dwell, sp, pickup, dropoff, code in self.con.execute(
                    """SELECT t.id, t.pattern_id, st.seq, st.departure, st.dwell, sp.gtfs_id, ps.pickup, ps.dropoff, l.code
                       FROM trip t JOIN service s ON s.id = t.service_id
                       JOIN stop_time st ON st.trip_id = t.id
                       JOIN pattern_stop ps ON ps.pattern_id = t.pattern_id AND ps.seq = st.seq
                       JOIN stop_point sp ON sp.id = ps.stop_point_id
                       JOIN pattern p ON p.id = t.pattern_id JOIN line l ON l.id = p.line_id
                       WHERE ((s.days >> ?) & 1) = 1 ORDER BY t.id, st.seq""", (idx,)):
                cur = (tid, seq, origin + dep, origin + dep - dwell, sp, pickup, dropoff)
                if prev is not None and prev[0] == tid and now_s <= prev[2] <= end:
                    conns.append((prev[2], cur[3], d.toordinal(), tid, prev[1], pid, code, prev[4], cur[4],
                                  prev[5] != 1 and (code, prev[4]) not in blocked,
                                  cur[6] != 1 and (code, cur[4]) not in blocked))
                prev = cur
        conns.sort(key=lambda c: (c[0], c[1], c[2], c[3], c[4]))
        return conns

    def _scan(self, conns: list, transfers: dict, origins: list, targets: frozenset, start: int) -> Optional[dict]:
        """Arrivée la plus tôt pour un départ à `start`. États par quai : arrivée par un véhicule (d'où partent
        les correspondances), présence au quai, quai prêt pour une montée. Les états précédents sont gardés dans
        des tuples immuables :
          ("origin", quai) ; ("walk", de, vers, durée, départ, état précédent) ;
          ("vehicle", montée, quai de descente, rang de descente, heure d'arrivée),
          montée = (quai, rang, heure, état de préparation du quai, mission, ligne)."""
        inf = float("inf")
        vehicle, reach, ready = {}, {}, {}
        best, best_sp = inf, None

        def walk(frm: str, t0: int, prev: tuple) -> None:
            nonlocal best, best_sp
            for w, t in transfers.get(frm, ()):
                a = t0 + t
                wp = ("walk", frm, w, t, t0, prev)
                if a < ready.get(w, (inf,))[0]:
                    ready[w] = (a, wp)
                if a < reach.get(w, (inf,))[0]:
                    reach[w] = (a, wp)
                    if w in targets and a < best:
                        best, best_sp = a, w

        for o in origins:
            reach[o] = ready[o] = (start, ("origin", o))
        for o in origins:
            walk(o, start, ("origin", o))
        boarded = {}
        for dep, arr, day, tid, seq, pid, code, frm, to, can_board, can_alight in conns:
            if dep >= best:
                break
            key = (day, tid)
            b = boarded.get(key)
            if b is None and can_board and ready.get(frm, (inf,))[0] <= dep:
                b = boarded[key] = (frm, seq, dep, ready[frm][1], pid, code)
            if b is None or not can_alight or arr >= vehicle.get(to, (inf,))[0]:
                continue
            parent = ("vehicle", b, to, seq + 1, arr)
            vehicle[to] = (arr, parent)
            if arr < reach.get(to, (inf,))[0]:
                reach[to] = (arr, parent)
                if to in targets and arr < best:
                    best, best_sp = arr, to
            if arr + OFFLINE_SAME_STOP_S < ready.get(to, (inf,))[0]:
                ready[to] = (arr + OFFLINE_SAME_STOP_S, parent)
            walk(to, arr, parent)
        if best_sp is None:
            return None
        return self._offline_journey(reach[best_sp][1], best)

    def _stop_info(self) -> dict:
        if self._stops is None:
            self._stops = {sp: (name, area) for sp, name, area in self.con.execute(
                "SELECT sp.gtfs_id, sp.name, a.gtfs_id FROM stop_point sp JOIN stop_area a ON a.id = sp.stop_area_id")}
        return self._stops

    def _offline_journey(self, last: tuple, arrival: int) -> dict:
        """Étapes du trajet, dans le format de la lecture des itinéraires en ligne."""
        steps, node = [], last
        while node[0] != "origin":
            steps.append(node)
            node = node[5] if node[0] == "walk" else node[1][3]
        steps.reverse()
        stops = self._stop_info()
        lines = {code: (route_id, short, color, text_color, network) for code, route_id, short, color, text_color, network
                 in self.con.execute("SELECT code, route_id, short_name, color, text_color, network FROM line")}
        legs, here = [], None            # here : instant de présence au quai courant
        for i, s in enumerate(steps):
            if s[0] == "walk":
                _, frm, to, dur, begin, _prev = s
                if i == 0 and len(steps) > 1:
                    begin = steps[1][1][2] - dur      # marche de départ : juste à temps pour la montée (comme Navitia)
                if dur > 0:
                    legs.append({"kind": "walk", "departure": begin, "arrival": begin + dur, "duration": dur,
                                 "from": stops[frm][0], "to": stops[to][0]})
                here = begin + dur
                continue
            _, (frm, board_seq, board_time, _rp, pid, code), to, alight_seq, alight_time = s
            if here is not None and board_time > here:
                legs.append({"kind": "wait", "duration": board_time - here})
            route_id, short, color, text_color, network = lines[code]
            names = [stops[sp][0] for seq, sp, _drop in self.pattern_stops(pid) if board_seq <= seq <= alight_seq]
            legs.append({
                "kind": "pt", "line": f"line:{route_id}", "code": short or code, "mode": "", "physical_mode": "",
                "network": network or "", "color": color.lstrip("#").upper() if color else None,
                "text_color": text_color.lstrip("#").upper() if text_color else None,
                "direction": self.terminus(pid), "vehicle_journey": None,
                "from": {"stop_point": f"stop_point:{frm}", "stop_area": f"stop_area:{stops[frm][1]}", "name": stops[frm][0]},
                "to": {"stop_point": f"stop_point:{to}", "stop_area": f"stop_area:{stops[to][1]}", "name": stops[to][0]},
                "departure": board_time, "arrival": alight_time, "base_departure": board_time,
                "base_arrival": alight_time, "realtime": False, "stops": alight_seq - board_seq,
                "stop_names": names, "wheelchair": False, "notices": [],
            })
            here = alight_time
        pts = [l for l in legs if l["kind"] == "pt"]
        departure = legs[0]["departure"] if legs else arrival
        walking = sum(l["duration"] for l in legs if l["kind"] == "walk")
        signature = ("|".join(f"{l['line']}@{l['from']['stop_point']}@{l['departure']}" for l in pts)
                     if pts else f"walk@{departure}@{arrival}")
        return {"departure": departure, "arrival": arrival, "duration": arrival - departure,
                "transfers": max(len(pts) - 1, 0), "walking": walking, "walk_only": not pts,
                "signature": signature, "disrupted_lines": [], "legs": legs}
