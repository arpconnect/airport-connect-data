# Airport Connect — données publiées

Horaires et plans des transports vers les aéroports de Paris, construits chaque jour à partir des données ouvertes d'Île-de-France Mobilités, pour l'application Android Airport Connect.

## Ce qui est publié

| Contenu | Adresse | Mise à jour |
|---|---|---|
| Manifeste de la base d'horaires (version, validité, empreintes, adresse de la base) | `published/timetable/manifest.json` | Quand le GTFS IDFM change |
| Base d'horaires SQLite compressée (gzip) | Pièce jointe de la release `timetable-<version>` | Une release par version ; les 8 plus récentes sont gardées |
| Catalogue des plans de lignes | `published/plans/catalogue.json` | Quand un plan change |

Adresses publiques :

- `https://raw.githubusercontent.com/arpconnect/airport-connect-data/main/published/timetable/manifest.json`
- `https://raw.githubusercontent.com/arpconnect/airport-connect-data/main/published/plans/catalogue.json`
- la base : l'adresse `file.url` du manifeste, toujours vérifiée par son empreinte `file.sha256_gzip`.

Les traitements (`.github/workflows/`) tournent chaque jour sur GitHub Actions. Ils ne publient que si les contrôles bloquants passent ; sinon, la version précédente reste en ligne.

## Source et licence des données

- Horaires : GTFS « Horaires prévus sur les lignes de transport en commun d'Ile-de-France (GTFS Datahub) », publié par Île-de-France Mobilités sous [Licence Mobilités](https://data.iledefrance-mobilites.fr/explore/dataset/offre-horaires-tc-gtfs-idfm/information/). Les bases publiées ici en sont des bases dérivées, diffusées sous la même licence.
- Plans : liens vers les documents publiés par Île-de-France Mobilités et les transporteurs ; les documents restent chez leur source et sous leur propre licence.

## Organisation

Ce dépôt est une copie générée d'une partie du dépôt de l'application (chaîne de traitement `tools/`, référentiel `data/`, `design/`, `schema/`, `tests/`). Il ne se modifie pas directement : seuls les traitements y écrivent, dans `published/`.

```bash
python3 tools/check_fixtures.py
python3 tools/pipeline/build_timetable.py IDFM-gtfs.zip --out dist
python3 tests/test_timetable_db.py dist/timetable-*.sqlite.gz
```

Python 3.9 ou plus récent, bibliothèque standard uniquement.
