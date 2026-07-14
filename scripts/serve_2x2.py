"""Authoritative serving 2x2 on the frozen anchors: {pass@1, best-of-N} x {no-repair,
+self-repair} for one model, batched vLLM. This is the reproducible source for the
"pass@1 + repair matches best-of-N + repair" claim — same dev/holdout anchors as the
gate and flywheel_stats, so every number in the deck reconciles.

    python scripts/serve_2x2.py --model s3://.../iac-grpo-fw1 --n 60 --n_bo 4
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--n_bo", type=int, default=4)
    ap.add_argument("--max_prev_chars", type=int, default=2800)
    args = ap.parse_args()

    from vllm import LLM, SamplingParams
    from transformers import AutoTokenizer
    from rl_gym.gym.registry import get_env
    from rl_gym.gym.core import score_completion
    from rl_gym.iac.repair import findings_text, build_repair_prompt
    from rl_gym.gym.s3io import materialize

    env = get_env("iac", data_dir="real")
    p = materialize(args.model)
    tok = AutoTokenizer.from_pretrained(p)
    mnt = getattr(env, "max_new_tokens", 1024)
    llm = LLM(model=p, gpu_memory_utilization=0.85, max_model_len=4096, enforce_eager=True)

    def render(text):
        return tok.apply_chat_template([{"role": "user", "content": text}],
                                       add_generation_prompt=True, tokenize=False)

    def repair_batch(eps, pres):
        """One repair pass over the imperfect (reward<1) parseable configs; returns the
        better of (pre, repaired) per episode — the served-system score."""
        todo, prompts = [], []
        for i, (ep, (rew, txt)) in enumerate(zip(eps, pres)):
            hcl = env.parse(txt, ep)
            if rew < 1.0 and hcl and len(hcl) <= args.max_prev_chars:
                todo.append(i)
                prompts.append(render(build_repair_prompt(
                    {**ep, "prev_hcl": hcl.strip(), "findings": findings_text(hcl, ep)})))
        finals = [rew for rew, _ in pres]
        if prompts:
            outs = llm.generate(prompts, SamplingParams(temperature=0.0, max_tokens=mnt))
            for j, i in enumerate(todo):
                r2 = score_completion(env, outs[j].outputs[0].text, eps[i]).reward
                finals[i] = max(finals[i], r2)
        return sum(finals) / max(1, len(finals))

    for split in ("dev", "holdout"):
        eps = env.episodes(split)[: args.n]
        prompts = [render(env.prompt(e)) for e in eps]

        # pass@1 (greedy)
        o1 = llm.generate(prompts, SamplingParams(temperature=0.0, max_tokens=mnt))
        p1 = [(score_completion(env, o.outputs[0].text, e).reward, o.outputs[0].text)
              for e, o in zip(eps, o1)]
        p1_raw = sum(r for r, _ in p1) / len(p1)
        p1_rep = repair_batch(eps, p1)

        # best-of-N (scanner picks best of N)
        oN = llm.generate(prompts, SamplingParams(temperature=0.7, n=args.n_bo,
                                                  max_tokens=mnt, seed=0))
        bo = []
        for e, o in zip(eps, oN):
            best = max(o.outputs, key=lambda c: score_completion(env, c.text, e).reward)
            bo.append((score_completion(env, best.text, e).reward, best.text))
        bo_raw = sum(r for r, _ in bo) / len(bo)
        bo_rep = repair_batch(eps, bo)

        print(f"\n================ SERVE 2x2 — {os.path.basename(p)} on {split} (n={len(eps)}) ================")
        print(f"                     no-repair   +self-repair")
        print(f"pass@1                {p1_raw:.3f}       {p1_rep:.3f}")
        print(f"best-of-{args.n_bo}             {bo_raw:.3f}       {bo_rep:.3f}")
        print(f"  >>> pass@1+repair vs best-of-{args.n_bo}+repair: {p1_rep - bo_rep:+.3f} "
              f"(pass@1+repair {'>=' if p1_rep >= bo_rep - 0.005 else '<'} best-of-{args.n_bo}+repair)")


if __name__ == "__main__":
    main()
