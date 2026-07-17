--
-- Find where Stuttgart is...
--
SELECT region_name
FROM "demo"."german_regions"
WHERE WITHIN('POINT( 9.0120664 48.7793174)', geo_coords);

--
-- Find regions with Castles
--
SELECT region_name, _score
FROM "demo"."german_regions"
WHERE MATCH(tourism_info, 'castles')
ORDER BY _score DESC
LIMIT 3;

--
-- 10 closest towns to Stuttgart that are in a region known for
-- wine production.
--
WITH matched_region AS (
    SELECT region_name, geo_coords, _score
    FROM demo.german_regions
    WHERE MATCH(
            (tourism_info, transportation, economics, introduced_species),
            'wine vineyards'
          )
    ORDER BY _score DESC
    LIMIT 1                                -- top BM25 hit only
)
SELECT r.region_name,
       r._score,
       p.nearest_town,
       DISTANCE(p.geo_location, 'POINT(9.0120664 48.7793174)')::LONG AS distance_m
FROM matched_region r
JOIN demo.geo_points p
  ON WITHIN(p.geo_location, r.geo_coords)
ORDER BY r._score DESC,
         distance_m ASC
LIMIT 10;