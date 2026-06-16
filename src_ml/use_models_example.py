"""Minimal example: load the trained models and score one feature row.

Mirrors the snippet in ML_GUIDE.md ("Using the models in your own code").
Run from the src_ml/ directory after train_model.py has produced the .pkl files
in model/.
"""

import pickle
import pandas as pd

# Load
with open('model/predictive_maintenance_model.pkl', 'rb') as f:
    clf = pickle.load(f)
with open('model/anomaly_detector_model.pkl', 'rb') as f:
    iso = pickle.load(f)

print(clf['features'])          # exact ordered feature list the model expects (20 features)
print(f"Horizon: {clf['horizon']} readings   ROC-AUC: {clf['roc_auc']}")

# device_type must be label-encoded first (strings -> ints)
device_type_enc = clf['label_encoder'].transform(['vibration_sensor'])[0]

# Build one feature row as {name: value}, then select each model's feature list
# BY NAME. The classifier uses all 20 features; the anomaly detector uses a
# 7-feature subset in a *different* order — so never slice the array positionally.
row = {
    'metric_value': 85.0, 'quality_score': 91.0, 'hour': 14, 'day_of_week': 2,
    'device_type_enc': device_type_enc, 'metric_delta': 1.2, 'quality_delta': -0.5,
    'fw_major': 2,
    # Rolling-window features (abbreviated to "..." in ML_GUIDE.md). In real use
    # these come from engineer_features(); here they're filled with plausible
    # values so this script runs standalone.
    'metric_mean_5': 84.0, 'metric_mean_10': 83.5, 'metric_mean_20': 83.0,
    'metric_std_5': 1.5, 'metric_std_10': 1.8, 'metric_std_20': 2.1,
    'quality_mean_5': 91.5, 'quality_mean_10': 92.0, 'quality_mean_20': 92.5,
    'fault_rate_5': 0.0, 'fault_rate_10': 0.0, 'fault_rate_20': 0.0,
}
X = pd.DataFrame([row])

prob    = clf['model'].predict_proba(X[clf['features']])[0, 1]
anomaly = -iso['model'].score_samples(X[iso['features']])[0]
print(f'Fault probability: {prob:.1%}   Anomaly score: {anomaly:.4f}')
