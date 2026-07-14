"""KMS-specific eval — settle whether SFT-inject closed the discovered blind spot.

The 60-episode aggregate can't resolve this: aws_kms_key episodes are ~9% of the real
benchmark (dev 4 + holdout 3), so even a complete fix moves the aggregate by ~0.02 —
inside the noise. This isolates the signal: on KMS episodes ONLY, what fraction of a
model's outputs PASS the kms_key_policy rule (a key policy present on every aws_kms_key)?

Two clean held-out test sets, deterministic greedy decode (no sampling noise):
  - real-heldout : dev + holdout KMS episodes (real IaC-Eval / Checkov-mined; OOD, the
                   model never trained on these specific requests).
  - param-heldout: fresh-seed parametric KMS episodes (in-distribution generator, but a
                   seed disjoint from training's seed-0 pool -> held-out instances).

Reports kms_key_policy pass/applicable + rate + avg reward per model, fw1 vs fw2.
"""
import os
import sys
# run as `python scripts/kms_eval.py` -> sys.path[0] is scripts/, not the repo root, so
# `import rl_gym` fails. Add the repo root (parent of scripts/) explicitly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RLGYM_IAC_DISCOVERED_RULES", "1")   # score with the kms rule ON
import argparse
from rl_gym.iac.env import IacEnv
from rl_gym.iac import scan as s
from rl_gym.iac.tasks import sample_episodes
from rl_gym.gym.core import score_completion
from rl_gym.gym.eval import vllm_policy

ap = argparse.ArgumentParser()
ap.add_argument("--models", required=True, help="comma list of label=path (path may be s3://...)")
ap.add_argument("--param_n", type=int, default=40)
ap.add_argument("--param_seed", type=int, default=7, help="disjoint from train's seed-0 pool")
ap.add_argument("--tok_fallback", default="ibm-granite/granite-4.1-8b")
args = ap.parse_args()

env = IacEnv(data_dir="real")

real_kms = [e for e in (env.episodes("dev") + env.episodes("holdout"))
            if "aws_kms_key" in (e.get("required") or [])]
param_kms = [e for e in sample_episodes(1200, args.param_seed)
             if "aws_kms_key" in (e.get("required") or [])][: args.param_n]
sets = [("real-heldout", real_kms), ("param-heldout", param_kms)]
print(f"[kms_eval] real-heldout={len(real_kms)}  param-heldout={len(param_kms)}  "
      f"(param_seed={args.param_seed})", flush=True)

for spec in args.models.split(","):
    label, path = spec.split("=", 1)
    all_eps = [ep for _, eps in sets for ep in eps]
    # vllm_policy pre-generates for exactly `all_eps`, in order -> call in order.
    pol = vllm_policy(env, path, all_eps, tok_fallback=args.tok_fallback)
    gens = [pol(ep) for ep in all_eps]
    idx = 0
    print(f"\n===== {label}  ({path}) =====", flush=True)
    for set_name, eps in sets:
        npass = nfail = nna = 0
        rewards = []
        for ep in eps:
            gen = gens[idx]; idx += 1
            hcl = env.parse(gen, ep) or ""
            r = s._r_kms_key_policy(hcl)
            if r == "pass":
                npass += 1
            elif r == "fail":
                nfail += 1
            else:
                nna += 1
            rewards.append(score_completion(env, gen, ep).reward)
        appl = npass + nfail
        rate = npass / appl if appl else 0.0
        avg = sum(rewards) / len(rewards) if rewards else 0.0
        print(f"  {set_name:15s}  kms_policy_pass={npass:2d}/{appl:<2d} ({rate:6.1%})  "
              f"na={nna:2d}  avg_reward={avg:.3f}", flush=True)
