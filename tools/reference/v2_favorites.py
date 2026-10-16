#!/usr/bin/env python3
"""
Favoris automatiques de l'accueil (spec R-103, décision D-8 tranchée le 16 octobre 2026), en Python exécutable de
référence. L'application (domain/Favorites.kt) reproduit ces fonctions à l'identique ; tests/rules-v2/favorites.json
en fixe les cas, vérifiés par tools/check_fixtures.py et par FavoritesTest.

L'application note les **jours** où l'on se sert vraiment d'un trajet ou d'une ligne, sur l'appareil seulement :
- un **trajet** est un couple « Vous êtes à » → « Vous allez à » (lieu de départ, destination ou toutes
  destinations), noté quand l'accueil l'affiche 20 s de suite au premier plan ;
- une **ligne** est notée quand sa fiche reste affichée 10 s de suite au premier plan.

Un trajet ou une ligne devient favori dès qu'il a servi 3 jours différents sur les 28 derniers (une habitude, même
hebdomadaire) ; il cesse de l'être s'il ne sert plus. Rien n'est jamais envoyé : ni les jours, ni les favoris.

    usage = {"trips": [{"place", "destination", "days": [jours]}], "lines": [{"code", "days"}], "hidden": [clés]}

Un jour est un numéro de jour (jours depuis le 1er janvier 1970), à l'heure de Paris. Bibliothèque standard seule.
"""

from __future__ import annotations

WINDOW_DAYS = 28          # fenêtre glissante : 4 semaines, pour saisir une habitude hebdomadaire
MIN_DAYS = 3              # jours d'usage différents pour devenir un favori
MAX_SHOWN = 5             # favoris affichés au plus
TRIP_DWELL_S = 20         # un trajet compte quand l'accueil l'affiche 20 s de suite au premier plan
LINE_DWELL_S = 10         # une ligne compte quand sa fiche reste affichée 10 s de suite au premier plan


def empty() -> dict:
    return {"trips": [], "lines": [], "hidden": []}


def trip_key(place: str, destination: str | None) -> str:
    """Clé d'un trajet : « trip:<lieu>|<destination> », destination vide pour « toutes destinations »."""
    return f"trip:{place}|{destination or ''}"


def line_key(code: str) -> str:
    return f"line:{code}"


def _key(kind: str, item: dict) -> str:
    return trip_key(item["place"], item["destination"]) if kind == "trip" else line_key(item["code"])


def _in_window(days: list, today: int) -> list:
    """Jours de la fenêtre glissante : les 28 derniers, aujourd'hui compris."""
    return sorted({d for d in days if today - WINDOW_DAYS < d <= today})


def _forget_old(days: list, today: int) -> list:
    """Jours gardés : ceux qui ne sont pas sortis de la fenêtre. Un jour postérieur à `today` (horloge reculée)
    est gardé, mais ne compte qu'une fois revenu dans la fenêtre."""
    return sorted({d for d in days if d > today - WINDOW_DAYS})


def record_trip(usage: dict, place: str, destination: str | None, today: int) -> dict:
    """Note l'usage d'un trajet ce jour-là (une fois par jour). Un trajet écarté (« Ne plus proposer ») n'est plus
    noté. Les jours hors de la fenêtre sont oubliés, et un élément sans jour disparaît."""
    if trip_key(place, destination) in usage["hidden"]:
        return _prune_all(usage, today)
    hit = next((t for t in usage["trips"] if t["place"] == place and t["destination"] == destination), None)
    if hit is None:
        usage["trips"].append({"place": place, "destination": destination, "days": [today]})
    elif today not in hit["days"]:
        hit["days"].append(today)
    return _prune_all(usage, today)


def record_line(usage: dict, code: str, today: int) -> dict:
    """Note l'usage d'une ligne ce jour-là (une fois par jour), comme `record_trip`."""
    if line_key(code) in usage["hidden"]:
        return _prune_all(usage, today)
    hit = next((l for l in usage["lines"] if l["code"] == code), None)
    if hit is None:
        usage["lines"].append({"code": code, "days": [today]})
    elif today not in hit["days"]:
        hit["days"].append(today)
    return _prune_all(usage, today)


def hide(usage: dict, key: str) -> dict:
    """« Ne plus proposer » : la clé est écartée pour de bon et ses jours d'usage sont effacés."""
    if key not in usage["hidden"]:
        usage["hidden"] = sorted(usage["hidden"] + [key])
    usage["trips"] = [t for t in usage["trips"] if _key("trip", t) != key]
    usage["lines"] = [l for l in usage["lines"] if _key("line", l) != key]
    return usage


def _prune_all(usage: dict, today: int) -> dict:
    for kind in ("trips", "lines"):
        kept = []
        for item in usage[kind]:
            item["days"] = _forget_old(item["days"], today)
            if item["days"]:
                kept.append(item)
        usage[kind] = sorted(kept, key=lambda x: _key(kind[:-1], x))
    return usage


def favorites(usage: dict, today: int, current: tuple | None, valid) -> list:
    """
    Favoris à afficher : trajets et lignes qui ont servi au moins 3 jours différents sur les 28 derniers (aujourd'hui
    compris), sauf ceux écartés, le trajet affiché en ce moment (`current` : (lieu, destination)) et ceux que la base
    ne connaît plus (`valid(kind, item)` faux : lieu, destination ou ligne disparus). Ordre : plus de jours d'abord,
    puis usage le plus récent, puis les trajets avant les lignes, puis la clé ; 5 au plus. Rend
    `[{"kind", "key", "days", "last", ...élément}]`.
    """
    out = []
    for kind, items in (("trip", usage["trips"]), ("line", usage["lines"])):
        for item in items:
            key = _key(kind, item)
            days = _in_window(item["days"], today)
            if len(days) < MIN_DAYS or key in usage["hidden"]:
                continue
            if kind == "trip" and current is not None and (item["place"], item["destination"]) == tuple(current):
                continue
            if not valid(kind, item):
                continue
            entry = {"kind": kind, "key": key, "days": len(days), "last": days[-1]}
            entry.update({k: v for k, v in item.items() if k != "days"})
            out.append(entry)
    out.sort(key=lambda e: (-e["days"], -e["last"], 0 if e["kind"] == "trip" else 1, e["key"]))
    return out[:MAX_SHOWN]


# -- cas de règle (tests/rules-v2/favorites.json) ------------------------------------------------------------
def run_case(case: dict) -> object:
    """Joue un cas : suite d'opérations sur un usage, puis les favoris ou l'usage obtenus."""
    import copy
    usage = copy.deepcopy(case.get("usage") or empty())
    for op in case.get("ops", []):
        if op[0] == "trip":
            record_trip(usage, op[1], op[2], op[3])
        elif op[0] == "line":
            record_line(usage, op[1], op[2])
        elif op[0] == "hide":
            hide(usage, op[1])
    if "favorites" in case:
        q = case["favorites"]
        invalid = set(q.get("invalid", []))
        got = favorites(usage, q["today"], q.get("current"), lambda kind, item: _key(kind, item) not in invalid)
        return [[e["key"], e["days"], e["last"]] for e in got]
    return usage
