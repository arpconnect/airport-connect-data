# Relevé d'exactitude du temps réel

41 veille(s), du 2026-11-10 22:08 au 2026-11-24 19:26 (heure de Paris). Requêtes : 39360, dont 3 sans réponse exploitable ; 0 veille(s) arrêtée(s) par un 429.

Le « départ » est la dernière annonce lue avant que la course quitte le quai, connue à 30 s près (intervalle de lecture) plus 25 s (partage du proxy). Écart positif : parti plus tard qu'annoncé.

## Écart entre l'annonce et le départ (médiane, 9 départs sur 10, part à 60 s près)

| Ligne | Courses | Jamais mesurées | Disparues | 10 min avant | 5 min avant | 3 min avant | 2 min avant | 1 min avant |
|---|---|---|---|---|---|---|---|---|
| BUS_183 | 109 | 0 | 5 | 44 s ; 145 s ; 62 % (55) | 44 s ; 140 s ; 64 % (75) | 48 s ; 112 s ; 62 % (80) | 34 s ; 73 s ; 81 % (78) | 4 s ; 34 s ; 92 % (79) |
| BUS_350 | 42 | 2 | 18 | 66 s ; 547 s ; 50 % (16) | 123 s ; 238 s ; 38 % (21) | 93 s ; 123 s ; 32 % (25) | 36 s ; 91 s ; 67 % (24) | 0 s ; 33 s ; 100 % (26) |
| BUS_351 | 35 | 2 | 6 | 196 s ; 380 s ; 20 % (10) | 79 s ; 362 s ; 11 % (19) | 79 s ; 262 s ; 29 % (17) | 70 s ; 181 s ; 31 % (16) | 33 s ; 161 s ; 50 % (18) |
| METRO_14 | 568 | 0 | 61 | 0 s ; 64 s ; 87 % (189) | 0 s ; 57 s ; 90 % (363) | 0 s ; 30 s ; 91 % (433) | 0 s ; 23 s ; 93 % (442) | 0 s ; 0 s ; 97 % (453) |
| ORLYVAL | 0 | 0 | 0 | — | — | — | — | — |
| RER_B | 441 | 0 | 72 | 25 s ; 114 s ; 77 % (226) | 16 s ; 80 s ; 88 % (318) | 8 s ; 40 s ; 97 % (348) | 7 s ; 47 s ; 93 % (367) | 0 s ; 28 s ; 99 % (382) |
| T7 | 187 | 0 | 2 | 28 s ; 68 s ; 64 % (91) | 5 s ; 70 s ; 64 % (128) | 0 s ; 69 s ; 67 % (123) | 0 s ; 68 s ; 68 % (134) | 0 s ; 67 s ; 71 % (143) |
| T9 | 229 | 6 | 56 | 166 s ; 295 s ; 24 % (96) | 88 s ; 165 s ; 25 % (122) | 70 s ; 125 s ; 36 % (141) | 48 s ; 100 s ; 56 % (152) | 20 s ; 80 s ; 78 % (153) |

## Sens de l'écart (médiane signée)

| Ligne | 10 min avant | 5 min avant | 3 min avant | 2 min avant | 1 min avant |
|---|---|---|---|---|---|
| BUS_183 | +5 s | -22 s | -42 s | -28 s | +0 s |
| BUS_350 | +66 s | +123 s | +93 s | +36 s | +0 s |
| BUS_351 | -98 s | -65 s | +0 s | +0 s | +0 s |
| METRO_14 | +0 s | +0 s | +0 s | +0 s | +0 s |
| ORLYVAL | — | — | — | — | — |
| RER_B | +7 s | +0 s | +0 s | +0 s | +0 s |
| T7 | +0 s | +0 s | +0 s | +0 s | +0 s |
| T9 | +163 s | +88 s | +70 s | +48 s | +11 s |

## Âge de la mesure à la lecture (dans les 10 min avant le départ, par tranches de 5 s)

| Ligne | Annonces | Médiane | 9 sur 10 | < 15 s | < 30 s | < 60 s | < 120 s | < 300 s |
|---|---|---|---|---|---|---|---|---|
| BUS_183 | 1970 | 255 s | 900 s | 6 % | 16 % | 28 % | 39 % | 52 % |
| BUS_350 | 532 | 195 s | 900 s | 0 % | 1 % | 18 % | 40 % | 58 % |
| BUS_351 | 489 | 130 s | 545 s | 0 % | 3 % | 18 % | 45 % | 74 % |
| METRO_14 | 8711 | 345 s | 775 s | 2 % | 5 % | 10 % | 19 % | 43 % |
| ORLYVAL | 0 | — | — | — | — | — | — | — |
| RER_B | 7203 | 30 s | 100 s | 26 % | 47 % | 74 % | 92 % | 98 % |
| T7 | 3309 | 485 s | 900 s | 3 % | 5 % | 9 % | 16 % | 34 % |
| T9 | 3640 | 145 s | 900 s | 5 % | 12 % | 25 % | 44 % | 72 % |
