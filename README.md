# Bases de référence (tests)

Bases d'horaires gardées pour rejouer les tests du dépôt de l'application, quelle que soit la base publiée du moment.
Le nettoyage des publications (`tools/pipeline/publish_timetable.sh`, 8 publications « timetable-* » gardées) ne
touche pas à cette branche.

| Fichier | Version | Rôle | SHA-256 (gzip) |
|---|---|---|---|
| `timetable-20261009-c6b9fb6fd6ae.sqlite.gz` | 20261009-c6b9fb6fd6ae | Base publiée le 9 octobre 2026, jour du relevé SIRI : la fusion temps réel des tests se rejoue sur ses horaires (`tools/app/realtime_goldens.py`) | `23081d7c8ad0abdd4d3f3cc5328c73004d37a5d100123177708a5c4cd7197a64` |

Même fichier, octet pour octet, que la publication d'origine (vérifié par son empreinte). Données d'Île-de-France
Mobilités, Licence Mobilités.
