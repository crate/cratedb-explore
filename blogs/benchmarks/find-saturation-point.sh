#!/bin/bash
set -x
# Load environment variables from .env file
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
else
    echo ".env file not found! Exiting..."
    exit 1
fi
# Remove previous output file
rm -f output.txt
# Define fixed parameters inside the script
BATCH_SIZE=16000
MAX_ROWS=1
SHARDS=12
CONCURRENT_REQUESTS=1
PROCESSES=6
REPLICAS=1
# Define number of processes
ROWS=(1000000 2000000 4000000 8000000 16000000 32000000)
# Loop through each batch size
for rows in "${ROWS[@]}"; do
    # Capture and log the start time
    START_TIME=$(date '+%Y-%m-%d %H:%M:%S')
    echo "Starting benchmark with number of max_rows: $rows at $START_TIME" >> output.txt
    # Execute the nodeIngestBench command using node
    node appCluster.js \
        --batch_size "$BATCH_SIZE" \
        --max_rows "$rows" \
        --shards "$SHARDS" \
        --replicas "$REPLICAS" \
        --concurrent_requests "$CONCURRENT_REQUESTS" \
        --processes "$PROCESSES" >> output.txt
sleep 30
done
echo "All benchmarks executed successfully."
# Display results
mv output.txt output-find-saturation-point.txt
cat output-find-saturation-point.txt
