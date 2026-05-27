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

CREATE TABLE IF NOT EXISTS demo.climate_data (
   measurement_time TIMESTAMP WITHOUT TIME ZONE,
   geo_location GEO_POINT,
   data OBJECT(DYNAMIC) AS (
      temperature DOUBLE PRECISION,
      pressure DOUBLE PRECISION,
      u10 DOUBLE PRECISION,
      v10 DOUBLE PRECISION,
      latitude DOUBLE PRECISION,
      longitude DOUBLE PRECISION
   )
);

CREATE TABLE IF NOT EXISTS demo.german_regions (
    region_name        TEXT PRIMARY KEY,
    geo_coords         GEO_SHAPE,
    tourism_info       TEXT INDEX USING FULLTEXT WITH (analyzer = 'english'),
    transportation     TEXT INDEX USING FULLTEXT WITH (analyzer = 'english'),
    economics          TEXT INDEX USING FULLTEXT WITH (analyzer = 'english'),
    introduced_species TEXT INDEX USING FULLTEXT WITH (analyzer = 'english'),
    embedding          FLOAT_VECTOR(1536)
);

CREATE TABLE IF NOT EXISTS  demo.geo_points (
    latitude     DOUBLE PRECISION,
    longitude    DOUBLE PRECISION,
    geo_location GEO_POINT,
    nearest_town TEXT,
    PRIMARY KEY (latitude, longitude)
);
