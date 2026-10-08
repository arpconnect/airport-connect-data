#!/usr/bin/env python3
"""Génère les ressources Android du design system Roissy 72 depuis design/ (source unique).

Produit, sans aucune recopie à la main :
  * app/src/main/java/com/airportconnect/app/ui/theme/Roissy72Tokens.kt  (couleurs, typographie, espaces…)
  * app/src/main/res/values/colors_roissy72.xml et values-night/colors_roissy72.xml (thèmes XML, écran de lancement)
  * app/src/main/res/drawable/ic_<nom>.xml        (pictogrammes, depuis design/icons/*.svg)
  * app/src/main/res/font/source_sans_3.ttf       (copie octet pour octet de design/fonts, ADR-6)

    python3 tools/design/gen_android.py           # écrit les fichiers
    python3 tools/design/gen_android.py --check   # vérifie qu'ils sont à jour (sortie 1 sinon)

Bibliothèque standard uniquement.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESIGN = ROOT / "design"
APP = ROOT / "app" / "src" / "main"
KT = APP / "java" / "com" / "airportconnect" / "app" / "ui" / "theme" / "Roissy72Tokens.kt"
SVG_NS = "{http://www.w3.org/2000/svg}"
HEADER = "Généré par tools/design/gen_android.py depuis design/ — ne pas modifier à la main."


def camel(name: str) -> str:
    parts = name.split("-")
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


def argb(hex_color: str, alpha: int = 0xFF) -> str:
    h = hex_color.lstrip("#").upper()
    if len(h) != 6:
        raise ValueError(f"couleur attendue en #RRGGBB : {hex_color}")
    return f"0x{alpha:02X}{h}"


def px(v: str) -> str:
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)px", v)
    if not m:
        raise ValueError(f"valeur en px attendue : {v}")
    return m.group(1)


def num(v: str) -> str:
    return v[:-2] if v.endswith(".0") else v


def shadow(value: str) -> tuple:
    """« 0 1px 0 rgba(20,20,20,0.12) » ou « 0 0 0 1px #2B2A27 » → (décalage y, épaisseur, couleur ARGB)."""
    m = re.fullmatch(r"0 (\d+)px 0 rgba\((\d+),(\d+),(\d+),([\d.]+)\)", value)
    if m:
        r, g, b, a = int(m.group(2)), int(m.group(3)), int(m.group(4)), float(m.group(5))
        return m.group(1), "0", f"0x{round(a * 255):02X}{r:02X}{g:02X}{b:02X}"
    m = re.fullmatch(r"0 0 0 (\d+)px (#[0-9A-Fa-f]{6})", value)
    if m:
        return "0", m.group(1), argb(m.group(2))
    raise ValueError(f"ombre non reconnue : {value}")


