#!/usr/bin/env python3
"""
Vérifie que chaque cas de tests/characterization/*.json correspond au comportement réel
de la v1, via le port de référence tools/reference/v1_rules.py.

Usage : python3 tools/check_fixtures.py        (code de sortie 1 si un cas échoue)

Les cas « terrain » (gtfs-2026-06/) se vérifient à part, avec le GTFS d'origine :
    python3 tools/extract_gtfs_cases.py IDFM-gtfs.zip --check tests/characterization/gtfs-2026-06
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "reference"))
import v1_rules as r  # noqa: E402

FIX = ROOT / "tests" / "characterization"
failures: list = []
passed = 0


def check(file: str, case_id: str, got, expected) -> None:
    global passed
    if got == expected:
        passed += 1
    else:
        failures.append(f"{file} :: {case_id} → obtenu {got!r}, attendu {expected!r}")


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def eta() -> None:
    doc = load("eta_display.json")
    for c in doc["cases"]:
        i = c["input"]
        sec = None if i["ms_until"] is None else r.seconds_between_millis(i["ms_until"], 0)
        got = r.eta_ui(sec, i["realtime"], i.get("eta_min_fallback")).as_dict()
        check("eta_display.json", c["id"], got, c["expected"])


def _expand(pattern: dict) -> list:
    sd = date.fromisoformat(pattern["service_date"])
    fh, fm = (int(x) for x in pattern["first"].split(":"))
    lh, lm = (int(x) for x in pattern["last"].split(":"))
    cur, end = fh * 60 + fm, lh * 60 + lm
    out = []
    while cur <= end:
        out.append((sd, r.gtfs_time_to_epoch_ms(sd, f"{cur // 60:02d}:{cur % 60:02d}:00")))
        cur += pattern["headway_min"]
    return out


def _local(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, r.PARIS).strftime("%Y-%m-%dT%H:%M")


def boundary() -> None:
    doc = load("service_boundary.json")
    for c in doc["cases"]:
        events = [e for p in doc["fixtures"][c["fixture"]] for e in _expand(p)]
        now = int(datetime.fromisoformat(c["now"]).replace(tzinfo=r.PARIS).timestamp() * 1000)
        res = r.station_snapshot(events, now)
        got = {"visible": [_local(ms) for ms in res.visible_epochs_ms], "boundary": res.boundary}
        check("service_boundary.json", c["id"], got, c["expected"])


def scope() -> None:
    doc = load("direction_scope.json")
    for c in doc["supported_cases"]:
        check("direction_scope.json", c["id"], r.direction_supported(c["line"], c["direction"]), c["expected"])
    for c in doc["compatible_cases"]:
        got = r.direction_compatible_with_selection(c["line"], c["direction"], c["keywords"], c["match_score"])
        check("direction_scope.json", c["id"], got, c["expected"])


def traffic() -> None:
    doc = load("traffic_axis_filter.json")
    for c in doc["cases"]:
        got = r.keep_traffic_incident(c["line_names"], c["stops"], c["summary"], c.get("details", ""))
        check("traffic_axis_filter.json", c["id"], got, c["expected"])


def freshness() -> None:
    doc = load("realtime_freshness.json")
    for c in doc["cases"]:
        check("realtime_freshness.json", c["id"] + " [v1]", r.infer_realtime_v1(c["requested"], c["freshness"]), c["expected_v1"])
        check("realtime_freshness.json", c["id"] + " [strict]", r.infer_realtime_strict(c["freshness"]), c["expected_strict"])


def identity() -> None:
    doc = load("line_identity.json")
    for c in doc["v1_name_match_cases"]:
        got = r.v1_name_match(c["candidates"], c["route_short_name"], c["route_long_name"])
        check("line_identity.json", c["id"], got, c["expected_v1"])
    lines = {l["id"]: l for l in json.loads((ROOT / "data" / "lines.json").read_text(encoding="utf-8"))["lines"]}
    for c in doc["realtime_line_match_cases"]:
        expected_id = lines[c["line"]]["navitia_line_id"]
        got = expected_id is not None and expected_id == c["payload_line_id"]
        check("line_identity.json", c["id"], got, c["expected"])


def lines_registry() -> None:
    doc = json.loads((ROOT / "data" / "lines.json").read_text(encoding="utf-8"))
    ids = [l["id"] for l in doc["lines"]]
    codes = [l["idfm_route_id"] for l in doc["lines"] if l["idfm_route_id"]]
    check("lines.json", "identifiants uniques", len(ids) == len(set(ids)), True)
    check("lines.json", "codes C uniques", len(codes) == len(set(codes)), True)
    for l in doc["lines"]:
        coherent = (l["state"] == "future") == (l["idfm_route_id"] is None)
        check("lines.json", f"{l['id']} état/code cohérents", coherent, True)
        if l["idfm_route_id"]:
            check("lines.json", f"{l['id']} navitia", l["navitia_line_id"], "line:" + l["idfm_route_id"])
    hub_members = [m for members in doc["groups"]["NOCTILIEN"]["hubs"].values() for m in members]
    for m in hub_members:
        check("lines.json", f"pôle Noctilien {m} présent", m in ids, True)


def realtime_v2() -> None:
    import v2_realtime as v2
    doc = json.loads((ROOT / "tests" / "rules-v2" / "realtime_merge.json").read_text(encoding="utf-8"))
    ctx = doc["context"]
    for c in doc["merge_cases"]:
        res = v2.merge_board(ctx["theoretical"], c["response"], ctx["route_id"], ctx["stop_id"], c.get("now", ctx["now"]))
        got = {"states": [p["state"] for p in res.passages], "epochs": [p["epoch"] for p in res.passages],
               "reason": res.reason, "rejected": res.rejected, "realtime_used": res.realtime_used}
        check("rules-v2/realtime_merge.json", c["id"], got, c["expected"])
    for c in doc["breaker_cases"]:
        br = v2.CircuitBreaker()
        for t, ok, retry in c["events"]:
            br.record(t, ok, retry)
        check("rules-v2/realtime_merge.json", c["id"], [[t, br.allow(t)] for t, _ in c["probes"]], c["probes"])


def data_files() -> None:
    places = json.loads((ROOT / "data" / "places.json").read_text(encoding="utf-8"))["places"]
    ids = [p["id"] for p in places]
    check("places.json", "identifiants uniques", len(ids) == len(set(ids)), True)
    lines = {l["id"]: l for l in json.loads((ROOT / "data" / "lines.json").read_text(encoding="utf-8"))["lines"]}
    sectors = ("ROISSY", "ORLY", "BOURGET", "BEAUVAIS")
    for p in places:
        check("places.json", f"{p['id']} parent connu", p.get("parent") in (None, *ids), True)
        kind = p.get("kind")
        check("places.json", f"{p['id']} sorte de lieu", kind in ("airport", "city"), True)
        if kind == "airport":
            check("places.json", f"{p['id']} secteur", p.get("sector") in sectors, True)
            check("places.json", f"{p['id']} au moins un arrêt", bool(p.get("stop_areas") or p.get("stop_points")), True)
            check("places.json", f"{p['id']} sans point de montée", "boarding" in p, False)
        elif kind == "city":
            # R-100, D-13 : un point de montée par ligne active, zone d'arrêt GTFS et nom attendu
            check("places.json", f"{p['id']} ville sans secteur, arrêts ni parent",
                  [k for k in ("sector", "stop_areas", "stop_points", "parent") if k in p], [])
            boarding = p.get("boarding") or []
            check("places.json", f"{p['id']} points de montée", bool(boarding), True)
            codes = [b.get("line") for b in boarding]
            check("places.json", f"{p['id']} une ligne au plus une fois", len(codes) == len(set(codes)), True)
            for b in boarding:
                ln = lines.get(b.get("line"), {})
                check("places.json", f"{p['id']} {b.get('line')} ligne active", ln.get("state"), "active")
                check("places.json", f"{p['id']} {b.get('line')} zone d'arrêt et nom",
                      str(b.get("stop_area", "")).startswith("IDFM:") and bool(b.get("name")), True)
    check("places.json", "un seul lieu ville, en tête (lieu par défaut de l'accueil)",
          [p["id"] for p in places if p.get("kind") == "city"] == [places[0]["id"]], True)
    terminal_names(json.loads((ROOT / "data" / "places.json").read_text(encoding="utf-8")).get("terminal_names"), places, ids)
    adjustments(ids_lines=lines)
    plans_sources(lines)
    design_tokens(lines)
    embedded_font()
    android_resources()


def plans_sources(lines: dict) -> None:
    """Contrôle statique de data/plans-sources.json (R-180 à R-186), sans réseau."""
    doc = json.loads((ROOT / "data" / "plans-sources.json").read_text(encoding="utf-8"))
    check("plans-sources.json", "schéma", doc["schema"], "airport-connect/plans-sources@1")
    for key in ("line_documents", "network_plans"):
        ds = doc["datasets"][key]
        check("plans-sources.json", f"jeu {key} complet", all(ds.get(k) for k in ("portal", "dataset_id", "licence")), True)
    for lid, fb in doc["fallbacks"].items():
        check("plans-sources.json", f"repli {lid} : ligne active", lines.get(lid, {}).get("state"), "active")
        path = fb["url"].split("?")[0].lower()
        check("plans-sources.json", f"repli {lid} : lien direct vers un fichier en https",
              fb["url"].startswith("https://") and path.endswith((".pdf", ".jpg", ".jpeg", ".png", ".webp")), True)
        check("plans-sources.json", f"repli {lid} : motif et éditeur", bool(fb.get("reason") and fb.get("provider")), True)
    for lid, np in doc["no_plan"].items():
        check("plans-sources.json", f"sans plan {lid} : ligne active", lines.get(lid, {}).get("state"), "active")
        check("plans-sources.json", f"sans plan {lid} : motif", bool(np.get("reason")), True)
        check("plans-sources.json", f"sans plan {lid} : pas de repli contradictoire", lid in doc["fallbacks"], False)
    groups = {l.get("group") for l in lines.values() if l.get("group")}
    for pid, spec in doc["network_plans"].items():
        check("plans-sources.json", f"plan de réseau {pid} : groupe de lignes connu", spec["lines_group"] in groups, True)
    lim = doc["limits"]
    check("plans-sources.json", "limites cohérentes",
          lim["max_bytes"] > 0 and lim["old_document_days"] > 0 and 0 < lim["max_failure_ratio"] < 1, True)


def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def badge_text_color(bg: str, official: str, minimum: float) -> str:
    """Règle des pastilles de lignes (design/contrast.json) : texte officiel s'il suffit, sinon noir ou blanc."""
    if contrast(bg, official) >= minimum:
        return official
    return "#000000" if contrast(bg, "#000000") >= contrast(bg, "#FFFFFF") else "#FFFFFF"


