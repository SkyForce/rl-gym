"""S3 bucket cleanup — list objects grouped by top-level prefix, keep a whitelist,
delete the rest. DRY-RUN by default (prints the plan); set CONFIRM=1 to actually delete.

Runs inside a Nebius job with the S3 creds from the rl-gym-s3-creds secret. Never touches
anything in KEEP. Prints a compact summary that survives log truncation.

  env: S3_BUCKET, S3_PREFIX (default rl-gym-iac), S3_ENDPOINT, CONFIRM (1=delete),
       KEEP (comma list; overrides the default keep-set)
"""
import os
import sys
from collections import defaultdict

import boto3

BUCKET = os.environ["S3_BUCKET"]
PREFIX = os.environ.get("S3_PREFIX", "rl-gym-iac").rstrip("/")
ENDPOINT = os.environ.get("S3_ENDPOINT", "https://storage.eu-north1.nebius.cloud")
CONFIRM = os.environ.get("CONFIRM", "") == "1"

# Needed for the demo + the article. Conservative: keep anything cited anywhere.
DEFAULT_KEEP = [
    "iac-grpo",         # default demo model (unversioned)
    "iac-grpo-v16r",    # article headline (0.694 / 0.876)
    "iac-grpo-fw1",     # flywheel-promoted serving/demo model
    "iac-grpo-v18",     # self-repair system (article 0.802)
    "iac-grpo-drift",   # continual-learning demo checkpoint
    "base",             # base-weights cache (needed to load/serve)
    "served",           # flywheel served logs (input to rule gap-mining)
    "evalres",          # eval result tables (small; article numbers)
]
KEEP = set(x.strip() for x in os.environ.get("KEEP", ",".join(DEFAULT_KEEP)).split(",") if x.strip())


def group_of(key: str) -> str:
    rest = key[len(PREFIX):].lstrip("/")
    parts = rest.split("/")
    return parts[0] if len(parts) > 1 else "(root files)"


def human(n: int) -> str:
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or u == "TiB":
            return f"{n:.1f}{u}"
        n /= 1024


def main():
    s3 = boto3.client("s3", endpoint_url=ENDPOINT,
                      region_name=os.environ.get("S3_REGION", "eu-north1"))
    sizes, counts = defaultdict(int), defaultdict(int)
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=PREFIX + "/"):
        for obj in page.get("Contents", []):
            g = group_of(obj["Key"])
            sizes[g] += obj["Size"]
            counts[g] += 1

    # DEL=comma,list — targeted delete of specific groups (e.g. failed retrain checkpoints),
    # instead of the KEEP-whitelist. Absent groups are skipped. Dry-run unless CONFIRM=1.
    del_list = [x.strip() for x in os.environ.get("DEL", "").split(",") if x.strip()]
    if del_list:
        tgt = [g for g in del_list if g in sizes]
        print(f"\n=== TARGETED DELETE from {BUCKET}/{PREFIX} ===")
        for g in del_list:
            print(f"  {g:32s} {'present ' + human(sizes[g]) if g in sizes else 'ABSENT — skip'}")
        freed = sum(sizes[g] for g in tgt)
        print(f"\nwould free {human(freed)} across {len(tgt)} groups")
        if not CONFIRM:
            print("DRY-RUN (set CONFIRM=1 to delete). Nothing changed."); return
        for g in tgt:
            batch = []
            for page in paginator.paginate(Bucket=BUCKET, Prefix=f"{PREFIX}/{g}/"):
                for obj in page.get("Contents", []):
                    batch.append({"Key": obj["Key"]})
                    if len(batch) == 1000:
                        s3.delete_objects(Bucket=BUCKET, Delete={"Objects": batch}); batch = []
            if batch:
                s3.delete_objects(Bucket=BUCKET, Delete={"Objects": batch})
            print(f"  deleted {g}")
        print(f"\ndone — freed ~{human(freed)}.")
        return

    total = sum(sizes.values())
    keep_groups = [g for g in sizes if g in KEEP]
    del_groups = [g for g in sizes if g not in KEEP]
    keep_sz = sum(sizes[g] for g in keep_groups)
    del_sz = sum(sizes[g] for g in del_groups)

    print(f"\n=== BUCKET {BUCKET}/{PREFIX}  total {human(total)} across {sum(counts.values())} objects ===")
    print(f"{'GROUP':32s} {'OBJS':>7s} {'SIZE':>10s}  ACTION")
    for g in sorted(sizes, key=lambda g: -sizes[g]):
        act = "KEEP" if g in KEEP else "DELETE"
        print(f"{g:32s} {counts[g]:>7d} {human(sizes[g]):>10s}  {act}")
    print(f"\nKEEP  {len(keep_groups):2d} groups  {human(keep_sz)}")
    print(f"FREE  {len(del_groups):2d} groups  {human(del_sz)}   <-- reclaimed")

    if not CONFIRM:
        print("\nDRY-RUN (set CONFIRM=1 to delete the DELETE groups). Nothing changed.")
        return
    if not del_groups:
        print("\nnothing to delete."); return
    print(f"\nCONFIRM=1 — deleting {len(del_groups)} groups ({human(del_sz)}) …")
    deleted = 0
    for g in del_groups:
        gp = f"{PREFIX}/{g}/" if g != "(root files)" else PREFIX + "/"
        batch = []
        for page in paginator.paginate(Bucket=BUCKET, Prefix=gp):
            for obj in page.get("Contents", []):
                batch.append({"Key": obj["Key"]})
                if len(batch) == 1000:
                    s3.delete_objects(Bucket=BUCKET, Delete={"Objects": batch}); deleted += len(batch); batch = []
        if batch:
            s3.delete_objects(Bucket=BUCKET, Delete={"Objects": batch}); deleted += len(batch)
        print(f"  deleted group {g}")
    print(f"\ndone — deleted {deleted} objects, reclaimed ~{human(del_sz)}.")


if __name__ == "__main__":
    main()
