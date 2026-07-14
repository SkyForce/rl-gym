"""Flywheel gain at pass@1 vs best-of-4: eval incumbent + candidate at both sampling
modes on the frozen anchors, print the table. Answers "does the flywheel lift pass@1
as much as best-of-n?" — which decides whether best-of-n is still worth its cost.

    python scripts/flywheel_stats.py \
        --incumbent s3://.../iac-grpo-v18 --candidate s3://.../iac-grpo-fw1 --n_bo 4
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--incumbent", required=True)
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--n", type=int, default=60, help="episodes per anchor")
    ap.add_argument("--n_bo", type=int, default=4, help="best-of-n sample count")
    args = ap.parse_args()

    from rl_gym.gym.registry import get_env
    from rl_gym.gym.eval import vllm_policy, evaluate_policy

    env = get_env("iac", data_dir="real")
    anchors = {"dev": env.episodes("dev")[: args.n],
               "holdout": env.episodes("holdout")[: args.n]}
    models = [("incumbent", args.incumbent), ("candidate", args.candidate)]

    rows = {}
    for split, eps in anchors.items():
        for mlabel, path in models:
            for mode, nn in (("pass@1", 1), (f"best-of-{args.n_bo}", args.n_bo)):
                pol = vllm_policy(env, path, eps, n=nn)
                m = evaluate_policy(env, pol, eps)
                rows[(split, mlabel, mode)] = m["reward"]
                print(f"  {split:8s} {mlabel:10s} {mode:12s} reward={m['reward']:.4f} "
                      f"gate={m['gate_rate']*100:.1f}%")

    print("\n================ FLYWHEEL GAIN: pass@1 vs best-of-%d ================" % args.n_bo)
    print(f"{'anchor':10s}{'mode':14s}{'incumbent':>11s}{'candidate':>11s}{'flywheel Δ':>12s}")
    for split in anchors:
        for mode in ("pass@1", f"best-of-{args.n_bo}"):
            inc = rows[(split, "incumbent", mode)]
            cand = rows[(split, "candidate", mode)]
            print(f"{split:10s}{mode:14s}{inc:>11.4f}{cand:>11.4f}{cand-inc:>+12.4f}")


if __name__ == "__main__":
    main()
