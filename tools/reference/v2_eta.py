#!/usr/bin/env python3
"""
Affichage du temps d'attente d'un passage, règle cible de la v2 (spec R-55 corrigée, décision D-16 révisée le
16 octobre 2026).

Écart avec la v1 (`v1_rules.eta_ui`, caractérisée par tests/characterization/eta_display.json) : les mentions
« À l'approche » et « À l'arrêt » disparaissent, comme tout seuil fixe propre à l'application (les 25 s du
« 0 min » clignotant et les 24 s d'« Imminent » de la première version de D-16). Un passage mesuré décompte les
minutes jusqu'à son heure de départ annoncée, puis affiche « 0 min », qui clignote : c'est IDFM qui dit ensuite s'il est parti
(heure passée dans sa réponse suivante, retrait à la fusion, `v2_realtime.merge`) ou repoussé (nouvelle heure). Sans
réponse, il est retiré 59 s après l'heure annoncée, comme un passage théorique. Le passage théorique est inchangé :
il ne clignote jamais, le véhicule n'étant pas suivi.

Bibliothèque standard uniquement ; l'application (domain/WaitTime.kt) reproduit cette fonction à l'identique et
tests/rules-v2/eta_display.json en fixe les cas, vérifiés par tools/check_fixtures.py et par WaitTimeTest.
"""

from __future__ import annotations

# Secondes après l'heure de départ pendant lesquelles un passage reste affiché (« 0 min ») : théorique, ou mesuré
# sans réponse d'IDFM qui confirme son départ ou le repousse.
RETAIN_S = 59


def _trunc_div(a: int, b: int) -> int:
    """Division entière tronquée vers zéro, comme Kotlin."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b > 0) else -q


def eta_display(ms_until: int, realtime: bool) -> dict:
    """ms_until = heure de départ − maintenant, en millisecondes ; secondes tronquées vers zéro (R-55)."""
    s = _trunc_div(ms_until, 1000)
    if s < -RETAIN_S:
        return {"kind": "hidden"}
    if s > 0:
        return {"kind": "minutes", "minutes": _trunc_div(s + 59, 60), "blink": False}
    if not realtime:
        # théorique : jamais « 0 min » avant l'heure (v1), jamais clignotant
        return {"kind": "minutes", "minutes": 1 if s == 0 else 0, "blink": False}
    return {"kind": "minutes", "minutes": 0, "blink": True}     # heure annoncée atteinte : « 0 min » clignotant
