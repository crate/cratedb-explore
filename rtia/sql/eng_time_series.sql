-- See  https://cratedb.com/blog/sql-feature-engineering-time-series-ml

SELECT
      tags['device_id']    AS device_id,
      tags['device_type']  AS device_type,
      tags['plant_id']     AS plant_id,
      DATE_BIN('1 hour'::INTERVAL, "timestamp", TIMESTAMP '2025-09-01') AS window_start,
      AVG(fields['metric_value'])    AS metric_mean,
      STDDEV(fields['metric_value']) AS metric_std,
      AVG(fields['quality_score'])   AS quality_mean,
      COUNT(*) FILTER (WHERE tags['status'] IN ('warning', 'critical'))
          * 1.0 / NULLIF(COUNT(*), 0) AS fault_rate,
      MAX(CASE WHEN tags['status'] IN ('warning', 'critical') THEN 1 ELSE 0 END) AS had_fault
  FROM rtia.iot_data
  WHERE "timestamp" >= TIMESTAMP '2025-09-01'
  GROUP BY tags['device_id'], tags['device_type'], tags['plant_id'], window_start
  ORDER BY tags['device_id'], window_start;


-- note that 'NOW - 90' is 'NOW -365', as otherwise there is no matching data
SELECT
      tags['device_id']    AS device_id,
      tags['device_type']  AS device_type,
      tags['plant_id']     AS plant_id,
      DATE_BIN('1 hour'::INTERVAL, "timestamp", TIMESTAMP '2025-09-01') AS window_start,
      AVG(fields['metric_value'])    AS metric_mean,
      STDDEV(fields['metric_value']) AS metric_std,
      AVG(fields['quality_score'])   AS quality_mean,
      COUNT(*) FILTER (WHERE tags['status'] IN ('warning', 'critical'))
          * 1.0 / NULLIF(COUNT(*), 0) AS fault_rate,
      MAX(CASE WHEN tags['status'] IN ('warning', 'critical') THEN 1 ELSE 0 END) AS had_fault
  FROM rtia.iot_data
  WHERE "timestamp" >= NOW() - INTERVAL '365' DAY
  GROUP BY tags['device_id'], tags['device_type'], tags['plant_id'], window_start
  ORDER BY tags['device_id'], window_start limit 100;

