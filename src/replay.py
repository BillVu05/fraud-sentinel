"""Replay the labelled holdout window into Kinesis as if it were live traffic.

Usage:
    python src/replay.py --n 1000 --rate 50

The holdout is the last 20% of TransactionDT, the same slice train.py scored
offline, so anything the stream produces can be checked against a known label.
"""

import argparse
import json
import time

import boto3
import pandas as pd

import train as train_mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/train_transaction.csv")
    ap.add_argument("--stream", default="fraud-sentinel-txn")
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--rate", type=float, default=50, help="records per second")
    args = ap.parse_args()

    _, holdout = train_mod.time_split(train_mod.load(args.data))
    batch = holdout.head(args.n).where(pd.notna(holdout.head(args.n)), None)

    kinesis = boto3.client("kinesis")
    sent, t0 = 0, time.time()

    # ponytail: one PutRecord per transaction. put_records batches 500 at a time
    # and is ~10x cheaper in API calls, but one-at-a-time is what makes the
    # per-transaction latency in CloudWatch mean anything. Switch if the demo
    # ever needs real throughput rather than a real latency number.
    for txn in batch.to_dict("records"):
        kinesis.put_record(
            StreamName=args.stream,
            Data=json.dumps(txn, default=str).encode(),
            PartitionKey=str(txn.get("card1", "unknown")),
        )
        sent += 1
        if sent % 100 == 0:
            print(f"  sent {sent}/{len(batch)}")
        time.sleep(max(0, (sent / args.rate) - (time.time() - t0)))

    print(f"sent {sent} records in {time.time() - t0:.1f}s")
    print(f"actual fraud in this slice: {int(batch.isFraud.sum())}/{len(batch)}")


if __name__ == "__main__":
    main()
