#!/usr/bin/env python3
"""
Info trafic, règles cibles de la v2 (spec § 9 : R-80 à R-82, R-85 ; docs/modele-donnees.md § 8).

Source : l'info trafic globale de PRIM (`disruptions_bulk/disruptions/v2`, une seule réponse pour tout le réseau,
gardée 120 s dans le cache du proxy, R-84). Une réponse contient :

- `disruptions` : chaque perturbation (`id`, `applicationPeriods` en heure de Paris, `cause`, `severity`, `title`,
  `message` en HTML, `shortMessage` facultatif, `impactedSections` facultatif) ;
- `lines` : les seules lignes perturbées, chacune avec ses `impactedObjects` (`line`, `network` ou `stop_point`)
  et les perturbations qui les touchent ;
- `lastUpdatedDate` : l'heure de la mise à jour d'IDFM (UTC).

Fonctions :

- `line_disruptions` : perturbations d'une ligne (rattachement R-81, filtre d'axe R-82, état en cours ou à venir) ;
- `line_status` : statut affiché d'une ligne (R-80) ;
- `reading_state` : fraîcheur d'une réponse (R-85) ;
- `html_to_text` : texte affiché d'un message.

Bibliothèque standard uniquement ; l'application (data/traffic/TrafficRules.kt) reproduit ces fonctions à
l'identique, vérifiée par TrafficGoldenTest sur la réponse réelle du 14 octobre 2026 et sur les cas cibles de
tests/rules-v2/traffic_rules.json.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import v1_rules

PARIS = ZoneInfo("Europe/Paris")

# R-80 : rang des sévérités d'IDFM (la plus forte l'emporte) ; une sévérité inconnue compte comme une information.
SEVERITY_RANK = {"BLOQUANTE": 3, "PERTURBEE": 2, "INFORMATION": 1}
# R-85 : une perturbation « à venir » commence dans les 7 jours ; au-delà, elle n'est pas affichée.
UPCOMING_S = 7 * 86400
# R-85 : une réponse sert 10 min après sa réception ; une mise à jour d'IDFM de plus de 30 min est périmée.
READING_MAX_AGE_S = 600
SOURCE_MAX_AGE_S = 1800

# R-82 : axes couverts des RER (R-40), chacun entre deux zones d'arrêt GTFS. Les arrêts couverts sont ceux qu'une
# mission de la ligne parcourt entre les deux extrémités d'un axe.
AXES = {
    # Aéroport CDG (Terminal 2) - TGV ↔ Saint-Rémy-lès-Chevreuse (Massy - Palaiseau, terminus partiel, est entre les deux).
    "RER_B": [("IDFM:73699", "IDFM:62978")],
    # Gare d'Austerlitz ↔ Massy - Palaiseau par Pont de Rungis (Bibliothèque François Mitterrand est entre les deux).
    "RER_C": [("IDFM:71135", "IDFM:63244")],
}
# Nom de ligne attendu par la règle v1 en texte libre (tests/characterization/traffic_axis_filter.json).
V1_LINE_NAMES = {"RER_B": "RER B", "RER_C": "RER C"}

# R-81 : titre d'une perturbation d'arrêt créée pour une ligne précise (« Bus 152 : Mesures de sécurité - … »).
# Espaces de `str.isspace`, écrits en clair comme dans l'application (le moteur d'Android et la JVM lisent `\s`
# différemment).
_WS = "[\t\n\x0b\x0c\r\x1c-\x1f \x85\xa0  -     　]"
_TITLE_LINE = re.compile(rf"^{_WS}*(?:bus|tramway|tram|m[ée]tro|rer|train|noctilien|ligne){_WS}*([0-9a-z]+){_WS}*:", re.I)


_LOCAL = re.compile(r"[0-9]{8}T[0-9]{6}")


def parse_local(value: str) -> int:
    """
    `AAAAMMJJTHHMMSS` en heure de Paris → secondes Unix. Heure ambiguë (passage à l'heure d'hiver) : la première ;
    heure inexistante (passage à l'heure d'été) : décalée de la durée du saut. ValueError si mal formée.
    """
    if not _LOCAL.fullmatch(value):
        raise ValueError(value)
    dt = datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=PARIS, fold=0)
    return int(dt.timestamp())


def parse_utc(value: str) -> int:
    """`lastUpdatedDate` (ISO 8601, `Z`) → secondes Unix, fraction tronquée."""
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp())


def strip_prefix(ident: str) -> str:
    """`stop_area:IDFM:71135` → `IDFM:71135` ; `stop_point:IDFM:monomodalStopPlace:47905` → `IDFM:monomodalStopPlace:47905`."""
    for p in ("stop_area:", "stop_point:", "line:"):
        if ident.startswith(p):
            return ident[len(p):]
    return ident


# ---------------------------------------------------------------------------------------------------------------
# Texte d'un message
# ---------------------------------------------------------------------------------------------------------------
_BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "table"}
_INLINE_TAGS = {"strong", "b", "em", "i", "u", "span", "a", "font", "small", "sup", "sub", "td", "th", "tbody", "thead"}
_TAG = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9]*)(?!\w)[^<>]*?(/?)>")
_ENTITY = re.compile(r"&(#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);")
_NAMED = {
    "nbsp": " ", "amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'", "rsquo": "’", "lsquo": "‘",
    "rdquo": "”", "ldquo": "“", "laquo": "«", "raquo": "»", "hellip": "…", "ndash": "–", "mdash": "—",
    "euro": "€", "deg": "°", "middot": "·",
    "eacute": "é", "egrave": "è", "ecirc": "ê", "euml": "ë", "agrave": "à", "acirc": "â", "auml": "ä",
    "ccedil": "ç", "icirc": "î", "iuml": "ï", "ocirc": "ô", "ouml": "ö", "ugrave": "ù", "ucirc": "û", "uuml": "ü",
    "Eacute": "É", "Egrave": "È", "Ecirc": "Ê", "Agrave": "À", "Acirc": "Â", "Ccedil": "Ç", "Icirc": "Î",
    "Ocirc": "Ô", "Ucirc": "Û", "oelig": "œ", "OElig": "Œ",
}


def _entity(m: re.Match) -> str:
    body = m.group(1)
    if body[0] == "#":
        try:
            code = int(body[2:], 16) if body[1] in "xX" else int(body[1:])
            return chr(code) if 0 < code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else m.group(0)
        except ValueError:
            return m.group(0)
    return _NAMED.get(body, m.group(0))


def html_to_text(html: str | None) -> str:
    """
    Message HTML d'IDFM → texte affiché. Seules les balises HTML connues sont lues : un paragraphe est séparé par une
    ligne vide, un saut de ligne ou une rangée de tableau passe à la ligne, un élément de liste commence une ligne
    par « • » (une liste se termine par un passage à la ligne), les autres balises connues sont retirées ; une
    balise inconnue reste telle quelle (IDFM écrit « Gare de Juvisy <RER> » en clair). Entités décodées (une entité
    inconnue reste telle quelle), espaces insécables et tabulations ramenés à une espace, une ligne vide au plus.
    """
    if not html:
        return ""
    out = []
    pos = 0
    for m in _TAG.finditer(html):
        out.append(_ENTITY.sub(_entity, html[pos:m.start()]))
        closing, name = m.group(1) == "/", m.group(2).lower()
        if name in ("br", "tr"):
            out.append("\n")
        elif name == "li":
            if not closing:
                out.append("\n• ")
        elif name in ("ul", "ol"):
            if closing:
                out.append("\n")
        elif name in _BLOCK_TAGS:
            out.append("\n\n")
        elif name in _INLINE_TAGS:
            pass
        else:
            out.append(_ENTITY.sub(_entity, m.group(0)))
        pos = m.end()
    out.append(_ENTITY.sub(_entity, html[pos:]))
    text = "".join(out).replace(" ", " ").replace(" ", " ").replace("\t", " ").replace("\r", "")
    lines = [re.sub(r" {2,}", " ", ln).strip() for ln in text.split("\n")]
    result, blank = [], False
    for ln in lines:
        if not ln:
            blank = bool(result)
            continue
        if blank:
            result.append("")
        result.append(ln)
        blank = False
    return "\n".join(result)


# ---------------------------------------------------------------------------------------------------------------
# R-82 : axes couverts
# ---------------------------------------------------------------------------------------------------------------
def covered_areas(line_code: str, patterns: list) -> frozenset | None:
    """
    Zones d'arrêt couvertes d'un RER à axes (R-40), ou None pour une ligne sans axe. `patterns` : missions de la
    ligne, chacune la liste ordonnée de ses quais `(zone d'arrêt, quai)`.
    """
    axes = AXES.get(line_code)
    if axes is None:
        return None
    out = set()
    for p in patterns:
        areas = [a for a, _ in p]
        for a, b in axes:
            if a in areas and b in areas:
                i, j = areas.index(a), areas.index(b)
                lo, hi = min(i, j), max(i, j)
                out.update(areas[lo:hi + 1])
    return frozenset(out)


def _section_areas(a: str, b: str, patterns: list) -> list:
    """Zones parcourues entre deux zones d'arrêt : le plus court tronçon d'une mission qui passe par les deux."""
    best = None
    for p in patterns:
        areas = [x for x, _ in p]
        if a in areas and b in areas:
            i, j = areas.index(a), areas.index(b)
            lo, hi = min(i, j), max(i, j)
            if best is None or hi - lo < len(best) - 1:
                best = areas[lo:hi + 1]
    if best is not None:
        return best
    known = {x for p in patterns for x, _ in p}
    return [x for x in (a, b) if x in known]


def impacted_areas(disruption: dict, line_id: str, stop_points: list, patterns: list) -> frozenset:
    """Zones d'arrêt de la ligne touchées : tronçons (`impactedSections` de la ligne) et quais cités pour la ligne."""
    area_of = {sp: a for p in patterns for a, sp in p}
    known = set(area_of.values())

    def area(obj: dict) -> str | None:
        ident = strip_prefix(obj.get("id") or "")
        if obj.get("type") == "stop_point":
            return area_of.get(ident)
        return ident if ident in known else None

    out = set()
    for s in disruption.get("impactedSections") or []:
        if s.get("lineId") != line_id:
            continue
        a, b = area(s.get("from") or {}), area(s.get("to") or {})
        if a and b:
            out.update(_section_areas(a, b, patterns))
        else:
            out.update(x for x in (a, b) if x)
    for sp in stop_points:
        a = area_of.get(strip_prefix(sp))
        if a:
            out.add(a)
    return frozenset(out)


