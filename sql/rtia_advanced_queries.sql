--
-- Licensed to Crate.io GmbH ("Crate") under one or more contributor
-- license agreements.  See the NOTICE file distributed with this work for
-- additional information regarding copyright ownership.  Crate licenses
-- this file to you under the Apache License, Version 2.0 (the "License");
-- you may not use this file except in compliance with the License.  You may
-- obtain a copy of the License at
--
--   http://www.apache.org/licenses/LICENSE-2.0
--
-- Unless required by applicable law or agreed to in writing, software
-- distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
-- WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  See the
-- License for the specific language governing permissions and limitations
-- under the License.
--
-- However, if you have executed another commercial license agreement
-- with Crate these terms will supersede the license and you may use the
-- software solely pursuant to the terms of the relevant commercial agreement.
-- ─────────────────────────────────────────────────────────────────────────────
-- CrateDB Industrial IoT — Advanced Queries
-- Full-text search and geospatial capabilities
-- Tables:  iot_data · plants · devices · maintenance_log
-- ─────────────────────────────────────────────────────────────────────────────


-- ─────────────────────────────────────────────────────────────────────────────
-- ANALYTICAL PATTERNS
-- CTE, rate-of-change, and sustained-fault detection — patterns demonstrated
-- in the predictive maintenance and SQL vs. Flux blog posts.
-- ─────────────────────────────────────────────────────────────────────────────

-- ── 1. CTE: DETECT DEVICES WITH RISING METRIC VALUES ─────────────────────────
-- Compare each device's average reading across two consecutive 6-hour windows.
-- Flags devices where the recent window is >10% higher than the prior window —
-- the early-drift signature used in predictive maintenance workflows.

SELECT
      device_id,
      device_type,
      plant_id,
      ROUND(avg_prior,  2) AS avg_prior_6h,
      ROUND(avg_recent, 2) AS avg_recent_6h,
      ROUND(avg_recent - avg_prior, 2) AS absolute_delta,
      ROUND((avg_recent - avg_prior) / NULLIF(avg_prior, 0) * 100, 1) AS pct_change
  FROM (
      SELECT
          device_id,
          device_type,
          plant_id,
          AVG(CASE WHEN "timestamp" >= TIMESTAMP '2025-09-07 06:00:00'
                    AND "timestamp" <  TIMESTAMP '2025-09-07 12:00:00'
                   THEN metric_value END) AS avg_recent,
          AVG(CASE WHEN "timestamp" >= TIMESTAMP '2025-09-07 00:00:00'
                    AND "timestamp" <  TIMESTAMP '2025-09-07 06:00:00'
                   THEN metric_value END) AS avg_prior
      FROM rtia.iot_data
      WHERE "timestamp" >= TIMESTAMP '2025-09-07 00:00:00'
        AND "timestamp" <  TIMESTAMP '2025-09-07 12:00:00'
      GROUP BY device_id, device_type, plant_id
  ) windows
  WHERE avg_prior IS NOT NULL
    AND (avg_recent - avg_prior) / NULLIF(avg_prior, 0) > 0.10
  ORDER BY pct_change DESC
  LIMIT 20


-- not good

WITH windows AS (
    SELECT
        device_id,
        device_type,
        plant_id,
        AVG(metric_value) FILTER (
            WHERE "timestamp" >= TIMESTAMP '2025-09-07 06:00:00'
              AND "timestamp" <  TIMESTAMP '2025-09-07 12:00:00'
        ) AS avg_recent,
        AVG(metric_value) FILTER (
            WHERE "timestamp" >= TIMESTAMP '2025-09-07 00:00:00'
              AND "timestamp" <  TIMESTAMP '2025-09-07 06:00:00'
        ) AS avg_prior
    FROM rtia.iot_data
    WHERE "timestamp" >= TIMESTAMP '2025-09-07 00:00:00'
      AND "timestamp" <  TIMESTAMP '2025-09-07 12:00:00'
    GROUP BY device_id, device_type, plant_id
)
SELECT
    device_id,
    device_type,
    plant_id,
    ROUND(avg_prior,  2) AS avg_prior_6h,
    ROUND(avg_recent, 2) AS avg_recent_6h,
    ROUND(avg_recent - avg_prior, 2) AS absolute_delta,
    ROUND((avg_recent - avg_prior) / NULLIF(avg_prior, 0) * 100, 1) AS pct_change
FROM windows
WHERE avg_prior IS NOT NULL
  AND (avg_recent - avg_prior) / NULLIF(avg_prior, 0) > 0.10
ORDER BY pct_change DESC
LIMIT 20;


-- ── 2. SUSTAINED FAULT DETECTION WITH HAVING ─────────────────────────────────
-- Find devices that breached warning threshold at least 10 times in a single
-- day — distinguishing sustained degradation from transient noise spikes.

