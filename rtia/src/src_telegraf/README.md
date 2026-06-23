# src_telegraf — Telegraf ingestion demo

Stream the RTIA IoT dataset into CrateDB through [Telegraf](https://www.influxdata.com/time-series-platform/telegraf/).
A Python script replays the dataset to a Telegraf HTTP listener, which writes to
`rtia.iot_data` via Telegraf's native **`outputs.cratedb`** plugin (PostgreSQL
wire protocol, port 5432).

> CrateDB has **no** InfluxDB line-protocol endpoint — `outputs.cratedb` is the
> supported path. Geo works because `geo_location` is `GENERATED` from the
> `geo_lon` / `geo_lat` fields.

## Files

| File | Purpose |
| --- | --- |
| `TELEGRAF_GUIDE.md` | The full walkthrough — read this. |
| `telegraf_demo.conf` | Telegraf config: `http_listener_v2` input + `outputs.cratedb`. |
| `replay_to_telegraf.py` | Replays the dataset to Telegraf (auto-downloads it on first run). |
| `requirements.txt` | Python dependency for the replay script (`requests`). |

## Quick start

```bash
# 0. create the table (once)
#    run CREATE TABLE rtia.iot_data from ../../sql/rtia_schema_create.sql

# 1. install the replay script's dependency
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. start Telegraf (install it first if needed — see the guide)
telegraf --config telegraf_demo.conf

# 3. in a second terminal, replay a slice
source .venv/bin/activate
python replay_to_telegraf.py --limit 5000 --delay 0.1
```

Then verify in CrateDB:

```sql
REFRESH TABLE rtia.iot_data;
SELECT COUNT(*), COUNT(DISTINCT tags['device_id']) FROM rtia.iot_data;
```

See **[TELEGRAF_GUIDE.md](TELEGRAF_GUIDE.md)** for the full setup, CrateDB Cloud
config, and a Reliability & gotchas section.