def keep_on_axis(line_code: str, disruption: dict, line_id: str, stop_points: list, patterns: list) -> bool:
    """
    R-82 : une perturbation d'un RER à axes est gardée si elle touche l'axe couvert. Sur les zones d'arrêt touchées
    (tronçons et quais) : gardée si au moins deux sont couvertes (un tronçon de l'axe) ou si toutes le sont (un
    arrêt de l'axe) ; écartée sinon (une branche exclue, qui ne partage au plus qu'un arrêt de bifurcation). Sans
    zone connue, la règle v1 en texte libre s'applique au titre et au message (traffic_axis_filter.json).
    """
    covered = covered_areas(line_code, patterns)
    if covered is None:
        return True
    hit = impacted_areas(disruption, line_id, stop_points, patterns)
    if not hit:
        return v1_rules.keep_traffic_incident([V1_LINE_NAMES[line_code]], [], disruption.get("title") or "",
                                              html_to_text(disruption.get("message")))
    return len(hit & covered) >= 2 or hit <= covered


# ---------------------------------------------------------------------------------------------------------------
# R-81 : rattachement, R-85 : périodes
# ---------------------------------------------------------------------------------------------------------------
def title_line(title: str | None) -> str | None:
    """Numéro de ligne en tête d'un titre (« Bus 152 : … » → « 152 », « RERC: … » → « C »), en minuscules."""
    m = _TITLE_LINE.match(title or "")
    return m.group(1).lower() if m else None