SELECT
    device_id,
    device_type,
    plant_id,
    COUNT(*)                    AS threshold_exceedances,
    ROUND(MAX(metric_value), 2) AS peak_value,
    ROUND(MIN(quality_score), 1) AS lowest_quality,
    MIN("timestamp")            AS first_breach_at
FROM rtia.iot_data
WHERE "timestamp" >= TIMESTAMP '2025-09-07 00:00:00'
  AND "timestamp" <  TIMESTAMP '2025-09-08 00:00:00'
  AND status IN ('warning', 'critical')
GROUP BY device_id, device_type, plant_id
HAVING COUNT(*) >= 10
ORDER BY threshold_exceedances DESC;


-- ── 3. NULLIF: QUALITY DEVIATION AS PERCENTAGE OF BASELINE ───────────────────
-- Compute how far each device's current quality score deviates from its own
-- all-time average. NULLIF guards against division by zero on devices with no
-- historical baseline.

SELECT
    device_id,
    device_type,
    plant_id,
    ROUND(AVG(quality_score) FILTER (
        WHERE "timestamp" < TIMESTAMP '2025-09-07 00:00:00'
    ), 1) AS baseline_quality,
    ROUND(AVG(quality_score) FILTER (
        WHERE "timestamp" >= TIMESTAMP '2025-09-07 00:00:00'
    ), 1) AS recent_quality,
    ROUND(
        (
            AVG(quality_score) FILTER (WHERE "timestamp" >= TIMESTAMP '2025-09-07 00:00:00')
            - AVG(quality_score) FILTER (WHERE "timestamp" < TIMESTAMP '2025-09-07 00:00:00')
        )
        / NULLIF(
            AVG(quality_score) FILTER (WHERE "timestamp" < TIMESTAMP '2025-09-07 00:00:00'),
            0
          ) * 100,
        1
    ) AS quality_change_pct
FROM rtia.iot_data
GROUP BY device_id, device_type, plant_id
HAVING AVG(quality_score) FILTER (WHERE "timestamp" < TIMESTAMP '2025-09-07 00:00:00') IS NOT NULL
   AND AVG(quality_score) FILTER (WHERE "timestamp" >= TIMESTAMP '2025-09-07 00:00:00') IS NOT NULL
ORDER BY quality_change_pct ASC
LIMIT 20;


-- ─────────────────────────────────────────────────────────────────────────────
-- FULL-TEXT SEARCH
-- Uses the FULLTEXT index on maintenance_log.notes (standard analyzer).
-- MATCH() scores results by relevance; combine with filters to narrow the set.
-- ─────────────────────────────────────────────────────────────────────────────

-- ── 1. FULL-TEXT: FIND MAINTENANCE RECORDS BY FAULT KEYWORD ──────────────────
-- Locate all work orders that mention calibration in the technician notes

SELECT
    work_order_id,
    device_id,
    maintenance_type,
    technician,
    completed_date,
    notes
FROM rtia.maintenance_log
WHERE MATCH(notes, 'calibration')
ORDER BY completed_date DESC;


-- ── 2. FULL-TEXT: MULTI-TERM SEARCH ACROSS CORRECTIVE JOBS ───────────────────
-- Find corrective jobs where notes mention a sensor replacement

SELECT
    work_order_id,
    device_id,
    notes,
    cost_eur,
    completed_date
FROM rtia.maintenance_log
WHERE MATCH(notes, 'sensor replaced')
  AND maintenance_type = 'corrective'
ORDER BY cost_eur DESC;


-- ── 3. FULL-TEXT + JOIN: FAULT KEYWORD COST BY PLANT ─────────────────────────
-- Which plants have the highest spend on emergency shutdowns?

SELECT
    p.plant_name,
    p.industry_segment,
    COUNT(*)                   AS matching_jobs,
    ROUND(SUM(m.cost_eur), 0)  AS total_cost_eur
FROM rtia.maintenance_log m
JOIN rtia.plants p ON m.plant_id = p.plant_id
WHERE MATCH(m.notes, 'emergency shutdown')
GROUP BY p.plant_name, p.industry_segment
ORDER BY total_cost_eur DESC;


-- ── 4. FULL-TEXT + JOIN: DEVICES WITH RECURRING SIGNAL FAULTS ────────────────
-- Identify assets with repeated signal or cable issues across all work orders

SELECT
    m.device_id,
    d.manufacturer,
    d.device_type,
    COUNT(*)                   AS matching_jobs,
    ROUND(SUM(m.cost_eur), 0)  AS total_repair_cost
FROM rtia.maintenance_log m
JOIN rtia.devices d ON m.device_id = d.device_id
WHERE MATCH(m.notes, 'signal cable restored')
  AND m.status = 'completed'
