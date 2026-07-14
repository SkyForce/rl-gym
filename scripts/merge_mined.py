"""Merge mined Terraform episode sources into the train file + a frozen holdout.

Inputs (same jsonl schema: id, resource_types, hcl, origin, license):
  rl_gym/iac/data/github_tf.jsonl    the original terraform-aws-modules mine
  rl_gym/iac/data/checkov_tf.jsonl   Checkov's test corpus (ingest_checkov_corpus.py)

Output:
  rl_gym/iac/data/github_tf.jsonl        train-side pool (the realdata loader's path)
  rl_gym/iac/data/mined_holdout.jsonl    ~10% content-hash-disjoint frozen eval slice

Split is deterministic (sha1 of content mod 10) so re-running with more sources never
moves an episode across the boundary — the holdout stays frozen as sources grow.
"""
from __future__ import annotations

import hashlib
import json
import os

_DATA = os.path.join(os.path.dirname(__file__), "..", "rl_gym", "iac", "data")
_SOURCES = ["github_tf.jsonl", "checkov_tf.jsonl"]


def main():
    rows, seen = [], set()
    for name in _SOURCES:
        p = os.path.join(_DATA, name)
        if not os.path.isfile(p):
            print(f"[merge_mined] {name} missing — skipped")
            continue
        n = 0
        for line in open(p):
            if not line.strip():
                continue
            r = json.loads(line)
            key = hashlib.sha1((r.get("hcl") or "|".join(r["resource_types"])).encode()).hexdigest()
            if key in seen:
                continue
            seen.add(key)
            r["_hash"] = key
            rows.append(r)
            n += 1
        print(f"[merge_mined] {name}: +{n}")

    train, hold = [], []
    for r in rows:
        (hold if int(r.pop("_hash"), 16) % 10 == 0 else train).append(r)

    with open(os.path.join(_DATA, "github_tf.jsonl"), "w") as f:
        for r in train:
            f.write(json.dumps(r) + "\n")
    with open(os.path.join(_DATA, "mined_holdout.jsonl"), "w") as f:
        for r in hold:
            f.write(json.dumps(r) + "\n")
    print(f"[merge_mined] train {len(train)} | holdout {len(hold)} (hash-disjoint, frozen)")


if __name__ == "__main__":
    main()
