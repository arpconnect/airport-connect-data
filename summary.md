# Relevé d'exactitude du temps réel

13 veille(s), du 2026-11-10 22:08 au 2026-11-14 16:42 (heure de Paris). Requêtes : 12480, dont 1 sans réponse exploitable ; 0 veille(s) arrêtée(s) par un 429.

Le « départ » est la dernière annonce lue avant que la course quitte le quai, connue à 30 s près (intervalle de lecture) plus 25 s (partage du proxy). Écart positif : parti plus tard qu'annoncé.

## Écart entre l'annonce et le départ (médiane, 9 départs sur 10, part à 60 s près)

| Ligne | Courses | Jamais mesurées | Disparues | 10 min avant | 5 min avant | 3 min avant | 2 min avant | 1 min avant |
|---|---|---|---|---|---|---|---|---|
| BUS_183 | 34 | 0 | 2 | 57 s ; 145 s ; 53 % (15) | 48 s ; 123 s ; 62 % (24) | 50 s ; 123 s ; 62 % (24) | 40 s ; 72 s ; 73 % (22) | 5 s ; 67 s ; 83 % (24) |
| BUS_350 | 13 | 0 | 8 | 136 s ; 547 s ; 20 % (5) | 158 s ; 302 s ; 11 % (9) | 97 s ; 121 s ; 10 % (10) | 60 s ; 94 s ; 56 % (9) | 0 s ; 14 s ; 100 % (8) |
| BUS_351 | 12 | 1 | 4 | 199 s ; 664 s ; 33 % (3) | 106 s ; 362 s ; 12 % (8) | 62 s ; 262 s ; 50 % (6) | 65 s ; 181 s ; 43 % (7) | 0 s ; 161 s ; 57 % (7) |
| METRO_14 | 184 | 0 | 19 | 0 s ; 68 s ; 79 % (68) | 0 s ; 66 s ; 86 % (118) | 0 s ; 40 s ; 90 % (133) | 0 s ; 32 s ; 91 % (138) | 0 s ; 5 s ; 98 % (144) |
| ORLYVAL | 0 | 0 | 0 | — | — | — | — | — |
| RER_B | 131 | 0 | 18 | 28 s ; 163 s ; 77 % (66) | 20 s ; 66 s ; 89 % (94) | 10 s ; 46 s ; 99 % (105) | 14 s ; 57 s ; 92 % (108) | 0 s ; 16 s ; 100 % (112) |
| T7 | 61 | 0 | 2 | 16 s ; 66 s ; 72 % (32) | 0 s ; 67 s ; 72 % (39) | 0 s ; 70 s ; 71 % (38) | 0 s ; 68 s ; 68 % (44) | 0 s ; 64 s ; 79 % (47) |
| T9 | 82 | 1 | 9 | 192 s ; 279 s ; 15 % (34) | 110 s ; 188 s ; 11 % (38) | 80 s ; 135 s ; 26 % (50) | 64 s ; 109 s ; 45 % (49) | 27 s ; 72 s ; 80 % (51) |

## Sens de l'écart (médiane signée)

| Ligne | 10 min avant | 5 min avant | 3 min avant | 2 min avant | 1 min avant |
|---|---|---|---|---|---|
| BUS_183 | +5 s | -26 s | -46 s | -28 s | +0 s |
| BUS_350 | +136 s | +158 s | +97 s | +60 s | +0 s |
| BUS_351 | +10 s | -14 s | +0 s | +0 s | +0 s |
| METRO_14 | +0 s | +0 s | +0 s | +0 s | +0 s |
| ORLYVAL | — | — | — | — | — |
| RER_B | +4 s | +0 s | +0 s | -10 s | +0 s |
| T7 | +0 s | +0 s | +0 s | +0 s | +0 s |
| T9 | +192 s | +110 s | +80 s | +64 s | +27 s |

## Âge de la mesure à la lecture (dans les 10 min avant le départ, par tranches de 5 s)

| Ligne | Annonces | Médiane | 9 sur 10 | < 15 s | < 30 s | < 60 s | < 120 s | < 300 s |
|---|---|---|---|---|---|---|---|---|
| BUS_183 | 609 | 275 s | 900 s | 11 % | 15 % | 26 % | 36 % | 52 % |
| BUS_350 | 182 | 115 s | 900 s | 0 % | 1 % | 21 % | 50 % | 65 % |
| BUS_351 | 195 | 115 s | 480 s | 1 % | 4 % | 23 % | 51 % | 78 % |
| METRO_14 | 2910 | 335 s | 740 s | 2 % | 5 % | 10 % | 19 % | 45 % |
| ORLYVAL | 0 | — | — | — | — | — | — | — |
| RER_B | 2156 | 30 s | 105 s | 25 % | 47 % | 75 % | 91 % | 98 % |
| T7 | 1077 | 470 s | 900 s | 4 % | 7 % | 10 % | 16 % | 33 % |
| T9 | 1317 | 125 s | 900 s | 4 % | 14 % | 28 % | 49 % | 79 % |
