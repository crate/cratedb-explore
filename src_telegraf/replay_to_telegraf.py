"""
CrateDB Industrial IoT - Replay dataset to Telegraf
====================================================
Reads iot_demo_dataset.json line by line and POSTs each record (one JSON
object per request) to a Telegraf http_listener_v2 endpoint.

This simulates live sensor data flowing through Telegraf into CrateDB.
Telegraf receives the JSON, maps it via the json_v2 parser, and writes
to CrateDB with the native outputs.cratedb plugin (PostgreSQL wire, :5432).
CrateDB has no InfluxDB line-protocol endpoint — outputs.cratedb is the
supported path.

Architecture:
  [this script] --HTTP POST--> Telegraf (http_listener_v2)
                                     |
                               json_v2 parser
                                     |
                               outputs.cratedb  (PostgreSQL wire, :5432)
                                     |
                              CrateDB (rtia.iot_data table)

Prerequisites:
  pip install requests

Usage:
  # All 500,000 records, as fast as possible
  python replay_to_telegraf.py

  # Demo mode: ~2,000 rec/s, visible in CrateDB Admin UI
  python replay_to_telegraf.py --delay 0.05

  # Quick demo: first 5,000 records with a pause between batches
  python replay_to_telegraf.py --limit 5000 --delay 0.1

  # Single device only
  python replay_to_telegraf.py --device DEVICE_0042

  # Custom Telegraf URL
  python replay_to_telegraf.py --url http://my-telegraf-host:8186/telegraf

  # Set via environment variable
  TELEGRAF_URL=http://my-telegraf-host:8186/telegraf python replay_to_telegraf.py
"""

import argparse
import json
import os
import sys
import time

try:
    import requests
except ImportError:
    print("Missing dependency: pip install requests")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TELEGRAF_URL = os.getenv("TELEGRAF_URL", "http://localhost:8186/telegraf")

BASE      = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE, '..', 'data', 'iot_demo_dataset.json')

