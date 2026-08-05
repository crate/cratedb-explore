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
MAX_ROWS=1000000
SHARDS=3
CONCURRENT_REQUESTS=1
PROCESSES=1
REPLICAS=1
# Define number of shards
SHARDS=(3 9 12 18 36 54)
# Loop through each batch size
for shards in "${SHARDS[@]}"; do
    # Capture and log the start time
    START_TIME=$(date '+%Y-%m-%d %H:%M:%S')
    echo "Starting benchmark with number of shards: $shards at $START_TIME" >> output.txt
    # Execute the nodeIngestBench command using node
    node appCluster.js \
        --batch_size "$BATCH_SIZE" \
        --max_rows "$MAX_ROWS" \
        --shards "$shards" \
        --replicas "$REPLICAS" \
        --concurrent_requests "$CONCURRENT_REQUESTS" \
        --processes "$PROCESSES" >> output.txt
sleep 30
done
echo "All benchmarks executed successfully."
# Display results
mv output.txt output-find-optimal-shards.txt
cat output-find-optimal-shards.txt
