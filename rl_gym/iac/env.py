"""IaC environment — generate secure Terraform, scored by a security scanner.

The verifiable reward is the scanner's **pass-rate** (security posture). Two hard
gates keep it honest:
  * builds_required — the config must declare the resources the task asked for.
    This is what stops the canonical reward-hack: an *empty* config passes every
    security check (nothing to flag → pass-rate 1.0) but builds nothing. The gate
    zeroes it.
  * no_critical — any critical finding (public bucket, world-open SSH, hardcoded
    secret, public RDS, IAM wildcard) zeroes the reward; criticals must be fixed,
    not traded off.

Same Reward = Components + Gates shape as the gym core, so the trainer, eval, and
reward-hacking audit work unchanged. The task *dataset* comes from the parametric
generator in `rl_gym.iac.tasks` (hundreds of distinct requests → GRPO groups have
real variance). Reward-hack tell: the policy collapses to one template config
(uniq% drops) or learns the empty-config exploit (gate catches it).
"""
from __future__ import annotations

import random as _random
import re
from dataclasses import dataclass
from typing import Optional

from ..gym.core import Component, Gate, Reward, Environment
from .scan import scan, resource_types, resource_bodies
from .tasks import sample_episodes
from .realdata import load_real_episodes, load_github_episodes, load_holdout_episodes

_BARE = {  # minimal, insecure-but-not-critical block per resource type (the floor baseline)
    "aws_s3_bucket": 'resource "aws_s3_bucket" "b" { bucket = "x" }',
    "aws_security_group": 'resource "aws_security_group" "b" {\n  ingress { from_port = 443 to_port = 443 protocol = "tcp" cidr_blocks = ["0.0.0.0/0"] }\n}',
    "aws_instance": 'resource "aws_instance" "b" { ami = "ami-1" instance_type = "t3.micro" }',
    "aws_db_instance": 'resource "aws_db_instance" "b" { engine = "postgres" instance_class = "db.t3.micro" }',
    "aws_iam_role": 'resource "aws_iam_role" "b" { name = "r" assume_role_policy = jsonencode({}) }',
    "aws_iam_role_policy": 'resource "aws_iam_role_policy" "b" { role = "r" policy = jsonencode({}) }',
    "aws_ebs_volume": 'resource "aws_ebs_volume" "b" { availability_zone = "us-east-1a" size = 8 }',
    "aws_kms_key": 'resource "aws_kms_key" "b" { description = "x" }',
    "aws_dynamodb_table": 'resource "aws_dynamodb_table" "b" {\n  name = "x" hash_key = "id"\n  attribute { name = "id" type = "S" }\n}',
    "aws_lambda_function": 'resource "aws_lambda_function" "b" { function_name = "x" role = "arn:aws:iam::1:role/r" handler = "i.h" runtime = "python3.12" filename = "a.zip" }',
    "aws_cloudwatch_log_group": 'resource "aws_cloudwatch_log_group" "b" { name = "x" }',
    "aws_vpc": 'resource "aws_vpc" "b" { cidr_block = "10.0.0.0/16" }',
    "aws_flow_log": 'resource "aws_flow_log" "b" { vpc_id = "vpc-1" traffic_type = "ALL" log_destination = "arn:x" }',
}

_CRITICAL = {"critical"}


def _criticals(hcl) -> list:
    return [f for f in scan(hcl)["findings"] if f[1] in _CRITICAL]


# Equivalent resource choices: demanding the literal type name zeroes functionally
# correct configs (e.g. both 7Bs express "role + policy" as the managed
# aws_iam_policy + attachment pattern, arguably better practice than the inline
# aws_iam_role_policy the reference happened to use). The gate should test intent
# ("a policy is attached"), not the author's stylistic pick — the same
# over-syntactic-check failure, just inverted.
_EQUIV = {
    "aws_iam_role_policy": ("aws_iam_policy", "aws_iam_role_policy_attachment"),
    "aws_iam_policy": ("aws_iam_role_policy",),
    "aws_iam_role_policy_attachment": ("aws_iam_role_policy", "aws_iam_policy"),
    "aws_iam_user_policy": ("aws_iam_policy",),
    "aws_s3_bucket_acl": ("aws_s3_bucket",),          # acl often set inline on the bucket
    "aws_instance": ("aws_launch_template",),
}


def _builds_required(ep, hcl) -> bool:
    """Every required type must be declared AND actually configured (>=1 attribute or
    nested block) — an empty stub `resource "t" "x" {}` is a syntactic
    bypass. A configured member of the type's equivalence family also satisfies it."""
    bodies = resource_bodies(hcl)
    def configured(rt):
        return any(("=" in b) or ("{" in b) for b in bodies.get(rt, []))
    return all(configured(rt) or any(configured(alt) for alt in _EQUIV.get(rt, ()))
               for rt in ep["required"])