# Canonical dataset (~240 MB, gitignored) — same S3 object the COPY FROM
# statements in sql/rtia_schema_create.sql read. Downloaded on first run.
S3_BUCKET = "iot2-601357753311-eu-west-1-an.s3.eu-west-1.amazonaws.com"
DATASET_URL = f"https://{S3_BUCKET}/iot_demo_dataset.json"


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def ensure_dataset(path: str) -> None:
    """Download the dataset from S3 if it isn't already on disk.

    The 240 MB file is gitignored and not shipped with the repo, so the first
    run fetches it. Progress is printed — this is never silent.
    """
    if os.path.exists(path):
        print(f"  Dataset found: {path}")
        return

    os.makedirs(os.path.dirname(path), exist_ok=True)
    print(f"  Dataset not found at {path}")
    print(f"  Downloading from {DATASET_URL}")
    print(f"  (~240 MB — runs once, then cached locally; the file is gitignored)")

    tmp = path + ".part"
    try:
        with requests.get(DATASET_URL, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length", 0))
            done  = 0
            with open(tmp, "wb") as out:
                for chunk in r.iter_content(chunk_size=1 << 20):  # 1 MiB
                    out.write(chunk)
                    done += len(chunk)
                    if total:
                        print(f"  {done / 1e6:>8,.1f} / {total / 1e6:,.1f} MB"
                              f"  ({done * 100 / total:4.1f}%)", end="\r")
                    else:
                        print(f"  {done / 1e6:>8,.1f} MB downloaded", end="\r")
        os.replace(tmp, path)            # atomic: only a complete file lands at `path`
        print(f"\n  Download complete: {path}\n")
    except (requests.RequestException, OSError) as exc:
        if os.path.exists(tmp):
            os.remove(tmp)
        print(f"\n  ERROR downloading dataset: {exc}", file=sys.stderr)
        print(f"  You can fetch it manually:\n"
              f"    curl -o {path} {DATASET_URL}", file=sys.stderr)
        sys.exit(1)


def replay(url: str, path: str, batch_size: int, delay: float,
           device_filter: str | None, limit: int | None) -> None:

    session = requests.Session()
    total   = 0
    errors  = 0
    batch   = []
    t0      = time.time()

    print("=" * 60)
    print("CrateDB IoT Demo — Telegraf Replay")
    print("=" * 60)
    print(f"  Source : {os.path.basename(path)}")
    print(f"  Target : {url}")
    print(f"  Batch  : {batch_size} records per progress update")
    print(f"  Delay  : {delay}s between batches")
    if device_filter:
        print(f"  Filter : device_id = {device_filter}")
    if limit:
        print(f"  Limit  : {limit:,} records")
    print()

    # Fetch the dataset on first run (prints progress — never silent).
    ensure_dataset(path)

    def flush(b: list) -> None:
        # http_listener_v2's json_v2 parser accepts ONE metric object per
        # request here — a JSON array (bare [...] or wrapped {"metrics":[...]})
        # is rejected with HTTP 400. So POST each record individually over the
        # keep-alive session; the batch is just the pacing unit for progress
        # and --delay.
        nonlocal total, errors
        for rec in b:
            try:
                r = session.post(url, json=rec, timeout=10)
                if r.status_code not in (200, 204):
                    errors += 1
                    print(
                        f"\n  WARN HTTP {r.status_code}: {r.text[:120]}",
                        file=sys.stderr,
                    )
            except requests.RequestException as exc:
                errors += 1
                print(f"\n  ERROR: {exc}", file=sys.stderr)
        total += len(b)

    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            if device_filter and rec.get("device_id") != device_filter:
                continue

            batch.append(rec)

            if len(batch) >= batch_size:
                flush(batch)
                batch = []

                elapsed = time.time() - t0
                rps     = total / elapsed if elapsed > 0 else 0
                print(
                    f"  {total:>9,} records sent  |  {rps:>8,.0f} rec/s  |  "
                    f"{errors} errors",
                    end="\r",
                )

                if delay > 0:
                    time.sleep(delay)

            if limit and total >= limit:
                break

    # Flush remaining
    if batch:
        flush(batch)

    elapsed = time.time() - t0
    print(f"\n\n{'=' * 60}")
    print(f"  Done. {total:,} records in {elapsed:.1f}s")
    print(f"  Throughput: {total / elapsed:,.0f} rec/s average")
    if errors:
        print(f"  Errors: {errors} batches failed — check Telegraf logs")
    print()
    print("  Verify in CrateDB:")
    print("  SELECT COUNT(*) FROM rtia.iot_data;")
    print("  SELECT tags['device_id'], COUNT(*) FROM rtia.iot_data")
    print("  GROUP BY tags['device_id'] ORDER BY COUNT(*) DESC LIMIT 10;")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Replay iot_demo_dataset.json to a Telegraf HTTP listener",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python replay_to_telegraf.py                         # full speed, all records
  python replay_to_telegraf.py --delay 0.05            # ~2,000 rec/s demo mode
  python replay_to_telegraf.py --limit 5000 --delay 0.1
  python replay_to_telegraf.py --device DEVICE_0042
        """,
    )
    parser.add_argument(
        "--url",
        default=TELEGRAF_URL,
        help="Telegraf HTTP listener URL (default: %(default)s)",
    )
    parser.add_argument(
        "--input",
        default=DATA_FILE,
        help="Path to NDJSON file (default: data/iot_demo_dataset.json; "
             "auto-downloaded from S3 if missing)",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=100,
        help="Records per progress update / --delay pause (default: 100)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Seconds to wait between batches (default: 0 — full speed)",
    )
    parser.add_argument(
        "--device",
        default=None,
        help="Replay only this device_id",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after N records (default: all 500,000)",
    )
    args = parser.parse_args()

    replay(
        url           = args.url,
        path          = args.input,
        batch_size    = args.batch,
        delay         = args.delay,
        device_filter = args.device,
        limit         = args.limit,
    )