def timing(disruption: dict, now_s: int) -> dict:
    """
    État d'une perturbation à l'instant `now_s` : `current` (une période en cours ; `until` = sa fin), `upcoming`
    (prochaine période dans les 7 jours ; `next` = son début, `next_end` = sa fin) ou `hidden` (terminée ou trop
    lointaine). Toujours : `periods` (nombre de périodes lisibles) et `last_end` (fin de la dernière, ou None).
    Une perturbation sans période est en cours, sans fin connue ; une période illisible (date mal formée, fin avant
    le début) est ignorée, et une perturbation dont aucune période n'est lisible n'est pas affichée. Périodes en
    heure de Paris, fin exclue.
    """
    raw = disruption.get("applicationPeriods") or []
    if not raw:
        return {"state": "current", "until": None, "periods": 0, "last_end": None}
    periods = []
    for p in raw:
        try:
            b, e = parse_local(p.get("begin") or ""), parse_local(p.get("end") or "")
        except ValueError:
            continue
        if e > b:
            periods.append((b, e))
    if not periods:
        return {"state": "hidden", "periods": 0, "last_end": None}
    base = {"periods": len(periods), "last_end": max(e for _, e in periods)}
    current = [e for b, e in periods if b <= now_s < e]
    if current:
        return {"state": "current", "until": max(current), **base}
    future = sorted((b, e) for b, e in periods if b > now_s)
    if future and future[0][0] - now_s <= UPCOMING_S:
        return {"state": "upcoming", "next": future[0][0], "next_end": future[0][1], **base}
    return {"state": "hidden", **base}


def _rank(d: dict) -> int:
    return SEVERITY_RANK.get(d.get("severity"), 1)