def make_iac_reward(saturate: float = 1.0) -> Reward:
    return Reward(
        components=[Component("security", 1.0, lambda ep, hcl: scan(hcl)["pass_rate"])],
        gates=[
            Gate("builds_required", _builds_required),
            Gate("no_critical", lambda ep, hcl: len(_criticals(hcl)) == 0),
        ],
        format_bonus=0.0,
        saturate=saturate,
    )


@dataclass
class IacEnv:
    """`Environment` for secure-Terraform generation. `data_dir` unused (tasks are
    generated); kept for a uniform constructor signature with other envs."""
    data_dir: Optional[str] = None
    saturate: float = 1.0
    name: str = "iac"

    def __post_init__(self):
        self.reward = make_iac_reward(self.saturate)
        self.max_new_tokens = 1024        # match the GRPO train cap; stacks + real refs are long
        self.use_chat_template = True     # instruction-style prompt; needs the Instruct chat template

    def episodes(self, split: str) -> list:
        # Default: parametric generator (rl_gym.iac.tasks) — hundreds of distinct requests
        # at varied difficulty, so GRPO groups actually have reward variance to learn from.
        # data_dir="real": train mixes parametric + IaC-Eval + GitHub-mined
        # (terraform-aws-modules) requests — the reward needs no reference, so real
        # episodes train fine even though their configs aren't security-hardened.
        # Dev stays all-IaC-Eval: a human-curated benchmark of human-written requests.
        import os
        seed = 0 if split == "train" else 1
        # RLGYM_IAC_TRAIN_N: train-pool size knob — with the RAFT difficulty-band filter
        # shrinking the pool, a larger unique pool keeps GRPO under one epoch (no repeats)
        n = int(os.environ.get("RLGYM_IAC_TRAIN_N", "600")) if split == "train" else 120
        if self.data_dir == "real":
            if split == "holdout":
                # second frozen OOD benchmark: mined episodes hash-disjoint from the
                # train mix, from a source (Checkov corpus/GitHub) we never sample into
                # training — stays virgin even though IaC-Eval's train half is in-mix
                return load_holdout_episodes()[:120]
            # DISJOINT split of IaC-Eval: dev = fixed 120 (seed-1 shuffle — identical
            # benchmark to all prior runs), train draws ONLY from the other 335.
            # (Pre-fix, train sampled from all 455 → 67% of dev leaked into v16's mix.)
            pool = load_real_episodes()
            _random.Random(1).shuffle(pool)
            dev, train_real = pool[:120], pool[120:]
            if split != "train":
                return dev[:n]
            mixed = (sample_episodes(n // 3, seed)
                     + [dict(e) for e in train_real]
                     + [dict(e) for e in load_github_episodes()])
            # RLGYM_IAC_BOOST_TYPE: oversample episodes that declare a target resource type,
            # so a sparse-but-important rule (e.g. kms_key_policy, ~11% of the pool) gets real
            # gradient in a targeted continual update instead of being drowned out.
            _bt = os.environ.get("RLGYM_IAC_BOOST_TYPE")
            if _bt:
                _bn = max(1, int(os.environ.get("RLGYM_IAC_BOOST_N", "5")))
                _hit = [dict(e) for e in mixed if _bt in (e.get("required") or [])]
                mixed = mixed + _hit * (_bn - 1)
            _random.Random(seed).shuffle(mixed)
            return mixed[:n]
        return sample_episodes(n, seed)

    def prompt(self, ep) -> str:
        # RLGYM_PROMPT_SUFFIX: model-control tokens appended uniformly across train/eval/
        # demo — e.g. " /no_think" flips Qwen3's hybrid-thinking off with zero template
        # plumbing (one mechanism everywhere beats per-tool chat_template kwargs).
        import os
        return ("You are a senior DevOps engineer. Write a SECURE Terraform (HCL) configuration "
                f"that provisions: {ep['req']}.\n"
                f"It must declare these resources: {', '.join(ep['required'])}.\n"
                "Follow security best practices (encryption at rest, least privilege, no public "
                "access, no hardcoded secrets, tags). Output ONLY the HCL in a ```hcl code block."
                + os.environ.get("RLGYM_PROMPT_SUFFIX", ""))

    def parse(self, completion: str, ep) -> Optional[str]:
        m = re.search(r"```(?:hcl|terraform|tf)?\s*(.*?)```", completion, re.S)
        hcl = m.group(1) if m else completion
        return hcl if resource_types(hcl) else None      # no resources -> malformed

    def oracle(self, ep) -> str:
        return "```hcl\n" + ep["oracle"] + "\n```"

    def random(self, ep) -> str:
        # fallback stub carries one attribute — an empty {} now (rightly) fails the
        # hardened builds_required gate, and the floor policy should stay a valid floor
        body = "\n".join(_BARE.get(rt, f'resource "{rt}" "b" {{ name = "b" }}') for rt in ep["required"])
        return "```hcl\n" + body + "\n```"


_: Environment = IacEnv()
