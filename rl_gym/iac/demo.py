"""Live demo — infra request in, secure Terraform out, security scorecard on top.

Shows the trained agent against the base model on the same request, with the
scanner's rule-by-rule verdict (the verifiable reward, visualized): pass/fail per
rule with severity, the two hard gates, and the final reward. The tuned model's
win is legible in one glance: criticals -> 0, gates FAIL -> PASS, reward up.

Best-parameter defaults are baked in: the v12 GRPO checkpoint from S3, greedy
decoding, 1024-token budget, chat template (matches training).

    # full demo (GPU or patient Mac; downloads the 7B from S3 on first run)
    python -m rl_gym.iac.demo --req "an S3 bucket for invoices with KMS encryption"

    # offline rendering check, no model needed (oracle vs bare-minimum policies)
    python -m rl_gym.iac.demo --canned --episode 3
"""
from __future__ import annotations

import argparse

from .env import IacEnv
from .scan import scan, RULES
from ..gym.core import score_completion

DEFAULT_TUNED = "s3://green-meadowlark-bucket-7/rl-gym-iac/iac-grpo"
DEFAULT_BASE = "Qwen/Qwen2.5-7B-Instruct"

# ANSI (kept dependency-free)
G, R, Y, B, DIM, END = "\033[92m", "\033[91m", "\033[93m", "\033[1m", "\033[2m", "\033[0m"
_SEV = {"critical": R, "high": Y, "medium": Y, "low": DIM}


def scorecard(env, ep, completion: str, label: str, show_hcl: bool) -> float:
    rb = score_completion(env, completion, ep)
    hcl = env.parse(completion, ep) or ""
    print(f"\n{B}── {label} " + "─" * max(1, 58 - len(label)) + END)
    if show_hcl and hcl:
        print(DIM + hcl.strip()[:2000] + (" …" if len(hcl) > 2000 else "") + END)
    if not rb.valid:
        print(f"{R}✗ malformed output (no resources parsed) -> reward 0.0{END}")
        return 0.0
    res = scan(hcl)
    for name, sev, fn in RULES:
        v = fn(hcl)
        if v == "na":
            continue
        mark = f"{G}✓{END}" if v == "pass" else f"{_SEV[sev]}✗{END}"
        print(f"  {mark} {name:<18} {DIM}{sev}{END}")
    bar = int(res["pass_rate"] * 24)
    print(f"  security {G}{'█' * bar}{DIM}{'░' * (24 - bar)}{END} {res['pass_rate']:.2f}")
    for gname, ok in rb.gates.items():
        print(f"  gate {gname:<16} " + (f"{G}PASS{END}" if ok else f"{R}FAIL -> reward zeroed{END}"))
    print(f"  {B}reward {rb.reward:.3f}{END}")
    return rb.reward


def main():
    ap = argparse.ArgumentParser(description="rl-gym IaC demo: request -> secure Terraform + scorecard")
    ap.add_argument("--req", default="", help="free-text infra request (else --episode from IaC-Eval)")
    ap.add_argument("--required", default="",
                    help="comma-separated resource types the builds_required gate demands "
                         "(with --req; empty = gate only on no_critical)")
    ap.add_argument("--episode", type=int, default=0, help="pick a real IaC-Eval dev episode")
    ap.add_argument("--episodes", default="", help="comma list of dev episodes (one model load, many scorecards)")
    ap.add_argument("--tuned", default=DEFAULT_TUNED, help="tuned checkpoint (s3://, local, HF id)")
    ap.add_argument("--base", default=DEFAULT_BASE, help="baseline model ('' to skip the comparison)")
    ap.add_argument("--canned", action="store_true",
                    help="no models: oracle vs bare-minimum policies (offline rendering check)")
    ap.add_argument("--hide_hcl", action="store_true")
    args = ap.parse_args()

    env = IacEnv(data_dir=None if args.req else "real")
    if args.req:
        eps = [{"id": "custom", "req": args.req,
                "required": [t for t in args.required.split(",") if t], "oracle": ""}]
    else:
        dev = env.episodes("dev")
        idxs = [int(i) for i in args.episodes.split(",") if i] or [args.episode]
        eps = [dev[i] for i in idxs]

    # build the policies ONCE (each is a 7B load) — then loop episodes
    if args.canned:
        policies = [("bare-minimum (random policy)", env.random)]
    else:
        from ..gym.eval import hf_policy
        policies = []
        if args.base:
            policies.append((f"BASE  {args.base}", hf_policy(env, args.base)))
        policies.append((f"TUNED {args.tuned}", hf_policy(env, args.tuned, tok_fallback=DEFAULT_BASE)))

    for ep in eps:
        print(f"\n{B}{'=' * 64}{END}")
        print(f"{B}REQUEST{END}  {ep['req'][:400]}")
        if ep["required"]:
            print(f"{DIM}must declare: {', '.join(ep['required'])}{END}")
        pairs = [(label, pol(ep)) for label, pol in policies]
        if args.canned and ep.get("oracle"):
            pairs.append(("reference (oracle)", "```hcl\n" + ep["oracle"] + "\n```"))
        rewards = [(label, scorecard(env, ep, text, label, not args.hide_hcl)) for label, text in pairs]
        if len(rewards) == 2:
            (l0, r0), (l1, r1) = rewards
            arrow = f"{G}▲{END}" if r1 > r0 else (f"{R}▼{END}" if r1 < r0 else "=")
            print(f"\n{B}VERDICT{END}  {r0:.3f} -> {r1:.3f} {arrow}\n")


if __name__ == "__main__":
    main()
