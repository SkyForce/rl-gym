"""Real-world episodes for the IaC env from IaC-Eval (autoiac-project/iac-eval).

IaC-Eval is a human-curated NL->Terraform benchmark (458 rows, CC-BY-4.0 — see
https://huggingface.co/datasets/autoiac-project/iac-eval). Mapping to gym episodes:

    Prompt           -> req        (a real human-written infra request)
    Reference output -> oracle     (a *functional* reference — NOT a security ceiling:
                                    these score ~0.45 on our scanner and ~19% carry a
                                    CRITICAL, so the eval's "oracle" row shows the real
                                    security posture of human-written Terraform, which a
                                    security-trained policy can legitimately beat)
    resource blocks  -> required   (derived from the reference via resource_types(),
                                    so the builds_required gate stays ground-truthed)
    Difficulty       -> difficulty (1-6; curriculum knob)

The CSV is committed at rl_gym/iac/data/iac_eval.csv (attribution above); if missing
(e.g. a slim checkout) it is fetched once from the HF Hub.
"""
from __future__ import annotations

import csv
import os
import sys
import urllib.request

from .scan import resource_types

_HERE = os.path.dirname(__file__)
_CSV = os.path.join(_HERE, "data", "iac_eval.csv")
_HF_URL = "https://huggingface.co/datasets/autoiac-project/iac-eval/resolve/main/data.csv"


def _ensure_csv(path: str = _CSV) -> str:
    if not os.path.isfile(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        print(f"[iac.realdata] fetching IaC-Eval from HF Hub -> {path}")
        urllib.request.urlretrieve(_HF_URL, path)
    return path


_GH_JSONL = os.path.join(_HERE, "data", "github_tf.jsonl")
# frozen mined holdout — episodes from repos/files the train mix NEVER sees (content-hash
# disjoint split in scripts/merge_mined.py); the second, never-trained-on OOD benchmark
_HOLDOUT_JSONL = os.path.join(_HERE, "data", "mined_holdout.jsonl")


def load_holdout_episodes() -> list:
    return load_github_episodes(_HOLDOUT_JSONL)

# friendly names for the request template (fallback: the raw type)
_FRIENDLY = {
    "aws_s3_bucket": "an S3 bucket", "aws_db_instance": "an RDS database instance",
    "aws_instance": "an EC2 instance", "aws_security_group": "a security group",
    "aws_iam_role": "an IAM role", "aws_iam_policy": "an IAM policy",
    "aws_iam_role_policy": "an IAM role policy",
    "aws_iam_role_policy_attachment": "an IAM role policy attachment",
    "aws_kms_key": "a KMS key", "aws_dynamodb_table": "a DynamoDB table",
    "aws_lambda_function": "a Lambda function",
    "aws_cloudwatch_log_group": "a CloudWatch log group",
    "aws_sqs_queue": "an SQS queue", "aws_sns_topic": "an SNS topic",
    "aws_vpc": "a VPC", "aws_subnet": "subnets", "aws_lb": "a load balancer",
}


def load_github_episodes(path: str | None = None) -> list:
    """Episodes mined from real GitHub Terraform (terraform-aws-modules, Apache-2.0;
    see scripts/mine_github_tf.py). The request is templated from the real config's
    resource types; the reward needs no reference, so even large module files train
    fine (their `hcl` is None). Small files keep the real HCL as a reference."""
    import json
    eps = []
    p = path or _GH_JSONL
    if not os.path.isfile(p):   # optional dataset — degrade, don't kill a paid job (v11)
        print(f"[iac.realdata] {p} missing — GitHub episodes skipped (run scripts/mine_github_tf.py)")
        return eps
    with open(p) as f:
        for line in f:
            r = json.loads(line)
            names = ", ".join(_FRIENDLY.get(t, f"a {t} resource") for t in r["resource_types"])
            eps.append({
                "id": r["id"],
                "req": (f"production-grade AWS infrastructure (as used in real Terraform "
                        f"modules) composed of: {names}"),
                "required": list(r["resource_types"]),
                "oracle": r["hcl"] or "",   # training-only rows have no reference
                "difficulty": len(r["resource_types"]),
            })
    return eps


def load_real_episodes(path: str | None = None) -> list:
    """All usable IaC-Eval rows as gym episodes (deterministic order)."""
    csv.field_size_limit(10**8)
    eps = []
    with open(_ensure_csv(path or _CSV), newline="") as f:
        for i, r in enumerate(csv.DictReader(f)):
            ref = (r.get("Reference output") or "").strip()
            req = (r.get("Prompt") or "").strip()
            required = sorted(set(resource_types(ref)))
            if not ref or not req or not required:   # data-source-only rows have no resource blocks
                continue
            eps.append({
                "id": f"real-{i}",
                "req": req,
                "required": required,
                "oracle": ref,
                "difficulty": int(r.get("Difficulty") or 0),
            })
    return eps