def design_tokens(lines: dict) -> None:
    """Design system Roissy 72 : chaque couple de couleurs déclaré atteint son contraste dans chaque thème."""
    tokens = json.loads((ROOT / "design" / "tokens.json").read_text(encoding="utf-8"))
    rules = json.loads((ROOT / "design" / "contrast.json").read_text(encoding="utf-8"))
    themes = [t["id"] for t in tokens["color"]["themes"]]
    colors = {t["name"]: t["value"] for t in tokens["color"]["tokens"]}
    names = [t["name"] for fam in ("color", "spacing", "radius", "size", "border", "shadow") for t in tokens[fam]["tokens"]]
    check("design/tokens.json", "noms uniques", len(names) == len(set(names)), True)
    for name, value in colors.items():
        check("design/tokens.json", f"{name} défini dans chaque thème", sorted(value) == sorted(themes), True)
    for p in rules["pairs"]:
        for th in themes:
            r = contrast(colors[p["fg"]][th], colors[p["bg"]][th])
            check("design/contrast.json", f"{p['fg']} sur {p['bg']} ({th}) ≥ {p['min']}", r >= p["min"], True)
    for st in (s for g in tokens["type"]["groups"] for s in g["styles"]):
        check("design/tokens.json", f"style {st['name']} ≥ 14 px", int(st["fontSize"].rstrip("px")) >= 14, True)
    badge = next(s for g in tokens["type"]["groups"] for s in g["styles"] if s["name"] == "badge")
    check("design/tokens.json", "pastille en gras de 19 px minimum", (int(badge["fontSize"].rstrip("px")) >= 19, badge["fontWeight"] >= 700), (True, True))
    m = rules["line_badges"]["min"]
    for l in lines.values():
        if l["state"] != "active":
            continue
        bg = l["gtfs"]["color"]
        fg = badge_text_color(bg, l["gtfs"]["text_color"], m)
        check("design/contrast.json", f"pastille {l['id']} ≥ {m}:1", contrast(bg, fg) >= m, True)
    icons = sorted(p.stem for p in (ROOT / "design" / "icons").glob("*.svg"))
    check("design/icons", "jeu de pictogrammes complet", icons, sorted([
        "aeroport", "alerte", "bus", "changer", "departs", "direction", "favori", "fermer", "incomplet", "info",
        "lignes", "marche", "metro", "navette", "non-desservi", "nuit", "plan", "position", "prevu", "retour",
        "temps-reel", "trafic", "train", "tram"]))


