-- Base d'horaires Airport Connect — schéma 1 (pas encore publié : il peut encore changer sans montée de version)
-- Fichier SQLite en lecture seule, produit côté serveur par tools/pipeline/build_timetable.py,
-- téléchargé par l'application (spec § 17). PRAGMA user_version = version du schéma.
-- Conventions :
--   * identifiants internes INTEGER compacts, attribués de façon déterministe (tri des identifiants GTFS) ;
--   * identifiants externes conservés tels quels (route_id, stop_id GTFS) pour PRIM/Navitia
--     (line:<route_id>, stop_point:<stop_id>, stop_area:<parent_station>) ;
--   * heures en secondes depuis « midi moins 12 h » du jour de service (norme GTFS), > 86 400 autorisé ;
--   * jours de service : bit i de service.days = actif le jour meta.base_date + i (fenêtre ≤ 62 jours).

PRAGMA user_version = 1;

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
  sector      TEXT NOT NULL CHECK (sector IN ('ROISSY','ORLY','BOURGET','BEAUVAIS')),
  label_fr    TEXT NOT NULL,
  label_en    TEXT NOT NULL,
  short_label TEXT NOT NULL,
  former_fr   TEXT,                                  -- ancien nom affiché en second (R-35), ex. « ex-Terminal 2E »
  former_en   TEXT,
  CHECK ((former_fr IS NULL) = (former_en IS NULL))
) WITHOUT ROWID;

CREATE TABLE place_stop_point (
  place_id      TEXT    NOT NULL REFERENCES place(id),
  stop_point_id INTEGER NOT NULL REFERENCES stop_point(id),
  PRIMARY KEY (place_id, stop_point_id)
) WITHOUT ROWID;
-- un rattachement par zone d'arrêt est développé en ses quais à la génération

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
