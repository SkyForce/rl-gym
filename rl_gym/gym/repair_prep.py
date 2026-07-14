"""Prep stage for the repair environment: roll turn 1 with the current checkpoint,
scan it, and write the repair/gen training mix.

For each train episode: one greedy turn-1 config from --model. Imperfect ones
(reward < 1.0, parseable, config small enough to embed) become `repair` episodes
carrying the config + the scanner's findings; a slice of plain `gen` episodes is
mixed in so the generation skill doesn't drift while repair is learned.

    python -m rl_gym.gym.repair_prep --model s3://.../iac-grpo-v16r --out_dir ./out/repair
"""
from __future__ import annotations

import argparse
import json
import os


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="iac")
    ap.add_argument("--data_dir", default="real")
    ap.add_argument("--model", required=True, help="turn-1 generator (the checkpoint to improve)")
    ap.add_argument("--max_completion_len", type=int, default=1024)
    ap.add_argument("--max_prev_chars", type=int, default=2800,
                    help="skip repair episodes whose turn-1 config exceeds this (prompt budget)")
    ap.add_argument("--gen_frac", type=float, default=0.4,
                    help="fraction of plain generation episodes mixed into the train file")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out_dir", default="./out/repair")
    ap.add_argument("--gpu_mem", type=float, default=0.85)
    args = ap.parse_args()

    from vllm import LLM, SamplingParams
    from transformers import AutoTokenizer
    from .registry import get_env
    from .core import score_completion
    from rl_gym.gym.s3io import materialize
    from ..iac.repair import findings_text

    env = get_env(args.env, data_dir=args.data_dir)
    episodes = env.episodes("train")
    if args.limit and args.limit > 0:
        episodes = episodes[: args.limit]
    model_path = materialize(args.model)
    print(f"[repair_prep] turn-1 rollout: {len(episodes)} episodes with {model_path}")

    tok = AutoTokenizer.from_pretrained(model_path)
    chat = getattr(env, "use_chat_template", False) and tok.chat_template

    def render(ep):
        p = env.prompt(ep)
        return (tok.apply_chat_template([{"role": "user", "content": p}],
                                        add_generation_prompt=True, tokenize=False)
                if chat else p)

    llm = LLM(model=model_path, gpu_memory_utilization=args.gpu_mem,
              max_model_len=2048, enforce_eager=True)
    outs = llm.generate([render(ep) for ep in episodes],
                        SamplingParams(temperature=0.0, max_tokens=args.max_completion_len))

    repair, perfect, skipped = [], 0, 0
    for ep, out in zip(episodes, outs):
        completion = out.outputs[0].text
        rb = score_completion(env, completion, ep)
        hcl = env.parse(completion, ep)
        if rb.reward >= 1.0:
            perfect += 1
            continue
        if not hcl or len(hcl) > args.max_prev_chars:
            skipped += 1
            continue
        repair.append({"mode": "repair", "id": f"rep-{ep.get('id','?')}", "req": ep["req"],
                       "required": list(ep["required"]), "oracle": ep.get("oracle", ""),
                       "prev_hcl": hcl.strip(), "findings": findings_text(hcl, ep),
                       "turn1_reward": round(rb.reward, 4)})

    n_gen = int(len(repair) * args.gen_frac / max(1e-9, 1 - args.gen_frac))
    gen = [{"mode": "gen", "id": ep.get("id", str(i)), "req": ep["req"],
            "required": list(ep["required"]), "oracle": ep.get("oracle", "")}
           for i, ep in enumerate(episodes[:n_gen])]
    mix = repair + gen
    import random
    random.Random(0).shuffle(mix)

    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(args.out_dir, "repair_train.jsonl")
    with open(out_path, "w") as f:
        for r in mix:
            f.write(json.dumps(r) + "\n")
    t1 = [r["turn1_reward"] for r in repair]
    print(f"[repair_prep] turn-1: {perfect} perfect / {len(repair)} repairable "
          f"(mean turn-1 reward {sum(t1)/max(1,len(t1)):.3f}) / {skipped} skipped (unparseable/too long)")
    print(f"[repair_prep] wrote {len(mix)} episodes ({len(repair)} repair + {len(gen)} gen) -> {out_path}")


if __name__ == "__main__":
    main()