def _iso_required(v) -> bool:
    return v is not None and _iso_or_none(v)


def terminal_names(meta, places: list, ids: list) -> None:
    """R-35 et D-11 : noms des terminaux de CDG appliqués dès la première version, sans collision de libellés."""
    check("places.json", "noms des terminaux : bloc présent", isinstance(meta, dict), True)
    if not isinstance(meta, dict):
        return
    check("places.json", "noms des terminaux : date d'effet ISO", _iso_required(meta.get("effective")), True)
    check("places.json", "noms des terminaux : date de vérification ISO", _iso_required(meta.get("checked_on")), True)
    check("places.json", "noms des terminaux : sources https",
          bool(meta.get("sources")) and all(u.startswith("https://") for u in meta["sources"]), True)
    for pid in meta.get("descriptive_labels") or {}:
        check("places.json", f"libellé descriptif {pid} : lieu connu", pid in ids, True)
    for p in places:
        check("places.json", f"{p['id']} ancien nom en français et en anglais, ou aucun",
              bool(p.get("former_fr")) == bool(p.get("former_en")), True)
    for key in ("label_fr", "label_en", "short_label"):
        values = [p[key] for p in places]
        dup = sorted({v for v in values if values.count(v) > 1})
        check("places.json", f"{key} uniques", dup, [])


