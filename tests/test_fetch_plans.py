#!/usr/bin/env python3
"""
Tests du catalogue des plans (tools/pipeline/fetch_plans.py), sans réseau.

    python3 tests/test_fetch_plans.py

Les réponses de l'API IDFM sont les vraies, relevées le 8 octobre 2026
(tests/plans/opendata-2026-10-08.json). Les documents sont simulés : de petits PDF, JPEG et
pages HTML qui reproduisent les cas rencontrés (lien vers une page web, document ancien…).
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import urllib.parse
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "pipeline"))
import fetch_plans as fp  # noqa: E402

FIX = json.loads((ROOT / "tests" / "plans" / "opendata-2026-10-08.json").read_text(encoding="utf-8"))
# Référentiel des lignes tel qu'il était le 8 octobre 2026, date des réponses de l'API enregistrées
# (avant l'alignement sur le GTFS du jour) : les renumérotations y sont encore à détecter.
LINES = json.loads((ROOT / "tests" / "plans" / "lines-2026-10-08.json").read_text(encoding="utf-8"))
LINES_NOW = json.loads((ROOT / "data" / "lines.json").read_text(encoding="utf-8"))
SOURCES = json.loads((ROOT / "data" / "plans-sources.json").read_text(encoding="utf-8"))
TODAY = date(2026, 10, 8)
failures, passed = [], 0


def check(name, got, expected):
    global passed
    if got == expected:
        passed += 1
    else:
        failures.append(f"{name}\n    obtenu  : {got!r}\n    attendu : {expected!r}")


def pdf(tag: str) -> bytes:
    return (b"%PDF-1.7\n1 0 obj << /Type /Pages /Kids [2 0 R] /Count 1 >> endobj\n"
            b"2 0 obj << /Type /Page /MediaBox [0 0 1190.55 841.89] >> endobj\n% " + tag.encode() + b"\n%%EOF\n")


def jpeg(w: int, h: int) -> bytes:
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0\x00\x11\x08" + h.to_bytes(2, "big") + w.to_bytes(2, "big") + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


HTML = b"<!DOCTYPE html><html><head><title>Plan RER</title></head><body>page</body></html>"


class FakeHttp:
    def __init__(self):
        self.docs, self.fail, self.api_down, self.calls = {}, set(), False, []
        for rec in FIX["line_documents"]["results"]:
            u = rec["url"]
            if u.endswith(".jpg") or ".jpg?" in u:
                self.docs[u] = (jpeg(2480, 1754), {"content-type": "image/jpeg"})
            elif "/plans-lignes/" in u or "/page-lignes/" in u:
                self.docs[u] = (HTML, {"content-type": "text/html; charset=utf-8"})
            else:
                self.docs[u] = (pdf(u), {"content-type": "application/pdf"})
        for u, head in FIX["document_heads"].items():
            body, h = self.docs.get(u, (pdf(u), {"content-type": "application/pdf"}))
            self.docs[u] = (body, {**h, "last-modified": head["last-modified"]})
        for u in (SOURCES["fallbacks"]["RER_B"]["url"], SOURCES["fallbacks"]["RER_C"]["url"]):
            self.docs.setdefault(u, (pdf(u), {"content-type": "application/pdf"}))
        self.docs[FIX["network_plans"]["results"][0]["url"]] = (pdf("noctilien"), {"content-type": "application/pdf"})
        for u in list(self.docs):   # en-tête constaté le 8 octobre 2026 sur tous les fichiers de Paris Aéroport
            if "parisaeroport.fr" in u:
                body, h = self.docs[u]
                self.docs[u] = (body, {**h, "last-modified": "Thu, 22 Nov 2018 13:32:55 GMT"})
        self.api = {"liens-fiches-horaires-et-plans-des-lignes-de-transport-en-commun-dile-de-france": FIX["line_documents"],
                    "plans-region": FIX["network_plans"]}

    def get(self, url, headers=None):
        self.calls.append(url)
        parts = urllib.parse.urlsplit(url)
        if parts.hostname == "data.iledefrance.fr":
            if self.api_down:
                raise IOError("connexion refusée")
            dataset = parts.path.split("/datasets/")[1].split("/")[0]
            q = urllib.parse.parse_qs(parts.query)
            payload = self.api[dataset] if q.get("offset", ["0"])[0] == "0" else {"results": []}
            return fp.Response(200, {"content-type": "application/json"}, json.dumps(payload).encode(), url)
        if url in self.fail:
            raise IOError("HTTP 503")
        body, h = self.docs[url]
        etag = '"' + fp.hashlib.sha256(body).hexdigest()[:16] + '"'
        if headers and headers.get("If-None-Match") == etag:
            return fp.Response(304, {"etag": etag}, b"", url)
        return fp.Response(200, {**h, "etag": etag}, body, url)


def run(http, previous=None, mirror_dir=None, base_url=""):
    return fp.build(LINES, SOURCES, previous, http, TODAY, mirror_dir, base_url)


def by_id(cat):
    return {p["id"]: p for p in cat["plans"]}


def codes(report, code):
    return sorted((w["detail"]["plan"] if isinstance(w["detail"], dict) and "plan" in w["detail"] else w["detail"].get("line"))
                  for w in report["warnings"] if w["code"] == code)


# 1. Premier passage
http = FakeHttp()
cat, rep = run(http)
p = by_id(cat)
check("1 : aucune erreur", rep["errors"], [])
check("1 : comptes (48 lignes + plan Noctilien ; A01 et A04 sans plan)", cat["counts"],
      {"ok": 49, "kept_previous": 0, "error": 0, "no_plan": 2})
check("1 : 51 entrées (50 lignes actives + 1 plan de réseau)", len(cat["plans"]), 51)
check("1 : 351, plan IDFM du 24 août 2026",
      (p["BUS_351"]["source"]["via"], p["BUS_351"]["document"]["date"], p["BUS_351"]["document"]["date_source"],
       p["BUS_351"]["document"]["media_type"], p["BUS_351"]["document"]["pages"]),
      ("idfm", "2026-08-24", "url", "application/pdf", 1))
check("1 : format A3 paysage lu dans le PDF (points)", (p["BUS_351"]["document"]["width"], p["BUS_351"]["document"]["height"]), (1191, 842))
check("1 : RER B et C, page web IDFM refusée puis repli RATP",
      [(p[k]["source"]["via"], p[k]["source"]["provider"]) for k in ("RER_B", "RER_C")],
      [("fallback", "www.ratp.fr"), ("fallback", "www.ratp.fr")])
check("1 : alerte de repli", codes(rep, "plan_fallback_used"), ["RER_B", "RER_C"])
check("1 : RER C daté par Last-Modified", (p["RER_C"]["document"]["date"], p["RER_C"]["document"]["date_source"]),
      ("2024-09-23", "last_modified"))
check("1 : N2, image JPEG datée par son titre", (p["BUS_N2"]["document"]["media_type"], p["BUS_N2"]["document"]["width"],
      p["BUS_N2"]["document"]["height"], p["BUS_N2"]["document"]["date"], p["BUS_N2"]["document"]["date_source"]),
      ("image/jpeg", 2480, 1754, "2021-12-29", "title"))
check("1 : CDGVal, Last-Modified de Paris Aéroport ignoré (non fiable) : pas de date inventée",
      (p["CDGVAL"]["document"]["date"], p["CDGVAL"]["document"]["date_source"]), (None, None))
check("1 : 703, date tirée de l'identifiant uniqid du fichier", p["BUS_703"]["document"]["date"], "2023-09-11")
check("1 : Noctilien, plan du réseau de nuit daté par le jeu IDFM",
      (p["NOCTILIEN"]["kind"], p["NOCTILIEN"]["document"]["date"], p["NOCTILIEN"]["source"]["dataset_licence"],
       len(p["NOCTILIEN"]["lines"]) > 0), ("network", "2026-09", "CC BY-NC-ND 4.0", True))
check("1 : A01 et A04 sans plan, avec page d'information",
      [(p[k]["status"], bool(p[k]["info_url"])) for k in ("BUS_A01", "BUS_A04")], [("no_plan", True), ("no_plan", True)])
check("1 : documents de plus de deux ans signalés", codes(rep, "plan_old"),
      sorted(["BUS_N1", "BUS_N2", "BUS_703", "BUS_TBUS1", "BUS_607", "BUS_609", "BUS_610", "BUS_9518", "RER_C"]))
check("1 : renumérotations repérées dans le référentiel IDFM",
      sorted((w["detail"]["line"], w["detail"]["idfm_referential"]) for w in rep["warnings"] if w["code"] == "line_name_differs"),
      [("BUS_9501", "1601"), ("BUS_9502", "1602"), ("BUS_EX100", "700"), ("BUS_EX19", "7719"),
       ("BUS_EX93", "9301"), ("BUS_R4", "1614")])
_, rep_now = fp.build(LINES_NOW, SOURCES, None, FakeHttp(), TODAY, None, "")
check("1 : référentiel actuel aligné sur les noms du référentiel IDFM (aucune renumérotation restante)",
      [w["detail"]["line"] for w in rep_now["warnings"] if w["code"] == "line_name_differs"], [])
check("1 : distribution par lien par défaut", (cat["distribution"], "mirror_name" in p["BUS_351"]["file"]), ("link", False))

# 2. Deuxième passage identique : requêtes conditionnelles, même version
http2 = FakeHttp()
cat2, rep2 = run(http2, previous=json.loads(json.dumps(cat)))
check("2 : même version quand rien ne change", cat2["version"], cat["version"])
check("2 : aucune mise à jour signalée", codes(rep2, "plan_updated"), [])
check("2 : premier relevé conservé", by_id(cat2)["BUS_351"]["first_seen"], "2026-10-08")

# 3. Nouveau plan publié pour la 351 (nouvelle URL ilico)
http3 = FakeHttp()
new_url = "https://ilico.iledefrance-mobilites.fr/uploads/plans/20261007T090000Z-00000000-0000-0000-0000-000000000351.pdf"
fixture = json.loads(json.dumps(FIX["line_documents"]))
for r in fixture["results"]:
    if r["id_line"] == "C01299":
        r["url"] = new_url
http3.api["liens-fiches-horaires-et-plans-des-lignes-de-transport-en-commun-dile-de-france"] = fixture
http3.docs[new_url] = (pdf("351 v04"), {"content-type": "application/pdf"})
cat3, rep3 = run(http3, previous=json.loads(json.dumps(cat)))
check("3 : mise à jour détectée et datée", [(w["detail"]["plan"], w["detail"]["before"], w["detail"]["now"])
                                           for w in rep3["warnings"] if w["code"] == "plan_updated"],
      [("BUS_351", "2026-08-24", "2026-10-07")])
check("3 : nouvelle version du catalogue", cat3["version"] != cat["version"], True)

# 4. Panne d'une source : la version connue est conservée ; sans version connue, erreur
http4 = FakeHttp()
u350 = by_id(cat)["BUS_350"]["source"]["url"]
http4.fail.add(u350)
cat4, rep4 = run(http4, previous=json.loads(json.dumps(cat)))
check("4 : plan conservé après échec", (by_id(cat4)["BUS_350"]["status"], by_id(cat4)["BUS_350"]["document"]["sha256"]),
      ("kept_previous", by_id(cat)["BUS_350"]["document"]["sha256"]))
check("4 : catalogue toujours publiable", rep4["errors"], [])
cat4b, rep4b = run(http4)
check("4 : sans version connue, erreur sur ce seul plan", (by_id(cat4b)["BUS_350"]["status"], rep4b["errors"]), ("error", []))

# 5. Pannes massives, puis API injoignable : rien n'est publiable
http5 = FakeHttp()
http5.fail.update(u for u in http5.docs)
cat5, rep5 = run(http5)
check("5 : trop d'échecs bloque la publication", [e["code"] for e in rep5["errors"]], ["too_many_failures"])
http6 = FakeHttp()
http6.api_down = True
cat6, rep6 = run(http6)
check("5 : API injoignable", (cat6, [e["code"] for e in rep6["errors"]]), (None, ["source_unreachable"]))

# 6. Faux PDF : page HTML servie en application/pdf
http7 = FakeHttp()
http7.docs[u350] = (HTML, {"content-type": "application/pdf"})
cat7, rep7 = run(http7)
check("6 : signature vérifiée, Content-Type ignoré", (by_id(cat7)["BUS_350"]["status"],
      by_id(cat7)["BUS_350"]["problems"][0]["error"]), ("error", "pas un document (page web ou format inconnu)"))

# 7. Mode copie : fichiers nommés par empreinte, adresse publique
tmp = Path(tempfile.mkdtemp())
try:
    cat8, rep8 = run(FakeHttp(), mirror_dir=tmp, base_url="https://data.example/plans")
    f = by_id(cat8)["BUS_351"]["file"]
    check("7 : copie nommée par empreinte", f["mirror_name"], by_id(cat8)["BUS_351"]["document"]["sha256"] + ".pdf")
    check("7 : fichier écrit, contenu identique",
          fp.hashlib.sha256((tmp / f["mirror_name"]).read_bytes()).hexdigest(), by_id(cat8)["BUS_351"]["document"]["sha256"])
    check("7 : adresse publique", f["mirror_url"], "https://data.example/plans/files/" + f["mirror_name"])
    shas = {p["document"]["sha256"] for p in cat8["plans"] if p.get("document")}
    check("7 : un fichier par contenu distinct (CDGVal et N2 simulés identiques : 48)",
          (len(list(tmp.iterdir())), len(shas)), (48, 48))
    # passage suivant en mode copie : pas de nouveau téléchargement nécessaire
    http9 = FakeHttp()
    cat9, _ = run(http9, previous=json.loads(json.dumps(cat8)), mirror_dir=tmp, base_url="https://data.example/plans")
    check("7 : version stable en mode copie", cat9["version"], cat8["version"])
finally:
    shutil.rmtree(tmp)

# 8. Régressions relevées en revue (8 octobre 2026)
http = FakeHttp()
http.api["liens-fiches-horaires-et-plans-des-lignes-de-transport-en-commun-dile-de-france"] = {"total_count": 0, "results": []}
c, r = run(http)
check("8 : réponse vide de l'API = source injoignable, rien de publié", (c, [e["code"] for e in r["errors"]]),
      (None, ["source_unreachable"]))

http = FakeHttp()
fixture = json.loads(json.dumps(FIX["line_documents"]))
fixture["results"] = [x for x in fixture["results"] if x["id_line"] != "C01298"]
http.api["liens-fiches-horaires-et-plans-des-lignes-de-transport-en-commun-dile-de-france"] = fixture
c, r = run(http, previous=json.loads(json.dumps(cat)))
check("8 : plan connu retiré du jeu IDFM, version conservée (R-184)",
      (by_id(c)["BUS_350"]["status"], by_id(c)["BUS_350"]["document"]["sha256"]),
      ("kept_previous", by_id(cat)["BUS_350"]["document"]["sha256"]))

lines_before = by_id(cat)["NOCTILIEN"]["lines"]
prev = json.loads(json.dumps(cat))
saved = LINES
LINES = json.loads(json.dumps(saved))
for l in LINES["lines"]:
    if l["id"] == "BUS_N22":
        l["group"] = None
c, r = run(FakeHttp(), previous=prev)
check("8 : réponse 304, mais composition du groupe Noctilien mise à jour",
      ("BUS_N22" in lines_before, "BUS_N22" in by_id(c)["NOCTILIEN"]["lines"], c["version"] != cat["version"]), (True, False, True))
LINES = saved

tmp = Path(tempfile.mkdtemp())
try:
    cm, _ = run(FakeHttp(), mirror_dir=tmp, base_url="https://data.example/plans")
    cl, _ = run(FakeHttp(), previous=json.loads(json.dumps(cm)))
    check("8 : passage de la copie au lien publié (version différente, plus aucune copie annoncée)",
          (cl["version"] != cm["version"], cl["distribution"], any("mirror_name" in p.get("file", {}) for p in cl["plans"])),
          (True, "link", False))
finally:
    shutil.rmtree(tmp)

http = FakeHttp()
http.fail.add(u350)
c1, _ = run(http, previous=json.loads(json.dumps(cat)))
c2, _ = run(http, previous=json.loads(json.dumps(c1)))
check("8 : panne qui dure, catalogue non republié chaque jour", c2["version"], c1["version"])

check("8 : date de titre impossible ignorée, sans plantage",
      fp.document_date("https://x/plan.pdf", "mise à jour 31/02/2026", None, None, TODAY), (None, None))
check("8 : identifiant uniqid invraisemblable ignoré",
      fp.document_date("https://x/deadbeefcafe1_plan.pdf", "", None, None, TODAY), (None, None))
check("8 : date de jeu au mois",
      fp.document_date("https://x/p.pdf", "", "2026-09", None, TODAY), ("2026-09", "dataset"))

http = FakeHttp()
seen = []
orig = http.get
def spy(url, headers=None):
    if "parisaeroport.fr" in url:
        seen.append(dict(headers or {}))
    return orig(url, headers)
http.get = spy
prev = json.loads(json.dumps(cat))
for pl in prev["plans"]:
    if pl["id"] in ("CDGVAL", "BUS_N1", "BUS_N2"):
        pl["http"] = {"etag": None, "last_modified": "Thu, 22 Nov 2018 13:32:55 GMT"}
run(http, previous=prev)
check("8 : pas d'If-Modified-Since vers un serveur aux dates non fiables",
      [h for h in seen if "If-Modified-Since" in h], [])

fill = jpeg(800, 600).replace(b"\xff\xc0", b"\xff\xff\xff\xc0", 1)
check("8 : JPEG avec octets de remplissage", fp.image_size(fill, "image/jpeg"), (800, 600))
incremental = pdf("v1") + b"2 0 obj << /Type /Page /MediaBox [0 0 1190.55 841.89] /Rotate 0 >> endobj\n%%EOF\n"
check("8 : PDF à mise à jour incrémentale, pages lues dans /Count", fp.pdf_info(incremental)[0], 1)

for f in failures:
    print("ÉCHEC", f)
print(f"{passed} contrôles conformes, {len(failures)} échec(s)")
sys.exit(1 if failures else 0)
