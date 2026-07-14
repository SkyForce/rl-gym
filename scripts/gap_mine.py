"""Self-improving loop — the DISCOVERY step (honest version).

Generate the MODEL's own Terraform over real requests, run Checkov (the independent,
maintained verifier) over each config, and find where **Checkov flags an issue our
scanner does NOT** — i.e. our scanner rated the config clean on a dimension Checkov
failed. Those disagreements are coverage gaps the *traffic* reveals; ranked by frequency
they are the candidate new policies. The big/thinking model then drafts our-scanner
predicates for the top ones (gated by tests) — that step is rulegen, run after this.

Nobody hands it a CIS list: the policies emerge from (model traffic) x (Checkov). Runs in
a GPU job (vLLM) with Checkov installed. Prints a compact ranking (survives log tail) and
saves the full gap corpus + fail-example configs to S3 for the drafting step.

    python scripts/gap_mine.py --model s3://.../iac-grpo-fw1 --n 120 --out gapmine
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rl_gym.iac.env import IacEnv
from rl_gym.iac.scan import scan, RULES
from rl_gym.gym.s3io import materialize, upload_dir


def checkov_fails(hcl: str):
    """Checkov's failed checks on one config: (check_id, name, severity, resource)."""
    if not hcl:
        return []
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "main.tf"), "w").write(hcl)
        try:
            out = subprocess.run(["checkov", "-d", d, "-o", "json", "--compact",
                                  "--framework", "terraform"],
                                 capture_output=True, text=True, timeout=90).stdout.strip()
            if not out:
                return []
            data = json.loads(out)
        except Exception as e:
            print(f"[gap_mine] checkov run/parse error: {type(e).__name__}: {e}", file=sys.stderr)
            return []
    # checkov -o json is a LIST (one dict per framework) OR a single dict; be robust.
    fails = []
    for fw in (data if isinstance(data, list) else [data]):
        if not isinstance(fw, dict):
            continue
        res = fw.get("results") or {}
        for c in (res.get("failed_checks") or []):
            fails.append((c.get("check_id", "?"), c.get("check_name", ""),
                          str(c.get("severity", "") or "").lower(), c.get("resource", "")))
    return fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="s3:// or path to the model whose traffic we mine")
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("--split", default="dev")
    ap.add_argument("--out", default="gapmine", help="S3 prefix suffix for the gap corpus")
    ap.add_argument("--clean_thresh", type=float, default=0.8,
                    help="our pass_rate at/above which WE consider the config clean (so a "
                         "Checkov failure here is a gap WE missed)")
    args = ap.parse_args()

    env = IacEnv(data_dir="real")
    eps = env.episodes(args.split)[: args.n]

    # --- generate the model's traffic (vLLM, greedy) ---
    from vllm import LLM, SamplingParams
    from transformers import AutoTokenizer
    path = materialize(args.model)
    print(f"[gap_mine] loading {path}")
    llm = LLM(model=path, gpu_memory_utilization=0.85, max_model_len=2048, enforce_eager=True)
    tok = AutoTokenizer.from_pretrained(path)
    prompts = [tok.apply_chat_template([{"role": "user", "content": env.prompt(ep)}],
                                       add_generation_prompt=True, tokenize=False) for ep in eps]
    outs = llm.generate(prompts, SamplingParams(temperature=0.0, max_tokens=1024))
    configs = [env.parse(o.outputs[0].text, ep) or "" for o, ep in zip(outs, eps)]
    print(f"[gap_mine] generated {sum(1 for c in configs if c)} non-empty configs")

    our_rule_ids = {r[0] for r in RULES}
    gap = Counter()                 # checkov check_id -> # configs where WE missed it
    fires = Counter()               # checkov check_id -> total fires (context)
    meta = {}                       # check_id -> (name, sev)
    examples = defaultdict(list)    # check_id -> [fail-example configs]

    for ep, hcl in zip(eps, configs):
        if not hcl:
            continue
        our = scan(hcl)
        our_clean = our["pass_rate"] >= args.clean_thresh
        for cid, name, sev, res in checkov_fails(hcl):
            fires[cid] += 1
            meta[cid] = (name, sev)
            if our_clean:                       # Checkov failed a config WE rated clean = a gap
                gap[cid] += 1
                if len(examples[cid]) < 4:
                    examples[cid].append(hcl[:1800])

    # --- rank + persist ---
    ranked = gap.most_common()
    print("\n==================== DISCOVERED COVERAGE GAPS ====================")
    print("(Checkov fails a config OUR scanner rated clean — ranked by frequency)")
    print(f"{'CHECKOV ID':16s} {'GAP':>4s} {'FIRES':>5s} {'SEV':>8s}  NAME")
    for cid, g in ranked[:20]:
        name, sev = meta.get(cid, ("", ""))
        print(f"{cid:16s} {g:>4d} {fires[cid]:>5d} {sev:>8s}  {name[:60]}")
    print(f"\n{len(ranked)} distinct gap policies from {len(eps)} requests. "
          f"our scanner has {len(our_rule_ids)} rules.")

    corpus = {"model": args.model, "n": len(eps),
              "gaps": [{"check_id": cid, "gap": g, "fires": fires[cid],
                        "name": meta[cid][0], "severity": meta[cid][1],
                        "fail_examples": examples[cid]} for cid, g in ranked]}
    outdir = "/tmp/gapmine"
    os.makedirs(outdir, exist_ok=True)
    json.dump(corpus, open(os.path.join(outdir, "gaps.json"), "w"), indent=1)
    if os.environ.get("S3_BUCKET"):
        prefix = f"{os.environ.get('S3_PREFIX', 'rl-gym-iac')}/{args.out}"
        try:
            upload_dir(outdir, prefix)
            print(f"[gap_mine] gap corpus -> s3://{os.environ['S3_BUCKET']}/{prefix}/gaps.json")
        except Exception as e:
            print(f"[gap_mine] S3 upload skipped ({type(e).__name__}: {e})")


if __name__ == "__main__":
    main()