def embedded_font() -> None:
    """ADR-6 : police embarquée inchangée (empreinte), licence livrée, graisses et caractères couverts."""
    doc = json.loads((ROOT / "design" / "fonts" / "fonts.json").read_text(encoding="utf-8"))
    tokens = json.loads((ROOT / "design" / "tokens.json").read_text(encoding="utf-8"))
    weights = sorted({st["fontWeight"] for g in tokens["type"]["groups"] for st in g["styles"]})
    check("design/fonts", "une seule police", len(doc["fonts"]), 1)
    f = doc["fonts"][0]
    data = (ROOT / "design" / "fonts" / f["file"]).read_bytes()
    check("design/fonts", f"{f['file']} : taille de la fiche", len(data), f["bytes"])
    check("design/fonts", f"{f['file']} : empreinte de la fiche (fichier non modifié)", hashlib.sha256(data).hexdigest(), f["sha256"])
    lic = (ROOT / "design" / "fonts" / f["license_file"]).read_text(encoding="utf-8")
    check("design/fonts", "licence OFL 1.1 livrée avec son nom réservé",
          "SIL Open Font License, Version 1.1" in lic and f"Reserved Font Name '{f['reserved_font_name']}'" in lic, True)
    check("design/tokens.json", "famille des tokens = police embarquée",
          tokens["type"]["families"]["sans"].startswith(f'"{f["family"]}"'), True)
    check("design/fonts", "graisses des tokens = graisses déclarées", weights, f["weights_used"])
    lo, hi = f["wght_axis"]
    check("design/fonts", "graisses dans l'axe de la police", all(lo <= w <= hi for w in weights), True)
    for w in f["weights_used"]:
        check("design/fonts", f"chiffres à chasse fixe en {w}", len(f["digit_advances"][str(w)]), 1)
    cover = [tuple(int(x, 16) for x in r.split("-")) for r in f["coverage"]]
    # Textes affichés par l'application et issus des données (les notes et titres de sources ne s'affichent pas).
    places = json.loads((ROOT / "data" / "places.json").read_text(encoding="utf-8"))["places"]
    lines_doc = json.loads((ROOT / "data" / "lines.json").read_text(encoding="utf-8"))["lines"]
    adj = json.loads((ROOT / "data" / "adjustments.json").read_text(encoding="utf-8"))["adjustments"]
    shown = [p.get(k) for p in places for k in ("label_fr", "label_en", "short_label", "former_fr", "former_en")]
    shown += [l.get("display_name") for l in lines_doc]
    shown += [(l.get("gtfs") or {}).get(k) for l in lines_doc for k in ("short_name", "long_name")]
    shown += [(l.get("future") or {}).get("scope") for l in lines_doc]
    shown += [a.get(k) for a in adj for k in ("title_fr", "summary_fr")]
    shown += [s_.get(k) for a in adj for s_ in a.get("not_served", []) for k in ("direction_label", "replacement_label")]
    shown += [st.get("sample") for g in tokens["type"]["groups"] for st in g["styles"]]
    shown = [t for t in shown if t]
    check("design/fonts", "textes affichés recensés", len(shown) > 100, True)
    missing = sorted({c for t in shown for c in t if not any(a <= ord(c) <= b for a, b in cover)})
    check("design/fonts", "tous les caractères des données couverts par la police", missing, [])


