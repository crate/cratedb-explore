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
-- CrateDB Industrial IoT — First Queries
-- Dataset: 5 German plants · 500 devices · 500,000 sensor readings
-- Tables:  iot_data · plants · devices · maintenance_log
--
-- iot_data is stored in the Telegraf line-protocol shape: the string dimensions
-- live in the `tags` OBJECT (tags['device_id'], tags['status'], tags['plant_id'],
-- the flattened tags['metadata_*'], …) and the numeric measurements in `fields`
-- (fields['metric_value'], fields['quality_score']). geo_location is a GEO_POINT
-- GENERATED from fields['geo_lon'] / fields['geo_lat'].
-- ─────────────────────────────────────────────────────────────────────────────


-- ── 1. INGEST CHECK ───────────────────────────────────────────────────────────
-- Verify row count and date range after COPY FROM

SELECT
    COUNT(*)                                    AS total_readings,
    COUNT(DISTINCT tags['device_id'])           AS devices,
    COUNT(DISTINCT tags['plant_id'])            AS plants,
    MIN("timestamp")                            AS earliest,
    MAX("timestamp")                            AS latest
FROM rtia.iot_data;


-- ── 2. STATUS DISTRIBUTION ────────────────────────────────────────────────────
-- How healthy is the fleet right now?

SELECT
    tags['status']                              AS status,
    COUNT(*)                                    AS readings,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) AS pct
FROM rtia.iot_data
GROUP BY tags['status']
ORDER BY readings DESC;


-- ── 3. FAULT RATE BY DEVICE TYPE ─────────────────────────────────────────────
-- Which sensor category generates the most alerts?

SELECT
    tags['device_type']                                    AS device_type,
    COUNT(*)                                                AS total,
    COUNT(*) FILTER (WHERE tags['status'] = 'warning')     AS warnings,
    COUNT(*) FILTER (WHERE tags['status'] = 'critical')    AS criticals,
    ROUND(AVG(fields['quality_score']), 1)                 AS avg_quality
FROM rtia.iot_data
GROUP BY tags['device_type']
ORDER BY criticals DESC;


-- ── 4. TIME-SERIES: HOURLY FAULT TREND ───────────────────────────────────────
-- Critical events per hour — spot operational patterns

SELECT
    DATE_TRUNC('hour', "timestamp")                     AS hour,
    COUNT(*) FILTER (WHERE tags['status'] = 'critical') AS critical_count
FROM rtia.iot_data
GROUP BY hour
ORDER BY hour;


-- ── 5. GEO: ALERT DENSITY PER PLANT ──────────────────────────────────────────
-- Where on the map are faults clustering?

SELECT
    tags['plant_id']                                    AS plant_id,
    ANY_VALUE(geo_location)                             AS location,
    COUNT(*) FILTER (WHERE tags['status'] = 'critical') AS critical_readings,
    ROUND(AVG(fields['quality_score']), 1)              AS avg_quality
FROM rtia.iot_data
GROUP BY tags['plant_id']
ORDER BY critical_readings DESC;


-- ── 6. JOIN: FAULT RATE BY INDUSTRY SEGMENT ──────────────────────────────────
-- iot_data + plants  →  which industry has the worst quality?

SELECT
    p.industry_segment,
    p.plant_name,
    p.employee_count,
    COUNT(*) FILTER (WHERE i.tags['status'] = 'critical') AS critical_events,
    COUNT(*) FILTER (WHERE i.tags['status'] = 'warning')  AS warning_events,
    ROUND(AVG(i.fields['quality_score']), 1)              AS avg_quality
FROM rtia.iot_data i
JOIN rtia.plants p ON i.tags['plant_id'] = p.plant_id
GROUP BY p.industry_segment, p.plant_name, p.employee_count
ORDER BY critical_events DESC;


-- ── 7. JOIN: CRITICAL ALERTS ON OUT-OF-WARRANTY ASSETS ───────────────────────
-- iot_data + devices  →  find exposed assets that need budget attention

SELECT
    d.device_id,
    d.manufacturer,
    d.device_type,
    d.warranty_expiry,
    d.asset_value_eur,
    COUNT(*)           AS critical_readings,
    MAX(i."timestamp") AS last_critical_at
FROM rtia.iot_data i
JOIN rtia.devices d ON i.tags['device_id'] = d.device_id
WHERE i.tags['status'] = 'critical'
  AND d.warranty_expiry < TIMESTAMP '2025-09-01'
GROUP BY d.device_id, d.manufacturer, d.device_type, d.warranty_expiry, d.asset_value_eur
ORDER BY critical_readings DESC
LIMIT 15;


-- ── 8. JOIN: OVERDUE MAINTENANCE ─────────────────────────────────────────────
-- iot_data + devices  →  devices past their scheduled service date and still active

SELECT
    d.device_id,
    d.device_type,
    d.plant_id,
    d.responsible_technician,
    d.next_maintenance_due,
    COUNT(*) FILTER (WHERE i.tags['status'] IN ('warning', 'critical')) AS fault_readings
FROM rtia.iot_data i
JOIN rtia.devices d ON i.tags['device_id'] = d.device_id
WHERE d.next_maintenance_due < TIMESTAMP '2025-09-01'
GROUP BY d.device_id, d.device_type, d.plant_id, d.responsible_technician, d.next_maintenance_due
ORDER BY fault_readings DESC
LIMIT 15;


-- ── 9. JOIN: DEVICES STILL FAULTING AFTER MAINTENANCE ────────────────────────
-- iot_data + devices + maintenance_log  →  maintenance that did not hold

