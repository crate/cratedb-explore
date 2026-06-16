-- Row count + device count. REFRESH makes just-written rows visible (CrateDB's
-- table refresh_interval defaults to 1s). A healthy load shows the full row
-- count AND >1 distinct device — if device count is 1 (or device_id is null),
-- the tags aren't reaching the metric (see "Reliability & gotchas").
REFRESH TABLE rtia.iot_data;
SELECT COUNT(*)                          AS rows,
       COUNT(DISTINCT tags['device_id']) AS devices
FROM rtia.iot_data;

-- Records per device
SELECT tags['device_id'] AS device_id, tags['device_type'] AS device_type,
       COUNT(*) AS readings
FROM rtia.iot_data
GROUP BY tags['device_id'], tags['device_type']
ORDER BY readings DESC
LIMIT 10;

-- Latest reading per device
 SELECT device_id, last_seen, status AS last_status
 FROM (
    SELECT tags['device_id'] AS device_id,
           "timestamp"        AS last_seen,
           tags['status']     AS status,
              ROW_NUMBER() OVER (PARTITION BY tags['device_id']
                              ORDER BY "timestamp" DESC) AS rn
    FROM rtia.iot_data
  ) t
  WHERE rn = 1
  ORDER BY last_seen DESC
  LIMIT 10;


-- Geo works on Telegraf-ingested rows: geo_location is GENERATED from
-- fields['geo_lon'] / fields['geo_lat'], so DISTANCE() queries run directly.
SELECT tags['device_id'] AS device_id, geo_location,
       DISTANCE(geo_location, [9.1819, 48.7843]) AS metres_from_stuttgart
FROM rtia.iot_data
WHERE geo_location IS NOT NULL
ORDER BY metres_from_stuttgart
LIMIT 5;