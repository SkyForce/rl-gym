"""S3 (Nebius Object Storage) save/load for model checkpoints (S3:// paths).

Persists trained checkpoints to an S3-compatible bucket instead of the HF Hub.
Credentials + target come from the environment (boto3 reads the AWS_* vars):

    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY   (required)
    S3_ENDPOINT   (default https://storage.eu-north1.nebius.cloud)
    S3_REGION     (default eu-north1)
    S3_BUCKET     (required unless --bucket / an s3:// URI is given)

In a Nebius job, inject the creds from the MysteryBox secret `rl-gym-s3-creds`:
    --env-secret AWS_ACCESS_KEY_ID=rl-gym-s3-creds \
    --env-secret AWS_SECRET_ACCESS_KEY=rl-gym-s3-creds \
    --env S3_BUCKET=green-meadowlark-bucket-7

CLI:
    python -m rl_gym.gym.s3io upload   --local ./out/iac-grpo --prefix rl-gym-iac/iac-grpo
    python -m rl_gym.gym.s3io download --local ./out/iac-grpo --prefix rl-gym-iac/iac-grpo

Loaders accept an `s3://bucket/prefix` URI directly via `materialize()` (see
evaluate.hf_meal_policy), which downloads once into a local cache and returns the
path — so `compare`/`audit` can point at S3 with `--grpo s3://.../iac-grpo`.
"""
from __future__ import annotations

import argparse
import os

DEFAULT_ENDPOINT = "https://storage.eu-north1.nebius.cloud"
DEFAULT_REGION = "eu-north1"
CACHE_ROOT = os.environ.get("S3_CACHE", "/tmp/rl-gym-s3")


def _client():
    import boto3  # lazy: only needed when S3 is actually used
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("S3_ENDPOINT", DEFAULT_ENDPOINT),
        region_name=os.environ.get("S3_REGION", DEFAULT_REGION),
    )


def _need_bucket(bucket: str | None) -> str:
    bucket = bucket or os.environ.get("S3_BUCKET")
    if not bucket:
        raise SystemExit("set S3_BUCKET (or pass --bucket / an s3://bucket/... URI)")
    return bucket


def upload_dir(local_dir: str, prefix: str, bucket: str | None = None) -> int:
    """Upload the top-level files of `local_dir` to s3://bucket/prefix/.

    Only files are uploaded — sub-directories (e.g. the optimizer `checkpoint-*`
    dirs) are skipped, so the prefix holds just the inference-ready model.
    """
    s3, bucket = _client(), _need_bucket(bucket)
    prefix = prefix.strip("/")
    n = 0
    for name in sorted(os.listdir(local_dir)):
        p = os.path.join(local_dir, name)
        if not os.path.isfile(p):
            continue
        s3.upload_file(p, bucket, f"{prefix}/{name}")
        print(f"uploaded {name} ({os.path.getsize(p)})")
        n += 1
    print(f"-> s3://{bucket}/{prefix} ({n} files)")
    return n


def download_dir(prefix: str, local_dir: str, bucket: str | None = None) -> int:
    """Download every object under s3://bucket/prefix/ into `local_dir`."""
    s3, bucket = _client(), _need_bucket(bucket)
    prefix = prefix.strip("/") + "/"
    os.makedirs(local_dir, exist_ok=True)
    n = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(prefix):]
            if not rel:
                continue
            dest = os.path.join(local_dir, rel)
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            s3.download_file(bucket, obj["Key"], dest)
            print(f"downloaded {rel} ({obj['Size']})")
            n += 1
    if n == 0:
        raise SystemExit(f"no objects under s3://{bucket}/{prefix}")
    print(f"<- s3://{bucket}/{prefix} ({n} files) -> {local_dir}")
    return n


def exists(prefix: str, bucket: str | None = None) -> bool:
    """True if at least one object lives under s3://bucket/prefix/ (cache probe)."""
    s3, bucket = _client(), _need_bucket(bucket)
    prefix = prefix.strip("/") + "/"
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    return resp.get("KeyCount", 0) > 0


def usage(prefix: str = "", bucket: str | None = None) -> int:
    """Sum object bytes under s3://bucket/prefix (whole bucket if prefix empty).
    Logged at job start so we can see headroom vs the bucket size limit — the CLI
    management API needs IAM perms this job's data-plane keys don't have."""
    s3, bucket = _client(), _need_bucket(bucket)
    p = prefix.strip("/")
    kw = {"Bucket": bucket, **({"Prefix": p + "/"} if p else {})}
    total = n = 0
    for page in s3.get_paginator("list_objects_v2").paginate(**kw):
        for obj in page.get("Contents", []):
            total += obj["Size"]; n += 1
    print(f"s3://{bucket}/{p}: {n} objects, {total / 1e9:.1f} GB")
    return total


def materialize(path: str, cache_root: str = CACHE_ROOT) -> str:
    """If `path` is an s3://bucket/prefix URI, download it once into a local cache
    and return the local dir; otherwise return `path` unchanged (a no-op for HF
    ids and local paths)."""
    if not isinstance(path, str) or not path.startswith("s3://"):
        return path
    bucket, _, prefix = path[len("s3://"):].partition("/")
    local = os.path.join(cache_root, prefix.strip("/").replace("/", "_"))
    if not (os.path.isdir(local) and os.listdir(local)):
        download_dir(prefix, local, bucket=bucket)
    return local


def main():
    ap = argparse.ArgumentParser(description="Nebius S3 save/load for model checkpoints")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("upload", "download"):
        sp = sub.add_parser(name)
        sp.add_argument("--local", required=True)
        sp.add_argument("--prefix", required=True)
        sp.add_argument("--bucket")
    sp = sub.add_parser("exists")   # cache probe: exit 0 if present, non-zero if not (for shell `if`)
    sp.add_argument("--prefix", required=True)
    sp.add_argument("--bucket")
    sp = sub.add_parser("usage")    # bytes used under a prefix (whole bucket if omitted)
    sp.add_argument("--prefix", default="")
    sp.add_argument("--bucket")
    args = ap.parse_args()
    if args.cmd == "upload":
        upload_dir(args.local, args.prefix, args.bucket)
    elif args.cmd == "download":
        download_dir(args.prefix, args.local, args.bucket)
    elif args.cmd == "usage":
        usage(args.prefix, args.bucket)
    else:  # exists
        raise SystemExit(0 if exists(args.prefix, args.bucket) else 3)


if __name__ == "__main__":
    main()
