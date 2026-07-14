"""STaR / rejection-sampling for the KMS blind spot: sample the CURRENT model n times per
KMS episode at high temperature, keep only the completions the scanner VERIFIES as
kms_key_policy-passing (+ gates ok + decent reward), and write them as SFT targets.

This bootstraps the rare key-policy behavior from the model's OWN successes — no big model,
no GRPO wash-out. Each round's SFT raises the sampling rate, so the next round harvests more.
Also prints the observed policy-sampling rate, the metric that has to climb across rounds.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RLGYM_IAC_DISCOVERED_RULES", "1")   # score with the kms rule ON
import argparse
import json

from rl_gym.iac.env import IacEnv
from rl_gym.iac import scan as s
from rl_gym.iac.tasks import sample_episodes
from rl_gym.gym.core import score_completion

ap = argparse.ArgumentParser()
ap.add_argument("--model", required=True, help="current model (s3:// or local path)")
ap.add_argument("--out", required=True)
ap.add_argument("--n_episodes", type=int, default=150, help="KMS episodes to practice on")
ap.add_argument("--samples", type=int, default=20, help="n samples per episode")
ap.add_argument("--temp", type=float, default=1.0)
ap.add_argument("--ep_seed", type=int, default=0, help="episode pool (train distribution, != eval seed 7)")
ap.add_argument("--gen_seed", type=int, default=0, help="vary per round for fresh samples")
ap.add_argument("--keep_per_ep", type=int, default=4)
ap.add_argument("--min_reward", type=float, default=0.6)
ap.add_argument("--tok_fallback", default="ibm-granite/granite-4.1-8b")
args = ap.parse_args()

env = IacEnv(data_dir="real")
eps = [e for e in sample_episodes(5000, args.ep_seed)
       if "aws_kms_key" in (e.get("required") or [])][: args.n_episodes]
print(f"[star_sample] {len(eps)} KMS episodes · {args.samples} samples each · temp {args.temp} "
      f"· model {args.model}", flush=True)

from vllm import LLM, SamplingParams
from transformers import AutoTokenizer
from rl_gym.gym.s3io import materialize

p = materialize(args.model)
try:
    tok = AutoTokenizer.from_pretrained(p)
except Exception:
    tok = AutoTokenizer.from_pretrained(materialize(args.tok_fallback))
use_chat = getattr(env, "use_chat_template", False) and tok.chat_template
prompts = []
for e in eps:
    pr = env.prompt(e)
    prompts.append(tok.apply_chat_template([{"role": "user", "content": pr}],
                                           add_generation_prompt=True, tokenize=False)
                   if use_chat else pr)

sp = SamplingParams(temperature=args.temp, n=args.samples,
                    max_tokens=getattr(env, "max_new_tokens", 1024), seed=args.gen_seed)
llm = LLM(model=p, gpu_memory_utilization=0.85, max_model_len=2048, enforce_eager=True)
outs = llm.generate(prompts, sp)

total = pol_pass = kept = 0
with open(args.out, "w") as f:
    for e, o in zip(eps, outs):
        cands = []
        for c in o.outputs:
            total += 1
            hcl = env.parse(c.text, e) or ""
            if s._r_kms_key_policy(hcl) != "pass":
                continue
            pol_pass += 1
            rb = score_completion(env, c.text, e)
            if rb.gates and all(rb.gates.values()) and rb.reward >= args.min_reward:
                cands.append((rb.reward, hcl))
        cands.sort(reverse=True, key=lambda t: t[0])
        seen = set()
        for _, hcl in cands:
            if hcl in seen:
                continue
            seen.add(hcl)
            f.write(json.dumps({"messages": [
                {"role": "user", "content": env.prompt(e)},
                {"role": "assistant", "content": "```hcl\n" + hcl + "\n```"}]}) + "\n")
            kept += 1
            if len([x for x in seen]) >= args.keep_per_ep:
                break

rate = pol_pass / total if total else 0.0
print(f"[star_sample] observed policy-sampling rate = {pol_pass}/{total} ({rate:.1%})  ->  "
      f"kept {kept} verified SFT targets -> {args.out}", flush=True)
