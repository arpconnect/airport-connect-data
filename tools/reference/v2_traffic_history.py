#!/usr/bin/env python3
"""
Historique des incidents de trafic (spec R-86, décision D-17 du 16 octobre 2026), en Python exécutable de référence.

Un collecteur relit l'info trafic globale d'IDFM (R-85) à intervalle régulier (`tools/pipeline/traffic_history.py`,
traitement planifié du dépôt public) et tient un **état** : chaque perturbation rattachée à une ligne suivie (R-81,
R-82), sa dernière version, et quand elle a été vue. Les règles d'historique se lisent ensuite dans cet état, à
n'importe quel instant :

- `observe(state, bulk, observed_s, lines)` : met l'état à jour avec une réponse d'IDFM lue à `observed_s` ;
- `is_incident(record)` : incident, ou perturbation planifiée (travaux, information, événement annoncé à l'avance,
  perturbation de longue durée) ;
- `window(state, now_s, window_s)` : incidents de chaque ligne sur une fenêtre glissante (24 h, 7 jours), les
  doublons d'IDFM regroupés.

Constat du relevé du 14 octobre 2026 (20 h 20) : IDFM publie la plupart des incidents avec une période d'une minute
(« Malaise voyageur » du T7 : 19:37:32 → 19:38:32), alors que l'incident reste publié bien après ; il publie aussi
un même incident en plusieurs exemplaires (trois « Mesures de sécurité » sur la 152, à 19:45 et 19:49). La durée d'un
incident est donc celle de sa **publication** (de son début à sa disparition de l'info trafic), pas sa période.

L'état garde aussi les perturbations planifiées : la règle d'incident peut évoluer sans perdre l'historique.
Bibliothèque standard, plus `v2_traffic` (rattachement et filtre d'axe, identiques à l'onglet Trafic).
"""

from __future__ import annotations

import v2_traffic as tr

SCHEMA = "airport-connect/traffic-history@1"
RETAIN_S = 8 * 86400            # l'état garde 8 jours : la fenêtre de 7 jours, plus une marge
ANNOUNCED_AHEAD_S = 30 * 60     # une perturbation vue plus de 30 min avant son début a été annoncée à l'avance
LONG_SPAN_S = 24 * 3600         # une perturbation déclarée sur plus de 24 h n'est pas un incident (déviation, grève)
SAME_INCIDENT_S = 30 * 60       # même ligne, même titre, débuts à 30 min au plus : un seul incident (doublons d'IDFM)
MAX_GAP_S = 20 * 60             # au-delà de 20 min sans lecture, le suivi est interrompu (lacune signalée)
DAY_S = 86400
WEEK_S = 7 * 86400
INCIDENT_SEVERITIES = ("BLOQUANTE", "PERTURBEE")


def new_state() -> dict:
    return {"schema": SCHEMA, "tracking_since": None, "observations": [], "disruptions": {}}


def _periods(d: dict) -> list:
    """Périodes lisibles, en secondes Unix, triées ; une période illisible ou vide est ignorée (comme R-85)."""
    out = []
    for p in d.get("applicationPeriods") or []:
        try:
            b, e = tr.parse_local(p.get("begin") or ""), tr.parse_local(p.get("end") or "")
        except ValueError:
            continue
        if e > b:
            out.append([b, e])
    return sorted(out)


def _last_update(d: dict):
    try:
        return tr.parse_local(d.get("lastUpdate") or "")
    except ValueError:
        return None


def observe(state: dict, bulk: dict, observed_s: int, lines: list) -> dict:
    """
    Met l'état à jour avec une réponse de l'info trafic globale lue à `observed_s` (heure de la lecture chez IDFM,
    `x-airportconnect-fetched-at` du proxy). `lines` : lignes suivies, `(code, route_id, patterns)`.

    - Une lecture déjà enregistrée (même instant) est ignorée : le proxy sert la même réponse à plusieurs appels.
    - Chaque perturbation rattachée à une ligne suivie est enregistrée à sa première lecture (`first_seen`), puis
      mise à jour à chaque lecture (`last_seen`, dernière version : titre, sévérité, cause, périodes) ; ses lignes
      s'accumulent (une perturbation peut toucher plusieurs lignes suivies).
    - `announced` est fixé à la première lecture : vraie si le premier début connu est à plus de 30 min de cette
      lecture. Une perturbation déjà publiée à la toute première lecture de l'état (début du suivi) est marquée
      `at_tracking_start` : on ne sait pas quand elle a été publiée.
    - Une perturbation absente d'une lecture alors qu'elle était présente à la précédente est notée disparue à cette
      lecture (`gone_at`) ; si elle réapparaît, `gone_at` est effacé.
    - L'état ne garde que 8 jours de lectures et de perturbations.
    """
    obs = state["observations"]
    if obs and observed_s <= obs[-1]:
        return state
    first = not obs
    if first:
        state["tracking_since"] = observed_s
    previous = obs[-1] if obs else None
    obs.append(observed_s)
    seen = set()
    recs = state["disruptions"]
    for code, route_id, patterns in lines:
        for a in tr.attached_disruptions(bulk, code, route_id, patterns):
            d, did = a["disruption"], a["id"]
            seen.add(did)
            periods = _periods(d)
            rec = recs.get(did)
            if rec is None:
                begin = periods[0][0] if periods else None
                rec = recs[did] = {
                    "first_seen": observed_s,
                    "at_tracking_start": first,
                    "announced": begin is not None and begin - observed_s > ANNOUNCED_AHEAD_S,
                    "lines": {},
                }
            rec.update({
                "last_seen": observed_s,
                "gone_at": None,
                "cause": d.get("cause"),
                "severity": d.get("severity"),
                "title": d.get("title") or "",
                "short_message": d.get("shortMessage"),
                "periods": periods,
                "last_update": _last_update(d),
            })
            rec["lines"][code] = {"kind": a["kind"], "stops": a["stops"]}
    for did, rec in recs.items():
        if did not in seen and rec.get("gone_at") is None and previous is not None and rec["last_seen"] == previous:
            rec["gone_at"] = observed_s
    horizon = observed_s - RETAIN_S
    state["observations"] = [t for t in obs if t >= horizon]
    state["disruptions"] = {k: r for k, r in recs.items() if (r["gone_at"] or r["last_seen"]) >= horizon}
    return state


