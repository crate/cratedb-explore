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
BATCH_SIZE=1
MAX_ROWS=1000000
SHARDS=12
CONCURRENT_REQUESTS=1
PROCESSES=1
REPLICAS=1
# Define batch sizes
BATCH_SIZES=(500 1000 2000 4000 8000 16000 32000)
# BATCH_SIZES=(500 1000)
# Loop through each batch size
for batch_size in "${BATCH_SIZES[@]}"; do
    # Capture and log the start time
    START_TIME=$(date '+%Y-%m-%d %H:%M:%S')
    echo "Starting benchmark with batch size: $batch_size at $START_TIME" >> output.txt
    # Execute the nodeIngestBench command using node
    node appCluster.js \
        --batch_size "$batch_size" \
        --max_rows "$MAX_ROWS" \
        --shards "$SHARDS" \
        --replicas "$REPLICAS" \
        --concurrent_requests "$CONCURRENT_REQUESTS" \
        --processes "$PROCESSES" >> output.txt
sleep 30
done
echo "All benchmarks executed successfully."
# Display results
mv output.txt output-find-optimal-batchsize.txt
cat output-find-optimal-batchsize.txt
