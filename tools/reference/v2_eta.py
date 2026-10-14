#!/usr/bin/env python3
"""
Affichage du temps d'attente d'un passage, règle cible de la v2 (spec R-55 corrigée, décision D-16).

Écart avec la v1 (`v1_rules.eta_ui`, caractérisée par tests/characterization/eta_display.json) : les mentions
« À l'approche » et « À l'arrêt » disparaissent. Un passage mesuré à 25 s ou moins de son départ affiche « 0 min »
qui clignote ; de son heure de départ jusqu'à 24 s après, « Imminent ». Le passage théorique est inchangé : il n'est
jamais « Imminent » et ne clignote jamais, le véhicule n'étant pas suivi.

Bibliothèque standard uniquement ; l'application (domain/WaitTime.kt) reproduit cette fonction à l'identique et
tests/rules-v2/eta_display.json en fixe les cas, vérifiés par tools/check_fixtures.py et par WaitTimeTest.
"""

from __future__ import annotations

# Secondes avant le départ en deçà desquelles un passage mesuré affiche « 0 min » clignotant.
BLINK_FROM_S = 25
# Secondes après l'heure de départ pendant lesquelles un passage mesuré reste « Imminent ».
IMMINENT_UNTIL_S = 24
# Secondes après l'heure de départ pendant lesquelles un passage théorique reste affiché (« 0 min »).
PLANNED_RETAIN_S = 59


def _trunc_div(a: int, b: int) -> int:
    """Division entière tronquée vers zéro, comme Kotlin."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b > 0) else -q


def eta_display(ms_until: int, realtime: bool) -> dict:
    """ms_until = heure de départ − maintenant, en millisecondes ; secondes tronquées vers zéro (R-55)."""
    s = _trunc_div(ms_until, 1000)
    if not realtime:
        if s >= 0:
            return {"kind": "minutes", "minutes": max(1, _trunc_div(s + 59, 60)), "blink": False}
        if s >= -PLANNED_RETAIN_S:
            return {"kind": "minutes", "minutes": 0, "blink": False}
        return {"kind": "hidden"}
    if s > BLINK_FROM_S:
        return {"kind": "minutes", "minutes": _trunc_div(s + 59, 60), "blink": False}
    if s > 0:
        return {"kind": "minutes", "minutes": 0, "blink": True}
    if s >= -IMMINENT_UNTIL_S:
        return {"kind": "imminent"}
    return {"kind": "hidden"}
