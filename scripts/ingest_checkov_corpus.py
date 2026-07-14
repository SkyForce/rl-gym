"""Harvest Checkov's Terraform test corpus into IaC-env episodes.

bridgecrewio/checkov (Apache-2.0) ships thousands of small, hand-written Terraform
examples under tests/terraform/** — one pass/fail pair per policy. They are ideal
episode material: real-world idioms, tightly coupled to the exact rule families our
scanner scores, permissively licensed, and small enough to keep as references.

Output rows use the same schema as scripts/mine_github_tf.py (id, resource_types,
hcl, origin, license), so scripts/merge_mined.py can pool both sources.

Usage:
    python scripts/ingest_checkov_corpus.py            # clones (sparse) to scratch
    python scripts/ingest_checkov_corpus.py --src /path/to/existing/checkov
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from rl_gym.iac.scan import resource_types  # noqa: E402

MAX_CHARS = 3500          # must fit the 1024-token completion budget as a reference
MIN_AWS_TYPES = 1
MAX_TYPES = 6


def sparse_clone(dst: str) -> str:
    subprocess.run(["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse",
                    "https://github.com/bridgecrewio/checkov.git", dst], check=True)
    subprocess.run(["git", "-C", dst, "sparse-checkout", "set", "tests/terraform"], check=True)
    return dst


def harvest(src: str):
    rows, seen = [], set()
    n_files = 0
    for root, dirs, files in os.walk(os.path.join(src, "tests", "terraform")):
        dirs[:] = [d for d in dirs if d not in {".git"}]
        for name in sorted(files):
            if not name.endswith(".tf"):
                continue
            n_files += 1
            try:
                hcl = open(os.path.join(root, name), encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            if len(hcl) > MAX_CHARS:
                continue
            types = sorted({t for t in set(resource_types(hcl)) if t.startswith("aws_")})
            if not (MIN_AWS_TYPES <= len(types) <= MAX_TYPES):
                continue
            h = hashlib.sha1(hcl.encode()).hexdigest()[:16]
            if h in seen:          # pass/fail pairs often share boilerplate
                continue
            seen.add(h)
            rows.append({
                "id": f"ckv-{h}",
                "resource_types": types,
                "hcl": hcl,
                "origin": "bridgecrewio/checkov",
                "license": "Apache-2.0",
            })
    return rows, n_files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="", help="existing checkov checkout (else sparse-clone)")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..",
                                                  "rl_gym", "iac", "data", "checkov_tf.jsonl"))
    args = ap.parse_args()

    src = args.src
    tmp = None
    if not src:
        tmp = tempfile.mkdtemp(prefix="checkov-corpus-")
        src = sparse_clone(tmp)
    rows, n_files = harvest(src)
    with open(args.out, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"[ingest_checkov] {len(rows)} episodes from {n_files} .tf files -> {args.out}")


if __name__ == "__main__":
    main()
