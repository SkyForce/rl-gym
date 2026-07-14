"""Flywheel stage 1: aggregate served episodes from S3 into a training mix.

Serving (rl_gym.iac.webdemo) logs every request as a `gen` row and every repair
attempt as a `repair` row (config + findings + outcome) under
s3://$S3_BUCKET/$S3_PREFIX/served/<boot-ts>/. This script pools them, dedupes,
filters, mixes in replay episodes from the standard pool (so the generation skill
doesn't drift), and writes repair_train.jsonl — the exact file gym.train
--env iac_repair consumes.

Exit codes: 0 ok · 3 not enough served repair traffic (no cycle warranted).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", default=os.environ.get("S3_BUCKET", "green-meadowlark-bucket-7"))
    ap.add_argument("--prefix", default=os.environ.get("S3_PREFIX", "rl-gym-iac"))
    ap.add_argument("--out_dir", default="./out/flywheel")
    ap.add_argument("--min_repair", type=int, default=6,
                    help="minimum served repair episodes to justify a cycle")
    ap.add_argument("--replay", type=int, default=200,
                    help="replay gen episodes mixed in from the standard pool")
    ap.add_argument("--max_prev_chars", type=int, default=2800)
    args = ap.parse_args()

    import boto3
    s3 = boto3.client("s3", endpoint_url=os.environ.get("S3_ENDPOINT",
                      "https://storage.eu-north1.nebius.cloud"))
    rows, seen = [], set()
    n_files = 0
    pref = f"{args.prefix}/served/"
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=args.bucket, Prefix=pref):
        for o in page.get("Contents", []):
            if not o["Key"].endswith(".jsonl"):
                continue
            n_files += 1
            body = s3.get_object(Bucket=args.bucket, Key=o["Key"])["Body"].read().decode()
            for line in body.splitlines():
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = hashlib.sha1((r.get("req", "") + "|" + r.get("prev_hcl", "")
                                    + "|" + r.get("mode", "")).encode()).hexdigest()
                if key in seen:
                    continue
                seen.add(key)
                rows.append(r)

    repair = [r for r in rows if r.get("mode") == "repair" and r.get("prev_hcl")
              and r.get("findings") and len(r["prev_hcl"]) <= args.max_prev_chars
              and r.get("required")]
    gen = [r for r in rows if r.get("mode") == "gen" and r.get("req") and r.get("required")]
    print(f"[flywheel] served: {n_files} files -> {len(rows)} unique rows "
          f"({len(repair)} repair / {len(gen)} gen)")
    if len(repair) < args.min_repair:
        print(f"[flywheel] only {len(repair)} repair episodes (< {args.min_repair}) — "
              "not enough traffic for a cycle")
        raise SystemExit(3)

    # replay: standard-pool gen episodes keep the generation skill anchored
    os.environ.setdefault("RLGYM_IAC_TRAIN_N", str(max(600, args.replay * 3)))
    from rl_gym.iac.env import IacEnv
    pool = IacEnv(data_dir="real").episodes("train")
    replay = [{"mode": "gen", "id": f"replay-{e.get('id', i)}", "req": e["req"],
               "required": list(e["required"]), "oracle": ""}
              for i, e in enumerate(pool[: args.replay])]

    mix = ([{**r, "mode": "repair"} for r in repair]
           + [{**g, "oracle": ""} for g in gen]
           + replay)
    import random
    random.Random(0).shuffle(mix)
    os.makedirs(args.out_dir, exist_ok=True)
    out = os.path.join(args.out_dir, "repair_train.jsonl")
    with open(out, "w") as f:
        for r in mix:
            f.write(json.dumps(r) + "\n")
    t1 = [r.get("turn1_reward") for r in repair if r.get("turn1_reward") is not None]
    print(f"[flywheel] wrote {len(mix)} episodes ({len(repair)} served-repair + "
          f"{len(gen)} served-gen + {len(replay)} replay) -> {out}")
    if t1:
        print(f"[flywheel] served repair difficulty: mean turn-1 reward {sum(t1)/len(t1):.3f}")


if __name__ == "__main__":
    main()
