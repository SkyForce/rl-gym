"""Promotion gate — the ratchet as an executable verdict.

Compares a candidate checkpoint against the incumbent on the frozen anchors
(real dev split + never-trained holdout) with the batched vLLM evaluator, checks
the training-time collapse alarm, and exits 0 (PROMOTE) or 2 (BLOCK). The flywheel
calls this after every continual update; nothing ships on vibes.

    python -m rl_gym.gym.gate --candidate ./out/iac-grpo-fw \
        --incumbent s3://bucket/rl-gym-iac/iac-grpo-v18 --n 60
"""
from __future__ import annotations

import argparse
import json
import os


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="iac")
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--incumbent", required=True)
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--tol", type=float, default=0.01,
                    help="max allowed regression vs incumbent on either anchor")
    args = ap.parse_args()

    from .registry import get_env
    from .eval import vllm_policy, evaluate_policy

    env = get_env(args.env, data_dir="real")
    anchors = {"dev": env.episodes("dev")[: args.n],
               "holdout": env.episodes("holdout")[: args.n]}

    verdict = {"tol": args.tol, "n": args.n, "anchors": {}}
    ok = True

    alarm = os.path.isfile(os.path.join(args.candidate, "COLLAPSE_ALARM")) \
        if os.path.isdir(args.candidate) else False
    verdict["collapse_alarm"] = alarm
    if alarm:
        ok = False

    for name, eps in anchors.items():
        scores = {}
        for label, path in (("candidate", args.candidate), ("incumbent", args.incumbent)):
            pol = vllm_policy(env, path, eps, tok_fallback="")
            scores[label] = evaluate_policy(env, pol, eps)
        d = scores["candidate"]["reward"] - scores["incumbent"]["reward"]
        passed = d >= -args.tol
        verdict["anchors"][name] = {"candidate": scores["candidate"],
                                    "incumbent": scores["incumbent"],
                                    "delta": round(d, 4), "pass": passed}
        ok = ok and passed

    verdict["promote"] = ok
    print("GATE_VERDICT " + json.dumps(verdict))
    print(f"GATE: {'PROMOTE' if ok else 'BLOCK'} "
          + " ".join(f"{k}:{v['delta']:+.3f}" for k, v in verdict["anchors"].items())
          + (" COLLAPSE_ALARM" if alarm else ""))
    raise SystemExit(0 if ok else 2)


if __name__ == "__main__":
    main()