def android_resources() -> None:
    """Les ressources Android générées depuis design/ (thème Compose, couleurs, pictogrammes, police) sont à jour.

    Sans dossier app/ (dépôt public airport-connect-data, ADR-8), il n'y a rien à vérifier."""
    if not (ROOT / "app").is_dir():
        return
    import importlib.util
    spec = importlib.util.spec_from_file_location("gen_android", ROOT / "tools" / "design" / "gen_android.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    files = gen.outputs()
    for path, data in sorted(files.items()):
        rel = path.relative_to(gen.ROOT).as_posix()
        check("app (généré)", rel, path.exists() and path.read_bytes() == data, True)
    app_timetable()


def _load_tool(name: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / "app" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def app_timetable() -> None:
    """Lot 1 : schéma SQLDelight et version lue par l'application à jour (ADR-3), base embarquée conforme à son
    manifeste et au schéma, résultats attendus du lecteur calculés sur cette base, textes FR et EN appariés."""
    import gzip
    import re
    import xml.etree.ElementTree as ET
    sq = _load_tool("gen_sqldelight")
    for path, content in sorted(sq.render().items()):
        check("app (généré)", path.relative_to(ROOT).as_posix(),
              path.exists() and path.read_text(encoding="utf-8") == content, True)
    assets = ROOT / "app/src/main/assets/timetable"
    m = json.loads((assets / "manifest.json").read_text(encoding="utf-8"))
    gz = (assets / "timetable.sqlite.gz").read_bytes()
    raw = gzip.decompress(gz)
    version = int(re.search(r"^PRAGMA user_version = (\d+);$", (ROOT / "schema/timetable.sql").read_text(encoding="utf-8"), re.M).group(1))
    check("base embarquée", "schéma du manifeste", (m["schema"], m["db_schema_version"]),
          ("airport-connect/timetable-manifest@1", version))
    check("base embarquée", "taille et empreinte du fichier compressé",
          (len(gz), hashlib.sha256(gz).hexdigest()), (m["file"]["bytes_gzip"], m["file"]["sha256_gzip"]))
    check("base embarquée", "taille et empreinte de la base",
          (len(raw), hashlib.sha256(raw).hexdigest()), (m["file"]["bytes"], m["file"]["sha256"]))
    check("base embarquée", "PRAGMA user_version", int.from_bytes(raw[60:64], "big"), version)
    goldens = _load_tool("reader_goldens")
    out = goldens.OUT
    check("app (généré)", out.relative_to(ROOT).as_posix(),
          out.exists() and out.read_text(encoding="utf-8") == goldens.render(), True)
    # Itinéraires (spec § 12) : résultats attendus de la lecture de référence sur les réponses PRIM enregistrées.
    jg = _load_tool("journey_goldens")
    text = json.dumps(jg.build(), ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    check("app (généré)", jg.OUT.relative_to(ROOT).as_posix(),
          jg.OUT.exists() and jg.OUT.read_text(encoding="utf-8") == text, True)
    names = {}
    for lang in ("values", "values-en"):
        tree = ET.parse(ROOT / "app/src/main/res" / lang / "strings.xml")
        names[lang] = {e.get("name") for e in tree.getroot() if e.get("translatable") != "false"}
    check("app (textes)", "français et anglais : mêmes clés", sorted(names["values"] ^ names["values-en"]), [])


def _iso_or_none(v) -> bool:
    if v is None:
        return True
    try:
        date.fromisoformat(v)
        return True
    except (TypeError, ValueError):
        return False


def adjustments(ids_lines: dict) -> None:
    """Contrôle statique de data/adjustments.json (R-90 à R-93), sans GTFS."""
    doc = json.loads((ROOT / "data" / "adjustments.json").read_text(encoding="utf-8"))
    check("adjustments.json", "schéma", doc["schema"], "airport-connect/adjustments@2")
    ids = [a["id"] for a in doc["adjustments"]]
    check("adjustments.json", "identifiants uniques", len(ids) == len(set(ids)), True)
    for a in doc["adjustments"]:
        k = a["id"]
        check("adjustments.json", f"{k} statut", a["status"] in ("draft", "published"), True)
        check("adjustments.json", f"{k} nature", a["kind"] in ("temporary", "permanent"), True)
        check("adjustments.json", f"{k} ligne active connue", ids_lines.get(a["line"], {}).get("state"), "active")
        check("adjustments.json", f"{k} dates ISO", _iso_or_none(a["valid_from"]) and _iso_or_none(a["valid_until"])
              and _iso_or_none(a["checked_on"]), True)
        if a["valid_from"] and a["valid_until"]:
            check("adjustments.json", f"{k} période ordonnée", a["valid_from"] <= a["valid_until"], True)
        if a["status"] != "published":
            continue
        check("adjustments.json", f"{k} publiable : textes, source, vérification",
              all(a.get(f) for f in ("title_fr", "summary_fr", "source_url", "checked_on")) and bool(a.get("sources")), True)
        check("adjustments.json", f"{k} temporaire ⇒ date de fin", a["kind"] == "permanent" or bool(a["valid_until"]), True)
        sps = [ns["stop_point"] for ns in a["not_served"]]
        check("adjustments.json", f"{k} quais non desservis uniques", len(sps) == len(set(sps)), True)
        in_force = {r["stop_area"] for r in a["route_in_force"]}
        for ns in a["not_served"]:
            sp = ns["stop_point"]
            check("adjustments.json", f"{k} {sp} identifiant de quai GTFS", sp.startswith("IDFM:"), True)
            check("adjustments.json", f"{k} {sp} sens GTFS", ns["gtfs_direction_id"] in (0, 1), True)
            check("adjustments.json", f"{k} {sp} libellés", bool(ns["label"] and ns["direction_label"]), True)
            rep = ns.get("replacement_stop_area")
            check("adjustments.json", f"{k} {sp} report sur l'itinéraire en vigueur",
                  rep is None or (rep in in_force and bool(ns.get("replacement_label"))), True)


def main() -> int:
    for fn in (eta, boundary, scope, traffic, freshness, identity, lines_registry, realtime_v2, data_files):
        fn()
    for f in failures:
        print("ÉCHEC", f)
    print(f"{passed} cas conformes, {len(failures)} échec(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
