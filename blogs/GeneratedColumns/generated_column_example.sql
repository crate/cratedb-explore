--
-- Remove table if it already exists..
--
DROP TABLE IF EXISTS gencol.bag_loading_events ;

--
-- Create new table. Note the five GENERATED columns: 'oversize' is derived
-- from the bag dimensions, 'reported_late' from how far conveyer_timestamp
-- lags the clock at insert time, 'geo_location' from the reported_location
-- object, 'in_t5' from an insert-time geofence check against the Terminal 5
-- footprint, and 'event_week' — which is also the PARTITIONED BY key, so
-- rows route themselves into weekly partitions.
CREATE TABLE gencol.bag_loading_events (
  bag_id TEXT NOT NULL,
  conveyer_timestamp TIMESTAMP NOT NULL,
  reported_location OBJECT(STRICT) AS(lat DOUBLE PRECISION,long DOUBLE PRECISION),
  bag_length_cm SMALLINT NOT NULL,
  bag_width_cm SMALLINT NOT NULL,
  bag_height_cm SMALLINT NOT NULL,
  oversize BOOLEAN GENERATED ALWAYS AS (
    CASE
      WHEN bag_length_cm > 55 OR bag_width_cm > 40 OR bag_height_cm > 20 THEN true
      ELSE false
    END
  ),
  reported_late BOOLEAN GENERATED ALWAYS AS (
    CASE
      WHEN conveyer_timestamp < CURRENT_TIMESTAMP - INTERVAL '5' MINUTE THEN true
      ELSE false
    END
  ),
  geo_location GEO_POINT GENERATED ALWAYS AS [reported_location['long'], reported_location['lat']],
  -- Pay the geofence check once at write time instead of on every query.
  -- (A generated column may not reference another generated column, so this
  -- builds its own geo_point from reported_location rather than reusing
  -- geo_location.)
  in_t5 BOOLEAN GENERATED ALWAYS AS within(
    CAST([reported_location['long'], reported_location['lat']] AS GEO_POINT),
    'POLYGON ((-0.4930 51.4695, -0.4845 51.4695, -0.4845 51.4745, -0.4930 51.4745, -0.4930 51.4695))'
  ),
  -- The partition key is generated too: rows route themselves into weekly
  -- partitions the application never has to know about.
  event_week TIMESTAMP GENERATED ALWAYS AS date_trunc('week', conveyer_timestamp),
  PRIMARY KEY (bag_id, conveyer_timestamp, event_week)
) PARTITIONED BY (event_week);

--
-- Insert a bag that fits Ryanair's 55 x 40 x 20 cm cabin limit: 'oversize'
-- will be generated as false.
--
INSERT INTO gencol.bag_loading_events
  (bag_id, conveyer_timestamp, reported_location, bag_length_cm, bag_width_cm, bag_height_cm)
VALUES ('good_bag', NOW(), {lat = 51.4715, long = -0.4889}, 50, 35, 18);  -- inside LHR Terminal 5

--
-- Insert a bag that exceeds the limit on every dimension: 'oversize'
-- will be generated as true.
--
INSERT INTO gencol.bag_loading_events
  (bag_id, conveyer_timestamp, reported_location, bag_length_cm, bag_width_cm, bag_height_cm)
VALUES ('bad_bag', NOW(), {lat = 51.4726, long = -0.4884}, 81, 45, 30);

--
-- Insert a bag whose conveyer event is reported 10 minutes after the fact:
-- 'reported_late' will be generated as true.
--
INSERT INTO gencol.bag_loading_events
  (bag_id, conveyer_timestamp, reported_location, bag_length_cm, bag_width_cm, bag_height_cm)
VALUES ('late_bag', NOW() - INTERVAL '10' MINUTE, {lat = 51.4720, long = -0.4891}, 52, 36, 19);

--
-- Insert a bag scanned over by Terminal 2: 'in_t5' will be generated
-- as false.
--
INSERT INTO gencol.bag_loading_events
  (bag_id, conveyer_timestamp, reported_location, bag_length_cm, bag_width_cm, bag_height_cm)
VALUES ('stray_bag', NOW(), {lat = 51.4700, long = -0.4520}, 48, 33, 17);

--
-- Make the freshly inserted rows visible to the query below.
--
REFRESH TABLE gencol.bag_loading_events;

--
-- See the generated columns at work: 'bad_bag' is oversize, 'late_bag' was
-- reported late, 'stray_bag' is outside the Terminal 5 geofence, and every
-- row carries its derived geo_location and event_week.
--
SELECT bag_id, oversize, reported_late, in_t5, event_week, geo_location
FROM gencol.bag_loading_events
ORDER BY bag_id;

--
-- The generated event_week column doubles as the partition key — CrateDB
-- created this week's partition automatically, and old weeks can be
-- dropped wholesale instead of deleted row by row.
--
SELECT table_name, values
FROM information_schema.table_partitions
WHERE table_schema = 'gencol' AND table_name = 'bag_loading_events';

