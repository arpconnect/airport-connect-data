-- Base d'horaires Airport Connect — schéma 3
-- Fichier SQLite en lecture seule, produit côté serveur par tools/pipeline/build_timetable.py,
-- téléchargé par l'application (spec § 17). PRAGMA user_version = version du schéma.
-- Historique : 1 (8 octobre 2026), première publication ; 2 (8 octobre 2026), lieu « ville » (Paris) et ses
-- points de montée (place.kind, place.sort_order, place_boarding ; spec R-100, décision D-13) ; 3 (9 octobre 2026),
-- correspondances officielles entre quais (transfer, GTFS transfers.txt) pour les itinéraires hors connexion (R-119).
-- L'application lit exactement une version : une base d'une autre version n'est jamais adoptée (ADR-2).
-- Conventions :
--   * identifiants internes INTEGER compacts, attribués de façon déterministe (tri des identifiants GTFS) ;
--   * identifiants externes conservés tels quels (route_id, stop_id GTFS) pour PRIM/Navitia
--     (line:<route_id>, stop_point:<stop_id>, stop_area:<parent_station>) ;
--   * heures en secondes depuis « midi moins 12 h » du jour de service (norme GTFS), > 86 400 autorisé ;
--   * jours de service : bit i de service.days = actif le jour meta.base_date + i (fenêtre ≤ 62 jours).

PRAGMA user_version = 3;

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;
-- clés obligatoires : schema_version, base_date (AAAA-MM-JJ), validity_start, validity_end,
-- source_sha256, source_url, timezone

CREATE TABLE line (
  id           INTEGER PRIMARY KEY,
  code         TEXT    NOT NULL UNIQUE,              -- identifiant applicatif stable (RER_B, BUS_N140…)
  route_id     TEXT    UNIQUE,                       -- code C IDFM (IDFM:C01743) ; NULL pour une ligne future
  display_name TEXT    NOT NULL,
  short_name   TEXT,                                 -- route_short_name du GTFS courant (R-14)
  long_name    TEXT,
  app_type     TEXT    NOT NULL CHECK (app_type IN ('SHUTTLE','TRAIN','METRO','BUS','TRAM','TRAM_TRAIN','NIGHT_BUS')),
  category     TEXT    NOT NULL CHECK (category IN ('RAIL','SURFACE')),
  network      TEXT,                                 -- agency_name du GTFS
  color        TEXT,                                 -- #RRGGBB, route_color (R-13)
  text_color   TEXT,
  state        TEXT    NOT NULL CHECK (state IN ('active','future')),
  group_code   TEXT,                                 -- 'NOCTILIEN' pour les 7 branches (R-01)
  CHECK ((state = 'active') = (route_id IS NOT NULL))
);

CREATE TABLE line_sector (
  line_id    INTEGER NOT NULL REFERENCES line(id),
  sector     TEXT    NOT NULL CHECK (sector IN ('ROISSY','ORLY','BOURGET','BEAUVAIS')),
  sort_order INTEGER,
  shortcut   INTEGER NOT NULL DEFAULT 0 CHECK (shortcut IN (0,1)),
  PRIMARY KEY (line_id, sector)
) WITHOUT ROWID;

CREATE TABLE line_future (
  line_id    INTEGER PRIMARY KEY REFERENCES line(id),
  opening    TEXT NOT NULL,
  scope      TEXT,
  source_url TEXT NOT NULL,
  checked_on TEXT NOT NULL
);

CREATE TABLE stop_area (
  id      INTEGER PRIMARY KEY,
  gtfs_id TEXT NOT NULL UNIQUE,                      -- parent_station GTFS
  name    TEXT NOT NULL,
  lat     REAL NOT NULL,
  lon     REAL NOT NULL
);

CREATE TABLE stop_point (
  id           INTEGER PRIMARY KEY,
  gtfs_id      TEXT    NOT NULL UNIQUE,              -- stop_id GTFS
  stop_area_id INTEGER NOT NULL REFERENCES stop_area(id),
  name         TEXT    NOT NULL,
  lat          REAL    NOT NULL,
  lon          REAL    NOT NULL
);
CREATE INDEX stop_point_area ON stop_point(stop_area_id);

CREATE TABLE place (
  id          TEXT PRIMARY KEY,
  parent_id   TEXT REFERENCES place(id),
  kind        TEXT NOT NULL CHECK (kind IN ('airport','city')),
  sector      TEXT CHECK (sector IN ('ROISSY','ORLY','BOURGET','BEAUVAIS')),   -- NULL pour un lieu « ville »
  sort_order  INTEGER NOT NULL UNIQUE,               -- ordre du sélecteur de l'accueil (ordre de data/places.json)
  label_fr    TEXT NOT NULL,
  label_en    TEXT NOT NULL,
  short_label TEXT NOT NULL,
  former_fr   TEXT,                                  -- ancien nom affiché en second (R-35), ex. « ex-Terminal 2E »
  former_en   TEXT,
  CHECK ((kind = 'airport') = (sector IS NOT NULL)),
  CHECK ((former_fr IS NULL) = (former_en IS NULL))
) WITHOUT ROWID;

CREATE TABLE place_stop_point (
  place_id      TEXT    NOT NULL REFERENCES place(id),
  stop_point_id INTEGER NOT NULL REFERENCES stop_point(id),
  PRIMARY KEY (place_id, stop_point_id)
) WITHOUT ROWID;
-- un rattachement par zone d'arrêt est développé en ses quais à la génération