SELECT
    i.tags['device_id'] AS device_id,
    d.manufacturer,
    d.device_type,
    m.maintenance_type,
    m.completed_date,
    m.cost_eur,
    COUNT(*) AS fault_readings_after_service
FROM rtia.iot_data i
JOIN rtia.devices d          ON i.tags['device_id'] = d.device_id
JOIN rtia.maintenance_log m  ON i.tags['device_id'] = m.device_id
WHERE i.tags['status'] IN ('warning', 'critical')
  AND m.status = 'completed'
  AND i."timestamp" > m.completed_date::TIMESTAMP
GROUP BY i.tags['device_id'], d.manufacturer, d.device_type,
         m.maintenance_type, m.completed_date, m.cost_eur
ORDER BY fault_readings_after_service DESC
LIMIT 10;


-- ── 11. OBJECT FIELD ACCESS: FAULT RATE BY FIRMWARE VERSION ─────────────────
-- Demonstrates bracket notation on the tags OBJECT column. Telegraf flattens the
-- per-device metadata into tags['metadata_*'] (it can only carry flat strings),
-- so the firmware/model dimensions live there.
-- Surfaces whether a specific firmware release correlates with higher fault rates.

SELECT
    tags['metadata_firmware_version']                         AS firmware_version,
    tags['metadata_model']                                    AS model,
    COUNT(*)                                                  AS total_readings,
    COUNT(*) FILTER (WHERE tags['status'] = 'critical')       AS critical_count,
    COUNT(*) FILTER (WHERE tags['status'] = 'warning')        AS warning_count,
    ROUND(AVG(fields['quality_score']), 1)                    AS avg_quality
FROM rtia.iot_data
GROUP BY tags['metadata_firmware_version'], tags['metadata_model']
ORDER BY critical_count DESC
LIMIT 20;


-- ── 12. DATE_BIN: FIXED-WIDTH 15-MINUTE TIME WINDOWS ─────────────────────────
-- DATE_BIN buckets readings into precise fixed-width intervals regardless of
-- natural calendar boundaries — useful for shift reporting and SLA windows.

SELECT
    DATE_BIN('15 minutes'::INTERVAL, "timestamp", TIMESTAMP '2025-09-01') AS window_start,
    tags['device_type']                                     AS device_type,
    COUNT(*)                                                AS readings,
    COUNT(*) FILTER (WHERE tags['status'] = 'critical')     AS criticals,
    ROUND(AVG(fields['metric_value']), 2)                   AS avg_value
FROM rtia.iot_data
WHERE "timestamp" >= TIMESTAMP '2025-09-01 06:00:00'
  AND "timestamp" <  TIMESTAMP '2025-09-01 14:00:00'
GROUP BY window_start, tags['device_type']
ORDER BY window_start, device_type;


-- ── 10. AGGREGATED MAINTENANCE COST BY PLANT ─────────────────────────────────
-- maintenance_log + plants  →  operational spend overview

SELECT
    p.plant_name,
    p.industry_segment,
    COUNT(m.work_order_id)                                   AS work_orders,
    COUNT(*) FILTER (WHERE m.maintenance_type = 'emergency') AS emergency_jobs,
    ROUND(SUM(m.cost_eur), 0)                                AS total_cost_eur,
    ROUND(AVG(m.cost_eur), 0)                                AS avg_cost_per_job
FROM rtia.maintenance_log m
JOIN rtia.plants p ON m.plant_id = p.plant_id
WHERE m.status = 'completed'
GROUP BY p.plant_name, p.industry_segment
ORDER BY total_cost_eur DESC;


-- ── 13. OEE APPROXIMATION BY PLANT AND DEVICE TYPE ───────────────────────────
-- Overall Equipment Effectiveness derived from sensor status and quality_score.
-- This is an approximation — a full OEE calculation requires a shift_production
-- table with runtime, planned time, actual output, and good units. Using the
-- fields available in iot_data:
--   Availability = share of readings where device was online (status != 'offline')
--   Performance  = share of online readings in normal operating state
--   Quality      = average quality_score of online readings, normalised to 0–1
--   OEE          = Availability × Performance × Quality × 100

SELECT
    i.tags['plant_id']                                               AS plant_id,
    p.plant_name,
    p.industry_segment,
    i.tags['device_type']                                           AS device_type,
    COUNT(*)                                                         AS total_readings,
    ROUND(
        COUNT(*) FILTER (WHERE i.tags['status'] != 'offline') * 100.0
        / NULLIF(COUNT(*), 0), 1
    )                                                                AS availability_pct,
    ROUND(
        COUNT(*) FILTER (WHERE i.tags['status'] = 'normal') * 100.0
        / NULLIF(COUNT(*) FILTER (WHERE i.tags['status'] != 'offline'), 0), 1
    )                                                                AS performance_pct,
    ROUND(
        AVG(i.fields['quality_score']) FILTER (WHERE i.tags['status'] != 'offline'), 1
    )                                                                AS quality_score_avg,
    ROUND(
          (COUNT(*) FILTER (WHERE i.tags['status'] != 'offline')    * 1.0 / NULLIF(COUNT(*), 0))
        * (COUNT(*) FILTER (WHERE i.tags['status'] = 'normal')      * 1.0 / NULLIF(COUNT(*) FILTER (WHERE i.tags['status'] != 'offline'), 0))
        * (AVG(i.fields['quality_score']) FILTER (WHERE i.tags['status'] != 'offline') / 100.0)
        * 100, 1
    )                                                                AS oee_approx_pct
FROM rtia.iot_data i
JOIN rtia.plants p ON i.tags['plant_id'] = p.plant_id
GROUP BY i.tags['plant_id'], p.plant_name, p.industry_segment, i.tags['device_type']
ORDER BY oee_approx_pct ASC;
