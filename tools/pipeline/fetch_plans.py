#!/usr/bin/env python3
"""
Catalogue des plans de lignes, reconstruit automatiquement (spec R-180 à R-186, modèle § 9).

    python3 tools/pipeline/fetch_plans.py --out dist-plans
        [--previous-catalogue prev/catalogue.json] [--mirror] [--base-url https://…/plans]

Sources (data/plans-sources.json) :
  * jeu IDFM « liens fiches horaires et plans », interrogé par code C, documents de type PLAN ;
  * jeu IDFM « plans du réseau régional », pour le plan du réseau de nuit (groupe NOCTILIEN) ;
  * une source de repli par ligne, utilisée seulement si le lien IDFM ne mène pas à un document.

Produit dans --out :
    catalogue.json   un enregistrement par ligne active et par plan de réseau
    report.json      alertes et erreurs (toujours écrit)
    files/<sha256>.<ext>   avec --mirror uniquement : copie des documents, à publier avant le catalogue

Chaque document est téléchargé, identifié par sa signature (PDF, JPEG, PNG, WebP ; une page HTML
est refusée), puis décrit : empreinte, taille, pages ou dimensions, date d'édition. Un échec de
téléchargement ne fait jamais disparaître un plan déjà connu : la version précédente est conservée.
Code de sortie : 0 si le catalogue est publiable, 2 sinon. Stdlib uniquement, Python 3.9+.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
USER_AGENT = "AirportConnect-plans/1.0 (+https://airportconnect.info)"
EXTENSIONS = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


# --------------------------------------------------------------------------- HTTP
class Response:
    def __init__(self, status: int, headers: dict, body: bytes, url: str) -> None:
        self.status, self.headers, self.body, self.url = status, headers, body, url


class Http:
    """Client HTTP minimal. Remplaçable dans les tests (méthode get)."""

    def __init__(self, timeout: float = 60.0, retries: int = 3, max_bytes: int = 40 << 20) -> None:
        self.timeout, self.retries, self.max_bytes = timeout, retries, max_bytes

    def get(self, url: str, headers: dict = None) -> Response:
        last = None
        for attempt in range(self.retries):
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    body = r.read(self.max_bytes + 1)
                    return Response(r.status, {k.lower(): v for k, v in r.headers.items()}, body, r.geturl())
            except urllib.error.HTTPError as e:
                if e.code == 304 or 400 <= e.code < 500:
                    return Response(e.code, {k.lower(): v for k, v in e.headers.items()}, b"", url)
                last = e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = e
            time.sleep(2 * (attempt + 1))
        raise IOError(f"{url} : {last}")


# --------------------------------------------------------------------------- documents
def sniff(body: bytes):
    """Type réel d'un fichier d'après sa signature, indépendamment du Content-Type annoncé."""
    head = body[:1024]
    if b"%PDF-" in head:
        return "application/pdf"
    if body[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if body[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_size(body: bytes, media_type: str):
    try:
        if media_type == "image/png":
            return struct.unpack(">II", body[16:24])
        if media_type == "image/jpeg":
            i = 2
            while i + 9 < len(body):
                if body[i] != 0xFF:
                    i += 1
                    continue
                marker = body[i + 1]
                if marker == 0xFF:          # octet de remplissage
                    i += 1
                    continue
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                length = struct.unpack(">H", body[i + 2:i + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", body[i + 5:i + 9])
                    return w, h
                i += 2 + length
        if media_type == "image/webp":
            chunk = body[12:16]
            if chunk == b"VP8X":
                return 1 + int.from_bytes(body[24:27], "little"), 1 + int.from_bytes(body[27:30], "little")
            if chunk == b"VP8L":
                b = int.from_bytes(body[21:25], "little")
                return 1 + (b & 0x3FFF), 1 + ((b >> 14) & 0x3FFF)
            if chunk == b"VP8 ":
                w, h = struct.unpack("<HH", body[26:30])
                return w & 0x3FFF, h & 0x3FFF
    except (struct.error, IndexError):
        pass
    return None


_PAGE = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")
_PAGES_COUNT = re.compile(rb"/Type\s*/Pages\b[^>]*?/Count\s+(\d+)|/Count\s+(\d+)[^>]*?/Type\s*/Pages\b")
_MEDIABOX = re.compile(rb"/MediaBox\s*\[\s*(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s*\]")
_STREAM = re.compile(rb"(?<!end)stream\r?\n")


def pdf_info(body: bytes):
    """(pages, largeur, hauteur en points) ; None quand l'information n'est pas lisible simplement.
    Les objets compressés (flux d'objets) sont décompressés pour la recherche."""
    texts = [body]
    for m in _STREAM.finditer(body):
        end = body.find(b"endstream", m.end())
        if end < 0:
            break
        try:
            texts.append(zlib.decompressobj().decompress(body[m.end():end], 4 << 20))
        except zlib.error:
            continue
    # nombre de pages : /Count de l'arbre des pages (le plus grand = la racine) ; à défaut, objets /Page
    # (surestimé si le fichier contient des mises à jour incrémentales)
    counts = [int(a or b) for t in texts for a, b in _PAGES_COUNT.findall(t)]
    pages = max(counts) if counts else sum(len(_PAGE.findall(t)) for t in texts)
    box = next((b for t in texts for b in [_MEDIABOX.search(t)] if b), None)
    size = None
    if box:
        x0, y0, x1, y1 = (float(v) for v in box.groups())
        size = (round(abs(x1 - x0)), round(abs(y1 - y0)))
    return (pages or None), size


_ILICO_TS = re.compile(r"/(\d{8})T\d{6}Z-")
_UNIQID = re.compile(r"/([0-9a-f]{8})[0-9a-f]{5}_")
_FR_DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


EARLIEST_DOCUMENT = date(2010, 1, 1)
_DATASET_DATE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")


def document_date(url: str, title: str, dataset_date, last_modified, today: date):
    """Date d'édition du document, de la source la plus fiable à la moins fiable. Une valeur
    illisible ou invraisemblable (avant 2010 ou après aujourd'hui) est ignorée, jamais inventée."""
    def plausible(d):
        return d if d and EARLIEST_DOCUMENT <= d <= today else None

    def attempt(fn):
        try:
            return plausible(fn())
        except (ValueError, OverflowError, OSError):
            return None

    m = _ILICO_TS.search(url)
    d = attempt(lambda: datetime.strptime(m.group(1), "%Y%m%d").date()) if m else None
    if d:
        return d.isoformat(), "url"
    m = _UNIQID.search(url)
    d = attempt(lambda: datetime.fromtimestamp(int(m.group(1), 16), timezone.utc).date()) if m else None
    if d:
        return d.isoformat(), "url"
    m = _FR_DATE.search(title or "")
    d = attempt(lambda: date(int(m.group(3)), int(m.group(2)), int(m.group(1)))) if m else None
    if d:
        return d.isoformat(), "title"
    if dataset_date and _DATASET_DATE.match(dataset_date):
        first = dataset_date if len(dataset_date) == 10 else dataset_date + "-01"
        if attempt(lambda: date.fromisoformat(first)):
            return dataset_date, "dataset"          # précision du jeu : jour ou mois
    if last_modified:
        d = attempt(lambda: datetime.strptime(last_modified, "%a, %d %b %Y %H:%M:%S GMT").date())
        if d:
            return d.isoformat(), "last_modified"
    return None, None


# --------------------------------------------------------------------------- sources
def api_records(http: Http, portal: str, dataset: str, where: str, select: str = None) -> list:
    out, offset = [], 0
    while True:
        q = {"limit": "100", "offset": str(offset), "where": where, "order_by": "id_line,url" if "id_line" in where else "id"}
        if select:
            q["select"] = select
        url = f"{portal}/api/explore/v2.1/catalog/datasets/{dataset}/records?{urllib.parse.urlencode(q)}"
        r = http.get(url)
        if r.status != 200:
            raise IOError(f"{url} : HTTP {r.status}")
        page = json.loads(r.body.decode("utf-8"))["results"]
        out += page
        if len(page) < 100:
            return out
        offset += 100


def candidates(lines_doc: dict, sources: dict, http: Http, report: dict) -> list:
    """Liste des plans à résoudre : une entrée par ligne active, une par plan de réseau."""
    ds = sources["datasets"]["line_documents"]
    active = [l for l in lines_doc["lines"] if l["state"] == "active"]
    codes = {l["idfm_route_id"].split(":", 1)[1]: l for l in active}
    where = "id_line in ({}) and type=\"{}\"".format(",".join(f'"{c}"' for c in sorted(codes)), ds["document_type"])
    recs = api_records(http, ds["portal"], ds["dataset_id"], where, "id_line,name_line,document_name,url,type")
    if not recs:
        raise IOError("le jeu IDFM ne renvoie aucun plan pour nos lignes (réponse vide)")
    by_line = {}
    for r in recs:
        by_line.setdefault(r["id_line"], []).append(r)
    out = []
    for code, line in sorted(codes.items(), key=lambda x: x[1]["id"]):
        lid = line["id"]
        entry = {"id": lid, "kind": "line", "lines": [lid], "tries": []}
        rows = sorted(by_line.get(code, []), key=lambda r: (r["document_name"] or "", r["url"]))
        if len(rows) > 1:
            report["warnings"].append({"code": "several_plans", "detail": {"line": lid, "count": len(rows)}})
        for r in rows:
            entry["tries"].append({"url": r["url"], "title": r["document_name"], "via": "idfm",
                                   "dataset": ds["dataset_id"], "dataset_licence": ds["licence"]})
            short = (line.get("gtfs") or {}).get("short_name")
            if r.get("name_line") and short and r["name_line"].strip() != short.strip():
                report["warnings"].append({"code": "line_name_differs", "detail": {
                    "line": lid, "lines_json": short, "idfm_referential": r["name_line"]}})
        fb = sources["fallbacks"].get(lid)
        if fb:
            entry["tries"].append({"url": fb["url"], "title": fb["title"], "via": "fallback", "provider": fb["provider"],
                                   "dataset": None, "dataset_licence": None})
        if lid in sources["no_plan"]:
            entry["no_plan"] = sources["no_plan"][lid]
        out.append(entry)
    net = sources["datasets"]["network_plans"]
    for pid, spec in sorted(sources["network_plans"].items()):
        recs = api_records(http, net["portal"], net["dataset_id"], f'id="{spec["record_id"]}"')
        entry = {"id": pid, "kind": "network", "lines": sorted(l["id"] for l in active if l.get("group") == spec["lines_group"]),
                 "tries": []}
        for r in recs:
            if r.get("nom_plan") != spec["expected_title"]:
                report["warnings"].append({"code": "network_plan_renamed", "detail": {
                    "plan": pid, "expected": spec["expected_title"], "now": r.get("nom_plan")}})
            entry["tries"].append({"url": r["url"], "title": r["nom_plan"], "via": "idfm", "dataset": net["dataset_id"],
                                   "dataset_licence": net["licence"], "dataset_date": r.get("date_publication")})
        out.append(entry)
    return out


# --------------------------------------------------------------------------- résolution
def _carry(known: dict, base: dict, mirror_dir, **extra) -> dict:
    """Version connue reprise telle quelle, mais rattachée à l'entrée du jour (lignes, nature) et au
    mode de diffusion du jour : en mode lien, aucune copie n'est annoncée."""
    out = {**known, **base, **extra}
    if mirror_dir is None:
        out["file"] = {"url": known["file"]["url"]}
    return out


def _age_days(ddate: str, today: date):
    if not ddate:
        return None
    return (today - date.fromisoformat(ddate if len(ddate) == 10 else ddate + "-01")).days


def resolve(entry: dict, prev: dict, http: Http, limits: dict, today: date, mirror_dir, report: dict,
            untrusted_lm=()) -> dict:
    """Télécharge le premier document valide parmi les sources de l'entrée."""
    base = {"id": entry["id"], "kind": entry["kind"], "lines": entry["lines"]}
    known = {k: v for k, v in (prev or {}).items() if k != "problems"} if prev and prev.get("document") else None
    if not entry["tries"]:
        if "no_plan" in entry:
            return {**base, "status": "no_plan", "reason": entry["no_plan"]["reason"], "info_url": entry["no_plan"]["info_url"]}
        if known:   # R-184 : un plan connu ne disparaît pas parce que sa source ne le référence plus
            problem = {"url": None, "error": "plus référencé par la source"}
            report["warnings"].append({"code": "plan_kept_previous", "detail": {"plan": entry["id"], "problems": [problem]}})
            return _carry(known, base, mirror_dir, status="kept_previous", problems=[problem])
        report["warnings"].append({"code": "plan_source_missing", "detail": entry["id"]})
        return {**base, "status": "no_plan", "reason": "Aucune source connue.", "info_url": None}
    problems = []
    for t in entry["tries"]:
        cond = {}
        # requête conditionnelle seulement si la version connue suffit telle quelle
        # (en mode copie, il faut qu'elle ait déjà été copiée : son nom de fichier est immuable)
        if known and known["source"]["url"] == t["url"] and (mirror_dir is None or known["file"].get("mirror_name")):
            if known.get("http", {}).get("etag"):
                cond["If-None-Match"] = known["http"]["etag"]
            # If-Modified-Since seulement si le serveur date vraiment ses fichiers
            if known.get("http", {}).get("last_modified") and urllib.parse.urlsplit(t["url"]).hostname not in untrusted_lm:
                cond["If-Modified-Since"] = known["http"]["last_modified"]
        try:
            r = http.get(t["url"], cond)
        except IOError as e:
            problems.append({"url": t["url"], "error": str(e)})
            continue
        if r.status == 304 and cond:
            return _carry(known, base, mirror_dir, status="ok", title=t["title"], checked_on=today.isoformat())
        if r.status != 200:
            problems.append({"url": t["url"], "error": f"HTTP {r.status}"})
            continue
        if len(r.body) > limits["max_bytes"]:
            problems.append({"url": t["url"], "error": "document trop lourd"})
            continue
        media = sniff(r.body)
        if media is None:
            problems.append({"url": t["url"], "error": "pas un document (page web ou format inconnu)",
                             "content_type": r.headers.get("content-type")})
            continue
        sha = hashlib.sha256(r.body).hexdigest()
        doc = {"media_type": media, "bytes": len(r.body), "sha256": sha}
        if media == "application/pdf":
            pages, size = pdf_info(r.body)
            doc.update({"pages": pages, "width": size[0] if size else None, "height": size[1] if size else None, "unit": "pt"})
        else:
            size = image_size(r.body, media)
            doc.update({"pages": 1, "width": size[0] if size else None, "height": size[1] if size else None, "unit": "px"})
        host = urllib.parse.urlsplit(t["url"]).hostname
        trusted_lm = host not in untrusted_lm
        ddate, dsrc = document_date(t["url"], t["title"], t.get("dataset_date"),
                                    r.headers.get("last-modified") if trusted_lm else None, today)
        same = bool(known) and known["document"]["sha256"] == sha
        if same and known["document"].get("date"):
            # même contenu : la date déjà retenue est gardée (un serveur qui réhorodate ne change rien)
            ddate, dsrc = known["document"]["date"], known["document"]["date_source"]
        doc.update({"date": ddate, "date_source": dsrc})
        file = {"url": t["url"]}
        if mirror_dir is not None:
            name = f"{sha}.{EXTENSIONS[media]}"
            (mirror_dir / name).write_bytes(r.body)
            file["mirror_name"] = name
        if known and not same:
            report["warnings"].append({"code": "plan_updated", "detail": {
                "plan": entry["id"], "before": known["document"].get("date"), "now": ddate, "url": t["url"]}})
        age = _age_days(ddate, today)
        if age is not None and age > limits["old_document_days"]:
            report["warnings"].append({"code": "plan_old", "detail": {"plan": entry["id"], "date": ddate}})
        if t["via"] == "fallback":
            report["warnings"].append({"code": "plan_fallback_used", "detail": {"plan": entry["id"], "url": t["url"],
                                                                               "skipped": problems}})
        return {**base, "status": "ok", "title": t["title"],
                "source": {"url": t["url"], "via": t["via"], "provider": host,
                           "dataset": t["dataset"], "dataset_licence": t["dataset_licence"]},
                "document": doc, "file": file,
                "http": {"etag": r.headers.get("etag"), "last_modified": r.headers.get("last-modified")},
                "first_seen": known["first_seen"] if same else today.isoformat(),
                "checked_on": today.isoformat()}
    if known:
        report["warnings"].append({"code": "plan_kept_previous", "detail": {"plan": entry["id"], "problems": problems}})
        return _carry(known, base, mirror_dir, status="kept_previous", problems=problems)
    report["warnings"].append({"code": "plan_unavailable", "detail": {"plan": entry["id"], "problems": problems}})
    return {**base, "status": "error", "problems": problems}


def build(lines_doc: dict, sources: dict, previous: dict, http: Http, today: date, mirror_dir, base_url: str):
    report = {"errors": [], "warnings": []}
    prev_by_id = {p["id"]: p for p in (previous or {}).get("plans", [])}
    try:
        entries = candidates(lines_doc, sources, http, report)
    except (IOError, ValueError, KeyError) as e:
        report["errors"].append({"code": "source_unreachable", "detail": str(e)})
        return None, report
    plans = [resolve(e, prev_by_id.get(e["id"]), http, sources["limits"], today, mirror_dir, report,
                     tuple(sources.get("untrusted_last_modified_hosts", {}))) for e in entries]
    tried = [p for p in plans if p["status"] != "no_plan"]
    failed = [p for p in tried if p["status"] in ("error", "kept_previous")]
    if tried and len(failed) / len(tried) > sources["limits"]["max_failure_ratio"]:
        report["errors"].append({"code": "too_many_failures", "detail": {"failed": len(failed), "tried": len(tried)}})
    for p in plans:
        if base_url and p.get("file", {}).get("mirror_name"):
            p["file"]["mirror_url"] = base_url.rstrip("/") + "/files/" + p["file"]["mirror_name"]
    # version : contenu diffusé et mode de diffusion ; ni horodatages ni texte d'erreur (une panne qui
    # dure ne republie pas le catalogue chaque jour)
    stable = json.dumps({"distribution": "mirror" if mirror_dir is not None else "link",
                         "plans": [{k: v for k, v in p.items() if k not in ("checked_on", "http", "problems")} for p in plans]},
                        ensure_ascii=False, sort_keys=True)
    catalogue = {
        "schema": "airport-connect/plans-catalogue@1",
        "version": hashlib.sha256(stable.encode()).hexdigest()[:16],     # change ssi le contenu change
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "distribution": "mirror" if mirror_dir is not None else "link",
        "counts": {s: sum(1 for p in plans if p["status"] == s) for s in ("ok", "kept_previous", "error", "no_plan")},
        "plans": plans,
    }
    return catalogue, report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--data", type=Path, default=ROOT / "data")
    ap.add_argument("--previous-catalogue", type=Path)
    ap.add_argument("--mirror", action="store_true", help="copier les documents (droits à confirmer, décision D-9)")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--today", type=date.fromisoformat, default=None)
    args = ap.parse_args()

    lines_doc = json.loads((args.data / "lines.json").read_text(encoding="utf-8"))
    sources = json.loads((args.data / "plans-sources.json").read_text(encoding="utf-8"))
    previous = None
    if args.previous_catalogue and args.previous_catalogue.exists():
        previous = json.loads(args.previous_catalogue.read_text(encoding="utf-8"))
    args.out.mkdir(parents=True, exist_ok=True)
    mirror_dir = None
    if args.mirror:
        mirror_dir = args.out / "files"
        mirror_dir.mkdir(exist_ok=True)
    today = args.today or datetime.now(timezone.utc).date()
    catalogue, report = build(lines_doc, sources, previous, Http(max_bytes=sources["limits"]["max_bytes"]),
                              today, mirror_dir, args.base_url)
    report["ok"] = not report["errors"]
    (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if report["errors"] or catalogue is None:
        print("ÉCHEC : catalogue non publié", json.dumps(report["errors"], ensure_ascii=False)[:2000])
        return 2
    (args.out / "catalogue.json").write_text(json.dumps(catalogue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    c = catalogue["counts"]
    print(f"OK {catalogue['version']} : {c['ok']} plans, {c['kept_previous']} conservés, {c['error']} en erreur, "
          f"{c['no_plan']} sans plan, {len(report['warnings'])} alerte(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
