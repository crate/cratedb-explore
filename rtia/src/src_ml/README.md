# AI Frameworks

CrateDB can be used as a backend for AI and LLM-based applications, particularly for:
* Retrieval-Augmented Generation (RAG)
* Time-series analytics for AI workflows
* Real-time data enrichment

## Connecting to your framework

You can connect to CrateDB from AI frameworks using standard PostgreSQL drivers or HTTP APIs, making it straightforward to integrate with tools such as:
* LangChain
* LlamaIndex
* Custom Python-based AI pipelines

In most cases, you simply provide:

* Host: <your-host>
* Port: 5432
* Username: crate
* Database: demo

Your AI application can then query CrateDB in real time using SQL.

## Example: Machine Learning

There is an ML component that's part of the IOT Scenario you've been working on. 

There is a [github repo](https://github.com/crate/cratedb-explore/tree/main/rtia/src/src_ml) in [CrateDB explore](https://github.com/crate/cratedb-explore/tree/main/rtia/src/src_ml)  that 
allows you to do the following:

Starting from 500,000 real sensor readings across 500 devices, the [ML guide](https://github.com/crate/cratedb-explore/blob/main/rtia/src/src_ml/ML_GUIDE.md) walks you end-to-end from a JSON file to a live service backed by CrateDB:

* **Train a predictive-maintenance classifier** — an [XGBoost](https://xgboost.readthedocs.io/) model that learns the *pattern before a fault*, predicting whether a device will hit warning/critical within its next 5 readings.
* **Train an anomaly detector** — an [Isolation Forest](https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.IsolationForest.html) fit on healthy readings only, scoring how far any reading drifts from the fleet baseline.
* **Engineer time-series features the right way** — per-device rolling windows (5/10/20 readings), backward-looking only, split by `device_id` so the model never leaks future data or memorises devices.
* **Avoid the classic ML traps** — class-imbalance correction, leakage-proof train/test splits, and forward-shifted targets, all explained as you go.
* **Score on demand or in batch** — a CSV-producing batch script for offline runs, plus a runnable "use the models" snippet.
* **Use CrateDB as the data source** — swap the static file for live SQL across three scenarios: scheduled retraining on the last 90 days, pushing rolling-feature aggregation down into CrateDB with [`DATE_BIN`](https://cratedb.com/docs/crate/reference/en/latest/general/builtins/scalar-functions.html#date-bin-interval-timestamp-origin), and live fleet scoring that writes predictions back.
* **Run a real-time [FastAPI](https://fastapi.tiangolo.com/) inference service** — fetch a device's recent history from CrateDB, build features in memory, score, and persist `fault_probability` back to `rtia.fault_predictions` in under 20 ms per device.
* **Query predictions alongside live sensor data** — rank the riskiest devices, roll risk up by plant, and join asset metadata, all in plain SQL.
* **Visualise it in [Grafana](https://grafana.com/)** — import [`rtia/grafana/rtia.json`](https://github.com/crate/cratedb-explore/blob/main/rtia/grafana/rtia.json) (the "Real Time Industrial Analytics Dashboard"), whose "ML Fault Predictions" panel surfaces the highest-risk devices straight from CrateDB.

Clone the repo, point it at your own CrateDB cluster (or a local Docker one), and you can reproduce the entire pipeline in minutes.

