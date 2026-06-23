-- See  https://cratedb.com/blog/anomaly-detection-industrial-sensor-data

SELECT
      device_id,
      device_type,
      plant_id,
      current_status,
      fault_probability,
      anomaly_score,
      scored_at
  FROM rtia.fault_predictions
  WHERE anomaly_score > 0.30
    AND fault_probability < 0.25
  ORDER BY anomaly_score DESC
  LIMIT 20;

  SELECT
      device_id,
      device_type,
      plant_id,
      fault_probability,
      fault_risk_label,
      anomaly_score,
      scored_at
  FROM rtia.fault_predictions
  WHERE fault_probability > 0.60
    AND anomaly_score > 0.30
  ORDER BY fault_probability DESC, anomaly_score DESC
  LIMIT 20;