GROUP BY m.device_id, d.manufacturer, d.device_type
ORDER BY matching_jobs DESC
LIMIT 10;


-- ─────────────────────────────────────────────────────────────────────────────
-- GEOSPATIAL
-- geo_location is a GEO_POINT stored as [longitude, latitude].
-- DISTANCE() returns meters. WITHIN() tests point-in-polygon.
-- ─────────────────────────────────────────────────────────────────────────────

-- ── 5. GEO: DISTANCE FROM A REFERENCE COORDINATE ─────────────────────────────
-- How far is each plant from Stuttgart HQ?

SELECT
    plant_name,
    city,
    federal_state,
    ROUND(DISTANCE(geo_location, (SELECT geo_location FROM rtia.locations WHERE location_name = 'Stuttgart')) / 1000, 0) AS km_from_stuttgart
FROM rtia.plants
ORDER BY km_from_stuttgart;


-- ── 6. GEO: CRITICAL READINGS WITHIN A RADIUS ────────────────────────────────
-- All critical sensor events within 100 km of Frankfurt

SELECT
    device_id,
    device_type,
    plant_id,
    metric_value,
    metric_unit,
    "timestamp",
    ROUND(DISTANCE(geo_location, (SELECT geo_location FROM rtia.locations WHERE location_name = 'Frankfurt')) / 1000, 1) AS km_from_frankfurt
FROM rtia.iot_data
WHERE status = 'critical'
  AND DISTANCE(geo_location, (SELECT geo_location FROM rtia.locations WHERE location_name = 'Frankfurt')) < 150000
ORDER BY km_from_frankfurt
LIMIT 20;


-- ── 7. GEO: FAULT DENSITY WITHIN A BOUNDING POLYGON ─────────────────────────
-- Critical alerts inside the Bavaria bounding box

SELECT
    device_type,
    COUNT(*)                     AS critical_count,
    ROUND(AVG(quality_score), 1) AS avg_quality
FROM rtia.iot_data
WHERE status = 'critical'
  AND WITHIN(
      geo_location,
      (SELECT geo_area FROM rtia.locations WHERE location_name = 'Bavaria')
  )
GROUP BY device_type
ORDER BY critical_count DESC;


-- ── 8. GEO + JOIN: CRITICAL ALERTS WITH PLANT DISTANCE FROM HQ ───────────────
-- Combine sensor alerts with plant coordinates and distance from a central point

SELECT
    p.city,
    p.federal_state,
    p.industry_segment,
    COUNT(*)                                                   AS total_readings,
    COUNT(*) FILTER (WHERE i.status = 'critical')              AS critical_count,
    ROUND(AVG(i.quality_score), 1)                             AS avg_quality,
    ROUND(DISTANCE(p.geo_location, (SELECT geo_location FROM rtia.locations WHERE location_name = 'Stuttgart')) / 1000, 0) AS km_from_stuttgart
FROM rtia.iot_data i
JOIN rtia.plants p ON i.plant_id = p.plant_id
GROUP BY p.city, p.federal_state, p.industry_segment, p.geo_location
ORDER BY critical_count DESC;


-- ── 9. GEO: NEAREST FAULTING DEVICE TO A COORDINATE ─────────────────────────
-- Find the closest device currently in warning or critical state to Munich

SELECT
    device_id,
    device_type,
    plant_id,
    status,
    metric_value,
    metric_unit,
    geo_location,
    ROUND(DISTANCE(geo_location, (SELECT geo_location FROM rtia.locations WHERE location_name = 'Munich')) / 1000, 2) AS km_from_munich
FROM rtia.iot_data
WHERE status IN ('warning', 'critical')
ORDER BY DISTANCE(geo_location, (SELECT geo_location FROM rtia.locations WHERE location_name = 'Munich'))
LIMIT 10;


-- ─────────────────────────────────────────────────────────────────────────────
-- VECTOR SEARCH
-- notes_embedding is a FLOAT_VECTOR(384) generated by sentence-transformers/
-- all-MiniLM-L6-v2 from the maintenance_log.notes field.
-- Vectors below were precomputed by generate_embeddings.py.
-- To search for a different concept: model.encode(['your text'], normalize_embeddings=True)[0].tolist()
-- KNN_MATCH returns the k nearest neighbours by cosine similarity (_score).
-- ─────────────────────────────────────────────────────────────────────────────

-- ── 10. VECTOR: SEMANTIC SEARCH — EMERGENCY SHUTDOWNS ────────────────────────
-- Query vector encodes: "emergency shutdown thermal runaway critical breach"
-- Returns the 5 work orders whose notes are semantically closest to a
-- thermal emergency event — even if they use different words (e.g.
-- "critical fault", "unplanned stoppage", "thermal event contained").

