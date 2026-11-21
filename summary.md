# Relevé d'exactitude du temps réel

33 veille(s), du 2026-11-10 22:08 au 2026-11-21 22:05 (heure de Paris). Requêtes : 31680, dont 2 sans réponse exploitable ; 0 veille(s) arrêtée(s) par un 429.

Le « départ » est la dernière annonce lue avant que la course quitte le quai, connue à 30 s près (intervalle de lecture) plus 25 s (partage du proxy). Écart positif : parti plus tard qu'annoncé.

## Écart entre l'annonce et le départ (médiane, 9 départs sur 10, part à 60 s près)

| Ligne | Courses | Jamais mesurées | Disparues | 10 min avant | 5 min avant | 3 min avant | 2 min avant | 1 min avant |
|---|---|---|---|---|---|---|---|---|
| BUS_183 | 84 | 0 | 5 | 58 s ; 147 s ; 55 % (40) | 48 s ; 169 s ; 61 % (56) | 50 s ; 121 s ; 59 % (59) | 34 s ; 73 s ; 77 % (57) | 5 s ; 61 s ; 89 % (57) |
| BUS_350 | 32 | 1 | 16 | 48 s ; 232 s ; 50 % (10) | 139 s ; 241 s ; 32 % (19) | 94 s ; 123 s ; 24 % (21) | 18 s ; 94 s ; 61 % (18) | 0 s ; 33 s ; 100 % (19) |
| BUS_351 | 25 | 2 | 6 | 199 s ; 664 s ; 20 % (5) | 142 s ; 362 s ; 8 % (13) | 90 s ; 262 s ; 25 % (12) | 71 s ; 181 s ; 23 % (13) | 70 s ; 142 s ; 38 % (13) |
| METRO_14 | 454 | 0 | 47 | 0 s ; 66 s ; 84 % (160) | 0 s ; 62 s ; 89 % (295) | 0 s ; 34 s ; 91 % (341) | 0 s ; 28 s ; 93 % (356) | 0 s ; 0 s ; 98 % (363) |
| ORLYVAL | 0 | 0 | 0 | — | — | — | — | — |
| RER_B | 335 | 0 | 54 | 27 s ; 126 s ; 76 % (177) | 17 s ; 84 s ; 87 % (240) | 9 s ; 43 s ; 97 % (263) | 8 s ; 50 s ; 92 % (280) | 0 s ; 28 s ; 99 % (288) |
| T7 | 148 | 0 | 2 | 49 s ; 68 s ; 60 % (72) | 4 s ; 70 s ; 62 % (99) | 0 s ; 68 s ; 65 % (93) | 0 s ; 68 s ; 66 % (104) | 0 s ; 66 s ; 71 % (115) |
| T9 | 189 | 4 | 36 | 163 s ; 295 s ; 24 % (82) | 89 s ; 161 s ; 26 % (101) | 71 s ; 133 s ; 36 % (120) | 52 s ; 101 s ; 56 % (129) | 22 s ; 85 s ; 78 % (130) |

## Sens de l'écart (médiane signée)

| Ligne | 10 min avant | 5 min avant | 3 min avant | 2 min avant | 1 min avant |
|---|---|---|---|---|---|
| BUS_183 | +2 s | -12 s | -45 s | -31 s | +0 s |
| BUS_350 | +48 s | +139 s | +94 s | +18 s | +0 s |
| BUS_351 | -194 s | -65 s | +0 s | +0 s | +0 s |
| METRO_14 | +0 s | +0 s | +0 s | +0 s | +0 s |
| ORLYVAL | — | — | — | — | — |
| RER_B | +9 s | +0 s | +0 s | +0 s | +0 s |
| T7 | +0 s | +0 s | +0 s | +0 s | +0 s |
| T9 | +162 s | +89 s | +71 s | +52 s | +17 s |

## Âge de la mesure à la lecture (dans les 10 min avant le départ, par tranches de 5 s)

| Ligne | Annonces | Médiane | 9 sur 10 | < 15 s | < 30 s | < 60 s | < 120 s | < 300 s |
|---|---|---|---|---|---|---|---|---|
| BUS_183 | 1523 | 240 s | 900 s | 7 % | 16 % | 28 % | 40 % | 54 % |
| BUS_350 | 401 | 235 s | 900 s | 0 % | 1 % | 20 % | 41 % | 55 % |
| BUS_351 | 346 | 115 s | 415 s | 0 % | 3 % | 20 % | 52 % | 79 % |
| METRO_14 | 7090 | 360 s | 775 s | 2 % | 4 % | 9 % | 18 % | 42 % |
| ORLYVAL | 0 | — | — | — | — | — | — | — |
| RER_B | 5501 | 30 s | 105 s | 26 % | 47 % | 74 % | 92 % | 98 % |
| T7 | 2608 | 515 s | 900 s | 3 % | 5 % | 9 % | 14 % | 31 % |
| T9 | 3084 | 150 s | 900 s | 5 % | 12 % | 25 % | 43 % | 70 % |
