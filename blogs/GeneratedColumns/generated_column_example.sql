--
-- Remove table if it already exists..
--
DROP TABLE IF EXISTS gencol.bag_loading_events ;

--
-- Create new table. Note the three GENERATED columns: 'oversize' is derived
-- from the bag dimensions, 'reported_late' from how far conveyer_timestamp
-- lags the clock at insert time, and 'geo_location' from the
-- reported_location object.
CREATE TABLE gencol.bag_loading_events (
  bag_id TEXT NOT NULL,
  conveyer_timestamp TIMESTAMP NOT NULL,
  reported_location OBJECT(STRICT) AS(lat DOUBLE PRECISION,long DOUBLE PRECISION),
  bag_length_cm SMALLINT NOT NULL,
  bag_width_cm SMALLINT NOT NULL,
  bag_size_cm SMALLINT NOT NULL,
  oversize BOOLEAN GENERATED ALWAYS AS (
    CASE
      WHEN bag_length_cm > 55 OR bag_width_cm > 40 OR bag_size_cm > 20 THEN true
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
  PRIMARY KEY (bag_id,conveyer_timestamp)
);

--
-- Insert a bag that fits Ryanair's 55 x 40 x 20 cm cabin limit: 'oversize'
-- will be generated as false.
--
INSERT INTO gencol.bag_loading_events
  (bag_id, conveyer_timestamp, reported_location, bag_length_cm, bag_width_cm, bag_size_cm)
VALUES (
  'good_bag',
  NOW(),
  {lat = 51.4715, long = -0.4889},  -- inside LHR Terminal 5
  50,
  35,
  18
);

--
-- Insert a bag that exceeds the limit on every dimension: 'oversize'
-- will be generated as true.
--
INSERT INTO gencol.bag_loading_events
  (bag_id, conveyer_timestamp, reported_location, bag_length_cm, bag_width_cm, bag_size_cm)
VALUES (
  'bad_bag',
  NOW(),
  {lat = 51.4726, long = -0.4884},
  81,
  45,
  30
);

--
-- Insert a bag whose conveyer event is reported 10 minutes after the fact:
-- 'reported_late' will be generated as true.
--
INSERT INTO gencol.bag_loading_events
  (bag_id, conveyer_timestamp, reported_location, bag_length_cm, bag_width_cm, bag_size_cm)
VALUES (
  'late_bag',
  NOW() - INTERVAL '10' MINUTE,
  {lat = 51.4720, long = -0.4891},
  52,
  36,
  19
);

--
-- Make the freshly inserted rows visible to the query below.
--
REFRESH TABLE gencol.bag_loading_events;

--
-- See the generated columns at work: 'bad_bag' comes back with
-- oversize = true, 'good_bag' with oversize = false, and geo_location
-- is a geo_point derived from reported_location.
--
SELECT bag_id, oversize, reported_late, geo_location
FROM gencol.bag_loading_events
ORDER BY bag_id;

