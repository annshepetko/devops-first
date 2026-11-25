import hashlib
import json
import os
import socket
import time
import urllib.parse

import boto3

REGION = os.environ["AWS_REGION"]
QUEUE_URL = os.environ["QUEUE_URL"]
LOG_GROUP = os.environ["LOG_GROUP"]

sqs = boto3.client("sqs", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
logs = boto3.client("logs", region_name=REGION)
STREAM = f"{socket.gethostname()}-{int(time.time())}"


def log(msg):
    print(msg, flush=True)
    try:
        logs.put_log_events(
            logGroupName=LOG_GROUP,
            logStreamName=STREAM,
            logEvents=[{"timestamp": int(time.time() * 1000), "message": msg}],
        )
    except Exception as e:
        print(f"cloudwatch error: {e}", flush=True)


def process(bucket, key):
    start = time.perf_counter()
    data = s3.get_object(Bucket=bucket, Key=key)["Body"].read()

    if len(data) == 0:
        raise ValueError(f"empty file: {key}")

    file_name = key.split("/", 1)[1] if "/" in key else key
    result = {
        "file_name": file_name,
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "processing_time_ms": round((time.perf_counter() - start) * 1000, 2),
    }
    s3.put_object(
        Bucket=bucket,
        Key=f"results/{file_name}.json",
        Body=json.dumps(result).encode(),
        ContentType="application/json",
    )
    log(f"processed {key}")


def handle(message):
    body = json.loads(message["Body"])

    for record in body.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        process(bucket, key)


def main():
    logs.create_log_stream(logGroupName=LOG_GROUP, logStreamName=STREAM)
    log("worker started")
    while True:
        try:
            resp = sqs.receive_message(
                QueueUrl=QUEUE_URL, MaxNumberOfMessages=1, WaitTimeSeconds=20
            )
        except Exception as e:

            log(f"receive failed: {e}")
            time.sleep(30)
            continue

        for message in resp.get("Messages", []):
            try:
                handle(message)
            except Exception as e:
                log(f"failed: {e}")
                continue
            sqs.delete_message(
                QueueUrl=QUEUE_URL, ReceiptHandle=message["ReceiptHandle"]
            )


if __name__ == "__main__":
    main()