-- Lieu « ville » (Paris) : un point de montée par ligne (spec R-100, décision D-13). Les départs retenus à
-- l'accueil sont ceux qui desservent ensuite, en descente autorisée, un quai d'un lieu aéroportuaire.
CREATE TABLE place_boarding (
  place_id     TEXT    NOT NULL REFERENCES place(id),
  line_id      INTEGER NOT NULL REFERENCES line(id),
  stop_area_id INTEGER NOT NULL REFERENCES stop_area(id),
  PRIMARY KEY (place_id, line_id)
) WITHOUT ROWID;

CREATE TABLE service (
  id   INTEGER PRIMARY KEY,
  days INTEGER NOT NULL                              -- masque de bits relatif à meta.base_date
);

CREATE TABLE pattern (
  id        INTEGER PRIMARY KEY,
  line_id   INTEGER NOT NULL REFERENCES line(id),
  direction INTEGER,                                 -- direction_id GTFS (0/1) ou NULL
  headsign  TEXT    NOT NULL
);
CREATE INDEX pattern_line ON pattern(line_id);

CREATE TABLE pattern_stop (
  pattern_id    INTEGER NOT NULL REFERENCES pattern(id),
  seq           INTEGER NOT NULL,                    -- 0..n-1, renuméroté
  stop_point_id INTEGER NOT NULL REFERENCES stop_point(id),
  pickup        INTEGER NOT NULL,                    -- pickup_type GTFS (1 = montée interdite, R-32)
  dropoff       INTEGER NOT NULL,                    -- drop_off_type GTFS
  PRIMARY KEY (pattern_id, seq)
) WITHOUT ROWID;
CREATE INDEX pattern_stop_point ON pattern_stop(stop_point_id, pattern_id, seq);

CREATE TABLE trip (
  id         INTEGER PRIMARY KEY,
  pattern_id INTEGER NOT NULL REFERENCES pattern(id),
  service_id INTEGER NOT NULL REFERENCES service(id)
);
CREATE INDEX trip_pattern ON trip(pattern_id, service_id);

CREATE TABLE stop_time (
  trip_id   INTEGER NOT NULL REFERENCES trip(id),
  seq       INTEGER NOT NULL,
  departure INTEGER NOT NULL,                        -- secondes depuis midi − 12 h
  dwell     INTEGER NOT NULL DEFAULT 0,              -- departure − arrival (≥ 0)
  PRIMARY KEY (trip_id, seq)
) WITHOUT ROWID;

-- Correspondances entre quais de la base (spec R-119) : lignes de transfers.txt du GTFS IDFM de type 2 (temps
-- minimal de correspondance, marche comprise), entre deux quais différents présents dans la base. Ce sont les temps
-- publiés par IDFM, utilisés tels quels ; aucune distance n'est estimée.
CREATE TABLE transfer (
  from_stop_point_id INTEGER NOT NULL REFERENCES stop_point(id),
  to_stop_point_id   INTEGER NOT NULL REFERENCES stop_point(id),
  min_time           INTEGER NOT NULL CHECK (min_time >= 0),   -- secondes (min_transfer_time)
  PRIMARY KEY (from_stop_point_id, to_stop_point_id),
  CHECK (from_stop_point_id != to_stop_point_id)
) WITHOUT ROWID;

-- Aménagements durables de desserte (spec R-90 à R-93), seules les entrées publiées.
CREATE TABLE adjustment (
  id          TEXT PRIMARY KEY,
  line_id     INTEGER NOT NULL REFERENCES line(id),
  kind        TEXT NOT NULL CHECK (kind IN ('temporary','permanent')),
  title_fr    TEXT NOT NULL,
  summary_fr  TEXT NOT NULL,
  valid_from  TEXT,                                  -- AAAA-MM-JJ ; NULL = déjà en vigueur, début non documenté
  valid_until TEXT,                                  -- AAAA-MM-JJ inclus ; NULL = sans fin connue
  source_url  TEXT NOT NULL,
  checked_on  TEXT NOT NULL,
  gtfs_lagging_trips INTEGER NOT NULL                -- trajets du GTFS qui desservent encore un quai non desservi (R-92)
) WITHOUT ROWID;

-- Quais qui ne sont plus desservis par la ligne de l'aménagement.
CREATE TABLE adjustment_stop (
  adjustment_id         TEXT    NOT NULL REFERENCES adjustment(id),
  stop_point_id         INTEGER NOT NULL REFERENCES stop_point(id),
  direction_label       TEXT    NOT NULL,
  replacement_area_id   INTEGER REFERENCES stop_area(id),
  replacement_label     TEXT,                        -- nom affiché du report, tel que sur le plan de ligne
  distance_m            INTEGER,                     -- vol d'oiseau quai → zone de report, arrondi à 10 m
  PRIMARY KEY (adjustment_id, stop_point_id)
) WITHOUT ROWID;

-- Quais de l'itinéraire en vigueur contournés par des missions « en retard » du GTFS (missions qui
-- desservent encore un quai non desservi, et aucun quai de la zone de ce quai). Tant que ces missions
-- circulent, les horaires théoriques du quai sont incomplets (R-92).
CREATE TABLE adjustment_gap (
  adjustment_id  TEXT    NOT NULL REFERENCES adjustment(id),
  stop_point_id  INTEGER NOT NULL REFERENCES stop_point(id),
  pattern_id     INTEGER NOT NULL REFERENCES pattern(id),
  PRIMARY KEY (adjustment_id, stop_point_id, pattern_id)
) WITHOUT ROWID;
