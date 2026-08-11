#!/bin/sh
#set -x
# Load environment variables from .env file
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
else
    echo ".env file not found! Exiting..."
    exit 1
fi
# Remove previous output file
rm -f output.txt

# Define parameters
MAX_ROWS=1000000
SHARDS=12
CONCURRENT_REQUESTS=1
PROCESSES=1
REPLICAS=1
HIGH_WATER_MARK=0
LATEST_ROWS_PER_SEC=0

SUMMARY_FILE=/tmp/$$.summary
rm -rf ${SUMMARY_FILE} 2> /dev/null

BATCH_SIZE=500

# Loop through each batch size, increase until 
# throughput drops
while
	true
do
    HIGH_WATER_MARK=${LATEST_ROWS_PER_SEC}
    # Capture and log the start time
    START_TIME=$(date '+%Y-%m-%d %H:%M:%S')
    echo "Starting benchmark with batch size: $BATCH_SIZE at $START_TIME" | tee -a  output.txt
    # Execute the nodeIngestBench command using node
    node appCluster.js \
        --batch_size "$BATCH_SIZE" \
        --max_rows "$MAX_ROWS" \
        --shards "$SHARDS" \
        --replicas "$REPLICAS" \
        --concurrent_requests "$CONCURRENT_REQUESTS" \
        --processes "$PROCESSES" > /tmp/$$
    LATEST_ROWS_PER_SEC=`tail -5  /tmp/$$  | grep Speed | awk '{ print $2 }' | awk -F . '{ print $1 }' | sed '1,$s/,//g'`
    echo Batch size: $BATCH_SIZE. Rows Per Second = $LATEST_ROWS_PER_SEC | tee -a $SUMMARY_FILE
    cat /tmp/$$ >>  output.txt
    rm /tmp/$$

    if
 	[ "${HIGH_WATER_MARK}" -le ${LATEST_ROWS_PER_SEC} ]
    then
	BATCH_SIZE=`expr $BATCH_SIZE \* 2`
    else
	break
    fi

	sleep 30
done
echo "All benchmarks executed successfully."

# Display results
mv output.txt output-find-optimal-batchsize.txt
cat output-find-optimal-batchsize.txt
cat $SUMMARY_FILE
rm $SUMMARY_FILE
echo "Optimal batch size is ${BATCH_SIZE}."
