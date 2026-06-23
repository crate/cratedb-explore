-- See https://cratedb.com/blog/real-time-ml-inference-cratedb-fastapi
SELECT
      tags['device_id']                 AS device_id,
      tags['device_type']               AS device_type,
      tags['plant_id']                  AS plant_id,
      "timestamp",
      fields['metric_value']            AS metric_value,
      fields['quality_score']           AS quality_score,
      tags['status']                    AS status,
      tags['metadata_firmware_version'] AS firmware_version
  FROM rtia.iot_data
  WHERE tags['device_id'] = 'DEVICE_0042'
  ORDER BY "timestamp" DESC
  LIMIT 50;

 SELECT
      device_id,
      device_type,
      plant_id,
      current_status,
      fault_probability,
      fault_risk_label,
      scored_at
  FROM rtia.fault_predictions
  WHERE current_status IN ('warning', 'critical')
    AND fault_probability > 0.60
  ORDER BY fault_probability DESC
  LIMIT 20;

SELECT
    f.plant_id,
    COUNT(DISTINCT f.device_id) AS devices_scored,
    ROUND(AVG(f.fault_probability), 3) AS avg_fault_probability,
    COUNT(*) FILTER (WHERE f.fault_risk_label = 'high') AS high_risk_devices
FROM rtia.fault_predictions f
GROUP BY f.plant_id
ORDER BY avg_fault_probability DESC limit 100;