def line_disruptions(bulk: dict, line_code: str, route_id: str, patterns: list, now_s: int) -> list:
    """
    Perturbations affichées d'une ligne (R-81, R-82, R-85), en cours puis à venir ; dans chaque groupe, sévérité
    décroissante, puis début (en cours : fin la plus proche d'abord ; à venir : début le plus proche), puis identifiant.

    Rattachement (R-81) : une perturbation citée par l'entrée de la ligne au niveau `line` ou `network`, ou dont un
    tronçon (`impactedSections`) est sur la ligne, est une perturbation **de ligne**. Une perturbation citée seulement
    à des quais est une perturbation **d'arrêt** : écartée si son titre nomme une autre ligne que le `shortName` de
    l'entrée (IDFM crée une perturbation par ligne et la rattache aux quais, partagés entre lignes) ; gardée sinon,
    avec le nom de ses quais. `patterns` : missions de la ligne (R-82), vide pour une ligne sans axe.
    """
    line_id = "line:" + route_id
    entry = next((l for l in bulk.get("lines") or [] if l.get("id") == line_id), None)
    if entry is None:
        return []
    by_id = {d["id"]: d for d in bulk.get("disruptions") or []}
    level, stops, order = set(), {}, []
    for obj in entry.get("impactedObjects") or []:
        for did in obj.get("disruptionIds") or []:
            if did not in by_id:
                continue
            if did not in stops:
                stops[did] = []
                order.append(did)
            if obj.get("type") == "stop_point":
                if obj.get("id") and obj["id"] not in [s for s, _ in stops[did]]:
                    stops[did].append((obj["id"], obj.get("name") or ""))
            else:
                level.add(did)
    own = (entry.get("shortName") or "").lower()
    out = []
    for did in order:
        d = by_id[did]
        on_line = did in level or any(s.get("lineId") == line_id for s in d.get("impactedSections") or [])
        if not on_line:
            named = title_line(d.get("title"))
            if named is not None and named != own:
                continue
        sps = [s for s, _ in stops[did]]
        if not keep_on_axis(line_code, d, line_id, sps, patterns):
            continue
        t = timing(d, now_s)
        if t["state"] == "hidden":
            continue
        out.append({
            "id": did,
            "kind": "line" if on_line else "stop",
            "severity": d.get("severity"),
            "cause": d.get("cause"),
            "title": d.get("title") or "",
            "short_message": d.get("shortMessage"),
            "stops": [] if on_line else sorted({n for _, n in stops[did]}),
            **t,
        })

    def key(x):
        when = (x["until"] if x["until"] is not None else 2 ** 62) if x["state"] == "current" else x["next"]
        return (x["state"] != "current", -SEVERITY_RANK.get(x["severity"], 1), when, x["id"])

    out.sort(key=key)
    return out


def line_status(items: list) -> dict:
    """
    R-80 : statut d'une ligne d'après ses perturbations affichées. Sans perturbation en cours : `clear` (aucune) ou
    `upcoming` (n à venir). Sinon, la plus forte en cours donne le niveau (`blocking`, `disrupted`, `info`) et le
    libellé : « Information » pour une sévérité INFORMATION, « Travaux » pour la cause TRAVAUX, « Trafic perturbé »
    sinon ; son `shortMessage` est repris tel quel s'il existe et n'est pas « Autre ». À sévérité égale, la première
    dans l'ordre de `line_disruptions`.
    """
    current = [x for x in items if x["state"] == "current"]
    upcoming = [x for x in items if x["state"] == "upcoming"]
    if not current:
        return {"level": "upcoming" if upcoming else "clear", "label": None, "detail": None,
                "current": 0, "upcoming": len(upcoming)}
    top = max(current, key=lambda x: SEVERITY_RANK.get(x["severity"], 1))
    rank = SEVERITY_RANK.get(top["severity"], 1)
    level = {3: "blocking", 2: "disrupted"}.get(rank, "info")
    label = "information" if rank == 1 else ("works" if top["cause"] == "TRAVAUX" else "disrupted")
    sm = (top["short_message"] or "").strip()
    detail = top["short_message"] if sm and sm.lower() != "autre" else None
    return {"level": level, "label": label, "detail": detail, "current": len(current), "upcoming": len(upcoming)}


def reading_state(received_s: int, last_updated_s: int | None, now_s: int) -> str:
    """R-85 : `ok` ou `stale` (réponse reçue il y a plus de 10 min, ou mise à jour d'IDFM de plus de 30 min)."""
    if now_s - received_s > READING_MAX_AGE_S:
        return "stale"
    if last_updated_s is None or received_s - last_updated_s > SOURCE_MAX_AGE_S:
        return "stale"
    return "ok"
