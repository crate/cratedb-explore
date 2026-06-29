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
-- DEVICE_BEHAVIOR ─────────────────────────────────────────────────────────────
-- One behaviour vector per device for "find devices behaving alike" KNN search
-- (the numeric counterpart to maintenance_log.notes_embedding). Populated by the
-- src_behavior_search module's backfill.py; see that module's README.
--
-- The vector is 9 value-derived summary stats over the device's iot_data window
-- (mean, std, min, max, range, p05, p50, p95, trend_slope), z-scored WITHIN each
-- device_type so within-type KNN is scale-fair. Each device measures one metric
-- (device_type <-> metric_unit is 1:1), so similarity is only meaningful within a
-- device_type — similar_devices scopes KNN to the query device's type.
--
-- device_type / n_critical / n_warning are denormalised onto the row so a
-- neighbour result carries its fault counts without a join. 1 row per device, so
-- it never fans out. status counts are a held-out label, never part of the vector.
-- FLOAT_VECTOR supports KNN_MATCH directly; no explicit vector index is needed.

CREATE TABLE IF NOT EXISTS rtia.device_behavior (
    device_id       TEXT PRIMARY KEY,
    device_type     TEXT,                          -- temperature_sensor | vibration_sensor | power_meter | pressure_sensor | flow_meter
    window_start    TIMESTAMP WITH TIME ZONE,
    window_end      TIMESTAMP WITH TIME ZONE,
    n_readings      INTEGER,
    n_critical      INTEGER,                        -- held-out fault label, not a feature
    n_warning       INTEGER,                        -- held-out fault label, not a feature
    behavior_vector FLOAT_VECTOR(9)                 -- within-type z-scored summary stats
);