def kotlin_tokens(tokens: dict) -> str:
    colors = tokens["color"]["tokens"]
    themes = [t["id"] for t in tokens["color"]["themes"]]
    assert themes == ["light", "dark"], themes
    out = [f"// {HEADER}", "package com.airportconnect.app.ui.theme", "",
           "import androidx.compose.runtime.Immutable",
           "import androidx.compose.ui.graphics.Color",
           "import androidx.compose.ui.text.TextStyle",
           "import androidx.compose.ui.text.font.FontFamily",
           "import androidx.compose.ui.text.font.FontWeight",
           "import androidx.compose.ui.unit.Dp",
           "import androidx.compose.ui.unit.dp",
           "import androidx.compose.ui.unit.em",
           "import androidx.compose.ui.unit.sp", ""]
    # Couleurs
    out.append("/** Rôles de couleur Roissy 72 (design/tokens.json), un jeu par thème. */")
    out.append("@Immutable")
    out.append("data class Roissy72Colors(")
    for c in colors:
        out.append(f"    /** {c['usage']} */")
        out.append(f"    val {camel(c['name'])}: Color,")
    out.append(")")
    out.append("")
    for th in themes:
        out.append(f"internal val {th.capitalize()}Roissy72Colors = Roissy72Colors(")
        for c in colors:
            out.append(f"    {camel(c['name'])} = Color({argb(c['value'][th])}),")
        out.append(")")
        out.append("")
    # Ombre de carte
    sh = {t["name"]: t for t in tokens["shadow"]["tokens"]}["shadow-card"]
    out.append("/** Séparation d'une carte : un filet décalé (clair) ou un liseré (sombre), jamais d'ombre diffuse. */")
    out.append("@Immutable")
    out.append("data class CardEdge(val offsetY: Dp, val ring: Dp, val color: Color)")
    out.append("")
    for th in themes:
        oy, ring, col = shadow(sh["value"][th])
        out.append(f"internal val {th.capitalize()}CardEdge = CardEdge(offsetY = {oy}.dp, ring = {ring}.dp, color = Color({col}))")
    out.append("")
    # Dimensions
    for fam, obj in (("spacing", "Roissy72Spacing"), ("radius", "Roissy72Radius"), ("size", "Roissy72Size"), ("border", "Roissy72Border")):
        out.append(f"/** Tokens « {fam} » de design/tokens.json. */")
        out.append(f"object {obj} {{")
        for t in tokens[fam]["tokens"]:
            out.append(f"    /** {t['usage']} */")
            out.append(f"    val {camel(t['name'])}: Dp = {px(t['value'])}.dp")
        out.append("}")
        out.append("")
    # Typographie
    styles = [s for g in tokens["type"]["groups"] for s in g["styles"]]
    out.append("/** Styles de texte Roissy 72 (design/tokens.json), tous en Source Sans 3. */")
    out.append("@Immutable")
    out.append("data class Roissy72Typography(")
    for s in styles:
        out.append(f"    /** {s['usage']} */")
        out.append(f"    val {camel(s['name'])}: TextStyle,")
    out.append(")")
    out.append("")
    out.append("internal fun roissy72Typography(family: FontFamily) = Roissy72Typography(")
    for s in styles:
        args = [f"fontFamily = family", f"fontSize = {px(s['fontSize'])}.sp", f"lineHeight = {px(s['lineHeight'])}.sp",
                f"fontWeight = FontWeight({s['fontWeight']})"]
        if "letterSpacing" in s:
            m = re.fullmatch(r"(\d+(?:\.\d+)?)em", s["letterSpacing"])
            if not m:
                raise ValueError(f"letterSpacing attendu en em : {s['letterSpacing']}")
            args.append(f"letterSpacing = {m.group(1)}.em")
        out.append(f"    {camel(s['name'])} = TextStyle({', '.join(args)}),")
    out.append(")")
    out.append("")
    weights = sorted({s["fontWeight"] for s in styles})
    out.append(f"/** Graisses utilisées par les styles, à déclarer sur l'axe wght de la police (ADR-6). */")
    out.append(f"internal val Roissy72FontWeights = listOf({', '.join(str(w) for w in weights)})")
    return "\n".join(out) + "\n"


def colors_xml(tokens: dict, theme: str) -> str:
    wanted = ["paper", "signal"]  # utilisées par les thèmes XML (fenêtre, écran de lancement)
    vals = {c["name"]: c["value"][theme] for c in tokens["color"]["tokens"]}
    lines = ['<?xml version="1.0" encoding="utf-8"?>', f"<!-- {HEADER} -->", "<resources>"]
    for n in wanted:
        lines.append(f'    <color name="r72_{n.replace("-", "_")}">{vals[n].upper()}</color>')
    lines.append("</resources>")
    return "\n".join(lines) + "\n"


