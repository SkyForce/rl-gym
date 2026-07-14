"""RAFT stage — rejection-sampling fine-tuning data for any gym Environment.

Samples n completions per *train* prompt with the current (base) model, scores them
with the env's verifiable reward, and writes:

  <out_dir>/sft.jsonl    the best completion per episode, kept only if reward >= --keep
                         (chat `messages` rows — drop-in for gym.sft --data_file).
                         The SFT stage then imitates the model's own scanner-perfect
                         outputs on REAL prompts instead of templated oracles (RAFT;
                         Dong et al. "RAFT: Reward rAnked FineTuning").
  <out_dir>/stats.jsonl  per-episode difficulty {idx, id, mean, max, frac_perfect} —
                         the map gym.train --stats_file/--band uses to spend GRPO steps
                         only on the learnable band (DAPO-style curriculum, computed
                         once here instead of resampled every step).

Runs standalone on the GPU before SFT (vLLM batch inference — one call for the whole
dataset). CUDA-only, like all vLLM stages.

    python -m rl_gym.gym.raft --env iac --data_dir real --model s3://bucket/base/... \
        --n 8 --out_dir ./out/raft
"""
from __future__ import annotations

import argparse
import json
import os


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="iac")
    ap.add_argument("--data_dir", default=None)
    ap.add_argument("--model", required=True, help="HF id, local path, or s3://…")
    ap.add_argument("--n", type=int, default=8, help="samples per prompt")
    ap.add_argument("--temperature", type=float, default=0.9)
    ap.add_argument("--max_completion_len", type=int, default=1024)
    ap.add_argument("--keep", type=float, default=1.0,
                    help="min reward for a completion to become SFT data (1.0 = gates + "
                         "every applicable rule pass)")
    ap.add_argument("--limit", type=int, default=0, help="cap #episodes (0=all)")
    ap.add_argument("--out_dir", default="./out/raft")
    ap.add_argument("--gpu_mem", type=float, default=0.85,
                    help="vLLM gpu fraction — this stage owns the GPU (no colocate trainer)")
    args = ap.parse_args()

    from vllm import LLM, SamplingParams
    from transformers import AutoTokenizer
    from .registry import get_env
    from .core import score_completion
    from rl_gym.gym.s3io import materialize

    env = get_env(args.env, data_dir=args.data_dir)
    episodes = env.episodes("train")
    if args.limit and args.limit > 0:
        episodes = episodes[: args.limit]
    model_path = materialize(args.model)
    print(f"[gym.raft] env={args.env} | episodes={len(episodes)} | n={args.n} | model={model_path}")

    tok = AutoTokenizer.from_pretrained(model_path)
    chat = getattr(env, "use_chat_template", False) and tok.chat_template
    ctx = 2048 if args.max_completion_len <= 1024 else args.max_completion_len + 1024

    def render(ep):
        p = env.prompt(ep)
        if chat:
            return tok.apply_chat_template([{"role": "user", "content": p}],
                                           add_generation_prompt=True, tokenize=False)
        return p

    llm = LLM(model=model_path, gpu_memory_utilization=args.gpu_mem,
              max_model_len=ctx, enforce_eager=True)
    sp = SamplingParams(n=args.n, temperature=args.temperature,
                        max_tokens=args.max_completion_len)
    outs = llm.generate([render(ep) for ep in episodes], sp)

    os.makedirs(args.out_dir, exist_ok=True)
    kept, rows_sft, rows_stats = 0, [], []
    for i, (ep, out) in enumerate(zip(episodes, outs)):
        rewards = [score_completion(env, o.text, ep).reward for o in out.outputs]
        best_j = max(range(len(rewards)), key=lambda j: rewards[j])
        mean = sum(rewards) / max(1, len(rewards))
        frac_perfect = sum(r >= args.keep for r in rewards) / max(1, len(rewards))
        rows_stats.append({"idx": i, "id": ep.get("id", str(i)), "mean": round(mean, 4),
                           "max": round(rewards[best_j], 4),
                           "frac_perfect": round(frac_perfect, 4)})
        if rewards[best_j] >= args.keep:
            kept += 1
            rows_sft.append({"messages": [
                {"role": "user", "content": env.prompt(ep)},
                {"role": "assistant", "content": out.outputs[best_j].text},
            ]})

    with open(os.path.join(args.out_dir, "sft.jsonl"), "w") as f:
        for r in rows_sft:
            f.write(json.dumps(r) + "\n")
    with open(os.path.join(args.out_dir, "stats.jsonl"), "w") as f:
        for r in rows_stats:
            f.write(json.dumps(r) + "\n")

    means = [r["mean"] for r in rows_stats]
    band = sum(0.05 <= m <= 0.95 for m in means)
    print(f"[gym.raft] kept {kept}/{len(episodes)} episodes as SFT data (reward >= {args.keep})")
    print(f"[gym.raft] difficulty: mean-of-means {sum(means)/max(1,len(means)):.3f} | "
          f"learnable band (0.05..0.95): {band}/{len(means)}")
    print(f"[gym.raft] wrote {args.out_dir}/sft.jsonl + stats.jsonl")


if __name__ == "__main__":
    main()
