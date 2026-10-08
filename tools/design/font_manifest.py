#!/usr/bin/env python3
"""Fiche de la police embarquée (ADR-6) : design/fonts/fonts.json.

Lit le fichier de police avec fontTools et en tire ce que l'application et les contrôles utilisent :
empreinte, nom, axe de graisse, avance des chiffres à chaque graisse utilisée, couverture des caractères.
tools/check_fixtures.py vérifie ensuite, sans fontTools, que le fichier n'a pas changé (empreinte) et que
tous les textes des données sont couverts.

    python3 tools/design/font_manifest.py --check    # compare la fiche au fichier (sortie 1 si écart)
    python3 tools/design/font_manifest.py --write    # réécrit la fiche après un changement de police

Dépendance : fontTools (pip install fonttools).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ROOT = Path(__file__).resolve().parents[2]
FONTS = ROOT / "design" / "fonts"
MANIFEST = FONTS / "fonts.json"
DIGITS = "0123456789"


def ranges(codepoints: list) -> list:
    """Plages contiguës de points de code, en hexadécimal : ["0020-007E", "00A0-017F", …]."""
    out = []
    for cp in sorted(codepoints):
        if out and cp == out[-1][1] + 1:
            out[-1][1] = cp
        else:
            out.append([cp, cp])
    return [f"{a:04X}-{b:04X}" for a, b in out]


def describe(entry: dict) -> dict:
    path = FONTS / entry["file"]
    data = path.read_bytes()
    font = TTFont(path)
    names = {r.nameID: r.toUnicode() for r in font["name"].names if r.platformID == 3 and r.langID == 0x409}
    axis = next(a for a in font["fvar"].axes if a.axisTag == "wght")
    advances = {}
    for w in entry["weights_used"]:
        inst = instancer.instantiateVariableFont(TTFont(path), {"wght": w})
        cmap, hmtx = inst.getBestCmap(), inst["hmtx"]
        advances[str(w)] = sorted({hmtx[cmap[ord(d)]][0] for d in DIGITS})
    return {
        **entry,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "family": names.get(16) or names[1],
        "version": names[5].split(";")[0],
        "copyright": names[0],
        "wght_axis": [axis.minValue, axis.maxValue],
        "digit_advances": advances,
        "coverage": ranges(list(font.getBestCmap())),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--write", action="store_true")
    args = ap.parse_args()
    doc = json.loads(MANIFEST.read_text(encoding="utf-8"))
    fresh = dict(doc, fonts=[describe(e) for e in doc["fonts"]])
    if args.write:
        MANIFEST.write_text(json.dumps(fresh, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"fiche réécrite : {MANIFEST.relative_to(ROOT)}")
        return 0
    if fresh != doc:
        print("ÉCART entre la fiche et le fichier de police : relancer avec --write après vérification")
        return 1
    print("fiche de police conforme")
    return 0


if __name__ == "__main__":
    sys.exit(main())
