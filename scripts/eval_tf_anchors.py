"""Re-eval the frozen anchors over Nebius Token Factory — GPU-free. Generation runs
per-token on Token Factory (`tf_policy`); scoring uses the LOCAL scanner, so whatever
rules are active locally (incl. the just-hardened ones) are what's measured. No GPU,
no git push, no S3 — just a Token Factory model id + the key.

    export TOKEN_FACTORY_API_KEY=...
    python scripts/eval_tf_anchors.py --model <fw1-tf-id> --split dev --n 60
    python scripts/eval_tf_anchors.py --model <fw1-tf-id> --split holdout --n 60
    python scripts/eval_tf_anchors.py --model <fw1-tf-id> --split all           # both anchors

Serving mode = pass@1 + self-repair (the serving default). Pass --repair 0 for bare pass@1.
Pre-hardening fw1 baselines (README serving 2x2, n=60): dev 0.865 / holdout 0.963.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rl_gym.iac.env import IacEnv
from rl_gym.iac import scan as scan_mod
from rl_gym.iac.repair import findings_text, build_repair_prompt
from rl_gym.gym.core import score_completion
from rl_gym.gym.llm_client import TokenFactory

BASELINE = {"dev": 0.865, "holdout": 0.963}   # pre-hardening fw1, pass@1+repair, n=60


def _gates_ok(rb) -> bool:
    return bool(rb.gates) and all(rb.gates.values())


def eval_split(tf, model, env, episodes, repair, max_new_tokens):
    rewards, gate_hits, rep_attempts, converted = [], 0, 0, 0
    for i, ep in enumerate(episodes):
        c1 = tf.chat(model, [{"role": "user", "content": env.prompt(ep)}],
                     temperature=0.0, max_tokens=max_new_tokens)
        rb = score_completion(env, c1, ep)
        hcl1 = env.parse(c1, ep) or ""
        if repair and rb.reward < 1.0 and hcl1:
            rep_attempts += 1
            fnd = findings_text(hcl1, ep)
            c2 = tf.chat(model, [{"role": "user", "content": build_repair_prompt(
                {**ep, "prev_hcl": hcl1, "findings": fnd})}],
                temperature=0.0, max_tokens=max_new_tokens)
            rb2 = score_completion(env, c2, ep)
            if rb2.reward >= rb.reward:
                if rb2.reward > rb.reward:
                    converted += 1
                rb = rb2
        rewards.append(rb.reward)
        gate_hits += 1 if _gates_ok(rb) else 0
        print(f"  [{i+1}/{len(episodes)}] reward={rb.reward:.3f} gate={'ok' if _gates_ok(rb) else 'FAIL'}",
              end="\r", flush=True)
    n = max(1, len(rewards))
    print(" " * 60, end="\r")
    return {"n": len(rewards), "reward": sum(rewards) / n, "gate": gate_hits / n,
            "repair_attempts": rep_attempts, "converted": converted}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="Token Factory model id (your uploaded fw1, or a shared model to test)")
    ap.add_argument("--split", default="dev", help="dev | holdout | all")
    ap.add_argument("--n", type=int, default=60, help="episodes per split (anchors are 60)")
    ap.add_argument("--repair", type=int, default=1, help="1 = pass@1 + self-repair (default); 0 = bare pass@1")
    ap.add_argument("--max_new_tokens", type=int, default=1024)
    args = ap.parse_args()

    tf = TokenFactory()
    if not tf.available():
        raise SystemExit("set TOKEN_FACTORY_API_KEY")
    env = IacEnv(data_dir="real")
    splits = ["dev", "holdout"] if args.split == "all" else [args.split]
    active = len(scan_mod.RULES)
    mode = "pass@1 + self-repair" if args.repair else "pass@1"
    print(f"anchor re-eval · model={args.model} · {mode} · scanner={active} local rules "
          f"(hardened)\n")

    for sp in splits:
        eps = env.episodes(sp)[: args.n]
        print(f"[{sp}] {len(eps)} episodes …")
        r = eval_split(tf, args.model, env, eps, bool(args.repair), args.max_new_tokens)
        base = BASELINE.get(sp)
        delta = f"  (was {base:.3f}, Δ {r['reward']-base:+.3f})" if base else ""
        print(f"[{sp}] reward {r['reward']:.3f} · gate {r['gate']*100:.1f}%{delta}")
        if r["repair_attempts"]:
            print(f"        repair fired {r['repair_attempts']}/{r['n']}, improved {r['converted']}")
    print("\nnote: Δ vs the pre-hardening baseline is the effect of the tightened rules.")


if __name__ == "__main__":
    main()