def _f(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return s if s != "-0" else "0"


def element_path(el: ET.Element) -> str:
    tag = el.tag.replace(SVG_NS, "")
    a = el.attrib
    if tag == "path":
        return a["d"]
    if tag == "circle":
        cx, cy, r = float(a["cx"]), float(a["cy"]), float(a["r"])
        return (f"M{_f(cx - r)},{_f(cy)}a{_f(r)},{_f(r)} 0 1,0 {_f(2 * r)},0"
                f"a{_f(r)},{_f(r)} 0 1,0 {_f(-2 * r)},0z")
    if tag == "rect":
        x, y, w, h = (float(a[k]) for k in ("x", "y", "width", "height"))
        rx = float(a.get("rx", a.get("ry", 0)))
        if rx == 0:
            return f"M{_f(x)},{_f(y)}h{_f(w)}v{_f(h)}h{_f(-w)}z"
        return (f"M{_f(x + rx)},{_f(y)}h{_f(w - 2 * rx)}a{_f(rx)},{_f(rx)} 0 0,1 {_f(rx)},{_f(rx)}"
                f"v{_f(h - 2 * rx)}a{_f(rx)},{_f(rx)} 0 0,1 {_f(-rx)},{_f(rx)}"
                f"h{_f(-(w - 2 * rx))}a{_f(rx)},{_f(rx)} 0 0,1 {_f(-rx)},{_f(-rx)}"
                f"v{_f(-(h - 2 * rx))}a{_f(rx)},{_f(rx)} 0 0,1 {_f(rx)},{_f(-rx)}z")
    raise ValueError(f"élément SVG non pris en charge : {tag}")


def vector_drawable(svg_path: Path) -> str:
    root = ET.parse(svg_path).getroot()
    vb = root.attrib["viewBox"].split()
    if vb != ["0", "0", "24", "24"]:
        raise ValueError(f"{svg_path.name} : viewBox 0 0 24 24 attendu")
    stroke, width = root.attrib["stroke"], root.attrib["stroke-width"]
    cap, join = root.attrib["stroke-linecap"], root.attrib["stroke-linejoin"]
    lines = ['<?xml version="1.0" encoding="utf-8"?>', f"<!-- {HEADER} Source : design/icons/{svg_path.name} -->",
             '<vector xmlns:android="http://schemas.android.com/apk/res/android"',
             '    android:width="24dp"', '    android:height="24dp"',
             '    android:viewportWidth="24"', '    android:viewportHeight="24">']
    for el in root:
        lines.append("    <path")
        lines.append(f'        android:pathData="{element_path(el)}"')
        lines.append('        android:fillColor="#00000000"')
        lines.append(f'        android:strokeColor="{argb(stroke).replace("0x", "#")}"')
        lines.append(f'        android:strokeWidth="{width}"')
        lines.append(f'        android:strokeLineCap="{cap}"')
        lines.append(f'        android:strokeLineJoin="{join}" />')
    lines.append("</vector>")
    return "\n".join(lines) + "\n"


def outputs() -> dict:
    tokens = json.loads((DESIGN / "tokens.json").read_text(encoding="utf-8"))
    files = {
        KT: kotlin_tokens(tokens).encode("utf-8"),
        APP / "res" / "values" / "colors_roissy72.xml": colors_xml(tokens, "light").encode("utf-8"),
        APP / "res" / "values-night" / "colors_roissy72.xml": colors_xml(tokens, "dark").encode("utf-8"),
    }
    for svg in sorted((DESIGN / "icons").glob("*.svg")):
        name = "ic_" + svg.stem.replace("-", "_")
        files[APP / "res" / "drawable" / f"{name}.xml"] = vector_drawable(svg).encode("utf-8")
    fonts = json.loads((DESIGN / "fonts" / "fonts.json").read_text(encoding="utf-8"))["fonts"]
    for f in fonts:
        files[APP / f["android_resource"]] = (DESIGN / "fonts" / f["file"]).read_bytes()
    return files


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    files = outputs()
    stale = [p for p, data in files.items() if not p.exists() or p.read_bytes() != data]
    # Pictogrammes générés qui n'ont plus de source
    expected = {p.name for p in files if p.parent.name == "drawable"}
    orphans = [p for p in (APP / "res" / "drawable").glob("ic_*.xml")
               if p.name not in expected and HEADER in p.read_text(encoding="utf-8")]
    if args.check:
        for p in stale + orphans:
            print(f"À RÉGÉNÉRER : {p.relative_to(ROOT)}")
        if stale or orphans:
            print("lancer : python3 tools/design/gen_android.py")
            return 1
        print(f"ressources Android à jour ({len(files)} fichiers)")
        return 0
    for p in stale:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(files[p])
    for p in orphans:
        p.unlink()
    print(f"{len(stale)} fichier(s) écrit(s), {len(orphans)} supprimé(s), {len(files)} au total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