def is_incident(rec: dict) -> bool:
    """
    Incident (D-17) : perturbation imprévue. Toutes les conditions :
    - cause « PERTURBATION » d'IDFM (pas « TRAVAUX » ni « INFORMATION ») ;
    - sévérité bloquante ou perturbante (pas « INFORMATION ») ;
    - pas annoncée : vue pour la première fois moins de 30 min avant son début (une manifestation, des horaires de
      vacances ou une perturbation du lendemain, publiés à l'avance, sont planifiés) ;
    - courte : ses périodes tiennent en 24 h au plus (une déviation de plusieurs semaines ou une grève d'une semaine,
      même publiées sous la cause « PERTURBATION », sont planifiées) ;
    - publiée après le début du suivi : pour une perturbation déjà là, on ne sait pas si elle a été annoncée.
    """
    periods = rec.get("periods") or []
    span = (periods[-1][1] - periods[0][0]) if periods else 0
    return (rec.get("cause") == "PERTURBATION" and rec.get("severity") in INCIDENT_SEVERITIES
            and not rec.get("announced") and not rec.get("at_tracking_start") and span <= LONG_SPAN_S)


def interval(rec: dict) -> tuple:
    """
    Durée d'un incident : `(début, fin)`. Début : le premier début de période d'IDFM (sa première lecture s'il n'a
    pas de période). Fin : la lecture où il a disparu de l'info trafic ; None tant qu'il est publié. Les fins de
    période ne comptent pas : IDFM publie souvent une période d'une minute pour un incident qui dure.
    """
    periods = rec.get("periods") or []
    start = periods[0][0] if periods else rec["first_seen"]
    return start, rec.get("gone_at")


def gaps(state: dict, now_s: int, window_s: int) -> list:
    """Lacunes du suivi sur la fenêtre : intervalles de plus de 20 min sans lecture, `[début, fin]`."""
    lo = now_s - window_s
    obs = [t for t in state["observations"] if t <= now_s]
    out, prev = [], max(lo, state.get("tracking_since") or lo)
    for t in obs + [now_s]:
        if t < lo:
            continue
        if t - prev > MAX_GAP_S:
            out.append([prev, t])
        prev = max(prev, t)
    return out


def window(state: dict, now_s: int, window_s: int) -> dict:
    """
    Incidents de chaque ligne suivie sur la fenêtre glissante `[now − window, now]` (24 h : `DAY_S`, 7 jours :
    `WEEK_S`) : un incident compte s'il a eu lieu, même en partie, dans la fenêtre. Les exemplaires d'un même incident
    (même ligne, même titre, débuts à 30 min au plus du premier) n'en font qu'un : début le plus tôt, fin la plus
    tardive (aucune si l'un est encore publié), sévérité la plus forte, quais réunis. Rend, par ligne, la liste triée
    (plus récent début d'abord, puis premier identifiant) des incidents `{ids, start, end, severity, title,
    short_message, kind, stops}`, et `coverage` : début du suivi et lacunes dans la fenêtre (le compte est alors un
    minimum).
    """
    lo = now_s - window_s
    raw = {}
    for did, rec in state["disruptions"].items():
        if not is_incident(rec):
            continue
        start, end = interval(rec)
        if start > now_s or (end is not None and end <= lo):
            continue
        for code, att in rec["lines"].items():
            raw.setdefault(code, []).append((start, did, end, rec, att))
    by_line = {}
    for code, items in raw.items():
        groups = []
        for start, did, end, rec, att in sorted(items, key=lambda x: (x[0], x[1])):
            g = next((g for g in groups if g["title"] == rec["title"] and start - g["start"] <= SAME_INCIDENT_S), None)
            if g is None:
                groups.append({"ids": [did], "start": start, "end": end, "severity": rec["severity"],
                               "title": rec["title"], "short_message": rec["short_message"], "kind": att["kind"],
                               "stops": list(att["stops"])})
                continue
            g["ids"].append(did)
            g["end"] = None if g["end"] is None or end is None else max(g["end"], end)
            if tr.SEVERITY_RANK.get(rec["severity"], 1) > tr.SEVERITY_RANK.get(g["severity"], 1):
                g["severity"] = rec["severity"]
            if att["kind"] == "line":
                g["kind"] = "line"
            g["stops"] = [] if g["kind"] == "line" else sorted(set(g["stops"]) | set(att["stops"]))
        groups.sort(key=lambda g: (-g["start"], g["ids"][0]))
        by_line[code] = groups
    since = state.get("tracking_since")
    return {
        "lines": dict(sorted(by_line.items())),
        "coverage": {"tracking_since": since, "complete": since is not None and since <= lo and not gaps(state, now_s, window_s),
                     "gaps": gaps(state, now_s, window_s)},
    }
