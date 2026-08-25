--
-- Remove table if it already exists..
--
DROP TABLE IF EXISTS gencol.bag_loading_events ;

--
-- Create new table. Note the five GENERATED columns: 'oversize' is derived
-- from the bag dimensions, 'reported_late' from how far conveyor_timestamp
-- lags the moment the pipeline handed us the row, 'geo_location' from the
-- reported_location object, 'in_t5' from a write-time geofence check against
-- the Terminal 5 footprint, and 'event_week' — which is also the
-- PARTITIONED BY key, so rows route themselves into weekly partitions.
--
-- Every timestamp is WITH TIME ZONE. Heathrow spends half the year in BST
-- and half in GMT, and a bare TIMESTAMP (which means WITHOUT TIME ZONE)
-- would leave the comparisons below relying on an implicit cast.
--
CREATE TABLE gencol.bag_loading_events (
  bag_id TEXT NOT NULL,
  conveyor_timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
  -- Supplied by the ingest pipeline, not by the database. This is what makes
  -- 'reported_late' below a deterministic expression — see the note there.
  ingest_timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
  -- NOT NULL matters: a missing location would put [NULL, NULL] into the
  -- GEO_POINT cast used by 'geo_location' and 'in_t5'. Real scanner feeds
  -- drop GPS regularly, so either enforce it here or make the generation
  -- expressions NULL-safe.
  reported_location OBJECT(STRICT) AS(lat DOUBLE PRECISION,long DOUBLE PRECISION) NOT NULL,
  bag_length_cm SMALLINT NOT NULL,
  bag_width_cm SMALLINT NOT NULL,
  bag_height_cm SMALLINT NOT NULL,
  -- Ryanair's checked-bag limit is 119 x 119 x 81 cm. Anything beyond it
  -- can't take the standard belt and has to be diverted to out-of-gauge
  -- handling, so this is a flag the sortation system genuinely acts on.
  oversize BOOLEAN GENERATED ALWAYS AS (
    bag_length_cm > 119 OR bag_width_cm > 119 OR bag_height_cm > 81
  ),
  -- Deterministic: both operands are base columns of this row. Had we
  -- written CURRENT_TIMESTAMP here instead of ingest_timestamp, the column
  -- would be non-deterministic — CrateDB would skip validating a supplied
  -- value, and a later UPDATE to an unrelated column could re-evaluate the
  -- expression against the clock at that moment, silently flipping a stored
  -- false to true. A column that reads as an immutable fact about ingest
  -- should not be able to change its mind months later.
  reported_late BOOLEAN GENERATED ALWAYS AS (
    conveyor_timestamp < ingest_timestamp - INTERVAL '5' MINUTE
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
  event_week TIMESTAMP WITH TIME ZONE GENERATED ALWAYS AS date_trunc('week', conveyor_timestamp),
  -- event_week looks redundant in the primary key, since it is functionally
  -- dependent on conveyor_timestamp. It isn't optional: CrateDB requires
  -- every PARTITIONED BY column to be part of the primary key.
  PRIMARY KEY (bag_id, conveyor_timestamp, event_week)
) PARTITIONED BY (event_week);

--
-- Insert a bag inside the checked-baggage limit: 'oversize' will be
-- generated as false.
--
INSERT INTO gencol.bag_loading_events
  (bag_id, conveyor_timestamp, ingest_timestamp, reported_location, bag_length_cm, bag_width_cm, bag_height_cm)
VALUES ('good_bag', NOW(), NOW(), {lat = 51.4715, long = -0.4889}, 75, 50, 30);  -- inside LHR Terminal 5

--
-- Insert a bag that exceeds the limit on every dimension: 'oversize'
-- will be generated as true.
--
INSERT INTO gencol.bag_loading_events
  (bag_id, conveyor_timestamp, ingest_timestamp, reported_location, bag_length_cm, bag_width_cm, bag_height_cm)
VALUES ('bad_bag', NOW(), NOW(), {lat = 51.4726, long = -0.4884}, 130, 125, 90);

--
-- Insert a bag whose conveyor event reaches us 10 minutes after the fact:
-- 'reported_late' will be generated as true.
--
INSERT INTO gencol.bag_loading_events
  (bag_id, conveyor_timestamp, ingest_timestamp, reported_location, bag_length_cm, bag_width_cm, bag_height_cm)
VALUES ('late_bag', NOW() - INTERVAL '10' MINUTE, NOW(), {lat = 51.4720, long = -0.4891}, 72, 48, 29);

--
-- Insert a bag scanned over by Terminal 2: 'in_t5' will be generated
-- as false.
--
INSERT INTO gencol.bag_loading_events
  (bag_id, conveyor_timestamp, ingest_timestamp, reported_location, bag_length_cm, bag_width_cm, bag_height_cm)
VALUES ('stray_bag', NOW(), NOW(), {lat = 51.4700, long = -0.4520}, 68, 45, 27);

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

--
-- And the point of all that: the optimizer treats the generated partition
-- column exactly like a native one. Filtering on event_week prunes to a
-- single partition instead of scanning every week the table holds.
--
EXPLAIN
SELECT COUNT(*)
FROM gencol.bag_loading_events
WHERE event_week = date_trunc('week', NOW());

--
-- One caveat worth knowing before you point a bulk pipeline at this table.
--
-- On INSERT and UPDATE, a supplied value for a generated column is
-- validated: give CrateDB something that doesn't match the generation
-- expression and it raises SQLParseException telling you the value it
-- expected. COPY FROM does not do this. It computes the value only when
-- the column is absent from the imported data, and trusts whatever is
-- there when it isn't.
--
-- So a CSV or JSON export carrying a stale 'oversize' column will load
-- that stale value straight into a table that looks, from the DDL, like
-- it couldn't possibly hold one. The same applies to any ingest path that
-- supplies all mapped fields explicitly — a Kafka Connect JDBC sink, for
-- instance. The fix is to strip generated columns from the payload rather
-- than rely on the database to catch them.
--
-- The following would fail, as it should:
--
-- INSERT INTO gencol.bag_loading_events
--   (bag_id, conveyor_timestamp, ingest_timestamp, reported_location,
--    bag_length_cm, bag_width_cm, bag_height_cm, oversize)
-- VALUES ('liar_bag', NOW(), NOW(), {lat = 51.4715, long = -0.4889}, 130, 125, 90, false);
--