SELECT
    work_order_id,
    device_id,
    maintenance_type,
    technician,
    completed_date,
    notes,
    _score                 AS similarity
FROM rtia.maintenance_log
WHERE KNN_MATCH(notes_embedding, (SELECT embedding FROM rtia.knn_searches WHERE query_name = 'thermal_event'), 5)
ORDER BY _score DESC;


-- ── 11. VECTOR + FILTER: EMERGENCY SHUTDOWNS — EMERGENCY JOBS ONLY ───────────
-- Same thermal-event vector, filtered to maintenance_type = 'emergency'.
-- Demonstrates hybrid: KNN_MATCH narrows by semantic similarity, the SQL
-- predicate then restricts to only emergency work orders — one pass, no subquery.
-- Query vector encodes: "emergency shutdown thermal runaway critical breach"

SELECT
    work_order_id,
    device_id,
    notes,
    cost_eur,
    completed_date,
    _score                 AS similarity
FROM rtia.maintenance_log
WHERE KNN_MATCH(notes_embedding, (SELECT embedding FROM rtia.knn_searches WHERE query_name = 'thermal_event'), 10)
  AND maintenance_type = 'emergency'
ORDER BY _score DESC;


-- ── 12. VECTOR + JOIN: CALIBRATION FAULTS BY PLANT ───────────────────────────
-- Query vector encodes: "calibration drift sensor out of range recalibration"
-- Finds the top 50 semantically similar work orders and aggregates by plant —
-- surfaces which facilities have the most calibration-related maintenance spend.

SELECT
    p.plant_name,
    p.industry_segment,
    COUNT(*)                   AS similar_jobs,
    ROUND(SUM(m.cost_eur), 0)  AS total_cost_eur,
    ROUND(AVG(m.cost_eur), 0)  AS avg_cost_per_job
FROM rtia.maintenance_log m
JOIN rtia.plants p ON m.plant_id = p.plant_id
WHERE KNN_MATCH(m.notes_embedding, (SELECT embedding FROM rtia.knn_searches WHERE query_name = 'calibration_drift'), 50)
GROUP BY p.plant_name, p.industry_segment
ORDER BY similar_jobs DESC;


-- ── 13. VECTOR + JOIN: DEVICES WITH RECURRING SIGNAL / CABLE FAULTS ──────────
-- Query vector encodes: "signal cable damaged transmission restored"
-- Finds assets that appear more than once in the top-100 KNN results —
-- i.e. devices with a repeated signal/cable fault pattern across work orders.
-- These are candidates for a hardware fix rather than another repair cycle.

SELECT
    m.device_id,
    d.manufacturer,
    d.device_type,
    d.asset_value_eur,
    COUNT(*)                   AS similar_jobs,
    ROUND(SUM(m.cost_eur), 0)  AS total_repair_cost,
    MIN(m.completed_date)      AS first_occurrence,
    MAX(m.completed_date)      AS last_occurrence
FROM rtia.maintenance_log m
JOIN rtia.devices d ON m.device_id = d.device_id
WHERE KNN_MATCH(m.notes_embedding, (SELECT embedding FROM rtia.knn_searches WHERE query_name = 'signal_cable'), 100)
  AND m.status = 'completed'
GROUP BY m.device_id, d.manufacturer, d.device_type, d.asset_value_eur
HAVING COUNT(*) > 1
ORDER BY similar_jobs DESC, total_repair_cost DESC
LIMIT 15;


-- ── 14. HYBRID SEARCH: VECTOR + FULL-TEXT COMBINED SCORE ─────────────────────
-- Combines KNN_MATCH (semantic similarity) and MATCH (keyword relevance) with OR.
-- CrateDB merges both signals into a single _score:
--   - Records matching only the vector rank by cosine similarity alone.
--   - Records matching only the keyword rank by BM25 relevance alone.
--   - Records matching both receive a boosted combined score and surface first.
-- This is the key advantage over running two separate queries and merging results.
--
-- Vector encodes: "worn parts replaced mechanical failure vibration"
-- Keyword: 'vibration' — explicitly boosts notes that name the symptom directly.
-- Together: finds all vibration-related work orders whether they use that exact
-- word or describe the same concept differently ("excessive oscillation",
-- "bearing noise", "abnormal movement").

SELECT
    work_order_id,
    device_id,
    maintenance_type,
    completed_date,
    notes,
    _score                 AS combined_score
FROM rtia.maintenance_log
WHERE KNN_MATCH(notes_embedding, (SELECT embedding FROM rtia.knn_searches WHERE query_name = 'mechanical_failure'), 20)
   OR MATCH(notes, 'vibration')
ORDER BY _score DESC
LIMIT 15;
